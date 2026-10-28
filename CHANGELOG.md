# Changelog

## 0.19.0 (2026-10-28)

0.19.0 adds the building around a model. An optional `building` section holds rooms, walls with a height, thickness and finish, doors that stay on their wall, floor markings, labels and fixtures; a run never reads it, and two new checks flag a path through a wall and a door on no wall. `naga run` now prints a summary with intervals and law-check warnings. Bundled models give the same results as 0.18.0.

Changed:

- `naga run` prints a summary (mean, 95% half-width and n per system KPI), a `WARN` line for each law check the run did not pass and the replications a +/-5% half-width needs; `--raw` keeps the old per-replication frame dump, `--json` prints `{kpis, checks, replications}` and `--no-checks` skips the checks. A run cut short exits 1 with "stopped" on stderr.
- `naga check --json` prints `{problems, laws, results}` for one model.
- `naga run` hides the all-zero cost and log-truncation rows of its summary; `--raw` and `--json` keep every row.
- `naga verify` gives each case its own random numbers; `--blocks K` reports how many independent runs' 95% intervals hold the theory.
- `naga` commands print a model's validation problems with code, path and hint, as `naga validate` does.
- A DECIDE in Condition mode with no true condition and no ELSE link stops the run with the block named, as Chance mode does, instead of stranding the entity with a log warning.
- The player calls a snake_case block id by its words ("get_bay" reads "Get bay") in labels and the bottleneck callout.
- The viewer names its first tab after an author's scenario that changes nothing, and lists that scenario once.
- `ModelDocument.save` writes a temporary file and swaps it in, so a crash mid-write leaves the old file whole.
- `nagasim.viewer.engine_available()` replaces `engine_on_disk`, kept as an alias for one release; `write_engine_zip` writes the engine payload a viewer carries.
- `freeze_data(..., confine_to=FOLDER)` reads file data sources from that folder only, for a viewer of a model someone else wrote.

Added:

