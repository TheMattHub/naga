"""Segment traffic: zone control on capacity-limited segments (PHASE6 W3 M5,
ARCHITECTURE D117). The oracle is tests/test_segment_traffic_oracle.py; this file covers
the holds, the interrupted return, playback, validation and the no-capacity
fast path. Every number is hand-computed travel arithmetic (length / speed).
"""
import pytest

from nagasim import api
from nagasim.core import movers, schema
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder

from conftest import create, dispose, layout
from test_network import fleet, route, station, transport


def _line(segments, nodes=("A", "B", "C")):
    return {"nodes": [{"name": n, "x": 10 * i, "y": 0} for i, n in enumerate(nodes)],
            "segments": segments}


def _seg(a, b, length, capacity=None, two_way=False):
    return {"from": a, "to": b, "length": length, "two_way": two_way, "capacity": capacity}


def _carry_model(net, fleets, creates, sequences, extra_stations=(), links=(), limit=100):
    """Each CREATE goes through route_1 to the first station of its sequence;
    stations named in *links* hand the entity to their TRANSPORT."""
    blocks = [*creates, route(), *extra_stations, dispose()]
    all_links = [{"source": c["name"], "target": "route_1"} for c in creates]
    all_links += [{"source": "route_1", "target": "dispose_1"}, *links]
    return layout(blocks, all_links, network=net, fleets=fleets, sequences=sequences,
                  simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def _chain_model():
    """A->B (10 m) then B->C (30 m), both one-way capacity 1, speed 5: 2 and 6
    to cross. Three vehicles at A, entities at t = 0, 1, 2 going A -> C."""
    net = _line([_seg("A", "B", 10, 1), _seg("B", "C", 30, 1)])
    return _carry_model(
        net, [fleet("F", count=3, speed=5.0, speed_empty=5.0, load_time="0", unload_time="0",
                    home="A")],
        [create(max_arrivals=3, sequence="AC")],
        [{"name": "AC", "steps": [{"station": "SA"}, {"station": "SC"}]}],
        [station("SA", "A"), transport("carry", "F"), station("SC", "C", position=(4, 0))],
        [{"source": "SA", "target": "carry"}, {"source": "carry", "target": "dispose_1"},
         {"source": "SC", "target": "dispose_1"}])


def test_a_waiting_mover_keeps_holding_the_segment_it_stands_at_the_end_of():
    """V1: A->B 0-2, B->C 2-8. V2 (t=1) waits for A->B until 2, crosses 2-4,
    then waits AT B, still holding A->B, until V1 clears B->C at 8; B->C 8-14.
    V3 (t=2) therefore gets A->B only when V2 enters B->C at 8 (a 6 wait, not
    the 2 it would be if V2 let go on reaching B), crosses 8-10, waits at B
    until 14, B->C 14-20."""
    results = api.run_model(_chain_model(), replications=1)
    exits = sorted(results["entity_log"]["Exited At"])
    assert exits == pytest.approx([8.0, 14.0, 20.0])
    segments = results["segment_summary"].set_index("Segment")
    assert segments.loc["A->B", "Wait Time"] == pytest.approx(1.0 + 6.0)
    assert segments.loc["B->C", "Wait Time"] == pytest.approx(4.0 + 4.0)
    assert segments.loc["A->B", "Max Occupancy"] == 1
    assert segments.loc["B->C", "Traversals"] == 3
    fleet_row = results["fleet_summary"].iloc[0]
    assert fleet_row["Distance Loaded"] == pytest.approx(3 * 40.0)   # waits add no distance


def test_playback_holds_a_waiting_mover_still_at_the_segment_end():
    """V2 waits at B from 4 to 8: its last leg is A->B, finished, and the
    stationary leg emitted when the wait began pins it at B."""
    recorder = TraceRecorder()
    api.run_model(_chain_model(), replications=1, trace=recorder)
    state = Timeline(recorder.events).state_at(6.0)
    path, _progress, mover_state = state["movers"][("F", 1)]
    assert (path, mover_state) == (("B",), "loaded")
    legs = [e.path for e in recorder.events if e.kind == "mover_moved" and e.entity_id == 1
            and e.state == "loaded"]
    assert legs == [("A",), ("A", "B"), ("B",), ("B", "C")]


def _homing_model():
    """H --20-- M --10-- S, both two-way; H-M capacity 1, M-S capacity 2; two
    vehicles homed at H, park home, speed 5 (H-M takes 4, M-S 2), no handling.

    e1 (t=0, H->S) takes A: H->M 0-4, M->S 4-6. A heads home: S->M 6-8, then
    at M waits for H-M, still holding M-S, because B (e2, t=7, H->M) is on it
    7-11. e3 (t=9, M->S) diverts A while it WAITS: A drops its request and M-S
    and carries e3 M->S 9-11. B goes home M->H 11-15; A S->M 11-13, waits at M
    for H-M until 15, M->H 15-19."""
    net = _line([_seg("H", "M", 20, 1, two_way=True), _seg("M", "S", 10, 2, two_way=True)],
                nodes=("H", "M", "S"))
    return _carry_model(
        net, [fleet("F", count=2, speed=5.0, speed_empty=5.0, load_time="0", unload_time="0",
                    home="H", park="home")],
        [create(name="c1", max_arrivals=1, sequence="HS"),
         create(name="c2", max_arrivals=1, start_time=7, sequence="HM"),
         create(name="c3", max_arrivals=1, start_time=9, sequence="MS")],
        [{"name": "HS", "steps": [{"station": "SH"}, {"station": "SS"}]},
         {"name": "HM", "steps": [{"station": "SH"}, {"station": "MDrop"}]},
         {"name": "MS", "steps": [{"station": "MPick"}, {"station": "SS"}]}],
        [station("SH", "H"), station("MDrop", "M"), station("MPick", "M"),
         station("SS", "S"), transport("carry", "F")],
        [{"source": "SH", "target": "carry"}, {"source": "MPick", "target": "carry"},
         {"source": "carry", "target": "dispose_1"},
         {"source": "MDrop", "target": "dispose_1"}, {"source": "SS", "target": "dispose_1"}])


def test_a_return_interrupted_while_waiting_drops_its_request_and_its_held_segment():
    results = api.run_model(_homing_model(), replications=1)
    log = results["entity_log"].sort_values("Created At")
    assert list(log["Exited At"]) == pytest.approx([6.0, 11.0, 11.0])
    segments = results["segment_summary"].set_index("Segment")
    assert segments.loc["H<->M", "Wait Time"] == pytest.approx(1.0 + 2.0)  # A: 8-9, 13-15
    assert segments.loc["M<->S", "Wait Time"] == pytest.approx(0.0)
    fleet_row = results["fleet_summary"].iloc[0]
    assert fleet_row["Distance Loaded"] == pytest.approx(30 + 10 + 20)
    assert fleet_row["Distance Empty"] == pytest.approx(10 + 30 + 20)


def test_two_way_capacity_two_lets_vehicles_pass_head_on():
    """The oracle's head-on corridor with capacity 2 everywhere: no deadlock,
    each vehicle crosses its 30 m in 6."""
    import test_segment_traffic_oracle as oracle
    model = oracle._deadlock_model()
    for seg in model["network"]["segments"]:
        seg["capacity"] = 2
    results = api.run_model(model, replications=1)
    assert list(results["entity_log"]["Exited At"]) == pytest.approx([6.0, 6.0])


def test_deadlock_risk_counts_vehicles_over_every_fleet_and_skips_one_way():
    """Two single-unit fleets meet head-on as surely as one two-unit fleet
    (the oracle's own deadlock case); a one-way segment cannot."""
    import test_segment_traffic_oracle as oracle
    codes = [p.code for p in schema.validate_model_structured(oracle._deadlock_model())]
    assert codes.count("W_SEGMENT_DEADLOCK_RISK") == 3
    assert "W_SEGMENT_DEADLOCK_RISK" not in {
        p.code for p in schema.validate_model_structured(_chain_model())}


def test_no_capacity_keeps_the_one_timeout_drive(monkeypatch):
    """Perf contract: a network with no capacity builds no traffic controller
    and never enters the per-segment drive, so it runs exactly the old code."""
    def boom(*_args, **_kwargs):
        raise AssertionError("zoned drive used without a capacity")

    monkeypatch.setattr(movers.MoverFleet, "_drive_zoned", boom)
    model = _chain_model()
    for seg in model["network"]["segments"]:
        seg["capacity"] = None
    results = api.run_model(model, replications=1)
    assert sorted(results["entity_log"]["Exited At"]) == pytest.approx([8.0, 9.0, 10.0])
    assert results["segment_summary"]["Wait Time"].eq(0.0).all()
    assert results["segment_summary"]["Max Occupancy"].isna().all()
