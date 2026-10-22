# Naga

Naga is a simulation engine for Python, built on [SimPy](https://simpy.readthedocs.io/): you
describe a system as a model file, and it runs the model and gives back an answer with its
confidence interval. Python 3.11 or later; the `naga` command comes with it.

```console
pip install nagasim
```

Ten lines build the textbook single-server queue, run it twenty times and set the result beside
the exact answer:

```python
--8<-- "docs/snippets/landing-example.py"
```

```text
--8<-- "docs/snippets/landing-output.txt"
```

The simulated interval brackets the exact value, which is what a trustworthy simulator does and
what the [reference models](validation/reference-models.md) page checks against known answers. It also shows the
point of Naga: the same clock runs discrete-event processes, system dynamics, agents and
continuous levels with exact crossing times, so one model can hold all four.

![What goes into the engine, what it does, and what comes out](img/overview.svg)

<div class="grid cards" markdown>

-   **Python modeller**

    ---

    Build a model in code, run replications, read the results.

    [Learning path](learn/index.md), [Quick start](quickstart.md) and the [Python API](python-api.md)

-   **Planner or analyst**

    ---

    See what a finished study looks like, then compare scenarios of your own.

    [Worked examples](worked-examples/airport-ground-support.md), [Experiments](experiments.md) and [reading results](learn/6-results.md)

-   **Integrator**

    ---

    Run it from a pipeline, check files in CI, build on the formats.

    [Command line](reference/cli/index.md), [model file keys](reference/schema.md) and [MCP](mcp.md)

-   **AI agent**

    ---

    Read the site as plain text, validate against the schema, call the tools.

    [llms.txt](llms.txt), [llms-full.txt](https://thematthub.github.io/naga/llms-full.txt) (the whole site as one file), a Markdown copy of every page (its address with `.md`), the [JSON Schema](schema/model-v29.json) and the [agent tools](reference/tools.md)

</div>

## What you can model

- **Processes and queues**: blocks (create, process, seize and release, batch, match, decide,
  route, transport, ...) joined by links, with resources, sets, schedules, failures, costs,
  expressions and submodels.
- **Material handling**: path networks, vehicle fleets with batteries and chargers, conveyors,
  motion cycles, machines tended by mobile robots.
- **Stocks and flows** (system dynamics: auxiliaries, delays, subscripts, units, XMILE and Vensim
  import) and **agents** (statecharts, messages, space, networks), in the same model as the
  processes.
- **Rates**: tanks, batteries and other quantities that fill and drain continuously, with exact
  crossing times.

## What it does around a run

- **Statistics you can defend**: independent replications, Student-t intervals, warm-up
  handling, reproducible random streams, checked against models with a known answer.
- **Experiments**: named scenarios with paired comparisons, design of experiments,
  ranking-and-selection, optimisation, calibration and an investment case.
- **A twin of a plant**: readers for files, SQL, REST, MQTT and OPC UA, a run that starts from
  the plant's state, forecasts scored against what then happened, and actions compared on the
  forecast. It reads a plant and never writes to one.
- **Outputs**: pandas frames, Excel, CSV, JSON, a self-contained HTML report, a recorded run to
  replay, and a viewer in which the engine runs in a reader's browser.

## Three ways in

- **The command line**: `naga run single_server --report report.html` runs a bundled model and
  writes its report; `naga --help` lists every command.
- **Python**: `nagasim.api.run_model` runs a file and returns pandas frames, and
  `nagasim.build` writes a model in code, with your editor completing every parameter. See the
  [Quick start](quickstart.md) and the [Python API](python-api.md).
- **An agent**: an MCP server offers the same operations as tools, so an assistant can build,
  validate, run and explain a model. See [Use with an agent](mcp.md).

Whichever way you use, the model is the same JSON file, so what one person builds in Python an
agent can check and a colleague can open.

## What's new in 0.15

--8<-- "docs/whats-new.md:latest"

All releases: [What's new](whats-new.md) for the highlights, [Release notes](changelog.md) for
every change.

## What it is not

- **Not an editor.** There is no window, no canvas and no animation here. `naga run --player`
  writes one HTML file that replays a run in a browser, and `naga viewer` one in which a reader
  re-runs the model; those are the only pictures.
- **Not SimPy with helpers.** You do not write generator functions. A model is data, even when
  Python code builds it, and what a block can do is fixed by its type and its expressions. When the logic you need is not a block,
  raw SimPy is the better tool: see [Coming from SimPy](coming-from/simpy.md).
- **Not stable yet.** Before 1.0 the Python API may change between minor versions. Model files
  always migrate forward, and agent tool names and problem codes are permanent.

## Where to go

| You want to | Read |
|---|---|
| run something in a minute | [Quick start](quickstart.md) |
| see what a model looks like and what it gives | [Gallery](gallery/index.md) |
| check the engine against models with a known answer | [Reference models](validation/reference-models.md) |
| map what you know onto this | Coming from [SimPy](coming-from/simpy.md), [Arena](coming-from/arena.md), [AnyLogic](coming-from/anylogic.md), [Vensim](coming-from/vensim.md), [NetLogo](coming-from/netlogo.md) |
| write a model file | [Model file format](model-file-format.md), [Blocks](reference/blocks.md) |
| know what a part of a model does when it runs | [Modelling guide](modelling-guide/index.md) |
| compare, search and check models | [Experiments](experiments.md), [Model assertions](model-assertions.md), [Semantic diff](semantic-diff.md) |
| call it from a script or a notebook | [Python API](python-api.md) |
| let an agent build and run models | [Use with an agent](mcp.md) |
| give someone a model they can run in a browser | [Sharing a model](sharing-a-model.md) |
| follow a plant and forecast from its state | [Digital twin](digital-twin.md) |
| know what a model file can and cannot do to your machine | [Security](security.md) |

An LLM agent reading this site can start from [`llms.txt`](llms.txt).

Apache License 2.0. Source and issues: [github.com/TheMattHub/naga](https://github.com/TheMattHub/naga).
