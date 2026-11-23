"""Divert `angle` (conveyor plan A2, task 3). Written from the brief and spec only, before the code.

API assumptions (names may need re-pointing once the code exists):
  - A divert dict takes an optional "angle" (degrees, default 90, open range (0, 180)), next to
    "at", "condition" and "side". cp.ports(block, start, segments, diverts) reads it.
  - angle is the angle between the trunk's direction of travel and the branch's direction, so
    90 is the existing square exit and 45 leans forward (downstream). On the left the branch
    heading is  heading - angle; on the right it is  heading + angle (D74 floor frame: x east,
    y south, heading clockwise on screen, so "left" of an east-going part is north, -y).
    90 gives heading -/+ 90, exactly today's pose. The pose POINT does not depend on angle: it is
    the trunk centreline point at `at`.
  - Out of range angles (0, 180 and beyond) give E_CONVEYOR_PARAM in validate_model_structured.
  - Floor checks are read through validate_model_structured as in test_conveyor_checks.

Each expected value is hand-derived in the test's docstring; see also
.superpowers/sdd/2026-11-22-conveyor-system-A2-followups/spec-tests/task-3-derivations.md.
"""
import math

import pytest

from nagasim.core import conveyor_path as cp
from nagasim.core.schema import validate_model_structured
from test_conveyor_checks import _line, after, piece

FLOOR = ("W_CONVEYOR_OPEN_PORT", "W_CONVEYOR_OVERLAP", "W_CONVEYOR_SNAP")


def d0(start, at, side="left", **extra):
    straight = cp.parse_path([{"straight": 6.0}])
    div = {"at": at, "condition": "True", "side": side, **extra}
    ps = cp.ports("A", start, straight, [div])
    return next(p.pose for p in ps if p.name == "d0")


def check(pose, x, y, heading):
    assert math.isclose(pose.x, x, abs_tol=1e-9), pose
    assert math.isclose(pose.y, y, abs_tol=1e-9), pose
    assert cp.same_angle(pose.heading, heading), pose


@pytest.mark.parametrize("angle, side, heading", [
    (45, "left", -45), (90, "left", -90), (135, "left", -135),
    (45, "right", 45), (90, "right", 90), (135, "right", 135),
])
def test_port_pose_on_an_east_belt(angle, side, heading):
    """Straight 6 from (0,0) heading 0, divert at 2.0. The point is (2,0) for every angle. Left of
    east is north (-y): the branch heading is 0 - angle = -45, -90, -135 (that is 315, 270, 225).
    Right is 0 + angle = 45, 90, 135. Check: heading -45 has direction (cos, sin) = (0.707,
    -0.707), forward and north; -135 has (-0.707, -0.707), backward and north."""
    check(d0(cp.Pose(0, 0, 0), 2.0, side, angle=angle), 2.0, 0.0, heading)


@pytest.mark.parametrize("angle, side, heading", [
    (45, "left", -15), (90, "left", -60), (135, "left", -105),
    (45, "right", 75), (90, "right", 120), (135, "right", 165),
])
def test_port_pose_on_a_belt_heading_thirty(angle, side, heading):
    """Straight 6 from (1,2) heading 30, divert at 2.0. Point = (1 + 2 cos30, 2 + 2 sin30) =
    (1 + 1.7320508075688772, 3.0). Left: 30 - angle = -15, -60, -105. Right: 30 + angle = 75,
    120, 165."""
    check(d0(cp.Pose(1, 2, 30), 2.0, side, angle=angle),
          1 + 2 * math.cos(math.radians(30)), 3.0, heading)


@pytest.mark.parametrize("side", ["left", "right"])
def test_ninety_is_todays_pose_and_default(side):
    """No angle, angle 90 and the pre-angle expectation (heading -90 left / +90 right, point
    (2,0)) all agree, so existing models do not move."""
    omitted = d0(cp.Pose(0, 0, 0), 2.0, side)
    explicit = d0(cp.Pose(0, 0, 0), 2.0, side, angle=90)
    check(omitted, 2.0, 0.0, -90 if side == "left" else 90)
    check(explicit, omitted.x, omitted.y, omitted.heading)


def floor_pairs(doc):
    return sorted((p.code, tuple(sorted(filter(None, [p.block, (p.fields or {}).get("other")]))))
                  for p in validate_model_structured(doc) if p.code in FLOOR)


def test_a_snapped_forty_five_degree_branch_is_quiet():
    """A: straight 6 east from (0,0), divert at 2 angle 45 on the left: d0 = (2,0,-45). C starts
    at d0 (placed by `after`, so exactly on the port and heading -45) and runs 3 m: it ends at
    (2 + 3 cos45, -3 sin45) = (4.121, -2.121). Its centreline is s sin45 from A's after s
    metres, so it stays within one belt width (0.8) of A until s = 0.8 / sin45 = 1.131; the
    joint cut grows to half width / sin(angle) = 0.4 / 0.7071 = 0.566 m. The link meets the
    port within 0.05 m and 5 deg, so no SNAP; the near-trunk contact is joint contact, so no
    OVERLAP."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": 2.0, "condition": "True", "angle": 45}])
    c = after(a, "d0", "C", [{"straight": 3.0}])
    doc = _line(a, c, links=[
        {"source": "create_1", "target": "A"},
        {"source": "A", "target": "dispose_1", "source_port": "out"},
        {"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
        {"source": "C", "target": "dispose_1"}])
    assert floor_pairs(doc) == []


def test_a_belt_that_crosses_the_trunk_away_from_the_joint_still_overlaps():
    """Same A and a short C (1.5 m, ends (3.06, -1.06), 1.94 m from B's line: clear). B starts at
    (5, -3) heading 90 and runs 6 m south, crossing A's centreline at (5, 0), 3 m past the divert
    at x = 2: a true crossing, so exactly one OVERLAP, between A and B. Nothing for C, no SNAP."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": 2.0, "condition": "True", "angle": 45}])
    c = after(a, "d0", "C", [{"straight": 1.5}])
    b = piece("B", [{"straight": 6.0}], 5.0, -3.0, 90.0)
    doc = _line(a, c, b, links=[
        {"source": "create_1", "target": "A"}, {"source": "create_1", "target": "B"},
        {"source": "A", "target": "dispose_1", "source_port": "out"},
        {"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
        {"source": "C", "target": "dispose_1"}, {"source": "B", "target": "dispose_1"}])
    found = floor_pairs(doc)
    assert found == [("W_CONVEYOR_OVERLAP", ("A", "B"))], found


@pytest.mark.parametrize("angle", [0, 180, -10, 200])
def test_an_angle_outside_zero_to_one_eighty_is_refused(angle):
    """The range is open (0, 180): 0 and 180 lie along the trunk, -10 and 200 are outside. Each
    gives E_CONVEYOR_PARAM; angle 45 and 90 give none."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": 2.0, "condition": "True", "angle": angle}])
    doc = _line(a, links=[{"source": "create_1", "target": "A"},
                          {"source": "A", "target": "dispose_1"}])
    assert [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
    for ok in (45, 90):
        a["params"]["diverts"][0]["angle"] = ok
        assert not [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
