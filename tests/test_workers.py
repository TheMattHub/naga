"""Workers: a fleet of kind "worker" summoned by SEIZE/PROCESS (PHASE6 W3-M3, D114).

Every timing is hand-computed geometry: A (0,0) -- B (30,40) is 50 long, a
worker walks at 10, so A -> B is exactly 5.
"""
import pytest

from nagasim import api
from nagasim.core import schema
from nagasim.core.trace import TraceRecorder

from conftest import chain, create, delay, dispose, layout, process

NET = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 30, "y": 40}],
       "segments": [{"from": "A", "to": "B"}]}
SIM = {"time_limit": 1000, "warmup": 0, "replications": 1}


def workers(units=None, **spec):
    base = {"name": "W", "kind": "worker", "speed": 10.0, "home": "A",
            "units": units or [{"name": "Ann", "skills": ["weld"]}]}
    base.update(spec)
    return base


def model(blocks, links, fleet, **sections):
    return layout(blocks, links, network=NET, fleets=[fleet], simulation=SIM, **sections)


def times(results, e_type="DEFAULT"):
    rows = results["entity_summary"]
    row = rows[rows["Entity Type"] == e_type].iloc[0] if "Entity Type" in rows else rows.iloc[0]
    return row["Min Time"], row["Avg Time"], row["Max Time"]


def codes(doc):
    return {p.code for p in schema.validate_layout_structured(doc)}


# Walk + service, park stay, skill pools and skip-over are the oracle's
# (test_workers_oracle.py cases 1-5).
def test_park_home_walks_every_job_and_counts_the_distance():
    """One worker at A, the station at B: parking home, each of three jobs is
    walk 5 + service 2, and each trip drives 50 out and 50 back."""
    blocks = [create(max_arrivals=3, dist_params={"value": 100.0}),
              process(resource="", value=2.0, worker_fleet="W", node="B"), dispose()]
    results = api.run_model(model(blocks, chain(*blocks), workers(park="home")), replications=1)
    assert times(results) == pytest.approx((7.0, 7.0, 7.0))
    row = results["fleet_summary"].iloc[0]
    assert row["Trips"] == 3
    assert row["Distance Empty"] == pytest.approx(300.0)
    assert row["Distance Loaded"] == 0.0


def test_a_shift_that_takes_the_skilled_unit_holds_only_its_jobs():
    """Capacity 2 on [0,1), 1 on [1,10), 2 after. At 1 both are idle since 0,
    so the shift takes the lowest unit, Ann (weld). A weld job (3) at 2 waits
    for her return at 10: out at 13, time 11 -- the idle Bob never takes it.
    Paint jobs (2) at 2 and 5 go to Bob at once: time 2 each."""
    weld_src = create("c_weld", max_arrivals=1, start_time=2, e_type="Weld")
    paint_src = create("c_paint", max_arrivals=2, start_time=2, dist_params={"value": 3.0},
                       e_type="Paint")
    weld = process("p_weld", resource="", value=3.0, worker_fleet="W", worker_skill="weld",
                   node="A")
    paint = process("p_paint", resource="", value=2.0, worker_fleet="W", worker_skill="paint",
                    node="A")
    blocks = [weld_src, paint_src, weld, paint, dispose()]
    links = [{"source": "c_weld", "target": "p_weld"}, {"source": "c_paint", "target": "p_paint"},
             {"source": "p_weld", "target": "dispose_1"},
             {"source": "p_paint", "target": "dispose_1"}]
    fleet = workers([{"name": "Ann", "skills": ["weld"]}, {"name": "Bob", "skills": ["paint"]}],
                    capacity_schedule="shift")
    shift = {"name": "shift", "kind": "capacity", "points": [[1, 2], [9, 1], [990, 2]]}
    results = api.run_model(model(blocks, links, fleet, schedules=[shift]), replications=1)
    assert times(results, "WELD") == pytest.approx((11.0, 11.0, 11.0))
    assert times(results, "PAINT") == pytest.approx((2.0, 2.0, 2.0))


def test_the_machine_is_seized_first_so_no_worker_waits_for_it():
    """Machine M (capacity 1), two jobs at 0, service 4, one worker already at
    B. The second job waits for M, not holding the worker: request waits 0."""
    blocks = [create(max_arrivals=1, entities_per_arrival=2),
              process(resource="M", value=4.0, worker_fleet="W", node="B"), dispose()]
    results = api.run_model(model(blocks, chain(*blocks), workers(home="B"),
                                  resources=[{"name": "M", "capacity": 1}]), replications=1)
    assert times(results) == pytest.approx((4.0, 6.0, 8.0))
    assert results["fleet_summary"].iloc[0]["Max Request Wait"] == 0.0


