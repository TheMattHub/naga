"""Delayed conversion with a yield curve (Bio-1 G1, ARCHITECTURE D64).

A digester is fed tonnes of maize today and releases biogas over the next ten
days. That is not a flow: the output rate now depends on what went in over a
whole history, per feedstock, each with its own yield and its own release
curve. So `conversions` is its own section, beside `levels` and `flows`.

THE EXACTNESS RULE, unchanged from D60: a rate must be piecewise constant.
Two mechanisms keep it so.

1. COHORTS. Feed is accumulated and closed at a fixed `cohort_interval`
   (default 1 hour). A cohort's content is known when it closes, and from then
   on it releases at a rate that is constant within each step of its curve.
2. A SHARED GRID. Every curve knot age is rounded onto the cohort grid, so
   every cohort of every substrate changes its rate only at grid instants
   `k * cohort_interval`. A year of hourly cohorts with 3 substrates and
   12-step curves is 8760 engine events, not 3 x 8760 x 12.

Compute. The live cohorts are never scanned: closing a cohort posts its
`steps + 1` rate DELTAS into a dict keyed by grid index, and each tick is one
`dict.pop` plus one addition. Memory is bounded by the curve length in grid
steps, not by the run length.

The two approximations, stated:

* a cohort releases from its CLOSE, so the mean delay of everything fed into
  it is longer by up to half an interval (exactly half for a constant feed).
  Halving `cohort_interval` halves it.
* knot ages are rounded to the grid: at most half an interval per knot, and a
  knot may never collapse onto its predecessor (it is pushed one grid step on).

Conservation is EXACT regardless of both: the rates of a cohort's steps are
derived from its own quantity, so a cohort that completes its curve has
released `quantity x yield` to the last bit. What has not been released yet is
reported as `Unreleased`, tracked as a STATE (so a warm-up reset of the
statistics cannot corrupt it) rather than as fed-minus-produced.

Engine-side leaf: stdlib plus `core/expressions.py`, no pandas, no Qt.
"""
from __future__ import annotations

import math

from nagasim.core.expressions import ExpressionScope, compile_expression
from nagasim.core.problems import problem
from nagasim.core.timeunits import ModelUnits, base_factor
from nagasim.core.utils import is_number as _is_number

__all__ = ["Conversion", "Substrate", "ConversionError", "build_conversions",
           "validate_conversions_structured", "DEFAULT_STEPS", "DEFAULT_TAIL"]

DEFAULT_STEPS = 12          # steps a half-life curve is discretised into
DEFAULT_TAIL = 6.0          # half-lives that discretisation spans (98.4% of the yield)
EPS = 1e-9


class ConversionError(ValueError):
    """A malformed conversion. `validate_conversions_structured` reports the
    same faults as Problems long before a run reaches here."""


