"""`optimize.sobol` (PHASE6 S7): Saltelli sampling (A, B, A_B^(i)) with a
Jansen total-order / Saltelli first-order estimator, in numpy -- SALib is not
a dependency (CLAUDE.md).

Two oracles, both run through the module's one evaluation seam
(`optimize.evaluation._evaluate_point`, monkeypatched -- the same seam `test_optimize.py`
uses for its own synthetic responses) so the estimator is checked against a
closed-form answer at Sobol-sequence speed, no simulation involved:

* the Ishigami function (a=7, b=0.1), whose S1/ST are textbook constants;
* a linear model, where S1 == ST == each term's variance share exactly, in
  the large-sample limit.

A last, real test runs one tiny deterministic block model end to end, to
catch an integration bug the synthetic oracles cannot (a bad override path,
a KPI key that does not flatten the way `sobol()` expects).
"""
import numpy as np
import pandas as pd
import pytest

from nagasim import optimize
from nagasim.optimize import Factor

from test_experiments import deterministic_layout

KPI = "kpi"


# ---------------------------------------------------------------- Ishigami oracle
A, B = 7.0, 0.1


def _ishigami(x1, x2, x3):
    return np.sin(x1) + A * np.sin(x2) ** 2 + B * (x3 ** 4) * np.sin(x1)


def _ishigami_factors():
    return [Factor(f"x{i}", "continuous", -np.pi, np.pi) for i in (1, 2, 3)]


def _fake_from(fn):
    """A fake `_evaluate_point`: the point IS the model, no simulation -- the
    overrides carry x1..x3 (or a1..an), *fn* turns them into the KPI."""
    def fake(layout, overrides, seeds, **kwargs):
        values = {o["path"]: o["value"] for o in overrides}
        y = fn(values)
        return {KPI: {s: y for s in seeds}}
    return fake


def test_sobol_matches_ishigami_analytical_indices(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _fake_from(lambda v: _ishigami(v["x1"], v["x2"], v["x3"])))
    result = optimize.sobol({}, _ishigami_factors(), KPI, n=8192, seed=0, bootstrap=0)

    s1 = dict(zip(result["Factor"], result["S1"]))
    st = dict(zip(result["Factor"], result["ST"]))
    assert s1["x1"] == pytest.approx(0.314, abs=0.05)
    assert s1["x2"] == pytest.approx(0.442, abs=0.05)
    assert s1["x3"] == pytest.approx(0.0, abs=0.05)
    assert st["x1"] == pytest.approx(0.558, abs=0.07)
    assert st["x2"] == pytest.approx(0.442, abs=0.05)
    assert st["x3"] == pytest.approx(0.244, abs=0.07)


# ---------------------------------------------------------------- linear-model oracle
def test_sobol_linear_model_s1_equals_st_equals_variance_share(monkeypatch):
    """Y = sum(c_i * x_i), x_i ~ U(0, 1): purely additive, so S1 == ST for
    every factor (no interaction to add), each equal to that term's own
    variance share c_i^2 / sum(c_j^2) (Var(x_i) is the same 1/12 for every
    factor and cancels)."""
    coeffs = {"a1": 1.0, "a2": 2.0, "a3": 3.0}
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _fake_from(lambda v: sum(coeffs[name] * value
                                                 for name, value in v.items())))
    factors = [Factor(name, "continuous", 0.0, 1.0) for name in coeffs]
    result = optimize.sobol({}, factors, KPI, n=4096, seed=1, bootstrap=0)

    total_sq = sum(c ** 2 for c in coeffs.values())
    expected = {name: c ** 2 / total_sq for name, c in coeffs.items()}
    for _, row in result.iterrows():
        share = expected[row["Factor"]]
        assert row["S1"] == pytest.approx(share, abs=0.02)
        assert row["ST"] == pytest.approx(share, abs=0.02)
        assert row["S1"] == pytest.approx(row["ST"], abs=0.02)


def test_sobol_sorted_by_total_order_descending(monkeypatch):
    coeffs = {"a1": 1.0, "a2": 3.0, "a3": 2.0}
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _fake_from(lambda v: sum(coeffs[name] * value
                                                 for name, value in v.items())))
    factors = [Factor(name, "continuous", 0.0, 1.0) for name in coeffs]
    result = optimize.sobol({}, factors, KPI, n=1024, seed=3, bootstrap=0)

    assert list(result["Factor"]) == ["a2", "a3", "a1"]      # biggest coefficient first
    assert (result["ST"].to_numpy()[:-1] >= result["ST"].to_numpy()[1:]).all()


def test_sobol_bootstrap_ci_brackets_the_point_estimate(monkeypatch):
    coeffs = {"a1": 1.0, "a2": 3.0}
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _fake_from(lambda v: sum(coeffs[name] * value
                                                 for name, value in v.items())))
    factors = [Factor(name, "continuous", 0.0, 1.0) for name in coeffs]
    result = optimize.sobol({}, factors, KPI, n=512, seed=2, bootstrap=50)

    assert (result["S1 low"] <= result["S1"] + 1e-9).all()
    assert (result["S1"] <= result["S1 high"] + 1e-9).all()
    assert (result["ST low"] <= result["ST"] + 1e-9).all()
    assert (result["ST"] <= result["ST high"] + 1e-9).all()
    # a2's coefficient is bigger: it explains more of the variance
    by_factor = result.set_index("Factor")
    assert by_factor.loc["a2", "ST"] > by_factor.loc["a1", "ST"]


def test_sobol_is_seeded_reproducible(monkeypatch):
    coeffs = {"a1": 1.0, "a2": 2.0}
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _fake_from(lambda v: sum(coeffs[name] * value
                                                 for name, value in v.items())))
    factors = [Factor(name, "continuous", 0.0, 1.0) for name in coeffs]
    first = optimize.sobol({}, factors, KPI, n=128, seed=5, bootstrap=20)
    second = optimize.sobol({}, factors, KPI, n=128, seed=5, bootstrap=20)
    pd.testing.assert_frame_equal(first, second)


# ---------------------------------------------------------------- end-to-end (real engine)
def test_sobol_runs_end_to_end_on_a_tiny_deterministic_model():
    """No monkeypatch: the real evaluator, schema validation and KPI
    flattening, on a model small enough to stay well under budget."""
    layout = deterministic_layout(max_arrivals=5)
    factors = [Factor("blocks.delay_1.params.dist_params.value", "continuous", 1.0, 3.0)]
    result = optimize.sobol(layout, factors, "system/Avg Throughput Time", n=4, seed=0,
                            bootstrap=0, run_length=50)

    assert list(result.columns) == ["Factor", "S1", "S1 low", "S1 high",
                                    "ST", "ST low", "ST high"]
    assert len(result) == 1
    assert np.isfinite(result["S1"].iloc[0]) and np.isfinite(result["ST"].iloc[0])
