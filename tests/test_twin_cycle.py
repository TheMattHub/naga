"""The generic twin cycle (PHASE7 7.12, D265-D276): `nagasim/twin.py`.

Clock, source and forecast runner are injected; a background forecast is waited for on
events. Nothing here sleeps or reads the wall clock.
"""
import json
import os
import threading
import types
from pathlib import Path

import pytest

import nagasim
from conftest import chain, create, dispose, layout, process
from nagasim import api, live, plant, state, twin
from nagasim.core.playback import Timeline
from nagasim.core.problems import problem
from nagasim.core.trace import TraceRecorder
from nagasim.sources import ReadError

MODELS = Path(nagasim.__file__).resolve().parent / "models"


class Frame(list):
    """The one call the cycle makes on a forecast's frames."""

    def to_dict(self, orient):
        assert orient == "records"
        return list(self)


def bands(start, horizon, series=("system/WIP",)):
    return Frame({"Series": name, "Time": start + horizon * i / 60, "Mean": 4.0, "Q05": 2.0,
                  "Q95": 6.0, "N": 3} for name in series for i in range(61))


class Source:
    name, model_hash, approximate = "line", "m" * 64, True

    def __init__(self):
        self.at, self.ready, self.values, self.refuse = 0.0, True, {"system/WIP": 3.0}, None
        self.starts = 0

    def observe(self):
        return {"at": self.at, "stamp": None, "ready": self.ready, "problems": [],
                "sources": [{"name": "plc", "state": "live", "age": 0.0, "lost": 0, "gaps": 0}],
                "view": {"approximate": True, "wip": 3, "blocks": {"Drill": 3}},
                "observed": dict(self.values)}

    def start(self, horizon):
        self.starts += 1
        if self.refuse is not None:
            return None, None, [self.refuse]
        return ({"initial_state": {"clock": self.at}},
                {"hash": f"s{self.starts}", "model": {"hash": self.model_hash}}, [])


