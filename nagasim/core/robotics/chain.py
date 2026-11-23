"""Kinematic chains: joints at the zero pose, coupled joints, forward kinematics,
the Jacobian, singularity and inverse kinematics.

A chain is a serial list of joints, each an origin point and a unit axis in the
robot base frame at q = 0 (the product-of-exponentials form): Z up, X forward,
metres. Joint values in records are degrees (revolute) or metres (prismatic);
the maths works in radians. A joint either follows the actuated axis of its own
name or is `coupled` to a linear sum of axes (`theta = C q`), which covers a
parallelogram linkage and a URDF mimic joint without a closed-chain solver.
Limits apply to the actuated axes `q`.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

import numpy as np

from nagasim.core.robotics.delta import delta_fk

SINGULAR_TOL = 1e-3
IK_POS_TOL = 1e-5     # m
IK_ROT_TOL = 1e-4     # rad

JOINT_KINDS = ("revolute", "prismatic")
TASKS = {"6d": (0, 1, 2, 3, 4, 5), "xyz_yaw": (0, 1, 2, 5), "xyz": (0, 1, 2)}

# structure -> callable(robot, target 4x4) -> list of candidate q (record units),
# unfiltered. templates.py fills it; it lives here so that ik() needs no import
# of templates (which imports this module).
ANALYTIC_IK: dict = {}

_LAMBDA = 0.05                       # DLS damping
_LAMBDA_FADE = 0.05                  # error (m / rad) below which the damping fades
_MAX_ITER = 200
_STEP_REV = math.radians(10.0)       # step clamp per iteration
_STEP_PRI = 0.05
_NULL_GAIN = 0.1                     # pull to mid-range for a redundant chain


@dataclass(frozen=True)
class Joint:
    name: str
    kind: str                      # "revolute" | "prismatic"
    origin: np.ndarray             # (3,) m, base frame, q = 0
    axis: np.ndarray               # (3,) unit
    parent: str | None
    coupled: dict | None           # {axis name: factor}; None = its own axis


@dataclass(frozen=True)
class Axis:
    name: str
    kind: str
    lo: float
    hi: float
    speed: float
    accel: float


@dataclass(frozen=True)
class Robot:
    name: str
    structure: str
    joints: tuple
    axes: tuple
    tool: np.ndarray               # 4x4 flange -> TCP
    home: np.ndarray               # (n_axes,) record units
    params: dict
    task: str                      # "6d" | "xyz_yaw" | "xyz"
    reach_m: float | None = None
    payload_max: float | None = None
    payload_derate: float = 1.0
    tcp_speed: float | None = None
    tcp_accel: float | None = None
    # The flange frame at q = 0 in the base frame. None: at the last joint's
    # origin with the base orientation. A template sets it so the flange z
    # points out of the last link (arms) or down (four-axis robots, delta, gantry).
    flange: np.ndarray | None = field(default=None, compare=False)


# --- small rotation helpers -------------------------------------------------

def _rot(axis, angle: float) -> np.ndarray:
    """Rotation matrix about a unit axis (Rodrigues)."""
    x, y, z = axis
    k = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + math.sin(angle) * k + (1.0 - math.cos(angle)) * (k @ k)


_DOWN = _rot((1, 0, 0), math.pi)       # a flange's or tool's z pointing down, x forward


def pose(xyz, rpy_deg) -> np.ndarray:
    """4x4 pose; roll-pitch-yaw as fixed X, then Y, then Z (URDF's convention)."""
    r, p, y = (math.radians(float(a)) for a in rpy_deg)
    t = np.eye(4)
    t[:3, :3] = _rot((0, 0, 1), y) @ _rot((0, 1, 0), p) @ _rot((1, 0, 0), r)
    t[:3, 3] = np.asarray(xyz, dtype=float)
    return t


def rot_log(rot: np.ndarray) -> np.ndarray:
    """Axis times angle (rad) of a rotation matrix."""
    cos = max(-1.0, min(1.0, (np.trace(rot) - 1.0) / 2.0))
    angle = math.acos(cos)
    vee = np.array([rot[2, 1] - rot[1, 2], rot[0, 2] - rot[2, 0], rot[1, 0] - rot[0, 1]])
    if angle < 1e-9:
        return vee / 2.0
    if math.pi - angle < 1e-6:
        # Near a half turn the skew part vanishes; the axis is the column of
        # R + I with the largest norm.
        m = rot + np.eye(3)
        col = m[:, int(np.argmax(np.linalg.norm(m, axis=0)))]
        return col / np.linalg.norm(col) * angle
    return vee * (angle / (2.0 * math.sin(angle)))


# --- building ---------------------------------------------------------------

def _vec3(value, what: str) -> np.ndarray:
    v = np.asarray(value, dtype=float)
    if v.shape != (3,) or not np.all(np.isfinite(v)):
        raise ValueError(f"{what} must be three numbers")
    return v


def _axis_of(d: dict) -> Axis:
    name = d.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("every axis needs a name")
    rng = d.get("range")
    if (not isinstance(rng, (list, tuple)) or len(rng) != 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in rng)
            or not rng[0] < rng[1]):
        raise ValueError(f"axis '{name}': range must be [lo, hi] with lo < hi")
    for key in ("speed", "accel"):
        v = d.get(key)
        if v is None:
            raise ValueError(f"axis '{name}': {key} is missing")
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"axis '{name}': {key} must be a number above 0")
    return Axis(name, "revolute", float(rng[0]), float(rng[1]), float(d["speed"]), float(d["accel"]))


