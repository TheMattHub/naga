"""The six structure templates: a robot from its type and a few dimensions.

Each serial template turns its dimensions into per-joint dicts and calls
`chain_robot`, so forward kinematics and the Jacobian have one implementation;
the delta has its own closed form (`delta.py`). Every template with a textbook
solution registers it in `ANALYTIC_IK`: closed form is exact and fast where the
geometry allows it. The redundant `serial7` and a free `chain` have none, so
they use the damped least squares solver.

Frame: robot base, Z up, X forward, metres; axis values in degrees (revolute)
or metres (prismatic). The flange at q = 0 has its z out along the last link
for the arms (`serial6`, `serial7`) and pointing down for the others.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import numpy as np

from nagasim.core.robotics.delta import ARM_ANGLES, fk_batch, ik_all
from nagasim.core.robotics.chain import (
    ANALYTIC_IK, _DOWN, Joint, Robot, _axis_of, _rot, chain_robot, home_vector, tool_pose)


@dataclass(frozen=True)
class Template:
    name: str
    axes: tuple
    axis_kinds: tuple
    params: tuple
    task: str
    defaults: dict              # proportions at reach 1.0 m, for proposals
    reach_kind: str             # "wrist" | "flange" | "radius" | "strokes"
    axis_notes: tuple           # per axis: its zero pose and positive direction
    optional_axis: str | None = None   # a fourth, yaw axis the structure may have


PARAMS = {
    "serial6": {"base_height": "d1: height of axis 2 above the base",
                "shoulder_offset": "a1: axis 2 ahead of axis 1",
                "upper_arm": "a2: axis 2 to axis 3",
                "elbow_offset": "a3: forearm axis above axis 3",
                "forearm": "d4: axis 3 to the wrist centre along the forearm",
                "wrist_flange": "d6: wrist centre to the flange"},
    "palletizer4": {"base_height": "height of axis 2 above the base",
                    "shoulder_offset": "axis 2 ahead of axis 1",
                    "upper_arm": "axis 2 to the elbow",
                    "forearm": "elbow to the wrist pivot",
                    "wrist_drop": "wrist pivot down to the flange"},
    "scara4": {"base_height": "height of the arms above the base",
               "arm1": "axis 1 to axis 2", "arm2": "axis 2 to the quill",
               "stroke": "travel of the quill", "flange_drop": "arm plane down to the flange at the top of the stroke"},
    "delta": {"mount_height": "height of the base plate above the floor",
              "base_radius": "centre to each upper-arm pivot",
              "effector_radius": "effector centre to each forearm attachment",
              "upper_arm": "upper-arm length", "forearm": "forearm (parallelogram) length"},
    "gantry": {"mount_height": "height of the flange at the top of the z stroke"},
    "serial7": {"base_height": "d1: height of axis 2 above the base",
                "upper_arm": "d3: axis 2 to axis 4", "forearm": "d5: axis 4 to the wrist centre",
                "wrist_flange": "d7: wrist centre to the flange"},
}

# Proportions at reach 1.0 m: a typical robot of each kind scaled by its reach
# with typical ranges (serial6 an IRB 6700-like arm, 2.596 m; palletizer4
# 2.945 m; scara4 0.6 m; delta a 0.650 m working radius; gantry a 3 m stroke;
# serial7 0.8 m). Fixed numbers, so importing computes nothing.
_DEFAULTS = {
    "serial6": dict(base_height=0.300456, shoulder_offset=0.123264, upper_arm=0.43335, elbow_offset=0.07704,
                    forearm=0.440091, wrist_flange=0.07704),
    "palletizer4": dict(base_height=0.305597, shoulder_offset=0.084888, upper_arm=0.441418, forearm=0.475373,
                        wrist_drop=0.101866),
    "scara4": dict(base_height=0.666667, arm1=0.583333, arm2=0.416667, stroke=0.333333, flange_drop=0.083333),
    "delta": dict(mount_height=2.306996, base_radius=0.307599, effector_radius=0.076900,
                  upper_arm=0.538299, forearm=1.230398),
    "gantry": dict(mount_height=0.833333),
    "serial7": dict(base_height=0.425, upper_arm=0.5, forearm=0.5, wrist_flange=0.1575),
}

_POSITIVE = {"serial6": ("upper_arm", "forearm"), "palletizer4": ("upper_arm", "forearm"),
             "scara4": ("arm1", "arm2"), "delta": ("upper_arm", "forearm", "base_radius"),
             "gantry": (), "serial7": ("upper_arm", "forearm")}


def _flange(xyz, rot) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = rot
    m[:3, 3] = xyz
    return m


def _rev(name, origin, axis, **kw):
    return {"name": name, "type": "revolute", "origin": list(origin), "axis": list(axis), **kw}


def _pri(name, origin, axis):
    return {"name": name, "type": "prismatic", "origin": list(origin), "axis": list(axis)}


def _serial6(p):
    d1, a1, a2, a3, d4, d6 = (p[k] for k in PARAMS["serial6"])
    z = d1 + a2 + a3
    joints = [_rev("a1", (0, 0, 0), (0, 0, 1)), _rev("a2", (a1, 0, d1), (0, 1, 0)),
              _rev("a3", (a1, 0, d1 + a2), (0, -1, 0)), _rev("a4", (a1, 0, z), (1, 0, 0)),
              _rev("a5", (a1 + d4, 0, z), (0, 1, 0)), _rev("a6", (a1 + d4 + d6, 0, z), (1, 0, 0))]
    return joints, _flange((a1 + d4 + d6, 0, z), _rot((0, 1, 0), math.pi / 2))


def _palletizer4(p):
    bh, so, ua, fa, wd = (p[k] for k in PARAMS["palletizer4"])
    # Parallelogram: the elbow turns by j3 - j2 and the passive wrist by -j3, so
    # the forearm keeps its absolute angle j3 and the flange stays level.
    joints = [_rev("j1", (0, 0, 0), (0, 0, 1)), _rev("j2", (so, 0, bh), (0, 1, 0)),
              _rev("j3", (so, 0, bh + ua), (0, 1, 0), coupled={"j3": 1.0, "j2": -1.0}),
              _rev("wrist", (so + fa, 0, bh + ua), (0, 1, 0), coupled={"j3": -1.0}, passive=True),
              _rev("j6", (so + fa, 0, bh + ua - wd), (0, 0, 1))]
    return joints, _flange((so + fa, 0, bh + ua - wd), _DOWN)


def _scara4(p):
    bh, l1, l2, _stroke, fd = (p[k] for k in PARAMS["scara4"])
    joints = [_rev("a1", (0, 0, 0), (0, 0, 1)), _rev("a2", (l1, 0, bh), (0, 0, 1)),
              _pri("a3", (l1 + l2, 0, bh), (0, 0, -1)), _rev("a4", (l1 + l2, 0, bh - fd), (0, 0, 1))]
    return joints, _flange((l1 + l2, 0, bh - fd), _DOWN)


def _gantry(p, yaw: bool):
    h = p["mount_height"]
    joints = [_pri("x", (0, 0, h), (1, 0, 0)), _pri("y", (0, 0, h), (0, 1, 0)), _pri("z", (0, 0, h), (0, 0, -1))]
    if yaw:
        joints.append(_rev("yaw", (0, 0, h), (0, 0, 1)))
    return joints, _flange((0, 0, h), _DOWN)


def _serial7(p):
    d1, d3, d5, d7 = (p[k] for k in PARAMS["serial7"])
    joints = [_rev("a1", (0, 0, 0), (0, 0, 1)), _rev("a2", (0, 0, d1), (0, 1, 0)),
              _rev("a3", (0, 0, d1), (0, 0, 1)), _rev("a4", (0, 0, d1 + d3), (0, -1, 0)),
              _rev("a5", (0, 0, d1 + d3), (0, 0, 1)), _rev("a6", (0, 0, d1 + d3 + d5), (0, 1, 0)),
              _rev("a7", (0, 0, d1 + d3 + d5 + d7), (0, 0, 1))]
    return joints, _flange((0, 0, d1 + d3 + d5 + d7), np.eye(3))


STRUCTURES = {
    "serial6": Template(
        "serial6", ("a1", "a2", "a3", "a4", "a5", "a6"), ("revolute",) * 6, tuple(PARAMS["serial6"]), "6d",
        _DEFAULTS["serial6"], "wrist",
        ("a1: 0 = arm facing +X; positive turns it left (counter-clockwise from above)",
         "a2: 0 = upper arm vertical; positive leans it forward",
         "a3: 0 = forearm horizontal with the upper arm vertical; positive raises the forearm",
         "a4: 0 = wrist unturned; positive turns it about the forearm, clockwise seen from behind",
         "a5: 0 = wrist straight; positive bends the flange down",
         "a6: 0 = flange unturned; positive turns it about the flange axis, clockwise seen from behind")),
    "palletizer4": Template(
        "palletizer4", ("j1", "j2", "j3", "j6"), ("revolute",) * 4, tuple(PARAMS["palletizer4"]), "xyz_yaw",
        _DEFAULTS["palletizer4"], "flange",
        ("j1: 0 = arm facing +X; positive turns it left (counter-clockwise from above)",
         "j2: 0 = upper arm vertical; positive leans it forward",
         "j3: absolute forearm angle; 0 = forearm horizontal whatever j2 is; positive tilts the forearm down",
         "j6: 0 = flange unturned; positive turns it counter-clockwise from above")),
    "scara4": Template(
        "scara4", ("a1", "a2", "a3", "a4"), ("revolute", "revolute", "prismatic", "revolute"),
        tuple(PARAMS["scara4"]), "xyz_yaw", _DEFAULTS["scara4"], "flange",
        ("a1: 0 = first arm along +X; positive turns it counter-clockwise from above",
         "a2: 0 = second arm in line with the first; positive turns it counter-clockwise from above",
         "a3: 0 = quill at the top of its stroke; positive moves it down (metres)",
         "a4: 0 = flange unturned; positive turns it counter-clockwise from above")),
    "delta": Template(
        "delta", ("a1", "a2", "a3"), ("revolute",) * 3, tuple(PARAMS["delta"]), "xyz",
        _DEFAULTS["delta"], "radius",
        ("a1: upper arm at 0 degrees about Z; 0 = pointing straight out, positive swings it down",
         "a2: upper arm at 120 degrees about Z; 0 = pointing straight out, positive swings it down",
         "a3: upper arm at 240 degrees about Z; 0 = pointing straight out, positive swings it down",
         "a4 (optional): effector turn; positive counter-clockwise from above"),
        optional_axis="a4"),
    "gantry": Template(
        "gantry", ("x", "y", "z"), ("prismatic",) * 3, tuple(PARAMS["gantry"]), "xyz",
        _DEFAULTS["gantry"], "strokes",
        ("x: travel along +X (metres)", "y: travel along +Y (metres)",
         "z: 0 = top of the stroke; positive moves the flange down (metres)",
         "yaw (optional): flange turn; positive counter-clockwise from above"),
        optional_axis="yaw"),
    "serial7": Template(
        "serial7", ("a1", "a2", "a3", "a4", "a5", "a6", "a7"), ("revolute",) * 7, tuple(PARAMS["serial7"]),
        "6d", _DEFAULTS["serial7"], "wrist",
        ("a1: 0 = arm facing +X; positive turns it counter-clockwise from above",
         "a2: 0 = upper arm vertical; positive leans it forward",
         "a3: 0 = upper arm unturned; positive turns it counter-clockwise about itself",
         "a4: 0 = forearm in line with the upper arm; positive bends it back",
         "a5: 0 = forearm unturned; positive turns it counter-clockwise about itself",
         "a6: 0 = wrist straight; positive bends it forward",
         "a7: 0 = flange unturned; positive turns it counter-clockwise about the flange axis")),
}


def template_robot(structure: str, params: dict, axes: list, *, name="robot", tool=None, home=None,
                   reach_m=None) -> Robot:
    """A robot of a known structure from its dimensions and its axis dicts
    (`{name?, range, speed, accel}`, in the template's axis order)."""
    tpl = STRUCTURES.get(structure)
    if tpl is None:
        raise ValueError(f"unknown structure '{structure}'; known: {', '.join(STRUCTURES)}")
    p = {}
    for key in tpl.params:
        v = (params or {}).get(key)
        if v is None:
            raise ValueError(f"{structure}: dimension {key} is missing")
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f"{structure}: dimension {key} must be a number of metres, 0 or more")
        if key in _POSITIVE[structure] and v <= 0:
            raise ValueError(f"{structure}: dimension {key} must be above 0")
        p[key] = float(v)
    n = len(tpl.axes)
    names = list(tpl.axes)
    if tpl.optional_axis and len(axes) == n + 1:
        names.append(tpl.optional_axis)
    if len(axes) != len(names):
        extra = f" (or {n + 1} with {tpl.optional_axis})" if tpl.optional_axis else ""
        raise ValueError(f"{structure} needs {n} axes{extra}, got {len(axes)}")
    named = []
    for i, (a, want) in enumerate(zip(axes, names)):
        if a.get("name", want) != want:
            raise ValueError(f"{structure} axis {i + 1} is named '{want}', not '{a['name']}'")
        named.append({**a, "name": want})
    yaw = len(names) > n
    task = "xyz_yaw" if yaw and tpl.task == "xyz" else tpl.task
    if structure == "delta":
        robot = _delta_robot(p, named, name, tool, home)
    else:
        build = {"serial6": _serial6, "palletizer4": _palletizer4, "scara4": _scara4,
                 "serial7": _serial7}.get(structure)
        joints, flange = build(p) if build else _gantry(p, yaw)
        robot = chain_robot(joints, named, name=name, tool=tool, home=home, structure=structure,
                            task=task, params=p)
        robot = dataclasses.replace(robot, flange=flange)
    return dataclasses.replace(robot, reach_m=None if reach_m is None else float(reach_m))


