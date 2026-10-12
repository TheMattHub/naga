"""Independent oracle for W3-M7 "Gantry cranes" (docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md,
ARCHITECTURE.md 5.21). Every expected number below is computed BY HAND from the plan's own
words -- this file never imports or runs the M7 implementation to derive an expectation, so
its failures are meaningful once that implementation lands. As of 2026-09-25 the feature does
not exist yet: every test here is expected to fail (validation error, missing param, or a
number computed the "network" way) until it does.

Wire format assumed (plan's own words), kept as module constants so a coordinator
reconciling the real implementation's names has one place to look:

    fleet: {..., "motion": "gantry" (default "network"), "speed_x", "speed_y", "hoist_time"}
    travel between two nodes = max(|dx| / speed_x, |dy| / speed_y) + hoist_time, ignoring
    segments -- nodes still give the places; TRANSPORT and dispatch/select rules are
    unchanged, and `select`/`dispatch` "nearest" must rank by that same travel time.

Two readings this file commits to, since the plan states them only in outline:

  1. **Reachability validation is exempted for a gantry fleet's own travel.** Today's
     `E_NETWORK_UNREACHABLE` walk (schema.py, `unreachable_pairs`) requires a segment path
     between every pair of stations a sequence visits. "A gantry fleet needs no segments
     between them" only makes sense if that check does not apply when the pair is served by
     a gantry-motion fleet -- case 3 below is a model with NO segments at all, which must
     still validate and run.
  2. **`point_on_path`'s direct case** (ARCHITECTURE 5.21, "playback: ... `point_on_path`
     gains the direct case") is read literally: a 2-node path with no segment between its
     nodes interpolates STRAIGHT-LINE between their raw (x, y) coordinates by progress alone,
     since there is no segment length to divide by. Case 5 tests exactly this.

`speed` stays in every fleet dict below as a filler value: today's `validate_fleets_structured`
requires it (`E_FLEET_FIELD` otherwise) regardless of `motion`, and nothing in the plan says
M7 drops that requirement. The gantry travel time itself never reads it.
"""
import pytest

from nagasim import api
from nagasim.core import schema
from nagasim.core.network import build_network, point_on_path
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder

from conftest import create, dispose, layout

# -- wire-format constants ---------------------------------------------------

PARAM_MOTION = "motion"
MOTION_GANTRY = "gantry"
PARAM_SPEED_X = "speed_x"
PARAM_SPEED_Y = "speed_y"
PARAM_HOIST_TIME = "hoist_time"


def _crane(name, home, speed_x, speed_y, hoist_time=0.0, units=None, **overrides):
    base = {"name": name, PARAM_MOTION: MOTION_GANTRY, "speed": 1.0,
            PARAM_SPEED_X: speed_x, PARAM_SPEED_Y: speed_y, PARAM_HOIST_TIME: hoist_time,
            "load_time": "0", "unload_time": "0", "home": home,
            "park": "stay", "dispatch": "fifo", "select": "nearest"}
    if units is not None:
        base["units"] = units
    base.update(overrides)
    return base


def _station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [2, 0],
            "params": {"node": node}}


def _route(position=(1, 0)):
    return {"name": "route_1", "description": "route_1", "type": "ROUTE", "position": list(position),
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                       "travel": "time", "speed": ""}}


def _transport(name, fleet_name, position=(3, 0)):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": list(position),
            "params": {"fleet": fleet_name, "destination": "", "priority": 0}}


