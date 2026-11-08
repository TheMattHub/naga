"""E3 (D1135, D1136): optimisation objectives on the existing `optimize()` -- a percentile over
replications, a composite objective (KPI + weight x KPI2), a holdout seed set apart from the
tuning seeds, and the model's own values enqueued as trial 0.

Synthetic responses through the evaluation seam (`optimize.evaluation._evaluate_point`) with
known optima: the mean is best at x = 3, the 95th percentile at x = 0 (the spread grows with x),
and a penalty of 2 x max(0, 4 - x) moves the composite optimum to x = 4.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

import nagasim
from nagasim import experiments, optimize
from nagasim.optimize import Factor

KPI = "system/Avg Throughput Time"
PEN = "slot/Buffer/Overflows"
PATH = "simulation.time_limit"
LAYOUT = {"schema_version": 9, "simulation": {"time_limit": 5}}


def _fake(calls=None):
    def fake(layout, overrides, seeds, **kwargs):
        x = float(overrides[0]["value"])
        out = {KPI: {}, PEN: {}}
        for seed in seeds:
            if calls is not None:
                calls.append((x, seed))
            z = np.random.default_rng([11, seed]).normal()        # common random numbers
            out[KPI][seed] = (x - 3.0) ** 2 / 4.0 + 10.0 + (0.5 + x) * z
            out[PEN][seed] = max(0.0, 4.0 - x)
        return out
    return fake


def _factor():
    return [Factor(PATH, "integer", 0, 6, name="x")]


def _run(monkeypatch, calls=None, **kwargs):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _fake(calls))
    args = {"response": KPI, "goal": "min", "budget": 400, "replications_per_point": 20,
            "seed": 0, "method": "pattern", **kwargs}
    return optimize.optimize(LAYOUT, _factor(), **args)


def test_statistic_names():
    assert experiments.statistic("mean")([1.0, 3.0]) == 2.0
    assert experiments.statistic("P50")([1.0, 2.0, 9.0]) == 2.0
    for bad in ("p0", "p100", "max", "q95"):
        with pytest.raises(ValueError, match="statistic"):
            experiments.statistic(bad)


def test_a_percentile_objective_finds_the_tail_optimum_not_the_mean_one(monkeypatch):
    mean = _run(monkeypatch)
    assert mean.best["x"] == 3 and mean.statistic == "mean"
    tail = _run(monkeypatch, statistic="p95")
    assert tail.best["x"] == 0 and tail.statistic == "p95"
    assert "Objective" in tail.history.columns and "Objective" not in mean.history.columns
    # the final step tops up and ranks by the percentile, with no KN guarantee claimed
    assert tail.selection is not None and tail.selection.guaranteed is False
    assert tail.best_mean == pytest.approx(
        float(tail.selection.table.loc[tail.selection.table["Selected"], "Mean"].iloc[0]))


def test_a_composite_objective_adds_weight_times_the_penalty_kpi(monkeypatch):
    plain = _run(monkeypatch)
    composite = _run(monkeypatch, penalties=[(PEN, 2.0)])
    assert plain.best["x"] == 3 and composite.best["x"] == 4
    # KN ran on the per-replication composite and reports the response's name
    assert composite.selection.response == KPI and composite.selection.guaranteed in (True, False)
    with pytest.raises(ValueError, match="unknown response KPI"):
        _run(monkeypatch, penalties=[("system/Nope", 1.0)])


def test_baseline_is_trial_0_and_the_holdout_runs_on_unseen_seeds(monkeypatch):
    calls = []
    result = _run(monkeypatch, calls, baseline=True, holdout=30, statistic="p95")
    assert result.baseline == {"x": 5}
    assert result.history["Point"].iloc[0] == "x=5"
    tuned = {seed for _x, seed in calls if seed < 1_000_000}
    held = {seed for _x, seed in calls if seed >= 1_000_000}
    assert held == set(range(1_000_000, 1_000_030)) and not tuned & held
    assert result.budget_used <= 400        # the holdout is not charged to the budget
    table = result.holdout
    assert list(table["Role"])[:2] == ["baseline", "best"] and (table["N"] == 30).all()
    best = table[table["Role"] == "best"].iloc[0]
    assert best["Point"] == "x=0" and best["Diff vs baseline"] < 0 and best["Diff UB"] < 0
    assert np.isnan(table.loc[table["Role"] == "baseline", "Diff vs baseline"].iloc[0])


def test_a_mean_holdout_uses_the_paired_t_difference(monkeypatch):
    result = _run(monkeypatch, baseline=True, holdout=10, holdout_seed=500)
    best = result.holdout[result.holdout["Role"] == "best"].iloc[0]
    # x = 3 against x = 5: the mean differs by exactly 1 + 2 * mean(z) on the same seeds
    z = np.array([np.random.default_rng([11, s]).normal() for s in range(500, 510)])
    assert best["Diff vs baseline"] == pytest.approx(-1.0 - 2.0 * z.mean())


def test_a_baseline_outside_the_range_is_refused(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _fake())
    with pytest.raises(ValueError, match="outside the factor's range"):
        optimize.optimize({**LAYOUT, "simulation": {"time_limit": 9}}, _factor(), response=KPI,
                          budget=40, baseline=True)
    assert optimize.current_point(LAYOUT, _factor()) == {"x": 5}


@pytest.mark.skipif(importlib.util.find_spec("optuna") is None, reason="optuna not installed")
def test_optuna_enqueues_the_baseline_as_trial_0(monkeypatch):
    result = _run(monkeypatch, method="optuna", baseline=True, budget=200)
    assert result.history["Point"].iloc[0] == "x=5"


def test_a_real_model_with_every_option():
    """Bundled single-server model: P90 of the throughput time plus a queue penalty, baseline
    and holdout -- the options together on real runs."""
    path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    factor = Factor("blocks.process_1.params.dist_params.rate", "continuous", 1.1, 2.0, name="rate")
    result = optimize.optimize(str(path), [factor], response=KPI, statistic="p90",
                               penalties=[("system/Avg Queue Length", 0.5)], budget=16,
                               replications_per_point=2, run_length=200, warmup=20, seed=0,
                               baseline=False, holdout=3, select_top=2)
    assert 1.1 <= result.best["rate"] <= 2.0
    assert set(result.holdout["Role"]) <= {"best", "candidate"} and (result.holdout["N"] == 3).all()


def test_the_optimize_cli_takes_the_objective_flags(monkeypatch, tmp_path, capsys):
    import json

    from nagasim.optimize import cli

    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _fake())
    path = tmp_path / "m.json"
    path.write_text(json.dumps(LAYOUT), encoding="utf-8")
    assert cli.main([str(path), "--factor", f"{PATH}=integer:0:6", "--response", KPI,
                     "--replications", "20", "--budget", "400", "--method", "pattern",
                     "--statistic", "p95", "--penalty", f"{PEN}:0", "--baseline",
                     "--holdout", "5"], "optimize") == 0
    out = capsys.readouterr().out
    assert "objective (p95 of" in out and "Holdout (seeds the search never used):" in out
    assert "baseline" in out
