"""Input uncertainty (Phase 11 Task A, D719): a bootstrap of each input's data, refitted in
the family of its best fit, the model run per resample on common random numbers."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from nagasim import agent_tools as T
from nagasim import api, inputfit, uncertainty

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
TT = "system/Avg Throughput Time"
SERVICE = "blocks.process_1.params.distribution"
WINDOW = {"run_length": 400, "warmup": 40}


def _mm1() -> dict:
    return json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))


def _service_times(n: int, seed: int = 11) -> list[float]:
    # The model's own service: Exponential, rate 1.25 (mean 0.8).
    return np.random.default_rng(seed).exponential(0.8, n).tolist()


def _deterministic() -> dict:
    layout = _mm1()
    layout["blocks"][0]["params"] = {"distribution": "Constant", "dist_params": {"value": 3.0}}
    layout["blocks"][1]["params"]["distribution"] = "Constant"
    layout["blocks"][1]["params"]["dist_params"] = {"value": 2.0}
    return layout


@pytest.mark.slow
def test_less_data_gives_a_wider_interval_that_holds_the_true_parameter_run():
    layout = _mm1()
    true = api.run_model(layout, replications=3, seed=0, **WINDOW)
    true_mean = float(np.mean(true["system"].set_index("Metric").loc[
        "Avg Throughput Time", "Value"].astype(float)))
    answers = {n: uncertainty.input_uncertainty(
        layout, [{"path": SERVICE, "values": _service_times(n)}], kpis=[TT],
        resamples=12, replications=3, seed=0, **WINDOW) for n in (50, 5000)}
    small, large = (answers[n]["kpis"][0] for n in (50, 5000))
    width = {n: a["kpis"][0]["input_interval"][1] - a["kpis"][0]["input_interval"][0]
             for n, a in answers.items()}
    assert width[50] > width[5000]
    assert small["input_interval"][0] <= true_mean <= small["input_interval"][1]
    # The input interval always holds the run-to-run one; with 50 points the inputs dominate.
    for row in (small, large):
        assert row["input_interval"][0] <= row["run_ci"][0] <= row["run_ci"][1] <= row["input_interval"][1]
    assert small["input_share"] > 0.5
    assert answers[50]["resamples_used"] == small["resamples_used"] == 12
    assert answers[50]["runs"] == 13 * 3
    assert any("12 bootstrap resamples" in note for note in answers[50]["notes"])


def test_a_deterministic_model_keeps_a_zero_width_interval():
    answer = uncertainty.input_uncertainty(
        _deterministic(), [{"path": SERVICE, "values": [2] * 30}], kpis=[TT],
        resamples=4, replications=2, seed=0, run_length=60, warmup=0)
    row = answer["kpis"][0]
    assert row["run_ci"][0] == row["run_ci"][1] == row["mean"]
    assert row["input_interval"] == row["run_ci"]
    assert row["input_share"] == 0.0


def test_a_skewed_input_effect_is_not_given_a_negative_bound_it_never_showed():
    row = uncertainty.interval([1.0, 1.2, 1.1, 1.0, 1.2],
                               [[1.0, 1.1], [5.0, 5.2], [0.9, 1.0], [9.0, 9.1]])
    assert row["input_interval"][0] == 0.0 < row["run_ci"][0]
    assert row["input_interval"][1] > 9.0 and row["resamples_used"] == 4
    negative = uncertainty.interval([-1.0, 1.2], [[1.0, 1.1], [5.0, 5.2], [0.9, 1.0]])
    assert negative["input_interval"][0] < 0.0
    assert uncertainty.interval([1.0], [[1.0, 2.0]])["run_ci"] is None


def test_the_same_seed_gives_the_same_answer_and_engine_seeds_are_untouched():
    args = dict(kpis=[TT], resamples=3, replications=2, seed=5, run_length=150, warmup=15)
    inputs = [{"path": SERVICE, "values": _service_times(40)}]
    first = uncertainty.input_uncertainty(_mm1(), inputs, **args)
    assert first == uncertainty.input_uncertainty(_mm1(), inputs, **args)
    other = uncertainty.input_uncertainty(_mm1(), inputs, **{**args, "seed": 6})
    assert other["kpis"][0]["input_interval"] != first["kpis"][0]["input_interval"]
    # The run-to-run interval is exactly the point-fit model run on seeds seed + r.
    point = first["inputs"][0]["point"]
    layout = _mm1()
    layout["blocks"][1]["params"].update(point)
    runs = api.run_model(layout, replications=2, seed=5, run_length=150, warmup=15)
    values = runs["system"].set_index("Metric").loc["Avg Throughput Time", "Value"].astype(float)
    assert first["kpis"][0]["mean"] == pytest.approx(float(np.mean(values)), abs=1e-12)


def test_a_resample_that_cannot_be_fitted_is_counted_and_a_floor_holds(monkeypatch):
    real = inputfit.fit_candidates
    calls = {"n": 0}

    def every_third_fails(values, **kw):
        calls["n"] += 1
        return [] if calls["n"] % 3 == 0 else real(values, **kw)

    monkeypatch.setattr(inputfit, "fit_candidates", every_third_fails)
    # Call 1 is the point fit; calls 2..7 the six resamples, of which calls 3 and 6 fail.
    answer = uncertainty.input_uncertainty(
        _mm1(), [{"path": SERVICE, "values": _service_times(40)}], kpis=[TT],
        resamples=6, replications=2, seed=0, run_length=100, warmup=10)
    assert answer["resamples_used"] == 4 and answer["failed"] == {SERVICE: 2}
    assert answer["runs"] == 5 * 2

    monkeypatch.setattr(inputfit, "fit_candidates",
                        lambda values, **kw: [] if kw.get("candidates") else real(values, **kw))
    with pytest.raises(ValueError, match="0 of 6 resamples"):
        uncertainty.input_uncertainty(_mm1(), [{"path": SERVICE, "values": _service_times(40)}],
                                      resamples=6, replications=2, run_length=100)


def test_bad_requests_are_refused_before_any_run():
    inputs = [{"path": SERVICE, "values": _service_times(40)}]
    with pytest.raises(ValueError, match="runs"):
        uncertainty.input_uncertainty(_mm1(), inputs, resamples=400, replications=10)
    with pytest.raises(ValueError, match="replications"):
        uncertainty.input_uncertainty(_mm1(), inputs, replications=1)
    with pytest.raises(ValueError, match="distribution"):
        uncertainty.input_uncertainty(_mm1(), [{"path": "resources.Server.capacity",
                                                "values": [1, 2]}])
    with pytest.raises(ValueError, match="cycle"):
        uncertainty.input_uncertainty(_mm1(), [{"path": "schedules.day.points", "values": [1, 2]}])


def test_an_arrival_table_input_resamples_whole_cycles():
    layout = _mm1()
    layout["schedules"] = [{"name": "day", "kind": "arrival", "points": [[10, 1.0]], "repeat": True}]
    layout["blocks"][0]["params"] = {"arrival_mode": "Schedule", "schedule_name": "day"}
    rng = np.random.default_rng(3)
    stamps = np.sort(np.concatenate([c * 20 + rng.uniform(0, 20, rng.poisson(15))
                                     for c in range(12)]))
    answer = uncertainty.input_uncertainty(
        layout, [{"path": "schedules.day.points", "values": stamps.tolist(), "bin_width": 10,
                  "cycle": 20}], kpis=[TT], resamples=4, replications=2, seed=0,
        run_length=200, warmup=0)
    assert answer["inputs"][0]["kind"] == "arrival rates"
    assert answer["inputs"][0]["cycles"] == 12
    assert len(answer["inputs"][0]["point"]) == 2
    assert answer["resamples_used"] == 4


def test_cli_and_agent_tool(tmp_path, capsys):
    data = tmp_path / "service.csv"
    data.write_text("minutes\n" + "\n".join(f"{v:.6f}" for v in _service_times(40)) + "\n",
                    encoding="utf-8")
    shutil.copy(MODELS / "single_server.json", tmp_path / "single_server.json")
    model = str(tmp_path / "single_server.json")
    assert uncertainty.main([model, "--data", f"{SERVICE}={data}:minutes", "--kpi", TT,
                             "--resamples", "3", "--replications", "2",
                             "--run-length", "100"]) == 0
    text = capsys.readouterr().out
    assert "3 resamples" in text and TT in text and "inputs' share" in text

    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="single_server.json", name="m")
    out = T.call("input_uncertainty", model="m",
                 inputs=[{"path": SERVICE, "data": "service.csv", "column": "minutes"}],
                 kpis=[TT], resamples=3, replications=2, run_length=100)
    assert "error" not in out, out
    assert out["resamples_used"] == 3 and out["kpis"][0]["kpi"] == TT
    refused = T.call("input_uncertainty", model="m",
                     inputs=[{"path": SERVICE, "data": "../outside.csv"}])
    assert refused["error"]["code"] == "E_PATH_OUTSIDE_ROOT"


def test_cli_data_argument_keeps_a_windows_drive_letter():
    assert uncertainty._data_arg(r"a.b.distribution=C:\data\x.csv") == \
        ("a.b.distribution", r"C:\data\x.csv", None)
    assert uncertainty._data_arg(r"a.b.distribution=C:\data\x.csv:svc") == \
        ("a.b.distribution", r"C:\data\x.csv", "svc")
    assert uncertainty._data_arg("p=x.csv") == ("p", "x.csv", None)


# ---------------------------------------------------------------- a change's difference (C3, D737)
DELAY = "blocks.delay_1.params.dist_params.value"


def _with_delay() -> dict:
    """M/M/1 then a constant delay with no resource: a longer delay adds exactly its extra time to
    every entity's time in system, whatever the service draws. 40 arrivals, all done well inside
    the run, so the change moves no entity out of the window."""
    layout = _mm1()
    layout["blocks"][0]["params"]["max_arrivals"] = 40
    layout["blocks"].insert(2, {"name": "delay_1", "type": "DELAY", "position": [680.0, 320.0],
                                "params": {"distribution": "Constant", "dist_params": {"value": 1.0}}})
    layout["links"] = [{"source": "create_1", "target": "process_1"},
                       {"source": "process_1", "target": "delay_1"},
                       {"source": "delay_1", "target": "dispose_1"}]
    return layout


def test_a_constant_change_has_a_zero_width_difference_where_the_level_spreads():
    args = dict(kpis=[TT], resamples=4, replications=2, seed=3, run_length=150, warmup=0)
    inputs = [{"path": SERVICE, "values": _service_times(30)}]
    change = [{"path": DELAY, "value": 3.0}]
    level = uncertainty.input_uncertainty(_with_delay(), inputs, **args)
    diff = uncertainty.input_uncertainty(_with_delay(), inputs, overrides=change, **args)
    lrow, drow = level["kpis"][0], diff["kpis"][0]
    assert lrow["input_interval"][1] - lrow["input_interval"][0] > 0.1
    assert drow["mean"] == pytest.approx(2.0, abs=1e-9)
    assert drow["input_interval"][1] - drow["input_interval"][0] == pytest.approx(0.0, abs=1e-9)
    assert diff["measure"] == "difference" and level["measure"] == "level"
    assert diff["overrides"] == change and level["overrides"] is None
    # Baseline and changed model per resample: twice the runs, on the same draws of the data.
    assert diff["runs"] == 2 * level["runs"] and diff["inputs"] == level["inputs"]
    assert any("difference" in note for note in diff["notes"])
    assert diff == uncertainty.input_uncertainty(_with_delay(), inputs, overrides=change, **args)


def test_a_difference_keeps_its_negative_bound_and_bad_overrides_fail_first():
    # A level no run took below 0 is cut at 0; a difference is not (its sign is the question).
    point, resamples = [1.0, 1.2, 1.1, 1.0, 1.2], [[1.0, 1.1], [5.0, 5.2], [0.9, 1.0], [9.0, 9.1]]
    assert uncertainty.interval(point, resamples)["input_interval"][0] == 0.0
    assert uncertainty.interval(point, resamples, floor=False)["input_interval"][0] < 0.0
    from nagasim.core.schema import ModelValidationError
    inputs = [{"path": SERVICE, "values": _service_times(30)}]
    with pytest.raises(ModelValidationError):
        uncertainty.input_uncertainty(_mm1(), inputs, resamples=2, replications=2,
                                      overrides=[{"path": "resources.Nope.capacity", "value": 2}])
    with pytest.raises(ValueError, match="runs"):     # the changed model doubles the count
        uncertainty.input_uncertainty(_mm1(), inputs, resamples=199, replications=6,
                                      overrides=[{"path": "resources.Server.capacity", "value": 2}])
