"""Robot moves: PTP and LIN Move and MoveOverTime timing, the closed cycle plan,
targets in cell coordinates, and the belt intercept.

Every time here is deterministic: a robot move draws nothing from a random
stream. A move runs one normalised trapezoid in its path parameter s (0 to 1),
timed by `motion.move_time(1, V, A)`:

- PTP: all axes share it, with V and A set by the axis that needs longest, so
  every axis stays within its own speed and acceleration and all finish together.
- LIN: the tool runs a straight line (position) with slerp orientation, sampled
  through IK seeded from the previous sample. Joint acceleration along the path
  is q' sddot + q'' sdot^2, so the caps are taken over every sample and axis.
- MoveOverTime: a literal `time` no shorter than the move needs; the same A, the
  V that makes the trapezoid last exactly that long. Never silently stretched.

Frames: the robot base frame is Z up, X forward, metres. The cell frame is the
floor plan's (+x east, +y south, metres, `layout_rotation` clockwise from above)
made right-handed with z up: `(x, -y, z)` with yaw = -layout_rotation.
`cell_point` and `cell_pose` are the one conversion.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# motion imports this module only inside its functions, so this way round is acyclic.
from nagasim.core.motion import ROBOT_KINDS, derate, move_time
from nagasim.core.robotics.chain import (
    IK_ROT_TOL, SINGULAR_TOL, _DOWN, Robot, _rot, fk, ik, joint_values, pose, rot_log, task_sigma_min,
    within_limits)
from nagasim.core.robotics.delta import delta_display
from nagasim.core.utils import is_finite_number

__all__ = ["ROBOT_KINDS", "RobotMoveError", "RobotMove", "ptp_limits", "ptp_seconds", "lin_limits",
           "over_time", "fraction", "cell_point", "cell_pose", "Place", "resolve_target", "CyclePlan",
           "plan_cycle", "joints_at", "display_values", "BeltLine", "belt_line", "Intercept",
           "intercept_ptp", "places_of", "robot_base", "BeltPick"]

_END_TOL = 0.01                        # a LIN's tracked end vs its target, record units
_SAME = 1e-9                           # joint values this close are the same pose


class RobotMoveError(ValueError):
    """A robot move that cannot be planned: `code` is its problem code, and
    `fields` always holds `target` plus what that code's plain line names.
    `segment` is the cycle index, once `plan_cycle` knows it."""

    def __init__(self, code: str, message: str, segment: int | None = None, **fields):
        super().__init__(message)
        self.code, self.segment = code, segment
        self.fields = {"target": "", **fields}


def intercept_error(case: str, message: str, belt="", **fields) -> RobotMoveError:
    # Every field the plain lines of E_ROBOT_INTERCEPT name is always there.
    return RobotMoveError("E_ROBOT_INTERCEPT", message, case=case, belt=str(belt),
                          **{"meet": "", "limit": "", **fields})


@dataclass(frozen=True)
class RobotMove:
    kind: str
    target: str
    q_start: np.ndarray
    q_end: np.ndarray
    seconds: float
    V: float
    A: float
    samples: np.ndarray | None = None       # (n, n_axes) for lin
    meet: float | None = None               # a belt pick's meeting point, metres along the belt


# -- timing ------------------------------------------------------------------------------
def _limits(robot: Robot, payload_kg: float) -> tuple[np.ndarray, np.ndarray]:
    """Axis speeds derated for the payload (the stand-in for dynamics), and accelerations."""
    payload = float(payload_kg or 0.0)
    if robot.payload_max is not None and payload > robot.payload_max + 1e-9:
        raise RobotMoveError("E_MOTION_PAYLOAD", f"payload {payload:g} kg is above the robot's "
                             f"payload_max {robot.payload_max:g} kg",
                             payload=payload, payload_max=robot.payload_max)
    factor = derate({"payload_max": robot.payload_max, "payload_derate": robot.payload_derate}, payload)
    return (np.array([a.speed for a in robot.axes]) * factor,
            np.array([a.accel for a in robot.axes]))


def ptp_limits(robot: Robot, q0, q1, payload_kg=0.0) -> tuple[float, float]:
    """V and A of the shared normalised trapezoid; inf when q0 == q1."""
    speed, accel = _limits(robot, payload_kg)
    dq = np.abs(np.asarray(q1, dtype=float) - np.asarray(q0, dtype=float))
    moving = dq > _SAME
    if not moving.any():
        return math.inf, math.inf
    return float(np.min(speed[moving] / dq[moving])), float(np.min(accel[moving] / dq[moving]))


def ptp_seconds(robot: Robot, q0, q1, payload_kg=0.0) -> float:
    V, A = ptp_limits(robot, q0, q1, payload_kg)
    return 0.0 if math.isinf(V) else move_time(1.0, V, A)


def _lin_pose(p0: np.ndarray, p1: np.ndarray, s: float) -> np.ndarray:
    """The pose at s along the straight line from p0 to p1, orientation by slerp."""
    out = np.eye(4)
    out[:3, 3] = p0[:3, 3] + (p1[:3, 3] - p0[:3, 3]) * s
    w = rot_log(p0[:3, :3].T @ p1[:3, :3])
    angle = float(np.linalg.norm(w))
    out[:3, :3] = p0[:3, :3] @ (_rot(w / angle, angle * s) if angle > 1e-12 else np.eye(3))
    return out


def lin_limits(robot: Robot, q0, q1, payload_kg=0.0) -> tuple[float, float, np.ndarray]:
    """(sdot cap, sddot cap, samples) of the straight line from fk(q0) to fk(q1).

    The cap is uniform over the path: deterministic and never above a limit, at
    the cost of a slightly long time near a singularity. With `|q''_i| sdot^2`
    the curvature share of an axis's acceleration, the path acceleration gets
    what is left, `(a_i - |q''_i| sdot^2) / |q'_i|`; sdot keeps the curvature
    share to at most half of `a_i`, so a straight joint path (q'' = 0, a
    gantry) gets the whole budget and is the one-axis trapezoid.
    """
    q0, q1 = np.asarray(q0, dtype=float), np.asarray(q1, dtype=float)
    speed, accel = _limits(robot, payload_kg)
    p0, p1 = fk(robot, q0), fk(robot, q1)
    length = float(np.linalg.norm(p1[:3, 3] - p0[:3, 3]))
    angle = math.degrees(float(np.linalg.norm(rot_log(p0[:3, :3].T @ p1[:3, :3]))))
    n = int(np.clip(max(16, math.ceil(length / 0.005), math.ceil(angle / 1.0)), 16, 2000))
    s = np.linspace(0.0, 1.0, n)
    samples, seed = [q0], q0
    for k in range(1, n):
        q = ik(robot, _lin_pose(p0, p1, s[k]), seed)
        if q is None:
            raise RobotMoveError("E_ROBOT_LIN", f"the line leaves the reach at {s[k]:.2f} of the way",
                                 case="reach", at=round(float(s[k]), 2))
        samples.append(q)
        seed = q
    samples = np.array(samples)
    for k in range(n):
        if task_sigma_min(robot, samples[k]) < SINGULAR_TOL:
            raise RobotMoveError("E_ROBOT_SINGULAR", f"the line passes a singular pose at "
                                 f"{s[k]:.2f} of the way", at=round(float(s[k]), 2))
    if float(np.max(np.abs(samples[-1] - q1))) > _END_TOL:
        # The tracked IK ends in another arm configuration than the target: a
        # straight line cannot get there, and the move is never drawn as a jump.
        raise RobotMoveError("E_ROBOT_LIN", "the line ends in another arm configuration than the "
                             "target", case="config", at=1.0)
    samples[-1] = q1
    h = s[1] - s[0]
    # q' per segment (the slope between two samples), the largest beside each sample;
    # q'' the second difference, the inner value repeated at the ends.
    slope = np.abs(np.diff(samples, axis=0)) / h
    qp = np.maximum(np.vstack([slope[:1], slope]), np.vstack([slope, slope[-1:]]))
    qpp = np.abs(samples[2:] - 2 * samples[1:-1] + samples[:-2]) / h ** 2
    qpp = np.vstack([qpp[:1], qpp, qpp[-1:]])
    qp_max, qpp_max = qp.max(axis=0), qpp.max(axis=0)
    with np.errstate(divide="ignore"):
        caps = [math.inf]
        caps += list(np.where(qp_max > _SAME, speed / qp_max, math.inf))
        caps += list(np.where(qpp_max > _SAME, np.sqrt(accel / (2 * qpp_max)), math.inf))
        if robot.tcp_speed and length > _SAME:
            caps.append(robot.tcp_speed / length)
        sdot = float(min(caps))
        caps = [math.inf]
        left = accel[None, :] - qpp * sdot ** 2                  # per sample, per axis
        caps += list(np.where(qp > _SAME, left / np.where(qp > _SAME, qp, 1.0), math.inf).ravel())
        if robot.tcp_accel and length > _SAME:
            caps.append(robot.tcp_accel / length)
        sddot = float(min(caps))
    return sdot, sddot, samples


def over_time(A: float, T: float, T_min: float) -> float:
    """The V of the trapezoid at acceleration A that lasts exactly T."""
    if T < T_min - 1e-9:
        raise RobotMoveError("E_ROBOT_MOVE_TIME", f"the move needs {T_min:.4g} s, not {T:.4g} s",
                             needed=T_min, given=T)
    disc = max(A * A * T * T - 4.0 * A, 0.0)
    return (A * T - math.sqrt(disc)) / 2.0


def fraction(V: float, A: float, T: float, tau: float) -> float:
    """s in [0, 1] at time tau of the trapezoid (or triangle) `move_time` times."""
    if T <= 0 or math.isinf(V) or math.isinf(A):
        return 1.0
    tau = min(max(tau, 0.0), T)
    peak = min(V, math.sqrt(A))             # a triangle never reaches V
    ramp = peak / A
    if tau <= ramp:
        return 0.5 * A * tau * tau
    if tau >= T - ramp:
        return 1.0 - 0.5 * A * (T - tau) ** 2
    return 0.5 * A * ramp * ramp + peak * (tau - ramp)


# -- frames and targets ------------------------------------------------------------------
def cell_point(x, y, z=0.0) -> np.ndarray:
    return np.array([float(x), -float(y), float(z)])


def cell_pose(x, y, rotation_deg, z=0.0) -> np.ndarray:
    """4x4 pose of a floor-plan place (rotation clockwise from above) in the cell frame."""
    out = np.eye(4)
    out[:3, :3] = _rot((0, 0, 1), -math.radians(float(rotation_deg or 0.0)))
    out[:3, 3] = cell_point(x, y, z)
    return out


@dataclass(frozen=True)
class Place:
    """A block's place on the floor plan, in metres (#1986)."""
    x: float
    y: float
    rotation: float
    length: float | None = None