class Substrate:
    """One feedstock of one conversion: its feed rate expression, its yield and
    its discretised release curve.

    `steps` is the curve as `(end_index, rate_per_unit_fed)` pairs, where
    `end_index` counts grid steps from the cohort's close and the rate is
    already multiplied by the yield -- so a cohort of quantity q releases
    `q * rate` per base time unit while it is inside that step.
    """

    __slots__ = ("name", "yield_per_unit", "steps", "components", "feed_expr",
                 "feed_rate", "rate", "pending", "pipeline", "fed", "produced", "peak",
                 "_deltas", "_start")

    def __init__(self, name, yield_per_unit, steps, components=None, feed_expr=None):
        self.name = name
        self.yield_per_unit = float(yield_per_unit)
        self.steps = tuple(steps)
        self.components = dict(components or {})
        self.feed_expr = feed_expr
        self.feed_rate = 0.0
        self.rate = 0.0              # released quantity per base time unit, now
        self.pending = 0.0           # fed since the last cohort close
        self.pipeline = 0.0          # closed but not yet released
        self._deltas: dict[int, float] = {}
        self.reset_stats(0.0)

    # -- statistics ---------------------------------------------------------
    def reset_stats(self, t: float) -> None:
        """Warm-up resets the STATISTICS only: the live cohorts keep releasing
        and `pipeline` is state, not a statistic."""
        self._start = t
        self.fed = self.produced = self.peak = 0.0

    def avg_rate(self, t_end: float) -> float:
        span = t_end - self._start
        return self.produced / span if span > 0 else 0.0

    @property
    def unreleased(self) -> float:
        """Quantity of product still to come: what closed cohorts owe, plus the
        yield of feed that has not been closed into a cohort yet."""
        return self.pipeline + self.pending * self.yield_per_unit

    # -- the cohort machinery ----------------------------------------------
    def accumulate(self, dt: float) -> None:
        self.pending += self.feed_rate * dt
        released = self.rate * dt
        self.produced += released
        self.pipeline -= released
        self.peak = max(self.peak, self.rate)

    def close(self, index: int, now_index: int) -> None:
        """Close the pending feed as one cohort at grid *index* and post its
        rate deltas. A boundary at or before *now_index* (the initial-inventory
        ladder, which closes cohorts in the past) lands on the rate directly."""
        quantity, self.pending = self.pending, 0.0
        if quantity <= 0.0:
            return
        self.fed += quantity
        self.pipeline += quantity * self.yield_per_unit
        previous, start = 0.0, 0
        for end, rate in self.steps:
            self._delta(index + start, quantity * (rate - previous), now_index)
            previous, start = rate, end
        self._delta(index + start, -quantity * previous, now_index)

    def _delta(self, index: int, amount: float, now_index: int) -> None:
        if index <= now_index:
            self.rate += amount
        else:
            self._deltas[index] = self._deltas.get(index, 0.0) + amount

    def tick(self, index: int) -> None:
        delta = self._deltas.pop(index, None)
        if delta is not None:
            self.rate += delta
        if not self._deltas:
            # Every closed cohort has finished its curve. Snapping both to zero
            # is what makes conservation exact instead of float-exact: the
            # deltas of one cohort sum to 0 by construction.
            self.rate = 0.0
            self.pipeline = 0.0

    @property
    def horizon_index(self) -> int:
        """Grid steps from close to the end of the curve."""
        return self.steps[-1][0] if self.steps else 0

    def __repr__(self):
        return f"<Substrate {self.name!r} rate={self.rate:g}>"


class Conversion:
    """One delayed converter: several substrates in, one product rate out.

    The product is read by `CONV("name")` and its blend by
    `FRACTION("name", "component")`, so it enters the model as an ordinary
    flow's rate expression. That is deliberate (D64): the flow then gets the
    whole existing machinery -- a destination level, saturation and spill when
    that level is full, a tag on the site bus, a price, an impact factor -- and
    the conversion itself needs none of it.
    """

    __slots__ = ("name", "unit", "interval", "substrates", "index", "scope")

    def __init__(self, name, interval, substrates, unit="", scope=None):
        self.name = name
        self.unit = unit
        self.interval = float(interval)
        self.substrates = list(substrates)
        self.index = 0
        self.scope = scope

    @property
    def rate(self) -> float:
        return math.fsum(s.rate for s in self.substrates)

    def component_rate(self, component: str) -> float:
        return math.fsum(s.rate * s.components.get(component, 0.0) for s in self.substrates)

    def fraction(self, component: str) -> float:
        """The blend being released RIGHT NOW: the share of the product rate
        that is *component*. Zero while nothing is being released -- the flow
        carrying it is zero then too, so no component is created or lost."""
        total = self.rate
        return self.component_rate(component) / total if total > EPS else 0.0

    def next_time(self) -> float:
        return (self.index + 1) * self.interval

    def advance_to(self, t: float) -> None:
        """Close cohorts and apply curve-step boundaries up to time *t*."""
        target = int(math.floor(t / self.interval + EPS))
        while self.index < target:
            self.index += 1
            for substrate in self.substrates:
                substrate.tick(self.index)
                substrate.close(self.index, self.index)

    def accumulate(self, dt: float) -> None:
        for substrate in self.substrates:
            substrate.accumulate(dt)

    def reset_stats(self, t: float) -> None:
        for substrate in self.substrates:
            substrate.reset_stats(t)

    def __repr__(self):
        return f"<Conversion {self.name!r} rate={self.rate:g}>"


