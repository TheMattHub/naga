"""Belt-to-belt back-pressure (PHASE6 W3 M6, ARCHITECTURE D115).

A belt whose transparent walk reaches another belt blocks after service like a
station (the D78 token): its end part stays on it, holding its cell, until the
downstream belt physically takes it. Every number here is hand-computed; all
belts run at speed 1, so a length is also a travel time and a spacing the entry
rhythm.
"""
import pytest

from nagasim import api
from nagasim.core.schema import validate_model_structured

from conftest import create, dispose, layout


def belt(name, length, spacing=1.0, accumulating=True, exit_resource=""):
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": {"length": length, "speed": 1.0, "spacing": spacing,
                       "accumulating": accumulating, "exit_resource": exit_resource,
                       "from_node": "", "to_node": ""}}


def station(name, action, resource, value, quantity=1):
    return {"name": name, "description": name, "type": "PROCESS", "position": [2, 0],
            "params": {"action": action, "resource_name": resource, "distribution": "Constant",
                       "dist_params": {"value": value}, "priority": 0, "quantity": quantity}}


def hog(name, resource, hold, quantity=1):
    """A part that takes *resource* at 0 and holds it for *hold*."""
    return ([create(f"{name}_src", max_arrivals=1, dist_params={"value": 1000.0}),
             station(name, "Seize Delay Release", resource, hold, quantity)],
            [{"source": f"{name}_src", "target": name}, {"source": name, "target": "dispose_1"}])


def always_first(name):
    return {"name": name, "description": name, "type": "DECIDE", "position": [2, 0],
            "params": {"mode": "Condition", "conditions": ["1 == 1"]}}


def link(a, b):
    return {"source": a, "target": b}


def belts(results):
    return results["conveyor_summary"].set_index("Conveyor")


# Accumulating belt -> full belt, free-running series, merge FIFO by blocking
# instant, divert after a belt and the no-belt-downstream guard are the
# oracle's (test_conveyor_systems_oracle.py cases 1-5).
def test_a_stopped_non_accumulating_belt_takes_no_new_part_on():
    """Parts arrive every 2 from 1 onto non-accumulating A (length 5, 5 cells)
    which feeds B (length 2, 2 cells) whose end stop is taken for ever at 0.

    Part k gets on A at 1 + 2k and reaches its end at 6 + 2k. Parts 0 and 1 go
    on B at 6 and 8; part 0 stops at B's end at 8, so B is full and part 2 is
    held at A's end from 10 -- A stops there: parts 5 and 6 take a cell but
    never get on (5 entered). Over the 20 of the run the on-belt integral is
    5+5+15+13+11.
    """
    blocks = [create(max_arrivals=0, start_time=1, dist_params={"value": 2.0}),
              belt("A", 5.0, accumulating=False), belt("B", 2.0, exit_resource="Stop"),
              station("out", "Delay Release", "Stop", 0.0), dispose()]
    links = [link("create_1", "A"), link("A", "B"), link("B", "out"), link("out", "dispose_1")]
    extra, extra_links = hog("stopper", "Stop", 1000.0)
    model = layout(blocks + extra, links + extra_links,
                   resources=[{"name": "Stop", "capacity": 1}],
                   simulation={"time_limit": 20, "warmup": 0, "replications": 1})
    rows = belts(api.run_model(model, replications=1))

    a, b = rows.loc["A"], rows.loc["B"]
    assert a["Exited"] == 2 and b["Entered"] == 2 and b["Exited"] == 0
    assert a["Blocked %"] == pytest.approx(100 * (20 - 10) / 20)
    assert b["Blocked %"] == pytest.approx(100 * (20 - 8) / 20)
    assert a["Entered"] == 5 and a["Max Occupancy"] == 3
    assert a["Avg Occupancy"] == pytest.approx(49 / 20)


def test_an_exit_resource_is_taken_first_and_then_the_downstream_belt():
    """Both must hold before a part leaves (D115): the end stop, then a cell on
    the next belt. A (1 cell, end stop Gate of 2 units) feeds B (length 2,
    spacing 2: 1 cell, end stop Stop); B -> release Stop -> release Gate.

    Gate is taken whole from 0 to 10, Stop from 0 to 30. Part 0 reaches A's end
    at 1, waits for Gate until 10, goes on B at 10 and waits at B's end from 12
    to 30. Part 1 gets on A at 10, reaches its end at 11, takes Gate's second
    unit at once and waits for B's cell until 30. A is blocked 9 + 19 = 28 of
    the 32 the run lasts; Gate is held 20 (the hog) + 20 + 21 of 64 unit-time.
    """
    blocks = [create(max_arrivals=2, dist_params={"value": 0.5}),
              belt("A", 1.0, exit_resource="Gate"),
              belt("B", 2.0, spacing=2.0, exit_resource="Stop"),
              station("rel_stop", "Delay Release", "Stop", 0.0),
              station("rel_gate", "Delay Release", "Gate", 0.0), dispose()]
    links = [link("create_1", "A"), link("A", "B"), link("B", "rel_stop"),
             link("rel_stop", "rel_gate"), link("rel_gate", "dispose_1")]
    g_blocks, g_links = hog("gate_hog", "Gate", 10.0, quantity=2)
    s_blocks, s_links = hog("stop_hog", "Stop", 30.0)
    model = layout(blocks + g_blocks + s_blocks, links + g_links + s_links,
                   resources=[{"name": "Gate", "capacity": 2}, {"name": "Stop", "capacity": 1}],
                   simulation={"time_limit": 100, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    rows = belts(results)

    assert rows.loc["A", "Blocked %"] == pytest.approx(100 * 28 / 32)
    assert rows.loc["B", "Blocked %"] == pytest.approx(100 * 18 / 32)
    usage = results["resource_summary"].set_index("Resource").loc["Gate", "Usage % (snapshot)"]
    assert usage == pytest.approx(100 * 61 / 64, abs=0.01)


@pytest.mark.parametrize("accumulating", [True, False])
def test_a_closed_belt_loop_warns_and_its_deadlock_ends_the_run(accumulating):
    """A -> B -> DECIDE -> A: parts circulate for ever. Accumulating, both belts
    fill and both ends block on each other; non-accumulating, a stopped belt
    takes nothing on, so the loop closes before it is full. Either way the
    static check names the loop and the run refuses to go on."""
    blocks = [create(max_arrivals=6, dist_params={"value": 0.1}),
              belt("A", 2.0, accumulating=accumulating),
              belt("B", 2.0, accumulating=accumulating), always_first("again"), dispose()]
    links = [link("create_1", "A"), link("A", "B"), link("B", "again"), link("again", "A"),
             link("again", "dispose_1")]
    model = layout(blocks, links, simulation={"time_limit": 100, "warmup": 0, "replications": 1})
    loops = [p for p in validate_model_structured(model) if p.code == "W_BLOCKING_LOOP"]
    assert loops and "A" in loops[0].message and "B" in loops[0].message

    with pytest.raises(RuntimeError, match="deadlock"):
        api.run_model(model, replications=1)
