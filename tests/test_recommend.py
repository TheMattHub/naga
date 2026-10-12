"""Recommend, with the action outbox (PHASE7 7.15, D227-D230, D298-D310).

Candidates compared by simulation on common random numbers, records in the caller's outbox,
replies read back, the effect measured from the plant's counters. The exit check: a stored
recommendation reproduces from its stored state and seed, in another process.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import nagasim
from conftest import chain, create, dispose, layout, process
from test_score import Clock, busy_line, cycle_on, left, playing, slow_line
from nagasim import plant, twin


# ---------------------------------------------------------------- a twin started again
def test_a_twin_started_again_is_refused_by_name_until_it_knows_who_is_in_service(tmp_path):
    """Point 10 of the brief: in 7.14's restart test the new twin made no forecast, and
    nobody had looked at why. It joins a stream that names a job for an item its mapper
    never saw: the state contradicts itself and the start is REFUSED (D270), by name, and
    made by itself once the items it could not place have left. Designed, not a defect."""
    model = slow_line()
    _source, _clock, _events, mapping, log = playing(model, counters=True)
    later = Clock()
    tail = [line for line in log if line["source_time"] >= 11.0]
    feed = twin.LogFeed(tail, speed=1.0, monotonic=lambda: later.mono)
    again = cycle_on(twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock),
                     later, tmp_path, forecast_every=1.0)
    later.mono = 1.75
    doc = again.step()
    assert (doc["forecast"]["status"], doc["forecast"]["reason"]) == ("refused", "E_INITIAL_BUSY")
    assert "W_PLANT_CONTRADICTION" in {p["code"] for p in doc["problems"]}
    later.mono = 7.0                        # the job it could not name has ended
    assert again.step()["forecast"]["status"] == "ok"


# ---------------------------------------------------------------- candidates (D298-D302)
from nagasim import recommend, state  # noqa: E402

KPI = "timeseries/block/p/out/max"
MORE = {"verb": "set_capacity", "target": "Server", "value": 2}
SPARE = {"verb": "set_capacity", "target": "Spare", "value": 5}


def counted(model, *blocks):
    model["simulation"]["timeseries_counts"] = list(blocks or ["p"])
    model["resources"].append({"name": "Spare", "capacity": 1})
    return model


def asked(tmp_path, *actions, **more):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"format": 1, "kind": "naga-candidates", "kpi": KPI,
                                "candidates": list(actions or [MORE]), **more}), encoding="utf-8")
    return path


def test_a_candidates_file_is_closed(tmp_path):
    """D300, and the security table's row: the file is data, checked against the model."""
    model = counted(slow_line())
    got = recommend.load(asked(tmp_path, MORE, SPARE, better="lower", expires="30m"), model)
    assert got == {"kpi": KPI, "better": "lower", "expires": 1800.0, "actions": [MORE, SPARE]}

    def refused(*actions, **more):
        with pytest.raises(recommend.Refused) as caught:
            recommend.load(asked(tmp_path, *actions, **more), model)
        return caught.value.code, caught.value.message

    # No key of its own making: a sink, a note, a path.
    for key in ("sink", "outbox", "note", "path"):
        assert refused(**{key: "file:C:/x"})[0] == "E_CANDIDATES"
        assert refused({**MORE, key: "x"})[0] == "E_CANDIDATES"
    # The verbs listed and not built are refused by name, with why.
    for verb in ("expedite", "resequence", "hold", "release", "reroute"):
        code, message = refused({"verb": verb, "target": "Server", "value": 1})
        assert code == "E_CANDIDATE_VERB" and verb in message and "not built" in message
    assert "no verb" in refused({"verb": "run", "target": "Server", "value": 1})[1]
    for action in ({**MORE, "target": "Nobody"}, {**MORE, "value": 0}, {**MORE, "value": True},
                   {**MORE, "value": 2.5}, {**MORE, "value": 10_001}, {**MORE, "target": 3}):
        assert refused(action)[0] == "E_CANDIDATES"
    assert "same action" in refused(MORE, dict(MORE))[1]
    assert refused(*[{**MORE, "value": n} for n in range(1, 22)])[0] == "E_CANDIDATES"
    assert refused(kpi="")[0] == refused(better="up")[0] == refused(expires="2d")[0] == "E_CANDIDATES"
    scheduled = counted(slow_line(schedules=[{"name": "s", "kind": "capacity"}]))
    scheduled["resources"][0]["capacity_schedule"] = "s"
    with pytest.raises(recommend.Refused, match="schedule"):
        recommend.check_action(MORE, scheduled)
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (recommend.LIMITS["file_bytes"] + 1))
    with pytest.raises(recommend.Refused, match="over"):
        recommend.load(big, model)


def test_candidates_are_compared_on_one_seed_and_only_a_clear_gain_is_ranked():
    """D301, D302: paired on common random numbers; an interval that holds "no effect" is
    shown as such; ties and the order are the same on every machine."""
    model = state._model(counted(busy_line()))
    three = {"verb": "set_capacity", "target": "Server", "value": 3}
    asked_ = dict(kpi=KPI, better="higher", horizon=200.0, replications=12, seed=4)
    rows, reason = recommend.effects(model, [SPARE, three, MORE], **asked_)
    assert reason is None and [r["verdict"] for r in rows] == ["better", "better", "no_effect"]
    assert [r.get("rank") for r in rows] == [1, 2, None]
    # More capacity moves more; and the larger effect is ranked first.
    assert rows[0]["effect"] >= rows[1]["effect"] > 0 < rows[1]["low"] <= rows[1]["high"]
    best = rows[0]
    assert best["baseline"] + best["effect"] == pytest.approx(best["with_action"])
    assert best["q05"] <= best["with_action"] <= best["q95"] and 0.5 < best["confidence"] <= 1
    # The unused resource: the very same run a replication, so an effect of exactly none.
    assert (rows[2]["effect"], rows[2]["low"], rows[2]["high"], rows[2]["confidence"]) == (0, 0, 0, 0)
    # The same again: nothing in it depends on the order of the file or on the process.
    again, _reason = recommend.effects(model, [MORE, three, SPARE], **asked_)
    assert again == rows
    # Lower is better: the same gain is then a loss, and nothing is recommended.
    rows, reason = recommend.effects(model, [MORE], **{**asked_, "better": "lower"})
    assert reason == "none_better" and rows[0]["verdict"] == "worse" and "rank" not in rows[0]


def test_no_recommendation_always_has_its_reason():
    model = state._model(counted(busy_line()))
    asked_ = dict(better="higher", horizon=50.0, replications=3, seed=1)
    assert recommend.effects(model, [MORE], kpi="system/No such", **asked_) == ([], "kpi")
    assert recommend.effects(model, [MORE], kpi=KPI, **{**asked_, "replications": 1}) \
        == ([], "replications")
    assert recommend.effects(model, [SPARE], kpi=KPI, **asked_)[1] == "none_better"

    def breaks(candidate, **more):
        if candidate["resources"][0]["capacity"] == 2:
            raise ValueError("C:/secret/path in a message")
        return twin.forecast_from_state(candidate, **more)
    rows, reason = recommend.effects(model, [MORE], kpi=KPI, runner=breaks, **asked_)
    assert reason == "failed" and rows == [{"action": MORE, "verdict": "failed", "why": "ValueError"}]
    made = twin.forecast_from_state(model, horizon=50.0, replications=3, seed=1, workers=1,
                                    should_stop=None)
    assert recommend.effects(model, [MORE], kpi=KPI, baseline=made, should_stop=lambda: True,
                             **asked_) == (None, None)


