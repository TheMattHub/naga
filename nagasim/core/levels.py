"""Levels and flows: continuous quantities in a discrete-event engine
(PHASE4 D1).

A LEVEL is a continuous quantity with a capacity and a minimum -- battery
energy, tank volume, an inventory position. A FLOW moves quantity at a RATE
that is an ordinary model expression (a constant, a variable, `TS()`,
`TABLE()`, `LEVEL()`, `FLOW()`). The whole design rests on one rule:

    A FLOW RATE IS RE-EVALUATED ONLY AT EVENTS, SO IT IS PIECEWISE CONSTANT,
    SO EVERY LEVEL IS PIECEWISE LINEAR BETWEEN EVENTS.

Between two events a level moves along a straight line, so the instant it
reaches a bound or a threshold is `(target - level) / rate` -- solved in closed
form and scheduled as one SimPy event. There is no ODE solver and no fixed
time step: a week of a battery on an hourly tariff costs about 170 events, not
10 080.

Events that re-evaluate the rates:

* a time-series change instant (`Series.next_change`, exact for a `step`
  series; the knot grid for a `linear` one -- see the module docstring of
  `core/timeseries_inputs.py`),
* a level reaching a bound (capacity/minimum) or one of its thresholds,
* a TRANSFER block moving quantity, or an ASSIGN writing a variable
  (`LevelSystem.trigger`),
* a pending TRANSFER's demand becoming servable,
* with `simulation.sd` (PHASE5 SD-1, D89-D91), every grid point `k * dt` --
  where only the DYNAMIC rates (they read LEVEL, FRACTION or the clock) are
  evaluated again. Euler is exactly that; RK4 replaces a dynamic rate by its
  effective rate over the step (`_rk4_rates`), so the level still moves along
  a straight chord and everything below holds unchanged,
* a literal STEP/PULSE/RAMP switching time, and the instant a pipeline delay
  releases a recorded input change (PHASE5 SD-2, `_Pipeline`).

System-dynamics delays other than the pipeline arrive here already expanded
into HIDDEN levels and flows (`core/sd.expand`); `hidden` records run like any
other and are left out of every output (`visible`).

SATURATION. The engine, not the model, enforces the bounds. At every
re-evaluation each level's inflows and outflows are scaled DOWN, proportionally
within the limited side, until every bound holds:

1. `max_inflow` / `max_outflow` (charge and discharge power limits) cap the
   total rate added to / removed from the level;
2. a FULL level scales its inflows to its outflows -- the difference is
   `spilled` (curtailed energy, overflow);
3. an EMPTY level scales its outflows to its inflows -- the difference is
   `unmet` (unserved demand, a backorder).

Proportional scaling is the simplest rule that is both sound and neutral: two
flows competing for the same bound keep their ratio. A model that needs a merit
order writes it into the rate expressions instead. Scaling one flow changes the
other end's totals, so the rule is iterated to a fixed point; rates only ever
decrease, so it converges.

LOSSES are constant-rate only, deliberately: a proportional loss
`dL/dt = -k L` is exponential, which would break the piecewise-linear
exactness the whole design depends on. A constant leak is a flow out of the
level with a constant rate; a self-discharge proportional to the level is a
flow whose rate expression reads `LEVEL("name")`, re-evaluated at every event
(exact at the events, a zero-order hold in between) -- the modeller chooses
the resolution by adding thresholds or a decision clock.

COSTS AND IMPACTS. A flow may carry a `price` and an `emission_factor`, both
expressions. Rate and price are constant between events, so the integral is
exact: `price * rate * dt` accumulated per interval. `emission_factor` is the
same mechanism with a second factor, deliberately generic ("impact per unit")
so any sustainability metric reuses it.

Engine-side module: simpy + stdlib, no pandas, no Qt. A model with no levels
never builds a `LevelSystem` at all, so it pays nothing.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from graphlib import TopologicalSorter

import numpy as np

from nagasim.core import costs, sd
from nagasim.core.base_process import deterministic_hash
from nagasim.core.conversions import build_conversions
from nagasim.core.expressions import (ExpressionError, ExpressionScope, bind_constants,
                                      compile_expression, functions_with)
from nagasim.core.quantities import SECTION_FIELDS, param, to_base
from nagasim.core.timeunits import ModelUnits, base_factor

__all__ = ["Level", "Flow", "LevelSystem", "LevelError", "THRESHOLD_DIRECTIONS",
           "TRANSFER_MODES", "DEFAULT_BLEND_TOLERANCE", "SD_METHODS", "build_level_system",
           "is_dynamic", "visible"]

THRESHOLD_DIRECTIONS = ("falling", "rising", "both")
# How far a blended level's component fraction may drift from the value its
# outflows are drawing at before the engine forces a re-evaluation (Bio-1 G2,
# ARCHITECTURE D65). Absolute, on the fraction itself.
DEFAULT_BLEND_TOLERANCE = 0.005
# What a TRANSFER block does to a level (core/blocks/transfer.py); declared
# here so `core/schema/` validates it without importing a block module.
TRANSFER_MODES = ("remove", "add")

# Quantities closer than this to a bound count as AT it. Snapping the level to
# the bound exactly is what stops a saturated level from chattering: at the
# bound the saturation rule makes the net rate exactly 0, so the next crossing
# is at infinity rather than 1e-15 away (item i, Zeno).
EPS = 1e-9
# Fixed-point iterations. Each pass only ever scales rates down, so the
# saturation loop converges; the outer loop re-evaluates rate expressions,
# which lets an accounting flow read `FLOW()` of a physical one.
# ponytail: capped iteration, last pass wins; a proper network LP only if a
# model with many chained saturated levels ever needs one.
MAX_SATURATION_PASSES = 12
MAX_EVAL_PASSES = 8

# Expression functions whose value can change BETWEEN two passes of one
# re-evaluation (ARCHITECTURE 5.19, evaluation caching). Only `_saturate` runs
# between passes, and all it writes is `flow.actual` -- which FLOW() and
# FLOWSUM() read. Everything else a rate may read (the clock, a series, a level,
# a conversion, a resource, a variable, a held blend) is frozen for the whole
# re-evaluation, so a rate that reads none of these gives the same number on
# every pass and is evaluated once.
_BUS_CALLS = frozenset(functions_with("bus"))
# A random rate must keep drawing once per pass or the RNG stream would shift,
# so it is re-evaluated unconditionally whatever it reads.
_SAMPLER_CALLS = frozenset(functions_with("sampler"))
_VARYING_CALLS = _BUS_CALLS | _SAMPLER_CALLS
# System dynamics (PHASE5 SD-1, D89-D91). A DYNAMIC rate reads something that
# moves BETWEEN events -- a level, a blend, the clock -- so it is re-evaluated
# on the `simulation.sd` grid; every other rate only changes at an event and is
# never touched by a grid wake-up. AVAILABLE/NQ and friends stay event-driven:
# they change only when a resource does, which already wakes the engine.
# DELAY (SD-2) reads a hidden stage, or a pipeline that moves on its own. TS
# (DEVLOG #129): a `linear` series moves between its knots; a `step` one
# re-evaluates to the value it already has. COUNT/SUM/MEAN/NEAR/NEAREST (and,
# PHASE6 A3, the neighbour aggregates) read the agents (DEVLOG #140), which
# also wake the system at every agent event.
_DYNAMIC_CALLS = frozenset(functions_with("dynamic"))
_CLOCK_NAMES = frozenset({"TNOW", "now"})
SD_METHODS = ("euler", "rk4")
# Playback budget for grid-only corners (D75): about this many level_changed
# records per run, whatever dt and the level count are.
TRACE_BUDGET = 20_000


def is_dynamic(expression, auxes=None) -> bool:
    """Whether a compiled rate can change between two events (D91): it reads
    a level, a blend or the clock -- itself or through an AUX() it reads
    (SD-2) -- or a STEP/PULSE whose time is computed."""
    return any(_DYNAMIC_CALLS & e.calls or _CLOCK_NAMES & e.names or e.instants is None
               for e in sd.reach(expression, auxes or {}))


def visible(records) -> list:
    """Every record but the hidden ones `sd.expand` generates (SD-2): the one
    filter the summaries, the trace and the sampler use, so a model's outputs
    show its own levels and flows only."""
    return [record for record in records if not record.hidden]


def _reads_of(flow, auxes=None):
    """The bus keys *flow*'s rate reads (through its AUX() reads too), or None
    for "re-evaluate always"."""
    reached = sd.reach(flow.rate_expr, auxes or {})
    calls = frozenset().union(*(e.calls for e in reached))
    if _SAMPLER_CALLS & calls:
        return None
    if not (_BUS_CALLS & calls):
        return frozenset()
    reads = [e.bus_reads for e in reached]
    return None if None in reads else frozenset().union(*reads)


class LevelError(ValueError):
    """A malformed level or flow. Raised at build time; `validate_levels_structured`
    reports the same faults as Problems long before a run reaches here."""


class _Threshold:
    """A value that fires discrete logic when crossed -- on a level, or (Bio-1
    G3) on a flow's realised rate. `cost` accrues one lump into the named cost
    item per crossing, which is what a generator START cost is."""

    __slots__ = ("value", "direction", "signal", "variable", "cost", "item", "category")

    def __init__(self, value, direction, signal, variable, cost=0.0, item="", category=""):
        self.value = float(value)
        self.direction = direction
        self.signal = signal or ""
        self.variable = variable or ""
        self.cost = float(cost or 0.0)
        self.item = item or ""
        self.category = category or ""

    def crossed(self, before: float, after: float) -> bool:
        """Whether this threshold fires on the move from *before* to *after*.

        The engine schedules the crossing instant exactly, so `after` lands ON
        the value and the test is half-open at that end -- which is also what
        stops a threshold sitting exactly at the current level from firing
        again on the next step."""
        if self.direction in ("falling", "both") and before > self.value >= after:
            return True
        return self.direction in ("rising", "both") and before < self.value <= after


class Level:
    """One continuous quantity, with its bounds, power limits and statistics."""

    __slots__ = ("name", "capacity", "minimum", "initial", "unit", "thresholds", "level",
                 "max_inflow", "max_outflow", "inflow_total", "outflow_total", "spilled",
                 "unmet", "in_rate", "out_rate", "spill_rate", "unmet_rate",
                 "_start", "_last_t", "_area", "_min", "_max", "_empty_time", "_full_time",
                 "pending", "components", "tolerance", "content", "hold", "_content_level",
                 "comp_in_rate", "comp_out_rate", "_comp_area", "comp_in", "comp_out",
                 "_frac_min", "_frac_max", "hidden", "conveyor")

    def __init__(self, name, capacity=None, minimum=0.0, initial=0.0, unit="",
                 max_inflow=None, max_outflow=None, thresholds=(), components=(),
                 tolerance=DEFAULT_BLEND_TOLERANCE, hidden=False):
        self.name = name
        self.hidden = bool(hidden)      # a delay stage `sd.expand` made (SD-2)
        self.conveyor = None            # a `_Conveyor` when this stock is one (C1)
        self.capacity = None if capacity is None else float(capacity)
        # `min: null` is a stock unbounded below (SD-1): -inf makes every
        # "empty" test false and every falling crossing infinitely far away.
        self.minimum = -math.inf if minimum is None else float(minimum)
        self.initial = float(initial)
        self.unit = unit
        self.max_inflow = None if max_inflow is None else float(max_inflow)
        self.max_outflow = None if max_outflow is None else float(max_outflow)
        self.thresholds = tuple(thresholds)
        self.level = self.initial
        self.in_rate = self.out_rate = self.spill_rate = self.unmet_rate = 0.0
        # Pending TRANSFER demands, FIFO: [amount, "remove"|"add", simpy.Event].
        self.pending: list = []
        # Blended quality (Bio-1 G2): named components tracked as ABSOLUTE
        # content, so the fraction is always `content / level` and conservation
        # never depends on a ratio. `hold` is the zero-order-hold fraction the
        # outflows are currently drawing at.
        self.components = tuple(str(name) for name, _frac in components)
        self.tolerance = float(tolerance)
        self.content = {str(name): float(frac) * self.level for name, frac in components}
        self.hold = {str(name): float(frac) for name, frac in components}
        self.comp_in_rate = {name: 0.0 for name in self.components}
        self.comp_out_rate = dict(self.comp_in_rate)
        self._content_level = self.level
        self.reset_stats(0.0)

    def restart(self, value: float) -> None:
        """Start at *value* instead: an `initial` expression, evaluated once
        the system exists (SD-2). Component shares keep their fractions."""
        self.initial = self.level = self._content_level = float(value)
        self.content = {name: self.hold[name] * self.level for name in self.components}
        self.reset_stats(0.0)

    # -- statistics ---------------------------------------------------------
    def reset_stats(self, t: float) -> None:
        """Warm-up is a statistics reset, not a state reset: the level keeps
        its value and its component content, every accumulator restarts at *t*."""
        self._start = self._last_t = t
        self._area = self._empty_time = self._full_time = 0.0
        self._min = self._max = self.level
        self.inflow_total = self.outflow_total = self.spilled = self.unmet = 0.0
        self._comp_area = {name: 0.0 for name in self.components}
        self.comp_in = dict(self._comp_area)
        self.comp_out = dict(self._comp_area)
        self._frac_min = {name: self.fraction(name) for name in self.components}
        self._frac_max = dict(self._frac_min)

    def _accrue(self, new_level: float, dt: float, t: float) -> None:
        """Fold the interval [last_t, t], over which the level moved LINEARLY
        from `self.level` to *new_level*, into the accumulators. The area is
        the trapezoid -- a piecewise-constant accumulator would be wrong here,
        which is why levels do not reuse `stats.TimeWeighted`."""
        self._area += 0.5 * (self.level + new_level) * dt
        # Both ends (#454): the start is the value a TRANSFER's lump left, which no interval
        # ends at; the interval is linear, so its ends are its extremes.
        self._min = min(self._min, self.level, new_level)
        self._max = max(self._max, self.level, new_level)
        at_min = self.level <= self.minimum + EPS and new_level <= self.minimum + EPS
        if at_min:
            self._empty_time += dt
        elif (self.capacity is not None and self.level >= self.capacity - EPS
              and new_level >= self.capacity - EPS):
            self._full_time += dt
        self.inflow_total += self.in_rate * dt
        self.outflow_total += self.out_rate * dt
        self.spilled += self.spill_rate * dt
        self.unmet += self.unmet_rate * dt
        if self.components:
            self._accrue_components(new_level, dt)
        self._last_t = t

    def _accrue_components(self, new_level: float, dt: float) -> None:
        """Component content over the same interval (Bio-1 G2).

        The content is piecewise LINEAR too (both its rates are constant), so
        its area is the trapezoid and the mass-weighted mean fraction
        `content area / level area` is exact. The clamp is a float guard: the
        drift re-evaluation already keeps `content / level` inside
        [hold - tolerance, hold + tolerance].
        """
        for name in self.components:
            before = self.content[name]
            after = before + (self.comp_in_rate[name] - self.comp_out_rate[name]) * dt
            after = min(max(after, 0.0), max(new_level, 0.0))
            self._comp_area[name] += 0.5 * (before + after) * dt
            self.comp_in[name] += self.comp_in_rate[name] * dt
            self.comp_out[name] += self.comp_out_rate[name] * dt
            self.content[name] = after
            fraction = after / new_level if new_level > EPS else self.hold[name]
            self._frac_min[name] = min(self._frac_min[name], fraction)
            self._frac_max[name] = max(self._frac_max[name], fraction)
        self._content_level = new_level

    # -- components (Bio-1 G2) ----------------------------------------------
    def fraction(self, name: str) -> float:
        """The component's share of the level RIGHT NOW (exact, not the held
        value): `FRACTION("level", "component")`."""
        if self.level > EPS:
            return self.content.get(name, 0.0) / self.level
        return self.hold.get(name, 0.0)

    def refresh_hold(self) -> None:
        """Re-arm the zero-order hold: from here the outflows draw at the
        level's true fraction until it has drifted by `tolerance`."""
        for name in self.components:
            self.hold[name] = self.fraction(name)

    def carry_components(self) -> None:
        """A discrete lump (a TRANSFER, a granted backorder) carries the level's
        OWN blend, so the fraction is unchanged and nothing is created or lost.
        Idempotent: it reconciles the content with whatever the level is now."""
        if not self.components or abs(self.level - self._content_level) <= EPS:
            self._content_level = self.level
            return
        delta = self.level - self._content_level
        for name in self.components:
            share = (self.content[name] / self._content_level
                     if self._content_level > EPS else self.hold[name])
            moved = share * delta
            self.content[name] = min(max(self.content[name] + moved, 0.0), max(self.level, 0.0))
            if moved >= 0:
                self.comp_in[name] += moved
            else:
                self.comp_out[name] -= moved
        self._content_level = self.level

    def avg_fraction(self, name: str) -> float:
        """Mass-weighted mean share over the window -- exact, because both
        areas are trapezoids of piecewise-linear curves."""
        return self._comp_area[name] / self._area if self._area > 0 else self.fraction(name)

    def frac_min(self, name: str) -> float:
        return self._frac_min[name]

    def frac_max(self, name: str) -> float:
        return self._frac_max[name]

    def time_avg(self, t_end: float) -> float:
        span = t_end - self._start
        return self._area / span if span > 0 else self.level

    @property
    def min(self) -> float:
        return self._min

    @property
    def max(self) -> float:
        return self._max

    def empty_fraction(self, t_end: float) -> float:
        span = t_end - self._start
        return self._empty_time / span if span > 0 else 0.0

    def full_fraction(self, t_end: float) -> float:
        span = t_end - self._start
        return self._full_time / span if span > 0 else 0.0

    def __repr__(self):
        return f"<Level {self.name!r} level={self.level:g}>"


