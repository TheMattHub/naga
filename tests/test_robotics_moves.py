"""PTP/LIN Move and MoveOverTime timing, the closed cycle plan, targets in cell
coordinates and the belt intercept (exact values, hand-derived)."""
import math

import numpy as np
import pytest

from robotcases import ZEROS, case_robot, gantry_robot, irb_robot

from nagasim.core.motion import move_time
from nagasim.core.robotics.chain import fk
from nagasim.core.robotics.moves import (
    ROBOT_KINDS, BeltLine, Place, RobotMoveError, belt_line, cell_point, cell_pose, display_values,
    fraction, intercept_ptp, joints_at, lin_limits, over_time, plan_cycle, ptp_limits, ptp_seconds,
    resolve_target)

irb = irb_robot()
gantry = gantry_robot()


def lin_seconds(robot, q0, q1, payload_kg=0.0):
    sdot, sddot, _samples = lin_limits(robot, q0, q1, payload_kg)
    return move_time(1.0, sdot, sddot)


def test_the_robot_kinds():
    assert ROBOT_KINDS == ("ptp", "lin")


# -- PTP ---------------------------------------------------------------------------------
def test_ptp_single_axis_trapezoid():
    assert ptp_seconds(irb, ZEROS, [90, 0, 0, 0, 0, 0]) == pytest.approx(1.15)       # 0.9 + 0.25


def test_ptp_single_axis_triangle():
    assert ptp_seconds(irb, ZEROS, [10, 0, 0, 0, 0, 0]) == pytest.approx(0.316228, abs=1e-6)


def test_ptp_the_slowest_axis_sets_the_time_and_all_finish_together():
    # axis 1: 90 deg at 100/400, axis 2: 30 deg at 90/360 -> V = min(100/90, 90/30), A = min(400/90, 360/30)
    q1 = np.array([90, 30, 0, 0, 0, 0])
    assert ptp_seconds(irb, ZEROS, q1) == pytest.approx(1.15)
    V, A = ptp_limits(irb, ZEROS, q1)
    for dq, axis in zip(q1, irb.axes):
        assert abs(dq) * V <= axis.speed + 1e-9 and abs(dq) * A <= axis.accel + 1e-9


def test_ptp_payload_derates_axis_speeds():
    half = irb_robot(payload_derate=0.5)          # payload_max 175: 175 kg -> every speed halves
    assert ptp_seconds(half, ZEROS, [90, 0, 0, 0, 0, 0], 175.0) == pytest.approx(1.8 + 0.125)
    assert ptp_seconds(half, ZEROS, [90, 0, 0, 0, 0, 0], 0.0) == pytest.approx(1.15)
    with pytest.raises(RobotMoveError) as e:
        ptp_seconds(half, ZEROS, [90, 0, 0, 0, 0, 0], 200.0)
    assert e.value.code == "E_MOTION_PAYLOAD"


def test_move_over_time_runs_exactly_its_time_and_stays_inside_the_limits():
    A = 400 / 90
    V = over_time(A, 2.0, 1.15)
    assert V == pytest.approx(0.531818, abs=1e-6) and V * 90 == pytest.approx(47.8637, abs=1e-4)
    assert move_time(1.0, V, A) == pytest.approx(2.0)


def test_move_over_time_too_short_is_E_ROBOT_MOVE_TIME_with_both_times():
    with pytest.raises(RobotMoveError) as e:
        over_time(400 / 90, 1.0, 1.15)
    assert e.value.code == "E_ROBOT_MOVE_TIME" and e.value.fields["needed"] == pytest.approx(1.15)
    assert e.value.fields["given"] == pytest.approx(1.0)


def test_fraction_follows_the_trapezoid_and_the_triangle():
    V, A = 100 / 90, 400 / 90          # 1.15 s: ramps of 0.25 s
    assert fraction(V, A, 1.15, 0.0) == 0.0 and fraction(V, A, 1.15, 1.15) == pytest.approx(1.0)
    assert fraction(V, A, 1.15, 0.25) == pytest.approx(0.5 * A * 0.25 ** 2)
    assert fraction(V, A, 1.15, 0.575) == pytest.approx(0.5)
    assert fraction(10.0, 40.0, 2 * math.sqrt(1 / 40), math.sqrt(1 / 40)) == pytest.approx(0.5)
    assert fraction(math.inf, math.inf, 0.0, 0.0) == 1.0


# -- LIN ---------------------------------------------------------------------------------
def test_lin_on_a_gantry_is_the_cartesian_trapezoid():
    # gantry axes x, y speed 1 m/s accel 2 m/s^2: 1 m along x, no curvature -> the whole budget
    assert lin_seconds(gantry, [0, 0, 0], [1, 0, 0]) == pytest.approx(1.5)


