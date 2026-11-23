"""Kinematic chain core: FK by product of exponentials, coupling, Jacobian, DLS IK."""
import numpy as np
import pytest

from nagasim.core.robotics.chain import (
    IK_POS_TOL, chain_robot, coupling_matrix, fk, ik, ik_numeric, jacobian, joint_values, pose,
    rot_log, task_sigma_min, within_limits)

PLANAR = dict(joints=[{"name": "a", "type": "revolute", "origin": [0, 0, 0], "axis": [0, 0, 1]},
                      {"name": "b", "type": "revolute", "origin": [1.0, 0, 0], "axis": [0, 0, 1], "parent": "a"}],
              axes=[{"name": "a", "range": [-180, 180], "speed": 90, "accel": 360},
                    {"name": "b", "range": [-180, 180], "speed": 90, "accel": 360}],
              tool={"xyz": [0.5, 0, 0], "rpy_deg": [0, 0, 0]})


def _ax(name, lo=-170, hi=170):
    return {"name": name, "range": [lo, hi], "speed": 100, "accel": 400}


# A six-joint chain with tilted axes and one prismatic joint.
MIXED = dict(
    joints=[{"name": "j1", "type": "revolute", "origin": [0, 0, 0], "axis": [0, 0, 1]},
            {"name": "j2", "type": "revolute", "origin": [0.2, 0, 0.5], "axis": [0, 1, 0]},
            {"name": "j3", "type": "prismatic", "origin": [0.2, 0, 1.2], "axis": [1, 0, 0.2]},
            {"name": "j4", "type": "revolute", "origin": [0.9, 0.1, 1.2], "axis": [1, 0, 0]},
            {"name": "j5", "type": "revolute", "origin": [1.1, 0.1, 1.2], "axis": [0, 1, 1]},
            {"name": "j6", "type": "revolute", "origin": [1.2, 0.1, 1.2], "axis": [1, 0, 0]}],
    axes=[_ax("j1"), _ax("j2"), _ax("j3", -0.3, 0.3), _ax("j4"), _ax("j5"), _ax("j6")],
    tool={"xyz": [0.1, 0.0, 0.05], "rpy_deg": [0, 90, 0]})

# A spatial six-axis arm without an analytic solver.
R6 = dict(
    joints=[{"name": "a1", "origin": [0, 0, 0], "axis": [0, 0, 1]},
            {"name": "a2", "origin": [0.15, 0, 0.6], "axis": [0, 1, 0]},
            {"name": "a3", "origin": [0.15, 0, 1.4], "axis": [0, -1, 0]},
            {"name": "a4", "origin": [0.15, 0, 1.55], "axis": [1, 0, 0]},
            {"name": "a5", "origin": [0.95, 0, 1.55], "axis": [0, 1, 0]},
            {"name": "a6", "origin": [1.05, 0, 1.55], "axis": [1, 0, 0]}],
    axes=[_ax("a1"), _ax("a2", -90, 90), _ax("a3", -150, 90), _ax("a4"), _ax("a5", -120, 120), _ax("a6")])


def test_fk_of_a_planar_two_link_arm():
    r = chain_robot(**PLANAR)
    assert np.allclose(fk(r, [90, 0])[:3, 3], [0, 1.5, 0], atol=1e-12)
    assert np.allclose(fk(r, [0, 90])[:3, 3], [1.0, 0.5, 0], atol=1e-12)


def test_pose_is_fixed_x_then_y_then_z():
    t = pose([1, 2, 3], [90, 0, 90])
    # Rz(90) Rx(90): x -> y, y -> z, z -> x.
    assert np.allclose(t[:3, :3], [[0, 0, 1], [1, 0, 0], [0, 1, 0]], atol=1e-12)
    assert np.allclose(t[:3, 3], [1, 2, 3])


def test_jacobian_matches_finite_differences():
    r = chain_robot(**MIXED)
    q = np.array([20.0, -15.0, 0.1, 30.0, 40.0, -25.0])
    jac = jacobian(r, q)
    h = 1e-6
    for k, a in enumerate(r.axes):
        step = np.zeros(6)
        step[k] = np.degrees(h) if a.kind == "revolute" else h
        tp, tm = fk(r, q + step), fk(r, q - step)
        lin = (tp[:3, 3] - tm[:3, 3]) / (2 * h)
        ang = rot_log(tp[:3, :3] @ tm[:3, :3].T) / (2 * h)
        assert np.allclose(jac[:, k], np.concatenate([lin, ang]), atol=1e-6)