class Flow:
    """One rate-based transfer. `src`/`dst` are `Level`s or None (a source or
    sink outside the model: the sun, the grid, demand).

    `tag` and `priority` are the site BUS (PHASE4 D3, ARCHITECTURE D62): flows
    carrying the same tag are summed by `FLOWSUM("tag")`, and `priority` is the
    merit order among them -- `FLOWSUM("tag", p)` sums only the flows dispatched
    BEFORE priority *p*. `scope` is the flow's own expression scope, carrying a
    submodel instance's parameters for a `$`-cloned flow.
    """

    __slots__ = ("name", "index", "src", "dst", "rate_expr", "efficiency", "price_expr",
                 "impact_expr", "biflow", "hidden", "leak",
                 "tag", "priority", "scope", "peak_price", "min_rate", "comp_exprs",
                 "comp_frac", "thresholds",
                 "desired", "actual", "price", "impact_factor", "total", "cost", "impact",
                 "peak", "limited_time", "starts", "_rate_before", "_start")

    def __init__(self, name, src, dst, rate_expr, efficiency=1.0,
                 price_expr=None, impact_expr=None, tag="", priority=0.0, scope=None,
                 peak_price=0.0, min_rate=None, comp_exprs=None, thresholds=(),
                 biflow=False, hidden=False, leak=None):
        self.name = name
        self.hidden = bool(hidden)      # a delay stage's flow (SD-2)
        # A conveyor's leakage outflow (C1): (fraction expression, zone start,
        # zone end); None for every other flow, the primary outflow included.
        self.leak = leak
        # A bi-directional flow keeps a NEGATIVE rate (it runs backwards)
        # instead of clamping it to 0. Validation allows one only between
        # unbounded ends, so saturation never has to split a signed rate.
        self.biflow = bool(biflow)
        # Position in `LevelSystem.flows`, set when the system is built: the
        # saturation scales are lists, not dicts keyed by name.
        self.index = 0
        self.src = src
        self.dst = dst
        self.rate_expr = rate_expr
        self.efficiency = float(efficiency)
        self.price_expr = price_expr
        self.impact_expr = impact_expr
        self.tag = str(tag or "")
        self.priority = float(priority or 0.0)
        self.scope = scope
        # A demand charge (PHASE4 D5): money per unit of `peak`, billed ONCE at
        # the end of the run rather than integrated, which is exactly how a
        # network operator bills peak power. A plain number, not an expression:
        # a tariff that varies over time is a `price` on a flow, not a peak.
        self.peak_price = float(peak_price or 0.0)
        # Semi-continuous equipment (Bio-1 G3): a CHP is off or at least at its
        # minimum load. The gate is applied to the DESIRED rate only, before
        # saturation, so it introduces no event of its own and cannot chatter.
        self.min_rate = None if min_rate is None else float(min_rate)
        # What share of this flow's quantity is each component (Bio-1 G2). A
        # missing entry defaults to the SOURCE level's own blend.
        self.comp_exprs = dict(comp_exprs or {})
        self.comp_frac: dict[str, float] = {}
        # Rate thresholds (Bio-1 G3): a generator start cost, a "running"
        # signal. Mirrors a level's thresholds, on the realised rate.
        self.thresholds = tuple(thresholds)
        self.desired = self.actual = self.price = self.impact_factor = 0.0
        self._rate_before = 0.0
        self.reset_stats(0.0)

    def reset_stats(self, t: float) -> None:
        self._start = t
        self.total = self.cost = self.impact = self.limited_time = self.peak = 0.0
        self.starts = 0

    def avg_rate(self, t_end: float) -> float:
        span = t_end - self._start
        return self.total / span if span > 0 else 0.0

    def limited_fraction(self, t_end: float) -> float:
        span = t_end - self._start
        return self.limited_time / span if span > 0 else 0.0

    @property
    def peak_cost(self) -> float:
        """The demand charge: peak rate held x the tariff (PHASE4 D5)."""
        return self.peak * self.peak_price

    def __repr__(self):
        return f"<Flow {self.name!r} rate={self.actual:g}>"


