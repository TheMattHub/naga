# Units

Every quantity in a model names its unit: a duration, a length, a speed, a mass,
an energy, a power or a rate per time. You never convert by hand.

## The model's units

`simulation.time_unit`, `simulation.length_unit` and `simulation.currency` are
the default unit of every field that names none and the unit of results, KPIs,
charts, the clock, the run length and the warm-up.

An order's release and due times, a patch tick, the initial state's time and
entity ages and a conveyor's transit time have their own unit too, like every
other field. A dated order keeps its own clock.

Two limitations: an expression that reads the clock (TNOW, HORIZON) or passes
times to STEP, PULSE, RAMP or an SD delay/smooth function reads them in the
model's time unit and is not converted, so they must be updated by hand when
the model's time unit changes. And a
factor that is not exact in binary (minutes to hours is 1/60) can reorder two
events scheduled for the same instant, so a run may differ in its last digits
where minutes to seconds is bit-identical.

## Field units

In the model file a unit is a sibling key, `<field>_unit` (`time_unit` for a
distribution; see the "Unit key" column of the [block reference](../reference/blocks.md)).
A file written before units existed opens with every missing or `"base"` unit
filled in with the unit it already meant, so it keeps its meaning.

| Quantity | Units |
|---|---|
| time (and rates per time) | milliseconds, seconds, minutes, hours, days, weeks, months (30 days), years (365 days) |
| length | millimeters, centimeters, meters, kilometers |
| speed | m/s, m/min, km/h, or any length/time such as mm/s |
| mass | g, kg, t |
| energy | Wh, kWh, MWh |
| power | W, kW, MW |
