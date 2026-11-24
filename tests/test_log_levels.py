"""Run log levels (D1395), Tasks 1-3 of docs/superpowers/plans/2026-11-23-run-log-levels.md: the
implementer's own tests beside the spec tests of `tests/test_log_levels_spec.py`.

The identity check over every bundled model is the slow one: at a named kpi or series level every
frame and column outside `FRAME_LEVEL` / `COLUMN_LEVEL` is bit-identical to the full run, and the
declared ones are absent (appendix A3 of the plan, now a test)."""
from pathlib import Path

import pandas as pd
import pytest

from nagasim import api, calibrate, experiments, forecast, loglevels, report
from nagasim.core.export import export_workbook
from nagasim.optimize import evaluation

MODELS = Path(api.__file__).parent / "models"
SINGLE = str(MODELS / "single_server.json")
SHORT = dict(seed=0, replications=2, run_length=300, warmup=30)


def _kpi_run():
    return api.run_model(SINGLE, log_level="kpi", **SHORT)


def _text(frame) -> list:
    return [[repr(v) for v in frame[c]] for c in frame.columns] + [list(frame.columns)]


@pytest.mark.slow
@pytest.mark.parametrize("seed, reps", [(0, 1), (7, 2)])
@pytest.mark.parametrize("path", sorted(MODELS.glob("*.json")), ids=lambda p: p.stem)
def test_every_bundled_model_is_identical_below_full_outside_the_declared_frames(path, seed, reps):
    # One case per model: as one loop it was a single ~500 s test that no xdist worker could share.
    differ = []
    full = api.run_model(str(path), seed=seed, replications=reps, keep_empty=True)
    for level in ("kpi", "series"):
        low = api.run_model(str(path), seed=seed, replications=reps, keep_empty=True,
                            log_level=level)
        gone = loglevels.not_recorded(level)
        for name, frame in full.items():
            if name in gone:
                assert name not in low, (path.stem, level, name)
                continue
            if not isinstance(frame, pd.DataFrame):
                continue
            kept = frame.drop(columns=[c for c in frame.columns if f"{name}.{c}" in gone])
            if _text(kept) != _text(low[name]):
                differ.append((level, name))
        # The live count stands for the live rows of the full log, replication by replication.
        log = full["entity_log"]
        live = log[log["Exited At"].isna() & log["In Batch"].isna()].groupby("Run").size()
        if (low.live_at_end != {run: int(live.get(run, 0)) for run in low.live_at_end}
                or sorted(low.live_at_end) != sorted(full["system"]["Run"].unique())):
            differ.append((level, "live_at_end"))
    assert differ == []


def test_a_series_run_of_a_model_that_samples_nothing_says_so():
    quiet = api.with_run_setting(api.load_layout(SINGLE), "timeseries_points", 0)
    results = api.run_model(quiet, log_level="series", **SHORT)
    assert results.log_level == "series"
    assert [p.code for p in results.run_problems] == ["W_SAMPLES_NOTHING"]
    # A full or kpi run of it, and a series run that samples, carry no such notice.
    assert not api.run_model(quiet, log_level="kpi", **SHORT).run_problems
    assert not api.run_model(SINGLE, log_level="series", **SHORT).run_problems


def test_a_level_reaches_pooled_replications():
    """The level travels with the layout, and the live count with each replication's KPIs."""
    serial = api.run_model(SINGLE, log_level="kpi", **SHORT)
    pooled = api.run_model(SINGLE, log_level="kpi", workers=2, **SHORT)
    assert pooled.log_level == "kpi" and "entity_log" not in pooled
    assert pooled.live_at_end == serial.live_at_end


def test_scenarios_take_a_level():
    layout = api.load_layout(SINGLE)
    study = experiments.run_scenarios(layout, replications=1, run_length=200, warmup=20,
                                      log_level="study")
    assert study.log_level == "kpi"
    assert all(r.log_level == "kpi" and "entity_log" not in r for r in study.results.values())
    assert experiments.run_scenarios(layout, replications=1, run_length=200, warmup=20).log_level == "full"


