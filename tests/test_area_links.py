"""Area links with delay and capacity (conveyor system A Task 10, spec goal 9; A2 Task 6).

Written from the spec and brief only, before the code. API assumption: the two fields sit on the
link dict itself, next to "source" and "target":
    {"source": "a", "target": "b", "delay": const(10), "capacity": 2}
delay a Constant (default none), capacity an int (default 0 = unlimited). A link with both at
0 (or without the fields) is today's instant link. A part enters a limited link when a slot is
free, spends the delay in it, and only then reaches the target; a full link makes its upstream
belt hold the end part (D78 token), like a full downstream belt. Times are read from the
entity_log "Exited At" column; the Dispose blocks delay 0, so a part exits when it arrives.
Every value is hand-derived in the docstring (see also task-10-derivations.md).
"""
import math

import pytest

from conftest import create, dispose, layout

from nagasim import api


def belt(name="A", length=2.0):
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": {"length": length, "speed": 1.0, "spacing": 1.0, "accumulating": True,
                       "exit_resource": "", "from_node": "", "to_node": ""}}


def const(value):
    """A link delay of *value* (the one time field since schema 32)."""
    return {"distribution": "Constant", "dist_params": {"value": value}}


def lnk(source, target, **fields):
    return {"source": source, "target": target, **fields}


def run(blocks, links, time_limit):
    model = layout(blocks, links, simulation={"time_limit": time_limit, "warmup": 0,
                                              "replications": 1})
    return api.run_model(model, replications=1)


def burst(links_fields, per_arrival=3):
    """Three parts at t = 0 in one arrival (the next one is at 1000, past the 100 run), then
    create_1 -> dispose_1 over a link with the given fields."""
    blocks = [create(entities_per_arrival=per_arrival, dist_params={"value": 1000.0}, start_time=0),
              dispose()]
    return run(blocks, [lnk("create_1", "dispose_1", **links_fields)], 100)


def exits(results):
    return sorted(results["entity_log"]["Exited At"].tolist())


def assert_times(results, expected):
    got = exits(results)
    assert len(got) == len(expected)
    assert all(math.isclose(g, e, abs_tol=1e-9) for g, e in zip(got, expected)), got


def belt_line(link_fields):
    """Parts at 0, 1, 2 onto belt A (length 2, speed 1, spacing 1: 2 cells), A -> dispose_1 over
    a link with the given fields."""
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0), belt(), dispose()]
    links = [lnk("create_1", "A"), lnk("A", "dispose_1", **link_fields)]
    return run(blocks, links, 1000)


def test_area_link_delay_and_capacity():
    """delay 10, capacity 2, three parts at t = 0. Parts 1 and 2 take the two slots at 0
    and leave at 10. Part 3 waits at the link's entry; a slot frees at 10, so it enters at 10 and
    leaves at 10 + 10 = 20. Exit times 10, 10, 20."""
    assert_times(burst({"delay": const(10), "capacity": 2}), [10.0, 10.0, 20.0])


def test_delay_alone_is_an_unlimited_delay():
    """capacity 0 = unlimited: all three parts enter at 0 and leave at 0 + 10. Exit times 10 x 3."""
    assert_times(burst({"delay": const(10), "capacity": 0}), [10.0, 10.0, 10.0])
    assert_times(burst({"delay": const(10)}), [10.0, 10.0, 10.0])


@pytest.mark.parametrize("fields", [{}, {"delay": const(0), "capacity": 0}])
def test_a_link_with_both_fields_at_zero_is_todays_instant_link(fields):
    """No fields, or both 0: parts pass at once, exit at 0, 0, 0, and the entity log is identical to
    that of the model without the fields."""
    plain = burst({})
    got = burst(fields)
    assert_times(got, [0.0, 0.0, 0.0])
    assert got["entity_log"].equals(plain["entity_log"])


def test_capacity_alone_behaves_as_an_instant_link():
    """capacity 1 with delay 0: a part spends no time in the link, so the slot is free again
    the same instant and all three still exit at 0, 0, 0 (same log as without the fields)."""
    got = burst({"delay": const(0), "capacity": 1})
    assert_times(got, [0.0, 0.0, 0.0])
    assert got["entity_log"].equals(burst({})["entity_log"])


def test_full_area_link_blocks_belt():
    """Belt A (2 cells, speed 1, spacing 1) feeds a link of delay 100, capacity 1; parts board
    A at 0, 1, 2. Part 0 reaches A's end at 2, takes the free slot, leaves at 102. Part 1 reaches the
    end at 3 and finds the link full: A holds it there (D78) until 102, enters then, leaves at 202.
    Part 2 stands behind part 1 (at 1 from t = 3), gets to the end at 103 when part 1 has gone,
    finds the link full again until 202, enters then, leaves at 302. Exit times 102, 202, 302,
    and A reports blocked time."""
    results = belt_line({"delay": const(100), "capacity": 1})
    assert_times(results, [102.0, 202.0, 302.0])
    a = results["conveyor_summary"].set_index("Conveyor").loc["A"]
    assert a["Exited"] == 3 and a["Blocked %"] > 0


def test_unlimited_area_link_does_not_block_belt():
    """Same line, capacity 0: no part is held. Part k reaches A's end at k + 2 (part 2 behind part 1
    reaches it at 4 since part 1 left at 3) and leaves at k + 2 + 100. Exit times 102, 103, 104,
    and A is never blocked."""
    results = belt_line({"delay": const(100), "capacity": 0})
    assert_times(results, [102.0, 103.0, 104.0])
    a = results["conveyor_summary"].set_index("Conveyor").loc["A"]
    assert a["Blocked %"] == 0