class _Pipeline:
    """A pipeline delay (DELAYP, Vensim DELAY FIXED; PHASE5 SD-2, D92):
    `out(t) = in(t - tau)`, EXACT, with no grid.

    The input is recorded as a `(t, value)` history at every re-evaluation of
    the level engine -- between two of them it is piecewise constant, exactly
    as a rate is -- and read back at `t - tau`. The instant a recorded change
    comes out is one more change instant of the engine (`next_change`), so a
    reader sees it on time. `tau` is fixed when the run starts (Vensim's rule
    for a fixed delay); the output is `initial` until the first input is due.
    """

    __slots__ = ("name", "input", "tau", "initial", "scope", "times", "values")

    def __init__(self, name, input_expr, tau, initial_expr, scope):
        if not tau > 0:
            raise LevelError(f"delay '{name}': delay_time must be > 0, got {tau}")
        self.name, self.input, self.tau = name, input_expr, float(tau)
        self.initial, self.scope = initial_expr, scope
        self.times: list = []
        self.values: list = []

    def record(self, t: float) -> None:
        try:
            value = float(self.input.evaluate(self.scope))
        except ExpressionError as exc:
            raise ExpressionError(f"delay '{self.name}' input: {exc.message}") from None
        times, values = self.times, self.values
        if times and times[-1] == t:
            values[-1] = value          # the last word at one instant wins
        elif not values or values[-1] != value:
            times.append(t)
            values.append(value)
        # Forget what can no longer come out; keep the value in force.
        gone = bisect_right(times, t - self.tau + EPS) - 1
        if gone > 64:
            del times[:gone], values[:gone]

    def evaluate(self, _scope=None) -> float:
        i = bisect_right(self.times, self.scope.ctx.now - self.tau + EPS) - 1
        if i >= 0:
            return self.values[i]
        return 0.0 if self.initial is None else float(self.initial.evaluate(self.scope))

    def next_change(self, now: float):
        i = bisect_right(self.times, now - self.tau + EPS)
        return self.times[i] + self.tau if i < len(self.times) else None


class _Sampler:
    """SAMPLE_IF_TRUE(cond, x, init) (Vensim; PHASE6 S2, `core/sd._value_macro`):
    holds the last x read while cond was true, `init` before the first true
    reading. Updated from `LevelSystem._recompute`, at every re-evaluation --
    an event, or, with `simulation.sd`, a grid point -- exactly like a
    pipeline's `record()`. EXACT AT those instants: cond's truth is a
    zero-order hold in between, the same granularity a self-discharge
    LEVEL()-driven rate already lives with."""

    __slots__ = ("name", "cond", "input", "scope", "value")

    def __init__(self, name, cond_expr, input_expr, initial_expr, scope):
        self.name, self.cond, self.input, self.scope = name, cond_expr, input_expr, scope
        self.value = 0.0 if initial_expr is None else float(initial_expr.evaluate(scope))

    def update(self, _t: float) -> None:
        try:
            if self.cond.evaluate(self.scope):
                self.value = float(self.input.evaluate(self.scope))
        except ExpressionError as exc:
            raise ExpressionError(f"delay '{self.name}': {exc.message}") from None

    def evaluate(self, _scope=None) -> float:
        return self.value


class _Noise:
    """NOISE_NORMAL(mean, sd, lo, hi) (PHASE6 S2, `core/sd._value_macro`): a
    normal draw clipped to [lo, hi], redrawn from `LevelSystem._recompute` at
    every re-evaluation (an event, or a grid point with `simulation.sd`).

    The stream is seeded once from `[model seed, hash(this delay's name)]` --
    the same recipe `BaseProcess` uses for a block's own rng -- so the draws
    are FIXED PER CALL SITE (CRN): the same model and seed reproduce the same
    noise, and adding an unrelated flow elsewhere never shifts it. RK4 refuses
    a rate that reads one (E_SD_RK4_RANDOM precedent, `levels._check_sd_settings`):
    four stage re-evaluations would draw four times per step. Deferred:
    coloured/pink noise -- this is white noise only."""

    __slots__ = ("name", "mean", "sd", "lo", "hi", "scope", "rng", "value")

    def __init__(self, name, mean_expr, sd_expr, lo_expr, hi_expr, scope, seed):
        self.name = name
        self.mean, self.sd, self.lo, self.hi = mean_expr, sd_expr, lo_expr, hi_expr
        self.scope = scope
        self.rng = np.random.default_rng(np.random.SeedSequence([seed, deterministic_hash(name)]))
        self.value = 0.0
        self.update(None)

    def update(self, _t) -> None:
        try:
            mean = float(self.mean.evaluate(self.scope))
            sd = float(self.sd.evaluate(self.scope))
            lo = float(self.lo.evaluate(self.scope))
            hi = float(self.hi.evaluate(self.scope))
        except ExpressionError as exc:
            raise ExpressionError(f"delay '{self.name}': {exc.message}") from None
        self.value = min(max(float(self.rng.normal(mean, sd)), lo), hi)

    def evaluate(self, _scope=None) -> float:
        return self.value


def _half_up(x: float) -> int:
    """Round half away from zero (x >= 0): Stella's slat count, so 16.5 -> 17."""
    return int(math.floor(x + 0.5))


class _Cohort:
    """What entered a conveyor in one DT (C1): its content, its transit in
    slats `d` (latched at entry), the DTs it has ridden (`age`), each linear
    leak's base (the content at the start of that leak's zone) and the leak
    planned for the current step. Per-cohort rather than per-slat because a
    linear leak's schedule and base belong to the batch, and a shorter
    transit can merge two batches into one slat."""

    __slots__ = ("age", "d", "content", "bases", "leak")

    def __init__(self, d: int, content: float, leaks: int):
        self.age, self.d, self.content, self.leak = 0, d, content, 0.0
        self.bases = [None] * leaks


class _Conveyor:
    """A Stella conveyor stock (C1, docs/history/STELLA-CONVEYOR-SEMANTICS-
    2026-09-27.md): material rides a belt of slats for a transit time fixed
    when it entered, may leak on the way and leaves as the primary outflow.

    Runs on the SD grid only, Euler (one slat per DT). Each grid step k:
    `commit` closes step k-1 (planned leaks and exit leave, the belt shifts,
    what actually arrived over the step is inserted at the latched depth),
    then `plan` computes this step's leaks and exit from the start-of-step
    cohorts and `admit` clamps the equation-driven inflows. The flows then run
    at volume / DT over [k DT, (k+1) DT) like any other rate, while the stock
    itself is PIECEWISE CONSTANT: LEVEL() between two grid points is the
    value of the last grid step."""

    __slots__ = ("level", "transit", "capacity", "in_limit", "arrest", "exponential", "scope",
                 "out", "leaks", "ins", "fed", "feeds", "cohorts", "arriving", "arrested",
                 "d", "removed")

    def __init__(self, level, transit, capacity, in_limit, arrest, exponential, scope):
        self.level = level
        self.transit, self.capacity, self.in_limit, self.arrest = (
            transit, capacity, in_limit, arrest)
        self.exponential = exponential
        self.scope = scope
        self.out = None         # the primary outflow
        self.leaks: list = []   # (flow, fraction expression, zone start, zone end)
        self.ins: tuple = ()    # equation-driven inflows: `LevelSystem._rates` entries
        self.fed: tuple = ()    # inflows that are another conveyor's outflow or leak
        self.feeds: tuple = ()  # every inflow, for the arrival volume
        self.cohorts: list = []
        self.arriving = 0.0     # volume arrived since the last grid step
        self.arrested = False
        self.d = 1              # insertion depth latched this step
        self.removed = 0.0      # leak + exit volume planned this step

    def _value(self, expression, field: str, default=None, scope=None) -> float:
        if expression is None:
            return default
        try:
            return float(expression.evaluate(scope or self.scope))
        except ExpressionError as exc:
            raise ExpressionError(f"conveyor '{self.level.name}' {field}: {exc.message}") from None

    def _slats(self, dt: float) -> int:
        return max(1, _half_up(self._value(self.transit, "transit_time") / dt))

    def _fractions(self) -> list:
        return [self._value(expr, "leak fraction", scope=flow.scope)
                for flow, expr, _lo, _hi in self.leaks]

    def _leak(self, cohort: _Cohort, fractions: list, dt: float) -> list:
        """Each leak's volume from *cohort* this DT, from its start-of-step
        content. Linear: f * base / (DTs in the zone), base = the content at
        the zone's start (so staggered zones act on what is left). Exponential:
        content * f * DT, overlapping leaks each on the same content. Leaks
        take in listed order and never more than is left."""
        a = cohort.age + 1
        start = left = cohort.content
        volumes = []
        for j, ((_flow, _expr, lo, hi), f) in enumerate(zip(self.leaks, fractions)):
            first, last = _half_up(lo * cohort.d), _half_up(hi * cohort.d)
            if not first < a <= last:
                volumes.append(0.0)
                continue
            if self.exponential:
                volume = start * f * dt
            else:
                if a == first + 1:
                    cohort.bases[j] = start
                volume = f * cohort.bases[j] / (last - first)
            volume = min(max(volume, 0.0), left)
            left -= volume
            volumes.append(volume)
        return volumes

    def fill(self, dt: float) -> None:
        """Spread the initial value as the steady state of a constant entry
        under this conveyor's own leak rules, scaled to sum to it: a unit
        cohort per slat, aged by the leak routine itself."""
        total = self.level.level
        if total <= 0:
            return
        n, fractions = self._slats(dt), self._fractions()
        for age in range(n):
            cohort = _Cohort(n, 1.0, len(self.leaks))
            for _ in range(age):
                cohort.content -= math.fsum(self._leak(cohort, fractions, dt))
                cohort.age += 1
            self.cohorts.append(cohort)
        scale = total / math.fsum(c.content for c in self.cohorts)
        for cohort in self.cohorts:
            cohort.content *= scale
            cohort.bases = [None if b is None else b * scale for b in cohort.bases]

    def commit(self) -> None:
        """Close the step. Reads no expression, so `sync()` can run it the
        moment it reaches a grid instant (DEVLOG #239); the level's totals
        are accrued by `sync()` as the flows run, not here (DEVLOG #241)."""
        if not self.arrested:
            for cohort in self.cohorts:
                cohort.content -= cohort.leak
                cohort.leak = 0.0
                cohort.age += 1
            self.cohorts = [c for c in self.cohorts if c.age < c.d]
        if self.arriving > 0.0:
            self.cohorts.append(_Cohort(self.d, self.arriving, len(self.leaks)))
        self.level.level = math.fsum(c.content for c in self.cohorts)
        self.arriving = self.removed = 0.0

    def plan(self, dt: float) -> None:
        self.arrested = bool(self._value(self.arrest, "arrest", 0.0))
        self.d = self._slats(dt)        # the transit latched for what enters now
        volumes = [0.0] * len(self.leaks)
        exit_volume = 0.0
        if not self.arrested:
            fractions = self._fractions()
            for cohort in self.cohorts:
                taken = self._leak(cohort, fractions, dt)
                cohort.leak = math.fsum(taken)
                for j, volume in enumerate(taken):
                    volumes[j] += volume
                if cohort.age + 1 >= cohort.d:
                    exit_volume += cohort.content - cohort.leak
        self.out.desired = exit_volume / dt
        for (flow, *_rest), volume in zip(self.leaks, volumes):
            flow.desired = volume / dt
        self.removed = exit_volume + math.fsum(volumes)

    def admit(self, dt: float, evaluate) -> None:
        """Clamp the equation-driven inflows, in listed order, to
        min(requested, in_limit * DT, room), room = capacity - contents + this
        step's leak and exit volume - what other conveyors pour in (those are
        never clamped). The flow reports the admitted rate, so refused
        material stays upstream."""
        if not self.ins:
            return
        evaluate(self.ins)
        room = math.inf
        if self.capacity is not None:
            room = self._value(self.capacity, "capacity") - self.level.level + self.removed
            room -= math.fsum(f.desired * f.efficiency for f in self.fed) * dt
        if self.in_limit is not None:
            room = min(room, self._value(self.in_limit, "in_limit") * dt)
        for flow, *_rest in self.ins:
            if self.arrested:
                flow.desired = 0.0
                continue
            take = max(min(flow.desired * flow.efficiency * dt, room), 0.0)
            room -= take
            flow.desired = take / (flow.efficiency * dt)