def _place_of(block: dict, nodes: dict, length_unit: str) -> Place | None:
    from nagasim.core.network import block_place, rotate_offset
    from nagasim.core.timeunits import conversion_factor
    params = block.get("params") or {}
    try:
        # A network node or a belt's ends are on the floor plan already; a `layout_position`
        # is the block's own, inside its asset when it has one.
        world = block_place(params, None, nodes)
        own = block.get("layout_position")
        own = (float(own[0]), float(own[1])) if own else None
    except (TypeError, ValueError, IndexError, KeyError):   # #1979: a malformed place is no place
        return None
    rotation = float(block.get("layout_rotation") or 0.0) if is_finite_number(
        block.get("layout_rotation") or 0.0) else 0.0
    # A block flattened from a placed asset sits in the instance's frame (D1343, #1981): its own
    # place turned and moved by the instance's pose, the instance's place when it has none.
    # Only a flattened block (`instance/name`) has one; one written by hand moves nothing (#1989).
    inst = block.get("instance_place") if "/" in str(block.get("name", "")) else None
    if _numbers(inst, 3):
        ix, iy, turn = (float(v) for v in inst)
        if world is None:
            dx, dy = rotate_offset(*(own or (0.0, 0.0)), turn)
            world = (ix + dx, iy + dy)
        rotation += turn
    elif world is None:
        world = own
    if world is None:
        return None
    # #1986: robot geometry is in metres; the floor plan is in the model's length unit.
    try:
        to_m = conversion_factor("length", length_unit, "meters")
    except (KeyError, ValueError):
        return None             # an unknown model unit: validation says why
    length = params.get("length")
    unit = str(params.get("length_unit") or "base")
    try:
        length = (float(length) * conversion_factor(
            "length", length_unit if unit == "base" else unit, "meters") if is_finite_number(length)
                    and length > 0 else None)
    except (KeyError, ValueError):
        length = None
    return Place(float(world[0]) * to_m, float(world[1]) * to_m, rotation, length)


