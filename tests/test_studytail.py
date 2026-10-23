"""Tail goals (share of waits above x) and study_validate(kind="queue_formula") on the study tools."""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
TAIL = {"id": "g1", "text": "at most 5% wait over 15", "kpi": "Server/Time in Queue", "share_above": 15, "high": 0.05}
NAME = "Server/Share waiting over 15"


@pytest.fixture
def ws(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    return T.use_workspace(T.Workspace(tmp_path))


def layout(ws):
    return json.loads((ws.root / "single_server.json").read_text(encoding="utf-8"))


def start(ws, goals):
    # quick: replications cap at 50 not 200; the checks here are plumbing, and study_validate ignores the count
    r = T.call("study_start", question="Is one server enough?", size="quick", changes={"goals": goals, "plan_approved": True})
    assert "error" not in r, r
    return r["study"]


@pytest.mark.slow
def test_tail_goal_is_a_goal_kpi(ws):
    sid = start(ws, [TAIL])
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert "error" not in r, r
    share = r["kpis"][NAME]
    assert 0 < share < 1
    assert NAME in r["precision"] and r["precision"][NAME]["half_width"] > 0
    assert any(c["name"] == "goal g1" and c["verdict"] in ("passed", "failed") for c in r["checks"])
    st = T.call("study_status", study=sid)
    assert st["goals"][0]["status"] in ("met", "not_met")
    assert NAME in json.loads((ws.root / sid / "versions" / "v001" / "step.json").read_text())["kpis"]


def test_tail_goal_not_verified_when_the_shares_cannot_be_rebuilt(ws, monkeypatch):
    monkeypatch.setattr("nagasim.agent_tools.study.wait_shares", lambda *a, **k: None)
    sid = start(ws, [TAIL])
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert NAME not in r["kpis"] and "Server" in r["tail_note"] and "g1" in r["tail_note"]
    assert T.call("study_status", study=sid)["goals"][0]["status"] == "not_verified"


@pytest.mark.slow
def test_tail_goal_in_scenarios(ws):
    m = layout(ws)
    m["scenarios"] = [{"name": "two", "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    sid = start(ws, [TAIL])
    r = T.call("study_step", study=sid, layout=m, note="with a second server")
    sc = r["scenarios"]["two"]
    assert sc["kpis"][NAME]["mean"] < r["kpis"][NAME] and sc["goals"]["g1"] == "met"


@pytest.mark.slow
def test_validate_the_queue_formula(ws):
    sid = start(ws, [{"id": "g1", "text": "wait", "kpi": "Avg Throughput Time", "high": 50}])
    T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert "study_validate" in T.call("study_status", study=sid)["validate"]
    r = T.call("study_validate", study=sid)
    assert "error" not in r, r
    assert r["kind"] == "queue_formula" and r["passed"] is True and abs(r["rel_error"]) <= 0.05
    assert r["formula"] == pytest.approx(3.2)       # rho^2 / (mu (1 - rho)) = 0.64 / (1.25 * 0.2)
    entry = json.loads((ws.root / sid / "study.json").read_text())["validation"][-1]
    assert entry["level"] == "known_answer" and entry["passed"] is True and "formula" in entry["text"]
    assert "validate" not in T.call("study_status", study=sid)


@pytest.mark.slow
def test_validate_not_applicable_to_two_resources(ws):
    m = layout(ws)
    m["resources"].append({"name": "Other", "capacity": 1, "discipline": "FIFO"})
    sid = start(ws, [{"id": "g1", "text": "wait", "kpi": "Avg Throughput Time", "high": 50}])
    T.call("study_step", study=sid, layout=m, note="two resources")
    err = T.call("study_validate", study=sid)["error"]
    assert err["code"] == "E_STUDY_NOT_APPLICABLE" and "one resource" in err["message"]
    assert "validate" not in T.call("study_status", study=sid)


def test_validate_needs_a_version(ws):
    sid = start(ws, [])
    assert T.call("study_validate", study=sid)["error"]["code"] == "E_STUDY_NO_VERSION"
