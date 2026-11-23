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

# D1244: every successful tool answer here is checked against its tool's output schema.
pytestmark = pytest.mark.usefixtures("answers_match_schemas")

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
    layout["simulation"]["warmup"] = 0  # #1720
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
    assert html.count('<section class="slide"') == 10          # 9 with the sensitivity slide, and one Words used slide (D1327)
    assert "scroll-snap-type" in html and "@page" in html and "16in 9in" in html
    assert "break-after:page" in html.replace(" ", "")
    assert "<script" not in html and "default-src 'none'" in html
    assert EVIL not in html and "&lt;script&gt;" in html
    assert "<svg" in html and "two servers" in html
    assert "aspect-ratio:16/9" in html and "box-shadow" in html
    assert "1 / 10</span>" in html and "10 / 10</span>" in html
    ans = html.split("The answer</h2>")[1].split("</section>")[0]
    assert 'class="banner b-' in ans and ans.count('class="tile"') == 2 and 'class="badge' in ans
    assert 'class="charts"' in html                        # options slide bar charts
    html.encode("ascii")


def test_slides_skip_sensitivity_when_absent(ws):
    sid = T.call("study_start", question="q")["study"]
    layout = json.loads((ws.root / "m.json").read_text(encoding="utf-8"))
    layout["simulation"]["time_limit"] = 100
    layout["simulation"]["warmup"] = 0  # #1720
    T.call("study_step", study=sid, layout=layout, note="n")
    html = (ws.root / T.call("study_slides", study=sid)["path"]).read_text(encoding="utf-8")
    assert html.count('<section class="slide"') == 9           # 8, and one Words used slide (D1327)


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


def test_1784_answer_slide_leads_with_the_recommended_option(nurse_study):
    """S4-01, S2-08: when the study is sufficient with a scenario, "The answer" leads with that
    option's number and a "met" verdict, the Baseline beside it as the comparison (D1237)."""
    folder, st = nurse_study
    html = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    answer = html.split("The answer</h2>")[1].split("</section>")[0]
    big = answer.split('class="big">')[1].split("<")[0]
    assert big.startswith("5.4%"), big                      # the option, not the base's 27.6%
    assert "Fourth nurse" in answer and "Baseline" in answer and "27.6%" in answer
    assert answer.index("5.4%") < answer.index("27.6%")
    assert 'class="badge met"' in answer and "not met" not in answer.split("Baseline")[0]
    assert "at most 10%" in answer
    # One name for the reference case (Open 2): the options table says Baseline too.
    assert "base model" not in html


def test_deck_says_reported_not_checked(nurse_study):
    """S2-19: a validation the assistant wrote itself (source "assistant", D1234) is not a passed
    check: the deck, the write-up and both package pages say "reported, not checked" and do not
    count it in the validation level; the engine's and the person's do count; an entry without a
    source (a file from before D1234) is reported, not checked, as study.checked has it (P10)."""
    from nagasim import studypackage, studypage

    folder, st = nurse_study
    st["validation"] = [{"level": "known_answer", "passed": True, "text": "my arithmetic", "source": "assistant"}]
    html = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    assert "reported, not checked" in html and "Validation: known answer" not in html
    assert studypage._validation_level(st) == "none recorded"
    studypackage.package(folder, st)
    for page in ("client.html", "control-room.html"):
        text = (folder / "package" / page).read_text(encoding="utf-8")
        assert "reported, not checked" in text, page
    for source in ("engine", "person"):
        entry = {"level": "known_answer", "passed": True, "text": "formula"}
        if source:
            entry["source"] = source
        st["validation"] = [entry]
        assert studypage._validation_level(st) == "known answer", source
        assert "reported, not checked" not in studywriteup.slides(folder, st).read_text(encoding="utf-8")
    st["validation"] = [{"level": "known_answer", "passed": True, "text": "formula"}]
    assert studypage._validation_level(st) == "none recorded"


