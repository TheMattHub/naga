# Costing

Cost KPIs are computed at report time from the collectors every run already keeps -- costing adds
no new simulation logic, only new columns on existing result frames. `job_shop_jackson.json` is a
worked example (costed resources, a WIP holding cost, and scenarios that change cost).

## Resource cost

Set on a resource, in the model's `currency` (`simulation.currency`, default "EUR", label only --
never used in arithmetic):

| Field | Meaning |
|---|---|
| `cost_busy` | Rate charged while the resource is busy, per `cost_time_unit`. |
| `cost_idle` | Rate charged while the resource is scheduled but idle. Off-shift and failed time cost nothing (they are outside the *scheduled* window). |
| `cost_per_use` | Flat charge per seize. |
| `cost_time_unit` | Unit `cost_busy`/`cost_idle` are expressed per (default "hours"); converted against the model's base time unit. |

Busy/idle/usage cost all come straight from the resource's existing busy-time and seize-count
collectors -- see `resource_summary`'s `Busy Cost`/`Idle Cost`/`Usage Cost`/`Total Cost` columns in
the [KPI reference](../reference/kpis.md).

## Entity cost

| Source | When |
|---|---|
| Usage share | At SEIZE/PROCESS, a share of the resource's per-use cost. |
| Holding cost | At DISPOSE, `holding_cost x (time in system since warm-up, whichever is later)`, from the entity type's declared `holding_cost` per model base time unit. |
| Model-assigned | Anything a model itself adds, e.g. `entity.cost = entity.cost + x` in an ASSIGN block. |

A currently-live entity's cost is *projected* at report time (busy/holding cost it would have
accrued up to now); a completed entity's cost is exact, streamed at DISPOSE.

BATCH sums its members' cost into the batch entity; SEPARATE splits it back evenly across the
children when the batch is split. A member currently absorbed into a batch entity is excluded from
every cost sum (its cost lives in the batch entity, not double-counted).

## Lump-sum and fixed costs

Everything above accrues as a **rate**. Three real costs are not rates, and each one has its own
mechanism. All three join `Total Cost` and are itemised in the `cost_summary` frame
(`Run`, `Item`, `Category`, `Kind`, `Amount`), one row each.

### A cost per event: `RECORD` with `kind="cost"`

An ordering cost every time a replenishment is placed, a penalty per lost customer. Put a RECORD
on the path the event takes and set:

| Field | Meaning |
|---|---|
| `kind` | `"cost"` -- the one RECORD kind that builds no collector. |
| `stat_name` | The cost item's name; its KPI key is `cost/<name>/Amount`. |
| `expr` | The **money amount** added each time an entity passes: a constant, or an expression such as `entity.scm_qty * penalty`. |
| `category` | Free-text grouping shown in `cost_summary` (ordering, penalty, capital). |

An event cost is a statistic, so amounts charged **before the warm-up reset are discarded** with
every other statistic.

### A cost per changeover: PROCESS's `setup_cost`

A changeover is **not** a job, so a RECORD cannot price it: a RECORD sits on the path entities
take and would charge every job that passes, whether or not the station actually changed over.
PROCESS has its own field instead, beside the changeover TIME:

| Field | Meaning |
|---|---|
| `setup_cost` | Money charged **once per actual changeover**, e.g. `TABLE("Changeover cost", previous_key, key)`. `previous_key` and `key` are in scope here, exactly as in `setup`. |
| `setup_cost_category` | The grouping in `cost_summary`; defaults to `Changeover`. |

The item is named `Changeover <block>`, so two stations never share one line, and its KPI key is
`cost/Changeover <block>/Amount`. It joins `Total Cost` and resets at warm-up like any other event
cost. A changeover that takes **no time** but still costs scrap and a technician is charged all
the same: the event is the changeover, not the delay it happens to need.

