"""Sensitivity on request (nagasim.studysensitivity, study_sensitivity)."""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
import nagasim.agent_tools.study_sensitivity  # noqa: F401  registers the tool
from nagasim import studysensitivity

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
KPI = "Avg Throughput Time"


def _goal(high):
    return {"id": "g1", "text": "time", "kpi": KPI, "high": high}


@pytest.fixture
def ws(tmp_path):
    shutil.copy(MODELS / "single_server.json", tmp_path / "m.json")
    return T.use_workspace(T.Workspace(tmp_path))


def _study(ws):
    sid = T.call("study_start", question="Is one server enough?")["study"]
    T.call("study_update", study=sid, changes={"goals": [_goal(1000)]})
    layout = json.loads((ws.root / "m.json").read_text(encoding="utf-8"))
    layout["simulation"]["time_limit"] = 300
    r = T.call("study_step", study=sid, layout=layout, note="first")
    assert r["kind"] == "version", r
    return sid


def test_factors_of_single_server():
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    assert [x["path"] for x in studysensitivity.factors(layout)] == [
        "blocks.create_1.params.dist_params.rate", "blocks.process_1.params.dist_params.rate",
        "resources.Server.capacity"]


def test_run_stores_swing_per_factor(ws):
    r = T.call("study_sensitivity", study=_study(ws))
    assert "error" not in r, r
    assert len(r["ranked"]) == 3 and r["changes_answer"] == []
    assert r["ranked"][0]["swing"] >= r["ranked"][-1]["swing"]
    stored = json.loads((ws.root / r["file"]).read_text(encoding="utf-8"))
    assert stored["charts"][KPI].startswith("<svg")


def test_a_goal_near_the_edge_is_flagged(ws):
    sid = _study(ws)
    base = T.call("study_status", study=sid)["last_version"]["kpis"][KPI]
    T.call("study_update", study=sid, changes={"goals": [_goal(base * 1.01)]})
    path = "blocks.create_1.params.dist_params.rate"
    r = T.call("study_sensitivity", study=sid, factors=[path], delta=0.5)
    assert r["changes_answer"] == [path]


def test_capacity_never_below_one(ws):
    r = T.call("study_sensitivity", study=_study(ws), factors=["resources.Server.capacity"])
    row = json.loads((ws.root / r["file"]).read_text(encoding="utf-8"))["factors"][0]
    assert (row["low"], row["high"]) == (1, 2)


def test_tail_goal_is_included(ws):
    sid = _study(ws)
    tail = {"id": "g2", "text": "few wait", "kpi": "Server/Time in Queue", "share_above": 1, "high": 0.9}
    T.call("study_update", study=sid, changes={"goals": [tail]})
    r = T.call("study_sensitivity", study=sid, factors=["resources.Server.capacity"])
    assert "error" not in r, r
    k = json.loads((ws.root / r["file"]).read_text(encoding="utf-8"))["factors"][0]["kpis"]
    assert k["Server/Share waiting over 1"]["low"]["mean"] is not None
