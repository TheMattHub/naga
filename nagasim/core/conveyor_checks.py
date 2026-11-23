"""Run checks of conveyor lines (conveyor system A11, spec goal 11).

The engine's timing is exact, so what can be wrong with a line is in its settings and its floor
geometry, plus two guards in the run. Warnings by default; `simulation.strict_checks` turns
each into an error with the same code, which stops the run (D1371).

* **Static** (`static_checks`, called by structured validation, so the editor, the command
  line and the agent tools all see it): on the pieces placed on the floor, a CONVEYOR with a
  `path` and a CONVEYOR_TRANSFER (R55); a legacy belt with no path is skipped.
  - W_CONVEYOR_OPEN_PORT: a port no link joins.
  - W_CONVEYOR_OVERLAP: two belts' paths closer than half their widths, unless joined.
  - W_CONVEYOR_SNAP: two linked ports more than 0.05 m or 5 degrees apart.
* **Run guards** (`run_warning`, a coded log warning once per block and code):
  - W_CONVEYOR_HANDED_TO_NOTHING: a part reaches an end with no next block; it stands there.
  - W_ROUTE_FORBIDDEN: a part takes an exit its rule cannot choose now (`route_taken`).

A CONVEYOR_TRANSFER has no footprint or port poses in the engine yet, so it is checked for open
ports only, and the belts on either side of one count as joined (D1371).
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace

from nagasim.core import conveyor_path
from nagasim.core.errors import ModelRunError
from nagasim.core.problems import Problem, problem

__all__ = ["STRICT_CODES", "RUN_HINTS", "static_checks", "run_warning", "keep_run_warning", "route_taken", "UNTYPED_WIDTH_M"]

log = logging.getLogger(__name__)

# The codes `strict_checks` turns into errors; every other W_ code stays a warning.
STRICT_CODES = ("W_CONVEYOR_OPEN_PORT", "W_CONVEYOR_OVERLAP", "W_CONVEYOR_SNAP",
                "W_CONVEYOR_HANDED_TO_NOTHING", "W_ROUTE_FORBIDDEN")

# An untyped belt's width for the overlap check: the width the editor draws it at (3D view).
UNTYPED_WIDTH_M = 0.8
SNAP_M, SNAP_DEG = 0.05, 5.0          # the snapping tolerance of `conveyor_path.snap_candidates`
_ARC_STEP_DEG = 10.0                  # an arc is checked as chords of at most this angle


# -- the strict switch -------------------------------------------------------------------------
def _strict(layout: dict) -> bool:
    sim = layout.get("simulation") if isinstance(layout, dict) else None
    return isinstance(sim, dict) and sim.get("strict_checks") is True


# The hint of each run-time code, for the Problem a run hands back (D1389).
RUN_HINTS = {
    "W_CONVEYOR_HANDED_TO_NOTHING": "link the belt's end to the next block, or end the line in a DISPOSE",
    "W_ROUTE_FORBIDDEN": "set the attribute the rule reads before the part is routed and keep it "
                         "until it has left, or name the exit it holds",
    "W_ROUTE_HELD_AT_END": "check the flow after the exits of the rule, or raise its limit",
    "W_REVERSED_HELD_AT_END": "check what the belt's next block waits for, and the signal that turns "
                              "the belt again",
    # D1395, not a conveyor code: the one run notice of a log level, kept by the same channel.
    "W_SAMPLES_NOTHING": "give the model a run length or simulation.timeseries_points, or run at "
                         "level kpi",
}


def keep_run_warning(ctx, code: str, message: str, block: str, case: str = "") -> None:
    """D1389: keep a run-time warning as a Problem in the run's result, once per (code, block,
    case); the log line is the caller's. A replication's list is unioned by the same key."""
    key = (code, block, case)
    if key not in ctx.run_problems:
        # a Problem built directly: the code is a variable here, its plain line is by the code
        ctx.run_problems[key] = Problem(code=code, severity="warning", message=message,
                                        block=block, hint=RUN_HINTS.get(code))


def run_warning(block, code: str, message: str) -> None:
    """A run guard fired at *block*: an error with *code* that stops the run under
    `strict_checks`, else a warning logged once per block and code (a line of parts would
    repeat it for each)."""
    ctx = block.ctx
    text = f"Block '{block.name}': {message} ({code})"
    if ctx.strict_checks:
        error = ModelRunError(f"{text}; simulation.strict_checks stops the run on it.")
        error.block, error.code = block.name, code
        raise error
    key = (block.name, code)
    if key not in ctx.run_warned:
        ctx.run_warned.add(key)
        log.warning("%s", text)
        keep_run_warning(ctx, code, text, block.name)


def route_taken(block, rule, entity, index: int) -> None:
    """W_ROUTE_FORBIDDEN: *entity* leaves *block* by exit *index* of routing *rule*, chosen
    earlier (at a divert group's head, before a transfer's move). An exit the rule cannot
    choose now, such as one picked by an attribute that has changed since, or an attribute
    naming no exit, is warned. `possible_exits` has no side effects and draws nothing."""
    if rule.kind not in ("fixed", "attribute"):
        return              # any exit is possible for every other kind (an item rule: unknown)
    try:
        possible = rule.possible_exits(entity)
    except ValueError as exc:
        run_warning(block, "W_ROUTE_FORBIDDEN", f"a part took exit '{rule.exits[index]}' of "
                    f"its routing rule, which cannot choose any exit for it now: {exc}")
        return
    if possible is not None and index not in possible:
        names = ", ".join(f"'{rule.exits[i]}'" for i in sorted(possible))
        run_warning(block, "W_ROUTE_FORBIDDEN", f"a part took exit '{rule.exits[index]}' of "
                    f"its routing rule, which would choose {names} for it now")


# -- static checks -----------------------------------------------------------------------------
def static_checks(layout: dict) -> list[Problem]:
    """W_CONVEYOR_OPEN_PORT, W_CONVEYOR_OVERLAP and W_CONVEYOR_SNAP of the floor pieces among the
    layout's top-level blocks (a submodel definition's positions are not floor positions), as
    errors under `strict_checks`. Nothing to do, and no cost, for a model with no floor piece."""
    blocks = layout.get("blocks") if isinstance(layout, dict) else None
    if not isinstance(blocks, list):
        return []
    floor = {}
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("name"), str):
            continue
        kind = str(block.get("type", "")).upper()
        params = block.get("params") if isinstance(block.get("params"), dict) else {}
        if (kind == "CONVEYOR" and params.get("path")) or kind == "CONVEYOR_TRANSFER":
            floor[block["name"]] = (index, block, kind, params)
    if not floor:
        return []
    links = [k for k in layout.get("links") or [] if isinstance(k, dict)]
    pieces = _pieces(layout, floor)
    found = (_open_ports(floor, links) + _snaps(pieces, floor, links)
             + _overlaps(pieces, floor, links))
    if _strict(layout):
        found = [replace(p, severity="error") for p in found]
    return found


def _pieces(layout: dict, floor: dict) -> dict:
    """Belt name -> (index, {port name: Port}, polyline points, width m) of each floor belt whose
    geometry reads; one with a malformed path is left to E_CONVEYOR_PATH."""
    from nagasim.core.blocks.conveyor import belt_geometry
    from nagasim.core.conveyor_types import TYPES
    from nagasim.core.timeunits import units_of
    units = units_of(layout.get("simulation"))
    out = {}
    for name, (index, block, kind, params) in floor.items():
        if kind != "CONVEYOR":
            continue
        try:
            start, segments, diverts = belt_geometry(block, units)
        except (ValueError, TypeError, KeyError):
            continue
        if not segments:
            continue
        ports = {p.name: p for p in conveyor_path.ports(name, start, segments, diverts)}
        kind_name = params.get("type")
        width = TYPES[kind_name].width if kind_name in TYPES else UNTYPED_WIDTH_M
        out[name] = (index, ports, _polyline(start, segments), width)
    return out


def _polyline(start, segments) -> list:
    """The path's points, entry to end: a straight's ends, an arc's chords."""
    points, pose = [(start.x, start.y)], conveyor_path.Pose(*start)
    for seg in segments:
        steps = 1 if seg.kind == "straight" else max(1, math.ceil(seg.angle / _ARC_STEP_DEG))
        for k in range(1, steps + 1):
            p = conveyor_path.pose_at(pose, [seg], seg.length * k / steps)
            points.append((p.x, p.y))
        pose = conveyor_path.pose_at(pose, [seg], seg.length)
    return points