def test_lin_tcp_speed_caps_a_diagonal():
    # tcp_speed 0.5 m/s, 1 m along x and y: sdot = 0.5 / sqrt(2), sddot = 2
    assert lin_seconds(gantry_robot(tcp_speed=0.5), [0, 0, 0], [1, 1, 0]) == pytest.approx(3.00520, abs=1e-5)


LIN_A = np.array([0.0, 10.0, 0.0, 0.0, 45.0, 0.0])
LIN_B = np.array([30.0, 20.0, -10.0, 0.0, 45.0, 0.0])


def test_lin_on_serial6_keeps_every_axis_inside_its_speed_and_acceleration():
    """The true straight line timed by the plan, resampled at 1 ms through exact IK."""
    from nagasim.core.robotics.chain import ik
    from nagasim.core.robotics.moves import _lin_pose
    sdot, sddot, samples = lin_limits(irb, LIN_A, LIN_B)
    T = move_time(1.0, sdot, sddot)
    assert np.allclose(samples[0], LIN_A) and np.allclose(samples[-1], LIN_B, atol=1e-6)
    p0, p1 = fk(irb, LIN_A), fk(irb, LIN_B)
    dt = 1e-3
    qs, seed = [], LIN_A
    for t in np.arange(0.0, T + dt / 2, dt):
        seed = ik(irb, _lin_pose(p0, p1, fraction(sdot, sddot, T, min(t, T))), seed)
        qs.append(seed)
    qs = np.array(qs)
    vel = np.diff(qs, axis=0) / dt
    acc = np.diff(qs, 2, axis=0) / dt ** 2
    speed = np.array([a.speed for a in irb.axes])
    accel = np.array([a.accel for a in irb.axes])
    assert np.all(np.abs(vel).max(axis=0) <= speed * (1 + 1e-6))
    assert np.all(np.abs(acc).max(axis=0) <= accel * 1.02)
    # and the tool really runs on the line
    mid = fk(irb, joints_at(plan_cycle(irb, {"a": {"joints": list(LIN_A)}, "b": {"joints": list(LIN_B)}},
                                       [{"kind": "lin", "to": "a"}, {"kind": "lin", "to": "b"}]).moves[1], 0.5))
    on_line = p0[:3, 3] + (p1[:3, 3] - p0[:3, 3]) * 0.5
    assert np.linalg.norm(mid[:3, 3] - on_line) < 1e-3


def test_a_zero_length_move_takes_no_time_and_a_move_over_time_to_it_holds():
    assert ptp_seconds(irb, ZEROS, ZEROS) == 0.0
    plan = plan_cycle(irb, {"a": {"joints": [10, 0, 0, 0, 30, 0]}}, [{"kind": "ptp", "to": "a", "time": 2.0}])
    move = plan.moves[0]
    assert plan.errors == () and move.seconds == pytest.approx(2.0)
    assert np.allclose(joints_at(move, 0.0), joints_at(move, 0.7)) and np.allclose(joints_at(move, 1.0), [10, 0, 0, 0, 30, 0])


def test_a_move_over_time_in_minutes_is_converted():
    targets = {"a": {"joints": [0] * 6}, "b": {"joints": [90, 0, 0, 0, 0, 0]}}
    plan = plan_cycle(irb, targets, [{"kind": "ptp", "to": "a"}, {"kind": "ptp", "to": "b", "time": 0.05}], 60.0)
    assert plan.moves[1].seconds == pytest.approx(3.0) and plan.moves[0].seconds == pytest.approx(1.15)


def _errors(robot, targets, segments, **kw):
    return [(e.code, e.fields.get("case"), e.segment) for e in plan_cycle(robot, targets, segments, **kw).errors]


def test_lin_through_the_wrist_singularity_is_E_ROBOT_SINGULAR():
    targets = {"a": {"joints": [0, 0, 0, 0, 30, 0]}, "b": {"joints": [0, 0, 0, 0, 0, 0]}}
    found = _errors(irb, targets, [{"kind": "ptp", "to": "a"}, {"kind": "lin", "to": "b"}])
    assert [f[0] for f in found] == ["E_ROBOT_SINGULAR"] and found[0][2] == 1


def test_lin_between_two_wrist_configurations_is_E_ROBOT_LIN():
    # the same tool pose, wrist flipped: a straight line cannot turn the wrist over (Review Focus 1)
    flip = [0, 0, 0, 180, -30, 180]
    assert np.allclose(fk(irb, flip), fk(irb, [0, 0, 0, 0, 30, 0]), atol=1e-9)
    targets = {"a": {"joints": [0, 0, 0, 0, 30, 0]}, "b": {"joints": flip}}
    found = _errors(irb, targets, [{"kind": "ptp", "to": "a"}, {"kind": "lin", "to": "b"}])
    assert ("E_ROBOT_LIN", "config", 1) in found
    # a PTP between them is a plain joint move
    assert _errors(irb, targets, [{"kind": "ptp", "to": "a"}, {"kind": "ptp", "to": "b"}]) == []


