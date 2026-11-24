"""Conveyor ports, port-addressed links and snapping (conveyor system A, task 3).

Written from the brief and spec only, before the code. Every expected value is derived by hand in
the test's docstring. API assumptions:
  - cp.ports(block_name, start: Pose, segments, diverts) -> list[Port]; Port(block, name, pose, role).
    A divert is {"at": m, "condition": ..., "side": "left"|"right"} ("left" is the default).
  - cp.snap_candidates(ports_a, ports_b) -> list of (Port, Port, status); "ok" needs an out port
    of A to meet an in port of B within 0.05 m and 5 deg.
  - Link dicts take "source_port" / "target_port". One frame (D1354, D74): x east, y SOUTH,
    heading = layout_rotation, degrees clockwise on screen (0 east, 90 south). "Left" is the
    left of a part travelling along the belt as seen on screen: heading - 90. Pose values were
    re-derived for that frame with the owner's approval (checkpoint T3, C1).
  - Agent tools are called as T.call(name, **args); get_model_json returns {"layout": {...}}.
"""
import math

import pytest

from nagasim import agent_tools as T
from nagasim import api
from nagasim.core import conveyor_path as cp

from conftest import create, delay, dispose, layout


def by_name(plist, name):
    return next(p for p in plist if p.name == name)


def near(pose, x, y, heading):
    assert math.isclose(pose.x, x, abs_tol=1e-9), pose
    assert math.isclose(pose.y, y, abs_tol=1e-9), pose
    assert cp.same_angle(pose.heading, heading), pose        # -90 and 270 are one direction


def straight(length):
    return cp.parse_path([{"straight": length}])


# -- port poses ----------------------------------------------------------------

def test_port_poses_of_straight_with_left_divert():
    """Straight 3.0 from (0,0) heading 0, divert 0 at 1.0 on the left.
    in = start (0,0,0) role in; out = (3,0,0) role out; d0 = point at s=1 is (1,0), and "left" of
    a part travelling east is north on screen (-y), so it faces out of the belt at heading
    0 - 90 = -90."""
    ps = cp.ports("A", cp.Pose(0, 0, 0), straight(3.0), [{"at": 1.0, "condition": "True"}])
    assert {p.name for p in ps} == {"in", "out", "d0"}
    assert all(p.block == "A" for p in ps)
    near(by_name(ps, "in").pose, 0, 0, 0)
    near(by_name(ps, "out").pose, 3, 0, 0)
    near(by_name(ps, "d0").pose, 1, 0, -90)
    assert by_name(ps, "in").role == "in" and by_name(ps, "out").role == "out"


def test_right_divert_faces_plus_ninety():
    """Same belt, side "right": the exit faces heading 0 + 90 = 90 (south on screen, +y side);
    the pose point is still (1,0). Without a side the default is left (heading -90)."""
    right = cp.ports("A", cp.Pose(0, 0, 0), straight(3.0),
                     [{"at": 1.0, "condition": "True", "side": "right"}])
    near(by_name(right, "d0").pose, 1, 0, 90)
    default = cp.ports("A", cp.Pose(0, 0, 0), straight(3.0), [{"at": 1.0, "condition": "True"}])
    near(by_name(default, "d0").pose, 1, 0, -90)


def test_divert_headings_follow_the_belt_heading():
    """Belt starting at (2,1) heading 90 (south, straight 3), divert 0 at 1.0 left: the point is
    (2, 1+1) = (2,2); left of a part travelling south is east on screen, heading 90 - 90 = 0.
    Right gives 180 (west). Out = (2,4,90)."""
    ps = cp.ports("A", cp.Pose(2, 1, 90), straight(3.0),
                  [{"at": 1.0, "condition": "True"}, {"at": 1.0, "condition": "True", "side": "right"}])
    near(by_name(ps, "d0").pose, 2, 2, 0)
    near(by_name(ps, "d1").pose, 2, 2, 180)
    near(by_name(ps, "out").pose, 2, 4, 90)