# ---------------------------------------------------------------- the curve
def half_life_knots(half_life: float, steps: int, tail: float) -> list[tuple]:
    """A first-order decay `F(a) = 1 - 2^(-a/h)` as equal-width steps over
    `tail` half-lives, renormalised so the last knot is exactly 1.

    Renormalising rather than truncating is what keeps conservation exact: the
    tail beyond the span (1.6% at the default 6 half-lives) is spread over the
    steps instead of being lost, which releases the curve very slightly early.
    """
    span = tail * half_life
    denominator = 1.0 - 2.0 ** (-tail)
    out = []
    for i in range(1, steps + 1):
        age = span * i / steps
        out.append((age, (1.0 - 2.0 ** (-age / half_life)) / denominator))
    return out


def _grid_steps(knots, interval: float, yield_per_unit: float) -> tuple:
    """Knots `(age, cumulative fraction)` -> `(end grid index, rate per unit fed)`.

    Ages round to the nearest grid index; a knot that would collapse onto its
    predecessor is pushed one step on, so every step has a positive width and a
    finite rate.
    """
    steps = []
    previous_index, previous_cum = 0, 0.0
    for age, cumulative in knots:
        index = max(previous_index + 1, int(round(age / interval)))
        width = (index - previous_index) * interval
        steps.append((index, yield_per_unit * (cumulative - previous_cum) / width))
        previous_index, previous_cum = index, float(cumulative)
    return tuple(steps)


# ---------------------------------------------------------------- building
def build_conversions(env, layout: dict, variables) -> list[Conversion]:
    """Build the run's conversions from the layout's "conversions" section."""
    base_unit = getattr(env.ctx, "time_unit", "minutes")
    out: list[Conversion] = []
    seen: set[str] = set()
    for entry in layout.get("conversions") or []:
        name = str(entry.get("name", ""))
        if not name:
            raise ConversionError("a conversion has no name")
        if name in seen:
            raise ConversionError(f"duplicate conversion name: {name}")
        seen.add(name)
        unit_factor = base_factor("time", entry.get("time_unit", "base") or "base",
                                  ModelUnits(base_unit))
        interval = float(entry.get("cohort_interval", 1.0) or 1.0) * unit_factor
        if interval <= 0:
            raise ConversionError(f"conversion '{name}': cohort_interval must be positive")
        locals_ = entry.get("locals") or {}
        names = set(variables) | set(locals_) if locals_ else set(variables)
        scope = ExpressionScope(ctx=env.ctx, locals=dict(locals_)) if locals_ else None
        substrates = [_build_substrate(name, spec, interval, unit_factor, base_unit, names,
                                       scope or ExpressionScope(ctx=env.ctx))
                      for spec in entry.get("substrates") or []]
        conversion = Conversion(name, interval, substrates, str(entry.get("unit", "") or ""),
                                scope)
        for substrate, spec in zip(substrates, entry.get("substrates") or []):
            _seed_inventory(substrate, spec, interval)
        out.append(conversion)
    return out


def _build_substrate(conversion: str, spec: dict, interval: float, unit_factor: float,
                     base_unit: str, names, scope) -> Substrate:
    name = str(spec.get("name", ""))
    if not name:
        raise ConversionError(f"conversion '{conversion}': a substrate has no name")
    yield_per_unit = float(spec.get("yield", 0.0) or 0.0)
    knots = _knots_of(conversion, name, spec, unit_factor)
    feed = spec.get("feed", "0")
    scale = base_factor("rate", spec.get("feed_time_unit", "base") or "base", ModelUnits(base_unit))
    text = str(feed)
    expression = compile_expression(text if scale == 1.0 else f"({text}) * {scale!r}", names)
    return Substrate(name, yield_per_unit, _grid_steps(knots, interval, yield_per_unit),
                     _component_values(spec.get("components"), names, scope), expression)


def _component_values(components: dict, names, scope) -> dict:
    """A substrate's component shares. An expression is allowed (an asset's
    `ch4_share` parameter) but is evaluated ONCE, at build: a feedstock's
    methane content is a property of the feedstock, not of the hour."""
    out = {}
    for key, value in (components or {}).items():
        if isinstance(value, str):
            value = compile_expression(value, names).evaluate(scope)
        out[str(key)] = float(value)
    return out