class Clock:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 50.0

    def tick(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make(tmp_path=None, source=None, runner=None, **more):
    source, clock, calls = source or Source(), Clock(), []

    def run(model, *, horizon, replications, seed, workers, should_stop):
        calls.append(model)
        start = model["initial_state"]["clock"]
        return types.SimpleNamespace(
            kpis=Frame([{"KPI": "system/Total Completed", "Mean": 9.0, "LB": 8.0, "UB": 10.0,
                         "Q05": 8.0, "Q95": 10.0, "N": replications}]),
            bands=bands(start, horizon))

    cycle = twin.Cycle(source, horizon=60.0, replications=3, seed=7, out_dir=tmp_path,
                       runner=runner or run, clock=lambda: clock.wall,
                       monotonic=lambda: clock.mono, **{"forecast_every": 300.0, **more})
    return cycle, source, clock, calls


def codes(doc):
    return [p["code"] for p in doc["problems"]]


# ---------------------------------------------------------------- the result
def test_a_step_writes_one_whole_versioned_result_a_second_process_reads(tmp_path):
    cycle, _source, _clock, _calls = make(tmp_path)
    doc = cycle.step()
    read, problems = twin.read_result(tmp_path)
    assert problems == [] and read == json.loads(json.dumps(doc))
    assert (doc["format"], doc["kind"], doc["cycle"]) == (1, "naga-twin", 1)
    assert doc["model"] == {"name": "line", "hash": "m" * 64, "time_unit": None, "epoch": None}
    assert doc["written"].startswith("2027-01-15T08:00:00") and doc["started"] == doc["written"]
    assert doc["cadence"] == {"every": None, "forecast_every": 300.0}
    assert doc["sources"][0]["state"] == "live" and doc["view"]["blocks"] == {"Drill": 3}
    forecast = doc["forecast"]
    assert forecast["status"] == "ok"
    assert {k: forecast[k] for k in ("state", "model", "seed", "from", "horizon",
                                     "replications", "approximate")} == {
        "state": "s1", "model": "m" * 64, "seed": 7, "from": 0.0, "horizon": 60.0,
        "replications": 3, "approximate": True}
    assert (forecast["finished"], forecast["took"]) == (doc["written"], 0.0)
    assert forecast["kpis"] == [{"kpi": "system/Total Completed", "mean": 9.0, "lb": 8.0,
                                 "ub": 10.0, "q05": 8.0, "q95": 10.0, "n": 3}]
    # KPIs at stated times: six of them, each with its interval, none observed yet.
    assert [p["at"] for p in forecast["points"]] == [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    assert forecast["points"][0] == {"series": "system/WIP", "at": 10.0, "mean": 4.0,
                                     "q05": 2.0, "q95": 6.0, "observed": None,
                                     "observed_at": None}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin_history.json"]


def test_a_result_says_its_cadence_when_it_began_and_how_long_a_forecast_took(tmp_path):
    def run(model, **_):
        clock.tick(4.0)                     # the forecast takes four seconds
        return types.SimpleNamespace(kpis=Frame(), bands=bands(0.0, 60.0))

    cycle, _source, clock, _calls = make(tmp_path, runner=run, every=5.0)
    clock.tick(60.0)                        # the twin began a minute before its first step
    doc = cycle.step()
    assert doc["cadence"] == {"every": 5.0, "forecast_every": 300.0}
    assert doc["started"] == "2027-01-15T08:00:00+00:00"
    assert doc["written"] == "2027-01-15T08:01:00+00:00"
    assert doc["forecast"]["finished"] == "2027-01-15T08:01:04+00:00"
    assert doc["forecast"]["took"] == 4.0
    clock.tick(5.0)
    assert cycle.step()["started"] == doc["started"]


def test_nothing_is_written_with_no_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cycle, *_ = make(None)
    assert cycle.step()["forecast"]["status"] == "ok"
    assert list(tmp_path.iterdir()) == []


def test_a_reader_refuses_what_is_not_a_result(tmp_path):
    assert twin.read_result(tmp_path)[1][0].code == "E_TWIN_RESULT"          # none yet
    target = tmp_path / twin.RESULT_FILE
    for text in ("{", "[1]", json.dumps({"format": 2, "kind": "naga-twin"}),
                 json.dumps({"format": 1, "kind": "other"}),
                 " " * (twin.LIMITS["result_bytes"] + 1)):
        target.write_text(text, encoding="utf-8")
        doc, problems = twin.read_result(tmp_path)
        assert doc is None and problems[0].code == "E_TWIN_RESULT"


# ---------------------------------------------------------------- cadence
def test_a_forecast_has_its_own_slower_cadence(tmp_path):
    cycle, _source, clock, calls = make(tmp_path)
    for _ in range(4):                      # monitoring every 5 s
        assert cycle.step()["forecast"]["status"] == "ok"
        clock.tick(5.0)
    assert len(calls) == 1
    clock.tick(300.0)
    assert cycle.step()["forecast"]["state"] == "s2" and len(calls) == 2


def test_successive_forecasts_are_independent_samples_each_with_its_seed_on_record(tmp_path):
    used = []

    def run(model, *, seed, replications, **_):
        used.append(seed)
        return types.SimpleNamespace(kpis=Frame(), bands=bands(0.0, 60.0))

    cycle, _source, clock, _calls = make(tmp_path, runner=run, forecast_every=0.0)
    said = []
    for _ in range(3):
        said.append(cycle.step()["forecast"]["seed"])
        clock.tick(1.0)
    # Three replications each, on seeds 7-9, 10-12, 13-15: no stream is used twice.
    assert said == used == [7, 10, 13]


def test_a_running_forecast_is_not_started_twice_and_stop_cancels_it():
    entered, gate, seen = threading.Event(), threading.Event(), []

    def slow(model, *, should_stop, **_):
        entered.set()
        assert gate.wait(30)
        seen.append(should_stop())
        return types.SimpleNamespace(kpis=Frame(), bands=bands(0.0, 60.0))

    cycle, source, clock, _calls = make(runner=slow, background=True)
    assert cycle.step()["forecast"]["status"] == "running"
    assert entered.wait(30)
    clock.tick(300.0)
    busy = cycle.step()
    assert "W_TWIN_FORECAST_BUSY" in codes(busy) and source.starts == 1
    gate.set()
    cycle.join()
    assert cycle.step()["forecast"]["status"] == "ok" and seen == [False]

    entered.clear(), gate.clear()
    clock.tick(300.0)
    assert cycle.step()["forecast"]["status"] == "running" and entered.wait(30)
    cycle.stop()                            # asks the forecast to stop; the runner sees it
    gate.set()
    cycle.join()
    assert seen == [False, True]
    assert cycle.step()["forecast"]["state"] == "s1"        # the cancelled one left no record


def test_a_stop_is_terminal_wherever_it_lands(tmp_path):
    seen = []

    def run(model, *, should_stop, **_):
        seen.append(should_stop())
        return types.SimpleNamespace(kpis=Frame(), bands=bands(0.0, 60.0))

    cycle, source, clock, _calls = make(tmp_path, runner=run)
    start = source.start

    def stopped_meanwhile(horizon):         # the stop lands while the start is being made
        cycle.stop()
        return start(horizon)

    source.start = stopped_meanwhile
    assert cycle.step()["forecast"] == {"status": "none"} and seen == [True]
    clock.tick(300.0)
    doc = cycle.step()                      # still monitoring, and no forecast ever again
    assert doc["view"]["wip"] == 3 and doc["forecast"] == {"status": "none"}
    assert source.starts == 1 and cycle.join(0.0) is True


# ---------------------------------------------------------------- compare
def test_each_stated_point_is_compared_once_with_what_was_then_observed(tmp_path):
    first, source, _clock, _calls = make(tmp_path)
    first.step()                            # a forecast from t = 0: points at 10, 20 ... 60
    # Another process (the next scheduled `once`) finds the forecast in the directory.
    cycle, source, _clock, _calls = make(tmp_path, source=source)
    source.ready = False                    # this one only watches and compares
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    rows = cycle.step()["comparison"]
    assert rows == [{"state": "s1", "series": "system/WIP", "at": 10.0, "mean": 4.0,
                     "q05": 2.0, "q95": 6.0, "observed": 5.0, "observed_at": 12.0,
                     "inside": True}]
    source.at, source.values = 13.0, {"system/WIP": 99.0}
    assert cycle.step()["comparison"] == rows               # once, never again
    # A twin that was away: what it did not see in time is missed, not made up.
    source.at = 52.0
    doc = cycle.step()
    assert [(r["at"], r["observed"], r["inside"]) for r in doc["comparison"]] == [
        (50.0, 99.0, False), (10.0, 5.0, True)]
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))
    assert [p.get("missed", False) for p in history["forecasts"][0]["points"]] == [
        False, True, True, True, False, False]
    source.at, source.values = 60.0, {}                     # a series nobody observes
    assert len(cycle.step()["comparison"]) == 2
    # A history that is not what this module wrote is not walked: its records are left out.
    history["forecasts"] += [{"state": "x", "horizon": 60.0, "points": [{"at": "soon"}]}, 7]
    twin.write_json(tmp_path / twin.HISTORY_FILE, history)
    again, source, _clock, _calls = make(tmp_path, source=source)
    source.ready = False
    assert len(again.step()["comparison"]) == 2


def test_a_result_handed_out_does_not_change_at_the_next_step(tmp_path):
    cycle, source, clock, _calls = make(tmp_path)
    doc = cycle.step()
    kept = json.dumps(doc)
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    clock.tick(5.0)
    assert cycle.step()["forecast"]["points"][0]["observed"] == 5.0
    assert json.dumps(doc) == kept


