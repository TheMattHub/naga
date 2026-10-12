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

__all__ = ["DistParam", "DistributionSpec", "ScaleParam"]


@dataclass(frozen=True)
class DistParam:
    name: str
    default: object
    hint: str = ""        # reserved for a future per-field tooltip; unused by the UI today
    kind: str = "number"  # "number" (spin box, fx-toggle to an expression) | "list" (JSON field) | "expression"


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
