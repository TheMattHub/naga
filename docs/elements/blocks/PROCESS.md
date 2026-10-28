Use PROCESS for a machine, workstation or service desk: an entity waits for a resource, is served for a time, and frees the resource. It fuses [SEIZE](seize.md), [DELAY](delay.md) and [RELEASE](release.md) in one block. Use the separate blocks when one resource must be held across several steps. Set `action` to `Delay` for a plain wait that needs no resource.

## Behaviour and rules

The `action` picks the steps that run. `Seize Delay Release` is the default. `Seize Delay` keeps the resource for a later RELEASE. `Delay Release` frees a resource that an earlier block seized. `Delay` uses no resource. A resource name is required unless the action is `Delay` or a worker fleet does the work alone.

An entity that finds no free unit joins the queue. The lower `priority` number is served first, and equal priorities follow the resource's queue discipline, which is first in first out by default. A request for several units is granted whole, so a waiting entity never holds part of it. A negative service time is cut to 0.

With `balk_if` or `renege_after`, entities can leave without service by a second outgoing link, and the block needs that link. With `preempt` on, an entity can take a unit from the lowest-priority job in service when its own priority is strictly better. Preemption works only with `Seize Delay Release` and not on a resource set. The preempted block's `on_preempt` decides whether the job resumes or restarts.

A failure or a shift end never interrupts a job in service. The unit goes out of service when the job frees it. A `setup` time is charged after the seize when the setup key differs from the previous job's at this block. The first job pays none unless `initial_key` is set.

A finite `queue_capacity` or a full downstream space makes a finished part keep its machine until it is accepted. The `resource_summary` frame reports utilization and queue length. The `queue_summary` frame has a row for the block when it declares balking, reneging or preemption.
