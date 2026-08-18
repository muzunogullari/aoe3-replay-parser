#!/usr/bin/env python3
"""AoE3 Definitive Edition replay parser.

Extracts the full game history from a .age3Yrec replay into plain text:
game setup, players, a timestamped chronological log (chat, age-ups, completed
upgrades, shipments, tributes, danger flares, tech research), units trained,
buildings placed, resigns and the winning team.

Usage:
    python aoe3_replay_parser.py                  # latest replay, print to stdout
    python aoe3_replay_parser.py path\to\file.age3Yrec
    python aoe3_replay_parser.py -o history.txt   # write to file

No dependencies beyond the Python 3.8+ standard library.

How it works:
- .age3Yrec = 'l33t' magic + zlib stream containing a world snapshot plus the
  command stream.
- The snapshot embeds the runtime protoy.xml and techtreey.xml as XMB
  documents; unit names are resolved from proto 'id' attributes and tech names
  from their ordinal position in techtreey (both verified against in-game
  events). Because the tables come from the replay itself, the parser tracks
  game patches automatically.
- Command-stream framing (entry delimiter, header skip table, sub-command
  payload sizes) ported from github.com/h3902340/aoe3de-replay-parser (MIT).
  Command 1 = research tech (techtree ordinal), command 2 = proto-target
  command (train unit, and also wall-segment placement / buildable mines /
  map-object interaction; proto -1 with mode 2 = send home-city shipment),
  command 3 = place building, command 16 = resign. Time deltas are
  milliseconds of game time.
"""
import argparse
import datetime
import glob
import json
import os
import re
import struct
import sys
import zlib
from collections import Counter, defaultdict

SAVE_GLOB = os.path.expanduser(r"~\Games\Age of Empires 3 DE\*\Savegame\*.age3Yrec")

GAME_KEYS = [
    "gamename", "gamenumplayers", "gamemapname", "gamefilename", "gamemapsize",
    "gamedifficulty", "gamespeed", "gamestartingage", "gameendingage",
    "gamemodetype", "gamemapvisibility", "gamefreeforall", "gameteamlock",
    "gamerandomseed", "gamestartwithtreaty", "gamemapresources", "gamenorush",
    "gamekoth", "gametrademonopoly", "gameblockade",
]
PLAYER_FIELDS = [
    "name", "teamid", "color", "civ", "type", "hclevel", "hcfilename",
    "homecityname", "explorername", "id",
]

CIV_NAMES = {
    1: "Spanish", 2: "British", 3: "French", 4: "Portuguese", 5: "Dutch",
    6: "Russians", 7: "Germans", 8: "Ottomans", 9: "Haudenosaunee", 10: "Lakota",
    11: "Aztecs", 12: "Chinese", 13: "Japanese", 14: "Indians", 15: "Inca",
    16: "Swedes", 17: "United States", 18: "Ethiopians", 19: "Hausa",
    20: "Mexicans", 21: "Italians", 22: "Maltese",
}

ICON_PAT = re.compile(r'<icon="[^"]*"> ?')
COORD_PAT = re.compile(r"^(-?\d+\.\d+) (-?\d+\.\d+) (-?\d+\.\d+) \d+$")

# proto-target commands that are placements/interactions rather than unit
# training (wall pieces, buildable treaty mines, map objects)
PLACEMENT_PAT = re.compile(r"Wall|Buildable|Nugget|Socket|Prop|Token|SPC", re.IGNORECASE)


def find_latest_replay():
    files = glob.glob(SAVE_GLOB)
    if not files:
        sys.exit(f"No .age3Yrec replays found under {SAVE_GLOB}")
    return max(files, key=os.path.getmtime)


def load(path):
    raw = open(path, "rb").read()
    if raw[:4] != b"l33t":
        sys.exit(f"{path} is not an l33t-compressed AoE3 record")
    return zlib.decompress(raw[8:])


# ---------------------------------------------------------------- settings

def _find_key(data, name):
    b = name.encode("utf-16-le")
    return data.find(struct.pack("<I", len(name)) + b)


def _read_value(data, pos):
    n = struct.unpack_from("<I", data, pos)[0]
    p = pos + 4 + 2 * n
    tag = struct.unpack_from("<I", data, p)[0]
    p += 4
    if tag == 9:  # string
        sl = struct.unpack_from("<I", data, p)[0]
        if sl > 5000:
            return None
        return data[p + 4:p + 4 + 2 * sl].decode("utf-16-le", "replace")
    if tag == 2:
        return struct.unpack_from("<i", data, p)[0]
    if tag == 1:
        return round(struct.unpack_from("<f", data, p)[0], 3)
    if tag == 5:
        return bool(data[p])
    return None


def parse_settings(data):
    game = {}
    for k in GAME_KEYS:
        off = _find_key(data, k)
        if off >= 0:
            game[k] = _read_value(data, off)
    players = {}
    for pn in range(1, 13):
        row = {}
        for f in PLAYER_FIELDS:
            off = _find_key(data, f"gameplayer{pn}{f}")
            if off >= 0:
                row[f] = _read_value(data, off)
        if row.get("id", -1) == -1:
            continue
        m = re.match(r"sp_(\w+?)_homecity", row.get("hcfilename") or "")
        if m:
            row["civname"] = re.sub(r"^DE", "", m.group(1))
        else:
            row["civname"] = CIV_NAMES.get(row.get("civ"), f"civ {row.get('civ')}")
        players[row["id"]] = row
    return game, players


# ------------------------------------------------------- embedded XMB tables

def _find_xmb_docs(data, limit=4):
    docs = []
    p = 0
    while len(docs) < limit:
        p = data.find(b"X1", p)
        if p == -1:
            break
        if data[p + 6:p + 8] == b"XR":
            docs.append(p)
        p += 2
    return docs


def _xmb_tables(data, off):
    """Return (elements, attrs, body_pos) for the XMB document at off."""
    p = off + 8 + 8
    ne = struct.unpack_from("<I", data, p)[0]; p += 4
    elements = []
    for _ in range(ne):
        n = struct.unpack_from("<I", data, p)[0]; p += 4
        elements.append(data[p:p + 2 * n].decode("utf-16-le", "replace")); p += 2 * n
    na = struct.unpack_from("<I", data, p)[0]; p += 4
    attrs = []
    for _ in range(na):
        n = struct.unpack_from("<I", data, p)[0]; p += 4
        attrs.append(data[p:p + 2 * n].decode("utf-16-le", "replace")); p += 2 * n
    return elements, attrs, p


def _parse_node(data, elements, attrs, p, visit, depth=0):
    length = struct.unpack_from("<I", data, p + 2)[0]
    q = p + 6
    tn = struct.unpack_from("<I", data, q)[0]; q += 4
    text = data[q:q + 2 * tn].decode("utf-16-le", "replace"); q += 2 * tn
    name_id = struct.unpack_from("<I", data, q)[0]; q += 4
    q += 4  # line number
    nat = struct.unpack_from("<I", data, q)[0]; q += 4
    a = {}
    for _ in range(nat):
        aid = struct.unpack_from("<I", data, q)[0]; q += 4
        vn = struct.unpack_from("<I", data, q)[0]; q += 4
        a[attrs[aid]] = data[q:q + 2 * vn].decode("utf-16-le", "replace"); q += 2 * vn
    nc = struct.unpack_from("<I", data, q)[0]; q += 4
    elem = elements[name_id] if name_id < len(elements) else "?"
    recurse = visit(elem, a, text, depth)
    if recurse:
        for _ in range(nc):
            q = _parse_node(data, elements, attrs, q, visit, depth + 1)
    return p + 6 + length


def build_name_tables(data):
    """From the embedded runtime protoy/techtreey XMB documents:
    proto id -> unit name, tech ordinal -> tech name, and the simulation
    data needed for spend reconstruction: per-unit cost + train batch size
    and per-tech cost."""
    protos, techs = {}, []
    unit_info = {}   # proto id -> {"cost": {res: amt}, "batch": int}
    tech_costs = []  # ordinal -> {res: amt}
    for off in _find_xmb_docs(data, limit=4):
        try:
            elements, attrs, body = _xmb_tables(data, off)
        except (struct.error, IndexError):
            continue
        root = elements[0] if elements else ""
        if root == "proto":
            state = {"cur": None, "act": None}

            def visit(elem, a, text, depth):
                def num(default=0.0):
                    try:
                        return float(text)
                    except ValueError:
                        return default
                if elem == "unit" and "id" in a and "name" in a:
                    try:
                        pid = int(a["id"])
                    except ValueError:
                        return False
                    protos[pid] = a["name"]
                    state["cur"] = unit_info.setdefault(
                        pid, {"cost": {}, "batch": 1, "hp": 0.0, "initres": 0.0,
                              "actions": []})
                    state["act"] = None
                    return True
                cu = state["cur"]
                if cu is None:
                    return depth == 0
                if depth == 2:
                    if elem == "cost" and "resourcetype" in a:
                        cu["cost"][a["resourcetype"]] = num()
                    elif elem == "trainbatchsize":
                        cu["batch"] = max(1, int(num(1)))
                    elif elem == "maxhitpoints":
                        cu["hp"] = num()
                    elif elem == "trainpoints":
                        cu["trainpoints"] = num()
                    elif elem == "buildlimit":
                        cu["buildlimit"] = int(num(0))
                    elif elem == "initialresource":
                        cu["initres"] = num()
                    elif elem == "protoaction":
                        state["act"] = {"name": None, "damage": 0.0, "rof": 0.0,
                                        "rates": {}}
                        cu["actions"].append(state["act"])
                        return True
                    return False
                if depth == 3 and state["act"] is not None:
                    act = state["act"]
                    if elem == "name":
                        act["name"] = text
                    elif elem == "damage":
                        act["damage"] = num()
                    elif elem == "rof":
                        act["rof"] = num()
                    elif elem == "rate" and "type" in a:
                        act["rates"][a["type"]] = num()
                return depth == 0
        elif root == "techtree":
            tstate = {"cur": None, "pend": None}

            def visit(elem, a, text, depth):
                if elem == "tech" and depth == 1:
                    techs.append(a.get("name") or "?")
                    tstate["cur"] = {"cost": {}, "gather": [], "combat": [],
                                     "units": [], "settler_mods": []}
                    tstate["pend"] = None
                    tech_costs.append(tstate["cur"])
                    return True
                cu = tstate["cur"]
                if cu is None:
                    return depth == 0
                if depth == 2:
                    if elem == "cost" and "resourcetype" in a:
                        try:
                            cu["cost"][a["resourcetype"]] = float(text)
                        except ValueError:
                            pass
                    elif elem == "effects":
                        return True
                    return False
                if depth == 3 and elem == "effect" and a.get("type") == "Data":
                    try:
                        amt = float(a.get("amount", "1"))
                    except ValueError:
                        return False
                    sub = a.get("subtype")
                    if sub == "WorkRate" and a.get("action") == "Gather":
                        cu["gather"].append((a.get("unittype", ""), amt))
                    elif sub in ("Damage", "Hitpoints"):
                        cu["combat"].append((sub, a.get("unittype", ""), amt))
                    elif sub == "FreeHomeCityUnit":
                        cu["units"].append((a.get("unittype", ""), int(amt)))
                    elif sub in ("TrainPoints", "BuildLimit"):
                        tstate["pend"] = (sub, amt)
                        return True
                    return False
                if depth == 4 and elem == "target" and tstate["pend"]:
                    if "Settler" in text or "Villager" in text or "Coureur" in text:
                        cu["settler_mods"].append(tstate["pend"])
                    tstate["pend"] = None
                return depth == 0
        else:
            continue
        sys.setrecursionlimit(100000)
        try:
            _parse_node(data, elements, attrs, body, visit)
        except (struct.error, IndexError, RecursionError):
            pass
        if protos and techs:
            break
    for info in unit_info.values():
        dps, gather = 0.0, {}
        for act in info.pop("actions", []):
            if (act["damage"] > 0 and act["rof"] > 0
                    and act["name"] not in ("Build", "ChopAttack", "BuildingAttack",
                                            "HandAttackCrate", "SpearAttack")):
                dps = max(dps, act["damage"] / act["rof"])
            if act["name"] == "Gather":
                gather.update(act["rates"])
        info["dps"] = round(dps, 2)
        info["gather"] = gather
    return protos, techs, unit_info, tech_costs


# --------------------------------------------- game data (Data.bar) support

BAR_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\AoE3DE\Game\Data\Data.bar",
    r"C:\Program Files\Steam\steamapps\common\AoE3DE\Game\Data\Data.bar",
]
CIV_HC = {"Russians": "homecityrussians", "British": "homecitybritish",
          "Ottomans": "homecityottomans", "French": "homecityfrench",
          "Germans": "homecitygerman", "Dutch": "homecitydutch",
          "Spanish": "homecityspanish", "Portuguese": "homecityportuguese",
          "Swedish": "homecityswedish", "Americans": "homecityamericans",
          "Mexicans": "homecitymexicans", "Italians": "homecityitalians",
          "Maltese": "homecitymaltese", "Chinese": "homecitychinese",
          "Japanese": "homecityjapanese", "Indians": "homecityindians",
          "Inca": "homecitydeinca", "Ethiopians": "homecityethiopians",
          "Hausa": "homecityhausa", "Haudenosaunee": "homecityxpiroquois",
          "Lakota": "homecityxpsioux", "Aztecs": "homecityxpaztec"}


def _lz4_block(src, usize):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n and len(dst) < usize:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]; i += lit
        if i >= n or len(dst) >= usize:
            break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while True:
                b = src[i]; i += 1
                ml += b
                if b != 255:
                    break
        start = len(dst) - off
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst)


def read_bar_file(name_want):
    """Read one file out of the game's Data.bar (None if game not installed)."""
    bar = next((b for b in BAR_CANDIDATES if os.path.exists(b)), None)
    if not bar:
        return None
    try:
        with open(bar, "rb") as f:
            f.seek(0x120)
            table_off = struct.unpack("<Q", f.read(8))[0]
            f.seek(table_off)
            buf = f.read()
        p = 0
        rl = struct.unpack_from("<I", buf, p)[0]; p += 4 + 2 * rl
        count = struct.unpack_from("<I", buf, p)[0]; p += 4
        for _ in range(count):
            off, sz1, sz2, sz3, nl = struct.unpack_from("<QIIII", buf, p); p += 24
            name = buf[p:p + 2 * nl].decode("utf-16-le"); p += 2 * nl
            p += 4
            if name.lower() == name_want.lower():
                with open(bar, "rb") as f:
                    f.seek(off)
                    hdr = f.read(16)
                    if hdr[:4] == b"alz4":
                        usize, csize = struct.unpack_from("<II", hdr, 4)
                        f.seek(off + 16)
                        return _lz4_block(f.read(csize), usize)
                    f.seek(off)
                    return f.read(sz1)
    except (OSError, struct.error):
        return None
    return None


