"""Predict and score, with plant counters (PHASE7 7.14, D288-D297).

Counters: `plant.py` and the engine's `block/<name>/out` series. Scores: `score.py`, with
oracles worked by hand. The exit check: a stored score equals its recomputation from the log.
"""
import json
from pathlib import Path

import pytest

import nagasim
from conftest import chain, create, dispose, layout, process
from test_twin_cycle import make
from nagasim import api, monitor, plant, score, twin
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder


def slow_line(**sections):
    """Arrivals every 2 from t = 2, a service of 3, one server: the queue grows."""
    c, p, d = create("c", dist_params={"value": 2.0}), process("p", value=3.0), dispose("d")
    sections.setdefault("simulation", {"time_limit": 40, "warmup": 0, "replications": 1,
                                       "time_unit": "seconds"})
    sections.setdefault("resources", [{"name": "Server", "capacity": 1}])
    return layout([c, p, d], chain(c, p, d), **sections)


def left(events, block, t):
    """Items that left *block* by *t* in a trace: moved on, or ended there."""
    return sum(1 for e in events if e.block == block and e.t <= t and (
        e.kind == "entity_disposed" or e.kind == "entity_moved" and e.target not in (None, block)))


# ---------------------------------------------------------------- the engine's series
def test_departures_are_sampled_only_for_the_blocks_a_model_names():
    plain = api.run_model(slow_line(), replications=1, seed=1)
    assert not [s for s in plain["timeseries"]["Series"].unique() if s.startswith("block/")]
    model = slow_line()
    model["simulation"]["timeseries_counts"] = ["p", "d"]
    recorder = TraceRecorder()
    counted = api.run_model(model, replications=1, seed=1, trace=recorder)
    series = counted["timeseries"]
    assert sorted(s for s in series["Series"].unique() if s.startswith("block/")) == [
        "block/d/out", "block/p/out"]
    for block in ("p", "d"):
        rows = series[series["Series"] == f"block/{block}/out"]
        assert len(rows) > 100 and rows["Value"].iloc[-1] > 5
        # Sampled between two events: a departure at exactly a sample time is on either side.
        for t, value in zip(rows["Time"], rows["Value"]):
            assert left(recorder.events, block, t - 1e-9) <= value <= left(recorder.events, block, t)
    # Counting changes nothing else: the same run, the same numbers.
    assert plain["system"].equals(counted["system"])


def test_a_run_from_a_state_counts_departures_from_its_clock():
    from nagasim import state

    model = slow_line()
    doc = state.take(model, at=20.5, seed=1)
    started, refused = state.start(model, doc)
    assert refused == []
    started["simulation"]["timeseries_counts"] = ["p"]
    series = api.run_model(started, replications=1, seed=1)["timeseries"]
    rows = series[series["Series"] == "block/p/out"]
    # A service of 3 on one server: from 20.5 to 40 six or seven jobs end, not the thirteen
    # of the whole run.
    assert rows["Time"].min() == 20.5 and rows["Value"].iloc[0] == 0
    assert 6 <= rows["Value"].iloc[-1] <= 7


# ---------------------------------------------------------------- the counter signal
EPOCH = {"epoch": "1970-01-01T00:00:00+00:00", "unit": "seconds"}


def counting(*signals, model=None):
    return plant.Mapper({"format": 1, "time": EPOCH, "signals": list(signals)}, model)


def said(key, value, t):
    return {"source": "plc", "key": key, "value": value, "source_time": t, "received": t}


def test_a_counter_is_checked_against_the_model_and_told_once():
    model = slow_line()

    def codes(*signals):
        return [p.code for p in plant.check({"format": 1, "time": EPOCH,
                                             "signals": list(signals)}, model)]
    counter = {"key": "parts", "as": "counter", "block": "p"}
    assert codes(counter) == []
    # What is in a block and what has left it are two things: both may be told.
    assert codes(counter, {"key": "n", "as": "queue", "block": "p"}) == []
    assert codes(counter, {"key": "other", "as": "counter", "block": "p"}) == ["E_PLANTMAP_DOUBLE"]
    assert codes({**counter, "block": "nowhere"}) == ["E_PLANTMAP_NAME"]
    assert codes({**counter, "key": "parts/{id}"}) == ["E_PLANTMAP_FORMAT"]
    assert codes({**counter, "type": "A"}) == ["E_PLANTMAP_FORMAT"]     # no counter per type
    for bad in (1, 0, -8, 2.5, "65536", True):
        assert codes({**counter, "rollover": bad}) == ["E_PLANTMAP_FORMAT"], bad
    assert codes({**counter, "rollover": 65536}) == []
    assert "counter" in plant.schema()["properties"]["signals"]["items"]["properties"]["as"]["enum"]


def test_a_counter_counts_from_where_it_is_first_seen_and_makes_no_item():
    mapper = counting({"key": "parts", "as": "counter", "block": "p"})
    # First seen mid-run at 1200: a baseline, not 1200 parts.
    assert mapper.feed([said("parts", 1200, 10.0)]) == []
    assert mapper.counters() == {"p": {"total": 0, "since": 10.0, "resets": 0}}
    events = mapper.feed([said("parts", 1203, 20.0), said("parts", 1203, 20.0),    # twice
                          said("parts", 1203, 25.0), said("parts", 1204.0, 30.0)])
    assert events == [] and mapper.counters()["p"] == {"total": 4, "since": 10.0, "resets": 0}
    assert mapper.snapshot()["counters"] == mapper.counters() and mapper.snapshot()["entities"] == []
    # What is no count is dropped and counted.
    mapper.feed([said("parts", -1, 31.0), said("parts", 1204.5, 32.0), said("parts", "many", 33.0),
                 said("parts", None, 34.0), said("parts", True, 35.0)])
    assert mapper.counters()["p"]["total"] == 4 and mapper.report()["counts"]["value"] == 5


