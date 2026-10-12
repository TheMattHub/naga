# Mobile cobots: machine tending

A mobile cobot is a vehicle with an arm: it carries a part to a machine, puts it
in, and later takes it out and carries it on. Use it to answer "how many machines
can one cobot tend": the machine waits for the cobot, and the cobot waits for the
machine.

## The fleet

Give a fleet an arm:

| Field | Meaning |
|---|---|
| `arm` | the name of a record of `motion_profiles`; empty: an ordinary fleet |
| `exchange` | the cobot has a second holding place (double gripper or tray); default off |

Everything else about the fleet (speeds, dispatch, park, battery, schedules,
failures) works as for any vehicle. An arm fleet carries one part, or two during an
exchange; it cannot be a worker or gantry fleet.

## The station

Tick Tending on a STATION block (file key `tending`):

| Field | Meaning |
|---|---|
| `place` | the arm cycle that puts a part into the station, written as [MOTION segments](motion.md) |
| `pick` | the arm cycle that takes a part out |
| `time_unit` | unit of the timed segments, default seconds |
| `hrc`, `zone` | human-robot collaboration, as on a MOTION block |
| `cobot` | `leave` (default) or `stay` |

A missing cycle uses the fleet's flat unload or load time. Tending applies only
when an arm fleet serves a station that has it: a cobot at a plain station, or a
forklift at a tending station, behaves like any vehicle.

## What happens

"The machine" is the first block after the STATION that holds a resource (PROCESS,
SEIZE, MOTION) or has finite space (a BUFFER, a `queue_capacity`).

- **Pick.** A finished part stays in the machine, which cannot start another,
  until a cobot has arrived and finished the pick cycle.
- **Place.** A cobot arriving with a part waits, holding it, until the machine
  accepts it. Then the place cycle runs, and the machine starts when it ends.
- **Leave.** After placing, the cobot is free for other jobs; any cobot of the
  fleet comes back for the pick.
- **Stay.** After placing, the cobot waits at the station and picks the part
  itself, with no dispatch. It counts as busy the whole time and drains its
  battery at the idle rate. If the part goes on with another fleet or leaves the
  model (a DISPOSE, a balk or renege routed to one, a BATCH that absorbs it), the
  cobot is freed at that instant.
- **Exchange.** A cobot with `exchange` arriving with a new part at a machine
  holding a finished one takes the finished part out, puts the new one in, and
  carries the finished part on, saving a trip.

## One cobot and no exchange

A single cobot holding a new part for a machine that waits to be emptied can never
finish. Validation warns beforehand (`W_TENDING_DEADLOCK`), and the run stops with a
deadlock error, naming the fleet and the stations, at the instant every cobot of the
fleet waits loaded at a machine whose finished part waits for a cobot of that fleet.
Turn on `exchange`, add a second cobot, put a BUFFER before the machine, or let the
cobot `stay`.

The error is raised only when the stall is certain: the machine is one PROCESS or
MOTION with no queue places, balking, reneging or preemption, on a plain resource
with no capacity schedule. Other stalls (a full BUFFER before the machine, a SEIZE
... RELEASE machine) still show as `Picks` far below `Places`.

## Results

The `tending_summary` frame gives, per station and fleet: picks,
places, exchanges, the mean pick and place cycle, the mean time a cobot waited
loaded for the machine, the mean time a finished part waited in the machine for a
pick to start, and the mean stay. Scenario and optimizer keys read
`tending/<station>/<fleet>/<column>`.

## Not modelled

Working on a part on board or while driving, more than two holding places, and
multi-load tours for arm fleets. If the first block after the station holds
nothing (a DELAY), it starts while the place cycle is still running.
