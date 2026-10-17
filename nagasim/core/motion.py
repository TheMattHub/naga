"""Motion timing (automation sub-project 1, D156-D158): how long one move of a
MOTION cycle takes, and the cycle's nominal time for the editor.

A move of `distance_m` at speed v and acceleration a follows a trapezoidal
velocity profile, rest to rest: t = d/v + v/a when d >= v^2/a (it reaches v),
else the triangle t = 2*sqrt(d/a). v is the profile's speed -- or its
`collaborative_speed` when a human-robot collaboration mode applies (D158) --
derated linearly with payload, from 1.0 at 0 kg to `payload_derate` at
`payload_max`. SI units throughout: metres, seconds, kilograms.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.problems import problem
from nagasim.core.quantities import scaled
from nagasim.core.timeunits import base_factor, conversion_factor, factor
from nagasim.core.utils import is_finite_number

__all__ = ["SEGMENT_KINDS", "TIMED_KINDS", "PROFILE_KINDS", "HRC_MODES", "derate",
           "move_speed", "move_time", "move_seconds", "collaborative_now", "segment_seconds",
           "nominal_cycle_seconds", "build_motion_profiles", "validate_motion"]

SEGMENT_KINDS = ("move", "pick", "place", "tool_change", "dwell", "process")
TIMED_KINDS = SEGMENT_KINDS[1:]            # every kind but "move" carries a `time`
PROFILE_KINDS = ("robot", "human", "mechanism")      # descriptive only: one set of rules (D157)
HRC_MODES = ("none", "collaborative", "separation")  # D158


def _number(value) -> float | None:
    """*value* as a finite float, or None (a bool is not a number here)."""
    return float(value) if is_finite_number(value) else None


def derate(profile: dict, payload_kg=0.0) -> float:
    """Speed fraction while carrying *payload_kg*: linear from 1.0 at 0 kg to
    `payload_derate` at `payload_max`, held there above it (validation refuses a
    payload above the maximum, E_MOTION_PAYLOAD). 1.0 when the profile has no
    `payload_max`: nothing says how a load slows it."""
    payload = _number(payload_kg) or 0.0
    payload_max = _number(profile.get("payload_max"))
    if payload <= 0 or payload_max is None or payload_max <= 0:
        return 1.0
    floor = _number(profile.get("payload_derate"))
    floor = 1.0 if floor is None else floor
    return 1.0 - (1.0 - floor) * min(payload / payload_max, 1.0)


def move_speed(profile: dict, payload_kg=0.0, collaborative: bool = False) -> float:
    """m/s for one move: the profile's speed, replaced by `collaborative_speed`
    when the HRC mode applies, times the payload derating (a load slows a
    collaborating robot too)."""
    base = profile["collaborative_speed"] if collaborative else profile["speed"]
    return float(base) * derate(profile, payload_kg)


def move_time(distance_m: float, speed: float, accel: float) -> float:
    """Seconds to move *distance_m* rest to rest: trapezoidal when the move is
    long enough to reach *speed* (d >= v^2/a), triangular otherwise."""
    if distance_m <= 0:
        return 0.0
    if distance_m >= speed * speed / accel:
        return distance_m / speed + speed / accel
    return 2.0 * math.sqrt(distance_m / accel)


def move_seconds(profile: dict, segment: dict, collaborative: bool = False) -> float:
    """Seconds one `move` segment takes with *profile*: its distance at the
    payload-derated (and, when *collaborative*, slowed) speed."""
    speed = move_speed(profile, segment.get("payload_kg") or 0.0, collaborative)
    return move_time(float(segment.get("distance_m") or 0.0), speed, float(profile["accel"]))


def collaborative_now(ctx, hrc: str, zone: dict) -> bool:
    """D158: "collaborative" always; "separation" when a worker-fleet unit
    is within `radius_m` of the zone node, by network distance, right now.
    Mover.distance_to reads the true mid-walk position of an interruptible
    drive (a worker walking home, `park: "home"`); a worker summoned to a
    station walks non-interruptibly and counts at the node it left until
    it arrives (Mover.node). ponytail: good enough for a zone check, since
    a summons walk ends at the station anyway."""
    if hrc == "collaborative":
        return True
    if hrc != "separation":
        return False
    node, radius = str(zone.get("node", "")), float(zone.get("radius_m", 0.0))
    return any(unit.distance_to(ctx.network, node) <= radius
               for fleet in ctx.fleets.values() if fleet.kind == "worker"
               for unit in fleet.movers)


def segment_seconds(block, entity, segments, profile: dict, time_unit: str = "seconds",
                    hrc: str = "none", zone: dict | None = None) -> list[tuple[str, float, bool]]:
    """One `(kind, duration, collaborative)` per segment of a cycle that
    starts NOW, durations in MODEL time units: a timed segment's `time` is
    sampled in order on *block*'s stream (in *time_unit*), a move is timed
    from *profile* with the HRC mode as it stands at this instant. A cobot's
    tending cycle (core/tending.py) is one uninterrupted handling time, so it
    is timed whole; MOTION times each move when that move starts (D158) and so
    calls `move_seconds`/`collaborative_now` per move instead."""
    collaborative = collaborative_now(block.ctx, hrc, zone or {})
    to_base = base_factor("time", "seconds", block.ctx.units)
    out = []
    for segment in segments:
        kind = segment.get("kind")
        if kind == "move":
            out.append((kind, move_seconds(profile, segment, collaborative) * to_base,
                        collaborative))
        else:
            dist = StochasticDistribution("Constant", {"value": segment.get("time") or 0},
                                          time_unit=time_unit)
            out.append((kind, block.sample_duration(dist, entity), False))
    return out


def nominal_cycle_seconds(segments, profile: dict | None, time_unit: str = "seconds",
                          model_unit: str = "minutes") -> float | None:
    """The cycle's nominal time in SECONDS, for the editor: each timed
    segment's mean (its expression evaluated in deterministic mode, D119) in
    *time_unit* ("base" = *model_unit*), every move at the profile's speed after
    payload derating and with no HRC slowdown. None when a segment cannot be
    evaluated outside a run (it reads an entity attribute, a variable, a
    table), a move has no usable profile, or a unit is unknown."""
    scope = ExpressionScope(ctx=SimpleNamespace(deterministic=True))
    total = 0.0
    try:
        to_seconds = factor(model_unit if time_unit == "base" else time_unit, "seconds")
        for segment in segments:
            if segment.get("kind") == "move":
                if not profile or (_number(profile.get("speed")) or 0) <= 0 \
                        or (_number(profile.get("accel")) or 0) <= 0:
                    return None
                total += move_seconds(profile, segment)
                continue
            value = float(compile_expression(str(segment.get("time", ""))).evaluate(scope))
            total += max(value, 0.0) * to_seconds
    except (ExpressionError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return total


def build_motion_profiles(layout: dict) -> dict[str, dict]:
    """{name: profile record} for the run; MOTION blocks read it from ctx."""
    return {str(p["name"]): _profile_in_si(p) for p in layout.get("motion_profiles") or []
            if isinstance(p, dict) and p.get("name")}


def _profile_in_si(profile: dict) -> dict:
    """A profile with its speeds in m/s and payload_max in kg: motion keeps its
    SI internals (D157) whatever `speed_unit`/`payload_unit` it was written in
    ("base" reads as the SI unit itself). A factor of 1.0 changes nothing."""
    out = dict(profile)
    speed_unit = out.get("speed_unit") or "m/s"
    speed_unit = "m/s" if speed_unit == "base" else speed_unit
    for name in ("speed", "collaborative_speed"):
        if _number(out.get(name)) is not None:
            out[name] = scaled(out[name], conversion_factor("speed", speed_unit, "m/s"))
    payload_unit = out.get("payload_unit") or "kg"
    payload_unit = "kg" if payload_unit == "base" else payload_unit
    if _number(out.get("payload_max")) is not None:
        out["payload_max"] = scaled(out["payload_max"],
                                    conversion_factor("mass", payload_unit, "kg"))
    return out


def _positive(value) -> bool:
    number = _number(value)
    return number is not None and number > 0


def validate_motion(layout: dict, problems: list) -> None:
    """The `motion_profiles` section's hook (core/sections.py), which also
    checks every MOTION block's cycle, HRC mode and zone: a block check needs
    only the profile names and the network node names, both read here from
    the layout (validated flattened, so a `$arg` is already a value)."""
    profiles = _validate_profiles(layout, problems)
    nodes = {str(n.get("name", "")).strip()
             for n in (layout.get("network") or {}).get("nodes") or [] if isinstance(n, dict)}
    for index, block in enumerate(layout.get("blocks") or []):
        if str(block.get("type", "")).upper() == "MOTION":
            _validate_block(block, f"/blocks/{index}/params", profiles, nodes, problems)


def _validate_profiles(layout: dict, problems: list) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for index, record in enumerate(layout.get("motion_profiles") or []):
        at = f"/motion_profiles/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""
        if not name or name in profiles:
            problems.append(problem("E_MOTION_PROFILE",
                                    f"motion profile {index}: empty or duplicate name '{name}'",
                                    f"{at}/name", hint="give every motion profile its own name"))
            continue
        profiles[name] = record

        def bad(field, text, hint, name=name, at=at):
            problems.append(problem("E_MOTION_PROFILE", f"motion profile '{name}': {text}",
                                    f"{at}/{field}", hint=hint))

        for field in ("speed", "accel"):
            if not _positive(record.get(field)):
                bad(field, f"{field} must be a number > 0", "speed in m/s, accel in m/s^2")
        if record.get("kind", "robot") not in PROFILE_KINDS:
            bad("kind", f"unknown kind '{record.get('kind')}'", f"one of: {', '.join(PROFILE_KINDS)}")
        # None is "not set": the Model data grid writes an emptied optional cell as None.
        for field in ("payload_max", "collaborative_speed"):
            if record.get(field) is not None and not _positive(record[field]):
                bad(field, f"{field} must be a number > 0", "clear it, or give a positive number")
        floor = record.get("payload_derate")
        if floor is not None and not (_positive(floor) and floor <= 1):
            bad("payload_derate", "payload_derate must be in (0, 1]",
                "the speed fraction left at payload_max")
    return profiles


def _validate_block(block: dict, at: str, profiles: dict, nodes: set, problems: list) -> None:
    name = block.get("name", "<unnamed>")
    params = block.get("params") or {}

    def report(code, text, path, hint):
        problems.append(problem(code, f"{name}: {text}", f"{at}/{path}", block=name, hint=hint))

    profile = profiles.get(str(params.get("profile", "") or ""))
    if profile is None:
        report("E_MOTION_PROFILE_UNKNOWN", f"unknown motion profile '{params.get('profile', '')}'",
               "profile", "declare it under \"motion_profiles\"; declared: "
                          f"{', '.join(sorted(profiles)) or '(none)'}")
    segments = params.get("segments")
    if not isinstance(segments, list) or not segments:
        report("E_MOTION_SEGMENT", "the cycle has no segments", "segments",
               "add at least one segment")
        segments = []
    for index, segment in enumerate(segments):
        validate_segment(segment, index, profile, report)
    validate_hrc(params, profile, nodes, report)


def validate_segment(segment, index: int, profile: dict | None, report) -> None:
    where = f"segments/{index}"
    kind = segment.get("kind") if isinstance(segment, dict) else segment
    if kind not in SEGMENT_KINDS:
        report("E_MOTION_SEGMENT", f"segment {index}: unknown kind '{kind}'", f"{where}/kind",
               f"one of: {', '.join(SEGMENT_KINDS)}")
        return
    allowed = {"kind", "distance_m", "payload_kg"} if kind == "move" else {"kind", "time"}
    for field in sorted(set(segment) - allowed):
        report("E_MOTION_SEGMENT", f"segment {index} ({kind}): unknown field '{field}'",
               f"{where}/{field}", f"a {kind} takes: {', '.join(sorted(allowed - {'kind'}))}")
    if kind == "move":
        distance = _number(segment.get("distance_m"))
        if distance is None or distance < 0:
            report("E_MOTION_SEGMENT", f"segment {index} (move): distance_m must be a number >= 0",
                   f"{where}/distance_m", "the distance in metres")
        mass = _number(segment.get("payload_kg", 0))
        payload_max = _number((profile or {}).get("payload_max"))
        if mass is None or mass < 0:
            report("E_MOTION_SEGMENT", f"segment {index} (move): payload_kg must be a number >= 0",
                   f"{where}/payload_kg", "the carried mass in kg")
        elif payload_max is not None and mass > payload_max:
            report("E_MOTION_PAYLOAD", f"segment {index} (move): payload {mass:g} kg is above the "
                   f"profile's payload_max {payload_max:g} kg", f"{where}/payload_kg",
                   "lighten the load or pick a profile rated for it")
        return
    time = segment.get("time")
    literal = _number(time)
    if isinstance(time, str):
        try:
            literal = float(time)
        except ValueError:
            literal = None          # an expression: compiled by core/schema/layout.py
    if time is None or (isinstance(time, str) and not time.strip()):
        report("E_MOTION_SEGMENT", f"segment {index} ({kind}): no time", f"{where}/time",
               "a number or an expression, e.g. \"TRIA(0.8,1.0,1.4)\"")
    elif not isinstance(time, str) and literal is None:
        report("E_MOTION_SEGMENT", f"segment {index} ({kind}): time must be a number or an "
               "expression", f"{where}/time", "e.g. 1.5 or \"NORM(12,1)\"")
    elif literal is not None and literal < 0:
        report("E_MOTION_SEGMENT", f"segment {index} ({kind}): time must be >= 0",
               f"{where}/time", "a duration cannot be negative")


def validate_hrc(params: dict, profile: dict | None, nodes: set, report) -> None:
    mode = params.get("hrc", "none")
    if mode not in HRC_MODES:
        report("E_MOTION_ZONE", f"unknown hrc mode '{mode}'", "hrc",
               f"one of: {', '.join(HRC_MODES)}")
        return
    if mode == "none":
        return
    if profile is not None and not _positive(profile.get("collaborative_speed")):
        report("E_MOTION_PROFILE", f"hrc '{mode}' needs the profile's collaborative_speed",
               "profile", "set collaborative_speed (m/s) on the motion profile")
    if mode != "separation":
        return
    zone = params.get("zone") if isinstance(params.get("zone"), dict) else {}
    node = str(zone.get("node", "") or "").strip()
    if node not in nodes:
        report("E_MOTION_ZONE", f"zone node '{node}' is not a network node", "zone/node",
               f"declared: {', '.join(sorted(nodes)) or '(none)'}")
    if not _positive(zone.get("radius_m")):
        report("E_MOTION_ZONE", "zone radius_m must be a number > 0", "zone/radius_m",
               "the separation distance in metres")
