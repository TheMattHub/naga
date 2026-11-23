"""CONVEYOR_TRANSFER beyond the spec cases (conveyor system A8): the feeding belt's speed as
transfer_speed, back-pressure on the feeder, a deadlock that names the module, exits to a
non-belt, turn angles, rules, ports through flattening, validation, schema and state.

Speed 0.5 m/s, items 0.41 m, as tests/test_transfer_piece.py; every value hand-derived."""
import math

import pytest

from conftest import create, dispose, layout
from test_conveyor_divert_spec import belt
from test_transfer_piece import ITEM, SPEED, build, close, fronts, popup_side, receiving, run, xfer

from nagasim import api
from nagasim.core import plainlines
from nagasim.core.blocks.transfer_piece import turn_angle
from nagasim.core.schema import validate_model_structured


SECONDS = {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "seconds"}


def cases(doc, code):
    return sorted(p.fields["case"] for p in validate_model_structured(doc) if p.code == code)


def feeder(length=1.0):
    return belt(name="feeder", length=length, speed=SPEED, spacing=ITEM)


def test_transfer_speed_0_takes_the_feeding_belts_speed():
    """No transfer_speed: the feeder's 0.5. As the spec's feeder case: accepted at 2.0, eye at
    2.0 + 0.1 + 1.2 + 0.6 = 3.9."""
    side = xfer("popup", [{"port": "side", "angle": 90}], lift_time=0.1, cross_length=0.6)
    del side["params"]["transfer_speed"]
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1, feeder=feeder())
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [3.9])


def test_transfer_speed_0_with_no_feeding_belt_is_refused_when_it_is_needed():
    side = xfer("popup", [{"port": "side", "angle": 90}])
    del side["params"]["transfer_speed"]
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1)
    with pytest.raises(Exception, match="transfer_speed"):
        run(doc)


def test_the_feeder_holds_its_load_while_the_module_is_busy():
    """Two loads on a 1.0 m feeder, boarding 0 and 0.82 (its entry rhythm). Load 1 reaches the
    module at 2.0, which is busy to 4.0 (the 2.0 s side cycle); load 2 reaches the feeder's end
    at 2.82 and is held there (the token) until 4.0: eye events 3.9 and 5.9."""
    doc = build(popup_side(), {"side": receiving("recv", eye_at=0.3)}, loads=2,
                feeder=feeder())
    results, rec = run(doc)
    close(fronts(rec, "recv"), [3.9, 5.9])
    row = results["conveyor_summary"].set_index("Conveyor").loc["feeder"]
    assert row["Exited"] == 2


@pytest.mark.parametrize("zones, eye", [(None, 5.9), ({"count": 2, "release_delay": 1.0}, 6.9)])
def test_a_zoned_feeder_keeps_its_held_lead_part_its_zone_1_delay(zones, eye):
    """As above, load 2 held at the feeder's end from 2.82 to 4.0. On a zoned feeder (delay
    1.0) its exit was held, so the part waits its zone-1 delay before the hand-off (R23), as
    into a belt: accepted at 5.0, eye at 5.0 + 1.9 = 6.9; without zones 4.0 + 1.9 = 5.9."""
    conveyor = feeder()
    if zones:
        conveyor["params"]["zones"] = zones
    doc = build(popup_side(), {"side": receiving("recv", eye_at=0.3)}, loads=2, feeder=conveyor)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [3.9, eye])


def test_a_module_that_cannot_hand_over_is_named_in_the_deadlock():
    """The receiving belt feeds a belt whose end stop R a hog holds for ever (SEIZE, then a
    HOLD that never releases): that belt fills, the receiving belt fills, and the module keeps
    its load. The run ends with the module named among the blocked."""
    sink = belt(name="sink", length=0.82, speed=SPEED, spacing=ITEM, exit_resource="R")
    hold = {"name": "hog_hold", "description": "hog_hold", "type": "HOLD", "position": [0, 0],
            "params": {"mode": "Infinite"}}
    seize = {"name": "hog_seize", "description": "hog_seize", "type": "SEIZE",
             "position": [0, 0], "params": {"resource_name": "R"}}
    blocks = [create(e_type="BOX", entities_per_arrival=8, max_arrivals=1, start_time=0),
              popup_side(), receiving("recv", eye_at=0.3, length=0.82), sink, dispose(),
              create(name="hog_src", max_arrivals=1, start_time=0), seize, hold,
              dispose("hog_out")]
    links = [{"source": "create_1", "target": "xfer"},
             {"source": "xfer", "target": "recv", "source_port": "side"},
             {"source": "recv", "target": "sink"}, {"source": "sink", "target": "dispose_1"},
             {"source": "hog_src", "target": "hog_seize"},
             {"source": "hog_seize", "target": "hog_hold"},
             {"source": "hog_hold", "target": "hog_out"}]
    doc = layout(blocks, links, resources=[{"name": "R", "capacity": 1}], simulation=SECONDS,
                 entities=[{"name": "BOX", "size": {"length": ITEM, "width": 0.3, "height": 0.2}}])
    with pytest.raises(RuntimeError, match=r"deadlock.*xfer"):
        api.run_model(doc, replications=1)


