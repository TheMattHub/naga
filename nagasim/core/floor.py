"""Floor pieces of a model: a CONVEYOR or CONVEYOR_TRANSFER laid out by its floor-plan pose.

Their ports, the open ones, a merge point and its refusal, adding a piece, and the one move rule
(plan B O10, agent tool gaps Task 4) that every editor move and the agent's `set_layout` share.
Free functions over a `ModelDocument` (or a `DefinitionView`), written only through its API;
split out of `core/document.py` so the document stays the record and its undo, and this the
conveyor floor's geometry rules. Stdlib + engine only, like the document.
"""
from __future__ import annotations

import copy
import math
from typing import TYPE_CHECKING

from nagasim.core.document import _link_entry
from nagasim.core.timeunits import units_of

if TYPE_CHECKING:
    from nagasim.core.document import DefinitionView, ModelDocument
    Document = ModelDocument | DefinitionView


def connect_ports(document: Document, out_port, in_port) -> None:
    """Link *out_port* to *in_port* (`conveyor_path.Port`s): a link from the out port's block to
    the in port's block carrying `source_port` and `target_port`."""
    if out_port.role != "out" or in_port.role != "in":
        raise ValueError("connect_ports: join an out port to an in port")
    document.add_link(out_port.block, in_port.block,
                      source_port=out_port.name, target_port=in_port.name)


def piece_ports(document: Document, name: str) -> list:
    """The `conveyor_path.Port`s of block *name*, laid out from its floor-plan pose: a CONVEYOR's
    (`conveyor.belt_ports`), a CONVEYOR_TRANSFER's (`conveyor_path.transfer_ports`), any other
    block's none. The one rule of the agent tools and the editor."""
    from nagasim.core import conveyor_path
    from nagasim.core.blocks import conveyor
    block = document.blocks[name]
    if block.type == "CONVEYOR":
        return conveyor.belt_ports({"name": name, "params": block.params,
                                    "layout_position": block.layout_position,
                                    "layout_rotation": block.layout_rotation},
                                   units_of(document.simulation))
    if block.type == "CONVEYOR_TRANSFER":
        return conveyor_path.transfer_ports(name, block.layout_position, block.layout_rotation,
                                            block.params)
    return []


def floor_geometry(document: Document, name: str):
    """`conveyor.belt_geometry` of belt *name*: (start pose, segments, diverts) in metres, or
    None for a missing block, a non-conveyor, a belt with no path, or a path the engine
    refuses. The editor's drawing and `place_feeder` read a trunk by this one rule."""
    from nagasim.core.blocks.conveyor import belt_geometry
    block = document.blocks.get(name)
    if block is None or block.type != "CONVEYOR" or not (block.params or {}).get("path"):
        return None
    try:
        return belt_geometry({"name": name, "params": block.params,
                              "layout_position": block.layout_position,
                              "layout_rotation": block.layout_rotation},
                             units_of(document.simulation))
    except (ValueError, TypeError, KeyError):
        return None


def length_factor(document: Document, params: dict) -> float:
    """Metres per length unit of one conveyor piece.

    A piece's path, part lengths and stored guide openings are in its `length_unit`, else the
    model's (`simulation.length_unit`); multiply by this to get metres.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    params : dict
        The CONVEYOR block's params.

    Returns
    -------
    float
        Metres per unit: 1.0 in a metre model, 0.001 in a millimetre one.

    See Also
    --------
    nagasim.appearance.look_of : Takes this factor to give openings in metres.
    nagasim.appearance.piece_length_m : A piece's length already in metres.

    Examples
    --------
    >>> from nagasim.appearance import length_factor
    >>> from nagasim.appearance import ModelDocument
    >>> length_factor(ModelDocument({"simulation": {"length_unit": "millimeters"}}), {})
    0.001
    """
    from nagasim.core.blocks import conveyor
    return conveyor.length_factor(params, units_of(document.simulation))


