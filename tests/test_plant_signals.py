"""Plant signals for what a state from a plant refused (spec 2026-10-16, D457 to D464): a log
made from a run (`plant.log_of`) carries them, and `state.from_plant` writes the state."""
from collections import Counter

import pytest

from nagasim import api, plant, state
from nagasim.core.timeunits import UNITS
from nagasim.core.trace import TraceRecorder
from nagasim.examples import model_path


def _bundled(name):
    return api.load_layout(model_path(name))


def _at(mapping):
    return next(s for s in mapping["signals"] if s["as"] == "at")


def _plant_at(model, at, seed=0, mapping=None):
    """(the state document from_plant writes at *at* from the log of a run, its problems, the
    taken state of that run at *at*, the mapper)."""
    mapping = mapping or plant.scaffold(model)
    recorder = plant.Recorder()
    api.run_model(model, replications=1, seed=seed, trace=recorder, record_logs=False)
    unit = UNITS[mapping["time"]["unit"]]
    log = [line for line in plant.log_of(recorder.events, mapping, attributes=recorder.attributes)
           if line["source_time"] <= at * unit]
    _events, mapper = plant.replay(mapping, model, log)
    doc, problems = state.from_plant(model, dict(mapper.snapshot(), at=at))
    return doc, problems, mapper, recorder.events


# ---------------------------------------------------------------- D457, D458
def test_a_log_made_from_a_run_says_where_each_vehicle_is_and_what_it_carries():
    """Each vehicle is at the node it last arrived at, or the one it set out from while it
    drives; the mapper places every unit and the loads it carried."""
    model = _bundled("warehouse_forklifts")
    mapping = plant.scaffold(model)
    assert [s["unit"] for s in mapping["signals"] if s["as"] == "vehicle"] == [0, 1, 2]
    assert not any(s["as"] in ("vehicle", "load") for s in plant.scaffold(model, anonymous=True)
                   ["signals"])
    recorder = TraceRecorder()
    api.run_model(model, replications=1, seed=0, trace=recorder, record_logs=False)
    moves = [e for e in recorder.events if e.kind == "mover_moved" and e.path]
    log = plant.log_of(recorder.events, mapping)
    loads = 0
    for at in (30.3, 95.1, 170.2, 333.3, 455.5, 901.3):
        _events, mapper = plant.replay(mapping, model, [l for l in log if l["source_time"] <= at * 60])
        said = {v["unit"]: v for v in mapper.snapshot()["vehicles"]["Forklifts"]}
        for unit in range(3):
            last = [e for e in moves if e.entity_id == unit and e.t <= at][-1]
            ended = last.t + (last.travel or last.duration) <= at
            assert said[unit]["node"] == (last.path[-1] if ended else last.path[0]), (at, unit)
        loads += sum(v["load"] is True for v in said.values())
    assert loads


def test_routing_a_plant_does_not_send_is_the_model_s_own():
    """D458: the station, sequence and step of every item a plant shows at a station's
    blocks are those of the run, and a load at the TRANSPORT goes where the run sends it."""
    model = _bundled("warehouse_forklifts")
    doc, _problems, _mapper, _events = _plant_at(model, 455.5)
    taken = state.take(model, 455.5)

    # load_truck and takeaway_belt are left out: the run's parts there carry the station, sequence
    # and step of the order they were picked for (history), which no block of the model gives a
    # plant (D458 derives routing from the block an item is seen at, and these are the last two).
    def routed(entities):
        return Counter((e["block"], e.get("e_type"), *(e.get("attributes", {}).get(k)
                                                        for k in ("station", "sequence", "step")),
                        (e.get("trip") or {}).get("target"))
                       for e in entities if e["block"] != "load_truck"
                       and e["block"] != "takeaway_belt")
    assert routed(doc["state"]["entities"]) == routed(taken["state"]["entities"])


# ---------------------------------------------------------------- D460
def _belts(doc):
    """Each part on a belt, rounded: (block, stage, until, entered, since at the stop)."""
    def r(x):
        return None if x is None else round(x, 9)
    return sorted((e["block"], e["belt"]["stage"], r(e["belt"].get("until")),
                   r(e["belt"].get("entered")), r(e.get("since")) if e["belt"]["stage"] == "stop"
                   else None) for e in doc["state"].get("entities", []) if "belt" in e)


