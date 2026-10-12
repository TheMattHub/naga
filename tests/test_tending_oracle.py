"""Independent oracle for mobile cobots / machine tending (automation sub-project 3, D168-D170).

Written ONLY from docs/superpowers/specs/2026-10-04-mobile-cobots-design.md and the plan's
"Spec clarifications", "Review Focus" and Task 1-3 "Interfaces". No tending code was opened.
Every expected number is hand-derived in the test's docstring. New modules would be imported
INSIDE the tests so the file collects before they exist (this file needs none: it runs through
`nagasim.api` and public results only).

Shared model, TIME UNIT = SECONDS. Network: src(0,0) -- mach(10,0), length 10; fleet speed 5 so
every leg is 2 s. Flat load/unload times 0, so only tending cycles take time.
    CREATE -> Src -> feed(TRANSPORT, dest Mach) -> Mach(STATION, tending) -> [buf] -> lathe
    (PROCESS, resource Lathe, constant service S) -> out(TRANSPORT, dest Out) -> Out -> DISPOSE.
Lathe is one machine; `place` = 2 s and `pick` = 3 s unless stated; S = 10 s. The cobots'
home is src and `park` is "stay" unless stated. Entity k is created at (k-1)*gap.

How times are read (public events only): `entity_seized` / `entity_released` with target = a
fleet or a resource name and block = the emitting block; `entity_disposed`; `mover_moved`
(state "empty" / "loaded", t = start of that leg); `fleet_summary` "Distance Empty";
`tending_summary`. The moment the machine finishes its work is observed as the grant of the
`out` TRANSPORT's vehicle when a cobot is free at the machine (grant time = service end).

ASSUMPTIONS where the spec/plan is silent (the coordinator reconciles these):
 A1  Dispatch is the fleet default "nearest": with two free cobots the request goes to the
     one closest to the pick-up node. Tests are built so this never matters except where
     stated.
 A2  The first CREATE arrival is at t=0, then every `gap`.
 A3  Timed tending segments take a number (`"time": 2.0`) as in the spec's example.
 A4  The Lathe resource is "released" (entity_released, target "Lathe") when the machine
     lets go of the finished part, i.e. when the pick cycle ends (blocking mechanism, spec
     D169). A staying or leaving cobot's fleet slot is released by `entity_released` with
     target = the fleet.
 A5  Under `stay`, the second TRANSPORT of the same fleet emits NO `entity_seized` for the
     fleet (no dispatch), and no fleet `entity_released` happens between place end and the
     end of the next trip.
 A6  A loaded cobot waiting at a busy machine is a normal request holder: its fleet slot is
     released only after the place cycle.
 A7  Exchange: the cobot arrives loaded AFTER the finished part has reached its TRANSPORT
     (so no ambiguity about hand-over of an already waiting cobot). The part's vehicle request
     is withdrawn; the machine accepts the new part when the pick ends (it is free then).
 A8  `tending_summary`: one row per (station, fleet) named by the STATION block name
     ("Mach") and fleet name ("Cobot"); "Avg Loaded Wait" is the mean over ALL places
     (a place that did not wait counts 0); "Avg Blocked For Pick" the mean over picks of
     (pick start - arrival of the part at its TRANSPORT). In an exchange the loaded wait of the
     new part ends when the machine accepts it (after the pick), per the plan clarification.
 A9  `tend` trace event (only test_tend_events_report_cycles relies on it): TraceEvent.state
     is "pick" or "place", `t` the cycle start, `duration` the cycle length.
 A10 Plain-fleet-at-tending-station behaves exactly as without `tending` (plan clarification
     "Tending applies to one trip end only when BOTH hold").
"""
import math

import pytest

from conftest import create, dispose, layout, process

from nagasim import api
from nagasim.core.trace import TraceRecorder

NETWORK = {"nodes": [{"name": "src", "x": 0, "y": 0}, {"name": "mach", "x": 10, "y": 0}],
           "segments": [{"from": "src", "to": "mach", "length": 10}]}

PROFILE = {"name": "P", "speed": 1.0, "accel": 2.0, "collaborative_speed": 0.25,
           "payload_max": 12.5, "payload_derate": 0.6}


# ------------------------------------------------------------------ builders
def _station(name, node, **extra):
    return {"name": name, "description": name, "type": "STATION", "position": [0, 0],
            "params": {"node": node, **extra}}