def test_needs_event_logs_is_needed_level_full():
    for kpis in ([], ["system/Avg Throughput Time"], ["timeseries/system/WIP/max"],
                 ["resource/Server/Queue Len P95"], ["entity_log/Cost"]):
        assert experiments.needs_event_logs(kpis) is (loglevels.needed_level(kpis) == "full")


def test_calibration_with_a_series_target_runs_with_the_sampler(monkeypatch):
    seen = []
    real = evaluation.study_layout

    def spy(layout, kpis=(), record_logs=None, log_level=None):
        out = real(layout, kpis, record_logs, log_level)
        seen.append(out["simulation"]["log_level"])
        return out
    monkeypatch.setattr(evaluation, "study_layout", spy)
    target = calibrate.SeriesTarget("system/WIP", [(100.0, 2.0), (200.0, 3.0)])
    with pytest.raises(Exception):      # only the level is asked for here, not a fit
        calibrate.calibrate(SINGLE, [target], [], budget=0)
    assert seen and seen[0] == "series"


# ---------- the readers that tell "not recorded" from "nothing happened" -----------------------
def test_mser_warmup_refuses_a_run_without_the_entity_log():
    with pytest.raises(loglevels.NotRecordedError, match="log_level='full'"):
        experiments.suggest_warmup(_kpi_run())


def test_series_readers_refuse_a_kpi_run():
    results = _kpi_run()
    with pytest.raises(loglevels.NotRecordedError, match="log_level='series'"):
        forecast.series_extreme_kpis(results)
    with pytest.raises(loglevels.NotRecordedError, match="level kpi"):
        calibrate.series_residuals(results, calibrate.SeriesTarget("system/WIP", [(1.0, 1.0)]))
    # A plain dict (frames without a level) keeps the old behaviour: no series, no rows.
    assert forecast.series_extreme_kpis(dict(results)).empty


def test_tail_goal_reason_names_the_level():
    from nagasim.agent_tools.study import _tails
    goal = {"id": "g1", "kpi": "Server/Time in Queue", "share_above": 5.0, "high": 0.1}
    why = _tails({"goals": [goal]}, {}, _kpi_run(), api.load_layout(SINGLE))
    assert len(why) == 1 and "level kpi" in why[0] and "log_level='full'" in why[0]


def test_report_says_what_was_not_recorded():
    html = report.build_report(api.load_layout(SINGLE), _kpi_run())
    assert "Not recorded at level kpi" in html and "entity_log" in html
    assert "Not recorded at level" not in report.build_report(api.load_layout(SINGLE),
                                                              api.run_model(SINGLE, **SHORT))


def test_a_forecast_report_does_not_ask_to_re_run():
    """A forecast takes no level, so its report says what it does not record, no 're-run'."""
    from nagasim import forecast
    layout = api.load_layout(SINGLE)
    result = forecast.forecast(layout, horizon=100, replications=2, seed=0)
    html = report.build_report(layout, result.results, forecast=result)
    assert "A forecast does not record entity_log." in html and "re-run" not in html


def test_workbook_leaves_out_what_was_not_recorded(tmp_path):
    from openpyxl import load_workbook
    path = export_workbook(_kpi_run(), tmp_path / "kpi.xlsx")
    sheets = load_workbook(path, read_only=True).sheetnames
    assert "Not recorded" in sheets
    assert not {"Entities", "Resources", "Stores", "Time Series"} & set(sheets)
    full = load_workbook(export_workbook(api.run_model(SINGLE, **SHORT), tmp_path / "full.xlsx"),
                         read_only=True).sheetnames
    assert "Not recorded" not in full and {"Entities", "Time Series"} <= set(full)


