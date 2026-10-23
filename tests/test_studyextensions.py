"""Candidate extension modules (nagasim/studyextensions.py, agent_tools/study_extensions.py)."""
import json
import shutil
from pathlib import Path

import pytest

import nagasim.agent_tools.study_extensions  # noqa: F401  (registers the tool)
from nagasim import agent_tools as T
from nagasim import studyextensions as ext

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
GOAL = {"id": "g1", "text": "wait under 5", "kpi": "Avg Throughput Time", "high": 5}


@pytest.fixture
def ws(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    return T.use_workspace(T.Workspace(tmp_path))


def base(ws):
    return json.loads((ws.root / "single_server.json").read_text(encoding="utf-8"))


@pytest.fixture
def sid(ws):
    # quick: replications cap at 50 not 200; these tests check plumbing, not a statistic
    s = T.call("study_start", question="Is one server enough?", size="quick")["study"]
    T.call("study_update", study=s, changes={"plan_approved": True, "goals": [GOAL]})
    assert T.call("study_step", study=s, layout=base(ws), note="first")["kind"] == "version"
    return s


@pytest.mark.parametrize("name", list(ext.MODULES))
def test_each_module_validates_and_runs(ws, name):
    layout = ext.apply(name, base(ws))
    assert layout != base(ws)
    assert T.call("write_model", model="m", layout=layout)["ok"]
    assert "error" not in T.call("run_model", model="m", replications=1)


@pytest.mark.slow
def test_candidates_offered_with_indicative_effect(ws, sid):
    got = T.call("study_extensions", study=sid)["candidates"]
    assert {c["name"] for c in got} == set(ext.MODULES)
    assert all(GOAL["kpi"] in c["indicative"] and c["effort"] == "one step" for c in got)


@pytest.mark.slow
def test_a_module_that_breaks_the_model_is_not_offered(ws, sid, monkeypatch):
    def broken(layout, **kw):
        out = json.loads(json.dumps(layout))
        out["blocks"][0]["type"] = "NOT_A_BLOCK"
        return out
    monkeypatch.setitem(ext.MODULES, "shifts", {**ext.MODULES["shifts"], "fn": broken})
    names = {c["name"] for c in T.call("study_extensions", study=sid)["candidates"]}
    assert names == {"failures", "arrival_peak"}


@pytest.mark.slow
def test_apply_returns_a_layout_study_step_accepts(ws, sid):
    r = T.call("study_extensions", study=sid, apply="failures", params={"mtbf": 300})
    assert r["layout"]["failures"][0]["uptime"]["dist_params"]["rate"] == pytest.approx(1 / 300)
    step = T.call("study_step", study=sid, layout=r["layout"], note="add failures")
    assert step["kind"] == "version" and step["id"] == "v002"


@pytest.mark.slow
def test_bad_module_or_params_are_coded_errors(ws, sid):
    assert T.call("study_extensions", study=sid, apply="nope")["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert T.call("study_extensions", study=sid, apply="shifts", params={"x": 1})["error"]["code"] == "E_STUDY_BAD_VALUE"