@pytest.mark.parametrize("variant", ["stop", "station"])
def test_a_part_on_a_belt_is_where_its_entry_and_the_belt_put_it(variant):
    """D460: twelve parts onto a belt of 4 cells with an end stop (fed by a machine that a
    full belt blocks): at every take-time the plant's stages are the run's, and the state
    starts. Every stage but `cell` is met."""
    from test_state_belts import _belt_line

    model = _belt_line(variant)
    stages = set()
    for i in range(80):
        at = 0.3 + i * 0.55
        try:
            taken = state.take(model, at)
        except state.StateError:
            continue                    # an event due at that instant
        doc, problems, _mapper, _events = _plant_at(model, at)
        assert [p.code for p in problems if p.severity == "error"] == [], at
        assert _belts(doc) == _belts(taken), at
        assert doc["state"].get("blocks", {}).get("belt") == taken["state"].get(
            "blocks", {}).get("belt"), at
        assert state.start(model, doc)[1] == [], at
        stages |= {stage for _b, stage, *_rest in _belts(doc)}
    assert stages >= {"slot", "ride", "packed", "stop"}, stages


# ---------------------------------------------------------------- D461
def _manifests(doc):
    """Each formed batch: (block, its type, its members' types and the resources they hold)."""
    return sorted((e["block"], e.get("e_type"), [(m.get("e_type"), sorted(m.get("holds", {})))
                                                  for m in e["members"]])
                  for e in doc["state"].get("entities", []) if e.get("members"))


@pytest.mark.parametrize("name, times", [
    ("oven", (2.5, 6.2, 13.7, 20.1, 33.3, 47.95)), ("whole", (6.2, 13.7, 33.3)),
    ("pairs", (7.3, 15.7, 31.1)), ("mfg_line_library", (56.5, 700.3)),
    ("warehouse_library", (36.3, 52.07, 300.3))])
def test_a_batch_a_plant_shows_carries_the_members_its_manifest_names(name, times):
    """D461: a load in the oven with the parts that hold their tokens, a pair a MATCH formed,
    the bundled oven loads, trucks and truckloads: the members are the run's, and the state
    starts."""
    from test_state_batches import _oven, _pairs

    model = {"oven": _oven, "whole": lambda: _oven(split=False), "pairs": _pairs}.get(
        name, lambda: _bundled(name))()
    model["simulation"]["deterministic"] = True         # the times of the batch round
    for at in times:
        doc, problems, _mapper, _events = _plant_at(model, at)
        assert [p.code for p in problems if p.severity == "error"] == [], at
        assert _manifests(doc) == _manifests(state.take(model, at)) != [], at
        assert state.start(model, doc)[1] == [], at


def test_a_member_s_batch_is_an_id_and_without_a_manifest_the_batch_is_refused():
    from test_state_batches import _oven

    model = _oven()
    mapping = plant.scaffold(model)
    assert _at(mapping)["batch_field"] == "batch"
    _at(mapping)["batch_field"] = ["batch"]
    assert "E_PLANTMAP_FORMAT" in {p.code for p in plant.check(mapping, model)}
    mapper = plant.Mapper(plant.scaffold(model), model)
    for t, (ident, row) in enumerate([("1", {"at": "b"}), ("1", {"at": "+", "batch": True}),
                                      ("2", {"at": "b"}), ("2", {"at": "+", "batch": 2.5})]):
        mapper.feed([{"source": "s", "key": f"entity/{ident}", "value": row,
                      "source_time": t * 60.0, "received": t * 60.0}])
    assert mapper.report()["counts"]["value"] == 2
    del _at(mapping)["batch_field"]
    doc, problems, _mapper, _events = _plant_at(model, 6.2, mapping=mapping)
    assert "E_INITIAL_BATCH" in {p.code for p in state.start(model, doc)[1]}


# ---------------------------------------------------------------- D463, D464
def _cycles(doc):
    """(block, MOTION stage) of each job mid-cycle; (trip stage, cycle, held_by) of each pick
    or place; the cobots' until."""
    entities = doc["state"].get("entities", [])
    vehicles = doc["state"].get("fleets", {}).get("mobile_cobot_fleet_1/Auto Mobile cobots", {})
    return (sorted((e["block"], e["cycle"]["stage"]) for e in entities if "cycle" in e),
            sorted((e["trip"]["stage"], round(e["trip"]["cycle"], 9), e.get("held_by"))
                   for e in entities if (e.get("trip") or {}).get("cycle") is not None),
            [round(v["until"], 9) for v in vehicles.get("vehicles", []) if "until" in v])