def _out_ports(names: list, outgoing: list, reverse_to: str = "") -> dict:
    """Exit name -> its link, for a block with exits *names* and outgoing links *outgoing*: a
    link naming its port takes it, the others take the lowest free exits in order (the run's
    rule, `conveyor_path.exit_order`); a reversible belt's link to its reverse_to takes "in"."""
    taken, free = {}, []
    for link in outgoing:
        port = link.get("source_port")
        if port in (None, "") and reverse_to and link.get("target") == reverse_to:
            port = "in"
        if port in (None, ""):
            free.append(link)
        elif port in names:
            taken.setdefault(port, link)
    spare = iter(n for n in names if n not in taken)
    for link in free:
        name = next(spare, None)
        if name is not None:
            taken[name] = link
    return taken


def _exits(block: dict, params: dict) -> tuple[list, str]:
    names = conveyor_path.exit_names(block) or []
    reverse_to = ""
    if params.get("reversible") is True:
        target = params.get("reverse_to")
        reverse_to = target.strip() if isinstance(target, str) and target.strip() != "in" else ""
    return names, reverse_to


def _open_ports(floor: dict, links: list) -> list:
    found, outgoing, incoming = [], {}, {}
    for link in links:
        outgoing.setdefault(link.get("source"), []).append(link)
        incoming.setdefault(link.get("target"), []).append(link)
    for name, (index, block, kind, params) in floor.items():
        names, reverse_to = _exits(block, params)
        linked = set(_out_ports(names, outgoing.get(name, []), reverse_to))
        ports = list(names)
        if kind == "CONVEYOR":
            merges = _merge_sources(params)
            if any(k.get("source") not in merges and k.get("target_port") in (None, "", "in")
                   for k in incoming.get(name, [])):
                linked.add("in")
            if "in" not in ports:
                ports.insert(0, "in")
        for port in ports:
            if port in linked:
                continue
            what = "Belt" if kind == "CONVEYOR" else "Transfer"
            found.append(problem(
                "W_CONVEYOR_OPEN_PORT", f"{what} {name}: port '{port}' is open, no link "
                f"joins it", f"/blocks/{index}", block=name,
                hint="snap the next piece to it or draw a link from it; between areas a link "
                     "with a delay counts",
                fields={"case": "belt" if what == "Belt" else "transfer", "port": port}))
    return found


