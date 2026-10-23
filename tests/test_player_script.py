"""The player's script (PHASE7 7.7, D197): its pure half, run in a JavaScript engine.

The script (`player.SCRIPT`) has two halves. The pure one says which sample a time
falls in and what may happen between two samples (hold a count, blend a position or a
level); it is tested here under `node`, with no browser and no dependency. The page
half runs only where there is a `document`: it is read by eye and was opened in a
browser when the plan was written. Skipped where `node` is not installed.
"""
import bisect
import re
import json
import shutil
import subprocess

import pytest

from nagasim import player
from nagasim.core import tape
from nagasim.experiments import BASELINE
from nagasim.core.trace import TraceEvent
from test_tape import recorded

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_script(calls: str, **data):
    """Evaluate *calls* (a JavaScript expression) after the player's script, with each
    of *data* as a variable; returns its value. There is no `document`, so the page
    half does not start."""
    program = (player.SCRIPT + "\n"
               + "".join(f"var {name} = {json.dumps(value)};\n" for name, value in data.items())
               + f"console.log(JSON.stringify({calls}));\n")
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# Three samples at t = 0, 10, 20. Token 1 is in all three, token 2 only in the first,
# token 3 only from the second on; the vehicle turns; the agent moves; the level rises.
RUN = {"t": [0.0, 10.0, 20.0], "frames": [
    {"n": [1, 0], "s": [1, 0], "k": [1, 0], "st": ["busy", ""], "lv": {"0": 2.0},
     "tok": [[1, 0, 0.0, 0.0], [2, 0, 5.0, 5.0]], "veh": [[0, 0, 0.0, 0.0, 90.0, "loaded"]],
     "ag": [[0, 7, 1.0, 1.0, -1]], "tokx": 3},
    {"n": [0, 2], "s": [0, 1], "k": [2, 0], "lv": {"0": 4.0, "1": 9.0},
     "tok": [[1, 0, 10.0, 20.0], [3, 1, 7.0, 7.0]], "veh": [[0, 0, 10.0, 0.0, 0.0, "empty"]],
     "ag": [[0, 7, 3.0, 5.0, 2]], "mv": [[0, 0, 1]], "q": {"1": [[0], [1]]}},
    {"n": [0, 0], "s": [0, 0], "k": [2, 2], "lv": {"0": 4.0, "1": 1.0}, "mvx": 300}]}


def test_between_two_samples_counts_hold_and_positions_blend():
    at = run_script("[0, 5, 9.999, 10, 15, 20, 99, -3].map(function (t) { return frameAt(RUN, t); })",
                    RUN=RUN)
    start, middle, late, second, after, end, beyond, before = at
    # Hold: every discrete value is the earlier sample's, to the last instant of the step.
    for frame in (start, middle, late):
        assert (frame["k"], frame["n"], frame["s"], frame["kpi"]) == (0, [1, 0], [1, 0], [1, 0])
        assert frame["st"] == ["busy", ""] and frame["q"] == {} and frame["more"] == 3
    assert (second["k"], second["f"], second["n"], second["kpi"]) == (1, 0, [0, 2], [2, 0])
    assert second["q"] == {"1": [[0], [1]]} and second["st"] == [] and second["more"] == 0
    # Blend: half way between its two places; the level on the line between its two values.
    assert (middle["f"], middle["tok"][0]) == (0.5, [1, 0, 5.0, 10.0])
    assert middle["veh"] == [[0, 0, 5.0, 0.0, 90.0, "loaded"]]     # heading and state hold
    assert middle["ag"] == [[0, 7, 2.0, 3.0, -1]] and middle["lv"] == {"0": 3.0}
    assert late["tok"][0][2:] == pytest.approx([9.999, 19.998])
    # Vanish: one only in the earlier sample stays where it was, then is gone.
    assert middle["tok"][1] == [2, 0, 5.0, 5.0] and late["tok"][1] == [2, 0, 5.0, 5.0]
    assert [entry[0] for entry in second["tok"]] == [1, 3]
    # Appear: one only in the later sample is not drawn before that sample.
    assert [entry[0] for entry in middle["tok"]] == [1, 2]
    assert after["tok"] == [[1, 0, 10.0, 20.0], [3, 1, 7.0, 7.0]] and after["veh"][0][2] == 10.0
    assert after["lv"] == {"0": 4.0, "1": 5.0} and start["lv"] == {"0": 2.0}
    # The ends: the last sample is itself, and a time outside the run is the nearest end.
    assert (end["k"], end["f"], end["n"], end["tok"], end["lv"]) == (2, 0, [0, 0], [], {"0": 4.0, "1": 1.0})
    assert end["skipped"] == 300 and start["skipped"] == 0       # the moves the tape counted, not listed
    assert (beyond["k"], beyond["f"]) == (2, 0) and (before["k"], before["f"]) == (0, 0)
    # Nothing is changed in place: the tape is the same after as before.
    assert run_script("(frameAt(RUN, 5), RUN)", RUN=RUN) == RUN