@pytest.mark.parametrize("name, at, what", [
    ("automation_floor", 109.9, "place"), ("automation_floor", 425.0, "pick"),
    ("automation_floor", 1055.2, "motion"), ("automation_cell", 382.8, "motion"),
    ("automation_cell", 5737.8, "motion")])
def test_a_robot_s_step_and_a_cobot_s_cycle_put_the_job_back_where_it_was(name, at, what):
    """D463, D464: a cobot placing into the lathe or picking from it, and every MOTION job
    mid-cycle, are where the run has them; started, a fixed segment in progress ends when
    the run ends it (the take of the started run at the clock has the run's `until`)."""
    model = _bundled(name)
    model["simulation"]["deterministic"] = True
    taken = state.take(model, at)
    doc, problems, _mapper, _events = _plant_at(model, at)
    assert [p.code for p in problems if p.severity == "error"] == []
    assert _cycles(doc) == _cycles(taken)
    assert what == "motion" or any(e.get("trip", {}).get("cycle") for e in doc["state"]["entities"])
    started, refused = state.start(model, doc)
    assert refused == []
    session = state.SimSession(started, 0)
    session.advance(to=at)
    mine = sorted((cycle[0], round(cycle[1], 9)) for cycle in (
        getattr(e, "_cycle", None) for e in session.ctx.live_entities._items.values()) if cycle)
    theirs = sorted((e["cycle"]["stage"], round(e["cycle"]["until"], 9))
                    for e in taken["state"]["entities"] if "cycle" in e)
    assert len(mine) == len(theirs)
    for ours, run in zip(mine, theirs):
        fixed = name == "automation_floor" or ours[0] < 4       # the cell's process is drawn
        assert ours == run or not fixed, (ours, run)


@pytest.mark.parametrize("change, code", [
    ({"cycles": {"10": "pick", "20": "drop"}}, "E_PLANTMAP_FORMAT"),
    ({"cycles": None}, "E_PLANTMAP_FORMAT"), ({"unit": 7}, "E_PLANTMAP_NAME"),
    ({"fleet": "amr_fleet_1/Auto AMRs", "as": "step"}, "E_PLANTMAP_FORMAT"),
], ids=range(4))
def test_a_bad_tend_signal_is_refused(change, code):
    model = _bundled("automation_floor")
    mapping = plant.scaffold(model)
    signal = next(s for s in mapping["signals"] if s["as"] == "tend")
    signal.update({k: v for k, v in change.items() if v is not None})
    for key in [k for k, v in change.items() if v is None]:
        del signal[key]
    assert code in {p.code for p in plant.check(mapping, model)}


def test_a_cobot_s_cycle_is_said_once_and_a_stale_one_ends_now():
    """A state asserted again does not start a new cycle; a value the table does not name is
    no value; a pick said for longer than the model's pick ends at the clock."""
    model = _bundled("automation_floor")
    mapping = plant.scaffold(model)
    key = next(s for s in mapping["signals"] if s["as"] == "tend")["key"]
    mapper = plant.Mapper(mapping, model)
    for t, value in ((1.0, "pick"), (5.0, "pick"), (6.0, "drop"), (7.0, 3)):
        mapper.feed([{"source": "s", "key": key, "value": value, "source_time": t,
                      "received": t}])
    unit = mapper.snapshot()["vehicles"]["mobile_cobot_fleet_1/Auto Mobile cobots"][0]
    assert unit["cycle"] == ["pick", 1.0] and mapper.report()["counts"]["value"] == 2
    mapper.feed([{"source": "s", "key": key, "value": None, "source_time": 8.0, "received": 8.0}])
    assert mapper.snapshot()["vehicles"] == {}          # idle, and no node said
    model["simulation"]["deterministic"] = True
    doc, _problems, mapper, _events = _plant_at(model, 425.0)
    seen = dict(mapper.snapshot(), at=425.0)
    for vehicle in seen["vehicles"]["mobile_cobot_fleet_1/Auto Mobile cobots"]:
        if vehicle.get("cycle"):
            vehicle["cycle"] = [vehicle["cycle"][0], 400.0]
    doc, problems = state.from_plant(model, seen)
    records = doc["state"]["fleets"]["mobile_cobot_fleet_1/Auto Mobile cobots"]["vehicles"]
    assert [r["until"] for r in records if "until" in r] == [425.0]
    assert state.start(model, doc)[1] == []