def hc_card_order(civname):
    """The civ's home-city card list: name -> (age, position). Slot order in
    the in-game shipment panel is deck cards sorted by (age, position)."""
    fname = CIV_HC.get(civname)
    if not fname:
        return None
    xmb = read_bar_file(fname + ".xml.XMB")
    if not xmb or xmb[:2] != b"X1":
        return None
    try:
        elements, attrs, body = _xmb_tables(xmb, 0)
    except (struct.error, IndexError):
        return None
    cards = {}
    state = {"cur": None, "pos": 0}

    def visit(elem, a, text, depth):
        if elem == "card":
            state["cur"] = {"name": None, "age": 0}
            return True
        if state["cur"] is not None:
            if elem == "name" and state["cur"]["name"] is None:
                state["cur"]["name"] = text
            elif elem == "age":
                try:
                    state["cur"]["age"] = int(text)
                except ValueError:
                    pass
                if state["cur"]["name"] and state["cur"]["name"] not in cards:
                    cards[state["cur"]["name"]] = (state["cur"]["age"], state["pos"])
                    state["pos"] += 1
        return depth <= 2
    sys.setrecursionlimit(100000)
    try:
        _parse_node(xmb, elements, attrs, body, visit)
    except (struct.error, IndexError, RecursionError):
        pass
    return cards or None


# ---------------------------------------------------------------- decks

def parse_decks(data, techs):
    """Home-city decks serialized in the replay: '\\x00\\x00\\x00Dk' marker,
    then id, name, game id, card list (techtree ordinals)."""
    decks = []
    pos = 0
    while True:
        pos = data.find(b"\x00\x00\x00\x44\x6b", pos)
        if pos == -1:
            break
        p = pos + 9
        if struct.unpack_from("<i", data, p)[0] != 5:
            pos += 1
            continue
        p += 8
        nlen = struct.unpack_from("<i", data, p)[0]; p += 4
        if not (0 < nlen < 60):
            pos += 1
            continue
        name = data[p:p + 2 * nlen].decode("utf-16-le", "replace"); p += 2 * nlen
        game_id = struct.unpack_from("<i", data, p)[0]; p += 4 + 2
        cc = struct.unpack_from("<i", data, p)[0]; p += 4
        if not (0 <= cc <= 40):
            pos += 1
            continue
        cards = struct.unpack_from(f"<{cc}i", data, p)
        decks.append({"off": pos, "name": name, "game_id": game_id,
                      "cards": [techs[c] if 0 <= c < len(techs) else f"card#{c}"
                                for c in cards]})
        pos += 1
    return decks


# ------------------------------------------------------- start-world objects

def parse_start_objects(data, protos):
    """Objects present at game start: instance id -> (proto id, owner).
    Serialized as '01 4b 39' records in the initial world snapshot."""
    objects = {}
    p = 0
    while True:
        p = data.find(b"\x01\x4b\x39", p)
        if p == -1 or p + 16 > len(data):
            break
        inst, pid = struct.unpack_from("<II", data, p + 7)
        owner = data[p + 15]
        if pid in protos and owner <= 12 and 0 < inst < 50_000_000:
            x, z = struct.unpack_from("<ff", data, p + 17)
            if not (0 <= x <= 10000 and 0 <= z <= 10000):
                x = z = None
            objects.setdefault(inst, (pid, owner, x, z))
        p += 3
    return objects


# ---------------------------------------------------------------- commands

DELIM = bytes([0x1, 0, 0, 0, 0, 0, 0, 0, 0, 0x19])
NO_SUB = {1, 129, 3, 5, 9, 131, 133, 137, 7, 11, 13, 135, 139, 141, 15, 143,
          17, 145, 19, 21, 25, 147, 149, 153, 23, 27, 29, 151, 155, 157, 31, 159}
HEADER_SKIP = {}
for _size, _ids in [(0, {33, 65, 161, 193, 1, 129}),
                    (4, {35, 37, 41, 67, 73, 163, 165, 169, 195, 201, 3, 5, 9, 131, 133, 137}),
                    (8, {39, 43, 45, 75, 167, 171, 173, 203, 7, 11, 13, 135, 139, 141}),
                    (12, {47, 175, 207, 15, 143}), (36, {49, 177, 17, 145}),
                    (40, {19, 21, 25, 147, 149, 153, 51, 53, 57, 179, 181, 185}),
                    (44, {55, 59, 61, 183, 187, 189, 23, 27, 29, 151, 155, 157}),
                    (48, {63, 191, 223, 31, 159})]:
    for _c in _ids:
        HEADER_SKIP[_c] = _size
INT_COUNT = {65, 67, 73, 75, 193, 195, 201, 203, 207, 223}
SUB_SIZE = {4: 25, 6: 36, 7: 1, 9: 0, 13: 12, 18: 4, 19: 17, 23: 6, 24: 12,
            25: 6, 26: 4, 34: 0, 35: 4, 37: 5, 44: 8, 46: 8, 48: 9, 53: 8,
            57: 12, 58: 4, 61: 8, 62: 4, 63: 16, 64: 0, 65: 4, 67: 12, 71: 4,
            72: 16, 73: 0, 80: 8}


def parse_commands(data):
    def i32(p):
        return struct.unpack_from("<i", data, p)[0]

    def f32(p):
        return struct.unpack_from("<f", data, p)[0]

    out = {"messages": [], "resigns": [], "trains": [], "builds": [],
           "techs": [], "shipments": [], "orders": [], "tributes": [],
           "markets": [], "prod_sels": [], "duration": 0}
    duration = 0
    pos = data.find(DELIM)
    while True:
        pos = data.find(DELIM, pos)
        if pos == -1:
            break
        pos += 113
        command = data[pos]; pos += 1
        if command not in HEADER_SKIP:
            continue
        pos += HEADER_SKIP[command]
        mc = i32(pos); pos += 4
        if mc < 0 or mc > 100:
            continue
        for _ in range(mc):
            frm, to = i32(pos), i32(pos + 4); pos += 8
            bl = i32(pos); pos += 4
            msg = data[pos:pos + 2 * bl].decode("utf-16-le", "replace")
            pos += 2 * bl + 1
            out["messages"].append({"t": duration, "from": frm, "to": to, "msg": msg})
        duration += data[pos]; pos += 1
        if command in NO_SUB:
            continue
        cc = i32(pos) if command in INT_COUNT else data[pos]
        pos += 4 if command in INT_COUNT else 1
        for _ in range(cc):
            pos += 1
            cmd_id = i32(pos); pos += 4
            if cmd_id == 14:
                pos += 12
            pos += 1
            player = i32(pos); pos += 4
            pos += 16
            unknown0 = i32(pos - 4)
            if unknown0 == 1:
                pos += 4
            unknown1 = i32(pos); pos += 4
            sel = i32(pos); pos += 4
            sel_ids = [i32(pos + 4 * k) for k in range(sel)]
            pos += 4 * sel
            u2 = i32(pos); pos += 4
            pos += u2 * 12
            uc = i32(pos); pos += 4
            pos += uc + 1 + 16 + 4
            if cmd_id in (1, 2) and sel:
                out["prod_sels"].extend(u for u in sel_ids if u > 100)
            if cmd_id == 0:
                size = 24 + (8 if data[pos + 24] == 255 else 0)
                target = i32(pos)
                out["orders"].append({
                    "t": duration, "p": player, "sel": sel, "ids": sel_ids,
                    "kind": "target" if target != -1 else "move",
                    "target": target if target != -1 else None,
                    "x": round(f32(pos + 8), 1), "z": round(f32(pos + 16), 1)})
            elif cmd_id in (4, 12, 13, 23, 24, 25, 34, 37, 46, 53, 57, 61, 63):
                out["orders"].append({"t": duration, "p": player, "sel": sel,
                                      "ids": sel_ids, "kind": "control",
                                      "cmd": cmd_id})
            if cmd_id == 0:
                pass
            elif cmd_id == 1:
                out["techs"].append({"t": duration, "p": player, "tech": i32(pos)})
                size = 4
            elif cmd_id == 2:
                proto = i32(pos)
                if proto == -1 and unknown1 == 2:
                    out["shipments"].append({"t": duration, "p": player, "card": i32(pos + 4)})
                elif proto != -1:
                    out["trains"].append({"t": duration, "p": player, "proto": proto})
                size = 14 + (2 if unknown1 in (0, 2) else 0)
            elif cmd_id == 3:
                out["builds"].append({"t": duration, "p": player, "proto": i32(pos),
                                      "x": round(f32(pos + 4), 1),
                                      "z": round(f32(pos + 12), 1)})
                size = 44
            elif cmd_id == 12:
                size = 36 + (1 if unknown1 == 0 else 0)
            elif cmd_id == 13:
                out["markets"].append({"t": duration, "p": player,
                                       "mode": i32(pos), "res": i32(pos + 4),
                                       "amount": round(f32(pos + 8))})
                size = 12
            elif cmd_id == 16:
                out["resigns"].append({"t": duration, "slot": i32(pos + 4)})
                size = 13
            elif cmd_id == 19:
                out["tributes"].append({"t": duration, "p": player,
                                        "res": i32(pos), "to": i32(pos + 4),
                                        "amount": round(f32(pos + 8))})
                size = 17
            elif cmd_id == 41:
                c1 = i32(pos)
                size = 20
                if c1 == 1:
                    size += 4
                    if i32(pos + 20) == 1:
                        size += 4
                    size += 13
            elif cmd_id == 66:
                size = 8
            elif cmd_id in SUB_SIZE:
                size = SUB_SIZE[cmd_id]
            else:
                size = 0
            pos += size
    out["duration"] = duration
    return out


# ---------------------------------------------------------------- report

def fmt_t(ms):
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def pname(players, slot):
    if slot == 0:
        return "SYSTEM"
    p = players.get(slot)
    return p["name"] if p else f"player{slot}"


RES_NAMES = {0: "coin", 1: "wood", 2: "food"}


def _cardnorm(nm):
    nm = re.sub(r"^(DE|RG|YP|XP)?HC(REV)?(Ship|XP)?", "", nm)
    nm = re.sub(r"(Team|Russian|British|Ottoman|French|German)$", "", nm)
    return re.sub(r"[^a-z]", "", nm.lower())


def _cards_match(a, b):
    from difflib import SequenceMatcher
    if not a or not b:
        return False
    return a == b or a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.8


def card_display(c):
    c = re.sub(r"^(DE|RG|YP|XP)?HC(REV)?(Ship|XP)?", "", c)
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", c)