def test_a_damaged_history_record_is_dropped_and_the_cycle_goes_on(tmp_path):
    first, source, _clock, _calls = make(tmp_path)
    first.step()
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))
    whole = history["forecasts"][0]["points"][0]
    # Format 1 and well typed, but a point lacks a key (a hand edit, another tool).
    history["forecasts"] += [{"state": "x", "horizon": 60.0, "points": [
        {k: v for k, v in whole.items() if k != missing}]} for missing in whole]
    twin.write_json(tmp_path / twin.HISTORY_FILE, history)
    cycle, source, _clock, _calls = make(tmp_path, source=source)
    assert len(cycle._history) == 1
    source.ready = False
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    assert len(cycle.step()["comparison"]) == 1


# ---------------------------------------------------------------- failures
def test_a_failed_forecast_keeps_the_last_good_one_and_says_only_the_class(tmp_path):
    state = {"fail": False}

    def run(model, **_):
        if state["fail"]:
            raise RuntimeError("password=hunter2")
        return types.SimpleNamespace(kpis=Frame(), bands=bands(0.0, 60.0))

    cycle, _source, clock, _calls = make(tmp_path, runner=run)
    cycle.step()
    state["fail"] = True
    clock.tick(300.0)
    doc = cycle.step()
    assert doc["forecast"]["status"] == "failed" and doc["forecast"]["state"] == "s1"
    assert doc["forecast"]["reason"] == "E_TWIN_FORECAST"
    text = (tmp_path / twin.RESULT_FILE).read_text(encoding="utf-8")
    assert "RuntimeError" in text and "hunter2" not in text
    clock.tick(5.0)
    assert "E_TWIN_FORECAST" in codes(cycle.step())         # it stands until the next attempt
    state["fail"] = False
    clock.tick(300.0)
    doc = cycle.step()
    assert doc["forecast"]["status"] == "ok" and "E_TWIN_FORECAST" not in codes(doc)


def test_a_refused_start_is_said_and_never_replaced_by_a_cold_start(tmp_path):
    cycle, source, clock, calls = make(tmp_path)
    source.refuse = problem("E_TWIN_STATE", "this model cannot be started from plant state "
                                            "yet: fleets and vehicles", "/initial_state")
    doc = cycle.step()
    assert calls == [] and doc["view"]["wip"] == 3          # still monitoring
    assert doc["forecast"] == {"status": "refused", "reason": "E_TWIN_STATE"}
    clock.tick(5.0)
    again = cycle.step()
    assert "E_TWIN_STATE" in codes(again) and source.starts == 1    # said, not retried yet


def test_a_source_that_is_not_ready_skips_the_forecast_until_it_is(tmp_path):
    cycle, source, clock, calls = make(tmp_path)
    source.ready = False
    assert cycle.step()["forecast"] == {"status": "skipped", "reason": "W_TWIN_SOURCE"}
    clock.tick(5.0)
    source.ready = True
    assert cycle.step()["forecast"]["status"] == "ok" and len(calls) == 1

    def broken():
        raise OSError("//host/share/secret")

    source.observe = broken
    doc = cycle.step()
    assert codes(doc) == ["W_TWIN_SOURCE"] and "secret" not in json.dumps(doc)
    assert doc["view"] is None and doc["forecast"]["status"] == "ok"


def test_a_clock_jump_is_said_once_and_moves_no_cadence(tmp_path):
    cycle, _source, clock, calls = make(tmp_path)
    cycle.step()
    clock.wall += 3600.0                    # the wall clock is set forward; no time passed
    assert "W_TWIN_CLOCK" in codes(cycle.step()) and len(calls) == 1
    clock.tick(5.0)
    assert "W_TWIN_CLOCK" not in codes(cycle.step())


def test_a_write_that_fails_is_said_in_the_next_result_and_leaves_no_half_file(
        tmp_path, monkeypatch, caplog):
    cycle, *_ = make(tmp_path)
    before = cycle.step()
    real, failing = os.replace, {"on": True}

    def replace(src, dst):
        if failing["on"]:
            raise PermissionError("a reader holds it")
        return real(src, dst)

    monkeypatch.setattr(twin.os, "replace", replace)
    doc = cycle.step()                      # the step still answers
    assert doc["cycle"] == 2
    cycle.step()                            # failure after failure leaves nothing behind
    # The operator's log has the exception itself; a result only ever its class.
    assert "a reader holds it" in caplog.text
    assert twin.read_result(tmp_path)[0]["cycle"] == before["cycle"]    # the old file, whole
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin_history.json"]
    failing["on"] = False
    doc = cycle.step()
    assert "E_TWIN_OUTPUT" in codes(doc) and "a reader holds it" not in json.dumps(doc)
    assert "E_TWIN_OUTPUT" in codes(twin.read_result(tmp_path)[0])
    assert "E_TWIN_OUTPUT" not in codes(cycle.step())


