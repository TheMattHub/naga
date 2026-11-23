"""Task 14 (conveyor system A): the assistant builds and checks a conveyor layout through tools
alone: place_piece (with a speed), place_transfer, get_ports, check_layout, then connect for the
exits. The file the tools wrote runs unchanged through api.run_model."""
import math

import pytest

from nagasim import agent_tools as T
from nagasim import api
from nagasim.core import conveyor_path as cp


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def ok(result):
    assert "error" not in result, result.get("error")
    return result


def _line(model="m"):
    """create -> A (straight 3) -> B (left arc r 1, 90 deg) -> C (straight 2) -> X (transfer
    with a left and a straight exit) -> L (left branch, 2 m) and S (straight, 2 m) -> a DISPOSE
    each, all through tools."""
    ok(T.call("new_model", name=model))
    ok(T.call("add_block", model=model, type="CREATE", name="src",
              params={"max_arrivals": 6, "distribution": "Constant", "dist_params": {"value": 1.0}}))
    ok(T.call("place_piece", model=model, name="A", segments=[{"straight": 3.0}], speed=2.0))
    ok(T.call("place_piece", model=model, name="B", snap_to="A.out",
              segments=[{"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}], speed=2.0))
    ok(T.call("place_piece", model=model, name="C", snap_to="B.out",
              segments=[{"straight": 2.0}], speed=2.0))
    ok(T.call("place_transfer", model=model, name="X", snap_to="C.out",
              exits=[{"port": "left", "angle": -90}, {"port": "straight", "angle": 0}]))
    for exit_name in ("left", "straight"):
        belt = exit_name[0].upper()
        ok(T.call("place_piece", model=model, name=belt, speed=2.0, segments=[{"straight": 2.0}],
                  snap_to=f"X.{exit_name}"))
        ok(T.call("add_block", model=model, type="DISPOSE", name=f"end_{exit_name}"))
        ok(T.call("connect", model=model, source=belt, target=f"end_{exit_name}",
                  source_port="out"))
    ok(T.call("connect", model=model, source="src", target="A"))
    return ok(T.call("get_model_json", model=model))["layout"]


def test_three_pieces_and_a_transfer_with_two_exits_build_check_and_run(ws):
    """Straight, left arc, straight, a transfer with two exits: built by tools only, check_layout
    has nothing to say, and the file runs as it is: round robin sends 3 of 6 parts out of each
    exit's belt."""
    layout = _line()
    assert ok(T.call("check_layout", model="m"))["problems"] == []
    assert T.call("check_layout", model="m")["ok"] is True
    assert [e["port"] for e in next(b for b in layout["blocks"] if b["name"] == "X")["params"]["exits"]] \
        == ["left", "straight"]
    belt = next(b for b in layout["blocks"] if b["name"] == "A")
    assert belt["params"]["speed"] == 2.0
    assert not [p for p in ok(T.call("validate_model", model="m"))["problems"]
                if p["code"].startswith(("E_", "W_CONVEYOR"))]
    layout["simulation"]["run_length"] = 200
    exited = api.run_model(layout, replications=1)["conveyor_summary"].set_index("Conveyor")["Exited"]
    assert (exited["L"], exited["S"]) == (3, 3)


def test_get_ports_gives_poses_and_links(ws):
    """A: straight 3 east from (1, 2) heading 90 (south): in at (1, 2) and out at (1, 5), both
    heading 90, B snapped at out. The out port lists B.in, the in port its feeder."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}], x=1.0, y=2.0,
              rotation=90.0))
    ok(T.call("place_piece", model="m", name="B", segments=[{"straight": 1.0}], snap_to="A.out"))
    ports = {p["name"]: p for p in ok(T.call("get_ports", model="m", block="A"))["ports"]}
    assert (ports["in"]["x"], ports["in"]["y"], ports["in"]["heading"]) == (1.0, 2.0, 90.0)
    assert (ports["out"]["x"], ports["out"]["y"], ports["out"]["heading"]) == (1.0, 5.0, 90.0)
    assert ports["out"]["linked_to"] == ["B.in"] and ports["in"]["linked_to"] == []
    assert ok(T.call("get_ports", model="m", block="B"))["ports"][0]["linked_to"] == ["A.out"]


def test_get_ports_of_a_transfer_names_its_exits_and_what_they_feed(ws):
    _line()
    ports = {p["name"]: p for p in ok(T.call("get_ports", model="m", block="X"))["ports"]}
    assert list(ports) == ["in", "left", "straight"]
    assert ports["in"]["linked_to"] == ["C.out"] and ports["left"]["linked_to"] == ["L.in"]
    # entry heading 270 (the arc turned the line north), left exit -90 from it
    assert cp.same_angle(ports["in"]["heading"], 270.0)
    assert cp.same_angle(ports["left"]["heading"], 180.0)


def test_snap_to_a_transfer_exit_takes_the_pose_get_ports_gives(ws):
    """The belts after the exits were placed with snap_to "X.left" / "X.straight" in _line: each
    starts at the pose get_ports gives that exit and is linked from it by source_port."""
    layout = _line()
    ports = {p["name"]: p for p in ok(T.call("get_ports", model="m", block="X"))["ports"]}
    for exit_name, belt in (("left", "L"), ("straight", "S")):
        block = next(b for b in layout["blocks"] if b["name"] == belt)
        assert block["layout_position"] == [ports[exit_name]["x"], ports[exit_name]["y"]]
        assert cp.same_angle(block["layout_rotation"], ports[exit_name]["heading"])
        link = next(k for k in layout["links"] if k["source"] == "X" and k["target"] == belt)
        assert (link["source_port"], link["target_port"]) == (exit_name, "in")
    assert T.call("place_piece", model="m", name="Q", segments=[{"straight": 1}],
                  snap_to="X.nope")["error"]["code"] == "E_UNKNOWN_PORT"


def test_check_layout_reports_an_open_port_with_a_plain_line(ws):
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    got = ok(T.call("check_layout", model="m"))
    assert not got["ok"] and got["warnings"] == 2
    assert {p["code"] for p in got["problems"]} == {"W_CONVEYOR_OPEN_PORT"}
    assert len(got["lines"]) == 2 and all("A" in line for line in got["lines"])


def test_tools_refuse_what_they_cannot_place(ws):
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    def bad(tool, **kw):
        return T.call(tool, model="m", **kw)["error"]["code"]
    assert bad("place_piece", name="Z", segments=[{"straight": 1}], speed=0) == "E_PLACE_SPEED"
    assert bad("place_transfer", name="X", mechanism="belt") == "E_PLACE_TYPE"
    assert bad("place_transfer", name="X", exits=[{"port": "a:b", "angle": 0}]) == "E_PLACE_EXITS"
    assert bad("get_ports", block="zzz") == "E_UNKNOWN_BLOCK"
    ok(T.call("add_block", model="m", type="DISPOSE", name="d0"))
    for k in range(1, 8):
        ok(T.call("add_block", model="m", type="DISPOSE", name=f"d{k}"))
    hint = T.call("get_ports", model="m", block="zzz")["error"]["hint"]
    assert "more" in hint and hint.count("d") < 12             # capped at 5 names plus a count
    ok(T.call("add_block", model="m", type="DISPOSE", name="d"))
    assert bad("get_ports", block="d") == "E_NOT_A_PIECE"


def test_a_typed_piece_takes_an_explicit_speed_over_the_datasheet(ws):
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="D", segments=[{"straight": 3.0}], type="belt"))
    ok(T.call("place_piece", model="m", name="E", segments=[{"straight": 3.0}], type="belt",
              speed=0.4))
    speed = {b["name"]: b["params"]["speed"]
             for b in ok(T.call("get_model_json", model="m"))["layout"]["blocks"]}
    assert speed["E"] == 0.4 and speed["D"] != 0.4


@pytest.mark.parametrize("unit_first", [True, False])
def test_a_typed_piece_keeps_its_datasheet_speed_when_the_time_unit_changes(ws, unit_first):
    """Final review I1: the datasheet speed is written in m/s, so 3.3 m of "belt" (0.33 m/s)
    take 10 s whether the model is switched from minutes to seconds before or after the piece
    is placed (it was 0.17 s when set after: the speed had been frozen in m/min)."""
    ok(T.call("new_model", name="m"))
    if unit_first:
        ok(T.call("set_simulation_settings", model="m", time_unit="seconds"))
    ok(T.call("add_block", model="m", type="CREATE", name="src",
              params={"max_arrivals": 1, "distribution": "Constant", "dist_params": {"value": 1.0}}))
    ok(T.call("place_piece", model="m", name="A", type="belt", segments=[{"straight": 3.3}]))
    ok(T.call("add_block", model="m", type="DISPOSE", name="end"))
    ok(T.call("connect", model="m", source="src", target="A"))
    ok(T.call("connect", model="m", source="A", target="end", source_port="out"))
    if not unit_first:
        ok(T.call("set_simulation_settings", model="m", time_unit="seconds"))
    layout = ok(T.call("get_model_json", model="m"))["layout"]
    params = next(b for b in layout["blocks"] if b["name"] == "A")["params"]
    assert (params["speed"], params["speed_unit"]) == (0.33, "m/s")
    system = api.run_model(layout, replications=1)["system"].set_index("Metric")["Value"]
    assert math.isclose(float(system["Avg Throughput Time"]), 10.0, rel_tol=1e-9)


def test_strict_checks_is_set_by_the_simulation_settings_tool(ws):
    """Final review I4: strict_checks has a setter; with it an open port is an error with the
    same code, and false turns it back into a warning."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 2.0}]))

    def severities():
        return {p["severity"] for p in ok(T.call("validate_model", model="m"))["problems"]
                if p["code"] == "W_CONVEYOR_OPEN_PORT"}
    assert severities() == {"warning"}
    sim = ok(T.call("set_simulation_settings", model="m", strict_checks=True))["simulation"]
    assert sim["strict_checks"] is True and severities() == {"error"}
    ok(T.call("set_simulation_settings", model="m", strict_checks=False))
    assert severities() == {"warning"}


def test_a_divert_port_heading_is_folded_like_every_other(ws):
    """Final review minor: a left divert of an east-running belt read -90; it is 270."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    ok(T.call("update_block", model="m", name="A",
              params={"diverts": [{"at": 1.0, "side": "left"}]}))
    ports = {p["name"]: p for p in ok(T.call("get_ports", model="m", block="A"))["ports"]}
    assert ports["d0"]["heading"] == 270.0 and ports["out"]["heading"] == 0.0


def test_no_tool_argument_names_a_file_or_a_permission():
    schemas = {t["name"]: t for t in T.tool_schemas()}
    for name in ("place_piece", "place_transfer", "get_ports", "check_layout"):
        args = set(schemas[name]["input_schema"]["properties"])
        assert not args & {"path", "file", "allow_network", "permission", "trusted"}, name


@pytest.mark.parametrize("unit,factor", [("centimeters", 0.01), ("millimeters", 0.001)])
def test_a_model_in_another_length_unit_places_pieces_in_that_unit(ws, unit, factor):
    """#2011's working side: in a centimetre model a straight of 300 is 3 m on the floor, so B
    snapped at A's out meets it at x = 3 m; a typed piece (datasheet speed) and a snap work."""
    ok(T.call("new_model", name="m"))
    layout = ok(T.call("get_model_json", model="m"))["layout"]
    layout["simulation"]["length_unit"] = unit
    ok(T.call("write_model", model="m", layout=layout))
    ok(T.call("place_piece", model="m", name="A", type="belt",
              segments=[{"straight": 3.0 / factor}]))
    ok(T.call("place_piece", model="m", name="B", snap_to="A.out",
              segments=[{"straight": 1.0 / factor}]))
    b = next(b for b in ok(T.call("get_model_json", model="m"))["layout"]["blocks"]
             if b["name"] == "B")
    assert math.isclose(b["layout_position"][0], 3.0, abs_tol=1e-6)
    out = {p["name"]: p for p in ok(T.call("get_ports", model="m", block="B"))["ports"]}["out"]
    assert math.isclose(out["x"], 4.0, abs_tol=1e-6)