# ---------------------------------------------------------------- the outbox (D305, D306)
from nagasim import outbox  # noqa: E402

AN_ID, OTHER_ID = "a" * 64, "b" * 64


def test_only_a_file_sink_is_built_and_the_rest_is_refused_by_name(tmp_path):
    for spec, said in (("mqtt:plant", "mqtt sink is not built"), ("rest:mes", "names no sink"),
                       ("file:", "names no sink"), (str(tmp_path / "q"), "names no sink"),
                       (r"file:\\server\share\q.jsonl", "UNC"),
                       (f"file:{tmp_path / 'none' / 'q.jsonl'}", "folder")):
        with pytest.raises(outbox.SinkError, match=said) as caught:
            outbox.open_sink(spec)
        assert caught.value.code == "E_OUTBOX_SINK"
    assert not (tmp_path / "none").exists()         # no folder is made
    assert outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}").kind == "file"


def test_a_sink_path_that_is_not_a_plain_file_is_refused(tmp_path):
    """The security table: a folder or a link at the path, and the ceiling."""
    (tmp_path / "folder").mkdir()
    with pytest.raises(outbox.SinkError, match="not a plain file"):
        outbox.open_sink(f"file:{tmp_path / 'folder'}")
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    sink.path.mkdir()                               # put there after the start
    with pytest.raises(OSError) as caught:
        sink.deliver({"id": AN_ID})
    assert str(tmp_path) not in str(caught.value)   # the class, never the path
    sink.path.rmdir()
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("mine\n", encoding="utf-8")
    try:
        sink.path.symlink_to(secret)
    except OSError:
        pytest.skip("this account may make no link")
    with pytest.raises(OSError):
        sink.deliver({"id": AN_ID})
    assert secret.read_text(encoding="utf-8") == "mine\n"
    with pytest.raises(outbox.SinkError):
        outbox.open_sink(f"file:{sink.path}")


def test_a_record_is_one_line_read_back_and_the_file_stops_at_its_ceiling(tmp_path, monkeypatch):
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    sink.path.write_bytes(b'{"torn": ')             # another writer's half line
    sink.deliver({"id": AN_ID, "n": 1})
    sink.deliver({"id": OTHER_ID, "n": 2})
    lines = sink.path.read_bytes().split(b"\n")
    assert lines[0] == b'{"torn": ' and lines[3] == b""
    assert [json.loads(line)["n"] for line in lines[1:3]] == [1, 2]
    with pytest.raises(ValueError, match="TooLarge"):
        sink.deliver({"id": AN_ID, "pad": "x" * outbox.LIMITS["record_bytes"]})
    with pytest.raises(ValueError):                 # not a number JSON can carry
        sink.deliver({"id": AN_ID, "n": float("nan")})
    # A write that another writer went through is a delivery that failed.
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, data[:5] + b"\n") and len(data))
    with pytest.raises(OSError, match="NotWritten"):
        sink.deliver({"id": AN_ID, "n": 3})
    monkeypatch.undo()
    monkeypatch.setitem(outbox.LIMITS, "sink_bytes", sink.path.stat().st_size + 10)
    with pytest.raises(outbox.Full):
        sink.deliver({"id": AN_ID, "n": 4})