def test_run_noise_row_speaks_the_pages_wording(nurse_study):
    """Tier review: the deck's Run noise row printed the raw fractions ("... 0.05958 (target 0.02851)")
    where the client page says percentage points; every surface uses the one wording (D1236)."""
    from nagasim import studypage
    folder, st = nurse_study
    steps = {v["id"]: studypage._read(folder / "versions" / v["id"] / "step.json") or {} for v in st["versions"]}
    noise = dict(studypage.trust_rows(folder, st, steps))["Run noise"]
    assert "3.6 points (target 2 points)" in noise and "0.036" not in noise
    assert "3.6 points (target 2 points)" in studypage._precision_line(st, steps)
    deck = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    assert "0.036" not in deck and "3.6 points" in deck


def test_old_validation_is_reported_with_a_revalidate_offer(nurse_study):
    """D1253 (journeys item 5): an entry from before D1234 (no source) still does not pass, and
    every page that shows the checklist says "reported, not checked" and offers the engine's check
    where it applies: the call on a static page, a Re-validate button in the room."""
    from nagasim import studyapp, studypage, studyroom

    folder, st = nurse_study
    st["validation"] = [{"level": "known_answer", "passed": True, "text": "queueing formula"}]
    assert study_mod.validation_state(st) == "reported" and not study_mod.sufficiency(st)["sufficient"]
    item = next(c for c in study_mod.checklist(st, folder) if c["item"] == "validation")
    assert item["verdict"] == "not_verified" and item["why"].startswith(study_mod.REPORTED)
    assert not study_mod.can_revalidate(st, folder)          # a Delay: the queue formula does not cover it
    assert "study_validate(" not in studypage.render(folder, st, study_id="s")
    shutil.copy(MODELS / "single_server.json", folder / "versions" / st["versions"][-1]["id"] / "model.json")
    assert study_mod.can_revalidate(st, folder)
    call = 'study_validate(study=&quot;s&quot;)'
    room = studypage.render(folder, st, study_id="s", in_room=True)
    assert "data-revalidate" in room and call in room and study_mod.REPORTED in room
    client = studypage.render_client(folder, st, [])      # no id known: the tool and the room, no guess
    assert "study_validate on this study" in client and "naga room s" in client and study_mod.REPORTED in client
    assert "study_validate" in studyroom.DIRECT and "[data-revalidate]" in studyapp.room_html()
    for state, entry in (("checked", {"source": "engine"}), ("failed", {"passed": False}), ("none", None)):
        st["validation"] = [] if entry is None else [{"level": "data", "passed": True, **entry}]
        assert study_mod.validation_state(st) == state and not study_mod.can_revalidate(st, folder)
    assert studypage.validation_result is study_mod.validation_result      # one rule, not a copy


CENSORED = {"name": "censoring", "verdict": "partial", "law": "censoring",
            "why": "30% of created entities are still inside at the end: time-based KPIs are biased low"}
OVERLOADED = {"name": "offered load: Nurse", "verdict": "partial", "law": "offered load",
              "why": "offered load 1.20 >= 1 on Nurse: unstable by design, the queue grows for the whole run"}
CAVEAT = "may be biased low: 30% of created entities are still inside at the end"


def test_1880_an_option_that_is_not_valid_is_written_as_not_valid_with_why(nurse_study):
    """#1880: an option whose law checks warned (study_step stores its "laws" and not_verified
    goals) reads "not valid" and why on every page built from the scenario rows, never its number;
    the valid options read as before."""
    from nagasim import studypage
    folder, st = nurse_study
    kiosk = st["versions"][-1]["scenarios"]["Kiosk"]
    kiosk.update(laws={"passed": 1, "partial": [OVERLOADED]}, goals={"g1": "not_verified"})
    _html, md = studywriteup.writeup(folder, st)
    lines = [line for line in md.read_text(encoding="utf-8").splitlines() if "Kiosk" in line]
    assert lines and all("not valid: Nurse gets more work than it can do" in line and "25.8" not in line
                         for line in lines), lines
    assert "| Fourth nurse | 5.4%" in md.read_text(encoding="utf-8")
    deck = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    assert "not valid" in deck and "25.8" not in deck
    room = studypage.render(folder, st, study_id="s")
    assert "Kiosk is not valid" in room and "25.8" not in room