def test_an_exit_to_a_non_belt_frees_the_module_as_it_is_taken():
    """Side exit straight to a DISPOSE: up 0.1 + cross 1.2, taken at 1.3, down 0.1, free at
    1.4. Exits 1.3, 2.7, 4.1."""
    blocks = [create(e_type="BOX", entities_per_arrival=3, max_arrivals=1, start_time=0),
              popup_side(), dispose()]
    links = [{"source": "create_1", "target": "xfer"}, {"source": "xfer", "target": "dispose_1"}]
    doc = layout(blocks, links, simulation=SECONDS,
                 entities=[{"name": "BOX", "size": {"length": ITEM, "width": 0.3, "height": 0.2}}])
    log = api.run_model(doc, replications=1)["entity_log"]
    close(sorted(log["Exited At"].tolist()), [1.3, 2.7, 4.1])


@pytest.mark.parametrize("angle, turn", [(90, 90.0), (-90, 90.0), (270, 90.0), (180, 180.0),
                                         (360, 0.0), (-450, 90.0)])
def test_a_table_turns_the_shorter_way(angle, turn):
    assert math.isclose(turn_angle(angle), turn)


def test_a_turntable_exit_at_minus_90_takes_the_90_degree_time():
    """-90 = 90 the other way: the spec's 13.4 s cycle, eye events 8.1, 21.5."""
    doc = build(xfer("turntable", [{"port": "west", "angle": -90}], entry_length=1.0,
                     turn_rate=18, turn_ramp=0.3),
                {"west": receiving("recv", eye_at=0.4)}, loads=2)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [8.1, 21.5])


def test_a_condition_rule_with_an_else_sends_every_load_to_the_else_exit():
    """conditions ["False"]: exit a never, the ELSE exit b always. Side cycles of 2.0 s: b's
    eye sees 1.9, 3.9, 5.9, 7.9."""
    exits = [{"port": "a", "angle": 90}, {"port": "b", "angle": -90}]
    doc = build(xfer("popup", exits, lift_time=0.1, cross_length=0.6,
                     rule={"kind": "condition", "conditions": ["False"]}),
                {"a": receiving("recv_a", eye_at=0.3), "b": receiving("recv_b", eye_at=0.3)},
                loads=4)
    _results, rec = run(doc)
    assert fronts(rec, "recv_a") == []
    close(fronts(rec, "recv_b"), [1.9, 3.9, 5.9, 7.9])


def test_lengths_follow_their_unit():
    """cross_length 60 centimeters = 0.6 m: the spec's side cycle, eye at 1.9."""
    side = popup_side()
    side["params"].update(cross_length=60, cross_length_unit="centimeters")
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [1.9])


def _definition(blocks, links):
    return {"info": {}, "parameters": [], "blocks": blocks, "links": links, "annotations": []}


def _port(name, kind, port):
    return {"name": name, "description": name, "type": kind, "position": [0, 0],
            "params": {"port": port}}


