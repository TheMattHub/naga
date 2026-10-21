"""An Open Industry Project run as a physics-simulated test plant (D682).

`tests/data/oip_belt_line/` holds five minutes of a box belt that OIP's physics simulator ran
(its README names the project, the licence and the commands that wrote it), a Naga model of the
same belt, and the mapping between them. The twin reads the log at one second a second here, on
a clock the test moves. Every figure the assertions use is read off the log by this file, not off
the twin: the log is the plant, the twin is what is being tested.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

from nagasim import api, cli, plant, twin

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "data" / "oip_belt_line"
TAGS = {"Belt1Run", "DischargeEye", "Running", "SinkCount", "SpawnCount", "SpawnerRun",
        "Start", "Stop"}


def read_fixture():
    model = api.load_layout(FIXTURE / "line.json")
    mapping, problems = plant.load(FIXTURE / "line.plantmap.json")
    log, found = plant.read_log(FIXTURE / "plant.jsonl")
    assert problems == [] and found == []
    return model, mapping, log


def said(log, key, at):
    """What the plant's *key* said last at or before *at* (the log is the plant)."""
    return [line["value"] for line in log if line["key"] == key and line["source_time"] <= at][-1]


class Clock:
    """Wall and monotonic clocks the test moves together, as a real second would."""

    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 0.0

    def to(self, mono):
        self.wall += mono - self.mono
        self.mono = mono


