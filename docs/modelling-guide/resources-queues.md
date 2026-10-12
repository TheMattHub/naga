# Resources, sets and queue disciplines

## Resources

A resource is a named capacity (e.g. a machine, a server, an agent pool) declared in the
**Resources** table. Every resource is always a priority resource: the queue discipline and an
optional ranking expression are properties of the resource itself, not a separate resource type.

| Discipline | Order |
|---|---|
| `FIFO` (default) | First seized, first granted, within the same priority. |
| `LIFO` | Last seized, first granted. |
| `LowestValueFirst` | Grants the request whose `ranking_expr` evaluates lowest first. |
| `HighestValueFirst` | Grants the request whose `ranking_expr` evaluates highest first. |
| `Plan` | Works each station's planned order: an entity queues once every entity planned earlier at its station has started, ranked by `entity.plan[station]`; unplanned entities come after planned ones, FIFO. Written by `cpsat.propose`. |

`ranking_expr` is a normal expression (see [Expressions and variables](expressions-variables.md)),
evaluated in the *requesting entity's* scope -- e.g. `entity.due_date` for earliest-due-date
scheduling.

## SEIZE / RELEASE / PROCESS

- **SEIZE** requests units of a resource (or set), waits, holds a delay, forwards; the resource
  stays held until a matching **RELEASE**.
- **RELEASE** releases everything the entity holds under a given resource name.
- **PROCESS** fuses seize-delay-release into one block (`action` selects which steps run) -- use it
  for a simple, self-contained service step. Use separate SEIZE/RELEASE only when something needs
  to happen *between* seizing and releasing (a routing decision, a second delay elsewhere) --
  `priority_rework.json` is a worked example.

Priority (`priority` param, lower draws first) and `quantity` (units requested, default 1) apply
to SEIZE, RELEASE and PROCESS alike.

## Resource sets

A named group of resources, seized as a pool: an entity waits for "any member with a free unit"
and is counted once on the **set's own** queue/wait collectors, never once per member. Declared in
the **Resource sets** table: `members` (resource names) and a selection `rule`.

| Rule | Picks |
|---|---|
| `PreferredOrder` | The first member (in declared order) with a free unit. |
| `Cyclical` | Round-robins across members. |
| `Random` | A uniformly random member with a free unit. |
| `SmallestNumberBusy` | The member with the fewest units currently in use. |
| `LargestRemainingCapacity` | The member with the most free capacity. |

A block's `resource_name` may name either a plain resource or a set -- they share one namespace.
`two_teller_set.json` is the minimal worked example.

## Ceiling: quantity > 1 on a set

Wait-for-any across a set's members works by requesting every member and cancelling the losers.
With `quantity > 1`, a losing member can briefly hold a partially granted subset of units before
being cancelled -- a known ceiling, not a correctness bug
for `quantity = 1`.

## Queue behaviours: balking, reneging, preemption

The two blocks that queue for a resource, SEIZE and PROCESS, can let an entity leave instead of
waiting, and PROCESS can let an urgent job take a resource off a running one.

| Param | On | Meaning |
|---|---|---|
| `balk_if` | SEIZE, PROCESS | Evaluated on ARRIVAL, before joining the queue; true = leave. `queue_length` (the queue it would have joined) is in scope. |
| `renege_after` | SEIZE, PROCESS | How long the entity is willing to wait, sampled when it joins (e.g. `EXPO(10)`, `entity.patience`). |
| `renege_time_unit` | SEIZE, PROCESS | Unit `renege_after` is written in; default the model's base unit. |
| `preempt` | PROCESS | When no unit is free, interrupt the lowest-priority job in service on that resource. |
| `on_preempt` | PROCESS | What THIS block's job does when it is interrupted: `resume` (default) or `restart`. |

A block with `balk_if` or `renege_after` needs a **second outgoing link**: branch 0 is the normal
way out, branch 1 is "left without service" (branch index is link order, exactly as for DECIDE).
The entity carries `left_reason`, `"balk"` or `"renege"`.

Two rules that make results reproducible:

* **A grant in the same instant beats the patience.** If a unit is released at exactly the moment
  a patience runs out, the entity is served.
* **Preemption is narrow.** Only a PROCESS in the `Seize Delay Release` action, on a plain
  resource (not a set), can preempt or be preempted -- that is the only block that owns a whole
  service and can therefore give the interrupted job its remaining time back. Anything else is a
  validation error. Capacity schedules and failures still never interrupt a job in service: they
  wait for a free unit (the WAIT rule).
* **A changeover follows the same rule.** A setup interrupted half-way is *resumed* under
  `on_preempt: resume` -- the job keeps the minutes already spent and finishes only what is left,
  and the changeover is counted and costed once. Under `restart` the whole changeover is paid,
  counted and costed again, which is what a station taken over mid-setup really does. Either way,
  the station is left set up for what it was set up for before, so whatever takes the unit over
  changes over from where the station actually is.

Counts land in the `queue_summary` result frame, one row per block: Balked, Reneged, Preempted
and the wait before reneging. `impatient_customers.json` is the worked example.

## HOLD and SIGNAL

HOLD gates an entity until one of three things happens (`mode`):

| Mode | Releases when |
|---|---|
| `Signal` | A SIGNAL block with the matching `signal_name` succeeds (broadcasts to every waiter at once, edge-triggered, auto-reset). |
| `Condition` | Every expression in `conditions` is true; re-checked the instant a variable, resource, store or level the conditions name is written, and every `scan_interval` for what has no event (TNOW, TS, random draws, level drift). |
| `Infinite` | Never -- the entity stays held for the rest of the run, by design (not a leak). |

`hold_signal_flow.json` is the worked example for both HOLD and SIGNAL;
`hold_on_store.json` is the one for a condition that reads a store (`NS`).
