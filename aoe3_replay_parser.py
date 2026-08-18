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
            tstate = {"cur": None}

            def visit(elem, a, text, depth):
                if elem == "tech" and depth == 1:
                    techs.append(a.get("name") or "?")
                    tstate["cur"] = {"cost": {}, "gather": [], "combat": []}
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
           "markets": [], "duration": 0}
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

    def unit_cost(proto_id):
        info = unit_info.get(proto_id)
        if not info or not info["cost"]:
            return None, 1
        batch = info["batch"]
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
    plist = [{"id": pid, "name": p.get("name"), "civ": p.get("civname")}
             for pid, p in sorted(players.items())]

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

    # battle unit breakdown from selection ids
    last_seen = {}
    for o in cmds["orders"]:
        for u in o.get("ids", []):
            if u > 0:
                last_seen[u] = max(last_seen.get(u, 0), o["t"])
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

    def mil_types_at(pid, t):
        return {u: sum(n for tt, n in lst if tt <= t)
                for u, lst in mil_type_events[pid].items()
                if any(tt <= t for tt, _ in lst)}
    name_stats = {protos[pid]: info for pid, info in unit_info.items()
                  if pid in protos}
    battles = find_battles(events, cmds["duration"])
    battle_power(plist, events, battles, name_stats, tech_costs)
    for w in battles:
        w["units"] = {}
        for p in plist:
            pid = p["id"]
            involved = set()
            for o in cmds["orders"]:
                if (o["p"] == pid and o["kind"] == "target"
                        and w["start"] <= o["t"] < w["end"]):
                    tgt = o.get("target")
                    if tgt is not None and tgt in objects and objects[tgt][1] == 0:
                        continue  # gather order on a Gaia object
                    involved.update(u for u in o.get("ids", []) if u > 0)
            if not involved:
                continue
            typed = Counter()
            for u in involved:
                if u in objects:
                    typed[protos.get(objects[u][0], "?")] += 1
            grace = w["end"] + 30_000
            endgame = w["end"] + 120_000 >= cmds["duration"]
            lost = sum(1 for u in involved if last_seen.get(u, 0) < grace)
            reinf, repl = Counter(), Counter()
            for e in events:
                if e["type"] == "train" and e["player_id"] == pid:
                    if w["start"] <= e["t_ms"] < w["end"]:
                        reinf[e["unit"]] += e.get("count", 1)
                    elif w["end"] <= e["t_ms"] < w["end"] + 120_000:
                        repl[e["unit"]] += e.get("count", 1)
            # Before / made / lost / after per unit type. Lost total is the
            # not-seen-again id count, allocated across types by army mix.
            before = mil_types_at(pid, w["start"])
            made = {u: n for u, n in reinf.items()
                    if u in mil_type_events[pid] or not u.startswith(("Settler", "Coureur", "Fishing"))}
            lost_total = 0 if endgame else min(lost, sum(before.values()) + sum(made.values()))
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
            w["units"][p["name"]] = {
                "involved": len(involved),
                "known_types": dict(typed.most_common(4)),
                "not_seen_after": None if endgame else lost,
                "seen_after": None if endgame else len(involved) - lost,
                "military_trained_total": mil_total_at(pid, w["start"]),
                "reinforced_during": dict(reinf.most_common(6)),
                "queued_after": dict(repl.most_common(6)),
                "table": table,
            }
    estimates = estimate_economy(plist, events, tech_costs, start_res,
                                 start_vills, cmds["duration"])
    battles_json = [
        {"n": i + 1, "start_ms": w["start"], "end_ms": w["end"],
         "start": fmt_t(w["start"]), "end": fmt_t(w["end"]),
         "attack_orders": w["orders"], "peak_army": w["peak_sel"],
         "loc": w["loc"], "targets_hit": w["targets"],
         "power_estimate": w["power"], "units": w["units"]}
        for i, w in enumerate(battles)]
    return {
        "start_objects": start_objects,
        "start_resources_estimate": dict(start_res),
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


def estimate_economy(players, events, tech_info, start_res, start_vills,
                     duration_ms):
    """Model estimate: villagers x blended gather rate x researched gather
    multipliers, integrated over time; stockpile = start + gathered - spent.
    Spend is exact; income is a first-order model."""
    est = {}
    for p in players:
        pid = p["id"]
        vill_times = []
        tc_times = []
        spend = []
        research = []
        for e in events:
            if e.get("player_id") != pid:
                continue
            if e["type"] == "train" and (e["unit"] == "Coureur"
                                         or e["unit"].startswith("Settler")):
                vill_times.append(e["t_ms"])
            elif e["type"] == "build" and e["building"] == "TownCenter":
                tc_times.append(e["t_ms"])
            if e.get("cost"):
                spend.append((e["t_ms"], sum(e["cost"].values())))
            if e["type"] == "research":
                research.append((e["t_ms"], e["tech_id"]))
        spend.sort()
        rows = []
        gathered = 0.0
        vi = si = ri = 0
        spent = 0
        blend = 0.0
        auto = p["civ"] == "Ottomans"
        for t in range(0, duration_ms + 1, BUCKET_MS):
            while vi < len(vill_times) and vill_times[vi] <= t:
                vi += 1
            while si < len(spend) and spend[si][0] <= t:
                spent += spend[si][1]
                si += 1
            while ri < len(research) and research[ri][0] <= t:
                tid = research[ri][1]
                if 0 <= tid < len(tech_info):
                    for _ut, amt in tech_info[tid]["gather"]:
                        blend += (amt - 1) / 3
                ri += 1
            if auto:
                ntc = 1 + sum(1 for x in tc_times if x <= t)
                vills = min(99, start_vills.get(pid, 6) + int(t / 30000 * ntc))
            else:
                vills = start_vills.get(pid, 0) + vi
            gathered += vills * BASE_GATHER * (1 + blend) * (BUCKET_MS / 1000)
            stock = max(0, round(start_res.get(pid, 0) + gathered - spent))
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
  color-scheme: light dark;
  --surface: #fcfcfb; --page: #f9f9f7; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --p1: #2a78d6; --p2: #eb6834; --p3: #1baf7a; --p4: #eda100;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #1a1a19; --page: #0d0d0d; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --p1: #3987e5; --p2: #d95926; --p3: #199e70; --p4: #c98500;
  }
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 32px 16px; }
main { max-width: 920px; margin: 0 auto; }
h1 { font-size: 26px; }
.meta { color: var(--ink-2); margin: 4px 0 8px; }
h2 { font-size: 15px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--muted); margin: 36px 0 10px; }
section { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; color: var(--muted); font-weight: 500; font-size: 13px;
  padding: 4px 10px 4px 0; border-bottom: 1px solid var(--grid); }
