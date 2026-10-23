"""The in-chat app template (nagasim.studyapp) and the hooks it needs in the page."""
import re

from nagasim import study, studypage, studyapp


def test_template_is_static_and_self_contained():
    html = studyapp.HTML
    assert studyapp.URI == "ui://naga/study.html"
    assert "http://" not in html and "https://" not in html and "src=" not in html
    assert html.count("<script") == 1 and html.count("</script>") == 1
    html.encode("ascii")


def test_template_calls_only_study_tools():
    from nagasim import agent_tools
    for html in (studyapp.HTML, studyapp.room_html()):
        names = set(re.findall(r'callTool\("(\w+)"', html))
        assert names and all(n in agent_tools.TOOLS and n.startswith("study_") for n in names), names
    assert studyapp.HTML.count('"tools/call"') == 1


def test_room_variant_uses_fetch_and_polls():
    html = studyapp.room_html()
    assert "/api/rev" in html and "/api/tool" in html and "2000" in html
    assert "parent.postMessage" not in html and "innerHTML" not in html
    html.encode("ascii")


def test_host_variant_has_clipboard_fallback():
    html = studyapp.HTML
    assert '"ui/message"' in html and "navigator.clipboard" in html and "/api/" not in html


def test_buttons_are_drawn_from_the_marks():
    html = studyapp.HTML
    for mark in ("data-latest", "data-plan-pending", "data-accepted", "data-deliverable", "data-path",
                 "#sensitivity[data-empty]"):
        assert mark in html
    for label in ("This answers my request", "Not quite", "Next iteration", "Approve plan", "Show flowchart",
                  "Make it", "Run sensitivity"):
        assert f'"{label}"' in html


def test_every_study_tool_carries_the_ui():
    from nagasim import agent_tools, mcp_server
    assert {n for n in agent_tools.TOOLS if n.startswith("study_")} == set(mcp_server._STUDY_UI)
    assert mcp_server._STUDY_UI["study_page_html"]["visibility"] == ["app"]


def test_template_has_expand_and_collapse_all():
    html = studyapp.HTML
    assert '"Expand all"' in html and '"Collapse all"' in html
    assert "d.open = p[1]" in html          # both buttons toggle every panel


def test_template_remembers_open_panels_in_memory_only():
    html = studyapp.HTML
    assert 'addEventListener("toggle"' in html and "restorePanels();" in html
    assert "localStorage" not in html and "sessionStorage" not in html


def test_template_never_uses_innerhtml():
    for html in (studyapp.HTML, studyapp.room_html()):
        assert "innerHTML" not in html and "document.write" not in html


def test_page_marks_versions_and_actions(tmp_path):
    folder = tmp_path / "s"
    st = study.create(folder, 'Why "<b>"?')
    html = studypage.render(folder, st, study_id='a"b/c')
    assert 'id="actions"' in html
    assert 'data-study="a&quot;b/c"' in html
    assert "data-study" not in studypage.render(folder, st)


def test_room_puts_the_assistant_in_a_side_column_on_wide_screens():
    assert "@media (min-width:1100px)" in studyapp.room_html()
    assert "@media" not in studyapp.HTML


def test_insert_drops_javascript_urls():
    html = studyapp.room_html()
    assert 'indexOf("javascript:") === 0' in html and r'replace(/\s+/g, "")' in html and "toLowerCase()" in html


