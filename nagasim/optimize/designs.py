from __future__ import annotations

import itertools
import pandas as pd

# ---------------------------------------------------------------- designs
def full_factorial(factors, levels=2) -> pd.DataFrame:
    """Every combination of every factor's levels.

    *levels* is a count for all factors or a {factor name: count} dict; a
    categorical factor always uses all of its own levels.
    """
    factors = list(factors)
    per = levels if isinstance(levels, dict) else {}
    default = 2 if isinstance(levels, dict) else int(levels)
    grids = [f.design_levels(int(per.get(f.name, default))) for f in factors]
    rows = list(itertools.product(*grids)) if grids else []
    return pd.DataFrame(rows, columns=[f.name for f in factors])


def latin_hypercube(factors, n: int, seed: int = 0) -> pd.DataFrame:
    """*n* points from a Latin hypercube (scipy's `qmc.LatinHypercube`).

    Stratification is the point: each factor's n values fall one per 1/n
    stratum of its range. Integer factors are rounded and categorical ones
    taken by index afterwards, which can of course collapse two strata onto the
    same level -- the property is exact only on the continuous factors.
    Reproducible for a given *seed*.
    """
    from scipy.stats import qmc

    factors = list(factors)
    n = max(int(n), 1)
    if not factors:
        return pd.DataFrame()
    unit = qmc.LatinHypercube(d=len(factors), seed=seed).random(n)
    data = {f.name: [f.from_unit(u) for u in unit[:, i]] for i, f in enumerate(factors)}
    return pd.DataFrame(data, columns=[f.name for f in factors])


def one_at_a_time(factors, baseline: dict | None = None) -> pd.DataFrame:
    """The baseline point, then one point per factor level with every other
    factor held at its baseline value. Missing baseline values default to the
    middle of the factor's range."""
    factors = list(factors)
    baseline = dict(baseline or {})
    base = {f.name: f.coerce(baseline[f.name]) if f.name in baseline else f.from_unit(0.5)
            for f in factors}
    rows = [dict(base)]
    for factor in factors:
        for level in factor.design_levels(2):
            if level == base[factor.name]:
                continue
            rows.append({**base, factor.name: level})
    return pd.DataFrame(rows, columns=[f.name for f in factors])

