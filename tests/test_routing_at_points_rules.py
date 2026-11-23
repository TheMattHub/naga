"""Routing rules at points beyond the spec cases (conveyor system A7, D1359): a DECIDE hold in
line, the R5 eye boundary, params written back, checks, states.

Hand derivations. Branch belts B1, B2: length 2, speed 1, spacing 1 (2 s through), then DISPOSE.
- DECIDE hold: round_robin limit 1 over B1, B2; E0 at 0 -> B1 (under way until it leaves B1's
  end at 2), E1 at 1 -> B2 (until 3), E2 at 1.5: both full, it waits in the DECIDE; at 2 E0
  leaves B1, the turn is B1 (after B2): E2 boards B1 at 2 and leaves at 4. Without the limit E2
  would go to B1 at 1.5 and leave at 3.5.
- Eye boundary (R5): as above, but B1 has a front eye at 1.0: E0 is under way only until its
  front reaches the eye at 1. E1 at 0.5 -> B2 (until 2.5), E2 at 0.75: both full; at 1 E0's eye
  frees B1: E2 boards B1 at 1 (E0 boarded at 0, spacing 1 s) and leaves at 3.
"""
import pytest
from conftest import create, dispose, layout, process, run_env

from nagasim import api
from nagasim.core.schema import validate_model_structured


def belt(name, length=2.0, diverts=None, **extra):
    params = {"length": length, "speed": 1.0, "spacing": 1.0, "accumulating": True,
              "exit_resource": "", "from_node": "", "to_node": "", "stops": [], **extra}
    if diverts is not None:
        params["diverts"] = diverts
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": params}


def decide(rule):
    return {"name": "route", "description": "route", "type": "DECIDE", "position": [1, 0],
            "params": {"mode": "Rule", "rule": rule, "probs": [], "conditions": [],
                       "policy": "", "observation": [], "fallback": "first", "queue_exprs": []}}


def _decide_model(starts, rule, b1=None):
    creates = [create(name=f"c{i}", e_type=f"E{i}", max_arrivals=1, start_time=t)
               for i, t in enumerate(starts)]
    blocks = [*creates, decide(rule), b1 or belt("B1"), belt("B2"), dispose()]
    links = [{"source": f"c{i}", "target": "route"} for i in range(len(starts))]
    links += [{"source": "route", "target": "B1"}, {"source": "route", "target": "B2"},
              {"source": "B1", "target": "dispose_1"}, {"source": "B2", "target": "dispose_1"}]
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0,
                                             "replications": 1})


def _exit(results, e_type):
    log = results["entity_log"]
    return log[log["Type"] == e_type]["Exited At"].tolist()


def test_a_rule_decide_holds_a_part_until_an_exit_frees():
    rule = {"kind": "round_robin", "limit": 1}
    results = api.run_model(_decide_model([0.0, 1.0, 1.5], rule), replications=1)
    assert _exit(results, "E2") == [pytest.approx(4.0)]
    results = api.run_model(_decide_model([0.0, 1.0, 1.5], {"kind": "round_robin"}),
                            replications=1)
    assert _exit(results, "E2") == [pytest.approx(3.5)]


def test_under_way_ends_at_the_target_pieces_first_eye():
    b1 = belt("B1", eyes=[{"at": 1.0}])
    results = api.run_model(_decide_model([0.0, 0.5, 0.75], {"kind": "round_robin", "limit": 1},
                                          b1), replications=1)
    assert _exit(results, "E2") == [pytest.approx(3.0)]


def test_a_rule_is_written_back_only_when_set():
    doc = _trunk_doc([{"at": 3.0, "rule": {"kind": "fixed"}}, {"at": 5.0, "condition": "True"}])
    _env, model = run_env(doc, 10)
    diverts = model.blocks["T"].get_params_dict()["diverts"]
    assert diverts[0] == {"at": 3.0, "time": 0.0, "when_full": "wait", "rule": {"kind": "fixed"}}
    assert "rule" not in diverts[1] and "rule_group" not in diverts[1]
    _env, model = run_env(_decide_model([0.0], {"kind": "round_robin"}), 10)
    assert model.blocks["route"].get_params_dict()["rule"] == {"kind": "round_robin"}
    model.blocks["route"].set_params_from_dict({"mode": "Chance", "probs": [0.5]})
    assert "rule" not in model.blocks["route"].get_params_dict()


