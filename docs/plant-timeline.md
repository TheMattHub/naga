# A plant as a timeline

The views of a run (the HTML player, the debugger's
inspection) draw one thing: the timeline a run's trace reduces to. A **plant mapping** turns what
a plant sends into the same trace events, so the same views show the plant, and a run can start
from what the plant looks like now.

```console
naga plant init model.json --out line.plantmap.json      # a starter mapping, to edit
naga plant check line.plantmap.json model.json           # does it fit this model?
naga plant mock model.json --map line.plantmap.json --out plant.jsonl   # no plant at hand: a run of the model writes the log
naga plant replay line.plantmap.json model.json plant.jsonl --player plant.html --state now.json
naga run model.json --state now.json                     # start from what the plant looks like
```

With a plant, record what it sends instead of the mock, or follow it as it arrives:

<!-- no-run: needs a plant and your connections file -->
```console
naga watch model.json --connections my.json --record plant.jsonl
naga plant watch line.plantmap.json model.json --connections my.json --state now.json
```

Everything here is **read-only**.

## The mapping file

A plant says "this key has this value at this time" ([Live data](live-data.md)). The mapping says
what each key asserts about the model. It is data: no path, no address, no code, no expression.

```json
{"format": 1,
 "time": {"epoch": "2026-10-05T06:00:00+02:00", "unit": "minutes"},
 "signals": [
  {"source": "line", "key": "press/state", "as": "state", "resource": "Press",
   "states": {"RUN": "busy", "IDLE": "idle", "FAULT": "down"}},
  {"key": "press/job/*", "as": "job", "resource": "Press"},
  {"key": "buffer/count", "as": "queue", "block": "Buffer", "type": "PART"},
  {"key": "wo/{id}", "field": "station", "as": "at",
   "blocks": {"OP10": "Drill", "OP20": "Press"}, "gone": ["DONE"],
   "type_field": "product", "types": {"A-100": "PART"}},
  {"key": "gate/rfid", "as": "seen", "block": "Inspect"},
  {"key": "tank/level", "as": "level", "level": "Tank", "scale": 0.001},
  {"key": "press/parts", "as": "counter", "block": "Press", "rollover": 65536}]}
```

| `as` | The key says | The timeline shows |
|---|---|---|
| `state` | the station's state: `states` maps the plant's value to `busy`, `idle`, `blocked`, `down` or `up` | the resource's state |
| `capacity` | how many units the resource has | its capacity |
| `busy`, `blocked` | how many units are busy, or blocked | that many units held, by nobody in particular |
| `job` | which item the resource works on, or none; `*` in the key is a position of a station with several | that entity in service |
| `queue` | how many items are in a block | that many anonymous entities |
| `at` | where item `{id}` is: `blocks` maps the plant's value to a block; a value of `gone` (or no value) ends it, one of `merged` takes it into a batch | the entity created where it is first seen, moved when seen elsewhere, gone |
| `seen` | the item named by the value is in `block`, or with `"leaves": true` has left | the same |
| `vehicle` | where vehicle `unit` (from 0) of `fleet` is: `nodes` maps the plant's value to the network node it last reported | the vehicle at that node |
| `load` | which item vehicle `unit` of `fleet` carries, or none | nothing by itself: the state of now puts the item on that vehicle |
| `level` | what a level holds, times `scale` | the level, linear between two values |
| `counter` | the plant's running total of items that left `block` (parts made at a station, parts scrapped at an exit) | nothing: a count is not an item. The twin shows it and compares forecasts with it |
| `order` | order `{id}` of the plant's book, one row: `fields` names its cells, `statuses` maps its status | nothing: an order is not an item. The state of now carries the book (see "Orders") |

- `time.epoch` is the instant model time 0 stands for; `unit` defaults to the model's time unit.
  The time of a value is its source's own stamp; `"stamp": "received"` uses the receipt time.
- `source` limits a signal to one data source. `field` picks one cell when the value is a row (a
  polled source). Names are the flattened model's (`cell_1/drill`). A value is compared as text:
  `true`, `false`, `null`, a whole number with no fraction.
- A resource may be a fleet. An entity's type comes from the mapping (`type`, or `types`), never
  from the plant.

**Not mappable now:** a vehicle's progress along a segment, its battery and what it is on its way
to; chargers, conveyors, cycle phases and agents; an item on a timed link (it stays in the block it left until it is seen at the next);
attributes; a job's remaining time; a counter of a resource or of one item type.

**Orders.** A twin can follow the plant's order book, read from whatever your MES or ERP
publishes, so its forecast releases your orders and not the model file's. An `order` signal reads
one row per order. `fields` names the row's cells for `type` (the product, mapped by `types` to
an entity type and by `sequences` to one of the model's routings), `quantity`, `done` (jobs
finished), `due`, `release` (instants, as stamps are), `priority` and `status`. `statuses` maps
each of your statuses to `waiting`, `held`, `released`, `done` or `gone`:

```json
{"source": "work_orders", "key": "{id}", "as": "order",
 "fields": {"type": "product", "quantity": "quantity", "done": "quantity_done",
            "due": "due", "priority": "priority", "release": "release", "status": "status"},
 "types": {"WIDGET": "WIDGET"}, "sequences": {"WIDGET": "Widget"},
 "statuses": {"planned": "waiting", "in progress": "released", "complete": "done",
              "held": "held"}}
```

An order's jobs are items: an `at` signal names them, and its `order_field` names the cell
holding each item's order. A job then carries its order's id, due date and priority, and its
routing follows the order's sequence. A row that cannot be read is dropped whole, and an
order's id is kept only when it is a plain id (a letter or digit, then up to 63 letters,
digits and `_ . : / -`). With this, the twin can also recommend `hold`, `release`, `expedite`
and `resequence` (see Digital twin, "Recommendations").

