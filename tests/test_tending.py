"""Mobile cobots: machine tending (automation sub-project 3, D168-D170)."""
import copy

import pytest

from nagasim.core import tending
from nagasim.core.document import ModelDocument

from conftest import create, dispose, layout, problem_codes, process, run_env

# A line In - Lathe - Out, 10 m apart; the model runs in seconds.
NET = {"nodes": [{"name": "In", "x": 0, "y": 0}, {"name": "Lathe", "x": 10, "y": 0},
                 {"name": "Out", "x": 20, "y": 0}],
       "segments": [{"from": "In", "to": "Lathe"}, {"from": "Lathe", "to": "Out"}]}
ARM = {"name": "UR10", "speed": 1.0, "accel": 2.0, "collaborative_speed": 0.25}
PLACE = [{"kind": "move", "distance_m": 0.8}, {"kind": "place", "time": 2.0}]     # 1.3 + 2.0 s
PICK = [{"kind": "pick", "time": 1.5}]
SIM = {"time_limit": 1000, "warmup": 0, "replications": 1, "time_unit": "seconds"}


def cobot(count=1, **fields):
    """An arm fleet at In, driving 1 m/s (10 s per leg), with no flat handling time."""
    return {"name": "Cobot", "count": count, "speed": 1.0, "home": "In", "arm": "UR10", **fields}


def station(name, node, **tend):
    params = {"node": node}
    if tend:
        params["tending"] = tend
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": params}


def transport(name, destination, fleet="Cobot"):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [2, 0],
            "params": {"fleet": fleet, "destination": destination, "priority": 0}}


def model(fleet=None, *, jobs=1, every=100.0, service=20.0, tend=None, sim=SIM, before=(),
          **sections):
    """*jobs* parts, one every *every* s: In -> (cobot) -> Lathe, machined for
    *service* s -> (cobot) -> Out. *before* are blocks put between the Lathe
    station and its machine."""
    tend = {"place": PLACE, "pick": PICK} if tend is None else tend
    inner = [station("Lathe", "Lathe", **tend), *before, process("machine", "Chuck", service)]
    blocks = [create(max_arrivals=jobs, dist_params={"value": every}), station("S_In", "In"),
              transport("to_lathe", "Lathe"), *inner, transport("to_out", "S_Out"),
              station("S_Out", "Out"), dispose()]
    links = [{"source": a, "target": b} for a, b in (
        ("create_1", "S_In"), ("S_In", "to_lathe"), ("to_lathe", "dispose_1"),
        *zip([b["name"] for b in inner], [b["name"] for b in inner[1:]] + ["to_out"]),
        ("to_out", "dispose_1"), ("S_Out", "dispose_1"))]
    return layout(blocks, links, network=NET, fleets=[fleet or cobot()],
                  motion_profiles=[dict(ARM)], resources=[{"name": "Chuck", "capacity": 1}],
                  simulation=dict(sim), **sections)


def codes(doc):
    return {c for c in problem_codes(doc, severity=None)
            if "TENDING" in c or "FLEET_ARM" in c or c == "E_EXPRESSION"}


# -- Task 1: format, validation, cycle timing -------------------------------------

def test_a_valid_tending_model_has_no_problems():
    doc = model(cobot(count=2, exchange=True),
                tend={"place": PLACE, "pick": PICK, "time_unit": "seconds", "hrc": "separation",
                      "zone": {"node": "Lathe", "radius_m": 2.0}, "cobot": "stay"})
    assert problem_codes(doc) == set() and codes(doc) == set()


def _fleet(doc):
    return doc["fleets"][0]


def _tend(doc):
    return doc["blocks"][3]["params"]["tending"]


