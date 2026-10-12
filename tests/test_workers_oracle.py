"""Independent oracle for W3-M3 "Workers" (docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md,
ARCHITECTURE.md 5.21). Every expected number below is computed BY HAND from the wire
format the plan describes -- this file never imports or runs the M3 implementation to
derive an expectation, so its failures are meaningful once that implementation lands.

Wire format assumed (plan's own words), kept as module constants so a coordinator
reconciling the real implementation's names has one place to look:

    fleet: {"name", "kind": "worker", "units": [{"name", "skills": [...], "home"}],
            "speed", "speed_empty", "park": "stay"|"home", "dispatch", "select"}
    PROCESS/SEIZE: existing params plus "worker_fleet", "worker_skill" (optional),
                   "node" (required when worker_fleet is set).

Run semantics (plan): a PROCESS seizes its own resource first (if any), then requests
a qualified worker; the worker walks EMPTY along the shortest network path to the
station's node, is held for the service, then parks per `park`. `park: "stay"` leaves
the worker at the station node; `park: "home"` releases it the instant service ends and
sends it home on its own interruptible process (D68 T7 precedent for vehicles) -- so the
return walk is not part of the seize-to-release busy window.

Every model below pins `speed == speed_empty` so a case's arithmetic does not depend on
which of the two fields the implementation reads for an unladen walk.

Horizon (DEVLOG #32): a KPI is computed over `time_limit` only if events remain pending
past it, otherwise over the drain time. Cases 1-3 and 5 let the model drain naturally
(few, finite arrivals) and read totals that do not depend on which window was used.
Case 4 keeps arrivals unlimited (`max_arrivals: 0`) past its `time_limit`, which is
therefore the exact horizon its throughput count is computed over.
"""
import pytest

from nagasim import api
from nagasim.core import schema

from conftest import chain, create, dispose, layout, process

# -- wire-format constants ---------------------------------------------------

FLEET_KIND_WORKER = "worker"
PARAM_NODE = "node"
PARAM_WORKER_FLEET = "worker_fleet"
PARAM_WORKER_SKILL = "worker_skill"
PARK_STAY = "stay"
PARK_HOME = "home"
E_WORKER_SKILL_UNKNOWN = "E_WORKER_SKILL_UNKNOWN"
E_WORKER_NO_NODE = "E_WORKER_NO_NODE"

# H (worker home) <-> S (station), a single 40 m leg -- the only geometry cases 1-4
# need. Case 5 puts every station AT H (distance 0), so no further geometry is needed.
NETWORK = {"nodes": [{"name": "H", "x": 0, "y": 0}, {"name": "S", "x": 40, "y": 0}],
           "segments": [{"from": "H", "to": "S", "length": 40.0, "two_way": True}]}

SPEED = 5.0            # == speed_empty everywhere (see module docstring)
WALK = 40.0 / SPEED    # 8.0: the H<->S leg
SERVICE = 3.0          # D, the station's own service time


def _fleet(units, **overrides):
    base = {"name": "Techs", "kind": FLEET_KIND_WORKER, "units": units,
            "speed": SPEED, "speed_empty": SPEED, "park": PARK_STAY,
            "dispatch": "nearest", "select": "nearest"}
    base.update(overrides)
    return base


def _unit(name, skills, home="H"):
    return {"name": name, "skills": list(skills), "home": home}


def _wproc(name, value, node, skill, fleet_name="Techs", **extra):
    """A worker-served station: no resource of its own (`resource=""`), one
    Constant service time, attached to `node` and restricted to `skill`."""
    params = {PARAM_NODE: node, PARAM_WORKER_FLEET: fleet_name, PARAM_WORKER_SKILL: skill}
    params.update(extra)
    return process(name=name, resource="", value=value, **params)


# -- 1: one worker, one trip -------------------------------------------------