def chain_robot(joints: list, axes: list, *, name="robot", tool: dict | None = None,
                home: list | None = None, structure="chain", task=None, params=None) -> Robot:
    """A robot from joint and axis dicts. Raises ValueError with a sentence on a bad shape.
    *task* None follows the axes (#1984): up to 3 position only, 4 position and yaw, 5 or more
    the full pose, as the templates of those axis counts use."""
    if not joints:
        raise ValueError("a chain needs at least one joint")
    if task is None:
        task = "xyz" if len(axes) <= 3 else "xyz_yaw" if len(axes) == 4 else "6d"
    if task not in TASKS:
        raise ValueError(f"unknown task '{task}'")
    axes_in = [_axis_of(a) for a in axes]
    axis_names = [a.name for a in axes_in]
    if len(set(axis_names)) != len(axis_names):
        raise ValueError("two axes have the same name")
    built, names = [], []
    for i, d in enumerate(joints):
        jname = d.get("name")
        if not isinstance(jname, str) or not jname or jname in names:
            raise ValueError(f"joint {i} has no name or a repeated one")
        kind = d.get("type", "revolute")
        if kind not in JOINT_KINDS:
            raise ValueError(f"joint '{jname}': type must be revolute or prismatic")
        axis = _vec3(d.get("axis"), f"joint '{jname}' axis")
        norm = float(np.linalg.norm(axis))
        if norm < 1e-12:
            raise ValueError(f"joint '{jname}': axis has no direction")
        parent = d.get("parent", names[-1] if names else None)
        listed = [j.get("name") for j in joints]
        if parent is not None and (parent not in listed or parent == jname):
            raise ValueError(f"joint '{jname}': parent '{parent}' is not a joint")
        coupled = d.get("coupled")
        if coupled is not None:
            if not isinstance(coupled, dict) or not coupled:
                raise ValueError(f"joint '{jname}': coupled must be {{axis: factor}}")
            for an, f in coupled.items():
                if an not in axis_names:
                    raise ValueError(f"joint '{jname}': coupled axis '{an}' is not an axis")
                if not isinstance(f, (int, float)) or not math.isfinite(f):
                    raise ValueError(f"joint '{jname}': coupling factors must be numbers")
            coupled = {k: float(v) for k, v in coupled.items()}
        elif d.get("passive"):
            raise ValueError(f"joint '{jname}': a passive joint needs coupled axes")
        elif jname not in axis_names:
            raise ValueError(f"joint '{jname}' has no axis of its name")
        built.append(Joint(jname, kind, _vec3(d.get("origin"), f"joint '{jname}' origin"),
                           axis / norm, parent, coupled))
        names.append(jname)
    # Serial order: walk from the root through parents (a later joint without a
    # parent hangs on the previous one, set above).
    by_parent: dict = {}
    for j in built:
        if j.parent in by_parent:
            raise ValueError(f"joint '{j.name}': two joints share parent '{j.parent}'; a chain is serial")
        by_parent[j.parent] = j
    ordered, cur = [], None
    while cur in by_parent:
        ordered.append(by_parent[cur])
        cur = ordered[-1].name
    if len(ordered) != len(built):
        raise ValueError("the joints do not form one serial chain")
    # Each axis takes the kind of the joint it drives; every axis drives one.
    kinds = {}
    for j in ordered:
        for an in (j.coupled or {j.name: 1.0}):
            if kinds.setdefault(an, j.kind) != j.kind:
                raise ValueError(f"axis '{an}' drives both revolute and prismatic joints")
    for a in axis_names:
        if a not in kinds:
            raise ValueError(f"axis '{a}' moves no joint")
    axes_t = tuple(dataclasses.replace(a, kind=kinds[a.name]) for a in axes_in)
    return Robot(name, structure, tuple(ordered), axes_t, tool_pose(tool), home_vector(axes_t, home),
                 dict(params or {}), task)