@pytest.mark.parametrize("change, code", [
    (lambda d: _fleet(d).update(arm="Nobody"), "E_FLEET_ARM"),
    (lambda d: _fleet(d).update(kind="worker"), "E_FLEET_ARM"),
    (lambda d: _fleet(d).update(motion="gantry", speed_x=1.0, speed_y=1.0), "E_FLEET_ARM"),
    (lambda d: _fleet(d).update(loads=2), "E_FLEET_ARM"),
    (lambda d: (_fleet(d).pop("arm"), _fleet(d).update(exchange=True)), "E_FLEET_ARM"),
    (lambda d: _tend(d).update(place=[{"time": 2.0}]), "E_TENDING"),
    # a segment's time expression is checked where MOTION's are
    (lambda d: _tend(d).update(pick=[{"kind": "pick", "time": "TRIA(1,"}]), "E_EXPRESSION"),
    (lambda d: (d["motion_profiles"][0].pop("collaborative_speed"),
                _tend(d).update(hrc="collaborative")), "E_TENDING"),
    (lambda d: _tend(d).update(hrc="separation", zone={"node": "nowhere", "radius_m": 1}),
     "E_TENDING"),
    (lambda d: _tend(d).update(cobot="wait"), "E_TENDING"),
    (lambda d: _tend(d).update(time_unit="fortnights"), "E_TENDING"),
    (lambda d: d["blocks"][3]["params"].update(node=""), "E_TENDING"),
    # stay, but the only way out of the station is the DISPOSE
    (lambda d: (_tend(d).update(cobot="stay"),
                d["links"].__setitem__(4, {"source": "machine", "target": "dispose_1"})),
     "W_TENDING_STAY"),
    (lambda d: _fleet(d).pop("arm"), "W_TENDING_UNUSED"),
])
def test_each_tending_problem_has_its_code(change, code):
    doc = model(cobot(count=2))
    assert codes(doc) == set()
    change(doc)
    assert code in codes(doc)


def test_one_cobot_feeding_and_emptying_a_machine_with_no_space_is_a_deadlock_warning():
    assert codes(model()) == {"W_TENDING_DEADLOCK"}
    buffer = {"name": "rack", "description": "rack", "type": "BUFFER", "position": [1, 0],
              "params": {"capacity": 1}}
    for safe in (model(cobot(count=2)), model(cobot(exchange=True)), model(before=[buffer]),
                 model(tend={"place": PLACE, "pick": PICK, "cobot": "stay"})):
        assert codes(safe) == set()


def test_a_model_without_arm_or_tending_round_trips_unchanged():
    doc = model(tend={})
    _fleet(doc).pop("arm")
    assert problem_codes(doc) == set() and codes(doc) == set()
    saved = ModelDocument(copy.deepcopy(doc)).to_layout()
    assert "arm" not in saved["fleets"][0] and "exchange" not in saved["fleets"][0]
    assert saved["blocks"][3]["params"] == {"node": "Lathe"}


@pytest.mark.parametrize("unit, per_second", [("seconds", 1.0), ("minutes", 1 / 60)])
def test_cycle_seconds_is_the_motion_timing_in_the_model_unit(unit, per_second):
    env, built = run_env(model(sim={**SIM, "time_unit": unit}), 0)
    block, fleet = built.blocks["to_lathe"], env.ctx.fleets["Cobot"]
    lathe = env.ctx.stations["Lathe"]
    assert tending.applies(fleet, lathe) and not tending.applies(fleet, env.ctx.stations["S_In"])
    # move 0.8 m at 1 m/s, 2 m/s^2: 0.8/1.0 + 1.0/2.0 = 1.3 s; place 2.0 s
    assert tending.cycle_seconds(block, None, fleet, lathe, "place") == pytest.approx(
        3.3 * per_second)
    assert tending.cycle_seconds(block, None, fleet, lathe, "pick") == pytest.approx(
        1.5 * per_second)
    lathe.tending["hrc"] = "collaborative"      # 0.8/0.25 + 0.25/2.0 = 3.325 s
    assert tending.cycle_seconds(block, None, fleet, lathe, "place") == pytest.approx(
        5.325 * per_second)
    del lathe.tending["pick"]                   # no cycle: the caller uses the flat time
    assert tending.cycle_seconds(block, None, fleet, lathe, "pick") is None


