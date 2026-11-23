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

import functools
import json
import math
from types import SimpleNamespace

from nagasim.core.conveyor_types import TYPES
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.problems import problem
from nagasim.core.quantities import scaled
from nagasim.core.timeunits import base_factor, conversion_factor, factor
from nagasim.core.utils import cache_key, is_finite_number

__all__ = ["SEGMENT_KINDS", "ROBOT_KINDS", "TIMED_KINDS", "PROFILE_KINDS", "HRC_MODES", "derate",
           "move_speed", "move_time", "move_seconds", "collaborative_now", "segment_seconds",
           "nominal_cycle_seconds", "build_motion_profiles", "validate_motion",
           "ZONE_MEASURES", "resolve_zone", "worker_point", "build_zones", "validate_zones",
           "zone_names", "robot_plan", "block_robot_plan", "robot_plans", "belt_picks"]

SEGMENT_KINDS = ("move", "pick", "place", "tool_change", "dwell", "process")
TIMED_KINDS = SEGMENT_KINDS[1:]            # every kind but "move" carries a `time`
# D1343: robot moves to a named target, timed from the robot's axis limits
# (core/robotics/moves.py); new kinds rather than fields on "move", so every site keyed on
# "move" keeps its meaning. Accepted by a MOTION block only (validate_segment's robot_kinds).
ROBOT_KINDS = ("ptp", "lin")
PROFILE_KINDS = ("robot", "human", "mechanism")      # descriptive only: one set of rules (D157)
HRC_MODES = ("none", "collaborative", "separation")  # D158
# D834: how far a worker is from a zone node. "path" walks the network (D158's
# rule); "straight" is the line a safety laser scanner sees.
ZONE_MEASURES = ("path", "straight")
_ZONE_FIELDS = frozenset({"name", "description", "node", "radius_m", "measure"})


def _finite_float(value) -> float | None:
    """*value* as a finite float, or None (a bool is not a number here)."""
    return float(value) if is_finite_number(value) else None


def derate(profile: dict, payload_kg=0.0) -> float:
    """Speed fraction while carrying *payload_kg*: linear from 1.0 at 0 kg to
    `payload_derate` at `payload_max`, held there above it (validation refuses a
    payload above the maximum, E_MOTION_PAYLOAD). 1.0 when the profile has no
    `payload_max`: nothing says how a load slows it."""
    payload = _finite_float(payload_kg) or 0.0
    payload_max = _finite_float(profile.get("payload_max"))
    if payload <= 0 or payload_max is None or payload_max <= 0:
        return 1.0
    floor = _finite_float(profile.get("payload_derate"))
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
    zone = resolve_zone(ctx, zone)
    node, radius = str(zone.get("node", "")), float(zone.get("radius_m", 0.0))
    units = [unit for fleet in ctx.fleets.values() if fleet.kind == "worker"
             for unit in fleet.movers]
    if (zone.get("measure") or "path") == "straight":
        zx, zy = ctx.network.point(node)
        return any(math.hypot(x - zx, y - zy) <= radius
                   for x, y in (worker_point(ctx.network, unit) for unit in units))
    return any(unit.distance_to(ctx.network, node) <= radius for unit in units)


def resolve_zone(ctx, zone: dict) -> dict:
    """The zone a block's `zone` dict means (D830): the `zones` record its
    non-empty `name` names, whose node and radius then replace the inline
    ones; the inline dict itself when it names none (D158 as it was)."""
    name = str(zone.get("name") or "")
    if not name:
        return zone
    record = ctx.zones.get(name)
    if record is None:
        raise RuntimeError(f"unknown zone '{name}' (E_MOTION_ZONE_UNKNOWN).")
    return record


