# Coming from salabim

salabim is a process-oriented simulation library: you write a class for each kind of thing that moves through the system, and its `process` method says what that thing does, one `request`, `hold` or `release` at a time. Naga takes the other route. The system is a model file of blocks, and the engine supplies the processes, the random streams, the replications and the statistics. This page shows one queue in both and maps the concepts.

## The same model twice

A single-server queue: customers arrive at 1 a minute on average, service takes 0.8 minutes on average, and each model runs 5 500 minutes of which the first 500 are thrown away. In salabim you write the customer's life, the generator, the monitor and the replication loop:

```python
--8<-- "docs/snippets/from-salabim-salabim.py"
```

```text
--8<-- "docs/snippets/from-salabim-salabim-output.txt"
```

In Naga you write what the system is, and the replications, the monitoring and the interval come with the run:

```python
--8<-- "docs/snippets/from-salabim-naga.py"
```

```text
--8<-- "docs/snippets/from-salabim-naga-output.txt"
```

Both estimate the same quantity from different random numbers, and the exact value for this queue is 4 minutes. Five replications are few, so the interval is wide; the point is that Naga reports it and that the model stays a file that can be saved, diffed and handed to an optimiser or an agent.

## What maps to what

| In salabim | Here |
|---|---|
| `sim.Environment(random_seed=...)` | `seed` of the run; replication *i* uses seed + *i* |
| a `Component` subclass with a `process` method | blocks joined by links; an entity flows through them |
| `sim.ComponentGenerator(Customer, iat=...)` | a CREATE block with a distribution |
| `self.request(r)`, `self.hold(t)`, `self.release()` | one PROCESS block with the action Seize Delay Release, or SEIZE, DELAY and RELEASE |
| `sim.Resource(capacity=...)` | an entry in `resources`, with a schedule or failures if it needs them |
| `sim.Exponential(rate=...)` and the other distributions | `nb.Exponential(rate=...)`; every block has its own random stream |
| `sim.Monitor`, `.tally`, `.mean()` | result frames for every block, resource, entity type and queue, with intervals |
| `sim.State`, `self.wait(state)` | SIGNAL and a HOLD block, or a HOLD in Condition mode |
| `sim.Store`, `sim.Queue` | the queue of a resource, BATCH and MATCH |
| `animate=True` | none while running; `naga run --player` writes an HTML file that replays the run |

## What has no equivalent here

- **Free-form processes.** A block does what its type does. Logic that is not a block goes in an expression, or in a LOGIC block or a plugin, which are Python you write and install yourself.
- **Live animation.** There is no window; the report and the player are the pictures.

## When salabim is the better choice

- The behaviour is mostly control logic that Python says more clearly than blocks do.
- You want salabim's animation while the model runs.
- The model is small and you do not need replications, scenarios or a file other tools can read.

Naga is the better choice when the system is flow through stations and resources, when you want the statistics without writing them, or when someone else, or an agent, must read and change the model. On speed, SimPy and salabim are measured beside Naga in the [benchmarks](../validation/benchmarks.md).
