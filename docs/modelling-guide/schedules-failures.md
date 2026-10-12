# Schedules, failures and the WAIT rule

## Schedules

A `Schedule` is piecewise-constant: `points = [[duration, value], ...]`, each value holding for
`duration` time units, optionally `repeat`ing forever (without `repeat`, the last value holds
forever once the points run out). Declared in `schedules`, one of two kinds:

| Kind | Values mean | Consumed by |
|---|---|---|
| `arrival` | Arrival rate per time unit | A CREATE block with `arrival_mode = "Schedule"` and matching `schedule_name` -- drawn as a non-homogeneous Poisson process via thinning (Lewis-Shedler): candidates come from an exponential at the schedule's *maximum* rate and are kept with probability `rate(t) / max_rate`. |
| `capacity` | Integer unit count | A resource's `capacity_schedule` field. |

`call_centre_day.json` uses both kinds together (an arrival schedule for call volume, a capacity
schedule for agent headcount).

### Weekday schedules

When the model has a calendar, either kind can be written as a **weekday grid** in local
wall-clock time -- `{"mon": [["06:00", "14:00", 3]]}` -- with dated `exceptions` for holidays and
shutdowns, instead of `points`. It compiles to exactly the points above, once per run. See
[Dates and calendars](dates-and-calendars.md).

## Failures

A failure names one or more resources and an uptime/downtime distribution pair (clock time, not
busy time). The controller fails a resource every `uptime`, taking whatever units the capacity
schedule currently leaves available, holds for `downtime`, then restores them.
`machine_with_breakdowns.json` is the worked example.

There is no per-unit arbitration between a capacity schedule and a failure competing for the same
units at the same time (a known ceiling).

## The WAIT rule

Capacity schedules and failures never change a resource's *live* SimPy capacity -- doing so would
strand already-queued requests. Instead, the resource is always built at its **maximum** capacity,
and a controller process holds invisible, top-priority "blocker" requests to take units out of
service. A blocker outranks every entity request but still waits for a free unit, which is exactly
Arena's WAIT rule: **a job already in service always finishes** before its resource goes off shift
or fails.

Blockers are invisible to every statistic -- `NQ`/`NR`/`MR` and set selection see only the *net*
values (units not currently blocked). Utilization (`Usage % (snapshot)`) divides busy time by the
*scheduled* (available) capacity, so time a resource spends off-shift or down never dilutes it; use
`Scheduled Capacity Avg` and `Down %` to see the schedule/failure effect directly.

## Run length

A model with an unbounded CREATE (`max_arrivals = 0`), any failure, or a repeating capacity
schedule never drains on its own -- it needs an explicit run length, or `naga run` /
`nagasim.api.run_model` raises a clear `ModelValidationError` instead of hanging.