def _transport(name, destination):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [0, 0],
            "params": {"fleet": "Cobot", "destination": destination, "priority": 0}}


def _buffer(name, capacity):
    return {"name": name, "description": name, "type": "BUFFER", "position": [0, 0],
            "params": {"capacity": capacity, "level": ""}}


def model(*, count=1, arrivals=1, gap=1.0, service=10.0, place=2.0, pick=3.0, cobot="leave",
          exchange=False, buffer=0, arm=True, tending=True, park="stay", hrc="none",
          place_segments=None, pick_segments=None, load="0", unload="0", time_limit=60):
    """The shared model. `arm=False` strips the fleet's arm and `exchange`; `tending=False`
    strips the station's group: with both off it is a plain TRANSPORT model (used to check
    the builder itself runs)."""
    fleet = {"name": "Cobot", "count": count, "speed": 5.0, "speed_empty": 5.0,
             "load_time": load, "unload_time": unload, "home": "src", "park": park}
    if arm:
        fleet["arm"] = "P"
        if exchange:
            fleet["exchange"] = True
    group = {"place": place_segments or [{"kind": "place", "time": place}],
             "pick": pick_segments or [{"kind": "pick", "time": pick}],
             "time_unit": "seconds", "hrc": hrc, "zone": {}, "cobot": cobot}
    mach = _station("Mach", "mach", **({"tending": group} if tending else {}))
    first = [_buffer("buf", buffer)] if buffer else []
    blocks = [create(max_arrivals=arrivals, dist_params={"value": gap}),
              _station("Src", "src"), _transport("feed", "Mach"), mach, *first,
              process("lathe", resource="Lathe", value=service),
              _transport("out", "Out"), _station("Out", "src"), dispose()]
    names = ["create_1", "Src", "feed", "Mach", *(["buf"] if buffer else []), "lathe", "out",
             "Out", "dispose_1"]
    links = [{"source": a, "target": b} for a, b in zip(names, names[1:])]
    return layout(blocks, links, network=NETWORK, fleets=[fleet], motion_profiles=[PROFILE],
                  resources=[{"name": "Lathe", "capacity": 1}],
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1,
                              "time_unit": "seconds"})


def run(doc):
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    return results, recorder


def ev(rec, kind, target=None, block=None, entity=None):
    out = [e for e in rec.events if e.kind == kind
           and (target is None or e.target == target)
           and (block is None or e.block == block)
           and (entity is None or e.entity_id == entity)]
    return sorted(out, key=lambda e: e.t)


def ts(events):
    return [e.t for e in events]


def entity_ids(rec):
    """Entity ids in creation order: [e1, e2, ...]."""
    return [e.entity_id for e in ev(rec, "entity_created")]


def lathe_releases(rec):
    return ts(ev(rec, "entity_released", target="Lathe"))


def grants(rec, block):
    return ts(ev(rec, "entity_seized", target="Cobot", block=block))


def oracle_derate(payload_kg, payload_max, floor):
    return 1.0 - (1.0 - floor) * min(payload_kg / payload_max, 1.0)


def oracle_move(distance_m, speed, accel):
    """Trapezoid when the move reaches v (d >= v^2/a), else the triangle."""
    if distance_m >= speed * speed / accel:
        return distance_m / speed + speed / accel
    return 2.0 * math.sqrt(distance_m / accel)


# --------------------------------------------------------------- sanity check
def test_builder_runs_without_arm_or_tending():
    """Plain TRANSPORT model (flat times 0, cobot idle at src): e1 granted 0, drive 0->2 to
    Mach, service 2->12 (no place cycle), `out` grant 12, drive 12->14 (cobot at Mach: 0
    empty), disposed 14."""
    results, rec = run(model(arm=False, tending=False))
    assert grants(rec, "out") == pytest.approx([12.0])
    assert ts(ev(rec, "entity_disposed")) == pytest.approx([14.0])
    assert lathe_releases(rec) == pytest.approx([12.0])