def solve_selected_deck(cluster, sends, arrivals, age_at, hc_cards):
    """Pick the deck + slot ordering that explains the send/arrival record.
    Slot order = deck cards sorted by (card age, home-city list position)."""
    if not hc_cards:
        return None
    best = None
    for d in cluster:
        if len(d["cards"]) < 15:
            continue
        order = sorted(d["cards"],
                       key=lambda c: hc_cards.get(c, (9, 9999)))
        age_ok = all(s < len(order)
                     and hc_cards.get(order[s], (0, 0))[0] + 1 <= age_at(t)
                     for t, s in sends)
        matched = 0
        used = set()
        for at, an in arrivals:
            for st, s in sends:
                if (st, s) in used or st >= at or s >= len(order):
                    continue
                if _cards_match(_cardnorm(order[s]), re.sub(r"[^a-z]", "", an.lower())):
                    matched += 1
                    used.add((st, s))
                    break
        score = (age_ok, matched)
        if best is None or score > best[0]:
            best = (score, d, order)
    if best is None or best[0][1] < max(1, len(arrivals) // 3):
        return None
    return {"name": best[1]["name"], "slots": best[2],
            "matched_arrivals": best[0][1], "arrivals_total": len(arrivals),
            "age_consistent": best[0][0]}


def build_events(path, game, players, cmds, protos, techs, objects=None,
                 unit_info=None, tech_costs=None, decks=None):
    """Every action from the replay as one flat timestamped event list (JSON)."""
    objects = objects or {}
    unit_info = unit_info or {}
    tech_costs = tech_costs or []
    decks = decks or []

    # block-trained units whose batch size is engine-side, not in protoy
    # (validated against the distinct-unit-id population of the replay)
    KNOWN_BATCH = {"Strelet": 10, "Cossack": 5}

    def unit_cost(proto_id):
        info = unit_info.get(proto_id)
        if not info or not info["cost"]:
            return None, 1
        batch = max(info["batch"], KNOWN_BATCH.get(protos.get(proto_id, ""), 1))
        return {r: round(v * batch) for r, v in info["cost"].items()}, batch

    def player_of(slot):
        return None if slot == 0 else pname(players, slot)

    events = []
    for c in cmds["orders"]:
        e = {"t_ms": c["t"], "type": "order", "kind": c["kind"],
             "player_id": c["p"], "player": player_of(c["p"]), "units_selected": c["sel"]}
        if c["kind"] in ("move", "target"):
            e["x"], e["z"] = c["x"], c["z"]
        if c.get("target") is not None:
            e["target_id"] = c["target"]
            res = objects.get(c["target"])
            if res:
                e["target_unit"] = protos.get(res[0], f"unit#{res[0]}")
                e["target_owner"] = player_of(res[1]) or "Gaia"
        events.append(e)
    for m in cmds["messages"]:
        msg = ICON_PAT.sub("coin ", m["msg"]).strip()
        if not msg:
            continue
        cm = COORD_PAT.match(msg)
        if m["from"] != 0 and cm:
            events.append({"t_ms": m["t"], "type": "flare",
                           "player_id": m["from"], "player": player_of(m["from"]),
                           "x": float(cm.group(1)), "z": float(cm.group(3))})
        elif m["from"] == 0:
            events.append({"t_ms": m["t"], "type": "system",
                           "to_id": m["to"], "to": player_of(m["to"]), "text": msg})
            rm = re.match(r"(.+) has paid (\d+) coin to you as explorer ransom", msg)
            if rm:
                events.append({"t_ms": m["t"], "type": "explorer_ransom",
                               "player": rm.group(1), "amount": int(rm.group(2)),
                               "paid_to": player_of(m["to"])})
        else:
            events.append({"t_ms": m["t"], "type": "chat",
                           "player_id": m["from"], "player": player_of(m["from"]),
                           "to_id": m["to"], "text": msg})
    for c in cmds["trains"]:
        nm = protos.get(c["proto"], f"unit#{c['proto']}")
        cost, batch = unit_cost(c["proto"])
        e = {"t_ms": c["t"],
             "type": "placement" if PLACEMENT_PAT.search(nm) else "train",
             "player_id": c["p"], "player": player_of(c["p"]),
             "unit": nm, "proto_id": c["proto"], "count": batch}
        if cost:
            e["cost"] = cost
        events.append(e)
    for c in cmds["builds"]:
        cost, _ = unit_cost(c["proto"])
        e = {"t_ms": c["t"], "type": "build",
             "player_id": c["p"], "player": player_of(c["p"]),
             "building": protos.get(c["proto"], f"bldg#{c['proto']}"),
             "proto_id": c["proto"], "x": c["x"], "z": c["z"]}
        if cost:
            e["cost"] = cost
        events.append(e)
    for c in cmds["techs"]:
        tech = techs[c["tech"]] if 0 <= c["tech"] < len(techs) else f"tech#{c['tech']}"
        e = {"t_ms": c["t"], "type": "research",
             "player_id": c["p"], "player": player_of(c["p"]),
             "tech": tech, "tech_id": c["tech"]}
        if 0 <= c["tech"] < len(tech_costs) and tech_costs[c["tech"]]["cost"]:
            e["cost"] = {r: round(v) for r, v in tech_costs[c["tech"]]["cost"].items()}
        events.append(e)
    for c in cmds["shipments"]:
        events.append({"t_ms": c["t"], "type": "shipment",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "card_slot": c["card"]})
    for c in cmds["tributes"]:
        events.append({"t_ms": c["t"], "type": "tribute",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "to_id": c["to"], "to": player_of(c["to"]),
                       "resource": RES_NAMES.get(c["res"], f"res{c['res']}"),
                       "amount": c["amount"]})
    for c in cmds["markets"]:
        events.append({"t_ms": c["t"], "type": "market",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "mode": "buy" if c["mode"] == 1 else "sell",
                       "resource": RES_NAMES.get(c["res"], f"res{c['res']}"),
                       "amount": c["amount"]})
    for r in cmds["resigns"]:
        events.append({"t_ms": r["t"], "type": "resign",
                       "player_id": r["slot"], "player": player_of(r["slot"])})
    events.sort(key=lambda e: e["t_ms"])
    for e in events:
        e["t"] = fmt_t(e["t_ms"])

    start_objects = []
    start_res = Counter()
    start_vills = Counter()
    for inst, (pid, owner, x, z) in objects.items():
        nm = protos.get(pid, "")
        if not (1 <= owner <= 12):
            continue
        if "Crate" in nm:
            start_res[owner] += round(unit_info.get(pid, {}).get("initres", 0))
            continue
        if nm == "Coureur" or nm.startswith("Settler") or "Villager" in nm:
            start_vills[owner] += 1
        if x is not None and not re.search(r"Flag", nm):
            start_objects.append({"player_id": owner, "unit": nm,
                                  "x": round(x, 1), "z": round(z, 1)})
    for pid in start_vills:
        start_res[pid] += STANDARD_START_RES

    resigned = {r["slot"] for r in cmds["resigns"]}
    plist = [{"id": pid, "name": p.get("name"), "civ": p.get("civname"),
              "team": p.get("teamid")}
             for pid, p in sorted(players.items())]
    side_of = {}
    for si, side in enumerate(_sides(plist)):
        for p in side:
            side_of[p["id"]] = si

    # deck clusters -> players, by civ-specific card names
    clusters = []
    for d in sorted(decks, key=lambda d: d["off"]):
        if clusters and d["off"] - clusters[-1][-1]["off"] < 0x40000:
            clusters[-1].append(d)
        else:
            clusters.append([d])
    player_clusters = {}
    for cl in clusters:
        text = " ".join(c for d in cl for c in d["cards"])
        best, best_n = None, 0
        for p in plist:
            hint = re.sub(r"s$", "", p["civ"] or "")
            n = text.count(hint) if hint else 0
            if n > best_n:
                best, best_n = p["id"], n
        if best is not None and best not in player_clusters:
            player_clusters[best] = cl

    # selected deck per player + shipment card naming
    age_msgs = defaultdict(list)
    for e in events:
        if e["type"] == "system":
            m = re.search(r"(.+) has reached the (\w+) AGE", e["text"])
            if m:
                age_msgs[m.group(1)].append(
                    (e["t_ms"], {"COMMERCE": 2, "FORTRESS": 3,
                                 "INDUSTRIAL": 4, "IMPERIAL": 5}.get(m.group(2), 2)))
    hc_cache = {}
    selected_decks = {}
    for p in plist:
        pid = p["id"]
        sends = [(e["t_ms"], e["card_slot"]) for e in events
                 if e["type"] == "shipment" and e["player_id"] == pid]
        if not sends or pid not in player_clusters:
            continue
        arrivals = [(e["t_ms"], e["text"].split(" Shipment has arrived")[0])
                    for e in events if e["type"] == "system"
                    and "Shipment has arrived" in e["text"] and e["to_id"] == pid]
        my_ages = sorted(age_msgs.get(p["name"], []))

        def age_at(t, ages=my_ages):
            a = 1
            for at, av in ages:
                if at <= t:
                    a = av
            return a
        civ = p["civ"]
        if civ not in hc_cache:
            hc_cache[civ] = hc_card_order(civ)
        solved = solve_selected_deck(player_clusters[pid], sends, arrivals,
                                     age_at, hc_cache[civ])
        if solved:
            selected_decks[pid] = solved
            order = solved["slots"]
            for e in events:
                if e["type"] == "shipment" and e["player_id"] == pid:
                    s = e["card_slot"]
                    if s < len(order):
                        e["card"] = order[s]
                        e["card_name"] = card_display(order[s])

    # unit lifetime observations from every selection in every order
    obs = {}  # id -> [owner, first, last, sightings, gather_sightings]
    for o in cmds["orders"]:
        gather = 1 if (o["kind"] == "target" and o.get("target") in objects
                       and objects[o["target"]][1] == 0) else 0
        for u in o.get("ids", []):
            if u <= 0:
                continue
            r = obs.get(u)
            if r is None:
                obs[u] = [o["p"], o["t"], o["t"], 1, gather]
            else:
                r[2] = max(r[2], o["t"])
                r[3] += 1
                r[4] += gather
    prod_ids = set(cmds["prod_sels"])

    def _vill_name(nm):
        return nm == "Coureur" or nm.startswith("Settler") or "Villager" in nm

    # Veteran/Guard-style upgrades replace every unit of a type with a new
    # instance id; disappearances clustered around such a research are
    # re-instancing artifacts, not deaths.
    upgrade_times = defaultdict(list)
    for c in cmds["techs"]:
        if (0 <= c["tech"] < len(tech_costs)
                and tech_costs[c["tech"]].get("combat")):
            upgrade_times[c["p"]].append(c["t"])

    # a unit that stops appearing in selections while the game goes on is
    # treated as lost at its last sighting
    deaths = defaultdict(list)  # pid -> [(t, "military"|"villager")]
    for u, (owner, first, last, n, gn) in obs.items():
        if u in prod_ids or last >= cmds["duration"] - 120_000:
            continue
        nm = protos.get(objects[u][0], "") if u in objects else ""
        if nm and not _vill_name(nm) and ("Explorer" in nm or "TownCenter" in nm
                                          or "Crate" in nm or "Flag" in nm
                                          or objects[u][1] == 0):
            continue
        if any(t - 90_000 <= last <= t + 5_000 for t in upgrade_times[owner]):
            continue
        kind = "villager" if (_vill_name(nm) or (n and gn / n > 0.5)) else "military"
        deaths[owner].append((last, kind))
    for d in deaths.values():
        d.sort()
    mil_by = defaultdict(list)  # pid -> [(t, count)]
    mil_run2 = Counter()
    for e in events:
        if e["type"] == "train" and not (e["unit"] == "Coureur"
                                         or e["unit"].startswith("Settler")
                                         or e["unit"].startswith("Fishing")):
            mil_run2[e["player_id"]] += e.get("count", 1)
            mil_by[e["player_id"]].append((e["t_ms"], mil_run2[e["player_id"]]))

    def mil_total_at(pid, t):
        n = 0
        for tt, v in mil_by[pid]:
            if tt <= t:
                n = v
            else:
                break
        return n

    mil_type_events = defaultdict(lambda: defaultdict(list))  # pid -> unit -> [(t, n)]
    for e in events:
        if e["type"] == "train" and not (e["unit"] == "Coureur"
                                         or e["unit"].startswith("Settler")
                                         or e["unit"].startswith("Fishing")):
            mil_type_events[e["player_id"]][e["unit"]].append(
                (e["t_ms"], e.get("count", 1)))
    # shipment cards that deliver units count toward the army too;
    # crate cards count toward resources
    tech_ord = {name: i for i, name in enumerate(techs)}
    name_initres = {protos[k]: v.get("initres", 0) for k, v in unit_info.items()
                    if k in protos}
    crate_gifts = defaultdict(list)  # pid -> [(t, resources)]
    for e in events:
        if e["type"] == "shipment" and e.get("card") in tech_ord:
            info = tech_costs[tech_ord[e["card"]]]
            delivered = {}
            for ut, n in info.get("units", []):
                if "Crate" in ut and n > 0 and name_initres.get(ut):
                    crate_gifts[e["player_id"]].append(
                        (e["t_ms"] + 40_000, round(n * name_initres[ut])))
                    continue
                if (_vill_name(ut) or n <= 0
                        or re.search(r"Crate|Wagon|Flag|Covered|Sheep|Cow|Llama", ut)):
                    continue
                t_arr = e["t_ms"] + 40_000
                mil_type_events[e["player_id"]][ut].append((t_arr, n))
                mil_run2[e["player_id"]] += n
                mil_by[e["player_id"]].append((t_arr, mil_run2[e["player_id"]]))
                delivered[ut] = delivered.get(ut, 0) + n
            if delivered:
                e["units_delivered"] = delivered
    # rebuild cumulative military totals from the merged train+shipment pools
    mil_by = defaultdict(list)
    for pid in mil_type_events:
        entries = sorted((t, n) for lst in mil_type_events[pid].values()
                         for t, n in lst)
        run = 0
        for t, n in entries:
            run += n
            mil_by[pid].append((t, run))
        for u in mil_type_events[pid]:
            mil_type_events[pid][u].sort()

    def mil_types_at(pid, t):
        return {u: sum(n for tt, n in lst if tt <= t)
                for u, lst in mil_type_events[pid].items()
                if any(tt <= t for tt, _ in lst)}
    name_stats = {protos[pid]: info for pid, info in unit_info.items()
                  if pid in protos}
    battles = find_battles(events, cmds["duration"])
    battle_power(plist, events, battles, name_stats, tech_costs)
    # building registry for attack-target matching: start buildings + placed
    bldgs = []
    for inst, (bpid, owner, x, z) in objects.items():
        nm = protos.get(bpid, "")
        if 1 <= owner <= 12 and x is not None and nm == "TownCenter":
            bldgs.append({"t": 0, "owner": owner, "name": nm, "x": x, "z": z,
                          "inst": inst,
                          "hp": unit_info.get(bpid, {}).get("hp", 3000) or 3000})
    for c in cmds["builds"]:
        nm = protos.get(c["proto"], "")
        st = next((info for pid2, info in unit_info.items()
                   if pid2 == c["proto"]), {})
        bldgs.append({"t": c["t"], "owner": c["p"], "name": nm,
                      "x": c["x"], "z": c["z"], "inst": None,
                      "hp": st.get("hp", 2000) or 2000})

    def avg_dps(pid, t):
        tot = n = 0.0
        for u, lst in mil_type_events[pid].items():
            cnt = sum(k for tt, k in lst if tt <= t)
            d = name_stats.get(u, {}).get("dps", 0)
            if cnt and d:
                tot += cnt * d
                n += cnt
        return tot / n if n else 8.0

    used_deaths = defaultdict(set)
    loss_totals = {p["id"]: {"military": 0, "villagers": 0,
                             "in_battles": 0, "outside_battles": 0}
                   for p in plist}
    for p in plist:
        for t, kind in deaths[p["id"]]:
            loss_totals[p["id"]]["military" if kind == "military" else "villagers"] += 1
    for w in battles:
        w["units"] = {}
        for p in plist:
            pid = p["id"]
            mil_lost = vill_lost = 0
            for di, (t, kind) in enumerate(deaths[pid]):
                if di in used_deaths[pid]:
                    continue
                if w["start"] - 30_000 <= t < w["end"] + 90_000:
                    used_deaths[pid].add(di)
                    if kind == "military":
                        mil_lost += 1
                    else:
                        vill_lost += 1
            reinf = Counter()
            for e in events:
                if (e["type"] == "train" and e["player_id"] == pid
                        and w["start"] <= e["t_ms"] < w["end"] + 90_000):
                    reinf[e["unit"]] += e.get("count", 1)
            before = mil_types_at(pid, w["start"])
            tt = sum(before.values())
            mdead = sum(1 for dt, dk in deaths[pid]
                        if dk == "military" and dt < w["start"])
            if tt:
                ratio = max(0.0, (tt - mdead) / tt)
                before = {u: round(n * ratio) for u, n in before.items()}
                before = {u: n for u, n in before.items() if n}
            made = {u: n for u, n in reinf.items()
                    if not (u == "Coureur" or u.startswith(("Settler", "Fishing")))}
            if not before and not made and not mil_lost and not vill_lost:
                continue
            loss_totals[pid]["in_battles"] += mil_lost + vill_lost
            lost_total = min(mil_lost, sum(before.values()) + sum(made.values()))
            pool = {u: before.get(u, 0) + made.get(u, 0)
                    for u in set(before) | set(made)}
            pool_sum = sum(pool.values())
            table = []
            remaining = lost_total
            for u, have in sorted(pool.items(), key=lambda kv: -kv[1]):
                li = min(have, round(lost_total * have / pool_sum)) if pool_sum else 0
                li = min(li, remaining)
                remaining -= li
                table.append({"unit": u, "before": before.get(u, 0),
                              "made": made.get(u, 0), "lost": li,
                              "after": before.get(u, 0) + made.get(u, 0) - li})
            table.sort(key=lambda r: -r["after"])
            def _value(key):
                return round(sum(r[key] * sum(name_stats.get(r["unit"], {})
                                              .get("cost", {}).values())
                                 for r in table))
            vb, va = _value("before"), _value("after")
            w["units"][p["name"]] = {
                "military_lost": mil_lost,
                "villagers_lost": vill_lost,
                "military_trained_total": mil_total_at(pid, w["start"]),
                "value_before": vb, "value_after": va,
                "villsec_before": round(vb / BASE_GATHER),
                "villsec_after": round(va / BASE_GATHER),
                "table": table,
            }

        # buildings attacked in this battle, with rough damage-share estimate
        pname_by_id = {p["id"]: p["name"] for p in plist}
        hits = {}
        for o in cmds["orders"]:
            if o["kind"] != "target" or not (w["start"] <= o["t"] < w["end"] + 30_000):
                continue
            tgt = o.get("target")
            b = None
            if tgt is not None and tgt in objects:
                if objects[tgt][1] == 0:
                    continue
                b = next((bb for bb in bldgs if bb["inst"] == tgt), None)
            else:
                best = None
                for bb in bldgs:
                    if bb["owner"] == o["p"] or bb["t"] > o["t"]:
                        continue
                    d2 = (bb["x"] - o["x"]) ** 2 + (bb["z"] - o["z"]) ** 2
                    if d2 <= 64 and (best is None or d2 < best[0]):
                        best = (d2, bb)
                b = best[1] if best else None
            if b is None or side_of.get(b["owner"]) == side_of.get(o["p"]):
                continue
            key = (b["owner"], b["name"], round(b["x"]), round(b["z"]))
            h = hits.setdefault(key, {"hp": b["hp"], "attackers": {}})
            a = h["attackers"].setdefault(
                o["p"], {"orders": 0, "t0": o["t"], "t1": o["t"], "sel_sum": 0})
            a["orders"] += 1
            a["t0"] = min(a["t0"], o["t"])
            a["t1"] = max(a["t1"], o["t"])
            a["sel_sum"] += o["sel"]
        agg = {}
        for (owner, nm, bx, bz), h in hits.items():
            per = {}
            total = 0.0
            for apid, a in h["attackers"].items():
                span = max(8.0, (a["t1"] - a["t0"]) / 1000)
                mean_sel = a["sel_sum"] / a["orders"]
                dmg = mean_sel * avg_dps(apid, w["start"]) * span * 0.5
                per[pname_by_id.get(apid, apid)] = dmg
                total += dmg
            g = agg.setdefault((owner, nm),
                               {"n": 0, "pct": 0, "by": {}, "loc": [bx, bz]})
            g["n"] += 1
            g["pct"] = max(g["pct"], min(100, round(100 * total / h["hp"])))
            for k, v in per.items():
                g["by"][k] = max(g["by"].get(k, 0),
                                 min(100, round(100 * v / h["hp"])))
        w["buildings"] = [
            {"building": f'{pname_by_id.get(owner, owner)} {nm}',
             "segments": g["n"], "loc": g["loc"],
             "damage_pct_est": g["pct"], "by": g["by"]}
            for (owner, nm), g in agg.items()]
        w["buildings"].sort(key=lambda b: (-b["damage_pct_est"], -b["segments"]))
    for p in plist:
        lt = loss_totals[p["id"]]
        lt["outside_battles"] = lt["military"] + lt["villagers"] - lt["in_battles"]
    extra_income = defaultdict(list)
    for pid2, gifts in crate_gifts.items():
        extra_income[pid2].extend(gifts)
    for c in cmds["tributes"]:
        amt = round(c["amount"] * 0.9)  # tribute fee
        extra_income[c["p"]].append((c["t"], -c["amount"]))
        extra_income[c["to"]].append((c["t"], amt))
    vill_deaths = {p["id"]: [t for t, k in deaths[p["id"]] if k == "villager"]
                   for p in plist}
    estimates = estimate_economy(plist, events, tech_costs, start_res,
                                 start_vills, cmds["duration"],
                                 vill_deaths=vill_deaths,
                                 extra_income=dict(extra_income),
                                 vill_stats=name_stats)
    battles_json = [
        {"n": i + 1, "start_ms": w["start"], "end_ms": w["end"],
         "start": fmt_t(w["start"]), "end": fmt_t(w["end"]),
         "attack_orders": w["orders"], "peak_army": w["peak_sel"],
         "loc": w["loc"], "targets_hit": w["targets"],
         "power_estimate": w["power"], "units": w["units"],
         "buildings": w["buildings"]}
        for i, w in enumerate(battles)]
    return {
        "start_objects": start_objects,
        "start_resources_estimate": dict(start_res),
        "loss_estimate": {str(k): v for k, v in loss_totals.items()},
        "loss_events": {str(k): v for k, v in deaths.items()},
        "selected_decks": {str(k): {"name": v["name"],
                                    "matched_arrivals": v["matched_arrivals"],
                                    "arrivals_total": v["arrivals_total"],
                                    "slots": v["slots"]}
                           for k, v in selected_decks.items()},
        "battles": battles_json,
        "economy_estimate_10s": {str(k): v for k, v in estimates.items()},
        "_battles_internal": battles,
        "replay": path,
        "recorded": datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
        "duration_ms": cmds["duration"],
        "duration": fmt_t(cmds["duration"]),
        "game": {
            "name": game.get("gamename"),
            "map": game.get("gamefilename"),
            "map_set": game.get("gamemapname"),
            "num_players": game.get("gamenumplayers"),
            "treaty": bool(game.get("gamestartwithtreaty")),
            "trade_monopoly": bool(game.get("gametrademonopoly")),
            "team_lock": bool(game.get("gameteamlock")),
            "free_for_all": bool(game.get("gamefreeforall")),
            "random_seed": game.get("gamerandomseed"),
        },
        "players": [
            {"id": pid, "name": p.get("name"), "civ": p.get("civname"),
             "team": p.get("teamid"), "homecity": p.get("homecityname"),
             "homecity_level": p.get("hclevel"),
             "explorer": (p.get("explorername") or "").strip(),
             "resigned": pid in resigned}
            for pid, p in sorted(players.items())
        ],
        "event_count": len(events),
        "events": events,
    }


def file_stem(path, game):
    """Standardized output name: aoe3_<date>_<time>_<map>."""
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    map_name = re.sub(r"^(eu|yp|de|xp)(?=[A-Z])", "", game.get("gamefilename") or "unknownmap")
    map_name = re.sub(r"[^A-Za-z0-9]", "", map_name).lower()
    return f"aoe3_{ts:%Y-%m-%d_%H%M}_{map_name}"


BUCKET_MS = 10_000

# blended villager gather rate (res/sec) across food/wood/coin tasks, from
# protoy Gather rates (hunt .84, mill .67, mine .60, tree .50, estate .50)
BASE_GATHER = 0.6
STANDARD_START_RES = 600  # approximate combined starting stockpile


# which gather-effect unit types feed which income stream
GATHER_CLASS = {"Huntable": "hunt", "AbstractBerryBush": "hunt",
                "Mill": "mill", "Farm": "mill", "deField": "mill",
                "Tree": "tree", "ypGroveBuilding": "tree",
                "AbstractMine": "mine", "AbstractMountainMonastery": "mine",
                "Plantation": "plant", "ypRicePaddy": "plant",
                "deHacienda": "plant"}
# macro allocation of villagers across food/wood/coin, and time actually
# spent gathering (walking/idle/building discount)
FOOD_SHARE, WOOD_SHARE, COIN_SHARE, UTILIZATION = 0.45, 0.25, 0.30, 0.9
# auto-spawned Ottoman settlers: classic base cap of 25 (raised by Galata /
# Topkapi BuildLimit effects) and a spawn-pacing factor on Settler trainpoints
OTTOMAN_BASE_CAP = 25
OTTOMAN_SPAWN_FACTOR = 1.6


def estimate_economy(players, events, tech_info, start_res, start_vills,
                     duration_ms, vill_deaths=None, extra_income=None,
                     vill_stats=None):
    """Income model built from the game's own numbers: the civ villager's
    per-task gather rates (protoy), researched gather multipliers applied to
    their matching task, mills/plantations switching food/coin tasks off
    hunts/mines, and Ottoman auto-spawn driven by Settler trainpoints,
    church TrainPoints/BuildLimit effects and Town Center count.
    Stockpile = start + gathered + crates + tribute net - exact spend."""
    vill_deaths = vill_deaths or {}
    extra_income = extra_income or {}
    vill_stats = vill_stats or {}
    est = {}
    for p in players:
        pid = p["id"]
        vp = "Coureur" if p["civ"] == "French" else "Settler"
        st = vill_stats.get(vp, {})
        g = st.get("gather", {})
        r_hunt = g.get("Huntable", 0.84)
        r_mill = g.get("Mill", 0.67)
        r_tree = g.get("Tree", 0.50)
        r_mine = g.get("AbstractMine", 0.60)
        r_plant = g.get("Plantation", 0.50)
        auto = p["civ"] == "Ottomans"
        cap_base = OTTOMAN_BASE_CAP if auto else (st.get("buildlimit") or 99)
        tp_base = st.get("trainpoints") or 25.0

        vill_times, tc_times, mills, plants, spend, research = [], [], [], [], [], []
        for e in events:
            if e.get("player_id") != pid:
                continue
            if e["type"] == "train" and (e["unit"] == "Coureur"
                                         or e["unit"].startswith("Settler")):
                vill_times.append(e["t_ms"])
            elif e["type"] == "build":
                if e["building"] == "TownCenter":
                    tc_times.append(e["t_ms"])
                elif e["building"] in ("Mill", "Farm", "deField", "ypRicePaddy"):
                    mills.append(e["t_ms"])
                elif e["building"] in ("Plantation", "deHacienda", "Estate"):
                    plants.append(e["t_ms"])
            if e.get("cost"):
                spend.append((e["t_ms"], sum(e["cost"].values())))
            if e["type"] == "research":
                research.append((e["t_ms"], e["tech_id"]))
        spend.sort()
        vdead = sorted(vill_deaths.get(pid, []))
        extra = sorted(extra_income.get(pid, []))

        rows = []
        gathered = 0.0
        vi = si = ri = di = xi = 0
        spent = bonus = 0
        b = {"hunt": 0.0, "mill": 0.0, "tree": 0.0, "mine": 0.0, "plant": 0.0}
        cap = cap_base
        tp = tp_base
        auto_v = float(start_vills.get(pid, 6))
        for t in range(0, duration_ms + 1, BUCKET_MS):
            while vi < len(vill_times) and vill_times[vi] <= t:
                vi += 1
            while si < len(spend) and spend[si][0] <= t:
                spent += spend[si][1]
                si += 1
            while di < len(vdead) and vdead[di] <= t:
                di += 1
            while xi < len(extra) and extra[xi][0] <= t:
                bonus += extra[xi][1]
                xi += 1
            while ri < len(research) and research[ri][0] <= t:
                tid = research[ri][1]
                if 0 <= tid < len(tech_info):
                    for ut, amt in tech_info[tid]["gather"]:
                        cls = GATHER_CLASS.get(ut)
                        if cls:
                            b[cls] += amt - 1
                    for sub, amt in tech_info[tid].get("settler_mods", []):
                        if sub == "TrainPoints":
                            tp = max(6.0, tp + amt)
                        elif sub == "BuildLimit":
                            cap += int(amt)
                ri += 1
            if auto:
                ntc = 1 + sum(1 for x in tc_times if x <= t)
                auto_v += ntc * (BUCKET_MS / 1000) / (tp * OTTOMAN_SPAWN_FACTOR)
                vills = int(min(cap, auto_v))
            else:
                vills = min(cap, start_vills.get(pid, 0) + vi)
            vills = max(0, vills - di)
            food_r = (r_mill * (1 + b["mill"])
                      if any(mt <= t - 60_000 for mt in mills)
                      else r_hunt * (1 + b["hunt"]))
            coin_r = (r_plant * (1 + b["plant"])
                      if any(pt <= t - 60_000 for pt in plants)
                      else r_mine * (1 + b["mine"]))
            rate = UTILIZATION * (FOOD_SHARE * food_r
                                  + WOOD_SHARE * r_tree * (1 + b["tree"])
                                  + COIN_SHARE * coin_r)
            gathered += vills * rate * (BUCKET_MS / 1000)
            stock = max(0, round(start_res.get(pid, 0) + gathered + bonus - spent))
            rows.append([t, vills, round(gathered), stock])
        est[pid] = rows
    return est


def battle_power(players, events, battles, name_stats, tech_info):
    """Per battle/player: observed peak army size x average unit strength of
    the military mix trained by then (hp x dps, with researched Damage /
    Hitpoints upgrades applied). Model estimate."""
    for w in battles:
        w["power"] = {}
        for p in players:
            comp = Counter()
            mult = defaultdict(lambda: {"Damage": 0.0, "Hitpoints": 0.0})
            for e in events:
                if e.get("player_id") != p["id"] or e["t_ms"] >= w["start"]:
                    continue
                if e["type"] == "train":
                    st = name_stats.get(e["unit"])
                    if st and st["dps"] > 0:
                        comp[e["unit"]] += e.get("count", 1)
                elif e["type"] == "research" and 0 <= e["tech_id"] < len(tech_info):
                    for sub, ut, amt in tech_info[e["tech_id"]]["combat"]:
                        mult[ut][sub] += amt - 1
            total_n = sum(comp.values())
            if not total_n:
                continue
            tot = 0.0
            for unit, n in comp.items():
                st = name_stats[unit]
                dmg = 1 + mult[unit]["Damage"]
                hp = 1 + mult[unit]["Hitpoints"]
                tot += n * (st["hp"] * hp) * (st["dps"] * dmg) / 100
            avg = tot / total_n
            army = w["peak_sel"].get(p["name"], 0)
            if army:
                w["power"][p["name"]] = round(army * avg)
    return battles


def find_battles(events, duration_ms):
    """Cluster combat activity into battles: 10s buckets scored by targeted
    orders plus danger alerts/flares, thresholded, adjacent runs merged."""
    nb = duration_ms // BUCKET_MS + 1
    score = [0.0] * nb
    alert_b = set()
    for e in events:
        b = min(e["t_ms"] // BUCKET_MS, nb - 1)
        if (e["type"] == "order" and e["kind"] == "target"
                and e.get("target_owner") != "Gaia"):
            score[b] += 1
        elif e["type"] == "flare" or (e["type"] == "system" and "alerted danger" in e["text"]):
            score[b] += 5
            alert_b.add(b)
    nz = [s for s in score if s > 0] or [0]
    mean = sum(nz) / len(nz)
    thr = max(6.0, mean * 1.6)
    runs = []
    i = 0
    while i < nb:
        if score[i] >= thr or i in alert_b:
            j = i
            gap = 0
            while j + 1 < nb and gap <= 3:
                j += 1
                if score[j] >= thr or j in alert_b:
                    gap = 0
                else:
                    gap += 1
            j -= gap
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    battles = []
    for i, j in runs:
        t0, t1 = i * BUCKET_MS, (j + 1) * BUCKET_MS
        has_alert = any(b in alert_b for b in range(i, j + 1))
        if j - i < 1 and not has_alert:
            continue
        per_player = Counter()
        peak_sel = Counter()
        xs, zs = [], []
        targets = Counter()
        for e in events:
            if not (t0 <= e["t_ms"] < t1):
                continue
            if (e["type"] == "order" and e["kind"] == "target"
                    and e.get("target_owner") != "Gaia"):
                per_player[e["player"]] += 1
                peak_sel[e["player"]] = max(peak_sel[e["player"]], e["units_selected"])
                xs.append(e["x"]); zs.append(e["z"])
                if e.get("target_unit"):
                    targets[f'{e["target_owner"]} {e["target_unit"]}'] += 1
        if not per_player and not has_alert:
            continue
        xs.sort(); zs.sort()
        battles.append({
            "start": t0, "end": t1, "count": int(sum(per_player.values())),
            "players": [p for p, _ in per_player.most_common()],
            "orders": dict(per_player), "peak_sel": dict(peak_sel),
            "loc": (xs[len(xs) // 2], zs[len(zs) // 2]) if xs else None,
            "targets": targets.most_common(3), "alert": has_alert})
    return battles


HTML_STYLE = """
:root {
  color-scheme: light;
  --surface: #f6ecd4; --page: #e7d8b5; --ink: #2b1f10; --ink-2: #55432a;
  --muted: #8a7350; --grid: #d9c8a2; --border: #b49a68; --accent: #6b4a1f;
  --battle: #7a2317;
  --p1: #1f5fa8; --p2: #c05517; --p3: #14707a; --p4: #8a6d1c;
}
* { box-sizing: border-box; margin: 0; }
body { color: var(--ink);
  background: radial-gradient(1100px 700px at 30% -5%, #f2e6c8, #e7d8b5 55%, #dbc9a0);
  background-attachment: fixed;
  font: 15px/1.55 "Palatino Linotype", Palatino, Georgia, "Times New Roman", serif;
  padding: 36px 16px 60px; }
main { max-width: 940px; margin: 0 auto; }
h1 { font-family: Georgia, "Times New Roman", serif; font-size: 38px;
  font-variant: small-caps; letter-spacing: 0.05em; color: var(--accent);
  text-shadow: 0 1px 0 #fff3d6; }
.meta { color: var(--ink-2); margin: 4px 0 8px; font-style: italic; }
.orn { text-align: center; color: var(--border); font-size: 20px;
  margin: 10px 0 2px; letter-spacing: 0.6em; }
h2 { font-family: Georgia, serif; font-size: 15px; font-variant: small-caps;
  letter-spacing: 0.1em; color: var(--accent); margin: 20px 0 8px; }
section { background: var(--surface); border: 1px solid var(--border);
  border-radius: 3px; padding: 15px 18px;
  box-shadow: inset 0 0 0 3px rgba(255,246,222,0.55), 0 2px 6px rgba(60,40,10,0.18); }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; color: var(--muted); font-weight: 600; font-size: 12.5px;
  font-variant: small-caps; letter-spacing: 0.06em;
  padding: 4px 10px 4px 0; border-bottom: 2px solid var(--border); }
td { padding: 5px 10px 5px 0; border-bottom: 1px solid var(--grid);
  vertical-align: top; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
.chip { display: inline-block; width: 11px; height: 11px; border-radius: 2px;
  border: 1px solid rgba(43,31,16,0.45); margin-right: 7px;
  vertical-align: baseline; box-shadow: inset 0 1px 0 rgba(255,255,255,0.4); }
.bar { display: inline-block; height: 12px; border-radius: 0 4px 4px 0;
  border: 1px solid rgba(43,31,16,0.3); border-left: none;
  vertical-align: middle; margin-right: 7px; }
.num { color: var(--ink-2); }
.won { font-weight: 700; color: #3d5c1f; }
.chart svg { display: block; width: 100%; height: auto; }
.bcards { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 720px) { .bcards { grid-template-columns: 1fr; } }
.bcard { background: var(--surface); border: 1px solid var(--border);
  border-radius: 3px; padding: 15px 18px;
  box-shadow: inset 0 0 0 3px rgba(255,246,222,0.55), 0 2px 6px rgba(60,40,10,0.18); }
.bcard > svg { width: 100%; height: auto; margin-bottom: 10px;
  border: 1px solid var(--grid); }
.bhead { font-family: Georgia, serif; font-variant: small-caps; font-weight: 700;
  font-size: 18px; color: var(--battle); letter-spacing: 0.04em; }
.bmeta { color: var(--muted); font-size: 13px; margin-bottom: 6px;
  font-variant-numeric: tabular-nums; }
.bside { padding: 3px 0; border-top: 1px solid var(--grid); font-size: 13px; }
.bt { color: var(--ink-2); }
details.grp { margin-top: 22px; }
details.grp > summary { cursor: pointer; font-family: Georgia, serif;
  font-size: 18px; font-weight: 700; font-variant: small-caps;
  letter-spacing: 0.08em; color: var(--accent); padding: 8px 12px;
  user-select: none; background: linear-gradient(#f0e3c2, #e2d0a6);
  border: 1px solid var(--border); border-radius: 3px;
  box-shadow: 0 1px 3px rgba(60,40,10,0.25), inset 0 1px 0 rgba(255,248,228,0.8); }
details.grp > summary:hover { color: var(--battle); background:
  linear-gradient(#f4e8ca, #e7d6ae); }
details.grp[open] > summary { border-radius: 3px 3px 0 0; margin-bottom: 10px; }
details.grp h2 { margin: 18px 0 8px; }
details.deck { margin: 4px 0; }
details.deck > summary { cursor: pointer; color: var(--ink-2); padding: 2px 0; }
details.deck > div { color: var(--muted); font-size: 13px; padding: 4px 0 6px 16px; }
.maplegend { margin-top: 8px; font-size: 12.5px; color: var(--ink-2); }
.maplegend label { margin-right: 14px; cursor: pointer; font-variant: small-caps;
  letter-spacing: 0.04em; white-space: nowrap; }
.maplegend input { accent-color: var(--accent); vertical-align: -2px;
  margin-right: 4px; }
.tip { position: fixed; display: none; background: #fdf6e3; color: var(--ink);
  border: 1px solid var(--border); border-radius: 3px; padding: 4px 9px;
  font-size: 12.5px; pointer-events: none; z-index: 9;
  font-variant-numeric: tabular-nums; box-shadow: 0 2px 8px rgba(60,40,10,0.3); }
"""

TOOLTIP_JS = """
const tip = document.createElement('div'); tip.className = 'tip';
document.body.appendChild(tip);
let tipTimer = null, tipEl = null;
document.addEventListener('mouseover', e => {
  const t = e.target.closest('[data-tip]');
  if (t === tipEl) return;
  tipEl = t;
  clearTimeout(tipTimer);
  tip.style.display = 'none';
  if (t) tipTimer = setTimeout(() => {
    tip.textContent = t.dataset.tip; tip.style.display = 'block';
  }, 200);
});
document.addEventListener('mousemove', e => {
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 200) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
});
"""


def _nice_max(v):
    if v <= 10:
        return 12
    step = max(20, 4 * 10 ** (len(str(v)) - 2))
    return ((v // step) + 1) * step


def _x_ticks(duration_ms):
    step = 600_000 if duration_ms > 1_500_000 else 120_000
    return list(range(0, duration_ms + 1, step))


def _step_chart(series, duration_ms, colors, tip_label):
    """Cumulative step-line SVG. series = {player_id: [(t_ms, cumcount), ...]}."""
    W, H, L, R, T, B = 860, 240, 40, 130, 12, 26
    pw, ph = W - L - R, H - T - B
    ymax = _nice_max(max((pts[-1][1] for pts in series.values() if pts), default=1))

    def X(t):
        return L + t / duration_ms * pw

    def Y(v):
        return T + ph - v / ymax * ph

    s = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui" font-size="11">']
    for i in range(0, 5):
        v = ymax * i / 4
        y = Y(v)
        lbl = f"{v / 1000:g}k" if ymax >= 10000 else f"{v:.0f}"
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L + pw}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" fill="var(--muted)">{lbl}</text>')
    for t in _x_ticks(duration_ms):
        x = X(t)
        s.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle" fill="var(--muted)">{t // 60000}m</text>')
    s.append(f'<line x1="{L}" y1="{T + ph}" x2="{L + pw}" y2="{T + ph}" stroke="var(--grid)" stroke-width="1"/>')

    ends = []
    for pid, pts in series.items():
        if not pts:
            continue
        d = f"M{X(0):.1f} {Y(0):.1f}"
        for t, v in pts:
            d += f" H{X(t):.1f} V{Y(v):.1f}"
        d += f" H{X(duration_ms):.1f}"
        s.append(f'<path d="{d}" fill="none" stroke="{colors[pid]}" stroke-width="2" stroke-linejoin="round"/>')
        for t, v in pts:
            s.append(f'<circle cx="{X(t):.1f}" cy="{Y(v):.1f}" r="9" fill="transparent" '
                     f'data-tip="{fmt_t(t)} — {tip_label[pid]}: {v}"/>')
        ends.append((Y(pts[-1][1]), pid, pts[-1][1]))
    ends.sort()
    for k in range(1, len(ends)):
        if ends[k][0] - ends[k - 1][0] < 14:
            ends[k] = (ends[k - 1][0] + 14, ends[k][1], ends[k][2])
    for y, pid, v in ends:
        x = L + pw + 8
        vl = f"{v / 1000:.1f}k" if v >= 10000 else str(v)
        s.append(f'<circle cx="{x + 4}" cy="{y - 3:.1f}" r="4" fill="{colors[pid]}"/>')
        s.append(f'<text x="{x + 12}" y="{y:.1f}" fill="var(--ink-2)">{tip_label[pid][:12]} · {vl}</text>')
    s.append("</svg>")
    return "".join(s)


def _timeline_chart(players, events, wars, duration_ms, colors):
    W, L, R, T = 860, 130, 24, 16
    lane_h, B = 40, 26
    H = T + lane_h * len(players) + B
    pw = W - L - R

    def X(t):
        return L + t / duration_ms * pw

    lane_y = {p["id"]: T + i * lane_h for i, p in enumerate(players)}
    s = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui" font-size="11">']
    for i, w in enumerate(wars, 1):
        x0, x1 = X(w["start"]), max(X(w["end"]), X(w["start"]) + 3)
        s.append(f'<rect x="{x0:.1f}" y="{T}" width="{x1 - x0:.1f}" height="{lane_h * len(players)}" '
                 f'fill="var(--muted)" opacity="0.18" '
                 f'data-tip="Battle {i}: {fmt_t(w["start"])}–{fmt_t(w["end"])}, {w["count"]} attack orders"/>')
    for p in players:
        y = lane_y[p["id"]]
        mid = y + lane_h / 2
        s.append(f'<line x1="{L}" y1="{mid:.1f}" x2="{L + pw}" y2="{mid:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<circle cx="{L - 118}" cy="{mid - 3:.1f}" r="4" fill="{colors[p["id"]]}"/>')
        s.append(f'<text x="{L - 108}" y="{mid:.1f}" fill="var(--ink-2)">{p["name"][:14]}</text>')
    age_short = {"COMMERCE": "C", "FORTRESS": "F", "INDUSTRIAL": "I", "IMPERIAL": "Im"}
    name_to_id = {p["name"]: p["id"] for p in players}
    seen_ages = set()
    for e in events:
        if e["type"] == "system":
            m = re.search(r"(.+) has reached the (\w+) AGE", e["text"])
            if m and (m.group(1), m.group(2)) not in seen_ages and m.group(1) in name_to_id:
                seen_ages.add((m.group(1), m.group(2)))
                pid = name_to_id[m.group(1)]
                x, mid = X(e["t_ms"]), lane_y[pid] + lane_h / 2
                s.append(f'<rect x="{x - 5:.1f}" y="{mid - 5:.1f}" width="10" height="10" '
                         f'transform="rotate(45 {x:.1f} {mid:.1f})" fill="{colors[pid]}" '
                         f'data-tip="{m.group(1)}: {m.group(2).title()} Age at {e["t"]}"/>')
                s.append(f'<text x="{x:.1f}" y="{mid - 9:.1f}" text-anchor="middle" '
                         f'fill="var(--muted)">{age_short.get(m.group(2), "?")}</text>')
        elif e["type"] == "shipment":
            x, mid = X(e["t_ms"]), lane_y[e["player_id"]] + lane_h / 2
            s.append(f'<rect x="{x - 1:.1f}" y="{mid + 6:.1f}" width="2.5" height="9" '
                     f'fill="{colors[e["player_id"]]}" data-tip="{e["player"]}: shipment at {e["t"]}"/>')
        elif e["type"] == "resign":
            x, mid = X(e["t_ms"]), lane_y[e["player_id"]] + lane_h / 2
            s.append(f'<g stroke="var(--ink-2)" stroke-width="2" data-tip="{e["player"]} resigned at {e["t"]}">'
                     f'<line x1="{x - 5:.1f}" y1="{mid - 5:.1f}" x2="{x + 5:.1f}" y2="{mid + 5:.1f}"/>'
                     f'<line x1="{x - 5:.1f}" y1="{mid + 5:.1f}" x2="{x + 5:.1f}" y2="{mid - 5:.1f}"/></g>')
    for t in _x_ticks(duration_ms):
        s.append(f'<text x="{X(t):.1f}" y="{H - 8}" text-anchor="middle" fill="var(--muted)">{t // 60000}m</text>')
    s.append("</svg>")
    return "".join(s)


def _activity_chart(players, events, duration_ms, colors):
    """Small-multiple histograms: order events per player per 10s bucket,
    shared scale, with a brush overlay wired up by the page script."""
    nb = duration_ms // BUCKET_MS + 1
    counts = {p["id"]: [0] * nb for p in players}
    for e in events:
        if e["type"] == "order" and e["player_id"] in counts:
            counts[e["player_id"]][min(e["t_ms"] // BUCKET_MS, nb - 1)] += 1
    peak = max((max(c) for c in counts.values()), default=1) or 1

    W, L, R, T = 860, 130, 24, 8
    lane_h, plot_h, B = 56, 44, 24
    H = T + lane_h * len(players) + B
    pw = W - L - R
    bw = pw / nb
    s = [f'<svg id="actsvg" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
         f'font-family="system-ui" font-size="11" style="touch-action:none">']
    for li, p in enumerate(players):
        y0 = T + li * lane_h
        base = y0 + plot_h
        s.append(f'<line x1="{L}" y1="{base}" x2="{L + pw}" y2="{base}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<circle cx="{L - 118}" cy="{y0 + plot_h / 2 - 3:.1f}" r="4" fill="{colors[p["id"]]}"/>')
        s.append(f'<text x="{L - 108}" y="{y0 + plot_h / 2:.1f}" fill="var(--ink-2)">{p["name"][:14]}</text>')
        s.append(f'<text x="{L - 6}" y="{y0 + 9}" text-anchor="end" fill="var(--muted)">{peak}</text>')
        for b, n in enumerate(counts[p["id"]]):
            if n == 0:
                continue
            h = max(1.5, n / peak * plot_h)
            s.append(f'<rect x="{L + b * bw:.2f}" y="{base - h:.2f}" width="{max(bw - 0.6, 1):.2f}" '
                     f'height="{h:.2f}" fill="{colors[p["id"]]}" '
                     f'data-tip="{p["name"]} {fmt_t(b * BUCKET_MS)}–{fmt_t((b + 1) * BUCKET_MS)}: {n} orders"/>')
    for t in _x_ticks(duration_ms):
        x = L + t / duration_ms * pw
        s.append(f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle" fill="var(--muted)">{t // 60000}m</text>')
    s.append(f'<rect id="brushrect" x="{L}" y="{T}" width="0" height="{lane_h * len(players)}" '
             f'fill="var(--muted)" opacity="0.22" pointer-events="none"/>')
    s.append(f'<rect id="brushzone" x="{L}" y="{T}" width="{pw}" height="{lane_h * len(players)}" '
             f'fill="transparent" style="cursor:crosshair"/>')
    s.append("</svg>")
    return "".join(s)


def _page_data_js(players, events, duration_ms, colors, battles, start_objects):
    """Compact per-page dataset + brush/details/map logic."""
    unit_names, unit_idx = [], {}
    tgt_names, tgt_idx = [], {}

    def uidx(u):
        if u not in unit_idx:
            unit_idx[u] = len(unit_names)
            unit_names.append(u)
        return unit_idx[u]

    orders, trains, builds, alerts, flares = [], [], [], [], []
    kind_code = {"move": 0, "target": 1, "control": 2}
    for e in events:
        ts = e["t_ms"] // 1000
        if e["type"] == "order":
            k = 3 if e.get("target_owner") == "Gaia" else kind_code[e["kind"]]
            if k == 2:
                orders.append([ts, e["player_id"], k, e["units_selected"]])
            else:
                tname = (f'{e["target_owner"]} {e["target_unit"]}'
                         if e.get("target_unit") and e.get("target_owner") != "Gaia" else None)
                if tname is not None and tname not in tgt_idx:
                    tgt_idx[tname] = len(tgt_names)
                    tgt_names.append(tname)
                orders.append([ts, e["player_id"], k, e["units_selected"],
                               round(e["x"]), round(e["z"]),
                               tgt_idx.get(tname, -1)])
        elif e["type"] == "train":
            trains.append([ts, e["player_id"], uidx(e["unit"])])
        elif e["type"] == "build":
            builds.append([ts, e["player_id"], uidx(e["building"]),
                           round(e["x"]), round(e["z"])])
        elif e["type"] == "flare":
            flares.append([ts, e["player_id"], round(e["x"]), round(e["z"])])
            alerts.append([ts, e["player_id"]])
        elif e["type"] == "system" and "alerted danger" in e["text"]:
            alerts.append([ts, e.get("player_id") or 0])
    coords = ([o[4] for o in orders if len(o) > 4] + [o[5] for o in orders if len(o) > 4]
              + [b[3] for b in builds] + [b[4] for b in builds])
    mapsz = max(coords + [400])
    mapsz = ((mapsz // 100) + 1) * 100
    data = {
        "dur": duration_ms // 1000,
        "players": [[p["id"], p["name"]] for p in players],
        "colors": {p["id"]: colors[p["id"]] for p in players},
        "units": unit_names, "tnames": tgt_names,
        "orders": orders, "trains": trains, "builds": builds,
        "alerts": alerts, "flares": flares,
        "battles": [[i + 1, w["start"] // 1000, w["end"] // 1000,
                     round(w["loc"][0]) if w["loc"] else -1,
                     round(w["loc"][1]) if w["loc"] else -1] for i, w in enumerate(battles)],
        "sobj": [[o["player_id"], uidx(o["unit"]), round(o["x"]), round(o["z"])]
                 for o in start_objects],
        "geom": {"L": 130, "PW": 706, "W": 860, "MS": mapsz},
    }
    return "const D = " + json.dumps(data, separators=(",", ":")) + ";" + """
function fmt(s){return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
function inR(t,a,b){return t>=a&&t<b;}
function render(a,b){
  document.getElementById('selrange').textContent=fmt(a)+' – '+fmt(b);
  let rows='<tr><th>Player</th><th>Attack orders</th><th>Gather</th><th>Moves</th><th>Peak army</th><th>Alerts</th><th>Targets</th><th>Units queued</th></tr>';
  for(const [pid,name] of D.players){
    const o=D.orders.filter(e=>e[1]===pid&&inR(e[0],a,b));
    const tgt=o.filter(e=>e[2]===1).length, mv=o.filter(e=>e[2]===0).length, ga=o.filter(e=>e[2]===3).length;
    const peak=o.reduce((m,e)=>Math.max(m,e[3]),0);
    const al=D.alerts.filter(e=>e[1]===pid&&inR(e[0],a,b)).length;
    const tg={};
    for(const e of o) if(e.length>6&&e[6]>=0) tg[D.tnames[e[6]]]=(tg[D.tnames[e[6]]]||0)+1;
    const tgl=Object.entries(tg).sort((x,y)=>y[1]-x[1]).map(([u,n])=>u+' ×'+n).join(', ');
    const tc={};
    for(const t of D.trains) if(t[1]===pid&&inR(t[0],a,b)) tc[D.units[t[2]]]=(tc[D.units[t[2]]]||0)+1;
    const tl=Object.entries(tc).sort((x,y)=>y[1]-x[1]).map(([u,n])=>u+' ×'+n).join(', ');
    rows+=`<tr><td><span class="chip" style="background:${D.colors[pid]}"></span>${name}</td>
      <td class="num">${tgt}</td><td class="num">${ga}</td><td class="num">${mv}</td><td class="num">${peak||'—'}</td>
      <td class="num">${al}</td><td>${tgl||'—'}</td><td>${tl||'—'}</td></tr>`;
  }
  document.getElementById('seldetail').innerHTML='<table>'+rows+'</table>';
  renderMap(a,b);
}
function renderMap(a,b){
  const g=document.getElementById('maplayer');
  if(!g)return;
  const S=D.geom.MS, Y=z=>S-z;
  let bld='',atk='',mv='',flr='';
  for(const bd of D.builds) if(inR(bd[0],a,b))
    bld+=`<rect x="${bd[3]-3.5}" y="${Y(bd[4])-3.5}" width="7" height="7" fill="${D.colors[bd[1]]}" stroke="var(--surface)" stroke-width="1" data-tip="${(D.players.find(p=>p[0]===bd[1])||[0,'?'])[1]}: ${D.units[bd[2]]} at ${fmt(bd[0])}"/>`;
  for(const k of [0,3,1]){
    const os=D.orders.filter(e=>e.length>4&&e[2]===k&&inR(e[0],a,b));
    const step=Math.max(1,Math.ceil(os.length/3000));
    for(let i=0;i<os.length;i+=step){
      const e=os[i];
      if(k===1) atk+=`<circle cx="${e[4]}" cy="${Y(e[5])}" r="2.6" fill="${D.colors[e[1]]}"/>`;
      else mv+=`<circle cx="${e[4]}" cy="${Y(e[5])}" r="1.3" fill="${D.colors[e[1]]}" opacity="${k===0?0.3:0.14}"/>`;
    }
  }
  for(const f of D.flares) if(inR(f[0],a,b))
    flr+=`<g stroke="${D.colors[f[1]]}" stroke-width="2.4"><line x1="${f[2]-6}" y1="${Y(f[3])-6}" x2="${f[2]+6}" y2="${Y(f[3])+6}"/><line x1="${f[2]-6}" y1="${Y(f[3])+6}" x2="${f[2]+6}" y2="${Y(f[3])-6}"/></g>`;
  g.innerHTML=`<g id="lay-moves">${mv}</g><g id="lay-builds">${bld}</g><g id="lay-attacks">${atk}</g><g id="lay-flares">${flr}</g>`;
  applyLays();
}
function applyLays(){
  document.querySelectorAll('#maplegend input').forEach(cb=>{
    const g=document.getElementById('lay-'+cb.dataset.lay);
    if(g) g.style.display=cb.checked?'':'none';
  });
}
document.querySelectorAll('#maplegend input').forEach(cb=>cb.addEventListener('change',applyLays));
const svg=document.getElementById('actsvg');
if(svg){
  const zone=document.getElementById('brushzone'), rect=document.getElementById('brushrect');
  const G=D.geom; let x0=null;
  const toVB=e=>{const r=svg.getBoundingClientRect();return (e.clientX-r.left)*G.W/r.width;};
  const toT=x=>Math.round(Math.min(Math.max((x-G.L)/G.PW,0),1)*D.dur);
  zone.addEventListener('pointerdown',e=>{x0=toVB(e);zone.setPointerCapture(e.pointerId);});
  zone.addEventListener('pointermove',e=>{
    if(x0===null)return;
    const x1=toVB(e), a=Math.min(x0,x1), w=Math.abs(x1-x0);
    rect.setAttribute('x',Math.max(a,G.L)); rect.setAttribute('width',w);
  });
  zone.addEventListener('pointerup',e=>{
    const x1=toVB(e);
    if(Math.abs(x1-x0)<4){rect.setAttribute('width',0);render(0,D.dur);}
    else render(toT(Math.min(x0,x1)),toT(Math.max(x0,x1)));
    x0=null;
  });
  render(0,D.dur);
}
"""


def _map_size(doc):
    coords = []
    for e in doc["events"]:
        if e["type"] in ("order", "build", "flare") and "x" in e:
            coords += [e["x"], e["z"]]
    mapsz = max([c for c in coords if c is not None] + [400])
    return (int(mapsz) // 100 + 1) * 100


def _battle_minimap(doc, battle, colors, mapsz, events):
    """Locator map for one battle: town centers for orientation, a dot for
    every attack order in the window, and crosses where resolved
    buildings/units were hit."""
    s = [f'<svg viewBox="0 0 {mapsz} {mapsz}" xmlns="http://www.w3.org/2000/svg">']
    s.append(f'<rect width="{mapsz}" height="{mapsz}" rx="{mapsz * 0.03:.0f}" '
             f'fill="var(--page)" stroke="var(--grid)" stroke-width="2"/>')
    for o in doc["start_objects"]:
        if o["unit"] != "TownCenter":
            continue
        y = mapsz - o["z"]
        s.append(f'<rect x="{o["x"] - 9:.0f}" y="{y - 9:.0f}" width="18" height="18" '
                 f'fill="{colors.get(o["player_id"], "var(--muted)")}" opacity="0.55"/>')
    hits = []
    for e in events:
        if (e["type"] == "order" and e["kind"] == "target"
                and battle["start"] <= e["t_ms"] < battle["end"]
                and e.get("target_owner") != "Gaia"):
            x, y = e["x"], mapsz - e["z"]
            s.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="5" '
                     f'fill="{colors.get(e["player_id"], "var(--muted)")}" opacity="0.75"/>')
            if e.get("target_unit"):
                hits.append((x, y, f'{e["player"]} → {e["target_owner"]} {e["target_unit"]} '
                                   f'at {fmt_t(e["t_ms"])}'))
    for x, y, tip in hits:
        s.append(f'<g stroke="var(--ink)" stroke-width="3.5" data-tip="{tip}">'
                 f'<line x1="{x - 8:.0f}" y1="{y - 8:.0f}" x2="{x + 8:.0f}" y2="{y + 8:.0f}"/>'
                 f'<line x1="{x - 8:.0f}" y1="{y + 8:.0f}" x2="{x + 8:.0f}" y2="{y - 8:.0f}"/></g>')
    s.append("</svg>")
    return "".join(s)


def _sides(players):
    """Group players into sides by team id; team -1 joins the smallest side."""
    teams = defaultdict(list)
    for p in players:
        if p["team"] not in (None, -1):
            teams[p["team"]].append(p)
    if not teams:
        return [[p] for p in players]
    for p in players:
        if p["team"] in (None, -1):
            min(teams.values(), key=len).append(p)
    return [teams[k] for k in sorted(teams)]


def _map_svg(doc, battles, colors):
    """Static map frame: grid, start objects, numbered battle markers, and an
    empty layer the page script fills with brushed-range activity."""
    mapsz = _map_size(doc)
    s = [f'<svg id="mapsvg" viewBox="0 0 {mapsz} {mapsz}" xmlns="http://www.w3.org/2000/svg" '
         f'font-family="system-ui" font-size="13">']
    s.append(f'<rect width="{mapsz}" height="{mapsz}" fill="var(--surface)" stroke="var(--grid)"/>')
    for gline in range(100, mapsz, 100):
        s.append(f'<line x1="{gline}" y1="0" x2="{gline}" y2="{mapsz}" stroke="var(--grid)" stroke-width="0.6"/>')
        s.append(f'<line x1="0" y1="{gline}" x2="{mapsz}" y2="{gline}" stroke="var(--grid)" stroke-width="0.6"/>')
    s.append('<g id="lay-start">')
    for o in doc["start_objects"]:
        y = mapsz - o["z"]
        s.append(f'<rect x="{o["x"] - 4:.0f}" y="{y - 4:.0f}" width="8" height="8" '
                 f'fill="none" stroke="{colors.get(o["player_id"], "var(--muted)")}" stroke-width="2" '
                 f'data-tip="{pname_map(doc, o["player_id"])}: {o["unit"]} (start)"/>')
    s.append('</g><g id="maplayer"></g><g id="lay-battles">')
    for i, w in enumerate(battles, 1):
        if not w["loc"]:
            continue
        x, y = w["loc"][0], mapsz - w["loc"][1]
        s.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="13" fill="rgba(246,236,212,0.8)" '
                 f'stroke="var(--battle)" stroke-width="2.5" '
                 f'data-tip="Battle {i}: {fmt_t(w["start"])}–{fmt_t(w["end"])}"/>')
        s.append(f'<text x="{x:.0f}" y="{y + 5:.0f}" text-anchor="middle" '
                 f'fill="var(--battle)" font-weight="700" font-size="15">{i}</text>')
    s.append("</g></svg>")
    return "".join(s)


def pname_map(doc, pid):
    for p in doc["players"]:
        if p["id"] == pid:
            return p["name"]
    return f"player{pid}"


def build_html(doc):
    import html as H

    players = doc["players"]
    events = doc["events"]
    colors = {p["id"]: f"var(--p{i + 1})" for i, p in enumerate(players)}
    names = {p["id"]: p["name"] for p in players}

    def chip(pid):
        return f'<span class="chip" style="background:{colors.get(pid, "var(--muted)")}"></span>'

    def esc(s):
        return H.escape(str(s))

    out = []
    g = doc["game"]
    map_disp = re.sub(r"^(eu|yp|de|xp)(?=[A-Z])", "", g["map"] or "?")
    map_disp = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", map_disp)
    mode = " · ".join(x for x in [
        f"{g['num_players']} players", "treaty" if g["treaty"] else None,
        "FFA" if g["free_for_all"] else None] if x)
    winners = [p["name"] for p in players if not p["resigned"]]
    out.append(f"<main><h1>{esc(map_disp)}</h1>")
    out.append(f'<div class="meta">{esc(g["name"])} · {mode} · {doc["duration"]} · '
               f'{doc["recorded"][:16].replace("T", " ")}</div>')
    if winners and len(winners) < len(players):
        out.append(f'<div class="meta won">Winners: {esc(", ".join(winners))}</div>')
    out.append('<div class="orn">❦</div>')

    # players
    out.append('<details class="grp"><summary>⚑︎ Players &amp; Timeline</summary>')
    out.append("<h2>Players</h2><section><table>")
    out.append("<tr><th>Player</th><th>Civ</th><th>Team</th><th>Home City</th><th>Result</th></tr>")
    resign_t = {e["player_id"]: e["t"] for e in events if e["type"] == "resign"}
    for p in players:
        result = f"Resigned {resign_t.get(p['id'], '')}" if p["resigned"] else "Won"
        cls = "" if p["resigned"] else ' class="won"'
        team = "—" if p["team"] in (None, -1) else p["team"]
        out.append(f"<tr><td>{chip(p['id'])}{esc(p['name'])}</td><td>{esc(p['civ'])}</td>"
                   f"<td>{team}</td><td>{esc(p['homecity'])} (lvl {p['homecity_level']})</td>"
                   f"<td{cls}>{result}</td></tr>")
    out.append("</table></section>")

    # ---- cumulative state helpers ----
    from bisect import bisect_right

    def is_villager(u):
        return u == "Coureur" or u.startswith("Settler") or "Villager" in u

    def is_military(u):
        return not is_villager(u) and not u.startswith("Fishing") and not any(
            u.startswith(x) for x in ("Sheep", "Cow", "Goat", "Llama", "Pet"))

    vill_t = {p["id"]: [] for p in players}
    mil_t = {p["id"]: [] for p in players}      # (t, cumulative units)
    sp_t = {p["id"]: [] for p in players}       # (t, cumulative resources spent)
    sp_res = {p["id"]: Counter() for p in players}
    mil_run, sp_run = Counter(), Counter()
    for e in events:
        pid = e.get("player_id")
        if e["type"] == "train":
            if is_villager(e["unit"]):
                vill_t[pid].append(e["t_ms"])
            elif is_military(e["unit"]):
                mil_run[pid] += e.get("count", 1)
                mil_t[pid].append((e["t_ms"], mil_run[pid]))
        if e.get("cost") and pid in sp_res:
            sp_res[pid].update(e["cost"])
            sp_run[pid] += sum(e["cost"].values())
            sp_t[pid].append((e["t_ms"], sp_run[pid]))
    start_vills = Counter()
    for o in doc["start_objects"]:
        if is_villager(o["unit"]):
            start_vills[o["player_id"]] += 1
    autovill = {p["id"]: p["civ"] == "Ottomans" for p in players}

    def _cum_at(pairs, t):
        lo, hi = 0, len(pairs)
        while lo < hi:
            mid = (lo + hi) // 2
            if pairs[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        return pairs[lo - 1][1] if lo else 0

    loss_ev = {p["id"]: sorted(doc.get("loss_events", {}).get(str(p["id"]), []))
               for p in players}

    def _dead(pid, t, kind):
        return sum(1 for dt, dk in loss_ev.get(pid, []) if dk == kind and dt <= t)

    def vills_at(pid, t):
        if autovill.get(pid):
            r = est_row(pid, t)
            base = r[1] if r else 0
        else:
            base = start_vills[pid] + bisect_right(vill_t[pid], t)
        alive = max(0, base - _dead(pid, t, "villager"))
        return f"~{alive}" if autovill.get(pid) else alive

    def mil_at(pid, t):
        return max(0, _cum_at(mil_t[pid], t) - _dead(pid, t, "military"))

    def fmt_k(v):
        return f"{v / 1000:.1f}k" if v >= 1000 else str(int(v))

    def spent_at(pid, t):
        return _cum_at(sp_t[pid], t)

    def state_cells(t):
        return "".join(f'<td class="num">{vills_at(p["id"], t)} v · '
                       f'{mil_at(p["id"], t)} m<br>'
                       f'<span style="color:var(--muted)">{fmt_k(spent_at(p["id"], t))} spent</span></td>'
                       for p in players)

    battles = doc["_battles_internal"]
    est = doc["economy_estimate_10s"]
    tip_label = {p["id"]: p["name"] for p in players}

    def est_row(pid, t):
        rows = est.get(str(pid)) or est.get(pid)
        if not rows:
            return None
        return rows[min(t // BUCKET_MS, len(rows) - 1)]

    # timeline: age-ups, shipments, battles, resigns on one axis
    out.append('<h2>Timeline</h2><section class="chart">')
    out.append(_timeline_chart(players, events, battles, doc["duration_ms"], colors))
    out.append("</section></details>")
    out.append('<details class="grp"><summary>⌛︎ Aging</summary>')

    # aging: per age-up event, everyone's villager / military-trained state
    age_events = []
    seen_age = set()
    for e in events:
        if e["type"] == "system":
            m = re.search(r"(.+) has reached the (\w+) AGE", e["text"])
            if m and (m.group(1), m.group(2)) not in seen_age:
                seen_age.add((m.group(1), m.group(2)))
                age_events.append((e["t_ms"], m.group(1), m.group(2).title()))
    name_to_id = {p["name"]: p["id"] for p in players}
    out.append("<h2>Aging (state of all players at each age-up)</h2><section><table>")
    out.append("<tr><th>Time</th><th>Age-up</th>" + "".join(
        f"<th>{chip(p['id'])}{esc(p['name'])}</th>" for p in players) + "</tr>")
    for t, who, age in age_events:
        pid = name_to_id.get(who)
        out.append(f"<tr><td>{fmt_t(t)}</td><td>{chip(pid) if pid else ''}{esc(who)} → {age}</td>"
                   + state_cells(t) + "</tr>")
    out.append("</table></section>")

    # economy
    def cumulative(pred):
        series = {p["id"]: [] for p in players}
        counts = Counter()
        for e in events:
            if e["type"] == "train" and pred(e["unit"]):
                counts[e["player_id"]] += e.get("count", 1)
                series[e["player_id"]].append((e["t_ms"], counts[e["player_id"]]))
        return series

    MIL_BLD = re.compile(r"Barracks|Stable|ArtilleryDepot|Blockhouse|Outpost|Fort|"
                         r"Wall|Arsenal|Corral|Tower")
    builds = defaultdict(Counter)
    for e in events:
        if e["type"] == "build":
            builds[e["player_id"]][e["building"]] += 1

    out.append('</details><details class="grp"><summary>⚖︎ Economy</summary>')
    out.append('<h2>Economy (villager production, cumulative)</h2><section class="chart">')
    out.append(_step_chart(cumulative(is_villager), doc["duration_ms"], colors, tip_label))
    out.append("</section>")
    out.append('<h2>Economy (resources spent on units, buildings and research, cumulative)</h2>'
               '<section class="chart">')
    out.append(_step_chart(sp_t, doc["duration_ms"], colors, tip_label))
    out.append("</section>")
    stock_series = {p["id"]: [(r[0], r[3]) for r in
                              (est.get(str(p["id"])) or est.get(p["id"]) or [])]
                    for p in players}
    out.append('<h2>Economy (estimated stockpile — rough model, treat as directional)</h2>'
               '<section class="chart">')
    out.append(_step_chart(stock_series, doc["duration_ms"], colors, tip_label))
    out.append("</section>")
    out.append("<h2>Resources Spent (totals)</h2><section><table>")
    res_cols = ["Food", "Wood", "Gold"]
    out.append("<tr><th>Player</th>" + "".join(f"<th>{r}</th>" for r in res_cols)
               + "<th>Total</th></tr>")
    for p in players:
        c = sp_res[p["id"]]
        out.append(f"<tr><td>{chip(p['id'])}{esc(p['name'])}</td>"
                   + "".join(f'<td class="num">{fmt_k(c.get(r, 0))}</td>' for r in res_cols)
                   + f'<td class="num">{fmt_k(sum(c.values()))}</td></tr>')
    out.append("</table></section>")
    ECO = {"HuntingDogs", "SteelTraps", "Gangsaw", "LogFlume", "PlacerMines",
           "Amalgamation", "SeedDrill", "ArtificialFertilizer", "Homesteading",
           "SteamPower", "WaterPower", "CircularSaw", "Bookkeeping", "GasLighting",
           "Refineries", "Cannery", "EconomicTheory", "GillNets", "LongLines"}
    eco = defaultdict(dict)
    for e in events:
        if e["type"] == "research" and e["tech"] in ECO and e["player_id"] not in eco[e["tech"]]:
            eco[e["tech"]][e["player_id"]] = e["t"]
    if eco:
        rows = sorted(eco.items(), key=lambda kv: min(kv[1].values()))
        out.append("<h2>Economy Upgrades (research time)</h2><section><table>")
        out.append("<tr><th>Upgrade</th>" + "".join(
            f"<th>{chip(p['id'])}{esc(p['name'])}</th>" for p in players) + "</tr>")
        for tech, times in rows:
            disp = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tech)
            out.append(f"<tr><td>{esc(disp)}</td>" + "".join(
                f"<td>{times.get(p['id'], '—')}</td>" for p in players) + "</tr>")
        out.append("</table></section>")
    out.append("<h2>Economy Buildings</h2><section><table>")
    for p in players:
        items = ", ".join(f"{b} ×{n}" for b, n in builds.get(p["id"], Counter()).most_common()
                          if not MIL_BLD.search(b))
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td>{esc(items) or '—'}</td></tr>")
    out.append("</table></section>")

    # transfers, ransoms and market activity
    trans = [e for e in events if e["type"] in ("tribute", "explorer_ransom")]
    mkt = defaultdict(lambda: [0, 0])
    for e in events:
        if e["type"] == "market":
            mkt[e["player_id"]][0 if abs(e["amount"]) == 100 else 1] += 1
    if trans or mkt:
        out.append("<h2>Transfers &amp; Market</h2><section><table>")
        out.append("<tr><th>Time</th><th>Player</th><th>Action</th></tr>")
        for e in trans:
            if e["type"] == "tribute":
                d = f'tribute {e["amount"]} {e["resource"]} → {e["to"]}'
            else:
                d = f'explorer ransom {e["amount"]} coin → {e["paid_to"]}'
            out.append(f'<tr><td>{fmt_t(e["t_ms"])}</td>'
                       f'<td>{chip(e.get("player_id", 0))}{esc(e["player"])}</td>'
                       f"<td>{esc(d)}</td></tr>")
        for pid, (lots, other) in sorted(mkt.items()):
            parts = []
            if lots:
                parts.append(f"market trades ×{lots}")
            if other:
                parts.append(f"livestock/other sales ×{other}")
            out.append(f'<tr><td>—</td><td>{chip(pid)}{esc(names.get(pid, pid))}</td>'
                       f"<td>{esc(', '.join(parts))}</td></tr>")
        out.append("</table></section>")

    # military
    out.append('</details><details class="grp"><summary>⚔︎ Military</summary>')
    out.append('<h2>Military (production, cumulative train commands)</h2><section class="chart">')
    out.append(_step_chart(cumulative(is_military), doc["duration_ms"], colors, tip_label))
    out.append("</section>")
    trains = defaultdict(Counter)
    for e in events:
        if e["type"] == "train":
            trains[e["player_id"]][e["unit"]] += e.get("count", 1)
    peak = max((n for c in trains.values() for n in c.values()), default=1)
    out.append("<h2>Military Units Trained</h2><section><table>")
    out.append("<tr><th>Player</th><th>Unit</th><th>Units queued</th></tr>")
    for p in players:
        rows = [(u, n) for u, n in trains.get(p["id"], Counter()).most_common()
                if is_military(u)]
        for j, (unit, n) in enumerate(rows):
            w = max(6, round(n / peak * 220))
            name_cell = f"{chip(p['id'])}{esc(p['name'])}" if j == 0 else ""
            out.append(f'<tr><td>{name_cell}</td><td>{esc(unit)}</td>'
                       f'<td><span class="bar" style="width:{w}px;background:{colors[p["id"]]}"'
                       f' title="{esc(unit)}: {n}"></span><span class="num">{n}</span></td></tr>')
    out.append("</table></section>")
    losses = doc.get("loss_estimate", {})
    if losses:
        out.append("<h2>Estimated Losses (units that stop appearing in selections)</h2>"
                   "<section><table>")
        out.append("<tr><th>Player</th><th>Military</th><th>Villagers</th>"
                   "<th>In battles</th><th>Outside battles</th></tr>")
        for p in players:
            lt = losses.get(str(p["id"])) or losses.get(p["id"]) or {}
            out.append(f'<tr><td>{chip(p["id"])}{esc(p["name"])}</td>'
                       f'<td class="num">{lt.get("military", 0)}</td>'
                       f'<td class="num">{lt.get("villagers", 0)}</td>'
                       f'<td class="num">{lt.get("in_battles", 0)}</td>'
                       f'<td class="num">{lt.get("outside_battles", 0)}</td></tr>')
        out.append("</table></section>")
    out.append("<h2>Military Buildings</h2><section><table>")
    for p in players:
        items = ", ".join(f"{b} ×{n}" for b, n in builds.get(p["id"], Counter()).most_common()
                          if MIL_BLD.search(b))
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td>{esc(items) or '—'}</td></tr>")
    out.append("</table></section>")

    # shipments & decks
    out.append('</details><details class="grp"><summary>⚓︎ Shipments &amp; Decks</summary>')
    ships = defaultdict(list)
    for e in events:
        if e["type"] == "shipment":
            label = e.get("card_name") or f'slot {e["card_slot"]}'
            ships[e["player_id"]].append(f'{e["t"]} {label}')
    out.append("<h2>Shipments Sent (card resolved from the selected deck)</h2>"
               "<section><table>")
    out.append("<tr><th>Player</th><th>Sent</th><th>Shipments</th></tr>")
    for p in players:
        ts = ships.get(p["id"], [])
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td class='num'>{len(ts)}</td><td>{esc(' · '.join(ts)) or '—'}</td></tr>")
    out.append("</table></section>")

    sel_decks = doc.get("selected_decks", {})
    if sel_decks:
        out.append("<h2>Selected Decks (validated against arrival notifications)</h2>"
                   "<section>")
        for p in players:
            d = sel_decks.get(str(p["id"])) or sel_decks.get(p["id"])
            if not d:
                continue
            sent_slots = Counter(e["card_slot"] for e in events
                                 if e["type"] == "shipment" and e["player_id"] == p["id"])
            items = []
            for i, c in enumerate(d["slots"]):
                nm = esc(card_display(c))
                n = sent_slots.get(i, 0)
                items.append(f"<b>{nm} ×{n}</b>" if n else nm)
            out.append(f'<details class="deck" open><summary>{chip(p["id"])}'
                       f'{esc(p["name"])} — {esc(d["name"])} '
                       f'<span class="num">(matched {d["matched_arrivals"]}/'
                       f'{d["arrivals_total"]} arrivals; sent cards in bold)</span></summary>'
                       f'<div>{", ".join(items)}</div></details>')
        out.append("</section>")

    # improvements: every research, chronological
    out.append('</details><details class="grp"><summary>⚙︎ Improvements</summary>')
    out.append("<h2>Improvements (research queued)</h2><section><table>")
    out.append("<tr><th>Time</th><th>Player</th><th>Improvement</th></tr>")
    for e in events:
        if e["type"] == "research":
            disp = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", e["tech"])
            out.append(f'<tr><td>{e["t"]}</td>'
                       f'<td>{chip(e["player_id"])}{esc(e["player"])}</td>'
                       f"<td>{esc(disp)}</td></tr>")
    out.append("</table></section>")

    # activity histogram with brush selection
    out.append('</details><details class="grp"><summary>☠︎ Battles</summary>')
    out.append('<h2>Activity (orders per 10s — drag to select a range)</h2>'
               '<section class="chart">')
    out.append(_activity_chart(players, events, doc["duration_ms"], colors))
    out.append("</section>")
    out.append('<h2>Selection <span id="selrange" class="num"></span></h2>'
               '<section id="seldetail"></section>')

    # map: brushed-range activity over map coordinates
    out.append('<h2>Map (selection)</h2>'
               '<section class="chart" style="max-width:780px;margin:0 auto">')
    out.append(_map_svg(doc, battles, colors))
    out.append('<div class="maplegend" id="maplegend">'
               '<label><input type="checkbox" checked data-lay="battles">&#9675; battles</label>'
               '<label><input type="checkbox" checked data-lay="attacks">&#9679; attack orders</label>'
               '<label><input type="checkbox" checked data-lay="moves">&#183; moves/gather</label>'
               '<label><input type="checkbox" checked data-lay="builds">&#9632; buildings placed</label>'
               '<label><input type="checkbox" checked data-lay="flares">&#10005; flares</label>'
               '<label><input type="checkbox" checked data-lay="start">&#9633; start buildings</label>'
               '</div></section>')

    # battles
    mapsz = _map_size(doc)
    sides = _sides(players)
    out.append('<h2>Battles</h2>')
    out.append('<div class="bcards">')
    for i, w in enumerate(battles, 1):
        loc = f"({w['loc'][0]:.0f}, {w['loc'][1]:.0f})" if w["loc"] else ""
        total = w["count"]
        out.append('<div class="bcard">')
        out.append(_battle_minimap(doc, w, colors, mapsz, events))
        out.append("<div>")
        out.append(f'<div class="bhead">Battle {i}</div>')
        out.append(f'<div class="bmeta">{fmt_t(w["start"])} – {fmt_t(w["end"])}'
                   f'{" · " + loc if loc else ""} · {total} attack orders</div>')
        if w.get("units"):
            side_txt = []
            for side in sides:
                sb = sa = 0
                for p in side:
                    u = w["units"].get(p["name"])
                    if u and u.get("table"):
                        sb += sum(r["before"] for r in u["table"])
                        sa += sum(r["after"] for r in u["table"])
                side_txt.append(f"{sb}&#8594;{sa}")
            out.append(f'<div class="bmeta">&#9876; military {" vs ".join(side_txt)}</div>')
        for side in sides:
            parts = []
            for p in side:
                n = w["orders"].get(p["name"], 0)
                army = w["peak_sel"].get(p["name"], 0)
                pw_ = w.get("power", {}).get(p["name"])
                if n or army:
                    parts.append(f'{chip(p["id"])}{esc(p["name"])} '
                                 f'<span class="num">{n} orders, army {army}'
                                 f'{", power ~" + fmt_k(pw_) if pw_ else ""}</span>')
            if parts:
                out.append(f'<div class="bside">{" &nbsp; ".join(parts)}</div>')
        state = " &nbsp; ".join(f'{chip(p["id"])}<span class="num">'
                                f'{vills_at(p["id"], w["start"])} v · '
                                f'{mil_at(p["id"], w["start"])} m · '
                                f'{fmt_k(spent_at(p["id"], w["start"]))}</span>' for p in players)
        out.append(f'<div class="bside bt">At start (v · military · spent): {state}</div>')
        if w["targets"]:
            tg = ", ".join(f"{esc(name)} ×{n}" for name, n in w["targets"])
            out.append(f'<div class="bside bt">Hit: {tg}</div>')
        for e in events:
            if e["type"] == "explorer_ransom" and w["start"] <= e["t_ms"] < w["end"]:
                out.append(f'<div class="bside bt">Explorer down: {esc(e["player"])} '
                           f'(ransom {e["amount"]} coin → {esc(e["paid_to"])})</div>')
        if w.get("units"):
            out.append('<details class="deck"><summary class="bt">Unit breakdown</summary>')
            out.append('<table style="font-size:12.5px"><tr><th>Player</th><th>Unit</th>'
                       '<th>Before</th><th>Made during</th><th>Lost</th><th>After</th></tr>')
            for p in players:
                u = w["units"].get(p["name"])
                if not u or not u.get("table"):
                    continue
                rows = u["table"]
                tb = sum(r["before"] for r in rows)
                tm = sum(r["made"] for r in rows)
                tl = sum(r["lost"] for r in rows)
                ta = sum(r["after"] for r in rows)
                for j, r in enumerate(rows):
                    name_cell = f'{chip(p["id"])}{esc(p["name"])}' if j == 0 else ""
                    out.append(f'<tr><td>{name_cell}</td><td>{esc(r["unit"])}</td>'
                               f'<td class="num">{r["before"]}</td>'
                               f'<td class="num">{r["made"] or "—"}</td>'
                               f'<td class="num">{r["lost"] or "—"}</td>'
                               f'<td class="num">{r["after"]}</td></tr>')
                out.append(f'<tr><td></td><td class="won">Total military</td>'
                           f'<td class="num won">{tb}</td><td class="num won">{tm or "—"}</td>'
                           f'<td class="num won">{tl or "—"}</td>'
                           f'<td class="num won">{ta}</td></tr>')
                if u["military_lost"] > tl + 2:
                    out.append(f'<tr><td></td><td>Unattributed losses (est)</td>'
                               f'<td></td><td></td>'
                               f'<td class="num">{u["military_lost"] - tl}</td>'
                               f'<td></td></tr>')
                out.append(f'<tr><td></td><td class="bt">Army value (vill·sec)</td>'
                           f'<td class="num" colspan="2">{fmt_k(u["value_before"])} res '
                           f'({fmt_k(u["villsec_before"])} v·s)</td><td></td>'
                           f'<td class="num">{fmt_k(u["value_after"])} res '
                           f'({fmt_k(u["villsec_after"])} v·s)</td></tr>')
                if u["villagers_lost"]:
                    out.append(f'<tr><td></td><td>Villagers (est)</td><td></td><td></td>'
                               f'<td class="num">{u["villagers_lost"]}</td><td></td></tr>')
            out.append("</table>")
            if w.get("buildings"):
                out.append('<div class="bside bt" style="margin-top:6px">Buildings attacked '
                           '(damage est):</div>')
                for b in w["buildings"][:6]:
                    by = ", ".join(f"{esc(k)} ~{v}%" for k, v in b["by"].items())
                    seg = f' ×{b["segments"]}' if b["segments"] > 1 else ""
                    out.append(f'<div class="bside">{esc(b["building"])}{seg} '
                               f'<span class="num">~{b["damage_pct_est"]}% ({by})</span></div>')
            out.append("</details>")
        out.append("</div></div>")
    out.append("</div></details>")

    out.append("</main>")

    data_js = _page_data_js(players, events, doc["duration_ms"], colors,
                            battles, doc["start_objects"])
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{H.escape(map_disp)} {doc['recorded'][:10]}</title>"
            f"<style>{HTML_STYLE}</style></head><body>" + "".join(out)
            + f"<script>{TOOLTIP_JS}</script><script>{data_js}</script></body></html>")


def format_report(path, game, players, cmds, protos, techs):
    out = []
    dur = cmds["duration"]
    out.append("AOE3 DE GAME HISTORY (extracted from replay)")
    out.append(f"Replay file: {path}")
    out.append(f"Recorded: {datetime.datetime.fromtimestamp(os.path.getmtime(path))}")
    out.append(f"Game duration: {fmt_t(dur)} (mm:ss)")
    out.append("")
    out.append("=== GAME SETUP ===")
    out.append(f"Game name: {game.get('gamename')}")
    out.append(f"Map: {game.get('gamefilename')} ({game.get('gamemapname')})")
    out.append(f"Players: {game.get('gamenumplayers')}")
    out.append(f"Treaty: {'on' if game.get('gamestartwithtreaty') else 'off'}"
               f" | Trade monopoly: {'on' if game.get('gametrademonopoly') else 'off'}"
               f" | Team lock: {'on' if game.get('gameteamlock') else 'off'}"
               f" | FFA: {'on' if game.get('gamefreeforall') else 'off'}")
    out.append("")
    out.append("=== PLAYERS ===")
    for pid, p in sorted(players.items()):
        out.append(f"Player {pid}: {p.get('name')} - {p.get('civname')}"
                   f" | team {p.get('teamid')}"
                   f" | Home City: {p.get('homecityname')} (level {p.get('hclevel')})"
                   f" | explorer: {(p.get('explorername') or '').strip()}")

    out.append("")
    out.append("=== OUTCOME ===")
    resigned = set()
    for r in cmds["resigns"]:
        out.append(f"{fmt_t(r['t'])} {pname(players, r['slot'])} RESIGNED")
        resigned.add(r["slot"])
    if resigned:
        survivors = [p["name"] for pid, p in players.items() if pid not in resigned]
        out.append(f"Winners (did not resign): {', '.join(survivors)}")
    else:
        out.append("No resign commands in the recording (game may have ended by score or disconnect).")

    def unit_name(proto_id):
        return protos.get(proto_id, f"unit#{proto_id}")

    out.append("")
    out.append("=== UNITS TRAINED (train commands; batch civs queue multiple units per command) ===")
    trains = defaultdict(Counter)
    placements = defaultdict(Counter)
    for c in cmds["trains"]:
        nm = unit_name(c["proto"])
        if PLACEMENT_PAT.search(nm):
            placements[c["p"]][nm] += 1
        else:
            trains[c["p"]][nm] += 1
    for pid in sorted(trains):
        items = ", ".join(f"{u} x{n}" for u, n in trains[pid].most_common())
        out.append(f"{pname(players, pid)}: {items}")
    if placements:
        out.append("")
        out.append("=== PLACEMENTS / MAP INTERACTIONS (wall segments, buildable mines, map objects) ===")
        for pid in sorted(placements):
            items = ", ".join(f"{u} x{n}" for u, n in placements[pid].most_common())
            out.append(f"{pname(players, pid)}: {items}")

    out.append("")
    out.append("=== BUILDINGS PLACED ===")
    builds = defaultdict(Counter)
    for c in cmds["builds"]:
        builds[c["p"]][unit_name(c["proto"])] += 1
    for pid in sorted(builds):
        items = ", ".join(f"{b} x{n}" for b, n in builds[pid].most_common())
        out.append(f"{pname(players, pid)}: {items}")

    out.append("")
    out.append("=== SHIPMENTS SENT (deck card slot; names appear in the log when they arrive) ===")
    ships = defaultdict(list)
    for c in cmds["shipments"]:
        ships[c["p"]].append((c["t"], c["card"]))
    for pid in sorted(ships):
        items = ", ".join(f"{fmt_t(t)}(card {card})" for t, card in ships[pid])
        out.append(f"{pname(players, pid)} ({len(ships[pid])} shipments): {items}")

    out.append("")
    out.append("Notes: timestamps are game time (mm:ss). CHAT lines show the sender.")
    out.append("SYSTEM lines show the notification and its recipient ('->name');")
    out.append("'you' in tribute/ransom lines means the player whose machine saved")
    out.append("this replay. RESEARCH lines are the moment the tech was queued;")
    out.append("'improvement complete' SYSTEM lines are when it finished. FLARE")
    out.append("lines are minimap pings with map coordinates - clusters of flares")
    out.append("and danger alerts usually mark battles. Kill counts and score are")
    out.append("computed during playback and are not stored in replay files.")
    out.append("")
    out.append("=== GAME LOG (chronological) ===")

    log = []
    seen = set()
    for m in cmds["messages"]:
        msg = ICON_PAT.sub("coin ", m["msg"]).strip()
        if not msg:
            continue
        cm = COORD_PAT.match(msg)
        if m["from"] != 0 and cm:
            x, y = float(cm.group(1)), float(cm.group(3))
            log.append((m["t"], f"FLARE  {pname(players, m['from'])} pinged map at ({x:.0f}, {y:.0f})"))
            continue
        if m["from"] == 0:
            key = (m["t"], msg)
            if key in seen:
                continue
            seen.add(key)
            log.append((m["t"], f"SYSTEM {msg} ->{pname(players, m['to'])}"))
        else:
            log.append((m["t"], f"CHAT   {pname(players, m['from'])}: {msg}"))
    for c in cmds["techs"]:
        tech = techs[c["tech"]] if 0 <= c["tech"] < len(techs) else f"tech#{c['tech']}"
        log.append((c["t"], f"RESEARCH {pname(players, c['p'])} queued {tech}"))
    log.sort(key=lambda x: x[0])
    for t, line in log:
        out.append(f"{fmt_t(t)} {line}")

    out.append("")
    out.append("=== AGE-UP TIMES ===")
    ages = defaultdict(dict)
    for t, line in log:
        m = re.search(r"SYSTEM (.+) has reached the (\w+) AGE", line)
        if m and m.group(2) not in ages[m.group(1)]:
            ages[m.group(1)][m.group(2)] = t
    for name_, d in ages.items():
        parts = ", ".join(f"{age.title()} {fmt_t(t)}" for age, t in d.items())
        out.append(f"{name_}: {parts}")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Extract game history from an AoE3 DE replay")
    ap.add_argument("replay", nargs="?", help="path to .age3Yrec (default: latest replay)")
    ap.add_argument("-j", "--json", nargs="?", const="auto", metavar="FILE",
                    help="write the full timestamped event stream as JSON (main output); "
                         "no value = standardized aoe3_<date>_<map>.json")
    ap.add_argument("-o", "--output", nargs="?", const="auto", metavar="FILE",
                    help="write the human-readable text report; no value = standardized name")
    ap.add_argument("--html", nargs="?", const="auto", metavar="FILE",
                    help="write a post-game HTML summary; no value = standardized name")
    args = ap.parse_args()

    path = args.replay or find_latest_replay()
    data = load(path)
    game, players = parse_settings(data)
    protos, techs, unit_info, tech_costs = build_name_tables(data)
    cmds = parse_commands(data)
    objects = parse_start_objects(data, protos)
    stem = file_stem(path, game)

    def resolve(arg, ext):
        return f"{stem}.{ext}" if arg == "auto" else arg

    doc = build_events(path, game, players, cmds, protos, techs, objects,
                       unit_info, tech_costs, parse_decks(data, techs))
    wrote_any = False
    if args.json:
        target = resolve(args.json, "json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in doc.items() if not k.startswith("_")},
                      f, indent=1, ensure_ascii=False)
        print(f"Wrote {target}: {doc['event_count']} events, duration {doc['duration']}")
        wrote_any = True
    if args.output:
        target = resolve(args.output, "txt")
        report = format_report(path, game, players, cmds, protos, techs)
        with open(target, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"Wrote {target} (text report)")
        wrote_any = True
    if args.html:
        target = resolve(args.html, "html")
        with open(target, "w", encoding="utf-8") as f:
            f.write(build_html(doc))
        print(f"Wrote {target} (HTML summary)")
        wrote_any = True
    if not wrote_any:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        json.dump(doc, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