def tool_pose(tool: dict | None) -> np.ndarray:
    """The flange -> TCP transform of a `{xyz, rpy_deg}` tool dict."""
    tool = tool or {}
    return pose(tool.get("xyz", [0, 0, 0]), tool.get("rpy_deg", [0, 0, 0]))


def home_vector(axes, home) -> np.ndarray:
    """`home` checked against the axis ranges; None is all zeros clipped into range."""
    lo = np.array([a.lo for a in axes])
    hi = np.array([a.hi for a in axes])
    if home is None:
        return np.clip(np.zeros(len(axes)), lo, hi)
    home_v = np.asarray(home, dtype=float)
    if home_v.shape != (len(axes),):
        raise ValueError(f"home must have {len(axes)} values, one per axis")
    if np.any(home_v < lo) or np.any(home_v > hi):
        raise ValueError("home is outside an axis range")
    return home_v


# --- forward kinematics -----------------------------------------------------

def coupling_matrix(robot: Robot) -> np.ndarray:
    """(n_joints, n_axes): theta = C q."""
    index = {a.name: k for k, a in enumerate(robot.axes)}
    c = np.zeros((len(robot.joints), len(robot.axes)))
    for i, j in enumerate(robot.joints):
        for an, f in (j.coupled or {j.name: 1.0}).items():
            c[i, index[an]] = f
    return c


def joint_values(robot: Robot, q) -> np.ndarray:
    """Chain joint values theta = C q, in record units."""
    return coupling_matrix(robot) @ np.asarray(q, dtype=float)


def _scale(kinds) -> np.ndarray:
    return np.array([math.pi / 180.0 if k == "revolute" else 1.0 for k in kinds])


def _flange0(robot: Robot) -> np.ndarray:
    if robot.flange is not None:
        return robot.flange
    m = np.eye(4)
    m[:3, 3] = robot.joints[-1].origin
    return m


def _frames(robot: Robot, q):
    """Accumulated transforms after each joint, and the TCP pose."""
    theta = joint_values(robot, q) * _scale(j.kind for j in robot.joints)
    t = np.eye(4)
    after = []
    for j, th in zip(robot.joints, theta):
        e = np.eye(4)
        if j.kind == "revolute":
            r = _rot(j.axis, th)
            e[:3, :3] = r
            e[:3, 3] = j.origin - r @ j.origin
        else:
            e[:3, 3] = j.axis * th
        t = t @ e
        after.append(t)
    return after, t @ _flange0(robot) @ robot.tool


