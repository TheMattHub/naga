"""Spec tests for Task 7: routing rules at CONVEYOR diverts and in DECIDE mode "Rule" (before the code).

Hand derivations are in the docstrings and in task-7-derivations.md.

API assumptions (the brief and R5 only; no engine source read):
  * A divert item is {"at": x, "rule": {kind, exits, conditions, limit, attribute}} in place of
    "condition". The first divert of a rule carries "rule_group": "<name>"; later diverts of the same
    belt name that group (no rule of their own). Exit i of the rule is divert i's own link (belt
    link i + 1); the rule is evaluated when a load's front reaches the FIRST divert of the group; a
    load whose choice is exit i passes diverts 0..i-1 untouched and leaves at divert i.
  * "exits" is a list of exit names; each branch belt is named like its exit ("L1", "L2", "L3"), so
    the name is valid whether the engine reads exits as names or as branch block names. The
    target piece is then the branch belt itself (default: last CONVEYOR before a non-conveyor).
  * DECIDE: {"mode": "Rule", "rule": {"kind": ..., ...}}; with no "exits" the exits are the DECIDE's
    outgoing links in link order. (Provided here without "exits".)
  * Hold: when choose returns None the load keeps its cell at the divert; "conveyor_diverts" counts
    the held time as wait (Avg Wait / Max Wait, as in the existing divert wait test).
  * A run that ends with a load stuck raises RuntimeError whose message contains "deadlock" and the
    stuck belt's name (blocking.deadlock_at_end, "nothing is left to happen but <names>").
  * Entity types are stored upper-cased in the entity log; "Exited At" is the exit time.
"""

import pytest

from conftest import create, dispose, layout, process

from nagasim import api
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder


def belt(name, length=10.0, speed=1.0, spacing=1.0, diverts=None, exit_resource=""):
    params = {"length": length, "speed": speed, "spacing": spacing, "accumulating": True,
              "exit_resource": exit_resource, "from_node": "", "to_node": "", "stops": []}
    if diverts is not None:
        params["diverts"] = list(diverts)
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": params}


def model(blocks, links, resources=()):
    return layout(blocks, links, resources=[{"name": r, "capacity": 1} for r in resources],
                  simulation={"time_limit": None, "warmup": 0, "replications": 1})


def exits_of(results, e_type):
    log = results["entity_log"]
    return sorted(log[log["Type"] == e_type.upper()]["Exited At"].tolist())


def belt_row(results, name):
    return results["conveyor_summary"].set_index("Conveyor").loc[name]


def divert_row(results, k):
    frame = results["conveyor_diverts"]
    return frame[frame["Divert"] == k].iloc[0]


def rr_diverts(rule_extra=None, ats=(3, 5, 7), group="spike"):
    """Three diverts at `ats`; the rule once on the first, the group name on the others."""
    rule = {"kind": "round_robin", "exits": ["L1", "L2", "L3"], "limit": 1}
    rule.update(rule_extra or {})
    first = {"at": ats[0], "rule": rule, "rule_group": group}
    return [first] + [{"at": a, "rule_group": group} for a in ats[1:]]


# -- the spike router ----------------------------------------------------------

def _spike_model():
    """Trunk (length 10, speed 1, spacing 1) with diverts at 3, 5, 7 to branches L1, L2, L3
    (length 2, speed 0.5: 4 s through each), each ending in a PROCESS P_i of 10 s on a resource S_i
    of capacity 1, then DISPOSE. Four items I0..I3 board at t = 0, 1, 2, 3."""
    creates = [create(name=f"c{i}", e_type=f"I{i}", max_arrivals=1, start_time=i)
               for i in range(4)]
    branches = [belt(f"L{i}", length=2.0, speed=0.5) for i in (1, 2, 3)]
    procs = [process(name=f"P{i}", resource=f"S{i}", value=10.0) for i in (1, 2, 3)]
    blocks = [*creates, belt("trunk", diverts=rr_diverts()), *branches, *procs, dispose()]
    links = [{"source": f"c{i}", "target": "trunk"} for i in range(4)]
    links.append({"source": "trunk", "target": "dispose_1"})           # link 0: the trunk end
    for i in (1, 2, 3):                                                # links 1..3: the diverts
        links.append({"source": "trunk", "target": f"L{i}"})
    for i in (1, 2, 3):
        links += [{"source": f"L{i}", "target": f"P{i}"}, {"source": f"P{i}", "target": "dispose_1"}]
    return model(blocks, links, resources=("S1", "S2", "S3"))