def places_of(layout: dict) -> dict:
    """{block name: Place} of every block with a place on the floor plan: its
    network node, its belt's midpoint or its `layout_position` (inside the asset
    instance it was flattened from, when it was), in metres."""
    nodes = {str(n.get("name")): n for n in (layout.get("network") or {}).get("nodes") or []
             if isinstance(n, dict)}
    length_unit = str((layout.get("simulation") or {}).get("length_unit") or "meters")
    out = {}
    for block in layout.get("blocks") or []:
        if isinstance(block, dict):
            place = _place_of(block, nodes, length_unit)
            if place is not None:
                out[str(block.get("name"))] = place
    return out


def robot_base(layout: dict, block_name: str) -> np.ndarray | None:
    """The cell pose of the robot that block *block_name* runs, or None when it has
    no place on the floor plan."""
    place = places_of({**layout, "blocks": [b for b in layout.get("blocks") or []
                                            if isinstance(b, dict) and b.get("name") == block_name]
                       }).get(block_name)
    return None if place is None else cell_pose(place.x, place.y, place.rotation)


def _numbers(v, n: int) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == n and all(is_finite_number(x) for x in v)


def _cell_frame(target: dict, places: dict, placed: bool) -> np.ndarray:
    """The tool pose a frame or on-block target asks for, in the cell frame."""
    if "on" in target:
        block = str(target.get("on"))
        place = (places or {}).get(block)
        if place is None or not placed:
            raise RobotMoveError("E_ROBOT_TARGET", f"block '{block}' has no place on the floor plan",
                                 case="place")
        at = target.get("at", "centre")
        offset = target.get("offset", [0, 0, 0])
        if at not in ("centre", "start", "end") or not _numbers(offset, 3) or not is_finite_number(
                target.get("yaw_deg", 0)) or (at != "centre" and not place.length):
            raise RobotMoveError("E_ROBOT_TARGET", "an on-block target is {on, at?: centre|start|"
                                 "end (of a block with a length), offset?: [dx, dy, dz], yaw_deg?}",
                                 case="shape")
        along = {"centre": 0.0, "start": -0.5, "end": 0.5}[at] * float(place.length or 0.0)
        frame = cell_pose(place.x, place.y, place.rotation)
        local = np.eye(4)
        # #1985: the offset is in the block's floor-plan frame (dy toward +y at rotation 0, as
        # layout and frame `xyz` y), so its dy flips into the right-handed cell frame.
        dx, dy, dz = (float(v) for v in offset)
        local[:3, 3] = np.array([along + dx, -dy, dz])
        # yaw_deg on a block turns from the block's facing, clockwise as layout_rotation
        local[:3, :3] = _rot((0, 0, 1), -math.radians(float(target.get("yaw_deg", 0)))) @ _DOWN
        return frame @ local
    if not placed:
        raise RobotMoveError("E_ROBOT_TARGET", "a frame target needs the robot's block placed on "
                             "the floor plan", case="place")
    xyz, rpy, yaw = target.get("xyz"), target.get("rpy_deg"), target.get("yaw_deg", 0)
    if not _numbers(xyz, 3) or (rpy is not None and not _numbers(rpy, 3)) or not is_finite_number(yaw):
        raise RobotMoveError("E_ROBOT_TARGET", "a frame target is {xyz: [x, y, z], yaw_deg?, "
                             "rpy_deg?}", case="shape")
    if rpy is not None:
        return pose(cell_point(*xyz), rpy)
    out = np.eye(4)
    out[:3, :3] = _rot((0, 0, 1), -math.radians(float(yaw))) @ _DOWN
    out[:3, 3] = cell_point(*xyz)
    return out