def test_a_frame_out_of_reach_is_E_ROBOT_REACH_case_reach():
    found = _errors(irb, {"far": {"xyz": [4, 0, 1]}}, [{"kind": "ptp", "to": "far"}])
    assert found == [("E_ROBOT_REACH", "reach", 0)]


def test_a_joint_target_outside_a_range_is_E_ROBOT_REACH_case_limit():
    found = _errors(irb, {"wide": {"joints": [200, 0, 0, 0, 0, 0]}}, [{"kind": "ptp", "to": "wide"}])
    assert found == [("E_ROBOT_REACH", "limit", 0)]


def test_a_joint_target_a_delta_cannot_close_is_E_ROBOT_REACH_case_reach():
    from robotcases import CASES
    from nagasim.core.robotics.templates import template_robot
    short = template_robot("delta", {**CASES["delta"]["params"], "forearm": 0.4},
                           [{"range": [-40, 90], "speed": 100.0, "accel": 400.0}] * 3)
    targets = {"a": {"joints": [60, 60, 60]}, "b": {"joints": [-40, -40, -40]}}
    for kind in ("ptp", "lin"):
        found = _errors(short, targets, [{"kind": "ptp", "to": "a"}, {"kind": kind, "to": "b"}])
        assert found == [("E_ROBOT_REACH", "reach", 1)], kind


def test_a_tilted_frame_for_a_four_axis_robot_is_E_ROBOT_REACH_case_orientation():
    scara = case_robot("scara4")
    level = {"xyz": [0.4, 0.2, 0.25]}
    assert _errors(scara, {"t": level}, [{"kind": "ptp", "to": "t"}]) == []
    tilted = {**level, "rpy_deg": [150, 0, 0]}
    assert _errors(scara, {"t": tilted}, [{"kind": "ptp", "to": "t"}]) == [("E_ROBOT_REACH", "orientation", 0)]


def test_a_turned_gantry_still_reaches_a_target_pointing_down():
    # a yaw the gantry cannot make is not a tilt: only tilt is refused
    base = cell_pose(0, 0, 90)
    q = resolve_target(gantry, base, {"xyz": [1.0, 1.0, 1.8]}, {}, np.zeros(3))
    assert np.allclose((base @ fk(gantry, q))[:3, 3], cell_point(1.0, 1.0, 1.8), atol=1e-9)


def test_target_shape_and_name_errors():
    found = _errors(irb, {"a": {"joints": [0, 0]}, "b": "nope"},
                    [{"kind": "ptp", "to": "a"}, {"kind": "ptp", "to": "b"}, {"kind": "ptp", "to": "c"}])
    assert found == [("E_ROBOT_TARGET", "shape", 0), ("E_ROBOT_TARGET", "shape", 1),
                     ("E_ROBOT_TARGET", "unknown", 2)]


def test_a_frame_target_of_an_unplaced_robot_is_E_ROBOT_TARGET_case_place():
    found = _errors(irb, {"f": {"xyz": [1.8, 0, 0.8]}}, [{"kind": "ptp", "to": "f"}], placed=False)
    assert found == [("E_ROBOT_TARGET", "place", 0)]
    on = {"o": {"on": "belt_1"}}
    assert _errors(irb, on, [{"kind": "ptp", "to": "o"}], places={}) == [("E_ROBOT_TARGET", "place", 0)]


def test_plan_cycle_is_closed_and_deterministic():
    T = {"a": {"joints": [90, 0, 0, 0, 30, 0]}, "b": {"joints": [0, 10, 0, 0, 30, 0]}}
    SEG = [{"kind": "ptp", "to": "a"}, {"kind": "pick", "time": 1.0}, {"kind": "ptp", "to": "b"},
           {"kind": "place", "time": 1.0}]
    p1, p2 = plan_cycle(irb, T, SEG), plan_cycle(irb, T, SEG)
    assert [m.seconds for m in p1.moves.values()] == [m.seconds for m in p2.moves.values()]
    assert set(p1.moves) == {0, 2}
    assert np.allclose(p1.moves[0].q_start, p1.moves[2].q_end)    # first starts where the last ended
    assert np.allclose(p1.moves[2].q_start, p1.moves[0].q_end)
    assert [list(h) for h in p1.held] == [[90, 0, 0, 0, 30, 0]] * 2 + [[0, 10, 0, 0, 30, 0]] * 2


