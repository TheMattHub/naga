"""Value types for a distribution's registry entry: DistParam, DistributionSpec.

Kept in their own leaf module (ROADMAP 2.4 A), mirroring `core/block_spec.py`'s
role for BLOCK_REGISTRY: no dependency on `distributions.py`, so a plugin
module can build a DistributionSpec without importing the (larger)
distributions module, and distributions.py can import this one with no
circularity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import math

__all__ = ["DistParam", "DistributionSpec", "ScaleParam", "bound_text", "value_problems"]


@dataclass(frozen=True)
class DistParam:
    name: str
    default: object
    hint: str = ""        # reserved for a future per-field tooltip; unused by the UI today
    kind: str = "number"  # "number" (spin box, fx-toggle to an expression) | "list" (JSON field) | "expression" | "choice"
    # "choice" only: the allowed words (a combo box); the value is a literal word, never an expression.
    choices: tuple = ()
    # D1221: the bounds of a "number" value, as a block ParamSpec states them (`min`, `max`,
    # `min_exclusive` = "> min"); validation (E_DIST_PARAM_VALUE) and the Properties field
    # read these, never their own copy. Set where the sampler refuses or divides by the value.
    min: float | None = None
    max: float | None = None
    min_exclusive: bool = False


@dataclass(frozen=True)
class ScaleParam:
    """One parameter of a location-scale family that scales linearly with the
    distribution's mean (W7 Q5 review item 3, ARCHITECTURE 5.37/D128 amended
    2026-09-26): `nagasim.core.distributions.scale_distribution_params`
    rewrites it as an expression of a per-visit scale factor, keeping every
    other (shape) parameter literal. `kind` is how the parameter itself moves
    when the MEAN is multiplied by a factor `f`:
    - "linear" (most location/scale params): new = old * f
    - "inverse" (a rate-parameterised param, e.g. Exponential's `rate` --
      mean = 1/rate, so rate must shrink as the mean grows): new = old / f
    - "log" (a log-scale location param, e.g. Lognormal's `mu` -- mean =
      exp(mu + sigma^2/2), so multiplying the mean by f adds ln(f) to mu):
      new = old + ln(f)
    """
    name: str
    kind: str = "linear"   # "linear" | "inverse" | "log"


@dataclass(frozen=True)
class DistributionSpec:
    name: str
    params: tuple                  # tuple[DistParam, ...]
    # sampler(rng, params_dict, *, current=None, scope=None, state=None) -> value.
    # Almost every sampler only needs (rng, params_dict) and declares the rest
    # via **_; `current`/`state` exist only for FixedSchedule's cursor and
    # `scope` only for CustomExpression's own evaluation (see distributions.py).
    sampler: Callable
    category: str                  # "continuous" | "discrete" | "special"
    use_case: str                  # multi-line description shown as a tooltip
    arrival: bool = False          # offered for CREATE (inter-arrival)
    delay: bool = False            # offered for DELAY/SEIZE/RELEASE/SIGNAL/DISPOSE
    support_lower: float | None = None   # None = support unbounded below (negative-duration warning)
    fit: str | None = None         # optional nagasim.inputfit family name that fits this distribution
    # mean(params_dict) -> float | None (PHASE6 W4 P1, D119): the closed-form
    # mean `StochasticDistribution.sample()` substitutes for a random draw in
    # deterministic mode, from the SAME parameterisation `sampler` uses; None
    # for a params combination with no finite mean (e.g. Cauchy, always).
    # Left unset (None) for the two distributions handled without one --
    # CustomExpression (its own RNG-drawing functions become deterministic in
    # core/expressions.py, so evaluating it normally already gives the mean)
    # and FixedSchedule (draws no random number in the first place).
    mean: Callable | None = None
    # The location/scale parameters that scale linearly with the mean (W7 Q5
    # review item 3, D128 amended 2026-09-26), or None when this family has no
    # well-defined rescale -- an empirical table, CustomExpression,
    # FixedSchedule, or a bounded family whose bounds are not themselves scale
    # params (e.g. BoundedNormal, Beta, JohnsonSB): `tablegen.py` refuses a
    # per-product `process_time` override at such a station.
    scale_params: tuple | None = None   # tuple[ScaleParam, ...] | None
    # D1221: (smaller, larger) parameter pairs the values must keep, e.g. Triangular's
    # (("low", "mode"), ("mode", "high")): low <= mode <= high.
    order: tuple = ()


def bound_text(param: DistParam) -> str:
    """'> 0', '>= 0', 'in [0, 1]', '<= 1' or '' for a parameter's bounds."""
    low = None if param.min is None else f"{'(' if param.min_exclusive else '['}{param.min:g}"
    if low is not None and param.max is not None:
        return f"in {low}, {param.max:g}]"
    if low is not None:
        return f"{'>' if param.min_exclusive else '>='} {param.min:g}"
    return "" if param.max is None else f"<= {param.max:g}"


def value_problems(spec: DistributionSpec, values: dict) -> list[tuple[str, str]]:
    """(parameter, what is wrong) for each value *values* gives that *spec* cannot use: an
    empty one, one outside its bounds, or a pair out of order. A string is an expression
    and is judged when it is evaluated; an absent key takes its default."""
    found = []
    for param in spec.params:
        if param.name not in values or param.kind != "number":
            continue
        value = values[param.name]
        if value is None:
            found.append((param.name, "has no value"))
        elif isinstance(value, bool) or not isinstance(value, (int, float, str)):
            found.append((param.name, f"must be a number or an expression; got {value!r}"))
        elif isinstance(value, str):
            continue
        elif not math.isfinite(value) or (
                param.min is not None and (value <= param.min if param.min_exclusive
                                           else value < param.min)) or (
                param.max is not None and value > param.max):
            found.append((param.name, f"must be {bound_text(param) or 'a finite number'}; "
                                      f"got {value:g}"))
    numbers = {k: v for k, v in values.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for smaller, larger in spec.order:
        if smaller in numbers and larger in numbers and numbers[smaller] > numbers[larger]:
            found.append((larger, f"must be at least {smaller} ({numbers[smaller]:g}); "
                                  f"got {numbers[larger]:g}"))
    return found