def joint_origins(robot: Robot, q) -> np.ndarray:
    """(n_joints, 3) where each chain joint's origin is at q, base frame."""
    after, _tcp = _frames(robot, q)
    return np.array([t[:3, :3] @ j.origin + t[:3, 3] for j, t in zip(robot.joints, after)])


def fk(robot: Robot, q) -> np.ndarray:
    """4x4 TCP pose in the base frame; q in record units."""
    if robot.structure == "delta":
        q = np.asarray(q, dtype=float)
        t = np.eye(4)
        t[:3, :3] = _rot((0, 0, 1), math.radians(q[3]) if len(q) > 3 else 0.0) @ _rot((1, 0, 0), math.pi)
        t[:3, 3] = delta_fk(robot.params, q[:3])
        return t @ robot.tool
    return _frames(robot, q)[1]


def jacobian(robot: Robot, q) -> np.ndarray:
    """6 x n_axes, rows vx, vy, vz, wx, wy, wz at the TCP in the base frame;
    columns per radian (revolute axis) or per metre (prismatic axis)."""
    q = np.asarray(q, dtype=float)
    if robot.structure == "delta":
        return _numeric_jacobian(robot, q)     # its FK is closed form, not a chain
    after, tcp = _frames(robot, q)
    p = tcp[:3, 3]
    jj = np.zeros((6, len(robot.joints)))
    for i, (j, t) in enumerate(zip(robot.joints, after)):
        w = t[:3, :3] @ j.axis
        if j.kind == "revolute":
            jj[:3, i] = np.cross(w, p - (t[:3, :3] @ j.origin + t[:3, 3]))
            jj[3:, i] = w
        else:
            jj[:3, i] = w
    return jj @ coupling_matrix(robot)


def _numeric_jacobian(robot: Robot, q) -> np.ndarray:
    h = 1e-6
    s = _scale(a.kind for a in robot.axes)
    out = np.zeros((6, len(robot.axes)))
    for k in range(len(robot.axes)):
        dq = np.zeros(len(robot.axes))
        dq[k] = h / s[k]
        tp, tm = fk(robot, q + dq), fk(robot, q - dq)
        out[:3, k] = (tp[:3, 3] - tm[:3, 3]) / (2 * h)
        out[3:, k] = rot_log(tp[:3, :3] @ tm[:3, :3].T) / (2 * h)
    return out


def _length_scale(robot: Robot) -> float:
    if robot.reach_m:
        return float(robot.reach_m)
    if robot.structure == "delta":
        return float(robot.params["upper_arm"]) + float(robot.params["forearm"])
    pts = [j.origin for j in robot.joints] + [_flange0(robot)[:3, 3]]
    length = sum(float(np.linalg.norm(b - a)) for a, b in zip(pts, pts[1:]))
    length += float(np.linalg.norm(robot.tool[:3, 3]))
    return length if length > 1e-9 else 1.0


def task_sigma_min(robot: Robot, q) -> float:
    """Smallest singular value of the task Jacobian, linear rows divided by the reach."""
    jac = jacobian(robot, q)
    jac[:3] /= _length_scale(robot)
    return float(np.linalg.svd(jac[list(TASKS[robot.task])], compute_uv=False).min())


def within_limits(robot: Robot, q, tol=1e-9) -> bool:
    q = np.asarray(q, dtype=float)
    return bool(all(a.lo - tol <= v <= a.hi + tol for a, v in zip(robot.axes, q)))


# --- inverse kinematics -----------------------------------------------------

def _error(robot: Robot, target: np.ndarray, current: np.ndarray) -> np.ndarray:
    e = np.concatenate([target[:3, 3] - current[:3, 3], rot_log(target[:3, :3] @ current[:3, :3].T)])
    return e[list(TASKS[robot.task])]


def _converged(robot: Robot, err: np.ndarray) -> bool:
    return bool(np.linalg.norm(err[:3]) < IK_POS_TOL
                and (len(err) == 3 or np.linalg.norm(err[3:]) < IK_ROT_TOL))


