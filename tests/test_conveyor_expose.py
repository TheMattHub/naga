"""Conveyor state exposed as model variables (conveyor system A13, D1377-D1379), and the CONVEYOR
param round trip (R39 follow-up).

Times are hand-derived. Belt defaults (test_conveyor_divert_spec.belt): length 10, speed 1,
spacing 1, accumulating; parts every 1 from t = 0, so a free part's front is at x at its
boarding time + x."""
import copy
import math

import pandas as pd
import pytest

from conftest import create, dispose, run_env
from test_conveyor_divert_spec import belt, hog, model, pick, plain_line
from test_conveyor_eyes import _queue_line, eyed, traced
from test_conveyor_reverse import _doc as reverse_doc
from test_conveyor_zones import _doc as zoned_doc
from test_transfer_piece import build, receiving, xfer

from nagasim import api
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import setup_run, step_until


# -- R39 follow-up: params come back as written -------------------------------------------------

def _engine_numbers(conveyor):
    """What the run reads: the scaled values, points included."""
    return (conveyor.length, conveyor.speed, conveyor.spacing, conveyor.gap, conveyor.cell,
            conveyor._part_length, [s[0] for s in conveyor.stops],
            [d.at for d in conveyor.diverts], [e[0] for e in conveyor.eyes])


@pytest.mark.parametrize("sim, units", [
    # The engine has no feet; centimetres is the non-metre length unit it has.
    ({"length_unit": "centimeters"}, {}),
    ({"time_unit": "minutes"}, {"length_unit": "centimeters", "speed_unit": "m/s",
                                "spacing_unit": "millimeters", "part_length_unit": "millimeters"}),
], ids=["centimetres model", "minutes model, explicit units"])
def test_params_come_back_as_written_and_a_round_trip_keeps_the_belt(sim, units):
    conveyor = belt(length=500.0, speed=20.0, spacing=100.0,
                    stops=[{"at": 300.0, "time": 0.0, "resource": ""}],
                    diverts=[{"at": 200.0, "condition": "False", "time": 0.0,
                              "when_full": "wait"}])
    conveyor["params"].update({"eyes": [{"at": 250.0}], "part_length": 40.0, **units})
    doc = plain_line(conveyor, arrivals=1, extra_blocks=[], extra_links=[])
    doc["links"].append({"source": "belt", "target": "dispose_1"})
    doc["simulation"].update(sim)
    _env, model = run_env(doc, 1)
    block = model.blocks["belt"]
    params = block.get_params_dict()
    assert (params["length"], params["speed"], params["spacing"], params["part_length"]) == \
        (500.0, 20.0, 100.0, 40.0)
    assert params["stops"][0]["at"] == 300.0 and params["diverts"][0]["at"] == 200.0
    assert params["eyes"][0]["at"] == 250.0
    # loading fills a "base" unit with the model's own (#496), so every unit key comes back
    assert all(params[key] == unit for key, unit in units.items())
    before = _engine_numbers(block)
    block.set_params_from_dict(params)
    assert block.get_params_dict() == params
    assert _engine_numbers(block) == before


def test_a_typed_belts_gap_and_cell_come_back_as_written():
    conveyor = belt(length=500.0, speed=20.0)
    conveyor["params"].update({"type": "roller", "gap": 5.0, "cell": 2.0})
    doc = plain_line(conveyor, arrivals=1)
    doc["simulation"]["length_unit"] = "centimeters"
    _env, model = run_env(doc, 1)
    block = model.blocks["belt"]
    params = block.get_params_dict()
    assert (params["gap"], params["cell"]) == (5.0, 2.0)
    before = (block.gap, block.cell, block.cell_length)
    block.set_params_from_dict(params)
    assert (block.gap, block.cell, block.cell_length) == before == (0.05, 0.02, 0.02)


# -- exposed state (D1378, D1379) -----------------------------------------------------------


