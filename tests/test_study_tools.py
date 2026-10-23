"""The seven study tools on the agent registry (nagasim/agent_tools/study.py)."""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import mcp_server

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
GOAL = {"id": "g1", "text": "wait under 5", "kpi": "Avg Throughput Time", "high": 5}


@pytest.fixture
def ws(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    return T.use_workspace(T.Workspace(tmp_path))


def layout(ws):
    return json.loads((ws.root / "single_server.json").read_text(encoding="utf-8"))


def start(ws, size="quick", **kw):
    # quick caps replications at 50, not 200: a goal KPI on this noisy queue always runs to the cap and
    # these tests check plumbing (records, pages, wording), not a statistic. About 4x less per step.
    r = T.call("study_start", question="Is one server enough?", size=size, **kw)
    assert "error" not in r, r
    return r["study"]


@pytest.mark.slow
def test_start_status_step_round_trip(ws):
    sid = start(ws)
    st = T.call("study_status", study=sid)
    assert "plan_card" in st and st["versions"] == 0 and st["last_version"] is None
    up = T.call("study_update", study=sid, changes={"plan_approved": True, "goals": [GOAL]})
    assert up["ok"] is True and "next_step" in up
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert "error" not in r, r
    assert r["kind"] == "version" and r["id"] == "v001"
    assert not (ws.root / sid / "versions" / "v001" / "report.html").exists()
    assert "sufficiency" in r and "next_step" in r
    st = T.call("study_status", study=sid)
    assert st["last_version"]["id"] == "v001" and st["versions"] == 1 and "plan_card" not in st


def test_default_folder_gets_a_suffix_when_taken(ws):
    a, b = start(ws), start(ws)
    assert a.startswith("naga-studies/") and b == a + "-2"
    assert T.call("study_start", question="q", study=a)["error"]["code"] == "E_STUDY_EXISTS"


def test_start_refuses_an_empty_question(ws):
    assert T.call("study_start", question="  ")["error"]["code"] == "E_STUDY_BAD_VALUE"


def test_a_broken_model_becomes_a_draft(ws):
    sid = start(ws)
    bad = layout(ws)
    bad["blocks"][0]["type"] = "NOT_A_BLOCK"
    r = T.call("study_step", study=sid, layout=bad, note="oops")
    assert "error" not in r and r["kind"] == "draft"


def test_study_outside_the_root_is_refused(ws):
    r = T.call("study_start", question="q", study="../escape")
    assert r["error"]["code"] == "E_PATH_OUTSIDE_ROOT"
    assert not (ws.root.parent / "escape").exists()


@pytest.mark.parametrize("changes", [
    {"accepted": {"reason": "x"}}, {"mode": "fast"}, {"plan_approved": "yes"},
    {"goals": [{"text": "no id"}]}, {"validation": [{"level": "magic", "passed": True}]},
    {"size": "huge"}, {"goals": "nope"}])
def test_update_refuses_bad_values(ws, changes):
    sid = start(ws)
    assert T.call("study_update", study=sid, changes=changes)["error"]["code"] == "E_STUDY_BAD_VALUE"


def test_reissued_step_is_not_recorded_twice(ws):
    sid = start(ws)
    for _ in range(2):
        T.call("study_step", study=sid, layout=layout(ws), note="n", request_id="x")
    assert T.call("study_status", study=sid)["versions"] == 1


def test_add_input_stores_a_transcript_and_sources_resolve(ws):
    sid = start(ws)
    r = T.call("study_add_input", study=sid, name="call.txt", text="We run two cranes...")
    assert r == {"input": "inputs/call.txt"}
    assert (ws.root / sid / "inputs" / "call.txt").read_text(encoding="utf-8") == "We run two cranes..."
    assert T.call("study_add_input", study=sid, name="call.txt", text="x")["error"]["code"]
    T.call("study_update", study=sid, changes={"sources": [{"id": "s1", "text": "call", "input": r["input"]}]})
    inputs = next(c for c in T.call("study_status", study=sid)["checklist"] if c["item"] == "inputs")
    assert inputs["verdict"] == "passed"


def test_add_input_copies_a_workspace_file_and_refuses_odd_names(ws):
    sid = start(ws)
    assert T.call("study_add_input", study=sid, name="m.json", path="single_server.json")["input"] == "inputs/m.json"
    assert T.call("study_add_input", study=sid, name="../x", text="t")["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert T.call("study_add_input", study=sid, name="n", path="nope.csv")["error"]["code"] == "E_FILE_NOT_FOUND"
    assert T.call("study_add_input", study=sid, name="n")["error"]["code"] == "E_STUDY_BAD_VALUE"


def test_accept_and_revert(ws):
    sid = start(ws)
    T.call("study_step", study=sid, layout=layout(ws), note="a")
    T.call("study_step", study=sid, layout=layout(ws), note="b")
    r = T.call("study_revert", study=sid, version="v001", note="back", request_id="r")
    assert r["kind"] == "version" and r["id"] == "v003"
    assert T.call("study_revert", study=sid, version="v009", note="x")["error"]["code"] == "E_STUDY_NO_VERSION"
    acc = T.call("study_accept", study=sid, reason="good enough for now")
    assert acc["sufficiency"].startswith(("accepted with open issues", "sufficient"))
    assert T.call("study_status", study=sid)["sufficiency"] == acc["sufficiency"]


def test_status_is_read_only_and_usage_is_logged(ws):
    sid = start(ws)
    path = ws.root / sid / "study.json"
    before = path.read_bytes()
    T.call("study_status", study=sid)
    assert path.read_bytes() == before
    T.call("study_update", study=sid, changes={"decision": "d"})
    assert json.loads(path.read_text())["usage"][-1].keys() == {"tool", "seconds", "at"}
    assert T.TOOLS["study_status"].read_only


def test_missing_study_is_a_coded_error(ws):
    assert T.call("study_status", study="nothing")["error"]["code"] == "E_STUDY_NOT_FOUND"


def test_instructions_lead_with_the_study_protocol():
    assert "study_status" in mcp_server.INSTRUCTIONS[:512] and "next_step" in mcp_server.INSTRUCTIONS[:512]


def test_a_reissued_draft_keeps_its_problems(ws):
    sid = start(ws)
    bad = layout(ws)
    bad["blocks"][0]["type"] = "NOT_A_BLOCK"
    a = T.call("study_step", study=sid, layout=bad, note="x", request_id="d")
    b = T.call("study_step", study=sid, layout=bad, note="x", request_id="d")
    assert a["kind"] == "draft" and a["problems"] and b["problems"] == a["problems"]


@pytest.mark.parametrize("inp", ["../x", "inputs/../x", "/etc/x", "C:/x", "other/x", "inputs/", 3])
def test_sources_input_must_stay_in_inputs(ws, inp):
    sid = start(ws)
    r = T.call("study_update", study=sid, changes={"sources": [{"id": "s", "input": inp}]})
    assert r["error"]["code"] == "E_STUDY_BAD_VALUE" and "sources" in r["error"]["message"]


@pytest.mark.parametrize("where", [".", ""])
def test_the_workspace_root_is_not_a_study(ws, where):
    assert T.call("study_start", question="q", study=where)["error"]["code"] == "E_STUDY_BAD_VALUE"


def test_add_input_logs_usage_and_leaves_no_temp(ws):
    sid = start(ws)
    T.call("study_add_input", study=sid, name="m.json", path="single_server.json")
    assert [p.name for p in (ws.root / sid / "inputs").iterdir()] == ["m.json"]
    assert json.loads((ws.root / sid / "study.json").read_text())["usage"][-1]["tool"] == "study_add_input"


@pytest.mark.slow
def test_unmet_goal_is_a_version_not_a_draft(ws):
    sid = start(ws)
    T.call("study_update", study=sid, changes={"goals": [
        {"id": "g1", "text": "impossible", "kpi": "Avg Throughput Time", "high": 0.0001}]})
    r = T.call("study_step", study=sid, layout=layout(ws), note="first")
    assert r["kind"] == "version" and r["id"] == "v001" and r["verdict"] == "partial"
    st = T.call("study_status", study=sid)
    assert st["goals"][0]["status"] == "not_met" if "status" in st["goals"][0] else "not_met" in json.dumps(st["goals"])
    assert "first version" not in st["next_step"].lower()


def test_step_does_not_touch_an_open_model_named_like_the_folder(ws):
    sid = start(ws, study="mystudy")
    lay = layout(ws)
    T.call("write_model", model="mystudy", layout=lay)
    before = json.dumps(ws.models["mystudy"].to_layout(), sort_keys=True)
    other = dict(lay, blocks=lay["blocks"][:-1] if len(lay["blocks"]) > 2 else lay["blocks"])
    T.call("study_step", study=sid, layout=other, note="n")
    assert json.dumps(ws.models["mystudy"].to_layout(), sort_keys=True) == before


def test_revert_puts_the_old_layout_in_the_slot(ws):
    sid = start(ws)
    lay = layout(ws)
    T.call("study_step", study=sid, layout=lay, note="a")
    lay2 = json.loads(json.dumps(lay))
    lay2["blocks"][0]["name"] = lay2["blocks"][0]["name"] + "X"
    T.call("study_step", study=sid, layout=lay2, note="b")
    r = T.call("study_revert", study=sid, version="v001", note="back")
    assert r["kind"] == "version"
    names = [b.name if hasattr(b, "name") else b["name"] for b in ws.models[f"study:{sid}"].to_layout()["blocks"]]
    assert names == [b["name"] for b in lay["blocks"]]
    assert not (ws.root / sid / "versions" / r["id"] / "report.html").exists()


def test_request_id_is_per_tool(ws):
    sid = start(ws)
    T.call("study_step", study=sid, layout=layout(ws), note="a", request_id="x")
    r = T.call("study_revert", study=sid, version="v001", note="back", request_id="x")
    assert r["id"] == "v002"
    assert T.call("study_revert", study=sid, version="v001", note="back", request_id="x")["id"] == "v002"
    assert T.call("study_status", study=sid)["versions"] == 2


def _one_version(ws):
    sid = start(ws)
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert r["id"] == "v001", r
    return sid


def test_view_returns_a_png_image(ws):
    pytest.importorskip("matplotlib")
    import base64
    sid = _one_version(ws)
    r = T.call("study_view", study=sid)
    assert r["version"] == "v001" and r["format"] == "png"
    assert r["image"]["mime_type"] == "image/png"
    assert base64.b64decode(r["image"]["data"]).startswith(b"\x89PNG")
    assert "built" in r["text"]
    assert "<svg" in T.call("study_view", study=sid, format="svg")["svg"]


def test_view_text_without_matplotlib(ws, monkeypatch):
    from nagasim import flowchart
    sid = _one_version(ws)
    monkeypatch.setattr(flowchart, "to_png", lambda *a, **k: None)
    r = T.call("study_view", study=sid)
    assert r["format"] == "text" and "image" not in r and "nagasim[plot]" in r["note"]


def test_view_unknown_version_or_format(ws):
    sid = _one_version(ws)
    for bad in ("v009", "../x", "d001"):
        assert T.call("study_view", study=sid, version=bad)["error"]["code"] == "E_STUDY_NO_VERSION"
    assert T.call("study_view", study=sid, format="gif")["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert T.call("study_view", study=start(ws, study="naga-studies/empty"))["error"]["code"] == "E_STUDY_NO_VERSION"


def test_view_of_a_wide_model_points_to_the_text(ws, monkeypatch):
    pytest.importorskip("matplotlib")
    from nagasim import flowchart
    sid = _one_version(ws)
    assert "note" not in T.call("study_view", study=sid)
    real = flowchart.geometry
    monkeypatch.setattr(flowchart, "geometry", lambda *a, **k: {**real(*a, **k), "width": 5000})
    assert "read the text" in T.call("study_view", study=sid)["note"]


@pytest.mark.slow
def test_a_version_refreshes_the_page(ws):
    sid = start(ws)
    T.call("study_update", study=sid, changes={"plan_approved": True, "goals": [GOAL]})
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert r["page"] == f"{sid}/control-room.html"
    assert "v001" in (ws.root / sid / "control-room.html").read_text(encoding="utf-8")
    assert T.call("study_status", study=sid)["page"] == r["page"]


def test_a_draft_does_not_write_the_page(ws):
    sid = start(ws)
    bad = layout(ws)
    bad["blocks"][0]["type"] = "NOT_A_BLOCK"
    r = T.call("study_step", study=sid, layout=bad, note="oops")
    assert r["kind"] == "draft" and "page" not in r
    assert not (ws.root / sid / "control-room.html").exists()
    assert "page" not in T.call("study_status", study=sid)


def test_package_tool_returns_files(ws):
    sid = start(ws)
    T.call("study_step", study=sid, layout=layout(ws), note="first model")
    r = T.call("study_package", study=sid)
    assert "error" not in r, r
    assert len(r["files"]) == 4 and all((ws.root / f).is_file() for f in r["files"])
    assert r["files"][0].startswith(sid + "/package/")


def test_package_tool_without_versions(ws):
    sid = start(ws)
    assert T.call("study_package", study=sid)["error"]["code"] == "E_STUDY_NO_VERSION"


def test_accept_refreshes_the_page(ws):
    sid = _one_version(ws)
    T.call("study_accept", study=sid, reason="good enough for the budget talk")
    page = (ws.root / sid / "control-room.html").read_text(encoding="utf-8")
    assert "accepted with open issues" in page


@pytest.mark.slow
def test_show_summarises(ws):
    sid = start(ws)
    T.call("study_update", study=sid, changes={"plan_approved": True, "goals": [GOAL]})
    assert "error" not in T.call("study_step", study=sid, layout=layout(ws), note="first model")
    r = T.call("study_show", study=sid)
    assert r["study"] == sid and r["versions"] == 1 and r["goals_met"].endswith(" of 1")
    assert r["page"] == f"{sid}/control-room.html" and r["sufficiency"] in r["text"]


def test_show_on_an_empty_study(ws):
    sid = start(ws)
    r = T.call("study_show", study=sid)
    assert r["versions"] == 0 and r["goals_met"] == "0 of 0" and r["page"] is None and r["text"]


def test_page_html_is_the_page(ws):
    sid = start(ws)
    html = T.call("study_page_html", study=sid)["html"]     # no version yet: the empty-study page
    assert html.startswith("<!DOCTYPE html>") and 'id="actions"' in html
    assert f'data-study="{sid}"' in html
    assert T.call("study_page_html", study="nope")["error"]["code"]


def test_validation_without_a_boolean_passed_is_refused_with_the_shape(ws):
    # dogfood 2026-10-19: {"status": "passed"} was stored and silently ignored by the checklist.
    sid = start(ws)
    bad = T.call("study_update", study=sid, changes={"validation": [{"level": "known_answer", "status": "passed"}]})
    assert bad["error"]["code"] == "E_STUDY_BAD_VALUE" and '"passed": true' in bad["error"]["hint"]
    ok = T.call("study_update", study=sid, changes={"validation": [{"level": "known_answer", "passed": True}]})
    assert ok["ok"] is True
    T.call("study_update", study=sid, changes={"plan_approved": True})
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert r["replications"] == 10        # no goal KPI: the first pass, whatever the model says
    item = [c for c in T.call("study_status", study=sid)["checklist"] if c["item"] == "validation"]
    assert item[0]["verdict"] == "passed"   # validation holds across versions


def test_scenarios_of_a_step_run_on_the_study_slot(ws):
    # The experiments next_step: scenarios in the layout, then run_scenarios on "study:<id>".
    sid = start(ws)
    lay = layout(ws)
    lay["scenarios"] = [{"name": "two", "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    assert T.call("study_step", study=sid, layout=lay, note="with a scenario")["kind"] == "version"
    r = T.call("run_scenarios", model=f"study:{sid}", replications=3)
    assert "error" not in r, r
    assert any(row["scenario"] == "two" for row in r["comparison"])


def _with_scenarios(ws, n=2):
    lay = layout(ws)
    lay["scenarios"] = [{"name": f"S{i}", "overrides": [{"path": "resources.Server.capacity", "value": 1 + i}]}
                        for i in range(1, n + 1)]
    return lay


@pytest.mark.slow
def test_step_runs_scenarios_and_judges_the_goals(ws):
    sid = start(ws)
    goals = [GOAL, {"id": "g2", "text": "server busy", "kpi": "Server/Time in Queue Avg", "high": 100}]
    T.call("study_update", study=sid, changes={"goals": goals})
    r = T.call("study_step", study=sid, layout=_with_scenarios(ws), note="two scenarios")
    assert r["kind"] == "version" and "scenario_error" not in r, r
    assert set(r["scenarios"]) == {"S1", "S2"}
    s1 = r["scenarios"]["S1"]
    assert set(s1["goals"]) == {"g1", "g2"} and "mean" in s1["kpis"]["Avg Throughput Time"]
    assert "ci_half_width" in s1["kpis"]["Avg Throughput Time"]
    step = json.loads((ws.root / sid / "versions" / "v001" / "step.json").read_text(encoding="utf-8"))
    study_json = json.loads((ws.root / sid / "study.json").read_text(encoding="utf-8"))
    assert step["scenarios"] == r["scenarios"] == study_json["versions"][0]["scenarios"]
    # the page shows the scenario table
    assert "S2" in (ws.root / sid / "control-room.html").read_text(encoding="utf-8")


@pytest.mark.slow
def test_a_fresh_workspace_steps_with_scenarios(ws):
    sid = start(ws)
    T.call("study_update", study=sid, changes={"goals": [GOAL]})
    T.call("study_step", study=sid, layout=_with_scenarios(ws), note="one")
    again = T.use_workspace(T.Workspace(ws.root))      # a new process: the model slots are gone
    r = T.call("study_step", study=sid, layout=_with_scenarios(ws), note="two")
    assert r["kind"] == "version" and set(r["scenarios"]) == {"S1", "S2"}, r
    assert again is not ws


@pytest.mark.slow
def test_scenarios_are_capped_by_the_size(ws):
    sid = start(ws, size="quick")
    T.call("study_update", study=sid, changes={"goals": [GOAL]})
    r = T.call("study_step", study=sid, layout=_with_scenarios(ws, 3), note="three")
    assert set(r["scenarios"]) == {"S1", "S2"} and "S3" in r["scenarios_skipped"]


def test_a_bad_scenario_never_fails_the_step(ws):
    sid = start(ws)
    lay = layout(ws)
    lay["scenarios"] = [{"name": "Bad", "overrides": [{"path": "resources.Nope.capacity", "value": 2}]}]
    r = T.call("study_step", study=sid, layout=lay, note="bad scenario")
    assert r["kind"] == "version" and "scenario_error" in r and "scenarios" not in r


QGOAL = {"id": "g1", "text": "queue wait", "kpi": "Server/Time in Queue Avg", "high": 5}


def step_with(ws, goals, **upd):
    sid = start(ws)
    T.call("study_update", study=sid, changes={"plan_approved": True, "goals": goals, **upd})
    return sid, T.call("study_step", study=sid, layout=layout(ws), note="m")


def test_step_without_goals_uses_first_pass(ws):
    sid, r = step_with(ws, [])
    assert r["replications"] == 10 and "provisional" in r["precision_note"] and r["precision"] == {}


@pytest.mark.slow
def test_step_raises_replications_for_a_noisy_goal_kpi(ws):
    sid, r = step_with(ws, [{**QGOAL, "precision": 0.05}])
    p = r["precision"]["Server/Time in Queue Avg"]
    assert r["replications"] > 10
    assert p["met"] or r["replications"] == 50
    step = json.loads((ws.root / sid / "versions" / "v001" / "step.json").read_text(encoding="utf-8"))
    assert step["replications"] == r["replications"] and step["precision"] == r["precision"]
    assert T.call("study_status", study=sid)["last_version"].get("replications") == r["replications"]


def test_zero_mean_goal_uses_absolute_target(ws):
    # Total Cost is 0 in this model: a relative target on a zero mean must not demand the cap.
    sid, r = step_with(ws, [{"id": "g1", "text": "no cost", "kpi": "Total Cost", "high": 5}])
    assert r["replications"] == 10 and r["precision"]["Total Cost"]["met"] is True


@pytest.mark.slow
def test_cap_reached_is_partial_not_failed(ws):
    sid, r = step_with(ws, [{**QGOAL, "precision": 1e-9}], size="quick")
    assert r["replications"] == 50 and r["kind"] == "version" and r["verdict"] == "partial"
    c = [c for c in r["checks"] if c["kind"] == "precision"][0]
    assert c["verdict"] == "partial" and "cap" in r["precision_note"]


@pytest.mark.slow
def test_scenarios_use_the_final_count(ws):
    lay = layout(ws)
    lay["scenarios"] = [{"name": "two", "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    sid = start(ws)
    T.call("study_update", study=sid, changes={"goals": [{**QGOAL, "precision": 0.05}]})
    r = T.call("study_step", study=sid, layout=lay, note="m")
    assert r["replications"] > 10 and "two" in r["scenarios"]


@pytest.mark.slow
def test_precision_sentence_in_answer(ws):
    sid, r = step_with(ws, [{**QGOAL, "precision": 0.05}])
    assert f"{r['replications']} replications" in r["precision_note"]


def test_precision_settings_are_validated(ws):
    sid = start(ws)
    for ch in ({"precision": {"relative": 1.5}}, {"precision": {"relative": "x"}}, {"precision": 0.1},
               {"goals": [{"id": "g", "precision": -1}]}, {"goals": [{"id": "g", "precision": True}]}):
        assert T.call("study_update", study=sid, changes=ch)["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert T.call("study_update", study=sid, changes={"precision": {"relative": 0.03}})["ok"] is True


def test_a_version_has_no_report_by_default(ws):
    sid = start(ws)
    r = T.call("study_step", study=sid, layout=layout(ws), note="a")
    assert "report_error" not in r
    assert not (ws.root / sid / "versions" / r["id"] / "report.html").exists()


@pytest.mark.slow
def test_report_on_request(ws):
    sid = start(ws)
    T.call("study_step", study=sid, layout=layout(ws), note="a", seed=3)
    out = T.call("study_report", study=sid, version="v001")
    assert out["path"] == f"{sid}/versions/v001/report.html" and set(out) == {"path", "page_url"}, out
    assert "<html" in (ws.root / out["path"]).read_text(encoding="utf-8").lower()
    # no version means the last one
    assert T.call("study_report", study=sid)["path"] == out["path"]


def test_report_unknown_version(ws):
    sid = start(ws)
    assert T.call("study_report", study=sid)["error"]["code"] == "E_STUDY_NO_VERSION"
    T.call("study_step", study=sid, layout=layout(ws), note="a")
    assert T.call("study_report", study=sid, version="v009")["error"]["code"] == "E_STUDY_NO_VERSION"


# -- one call does more -------------------------------------------------------

def test_canonical_update_answer_is_unchanged(ws):
    sid = start(ws)
    up = T.call("study_update", study=sid, changes={"goals": [GOAL]})
    assert set(up) == {"ok", "next_step", "options"}     # options: what next (D727)


def test_near_miss_update_is_mapped_and_reported(ws):
    sid = start(ws)
    up = T.call("study_update", study=sid, changes={"goals": [
        {"id": "g1", "statement": "wait under 5", "kpi": "Avg Throughput Time", "range": [0, 5]}]})
    assert up["ok"] is True and any("statement -> text" in n for n in up["normalised"])
    stored = T.call("study_status", study=sid)["goals"]
    assert stored[0]["id"] == "g1"
    s = json.loads((ws.root / sid / "study.json").read_text(encoding="utf-8"))
    assert s["goals"][0]["text"] == "wait under 5" and s["goals"][0]["high"] == 5 and "range" not in s["goals"][0]


def test_update_refuses_a_typo_with_an_example(ws):
    sid = start(ws)
    e = T.call("study_update", study=sid, changes={"goalz": []})["error"]
    assert e["code"] == "E_STUDY_BAD_VALUE" and "goals" in e["message"] and e["hint"]


def test_update_takes_record_keys_as_parameters(ws):
    sid = start(ws)
    up = T.call("study_update", study=sid, goals=[GOAL], plan_approved=True)
    assert up["ok"] is True and "normalised" not in up
    assert T.call("study_status", study=sid)["goals"]
    both = T.call("study_update", study=sid, changes={"question": "a"}, question="b")["error"]
    assert both["code"] == "E_STUDY_BAD_VALUE" and "'a'" in both["message"] and "'b'" in both["message"]
    assert T.call("study_update", study=sid)["error"]["code"] == "E_STUDY_BAD_VALUE"


def test_start_takes_the_plan_card_in_one_call(ws):
    r = T.call("study_start", question="q?", changes={"goals": [GOAL], "plan_approved": True})
    assert "error" not in r, r
    assert r["plan_card"]["goals"][0]["id"] == "g1"
    assert "normalised" not in r
    bad = T.call("study_start", question="q?", study="naga-studies/x", changes={"goalz": []})
    assert bad["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert not (ws.root / "naga-studies" / "x").exists()


@pytest.mark.slow
def test_step_records_changes_then_steps(ws):
    sid = start(ws)
    lay = layout(ws)
    lay["scenarios"] = [{"name": "more", "overrides": {"resources.Server.capacity": 2}}]
    r = T.call("study_step", study=sid, layout=lay, note="a", changes={"goals": [GOAL], "plan_approved": True})
    assert "error" not in r, r
    assert any("overrides" in n for n in r["normalised"])
    assert T.call("study_status", study=sid)["goals"][0]["id"] == "g1"
    bad = T.call("study_step", study=sid, layout=layout(ws), note="b", changes={"goalz": []})
    assert bad["error"]["code"] == "E_STUDY_BAD_VALUE"
    assert T.call("study_status", study=sid)["versions"] == 1


def test_next_step_is_a_ready_to_send_call(ws):
    sid = start(ws)
    up = T.call("study_update", study=sid, changes={"decision": "d"})
    assert f'study_update(study="{sid}", changes=' in up["next_step"]


def test_target_component_maps_to_the_block_with_the_same_name(ws):
    sid = start(ws)
    lay = layout(ws)
    name = next(b["name"] for b in lay["blocks"])
    comp = name.lower().replace("_", " ") if "_" in name else name.swapcase()
    T.call("study_update", study=sid, changes={"target": [{"name": comp, "purpose": "p"}]})
    r = T.call("study_step", study=sid, layout=lay, note="a")
    assert any(name in n for n in r["normalised"]), r.get("normalised")
    s = json.loads((ws.root / sid / "study.json").read_text(encoding="utf-8"))
    assert s["target"][0]["blocks"] == [name]


def test_step_without_a_layout_points_to_study_update(ws):
    sid = start(ws)
    err = T.call("study_step", study=sid, note="n", changes={"decision": "d"})["error"]
    assert err["code"] == "E_BAD_ARGUMENT" and "study_update(changes=" in err["hint"]


@pytest.mark.slow
def test_second_pass_extends_the_first(ws, monkeypatch):
    # Exact, not approximate: replication i uses seed + i, so 10 + (n - 10) equals one run of n.
    from nagasim import api
    from nagasim.studychecks import kpi_mean
    real, calls = api.run_model, []

    def spy(layout, *a, **k):
        calls.append(k.get("replications"))
        return real(layout, *a, **k)
    monkeypatch.setattr(api, "run_model", spy)
    sid, r = step_with(ws, [{**QGOAL, "precision": 0.05}])
    n = r["replications"]
    assert n > 10 and (n - 10) in calls and n not in calls
    monkeypatch.setattr(api, "run_model", real)
    step = json.loads((ws.root / sid / "versions" / "v001" / "step.json").read_text(encoding="utf-8"))
    assert T.call("write_model", model="shot", layout=layout(ws))["ok"]
    oneshot = T.call("run_model", model="shot", replications=n, seed=0)
    assert step["kpis"] == {k: kpi_mean(oneshot, k) for k in step["kpis"]}
    assert step["kpis"]


def test_a_version_argument_never_leaves_the_study(ws):
    sid = start(ws)
    T.call("study_step", study=sid, layout=layout(ws), note="a")
    for bad in ["../x", "../../../evil", "V001", "v1", "v001/../v001", str(ws.root / "evil")]:
        for name in ["study_sensitivity", "study_animation", "study_report", "study_view", "study_revert"]:
            args = {"note": "x"} if name == "study_revert" else {}
            r = T.call(name, study=sid, version=bad, **args)
            assert r["error"]["code"] == "E_STUDY_NO_VERSION", (name, bad, r)
    assert not list(ws.root.rglob("evil")) and not (ws.root / "x").exists()


@pytest.fixture
def other_process():
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield child.pid
    child.kill()
    child.wait()


def _foreign_lock(ws, sid, pid):
    from nagasim import study as study_mod
    folder = (ws.root / sid).resolve()
    study_mod._claimed.discard(folder)      # this test process took it on study_start
    (folder / ".naga-writer").write_text(json.dumps({"pid": pid, "started": "x"}), encoding="utf-8")


def test_second_process_is_refused(ws, other_process):
    sid = start(ws)
    _foreign_lock(ws, sid, other_process)
    r = T.call("study_update", study=sid, changes={"question": "changed?"})
    assert r["error"]["code"] == "E_STUDY_BUSY" and str(other_process) in r["error"]["message"]
    assert T.call("study_status", study=sid)["question"] == "Is one server enough?"


def test_stale_writer_is_taken_over(ws):
    import os
    sid = start(ws)
    _foreign_lock(ws, sid, 999999)
    assert T.call("study_update", study=sid, changes={"question": "changed?"})["ok"] is True
    held = json.loads((ws.root / sid / ".naga-writer").read_text(encoding="utf-8"))
    assert held["pid"] == os.getpid()


def test_read_tools_ignore_the_lock(ws, other_process):
    sid = start(ws)
    _foreign_lock(ws, sid, other_process)
    assert "error" not in T.call("study_status", study=sid)


def test_answers_carry_page_url(ws, monkeypatch):
    from nagasim import studyroom
    sid = _one_version(ws)
    st = T.call("study_status", study=sid)
    assert st["page_url"].startswith("file:///") and st["page_url"].endswith("/control-room.html")
    assert "room_url" not in st
    monkeypatch.setattr(studyroom, "url_for", lambda s: f"http://127.0.0.1:9/s/{s}/")
    assert T.call("study_status", study=sid)["room_url"] == f"http://127.0.0.1:9/s/{sid}/"
    assert "page_url" not in T.call("study_status", study="nope")      # a refusal carries no links