def worker_point(network, unit) -> tuple[float, float]:
    """A worker's floor point for a "straight" zone (D834), in layout
    coordinates: mid-segment on an interruptible drive (the position
    `Mover.distance_to` reads), a gantry's own `pos`, else its node -- so a
    non-interruptible walk counts at the node it left, as under "path"."""
    if unit.fleet.motion == "gantry":
        return unit.pos if unit.pos is not None else network.point(unit.node)
    at = unit.progress()
    if at is None:
        return network.point(unit.node)
    a, b, seg, along, _before = at
    (ax, ay), (bx, by) = network.point(a), network.point(b)
    share = along / seg.length if seg.length > 0 else 0.0
    return ax + (bx - ax) * share, ay + (by - ay) * share


def build_zones(layout: dict) -> dict[str, dict]:
    """{name: zone record} for the run (D830); MOTION and tending read it from ctx."""
    return {str(z["name"]): dict(z) for z in layout.get("zones") or []
            if isinstance(z, dict) and z.get("name")}


def validate_zones(layout: dict, problems: list) -> None:
    """The `zones` section's hook (core/sections.py): E_ZONE per bad record."""
    nodes = _node_names(layout)
    seen: set[str] = set()
    for index, record in enumerate(layout.get("zones") or []):
        at = f"/zones/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""

        def bad(field, text, hint, name=name, at=at):
            problems.append(problem("E_ZONE", f"zone '{name}': {text}", f"{at}/{field}",
                                    hint=hint,
                                    fields={"case": "said"}))

        if not name or name in seen:
            problems.append(problem("E_ZONE", f"zone {index}: empty or duplicate name '{name}'",
                                    f"{at}/name", hint="give every zone its own name",
                                    fields={"case": "name", "index": index, "name": name}))
            if not isinstance(record, dict):
                continue
        seen.add(name)
        for field in sorted(set(record) - _ZONE_FIELDS):
            bad(field, f"unknown field '{field}'", f"one of: {', '.join(sorted(_ZONE_FIELDS))}")
        node = str(record.get("node", "") or "").strip()
        if node not in nodes:
            bad("node", f"node '{node}' is not a network node",
                f"declared: {', '.join(sorted(nodes)) or '(none)'}")
        if not _positive(record.get("radius_m")):
            bad("radius_m", "radius_m must be a number > 0", "the distance in metres")
        if (record.get("measure") or "path") not in ZONE_MEASURES:
            bad("measure", f"unknown measure '{record.get('measure')}'",
                f"one of: {', '.join(ZONE_MEASURES)}")


def _node_names(layout: dict) -> set:
    return {str(n.get("name", "")).strip()
            for n in (layout.get("network") or {}).get("nodes") or [] if isinstance(n, dict)}