def test_a_seize_holds_the_worker_until_the_release_names_the_fleet():
    """SEIZE (walk 5, delay 1) -> DELAY 3 -> RELEASE W: the worker is busy 0-9,
    so the second entity (arriving at 0) starts at 9, walk 0: out at 13."""
    seize = {"name": "grab", "description": "grab", "type": "SEIZE", "position": [1, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 1.0},
                        "resource_name": "", "worker_fleet": "W", "node": "B"}}
    release = {"name": "free", "description": "free", "type": "RELEASE", "position": [3, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                          "resource_name": "W"}}
    blocks = [create(max_arrivals=1, entities_per_arrival=2), seize, delay(value=3.0),
              release, dispose()]
    results = api.run_model(model(blocks, chain(*blocks), workers()), replications=1)
    assert times(results) == pytest.approx((9.0, 11.0, 13.0))


def test_the_walk_is_traced_as_an_empty_mover_move():
    blocks = [create(max_arrivals=1), process(resource="", value=2.0, worker_fleet="W", node="B"),
              dispose()]
    recorder = TraceRecorder()
    api.run_model(model(blocks, chain(*blocks), workers()), replications=1, trace=recorder)
    walks = [e for e in recorder.events if e.kind == "mover_moved" and e.state == "empty"]
    assert [(w.path, w.duration) for w in walks] == [(("A", "B"), pytest.approx(5.0))]


def test_validation_codes():
    ok = [create(), process(resource="", worker_fleet="W", worker_skill="weld", node="B"),
          dispose()]
    assert not {c for c in codes(model(ok, chain(*ok), workers())) if c.startswith("E_")}

    def with_params(**params):
        blocks = [create(), process(resource="", **params), dispose()]
        return codes(model(blocks, chain(*blocks), workers()))

    # E_WORKER_NO_NODE and E_WORKER_SKILL_UNKNOWN: oracle case 6.
    assert "E_WORKER_FLEET" in with_params(worker_fleet="Nobody", node="B")
    assert "E_WORKER_PREEMPT" in with_params(worker_fleet="W", node="B", preempt=True)
    vehicles = [create(), process(resource="", worker_fleet="W", node="B"), dispose()]
    assert "E_WORKER_FLEET" in codes(model(vehicles, chain(*vehicles), workers(kind="vehicle")))
    twins = workers([{"name": "Ann"}, {"name": "Ann"}])
    assert "E_FLEET_UNITS" in codes(model(ok[:1], [], twins))
    assert "E_FLEET_UNITS" in codes(model(ok[:1], [], workers(count=3)))
    assert "E_FLEET_FIELD" in codes(model(ok[:1], [], workers(kind="robot")))


# -- the worker at a blocked station (D171) ----------------------------------
#
# Parts at 0 and 1 are worked on for 2 by the one worker (everything at A, no
# walk), then go to M2 (20) with no place before it. Part 1 is in M2 2 -> 22,
# so part 2, done at 4, stays at its station until 22. An "Other" job arrives
# at 5 and needs the same worker for 2.

def _block(name, kind, **params):
    return {"name": name, "description": name, "type": kind, "position": [1, 0], "params": params}


def blocked_station(style, mode=None, space=True):
    when = {} if mode is None else {"worker_when_blocked": mode}
    if style == "process":
        station = [process("m1", resource="M1", value=2.0, worker_fleet="W", node="A", **when)]
    else:
        station = [_block("grab", "SEIZE", distribution="Constant", dist_params={"value": 2.0},
                          resource_name="", worker_fleet="W", node="A"),
                   _block("free", "RELEASE", distribution="Constant", dist_params={"value": 0.0},
                          resource_name="W", **when)]
    line = [create(max_arrivals=2), *station, *([_block("gap", "BUFFER", capacity=0)] * space),
            process("slow", "M2", 20.0), dispose()]
    side = [create("c_other", max_arrivals=1, start_time=5, e_type="Other"),
            process("other", resource="", value=2.0, worker_fleet="W", node="A"), dispose("d_other")]
    return model(line + side, chain(*line) + chain(*side), workers(home="A"),
                 resources=[{"name": "M1", "capacity": 1}, {"name": "M2", "capacity": 1}])


@pytest.mark.parametrize("style", ["process", "seize_release"])
@pytest.mark.parametrize("mode, other", [
    (None, 2.0),            # the default is "leaves"
    ("leaves", 2.0),        # the worker is free from 4: the job at 5 is done at 7
    ("stays", 19.0),        # the worker waits with part 2 until 22: done at 24
])
def test_worker_when_blocked_chooses_whether_the_worker_waits_with_the_part(style, mode, other):
    results = api.run_model(blocked_station(style, mode), replications=1)
    assert times(results, "OTHER") == pytest.approx((other, other, other))


@pytest.mark.parametrize("style", ["process", "seize_release"])
def test_worker_when_blocked_changes_nothing_where_nothing_blocks(style):
    leaves, stays = (api.run_model(blocked_station(style, mode, space=False), replications=1)
                     for mode in ("leaves", "stays"))
    assert times(leaves, "OTHER") == pytest.approx((2.0, 2.0, 2.0))
    for frame in ("entity_summary", "resource_summary", "fleet_summary"):
        assert leaves[frame].equals(stays[frame]), frame