# -- Task 2: the run ---------------------------------------------------------------
#
# Timeline of one part (seconds): the cobot drives 10 s In -> Lathe, places for
# 3.3 s, the machine works 20 s, the pick takes 1.5 s, the drive to Out 10 s.

from nagasim.core.trace import TraceRecorder                       # noqa: E402

TWO = dict(jobs=2, every=5.0)       # the second part reaches the lathe at t=15, on its own cobot


def run(doc, until=1000, **options):
    recorder = TraceRecorder()
    env, built = run_env(doc, until, subscribers=[recorder], **options)
    return env, built, recorder.events


def at(events, kind, **match):
    return [e.t for e in events
            if e.kind == kind and all(getattr(e, k) == v for k, v in match.items())]


def stats(env):
    return env.ctx.tending[("Lathe", "Cobot")]


def test_the_machine_is_free_when_the_pick_ends_and_starts_a_placed_part_after_the_place():
    env, _built, events = run(model(cobot(count=2), **TWO))
    # Part 1 is placed 10 -> 13.3 and machined to 33.3; the pick ends at 34.8,
    # and only then does part 2, waiting on its cobot since 15, get the machine.
    assert at(events, "entity_seized", target="Chuck") == pytest.approx([10.0, 34.8])
    tends = [e for e in events if e.kind == "tend"]
    assert [(e.state, e.target, e.resource) for e in tends] == [
        (which, "Cobot", "Lathe") for which in ("place", "pick", "place", "pick")]
    assert [e.t for e in tends] == pytest.approx([10.0, 33.3, 34.8, 58.1])
    assert [e.duration for e in tends] == pytest.approx([3.3, 1.5, 3.3, 1.5])
    # Service starts one place cycle after the machine accepts: 13.3 + 20 + 1.5 + 10.
    assert at(events, "entity_disposed") == pytest.approx([44.8, 69.6])
    # Machine free: no loaded wait. Busy: the rest of the service and the pick.
    assert stats(env).loaded_wait.values == pytest.approx([0.0, 19.8])
    assert stats(env).blocked_for_pick.values == pytest.approx([0.0, 0.0])
    assert (stats(env).picks, stats(env).places, stats(env).exchanges) == (2, 2, 0)


def test_a_buffer_before_the_machine_accepts_the_part_and_frees_the_cobot():
    rack = {"name": "rack", "description": "rack", "type": "BUFFER", "position": [1, 0],
            "params": {"capacity": 1}}
    env, _built, events = run(model(before=[rack], **TWO))
    # The one cobot places part 2 into the rack at 33.3 -> 36.6 while the lathe
    # still holds part 1, then picks part 1 (36.6 -> 38.1): no deadlock.
    assert stats(env).loaded_wait.values == pytest.approx([0.0, 0.0])
    assert stats(env).blocked_for_pick.values == pytest.approx([3.3, 10.0])
    assert at(events, "entity_disposed") == pytest.approx([48.1, 79.6])


@pytest.mark.parametrize("mode, free_at_20, empty_m, disposed", [
    ("leave", 1, 40.0, 54.8),      # home at 23.3, called back: 10 + 10 m, then 20 m home
    ("stay", 0, 20.0, 44.8),       # waits at the lathe: only the 20 m home at the end
])
def test_a_staying_cobot_keeps_its_slot_and_picks_with_no_dispatch(mode, free_at_20, empty_m,
                                                                  disposed):
    doc = model(cobot(park="home"), tend={"place": PLACE, "pick": PICK, "cobot": mode})
    env, _built, _events = run(doc, 20)
    assert len(env.ctx.fleets["Cobot"]._free) == free_at_20
    env, _built, events = run(doc)
    assert at(events, "entity_disposed") == pytest.approx([disposed])
    assert env.ctx.fleets["Cobot"].movers[0].distance_empty == pytest.approx(empty_m)
    assert stats(env).stay.values == pytest.approx([20.0] if mode == "stay" else [])