def zone_names(layout: dict) -> set:
    """The declared zone names, for a block's `zone.name` check."""
    return {str(z.get("name", "")).strip() for z in layout.get("zones") or []
            if isinstance(z, dict)} - {""}


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
                          model_unit: str = "minutes", plan=None) -> float | None:
    """The cycle's nominal time in SECONDS, for the editor: each timed
    segment's mean (its expression evaluated in deterministic mode, D119) in
    *time_unit* ("base" = *model_unit*), every move at the profile's speed after
    payload derating and with no HRC slowdown, every robot move (ptp, lin) as
    *plan* (the block's `block_robot_plan`, the run's own, #1982) times it. None
    when a segment cannot be evaluated outside a run (it reads an entity
    attribute, a variable, a table), a move has no usable profile, a robot move
    no plan, or a unit is unknown."""
    scope = ExpressionScope(ctx=SimpleNamespace(deterministic=True))
    total = 0.0
    try:
        to_seconds = factor(model_unit if time_unit == "base" else time_unit, "seconds")
        if any(isinstance(s, dict) and s.get("kind") in ROBOT_KINDS for s in segments):
            if plan is None or plan.errors:
                return None
            total += sum(m.seconds for m in plan.moves.values())
        for segment in segments:
            if segment.get("kind") in ROBOT_KINDS:
                continue
            if segment.get("kind") == "move":
                if not profile or (_finite_float(profile.get("speed")) or 0) <= 0 \
                        or (_finite_float(profile.get("accel")) or 0) <= 0:
                    return None
                total += move_seconds(profile, segment)
                continue
            value = float(compile_expression(str(segment.get("time", ""))).evaluate(scope))
            total += max(value, 0.0) * to_seconds
    except (ExpressionError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return total


def nominal_segment_seconds(segments, profile: dict | None, time_unit: str = "seconds",
                            model_unit: str = "minutes", plan=None) -> list[float] | None:
    """`nominal_cycle_seconds` per segment, in segment order, in SECONDS; None where that
    function gives None. Its own loop, as the cycle's total sums in another order and must
    keep its float value."""
    scope = ExpressionScope(ctx=SimpleNamespace(deterministic=True))
    out = []
    try:
        to_seconds = factor(model_unit if time_unit == "base" else time_unit, "seconds")
        if any(isinstance(s, dict) and s.get("kind") in ROBOT_KINDS for s in segments) \
                and (plan is None or plan.errors):
            return None
        for index, segment in enumerate(segments):
            if segment.get("kind") in ROBOT_KINDS:
                out.append(plan.moves[index].seconds)
            elif segment.get("kind") == "move":
                if not profile or (_finite_float(profile.get("speed")) or 0) <= 0 \
                        or (_finite_float(profile.get("accel")) or 0) <= 0:
                    return None
                out.append(move_seconds(profile, segment))
            else:
                value = float(compile_expression(str(segment.get("time", ""))).evaluate(scope))
                out.append(max(value, 0.0) * to_seconds)
    except (ExpressionError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return out


@functools.lru_cache(maxsize=256)
def _cached_plan(record: str, targets: str, segments: str, unit: float, base: str, places: str,
                 placed: bool, belts: str = "{}"):
    from nagasim.core.robotics.moves import BeltLine, BeltPick, Place, plan_cycle
    from nagasim.core.robotics.records import robot_from_record
    import numpy as np
    try:
        robot = robot_from_record(json.loads(record))
    except (ValueError, TypeError, KeyError):
        return None             # E_ROBOT says why
    pose = json.loads(base)
    picks = {k: BeltPick(BeltLine(np.array(o), np.array(d), v, s0), limit)
             for k, (o, d, v, s0, limit) in json.loads(belts).items()}
    return plan_cycle(robot, json.loads(targets), json.loads(segments),
                      unit, base=None if pose is None else np.array(pose),
                      places={k: Place(*v) for k, v in json.loads(places).items()}, placed=placed,
                      belts=picks)


def belts_key(belts: dict | None) -> str:
    return cache_key({k: [[float(v) for v in p.line.origin], [float(v) for v in p.line.direction],
                           p.line.speed, p.line.s0, p.limit] for k, p in (belts or {}).items()})


def belt_picks(layout: dict, targets: dict, places: dict, scope: str = "") -> dict:
    """{target name: BeltPick} of every belt target ({belt, divert, z?, yaw_deg?}) of
    *targets* whose belt is a CONVEYOR of *layout* with a declared length and a place
    in *places*, and whose divert is flagged `intercept`: the part's line from the
    divert's `at` (the call), in metres and metres per second, and the next point after
    the call (a stop, divert, merge or eye, else the end). *scope* is a flattened
    instance's prefix, tried first as for `on`. A target left out is refused by the
    plan (E_ROBOT_INTERCEPT case `belt`)."""
    from nagasim.core.robotics.moves import BeltPick, belt_line
    from nagasim.core.timeunits import units_of
    wanted = {k: t for k, t in (targets or {}).items() if isinstance(t, dict) and "belt" in t}
    if not wanted:
        return {}
    blocks = {str(b.get("name")): b for b in layout.get("blocks") or [] if isinstance(b, dict)}
    units = units_of(layout.get("simulation"))
    out = {}
    for key, target in wanted.items():
        name = str(target.get("belt"))
        name = scope + name if scope and scope + name in blocks else name
        block, place = blocks.get(name), (places or {}).get(name)
        params = (block or {}).get("params") or {}
        divert, z = target.get("divert"), target.get("z", 0.0)
        diverts = params.get("diverts") if isinstance(params.get("diverts"), list) else []
        if block is None or str(block.get("type", "")).upper() != "CONVEYOR" or place is None \
                or not place.length or not isinstance(divert, int) or isinstance(divert, bool) \
                or not 0 <= divert < len(diverts) or not isinstance(diverts[divert], dict) \
                or diverts[divert].get("intercept") is not True or _finite_float(z) is None:
            continue
        try:
            to_m = conversion_factor("length", units.length, "meters")
            at_m = base_factor("length", params.get("length_unit"), units) * to_m
            speed = (_finite_float(params.get("speed", 1.0)) or 1.0) * base_factor(
                "speed", params.get("speed_unit"), units) * to_m / factor(units.time, "seconds")
            spacing = (_finite_float(params.get("spacing", 1.0)) or 1.0) * base_factor(
                "length", params.get("spacing_unit"), units) * to_m
            part = (_finite_float(params.get("part_length")) or 0.0) * base_factor(
                "length", params.get("part_length_unit"), units) * to_m
        except (KeyError, ValueError):
            continue                # an unknown unit: its own check says why
        call = (_finite_float(diverts[divert].get("at")) or 0.0) * at_m
        back = part if part > 0 else spacing
        kind = str(params.get("type") or "").strip().lower()
        if kind in TYPES:
            # D1355: a typed belt lays a back eye's point per item length; the first one is
            # the shortest item's (sizes in the model's length unit, else the unsized one's)
            sizes = [length * to_m for row in layout.get("entities") or [] if isinstance(row, dict)
                     for length in [_finite_float((row.get("size") or {}).get("length"))
                                    if isinstance(row.get("size"), dict) else None]
                     if length and length > 0]
            back = min([*sizes, part if part > 0 else TYPES[kind].zone_length])
        # The points a part passes, in metres; an eye's is where the front is as it fires.
        points = [(_finite_float(row.get("at")) or 0.0) * at_m for field in ("stops", "diverts", "merges")
                  for row in params.get(field) or [] if isinstance(row, dict)]
        points += [(_finite_float(row.get("at")) or 0.0) * at_m
                   + (back if row.get("edge") == "back" else 0.0)
                   for row in params.get("eyes") or [] if isinstance(row, dict)]
        limit = min([p for p in points if p > call + 1e-9], default=float(place.length))
        out[key] = BeltPick(belt_line(place, speed, call, z=float(z)),
                            min(limit, float(place.length)))
    return out


def robot_plan(layout_robots: dict, params: dict, time_unit_seconds: float, *, base=None,
               places: dict | None = None, placed: bool = True, belts: dict | None = None):
    """The CyclePlan of a MOTION block's robot moves, or None when its `robot` is
    not a record of *layout_robots* ({name: record}) that builds. Cached by
    content: the robot record, the targets, the segments, the unit, the base pose,
    the places of the blocks the targets name and the belt picks (*belts*, from
    `belt_picks`), so an edited limit, target, floor-plan place or belt is planned
    afresh."""
    record = layout_robots.get(str(params.get("robot") or ""))
    if record is None:
        return None
    targets = params.get("targets") if isinstance(params.get("targets"), dict) else {}
    segments = [s for s in params.get("segments") or [] if isinstance(s, dict)]
    named = {str(t.get("on")) for t in targets.values() if isinstance(t, dict) and "on" in t}
    near = {k: [p.x, p.y, p.rotation, p.length] for k, p in (places or {}).items() if k in named}
    pose = None if base is None else [[float(v) for v in row] for row in base]
    return _cached_plan(cache_key(record), cache_key(targets), cache_key(segments),
                        float(time_unit_seconds), json.dumps(pose), cache_key(near), bool(placed),
                        belts_key(belts))


def robot_records(layout: dict) -> dict:
    """{name: record} of the robots section, the first of a repeated name."""
    out: dict = {}
    for record in layout.get("robots") or []:
        if isinstance(record, dict) and str(record.get("name", "")).strip():
            out.setdefault(str(record["name"]).strip(), record)
    return out


def block_robot_plan(layout: dict, block: dict, records: dict | None = None,
                     places: dict | None = None, base=None, belts: dict | None = None):
    """The plan of one MOTION block's robot moves in *layout* (flattened), with its
    base pose (*base*, else the block's own place), the floor-plan places its targets
    read and its picks from moving belts (*belts*, else `belt_picks` of its targets);
    None without a robot."""
    from nagasim.core.robotics.moves import places_of, robot_base
    params = block.get("params") or {}
    records = robot_records(layout) if records is None else records
    if str(params.get("robot") or "") not in records:
        return None
    unit = str(params.get("time_unit", "seconds") or "seconds")
    model_unit = str((layout.get("simulation") or {}).get("time_unit") or "minutes")
    try:
        seconds = factor(model_unit if unit == "base" else unit, "seconds")
    except (KeyError, ValueError):
        return None             # E_TIME_UNIT says why
    name = str(block.get("name", ""))
    base = robot_base(layout, name) if base is None else base
    places = places_of(layout) if places is None else places
    scope = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    if scope:
        # #1981: inside an asset, `on` names a block of the same instance first, then a top one.
        targets = params.get("targets") if isinstance(params.get("targets"), dict) else {}
        params = {**params, "targets": {
            key: {**t, "on": scope + str(t["on"])} if isinstance(t, dict) and "on" in t
            and scope + str(t["on"]) in places else t for key, t in targets.items()}}
    if belts is None:
        belts = belt_picks(layout, params.get("targets") if isinstance(
            params.get("targets"), dict) else {}, places, scope)
    return robot_plan(records, params, seconds, base=base, places=places, placed=base is not None,
                      belts=belts)


def robot_plans(layout: dict) -> dict:
    """{MOTION block name: CyclePlan} for the run; MOTION reads its robot times from ctx."""
    records = robot_records(layout)
    if not records:
        return {}
    from nagasim.core.robotics.moves import places_of
    places = places_of(layout)
    out = {}
    for block in layout.get("blocks") or []:
        if str(block.get("type", "")).upper() == "MOTION":
            plan = block_robot_plan(layout, block, records, places)
            if plan is not None:
                out[str(block.get("name"))] = plan
    return out


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
        if _finite_float(out.get(name)) is not None:
            out[name] = scaled(out[name], conversion_factor("speed", speed_unit, "m/s"))
    payload_unit = out.get("payload_unit") or "kg"
    payload_unit = "kg" if payload_unit == "base" else payload_unit
    if _finite_float(out.get("payload_max")) is not None:
        out["payload_max"] = scaled(out["payload_max"],
                                    conversion_factor("mass", payload_unit, "kg"))
    return out


def _positive(value) -> bool:
    number = _finite_float(value)
    return number is not None and number > 0


def validate_motion(layout: dict, problems: list) -> None:
    """The `motion_profiles` section's hook (core/sections.py), which also
    checks every MOTION block's cycle, HRC mode and zone: a block check needs
    only the profile names and the network node names, both read here from
    the layout (validated flattened, so a `$arg` is already a value)."""
    profiles = _validate_profiles(layout, problems)
    nodes, zones = _node_names(layout), zone_names(layout)
    robots, places = robot_records(layout), None
    for index, block in enumerate(layout.get("blocks") or []):
        if str(block.get("type", "")).upper() == "MOTION":
            _validate_block(block, f"/blocks/{index}/params", profiles, nodes, zones, problems)
            if robots or any(isinstance(s, dict) and s.get("kind") in ROBOT_KINDS
                             for s in (block.get("params") or {}).get("segments") or []):
                if places is None:
                    from nagasim.core.robotics.moves import places_of
                    places = places_of(layout)
                _validate_robot(layout, block, f"/blocks/{index}/params", robots, places, problems)


def _validate_profiles(layout: dict, problems: list) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    for index, record in enumerate(layout.get("motion_profiles") or []):
        at = f"/motion_profiles/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""
        if not name or name in profiles:
            problems.append(problem("E_MOTION_PROFILE",
                                    f"motion profile {index}: empty or duplicate name '{name}'",
                                    f"{at}/name", hint="give every motion profile its own name",
                                    fields={"case": "name", "index": index, "name": name}))
            continue
        profiles[name] = record

        def bad(field, text, hint, name=name, at=at):
            problems.append(problem("E_MOTION_PROFILE", f"motion profile '{name}': {text}",
                                    f"{at}/{field}", hint=hint,
                                    fields={"case": "said"}))

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


def _validate_block(block: dict, at: str, profiles: dict, nodes: set, zones: set,
                    problems: list) -> None:
    name = block.get("name", "<unnamed>")
    params = block.get("params") or {}

    def report(code, text, path, hint, fields=None):
        problems.append(problem(code, f"{name}: {text}", f"{at}/{path}", block=name, hint=hint,
                                fields=fields))

    profile = profiles.get(str(params.get("profile", "") or ""))
    segments = params.get("segments")
    # With a robot, the profile times only `move` segments and the hrc slowdown (D1343).
    needs_profile = not params.get("robot") or params.get("hrc", "none") != "none" or any(
        isinstance(s, dict) and s.get("kind") == "move" for s in segments or [])
    if profile is None and needs_profile:
        report("E_MOTION_PROFILE_UNKNOWN", f"unknown motion profile '{params.get('profile', '')}'",
               "profile", "declare it under \"motion_profiles\"; declared: "
                          f"{', '.join(sorted(profiles)) or '(none)'}",
               fields={"profile": params.get("profile", "")})
    if not isinstance(segments, list) or not segments:
        report("E_MOTION_SEGMENT", "the cycle has no segments", "segments",
               "add at least one segment")
        segments = []
    for index, segment in enumerate(segments):
        validate_segment(segment, index, profile, report, robot_kinds=True)
    validate_hrc(params, profile, nodes, report, zones,
                 robot_moves=any(isinstance(s, dict) and s.get("kind") in ROBOT_KINDS for s in segments))


def _validate_robot(layout: dict, block: dict, at: str, robots: dict, places: dict,
                    problems: list) -> None:
    """A MOTION block's robot and its robot moves, planned before the run: one problem per
    target or move the plan refuses (D1343)."""
    name = block.get("name", "<unnamed>")
    params = block.get("params") or {}
    segments = [s for s in params.get("segments") or [] if isinstance(s, dict)]
    robot = str(params.get("robot", "") or "")

    def say(code, text, path, hint, fields=None):
        problems.append(problem(code, f"{name}: {text}", f"{at}/{path}", block=name, hint=hint,
                                fields=fields))

    if not robot_named(say, robot, robots, segments):
        return
    plan = block_robot_plan(layout, block, robots, places)
    plan_problems(say, plan.errors if plan is not None else ())


def robot_named(say, robot: str, robots: dict, segments: list) -> bool:
    """True when *robot* is a record of *robots*; else says E_ROBOT_UNKNOWN (when it is
    set, or robot moves need one) through *say*(code, text, path, hint, fields)."""
    if not robot:
        if any(s.get("kind") in ROBOT_KINDS for s in segments):
            say("E_ROBOT_UNKNOWN", f"robot '{robot}' is not set; ptp and lin segments need one",
                "robot", "set `robot` to a record of \"robots\"", fields={"robot": robot})
        return False
    if robot not in robots:
        say("E_ROBOT_UNKNOWN", f"unknown robot '{robot}'", "robot",
            "declare it under \"robots\"; declared: " f"{', '.join(sorted(robots)) or '(none)'}",
            fields={"robot": robot})
        return False
    return True


def plan_problems(say, errors, prefix: str = "") -> None:
    """One problem per error a cycle plan refuses, its text after *prefix* ("slot 4: ")."""
    # One literal code per call: the plain-line check reads the producers (D1334).
    for err in errors:
        where = f"segments/{err.segment}"
        if err.code == "E_ROBOT_TARGET":
            say("E_ROBOT_TARGET", f"{prefix}segment {err.segment}: {err}", f"{where}/to",
                "a target of the block's `targets`: {joints}, {xyz, yaw_deg?, rpy_deg?} or "
                "{on, at?, offset?, yaw_deg?}",
                fields={"case": err.fields["case"], "target": err.fields["target"]})
        elif err.code == "E_ROBOT_REACH":
            say("E_ROBOT_REACH", f"{prefix}segment {err.segment}: {err}", f"{where}/to",
                "move the target or the robot, or pick a robot with the reach and axis ranges",
                fields={"case": err.fields["case"], "target": err.fields["target"]})
        elif err.code == "E_ROBOT_SINGULAR":
            say("E_ROBOT_SINGULAR", f"{prefix}segment {err.segment}: {err}", f"{where}/to",
                "use ptp for this move, or move the target off the singular pose",
                fields={"target": err.fields["target"], "at": err.fields["at"]})
        elif err.code == "E_ROBOT_LIN":
            say("E_ROBOT_LIN", f"{prefix}segment {err.segment}: {err}", f"{where}/to",
                "use ptp for this move, or a target the line reaches in the same arm configuration",
                fields={"case": err.fields["case"], "target": err.fields["target"],
                        "at": err.fields["at"]})
        elif err.code == "E_ROBOT_MOVE_TIME":
            say("E_ROBOT_MOVE_TIME", f"{prefix}segment {err.segment}: {err}", f"{where}/time",
                "give the move at least the time it needs, or drop `time` to move as fast as "
                "the axes allow",
                fields={"target": err.fields["target"], "needed": err.fields["needed"],
                        "given": err.fields["given"]})
        elif err.code == "E_ROBOT_INTERCEPT":
            say("E_ROBOT_INTERCEPT", f"{prefix}segment {err.segment}: {err}", f"{where}/to",
                "pick from the belt in segment 0, a ptp to {belt, divert, z?, yaw_deg?} naming a "
                "divert with `intercept`, met before the next point on the belt; end the cycle "
                "at a fixed target",
                fields={"case": err.fields["case"], "target": err.fields["target"],
                        "belt": err.fields["belt"], "meet": err.fields["meet"],
                        "limit": err.fields["limit"]})
        elif err.code == "E_MOTION_PAYLOAD":
            say("E_MOTION_PAYLOAD", f"{prefix}segment {err.segment}: {err}", f"{where}/payload_kg",
                "lighten the load or pick a robot rated for it")


def validate_segment(segment, index: int, profile: dict | None, report,
                     robot_kinds: bool = False) -> None:
    """One cycle segment; *robot_kinds*: ptp and lin are kinds too (a MOTION block's)."""
    where = f"segments/{index}"
    kind = segment.get("kind") if isinstance(segment, dict) else segment
    kinds = SEGMENT_KINDS + ROBOT_KINDS if robot_kinds else SEGMENT_KINDS
    if kind not in kinds:
        report("E_MOTION_SEGMENT", f"segment {index}: unknown kind '{kind}'", f"{where}/kind",
               f"one of: {', '.join(kinds)}")
        return
    if kind in ROBOT_KINDS:
        allowed = {"kind", "to", "time", "payload_kg"}
        for field in sorted(set(segment) - allowed):
            report("E_MOTION_SEGMENT", f"segment {index} ({kind}): unknown field '{field}'",
                   f"{where}/{field}", f"a {kind} takes: {', '.join(sorted(allowed - {'kind'}))}")
        # A literal number only: an expression would make "too short" undecidable before the run.
        if "time" in segment and not (_finite_float(segment["time"]) is not None and segment["time"] >= 0):
            report("E_MOTION_SEGMENT", f"segment {index} ({kind}): time must be a number >= 0",
                   f"{where}/time", "seconds in the block's time_unit; leave it out to move as "
                   "fast as the axes allow")
        mass = _finite_float(segment.get("payload_kg", 0))
        if mass is None or mass < 0:
            report("E_MOTION_SEGMENT", f"segment {index} ({kind}): payload_kg must be a number >= 0",
                   f"{where}/payload_kg", "the carried mass in kg")
        return
    allowed = {"kind", "distance_m", "payload_kg"} if kind == "move" else {"kind", "time"}
    for field in sorted(set(segment) - allowed):
        report("E_MOTION_SEGMENT", f"segment {index} ({kind}): unknown field '{field}'",
               f"{where}/{field}", f"a {kind} takes: {', '.join(sorted(allowed - {'kind'}))}")
    if kind == "move":
        distance = _finite_float(segment.get("distance_m"))
        if distance is None or distance < 0:
            report("E_MOTION_SEGMENT", f"segment {index} (move): distance_m must be a number >= 0",
                   f"{where}/distance_m", "the distance in metres")
        mass = _finite_float(segment.get("payload_kg", 0))
        payload_max = _finite_float((profile or {}).get("payload_max"))
        if mass is None or mass < 0:
            report("E_MOTION_SEGMENT", f"segment {index} (move): payload_kg must be a number >= 0",
                   f"{where}/payload_kg", "the carried mass in kg")
        elif payload_max is not None and mass > payload_max:
            report("E_MOTION_PAYLOAD", f"segment {index} (move): payload {mass:g} kg is above the "
                   f"profile's payload_max {payload_max:g} kg", f"{where}/payload_kg",
                   "lighten the load or pick a profile rated for it")
        return
    time = segment.get("time")
    literal = _finite_float(time)
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


def validate_hrc(params: dict, profile: dict | None, nodes: set, report,
                 zones: set = frozenset(), robot_moves: bool = False) -> None:
    mode = params.get("hrc", "none")
    if mode not in HRC_MODES:
        report("E_MOTION_ZONE", f"unknown hrc mode '{mode}'", "hrc",
               f"one of: {', '.join(HRC_MODES)}")
        return
    if mode == "none":
        return
    if robot_moves:
        # D1343: silently ignoring hrc on a robot move would mislead.
        report("E_MOTION_ZONE", f"hrc '{mode}' slows move segments only; a robot's ptp and lin "
               "moves cannot be slowed", "hrc", "set hrc to \"none\", or time the moves as move "
               "segments with a motion profile", fields={"case": "robot"})
    if profile is not None and not _positive(profile.get("collaborative_speed")):
        report("E_MOTION_PROFILE", f"hrc '{mode}' needs the profile's collaborative_speed",
               "profile", "set collaborative_speed (m/s) on the motion profile",
               fields={"case": "speed"})
    if mode != "separation":
        return
    zone = params.get("zone") if isinstance(params.get("zone"), dict) else {}
    name = str(zone.get("name", "") or "").strip()
    if name:
        # D830: a named zone's node and radius are the record's (E_ZONE checks
        # those); the inline fields are only the fallback and go unchecked.
        if name not in zones:
            report("E_MOTION_ZONE_UNKNOWN", f"unknown zone '{name}'", "zone/name",
                   f"declare it under \"zones\"; declared: {', '.join(sorted(zones)) or '(none)'}",
                   fields={"zone": name})
        return
    if (zone.get("measure") or "path") not in ZONE_MEASURES:
        report("E_MOTION_ZONE", f"unknown zone measure '{zone.get('measure')}'", "zone/measure",
               f"one of: {', '.join(ZONE_MEASURES)}")
    node = str(zone.get("node", "") or "").strip()
    if node not in nodes:
        report("E_MOTION_ZONE", f"zone node '{node}' is not a network node", "zone/node",
               f"declared: {', '.join(sorted(nodes)) or '(none)'}")
    if not _positive(zone.get("radius_m")):
        report("E_MOTION_ZONE", "zone radius_m must be a number > 0", "zone/radius_m",
               "the separation distance in metres")
