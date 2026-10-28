Use RELEASE to free a resource that an earlier [SEIZE](seize.md) or a PROCESS with the action `Seize Delay` gave the entity. It ends a hold that spans several blocks. A PROCESS with the default action frees its own resource, so it needs no RELEASE.

## Behaviour and rules

The block waits for its optional delay and then frees every unit the entity holds under `resource_name`. For a set, it frees the member that was chosen at the seize. Naming a fleet frees a vehicle that a TRANSPORT kept.

If the entity holds nothing under that name, the run stops with an error that names the entity and the resource. This is deliberate, so a missing SEIZE does not pass unnoticed. A resource name that is not declared is also caught by a check.

The end of a SEIZE and RELEASE pair blocks like the end of a PROCESS. If the next block has a finite space, a finished part keeps its resource until the space accepts it. A worker that leaves when the work ends is the one exception, unless `worker_when_blocked` is `stays`.

The hold is recorded when it ends, so the busy time, the busy cost and the schedule log all count from the seize to this block.
