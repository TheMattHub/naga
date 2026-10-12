from __future__ import annotations

from nagasim import experiments
import math
import numpy as np
import pandas as pd
from .evaluation import _Evaluator
from .factors import overrides_for

# ---------------------------------------------------------------- Sobol indices
def _sobol_indices(y_a: np.ndarray, y_b: np.ndarray, y_ab: np.ndarray) -> tuple[float, float]:
    """(S1, ST) for one factor from its Saltelli triple (y_a, y_b, y_ab, each
    length N): S1 by Saltelli's (2010) estimator, ST by Jansen's (1999),
    both normalized by the pooled A/B sample variance. A row where any of the
    three is missing -- `should_stop` cut a wave short -- is dropped rather
    than propagating a NaN into the whole factor's estimate."""
    mask = np.isfinite(y_a) & np.isfinite(y_b) & np.isfinite(y_ab)
    y_a, y_b, y_ab = y_a[mask], y_b[mask], y_ab[mask]
    if y_a.size < 2:
        return math.nan, math.nan
    var = np.var(np.concatenate([y_a, y_b]))
    if not np.isfinite(var) or var <= 0:
        return 0.0, 0.0
    s1 = float(np.mean(y_b * (y_ab - y_a)) / var)
    st = float(0.5 * np.mean((y_a - y_ab) ** 2) / var)
    return s1, st


def sobol(layout, factors, kpi: str, n: int = 256, seed: int = 0, replications: int = 1,
         bootstrap: int = 100, *, run_length=None, warmup=None, workers: int = 1,
         on_progress=None, should_stop=None, pool=None, evaluator=None,
         record_logs=None) -> pd.DataFrame:
    """Sobol first- and total-order sensitivity indices (PHASE6 S7): how much
    of *kpi*'s variance each factor accounts for alone (S1) and together with
    every interaction it takes part in (ST).

    Saltelli's (2010) sampling design: A and B, two `n`-row halves of ONE
    scrambled-Sobol sequence over `2k` dimensions (`n` rounded up to a power
    of two, what `scipy.stats.qmc.Sobol` needs to stay balanced) -- drawing A
    and B from a single 2k-dimensional sequence, rather than two independent
    k-dimensional ones, is what keeps the pairing low-discrepancy, which the
    estimator below relies on -- plus one A_B^(i) per factor -- A with column
    i swapped for B's -- for N*(k+2) runs in all. CRN: every row shares the
    same *replications* seeds, so a row's value is the factor sweep, not
    sampling noise, the same discipline `sensitivity` and `tornado` use.
    Every point goes through the shared `_Evaluator` as ONE wave (PHASE4 T3),
    so it runs in parallel over the same pool `run_design`/`optimize` use
    rather than a loop of its own.

    S1 and ST come from `_sobol_indices`; *bootstrap* resamples of the N rows
    (0 -> no CI, the point estimate twice) give each index a 95% percentile
    CI. Returns one row per factor -- Factor, S1, S1 low, S1 high, ST, ST low,
    ST high -- sorted by ST descending, the order a sensitivity bar chart
    wants.
    """
    from scipy.stats import qmc

    factors = list(factors)
    k = len(factors)
    if k == 0:
        raise ValueError("sobol needs at least one factor")
    base = experiments._as_layout(layout)
    seeds = list(range(seed, seed + max(int(replications), 1)))
    exponent = max(int(np.ceil(np.log2(max(int(n), 2)))), 1)
    unit = qmc.Sobol(d=2 * k, scramble=True, seed=seed).random_base2(exponent)
    unit_a, unit_b = unit[:, :k], unit[:, k:]
    n = unit_a.shape[0]

    def point(unit_row: np.ndarray) -> dict:
        return {f.name: f.from_unit(unit_row[i]) for i, f in enumerate(factors)}

    requests = [(("A", j), overrides_for(factors, point(unit_a[j])), seeds) for j in range(n)]
    requests += [(("B", j), overrides_for(factors, point(unit_b[j])), seeds) for j in range(n)]
    for i in range(k):
        mixed = unit_a.copy()
        mixed[:, i] = unit_b[:, i]
        requests += [(("AB", i, j), overrides_for(factors, point(mixed[j])), seeds)
                    for j in range(n)]

    evaluator = evaluator or _Evaluator(base, run_length=run_length, warmup=warmup,
                                        workers=workers, pool=pool, record_logs=record_logs,
                                        kpis=[kpi])
    settled = evaluator.values_many(requests, on_progress=on_progress, should_stop=should_stop,
                                    total=len(requests))

    def mean_series(tag, i=None) -> np.ndarray:
        out = np.full(n, np.nan)
        for j in range(n):
            key = (tag, j) if i is None else (tag, i, j)
            arr = settled.get(key, {}).get(kpi)
            if arr is not None:
                out[j] = np.nanmean(arr)
        return out

    y_a, y_b = mean_series("A"), mean_series("B")
    if np.isnan(y_a).all() and np.isnan(y_b).all():
        raise ValueError(f"unknown response KPI: {kpi!r}")
    y_ab = [mean_series("AB", i) for i in range(k)]

    rng = np.random.default_rng(seed)
    n_boot = max(int(bootstrap), 0)
    rows = []
    for i, factor in enumerate(factors):
        s1, st = _sobol_indices(y_a, y_b, y_ab[i])
        if n_boot:
            s1_draws, st_draws = np.empty(n_boot), np.empty(n_boot)
            for b in range(n_boot):
                idx = rng.integers(0, n, size=n)
                s1_draws[b], st_draws[b] = _sobol_indices(y_a[idx], y_b[idx], y_ab[i][idx])
            s1_lo, s1_hi = np.nanpercentile(s1_draws, [2.5, 97.5])
            st_lo, st_hi = np.nanpercentile(st_draws, [2.5, 97.5])
        else:
            s1_lo, s1_hi, st_lo, st_hi = s1, s1, st, st
        rows.append({"Factor": factor.name, "S1": s1, "S1 low": s1_lo, "S1 high": s1_hi,
                     "ST": st, "ST low": st_lo, "ST high": st_hi})
    frame = pd.DataFrame(rows, columns=["Factor", "S1", "S1 low", "S1 high",
                                        "ST", "ST low", "ST high"])
    return frame.sort_values("ST", ascending=False, ignore_index=True)