def test_a_counter_that_goes_back_adds_nothing_unless_it_is_declared_to_roll_over():
    mapper = counting({"key": "parts", "as": "counter", "block": "p"},
                      {"key": "wrap", "as": "counter", "block": "d", "rollover": 100})
    mapper.feed([said("parts", 105, 1.0), said("parts", 110, 2.0),
                 said("parts", 103, 3.0),       # a correction, or a reset: not 103 parts
                 said("parts", 104, 4.0)])
    assert mapper.counters()["p"] == {"total": 6, "since": 1.0, "resets": 1}
    assert mapper.report()["counts"]["counter_back"] == 1
    assert any(p.code == "W_PLANT_CONTRADICTION" and "went backwards" in p.message
               for p in mapper.problems())
    # A declared rollover: 98 -> 3 is five parts.
    mapper.feed([said("wrap", 98, 5.0), said("wrap", 3, 6.0), said("wrap", 4, 7.0)])
    assert mapper.counters()["d"] == {"total": 6, "since": 5.0, "resets": 0}
    # A value the declared counter cannot hold is no rollover.
    mapper.feed([said("wrap", 500, 8.0), said("wrap", 2, 9.0)])
    assert mapper.counters()["d"]["resets"] == 1


def test_a_mock_plant_counts_what_the_run_made_and_the_state_carries_no_count():
    from nagasim import state

    model = slow_line()
    mapping = plant.scaffold(model, counters=True)
    mapping["time"] = EPOCH
    assert plant.check(mapping, model) == []
    recorder = TraceRecorder()
    api.run_model(model, replications=1, seed=1, trace=recorder, record_logs=False)
    log = plant.log_of(recorder.events, mapping)
    _events, mapper = plant.replay(mapping, model, log)
    for block in ("c", "p", "d"):
        assert mapper.counters()[block]["total"] == left(recorder.events, block, 40.0) > 5
    # Counters change neither the timeline nor the state a forecast starts from.
    plain = plant.scaffold(model)
    plain["time"] = EPOCH
    events, without = plant.replay(plain, model, plant.log_of(recorder.events, plain))
    assert [(e.t, e.kind, e.entity_id) for e in events] == [(e.t, e.kind, e.entity_id) for e in _events]
    assert state.from_plant(model, mapper.snapshot())[0]["hash"] \
        == state.from_plant(model, without.snapshot())[0]["hash"]


def test_naga_plant_init_offers_counters(tmp_path, capsys):
    import json

    path = tmp_path / "line.json"
    path.write_text(json.dumps(slow_line()), encoding="utf-8")
    assert plant.main(["init", str(path), "--counters"]) == 0
    signals = json.loads(capsys.readouterr().out)["signals"]
    assert {s["block"] for s in signals if s["as"] == "counter"} == {"c", "p", "d"}
    assert plant.main(["init", str(path)]) == 0
    assert "counter" not in capsys.readouterr().out


# ---------------------------------------------------------------- counters in the twin
class Clock:
    wall, mono = 1_800_000_000.0, 0.0


def playing(model, **scaffold):
    """(a plant source on the model's own mock log, played at one second a second on a
    clock the test moves; that clock; the run's trace; the mapping; the log)."""
    mapping = plant.scaffold(model, **scaffold)
    mapping["time"] = EPOCH
    recorder = TraceRecorder()
    api.run_model(model, replications=1, seed=1, trace=recorder, record_logs=False)
    log = plant.log_of(recorder.events, mapping)
    clock = Clock()
    feed = twin.LogFeed(log, speed=1.0, monotonic=lambda: clock.mono)
    source = twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock)
    return source, clock, recorder.events, mapping, log


def cycle_on(source, clock, tmp_path, **more):
    return twin.Cycle(source, clock=lambda: clock.wall, monotonic=lambda: clock.mono,
                      out_dir=tmp_path, **{"horizon": 12.0, "replications": 2, "seed": 3,
                                           "forecast_every": 1000.0, **more})


def test_a_counted_block_is_observed_and_compared_as_its_increase_since_the_forecast(tmp_path):
    source, clock, events, _mapping, _log = playing(slow_line(), counters=True)
    cycle = cycle_on(source, clock, tmp_path)
    clock.mono = 10.5
    doc = cycle.step()
    assert doc["view"]["counters"]["p"] == {"total": left(events, "p", 10.5), "since": events[0].t,
                                            "resets": 0}
    assert doc["view"]["approximate"] is False
    forecast = doc["forecast"]
    counted = [p for p in forecast["points"] if p["series"] == "block/p/out"]
    # #365 stands: a forecast late on the clock has its six stated points a series.
    assert len(counted) == 6 and {p["series"] for p in forecast["points"]} == {
        "system/WIP", "resource/Server/in_service", "block/c/out", "block/p/out", "block/d/out"}
    # The start of a count: the total, and whose count it is (since when, lost how often).
    assert forecast["base"]["block/p/out"] == [float(left(events, "p", 10.5)), [events[0].t, 0]]
    for point in counted:
        clock.mono = point["at"] + 0.25
        rows = [r for r in cycle.step()["comparison"]
                if r["series"] == "block/p/out" and r["at"] == point["at"]]
        # Observed: what the plant's counter added since the forecast started.
        assert rows[0]["observed"] == left(events, "p", clock.mono) - left(events, "p", 10.5)
        # A constant line: the forecast is the plant, but for the service drawn afresh.
        assert abs(rows[0]["mean"] - rows[0]["observed"]) <= 1


