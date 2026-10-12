from __future__ import annotations

from nagasim import experiments
import numpy as np
import pandas as pd
from . import evaluation
from .factors import overrides_for

# ---------------------------------------------------------------- tornado (OAT) sensitivity
def tornado(layout, factors, kpi: str, seed: int = 0, replications: int = 1,
           rel_step: float | None = None, record_logs=None) -> pd.DataFrame:
    """One-at-a-time sensitivity (PHASE6 C6): from a base point (every
    factor at the middle of its own range), sweep each factor in turn to its
    low and high value with every OTHER factor held at the base -- so a
    row's swing is that factor's own effect, not an interaction. Every run,
    base and swept, shares the SAME seeds (CRN): the swing is the factor's
    effect, not sampling noise.

    *rel_step*, when given, narrows the sweep to *rel_step* of the factor's
    own range around the base point instead of its full low/high (useful
    when the declared range is wide but the question is "how sensitive is
    the model HERE").

    Returns a frame with one row per factor -- Factor, Low value, High
    value, KPI at low, KPI at high, Base KPI, Swing (|high - low|) -- sorted
    by Swing descending, the order a tornado chart's bars want.
    """
    factors = list(factors)
    base_layout = evaluation.study_layout(experiments._as_layout(layout), [kpi], record_logs)
    seeds = list(range(seed, seed + max(int(replications), 1)))
    point = {f.name: f.from_unit(0.5) for f in factors}

    def kpi_at(trial: dict) -> float:
        # Looked up on the module at call time: tests patch `evaluation._evaluate_point`.
        values = evaluation._evaluate_point(base_layout, overrides_for(factors, trial), seeds)
        by_seed = values.get(kpi)
        if by_seed is None:
            raise ValueError(f"unknown response KPI: {kpi!r}")
        return float(np.nanmean([by_seed.get(s, np.nan) for s in seeds]))

    base_kpi = kpi_at(point)
    rows = []
    for factor in factors:
        if rel_step:
            span = (factor.high - factor.low) * float(rel_step)
            lo_v = max(factor.low, point[factor.name] - span)
            hi_v = min(factor.high, point[factor.name] + span)
        else:
            lo_v, hi_v = factor.low, factor.high
        lo_kpi = kpi_at({**point, factor.name: factor.coerce(lo_v)})
        hi_kpi = kpi_at({**point, factor.name: factor.coerce(hi_v)})
        rows.append({"Factor": factor.name, "Low value": factor.coerce(lo_v),
                     "High value": factor.coerce(hi_v), "KPI at low": lo_kpi,
                     "KPI at high": hi_kpi, "Base KPI": base_kpi, "Swing": abs(hi_kpi - lo_kpi)})
    frame = pd.DataFrame(rows, columns=["Factor", "Low value", "High value", "KPI at low",
                                        "KPI at high", "Base KPI", "Swing"])
    return frame.sort_values("Swing", ascending=False, ignore_index=True)

