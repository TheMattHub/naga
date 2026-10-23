"""Phase 11 J: grounded combinations (D729-D731). C2 laws on a search's best point, C1
factors from an explain answer, C7 route rules from a log, C8 KN on a surrogate's
suggestions, C14 explain_kpi and what_if in the study room."""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T, laws

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
KPI = "system/Avg Throughput Time"
SERVICE = "blocks.process_1.params.dist_params.rate"


def _open_mm1(tmp_path, name="m"):
    """M/M/1 (arrival rate 1, service rate 1.25), short and with no warm-up, open as "m". A
    search over the service rate in [0.5, 0.8] has only unstable points: censored runs."""
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    layout["simulation"] = {"time_limit": 200, "warmup": 0, "replications": 2}
    (tmp_path / f"{name}.json").write_text(json.dumps(layout), encoding="utf-8")
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path=f"{name}.json", name=name)
    return layout


# ---------------------------------------------------------------- C2 (D729)
def test_optimize_answer_carries_the_best_points_laws_and_keeps_its_answer(tmp_path):
    from nagasim import optimize as opt
    layout = _open_mm1(tmp_path)
    factor = {"path": SERVICE, "kind": "continuous", "low": 0.5, "high": 0.8}
    out = T.call("optimize", model="m", factors=[factor], response=KPI, budget=12,
                 replications_per_point=2, seed=3)
    assert "error" not in out, out
    law = out["laws"]
    assert law["replications"] == 2 and law["seed"] == 3
    assert "not counted in the search's budget" in law["note"]
    assert any(c["name"] == "censoring" for c in law["partial"]), law
    # The law batch annotates: the same search without it picks and confirms the same point.
    bare = opt.optimize(layout, [opt.Factor(**factor)], response=KPI, budget=12,
                        replications_per_point=2, seed=3)
    assert out["best"] == bare.best and out["confirmed"] == bare.confirmed
    assert out["budget_used"] == bare.budget_used


def test_calibrate_and_forecast_answers_carry_laws(tmp_path):
    _open_mm1(tmp_path)
    cal = T.call("calibrate", model="m", targets=[{"kpi": KPI, "observed": 30.0}],
                 factors=[{"path": SERVICE, "kind": "continuous", "low": 0.5, "high": 0.8}],
                 budget=10, replications_per_point=2)
    assert "error" not in cal, cal
    assert cal["laws"]["replications"] == 2 and cal["laws"]["partial"]
    fc = T.call("forecast", model="m", horizon=100, replications=3)
    assert "error" not in fc, fc
    assert fc["laws"]["replications"] == 0 and "no extra run" in fc["laws"]["note"]
    assert "passed" in fc["laws"]


def test_at_point_says_when_the_budget_ran_out():
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    out = laws.at_point(layout, [], replications=2, run_length=50, should_stop=lambda: True)
    assert out["replications"] == 0 and out["partial"] == [] and "budget" in out["note"]
    stable = laws.at_point(layout, [{"path": SERVICE, "value": 2.0}], replications=2,
                           run_length=300, warmup=30)
    assert stable["replications"] == 2 and stable["passed"] > 0
    assert not any(c["name"] == "censoring" for c in stable["partial"])


# ---------------------------------------------------------------- C1 (D730)
def _two_resources():
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    layout["blocks"].insert(2, {"name": "pack", "type": "PROCESS", "params": {
        "distribution": "Exponential", "dist_params": {"rate": 3.0}, "resource_name": "Packer"}})
    layout["links"] = [{"source": "create_1", "target": "process_1"},
                       {"source": "process_1", "target": "pack"},
                       {"source": "pack", "target": "dispose_1"}]
    layout["resources"].append({"name": "Packer", "capacity": 2})
    return layout


def _rows(*pairs):
    return [{"row": name, "resource": None if name == "rest" else name, "mean": mean}
            for name, mean in pairs]