def test_a_count_is_not_compared_across_a_reset_nor_without_a_start(tmp_path):
    source, clock, _events, _mapping, _log = playing(slow_line(), counters=True)
    cycle = cycle_on(source, clock, tmp_path)
    clock.mono = 10.5
    points = cycle.step()["forecast"]["points"]
    first = min(p["at"] for p in points)
    # The counter of p loses count before the first point's time; the others go on.
    source.mapper.feed([{**said("block/p/out", 0, 11.0), "source": "plant"}])
    clock.mono = first + 0.25
    doc = cycle.step()
    assert doc["view"]["counters"]["p"]["resets"] == 1
    compared = {r["series"] for r in doc["comparison"]}
    assert "block/d/out" in compared and "block/p/out" not in compared
    history = twin._read(tmp_path / twin.HISTORY_FILE, twin.KIND + "-history", 10 ** 7)
    lost = [p for p in history["forecasts"][0]["points"] if p["series"] == "block/p/out"][0]
    assert lost["missed"] is True and lost["lost"] is True      # said apart from "late"
    assert history["forecasts"][0]["score"]["lost"] == 1 == doc["scores"]["basis"]["lost"]
    assert doc["scores"]["basis"]["missed"] == 0
    # A record with no start for a counted series (a counter first seen later): missed too.
    del cycle._history[0]["base"]["block/d/out"]
    clock.mono = first + 12.0 / 6 + 0.25
    assert not [r for r in cycle.step()["comparison"]
                if r["series"] == "block/d/out" and r["at"] > first]


# ---------------------------------------------------------------- scores: oracles
def test_a_point_is_scored_as_worked_by_hand():
    # A band of 2 to 6 around a mean of 4.
    assert score.point(4, 2, 6, 5) == {"inside": True, "error": 1.0,
                                       "pinball": pytest.approx(0.1)}     # 0.05 * (6 - 2) / 2
    # The mean of the two quantile losses: 1/40 of the interval score, (6 - 2) + 20 * miss.
    # Two above the band: the width as before, and the miss in full.
    assert score.point(4, 2, 6, 8) == {"inside": False, "error": 4.0,
                                       "pinball": pytest.approx((0.05 * 4 + 2) / 2)}
    assert score.point(4, 2, 6, 0)["pinball"] == pytest.approx((0.05 * 4 + 2) / 2)
    assert score.point(4, 2, 6, 2)["inside"] and score.point(4, 2, 6, 6)["inside"]   # the edges
    for missing in ((None, 2, 6, 5), (4, None, 6, 5), (4, 2, float("nan"), 5), (4, 2, 6, None),
                    (4, 2, 6, float("inf")), (4, 2, 6, "5")):
        assert score.point(*missing) is None


def uniform(q05, q95, n=1000):
    """The rows of a forecast that states *q05* and *q95* for a plant that is uniform on
    0 to 1, seen at *n* evenly spread values: one point a forecast, at its first lead."""
    totals = score.Totals("m", 60.0, "then")
    for i in range(n):
        record = {"model": "m", "horizon": 60.0, "from": 0.0, "seed": i}
        found = {"series": "system/WIP", "at": 10.0, "mean": 0.5, "q05": q05, "q95": q95,
                 "observed": (i + 0.5) / n, "observed_at": 10.0 + i}
        totals.count(record)
        totals.add(record, found)
    return totals


def test_a_known_distribution_scores_what_its_analysis_says():
    """Uniform on 0 to 1, forecast exactly: coverage 0.9, no bias, E|U - 1/2| = 1/4, and a
    pinball loss of tau (1 - tau) / 2 at each quantile."""
    totals = uniform(0.05, 0.95)
    [row] = totals.rows()
    assert (row["series"], row["lead"], row["points"], row["missed"], row["lost"], row["whole"]) \
        == ("system/WIP", 10.0, 1000, 0, 0, False)
    assert row["coverage"] == 0.9
    assert row["bias"] == pytest.approx(0.0, abs=1e-12) and row["error"] == pytest.approx(0.25)
    assert row["pinball"] == pytest.approx(0.05 * 0.95 / 2)
    assert totals.basis() == {"forecasts": 1000, "same_seed": 0, "points": 1000, "missed": 0, "lost": 0,
                              "since": "then", "first": 10.0, "last": 1009.0}
    # A band half as wide covers half and is not rewarded for it: the score is proper.
    # E rho_tau(U - q) = tau (1/2 - q) + q^2 / 2: 0.04375 at both ends.
    [narrow] = uniform(0.25, 0.75).rows()
    assert narrow["coverage"] == 0.5 and narrow["pinball"] == pytest.approx(0.04375)
    # So is a band so wide that it covers everything.
    [wide] = uniform(-1.0, 2.0).rows()
    assert wide["coverage"] == 1.0 and wide["pinball"] > row["pinball"]


