"""M6 "vehicle held across legs", written from the spec only
(docs/superpowers/specs/2026-10-26-phase15-limits-design.md, section "M6", 17 exact cases;
the numbering below is the spec's). No implementation was read; every expected number is
re-derived by hand in the docstring beside it.

Shared model: line A(0,0) - B(10,0) - C(20,0), two-way segments of length 10; fleet AGV: one
vehicle, speed 1 both ways, load 1, unload 1, home A, park "stay". Base flow F: one part at
t=0 -> SA -> T1 (dest SB, keep_vehicle) -> SB -> PROCESS "Delay" 5 -> T2 (dest SC) -> SC ->
DISPOSE; run length 40, model time in hours.

Assumptions where the spec is silent:
 A1  `keep_vehicle` is a TRANSPORT param; T2 leaves it unset (false).
 A2  "Utilization" is the resource_summary column "Usage % (snapshot)", a percentage (the spec writes 72.5%).
 A3  A mover_moved / entity_released / entity_seized event names the fleet in `target`, as in
     test_tending_oracle; a mover_moved's `t` is not asserted (start or end of the move).
 A4  Case 2's second part is a second arrival of the same CREATE two units after the first;
     case 11's two parts are one arrival of two entities.
 A5  Case 6's ROUTE jump is a one-step sequence ("ToC": SC). The spec's "T2 to SA" is a second
     station SA2 on node A: a TRANSPORT continues at its destination STATION, and SA leads back
     into T1 (a loop). Same geometry. Case 13's second part is its own CREATE of type "Other" with a sequence ("ToB":
     SB) behind a ROUTE of time 20.
 A6  Case 16 compares a started run with the uninterrupted one through test_state._carried
     (the repo's exit check), which is "resumes and gives test 1's results".
 A7  Case 17 is split: the no-keep numbers (pass on any engine) and the all-zero `Held %`
     column (fails until the column exists).
"""
import pytest

from conftest import chain, create, dispose, layout, problem_codes, process, resource_row
from test_state import _carried, _refusal

from nagasim import api
from nagasim.core.trace import TraceRecorder

NETWORK = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0},
                     {"name": "C", "x": 20, "y": 0}],
           "segments": [{"from": "A", "to": "B", "length": 10.0, "two_way": True},
                        {"from": "B", "to": "C", "length": 10.0, "two_way": True}]}


# ------------------------------------------------------------------ builders
def _fleet(**over):
    base = {"name": "AGV", "count": 1, "speed": 1.0, "speed_empty": 1.0, "load_time": "1",
            "unload_time": "1", "home": "A", "park": "stay"}
    base.update(over)
    return base


def _station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": {"node": node}}


def _transport(name, destination, keep=False):
    params = {"fleet": "AGV", "destination": destination, "priority": 0}
    if keep:
        params["keep_vehicle"] = True
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [2, 0],
            "params": params}


def _delay5():
    return process("proc", resource="", value=5.0, action="Delay")


def _release(name="free"):
    return {"name": name, "description": name, "type": "RELEASE", "position": [3, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                       "resource_name": "AGV"}}


def _sim(run, warmup=0):
    return {"time_limit": run, "warmup": warmup, "replications": 1, "time_unit": "hours"}


def _doc(blocks, links, *, run=40, warmup=0, fleet=None, **sections):
    # A keeper arrival far past the horizon keeps the clock running to `run_length` (DEVLOG #32);
    # without it the observed window is the drain time (29) and the percentages are not the
    # spec's (over 40).
    keeper = [create("keeper_create", start_time=10 ** 9, max_arrivals=1),
              dispose("keeper_dispose")]
    blocks, links = blocks + keeper, links + chain(*keeper)
    return layout(blocks, links, network=NETWORK, fleets=[fleet or _fleet()],
                  simulation=_sim(run, warmup), **sections)


def _base(*, keep1=True, keep2=False, arrivals=1, gap=2.0, per_arrival=1, run=40, warmup=0,
          fleet=None, tail=None, **sections):
    """Flow F. `tail` replaces the T2 -> SC part: a list of blocks after the PROCESS."""
    head = [create(max_arrivals=arrivals, dist_params={"value": gap},
                   entities_per_arrival=per_arrival),
            _station("SA", "A"), _transport("T1", "SB", keep1), _station("SB", "B"), _delay5()]
    rest = tail if tail is not None else [_transport("T2", "SC", keep2), _station("SC", "C"),
                                          dispose()]
    blocks = head + rest
    return _doc(blocks, chain(*blocks), run=run, warmup=warmup, fleet=fleet, **sections)