def _record(doc, names, horizon):
    """Run *doc* once; {name: [(t, value) of each write]} for the exposed *names*, and the
    variables at the end."""
    env, _model = setup_run(doc, seed=0, run_len=horizon, warmup=0.0)
    log = {name: [] for name in names}
    for name in names:
        env.ctx.variable_hooks.setdefault(name, []).append(
            lambda n: log[n].append((env.now, env.ctx.variables[n])))
    step_until(env, horizon)
    return log, env.ctx.variables


def _close(got, want):
    assert len(got) == len(want), got
    for (t, v), (t2, v2) in zip(got, want):
        assert math.isclose(t, t2, abs_tol=1e-9) and v == v2, got


def _hold(name, condition):
    return {"name": name, "description": name, "type": "HOLD", "position": [1, 1],
            "params": {"mode": "Condition", "conditions": [condition]}}


def _stopping_line(expose=True):
    """2 parts every 1 onto non-accumulating B1 (10 long, speed 1) whose end stop R a hog holds
    until 20: part 1 reaches the end at 10, the belt stands until 20 (R67), part 1 leaves at 20,
    part 2 (front at 9 when the belt stood) at 21. HOLD `down` (an entity at 0) waits for
    running == 0; HOLD `up` (an entity at 12) for running == 1."""
    hog_blocks, hog_links = hog("R", 20)
    conveyor = belt(name="B1", accumulating=False, exit_resource="R")
    conveyor["params"]["expose"] = expose
    blocks = [create(max_arrivals=2, dist_params={"value": 1.0}, start_time=0), conveyor,
              pick("R"), dispose(), *hog_blocks,
              create(name="down_src", e_type="D", max_arrivals=1, start_time=0),
              _hold("down", 'VAR("B1.running") == 0'),
              create(name="up_src", e_type="U", max_arrivals=1, start_time=12),
              _hold("up", 'VAR("B1.running") == 1'), dispose(name="out")]
    links = [{"source": "create_1", "target": "B1"}, {"source": "B1", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, *hog_links,
             {"source": "down_src", "target": "down"}, {"source": "down", "target": "out"},
             {"source": "up_src", "target": "up"}, {"source": "up", "target": "out"}]
    return model(blocks, links, ["R"])


def _left(results, e_type):
    log = results["entity_log"]
    return sorted(log[log["Type"] == e_type]["Exited At"].tolist())


def test_a_hold_on_running_0_releases_when_the_belt_stops():
    doc = _stopping_line()
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]
    results = api.run_model(doc, replications=1)
    assert _left(results, "D") == [pytest.approx(10.0)]
    assert _left(results, "U") == [pytest.approx(20.0)]
    log, _ = _record(doc, ["B1.running"], 40)
    _close(log["B1.running"], [(10.0, 0), (20.0, 1)])


def test_an_unexposed_belts_name_is_an_unknown_variable():
    codes = {p.code for p in validate_model_structured(_stopping_line(expose=False))}
    assert "E_VARIABLE_UNKNOWN" in codes


def _exposed(doc):
    on = copy.deepcopy(doc)
    for block in on["blocks"]:
        if block["type"] in ("CONVEYOR", "CONVEYOR_TRANSFER"):
            block["params"]["expose"] = True
    return on


@pytest.mark.parametrize("case", ["held accumulating", "held stopping", "zoned"])
def test_expose_false_writes_nothing_and_true_changes_no_result(case):
    """A queue standing across the eye (test_conveyor_eyes: the end held until 20), and a zoned
    belt held until 20 with two eyes: every frame and every eye firing as without expose."""
    if case == "zoned":
        doc = zoned_doc({"count": 3, "release_delay": 1.0}, 6)
        doc["blocks"][0]["params"]["dist_params"] = {"value": 0.9}
        doc["blocks"][1]["params"]["eyes"] = [{"at": 0.3}, {"at": 1.0, "edge": "back"}]
    else:
        doc = _queue_line([{"at": 8.5}], case == "held accumulating")
    plain, exposed = (traced(d) for d in (doc, _exposed(doc)))
    assert [e.t for e in plain[1]] == [e.t for e in exposed[1]]
    for key, frame in plain[0].items():
        if isinstance(frame, pd.DataFrame):
            assert frame.equals(exposed[0][key]), key
    _log, variables = _record(doc, [], 60)
    assert variables == {}


