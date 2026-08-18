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

`--html` writes a single-file post-game page organized as collapsible
sections: Players &amp; Timeline · Aging · Economy · Military · Shipments
&amp; Decks · Improvements · Battle Analysis · Battles. Content: Players · Timeline
· Aging (every age-up with all players' villagers, military and resources
spent at that moment) · Economy (villager production, cumulative resources
spent, spend totals per resource, economy upgrade timings, economy
buildings) · Military (production curve, units trained, military buildings)
· Shipments · Improvements (all research, chronological) · Activity (orders
per player per 10s — drag to select a range and inspect attack/gather/move
orders, peak army, alerts, resolved targets and units queued) · Map (the
selected range plotted spatially) · Battles (cards with a locator mini-map
of every attack order and hit, sides, per-player orders and army size, and
each player's villagers/military/spend at battle start). Light/dark follows
the OS.

### Simulation engines (built on the game's own data)

The replay embeds the runtime `protoy.xml`/`techtreey.xml`, which carry the
actual simulation data: unit/building costs, train batch sizes, tech costs,
villager gather rates, unit hit points, damage and rate of fire, crate
contents, and tech effects (gather work-rate and Damage/Hitpoints
modifiers). On top of the deterministic command stream the parser runs:

- **Spend reconstruction (exact):** every train/build/research event is
  annotated with its real cost; cumulative resources spent per player is
  derived, not estimated.
- **Economy engine (model estimate):** income is built from the game's own
  numbers — the civ villager's per-task gather rates from protoy (hunt
  0.84/s, mill 0.67, tree 0.50, mine 0.60, estate 0.50), researched gather
  techs applied to their matching task, mills/plantations switching
  food/coin gathering off hunts/mines once built, villager build limits
  (e.g. Coureur 80), and villager deaths reducing the workforce. Ottoman
  auto-spawn uses Settler trainpoints with their church TrainPoints
  effects (Millet −4s, Koprulu −6s) and BuildLimit effects (base cap 25,
  Galata +20, Topkapi +25) per Town Center. Stockpile = start + crates +
  tributes + income − exact spend. The macro food/wood/coin split and a
  0.9 utilization factor are the remaining modeling assumptions. Exposed
  as `economy_estimate_10s` in the JSON and the stockpile chart.
- **Selected deck &amp; named shipments:** each player's deck library is
  parsed from the replay; the deck actually used is solved by testing every
  candidate against the game's send/arrival record, using the fact that the
  in-game shipment panel orders a deck's cards by (card age, home-city list
  position) — card ages come from the per-civ homecity files in the game's
  Data.bar. Every shipment send is then named (`card` / `card_name` on
  shipment events; `selected_decks` in the JSON), with the match confidence
  reported.
- **Loss detection (mechanical core):** instance ids are
  `(generation << 16) | slot`, and the engine reuses a slot only after its
  unit dies — a later id appearing on the same slot proves the earlier unit
  died between the two sightings (verified on a 50-minute game: 529 reused
  slots, 1031 consistent orderings, 1 violation, generation strictly
  monotonic). Confirmed deaths anchor the loss model; ids that stop
  appearing mid-game are added as unconfirmed losses. Deaths carry a
  [last-seen, slot-reuse] time bracket and are assigned to battles by
  bracket overlap, so defenders killed while unselected still land in the
  right battle. Disappearance clusters around Veteran/Guard researches are
  recognized as upgrade re-instancing, not deaths. Villager vs military
  classification uses start-object types, build-command selections (only
  villagers and wagons construct) and gather-order ratios.
- **Tributes and market trades:** tribute commands (who sent what resource
  to whom) and market buy/sell commands are decoded as `tribute` and
  `market` events, shown under Economy → Transfers &amp; Market.
- **Combat power engine (model estimate):** per battle and player, observed
  peak army size × average unit strength (hp × dps from protoy, with
  researched Veteran/Guard-style Damage/Hitpoints upgrades applied).
  Exposed as `power_estimate` in the JSON `battles` list and on battle
  cards.

The hard boundary remains: actual combat outcomes (kills, deaths) depend on
engine targeting and pathfinding and cannot be faithfully reproduced from
orders — power numbers are strength-on-paper, not results.

### Combat data: what a replay can and cannot tell you

Replays store player **orders**, not simulation results. Damage dealt/taken,
kills and hit points are computed by the engine during playback and are not
in the file. What this parser extracts instead: every targeted order (which
object a group of N selected units was ordered to attack, with map
coordinates), resolved to a unit type and owner when the target existed at
game start (buildings, explorers, starting units, huntables). Orders
targeting Gaia objects (hunts, trees, treasures) are classified as gather,
not combat. The one recorded death-adjacent event is the explorer knockdown:
ransom payments appear in the notification feed and are emitted as
`explorer_ransom` events (who fell, the amount, who was paid) and shown on
the battle card for the window they fell in.

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
