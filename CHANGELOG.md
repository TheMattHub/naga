# Changelog

## Unreleased

- AI-run studies in the chat: in assistants that support MCP Apps (needs the `mcp` 2.x SDK), `study_show` opens the control room inside the chat, with buttons to go back to an earlier version and to accept the study with a reason; elsewhere it answers a short summary and the page's path.
- AI-run studies have a control room: `control-room.html` in the study folder opens with the verdict, then the goals and checklist, the target, and the version timeline with flowcharts and KPI charts; it runs no script, prints to PDF and is refreshed after every version. `study_package` writes the deliverable: the page, an Excel workbook, the current model and RO-Crate metadata.
- AI-run studies can be seen: every study version keeps a flowchart of its model (SVG and a text list), with the step's changes outlined and the target's unbuilt parts dashed, and `study_view` returns it to the assistant as a PNG image over MCP. The `mcp` extra now needs `mcp>=1.19`, the first SDK that passes an image result through.
- AI-run studies: seven agent tools (`study_start`, `study_status`, `study_step`, `study_update`, `study_add_input`, `study_revert`, `study_accept`) let any assistant run a simulation study through the engine. The study lives in a folder (the question, goals with KPI ranges, assumptions and sources, validation, inputs, and every model version with its checks and report), so a different assistant or a person can resume it. Each step runs the whole model, checks it (validation, utilisation and flow balance, reproducibility, assumption and goal ranges) and stores it as a version, or as a draft with its problems when it does not run; every answer ends with the next step. Checks end in one of four verdicts (passed, partial, failed, not verified), and a study is "sufficient for its goals" or "not yet sufficient", never finished. New page: Run a study with an assistant.
- Docs: the engine site is reorganised for four readers. The landing page opens with the install line and a runnable ten-line example whose printed output the docs build generates; a six-step Learn path grows one model from a queue to scenarios and the event log; the menu is Learn, How-to, Reference, Background, Validation and Release notes, with the reference models, benchmarks, a Citing page and a stability policy under Validation and Background. The Python API reference is generated from numpydoc docstrings whose examples run as tests; the command-line reference has one page per command group; moved pages redirect. Every page has a plain-Markdown copy and `llms-full.txt` holds the whole site for AI readers. New Coming from salabim and Mesa pages run the same model in both libraries.
- Docs: the Python API reference's Model page lists `run`, `to_dict`, `save`, `definition`, `link` and the `add_*` list builders, and a new Results page covers `kpi`, `kpis`, `frame`, `plot` and `to_excel`; every method has a doctested example, and the Markdown copies include them.
- Docs: two worked examples on the engine's site, airport ground support and a container terminal, whose numbers and charts come from runs of the bundled models; `turnaround_workload` gains scenarios with fewer ramp agents and belt loaders.

## 0.12.0 (2026-10-17)

One table now holds what the engine knows about each field of a model, and the published
schema, submodel flattening and validation all read it, so they can no longer disagree. A minor
version, because validation is stricter (a number outside its field's bounds is refused, where a
negative level inflow used to crash the run) and a few unused commands and Python names are removed.

- Validation warns when an asset reads a name from a parameter left empty, for example an **Equipment draw** with no equipment set, instead of failing when the model runs.
- The container terminal and ground support electrification showcases put their chargers' power on the grid through the Energy **Equipment draw** asset: the `Charging draw` flow is now reported per Equipment draw (`equipment_draw_1/Nrg equipment`, ...), and every other result is unchanged.
- Expanding a library asset in place now saves a model that validates against the published JSON schema.
- Fixed: flattening a submodel now resolves `$` parameters in a delay's `condition`, a fleet's load and unload times, a charger's swap time and a level conveyor's transit time, and a whole-number field such as a fleet's `loads` stays a whole number.
- Validation now refuses a number outside the bounds of its field, which the published schema already refused: a zero time series `period`, a zero fleet speed, a negative `length_m` or `hoist_time`, a zero charger `power_kw`, a negative level `max_inflow` or `max_outflow` (`E_FIELD_RANGE`). A negative `max_inflow` or `max_outflow` used to crash the run with a ZeroDivisionError.
- Fixed: expanding a library asset or a submodel instance writes its added variables in the same order every run.
- The published model schema takes the bounds the engine accepts: `simulation.timeseries_interval` may be 0 (no sampling) and a charger's `swap_time` may be a number. The schema's field types, defaults and bounds now come from the same table the engine reads, so the two cannot disagree.
- Removed: `naga state observe`. A state of the plant comes from `naga twin`; `naga state take` and `naga state check` stay. Hints that said "take or observe" now say "take it again" or "write it again".
- Changed in the Python API: `report.build_report(twin=)` is now `extra_sections=` (finished HTML sections; the product builds its MES twin section with `report.esc`, `fmt` and `table`), and the values of `expressions.LOOKUP_SECTION` are tuples of sections (`"POWER"` reads three).
- Removed from the Python API: `costs.COST_KINDS`, `SimSession.take_events`, `ModelDocument.set_description_text` (use `set_info(description=...)`) and `api.run_replication`.
- `naga twin status` keeps its head line, source rows and view line, and now prints the forecast and the compared points in the monitor's wording (`Status: ok`, "N compared points ..."); the "forecast: ..." and "compared: ..." lines are gone, and the notes, counters, scores and recommendations follow the monitor page.
- `naga run --out` and `--report` go through the exporters and print "xlsx written to PATH" and "html written to PATH"; the files are unchanged.