def test_a_coupled_joint_follows_its_axes():
    joints = PLANAR["joints"] + [{"name": "c", "origin": [2.0, 0, 0], "axis": [0, 0, 1],
                                  "parent": "b", "coupled": {"a": -1.0, "b": -1.0}, "passive": True}]
    r = chain_robot(joints=joints, axes=PLANAR["axes"])
    assert np.allclose(joint_values(r, [30, 20])[2], -50.0)
    assert coupling_matrix(r).shape == (3, 2)
    # The passive joint undoes both turns: the flange keeps the base orientation.
    assert np.allclose(fk(r, [30, 20])[:3, :3], np.eye(3), atol=1e-12)


def test_joints_listed_out_of_order_follow_their_parents():
    # A joint without "parent" hangs on the one listed before it; null is the base.
    a, b = PLANAR["joints"]
    r = chain_robot(joints=[b, {**a, "parent": None}], axes=PLANAR["axes"])
    assert [j.name for j in r.joints] == ["a", "b"]
    assert np.allclose(fk(r, [90, 0])[:3, 3], [0, 1.0, 0], atol=1e-12)


def test_numeric_ik_reaches_a_reachable_frame_and_refuses_an_unreachable_one():
    r6 = chain_robot(**R6)
    q_true = np.array([25.0, 20.0, -30.0, 40.0, 50.0, -35.0])
    target = fk(r6, q_true)
    q = ik_numeric(r6, target, seed=q_true + 5.0)
    assert q is not None and within_limits(r6, q)
    assert np.allclose(fk(r6, q)[:3, 3], target[:3, 3], atol=IK_POS_TOL)
    assert np.allclose(fk(r6, q)[:3, :3], target[:3, :3], atol=1e-4)
    assert ik_numeric(r6, pose([10, 0, 0], [0, 0, 0]), seed=r6.home) is None
    # ik() with no analytic solver for "chain" is the numeric one.
    assert np.allclose(fk(r6, ik(r6, target, q_true + 5.0))[:3, 3], target[:3, 3], atol=IK_POS_TOL)


def test_ik_respects_limits():
    # The point at b = 120 degrees, while b's range is [-90, 90]: only the
    # mirrored elbow (b = -120) reaches it, which is also outside the range.
    axes = [PLANAR["axes"][0], {"name": "b", "range": [-90, 90], "speed": 90, "accel": 360}]
    free = chain_robot(**PLANAR)
    r = chain_robot(joints=PLANAR["joints"], axes=axes, tool=PLANAR["tool"], task="xyz")
    target = fk(free, [10, 120])
    assert ik(r, target, [0, 0]) is None
    assert ik(r, fk(free, [10, 60]), [0, 0]) is not None


def test_singularity_of_a_stretched_planar_arm():
    r = chain_robot(**PLANAR, task="xyz")
    assert task_sigma_min(r, [0, 0]) < 1e-9
    r = chain_robot(joints=PLANAR["joints"], axes=PLANAR["axes"], tool=PLANAR["tool"], task="xyz_yaw")
    assert task_sigma_min(r, [0, 90]) > 1e-3


def test_home_defaults_to_zero_clipped_into_range():
    axes = [PLANAR["axes"][0], {"name": "b", "range": [10, 90], "speed": 90, "accel": 360}]
    r = chain_robot(joints=PLANAR["joints"], axes=axes)
    assert list(r.home) == [0.0, 10.0]
    with pytest.raises(ValueError, match="home"):
        chain_robot(joints=PLANAR["joints"], axes=axes, home=[0, 0])


def test_a_bad_chain_says_why():
    a, b = PLANAR["joints"]
    with pytest.raises(ValueError, match="parent 'x' is not a joint"):
        chain_robot(joints=[a, {**b, "parent": "x"}], axes=PLANAR["axes"])
    with pytest.raises(ValueError, match="axis has no direction"):
        chain_robot(joints=[a, {**b, "axis": [0, 0, 0]}], axes=PLANAR["axes"])
    ax_a, ax_b = PLANAR["axes"]
    with pytest.raises(ValueError, match="range"):
        chain_robot(joints=PLANAR["joints"], axes=[ax_a, {**ax_b, "range": [90, -90]}])
    with pytest.raises(ValueError, match="speed"):
        chain_robot(joints=PLANAR["joints"], axes=[ax_a, {**ax_b, "speed": 0}])
    with pytest.raises(ValueError, match="accel"):
        chain_robot(joints=PLANAR["joints"], axes=[ax_a, {k: v for k, v in ax_b.items() if k != "accel"}])
    with pytest.raises(ValueError, match="no axis of its name"):
        chain_robot(joints=PLANAR["joints"], axes=[ax_a])
    with pytest.raises(ValueError, match="moves no joint"):
        chain_robot(joints=PLANAR["joints"], axes=[ax_a, ax_b, {**ax_b, "name": "z"}])
