Use DUPLICATE when one entity must become several that carry on independently. Examples are an order that spawns picking tasks, a job that needs work in two places, and a message for several recipients. It differs from [SEPARATE](separate.md), which splits a batch back into the entities that formed it. To send the copies down different branches, follow DUPLICATE with a DECIDE.

## Behaviour and rules

`number_of_duplicates` is the total number of entities that leave, the original included, and must be at least 2. The original goes first, unchanged. The copies follow in the same instant, each as a new entity with its own id.

All of them leave by the block's single outgoing link. A copy takes the original's attribute values as they are at that moment, in a shallow copy. A copy does not take the original's name, id or resource holds. A list of batch members is copied into its own list.

Each copy is announced as a new entity, so it counts in the entities created and in the work in progress until it is disposed. The block takes no time.