def test_one_worker_one_trip_is_the_walk_plus_the_service():
    """W1 starts idle at home H; the only entity's station is S, 40 m away at
    speed 5 -> walk 8. Service 3. Time in system = 8 + 3 = 11 exactly."""
    fleet = _fleet([_unit("W1", ["weld"], home="H")])
    blocks = [create(max_arrivals=1),
              _wproc("weld", SERVICE, "S", "weld"),
              dispose()]
    model = layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet],
                   simulation={"time_limit": 50, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(WALK + SERVICE)


# -- 2: park stay -------------------------------------------------------------

def test_park_stay_serves_a_later_entity_at_the_same_station_with_no_second_walk():
    """`park: "stay"` leaves W1 AT the station after entity 1 (out at 11).
    Entity 2 arrives at t=20, long after W1 is idle at S: distance 0, so its
    time in system is the service alone, 3 -- not 11 again."""
    fleet = _fleet([_unit("W1", ["weld"], home="H")], park=PARK_STAY)
    blocks = [create(max_arrivals=2, dist_params={"value": 20.0}),
              _wproc("weld", SERVICE, "S", "weld"),
              dispose()]
    model = layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet],
                   simulation={"time_limit": 50, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(SERVICE)          # entity 2: no walk
    assert row["Max Time"] == pytest.approx(WALK + SERVICE)   # entity 1: full trip


# -- 3: park home --------------------------------------------------------------

def test_park_home_return_walk_is_not_busy_time():
    """D68 T7 applies to workers exactly as it does to vehicles: the worker is
    freed the instant service ends and drives home on its own process, so only
    the OUTBOUND walk plus the service count as seized/busy time (11), not the
    inbound 8 as well. With one entity and nothing else in the model, the run
    drains at t = 11 (busy) + 8 (walk home) = 19, so
    Usage % = 100 * 11 / 19.
    """
    fleet = _fleet([_unit("W1", ["weld"], home="H")], park=PARK_HOME)
    blocks = [create(max_arrivals=1),
              _wproc("weld", SERVICE, "S", "weld"),
              dispose()]
    model = layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet],
                   simulation={"time_limit": 100, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    row = results["resource_summary"].set_index("Resource").loc["Techs"]
    assert row["Usage % (snapshot)"] == pytest.approx(100.0 * (WALK + SERVICE) / (WALK + SERVICE + WALK),
                                                       abs=0.05)


# -- 4: skills, saturated ------------------------------------------------------

def test_a_skill_restricted_pool_behaves_as_one_server():
    """Two workers, only W1 has "weld"; W2 ("paint") is idle the whole time and
    must never be drawn into weld work. Arrivals are unlimited with a 0.1 gap
    (<< the 3-unit service), so the weld station is saturated from t=0: W1
    walks 8 to serve entity 1 (done at 11), then -- park stay, next request
    already queued -- serves back to back with NO further walk, one every 3:
    entities finish at 11, 14, 17, 20, 23. A window just past the fifth (23.5,
    before the sixth at 26) must show exactly 5 completed. If skill filtering
    ever let W2 help, the pool would clear roughly twice as fast and this exact
    count would fail.
    """
    fleet = _fleet([_unit("W1", ["weld"], home="H"), _unit("W2", ["paint"], home="H")])
    blocks = [create(max_arrivals=0, dist_params={"value": 0.1}),
              _wproc("weld", SERVICE, "S", "weld"),
              dispose()]
    model = layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet],
                   simulation={"time_limit": 23.5, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Total Completed"] == 5
    assert row["Total Created"] > 5          # the queue is still growing: truly saturated


# -- 5: skip-over ---------------------------------------------------------------

def test_a_waiting_weld_request_does_not_block_a_later_paint_request():
    """W1 ("weld") is tied up for 100 units by an entity created at t=0; a
    second weld request at t=1 has to wait for W1 -- it is deliberately left
    waiting. W2 ("paint") is idle throughout. A paint request at t=2 must be
    served AT ONCE by W2 (0 m away, service 5): time in system 5, not ~103,
    which is what it would be if the queued weld request were allowed to block
    it (a single combined FIFO queue instead of per-skill availability).
    """
    fleet = _fleet([_unit("W1", ["weld"], home="H"), _unit("W2", ["paint"], home="H")])
    blocks = [
        create(name="a_src", max_arrivals=1, start_time=0, e_type="WeldLong"),
        _wproc("weld_a", 100.0, "H", "weld"),
        create(name="b_src", max_arrivals=1, start_time=1, e_type="WeldWait"),
        _wproc("weld_b", 5.0, "H", "weld"),
        create(name="c_src", max_arrivals=1, start_time=2, e_type="Paint"),
        _wproc("paint_c", 5.0, "H", "paint"),
        dispose(),
    ]
    links = (chain(blocks[0], blocks[1], blocks[-1])
            + chain(blocks[2], blocks[3], blocks[-1])
            + chain(blocks[4], blocks[5], blocks[-1]))
    model = layout(blocks, links, network=NETWORK, fleets=[fleet],
                   simulation={"time_limit": 150, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    entities = results["entity_summary"].set_index("Entity Type")
    # `Entity.e_type` upper-cases whatever it is given (DEVLOG #87), unrelated to M3.
    assert entities.loc["PAINT", "Avg Time"] == pytest.approx(5.0)


# -- 6: validation ---------------------------------------------------------------

def _worker_model(process_extra):
    fleet = _fleet([_unit("W1", ["weld"], home="H")])
    blocks = [create(max_arrivals=1),
              process(name="p1", resource="", value=1.0, worker_fleet="Techs", **process_extra),
              dispose()]
    return layout(blocks, chain(*blocks), network=NETWORK, fleets=[fleet],
                  simulation={"time_limit": 20, "warmup": 0, "replications": 1})


def test_an_unknown_worker_skill_is_a_validation_error():
    """The fleet's only unit has "weld"; asking for "paint" names a skill no
    unit in the fleet has."""
    model = _worker_model({PARAM_NODE: "S", PARAM_WORKER_SKILL: "paint"})
    codes = {p.code for p in schema.validate_model_structured(model)}
    assert E_WORKER_SKILL_UNKNOWN in codes


def test_a_worker_fleet_without_a_node_is_a_validation_error():
    """`worker_fleet` with no `node` param: the worker has nowhere to walk to."""
    model = _worker_model({PARAM_WORKER_SKILL: "weld"})
    codes = {p.code for p in schema.validate_model_structured(model)}
    assert E_WORKER_NO_NODE in codes


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