def merge_point(document: Document, trunk: str, at: float):
    """The floor pose (metres; heading the trunk's travel there) on belt *trunk*'s centreline
    at *at* (its unit, clamped to the path), or None when the trunk is not on the floor. The
    editor's merge drawing and `place_feeder` share it."""
    from nagasim.core import conveyor_path
    geometry = floor_geometry(document, trunk)
    if geometry is None:
        return None
    s_m = at * length_factor(document, document.blocks[trunk].params or {})
    return conveyor_path.pose_at(geometry[0], geometry[1], s_m)      # pose_at clamps s


def point_refusal(document: Document, name: str, field: str, at: float,
                  source: str | None = None) -> str | None:
    """Why a new divert or merge at *at* (the belt's unit) cannot go on belt *name*, in the
    engine's one-sentence words (`plainlines.plain`); None when it can. A merge's parts come
    from *source* (None: a feeder not drawn yet).

    #2040: the engine's own validation judges trials of the belt with the point added, so a
    rule it has (zones, eyes, a reversible belt, the path's end) is never re-implemented here
    and never drifts. A trial divert carries the editor's condition and its merge the link
    from its feeder; a divert's missing exit link (`a7_links`) is not a refusal, since the
    branch is drawn after it. One rule for the editor and `place_feeder`."""
    from nagasim.core.plainlines import plain
    from nagasim.core.schema import validate_model_structured
    block = document.blocks[name]
    feeder = source or f"{name} feeder (trial)"
    trunk = {"name": name, "type": block.type, "params": copy.deepcopy(block.params or {}),
             "layout_position": block.layout_position, "layout_rotation": block.layout_rotation}
    # Only the belt and its feeder: the point rules are the belt's own, and a submodel level
    # has no whole-model layout to put them in.
    layout = {"simulation": copy.deepcopy(document.simulation), "blocks": [trunk], "links": []}
    if field == "merges":
        layout["blocks"].append({"name": feeder, "type": "CONVEYOR", "params": {}})
        layout["links"].append({"source": feeder, "target": name, "source_port": "out"})

    def errors(entries: list) -> list:
        trunk["params"][field] = entries
        return [p for p in validate_model_structured(layout)
                if p.severity == "error" and p.block == name
                and not (field == "diverts" and (p.fields or {}).get("case") == "a7_links")]

    def new(entries: list) -> list:
        before = {(p.code, p.message) for p in errors(entries)}
        return [p for p in errors(entries + [entry]) if (p.code, p.message) not in before]
    entry = {"at": at, "from": feeder} if field == "merges" else {"at": at, "condition": "0"}
    # The point among the belt's own (another point there, zones), then alone: a belt-level
    # error the existing points already raise (a merge on a non-accumulating belt) reads the
    # same with one more point, so only the trial without them shows the new one has it too.
    found = new(list(block.params.get(field) or [])) or new([])
    return plain(found[0]) if found else None


def open_ports(document: Document) -> list:
    """The open `conveyor_path.Port`s of *document*'s floor pieces (`conveyor_checks.open_ports`,
    the rule W_CONVEYOR_OPEN_PORT reports), laid out by `piece_ports`; no `problems()` run, so
    the editor can index them once when a gesture starts."""
    from nagasim.core import conveyor_checks
    blocks = [{"name": name, "type": block.type, "params": block.params}
              for name, block in document.blocks.items()]
    links = [_link_entry(link) for link in document.link_records()]
    open_names = conveyor_checks.open_ports({"blocks": blocks, "links": links})
    wanted: dict[str, set] = {}
    for name, port in open_names:
        wanted.setdefault(name, set()).add(port)
    found = []
    for name, ports in wanted.items():
        try:
            laid = piece_ports(document, name)
        except (ValueError, TypeError, KeyError):
            continue        # a malformed path is E_CONVEYOR_PATH's; it has no pose to snap to
        found += [p for p in laid if p.name in ports]
    return found


# -- moving a floor piece (plan B O10; agent tool gaps Task 4): one rule for every editor move
# and the agent's `set_layout`. A move unlinks the moved pieces' joins, keeps area links, drops
# the merges whose feeder end no longer meets and joins the ports that then meet at "ok".