# -- the room's conversation in a real browser, with a fake microphone and voice (D727) ---------------
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BROWSER = next((path for path in (
    shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
    shutil.which("msedge"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if path and Path(path).is_file()), None)

# Stands in for the room server (fetch), the speech recogniser (one scripted utterance per start)
# and the voice (speaking for 50 ms), and drives the panel; the log lands in <pre id="harness">.
_HARNESS = r"""<script>
(function () {
  var log = window.__log = [], events = [];
  function reply(o) { return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(o); } }); }
  window.fetch = function (url, opts) {
    var body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (url.indexOf("api/page") >= 0) { return reply({ html: "<main><section id='actions'></section></main>", rev: "1" }); }
    if (url.indexOf("api/rev") >= 0) { return reply({ rev: "1" }); }
    if (url.indexOf("api/chat?since=") >= 0) {
      return reply({ events: events.slice(+url.split("since=")[1]), running: false, pending: false });
    }
    if (url.indexOf("api/chat") >= 0 && body) {
      log.push("post " + JSON.stringify(body));
      events.push({ kind: "user", text: body.text });
      if (body.what_next) { events.push({ kind: "options", text: "From the study's numbers",
        items: [{ title: "Run sensitivity", why: "8 parameters could move the goal KPIs.", cost: "about 17 runs" }] }); }
      if (body.text !== "silent") { events.push({ kind: "text", text: "Answer to " + body.text }); }
      events.push({ kind: "done", text: "" });
      return reply({ turn: 0 });
    }
    return reply({});
  };
  var synth = { speaking: false,
    getVoices: function () { return [{ lang: "en-US", name: "Natural", localService: true }]; },
    speak: function (u) {
      log.push("speak " + u.text);
      synth.speaking = true;
      setTimeout(function () { synth.speaking = false; if (u.onend) { u.onend(); } }, 50);
    } };
  Object.defineProperty(window, "speechSynthesis", { configurable: true, value: synth });
  window.SpeechSynthesisUtterance = function (t) { this.text = t; };
  // "#no-speech": Chrome's end after a silence (#945), an error then the end, nothing said.
  window.__say = ["#no-speech", "how busy is the server", "stop"];
  window.SpeechRecognition = function () {
    var r = this;
    log.push("listen" + (synth.speaking ? " WHILE SPEAKING" : ""));
    r.start = function () {
      log.push("box " + window.__box() + " listening=" + document.getElementById("chat-listen").className);
      setTimeout(function () {
        var said = window.__say.shift(), res = [{ transcript: said }];
        res.isFinal = true;
        if (said === "#no-speech") { r.onerror({ error: "no-speech" }); }
        else if (said) { r.onresult({ results: [res] }); }
        r.onend();
      }, 30);
    };
    r.abort = function () { log.push("abort"); };
  };
  function at(ms, fn) { setTimeout(fn, ms); }
  // The controls' boxes relative to their row (the log above grows, the row moves as a whole).
  window.__box = function () {
    var row = document.getElementById("chat-send").parentNode.getBoundingClientRect();
    return ["chat-send", "chat-next", "chat-mic", "chat-hands-box", "chat-listen"].map(function (id) {
      var b = document.getElementById(id).getBoundingClientRect();
      return [b.left - row.left, b.top - row.top, b.width].join(":");
    }).join("|");
  };
  window.addEventListener("load", function () {
    at(100, function () { log.push("box " + window.__box() + " start"); document.getElementById("chat-hands").click(); });
    at(1500, function () {
      log.push("hands " + document.getElementById("chat-hands").checked);
      document.getElementById("chat-aloud").click();
      document.getElementById("chat-in").value = "silent";
      document.getElementById("chat-send").click();
    });
    at(2500, function () { document.getElementById("chat-next").click(); });
    at(3500, function () {
      var b = document.querySelectorAll('.chat-opts[data-kind="options"] button');
      log.push("options " + b.length + " " + (b[0] ? b[0].textContent : ""));
      if (b[0]) { b[0].click(); }
    });
    at(4500, function () {
      var pre = document.createElement("pre");
      pre.id = "harness";
      log.push("mic failed " + (document.body.innerText.indexOf("did not work") >= 0));
      pre.textContent = log.join("\n");
      document.body.appendChild(pre);
    });
  });
})();
</script>"""


@pytest.mark.skipif(BROWSER is None, reason="neither Chrome nor Edge is installed")
def test_hands_free_listens_again_after_speaking_and_stops_on_stop(tmp_path):
    page = tmp_path / "room.html"
    page.write_text(studyapp.room_html().replace("<head>", "<head>" + _HARNESS, 1), encoding="utf-8")
    done = subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--no-first-run", "--dump-dom",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=6000", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    found = re.search(r'<pre id="harness">(.*?)</pre>', done.stdout, re.S)
    assert found, done.stderr[-2000:]
    log = found.group(1).replace("&quot;", '"').replace("&amp;", "&").splitlines()
    posts = [json.loads(x[5:]) for x in log if x.startswith("post ")]
    assert posts == [{"text": "how busy is the server", "voice": True},
                     {"text": "silent", "voice": False},
                     {"text": "What next?", "voice": False, "what_next": True},
                     {"text": "Go with option 1: Run sensitivity.", "voice": False}], log
    # A silent end listened again with no error shown (#945); then listened, answered aloud,
    # listened again only after speaking (never WHILE SPEAKING); "stop" ended it.
    assert [x for x in log if x.startswith(("listen", "speak"))] == [
        "listen", "listen", "speak Answer to how busy is the server", "listen",
        "speak Answer to What next?", "speak Answer to Go with option 1: Run sensitivity."], log
    assert "hands false" in log and "mic failed false" in log
    # #922: a turn with no text reads nothing aloud (it used to repeat the answer before it).
    assert "speak Answer to silent" not in log and log.count("speak Answer to how busy is the server") == 1
    # The listening slot is reserved: the What next? button does not move when it shows.
    boxes = [x.split()[1] for x in log if x.startswith("box ")]
    assert len(set(boxes)) == 1 and any("chat-slot on" in x for x in log), log
    assert any(x.startswith("options 1 1. Run sensitivity: 8 parameters") for x in log), log
