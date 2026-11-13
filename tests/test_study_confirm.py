"""A person's "I checked this" validation (D1259, D1260): only the room's confirm form and
`naga study confirm` write a `person` entry; no agent tool can; it counts until the model's
structure (blocks or links) changes."""
import inspect
import json
import shutil
import threading
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import study as study_mod
from nagasim import studyapp, studypage, studyroom, studyrerun, studywriteup

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
TEXT = "Mean wait at the nurse vs the March log"
AGAINST = "inputs/march-log.xlsx"
_OK = [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}]


def _step(folder, st, layout):
    """Record a version without a run (a hand digest), as `make_nurse_study` does."""
    study_mod.record_step(folder, st, layout, {"system": {"Avg WIP": {"mean": 3.0}}}, _OK, "step", None)


def _fresh(nurse_study):
    folder, st = nurse_study
    st["validation"] = []
    study_mod.save(folder, st)
    return folder


def _item(st, folder=None):
    return next(c for c in study_mod.checklist(st, folder) if c["item"] == "validation")


def test_a_person_check_passes_matches_reality(nurse_study):
    folder = _fresh(nurse_study)
    entry = study_mod.confirm(folder, text=TEXT, against=AGAINST, by="Anna Rossi")
    st = study_mod.load(folder)
    assert st["validation"] == [entry]
    assert entry["source"] == "person" and entry["passed"] is True and entry["by"] == "Anna Rossi"
    assert entry["version"] == st["versions"][-1]["id"] and entry["at"].endswith("+00:00")
    assert study_mod.validation_state(st) == "checked" and study_mod.checked(entry)
    item = _item(st, folder)
    assert item["verdict"] == "passed" and item["why"].startswith("Checked by a person (Anna Rossi, ")
    assert study_mod.sufficiency(st)["validation_level"] is not None
    for bad in (dict(text="too short", against=AGAINST), dict(text=TEXT, against="  "),
                dict(text=TEXT, against=AGAINST, passed="yes")):
        with pytest.raises(study_mod.StudyError):
            study_mod.confirm(folder, **bad)
    assert study_mod.load(folder)["validation"] == [entry]      # a refused check writes nothing