def is_piece(document: Document, name: str) -> bool:
    """A floor piece with a floor place: the engine's rule (`conveyor_checks.floor_piece`, a
    CONVEYOR with a path or a CONVEYOR_TRANSFER) plus a `layout_position`, the place a drawing
    or a drag needs. A length-only belt is a plain block on the floor: a move keeps its links
    (#2035)."""
    from nagasim.core.conveyor_checks import floor_piece
    block = document.blocks.get(name)
    return (block is not None and block.layout_position is not None
            and floor_piece(block.type, block.params))


def link_keys(records) -> list[tuple[str, str, int]]:
    """(source, target, index among that pair) of every record, in record order."""
    seen: dict[tuple[str, str], int] = {}
    keys = []
    for link in records:
        pair = (link.source, link.target)
        keys.append((link.source, link.target, seen.get(pair, 0)))
        seen[pair] = seen.get(pair, 0) + 1
    return keys


def is_join(document: Document, link) -> bool:
    """A port-to-port join: both ends floor pieces and no delay (a delayed link joins two
    areas, D1369, and survives a move). A feeder's link into a merge enters mid-belt, not at a
    port: a move drops it with its merge entry or keeps both (`stale_merges`), never the link
    alone (E_CONVEYOR_MERGE)."""
    if link.delay is not None or not is_piece(document, link.source) \
            or not is_piece(document, link.target):
        return False
    merges = (document.blocks[link.target].params or {}).get("merges") or []
    return not any(isinstance(m, dict) and m.get("from") == link.source for m in merges)


def piece_joins(document: Document, moved) -> list[tuple[str, str, int]]:
    """The joins between a piece in *moved* and one left behind: what a move removes. Joins
    among the moved pieces move with them and stay."""
    moved = set(moved)
    records = document.link_records()
    return [key for key, link in zip(link_keys(records), records)
            if is_join(document, link) and ((link.source in moved) != (link.target in moved))]


def unlink(document: Document, keys) -> None:
    """Remove the links *keys* names; the highest index of a pair first, so the rest hold."""
    for src, tgt, index in sorted(keys, key=lambda k: -k[2]):
        document.remove_link(src, tgt, index=index)


def _split_open(document: Document, moved):
    found = open_ports(document)
    return ([p for p in found if p.block in moved], [p for p in found if p.block not in moved])


def capture(ports: list, point, role: str, tol_m: float):
    """The nearest port of *role* within *tol_m* of *point*, found through `snap_candidates`
    (a probe of the opposite role, any heading)."""
    from nagasim.core.conveyor_path import Port, Pose, snap_candidates
    probe = Port("", "probe", Pose(point[0], point[1], 0.0), "in" if role == "out" else "out")
    pairs = snap_candidates(ports, [probe], tol_m=tol_m, tol_deg=180.0)
    hits = [a for a, _b, status in pairs if status == "ok" and a.role == role]
    return min(hits, key=lambda a: math.hypot(a.pose.x - point[0], a.pose.y - point[1]),
               default=None)


def drop_delta(document: Document, moved, tol_m: float) -> tuple[float, float] | None:
    """The shift (dx, dy) in metres that puts the moved pieces' open port nearest an open port
    of the opposite role left behind exactly onto it, when one is within *tol_m*; else None."""
    mine, theirs = _split_open(document, set(moved))
    best = None
    for port in mine:
        other = "in" if port.role == "out" else "out"
        hit = capture(theirs, (port.pose.x, port.pose.y), other, tol_m)
        if hit is None:
            continue
        gap = math.hypot(hit.pose.x - port.pose.x, hit.pose.y - port.pose.y)
        if best is None or gap < best[0]:
            best = (gap, hit.pose.x - port.pose.x, hit.pose.y - port.pose.y)
    return None if best is None else (best[1], best[2])


def _pinned(document: Document) -> set:
    """(block, port) of every port a link names, at either end."""
    found = set()
    for link in document.link_records():
        if link.ports.get("source_port"):
            found.add((link.source, link.ports["source_port"]))
        if link.ports.get("target_port"):
            found.add((link.target, link.ports["target_port"]))
    return found


