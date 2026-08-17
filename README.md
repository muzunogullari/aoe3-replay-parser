# AoE3 DE Replay Parser

Extracts the full game history from an Age of Empires III: Definitive Edition
replay (`.age3Yrec`) into plain text you can read directly or paste into an AI
assistant (Claude, etc.) for game analysis.

What it extracts, all with game-time timestamps (mm:ss):
- **Game setup** — game name, map, treaty/trade-monopoly/team-lock flags
- **Players** — name, civilization, team, Home City name/level, explorer
- **Outcome** — who resigned when, and the winning team
- **Chronological log** — chat (attributed to sender), age-ups, tech research
  (queued and completed), shipment arrivals, tributes and explorer ransoms,
  minimap flares with map coordinates (flare/danger clusters mark battles)
- **Units trained** per player (train commands; batch civs queue several units
  per command)
- **Buildings placed** per player
- **Wall segments / buildable mines / map-object interactions** per player
- **Shipments sent** per player (deck card slot + time)
- **Age-up recap** per player

## Requirements

Python 3.8+ (standard library only, no installs needed).

## Usage

```
# Main output: full timestamped event stream as JSON (latest replay auto-found).
# Flags without a value use the standardized name aoe3_<date>_<time>_<map>.<ext>
python aoe3_replay_parser.py -j

# All three outputs: JSON event stream, text report, post-game HTML summary
python aoe3_replay_parser.py -j -o --html

# Explicit names and a specific replay also work; no flags = JSON to stdout
python aoe3_replay_parser.py "path\to\Record Game.age3Yrec" -j out.json --html report.html
```

`--html` writes a single-file post-game page: players and result, age-up
times, wars (clusters of danger alerts and minimap flares with locations),
units trained, buildings, shipments and chat. Light/dark follows the OS.

The JSON document has `game` (settings), `players`, and `events` — one flat
array where every action is its own record with `t_ms` (game-time
milliseconds), `t` (mm:ss), `type`, and the acting player. Event types:
`train`, `build`, `placement` (walls/mines/map objects), `research`,
`shipment`, `chat`, `system` (notifications with recipient), `flare`
(minimap ping with x/z coordinates), `resign`.

Replays live in `C:\Users\<you>\Games\Age of Empires 3 DE\<steam-id>\Savegame\`.
The game overwrites `Record Game.age3Yrec` each match, so copy it out if you
want to keep a game permanently.

## How it works

`.age3Yrec` = an `l33t` magic header + one zlib stream holding a world
snapshot and the command stream. The parser:

1. Reads the typed key/value game-settings block (players, map, options).
2. Resolves unit and tech names from the runtime `protoy.xml` / `techtreey.xml`
   XMB documents embedded in the snapshot itself — so it automatically matches
   whatever game patch the replay was recorded on. Unit ids come from proto
   `id` attributes; tech ids are ordinals in the techtree document (both
   verified against in-game completion messages).
3. Walks the command stream (framing ported from
   [h3902340/aoe3de-replay-parser](https://github.com/h3902340/aoe3de-replay-parser),
   MIT). Time deltas are milliseconds of game time. Command 1 = research,
   command 2 = proto-target command (training, wall placement, buildable
   mines, map objects; proto −1 in shipment mode = send home-city card),
   command 3 = place building, command 16 = resign.

## Known limits

- Kill counts, resources gathered, and score are computed by the game engine
  during playback and are **not stored** in replay files. For the score screen,
  rewatch the replay in-game.
- Chat "to" routing distinguishes team/all imperfectly; senders are exact.
- The log ends at the last resign — victory banners fire after recording stops.
