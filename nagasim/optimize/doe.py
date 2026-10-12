from __future__ import annotations

from nagasim.core.stats import ci95
from dataclasses import dataclass
from nagasim import experiments
import numpy as np
import pandas as pd
from .evaluation import _Evaluator
from .factors import _point_key, _sort_key, overrides_for
from ._constants import _DEFAULT_RESPONSE_PREFIX

# ---------------------------------------------------------------- DOE
@dataclass
class DesignResult:
    """What `run_design` returns: the design, the per-replication response
    values of every point, and the effect analyses derived from them."""

    factors: list
    design: pd.DataFrame
    values: dict
    responses: list[str]
    seeds: dict
    crn: bool = True
    replications: int = 0
    completed: bool = True

    @property
    def factor_names(self) -> list[str]:
        return [f.name for f in self.factors]

    @property
    def table(self) -> pd.DataFrame:
        """Tidy frame: one row per (point, response) with the mean and its CI."""
        rows = []
        for point, per_response in self.values.items():
            coords = {f.name: self.design.loc[point, f.name] for f in self.factors}
            for response in self.responses:
                arr = per_response.get(response)
                stat = ci95(arr if arr is not None else [])
                rows.append({"Point": point, **coords, "Response": response,
                             "Mean": stat["mean"], "LB": stat["lb"], "UB": stat["ub"],
                             "N": 0 if arr is None else int(np.isfinite(arr).sum())})
        return pd.DataFrame(rows, columns=["Point", *self.factor_names,
                                           "Response", "Mean", "LB", "UB", "N"])

    def _by_level(self, factor, response: str) -> dict:
        """{level: (points x replications) array of the response at that level}."""
        out: dict = {}
        for point, per_response in self.values.items():
            arr = per_response.get(response)
            if arr is None:
                continue
            out.setdefault(self.design.loc[point, factor.name], []).append(arr)
        return {level: np.vstack(arrays) for level, arrays in out.items()}

    def main_effects(self, response: str) -> pd.DataFrame:
        """Mean *response* at each level of each factor, with a Student-t CI
        over every replication observed at that level.

        For a factor the design varies at exactly two levels the classical
        effect estimate (high minus low) comes with its own interval, computed
        on the PER-REPLICATION effects: with common random numbers replication
        r of every point ran on the same seed, so `mean(high, r) - mean(low, r)`
        is one observation of the effect and the shared noise cancels, exactly
        as in a paired scenario comparison.
        """
        rows = []
        for factor in self.factors:
            by_level = self._by_level(factor, response)
            levels = sorted(by_level, key=_sort_key)
            effect = {"mean": np.nan, "lb": np.nan, "ub": np.nan}
            if len(levels) == 2:
                low, high = by_level[levels[0]], by_level[levels[1]]
                n = min(low.shape[1], high.shape[1])
                effect = ci95(high[:, :n].mean(axis=0) - low[:, :n].mean(axis=0))
            for level in levels:
                flat = by_level[level].ravel()
                stat = ci95(flat)
                rows.append({"Factor": factor.name, "Level": level, "Mean": stat["mean"],
                             "LB": stat["lb"], "UB": stat["ub"],
                             "N": int(np.isfinite(flat).sum()),
                             "Effect": effect["mean"], "Effect LB": effect["lb"],
                             "Effect UB": effect["ub"]})
        return pd.DataFrame(rows, columns=["Factor", "Level", "Mean", "LB", "UB", "N",
                                           "Effect", "Effect LB", "Effect UB"])

    def interaction(self, response: str, a: str, b: str) -> pd.DataFrame:
        """Cell means of *response* for each (a, b) level pair.

        With both factors at exactly two levels the two-factor interaction
        ((hh - hl) - (lh - ll)) / 2 and its paired t-interval ride along in the
        frame's `.attrs` ("effect", "lb", "ub"); `attrs["effect"]` is NaN for
        any other design.
        """
        cells: dict = {}
        for point, per_response in self.values.items():
            arr = per_response.get(response)
            if arr is None:
                continue
            key = (self.design.loc[point, a], self.design.loc[point, b])
            cells.setdefault(key, []).append(arr)
        stacked = {key: np.vstack(arrays) for key, arrays in cells.items()}

        rows = []
        for key in sorted(stacked, key=lambda k: (_sort_key(k[0]), _sort_key(k[1]))):
            flat = stacked[key].ravel()
            stat = ci95(flat)
            rows.append({a: key[0], b: key[1], "Mean": stat["mean"], "LB": stat["lb"],
                         "UB": stat["ub"], "N": int(np.isfinite(flat).sum())})
        frame = pd.DataFrame(rows, columns=[a, b, "Mean", "LB", "UB", "N"])

        effect = {"mean": np.nan, "lb": np.nan, "ub": np.nan}
        a_levels = sorted({k[0] for k in stacked}, key=_sort_key)
        b_levels = sorted({k[1] for k in stacked}, key=_sort_key)
        if len(a_levels) == 2 and len(b_levels) == 2 and len(stacked) == 4:
            def cell(i, j):
                return stacked[(a_levels[i], b_levels[j])].mean(axis=0)
            effect = ci95(((cell(1, 1) - cell(1, 0)) - (cell(0, 1) - cell(0, 0))) / 2.0)
        frame.attrs.update(effect=effect["mean"], lb=effect["lb"], ub=effect["ub"])
        return frame


