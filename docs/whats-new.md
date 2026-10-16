# What's new

This page gives the major changes of each minor release, newest first: what you can do now
that you could not before, and what changes when you upgrade. Small fixes and details are left
out on purpose. Every change, fixes included, is in the [release notes](changelog.md).

## 0.9: a twin that follows the plant's orders

<!-- --8<-- [start:latest] -->
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
<!-- --8<-- [end:latest] -->

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