def _snaps(pieces: dict, floor: dict, links: list) -> list:
    """Each link between two floor belts, from the port it leaves by to the port it enters,
    within the snapping tolerance. A timed link (an area link or a delay) joins two areas, a
    merge joins the belt's side and a reversible belt's start exit runs against its "in" port,
    so none of them is a port pair."""
    found = []
    outgoing: dict = {}
    for link in links:
        outgoing.setdefault(link.get("source"), []).append(link)
    for name, (index, ports, _points, _width) in pieces.items():
        _i, block, _kind, params = floor[name]
        names, reverse_to = _exits(block, params)
        for port, link in _out_ports(names, outgoing.get(name, []), reverse_to).items():
            target = link.get("target")
            if port == "in" or target not in pieces or link.get("delay"):
                continue
            into = link.get("target_port") or "in"
            if into != "in" or name in _merge_sources(floor[target][3]):
                continue
            a, b = ports[port].pose, pieces[target][1]["in"].pose
            gap = math.hypot(a.x - b.x, a.y - b.y)
            if gap <= SNAP_M and conveyor_path.same_angle(a.heading, b.heading, SNAP_DEG):
                continue
            turn = abs((a.heading - b.heading + 180.0) % 360.0 - 180.0)
            found.append(problem(
                "W_CONVEYOR_SNAP", f"{name}: port '{port}' and {target}'s 'in' are "
                f"linked but {gap:.3g} m and {turn:.3g} degrees apart", f"/blocks/{index}",
                block=name, hint="snap the piece again (place_piece snap_to) or move it so the "
                                 "two ports meet",
                fields={"case": "apart", "port": port, "target": target,
                        "gap": round(gap, 3), "turn": round(turn, 3)}))
    return found


def _merge_sources(params: dict) -> set:
    """The blocks a belt's merges take parts from (their links enter at the merge, not "in")."""
    merges = params.get("merges")
    return {m.get("from") for m in merges if isinstance(m, dict)}         if isinstance(merges, list) else set()


def _branch_stretch(params: dict, port) -> float:
    """1 / sin(angle) of the divert whose exit *port* ("d<k>") is, 1.0 for any other port."""
    diverts = params.get("diverts")
    diverts = diverts if isinstance(diverts, list) else []
    slot = conveyor_path.exit_slot(port, len(diverts))
    if not slot:
        return 1.0
    return 1.0 / math.sin(math.radians(conveyor_path.divert_angle(diverts[slot - 1])))


