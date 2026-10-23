# Digital twin

A digital twin is a model that reads its parameters from real data, starts from the state the
real system is in now and forecasts from there; `naga twin` keeps one running beside a plant,
scores its forecasts against what then happened and recommends among actions you name.

A study hard-codes a parameter ("service time ~ Exponential(1/6)"). A twin reads it from real data
and starts from where the real system actually is right now. Three pieces build that on top of an
ordinary model: data sources and bindings, initial state (warm start), and calibration/forecast.
The worked example is `nagasim/models/clinic_twin.json`.

## Data sources and bindings

A **data source** names where data lives; a **binding** reads one column from it, optionally
filters and transforms it, and writes the result over a model parameter (the same override-path
grammar scenarios use).

```jsonc
"data_sources": [{"name": "visits", "kind": "csv", "location": "data/clinic_visits.csv"}],
"data_bindings": [{"name": "service", "source": "visits", "column": "service_minutes",
                   "filter": {"clinic": "north", "day >=": 20},
                   "transform": "fit", "target": "blocks.Consult.params.distribution"}]
```

| Transform | Value written |
|---|---|
| `mean` / `median` / `sum` / `count` / `last` | that statistic of the filtered column |
| `quantile:<q>` | the q-quantile, q in [0, 1] |
| `value` | the first cell -- a column that holds one setting |
| `rate_per:<unit>` | rows / observed span, as a rate per `<unit>` |
| `fit` | best-AIC distribution, writes both `distribution` and `dist_params` |
| `empirical` | piecewise-uniform empirical spec, same pair as `fit` |
| `arrival_schedule:<bin>[:<cycle>]` | schedule points, targets `schedules.<name>.points` |

Source kinds are `csv`/`xlsx`/`sqlite` (a file, path relative to the model file), `sql`/`json_url`
(a network address), or the live kinds `rest`/`mqtt`/`opcua`, which name a connection you define
yourself and never an address: see [Live data](live-data.md). Bindings never run on their own -- `naga bind` or
`api.run_model(bind_data=True)` resolves them explicitly:

```console
naga bind clinic_twin
```

A failed binding (missing file, bad query, empty column, wrong type) reports its own problem and
leaves the model's stored value in place, so one bad source never stops the rest of the model from
running. See [Security](security.md) for network consent -- a `sql`/`json_url` source needs it
explicitly, never from the model file itself.

## Initial state (warm start)

`initial_state` puts entities into the model at t=0, before any CREATE fires, so a run starts from
"the shop floor as it is now" instead of empty:

```jsonc
"initial_state": {
  "variables": {"shift": 2},
  "schedule_offset": 180.0,            // three hours into the shift calendar's cycle
  "entities": [{"block": "Consult", "count": 2, "e_type": "PATIENT", "in_service": 1,
                "remaining": [6.0]}],
  "resources": {"Scanner": {"down_until": 30.0}}
}
```

The eight rules, exactly as implemented:

1. `variables` overwrite the model's own initial values.
2. `entities` inject `count` entities into the named block, in order, as if they had just arrived.
3. `age` sets the entity's creation time in the past, so its reported time in system includes that
   history.
4. `in_service: k` (PROCESS/SEIZE/DELAY only) seizes the resource immediately for the first `k`
   entities, holding for `remaining[i]` if given -- exact for an exponential service time, an
   approximation otherwise unless `remaining` is given.
5. `resources: {"name": {"down_until": t}}` starts a resource failed until `t`, respecting the WAIT
   rule (a unit already serving an in-service entity is not taken).
6. `time` records the snapshot's own timestamp for the report; the run clock still starts at 0.
7. `schedule_offset` is how far into its own cycle the calendar already is at t=0, in base time
   units. Every schedule -- capacity and arrival -- and every repeating time-series input is then
   read at `t + schedule_offset`: warm-started three hours into an eight-hour shift, the model
   stops five hours in, not eight, and a day-long price profile continues mid-day instead of
   starting again at midnight. It changes no statistic; one offset serves the whole model, because
   a snapshot has one clock.
8. Warm-up and an initial state work against each other -- a warm-up would discard exactly the
   history the initial state supplies -- so `api.run_model` defaults the warm-up to 0 when an
   initial state is present.

## A state as a file

A state can also live outside the model, as a document with a content hash:

```console
naga state take model.json --at 480 --out state.json     # run to t = 480 and write the state
naga state check model.json state.json --max-age 300     # may this model start from it?
naga run model.json --state state.json                   # start from it
```

- **The clock.** A state taken at t carries `"clock": t`: the run starts at that model time, so
  shifts, dated series and order releases need no shifting, and its statistics cover the run from
  there, as after a warm-up of that length. The system results say `Started From State At`, so
  nobody reads such a mean as a full run's.