def test_each_write_has_a_temporary_file_of_its_own(tmp_path, monkeypatch):
    target, foreign = tmp_path / twin.RESULT_FILE, tmp_path / (twin.RESULT_FILE + ".tmp")
    foreign.write_text("{partial", encoding="utf-8")    # a second twin's, or a crashed one's
    used, real = [], os.replace
    monkeypatch.setattr(twin.os, "replace", lambda src, dst: used.append(src) or real(src, dst))
    twin.write_json(target, {"n": 1})
    twin.write_json(target, {"n": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 2}
    assert foreign.read_text(encoding="utf-8") == "{partial" and len(set(used)) == 2
    if os.name == "posix":                  # the reader may be another account's monitor
        made = tmp_path / "made"
        made.touch()                        # what this process's umask gives a new file
        assert target.stat().st_mode == made.stat().st_mode
        made.unlink()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin.json.tmp"]
    with pytest.raises(OSError, match="^ValueError$"):  # not plain data: nothing is left
        twin.write_json(target, {"n": float("nan")})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin.json.tmp"]


def test_a_directory_that_cannot_be_made_does_not_stop_the_cycle(tmp_path):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    cycle, *_ = make(tmp_path / "file" / "out")
    assert cycle.step()["cycle"] == 1 and "E_TWIN_OUTPUT" in codes(cycle.step())


# ---------------------------------------------------------------- bounds
def test_every_bound_has_a_ceiling_the_caller_cannot_pass(tmp_path, monkeypatch):
    assert twin.Cycle(Source(), horizon=1.0, replications=10 ** 9).replications \
        == twin.LIMITS["replications"]
    for horizon in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            twin.Cycle(Source(), horizon=horizon)

    def run(model, **_):
        start = model["initial_state"]["clock"]
        return types.SimpleNamespace(
            kpis=Frame({"KPI": f"block/{i}/x", "Mean": float("nan"), "LB": 0.0, "UB": 0.0,
                        "Q05": 0.0, "Q95": 0.0, "N": 1} for i in range(500)),
            bands=bands(start, 60.0, [f"level/L{i}" for i in range(300)] + ["block/x/y"]))

    cycle, source, clock, _calls = make(tmp_path, runner=run, forecast_every=0.0)
    doc = cycle.step()
    assert len(doc["forecast"]["kpis"]) == twin.LIMITS["kpis"]
    assert doc["forecast"]["kpis"][0]["mean"] is None       # not a number: null, valid JSON
    points = doc["forecast"]["points"]
    assert len(points) == twin.LIMITS["series"] * twin.LIMITS["times"]
    assert all(p["series"].startswith("level/") for p in points)
    assert "W_TWIN_CUT" in codes(doc)
    monkeypatch.setitem(twin.LIMITS, "forecasts", 3)
    for _ in range(5):
        clock.tick(1.0)
        cycle.step()
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))
    assert len(history["forecasts"]) == twin.LIMITS["forecasts"]

    def noisy():
        return {"at": 0.0, "ready": False, "view": {"wip": 0},
                "problems": [problem("W_LIVE_GAP", f"gap {i}") for i in range(300)]}

    source.observe = noisy
    doc = cycle.step()
    assert len(doc["problems"]) == twin.LIMITS["problems"] and codes(doc)[-1] == "W_TWIN_CUT"

    source.advise = lambda forecast, model: {"orders": "x" * (3 * 2 ** 20)}
    del source.observe
    clock.tick(1.0)
    doc = cycle.step()
    assert "advice" not in doc and "W_TWIN_CUT" in codes(doc)
    assert (tmp_path / twin.RESULT_FILE).stat().st_size <= twin.LIMITS["result_bytes"]


def test_a_sources_advice_travels_in_the_result(tmp_path):
    source = Source()
    source.advise = lambda forecast, model: {"alerts": [{"rule": "wip above limit"}]}
    cycle, *_ = make(tmp_path, source=source)
    assert cycle.step()["advice"] == {"alerts": [{"rule": "wip above limit"}]}
    assert cycle.forecast is not None       # the forecast itself, for the layer that advises


# ---------------------------------------------------------------- the loop
def test_the_loop_goes_on_after_a_bad_step_and_ends_cleanly():
    waits, steps = [], []

    def step():
        steps.append(1)
        if len(steps) == 2:
            raise RuntimeError("the plant was away for a minute")

    assert twin.loop(step, every=5.0, max_cycles=3, wait=waits.append) == 3
    assert len(steps) == 3 and len(waits) == 2 and all(0 <= w <= 5.0 for w in waits)

    def interrupt():
        raise KeyboardInterrupt

    assert twin.loop(interrupt, every=5.0, max_cycles=3, wait=waits.append) == 0


def test_interval_parsing():
    assert [twin.parse_interval(t) for t in ("30s", "15m", "2h", "90", "1d")] == [
        30, 900, 7200, 90, 86400]
    with pytest.raises(ValueError):
        twin.parse_interval("soon")


# ================================================================ a plant as the source
def slow_line(**sections):
    """Arrivals every 2 from t = 2, a service of 3, one server: the queue grows. At
    t = 10.5 the third job is in service and two wait."""
    c, p, d = create("c", dist_params={"value": 2.0}), process("p", value=3.0), dispose("d")
    sections.setdefault("simulation", {"time_limit": 40, "warmup": 0, "replications": 1,
                                       "time_unit": "seconds"})
    sections.setdefault("resources", [{"name": "Server", "capacity": 1}])
    return layout([c, p, d], chain(c, p, d), **sections)


def mock_plant(model, **scaffold):
    """(the mapping, the plant log of a run of *model*, the run's trace events)."""
    mapping = plant.scaffold(model, **scaffold)
    # Seconds from 1970: a stamp is then the model clock itself.
    mapping["time"] = {"epoch": "1970-01-01T00:00:00+00:00", "unit": "seconds"}
    recorder = TraceRecorder()
    api.run_model(model, replications=1, seed=1, trace=recorder, record_logs=False)
    return mapping, plant.log_of(recorder.events, mapping), recorder.events


def playing(model, **scaffold):
    """A plant source on the model's own mock log, played at one second a second on a
    clock the test moves."""
    mapping, log, events = mock_plant(model, **scaffold)
    clock = Clock()
    clock.mono = 0.0
    feed = twin.LogFeed(log, speed=1.0, monotonic=lambda: clock.mono)
    return twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock), clock, events


