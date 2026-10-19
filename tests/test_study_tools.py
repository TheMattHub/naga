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


def start(ws, **kw):
    r = T.call("study_start", question="Is one server enough?", **kw)
    assert "error" not in r, r
    return r["study"]


def test_start_status_step_round_trip(ws):
    sid = start(ws)
    st = T.call("study_status", study=sid)
    assert "plan_card" in st and st["versions"] == 0 and st["last_version"] is None
    up = T.call("study_update", study=sid, changes={"plan_approved": True, "goals": [GOAL]})
    assert up["ok"] is True and "next_step" in up
    r = T.call("study_step", study=sid, layout=layout(ws), note="first model")
    assert "error" not in r, r
    assert r["kind"] == "version" and r["id"] == "v001"
    assert (ws.root / sid / "versions" / "v001" / "report.html").is_file()
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
    assert (ws.root / sid / "versions" / r["id"] / "report.html").is_file()


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