class LevelSystem:
    """The one process that integrates every level and every flow.

    `sync(t)` brings the integration up to *t* without re-evaluating anything
    (idempotent, so `LEVEL()` may call it from any expression at any time);
    `trigger()` wakes the process so a discrete event's effect on the rates is
    picked up immediately.
    """

    def __init__(self, env, levels: dict, flows: list, conversions=(), auxes=None):
        self.env = env
        # Auxiliaries (SD-2), name -> compiled expression: a rate that reads
        # one reads what it reads, for the dynamic and the bus tests below.
        auxes = auxes or {}
        self._auxed = bool(auxes)
        # Delays (SD-2): name -> (output with `.evaluate`, its scope), read by
        # DELAY(); the pipelines among them are driven by this process.
        self.delays: dict = {}
        self.pipelines: list = []
        # "sample"/"noise" delays (PHASE6 S2): no `next_change` of their own --
        # they only react to a re-evaluation the engine already scheduled --
        # so they update alongside the pipelines instead of scheduling one.
        self.stateful: list = []
        self._instants: tuple = ()      # literal STEP/PULSE/RAMP times
        self.levels = levels
        self.flows = flows
        # Delayed converters (Bio-1 G1): read by CONV()/FRACTION(), driven by
        # this same process so their cohort grid is one more crossing time.
        self.conversions = list(conversions)
        self.conversion_index = {c.name: c for c in self.conversions}
        self.flow_index = {flow.name: flow for flow in flows}
        for position, flow in enumerate(flows):
            flow.index = position
        # Precomputed so a model without the feature pays one truth test per
        # event rather than a scan (the D60 "pays nothing" rule).
        self._blended = tuple(l for l in levels.values() if l.components)
        self._thresholded = tuple(f for f in flows if f.thresholds)
        # Flows with a price or an impact expression: the rest keep 0.0 (SD-3).
        self._priced = tuple(f for f in flows
                             if f.price_expr is not None or f.impact_expr is not None)
        # Saturation wiring, which never changes during a run: rebuilding these
        # two lists per level per pass was O(levels x flows) on every pass.
        self._ins = {level: [f for f in flows if f.dst is level] for level in levels.values()}
        self._outs = {level: [f for f in flows if f.src is level] for level in levels.values()}
        # FLOWSUM memo for one pass, keyed (tag, below). Only `_apply` moves a
        # realised rate, so that is the one place it has to be dropped -- which
        # is also where `_moved`, the bus keys that just changed, is filled.
        self._sums: dict = {}
        self._moved: set = set()
        # The site bus (PHASE4 D3): tag -> the flows carrying it, so FLOWSUM()
        # aggregates an unknown number of instances without naming any of them.
        self.tagged: dict[str, list] = {}
        for flow in flows:
            if flow.tag:
                self.tagged.setdefault(flow.tag, []).append(flow)
        self._last_t = float(env.now)
        self._wake = env.event()
        self._series = ()           # filled by build_level_system, read for next_change
        self._scope = ExpressionScope(ctx=env.ctx)
        # (flow, compiled closure, its scope, what it reads off the bus) for the
        # rate loop, bound once. `reads` is None for a rate that must be
        # re-evaluated on every pass whatever moved (it samples, or names its
        # bus target with an expression), and an empty set for one that cannot
        # change between passes at all.
        self._rates = tuple((f, f.rate_expr.closure(), f.scope or self._scope,
                             _reads_of(f, auxes)) for f in flows)
        # Conveyors (C1) own every flow they touch: those rates are set per
        # grid step by `_step_conveyors`, never by the rate loop.
        self._conveyors = tuple(l.conveyor for l in levels.values() if l.conveyor is not None)
        self._belt_step = None      # the grid index the conveyors last planned at
        self._belt_open = False     # a planned step awaits its commit
        self._belt_corners = ()     # (name, value) just before the last commit, for the trace
        if self._conveyors:
            self._rates = self._wire_conveyors()
        self._varying_rates = tuple(r for r in self._rates if r[3] is None or r[3])
        self._before = {level: level.level for level in levels.values()}
        self._thresholded_levels = tuple(l for l in levels.values() if l.thresholds)
        # System dynamics grid (SD-1, D89-D91): off until `set_sd` turns it on.
        self._dynamic = tuple(r for r in self._rates if is_dynamic(r[0].rate_expr, auxes))
        self.dt = None
        self.rk4 = False
        self._later = self._later_grid = self._varying_rates
        self._grid_only = False     # what `_next_step` last scheduled
        self._grid_wake = False     # the current wake-up is a pure grid point
        self._stride = 1
        self._grid_count = 0
        self._slopes: dict = {}
        # Last (t, value) put on the bus per level (PHASE4 T2, D75), so the two
        # emit sites never record the same corner twice.
        self._emitted: dict[str, tuple[float, float]] = {}
        # The run's time limit, set by `start`. A model with flows is still
        # ACTIVE after its last entity has gone -- the battery is still
        # charging -- so the process keeps a wake-up at the horizon rather than
        # letting the event queue drain early and cut every total short.
        # `schema.requires_run_length` is what guarantees there is a horizon.
        self.horizon = None
        # True only while `_start_initials` runs: FLOW() then evaluates the
        # flow's rate expression, since no rate exists yet (DEVLOG #161).
        self._initialising = False
        # True only while a game-mode snapshot reads the dashboard (DEVLOG
        # #229): LEVEL()/FRACTION() then `peek` instead of `sync`, so reading
        # never splits the integration and the run stays bit-identical.
        self.peeking = False

    # -- public surface -----------------------------------------------------
    def set_sd(self, dt: float, method: str = "euler") -> None:
        """Turn on the fixed step grid (D89). A system with no DYNAMIC rate has
        nothing to integrate between events, so it keeps no grid at all --
        unless a sample/noise/pipeline delay needs a reading per grid point
        (DEVLOG #158: SAMPLE_IF_TRUE held its init, NOISE_NORMAL drew once)."""
        if not (self._dynamic or self.stateful or self.pipelines or self._conveyors):
            return
        self.dt = float(dt)
        self.rk4 = method == "rk4"
        dynamic = {entry[0] for entry in self._dynamic}     # flows: a scope is unhashable
        # Later passes re-read the bus. Under RK4 a dynamic rate keeps the
        # effective rate its stages produced (W_SD_RK4_BUS says so); on a grid
        # wake-up a static rate that samples must not draw again, since nothing
        # it depends on has moved.
        self._later = tuple(r for r in self._varying_rates
                            if not (self.rk4 and r[0] in dynamic))
        self._later_grid = tuple(r for r in self._later
                                 if r[0] in dynamic or not _SAMPLER_CALLS & r[0].rate_expr.calls)

    def _wire_conveyors(self) -> tuple:
        """Hand each conveyor its flows (C1) and return the rate-loop entries
        left for the ordinary loop: none that touch a conveyor."""
        belts = {belt.level: belt for belt in self._conveyors}
        for belt in self._conveyors:
            belt.scope = belt.scope or self._scope
            outs = [f for f in self.flows if f.src is belt.level]
            primary = [f for f in outs if f.leak is None]
            if len(primary) != 1:
                raise LevelError(f"conveyor '{belt.level.name}' needs exactly one primary "
                                 f"outflow, has {len(primary)}")
            belt.out = primary[0]
            belt.leaks = [(f, *f.leak) for f in outs if f.leak is not None]
            belt.ins = tuple(r for r in self._rates
                             if r[0].dst is belt.level and r[0].src not in belts)
            belt.fed = tuple(f for f in self.flows if f.dst is belt.level and f.src in belts)
            belt.feeds = tuple(f for f in self.flows if f.dst is belt.level)
        return tuple(r for r in self._rates if r[0].src not in belts and r[0].dst not in belts)

    def _step_conveyors(self) -> None:
        """Plan one conveyor step per grid point (C1), at most once per index:
        every conveyor's leak and exit before any admits, so a downstream
        conveyor knows what an upstream one pours in. The last step was
        already closed by `sync()` reaching this instant (`_commit_conveyors`).
        Off-grid wake-ups change nothing on a belt."""
        if not self._conveyors:
            return
        now, dt = self.env.now, self.dt
        k = round(now / dt)
        if abs(now - k * dt) > EPS or k == self._belt_step:
            return
        if self._belt_step is None:
            for belt in self._conveyors:
                belt.fill(dt)
        self._belt_step, self._belt_open = k, True
        for belt in self._conveyors:
            belt.plan(dt)
        for belt in self._conveyors:
            belt.admit(dt, self._evaluate)

    def _commit_conveyors(self, t: float) -> None:
        """Close the open step once `sync()` reaches the next grid instant
        (DEVLOG #239): a LEVEL() read there, by a DES process that runs
        before this process wakes, sees the step, as it would a plain level.
        No expression is read, so `sync()` stays side-effect free for them."""
        k = round(t / self.dt)
        if abs(t - k * self.dt) > EPS or k <= self._belt_step:
            return
        if self.env.ctx.bus.has_subscribers:     # the corner before the jump, for playback
            self._belt_corners = tuple((b.level.name, b.level.level) for b in self._conveyors
                                       if not b.level.hidden)
        for belt in self._conveyors:
            belt.commit()
        self._belt_open = False

    def start(self, horizon: float | None = None) -> None:
        self.horizon = float(horizon) if horizon and horizon > 0 else None
        if self.dt is not None and self.horizon is not None:
            steps = math.ceil(self.horizon / self.dt)
            self._stride = max(1, math.ceil(steps * len(self.levels) / TRACE_BUDGET))
        self.env.process(self._run())

    def trigger(self) -> None:
        """A discrete event changed something the rates read: re-evaluate now."""
        if not self._wake.triggered:
            self._wake.succeed()

    def flow_sum(self, tag, below=None) -> float:
        """The realised rates of every flow tagged *tag* (PHASE4 D3).

        With *below* given, only the flows whose own `priority` is strictly
        lower are summed -- "what the devices ahead of me in the merit order
        actually took", which is how a second battery sees the surplus the
        first one left. An unknown tag sums to 0: a site with no generator has
        no generation, which is an answer, not an error.
        """
        key = (tag, below)
        cached = self._sums.get(key)
        if cached is not None:
            return cached
        flows = self.tagged.get(str(tag))
        if not flows:
            total = 0.0
        elif below is None:
            total = math.fsum(flow.actual for flow in flows)
        else:
            limit = float(below)
            total = math.fsum(flow.actual for flow in flows if flow.priority < limit)
        self._sums[key] = total
        return total

    def sync(self, t: float | None = None) -> None:
        """Integrate up to *t* (default: now). Idempotent -- a second call at
        the same instant does nothing, which is what lets `LEVEL()` call it
        from inside any expression."""
        t = float(self.env.now if t is None else t)
        dt = t - self._last_t
        if dt <= 0:
            return
        for flow in self.flows:
            quantity = flow.actual * dt
            flow.total += quantity
            flow.cost += flow.price * quantity
            flow.impact += flow.impact_factor * quantity
            # The peak counts a rate that was actually HELD for some time, so a
            # rate written and superseded at the same instant never sets it --
            # which is what makes it a demand charge's peak power.
            flow.peak = max(flow.peak, flow.actual)
            if flow.actual < flow.desired - EPS:
                flow.limited_time += dt
        for level in self.levels.values():
            new = self._snap(level, level.level + (level.in_rate - level.out_rate) * dt)
            level._accrue(new, dt, t)
            level.level = new
        for conversion in self.conversions:
            conversion.accumulate(dt)
        for belt in self._conveyors:
            arrived = math.fsum(f.actual * f.efficiency for f in belt.feeds) * dt
            belt.arriving += arrived
            # Accrued as the flows run, so a warm-up reset mid-step keeps only
            # the share after it, like a flow's own total (DEVLOG #241).
            belt.level.inflow_total += arrived
            belt.level.outflow_total += belt.removed * dt / self.dt
        self._last_t = t
        if self._belt_open:
            self._commit_conveyors(t)

    def peek(self, level: Level) -> float:
        """The value `sync()` would give *level* now, without integrating
        (DEVLOG #229). Exact: the rates are constant up to the next wake-up,
        and every crossing, grid point and pipeline change is one."""
        dt = self.env.now - self._last_t
        if dt <= 0:
            return level.level
        return self._snap(level, level.level + (level.in_rate - level.out_rate) * dt)

    def peek_fraction(self, level: Level, name: str) -> float:
        """`level.fraction(name)` as `sync()` would leave it now, likewise
        read-only: `_accrue_components`' own arithmetic on copies."""
        new_level = self.peek(level)
        dt = self.env.now - self._last_t
        content = level.content.get(name, 0.0)
        if dt > 0 and name in level.content:
            content += (level.comp_in_rate[name] - level.comp_out_rate[name]) * dt
            content = min(max(content, 0.0), max(new_level, 0.0))
        return content / new_level if new_level > EPS else level.hold.get(name, 0.0)

    def reset_stats(self, t: float) -> None:
        for level in self.levels.values():
            level.reset_stats(t)
        for flow in self.flows:
            flow.reset_stats(t)
        for conversion in self.conversions:
            conversion.reset_stats(t)

    # -- the process --------------------------------------------------------
    def after_discrete_change(self) -> None:
        """A TRANSFER block changed a level by a lump: fire whatever thresholds
        it crossed, then wake the process so the rates are re-evaluated."""
        self._check_thresholds()
        self.trigger()

    def move(self, level_name: str, amount: float, mode: str) -> float:
        """Move *amount* of *level_name* in or out right now, non-blocking:
        the instant branch of TRANSFER's own move, factored out here so an
        agent `transfer` action (PHASE5 AB-2) can reuse it without a second
        copy of the clamp/unmet/spilled arithmetic. Returns what actually
        moved; a partial move records the rest as unmet (mode="remove") or
        spilled (mode="add"), same as TRANSFER with wait=false."""
        level = self.levels.get(level_name)
        if level is None:
            raise ExpressionError(f"no level named '{level_name}'")
        if level.conveyor is not None:
            # C2: a TRANSFER block or agent transfer action writing straight to
            # `level.level` would be silently overwritten at the next grid
            # step, since a conveyor's value is the sum of its own cohorts
            # (validation refuses this too; this is the one runtime choke
            # point both callers share).
            raise ValueError(f"'{level_name}' is a conveyor; it cannot be written by a "
                             f"TRANSFER or a transfer action")
        self.sync()
        self.mark_breakpoint()
        amount = max(float(amount), 0.0)
        available = (level.level - level.minimum if mode == "remove"
                    else (math.inf if level.capacity is None else level.capacity - level.level))
        moved = amount if amount <= available + EPS else max(available, 0.0)
        if mode == "remove":
            level.level -= moved
            level.outflow_total += moved
            if moved < amount:
                level.unmet += amount - moved
        else:
            level.level += moved
            level.inflow_total += moved
            if moved < amount:
                level.spilled += amount - moved
        self.after_discrete_change()
        return moved

    def _run(self):
        env = self.env
        self._step_conveyors()
        while True:
            self._recompute()
            self._check_flow_thresholds()
            step = self._next_step()
            if step is None:                       # nothing changes on its own
                yield self._wake
                self._wake = env.event()
                self._grid_wake = False
            else:
                yield env.timeout(step) | self._wake
                # A pure grid wake-up (D91): nothing but the clock moved, so
                # only the dynamic rates need evaluating again.
                self._grid_wake = self._grid_only and not self._wake.triggered
                if self._wake.triggered:
                    self._wake = env.event()
            self.sync()                            # closes a conveyor step on the grid
            self._step_conveyors()
            for conversion in self.conversions:    # close cohorts, advance the curves
                conversion.advance_to(env.now)
            self._check_thresholds()
            self._serve_pending()
            self._check_thresholds()    # a grant is a lump, and can cross one too

    def _next_step(self):
        """Time until the next instant anything can change, or None."""
        now = self.env.now
        best = math.inf
        for series in self._series:
            change = series.next_change(now)
            if change is not None:
                best = min(best, change - now)
        for level in self.levels.values():
            rate = level.in_rate - level.out_rate
            if abs(rate) <= EPS:
                continue
            for target in self._targets(level, rate):
                dt = (target - level.level) / rate
                if EPS < dt < best:
                    best = dt
        for conversion in self.conversions:
            best = min(best, conversion.next_time() - now)
        if self._instants:
            i = bisect_right(self._instants, now + EPS)
            if i < len(self._instants):
                best = min(best, self._instants[i] - now)
        for pipeline in self.pipelines:
            change = pipeline.next_change(now)
            if change is not None:
                best = min(best, change - now)
        for level in self._blended:
            best = min(best, self._drift_step(level))
        self._grid_only = False
        if self.dt is not None:
            # The next grid point (D89). An event within EPS of it takes its
            # place, so that wake-up is a full re-evaluation.
            grid = self._grid_after(now) - now
            if grid < best - EPS:
                self._grid_only = True
                return grid
        if not math.isfinite(best) and self.horizon is not None and self.horizon > now:
            return self.horizon - now       # stay alive to the end of the run
        return None if not math.isfinite(best) else best

    def _grid_after(self, now: float) -> float:
        """The first grid point `k * dt` strictly (by more than EPS) after
        *now*. Computed from the integer k, never accumulated, so the grid does
        not drift and is the same in every scenario (CRN)."""
        dt = self.dt
        k = math.floor(now / dt) + 1
        if k * dt - now <= EPS:
            k += 1
        return k * dt

    def _targets(self, level: Level, rate: float):
        """The values this level could reach next, in its direction of travel."""
        rising = rate > 0
        if rising and level.capacity is not None:
            yield level.capacity
        if not rising:
            yield level.minimum
        for threshold in level.thresholds:
            if threshold.direction == ("rising" if rising else "falling") or \
                    threshold.direction == "both":
                yield threshold.value
        for amount, mode, _event in level.pending:
            if mode == "remove":
                yield level.minimum + amount
            elif level.capacity is not None:
                yield level.capacity - amount

    def _drift_step(self, level: Level) -> float:
        """Time until a blended level's fraction has drifted `tolerance` away
        from the value its outflows are drawing at -- in CLOSED FORM (Bio-1 G2).

        Content and level are both linear in t, so the fraction is a ratio of
        two linear functions. With `C0 = f * L0` at the last evaluation,
        `C(t) - (f +- tol) L(t) = 0` solves to `t = -/+ tol L0 / (C' - (f +- tol) L')`.
        No inflow means `C' = f L'`, which makes both roots infinite: draining a
        blended level never drifts, and the zero-order hold is then EXACT.
        """
        total = level.level
        if total <= EPS:
            return math.inf
        level_rate = level.in_rate - level.out_rate
        best = math.inf
        tolerance = level.tolerance
        for name in level.components:
            content_rate = level.comp_in_rate[name] - level.comp_out_rate[name]
            for sign in (1.0, -1.0):
                denominator = content_rate - (level.hold[name] + sign * tolerance) * level_rate
                if abs(denominator) <= EPS:
                    continue
                dt = -sign * tolerance * total / denominator
                if EPS < dt < best:
                    best = dt
        return best

    def _check_flow_thresholds(self) -> None:
        """Signals, variables and lump costs for every flow whose REALISED rate
        crossed a threshold since the last evaluation (Bio-1 G3).

        A rising crossing is a START -- of a CHP, a pump, an upgrading unit --
        so it is counted per flow and reported as `Starts`, and its `cost`
        accrues one lump into `cost_items` exactly as a RECORD kind="cost"
        does (D63).
        """
        if not self._thresholded:
            return
        env, ctx = self.env, self.env.ctx
        for flow in self._thresholded:
            before, after = flow._rate_before, flow.actual
            flow._rate_before = after
            for threshold in flow.thresholds:
                if not threshold.crossed(before, after):
                    continue
                if after > before:
                    flow.starts += 1
                if threshold.variable:
                    ctx.variables[threshold.variable] = after
                    ctx.touch(("var", threshold.variable))      # PHASE5 O4
                if threshold.signal:
                    ctx.raise_signal(threshold.signal)
                if threshold.cost:
                    costs.accrue(ctx, threshold.item or flow.name, threshold.cost,
                                 threshold.category)
                if ctx.bus.has_subscribers:
                    ctx.bus.emit("flow_threshold", block=None, t=env.now, resource=flow.name,
                                 state=threshold.signal or threshold.variable or "threshold",
                                 value=after)

    def _component_rates(self) -> None:
        """Per-level component in/out rates (Bio-1 G2).

        An inflow carries the fraction it declares, or -- by default -- the
        blend of the level it draws FROM, which is what makes a chain of
        blended tanks work with no annotation. An outflow draws at the level's
        HELD fraction, and the level loses exactly what the outflows carry, so
        the component is conserved whatever the hold is worth.

        The one case where the hold cannot be trusted is an EMPTY level (DEVLOG
        #95): `fraction()` has no content to divide, so `refresh_hold` keeps the
        last value it had, while the engine is passing the inflow straight
        through to the outflows. Whatever leaves an empty level IS what is
        arriving, so the hold is re-armed from the arriving blend -- which is
        why the inflow rates are accumulated FIRST and the outflows read them.
        """
        for level in self._blended:
            for name in level.components:
                level.comp_in_rate[name] = level.comp_out_rate[name] = 0.0
        for flow in self.flows:
            if flow.dst is not None and flow.dst.components:
                moved = flow.actual * flow.efficiency
                for name in flow.dst.components:
                    flow.dst.comp_in_rate[name] += moved * self._carried(flow, name)
        for level in self._blended:
            if level.level > level.minimum + EPS or level.in_rate <= EPS:
                continue
            for name in level.components:
                level.hold[name] = level.comp_in_rate[name] / level.in_rate
        for flow in self.flows:
            if flow.src is not None and flow.src.components:
                for name in flow.src.components:
                    flow.src.comp_out_rate[name] += flow.actual * flow.src.hold[name]

    def _carried(self, flow: Flow, name: str) -> float:
        if name in flow.comp_frac:
            return flow.comp_frac[name]
        if flow.src is not None and name in flow.src.hold:
            return flow.src.hold[name]
        return 0.0

    def _check_thresholds(self) -> None:
        """Signals, variables and a bus event for every threshold crossed since
        the last check. Idempotent: the reference levels advance with it, so a
        discrete jump (a TRANSFER) fires exactly as a continuous crossing does."""
        env, ctx = self.env, self.env.ctx
        for level in self._thresholded_levels:      # the only ones with anything to fire
            before = self._before.get(level, level.level)
            after = level.level
            self._before[level] = after
            for threshold in level.thresholds:
                if not threshold.crossed(before, after):
                    continue
                if threshold.variable:
                    ctx.variables[threshold.variable] = after
                    ctx.touch(("var", threshold.variable))      # PHASE5 O4
                if threshold.signal:
                    # Exactly the SIGNAL block's mechanism (core/blocks/signal.py):
                    # succeed the event, releasing every HOLD waiting on it, then
                    # replace it -- broadcast, edge-triggered, auto-reset.
                    if threshold.signal not in ctx.signals:
                        ctx.signals[threshold.signal] = env.event()
                    ctx.signals[threshold.signal].succeed()
                    ctx.signals[threshold.signal] = env.event()
                if ctx.bus.has_subscribers:
                    ctx.bus.emit("level_threshold", block=None, t=env.now, resource=level.name,
                                 state=threshold.signal or threshold.variable or "threshold",
                                 value=after)

    def mark_breakpoint(self) -> None:
        """Record where the levels are RIGHT NOW, before a discrete lump moves
        one (PHASE4 T2, D75). TRANSFER calls it after its own `sync`, so the
        trajectory carries the value just before the jump as well as the one
        just after and playback draws a jump rather than a ramp."""
        self._emit_levels()

    def _emit_levels(self) -> None:
        """A trajectory BREAKPOINT per level on the bus (PHASE4 T2, D75).

        A level is piecewise linear between two RE-EVALUATIONS (D60), so its
        corners are the whole curve and playback interpolates between them --
        no sampling, and memory bounded by the number of re-evaluations rather
        than by the run length. `_recompute` is therefore the emit site, since
        that IS the re-evaluation; `sync` is not, because the time-series
        sampler and any `LEVEL()` read call it and would turn the breakpoints
        back into samples.

        Costs one attribute test per re-evaluation when nobody is listening,
        which is every run without tracing. A level's component shares (D65)
        are not recorded: the canvas draws one bar, not a stack.
        """
        ctx = self.env.ctx
        if not ctx.bus.has_subscribers:
            return
        t = float(self.env.now)
        corners, self._belt_corners = self._belt_corners, ()
        now = [(name, level.level) for name, level in self.levels.items() if not level.hidden]
        for name, value in (*corners, *now):
            if self._emitted.get(name) == (t, value):
                continue        # the same corner from the other emit site
            self._emitted[name] = (t, value)
            ctx.bus.emit("level_changed", block=None, t=t, resource=name, value=value)

    def _serve_pending(self) -> None:
        """Grant queued TRANSFER demands, FIFO, while the level can pay."""
        for level in self.levels.values():
            while level.pending:
                amount, mode, event = level.pending[0]
                if mode == "remove":
                    if level.level < level.minimum + amount - EPS:
                        break
                    level.level -= amount
                    level.outflow_total += amount
                else:
                    room = math.inf if level.capacity is None else level.capacity - level.level
                    if room < amount - EPS:
                        break
                    level.level += amount
                    level.inflow_total += amount
                level.pending.pop(0)
                event.succeed(amount)

    # -- rate evaluation and saturation -------------------------------------
    def _evaluate(self, todo) -> None:
        """Evaluate the rates in *todo* into `flow.desired`.

        The one loop a continuous model spends its run in: the compiled
        closure and its scope are bound at build time, so a rate costs one
        call rather than `_value` -> `evaluate` -> the closure."""
        if self._auxed:
            self.env.ctx.aux_memo = {}      # AUX() is memoised for this pass only (SD-2)
        for flow, rate_fn, scope, _reads in todo:
            try:
                desired = float(rate_fn(scope))
            except ExpressionError as exc:
                raise ExpressionError(f"flow '{flow.name}' rate: {exc.message}") from None
            if desired < 0.0 and not flow.biflow:
                desired = 0.0
            # Semi-continuous: below its minimum load the machine is OFF.
            flow.desired = 0.0 if flow.min_rate and abs(desired) < flow.min_rate else desired
        if self._auxed:
            self.env.ctx.aux_memo = None

    def _recompute(self) -> None:
        """Evaluate every rate/price/factor expression and saturate, to a fixed
        point (so an accounting flow may read `FLOW()` of a physical one).

        On a pure grid wake-up (`_grid_wake`, D91) only the DYNAMIC rates are
        evaluated: nothing a static rate reads has moved since the last event."""
        grid = self._grid_wake
        for level in self._blended:
            level.carry_components()    # a TRANSFER lump carried the level's own blend
            level.refresh_hold()        # re-arm the zero-order hold at the true fraction
        first = True
        todo = self._dynamic if grid else self._rates
        later = self._later_grid if grid else self._later
        for _ in range(MAX_EVAL_PASSES):
            self._evaluate(todo)
            if first and self.rk4:
                self._rk4_rates()
            # `_apply` is where the realised rates are written, so it is where
            # "this pass changed nothing" is known -- no second copy of every
            # rate per pass to compare against. The first pass never ends the
            # loop: it is compared against the PREVIOUS event's rates -- unless
            # no rate can re-read anything, when a second pass would repeat the
            # first exactly (`_saturate` is a pure function of the desired rates).
            stable = self._saturate()
            if (stable and not first) or not later:
                break
            first = False
            # Only `_saturate` ran, and only the rates it moved can have
            # changed anything: the next pass re-evaluates the rates that read
            # one of them, and no others (ARCHITECTURE 5.19).
            moved = self._moved
            todo = [entry for entry in later
                    if entry[3] is None or not entry[3].isdisjoint(moved)]
        for flow in self._priced:
            flow.price = self._value(flow.price_expr, flow.name, "price", flow)
            flow.impact_factor = self._value(flow.impact_expr, flow.name, "emission_factor", flow)
        if self._blended:
            for flow in self.flows:
                for name, expression in flow.comp_exprs.items():
                    flow.comp_frac[name] = self._value(expression, flow.name, "components", flow)
            self._component_rates()
        for conversion in self.conversions:
            for substrate in conversion.substrates:
                substrate.feed_rate = max(self._value(
                    substrate.feed_expr, f"{conversion.name}/{substrate.name}", "feed",
                    conversion, kind="conversion"), 0.0)
        for pipeline in self.pipelines:     # its input as of this re-evaluation (SD-2)
            pipeline.record(self.env.now)
        for entry in self.stateful:         # "sample"/"noise" delays (PHASE6 S2)
            entry.update(self.env.now)
        if not grid or self._stride == 1 or self._grid_corner_due():
            self._emit_levels()     # the corner this re-evaluation just created
        self._belt_corners = ()     # a thinned grid step drops its conveyor corner too (#238)
        ctx = self.env.ctx
        if ctx.watched:         # a HOLD condition reading LEVEL() (PHASE5 O4)
            for name in self.levels:
                ctx.touch(("level", name))

    def _grid_corner_due(self) -> bool:
        """Trace stride for grid-only corners (D75, SD-1): a long SD run would
        otherwise record every level at every step. Emit every `stride`-th
        grid step, and at once when any level's slope changes sign, so a peak
        or a trough is never cut off by the thinning."""
        if not self.env.ctx.bus.has_subscribers:
            return False
        self._grid_count += 1
        flipped = False
        for name, level in self.levels.items():
            net = level.in_rate - level.out_rate
            sign = (net > EPS) - (net < -EPS)
            if self._slopes.get(name, sign) != sign:
                flipped = True
            self._slopes[name] = sign
        return flipped or self._grid_count % self._stride == 0

    def _rk4_rates(self) -> None:
        """Replace every dynamic rate's `desired` by the classical RK4 EFFECTIVE
        rate over the step to the next grid point (D90).

        The stages move the levels to `y0 + c*h*net` (clamped to the bounds)
        and the clock to `now + c*h`, re-evaluate the dynamic rates, and put
        everything back; the step then runs at `(k1 + 2 k2 + 2 k3 + k4) / 6`
        through the normal saturation. The level stays LINEAR inside the step,
        so sync, the trapezoid statistics, closed-form crossings, thresholds
        and playback are all unchanged. Stages do not saturate, fire nothing
        and re-read no bus. A blended level keeps its fraction in a stage."""
        now = self.env.now
        h = self._grid_after(now) - now
        ctx = self.env.ctx
        self.sync()     # so a LEVEL() read inside a stage cannot integrate anything
        dynamic = self._dynamic
        start = {level: level.level for level in self.levels.values()}
        content = {level: dict(level.content) for level in self._blended}
        ks = [[flow.desired for flow, *_ in dynamic]]
        try:
            for c in (0.5, 0.5, 1.0):
                net = dict.fromkeys(start, 0.0)
                for flow in self.flows:
                    if flow.src is not None:
                        net[flow.src] -= flow.desired
                    if flow.dst is not None:
                        net[flow.dst] += flow.desired * flow.efficiency
                for level, y0 in start.items():
                    y = y0 + c * h * net[level]
                    if y < level.minimum:
                        y = level.minimum
                    elif level.capacity is not None and y > level.capacity:
                        y = level.capacity
                    level.level = y
                for level, saved in content.items():
                    y0 = start[level]
                    share = level.level / y0 if y0 > EPS else 1.0
                    level.content = {name: value * share for name, value in saved.items()}
                ctx.stage_offset = c * h
                self._evaluate(dynamic)
                ks.append([flow.desired for flow, *_ in dynamic])
        finally:
            ctx.stage_offset = 0.0
            for level, y0 in start.items():
                level.level = y0
            for level, saved in content.items():
                level.content = saved
        for i, (flow, *_rest) in enumerate(dynamic):
            flow.desired = (ks[0][i] + 2.0 * ks[1][i] + 2.0 * ks[2][i] + ks[3][i]) / 6.0

    def _value(self, expression, flow_name: str, field: str, flow=None, kind="flow") -> float:
        if expression is None:
            return 0.0
        try:
            return float(expression.evaluate(
                self._scope if flow is None or flow.scope is None else flow.scope))
        except ExpressionError as exc:
            raise ExpressionError(f"{kind} '{flow_name}' {field}: {exc.message}") from None

    def _saturate(self) -> bool:
        """Scale every flow down until every bound and power limit holds, and
        say whether any realised rate actually moved.

        A cut on a flow's SOURCE side scales `src_scale` (the rate drawn), one
        on its DESTINATION side scales `dst_scale` (the rate accepted); keeping
        the two apart is what lets `_apply` say whether the lost quantity was
        unmet demand or spillage. The outer loop is the fixed point: scaling a
        flow changes the totals at its other end, and since every pass only
        ever scales down, it converges.

        The scales are lists indexed by each flow's position, not dicts keyed
        by its name: this runs several times per pass per level, and a name
        hash per flow per inner sum was showing up in the profile."""
        src = [1.0] * len(self.flows)
        dst = [1.0] * len(self.flows)
        for _ in range(MAX_SATURATION_PASSES):
            changed = False
            for level in self.levels.values():
                full = level.capacity is not None and level.level >= level.capacity - EPS
                empty = level.level <= level.minimum + EPS
                if not (full or empty) and level.max_inflow is None and level.max_outflow is None:
                    continue    # no limit can bind: skip the two sums (SD-3 cheap win)
                ins = self._ins[level]
                outs = self._outs[level]
                for _inner in range(4):     # one limit per pass, totals recomputed each time
                    in_rate = sum(f.desired * src[f.index] * dst[f.index] * f.efficiency
                                  for f in ins)
                    out_rate = sum(f.desired * src[f.index] * dst[f.index] for f in outs)
                    limit = None
                    if level.max_inflow is not None and in_rate > level.max_inflow + EPS:
                        limit = (ins, dst, in_rate, level.max_inflow)
                    elif level.max_outflow is not None and out_rate > level.max_outflow + EPS:
                        limit = (outs, src, out_rate, level.max_outflow)
                    elif full and in_rate > out_rate + EPS:
                        limit = (ins, dst, in_rate, out_rate)      # nowhere to put it: spill
                    elif empty and out_rate > in_rate + EPS:
                        limit = (outs, src, out_rate, in_rate)     # nothing to draw: unmet
                    if limit is None:
                        break
                    flows, scale, total, cap = limit
                    share = max(cap, 0.0) / total
                    for f in flows:
                        scale[f.index] *= share
                    changed = True
            if not changed:
                break
        return self._apply(src, dst)

    def _apply(self, src_scale: list, dst_scale: list) -> bool:
        """Write the saturated rates back, and attribute what was lost: a flow
        cut on its SOURCE side is unmet demand there, one cut on its
        DESTINATION side is spilled quantity there.

        Returns True when no realised rate moved by more than EPS -- which is
        the evaluation fixed point's own stopping test, known here for free --
        and leaves the bus keys that DID move in `_moved`, which is what tells
        the next pass which rates are worth re-evaluating."""
        self._sums.clear()      # the realised rates move here, and only here
        moved = self._moved
        moved.clear()
        for level in self.levels.values():
            level.in_rate = level.out_rate = level.spill_rate = level.unmet_rate = 0.0
        stable = True
        for flow in self.flows:
            after_src = flow.desired * src_scale[flow.index]
            actual = after_src * dst_scale[flow.index]
            # `moved` is EXACT, not within EPS: a sub-EPS move still has to
            # reach the rates that read it, exactly as it did when every rate
            # was re-evaluated on every pass. `stable` keeps the EPS tolerance,
            # because that is the fixed point's own stopping rule.
            if actual != flow.actual:
                moved.add(("flow", flow.name))
                if flow.tag:
                    moved.add(("tag", flow.tag))
                if stable and abs(actual - flow.actual) > EPS:
                    stable = False
            flow.actual = actual
            if flow.src is not None:
                flow.src.out_rate += flow.actual
                flow.src.unmet_rate += flow.desired - after_src
            if flow.dst is not None:
                flow.dst.in_rate += flow.actual * flow.efficiency
                flow.dst.spill_rate += (after_src - flow.actual) * flow.efficiency
        for belt in self._conveyors:    # a conveyor moves only at its grid step (C1)
            level = belt.level
            level.in_rate = level.out_rate = level.spill_rate = level.unmet_rate = 0.0
        return stable

    def _snap(self, level: Level, value: float) -> float:
        """Clamp to the bounds and snap anything within EPS of a bound OR of a
        threshold exactly onto it -- the whole anti-chatter guarantee (item i).

        The threshold half is DEVLOG #93: a crossing instant is computed as
        `(value - level) / rate` and then replayed as `level + rate * dt`, which
        in floating point can land an ulp SHORT of the threshold. The threshold
        then does not fire (its test is half-open at the arriving end), the next
        crossing is sub-EPS and is skipped as Zeno, and a rate expression
        testing `LEVEL(...) >= value` never sees it either. Snapping onto the
        value the engine deliberately aimed at removes all three.
        """
        if value <= level.minimum + EPS:
            return level.minimum
        if level.capacity is not None and value >= level.capacity - EPS:
            return level.capacity
        for threshold in level.thresholds:
            if abs(value - threshold.value) <= EPS:
                return threshold.value
        return value


