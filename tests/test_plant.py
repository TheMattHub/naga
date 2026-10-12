"""The observed timeline (PHASE7 7.11, D252-D259): the plant mapping, the mapper, the exit
check "the same view state from a recorded log and from the simulated run it was taken from",
live, and a state from what a plant looks like now."""
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

import nagasim
from conftest import chain, create, dispose, layout, metric_value, process

from nagasim import api, live, plant, state
from nagasim.core import playback
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceEvent, TraceRecorder

MODELS = Path(nagasim.__file__).resolve().parent / "models"
SERVER = [{"name": "Server", "capacity": 1}]
EPOCH = "1970-01-01T00:00:00+00:00"


def _line(**sections):
    """Arrivals every 2, a 1.5 service, constant: nothing random."""
    c, p, d = create("c", dist_params={"value": 2.0}), process("p", value=1.5), dispose("d")
    sections.setdefault("simulation", {"time_limit": 20, "warmup": 0, "replications": 1,
                                       "time_unit": "seconds"})
    sections.setdefault("resources", SERVER)
    return layout([c, p, d], chain(c, p, d), **sections)


def _mapping(*signals, **time):
    return {"format": 1, "time": {"epoch": EPOCH, "unit": "seconds", **time},
            "signals": list(signals)}


def _codes(problems):
    return sorted({p.code for p in problems})


# ---------------------------------------------------------------- the mapping (D252)
def test_a_mapping_of_every_kind_checks_clean_against_its_model():
    mapping = _mapping(
        {"source": "line", "key": "press/state", "as": "state", "resource": "Server",
         "states": {"RUN": "busy", "IDLE": "idle", "FAULT": "down"}},
        {"key": "press/units", "as": "capacity", "resource": "Server"},
        {"key": "wo/{id}", "field": "station", "as": "at", "blocks": {"OP10": "p"},
         "gone": ["DONE"], "merged": ["KIT"], "type_field": "product", "types": {"A": "DEFAULT"}},
        {"key": "gate/rfid", "as": "seen", "block": "p", "type": "DEFAULT"},
        {"key": "exit/rfid", "as": "seen", "leaves": True})
    assert plant.check(mapping, _line()) == []
    assert plant.check(mapping) == []           # with no model: the structure alone


@pytest.mark.parametrize("change, code", [
    (lambda m: m.update(format=2), "E_PLANTMAP_FORMAT"),
    (lambda m: m.update(script="x"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["time"].update(epoch="yesterday"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["time"].update(unit="fortnights"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["time"].update(stamp="mine"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][0].update({"as": "counter"}), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][0].update(expression="1+1"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][0].update(key="a/{id}"), "E_PLANTMAP_FORMAT"),      # only `at`
    (lambda m: m["signals"][0].update(key="a/*/*"), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][0].update(key=""), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][0].update(key="k" * 2000), "E_PLANTMAP_SIZE"),
    (lambda m: m["signals"][1].update(states={"X": "melting"}), "E_PLANTMAP_FORMAT"),
    (lambda m: m["signals"][1].update(states={str(i): "down" for i in range(10_001)}),
     "E_PLANTMAP_SIZE"),
    (lambda m: m.update(signals=[m["signals"][0]] * 2001), "E_PLANTMAP_SIZE"),
    (lambda m: m["signals"][0].update(resource="Ghost"), "E_PLANTMAP_NAME"),
    (lambda m: m["signals"][2].update(blocks={"OP10": "nowhere"}), "E_PLANTMAP_NAME"),
    (lambda m: m["signals"][3].update(level="Pond"), "E_PLANTMAP_NAME"),
    # two signals drive one thing (D256)
    (lambda m: m["signals"].append({"key": "n", "as": "busy", "resource": "Server"}),
     "E_PLANTMAP_DOUBLE"),
    (lambda m: m["signals"].append({"key": "q", "as": "queue", "block": "p"}),
     "E_PLANTMAP_DOUBLE"),
])
def test_what_a_mapping_may_not_be(change, code):
    mapping = _mapping(
        {"key": "press/job", "as": "job", "resource": "Server"},
        {"key": "press/state", "as": "state", "resource": "Server", "states": {"F": "down"}},
        {"key": "wo/{id}", "as": "at", "blocks": {"OP10": "p"}},
        {"key": "tank", "as": "level", "level": "Tank", "scale": 0.5})
    model = _line(levels=[{"name": "Tank", "initial": 0.0}])
    assert plant.check(mapping, model) == []
    change(mapping)
    assert code in _codes(plant.check(mapping, model))


def test_a_mapping_file_is_read_bounded_and_is_only_data(tmp_path):
    path = tmp_path / "line.plantmap.json"
    path.write_text(json.dumps(_mapping()), encoding="utf-8")
    assert plant.load(path) == (_mapping(), [])
    path.write_text("{not json", encoding="utf-8")
    assert _codes(plant.load(path)[1]) == ["E_PLANTMAP_FORMAT"]
    path.write_text(" " * (plant.LIMITS["map_bytes"] + 1), encoding="utf-8")
    assert _codes(plant.load(path)[1]) == ["E_PLANTMAP_SIZE"]
    assert _codes(plant.load(tmp_path / "missing.json")[1]) == ["E_PLANTMAP_FORMAT"]


def test_the_published_schema_accepts_the_scaffold_and_names_every_kind():
    jsonschema = pytest.importorskip("jsonschema")
    schema = plant.schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    for anonymous in (False, True):
        jsonschema.validate(plant.scaffold(_line(), anonymous=anonymous), schema)
    assert set(schema["properties"]["signals"]["items"]["properties"]["as"]["enum"]) \
        == set(plant.KINDS)


@pytest.mark.parametrize("path", sorted(MODELS.glob("*.json")), ids=lambda p: p.stem)
def test_the_scaffold_of_every_bundled_model_checks_clean(path):
    model = api.load_layout(path)
    for anonymous in (False, True):
        assert plant.check(plant.scaffold(model, anonymous=anonymous), model) == []


# ---------------------------------------------------------------- the mapper (D253-D256)
LINE = _mapping(
    {"key": "press/state", "as": "state", "resource": "Server",
     "states": {"RUN": "busy", "IDLE": "idle", "FAULT": "down", "FULL": "blocked", "2": "busy"}},
    {"key": "press/units", "as": "capacity", "resource": "Server"},
    {"key": "wo/{id}", "field": "station", "as": "at", "blocks": {"OP10": "p", "IN": "c"},
     "gone": ["DONE"], "merged": ["KIT"], "type_field": "product", "types": {"A": "PART"}},
    {"key": "gate/rfid", "as": "seen", "block": "p", "type": "TOTE"},
    {"key": "exit/rfid", "as": "seen", "leaves": True},
    {"key": "tank", "as": "level", "level": "Tank", "scale": 0.5})
COUNTS = _mapping(
    {"key": "press/job/*", "as": "job", "resource": "Server"},
    {"key": "press/full", "as": "blocked", "resource": "Server"},
    {"key": "saw/busy", "as": "busy", "resource": "Saw"},
    {"key": "p/len", "as": "queue", "block": "p", "type": "PART"},
    {"key": "wo/{id}", "as": "at", "blocks": {"IN": "c"}})


def ob(key, value, t, source="line", received=None):
    return {"source": source, "key": key, "value": value, "source_time": t,
            "received": t if received is None else received, "seq": 0}


