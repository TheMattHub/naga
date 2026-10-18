# Planning and scheduling

An order book (the bundled model `order_due_dates`) answers "what happens if
this shop runs". A planner asks three narrower questions instead: **what is the plan**, **is
there a better one**, and **how risky is it**. `naga plan`
answers all three from one model.

## What is the plan: deterministic mode

Set `simulation.deterministic: true` (or pass `--deterministic`/`deterministic=True` to the
notebook API) and every random draw becomes its own mean instead of a sample: a distribution
returns its closed-form mean, a DECIDE-by-chance branch splits by largest-remainder allocation
instead of a coin flip, and replications are forced to 1. One run is then a single, reproducible
timeline -- the plan -- rather than one sample among many.

This also turns on `schedule_log`: one row per resource hold (order, block, resource, start, end,
setup share), drawn as a **Gantt chart** by resource or by order in the HTML report's Schedule
section. `outputs.schedule: true` turns the same frame on
in an ordinary stochastic run, if you want the timeline without going deterministic.

Deterministic mode is a **mean-value model**, not a fluid limit: because of queueing and
branching, the mean of many random runs is not the same number as one run of the means. Use it to
see the plan's shape, and [plan risk](#how-risky-is-it-plan-risk) to see how much a real shop
would vary around it.

## Handing the plan to an MES

`plan_export(schedule_log, path)` (Python) or `naga plan model.json --export plan.csv` (CLI)
writes the schedule as a plain CSV or JSON by the path's extension -- Order, Operation, Resource,
Planned Start, Planned End, plus dated columns when the model has a calendar
([Dates and calendars](dates-and-calendars.md)). This is a one-way hand-off: nothing is read back
from an MES here.

## How risky is it: plan risk

`planning.plan_risk(layout, replications)` runs the SAME layout twice over: once deterministically
for the plan, then N times with the model's own randomness and queue discipline left alone. The
result, one row per order:

| Column | Meaning |
|---|---|
| `Planned End` | the deterministic plan's own completion time |
| `P(On Time)` | share of replications where the order finished by its due date (an order that never finished counts as late) |
| `Mean Lateness` / `P90 Lateness` | signed lateness (`end - due`) across the replications that finished |
| `Mean End` | mean completion time across the replications that finished |

`naga plan model.json --risk 200` prints this table.

## Is there a better one: the CP-SAT proposer

`cpsat.propose(layout, objective="makespan"|"weighted_tardiness")` reads the order book as a job
shop -- each order's routing sequence (as in the bundled model `job_shop_routed`) is a machine routing, its
processing time the deterministic mean of the station's PROCESS -- and solves it with
[OR-Tools](https://developers.google.com/optimization) CP-SAT. It needs the optional `ortools`
package (the `plan` extra: `pip install "nagasim[plan]"`); without it, `--propose` prints a clear
message and exits with status 2.

The result is an ordinary **scenario**: every order gains a `plan` (its planned start per
station) and, where the plan starts it later, a later `release`; every resource the plan uses
gets the new **`Plan`** queue discipline, which dispatches each station in the planned order. The
proposal is not applied automatically -- run it through `plan_risk` or the deterministic plan
again to see what it does, exactly as you would any other scenario.

Not every routing survives the trip through the solver. **Refused** features (ROUTE by
expression, BATCH/SEPARATE, conveyors, worker/vehicle fleets, capacity schedules and failures,
setups outside a single-unit resource) are listed on the result and left out of the CP-SAT model,
but the simulation still executes them exactly as written -- the solver proposes, the model still
decides what actually happens.

## The `plan` CLI

```console
naga plan order_due_dates
naga plan order_due_dates --risk 20 --export plan.csv --report plan.html
```

The other options are `--orders orders.csv`, `--propose makespan|weighted_tardiness`,
`--time-limit S` and `--workers W`.

`--orders` replaces the model's order book from a CSV. With no other flag it just prints the
as-is schedule; add `--propose` to solve first (the proposed layout, not the as-is one, is what
`--risk`/`--export`/`--report` then use), `--risk N` to score it, `--export` to write it, and
`--report` for an HTML page with the Gantt and the risk table together. `naga plan --help`
lists every flag with its default.

