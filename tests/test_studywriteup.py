"""Write-up and slides on request (nagasim.studywriteup, study_writeup, study_slides)."""
import json
import re
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
import nagasim.agent_tools.study_sensitivity  # noqa: F401  registers the tool
import nagasim.agent_tools.study_writeup  # noqa: F401  registers the tools
from nagasim import study as study_mod, studywriteup

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
KPI = "Avg Throughput Time"
EVIL = '<script>alert(1)</script> & "q"'
HEADINGS = ["Abstract", "1 Objective", "2 Model logic", "3 Data and assumptions", "4 Experimentation",
            "5 Verification and validation", "6 Results", "7 Limitations", "8 Discussion", "References"]


@pytest.fixture
def ws(tmp_path):
    shutil.copy(MODELS / "single_server.json", tmp_path / "m.json")
    return T.use_workspace(T.Workspace(tmp_path))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One realistic study for the whole module (a tail goal makes its steps slow)."""
    root = tmp_path_factory.mktemp("wu")
    shutil.copy(MODELS / "single_server.json", root / "m.json")
    ws = T.use_workspace(T.Workspace(root))
    return ws, _build(ws)


@pytest.fixture
def sid(built):
    T.use_workspace(built[0])
    return built[1]


def _build(ws):
    # quick: replications cap at 50 not 200; the write-up tests read wording and structure
    sid = T.call("study_start", question=f"Is one server enough? {EVIL}", size="quick")["study"]
    goals = [{"id": "g1", "text": "time", "kpi": KPI, "high": 1000},
             {"id": "g2", "text": "waits", "kpi": "Server/Time in Queue", "share_above": 5, "high": 0.9}]
    r = T.call("study_update", study=sid, changes={
        "target": [{"name": "server", "purpose": "serve", "blocks": ["process_1"]}],
        "goals": goals, "decision": f"Buy a second server? {EVIL}",
        "assumptions": [{"id": "a1", "text": f"Arrivals are Poisson {EVIL}", "source": "plant log 2026"}],
        "validation": [{"level": "known_answer", "passed": True, "text": "M/M/1 formula"}]})
    assert "error" not in r, r
    layout = json.loads((ws.root / "m.json").read_text(encoding="utf-8"))
    layout["simulation"]["time_limit"] = 150
    assert T.call("study_step", study=sid, layout=layout, note="first")["kind"] == "version"
    layout["scenarios"] = [{"name": "two servers",
                            "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    assert T.call("study_step", study=sid, layout=layout, note=f"scenario {EVIL}")["kind"] == "version"
    assert "error" not in T.call("study_sensitivity", study=sid)
    return sid


def _folder(ws, sid):
    return ws.resolve(sid)


@pytest.mark.slow
def test_writeup_has_every_heading_and_the_numbers(built, sid):
    ws = built[0]
    r = T.call("study_writeup", study=sid, discussion="Capacity is the lever.")
    assert "error" not in r, r
    html = (ws.root / r["html"]).read_text(encoding="utf-8")
    md = (ws.root / r["markdown"]).read_text(encoding="utf-8")
    s = study_mod.load(_folder(ws, sid))
    for h in HEADINGS:
        assert f">{h}<" in html, h
        assert re.search(rf"^#+ {re.escape(h)}$", md, re.M), h
    assert study_mod.sufficiency(s)["label"] in html and study_mod.sufficiency(s)["label"] in md
    mean = studywriteup._n(s['versions'][-1]['kpis'][KPI])
    assert mean in md
    assert mean in md
    assert "two servers" in html and "plant log 2026" in md and "M/M/1 formula" in md
    assert "Capacity is the lever." in html and "Capacity is the lever." in md
    assert "<em>Simulation Modeling and Analysis</em>" in html and "*" not in html.split("References")[-1]
    assert "*Simulation Modeling and Analysis*" in md
    assert "Law, A. M." in html and "Sargent" in md                  # sources from method.md
    assert "Figure 1: flowchart of v002" in html and "Figure 1: flowchart of v002" in md
    assert "Swing of" in html and "<svg" not in md                   # sensitivity chart in the HTML only
    assert "| Scenario |" in md


def test_writeup_without_discussion_or_sensitivity(ws):
    sid = T.call("study_start", question="q")["study"]
    layout = json.loads((ws.root / "m.json").read_text(encoding="utf-8"))
    layout["simulation"]["time_limit"] = 100
    T.call("study_step", study=sid, layout=layout, note="n")
    r = T.call("study_writeup", study=sid)
    html = (ws.root / r["html"]).read_text(encoding="utf-8")
    assert "No discussion provided." in html
    assert "No sensitivity analysis was run." in html


@pytest.mark.slow
def test_writeup_escapes_and_has_no_script(built, sid):
    ws = built[0]
    r = T.call("study_writeup", study=sid, discussion=EVIL)
    html = (ws.root / r["html"]).read_text(encoding="utf-8")
    assert EVIL not in html and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;" in html
    assert "<script" not in html and "default-src 'none'" in html
    html.encode("ascii")
    (ws.root / r["markdown"]).read_text(encoding="ascii")


def test_writeup_needs_a_version(ws):
    sid = T.call("study_start", question="q")["study"]
    assert T.call("study_writeup", study=sid)["error"]["code"] == "E_STUDY_NO_VERSION"


@pytest.mark.slow
def test_slides(built, sid):
    ws = built[0]
    r = T.call("study_slides", study=sid)
    assert "error" not in r, r
    html = (ws.root / r["path"]).read_text(encoding="utf-8")
    assert html.count('<section class="slide"') == 9           # sensitivity slide included
    assert "scroll-snap-type" in html and "@page" in html and "16in 9in" in html
    assert "break-after:page" in html.replace(" ", "")
    assert "<script" not in html and "default-src 'none'" in html
    assert EVIL not in html and "&lt;script&gt;" in html
    assert "<svg" in html and "two servers" in html
    assert "aspect-ratio:16/9" in html and "box-shadow" in html
    assert "1 / 9</span>" in html and "9 / 9</span>" in html
    ans = html.split("The answer</h2>")[1].split("</section>")[0]
    assert 'class="banner b-' in ans and ans.count('class="tile"') == 2 and 'class="badge' in ans
    assert 'class="charts"' in html                        # options slide bar charts
    html.encode("ascii")


def test_slides_skip_sensitivity_when_absent(ws):
    sid = T.call("study_start", question="q")["study"]
    layout = json.loads((ws.root / "m.json").read_text(encoding="utf-8"))
    layout["simulation"]["time_limit"] = 100
    T.call("study_step", study=sid, layout=layout, note="n")
    html = (ws.root / T.call("study_slides", study=sid)["path"]).read_text(encoding="utf-8")
    assert html.count('<section class="slide"') == 8


@pytest.mark.slow
def test_hand_edited_sensitivity_cannot_inject(built, sid):
    ws = built[0]
    f = _folder(ws, sid) / "versions" / "v002" / "sensitivity.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    d["charts"] = {k: "<svg><script>x</script></svg>" for k in d["charts"]}
    d["factors"][0]["label"] = EVIL
    f.write_text(json.dumps(d), encoding="utf-8")
    html = studywriteup.writeup(_folder(ws, sid), study_mod.load(_folder(ws, sid)))[0].read_text(encoding="utf-8")
    assert "<script" not in html and EVIL not in html


def test_documents_and_the_control_room_print_the_same_numbers():
    from nagasim import studypage
    assert studypage._num(2.315e-05) == studywriteup._n(2.315e-05) == "0.00002315"
    assert studypage._num(1234567.0) == "1234567" and studypage._num(None) == ""
    assert studypage._num(0.2215, "X/Share waiting") == "22.1%"


@pytest.mark.slow
def test_outputs_json_records_the_version_built_from(built, sid):
    folder = _folder(built[0], sid)
    studywriteup.writeup(folder, study_mod.load(folder))
    studywriteup.slides(folder, study_mod.load(folder))
    assert json.loads((folder / "package" / "outputs.json").read_text(encoding="utf-8")) == {
        "writeup": "v002", "slides": "v002"}


def test_number_formatting():
    n, sh = studywriteup._n, "X/Share waiting over 5"
    assert n(0.2215, sh) == "22.1%" and n(2.315e-05, sh) == "0.0023%"
    assert studywriteup._hw(0.028, sh) == "2.8 points"
    assert n(1234567.0) == "1234567" and n(2.315e-05) == "0.00002315" and n(12.34567) == "12.35"
    assert "e" not in n(1e-9) and n(2.0) == "2" and n(None) == ""
    assert studywriteup._span({"high": 5, "low": float("-inf")}) == "<= 5"
    assert studywriteup._span({"low": 2}) == ">= 2" and studywriteup._span({"low": 1, "high": 3}) == "1 to 3"
    assert studywriteup._span({}) == ""
