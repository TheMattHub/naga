# Continuous quantities: levels, flows and time-series inputs

Some things in a system are not entities. Energy in a battery, volume in a tank, units on a shelf
and money in an account are **continuous quantities** that rise and fall at a *rate*. The engine
models them with three sections that sit beside `resources` in the model file, all of them data
rather than blocks:

| Section | What it is | Read from an expression with |
|---|---|---|
| `timeseries_inputs` | a named profile the model reads: a PV curve, a load, a tariff by hour | `TS("name")`, `TS("name", t)` |
| `levels` | a continuous quantity with bounds | `LEVEL("name")` |
| `flows` | a rate-based transfer between levels | `FLOW("name")` |

## The one rule that makes this cheap

**A flow rate is an expression that is re-evaluated only at events.** Between two events it is
constant, so every level moves along a *straight line*, so the instant it reaches a bound or a
threshold is `(target - level) / rate` -- solved in closed form and scheduled as one event. There
is no ODE solver and no fixed time step: a week of a battery on an hourly tariff costs about 170
events, not 10 080.

The events that re-evaluate the rates are:

* a time-series change instant (the next knot of a profile),
* a level reaching its capacity, its minimum, or one of its thresholds,
* an ASSIGN writing a model variable, or a TRANSFER block moving a lump,
* a pending TRANSFER demand becoming servable.

## Time-series inputs

Two authoring shapes:

```json
"timeseries_inputs": [
  {"name": "pv", "start": 0, "step": 1, "time_unit": "hours",
   "values": [0, 0, 0.8, 2.8, 4.2, 3.5, 1.6, 0],
   "interpolation": "step", "beyond": "repeat"},

  {"name": "price", "times": [0, 8, 18, 22], "values": [0.15, 0.25, 0.40, 0.15]}
]
```

| Field | Meaning |
|---|---|
| `start` + `step` | a regular profile: 24 hourly values, 96 quarter-hourly ones |
| `times` | an irregular one; strictly increasing, one per value. Exclusive with `step` |
| `time_unit` | the unit the times are in; `base` (the default) means the model's own |
| `interpolation` | `step` (piecewise constant, the default) or `linear` |
| `beyond` | past the last sample: `hold` the last value, `repeat` the series, or `error` |
| `period` | what `repeat` cycles on; defaults to `step * len(values)` -- 24 hourly values = one day |

Below the first time the first value always applies.

!!! note "step vs linear for a flow rate"
    A `step` series driving a flow rate is **exact**: the rate really is constant between knots.
    A `linear` one is sampled at the knots and held constant in between, because a continuously
    varying rate would make the level a curve rather than a line. `TS()` read from an ordinary
    expression still interpolates. Use `step`, or add knots, when the difference matters. Under
    `simulation.sd` a `TS()` rate is also re-read at every grid point, so a `linear` series is
    integrated at `dt`.

## Levels

```json
"levels": [
  {"name": "Battery", "unit": "kWh", "capacity": 10, "min": 0, "initial": 2,
   "max_inflow": 3, "max_outflow": 3, "rate_time_unit": "hours",
   "thresholds": [{"value": 1, "direction": "falling", "signal": "battery_low",
                   "variable": "low_since"}]}
]
```

`max_inflow` and `max_outflow` are power limits: the largest total rate that may be added to or
removed from the level (a charge and a discharge rate). Leave `capacity` out for an unbounded
accumulator.

A **threshold** is a level value that fires discrete logic when the level crosses it. Crossing it
succeeds the named `signal` -- which releases every HOLD block waiting on it, exactly as a SIGNAL
block would -- and writes the level into the named `variable`. That is how a reorder point, a
low-battery alarm or a high-water alarm reaches the entity side of the model.

## Flows

```json
"flows": [
  {"name": "charge", "to": "Battery", "efficiency": 0.95,
   "rate": "max(TS(\"pv\") - TS(\"load\"), 0)", "rate_time_unit": "hours"},
  {"name": "grid_import", "rate": "max(TS(\"load\") - TS(\"pv\"), 0)",
   "price": "TS(\"tariff\")", "emission_factor": 0.35}
]
```

`from` and `to` name levels; leaving one out means a source or sink outside the model (the sun,
the grid, demand). A flow with **neither** end on a level is an *accounting flow*: it accumulates
its own total, cost and impact and nothing else -- which is exactly what a grid import or a
metered purchase is.