def _run(doc, run=40, warmup=0):
    """The horizon is passed as `run_length`: the percentages are over it, not over the drain
    time (the spec's 40)."""
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, run_length=run, warmup=warmup, trace=recorder)
    return results, recorder


def _ev(recorder, kind, target=None, block=None):
    return sorted((e for e in recorder.events if e.kind == kind
                   and (target is None or e.target == target)
                   and (block is None or e.block == block)), key=lambda e: e.t)


def _fleet_row(results):
    return results["fleet_summary"].iloc[0]


def _part_times(results):
    row = results["entity_summary"].iloc[0]
    return row["Min Time"], row["Max Time"], row["Avg Time"]


# ------------------------------------------------------------------ 1
def test_1_the_held_vehicle_waits_at_b_and_carries_the_same_part_on():
    """Load 0-1, drive 1-11 (10), unload 11-12, held 12-17 (the Delay of 5), load 17-18, drive
    18-28, unload 28-29: the part is out at 29. Run 40: loaded distance 20, empty 0; Travel
    Loaded 20/40 = 50, Load/Unload 4/40 = 10, Held 5/40 = 12.5, Idle 11/40 = 27.5 (sum 100).
    The unit stays seized 0-29: Utilization 29/40 = 72.5 %, one seize, no request wait."""
    results, _ = _run(_base())
    assert _part_times(results)[2] == pytest.approx(29.0)
    row = _fleet_row(results)
    assert row["Trips"] == 2 and row["Loads per Trip"] == pytest.approx(1.0)
    assert row["Distance Loaded"] == pytest.approx(20.0)
    assert row["Distance Empty"] == pytest.approx(0.0)
    assert row["Travel Loaded %"] == pytest.approx(50.0)
    assert row["Load/Unload %"] == pytest.approx(10.0)
    assert row["Held %"] == pytest.approx(12.5)
    assert row["Idle %"] == pytest.approx(27.5)
    res = resource_row(results, "AGV")
    assert res["Usage % (snapshot)"] == pytest.approx(72.5)
    assert res["Total Seizes"] == 1                 # one per job, not per leg
    assert row["Avg Request Wait"] == pytest.approx(0.0)


# ------------------------------------------------------------------ 2
def test_2_a_second_part_waits_for_the_whole_job_of_the_first():
    """Parts at 0 and 2. Part 1 as case 1 (vehicle freed at C at 29). Part 2 asked at 2 and
    waits 27 (Avg Request Wait (0 + 27) / 2 = 13.5). From 29: empty C->A 29-49, load 49-50,
    drive 50-60, unload 60-61, held 61-66, load 66-67, drive 67-77, unload 77-78: part 2
    time 78 - 2 = 76."""
    results, _ = _run(_base(arrivals=2, gap=2.0), 100)
    assert _part_times(results)[:2] == pytest.approx((29.0, 76.0))
    row = _fleet_row(results)
    assert row["Avg Request Wait"] == pytest.approx(13.5)
    assert row["Trips"] == 4
    assert row["Distance Empty"] == pytest.approx(20.0)
    assert row["Distance Loaded"] == pytest.approx(40.0)


# ------------------------------------------------------------------ 3
def test_3_without_keep_the_second_part_takes_the_vehicle_at_b():
    """Test 2 with T1 not keeping: part 1 unloads at B at 12 and frees the vehicle; part 2
    (waiting since 2) takes it there: empty B->A 12-22, load 22-23, drive 23-33, unload
    33-34. Part 1's T2 (asked at 17) is granted at 34, at B: load 34-35, drive 35-45, unload
    45-46: part 1 ends at 46. Part 2 reaches B at 34 and its Delay ends at 39 (T2 asked after
    part 1's already waiting), so only part 1's time is asserted."""
    results, _ = _run(_base(keep1=False, arrivals=2, gap=2.0), 100)
    assert _part_times(results)[0] == pytest.approx(46.0)


# ------------------------------------------------------------------ 4
def _release_flow(**kw):
    tail = [_release(), dispose()]
    return _base(tail=tail, **kw)