def _knots_of(conversion: str, name: str, spec: dict, unit_factor: float) -> list[tuple]:
    """The release curve as `(age in base time units, cumulative fraction)`."""
    curve = spec.get("curve")
    if curve:
        return [(float(age) * unit_factor, float(cumulative)) for age, cumulative in curve]
    half_life = float(spec.get("half_life", 0.0) or 0.0)
    if half_life <= 0:
        raise ConversionError(f"conversion '{conversion}', substrate '{name}': "
                              f"needs a \"curve\" or a positive \"half_life\"")
    knots = half_life_knots(half_life * unit_factor, int(spec.get("steps", DEFAULT_STEPS)),
                            float(spec.get("tail", DEFAULT_TAIL)))
    return knots


def _seed_inventory(substrate: Substrate, spec: dict, interval: float) -> None:
    """`initial_inventory` and `initial_feed_rate` (item 9).

    A year-long sizing study must not have to simulate a start-up transient.
    `initial_inventory` is one cohort closed at t=0. `initial_feed_rate` is the
    STEADY STATE for that feed rate: one cohort per grid step of the curve,
    closed in the past, so the output starts at `feed_rate x yield` and stays
    there (exactly, up to the cohort granularity).
    """
    inventory = float(spec.get("initial_inventory", 0.0) or 0.0)
    if inventory > 0:
        substrate.pending += inventory
        substrate.close(0, 0)
    feed_rate = float(spec.get("initial_feed_rate", 0.0) or 0.0)
    if feed_rate > 0:
        quantity = feed_rate * interval
        # Ages 0 .. N-1: in the steady state one cohort closed at each past grid
        # instant INCLUDING this one, which is what puts exactly `interval`
        # worth of cohorts inside every curve step and makes the output
        # `feed_rate x yield` exactly, not one cohort short.
        for age in range(substrate.horizon_index):
            substrate.pending += quantity
            substrate.close(-age, 0)


# ---------------------------------------------------------------- validation
def validate_conversions_structured(layout: dict) -> tuple[list, set, set]:
    """Check the "conversions" section.

    Returns (problems, {conversion names}, {component names declared anywhere}),
    the names `core/schema/` checks every literal `CONV()` call against.
    """
    problems: list = []
    names: set = set()
    components: set = set()
    for index, entry in enumerate(layout.get("conversions", []) or []):
        at = f"/conversions/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_CONVERSION_NAME",
                                    "conversion: each entry must be an object", at,
                                    hint="{\"name\": \"Digester\", \"substrates\": [...]}"))
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_CONVERSION_NAME", "conversion: name must not be empty",
                                    f"{at}/name", hint="give every conversion a unique name"))
            continue
        if name in names:
            problems.append(problem("E_CONVERSION_NAME", f"duplicate conversion name: {name}",
                                    f"{at}/name", hint="rename one of the two conversions"))
        names.add(name)
        interval = entry.get("cohort_interval", 1.0)
        if not _is_number(interval) or float(interval) <= 0:
            problems.append(problem(
                "E_CONVERSION_INTERVAL",
                f"conversion '{name}': \"cohort_interval\" must be a positive number",
                f"{at}/cohort_interval",
                hint="feed is grouped into cohorts closed at this interval; 1 hour is the "
                     "default and halving it halves the release delay it introduces"))
        substrates = entry.get("substrates") or []
        if not substrates:
            problems.append(problem(
                "E_CONVERSION_SUBSTRATE", f"conversion '{name}': declare at least one substrate",
                f"{at}/substrates",
                hint="{\"name\": \"maize\", \"feed\": \"FLOW(\\\"feed\\\")\", \"yield\": 200, "
                     "\"half_life\": 60}"))
        seen: set = set()
        for s_index, spec in enumerate(substrates):
            s_at = f"{at}/substrates/{s_index}"
            if not isinstance(spec, dict) or not str(spec.get("name", "")).strip():
                problems.append(problem("E_CONVERSION_SUBSTRATE",
                                        f"conversion '{name}': a substrate has no name",
                                        f"{s_at}/name", hint="name every feedstock"))
                continue
            s_name = str(spec["name"])
            if s_name in seen:
                problems.append(problem("E_CONVERSION_SUBSTRATE",
                                        f"conversion '{name}': duplicate substrate '{s_name}'",
                                        f"{s_at}/name", hint="rename one of the two"))
            seen.add(s_name)
            components.update(str(key) for key in (spec.get("components") or {}))
            _check_curve(problems, name, s_name, spec, s_at)
    return problems, names, components


