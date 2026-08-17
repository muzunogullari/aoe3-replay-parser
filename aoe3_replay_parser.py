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
    """proto id -> unit name, and tech ordinal -> tech name, from the
    embedded runtime protoy/techtreey XMB documents."""
    protos, techs = {}, []
    for off in _find_xmb_docs(data, limit=4):
        try:
            elements, attrs, body = _xmb_tables(data, off)
        except (struct.error, IndexError):
            continue
        root = elements[0] if elements else ""
        if root == "proto":
            def visit(elem, a, text, depth):
                if elem == "unit" and "id" in a and "name" in a:
                    try:
                        protos[int(a["id"])] = a["name"]
                    except ValueError:
                        pass
                    return False
                return depth == 0
        elif root == "techtree":
            def visit(elem, a, text, depth):
                if elem == "tech" and depth == 1:
                    techs.append(a.get("name") or "?")
                    return False
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
    return protos, techs


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
            objects.setdefault(inst, (pid, owner))
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
           "techs": [], "shipments": [], "orders": [], "duration": 0}
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
            pos += 4 * sel
            u2 = i32(pos); pos += 4
            pos += u2 * 12
            uc = i32(pos); pos += 4
            pos += uc + 1 + 16 + 4
            if cmd_id == 0:
                size = 24 + (8 if data[pos + 24] == 255 else 0)
                target = i32(pos)
                out["orders"].append({
                    "t": duration, "p": player, "sel": sel,
                    "kind": "target" if target != -1 else "move",
                    "target": target if target != -1 else None,
                    "x": round(f32(pos + 8), 1), "z": round(f32(pos + 16), 1)})
            elif cmd_id in (4, 12, 13, 23, 24, 25, 34, 37, 46, 53, 57, 61, 63):
                out["orders"].append({"t": duration, "p": player, "sel": sel,
                                      "kind": "control", "cmd": cmd_id})
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
                out["builds"].append({"t": duration, "p": player, "proto": i32(pos)})
                size = 44
            elif cmd_id == 12:
                size = 36 + (1 if unknown1 == 0 else 0)
            elif cmd_id == 16:
                out["resigns"].append({"t": duration, "slot": i32(pos + 4)})
                size = 13
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


