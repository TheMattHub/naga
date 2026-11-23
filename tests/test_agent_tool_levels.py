"""D1395, Task 4: run log levels through the agent tools and `naga run`.

The scripted check: a tool whose source (or a helper of its own module it calls) names a
level-gated results frame (`loglevels.FRAME_LEVEL`) declares it with `@tool(reads_frames=...)`,
and each declaring tool is shown to answer E_NOT_RECORDED on a run below that frame's level (a
tool reading the stored run) or to read a run recorded at that level (a tool making its own).
"""
from __future__ import annotations

import ast
import inspect
import json
import shutil
import sys
import textwrap
import typing
from pathlib import Path

import pytest

import nagasim
from nagasim import agent_tools as T
from nagasim import api, experiments
from nagasim.agent_tools import core, runs
from nagasim.core import loglevels

pytestmark = pytest.mark.usefixtures("answers_match_schemas")

SINGLE = Path(nagasim.__file__).parent / "models" / "single_server.json"
SHORT = {"run_length": 200, "replications": 2}
# The declaring tools, by how they meet a level: they read the run stored by run_model, or they
# make their own run (its level then has to cover what they declare). A new declaring tool goes
# in one of the two, with a test below.
READS_STORED_RUN = {"get_kpi_table"}
MAKES_OWN_RUN = {"forecast", "study_step", "calibrate"}


@pytest.fixture
def ws(tmp_path):
    shutil.copy(SINGLE, tmp_path / SINGLE.name)
    workspace = T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path=str(tmp_path / SINGLE.name), name="m")
    return workspace