# ------------------------------------------------------------------- the cases
def test_pick_blocks_the_machine_until_the_cycle_ends():
    """Case 1. Two cobots, 2 parts at t=0 and t=1, S=10, place 2, pick 3.
    e1: loaded 0->2, machine free, place 2->4, service 4->14. e2 (only the second cobot is
    free): loaded 1->3, waits loaded at the busy machine. At 14 e1's `out` gets the cobot
    that is free at the machine (no empty drive) and its pick runs 14->17: the Lathe is
    released at 17 (NOT 14). e2 is accepted at 17, place 17->19, service 19->29, so the
    machine's next job could not start earlier than pick end + place: e2's `out` grant at
    29 (= 19 + 10). Assumptions A1, A4."""
    _, rec = run(model(count=2, arrivals=2, gap=1.0))
    assert lathe_releases(rec)[0] == pytest.approx(17.0)
    assert grants(rec, "out") == pytest.approx([14.0, 29.0])


def test_place_cycle_delays_service_even_when_the_machine_is_free():
    """Case 2 (Review Focus 1). One cobot, one part, place 3, pick 3, S=10: loaded arrival
    at t=2 at a free machine: accepted at once (no loaded wait) but the place cycle runs
    2->5, so service is 5->15 and the cobot (free at Mach since 5) is granted `out` at 15,
    not 12. Pick 15->18 releases the Lathe at 18; loaded 18->20: disposed at 20."""
    results, rec = run(model(place=3.0))
    assert grants(rec, "out") == pytest.approx([15.0])
    assert lathe_releases(rec) == pytest.approx([18.0])
    assert ts(ev(rec, "entity_disposed")) == pytest.approx([20.0])
    assert results["tending_summary"].iloc[0]["Avg Loaded Wait"] == pytest.approx(0.0)


def test_loaded_wait_at_a_busy_machine_holds_the_cobot():
    """Case 2b / plan Task 2. Same run as the pick-blocking test (count 2, gap 1).
    e2's cobot arrives loaded at t=3 and the machine only accepts at 17 (pick of e1 ends):
    loaded wait = 17 - 3 = 14 = remaining service (14 - 3 = 11) + the pick (3). The wait is
    holding the part AND the fleet slot: that cobot's `feed` request is released only after
    its place cycle, at 19 (A6). Avg Loaded Wait = (0 + 14) / 2 = 7."""
    results, rec = run(model(count=2, arrivals=2, gap=1.0))
    e2 = entity_ids(rec)[1]
    assert ts(ev(rec, "entity_released", target="Cobot", block="feed", entity=e2)) == \
        pytest.approx([19.0])
    assert results["tending_summary"].iloc[0]["Avg Loaded Wait"] == pytest.approx(7.0)


def test_leave_versus_stay_on_one_model():
    """Case 3. One cobot, one part, park "home", place 2, pick 3, S=10; the only change is
    `cobot`.
    leave: loaded 0->2, place 2->4, cobot freed at 4 and drives home 4->6 (10 empty). Service
      4->14. At 14 the cobot is at src: empty 14->16 (10 more empty), pick 16->19, Lathe
      released at 19, loaded 19->21, disposed 21. Distance Empty = 20.
    stay: the cobot waits at Mach; at 14 it picks at once (no dispatch, no empty drive),
      pick 14->17, Lathe released at 17, loaded 17->19, disposed 19. Distance Empty = 0."""
    out = {}
    for mode in ("leave", "stay"):
        results, rec = run(model(cobot=mode, park="home"))
        out[mode] = (lathe_releases(rec)[0], ts(ev(rec, "entity_disposed"))[0],
                     results["fleet_summary"].iloc[0]["Distance Empty"])
    assert out["leave"] == pytest.approx((19.0, 21.0, 20.0))
    assert out["stay"] == pytest.approx((17.0, 19.0, 0.0))


def test_stay_keeps_the_fleet_slot_through_the_machine_cycle():
    """Case 3b (A5). Same stay run: the cobot is unavailable for other work from place end
    (t=4) until the part is delivered (t=19): the fleet records NO release in (0, 19) and no
    second grant at 14. Under leave there IS a release at 4 (freed after the place cycle)."""
    _, stay = run(model(cobot="stay", park="home"))
    _, leave = run(model(cobot="leave", park="home"))
    assert [t for t in ts(ev(stay, "entity_released", target="Cobot")) if t < 19.0 - 1e-9] == []
    assert grants(stay, "out") == []
    assert 4.0 in [pytest.approx(t) for t in ts(ev(leave, "entity_released", target="Cobot"))]
    assert grants(leave, "out") == pytest.approx([14.0])


EXCHANGE_KW = dict(count=1, arrivals=2, gap=12.0, exchange=True)


