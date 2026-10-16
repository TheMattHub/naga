# Model file format

A model is one JSON file with a `schema_version`. `ModelDocument.to_layout()` writes it, `GraphModel.
load_from_json` reads it for the engine after migrating (older files) and validating. Block
`position` is always the block **centre**, in scene units. See the generated
[schema reference](reference/schema.md) for the top-level key list, derived
live from `core/schema/`; this page is the field-by-field guide.

## Completion in your editor

The JSON Schema of each schema version is published at
`https://thematthub.github.io/naga/schema/model-v<N>.json` (`naga schema` prints the current
one). Name it in the file's first key and VS Code, PyCharm and most editors complete keys,
block types and parameters, show each parameter's description, and flag a wrong key as you
type:

```json
{"$schema": "https://thematthub.github.io/naga/schema/model-v29.json", "schema_version": 29}
```

The engine ignores `"$schema"`.

## The short form: `naga fmt`

```console
naga fmt model.json
naga fmt --check model.json
```

`naga fmt` rewrites a file in its shortest form, with `"$schema"` first: every value the
engine fills in anyway is left out (a parameter at its default, a description that is the
block's name, a unit that is the model's, an empty section), and keys come in one order. The
model is the same before and after: it runs the same, validates the same, and has the same
hash for a saved state. Layout (positions, annotations) stays. `--check` changes nothing and
exits 1 when a file is not in that form, for CI. The desktop editor saves the long
form, so `--check` flags a file saved from it until you run `naga fmt` on it. A parameter
you leave out of a file you write by hand means its default in the
[block reference](reference/blocks.md).

## Top-level shape

```jsonc
{
  "$schema": "https://thematthub.github.io/naga/schema/model-v29.json",
  "schema_version": 29,
  "blocks": [ ... ], "links": [ ... ],
  "resources": [ ... ], "resource_sets": [ ... ],
  "entities": [ ... ], "variables": [ ... ], "tables": [ ... ], "sequences": [ ... ], "orders": [ ... ],
  "timeseries_inputs": [ ... ], "levels": [ ... ], "flows": [ ... ],
  "conversions": [ ... ], "fixed_costs": [ ... ],
  "schedules": [ ... ], "failures": [ ... ],
  "scenarios": [ ... ], "submodels": { ... },
  "annotations": [ ... ], "background": null, "info": {},
  "data_sources": [ ... ], "data_bindings": [ ... ], "initial_state": { ... },
  "simulation": { ... }
}
```

## blocks

```jsonc
{"name": "cutting", "description": "cutting", "type": "PROCESS", "position": [200, 320],
 "params": {"action": "Seize Delay Release", "resource_name": "Cutter",
            "distribution": "Exponential", "dist_params": {"rate": 0.5}, "time_unit": "base",
            "priority": 0, "quantity": 1}}
```

`params` is validated against that block type's registered schema -- see the generated
[block reference](reference/blocks.md) for every type's exact parameters, defaults and
descriptions.

## links

```jsonc
{"source": "create_1", "target": "cutting", "delay": null,
 "source_port": null, "target_port": null}
```

Array order **is** branch order (there is no port model beyond submodel instances). `delay` is an
optional duration spec (distribution + dist_params + time_unit); `source_port`/`target_port` are
read only when the matching endpoint is a SUBMODEL instance.

Several links may join the **same pair of blocks** -- a DECIDE whose branches 0 and 2 both end at
the same DISPOSE, a SEIZE whose "left without service" exit meets its normal one, two exits of one
asset returning to the same block. They are separate entries in the array, each with its own
`delay` and ports, and a link is identified by its source, its target **and** which of the
parallel links between that pair it is (0 first, in array order). No extra field: nothing changes
for a file with one link per pair. A scenario override addresses the second one as
`links.a->b#1.delay...`.

## resources / resource_sets

