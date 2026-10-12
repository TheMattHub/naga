# Quick start

A single-server queue, from nothing to numbers.

## Install

```console
pip install nagasim
```

Python 3.11 or later. This gives you the `nagasim` package and the `naga` command.

## A model file

Customers arrive one a minute on average, one server serves 1.25 a minute, and they leave. Save
this as `mm1.json`:

```json title="mm1.json"
{
  "blocks": [
    {"name": "arrive", "type": "CREATE",
     "params": {"distribution": "Exponential", "dist_params": {"rate": 1.0}}},
    {"name": "serve", "type": "PROCESS",
     "params": {"action": "Seize Delay Release", "resource_name": "Server",
                "distribution": "Exponential", "dist_params": {"rate": 1.25}}},
    {"name": "leave", "type": "DISPOSE", "params": {}}
  ],
  "links": [{"source": "arrive", "target": "serve"}, {"source": "serve", "target": "leave"}],
  "resources": [{"name": "Server", "capacity": 1}],
  "simulation": {"time_limit": 10000, "warmup": 1000, "replications": 5}
}
```

Three blocks, two links, one resource. Times are in minutes unless the file says otherwise.
Every parameter of every block type is in the [block reference](reference/blocks.md).

## Run it

```console
naga validate mm1.json
naga run mm1.json
```

`validate` prints `mm1.json: OK`, or each problem with a stable code. `run` prints the system
table of each replication: entities created and completed, average time in the system,
throughput, queue lengths, utilisation.

Queueing theory gives this queue a mean time in the system of 1 / (1.25 - 1) = 4 minutes and a
utilisation of 80%. Your numbers should be close to both; how close is what the confidence
interval says, which the Python call below returns.

A report with charts, or the frames as a workbook:

```console
naga run mm1.json --replications 10 --report report.html
naga run mm1.json --no-logs --out results.xlsx
```

Without `--no-logs` the workbook also holds every event of every entity, which on a run this
long is most of its size and of the time it takes to write.

## From Python

```python
from nagasim.api import run_model

results = run_model("mm1.json", replications=10, seed=0)
kpis = results.kpis()                       # metric, mean, std, ci95 (the 95% half-width)
print(kpis[kpis["metric"].isin(["system/Avg Throughput Time", "system/Avg Resource Usage %"])])
print(results["resource_summary"].head())   # one row per resource and replication
```

`results` is a dictionary of pandas frames; the [KPI reference](reference/kpis.md) lists every
frame and column. The same seed gives the same frames.

## Next

- Change the model and compare: `naga scenarios`, `naga optimize`, `naga doe`
  ([command line reference](reference/cli.md)).
- Larger models that run as they are: `naga examples` lists them, `naga run job_shop_jackson`
  runs one, and the [gallery](gallery/index.md) shows each with its numbers.
- The whole file format: [Model file format](model-file-format.md).
- The same model in raw SimPy, side by side: [Coming from SimPy](coming-from/simpy.md).