def _delta_robot(p, axes, name, tool, home) -> Robot:
    axes_t = tuple(_axis_of(a) for a in axes)
    joints = []
    for i, phi in enumerate(np.radians(ARM_ANGLES)):
        c, s = math.cos(phi), math.sin(phi)
        joints.append(Joint(f"a{i + 1}", "revolute", np.array([p["base_radius"] * c, p["base_radius"] * s,
                                                              p["mount_height"]]),
                            np.array([-s, c, 0.0]), None, None))
    if len(axes_t) == 4:
        joints.append(Joint("a4", "revolute", np.array([0.0, 0.0, p["mount_height"]]),
                            np.array([0.0, 0.0, 1.0]), None, None))
    return Robot(name, "delta", tuple(joints), axes_t, tool_pose(tool), home_vector(axes_t, home), dict(p),
                 "xyz_yaw" if len(axes_t) == 4 else "xyz")


# --- analytic inverse kinematics --------------------------------------------
# Each returns every candidate (degrees / metres), unfiltered; chain.ik keeps
# those within limits that reproduce the target and picks the one nearest the seed.

def _flange_target(robot: Robot, target: np.ndarray) -> np.ndarray:
    return np.asarray(target, dtype=float) @ np.linalg.inv(robot.tool)


def _yaw(rot: np.ndarray) -> float:
    return math.degrees(math.atan2(rot[1, 0], rot[0, 0]))