def test_a_staying_cobot_is_freed_when_its_part_is_disposed_and_serves_again():
    doc = model(jobs=2, every=50.0, tend={"place": PLACE, "pick": PICK, "cobot": "stay"})
    doc["links"][4] = {"source": "machine", "target": "dispose_1"}
    env, _built, events = run(doc)
    # Part 2 (t=50): the freed cobot drives back to In (60), to the lathe (70).
    assert at(events, "entity_disposed") == pytest.approx([33.3, 93.3])
    assert stats(env).stay.values == pytest.approx([20.0, 20.0])
    assert len(env.ctx.fleets["Cobot"]._free) == 1


def _block(name, kind, **params):
    return {"name": name, "description": name, "type": kind, "position": [1, 0], "params": params}


@pytest.mark.parametrize("leaves, count, disposals", [
    ("balk", 1, 1),         # balks to the DISPOSE while the cobot is still placing it
    ("batch", 2, 1),        # absorbed into a batch, which leaves on its own
    ("duplicate", 1, 2),    # a copy never shares the cobot: one trip, not two
])
def test_a_staying_cobot_is_freed_when_its_part_leaves_the_model_another_way(leaves, count,
                                                                             disposals):
    stay = {"place": PLACE, "pick": PICK, "cobot": "stay"}
    doc = model(cobot(count=count), jobs=count, every=5.0, tend=stay)
    if leaves == "balk":
        doc["blocks"][4]["params"]["balk_if"] = "queue_length >= 0"
        doc["links"].append({"source": "machine", "target": "dispose_1"})
    else:
        block = (_block("batch_1", "BATCH", batch_size=2) if leaves == "batch"
                 else _block("copy", "DUPLICATE", number_of_duplicates=2))
        doc["blocks"] += [block, station("Lathe_out", "Lathe")]
        doc["links"] = [l for l in doc["links"] if l["target"] != "to_out"] + [
            {"source": "machine", "target": block["name"]},
            {"source": block["name"], "target": "Lathe_out"},
            {"source": "Lathe_out", "target": "to_out"}]
    env, _built, events = run(doc)
    assert len(at(events, "entity_disposed")) == disposals
    assert len(env.ctx.fleets["Cobot"]._free) == count


@pytest.mark.parametrize("service, picks, waits, blocked, disposed", [
    # The loaded cobot (at 33.3) waits; part 1, done at 43.3, hands itself over.
    (30.0, [43.3, 88.1], [0.0, 11.5], [0.0, 10.0], [58.1, 99.6]),
    # Part 1, done at 28.3, already queues for a cobot when the loaded one arrives.
    (15.0, [33.3, 63.1], [0.0, 1.5], [5.0, 10.0], [48.1, 74.6]),
])
def test_an_exchange_picks_then_places_and_carries_on_with_no_empty_trip(
        service, picks, waits, blocked, disposed):
    env, _built, events = run(model(cobot(exchange=True), service=service, **TWO))
    assert at(events, "tend", state="pick") == pytest.approx(picks)
    assert at(events, "tend", state="place") == pytest.approx([10.0, picks[0] + 1.5])
    assert stats(env).loaded_wait.values == pytest.approx(waits)
    assert stats(env).blocked_for_pick.values == pytest.approx(blocked)
    assert at(events, "entity_disposed") == pytest.approx(disposed)
    assert (stats(env).picks, stats(env).places, stats(env).exchanges) == (2, 2, 1)
    # Empty: back to In for part 2 (10 m), then Out -> Lathe for the last pick (10 m).
    assert env.ctx.fleets["Cobot"].movers[0].distance_empty == pytest.approx(20.0)


