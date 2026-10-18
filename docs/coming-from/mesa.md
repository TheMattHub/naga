# Coming from Mesa

Mesa is an agent-based modelling library for Python: an agent is a class, a model is a class that creates the agents, and by default time advances in steps in which each agent runs its `step` method (Mesa also has an experimental event scheduler). Naga describes an agent as a statechart in a model file, and the engine runs the agents on its continuous clock, so a transition happens after a delay or at a rate and not once per step. This page shows one epidemic in both and maps the concepts.

## The same model twice

A well-mixed population of 500 people, 10 of them infected on day 0. A susceptible person is infected at a rate of 0.25 a day times the infected share, an infected person recovers at 0.1 a day, and the model runs 150 days. In Mesa you write the agent, the model, the step loop and the bookkeeping:

```python
--8<-- "docs/snippets/from-mesa-mesa.py"
```

```text
--8<-- "docs/snippets/from-mesa-mesa-output.txt"
```

In Naga you write the states and the transitions between them, and the population and the replications around them:

```python
--8<-- "docs/snippets/from-mesa-naga.py"
```

```text
--8<-- "docs/snippets/from-mesa-naga-output.txt"
```

The share of the population ever infected is close to the textbook value in both. They are not equal, because Mesa turns a rate into a probability per daily step and Naga draws exact event times, and because each is a sample of a random process with a population of only 500.

## What maps to what

| In Mesa | Here |
|---|---|
| `mesa.Agent` subclass | an entry in `agent_types`: attributes, states, transitions |
| `mesa.Model` that creates the agents | `populations`: a type, a count and an initial placement |
| `step()` called once per tick | transitions with a `timeout` (a delay) or a `rate`, or a `tick` when you want fixed steps |
| `self.model.agents` and `shuffle_do` | `COUNT("Population", "State")` and the other agent expressions; the engine orders the events |
| `mesa.discrete_space` grids and networks (`mesa.space` in older code) | a `space` on the agent type and a `network` on the population |
| `DataCollector` | `agent_summary` and the agent attribute log in the results |
| `mesa.batchrunner`, a loop over seeds | `replications`, and scenarios on common random numbers |

## What has no equivalent here

- **Arbitrary Python in a step.** A transition is a rule in the model file. Python in an agent is possible but needs your explicit consent when the model runs, because a model file is data and does not run code by itself.
- **Mesa's visualisation.** The report and the player show the run; there is no live browser view while it runs.

## When Mesa is the better choice

- The agents' behaviour is mostly algorithmic and easier to write as Python than as states and rules.
- You depend on the Python scientific stack inside the agents' decisions.
- The model is small and you do not need continuous time, a queue network or the statistics around the run.

Naga is the better choice when the agents sit in the same model as queues, resources or stocks and flows, when you want replications with intervals and scenarios without writing them, or when the model must be readable by someone who does not run Python. The [agent-based modelling guide](../agent-based-modelling.md) explains the statecharts in full.