def test_scores_are_kept_by_lead_and_a_missed_point_is_counted_not_scored():
    totals = score.Totals("m", 60.0, "then")
    record = {"model": "m", "horizon": 60.0, "from": 100.0, "seed": 1}
    stated = {"series": "level/Tank", "mean": 4.0, "q05": 2.0, "q95": 6.0}
    totals.count(record)
    totals.add(record, {**stated, "at": 110.0, "observed": 5.0, "observed_at": 111.0})
    totals.add(record, {**stated, "at": 130.2, "observed": 9.0, "observed_at": 131.0})
    totals.add(record, {**stated, "at": 160.0, "observed": None, "observed_at": None, "missed": True})
    totals.add(record, {**stated, "at": 150.0, "observed": None, "observed_at": None})  # pending
    assert [(r["lead"], r["points"], r["missed"], r["coverage"], r["bias"]) for r in totals.rows()] \
        == [(10.0, 1, 0, 1.0, 1.0), (30.0, 1, 0, 0.0, 5.0), (60.0, 0, 1, None, None)]
    assert score.lead(record, {"at": 100.0}) == 1 and score.lead(record, {"at": 999.0}) == 6
    assert score.lead({"horizon": 60.0}, {"at": 110.0}) is None
    assert score.of({"points": [{**stated, "observed": 5.0}, {**stated, "observed": 9.0},
                                {**stated, "observed": None, "missed": True},
                                {**stated, "observed": None}]}) \
        == {"points": 2, "inside": 1, "missed": 1, "lost": 0}
    # The forecasts of another model, or of another horizon, are not this score's.
    assert totals.takes(record) and not totals.takes({**record, "model": "x"})
    assert not totals.takes({**record, "horizon": 30.0})
    # A forecast on the seed of the one before is not an independent sample: counted.
    totals.count({**record, "seed": 1})
    totals.count({**record, "seed": 4})
    assert (totals.forecasts, totals.same_seed) == (3, 1)


# ---------------------------------------------------------------- scores in the cycle
def test_a_cycle_scores_each_point_once_with_its_basis_or_the_reason_for_none(tmp_path):
    cycle, source, clock, _calls = make(tmp_path)
    doc = cycle.step()                      # a forecast from t = 0: points at 10 ... 60
    assert doc["scores"] == {"nominal": 0.9, "rows": [], "reason": "none_due", "basis": {
        "forecasts": 0, "same_seed": 0, "points": 0, "missed": 0, "lost": 0,
        "since": doc["started"], "first": None, "last": None}}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin_history.json"]
    source.ready = False
    source.at, source.values = 12.0, {"system/WIP": 5.0}    # inside 2 to 6, one over the mean
    doc = cycle.step()
    assert doc["forecast"]["score"] == {"points": 1, "inside": 1, "missed": 0, "lost": 0}
    assert doc["scores"]["rows"] == [{"series": "system/WIP", "lead": 10.0, "points": 1,
                                      "missed": 0, "lost": 0, "whole": True, "coverage": 1.0,
                                      "bias": 1.0, "error": 1.0, "pinball": pytest.approx(0.1)}]
    assert "reason" not in doc["scores"]
    assert doc["scores"]["basis"] == {"forecasts": 1, "same_seed": 0, "points": 1, "missed": 0, "lost": 0,
                                      "since": doc["started"], "first": 12.0, "last": 12.0}
    source.at = 13.0
    assert cycle.step()["scores"] == doc["scores"]          # once, never again
    # Another process finds the score and goes on from it; what it did not see is missed.
    again, source, _clock, _calls = make(tmp_path, source=source)
    source.at, source.values = 52.0, {"system/WIP": 9.0}    # three over the band
    doc = again.step()
    assert [(r["lead"], r["points"], r["missed"], r["coverage"]) for r in doc["scores"]["rows"]] \
        == [(10.0, 1, 0, 1.0), (20.0, 0, 1, None), (30.0, 0, 1, None), (40.0, 0, 1, None),
            (50.0, 1, 0, 0.0)]
    assert doc["scores"]["rows"][-1]["bias"] == 5.0
    assert doc["scores"]["rows"][-1]["pinball"] == pytest.approx((0.05 * 4 + 3) / 2)
    # One document holds the forecasts and the score that sums them: one write, never torn.
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))
    assert (history["scores"]["forecasts"], history["scores"]["model"]) == (1, "m" * 64)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["twin.json", "twin_history.json"]
    assert history["forecasts"][0]["score"] == {"points": 2, "inside": 1, "missed": 3, "lost": 0}
    # The second process forecast on the same base seed: said, with the basis.
    source.ready, source.at = True, 115.0
    again.step()                            # as each scheduled `naga twin once` does
    source.at, source.values = 126.0, {"system/WIP": 4.0}
    basis = again.step()["scores"]["basis"]
    assert (basis["forecasts"], basis["same_seed"], basis["points"], basis["missed"]) == (2, 1, 3, 4)


def test_no_score_says_why(tmp_path):
    cycle, source, _clock, _calls = make(tmp_path)
    source.refuse = twin.problem("E_TWIN_STATE", "no")
    assert cycle.step()["scores"]["reason"] == "no_forecast"

    class Unobserved(type(source)):
        def observe(self):
            seen = super().observe()
            del seen["observed"]
            return seen
    cycle, _source, _clock, _calls = make(tmp_path / "mes", source=Unobserved())
    assert cycle.step()["scores"]["reason"] == "not_observed"