def test_4_a_release_frees_the_held_vehicle_where_it_stands():
    """T1 keep -> SB -> Delay 5 -> RELEASE AGV -> DISPOSE: held 12-17, freed at 17 at B (event
    `entity_released` of the AGV at 17 from the RELEASE block). The hold is no resource seize
    for the entity: its cost is 0 (A14)."""
    results, recorder = _run(_release_flow())
    (freed,) = _ev(recorder, "entity_released", target="AGV")
    assert freed.t == pytest.approx(17.0) and freed.block == "free"
    row = _fleet_row(results)
    assert row["Held %"] == pytest.approx(5.0 / 40 * 100)
    assert row["Trips"] == 1


def test_4_park_home_drives_back_after_the_free():
    """Same with park "home": no drive between the legs, so the vehicle leaves B at 17 and is
    back at A at 27 (10 units of empty travel)."""
    results, recorder = _run(_release_flow(fleet=_fleet(park="home")))
    assert _fleet_row(results)["Distance Empty"] == pytest.approx(10.0)
    back = [e for e in _ev(recorder, "mover_moved", target="AGV") if e.state == "empty"]
    assert [(m.path, m.duration) for m in back] == [(("B", "A"), pytest.approx(10.0))]
    assert back[0].t in (pytest.approx(17.0), pytest.approx(27.0))
    # No empty drive happened before the release (the part never needed a second vehicle).
    assert all(e.t >= 17.0 - 1e-9 for e in back)


# ------------------------------------------------------------------ 5
def _auto_free(with_release=False):
    tail = ([_release()] if with_release else []) + [dispose()]
    head = [create(max_arrivals=1), _station("SA", "A"), _transport("T1", "SB", True),
            _station("SB", "B")]
    blocks = head + tail
    return _doc(blocks, chain(*blocks))


def test_5_a_disposed_part_frees_the_vehicle_at_the_dispose():
    """T1 keep -> SB -> DISPOSE: unload ends at 12, the part reaches the DISPOSE at once and
    the vehicle is freed at 12 by it."""
    _, recorder = _run(_auto_free())
    (freed,) = _ev(recorder, "entity_released", target="AGV")
    assert freed.t == pytest.approx(12.0) and freed.block == "dispose_1"


def test_5_validation_warns_until_a_release_is_added():
    assert "W_TRANSPORT_KEEP_UNFREED" in problem_codes(_auto_free(), severity=None)
    assert "W_TRANSPORT_KEEP_UNFREED" not in problem_codes(_auto_free(True), severity=None)


# ------------------------------------------------------------------ 6
def test_6_a_part_moved_on_by_route_is_fetched_by_its_own_vehicle():
    """Delay ends 17; ROUTE (time 3) puts the part at SC at 20. T2 (dest SA) uses the held
    vehicle, standing at B: empty B->C 20-30, load 30-31, drive C->A 31-51, unload 51-52. Part
    time 52; empty 10, loaded 10 + 20 = 30."""
    route = {"name": "jump", "description": "jump", "type": "ROUTE", "position": [2, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 3.0},
                        "travel": "time", "speed": ""}}
    head = [create(max_arrivals=1, sequence="ToC"), _station("SA", "A"),
            _transport("T1", "SB", True), _station("SB", "B"), _delay5(), route]
    blocks = head + [dispose()]
    sc = [_station("SC", "C"), _transport("T2", "SA2", False), _station("SA2", "A")]
    links = chain(*head) + [{"source": "jump", "target": "dispose_1"}] + \
        chain(*sc) + [{"source": "SA2", "target": "dispose_1"}]
    doc = _doc(blocks + sc, links, run=100,
               sequences=[{"name": "ToC", "steps": [{"station": "SC"}]}])
    results, _ = _run(doc, 100)
    assert _part_times(results)[2] == pytest.approx(52.0)
    row = _fleet_row(results)
    assert row["Distance Empty"] == pytest.approx(10.0)
    assert row["Distance Loaded"] == pytest.approx(30.0)


# ------------------------------------------------------------------ 7
def test_7_a_chain_of_two_keeps_is_freed_by_the_release():
    """T1 keep, T2 keep, RELEASE right after SC: held 12-17, held again at 29 (zero time) and
    freed by the RELEASE at 29. Trips 2; held time 5 (12.5 % of 40)."""
    tail = [_transport("T2", "SC", True), _station("SC", "C"), _release(), dispose()]
    results, recorder = _run(_base(tail=tail))
    (freed,) = _ev(recorder, "entity_released", target="AGV")
    assert freed.t == pytest.approx(29.0) and freed.block == "free"
    row = _fleet_row(results)
    assert row["Trips"] == 2
    assert row["Held %"] == pytest.approx(12.5)
    assert "W_TRANSPORT_KEEP_UNFREED" not in problem_codes(_base(tail=tail), severity=None)


