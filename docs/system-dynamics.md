# System dynamics

Stocks and flows with feedback, alongside the ordinary block flow chart -- the same model file,
the same `run_model`.

## The grid

`simulation.sd = {"dt": 0.25, "method": "euler"|"rk4"}` turns the level engine into a fixed-step
integrator. Without it nothing changes: a level re-evaluates only at events, exactly as before, so
every older model stays bit-identical. With it, a level whose rate reads `LEVEL`, `FRACTION`,
`RAMP` or `TNOW` -- a DYNAMIC rate, one that can change between events on the clock alone -- is
re-integrated at every grid point. `euler` is the ordinary step; `rk4` replaces a dynamic rate by
its effective value over the step (fourth-order accurate, still exact at a crossing or a
threshold, since levels stay piecewise-linear inside a step either way).

## Auxiliaries, delays, functions of time

* **Auxiliaries** (`AUX("name")`) are named expressions, evaluated on read and memoised for one
  pass -- a computed variable with no stock of its own (a price, a ratio, a policy).
* **Delays** (`DELAY("name")`) are `material` (a pipeline, conserves what enters), `information`
  (SMOOTH, an exponential lag) or `pipeline` (an exact, dt-free history read back `tau` ago).
  Shorthand `SMOOTH`/`SMOOTH3`/`DELAY1`/`DELAY3`/`DELAYN`/`DELAYP` inside a flow rate, an
  auxiliary or a delay's own input expands into hidden stages automatically; with no initial
  argument it starts in equilibrium at the input's t=0 value, as in Vensim.
* `STEP(height, t0)`, `PULSE(height, t0, width)`, `RAMP(slope, t0[, t1])` and a lazy
  `IF(cond, a, b)` are ordinary expression functions; a literal switching time is exact even
  without a grid.

## Function pack

Beyond `STEP`/`PULSE`/`RAMP`/`IF`, an expression anywhere in a level's initial value, a flow rate,
an auxiliary or a delay field may call:

* `ZIDZ(a, b)` -- `a/b`, or 0 when `b` is 0. `XIDZ(a, b, x)` -- `a/b`, or `x` (default 0) when `b`
  is 0. `MODULO(a, b)` -- Vensim's remainder, with the sign of `a`.
* `TABLE_EXTRAPOLATE("name", ...)` -- like `TABLE`, but a `linear` axis keeps following the slope
  of its last segment past the ends instead of clamping.
* `PROD(x)`, `VMIN(x)`, `VMAX(x)` -- like `SUM`/`MEAN`, the product/minimum/maximum over the `[*]`
  elements of a subscripted quantity (`PROD(LEVEL("Cost[*]"))`).
* `INITIAL(x)` -- the value of `x` at the start of the run, held for the rest of it.
* `ACTIVE_INITIAL(x, init)` -- `init` while a level's OWN initial value is being computed, `x`
  everywhere else (a flow rate, an auxiliary, another level or delay's field) -- Vensim's rule for
  breaking an initialisation-order tie.
* `SAMPLE_IF_TRUE(cond, x, init)` -- holds the last `x` read while `cond` was true, `init` before
  the first true reading; updated at every event and, with `simulation.sd`, every grid point.
* `TREND(x, avg_time[, init_trend])` and `FORECAST(x, avg_time, horizon)` -- the fractional growth
  rate of `x` (an internal SMOOTH), and a linear projection `x * (1 + trend * horizon)`.
* `NPV(stream, discount_rate, init, factor)` -- the present value of `stream` since the run
  started, Vensim's own formula: `(cum + stream * dt * df) * factor`, where `df` decays from 1 at
  `rate / (1 + rate * dt)` and `cum` accumulates `stream * df` from `init` (dt: the SD grid step).
* `NOISE_NORMAL(mean, sd, lo, hi)` -- a normal draw clipped to `[lo, hi]`, redrawn every
  re-evaluation from a stream fixed by the call's own position in the model (reproducible across
  runs of the same seed); refused under `"method": "rk4"` (it would draw four times per step).