def test_factors_from_takes_the_capacities_that_hold_most_of_the_wait():
    from nagasim import explain
    layout = _two_resources()
    one = explain.factors_from(layout, {"mode": "level",
                                        "rows": _rows(("Server", 6.0), ("rest", 3.0), ("Packer", 0.2))})
    assert one["factors"] == [{"path": "resources.Server.capacity", "kind": "integer",
                               "low": 1, "high": 3}]
    assert "97%" in one["from"][0]
    both = explain.factors_from(layout, {"mode": "level",
                                         "rows": _rows(("Server", 5.0), ("Packer", 4.0))})
    assert [f["path"] for f in both["factors"]] == ["resources.Server.capacity",
                                                    "resources.Packer.capacity"]
    assert both["factors"][1]["low"] == 1 and both["factors"][1]["high"] == 4
    # A change answer is ranked by the size of the paired difference.
    change = explain.factors_from(layout, {"mode": "change", "rows": [
        {"row": "Packer", "resource": "Packer", "diff": -4.0},
        {"row": "Server", "resource": "Server", "diff": -0.1}]})
    assert [f["path"] for f in change["factors"]] == ["resources.Packer.capacity"]


def test_factors_from_adds_sensitivity_paths_that_change_the_answer_once():
    from nagasim import explain
    layout = _two_resources()
    rate = "blocks.create_1.params.dist_params.rate"
    # A study_sensitivity tool answer (ranked rows, no ends) and the file's own rows.
    tool_answer = {"ranked": [{"path": rate, "swing": 2.0}],
                   "changes_answer": [rate, "resources.Server.capacity"]}
    out = explain.factors_from(layout, {"rows": _rows(("Server", 5.0))}, tool_answer)
    assert out["factors"] == [
        {"path": "resources.Server.capacity", "kind": "integer", "low": 1, "high": 3},
        {"path": rate, "kind": "continuous", "low": 0.8, "high": 1.2}]
    from_file = {"changes_answer": [rate],
                 "factors": [{"path": rate, "value": 1.0, "low": 0.7, "high": 1.3}]}
    out = explain.factors_from(layout, {"rows": []}, from_file)
    assert out["factors"] == [{"path": rate, "kind": "continuous", "low": 0.7, "high": 1.3}]
    empty = explain.factors_from(layout, {"rows": _rows(("rest", 1.0))})
    assert empty["factors"] == [] and empty["notes"]


def test_explain_kpi_tool_answers_factors_optimize_takes(tmp_path):
    _open_mm1(tmp_path)
    out = T.call("explain_kpi", model="m", kpi=KPI, replications=2)
    assert "error" not in out, out
    assert out["factors"] == [{"path": "resources.Server.capacity", "kind": "integer",
                               "low": 1, "high": 3}] and out["factors_from"]
    search = T.call("optimize", model="m", factors=out["factors"], response=KPI, budget=8,
                    replications_per_point=2)
    assert "error" not in search, search