- **What a state holds.** Variables, levels and stocks, the entities waiting and in service
  (type, attributes, age, when each service ends, since when each waits; a queue keeps the order
  of its discipline), entities on a link (one put on a link by a state appears in the views when
  it arrives),
  each CREATE's count and next arrival, each failure's next time or repair time. Also: the
  entities waiting in a HOLD, in a BATCH that is forming and in a MATCH, in their order; the
  time each waiting entity with patience gives up (`deadline`); which member of a resource set
  serves each job, and whose turn it is in a Cyclical set; what each station with a changeover
  is set up for.

  | Kind | In a state |
  |---|---|
  | Variables, levels, SD stocks and staged delays | carried |
  | Entities in service or waiting for a resource (FIFO, LIFO, ranked), on a link | carried |
  | Entities waiting in a HOLD (signal, or a condition on events), a BATCH in formation, a MATCH with a `type_store_map` | carried |
  | A queue with patience | carried (`deadline`) |
  | Resource sets (member per job, a Cyclical set's turn) | carried |
  | Arrivals, failures, schedules and calendars | carried |
  | What a station is set up for | carried; a changeover under way is refused |
  | A batch whose maximum wait is running | carried (`blocks.<batch>.due`) |
  | A HOLD on a condition that is scanned on a clock; a MATCH with no `type_store_map` | refused |
  | A job holding a resource it seized in an earlier block (a SEIZE ... RELEASE, a belt's end stop carried to a "Delay Release") | carried (`holds`) |
  | A formed batch (of a BATCH or a MATCH), wherever it is: the batch entity with its members inside, each with its age, attributes and holds | carried (`members`) |
  | An entity at a TRANSFER waiting for its level (for room or for stock), in the level's queue order | carried (`transfer`) |
  | A MOTION job mid-cycle: the segment in progress and when it ends, each segment's time (a move after the clock is timed when it starts); the cycle counts whole. From a plant, a MOTION job resumes at the segment the plant's `step` signal reports, and without one runs a whole cycle drawn afresh | carried (`cycle`) |
  | A preempted job; a batch inside a batch; in a state from a plant, a formed batch whose members no `batch_field` names, or an entity waiting at a TRANSFER whose amount is neither a number nor an observed attribute | refused |
  | A ranked queue whose `ranking_expr` reads more than the entity (a variable, the clock) and does not `rerank` | refused |
  | Blocking after service and finite buffers (a BUFFER, a `queue_capacity`): the parts in a buffer, the machine holding its finished part, who waits for a place | carried (`held_by`, `place`) |
  | Blocking before service (`hold_until_space`); a finite queue that balks or reneges while an entity waits for a place | refused |
  | Fleets and vehicles: where each one is, its drive, loading or unloading under way, its load, the dispatch queue in its order, a return home | carried (`fleets`, `trip`) |
  | A battery's charge; a vehicle driving to a charger, waiting for a point or charging | carried (`soc`, `charge`) |
  | A mobile cobot tending a station: a finished part waiting for a cobot, held by its machine, the cobot driving to pick it or picking it; a part carried to the station, or placed (the machine holds it and starts on it when the place ends); each pick's and place's time; the tending rows earned before | carried (`trip.cycle`, `held_by`, `tended`) |
  | A tour under way (`loads` above 1); a vehicle called back from its way home, or out of service; segment capacities; a gantry; a worker fleet; a cobot waiting loaded for its machine, placing into a buffer or a machine with a finite queue, or staying with its part; a cobot fleet that exchanges parts; a swap station; scheduled charging | refused |
  | An accumulating conveyor: each part waiting for a cell or its entry slot, riding, packed at the end, moving up, at the end stop, or handed to the next belt; the next entry slot | carried (`belt`, `held_by`, `blocks.<belt>.next_entry`) |
  | A non-accumulating conveyor with parts on it; an end stop that other jobs queue for | refused |
  | Agent populations | refused |

- **What it reproduces.** A run started from a state taken at t gives the results of the
  uninterrupted run with a warm-up of t: exactly when nothing random is drawn after t, and in
  distribution otherwise. Random streams are not part of a state, and entity ids are the new
  run's. Not exact even then: the queue-length mode, percentile and standard deviation columns
  (they follow the order of events inside one instant) and the per-type cost of the entities that were inside at t. A CREATE driven by
  an arrival schedule restarts its random thinning at the clock, so it matches in distribution
  only. Time-series samples are taken on a grid that starts at the clock: their instants are the
  uninterrupted run's only when the clock is on that grid.
- **What it refuses.** `naga state take` writes a whole state or none. Agent populations,
  what the table above lists as refused, a changeover under way, a
  store's content that is not entities waiting at a HOLD, a BATCH or a MATCH, a pending
  decision and a pipeline delay are refused with `E_STATE_UNSUPPORTED`, which names the one
  found. So is a time at which a model event is due (a service ending, an arrival, a failure, a
  shift change: take the state between two events) and a time at or after the end of the run.
- **From a plant.** A data binding may target `initial_state.variables.<name>`,
  `initial_state.levels.<name>`, `initial_state.resources.<name>.down_until` or `.busy`, and
  `initial_state.entities.<block>.count` or `.in_service`; with the `last` transform it writes the
  latest value a source holds when a run binds its data. An observation the model cannot hold is refused, never repaired: more in service than the
  resource has units (`E_INITIAL_IN_SERVICE`), more waiting than the queue holds
  (`E_INITIAL_QUEUE_CAPACITY`), a resource reported busy with no entity in service
  (`E_INITIAL_BUSY`), a state older than `--max-age` (`E_STATE_STALE`). What no binding reports
  starts as the model declares it, and the command says so (`W_STATE_NOT_COVERED`).
- **Identity.** The document names the model it was taken for by a hash of the model file (all of
  it but `initial_state`); a state for another model, or one edited by hand, is refused
  (`E_STATE_MODEL`, `E_STATE_HASH`). A state holds at most 16 MiB and 100 000 entities.

## Calibration

Tune parameters until simulated KPIs match observed ones, reusing the same search as
`naga optimize`:

```console
naga calibrate single_server --factor "blocks.create_1.params.dist_params.rate=continuous:0.05:1.2" --target "system/Avg Throughput Time=2.0:0.5" --budget 60 --replications 3 --run-length 1000
```

A target's error is relative (`(simulated - observed) / observed`, squared) so a percentage KPI and
a minutes KPI count equally; a `--target` may instead name an `observed_sample` to match by the
Kolmogorov-Smirnov statistic against a whole distribution, not just a mean. The search minimises
the objective's mean over replications, which prefers a stable setting over one that only hits the
mean by chance. A target with a tolerance passes when `|simulated mean - observed| <= tolerance`;
the confidence interval is reported alongside so a borderline pass is visible as one.
To fit a whole trajectory (a level, an auxiliary, an agent state count) rather than one number,
use a `SeriesTarget` from Python; see
[System dynamics: calibration to data](system-dynamics.md#calibration-to-data).

## Forecast

A short-horizon, no-warm-up run reporting a **prediction band**, not a confidence interval:

```console
naga forecast clinic_twin --horizon 120 --replications 5
```

The band at each sample time is the empirical 5th-95th percentile across replications -- the range
a single future day is likely to fall in, which does not shrink as replications grow (a confidence
interval of the mean does, and answers a different question). `--event
'timeseries/system/WIP/max>8'` reports the probability of a threshold event with a Wilson interval,
which behaves sensibly even when the true probability is near 0 or 1. Add `--report out.html` for a
fan chart, or `--out fc.xlsx` for the tables.

## An operational twin: `naga twin`

`naga twin` keeps the pieces above running against a plant: it watches the plant through a
[plant mapping](plant-timeline.md), starts a forecast from the plant's state of now, sets each
forecast beside what then happened, and writes one result file that a monitor or
an agent reads. It is read-only: nothing is sent to a plant, and only the result directory is
written.

You can try it with no plant. A run of the model itself stands in for one:

```console
naga plant init line.json --out line.plantmap.json
naga plant mock line.json --map line.plantmap.json --out plant.jsonl
naga twin once line.json --map line.plantmap.json --log plant.jsonl --out twin_out \
     --horizon 60 --replications 20
naga twin status twin_out
```

That is one cycle, over the whole log. `naga twin run` keeps going until Ctrl+C:

<!-- no-run: runs until Ctrl+C -->
```console
naga twin run line.json --map line.plantmap.json --log plant.jsonl --out twin_out \
     --horizon 60 --replications 20 --every 2s --forecast-every 30s --speed 60
```

`--log` plays the recorded log at `--speed` seconds of the log per second. Against a real plant,
replace it with `--connections FILE`, your [connections file](live-data.md): the model's live
sources are read over the connections it defines, and over nothing else. What has been run
against third-party servers, and what has not, is under
[Tested against](live-data.md#tested-against).

| Command | Does |
|---|---|
| `naga twin run` | monitors every `--every` (default 5 s) and forecasts every `--forecast-every` (default 5 min) until Ctrl+C; a forecast still running is not started again. With `--workers` above 1, a forecast running at Ctrl+C finishes the replications it has started before the command exits, and says so |
| `naga twin once` | one cycle with its forecast, for the OS scheduler; exit 1 when the result holds an error |
| `naga twin status DIR` | prints the latest result |

The result is `twin.json` in `--out` (replaced whole each cycle, so it can be read at any time)
and `twin_history.json` (the last 50 forecasts and the rolling score). The files are as
readable as the folder you give `--out`: they are written for other programs and other
accounts to read, so on a shared machine give the twin a folder only the right accounts can
open. `twin.json` holds:

- `written`, `started` (when this twin began) and `cadence` (`every` and `forecast_every`, in
  seconds): a result older than two cadences is a twin that stopped;
- `model`: its name and hash, `time_unit` and `epoch` (the instant model time 0 stands for), so
  `at` and every predicted time read as an instant;
- `sources`: each source's state, the age of its last word, events lost and gaps;
- `view`: items in each block, units in service, each resource's state, levels, at model time
  `at`. `approximate` is true when the plant names no items: counts of items are then
  estimates. `counters` is what the plant itself counted as having left each block that has a
  counter signal, since the twin first saw it: observed, not estimated;
- `forecast`: the latest forecast with what it was computed from (the state's hash, the model's
  hash, the seed), when it `finished` and how long it `took`, its KPIs at the end of the horizon
  with intervals, and predictions at six stated times. Each forecast of a twin runs on seeds of
  its own (`--seed`, then on by the number of replications), so two forecasts are independent
  and any one can be run again from its record;
- `comparison`: each prediction whose time has come, beside the value then observed;
- `scores`: how the stored forecasts held (below);
- `readings`: each counter's last raw `value`, the model time `at` which it was said, and the
  count it stands in (`total`, `since`, `resets`). The twin started next on the same `--out`
  goes on counting from it;
- `problems`: coded, each with what the twin did about it.

A twin never starts a forecast from an empty model when it cannot read the plant's state. A model
with agents, or with a fleet whose vehicles the plant mapping does not observe, is monitored and
its forecast is refused (`E_TWIN_STATE`, naming the kind); a line with finite buffers,
accumulating conveyors, batches, transfers, robot and cobot cycles and the plant's orders, and a
fleet whose every vehicle reports where it is, are forecast like any other model; a source that is down skips the forecast until it answers
(`W_TWIN_SOURCE`), and so does a plant whose observations the mapping reads nothing of. The twin
keeps the model it loaded: after editing the model file, start the twin again.

A connected source that is merely quiet is not stale, so by default its age skips nothing. Set
your own limit with `--max-age` (on `run` and `once`; `30s`, `15m`, `2h`): while a source has
been silent for longer, no forecast is started, the result says which source and how long
(`W_TWIN_SOURCE`), and the view keeps its last picture.

<!-- no-run: needs a plant and your connections file -->
```console
naga twin run line.json --map line.plantmap.json --connections conn.json --out twin_out \
     --horizon 60 --max-age 15m
```

### Scores

Every forecast states, for each series it can be compared on (items in the system, units of a
resource in service, a level, and the parts a counter counted), a mean and a 5% to 95% band at
six times across its horizon. When each time comes the twin sets the plant's reading beside it
and scores it, once. `scores.rows` sums that by series and by lead (how far ahead the forecast
looked, in the model's time unit):

| Field | Meaning |
|---|---|
| `points`, `missed`, `lost` | readings scored; stated times the twin was not there for (stopped, source down); counted readings not compared because, after the forecast began, the counter lost count or the twin was started again with no last reading to go on from |
| `coverage` | share of readings inside the band. About 0.9 when the model is right; much lower says the forecast is too sure or off, 1.0 over many readings that its band is too wide. A row of whole numbers (`whole`, shown with a *: units in service, items, counts) covers more than 0.9 on a right model, because a band of whole numbers cannot be cut at exactly 5% and 95%: compare such a row by its `pinball` |
| `bias` | mean of observed minus forecast mean: positive when the plant runs above the forecast |
| `error` | mean absolute error |
| `pinball` | a band score: lower is better, and a band is paid for its width and, twenty times more, for what it misses by. Compare it only within one series |

`scores.basis` says what the rows stand on: how many forecasts and readings, how many missed,
since when, and the plant time they span. With no row, `scores.reason` says why. Read a score
as evidence, not as a test: work in progress is drawn afresh at each forecast, successive
forecasts overlap in time and one plant history is behind all of them, so the readings are
not independent and no interval is given. A twin run by `naga twin once` under a scheduler
forecasts on the same seed every time (`basis.same_seed` counts those): give each run its own
`--seed` if you want independent forecasts. The rolling score restarts when the model or
`--horizon` changes, and the twin says so once (`W_TWIN_SCORE`): keep one `--out` directory a
model and a horizon. A count is compared as its increase since the forecast started, and not
at all across a counter reset. A twin that is started again, as each `naga twin once` under a
scheduler is, goes on counting from the last reading in `twin.json` (`readings`), so its
forecasts of throughput are scored like the others. It does not when the counter then shows
less than that reading and declares no `rollover` (a reset: `lost`), when the counter next
speaks more than one `--horizon` after it, or when the reading is missing or cannot be read
(a twin of 0.6.1 or older wrote none): the count then begins where the counter is first seen.
A counter that was reset between two runs and has counted past its old value again cannot be
told from one that went on, as between any two readings; and a counter that says nothing
while a scheduled twin listens gives that step no reading.

To check a score, or to see what late plant events cost it, record the plant
(`naga watch --record`) and compute everything again from the log:

```console
naga twin score twin_out line.json --map line.plantmap.json --log plant.jsonl
```

It reads every stored reading again at the instant it was taken, computes each forecast's score
and the rolling score again, prints each difference and exits 1 if there is one. It writes
nothing. A twin that ran on the log itself (`--log`) agrees exactly; a live twin differs by
the events that reached it late.

### Alerts

The twin raises an alert when a compared series drifts away from its forecasts and stays away.
Each filled reading becomes a residual, observed minus the forecast mean, scaled by its band
(the 5% to 95% width read as 3.29 standard deviations), and a two-sided CUSUM sums those
beyond an allowance `k`; a side alerts while its sum is over the limit `h`. Both come from
your flags only, `--alert-k` (default 0.5) and `--alert-h` (default 4), never from the model
file. On independent readings these defaults alert after about 8 readings of a one-standard-
deviation shift and about once in 168 readings of none; a twin's readings come from
overlapping forecasts of one plant history, so they are not independent and false alarms
come more often. `alerts.rows` in `twin.json` lists the series alerting now, each with its
side, the plant time it began and its sum; `alerts.basis` counts the readings summed and the
alerts raised. The sums are kept in `twin_history.json` and go on when the twin is started
again with the same model and limits; otherwise they start again and the twin says so once
(`W_TWIN_ALERT`). Read an alert as a sustained shift worth a look, not as a diagnosis.

### Recommendations and the outbox

Give the twin a file of candidate actions and it compares each with doing nothing after every
forecast, from the same plant state and on the same random numbers:

<!-- no-run: needs a plant and your connections file -->
```console
naga twin run line.json --map line.plantmap.json --connections conn.json --out twin_out \
    --horizon 480 --candidates candidates.json --outbox file:proposals.jsonl
```

```json
{"format": 1, "kind": "naga-candidates", "kpi": "timeseries/block/Pack/out/max",
 "better": "higher", "expires": "30m",
 "candidates": [{"verb": "set_capacity", "target": "Packer", "value": 3}]}
```

- **Candidates are yours and closed.** At most 20, each `{"verb", "target", "value"}`. Built in
  this version: `set_capacity` (a resource of the model with a plain capacity, 1 to 10 000;
  for a resource inside an asset instance, name it as `<instance>/<resource>`, such as
  `station_1/Mfg Machine`: the template's own capacity is refused, since each instance sets
  its own) and `reroute` (`{"verb": "reroute", "target": "<a DECIDE>", "value": "<the block
  one of its links leads to>"}`: every item that reaches that branch from now on goes down
  that link; a DECIDE of the model itself, and one link to that block).
  The order verbs act on the plant's order book, which a mapping's `order` signal reads (see
  [A plant as a timeline](plant-timeline.md), "Orders"): `hold` (`"value": true`: not
  released until you release it), `release` (`true`: released now), `expedite` (`"value":
  <priority>`: on the order and on each of its jobs on the floor) and `resequence` (`"value":
  "<another order>"`: the two swap release times). Each targets an order by its id. They are
  refused by name where the forecast could not answer: a model that releases no orders,
  `expedite` where nothing reads `entity.priority` (a FIFO shop), and, at each forecast, an
  order the plant does not report, `hold`, `release` and `resequence` of one already
  released (its jobs are on the floor), and `expedite` of one already completed. To try
  `expedite`, use the bundled `order_priority_shop`: its Cutter serves the lowest priority
  number first, so expediting an order moves its completion.
  A candidate is tried as an override on the forecast; your model file is never changed.
- **What you get.** For each candidate, on the one KPI you named: the effect (the mean
  difference against doing nothing) with its 95% interval, and `confidence`, the share of
  replications in which the action did better. Only an action whose interval excludes "no
  effect" is recommended and ranked; the others are shown with their verdict (`no_effect`,
  `worse`, `failed`). With nothing to recommend, the result says why (`no_forecast`,
  `none_better`, `kpi`, `replications`, `failed`).
- **What it means.** What this model says the action would change, in distribution. It is not a
  causal claim about your plant. Each candidate costs one more forecast at every forecast,
  and each is judged alone: of candidates that change nothing, about 1 in 20 pass by chance
  (of N, about N/20).
- **The outbox is a queue of yours.** With `--outbox file:<path>` each recommendation is also
  appended to that file as one JSON line (a record: the action, the expected effect, an expiry,
  and the state, seed and model it was computed from). Without it, nothing is written outside
  `--out`. The twin never acts on a plant: a record is a proposal until you act on it.
  With `--outbox mqtt:<connection>` each record is published instead, at QoS 1 and not
  retained, to `<root>/proposals/<id>` on a connection of your `--connections` file that you
  kept for it (`"use": "outbox"`, `"root"` a plain topic such as `plant/line1/naga`); no
  model, mapping or tool can name one, and no source can read over it. A broker that is
  down keeps the record waiting, tried again until it expires.
  **A consumer that executes proposals without a human is the caller's system acting, on the
  caller's authority.**
- **Answering.** Append a line to `<path>.replies` (with an MQTT outbox, publish it to
  `<root>/replies`, the id in the payload, at most 1 KiB; the twin hears replies only while it is connected,
  unless you publish them retained):
  `{"id": "<the record's id>", "decision": "taken"}` (or `"rejected"`, `"expired"`; `at`, `by`
  and `note` are optional). The first reply for a
  record wins; one for an unknown id, a second one, or one after the expiry is counted and
  dropped. A record nobody answers expires.
- **After "taken".** The twin assumes the action is in the plant from then on: every later
  forecast starts with it (and says so: "This forecast assumes 1 taken action(s)"), until
  you end it (a reply `{"id": ..., "decision": "ended"}`), take another
  action on the same target, or change the model. If the KPI is what a counted block let out (`timeseries/block/<name>/out/max`, see
  counters above), the twin measures the effect itself: the counter's increase over one horizon
  from the moment it read your reply, set against the band it had given. Any other KPI is
  `not_observable`; a twin that was late says `missed`, and one that lost the count in
  between (a counter reset, or a restart with no last reading to go on from) says `lost`.
- **Reproducing.** `naga twin reproduce twin_out line.json` computes every stored
  recommendation again from its stored state and seed and prints each difference (exit 1 if
  there is one). The twin keeps the newest 20 states under `twin_out/states/` for this.

The page (`--monitor`) shows the recommendations read-only. An AI assistant can read them
(`get_twin_recommendations`) and can neither deliver nor approve one.

### Refit on approval

A twin given a refit file proposes a correction of its own model when an alert holds, and
applies it only after a person accepts. The file is yours, named by `--refit` and by nothing
else; it lists the parameters a refit may change with their bounds, the calibration budget
and the number of readings the candidate is judged on:

<!-- no-run: needs a plant and your connections file -->
```console
naga twin run line.json --map line.plantmap.json --connections conn.json --out twin_out \
    --horizon 480 --refit line.refit.json --outbox file:proposals.jsonl
```

```json
{"format": 1, "kind": "naga-refit", "budget": 60, "replications": 3, "readings": 12,
 "series": ["block/Infeed/out"],
 "factors": [{"path": "blocks.Infeed.params.dist_params.high", "low": 1.5, "high": 6.0}]}
```

- **When.** An alert on a series (`series` narrows which, default any) starts a refit once the
  first forecast made after the alert began has a whole horizon of readings behind it, at
  least ceil(h/k) of them (8 with the default flags). A shorter window chases noise; an
  alert that ends first starts nothing.
- **What.** The factors are calibrated on that window (`calibrate`, a series target, the file's
  bounds only); a factor may not set the state, a run setting or a data source.
- **The proof.** Every later forecast also runs the candidate on the same seed. Once
  `readings` readings of whole candidate forecasts are in, both are scored by their mean
  pinball loss, and the candidate is proposed through the outbox only when it is lower: "a
  refit of 1 parameter cuts the pinball loss 31% on the last 12 readings: adopt?". When it is
  not, the result says so, with both numbers. One refit runs at a time, and none starts while
  a proposal waits for its reply.
- **Adopting.** A `taken` reply adopts it from the next cycle: the overrides become a layer
  kept in `twin_history.json` (who replied and when), every forecast runs with it, and the
  score and the alerts start again, since they are now of another model. An `ended` reply
  reverts it. The model file is never written. A history whose layer was edited by hand is
  refused whole and the layer is taken again from the outbox ledger, whose record is
  content-addressed.

### Setpoints to a soft PLC

A twin can propose a setpoint, and a person can then write it to a soft PLC on this machine.
The twin itself never writes to a PLC: a `setpoint` candidate travels through the outbox like
any recommendation, and `naga plc-write`, a command you run, is the only writer. Your
setpoints file lists the tags that may be written, each with the register it goes to, the model
path that simulates it and its limits:

```json
{"format": 1, "kind": "naga-setpoints", "connection": "softplc",
 "tags": {"line.cycle": {"register": "cycle", "path": "blocks.Press.params.dist_params.value",
                         "min": 1, "max": 5, "max_step": 1, "min_interval": "60s"}}}
```

<!-- no-run: needs a plant, your connections file and a soft PLC -->
```console
naga twin run line.json --map line.plantmap.json --connections conn.json --out twin_out \
    --horizon 480 --candidates candidates.json --setpoints line.setpoints.json \
    --outbox file:proposals.jsonl
naga plc-write --outbox file:proposals.jsonl --setpoints line.setpoints.json \
    --connections plc.json --audit writes.jsonl            # dry-run; add --write to write
```

- **Proposing.** A candidate `{"verb": "setpoint", "target": "line.cycle", "value": 2}` names a
  tag of your setpoints file (`--setpoints`) inside its range; the forecast runs it as an
  override of the tag's model path.
- **Where.** The file's `connection` is a modbus connection of your connections file with
  `"use": "write"`, its registers holding registers. It must resolve to this machine
  (127.0.0.1 or ::1): this version writes to a soft PLC only, because a plant's safety system
  must own real writes. No source can read over that connection.
- **When.** Only a record whose first reply is `taken` with a `by` (who took it), not ended, not
  expired and not written before. The value must be within `min` and `max`, at most
  `max_step` from what the PLC holds now, and `min_interval` after the tag's last write.
  Anything else is refused by name (`E_PLC_NOT_TAKEN`, `E_PLC_STEP`, `E_PLC_INTERVAL`, ...).
- **How.** Without `--write` the command reads the PLC and writes nothing but the audit. With
  it, the value is written (Modbus function 6, or 16 for a 32-bit register) and read back; a
  different value is `E_PLC_READBACK`. Every attempt gets an audit line before anything is
  sent and one after.

An AI assistant has no tool that writes a setpoint: a write is always a person's act, with
every limit in that person's own files.

Know what this guard does not do, because it trusts the files and the machine it runs on. The
outbox and its `.replies` file are the trust boundary: whoever can write them can approve a
write, so keep their permissions to the people who may. The `by` in a reply is a claim, not
authentication. A loopback port can be a tunnel to a real PLC, so check what answers there.
Deleting audit lines defeats the interval check, and two `plc-write` processes at once are not
locked against each other, so run one. Treat these as limits of an advisory path, not as
protection against someone with access to your files.

### A monitor page

Add `--monitor` and the twin also writes `monitor.html` into `--out`. Open that file in a
browser beside the running command:

<!-- no-run: runs until Ctrl+C -->
```console
naga twin run line.json --map line.plantmap.json --log plant.jsonl --out twin_out \
     --horizon 60 --every 2s --monitor
```

The page reloads itself (every half cadence, between 1 and 30 seconds) and shows:

- the plant as it is now, drawn on the model's layout: items in each block, each resource's
  state, levels;
- each source's state and age, and the problems, each with its hint;
- the forecast: its status, and when there is none, why (a source that is down or silent
  beyond `--max-age`, a mapping that reads nothing, a model that cannot be forecast from a
  plant's state yet, which is monitored all the same);
- the latest forecast beside what was then observed. This is evidence in distribution, not a
  replay: work in progress is drawn afresh at each forecast, so a point inside its 5% to 95%
  band agrees with the forecast and one outside it is a signal, not a proof;
- the scores, by series and by how far ahead, with what they stand on, or why there are none;
- what the plant's counters counted, where the mapping has counter signals;
- the latest events, newest first: items entering, moving and leaving, resources going down
  and up, sources and the forecast changing state.

For a plant that names no items (queue lengths or unit counts only) the page says that counts
of items are approximate, and that the item events in the list are inferred from those counts;
what a counter signal counts is observed and is not under that label. The page
always says how long ago the twin last wrote; after two cadences with nothing written it says
STALE: what it shows is then old. That age is judged by the clock of the computer showing the
page, so a viewer whose clock is wrong sees a wrong age. A twin run by `naga twin once` under a
scheduler has no cadence: its page says it is not watched for staleness. The page is one file that asks the network for
nothing; it holds your model's layout (names and places), so it is written only when you ask.
It shows the present: it does not replay, and it uses generic shapes.

A program that reads `monitor.html` or `twin.json` must open the file, read it and close it.
While a reader holds a file open, Windows refuses the twin's write: the twin says so once,
keeps the old file whole, tries again at every cycle and says once when it is written again.

To let an assistant read a running twin, start the server with `naga mcp --twin twin_out`: the
tools `get_twin_monitor`, `get_twin_forecast` and `get_twin_recommendations` answer from that directory. No tool can start a
twin or choose a connection.

## A physics-simulated test plant

The twin can be tried against a plant whose timing a physics engine decided, not the model under
test. [Open Industry Project](https://www.openindustryproject.org/) (OIP, MIT licence) is a
browser simulator of conveyors with Jolt physics and a built-in IEC 61131-3 PLC. Its headless
runner steps a scene and its program for any number of simulated seconds and prints every tag
change with its time; `tools/oip_trace_to_log.py` turns that into the plant log `naga twin --log`
replays. The repository carries five minutes of one such run, so a test or a demo needs no plant,
no broker and no OIP install.

The run is a box spawner (30 boxes a minute, each gap random between half and one and a half
times 2 s), a 4 m belt at 1 m/s, a photo eye and a despawner. The operator presses Start at 1 s,
Stop at 150 s and Start again at 165 s. In `tests/data/oip_belt_line/`:

- `plant.jsonl`: the log, 596 lines, one tag change each, in simulated seconds;
- `line.json` and `line.plantmap.json`: a model of the belt (spawner, a 3.635 m conveyor, sink)
  and the mapping that turns two PLC counters, boxes spawned and boxes into the sink, into the
  counters of two blocks;
- `README.md`: where it comes from, OIP's licence, its commit and the commands that write the log
  again, which give the same bytes on the machine that recorded it.

<!-- no-run: the fixture is in the repository's tests/data, not in the page's scratch folder -->
```console
naga plant check tests/data/oip_belt_line/line.plantmap.json tests/data/oip_belt_line/line.json
naga twin once tests/data/oip_belt_line/line.json --map tests/data/oip_belt_line/line.plantmap.json \
     --log tests/data/oip_belt_line/plant.jsonl --out twin_out --horizon 30 --replications 50
naga twin status twin_out
```

The twin reads 144 boxes spawned and 143 counted, as the log says. Played at one second a
second (`naga twin run --speed 1`, or `tests/test_oip_test_plant.py`, which moves the clock
itself), a forecast made at 100.5 s is set beside what the plant then counted. Its forecast of
arrivals follows the plant within a box or two. Its forecast of exits runs about two boxes low,
the boxes that were on the belt when it began: a mapping of counters names no items, so the twin
starts from an empty belt (a conveyor cannot be started from an anonymous count). A forecast made
at 140.5 s runs on through the stop, because the model has no stop, while the plant counts nothing
from 150 s to about 166 s: the observation falls far below the 5% to 95% band, which is what a
score is for.

To record another OIP line, scaffold or open a project, add a test spec that presses Start and
runs as long as you want, run it with `--trace all --json`, convert the trace, and write a
[mapping](plant-timeline.md) for the tags you care about: each key of the log is a tag's name.

<!-- no-run: needs an OIP clone and Node -->
```console
npm run test-project --silent -- my-line --only record --trace all --json > trace.json
python tools/oip_trace_to_log.py trace.json plant.jsonl
```

### The live route (untested)

OIP's desktop app can publish the running program's variables as an OPC UA server, with security
`None` and anonymous sessions, on this machine only (its panel shows the endpoint; the example
below assumes port 4843). Our `opcua` reader ([Live data](live-data.md)) supports exactly that
for a connection you mark `private` (a loopback address is refused without it), and the pieces
that need no server are tested. **Nothing here has been run against OIP desktop**: the port, the namespace index and the
node names are to be read from its panel, or from any OPC UA browser.

```json
{"format": 1, "connections": {
  "oip": {"kind": "opcua", "url": "opc.tcp://127.0.0.1:4843", "private": true, "root": "ns=1;"}}}
```

Add the source to the model, and map each node id as the key (`time.epoch` is the minute you
start: model time 0):

```json
{"name": "oip", "kind": "opcua", "connection": "oip", "options": {"interval_ms": 500},
 "location": "ns=1;s=/SpawnCount\nns=1;s=/SinkCount"}
```

```json
{"format": 1, "time": {"epoch": "2026-10-22T09:00:00+00:00", "unit": "seconds"}, "signals": [
  {"source": "oip", "key": "ns=1;s=/SpawnCount", "as": "counter", "block": "Infeed"},
  {"source": "oip", "key": "ns=1;s=/SinkCount", "as": "counter", "block": "Belt"}]}
```

<!-- no-run: needs OIP desktop with its OPC UA server on, and nagasim[opcua] -->
```console
naga watch line.json --connections oip.json --check
naga twin run line.json --map live.plantmap.json --connections oip.json --out twin_out --horizon 30
```

The reader only subscribes: it writes no node and calls no method, so the simulated PLC cannot be
changed from here.

## Scheduling re-runs

There is no bespoke scheduler here: a forecast that should refresh every hour is `naga forecast
... --out fc.xlsx` under the OS scheduler you already have (Task Scheduler on Windows, cron or a
systemd timer elsewhere), which already handles restarts, logging and overlap.