def test_a_score_that_is_not_this_twins_own_is_started_again_and_never_in_silence(tmp_path):
    cycle, source, _clock, _calls = make(tmp_path)
    cycle.step()
    source.ready = False
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    cycle.step()
    path = tmp_path / twin.HISTORY_FILE
    history = json.loads(path.read_text(encoding="utf-8"))
    good = history["scores"]

    def loaded(doc, horizon=60.0):
        totals, why = score.Totals.load(doc, "m" * 64, horizon, "now")
        return totals.basis()["points"], why
    assert loaded(good) == (1, None) and loaded(None) == (0, "absent")
    assert loaded(good, horizon=30.0) == (0, "horizon")     # another horizon: another score
    assert loaded({**good, "model": "x" * 64}) == (0, "model")
    row = good["rows"]["system/WIP"][0]
    for damage in ({"forecasts": -1}, {"forecasts": 1.5}, {"same_seed": 9}, {"since": 5},
                   {"seed": "7"}, {"first": "soon"}, {"rows": []}, {"rows": {"s": [row] * 5}},
                   {"rows": {"s": [[1, 2, 0.0, 0.0, 0.0, 0, 0, 0]] * 6}},   # more inside than scored
                   {"rows": {"s": [[1, 1, float("nan"), 0.0, 0.0, 0, 0, 0]] * 6}},
                   {"rows": {"s": [[1, 1, 0.0, -1.0, 0.0, 0, 0, 0]] * 6}},
                   {"rows": {"s": [[True, 0, 0.0, 0.0, 0.0, 0, 0, 0]] * 6}},
                   {"rows": {"s": [[1, 1, 0.0, 0.0, 0.0, 0, 0, 2]] * 6}},
                   {"rows": {"s": [[1, 1, 1e200, 0.0, 0.0, 0, 0, 0]] * 6}},     # sums stay finite
                   {"rows": {"s": [[10 ** 400, 1, 0.0, 0.0, 0.0, 0, 0, 0]] * 6}},
                   {"rows": {"s": [[1, 1, 0.0, 0.0, 0.0, 0, 0]] * 6}},
                   {"horizon": "long"}, {"model": 5},
                   {"rows": {f"s{i}": [row] * 6 for i in range(51)}}):
        assert loaded({**good, **damage}) == (0, "unsound"), damage
    assert loaded({**good, "horizon": 10 ** 400}) == (0, "unsound")
    # A row of the first build (six sums) is still read.
    assert loaded({**good, "rows": {"s": [[2, 1, 0.5, 0.5, 0.1, 1]] * 6}}) == (12, None)

    def restarted(**more):
        again = twin.Cycle(source, runner=lambda *a, **k: 1 / 0, out_dir=tmp_path,
                           **{"horizon": 60.0, **more})
        first, second = again.step(), again.step()
        return ([p["message"] for p in first["problems"] if p["code"] == "W_TWIN_SCORE"],
                [p for p in second["problems"] if p["code"] == "W_TWIN_SCORE"],
                first["scores"]["basis"]["points"])
    # The same twin again: its score goes on, and nothing is said.
    assert restarted() == ([], [], 1)
    # Another horizon: the score starts again, said once, in the first result only.
    said, again, points = restarted(horizon=30.0)
    assert len(said) == 1 and "another horizon" in said[0] and again == [] and points == 0
    history["scores"] = {**good, "rows": "none"}
    twin.write_json(path, history)
    assert "cannot be read" in restarted()[0][0]
    history["scores"] = {**good, "model": "x" * 64}
    twin.write_json(path, history)
    assert "another model" in restarted()[0][0]
    # A history with no score in it (a twin of 7.12): nothing to say.
    del history["scores"]
    twin.write_json(path, history)
    assert restarted()[:2] == ([], [])


def test_a_first_builds_score_file_is_read_once_and_never_written(tmp_path):
    cycle, source, _clock, _calls = make(tmp_path)
    cycle.step()
    path = tmp_path / twin.HISTORY_FILE
    history = json.loads(path.read_text(encoding="utf-8"))
    del history["scores"]
    twin.write_json(path, history)
    old = {"format": 1, "kind": "naga-twin-scores", "model": "m" * 64, "horizon": 60.0,
           "since": "2026-10-10T08:00:00+00:00", "forecasts": 4, "same_seed": 0, "seed": 7,
           "first": 1.0, "last": 9.0, "rows": {"system/WIP": [[2, 1, 0.5, 0.5, 0.1, 1]] * 6}}
    (tmp_path / score.LEGACY_FILE).write_text(json.dumps(old), encoding="utf-8")
    kept = (tmp_path / score.LEGACY_FILE).read_bytes()
    again, source, _clock, _calls = make(tmp_path, source=source)
    source.ready = False
    source.at, source.values = 12.0, {"system/WIP": 5.0}
    basis = again.step()["scores"]["basis"]
    assert (basis["forecasts"], basis["points"], basis["since"]) == (5, 13, old["since"])
    assert (tmp_path / score.LEGACY_FILE).read_bytes() == kept
    # From now on the history has the sums, and the old file is not looked at again.
    (tmp_path / score.LEGACY_FILE).write_text("{", encoding="utf-8")
    third, *_ = make(tmp_path)
    assert third._scores.basis()["points"] == 13
    for text in ("", "[]", "{", "[" * 100_000, " " * (score.MAX_BYTES + 1)):
        (tmp_path / score.LEGACY_FILE).write_text(text, encoding="utf-8")
        assert score.legacy(tmp_path / score.LEGACY_FILE) is None


def poisson(mean, top=40):
    weights, weight = [], 2.718281828459045 ** -mean
    for k in range(top):
        weights.append(weight)
        weight *= mean / (k + 1)
    return weights


