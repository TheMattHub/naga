"""Delta robot kinematics in closed form.

Three upper arms hang from a base at `mount_height`, their pivots on a circle
of `base_radius` at 0, 120 and 240 degrees about Z. An upper arm's angle is 0
when it points straight out and positive when it swings down. Each forearm
(a parallelogram, one rod here) joins the elbow to the effector at
`effector_radius` from its centre, so the effector stays level. Units: metres
and degrees; the frame is the robot base, Z up.
"""
from __future__ import annotations

import itertools
import math

import numpy as np

ARM_ANGLES = (0.0, 120.0, 240.0)
PARAMS = ("mount_height", "base_radius", "effector_radius", "upper_arm", "forearm")


def _p(params: dict):
    return tuple(float(params[k]) for k in PARAMS)


def _dirs() -> np.ndarray:
    a = np.radians(ARM_ANGLES)
    return np.stack([np.cos(a), np.sin(a), np.zeros(3)], axis=1)     # (3, 3) radial


def fk_batch(params: dict, q: np.ndarray) -> np.ndarray:
    """(N, 3) effector centres for (N, 3) upper-arm angles; NaN where the arms cannot close."""
    h, rb, re, ua, fa = _p(params)
    q = np.radians(np.atleast_2d(np.asarray(q, dtype=float)))
    e = _dirs()
    radial = (rb - re) + ua * np.cos(q)                                # (N, 3)
    # Sphere centres: each elbow moved in by the effector radius.
    c = radial[:, :, None] * e[None, :, :]
    c[:, :, 2] = h - ua * np.sin(q)
    p1, p2, p3 = c[:, 0], c[:, 1], c[:, 2]
    d12 = p2 - p1
    d = np.linalg.norm(d12, axis=1)
    ex = d12 / d[:, None]
    i = np.einsum("ij,ij->i", ex, p3 - p1)
    ey = p3 - p1 - i[:, None] * ex
    j = np.linalg.norm(ey, axis=1)
    ey = ey / j[:, None]
    ez = np.cross(ex, ey)
    x = d / 2.0
    y = (i * i + j * j) / (2.0 * j) - i * x / j
    z2 = fa * fa - x * x - y * y
    with np.errstate(invalid="ignore"):
        z = np.sqrt(z2)
    base = p1 + x[:, None] * ex + y[:, None] * ey
    # The effector hangs below the base: take the intersection with the lower Z.
    sign = np.where(ez[:, 2] > 0, -1.0, 1.0)
    out = base + (sign * z)[:, None] * ez
    out[z2 < 0] = np.nan
    return out


def delta_fk(params: dict, q) -> np.ndarray:
    """(3,) effector centre for the three upper-arm angles (degrees)."""
    out = fk_batch(params, np.asarray(q, dtype=float)[:3])[0]
    if not np.all(np.isfinite(out)):
        raise ValueError("the delta arms cannot close at these angles")
    return out


def ik_branches(params: dict, xyz) -> list | None:
    """Per arm, the two upper-arm angles (degrees) that reach `xyz`; None when one arm cannot."""
    h, rb, re, ua, fa = _p(params)
    x, y, z = (float(v) for v in xyz)
    out = []
    for phi in np.radians(ARM_ANGLES):
        xl = math.cos(phi) * x + math.sin(phi) * y         # into the arm's plane
        yl = -math.sin(phi) * x + math.cos(phi) * y
        a, b = rb - re - xl, h - z
        k = (fa * fa - yl * yl - a * a - b * b - ua * ua) / (2.0 * ua)
        rho = math.hypot(a, b)
        if rho < 1e-12 or abs(k) > rho:
            return None
        delta = math.atan2(b, a)
        base = math.acos(max(-1.0, min(1.0, k / rho)))
        pair = [math.degrees(math.remainder(s * base - delta, 2 * math.pi)) for s in (1.0, -1.0)]
        # Knee out first: the elbow farther from the centre (larger cos).
        pair.sort(key=lambda d: -math.cos(math.radians(d)))
        out.append(pair)
    return out


def ik_all(params: dict, xyz) -> list:
    """Every combination of arm branches that reaches `xyz`."""
    br = ik_branches(params, xyz)
    return [] if br is None else [np.array(c) for c in itertools.product(*br)]


def delta_ik(params: dict, xyz) -> np.ndarray | None:
    """(3,) upper-arm angles (degrees) with every knee out, or None outside the workspace."""
    br = ik_branches(params, xyz)
    return None if br is None else np.array([b[0] for b in br])


def delta_display(params: dict, q) -> dict:
    """Values for drawing: the upper-arm angles `a1`-`a3`, and per forearm the two
    angles of a universal joint at the elbow, `f{i}_a` (about the upper arm's own
    pivot axis, relative to the upper arm) and `f{i}_b` (about the perpendicular
    axis that follows). At f = 0 a forearm is square to its upper arm, pointing
    straight down when the upper arm is level."""
    h, rb, re, ua, fa = _p(params)
    q = np.asarray(q, dtype=float)
    p = delta_fk(params, q[:3])
    out = {}
    for i, phi_deg in enumerate(ARM_ANGLES):
        phi = math.radians(phi_deg)
        qi = math.radians(q[i])
        c, s = math.cos(phi), math.sin(phi)
        # Elbow-to-attachment vector in the arm's frame (x out, y along the pivot, z up).
        px, py = c * p[0] + s * p[1], -s * p[0] + c * p[1]
        dx = px + re - (rb + ua * math.cos(qi))
        dy = py
        dz = p[2] - (h - ua * math.sin(qi))
        n = math.sqrt(dx * dx + dy * dy + dz * dz)
        dx, dy, dz = dx / n, dy / n, dz / n
        alpha = math.atan2(-dx, -dz)
        beta = math.asin(max(-1.0, min(1.0, dy)))
        out[f"a{i + 1}"] = float(q[i])
        out[f"f{i + 1}_a"] = math.degrees(alpha) - float(q[i])
        out[f"f{i + 1}_b"] = math.degrees(beta)
    if len(q) > 3:
        out["a4"] = float(q[3])
    return out
