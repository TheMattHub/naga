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

## Robot moves

A MOTION block can run a real robot instead of a profile speed. Name one of the
model's `robots` records in the block's `robot` parameter. Its `ptp` and `lin`
segments then take their times from the robot's joints, axis by axis. Plain `move`
segments keep their meaning, so a block can mix both kinds.

The `robots` record describes the hardware once, and any number of blocks can use it:

| Field | Meaning |
|---|---|
| `name` | unique name, picked by a block's `robot` |
| `structure` | `serial6` (six-axis arm), `palletizer4`, `scara4`, `delta`, `gantry`, `serial7`, or `chain` for any serial joint chain |
| `params` | the structure's dimensions in metres (not for `chain`) |
| `joints` | for `chain` only: each joint's `name`, `type` (`revolute` or `prismatic`), `origin` and `axis` at the zero pose, Z up |
| `axes` | one entry per driven axis: `name`, `range` [low, high] in degrees (metres for a sliding axis), `speed` per second, `accel` per second squared |
| `tool` | the tool point from the flange: `xyz` in metres, `rpy_deg` in degrees |
| `payload_max`, `payload_derate` | kg, and the speed fraction at `payload_max`, as on a profile |
| `tcp_speed`, `tcp_accel` | optional caps on the tool's own speed along a `lin` move |
| `reach_m`, `approximate`, `source` | the datasheet's reach, and whether the numbers are estimates |

A robot segment is `{"kind": "ptp" | "lin", "to": <target>}`, with an optional
`payload_kg` that derates every axis. `ptp` moves all axes together so that they
finish at the same moment, and the slowest axis sets the time. `lin` keeps the
tool on a straight line, so it is slower near awkward poses. Without a `time` the
segment is a Move and takes the shortest time the limits allow; with a `time`, a
number in the block's `time_unit`, it is a MoveOverTime that lasts exactly that long. A MoveOverTime shorter
than the limits allow is a problem, never silently stretched.

Targets are named on the block, in its `targets` parameter, because they belong to
one cell while the robot record is reusable. A target is one of four forms:

- `{"joints": [...]}`: one value per axis, in degrees (metres for a sliding axis);
- `{"xyz": [x, y, z]}`: a point of the floor plan in metres, with optional
  `yaw_deg` or `rpy_deg`; the tool points down by default;
- `{"on": "<block>"}`: a point on another block, `at` its `centre`, `start` or
  `end`, moved by an `offset` in the block's own frame (a pallet slot, say).
- `{"belt": "<CONVEYOR>", "divert": k, "z": metres}`: where the robot meets a part on a moving
  belt, solved for you, so the first segment is a `ptp` to it. The belt's divert k has
  `"intercept": true`, and its `on_late` says whether a part waits for a busy robot (`wait`),
  rides on (`pass`) or is judged by an expression.

The robot stands where its block stands on the floor plan, and a frame target is
mapped into the robot's own frame from there. The times are fixed numbers, so they
draw nothing from the block's random stream and a model's other results do not
move. Every cycle starts from the last target of the cycle before, so every cycle
takes the same time.

This cell has two `ptp` moves of 90 degrees on axis 1, each 1.15 s, and a pick
and a place of 1 s each:

```python
from nagasim.core import motion
from nagasim.core.robotics.moves import plan_cycle
from nagasim.core.robotics.records import robot_from_record

arm = {"name": "Arm", "structure": "serial6",
       "params": {"base_height": 0.78, "shoulder_offset": 0.32, "upper_arm": 1.125,
                  "elbow_offset": 0.2, "forearm": 1.1425, "wrist_flange": 0.2},
       "axes": [{"name": f"a{i + 1}", "range": r, "speed": s, "accel": 4 * s}
                for i, (r, s) in enumerate([([-170, 170], 100), ([-65, 85], 90), ([-180, 70], 90),
                                            ([-300, 300], 170), ([-130, 130], 120), ([-360, 360], 190)])],
       "tool": {"xyz": [0, 0, 0.2], "rpy_deg": [0, 0, 0]}, "payload_max": 175}
targets = {"above_pick": {"joints": [90, 0, 0, 0, 30, 0]},
           "above_place": {"joints": [0, 0, 0, 0, 30, 0]}}
segments = [{"kind": "ptp", "to": "above_pick"}, {"kind": "pick", "time": 1.0},
            {"kind": "ptp", "to": "above_place"}, {"kind": "place", "time": 1.0}]
plan = plan_cycle(robot_from_record(arm), targets, segments)     # the moves, as the run plans them
cycle = motion.nominal_cycle_seconds(segments, None, "seconds", "seconds", plan=plan)
print(f"cycle {cycle:.2f} s")      # cycle 4.30 s
```

Every robot problem is found when the model is checked, before the run starts:

| Code | What it means |
|---|---|
| `E_ROBOT` | a record is incomplete: a name, a structure, a dimension or an axis's range, speed or acceleration |
| `E_ROBOT_UNKNOWN` | a block names a robot the model lacks, or has robot moves and names none |
| `E_ROBOT_TARGET` | a move names a target the block lacks, or a target has none of the three forms |
| `E_ROBOT_REACH` | a target is out of reach or outside an axis range |
| `E_ROBOT_SINGULAR` | a `lin` move passes a pose where the joints would have to turn without limit |
| `E_ROBOT_LIN` | a `lin` move leaves the reach or changes the arm's configuration on the way |
| `E_ROBOT_MOVE_TIME` | a MoveOverTime is shorter than the limits allow |

Robot moves model times, not forces: there are no torques, and the payload only
slows the axes as above. `hrc` works on `move` segments only, so a block with
robot moves and `hrc` set is refused. Model a collaborative robot's slowdown with
plain `move` segments instead.

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