The count comes with it. `Changeovers` and `Setup Time` appear per block in the `queue_summary`
frame (`queue/<block>/Changeovers`), beside balking, reneging and preemption -- the other things
that happen to a block at its resource. A model with none of them still gets no rows there.

Manufacturing's **Workstation** asset exposes both as `Changeover time` and `Changeover cost`.

### A fixed cost of the scenario: the `fixed_costs` section

A capex, or an annuity over a sized parameter, so an optimisation can trade capital against
operating cost:

```json
"fixed_costs": [
  {"name": "Battery capital", "category": "Capital",
   "amount": "battery_kwh * 450 * 0.09 * HORIZON / 8760"}
]
```

`amount` is a number or an expression over the model's variables and data, **evaluated once at run
start**. It is **never time-prorated by the engine**: a fixed cost is the cost of having built the
thing, and it counts in full however long the run is. When you want a share of an annuity, write
that share yourself -- `HORIZON` is the run length that was asked for, in the model's base time
unit, so `amount * HORIZON / 8760` on an hourly model charges the run's share of a year and keeps
capex and the operating cost on the same window.

Inside a submodel definition a fixed cost is named under `scoped.fixed_costs`, exactly like a
flow: each instance gets its own copy, priced on its own parameters.

### A peak charge: a flow's `peak_price`

A demand charge is peak imported power times a tariff. A flow already tracks the highest rate it
actually *held* (`Peak Rate`); `peak_price` is a plain number, money per unit of that peak, billed
once at the end of the run. A tariff that varies over time is a flow `price`, not a peak price.

## Energy

A resource or fleet takes `power`, kW **per unit**, one number for each state it can be in:

```json
{"name": "Press", "capacity": 2, "power": {"busy": 5.0, "idle": 1.0, "down": 0.3}}
```

`busy`, `idle`, `down` and `off_shift` default to 0kW; `blocked` defaults to `busy` (a blocked unit
is still seized, holding a finished part). kWh per state comes straight from the busy/available/down
time the resource already tracks -- no new collector. `simulation.energy_price` (money per kWh,
evaluated once at run start) prices the total as cost kind `"energy"`, joining `Total Cost` and
`cost_summary` like any other cost. The `energy_summary` frame gives kWh per state, total kWh,
average kW and energy cost, one row per resource (fleets included); it resets at warm-up like any
other statistic and stays empty for a model with no `power` declared.

## Material flow and bottlenecks

Every link a model draws is also counted: the `material_flow_summary` frame (From, To, Entities) is
what `svgcharts.sankey` draws as the flow diagram in the report, showing
where volume actually goes without reading a single number.

Bottleneck analysis is opt-in -- set top-level `"outputs": {"bottleneck": true}` -- because it
tracks every resource's active/inactive intervals over the whole run (memory proportional to event
count, not something to pay for by default). A resource is **active** while working, in a
changeover or under repair, and **inactive** while idle, blocked or off shift; at each instant the
resource with the longest current active period is the bottleneck. Roser's method then splits
credit: **Sole** when it is the only one in its active period overlapping the next bottleneck's,
**Shifting** when two periods overlap. `bottleneck_summary` reports Sole %, Shifting % and Total %
per resource; the existing `Bottleneck Score` (queue-based) is unchanged and needs no opt-in.

## System-level cost

`System > Total Cost` = total resource cost + total entity holding cost + the model-assigned-only
portion of entity cost (never counts an entity's usage/holding share a second time on top of the
resource side) + total flow cost + every `cost_summary` amount. `Cost per Completed Entity`
divides that by completed entities.

## Where it shows up

- `resource_summary` and `entity_summary`/`entity_log` cost columns.
- `cost_summary`: the fixed, event and peak items, one row each, EMPTY for a model that declares
  none -- so an existing model's frames are unchanged.
- The report's Costs section and stacked bar chart, shown only when any cost is non-zero, with a
  "Fixed and lump-sum costs" table when there are any.
- Scenario comparison and optimisation address a cost item as `cost/<item>/Amount`, like any other
  KPI key.