def _two_link(u, w, l1, l2):
    """Absolute directions (rad) of two links from (0, 0) to (u, w): [(phi1, phi2), ...]."""
    c = (u * u + w * w - l1 * l1 - l2 * l2) / (2 * l1 * l2)
    if abs(c) > 1 + 1e-12:
        return []
    out = []
    for d in {math.acos(max(-1.0, min(1.0, c))), -math.acos(max(-1.0, min(1.0, c)))}:
        phi1 = math.atan2(w, u) - math.atan2(l2 * math.sin(d), l1 + l2 * math.cos(d))
        out.append((phi1, phi1 + d))
    return out


def _turns(x, y):
    """Axis 1 candidates (deg) and the signed radius each sees."""
    r = math.hypot(x, y)
    t = math.degrees(math.atan2(y, x)) if r > 1e-12 else 0.0
    return [(t, r), (t + 180.0 if t <= 0 else t - 180.0, -r)]


def _ik_serial6(robot: Robot, target):
    p = robot.params
    a1, a2, a3, d4, d6 = (p[k] for k in ("shoulder_offset", "upper_arm", "elbow_offset", "forearm", "wrist_flange"))
    d1 = p["base_height"]
    f = _flange_target(robot, target)
    wc = f[:3, 3] - d6 * f[:3, 2]
    big_l, beta0 = math.hypot(a3, d4), math.atan2(a3, d4)
    r_wrist = f[:3, :3] @ _rot((0, 1, 0), math.pi / 2).T
    out = []
    for q1, r in _turns(wc[0], wc[1]):
        u, w = r - a1, wc[2] - d1
        s = (u * u + w * w - a2 * a2 - big_l * big_l) / (2 * a2 * big_l)
        if abs(s) > 1 + 1e-12:
            continue
        asn = math.asin(max(-1.0, min(1.0, s)))
        for b3 in {asn, math.pi - asn}:
            q3 = b3 - beta0
            gamma = b3 - math.pi / 2
            phi = math.atan2(w, u) - math.atan2(big_l * math.sin(gamma), a2 + big_l * math.cos(gamma))
            q2 = math.pi / 2 - phi
            r03 = _rot((0, 0, 1), math.radians(q1)) @ _rot((0, 1, 0), q2 - q3)
            rw = r03.T @ r_wrist                       # = Rx(q4) Ry(q5) Rx(q6)
            sb = math.hypot(rw[1, 0], rw[2, 0])
            base = [q1, math.degrees(q2), math.degrees(q3)]
            if sb < 1e-9:
                # Wrist singular: only q4 + q6 is fixed; put it all on q6.
                q5 = 0.0 if rw[0, 0] > 0 else 180.0
                q6 = math.degrees(math.atan2(rw[2, 1], rw[1, 1])) if q5 == 0.0 else \
                    math.degrees(math.atan2(-rw[2, 1], rw[1, 1]))
                out.append(np.array(base + [0.0, q5, q6]))
                continue
            q5 = math.atan2(sb, rw[0, 0])
            q4 = math.atan2(rw[1, 0], -rw[2, 0])
            q6 = math.atan2(rw[0, 1], rw[0, 2])
            out.append(np.array(base + [math.degrees(q4), math.degrees(q5), math.degrees(q6)]))
            out.append(np.array(base + [math.degrees(q4) + 180.0, -math.degrees(q5), math.degrees(q6) + 180.0]))
    return out