# ------------------------------------------------------------------ 8
def test_8_a_capacity_schedule_never_takes_a_held_vehicle():
    """Capacity 1 until 14, 0 on 14-20, then 1. At 14 the unit is held (12-17) and the shift
    end waits for a free unit; the part is still done at 29."""
    shift = {"name": "Shift", "kind": "capacity", "points": [[14, 1], [6, 0], [100, 1]]}
    doc = _base(fleet=_fleet(capacity_schedule="Shift"), schedules=[shift])
    results, _ = _run(doc)
    assert _part_times(results)[2] == pytest.approx(29.0)


# ------------------------------------------------------------------ 9
def test_9_a_held_vehicle_drains_at_the_idle_power():
    """Busy 0.5 kW, idle 0.1 kW, 100 kWh: 12 h busy (0-12) = 6, held 5 h at idle = 0.5, 12 h
    busy (17-29) = 6: 12.5 kWh at 29, SoC 0.875 (0.85 would mean held drained at busy power).
    The T2 free at 29 settles the battery and emits a `battery` event."""
    fleet = _fleet(power={"busy": 0.5, "idle": 0.1},
                   battery={"capacity_kwh": 100.0, "low": 0.1, "initial": 1.0})
    _, recorder = _run(_base(fleet=fleet))
    socs = [(e.t, e.value) for e in _ev(recorder, "battery")]
    assert socs and socs[-1][0] == pytest.approx(29.0)
    assert socs[-1][1] == pytest.approx(0.875)


# ------------------------------------------------------------------ 10
def test_10_warm_up_counts_only_the_time_after_it():
    """Warm-up 15 of 40: 25 measured. Trips 1 (T2), loaded 18-28 = 10 (40 %), held 15-17 = 2
    (8 %), load/unload 17-18 and 28-29 = 2 (8 %), idle 29-40 = 11 (44 %)."""
    results, _ = _run(_base(), 40, 15)
    row = _fleet_row(results)
    assert row["Trips"] == 1
    assert row["Distance Loaded"] == pytest.approx(10.0)
    assert row["Held %"] == pytest.approx(8.0)
    assert row["Travel Loaded %"] == pytest.approx(40.0)
    assert row["Load/Unload %"] == pytest.approx(8.0)
    assert row["Idle %"] == pytest.approx(44.0)


# ------------------------------------------------------------------ 11
def test_11_a_keep_request_never_rides_a_tour():
    """Fleet loads 2, two parts at 0 into T1 keep: no tour forms. Part 2 is granted at 29
    when part 1's hold ends (waits 29, Avg (0 + 29) / 2 = 14.5) and then runs case 2's
    sequence from 29: time 78. One load per trip."""
    doc = _base(per_arrival=2, arrivals=1, fleet=_fleet(loads=2))
    results, _ = _run(doc, 100)
    row = _fleet_row(results)
    assert row["Loads per Trip"] == pytest.approx(1.0)
    assert row["Max Request Wait"] == pytest.approx(29.0)
    assert row["Avg Request Wait"] == pytest.approx(14.5)
    assert _part_times(results)[:2] == pytest.approx((29.0, 78.0))


# ------------------------------------------------------------------ 12
def test_12_trace_shows_one_seize_a_held_stand_and_one_release():
    results, recorder = _run(_base())
    assert [e.t for e in _ev(recorder, "entity_seized", target="AGV")] == [pytest.approx(0.0)]
    held = [e for e in _ev(recorder, "mover_moved", target="AGV") if e.state == "held"]
    assert [(e.t, e.path, e.duration) for e in held] == [
        (pytest.approx(12.0), ("B",), pytest.approx(0.0))]
    assert [e.t for e in _ev(recorder, "entity_released", target="AGV")] == [
        pytest.approx(29.0)]


