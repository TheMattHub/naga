"""Independent oracle for W3-M4 "Multi-load tours"
(docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md "M4" and "Common rules";
ARCHITECTURE.md 5.21). Every expected number below is computed BY HAND from
the wire format the plan describes -- this file never imports or runs the M4
implementation to derive an expectation (it is written while an independent
agent implements M4 concurrently), so its failures are meaningful once that
implementation lands.

Wire format assumed (plan's own words; the module constants below are the one
place a coordinator reconciling the real implementation's names has to look):

    fleet: {..., "loads": k}                    # default 1 = today's behaviour
    fleet_summary gains a "Loads per Trip" column
    E_FLEET_LOADS: loads is not an integer >= 1

Tour semantics (plan): the lead request holds the vehicle; at dispatch, up to
k-1 further WAITING TRANSPORT requests (in the fleet's own dispatch order)
attach to that vehicle's tour with no resource request of their own -- no
waiting to fill a vehicle. The tour visits pick-ups nearest-neighbour from the
vehicle's position, then drops nearest-neighbour; utilization is vehicle time,
not per-load time.

Every model below pins speed == speed_empty, so a case's arithmetic never
depends on which of the two fields the implementation reads for an unladen
drive. Load and unload are each a Constant 1 (HANDLING = 1.0).

Horizon (DEVLOG #32): every model here is a handful of finite arrivals that
drain naturally well inside a generous `time_limit`, so no keeper
CREATE/DISPOSE pair is needed -- the observed window is the drain time either
way.

The "vehicle busy until t0" cases (1, 2, 4) use a fleet `capacity_schedule`
that holds capacity at 0 for the schedule's first segment (duration T0) and
then 1 for ever -- this is the existing scheduled-capacity mechanism
(ARCHITECTURE, "capacity schedules... work with no mover-specific code" for a
fleet), not a decoy TRANSPORT trip, so it adds no trip of its own to
`fleet_summary` and leaves the vehicle exactly at `home` when it opens up.
Both waiting entities are created at t=0, while capacity is genuinely 0 (no
idle unit to race for), so both are certainly WAITING -- not merely
requested -- when the fleet opens at T0.
"""
import pytest

from nagasim import api
from nagasim.core import schema

from conftest import chain, create, dispose, layout

# -- wire-format constants ---------------------------------------------------

FLEET_FIELD_LOADS = "loads"
FLEET_SUMMARY_LOADS_PER_TRIP = "Loads per Trip"
E_FLEET_LOADS = "E_FLEET_LOADS"

SPEED = 5.0             # == speed_empty everywhere (see module docstring)
HANDLING = 1.0           # load_time == unload_time everywhere
T0 = 10.0                # the schedule's capacity-0 segment: the vehicle is
                         # certainly busy [unavailable] on [0, T0)

# A capacity schedule with ONE real transition (0 -> 1 at T0); the second
# point's own duration is irrelevant (schedules.py: "the last value holds"
# once every declared bound but the last is passed), so it is kept short.
SCHEDULE = {"name": "Shift", "kind": "capacity", "points": [[T0, 0], [1, 1]]}

SIM = {"time_limit": 200, "warmup": 0, "replications": 1}

# -- 1/3/4 geometry: H (home) -- P1 (pick-up) -- D (drop), a straight line --
NETWORK = {"nodes": [{"name": "H", "x": 0, "y": 0}, {"name": "P1", "x": 30, "y": 0},
                     {"name": "D", "x": 50, "y": 0}],
           "segments": [{"from": "H", "to": "P1", "length": 30.0, "two_way": True},
                        {"from": "P1", "to": "D", "length": 20.0, "two_way": True}]}
H_P1 = 30.0 / SPEED     # 6.0
P1_D = 20.0 / SPEED     # 4.0

# -- 2 geometry: H -- P_near -- P_far -- D, a straight line with two pick-ups
NETWORK2 = {"nodes": [{"name": "H", "x": 0, "y": 0}, {"name": "Pn", "x": 10, "y": 0},
                      {"name": "Pf", "x": 40, "y": 0}, {"name": "D", "x": 60, "y": 0}],
           "segments": [{"from": "H", "to": "Pn", "length": 10.0, "two_way": True},
                        {"from": "Pn", "to": "Pf", "length": 30.0, "two_way": True},
                        {"from": "Pf", "to": "D", "length": 20.0, "two_way": True}]}
H_PN = 10.0 / SPEED     # 2.0
PN_PF = 30.0 / SPEED    # 6.0
PF_D = 20.0 / SPEED     # 4.0


