"""The six structure templates: FK, analytic/numeric IK, Jacobian, singularity, reach."""
import math

import numpy as np
import pytest

from nagasim.core.robotics.chain import (
    SINGULAR_TOL, fk, ik, jacobian, joint_origins, rot_log, task_sigma_min, within_limits)
from nagasim.core.robotics.delta import delta_display, delta_fk, delta_ik
from nagasim.core.robotics.templates import ANALYTIC_IK, PARAMS, STRUCTURES, reach, template_robot
from robotcases import CASES, IRB_RECORD, case_robot, gantry_robot, irb_robot, robot_cell


def _samples(r, n, seed=1342):
    rng = np.random.default_rng(seed)
    lo = np.array([a.lo for a in r.axes])
    hi = np.array([a.hi for a in r.axes])
    pad = 0.1 * (hi - lo)
    return rng.uniform(lo + pad, hi - pad, size=(n, len(r.axes)))


@pytest.mark.parametrize("structure", sorted(CASES))
def test_ik_inverts_fk_inside_the_limits(structure):
    r = case_robot(structure)
    for q_true in _samples(r, 25):
        # serial7 is numeric and local: seed near the answer; the analytic ones at home.
        seed = q_true + 3.0 if structure == "serial7" else r.home
        q = ik(r, fk(r, q_true), seed)
        assert q is not None and within_limits(r, q)
        assert np.allclose(fk(r, q)[:3, 3], fk(r, q_true)[:3, 3], atol=1e-5)


def test_serial6_zero_pose_and_signs():
    r = irb_robot()
    o = joint_origins(r, np.zeros(6))
    p = CASES["serial6"]["params"]
    # Upper arm vertical, forearm horizontal along +X, wrist straight (flange z along +X).
    assert np.allclose(o[2] - o[1], [0, 0, p["upper_arm"]])
    assert np.allclose(o[4] - o[3], [p["forearm"], 0, 0])
    assert np.allclose(fk(r, np.zeros(6))[:3, 2], [1, 0, 0], atol=1e-12)
    lean = joint_origins(r, [0, 30, 0, 0, 0, 0])
    assert lean[2][0] > o[2][0]                              # q2 > 0 leans forward
    up = joint_origins(r, [0, 0, 30, 0, 0, 0])
    assert up[4][2] > o[4][2]                                # q3 > 0 raises the forearm
    # The forearm's elevation is q3 - q2.
    both = joint_origins(r, [0, 20, 50, 0, 0, 0])
    v = both[4] - both[3]
    assert math.degrees(math.atan2(v[2], v[0])) == pytest.approx(30.0)


def test_serial6_reach_is_the_wrist_centre_inside_the_axis_ranges():
    assert abs(reach(irb_robot()) - 2.596) < 0.002           # 2.6049 without the axis 2 limit


def test_scara_reach_is_both_arms():
    assert reach(case_robot("scara4")) == pytest.approx(0.60, abs=1e-9)


def test_gantry_reach_is_its_strokes():
    assert reach(case_robot("gantry")) == pytest.approx(3.0)


def test_reach_is_fast():
    import time
    t = time.perf_counter()
    for s in CASES:
        reach(case_robot(s))
    assert time.perf_counter() - t < 0.5 * len(CASES)


@pytest.mark.parametrize("structure", sorted(CASES))
def test_default_proportions_reach_one_metre(structure):
    tpl = STRUCTURES[structure]
    axes = [{"range": list(r), "speed": 100.0, "accel": 400.0} for r in CASES[structure]["ranges"]]
    if structure == "gantry":
        axes = [{**a, "range": [lo / 3.0, hi / 3.0]} for a, (lo, hi) in zip(axes, CASES["gantry"]["ranges"])]
    assert set(tpl.defaults) == set(tpl.params) == set(PARAMS[structure])
    assert reach(template_robot(structure, tpl.defaults, axes)) == pytest.approx(1.0, abs=1e-4)
    assert len(tpl.axis_notes) == len(tpl.axes) + (1 if tpl.optional_axis else 0)