```jsonc
{"name": "Cutter", "capacity": 1, "discipline": "FIFO", "ranking_expr": "",
 "capacity_schedule": null,
 "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0, "cost_time_unit": "hours"}
{"name": "Drilling", "members": ["Drill1", "Drill2"], "rule": "PreferredOrder"}
```

## entities

```jsonc
{"name": "A", "shape": "square", "color": "BLUE", "holding_cost": 0.0}
```

## variables

```jsonc
{"name": "rework_count", "initial": 0}
```

## tables

```jsonc
{"name": "ProcessTime",
 "keys": ["WIDGET", "GADGET"], "columns": ["Cut", "Pack"], "values": [[3, 2], [4, 6]],
 "key_mode": "exact", "column_mode": "exact", "default": null}
```

A lookup table read by `TABLE("ProcessTime", entity.e_type, "Cut")` in any expression. `keys` are
the row keys and `columns` the column keys; drop `columns` and `values` becomes a flat list, one
number per key (a 1-D table). Each dimension's mode is `exact` (the key must be present), `step`
(the largest numeric key <= x) or `linear` (interpolate); the last two need numeric keys, clamp at
both ends, and are sorted on load. With `exact`, a missing key falls back to `default` when the
table has one and stops the run naming table and key when it does not. See
`nagasim/models/table_process_times.json`.

## sequences

```jsonc
{"name": "Shaft",
 "steps": [{"station": "Cutting",  "values": {"operation": "rough_cut", "process_time": 0.7}},
           {"station": "Drilling", "values": {"process_time": 1.6}},
           {"station": "Cutting",  "values": {"operation": "finish_cut", "process_time": 0.4}}]}
```

A routing: the ordered operations a product follows through shared stations. `station` names a
STATION block -- a station's name **is** its block's name, so a station inside a submodel instance
is the flattened `instance/station` path. A step's `values` are assigned onto the entity as
attributes when a ROUTE block sends it to that step, which is how a per-operation process time
travels with the job.

An entity walks a sequence through its own `sequence` (the name) and `step` (the next index)
attributes: CREATE's `sequence` parameter sets both, and assigning `entity.sequence` in an ASSIGN
resets `step` to 0. A ROUTE whose entity has finished its sequence forwards along the block's own
outgoing link; one whose entity has no sequence at all stops the run. See
`nagasim/models/job_shop_routed.json`.

## orders

```jsonc
{"id": "WO-1001", "release": 0, "due": 480, "quantity": 2,
 "entity_type": "WIDGET", "sequence": "Shaft", "priority": 1,
 "source": "release_orders", "attributes": {"customer": "ACME"}}
```

A released job. `release` and `due` are in the model's **base time unit**, and a due date is an
ABSOLUTE time, not a lead time. A CREATE block whose `arrival_mode` is `"Orders"` releases each
order it owns at that order's release time, creating `quantity` entities that carry `order_id`,
`due_date`, the order's `entity_type` (overriding the block's own), its routing and its
`attributes`; the block's distribution, `max_arrivals` and `entities_per_arrival` are ignored in
that mode. With more than one Orders-mode CREATE, `source` names the block that releases an order;
without one it goes to the first.

The run reports per-order lateness and tardiness in the `order_summary` frame and a model-wide
service level in `due_date_summary`. Neither needs orders: any entity given a `due_date` by an
ASSIGN counts. See `nagasim/models/order_due_dates.json`.

## levels / flows / conversions

Continuous quantities, the rate-based transfers between them, and delayed converters with a yield
curve. The [schema reference](reference/schema.md) has the field list.

## schedules

```jsonc
{"name": "day", "kind": "arrival", "repeat": true, "time_unit": "base", "points": [[60, 2], [60, 8]]}
```

`kind` is `"arrival"` (values are rates) or `"capacity"` (values are unit counts).

## failures

```jsonc
{"name": "Wear", "resources": ["Cutter"],
 "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.001}},
 "downtime": {"distribution": "Constant", "dist_params": {"value": 30}}}
```

## scenarios

