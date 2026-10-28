These sections define what entities wait for and when it is available. `resources` are the machines, staff and spaces that SEIZE and PROCESS blocks request. `resource_sets` group resources so that a request takes any one of them. `schedules` give a resource a shift pattern or give a CREATE block a changing arrival rate. `failures` break resources down and repair them.

## Behaviour and rules

A resource has a `capacity` of units, a queue discipline and optional costs. The discipline is `FIFO` by default. The ranked disciplines `LowestValueFirst` and `HighestValueFirst` need a `ranking_expr`. With `rerank` on, the rank is re-evaluated before every grant.

A resource set is seized by its own name. The `rule` picks one member for each request. `PreferredOrder` tries the members in the listed order, and `Cyclical` takes turns. The member that was chosen is the one that is released later.

A schedule is a list of `[duration, value]` points. The value holds for the duration. With `repeat`, the pattern starts over, and without it the last value holds for ever. For a `capacity` schedule the value is a number of units. For an `arrival` schedule it is a rate per time unit. A weekly grid needs `simulation.calendar`.

A schedule or a failure never interrupts a job in service. The unit goes out of service when the job frees it. A resource is built at its full `capacity`, which must cover the highest value its schedule asks for. Each failure draws its own uptime and downtime for each resource it lists.