def test_the_sample_of_a_time_is_the_last_one_at_or_before_it():
    uneven = [0.0, 0.1, 0.2, 0.30000000000000004, 0.4, 5.0]
    asked = [0.0, 0.05, 0.1, 0.29, 0.3, 0.30000000000000004, 0.39, 4.99, 5.0, 6.0]
    got = run_script("ASKED.map(function (t) { return sampleIndex(TIMES, t); })",
                     TIMES=uneven, ASKED=asked)
    assert got == [max(bisect.bisect_right(uneven, t) - 1, 0) for t in asked]
    assert run_script("[sampleIndex([0], 3), sampleIndex([0, 0], 0)]") == [0, 0]


def test_a_step_goes_to_the_next_sample_and_stops_at_the_ends():
    times = [0.0, 10.0, 10.0, 20.0]
    got = run_script("[[0, 1], [5, 1], [10, 1], [20, 1], [25, 1], [5, -1], [10, -1], [20, -1],"
                     " [0, -1]].map(function (a) { return stepTime(TIMES, a[0], a[1]); })", TIMES=times)
    assert got == [10.0, 10.0, 20.0, 20.0, 25.0, 0.0, 0.0, 10.0, 0.0]
    assert run_script("stepTime([0], 0, 1)") == 0


def test_a_real_tape_is_read_as_python_reads_it():
    layout, recorder = recorded("warehouse_forklifts.json")
    run = tape.TapeWriter(layout, "layout").run(recorder, 120)
    times = run["t"]
    asked = [times[-1] * share for share in (0.0, 0.013, 0.25, 0.4999, 0.5, 0.777, 0.9999, 1.0)]
    for t, frame in zip(asked, run_script(
            "ASKED.map(function (t) { return frameAt(RUN, t); })", RUN=run, ASKED=asked)):
        k = max(bisect.bisect_right(times, t) - 1, 0)
        earlier, later = run["frames"][k], run["frames"][min(k + 1, len(times) - 1)]
        share = (t - times[k]) / (times[k + 1] - times[k]) if k + 1 < len(times) else 0
        assert frame["k"] == k and frame["f"] == pytest.approx(share)
        assert (frame["n"], frame["s"], frame["kpi"]) == (earlier["n"], earlier["s"], earlier["k"])
        assert frame["r"] == earlier.get("r", []) and frame["u"] == earlier.get("u", {})
        where = {tuple(entry[:2]): entry for entry in later.get("veh", [])}
        assert len(frame["veh"]) == len(earlier.get("veh", []))
        for got, was in zip(frame["veh"], earlier.get("veh", [])):
            to = where.get(tuple(was[:2]), was)
            assert got[2:4] == pytest.approx([was[2] + (to[2] - was[2]) * share,
                                              was[3] + (to[3] - was[3]) * share])
            assert got[4:] == was[4:]


def test_the_scene_fits_the_canvas_even_when_it_is_a_line_or_a_point():
    wide, line, point = run_script(
        "[fit([0, 0, 200, 100], 600, 400), fit([0, 5, 60, 5], 600, 400), fit([3, 3, 3, 3], 600, 400)]")
    assert wide["scale"] == pytest.approx(2.64)             # 36 px of margin each side
    assert wide["x"] + 100 * wide["scale"] == pytest.approx(300)
    assert wide["y"] + 50 * wide["scale"] == pytest.approx(200)
    assert line["scale"] == pytest.approx(528 / 60) and line["y"] + 5 * line["scale"] == pytest.approx(200, abs=1e-3)
    assert point["x"] + 3 * point["scale"] == pytest.approx(300, abs=1e-3)


