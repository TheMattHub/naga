# Changelog

## Unreleased

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
