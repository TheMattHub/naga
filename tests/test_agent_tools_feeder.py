"""Agent tool gaps, Task 2 (spec G3, tier oracle): `place_feeder` and `place_piece(gap=...)`.

Written from the spec alone, before the code. Every expected pose is worked out by hand in the
test's docstring; nothing here calls `conveyor_path` or `floor_geometry` to compute a value.

Conventions (floor plan, checked against `place_piece`/`get_ports` today):
- metres, south is +y; a heading is degrees clockwise on screen, 0 east, 90 south;
- a direction of travel h is the vector (cos h, sin h) in those screen axes, so the ordinary
  rotation (x cos p - y sin p, x sin p + y cos p) turns a local offset by heading p;
- "left" of travel h is the normal (sin h, -cos h): left of an east-running belt is north (-y),
  as a left divert's port faces h - angle and a left arc from heading 0 ends at heading 270.

Readings of the spec chosen here (each one a ruling if the builder reads it otherwise):
- R1 side: side="left" means the feeder lies on the trunk's left (north of an east trunk) and
  travels towards the trunk, so its end heading is trunk heading + angle; side="right" gives
  trunk heading - angle.
- R2 lean: angle < 90 "leans the feeder downstream" means its direction of travel has a
  downstream component (it mirrors a divert branch, whose exit leans downstream), so its body
  lies upstream of the merge point.
- R3 the angle is taken from the trunk's tangent at *at* (on an arc, the local heading).
- R4 the feeder's segments are in the model's length unit (it is a new belt that sets no
  length_unit, like place_piece); only *at* is in the trunk's unit.
- R5 the feeder's link is the editor's merge link: source_port "out", no target_port (it enters
  at the merge point, not at "in"), no delay; exactly one such link.
- R6 gap without snap_to is refused with E_PLACE_GAP (the spec names no code for it).
- R7 the gap link carries source_port "out" and target_port "in", as a snapped piece's link.
- R8 a trunk "not drawn on the floor" is tested with a CONVEYOR that has no path and length 0.

The tests were xfail(strict=True) until Task 2 was built; no expected value was changed to make
them pass, and a disagreement goes to the coordinator.
"""
import math

import pytest

from nagasim import agent_tools as T

R2 = math.sqrt(2.0)
S3 = math.sqrt(3.0)
TOL = 1e-5


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def ok(result):
    assert "error" not in result, result.get("error")
    return result


def same_heading(a, b, tol=1e-4):
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0) <= tol


def layout(model="m"):
    return ok(T.call("get_model_json", model=model))["layout"]


def block(model, name):
    return next(b for b in layout(model)["blocks"] if b["name"] == name)


def ports(model, name):
    return {p["name"]: p for p in ok(T.call("get_ports", model=model, block=name))["ports"]}


def assert_pose(model, name, start, end):
    """The belt's entry (layout pose and its "in" port) is *start* and its "out" port is *end*,
    each (x, y, heading)."""
    got = block(model, name)
    assert got["layout_position"] == pytest.approx(list(start[:2]), abs=TOL)
    assert same_heading(got["layout_rotation"], start[2])
    have = ports(model, name)
    for port, want in (("in", start), ("out", end)):
        assert (have[port]["x"], have[port]["y"]) == pytest.approx(want[:2], abs=TOL), port
        assert same_heading(have[port]["heading"], want[2]), port


def east_trunk(model="m"):
    """Trunk T: 10 m straight east from (0, 0)."""
    ok(T.call("new_model", name=model))
    ok(T.call("place_piece", model=model, name="T", segments=[{"straight": 10.0}]))


def refused(code, model="m", tool="place_feeder", **kw):
    """*tool* answers *code* in one sentence and leaves the model as it was."""
    before = layout(model)
    error = T.call(tool, model=model, **kw)["error"]
    assert error["code"] == code, error
    assert "\n" not in error["message"]
    assert layout(model) == before


# -- poses -------------------------------------------------------------------------------------

def test_square_on_from_the_left_by_default(ws):
    """East trunk T, at = 4, defaults side="left", angle=90; feeder F straight 2.5.
    Merge point (4, 0). R1: heading 0 + 90 = 90 (south, coming from the north side).
    Start = (4, 0) - 2.5 (cos 90, sin 90) = (4, -2.5), heading 90. End (4, 0, 90)."""
    east_trunk()
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T", at=4.0))
    assert_pose("m", "F", (4.0, -2.5, 90.0), (4.0, 0.0, 90.0))