def test_exit_ports_survive_flattening():
    """The two-exit module inside a submodel, its links listed b first with ports: load 1
    still goes to exit a (round robin), so a's eye sees 1.9 and 5.9, b's 3.9 and 7.9."""
    exits = [{"port": "a", "angle": 90}, {"port": "b", "angle": -90}]
    inner = _definition(
        [_port("in", "SUB_ENTRY", "in"), xfer("popup", exits, lift_time=0.1, cross_length=0.6),
         receiving("recv_a", eye_at=0.3), receiving("recv_b", eye_at=0.3),
         _port("out", "SUB_EXIT", "out")],
        [{"source": "in", "target": "xfer"},
         {"source": "xfer", "target": "recv_b", "source_port": "b"},
         {"source": "xfer", "target": "recv_a", "source_port": "a"},
         {"source": "recv_a", "target": "out"}, {"source": "recv_b", "target": "out"}])
    cell = {"name": "cell", "description": "cell", "type": "SUBMODEL", "position": [0, 0],
            "params": {"definition": "Cell", "arguments": {}}}
    doc = layout([create(e_type="BOX", entities_per_arrival=4, max_arrivals=1, start_time=0),
                  cell, dispose("x")],
                 [{"source": "create_1", "target": "cell"}, {"source": "cell", "target": "x"}],
                 submodels={"Cell": inner}, simulation=SECONDS,
                 entities=[{"name": "BOX", "size": {"length": ITEM, "width": 0.3,
                                                    "height": 0.2}}])
    _results, rec = run(doc)
    eyes = {e.block for e in rec.events if e.kind == "eye"}
    recv_a = next(name for name in eyes if name.endswith("recv_a"))
    recv_b = next(name for name in eyes if name.endswith("recv_b"))
    close(fronts(rec, recv_a), [1.9, 5.9])
    close(fronts(rec, recv_b), [3.9, 7.9])


# -- validation -----------------------------------------------------------------------
def two_exits(**params):
    exits = [{"port": "a", "angle": 90}, {"port": "b", "angle": -90}]
    return build(xfer("popup", exits, **params),
                 {"a": receiving("recv_a", eye_at=0.3), "b": receiving("recv_b", eye_at=0.3)},
                 loads=1)


def test_a_valid_transfer_has_no_problem():
    assert [p.code for p in validate_model_structured(two_exits()) if p.code.startswith("E_")] == []


def test_link_ports_must_be_exits_once_each_and_match_the_exit_count():
    doc = two_exits()
    doc["links"][-1]["source_port"] = "c"
    assert cases(doc, "E_CONVEYOR_DIVERT") == ["transfer_port"]
    doc["links"][-1]["source_port"] = "a"
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_DIVERT"]
    assert [p.fields["case"] for p in found] == ["transfer_port_twice"]
    assert plainlines.plain(found[0]) == "Transfer xfer: exit a has two links"
    doc = two_exits()
    doc["links"] = [k for k in doc["links"] if k.get("target") != "recv_b"]
    assert cases(doc, "E_CONVEYOR_DIVERT") == ["transfer_links"]


def test_bad_params_are_conveyor_param_problems():
    doc = two_exits(turn_rate=0, lift_time=-1)
    doc["blocks"][1]["params"]["mechanism"] = "slide"
    doc["blocks"][1]["params"]["exits"].append({"port": "a", "angle": 0})
    doc["blocks"][1]["params"]["exits"].append({"port": "c"})
    assert cases(doc, "E_CONVEYOR_PARAM") == ["number", "transfer_exit", "transfer_exit_twice",
                                              "transfer_mechanism", "transfer_number"]


def test_a_bad_rule_is_a_problem_and_a_limit_on_a_non_belt_exit_is_warned():
    doc = two_exits(rule={"kind": "nope"})
    assert cases(doc, "E_CONVEYOR_DIVERT") == ["transfer_rule"]
    doc = two_exits(rule={"kind": "round_robin", "exits": ["a"]})
    assert cases(doc, "E_CONVEYOR_DIVERT") == ["transfer_rule"]
    doc = two_exits(rule={"kind": "round_robin", "limit": 1})
    doc["links"] = [k if k.get("target") != "recv_b" else {**k, "target": "dispose_1"}
                    for k in doc["links"]]
    # exit a's count also ends at recv_a's eye, its handshake (I2)
    assert cases(doc, "W_ROUTE_RULE") == ["transfer_target", "uncounted"]