`efficiency` is the loss: `out = in * efficiency`. `rate` is the quantity drawn per time unit at
the *source* side.

That time unit is the flow's `rate_time_unit`, the model's own when it is left out. Everything the
engine reports about the flow's rate uses the same unit: `FLOW()`, `FLOWSUM()`, the flow's trace,
Avg Rate, Peak Rate, a rate threshold, `min_rate` and `peak_price`. A flow written in kW with
`"rate_time_unit": "hours"` therefore reads and reports kW, even in a model that counts minutes.
A rate that reads `FLOW()`, `FLOWSUM()` or `CONV()` of something per another time unit takes the
number unconverted, so validation warns about it (`W_RATE_UNIT_READ`). A name built from a
submodel parameter (`FLOWSUM("biogas" + circuit)`) is checked as the literal it becomes; a name
computed at run time is checked against every record it may read, and when those differ in time
unit validation says the read cannot be checked (`W_RATE_UNIT_UNCHECKED`). That warning is
conservative: the name may only ever read records in the right unit. Writing the name as a
literal, or giving every record the same time unit, removes it.

## What the engine enforces: saturation

You never have to write "and stop when it is full". At every re-evaluation the engine scales flows
**down**, proportionally within the limited side, until every bound holds:

1. `max_inflow` / `max_outflow` cap the total rate in or out;
2. a **full** level scales its inflows to its outflows -- the difference is **Spilled**;
3. an **empty** level scales its outflows to its inflows -- the difference is **Unmet**.

Two flows competing for the same bound keep their ratio. If you need a merit order instead, write
it into the rate expressions -- `FLOW("name")` reads what another flow actually managed, after
saturation, so an accounting flow can be defined in terms of a physical one.

## The site bus: FLOWSUM

`FLOW("name")` needs a name. When you do not know how many contributors there are -- two PV arrays,
three loads, a battery someone adds later -- give the flows a **tag** and read the total:

```json
{"name": "PV", "rate": "TS(\"sun\") * 6", "tag": "generation"},
{"name": "House", "rate": "TS(\"load\") * 2", "tag": "demand"}
```

| Call | What it returns |
|---|---|
| `FLOWSUM("generation")` | the realised rates of every flow tagged `generation`, added up |
| `FLOWSUM("storage_in", 2)` | only those with `priority` **below** 2 -- what the devices ahead of you in the merit order actually took |

Each flow counts in its own `rate_time_unit`, so the flows sharing a tag must share that unit; a
tag whose flows use two different time units is a validation error. An unknown tag is `0`, not an error: a site
with no generator has no generation. That is the whole
mechanism behind the bundled energy models: each device adds its power to a
named tag, the dispatchable assets read the totals, and a second battery with a higher merit number
sees exactly the surplus the first one left. Nothing is wired and nothing is renamed.

!!! note "Spilled is 'the destination refused it', not 'it was destroyed'"
    Spilled counts quantity a level could not accept. Where it actually went is the model's
    business: in the PV example the surplus the battery refuses is *exported*, and the export flow
    accounts for it.

## Losses

Only **constant-rate** losses are built in, deliberately: a proportional loss `dL/dt = -kL` is
exponential, which would break the straight-line exactness the whole design rests on. A constant
leak is a flow out of the level with a constant rate. A self-discharge proportional to the level
is a flow whose rate reads `LEVEL("name")`; it is exact at every event and a zero-order hold in
between, and you choose the resolution by adding thresholds or a decision clock.

## Moving a lump: the TRANSFER block

Everything above is rate-based. The one place a discrete entity touches a level is the **TRANSFER**
block: a truck unloading 20 t, a customer drawing 5 units.

| Param | Meaning |
|---|---|
| `level` | the level to change |
| `amount` | an expression -- `20`, `entity.quantity` |
| `mode` | `remove` or `add` |
| `wait` | queue until the level can pay the full amount (the backorder), or move what it can |

With `wait` off, the shortfall is recorded as the level's `Unmet` -- a lost sale, an overflow. With
`wait` on, the entity queues FIFO and the engine schedules the *exact* instant the level reaches
the amount, so the wait is solved, not polled. An amount the level never reaches waits for the rest
of the run, the same contract as HOLD's "Infinite" mode.

## Delayed conversion with a yield curve

A digester fed today releases biogas over the next ten days; a curing oven, a fermentation and a
compost windrow are the same shape. That is not a flow -- the output rate now depends on the whole
history of what went in, per feedstock. It is its own section, `conversions`.

