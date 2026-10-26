"""Twin refit on approval (Phase 12 Task D, D740, D741): `nagasim/refit.py`, the trigger and
the adopted layer in `twin.Cycle`, the refit record on the `recommend.Advisor` ledger.

The cycle tests run a twin on a fake source and a fake forecast whose band is centred on
ten times the model's rate: a plant that shows another level shifts the alerts, and a
refitted rate moves the band. The calibration is a seam; the last test runs the real one on
the OIP belt with a step change in its arrival rate.
"""
import copy
import json
import math
import random
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nagasim
from nagasim import api, experiments, monitor, outbox, plant, recommend, refit, score, state, twin
from test_twin_cycle import Frame

MODELS = Path(nagasim.__file__).resolve().parent / "models"
PATH = "blocks.process_1.params.dist_params.rate"
RATE, TRUE = 1.25, 2.0                  # the model's rate, and the plant's
FIXTURE = Path(__file__).resolve().parent / "data" / "oip_belt_line"


def single():
    return api.load_layout(MODELS / "single_server.json")


def plan_file(tmp_path, **more):
    path = tmp_path / "line.refit.json"
    path.write_text(json.dumps({"format": 1, "kind": "naga-refit", "budget": 20, "readings": 6,
                                "factors": [{"path": PATH, "low": 0.5, "high": 4.0}], **more}),
                    encoding="utf-8")
    return path


class Source:
    """A plant of one series; its start is the model with a clock, its state hash hex."""

    name = "line"

    def __init__(self, model):
        self.model, self.model_hash = model, state.model_hash(model)
        self.at, self.values, self.n = 0.0, {"system/WIP": 10 * RATE}, 0

    def observe(self):
        return {"at": self.at, "stamp": None, "ready": True, "problems": [], "sources": [],
                "view": None, "observed": dict(self.values)}

    def start(self, horizon):
        self.n += 1
        model = copy.deepcopy(self.model)
        model["initial_state"] = {"clock": self.at}
        return model, {"hash": f"{self.n:064x}", "model": {"hash": self.model_hash}}, []


