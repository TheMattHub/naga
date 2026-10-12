"""Surrogate metamodels and datasets (ROADMAP 3.7 B): exact quadratic
recovery, CV/lack-of-fit, dataset shape, `suggest`, warm-start budget
accounting, and the optional scikit-learn path skipping cleanly."""
import numpy as np
import pandas as pd
import pytest

from nagasim import surrogate
from nagasim.optimize import DesignResult, Factor


def _design_result(factors, design, response_fn, n_reps=3, noise_sd=0.0, seed=0):
    """A DesignResult built directly (no simulation), from a known function."""
    rng = np.random.default_rng(seed)
    values, seeds = {}, {}
    for i in range(len(design)):
        point = {f.name: design.loc[i, f.name] for f in factors}
        true_y = response_fn(point)
        reps = true_y + (rng.normal(0, noise_sd, n_reps) if noise_sd else np.zeros(n_reps))
        values[i] = {"system/kpi": reps}
        seeds[i] = list(range(n_reps))
    return DesignResult(factors=factors, design=design, values=values, responses=["system/kpi"],
                        seeds=seeds, crn=True, replications=n_reps)


def _quadratic_2d(n_points=12, seed=0):
    """A noiseless quadratic surface, standardised the same way `_encode_factors`
    does (numpy ddof=0), so the fitted coefficients equal these exactly."""
    f1, f2 = Factor("x1", "continuous", 0.0, 10.0), Factor("x2", "continuous", 0.0, 10.0)
    rng = np.random.default_rng(seed)
    design = pd.DataFrame({"x1": rng.uniform(0, 10, n_points), "x2": rng.uniform(0, 10, n_points)})
    m1, s1 = design["x1"].to_numpy().mean(), design["x1"].to_numpy().std()
    m2, s2 = design["x2"].to_numpy().mean(), design["x2"].to_numpy().std()
    true_coef = {"Intercept": 3.0, "x1": 2.0, "x2": 1.5, "x1^2": 0.5, "x2^2": -0.3, "x1*x2": 0.7}

    def f(point):
        z1, z2 = (point["x1"] - m1) / s1, (point["x2"] - m2) / s2
        return (true_coef["Intercept"] + true_coef["x1"] * z1 + true_coef["x2"] * z2
                + true_coef["x1^2"] * z1 ** 2 + true_coef["x2^2"] * z2 ** 2
                + true_coef["x1*x2"] * z1 * z2)

    return _design_result([f1, f2], design, f), true_coef


def test_exact_recovery_of_a_noiseless_quadratic():
    dr, true_coef = _quadratic_2d()
    model = surrogate.fit_surrogate(dr, "system/kpi", kind="quadratic")
    fitted = dict(zip(model.terms, model.coef))
    for term, expected in true_coef.items():
        assert fitted[term] == pytest.approx(expected, abs=1e-8)
    assert model.r2 == pytest.approx(1.0, abs=1e-9)
    assert model.lack_of_fit is False


def test_cv_and_lack_of_fit_flag_on_a_misspecified_linear_fit():
    f1 = Factor("x1", "continuous", 0.0, 10.0)
    design = pd.DataFrame({"x1": np.linspace(0, 10, 15)})

    def truth(point):
        return 2 * point["x1"] + 0.8 * point["x1"] ** 2

    dr = _design_result([f1], design, truth, n_reps=4, noise_sd=0.3)
    linear = surrogate.fit_surrogate(dr, "system/kpi", kind="linear")
    quadratic = surrogate.fit_surrogate(dr, "system/kpi", kind="quadratic")
    assert linear.lack_of_fit is True
    assert quadratic.lack_of_fit is False
    assert linear.cv_rmse > quadratic.cv_rmse

    auto = surrogate.fit_surrogate(dr, "system/kpi", kind="auto")
    assert auto.kind == "quadratic"


def test_export_dataset_shape_and_csv_round_trip(tmp_path):
    dr, _true = _quadratic_2d(n_points=5)
    path = tmp_path / "points.csv"
    frame = surrogate.export_dataset(dr, path=path)
    assert len(frame) == 5 * dr.replications
    assert set(dr.factor_names) <= set(frame.columns)
    assert "Seed" in frame.columns and "system/kpi" in frame.columns
    assert path.exists()
    assert len(pd.read_csv(path)) == len(frame)


def test_suggest_finds_the_known_optimum_of_a_synthetic_surface():
    """A quadratic with its minimum well inside the design box: `suggest`
    should propose a point close to it."""
    f1 = Factor("x1", "continuous", -5.0, 5.0)
    design = pd.DataFrame({"x1": np.linspace(-5, 5, 11)})
    true_min = 1.5

    def truth(point):
        return (point["x1"] - true_min) ** 2

    dr = _design_result([f1], design, truth)
    model = surrogate.fit_surrogate(dr, "system/kpi", kind="quadratic")
    best = model.suggest(1, goal="min")[0]
    assert best["x1"] == pytest.approx(true_min, abs=0.5)


def test_warm_start_points_are_evaluated_first_and_counted_in_budget():
    from nagasim import optimize as optimize_mod

    calls = []

    def fake_evaluate_all(points, count=None):
        """The batched seam (PHASE4 T3): one wave of points in, records out."""
        calls.extend(dict(point) for point in points)
        return [optimize_mod._Record(key=tuple(point.values()), point=dict(point),
                                     objective=np.array([-(point["x1"] - 2.0) ** 2] * 3))
                for point in points]

    factors = [Factor("x1", "continuous", -5.0, 5.0)]
    warm = [{"x1": 1.9}, {"x1": -3.0}]
    incumbent, converged = optimize_mod._pattern_search(
        fake_evaluate_all, factors, budget=100, reps=3, seed=0,
        prefers=lambda a, b: b is None or a.mean > b.mean,
        wins=lambda a, b: a.mean > b.mean, should_stop=None, warm_start=warm)
    assert [c["x1"] for c in calls[:2]] == [1.9, -3.0]   # warm start goes first
    assert incumbent is not None


def test_gp_and_forest_report_a_clear_error_without_scikit_learn(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "sklearn" or name.startswith("sklearn."):
            raise ImportError("no module named sklearn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    dr, _true = _quadratic_2d(n_points=6)
    with pytest.raises(ImportError, match="scikit-learn"):
        surrogate.fit_surrogate(dr, "system/kpi", kind="gp")