```json
{"name": "Digester", "cohort_interval": 1, "time_unit": "hours", "unit": "Nm3",
 "substrates": [
   {"name": "maize", "feed": "FLOW(\"Feed maize\")", "yield": 200.0,
    "half_life": 60, "steps": 12, "components": {"ch4": 0.52}},
   {"name": "slurry", "feed": "FLOW(\"Feed slurry\")", "yield": 25.0,
    "curve": [[24, 0.5], [72, 0.85], [180, 1.0]], "components": {"ch4": 0.60}}]}
```

| Field | Meaning |
|---|---|
| `feed` | the substrate's feed RATE, an expression -- usually `FLOW("<feeder>")` |
| `feed_time_unit` | the unit `feed` is per; give it the feeder flow's `rate_time_unit` |
| `yield` | product per unit of feed (200 Nm3 of biogas per tonne) |
| `curve` | the release curve as `[age, CUMULATIVE share]` knots, ending at 1.0 |
| `half_life` | instead of a curve: a first-order release, cut into `steps` equal-width steps |
| `components` | the share of THIS substrate's product that is each component (see below) |
| `initial_inventory` | feed already inside at t=0, released as one cohort |
| `initial_feed_rate` | start in the STEADY STATE for that feed rate -- no start-up transient |

The product is read by `CONV("name")` and enters the model as an ordinary flow's rate, which is
what gives it a destination level, saturation and spill when that level is full, a bus tag, a price
and an impact factor for free. `CONV()` and the conversion's Avg and Peak Rate are per the
conversion's `time_unit`, so the reading flow takes the same `rate_time_unit`:

```json
{"name": "Biogas", "to": "Gas holder", "rate": "CONV(\"Digester\")", "rate_time_unit": "hours"}
```

Mass leaving the source in the product is the same pattern: a second flow out of the digester level
whose rate reads `FLOW("Biogas") * density`.

**Why it stays exact and cheap.** Feed is accumulated and closed into a cohort at a fixed
`cohort_interval`, and every curve knot age is rounded onto that same grid. So the product rate
changes only at grid instants, and it is piecewise constant like every other rate in the engine. A
cohort close is `steps + 1` dictionary writes -- the live cohorts are never scanned -- so a YEAR of
hourly cohorts with three substrates and 12-step curves costs 8760 events, not 3 x 8760 x 12, and
runs in about 0.2 s.

**The two approximations, stated.** A cohort releases from its CLOSE, so the mean delay of what
went into it is longer by up to half an interval (exactly half for a constant feed); halving
`cohort_interval` halves it. Knot ages round to the grid, at most half an interval each. Neither
touches conservation: `Total In x yield = Produced + Unreleased`, exactly, and `Unreleased` is
tracked as a state, so it survives a warm-up that resets the other statistics.

A `half_life` curve spans `tail` half-lives (6 by default, 98.4% of the yield) and the remainder is
renormalised in rather than lost -- so the yield is conserved and the tail releases very slightly
early.

## Blended quality: components on a level

A gas holder is not just a volume, it is a volume at some methane share. Declare the components a
level tracks and the engine keeps their ABSOLUTE content, not a ratio:

```json
{"name": "Gas holder", "capacity": 1000, "initial": 300,
 "components": [{"name": "ch4", "initial_fraction": 0.53}]}
```

* an **inflow** carries the share it declares (`"components": {"ch4": 0.52}`, a number or an
  expression such as `FRACTION("Digester", "ch4")`), or, with nothing declared, the blend of the
  level it draws FROM -- so a chain of blended tanks needs no annotation at all;
* an **outflow** draws at the level's fraction as of the last evaluation (a zero-order hold);
* `FRACTION("level", "component")` reads the share now, and `FRACTION("conversion", "component")`
  the blend a converter is releasing now. `FLOW("gas") * FRACTION("Holder", "ch4") * 9.97` is the
  kWh of methane a CHP is burning.

**Conservation is exact, the hold is not.** The level loses exactly what its outflows carry away,
so the component can never be created or destroyed whatever the hold is worth. The hold's error is
bounded by `blend_tolerance` (default 0.005, absolute): both content and level are linear between
events, so the fraction is a ratio of two linear functions and the instant it drifts that far is
solved in closed form and scheduled. With no inflow the hold is EXACT -- a draining tank keeps its
blend to the last unit and never re-evaluates.