class Clock:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 50.0

    def tick(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make(tmp_path, *, fitted=TRUE, plan=None, source=None, **more):
    """A cycle that forecasts at every step, horizon 120 (a point every 20), stepped by 10."""
    model = single()
    source, clock, runs, fits = source or Source(model), Clock(), [], []

    def run(started, *, horizon, replications, seed, workers, should_stop):
        rate = experiments.value_at(started, PATH)
        runs.append(rate)
        start, names = started["initial_state"]["clock"], sorted(source.values)
        return types.SimpleNamespace(kpis=Frame(), bands=Frame(
            {"Series": name, "Time": start + horizon * i / 60, "Mean": 10 * rate,
             "Q05": 10 * rate - 2, "Q95": 10 * rate + 2, "N": 3}
            for name in names for i in range(61)))

    def calibrator(started, series, data, plan_, *, seed, workers, should_stop):
        fits.append({"series": series, "data": data, "seed": seed,
                     "rate": experiments.value_at(started, PATH)})
        return {"overrides": [{"path": PATH, "value": fitted}], "objective": 0.0, "used": 20}

    sink = outbox.FileSink(tmp_path / "outbox.jsonl")
    advisor = recommend.Advisor(None, model=model, horizon=120.0, name="line",
                                out_dir=tmp_path, sink=sink, wall=lambda: clock.wall)
    refitter = refit.Refitter(plan or refit.load(plan_file(tmp_path), model),
                              calibrator=calibrator)
    cycle = twin.Cycle(source, horizon=120.0, replications=3, seed=7, out_dir=tmp_path,
                       runner=run, advisor=advisor, refit=refitter, clock=lambda: clock.wall,
                       monotonic=lambda: clock.mono, forecast_every=10.0, **more)
    return types.SimpleNamespace(cycle=cycle, source=source, clock=clock, runs=runs, fits=fits,
                                 sink=sink, advisor=advisor, model=model)


def step(t, n=1):
    docs = []
    for _ in range(n):
        docs.append(t.cycle.step())
        t.source.at += 10.0
        t.clock.tick(10.0)
    return docs


def reply(t, record_id, decision, by="Matteo"):
    line = {"id": record_id, "decision": decision, "by": by, "note": "from the floor",
            "at": datetime.fromtimestamp(t.clock.wall, timezone.utc).isoformat()}
    with open(t.sink.replies, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def proposed(tmp_path, **more):
    """A twin whose plant shifted to the true rate at once, stepped to its proposal."""
    t = make(tmp_path, **more)
    t.source.values = {"system/WIP": 10 * TRUE}
    for _ in range(40):
        doc = step(t)[0]
        if doc["refit"]["status"] in ("proposed", "not_better", "failed"):
            return t, doc
    raise AssertionError(doc["refit"])


def ledger(tmp_path):
    return json.loads((tmp_path / recommend.LEDGER_FILE).read_text())["entries"]


# ---------------------------------------------------------------- the caller's file
def test_the_refit_file_is_closed_and_names_what_it_refuses(tmp_path):
    model = single()
    plan = refit.load(plan_file(tmp_path, series=["system/WIP"]), model)
    assert plan == refit.Plan(({"path": PATH, "low": 0.5, "high": 4.0},), 20, 2, 6, ("system/WIP",))

    def refused(**more):
        with pytest.raises(refit.Refused) as caught:
            refit.load(plan_file(tmp_path, **more), model)
        assert caught.value.code == "E_REFIT"
        return caught.value.message

    assert "'sink'" in refused(sink="file:x")
    for path, said in (("blocks.nobody.params.x", "addresses nothing"),
                       ("blocks.process_1.params.distribution", "does not address a number"),
                       ("simulation.time_limit", "sets no state"),
                       ("initial_state.variables.x", "sets no state"),
                       ("data_sources.plc.location", "sets no state")):
        message = refused(factors=[{"path": path, "low": 0, "high": 1}])
        assert path in message and said in message, message
    assert "listed twice" in refused(factors=[{"path": PATH, "low": 0, "high": 1}] * 2)
    assert "low under high" in refused(factors=[{"path": PATH, "low": 2, "high": 1}])
    assert "nothing more" in refused(factors=[{"path": PATH, "low": 0, "high": 1, "code": "x"}])
    assert "budget" in refused(budget=1) and "readings" in refused(readings=0)
    assert "series" in refused(series=["anything/else"])
    # An override a refit may make: the file's factors only, each inside its bounds.
    assert plan.check([{"path": PATH, "value": 2.0}]) is None
    assert "'blocks.create_1.params.dist_params.rate' is not a factor" in plan.check(
        [{"path": "blocks.create_1.params.dist_params.rate", "value": 1.0}])
    assert "outside its bounds" in plan.check([{"path": PATH, "value": 9.0}])
    assert "outside its bounds" in plan.check([{"path": PATH, "value": True}])
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (refit.LIMITS["file_bytes"] + 1))
    with pytest.raises(refit.Refused, match="over"):
        refit.load(big, model)
    with pytest.raises(refit.Refused, match="UNC"):
        refit.load(r"\\server\share\line.refit.json", model)


def test_the_refit_file_is_read_from_the_flag_only(tmp_path, capsys):
    folder = tmp_path / "model"
    folder.mkdir()
    for name in ("line.json", "line.plantmap.json", "plant.jsonl"):
        (folder / name).write_bytes((FIXTURE / name).read_bytes())
    (folder / "line.refit.json").write_text(json.dumps({     # beside the model: never read
        "format": 1, "kind": "naga-refit", "budget": 10, "readings": 3,
        "factors": [{"path": "blocks.Infeed.params.dist_params.high", "low": 1.5, "high": 6}]}))
    common = [str(folder / "line.json"), "--map", str(folder / "line.plantmap.json"), "--log",
              str(folder / "plant.jsonl"), "--horizon", "10", "--replications", "2"]
    assert twin.main(["once", *common, "--out", str(tmp_path / "a")]) == 0
    doc, _ = twin.read_result(tmp_path / "a")
    assert "refit" not in doc and "recommend" not in doc
    capsys.readouterr()
    refit_file = str(folder / "line.refit.json")
    assert twin.main(["once", *common, "--out", str(tmp_path / "b"), "--refit", refit_file]) == 1
    assert "--refit needs --outbox" in capsys.readouterr().out
    box = "file:" + str(tmp_path / "outbox.jsonl")
    assert twin.main(["once", *common, "--out", str(tmp_path / "b"), "--refit", refit_file,
                      "--outbox", box]) == 0
    assert "refit: 1 factors on an alert held for 8 readings" in capsys.readouterr().out
    doc, _ = twin.read_result(tmp_path / "b")
    assert doc["refit"]["status"] == "idle" and doc["refit"]["adopted"] is None
    assert "recommend" not in doc       # a ledger for refits only recommends nothing
    assert twin.main(["once", *common, "--out", str(tmp_path / "b"), "--refit",
                      str(folder / "line.json"), "--outbox", box]) == 1
    assert "E_REFIT: this is not a refit file" in capsys.readouterr().out
    assert twin.main(["once", *common, "--out", str(tmp_path / "b"), "--refit", refit_file,
                      "--outbox", box, "--alert-k", "0"]) == 1
    assert "--alert-k over 0" in capsys.readouterr().out


# ---------------------------------------------------------------- the window (D740)
def test_no_refit_starts_before_ceil_h_over_k_readings_under_the_alert(tmp_path):
    # h = 8, k = 0.5: 16 readings, more than a horizon of 120 holds at one a step of 10.
    t = make(tmp_path, alert_h=8.0)
    t.source.values = {"system/WIP": 10 * TRUE}
    docs = step(t, 30)
    assert docs[-1]["alerts"]["rows"] and t.fits == []
    assert docs[-1]["refit"]["status"] == "waiting"
    assert docs[-1]["refit"]["window"] == {"readings": 12, "needed": 16}
    assert "Waiting: 12 of 16 readings of system/WIP" in monitor.summary(docs[-1])["refit"][0]
    # The default h = 4: 8 readings, and the start's whole horizon behind them.
    other = tmp_path / "default"
    other.mkdir()
    t = make(other)
    t.source.values = {"system/WIP": 10 * TRUE}
    for _ in range(30):
        doc = step(t)[0]
        if t.fits:
            break
    fit, = t.fits
    window = doc["refit"]["window"]
    assert fit["series"] == "system/WIP"
    assert len(fit["data"]) == window["readings"] >= math.ceil(score.H / score.K)
    assert doc["at"] >= window["from"] + 120.0
    assert all(window["from"] < at <= window["from"] + 120.0 for at, _value in fit["data"])
    # From the first start inside the alert, so every reading is under the shift.
    alert = [a for a in doc["alerts"]["rows"] if a["subject"] == "system/WIP"][0]
    assert window["from"] >= alert["since"]
    assert all(value == 10 * TRUE for _t, value in fit["data"])
    assert fit["rate"] == RATE          # calibrated from the model as it was started


def test_an_alert_that_ends_before_the_window_fills_starts_nothing(tmp_path):
    t = make(tmp_path)
    t.source.values = {"system/WIP": 10 * TRUE}
    statuses = [doc["refit"]["status"] for doc in step(t, 3)]
    assert "waiting" in statuses
    t.source.values = {"system/WIP": 10 * RATE}
    docs = step(t, 25)
    assert t.fits == [] and docs[-1]["alerts"]["rows"] == []
    assert docs[-1]["refit"]["status"] == "idle"
    assert "No refit: no alert" in monitor.summary(docs[-1])["refit"][0]


# ---------------------------------------------------------------- the proof (D740)
def test_a_better_candidate_is_proposed_with_both_scores_and_its_readings(tmp_path):
    t, doc = proposed(tmp_path)
    said = doc["refit"]
    assert said["status"] == "proposed" and said["candidate"] == [{"path": PATH, "value": TRUE}]
    scores = said["scores"]
    assert scores["readings"] == 6 and scores["candidate"] < scores["current"]
    assert said["message"].startswith("a refit of 1 parameter cuts the pinball loss ")
    assert "on the last 6 readings of system/WIP: adopt?" in said["message"]
    entry, = [e for e in ledger(tmp_path) if e["record"]["kind"] == recommend.REFIT_KIND]
    record = entry["record"]
    assert recommend.check_refit(record) and record["id"] == said["id"]
    assert record["scores"] == scores and record["window"]["readings"] >= 8
    assert record["overrides"] == said["candidate"] and record["base"] is None
    # Proposed through the outbox, as data: the same record, one line.
    assert [json.loads(line)["id"] for line in t.sink.path.read_text().splitlines()] == [record["id"]]
    assert step(t)[0]["refit"]["outbox"] == "delivered"
    # The candidate ran on the forecasts' own seeds while it was scored, never as a forecast.
    assert TRUE in t.runs and all(rate == RATE for rate in t.runs[-3:])
    lines = monitor.summary(doc)["refit"]
    assert lines[0].startswith("Proposed: a refit of 1 parameter") and lines[-1] == monitor.REFITTED
    assert "refit: Proposed" in "\n".join(twin._summary(doc))


def test_a_candidate_that_scores_worse_is_not_proposed_and_the_result_says_why(tmp_path):
    t, doc = proposed(tmp_path, fitted=0.5)
    said = doc["refit"]
    assert said["status"] == "not_better" and "id" not in said
    assert said["scores"]["candidate"] >= said["scores"]["current"]
    assert "is not below the current model's" in said["message"]
    assert "nothing is proposed" in said["message"]
    assert not t.sink.path.exists() or t.sink.path.read_text() == ""
    assert not (tmp_path / recommend.LEDGER_FILE).exists() or not [
        e for e in ledger(tmp_path) if e["record"]["kind"] == recommend.REFIT_KIND]
    assert monitor.summary(doc)["refit"][0].startswith("Not proposed: ")
    # The same alert is not refitted again.
    step(t, 5)
    assert len(t.fits) == 1


def test_one_refit_at_a_time_and_none_while_a_proposal_waits(tmp_path):
    t = make(tmp_path)
    t.source.values = {"system/WIP": 10 * TRUE, "level/tank": 10 * TRUE}
    for _ in range(40):
        doc = step(t)[0]
        assert len(t.fits) <= 1
        if doc["refit"]["status"] == "proposed":
            break
    assert len(t.fits) == 1 and len(doc["alerts"]["rows"]) == 2
    step(t, 5)                          # unanswered: the other alert waits
    assert len(t.fits) == 1
    reply(t, doc["refit"]["id"], "rejected")
    step(t, 2)
    # The other alert's turn, once the first refit's proposal is answered.
    assert sorted(f["series"] for f in t.fits) == ["level/tank", "system/WIP"]


def test_a_start_is_held_uncopied_and_a_calibration_gets_its_own_copy(tmp_path):
    """Phase 15 FA: `hold` keeps the started model by reference (no copy every forecast);
    the one copy is made when a refit starts from it, so the calibration's thread cannot
    write into what the twin holds."""
    t = make(tmp_path)
    held = []
    real_hold = t.cycle._refit.hold
    t.cycle._refit.hold = lambda pending, model: held.append(model) or real_hold(pending, model)
    t.source.values = {"system/WIP": 10 * TRUE}
    for _ in range(30):
        step(t)
        if t.fits:
            break
    assert t.fits and all(s["model"] is m for s, m in zip(t.cycle._refit._held, held[-12:]))
    copies = []
    t.cycle._refit._calibrate = lambda started, *a, **k: copies.append(started) or {
        "overrides": [], "objective": 0.0, "used": 0}
    start = t.cycle._refit._held[0]
    t.cycle._refit._begin(start, "system/WIP", "high", 0.0, [[1.0, 2.0]], 1, None, False,
                          None, 1)
    assert copies[0] == start["model"] and copies[0] is not start["model"]


def test_a_calibration_runs_in_its_own_thread_and_no_second_one_starts_meanwhile(tmp_path):
    import threading

    gate, threads = threading.Event(), []
    t = make(tmp_path, background=True)
    t.source.values = {"system/WIP": 10 * TRUE, "level/tank": 10 * TRUE}
    calibrate = t.cycle._refit._calibrate

    def held(*args, **more):
        threads.append(threading.current_thread().name)
        assert gate.wait(10.0)
        return calibrate(*args, **more)
    t.cycle._refit._calibrate = held
    for _ in range(40):
        doc = step(t)[0]
        if doc["refit"]["status"] == "calibrating":
            break
        assert t.cycle.join(10.0)       # the forecast of this step, before the next
    for _ in range(5):                  # both alerts held, one calibration
        doc = step(t)[0]
        assert doc["refit"]["status"] == "calibrating" and len(threads) == 1
    assert threads == ["naga-twin-refit"]
    gate.set()
    assert t.cycle.join(10.0)
    assert step(t)[0]["refit"]["status"] == "scoring"
    t.cycle.stop()
    assert t.cycle.join(10.0)


# ---------------------------------------------------------------- adoption (D741)
def test_only_a_taken_reply_adopts_and_ended_reverts(tmp_path):
    t, doc = proposed(tmp_path)
    ident = doc["refit"]["id"]
    before = len(t.runs)
    step(t, 3)
    assert t.runs[before:] == [RATE] * 3        # no change of the model before a reply
    reply(t, ident, "taken")
    adopted = step(t)[0]                # read at this step: from the next forecast on
    assert t.runs[-1] == RATE and adopted["refit"]["adopted"]["id"] == ident
    assert adopted["refit"]["adopted"]["overrides"] == [{"path": PATH, "value": TRUE}]
    assert adopted["scores"]["basis"]["since"] == adopted["written"]
    assert adopted["alerts"]["basis"]["since"] == adopted["written"]
    after = step(t)[0]
    assert t.runs[-1] == TRUE and after["forecast"]["refit"] == ident
    assert after["forecast"]["model"] == refit.identity(t.source.model_hash, {"id": ident})
    # Who and when are in the history, beside the layer; in no result.
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text())
    assert history["refit"]["by"] == "Matteo" and history["refit"]["id"] == ident
    assert "Matteo" not in json.dumps(after) and "from the floor" not in json.dumps(after)
    assert monitor.summary(after)["refit"][-2].startswith(f"Adopted: refit {ident[:12]} since ")
    # The model file is not written, and the shifted plant is now in band: no alert.
    assert api.load_layout(MODELS / "single_server.json") == t.model
    assert step(t, 3)[-1]["alerts"]["rows"] == []
    reply(t, ident, "ended")
    ended = step(t)[0]
    assert ended["refit"]["adopted"] is None and t.runs[-1] == TRUE
    step(t)
    assert t.runs[-1] == RATE and "refit" not in json.loads(
        (tmp_path / twin.HISTORY_FILE).read_text())
    assert ended["scores"]["basis"]["since"] == ended["written"]