def test_an_unknown_worker_when_blocked_is_an_error():
    assert "E_WORKER_WHEN_BLOCKED" in codes(blocked_station("process", "waits"))
    assert "E_WORKER_WHEN_BLOCKED" in codes(blocked_station("seize_release", "waits"))
    assert "E_WORKER_WHEN_BLOCKED" not in codes(blocked_station("seize_release", "stays"))


# -- `$` scoping inside a submodel (PHASE6 6D-W3 follow-up) ------------------
#
# `submodels._rewrite` walks a block's WHOLE params dict blind to the key
# name, so any "$name" value is scoped the same way regardless of which param
# holds it (ARCHITECTURE section 6 "$` SCOPING). `worker_fleet` therefore
# already clones per instance exactly like TRANSPORT's `fleet`
# (test_network.py's test_a_scoped_fleet_clones_per_instance...), and a
# SEIZE/PROCESS `node` is rewritten exactly like STATION's `node` (D69: a
# geometry asset's `"node": "$door"`, test_network_layout.py). No code change
# was needed in core/submodels.py -- both were already generic.

def test_a_scoped_worker_fleet_clones_per_instance_like_a_transport_fleet():
    from nagasim.core import submodels

    definition = {
        "parameters": [{"name": "base", "default": "A", "type": "name"}],
        "scoped": {"fleets": {"W": {"home": {"$arg": "base"}}}},
        "blocks": [
            {"name": "in", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
            process("work", resource="", value=1.0, worker_fleet="$W", node="B"),
            {"name": "out", "type": "SUB_EXIT", "position": [2, 0], "params": {"port": "out"}},
        ],
        "links": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}],
    }
    raw = model([
        {"name": "cell1", "type": "SUBMODEL", "position": [0, 0],
         "params": {"definition": "Cell", "arguments": {"base": "A"}}},
        {"name": "cell2", "type": "SUBMODEL", "position": [1, 0],
         "params": {"definition": "Cell", "arguments": {"base": "B"}}},
    ], [], workers(), submodels={"Cell": definition})
    flat = submodels.flatten(schema.migrate(raw))

    assert {f["name"]: f.get("home") for f in flat["fleets"] if f["name"] != "W"} == {
        "cell1/W": "A", "cell2/W": "B"}
    assert {b["params"]["worker_fleet"] for b in flat["blocks"] if b["type"] == "PROCESS"} == {
        "cell1/W", "cell2/W"}
    # No fleet/worker/node problem: each instance's PROCESS resolves its own
    # cloned fleet, exactly as a TRANSPORT's scoped `fleet` does.
    bad = {p.code for p in schema.validate_model_structured(flat) if p.severity == "error"}
    assert not bad & {"E_WORKER_FLEET", "E_FLEET_UNKNOWN", "E_WORKER_NO_NODE",
                      "E_NETWORK_NODE_UNKNOWN", "E_WORKER_SKILL_UNKNOWN"}


def test_a_scoped_node_on_a_worker_station_is_rewritten_like_a_stations_node():
    """Mirrors D69's `"node": "$door"` for STATION (test_network_layout.py):
    an asset places its own node under `<instance>/door` and a definition
    block binds to it with `$door`; here the block is a worker PROCESS."""
    from nagasim.core import submodels

    definition = {
        "blocks": [
            {"name": "in", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
            process("work", resource="", value=1.0, worker_fleet="W", node="$door"),
            {"name": "out", "type": "SUB_EXIT", "position": [2, 0], "params": {"port": "out"}},
        ],
        "links": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}],
    }
    net = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "cell1/door", "x": 5, "y": 0},
                     {"name": "cell2/door", "x": 10, "y": 0}],
           "segments": [{"from": "A", "to": "cell1/door"}, {"from": "A", "to": "cell2/door"}]}
    raw = layout([
        {"name": "cell1", "type": "SUBMODEL", "position": [0, 0],
         "params": {"definition": "Cell", "arguments": {}}},
        {"name": "cell2", "type": "SUBMODEL", "position": [1, 0],
         "params": {"definition": "Cell", "arguments": {}}},
    ], [], network=net, fleets=[workers(home="A")], simulation=SIM,
        submodels={"Cell": definition})
    flat = submodels.flatten(schema.migrate(raw))

    assert {b["params"]["node"] for b in flat["blocks"] if b["type"] == "PROCESS"} == {
        "cell1/door", "cell2/door"}
    bad = {p.code for p in schema.validate_model_structured(flat) if p.severity == "error"}
    assert not bad & {"E_WORKER_FLEET", "E_FLEET_UNKNOWN", "E_WORKER_NO_NODE",
                      "E_NETWORK_NODE_UNKNOWN", "E_WORKER_SKILL_UNKNOWN"}
