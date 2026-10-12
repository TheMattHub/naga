# Python API

The command line is a thin layer over a few functions. This page shows the ones a script or a
notebook uses; their signatures are in [Python entry points](reference/api.md). Before 1.0 this
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

`ModelDocument` is the model as an editable, undoable document. It writes the same JSON a
hand-written file holds.

```python
from nagasim.core.document import ModelDocument

doc = ModelDocument()
doc.set_resources([{"name": "Server", "capacity": 1, "discipline": "FIFO"}])
doc.add_block("CREATE", (100, 100), "create_1")
doc.set_params("create_1", {"distribution": "Exponential", "dist_params": {"rate": 1.0}})
doc.add_block("PROCESS", (300, 100), "process_1")
doc.set_params("process_1", {"resource_name": "Server", "distribution": "Exponential",
                             "dist_params": {"rate": 1.25}})
doc.add_block("DISPOSE", (500, 100), "dispose_1")
doc.add_link("create_1", "process_1")
doc.add_link("process_1", "dispose_1")
doc.set_simulation(time_limit=200, warmup=20, replications=5)
doc.save("built.json")

print(api.run_model("built.json").kpis().head())
```

## What else is importable

- `nagasim.modelschema.build_json_schema()`: the JSON Schema of a model file (`naga schema`).
- `nagasim.modelcheck.assert_model(path)`: a model's own assertions, for your test suite.
- `nagasim.agent_tools`: the [agent tools](reference/tools.md) as plain functions, with no MCP.
- `nagasim.core.trace.TraceRecorder` and `nagasim.core.playback.Timeline`: every event of a
  run, and the state at any instant.
- `nagasim.plugins`: entry points for your own blocks, distributions and exporters.
- `nagasim.core.sd` and `nagasim.core.agents`: authoring helpers for
  [system dynamics](system-dynamics.md) and [agent-based](agent-based-modelling.md) models.