def test_joints_at_starts_and_ends_on_the_targets_and_is_monotone_for_ptp():
    plan = plan_cycle(irb, {"a": {"joints": [0] * 6}, "b": {"joints": [90, 30, 0, 0, 0, 0]}},
                      [{"kind": "ptp", "to": "a"}, {"kind": "ptp", "to": "b"}])
    move = plan.moves[1]
    assert np.allclose(joints_at(move, 0.0), move.q_start) and np.allclose(joints_at(move, 1.0), move.q_end)
    first = [joints_at(move, p)[0] for p in np.linspace(0, 1, 101)]
    assert all(b >= a for a, b in zip(first, first[1:]))


def test_display_values_name_the_chain_joints():
    values = display_values(irb, [10, 20, 30, 40, 50, 60])
    assert values == pytest.approx({"a1": 10, "a2": 20, "a3": 30, "a4": 40, "a5": 50, "a6": 60})
    delta = case_robot("delta")
    assert set(display_values(delta, [10, 10, 10])) and all(
        math.isfinite(v) for v in display_values(delta, [10, 10, 10]).values())


# -- cell frame, targets on blocks, intercept ---------------------------------------------
def test_the_cell_frame_conversion():
    assert np.allclose(cell_point(3, 4, 1), [3, -4, 1])
    assert np.allclose(cell_pose(0, 0, 90)[:3, 0], [0, -1, 0], atol=1e-12)   # 90 clockwise faces south
    assert np.allclose(cell_pose(2, 3, 0, 0.5)[:3, 3], [2, -3, 0.5])


def test_a_target_on_a_conveyor_end_moves_with_the_conveyor():
    base = cell_pose(13.2, 6.2, 0)          # beside the belt's end, within reach
    target = {"on": "belt_1", "at": "end", "offset": [0, 0, 0.8]}
    places = {"belt_1": Place(12.0, 5.0, 0.0, 4.0)}
    q = resolve_target(irb, base, target, places, irb.home)
    assert np.allclose((base @ fk(irb, q))[:3, 3], [14, -5, 0.8], atol=1e-6)
    moved = resolve_target(irb, base, target, {"belt_1": Place(13.0, 5.0, 0.0, 4.0)}, q)
    assert np.allclose((base @ fk(irb, moved))[:3, 3], [15, -5, 0.8], atol=1e-6)


def test_belt_line_runs_along_the_conveyor_from_its_start():
    line = belt_line(Place(12.0, 5.0, 90.0, 4.0), 0.5, 1.0)
    assert np.allclose(line.origin, [12, -3, 0], atol=1e-12) and np.allclose(line.direction, [0, -1, 0], atol=1e-12)
    assert line.speed == 0.5 and line.s0 == 1.0


Q0 = np.zeros(6)
BELT = ([1.8, -0.5, 0.8], [0, 1, 0])


def test_intercept_with_a_still_belt_is_the_ptp_time_and_a_moving_belt_meets_later():
    base = np.eye(4)
    still = intercept_ptp(irb, base, Q0, BeltLine(np.array(BELT[0]), np.array(BELT[1]), 0.0, 0.0))
    assert still.t == pytest.approx(still.seconds, abs=1e-6)
    moving = intercept_ptp(irb, base, Q0, BeltLine(np.array(BELT[0]), np.array(BELT[1]), 0.5, 0.0))
    assert moving.point[1] == pytest.approx(-0.5 + 0.5 * moving.t, abs=1e-6)
    assert moving.seconds <= moving.t + 1e-9 and np.allclose(fk(irb, moving.q)[:3, 3], moving.point, atol=1e-6)


def test_an_unreachable_belt_has_no_intercept():
    far = BeltLine(np.array([6.0, 0.0, 0.8]), np.array([0.0, 1.0, 0.0]), 0.5, 0.0)
    assert intercept_ptp(irb, np.eye(4), Q0, far, t_max=2.0) is None


def test_a_moved_robot_meets_the_same_belt_point_in_cell_coordinates():
    base = cell_pose(10, 5, 90)
    plain = intercept_ptp(irb, np.eye(4), Q0, BeltLine(np.array(BELT[0]), np.array(BELT[1]), 0.5, 0.0))
    origin = (base @ np.append(BELT[0], 1.0))[:3]
    direction = base[:3, :3] @ np.array(BELT[1], dtype=float)
    # yaw_deg is clockwise in the cell: 90 keeps the tool's yaw in the turned base frame
    moved = intercept_ptp(irb, base, Q0, BeltLine(origin, direction, 0.5, 0.0), yaw_deg=90.0)
    assert moved.t == pytest.approx(plain.t, abs=1e-9)
    assert np.allclose(moved.point, (base @ np.append(plain.point, 1.0))[:3], atol=1e-9)
