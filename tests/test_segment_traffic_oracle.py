"""Independent oracle for Phase 6D W3 M5 (Segment traffic),
docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md ("M5 Segment traffic" and
"Common rules") and ARCHITECTURE.md 5.21 (network, segments, fleets,
TRANSPORT, the W_SEGMENT_CAPACITY_IGNORED ceiling this milestone retires).

Every expected number below is derived BY HAND from the spec text, never by
running the implementation (M5 is being built by a concurrent agent, and
other agents are touching `core/movers.py` right now for the unrelated M4
milestone): failures naming a missing attribute/column, a numeric mismatch or
a raise that never happens are expected until M5 lands -- and a mismatch may
equally be this oracle's own wire-format guess, same as the M1/M2/M6
precedent (DEVLOG 2026-09-25 rows; the coordinator reconciles).

Spec semantics traced here (plan "M5 Segment traffic"): `Segment.capacity`
(int) is enforced for fleet travel; a mover reserves its NEXT segment before
leaving the CURRENT one and releases the current on entering the next (zone
control -- while waiting for the next segment it stays at the end of the
current, still holding it); each capacity-limited segment becomes a
`simpy.Resource` of that capacity (FIFO); a cycle in the wait-for graph ends
the run with an error naming the chain (as D78's blocking deadlock, section
5.22); validation warns `W_SEGMENT_DEADLOCK_RISK` for a two-way capacity-1
segment used by a fleet with more than one unit; `W_SEGMENT_CAPACITY_IGNORED`
(the old "recorded but not enforced" warning, `core/network.py`) is retired;
ROUTE `travel = "network"` (entity walking, no vehicle) stays unconstrained;
`segment_summary` (`core/outputs.py::summarize_segments`, columns Run,
Segment, Length, Traversals, Distance Driven today) gains "Wait Time" and
"Max Occupancy".

Wire format taken from `tests/test_network.py` (`NETWORK`, `station`,
`route`, `transport`, `fleet`, `_transport_model`, `_walk_model` -- a
`network.segments[i]` entry is `{"from", "to", "length", "two_way", "speed",
"capacity"}`; `Segment.name` is `f"{a}->{b}"` one-way or `f"{a}<->{b}"`
two-way, `core/network.py::Segment.name`) and `tests/test_conveyor_systems_oracle.py`
(module shape, docstring style, per-case hand traces).

Horizon (DEVLOG #32): KPIs use the time limit only when events remain
pending past it, else the drain time. Not needed here: every case below
either drains on its own (cases 1, 2, 4, 5) or is expected to raise before
the time limit is reached (case 3), so no keeper CREATE/DISPOSE pair is
required.
"""
import pytest

from conftest import create, dispose, layout

from nagasim import api
from nagasim.core import schema

# -- wire names (spec text, not yet in the codebase) -------------------------
W_SEGMENT_DEADLOCK_RISK = "W_SEGMENT_DEADLOCK_RISK"
W_SEGMENT_CAPACITY_IGNORED = "W_SEGMENT_CAPACITY_IGNORED"     # must be retired everywhere
SEGMENT_WAIT_COL = "Wait Time"
SEGMENT_OCCUPANCY_COL = "Max Occupancy"


def station(name, node, position=(2, 0), **params):
    return {"name": name, "description": name, "type": "STATION",
            "position": list(position), "params": {"node": node, **params}}


def route(name="route_1", travel="time", value=0.0, speed="", position=(1, 0)):
    return {"name": name, "description": name, "type": "ROUTE", "position": list(position),
            "params": {"distribution": "Constant", "dist_params": {"value": value},
                       "travel": travel, "speed": speed}}


def transport(name, fleet_name, destination="", position=(3, 0), priority=0):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": list(position),
            "params": {"fleet": fleet_name, "destination": destination, "priority": priority}}


def fleet(name, **spec):
    base = {"name": name, "count": 1, "speed": 5.0, "speed_empty": 5.0,
            "load_time": "0", "unload_time": "0", "home": "dock"}
    base.update(spec)
    return base


def segment_row(results, name):
    return results["segment_summary"].set_index("Segment").loc[name]


# -- Cases 1 and 2: one-way capacity-limited middle segment ------------------