def test_the_palletizer_flange_stays_level():
    r = case_robot("palletizer4")
    for q in _samples(r, 25):
        assert np.allclose(fk(r, q)[:3, 2], [0, 0, -1], atol=1e-12)


def test_the_palletizer_forearm_keeps_its_direction_when_j2_moves():
    r = case_robot("palletizer4")
    names = [j.name for j in r.joints]
    elbow, wrist = names.index("j3"), names.index("wrist")
    ref = None
    for j2 in (-30, 0, 40, 80):
        o = joint_origins(r, [10, j2, 25, 0])
        v = o[wrist] - o[elbow]
        ref = v if ref is None else ref
        assert np.allclose(v, ref, atol=1e-12)
    # j3 is the absolute forearm angle, positive below the horizontal.
    assert math.degrees(math.atan2(-ref[2], math.hypot(ref[0], ref[1]))) == pytest.approx(25.0)


def test_serial6_wrist_is_singular_at_q5_zero():
    r = irb_robot()
    assert task_sigma_min(r, [0, 10, -20, 0, 0, 0]) < 1e-6
    assert task_sigma_min(r, [0, 10, -20, 0, 45, 0]) > SINGULAR_TOL


def test_serial6_ik_returns_the_solution_nearest_the_seed():
    r = irb_robot()
    q_up = np.array([10.0, 20.0, -10.0, 30.0, 40.0, 20.0])
    target = fk(r, q_up)
    assert np.allclose(ik(r, target, q_up + 2.0), q_up, atol=1e-6)
    q_flip = np.array([10.0, 20.0, -10.0, -150.0, -40.0, -160.0])     # same pose, wrist flipped
    assert np.allclose(fk(r, q_flip), target, atol=1e-9)
    assert np.allclose(ik(r, target, q_flip + 2.0), q_flip, atol=1e-6)
    # Whole turns: a6 = 20 or -340 are the same pose; the seed decides.
    assert ik(r, target, np.array([10, 20, -10, 30, 40, -330.0]))[5] == pytest.approx(-340.0)


@pytest.mark.parametrize("structure, q, count", [
    ("serial6", [10.0, 20.0, -10.0, 30.0, 40.0, 20.0], 8),
    ("palletizer4", [10.0, 20.0, 30.0, 40.0], 4),
    ("scara4", [10.0, 40.0, 0.1, 30.0], 2)])
def test_every_analytic_branch_reproduces_the_pose(structure, q, count):
    # Ranges opened to whole turns, so no branch is lost to a limit.
    ranges = [(0.0, 0.2) if a == "prismatic" else (-360, 360) for a in STRUCTURES[structure].axis_kinds]
    axes = [{"range": list(r), "speed": 100.0, "accel": 400.0} for r in ranges]
    r = template_robot(structure, CASES[structure]["params"], axes)
    target = fk(r, q)
    cands = ANALYTIC_IK[structure](r, target)
    assert len(cands) == count
    for c in cands:
        assert np.allclose(fk(r, c), target, atol=1e-9)
        # Each branch is returned from a seed beside it.
        assert np.allclose(ik(r, target, c + 1.0), c, atol=1e-6)


def test_jacobian_of_a_template_matches_finite_differences():
    for r, q in ((irb_robot(), np.array([10, 20, -10, 30, 40, 20.0])),
                 (case_robot("palletizer4"), np.array([10, 20, 30, 40.0])),
                 (case_robot("delta"), np.array([20, 30, 10.0]))):
        jac = jacobian(r, q)
        h = 1e-6
        for k in range(len(q)):
            dq = np.zeros(len(q))
            dq[k] = math.degrees(h)
            tp, tm = fk(r, q + dq), fk(r, q - dq)
            assert np.allclose(jac[:3, k], (tp[:3, 3] - tm[:3, 3]) / (2 * h), atol=1e-5)
            assert np.allclose(jac[3:, k], rot_log(tp[:3, :3] @ tm[:3, :3].T) / (2 * h), atol=1e-5)


