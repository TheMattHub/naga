# Coming from SimPy

Naga runs on SimPy: every block is a SimPy process and every resource a SimPy resource. What
changes is where the model lives. In SimPy the model is your Python code. Here it is a JSON
file, and the engine supplies the processes, the random streams, the replications and the
statistics.

## The same model twice

A single-server queue: arrivals at 1 a minute, service at 1.25 a minute, 10 000 minutes of which
the first 1 000 are thrown away, five replications.

In SimPy you write the process, the monitoring and the replication loop:

```python
import random
import statistics

import simpy


def customer(env, server, times, warmup):
    arrived = env.now
    with server.request() as request:
        yield request
        yield env.timeout(random.expovariate(1.25))
    if arrived >= warmup:
        times.append(env.now - arrived)


def source(env, server, times, warmup):
    while True:
        yield env.timeout(random.expovariate(1.0))
        env.process(customer(env, server, times, warmup))


means = []
for seed in range(5):
    random.seed(seed)
    env = simpy.Environment()
    server = simpy.Resource(env, capacity=1)
    times = []
    env.process(source(env, server, times, 1000))
    env.run(until=10000)
    means.append(statistics.mean(times))
print("SimPy: mean time in system", round(statistics.mean(means), 2))
```

Here you write what the system is:

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

```python
from nagasim.api import run_model

kpis = run_model("mm1.json", seed=0).kpis().set_index("metric")
print("Naga: mean time in system", round(kpis.loc["system/Avg Throughput Time", "mean"], 2),
      "+/-", round(kpis.loc["system/Avg Throughput Time", "ci95"], 2))
print(kpis.loc[["system/Avg Queue Length", "resource/Server/Usage % (snapshot)"], ["mean", "ci95"]])
```

Both estimate the same quantity, 4 minutes in theory, from different random numbers. The second
one also gave the queue length, the utilisation and their confidence intervals without a line of
monitoring code, and the model can be validated, diffed, sent to an optimiser or edited by an
agent, because it is data.

## What maps to what

| In SimPy | Here |
|---|---|
| a generator function per kind of process | blocks joined by `links`; an entity flows through them |
| `env.process(...)` in an arrival loop | a CREATE block |
| `yield env.timeout(...)` with your own random draw | DELAY or PROCESS with a named distribution; every block has its own random stream |
| `simpy.Resource`, `with resource.request()` | an entry in `resources`, and PROCESS, or SEIZE ... RELEASE |
| `PriorityResource`, `PreemptiveResource` | `priority` on the seizing block; `preempt` on PROCESS |
| `simpy.Store`, `FilterStore` | HOLD, BATCH and MATCH, which keep entities until a signal, a condition or a partner |
| `simpy.Container` | a `level` with `flows`: a continuous quantity whose crossings are computed exactly |
| `env.event()`, `event.succeed()` | SIGNAL and a HOLD in "Signal" mode; a HOLD in "Condition" mode waits on an expression |
| `process.interrupt()` | `failures` on a resource, preemption, a renege time on PROCESS |
| lists you append to, monkey-patched resources | result frames for every block, resource, entity type and queue |
| a loop over seeds | `simulation.replications`; replication *i* uses seed + *i* |
| the same seeds for two variants, by hand | `naga scenarios`: common random numbers by block name, paired comparison |
| `env.run(until=...)` | `simulation.time_limit`, or no limit when arrivals are finite |

## What has no equivalent here

- **Free-form processes.** A block does what its type does. Logic that is not a block goes in an
  expression (a restricted grammar, never `eval`), or in a LOGIC block or a plugin block, which
  are Python you write and install yourself.
- **`simpy.rt`.** There is no real-time environment.
- **Your own event loop.** You do not get the `Environment` to schedule events on; `run_model`
  owns the run. The agent tools can step a run (`debug_run`), not drive it.

## When raw SimPy is the better choice

- The model is mostly control logic: a protocol, a scheduling algorithm, a state machine with
  many special cases. Python says that better than blocks do.
- You need real time, or to embed the simulation in another program's loop.
- You want one small pure-Python dependency. Naga needs NumPy, pandas, SciPy and openpyxl.
- The model is a few dozen lines and you do not need the statistics around it.

Naga is the better choice when the system is flow through stations and resources, when you want
replications and intervals without writing them, when someone else (or an agent) must read and
change the model, or when the model also has stocks and flows or agents.

## Speed

Naga runs on SimPy and does more per event, so on a model both can express it is slower than a
bare SimPy loop. This is by how much, on four small queueing models that SimPy, salabim, Ciw and
Naga can all express exactly: a single queue, a three-server queue, a line of three stations and
a three-station job shop with rework.

<!-- BENCHMARKS: benchmarks/results.md is written by benchmarks/compare.py from its
     measurements. Nothing in the table is typed. -->

--8<-- "benchmarks/results.md"

How to read it:

- **The same model, the same run.** Poisson arrivals, exponential service, first come first
  served, the same run length, warm-up and number of replications in every library, each
  written the way that library is normally used, one short file per library.
- **Correct before fast.** A library is timed only if the theory's mean time in system lies
  inside the confidence interval of its own estimate. The random numbers differ between
  libraries, so the estimates do too.
- **What is timed** is the CPU time of the whole run in one process: building the model, the
  replications, and reading the one statistic. "Customers per second" divides the expected number
  of arrivals, the same for every library, by that time.
- **The libraries do not do the same work in that time.** The SimPy code keeps one number per
  customer and nothing else. salabim keeps monitors on every resource and component. Ciw keeps
  a record of every visit to a station. Naga collects the statistics of every block, resource
  and queue, logs every entity and resource event, samples a time series, and builds its result
  tables for each replication. The row "logs and sampler off" is Naga with
  `"record_logs": false` and `"timeseries_points": 0` under `simulation`; the summary statistics
  have no switch.
- **The limits.** One machine, one Python version, small models with nothing but queues. A
  model with expressions, schedules or failures does more per event and was not measured; nor
  was memory. Take the ratios, not the seconds. In the salabim version measured, most of the
  time goes to creating a component per customer, not to its event loop.

The scripts are in the repository's `benchmarks/` folder. To measure on your machine:

<!-- no-run: needs salabim and Ciw installed, and takes minutes -->
```console
$ pip install -r benchmarks/requirements.txt
$ python benchmarks/compare.py
```