```jsonc
{"name": "Extra Assembly server",
 "overrides": [{"path": "resources.Assembly.capacity", "value": 3}]}
```

## submodels

```jsonc
"submodels": {
  "Cell": {
    "info": {"description": "..."},
    "parameters": [{"name": "rate", "default": 1.0, "description": "..."}],
    "blocks": [ ... ], "links": [ ... ], "annotations": [ ... ]
  }
}
```

## annotations / background / info

`annotations` are text notes and frames drawn behind blocks; `background` is an optional floor
plan image (path, position, scale, opacity, lock, real-world scale from two points); `info` is
free-text model metadata shown in the report header. None of the
three are read by the engine.

## data_sources / data_bindings

```jsonc
"data_sources": [{"name": "visits", "kind": "csv", "location": "data/clinic_visits.csv"}],
"data_bindings": [{"name": "service", "source": "visits", "column": "service_minutes",
                   "transform": "fit", "target": "blocks.Consult.params.distribution",
                   "enabled": true}]
```

The `orders` transform is the one that replaces a whole section rather than a value: it targets
`orders` and reads one order per source row, with an optional `map` of order field to source
column (`{"id": "WO_NUMBER"}`) for a work-order export whose columns are named differently.

Never resolved by simply loading the file -- see [Digital twin](digital-twin.md) for the transform
table and `naga bind`. A `sql`/`json_url` source needs explicit network consent from the caller,
never from the file itself; see [Security](security.md).

## initial_state

```jsonc
"initial_state": {"variables": {}, "entities": [{"block": "Consult", "count": 2,
                  "e_type": "PATIENT", "in_service": 1, "remaining": [6.0]}],
                  "resources": {"Scanner": {"down_until": 30.0}}}
```

A warm start: entities injected at t=0 before any CREATE fires. See
[Digital twin](digital-twin.md) for the rules. A state taken at a time t also carries `clock`
(where the run's clock starts), `arrivals` (per CREATE: `count`, `next`), per resource `failures`
(`next` or `down_until`) and `busy`, and per entity entry `ends`, `since` and `arrives`, all
absolute times; see "A state as a file" there.

## simulation

```jsonc
{"time_limit": 10000, "warmup": 2000, "replications": 20, "time_unit": "minutes",
 "currency": "EUR", "entity_log_limit": 100000, "record_logs": true,
 "calendar": {"start": "2026-01-05T08:00:00", "timezone": "Europe/Rome"}}
```

`time_limit` of `0`/`null` means "run until the queue drains"; a model that cannot drain on its
own (an unbounded CREATE, any failure, a repeating capacity schedule) needs an explicit one, or
`naga run` raises a clear error rather than hanging.

`calendar` is **optional** and absent by default; with it, model time 0 becomes a real instant,
schedules may be written as weekday grids, time series and orders may carry ISO timestamps, and
every axis, playback clock and export shows wall-clock time. A `calendar.end` instant replaces
`time_limit` as the run horizon.

`record_logs: false` stops the per-event entity, resource and store **logs** being recorded at
all. Every summary statistic is unchanged -- they come from the run's own collectors, never from
the logs -- and the three flat log sheets come back with their columns and no rows. The exceptions
are the handful of columns that *are* the log: queue-length and store-size mode, percentiles and
standard deviation, which report as blank. Worth it on a long study: on the bundled warehouse it
is about a quarter of the run time and a third of the memory at its own run length, and three
quarters of the memory over a very long one. `naga run --no-logs` sets it for one run.
Studies (`optimize`, `doe`, selection, sensitivity, calibration) and `forecast` run with the logs off by default, **unless** a KPI the study names is computed from them (an objective, constraint, response or calibration target such as `resource/<name>/Queue Len P95`), in which case they stay on, even for a model that set `record_logs: false`. An explicit `record_logs` from the caller always wins.

## Migration

Every field above defaults sensibly on an older file, so opening a v4 file and saving it
immediately produces byte-identical KPIs to before.