def _ik_palletizer4(robot: Robot, target):
    p = robot.params
    f = _flange_target(robot, target)
    wrist = f[:3, 3] + np.array([0.0, 0.0, p["wrist_drop"]])
    yaw = _yaw(f[:3, :3])
    out = []
    for q1, r in _turns(wrist[0], wrist[1]):
        for phi1, phi2 in _two_link(r - p["shoulder_offset"], wrist[2] - p["base_height"],
                                    p["upper_arm"], p["forearm"]):
            out.append(np.array([q1, 90.0 - math.degrees(phi1), -math.degrees(phi2), yaw - q1]))
    return out


def _ik_scara4(robot: Robot, target):
    p = robot.params
    f = _flange_target(robot, target)
    x, y, z = f[:3, 3]
    q3 = p["base_height"] - p["flange_drop"] - z
    yaw = _yaw(f[:3, :3])
    out = []
    for phi1, phi2 in _two_link(x, y, p["arm1"], p["arm2"]):
        q1, q2 = math.degrees(phi1), math.degrees(phi2 - phi1)
        out.append(np.array([q1, q2, q3, yaw - q1 - q2]))
    return out


def _ik_gantry(robot: Robot, target):
    f = _flange_target(robot, target)
    q = [f[0, 3], f[1, 3], robot.params["mount_height"] - f[2, 3]]
    if len(robot.axes) == 4:
        q.append(_yaw(f[:3, :3]))
    return [np.array(q)]


