# Coming from NetLogo

Naga has agents on a grid with patches, in continuous space or on a network, with links, ticks,
births and deaths. Six classic models ship with it. What it does not have is NetLogo's
language: an agent's behaviour is a statechart and a list of declarative actions, not
procedures.

## What maps to what

| NetLogo | Here |
|---|---|
| a breed | an agent type (`agent_types`) and a population of it (`populations`) |
| `turtles-own` | `attributes` of the agent type; `parameters` for what all agents share |
| `to go ... tick` | a `tick` on the type: `on_tick` actions and tick transitions, every `dt` |
| `ask turtles [ ... ]` in random order | `"activation": "random"`; also `sequential` and `synchronous` |
| a turtle variable that says what it is doing | a state of the statechart; transitions by timeout, rate, condition, message or tick |
| patches, `patches-own`, `diffuse`, evaporation | a grid `space` with `patches`: `init`, `diffuse`, `evaporate`; read with `PATCH("name")` |
| `neighbors`, `turtles-here`, `in-radius` | `NEIGHBOURS(...)`, `CELL_COUNT(...)`, `NEAR(...)`, and their sums and means |
| `forward`, `right`, `face` | movement actions: random walk, turn, move to |
| links, `link-neighbors` | a population's `network` (random, small-world, scale-free); `LINKED(...)`, `LINK_MEAN(...)` |
| `hatch`, `die` | the `spawn` and `remove` actions |
| `random-float`, `random-normal` | `RAND()`, `UNIF`, `NORM`, `EXPO`; every population has its own random streams |
| BehaviorSpace | `naga doe` (factorial, Latin hypercube, one at a time), `naga scenarios` |
| reporters and plots | the `agent_summary` and `agent_attributes` frames; `timeseries` |
| `random-seed` | `--seed`; replication *i* uses seed + *i* |

Each row is a section of [Agent-based modelling](../agent-based-modelling.md).

## The classics

`abm_wolf_sheep.json`, `abm_schelling_segregation.json`, `abm_boids_flock.json`,
`abm_sugarscape_lite.json`, `abm_nagel_schreckenberg.json` and `abm_sir_small_world.json`, with
their numbers in the [gallery](../gallery/agent-based.md). Each model's description says which
published model it follows.

```console
naga run abm_schelling_segregation
```

## What is missing here

- **A language.** No procedures, no `ask` with a block of code, no lists and no anonymous
  reporters. An action is one of a fixed set (`set`, `send`, `move`, `spawn`, `remove`, ...)
  with expressions as arguments. If the behaviour does not fit, an agent type can carry a Python
  function instead; it runs only with your consent
  ([Python behaviour](../agent-based-modelling.md#python-behaviour)).
- **The interface.** No world view, sliders, buttons or live plots. A run is headless; a
  recorded run replays agents as tokens, and patches are not drawn.
- **Patches as agents.** A patch holds named numbers that diffuse and evaporate. It runs no code
  and has no state of its own beyond them.
- **Links across breeds.** A link joins two agents of the same population.
- **A reader for `.nlogo` or `.nlogox` files.**
- **Extensions** (GIS, R, Python, sound, ...).

## What you gain

- Event-driven agents as well as ticked ones: a transition with a rate or a timeout fires at its
  own time, with no step to choose.
- Agents beside queues, resources and stocks in one model: an agent can send an entity into a
  block, and a block can message agents.
- Replications, confidence intervals and experiments from the command line.