# -- checks --------------------------------------------------------------------
def _trunk_doc(diverts, **extra):
    blocks = [create(name="c0", max_arrivals=1), belt("T", length=10.0, diverts=diverts, **extra),
              *(belt(f"L{i}") for i in range(len(diverts))), dispose()]
    links = [{"source": "c0", "target": "T"}, {"source": "T", "target": "dispose_1"}]
    links += [{"source": "T", "target": f"L{i}"} for i in range(len(diverts))]
    links += [{"source": f"L{i}", "target": "dispose_1"} for i in range(len(diverts))]
    return layout(blocks, links)


def _cases(doc, code="E_CONVEYOR_DIVERT"):
    return sorted(p.fields["case"] for p in validate_model_structured(doc) if p.code == code)


RR = {"kind": "round_robin", "limit": 1}


def test_good_divert_rules_are_no_problem():
    assert _cases(_trunk_doc([{"at": 3, "rule": RR, "rule_group": "g"},
                              {"at": 5, "rule_group": "g"}])) == []
    assert _cases(_trunk_doc([{"at": 3, "rule": {"kind": "fixed"}}])) == []
    # the group's first divert along the belt, whatever the declared order
    assert _cases(_trunk_doc([{"at": 5, "rule_group": "g"},
                              {"at": 3, "rule": RR, "rule_group": "g"}])) == []


@pytest.mark.parametrize("diverts, case", [
    ([{"at": 3, "rule_group": "g"}, {"at": 5, "rule": RR, "rule_group": "g"}], "route_group"),
    ([{"at": 3, "rule": RR, "rule_group": "g"}, {"at": 5, "rule": RR, "rule_group": "g"}],
     "route_group"),
    ([{"at": 3, "rule_group": "g"}], "route_group"),
    ([{"at": 3, "rule": RR, "rule_group": 7}], "route_group"),
    ([{"at": 3, "rule": {"kind": "ghost"}}], "route_rule"),
    ([{"at": 3, "rule": {"kind": "fixed", "exits": ["a", "b"]}}], "route_rule"),
    ([{"at": 3, "rule": RR, "condition": "True"}], "route_mix"),
    ([{"at": 3, "rule": RR, "when_full": "pass"}], "route_mix"),
])
def test_a_bad_divert_rule_is_refused(diverts, case):
    assert case in _cases(_trunk_doc(diverts))


def test_zones_refuse_a_rule_that_can_hold():
    zones = {"zones": {"count": 2}}
    assert _cases(_trunk_doc([{"at": 3, "rule": RR}], **zones), "E_CONVEYOR_PARAM") \
        == ["zones_hold"]
    # M1: only a limit can hold; cyclic without one never waits
    assert _cases(_trunk_doc([{"at": 3, "rule": {"kind": "cyclic", "limit": 1}}], **zones),
                  "E_CONVEYOR_PARAM") == ["zones_hold"]
    assert _cases(_trunk_doc([{"at": 3, "rule": {"kind": "cyclic"}}], **zones),
                  "E_CONVEYOR_PARAM") == []
    assert _cases(_trunk_doc([{"at": 3, "rule": {"kind": "round_robin"}}], **zones),
                  "E_CONVEYOR_PARAM") == []


@pytest.mark.parametrize("rule, case", [
    ({"kind": "ghost"}, "rule"),
    ({"kind": "round_robin", "limit": -1}, "rule"),
    ({"kind": "round_robin", "exits": ["a", "b", "c"]}, "exits"),
])
def test_a_bad_decide_rule_is_refused(rule, case):
    assert _cases(_decide_model([0.0], rule), "E_DECIDE_RULE") == [case]


def test_a_rule_condition_is_checked_as_an_expression():
    doc = _decide_model([0.0], {"kind": "condition", "conditions": ["entity.size >"]})
    assert any(p.path.endswith("/rule/conditions/0") for p in validate_model_structured(doc))


def test_a_routing_rule_is_refused_in_a_state_by_name():
    from nagasim import state
    doc = _decide_model([0.0], {"kind": "round_robin"})
    doc["simulation"]["time_limit"] = 30
    assert state.refused_kind(doc) == "a routing rule"


# -- fix round 1 (R26-R28, M2, M3, M7) -----------------------------------------
def tbelt(name, length=10.0, speed=1.0, diverts=None):
    out = belt(name, length=length, diverts=diverts)
    out["params"]["speed"] = speed
    return out


def _types(results):
    log = results["entity_log"]
    return dict(zip(log["Type"], log["Exited At"]))