def test_a_plant_source_shows_the_plant_and_a_forecast_starts_from_its_state(tmp_path):
    model = slow_line()
    source, clock, events = playing(model)
    cycle = twin.Cycle(source, horizon=12.0, replications=2, seed=3, out_dir=tmp_path,
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    clock.mono = 10.5
    doc = cycle.step()
    # The view is the run's own, at the instant the plant last spoke.
    run = Timeline(events).state_at(10.0)
    assert doc["view"]["blocks"] == run["counts"] and doc["view"]["wip"] == 3
    assert doc["view"]["resources"] == [{"name": "Server", "state": "busy", "busy": 1,
                                         "capacity": 1}]
    assert doc["view"]["approximate"] is False and doc["at"] == 10.5
    assert doc["stamp"] == "1970-01-01T00:00:10+00:00"
    assert doc["sources"] == [{"name": "plant", "state": "live", "age": 0.0, "lost": 0,
                               "gaps": 0}]
    assert doc["mapped"]["events"] > 0 and doc["model"]["hash"] == state.model_hash(model)
    # Model time reads as an instant: its unit, and what its 0 stands for.
    assert (doc["model"]["time_unit"], doc["model"]["epoch"]) == (
        "seconds", "1970-01-01T00:00:00+00:00")
    # The forecast names the state it started from, and that state is the plant's of now.
    forecast = doc["forecast"]
    assert forecast["status"] == "ok" and forecast["from"] == 10.5 and forecast["seed"] == 3
    expected, _problems = state.from_plant(model, dict(source.mapper.snapshot(), at=10.5))
    assert forecast["state"] == expected["hash"] and expected["state"]["clock"] == 10.5
    assert forecast["model"] == doc["model"]["hash"]
    assert {p["series"] for p in forecast["points"]} == {"system/WIP",
                                                         "resource/Server/in_service"}
    assert all(10.5 < p["at"] <= 22.5 for p in forecast["points"])
    assert any(k["kpi"].startswith("system/") for k in forecast["kpis"])
    # Later the plant has moved on: each point that came due is set beside what was seen.
    first = min(p["at"] for p in forecast["points"])
    clock.mono = first + 0.25
    rows = cycle.step()["comparison"]
    assert {r["series"] for r in rows} == {"system/WIP", "resource/Server/in_service"}
    seen = Timeline(events).state_at(first + 0.25)
    assert {r["observed"] for r in rows if r["series"] == "system/WIP"} \
        == {float(sum(seen["counts"].values()))}
    assert all(r["state"] == forecast["state"] and r["at"] == first for r in rows)


def test_a_model_no_state_can_carry_is_monitored_and_refused_by_name(tmp_path):
    model = api.load_layout(MODELS / "warehouse_forklifts.json")
    next(b for b in model["blocks"] if b["type"] == "CONVEYOR")["params"]["accumulating"] = False
    mapping = plant.scaffold(model)
    mapping["time"] = {"epoch": "1970-01-01T00:00:00+00:00", "unit": "seconds"}
    block = next(iter(next(s for s in mapping["signals"] if s["as"] == "at")["blocks"]))
    log = [{"source": "plant", "key": "entity/7", "value": {"at": block, "type": ""},
            "source_time": 4.0, "received": 4.0}]
    source = twin.PlantSource(model, mapping, twin.LogFeed(log), name="hub",
                              clock=lambda: 4.0)

    def never(*_args, **_kwargs):
        raise AssertionError("a cold start")

    cycle = twin.Cycle(source, horizon=60.0, out_dir=tmp_path, runner=never)
    doc = cycle.step()
    assert doc["forecast"] == {"status": "refused", "reason": "E_TWIN_STATE"}
    refusal = next(p for p in doc["problems"] if p["code"] == "E_TWIN_STATE")
    assert refusal["message"] == ("this model cannot be started from plant state yet: "
                                  "a non-accumulating conveyor")
    assert doc["view"]["wip"] == 1 and sum(doc["view"]["blocks"].values()) == 1


def test_a_plant_the_mapping_does_not_understand_is_said_and_not_forecast(tmp_path):
    model = slow_line()
    mapping, log, _events = mock_plant(model)
    # The plant speaks, under keys no signal of the mapping reads.
    renamed = [dict(line, key="other/" + line["key"]) if "key" in line else line for line in log]
    source = twin.PlantSource(model, mapping, twin.LogFeed(renamed), clock=lambda: 40.0)

    def never(*_args, **_kwargs):
        raise AssertionError("a forecast from an empty view that is only ignorance")

    doc = twin.Cycle(source, horizon=10.0, out_dir=tmp_path, runner=never).step()
    assert doc["sources"][0]["state"] == "live" and doc["mapped"]["events"] == 0
    assert doc["forecast"] == {"status": "skipped", "reason": "W_TWIN_SOURCE"}
    said = [p["message"] for p in doc["problems"] if p["code"] == "W_TWIN_SOURCE"]
    assert len(said) == 1 and said[0].startswith(f"{len(renamed)} observations, none mapped")


def test_an_idle_plant_the_mapping_reads_is_forecast_though_nothing_moves(tmp_path):
    model = slow_line()
    mapping = plant.scaffold(model, anonymous=True)
    mapping["time"] = {"epoch": "1970-01-01T00:00:00+00:00", "unit": "seconds"}
    idle = {"queue": 0, "busy": 0, "blocked": 0, "state": False}
    mapping["signals"] = [s for s in mapping["signals"] if s["as"] in idle]
    # Every key is one the mapping reads; an empty, idle plant gives no event at all.
    log = [{"source": "plant", "key": s["key"], "value": idle[s["as"]], "source_time": 5.0,
            "received": 5.0} for s in mapping["signals"]]
    source = twin.PlantSource(model, mapping, twin.LogFeed(log), clock=lambda: 6.0)
    doc = twin.Cycle(source, horizon=10.0, replications=2, out_dir=tmp_path).step()
    assert doc["mapped"]["observations"] == len(log) and doc["mapped"]["events"] == 0
    assert doc["forecast"]["status"] == "ok" and "W_TWIN_SOURCE" not in codes(doc)


class Feed:
    """Two sources: one the caller gave no connection for, one that is down."""

    def changes(self, source, cursor=None, limit=1000):
        if source == "mes":
            raise ReadError("E_DATA_CONNECTION", "no connection 'erp' is defined", "add it")
        return live.Changes((), "c", 0, True, "down", None, (
            problem("E_DATA_READ", "source 'plc': connection 'line' failed (TimeoutError)"),))


def test_a_source_that_is_down_or_refused_is_said_and_no_forecast_starts(tmp_path):
    model = slow_line()
    source = twin.PlantSource(model, plant.scaffold(model), Feed(), sources=["mes", "plc"])
    cycle = twin.Cycle(source, horizon=10.0, out_dir=tmp_path)
    doc = cycle.step()
    assert doc["forecast"] == {"status": "skipped", "reason": "W_TWIN_SOURCE"}
    assert [(s["name"], s["state"], s["gaps"]) for s in doc["sources"]] == [
        ("mes", "refused", 0), ("plc", "down", 1)]
    assert {"E_DATA_CONNECTION", "E_DATA_READ", "W_TWIN_SOURCE", "W_PLANT_GAP"} <= set(codes(doc))
    assert doc["view"]["wip"] == 0                          # the last picture: nothing yet


class Quiet:
    """A source that is connected and last spoke *age* seconds ago."""
    age = 200.0

    def changes(self, source, cursor=None, limit=1000):
        return live.Changes((), "c", 0, False, "live", self.age, ())


def test_a_source_silent_for_longer_than_the_callers_limit_skips_the_forecast(tmp_path):
    model, feed = slow_line(), Quiet()
    mapping = plant.scaffold(model)
    # No limit by default: a connected plant that is quiet is not stale.
    assert twin.PlantSource(model, mapping, feed, sources=["plc"]).observe()["ready"]
    source = twin.PlantSource(model, mapping, feed, sources=["plc"], max_age=120.0)
    doc = twin.Cycle(source, horizon=10.0, replications=2, out_dir=tmp_path).step()
    assert doc["forecast"] == {"status": "skipped", "reason": "W_TWIN_SOURCE"}
    said = [p for p in doc["problems"] if p["code"] == "W_TWIN_SOURCE"]
    assert len(said) == 1 and said[0]["path"] == "/data_sources/plc"
    assert said[0]["message"].startswith("source 'plc' last spoke 200 s ago, over the 120 s")
    assert doc["sources"][0]["age"] == 200.0 and doc["view"]["wip"] == 0    # still monitored
    feed.age = 60.0
    assert source.observe()["ready"]


class Busy:
    """A plant with *waiting* events to give, and a feed that gives *limit* a pull."""

    def __init__(self, waiting):
        self.waiting, self.pulls = waiting, 0

    def changes(self, source, cursor=None, limit=1000):
        self.pulls += 1
        since = int(cursor or 0)
        events = tuple({"source": source, "key": "k", "value": i, "source_time": float(i),
                        "received": float(i)} for i in range(since, min(since + limit, self.waiting)))
        return live.Changes(events, str(since + len(events)), 0, False, "live", 0.0, ())


def test_a_busy_plant_is_caught_up_with_in_one_step_up_to_a_ceiling(monkeypatch):
    model = slow_line()
    feed = Busy(2500)
    source = twin.PlantSource(model, plant.scaffold(model), feed, sources=["plc"])
    seen = source.observe()
    assert seen["mapped"]["observations"] == 2500 and feed.pulls == 3
    assert "W_TWIN_CUT" not in [p.code for p in seen["problems"]]
    monkeypatch.setitem(twin.LIMITS, "pulls", 2)        # one step never runs for ever
    feed = Busy(10 ** 9)
    source = twin.PlantSource(model, plant.scaffold(model), feed, sources=["plc"])
    seen = source.observe()
    assert feed.pulls == 2 and seen["mapped"]["observations"] == 2000
    assert "W_TWIN_CUT" in [p.code for p in seen["problems"]]
    assert source.observe()["mapped"]["observations"] == 4000   # and goes on from there


def test_a_mapping_that_does_not_fit_the_model_is_in_every_result(tmp_path):
    model = slow_line()
    mapping = plant.scaffold(model)
    mapping["signals"].append({"key": "x", "as": "queue", "block": "no such block"})
    source = twin.PlantSource(model, mapping, twin.LogFeed([]))
    cycle = twin.Cycle(source, horizon=10.0, out_dir=tmp_path)
    for _ in range(2):
        doc = cycle.step()
        assert "E_PLANTMAP_NAME" in codes(doc) and doc["view"] is None
        assert doc["forecast"]["status"] == "skipped"


def test_a_model_file_that_changes_on_disk_is_said_and_not_read_again(tmp_path):
    model, path = slow_line(), tmp_path / "line.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    source = twin.PlantSource(model, plant.scaffold(model), twin.LogFeed([]), path=path)
    assert "W_TWIN_MODEL_CHANGED" not in [p.code for p in source.observe()["problems"]]
    path.write_text(json.dumps(slow_line(resources=[{"name": "Server", "capacity": 2}])),
                    encoding="utf-8")
    assert "W_TWIN_MODEL_CHANGED" in [p.code for p in source.observe()["problems"]]
    assert source.model_hash == state.model_hash(model)


def test_counts_are_flagged_approximate_where_the_plant_names_no_item():
    model = slow_line()
    source, clock, _events = playing(model, anonymous=True)
    clock.mono = 10.5
    seen = source.observe()
    assert source.approximate and seen["view"]["approximate"] and seen["view"]["wip"] == 3
    assert "W_PLANT_SYNTHETIC" in [p.code for p in seen["problems"]]


def test_a_recorded_log_plays_at_its_own_pace_with_its_holes():
    lines = [{"source": "a", "key": "k", "value": 1, "source_time": 100.0, "received": 100.0},
             {"source": "a", "lost": 3},
             {"source": "b", "key": "k", "value": 2, "source_time": 104.0, "received": 104.0},
             {"source": "a", "key": "k", "value": 3, "source_time": 102.0, "received": 109.0},
             {"source": "a", "gap": True}, "not a line"]
    clock = Clock()
    feed = twin.LogFeed(lines, speed=2.0, monotonic=lambda: clock.mono)
    assert feed.sources == ["a", "b"] and feed.clock() == 100.0
    first = feed.changes("a")
    assert [e["value"] for e in first.events] == [1] and (first.lost, first.gap) == (3, False)
    clock.mono += 1.5                       # three seconds of the log
    later = feed.changes("a", first.cursor)
    assert [e["value"] for e in later.events] == [3] and (later.lost, later.gap) == (0, True)
    assert feed.changes("b", first.cursor).events == ()
    whole = twin.LogFeed(lines)             # no pace: all there at once
    assert len(whole.changes("a").events) == 2 and len(whole.changes("b").events) == 1


# ================================================================ naga twin
def on_disk(tmp_path, model):
    """A model, its starter mapping and the log of its own run: the demo with no plant."""
    paths = [str(tmp_path / name) for name in ("line.json", "line.plantmap.json", "plant.jsonl")]
    Path(paths[0]).write_text(json.dumps(model), encoding="utf-8")
    assert plant.main(["init", paths[0], "--out", paths[1]]) == 0
    assert plant.main(["mock", paths[0], "--map", paths[1], "--out", paths[2]]) == 0
    return paths


def test_naga_twin_once_and_status_on_a_mock_plant(tmp_path, capsys):
    model, mapping, log = on_disk(tmp_path, slow_line())
    out = tmp_path / "out"
    assert twin.main(["once", model, "--map", mapping, "--log", log, "--out", str(out),
                      "--horizon", "10", "--replications", "2"]) == 0
    doc, problems = twin.read_result(out)
    assert problems == [] and doc["model"]["name"] == "line"
    assert doc["forecast"]["status"] == "ok" and doc["forecast"]["replications"] == 2
    # The whole log was read: the plant is at the end of the run the model file describes,
    # and the forecast still looks its horizon ahead of that.
    assert doc["forecast"]["from"] == 40.0 == doc["at"]
    assert max(p["at"] for p in doc["forecast"]["points"]) == 50.0
    assert {p.name for p in tmp_path.iterdir()} == {"line.json", "line.plantmap.json",
                                                    "plant.jsonl", "out"}
    capsys.readouterr()
    assert twin.main(["status", str(out)]) == 0
    said = capsys.readouterr().out
    assert "model 'line'" in said and "source plant: live" in said and "Status: ok" in said
    assert twin.main(["status", str(tmp_path / "nowhere")]) == 1
    assert "E_TWIN_RESULT" in capsys.readouterr().out
    # A refusal is an error in the result, and the command's exit code.
    doc["problems"] = [problem("E_TWIN_STATE", "\x1b[31mred").as_dict()]
    twin.write_json(out / twin.RESULT_FILE, doc)
    assert twin.main(["status", str(out)]) == 1
    assert "\x1b" not in capsys.readouterr().out            # a file's text is never an escape


def test_naga_twin_run_loops_until_told_and_cancels_its_forecast(tmp_path, capsys):
    model, mapping, log = on_disk(tmp_path, slow_line())
    waits = []
    assert twin.main(["run", model, "--map", mapping, "--log", log, "--out", str(tmp_path / "o"),
                      "--horizon", "10", "--replications", "2", "--every", "2s",
                      "--speed", "1000", "--max-cycles", "3"], wait=waits.append) == 0
    assert len(waits) == 2 and all(0 <= w <= 2.0 for w in waits)
    doc = twin.read_result(tmp_path / "o")[0]
    assert doc["cycle"] == 3 and doc["cadence"] == {"every": 2.0, "forecast_every": 300.0}
    assert "3 cycle(s)" in capsys.readouterr().out


def test_naga_twin_run_says_when_a_forecast_outlives_its_stop(tmp_path, capsys, monkeypatch):
    model, mapping, log = on_disk(tmp_path, slow_line())
    gate, join = threading.Event(), twin.Cycle.join

    def deaf(model, **_):                   # a pool's running replications hear no stop
        gate.wait(30)
        raise RuntimeError("stopped")

    monkeypatch.setattr(twin, "forecast_from_state", deaf)
    monkeypatch.setattr(twin.Cycle, "join", lambda self, timeout=None: join(self, 0.01))
    try:
        assert twin.main(["run", model, "--map", mapping, "--log", log, "--out",
                          str(tmp_path / "o"), "--horizon", "10", "--workers", "2",
                          "--max-cycles", "1"], wait=lambda _seconds: None) == 0
    finally:
        gate.set()
    said = capsys.readouterr().out
    assert "a forecast is still finishing on 2 workers; the command exits when it ends" in said


def test_naga_twin_refuses_what_it_cannot_read_and_bounds_its_cadence(tmp_path, capsys):
    model, mapping, log = on_disk(tmp_path, slow_line())
    args = ["--out", str(tmp_path / "o"), "--horizon", "10"]
    assert twin.main(["once", model, "--map", model, "--log", log, *args]) == 1
    assert "E_PLANTMAP_FORMAT" in capsys.readouterr().out
    assert "E_PLANTMAP_FORMAT" in codes(twin.read_result(tmp_path / "o")[0])    # a monitor sees why
    assert twin.main(["once", model, "--map", mapping, "--log", str(tmp_path / "no.jsonl"),
                      "--out", str(tmp_path / "other"), "--horizon", "10"]) == 1
    assert not (tmp_path / "other").exists()                # refused before anything is written
    with pytest.raises(SystemExit):                         # a plant or a log, never neither
        twin.main(["once", model, "--map", mapping, *args])
    assert twin._cadence("0.01s", "0s") == (1.0, 1.0) and twin._cadence("5s", "5m") == (5.0, 300.0)


def test_naga_twin_takes_the_callers_maximum_age_of_a_source(tmp_path, monkeypatch):
    model, mapping, log = on_disk(tmp_path, slow_line())
    given, real = [], twin.PlantSource
    monkeypatch.setattr(twin, "PlantSource",
                        lambda *a, **more: given.append(more["max_age"]) or real(*a, **more))
    for flags in ([], ["--max-age", "2m"]):
        assert twin.main(["once", model, "--map", mapping, "--log", log, "--out",
                          str(tmp_path / "o"), "--horizon", "10", "--replications", "2",
                          *flags]) == 0
    assert twin.main(["run", model, "--map", mapping, "--log", log, "--out", str(tmp_path / "o"),
                      "--horizon", "10", "--replications", "2", "--max-age", "90",
                      "--max-cycles", "1"], wait=lambda _seconds: None) == 0
    assert given == [None, 120.0, 90.0]
    with pytest.raises(SystemExit):
        twin.main(["once", model, "--map", mapping, "--log", log, "--out", str(tmp_path / "o"),
                   "--horizon", "10", "--max-age", "soon"])


def test_naga_twin_reads_live_sources_only_over_the_callers_connections(tmp_path, capsys):
    """The real `live.Feed`, and a connections file that defines nothing: the source is
    refused before any socket, said in the result, and nothing is forecast."""
    model = slow_line(data_sources=[{"name": "plc", "kind": "mqtt", "connection": "broker",
                                     "location": "line/#"}])
    paths = [tmp_path / name for name in ("line.json", "line.plantmap.json", "conn.json")]
    paths[0].write_text(json.dumps(model), encoding="utf-8")
    paths[1].write_text(json.dumps(plant.scaffold(model)), encoding="utf-8")
    paths[2].write_text(json.dumps({"format": 1, "connections": {}}), encoding="utf-8")
    assert twin.main(["once", str(paths[0]), "--map", str(paths[1]), "--connections",
                      str(paths[2]), "--out", str(tmp_path / "o"), "--horizon", "10",
                      "--for", "0"]) == 1
    doc = twin.read_result(tmp_path / "o")[0]
    assert [(s["name"], s["state"]) for s in doc["sources"]] == [("plc", "refused")]
    assert "E_DATA_CONNECTION" in codes(doc) and doc["forecast"]["status"] == "skipped"
    assert "broker" not in json.dumps(doc["sources"])       # a source row names no connection


def test_naga_twin_is_an_engine_command():
    from nagasim import cli

    assert cli.COMMANDS["twin"][:2] == ("twin", "main")
    assert "twin" not in cli._PRODUCT_COMMAND_NAMES


# ================================================================ the agent tools
TWIN_TOOLS = ("get_twin_monitor", "get_twin_forecast")


def test_an_agent_reads_the_twin_the_person_started_and_can_choose_nothing(tmp_path):
    import argparse

    from nagasim import agent_tools as T
    from nagasim import mcp_server

    for name in TWIN_TOOLS:                 # no argument at all: nothing to choose with
        assert T.TOOLS[name].read_only and T.TOOLS[name].input_schema["properties"] == {}
    T.use_workspace(T.Workspace(tmp_path))
    for name in TWIN_TOOLS:
        error = T.call(name)["error"]
        assert error["code"] == "E_NO_TWIN" and "naga mcp --twin" in error["hint"]

    # The directory comes from the server's start flag and from nowhere else.
    parser = argparse.ArgumentParser()
    mcp_server.add_arguments(parser)
    out = tmp_path / "elsewhere" / "out"    # the person's choice: it need not be under --root
    args = parser.parse_args(["--root", str(tmp_path / "ws"), "--twin", str(out)])
    T.use_workspace(T.Workspace(args.root, twin_dir=args.twin_dir))
    assert T.call("get_twin_monitor")["error"]["code"] == "E_TWIN_RESULT"    # none written yet

    cycle, source, clock, _calls = make(out)
    cycle.step()
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    clock.tick(5.0)
    doc = cycle.step()
    monitor = T.call("get_twin_monitor")
    shown = ("cycle", "started", "cadence", "at", "model", "sources", "view", "problems")
    assert {k: monitor[k] for k in shown} == {k: doc[k] for k in shown}
    forecast = T.call("get_twin_forecast")
    assert forecast["forecast"]["state"] == "s1" and forecast["forecast"]["status"] == "ok"
    assert forecast["forecast"]["took"] == 0.0 and forecast["forecast"]["finished"]
    assert forecast["comparison"] == doc["comparison"] and len(forecast["comparison"]) == 1
    assert "advice" not in forecast and "advice" not in monitor


def test_a_tool_answer_is_bounded_whatever_the_result_file_holds(tmp_path):
    from nagasim import agent_tools as T

    twin.write_json(tmp_path / twin.RESULT_FILE, {
        "format": 1, "kind": "naga-twin", "cycle": 1, "at": 3.0,
        "model": {"name": "n" * 5000, "hash": "h"},
        "view": {"blocks": {f"b{i}": i for i in range(1500)}, "deep": [[[[[[[[1]]]]]]]]},
        "sources": [{"name": "s"}] * 1500,
        "problems": [{"code": "W_X", "message": "m" * 500}] * 1500,
        "forecast": {"status": "ok", "points": [{"at": i} for i in range(1500)],
                     "kpis": "not a list"},
        "comparison": [{"at": i} for i in range(1500)]})
    T.use_workspace(T.Workspace(tmp_path, twin_dir=tmp_path))
    monitor, forecast = T.call("get_twin_monitor"), T.call("get_twin_forecast")
    assert len(monitor["model"]["name"]) == 300 and len(monitor["view"]["blocks"]) == 200
    assert len(monitor["sources"]) == 200 and len(monitor["problems"]) == 50
    assert len(monitor["problems"][0]["message"]) == 300
    assert "[1]" not in json.dumps(monitor["view"]["deep"])         # cut at a depth
    assert len(forecast["forecast"]["points"]) == 200 and len(forecast["comparison"]) == 100
    assert len(json.dumps([monitor, forecast])) < 200_000
