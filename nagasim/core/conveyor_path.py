"""Belt path geometry: straight and arc pieces laid end to end (conveyor system A).

A pure helper, no engine state. A belt's `path` is a list of pieces, each either
`{"straight": length}` or `{"arc": {"radius", "angle", "turn"}}`; the belt's length is the sum
of the piece lengths and a distance along it maps to a floor pose.

One frame (D1354): a Pose is in the floor-plan frame of the canvas, the 3D view and D74
(`nagasim/core/network.py`): x east, y SOUTH, metres, and `heading` is `layout_rotation`,
degrees CLOCKWISE on screen (0 east, 90 south). A "left" arc turns counter-clockwise on
screen (the heading falls), a "right" arc clockwise (it rises); a divert's "left" side is on
the left of a part travelling along the belt as seen on screen. Pose maths keeps the raw
heading (-90 stays -90); `network.normalise_rotation` folds it into [0, 360) only where it
is stored.
"""
from __future__ import annotations

import math
from typing import NamedTuple


class Pose(NamedTuple):
    x: float
    y: float
    heading: float      # degrees clockwise on screen, as `layout_rotation` (0 east, 90 south)


class Segment(NamedTuple):
    kind: str           # "straight" or "arc"
    length: float       # an arc's is radius * radians(angle)
    radius: float       # 0.0 for a straight
    angle: float        # degrees swept; 0.0 for a straight
    turn: str           # "left" or "right"; "" for a straight


def _number(value, what: str, index: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"path piece {index}: {what} must be a number")
    return float(value)