def _state(mapper, observations):
    """(the events, the timeline's state at the mapper's clock)."""
    events = mapper.feed(observations)
    return events, Timeline(events).state_at(mapper.now)


def _reasons(mapper):
    return {reason: n for reason, n in mapper.report()["counts"].items()
            if reason not in ("unread", "synthetic")}


def test_a_mapper_refuses_a_mapping_that_does_not_check():
    with pytest.raises(plant.PlantError) as refused:
        plant.Mapper({"format": 1})
    assert _codes(refused.value.problems) == ["E_PLANTMAP_FORMAT"]
    with pytest.raises(plant.PlantError):
        plant.Mapper(LINE, _line())             # the model has no level 'Tank'


def test_a_station_state_is_a_resource_state_and_units_nobody_names():
    mapper = plant.Mapper(LINE)
    events, state = _state(mapper, [ob("press/units", 2, 1.0), ob("press/state", "RUN", 2.0)])
    assert state["resources"] == {"Server": {"state": "busy", "busy": 1, "capacity": 2.0}}
    assert state["entities"] == {} and state["in_service"] == {}
    seize = events[-1]
    assert (seize.kind, seize.entity_id, seize.block, seize.target) \
        == ("entity_seized", -1, "", "Server")              # synthetic, by construction (D254)
    for value, shown in (("FULL", "blocked"), ("FAULT", "down"), ("IDLE", "idle"), (2.0, "busy")):
        _events, state = _state(plant.Mapper(LINE), [ob("press/state", "RUN", 2.0),
                                                     ob("press/state", "FAULT", 3.0),
                                                     ob("press/state", value, 4.0)])
        assert state["resources"]["Server"]["state"] == shown
    assert _codes(mapper.problems()) == ["W_PLANT_SYNTHETIC"]


def test_an_entity_is_created_where_it_is_first_seen_and_moves_when_seen_elsewhere():
    mapper = plant.Mapper(LINE)
    events, state = _state(mapper, [
        ob("wo/7", {"station": "IN", "product": "A"}, 1.0),
        ob("wo/7", {"station": "OP10", "product": "A"}, 2.0),
        ob("wo/8", {"station": "OP10", "product": "A"}, 3.0),
        ob("gate/rfid", "T-1", 3.5)])
    assert [e.kind for e in events] == ["entity_created", "entity_moved", "entity_created",
                                        "entity_created"]
    seven, eight, tote = mapper.ids["7"], mapper.ids["8"], mapper.ids["T-1"]
    assert state["entities"] == {seven: ("p", "PART"), eight: ("p", "PART"), tote: ("p", "TOTE")}
    assert state["queues"] == {"p": [seven, eight, tote]}
    assert min(mapper.ids.values()) >= 0 and _reasons(mapper) == {}
    assert mapper.report()["counts"].get("synthetic", 0) == 0
    # it leaves by a value of `gone`, by a null (a polled row that is no more), or at an exit
    events = mapper.feed([ob("wo/7", {"station": "DONE"}, 4.0), ob("wo/8", None, 5.0),
                          ob("exit/rfid", "T-1", 6.0)])
    assert [e.kind for e in events] == ["entity_disposed"] * 3 and mapper.ids == {}
    events = plant.Mapper(LINE).feed([
        ob("wo/7", {"station": "OP10"}, 1.0), ob("wo/7", {"station": "KIT"}, 2.0),
        ob("wo/7", {"station": "OP10"}, 3.0)])
    timeline = Timeline(events)
    assert [e.kind for e in events] == ["entity_created", "entity_absorbed", "entity_moved"]
    assert timeline.state_at(2.5)["counts"] == {} and timeline.state_at(3.0)["counts"] == {"p": 1}
    assert timeline.created_until(3.0) == 1 and timeline.disposed_until(3.0) == 0


def test_a_job_puts_the_entity_in_service_and_a_count_holds_units():
    mapper = plant.Mapper(COUNTS)
    events, state = _state(mapper, [
        ob("wo/7", "IN", 1.0), ob("press/job/1", "7", 2.0), ob("saw/busy", 3, 2.0),
        ob("press/full", 1, 2.0), ob("p/len", 2, 2.0)])
    seven = mapper.ids["7"]
    assert state["in_service"] == {"c": 1} and state["queues"] == {"p": [-2, -3]}
    assert state["resources"]["Server"] == {"state": "blocked", "busy": 1, "capacity": None}
    assert state["resources"]["Saw"]["busy"] == 3
    assert state["entities"][-2] == ("p", "PART")
    events = mapper.feed([ob("press/job/1", None, 3.0), ob("saw/busy", 1, 3.0),
                          ob("press/full", 0, 3.0), ob("p/len", 1, 3.0)])
    assert [e.kind for e in events] == ["entity_released", "entity_released", "entity_released",
                                        "resource_state", "entity_disposed"]
    assert events[-1].entity_id == -2                       # the oldest leaves first
    assert events[0].entity_id == seven and events[1].entity_id == -1
    assert mapper.report()["counts"]["synthetic"] == 3 + 1 + 2 + 2 + 1 + 1


def test_a_level_is_a_breakpoint_scaled():
    mapper = plant.Mapper(LINE)
    events = mapper.feed([ob("tank", 10, 0.0), ob("tank", 10, 4.0), ob("tank", 30, 8.0)])
    timeline = Timeline(events)
    assert [timeline.level_at("Tank", t) for t in (0.0, 4.0, 6.0, 8.0)] == [5.0, 5.0, 10.0, 15.0]


def test_an_observation_delivered_twice_emits_nothing():
    for mapping, batch in (
            (LINE, [ob("press/units", 2, 1.0), ob("press/state", "RUN", 2.0), ob("tank", 4, 2.0),
                    ob("wo/7", {"station": "OP10", "product": "A"}, 3.0), ob("gate/rfid", "T", 3.0)]),
            (COUNTS, [ob("wo/7", "IN", 1.0), ob("press/job/1", "7", 2.0), ob("p/len", 2, 2.0),
                      ob("saw/busy", 2, 2.0), ob("press/full", 1, 2.0)])):
        mapper = plant.Mapper(mapping)
        assert mapper.feed(batch) and mapper.feed(batch) == [] and mapper.feed(batch[::-1]) == []


def test_what_does_not_fit_is_dropped_counted_and_coded():
    mapper = plant.Mapper(COUNTS)
    assert mapper.feed([
        ob("nobody/reads/this", 1, 1.0),
        ob("wo/7", "NOWHERE", 1.0),             # a value the table lacks
        ob("p/len", -1, 1.0), ob("p/len", 2.5, 1.0), ob("p/len", True, 1.0),
        ob("p/len", plant.LIMITS["count"] + 1, 1.0), ob("saw/busy", "many", 1.0),
        ob("wo/7", {"row": "IN"}, 1.0),         # a row, and the signal names no field
        ob("p/len", [1, 2], 1.0), ob(7, 1, 1.0), ob("k" * 2000, 1, 1.0),
        ob("p/len", 1, -5.0), ob("p/len", 1, float("nan")), ob("p/len", 1, 1e300),
        ob("p/len", 1, None, received=float("inf"))]) == []
    assert _reasons(mapper) == {"value": 6, "field": 1, "type": 2, "oversize": 1,
                                "time_range": 2, "no_time": 2, "no_stamp": 2}
    assert mapper.report()["counts"]["unread"] == 1
    assert _codes(mapper.problems()) == ["W_PLANT_DROPPED", "W_PLANT_TIME"]
    assert plant.Mapper(LINE).feed([ob("tank", "full", 1.0), ob("press/state", "MELTING", 1.0),
                                    ob("press/units", -1, 1.0)]) == []