def test_one_cobot_without_exchange_deadlocks_and_the_run_says_so():
    with pytest.raises(RuntimeError, match="deadlock"):
        run(model(service=30.0, **TWO))


# The stall with ENDLESS arrivals (jobs=0), which never drains the event queue:
# it is reported at the instant it becomes certain. One part every 30 s, 20 s
# of service: part 1 is machined 13.3 -> 33.3; the cobot, idle at the lathe,
# leaves at 30 for part 2 and is back loaded at 50, and part 1 has waited for
# it since 33.3.
ENDLESS = dict(jobs=0, every=30.0)
RACK = {"name": "rack", "description": "rack", "type": "BUFFER", "position": [1, 0],
        "params": {"capacity": 1}}


def test_the_stall_of_one_cobot_is_reported_when_the_cobot_arrives_loaded():
    with pytest.raises(RuntimeError, match=r"deadlock at t=50: .*'Cobot'.*Lathe.*exchange"):
        run(model(**ENDLESS))


def test_the_stall_is_reported_when_the_part_finishes_behind_a_waiting_cobot():
    # Every 5 s, 30 s of service: the cobot waits loaded from 33.3, part 1 is done at 43.3.
    with pytest.raises(RuntimeError, match=r"deadlock at t=43\.3: "):
        run(model(jobs=0, every=5.0, service=30.0))


# One part every 47 s: the one cobot fetches part 3 while part 2 is machined
# (80.3 -> 100.3) and is back loaded at 114.
@pytest.mark.parametrize("fleet, tend, before", [
    (cobot(count=2), None, ()),               # the second cobot picks
    (cobot(exchange=True), None, ()),         # the loaded cobot picks first
    (cobot(), None, (RACK,)),                 # the rack takes the new part, the cobot picks
    (cobot(), {"place": PLACE, "pick": PICK, "cobot": "stay"}, ()),
])
def test_the_four_ways_out_of_the_stall_run_to_the_time_limit(fleet, tend, before):
    with pytest.raises(RuntimeError, match="deadlock at t=114: "):
        run(model(jobs=0, every=47.0))
    env, _built, events = run(model(fleet, jobs=0, every=47.0, tend=tend, before=before))
    assert env.now == 1000 and len(at(events, "entity_disposed")) >= 15


def test_a_stall_needs_every_cobot_waiting_loaded_at_a_machine_that_waits_for_the_fleet():
    # In -> Lathe (20 s) -> Mill at Out (40 s) -> out, two cobots, a part every
    # 30 s. From 124.4 one cobot waits at the mill with part 3; part 2 is done
    # there at 142.9 and waits for a cobot. The other brings part 5 to the lathe
    # at 152.9, the instant part 4 is done in it: nothing can move again.
    tend = {"place": PLACE, "pick": PICK}
    doc = model(cobot(count=2), **ENDLESS)
    doc["blocks"][5]["params"]["destination"] = "Mill"          # to_out
    doc["blocks"] += [station("Mill", "Out", **tend), process("mill", "Spindle", 40.0),
                      transport("mill_out", "S_Out")]
    doc["resources"].append({"name": "Spindle", "capacity": 1})
    doc["links"] += [{"source": "Mill", "target": "mill"}, {"source": "mill", "target": "mill_out"},
                     {"source": "mill_out", "target": "dispose_1"}]
    with pytest.raises(RuntimeError, match=r"deadlock at t=152\.9: .*Lathe, Mill"):
        run(doc)


def test_a_part_granted_a_cobot_as_a_loaded_one_arrives_is_picked_once():
    # All times are halves, so the tie is exact: part 2 queues at 28.5; at 33.5
    # one cobot is freed at Out (and granted to it) and the other arrives loaded.
    tend = {"place": [{"kind": "place", "time": 3.5}], "pick": PICK}
    env, _built, events = run(model(cobot(count=2, exchange=True), jobs=3, every=4.0,
                                    service=5.0, tend=tend))
    assert 33.5 in at(events, "entity_released", target="Cobot")
    assert stats(env).picks == 3 and len(at(events, "entity_disposed")) == 3


