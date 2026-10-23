"""Rejection ABC in `nagasim.calibrate` (Phase 11 F, D724): `method="abc"`.

The mechanics run on a synthetic response through the evaluator seam (the KPI is
the factor plus seeded noise, so the truth and the noise are both known); the
M/M/1 coverage check and a deterministic model run the real engine."""
import json
from pathlib import Path

import numpy as np
import pytest

import nagasim
from nagasim import api, calibrate as cal, experiments
from nagasim.optimize import Factor

KPI = "system/Avg Throughput Time"
TRUE_X = 3.0
X = Factor("simulation.time_limit", "continuous", 0.0, 10.0, name="x")
MODEL = Path(nagasim.__file__).parent / "models" / "single_server.json"
USAGE = "resource/Server/Usage % (snapshot)"
RATE = "blocks.process_1.params.dist_params.rate"


def _noisy(sd):
    """A fake `_evaluate_point`: the KPI is x + sd * z(seed), the objective the
    calibrate objective against an observed TRUE_X."""
    def fake(layout, overrides, seeds, **kwargs):
        x = float(next(o["value"] for o in overrides if o["path"] == X.paths[0]))
        sim = {s: x + sd * float(np.random.default_rng(s).standard_normal()) for s in seeds}
        return {KPI: sim,
                cal.OBJECTIVE_KPI: {s: ((v - TRUE_X) / TRUE_X) ** 2 for s, v in sim.items()}}
    return fake


def _abc(monkeypatch, sd=0.5, **kw):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _noisy(sd))
    kw.setdefault("budget", 402)
    kw.setdefault("replications_per_point", 2)
    kw.setdefault("method", "abc")
    return cal.calibrate({"schema_version": 9}, [{"kpi": KPI, "observed": TRUE_X}], [X], **kw)


def _width(result):
    row = result.posterior.iloc[0]
    return row["P95"] - row["P5"]


def test_the_posterior_covers_the_truth_and_reports_its_sample(monkeypatch):
    result = _abc(monkeypatch)
    assert result.method == "abc" and result.search is None
    row = result.posterior.iloc[0]
    assert list(result.posterior.columns) == cal.POSTERIOR_COLUMNS
    assert row["P5"] <= TRUE_X <= row["P95"] and row["P5"] <= row["Median"] <= row["P95"]
    assert (row["Low"], row["High"]) == (0.0, 10.0)
    # 400 prior samples, the nearest 5% kept, the best point confirmed on 2 more.
    assert len(result.samples) == 400 and result.accepted == 20
    assert int(result.samples["Accepted"].sum()) == 20
    assert result.budget_used == 402 and result.converged
    assert result.tolerance == pytest.approx(result.samples.loc[result.samples["Accepted"],
                                                                "Distance"].max())
    assert (result.samples.loc[~result.samples["Accepted"], "Distance"] >= result.tolerance).all()
    # Each sample on its own engine seed, not common random numbers.
    assert list(result.samples["Seed"]) == list(range(400))
    assert "20 of 400 prior samples accepted" in result.notes[0]
    assert len(result.table) == 1 and result.best["x"] == pytest.approx(TRUE_X, abs=0.5)


def test_more_data_narrows_the_posterior(monkeypatch):
    """Less run-to-run noise in the distance (more data per simulated dataset)."""
    wide, narrow = _abc(monkeypatch, sd=1.0), _abc(monkeypatch, sd=0.25)
    assert _width(narrow) < 0.5 * _width(wide)


def test_the_same_seed_gives_the_same_prior_sample_and_another_seed_does_not(monkeypatch):
    a, b, c = _abc(monkeypatch), _abc(monkeypatch), _abc(monkeypatch, seed=1)
    assert a.samples.equals(b.samples)
    assert not np.allclose(a.samples["x"], c.samples["x"])


def test_the_acceptance_count_is_at_least_twenty_and_says_so(monkeypatch):
    raised = _abc(monkeypatch, budget=102)          # 5% of 100 is 5: raised to 20
    assert raised.accepted == 20 and any("more than the 5.0% asked" in n for n in raised.notes)
    few = _abc(monkeypatch, budget=12)              # 10 samples: all of them, and said
    assert few.accepted == 10 and any("fewer than 20" in n for n in few.notes)
    share = _abc(monkeypatch, accept=0.1)
    assert share.accepted == 40


def test_a_stopped_run_keeps_the_samples_it_has_and_says_so(monkeypatch):
    calls = []
    result = _abc(monkeypatch, budget=102, should_stop=lambda: calls.append(1) or len(calls) > 50)
    assert 20 <= len(result.samples) < 100 and not result.converged
    assert any("stopped after" in n for n in result.notes)
    with pytest.raises(ValueError, match="stopped before"):
        _abc(monkeypatch, should_stop=lambda: True)