def test_the_script_of_a_written_file_is_the_script_tested_here(tmp_path):
    out = tmp_path / "run.html"
    player.build_player({}, {BASELINE: []}, path=out)
    page = out.read_text(encoding="ascii")
    start = page.index("<script>") + len("<script>")
    assert page[start:page.index("</script>", start)] == player.SCRIPT
    assert run_script("typeof boot") == "function"


# -- the page half, against a stub of the few parts of the DOM it touches --------------
# A real browser is not here: this stub is enough to run boot, draw and panel end to end,
# and to record what the script makes (elements, `src`, text) so that a test can pin it.
STUB = r"""
"use strict";
var made = [], sources = [], banned = [], titles = [], frames = [], listeners = {}, nodes = {};
var queue = [], wall = 0;
function Node(tag, id) {
  this.tag = tag; this.id = id; this.children = []; this.style = {}; this.hidden = false;
  this.className = ""; this.value = ""; this._text = ""; this.clientWidth = 640; this.clientHeight = 400;
  this.width = 0; this.height = 0; this.dataset = {}; this.title = "";
}
Node.prototype = {
  appendChild: function (child) { this.children.push(child); return child; },
  removeChild: function (child) { this.children.splice(this.children.indexOf(child), 1); },
  addEventListener: function (type, fn) { listeners[this.id + ":" + type] = fn; },
  getContext: function () { return new Proxy({}, {get: function (target, name) {
                                                    return name === "measureText" ? function () { return {width: 30}; } : function () {}; },
                                                  set: function () { return true; }}); },
  get firstChild() { return this.children[0] || null; },
  get textContent() { return this._text; },
  set textContent(value) { this._text = String(value); this.children = []; },
  set innerHTML(value) { banned.push("innerHTML"); },
  set outerHTML(value) { banned.push("outerHTML"); }
};
var document = {
  activeElement: null,
  set title(value) { titles.push(String(value)); },
  createElement: function (tag) { made.push(tag); return new Node(tag, ""); },
  getElementById: function (id) {
    if (!nodes[id]) { nodes[id] = new Node("page", id); }
    return nodes[id];
  }
};
var window = {devicePixelRatio: 1, addEventListener: function (type, fn) { listeners["window:" + type] = fn; },
              requestAnimationFrame: function (fn) { queue.push(fn); },
              getComputedStyle: function () { return {getPropertyValue: function (name) { return " " + name + " "; }}; }};
document.documentElement = new Node("html", "");
function Image() { this.complete = true; this.naturalWidth = 10; }
Object.defineProperty(Image.prototype, "src", {set: function (value) { sources.push(value); }});
function flush(rounds) {
  var round, pending;
  for (round = 0; round < rounds; round += 1) {
    pending = queue; queue = []; wall += 300;
    pending.forEach(function (fn) { fn(wall); });
  }
}
function fire(id, type, event) { listeners[id + ":" + type](event); }
nodes.tape = new Node("script", "tape");
nodes.tape._text = DATA_TEXT;
"""

DRIVER = """
flush(3);
nodes.seek.value = 500; fire("seek", "input"); flush(3);
fire("play", "click"); flush(40);
nodes.speed.value = String(SPEEDS[SPEEDS.length - 1]); fire("speed", "change"); flush(10);
fire("play", "click"); fire("play", "click"); flush(5);
for (var i = 1; i < SCENARIOS.length; i += 1) {
  fire("tabs", "click", {target: {value: SCENARIOS[i]}}); flush(8);
}
fire("tabs", "click", {target: {value: "constructor"}}); fire("tabs", "click", {}); flush(2);
var themes = [];
for (var i = 0; i < 3; i += 1) { fire("theme", "click"); themes.push(nodes.theme.textContent); }
fire("lanes", "pointerdown", {offsetX: 300, pointerId: 1}); fire("lanes", "pointermove", {offsetX: 400});
fire("lanes", "pointerup"); fire("lanes", "pointermove", {offsetX: 100}); flush(2);
var dragged = Number(nodes.seek.value);
nodes.seek.value = 0; fire("seek", "input"); flush(2);
var at = [];
fire("step", "click"); flush(2); at.push(Number(nodes.seek.value));
fire("play", "click"); fire("step", "click"); flush(5); at.push(Number(nodes.seek.value), nodes.play.textContent);
fire("back", "click"); flush(2); at.push(Number(nodes.seek.value));
nodes.seek.value = 1000; fire("seek", "input"); flush(2);
fire("step", "click"); flush(2); at.push(Number(nodes.seek.value));
console.log(JSON.stringify({stepped: at, made: made.filter(function (tag, i) { return made.indexOf(tag) === i; }).sort(),
                            sources: sources, banned: banned, titles: titles, themes: themes,
                            dragged: dragged, tabs: nodes.tabs.children.map(function (tab) { return tab.className; })}));
"""