def _gantry_model(fleet, network, pickup_node, drop_node, time_limit=200):
    """create(1) -> Dock(pickup_node) -> TRANSPORT(fleet) -> Store(drop_node) -> dispose,
    the same shape `_transport_model` uses in tests/test_network.py."""
    blocks = [create(max_arrivals=1, sequence="InOut"), _route(),
              _station("Dock", pickup_node), _transport("carry", fleet["name"]),
              _station("Store", drop_node), dispose()]
    links = [{"source": "create_1", "target": "route_1"},
             {"source": "route_1", "target": "dispose_1"},
             {"source": "Dock", "target": "carry"},
             {"source": "carry", "target": "dispose_1"},
             {"source": "Store", "target": "dispose_1"}]
    return layout(blocks, links, network=network, fleets=[fleet],
                  sequences=[{"name": "InOut", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


# -- 1: one crane, hand-timed trip -------------------------------------------

# A(0,0) home, B(30,0) pick-up, C(30,40) drop. Bogus 999 segment lengths on both
# legs: if an implementation forgot to branch on `motion` and fell back to
# ordinary network-length/speed travel, it would answer something wildly
# different from the hand-computed Chebyshev time below, not silently agree.
NETWORK_1 = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 30, "y": 0},
                       {"name": "C", "x": 30, "y": 40}],
             "segments": [{"from": "A", "to": "B", "length": 999.0, "two_way": True},
                          {"from": "B", "to": "C", "length": 999.0, "two_way": True}]}


def test_one_crane_trip_is_chebyshev_legs_plus_hoist():
    """speed_x=3, speed_y=2, hoist=1, load/unload=0:
    empty A->B: dx=30, dy=0 -> max(10, 0) + 1 = 11
    loaded B->C: dx=0, dy=40 -> max(0, 20) + 1 = 21
    entity time in system = 11 + 21 = 32."""
    fleet = _crane("Crane", home="A", speed_x=3.0, speed_y=2.0, hoist_time=1.0)
    model = _gantry_model(fleet, NETWORK_1, pickup_node="B", drop_node="C")
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(32.0)


# -- 2: diagonal move, Chebyshev not Euclidean --------------------------------

NETWORK_2 = {"nodes": [{"name": "Home", "x": 0, "y": 0}, {"name": "DestX", "x": 12, "y": 2},
                       {"name": "DestY", "x": 2, "y": 8}],
             "segments": [{"from": "Home", "to": "DestX", "length": 999.0, "two_way": True},
                          {"from": "Home", "to": "DestY", "length": 999.0, "two_way": True}]}


def test_diagonal_move_uses_chebyshev_not_euclidean():
    """speed_x=4, speed_y=1, hoist=0, crane home == pick-up (empty leg 0), so the
    entity's whole time in system is the loaded leg's Chebyshev time:
    x-dominant DestX(12,2): max(12/4, 2/1) = max(3, 2) = 3
    y-dominant DestY(2,8):  max(2/4, 8/1) = max(0.5, 8) = 8
    A Euclidean reading would give sqrt(148)=~12.2 and sqrt(68)=~8.25 -- neither
    matches either hand-computed number, so the distinction is real."""
    fleet_x = _crane("Crane", home="Home", speed_x=4.0, speed_y=1.0)
    model_x = _gantry_model(fleet_x, NETWORK_2, pickup_node="Home", drop_node="DestX")
    row_x = api.run_model(model_x, replications=1)["entity_summary"].iloc[0]
    assert row_x["Avg Time"] == pytest.approx(3.0)

    fleet_y = _crane("Crane", home="Home", speed_x=4.0, speed_y=1.0)
    model_y = _gantry_model(fleet_y, NETWORK_2, pickup_node="Home", drop_node="DestY")
    row_y = api.run_model(model_y, replications=1)["entity_summary"].iloc[0]
    assert row_y["Avg Time"] == pytest.approx(8.0)


# -- 3: no segments needed -----------------------------------------------------

NETWORK_3 = {"nodes": [{"name": "P", "x": 0, "y": 0}, {"name": "Q", "x": 5, "y": 5}],
             "segments": []}


def test_gantry_needs_no_segments_between_its_nodes():
    """P(0,0) is both the crane's home and the pick-up, Q(5,5) the drop -- NO
    segment anywhere in the network. speed_x=speed_y=1, hoist=0: empty leg 0,
    loaded leg max(5, 5) = 5. Reading 1 above: this must validate and run even
    though today's E_NETWORK_UNREACHABLE walk would otherwise refuse a Dock->Store
    sequence step with no path between them."""
    fleet = _crane("Crane", home="P", speed_x=1.0, speed_y=1.0)
    model = _gantry_model(fleet, NETWORK_3, pickup_node="P", drop_node="Q")
    assert not schema.validate_model(model)
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(5.0)