A TRANSFER lump carries the level's own blend, so it moves the content and leaves the fraction
alone.

## Equipment state and flow thresholds

`AVAILABLE("resource")` is a resource's capacity in force right now: the maximum, minus what a
capacity schedule or a failure has taken out of service. Units an entity happens to hold are NOT
deducted -- a CHP is not seized by anybody -- so it is the right number for continuous equipment
that a flow merely *uses*. (`IDLE("resource")` is the same minus what is seized, for the rarer case
where entities and a flow share a machine.)

```json
{"name": "CHP gas", "from": "Gas holder", "min_rate": 200,
 "rate": "AVAILABLE(\"CHP engine\") * 250",
 "thresholds": [{"value": 20, "direction": "rising", "cost": 25,
                 "cost_item": "CHP starts", "category": "Operations"}]}
```

A rate reading `AVAILABLE()` is re-evaluated the instant that resource's state changes -- the
engine is woken by the failure or the schedule itself, never polled -- so a 12 h outage stops the
flow for exactly 12 h.

**Flow thresholds** mirror a level's, on the REALISED rate: they fire a `signal`, write a
`variable`, and accrue one lump `cost` per crossing into `cost_item` (see [Costing](costing.md)).
Rising crossings are counted as the flow's `Starts`, so a generator's start cost is
`Starts x cost` and both are reported. Put the threshold just above zero (or at the minimum load)
-- the crossing test is half-open at the arriving end, so a threshold AT 0 can never rise onto it.

**`min_rate`** makes a flow semi-continuous: it runs at 0 or at least `min_rate`, never in between.
The gate is applied to the rate the expression ASKED for, before saturation, so it schedules
nothing of its own and cannot chatter -- the flow can only switch at an event that was going to
happen anyway. For a minimum on-time, build the hysteresis in the model: two level thresholds
writing one variable (on above X, off below Y) that the rate multiplies by. The
the bundled model `biogas_digester_demo` does exactly that.

## Control

A rate is an ordinary expression, so a rule-based dispatch is just arithmetic over `TS()`,
`LEVEL()` and model variables. For a *trainable* policy, use the existing Policy-mode DECIDE as a
**decision clock**: a CREATE block ticking at the decision interval feeds a Policy DECIDE whose
`observation` reads `LEVEL(...)`, each branch an ASSIGN that writes a setpoint variable the flow
rates read. Writing a variable re-evaluates the rates immediately, so the setpoint takes effect at
that instant. `nagasim.rl.SimEnv` drives that loop unchanged.

## What you get back

| Frame | Columns |
|---|---|
| `level_summary` | Average (time-weighted over the straight-line curve), Min, Max, Final, Time Empty %, Time Full %, Total In, Total Out, Spilled, Unmet |
| `flow_summary` | Total, Avg Rate, Peak Rate (the highest rate actually held -- a demand charge's peak power), Time Limited %, Cost, Impact, Starts |
| `conversion_summary` | one row per `<conversion>/<substrate>`: Total In, Produced, Unreleased, Avg Rate, Peak Rate |
| `component_summary` | one row per `<level>/<component>`: Avg Fraction (mass-weighted, exact), Min, Max, Final Fraction, Total In, Total Out, Final Content |
| `cost_summary` | a `peak` row per flow that carries a `peak_price`: its Peak Rate times the tariff, billed once (see [Costing](costing.md)) |
| `system` | `Total Flow Cost` and `Total Impact`, present only for a model that has flows; flow cost joins `Total Cost`, and so does the demand charge |
| `timeseries` | a `level/<name>` curve per level |

Every one of those is addressable as a scenario or optimizer response:
`level/Battery/Average`, `flow/grid_import/Total`, `system/Total Cost`.

## Worked examples

* the bundled model `energy_pv_battery` -- profiles, a tariff, a battery
  with power limits and round-trip efficiency, a rule dispatch, and battery sizing with the
  optimizer.
* the bundled model `inventory_sS` -- a level as stock, a threshold
  as the reorder point, a TRANSFER that waits.
* the bundled model `energy_site_library` -- the same site
  as four submodels meeting on the bus, sized with the optimizer.
* the bundled model `supply_chain_library` -- stock
  as a level, customers as entities, and one echelon's lead time as the next one's queue.
* the bundled model `biogas_digester_demo` -- a delayed conversion with
  a yield curve per feedstock, a blended gas holder, and a CHP stopped by its own maintenance.