def test_square_on_from_the_right(ws):
    """As above with side="right": heading 0 - 90 = 270 (north, coming from the south side).
    Start = (4, 0) - 2.5 (cos 270, sin 270) = (4, 0) - 2.5 (0, -1) = (4, 2.5), heading 270."""
    east_trunk()
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=4.0, side="right"))
    assert_pose("m", "F", (4.0, 2.5, 270.0), (4.0, 0.0, 270.0))


def test_arc_plus_straight_feeder(ws):
    """Feeder: left arc r 1.5, 90 deg, then straight 2.5; onto east trunk at 4 from the left.
    Path from the origin heading 0: the left arc's centre is (0, -1.5) and it ends at
    (1.5, -1.5) heading 270; the straight runs north to (1.5, -4.0), heading 270.
    Target end (4, 0) heading 90, so the start heading h0 = 90 - 270 = -180 = 180.
    Offset (1.5, -4) turned by 180 is (-1.5, 4); start = (4, 0) - (-1.5, 4) = (5.5, -4).
    Forward check: west from (5.5, -4), left is south, arc centre (5.5, -2.5), ends (4, -2.5)
    heading 90; 2.5 south gives (4, 0)."""
    east_trunk()
    ok(T.call("place_feeder", model="m", name="F", trunk="T", at=4.0, side="left",
              segments=[{"arc": {"radius": 1.5, "angle": 90, "turn": "left"}},
                        {"straight": 2.5}]))
    assert_pose("m", "F", (5.5, -4.0, 180.0), (4.0, 0.0, 90.0))


@pytest.mark.parametrize("side, start", [
    ("left", (4.0 - 1.25 * R2, -1.25 * R2, 45.0)),
    ("right", (4.0 - 1.25 * R2, 1.25 * R2, 315.0)),
])
def test_a_45_degree_lean_points_downstream(ws, side, start):
    """East trunk, at 4, angle 45, feeder straight 2.5. R2: the feeder travels with a downstream
    (+x) component. Left: heading 0 + 45 = 45; start = (4, 0) - 2.5 (cos 45, sin 45)
    = (4 - 1.25 sqrt2, -1.25 sqrt2) = (2.232233, -1.767767). Right: heading 0 - 45 = 315;
    start = (4 - 1.25 sqrt2, +1.25 sqrt2) = (2.232233, 1.767767).
    The other reading (body downstream of the point) would give heading 135 / 225 and start
    (5.767767, -1.767767) / (5.767767, 1.767767); this test rules it out."""
    east_trunk()
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=4.0, side=side, angle=45))
    assert_pose("m", "F", start, (4.0, 0.0, start[2]))


def test_a_trunk_turned_90_and_away_from_the_origin(ws):
    """Trunk T: straight 10 from (3, 5) heading 90 (south); at 4 -> point (3, 9).
    Left of south travel is (sin 90, -cos 90) = (1, 0), east. R1: heading 90 + 90 = 180 (west).
    Start = (3, 9) - 2.5 (cos 180, sin 180) = (3, 9) - (-2.5, 0) = (5.5, 9), heading 180."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="T", segments=[{"straight": 10.0}], x=3.0, y=5.0,
              rotation=90.0))
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=4.0, side="left"))
    assert_pose("m", "F", (5.5, 9.0, 180.0), (3.0, 9.0, 180.0))


def test_the_merge_on_the_trunks_own_arc(ws):
    """Trunk T from (0, 0) heading 0: straight 2, then a right arc r 4, 90 deg (its out port is
    (6, 4) heading 90). at = 2 + pi: pi along the arc, a sweep of pi / 4 = 45 deg.
    Right normal at heading 0 is (-sin 0, cos 0) = (0, 1), so the centre is (2, 4); at 45 deg
    the heading is 45 and the point is (2, 4) - 4 (-sin 45, cos 45) = (2 + 2 sqrt2, 4 - 2 sqrt2)
    = (4.828427, 1.171573). R3: the angle is from the tangent there, so from the left (the
    curve's outside) the heading is 45 + 90 = 135, pointing at the centre.
    Start = P - 2.5 (cos 135, sin 135) = P + 1.25 sqrt2 (1, -1)
    = (2 + 3.25 sqrt2, 4 - 3.25 sqrt2) = (6.596194, -0.596194), heading 135."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="T", segments=[
        {"straight": 2.0}, {"arc": {"radius": 4.0, "angle": 90, "turn": "right"}}]))
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=2.0 + math.pi, side="left"))
    point = (2.0 + 2.0 * R2, 4.0 - 2.0 * R2)
    assert_pose("m", "F", (2.0 + 3.25 * R2, 4.0 - 3.25 * R2, 135.0), (*point, 135.0))


