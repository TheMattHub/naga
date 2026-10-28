Use MATCH when different kinds of entity must meet before work can start: a part and its kit, an order and its customer, a driver and a vehicle. It waits for one entity of each of several types. For a count of entities of any kind, such as a pallet of ten, use [BATCH](batch.md).

## Behaviour and rules

`number_to_match` is the number of distinct entity types that must be present, at least 2. Two entities of the same type never match each other. Each arrival is stored first, so it can never be lost. The block then checks whether that many types are waiting.

When they are, it takes the oldest waiting entity of each type, starting with the type that has waited longest. Within a type the order is first in first out. With `match_on` set to an attribute, only entities with an equal value of that attribute match, and a missing attribute stops the run with an error.

With `batch_option` on, the matched entities become one batch entity of type `batched_entity_type`, which a SEPARATE can split. With it off, they leave one after the other in the same instant along the single outgoing link. Unmatched entities wait for ever, and the `store_summary` frame shows them.