def _check_curve(problems: list, name: str, s_name: str, spec: dict, at: str) -> None:
    where = f"conversion '{name}', substrate '{s_name}'"
    if not _is_number(spec.get("yield", 0.0)) or float(spec.get("yield", 0.0) or 0.0) < 0:
        problems.append(problem("E_CONVERSION_CURVE", f"{where}: \"yield\" must be a number >= 0",
                                f"{at}/yield",
                                hint="product per unit of feed, e.g. 200 Nm3 of biogas per tonne"))
    curve = spec.get("curve")
    if curve in (None, []):
        half_life = spec.get("half_life")
        if not _is_number(half_life) or float(half_life) <= 0:
            problems.append(problem(
                "E_CONVERSION_CURVE", f"{where}: needs a \"curve\" or a positive \"half_life\"",
                f"{at}/half_life",
                hint="\"half_life\": 60 discretises a first-order release into 12 equal-width "
                     "steps; \"curve\": [[24, 0.4], [72, 1.0]] gives the knots directly"))
        steps = spec.get("steps", DEFAULT_STEPS)
        if not _is_number(steps) or int(steps) < 1:
            problems.append(problem("E_CONVERSION_CURVE", f"{where}: \"steps\" must be >= 1",
                                    f"{at}/steps", hint=f"the default is {DEFAULT_STEPS}"))
        return
    if not isinstance(curve, list):
        problems.append(problem("E_CONVERSION_CURVE", f"{where}: \"curve\" must be a list of "
                                                      f"[age, cumulative fraction] pairs",
                                f"{at}/curve", hint="[[24, 0.4], [72, 0.8], [240, 1.0]]"))
        return
    age, cumulative = 0.0, 0.0
    for point in curve:
        if not (isinstance(point, (list, tuple)) and len(point) == 2
                and _is_number(point[0]) and _is_number(point[1])):
            problems.append(problem("E_CONVERSION_CURVE",
                                    f"{where}: every curve knot is [age, cumulative fraction]",
                                    f"{at}/curve", hint="[[24, 0.4], [72, 1.0]]"))
            return
        if float(point[0]) <= age or float(point[1]) < cumulative:
            problems.append(problem(
                "E_CONVERSION_CURVE",
                f"{where}: curve ages must increase and the fraction must not decrease",
                f"{at}/curve", hint="the fraction is CUMULATIVE: 0.4 at 24 h, 1.0 at 240 h"))
            return
        age, cumulative = float(point[0]), float(point[1])
    if abs(cumulative - 1.0) > 1e-9:
        problems.append(problem(
            "E_CONVERSION_CURVE", f"{where}: the last curve knot must be 1.0, not {cumulative:g}",
            f"{at}/curve",
            hint="the curve is the cumulative SHARE of the yield; scale \"yield\" instead"))


def validate_conversion_expressions(layout: dict, variables: set, check) -> None:
    """Compile every substrate's `feed` expression (called by `core/schema/`,
    which owns the shared `_check_expression` helper passed in as *check*)."""
    for index, entry in enumerate(layout.get("conversions", []) or []):
        if not isinstance(entry, dict):
            continue
        in_scope = variables | set(entry.get("locals") or {})
        for s_index, spec in enumerate(entry.get("substrates") or []):
            if not isinstance(spec, dict):
                continue
            feed = spec.get("feed")
            if feed not in (None, ""):
                check(f"conversion '{entry.get('name', '')}'", feed, in_scope,
                      f"/conversions/{index}/substrates/{s_index}/feed")
            for key, value in (spec.get("components") or {}).items():
                if isinstance(value, str) and value:
                    check(f"conversion '{entry.get('name', '')}' component '{key}'", value,
                          in_scope, f"/conversions/{index}/substrates/{s_index}/components/{key}")