def _freed_ports(document: Document, dropped) -> list:
    """The ports *dropped* (removed joins) named, that no remaining link names: open again
    for `relink`, although an area link naming no port takes such a slot by the run's rule
    (`conveyor_checks._out_ports`, #2041). A join that still meets is made again where it was;
    the area link then takes no exit, as before the move."""
    pinned, found = _pinned(document), []
    for link in dropped:
        for block, key in ((link.source, "source_port"), (link.target, "target_port")):
            port = link.ports.get(key)
            if port and (block, port) not in pinned and block in document.blocks:
                found += [p for p in piece_ports(document, block) if p.name == port]
    return found


def relink(document: Document, moved, dropped=()) -> int:
    """Join every open port of the moved pieces to the open port left behind that
    `snap_candidates` calls "ok" at the engine's tolerance (0.05 m, 5 deg), nearest first;
    returns how many links it made. *dropped* (the `LinkData` of the joins the move removed)
    reopens their ports (#2041) and gives a join made again at the same ports its capacity and
    kept keys back (#2042)."""
    from nagasim.core.conveyor_path import snap_candidates
    moved = set(moved)
    mine, theirs = _split_open(document, moved)
    have = {(p.block, p.name) for p in mine + theirs}
    for port in _freed_ports(document, dropped):
        if (port.block, port.name) not in have:
            (mine if port.block in moved else theirs).append(port)
            have.add((port.block, port.name))
    pairs = [(math.hypot(a.pose.x - b.pose.x, a.pose.y - b.pose.y), a, b)
             for a, b, status in snap_candidates(mine, theirs) if status == "ok"]
    left = list(dropped)
    used, made = set(), 0
    for _gap, a, b in sorted(pairs, key=lambda row: row[0]):
        if (a.block, a.name) in used or (b.block, b.name) in used:
            continue
        out, into = (a, b) if a.role == "out" else (b, a)
        connect_ports(document, out, into)
        old = next((k for k in left if k.source == out.block and k.target == into.block
                    and k.ports.get("source_port") in (None, out.name)
                    and k.ports.get("target_port") in (None, into.name)), None)
        if old is not None:
            left.remove(old)
            index = document.link_count(out.block, into.block) - 1
            if old.area:
                document.set_link_area(out.block, into.block, old.area, index=index)
            if old.extra:
                document.set_link_extra(out.block, into.block, old.extra, index=index)
        used |= {(a.block, a.name), (b.block, b.name)}
        made += 1
    return made


def belt_width_m(params: dict) -> float:
    """A belt's real width: its type's, else the untyped width the engine checks with."""
    from nagasim.core import conveyor_look
    from nagasim.core.conveyor_checks import UNTYPED_WIDTH_M
    width = conveyor_look.width_m(params or {})
    return UNTYPED_WIDTH_M if width is None else width


def meets(end, point, width_m: float) -> bool:
    """A feeder's end at *end* meets a merge at *point* (both floor metres) on a belt
    *width_m* wide: it lies inside the band there, within the engine's snapping tolerance
    of its edge (`conveyor_checks.SNAP_M`). In metres, not screen pixels, so the drawing (no
    view) and a move read it the same at any zoom."""
    from nagasim.core.conveyor_checks import SNAP_M
    return math.hypot(end[0] - point[0], end[1] - point[1]) <= width_m / 2.0 + SNAP_M


def merge_meets(document: Document, trunk: str, merge: dict) -> bool:
    """`meets` for one merge entry of belt *trunk*; True when either belt is not drawn on the
    floor (a merge from a plain block has no end to meet, so a move never drops it)."""
    from nagasim.core.conveyor_path import end_pose
    feeder = merge.get("from")
    at = merge.get("at")
    if not isinstance(feeder, str) or isinstance(at, bool) or not isinstance(at, (int, float)):
        return True
    geometry = floor_geometry(document, feeder)
    point = merge_point(document, trunk, at)
    if geometry is None or point is None:
        return True
    end = end_pose(geometry[0], geometry[1])
    return meets((end.x, end.y), (point.x, point.y),
                 belt_width_m(document.blocks[trunk].params))