def test_a_step_is_a_motion_block_s_and_a_whole_number():
    model = _bundled("automation_cell")
    mapping = plant.scaffold(model)
    assert [s["block"] for s in mapping["signals"] if s["as"] == "step"] == ["assemble"]
    bad = {**mapping, "signals": [{"key": "x", "as": "step", "block": "prepare"}]}
    assert "E_PLANTMAP_NAME" in {p.code for p in plant.check(bad, model)}
    mapper = plant.Mapper(mapping, model)
    for t, value in enumerate([2, "two", True, 2.5, -1, 3, 3, None, 0]):
        mapper.feed([{"source": "s", "key": "block/assemble/step", "value": value,
                      "source_time": t + 1.0, "received": t + 1.0}])
        if t == 6:
            assert mapper.snapshot()["steps"] == {"assemble": [3, 6.0]}    # said again: 3 since 6
    assert mapper.snapshot()["steps"] == {"assemble": [0, 9.0]}
    assert mapper.report()["counts"]["value"] == 4


def test_a_step_beyond_the_block_s_segments_is_no_step():
    """M1: one bad PLC word must not make `start` refuse the forecast: the job runs afresh."""
    model = _bundled("automation_cell")
    mapper = plant.Mapper(plant.scaffold(model), model)
    for t, value in enumerate([9999, 3]):
        mapper.feed([{"source": "s", "key": "block/assemble/step", "value": value,
                      "source_time": t + 1.0, "received": t + 1.0}])
    assert mapper.snapshot()["steps"] == {"assemble": [3, 2.0]}
    assert mapper.report()["counts"]["value"] == 1


def test_a_step_older_than_its_job_is_said_and_the_job_runs_afresh():
    """M2 (D464): the previous job's step is not this job's."""
    model = _bundled("automation_cell")
    model["simulation"]["deterministic"] = True
    _doc, _problems, mapper, _events = _plant_at(model, 382.8)
    seen = dict(mapper.snapshot(), at=382.8)
    assert seen["steps"]
    seen["steps"] = {block: [step, 0.0] for block, (step, _since) in seen["steps"].items()}
    doc, problems = state.from_plant(model, seen)
    assert not any("cycle" in e for e in doc["state"]["entities"])
    assert any("predates" in p.message for p in problems)
    assert state.start(model, doc)[1] == []


def test_a_part_that_only_waited_for_a_cell_did_not_leave_the_belt_s_end():
    """M3: `exits` stamps the parts that rode the belt out, not one that never boarded."""
    from test_state_belts import _belt_line

    model = _belt_line("stop")
    mapper = plant.Mapper(plant.scaffold(model), model)
    for t, ident, at in [(1, "1", "belt"), (2, "2", "belt"), (3, "3", "belt"), (4, "4", "belt"),
                         (5, "5", "belt"), (6, "5", None)]:
        mapper.feed([{"source": "s", "key": f"entity/{ident}", "value": {"at": at},
                      "source_time": t * 60.0, "received": t * 60.0}])
    assert "belt" not in mapper.snapshot()["exits"]


# ---------------------------------------------------------------- the twin
@pytest.mark.parametrize("name, at, speed, kinds", [
    ("warehouse_library", 508.5, 60.0, {"belt", "members", "transfer", "trip"}),
    ("automation_floor", 200.5, 1.0, {"cycle", "members", "trip"})])
@pytest.mark.slow
def test_a_twin_forecasts_from_its_plant_and_is_scored(name, at, speed, kinds, tmp_path):
    """The headline: a recorded log of the model's own run stands in for the plant; the
    twin's forecast starts from the plant's state (belts, trucks and truckloads, putaways,
    picks and places, robots mid-cycle, both fleets) and is scored against what the log then
    shows, the stored score equal to its recomputation from the log."""
    from nagasim import score, twin

    model = _bundled(name)
    mapping = plant.scaffold(model, counters=True)
    recorder = plant.Recorder()
    api.run_model(model, replications=1, seed=1, trace=recorder, record_logs=False)
    log = plant.log_of(recorder.events, mapping, attributes=recorder.attributes)

    class Clock:
        wall, mono = 1_800_000_000.0, 0.0

    clock = Clock()
    feed = twin.LogFeed(log, speed=speed, monotonic=lambda: clock.mono)
    source = twin.PlantSource(model, mapping, feed, name=name, clock=feed.clock)
    horizon = 60.0 if speed > 1 else 120.0
    cycle = twin.Cycle(source, clock=lambda: clock.wall, monotonic=lambda: clock.mono,
                       out_dir=tmp_path, horizon=horizon, replications=3, seed=3,
                       forecast_every=1e9)
    clock.mono = at
    doc = cycle.step()
    assert doc["forecast"]["status"] == "ok", doc["problems"]
    taken, problems = state.from_plant(model, dict(source.mapper.snapshot(), at=doc["at"]))
    assert doc["forecast"]["state"] == taken["hash"]
    assert [p.code for p in problems if p.severity == "error"] == []
    assert taken["state"]["fleets"] and kinds <= {k for e in taken["state"]["entities"] for k in e}
    assert name != "automation_floor" or any(e.get("trip", {}).get("stage") == "unload"
                                             for e in taken["state"]["entities"])  # a place
    for clock.mono in (at + 20, at + 40, at + 61):
        doc = cycle.step()
    assert doc["scores"]["basis"]["points"] > 0 and doc["scores"]["rows"]
    lines, differences = score.recompute(tmp_path, model, mapping, log)
    assert differences == 0, lines


