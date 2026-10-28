Use DISPOSE where entities leave the model: shipped orders, served customers, finished parts, scrap. Every flow that does not loop back to a CREATE must end in one. Disposal is also where throughput time is recorded, so the point where you place it defines what counts as finished.

## Behaviour and rules

When an entity arrives, it is marked finished. After the optional deletion delay, the block stamps its deletion time and its throughput time, which is the time since creation. The entity is then removed from the work in progress and counts in the throughput results.

If the entity type has a holding cost, the block adds that cost for the time spent in the system. Only time after the warm-up counts, so an entity created before the warm-up ended is charged from the end of the warm-up.

A DISPOSE has no outgoing link, and adding one is an error. With `remove_agent` on, the entity's agent is removed too, unless it has none or has already died. A model with no DISPOSE fails a check. Entities that never reach one stay in the work in progress at the end of the run.