def test_exchange_swaps_parts_with_no_empty_trip():
    """Case 4. One cobot, `exchange: true`, parts at t=0 and t=12, S=10, place 2, pick 3.
    e1: loaded 0->2, place 2->4, cobot free at Mach, service 4->14.
    e2 (t=12): the cobot (at Mach) is granted at 12, drives EMPTY to src 12->14, loaded
      14->16, arrives at Mach at 16. Meanwhile e1 finished at 14 and waits at `out` (no free
      cobot).  Exchange at 16: pick e1 16->19 (Lathe released at 19), place e2 19->21, service
      21->31, then carry e1 loaded 21->23 with NO empty trip: e1 disposed at 23.
    Then e2 done at 31: `out` granted 31 (= 21 + 10), empty 31->33, pick 33->36, loaded
      36->38: disposed 38.
    Empty drives only start at 12 and 31 (none in [16, 23)); Distance Empty = 10 + 10 = 20."""
    results, rec = run(model(**EXCHANGE_KW))
    assert ts(ev(rec, "entity_disposed")) == pytest.approx([23.0, 38.0])
    assert lathe_releases(rec)[0] == pytest.approx(19.0)
    assert grants(rec, "out") == pytest.approx([31.0])
    empties = [e.t for e in ev(rec, "mover_moved") if e.state == "empty"]
    assert not [t for t in empties if 16.0 - 1e-9 <= t < 23.0]
    assert results["fleet_summary"].iloc[0]["Distance Empty"] == pytest.approx(20.0)


def test_single_cobot_without_exchange_is_reported_as_deadlock():
    """Case 5. The exchange model with `exchange: false`: at t=16 the only cobot arrives
    loaded with e2 at a machine that holds the finished e1, which waits for a vehicle that
    is that very cobot. Nothing can ever happen again: the run raises (message only loosely
    matched)."""
    with pytest.raises(Exception, match=r"(?i)deadlock"):
        run(model(**{**EXCHANGE_KW, "exchange": False}))


def test_a_buffer_before_the_machine_accepts_the_part_and_frees_the_cobot():
    """Case 6. One cobot, BUFFER(1) between Mach and the lathe, parts at t=0 and t=5, S=10,
    place 2, pick 3. e1: loaded 0->2, buffer accepts, place 2->4, to the lathe at 4, service
    4->14. e2: cobot (at Mach) empty 5->7, loaded 7->9, buffer is free again (e1 left at 4)
    and accepts at 9; place 9->11; the cobot's `feed` request is released at 11 (no loaded
    wait, no deadlock although there is a single cobot). At 14 the cobot (free at Mach)
    picks e1 14->17 (Lathe released 17); e2 moves from the buffer to the lathe at 17 with no
    further place delay: service 17->27: `out` grants at 14 and 27."""
    _, rec = run(model(arrivals=2, gap=5.0, buffer=1))
    e2 = entity_ids(rec)[1]
    assert ts(ev(rec, "entity_released", target="Cobot", block="feed", entity=e2)) == \
        pytest.approx([11.0])
    assert lathe_releases(rec)[0] == pytest.approx(17.0)
    assert grants(rec, "out") == pytest.approx([14.0, 27.0])


@pytest.mark.parametrize("hrc, place_segments, place_total", [
    # payload_max 12.5, derate floor 0.6. 6.25 kg is half: factor 1 - 0.4*0.5 = 0.8, v = 0.8,
    # v^2/a = 0.32 <= 10 so trapezoid: 10/0.8 + 0.8/2 = 12.9; plus place 1.0 = 13.9.
    ("none", [{"kind": "move", "distance_m": 10, "payload_kg": 6.25},
              {"kind": "place", "time": 1.0}], 12.9 + 1.0),
    # collaborative: v = 0.25 * (1 - 0.4*(5/12.5)) = 0.25*0.84 = 0.21; trapezoid 0.8/0.21 +
    # 0.21/2 = 3.809524 + 0.105 = 3.914524; plus place 2.0 = 5.914524.
    ("collaborative", [{"kind": "move", "distance_m": 0.8, "payload_kg": 5},
                       {"kind": "place", "time": 2.0}],
     0.8 / 0.21 + 0.21 / 2.0 + 2.0),
])
def test_arm_cycle_time_follows_payload_derating_and_collaborative_speed(
        hrc, place_segments, place_total):
    """Case 7. One cobot, one part, pick 1.0, S=10. Loaded arrival at 2, so service runs
    [2 + P, 12 + P] with P the place cycle total, and `out` is granted to the cobot waiting
    at Mach at 12 + P (the pick/place time is observable only through that). The expected P
    is restated from the motion formulas by a local function, not retyped."""
    doc = model(hrc=hrc, place_segments=place_segments, pick=1.0,
                pick_segments=[{"kind": "pick", "time": 1.0}])
    _, rec = run(doc)
    if hrc == "none":
        assert oracle_move(10, 1.0 * oracle_derate(6.25, 12.5, 0.6), 2.0) == pytest.approx(12.9)
    else:
        v = 0.25 * oracle_derate(5, 12.5, 0.6)
        assert oracle_move(0.8, v, 2.0) + 2.0 == pytest.approx(place_total)
    assert grants(rec, "out") == pytest.approx([12.0 + place_total])