def test_spike_router_holds_the_trunk_until_the_first_branch_clears():
    """Front of item Ik is at x at t = k + x. Decision when the front reaches d0 (x = 3), at k + 3.
      I0 at t=3: round_robin, nothing under way: L1 (index 0); diverts at d0 (dwell 0), boards L1
         at 3, 2 m at 0.5 m/s: leaves L1 end at 3 + 4 = 7 (no eye: that is "arrived", R5).
      I1 at t=4: L1 full (limit 1), turn is L2: L2. Passes d0, reaches d1 (x=5) at 1 + 5 = 6,
         boards L2 at 6, end at 10.
      I2 at t=5: L3; passes d0, d1, reaches d2 (x=7) at 2 + 7 = 9, boards L3 at 9, end at 13.
      I3 at t=6 (3 + 3) at d0: L1 under way since 3, L2 since 4, L3 since 5, all at limit 1:
         choose -> None, so I3 holds at x = 3 from 6. I0 leaves L1's end at 7 (P1 free: takes it at
         once): arrived, the decision re-runs, turn is L1: index 0, I3 leaves d0 at 7 (wait 1).
      I3 boards L1 at 7, reaches its end at 11; P1 holds I0 until 7 + 10 = 17, so I3 seizes S1 at
         17 and leaves P1 at 27. (Whether I3 waits on the belt end or in P1's queue, 27 is the same.)
    Process ends: I0 17, I1 10 + 10 = 20, I2 13 + 10 = 23, I3 27."""
    results = api.run_model(_spike_model(), replications=1)
    assert exits_of(results, "I0") == [pytest.approx(17.0)]
    assert exits_of(results, "I1") == [pytest.approx(20.0)]
    assert exits_of(results, "I2") == [pytest.approx(23.0)]
    assert exits_of(results, "I3") == [pytest.approx(27.0)]
    assert belt_row(results, "L1")["Entered"] == 2
    assert belt_row(results, "L2")["Entered"] == 1
    assert belt_row(results, "L3")["Entered"] == 1
    d0 = divert_row(results, 0)
    assert d0["Diverted"] == 2                      # I0 and the released I3
    assert d0["Max Wait"] == pytest.approx(1.0)     # held 6 -> 7
    assert divert_row(results, 1)["Diverted"] == 1
    assert divert_row(results, 2)["Diverted"] == 1


def test_spike_router_hold_keeps_the_cell_at_the_divert_on_the_timeline():
    """I3 boards at 3: at t = 5.5 it is at x = 2.5 (nothing else is behind it). From 6 it is held at
    d0 = 3 and at 6.5 still sits at x = 3.0, not past it."""
    recorder = TraceRecorder()
    api.run_model(_spike_model(), replications=1, trace=recorder)
    timeline = Timeline(recorder.events)
    rows = timeline.state_at(5.5)["belts"]["trunk"]
    assert min(x for _i, x, _s in rows) == pytest.approx(2.5)
    rows = timeline.state_at(6.5)["belts"]["trunk"]
    assert min(x for _i, x, _s in rows) == pytest.approx(3.0)


# -- DECIDE mode "Rule" --------------------------------------------------------