def test_a_restart_goes_on_with_the_layer_and_a_hand_edited_one_is_judged_whole(tmp_path):
    t, doc = proposed(tmp_path)
    reply(t, doc["refit"]["id"], "taken")
    step(t, 3)
    again = make(tmp_path, source=t.source)
    first = step(again)[0]
    assert again.runs[-1] == TRUE and "W_TWIN_REFIT" not in [p["code"] for p in first["problems"]]
    assert "W_TWIN_SCORE" not in [p["code"] for p in first["problems"]]
    path = tmp_path / twin.HISTORY_FILE
    for edit in ({"overrides": [{"path": PATH, "value": 3.5}]},       # inside the bounds
                 {"overrides": [{"path": PATH, "value": 99.0}]},
                 {"by": "someone else"}, {"extra": 1}):
        history = json.loads(path.read_text())
        history["refit"] = {**history["refit"], **edit}
        path.write_text(json.dumps(history))
        edited = make(tmp_path, source=t.source)
        said = step(edited)[0]
        codes = [p["code"] for p in said["problems"]]
        assert "W_TWIN_REFIT" in codes and "W_TWIN_SCORE" in codes, edit
        # Never a part of it: this forecast is of the model file, the next of the ledger's refit.
        assert edited.runs == [RATE] and 3.5 not in edited.runs
        step(edited)
        assert edited.runs[-1] == TRUE, (edit, edited.runs, [p["message"] for p in said["problems"]])
    # Given no --refit file, a kept layer is not applied, and said.
    from test_twin_cycle import make as plain
    cycle, *_ = plain(tmp_path)
    assert "no --refit file was given" in " ".join(p["message"] for p in cycle.step()["problems"])