def _fleet(loads=1, **overrides):
    base = {"name": "Movers", "count": 1, "speed": SPEED, "speed_empty": SPEED,
            "load_time": str(HANDLING), "unload_time": str(HANDLING), "home": "H",
            "park": "stay", "dispatch": "fifo", "select": "nearest",
            FLEET_FIELD_LOADS: loads}
    base.update(overrides)
    return base


def _station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": {"node": node}}


def _transport(name, fleet_name, destination):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [2, 0],
            "params": {"fleet": fleet_name, "destination": destination, "priority": 0}}


def _two_pickup_model(network, pickup_a, pickup_b, drop, fleet, arrivals_wait=True):
    """Two entities (a_src -> b_src), each via its own pick-up STATION, into a
    shared "carry" TRANSPORT and a shared drop STATION. Both created at t=0.
    `arrivals_wait`: the fleet carries `capacity_schedule="Shift"` so both
    entities are genuinely WAITING when the vehicle opens at T0."""
    a_pickup, b_pickup = _station("PickA", pickup_a), _station("PickB", pickup_b)
    drop_station = _station("Drop", drop)
    carry = _transport("carry", fleet["name"], "Drop")
    dispose_block = dispose()
    blocks = [create(name="a_src", max_arrivals=1, start_time=0), a_pickup,
              create(name="b_src", max_arrivals=1, start_time=0), b_pickup,
              carry, drop_station, dispose_block]
    links = [{"source": "a_src", "target": "PickA"}, {"source": "PickA", "target": "carry"},
             {"source": "b_src", "target": "PickB"}, {"source": "PickB", "target": "carry"},
             {"source": "carry", "target": "Drop"}, {"source": "Drop", "target": "dispose_1"}]
    schedules = [SCHEDULE] if arrivals_wait else []
    return layout(blocks, links, network=network, fleets=[fleet], schedules=schedules,
                  simulation=SIM)


# -- 1: two waiting requests at ONE pick-up, ONE drop ------------------------

def test_two_waiting_requests_at_one_pickup_are_carried_in_one_trip():
    """Fleet `loads=2`, `capacity_schedule="Shift"`: capacity is 0 on [0, T0),
    so entities A and B (both created at t=0, both routed through P1 -> D) are
    genuinely WAITING when the vehicle opens at T0=10, still at home H (it
    never moved while capacity was 0).

    Dispatch: A is first in `fifo` order, B is the one further waiting
    request `loads=2` may attach (k-1=1) -- one tour carries both. Only one
    pick-up node, so the tour is trivial: drive H->P1 empty, LOAD, LOAD
    (sequential; reading: first-loaded is first-unloaded, so the two possible
    orders are symmetric here since both share the same destination and
    handling time), drive P1->D loaded, UNLOAD, UNLOAD.

        T0 (10) -> H->P1 empty (6) -> 16
        load #1 (1) -> 17 ; load #2 (1) -> 18
        P1->D loaded (4) -> 22
        unload #1 (1) -> 23 (delivered) ; unload #2 (1) -> 24 (delivered)

    Both A and B were created at t=0, so their times in system are 23 and 24
    -- one apart, purely from the 1-unit gap between the two unloads, not from
    when either was created.
    """
    fleet = _fleet(loads=2, capacity_schedule="Shift")
    model = _two_pickup_model(NETWORK, "P1", "P1", "D", fleet)
    results = api.run_model(model, replications=1)

    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(23.0)
    assert row["Max Time"] == pytest.approx(24.0)

    totals = results["fleet_summary"].iloc[0]
    assert totals["Trips"] == 1
    assert totals[FLEET_SUMMARY_LOADS_PER_TRIP] == pytest.approx(2.0)
    assert totals["Distance Loaded"] == pytest.approx(20.0)   # one P1->D leg, not two
    assert totals["Distance Empty"] == pytest.approx(30.0)    # one H->P1 leg


# -- 2: two waiting requests at DIFFERENT pick-ups, one drop -----------------