def _dls(robot: Robot, target: np.ndarray, q0: np.ndarray):
    rows = list(TASKS[robot.task])
    s = _scale(a.kind for a in robot.axes)
    cap = np.array([_STEP_REV if a.kind == "revolute" else _STEP_PRI for a in robot.axes])
    lo = np.array([a.lo for a in robot.axes])
    hi = np.array([a.hi for a in robot.axes])
    mid = (lo + hi) / 2.0
    redundant = len(robot.axes) > len(rows)
    q = np.clip(np.asarray(q0, dtype=float), lo, hi)
    for _ in range(_MAX_ITER):
        err = _error(robot, target, fk(robot, q))
        if _converged(robot, err):
            return q
        jac = jacobian(robot, q)[rows]
        # The damping fades as the error shrinks: lambda keeps the far steps
        # stable, and close to the answer a near-singular pose still converges.
        lam = _LAMBDA * min(1.0, float(np.linalg.norm(err)) / _LAMBDA_FADE)
        dq = jac.T @ np.linalg.solve(jac @ jac.T + lam ** 2 * np.eye(len(rows)), err)
        if redundant:
            # The exact pseudo-inverse projector keeps the pull out of the task
            # to first order; it fades with the error like the damping, since a
            # pull that never stops keeps the curvature error above tolerance.
            null = np.eye(len(q)) - np.linalg.pinv(jac) @ jac
            dq = dq + null @ ((lam / _LAMBDA) * _NULL_GAIN * (mid - q) * s)
        ratio = float(np.max(np.abs(dq) / cap))
        if ratio > 1.0:
            dq = dq / ratio
        q = np.clip(q + dq / s, lo, hi)
    return q if _converged(robot, _error(robot, target, fk(robot, q))) else None


def ik_numeric(robot: Robot, target: np.ndarray, seed) -> np.ndarray | None:
    """Damped least squares from the seed, then from home, then from mid-range."""
    lo = np.array([a.lo for a in robot.axes])
    hi = np.array([a.hi for a in robot.axes])
    for start in (np.asarray(seed, dtype=float), robot.home, (lo + hi) / 2.0):
        q = _dls(robot, np.asarray(target, dtype=float), start)
        if q is not None:
            return q
    return None


def _nearest_turn(robot: Robot, q: np.ndarray, seed: np.ndarray) -> np.ndarray | None:
    """Shift each revolute value by whole turns toward the seed, inside its range."""
    out = q.copy()
    for k, a in enumerate(robot.axes):
        v = out[k]
        if a.kind == "revolute":
            options = [v + 360.0 * n for n in range(-3, 4)]
            options = [o for o in options if a.lo - 1e-9 <= o <= a.hi + 1e-9]
            if not options:
                return None
            v = min(options, key=lambda o: abs(o - seed[k]))
        elif not a.lo - 1e-9 <= v <= a.hi + 1e-9:
            return None
        out[k] = min(max(v, a.lo), a.hi)
    return out


def ik(robot: Robot, target: np.ndarray, seed) -> np.ndarray | None:
    """The solution within limits nearest the seed: analytic where the structure
    has a closed form, else numeric. Distance is per-axis time at full speed, so
    revolute and prismatic axes compare."""
    seed = np.asarray(seed, dtype=float)
    target = np.asarray(target, dtype=float)
    solver = ANALYTIC_IK.get(robot.structure)
    if solver is None:
        return ik_numeric(robot, target, seed)
    speed = np.array([a.speed for a in robot.axes])
    best, best_d = None, math.inf
    for cand in solver(robot, target):
        q = _nearest_turn(robot, np.asarray(cand, dtype=float), seed)
        if q is None or not _converged(robot, _error(robot, target, fk(robot, q))):
            continue
        d = float(np.sum(((q - seed) / speed) ** 2))
        if d < best_d:
            best, best_d = q, d
    return best
