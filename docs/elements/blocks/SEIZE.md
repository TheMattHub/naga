Use SEIZE with [RELEASE](release.md) when one resource must stay with an entity across several blocks. Examples are an operator who follows a job through three steps, a forklift held during loading, and a bed kept for a whole stay. For a single service step on one resource, [PROCESS](process.md) is simpler and does the same in one block.

## Behaviour and rules

The entity requests `quantity` units of the resource or resource set and waits in its queue. The lower `priority` number is served first, and equal priorities follow the resource's queue discipline. A request for several units is granted whole, so a waiting entity never holds part of it. For a set, the set's rule picks the member.

After the grant the entity spends the block's own delay, which may be 0, and moves on still holding the units. It keeps them until a RELEASE names the same resource. An entity cannot seize a name it already holds. It stops with an error that names the entity and the resource.

With `balk_if` or `renege_after`, an entity can leave without service by the second outgoing link, and the block needs that link. Preemption is not offered here, only in PROCESS. A failure or shift end does not interrupt a holder. The unit goes out of service when it is freed.

A finite `queue_capacity` limits the places in the block's own queue. The `resource_summary` frame reports seizes, utilization and queue length for the resource.
