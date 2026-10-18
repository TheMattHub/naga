<p align="center"><img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/banner.svg" alt="Naga: a simulation engine on SimPy" width="820"></p>

<p align="center">
<a href="https://github.com/TheMattHub/naga/actions/workflows/tests.yml"><img src="https://github.com/TheMattHub/naga/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
<a href="https://pypi.org/project/nagasim/"><img src="https://img.shields.io/pypi/v/nagasim" alt="PyPI"></a>
<a href="https://pypi.org/project/nagasim/"><img src="https://img.shields.io/pypi/pyversions/nagasim" alt="Python versions"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-blue" alt="Apache-2.0"></a>
</p>

# Naga

Naga is a simulation engine for Python, built on [SimPy](https://simpy.readthedocs.io/), for
people who have a question about how a system behaves and want an answer they can show their
work for. You describe the system once, as a data file of blocks, resources and expressions
(by hand, from Python code or through an agent), and Naga runs it as a discrete-event, system-dynamics or agent-based model, or as all three on
one clock. Around each run it puts what turns a single run into evidence: independent
replications with confidence intervals, scenarios compared on the same random numbers, designed
experiments, optimisation and calibration. The same model can also be connected to a plant, so
that it starts from what is happening now, forecasts what comes next, checks those forecasts
against what then happened, and proposes what to change. Because the model stays a file that
can be read, compared and shared, whichever way you drive it, the answer it gives is one a team
can inspect and repeat rather than take on trust.

```console
pip install nagasim
naga run single_server --replications 5 --report report.html
```

Documentation: <https://thematthub.github.io/naga/>

## How it fits together

<p align="center"><img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/overview.svg" alt="What goes into the engine, what it does, and what comes out" width="900"></p>

The picture reads from left to right. On the left is what you bring: a model file, the data
it draws on and, if you have one, a plant to follow. In the middle the engine runs the model on
a single clock, whichever of the three ways of modelling it uses, and wraps the run in
statistics, experiments or a twin. On the right is what comes back: results and a report,
pages you can send to someone else, and a twin's forecasts and proposals. Along the top are the
three ways in, the command line, Python and an agent, and because a model is a data file and
not a program, the same file gives the same answer through each of them.

## What it does

- **Discrete-event models** from blocks (create, process, seize/release, batch, match, decide,
  route, transport, ...) with resources, sets, schedules, failures, costs, expressions and
  submodels.
- **Material handling**: path networks, vehicle fleets with batteries and chargers, conveyors,
  motion cycles, machine tending.
- **System dynamics** (stocks, flows, auxiliaries, delays, subscripts, units, XMILE and Vensim
  import) and **agent-based** models (statecharts, messages, space, networks), in the same model
  and on the same clock as the discrete-event part.
- **Statistics you can defend**: independent replications, Student-t intervals, warm-up handling,
  reproducible random streams, validated against queueing theory.
- **Experiments**: named scenarios with paired comparisons, design of experiments,
  ranking-and-selection, optimisation, calibration, forecasting, an investment case.
- **A twin of a plant** (`naga twin`): live readers for files, SQL, REST, MQTT and OPC UA, a run
  that starts from the observed state, forecasts scored against what happened, and candidate
  actions compared on the forecast. It reads a plant and never writes to one.
- **Model files as data**: a versioned JSON schema published for editor completion, a short
  canonical form (`naga fmt`), validation with stable problem codes, semantic diff and
  three-way merge, assertions (`naga check`) with a CI template.
- **A Python builder** (`nagasim.build`, provisional until 1.0) that writes the same file, and
  `naga export --python` to turn any model into that code.
- **Outputs**: pandas frames, Excel, CSV, JSON, a self-contained HTML report, a recorded run to
  replay, and a viewer in which the engine itself runs in a browser.
- **Agent tools**: 71 tools over MCP to build, validate, run, debug and analyse a model.

## Quick start

From the command line, with the bundled example models (`naga examples` lists them; a command
that takes a model file also takes one of their names):

```console
naga examples
naga run single_server --replications 5 --report report.html
naga scenarios automation_cell
naga validate single_server
naga --help
```

From Python:

```python
from nagasim import api, examples

results = api.run_model(examples.path("single_server"), replications=5, seed=0)
print(results["system"])
```

`results` is a dictionary of pandas frames (system, resources, queues, entities, costs and more).

## Write a model

A model is a JSON file, and you can write it in Python. `nagasim.build` has one method per block
and one constructor per distribution, and the package ships type hints, so your editor completes
every parameter and flags a misspelt one before the model runs. The code below is a single
server with random arrivals, run for five replications:

```python
from nagasim import build as nb

m = nb.Model(time_limit=1000, warmup=100, replications=5)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()
print(m.run().kpis().head())
```

`m.save("mm1.json")` writes the same model as the short file below, which you can also write by
hand; the `$schema` line gives completion in an editor that reads JSON Schema:

```json
{
  "$schema": "https://thematthub.github.io/naga/schema/model-v29.json",
  "schema_version": 29,
  "blocks": [
    {
      "name": "create_1",
      "type": "CREATE",
      "params": {"distribution": "Exponential", "dist_params": {"rate": 1.0}}
    },
    {
      "name": "process_1",
      "type": "PROCESS",
      "params": {
        "distribution": "Exponential",
        "dist_params": {"rate": 1.25},
        "resource_name": "Server"
      }
    },
    {"name": "dispose_1", "type": "DISPOSE"}
  ],
  "links": [
    {"source": "create_1", "target": "process_1"},
    {"source": "process_1", "target": "dispose_1"}
  ],
  "resources": [{"name": "Server"}],
  "simulation": {"time_limit": 1000, "warmup": 100, "replications": 5}
}
```

To start from one of the bundled models instead, `naga export --python warehouse_forklifts`
writes the code that builds it, and `naga fmt FILE` rewrites any model file in this short form.
The JSON file stays the one model that the command line, Python, an agent and a twin all share.

## What you can see

A run is easier to trust when you can look at it, so the engine writes what it finds as pages
as well as tables. Each of the four below is a single file, made by one command, that opens in
a browser with no server behind it: a report to read, a recorded run to replay, a monitor that
follows a plant, and a viewer in which someone else can change the inputs and run the model
again. They are what you hand to the people who asked the question.

| | |
|---|---|
| <img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/shots/charts.png" alt="Charts from the HTML report" width="420"> | <img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/shots/player.png" alt="A recorded run in the HTML player" width="420"> |
| **A report.** `naga run MODEL --report report.html`: KPIs with intervals, resources, costs and charts. | **A recorded run.** `naga run MODEL --player run.html`: the run replayed on the model's layout, with its KPIs. |
| <img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/shots/monitor.png" alt="The twin's monitor page" width="420"> | <img src="https://raw.githubusercontent.com/TheMattHub/naga/main/docs/img/shots/viewer.png" alt="The viewer: the engine in a browser" width="420"> |
| **A twin's monitor.** `naga twin run ... --monitor`: the plant now, the forecast against what happened, problems with their hints. The page reloads itself. | **A viewer.** `naga viewer MODEL -o viewer.html`: the engine runs in the reader's browser; they change the inputs you exposed and run it again. |

## Example models

The package holds about sixty models: queues with a known analytical answer, job shops,
warehouses with forklifts, an energy site, a biogas plant, a supply chain, system-dynamics
classics, agent-based classics. Each runs as it is, and the documentation has a gallery of them
with numbers from a run:

```console
naga run warehouse_forklifts --replications 2
```

Some of them use submodels that came from asset libraries of a separate product; the engine
runs the submodels, the product's artwork is not included here.

## Use with an agent

The engine ships 71 tools an agent can call to build, validate, run, debug and analyse a model, and two
ways to offer them:

<!-- no-run: a server that waits for its client, and a request to a language model -->
```console
pip install "nagasim[mcp]"
naga mcp --root path/to/models                 # an MCP server over stdio (Claude Code, Claude Desktop)

pip install "nagasim[assistant]"               # only for Claude
naga assist model.json "add a second server"   # one assistant turn on a model file
```

A local model needs no extra.

`naga assist` talks to a local model by default (an OpenAI-compatible server such as Ollama at
`http://localhost:11434/v1`; pass its model name with `--model-id`) or to Claude when
`ANTHROPIC_API_KEY` is set. Where a request goes is the caller's choice and nothing else's:
command-line flags, then the environment (`NAGASIM_LLM_PROVIDER`, `NAGASIM_LLM_BASE_URL`,
`NAGASIM_LLM_MODEL`; also `NAGASIM_LLM_API_KEY` and `NAGASIM_LLM_REASONING`), then the defaults.
`naga mcp` keeps every file inside `--root` and has the network off unless you allow it;
`naga assist` works on the one model file you give it and reaches only the endpoint you
configured. A model's own Python behaviour never runs through either.

## Limits to know

- The readers, the MQTT outbox and the twin have run against Mosquitto 2.1.2, an asyncua 2.1.0
  OPC UA server and a Sparkplug B edge node built on the official schema, all on one machine
  (docs: Live data, "Tested against"). Nothing has run against a real plant, an MES, a plant
  counter, another broker or OPC UA server, or a commercial edge node.
- A twin starts from the plant's state for lines with finite buffers, accumulating conveyors,
  batches whose members the plant names, transfers, robot and cobot cycles, the plant's orders,
  and fleets whose vehicles each report where they are and what they carry. It cannot yet start
  from agent populations, a non-accumulating conveyor with parts on it, tours, gantries, segment
  capacities or worker fleets: such a model is monitored and its forecast is refused by name.
- Recommendations compare a resource's capacity, a DECIDE's route and, with an order signal,
  the order actions (hold, release, expedite, resequence), refused by name where the model
  cannot answer them. They are written to a file or an MQTT topic of yours. Nothing acts on a
  plant.
- The viewer needs Chrome, Edge or Firefox on a desktop.
- There is no editor and no animation here beyond the recorded player: Naga is an engine.

## Building a product on top

Naga is the engine only. It is written so that a product package (an editor, asset libraries,
an assistant, connectors) sits beside it and imports it, never the other way round: nothing in
`nagasim` imports such a package, and a test keeps it that way. Where the code or a comment says
`product` or "the product", that is the place such a package takes; none is part of this
repository.

What a product builds on:

- `nagasim.api` (`run_model` and friends) and `nagasim.core.document.ModelDocument`, the model
  as an editable, undoable document;
- the model schema (`naga schema`) and structured validation problems with stable codes;
- the event bus and `nagasim.core.trace.TraceRecorder`, with `nagasim.core.playback.Timeline`
  to turn a recorded or live run into the state at any instant (what an animation draws);
- plugin entry points for blocks, distributions and exporters (`nagasim.plugins`);
- `nagasim.netguard`, the one place file and network access is decided.

## Development

<!-- no-run: installs the package and runs its test suite -->
```console
git clone https://github.com/TheMattHub/naga
cd naga
pip install -e ".[plan,ml,sql,plot]" -r requirements-dev.txt
python -m pytest -q -n auto
```

Python 3.11 or later. The engine depends on SimPy, NumPy, pandas, SciPy and openpyxl; everything
else is optional and imported only where it is used. The documentation's sources are in `docs/`
(`pip install -r requirements-docs.txt`, then `python -m mkdocs serve`).

## Versions

Before 1.0:

- Model files always migrate forward: a file saved by an older version opens in a newer one.
- Agent tool names and problem codes are permanent.
- The Python API may change between minor versions; every such change has a changelog line.

See the [changelog](CHANGELOG.md).

## Licence and citation

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). If you use Naga in published
work, please cite it: see [CITATION.cff](CITATION.cff).
