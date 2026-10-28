These sections model quantities that change continuously: a tank, a battery, a stock, an energy bus. A `level` is the quantity with its bounds. A `flow` moves it at a rate. A `conversion` turns a feed into a product that is released later, such as biogas from feedstock. Use them when counting entities is the wrong view.

## Behaviour and rules

A flow rate is an expression that is re-evaluated only at events, so it is constant between them. Every level is then a straight line between events, and the engine solves for the instant a level reaches a bound or a threshold. There is no fixed time step unless `simulation.sd` asks for one.

The engine enforces the bounds. A level at its `min` passes on no more than flows into it, and a full level scales its inflows down. What is lost is recorded as `unmet` for demand not served and as `spilled` for overflow. A rate needs no guard for an empty level. A negative rate is clamped to 0 unless the flow is a `biflow`.

A `price` is money per unit moved and an `emission_factor` is an impact per unit. Both are integrated exactly. A `peak_price` is a demand charge, billed once at the end of the run. A flow with a `tag` is summed by `FLOWSUM("tag")`, which is how many assets share one site bus.

A TRANSFER block moves a lump between an entity and a level. A conversion releases its product after a delay that follows a yield curve, in cohorts closed every `cohort_interval`.
