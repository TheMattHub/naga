Use these functions in an agent type's rates, conditions and actions, or in a model expression that reports on a population. `COUNT` is the one most models need: it counts the live agents of a population, or those in a state, such as the infected in an epidemic. `AGENT` and `ALIVE` handle references to single agents. The `LINK` functions read the agents that self is linked to.

## Behaviour and rules

`COUNT("pop", "state")` is a direct lookup and costs the same however many agents there are. With two more arguments it counts the agents whose categorical attribute holds that value. `AGENT("pop", i)` is a reference to agent number i. It is what `entity.agent` holds, and a send takes it. A reference reads an attribute with `AGENT("pop", i).attr`.

`ALIVE(ref)` tells whether the referenced agent still exists. Using a reference to an agent that was removed stops the run with an error, so test `ALIVE` first when agents can die.

The link functions are directed: they follow the links that go out from self. `LINKED` counts them. `LINK_SUM` and `LINK_MEAN` aggregate an attribute over them, and give 0 when there is none. `IS_LINKED` tests one agent. The link functions work only inside an agent's own expressions, where self is defined.

An unknown population name is reported by a check.