# -- 4: nearest select ranks by gantry time, not Euclidean distance -----------

NETWORK_4 = {"nodes": [{"name": "C1home", "x": 0, "y": 3}, {"name": "C2home", "x": 4, "y": 0},
                       {"name": "Station", "x": 0, "y": 0}],
             "segments": []}


def test_nearest_select_uses_gantry_travel_time_not_euclidean_distance():
    """speed_x=100 (fast axis), speed_y=1 (slow axis), hoist=0. Pick-up and drop
    are the SAME node (Station), so only the empty leg -- the one `select`
    decides on -- matters.

    C1 at (0,3): pure y move, dy=3 -> Euclidean 3, gantry max(0/100, 3/1) = 3.
    C2 at (4,0): pure x move, dx=4 -> Euclidean 4, gantry max(4/100, 0/1) = 0.04.

    Euclidean ranks C1 nearer (3 < 4); gantry travel time ranks C2 nearer
    (0.04 < 3) -- the rankings disagree, so `select: "nearest"` must pick C2
    (unit index 1) for this to be testing the right thing."""
    fleet = _crane("Cranes", home="C1home", speed_x=100.0, speed_y=1.0,
                   units=[{"name": "C1", "home": "C1home"}, {"name": "C2", "home": "C2home"}])
    model = _gantry_model(fleet, NETWORK_4, pickup_node="Station", drop_node="Station")
    recorder = TraceRecorder()
    api.run_model(model, replications=1, trace=recorder)
    empty_moves = [e for e in recorder.events if e.kind == "mover_moved" and e.state == "empty"]
    assert len(empty_moves) == 1
    assert empty_moves[0].entity_id == 1          # C2, the gantry-nearest unit


# -- 5: playback's direct case -------------------------------------------------

def test_point_on_path_direct_case_interpolates_the_two_node_coordinates():
    """Reading 2 above, checked in isolation: a 2-node path with no segment
    between its nodes -- P(0,0), Q(5,5) -- interpolates on the straight line
    between them. Halfway is their midpoint, (2.5, 2.5)."""
    network = build_network(NETWORK_3)
    assert point_on_path(network, ("P", "Q"), 0.5) == pytest.approx((2.5, 2.5))
    assert point_on_path(network, ("P", "Q"), 0.0) == pytest.approx((0.0, 0.0))
    assert point_on_path(network, ("P", "Q"), 1.0) == pytest.approx((5.0, 5.0))


def test_playback_mover_position_at_half_the_loaded_leg_is_the_midpoint():
    """Same P->Q crane as case 3 (`Timeline`/`state_at` need no Qt: test_playback.py
    imports them with no `qapp` fixture), traced end to end. At half the loaded
    leg's duration the mover's progress must read 0.5, and feeding that through
    `point_on_path` must land on the P/Q midpoint (2.5, 2.5) -- exactly the direct
    case above, now driven by a real run's trace instead of asserted by hand."""
    fleet = _crane("Crane", home="P", speed_x=1.0, speed_y=1.0)
    model = _gantry_model(fleet, NETWORK_3, pickup_node="P", drop_node="Q")
    recorder = TraceRecorder()
    api.run_model(model, replications=1, trace=recorder)

    loaded = [e for e in recorder.events if e.kind == "mover_moved" and e.state == "loaded"]
    assert len(loaded) == 1
    move = loaded[0]
    assert move.path == ("P", "Q")
    assert move.duration == pytest.approx(5.0)      # max(5, 5), hoist 0

    timeline = Timeline(recorder.events)
    state = timeline.state_at(move.t + move.duration / 2.0)
    path, progress, _mover_state = state["movers"][("Crane", 0)]
    assert progress == pytest.approx(0.5)

    network = build_network(NETWORK_3)
    assert point_on_path(network, path, progress) == pytest.approx((2.5, 2.5))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