def test_delta_fk_inverts_ik_and_refuses_a_point_outside():
    p = CASES["delta"]["params"]
    assert np.allclose(delta_fk(p, delta_ik(p, [0.1, -0.05, 0.6])), [0.1, -0.05, 0.6], atol=1e-9)
    assert delta_ik(p, [2.0, 0, 0.6]) is None
    r = case_robot("delta")
    assert reach(r) > 0.1
    # The effector stays level, and the display forearms end on the effector.
    q = np.array([20.0, 35.0, 5.0])
    assert np.allclose(fk(r, q)[:3, 2], [0, 0, -1])
    d = delta_display(p, q)
    assert d["a2"] == 35.0
    # Rebuild each forearm from its display angles: it ends on the effector attachment.
    eff = delta_fk(p, q)
    for i, phi in enumerate(np.radians([0.0, 120.0, 240.0])):
        rz = np.array([[np.cos(phi), -np.sin(phi), 0], [np.sin(phi), np.cos(phi), 0], [0, 0, 1]])
        qi, a, b = (np.radians(v) for v in (d[f"a{i + 1}"], d[f"f{i + 1}_a"] + d[f"a{i + 1}"], d[f"f{i + 1}_b"]))
        elbow = rz @ [p["base_radius"] + p["upper_arm"] * np.cos(qi), 0, -p["upper_arm"] * np.sin(qi)]
        elbow[2] += p["mount_height"]
        rod = rz @ [-np.cos(b) * np.sin(a), np.sin(b), -np.cos(b) * np.cos(a)]
        attach = eff + rz @ [p["effector_radius"], 0, 0]
        assert np.allclose(elbow + p["forearm"] * rod, attach, atol=1e-9)


def test_gantry_and_scara_are_their_own_inverse():
    g = gantry_robot()
    assert np.allclose(fk(g, [1.0, 0.5, 0.2])[:3, 3], [1.0, 0.5, 2.3])
    assert np.allclose(ik(g, fk(g, [1.0, 0.5, 0.2]), g.home), [1.0, 0.5, 0.2])
    s = case_robot("scara4")
    assert np.allclose(fk(s, [0, 0, 0.1, 0])[:3, 3], [0.6, 0, 0.25])


def test_an_optional_yaw_axis_makes_the_task_xyz_yaw():
    axes = [{"range": list(r), "speed": 1.0, "accel": 2.0} for r in CASES["gantry"]["ranges"]]
    g = template_robot("gantry", CASES["gantry"]["params"], axes + [{"range": [-180, 180], "speed": 90, "accel": 360}])
    assert g.task == "xyz_yaw" and g.axes[3].name == "yaw"
    q = np.array([1.0, 1.0, 0.5, 30.0])
    assert np.allclose(ik(g, fk(g, q), g.home), q)


def test_an_unknown_structure_or_missing_parameter_says_which():
    with pytest.raises(ValueError, match="unknown structure 'arm9'"):
        template_robot("arm9", {}, [])
    params = {k: v for k, v in CASES["serial6"]["params"].items() if k != "forearm"}
    axes = [{"range": list(r), "speed": 100, "accel": 400} for r in CASES["serial6"]["ranges"]]
    with pytest.raises(ValueError, match="forearm"):
        template_robot("serial6", params, axes)
    with pytest.raises(ValueError, match="6 axes"):
        template_robot("serial6", CASES["serial6"]["params"], axes[:5])
    with pytest.raises(ValueError, match="named 'a2'"):
        template_robot("serial6", CASES["serial6"]["params"], [axes[0], {**axes[1], "name": "x"}] + axes[2:])


def test_the_shared_cases_build():
    r = irb_robot(payload_derate=0.5)
    assert r.payload_max == 175.0 and r.payload_derate == 0.5 and r.name == "IRB"
    assert np.allclose(r.tool[:3, 3], [0, 0, 0.2])
    assert gantry_robot(tcp_speed=0.5).tcp_speed == 0.5
    doc = robot_cell()
    cell = next(b for b in doc["blocks"] if b["name"] == "process_1")
    assert cell["type"] == "MOTION" and cell["params"]["robot"] == IRB_RECORD["name"]
    assert doc["robots"][0] == IRB_RECORD and doc["simulation"]["time_unit"] == "seconds"