def test_a_millimetre_trunk_in_a_metre_model(ws):
    """Model in metres; trunk T's length_unit is millimeters, path straight 10000 (10 m east).
    at = 4000 is in the trunk's unit: 4 m, point (4, 0). R4: the feeder's straight 2.5 is in
    the model's metres. Start (4, -2.5) heading 90, as the square-on case; the merge entry keeps
    at = 4000 (were *at* read in metres, 4000 would lie off the 10 m belt and be refused)."""
    east_trunk()
    ok(T.call("update_block", model="m", name="T",
              params={"length_unit": "millimeters", "path": [{"straight": 10000.0}]}))
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=4000.0))
    assert_pose("m", "F", (4.0, -2.5, 90.0), (4.0, 0.0, 90.0))
    merges = block("m", "T")["params"]["merges"]
    assert [(m["at"], m["from"]) for m in merges] == [(4000.0, "F")]


# -- what it writes ----------------------------------------------------------------------------

def test_the_merge_entry_and_the_link_in_one_undo_entry(ws):
    """One call adds block F, the trunk's merge {"at": 4, "from": "F"} and one undelayed link
    F -> T (R5: source_port "out", no target_port). One `undo` gives back the layout as it was
    before the call, exactly."""
    east_trunk()
    before = layout()
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T",
              at=4.0))
    after = layout()
    assert [(m["at"], m["from"]) for m in block("m", "T")["params"]["merges"]] == [(4.0, "F")]
    links = [k for k in after["links"] if k["source"] == "F"]
    assert len(links) == 1
    assert links[0]["target"] == "T" and links[0].get("delay") is None
    assert links[0].get("source_port") == "out"
    assert links[0].get("target_port") in (None, "")
    ok(T.call("undo", model="m"))
    assert layout() == before


@pytest.mark.parametrize("kw, end", [
    ({"at": 4.0}, (4.0, 0.0)),
    ({"at": 4.0, "side": "right", "angle": 45}, (4.0, 0.0)),
    ({"at": 6.5, "side": "left", "angle": 30}, (6.5, 0.0)),
])
def test_the_editor_draws_the_result_as_joined(ws, kw, end):
    """`conveyor_draw.meets`: the feeder's out port lands on the hand-derived merge point (at, 0)
    of the east trunk, well inside the untyped band (0.8 m wide); the editor's own
    `merge_meets` and `merge_joined` on the written document agree."""
    from nagasim.core import floor as conveyor_draw     # the engine owns the rule (T4, D1424)
    east_trunk()
    ok(T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}], trunk="T", **kw))
    out = ports("m", "F")["out"]
    assert (out["x"], out["y"]) == pytest.approx(end, abs=TOL)
    assert conveyor_draw.meets((out["x"], out["y"]), end, 0.8)
    doc = T.workspace().document("m")
    merge = next(m for m in doc.blocks["T"].params["merges"] if m["from"] == "F")
    assert conveyor_draw.merge_meets(doc, "T", merge)
    assert conveyor_draw.merge_joined(doc, "T", "F")


# -- refusals ----------------------------------------------------------------------------------

def test_refused_trunks(ws):
    """E_UNKNOWN_BLOCK: no such block; a DISPOSE; a CONVEYOR not drawn on the floor (R8)."""
    east_trunk()
    ok(T.call("add_block", model="m", type="DISPOSE", name="out"))
    ok(T.call("add_block", model="m", type="CONVEYOR", name="N"))
    for trunk in ("nope", "out", "N"):
        refused("E_UNKNOWN_BLOCK", name="F", segments=[{"straight": 2.5}], trunk=trunk, at=4.0)