def replies(sink, *lines):
    with open(sink.replies, "ab") as handle:
        for line in lines:
            handle.write((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n")


def test_replies_are_read_like_a_feed_and_a_hostile_file_gives_none(tmp_path):
    """D229, D306: what changed since a cursor; every line that is no reply is counted."""
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    got = sink.changes(outbox.REPLIES, None)
    assert (got.events, got.cursor, got.lost, got.state) == ((), "0", 0, "live")
    replies(sink, {"id": AN_ID, "decision": "taken", "at": "2026-10-10T08:00:00+00:00",
                   "by": "Ada\x1b[2J" + "x" * 500, "note": "run it", "path": "C:/x", "exec": ["rm"]})
    got = sink.changes(outbox.REPLIES, got.cursor)
    (event,) = got.events
    assert event.key == AN_ID and event.source == outbox.REPLIES and event.source_time == 1791619200.0
    assert set(event.value) == {"id", "decision", "at", "by", "note"}      # and nothing else
    assert event.value["by"].startswith("Ada?[2J") and len(event.value["by"]) == 200
    assert sink.changes(outbox.REPLIES, got.cursor).events == ()            # once
    cursor = got.cursor
    replies(sink, b"not json", b"[" * 3000, b'"text"', b"[1, 2]", b"",
            {"id": "short", "decision": "taken"}, {"id": AN_ID, "decision": "run"},
            {"id": AN_ID, "decision": ["taken"]}, {"id": 5, "decision": "taken"},
            b'{"id": "' + b"c" * 64 + b'", "decision": "taken", "note": "' + b"n" * 5000 + b'"}',
            {"id": OTHER_ID, "decision": "rejected", "at": 12})
    got = sink.changes(outbox.REPLIES, cursor)
    assert [(e.key, e.value["decision"], e.value["at"]) for e in got.events] == [
        (OTHER_ID, "rejected", None)]
    assert got.lost == 9 and got.state == "live"
    # A line still being written is not read before it ends.
    with open(sink.replies, "ab") as handle:
        handle.write(b'{"id": "' + AN_ID.encode() + b'", "decision": "expired"')
    assert sink.changes(outbox.REPLIES, got.cursor).cursor == got.cursor
    replies(sink, b"}")
    assert [e.value["decision"] for e in sink.changes(outbox.REPLIES, got.cursor).events] == ["expired"]
    # A line with no end, larger than a call reads: dropped as it comes, counted once.
    sink.replies.write_bytes(b"x" * (outbox.LIMITS["pull_bytes"] * 2 + 7) + b"\n"
                             + json.dumps({"id": AN_ID, "decision": "taken"}).encode() + b"\n")
    got, lost, events = sink.changes(outbox.REPLIES, "0"), 0, []
    for _ in range(5):
        lost, events = lost + got.lost, events + list(got.events)
        got = sink.changes(outbox.REPLIES, got.cursor)
    assert lost == 1 and [e.key for e in events] == [AN_ID]
    # The file shrank: read again from its start, and said.
    sink.replies.write_bytes(json.dumps({"id": OTHER_ID, "decision": "taken"}).encode() + b"\n")
    got = sink.changes(outbox.REPLIES, got.cursor)
    assert got.gap and [e.key for e in got.events] == [OTHER_ID]
    # A folder where the replies were: down, with the class and no path.
    sink.replies.unlink()
    sink.replies.mkdir()
    got = sink.changes(outbox.REPLIES, "0")
    assert got.state == "down" and got.problems[0].code == "W_OUTBOX"
    assert str(tmp_path) not in got.problems[0].message


def test_a_persons_reply_is_one_line_the_twin_reads_back(tmp_path):
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    sink.reply(AN_ID, "taken", by="editor")
    (event,) = sink.changes().events
    assert (event.key, event.value["decision"], event.value["by"]) == (AN_ID, "taken", "editor")
    assert event.value["at"] is not None and not sink.path.exists()
    for bad in ((AN_ID, "run"), ("../x", "taken"), (None, "taken")):
        with pytest.raises(ValueError):
            sink.reply(*bad)


# ---------------------------------------------------------------- the cycle (D301-D305)
def advised(tmp_path, *actions, sink=True, lifetime=60.0, **more):
    """A twin on the mock plant of the slow line, recommending on what `p` lets out:
    (the cycle, the advisor, the clock a test moves, the model, the mapping, the log)."""
    model = counted(slow_line())
    source, clock, _events, mapping, log = playing(model, counters=True)
    advisor = recommend.Advisor(
        recommend.load(asked(tmp_path, *(actions or (MORE, SPARE))), model), model=model,
        horizon=12.0, name="line", mapping=mapping, out_dir=tmp_path / "out", lifetime=lifetime,
        sink=outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}") if sink else None,
        wall=lambda: clock.wall, monotonic=lambda: clock.mono)
    cycle = cycle_on(source, clock, tmp_path / "out", advisor=advisor, **more)
    clock.mono = 10.5
    return cycle, advisor, clock, model, mapping, log


def lines_of(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def test_a_twin_recommends_from_the_plants_state_and_delivers_to_the_callers_outbox(tmp_path):
    cycle, advisor, clock, model, mapping, _log = advised(tmp_path)
    doc = cycle.step()
    said = doc["recommend"]
    assert (said["status"], said["reason"], said["kpi"], said["crn"]) == ("ok", None, KPI, True)
    won, same = said["items"]
    assert (won["action"], won["verdict"], won["rank"], won["status"]) == (MORE, "better", 1, "delivered")
    assert (same["action"], same["verdict"]) == (SPARE, "no_effect") and "id" not in same
    assert won["effect"] > 0 and won["low"] > 0 and won["confidence"] == 1.0
    # The record: one line in the caller's file, the same as the ledger's, data only.
    (record,) = lines_of(tmp_path / "q.jsonl")
    assert recommend.check(record) and record["id"] == won["id"]
    assert record["action"] == MORE and record["expected"]["kpi"] == KPI
    forecast = doc["forecast"]
    assert (record["seed"], record["state"]["hash"], record["model"]) == (
        forecast["seed"], forecast["state"], {"name": "line", "hash": forecast["model"]})
    assert record["mapping"] == recommend.identity(mapping) and record["engine"] == nagasim.__version__
    assert record["scenario"] == "Baseline" and set(record["state"]["cursors"]) == {"plant"}
    assert str(tmp_path) not in json.dumps(record) and "\\\\" not in json.dumps(record)
    ledger = json.loads((tmp_path / "out" / recommend.LEDGER_FILE).read_text(encoding="utf-8"))
    assert [e["record"] for e in ledger["entries"]] == [record]
    # The state DOCUMENT is kept: a plain state file this model may start from.
    kept = json.loads((tmp_path / "out" / "states" / f"{forecast['state']}.json").read_text("utf-8"))
    assert kept["hash"] == forecast["state"] and state.check(model, kept) == []
    # Delivered once: the next steps write nothing more.
    clock.mono = 11.0
    cycle.step()
    assert len(lines_of(tmp_path / "q.jsonl")) == 1
    assert said["outbox"]["records"]["delivered"] == 1 and said["outbox"]["sink"] == "file"


def test_candidates_never_enter_the_history_or_the_rolling_score(tmp_path):
    """D301: a candidate is no forecast. The same twin with and without candidates stores
    the same forecasts and the same scores, and no seed is counted as repeated."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cycle, _advisor, clock, model, _mapping, _log = advised(tmp_path / "a", forecast_every=4.0)
    source, other, *_rest = playing(model, counters=True)
    plain = cycle_on(source, other, tmp_path / "b" / "out", forecast_every=4.0)
    for t in (10.5, 14.5, 18.5, 22.5, 26.5):
        clock.mono = other.mono = t
        mine, theirs = cycle.step(), plain.step()
    assert mine["recommend"]["status"] == "ok" and "recommend" not in theirs
    assert mine["scores"] == theirs["scores"] and mine["scores"]["basis"]["points"] > 0
    assert mine["scores"]["basis"]["same_seed"] == 0
    stored = [json.loads((tmp_path / d / "out" / twin.HISTORY_FILE).read_text("utf-8"))
              for d in "ab"]
    strip = [[{k: v for k, v in r.items() if k not in ("finished", "took")}
              for r in s["forecasts"]] for s in stored]
    assert strip[0] == strip[1] and len(strip[0]) == 5


def test_without_an_outbox_nothing_leaves_the_result_directory(tmp_path):
    """D227: refused by default."""
    cycle, _advisor, _clock, *_rest = advised(tmp_path, sink=False)
    said = cycle.step()["recommend"]
    assert said["items"][0]["status"] == "proposed" and said["outbox"]["sink"] is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["candidates.json", "out"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "states", "twin.json", "twin_history.json", "twin_outbox.json"]


def test_no_recommendation_is_made_from_a_cycle_that_made_no_forecast(tmp_path):
    cycle, _advisor, clock, *_rest = advised(tmp_path, forecast_every=1.0)
    assert cycle.step()["recommend"]["status"] == "ok"
    real = cycle.source.observe
    cycle.source.observe = lambda: {**real(), "ready": False}
    clock.mono = 12.0
    doc = cycle.step()
    assert doc["forecast"]["status"] == "skipped"
    assert doc["recommend"]["status"] == "none" and doc["recommend"]["reason"] == "no_forecast"
    assert doc["recommend"]["items"] == [] and len(lines_of(tmp_path / "q.jsonl")) == 1
    # A KPI the forecast does not have: said by name, once.
    cycle, advisor, clock, *_rest = advised(tmp_path / "k", MORE, forecast_every=1.0) \
        if (tmp_path / "k").mkdir() is None else None
    advisor.kpi = "system/No such"
    codes = []
    for t in (10.5, 12.0):
        clock.mono = t
        doc = cycle.step()
        codes += [p["code"] for p in doc["problems"] if p["code"] == "E_RECOMMEND_KPI"]
    assert doc["recommend"]["reason"] == "kpi" and codes == ["E_RECOMMEND_KPI"]


def test_a_refused_write_is_said_once_and_a_record_waits_until_it_expires(tmp_path, monkeypatch):
    cycle, advisor, clock, *_rest = advised(tmp_path, MORE, lifetime=30.0)
    advisor.sink.path.mkdir()               # a folder where the outbox file should be
    said = []
    for t in (10.5, 11.0, 11.6, 13.0):
        clock.mono = t
        doc = cycle.step()
        said += [p["message"] for p in doc["problems"] if p["code"] == "W_OUTBOX"]
    assert len(said) == 1 and "could not take a record" in said[0] and str(tmp_path) not in said[0]
    assert doc["recommend"]["outbox"]["state"] == "failing"
    assert doc["recommend"]["items"][0]["status"] == "proposed"
    # Tried again with a wait that doubles: 10.5, 11.6 (after 1 s), then after 2 s more.
    assert advisor._entries[0]["attempts"] == 2
    advisor.sink.path.rmdir()
    clock.mono = 14.0
    doc = cycle.step()
    assert [p["message"] for p in doc["problems"] if p["code"] == "W_OUTBOX"] == [
        "the outbox takes records again"]
    assert doc["recommend"]["items"][0]["status"] == "delivered"
    # Another twin: its record is not delivered in time, and never after its date.
    (tmp_path / "x").mkdir()
    cycle, advisor, clock, *_rest = advised(tmp_path / "x", MORE, lifetime=5.0)
    advisor.sink.path.mkdir()
    cycle.step()
    advisor.sink.path.rmdir()
    clock.mono = 16.0                       # the plant's present, past the record's date
    doc = cycle.step()
    assert doc["recommend"]["items"][0]["status"] == "expired" and not advisor.sink.path.exists()
    # The ceilings: a day's count, and the file's size.
    (tmp_path / "y").mkdir()
    monkeypatch.setitem(recommend.LIMITS, "day", 0)
    cycle, advisor, clock, *_rest = advised(tmp_path / "y", MORE)
    doc = cycle.step()
    assert doc["recommend"]["items"][0]["status"] == "dropped" and not advisor.sink.path.exists()
    monkeypatch.undo()
    (tmp_path / "z").mkdir()
    monkeypatch.setitem(outbox.LIMITS, "sink_bytes", 10)
    cycle, advisor, clock, *_rest = advised(tmp_path / "z", MORE)
    doc = cycle.step()
    assert doc["recommend"]["outbox"]["state"] == "stopped"
    assert [p["code"] for p in doc["problems"]].count("W_OUTBOX") == 1


def test_a_record_carries_data_only(tmp_path):
    """The security table: `check` is what a ledger read back and a delivery stand on."""
    from nagasim import netguard

    cycle, advisor, _clock, model, _mapping, _log = advised(tmp_path, MORE)
    cycle.step()
    (record,) = lines_of(tmp_path / "q.jsonl")
    assert recommend.check(record)

    def changed(**parts):
        return recommend.check({**json.loads(json.dumps(record)), **parts})
    assert not changed(note="rm -rf /") and not changed(path="C:/x")        # no key of its own
    assert not changed(action={**MORE, "note": "x"})
    assert not changed(action={**MORE, "verb": "expedite"})
    assert not changed(action={**MORE, "value": 3})                         # another id
    assert not changed(seed=record["seed"] + 1)
    assert not changed(confidence=float("nan")) and not changed(confidence="1")
    assert not changed(model={"name": "x" * 201, "hash": record["model"]["hash"]})
    assert not changed(state={**record["state"], "cursors": {"k": "v" * 500}})
    assert not changed(expected={**record["expected"], "kpi": "k" * 201})
    assert not changed(created="yesterday") and not recommend.check([record])
    with pytest.raises(TypeError):          # a credential cannot be put into a line
        advisor.sink.deliver({**record, "password": netguard.Secret("s")})
    # A ledger someone edited: what is no record of ours is not walked, the rest is.
    path = tmp_path / "out" / recommend.LEDGER_FILE
    ledger = json.loads(path.read_text(encoding="utf-8"))
    entry = ledger["entries"][0]
    ledger["entries"] = [{**entry, "record": {**record, "note": "x"}}, "text", {"record": 1},
                         {**entry, "status": "sent"}, {**entry, "standing": [{"verb": "x"}]},
                         entry]
    path.write_text(json.dumps(ledger), encoding="utf-8")
    again = recommend.Advisor(recommend.load(asked(tmp_path, MORE), model), model=model,
                              horizon=12.0, out_dir=tmp_path / "out")
    assert [e["record"]["id"] for e in again._entries] == [record["id"]] and not again._said
    # A ledger of another model, and one that is no ledger: started again, said once.
    for text in (json.dumps({**ledger, "model": "0" * 64}), "{"):
        path.write_text(text, encoding="utf-8")
        again = recommend.Advisor(recommend.load(asked(tmp_path, MORE), model), model=model,
                                  horizon=12.0, out_dir=tmp_path / "out")
        assert again._entries == [] and [p.code for p in again._said] == ["W_TWIN_RECOMMEND"]


def test_only_the_callers_flag_names_a_sink(tmp_path, capsys, monkeypatch):
    """The security table's first row: no model, mapping, candidates file or environment
    variable reaches `open_sink`; the two callers are the command and the editor."""
    root = Path(nagasim.__file__).parent.parent
    callers = {path.relative_to(root).as_posix() for folder in ("nagasim", "product")
               for path in (root / folder).rglob("*.py")
               if "open_sink(" in path.read_text(encoding="utf-8")}
    assert callers <= {"nagasim/outbox.py", "nagasim/twin.py", "product/ui/monitor.py"}
    evil = tmp_path / "evil.jsonl"
    model = counted(slow_line())
    _source, _clock, _events, mapping, log = playing(model, counters=True)
    model["simulation"]["outbox"] = mapping["outbox"] = f"file:{evil}"
    monkeypatch.setenv("NAGA_OUTBOX", f"file:{evil}")
    monkeypatch.setenv("NAGA_TWIN_OUTBOX", f"file:{evil}")
    paths = [str(tmp_path / name) for name in ("line.json", "line.plantmap.json", "plant.jsonl")]
    for path, data in zip(paths, (model, mapping)):
        Path(path).write_text(json.dumps(data), encoding="utf-8")
    plant.write_log(paths[2], [line for line in log if line["source_time"] <= 10.5])
    command = ["once", paths[0], "--map", paths[1], "--log", paths[2], "--out",
               str(tmp_path / "out"), "--horizon", "12", "--replications", "2",
               "--candidates", str(asked(tmp_path, MORE))]
    twin.main(command)
    assert not evil.exists() and not Path(str(evil) + ".replies").exists()
    doc, _problems = twin.read_result(tmp_path / "out")
    assert doc is None or doc["recommend"]["outbox"]["sink"] is None
    # The flag itself: refused by name before anything runs, and it needs candidates.
    for more, said in ((["--outbox", "mqtt:plant"], "E_OUTBOX_SINK"),
                       (["--outbox", f"file:{tmp_path / 'no' / 'q'}"], "E_OUTBOX_SINK")):
        capsys.readouterr()
        assert twin.main(command + more) == 1 and said in capsys.readouterr().out
    assert twin.main(command[:-2] + ["--outbox", f"file:{tmp_path / 'q'}"]) == 1
    with pytest.raises(SystemExit):         # no such flag: agents deliver nothing
        twin.main(command + ["--outbox-from-agents"])


# ---------------------------------------------------------------- feedback (D306-D308)
def test_an_action_that_was_taken_stands_in_every_later_forecast(tmp_path):
    """D307: the plant's state does not say a capacity, so the model is told. The model
    FILE is not: its hash, the stored states and the rolling score go on."""
    cycle, advisor, clock, model, _mapping, _log = advised(tmp_path, forecast_every=4.0)
    first = cycle.step()
    won = first["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "taken", "by": "Ada", "note": "ok",
                           "action": {"verb": "set_capacity", "target": "Server", "value": 999},
                           "value": 999, "path": "C:/x"})
    clock.mono = 11.0
    doc = cycle.step()
    said = doc["recommend"]
    assert said["items"][0]["status"] == "taken"
    # What stands is the action the twin proposed: a reply names an id and nothing else.
    assert [(s["id"], s["action"]) for s in said["standing"]] == [(won["id"], MORE)]
    assert said["outbox"]["replies"]["taken"] == 1
    # A person's free text is in the ledger, and in no result.
    assert "Ada" not in json.dumps(doc)
    ledger = json.loads((tmp_path / "out" / recommend.LEDGER_FILE).read_text(encoding="utf-8"))
    assert ledger["entries"][0]["reply"]["by"] == "Ada"
    clock.mono = 14.5
    doc = cycle.step()
    forecast, said = doc["forecast"], doc["recommend"]
    assert forecast["actions"] == [won["id"]] and forecast["model"] == first["forecast"]["model"]
    # The candidate that stands is not compared again; the other still is.
    assert [(i["action"], i["verdict"]) for i in said["items"]] == [(SPARE, "no_effect"),
                                                                     (MORE, "standing")]
    assert (said["status"], said["reason"]) == ("none", "none_better")
    # The forecast is of the plant with the action in it: two servers let more out.
    def out(doc_):
        return next(k["mean"] for k in doc_["forecast"]["kpis"] if k["kpi"] == KPI)
    assert out(doc) > out(first)
    assert doc["scores"]["basis"]["forecasts"] >= 1 and not [
        p for p in doc["problems"] if p["code"] == "W_TWIN_SCORE"]
    # A twin started again reads its ledger: the action still stands.
    again = recommend.Advisor(recommend.load(asked(tmp_path, MORE), model), model=model,
                              horizon=12.0, out_dir=tmp_path / "out")
    started, standing = again.apply(state._model(model))
    assert [s["action"] for s in standing] == [MORE]
    assert started["resources"][0]["capacity"] == 2 and model["resources"][0]["capacity"] == 1


def test_the_effect_of_a_taken_action_is_measured_from_the_plants_counter(tmp_path):
    """D308: the counter's increase over one horizon from the step the reply was read at."""
    cycle, advisor, clock, _model, _mapping, _log = advised(tmp_path)
    _source, _clock, events, *_rest = playing(counted(slow_line()), counters=True)
    won = cycle.step()["recommend"]["items"][0]
    assert cycle.step()["recommend"]["measurable"] is True
    replies(advisor.sink, {"id": won["id"], "decision": "taken"})
    clock.mono = 12.5
    (effect,) = cycle.step()["recommend"]["effects"]
    assert (effect["status"], effect["from"], effect["until"]) == ("waiting", 12.5, 24.5)
    clock.mono = 20.0
    assert cycle.step()["recommend"]["effects"][0]["status"] == "waiting"
    clock.mono = 25.0
    (effect,) = cycle.step()["recommend"]["effects"]
    observed = left(events, "p", 25.0) - left(events, "p", 12.5)
    assert (effect["status"], effect["observed"]) == ("measured", observed) and observed > 0
    assert effect["inside_interval"] is (won["q05"] <= observed <= won["q95"])
    # Stored, bounded, with the record: {"id", "observed", "inside_interval"}.
    ledger = json.loads((tmp_path / "out" / recommend.LEDGER_FILE).read_text(encoding="utf-8"))
    assert ledger["entries"][0]["effect"]["observed"] == observed
    clock.mono = 40.0
    assert cycle.step()["recommend"]["effects"][0]["observed"] == observed      # once


def test_an_effect_that_cannot_be_read_is_said_by_name(tmp_path):
    for name in "abcd":
        (tmp_path / name).mkdir()
    # The twin was not there in time: missed.
    cycle, advisor, clock, *_rest = advised(tmp_path / "a")
    won = cycle.step()["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "taken"})
    clock.mono = 12.5
    cycle.step()
    clock.mono = 30.0
    assert cycle.step()["recommend"]["effects"][0]["status"] == "missed"
    # A twin started again counts from 0 again (D290): lost, never a negative increase.
    cycle, advisor, clock, model, mapping, log = advised(tmp_path / "b")
    won = cycle.step()["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "taken"})
    clock.mono = 12.5
    cycle.step()
    later = Clock()
    feed = twin.LogFeed([line for line in log if line["source_time"] >= 13.0], speed=1.0,
                        monotonic=lambda: later.mono)
    again = recommend.Advisor(recommend.load(asked(tmp_path / "b"), model), model=model,
                              horizon=12.0, out_dir=tmp_path / "b" / "out", sink=advisor.sink)
    restarted = cycle_on(twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock),
                         later, tmp_path / "b" / "out", advisor=again)
    later.mono = 12.0
    effect = restarted.step()["recommend"]["effects"][0]
    assert (effect["status"], effect["observed"]) == ("lost", None)
    # A KPI no plant counts: not observable, said at the start and in the result.
    cycle, advisor, clock, *_rest = advised(tmp_path / "c")
    advisor.kpi, advisor.better, advisor.series = "timeseries/system/WIP/max", "lower", None
    doc = cycle.step()
    won = doc["recommend"]["items"][0]
    assert won["verdict"] == "better" and doc["recommend"]["measurable"] is False
    replies(advisor.sink, {"id": won["id"], "decision": "taken"})
    clock.mono = 12.5
    assert cycle.step()["recommend"]["effects"][0]["status"] == "not_observable"


def test_a_hostile_replies_file_changes_nothing(tmp_path):
    """D306 and the security table: counted by kind, dropped, and the twin goes on."""
    cycle, advisor, clock, *_rest = advised(tmp_path, lifetime=8.0)
    advisor.sink.path.mkdir()               # the record cannot be delivered yet
    won = cycle.step()["recommend"]["items"][0]
    replies(advisor.sink, b"\x00\xff garbage", b"{" * 5000, b"x" * 10_000,
            {"id": "f" * 64, "decision": "taken"},                  # not ours
            {"id": won["id"], "decision": "taken"})                  # before it was delivered
    clock.mono = 11.0
    doc = cycle.step()
    counts = doc["recommend"]["outbox"]["replies"]
    assert (counts["malformed"], counts["unknown"], counts["early"], counts["taken"]) == (3, 1, 1, 0)
    assert doc["recommend"]["standing"] == [] and doc["recommend"]["items"][0]["status"] == "proposed"
    advisor.sink.path.rmdir()
    clock.mono = 12.0
    assert cycle.step()["recommend"]["items"][0]["status"] == "delivered"
    replies(advisor.sink, {"id": won["id"], "decision": "rejected"},
            {"id": won["id"], "decision": "taken"})                  # the first wins
    clock.mono = 13.0
    doc = cycle.step()
    counts = doc["recommend"]["outbox"]["replies"]
    assert (counts["rejected"], counts["repeated"], counts["taken"]) == (1, 1, 0)
    assert doc["recommend"]["items"][0]["status"] == "rejected" and doc["recommend"]["standing"] == []
    # The file is replaced by a shorter one: read again from its start, all of it known.
    advisor.sink.replies.write_bytes(json.dumps({"id": won["id"], "decision": "taken"}).encode() + b"\n")
    clock.mono = 14.0
    counts = cycle.step()["recommend"]["outbox"]["replies"]
    assert (counts["gaps"], counts["repeated"], counts["taken"]) == (1, 2, 0)
    # A reply after the record's date is late; a record nobody answered expires.
    (tmp_path / "late").mkdir()
    cycle, advisor, clock, *_rest = advised(tmp_path / "late", lifetime=3.0)
    won = cycle.step()["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "taken"})
    clock.mono = 15.0
    doc = cycle.step()
    assert doc["recommend"]["outbox"]["replies"]["late"] == 1
    assert doc["recommend"]["items"][0]["status"] == "expired" and doc["recommend"]["standing"] == []
    # The replies file is a folder: said once, and the cycle goes on.
    advisor.sink.replies.unlink()
    advisor.sink.replies.mkdir()
    said = []
    for t in (16.0, 17.0):
        clock.mono = t
        said += [p["code"] for p in cycle.step()["problems"] if p["code"] == "W_OUTBOX"]
    assert said == ["W_OUTBOX"]


def test_a_reply_can_only_take_what_was_proposed(tmp_path):
    """The security table: a reply cannot name another action, a value or a file, and a
    candidate that was no clear gain has no id to answer."""
    cycle, advisor, clock, *_rest = advised(tmp_path, SPARE)
    doc = cycle.step()
    assert doc["recommend"]["status"] == "none" and not advisor.sink.path.exists()
    made = recommend.record_id(doc["forecast"]["model"], "Baseline", doc["forecast"]["state"],
                               doc["forecast"]["seed"], SPARE, KPI, 12.0)   # the id it would have had
    replies(advisor.sink, {"id": made, "decision": "taken"})
    clock.mono = 11.0
    doc = cycle.step()
    assert doc["recommend"]["outbox"]["replies"]["unknown"] == 1
    assert doc["recommend"]["standing"] == [] and advisor.apply({"resources": []}) == ({"resources": []}, [])


# ---------------------------------------------------------------- the exit check (D303)
def naga_twin(*arguments, hashseed="0"):
    """`naga twin ...` in a process of its own, with its own hash seed."""
    root = Path(nagasim.__file__).parent.parent
    return subprocess.run(
        [sys.executable, "-c", "import sys; from nagasim import twin; sys.exit(twin.main(sys.argv[1:]))",
         *map(str, arguments)], capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": str(root), "PYTHONHASHSEED": hashseed})


def test_naga_twin_reproduce_is_the_exit_check_across_two_processes(tmp_path, capsys):
    """Row 7.15: recommendations reproduce from the same state and seed. One process
    recommends from a plant's state; another computes the stored records again from the
    stored state documents and seeds, and finds no difference."""
    model = counted(busy_line())
    _source, _clock, _events, mapping, log = playing(model, counters=True)
    paths = [tmp_path / name for name in ("line.json", "line.plantmap.json", "plant.jsonl")]
    for path, data in zip(paths, (model, mapping)):
        path.write_text(json.dumps(data), encoding="utf-8")
    plant.write_log(paths[2], [line for line in log if line["source_time"] <= 120.0])
    three = {"verb": "set_capacity", "target": "Server", "value": 3}
    out, sink = tmp_path / "out", tmp_path / "q.jsonl"
    ran = naga_twin("once", paths[0], "--map", paths[1], "--log", paths[2], "--out", out,
                    "--horizon", 200, "--replications", 12, "--seed", 7,
                    "--candidates", asked(tmp_path, MORE, three, SPARE), "--outbox", f"file:{sink}",
                    hashseed="1")
    assert ran.returncode == 0, ran.stdout + ran.stderr
    records = lines_of(sink)
    assert len(records) == 2 and all(recommend.check(r) and r["seed"] == 7 for r in records)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in out.rglob("*"))
    again = naga_twin("reproduce", out, paths[0], hashseed="2")
    assert again.returncode == 0, again.stdout + again.stderr
    assert again.stdout.count("reproduced") == 2 and "DIFFERENT" not in again.stdout
    assert "every recommendation whose state is kept reproduces from its state and seed" in again.stdout
    assert sorted((p.name, p.stat().st_mtime_ns) for p in out.rglob("*")) == before   # nothing written
    # One of them, by the first characters of its id.
    assert twin.main(["reproduce", str(out), str(paths[0]), "--id", records[0]["id"][:10]]) == 0
    assert capsys.readouterr().out.count("reproduced") == 1
    # Every difference is said: a stored number that is not the model's, another model, a
    # state that is no longer kept, a state someone edited.
    ledger_path = out / recommend.LEDGER_FILE
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["entries"][0]["record"]["expected"]["effect"] += 0.5
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    assert twin.main(["reproduce", str(out), str(paths[0])]) == 1
    said = capsys.readouterr().out
    assert said.count("DIFFERENT") == 1 and "effect stored" in said and "1 differences" in said
    other = tmp_path / "other.json"
    other.write_text(json.dumps(counted(busy_line(), "p", "d")), encoding="utf-8")
    assert twin.main(["reproduce", str(out), str(other)]) == 1
    assert "E_STATE_MODEL" in capsys.readouterr().out
    kept = next((out / "states").iterdir())
    edited = json.loads(kept.read_text(encoding="utf-8"))
    edited["state"]["clock"] += 1.0
    kept.write_text(json.dumps(edited), encoding="utf-8")
    assert twin.main(["reproduce", str(out), str(paths[0])]) == 1
    assert "E_STATE_HASH" in capsys.readouterr().out
    kept.unlink()
    assert twin.main(["reproduce", str(out), str(paths[0])]) == 0       # said, not compared
    assert "no longer kept" in capsys.readouterr().out
    assert twin.main(["reproduce", str(out), str(paths[0]), "--id", records[0]["id"]]) == 1
    assert twin.main(["reproduce", str(tmp_path), str(paths[0])]) == 1
    assert "no stored recommendation" in capsys.readouterr().out


def test_kept_states_are_bounded_and_a_set_that_cannot_be_kept_is_not_delivered(tmp_path, monkeypatch):
    monkeypatch.setitem(recommend.LIMITS, "states", 3)
    cycle, advisor, clock, *_rest = advised(tmp_path, MORE, forecast_every=2.0)
    for t in (10.5, 12.6, 14.7, 16.8, 18.9):
        clock.mono = t
        doc = cycle.step()
    states = sorted(p.name for p in (tmp_path / "out" / "states").iterdir())
    assert len(states) == 3 and f"{doc['forecast']['state']}.json" in states
    assert len(lines_of(tmp_path / "q.jsonl")) == 5
    # A state over the bound: the set is shown, it does not reproduce, nothing is delivered.
    monkeypatch.setitem(recommend.LIMITS, "state_bytes", 10)
    clock.mono = 21.0
    doc = cycle.step()
    assert doc["recommend"]["reproducible"] is False and doc["recommend"]["status"] == "ok"
    assert doc["recommend"]["items"][0]["status"] == "proposed"
    assert "W_TWIN_RECOMMEND" in {p["code"] for p in doc["problems"]}
    assert len(lines_of(tmp_path / "q.jsonl")) == 5


# ---------------------------------------------------------------- surfaces (D309)
from nagasim import monitor  # noqa: E402


def test_a_summary_words_the_recommendations_once_and_status_prints_them(tmp_path, capsys):
    cycle, advisor, clock, *_rest = advised(tmp_path)
    doc = cycle.step()
    shown = monitor.summary(doc)["recommend"]
    won = doc["recommend"]["items"][0]
    assert shown["fold"][0].startswith(f"On {KPI} (higher is better), from state ")
    assert shown["fold"][1] == monitor.ADVICE and "not a promise about the plant" in monitor.ADVICE
    assert shown["rows"][0] == [1, "set_capacity Server = 2", won["effect"], won["low"], won["high"],
                                1.0, "better", "delivered", won["id"][:12]]
    assert shown["rows"][1][:2] == [None, "set_capacity Spare = 5"] and shown["rows"][1][6] == "no effect"
    assert any(line.startswith("Outbox (file): 1 delivered") for line in shown["lead"])
    # D358: the standing action and the recommendation first, the outbox last; the rest folds.
    assert shown["lead"][0].startswith("Recommended: set_capacity Server = 2, effect ")
    assert shown["lead"][-1].startswith("Outbox (file)") and monitor.ADVICE not in shown["lead"]
    assert str(tmp_path) not in json.dumps(shown)           # no sink, no path
    replies(advisor.sink, {"id": won["id"], "decision": "taken", "by": "Ada"}, b"junk")
    for t in (12.5, 25.0):
        clock.mono = t
        doc = cycle.step()
    lines = monitor.summary(doc)["recommend"]["lead"]
    assert any(line.startswith("Taken and standing in every forecast since ") for line in lines)
    assert lines[0].startswith("Taken and standing") and lines[-1].startswith("Replies dropped")
    assert not any(line.startswith("Recommended") for line in lines)     # it is answered
    folded = monitor.summary(doc)["recommend"]["fold"]
    assert any(line.startswith("Effect of set_capacity Server = 2: measured, ") for line in folded)
    assert "Replies dropped: 1 malformed." in lines and "Ada" not in json.dumps(lines)
    assert twin.main(["status", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "basis: On timeseries/block/p/out/max" in printed and "Taken and standing" in printed
    # No recommendation: its reason, in words; and a twin that was asked for none says nothing.
    for reason, text in monitor.NO_ADVICE.items():
        said = monitor.summary({"recommend": {"status": "none", "reason": reason, "items": []}})
        assert text in said["recommend"]["lead"] and text.startswith("No recommendation: ")
    assert monitor.summary({})["recommend"] is None
    assert monitor.summary({"recommend": "text"})["recommend"]["rows"] == []      # a file's
    assert set(monitor.NO_ADVICE) == set(recommend.REASONS)


def test_the_page_shows_recommendations_and_no_sink(tmp_path):
    """D282 as amended: read-only rows; no sink, no path, no reply text."""
    cycle, advisor, clock, model, *_rest = advised(tmp_path)
    doc = cycle.step()
    won = doc["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "taken", "by": "Ada", "note": "<script>"})
    clock.mono = 12.5
    page = monitor.Page(model, tmp_path / "out" / monitor.PAGE_FILE, every=5.0)
    text = page.build(cycle.step(), cycle.events)
    assert "set_capacity Server = 2" in text and won["id"][:12] in text
    assert won["id"] not in text and "Ada" not in text and "q.jsonl" not in text
    assert str(tmp_path).replace("\\", "\\\\") not in text and str(tmp_path) not in text
    assert 'id="m-advice-rows"' in text and "Approve" not in text and "Reject" not in text
    # D358: the explanation and the rows sit inside the one <details>, after the lead.
    head = text.index('id="m-advice"')
    assert head < text.index("<details>", head) < text.index('id="m-advice-fold"')         < text.index('id="m-advice-rows"') < text.index("</details>", head)
    assert "fetch(" not in text and "XMLHttpRequest" not in text and "<form" not in text


def test_no_agent_tool_can_deliver_or_approve(tmp_path):
    """D230: one read-only tool with no argument; nothing in the catalogue takes a sink, a
    path to send to, or a record's id."""
    from nagasim import agent_tools as T

    cycle, advisor, _clock, *_rest = advised(tmp_path)
    doc = cycle.step()
    T.use_workspace(T.Workspace(tmp_path, twin_dir=tmp_path / "out"))
    got = T.call("get_twin_recommendations")
    assert got["recommend"]["items"][0]["id"] == doc["recommend"]["items"][0]["id"]
    assert got["recommend"]["outbox"]["sink"] == "file" and str(tmp_path) not in json.dumps(got)
    spec = next(t for t in T.tool_schemas() if t["name"] == "get_twin_recommendations")
    assert spec["read_only"] is True and not spec["input_schema"].get("properties")
    for tool in T.tool_schemas():
        names = set(tool["input_schema"].get("properties") or {})
        assert not names & {"sink", "outbox", "record_id", "decision", "approve", "reply"}, tool["name"]
        assert not any(word in tool["name"] for word in ("deliver", "approve", "outbox", "reply"))
    assert len(lines_of(advisor.sink.path)) == 1 and not advisor.sink.replies.exists()


# ---------------------------------------------------------------- the fix round
from nagasim import api  # noqa: E402

BUFFERS = Path(nagasim.__file__).parent / "models" / "line_with_buffers.json"


def test_a_capacity_an_instance_binds_is_refused_and_the_instances_own_is_targetable(tmp_path):
    """I1 of the review: on a model of asset instances the template's capacity is bound
    again by each instance's argument, so an override of it was accepted, changed no run
    and was shown as `no_effect`. Refused by name; the instance's resource, as it is named
    after flattening, is the instance's own argument."""
    model = state._model(api.load_layout(BUFFERS))

    def said(target, value=2, layout=model):
        with pytest.raises(recommend.Refused) as caught:
            recommend.check_action({"verb": "set_capacity", "target": target, "value": value}, layout)
        return caught.value.code, caught.value.message
    code, message = said("Mfg Machine")
    assert code == "E_CANDIDATES" and "is an instance's argument" in message
    assert "declares no resource" in said("station_9/Mfg Machine")[1]
    assert "declares no resource" in said("station_1/Nobody")[1]
    assert "declares no resource" in said("buffer_1/Mfg Machine")[1]
    action = {"verb": "set_capacity", "target": "station_1/Mfg Machine", "value": 2}
    assert recommend.check_action(action, model) == action
    assert recommend.override_of(action, model) == {
        "path": "blocks.station_1.params.arguments.machines", "value": 2}
    # An instance that leaves the argument at its default has nothing to override.
    bare = json.loads(json.dumps(model))
    next(b for b in bare["blocks"] if b["name"] == "station_1")["params"]["arguments"].pop("machines")
    assert "must set the argument" in said("station_1/Mfg Machine", layout=bare)[1]
    # The candidate changes the run, and the record reproduces from its state and seed.
    kpi = "buffer/station_1/machine/Blocked %"
    asked_ = {"kpi": kpi, "better": "higher", "expires": None, "actions": [action]}
    doc = state.document(model, {})
    started, _problems = state.start(model, doc)
    advisor = recommend.Advisor(asked_, model=model, horizon=300.0, name="line",
                                out_dir=tmp_path / "out")
    pending = {"state": doc["hash"], "model": doc["model"]["hash"], "seed": 0, "from": 0.0,
               "stamp": None, "horizon": 300.0, "replications": 4}
    made = twin.forecast_from_state(started, horizon=300.0, replications=4, seed=0, workers=1,
                                    should_stop=None)
    compared = advisor.compare(started, pending, made, runner=twin.forecast_from_state,
                               should_stop=None, workers=1)
    (row,) = compared["rows"]
    assert row["verdict"] == "better" and row["effect"] > 1 and row["with_action"] != row["baseline"]
    advisor.take(pending, doc, started, compared)
    advisor.step({}, 1_800_000_000.0)
    lines, differences = recommend.reproduce(tmp_path / "out", model)
    assert differences == 0 and "reproduced" in lines[0] and "station_1/Mfg Machine = 2" in lines[0]


def test_a_standing_action_is_ended_through_the_outbox(tmp_path, capsys):
    """I2 (D307 amended): `{"id", "decision": "ended"}` on a taken record. It stands no
    more from the next forecast; for anything that does not stand it is counted, dropped."""
    cycle, advisor, clock, *_rest = advised(tmp_path, forecast_every=4.0)
    first = cycle.step()
    won = first["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "ended"},     # not taken yet
            {"id": "e" * 64, "decision": "ended"},                    # not ours
            {"id": won["id"], "decision": "taken"},
            {"id": won["id"], "decision": "rejected"})                # anything else: repeated
    for t in (11.0, 14.5):                  # read at one step, in the forecast of the next
        clock.mono = t
        doc = cycle.step()
    counts = doc["recommend"]["outbox"]["replies"]
    assert (counts["not_standing"], counts["unknown"], counts["taken"], counts["repeated"],
            counts["ended"]) == (1, 1, 1, 1, 0)
    assert [s["id"] for s in doc["recommend"]["standing"]] == [won["id"]]
    assert doc["forecast"]["actions"] == [won["id"]]
    # M4: every forecast surface says the forecast assumes it.
    said = "This forecast assumes 1 taken action(s): see Recommendations."
    assert said in monitor.summary(doc)["forecast"]["lines"]
    assert said not in monitor.summary(first)["forecast"]["lines"]
    assert twin.main(["status", str(tmp_path / "out")]) == 0
    assert "assumes 1 taken action(s)" in capsys.readouterr().out
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(tmp_path, twin_dir=tmp_path / "out"))
    assert T.call("get_twin_forecast")["forecast"]["assumes"] == said
    standing = next(i for i in doc["recommend"]["items"] if i["verdict"] == "standing")
    assert standing["id"] == won["id"] and standing["status"] == "taken"
    replies(advisor.sink, {"id": won["id"], "decision": "ended", "by": "Ada"},
            {"id": won["id"], "decision": "ended"})                   # once
    for t in (15.0, 18.5):
        clock.mono = t
        doc = cycle.step()
    counts = doc["recommend"]["outbox"]["replies"]
    assert (counts["ended"], counts["not_standing"]) == (1, 2)
    assert doc["recommend"]["standing"] == [] and "actions" not in doc["forecast"]
    assert [(e["id"], e["action"]) for e in doc["recommend"]["ended"]] == [(won["id"], MORE)]
    assert "assumes" not in T.call("get_twin_forecast")["forecast"]
    # The candidate is compared again, and the surfaces say what ended.
    assert [i["verdict"] for i in doc["recommend"]["items"]] == ["better", "no_effect"]
    lines = monitor.summary(doc)["recommend"]["lead"]
    assert any(line.startswith("Ended ") and "set_capacity Server = 2" in line for line in lines)
    assert "Ada" not in json.dumps(doc)
    ledger = json.loads((tmp_path / "out" / recommend.LEDGER_FILE).read_text(encoding="utf-8"))
    entry = next(e for e in ledger["entries"] if e["record"]["id"] == won["id"])
    assert entry["stands"] is False and entry["ended"]["by"] == "Ada" and entry["ended"]["read"]
    # A twin started again does not take it up again.
    again = recommend.Advisor(recommend.load(asked(tmp_path), cycle.source.model),
                              model=cycle.source.model, horizon=12.0, out_dir=tmp_path / "out")
    assert again._standing() == [] and not again._said


def test_a_sink_with_another_name_or_inside_another_file_is_refused(tmp_path):
    """M1, M2: a hard link, and a ':' after the drive (a stream inside another file)."""
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    sink.deliver({"id": AN_ID})
    try:
        os.link(sink.path, tmp_path / "other-name.txt")
    except OSError:
        pytest.skip("this file system makes no hard link")
    with pytest.raises(OSError, match="HardLink"):
        sink.deliver({"id": OTHER_ID})
    assert len(lines_of(tmp_path / "other-name.txt")) == 1
    with pytest.raises(outbox.SinkError, match="hard link"):
        outbox.open_sink(f"file:{sink.path}")
    host = tmp_path / "host.jsonl"
    host.write_bytes(b"")
    with pytest.raises(outbox.SinkError, match="':'"):
        outbox.open_sink(f"file:{host}:stream")
    assert host.stat().st_size == 0


def test_an_id_names_the_proposal_with_its_kpi_and_horizon():
    """M3 (D228): the same state, seed and action on another KPI or horizon is another
    proposal; the numbers computed for it are not part of its name."""
    parts = ("m" * 64, "Baseline", "s" * 64, 3, MORE)
    one = recommend.record_id(*parts, KPI, 12.0)
    assert one == recommend.record_id(*parts, KPI, 12)
    assert len({one, recommend.record_id(*parts, "system/WIP", 12.0),
                recommend.record_id(*parts, KPI, 24.0)}) == 3


def test_a_ledger_is_judged_on_its_counts_and_on_what_may_stand(tmp_path):
    """M6: a count no float holds is none (#373); only what a reply took can stand.
    M7: `reproduce` reads a kept state under the twin's own bound."""
    cycle, advisor, clock, model, *_rest = advised(tmp_path, MORE)
    won = cycle.step()["recommend"]["items"][0]
    replies(advisor.sink, {"id": won["id"], "decision": "rejected"})
    clock.mono = 11.0
    cycle.step()
    path = tmp_path / "out" / recommend.LEDGER_FILE
    good = json.loads(path.read_text(encoding="utf-8"))

    def loaded(ledger):
        path.write_text(json.dumps(ledger), encoding="utf-8")
        return recommend.Advisor(recommend.load(asked(tmp_path, MORE), model), model=model,
                                 horizon=12.0, out_dir=tmp_path / "out")
    assert len(loaded(good)._entries) == 1
    for bad in ({**good, "counts": {**good["counts"], "unknown": 2 ** 60}},
                {**good, "counts": {**good["counts"], "taken": 10 ** 400}},
                {**good, "day": ["2026-10-10", 2 ** 54]}, {**good, "day": ["2026-10-10", -1]}):
        again = loaded(bad)
        assert again._entries == [] and [p.code for p in again._said] == ["W_TWIN_RECOMMEND"]
    entry = good["entries"][0]
    for edited in ({**entry, "stands": True},                       # rejected: it cannot stand
                   {**entry, "attempts": 2 ** 60},
                   {**entry, "record": {**entry["record"], "seed": 10 ** 400}}):
        again = loaded({**good, "entries": [edited]})
        assert again._entries == [] and again._standing() == []
    loaded(good)
    kept = next((tmp_path / "out" / "states").iterdir())
    kept.write_bytes(kept.read_bytes() + b" " * recommend.LIMITS["state_bytes"])
    lines, differences = recommend.reproduce(tmp_path / "out", model)
    assert differences == 1 and "over 2097152 bytes" in lines[0]


def test_a_candidate_the_state_cannot_hold_fails_alone():
    """From the review's "not done": a warm start with more units busy than a candidate's
    capacity. That row is `failed` with the class of the refusal; the others stand."""
    model = counted(slow_line())
    model["resources"][0]["capacity"] = 2
    started, problems = state.start(model, state.take(model, 10.5, seed=1))
    assert not problems and sum(e.get("in_service", 0) for e in started["initial_state"]["entities"]) == 2
    one, three = ({"verb": "set_capacity", "target": "Server", "value": n} for n in (1, 3))
    rows, reason = recommend.effects(started, [one, three], kpi=KPI, better="higher",
                                     horizon=12.0, replications=2, seed=3)
    by = {row["action"]["value"]: row for row in rows}
    assert by[1]["verdict"] == "failed" and by[1]["why"].endswith("Error") and "effect" not in by[1]
    assert by[3]["verdict"] in ("better", "no_effect") and reason in (None, "none_better")
    assert started["resources"][0]["capacity"] == 2


def test_the_advice_says_what_many_candidates_cost():
    """M5."""
    assert "about 1 in 20 pass by chance" in monitor.ADVICE