def test_a_count_that_ends_at_the_handshake_is_warned_and_a_named_target_is_not():
    """Both exits' belts lead to a DISPOSE, so their target piece is the receiving belt itself,
    whose first eye is the handshake: a limit would always read 0. Naming a belt further on
    ("a:down_a") moves the count's end there."""
    doc = two_exits(rule={"kind": "least_loaded"})
    found = [p for p in validate_model_structured(doc) if p.code == "W_ROUTE_RULE"]
    assert sorted(p.fields["port"] for p in found) == ["a", "b"]
    assert {p.fields["case"] for p in found} == {"transfer_target"}
    assert plainlines.plain(found[0]).startswith("Transfer xfer:")
    doc = two_exits(rule={"kind": "least_loaded", "exits": ["a:down_a", "b:down_b"]})
    for side in ("a", "b"):
        doc["blocks"].append(receiving(f"down_{side}"))
        link = next(k for k in doc["links"] if k["source"] == f"recv_{side}")
        doc["links"].append({"source": f"down_{side}", "target": link["target"]})
        link["target"] = f"down_{side}"
    assert cases(doc, "W_ROUTE_RULE") == []


def test_defaults_keep_their_measured_units_in_a_minutes_model():
    """R30: lift_time 0.1 s and cross_length 0.6 m are the defaults' own units, so a minutes
    model (belt speed 30 m/min = 0.5 m/s, transfer_speed 0.5 m/s) times the side cycle in
    seconds as the seconds model does: eye at 1.9 s = 1.9 / 60 min."""
    side = xfer("popup", [{"port": "side", "angle": 90}], transfer_speed_unit="m/s")
    recv = receiving("recv", eye_at=0.3)
    recv["params"]["speed"] = 30.0
    doc = build(side, {"side": recv}, loads=1)
    doc["simulation"]["time_unit"] = "minutes"
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [1.9 / 60])


def _through(middle: list, middle_links: list, loads=3):
    """CREATE -> side popup -> *middle* blocks -> recv (eye 0.3) -> DISPOSE, in seconds."""
    blocks = [create(e_type="BOX", entities_per_arrival=loads, max_arrivals=1, start_time=0),
              popup_side(), *middle, receiving("recv", eye_at=0.3), dispose()]
    links = [{"source": "create_1", "target": "xfer"}, *middle_links,
             {"source": "recv", "target": "dispose_1"}]
    return layout(blocks, links, simulation=SECONDS,
                  entities=[{"name": "BOX", "size": {"length": ITEM, "width": 0.3, "height": 0.2}}])


def test_a_station_between_the_module_and_its_belt_keeps_the_handshake():
    """The reviewer's probe: side popup -> STATION -> belt with an eye at 0.3. The handshake
    is still that eye: the spec's 2.0 s cycle, eyes 1.9, 3.9, 5.9 (1.9, 3.3, 4.7 without)."""
    station = {"name": "st", "description": "st", "type": "STATION", "position": [0, 0],
               "params": {"station": "Dock"}}
    doc = _through([station], [{"source": "xfer", "target": "st"},
                               {"source": "st", "target": "recv"}])
    assert cases(doc, "W_ROUTE_RULE") == []
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [1.9, 3.9, 5.9])


def test_a_branching_block_between_the_module_and_its_belts_is_warned():
    decide = {"name": "split", "description": "split", "type": "DECIDE", "position": [0, 0],
              "params": {"mode": "Chance", "probs": [0.5]}}
    other = receiving("recv_2", eye_at=0.3)
    doc = _through([decide, other], [{"source": "xfer", "target": "split"},
                                     {"source": "split", "target": "recv"},
                                     {"source": "split", "target": "recv_2"},
                                     {"source": "recv_2", "target": "dispose_1"}])
    found = [p for p in validate_model_structured(doc) if p.code == "W_ROUTE_RULE"]
    assert [p.fields["case"] for p in found] == ["transfer_fanout"]
    assert found[0].fields["fan"] == "split" and found[0].hint
    assert plainlines.plain(found[0]) == "Transfer xfer: exit side branches at split before its conveyor"


def test_a_belt_with_no_eye_and_a_stop_within_an_item_is_warned():
    doc = build(popup_side(), {"side": receiving("recv")}, loads=1)
    recv = next(b for b in doc["blocks"] if b["name"] == "recv")
    assert cases(doc, "W_ROUTE_RULE") == []
    recv["params"]["stops"] = [{"at": 0.2, "time": 5.0}]      # nearer than the 0.41 m box
    assert cases(doc, "W_ROUTE_RULE") == ["transfer_short"]
    recv["params"]["stops"] = [{"at": 0.5, "time": 5.0}]
    assert cases(doc, "W_ROUTE_RULE") == []