def build_events(path, game, players, cmds, protos, techs, objects=None):
    """Every action from the replay as one flat timestamped event list (JSON)."""
    objects = objects or {}

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
        else:
            events.append({"t_ms": m["t"], "type": "chat",
                           "player_id": m["from"], "player": player_of(m["from"]),
                           "to_id": m["to"], "text": msg})
    for c in cmds["trains"]:
        nm = protos.get(c["proto"], f"unit#{c['proto']}")
        events.append({"t_ms": c["t"],
                       "type": "placement" if PLACEMENT_PAT.search(nm) else "train",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "unit": nm, "proto_id": c["proto"]})
    for c in cmds["builds"]:
        events.append({"t_ms": c["t"], "type": "build",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "building": protos.get(c["proto"], f"bldg#{c['proto']}"),
                       "proto_id": c["proto"]})
    for c in cmds["techs"]:
        tech = techs[c["tech"]] if 0 <= c["tech"] < len(techs) else f"tech#{c['tech']}"
        events.append({"t_ms": c["t"], "type": "research",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "tech": tech, "tech_id": c["tech"]})
    for c in cmds["shipments"]:
        events.append({"t_ms": c["t"], "type": "shipment",
                       "player_id": c["p"], "player": player_of(c["p"]),
                       "card_slot": c["card"]})
    for r in cmds["resigns"]:
        events.append({"t_ms": r["t"], "type": "resign",
                       "player_id": r["slot"], "player": player_of(r["slot"])})
    events.sort(key=lambda e: e["t_ms"])
    for e in events:
        e["t"] = fmt_t(e["t_ms"])

    resigned = {r["slot"] for r in cmds["resigns"]}
    return {
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
.tip { position: fixed; display: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 8px;
  font-size: 12px; pointer-events: none; z-index: 9;
  font-variant-numeric: tabular-nums; box-shadow: 0 2px 8px rgba(0,0,0,0.12); }
"""

TOOLTIP_JS = """
const tip = document.createElement('div'); tip.className = 'tip';
document.body.appendChild(tip);
document.addEventListener('mouseover', e => {
  const t = e.target.closest('[data-tip]');
  if (t) { tip.textContent = t.dataset.tip; tip.style.display = 'block'; }
  else tip.style.display = 'none';
});
document.addEventListener('mousemove', e => {
  tip.style.left = Math.min(e.clientX + 12, innerWidth - 180) + 'px';
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
        s.append(f'<line x1="{L}" y1="{y:.1f}" x2="{L + pw}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        s.append(f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" fill="var(--muted)">{v:.0f}</text>')
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
        s.append(f'<circle cx="{x + 4}" cy="{y - 3:.1f}" r="4" fill="{colors[pid]}"/>')
        s.append(f'<text x="{x + 12}" y="{y:.1f}" fill="var(--ink-2)">{tip_label[pid][:12]} · {v}</text>')
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


def _page_data_js(players, events, duration_ms, colors):
    """Compact per-page dataset + brush/details logic for the activity chart."""
    unit_names = []
    unit_idx = {}
    orders, trains, alerts, chat = [], [], [], []
    kind_code = {"move": 0, "target": 1, "control": 2}
    for e in events:
        ts = e["t_ms"] // 1000
        if e["type"] == "order":
            k = 3 if e.get("target_owner") == "Gaia" else kind_code[e["kind"]]
            orders.append([ts, e["player_id"], k, e["units_selected"]])
        elif e["type"] == "train":
            if e["unit"] not in unit_idx:
                unit_idx[e["unit"]] = len(unit_names)
                unit_names.append(e["unit"])
            trains.append([ts, e["player_id"], unit_idx[e["unit"]]])
        elif e["type"] == "flare" or (e["type"] == "system" and "alerted danger" in e["text"]):
            alerts.append([ts, e.get("player_id") or 0])
        elif e["type"] == "chat":
            chat.append([ts, e["player_id"], e["text"][:120]])
    data = {
        "dur": duration_ms // 1000,
        "players": [[p["id"], p["name"]] for p in players],
        "colors": {p["id"]: colors[p["id"]] for p in players},
        "units": unit_names, "orders": orders, "trains": trains,
        "alerts": alerts, "chat": chat,
        "geom": {"L": 130, "PW": 706, "W": 860},
    }
    return "const D = " + json.dumps(data, separators=(",", ":")) + ";" + """
function fmt(s){return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
function inR(t,a,b){return t>=a&&t<b;}
function render(a,b){
  document.getElementById('selrange').textContent=fmt(a)+' – '+fmt(b);
  let rows='<tr><th>Player</th><th>Attack orders</th><th>Gather</th><th>Moves</th><th>Peak army</th><th>Alerts</th><th>Units queued</th></tr>';
  for(const [pid,name] of D.players){
    const o=D.orders.filter(e=>e[1]===pid&&inR(e[0],a,b));
    const tgt=o.filter(e=>e[2]===1).length, mv=o.filter(e=>e[2]===0).length, ga=o.filter(e=>e[2]===3).length;
    const peak=o.reduce((m,e)=>Math.max(m,e[3]),0);
    const al=D.alerts.filter(e=>e[1]===pid&&inR(e[0],a,b)).length;
    const tc={};
    for(const t of D.trains) if(t[1]===pid&&inR(t[0],a,b)) tc[D.units[t[2]]]=(tc[D.units[t[2]]]||0)+1;
    const tl=Object.entries(tc).sort((x,y)=>y[1]-x[1]).map(([u,n])=>u+' ×'+n).join(', ');
    rows+=`<tr><td><span class="chip" style="background:${D.colors[pid]}"></span>${name}</td>
      <td class="num">${tgt}</td><td class="num">${ga}</td><td class="num">${mv}</td><td class="num">${peak||'—'}</td>
      <td class="num">${al}</td><td>${tl||'—'}</td></tr>`;
  }
  let ch='';
  for(const c of D.chat) if(inR(c[0],a,b)){
    const name=(D.players.find(p=>p[0]===c[1])||[0,'?'])[1];
    ch+=`<div><span class="num">${fmt(c[0])}</span> <span class="num">${name}:</span> ${c[2].replace(/</g,'&lt;')}</div>`;
  }
  document.getElementById('seldetail').innerHTML='<table>'+rows+'</table>'+(ch?'<div style="margin-top:8px">'+ch+'</div>':'');
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

    # age-ups
    ages = defaultdict(dict)
    for e in events:
        if e["type"] == "system":
            m = re.search(r"(.+) has reached the (\w+) AGE", e["text"])
            if m and m.group(2) not in ages[m.group(1)]:
                ages[m.group(1)][m.group(2)] = e["t"]
    order = ["COMMERCE", "FORTRESS", "INDUSTRIAL", "IMPERIAL"]
    out.append("<h2>Age-Ups</h2><section><table>")
    out.append("<tr><th>Player</th>" + "".join(f"<th>{a.title()}</th>" for a in order) + "</tr>")
    for p in players:
        row = ages.get(p["name"], {})
        out.append(f"<tr><td>{chip(p['id'])}{esc(p['name'])}</td>"
                   + "".join(f"<td>{row.get(a, '—')}</td>" for a in order) + "</tr>")
    out.append("</table></section>")

    battles = find_battles(events, doc["duration_ms"])
    tip_label = {p["id"]: p["name"] for p in players}

    # timeline: age-ups, shipments, battles, resigns on one axis
    out.append('<h2>Timeline</h2><section class="chart">')
    out.append(_timeline_chart(players, events, battles, doc["duration_ms"], colors))
    out.append("</section>")

    # activity histogram with brush selection
    out.append('<h2>Activity (orders per 10s — drag to select a range)</h2>'
               '<section class="chart">')
    out.append(_activity_chart(players, events, doc["duration_ms"], colors))
    out.append("</section>")
    out.append('<h2>Selection <span id="selrange" class="num"></span></h2>'
               '<section id="seldetail"></section>')

    # production charts
    def is_villager(u):
        return u == "Coureur" or u.startswith("Settler") or "Villager" in u

    def is_military(u):
        return not is_villager(u) and not u.startswith("Fishing") and not any(
            u.startswith(x) for x in ("Sheep", "Cow", "Goat", "Llama", "Pet"))

    def cumulative(pred):
        series = {p["id"]: [] for p in players}
        counts = Counter()
        for e in events:
            if e["type"] == "train" and pred(e["unit"]):
                counts[e["player_id"]] += 1
                series[e["player_id"]].append((e["t_ms"], counts[e["player_id"]]))
        return series

    out.append('<h2>Villager Production (cumulative train commands)</h2><section class="chart">')
    out.append(_step_chart(cumulative(is_villager), doc["duration_ms"], colors, tip_label))
    out.append("</section>")
    out.append('<h2>Military Production (cumulative train commands)</h2><section class="chart">')
    out.append(_step_chart(cumulative(is_military), doc["duration_ms"], colors, tip_label))
    out.append("</section>")

    # eco upgrade timing
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
        out.append("<h2>Eco Upgrades (research time)</h2><section><table>")
        out.append("<tr><th>Upgrade</th>" + "".join(
            f"<th>{chip(p['id'])}{esc(p['name'])}</th>" for p in players) + "</tr>")
        for tech, times in rows:
            disp = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tech)
            out.append(f"<tr><td>{esc(disp)}</td>" + "".join(
                f"<td>{times.get(p['id'], '—')}</td>" for p in players) + "</tr>")
        out.append("</table></section>")

    # battles
    out.append("<h2>Battles</h2><section><table>")
    out.append("<tr><th>#</th><th>Time</th><th>Location</th><th>Attack orders (peak army)</th>"
               "<th>Known targets</th></tr>")
    for i, w in enumerate(battles, 1):
        span = f"{fmt_t(w['start'])} – {fmt_t(w['end'])}"
        loc = f"({w['loc'][0]:.0f}, {w['loc'][1]:.0f})" if w["loc"] else "—"
        per = ", ".join(f"{p} {w['orders'][p]} ({w['peak_sel'].get(p, 0)})" for p in w["players"])
        tg = ", ".join(f"{name} ×{n}" for name, n in w["targets"]) or "—"
        out.append(f"<tr><td>{i}</td><td>{span}</td><td>{loc}</td>"
                   f"<td>{esc(per) or '—'}</td><td>{esc(tg)}</td></tr>")
    out.append("</table></section>")

    # units trained (bars per player color; direct-labeled)
    trains = defaultdict(Counter)
    for e in events:
        if e["type"] == "train":
            trains[e["player_id"]][e["unit"]] += 1
    peak = max((n for c in trains.values() for n in c.values()), default=1)
    out.append("<h2>Units Trained</h2><section><table>")
    out.append("<tr><th>Player</th><th>Unit</th><th>Train commands</th></tr>")
    for p in players:
        rows = trains.get(p["id"], Counter()).most_common()
        for j, (unit, n) in enumerate(rows):
            w = max(6, round(n / peak * 220))
            name_cell = f"{chip(p['id'])}{esc(p['name'])}" if j == 0 else ""
            out.append(f'<tr><td>{name_cell}</td><td>{esc(unit)}</td>'
                       f'<td><span class="bar" style="width:{w}px;background:{colors[p["id"]]}"'
                       f' title="{esc(unit)}: {n}"></span><span class="num">{n}</span></td></tr>')
    out.append("</table></section>")

    # buildings
    builds = defaultdict(Counter)
    for e in events:
        if e["type"] == "build":
            builds[e["player_id"]][e["building"]] += 1
    out.append("<h2>Buildings</h2><section><table>")
    for p in players:
        items = ", ".join(f"{b} ×{n}" for b, n in builds.get(p["id"], Counter()).most_common())
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td>{esc(items)}</td></tr>")
    out.append("</table></section>")

    # shipments
    ships = defaultdict(list)
    for e in events:
        if e["type"] == "shipment":
            ships[e["player_id"]].append(e["t"])
    out.append("<h2>Shipments</h2><section><table>")
    for p in players:
        ts = ships.get(p["id"], [])
        out.append(f"<tr><td style='white-space:nowrap'>{chip(p['id'])}{esc(p['name'])}</td>"
                   f"<td class='num'>{len(ts)}</td><td>{esc(', '.join(ts))}</td></tr>")
    out.append("</table></section>")

    out.append("</main>")

    data_js = _page_data_js(players, events, doc["duration_ms"], colors)
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
    protos, techs = build_name_tables(data)
    cmds = parse_commands(data)
    objects = parse_start_objects(data, protos)
    stem = file_stem(path, game)

    def resolve(arg, ext):
        return f"{stem}.{ext}" if arg == "auto" else arg

    doc = build_events(path, game, players, cmds, protos, techs, objects)
    wrote_any = False
    if args.json:
        target = resolve(args.json, "json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, ensure_ascii=False)
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