def test_pickups_are_visited_nearest_neighbour_from_the_vehicles_position():
    """Fleet `loads=2` again; pick-up A is at the FAR node Pf (40 from home),
    pick-up B at the NEAR node Pn (10 from home). Both wait out [0, T0) the
    same way as case 1, so the vehicle opens at T0=10 still at H.

    Nearest-neighbour over pick-ups from H picks Pn (10) before Pf (40):

        T0 (10) -> H->Pn empty (2) -> 12 ; load B (1) -> 13
        Pn->Pf empty (6) -> 19 ; load A (1) -> 20
        Pf->D loaded (4) -> 24
        unload B (1) -> 25 (delivered) ; unload A (1) -> 26 (delivered)

    (reading: unload order follows load order, B then A.) Total distance
    driven, by hand: H->Pn (10) + Pn->Pf (30) + Pf->D (20) = 60. The
    empty/loaded split reads "loaded" as carrying at least one entity: the
    vehicle is EMPTY only before its first pick-up (H->Pn, 10) -- once B is
    aboard at Pn it is LOADED for the rest of the tour, Pn->Pf and Pf->D
    (30 + 20 = 50), even though A does not board until Pf.
    """
    fleet = _fleet(loads=2, capacity_schedule="Shift")
    model = _two_pickup_model(NETWORK2, "Pf", "Pn", "D", fleet)
    results = api.run_model(model, replications=1)

    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(25.0)
    assert row["Max Time"] == pytest.approx(26.0)

    totals = results["fleet_summary"].iloc[0]
    assert totals["Trips"] == 1
    assert totals[FLEET_SUMMARY_LOADS_PER_TRIP] == pytest.approx(2.0)
    assert totals["Distance Empty"] == pytest.approx(10.0)    # H->Pn, before any pick-up
    assert totals["Distance Loaded"] == pytest.approx(50.0)   # Pn->Pf->D, carrying B then both


# -- 3: loads=2 but only ONE request ever waits: no waiting to fill ---------

def test_a_single_waiting_request_is_not_held_to_fill_the_vehicle():
    """`loads=2`, no capacity schedule: the vehicle is idle at H from t=0 and
    only ONE entity ever exists, so there is nothing to attach to it -- "no
    waiting to fill" means it must not wait around for a second request that
    is never coming. Solo trip, identical to what a `loads=1` fleet would do:

        H->P1 empty (6) -> 6 ; load (1) -> 7 ; P1->D loaded (4) -> 11 ;
        unload (1) -> 12 (delivered).
    """
    fleet = _fleet(loads=2)   # no capacity_schedule: idle from t=0
    blocks = [create(max_arrivals=1), _station("Pick", "P1"),
              _transport("carry", fleet["name"], "Drop"), _station("Drop", "D"), dispose()]
    links = [{"source": "create_1", "target": "Pick"}, {"source": "Pick", "target": "carry"},
             {"source": "carry", "target": "Drop"}, {"source": "Drop", "target": "dispose_1"}]
    model = layout(blocks, links, network=NETWORK, fleets=[fleet], simulation=SIM)
    results = api.run_model(model, replications=1)

    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(H_P1 + HANDLING + P1_D + HANDLING)   # 12.0

    totals = results["fleet_summary"].iloc[0]
    assert totals["Trips"] == 1
    assert totals[FLEET_SUMMARY_LOADS_PER_TRIP] == pytest.approx(1.0)
    assert totals["Distance Loaded"] == pytest.approx(20.0)
    assert totals["Distance Empty"] == pytest.approx(30.0)


# -- 4: loads=1 regression, same model as case 1 -----------------------------

def test_loads_1_is_todays_behaviour_two_separate_trips():
    """Exactly case 1's model (same two waiting entities, same schedule) but
    `loads=1`: the plan's own "k=1 = today's behaviour". Both A and B are
    still waiting when the fleet opens at T0=10, but with no attaching, the
    vehicle must make the round trip twice:

        T0 (10) -> H->P1 empty (6) -> 16 ; load A (1) -> 17
        P1->D loaded (4) -> 21 ; unload A (1) -> 22 (A delivered, freed at once)
        D->P1 empty (4) -> 26 ; load B (1) -> 27
        P1->D loaded (4) -> 31 ; unload B (1) -> 32 (B delivered)

    A's time in system is 22, B's is 32 -- both created at t=0.
    """
    fleet = _fleet(loads=1, capacity_schedule="Shift")
    model = _two_pickup_model(NETWORK, "P1", "P1", "D", fleet)
    results = api.run_model(model, replications=1)

    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(22.0)
    assert row["Max Time"] == pytest.approx(32.0)

    totals = results["fleet_summary"].iloc[0]
    assert totals["Trips"] == 2
    assert totals[FLEET_SUMMARY_LOADS_PER_TRIP] == pytest.approx(1.0)
    assert totals["Distance Loaded"] == pytest.approx(40.0)    # two P1->D legs
    assert totals["Distance Empty"] == pytest.approx(50.0)     # H->P1 (30) + D->P1 (20)


# -- 5: validation ------------------------------------------------------------

def test_loads_zero_is_a_validation_error():
    """`loads: 0` is not "an integer >= 1" -- E_FLEET_LOADS."""
    fleet = _fleet(loads=0)
    blocks = [create(max_arrivals=1), dispose()]
    model = layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet], simulation=SIM)
    codes = {p.code for p in schema.validate_model_structured(model)}
    assert E_FLEET_LOADS in codes


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