def test_a_load_diverted_off_its_way_to_the_target_ends_its_count():
    """R26 (review probe A). Trunk 10 m, one divert at 3, rule round_robin limit 1 to L1a (2 m,
    a divert at 1.5 to a side DISPOSE, condition True), which feeds L1b (the target piece).
    I0 at d0 at 3, onto L1a, off its divert at 4.5: its count ends there. I1 at d0 at 4 holds
    until 4.5, off L1a at 6.0. I2 at d0 at 5 holds until 6.0, off L1a at 7.5. Before R26 the
    count waited for L1b, which no load reaches, and the run ended in a deadlock."""
    creates = [create(name=f"c{i}", e_type=f"I{i}", max_arrivals=1, start_time=i) for i in range(3)]
    blocks = [*creates, tbelt("trunk", diverts=[{"at": 3, "rule": {"kind": "round_robin",
                                                                   "limit": 1}}]),
              tbelt("L1a", length=2.0, diverts=[{"at": 1.5, "condition": "True"}]),
              tbelt("L1b", length=2.0), dispose(), dispose(name="side"), dispose(name="dt")]
    links = [{"source": f"c{i}", "target": "trunk"} for i in range(3)] + [
        {"source": "trunk", "target": "dt"}, {"source": "trunk", "target": "L1a"},
        {"source": "L1a", "target": "L1b"}, {"source": "L1a", "target": "side"},
        {"source": "L1b", "target": "dispose_1"}]
    results = api.run_model(layout(blocks, links, simulation={"time_limit": None, "warmup": 0,
                                                              "replications": 1}),
                            replications=1)
    assert _types(results) == pytest.approx({"I0": 4.5, "I1": 6.0, "I2": 7.5})


def _spike(rule, l_speeds=(0.5, 0.5, 0.5), procs=True, n=4):
    """The spec spike: trunk 10 m, diverts at 3, 5, 7 to L1..L3 (2 m), each into P_i (10 s on
    S_i) when *procs*, else straight to DISPOSE; the trunk end to `dt`."""
    creates = [create(name=f"c{i}", e_type=f"I{i}", max_arrivals=1, start_time=i) for i in range(n)]
    diverts = [{"at": 3, "rule": rule, "rule_group": "g"}, {"at": 5, "rule_group": "g"},
               {"at": 7, "rule_group": "g"}]
    branches = [tbelt(f"L{i}", length=2.0, speed=s) for i, s in zip((1, 2, 3), l_speeds)]
    blocks = [*creates, tbelt("trunk", diverts=diverts), *branches, dispose(), dispose(name="dt")]
    links = [{"source": f"c{i}", "target": "trunk"} for i in range(n)]
    links.append({"source": "trunk", "target": "dt"})
    links += [{"source": "trunk", "target": f"L{i}"} for i in (1, 2, 3)]
    resources = []
    for i in (1, 2, 3):
        if procs:
            blocks.append(process(name=f"P{i}", resource=f"S{i}", value=10.0))
            links += [{"source": f"L{i}", "target": f"P{i}"},
                      {"source": f"P{i}", "target": "dispose_1"}]
            resources.append({"name": f"S{i}", "capacity": 1})
        else:
            links.append({"source": f"L{i}", "target": "dispose_1"})
    return layout(blocks, links, resources=resources,
                  simulation={"time_limit": None, "warmup": 0, "replications": 1})


def test_an_out_exit_sends_the_overflow_past_every_divert():
    """R28. The spike with sequential limit 1 and a fourth exit `out`: I0 L1, I1 L2, I2 L3 as
    in the spec case; I3 at d0 at 6 finds the three lanes full and takes `out`: it passes every
    divert and leaves the trunk's end at 3 + 10 = 13, with no hold."""
    rule = {"kind": "sequential", "exits": ["L1", "L2", "L3", "out"], "limit": 1}
    results = api.run_model(_spike(rule), replications=1)
    assert _types(results) == pytest.approx({"I0": 17.0, "I1": 20.0, "I2": 23.0, "I3": 13.0})
    frame = results["conveyor_diverts"]
    assert frame["Diverted"].tolist() == [1, 1, 1]
    assert frame["Max Wait"].tolist() == [0.0, 0.0, 0.0]