def parse_path(raw) -> list[Segment]:
    """The pieces of a `path` list, or a ValueError saying which piece is wrong and why."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("path must be a list of pieces")
    out: list[Segment] = []
    for index, piece in enumerate(raw):
        if not isinstance(piece, dict) or len(piece) != 1:
            raise ValueError(f"path piece {index}: use {{\"straight\": length}} or "
                             f"{{\"arc\": {{\"radius\", \"angle\", \"turn\"}}}}")
        (kind, body), = piece.items()
        if kind == "straight":
            length = _number(body, "straight length", index)
            if length <= 0:
                raise ValueError(f"path piece {index}: straight length must be above 0")
            out.append(Segment("straight", length, 0.0, 0.0, ""))
        elif kind == "arc":
            if not isinstance(body, dict) or set(body) - {"radius", "angle", "turn"}:
                raise ValueError(f"path piece {index}: an arc is {{\"radius\", \"angle\", \"turn\"}}")
            radius = _number(body.get("radius"), "arc radius", index)
            angle = _number(body.get("angle"), "arc angle", index)
            turn = body.get("turn")
            if radius <= 0:
                raise ValueError(f"path piece {index}: arc radius must be above 0")
            if not 0 < angle <= 360:
                raise ValueError(f"path piece {index}: arc angle must be above 0 and at most 360")
            if turn not in ("left", "right"):
                raise ValueError(f"path piece {index}: arc turn must be left or right")
            out.append(Segment("arc", radius * math.radians(angle), radius, angle, turn))
        else:
            raise ValueError(f"path piece {index}: unknown piece {kind!r} (straight or arc)")
    return out


def length_decimals(factor: float) -> int:
    """Decimals of a length or point written in a belt's unit (*factor* metres per unit): the
    finer of 0.01 of the unit and 0.01 m, so 2 in m and mm, 5 in km (the editor's and
    `insert_block_on_link`'s one rule, conveyor plan B)."""
    return max(2, math.ceil(math.log10(factor) + 2 - 1e-9))


def round_length(value: float, factor: float) -> float:
    """*value* in a belt's unit rounded by `length_decimals`."""
    return round(value, length_decimals(factor))


def path_length(segments) -> float:
    return float(sum(seg.length for seg in segments))


def _advance(pose: Pose, seg: Segment, run: float) -> Pose:
    """The pose after *run* along one piece (0 <= run <= seg.length)."""
    heading = math.radians(pose.heading)
    if seg.kind == "straight":
        return Pose(pose.x + run * math.cos(heading), pose.y + run * math.sin(heading), pose.heading)
    # y points south, so a rising heading turns clockwise on screen: a left turn lowers it.
    sign = -1.0 if seg.turn == "left" else 1.0
    swept = run / seg.radius                    # radians turned
    # The circle's centre sits a radius to the side of the heading; the pose moves round it.
    cx = pose.x - sign * seg.radius * math.sin(heading)
    cy = pose.y + sign * seg.radius * math.cos(heading)
    end = heading + sign * swept
    return Pose(cx + sign * seg.radius * math.sin(end), cy - sign * seg.radius * math.cos(end),
                pose.heading + sign * math.degrees(swept))


def pose_at(start: Pose, segments, s: float) -> Pose:
    """The pose *s* along the path from *start*, *s* clamped to [0, length]."""
    s = min(max(float(s), 0.0), path_length(segments))
    pose = Pose(*start)
    for seg in segments:
        if s <= seg.length:
            return _advance(pose, seg, s)
        pose = _advance(pose, seg, seg.length)
        s -= seg.length
    return pose


def end_pose(start: Pose, segments) -> Pose:
    return pose_at(start, segments, path_length(segments))


# -- angles ----------------------------------------------------------------

def same_angle(a: float, b: float, tol_deg: float = 1e-9) -> bool:
    """Whether two angles in degrees are the same direction, compared modulo 360 (so -90 and
    270, or 179 and -181, agree)."""
    gap = abs(float(a) - float(b)) % 360.0
    return min(gap, 360.0 - gap) <= tol_deg


# -- ports and snapping --------------------------------------------------------

class Port(NamedTuple):
    block: str
    name: str           # "in", "out" or "d<k>" (divert k's exit)
    pose: Pose          # a port faces the way a part leaves (in: along the belt; d<k>: off it)
    role: str           # "in" or "out"


def divert_angle(divert) -> float:
    """A divert's `angle` (D1388): degrees between the trunk's travel and its branch, inside
    (0, 180), 90 by default. Anything else reads as 90, so the floor checks of a model whose
    angle validation refuses still see a usable pose and never divide by sin 0."""
    angle = divert.get("angle", 90.0) if isinstance(divert, dict) else 90.0
    if (isinstance(angle, bool) or not isinstance(angle, (int, float))
            or not math.isfinite(angle) or not 0.0 < angle < 180.0):
        return 90.0
    return float(angle)


def ports(block_name: str, start: Pose, segments, diverts) -> list[Port]:
    """The ports of a belt laid out from *start* along *segments*: `in` at the start, `out` at
    the end, and `d<k>` at divert k's `at` (metres along the path, as the path is), facing off
    the belt to the divert's `side` at its `angle` from the travel ("left", the default:
    heading - angle, or "right": heading + angle, on screen; angle 90 is a square exit). A belt with no path is the caller's to describe as one straight piece of
    its length (`conveyor.belt_geometry` does it for a block dict)."""
    start = Pose(*start)
    found = [Port(block_name, "in", start, "in"),
             Port(block_name, "out", end_pose(start, segments), "out")]
    for k, divert in enumerate(diverts or []):
        at = divert.get("at", 0.0) if isinstance(divert, dict) else 0.0
        at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0
        pose = pose_at(start, segments, at)
        turn = divert_angle(divert)
        if not (isinstance(divert, dict) and divert.get("side") == "right"):
            turn = -turn
        found.append(Port(block_name, f"d{k}", Pose(pose.x, pose.y, pose.heading + turn), "out"))
    return found


def snap_candidates(ports_a, ports_b, tol_m: float = 0.05, tol_deg: float = 5.0):
    """Every pair (a, b) of the two port lists with a status. "ok": one is an out port and the
    other an in port, within *tol_m* metres, headings agreeing within *tol_deg* (so a part
    leaving the out port enters the in port straight). "same_role": both out or both in.
    Otherwise "misaligned" (too far apart, or facing another way)."""
    pairs = []
    for a in ports_a:
        for b in ports_b:
            if a.role == b.role:
                status = "same_role"
            elif (math.hypot(a.pose.x - b.pose.x, a.pose.y - b.pose.y) <= tol_m
                  and same_angle(a.pose.heading, b.pose.heading, tol_deg)):
                status = "ok"
            else:
                status = "misaligned"
            pairs.append((a, b, status))
    return pairs


def exit_slot(port, diverts: int) -> int | None:
    """The exit slot a `source_port` names on a belt with *diverts* diverts: "out" is 0 (the
    end), "d<k>" is k + 1 (divert k, k < diverts); None for anything else. One parser for the
    run (`exit_order`) and for validation, so they cannot disagree: k is ASCII digits with no
    leading zero ("d00", "d01" and non-ASCII digits are not exits)."""
    if port == "out":
        return 0
    if not isinstance(port, str) or port[:1] != "d":
        return None
    digits = port[1:]
    if not digits or any(c not in "0123456789" for c in digits) or (len(digits) > 1 and digits[0] == "0"):
        return None
    k = int(digits)
    return k + 1 if k < diverts else None


def exit_names(block: dict) -> list[str] | None:
    """The exits of a block dict whose links may name one by `source_port`, in exit order: a
    CONVEYOR's "out" then "d<k>" per divert, a CONVEYOR_TRANSFER's exits' ports ("out" when it
    declares none); None for any other block. The one list of a block's exit names that the
    run's link order and validation read (checkpoint I5)."""
    if not isinstance(block, dict):
        return None
    kind = str(block.get("type", "")).upper()
    params = block.get("params") if isinstance(block.get("params"), dict) else {}
    if kind == "CONVEYOR" and params.get("reversible") is True:
        return ["out", "in"]            # D1368: the start is the exit while it runs backward
    if kind == "CONVEYOR":
        diverts = params.get("diverts")
        count = len(diverts) if isinstance(diverts, list) else 0
        return ["out", *(f"d{k}" for k in range(count))]
    if kind == "CONVEYOR_TRANSFER":
        exits = params.get("exits")
        if not isinstance(exits, list) or not exits:
            return ["out"]
        return [str(e.get("port", "")) if isinstance(e, dict) else "" for e in exits]
    return None


def _slot_reader(exits):
    """`exit_slot` for a belt's divert count, else a lookup in a list of exit names."""
    if isinstance(exits, int):
        return lambda port: exit_slot(port, exits)
    names = list(exits)
    return lambda port: names.index(port) if isinstance(port, str) and port in names else None


def exit_order(links, diverts) -> list[int]:
    """Indices into *links* (a block's outgoing links, as dicts) in exit order. *diverts* is a
    belt's divert count (slot 0 the end, slot k + 1 divert k, named "out" and "d<k>", read by
    `exit_slot`) or a list of exit names (`exit_names`). Links without a port take the lowest
    free slots in listed order (today's rule). Anything that does not fill slots 0..n-1
    exactly once (an unknown or repeated port, a gap) keeps the listed order, and validation
    reports the unknown or repeated port (E_CONVEYOR_DIVERT)."""
    slot_of = _slot_reader(diverts)
    named: dict[int, int] = {}
    free: list[int] = []
    for index, link in enumerate(links):
        port = link.get("source_port") if isinstance(link, dict) else None
        if port in (None, ""):
            free.append(index)
            continue
        slot = slot_of(port)
        if slot is None or slot in named:
            return list(range(len(links)))      # unknown or repeated: validation reports it
        named[slot] = index
    if not named:
        return list(range(len(links)))
    order = []
    spare = iter(free)
    for slot in range(len(links)):
        index = named[slot] if slot in named else next(spare, None)
        if index is None:
            return list(range(len(links)))
        order.append(index)
    return order


def _belt_diverts(layout: dict) -> dict:
    """Block name -> what `exit_order` reads: a CONVEYOR with diverts its divert count, a
    CONVEYOR_TRANSFER its exit names."""
    found = {}
    for block in layout.get("blocks", []):
        params = block.get("params") if isinstance(block, dict) else None
        if not isinstance(params, dict):
            continue
        kind = str(block.get("type", "")).upper()
        diverts = params.get("diverts")
        if kind == "CONVEYOR" and params.get("reversible") is True:
            found[block.get("name")] = exit_names(block)
        elif kind == "CONVEYOR" and isinstance(diverts, list) and diverts:
            found[block.get("name")] = len(diverts)
        elif kind == "CONVEYOR_TRANSFER":
            found[block.get("name")] = exit_names(block)
    return found


def _links_by_belt(links, belts) -> dict[str, list[int]]:
    """Belt name -> indices of its outgoing links, for the belts in *belts*, in one pass."""
    grouped: dict[str, list[int]] = {name: [] for name in belts}
    for index, link in enumerate(links):
        if isinstance(link, dict) and link.get("source") in grouped:
            grouped[link["source"]].append(index)
    return grouped


def _reverse_targets(layout: dict) -> dict:
    """Reversible belt name -> its `reverse_to` when that names a link target, not a port."""
    found = {}
    for block in layout.get("blocks", []):
        params = block.get("params") if isinstance(block, dict) else None
        if isinstance(params, dict) and params.get("reversible") is True \
                and str(block.get("type", "")).upper() == "CONVEYOR":
            target = str(params.get("reverse_to", "") or "").strip()
            if target and target not in exit_names(block):
                found[block.get("name")] = target
    return found


def _as_ported(link, reverse_to: dict):
    """D1368: a reversible belt's link to its `reverse_to` target, with no port, as the link
    naming its "in" exit, so the end is link 0 and the start link 1 whatever the drawn order."""
    if isinstance(link, dict) and link.get("source_port") in (None, "") \
            and reverse_to.get(link.get("source")) == link.get("target"):
        return {**link, "source_port": "in"}
    return link


def belt_exit_links(layout: dict) -> list:
    """The layout's links with each divert-belt's (or reversible belt's) outgoing links in
    exit order (see `exit_order`). A layout where no link names a port and no reversible belt
    names its reverse target comes back as it is."""
    links = layout.get("links", [])
    reverse_to = _reverse_targets(layout)
    if not reverse_to and not any(isinstance(link, dict) and link.get("source_port")
                                  for link in links):
        return links
    belts = _belt_diverts(layout)
    out = list(links)
    for name, at in _links_by_belt(links, belts).items():
        ported = [_as_ported(links[i], reverse_to) for i in at]
        for place, index in zip(at, exit_order(ported, belts[name])):
            out[place] = links[at[index]]
    return out


def reverse_chains(layout: dict) -> dict[str, str]:
    """D1392: reversible CONVEYOR name -> the reversible CONVEYOR its end links to directly
    (no block, delay or area link between), the next piece of the chain that reverses with it
    as one. Read from the belt's end link in exit order; a lone link that is the start exit
    (`reverse_to` names its target, or its port is "in") joins nothing, and a piece fed so by
    two belts keeps the first. The run's build and validation read this one rule."""
    rev = {}
    for block in layout.get("blocks", []):
        params = block.get("params") if isinstance(block, dict) else None
        if isinstance(params, dict) and params.get("reversible") is True \
                and str(block.get("type", "")).upper() == "CONVEYOR":
            rev[block.get("name")] = str(params.get("reverse_to", "") or "").strip()
    if len(rev) < 2:
        return {}
    ends: dict = {}
    for link in belt_exit_links(layout):
        if isinstance(link, dict) and link.get("source") in rev:
            ends.setdefault(link["source"], link)
    found, fed = {}, set()
    for name, link in ends.items():
        target = link.get("target")
        if target in rev and target != name and target not in fed and not link.get("delay") \
                and link.get("source_port") != "in" and target != rev[name]:
            found[name] = target
            fed.add(target)
    return found


def belt_exit_targets(layout: dict, by_name: dict) -> dict[str, list]:
    """Block name -> target names in exit order, for the divert-belts whose links name a port;
    empty when none does."""
    links = layout.get("links", [])
    if not _reverse_targets(layout) and not any(isinstance(link, dict) and
                                                link.get("source_port") for link in links):
        return {}
    ordered = belt_exit_links(layout)
    belts = {name: n for name, n in _belt_diverts(layout).items() if name in by_name}
    return {name: [ordered[i]["target"] for i in at]
            for name, at in _links_by_belt(ordered, belts).items()}


def transfer_ports(name: str, position, rotation, params: dict) -> list[Port]:
    """A CONVEYOR_TRANSFER's ports: "in" and one out port per exit (`exit_names`). It has no
    footprint, so each pose is the module's position; "in" heads along the module's rotation,
    an exit that heading turned by the exit's angle. The one rule of `get_ports`, of `snap_to`
    on an exit and of the editor."""
    x, y = position or (0.0, 0.0)
    heading = float(rotation or 0.0)
    angles = {str(e.get("port")): float(e.get("angle", 0.0))
              for e in params.get("exits") or [] if isinstance(e, dict)}
    names = exit_names({"type": "CONVEYOR_TRANSFER", "params": params})
    return [Port(name, "in", Pose(float(x), float(y), heading % 360.0), "in")] + [
        Port(name, port, Pose(float(x), float(y), (heading + angles.get(port, 0.0)) % 360.0),
             "out")
        for port in names]


def path_polyline(start: Pose, segments, step_deg: float = 10.0) -> list[tuple[float, float]]:
    """The path's points, entry to end: a straight's ends, an arc's chords of at most *step_deg*."""
    points, pose = [(start.x, start.y)], Pose(*start)
    for seg in segments:
        steps = 1 if seg.kind == "straight" else max(1, math.ceil(seg.angle / step_deg))
        for k in range(1, steps + 1):
            p = pose_at(pose, [seg], seg.length * k / steps)
            points.append((p.x, p.y))
        pose = pose_at(pose, [seg], seg.length)
    return points