def test_integer_factors_stay_integer(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _noisy(0.5))
    result = cal.calibrate({"schema_version": 9}, [{"kpi": KPI, "observed": TRUE_X}],
                           [Factor(X.paths[0], "integer", 0, 10, name="x")], method="abc",
                           budget=102, replications_per_point=2)
    assert all(isinstance(v, int) for v in result.samples["x"].tolist())
    assert isinstance(result.best["x"], int)


def test_bad_arguments_are_refused(monkeypatch):
    with pytest.raises(ValueError, match="unknown calibration method"):
        _abc(monkeypatch, method="mcmc")
    with pytest.raises(ValueError, match="share"):
        _abc(monkeypatch, accept=0)
    with pytest.raises(ValueError, match="budget above"):
        _abc(monkeypatch, budget=2)
    with pytest.raises(ValueError, match="numeric factors"):
        cal.calibrate({"schema_version": 9}, [{"kpi": KPI, "observed": TRUE_X}],
                      [Factor("simulation.time_limit", "categorical", levels=[1, 2])],
                      method="abc", budget=30)


def test_the_default_method_has_no_abc_fields(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _noisy(0.0))
    result = cal.calibrate({"schema_version": 9}, [{"kpi": KPI, "observed": TRUE_X}], [X],
                           budget=60, replications_per_point=2)
    assert result.method == "optimize" and result.search is not None
    assert result.samples is None and result.posterior is None and result.accepted is None
    assert result.tolerance is None and result.notes == []


def test_a_deterministic_model_accepts_exactly():
    """D/D/1 at an arrival every 1 min: service rate 2 gives 50% usage exactly. An
    integer rate over [1, 3] puts exactly 20 of 40 hypercube samples on 2."""
    layout = json.loads(MODEL.read_text())
    layout["simulation"]["deterministic"] = True
    result = cal.calibrate(layout, [{"kpi": USAGE, "observed": 50.0}],
                           [Factor(RATE, "integer", 1, 3, name="rate")], method="abc",
                           budget=41, replications_per_point=1, run_length=20, warmup=0, seed=3)
    assert result.accepted == 20 and result.tolerance == 0.0
    kept = result.samples[result.samples["Accepted"]]
    assert set(kept["rate"]) == {2} and (kept["Distance"] == 0).all()
    assert any("tolerance 0" in n for n in result.notes)
    assert result.best == {"rate": 2} and result.objective == 0.0


def _observed_usage(run_length):
    frames = api.run_model(str(MODEL), replications=1, seed=1001, run_length=run_length, warmup=0)
    flat = experiments.flatten_kpis(frames)
    return float(flat[flat["KPI"] == USAGE]["Value"].iloc[0])


@pytest.mark.slow
def test_mm1_posterior_covers_the_known_service_rate_and_narrows_with_more_data():
    """M/M/1, arrivals at rate 1, service rate 1.25: usage observed on one run of
    the true model (its own seed), then ABC over [1.05, 2]. An 8 times longer
    observation period (more data) gives a narrower 90% interval: the width falls to
    0.63 of itself on observation seed 1001, 0.55 on 1002. Seed 1000 misses at the
    short run (P5 1.28 from an observed usage of 60%), as a 90% interval does one
    time in ten, so the test pins 1001."""
    factor = [Factor(RATE, "continuous", 1.05, 2.0, name="rate")]
    widths = []
    for run_length in (50, 400):
        result = cal.calibrate(str(MODEL), [{"kpi": USAGE, "observed": _observed_usage(run_length)}],
                               factor, method="abc", budget=102, replications_per_point=2,
                               run_length=run_length, warmup=0)
        row = result.posterior.iloc[0]
        assert row["P5"] <= 1.25 <= row["P95"], (run_length, row.to_dict())
        widths.append(row["P95"] - row["P5"])
    assert widths[1] < 0.75 * widths[0]


def test_the_agent_tool_and_the_cli_take_the_method(monkeypatch, tmp_path, capsys):
    from nagasim import agent_tools as T

    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _noisy(0.5))
    path = tmp_path / "m.json"
    path.write_text(MODEL.read_text(), encoding="utf-8")
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="m.json", name="m")
    out = T.call("calibrate", model="m", targets=[{"kpi": KPI, "observed": TRUE_X}],
                 factors=[{"path": X.paths[0], "low": 0, "high": 10}], budget=62,
                 replications_per_point=2, method="abc")
    assert "error" not in out, out
    assert out["method"] == "abc" and out["samples"] == 60 and out["accepted"] == 20
    assert out["abc_tolerance"] >= 0 and out["notes"]
    assert set(out["posterior"][0]) == {"factor", "median", "p5", "p95"}

    code = cal.main([str(path), "--factor", f"{X.paths[0]}=float:0:10", "--target", f"{KPI}={TRUE_X}",
                     "--budget", "42", "--replications", "2", "--method", "abc"])
    assert code in (0, 1)
    assert "P95" in capsys.readouterr().out
