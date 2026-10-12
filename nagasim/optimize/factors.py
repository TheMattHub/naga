from __future__ import annotations

from dataclasses import dataclass
import numpy as np
from ._constants import KINDS, _KIND_ALIASES

# ---------------------------------------------------------------- factors
@dataclass
class Factor:
    """One experimental factor: a model parameter and the range it varies over.

    *path* is one override path or a list of them -- several paths driven with
    the SAME value, for a change that is only consistent when applied in more
    than one place (a resource capacity and the schedule that staffs it).

    *kind* is "continuous", "integer" or "categorical"; a categorical factor
    takes its values from *levels* and ignores low/high. *step*, on a numeric
    factor, snaps every value to `low + k * step`.
    """

    path: str | list[str]
    kind: str = "continuous"
    low: float = 0.0
    high: float = 1.0
    levels: list | None = None
    step: float | None = None
    name: str = ""

    def __post_init__(self) -> None:
        self.paths = [self.path] if isinstance(self.path, str) else list(self.path)
        if not self.paths or not all(self.paths):
            raise ValueError("a factor needs at least one override path")
        self.name = self.name or self.paths[0]
        self.kind = _KIND_ALIASES.get(self.kind, self.kind)
        if self.kind not in KINDS:
            raise ValueError(f"unknown factor kind: {self.kind!r} (expected one of {KINDS})")
        if self.kind == "categorical":
            if not self.levels:
                raise ValueError(f"factor '{self.name}': a categorical factor needs levels")
            self.levels = list(self.levels)
            self.low, self.high = 0.0, float(len(self.levels) - 1)
        else:
            self.low, self.high = float(self.low), float(self.high)
            if self.high < self.low:
                raise ValueError(f"factor '{self.name}': high {self.high} is below low {self.low}")

    # -- value handling ---------------------------------------------------
    def coerce(self, value):
        """The value as this factor can actually set it: snapped to the step,
        rounded for an integer factor, clipped into [low, high]."""
        if self.kind == "categorical":
            if value in self.levels:
                return value
            index = int(round(float(value)))
            return self.levels[min(max(index, 0), len(self.levels) - 1)]
        x = float(value)
        if self.step:
            x = self.low + round((x - self.low) / float(self.step)) * float(self.step)
        x = min(max(x, self.low), self.high)
        return int(round(x)) if self.kind == "integer" else float(x)

    def to_unit(self, value) -> float:
        if self.kind == "categorical":
            return self.levels.index(value) / max(len(self.levels) - 1, 1)
        span = self.high - self.low
        return 0.0 if span <= 0 else (float(value) - self.low) / span

    def from_unit(self, u: float):
        u = min(max(float(u), 0.0), 1.0)
        if self.kind == "categorical":
            return self.levels[min(int(u * len(self.levels)), len(self.levels) - 1)]
        return self.coerce(self.low + u * (self.high - self.low))

    def design_levels(self, count: int = 2) -> list:
        """*count* evenly spaced levels (all of them for a categorical factor),
        coerced and de-duplicated -- an integer factor over [3, 4] has two
        levels however many were asked for."""
        if self.kind == "categorical":
            return list(self.levels)
        out: list = []
        for value in np.linspace(self.low, self.high, max(int(count), 2)):
            coerced = self.coerce(value)
            if coerced not in out:
                out.append(coerced)
        return out

    def overrides(self, value) -> list[dict]:
        return [{"path": p, "value": self.coerce(value)} for p in self.paths]


def overrides_for(factors, point: dict) -> list[dict]:
    """The scenario overrides one design point stands for."""
    out: list[dict] = []
    for factor in factors:
        if factor.name in point:
            out.extend(factor.overrides(point[factor.name]))
    return out


def _point_key(factors, point: dict) -> tuple:
    """The cache key of a design point: its COERCED values, so two continuous
    proposals that round to the same integer point are the same point."""
    return tuple(f.coerce(point[f.name]) for f in factors)


def _sort_key(value):
    return (1, str(value)) if isinstance(value, str) else (0, float(value))