def test_decide_rule_round_robin_over_three_links_sends_a_b_c_a_b_c():
    """Six entities E0..E5 created at t = 0..5 go straight to the DECIDE (no belt), which has
    three outgoing links in order A, B, C (DELAYs of 1, 2, 3 s, then DISPOSE). round_robin without
    a limit: E0 A, E1 B, E2 C, E3 A, E4 B, E5 C. Exit = creation + delay of its exit:
    E0 0+1=1, E1 1+2=3, E2 2+3=5, E3 3+1=4, E4 4+2=6, E5 5+3=8."""
    creates = [create(name=f"c{i}", e_type=f"E{i}", max_arrivals=1, start_time=i) for i in range(6)]
    decide = {"name": "route", "description": "route", "type": "DECIDE", "position": [1, 0],
              "params": {"mode": "Rule", "rule": {"kind": "round_robin"}, "probs": [],
                         "conditions": [], "policy": "", "observation": [], "fallback": "first",
                         "queue_exprs": []}}
    delays = [{"name": n, "description": n, "type": "DELAY", "position": [2, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": float(v)}}}
              for n, v in (("A", 1), ("B", 2), ("C", 3))]
    links = [{"source": f"c{i}", "target": "route"} for i in range(6)]
    for n in ("A", "B", "C"):
        links += [{"source": "route", "target": n}, {"source": n, "target": "dispose_1"}]
    results = api.run_model(model([*creates, decide, *delays, dispose()], links), replications=1)
    expected = {"E0": 1.0, "E1": 3.0, "E2": 5.0, "E3": 4.0, "E4": 6.0, "E5": 8.0}
    for name, t in expected.items():
        assert exits_of(results, name) == [pytest.approx(t)], name


# -- attribute rule at diverts -------------------------------------------------

def test_attribute_rule_at_diverts_sends_each_item_to_the_branch_named_by_its_line():
    """Trunk as above, diverts at 3, 5, 7, rule {kind: attribute, attribute: "line"}, branches L1..L3
    of length 2 at speed 1 straight to DISPOSE (2 s through). X0 (line L3) boards 0, reaches d2
    (x = 7) at 7, leaves the branch end at 9. X1 (line L1) boards 1, d0 at 4, end at 6.
    X2 (line L2) boards 2, d1 (x = 5) at 7, end at 9."""
    lines = {"X0": "L3", "X1": "L1", "X2": "L2"}
    creates = [create(name=f"c{i}", e_type=f"X{i}", max_arrivals=1, start_time=i) for i in range(3)]
    assigns = [{"name": f"a{i}", "description": f"a{i}", "type": "ASSIGN", "position": [1, 0],
                "params": {"assignments": [{"target": "entity.line",
                                            "expr": '"%s"' % lines[f"X{i}"]}]}} for i in range(3)]
    diverts = rr_diverts({"kind": "attribute", "attribute": "line"})
    diverts[0]["rule"].pop("limit")
    blocks = [*creates, *assigns, belt("trunk", diverts=diverts),
              *[belt(f"L{i}", length=2.0) for i in (1, 2, 3)], dispose(), dispose(name="dispose_t")]
    links = []
    for i in range(3):
        links += [{"source": f"c{i}", "target": f"a{i}"}, {"source": f"a{i}", "target": "trunk"}]
    links.append({"source": "trunk", "target": "dispose_t"})
    for i in (1, 2, 3):
        links += [{"source": "trunk", "target": f"L{i}"}, {"source": f"L{i}", "target": "dispose_1"}]
    results = api.run_model(model(blocks, links), replications=1)
    assert exits_of(results, "X0") == [pytest.approx(9.0)]
    assert exits_of(results, "X1") == [pytest.approx(6.0)]
    assert exits_of(results, "X2") == [pytest.approx(9.0)]
    assert [belt_row(results, f"L{i}")["Entered"] for i in (1, 2, 3)] == [1, 1, 1]
    assert belt_row(results, "trunk")["Exited"] == 3


# -- Review Focus 5: a hold that never ends ------------------------------------

def test_exits_that_never_free_end_in_a_deadlock_naming_the_routing_belt():
    """Same trunk, but every branch end needs resource Gate (capacity 1), which a "Seize Delay" hog
    takes at t = 0 and never releases: no branch item ever leaves its belt end, so no exit ever
    drops its count. I0, I1, I2 take L1, L2, L3 and sit at the branch ends; I3 reaches d0 at 6 and
    holds for ever. Nothing is left to happen: the run must raise RuntimeError("deadlock ...")
    naming the routing belt `trunk` (it does not hang)."""
    creates = [create(name=f"c{i}", e_type=f"I{i}", max_arrivals=1, start_time=i) for i in range(4)]
    hog_src = create(name="hog_src", e_type="Hog", max_arrivals=1, start_time=0)
    hog = process(name="hog", resource="Gate", value=1.0, action="Seize Delay")
    pick = process(name="pick", resource="Gate", value=0.0, action="Delay Release")
    blocks = [*creates, belt("trunk", diverts=rr_diverts()),
              *[belt(f"L{i}", length=2.0, speed=0.5, exit_resource="Gate") for i in (1, 2, 3)],
              pick, dispose(), hog_src, hog]
    links = [{"source": f"c{i}", "target": "trunk"} for i in range(4)]
    links.append({"source": "trunk", "target": "dispose_1"})
    for i in (1, 2, 3):
        links += [{"source": "trunk", "target": f"L{i}"}, {"source": f"L{i}", "target": "pick"}]
    links += [{"source": "pick", "target": "dispose_1"}, {"source": "hog_src", "target": "hog"},
              {"source": "hog", "target": "dispose_1"}]
    with pytest.raises(RuntimeError, match="deadlock") as info:
        api.run_model(model(blocks, links, resources=("Gate",)), replications=1)
    assert "trunk" in str(info.value)