def test_forward_follows_each_reversal():
    """Signals at 1.0 and 1.5 (test_conveyor_reverse.test_reverse_twice): backward at 1.0,
    forward at 1.5; the belt is accumulating, so its drive never stands."""
    doc = reverse_doc([0.0], [1.0, 1.5])
    belt_block = next(b for b in doc["blocks"] if b["name"] == "belt")
    belt_block["params"]["expose"] = True
    log, variables = _record(doc, ["belt.forward", "belt.running"], 30)
    _close(log["belt.forward"], [(1.0, 0), (1.5, 1)])
    assert log["belt.running"] == [] and variables["belt.running"] == 1


@pytest.mark.parametrize("edge", ["front", "back"])
def test_an_eye_on_a_zoned_belt_is_blocked_from_the_front_edge_to_the_back_edge(edge):
    """Zoned belt (test_conveyor_zones: 1.8 m at 0.5, items 0.41, the exit held until 20, zone
    delay 0), parts at 0 and 5, an eye at 1.2. Part 1's front is at 1.2 at 2.4 and at 1.61 (its
    back clears) at 3.22, then it stands at the end. Part 2 is at 1.2 at 7.4 and stands with its
    front at 1.39, across the eye, until the exit frees at 20; it reaches 1.61 at 20.44.
    Whichever edge the eye fires on."""
    doc = zoned_doc({"count": 3, "release_delay": 0.0}, 2)
    doc["blocks"][1]["params"].update({"eyes": [{"at": 1.2, "edge": edge}], "expose": True})
    log, _ = _record(doc, ["belt.eye0.blocked"], 60)
    _close(log["belt.eye0.blocked"], [(2.4, 1), (3.22, 0), (7.4, 1), (20.44, 0)])


def test_eyes_off_a_zoned_belt_are_exposed_too():
    """D1390 (supersedes the D1379 limit): off a zoned belt an eye's level is written too, from
    the part's nose, so VAR() knows it; its count as before (D1380)."""
    doc = _exposed(_queue_line([{"at": 8.5}], True))
    doc["blocks"].append(create(name="w", e_type="W", max_arrivals=1, start_time=0))
    doc["blocks"].append(_hold("watch", 'VAR("belt.eye0.blocked") == 1'))
    doc["links"].append({"source": "w", "target": "watch"})
    doc["links"].append({"source": "watch", "target": "dispose_1"})
    assert not [p for p in validate_model_structured(doc) if p.code == "E_VARIABLE_UNKNOWN"]
    _log, variables = _record(_exposed(_queue_line([{"at": 8.5}], True)), [], 30)
    assert set(variables) == {"belt.running", "belt.eye0.count", "belt.eye0.blocked"}


def test_a_zoned_belt_with_a_divert_exposes_no_eye_level():
    """D1390: on a belt with zones and diverts a part can reach a point through the part ahead
    standing in the zone chain (#2018), so no stage gives its nose: VAR() names the level
    unknown and the run writes none; the count is there (D1380)."""
    doc = zoned_doc({"count": 3, "release_delay": 1.0}, 4)
    doc["blocks"][1]["params"].update({"eyes": [{"at": 0.9}], "expose": True,
                                       "diverts": [{"at": 0.05, "condition": "False"}]})
    doc["blocks"] += [dispose(name="side"), create(name="w", e_type="W", max_arrivals=1),
                      _hold("watch", 'VAR("belt.eye0.blocked") == 1')]
    doc["links"] += [{"source": "belt", "target": "side"}, {"source": "w", "target": "watch"},
                     {"source": "watch", "target": "side"}]
    problems = [p for p in validate_model_structured(doc) if p.code == "E_VARIABLE_UNKNOWN"]
    assert len(problems) == 1 and "belt.eye0.blocked" in problems[0].message
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] not in ("w", "watch")]
    doc["links"] = [k for k in doc["links"] if k["source"] not in ("w", "watch")]
    _log, variables = _record(doc, [], 60)
    assert "belt.eye0.blocked" not in variables and variables["belt.eye0.count"] == 4