def run_page(page: str) -> dict:
    """Run the written page's script against the stub: boot, seek, play, switch scenario."""
    match = re.search(r'<script type="application/json" id="tape">(.*?)</script>', page, re.S)
    data = json.loads(match.group(1))
    program = (f"var DATA_TEXT = {json.dumps(match.group(1))};\nvar SCENARIOS = {json.dumps(data['scenarios'])};\n"
               + STUB + player.SCRIPT + "\n" + DRIVER)
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert report["banned"] == []
    first, second, play, back, end = report["stepped"]
    # One sample forward, then another (pausing a playing page), one back, none past the end.
    assert 0 < first < second and play == "Play" and back == first and end == 1000
    assert set(report["made"]) <= {"button", "option", "tr", "th", "td", "div", "span"}
    # The theme switch goes round, and works with no storage (the stub has none).
    assert report["themes"] == ["Theme: light", "Theme: dark", "Theme: auto"]
    # A drag on the lanes seeks to where it was let go, not where the pointer went after.
    left, width = 92, 640 - 8 - 92
    assert report["dragged"] == round(1000 * (400 - left) / width)
    assert set(report["sources"]) <= set(data["pics"])      # no `src` but a picture of the data
    assert report["titles"] == [data["title"]]
    return report


def test_the_page_half_runs_a_benign_study_end_to_end():
    layout, recorder = recorded("warehouse_forklifts.json")
    other = recorded("two_cell_line.json")[1]
    page = player.build_player(layout, {BASELINE: recorder, "Again": other}, title="forklifts",
                               view="layout", samples=250)
    report = run_page(page)
    assert {"button", "option", "tr", "th", "td", "div", "span"} >= set(report["made"]) >= {"button", "option", "span"}
    assert report["tabs"] == ["tab base", "tab on"]            # the scenario last chosen


def test_names_that_are_members_of_every_object_do_not_blank_the_page():
    """B1: a block, state, fleet or scenario named `constructor` or `__proto__` must be
    a name, not the member every JavaScript object inherits."""
    safe = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            '<rect width="24" height="24"/></svg>')
    layout = {"blocks": [
        {"name": "constructor", "description": "", "type": "CREATE", "position": [0, 0],
         "params": {"node": "n1"}},
        {"name": "__proto__", "description": "", "type": "PROCESS", "position": [200, 0],
         "params": {"resource_name": "toString"}},
        {"name": "hasOwnProperty", "description": "", "type": "PROCESS", "position": [400, 0],
         "params": {}}],
        "links": [{"source": "constructor", "target": "__proto__"}],
        "entities": [{"name": "valueOf", "shape": "square", "color": "RED"}],
        "levels": [{"name": "constructor", "capacity": 10}]}
    events = [
        TraceEvent(0.0, "resource_state", -1, "", "", None, resource="toString", state="capacity", value=1),
        TraceEvent(0.0, "level_changed", -1, "", "", None, resource="constructor", value=1.0),
        TraceEvent(1.0, "entity_created", 1, "valueOf", "constructor", None),
        TraceEvent(1.0, "entity_moved", 1, "valueOf", "constructor", "__proto__"),
        TraceEvent(1.0, "entity_seized", 1, "valueOf", "__proto__", "toString"),
        TraceEvent(2.0, "mover_moved", 0, "", "", "constructor", resource="constructor",
                   state="__proto__", duration=1.0, path=("n1",)),
        TraceEvent(3.0, "level_changed", -1, "", "", None, resource="constructor", value=6.0),
        TraceEvent(5.0, "entity_moved", 1, "valueOf", "__proto__", "hasOwnProperty", duration=2.0),
        TraceEvent(8.0, "entity_disposed", 1, "valueOf", "hasOwnProperty", None)]
    pictures = {"blocks": {"__proto__": {"size": (160, 80), "states": {"constructor": safe}}},
                "entities": {"VALUEOF": safe}, "fleets": {"__proto__": safe},
                "fills": {"toString": ("__proto__", 5)}}
    page = player.build_player(layout, {"constructor": events, "__proto__": events},
                               title="constructor", view="schematic", pictures=pictures,
                               samples=250)
    run_page(page)


