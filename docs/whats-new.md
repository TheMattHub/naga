# What's new

This page gives the major changes of each minor release, newest first: what you can do now
that you could not before, and what changes when you upgrade. Small fixes and details are left
out on purpose. Every change, fixes included, is in the [release notes](changelog.md).

## 0.12: one source of truth for a model's fields

<!-- --8<-- [start:latest] -->
- **Schema and engine agree.** The published JSON schema is generated from the same field table
  the engine validates and flattens with, so an editor or an agent checking a model against the
  schema sees what `naga validate` sees.

```console
naga validate berth_shore_power
```

- **Out-of-range numbers caught at validation.** A number outside its field's bounds, such as a
  zero fleet speed or a negative level inflow, is reported when the model is checked instead of
  failing or crashing the run.
- **Expanded assets save cleanly.** A library asset expanded in place saves a model that
  validates against the schema, with its variables in the same order every time.
- **Before you upgrade:** a model with a number outside its field's bounds is now refused;
  `naga state observe` is removed (a plant's state comes from `naga twin`); and the Python names
  `costs.COST_KINDS`, `SimSession.take_events`, `ModelDocument.set_description_text` and
  `api.run_replication` are gone.
<!-- --8<-- [end:latest] -->

## 0.11: shore power at a berth

- **Shore power at a berth.** A new bundled model feeds ships' hotel loads from the grid up to
  the shore connection and leaves the rest on their auxiliary engines, priced and emitting as
  marine fuel. Four like-for-like scenarios (no shore power, 2 MW, 6 MW, every ship ready) end in
  an investment case that says who pays (the port) and who saves (the shipowners).

```console
naga run berth_shore_power
```

- **Misspelt names caught earlier.** A name passed into a submodel through a parameter, such as
  `POWER(source)` or `TS(profile)`, is now checked when the model is validated.
- **Before you upgrade:** a model whose submodel parameter names an equipment, series or level
  that does not exist is now refused at validation; it failed when run before.

## 0.10: from an event log to a model, energy on every fleet

- **Draft a model from an event log.** `naga discover` reads a CSV or XES log and writes a model
  with arrivals, activity times, routing and resources, and a report that says how far to trust
  it. `--check` tells you first whether the log is good enough. See
  [Event log to model](event-log-to-model.md).

```console
naga discover job_shop_log --check
naga discover job_shop_log -o draft.json --idle-waits
```

- **Energy on every fleet.** `POWER("name")` reads the kW a resource, fleet or charger draws, so an
  energy flow can price, peak-bill and emit equipment power, and any vehicle, worker or gantry
  fleet can carry a battery. Four new bundled models show it on a container terminal and an
  airport apron.
- **A debugger that steps back.** An agent's `debug_run` stops on a condition or one entity,
  steps back exactly to the previous point and steps inside one submodel instance.
- **Before you upgrade:** where a flow's `rate_time_unit` differs from the model's time unit, its
  rates, thresholds and demand charge are now read per the flow's own unit, so those numbers
  change (validation warns with `W_RATE_UNIT_CHANGED`), and flows sharing a tag in two units are
  refused. A battery fleet with an idle draw now charges while idle.

## 0.9: a twin that follows the plant's orders

- **The plant's orders in a forecast.** A plant mapping's `order` signal reads one row per order
  (product, quantity, due date, priority, release, status) from any MES or ERP, and links each job
  on the floor to its order. A forecast then releases the plant's own orders, each once, and never
  one that is held. See [Plant timeline](plant-timeline.md).
- **Advice on orders.** A twin compares `hold`, `release`, `expedite` and `resequence` on the
  forecast, as it does `reroute`, and refuses by name a verb the model cannot answer, for example
  `expedite` where nothing in the model reads a job's priority.
- **Live dashboards.** A model's `dashboard` section can describe a Live page (status, the
  plant's view, the forecast with its accuracy, and the recommendations) for a viewer that reads
  the results of a separately started twin.
- **Before you upgrade:** a state now refuses a key the engine does not know, instead of ignoring
  it. A model file that stores a Live page, or a state that carries an order book, needs 0.9:
  0.8 refuses the first and silently ignores the book in the second.

## 0.8: write a model without an editor

- **Build a model in Python.** `nagasim.build` has one method per block and one constructor per
  distribution, and your editor completes every built-in parameter and unit and flags a
  misspelt one before the model runs:

```python
from nagasim import build as nb

m = nb.Model(time_limit=1000, warmup=100, replications=5)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()
print(m.run().kpis().head())
```

- **Turn any model into that code.** `naga export --python model.json` writes the Python that
  builds it again, the bundled models included, so you can start from an example.
- **Short, readable model files.** `naga fmt` rewrites a file in its shortest form (defaults
  left out) without changing what it does; the bundled models ship that way. Add
  `"$schema": "https://thematthub.github.io/naga/schema/model-v29.json"` and an editor completes
  the keys of a JSON model too.
- **A twin from plant data on more lines.** A state read from a live plant now covers conveyors,
  batches, transfers and robot and cobot cycles, so a twin forecasts the warehouse and
  automation models from what the plant reports; every bundled model a state carries now
  restarts from it exactly.
- **Before you upgrade:** a block parameter left out of a model file now takes its published
  default, and a deterministic Schedule arrival no longer depends on the seed. A file that left
  parameters out may give other results, and a state saved for it must be taken again.

## 0.7: a twin that starts where the plant is

- **Restart a run from any moment.** A state taken from a run carries finite buffers, fleets and
  vehicles, conveyors and formed batches, so a twin starts from where the line stands.
- **An agent writes a whole model in one call** (`write_model`). In the LLM case study this made
  Claude Code more often right through Naga than writing SimPy.
- **Act on a forecast.** A twin recommends actions, `reroute` among them, and can publish its
  advice to an MQTT topic of yours.

## 0.6: the first public release

- **A simulation engine built on SimPy:** a model is a data file of blocks, resources and
  expressions, run from the command line, from Python or by an agent over MCP.
- **Live data and a twin:** readers for files, SQL, REST, MQTT and OPC UA; a twin cycle that
  follows a plant, forecasts, scores each forecast and compares candidate actions.
- **Share a model:** `--player` writes a recorded run as one HTML file; `naga viewer` writes one
  in which the engine runs in the browser.