def test_a_recirculating_out_exit_counts_until_the_loop_belt_end():
    """R28 + M2. Trunk 10 m, d0 at 3 to L1 (2 m at 0.5: 4 s) into P1 (10 s); the trunk's end
    feeds R (2 m, 2 s), whose end feeds the trunk again. Rule sequential limit 1 over
    [L1, out]. I0 at d0 at 3: L1 (under way until 7). I1 at d0 at 4: L1 full, so `out`: trunk
    end at 11, R end at 13 (its target piece is R: the walk stops at the trunk itself), back on
    the trunk at 13, at d0 at 16: L1 free, L1 end at 20, P1 free since 17: it leaves at 30."""
    creates = [create(name=f"c{i}", e_type=f"I{i}", max_arrivals=1, start_time=i) for i in range(2)]
    rule = {"kind": "sequential", "exits": ["L1", "out"], "limit": 1}
    blocks = [*creates, tbelt("trunk", diverts=[{"at": 3, "rule": rule}]),
              tbelt("L1", length=2.0, speed=0.5), tbelt("R", length=2.0),
              process(name="P1", resource="S1", value=10.0), dispose()]
    links = [{"source": f"c{i}", "target": "trunk"} for i in range(2)] + [
        {"source": "trunk", "target": "R"}, {"source": "trunk", "target": "L1"},
        {"source": "R", "target": "trunk"}, {"source": "L1", "target": "P1"},
        {"source": "P1", "target": "dispose_1"}]
    doc = layout(blocks, links, resources=[{"name": "S1", "capacity": 1}],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1})
    results = api.run_model(doc, replications=1)
    assert _types(results) == pytest.approx({"I0": 17.0, "I1": 30.0})
    _env, model = run_env(doc, 1)
    route = model.blocks["trunk"]._diverts[0].route
    assert route.target(0) is model.blocks["L1"] and route.target(1) is model.blocks["R"]


def test_a_hold_is_the_wait_of_the_divert_it_stood_at():
    """M3. The spike to DISPOSE with L1 at 0.25 m/s (8 s), L2 at 1 (2 s), L3 at 0.5 (4 s),
    round_robin limit 1. I0 L1 from 3 to 11, I1 L2 from 4 (at d1 at 6) to 8, I2 L3 from 5.
    I3 at d0 at 6 holds; at 8 L2 frees and the turn (back at L1, full) finds L2: I3 rides to
    d1 (x = 5) by 10 and leaves L2's end at 12. The 2 s hold is d0's wait, not d1's."""
    results = api.run_model(_spike({"kind": "round_robin", "limit": 1}, (0.25, 1.0, 0.5),
                                   procs=False), replications=1)
    assert _types(results)["I3"] == pytest.approx(12.0)
    frame = results["conveyor_diverts"]
    assert frame["Diverted"].tolist() == [1, 2, 1]
    assert frame["Max Wait"].tolist() == pytest.approx([2.0, 0.0, 0.0])


def test_a_condition_rule_with_no_else_fails_loud_and_warns():
    """R27: no true condition and no ELSE stops the run with a plain error (as DECIDE
    "Condition"), and the load-time check warns."""
    rule = {"kind": "condition", "conditions": ["1 == 2", "2 == 3"]}
    doc = _decide_model([0.0], rule)
    assert _cases(doc, "W_ROUTE_RULE") == ["no_else"]
    with pytest.raises(RuntimeError, match="no ELSE"):
        api.run_model(doc, replications=1)


def test_a_limit_over_exits_with_no_conveyor_is_warned():
    """I3: a limit or least_loaded over exits that lead to no CONVEYOR counts nothing."""
    doc = _decide_model([0.0], {"kind": "least_loaded"})
    for block in doc["blocks"]:
        if block["name"] in ("B1", "B2"):
            block.update(type="DELAY", params={"distribution": "Constant",
                                               "dist_params": {"value": 1.0}})
    assert _cases(doc, "W_ROUTE_RULE") == ["uncounted", "uncounted"]
    assert _cases(_decide_model([0.0], {"kind": "least_loaded"}), "W_ROUTE_RULE") == []
    trunk = _trunk_doc([{"at": 3, "rule": {"kind": "fixed", "exits": ["d0", "out"],
                                           "limit": 1}}])
    assert _cases(trunk, "W_ROUTE_RULE") == ["uncounted"]          # `out` leads to a DISPOSE


def test_an_item_rule_outside_an_item_with_code_is_a_problem():
    assert _cases(_decide_model([0.0], {"kind": "item"}), "E_ITEM_HOOK") == ["rule_no_code"]