def test_a_model_without_tending_has_an_empty_summary_with_the_plan_columns():
    """Case 8. No arm and no tending: `tending_summary` exists, is empty, and has exactly
    the plan's columns in the plan's order."""
    results, _ = run(model(arm=False, tending=False))
    frame = results["tending_summary"]
    assert frame.empty
    assert list(frame.columns) == ["Run", "Station", "Fleet", "Picks", "Places", "Exchanges",
                                   "Avg Pick Time", "Avg Place Time", "Avg Loaded Wait",
                                   "Avg Blocked For Pick", "Avg Stay"]


def test_tending_summary_counts_and_cycle_times_for_the_exchange_model():
    """Case 9. The exchange run of test 4 (timeline there): picks = e1 at 16 (the exchange)
    and e2 at 33 = 2; places = e1 at 2 and e2 at 19 = 2; Exchanges = 1; every pick is 3 s
    and every place 2 s. One row: Station "Mach", Fleet "Cobot" (A8)."""
    results, _ = run(model(**EXCHANGE_KW))
    frame = results["tending_summary"]
    assert len(frame) == 1
    row = frame.iloc[0]
    assert (row["Station"], row["Fleet"]) == ("Mach", "Cobot")
    assert (row["Picks"], row["Places"], row["Exchanges"]) == (2, 2, 1)
    assert row["Avg Pick Time"] == pytest.approx(3.0)
    assert row["Avg Place Time"] == pytest.approx(2.0)


def test_tending_summary_waits_for_the_exchange_model():
    """Case 9b (A8). Loaded wait: e1 accepted on arrival (0), e2 arrives loaded at 16 and is
    accepted at 19 after the exchange pick: 3 -> mean 1.5. Blocked for pick: e1 reaches
    `out` at 14 and its pick starts at 16 (2); e2 reaches `out` at 31 and its pick starts at
    33 (2) -> mean 2."""
    row = run(model(**EXCHANGE_KW))[0]["tending_summary"].iloc[0]
    assert row["Avg Loaded Wait"] == pytest.approx(1.5)
    assert row["Avg Blocked For Pick"] == pytest.approx(2.0)


def test_tend_events_report_cycles():
    """Spec Outputs / Trace (A9). One cobot, one part, place 2, pick 3, S=10: a `place`
    cycle starts at 2 (duration 2) and a `pick` cycle at 14 (duration 3)."""
    _, rec = run(model())
    tends = ev(rec, "tend")
    assert [(e.state, e.t, e.duration) for e in tends] == \
        [("place", pytest.approx(2.0), pytest.approx(2.0)),
         ("pick", pytest.approx(14.0), pytest.approx(3.0))]


def test_a_plain_fleet_at_a_tending_station_keeps_flat_times():
    """A10. Fleet without `arm`, flat load = unload = 1, station declares tending: tending is
    ignored (and only W_TENDING_UNUSED warns). e1: load 0->1, loaded 1->3, unload 3->4,
    delivered at 4, service 4->14 with the Lathe released at 14 (no pick blocking); `out`
    granted 14 (cobot at Mach), load 14->15, loaded 15->17, unload 17->18: disposed 18."""
    _, rec = run(model(arm=False, load="1", unload="1"))
    assert lathe_releases(rec) == pytest.approx([14.0])
    assert grants(rec, "out") == pytest.approx([14.0])
    assert ts(ev(rec, "entity_disposed")) == pytest.approx([18.0])