def test_a_band_of_whole_numbers_covers_more_than_its_nominal_share_and_is_marked():
    """Not a defect, and not to be "fixed": the 5% and 95% quantiles of a discrete variable
    overshoot, so a RIGHT forecast of a count covers more than 90%. Poisson(3): the band is
    1 to 6 and holds P(1 <= X <= 6) = 0.9665 - 0.0498 = 0.9167. A one-unit resource in
    service (Bernoulli) has the band 0 to 1 and is always inside it. The row is marked,
    and its pinball loss is the number to compare."""
    weights = poisson(3.0)
    cumulative = [sum(weights[:k + 1]) for k in range(len(weights))]
    q05 = next(k for k, c in enumerate(cumulative) if c >= 0.05)
    q95 = next(k for k, c in enumerate(cumulative) if c >= 0.95)
    assert (q05, q95) == (1, 6)
    totals = score.Totals("m", 60.0, "then")
    record = {"model": "m", "horizon": 60.0, "from": 0.0, "seed": 1}
    for value, weight in enumerate(weights):
        for _ in range(round(weight * 100_000)):
            totals.add(record, {"series": "block/p/out", "at": 10.0, "mean": 3.0, "q05": q05,
                                "q95": q95, "observed": float(value), "observed_at": 10.0})
            totals.add(record, {"series": "resource/Server/in_service", "at": 10.0, "mean": 0.7,
                                "q05": 0.0, "q95": 1.0, "observed": float(value % 2),
                                "observed_at": 10.0})
    counted, in_service = totals.rows()
    assert counted["whole"] is True and counted["coverage"] == pytest.approx(0.9167, abs=5e-4)
    assert counted["coverage"] > score.NOMINAL
    assert in_service["whole"] is True and in_service["coverage"] == 1.0
    # A continuous series is not marked, and covers its nominal share.
    [level] = uniform(0.05, 0.95).rows()
    assert level["whole"] is False and level["coverage"] == score.NOMINAL
    assert "marked *" in monitor.SCORED and "pinball loss, not its" in monitor.SCORED


# ---------------------------------------------------------------- the exit check (D295)
def busy_line():
    """Random arrivals every 5 s and services of 4 s on one server, for 400 s."""
    c = create("c", distribution="Exponential", dist_params={"rate": 0.2})
    p, d = process("p", value=0.25, distribution="Exponential"), dispose("d")
    return layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}],
                  simulation={"time_limit": 400, "warmup": 0, "replications": 1,
                              "time_unit": "seconds"})


def watched(tmp_path, until=200.0):
    """A twin on the mock plant of `busy_line`, stepped every 2.5 s with a forecast of 30 s
    every 20 s: (its last result, the model, the mapping, the log, the run's trace)."""
    model = busy_line()
    source, clock, events, mapping, log = playing(model, counters=True)
    cycle = cycle_on(source, clock, tmp_path, horizon=30.0, replications=20, forecast_every=20.0)
    clock.mono, doc = 20.0, None
    while clock.mono <= until:
        doc = cycle.step()
        clock.mono += 2.5
    return doc, model, mapping, log, events


def test_a_stored_score_equals_its_recomputation_from_the_log(tmp_path):
    """Row 7.14's exit check."""
    doc, model, mapping, log, events = watched(tmp_path)
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))["forecasts"]
    basis = doc["scores"]["basis"]
    assert len(history) == 10 and basis["forecasts"] >= 8 and basis["points"] > 200
    # Every forecast has its six stated points a series (#365), late on the clock too.
    assert all(len(r["points"]) == 6 * 5 for r in history)

    lines, differences = score.recompute(tmp_path, model, mapping, log)
    assert differences == 0, lines
    assert lines[-1] == "every stored score equals its recomputation from the log"
    assert f"the rolling score ({basis['forecasts']} forecasts) equals its recomputation" in lines

    # The same numbers a third way, with no code of the twin's: readings from the RUN's own
    # trace, and the sums by hand.
    timeline, sums = Timeline(events), {}
    for record in history:
        scored = [p for p in record["points"] if p["observed"] is not None]
        assert record["score"] == {
            "points": len(scored), "missed": sum(1 for p in record["points"] if p.get("missed")),
            "lost": 0,
            "inside": sum(p["q05"] <= p["observed"] <= p["q95"] for p in scored)}
        for found in scored:
            if found["series"] == "system/WIP":
                assert found["observed"] == sum(
                    timeline.state_at(found["observed_at"])["counts"].values())
            if found["series"] == "block/p/out":
                assert found["observed"] == left(events, "p", found["observed_at"]) \
                    - left(events, "p", record["from"])
            row = sums.setdefault((found["series"], round((found["at"] - record["from"]) / 5)),
                                  [0, 0, 0.0, 0.0])
            row[0] += 1
            row[1] += found["q05"] <= found["observed"] <= found["q95"]
            row[2] += found["observed"] - found["mean"]
            row[3] += abs(found["observed"] - found["mean"])
    rows = {(r["series"], round(r["lead"] / 5)): r for r in doc["scores"]["rows"] if r["points"]}
    assert rows.keys() == sums.keys() and len(rows) == 5 * 6
    for key, (n, inside, error, absolute) in sums.items():
        assert rows[key]["points"] == n and rows[key]["coverage"] == inside / n
        assert rows[key]["bias"] == pytest.approx(error / n)
        assert rows[key]["error"] == pytest.approx(absolute / n)
    # The model IS the plant here, so its forecasts hold: most readings are inside the band.
    assert sum(r["coverage"] * r["points"] for r in rows.values()) / basis["points"] > 0.75