def test_a_capacity_schedule_never_takes_a_cobot_that_waits_loaded():
    shift = {"name": "shift", "kind": "capacity", "points": [[20, 2], [10, 1], [1000, 2]]}
    doc = model(cobot(count=2, capacity_schedule="shift"), schedules=[shift], **TWO)
    env, _built, _events = run(doc, 25)
    assert [m.unit for m in env.ctx.fleets["Cobot"]._down] == [0]   # idle; unit 1 holds a part
    env, _built, _events = run(doc)
    assert stats(env).loaded_wait.values == pytest.approx([0.0, 19.8])


def test_warmup_in_a_loaded_wait_counts_it_from_the_reset():
    env, _built, _events = run(model(cobot(count=2), **TWO), warmup=20.0)
    assert stats(env).loaded_wait.values == pytest.approx([14.8])       # 20 -> 34.8
    assert (stats(env).picks, stats(env).places) == (2, 1)


@pytest.mark.parametrize("fleet, tend", [
    (cobot(load_time="2", unload_time="3"), {}),                               # plain station
    ({"name": "Cobot", "speed": 1.0, "home": "In", "load_time": "2", "unload_time": "3"},
     None),                                                                     # plain fleet
])
def test_tending_needs_both_an_arm_and_a_tending_station(fleet, tend):
    env, _built, events = run(model(fleet, tend=tend))
    # load 0-2, drive, unload 12-15, service to 35, load to 37, drive, unload to 50
    assert at(events, "entity_released", target="Chuck") == pytest.approx([35.0])
    assert at(events, "entity_disposed") == pytest.approx([50.0])
    assert env.ctx.tending == {} and not at(events, "tend")
    # No blocking at all, not even zero-length: the plain fleet's code path.
    assert env.ctx.blocking_nodes == {} and not at(events, "resource_state", state="blocked")


# -- Task 3: the tending_summary frame ---------------------------------------------

from nagasim import api                                             # noqa: E402
from nagasim.core.outputs import _TENDING_SUMMARY_COLS             # noqa: E402
from nagasim.experiments import flatten_kpis                       # noqa: E402


def test_a_model_without_tending_gets_an_empty_frame_with_its_columns():
    frame = api.run_model(model(tend={}), replications=1)["tending_summary"]
    assert frame.empty and list(frame.columns) == _TENDING_SUMMARY_COLS == [
        "Run", "Station", "Fleet", "Picks", "Places", "Exchanges", "Avg Pick Time",
        "Avg Place Time", "Avg Loaded Wait", "Avg Blocked For Pick", "Avg Stay"]


def test_tending_summary_reports_the_exchange_run_and_its_kpi_keys():
    results = api.run_model(model(cobot(exchange=True), service=30.0, **TWO), replications=1)
    row = results["tending_summary"].iloc[0]
    assert (row["Station"], row["Fleet"]) == ("Lathe", "Cobot")
    assert (row["Picks"], row["Places"], row["Exchanges"]) == (2, 2, 1)
    assert row["Avg Pick Time"] == pytest.approx(1.5)
    assert row["Avg Place Time"] == pytest.approx(3.3)
    assert row["Avg Loaded Wait"] == pytest.approx(5.75)            # (0 + 11.5) / 2
    assert row["Avg Blocked For Pick"] == pytest.approx(5.0)        # (0 + 10) / 2
    assert row.isna()["Avg Stay"]                                   # nobody stayed
    kpis = flatten_kpis(results).set_index("KPI")["Value"]
    assert kpis["tending/Lathe/Cobot/Exchanges"] == 1
    assert kpis["tending/Lathe/Cobot/Avg Loaded Wait"] == pytest.approx(5.75)