def test_a_turntables_state_follows_its_cycle():
    """T8 turntable (test_transfer_piece.test_turntable_90_cycle_is_13_4_seconds), one load:
    entry 1.0 m at 0.5 = 2.0 s transferring, turn 5.3 s, boarding to the eye at 8.1, turn back
    5.3 s, idle at 13.4."""
    module = xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                  turn_rate=18, turn_ramp=0.3, expose=True)
    doc = build(module, {"east": receiving("recv", eye_at=0.4)}, loads=1)
    log, _ = _record(doc, ["xfer.state"], 30)
    _close(log["xfer.state"], [(0.0, 2), (2.0, 3), (7.3, 2), (8.1, 3), (13.4, 0)])


def test_a_popups_side_exit_lifts_then_crosses():
    """T8 pop-up side cycle: up 0.1 lifting, cross 1.2 transferring, boards at 1.3 (still
    transferring) to the eye at 1.9, down 0.1 lifting, idle at 2.0."""
    module = xfer("popup", [{"port": "side", "angle": 90}], lift_time=0.1, cross_length=0.6,
                  expose=True)
    doc = build(module, {"side": receiving("recv", eye_at=0.3)}, loads=1)
    log, _ = _record(doc, ["xfer.state"], 30)
    _close(log["xfer.state"], [(0.0, 1), (0.1, 2), (1.9, 1), (2.0, 0)])


@pytest.mark.parametrize("inside", [False, True], ids=["top level", "inside the definition"])
def test_a_belt_in_a_submodel_writes_its_flattened_name(inside):
    """B1 inside instance `cell`: a top-level HOLD reads VAR("cell/B1.running"), one in the
    definition VAR("$B1.running") (flattened to the same name); both release at 10."""
    from test_conveyor_ports_rules import _definition, _entry
    conveyor = belt(name="B1", accumulating=False, exit_resource="R")
    conveyor["params"]["expose"] = True
    blocks = [_entry("in", "SUB_ENTRY", "in"), conveyor, pick("R"), _entry("out", "SUB_EXIT", "out")]
    links = [{"source": "in", "target": "B1"}, {"source": "B1", "target": "pick"},
             {"source": "pick", "target": "out"}]
    top_hold = _hold("down", 'VAR("cell/B1.running") == 0')
    if inside:
        blocks += [create(name="w", e_type="D", max_arrivals=1, start_time=0),
                   _hold("wh", 'VAR("$B1.running") == 0'), dispose("wd")]
        links += [{"source": "w", "target": "wh"}, {"source": "wh", "target": "wd"}]
    hog_blocks, hog_links = hog("R", 20)
    cell = {"name": "cell", "description": "cell", "type": "SUBMODEL", "position": [0, 0],
            "params": {"definition": "Cell", "arguments": {}}}
    top = [create(max_arrivals=1, start_time=0), cell, dispose(), *hog_blocks]
    top_links = [{"source": "create_1", "target": "cell"}, {"source": "cell", "target": "dispose_1"},
                 *hog_links]
    if not inside:
        top += [create(name="d_src", e_type="D", max_arrivals=1, start_time=0), top_hold,
                dispose("out2")]
        top_links += [{"source": "d_src", "target": "down"}, {"source": "down", "target": "out2"}]
    doc = model(top, top_links, ["R"])
    doc["submodels"] = {"Cell": _definition(blocks, links)}
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]
    assert _left(api.run_model(doc, replications=1), "D") == [pytest.approx(10.0)]