def run_design(layout, factors, design, *, responses=None, replications: int = 5, seed: int = 0,
               crn: bool = True, workers: int = 1, run_length=None, warmup=None,
               on_progress=None, should_stop=None, evaluator=None, pool=None,
               record_logs=None) -> DesignResult:
    """Run every point of *design* and collect *responses*.

    *design* is a frame from `full_factorial` / `latin_hypercube` /
    `one_at_a_time` (or any frame with one column per factor name).
    *responses* defaults to every `system/...` KPI key of the first point.

    With *crn* (the default) every point runs on the SAME seeds, so the effect
    estimates are paired and the shared sampling noise cancels; without it each
    point gets its own disjoint seed block.
    """
    base = experiments._as_layout(layout)
    factors = list(factors)
    points = design.reset_index(drop=True)
    evaluator = evaluator or _Evaluator(base, run_length=run_length, warmup=warmup,
                                        workers=workers, pool=pool, record_logs=record_logs,
                                        kpis=responses or ())
    total = len(points) * replications
    values: dict[int, dict] = {}
    seeds: dict[int, list[int]] = {}
    completed = True
    coerced = {f.name: [] for f in factors}

    # Every point of the design is submitted as one wave of (point, replication)
    # tasks (PHASE4 T3): the workers fill up even with ONE replication per point,
    # which is exactly the shape of a screening factorial.
    requests = []
    for index, row in points.iterrows():
        index = int(index)
        point = {f.name: f.coerce(row[f.name]) for f in factors}
        offset = seed if crn else seed + index * replications
        seeds[index] = [offset + r for r in range(replications)]
        requests.append((index, point, _point_key(factors, point),
                         overrides_for(factors, point)))

    if should_stop is not None and should_stop():
        completed = False
        requests = []
    settled = evaluator.values_many([(key, overrides, seeds[index])
                                     for index, _point, key, overrides in requests],
                                    on_progress=on_progress, should_stop=should_stop,
                                    total=total)
    for index, point, key, _overrides in requests:
        if key not in settled:
            completed = False           # out of budget, or stopped mid-wave
            break
        for name, value in point.items():
            coerced[name].append(value)
        values[index] = settled[key]
    seeds = {index: runs for index, runs in seeds.items() if index in values}

    if responses is None:
        first = next(iter(values.values()), {})
        responses = sorted(k for k in first if k.startswith(_DEFAULT_RESPONSE_PREFIX))
    # The design frame is rewritten with the COERCED values: a Latin hypercube
    # proposes 3.62 servers, the model ran 4, and the analysis must group by
    # what ran.
    applied = pd.DataFrame({name: column[:len(values)] for name, column in coerced.items()},
                           columns=[f.name for f in factors])
    return DesignResult(factors=factors, design=applied, values=values, responses=list(responses),
                        seeds=seeds, crn=crn, replications=replications, completed=completed)