def merge_joined(document: Document, trunk: str, feeder) -> bool:
    """Whether a merge from *feeder* into *trunk* is a join: an undelayed link carries it. One
    fed over a delayed (area) link joins two areas and, like that link, survives a move."""
    return any(link.source == feeder and link.target == trunk and link.delay is None
               for link in document.link_records())


def stale_merges(document: Document, moved, feeders=()) -> list[tuple[str, str]]:
    """(trunk, feeder) of every merge a move breaks (plan B Task 6 ruling, O10): one of the two
    belts in *moved* and the other not, and the feeder's end no longer meets the merge point.
    Pieces moved together keep their merge. A feeder in *feeders* (an end drag) loses its
    merges whatever it now meets, as an end drag drops its out port's join."""
    moved, feeders = set(moved), set(feeders)
    found = []
    for trunk, block in document.blocks.items():
        if block.type != "CONVEYOR":
            continue
        for merge in (block.params or {}).get("merges") or []:
            if not isinstance(merge, dict):
                continue
            feeder = merge.get("from")
            if feeder not in moved and trunk not in moved and feeder not in feeders:
                continue
            if not merge_joined(document, trunk, feeder):
                continue
            if feeder in feeders and trunk not in feeders:
                found.append((trunk, feeder))
            elif ((trunk in moved) != (feeder in moved)) \
                    and not merge_meets(document, trunk, merge):
                found.append((trunk, feeder))
    return found


def drop_merges(document: Document, pairs) -> None:
    """Remove each (trunk, feeder) merge entry together with the feeder's links into the trunk
    (never one without the other, so no E_CONVEYOR_MERGE); the caller's transaction."""
    by_trunk: dict[str, set] = {}
    for trunk, feeder in pairs:
        by_trunk.setdefault(trunk, set()).add(feeder)
    for trunk, feeders in by_trunk.items():
        params = document.blocks[trunk].params or {}
        document.set_params(trunk, {**params, "merges": [
            m for m in params.get("merges") or []
            if not (isinstance(m, dict) and m.get("from") in feeders)]})
        records = document.link_records()
        unlink(document, [key for key, link in zip(link_keys(records), records)
                          if link.target == trunk and link.source in feeders
                          and link.delay is None])


def move_floor(document: Document, positions=None, rotations=None,
               capture: float | None = None) -> bool:
    """Every floor move's one rule (O10, plan B Task 6 rulings), inside the caller's
    transaction: remove the moved pieces' joins to the pieces left behind (area links stay),
    write the new places (*positions*) and turns (*rotations*), with *capture* (metres) shift
    the moved pieces so a port inside it lands on an open port of the opposite role, drop the
    merges the move breaks with their links, and join every port that then meets one at the
    engine's "ok". Any block may be named; with no conveyor piece among them it only writes.
    True when a link changed. The editor's 2D drag, nudge and turns, the R key, Properties,
    Place on floor and the 3D view (#2032) and the agent's `set_layout` all move through here."""
    positions, rotations = dict(positions or {}), dict(rotations or {})
    moved = set(positions) | set(rotations)
    if not any(is_piece(document, n) for n in moved):
        if positions:
            document.set_layout_positions(positions)
        if rotations:
            document.set_layout_rotations(rotations)
        return False
    dropped = piece_joins(document, moved)
    records = dict(zip(link_keys(document.link_records()), document.link_records()))
    removed = [copy.deepcopy(records[key]) for key in dropped]
    unlink(document, dropped)
    if positions:
        document.set_layout_positions(positions)
    if rotations:
        document.set_layout_rotations(rotations)
    if capture is not None:
        delta = drop_delta(document, moved, capture)
        if delta is not None:
            document.set_layout_positions({
                n: (round(document.blocks[n].layout_position[0] + delta[0], 6),
                    round(document.blocks[n].layout_position[1] + delta[1], 6))
                for n in moved if document.blocks[n].layout_position is not None})
    # A merge is a join to the trunk's side: it goes, entry and link in this step, when one
    # of its two belts moved and the feeder's end no longer meets it.
    stale = stale_merges(document, moved)
    drop_merges(document, stale)
    made = relink(document, moved, removed)
    return bool(dropped) or bool(stale) or made > 0