# ---------------------------------------------------------------- building
def build_level_system(env, layout: dict, series: dict) -> LevelSystem | None:
    """Build the run's `LevelSystem` from the layout's "levels"/"flows"
    sections, or None when the model declares neither (so a model without
    levels pays nothing at all)."""
    level_section = layout.get("levels") or []
    flow_section = layout.get("flows") or []
    conversion_section = layout.get("conversions") or []
    delay_section = layout.get("delays") or []      # expanded by `sd.expand` (SD-2)
    if not level_section and not flow_section and not conversion_section and not delay_section:
        return None

    base_unit = getattr(env.ctx, "time_unit", "minutes")
    levels: dict[str, Level] = {}
    for entry in level_section:
        name = str(entry.get("name", ""))
        if not name:
            raise LevelError("a level has no name")
        if name in levels:
            raise LevelError(f"duplicate level name: {name}")
        rate_scale = base_factor("rate", entry.get("rate_time_unit", "base") or "base",
                                 ModelUnits(base_unit))
        # A conveyor (C1) enforces its own capacity and cannot go negative, so
        # the saturation rule has no bound to apply to it.
        belt = entry.get("conveyor")
        levels[name] = Level(
            name,
            capacity=None if belt else entry.get("capacity"),
            minimum=None if belt else entry.get("min", 0.0),    # None: unbounded below (SD-1)
            # An expression is evaluated below, once the system exists (SD-2).
            initial=(0.0 if isinstance(entry.get("initial"), str)
                     else entry.get("initial", 0.0) or 0.0),
            unit=entry.get("unit", ""),
            max_inflow=None if belt else _scaled(entry.get("max_inflow"), rate_scale),
            max_outflow=None if belt else _scaled(entry.get("max_outflow"), rate_scale),
            thresholds=[_threshold(t) for t in entry.get("thresholds") or []],
            components=[(c.get("name", ""), c.get("initial_fraction", 0.0) or 0.0)
                        for c in entry.get("components") or []],
            tolerance=entry.get("blend_tolerance", DEFAULT_BLEND_TOLERANCE),
            hidden=entry.get("hidden", False))

    variables = set(getattr(env.ctx, "variables", {}) or {})
    for entry in level_section:
        belt = entry.get("conveyor")
        if belt:
            locals_ = entry.get("locals") or {}
            names = variables | set(locals_) if locals_ else variables
            level = levels[str(entry["name"])]
            fields = SECTION_FIELDS["levels.conveyor"]
            transit = to_base(param(fields, "transit_time"), belt, ModelUnits(base_unit), fields)
            level.conveyor = _Conveyor(
                level, _expr(transit, names), _expr(belt.get("capacity"), names),
                _expr(belt.get("in_limit"), names), _expr(belt.get("arrest"), names),
                belt.get("leak_kind", "linear") == "exponential",
                ExpressionScope(ctx=env.ctx, locals=dict(locals_)) if locals_ else None)
    flows = []
    for entry in flow_section:
        name = str(entry.get("name", ""))
        if not name:
            raise LevelError("a flow has no name")
        scale = base_factor("rate", entry.get("rate_time_unit", "base") or "base",
                            ModelUnits(base_unit))
        # A `$`-cloned flow carries its instance's submodel parameters, exactly
        # as an expanded BLOCK does (PHASE4 D3), so an asset's rate can read
        # its own typed parameters: "TS(profile) * kwp * pr".
        locals_ = entry.get("locals") or {}
        names = variables | set(locals_) if locals_ else variables
        flows.append(Flow(
            name,
            _level_of(levels, entry.get("from"), name),
            _level_of(levels, entry.get("to"), name),
            _rate(entry.get("rate") or "0", scale, names, locals_),  # "": a conveyor's (C1)
            efficiency=entry.get("efficiency", 1.0) or 1.0,
            price_expr=_expr(entry.get("price"), names),
            impact_expr=_expr(entry.get("emission_factor"), names),
            tag=entry.get("tag", ""), priority=entry.get("priority", 0.0),
            peak_price=entry.get("peak_price", 0.0) or 0.0,
            min_rate=_scaled(entry.get("min_rate"), scale),
            comp_exprs={str(key): _expr(value, names)
                        for key, value in (entry.get("components") or {}).items()},
            thresholds=[_threshold(t) for t in entry.get("thresholds") or []],
            scope=(ExpressionScope(ctx=env.ctx, locals=dict(locals_)) if locals_ else None),
            biflow=entry.get("biflow", False), hidden=entry.get("hidden", False),
            leak=_leak_of(entry.get("leak"), names)))

    auxes = {name: expression for name, (expression, _locals)
             in (getattr(env.ctx, "auxiliaries", None) or {}).items()}
    system = LevelSystem(env, levels, flows,
                         build_conversions(env, layout, variables), auxes=auxes)
    system._series = tuple(series.values())
    _build_delays(system, env, delay_section, variables)
    instants = set()
    for expression in ([f.rate_expr for f in flows] + list(auxes.values())
                       + [p.input for p in system.pipelines]):
        instants.update(expression.instants or ())
    system._instants = tuple(sorted(instants))
    twins = {str(entry["name"]): (compile_expression(str(entry["initial"])),
                                  dict(entry["locals"]) if entry.get("locals") else None)
             for entry in layout.get("auxiliaries") or [] if entry.get("initial")}
    _start_initials(system, env, level_section, variables, auxes, delay_section, twins)
    spec = (layout.get("simulation") or {}).get("sd")
    if spec:
        system.set_sd(float(spec["dt"]) * base_factor("time", spec.get("time_unit", "base") or "base",
                                                       ModelUnits(base_unit)),
                      spec.get("method", "euler"))
    if system._conveyors and (system.dt is None or system.rk4):
        raise LevelError("a conveyor needs simulation.sd with method \"euler\"")
    return system


