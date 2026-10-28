Use DELAY for waiting that needs no machine, operator or other resource: transit, curing, cooling, a customer reading a menu. Any number of entities can wait at the same time. When the wait is a service that uses a resource, or when the resource's queue matters, use [PROCESS](process.md). For a belt with a capacity, use [CONVEYOR](conveyor.md).

## Behaviour and rules

The entity waits for one sample of the distribution and then moves on. The block has no capacity limit and no queue, so it never holds an entity longer than its sampled time. The distribution may be an expression that reads the entity's attributes.

A sampled duration below 0 is cut to 0, and a warning is logged on the first one. A distribution that can go negative, such as Normal, raises the warning W_UNBOUNDED_DURATION in a check. A time unit set on the block converts the sample to the model's unit.

In a deterministic run, the wait is the distribution's mean instead of a draw. The block adds no frame of its own. The waiting entities count as work in progress in the system results.