def belt_log(shift=25.0, slower=5.0, end=200.0, seed=3):
    """The OIP belt as its plant: boxes every Uniform(1, 3) s, from *shift* on Uniform(1,
    *slower*) s, each counted into the sink 3.635 s after it was spawned."""
    rng, at, spawned = random.Random(seed), 1.05, []
    while at < end:
        spawned.append(at)
        at += rng.uniform(1.0, 3.0 if at < shift else slower)
    lines = [{"source": "oip", "key": key, "value": 0, "source_time": 0.0, "received": 0.0}
             for key in ("SinkCount", "SpawnCount")]
    for n, at in enumerate(spawned, 1):
        for key, when in (("SpawnCount", at), ("SinkCount", at + 3.635)):
            if when <= end:
                lines.append({"source": "oip", "key": key, "value": n,
                              "source_time": round(when, 3), "received": round(when, 3)})
    return sorted(lines, key=lambda line: line["source_time"])


@pytest.mark.slow
def test_a_step_in_the_belts_arrival_rate_is_refitted_near_the_true_value(tmp_path):
    """The OIP belt's spawner slows from a box every 2 s to one every 3 s (Uniform(1, 3) to
    Uniform(1, 5)) at t = 25 s. The twin's alert on the spawn count holds, the real
    calibration refits the gap's upper bound on the window, and the candidate is scored
    against the model on the readings that follow."""
    model = api.load_layout(FIXTURE / "line.json")
    mapping, _ = plant.load(FIXTURE / "line.plantmap.json")
    path = "blocks.Infeed.params.dist_params.high"
    file = tmp_path / "line.refit.json"
    file.write_text(json.dumps({"format": 1, "kind": "naga-refit", "budget": 30,
                                "replications": 3, "readings": 12, "series": ["block/Infeed/out"],
                                "factors": [{"path": path, "low": 1.5, "high": 6.0}]}))
    clock = Clock()
    clock.mono = 0.0
    feed = twin.LogFeed(belt_log(), speed=1.0, monotonic=lambda: clock.mono)
    source = twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock)
    sink = outbox.FileSink(tmp_path / "outbox.jsonl")
    advisor = recommend.Advisor(None, model=model, horizon=30.0, name="line", out_dir=tmp_path,
                                sink=sink, wall=lambda: clock.wall)
    cycle = twin.Cycle(source, horizon=30.0, replications=10, seed=3, out_dir=tmp_path,
                       forecast_every=4.0, advisor=advisor,
                       refit=refit.Refitter(refit.load(file, model)),
                       clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    clock.tick(20.0)
    while clock.mono < 190.0:
        doc = cycle.step()
        if doc["refit"]["status"] in ("proposed", "not_better", "unscored", "failed"):
            break
        clock.tick(0.5)
    said = doc["refit"]
    assert said["series"] == "block/Infeed/out" and said["side"] == "below"
    assert said["window"]["from"] >= 25.0 and said["window"]["readings"] >= 8
    (fitted,) = [o["value"] for o in said["candidate"] if o["path"] == path]
    assert 1.5 <= fitted <= 6.0 and abs(fitted - 5.0) < 1.0     # the plant's, not the model's 3
    assert said["status"] == "proposed", said
    assert said["scores"]["readings"] == 12
    assert said["scores"]["candidate"] < said["scores"]["current"]


@pytest.mark.slow
def test_a_recommendation_made_under_an_adopted_refit_reproduces_with_it(tmp_path):
    """A recommendation's record names the layered model; `reproduce` applies that refit."""
    from test_recommend import DONE, FAST, SLOW, branch_line
    from test_score import cycle_on, playing

    model, gap = branch_line(), "blocks.c.params.dist_params.value"
    source, clock, _events, mapping, _log = playing(model, counters=True)
    asked = tmp_path / "candidates.json"
    asked.write_text(json.dumps({"format": 1, "kind": "naga-candidates", "kpi": DONE,
                                 "candidates": [FAST, SLOW]}), encoding="utf-8")
    out = tmp_path / "out"
    advisor = recommend.Advisor(
        recommend.load(asked, model), model=model, horizon=12.0, name="line", mapping=mapping,
        out_dir=out, sink=outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}"),
        wall=lambda: clock.wall, monotonic=lambda: clock.mono)
    # A refit a person took, as the ledger holds it.
    ident = advisor.propose_refit({
        "overrides": [{"path": gap, "value": 1.0}], "series": "block/d/out", "side": "above",
        "window": {"readings": 8, "from": 0.0, "until": 10.0, "state": "0" * 64},
        "scores": {"readings": 6, "current": 1.0, "candidate": 0.5}, "seed": 3, "budget": 10,
        "base": None}, clock.wall)
    advisor._entry(ident).update(status="taken", stands=True, reply={
        "decision": "taken", "at": None, "by": "M", "note": None,
        "read": "2027-01-15T08:00:00+00:00"})
    plan = refit.Plan(({"path": gap, "low": 0.5, "high": 3.0},), 10, 2, 6)
    cycle = cycle_on(source, clock, out, advisor=advisor, replications=8, forecast_every=4.0,
                     refit=refit.Refitter(plan))
    clock.mono = 10.5
    cycle.step()                        # of the model file; the refit is adopted after it
    clock.mono = 14.5
    doc = cycle.step()
    assert doc["forecast"]["refit"] == ident
    made = [e["record"] for e in ledger(out) if e["record"]["kind"] == recommend.RECORD_KIND]
    assert {r["model"]["hash"] for r in made} == {
        state.model_hash(model), refit.identity(state.model_hash(model), {"id": ident})}
    lines, differences = recommend.reproduce(out, model)
    assert differences == 0 and sum("reproduced" in line for line in lines) == len(made), lines


def test_a_taken_refit_the_file_no_longer_allows_is_refused_by_name(tmp_path):
    t, doc = proposed(tmp_path)
    reply(t, doc["refit"]["id"], "taken")
    step(t, 2)
    narrow = refit.Plan(({"path": PATH, "low": 0.5, "high": 1.5},), 20, 2, 6)
    again = make(tmp_path, source=t.source, plan=narrow)
    docs = step(again, 3)
    said = [p["message"] for d in docs for p in d["problems"] if p["code"] == "W_TWIN_REFIT"]
    assert any(f"'{PATH}' is outside its bounds" in text for text in said)
    assert set(again.runs) == {RATE} and docs[-1]["refit"]["adopted"] is None