# ---------------------------------------------------------------- the scripted check
def _frames_named(fn, seen) -> set[str]:
    """The FRAME_LEVEL frames *fn*'s body names as a string ("entity_log", "timeseries/..."),
    docstring and decorators left out, with the same-module helpers it calls by name."""
    fn = inspect.unwrap(fn)
    if fn in seen:
        return set()
    seen.add(fn)
    func = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    module = sys.modules[fn.__module__]
    found = set()
    for node in (n for stmt in body for n in ast.walk(stmt)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= {f for f in loglevels.FRAME_LEVEL
                      if node.value == f or node.value.startswith(f + "/")}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = getattr(module, node.func.id, None)
            if (inspect.isfunction(helper) and helper.__module__ == fn.__module__
                    and helper.__name__ not in core.TOOLS):     # a tool declares its own
                found |= _frames_named(helper, seen)
    return found


def test_a_tool_that_names_a_level_gated_frame_declares_it():
    undeclared = {}
    for name, spec in core.TOOLS.items():
        missing = _frames_named(spec.raw, set()) - set(spec.reads_frames)
        if missing:
            undeclared[name] = sorted(missing)
    assert not undeclared, f"add reads_frames=(...) to @tool: {undeclared}"


def test_declared_frames_are_level_gated_and_every_declaring_tool_is_tested():
    declaring = {name for name, spec in core.TOOLS.items() if spec.reads_frames}
    for name in declaring:
        assert set(core.TOOLS[name].reads_frames) <= set(loglevels.FRAME_LEVEL), name
    assert declaring == READS_STORED_RUN | MAKES_OWN_RUN
    # reads_frames is the test's, not the agent's: no schema carries it
    assert all("reads_frames" not in json.dumps(s) for s in T.tool_schemas())


@pytest.mark.parametrize("frame", sorted(loglevels.FRAME_LEVEL))
def test_get_kpi_table_refuses_a_frame_a_kpi_run_left_out(ws, frame):
    assert T.call("run_model", model="m", log_level="kpi", **SHORT)["log_level"] == "kpi"
    error = T.call("get_kpi_table", model="m", frame=frame)["error"]
    need = loglevels.FRAME_LEVEL[frame]
    assert error == {"code": "E_NOT_RECORDED", "message": loglevels.message(frame, "kpi", need),
                     "hint": f"re-run run_model with log_level='{need}'"}


def test_a_series_run_keeps_the_time_series_and_refuses_the_logs(ws):
    T.call("run_model", model="m", log_level="series", **SHORT)
    assert T.call("get_kpi_table", model="m", frame="timeseries")["total"] > 0
    assert T.call("get_kpi_table", model="m", frame="store_log")["error"]["code"] == "E_NOT_RECORDED"
    # a frame no level gates is still an unknown name
    assert T.call("get_kpi_table", model="m", frame="nosuch")["error"]["code"] == "E_UNKNOWN_FRAME"


def test_forecast_reads_a_run_that_records_its_series_and_then_says_the_log_is_absent(ws, monkeypatch):
    levels = []
    real = api.run_model

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        levels.append(out.log_level)
        return out
    monkeypatch.setattr(api, "run_model", spy)
    assert "error" not in T.call("forecast", model="m", horizon=100, replications=3, bind_data=False)
    need = max((loglevels.FRAME_LEVEL[f] for f in core.TOOLS["forecast"].reads_frames), key=loglevels.rank)
    assert levels and all(loglevels.rank(level) >= loglevels.rank(need) for level in levels)
    # the forecast's run is the stored one now: E_NOT_RECORDED, not E_UNKNOWN_FRAME
    error = T.call("get_kpi_table", model="m", frame="entity_log")["error"]
    assert error["code"] == "E_NOT_RECORDED" and "level series" in error["message"]
    assert error["hint"] == "re-run run_model with log_level='full'"


def test_calibrate_fits_a_series_target_on_runs_that_sample_it(ws, monkeypatch):
    levels = []
    real = api.run_model

    def spy(*args, **kwargs):
        out = real(*args, **kwargs)
        levels.append(out.log_level)
        return out
    monkeypatch.setattr(api, "run_model", spy)
    factor = {"path": "resources.Server.capacity", "kind": "integer", "low": 1, "high": 2}
    answer = T.call("calibrate", model="m", targets=[{"series": "system/WIP", "data": [[50, 1.0], [150, 1.0]]}],
                    factors=[factor], budget=4, replications_per_point=1)
    assert "error" not in answer, answer
    need = max((loglevels.FRAME_LEVEL[f] for f in core.TOOLS["calibrate"].reads_frames), key=loglevels.rank)
    assert levels and all(loglevels.rank(level) >= loglevels.rank(need) for level in levels)


def test_study_step_rebuilds_tail_waits_from_a_full_run(ws, monkeypatch):
    from nagasim.agent_tools import study
    levels = []
    real = study._tails

    def spy(s, digest, results, *args, **kwargs):
        levels.append(results.log_level)
        return real(s, digest, results, *args, **kwargs)
    monkeypatch.setattr(study, "_tails", spy)
    monkeypatch.setattr(study, "wait_shares", lambda *a, **k: None)     # plumbing only, as test_studytail
    goal = {"id": "g1", "text": "at most 5% wait over 15", "kpi": "Server/Time in Queue",
            "share_above": 15, "high": 0.05}
    sid = T.call("study_start", question="Is one server enough?", size="quick",
                 changes={"goals": [goal], "plan_approved": True})["study"]
    layout = json.loads((ws.root / SINGLE.name).read_text(encoding="utf-8"))
    T.call("study_step", study=sid, layout=layout, note="first model")
    need = max((loglevels.FRAME_LEVEL[f] for f in core.TOOLS["study_step"].reads_frames), key=loglevels.rank)
    assert levels and all(loglevels.rank(level) >= loglevels.rank(need) for level in levels)


# ---------------------------------------------------------------- run_model and the study tools
def test_run_model_tool_levels_are_the_engine_names():
    assert typing.get_args(runs.LogLevel) == loglevels.NAMES
    schema = core.TOOLS["run_model"].input_schema["properties"]["log_level"]
    assert schema.get("default") is None and set(schema["enum"]) - {None} == set(loglevels.NAMES)


def test_run_model_tool_answers_its_level(ws):
    assert T.call("run_model", model="m", **SHORT)["log_level"] == "full"      # explore by default
    answer = T.call("run_model", model="m", log_level="study", **SHORT)
    assert answer["log_level"] == "kpi"
    assert "entity_log" not in answer["frames"] and "system" in answer["frames"]
    assert "error" in T.call("run_model", model="m", log_level="everything", **SHORT)


def _set_simulation(ws, **settings):
    path = ws.root / SINGLE.name
    layout = json.loads(path.read_text(encoding="utf-8"))
    layout["simulation"].update(settings)
    path.write_text(json.dumps(layout), encoding="utf-8")
    assert "error" not in T.call("open_model", path=str(path), name="own")


def test_run_model_tool_default_leaves_the_model_files_own_setting(ws):
    _set_simulation(ws, record_logs=False)
    answer = T.call("run_model", model="own", **SHORT)
    assert answer["log_level"] == "series"          # the legacy switch, labelled, not overridden
    assert ws.results["own"]._not_recorded == {}    # no level named: today's frames
    assert T.call("run_model", model="own", log_level="full", **SHORT)["log_level"] == "full"


def test_run_model_tool_default_leaves_the_model_files_own_level(ws):
    _set_simulation(ws, log_level="kpi")
    assert T.call("run_model", model="own", **SHORT)["log_level"] == "kpi"
    assert T.call("run_model", model="own", log_level="series", **SHORT)["log_level"] == "series"


def test_a_study_tool_at_an_explicit_level_too_low_for_its_response_is_refused(ws):
    factor = {"path": "resources.Server.capacity", "kind": "integer", "low": 1, "high": 2}
    error = T.call("optimize", model="m", factors=[factor], response="resource/Server/Queue Len P95",
                   budget=10, replications_per_point=2, run_length=200, log_level="kpi")["error"]
    assert error == {"code": "E_NOT_RECORDED",
                     "message": loglevels.message("resource/Server/Queue Len P95", "kpi", "full"),
                     "hint": "call the tool again with log_level='full', or leave it out"}
    error = T.call("fit_surrogate", model="m", factors=[factor], response="timeseries/system/WIP/max",
                   replications=2, run_length=200, log_level="study")["error"]
    assert error["code"] == "E_NOT_RECORDED" and "log_level='series'" in error["hint"]


def test_a_not_recorded_error_from_the_engine_is_e_not_recorded(ws, monkeypatch):
    def raises(*args, **kwargs):
        raise loglevels.NotRecordedError.of("timeseries", "kpi", "series")
    monkeypatch.setattr(experiments, "suggest_warmup", raises)
    T.call("run_model", model="m", **SHORT)
    error = T.call("suggest_warmup", model="m", replications=2, run_length=200)["error"]
    assert error["code"] == "E_NOT_RECORDED" and error["hint"].startswith("run at log_level='series'")


@pytest.mark.parametrize("study", ["optimize", "run_design", "calibrate"])
def test_a_python_study_below_its_response_kpi_names_the_level(study):
    from nagasim import calibrate as cal
    from nagasim import optimize as opt
    factors = [opt.Factor(path="resources.Server.capacity", kind="integer", low=1, high=2)]
    key = "resource/Server/Queue Len P95"
    common = dict(run_length=200, log_level="kpi")
    calls = {
        "optimize": lambda: opt.optimize(str(SINGLE), factors, response=key, budget=6,
                                         replications_per_point=2, **common),
        "run_design": lambda: opt.run_design(str(SINGLE), factors, opt.build_design("full:2", factors, 0),
                                             responses=[key], replications=2, **common),
        "calibrate": lambda: cal.calibrate(str(SINGLE), [{"kpi": key, "observed": 1.0}], factors,
                                           budget=6, replications_per_point=2, **common)}
    with pytest.raises(loglevels.NotRecordedError, match="at level kpi; re-run with log_level='full'"):
        calls[study]()


def test_json_export_lists_the_frames_a_level_left_out(tmp_path):
    from nagasim.exporters import EXPORTER_REGISTRY
    write = EXPORTER_REGISTRY["json"].write
    kpi = api.run_model(str(SINGLE), log_level="kpi", **SHORT)
    doc = json.loads(write(kpi, tmp_path / "kpi.json").read_text())
    assert doc["not_recorded"] == {f: need for f, need in loglevels.FRAME_LEVEL.items()}
    full = api.run_model(str(SINGLE), **SHORT)
    assert "not_recorded" not in json.loads(write(full, tmp_path / "full.json").read_text())


def test_merged_replications_keep_their_level(ws):
    first = api.run_model(str(SINGLE), seed=0, log_level="kpi", **SHORT)
    more = api.run_model(str(SINGLE), seed=2, log_level="kpi", **SHORT)
    merged = experiments._merge(first, more)
    assert merged.log_level == "kpi" and merged.not_recorded("entity_log") is not None
    assert merged.live_at_end == {**first.live_at_end, **more.live_at_end}
    assert len(merged.live_at_end) == 4
    ws.results["m"] = merged
    assert T.call("get_kpi_table", model="m", frame="entity_log")["error"]["code"] == "E_NOT_RECORDED"


# ---------------------------------------------------------------- naga run --log-level
def _run(capsys, *argv):
    code = api.main([str(SINGLE), "--replications", "2", "--run-length", "200", *argv])
    return code, capsys.readouterr().out


def test_cli_no_logs_and_log_level_together_are_refused(capsys):
    code, out = _run(capsys, "--no-logs", "--log-level", "kpi")
    assert code == 2 and "give one of them" in out


def test_cli_refuses_an_unknown_level(capsys):
    with pytest.raises(SystemExit):
        _run(capsys, "--log-level", "everything")


@pytest.mark.parametrize("argv, level", [((), "full"), (("--no-logs",), "series"),
                                         (("--log-level", "series"), "series"),
                                         (("--log-level", "study"), "kpi")])
def test_cli_json_names_the_level(capsys, argv, level):
    code, out = _run(capsys, "--json", *argv)
    assert code == 0 and json.loads(out)["log_level"] == level


def test_cli_summary_names_a_level_below_full_last(capsys):
    _code, out = _run(capsys, "--log-level", "kpi")
    assert out.splitlines()[-1] == ("recorded at level kpi: the per-event logs and the time series "
                                    "left out (--log-level full records them)")
    _code, out = _run(capsys)
    assert "recorded at level" not in out
    _code, out = _run(capsys, "--log-level", "series", "--pace", "1000000000")
    assert out.splitlines()[-1] == ("recorded at level series: the per-event logs left out "
                                    "(--log-level full records them)")     # after the pace line
    assert any(line.startswith("paced at ") for line in out.splitlines())


def test_cli_legacy_no_logs_prints_todays_summary(capsys):
    """O4: a --no-logs run names no level, so its summary is the one it printed before levels."""
    _code, legacy = _run(capsys, "--no-logs")
    assert "recorded at level" not in legacy
    assert legacy.splitlines()[-1].startswith("replications for +/-5% on ")


@pytest.mark.parametrize("level, traced", [("replay", True), ("explore", False)])
def test_cli_replay_records_the_trace(monkeypatch, capsys, level, traced):
    seen = {}
    real = api.run_model

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(api, "run_model", spy)
    assert _run(capsys, "--log-level", level)[0] == 0
    assert (seen["trace"] is not None) is traced and seen["log_level"] == level


def test_a_study_tool_level_reaches_the_evaluator(ws, monkeypatch):
    from nagasim.optimize import evaluation
    asked = []
    real = evaluation.study_layout

    def spy(layout, kpis=(), record_logs=None, log_level=None):
        out = real(layout, kpis, record_logs, log_level)
        asked.append((log_level, out["simulation"]["log_level"]))
        return out
    monkeypatch.setattr(evaluation, "study_layout", spy)
    factor = {"path": "resources.Server.capacity", "kind": "integer", "low": 1, "high": 2}
    plain = dict(factors=[factor], run_length=200)
    T.call("fit_surrogate", model="m", response="system/Avg Throughput Time", replications=2,
           log_level="series", **plain)
    T.call("optimize", model="m", response="system/Avg Throughput Time", budget=4,
           replications_per_point=2, log_level="series", **plain)
    T.call("calibrate", model="m", targets=[{"kpi": "system/Avg Throughput Time", "observed": 5.0}],
           factors=[factor], budget=4, replications_per_point=1, log_level="series")
    T.call("optimize", model="m", response="system/Avg Throughput Time", budget=4,
           replications_per_point=2, **plain)
    assert asked[:3] == [("series", "series")] * 3
    assert asked[3] == (None, "kpi")        # no level asked: the lowest that records the response


def test_cli_no_logs_stays_the_legacy_switch(monkeypatch, capsys):
    seen = {}
    real = api.run_model

    def spy(*args, **kwargs):
        seen.update(kwargs)
        seen["results"] = real(*args, **kwargs)
        return seen["results"]
    monkeypatch.setattr(api, "run_model", spy)
    assert _run(capsys, "--no-logs")[0] == 0
    assert seen["record_logs"] is False and seen["log_level"] is None
    assert seen["results"]._not_recorded == {}      # today's frames, only labelled (O4)


def test_unrecorded_names_the_first_key_its_level_leaves_out():
    error = loglevels.unrecorded(["system/Avg Throughput Time", "timeseries/system/WIP/max",
                                  "resource/Server/Queue Len P95"], "study")
    assert (error.name, error.level, error.needed) == ("timeseries/system/WIP/max", "kpi", "series")
    assert isinstance(error, KeyError) and "re-run with log_level='series'" in str(error)
    assert loglevels.unrecorded(["resource/Server/Queue Len P95"], "explore") is None


# ---------------------------------------------------------------- a model that names its own level
def _own_kpi_model():
    layout = json.loads(SINGLE.read_text(encoding="utf-8"))
    layout["simulation"]["log_level"] = "kpi"
    return layout


LOG_KEY = "resource/Server/Queue Len P95"


@pytest.mark.parametrize("study", ["resolve_kpis", "paired_until_clear", "run_until_precision",
                                   "size_capacity"])
def test_a_study_on_a_model_at_its_own_kpi_level_names_the_level(study):
    layout = _own_kpi_model()
    calls = {
        "resolve_kpis": lambda: experiments.resolve_kpis(layout, [LOG_KEY], run_length=200),
        "paired_until_clear": lambda: experiments.paired_until_clear(
            layout, [{"path": "resources.Server.capacity", "value": 2}], [LOG_KEY], run_length=200),
        "run_until_precision": lambda: experiments.run_until_precision(
            layout, LOG_KEY, n0=2, max_replications=4, run_length=200),
        "size_capacity": lambda: experiments.size_capacity(
            layout, "resources.Server.capacity", kpi=LOG_KEY, replications=2, max_replications=4,
            stop=2, run_length=200)}
    with pytest.raises(loglevels.NotRecordedError, match="at level kpi; re-run with log_level='full'"):
        calls[study]()


def test_a_legacy_logs_off_model_still_gets_unknown_kpi():
    layout = json.loads(SINGLE.read_text(encoding="utf-8"))
    layout["simulation"]["record_logs"] = False
    with pytest.raises(experiments.UnknownKPIError):
        experiments.resolve_kpis(layout, [LOG_KEY], run_length=200)


def test_run_scenarios_tool_on_an_own_kpi_model_answers_e_not_recorded(ws):
    _set_simulation(ws, log_level="kpi")
    error = T.call("run_scenarios", model="own", kpis=[LOG_KEY], replications=2, run_length=200)["error"]
    assert error["code"] == "E_NOT_RECORDED" and "log_level='full'" in error["hint"]


def test_model_sets_own_is_the_one_rule():
    assert not loglevels.model_sets_own({"simulation": {"time_limit": 5}})
    assert loglevels.model_sets_own({"simulation": {"record_logs": True}})
    assert loglevels.model_sets_own({"simulation": {"log_level": "kpi"}})
    assert not loglevels.model_sets_own(None)


def test_a_mixed_sweep_is_labelled_by_its_lowest_level():
    layout = json.loads(SINGLE.read_text(encoding="utf-8"))
    layout["scenarios"] = [{"name": "quiet", "overrides": [{"path": "simulation.log_level", "value": "kpi"}]}]
    result = experiments.run_scenarios(layout, replications=1, run_length=200, warmup=0)
    assert {r.log_level for r in result.results.values()} == {"full", "kpi"}
    assert result.log_level == "kpi"