def play(log, model, mapping, start, horizon):
    """A twin on the log played at one second a second; it forecasts at *start* (log seconds)
    and is stepped just after each stated time of that forecast. (first result, last comparison)."""
    clock = Clock()
    feed = twin.LogFeed(log, speed=1.0, monotonic=lambda: clock.mono)
    source = twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock)
    cycle = twin.Cycle(source, horizon=horizon, replications=30, seed=3, forecast_every=1e9,
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    clock.to(start)
    first = cycle.step()
    assert first["forecast"]["status"] == "ok", first["forecast"]
    for at in sorted({p["at"] for p in first["forecast"]["points"]}):
        clock.to(at + 0.01)
        later = cycle.step()
    return first, [row for row in later["comparison"] if row["series"].startswith("block/")]


COUNTER = {"block/Belt/out": "SinkCount", "block/Infeed/out": "SpawnCount"}


# ---------------------------------------------------------------- the fixture
def test_the_fixture_is_a_plant_log_its_mapping_and_model_accept():
    model, mapping, log = read_fixture()
    assert [p.code for p in plant.check(mapping, model)] == []
    assert len(log) == 596 and {line["key"] for line in log} == TAGS
    assert {line["source"] for line in log} == {"oip"}
    stamps = [line["source_time"] for line in log]
    assert stamps == sorted(stamps) and stamps[0] == 0.0 and stamps[-1] <= 300.0


def test_the_log_says_what_the_oip_run_did():
    """Read off the OIP trace by hand (README): the spawner is gated by Start at 1.017 s, the
    operator's Stop at 150.017 s and Start at 165.017 s stop and restart the belt."""
    _model, _mapping, log = read_fixture()
    changes = lambda key: [(line["source_time"], line["value"]) for line in log if line["key"] == key]
    assert changes("Belt1Run") == [(0.0, False), (1.017, True), (150.017, False), (165.017, True)]
    # A count starts at 0 and goes up by one a box: 144 boxes spawned, 143 into the sink.
    for key, last in (("SpawnCount", 144), ("SinkCount", 143)):
        values = [value for _t, value in changes(key)]
        assert values == list(range(last + 1))
    assert changes("SpawnCount")[1] == (1.05, 1) and changes("SinkCount")[1] == (4.9, 1)
    # Nothing is counted while the belt is stopped, until it has run again for a second.
    for key, before, after in (("SpawnCount", 149.333, 165.917), ("SinkCount", 149.017, 166.917)):
        times = [t for t, _value in changes(key)]
        assert before in times and after in times and not [t for t in times if before < t < after]
    # The eye is blocked for 0.6 s by each 0.6 m box at 1 m/s: 143 boxes, 286 edges and the baseline.
    eye = changes("DischargeEye")
    assert len(eye) == 1 + 2 * 143 and eye[1] == (4.0, True) and eye[2] == (4.6, False)


def test_the_mapper_counts_what_the_plant_counted():
    model, mapping, log = read_fixture()
    _events, mapper = plant.replay(mapping, model, log)
    counted = mapper.counters()
    assert {name: row["total"] for name, row in counted.items()} == {"Belt": 143, "Infeed": 144}
    assert all(row["since"] == 0.0 and row["resets"] == 0 for row in counted.values())
    # Six keys of the eight say nothing the mapping reads: counted, not dropped as errors.
    assert mapper.report()["counts"]["unread"] == sum(
        1 for line in log if line["key"] not in ("SinkCount", "SpawnCount"))


# ---------------------------------------------------------------- the converter
def test_the_converter_writes_log_lines_from_an_oip_trace(tmp_path):
    sys.path.insert(0, str(ROOT))
    try:
        convert = importlib.import_module("tools.oip_trace_to_log")
    finally:
        sys.path.remove(str(ROOT))
    trace = [{"tag": "Eye", "start": False, "changes": [{"t": 4.0, "v": True}, {"t": 4.6000001, "v": False}]},
             {"tag": "Count", "start": 0, "changes": [{"t": 4.9, "v": 1}]}]
    document = {"ok": True, "tests": [{"name": "record", "trace": trace}]}
    path = tmp_path / "trace.json"
    path.write_text("note: tests/record.test.json is not listed\n" + json.dumps(document), encoding="utf-8")
    assert convert.read_trace(path) == trace
    got = [(line["key"], line["value"], line["source_time"], line["received"])
           for line in convert.to_lines(trace)]
    assert got == [("Count", 0, 0.0, 0.0), ("Eye", False, 0.0, 0.0), ("Eye", True, 4.0, 4.0),
                   ("Eye", False, 4.6, 4.6), ("Count", 1, 4.9, 4.9)]
    assert {line["source"] for line in convert.to_lines(trace, "plc")} == {"plc"}
    out = tmp_path / "plant.jsonl"
    convert.main([str(path), str(out)])
    assert plant.read_log(out)[0] == convert.to_lines(trace)
    path.write_text(json.dumps({"tests": [{}, {}]}), encoding="utf-8")
    with pytest.raises(SystemExit):
        convert.read_trace(path)


# ---------------------------------------------------------------- the twin
def test_a_forecast_made_while_the_belt_runs_is_set_beside_what_the_plant_then_counted():
    model, mapping, log = read_fixture()
    first, rows = play(log, model, mapping, start=100.5, horizon=30.0)
    # What the twin saw at its first step is what the log says by then.
    counters = first["view"]["counters"]
    assert (counters["Belt"]["total"], counters["Infeed"]["total"]) == (
        said(log, "SinkCount", 100.5), said(log, "SpawnCount", 100.5)) == (50, 53)
    assert first["view"]["approximate"] is True and first["forecast"]["approximate"] is True
    assert [row["series"] for row in rows].count("block/Belt/out") == 6
    for row in rows:
        # The reading is the plant's count then, less the count when the forecast started.
        key = COUNTER[row["series"]]
        assert row["observed"] == said(log, key, row["observed_at"]) - said(log, key, 100.5)
    # Arrivals are 30 a minute with a random gap: the forecast follows the plant within a box or two.
    by_time = sorted(rows, key=lambda row: row["at"])
    infeed = [row for row in by_time if row["series"] == "block/Infeed/out"]
    assert all(abs(row["observed"] - row["mean"]) <= 2.0 for row in infeed)
    assert all(row["q05"] <= row["mean"] <= row["q95"] for row in rows)
    assert infeed[-1]["mean"] == pytest.approx(30 / 2, abs=1.5)         # 15 boxes in 30 s
    # Exits run low by the boxes that were on the belt when the forecast began, which a plant
    # that names no items does not show: about 1.8 on average, in the model's own WIP.
    belt = [row for row in by_time if row["series"] == "block/Belt/out"]
    assert all(1.0 < row["observed"] - row["mean"] < 3.5 for row in belt)


def test_a_forecast_does_not_know_the_operator_stopped_the_line():
    model, mapping, log = read_fixture()
    _first, rows = play(log, model, mapping, start=140.5, horizon=30.0)
    for series, key in COUNTER.items():
        mine = sorted((row for row in rows if row["series"] == series), key=lambda row: row["at"])
        assert len(mine) == 6
        # Between the stop (150.017 s) and a second after the restart, the plant counted nothing.
        stopped = [row["observed"] for row in mine if 150.0 < row["observed_at"] < 165.6]
        assert len(stopped) == 4 and len(set(stopped)) == 1
        # The model runs on: at the last point it is far above what the plant counted.
        last = mine[-1]
        assert last["observed"] == said(log, key, last["observed_at"]) - said(log, key, 140.5)
        assert last["observed"] < last["q05"] - 2 and last["mean"] > 11


def test_naga_twin_once_reads_the_whole_log_and_forecasts_from_its_end(tmp_path, capsys):
    out = tmp_path / "twin_out"
    try:
        code = cli.main(["twin", "once", str(FIXTURE / "line.json"), "--map",
                         str(FIXTURE / "line.plantmap.json"), "--log", str(FIXTURE / "plant.jsonl"),
                         "--out", str(out), "--horizon", "30", "--replications", "10"])
    except SystemExit as stop:
        code = stop.code
    assert code == 0, capsys.readouterr().out
    doc, problems = twin.read_result(out)
    assert problems == [] and doc["forecast"]["status"] == "ok"
    assert {name: row["total"] for name, row in doc["view"]["counters"].items()} == {
        "Belt": 143, "Infeed": 144}
    assert doc["mapped"]["observations"] == 596 and doc["sources"][0]["state"] == "live"


# ---------------------------------------------------------------- the live route
# The recipe in docs/digital-twin.md ("The live route"), which has NOT been run against OIP
# desktop: only what needs no server and no asyncua is checked here.
LIVE_CONNECTIONS = {"format": 1, "connections": {"oip": {
    "kind": "opcua", "url": "opc.tcp://127.0.0.1:4843", "private": True, "root": "ns=1;"}}}
LIVE_SOURCE = {"name": "oip", "kind": "opcua", "connection": "oip", "options": {"interval_ms": 500},
               "location": "ns=1;s=/SpawnCount\nns=1;s=/SinkCount"}
LIVE_MAP = {"format": 1, "time": {"epoch": "2026-10-22T09:00:00+00:00", "unit": "seconds"}, "signals": [
    {"source": "oip", "key": "ns=1;s=/SpawnCount", "as": "counter", "block": "Infeed"},
    {"source": "oip", "key": "ns=1;s=/SinkCount", "as": "counter", "block": "Belt"}]}


def test_the_live_opcua_recipe_passes_every_check_that_needs_no_server(tmp_path):
    from nagasim import netguard
    from nagasim.readers import opcua

    def connection(**change):
        document = json.loads(json.dumps(LIVE_CONNECTIONS))
        document["connections"]["oip"].update(change)
        path = tmp_path / "oip.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return netguard.load_connections(path)["oip"]

    conn = connection()
    assert [str(ip) for ip in netguard.vet_connection(conn)] == ["127.0.0.1"]
    assert opcua.check(LIVE_SOURCE, conn)["nodes"] == ["ns=1;s=/SpawnCount", "ns=1;s=/SinkCount"]
    model, _mapping, _log = read_fixture()
    assert plant.check(LIVE_MAP, dict(model, data_sources=[LIVE_SOURCE])) == []
    # Loopback is the caller's to allow: without "private" the connection is refused, and a node
    # outside the root is refused whatever the model says.
    with pytest.raises(netguard.NetworkRefused) as refused:
        netguard.vet_connection(connection(private=False))
    assert refused.value.code == "E_DATA_URL_PRIVATE"
    with pytest.raises(Exception) as outside:
        opcua.check(dict(LIVE_SOURCE, location="ns=2;s=/SinkCount"), conn)
    assert outside.value.code == "E_DATA_SOURCE_LOCATION"
