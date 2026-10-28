Use MOTION for a robot or cobot station whose cycle time should come from the motions it makes: moves, picks, places, tool changes and dwells. It is a [PROCESS](process.md) whose service time is the sum of its segments. If a single distribution describes the service well enough, PROCESS is simpler.

## Behaviour and rules

The `segments` run in order. A `move` has a distance in metres and an optional payload in kilograms. The other kinds (`pick`, `place`, `tool_change`, `dwell`, `process`) have a `time`, a number or an expression, in the block's `time_unit`, which is seconds by default. Moves are always computed in seconds.

A move is timed from the `profile`, a record in `motion_profiles` with speed, acceleration, payload derating and a collaborative speed. A move is timed when it starts, because the separation mode looks at where the workers are at that moment. A move in progress is never re-timed.

With `hrc` set to `collaborative`, every move runs at the collaborative speed. With `separation`, a move runs at that speed only when a worker from a worker fleet is within the zone's radius. Seizing, setups, failures, preemption, workers and blocking work as in PROCESS.

The `motion_summary` frame breaks the cycle time down by segment kind and reports the share of moves run in collaboration.
