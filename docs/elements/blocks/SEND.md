Use SEND to let an entity reach an agent in an agent-based model. A customer calls a driver, an order wakes a machine agent, or a signal goes to a whole population. It is the bridge from the discrete flow to the agents. The agent's statechart reacts to the message in a `message` transition.

## Behaviour and rules

The `message` name is what an agent's transition matches. The default recipient `entity.agent` is the agent that the entity got when it entered the model. Other forms are `AGENT("Pop", i)` for one agent, or `{"population": "Pop"}` for a whole population. A `where` condition on the agents selects some, and `random` picks one of them from the block's own random stream.

The optional `payload` is an expression evaluated for the entity. The receiving transition reads it as `payload`. The message is delivered before the entity moves on, in order of sending, at the same instant. The block takes no time.

A model with no populations cannot use SEND, and the run stops with an error. A malformed `to` is caught by a check. The `agent_summary` frame reports the agents' states.