## 0.11.0 (2026-10-17)

A bundled model sizes shore power at a berth, and validation catches a misspelt name passed
through a submodel parameter. A minor version, because that check can now refuse a model that
was accepted before (it would have failed when run).

- Added: a bundled model, shore power at a berth: ships' hotel loads fed from the grid up to the
  connection and the rest on their auxiliary engines, four like-for-like scenarios and an
  investment case that says who pays and who saves.
- Validation: a reference function (`POWER`, `TS`, `LEVEL`, `FLOW` and the rest) whose argument
  is a submodel parameter bound to a name is checked like a literal name, in flows, system
  dynamics records, block expressions and `dist_params`. An unknown name is reported when the
  model is checked instead of failing the run; an unset (empty) parameter is not flagged.

## 0.10.0 (2026-10-17)

A model can now be drafted from an event log, and energy reaches every fleet. `naga discover`
turns a CSV or XES log into a model with a report of how far to trust it; `POWER()` and
batteries on any fleet price and bill equipment power; the debugger steps back exactly. A
minor version, because flow rates are now read in their own time unit and a tag mixing
units is refused (see the behaviour changes below).

- Added: `naga discover LOG` drafts a model from an event log (CSV or XES): arrivals, activity
  times, routing by chance or by variant, resources at their observed peak, and a report with a
  check of the log and next steps; `--check` reads a log without drafting. The agent tool
  `import_event_log` does the same in a workspace. A sample log (`job_shop_log`) and a tutorial
  come with it; the event-log page says how accurate drafts are on three public real logs.
- Event logs: suspend and resume are read, so suspended hours no longer count as service time;
  `--idle-waits` adds a modelled wait before activities that wait long at mostly idle
  resources; `--check` warns when a log starts empty.
- Added: `POWER("name")` reads the kW a resource, fleet or charger draws, so energy flows can
  price, peak-bill and emit equipment power. Any vehicle, worker or gantry fleet can carry a
  battery; `capacity_kwh` 0 switches it off.
- Added: four bundled models, a container terminal from quay to yard, electrifying the terminal,
  an airport hub day and electrifying ground support.
- Debugger: a block stop may carry a `when` condition or one entity, can be switched off and
  counts its hits; `debug_run(back=True)` steps back exactly (a replay from the seed);
  `debug_run(scope=)` steps inside one submodel instance.
- Scenarios take an optional `goal` (text), shown in the HTML report's scenario table.
- Submodels: a `dist_params` value can take `{"$arg": "<parameter>"}`; a block-less asset (a crew
  pool, a vehicle fleet) owns the resources and fleets it names under `scoped`.
- Orders bindings keep extra columns as order attributes; a column named like an order's own
  field or that is no attribute name is skipped with a `W_ORDER_FIELD` warning.
- Behaviour change: where a flow's `rate_time_unit` differs from the simulation's time unit,
  `FLOW()`, `FLOWSUM()`, Avg/Peak Rate, thresholds and the `peak_price` demand charge now read per
  that flow's unit, and a conversion with its own `time_unit` reports `CONV()` per that unit.
  Validation warns (`W_RATE_UNIT_CHANGED`, `W_RATE_UNIT_READ`); flows sharing a tag in two time
  units are refused.
- Behaviour change: a battery fleet with an idle draw now charges while standing idle, and such
  a run ends at its time limit.
- Builder: `results.kpi("system/Total Entities Completed")` reads one KPI's mean; a block's
  name may be given as `name=`; an `add_<item>` keyword the record does not have raises, as a
  block method's does.
- Added: the bundled model `order_priority_shop`, an order shop whose cutter ranks jobs on
  priority, so `expedite` shows its effect.
- Fixed: a reality check using `FLOW()` or `FLOWSUM()` no longer crashes, and one using `CONV()`,
  `FRACTION()` or `POWER()` says to read it through an auxiliary; `POWER()` after a shift change
  and in a flow price or a conversion feed.
- Fixed: unit checks follow a flow's own time unit. The system-dynamics dimension check
  expects level unit per `rate_time_unit` and reads kW as kWh per hour; a flow threshold's
  `value` is a rate field, so switching the flow's unit converts it; switching a unit to
  "base" converts to what "base" means; a FLOWSUM tag built from a submodel parameter is
  checked as the literal it becomes, and a name computed at run time is checked against every
  one it may read, or warned `W_RATE_UNIT_UNCHECKED` when those differ in time unit.

## 0.9.1 (2026-10-16)

A small release: a keyword for running only the scenarios you name, and documentation that
says what the engine is and how to write a model.

- Docs: the home page opens with what the engine is for, what it models, what it does around a
  run and the three ways in; the README shows how to write a model in Python and as a file;
  the twin page and the README's limits say what a state from a plant now carries.
- `run_scenarios(..., baseline=False)` runs only the named scenarios; `nagasim.dashboard.
  generated_widgets` gives the charts a dashboard viewer shows for a model that declares none.

## 0.9.0 (2026-10-16)

A twin now follows the plant's orders. A plant mapping reads one row per order from any MES or
ERP, the state carries the plant's order book, and the twin compares `hold`, `release`,
`expedite` and `resequence` on the forecast. A model's dashboard can hold a Live page read from
a separately started twin. A minor version, because a state now refuses a key the engine does
not know, and 0.8.0 refuses a file with a Live page and ignores an order book.