**Counters.** Many plants give no item ids, only totals. A `counter` is counted from where the
twin first sees it: a first value of 120 000 is a starting point, not 120 000 parts. Each
increase adds. A value lower than the last one adds nothing and the count goes on from it,
with a warning (`W_PLANT_CONTRADICTION`) and one more in the counter's `resets`: the total is
then a lower bound. If your counter wraps (a 16-bit register returns to 0 at 65 536), say so
with `rollover` and the wrap is counted through. A value that is negative, fractional or not a
number is dropped. A counter and a `queue` (or identities) may speak of the same block: one
says what is there, the other what has left. `naga plant init MODEL --counters` writes one
for every block.

## What the engine does with what does not fit

It never guesses silently. Each case is counted, and reported once with its code:

| Case | Rule | Code |
|---|---|---|
| A value the mapping cannot read, a time before the epoch, something over a bound | dropped | `W_PLANT_DROPPED` |
| An event older than what its key already said | dropped | `W_PLANT_DROPPED` |
| No source stamp; a stamp more than 30 s ahead of its receipt | the receipt time is used | `W_PLANT_TIME` |
| A late event, on a live feed | applied at the timeline's last instant (a recorded log is sorted first) | `W_PLANT_TIME` |
| A queue length, a unit count | anonymous entities (ids below -1), units held by nobody | `W_PLANT_SYNTHETIC` |
| A job for an item seen nowhere | the resource is busy, nobody is in service | `W_PLANT_CONTRADICTION` |
| A job for an item in a block that does not seize that resource | shown as said; a state of now puts nobody in service and the start is refused (`E_INITIAL_BUSY`) | `W_PLANT_CONTRADICTION` |
| More units busy than the capacity the plant gave; a level outside its declared `min` and `capacity` | shown as said | `W_PLANT_CONTRADICTION` |
| The feed lost events, or its stream broke | recorded with its time; the next values put the timeline right, and an item whose leaving was missed stays until it is seen again | `W_PLANT_GAP` |

The same value delivered twice changes nothing. A mapping in which two signals tell one thing (a
resource's busy units by `job` and by a count, a block by identities and by `queue`) is refused
(`E_PLANTMAP_DOUBLE`).

Worth knowing:

- **A late event on a live feed has no duration.** It is applied at the timeline's last instant,
  so a station that ran and stopped inside one late batch shows no busy time. Utilisation read
  off a live observed timeline is a lower bound; replay the recorded log, which is sorted first,
  for an exact one. A twin's scores use readings at an instant and counters, not durations:
  measured, lateness of up to ten seconds changes them little (see Digital twin, "Scores").
- **A job outlives its item.** A resource stays busy until its `job` key says nothing, also when
  the item it named is gone: the engine does not end one signal on another's word.
- **A long watch forgets.** When an item is gone its key is forgotten, and when 100 000 keys are
  held the one longest silent makes room for a new one (counted). A forgotten key loses only the
  check "is this older than what it said". The last 10 000 items that left keep the time they
  left at, so an old sighting of one of them, delivered again, is dropped; beyond those it
  would show the item again until its key says gone.
- **A release names where the item is**, not the block that released it in a run. No view reads it.
- An item nobody typed is `Unknown`, whether it has a name or is one of a queue length.
- A stamp is a number of seconds since 1970 or ISO 8601 text; `time.epoch` lies from 1970 to 2200.
- One count, state or level is told by one signal: two `queue` signals for one block, or two
  `capacity` signals for one resource, are refused like any two signals telling one thing.
  Several `job`, `at` and `seen` signals are fine.

## A state from now

`--state FILE` writes a [state document](digital-twin.md) of the plant at the last instant, with
its clock, so `naga run --state` starts there. It holds each entity in its block in arrival order,
which are in service, the busy units, a resource that is down (when it has one failure) and the
levels. The plant does not say when a service ends, so one in progress is drawn afresh. Units
reported busy with no job named: when one block seizes the resource its longest-waiting entities
are taken as in service (`W_PLANT_SERVICE_INFERRED`); otherwise the start is refused
(`E_INITIAL_BUSY`). An item in a BUFFER has one of its places, the ones beyond its capacity wait
to get in, and an item reported further on while its last station still holds its unit (`job`)
leaves that station blocked. A fleet is placed when every vehicle reports a node: each stands
there idle, or carries its load from there to the destination its TRANSPORT block declares, the
drive starting afresh. A fleet with a vehicle not observed, a load whose block declares no
destination and a load waiting for a vehicle are said (`W_STATE_NOT_COVERED`), and a twin then
refuses the forecast by name. Agents, variables and arrival clocks start as the model declares
them (`W_STATE_NOT_COVERED`).

With an `order` signal the state carries the plant's book (`order_book`) in place of the model's
orders. The released ones count as released, so nothing is released twice, and the others are
released at their own times; a held one waits until it is released. An order with a job on the
floor counts as released, whatever its status said (`W_PLANT_CONTRADICTION`). A done order is
left out, since the forecast looks ahead.

## Bounds

A mapping holds at most 1 MiB, 2000 signals and 10 000 entries a table. A log holds at most
256 MiB and a million lines of 64 KiB. A recording maps at most 250 000 events; 100 000 entities
are known at once (a part inside a batch makes room first); a count or a capacity is at most 10 000; a key, an id or a text is at most 1024 bytes.
Nothing from a log is ever text in a player file: an item's id becomes a number, and every name
comes from the mapping and the model. The one exception is an order's id, a plain id, which a
state, a recommendation and the order results name the order by. At most 100 000 orders are
known at once.