# ------------------------------------------------------------------ 13
def test_13_absorption_into_a_batch_frees_the_vehicle():
    """Part 1 at SB at 12 holds the vehicle. A second part (own CREATE, ROUTE of time 20) joins
    SB at 20 and the BATCH of 2 forms: the vehicle is freed at 20 and not before."""
    route = {"name": "late", "description": "late", "type": "ROUTE", "position": [1, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 20.0},
                        "travel": "time", "speed": ""}}
    batch = {"name": "pair", "description": "pair", "type": "BATCH", "position": [4, 0],
             "params": {"batch_size": 2}}
    main = [create(max_arrivals=1), _station("SA", "A"), _transport("T1", "SB", True),
            _station("SB", "B"), batch, dispose()]
    second = [create("c2", max_arrivals=1, e_type="Other", sequence="ToB"), route]
    links = chain(*main) + chain(*second) + [{"source": "late", "target": "dispose_1"}]
    doc = _doc(main + second, links,
               sequences=[{"name": "ToB", "steps": [{"station": "SB"}]}])
    _, recorder = _run(doc, 60)
    (freed,) = _ev(recorder, "entity_released", target="AGV")
    assert freed.t == pytest.approx(20.0)


# ------------------------------------------------------------------ 14
def test_14_the_hold_is_busy_cost_of_the_resource_never_of_the_part():
    """cost_busy 1 per hour, model in hours: the unit is busy 0-29, so AGV Busy Cost 29 in the
    resource frame; the part's cost stays 0."""
    fleet = _fleet(cost_busy=1.0, cost_time_unit="hours")
    results, _ = _run(_base(fleet=fleet))
    assert resource_row(results, "AGV")["Busy Cost"] == pytest.approx(29.0)
    assert results["entity_summary"].iloc[0]["Avg Cost"] == pytest.approx(0.0)


# ------------------------------------------------------------------ 15
def test_15_validation_refuses_keep_on_an_arm_fleet():
    profile = {"name": "P", "speed": 1.0, "accel": 2.0, "collaborative_speed": 0.25,
               "payload_max": 12.5, "payload_derate": 0.6}
    doc = _base(fleet=_fleet(arm="P"), motion_profiles=[profile])
    assert "E_TRANSPORT_KEEP_ARM" in problem_codes(doc)
    plain = _base(keep1=False, fleet=_fleet(arm="P"), motion_profiles=[profile])
    assert "E_TRANSPORT_KEEP_ARM" not in problem_codes(plain)


def test_15_the_unfreed_warning_follows_the_flow():
    # Base flow F: T2 (same fleet, keep false) frees the hold downstream of T1.
    assert "W_TRANSPORT_KEEP_UNFREED" not in problem_codes(_base(), severity=None)
    # A keep T2 and nothing after it: nothing frees the vehicle before the part leaves.
    assert "W_TRANSPORT_KEEP_UNFREED" in problem_codes(_base(keep2=True), severity=None)


# ------------------------------------------------------------------ 16
def test_16_a_state_with_a_held_vehicle_is_refused_by_name():
    assert "a vehicle held across legs" in _refusal(_base(), 15.0)


def test_16_a_state_before_the_hold_resumes_exactly():
    """At t=5 the first trip is under way (loaded drive 1-11): it resumes as today and holds
    at its drop, giving the uninterrupted run's results."""
    _carried(_base(), 5.0)


# ------------------------------------------------------------------ 17
def test_17_without_keep_nothing_moves():
    """No keep: T1 frees at 12 (vehicle stays at B), T2 asks at 17 and takes it where it
    stands: the same 29, two trips, and the vehicle busy 0-12 and 17-29 (60 %), no held time."""
    results, _ = _run(_base(keep1=False))
    assert _part_times(results)[2] == pytest.approx(29.0)
    row = _fleet_row(results)
    assert row["Trips"] == 2 and row["Distance Empty"] == pytest.approx(0.0)
    assert row["Load/Unload %"] == pytest.approx(10.0)
    assert resource_row(results, "AGV")["Usage % (snapshot)"] == pytest.approx(60.0)
    assert "W_TRANSPORT_KEEP_UNFREED" not in problem_codes(_base(keep1=False), severity=None)


def test_17_the_held_column_is_all_zero_without_keep():
    results, _ = _run(_base(keep1=False))
    columns = list(results["fleet_summary"].columns)
    assert columns.index("Held %") == columns.index("Load/Unload %") + 1
    assert _fleet_row(results)["Held %"] == pytest.approx(0.0)