def _corridor_model(capacity, gap=1.0, length=10.0, speed=5.0, limit=20):
    """dock --(capacity-limited, one-way)-- store. Two vehicles of the same
    fleet, both homed at dock (so no empty travel), each carrying one entity
    straight across. `load_time`/`unload_time` are 0 so every number below is
    pure travel/wait arithmetic."""
    net = {"nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "store", "x": length, "y": 0}],
           "segments": [{"from": "dock", "to": "store", "length": length, "two_way": False,
                        "capacity": capacity}]}
    fl = fleet("Forklifts", count=2, speed=speed, speed_empty=speed, home="dock")
    return layout(
        [create(max_arrivals=2, dist_params={"value": gap}, sequence="InOut"),
         route(), station("Dock", "dock"), transport("carry", "Forklifts"),
         station("Store", "store", position=(4, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "carry"},
         {"source": "carry", "target": "dispose_1"},
         {"source": "Store", "target": "dispose_1"}],
        network=net, fleets=[fl],
        sequences=[{"name": "InOut", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
        simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def test_a_capacity_one_segment_makes_the_second_vehicle_wait_for_the_first_to_clear():
    """Both vehicles start at dock (no empty travel). Entity 1 (t=0) boards at
    once and crosses the 10 m segment at speed 5 in 2, holding it 0..2.
    Entity 2 (t=1) boards its own idle vehicle at once too, but the segment
    (capacity 1) is still held: it waits from 1 until the first vehicle
    clears at 2 -- exactly the first's REMAINING traversal (2 - 1 = 1) at the
    instant it asks. Total Wait Time on the segment is that one wait (1); it
    is never occupied by more than one vehicle, so Max Occupancy is 1."""
    results = api.run_model(_corridor_model(capacity=1), replications=1)
    row = segment_row(results, "dock->store")
    assert row[SEGMENT_WAIT_COL] == pytest.approx(1.0)
    assert row[SEGMENT_OCCUPANCY_COL] == 1

    entities = results["entity_log"].sort_values("Created At")
    assert entities.iloc[0]["Exited At"] == pytest.approx(2.0)     # pure travel, no wait
    assert entities.iloc[1]["Exited At"] == pytest.approx(4.0)     # 1 created + 1 wait + 2 travel


def test_a_capacity_two_segment_lets_both_vehicles_cross_with_no_wait():
    """Identical model, capacity 2: entity 2's vehicle finds room the instant
    it asks (1 of 2 places taken), so it never waits, and for the one instant
    both vehicles are on the segment together (1..2) occupancy peaks at 2."""
    results = api.run_model(_corridor_model(capacity=2), replications=1)
    row = segment_row(results, "dock->store")
    assert row[SEGMENT_WAIT_COL] == pytest.approx(0.0)
    assert row[SEGMENT_OCCUPANCY_COL] == 2

    entities = results["entity_log"].sort_values("Created At")
    assert entities.iloc[0]["Exited At"] == pytest.approx(2.0)
    assert entities.iloc[1]["Exited At"] == pytest.approx(3.0)     # 1 created + 2 travel, no wait


# -- Case 3: head-on deadlock on a two-way capacity-1 corridor ---------------

def _deadlock_model(limit=20):
    """Left --(approach_left, two-way, capacity 1)-- L --(corridor, two-way,
    capacity 1)-- R --(approach_right, two-way, capacity 1)-- Right. Every
    segment is two-way because both directions are really used: FleetL's full
    path (Left->L->R->Right) needs approach_right in the R->Right direction,
    and FleetR's (Right->R->L->Left) needs it Right->R -- one-way segments
    here would just be an unreachable-path validation error, not a deadlock.

    Two single-vehicle fleets, one homed at each end, each carrying one
    entity to the opposite end, both created at t=0 with equal speeds: by
    symmetry both vehicles reach the corridor's own ends (L and R) at the
    same instant, each still holding its own approach segment (zone control:
    the next segment -- the corridor -- must be reserved before the current
    one is released). Only one can take the corridor; say it is FleetL's.
    That vehicle crosses, then needs approach_right (its own next segment) to
    finish the trip -- but approach_right is still held by FleetR's vehicle,
    which is itself waiting for the corridor FleetL's vehicle now holds. Each
    holds what the other needs: a two-cycle wait-for graph, so the run must
    raise rather than hang or silently report a quiet system (as D78's own
    closed-buffer-loop check does, section 5.22). The outcome is symmetric in
    whichever vehicle happens to win the corridor race, so no tie-break rule
    needs to be assumed."""
    net = {"nodes": [{"name": "Left", "x": 0, "y": 0}, {"name": "L", "x": 10, "y": 0},
                     {"name": "R", "x": 20, "y": 0}, {"name": "Right", "x": 30, "y": 0}],
           "segments": [{"from": "Left", "to": "L", "length": 10.0, "two_way": True,
                        "capacity": 1},
                        {"from": "L", "to": "R", "length": 10.0, "two_way": True, "capacity": 1},
                        {"from": "R", "to": "Right", "length": 10.0, "two_way": True,
                         "capacity": 1}]}
    fleets = [fleet("FleetL", home="Left"), fleet("FleetR", home="Right")]
    blocks = [
        create(name="create_L", start_time=0, max_arrivals=1, sequence="LeftToRight"),
        create(name="create_R", start_time=0, max_arrivals=1, sequence="RightToLeft"),
        route(name="route_L", position=(1, 0)), route(name="route_R", position=(1, 1)),
        station("StationLeft", "Left", position=(2, 0)),
        station("StationRight", "Right", position=(2, 1)),
        transport("carryL", "FleetL", position=(3, 0)),
        transport("carryR", "FleetR", position=(3, 1)),
        dispose(),
    ]
    links = [{"source": "create_L", "target": "route_L"},
             {"source": "route_L", "target": "dispose_1"},
             {"source": "create_R", "target": "route_R"},
             {"source": "route_R", "target": "dispose_1"},
             {"source": "StationLeft", "target": "carryL"},
             {"source": "StationRight", "target": "carryR"},
             {"source": "carryL", "target": "dispose_1"},
             {"source": "carryR", "target": "dispose_1"}]
    return layout(
        blocks, links, network=net, fleets=fleets,
        sequences=[{"name": "LeftToRight",
                    "steps": [{"station": "StationLeft"}, {"station": "StationRight"}]},
                   {"name": "RightToLeft",
                    "steps": [{"station": "StationRight"}, {"station": "StationLeft"}]}],
        simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def test_two_vehicles_head_on_on_a_two_way_capacity_one_corridor_raise_a_deadlock_error():
    with pytest.raises(Exception, match=r"(?i)deadlock"):
        api.run_model(_deadlock_model(), replications=1)


# -- Case 4: static validation ------------------------------------------------

def _deadlock_risk_model():
    """A -- (two-way, capacity 1) -- B, one fleet of TWO units crossing it:
    the shape the milestone's static check names directly."""
    net = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0}],
           "segments": [{"from": "A", "to": "B", "length": 10.0, "two_way": True,
                        "capacity": 1}]}
    fl = fleet("Movers", count=2, home="A")
    return layout(
        [create(max_arrivals=1, sequence="AB"), route(), station("StationA", "A"),
         transport("carry", "Movers"), station("StationB", "B", position=(4, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "StationA", "target": "carry"},
         {"source": "carry", "target": "dispose_1"},
         {"source": "StationB", "target": "dispose_1"}],
        network=net, fleets=[fl],
        sequences=[{"name": "AB", "steps": [{"station": "StationA"}, {"station": "StationB"}]}],
        simulation={"time_limit": 20, "warmup": 0, "replications": 1})


def test_a_two_way_capacity_one_segment_with_a_multi_unit_fleet_warns_of_deadlock_risk():
    codes = {p.code for p in schema.validate_model_structured(_deadlock_risk_model())}
    assert W_SEGMENT_DEADLOCK_RISK in codes


def test_w_segment_capacity_ignored_is_retired_everywhere():
    """The old ceiling warning must be gone, on every model that declares a
    segment capacity -- both the at-risk one above and the plain one-way
    capacity-1 model from cases 1/2."""
    for model in (_deadlock_risk_model(), _corridor_model(capacity=1)):
        codes = {p.code for p in schema.validate_model_structured(model)}
        assert W_SEGMENT_CAPACITY_IGNORED not in codes


# -- Case 5: entity walking (ROUTE travel="network") ignores capacity --------

def _walk_capacity_model(capacity, speed=2.0, length=50.0, limit=100):
    """dock --(capacity-limited, two-way)-- store, two entities born in the
    SAME arrival (`entities_per_arrival=2`) so they are exactly simultaneous,
    walking (no vehicle) straight across. If capacity were (wrongly) enforced
    on walkers, the second would be delayed behind the first; both must take
    exactly length / speed."""
    net = {"nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "store", "x": length, "y": 0}],
           "segments": [{"from": "dock", "to": "store", "length": length, "two_way": True,
                        "capacity": capacity}]}
    return layout(
        [create(max_arrivals=1, entities_per_arrival=2, sequence="Walk"),
         route(travel="network", speed=str(speed)), station("Dock", "dock"),
         station("Store", "store", position=(4, 0)),
         route(name="route_2", travel="network", speed=str(speed), position=(5, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "route_1"},
         {"source": "Store", "target": "route_2"},
         {"source": "route_2", "target": "dispose_1"}],
        network=net,
        sequences=[{"name": "Walk", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
        simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def test_network_walking_entities_ignore_segment_capacity():
    results = api.run_model(_walk_capacity_model(capacity=1), replications=1)
    expected = 50.0 / 2.0    # length / speed, unaffected by capacity=1
    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(expected)
    assert row["Max Time"] == pytest.approx(expected)
    assert row["Avg Time"] == pytest.approx(expected)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