def test_ports_of_a_path_with_a_left_arc():
    """Path straight 2 then left arc r=1, 90 deg from (0,0,0): the arc turns north on screen,
    end_pose (3,-1,-90) (as in test_conveyor_path). in (0,0,0); out equals end_pose. No
    diverts, so two ports."""
    segs = cp.parse_path([{"straight": 2.0}, {"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}])
    ps = cp.ports("A", cp.Pose(0, 0, 0), segs, [])
    assert {p.name for p in ps} == {"in", "out"}
    near(by_name(ps, "in").pose, 0, 0, 0)
    near(by_name(ps, "out").pose, 3, -1, -90)
    e = cp.end_pose(cp.Pose(0, 0, 0), segs)
    near(by_name(ps, "out").pose, e.x, e.y, e.heading)


# -- snapping ------------------------------------------------------------------

def port(name, x, y, heading, role, block="P"):
    return cp.Port(block, name, cp.Pose(x, y, heading), role)


def statuses(a, b):
    return [(pa.name, pb.name, st) for pa, pb, st in cp.snap_candidates(a, b)]


def test_snap_ok_and_misaligned():                      # Review Focus 4
    """out (3,0,0) meets in (3.02,0,2): distance 0.02 < 0.05 and heading diff 2 < 5: ok."""
    out = port("out", 3, 0, 0, "out", "A")
    assert statuses([out], [port("in", 3.02, 0, 2, "in", "B")]) == [("out", "in", "ok")]


def test_facing_the_wrong_way_or_crossing_is_never_ok():          # Review Focus 4
    """Same point (3,0): heading 0 vs 180 differ by 180 deg (head-on, not a continuation); 0 vs
    90 and 0 vs -90 differ by 90 deg. None of them may be ok; the right-angle ones are
    misaligned. Beyond tolerance in distance (0.06 > 0.05) is not ok either, and a heading diff
    of 10 deg (> 5) is not ok."""
    out = port("out", 3, 0, 0, "out", "A")
    for heading in (180, 90, -90, 10):
        res = statuses([out], [port("in", 3, 0, heading, "in", "B")])
        assert all(st != "ok" for _, _, st in res), (heading, res)
    assert statuses([out], [port("in", 3, 0, 90, "in", "B")]) == [("out", "in", "misaligned")]
    assert statuses([out], [port("in", 3, 0, -90, "in", "B")]) == [("out", "in", "misaligned")]
    assert all(st != "ok" for _, _, st in statuses([out], [port("in", 3.06, 0, 0, "in", "B")]))


def test_heading_difference_wraps_around():
    """179 and -179 are 2 deg apart (360 - 358), so they agree within 5 deg: ok."""
    out = port("out", 0, 0, 179, "out", "A")
    assert statuses([out], [port("in", 0, 0, -179, "in", "B")]) == [("out", "in", "ok")]


def test_two_outs_are_same_role():
    """out meets out at the same point and heading: they cannot join, status same_role."""
    res = statuses([port("out", 3, 0, 0, "out", "A")], [port("out", 3, 0, 0, "out", "B")])
    assert res == [("out", "out", "same_role")]


def test_snapping_whole_belts_finds_only_the_matching_pair():
    """A: straight 3 from (0,0,0) with a left divert at 1 (d0 (1,0,-90), out (3,0,0)). B: straight 2
    from (3,0,0): in (3,0,0). Only A.out/B.in is ok; A.d0 is 2 m away from B.in."""
    a = cp.ports("A", cp.Pose(0, 0, 0), straight(3.0), [{"at": 1.0, "condition": "True"}])
    b = cp.ports("B", cp.Pose(3, 0, 0), straight(2.0), [])
    oks = [(pa.block, pa.name, pb.block, pb.name) for pa, pb, st in cp.snap_candidates(a, b)
           if st == "ok"]
    assert oks == [("A", "out", "B", "in")]


# -- port-addressed links ------------------------------------------------------

def belt(diverts, **extra):
    params = {"length": 10.0, "speed": 1.0, "spacing": 1.0, "accumulating": True,
              "exit_resource": "", "from_node": "", "to_node": "", "diverts": diverts, **extra}
    return {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [1, 0],
            "params": params}


def routed_exit(d0_condition, d1_condition, links_after_source):
    """One part, belt length 10 at 1 m/s (it is at x at t = x). d0 at 3, d1 at 6. Targets are
    DELAYs of distinct length: end -> 10, d0 -> 1000, d1 -> 100. Returns the part's exit time."""
    blocks = [create(max_arrivals=1, start_time=0), belt([
        {"at": 3.0, "condition": d0_condition}, {"at": 6.0, "condition": d1_condition}]),
        delay("to_end", 10.0), delay("to_d0", 1000.0), delay("to_d1", 100.0),
        dispose("x_end"), dispose("x_d0"), dispose("x_d1")]
    links = [{"source": "create_1", "target": "belt"}, *links_after_source,
             {"source": "to_end", "target": "x_end"}, {"source": "to_d0", "target": "x_d0"},
             {"source": "to_d1", "target": "x_d1"}]
    doc = layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1})
    log = api.run_model(doc, replications=1)["entity_log"]
    assert len(log) == 1
    return float(log["Exited At"].iloc[0])


PORT_LINKS = [{"source": "belt", "target": "to_d1", "source_port": "d1"},
              {"source": "belt", "target": "to_end", "source_port": "out"},
              {"source": "belt", "target": "to_d0", "source_port": "d0"}]


@pytest.mark.parametrize("c0, c1, expected", [
    ("False", "True", 6 + 100),      # skips d0, taken at d1 (x = 6): 6 s + 100 s delay
    ("True", "True", 3 + 1000),      # taken at the first divert d0 (x = 3): 3 s + 1000 s delay
    ("False", "False", 10 + 10),     # never diverted: leaves the end at x = 10: 10 s + 10 s delay
])
def test_port_links_override_positional_order(c0, c1, expected):
    """Links are listed d1, out, d0 by source_port; positional order (link 0 = end) would send the
    part to the wrong delay. Hand-derived exit times in the parametrize ids above."""
    assert math.isclose(routed_exit(c0, c1, PORT_LINKS), expected, abs_tol=1e-9)


def test_links_without_ports_keep_the_positional_rule():
    """No source_port: link 0 = end, link 1 = d0, link 2 = d1, as today. Same conditions as above
    give the same hand-derived times with the targets in positional order end, d0, d1."""
    plain = [{"source": "belt", "target": "to_end"}, {"source": "belt", "target": "to_d0"},
             {"source": "belt", "target": "to_d1"}]
    assert math.isclose(routed_exit("False", "True", plain), 106.0, abs_tol=1e-9)
    assert math.isclose(routed_exit("True", "True", plain), 1003.0, abs_tol=1e-9)
    assert math.isclose(routed_exit("False", "False", plain), 20.0, abs_tol=1e-9)


# -- agent tools ---------------------------------------------------------------

@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def ok(result):
    assert "error" not in result, result.get("error")
    return result


def layout_of(model):
    return ok(T.call("get_model_json", model=model))["layout"]


def block_of(model, name):
    return next(b for b in layout_of(model)["blocks"] if b["name"] == name)


def test_place_piece_snaps_and_links(ws):
    """A at (1,2) rotation 90 (south), straight 3: out pose = (1, 2+3) = (1,5) rotation 90. B
    placed with snap_to "A.out" and deliberately wrong x/y/rotation (0,0,0): the snap wins, so
    B starts at (1,5): layout_position [1,5]; layout_rotation 90. A itself has rotation 90.
    A link A -> B with source_port out and target_port in exists. (R17: `type` is the conveyor
    type, "" here, and the block is always a CONVEYOR.)"""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}],
              x=1.0, y=2.0, rotation=90.0))
    ok(T.call("place_piece", model="m", name="B", segments=[{"straight": 2.0}],
              x=0.0, y=0.0, rotation=0.0, snap_to="A.out"))
    a, b = block_of("m", "A"), block_of("m", "B")
    assert a["type"] == "CONVEYOR" and b["type"] == "CONVEYOR"
    assert all(math.isclose(u, v, abs_tol=1e-9) for u, v in zip(a["layout_position"], [1, 2]))
    assert cp.same_angle(a["layout_rotation"], 90.0)
    assert all(math.isclose(u, v, abs_tol=1e-9) for u, v in zip(b["layout_position"], [1, 5]))
    assert cp.same_angle(b["layout_rotation"], 90.0)
    links = [lk for lk in layout_of("m")["links"] if lk["source"] == "A" and lk["target"] == "B"]
    assert len(links) == 1
    assert links[0]["source_port"] == "out" and links[0]["target_port"] == "in"


