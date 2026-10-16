"""The monitor (PHASE7 7.13, D278-D287): `nagasim/monitor.py`, the step's events out of
the cycle, and the page a browser reloads. Clocks are injected; nothing here sleeps."""
import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from nagasim import live, monitor, player, twin
from nagasim.core.trace import TraceEvent
from nagasim.core.playback import Timeline
from test_twin_cycle import mock_plant, playing, slow_line


def test_a_step_hands_its_events_to_its_process_and_not_to_the_result(tmp_path):
    source, clock, events = playing(slow_line())
    cycle = twin.Cycle(source, horizon=12.0, replications=2, out_dir=tmp_path, runner=lambda *a, **k: 1 / 0,
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    assert cycle.events == []
    clock.mono = 6.5
    doc = cycle.step()
    first = list(cycle.events)
    clock.mono = 10.5
    cycle.step()
    # A second timeline fed with what each step handed over shows what the result shows.
    mine = Timeline([], live=True)
    mine.append(first + cycle.events)
    assert first and cycle.events and all(e.t > 6.5 for e in cycle.events)
    assert mine.state_at(mine.duration)["counts"] == Timeline(events).state_at(10.0)["counts"]
    assert "events" not in doc                      # D268: the result holds no tape
    clock.mono = 10.6
    cycle.step()
    assert cycle.events == []                       # a quiet step has none


class Opening:
    """A subscription that has just opened: what it holds comes at the first pull."""

    def __init__(self, rows, first):
        self.rows, self.first = rows, first

    def changes(self, source, cursor=None, limit=1000):
        since = int(cursor or 0)
        cut = self.first if since == 0 else len(self.rows)
        return live.Changes(tuple(self.rows[since:cut]), str(cut), 0, False, "live", 0.0, ())


def listened(tmp_path):
    """(a source that was listened to once before any step, its cycle): as `naga twin` and
    the editor do on a live source, so that the first state is the whole plant's."""
    model = slow_line()
    mapping, log, _events = mock_plant(model)
    log.insert(1, {"source": "plant", "key": "resource/Server/down", "value": True,
                   "source_time": 0.0, "received": 0.0, "seq": 1})
    source = twin.PlantSource(model, mapping, Opening(log, 7), name="line", sources=["plant"],
                              clock=lambda: 10.5)
    cycle = twin.Cycle(source, horizon=12.0, runner=lambda *a, **k: 1 / 0, every=5.0)
    source.observe()                                # the listen window: no step yet
    return model, source, cycle


def test_what_was_observed_before_the_first_step_reaches_the_views(tmp_path):
    model, source, cycle = listened(tmp_path)
    doc = cycle.step()
    kinds = [(e.kind, e.state) for e in cycle.events[:2]]
    assert kinds == [("resource_state", "capacity"), ("resource_state", "down")]
    page = monitor.Page(model, tmp_path / "m.html", every=5.0)
    data = data_of(page, page.build(doc, cycle.events))
    frame = data["runs"][data["title"]]["frames"][0]
    # The drawing has the capacity and the stoppage said during the listen window, as the
    # result's view has them.
    assert doc["view"]["resources"] == [{"name": "Server", "state": "down", "busy": 1,
                                         "capacity": 1.0}]
    assert frame["r"] == [[0, "down", 1, 1.0]]
    lines = [row[2] for row in data["monitor"]["events"]]
    assert lines[-2:] == ["Server down", "Server capacity 1"]
    assert cycle.step() and cycle.events == []      # handed over once


# ---------------------------------------------------------------- what a result says
def result(**changes):
    doc = {"format": 1, "kind": "naga-twin", "cycle": 12, "written": "2026-10-10T12:00:10+00:00",
           "started": "2026-10-10T11:00:00+00:00", "cadence": {"every": 5.0, "forecast_every": 300.0},
           "model": {"name": "line", "hash": "m" * 64, "time_unit": "minutes",
                     "epoch": "2026-10-10T00:00:00+00:00"},
           "at": 720.0, "stamp": "2026-10-10T12:00:00+00:00",
           "sources": [{"name": "plc", "state": "live", "age": 1.5, "lost": 0, "gaps": 0}],
           "view": {"approximate": False, "wip": 3}, "problems": [],
           "forecast": {"status": "ok", "state": "s" * 64, "model": "m" * 64, "seed": 7,
                        "from": 700.0, "stamp": "2026-10-10T11:40:00+00:00", "horizon": 60.0,
                        "replications": 50, "approximate": False,
                        "kpis": [{"kpi": "system/WIP", "mean": 4.0, "q05": 2.0, "q95": 6.0}],
                        "points": [
                            {"series": "system/WIP", "at": 710.0, "mean": 4.0, "q05": 2.0,
                             "q95": 6.0, "observed": 7.0, "observed_at": 710.5},
                            {"series": "system/WIP", "at": 730.0, "mean": 4.0, "q05": 2.0,
                             "q95": 6.0, "observed": None, "observed_at": None}]},
           "comparison": [{"series": "system/WIP", "at": 710.0, "inside": False}]}
    doc.update(changes)
    return doc


def said(code, message, severity="warning"):
    return {"code": code, "severity": severity, "message": message, "path": "/", "hint": ""}


def test_a_summary_says_the_forecast_against_what_was_observed_and_how_to_read_it():
    shown = monitor.summary(result())
    assert shown["head"] == "Plant time 720 minutes (2026-10-10T12:00:00+00:00); cycle 12"
    assert shown["notes"] == [] and shown["sources"] == [["plc", "live", 1.5, 0, 0]]
    forecast = shown["forecast"]
    assert forecast["points"] == [["system/WIP", 710.0, 4.0, 2.0, 6.0, 7.0, "no"],
                                  ["system/WIP", 730.0, 4.0, 2.0, 6.0, None, ""]]
    assert forecast["lines"][0] == "Status: ok"
    assert forecast["lines"][1].startswith("From t = 700 minutes") and "seed 7" in forecast["lines"][1]
    assert "1 compared points over the stored forecasts, 0 inside" in forecast["lines"][2]
    assert "title" not in shown                     # a result's name may be a file's
    assert forecast["lines"][-1] == monitor.IN_DISTRIBUTION


def test_a_missing_forecast_always_has_its_reason_and_a_limit_is_named():
    down = "source 'plc' is down or has not answered yet"
    skipped = monitor.summary(result(
        forecast={"status": "skipped", "reason": "W_TWIN_SOURCE"},
        problems=[said("W_TWIN_SOURCE", down), said("W_PLANT_GAP", "a hole")], comparison=[]))
    assert skipped["forecast"]["lines"] == ["Status: skipped", "No forecast was started: " + down]
    assert skipped["forecast"]["points"] == [] == skipped["forecast"]["kpis"]
    # An older forecast stays on screen, marked as old, beside why there is no new one.
    refused = monitor.summary(result(
        forecast={**result()["forecast"], "status": "refused", "reason": "E_TWIN_STATE"},
        problems=[said("E_TWIN_STATE", "this model cannot be started from plant state yet: "
                                       "fleets and vehicles", "error")]))
    lines = refused["forecast"]["lines"]
    assert lines[1] == ("Stated limit: this model is monitored, not forecast. this model cannot "
                        "be started from plant state yet: fleets and vehicles")
    assert lines[2].startswith("Shown: the last finished forecast. From t = 700")
    assert monitor.summary(result(forecast={"status": "none"}, comparison=[]))["forecast"]["lines"] \
        == ["Status: none", "No forecast yet."]
    # A reason nobody explained is still a reason on screen.
    assert monitor.summary(result(forecast={"status": "failed", "reason": "E_TWIN_FORECAST"},
                                  comparison=[]))["forecast"]["lines"] \
        == ["Status: failed", "The latest forecast failed: E_TWIN_FORECAST"]


def test_approximate_counts_are_labelled_and_a_damaged_result_is_still_a_summary():
    assert monitor.summary(result(view={"approximate": True}))["notes"] == [monitor.APPROXIMATE]
    assert "item events in the list are inferred from those counts" in monitor.APPROXIMATE
    for junk in ({}, None, {"forecast": 3, "sources": "x", "problems": [1, {"code": 2}],
                            "model": [], "at": float("nan"), "cadence": 5,
                            "comparison": [{"inside": "yes"}]}):
        shown = monitor.summary(junk)
        assert shown["forecast"]["status"] == "none"
        assert monitor.state(shown, 0.0) == (shown["state"]["fresh"].replace("{age}", "?"), False)
        json.dumps(shown, allow_nan=False)


def test_a_twin_is_stale_after_two_cadences_and_never_when_a_scheduler_steps_it():
    at = 1791633610.0
    shown = monitor.summary(result())
    assert live.stamp(shown["written"]) == at
    # The age is always said, and whose clock judges it.
    assert monitor.state(shown, at + 9.0) == (
        "Updated 9 s ago; the twin looks at the plant every 5 s.", False)
    assert monitor.state(shown, at + 11.0) == (
        "STALE: no update for 11 s, though the twin looks at the plant every 5 s. What is "
        "shown is old.", True)
    scheduled = monitor.summary(result(cadence={"every": None, "forecast_every": 0.0}))
    assert monitor.state(scheduled, at + 9999.0) == (
        "Updated 9999 s ago; stepped by a scheduler, so not watched for staleness.", False)


# ---------------------------------------------------------------- event lines
def test_the_event_lines_are_what_happened_to_items_resources_and_the_twin():
    source, clock, _events = playing(slow_line())
    cycle = twin.Cycle(source, horizon=12.0, replications=2, runner=lambda *a, **k: 1 / 0,
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    log = monitor.Log()
    clock.mono = 5.5
    log.add(cycle.step(), cycle.events)
    texts = [row[2] for row in log.rows]
    assert texts[:5] == ["Server capacity 1", "an item entered at c", "an item moved from c to p",
                         "an item entered at c", "an item moved from c to p"]
    assert "an item left from d" in texts
    # Then what the twin itself did, at the instant of the step.
    assert texts[-4:-1] == ["source plant: live", "forecast: failed (E_TWIN_FORECAST)",
                            "E_TWIN_FORECAST: the forecast failed (ZeroDivisionError)"]
    # This plant's clock starts at 1970-01-01: a log in model time, so no date (D20).
    assert list(log.rows[3][:2]) == [2.0, None] and log.rows[-1][0] == 5.5
    before = len(log.rows)
    clock.mono = 5.6
    log.add(cycle.step(), cycle.events)
    assert len(log.rows) == before                  # nothing new: nothing said twice


def test_the_event_lines_are_bounded_and_the_rest_is_counted():
    log = monitor.Log()
    burst = [TraceEvent(t=float(i), kind="entity_disposed", entity_id=i, e_type="Part",
                        block="Pack", target=None) for i in range(250)]
    for _ in range(3):
        log.add(result(), burst)
    assert len(log.rows) == monitor.KEPT and log.more == 3 * 150
    assert log.rows[-1][2] == "Part left from Pack" and log.rows[-1][0] == 249.0


def test_event_lines_are_plain_words_said_once_per_instant():
    """D20: inside one asset its steps, between two the assets; the same line at one
    instant is said once with how many times."""
    def moved(t, src, dst, e_type="ORDER", item=1):
        return TraceEvent(t=t, kind="entity_moved", entity_id=item, e_type=e_type, block=src,
                          target=dst)
    log = monitor.Log()
    log.add(result(), [moved(1.0, "pick_a/waited", "pick_a/pick", item=i) for i in range(3)]
            + [moved(1.0, "dock/drop", "dock/free", "PALLET", 9),    # one item, three steps
               moved(1.0, "dock/free", "zone_a/at_zone", "PALLET", 9),
               moved(1.0, "zone_a/at_zone", "zone_a/claim", "PALLET", 9), moved(2.0, "a", "b")])
    assert [row[2] for row in log.rows][:3] == [
        "3 x ORDER in pick_a: waited to pick", "PALLET moved from dock to zone_a",
        "ORDER moved from a to b"]


# ---------------------------------------------------------------- the page
HOSTILE = '</script><img src=x onerror=alert(1)>'


def page_of(tmp_path, every=5.0):
    """A page beside a twin on the mock plant at t = 10.5, with what it was written from."""
    source, clock, events = playing(slow_line())
    cycle = twin.Cycle(source, horizon=12.0, replications=2, every=every,
                       runner=lambda *a, **k: 1 / 0,
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    page = monitor.Page(slow_line(), tmp_path / monitor.PAGE_FILE, every=every, title="line")
    clock.mono = 10.5
    doc = cycle.step()
    return page, doc, cycle.events, events


def data_of(page, text):
    before, after = page.shell.split("__TAPE__")
    assert text.startswith(before) and text.endswith(after)
    return json.loads(text[len(before):len(text) - len(after)])


def test_the_page_is_the_present_as_the_result_has_it_and_the_summary(tmp_path):
    page, doc, step, events = page_of(tmp_path)
    page.write(doc, step)
    raw = (tmp_path / monitor.PAGE_FILE).read_bytes()
    assert b"\r" not in raw and sorted(p.name for p in tmp_path.iterdir()) == [monitor.PAGE_FILE]
    data = data_of(page, raw.decode("ascii"))
    run = data["runs"]["line"]
    assert data["scenarios"] == ["line"] and run["t"] == [10.0] and len(run["frames"]) == 1
    # One frame, drawn from the page's own timeline: the counts the result's view has.
    names = [block["name"] for block in run["scene"]["blocks"]]
    assert {name: n for name, n in zip(names, run["frames"][0]["n"]) if n} \
        == doc["view"]["blocks"] == {"p": 3}
    shown = data["monitor"]
    assert shown["summary"] == json.loads(json.dumps(monitor.summary(doc)))
    assert shown["tail"] == "Twin started 2027-01-15T08:00:00+00:00. This page reloads every 2 s."
    assert shown["more"] == 0
    named = dict(slow_line(), info={"name": "Bottling line 3"})
    titles = [json.loads(re.search(r'id="tape">(.*?)</script>', monitor.Page(
        model, tmp_path / "t.html").build(doc, []), re.S).group(1))["title"]
        for model in (named, slow_line())]
    assert titles == ["Bottling line 3", "Plant monitor"]       # never a file's name (D282)
    # Newest first.
    assert shown["events"][0][2].startswith(("W_", "E_"))
    assert shown["events"][-1][2] == "Server capacity 1"
    # The next step adds to the same page; nothing but the one file is ever there.
    page.write(doc, [])
    assert sorted(p.name for p in tmp_path.iterdir()) == [monitor.PAGE_FILE]


def test_the_page_asks_for_nothing_and_reloads_itself_at_half_the_cadence(tmp_path):
    page, doc, step, _events = page_of(tmp_path)
    text = page.build(doc, step)
    assert text.isascii() and "//" not in text
    policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', text)
    script = re.search(r"<script>(.*)</script>", text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    assert f"script-src 'sha256-{digest}'" in policy.group(1)
    assert policy.group(1).startswith("default-src 'none'; img-src data:;")
    assert script.endswith(player._SCRIPT) and "function monitor(" in script
    fixed = page.shell.replace(script, "")
    assert not re.search(r"https?:|ftp:|wss?:|@import|url\(|fetch|XMLHttp|import\(", page.shell, re.I)
    assert not re.search(r"\b(src|href|srcset|action|poster|background|data)\s*=", fixed, re.I)
    assert not re.search(r"<(link|img|iframe|object|embed|a|base|form|video|audio|source)\b",
                         fixed, re.I)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacent|document\.write|eval\(|Function\(",
                         script)
    assert fixed.count('<meta http-equiv="refresh" content="2">') == 1
    reloads = {every: monitor.Page({}, tmp_path / "p.html", every=every).refresh
               for every in (None, 1.0, 5.0, 600.0)}
    assert reloads == {None: 30, 1.0: 1, 5.0: 2, 600.0: 30}


def test_text_of_a_plant_reaches_the_page_as_data_only(tmp_path):
    model = slow_line()
    model["blocks"][1]["description"] = HOSTILE
    page = monitor.Page(model, tmp_path / "m.html", every=5.0, title=HOSTILE)
    doc = result(problems=[said("W_X", HOSTILE)], sources=[{"name": HOSTILE, "state": "down"}],
                 advice={"orders_at_risk": ["secret order"]})
    doc["model"]["name"] = HOSTILE
    text = page.build(doc, [TraceEvent(t=1.0, kind="entity_created", entity_id=1,
                                       e_type=HOSTILE, block="p", target=None)])
    assert text.count("</script>") == 2 and "<img" not in text
    data = data_of(page, text)
    assert data["monitor"]["summary"]["problems"][0][2] == HOSTILE
    assert data["monitor"]["events"][-1][2] == HOSTILE + " entered at p"
    # Nothing of the model but what a player file holds, and nothing of the advice.
    assert "secret order" not in text and "dist_params" not in text and "resource_name" not in text


def test_a_page_that_cannot_be_written_leaves_the_old_one_whole(tmp_path, monkeypatch, caplog):
    page, doc, step, _events = page_of(tmp_path)
    page.write(doc, step)
    before = (tmp_path / monitor.PAGE_FILE).read_bytes()

    def held(_src, _dst):
        raise PermissionError("a browser holds it")

    real = twin.os.replace
    monkeypatch.setattr(twin.os, "replace", held)
    with caplog.at_level("WARNING"):
        said = [page.write(doc, []) for _ in range(3)]
    # Said once, with what a reader must do; not at every step for as long as it lasts.
    assert said[0].startswith("monitor.html could not be written (PermissionError)")
    assert "open, read and close" in said[0] and said[1:] == [None, None]
    assert caplog.text.count("could not be written") == 1
    assert (tmp_path / monitor.PAGE_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [monitor.PAGE_FILE]
    monkeypatch.setattr(twin.os, "replace", real)
    assert [page.write(doc, []), page.write(doc, [])] == ["monitor.html is written again", None]


# -- the script: under node against the player tests' stub of a document ---------------
PANEL = """
flush(3);
function text(node) { return node._text + node.children.map(text).join("|"); }
var out = {made: made.filter(function (tag, i) { return made.indexOf(tag) === i; }).sort(),
           banned: banned, sources: sources, hidden: nodes.bar.style.display};
["m-head", "m-state", "m-notes", "m-sources", "m-problems", "m-forecast", "m-kpis", "m-points",
 "m-events", "m-more", "now-t", "now-wip", "run-h", "foot", "m-scores", "m-score-rows",
 "m-counted", "m-counters", "m-advice", "m-advice-fold", "m-advice-rows"].forEach(function (id) {
  out[id] = text(nodes[id]); });
out["counted-hidden"] = nodes["m-counted-box"].style.display || "";
out.stale = nodes["m-state"].className;
console.log(JSON.stringify(out));
"""


def page_summary(text):
    return json.loads(re.search(r'id="tape">(.*?)</script>', text, re.S).group(1))["monitor"]["summary"]


def run_panel(text, now):
    from test_player_script import NODE, STUB

    if NODE is None:
        pytest.skip("node is not installed")
    block = re.search(r'<script type="application/json" id="tape">(.*?)</script>', text, re.S)
    script = re.search(r"<script>(.*)</script>", text, re.S).group(1)
    program = (f"var DATA_TEXT = {json.dumps(block.group(1))};\nDate.now = function () "
               f"{{ return {now * 1000}; }};\n" + STUB + script + "\n" + PANEL)
    done = subprocess.run([NODE, "-"], input=program, capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert report["banned"] == [] and report["sources"] == []
    assert set(report["made"]) <= {"option", "tr", "th", "td", "div", "span", "p"}
    return report


def test_the_script_shows_the_summary_the_events_and_says_when_the_twin_is_stale(tmp_path):
    page = monitor.Page(slow_line(), tmp_path / "m.html", every=5.0)
    doc = result(view={"approximate": True, "wip": 3},
                 problems=[said("W_TWIN_SOURCE", HOSTILE)])
    text = page.build(doc, [TraceEvent(t=700.0, kind="resource_state", entity_id=-1, e_type="",
                                       block="", target=None, resource="Server", state="down")])
    written = live.stamp(doc["written"])
    fresh = run_panel(text, written + 4.0)
    assert fresh["hidden"] == "none" and fresh["run-h"] == "The plant now"
    assert fresh["m-head"] == "Plant time 720 minutes (2026-10-10T12:00:00+00:00); cycle 12"
    assert fresh["stale"] == "" and fresh["m-state"] == (
        monitor.state(page_summary(text), written + 4.0)[0]
        + " Twin started 2026-10-10T11:00:00+00:00. This page reloads every 2 s.")
    assert "Updated 4 s ago; the twin looks at the plant every 5 s." in fresh["m-state"]
    assert fresh["m-notes"] == monitor.APPROXIMATE
    assert "plc|live|1.5|0|0" in fresh["m-sources"] and HOSTILE in fresh["m-problems"]
    assert monitor.IN_DISTRIBUTION in fresh["m-forecast"]
    assert fresh["m-forecast"].startswith("Status: ok|")
    assert "system/WIP|710|4|2|6|7|no" in fresh["m-points"] and "system/WIP|4|2|6" in fresh["m-kpis"]
    assert "700|2026-10-10T11:40:00+00:00|Server down" in fresh["m-events"]
    assert fresh["now-t"] == "700"
    old = run_panel(text, written + 11.0)
    assert old["stale"] == "stale" and old["m-state"].startswith(
        monitor.state(page_summary(text), written + 11.0)[0])
    assert "STALE: no update for 11 s" in old["m-state"]
    doc["cadence"] = {"every": None, "forecast_every": 0.0}
    scheduled = run_panel(monitor.Page(slow_line(), tmp_path / "s.html").build(doc, []),
                          written + 9999.0)
    assert scheduled["stale"] == "" and "Updated 9999 s ago" in scheduled["m-state"] \
        and "stepped by a scheduler, so not watched for staleness" in scheduled["m-state"]


# -- a real browser: Chrome or Edge prints the page it made ----------------------------
BROWSER = next((path for path in (
    shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
    shutil.which("msedge"), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if path and Path(path).is_file()), None)


@pytest.mark.skipif(BROWSER is None, reason="neither Chrome nor Edge is installed")
def test_a_real_browser_runs_the_page_opened_from_disk(tmp_path):
    page, doc, step, _events = page_of(tmp_path / "out")
    doc["problems"].append(said("W_X", HOSTILE))
    doc["written"] = "2020-01-01T00:00:00+00:00"
    page.write(doc, step)
    done = subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--no-first-run", "--dump-dom",
         f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=1500",
         (tmp_path / "out" / monitor.PAGE_FILE).as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    dom = done.stdout
    assert done.returncode == 0 and '<p id="m-head">Plant time 10.5 seconds' in dom, \
        done.stderr[-2000:]
    # The policy let the one script run, and the script filled the panels as text.
    assert '<h2 id="run-h">The plant now</h2>' in dom and "<td>an item entered at c</td>" in dom
    assert "&lt;img src=x onerror=alert(1)&gt;" in dom and "<img" not in dom
    assert 'class="stale">STALE: no update for' in dom
    # Not asserted: the drawing and its "now" panel, filled on an animation frame the
    # dump may come before (the script's drawing half is run under node, above).


# ---------------------------------------------------------------- naga twin --monitor
def test_naga_twin_writes_the_page_only_when_asked_and_goes_on_when_it_cannot(
        tmp_path, capsys, monkeypatch):
    from test_twin_cycle import on_disk

    model, mapping, log = on_disk(tmp_path, slow_line())
    common = ["--map", mapping, "--log", log, "--horizon", "10", "--replications", "2"]
    assert twin.main(["once", model, *common, "--out", str(tmp_path / "plain")]) == 0
    assert sorted(p.name for p in (tmp_path / "plain").iterdir()) == ["twin.json", "twin_history.json"]
    out = tmp_path / "out"
    assert twin.main(["run", model, *common, "--out", str(out), "--monitor", "--every", "2s",
                      "--speed", "1000", "--max-cycles", "3"], wait=lambda _s: None) == 0
    assert "monitor.html" in capsys.readouterr().out
    text = (out / monitor.PAGE_FILE).read_text(encoding="ascii")
    assert '<meta http-equiv="refresh" content="1">' in text
    shown = json.loads(re.search(r'id="tape">(.*?)</script>', text, re.S).group(1))["monitor"]
    # The page and the result are of the same step.
    doc = twin.read_result(out)[0]
    assert shown["summary"]["head"] == monitor.summary(doc)["head"] and doc["cycle"] == 3
    assert ">line<" not in text and '"title":"Plant monitor"' in text      # not the file's stem
    assert any(row[2] == "an item left from d" for row in shown["events"])
    # A page a browser holds open costs that one write, said, and nothing else.
    real = twin.write_text

    def held(path, text, **more):
        if Path(path).name == monitor.PAGE_FILE:
            raise OSError("PermissionError")
        real(path, text, **more)

    monkeypatch.setattr(twin, "write_text", held)
    assert twin.main(["run", model, *common, "--out", str(out), "--monitor", "--speed", "1000",
                      "--max-cycles", "3"], wait=lambda _s: None) == 0
    assert capsys.readouterr().out.count("monitor.html could not be written (PermissionError)") == 1
    assert twin.read_result(out)[0]["cycle"] == 3


# ---------------------------------------------------------------- scores and counters (7.14)
SCORES = {"nominal": 0.9,
          "basis": {"forecasts": 12, "same_seed": 0, "points": 40, "missed": 3, "lost": 2,
                    "since": "2026-10-10T08:00:00+00:00", "first": 100.0, "last": 715.0},
          "rows": [{"series": "system/WIP", "lead": 10.0, "points": 12, "missed": 0, "lost": 0,
                    "whole": True, "coverage": 0.75, "bias": -0.5, "error": 1.25, "pinball": 0.2},
                   {"series": "level/Tank", "lead": 10.0, "points": 12, "missed": 0, "lost": 0,
                    "whole": False, "coverage": 0.75, "bias": -0.5, "error": 1.25, "pinball": 0.2},
                   {"series": "block/Drill/out", "lead": 60.0, "points": 0, "missed": 3, "lost": 2,
                    "whole": False, "coverage": None, "bias": None, "error": None,
                    "pinball": None}]}
COUNTERS = {"Drill": {"total": 41, "since": 12.5, "resets": 1}}


def test_a_summary_never_shows_a_score_without_its_basis_nor_none_without_its_reason():
    shown = monitor.summary(result(scores=SCORES))["scores"]
    assert shown["lines"] == [
        "12 forecasts, 40 readings scored, 3 missed, 2 not compared, since "
        "2026-10-10T08:00:00+00:00 (plant time 100 to 715 minutes).", monitor.SCORED,
        monitor.MISSED]
    # A row of whole numbers is marked once, where its coverage is read.
    assert shown["rows"] == [["system/WIP", 10.0, 12, 0, 0, "75% *", -0.5, 1.25, 0.2],
                             ["level/Tank", 10.0, 12, 0, 0, "75%", -0.5, 1.25, 0.2],
                             ["block/Drill/out", 60.0, 0, 3, 2, "", None, None, None]]
    assert "a counter lost count" in monitor.MISSED and "source was not ready" in monitor.MISSED
    clean = {**SCORES, "basis": {**SCORES["basis"], "missed": 0, "lost": 0}}
    assert monitor.MISSED not in monitor.summary(result(scores=clean))["scores"]["lines"]
    assert "not a test" in monitor.SCORED and "90%" in monitor.SCORED
    # Forecasts on one seed are said not to be independent.
    again = monitor.summary(result(scores={**SCORES, "basis": {**SCORES["basis"], "same_seed": 11}}))
    assert again["scores"]["lines"][-1] == monitor.SAME_SEED.replace("{n}", "11")
    for reason, text in monitor.NO_SCORE.items():
        empty = monitor.summary(result(scores={"rows": [], "reason": reason, "basis": {}}))
        assert empty["scores"] == {"lines": [text], "rows": [], "more": 0}
    assert monitor.summary(result(scores={"reason": "x" * 900}))["scores"]["lines"] \
        == ["No score: " + "x" * 400 + "."]
    for junk in (None, 5, {"rows": "many"}, {"rows": [3, None]}, {}):
        assert monitor.summary(result(scores=junk))["scores"]["lines"] \
            == ["No score: no reason was given."]
    # A result of a twin older than scores says so, not "no reason".
    assert monitor.summary(result())["scores"]["lines"] \
        == ["No score: this result was written before scores existed."]
    lots = {**SCORES, "rows": SCORES["rows"][:1] * 400}
    assert (len(monitor.summary(result(scores=lots))["scores"]["rows"]),
            monitor.summary(result(scores=lots))["scores"]["more"]) == (300, 100)
    json.dumps(monitor.summary(result(scores={**SCORES, "rows": [{"coverage": float("nan")}]})),
               allow_nan=False)


def test_what_a_counter_counts_is_shown_as_observed_and_the_note_no_longer_waits_for_it():
    shown = monitor.summary(result(view={"approximate": True, "counters": COUNTERS}))
    assert shown["counters"] == [["Drill", 41, 12.5, 1]] and shown["counted"] == monitor.COUNTED
    assert shown["notes"] == [monitor.APPROXIMATE]          # items in a block are still estimates
    assert "not available yet" not in monitor.APPROXIMATE
    assert "What a counter signal counts is observed, not estimated." in monitor.APPROXIMATE
    assert "Observed, not estimated." in monitor.COUNTED and "lower bound" in monitor.COUNTED
    assert monitor.summary(result())["counters"] == []
    assert monitor.summary(result(view={"counters": {"a": 3, "b": {"total": "x"}}}))["counters"] \
        == [["b", None, None, None]]


def test_the_script_shows_the_scores_and_the_counters(tmp_path):
    page = monitor.Page(slow_line(), tmp_path / "m.html", every=5.0)
    doc = result(scores=SCORES, view={"approximate": False, "wip": 3, "counters": COUNTERS})
    shown = run_panel(page.build(doc, []), live.stamp(doc["written"]) + 1.0)
    assert shown["m-scores"].startswith("12 forecasts, 40 readings scored, 3 missed, 2 not compared")
    assert monitor.SCORED in shown["m-scores"]
    assert "system/WIP|10|12|0|0|75% *|-0.5|1.25|0.2" in shown["m-score-rows"]
    assert "block/Drill/out|60|0|3|2||-|-|-" in shown["m-score-rows"]
    assert "Not compared|Coverage" in shown["m-score-rows"]
    assert shown["m-counted"] == monitor.COUNTED and "Drill|41|12.5|1" in shown["m-counters"]
    # No counter: the section is hidden. No score: its reason.
    bare = run_panel(page.build(result(scores={"rows": [], "reason": "none_due"}), []),
                     live.stamp(doc["written"]) + 1.0)
    assert bare["counted-hidden"] == "none" and shown["counted-hidden"] == ""
    assert bare["m-scores"] == monitor.NO_SCORE["none_due"] and bare["m-score-rows"] == ""


def test_the_script_leads_with_the_action_and_folds_the_explanation_escaped(tmp_path):
    """D358: lead lines outside the fold, basis and rows inside it; a hostile name is text in both."""
    page = monitor.Page(slow_line(), tmp_path / "m.html", every=5.0)
    doc = result()
    doc["recommend"] = {
        "status": "ok", "kpi": HOSTILE, "better": "higher", "outbox": {"sink": "file"},
        "items": [{"rank": 1, "verdict": "better", "status": "delivered", "effect": 1.0,
                   "low": 0.5, "high": 1.5, "confidence": 1.0, "id": "a" * 64,
                   "action": {"verb": "set_capacity", "target": HOSTILE, "value": 2}}]}
    text = page.build(doc, [])
    assert "<details>" in text and "<img src=x" not in text.replace(HOSTILE, "")
    shown = run_panel(text, live.stamp(doc["written"]) + 1.0)
    assert shown["m-advice"].startswith("Recommended: set_capacity " + HOSTILE)
    assert monitor.ADVICE not in shown["m-advice"] and monitor.ADVICE in shown["m-advice-fold"]
    assert HOSTILE in shown["m-advice-fold"] and HOSTILE in shown["m-advice-rows"]