def test_a_recomputation_says_every_stored_number_the_log_does_not_give(tmp_path):
    _doc, model, mapping, log, _events = watched(tmp_path)
    path = tmp_path / twin.HISTORY_FILE
    kept = path.read_text(encoding="utf-8")
    history = json.loads(kept)

    def said(**more):
        lines, differences = score.recompute(tmp_path, model, mapping, more.get("log", log))
        return differences, "\n".join(lines)
    assert said()[0] == 0
    # One stored reading is not what the log gives; then one record's score is not either.
    found = next(p for p in history["forecasts"][2]["points"] if p["observed"] is not None)
    found["observed"] = found["q95"] + 50.0
    twin.write_json(path, history)
    differences, text = said()
    assert differences == 1 and "the log gives" in text
    assert f"DIFFERENT forecast {history['forecasts'][2]['state'][:12]}, {found['series']}" in text
    history["forecasts"][4]["score"]["inside"] += 1
    twin.write_json(path, history)
    differences, text = said()
    assert differences == 2 and f"forecast {history['forecasts'][4]['state'][:12]}: its score" in text
    path.write_text(kept, encoding="utf-8")
    # A score that was edited.
    history = json.loads(kept)
    history["scores"]["rows"]["system/WIP"][0][1] -= 1
    twin.write_json(path, history)
    differences, text = said()
    assert differences == 1 and "the rolling score is not the one its stored forecasts give" in text
    # A score older than the stored forecasts is said not to be recomputed; the records are.
    history["scores"]["rows"]["system/WIP"][0][1] += 1
    history["scores"]["forecasts"] += 40
    twin.write_json(path, history)
    differences, text = said()
    assert differences == 0 and "it is not recomputed" in text
    # A quarter of the log: the readings after it are not what it gives.
    assert said(log=log[:len(log) // 4])[0] > 10
    # Not a twin's directory, or a mapping that does not fit.
    assert score.recompute(tmp_path / "nowhere", model, mapping, log)[1] == 1
    assert score.recompute(tmp_path, model, {"format": 1, "time": {}}, log)[1] == 1


def test_naga_twin_score_is_the_recomputation_as_a_command(tmp_path, capsys):
    _doc, model, mapping, log, _events = watched(tmp_path / "out", until=80.0)
    paths = [str(tmp_path / name) for name in ("line.json", "line.plantmap.json", "plant.jsonl")]
    for path, data in zip(paths, (model, mapping)):
        Path(path).write_text(json.dumps(data), encoding="utf-8")
    plant.write_log(paths[2], log)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in (tmp_path / "out").iterdir())
    command = ["score", str(tmp_path / "out"), paths[0], "--map", paths[1], "--log", paths[2]]
    assert twin.main(command) == 0
    assert "every stored score equals its recomputation from the log" in capsys.readouterr().out
    # It writes nothing.
    assert sorted((p.name, p.stat().st_mtime_ns) for p in (tmp_path / "out").iterdir()) == before
    assert [name for name, _ in before] == ["twin.json", "twin_history.json"]
    plant.write_log(paths[2], log[:40])
    assert twin.main(command) == 1
    assert "DIFFERENT" in capsys.readouterr().out


# ---------------------------------------------------------------- surfaces (D297)
def test_naga_twin_status_and_the_agent_tool_give_the_scores_with_their_basis(tmp_path, capsys):
    from nagasim import agent_tools as T

    doc, *_ = watched(tmp_path, until=80.0)
    assert twin.main(["status", str(tmp_path)]) == 0
    said = capsys.readouterr().out
    basis = doc["scores"]["basis"]
    assert f"scores: {basis['forecasts']} forecasts, {basis['points']} readings scored" in said
    assert "  system/WIP, lead 5: " in said and "coverage " in said and "not a test" in said
    assert "counted by the plant: c " in said
    T.use_workspace(T.Workspace(tmp_path, twin_dir=tmp_path))
    answer = T.call("get_twin_forecast")
    assert answer["scores"]["basis"] == basis and answer["scores"]["nominal"] == 0.9
    assert answer["scores"]["rows"] == doc["scores"]["rows"]
    assert answer["forecast"]["score"] == doc["forecast"]["score"]
    assert T.call("get_twin_monitor")["view"]["counters"] == doc["view"]["counters"]
    # Bounded whatever the file holds.
    twin.write_json(tmp_path / twin.RESULT_FILE, {
        **doc, "scores": {"rows": [{"series": "s" * 1000}] * 1000, "basis": "x" * 5000}})
    cut = T.call("get_twin_forecast")["scores"]
    assert len(cut["rows"]) <= 300 and len(cut["rows"][0]["series"]) == 300
    assert len(cut["basis"]) == 300
    twin.write_json(tmp_path / twin.RESULT_FILE, {**doc, "scores": [1, 2]})
    assert T.call("get_twin_forecast")["scores"] == {"rows": []}
    # A result with no score says why, in the monitor's words.
    empty, *_ = make(tmp_path / "new")
    empty.step()
    assert twin.main(["status", str(tmp_path / "new")]) == 0
    assert "scores: No score yet: no stated time of a stored forecast has come." \
        in capsys.readouterr().out


# ---------------------------------------------------------------- the fix round
def test_a_twin_started_again_never_reads_a_count_against_another_mappers_start(tmp_path):
    """C1 of the review: a new process has a new mapper, whose counters begin again at 0.
    The forecast the process before it left has its start under the old count: its
    points are not compared, never filled with a negative increase."""
    model = slow_line()
    source, clock, events, mapping, log = playing(model, counters=True)
    first = cycle_on(source, clock, tmp_path)
    clock.mono = 10.5
    doc = first.step()
    assert doc["forecast"]["base"]["block/p/out"][0] == left(events, "p", 10.5) > 0
    due = min(p["at"] for p in doc["forecast"]["points"] if p["series"] == "block/p/out")
    # The second process: a broker gives it what the plant says from now on, not the past.
    later = Clock()
    tail = [line for line in log if line["source_time"] >= 11.0]
    feed = twin.LogFeed(tail, speed=1.0, monotonic=lambda: later.mono)
    again = cycle_on(twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock),
                     later, tmp_path)
    later.mono = due + 0.25 - min(line["source_time"] for line in tail)
    doc = again.step()
    assert doc["view"]["counters"]["p"]["since"] >= 11.0        # another count
    assert not [r for r in doc["comparison"] if r["series"].startswith("block/")]
    assert [r for r in doc["comparison"] if r["series"] == "system/WIP"]    # the rest is compared
    rows = [r for r in doc["scores"]["rows"] if r["series"] == "block/p/out"]
    assert [(r["points"], r["missed"], r["lost"], r["bias"]) for r in rows] == [(0, 0, 1, None)]
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text(encoding="utf-8"))
    assert all(p["observed"] is None or p["observed"] >= 0
               for record in history["forecasts"] for p in record["points"])