def test_set_layout_writes_position_and_rotation(ws):
    """set_layout(block, x, y, rotation): layout_position = [x, y], layout_rotation = rotation
    (degrees clockwise on screen, which is also the belt's heading)."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    ok(T.call("set_layout", model="m", block="A", x=4.5, y=-2.0, rotation=30.0))
    a = block_of("m", "A")
    assert math.isclose(a["layout_position"][0], 4.5, abs_tol=1e-9)
    assert math.isclose(a["layout_position"][1], -2.0, abs_tol=1e-9)
    assert math.isclose(a["layout_rotation"], 30.0, abs_tol=1e-9)


# -- shared ports and placement (conveyor plan B, task 1) --------------------------------------

EXITS = [{"port": "left", "angle": -90}, {"port": "straight", "angle": 0}]


def test_transfer_ports_are_the_tools_ports(ws):
    """A transfer at (2, 0), rotation 0, exits left -90 and straight 0: in (2,0,0), left (2,0,270),
    straight (2,0,0); `get_ports` on the same model lists the same poses."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_transfer", model="m", name="X", exits=EXITS, x=2.0, y=0.0, rotation=0.0))
    entry = block_of("m", "X")
    ps = cp.transfer_ports("X", entry["layout_position"], entry.get("layout_rotation", 0.0),
                           entry["params"])
    assert [(p.name, p.role) for p in ps] == [("in", "in"), ("left", "out"), ("straight", "out")]
    near(by_name(ps, "in").pose, 2, 0, 0)
    near(by_name(ps, "left").pose, 2, 0, 270)
    near(by_name(ps, "straight").pose, 2, 0, 0)
    listed = ok(T.call("get_ports", model="m", block="X"))["ports"]
    for p in ps:
        row = next(r for r in listed if r["name"] == p.name)
        assert (row["x"], row["y"], row["role"]) == (p.pose.x, p.pose.y, p.role)
        assert cp.same_angle(row["heading"], p.pose.heading)