def _leak_of(spec, names):
    """A leak flow's (fraction, zone start, zone end) (C1), or None."""
    if not spec:
        return None
    start, end = spec.get("zone") or (0.0, 1.0)
    return compile_expression(str(spec.get("fraction", 0)), names), float(start), float(end)


def _own_scope(env, entry: dict, fallback):
    locals_ = entry.get("locals") or {}
    return ExpressionScope(ctx=env.ctx, locals=dict(locals_)) if locals_ else fallback


def _build_delays(system: LevelSystem, env, section: list, variables: set) -> None:
    """DELAY() outputs (SD-2, PHASE6 S2): a material/information delay's is an
    expression over its hidden last stage, a pipeline's the `_Pipeline`
    itself, a sample's the `_Sampler` and a noise's the `_Noise`."""
    for entry in section:
        name = str(entry.get("name", ""))
        locals_ = entry.get("locals") or {}
        names = variables | set(locals_) if locals_ else variables
        scope = _own_scope(env, entry, system._scope)
        kind = entry.get("kind", "material")
        if kind == "pipeline":
            tau = compile_expression(str(entry.get("delay_time")), names).evaluate(scope)
            pipeline = _Pipeline(name, compile_expression(str(entry.get("input")), names), tau,
                                 _expr(entry.get("initial"), names), scope)
            system.pipelines.append(pipeline)
            system.delays[name] = (pipeline, None)
        elif kind == "sample":
            sampler = _Sampler(name, compile_expression(str(entry.get("condition")), names),
                               compile_expression(str(entry.get("input")), names),
                               _expr(entry.get("initial"), names), scope)
            system.stateful.append(sampler)
            system.delays[name] = (sampler, None)
        elif kind == "noise":
            noise = _Noise(name, *(compile_expression(str(entry.get(field)), names)
                                   for field in ("mean", "sd", "lo", "hi")),
                          scope, env.ctx.seed)
            system.stateful.append(noise)
            system.delays[name] = (noise, None)
        else:
            system.delays[name] = (compile_expression(sd.output_text(entry), names), scope)