- The dashboard section gains the `live` activity (status, view, forecast, recommendations),
  read by the product's Present mode; schema v29, no bump. A model file that stores a Live page
  needs this version: 0.8.0 refuses the whole file. `nagasim.dashboard.generated_dashboard` and
  `input_for` are new.
- Fixed: the twin result reader takes regular files only and refuses a UNC path; the bundled
  models ship in `naga fmt`'s form (0.8.0's wheel had them in the long form; they ran the same).
- Twin: a twin follows the plant's orders. A plant mapping's `order` signal reads one row per
  order from any MES or ERP, an `at` signal's `order_field` links each job to its order, and the
  state carries the plant's order book, so a forecast releases the plant's orders, each once,
  and never a held one. The order verbs `hold`, `release`, `expedite` and `resequence` are built
  and compared on the forecast like `reroute`, refused by name where the forecast cannot answer.
- Changed: a state refuses an `initial_state` key the engine does not know (`E_INITIAL_KEY`);
  0.8.0 ignores an order book silently.
- Fixed: a part past a STATION keeps its `station` in a state from a plant; `naga plant
  mock` with an order signal; `held` must be true or false; a released order from a
  plant completes in the forecast; a book drops `orders` bindings; the schema lists
  `initial_state.tended`.

## 0.8.0 (2026-10-16)

A model can now be written without an editor: `nagasim.build` builds one in Python, with an
editor completing every built-in parameter, distribution and unit; `naga export --python` turns
any model into that code; `naga fmt` writes a model file in its shortest form, and the bundled
models ship that way. A state read from a live plant now covers conveyors, batches, transfers and
robot and cobot cycles, so a twin forecasts the warehouse and automation models from plant data.
A minor version, because two results can change: a block parameter left out of a model file now
takes its published default (a state saved for such a file must be taken again), and a
deterministic Schedule arrival no longer depends on the seed.