def test_a_number_no_float_can_hold_stops_neither_a_counter_nor_a_twin(tmp_path):
    """I1 of the review (#373)."""
    mapper = counting({"key": "parts", "as": "counter", "block": "p"})
    mapper.feed([said("parts", 5, 1.0), said("parts", 1.7e308, 2.0), said("parts", 1.7e308, 3.0),
                 said("parts", 10 ** 400, 4.0), said("parts", 2 ** 53 + 2, 5.0),
                 said("parts", 7, 6.0)])
    assert mapper.counters()["p"] == {"total": 2, "since": 1.0, "resets": 0}
    assert mapper.report()["counts"]["value"] == 4
    json.dumps(mapper.snapshot(), allow_nan=False)
    assert score.point(4, 2, 6, 10 ** 400) is None and twin._num(10 ** 400) is None
    assert twin._num(-(10 ** 400)) is None and twin._num(2 ** 60) == float(2 ** 60)
    # In the twin's own files: the history's horizon, a sum of the score.
    cycle, source, _clock, _calls = make(tmp_path)
    cycle.step()
    path = tmp_path / twin.HISTORY_FILE
    history = json.loads(path.read_text(encoding="utf-8"))
    history["forecasts"][0]["horizon"] = 10 ** 400
    history["scores"]["rows"] = {"s": [[10 ** 400, 1, 0.0, 0.0, 0.0, 0, 0, 0]] * 6}
    path.write_text(json.dumps(history), encoding="utf-8")
    again, *_ = make(tmp_path)
    doc = again.step()
    assert doc["forecast"]["status"] == "ok" and "W_TWIN_SCORE" in [p["code"] for p in doc["problems"]]
    # And where a result is shown.
    shown = monitor.summary({**doc, "at": 10 ** 400, "scores": {"rows": [{"lead": 10 ** 400}],
                                                                  "basis": {"points": 10 ** 400}}})
    json.dumps(shown, allow_nan=False)
    assert "t = -" in shown["head"]


def test_the_blocks_to_count_are_validated_and_bounded():
    """I2 of the review."""
    from nagasim.core import schema
    from nagasim import modelschema

    def codes(counted):
        model = slow_line()
        model["simulation"]["timeseries_counts"] = counted
        return [p.code for p in schema.validate_model_structured(model)
                if p.code == "E_TIMESERIES_COUNTS"]
    assert codes(["p", "d"]) == [] and codes([]) == []
    assert codes(["p", "nowhere"]) == ["E_TIMESERIES_COUNTS"]
    for bad in ("p", {"p": 1}, [1, None], 5, ["p", "p"], [""], [f"b{i}" for i in range(1001)]):
        assert codes(bad) == ["E_TIMESERIES_COUNTS"], bad
    with pytest.raises(schema.ModelValidationError, match="nowhere"):
        api.run_model({**slow_line(), "simulation": {**slow_line()["simulation"],
                                                     "timeseries_counts": ["nowhere"]}},
                      replications=1, seed=1)
    text = json.dumps(modelschema.build_json_schema())
    spec = json.loads(text[text.index('"timeseries_counts": ') + 21:].split(', "description"')[0] + "}")
    assert (spec["maxItems"], spec["uniqueItems"]) == (1000, True)
    # A twin's counted names are the flattened ones: a model of assets is judged flattened.
    nested = api.load_layout(Path(nagasim.__file__).resolve().parent / "models" / "two_cell_line.json")
    flat = schema.prepare(nested)[0]
    name = next(b["name"] for b in flat["blocks"] if "/" in b["name"])
    nested["simulation"]["timeseries_counts"] = [name]
    assert not [p for p in schema.validate_model_structured(nested)
                if p.code == "E_TIMESERIES_COUNTS"]
    nested["simulation"]["timeseries_counts"] = [name + "x"]
    assert [p.code for p in schema.validate_model_structured(nested)
            if p.code == "E_TIMESERIES_COUNTS"] == ["E_TIMESERIES_COUNTS"]
