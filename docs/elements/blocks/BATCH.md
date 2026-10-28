Use BATCH when entities travel or are processed together as one unit: a pallet, a lot, a tray, a truck load. It groups any entities by count. If you need one entity of each of several different kinds, such as a part and its kit, use [MATCH](match.md) instead. [SEPARATE](separate.md) splits a batch again.

## Behaviour and rules

Each arrival waits in the block's store. When `batch_size` entities are waiting, the block takes exactly that many and sends one new entity of type `batched_entity_type` down the link. The members stop counting as work in progress and carry the batch id until a SEPARATE splits them. Their costs move into the batch.

With `max_wait` set, a partial batch also leaves. The timer starts when the first member of a new group arrives. A batch that fills in the same instant wins, and the timer releases nothing. Without `max_wait`, entities wait for a full batch for ever.

Setting `carrier_type` turns the block into a loading dock. An arriving entity of that type, such as a truck, is not stored. It loads the waiting entities one at a time, each taking `member_time`, until it holds `carrier_capacity` members or its `max_wait` has passed. Then it leaves as itself, with its members aboard. A negative `carrier_capacity` is an error.

Waiting entities show in the `store_summary` frame, with their average size and time in the store.