def resolve_target(robot: Robot, base: np.ndarray, target: dict, places: dict, seed,
                   placed: bool = True) -> np.ndarray:
    """The axis values a target asks for: a joint vector checked against the
    limits, or a cell frame / on-block point mapped by inv(base) and solved by IK.
    *placed* False: the robot's block has no place, so only joint targets resolve."""
    if not isinstance(target, dict):
        raise RobotMoveError("E_ROBOT_TARGET", "a target is a joint list, a frame or a point on a "
                             "block", case="shape")
    if "belt" in target:
        # Only plan_cycle meets a belt target, and only as the cycle's first move.
        raise intercept_error("first", f"the pick from belt '{target.get('belt')}' must be the "
                               "cycle's first move", target.get("belt"))
    if "joints" in target:
        q = target["joints"]
        if not _numbers(q, len(robot.axes)):
            raise RobotMoveError("E_ROBOT_TARGET", f"joints must be {len(robot.axes)} numbers, one "
                                 "per axis", case="shape")
        q = np.asarray(q, dtype=float)
        if not within_limits(robot, q):
            raise RobotMoveError("E_ROBOT_REACH", "a joint value is outside its axis range",
                                 case="limit")
        try:
            fk(robot, q)        # a delta's arms may not close at these angles
        except ValueError as exc:
            raise RobotMoveError("E_ROBOT_REACH", str(exc), case="reach") from exc
        return q
    if "on" not in target and "xyz" not in target:
        raise RobotMoveError("E_ROBOT_TARGET", "a target is a joint list, a frame or a point on a "
                             "block", case="shape")
    wanted = np.linalg.inv(base) @ _cell_frame(target, places, placed)
    q = ik(robot, wanted, seed)
    if q is None:
        raise RobotMoveError("E_ROBOT_REACH", "the target is out of reach", case="reach")
    # A robot without full orientation (xyz, xyz + yaw) ignores the tilt in IK:
    # a tilt it cannot make is refused here, its yaw never is.
    tilt = float(np.dot(fk(robot, q)[:3, 2], wanted[:3, 2]))
    if math.acos(max(-1.0, min(1.0, tilt))) > IK_ROT_TOL * 10:
        raise RobotMoveError("E_ROBOT_REACH", "the target is tilted and the robot cannot tilt its tool",
                             case="orientation")
    return q