# -- the look (D697) ------------------------------------------------------------------
def test_a_block_takes_its_place_in_the_flow():
    blocks = [{"name": "in"}, {"name": "work", "q": 1}, {"name": "decide"}, {"name": "out"},
              {"name": "alone"}]
    links = [[0, 1], [1, 2], [2, 3]]
    assert run_script("roles(B, L)", B=blocks, L=links) == ["source", "work", "block", "sink", "block"]


def test_states_are_quiet_when_normal_and_loud_with_a_word_when_not():
    """D705: running, idle, blocked, down; only the last two carry a word."""
    kinds = run_script('["busy", "loaded", "handling", "idle", "off", "empty", "blocked", "down", "", "constructor"]'
                       ".map(kindOf)")
    assert kinds == ["run", "run", "run", "idle", "idle", "idle", "blocked", "down", "", "idle"]
    assert run_script("WORD") == {"blocked": "BLOCKED", "down": "DOWN"}


def test_the_lanes_are_each_resources_states_from_the_tape():
    """D708: one lane per resource, spans joined while the state holds, a mark at each
    failure; a resource the tape does not list yet has no span."""
    tape_run = {"t": [0, 10, 20, 30, 40], "scene": {"resources": ["M1", "M2"]}, "frames": [
        {"r": [[0, "busy", 1, 1]]},
        {"r": [[0, "busy", 1, 1], [1, "idle", 0, 1]]},
        {"r": [[0, "down", 0, 1], [1, "blocked", 1, 1]]},
        {"r": [[0, "busy", 1, 1], [1, "off", 0, 1]]},
        {"r": [[0, "down", 0, 1], [1, "idle", 0, 1]]}]}
    m1, m2 = run_script("lanes(R)", R=tape_run)
    assert m1 == {"name": "M1", "spans": [[0, 20, "run"], [20, 30, "down"], [30, 40, "run"], [40, 40, "down"]],
                  "fails": [20, 40]}
    assert m2 == {"name": "M2", "spans": [[10, 20, "idle"], [20, 30, "blocked"], [30, 40, "idle"]], "fails": []}


def test_a_working_block_shows_its_share_of_the_time_so_far_and_a_long_queue_its_badge():
    """G2 polish: the % inside a block is the time its lamp said running over the time so
    far; a block with no lamp shows none. The first FEW waiting stand before it, the badge counts all."""
    tape_run = {"t": [0, 10, 20, 40], "scene": {"blocks": [{}, {}, {}]}, "frames": [
        {"st": ["busy", "down", ""]}, {"st": ["idle", "busy", ""]}, {"st": ["busy", "busy", ""]}, {}]}
    assert run_script("utilisation(R)", R=tape_run) == [[0, 1, 0.5, 0.75], [0, 0, 0.5, 0.75], None]
    assert run_script("FEW") == 6


SHOW = """
function texts(node) { return node.children.length ? node.children.map(texts) : node.textContent; }
function classes(node) { return node.children.map(function (c) { return c.className; }); }
"""