def test_a_state_of_an_exposed_belt_is_carried():
    """A state carries the exposed names as variables, and starting from it accepts them."""
    from test_state import _carried
    from test_state_belts import _belt_line
    _carried(_exposed(_belt_line(accumulating=True)), 5.0)


def test_a_merges_at_comes_back_as_written():
    conveyor = belt(length=500.0, merges=[{"at": 200.0, "from": "side", "priority": "belt"}])
    blocks = [create(max_arrivals=1, start_time=0), create(name="side", max_arrivals=0),
              conveyor, dispose()]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    doc = model(blocks, links, length_unit="centimeters")
    _env, built = run_env(doc, 1)
    block = built.blocks["belt"]
    params = block.get_params_dict()
    assert params["merges"][0]["at"] == 200.0
    before = [m[0] for m in block.merges]
    block.set_params_from_dict(params)
    assert [m[0] for m in block.merges] == before == [2.0]


def test_every_eye_counts_its_firings_on_a_plain_belt():
    """D1380 (R68): eye<k>.count on any exposed belt, one per firing of the eye's own edge:
    3 parts pass a front eye at 2.5 (2.5, 3.5, 4.5) and a back eye at 6.0 (part_length 0.4:
    6.4, 7.4, 8.4)."""
    conveyor = eyed([{"at": 2.5}, {"at": 6.0, "edge": "back"}], name="B1", part_length=0.4)
    conveyor["params"]["expose"] = True
    doc = model([create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0), conveyor,
                 dispose()],
                [{"source": "create_1", "target": "B1"}, {"source": "B1", "target": "dispose_1"}])
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]
    log, variables = _record(doc, ["B1.eye0.count", "B1.eye1.count"], 30)
    _close(log["B1.eye0.count"], [(2.5, 1), (3.5, 2), (4.5, 3)])
    _close(log["B1.eye1.count"], [(6.4, 1), (7.4, 2), (8.4, 3)])
    assert variables["B1.eye0.blocked"] == variables["B1.eye1.blocked"] == 0    # D1390


def test_running_on_a_reversible_non_accumulating_belt():
    """The part reaches the end of the 4 m belt at 4.0 and waits for R (held to 20): the drive
    stops (R67). The signal at 6.0 reverses the belt; nothing waits at the start, so the drive
    runs again backward."""
    doc = reverse_doc([0.0], [6.0])
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    conveyor["params"].update({"accumulating": False, "exit_resource": "R", "expose": True})
    hog_blocks, hog_links = hog("R", 20)
    doc["blocks"] += hog_blocks
    doc["links"] += hog_links
    doc["resources"] = [{"name": "R", "capacity": 1}]
    log, _ = _record(doc, ["belt.running", "belt.forward"], 30)
    _close(log["belt.running"], [(4.0, 0), (6.0, 1)])
    _close(log["belt.forward"], [(6.0, 0)])


def test_a_lifts_state_follows_its_cycle():
    """Lift (test_transfer_piece.test_lift_two_levels_cycle timings): lift_height 1.2 at 0.3 =
    4.0 s each way, lifting; boards at 4.0 and reaches the eye at 0.3 m 0.6 s later (4.6),
    transferring; back by 8.6, idle."""
    module = xfer("lift", [{"port": "up", "angle": 0}], lift_height=1.2, lift_speed=0.3,
                  expose=True)
    doc = build(module, {"up": receiving("recv", eye_at=0.3)}, loads=1)
    log, _ = _record(doc, ["xfer.state"], 30)
    _close(log["xfer.state"], [(0.0, 1), (4.0, 2), (4.6, 1), (8.6, 0)])


def test_a_bare_dotted_name_gets_a_var_hint():
    doc = _stopping_line()
    next(b for b in doc["blocks"] if b["name"] == "down")["params"]["conditions"] = \
        ["B1.running == 0"]
    found = [p for p in validate_model_structured(doc) if p.code == "E_EXPRESSION"]
    assert found and 'VAR("B1.running")' in found[0].hint
