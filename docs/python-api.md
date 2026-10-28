# Python API

The command line is a thin layer over a few functions. This page shows the ones a script or a
notebook uses; their signatures are in [Python entry points](reference/api/index.md). Before 1.0 this
API may change between minor versions, with a changelog line each time.

## Run a model

`run_model` takes a path or a model as a dictionary. The bundled models ship inside the package:

```python
from pathlib import Path

import nagasim
from nagasim import api

model_path = str(Path(nagasim.__file__).parent / "models" / "single_server.json")
results = api.run_model(model_path, replications=5, run_length=200, warmup=20, seed=0)
```

`results` is a `Results`: a `dict[str, pandas.DataFrame]` with a few conveniences.

```python
system = results["system"]          # one frame per kind of result, one row per run and object
kpis = results.kpis()               # metric, mean, std, ci95 over the replications
print(kpis.head())
print(results.kpi("system/Avg Queue Length"))   # one KPI's mean; KeyError if the key is absent
```

A KPI has a stable key, `family/name` or `family/object/column`
(`system/Avg Queue Length`, `resource/Server/Usage % (snapshot)`). Scenarios, optimisation,
calibration and assertions all name KPIs by these keys. The frames and their columns are in the
[KPI reference](reference/kpis.md).

In a notebook, `results` displays as the KPI table, and `results.plot(key)` draws a KPI or a
time series (it needs `matplotlib`, the `plot` extra). The repository has a notebook that does
all of this: `docs/notebooks/quickstart.ipynb`.

## Compare scenarios

A scenario is a name and a list of overrides, each a path into the model and a value.
`run_scenarios` runs them on common random numbers and compares each with the baseline by a
paired t-test.

```python
import json

from nagasim import experiments

layout = json.loads(Path(model_path).read_text(encoding="utf-8"))
layout["simulation"].update(time_limit=200, warmup=20)
layout["scenarios"] = [{
    "name": "Faster server",
    "overrides": [{"path": "blocks.process_1.params.dist_params.rate", "value": 1.6}],
}]

experiment = experiments.run_scenarios(layout, replications=5)
print(experiment.comparison(kpis=["system/Avg Queue Length", "system/Avg Throughput Time"]))
```

## Search a parameter

```python
from nagasim.optimize import Factor, optimize

factors = [Factor(path="blocks.process_1.params.dist_params.rate", kind="continuous",
                  low=1.1, high=2.0, name="service_rate")]
found = optimize(model_path, factors, response="system/Avg Queue Length", goal="min",
                 budget=20, replications_per_point=2, run_length=200, warmup=20)
print(found.best, found.best_mean)
```

`nagasim.optimize` also has designs of experiments, ranking and selection and sensitivity
analysis; `nagasim.calibrate` fits parameters to observed KPIs and `nagasim.forecast` gives
prediction bands. Each has a command of the same name.

## Build a model in Python

`nagasim.build` writes the model file from Python. Every block type is a method named after
it, taking the block's parameters by name (the [block reference](reference/blocks/index.md));
`a >> b` links two blocks; `add_resource`, `add_schedule`, `add_level` and the like add an
entry to a list of the file; `Model(...)` takes the run settings (`time_limit`, `warmup`,
`replications`, every key of `simulation`) and the other sections. Every distribution of the
[distribution reference](reference/distributions/index.md) is a constructor taking its parameters
by name, `nb.Exponential(rate=1.25)`; given as `distribution=` it writes the file's
`"distribution"` and `"dist_params"`, and a parameter named like a Python keyword takes a
trailing underscore (`nb.Poisson(lambda_=2)`). A block without a name gets one
(`process_1`). It is a thin layer: the file it writes is the model, and what each value
means is the same as in a file you write by hand. It may change between minor versions
before 1.0.

```python
from nagasim import build as nb

m = nb.Model(time_limit=1000, warmup=100, replications=5)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()

m.save("mm1.json")                       # the short form `naga fmt` writes
print(m.run(replications=2).kpis().head())
print(m.run(replications=2).kpi("system/Total Entities Completed"))   # one KPI's mean
```

A block's name is the first argument or `name=`, not both: `m.process(name="Lathe", ...)` and
`m.process("Lathe", ...)` are the same block, and giving both is a `TypeError`.