td { padding: 5px 10px 5px 0; border-bottom: 1px solid var(--grid);
  vertical-align: top; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
.chip { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  margin-right: 7px; vertical-align: baseline; }
.bar { display: inline-block; height: 12px; border-radius: 0 4px 4px 0;
  vertical-align: middle; margin-right: 7px; }
.num { color: var(--ink-2); }
.won { font-weight: 600; }
.chart svg { display: block; width: 100%; height: auto; }
.bcards { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 720px) { .bcards { grid-template-columns: 1fr; } }
.bcard { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; }
.bcard > svg { width: 100%; height: auto; margin-bottom: 10px; }
.bhead { font-weight: 600; }
.bmeta { color: var(--muted); font-size: 13px; margin-bottom: 6px;
  font-variant-numeric: tabular-nums; }
.bside { padding: 3px 0; border-top: 1px solid var(--grid); font-size: 13px; }
.bt { color: var(--ink-2); }
details.grp { margin-top: 26px; }
details.grp > summary { cursor: pointer; font-size: 15px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
  padding: 6px 0; user-select: none; }
details.grp > summary:hover { color: var(--ink-2); }
details.grp h2 { margin: 18px 0 8px; }
details.deck { margin: 4px 0; }
details.deck > summary { cursor: pointer; color: var(--ink-2); padding: 2px 0; }
details.deck > div { color: var(--muted); font-size: 13px; padding: 4px 0 6px 16px; }
.tip { position: fixed; display: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px;
  font-size: 12px; pointer-events: none; z-index: 9;
  font-variant-numeric: tabular-nums; box-shadow: 0 2px 8px rgba(0,0,0,0.12); }
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
  let s='';
  for(const bd of D.builds) if(inR(bd[0],a,b))
    s+=`<rect x="${bd[3]-3.5}" y="${Y(bd[4])-3.5}" width="7" height="7" fill="${D.colors[bd[1]]}" stroke="var(--surface)" stroke-width="1" data-tip="${(D.players.find(p=>p[0]===bd[1])||[0,'?'])[1]}: ${D.units[bd[2]]} at ${fmt(bd[0])}"/>`;
  for(const k of [0,3,1]){
    const os=D.orders.filter(e=>e.length>4&&e[2]===k&&inR(e[0],a,b));
    const step=Math.max(1,Math.ceil(os.length/3000));
    for(let i=0;i<os.length;i+=step){
      const e=os[i];
      if(k===1) s+=`<circle cx="${e[4]}" cy="${Y(e[5])}" r="2.6" fill="${D.colors[e[1]]}"/>`;
      else s+=`<circle cx="${e[4]}" cy="${Y(e[5])}" r="1.3" fill="${D.colors[e[1]]}" opacity="${k===0?0.3:0.14}"/>`;
    }
  }
  for(const f of D.flares) if(inR(f[0],a,b))
    s+=`<g stroke="${D.colors[f[1]]}" stroke-width="2.4"><line x1="${f[2]-6}" y1="${Y(f[3])-6}" x2="${f[2]+6}" y2="${Y(f[3])+6}"/><line x1="${f[2]-6}" y1="${Y(f[3])+6}" x2="${f[2]+6}" y2="${Y(f[3])-6}"/></g>`;
  g.innerHTML=s;
}
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
    for o in doc["start_objects"]:
        y = mapsz - o["z"]
        s.append(f'<rect x="{o["x"] - 4:.0f}" y="{y - 4:.0f}" width="8" height="8" '
                 f'fill="none" stroke="{colors.get(o["player_id"], "var(--muted)")}" stroke-width="2" '
                 f'data-tip="{pname_map(doc, o["player_id"])}: {o["unit"]} (start)"/>')
    for i, w in enumerate(battles, 1):
        if not w["loc"]:
            continue
        x, y = w["loc"][0], mapsz - w["loc"][1]
        s.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="11" fill="none" stroke="var(--muted)" '
                 f'stroke-width="1.5" data-tip="Battle {i}: {fmt_t(w["start"])}–{fmt_t(w["end"])}"/>')
        s.append(f'<text x="{x:.0f}" y="{y + 4:.0f}" text-anchor="middle" fill="var(--muted)">{i}</text>')
    s.append('<g id="maplayer"></g></svg>')
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

    # players
    out.append('<details class="grp"><summary>Players &amp; Timeline</summary>')
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

    def vills_at(pid, t):
        if autovill.get(pid):
            r = est_row(pid, t)
            return f"~{r[1]}" if r else "auto"
        return start_vills[pid] + bisect_right(vill_t[pid], t)

    def mil_at(pid, t):
        return _cum_at(mil_t[pid], t)

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
    out.append('<details class="grp"><summary>Aging</summary>')

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

    out.append('</details><details class="grp"><summary>Economy</summary>')
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
    out.append('<h2>Economy (estimated stockpile — modeled income minus exact spend)</h2>'
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

    # transfers, ransoms and market trades
    trans = [e for e in events if e["type"] in ("tribute", "market", "explorer_ransom")]
    if trans:
        out.append("<h2>Transfers &amp; Market</h2><section><table>")
        out.append("<tr><th>Time</th><th>Player</th><th>Action</th></tr>")
        for e in trans:
            if e["type"] == "tribute":
                d = f'tribute {e["amount"]} {e["resource"]} → {e["to"]}'
            elif e["type"] == "market":
                d = f'market {e["mode"]} {e["amount"]} {e["resource"]}'
            else:
                d = f'explorer ransom {e["amount"]} coin → {e["paid_to"]}'
            out.append(f'<tr><td>{fmt_t(e["t_ms"])}</td>'
                       f'<td>{chip(e.get("player_id", 0))}{esc(e["player"])}</td>'
                       f"<td>{esc(d)}</td></tr>")
        out.append("</table></section>")

    # military
    out.append('</details><details class="grp"><summary>Military</summary>')
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
    out.append("<h2>Military Buildings</h2><section><table>")
    for p in players:
        items = ", ".join(f"{b} ×{n}" for b, n in builds.get(p["id"], Counter()).most_common()
                          if MIL_BLD.search(b))
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td>{esc(items) or '—'}</td></tr>")
    out.append("</table></section>")

    # shipments & decks
    out.append('</details><details class="grp"><summary>Shipments &amp; Decks</summary>')
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
    out.append('</details><details class="grp"><summary>Improvements</summary>')
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
    out.append('</details><details class="grp"><summary>Battle Analysis</summary>')
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
    out.append('<div class="num" style="font-size:12px;margin-top:6px">'
               '&#9633; start building &nbsp; &#9632; building placed &nbsp; '
               '&#9679; attack order &nbsp; &#183; move/gather &nbsp; '
               '&#10005; flare &nbsp; &#9675; battle</div></section>')

    # battles
    mapsz = _map_size(doc)
    sides = _sides(players)
    out.append('</details><details class="grp"><summary>Battles</summary>')
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
            out.append(f'<div class="bside">{" &nbsp; ".join(parts) if parts else "—"}</div>')
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
                out.append(f'<tr><td></td><td class="won">Total</td>'
                           f'<td class="num won">{tb}</td><td class="num won">{tm or "—"}</td>'
                           f'<td class="num won">{tl or "—"}</td><td class="num won">{ta}</td></tr>')
            out.append("</table></details>")
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