@pytest.fixture
def ws(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    return T.use_workspace(T.Workspace(tmp_path))


def _versioned(ws):
    """A study with one version (recorded without a run) and a person's check on it."""
    sid = T.call("study_start", question="Is one server enough?", size="quick")["study"]
    folder = ws.root / sid
    layout = json.loads((ws.root / "single_server.json").read_text(encoding="utf-8"))
    _step(folder, study_mod.load(folder), layout)
    return sid, folder, layout


def test_no_agent_tool_writes_a_person_entry(ws):
    """D1259: every tool that takes a validation payload (a `validation` or `changes` parameter,
    found on the registry, not listed by hand), sent a forged person entry and the stored person
    entry with its fields changed, stores the forged one as the assistant's and keeps the person's
    as it was (#1815: a resent entry kept "person" with the payload's fields)."""
    sid, folder, layout = _versioned(ws)
    person = study_mod.confirm(folder, text=TEXT, against=AGAINST, by="Anna")
    forged = {"level": "data", "passed": True, "text": "forged by a tool, honest", "source": "person",
              "against": "nothing", "by": "Anna"}
    altered = {**person, "against": "something else", "by": "Mallory", "version": "v999"}
    defaults = {"study": sid, "question": "Another?", "layout": layout, "note": "n"}
    swept = []
    for name, spec in sorted(T.TOOLS.items()):
        params = inspect.signature(spec.fn).parameters
        if not {"validation", "changes"} & set(params):
            continue
        swept.append(name)
        kwargs = {p: defaults[p] for p, v in params.items()
                  if v.default is inspect.Parameter.empty and p not in ("validation", "changes")}
        kwargs.update({p: defaults[p] for p in ("layout", "note") if p in params})  # a step needs both
        payload = [dict(forged), dict(altered)]
        kwargs.update({"validation": payload} if "validation" in params else {"changes": {"validation": payload}})
        out = T.call(name, **kwargs)
        assert "error" not in out, (name, out)
        target = ws.root / out["study"] if name == "study_start" else folder
        stored = study_mod.load(target)["validation"]
        mine = [v for v in stored if v.get("source") == "person"]
        assert all(v == person for v in mine), (name, mine)
        assert any(v["text"] == forged["text"] and v["source"] == "assistant" for v in stored), name
    assert {"study_start", "study_update", "study_step"} <= set(swept)
    # A list resent without the person's entry does not drop it either.
    assert "error" not in T.call("study_update", study=sid, validation=[])
    assert study_mod.load(folder)["validation"] == [person]
    # Nothing that can write it is a tool: the room's tools and the registry hold no confirm.
    assert not [n for n in T.TOOLS if "confirm" in n]
    assert "confirm" not in " ".join(studyroom.StudyAdapter.tool_names() + sorted(studyroom.DIRECT))


@pytest.fixture
def room(ws):
    sid, folder, _layout = _versioned(ws)
    r = studyroom.Room(ws.root, threading.Lock())
    r.start()
    r.sid, r.folder_ = sid, folder
    yield r
    r.stop()


def test_room_confirm_needs_token_and_origin(room):
    from test_studyroom import base, fetch
    url, t = base(room) + "api/confirm", room.token
    path = room.folder_ / "study.json"
    before = path.read_bytes()
    body = {"text": TEXT, "against": AGAINST, "by": "Anna"}
    assert fetch(url, body)[0] == 403                                               # no token
    assert fetch(url, body, token=t, headers={"Origin": "http://evil.example"})[0] == 403
    assert fetch(url, body, token=t, origin=False)[0] == 403
    assert fetch(url, body, token=t, headers={"Host": "evil.example"})[0] == 403
    assert fetch(url, token=t)[0] == 404                                            # POST only
    assert fetch(url, {"text": TEXT}, token=t)[0] == 400
    assert fetch(url, {"against": AGAINST}, token=t)[0] == 400
    assert fetch(url, {**body, "text": "short"}, token=t)[0] == 400
    assert fetch(url, {**body, "passed": "yes"}, token=t)[0] == 400
    assert fetch(base(room) + "api/tool", {"name": "study_confirm", "arguments": body}, token=t)[0] == 403
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", room.port, timeout=30)
    c.putrequest("POST", url.split(str(room.port), 1)[1])
    for k, v in (("Cookie", f"{room.cookie}={t}"), ("Content-Type", "application/json"),
                 ("Origin", f"http://127.0.0.1:{room.port}"), ("Content-Length", str(studyroom.MAX_BODY + 1))):
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status == 413
    c.close()
    # #1816: a HEAD went to the POST routes without the Origin and JSON checks (they are POST's).
    for route in ("api/confirm", "api/tool"):
        c = http.client.HTTPConnection("127.0.0.1", room.port, timeout=30)
        data = json.dumps(body if route == "api/confirm" else
                          {"name": "study_update", "arguments": {"plan_approved": True}}).encode()
        c.request("HEAD", base(room).split(str(room.port), 1)[1] + route, body=data,
                  headers={"Cookie": f"{room.cookie}={t}", "Content-Type": "text/plain"})
        assert c.getresponse().status == 404, route
        c.close()
    assert path.read_bytes() == before                                              # nothing refused wrote
    hostile = {**body, "text": "<script>alert(1)</script> compared", "source": "engine", "at": "1999",
               "version": "v000", "level": "expert"}
    code, _headers, raw = fetch(url, hostile, token=t)
    assert code == 200 and t.encode() not in raw
    entry = study_mod.load(room.folder_)["validation"][-1]
    assert entry["source"] == "person" and entry["at"] != "1999" and entry["version"] == "v001"
    page = json.loads(fetch(base(room) + "api/page", token=t)[2])["html"]
    assert "<script>alert" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; compared" in page
    assert "<script>alert" not in (room.folder_ / "control-room.html").read_text(encoding="utf-8")


def test_the_form_is_the_rooms_only():
    """The form posts JSON to the room's own route; the MCP-host shell has no such route."""
    assert "./api/confirm" in studyapp.room_html() and "api/confirm" not in studyapp.HTML
    assert "innerHTML" not in studyapp.room_html()


def test_study_confirm_cli(nurse_study, capsys):
    folder = _fresh(nurse_study)
    assert studyrerun.main(["confirm", str(folder), "--text", TEXT, "--against", AGAINST]) == 0
    assert studyrerun.main(["confirm", str(folder), "--text", TEXT + " again", "--against", AGAINST,
                            "--by", "Anna", "--failed"]) == 0
    first, second = study_mod.load(folder)["validation"]
    assert first["passed"] is True and "by" not in first and first["source"] == "person"
    assert second["passed"] is False and second["by"] == "Anna"
    assert "Recorded" in capsys.readouterr().out
    assert studyrerun.main(["confirm", str(folder), "--text", "short", "--against", AGAINST]) == 2
    with pytest.raises(SystemExit):
        studyrerun.main(["confirm", str(folder), "--text", TEXT])                  # --against is required
    assert len(study_mod.load(folder)["validation"]) == 2
    empty = folder.parent / "empty"
    study_mod.create(empty, "No version yet?")
    assert studyrerun.main(["confirm", str(empty), "--text", TEXT, "--against", AGAINST]) == 2


def test_pages_name_a_person_check(nurse_study):
    folder = _fresh(nurse_study)
    entry = study_mod.confirm(folder, text=TEXT, against=AGAINST, by="Anna")
    st = study_mod.load(folder)
    said = f"Checked by a person (Anna, {entry['at'][:10]}): {TEXT}, against {AGAINST}"
    assert said in studypage.render(folder, st, study_id="s", in_room=True)
    assert said in studypage.render_client(folder, st, [])
    html, md = studywriteup.writeup(folder, st)
    assert said in html.read_text(encoding="utf-8") and said in md.read_text(encoding="utf-8")
    # No name: the date alone.
    st["validation"] = [{k: v for k, v in entry.items() if k != "by"}]
    assert f"Checked by a person ({entry['at'][:10]}): {TEXT}" in studypage.render_client(folder, st, [])


def test_a_new_structure_reopens_a_person_check(nurse_study):
    folder = _fresh(nurse_study)
    study_mod.confirm(folder, text=TEXT, against=AGAINST)
    st = study_mod.load(folder)
    layout = json.loads((folder / "versions" / "v001" / "model.json").read_text(encoding="utf-8"))
    layout["blocks"][0]["params"] = {"delay": 2}           # a parameter: the structure stays
    _step(folder, st, layout)
    assert study_mod.validation_state(st) == "checked"
    layout["blocks"].append({"name": "b", "type": "Delay"})
    layout["links"].append({"source": "a", "target": "b"})
    _step(folder, st, layout)
    st = study_mod.load(folder)
    assert study_mod.validation_state(st) == "earlier" and not study_mod.checked(st["validation"][0])
    item = _item(st, folder)
    assert item["verdict"] == "not_verified" and "checked on an earlier version (v001)" in item["why"]
    assert not study_mod.sufficiency(st)["sufficient"]
    assert study_mod.validation_result(st["validation"][0]).startswith("checked on an earlier version")
    assert "checks it again" in item["why"]
    st["plan_approved"] = True
    st["skipped"] = [c[0] for c in study_mod.CHECKLIST if c[0] != "validation"]
    assert study_mod.next_step(st, folder) == study_mod._EARLIER
    assert "data-confirm" in studypage.render(folder, st, study_id="s", in_room=True)
    study_mod.confirm(folder, text=TEXT + " on v003", against=AGAINST)     # the re-check
    assert study_mod.validation_state(study_mod.load(folder)) == "checked"