# -- the cycle ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CyclePlan:
    moves: dict                 # {segment index: RobotMove}
    held: tuple                 # the robot's axis values at every segment index
    errors: tuple               # RobotMoveError, in segment order


def _time(segment: dict) -> float | None:
    t = segment.get("time")
    return float(t) if is_finite_number(t) and t >= 0 else None


def _belt_target(targets: dict, segment) -> bool:
    name = segment.get("to") if isinstance(segment, dict) else None
    return isinstance(name, str) and isinstance((targets or {}).get(name), dict) \
        and "belt" in targets[name]


def plan_cycle(robot: Robot, targets: dict, segments: list, time_unit_seconds: float = 1.0, *,
               base: np.ndarray | None = None, places: dict | None = None,
               placed: bool = True, belts: dict | None = None) -> CyclePlan:
    """Every robot move of a closed cycle, timed, with every error collected.

    Targets resolve in segment order, seeded from `home` and then from the
    previous resolved target. The move into a robot segment starts where the
    previous robot segment ended; the first starts where the last one ends, so
    every cycle is the same. *base* (identity when None) is the robot's cell
    pose; *placed* False means its block has no place on the floor plan.

    A belt target ({belt, divert, z?, yaw_deg?}) is a pick from a moving belt:
    segment 0's ptp, met by `intercept_ptp` from the pose that closes the cycle,
    on the line *belts* gives ({target name: BeltPick}). That move lasts the
    meeting time, and `RobotMove.meet` is the meeting point."""
    base = np.eye(4) if base is None else np.asarray(base, dtype=float)
    errors, ends = [], {}
    seed = robot.home
    robot_idx = [i for i, s in enumerate(segments)
                 if isinstance(s, dict) and s.get("kind") in ROBOT_KINDS]
    picks = bool(segments) and _belt_target(targets, segments[0]) \
        and isinstance(segments[0], dict) and segments[0].get("kind") in ROBOT_KINDS
    for i in robot_idx:
        name = segments[i].get("to")
        try:
            if not isinstance(name, str) or name not in (targets or {}):
                raise RobotMoveError("E_ROBOT_TARGET", f"no target named '{name}'", case="unknown")
            if picks and i == 0:
                continue                    # met below, once the closing pose is known
            if picks and _belt_target(targets, segments[i]):
                raise intercept_error("two", "a cycle picks from a moving belt once",
                                       targets[name].get("belt"))
            ends[i] = resolve_target(robot, base, targets[name], places or {}, seed, placed)
            seed = ends[i]
        except RobotMoveError as exc:
            errors.append(at_segment(exc, i, name))
    meet = None
    if picks:
        name = segments[0]["to"]
        try:
            meet = _meet(robot, base, segments[0], targets[name], (belts or {}).get(name),
                         [i for i in robot_idx if i], ends)
        except RobotMoveError as exc:
            errors.append(at_segment(exc, 0, name))
        if meet is not None:
            ends[0] = meet.q_end
    moves = {}
    for k, i in enumerate(robot_idx):
        if i not in ends:
            continue
        if i == 0 and meet is not None:
            moves[0] = meet
            continue
        before = next((j for j in reversed(robot_idx[:k] or robot_idx) if j in ends), i)
        segment, name = segments[i], segments[i].get("to")
        try:
            moves[i] = _move(robot, segment, ends[before], ends[i], time_unit_seconds)
        except RobotMoveError as exc:
            errors.append(at_segment(exc, i, name))
    held, last = [], (ends[max(ends)] if ends else robot.home)
    for i in range(len(segments)):
        last = ends.get(i, last)
        held.append(np.array(last))
    return CyclePlan(moves, tuple(held), tuple(errors))