def test_contradictions_have_a_rule_and_a_code():
    mapper = plant.Mapper(COUNTS)
    _events, state = _state(mapper, [ob("press/job/1", "99", 1.0)])         # seen nowhere
    assert state["resources"]["Server"]["busy"] == 1 and state["in_service"] == {}
    assert _reasons(mapper) == {"job_unseen": 1}
    mapper = plant.Mapper(LINE)
    mapper.feed([ob("wo/7", {"station": "OP10", "product": "A"}, 1.0), ob("gate/rfid", "7", 2.0)])
    assert _reasons(mapper) == {"retyped": 1}   # PART stays PART: the first type stands
    assert _codes(mapper.problems()) == ["W_PLANT_CONTRADICTION"]


def test_time_is_the_source_stamp_from_the_epoch_in_the_models_unit():
    mapping = _mapping({"key": "tank", "as": "level", "level": "Tank"},
                       epoch="2026-10-05T06:00:00+02:00", unit="minutes")
    base = datetime(2026, 10, 5, 4, tzinfo=timezone.utc).timestamp()
    mapper = plant.Mapper(mapping)
    events = mapper.feed([ob("tank", 1, base + 600), ob("tank", 2, None, received=base + 1200),
                          ob("tank", 3, base + 9999, received=base + 1800)])    # ahead by hours
    assert [e.t for e in events] == [10.0, 20.0, 30.0] and mapper.now == 30.0
    assert _reasons(mapper) == {"no_stamp": 1, "ahead": 1}
    assert _codes(mapper.problems()) == ["W_PLANT_TIME"]
    assert mapper.stamp() == "2026-10-05T04:30:00+00:00"
    received = plant.Mapper({**mapping, "time": {**mapping["time"], "stamp": "received"}})
    assert [e.t for e in received.feed([ob("tank", 1, base, received=base + 60)])] == [1.0]
    # the unit defaults to the model's own
    hours = _line(simulation={"time_limit": 9, "time_unit": "hours"},
                  levels=[{"name": "Tank", "initial": 0.0}])
    del mapping["time"]["unit"]
    assert plant.Mapper(mapping, hours).feed([ob("tank", 1, base + 7200)])[0].t == 2.0


def test_a_stale_event_is_dropped_and_a_late_one_lands_on_the_last_instant():
    mapper = plant.Mapper(_mapping({"key": "a", "as": "level", "level": "A"},
                                   {"key": "b", "as": "level", "level": "B"}))
    events = mapper.feed([ob("a", 1, 10.0), ob("a", 2, 20.0), ob("a", 9, 15.0),    # stale
                          ob("b", 5, 12.0)])                                       # late
    assert [(e.resource, e.t, e.value) for e in events] == [("A", 10.0, 1.0), ("A", 20.0, 2.0),
                                                            ("B", 20.0, 5.0)]
    assert _reasons(mapper) == {"stale": 1, "late": 1}
    Timeline([], live=True).append(events)      # in time order: a live timeline takes them


def test_a_hole_is_recorded_with_its_time_and_the_timeline_goes_on():
    mapper = plant.Mapper(COUNTS)
    events = mapper.feed([ob("wo/7", "IN", 1.0), {"source": "line", "lost": 3},
                          {"source": "line", "gap": True}, ob("wo/8", "IN", 2.0)])
    assert len(events) == 2 and mapper.report()["holes"] == [(1.0, 3, False), (1.0, 0, True)]
    assert _codes(mapper.problems()) == ["W_PLANT_GAP"]


def test_a_flood_cannot_grow_a_mapper_past_its_ceilings(monkeypatch):
    monkeypatch.setitem(plant.LIMITS, "entities", 5)
    monkeypatch.setitem(plant.LIMITS, "keys", 8)
    mapper = plant.Mapper(COUNTS)
    events = mapper.feed([ob(f"wo/{i}", "IN", 1.0) for i in range(50)] + [ob("p/len", 4, 2.0)])
    assert len(events) == 5 and len(mapper.ids) == 5
    assert _reasons(mapper) == {"entities": 46, "forgotten": 43}
    capped = plant.Mapper(COUNTS, max_events=3)
    assert len(capped.feed([ob("saw/busy", n % 2, float(n)) for n in range(1, 20)])) == 3
    assert capped.truncated and _reasons(capped)["ceiling"] == 16


# ---------------------------------------------------------------- the exit check (D257)
# "The same view state from a recorded log and from the simulated run it was taken from."
def _recorded(model, cut=2000, seed=1):
    recorder = TraceRecorder(max_events=cut)
    api.run_model(model, replications=1, seed=seed, trace=recorder, record_logs=False)
    return recorder.events


def _observed(model, events, tmp_path, *, anonymous=False, time=None, disturb=None):
    """The run's trace as a plant log on disk, read back and mapped: (events, mapper)."""
    mapping = plant.scaffold(model, anonymous=anonymous)
    # Seconds from 1970: a stamp is then the model clock itself, to the last bit.
    mapping["time"] = time or {"epoch": EPOCH, "unit": "seconds"}
    log = plant.log_of(events, mapping)
    plant.write_log(tmp_path / "plant.jsonl", disturb(log) if disturb else log)
    observations, problems = plant.read_log(tmp_path / "plant.jsonl")
    assert problems == [] and len(observations) >= len(log)
    return plant.replay(mapping, model, observations)


def _instants(events, between_only=False):
    times = sorted({e.t for e in events})
    between = [(a + b) / 2 for a, b in zip(times, times[1:]) if b - a > 1e-6 or not between_only]
    return between if between_only else sorted({*times, *between})


def _renamed(state, events, simulated):
    """*state* with the mapper's entity ids as the run's: the n-th created is the n-th."""
    names = dict(zip((e.entity_id for e in events if e.kind == "entity_created"),
                     (e.entity_id for e in simulated if e.kind == "entity_created")))
    return {**state, "entities": {names[i]: v for i, v in state["entities"].items()},
            "queues": {b: [names[i] for i in q] for b, q in state["queues"].items()}}


def _derived(model):
    sources = playback.asset_state_sources(model)
    units = playback.unit_sources(model, sources)
    return lambda timeline, state, t: (
        playback.asset_states(state, sources), playback.unit_counts(state, units),
        playback.resource_rows(state), playback.kpi_snapshot(timeline, t))


def _moves(timeline, t0, t1, names=None):
    return [(names[m[0]] if names else m[0], *m[1:5]) for m in timeline.moves_between(t0, t1)]


LOST_KINDS = ("movers", "handling", "soc", "chargers", "charging", "phases", "phases_all",
              "placed", "tending", "agents", "agent_links", "entity_agents")