A line with a buffer between two machines, and a resource on a shift schedule beside a tank:

```python
line = nb.Model(time_limit=480)
line.add_resource(name="Lathe")
line.add_resource(name="Mill")
parts = line.create(distribution=nb.Exponential(rate=0.5))
lathe = line.process(resource_name="Lathe", distribution=nb.Triangular(low=1, mode=1.5, high=2))
mill = line.process(resource_name="Mill", distribution=nb.Constant(value=1.8))
parts >> lathe >> line.buffer(capacity=5) >> mill >> line.dispose()
line.add_failure(name="Mill breaks", resources=["Mill"], uptime=nb.Exponential(rate=0.01),
                 downtime=nb.Constant(value=15))

plant = nb.Model(time_limit=100)
plant.add_schedule(name="shifts", kind="capacity", points=[[50, 2], [50, 1]], repeat=True)
plant.add_resource(name="Pump", capacity=2, capacity_schedule="shifts")
plant.add_level(name="Tank", capacity=100, initial=20)
plant.add_flow(name="draw", **{"from": "Tank"}, rate="2")
```

The strings still work (`distribution="Exponential", dist_params={"rate": 1.25}`,
`simulation={"time_limit": 1000}`); a value given both ways is an error. The package ships
type hints for the builder (`build.pyi`), so an editor completes block parameters, records,
run settings and distribution parameters and flags a misspelt one before the model runs; a
plugin's blocks and distributions are there at run time only. A value is in the model's
time unit (`nb.Model(time_unit="hours")`) unless its own unit keyword says otherwise
(`setup=90, setup_unit="seconds"`; a block's distribution: `time_unit="hours"`); the
hints list the units each keyword accepts, so nothing needs converting by hand. `help(m.process)` lists a
block's parameters with their defaults. `naga export --python model.json` writes the code
that builds any model file, the bundled ones included:

```console
naga export --python single_server -o mm1.py
```

`ModelDocument` (`nagasim.core.document`) is the editor's document of the same file: undo,
positions, renames that follow every reference.

## Your own logic

A LOGIC block routes each entity by a Python class of your own, kept in a module you
write. The class needs one method, `execute(self, entity, ctx)`, that returns the index of
the outgoing link to take; the block's `logic_class` names it. Pass the module to the run
by its dotted name, or as an imported module when the run stays in one process:

```python
# my_logic.py, in a folder on PYTHONPATH
class RouteBig:
    def execute(self, entity, ctx):
        return 1 if getattr(entity, "size", 0) > 10 else 0   # an ASSIGNed attribute
```

<!-- no-run: needs my_logic.py on PYTHONPATH -->
```python
results = run_model("model.json", logic="my_logic", workers=4)
```

<!-- no-run: needs my_logic.py on PYTHONPATH -->
```console
naga run model.json --logic my_logic
```

A class your module lacks still comes from `nagasim.core.custom_logic`, and a name that
does not import, or a class found in neither, is an error that names both. With more than
one worker, each process imports the module again by name, so it must live in a file on
the path. Only the caller chooses the module: a model file has no key for it, so opening a
model never runs code it names.

## What else is importable

- `nagasim.modelschema.build_json_schema()`: the JSON Schema of a model file (`naga schema`).
- `nagasim.modelcheck.assert_model(path)`: a model's own assertions, for your test suite.
- `nagasim.agent_tools`: the [agent tools](reference/tools.md) as plain functions, with no MCP.
- `nagasim.core.trace.TraceRecorder` and `nagasim.core.playback.Timeline`: every event of a
  run, and the state at any instant.
- `nagasim.core.session.SimSession`: one replication driven from outside, in chunks, with a
  snapshot, safe edits (a variable, a resource's capacity) and answers to Policy decisions.
  `nagasim.rl.SimEnv` is the reset-and-step environment of a policy under training.
- `nagasim.plugins`: entry points for your own blocks, distributions and exporters. A worked
  example (one block, one distribution, one exporter) is in the repository under
  [examples/plugins/inspection_plugin](https://github.com/TheMattHub/naga/tree/main/examples/plugins/inspection_plugin).
- `nagasim.core.sd` and `nagasim.core.agents`: authoring helpers for
  [system dynamics](system-dynamics.md) and [agent-based](agent-based-modelling.md) models.
