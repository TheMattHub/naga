# Coming from Arena

The block vocabulary here is close to Arena's flowchart modules, and the expression functions
keep the SIMAN names. The difference is the medium: a model is a JSON file you run from a
command line or from Python. This repository has no editor and no animation.

## Modules and blocks

| Arena | Here |
|---|---|
| Create, Dispose | CREATE, DISPOSE |
| Process (Seize Delay Release, Delay, ...) | PROCESS with the same `action` values |
| Seize, Delay, Release | SEIZE, DELAY, RELEASE |
| Decide (by chance, by condition, N-way) | DECIDE, modes "Chance" and "Condition" |
| Assign | ASSIGN; attributes are `entity.<name>`, variables are `variables` |
| Record, tallies and counters | RECORD; its statistics are in the `user_summary` frame |
| Batch, Separate | BATCH, SEPARATE; DUPLICATE for copies |
| Hold (wait for signal, scan for condition, infinite) | HOLD, modes "Signal", "Condition", "Infinite" |
| Signal | SIGNAL |
| Match | MATCH |
| Station, Route, Sequence | STATION, ROUTE and `sequences` |
| Transporter, Request, Transport, Free | TRANSPORT with `fleets` on a path `network` |
| Conveyor, Access, Convey, Exit | CONVEYOR |
| Resource, Set, Schedule, Failure | `resources`, `resource_sets`, `schedules`, `failures` |
| Submodel | SUBMODEL, with parameters |
| Run Setup: replications, warm-up, length | `simulation.replications`, `warmup`, `time_limit` |
| Input Analyzer | `naga fit` |
| Process Analyzer | `naga scenarios` (common random numbers, paired comparison) |
| OptQuest | `naga optimize` (a pattern search, or Optuna when installed), `naga doe` |

All block parameters are in the [block reference](../reference/blocks.md).

## Expressions

`TNOW`, `NQ(name)`, `NR(name)`, `MR(name)`, `EXPO(mean)`, `UNIF(low, high)`,
`NORM(mean, sd)`, `TRIA(low, mode, high)` and `DISC(...)` exist under those names
([expression reference](../reference/expressions.md)). Two things differ:

- `DISC` takes value and probability pairs whose probabilities sum to 1, not cumulative
  probabilities.
- Queues are named by the resource or the store they wait for (`NQ("Server")`), not by a queue
  of their own.

## Schedules and failures

A capacity schedule or a failure never interrupts a job in service: the unit goes off when the
job ends. That is Arena's Wait rule. The Ignore and Preempt schedule rules are not offered;
preemption exists between jobs, by priority, on a PROCESS block.

## What is missing here

- An editor, animation and a run controller with a picture. `naga run --player` writes an HTML
  file that replays one replication; nothing more.
- A reader for `.doe` files. A model is rebuilt, by hand or through the agent tools.
- VBA and user-coded SIMAN. The nearest thing is a LOGIC block, a Python class you write.
- OptQuest's search methods. `naga optimize` is a simpler search with a replication budget.

## What you gain

Model files you can diff and merge (`naga diff`, `naga merge`), assertions that run in CI
(`naga check`), stocks and flows and agents in the same model, and a model an LLM agent can
build and run through [tools](../mcp.md).

A good first file to read is `nagasim/models/job_shop_routed.json`: stations, routes and
sequences ([gallery](../gallery/manufacturing.md)).
