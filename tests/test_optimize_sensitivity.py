"""`optimize.sensitivity` (PHASE5 SD-4): a Latin-hypercube sweep over factors,
one CRN replication per point, drawn through the same `forecast.bands` a
Monte Carlo forecast uses.

The oracle: a pure exponential decay dL/dt = -k*L with k ~ U(a, b) sampled by
the sweep. L(t) is a STRICTLY DECREASING function of k at any fixed t > 0, so
the band's low edge (Q05 of L(t) across the n points) is L(t) evaluated at the
HIGH edge of k's own empirical distribution (Q95) -- and, under Euler, that is
the closed-form discrete factor `(1 - k*dt)**(t/dt)`, exact for a MATCHING
sample (`np.quantile`'s own interpolation is not exactly equivariant under a
nonlinear transform at a fractional rank, hence the loose-but-tight
tolerance -- see PHASE5_PLAN's SD-4 row)."""
import numpy as np
import pytest

from nagasim import optimize

K_LOW, K_HIGH = 0.01, 0.12
DT = 0.5
L0 = 1.0
HORIZON = 20.0


def _decay_layout():
    return {
        "levels": [{"name": "L", "initial": L0, "min": None}],
        "flows": [{"name": "decay", "rate": 'AUX("k")*LEVEL("L")', "from": "L", "biflow": True}],
        "auxiliaries": [{"name": "k", "expr": str((K_LOW + K_HIGH) / 2)}],
        "simulation": {"time_limit": HORIZON, "sd": {"dt": DT, "method": "euler"}},
    }


def _factors():
    return [optimize.Factor("auxiliaries.k.expr", "continuous", K_LOW, K_HIGH)]


def test_sensitivity_band_is_monotone_and_q05_matches_the_discrete_decay_factor_at_k95():
    n, seed = 60, 7
    result = optimize.sensitivity(_decay_layout(), _factors(), n=n, seed=seed)
    assert result.replications == n
    assert result.horizon == HORIZON
    assert result.quantiles == (0.05, 0.95)
    band = result.band("level/L")
    assert not band.empty
    assert (band["Q05"] <= band["Mean"] + 1e-9).all()
    assert (band["Mean"] <= band["Q95"] + 1e-9).all()
    # A pure decay: the band only ever narrows towards 0 as L drains.
    ordered = band.sort_values("Time")
    assert (ordered["Q95"].to_numpy()[1:] <= ordered["Q95"].to_numpy()[:-1] + 1e-9).all()

    ks = optimize.latin_hypercube(_factors(), n, seed)["auxiliaries.k.expr"].to_numpy(dtype=float)
    k95 = np.quantile(ks, 0.95)

    row = band[np.isclose(band["Time"], 10.0)]
    assert len(row) == 1
    expected = L0 * (1 - k95 * DT) ** (10.0 / DT)
    assert row["Q05"].iloc[0] == pytest.approx(expected, rel=2e-3)


def test_sensitivity_quantiles_edges_used_for_the_band():
    """The default `quantiles=(0.05, 0.5, 0.95)` uses its first and last entry
    as the band's edges (the middle one is the caller's own read of the
    design, not a third band column -- see `optimize.sensitivity`'s
    docstring)."""
    result = optimize.sensitivity(_decay_layout(), _factors(), n=10, seed=0,
                                  quantiles=(0.1, 0.5, 0.9))
    assert result.quantiles == (0.1, 0.9)