def _meet(robot: Robot, base: np.ndarray, segment: dict, target: dict, pick, closing: list,
          ends: dict) -> RobotMove | None:
    """Segment 0's ptp to the part on the belt, from the pose that closes the cycle (the end
    of the last other robot move, else home); None when that pose did not resolve, since
    its own error says why."""
    belt = target.get("belt")
    if segment.get("kind") != "ptp":
        raise intercept_error("ptp", "a pick from a moving belt is a ptp move", belt)
    if not isinstance(pick, BeltPick):
        raise intercept_error("belt", f"divert {target.get('divert')} of '{belt}' is no robot "
                               "pick on a placed belt with a length", belt)
    if closing and closing[-1] not in ends:
        return None
    q0 = ends[closing[-1]] if closing else np.asarray(robot.home, dtype=float)
    payload = segment.get("payload_kg")
    payload = float(payload) if is_finite_number(payload) and payload > 0 else 0.0
    # yaw_deg turns from the belt's facing, as on a block, so a turned cell meets the same way.
    facing = -math.degrees(math.atan2(pick.line.direction[1], pick.line.direction[0]))
    yaw = target.get("yaw_deg", 0)
    got = intercept_ptp(robot, base, q0, pick.line, (float(yaw) if is_finite_number(yaw) else 0.0) + facing,
                        payload)
    if got is None:
        raise intercept_error("reach", f"the robot never reaches a part on '{belt}'", belt)
    meet = pick.line.s0 + pick.line.speed * got.t
    if meet > pick.limit - 1e-9:
        raise intercept_error("past", f"the robot meets the part at {meet:.4g} m, past "
                               f"{pick.limit:.4g} m on '{belt}'", belt, meet=round(meet, 3),
                               limit=round(pick.limit, 3))
    V, A = ptp_limits(robot, q0, got.q, payload)
    if not math.isinf(V):
        # The move lasts the meeting time exactly, as a MoveOverTime, so the robot and the
        # part arrive at one instant, not a bisection step apart.
        V = over_time(A, got.t, min(got.seconds, got.t))
    return RobotMove("ptp", str(segment.get("to")), q0, got.q, got.t, V, A, None, meet)


def at_segment(exc: RobotMoveError, index: int, name) -> RobotMoveError:
    exc.segment, exc.fields["target"] = index, str(name)
    return exc


