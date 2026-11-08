from __future__ import annotations

from nagasim.core.schema import load_layout
import numpy as np
import pandas as pd
from . import evaluation
from .factors import overrides_for

COLUMNS = ["Factor", "Low value", "High value", "KPI at low", "KPI at high", "Base KPI", "Swing"]
SECONDARY_COLUMNS = ["Secondary at low", "Secondary at high", "Secondary base", "Secondary swing"]


def _label(overrides) -> str:
    return "; ".join(f"{o['path']}={o['value']}" for o in overrides)


# ---------------------------------------------------------------- tornado (OAT) sensitivity
def tornado(layout, factors, kpi: str, seed: int = 0, replications: int = 1,
           rel_step: float | None = None, record_logs=None, logic=None,
           controllers=None, *, scenarios=(), secondary: str | None = None) -> pd.DataFrame:
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

    *scenarios* (E5, D1138) adds named structural changes a numeric range cannot express (a
    rule switched, a block's distribution replaced, a capacity and its schedule moved
    together): ``{"name", "low": overrides, "high": overrides}`` with either side optional,
    or ``{"name", "overrides"}`` for a one-sided change (its high side). Each is applied on
    top of the base point; a missing side reads as the base KPI (a zero-length bar). Their
    Low value / High value columns name the overrides.

    *secondary* (E5) is a second KPI read from the same runs: four more columns (at low,
    at high, base, swing), and ties in Swing are ranked by its swing -- the ordering a
    question like "how many more vehicles, then how much longer" wants.

    Returns a frame with one row per factor and scenario -- Factor, Low value, High value,
    KPI at low, KPI at high, Base KPI, Swing (|high - low|) -- sorted by Swing descending,
    the order a tornado chart's bars want.

    *logic* and *controllers* are the caller's policies, as `api.run_model` takes them; the
    runs are serial, so a controller need not pickle.
    """
    factors = list(factors)
    scenarios = [_scenario(s) for s in scenarios]
    if not factors and not scenarios:
        raise ValueError("tornado needs at least one factor or scenario")
    keys = [kpi] + ([secondary] if secondary else [])
    base_layout = evaluation.study_layout(load_layout(layout), keys, record_logs)
    seeds = list(range(seed, seed + max(int(replications), 1)))
    # only what was given, so a test's stand-in for `_evaluate_point` need not take them
    policy = {k: v for k, v in (("logic", logic), ("controllers", controllers)) if v is not None}
    point = {f.name: f.from_unit(0.5) for f in factors}
    base_overrides = overrides_for(factors, point)

    def kpis_at(overrides) -> list[float]:
        # Looked up on the module at call time: tests patch `evaluation._evaluate_point`.
        values = evaluation._evaluate_point(base_layout, overrides, seeds, **policy)
        out = []
        for key in keys:
            by_seed = values.get(key)
            if by_seed is None:
                raise ValueError(f"unknown response KPI: {key!r}")
            out.append(float(np.nanmean([by_seed.get(s, np.nan) for s in seeds])))
        return out

    base = kpis_at(base_overrides)
    rows = []

    def add(name, lo_v, hi_v, lo, hi) -> None:
        row = {"Factor": name, "Low value": lo_v, "High value": hi_v, "KPI at low": lo[0],
               "KPI at high": hi[0], "Base KPI": base[0], "Swing": abs(hi[0] - lo[0])}
        if secondary:
            row.update({"Secondary at low": lo[1], "Secondary at high": hi[1],
                        "Secondary base": base[1], "Secondary swing": abs(hi[1] - lo[1])})
        rows.append(row)

    for factor in factors:
        if rel_step:
            span = (factor.high - factor.low) * float(rel_step)
            lo_v = max(factor.low, point[factor.name] - span)
            hi_v = min(factor.high, point[factor.name] + span)
        else:
            lo_v, hi_v = factor.low, factor.high
        add(factor.name, factor.coerce(lo_v), factor.coerce(hi_v),
            kpis_at(overrides_for(factors, {**point, factor.name: factor.coerce(lo_v)})),
            kpis_at(overrides_for(factors, {**point, factor.name: factor.coerce(hi_v)})))
    for name, low, high in scenarios:
        add(name, _label(low) if low else None, _label(high) if high else None,
            kpis_at(base_overrides + low) if low else base,
            kpis_at(base_overrides + high) if high else base)
    frame = pd.DataFrame(rows, columns=COLUMNS + (SECONDARY_COLUMNS if secondary else []))
    order = ["Swing", "Secondary swing"] if secondary else ["Swing"]
    return frame.sort_values(order, ascending=False, ignore_index=True)


def _scenario(entry) -> tuple[str, list, list]:
    """(name, low overrides, high overrides) of one named scenario."""
    if not isinstance(entry, dict) or not entry.get("name"):
        raise ValueError(f"a tornado scenario needs a name and overrides, not {entry!r}")
    low = list(entry.get("low") or [])
    high = list(entry.get("high") or entry.get("overrides") or [])
    if not low and not high:
        raise ValueError(f"tornado scenario {entry['name']!r} changes nothing")
    for o in low + high:
        if not isinstance(o, dict) or "path" not in o or "value" not in o:
            raise ValueError(f"tornado scenario {entry['name']!r}: an override is "
                             "{'path': ..., 'value': ...}")
    return str(entry["name"]), low, high