def run_panels(data, then=""):
    """The page half on *data* against the stub; returns what the side panels show."""
    program = (f"var DATA_TEXT = {json.dumps(json.dumps(data))};\n" + STUB + player.SCRIPT + SHOW
               + "\nflush(2);\n" + then + "\nconsole.log(JSON.stringify({"
               "results: nodes.results.children.map(function (row) { return [classes(row), texts(row)]; }),"
               "tracks: nodes.results.children.map(function (row) { return classes(row.children[1]); }),"
               "trust: texts(nodes.trust), neck: [nodes.neck.hidden, texts(nodes.neck)],"
               "hidden: [nodes['results-box'].hidden, nodes['trust-box'].hidden]}));\n")
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_the_side_shows_the_results_with_words_the_trust_card_and_the_bottleneck():
    layout, recorder = recorded("two_cell_line.json")
    page = player.build_player(layout, {BASELINE: recorder, "Faster": recorder}, samples=50)
    data = json.loads(re.search(r'<script type="application/json" id="tape">(.*?)</script>', page, re.S).group(1))
    interval = {"mean": 2.5, "lo": 2.0, "hi": 3.0}
    data["results"] = {
        BASELINE: [{"kpi": "user/q", "label": "Queue", "unit": "parts", "yours": interval, "base": None,
                    "target": None, "better": "lower", "delta_pct": None, "verdict": None}],
        "Faster": [{"kpi": "user/q", "label": "Queue", "unit": "", "yours": {"mean": 1.25, "lo": 1.0, "hi": 1.5},
                    "base": interval, "target": 2, "better": "lower", "delta_pct": -50.0, "verdict": "better"},
                   {"kpi": "user/c", "label": "Cost", "unit": "", "yours": {"mean": 3, "lo": None, "hi": None},
                    "base": {"mean": 2.9, "lo": None, "hi": None}, "target": None, "better": None,
                    "delta_pct": 3.4483, "verdict": "no clear change"}]}
    data["trust"] = {BASELINE: {"laws_pass": 5, "laws_warn": 0, "warmup": 0, "replications": 1,
                                "half_width_pct": None},
                     "Faster": {"laws_pass": 4, "laws_warn": 2, "warmup": 60, "replications": 10,
                                "half_width_pct": 2.14}}
    data["runs"][BASELINE]["neck"] = {"block": 1, "wait": 3.04, "down": 0.41, "blocked": 0.0}
    data["runs"]["Faster"].pop("neck", None)
    label = data["runs"][BASELINE]["scene"]["blocks"][1]["label"]
    base = run_panels(data)
    assert base["results"] == [[["", "r-track", "r-num"], [["Queue", "parts"], ["", ""], ["2.5", ""]]]]  # the delta line kept (#841)
    assert base["tracks"] == [["r-range yours", "r-dot yours"]]          # no baseline, no target
    assert base["trust"] == [["Flow and Little's law checks", [chr(0x2713) + " ", "pass"]],
                             ["Warm-up removed", ["none"]], ["Replications, precision", ["1"]],
                             ["Input data", ["not measured"]], ["Parameters", ["not measured"]]]
    data["trust"][BASELINE].update(input_text="level, 40 resamples x 5 replications; a <b>x</b>",
                                   params_text="12 of 200 samples accepted, tolerance 0.3")
    again = run_panels(data)["trust"]
    assert again[3:] == [["Input data", ["level, 40 resamples x 5 replications; a <b>x</b>"]],
                         ["Parameters", ["12 of 200 samples accepted, tolerance 0.3"]]]   # as text, never markup
    assert base["neck"] == [False, [f"Bottleneck: {label}",
                                    "3 waiting on average; failures cause 41% of the waiting."]]
    faster = run_panels(data, 'fire("tabs", "click", {target: {value: "Faster"}}); flush(2);')
    assert [row[1] for row in faster["results"]] == [
        [["Queue"], [""] * 5, ["1.25", [chr(0x25bc), " -50% better"]]],
        [["Cost"], ["", ""], ["3", ["~", " +3.4% no clear change"]]]]
    assert faster["tracks"] == [["r-range base", "r-dot base", "r-range yours", "r-dot yours", "r-target"],
                                ["r-dot base", "r-dot yours"]]
    assert faster["trust"][0] == ["Flow and Little's law checks", [chr(0x26a0) + " ", "2 warnings"]]
    assert faster["trust"][1:] == [["Warm-up removed", ["60 minutes"]],
                                   ["Replications, precision", [f"10, {chr(0xb1)}2.1%"]],
                                   ["Input data", ["not measured"]], ["Parameters", ["not measured"]]]
    assert faster["neck"] == [True, ""] and faster["hidden"] == [False, False]
    # No results and no trust (a recording alone): both panels hidden, nothing empty shown.
    data["results"] = data["trust"] = None
    assert run_panels(data)["hidden"] == [True, True]