def _move(robot: Robot, segment: dict, q0: np.ndarray, q1: np.ndarray, unit: float) -> RobotMove:
    kind, payload = segment["kind"], segment.get("payload_kg")
    payload = float(payload) if is_finite_number(payload) and payload > 0 else 0.0      # else E_MOTION_SEGMENT
    given = _time(segment)
    given = None if given is None else given * unit
    samples = None
    if float(np.max(np.abs(q1 - q0))) <= _SAME:
        _limits(robot, payload)              # the payload check still applies
        V = A = math.inf
        needed = 0.0
    elif kind == "ptp":
        V, A = ptp_limits(robot, q0, q1, payload)
        needed = move_time(1.0, V, A)
    else:
        V, A, samples = lin_limits(robot, q0, q1, payload)
        needed = move_time(1.0, V, A)
    if given is None:
        return RobotMove(kind, str(segment.get("to")), q0, q1, needed, V, A, samples)
    if needed > 0:
        V = over_time(A, given, needed)
    return RobotMove(kind, str(segment.get("to")), q0, q1, given, V, A, samples)


def joints_at(move: RobotMove, progress: float) -> np.ndarray:
    """Axis values at *progress* (0 to 1 of the move's time)."""
    s = fraction(move.V, move.A, move.seconds, float(progress) * move.seconds)
    if move.samples is None:
        return move.q_start + (move.q_end - move.q_start) * s
    pos = s * (len(move.samples) - 1)
    k = min(int(pos), len(move.samples) - 2)
    return move.samples[k] + (move.samples[k + 1] - move.samples[k]) * (pos - k)


def display_values(robot: Robot, q) -> dict:
    """{chain joint name: value} for the rig; the delta's display angles for a delta."""
    if robot.structure == "delta":
        return delta_display(robot.params, q)
    return {j.name: float(v) for j, v in zip(robot.joints, joint_values(robot, q))}


# -- the belt intercept (the robot side of a conveyor pick) -----------------------------
@dataclass(frozen=True)
class BeltLine:
    """A product on a belt, cell frame: at `origin + direction * (s0 + speed * t)`."""
    origin: np.ndarray
    direction: np.ndarray
    speed: float
    s0: float


def belt_line(place: Place, speed: float, s0: float, z: float = 0.0) -> BeltLine:
    """The line of a conveyor at *place*: from its start end along its facing, *z* metres
    above the floor (the grip height)."""
    frame = cell_pose(place.x, place.y, place.rotation)
    origin = frame[:3, 3] - frame[:3, 0] * float(place.length or 0.0) / 2.0
    origin[2] += float(z)
    return BeltLine(origin, frame[:3, 0].copy(), float(speed), float(s0))


@dataclass(frozen=True)
class BeltPick:
    """A robot pick from a moving belt: the part's line from the call on, and `limit`, the
    metres along the belt the meeting point stays below (the next point after the call,
    else the belt's end)."""
    line: BeltLine
    limit: float


@dataclass(frozen=True)
class Intercept:
    t: float
    point: np.ndarray
    q: np.ndarray
    seconds: float


def intercept_ptp(robot: Robot, base: np.ndarray, q_start, belt: BeltLine, yaw_deg=0.0,
                  payload_kg=0.0, t_max=60.0) -> Intercept | None:
    """The earliest t at which a PTP from *q_start* reaches the belt point it
    meets: the first 0.05 s step that makes it, then bisection to 1e-6 s. None
    when no t in [0, t_max] is reachable."""
    q_start = np.asarray(q_start, dtype=float)
    inv = np.linalg.inv(base)

    def at(t):
        point = belt.origin + belt.direction * (belt.s0 + belt.speed * t)
        frame = np.eye(4)
        frame[:3, :3] = _rot((0, 0, 1), -math.radians(float(yaw_deg))) @ _DOWN
        frame[:3, 3] = point
        q = ik(robot, inv @ frame, q_start)
        seconds = None if q is None else ptp_seconds(robot, q_start, q, payload_kg)
        return point, q, seconds

    lo, t = None, 0.0
    while t <= t_max + 1e-12:
        point, q, seconds = at(t)
        if seconds is not None and seconds <= t:
            break
        lo, t = t, round(t + 0.05, 10)
    else:
        return None
    hi = t
    if lo is not None:
        while hi - lo > 1e-6:
            mid = (lo + hi) / 2.0
            _p, _q, sec = at(mid)
            if sec is not None and sec <= mid:
                hi = mid
            else:
                lo = mid
        point, q, seconds = at(hi)
    return Intercept(hi, point, q, seconds)