# ---------------------------------------------------------------- C7 (D731)
def _rework_log(path, cases=400):
    """Half the cases go A, B, A, C (one rework of A), half B, C: history matters, so a
    first-order (markov) draft can loop A-B-A-B-A, which the log never did."""
    rows, t0 = ["case,activity,timestamp"], 0.0
    for i in range(cases):
        steps = ["A", "B", "A", "C"] if i % 2 == 0 else ["B", "C"]
        for k, act in enumerate(steps):
            minute = t0 + i + 0.2 * (k + 1)
            rows.append(f"c{i},{act},2026-01-01T{int(minute // 60):02d}:{int(minute % 60):02d}:"
                        f"{int(round(minute * 60 % 60)):02d}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_discover_writes_route_rules_from_the_log_into_the_draft(tmp_path):
    from nagasim import discover, modelcheck
    from nagasim.core import schema
    log = _rework_log(tmp_path / "log.csv")
    layout, report = discover.discover(log, routing="variants")
    rules = {r["name"]: r for r in layout["path_checks"]}
    assert rules["log_always_C"]["rule"] == "always"
    assert rules["log_at_most_A"] == {"name": "log_at_most_A", "rule": "at_most", "block": "A",
                                      "count": 2, "description": rules["log_at_most_A"]["description"]}
    assert all(r["description"].startswith("from the log") for r in rules.values())
    # The last case may still be running at the log's end: left out of the routing, and so here.
    assert report["path_checks"]["rules"] == len(rules)
    assert report["path_checks"]["from"] in ("the log's 399 routed cases", "the log's 400 routed cases")
    assert any("route rules from the log" in s for s in report["next_steps"]["check"])
    assert not [p for p in schema.validate_model_structured(schema.migrate(layout))
                if p.severity == "error"]
    # The variants draft keeps every route rule; the first-order draft breaks the rework cap.
    assert all(r.passed for r in modelcheck.check(layout))
    markov, _ = discover.discover(log, routing="markov")
    verdict = {r.name: r.passed for r in modelcheck.check(markov)}
    assert verdict["log_at_most_A"] is False
    assert "route rules from the log" in discover.report_text(report)


# ---------------------------------------------------------------- C8
CAPACITY = {"path": "resources.Server.capacity", "kind": "integer", "low": 1, "high": 4}


def test_fit_surrogate_confirms_its_suggestions_with_kn_and_laws(tmp_path):
    _open_mm1(tmp_path)
    out = T.call("fit_surrogate", model="m", factors=[CAPACITY], response=KPI, design="full:4",
                 kind="quadratic", replications=2, run_length=300, confirm_top=3)
    assert "error" not in out, out
    assert out["lack_of_fit"] is False and out["confirm_refused"] is None
    confirmed = out["confirmed"]
    assert len({c["point"] for c in confirmed["candidates"]}) == 3       # #930: distinct
    assert confirmed["best"] in {c["point"] for c in confirmed["candidates"]}
    assert confirmed["overrides"][0]["path"] == "resources.Server.capacity"
    assert confirmed["runs"] >= 6 and out["laws"]["replications"] == 2
    bare = T.call("fit_surrogate", model="m", factors=[CAPACITY], response=KPI, design="full:2",
                  kind="linear", replications=2, run_length=100)
    assert "confirmed" not in bare and "laws" not in bare


def test_a_surface_with_lack_of_fit_is_not_confirmed(tmp_path, monkeypatch):
    from nagasim import surrogate
    _open_mm1(tmp_path)
    real = surrogate.fit_surrogate

    def misfit(*args, **kwargs):
        fit = real(*args, **kwargs)
        fit.lack_of_fit = True
        return fit

    monkeypatch.setattr(surrogate, "fit_surrogate", misfit)
    out = T.call("fit_surrogate", model="m", factors=[CAPACITY], response=KPI, design="full:2",
                 kind="linear", replications=2, run_length=100, confirm_top=3)
    assert "error" not in out, out
    assert out["confirmed"] is None and "lack of fit" in out["confirm_refused"]
    assert out["laws"] is None


# ---------------------------------------------------------------- C14
def test_the_room_offers_explain_kpi_and_what_if_on_the_last_version(tmp_path):
    import threading
    from nagasim import studyroom
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?", size="quick")["study"]
    adapter = studyroom.StudyAdapter(tmp_path, sid, threading.Lock())
    assert {"explain_kpi", "what_if", "study_step"} <= set(adapter.names)
    assert "write_model" not in adapter.names and "optimize" not in adapter.names
    adapter.offer(adapter.names)
    early = adapter.run_tool("explain_kpi", {"model": "anything", "kpi": KPI})
    assert early["error"]["code"] == "E_STUDY_NO_VERSION"
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    layout["simulation"] = {"time_limit": 100, "warmup": 0, "replications": 2}
    step = T.call("study_step", study=sid, layout=layout, note="first")
    assert "error" not in step, step
    out = adapter.run_tool("explain_kpi", {"model": "single_server", "kpi": KPI, "replications": 2,
                                           "study": "elsewhere"})
    assert "error" not in out, out
    assert out["model"] == f"study:{sid}" and out["rows"]
    change = adapter.run_tool("what_if", {"model": "x", "kpis": [KPI], "start": 2, "cap": 4,
                                         "overrides": [{"path": "resources.Server.capacity", "value": 2}]})
    assert "error" not in change, change
    assert change["model"] == f"study:{sid}"
    # Nothing was saved: the version on disk is unchanged.
    stored = json.loads((tmp_path / sid / "versions" / "v001" / "model.json").read_text(encoding="utf-8"))
    assert stored["resources"][0]["capacity"] == 1
