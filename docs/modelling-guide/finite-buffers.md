# Finite buffers and blocking after service

A queue in front of a station is unlimited unless you say otherwise. Real floor space is not:
when the space after a machine is full, the machine cannot put its finished part down, so it
keeps holding it and cannot start the next one. That is **blocking after service**, and it is
where a production line's throughput actually goes.

## Two ways to declare the space

| | Use it when |
|---|---|
| **BUFFER** block, `capacity` places | the space belongs between two stations -- a conveyor spur, a pallet stand, a bay |
| `queue_capacity` on a SEIZE or PROCESS | the space belongs to the station: its own input queue |

Both mean the same thing to the engine. A part takes one place when it arrives, keeps it while it
waits, and gives it back when a station starts work on it.

- `capacity` 0 on a BUFFER is legal and means **no place at all**: the part stays in the machine
  that made it until the next station starts it, the classic zero-buffer line.
- `queue_capacity` 0 (the default) means an **unlimited** queue -- exactly what every model did
  before. There is no unlimited BUFFER: an unlimited queue is what drawing no buffer means.

You never switch blocking on. A station holds its part whenever a finite space is what it feeds,
and behaves exactly as before when it is not.

## What counts as "handing the part over"

The part leaves the machine at the first block that gives it somewhere of its own to be. These
blocks are **transparent** -- the part crosses them without ever leaving the machine, because
they take no time and hold nothing:

`ASSIGN`, `RECORD`, `DECIDE`, `STATION`, `LOGIC`

Everything else takes the part on arrival, because it really does take it off the machine: a
`DELAY` (it is in transit), a link with a transfer delay, a `CONVEYOR` or `TRANSPORT` (which have
their own capacity and their own blocking), a `BATCH`, `MATCH`, `SEPARATE` or `HOLD` (unbounded
holding areas of their own -- put a BUFFER in front of one if the space matters), a `ROUTE` jump,
and `DISPOSE`.

A `DECIDE` between two stations is transparent, so the space is only needed on the branch the
part actually takes. One exception: a `TRANSPORT` of a fleet with an `arm` takes a part from a
station with [tending](tending.md) only when the cobot has finished its pick cycle, so that
machine stays blocked until then.

When several machines are blocked on one buffer, they are served **first blocked, first in**.

## What you get back

The `buffer_summary` frame has one row per buffer and per station that blocks:

| Column | Meaning |
|---|---|
| `Capacity`, `Avg Content`, `Max Content`, `Time Full %` | the block's input space: how big, how full, how often full |
| `Blocked %` | share of the station's scheduled time spent holding a finished part with nowhere to put it |
| `Starved %` | share spent with a free machine and nothing to work on |

Blocked time is time the resource is still seized, so it is inside `Usage %` and is charged at
the busy cost rate -- the machine is occupied, it is just not producing. `Usage % = working +
blocked`, and working + blocked + starved + down + off-shift is the whole of a station's time.
Buffer-allocation studies read blocked and starved together: space that stops one station
blocking only pays if it stops the next one starving.

The same numbers are scenario KPI keys (`buffer/<block>/Blocked %`), so `naga scenarios`,
`naga doe` and `naga optimize` can all trade buffer places off directly.

## Workers at a blocked station

A station whose work is done by a [worker](layout-paths-vehicles.md#workers) can keep or let go
of the worker while it holds a finished part. `worker_when_blocked` chooses, with the same meaning
in both block styles:

| Value | PROCESS (or MOTION) with `worker_fleet` | SEIZE ... RELEASE naming the worker fleet |
|---|---|---|
| `leaves` (default) | the worker is free when the work ends; the machine holds the part | the RELEASE frees the worker at once and never blocks |
| `stays` | the worker is freed with the machine, when the part is accepted downstream | the RELEASE holds the worker until the part is accepted downstream |

Set it on the PROCESS, or on the RELEASE that names the worker fleet. Where nothing downstream is
finite the two values give the same run. A PROCESS with action "Seize Delay" keeps its worker
either way (a later RELEASE frees it), and a RELEASE of an ordinary resource always holds it
until the part is accepted. Only the last RELEASE before the finite space blocks: release the
machine first and the worker last to keep the worker with the part.

## Blocking or loss?

A finite queue can also turn work away instead of stopping the line. `balk_if` on a station with
a `queue_capacity` sees the places taken as `queue_length`, so

```
queue_capacity = 5, balk_if = "queue_length >= 5"
```

is the **loss** system -- arrivals that find the queue full leave by the block's second output --
while the same station with no `balk_if` is the **blocking** system. A part that reneges frees
its place at once, so either way the space is exact.

## Deadlock

A closed loop of finite buffers can genuinely deadlock: a rework path back into a buffer that is
full while the station feeding it is blocked. Validation warns (`W_BLOCKING_LOOP`) and names the
loop; if it really closes at run time the run stops with an error naming the chain, rather than
idling quietly to the horizon and reporting the KPIs of a calm system.

A part that is blocked is finished, so there is no remaining service to hand back and nothing may
preempt it: `preempt` on a resource a blocking station holds is refused (`E_PREEMPT_BLOCKING`).

## Worked example

the bundled model `line_with_buffers` -- four stations, three buffers
and a study that moves twelve places around without ever adding any.