def _next_spot(document: Document) -> tuple[float, float]:
    return (200.0 + 220.0 * len(document.blocks), 300.0)


def _floor_pose(pose, source) -> tuple[float, float, float]:
    """*pose* (x, y, heading), else the pose of *source*, else the origin; x and y rounded to
    micrometres, as the agent tools write them."""
    x, y, heading = pose if pose is not None else (source.pose if source else (0.0, 0.0, 0.0))
    return round(float(x), 6), round(float(y), 6), heading


def add_belt_piece(document: Document, segments: list[dict], kind: str = "", pose=None,
                   source=None, speed: float | None = None, name: str | None = None,
                   position: tuple[float, float] | None = None) -> str:
    """Add one CONVEYOR in one transaction and return its name: `path` = *segments*, the type
    *kind* with its datasheet speed (m/s) when *kind* is set, *speed* (model length per time
    unit) when given, the floor *pose* (x, y, heading; None: the pose of *source*, else the
    origin) and, with *source* (an out `Port`), a
    link from it to the new piece's "in". *position* is the schematic spot (None: the next free
    one). The caller validates; `place_piece` and the editor share this write."""
    from nagasim.core import conveyor_look
    from nagasim.core.conveyor_types import TYPES
    x, y, heading = _floor_pose(pose, source)
    with document.transaction():
        created = document.add_block("CONVEYOR", position or _next_spot(document), name)
        params = {**document.blocks[created].params, "path": segments}
        if kind:
            # Written at creation so the file shows it (D1298), in the datasheet's own unit so a
            # later change of the model's time unit leaves the belt's speed as it is (I1).
            params.update(type=kind, speed=TYPES[kind].speed, speed_unit="m/s")
            if source is not None and document.blocks.get(source.block) is not None \
                    and document.blocks[source.block].type == "CONVEYOR" \
                    and document.blocks[source.block].params.get("type") == kind:
                shared = conveyor_look.inherit(document.blocks[source.block].params)
                if shared["look"]:                    # D1475: a snapped piece takes the look
                    params.update(shared)
        if speed is not None:
            params.update(speed=float(speed), speed_unit="base")
        document.set_params(created, params)
        document.set_layout_positions({created: (x, y)})
        document.set_layout_rotations({created: heading})      # folded into [0, 360) (D74)
        if source is not None:
            ours = next(p for p in piece_ports(document, created) if p.name == "in")
            connect_ports(document, source, ours)
    return created


def add_transfer_piece(document: Document, mechanism: str = "popup",
                       exits: list[dict] | None = None, pose=None, source=None,
                       name: str | None = None,
                       position: tuple[float, float] | None = None) -> str:
    """Add one CONVEYOR_TRANSFER in one transaction and return its name: *mechanism*, *exits*
    (`{"port", "angle"}` entries; None: one straight exit "out"), the floor *pose* and, with
    *source* (an out `Port`), a link from it. The caller validates; `place_transfer` and the
    editor share this write."""
    listed = exits or [{"port": "out", "angle": 0.0}]
    x, y, heading = _floor_pose(pose, source)
    with document.transaction():
        created = document.add_block("CONVEYOR_TRANSFER", position or _next_spot(document), name)
        document.set_params(created, {**document.blocks[created].params, "mechanism": mechanism,
                                      "exits": [{"port": e["port"].strip(),
                                                 "angle": float(e["angle"])} for e in listed]})
        document.set_layout_positions({created: (x, y)})
        document.set_layout_rotations({created: heading})
        if source is not None:
            document.add_link(source.block, created, source_port=source.name)
    return created
