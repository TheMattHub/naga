"""The player's script (PHASE7 7.7, D197): its pure half, run in a JavaScript engine.

The script (`player._SCRIPT`) has two halves. The pure one says which sample a time
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
    program = (player._SCRIPT + "\n"
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
    assert page[start:page.index("</script>", start)] == player._SCRIPT
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
  this.width = 0; this.height = 0;
}
Node.prototype = {
  appendChild: function (child) { this.children.push(child); return child; },
  removeChild: function (child) { this.children.splice(this.children.indexOf(child), 1); },
  addEventListener: function (type, fn) { listeners[this.id + ":" + type] = fn; },
  getContext: function () { return new Proxy({}, {get: function () { return function () {}; },
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
              requestAnimationFrame: function (fn) { queue.push(fn); }};
function Image() { this.complete = true; this.naturalWidth = 10; }
Object.defineProperty(Image.prototype, "src", {set: function (value) { sources.push(value); }});
function flush(rounds) {
  var round, pending;
  for (round = 0; round < rounds; round += 1) {
    pending = queue; queue = []; wall += 300;
    pending.forEach(function (fn) { fn(wall); });
  }
}
function fire(id, type) { listeners[id + ":" + type](); }
nodes.tape = new Node("script", "tape");
nodes.tape._text = DATA_TEXT;
"""

DRIVER = """
flush(3);
nodes.seek.value = 500; fire("seek", "input"); flush(3);
fire("play", "click"); flush(40);
nodes.speed.value = "8"; fire("speed", "change"); flush(10);
fire("play", "click"); fire("play", "click"); flush(5);
for (var i = 1; i < SCENARIOS.length; i += 1) {
  nodes.scenario.value = SCENARIOS[i]; fire("scenario", "change"); flush(8);
}
console.log(JSON.stringify({made: made.filter(function (tag, i) { return made.indexOf(tag) === i; }).sort(),
                            sources: sources, banned: banned, titles: titles}));
"""


def run_page(page: str) -> dict:
    """Run the written page's script against the stub: boot, seek, play, switch scenario."""
    match = re.search(r'<script type="application/json" id="tape">(.*?)</script>', page, re.S)
    data = json.loads(match.group(1))
    program = (f"var DATA_TEXT = {json.dumps(match.group(1))};\nvar SCENARIOS = {json.dumps(data['scenarios'])};\n"
               + STUB + player._SCRIPT + "\n" + DRIVER)
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert report["banned"] == []
    assert set(report["made"]) <= {"option", "tr", "th", "td", "div", "span"}
    assert set(report["sources"]) <= set(data["pics"])      # no `src` but a picture of the data
    assert report["titles"] == [data["title"]]
    return report


def test_the_page_half_runs_a_benign_study_end_to_end():
    layout, recorder = recorded("warehouse_forklifts.json")
    other = recorded("two_cell_line.json")[1]
    page = player.build_player(layout, {BASELINE: recorder, "Again": other}, title="forklifts",
                               view="layout", samples=250)
    report = run_page(page)
    assert {"option", "tr", "th", "td", "div", "span"} >= set(report["made"]) >= {"option", "span"}


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