def _joints(pieces: dict, floor: dict, links: list) -> dict:
    """R57, R61: {pair of belts: {belt: (length cut off its start, off its end)}} for the pairs
    that meet at a joint: linked end to end (port to port), at a divert port or a merge (only
    the joining belt's end is cut, the other runs past), or both linked to one
    CONVEYOR_TRANSFER. The two compare only beyond one belt width (the wider) from their joint,
    where they touch by design; every other pair compares the whole of both. A timed link (an
    area link, a delay) joins areas, not pieces.

    R62: a branch at a divert port of trunk piece A starts on A's centreline, so its first half
    trunk width is its mouth drawn on the trunk, which A's port neighbours continue; against
    each of them only that half width of the branch is cut (not a full width, so a neighbour
    turning toward the branch side still collides beyond the trunk's edge).

    D1388: a branch at a divert `angle` a leaves the trunk's edge only after its centreline is
    1 / sin(a) times as far along, so both of its cuts stretch by 1 / sin(a): the width against
    the trunk, and against the neighbours (h_trunk + h_branch |cos a|) / sin a, h the half
    widths, where its near corner leaves their band (A2-R7); at 90 they are as before."""
    found: dict = {}
    at_module: dict = {}            # transfer -> [(belt, cut its start)]
    neighbours: dict = {}           # belt -> the belts joined to its in or out port
    branches: list = []             # (trunk, branch, 1 / sin(angle)) joined at a divert port

    def width(name):
        return pieces[name][3]

    def cut(pair_a, a_end, pair_b, b_end, b_stretch=1.0):
        if pair_a == pair_b:
            return
        size = max(width(pair_a), width(pair_b))
        cuts = found.setdefault(frozenset((pair_a, pair_b)), {pair_a: [0.0, 0.0],
                                                             pair_b: [0.0, 0.0]})
        for name, end, stretch in ((pair_a, a_end, 1.0), (pair_b, b_end, b_stretch)):
            if end is not None:
                cuts[name][end] = max(cuts[name][end], size * stretch)

    outgoing: dict = {}
    for link in links:
        if not link.get("delay"):
            outgoing.setdefault(link.get("source"), []).append(link)
    for name, (_index, block, kind, params) in floor.items():
        names, reverse_to = _exits(block, params)
        for port, link in _out_ports(names, outgoing.get(name, []), reverse_to).items():
            target = link.get("target")
            own = {"out": 1, "in": 0}.get(port)     # a divert port lies along the belt
            if name in pieces and target in pieces:
                into = None if name in _merge_sources(floor[target][3]) else 0
                stretch = _branch_stretch(params, port) if own is None else 1.0
                cut(name, own, target, into, stretch)
                if own is None and port != "in":
                    branches.append((name, target, stretch))
                elif into is not None:
                    neighbours.setdefault(name, set()).add(target)
                    neighbours.setdefault(target, set()).add(name)
            elif name in pieces and target in floor:
                at_module.setdefault(target, []).append((name, own))
            elif name in floor and target in pieces:
                at_module.setdefault(name, []).append((target, 0))
    for members in at_module.values():
        for i, (a, a_end) in enumerate(members):
            for b, b_end in members[i + 1:]:
                cut(a, a_end, b, b_end)
    for trunk, branch, stretch in branches:
        # A2-R7 (final review I3): the branch's near corner leaves the trunk's neighbours'
        # band only (h_trunk + h_branch |cos a|) / sin a along it; h_trunk at 90 (R62)
        cos = math.sqrt(max(0.0, 1.0 - 1.0 / stretch ** 2))
        mouth = (width(trunk) / 2.0 + width(branch) / 2.0 * cos) * stretch
        for other in neighbours.get(trunk, ()):
            if other == branch:
                continue
            cuts = found.setdefault(frozenset((branch, other)), {branch: [0.0, 0.0],
                                                                 other: [0.0, 0.0]})
            cuts[branch][0] = max(cuts[branch][0], mouth)
    return found


OVERLAPS_LISTED = 20        # R59: a pile of pieces gives one line per pair; the rest are counted


