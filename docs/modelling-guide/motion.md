# Motion cycles

A **MOTION** block is a station whose service is a cycle of motions. Use it when a
robot arm, a cobot, a person or a mechanism does the work and you want the cycle
time to follow from distances, speeds and payloads instead of a single
distribution. It seizes, sets up, fails, is preempted and releases exactly like
PROCESS.

## Motion profiles

Declare how something moves in the model's `motion_profiles`:

| Field | Meaning |
|---|---|
| `name` | unique name, picked by the block's `profile` |
| `kind` | `robot`, `human` or `mechanism` (descriptive only) |
| `speed` | m/s, > 0 |
| `accel` | m/s^2, > 0 |
| `payload_max` | kg, optional |
| `payload_derate` | speed fraction at `payload_max`, linear from 1.0 at 0 kg; default 1.0 |
| `collaborative_speed` | m/s, needed when the block's `hrc` is not `none` |

## The cycle

Segments run in order and the service time is their sum:

- `move`: `distance_m`, optional `payload_kg`. Time `d/v + v/a` when `d >= v^2/a`,
  else `2*sqrt(d/a)`, where `v` is the profile speed times the payload derating.
- `pick`, `place`, `tool_change`, `dwell`, `process`: `time`, a number or an
  expression such as `TRIA(0.8,1.0,1.4)`, in the block's `time_unit`
  (default seconds).

The properties panel shows the nominal cycle (means, moves at nominal speed) as you
edit.

## Time units: moves are seconds, segment times follow `time_unit`

A `move` segment's time always comes out of the trapezoid/triangle formula in SI
seconds, whatever the model's base time unit is: `speed` is m/s and `accel` is
m/s^2 by definition, so a move's duration is physically seconds regardless of how
the rest of the model counts time. Every OTHER segment (`pick`, `place`,
`tool_change`, `dwell`, `process`) is timed by its own `time` expression in the
block's `time_unit` -- seconds by default, or `"base"` to follow the model's own
unit. Both kinds of segment then convert into the model's base unit for the run,
so the cycle is the same physical duration whether the model runs in seconds or in
minutes.

For example, take a cycle of one 0.8 m move (profile `speed=1.0` m/s,
`accel=2.0` m/s^2, so the move takes `0.8/1.0 + 1.0/2.0 = 1.3` s) followed by a
`pick` segment with `"time": "1.0"` and the block's default `time_unit` of
seconds:

- In a model whose base unit is **seconds**: the move contributes `1.3` (already
  seconds) and the pick contributes `1.0` (seconds, `time_unit` = seconds) -- a
  2.3 s cycle.
- In a model whose base unit is **minutes**: the move still takes 1.3 PHYSICAL
  seconds, converted to `1.3 / 60` minutes; the pick's `"time": "1.0"` is still
  seconds (the block's `time_unit` did not change), converted to `1.0 / 60`
  minutes -- the same 2.3 s cycle, now reported as `2.3 / 60` minutes.
- Setting the block's `time_unit` to `"base"` makes the pick's `1.0` mean 1.0 OF
  THE MODEL'S OWN UNIT instead -- 1.0 minute in a minutes model -- so the cycle
  is no longer the same physical duration; `"base"` is for when the segment
  really is meant to scale with the model's unit, not seconds.

## Human-robot collaboration

`hrc` is `none`, `collaborative` (every move at `collaborative_speed`) or
`separation`: a move starts at `collaborative_speed` when a unit of a worker fleet
is within the zone's radius of its node. A move already under way keeps its speed.

A safety zone is best declared once, as a record of the `zones` section, and
named by every block it protects. The record holds `name`, `node`, `radius_m` and
an optional `measure`, and a block names it with `"zone": {"name": "Cell A zone"}`.
Blocks that name the same zone share its radius, and a scenario or DOE factor can
vary it as `zones.<name>.radius_m`. A block that names no zone keeps its own
`"zone": {"node": ..., "radius_m": ...}`, which is also the fallback the inline
fields give when `name` is empty. A STATION's `tending` group takes the same
`zone`.

```json
"zones": [{"name": "Cell A zone", "node": "cell_a", "radius_m": 1.5, "measure": "straight"}]
```

`measure` says how the distance is taken. `"path"`, the default, walks the path
network from the worker to the zone node, so a worker behind a rack is far away.
`"straight"` takes the straight line between the worker's floor point and the
node, as a safety laser scanner measures it, and the layout view draws it as a
circle. A walking worker's floor point is its place along the aisle it is on. A
worker summoned to a station counts at the node it left until it arrives. The
choice matters most beside racks and corners, where the two distances differ.

The zone has these limits:

- The speed is decided when each move starts, or once per tending cycle. A
  worker who walks in during a move does not slow that move.
- A zone only slows, to the profile's collaborative speed. There is no
  protective stop and no speed ramp.
- Only worker fleets count, and `"straight"` ignores walls and line of sight.
- Only straight `zones` records are drawn, and only in the 2D layout view.
- The Automation library's Tended machine always tends with `hrc` `none`.

## Results

The `motion_summary` frame gives, per block and
segment kind, the mean time, the share of the cycle, and for moves the share run
at collaborative speed. Scenario and optimizer keys read
`motion/<block>/<kind>/Mean Time`. Library assets: Manufacturing > "Manual
assembly (motion)", Logistics > "Person-to-goods picking (motion)".

## Known gap: planning

The CP-SAT scheduler (`nagasim/cpsat.py`) ignores MOTION blocks: it plans around
PROCESS/SEIZE/RELEASE stations, so a MOTION station's cycle time is invisible to
the planner. A model that mixes planned stations with a MOTION cell should treat
the MOTION cell's throughput as a fixed input to the plan, not something the
scheduler optimizes around.
