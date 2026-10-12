# Coming from AnyLogic

Like AnyLogic, Naga runs discrete-event, system-dynamics and agent-based parts in one model on
one clock. Unlike AnyLogic it is an engine only: a JSON model file, a command line and a Python
API. There is no editor, no animation beyond a replay file, and no code attached to events.

## What maps to what

| AnyLogic | Here |
|---|---|
| Process Modeling Library: Source, Sink, Delay, Service, Seize, Release, SelectOutput, Batch, Hold, Match | CREATE, DISPOSE, DELAY, PROCESS, SEIZE, RELEASE, DECIDE, BATCH, HOLD, MATCH |
| ResourcePool, schedules, failures | `resources`, `resource_sets`, `schedules`, `failures` |
| Material Handling Library: transporters, paths, conveyors | TRANSPORT with `fleets` on a path `network`, CONVEYOR, batteries and chargers |
| An agent type with a statechart, parameters and variables | an entry in `agent_types`: states (composite states too), transitions by timeout, rate, condition or message, parameters and attributes |
| An agent population | an entry in `populations` |
| Agents in continuous space, in a grid, in a network | `space` of an agent type: continuous, grid with patches, or the model's path network; links between agents |
| A process entity that is also an agent | CREATE with a `population`: the entity has an agent, and each reaches the other |
| System dynamics: stocks, flows, dynamic variables, table functions | `levels`, `flows`, `auxiliaries`, `tables`, delays, subscripts, units ([System dynamics](../system-dynamics.md)) |
| Parameter variation, Monte Carlo | `naga doe`, `naga scenarios` |
| Optimization experiment | `naga optimize` |
| Calibration experiment | `naga calibrate` |
| Java in "On enter" and "On exit" fields | expressions in ASSIGN, DECIDE, HOLD and RECORD; a restricted grammar, not a language |

In AnyLogic an entity, a resource unit and an agent are all agents. Here they are three
different things: an entity flows through blocks, a resource is seized and released, and an
agent lives in a population and runs its statechart. They meet through expressions
(`COUNT("People", "I")`, `NQ("Server")`), messages (SEND) and actions (an agent can make an
entity `enter` a block, or `transfer` an amount between levels).

## What is missing here

- **Arbitrary code.** No Java, and no Python on events either. Behaviour is blocks, statecharts
  and expressions. The exceptions are deliberate and need you to opt in: a LOGIC block or a
  plugin (Python you install), and an agent type's Python behaviour, which runs only with your
  consent ([Security](../security.md)).
- **Pedestrian, rail, road and GIS libraries.** None of them.
- **A fluid library.** Levels and flows model tanks, silos and batteries at rates, with exact
  crossing times; there is no pipe network.
- **Nested agents and links across populations.** A population holds agents of one type; an
  agent does not contain a population, and a link joins two agents of the same population.
- **3D, a model canvas, a run-time UI with controls.** Not in this repository.
- **A cloud service.** Runs are local processes.

## What you gain

- The model is a text file: `naga diff`, `naga merge`, review in a pull request, assertions in
  CI (`naga check`).
- An LLM agent can build, validate, run and debug a model through [tools](../mcp.md) that
  return structured problems.
- Python around it: results are pandas frames ([Python API](../python-api.md)).

Models that mix the methods, to read first: `ed_patient_agents.json` (entities that are
agents, [gallery](../gallery/services.md)) and `agents_machine_repair.json`
([gallery](../gallery/agent-based.md)).
