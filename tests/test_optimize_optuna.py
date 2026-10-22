"""Optuna GP and NSGA-II samplers with KN confirmation (Phase 8, F1, D692).
Skipped when optuna is missing (it is in the optional `ml` extra)."""
from __future__ import annotations

import sys

import pytest

from nagasim import optimize
from test_optimize import KPI, QUEUE, _quadratic, _xy_factors

pytest.importorskip("optuna")

LAYOUT = {"schema_version": 9}


def _patch(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())


@pytest.mark.slow
def test_gp_finds_the_optimum_and_confirms_it_with_kn(monkeypatch):
    pytest.importorskip("torch")        # optuna's GPSampler needs it
    _patch(monkeypatch)
    result = optimize.optimize(LAYOUT, _xy_factors(), response=KPI, goal="min", method="optuna-gp",
                               budget=200, replications_per_point=4, seed=0)
    assert result.sampler == "GPSampler"
    assert abs(result.best["x"] - 3.0) < 1.5 and abs(result.best["y"] + 1.0) < 1.5
    assert result.budget_used <= 200
    confirmed = result.confirmed
    assert confirmed["best"] == result.best and 0 < confirmed["confidence"] < 1
    assert 1 < len(confirmed["candidates"]) <= 3        # confirm_top default
    assert result.pareto is None


def test_gp_without_torch_falls_back_to_multivariate_tpe_and_says_so(monkeypatch, caplog):
    _patch(monkeypatch)
    monkeypatch.setitem(sys.modules, "torch", None)     # import torch -> ImportError
    result = optimize.optimize(LAYOUT, _xy_factors(), response=KPI, goal="min", method="optuna-gp",
                               budget=60, replications_per_point=3, seed=0)
    assert result.sampler == "TPESampler"
    assert "needs torch" in caplog.text


def test_nsga2_returns_a_non_dominated_set_over_the_objectives(monkeypatch):
    # KPI wants x=3, QUEUE (= x) wants x small: every x in [-5, 3] at y=-1 is a trade-off.
    _patch(monkeypatch)
    result = optimize.optimize(LAYOUT, _xy_factors(), response=KPI, method="optuna-nsga2",
                               objectives=[(KPI, "min"), (QUEUE, "min")],
                               budget=300, replications_per_point=2, seed=0)
    front = result.pareto
    assert len(front) >= 3 and result.confirmed is None and result.sampler == "NSGAIISampler"
    rows = front[[KPI, QUEUE]].to_numpy()
    for a in rows:      # no member dominates another
        assert not any((b <= a).all() and (b < a).any() for b in rows)
    assert (front["x"] < 4.0).all() and (abs(front["y"] + 1.0) < 2.5).all()
    assert result.budget_used <= 300


def test_nsga2_needs_two_objectives(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(ValueError, match="objectives"):
        optimize.optimize(LAYOUT, _xy_factors(), response=KPI, method="optuna-nsga2",
                          budget=20, replications_per_point=2)