def test_plot_names_the_level_for_a_series_it_did_not_record():
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    with pytest.raises(KeyError, match="log_level='series'"):        # near "system/Avg WIP"
        _kpi_run().plot("system/WIP")
    with pytest.raises(loglevels.NotRecordedError, match="log_level='series'") as caught:
        _kpi_run().plot("level/Tank")
    assert (caught.value.name, caught.value.level, caught.value.needed) == ("level/Tank", "kpi", "series")
    with pytest.raises(loglevels.NotRecordedError, match="log_level='full'"):
        _kpi_run().plot("resource/Server/Queue Len P95")


def test_a_callers_record_logs_beats_the_models_level():
    """The caller is the more specific request, so its switch wins over a model's log_level (the
    level is dropped from the run's settings and the result is labelled by the switches)."""
    model = api.with_run_setting(api.load_layout(SINGLE), "log_level", "kpi")
    results = api.run_model(model, record_logs=True, **SHORT)
    assert results.log_level == "full" and "entity_log" in results
    assert evaluation.study_layout(model, record_logs=True)["simulation"].get("log_level") is None


def test_schema_validation_does_not_import_pandas():
    """Review I1: the level names reach the schema and the validator, never pandas with them."""
    import subprocess
    import sys
    code = ("import sys, nagasim.modelschema, nagasim.core.schema, nagasim.core.loglevels; "
            "print('pandas' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.parametrize("named_by", ["model", "caller"])
def test_kpi_stops_the_sampler_of_a_run_with_no_length(named_by):
    """Review I2: a run with no length samples on `timeseries_interval`; kpi stops that too."""
    from nagasim.core import sim_runner
    layout = api.load_layout(str(MODELS / "gse_electrification.json"))
    layout["simulation"] = {**layout["simulation"], "timeseries_interval": 60}
    layout["simulation"].pop("time_limit", None)
    if named_by == "model":
        layout = api.with_run_setting(layout, "log_level", "kpi")
    else:
        layout = loglevels.apply_level(layout, "kpi")
    env, _model = sim_runner.setup_run(layout, seed=0, run_len=None, warmup=0.0)
    assert env.ctx.sampler is None
    if named_by == "caller":     # a layout already at kpi, raised to series, still samples nothing
        assert loglevels.apply_level(layout, "series")["simulation"]["timeseries_interval"] is None


def test_a_misspelt_key_at_kpi_is_a_misspelling_not_a_missing_series():
    """Review M1, M2: plot() and interval() say "did you mean" for a typo and name the level for a
    KPI the run did not record."""
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    results = _kpi_run()
    with pytest.raises(KeyError, match="did you mean 'system/Avg Throughput Time'") as caught:
        results.plot("system/Avg Throughput Tme")
    assert not isinstance(caught.value, loglevels.NotRecordedError)
    assert "time series" in str(caught.value)       # and why a series would be missing too
    with pytest.raises(loglevels.NotRecordedError, match="log_level='full'"):
        results.interval("resource/Server/Queue Len P95")
    assert results.interval("system/Avg Throughput Time").n == 2


def test_a_game_result_keeps_its_law_checks():
    """A game's labelled result (api.session_results) carries its layout, so Problems still
    shows the run's law checks (final-review N1)."""
    import json
    from pathlib import Path
    import nagasim
    from nagasim import api
    from nagasim.core.session import SimSession
    layout = json.loads((Path(nagasim.__file__).parent / "models" / "single_server.json").read_text())
    layout["simulation"]["time_limit"] = 300
    results = api.session_results(SimSession(layout, seed=0).finish(), layout)
    assert results.checks().note != "no layout"
    assert results.checks().passed + len(results.checks().partial) > 0


def test_tornado_takes_a_series_extreme_kpi():
    """tornado on timeseries/<series>/max gets values, as optimize and run_design do (N2)."""
    import json
    from pathlib import Path
    import nagasim
    from nagasim import optimize
    layout = json.loads((Path(nagasim.__file__).parent / "models" / "single_server.json").read_text())
    layout["simulation"]["time_limit"] = 200
    factors = [optimize.Factor("resources.Server.capacity", "integer", 1, 3)]
    out = optimize.tornado(layout, factors, "timeseries/system/WIP/max", replications=2)
    assert out is not None