# ---------------------------------------------------------------- D459, D462
def _transfers(doc):
    return [(e["block"], e["transfer"], e["since"]) for e in doc["state"].get("entities", [])
            if "transfer" in e]


def _ordered_draws():
    """`_draws` with the amount on the order line: an ASSIGN gives each draw `qty` 4."""
    from test_state import _block
    from test_state_batches import _draws

    model = _draws()
    model["blocks"].insert(1, _block("line", "ASSIGN", assignments=[
        {"target": "entity.qty", "expr": "4"}]))
    model["blocks"][2]["params"]["amount"] = "entity.qty"
    model["links"] = [{"source": "c", "target": "line"}, {"source": "line", "target": "draw"},
                      {"source": "draw", "target": "d"}]
    return model


def test_an_entity_waiting_at_a_transfer_waits_for_its_block_s_amount_or_its_order_line():
    """D462: puts and takes of a number wait for it; a draw of `entity.qty` waits for the
    quantity its row gives (D459). Their order and since are the run's."""
    from test_state_batches import _stock

    for model, at in ((_stock(), 19.9), (_stock(), 37.7), (_ordered_draws(), 6.0)):
        doc, problems, _mapper, _events = _plant_at(model, at)
        assert [p.code for p in problems if p.severity == "error"] == [], at
        assert _transfers(doc) == _transfers(state.take(model, at)) != [], at
        assert state.start(model, doc)[1] == [], at
    assert _at(plant.scaffold(_ordered_draws()))["attributes"] == {"qty": "qty"}
    mapping = plant.scaffold(model)
    for signal in mapping["signals"]:
        signal.pop("attributes", None)
    _doc, problems, _mapper, _events = _plant_at(model, 6.0, mapping=mapping)
    assert any(p.code == "E_STATE_UNSUPPORTED" and "TRANSFER 'draw'" in p.message
               for p in problems)


@pytest.mark.parametrize("attributes, code", [
    ({"q": "qty"}, None), ({"q": "id"}, "E_PLANTMAP_FORMAT"), ({"q": "_x"}, "E_PLANTMAP_FORMAT"),
    ({"q": "step"}, "E_PLANTMAP_FORMAT"), ({"q": "station"}, "E_PLANTMAP_FORMAT"),
    ({"q": "a b"}, "E_PLANTMAP_FORMAT"), ({"q": "x" * 65}, "E_PLANTMAP_FORMAT"),
    ({"q": 5}, "E_PLANTMAP_FORMAT"), ("qty", "E_PLANTMAP_FORMAT"),
    ({f"c{i}": f"a{i}" for i in range(257)}, "E_PLANTMAP_SIZE"),
], ids=range(10))
def test_a_bad_attributes_table_is_refused(attributes, code):
    mapping = plant.scaffold(_ordered_draws())
    _at(mapping)["attributes"] = attributes
    found = {p.code for p in plant.check(mapping, _ordered_draws())}
    assert (code in found) if code else not found


def test_only_a_number_sets_an_attribute():
    mapping = {"format": 1, "time": {"epoch": "1970-01-01T00:00:00+00:00", "unit": "minutes"},
               "signals": [{"key": "e/{id}", "field": "at", "as": "at", "blocks": {"d": "draw"},
                            "attributes": {"q": "qty"}}]}
    mapper = plant.Mapper(mapping, _ordered_draws())
    for value, t in (("4", 1.0), (True, 2.0), (None, 3.0), (7, 4.0)):
        mapper.feed([{"source": "s", "key": "e/1", "value": {"at": "d", "q": value},
                      "source_time": t * 60, "received": t * 60}])
    assert mapper.snapshot()["entities"][0]["attributes"] == {"qty": 7}
    assert mapper.report()["counts"]["value"] == 3