def _compare(model, simulated, events, *, lost=(), instants=None):
    """Every key of the state but *lost*, and every derivation, at every instant. With an
    entity on a timed link only what the spec promises: it is in the block it left."""
    run, plant_ = Timeline(simulated), Timeline(events)
    derive, previous = _derived(model), None
    for t in instants or _instants(simulated):
        ours, theirs = run.state_at(t), _renamed(plant_.state_at(t), events, simulated)
        for key in lost:
            assert not theirs[key], key
        if ours["in_transit"]:
            assert theirs["in_transit"] == {}
            for entity, (block, _to, _progress, e_type, *_rest) in ours["in_transit"].items():
                assert theirs["entities"][entity] == (block, e_type)
            assert {i: v for i, v in theirs["entities"].items() if i not in ours["in_transit"]} \
                == ours["entities"]
            assert (ours["resources"], ours["levels"]) == (theirs["resources"], theirs["levels"])
        else:
            assert {k: v for k, v in ours.items() if k not in lost} \
                == {k: v for k, v in theirs.items() if k not in lost}, t
            if not lost:
                assert derive(run, ours, t) == derive(plant_, theirs, t), t
        assert playback.kpi_snapshot(run, t) == playback.kpi_snapshot(plant_, t)
        previous = t
    return previous


@pytest.mark.parametrize("name", [
    "single_server", "machine_with_breakdowns", "two_teller_set", "priority_rework",
    "call_centre_day", "job_shop_jackson", "impatient_customers",   # queues, sets, failures
    "two_cell_line", "clinic_library",                              # submodels
    "hold_on_store", "mfg_line_library",                            # batches
    "line_with_buffers",                                            # blocking
    "energy_pv_battery", "sd_sir", "inventory_sS", "supply_chain_library"])     # levels
def test_a_recorded_log_gives_the_view_state_of_the_run_it_was_taken_from(name, tmp_path):
    model = api.load_layout(MODELS / f"{name}.json")
    simulated = _recorded(model)
    events, mapper = _observed(model, simulated, tmp_path)
    assert events and not mapper.truncated
    # A shift that ends while jobs finish has more units busy than its capacity: no report.
    assert set(mapper.report()["counts"]) <= {"synthetic", "duplicate"}
    _compare(model, simulated, events)
    names = dict(zip((e.entity_id for e in events if e.kind == "entity_created"),
                     (e.entity_id for e in simulated if e.kind == "entity_created")))
    # Lost: which link a move took (the branch), and where a part back out of a batch came from.
    end = simulated[-1].t
    theirs, ours = _moves(Timeline(events), -1.0, end, names), _moves(Timeline(simulated), -1.0, end)
    assert theirs == [m if seen[2] else (*m[:2], "", *m[3:]) for m, seen in zip(ours, theirs)]
    assert len(theirs) == len(ours) and (name == "mfg_line_library") == any(
        not m[2] for m in theirs)


@pytest.mark.parametrize("name", ["job_shop_routed", "decide_routing", "mes_twin_shop"])
def test_an_entity_on_a_timed_link_stays_in_the_block_it_left(name, tmp_path):
    model = api.load_layout(MODELS / f"{name}.json")
    simulated = _recorded(model)
    assert any(e.kind == "entity_moved" and e.duration > 0 for e in simulated)
    _compare(model, simulated, _observed(model, simulated, tmp_path)[0])


@pytest.mark.parametrize("name, lost", [
    ("automation_cell", ("phases", "phases_all", "placed")),
    ("ed_patient_agents", ("entity_agents",)),
    ("warehouse_forklifts", ("movers", "handling")),
    ("abm_wolf_sheep", ("agents",))])
def test_what_is_not_mappable_is_lost_and_the_rest_is_the_run(name, lost, tmp_path):
    model = api.load_layout(MODELS / f"{name}.json")
    simulated = _recorded(model)
    run = Timeline(simulated)
    seen = [run.state_at(t) for t in _instants(simulated)[::50]]
    assert all(any(state[key] for state in seen) for key in lost) and set(lost) <= set(LOST_KINDS)
    _compare(model, simulated, _observed(model, simulated, tmp_path)[0], lost=lost)


@pytest.mark.parametrize("name", ["single_server", "line_with_buffers", "inventory_sS"])
def test_queue_lengths_and_unit_counts_give_the_counts_not_the_identities(name, tmp_path):
    model = api.load_layout(MODELS / f"{name}.json")
    simulated = _recorded(model)
    events, mapper = _observed(model, simulated, tmp_path, anonymous=True)
    run, plant_ = Timeline(simulated), Timeline(events)
    assert mapper.ids == {} and mapper.report()["counts"]["synthetic"] == len(events) - sum(
        e.kind == "level_changed" or e.state in ("capacity", "down", "up") for e in events)
    for t in _instants(simulated):
        ours, theirs = run.state_at(t), plant_.state_at(t)
        for key in ("counts", "resources", "levels"):
            assert ours[key] == theirs[key], (key, t)
        assert all(entity < -1 for entity in theirs["entities"]) and theirs["in_service"] == {}
        assert playback.kpi_snapshot(run, t)["wip"] == playback.kpi_snapshot(plant_, t)["wip"]
        assert playback.resource_rows(ours) == playback.resource_rows(theirs)
    # lost: created and completed count station visits
    assert plant_.created_until(simulated[-1].t) > run.created_until(simulated[-1].t)


def test_a_real_epoch_a_shuffled_log_and_lines_delivered_twice(tmp_path):
    model = api.load_layout(MODELS / "machine_with_breakdowns.json")
    simulated, delivered = _recorded(model, cut=800), []

    def disturb(log):
        delivered.append(len(log[::3]))
        twice = log + log[::3]
        random.Random(7).shuffle(twice)
        return twice
    events, mapper = _observed(model, simulated, tmp_path, disturb=disturb,
                               time={"epoch": "2026-10-05T06:00:00+02:00", "unit": "minutes"})
    # A line delivered twice says nothing new (a leaving said twice is not even counted).
    counts = mapper.report()["counts"]
    assert set(counts) == {"duplicate"} and 0 < counts["duplicate"] <= delivered[0]
    # A stamp is a float of seconds since 1970: an event lands within a microsecond of its
    # time, so the state is compared between two events, not on one.
    _compare(model, simulated, events, instants=_instants(simulated, between_only=True))


def test_a_plant_that_says_states_and_lengths_is_written_and_read_back(tmp_path):
    mapping = _mapping(
        {"key": "press/state", "as": "state", "resource": "Server",
         "states": {"RUN": "busy", "IDLE": "idle"}},
        {"key": "press/queue", "as": "queue", "block": "p", "type": "DEFAULT"})
    model = _line()
    simulated = _recorded(model)
    log = plant.log_of(simulated, mapping)
    assert {line["key"] for line in log} == {"press/state", "press/queue"}
    assert {line["value"] for line in log if line["key"] == "press/state"} == {"RUN", "IDLE"}
    events, _mapper = plant.replay(mapping, model, log)
    run, plant_ = Timeline(simulated), Timeline(events)
    for t in _instants(simulated):
        ours, theirs = run.state_at(t), plant_.state_at(t)
        assert ours["counts"].get("p", 0) == theirs["counts"].get("p", 0)
        assert ours["resources"]["Server"]["state"] \
            == theirs["resources"].get("Server", {"state": "idle"})["state"]