def _overlaps(pieces: dict, floor: dict, links: list) -> list:
    """Belts whose floor areas overlap: each piece of a path is a rectangle its belt's width
    across with flat ends (an arc a chain of chords), so pieces laid end to end only touch.
    Pieces that meet at a joint skip the belt width around it (R57, R61); a belt's own
    rectangles, a corner's included, are never compared with each other. The rectangles go
    into a uniform grid by their bounding boxes, so only those sharing a cell are measured:
    2,000 pieces check in well under a second."""
    if len(pieces) < 2:
        return []
    joints = _joints(pieces, floor, links)
    reach = max(width for _i, _p, _pts, width in pieces.values()) / 2.0
    cell = max(reach * 4.0, 1.0)
    grid: dict = {}
    segments, totals = [], {}
    for name, (_index, _ports, points, width) in pieces.items():
        run, half = 0.0, width / 2.0
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            step = math.hypot(x1 - x0, y1 - y0)
            if step <= 1e-12:
                continue
            k = len(segments)
            segments.append((name, x0, y0, x1, y1, half, run, run + step))
            run += step
            gx0, gx1 = math.floor((min(x0, x1) - half) / cell), math.floor((max(x0, x1) + half) / cell)
            gy0, gy1 = math.floor((min(y0, y1) - half) / cell), math.floor((max(y0, y1) + half) / cell)
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    grid.setdefault((gx, gy), []).append(k)
        totals[name] = run
    hits, seen = {}, set()
    for members in grid.values():
        for i, a in enumerate(members):
            sa = segments[a]
            for b in members[i + 1:]:
                sb = segments[b]
                if sa[0] == sb[0]:
                    continue
                pair = frozenset((sa[0], sb[0]))
                if pair in hits or (a, b) in seen:
                    continue
                seen.add((a, b))
                cuts = joints.get(pair)
                ra, rb = sa, sb
                if cuts is not None:
                    ra = _clipped(sa, *cuts[sa[0]], totals[sa[0]])
                    rb = _clipped(sb, *cuts[sb[0]], totals[sb[0]])
                    if ra is None or rb is None:
                        continue
                if _rectangles_overlap(ra, rb):
                    hits[pair] = sa[5] + sb[5]
    found = []
    ordered = sorted(hits, key=sorted)
    for pair in ordered[:OVERLAPS_LISTED]:
        a, b = sorted(pair)
        index = pieces[a][0]
        found.append(problem(
            "W_CONVEYOR_OVERLAP", f"{a} and {b} overlap: their paths come closer than "
                                  f"half their widths ({hits[pair]:.3g} m)", f"/blocks/{index}",
            block=a, hint="move one of them, or join them port to port if one feeds the other",
            fields={"case": "pair", "other": b}))
    more = len(ordered) - OVERLAPS_LISTED
    if more > 0:
        found.append(problem(
            "W_CONVEYOR_OVERLAP", f"{more} more pairs of belts overlap, not listed", "/blocks",
            hint="fix the pairs listed first: pieces stacked on each other give a pair each",
            fields={"case": "more", "count": more}))
    return found


def _clipped(seg, head: float, tail: float, total: float):
    """*seg* (name, x0, y0, x1, y1, half, s0, s1) cut to the stretch [head, total - tail] of its
    belt; None when none of it is left."""
    name, x0, y0, x1, y1, half, s0, s1 = seg
    lo, hi = max(s0, head), min(s1, total - tail)
    if hi - lo <= 1e-9:
        return None
    u0, u1 = (lo - s0) / (s1 - s0), (hi - s0) / (s1 - s0)
    return (name, x0 + u0 * (x1 - x0), y0 + u0 * (y1 - y0), x0 + u1 * (x1 - x0),
            y0 + u1 * (y1 - y0), half, lo, hi)


def _rectangles_overlap(s, t) -> bool:
    """The flat-ended rectangles of segments *s* and *t* (each its half-width across) overlap
    by more than a touch: no axis of either separates them (separating axis test)."""
    boxes = []
    for _name, x0, y0, x1, y1, half, _s0, _s1 in (s, t):
        length = math.hypot(x1 - x0, y1 - y0)
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        boxes.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, ux, uy, length / 2.0, half))
    (ax, ay, aux, auy, al, aw), (bx, by, bux, buy, bl, bw) = boxes
    dx, dy = bx - ax, by - ay
    for vx, vy in ((aux, auy), (-auy, aux), (bux, buy), (-buy, bux)):
        ra = al * abs(aux * vx + auy * vy) + aw * abs(-auy * vx + aux * vy)
        rb = bl * abs(bux * vx + buy * vy) + bw * abs(-buy * vx + bux * vy)
        if abs(dx * vx + dy * vy) >= ra + rb - 1e-6:
            return False
    return True


def exit_links(block: dict, outgoing: list) -> dict:
    """Exit name -> its link, for a CONVEYOR or CONVEYOR_TRANSFER block dict and its outgoing
    links: the resolver the static checks use, shared with the agent tools' `get_ports`."""
    params = block.get("params") if isinstance(block.get("params"), dict) else {}
    names, reverse_to = _exits(block, params)
    return _out_ports(names, outgoing, reverse_to)
