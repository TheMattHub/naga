"""Belt path geometry (conveyor system A, task 1): straight and arc pieces set the belt's length
and pose. Every number is hand-computed in the on-screen floor-plan frame (D1354, D74): x east,
y SOUTH, heading = layout_rotation in degrees clockwise on screen (0 east, 90 south); a left
turn lowers the heading, a right turn raises it. Values re-derived for that frame with the
owner's approval (checkpoint T3, C1)."""
import math

import pytest

from nagasim import api
from nagasim.core import conveyor_path as cp
from nagasim.core.schema import validate_model_structured

from conftest import create, dispose, layout


def test_straight_then_left_arc_length_and_pose():
    """From (0,0) heading 0 (east): straight 2 to (2,0); a left arc r=1 turns toward north
    (-y on screen), so its centre is (2,-1). After 45 deg: x = 2 + sin 45, y = -1 + cos 45
    = -(1 - cos 45), heading -45. After 90: (3,-1) heading -90 (north, stored 270)."""
    segs = cp.parse_path([{"straight": 2.0}, {"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}])
    assert math.isclose(cp.path_length(segs), 2 + math.pi / 2)
    p = cp.pose_at(cp.Pose(0, 0, 0), segs, 2 + math.pi / 4)
    assert math.isclose(p.x, 2 + math.sin(math.pi / 4), abs_tol=1e-9)
    assert math.isclose(p.y, -(1 - math.cos(math.pi / 4)), abs_tol=1e-9)
    assert math.isclose(p.heading, -45.0, abs_tol=1e-9)
    e = cp.end_pose(cp.Pose(0, 0, 0), segs)
    assert (round(e.x, 9), round(e.y, 9), round(e.heading, 9)) == (3.0, -1.0, -90.0)


def test_right_arc_turns_clockwise():
    """From (0,0) heading 0 (east) a right arc r=2 turns clockwise on screen, toward south
    (+y): centre (0,2), end (2,2) heading 90 (south)."""
    e = cp.end_pose(cp.Pose(0, 0, 0), cp.parse_path([{"arc": {"radius": 2.0, "angle": 90, "turn": "right"}}]))
    assert (round(e.x, 9), round(e.y, 9), round(e.heading, 9)) == (2.0, 2.0, 90.0)


def test_pose_is_clamped_and_follows_the_start_heading():
    # heading 90 is south (+y): 2 m from (1,1) is (1,3)
    segs = cp.parse_path([{"straight": 2.0}])
    assert cp.pose_at(cp.Pose(1, 1, 90), segs, -5) == cp.Pose(1, 1, 90)
    p = cp.pose_at(cp.Pose(1, 1, 90), segs, 99)
    assert (round(p.x, 9), round(p.y, 9), p.heading) == (1.0, 3.0, 90)


def test_a_semicircle_comes_back_the_other_way():
    """A left half-turn r=1 from (0,0) heading east: centre (0,-1) to the north on screen, end
    (0,-2) heading -180 (west)."""
    e = cp.end_pose(cp.Pose(0, 0, 0), cp.parse_path([{"arc": {"radius": 1.0, "angle": 180, "turn": "left"}}]))
    assert (round(e.x, 9), round(e.y, 9), round(e.heading, 9)) == (0.0, -2.0, -180.0)


def test_bad_arcs_refused():          # Review Focus 2
    for bad in ({"radius": 0, "angle": 90, "turn": "left"}, {"radius": 1, "angle": 0, "turn": "left"},
                {"radius": 1, "angle": 361, "turn": "left"}, {"radius": 1, "angle": 90, "turn": "up"}):
        with pytest.raises(ValueError):
            cp.parse_path([{"arc": bad}])


@pytest.mark.parametrize("raw", [{"straight": 1}, [{"straight": 0}], [{"straight": "2"}],
                                 [{"bend": 1}], [{"straight": 1, "arc": {}}], ["straight"],
                                 [{"arc": {"radius": 1, "angle": 90}}], [{"straight": True}]])
def test_bad_pieces_refused_with_a_message(raw):
    with pytest.raises(ValueError, match="path"):
        cp.parse_path(raw)


def belt(**params):
    base = {"length": 0.0, "speed": 0.5, "spacing": 1.0, "accumulating": True,
            "exit_resource": "", "from_node": "", "to_node": ""}
    return {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [1, 0],
            "params": {**base, **params}}


def line(conveyor):
    blocks = [create(max_arrivals=1, start_time=0), conveyor, dispose()]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"}]
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1})


def test_conveyor_with_path_times_by_path_length():
    # A belt with path [straight 2, arc r=1 90 deg] at 0.5 m/s: one part travels (2 + pi/2) / 0.5 s.
    path = [{"straight": 2.0}, {"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}]
    results = api.run_model(line(belt(path=path)), replications=1)
    exit_time = float(results["entity_log"]["Exited At"].iloc[0])
    assert math.isclose(exit_time, (2 + math.pi / 2) / 0.5, abs_tol=1e-9)


def test_a_path_in_another_length_unit_is_scaled():
    doc = line(belt(path=[{"straight": 200.0}], length_unit="centimeters"))
    exit_time = float(api.run_model(doc, replications=1)["entity_log"]["Exited At"].iloc[0])
    assert math.isclose(exit_time, 2.0 / 0.5, abs_tol=1e-9)


def test_start_pose_reads_the_layout_position_and_clockwise_rotation(env):
    # one frame: the heading IS layout_rotation (90, south), no sign change
    from nagasim.core.graph_model import GraphModel
    doc = line(belt(path=[{"straight": 1.0}]))
    doc["blocks"][1]["layout_position"] = [3.0, 4.0]
    doc["blocks"][1]["layout_rotation"] = 90.0
    graph = GraphModel()
    graph.load_from_json(env, doc)
    assert graph.blocks["belt"].start_pose == cp.Pose(3.0, 4.0, 90.0)


def errors(doc, code="E_CONVEYOR_PATH"):
    return [p for p in validate_model_structured(doc) if p.code == code]


def test_path_and_length_both_set_is_refused():
    path = [{"straight": 2.0}]
    for extra in ({"length": 5.0}, {"from_node": "a", "to_node": "b"}):
        found = errors(line(belt(path=path, **extra)))
        assert [p.fields["case"] for p in found] == ["both"]
    assert errors(line(belt(path=path))) == []


def test_a_bad_piece_is_refused_with_a_plain_line():
    from nagasim.core import plainlines
    found = errors(line(belt(path=[{"straight": -1}])))
    assert [p.fields["case"] for p in found] == ["piece"]
    assert plainlines.plain(found[0])


def test_an_empty_path_keeps_the_declared_length():
    doc = line(belt(length=5.0))
    assert errors(doc) == []
    exit_time = float(api.run_model(doc, replications=1)["entity_log"]["Exited At"].iloc[0])
    assert math.isclose(exit_time, 5.0 / 0.5, abs_tol=1e-9)


def test_same_angle_compares_modulo_360():
    assert cp.same_angle(-90, 270) and cp.same_angle(180, -180) and cp.same_angle(360, 0)
    assert cp.same_angle(359.9999, -0.0001, 1e-3) and cp.same_angle(0.0, 360.0 - 1e-12)
    assert not cp.same_angle(0, 1) and not cp.same_angle(0, 180) and not cp.same_angle(10, -10)
    assert cp.same_angle(179, -179, 2.0) and not cp.same_angle(179, -179, 1.9)


# -- one frame with the floor plan (D1354, checkpoint T3 C1) ------------------------------

from nagasim.core.blocks.conveyor import belt_geometry, belt_ports  # noqa: E402
from nagasim.core.network import rotate_offset  # noqa: E402
from nagasim.core.timeunits import ModelUnits  # noqa: E402


def _block(path, rotation, position=(2.0, 3.0), **params):
    return {"name": "A", "params": {"path": path, **params}, "layout_position": list(position),
            "layout_rotation": rotation}


def _close(a, b):
    return all(math.isclose(u, v, abs_tol=1e-9) for u, v in zip(a, b))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270, 30])
def test_a_straight_belt_ends_where_d74_puts_its_far_end(rotation):
    """D74 places a block's own point at layout_position + rotate_offset(dx, dy, rotation), the
    rule the canvas and the 3D view draw with. A straight belt of L = 3 must end there."""
    out = next(p for p in belt_ports(_block([{"straight": 3.0}], rotation), ModelUnits())
               if p.name == "out")
    dx, dy = rotate_offset(3.0, 0.0, rotation)
    assert _close((out.pose.x, out.pose.y), (2.0 + dx, 3.0 + dy))
    assert cp.same_angle(out.pose.heading, rotation)


@pytest.mark.parametrize("rotation, turn, end", [
    # from (2,3) heading east, r = 1: left turns north on screen (y falls), right south
    (0, "left", (3.0, 2.0, 270.0)), (0, "right", (3.0, 4.0, 90.0)),
    # heading south: left is east on screen, right is west
    (90, "left", (3.0, 4.0, 0.0)), (90, "right", (1.0, 4.0, 180.0)),
    # heading north: left is west, right is east
    (270, "left", (1.0, 2.0, 180.0)), (270, "right", (3.0, 2.0, 0.0)),
])
def test_an_arc_turns_the_way_it_says_on_screen(rotation, turn, end):
    """A quarter arc of radius 1 from (2,3), hand-derived on screen (y south)."""
    out = next(p for p in belt_ports(
        _block([{"arc": {"radius": 1.0, "angle": 90, "turn": turn}}], rotation), ModelUnits())
        if p.name == "out")
    assert _close((out.pose.x, out.pose.y), end[:2])
    assert cp.same_angle(out.pose.heading, end[2])


def test_belt_geometry_reads_length_units_and_a_plain_length():
    """A path of 300 cm is 3 m on the floor; a divert's `at` (100 cm) scales like the path; a
    belt with no path is one straight of `length`. A non-numeric rotation reads as 0."""
    _, segs, diverts = belt_geometry(_block([{"straight": 300.0}], 0, length_unit="centimeters",
                                            diverts=[{"at": 100.0, "condition": "True"}]),
                                     ModelUnits())
    assert math.isclose(cp.path_length(segs), 3.0) and math.isclose(diverts[0]["at"], 1.0)
    _, segs, _ = belt_geometry({"name": "B", "params": {"length": 4.0}}, ModelUnits())
    assert [(seg.kind, seg.length) for seg in segs] == [("straight", 4.0)]
    start, _, _ = belt_geometry(_block([{"straight": 1.0}], "north"), ModelUnits())
    assert start == cp.Pose(2.0, 3.0, 0.0)


def test_the_loaded_rotation_is_folded_and_a_word_is_refused_plainly(env):
    """450 degrees is 90 (D74's fold, so start_pose agrees with the canvas); a rotation that is
    not a number is a validation refusal, never a crash in the build."""
    from nagasim.core.graph_model import GraphModel
    from nagasim.core.schema import ModelValidationError
    doc = line(belt(path=[{"straight": 1.0}]))
    doc["blocks"][1]["layout_rotation"] = 450.0
    graph = GraphModel()
    graph.load_from_json(env, doc)
    assert graph.blocks["belt"].start_pose.heading == 90.0
    doc["blocks"][1]["layout_rotation"] = "east"
    with pytest.raises(ModelValidationError):
        GraphModel().load_from_json(env, doc)