def test_a_port_with_a_colon_is_refused():
    doc = two_exits()
    doc["blocks"][1]["params"]["exits"][0]["port"] = "a:x"
    assert "transfer_port_colon" in cases(doc, "E_CONVEYOR_PARAM")


def test_transfer_speed_0_with_no_feeding_belt_is_a_load_time_problem():
    side = xfer("popup", [{"port": "side", "angle": 90}])
    del side["params"]["transfer_speed"]
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1)
    assert cases(doc, "E_CONVEYOR_PARAM") == ["transfer_speed"]
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1, feeder=feeder())
    assert cases(doc, "E_CONVEYOR_PARAM") == []
    straight = xfer("turntable", [{"port": "s", "angle": 0}])     # no move needs the speed
    del straight["params"]["transfer_speed"]
    assert cases(build(straight, {"s": receiving("recv")}, loads=1), "E_CONVEYOR_PARAM") == []


def test_the_schema_describes_exits_and_the_mechanism():
    from nagasim.modelschema import build_json_schema
    variants = build_json_schema()["$defs"]["block"]["oneOf"]
    spec = next(v for v in variants if v["title"] == "CONVEYOR_TRANSFER")["properties"]["params"]
    exits = spec["properties"]["exits"]["anyOf"][0]
    assert exits["items"]["additionalProperties"] is False
    assert exits["items"]["required"] == ["port", "angle"]
    assert spec["properties"]["mechanism"]["anyOf"][0]["enum"] == ["popup", "turntable", "lift"]
    assert spec["additionalProperties"] is False


def test_a_state_refuses_a_transfer():
    from nagasim.state import refused_kind
    doc = two_exits()
    for block in doc["blocks"]:
        if block["type"] == "CONVEYOR":
            block["params"]["eyes"] = []        # a belt's eyes are refused first
    assert refused_kind(doc) == "a conveyor transfer"


def test_params_come_back_as_written_and_a_round_trip_keeps_the_times():
    """R39 (checkpoint S1): the module keeps each number in its own unit and applies the factor
    at use, so get_params_dict gives back what was set, with its units, and setting it again in
    a minutes model changes no time (it converted twice before: 0.1 s became 2.8e-05 min)."""
    from conftest import run_env
    side = xfer("popup", [{"port": "side", "angle": 90}], lift_time=6.0, lift_time_unit="seconds",
                transfer_speed=30.0, transfer_speed_unit="m/min")
    recv = receiving("recv", eye_at=0.3)
    recv["params"]["speed"] = 30.0
    doc = build(side, {"side": recv}, loads=1)
    doc["simulation"]["time_unit"] = "minutes"
    _env, model = run_env(doc, 1)
    module = model.blocks["xfer"]
    params = module.get_params_dict()
    assert (params["lift_time"], params["lift_time_unit"]) == (6.0, "seconds")
    assert (params["transfer_speed"], params["transfer_speed_unit"]) == (30.0, "m/min")
    assert (params["cross_length"], params["cross_length_unit"]) == (0.6, "meters")
    before = module.move_time(90.0), module.return_time(90.0)
    module.set_params_from_dict(params)
    assert module.get_params_dict() == params
    # 0.1 min lift, then 0.6 m at 30 m/min = 0.02 min; down 0.1 min
    close(list(before), [0.1 + 0.02, 0.1])
    close([module.move_time(90.0), module.return_time(90.0)], list(before))


@pytest.mark.parametrize("key,unit,code", [("length_unit", "feet", "E_UNIT"),
                                           ("time_unit", "fortnights", "E_TIME_UNIT")])
def test_an_unknown_unit_beside_a_transfer_is_a_problem_not_a_raise(key, unit, code):
    """Final review minor: the transfer exit checks read lengths in the model's units, and an
    unknown unit made validation raise a bare ValueError instead of listing its problem. No
    transfer_speed and a feeder, so the feeding-speed check runs too."""
    side = popup_side()
    del side["params"]["transfer_speed"]
    doc = build(side, {"side": receiving("recv", eye_at=0.3)}, loads=1, feeder=feeder())
    doc["simulation"][key] = unit
    assert code in {p.code for p in validate_model_structured(doc)}