def test_d1316_an_option_that_may_be_biased_low_keeps_its_numbers_with_the_caveat(nurse_study):
    """D1316: censoring alone does not hide an option's result: every page built from the scenario
    rows shows its number with the caveat in plain words, and its verdict as the step judged it."""
    from nagasim import studypage
    folder, st = nurse_study
    kiosk = st["versions"][-1]["scenarios"]["Kiosk"]
    kiosk.update(laws={"passed": 1, "partial": [CENSORED]}, goals={"g1": "not_met"})
    _html, md = studywriteup.writeup(folder, st)
    lines = [line for line in md.read_text(encoding="utf-8").splitlines() if "| Kiosk" in line]
    assert lines and all("25.8" in line and CAVEAT in line and "not valid" not in line for line in lines), lines
    deck = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    assert CAVEAT in deck and "not valid" not in deck
    room = studypage.render(folder, st, study_id="s")
    assert "Kiosk meets 0 of 1 (may be biased low)" in room and CAVEAT in room and "25.8" in room


def test_d1316_a_baseline_that_may_be_biased_low_keeps_its_number_and_a_verdict_it_cannot_reverse(nurse_study):
    """D1316: the Baseline's censoring alone leaves its number beside the caveat; its goal, already
    over its upper limit, stays not met."""
    folder, st = nurse_study
    step_file = folder / "versions" / st["versions"][-1]["id"] / "step.json"
    step = json.loads(step_file.read_text(encoding="utf-8"))
    step["checks"].append({**{k: CENSORED[k] for k in ("name", "verdict", "why")}, "kind": "law"})
    step_file.write_text(json.dumps(step), encoding="utf-8")
    facts = studywriteup.facts(folder, st)
    assert not facts["base_invalid"]
    g = facts["goals"][0]
    assert g["num"] is not None and CAVEAT in g["interval"] and g["status"] == "not_met"


def test_1880_a_baseline_that_is_not_valid_is_written_as_not_valid_with_why(nurse_study):
    """#1880: the Baseline's own law warnings (the step's partial law checks) replace its number
    in the write-up, and the checklist's verification row names them instead of an empty reason."""
    folder, st = nurse_study
    step_file = folder / "versions" / st["versions"][-1]["id"] / "step.json"
    step = json.loads(step_file.read_text(encoding="utf-8"))
    step["checks"].append({"name": "offered load: Nurse", "verdict": "partial", "why": "offered load 1.2 >= 1",
                           "kind": "law"})
    step_file.write_text(json.dumps(step), encoding="utf-8")
    st["versions"][-1]["verification"] = "partial"
    _html, md = studywriteup.writeup(folder, st)
    text = md.read_text(encoding="utf-8")
    why = "not valid: Nurse gets more work than it can do"
    abstract = text[text.index("## Abstract"):text.index("## 1 ")]
    assert why in abstract and "27.6" not in abstract, abstract
    assert re.search(r"^\| The model does what was intended \| partial \| " + why, text, re.M), text


def test_goals_lead_with_the_recommended_option(nurse_study):
    """D1252 (journeys item 14): the room's goal chips and the client page's Goals table show the
    recommended option's value and verdict, the Baseline's as the comparison; with no option
    recommended they show the Baseline as before."""
    from nagasim import studypage
    from nagasim.pagefmt import REFERENCE

    folder, st = nurse_study
    room = studypage.render(folder, st, study_id="s", in_room=True)
    chips = re.search(r'<div class="chips">(.*?)</div>', room).group(1)
    assert 'class="chip met"' in chips and "with Fourth nurse" in chips
    assert f"({REFERENCE} " in chips and ": not met)" in chips
    assert f"met in the {REFERENCE}, 1 with Fourth nurse" in room          # the Goals panel's summary
    client = studypage.render_client(folder, st, [])
    goals = client[client.index("<h2>Goals</h2>"):client.index("How far to trust it")]
    assert "<th>Fourth nurse value</th>" in goals and f"<th>{REFERENCE} value</th>" in goals
    assert '<span class="badge met">' in goals and "not met" in goals
    shutil.rmtree(folder / "versions" / st["versions"][-1]["id"], ignore_errors=True)
    st["versions"][-1]["scenarios"] = {}                                    # no option recommended
    plain = studypage.render(folder, st, study_id="s")
    assert "with Fourth nurse" not in plain and 'class="chip not_met"' in plain