`INITIAL`/`ACTIVE_INITIAL`/`SAMPLE_IF_TRUE`/`TREND`/`FORECAST`/`NPV`/`NOISE_NORMAL` are expanded
into hidden levels/delays when the model is built, the same way `SMOOTH`/`DELAYn` are -- they
cannot be called from a block expression (an ASSIGN, a DECIDE condition, a TRANSFER amount), only
from a level's initial value, a flow rate, an auxiliary or a delay field.

## Conveyors

A level with a `conveyor: {transit_time, capacity?, in_limit?, arrest?, leak_kind?}` field is a
Stella conveyor instead of an ordinary stock: whatever enters
keeps ITS OWN transit time (a later edit never reaches back into material already on the belt), it
moves forward one slat per grid step, and it leaves through exactly one **primary outflow** with no
rate of its own (`"rate": ""`) -- the amount reaching the end, not a rate you write. A **leak** flow
(`"leak": {"fraction": "<expr>", "zone"?: [start, end]}`, `zone` a fraction of the belt from the
inflowing side, default the whole thing) drains a fraction of what remains as it travels;
`leak_kind` is `"linear"` (a constant fraction lost per step) or `"exponential"` (a fraction of
what remains, compounding). `capacity`/`in_limit` cap what an equation-driven inflow may add per
step; `arrest` (a boolean-like expression) freezes the belt entirely while true -- nothing moves,
nothing leaks, nothing is admitted. Needs `simulation.sd` on method `"euler"` (the slat model moves
one slat per DT; RK4's stages have no meaning for it): `E_CONVEYOR_GRID`/`E_CONVEYOR_METHOD`
otherwise, `E_CONVEYOR_OUTFLOW` for anything but exactly one primary outflow,
`E_CONVEYOR_LEAK` for a leak on a flow that is not a conveyor's own outflow or a zone outside
`[0, 1]`, `W_CONVEYOR_TRANSIT` when a literal `transit_time` is not a multiple of `dt`.
`nagasim/models/sd_training_pipeline.json` is a worked example (a hiring pipeline with dropouts).

## Subscripts and units

`subscripts: [{"name": "region", "elements": ["north", "south"]}]` declares a dimension; giving a
level, flow, auxiliary or delay `"dims": ["region"]` flattens it into one record per element
(`Pop[north]`, `Pop[south]`) when the model is built -- every existing feature and every KPI works
per element, with no second code path. `SUM(x)`/`MEAN(x)` unroll over a `[*]` index.

A **subrange** names a subset of another dimension's elements:
`{"name": "North", "elements": ["n1", "n2"], "parent": "Region"}`. A record can run over it
(`"dims": ["North"]`), and inside one, both `Pop[North]` and `Pop[Region]` read the owner's
element. `SUM(LEVEL("Pop[North*]"))` sums a Region-dimensioned `Pop` over North's elements only
(`[Region*]` is the same as `[*]`).

A **mapping** ties one dimension to another element by element:
`{"name": "Region", "elements": ["r1", "r2"], "map_to": {"dim": "Zone", "elements": ["z2", "z1"]}}`
(without `elements`, by position, so the sizes must match). Inside a record over Region,
`AUX("k[Zone]")` reads the Zone element mapped to the owner's (r1 reads `k[z2]`), and a flow's
`"to": "Stock[Zone]"` ends on it. When the mapping is one-to-one it also works backwards: inside a
record over Zone, `[Region]` reads the Region element mapped onto it.

**Per-element equations** override a subscripted record's shared expression for some elements:
`{"name": "k", "dims": ["Region"], "expr": "0.1", "elements": {"r2": "0.3"}}` (for two dims, key
by `"a,x"`). The override replaces a level's `initial`, a flow's `rate`, an auxiliary's `expr` or
a delay's `input`. Codes: `E_SUBSCRIPT_RANGE` (a parent that is not a declared superset),
`E_SUBSCRIPT_MAP` (an unknown target, or a mismatched element list), `E_SUBSCRIPT_ELEMENT` (an
override key that is not an element of the record's dims).

A declared or inferred `unit` (kg, kWh, people, ...) is checked wherever `simulation.sd` is
present: a level's unit against its flows' (flow = level unit / the flow's `rate_time_unit`; a power such as kW is its energy per
hour, so a kW flow into a kWh level checks), an auxiliary's declared
unit against what its expression implies, a table's `unit_x`/`unit_y` against a `TABLE()` call's
key and result, a time series' `unit` against what `TS()` returns. `simulation.sd.units` picks
how strict this is:

* `"warn"` (the default) -- mismatches are warnings (`W_UNIT_MISMATCH`/`W_UNIT_TIME`), never
  errors.
* `"off"` -- skip the checker entirely.
* `"strict"` -- the same two conditions are errors (`E_UNIT_MISMATCH`/`E_UNIT_TIME`) instead, and
  a level or flow with no unit at all is flagged (`W_UNIT_MISSING`, always a warning -- a missing
  unit is not itself wrong).

`ZIDZ`/`XIDZ` divide their arguments' units; `MODULO`/`INITIAL`/`SAMPLE_IF_TRUE`/`FORECAST`/`NPV`/
`PROD`/`VMIN`/`VMAX`/`NOISE_NORMAL` all keep the unit of the argument they read, hold or reduce
over; `TREND` is a fractional rate of change, `1/time`. A literal number is always a wildcard
(never a false positive), so an untyped constant multiplying a canonical rate stays clean.

## Loop dominance

**Loops That Matter** (`nagasim.loopdominance.analyse`, `naga loops model.json`, or the agent
tool `run_loop_dominance`) answers which feedback loop is driving the model's behaviour and when
that hand-off happens, from one grid run: every loop gets a relative score at every step and a
dominant set (the fewest loops explaining at least half the change).

## Import from XMILE or Vensim

`naga sdimport model.xmile` (or the `import_xmile` agent tool)
reads a Stella/iThink `.xmile`/`.stmx` file into a new model: stocks, flows, auxiliaries,
graphical functions (as lookup tables), dimensions (as subscripts), non-apply-to-all arrays
(`<element subscript="...">`, as per-element equations), with `Pop[north]`, `Pop[*]` and
`SUM`/`MEAN`/`PROD`/`MIN`/`MAX` over an array, and modules, through a small
expression translator. A `<module name="m">` referencing another `<model name="m">` in the same
file becomes a submodel instance: the referenced model's own stocks/flows/auxiliaries become
`$`-scoped records of a submodel definition (the same blockless, continuous shape the SD asset
library uses), an `access="input"` auxiliary is fed by its `<connect to="m.input" from="...">`,
and an `access="output"` variable is read from the outer model as `m.output`, a dotted name
translated into that instance's own scoped record. Nested modules (a module inside a module) are
refused, along with a stock or flow marked `access="input"`, a graphical function inside a module,
and a subscripted variable inside one (subscripts inside a submodel definition are not supported
yet). A construct this reader cannot translate at all -- a conveyor/queue stock, a dimension
mapping (XMILE 1.0 defines none), an unknown function -- is reported and left out of the model
rather than guessed at; an approximation (a bare `DELAY`, a non-`euler`/`rk4` method) is reported
too. The file is refused outright, before parsing, if it carries a `DOCTYPE`/`ENTITY` declaration
or is over 20 MB -- the same rule every file the engine reads follows.

`naga sdimport model.mdl` (or the `import_mdl` agent tool)
reads a Vensim `.mdl` file the same way, for the common subset: `INTEG` stocks (a rate that is a
plain flow name or a `+`/`-` chain of them), `WITH LOOKUP(...)` as a lookup table, `DELAY1`/`DELAY3`/
`DELAY1I`/`DELAY3I`, subscript ranges and subranges (a range of another range's elements),
`->` mappings, per-element equations (`x[s1] = ...`, `x[Region] = 1, 2, 3`) and
`SUM`/`PROD`/`VMIN`/`VMAX` over `x[Region!]`. This is a small stdlib reader, not `pysd` -- `pysd` is not
and does not become a dependency. A `:MACRO:` definition, a `GET XLS/DIRECT ...` external
data reference or an unknown function/name is reported and left out, the same way. A variable
name with spaces is read with underscores (`Stock A` becomes `Stock_A`); a dimension name with a
space is not translated. `.mdl` is often Windows-1252, not UTF-8; this reader tries UTF-8 first and falls back
to Windows-1252 rather than guessing wrong or refusing a file most Vensim installs would open fine.

## Worked example: logistic growth

```python
from nagasim.api import run_model
from nagasim.core import sd

layout = sd.add(
    {"simulation": {"time_limit": 50, "sd": sd.SDSettings(0.25, "rk4").to_dict()}},
    sd.Stock("Population", initial=10, min=0.0),
    sd.Flow("growth", 'AUX("r") * LEVEL("Population") * (1 - LEVEL("Population") / AUX("K"))',
           target="Population"),
    sd.Aux("r", "0.2"), sd.Aux("K", "1000"))

results = run_model(layout, replications=1)
print(results["level_summary"])   # Population settles near its carrying capacity, K=1000
```

## Calibration to data

Fit `r` and `K` to a measured trajectory with a `SeriesTarget`, Vensim Optimize's weighted
payoff: `sum over data points of w * (model(t) - data(t))^2`, summed over targets and averaged
over replications (common random numbers).

```python
from nagasim import calibrate
from nagasim.optimize import Factor

target = calibrate.SeriesTarget("level/Population", [(0, 10), (10, 69.5), (20, 355.5)],
                                weight="1/sd")
result = calibrate.calibrate(layout, [target],
                             [Factor("auxiliaries.r.expr", "continuous", 0.05, 0.5),
                              Factor("auxiliaries.K.expr", "continuous", 500, 2000)],
                             budget=100)
result.best, result.objective          # fitted values, payoff
result.residuals["level/Population"]   # Time, Data, Model, Residual, Weight
```

The series is any key of the `timeseries` frame (`level/<name>`, `aux/<name>`,
`agents/<pop>/<state>`); data is a DataFrame (`time_col`, `value_col`), a list of `(t, value)`
pairs, or the name of a 1-D model table keyed by time. The model is read from the sampler's grid,
linearly interpolated between samples, so data on that grid (`k * time_limit /
timeseries_points`) is matched exactly. A numeric `weight` multiplies the squared error;
`"1/sd"` measures each error in standard deviations of the data, so series in different units add
up fairly. Series targets mix freely with scalar KPI targets, and the HTML report draws model
against data for each one.

See `nagasim.core.sd` for the full authoring API (`Stock`, `Flow`, `Aux`, `Delay`, `Subscript`),
and [Security](security.md) for how `sdimport` and every other file-reading surface is
confined.

## Reality checks

A reality check (Vensim's term) pairs a TEST INPUT -- a condition imposed on the model, such as
"workforce = 0" -- with a CONSTRAINT expected to hold given it, such as "production = 0", or
"inventory never negative" with no input at all. Each check runs as its own deterministic
replication and reports pass/fail with where and when it first failed:

<!-- no-run: a fragment: the checks name the records of a production model, not of the example above -->
```python
from nagasim import modelcheck

layout["reality_checks"] = [
    {"name": "shutdown", "inputs": {"auxiliaries.Workforce.expr": "0"},
     "constraint": 'AUX("Production") <= 0'},
    {"name": "no shortage", "inputs": {},
     "constraint": 'LEVEL("Inventory") >= 0', "when": "always"},
]
table = modelcheck.run_reality_checks(layout)
table[["Check", "Passed", "Violation time", "Snapshot"]]
```

`inputs` is `{override path: value}`, the same dotted-path grammar a scenario override uses (a map
here, since a check names each path once). `constraint` is a boolean expression over
LEVEL()/AUX()/FLOW() and TNOW -- the model's own safe expression grammar -- checked live at every
SD grid point (with `simulation.sd`) or every level-engine event (without), and at every
`timeseries` sample, which also gives the Snapshot (None when the failure fell between samples).
`when` narrows which instants are checked: `"always"` (default), `"final"` (the last sample only),
or `"after <t>"`.

The agent tool `run_reality_checks` runs a model's stored checks and returns
`{"check", "passed", "violation_time", "snapshot"}` per check, without exposing the raw frames.