- State: a MOTION job mid-cycle and a mobile cobot tending a machine (a pick held by its
  machine, a place in progress, each cycle's time) are carried; `automation_cell` and
  `automation_floor` are reproduced from a state taken at any time tried. Tending from a
  plant stays refused by name.
- Fixed: a run started from a state with a job in service beside a waiting preempting request
  no longer crashes or lets it overtake earlier waiters; a MOTION job put in service without a
  cycle takes a whole one; a cobot trip at a tending station is put back with its place cycle;
  a tending cycle time, a place into a machine with a queue, and an entry a TRANSPORT takes on
  with no station are checked on load.
- State: every bundled model that a state carries is now reproduced exactly from it: an order's
  counters, the order of arrivals due at one instant, and a schedule's offset are carried.
- Changed: in deterministic mode a Schedule arrival comes without a random draw: arrival k comes
  when the schedule's integrated rate reaches k, so results no longer depend on the seed.
- Changed results: the time-series sampler, its auxiliaries and dashboard widgets no longer
  change the level accounting; six bundled models move in the last bits (at most
  2.5e-14 relative), no value rounded to 4 decimals. A flow's Peak Rate no longer counts a rate
  held for float noise after a warm-up: `sd_population`, deterministic, warm-up 32.8,
  reports 21.403 instead of 21.728.
- Fixed: a deterministic Schedule arrival no longer hangs the run on a tiny rate or a repeating
  schedule whose bounds are not exact floats; a state document with arrival keys on
  a Schedule CREATE, a huge integer, or an order counter below zero is refused by name.
- Twin: a state from a live plant now covers conveyors (from when each part entered), batches
  (a manifest of their members), order lines and transfer amounts, a cobot's pick and place and
  a robot's cycle step, so a twin forecasts `warehouse_forklifts`, `warehouse_library`,
  `mfg_line_library`, `automation_cell` and `automation_floor` from plant data. New mapping
  signals: `attributes` and `batch_field` on `at`, `tend`, `step`; `naga plant init` maps them.
- Fixed: a state from a plant keeps the model's name and a resource a belt part holds;
  a part merged on a belt frees its place; `naga plant init` types the batches a
  model forms.
- Authoring: `naga fmt FILE... [--check]` rewrites a model in its shortest canonical form
  (defaults, units and empty sections left out, `$schema` first); the model and its runs are
  unchanged. `nagasim.build` (provisional) builds a model in Python, one method per block type
  generated from the registry; `naga export --python` writes that code for any model. The model
  JSON Schema is published at `https://thematthub.github.io/naga/schema/model-v<N>.json`: add
  `"$schema"` for completion in your editor. Bundled models ship in the short form.
- Changed results: a block parameter left out of a model file now means its published default
  (a CREATE without an entity type made type "A", now "DEFAULT"; a HOLD/SIGNAL without a signal
  name used "Default", now ""). The model hash of a file that left any parameter out moves
  once: take a saved state for it again.
- Fixed: `naga export --python` refuses a block type that is not registered; `naga fmt`
  keeps a value whose type differs from its default and refuses non-finite numbers
  and files that are not models.
- Builder: a distribution constructor per distribution, `nb.Exponential(rate=1.25)`, wherever a
  model has a distribution (a misspelt parameter is an error; `lambda` is `lambda_`); run settings
  are `Model` keywords, `nb.Model(time_limit=1000, warmup=100, replications=5)`; the package ships
  `build.pyi`, so editors complete block parameters, records, run settings and distribution
  parameters and flag a wrong one, a misspelt unit (`setup_unit="secs"`) included. The string
  forms still work; model files and results are unchanged.
- Fixed: a number given for a parameter that takes a number or an expression (a setup time,
  a renege time, a batch's maximum wait, ...) is accepted, as the schema says.

## 0.7.2 (2026-10-15)

A state now carries a batch that has already formed, with its members, and an entity waiting
at a TRANSFER, so the bundled `warehouse_library` and `mfg_line_library` models restart exactly
from a state taken at any time. From a plant both are still refused: no plant signal names a
batch's members or a transfer's amount. One result changes: a level's Min and Max now count the
value a TRANSFER leaves, which raises the Max of `biogas_plant_library`'s feed stores slightly.

- State: a formed batch (a truck at the dock, a truckload, an oven load; of a BATCH or a MATCH)
  is carried with its members, and an entity waiting at a TRANSFER for room or stock is carried
  in its level's queue order; `warehouse_library` and `mfg_line_library` are reproduced from a
  state taken at any time tried. From a plant both stay refused by name.
- Fixed: hand-written states that put a batch's members where no batch can be, leave out a hold
  a later RELEASE needs, name an attribute after the engine's own, ask a transfer for more than
  its level holds, or date a hold before its entity are refused by name.
- Changed results: a level's Min and Max count the value a TRANSFER lump leaves; in
  `biogas_plant_library` the feed stores report a slightly higher Max (10154.59 to 10155.70 at seed 0).

## 0.7.1 (2026-10-15)

The twin's live-data side is now tested against software we did not write: Mosquitto, an
independent Sparkplug B edge node and an asyncua OPC UA server, with two Sparkplug fixes found
that way. A state now carries parts on a conveyor and a job holding a resource between blocks,
so the bundled `warehouse_forklifts` model restarts exactly from a state taken at any time (from
a plant, a conveyor is still refused: no plant signal says where a part is on the belt). A state document that
was quietly wrong (a batch timer with nothing waiting, a unit held twice, a hold nothing
releases) is now refused by name.

- Tested: the MQTT reader, the MQTT outbox and Sparkplug B against Mosquitto 2.1.2 and an
  independent Sparkplug B edge node (protobuf, Tahu's encoding), the OPC UA reader against an
  asyncua 2.1.0 server, and `naga twin run` end to end over them (opt-in,
  `NAGA_REALSERVER=1`).
- Fixed: a negative Sparkplug Int8 or Int16 sent as Tahu's Python library sends it reads as
  the number it is; a Sparkplug node's death takes its devices offline.
- State: parts on an accumulating conveyor (every stage, and the hand-over to the next belt), a
  job that holds a resource seized in an earlier block, and a batch's running maximum wait are
  carried; `warehouse_forklifts` is reproduced exactly from a state taken at any time tried.
  A conveyor in a state from a plant is refused by name.
- Fixed: a state from a plant puts a job back with the unit it holds; a resource used only
  before a taken state keeps its report row; hand-written states with a batch timer and nothing
  waiting, a unit held twice, a hold nothing releases, or a conveyor from a plant are refused by
  name.

## 0.7.0 (2026-10-15)

An agent now writes a whole model in one call, and in the LLM case study that made Claude Code
more often right through Naga than writing SimPy. A twin starts from the state of lines with
finite buffers and of fleets of vehicles, recommends a new action, `reroute`, and can publish
its advice to an MQTT topic of yours. A minor version, because validation is stricter: a model
that validated under 0.6.1 may now be refused where it was wrong all along, for example a
PROCESS that seizes with no resource named, or a distribution parameter the distribution does
not take.

- Added: the recommendation verb `reroute` (a DECIDE of the model, to the block one of its
  links leads to); `naga twin --outbox mqtt:<connection>` publishes each record at QoS 1, not
  retained, to `<root>/proposals/<id>` over a connection of your file kept for an outbox, and
  reads replies from `<root>/replies`. The order verbs stay refused by name.
- Added: `write_model` takes a whole model file in one call and answers with its problems and
  their places; `get_model_format` describes the file from the engine's own registries. The
  compact tool list of the assistant and `naga mcp --tools compact` serve that surface. In the
  LLM case study this made Claude Code more often right through Naga than writing SimPy, at
  under two thirds of the cost of building block by block.
- Added: warnings for a DECIDE or balking condition that reads `AVAILABLE()` as if it were the
  free units (`W_AVAILABLE_IS_CAPACITY`) and for a time series whose period is never used
  (`W_SERIES_PERIOD_UNUSED`).
- Fixed: a distribution parameter the distribution does not take is `E_DIST_PARAM_UNKNOWN`
  instead of being ignored; a DECIDE given conditions in chance mode is told to set its mode;
  a repeating time-series profile in another unit never returns the instant asked as its next
  change.
- Added: a state carries a line with finite buffers (the parts in each buffer, a machine
  holding its finished part, who waits for a place) and fleets and vehicles (where each
  vehicle is, its drive, loading or unloading under way, its load, the dispatch queue in its
  order, its battery charge, a vehicle driving to a charger, waiting for a point or
  charging): `naga state take`, `naga run --state`.
- Added: a plant mapping can observe a vehicle (`"as": "vehicle"`: the node it last
  reported) and its load (`"as": "load"`); a twin of a line with finite buffers, or of a
  fleet whose vehicles each report where they are, forecasts instead of being refused. A
  twin refuses when any declared fleet is not observed whole.
- Added: `naga state check` says when a state says nothing about the model's vehicles or
  buffers, and which version wrote it.
- Still refused by name: conveyors, tours, segment capacities, gantries, worker fleets,
  mobile cobots that tend, swap stations, scheduled charging, a vehicle out of service,
  blocking before service.
- Changed: the minimum state of charge after a warm-up that falls during a charge is the
  charge at the warm-up, not the charge the vehicle plugged in with.
- Changed: a state no longer carries the cost charged to an entity before it was taken, so a
  run started from it reports the costs of its own window, as a warm-up does. Infinity and
  NaN are refused in a model file's `initial_state`.
- Fixed: a job put in service by an initial state blocks after service like any other. An
  initial-state entry whose `attributes` are no object, an entity that is no object, a
  `block` that is no name or `entities` that is no list is `E_INITIAL_BLOCK`, not an
  exception. From a plant, the level a BUFFER mirrors is made of its parts.
- Added: a twin started again, as each scheduled `naga twin once` is, goes on counting from
  each plant counter's last reading (`readings` in `twin.json`), so its forecasts of throughput
  are scored and an action's measured effect survives a restart; a reset, a reading older than
  one horizon, or a damaged or missing reading starts the count again and is never an error.
- Fixed: a flow rate that reads `TNOW` in a model with no step grid (`simulation.sd`) is
  warned (`W_FLOW_CLOCK_NO_GRID`); before, the rate was read once at the start and a timed
  pump or draw never switched, with no signal.
- Fixed: a PROCESS, SEIZE or RELEASE block with no resource is `E_RESOURCE_UNKNOWN` at
  validation instead of an error in the run, and the `run_model` tool answers a failed run
  as `E_RUN_FAILED`.
- Checked: SimPy 4.1.2 leaves its stop event in the queue after `run(until=)`; the engine
  never calls it, results are identical on 4.1.1 and 4.1.2, and a test holds that.

## 0.6.1 (2026-10-13)

What the first days after the release asked for: a Step button in the recorded player,
recommendations that say the action first, measured benchmarks and reference models in the
documentation, and four fixes that let a small local model get further with the agent tools.
Nothing in the model file or the Python API changed.

- Added: the engine's package carries `py.typed`, so a type checker reads its annotations
  from an install.
- Added: the HTML player has Step and Step back buttons that move the shown time by one
  recorded sample and pause (the twin monitor page does not show them).
- Changed: a recommendation on the monitor page and in `naga twin status` leads with what
  stands, the recommended action and the outbox line; the basis, the explanation and the
  compared candidates fold (a `<details>` block on the page, an indented "basis:" block in the
  status text). Nothing is removed and the result documents are unchanged.
- Added: **benchmarks.** `benchmarks/compare.py` measures Naga against raw SimPy, salabim and
  Ciw on four queueing models with known theory, timing an implementation only when its result
  agrees with it; the "Coming from SimPy" guide shows the measured table.
- Added: **reference models**, a documentation page that sets eleven exact answers (queueing
  formulas, a Jackson network, logistic, Bass and SIR closed forms, an agent epidemic's exact
  mean final size, a tank followed by hand) beside what a run of the bundled model gives, with
  its 95% interval; the page is generated and a test repeats every run.
- Added: two bundled models, `single_server_general` (an M/G/1 queue with a uniform service
  time) and `tank_fill_and_drain` (a tank that fills, overflows and runs dry, exact by hand).
- Added: `core.debugger.watch_functions()` and `watch_names(layout, function)`, and
  `expressions.name_sections(function)`: the functions a watch may call, with a call hint, and
  the model names each one's first argument takes.
- Added: an LLM case study under `benchmarks/llm_study/` (twelve tasks with derived answers,
  a tools arm and a SimPy arm, a local and a Claude Code harness) and its docs page.
- Fixed: an agent tool called with a JSON string for an object or array argument (what small
  local models send) now parses it; a string that is not that JSON is refused naming the
  argument. A required argument left out is named, and said to be inside another argument
  when it was put there. A block whose distribution is malformed is reported as a problem
  instead of making validation raise. The `new_model` tool no longer says an empty model is
  invalid.

## 0.6.0 (2026-10-12)

The first public release. Naga is a simulation engine built on SimPy: a model is a data file of
blocks, resources and expressions, run from the command line, from Python or by an agent over
MCP. This version adds, to the engine of 0.5.0:

- **Live data and a twin.** Readers for files, SQL, REST, MQTT and OPC UA with one guarded way
  in; a run that starts from a plant's observed state; a twin cycle (`naga twin`) that follows a
  plant, forecasts from where it stands, scores each forecast against what happened, and compares
  candidate actions on the forecast and writes the better ones to a file you name.
- **Two things to share.** `--player` writes a recorded run as one small HTML file; `naga viewer`
  writes one HTML file in which the engine itself runs in the browser, with no network.
- **A debugger for a run**, a self-reloading monitor page for a twin, and 68 agent tools.
- **Documentation** with a gallery of the 52 bundled models, each run by name
  (`naga run single_server`, `naga examples`).

Limits you should know before relying on it:

- No test has run against a real broker, OPC UA server, MES or plant counter: the readers and
  the twin are tested on in-process stand-ins and recorded logs.
- A twin cannot yet start from the state of a model with vehicles or blocking buffers; such a
  model is monitored and its forecast is refused by name.
- Recommendations build one action, a resource's capacity, and one sink, a file.
- The viewer needs Chrome, Edge or Firefox on a desktop; it does not open in Safari.
- The API may still change between minor versions before 1.0.

The details, as they were written while it was built:

- Added: the README's first image, an overview diagram of the engine, and pictures of the
  report, the recorded player, the twin's monitor and the viewer (`docs/img/`); the
  documentation site carries the logo. Install lines read `pip install nagasim`.
- Fixed: `NQ`, `NR`, `MR`, `AVAILABLE` and `IDLE` read a resource set as their reference says;
  on a set they answered "unknown resource".
- Added: `naga examples` lists the bundled models with their titles (`--path` prints their
  folder), and the model argument of every command falls back to a bundled model's name when no
  file is at the path given: `naga run single_server`. `nagasim.examples`: `names()`,
  `path(name)`, `model_path(value)`. A name is letters, digits, `_` and `-`; an existing file
  always wins.
- Added to the documentation site: the modelling guide, experiments, model assertions and
  semantic diff. The site loads nothing from another site except the script that draws the
  gallery's diagrams, and says so.
- Changed: block, schema, problem-code, tool and help texts no longer carry internal ids; the
  wording of some descriptions changed, no behaviour did.
- Fixed: the README's Python snippet read a result frame that does not exist.
- Changed: the descriptions of the bundled models say what each shows and what to read in its
  results, name no editor feature, and `automation_cell` and `automation_floor` have a title and a
  category. A model's description is part of its hash, so a state file (`naga state take`) taken
  from a bundled model before this version no longer matches it and must be taken again.
- Added: the documentation site (`docs/`, `mkdocs.yml`): quick start, a gallery of the bundled
  models with numbers from a run, guides for people coming from SimPy, Arena, AnyLogic, Vensim
  and NetLogo, the agent guide, `llms.txt` and a generated reference. `tools/gen_engine_docs.py`
  writes the generated pages; `tests/test_engine_docs.py` regenerates them and runs every
  command and snippet of the site.
- Fixed: a block that names no `position` is placed at the origin: a model file written by hand
  passed `naga validate` and then stopped `naga run` with `KeyError('position')`.
- Fixed: the entity log's "Exited At" and "Throughput Time" are float columns in every
  replication; a replication in which no entity left made them object columns, and joining the
  replications raised pandas' FutureWarning.
- Changed: the descriptions of sixteen bundled models no longer carry the development plan's own
  tags.
- Added: `nagasim.recommend`, `nagasim.outbox`, `naga twin run / once --candidates FILE
  [--outbox file:PATH]` and `naga twin reproduce DIR MODEL [--id ID]`. `twin.Cycle(advisor=
  recommend.Advisor(...))` compares the caller's candidate actions (`recommend.load`; built:
  `set_capacity`) with the forecast on its own seed (`recommend.effects`: effect, 95%
  paired-t interval, confidence, verdict), keeps a record a clear gain in `twin_outbox.json`
  with its state document under `states/`, appends it to the caller's file sink
  (`outbox.open_sink`, `FileSink`), reads replies from `<path>.replies`, applies a taken
  action to later forecasts and measures its effect from a plant counter. `twin.json` gains
  `recommend`; `monitor.summary` gains `recommend`; one more read-only agent tool,
  `get_twin_recommendations` (68 tools). Candidates never enter the rolling score. New
  codes: `E_CANDIDATES`, `E_CANDIDATE_VERB`, `E_RECOMMEND_KPI`, `W_TWIN_RECOMMEND`,
  `E_OUTBOX_SINK`, `W_OUTBOX`. `recommend.override_of(action, model)`: a target
  `<instance>/<resource>` is the instance's argument, and a capacity an instance binds is
  refused; `record_id` covers the KPI and the horizon; a reply `ended` ends a standing
  action; a sink with a second hard link or a `:` after its drive is refused. Not built,
  refused by name: the `mqtt:` sink, the verbs
  `expedite`, `resequence`, `hold`, `release`, `reroute`.
- Added: `naga viewer MODEL -o FILE.html` and `nagasim.viewer` (`build_viewer`, `what_if`,
  `viewer_problems`, `freeze_data`, `compare`): one HTML file in which this engine runs in a
  browser under Pyodide 314.0.7 and re-runs the model with the inputs of its `dashboard`
  definition, beside the author's baseline and recorded run. `naga viewer --fetch` downloads the
  pinned runtime (29 MB, checked by SHA-256; `--runtime DIR`, `NAGASIM_VIEWER_RUNTIME`); nothing
  is downloaded otherwise. `--freeze` reads the model's data sources once and writes the values
  into the copy. `build_viewer` takes `on_progress` and `should_stop` (`viewer.Cancelled`) and
  writes the file last. The file holds the whole model and the engine's source. Per-replication
  results equal the desktop's; interval bounds can differ in their last digits between scipy
  versions. Problem codes `E_VIEWER_MODEL`, `W_VIEWER_LONG_RUN`, `W_VIEWER_EMPTY`.
- Added: `nagasim.score` and `naga twin score DIR MODEL --map FILE --log FILE`. A twin scores
  each stated point of its forecasts once (`score.point`: inside the 5% to 95% band, error,
  pinball loss), keeps a record's `score` and a rolling score by series and lead
  (`score.Totals`, the `scores` key of `twin_history.json`), and puts `scores` (with `basis`, or
  `reason`; a row of whole numbers is marked `whole`) in
  `twin.json`; `score.recompute` computes all of it again from a plant log. Plant mapping:
  a tenth signal kind, `counter` (`block`, optional `rollover`), `Mapper.counters()`,
  `scaffold(counters=True)`, `naga plant init --counters`; the result has `view.counters`.
  Model file: `simulation.timeseries_counts` names blocks whose departures are sampled as
  `block/<name>/out`. `monitor.summary` gains `counters`, `counted` and `scores`;
  `get_twin_forecast` returns `scores`. Problem codes `E_TIMESERIES_COUNTS`, `W_TWIN_SCORE`.
- Fixed: no reading is taken from a plant source that is down, silent past `max_age` or unread
 ; a value of an item delivered again after the item left makes no new item; an
  integer too large for a float is no number where a file or a plant is read.

- Added: `nagasim.monitor` and `naga twin run | once --monitor`: a monitor page,
  `monitor.html`, written beside the result at every cycle and reloading itself. `summary(doc)`
  is what a result says to a person, `Log` the last event lines, `state(summary, now)`
  the sentence on its age and whether it is stale (two cadences with no result), `Page(layout, path, every=).write(doc, events)` the page.
  `Cycle.events` are the trace events of the latest step (never in the result), taken from
  `PlantSource.take()`, which keeps what was mapped until it is asked; `twin.write_text` is the atomic write; `TapeWriter.scene(frames)` is
  public.
- Fixed: a run started from a state is sampled over its own window, from its clock to its end;
  a short horizon late on the clock had one sample or none.

- Added: `nagasim.twin`, the twin cycle. `Cycle(source, horizon=...).step()` observes a plant,
  starts a forecast from its state of now (`forecast_every`, inline or in the background,
  cancelled by `stop()`), compares the stored forecasts with what is observed and writes one
  result (`twin.json`, format 1, replaced atomically; `read_result`, `write_json`). `PlantSource`
  reads a `Feed` through a plant mapping, `LogFeed` plays a recorded log, `loop` is the service
  loop. Commands: `naga twin run | once | status`, `naga mcp --twin DIR`. Agent tools:
  `get_twin_monitor`, `get_twin_forecast` (67 tools). `state.refused_kind(model)` names what no
  state can carry; `state.start(..., until=)` sets the end of the run a state is started for;
  `plant.Mapper.at(seconds)` gives a model time. Problem codes `E_TWIN_*`, `W_TWIN_*`.
  `PlantSource(..., max_age=)` and `naga twin run | once --max-age INTERVAL` skip the forecast
  while a source has been silent for longer than the caller allows (none by default). The result
  carries `started`, `cadence`, `model.time_unit`, `model.epoch`, and `finished` and `took` on a
  forecast; the k-th forecast of a cycle runs on `seed + k * replications`; `stop()` is terminal
  and `join()` returns whether no forecast is still running.

- Added: `initial_state` gains `entities[].deadline` (a patience running out), `entities[].member`
  and `resource_sets.<name>.cursor` (a resource set), `blocks.<name>.setup` (a changeover key), and
  `since` at a HOLD, BATCH or MATCH (entry time in the store). `nagasim.state.take` carries them:
  store waits, queues with patience, set members and a Cyclical cursor, and setups are no longer
  refused. Still `E_STATE_UNSUPPORTED`: fleets, chargers, blocking and finite buffers, conveyors,
  agents, a changeover under way. New code `E_INITIAL_SET`.
- Fixed: a state of a run started from a state keeps `since` of each waiting entity.
- Fixed: a resource set no longer sends an arriving entity to queue on one member alone when
  that member's freed unit is owed to its queue.
- Fixed: a state lists waiting entities in the order they entered a queue or a store (a timed
  link reordered them); a waiting entity of a state no longer seizes a unit that is down or off
  shift; a resource set counts its open waits rightly after a warm start.
- Added: schema v29 and `nagasim.dashboard`. The `dashboard` section may declare what a person
  who did not build the model may do with it: `inputs` (an override path as a number in a
  range, a switch or a listed choice), `results`, `limits`, `pages` and `hidden`, each with a
  ceiling (`E_DASHBOARD_DEFINITION`, `E_DASHBOARD_INPUT`). `default_dashboard(layout)` generates
  a definition, `resolve_dashboard` returns the stored one or the generated one, and
  `input_overrides(layout, values)` turns a viewer's values into scenario overrides, refusing
  any value outside its input. No migration step: a model without the new keys is unchanged.
- Fixed: a malformed `dashboard` section is a validation problem, not an exception.
- Added: `nagasim.plant`, a plant as a timeline. A plant mapping (`*.plantmap.json`: `load`,
  `check`, `schema`, `scaffold`) says what each key of a live source asserts about a model;
  `Mapper.feed(observations)` returns trace events for `Timeline`, `replay` maps a recorded log,
  `Watch.step()` a live `Feed`, `log_of` writes the plant log of a recorded run, and
  `state.from_plant(model, mapper.snapshot())` is the state of the plant now, with its clock.
  Commands: `naga plant check | init | mock | replay | watch`. Agent tool: `get_plant_timeline`
  (65 tools). Problem codes `E_PLANTMAP_*`, `E_PLANT_LOG`, `W_PLANT_*`. Not mappable yet:
  vehicles, chargers, conveyors, cycle phases, agents, counters, an entity on a timed link.
- Added: `nagasim.state`, a model's state as a hashed document: `take(layout, at, seed)` from a
  run, `observe(layout, ...)` from a plant through bindings into `initial_state.*`,
  `start(layout, doc)` with coded refusals (`E_STATE_*`), and the commands `naga state take |
  observe | check` and `naga run --state FILE`. `initial_state` gains `clock` (the run's clock
  starts there and its statistics cover the run from there), `arrivals`, per-failure clocks,
  `ends`, `since`, `arrives` and an observed `busy`; a state is validated as a whole
  (`E_INITIAL_BUSY`, `E_INITIAL_QUEUE_CAPACITY`). `SimSession(..., subscribers=)`.
  A run started from a state reproduces the uninterrupted run with a warm-up of that time:
  exactly when nothing random follows, in distribution otherwise. Agents, fleets, conveyors,
  blocking, changeovers, a store's content and an instant with an event due are refused
  (`E_STATE_UNSUPPORTED`), not carried.
- Fixed: a warm-started job in service keeps its unit when the shift is closed at the start; a
  failure that starts in the instant a shift closes is repaired on time.
- Added: live data sources. `rest`, `mqtt` (plain topics and Sparkplug B scalar metrics) and
  `opcua` (the optional extra `nagasim[opcua]`) read over connections only the caller defines:
  `netguard.load_connections`, `--connections FILE` on `naga bind`, `forecast`, `watch` and
  `mcp`. `nagasim.live.Feed` answers "what changed since a cursor" (events, an exact `lost`
  count, a `gap` flag) and `naga watch` prints it. Read-only: no reader can publish, post or
  write. `databind.READERS` is the table of readers; `databind.read_rows` reads one source as
  plain rows.
- Security: a `sql` data source may carry only a few URL parameters (`connect_timeout`,
  `application_name`, `charset`, a verifying `sslmode`); any other is refused
  (`E_DATA_URL_PARAMETER`). A `sqlite` or `sql` query must be one SELECT or WITH statement, with
  no comment before it and nothing after a semicolon (`E_DATA_QUERY`).
- Security: a model file may reference only environment variables whose names start with
  `NAGASIM_DATA_`; any other `${NAME}` is refused (`E_DATA_ENV_NAME`) before the
  environment is read. Rename a variable a model of yours used, for example `MY_DB_URL` to
  `NAGASIM_DATA_DB_URL`.
- Added: the shareable HTML player. `nagasim.player.build_player` writes one self-contained HTML
  file that replays a recorded run in a browser, with no server; `naga run MODEL --player PATH`
  and `naga scenarios MODEL --player PATH` (`experiments.record_scenarios` records the first
  replication of each scenario again with its seed). The file is a constant page plus one escaped
  data block, names no address and carries a Content-Security-Policy that refuses every request.
- Added: `nagasim.core.tape` (`TapeWriter`: a run sampled at a fixed step into drawable state) and
  the view derivations beside `Timeline` in `nagasim.core.playback` (`asset_states`, `unit_counts`,
  `resource_rows`, `kpi_snapshot` and the maps they read); `network.block_place`.
- Added: `Exporter.needs_trace`. Only an exporter that sets it is given `trace=`; the built-in
  `player` exporter does.
- Fixed: `Timeline` no longer counts an entity in a block it has left when a delayed move ends a
  float after the entity's next event; a live `Timeline` answers each chunk's moves once; a
  warm-started entity gets its `entity_created` event, so a trace starts with it.
- Security: failure messages, hints and log lines about a data source now quote a `${NAME}` as
  written in the model file, not the environment variable's value, except a value shorter than
  four characters and a part of a value that a library extracts (the host of a URL held in a
  variable), which can still be shown. A value can still leave as data, through a query, an
  option or a filter: do not run a model file you do not trust with secrets in the environment.
- Added: `Section.tab` and `Section.group` (`nagasim.core.sections`): the heading, and the family
  of headings, a client lists a section's records under.
- Added: `SimSession.advance(until=predicate)` and `nagasim.core.debugger` (stops on a block or
  an expression, watch values, an entity's attributes); the agent tools `debug_run` and
  `debug_inspect` use them.
- Fixed: entity ids belong to the run (`SimulationContext.entity_ids`), so a second run in one
  process no longer restarts the ids of a paused session.
- Fixed: a deeply nested expression is an `ExpressionError` ("expression is nested too deeply"),
  not a `RecursionError` or a `MemoryError`.
- Fixed: the MCP server runs one tool at a time, so batched calls no longer share a workspace
  mid-edit (two `debug_run` calls on one model built two sessions).
- Added: 64 agent tools (`nagasim.agent_tools`), an MCP server (`naga mcp`, extra `mcp`) and an
  assistant loop (`naga assist`, extra `assistant` for Claude) with Anthropic and OpenAI-compatible
  providers; a local server such as Ollama needs no key. The endpoint is set by flags and
  `NAGASIM_LLM_*` variables only. A model's Python behaviour never runs through an agent
  (`E_AGENT_PYTHON_REFUSED`), and only the tools a session offers run (`E_TOOL_NOT_OFFERED`).
- Added: a test matrix (Ubuntu, Windows, macOS x Python 3.11 to 3.14) run on a button and weekly,
  and a release workflow for PyPI.

## 0.5.0

- First version of the engine as its own package.