def test_a_log_is_read_bounded_line_by_line(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    good = json.dumps(ob("tank", 1, 1.0))
    path.write_text("\n".join([good, "[1, 2]", "{broken", "", '{"key": "' + "k" * 70_000 + '"}',
                               '{"source": "line", "gap": true}', good]) + "\n", encoding="utf-8")
    observations, problems = plant.read_log(path)
    assert len(observations) == 3 and _codes(problems) == ["W_PLANT_DROPPED"]
    assert "3 lines" in problems[0].message
    assert _codes(plant.read_log(tmp_path / "missing.jsonl")[1]) == ["E_PLANT_LOG"]
    monkeypatch.setitem(plant.LIMITS, "lines", 2)
    assert plant.read_log(path)[0] == []
    assert _codes(plant.read_log(path)[1]) == ["E_PLANT_LOG"]
    monkeypatch.setitem(plant.LIMITS, "log_bytes", 10)
    assert _codes(plant.read_log(path)[1]) == ["E_PLANT_LOG"]


# ---------------------------------------------------------------- live (D259)
class FakeFeed:
    """`Feed.changes` over a list, a chunk a pull: no thread, no clock, no socket."""

    def __init__(self, log, chunk, holes=None):
        self.log, self.chunk, self.holes = log, chunk, holes or {}

    def changes(self, source, cursor=None, limit=1000):
        start = int(cursor or 0)
        mine = [line for line in self.log if line["source"] == source]
        events = tuple(live.Event(**line) for line in mine[start:start + self.chunk])
        lost, gap = self.holes.get(start, (0, False))
        return live.Changes(events, str(start + len(events)), lost, gap, "live", 0.0, ())


def _plant_log(model, cut=600):
    simulated = _recorded(model, cut=cut)
    mapping = plant.scaffold(model)
    mapping["time"] = {"epoch": EPOCH, "unit": "seconds"}
    return simulated, mapping, plant.log_of(simulated, mapping)


def test_a_live_feed_in_chunks_is_the_recording_at_every_pull():
    model = api.load_layout(MODELS / "machine_with_breakdowns.json")
    simulated, mapping, log = _plant_log(model)
    watch = plant.Watch(mapping, model, FakeFeed(log, 37, holes={74: (2, True)}), sources=["plant"])
    showing, held, pulls = Timeline([], live=True), [], 0
    while events := watch.step():
        showing.append(events)
        held += events
        pulls += 1
        assert showing.state_at(watch.now) == Timeline(held).state_at(watch.now)
    assert pulls == -(-len(log) // 37) and watch.step() == []
    assert held == plant.replay(mapping, model, log)[0]
    _compare(model, simulated, held, instants=[simulated[-1].t])
    assert watch.mapper.report()["holes"] == [(held[73].t, 2, True)]


def test_a_watch_reads_every_live_source_of_the_model_and_orders_one_pull_by_time():
    model = _line(levels=[{"name": "A", "initial": 0.0}, {"name": "B", "initial": 0.0}],
                  data_sources=[{"name": "north", "kind": "mqtt", "connection": "broker"},
                                {"name": "south", "kind": "mqtt", "connection": "broker"},
                                {"name": "sheet", "kind": "csv", "location": "x.csv"}])
    mapping = _mapping({"source": "north", "key": "a", "as": "level", "level": "A"},
                       {"source": "south", "key": "b", "as": "level", "level": "B"})
    log = [ob("a", 1, 10.0, "north"), ob("a", 2, 30.0, "north"), ob("b", 5, 20.0, "south"),
           ob("a", 9, 40.0, "south")]                   # the last: not this signal's source
    watch = plant.Watch(mapping, model, FakeFeed(log, 10))
    assert [(e.resource, e.t) for e in watch.step()] == [("A", 10.0), ("B", 20.0), ("A", 30.0)]
    assert watch.now == 30.0 and watch.mapper.report()["counts"] == {"unread": 1}


# ---------------------------------------------------------------- a state from now (D258)
def _slow_line(**sections):
    """Arrivals every 2 from t = 2, a service of 3: the queue grows. Services start at 2, 5,
    8, 11 ...; at t = 10 the third job is in service and the fourth and fifth wait."""
    c, p, d = create("c", dist_params={"value": 2.0}), process("p", value=3.0), dispose("d")
    sections.setdefault("simulation", {"time_limit": 20, "warmup": 0, "replications": 1,
                                       "time_unit": "seconds"})
    sections.setdefault("resources", SERVER)
    return layout([c, p, d], chain(c, p, d), **sections)


def _seen_until(model, until, **scaffold):
    """The mapper and the events of the plant log of a run of *model*, cut at *until*."""
    mapping = plant.scaffold(model, **scaffold)
    mapping["time"] = {"epoch": EPOCH, "unit": "seconds"}
    log = [line for line in plant.log_of(_recorded(model), mapping) if line["source_time"] <= until]
    events, mapper = plant.replay(mapping, model, log)
    return mapper, events


def test_a_run_starts_from_what_the_plant_looks_like_now():
    model = _slow_line()
    mapper, events = _seen_until(model, 10.5)
    doc, problems = state.from_plant(model, mapper.snapshot(), name="line")
    assert doc["taken"] == {"source": "observed", "at": 10.0, "stamp": "1970-01-01T00:00:10+00:00"}
    assert doc["state"] == {
        "clock": 10.0,
        "entities": [{"block": "p", "count": 1, "e_type": "DEFAULT", "in_service": 1},
                     {"block": "p", "count": 1, "e_type": "DEFAULT", "since": 8.0},
                     {"block": "p", "count": 1, "e_type": "DEFAULT", "since": 10.0}],
        "resources": {"Server": {"busy": 1}}}
    # the plant says nothing of the arrival clock: coded, and it starts as declared
    assert _codes(problems) == ["W_STATE_NOT_COVERED"] and "arrivals" in problems[0].message
    # what the mapper holds is what it told the one reducer (rule 13)
    shown = Timeline(events).state_at(mapper.now)
    back = {i: text for text, i in mapper.ids.items()}
    assert [e["block"] for e in mapper.snapshot()["entities"]] \
        == [shown["entities"][i][0] for i in sorted(shown["entities"])]
    assert len(back) == len(shown["entities"]) == 3 and shown["resources"]["Server"]["busy"] == 1
    # log -> timeline -> state -> run
    started, refused = state.start(model, doc)
    assert refused == []
    system = api.run_model(started)
    assert metric_value(system, "Started From State At") == 10.0
    # The job in service draws a fresh service (the plant does not say when it ends): the
    # three leave at 13, 16 and 19.
    assert metric_value(system, "Total Entities Completed") == 3


def test_units_busy_with_nobody_named_are_the_first_at_the_one_block_that_seizes_them():
    model = _slow_line()
    mapper, _events = _seen_until(model, 10.5, anonymous=True)
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert doc["state"]["entities"] == [{"block": "p", "count": 1, "in_service": 1},
                                        {"block": "p", "count": 1}, {"block": "p", "count": 1}]
    assert _codes(problems) == ["W_PLANT_SERVICE_INFERRED", "W_STATE_NOT_COVERED"]
    started, refused = state.start(model, doc)
    assert refused == [] and metric_value(api.run_model(started), "Total Entities Completed") == 3


def test_a_state_the_model_cannot_hold_is_refused_by_the_state_checks_not_repaired():
    model = _slow_line()
    mapping = _mapping({"key": "press/busy", "as": "busy", "resource": "Server"},
                       {"key": "wo/{id}", "as": "at", "blocks": {"IN": "c", "OP": "p"}})
    busy = plant.Mapper(mapping, model)
    busy.feed([ob("press/busy", 1, 5.0)])                   # busy, and nobody is there
    doc, _problems = state.from_plant(model, busy.snapshot())
    assert _codes(state.start(model, doc)[1]) == ["E_INITIAL_BUSY"]
    nowhere = plant.Mapper(mapping, model)
    nowhere.feed([ob("wo/7", "IN", 5.0)])                   # an entity standing in a CREATE
    doc, _problems = state.from_plant(model, nowhere.snapshot())
    assert _codes(state.start(model, doc)[1]) == ["E_INITIAL_BLOCK_TYPE"]
    assert state.check(model, doc, max_age=60.0, now=5.0 + 61.0)[0].code == "E_STATE_STALE"
    assert state.check(model, doc, max_age=60.0, now=5.0 + 59.0) == []


def test_a_down_resource_is_carried_by_its_one_failure_or_said_not_to_be():
    down = {"key": "press/down", "as": "state", "resource": "Server",
            "states": {"true": "down", "false": "up"}}
    failure = {"name": "breakdown", "resources": ["Server"],
               "uptime": {"distribution": "Constant", "dist_params": {"value": 100.0}},
               "downtime": {"distribution": "Constant", "dist_params": {"value": 4.0}}}
    for failures, carried in (([failure], True), ([], False)):
        model = _slow_line(failures=failures)
        mapper = plant.Mapper(_mapping(down), model)
        mapper.feed([ob("press/down", True, 3.0)])
        doc, problems = state.from_plant(model, mapper.snapshot())
        resources = doc["state"].get("resources", {})
        assert (resources.get("Server", {}).get("failures")
                == {"breakdown": {"down_until": None}}) == carried
        assert any("'Server' is down" in p.message for p in problems) != carried
        assert state.start(model, doc)[1] == []


def test_a_state_from_a_plant_names_the_blocks_inside_an_asset_and_what_it_does_not_cover():
    model = api.load_layout(MODELS / "two_cell_line.json")
    mapper, events = _seen_until(model, 30.0)
    doc, _problems = state.from_plant(model, mapper.snapshot())
    blocks = {entry["block"] for entry in doc["state"]["entities"]}
    assert blocks and all("/" in block for block in blocks)         # the flattened names
    assert {block.split("/")[0] for block in blocks} \
        == set(Timeline(events).state_at(mapper.now)["counts"])     # the instances a view shows
    assert state.start(model, doc)[1] == []
    fleets = api.load_layout(MODELS / "warehouse_forklifts.json")
    _doc, problems = state.from_plant(fleets, {"at": 5.0})
    assert any("fleets and vehicles" in p.message for p in problems)
    assert {p.code for p in problems} == {"W_STATE_NOT_COVERED"}


# ---------------------------------------------------------------- commands and the tool (D259)
def _on_disk(tmp_path, model):
    (tmp_path / "line.json").write_text(json.dumps(model), encoding="utf-8")
    return [str(tmp_path / name) for name in ("line.json", "line.plantmap.json", "plant.jsonl")]


def test_naga_plant_from_a_starter_mapping_to_a_player_a_trace_and_a_state(tmp_path, capsys):
    model, mapping, log = _on_disk(tmp_path, _slow_line())
    assert plant.main(["init", model, "--out", mapping]) == 0
    assert plant.main(["check", mapping, model]) == 0
    assert plant.main(["mock", model, "--map", mapping, "--out", log, "--seed", "1"]) == 0
    lines = Path(log).read_text(encoding="utf-8").splitlines()
    Path(log).write_text("\n".join(line for line in lines
                                   if json.loads(line)["source_time"] <= 10.5), encoding="utf-8")
    page, trace, taken = (str(tmp_path / name) for name in ("p.html", "t.jsonl", "s.json"))
    capsys.readouterr()
    assert plant.main(["replay", mapping, model, log, "--player", page, "--trace", trace,
                       "--state", taken]) == 0
    said = capsys.readouterr().out
    assert "t = 10" in said and "W_STATE_NOT_COVERED" in said
    assert not any(line.startswith("E_") for line in said.splitlines())
    events = [json.loads(line) for line in Path(trace).read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events[:3]] == ["resource_state", "entity_created", "entity_moved"]
    assert Timeline([TraceEvent(**{**e, "path": tuple(e["path"])}) for e in events]).state_at(
        10.0)["counts"] == {"p": 3}
    assert "<html" in Path(page).read_text(encoding="utf-8").lower()
    assert state.main(["check", model, taken]) == 0
    assert plant.main(["init", model, "--anonymous"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["signals"][-1]["as"] == "queue"


def test_naga_plant_check_says_why_a_mapping_may_not_be_used(tmp_path, capsys):
    model, mapping, log = _on_disk(tmp_path, _slow_line())
    Path(mapping).write_text(json.dumps(_mapping(
        {"key": "x", "as": "capacity", "resource": "Ghost"})), encoding="utf-8")
    assert plant.main(["check", mapping, model]) == 1
    assert "E_PLANTMAP_NAME" in capsys.readouterr().out
    Path(log).write_text("", encoding="utf-8")
    assert plant.main(["replay", mapping, model, log]) == 1
    assert plant.main(["replay", str(tmp_path / "none.json"), model, log]) == 1
    Path(mapping).write_text(json.dumps(_mapping()), encoding="utf-8")
    assert plant.main(["replay", mapping, model, str(tmp_path / "none.jsonl")]) == 1
    assert "E_PLANT_LOG" in capsys.readouterr().out


def test_nothing_from_a_hostile_log_is_text_in_a_player_file_or_a_trace(tmp_path, capsys):
    model, mapping, log = _on_disk(tmp_path, _slow_line())
    bad = "<script>alert(1)</script>"
    Path(mapping).write_text(json.dumps(_mapping(
        {"key": "wo/{id}", "field": "at", "as": "at", "blocks": {"OP": "p"}, "type_field": "kind",
         "types": {"A": "DEFAULT"}},
        {"key": "press/job/*", "as": "job", "resource": "Server"},
        {"key": "gate", "as": "seen", "block": "p"})), encoding="utf-8")
    plant.write_log(log, [
        ob(f"wo/{bad}", {"at": "OP", "kind": bad}, 1.0), ob("wo/2", {"at": bad, "kind": "A"}, 2.0),
        ob(f"press/job/{bad}", bad, 3.0), ob("gate", bad, 4.0), ob(bad, bad, 5.0),
        ob("wo/3", {"at": "OP", bad: bad}, 6.0, source=bad)])
    page, trace = str(tmp_path / "p.html"), str(tmp_path / "t.jsonl")
    assert plant.main(["replay", mapping, model, log, "--player", page, "--trace", trace]) == 0
    written = Path(page).read_text(encoding="utf-8") + Path(trace).read_text(encoding="utf-8") \
        + capsys.readouterr().out
    assert "alert(1)" not in written and "entity_created" in written


def test_naga_plant_watch_shows_a_live_feed_as_an_observed_timeline(tmp_path, capsys, monkeypatch):
    model = _slow_line(data_sources=[{"name": "plant", "kind": "mqtt", "connection": "broker",
                                      "location": "line/#"}])
    model_path, mapping, _log = _on_disk(tmp_path, model)
    connections = tmp_path / "connections.json"
    connections.write_text(json.dumps({"format": 1, "connections": {}}), encoding="utf-8")
    scaffold = plant.scaffold(model)
    Path(mapping).write_text(json.dumps(scaffold), encoding="utf-8")
    log = [line for line in plant.log_of(_recorded(model), scaffold) if line["source_time"] <= 10.5]

    class Feed(FakeFeed):
        def __init__(self, specs, network=None):
            assert [s["name"] for s in specs] == ["plant"] and network.connections == {}
            super().__init__(log, 6)
            self.pulls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def wait(self, _timeout):
            self.pulls += 1
            if self.pulls * self.chunk >= len(self.log):
                raise KeyboardInterrupt        # the person stops it: no clock in this test
            return True

    monkeypatch.setattr(plant, "Feed", Feed)
    taken = str(tmp_path / "now.json")
    assert plant.main(["watch", mapping, model_path, "--connections", str(connections),
                       "--for", "0", "--state", taken]) == 0
    said = capsys.readouterr().out
    assert "t = 10" in said and "p 3" in said and "Server busy" in said
    doc, problems = state.read(taken)
    assert problems == [] and doc["state"]["clock"] == 10.0 and len(doc["state"]["entities"]) == 3
    assert state.start(model_path, doc)[1] == []


def test_the_agent_tool_checks_a_mapping_and_replays_a_log_under_the_workspace_root(tmp_path):
    from nagasim import agent_tools as T

    model, mapping, log = _on_disk(tmp_path, _slow_line())
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="line.json")
    plant.main(["init", model, "--out", mapping])
    plant.main(["mock", model, "--map", mapping, "--out", log])
    checked = T.call("get_plant_timeline", model="line", mapping="line.plantmap.json")
    assert checked["problems"] == [] and checked["signals"] == 5 and "events" not in checked
    replayed = T.call("get_plant_timeline", model="line", mapping="line.plantmap.json",
                      log="plant.jsonl")
    assert replayed["events"] == replayed["observations"] > 20 and not replayed["truncated"]
    assert replayed["blocks"] and replayed["resources"][0]["name"] == "Server"
    assert T.TOOLS["get_plant_timeline"].read_only
    assert set(T.TOOLS["get_plant_timeline"].input_schema["properties"]) == {"model", "mapping", "log"}
    outside = T.call("get_plant_timeline", model="line", mapping="../elsewhere.json")
    assert outside["error"]["code"] == "E_PATH_OUTSIDE_ROOT"
    Path(mapping).write_text(json.dumps({"format": 1, "time": {}}), encoding="utf-8")
    refused = T.call("get_plant_timeline", model="line", mapping="line.plantmap.json",
                     log="plant.jsonl")
    assert refused["problems"][0]["code"] == "E_PLANTMAP_FORMAT" and "events" not in refused


# ---------------------------------------------------------------- the review round
EVERY = _mapping(
    {"key": "press/state", "as": "state", "resource": "Server",
     "states": {"IDLE": "idle", "RUN": "busy", "FAULT": "down", "OK": "up"}},
    {"key": "press/units", "as": "capacity", "resource": "Server"},
    {"key": "saw/job/*", "as": "job", "resource": "Saw"},
    {"key": "saw/full", "as": "blocked", "resource": "Saw"},
    {"key": "lathe/busy", "as": "busy", "resource": "Lathe"},
    {"key": "c/len", "as": "queue", "block": "c"},
    {"key": "wo/{id}", "as": "at", "blocks": {"OP": "p"}, "gone": ["DONE"], "merged": ["KIT"]},
    {"key": "gate", "as": "seen", "block": "p"},
    {"key": "exit", "as": "seen", "leaves": True},
    {"key": "tank", "as": "level", "level": "Tank"})


@pytest.mark.parametrize("key, value", [
    ("saw/job/1", None), ("saw/job/1", ""), ("press/state", "IDLE"), ("press/state", "OK"),
    ("press/units", 0), ("saw/full", 0), ("lathe/busy", 0), ("c/len", 0), ("wo/7", None),
    ("wo/7", "DONE"), ("wo/7", "KIT"), ("gate", None), ("gate", ""), ("exit", "T-1"),
    ("exit", None), ("tank", 0)])
def test_the_first_thing_a_key_says_may_be_its_empty_value(key, value):
    mapper = plant.Mapper(EVERY)
    first = mapper.feed([ob(key, value, 1.0)])
    again = mapper.feed([ob(key, value, 2.0)])              # and the same again, later
    assert all(e.kind in ("resource_state", "level_changed") for e in first + again)
    state = Timeline(first + again).state_at(2.0)
    assert state["entities"] == {} and all(r["busy"] == 0 for r in state["resources"].values())
    assert _reasons(mapper) == {} and mapper.snapshot()["entities"] == []


def test_a_long_watch_forgets_what_is_gone_and_never_stops_showing_new_items(monkeypatch):
    monkeypatch.setitem(plant.LIMITS, "keys", 10)
    monkeypatch.setitem(plant.LIMITS, "entities", 10)
    mapper, shown = plant.Mapper(EVERY), 0
    for n in range(40):                                     # four times the limits
        events = mapper.feed([ob(f"wo/{n}", "OP", float(n)), ob(f"wo/{n}", "DONE", n + 0.5)])
        shown += [e.kind for e in events] == ["entity_created", "entity_disposed"]
    assert shown == 40 and _reasons(mapper) == {} and len(mapper._said) == 0
    # items that never leave: the oldest KEY is forgotten for a new one, counted; the items stay
    for n in range(40, 60):
        mapper.feed([ob(f"wo/{n}", "OP", float(n))])
    assert len(mapper._said) == 10 and len(mapper.ids) == 10
    assert _reasons(mapper) == {"entities": 10, "forgotten": 10}
    assert "W_PLANT_DROPPED" in _codes(mapper.problems())
    # parts taken into a batch and never heard of again make room, oldest first
    merged = plant.Mapper(EVERY)
    for n in range(30):
        merged.feed([ob(f"wo/{n}", "OP", float(n)), ob(f"wo/{n}", "KIT", n + 0.5)])
    assert len(merged.ids) == 10 and "entities" not in _reasons(merged)
    # a job per position: over the bound a new one is refused, and freed ones make room
    jobs = plant.Mapper(EVERY)
    jobs.feed([ob(f"saw/job/{n}", "x", 1.0) for n in range(15)])
    assert len(jobs._jobs) == 10 and _reasons(jobs)["keys"] == 5
    jobs.feed([ob(f"saw/job/{n}", None, 2.0) for n in range(10)])
    assert jobs._jobs == {} and jobs.snapshot()["resources"]["Saw"]["busy"] == 0


def test_a_job_for_an_entity_in_a_block_that_does_not_seize_the_resource_is_a_contradiction():
    c, p, q, d = create("c"), process("p"), process("q", resource="Saw"), dispose("d")
    model = layout([c, p, q, d], chain(c, p, q, d), resources=SERVER + [{"name": "Saw", "capacity": 1}],
                   simulation={"time_limit": 20, "time_unit": "seconds"})
    mapping = _mapping({"key": "press/job", "as": "job", "resource": "Server"},
                       {"key": "wo/{id}", "as": "at", "blocks": {"P": "p", "Q": "q"}})
    mapper = plant.Mapper(mapping, model)
    mapper.feed([ob("wo/1", "Q", 1.0), ob("wo/2", "P", 2.0), ob("press/job", "1", 3.0)])
    assert _reasons(mapper) == {"job_elsewhere": 1}
    assert _codes(mapper.problems()) == ["W_PLANT_CONTRADICTION"]
    # the state does not put ANOTHER entity in service: the start is refused, as the plant said
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert "W_PLANT_SERVICE_INFERRED" not in _codes(problems)
    assert all("in_service" not in entry for entry in doc["state"]["entities"])
    assert _codes(state.start(model, doc)[1]) == ["E_INITIAL_BUSY"]
    # a job of an entity seen nowhere infers nobody either
    unseen = plant.Mapper(mapping, model)
    unseen.feed([ob("wo/2", "P", 2.0), ob("press/job", "9", 3.0)])
    doc, problems = state.from_plant(model, unseen.snapshot())
    assert "W_PLANT_SERVICE_INFERRED" not in _codes(problems)
    assert _codes(state.start(model, doc)[1]) == ["E_INITIAL_BUSY"]
    # with no model the mapper cannot know which block seizes what, and says nothing
    blind = plant.Mapper(mapping)
    blind.feed([ob("wo/1", "Q", 1.0), ob("press/job", "1", 3.0)])
    assert _reasons(blind) == {}


@pytest.mark.parametrize("epoch, accepted", [
    ("1970-01-01T00:00:00+00:00", True), ("2199-12-31T00:00:00+00:00", True),
    ("1969-12-31T23:59:59+00:00", False), ("1600-01-01T00:00:00+00:00", False),
    ("2200-01-02T00:00:00+00:00", False), ("9999-12-31T00:00:00+00:00", False)])
def test_the_epoch_is_bounded_and_a_stamp_never_raises(epoch, accepted):
    mapping = _mapping({"key": "tank", "as": "level", "level": "Tank"}, epoch=epoch, unit="years")
    assert (plant.check(mapping) == []) == accepted
    if not accepted:
        assert _codes(plant.check(mapping)) == ["E_PLANTMAP_FORMAT"]
        return
    mapper = plant.Mapper(mapping)
    base = datetime.fromisoformat(epoch).timestamp()
    mapper.feed([ob("tank", 1, base + 99 * 365 * 86400.0)])     # the far end of model time
    assert round(mapper.now, 6) == 99.0 and isinstance(mapper.stamp(), str)
    for absurd in (float("nan"), float("inf"), -float("inf"), 1e300, -1e300):
        mapper.now = absurd
        assert mapper.stamp() is None and mapper.snapshot()["stamp"] is None


@pytest.mark.parametrize("twice", [
    {"key": "q2", "as": "queue", "block": "c"},
    {"key": "tank2", "as": "level", "level": "Tank"},
    {"key": "units2", "as": "capacity", "resource": "Server"},
    {"key": "busy2", "as": "busy", "resource": "Lathe"},
    {"key": "full2", "as": "blocked", "resource": "Saw"},
    {"key": "state2", "as": "state", "resource": "Server", "states": {"X": "down"}}])
def test_one_thing_told_by_two_signals_of_one_kind_is_refused(twice):
    assert plant.check(EVERY) == []
    doubled = {**EVERY, "signals": EVERY["signals"] + [twice]}
    assert _codes(plant.check(doubled)) == ["E_PLANTMAP_DOUBLE"]
    # several jobs, and several ways to see an item, are what a plant has
    more = [{"key": "saw/other", "as": "job", "resource": "Saw"},
            {"key": "gate2", "as": "seen", "block": "p"},
            {"key": "pallet/{id}", "as": "at", "blocks": {"OP": "p"}}]
    assert plant.check({**EVERY, "signals": EVERY["signals"] + more}) == []


def test_what_the_plant_says_against_itself_is_shown_as_said_and_reported():
    model = _line(levels=[{"name": "Tank", "initial": 0.0, "capacity": 10, "min": 0}],
                  resources=SERVER + [{"name": "Lathe", "capacity": 2}, {"name": "Saw"}])
    mapper = plant.Mapper(EVERY, model)
    events, shown = _state(mapper, [
        ob("press/units", 1, 1.0), ob("lathe/busy", 3, 1.0), ob("tank", 12, 1.0),
        ob("tank", -1, 2.0), ob("tank", 5, 3.0)])
    assert _reasons(mapper) == {"level_range": 2}           # Lathe: the plant said no capacity
    assert shown["resources"]["Lathe"]["busy"] == 3 and Timeline(events).level_at("Tank", 1.0) == 12
    mapper = plant.Mapper(_mapping(
        {"key": "units", "as": "capacity", "resource": "Server"},
        {"key": "busy", "as": "busy", "resource": "Server"},
        {"key": "job/*", "as": "job", "resource": "Saw"},
        {"key": "saw/units", "as": "capacity", "resource": "Saw"}))
    _events, shown = _state(mapper, [
        ob("units", 2, 1.0), ob("busy", 3, 2.0),            # more busy than it has
        ob("busy", 1, 3.0), ob("units", 0, 4.0),            # a shift ends on a job: no report
        ob("saw/units", 1, 5.0), ob("job/1", "a", 6.0), ob("job/2", "b", 7.0)])
    assert shown["resources"]["Server"] == {"state": "off", "busy": 1, "capacity": 0.0}
    assert shown["resources"]["Saw"]["busy"] == 2
    assert _reasons(mapper) == {"over_capacity": 2, "job_unseen": 2}
    assert "W_PLANT_CONTRADICTION" in _codes(mapper.problems())
    # a capacity is a count of units: over the bound it is not one
    assert mapper.feed([ob("units", 1e300, 8.0), ob("units", plant.LIMITS["count"] + 1, 8.0)]) == []
    assert _reasons(mapper)["value"] == 2


def test_the_event_ceiling_is_met_inside_one_observation():
    mapper = plant.Mapper(EVERY, max_events=7)
    events = mapper.feed([ob("c/len", 50, 1.0), ob("lathe/busy", 50, 2.0)])
    assert len(events) == 7 and mapper.truncated and _reasons(mapper)["ceiling"] == 2
    assert len(Timeline(events).state_at(1.0)["entities"]) == 7 == len(mapper.snapshot()["entities"])
    units = plant.Mapper(EVERY, max_events=4)
    assert len(units.feed([ob("lathe/busy", 9, 1.0)])) == 4 and units.truncated
    assert units.snapshot()["resources"]["Lathe"]["busy"] == 4


def test_the_lost_count_is_exact_however_many_holes_there_are():
    mapper = plant.Mapper(EVERY)
    mapper.feed([ob("tank", 1, 5.0)])
    for _ in range(1500):
        mapper.hole(2, True)
    assert len(mapper.report()["holes"]) == 1000
    assert "1500 holes (3000 events lost, 1500 breaks), the first at t = 5" \
        in mapper.problems()[0].message


def test_a_stamp_written_as_text_is_read_as_the_feed_reads_one():
    mapping = _mapping({"key": "tank", "as": "level", "level": "Tank"},
                       epoch="2026-10-05T06:00:00+02:00", unit="minutes")
    mapper = plant.Mapper(mapping)
    line = {"source": "line", "key": "tank", "value": 1, "source_time": "2026-10-05T06:10:00+02:00",
            "received": "2026-10-05T04:10:05Z"}
    events = mapper.feed([line, {**line, "value": 2, "source_time": "soon",
                                 "received": "2026-10-05T04:20:00+00:00"},
                          {**line, "value": 3, "source_time": "9" * 500, "received": None}])
    assert [e.t for e in events] == [10.0, 20.0]
    assert _reasons(mapper) == {"no_stamp": 2, "no_time": 1}


def test_an_item_nobody_typed_is_unknown_whether_it_has_a_name_or_not():
    mapper = plant.Mapper(EVERY)
    events = mapper.feed([ob("c/len", 1, 1.0), ob("wo/7", "OP", 1.0)])
    assert [e.e_type for e in events] == [plant.UNTYPED, plant.UNTYPED]
    assert [e["e_type"] for e in mapper.snapshot()["entities"]] == [None, None]