def test_d1348_two_options_that_meet_every_goal_are_both_named_and_neither_leads(tmp_path):
    """D1348: with two options meeting every goal the write-up recommended the first in list
    order; now both are named, the choice is left to the decision maker, and neither leads the
    Goals table, the headline, the room's chips or the client page."""
    from conftest import make_nurse_study
    from nagasim import studypage

    folder, st = make_nurse_study(tmp_path / "s")
    kiosk = st["versions"][-1]["scenarios"]["Kiosk"]
    kiosk["kpis"]["Nurse/Share waiting over 15"] = {"mean": 0.07, "ci_half_width": 0.01}
    kiosk["goals"] = {"g1": "met"}
    assert study_mod.scenarios_meeting(st) == ["Fourth nurse", "Kiosk"]
    f = studywriteup.facts(folder, st)
    assert "Fourth nurse and Kiosk meet" in f["verdict"] and "decision maker" in f["verdict"]
    assert f["best"] == [] and f["tied"] == ["Fourth nurse", "Kiosk"]
    assert all(g["option"] is None for g in f["goals"]) and studywriteup._headline(f) == ""
    room = studypage.render(folder, st, study_id="s", in_room=True)
    assert "with Fourth nurse" not in room and "decision maker" in room
    client = studypage.render_client(folder, st, [])
    assert "<th>Fourth nurse value</th>" not in client


_GOAL = {"id": "g1", "text": "wait under 20", "kpi": "Wait", "high": 20}


def _said(sc=None, base=None, base_bad="", base_cav="", goals=(_GOAL,)):
    """The write-up's plain answer for one goal, from options `sc` or the Baseline (mean, half-width)."""
    mean, hw = base or (None, None)
    return studywriteup._verdict({"goals": list(goals)}, {"kpis": {"Wait": mean}}, {"Wait": {"half_width": hw}},
                                 sc or {}, [n for n in sc or {}], base_bad, base_cav)


def _option(mean, hw, partial=()):
    return {"kpis": {"Wait": {"mean": mean, "ci_half_width": hw}}, **({"laws": {"partial": list(partial)}} if partial else {})}


def test_the_answer_says_yes_no_or_not_clear_yet_from_the_interval():
    """#1881, D1309: the whole interval inside the goal is yes, wholly outside no, across it not
    clear yet; with options the answer is about them, else about the Baseline."""
    assert _said({"More": _option(15, 1)}) == "Yes: More meets the goal."
    # Reads right for "which ...?" too: what meets the goal, and what does not.
    assert _said({"Nurse": _option(5, 1), "Kiosk": _option(28, 2)}) == "Yes: Nurse meets the goal."
    assert _said({"More": _option(25, 1), "Double": _option(40, 2)}) == "No: none of the options meets the goal."
    assert _said({"More": _option(19.5, 1)}) == "Not clear yet: no option is clearly within the goal."
    assert _said({"Slow": _option(5, 1, [{"law": "offered load", "why": "x"}])}).startswith("Not clear yet")
    assert _said(base=(10, 1)) == "Yes." and _said(base=(30, 1)) == "No."
    assert _said(base=(10, 1), base_bad="not valid: x").startswith("Not clear yet")
    assert _said(base=(10, 1), goals=[{"id": "g1", "text": "t", "kpi": "Wait"}]).startswith("Not clear yet: no goal")


def test_a_low_bias_keeps_only_the_answer_it_cannot_reverse():
    """D1316: censoring may only bias low, so a value over the limit stays no; one under it is
    not clear yet."""
    low = [{"law": "censoring", "why": "30% still inside: x"}]
    assert _said({"Double": _option(40, 2, low)}) == "No: none of the options meets the goal."
    assert _said({"Double": _option(10, 2, low)}).startswith("Not clear yet")
    assert _said(base=(10, 1), base_cav="may be biased low: x").startswith("Not clear yet")


def test_the_glossary_explains_each_word_the_text_uses():
    """#1882: a term the text uses gets "term (meaning)"; one it does not use is left out."""
    used = studywriteup.glossary("10 replications, seed 0. | arrivals | CREATE |")
    assert [u.split(" (")[0] for u in used] == ["replication", "seed", "CREATE"]
    assert studywriteup.glossary("a created entity") == []        # block types match as written