def _doc_of(model):
    from nagasim.core.document import ModelDocument
    return ModelDocument(layout_of(model))


def _comparable(model_layout):
    blocks = {b["name"]: (b["type"], b["params"], b.get("position"), b.get("layout_position"),
                          b.get("layout_rotation", 0.0))
              for b in model_layout["blocks"]}
    links = sorted((lk["source"], lk["target"], lk.get("source_port"), lk.get("target_port"))
                   for lk in model_layout["links"])
    return blocks, links


def test_add_belt_piece_matches_place_piece(ws):
    """The tool's write and the shared helper's write give the same layout, block for block and
    link for link: a roller piece B snapped to A.out."""
    from nagasim.core import floor as D
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}],
              x=1.0, y=2.0, rotation=90.0))
    copy = _doc_of("m")
    ok(T.call("place_piece", model="m", name="B", segments=[{"straight": 2.0}], type="roller",
              snap_to="A.out"))
    source = next(p for p in D.piece_ports(copy, "A") if p.name == "out")
    D.add_belt_piece(copy, [{"straight": 2.0}], "roller", source=source, name="B",
                     position=None)
    assert _comparable(copy.to_layout()) == _comparable(layout_of("m"))


def test_add_belt_piece_position_speed_and_pose(ws):
    """An explicit schematic position and a speed are kept; the pose is the layout pose."""
    from nagasim.core import floor as D
    doc = _doc_of(_new("m"))
    name = D.add_belt_piece(doc, [{"straight": 1.5}], pose=(4.0, 5.0, 450.0), speed=0.8,
                            name="P", position=(10.0, 20.0))
    b = doc.blocks[name]
    assert tuple(b.position) == (10.0, 20.0)
    assert b.params["path"] == [{"straight": 1.5}] and b.params["speed"] == 0.8
    assert tuple(b.layout_position) == (4.0, 5.0) and cp.same_angle(b.layout_rotation, 90.0)


def _new(name):
    ok(T.call("new_model", name=name))
    return name


def test_add_transfer_piece_matches_place_transfer(ws):
    from nagasim.core import floor as D
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    copy = _doc_of("m")
    ok(T.call("place_transfer", model="m", name="X", mechanism="turntable", exits=EXITS,
              snap_to="A.out"))
    source = next(p for p in D.piece_ports(copy, "A") if p.name == "out")
    D.add_transfer_piece(copy, "turntable", EXITS, source=source, name="X")
    assert _comparable(copy.to_layout()) == _comparable(layout_of("m"))


def test_add_transfer_piece_default_exit_and_position_match_place_transfer(ws):
    """No exits given: the helper's default exit equals the tool's; an explicit schematic
    position is kept."""
    from nagasim.core import floor as D
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    copy = _doc_of("m")
    ok(T.call("place_transfer", model="m", name="X", snap_to="A.out"))
    source = next(p for p in D.piece_ports(copy, "A") if p.name == "out")
    D.add_transfer_piece(copy, source=source, name="X")
    assert _comparable(copy.to_layout()) == _comparable(layout_of("m"))
    name = D.add_transfer_piece(copy, name="Y", position=(30.0, 40.0))
    assert tuple(copy.blocks[name].position) == (30.0, 40.0)


def test_piece_ports_by_block_type(ws):
    from nagasim.core import floor as D
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}]))
    ok(T.call("place_transfer", model="m", name="X", exits=EXITS, x=1.0, y=1.0))
    doc = _doc_of("m")
    assert {p.name for p in D.piece_ports(doc, "A")} == {"in", "out"}
    assert {p.name for p in D.piece_ports(doc, "X")} == {"in", "left", "straight"}
    doc.add_block("DELAY", (0, 0), "dl")
    assert D.piece_ports(doc, "dl") == []


def test_path_polyline_straight_and_arc():
    """A 2 m straight is its two ends; a 90 deg left arc of radius 1 from (0,0) east, in 10 deg
    chords, ends at (1, -1) (left is north on screen, -y)."""
    assert cp.path_polyline(cp.Pose(0, 0, 0), cp.parse_path([{"straight": 2.0}])) == [(0, 0), (2, 0)]
    pts = cp.path_polyline(cp.Pose(0, 0, 0), cp.parse_path(
        [{"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}]))
    assert len(pts) == 10
    assert math.isclose(pts[-1][0], 1.0, abs_tol=1e-9) and math.isclose(pts[-1][1], -1.0,
                                                                          abs_tol=1e-9)
