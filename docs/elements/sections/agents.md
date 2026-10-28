These sections add agent-based modelling. An `agent_type` is a statechart: attributes, states and the transitions between them. A `population` is a number of agents of one type, with an initial placement. Use them for individuals that decide and interact, such as people in an epidemic, drivers or machines with their own behaviour.

## Behaviour and rules

A transition has exactly one trigger: a `timeout`, a `rate`, a `condition`, a `message` or a `tick`. A rate must be shared by all agents, own to the agent, or a product of the two. A rate that changes continuously with time is refused. A condition or rate that reads model state outside the agents is re-checked only at agent events, unless it sets `scan`.

A population has a `count` and can have a `max_count`. A spawn into a full population stops the run with an error. The agents live in a space that is continuous, a grid or a network. The functions that read neighbours need a space of the matching kind. Agents can be linked to each other.

A CREATE block with `population` set gives each entity an agent. A SEND block or a send action delivers a message that a message transition waits for. A message nobody sends is reported by a check. Each agent draws random numbers from its own stream, so one agent's draws do not depend on the others.