def test_refused_merge_points(ws):
    """E_PLACE_MERGE: *at* outside the open interval (0, 10) of the 10 m trunk, on a divert's
    point (4), or on another merge's point (6, taken by G first)."""
    east_trunk()
    ok(T.call("update_block", model="m", name="T", params={"diverts": [{"at": 4.0}]}))
    ok(T.call("place_feeder", model="m", name="G", segments=[{"straight": 2.5}], trunk="T",
              at=6.0))
    for at in (0.0, 10.0, -1.0, 12.0, 4.0, 6.0):
        refused("E_PLACE_MERGE", name="F", segments=[{"straight": 2.5}], trunk="T", at=at)


def test_refused_angles(ws):
    """E_PLACE_ANGLE: the angle must lie in (0, 90]."""
    east_trunk()
    for angle in (0, -10, 90.5, 135, 180):
        refused("E_PLACE_ANGLE", name="F", segments=[{"straight": 2.5}], trunk="T", at=4.0,
                angle=angle)


def test_refused_segments(ws):
    """E_PLACE_PATH: a segment kind that does not exist; an arc turning neither left nor right."""
    east_trunk()
    for segments in ([{"wiggle": 1.0}],
                     [{"arc": {"radius": 1.0, "angle": 90, "turn": "up"}}]):
        refused("E_PLACE_PATH", name="F", segments=segments, trunk="T", at=4.0)


# -- place_piece(gap=...) ------------------------------------------------------------------------

@pytest.mark.parametrize("x, y, rotation, out, entry", [
    # A: straight 3 from (1, 2) heading 90 -> out (1, 5); 4 m further south: (1, 9).
    (1.0, 2.0, 90.0, (1.0, 5.0), (1.0, 9.0)),
    # A: straight 3 from (0.5, -1) heading 30 -> out (0.5 + 1.5 sqrt3, 0.5);
    # 4 m further along (cos 30, sin 30) = (sqrt3/2, 1/2): (0.5 + 3.5 sqrt3, 2.5).
    (0.5, -1.0, 30.0, (0.5 + 1.5 * S3, 0.5), (0.5 + 3.5 * S3, 2.5)),
])
def test_gap_places_the_entry_beyond_the_port_with_an_area_link(ws, x, y, rotation, out, entry):
    """place_piece P (straight 2) with snap_to "A.out", gap 4: P's entry is 4 m past A.out along
    A's heading, facing the same way, so P's out is 2 m further on. The link A -> P carries the
    delay and capacity as `connect` writes them (R7: ports "out" -> "in")."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}], x=x, y=y,
              rotation=rotation))
    assert (ports("m", "A")["out"]["x"], ports("m", "A")["out"]["y"]) == pytest.approx(out, abs=TOL)
    delay = {"distribution": "Constant", "dist_params": {"value": 2}}
    ok(T.call("place_piece", model="m", name="P", segments=[{"straight": 2.0}], snap_to="A.out",
              gap=4.0, delay=delay, capacity=4))
    h = math.radians(rotation)
    end = (entry[0] + 2.0 * math.cos(h), entry[1] + 2.0 * math.sin(h))
    assert_pose("m", "P", (*entry, rotation), (*end, rotation))
    link = next(k for k in layout()["links"] if k["source"] == "A" and k["target"] == "P")
    assert (link.get("source_port"), link.get("target_port")) == ("out", "in")
    assert link["delay"] == delay and link["capacity"] == 4


def test_a_gap_needs_a_delay_and_a_snap(ws):
    """E_PLACE_GAP: gap > 0 without a delay (an undelayed link across a gap would teleport
    parts), with or without a capacity; and gap without snap_to (R6)."""
    ok(T.call("new_model", name="m"))
    ok(T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}], rotation=90.0))
    refused("E_PLACE_GAP", tool="place_piece", name="P", segments=[{"straight": 2.0}],
            snap_to="A.out", gap=4.0)
    refused("E_PLACE_GAP", tool="place_piece", name="P", segments=[{"straight": 2.0}],
            snap_to="A.out", gap=4.0, capacity=4)
    refused("E_PLACE_GAP", tool="place_piece", name="P", segments=[{"straight": 2.0}],
            gap=4.0, delay={"distribution": "Constant", "dist_params": {"value": 2}})
