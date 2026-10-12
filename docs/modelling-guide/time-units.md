# Time units and transfer delays

## The model's base time unit

`simulation.time_unit` (default "minutes") is the unit every KPI, the clock, the run length and
the warm-up are expressed in, and the default unit of every new field. Set it once per model (see [Units](units.md)); a tool that changes it later must convert the run
length and the warm-up, since every other field keeps its own unit.

## Per-field units

Every duration-bearing field (a distribution's `time_unit`, a schedule's point durations, a
failure's uptime/downtime, a link's transfer delay) carries its own unit: `milliseconds`,
`seconds`, `minutes`, `hours`, `days`, `weeks`, `months` or `years`. This lets you enter a service
time in seconds and an arrival schedule in hours inside a model whose base unit is minutes,
without doing the arithmetic by hand.
A file written before units existed (a missing unit or `"base"`) opens with the unit it already
meant filled in.

An arrival schedule's *rate* values convert with the inverse factor (a rate, not a duration); a
capacity schedule's values (unit counts) never convert.

## Link transfer delays

A link between two blocks may carry a `delay` (a duration spec, same shape as any block's
distribution + `dist_params` + `time_unit`). When present, `forward()` samples it on the
*source* block's own RNG and runs a transfer process: the entity stays live and "in transit" for
that duration, and the transfer time counts toward its throughput time. With no delay declared,
a link is still zero time, exactly as before -- adding transfer delays never changes an existing
model that does not use them.

Use a transfer delay for simple travel time between two blocks. For a routing network, vehicle
fleets or a conveyor belt, see [Layout view, paths and vehicles](layout-paths-vehicles.md) (STATION/
ROUTE/TRANSPORT/CONVEYOR).

## Playback

In a recorded run an entity in transit is placed along its link at the position its travel time
gives, so a replay shows the delay.