- `Results.checks()` runs the law checks (utilisation, offered load, Little's law, flow conservation, censoring, initial bias) once on demand; in a notebook a result shows its warnings above the KPI table. Results themselves are unchanged.
- "Did you mean" suggestions for misspelt block and distribution parameters; `E_ASSIGN_KEY` for an ASSIGN entry key other than `target` and `expr`; literal DISC probabilities and TRIA(low, mode, high) ordering are checked at validation. `ModelValidationError.structured` carries the problems.
- `run_model(logic=...)` and `naga run --logic MODULE` take LOGIC block classes from your own module, with `nagasim.core.custom_logic` as the fallback; this works with `workers > 1`.
- A warm-start state carries belts with stops, diverts and merges at any instant without an event due.
- MCP: a client that sends a progress token receives progress notifications from run_model, run_scenarios, run_to_precision, forecast, optimize, calibrate, fit_surrogate and study_step.
- `nagasim.run_model`, `nagasim.Model` and `nagasim.Results` are importable from the package.
- An optional `building` section, in metres: rooms (rectangle or polygon, walled or not), free-standing `wall_lines`, a wall height, thickness and finish, doors and curtains, floor `markings`, labels and fixtures. It is validated and kept in the file, and a run never reads it, so it cannot change a result. The schema version stays 29.
- A building opening is attached to its wall, `{"wall": {"room": NAME, "edge": K} or {"line": INDEX, "edge": K}, "at": 0..1, "width": M}`, so it follows the wall when the wall moves; the older two-point form (`a`, `b`) is still read.
- Floor markings: a line or closed area in a RAL signal colour (by name) or `#RRGGBB`, solid, dashed or hatched, with a width and an optional label.
- `nagasim.core.building` derives the walls once for every drawing program: `wall_runs`, `wall_pieces`, `wall_crossings`, `opening_ends` and `attach_opening`.
- Problem codes `E_BUILDING` (a malformed building section), `W_WALL_CROSSING` (a path segment crosses a wall where it has no opening) and `W_OPENING_OFF_WALL` (an opening that cuts no wall).
- A `presentation` capability (`nagasim.core.sections.CAPABILITIES`) owns the `building` section.
- `E_ASSET_UNKNOWN_NAME`: a library check places one instance of each asset and names an unknown name in a definition's expression, with the closest parameter.
- Player: `pictures={"background": {...}}` draws a floor plan (SVG or PNG, JPEG, WebP data URI) under the layout view. On a plan the view shows the plan, people and fleets at twice their size, and "Show model blocks" brings back the wiring; the viewer gives a plan the page's width and carries the author's pictures, so a run made in the browser draws them too.
- Playback: fleet seize and release events carry the unit, and `Timeline.state_at` gains `carriers`, which entity holds each fleet unit.
- `nagasim.experiments.headline_keys(means)` picks the few KPIs that say what a run gave.
- A bundled model, `ed_bedside_scan`: a small emergency department with two pathways, two scenarios and a building.

Fixed:

- A state now carries when the last part left a belt's end, so a warm start after a recent exit matches the uninterrupted run.
- A LOGIC class that does not exist makes `naga run` exit 2 with the class named, instead of a traceback.
- `naga --help` answers in well under 0.1 s, and `import nagasim.api` no longer loads openpyxl.
- `InverseGaussian(mu, lambda)` has mean `mu`; it had mean `mu * lambda`. Input fitting writes `mu` to match, so a fit still reproduces its data. A model that uses it draws new values; the bundled models do not.
- A CREATE with a FixedSchedule arrives at its listed times only; it no longer makes an extra entity at 0 or at `start_time`.
- Auxiliaries in a model with no level, flow, conversion or delay advance with the clock, so `TNOW` and `RAMP` are no longer read once at 0.
- The cpsat planner keeps a multi-entity order's entities back to back at a single-unit station, as the replay of its one planned start does.

## 0.18.0 (2026-10-26)

0.18.0 widens what a model can say about real logistics and energy sites. Trucks unload and load as carriers, AGVs can be kept across legs, belts divert and merge, sorters take up to 200 lanes, EVs charge per vehicle under a site cap, storage can be dispatched by an optimiser, and robots share named safety zones. Bundled models give the same results as 0.17.0.

- Trucks are carriers: SEPARATE `carrier: keep` releases members one at a time (FIFO or LIFO) while the carrier leaves on its own link, and BATCH `carrier_type` loads members onto an arriving carrier up to its own capacity or a cut-off. Logistics 3.0.0: the inbound dock unloads pallet by pallet and the truck can leave by `trucks_out`; empty trucks are loaded at a new Truck loading dock; door times are statistics.
- TRANSPORT `keep_vehicle` keeps a vehicle for the same part across several legs (an AGV that waits at a machine and carries the part on), freed by a RELEASE naming the fleet or when the part leaves; `fleet_summary` gains `Held %` (all zero in models that do not use it).
- Conveyor `diverts` (a part leaves the belt at a point onto its own link, waiting or riding on when the target is full) and `merges` (a side stream joins at a point on a gap), with `conveyor_diverts` and `conveyor_merges` frames and `divert/` and `merge/` KPI keys. Conveyor stops get `stop/<belt>/<index>/<column>` KPI keys, and `W_CONVEYOR_STOP_HELD` flags a stop that seizes a resource the part still holds. Fixed: a zero-time belt stop no longer slows a free-flowing belt.
- Logistics 3.1.0: Sortation with 1 to 200 lanes and an optional reject chute (`max_recirculations`); a submodel definition can repeat a block group by a parameter (`repeat`), each copy with its own port.
- A new CHARGE block and the Energy asset "EV charging (per vehicle)" (Energy 1.6.0): each vehicle charges at its own and its charger's power under a shared site cap, so a small site's peak demand is right.
- Optimal storage dispatch: a `dispatch` section plans storage on a rolling-horizon linear program over the site's tariffs and profiles, with a perfect (`model`) or naive (`persistence`) forecast and a fallback to the battery's own rule; Energy 1.7.0 adds the battery's `optimal` mode and a `dispatch_summary` frame.
- Safety zones: a `zones` section that robots and cobots share by name, measured along the paths or in a straight line. Automation library 2.0.0: the Safety zone owns its radius.
- Supply chain library 1.3.0: the Warehouse can draw from, report on and refill another asset's stock; its default is unchanged.
- A seize of more than one unit, from a resource set or a plain resource, now gets all its units at once or none: a waiting seize holds nothing, so utilisation, busy cost and shift or failure timing are exact. `W_SET_PARTIAL_HOLD` is removed. Fixed: a crash when preempting a multi-unit seize.
- A Condition HOLD whose `LEVEL` name is computed now wakes at every level change, not only on its scan.
- Network: an `http(s)` data source connects to the very address that was vetted, at every redirect hop. An https source works behind a proxy the caller set (`HTTPS_PROXY`/`NO_PROXY` or the system settings) as a tunnel, with the host checked first and the certificate verified by name; plain http through a proxy is refused. Caller files (`--refit`, `--setpoints`, `--connections`, outbox, audit, `--asked`, twin results) are read under one rule: a local regular file, no link, a size cap.
- `--asked FILE` on `naga run --player`, `naga scenarios --player` and `naga viewer` quotes recorded input-uncertainty and ABC answers on the trust card.
- The plant monitor takes a setpoints file: a setpoint recommendation shows the range, step and interval it would be written with, and approving it writes the reply for `naga plc-write`. An opt-in OpenPLC smoke test covers `plc-write` against a real soft PLC.
- `--export "usd:PATH?fps=..&start=..&end=..&speed=.."`, and the USD replay draws asset state lamps and fill bars.
- `nagasim.modeldiff.record_changes(a, b)`: record-level changes (section, key, label, kind, old, new) on `naga diff`'s identity rules.

## 0.17.0 (2026-10-26)

0.17.0 helps a model stay readable and lets a picture follow a run in more detail. A block or instance name over 20 characters now draws a warning, the playback state counts departures per exit and what ended in each sink, library assets can carry a small line icon, and auto-layout spaces blocks by their real width. The bundled models use shorter names, which changes results in four of them (see below).

- `W_LONG_NAME` (warning): a block name, or a library instance name, over 20 characters. It checks instances nested in a submodel definition too, once per name.
- Playback: `Timeline.state_at` gains `departures` (moves out of each block per outgoing branch), `departed` (the same moves summed per block) and `disposed` (what ended in each sink), the data behind per-exit counters.
- Blocks accept `counter_offsets`, a dict that moves a counter beside an exit; the builder stubs carry it.
- Auto-layout spaces blocks by their drawn width, so long names no longer overlap their neighbours.
- Library assets take an optional `graphics.icon`, an SVG line icon under 2000 bytes that passes the same safety check as state graphics. The 89 bundled assets (airport, container terminals, energy, biogas, supply chain, system dynamics, agents, manufacturing, demo, automation, logistics, services) each ship one.
- Bundled names are shorter: the `clinic_library` model and the three agent-based models that had names over 20 characters now use shorter ones. Their random draws follow the names, so each draws a new sample that is statistically equal to the old one. Every other bundled model gives identical results on two seed sets.

## 0.16.0 (2026-10-23)

0.16.0 makes a result say how far to trust it and lets the twin act on what it sees. Input data now carries its own uncertainty, a twin raises alerts on sustained shifts and proposes a refit for you to approve, and a recommended setpoint can be written to a soft PLC on request. Conveyors gain stops, models gain entity-path checks, and reinforcement learning, ABC calibration, a paced clock and a SimPy log helper join the toolbox. Results keep their values except for one case: an accumulating conveyor whose end blocks.

- Input uncertainty: `naga uncertainty MODEL --data PATH=FILE[:COLUMN]` and the tool `input_uncertainty` bootstrap each input's data, refit it in its best-fit family (arrival tables by whole cycles) and rerun the model on common random numbers. Each KPI gets its run interval, its input interval and the input's share of the spread. `what_if` quotes the input spread as `input_note` and never runs one itself.
- Twin: a two-sided CUSUM raises an alert when a series shifts and stays shifted (`--alert-k`, `--alert-h`; `W_TWIN_ALERT`). After an alert, `naga twin --refit FILE` (needs `--outbox`) calibrates the factors you name and proposes the candidate only when it beats the current model's pinball loss; an approved reply adopts an overrides layer in `twin_history.json`, and the model file is never written.
- Advisory setpoint writes: a `setpoint` candidate names a tag of your `--setpoints` file, and `naga plc-write` (its own process, the only writer) sends an approved one to a soft PLC over Modbus TCP (function codes 6 and 16). It is a dry run unless `--write`, checks range, step and interval, reads the value back and audits before and after. Loopback only.
- Conveyor `stops` (`{at, time, resource?}`): an accumulating belt packs against a stop and caps each section at the gap divided by the spacing; a non-accumulating belt stops whole. Adds the `conveyor_stops` frame, `E_CONVEYOR_STOP` and a `belt_stage` trace event per stage change. `W_SET_PARTIAL_HOLD` warns on a set seize of quantity above 1.
- Entity-path checks: the optional section `path_checks` (`order`, `never`, `at_most`, `always`, optional entity type) is judged on one traced replication, and `naga check` names the entity and the time. `naga discover` writes up to four of each from an event log.
- `calibrate(method="abc")` and `naga calibrate --method abc --accept`: rejection ABC over the factors' boxes keeps the nearest 5% (at least 20) and reports each factor's posterior median and 90% interval; the budget is a hard cap.
- Reinforcement learning: `SimEnv.as_gymnasium_env()` gives a gymnasium environment that passes `check_env`; `others=` drives the other Policy DECIDEs (shortest queue by default) so one policy trains at a time; data-only JSON policies (table, linear) run with `naga policy-eval --policy-file`. The recipe for Stable-Baselines3 is in the docs.
- Paced clock: `run_model(pace=FACTOR)` and `naga run --pace FACTOR` make a model second take 1/FACTOR wall seconds; late events run at once and are counted in `Results.pace`. One worker only.
- `nagasim.simpylog.EventLog`, a stdlib helper that writes the CSV `naga discover` reads from a plain SimPy script; the new page "From a SimPy script" shows how. Naga never runs the script.
- What next: study answers carry the top three ranked next options (fix, then missing, improve, extend) from recorded facts, `get_model_summary` carries them, and the control room has a What next? button. An allow-listed hosted model can add up to three checked ideas labelled "assistant"; they are never run automatically.
- `explain_kpi` and `naga explain MODEL KPI [--set PATH=VALUE]` rank what drives a KPI with 95% intervals (level mode) or what a change does (paired differences, with one suggested `what_if` test that is never run). `optimize`, `calibrate` and `fit_surrogate` answers carry operational-law warnings (`laws`) from one extra batch at the best point; `fit_surrogate(confirm_top=N)` confirms the surface's best suggestions with KN selection.
- Trust card: the player and the viewer show run noise, input data and parameters as three rows with their bases ("not measured" when absent); the control room folds the same panel and the study deck has a "Validation and precision" slide. `naga study deck FOLDER` prints the deck to `slides.pdf` with a local Chrome or Edge.
- Client pages: the player and viewer follow the editor's theme (light and slate-blue dark), with KPI cards, a bottleneck callout, "What happened when" lanes, results as interval bars with a word, scenario tabs, levers with notes and an answer card. New optional dashboard fields `question`, `target` and `note`. The player plays in real model time, from 1x to 1 year per second.
- Results change in one case: an accumulating belt whose end blocks now lets a part reach the end no sooner than spacing divided by speed after the one ahead left. In the bundled `warehouse_forklifts`, the belt's Blocked % falls by 0.01 to 0.03 points and throughput is unchanged. Every other bundled model gives identical results on two seed sets.

## 0.15.0 (2026-10-22)

0.15.0 makes a run easier to trust. Every run now checks itself against the operational laws of queues, `naga facts` lists what happened in a run that nothing else reports, `naga verify` checks the engine against textbook queues, and `what_if` answers a change with a clear verdict. Reality checks gain `eventually` and time windows, and an agent can now see the model as a picture. Results keep their values; the system frame gains one measured `Avg WIP` row.

- Every run checks itself against the operational laws (`nagasim/laws.py`): utilisation, offered load, Little's law per resource and for the system, flow conservation, censoring (over 5% of the entities still inside at the end) and initial bias (no warm-up while MSER-5 would move the mean by over 5%). They are warnings that name the law (`W_LAW`), skipped wherever an input cannot be computed, shown in the `run_model` digest, the study steps and `naga check`.
- The system frame gains `Avg WIP`, the time-weighted count of entities in the system; `Avg WIP (Little)` keeps its values and is a lower bound when work is still inside at the end. `resource_summary` gains `Queue Length at End`.
- `naga verify` runs nine textbook queueing cases against their theory (a pass is within the larger of three confidence half-widths and 2%); `--quick` takes about 20 s, and `--html` writes a report.
- Agent tools: `what_if` answers "what if I change this" on paired common random numbers, doubling replications until each KPI's difference is clear or a cap is reached, and calls it better or worse only when told the goal's direction; `check_draft` compares a generated model with its source's own figures (an event log or tables) and never calls it valid. `run_scenarios` rows gain `Replications Needed` and `CRN Ratio`.
- `naga facts MODEL` runs one replication and lists what happened that nothing else reports, as facts, never verdicts: blocks no entity entered, resources never seized, resources with a unit doing no work (free, or held by an entity waiting for another resource) while entities waited for them, and entities left at the end ("never empty since t"). `--json` for scripts; exit 0 whenever the run happened. The same report is the read-only agent tool `run_facts`.
- Reality checks: `when` also accepts `before <t>`, `between <t1> and <t2>` and `eventually`, alone or with one of those windows: an eventually check passes when the constraint holds at least once in the window, and says so when it already held at the window's start. The `run_reality_checks` tool answers each check's note.
- `view_model(model, format)`, a read-only agent tool, draws the model's flowchart as a PNG image (SVG or text on request), so an agent can see what it built.
- Optimisation: `method="optuna-gp"` and `"optuna-nsga2"` (a Pareto set of two or more objectives), with the `ml` extra; single-objective top candidates are confirmed under noise by KN selection, and GP falls back to multivariate TPE without torch.
- A Modbus TCP reader for the twin: read-only (function codes 1 to 4), stdlib, with the host, unit and register map only in the caller's connection file.
- `calc_kpis` is about three times faster on short replications, with identical results.
- A physics-simulated test plant for the twin: five minutes of an Open Industry Project box belt (MIT), recorded headlessly, with a model and a plant map that replay it (`tests/data/oip_belt_line`, `tools/oip_trace_to_log.py`). The digital-twin page has a section on it, with an OPC UA recipe for Open Industry Project's desktop app that has not been run yet.
- Fixed: undo and redo inside an open transaction (an assistant turn) popped the edit made before it; `debug_inspect` counted a seize of several units as one busy unit; a twin whose plant map has counters only scored its WIP forecasts against a WIP of 0 it was never told.

## 0.14.0 (2026-10-21)

0.14.0 adds an offline backup for the Haiku tool picker. When Haiku cannot answer (no key, no network, an error), a local cross-encoder ranks every tool, so `find_tool` still puts the right one first. The cross-encoder also loads in the precision that suits the hardware.

- `naga mcp --tool-picker haiku --tool-picker-backup PATH` (optional, off by default): PATH is a local folder or a Hugging Face id of a CrossEncoder model (the `picker` extra). It is used when Haiku cannot answer and ranks every tool the server exposes, not only `find_tool`'s ten best; with no API key it runs alone.
- A cross-encoder loads in float16 on a CUDA GPU and in float32 on a CPU: the same top pick on 549 test rows, about three times faster on a GPU, and six times faster than float16 would be on a CPU.
- The page Pick tools with a small model quotes the new evidence over all 90 tools (Haiku 190 of 274, a gte backup trained from Opus picks 183).

## 0.13.0 (2026-10-20)

0.13.0 lets an assistant run a whole simulation study through the engine: a study folder with the question, goals, assumptions, checks and every model version; a live control room in the browser with buttons that act and an assistant of its own; and the deliverables on request. An assistant finds the right agent tool with `find_tool`, optionally helped by a local or a hosted tool picker. The `mcp` extra now needs `mcp>=1.19`.

- Block types and distributions have a full plain-language summary: `list_block_types` gives a whole first sentence and `list_distributions` entries gain `summary` (what it models and a typical use), so an assistant or a small model picks the right one more often. The NegativeBinomial description now says it counts failures before the r-th success.
- The control room's own assistant shows its answers' Markdown, reads them aloud in the answer's language (or stays silent when no such voice is installed), sends on Enter, never pastes the room's link, and marks a call that failed in its panel; on Windows it also finds an `ANTHROPIC_API_KEY` stored for the user with `setx`. With a tool picker on, `naga mcp` answers its client at once and loads the picker in the background.
- `naga mcp --tool-picker haiku` (optional, off by default): hosted Claude Haiku chooses among every tool the server exposes and its three picks lead `find_tool`'s answer (22 of 25 first on the frozen blind set, about 0.9 s). Needs the `assistant` extra and `ANTHROPIC_API_KEY`; each query and the tool catalogue go to Anthropic, and any failure keeps `find_tool`'s own order.
- `naga mcp --tool-picker gte` (optional, off by default): a small re-ranking model (gte-reranker-modernbert-base, the `picker` extra) reorders `find_tool`'s ten best candidates, and the answer says to try the first three in order. On the frozen blind set the right tool comes first for 14 of 25 requests (8 without it), in about 0.1 s on a laptop GPU; the first use downloads the weights from Hugging Face. New page: Pick tools with a small model, with the comparison and a link to the System-1 AI Gateway for other models.
- AI-run studies have a live control room: `naga room <study folder>`, or `naga mcp --room` inside the assistant's own server, serves it on 127.0.0.1 with a random token; it updates itself while the chat works and has buttons that act (this answers my request, not quite, next iteration, approve plan, show flowchart, revert, run sensitivity, and make or open each deliverable) and its own assistant (study tools only, Stop, optional voice input; an Anthropic API key or a local model). Every study answer carries `page_url` and, while a room runs, `room_url`; in MCP Apps chats the page shows after every study tool. A second naga process that tries to change an open study is refused (`E_STUDY_BUSY`).
- `find_tool(query)`: a deterministic search over the agent tools (names, descriptions, argument docs, a small synonym table) so an assistant finds the right tool instead of guessing a name; an unknown tool name now answers close typos plus search hits, and on a server with a tool subset both only name tools that server exposes. Measured on blind queries written from the tool names only: the right tool in the top 3 for 14 of 25.
- AI-run studies, on request: a sensitivity analysis (`study_sensitivity`), verified candidate modules (`study_extensions`: shifts, failures, an arrival peak), a GIF of the model running (`study_animation`), a paper-style write-up (`study_writeup`) and a slide deck (`study_slides`), all from the study folder; the precision pass now adds replications to the first pass instead of rerunning them; the in-chat control room has expand all and collapse all.
- AI-run studies, after the first build: replications come from a precision target (+-10%, 5% or 2% at 95% by study size, capped) and every step states the count and the precision reached; each version makes only the light output, with a version's full report (`study_report`), the PNG flowchart and the package on request; the control room fits one screen, with goal chips and collapsible panels; the study tools accept common near-miss input and say what they changed, unknown tool and argument names get "did you mean", the plan and a step take one call each; tail goals ("at most 5% wait over 15 min") and a queue-formula validation (`study_validate`) are computed by the engine; `naga study rerun FOLDER` checks a study reproduces.
- Study tools: the scenarios in the model run with every `study_step` (same replications and seed, capped by the study size) and come back judged against the goals, per scenario; the control room shows them. The study guide says how to write them and when a shift study differs from a steady-state one.
- Study tools, after an assistant ran a whole study through them: `next_step` names the real `study_update(changes={...})` call; a validation entry needs `level` and `passed: true|false` and is refused otherwise, with an example; an unmet goal no longer fails verification; goals on statistics the model records are counted on the cover; `study_step` reports the replications it used, which the study size sets.
- AI-run studies have a study guide for assistants: an Agent Skills folder (protocol, an interview in rounds adapted from mattpocock/skills, the study steps with their sources) that `naga skill` shows or copies and the MCP server offers as the `run_simulation_study` and `study_interview` prompts.
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
