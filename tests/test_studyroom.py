"""The live control room server (nagasim.studyroom): who may talk to it and what it may do."""
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pytest

from nagasim import agent_tools as T
from nagasim import studyroom
from nagasim import study as study_mod

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def room(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    r = studyroom.Room(tmp_path, threading.Lock())
    r.start()
    r.sid = sid
    yield r
    r.stop()


def base(r):
    return f"http://127.0.0.1:{r.port}/s/{quote(r.sid, safe='')}/"


def fetch(url, data=None, headers=None, token=None, origin=True, raw_path=None):
    """(status, headers, body) without raising on 4xx; the token goes in the cookie when given."""
    h = dict(headers or {})
    if token:
        h.setdefault("Cookie", f"naga_room_{url.split(':')[2].split('/')[0]}={token}")
    if data is not None:
        h.setdefault("Content-Type", "application/json")
        if origin:
            h.setdefault("Origin", "http://" + url.split("/")[2])
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def raw(r, path, token=True):
    """A GET with the path sent as written (urllib would not normalise it, but http.client is explicit)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=30)
    c.putrequest("GET", path, skip_accept_encoding=True)
    if token:
        c.putheader("Cookie", f"{r.cookie}={r.token}")
    c.endheaders()
    resp = c.getresponse()
    out = resp.status
    c.close()
    return out


def test_shell_needs_the_token(room):
    assert fetch(base(room))[0] == 403
    assert fetch(base(room) + "?t=wrong")[0] == 403
    code, headers, body = fetch(base(room) + f"?t={room.token}")
    assert code == 200 and b"/api/rev" in body
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and room.token in cookie
    assert headers["Referrer-Policy"] == "no-referrer" and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert fetch(base(room) + "api/rev", token=room.token)[0] == 200


def test_foreign_host_and_origin_are_refused(room):
    before = (room.root / room.sid / "study.json").read_bytes()
    t = room.token
    assert fetch(base(room) + "api/rev", token=t, headers={"Host": "evil.example"})[0] == 403
    body = {"name": "study_update", "arguments": {"plan_approved": True}}
    assert fetch(base(room) + "api/tool", body, token=t, headers={"Origin": "http://evil.example"})[0] == 403
    assert fetch(base(room) + "api/tool", body, token=t, origin=False)[0] == 403
    assert fetch(base(room) + "api/tool", body, token=t, headers={"Content-Type": "text/plain"})[0] == 403
    assert fetch(base(room) + "api/tool", body)[0] == 403        # no token
    assert (room.root / room.sid / "study.json").read_bytes() == before


def test_files_are_confined(room):
    pre = f"/s/{quote(room.sid, safe='')}/files/"
    (room.root / room.sid / "control-room.html").write_text("<p>page</p>", encoding="utf-8")
    for bad in ("../study.json", "%2e%2e/study.json", "C:/Windows/win.ini", "..%2f..%2fsingle_server.json",
                "%2Fetc%2Fpasswd", "nope.txt"):
        assert raw(room, pre + bad) == 404, bad
    assert raw(room, pre + "control-room.html", token=False) == 403
    code, headers, body = fetch(base(room) + "files/control-room.html", token=room.token)
    assert code == 200 and body == b"<p>page</p>" and headers["Content-Security-Policy"] == "sandbox"


def test_unknown_study_is_not_found(room):
    url = f"http://127.0.0.1:{room.port}/s/{quote('..', safe='')}/api/rev"
    assert fetch(url, token=room.token)[0] == 404
    assert fetch(f"http://127.0.0.1:{room.port}/s/single_server.json/api/rev", token=room.token)[0] == 404


def test_only_direct_tools_run(room):
    url, t = base(room) + "api/tool", room.token
    assert fetch(url, {"name": "study_step", "arguments": {"note": "x"}}, token=t)[0] == 403
    assert fetch(url, {"name": "write_model", "arguments": {}}, token=t)[0] == 403
    assert fetch(url, {"name": "study_update", "arguments": {"question": "x"}}, token=t)[0] == 403
    assert fetch(url, {"name": "study_update", "arguments": {"plan_approved": False}}, token=t)[0] == 403
    code, _, body = fetch(url, {"name": "study_update", "arguments": {"study": "elsewhere", "plan_approved": True}},
                          token=t)
    assert code == 200 and json.loads(body)["ok"] is True
    assert json.loads((room.root / room.sid / "study.json").read_text(encoding="utf-8"))["plan_approved"] is True


def test_a_large_body_is_refused_unread(room):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", room.port, timeout=30)
    c.putrequest("POST", f"/s/{quote(room.sid, safe='')}/api/tool")
    for k, v in (("Cookie", f"{room.cookie}={room.token}"), ("Content-Type", "application/json"),
                 ("Origin", f"http://127.0.0.1:{room.port}"), ("Content-Length", str(studyroom.MAX_BODY + 1))):
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status == 413
    c.close()


def test_rev_changes_when_the_chat_writes(room):
    url = base(room) + "api/rev"
    first = json.loads(fetch(url, token=room.token)[2])["rev"]
    T.call("study_update", study=room.sid, changes={"question": "Two servers?"})      # the chat, in-process
    assert json.loads(fetch(url, token=room.token)[2])["rev"] != first
    page = json.loads(fetch(base(room) + "api/page", token=room.token)[2])
    assert "Two servers?" in page["html"] and "Live room:" not in page["html"]     # #733


def test_room_url_is_registered(room):
    assert studyroom.url_for(room.sid) == room.url(room.sid)
    assert T.call("study_status", study=room.sid)["room_url"] == room.url(room.sid)
    room.stop()
    assert studyroom.url_for(room.sid) is None


def test_the_token_is_never_written_to_disk(room):
    # A study folder gets zipped and shared: the page file and the writer lock hold the address only.
    T.call("study_update", study=room.sid, plan_approved=True)
    folder = room.root / room.sid
    from nagasim.agent_tools import study as study_tools
    study_tools._page(folder, study_mod.load(folder))
    study_mod.claim(folder, room=room.address(room.sid))
    for name in ("control-room.html", ".naga-writer"):
        text = (folder / name).read_text(encoding="utf-8")
        assert room.token not in text, name
    assert room.address(room.sid) in (folder / "control-room.html").read_text(encoding="utf-8")


def test_mcp_room_flag_is_cli_only():
    import argparse
    from nagasim import mcp_server
    p = argparse.ArgumentParser()
    mcp_server.add_arguments(p)
    assert p.parse_args(["--room"]).room is True and p.parse_args([]).room is False
    assert all("room" not in spec.input_schema["properties"] for spec in T.TOOLS.values())


def test_cli_lists_room():
    from nagasim import cli
    assert cli.COMMANDS["room"][:2] == ("studyroom", "main")


# -- the room's assistant ---------------------------------------------------------------------

class Fake:
    """Scripted replies; an Exception is raised; a threading.Event is waited on first."""

    kind = "hosted"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, tools, history, on_text=None):
        self.calls.append({"system": system, "tools": [t["name"] for t in tools]})
        item = self.replies.pop(0)
        if isinstance(item, threading.Event):
            item.wait(30)
            item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        if on_text is not None and item.text:
            on_text(item.text)
        return item


def _calls(*calls):
    from nagasim import llm
    return llm.Reply("", tuple(llm.ToolCall(*c) for c in calls), "tools", llm.Usage(1, 1))


def _text(text):
    from nagasim import llm
    return llm.Reply(text, (), "end", llm.Usage(1, 1))


def chat(room, text):
    return fetch(base(room) + "api/chat", {"text": text}, token=room.token)


def events(room, wait=True):
    import time
    for _ in range(300):
        out = json.loads(fetch(base(room) + "api/chat?since=0", token=room.token)[2])
        if not (wait and out["running"]):
            return out
        time.sleep(0.05)
    raise AssertionError("the turn did not end")


def test_chat_turn_runs_study_tools(room):
    room.provider = Fake(_calls(("c1", "study_status", {"study": "some/other"})), _text("It stands at the start."))
    assert chat(room, "where are we?")[0] == 200
    out = events(room)
    kinds = [e["kind"] for e in out["events"]]
    assert kinds == ["user", "tool", "text", "done"], out
    assert out["events"][1]["text"] == "Reading where the study stands" and "study" not in out["events"][1]
    call = room.provider.calls[0]
    assert "study_step" in call["tools"] and "study_page_html" not in call["tools"]
    assert all(n.startswith("study_") for n in call["tools"]) and room.sid in call["system"]
    log = json.loads((room.root / room.sid / "study.json").read_text(encoding="utf-8"))["usage"]
    assert log[-1]["tool"] == "study_start"     # status is read-only; it ran on this study, not "some/other"
    assert not (room.root / "some").exists()


def test_chat_refuses_non_study_tools(room):
    room.provider = Fake(_calls(("c1", "write_model", {"model": "m", "layout": {}})), _text("ok"))
    chat(room, "write a model")
    events(room)
    history = room.chats[room.sid].session.history
    tool = next(e for e in history if e["role"] == "tool")
    assert "E_TOOL_NOT_OFFERED" in tool["content"]


def test_no_provider_says_how_to_set_one(room):
    assert room.provider is None
    assert chat(room, "hello")[0] == 200
    out = events(room)
    assert out["events"][-1]["kind"] == "error" and "ANTHROPIC_API_KEY" in out["events"][-1]["text"]
    assert out["running"] is False


def test_stop_ends_the_turn(room):
    gate = threading.Event()
    room.provider = Fake(gate, _calls(("c1", "study_step", {"layout": {}, "note": "x"})))
    chat(room, "build the model")
    assert chat(room, "again")[0] == 409            # one turn at a time
    assert fetch(base(room) + "api/chat/stop", {}, token=room.token)[0] == 200
    gate.set()
    out = events(room)
    assert out["running"] is False and out["events"][-1] == {"kind": "error", "text": "Stopped."}
    assert json.loads((room.root / room.sid / "study.json").read_text(encoding="utf-8"))["versions"] == []


def test_provider_error_is_redacted(room):
    from nagasim import llm
    room.provider, room.key = Fake(llm.LLMError("bad key sk-ant-XYZ")), "sk-ant-XYZ"
    chat(room, "hi")
    out = events(room)
    assert out["events"][-1]["kind"] == "error"
    assert "sk-ant-XYZ" not in json.dumps(out) and "[REDACTED]" in out["events"][-1]["text"]


def test_panel_only_in_room_variant():
    from nagasim import studyapp
    room = studyapp.room_html()
    assert "api/chat" in room and "SpeechRecognition" in room and "speechSynthesis" in room
    assert "Your browser may send speech to its vendor's service." in room and "700" in room
    assert "api/chat" not in studyapp.HTML and "SpeechRecognition" not in studyapp.HTML