def _ik_delta(robot: Robot, target):
    f = _flange_target(robot, target)
    cands = ik_all(robot.params, f[:3, 3])
    if len(robot.axes) == 4:
        yaw = _yaw(f[:3, :3])
        cands = [np.append(c, yaw) for c in cands]
    return cands


ANALYTIC_IK.update({"serial6": _ik_serial6, "palletizer4": _ik_palletizer4, "scara4": _ik_scara4,
                    "gantry": _ik_gantry, "delta": _ik_delta})


# --- reach ------------------------------------------------------------------

_STEP = 0.25        # degrees, the reach grid


def _grid(axis, step=_STEP) -> np.ndarray:
    g = np.arange(axis.lo, axis.hi, step)
    return np.append(g, axis.hi)


def _planar(robot: Robot, i: int, j: int):
    a, b = np.meshgrid(np.radians(_grid(robot.axes[i])), np.radians(_grid(robot.axes[j])), indexing="ij")
    return a, b


def reach(robot: Robot) -> float:
    """The structure's reach measure over its axis ranges, never the unconstrained length:
    the wrist centre (serial6, serial7) or flange centre (palletizer4, scara4)
    horizontally from axis 1, the working radius (delta) or the longest stroke (gantry)."""
    p, s = robot.params, robot.structure
    if s == "serial6":
        q2, q3 = _planar(robot, 1, 2)
        big_l, beta0 = math.hypot(p["elbow_offset"], p["forearm"]), math.atan2(p["elbow_offset"], p["forearm"])
        r = p["shoulder_offset"] + p["upper_arm"] * np.sin(q2) + big_l * np.cos(beta0 + q3 - q2)
        return float(np.abs(r).max())
    if s == "palletizer4":
        q2, q3 = _planar(robot, 1, 2)
        r = p["shoulder_offset"] + p["upper_arm"] * np.sin(q2) + p["forearm"] * np.cos(q3)
        return float(np.abs(r).max())
    if s == "scara4":
        q2 = np.radians(_grid(robot.axes[1]))
        l1, l2 = p["arm1"], p["arm2"]
        return float(np.sqrt(l1 * l1 + l2 * l2 + 2 * l1 * l2 * np.cos(q2)).max())
    if s == "serial7":
        q2, q4 = _planar(robot, 1, 3)
        r = p["upper_arm"] * np.sin(q2) + p["forearm"] * np.sin(q2 - q4)
        return float(np.abs(r).max())
    if s == "gantry":
        return float(max(a.hi - a.lo for a in robot.axes if a.kind == "prismatic"))
    if s == "delta":
        return _delta_radius(robot)
    if robot.reach_m is not None:
        return float(robot.reach_m)
    raise ValueError(f"structure '{s}' has no reach measure; give reach_m")


def _delta_radius(robot: Robot) -> float:
    """Working radius: the farthest reach about Z in its worst direction, the
    minimum over 5-degree sectors of the farthest effector centre in each.
    A 2-degree grid over the three arms keeps it fast; narrower sectors would
    leave some with too few grid points."""
    g = [_grid(robot.axes[k], 2.0) for k in range(3)]
    q = np.stack(np.meshgrid(*g, indexing="ij"), axis=-1).reshape(-1, 3)
    pts = fk_batch(robot.params, q)
    pts = pts[np.all(np.isfinite(pts), axis=1)]
    rad = np.hypot(pts[:, 0], pts[:, 1])
    sector = ((np.degrees(np.arctan2(pts[:, 1], pts[:, 0])) % 360.0) // 5.0).astype(int) % 72
    far = np.zeros(72)
    np.maximum.at(far, sector, rad)
    return float(far.min())


__all__ = ["ANALYTIC_IK", "PARAMS", "STRUCTURES", "Template", "reach", "template_robot"]