def _start_initials(system: LevelSystem, env, section: list, variables: set, auxes: dict,
                    delays: list, twins: dict | None = None) -> None:
    """Evaluate the levels' `initial` EXPRESSIONS (SD-2) in dependency order --
    a level's initial may read another's through LEVEL(), AUX(), DELAY() or
    FLOW() (validation refused a cycle, E_LEVEL_INITIAL_CYCLE).

    This pass is Vensim's initialisation: an auxiliary's ACTIVE_INITIAL
    `init` twin (*twins*, DEVLOG #160) replaces its expression, and FLOW()
    reads the flow's rate expression, since no rate exists yet (#161)."""
    pending = {}
    for entry in section:
        if isinstance(entry.get("initial"), str):
            locals_ = entry.get("locals") or {}
            names = variables | set(locals_) if locals_ else variables
            pending[str(entry["name"])] = (compile_expression(entry["initial"], names),
                                           _own_scope(env, entry, system._scope))
    if not pending:
        return
    env.ctx.level_system = system       # what LEVEL()/DELAY() read, before the model has it
    saved = env.ctx.auxiliaries
    if twins:
        env.ctx.auxiliaries = {**saved, **twins}
        auxes = {**auxes, **{name: expression for name, (expression, _own) in twins.items()}}
    reads = sd.delay_reads(delays)
    rates = {flow.name: flow.rate_expr for flow in system.flows}
    graph = {name: {ref for found in sd.reach(expression, auxes, reads, rates)
                    for ref in found.refs.get("LEVEL", ()) if ref in pending}
             for name, (expression, _scope) in pending.items()}
    system._initialising = True
    try:
        for name in TopologicalSorter(graph).static_order():
            expression, scope = pending[name]
            try:
                value = float(expression.evaluate(scope))
            except ExpressionError as exc:
                raise ExpressionError(f"level '{name}' initial: {exc.message}") from None
            system.levels[name].restart(value)
    finally:
        system._initialising = False
        env.ctx.auxiliaries = saved
    system._before = {level: level.level for level in system.levels.values()}


def _threshold(entry: dict) -> _Threshold:
    return _Threshold(entry.get("value", 0.0), entry.get("direction", "falling"),
                      entry.get("signal"), entry.get("variable"),
                      entry.get("cost", 0.0), entry.get("cost_item", ""),
                      entry.get("category", ""))


def _scaled(value, scale: float):
    return None if value is None else float(value) * scale


def _expr(text, variables):
    return None if text in (None, "") else compile_expression(str(text), variables)


def _rate(text, scale: float, variables, locals_):
    """A rate expression, converted into the model's base time unit. A rate in
    "per hour" on a model that counts minutes is `expr / 60`, so the scaling
    rides along inside the compiled expression rather than costing a multiply
    at every evaluation.

    The flow's submodel parameters are folded in (`bind_constants`): nothing
    writes a flow's `locals` after the build, and the rate loop is where a
    continuous model spends its run."""
    text = str(text)
    return bind_constants(text if scale == 1.0 else f"({text}) * {scale!r}", variables, locals_)


def _level_of(levels: dict, name, flow_name: str):
    if name in (None, ""):
        return None
    level = levels.get(str(name))
    if level is None:
        raise LevelError(f"flow '{flow_name}': no level named '{name}'")
    return level
