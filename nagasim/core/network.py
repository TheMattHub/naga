"""Path network: the "network" model section (PHASE4 E1, ARCHITECTURE D68).

A logistics model looks like the real building, so travel time is GEOMETRY, not
a guessed distribution: nodes sit at real coordinates, segments join them, and
the distance between two places is the shortest path along the segments.

    {"length_factor": 1.0,
     "nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "aisle_1", "x": 30, "y": 0}],
     "segments": [{"from": "dock", "to": "aisle_1"},
                  {"from": "aisle_1", "to": "pick", "length": 18, "two_way": false}]}

Coordinates are LAYOUT coordinates -- metres once the background image declares
`units_per_metre` (P2.3a), otherwise whatever unit the modeller means by them;
the engine only ever divides a length by a speed, so the pair must agree and
nothing else cares. A segment's `length` defaults to the Euclidean distance
between its endpoints times the network's `length_factor` (1.0 = straight
aisles; 1.2 says "the real route is 20% longer than the crow flies"), and may be
given explicitly for a ramp, a curve or a one-way detour.

Routes are by TIME once a segment caps a speed (PHASE4 T7): `path(a, b, speed)`
weights each segment by `length / min(speed, segment speed)`, so a short aisle
behind a 1 m/s limit loses to a longer free one, and the tree is cached per
(source, speed) -- most models have one or two distinct speeds. A network with
no limit anywhere keeps exactly one tree per source and routes by length, which
orders the routes identically.

Segment `capacity` is enforced for FLEET travel since PHASE6 W3 M5 (D117); the
traffic controller lives with the fleets in `core/movers.py`, and this module
only validates the field and warns of a head-on deadlock risk.

Stdlib only, a leaf module beside `sequences.py`/`tables.py`: no simpy, no
resources, so the geometry is testable on its own. The fleets that DRIVE on
this network live in `core/movers.py`.
"""
from __future__ import annotations

import heapq
import math

from nagasim.core.problems import problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.timeunits import ModelUnits, units_of

__all__ = ["Network", "Segment", "build_network", "node_of_block", "block_nodes", "block_place",
           "point_on_path", "validate_network_structured",
           "normalise_rotation", "rotate_offset"]

# Exact cosine/sine for the four quarter turns, so a 90-degree step lands on the
# grid instead of 6.1e-17 off it (PHASE4 T2, D74).
_QUARTER_TURNS = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}


def normalise_rotation(value) -> float:
    """*value* in degrees, folded into [0, 360); 0.0 for None or anything
    unreadable. One place, so the file format, the agent tool and the canvas
    all agree on what an angle is (D74)."""
    try:
        angle = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(angle):
        return 0.0
    angle %= 360.0
    return 0.0 if angle == 0.0 else angle       # fold -0.0


def rotate_offset(dx: float, dy: float, degrees: float) -> tuple[float, float]:
    """(dx, dy) turned *degrees* CLOCKWISE as seen on screen, in the layout
    frame where +x is east and +y is south (docs/ASSET_GRAPHICS.md).

    90 degrees sends east to south: (10, 0) -> (0, 10)."""
    angle = normalise_rotation(degrees)
    cos, sin = _QUARTER_TURNS.get(angle, (None, None))
    if cos is None:
        radians = math.radians(angle)
        cos, sin = math.cos(radians), math.sin(radians)
    return (dx * cos - dy * sin, dx * sin + dy * cos)


class Segment:
    """One drivable link between two nodes."""

    __slots__ = ("a", "b", "length", "two_way", "speed", "capacity", "index")

    def __init__(self, a: str, b: str, length: float, two_way: bool = True,
                 speed: float | None = None, capacity: int | None = None, index: int = 0):
        self.a = a
        self.b = b
        self.length = float(length)
        self.two_way = bool(two_way)
        self.speed = speed
        self.capacity = capacity
        self.index = index

    @property
    def name(self) -> str:
        """`a->b` for a one-way segment, `a<->b` for a two-way one -- the id the
        segment KPI frame reports under."""
        return f"{self.a}{'<->' if self.two_way else '->'}{self.b}"

    def __repr__(self):
        return f"<Segment {self.name} length={self.length:g}>"


class Network:
    """Nodes, segments and shortest paths between them.

    Dijkstra runs at most once per SOURCE node and the result is kept, so a run
    that routes between the same places over and over pays for the graph once.
    """

    def __init__(self, nodes: dict[str, tuple[float, float]], segments: list[Segment]):
        self.nodes = nodes
        self.segments = segments
        self.adjacency: dict[str, list[tuple[str, float, Segment]]] = {n: [] for n in nodes}
        for seg in segments:
            if seg.a in self.adjacency and seg.b in self.adjacency:
                self.adjacency[seg.a].append((seg.b, seg.length, seg))
                if seg.two_way:
                    self.adjacency[seg.b].append((seg.a, seg.length, seg))
        self._trees: dict[tuple, tuple[dict, dict]] = {}
        # Routing by TIME only exists once a segment caps a speed (PHASE4 T7,
        # ARCHITECTURE D80): with no limit anywhere every route is length / one
        # speed, so time and length order the routes identically and a model
        # that declares no limit keeps exactly one tree per source.
        self.speed_limited = any(seg.speed for seg in segments)

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def __repr__(self):
        return f"<Network nodes={len(self.nodes)} segments={len(self.segments)}>"

    def has(self, node: str) -> bool:
        return node in self.nodes

    def point(self, node: str) -> tuple[float, float]:
        return self.nodes[node]

    # -- shortest paths ------------------------------------------------------
    def _weight(self, seg: Segment, speed) -> float:
        """What one segment costs: its length, or the time to cross it at
        *speed* under its own limit."""
        if speed is None:
            return seg.length
        return seg.length / (speed if seg.speed is None else min(speed, seg.speed))

    def _cost_speed(self, speed):
        """The speed a route is weighted by: None (length) unless a segment
        really caps one, so an unlimited network keeps one tree per source."""
        return float(speed) if (self.speed_limited and speed and speed > 0) else None

    def _tree(self, source: str, speed=None) -> tuple[dict, dict]:
        """(cost, predecessor) from *source* to every reachable node, the cost
        being length or -- for a mover with a *speed* on a network that caps
        one -- travel time. One tree per (source, speed), and most models have
        one or two distinct speeds."""
        key = (source, speed)
        cached = self._trees.get(key)
        if cached is not None:
            return cached
        dist = {source: 0.0}
        prev: dict[str, str] = {}
        heap = [(0.0, source)]
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, math.inf):
                continue
            for neighbour, _length, seg in self.adjacency.get(node, ()):
                nd = d + self._weight(seg, speed)
                if nd < dist.get(neighbour, math.inf):
                    dist[neighbour] = nd
                    prev[neighbour] = node
                    heapq.heappush(heap, (nd, neighbour))
        self._trees[key] = (dist, prev)
        return dist, prev

    def distance(self, a: str, b: str) -> float:
        """Shortest-path length, or `inf` when *b* is not reachable from *a*.

        An unknown node raises: a model naming a node it never declared is a
        modelling mistake, not a distance of infinity.
        """
        self._require(a)
        self._require(b)
        if a == b:
            return 0.0
        return self._tree(a)[0].get(b, math.inf)

    def path(self, a: str, b: str, speed=None) -> list[str]:
        """The node list of the shortest path, `[a, ..., b]`; empty when *b* is
        unreachable. `[a]` when a == b.

        With a *speed* the route is the QUICKEST one for a mover at that speed
        (PHASE4 T7): a short segment behind a speed limit loses to a longer free
        one. Without a speed -- and on any network that declares no limit at all
        -- it is the shortest by length, exactly as before.
        """
        self._require(a)
        self._require(b)
        if a == b:
            return [a]
        dist, prev = self._tree(a, self._cost_speed(speed))
        if b not in dist:
            return []
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        out.reverse()
        return out

    def path_segments(self, path: list[str], speed=None) -> list[Segment]:
        """The segments a node path crosses, in order. Ties (parallel segments
        between the same pair) go to the cheapest under the same weight the
        route was chosen by, which is the one the route actually used."""
        speed = self._cost_speed(speed)
        out = []
        for a, b in zip(path, path[1:]):
            best = best_cost = None
            for neighbour, _length, seg in self.adjacency.get(a, ()):
                cost = self._weight(seg, speed)
                if neighbour == b and (best is None or cost < best_cost):
                    best, best_cost = seg, cost
            if best is not None:
                out.append(best)
        return out

    def travel_time(self, path: list[str], speed: float) -> float:
        """Time to drive *path* at *speed*, obeying each segment's own limit."""
        if speed <= 0:
            raise ValueError("travel speed must be > 0")
        total = 0.0
        for seg in self.path_segments(path, speed):
            limit = speed if seg.speed is None else min(speed, seg.speed)
            total += seg.length / limit
        return total

    def path_length(self, path: list[str], speed=None) -> float:
        return sum(seg.length for seg in self.path_segments(path, speed))

    def _require(self, node: str) -> None:
        if node not in self.nodes:
            known = ", ".join(sorted(self.nodes)) or "(none)"
            raise KeyError(f"unknown network node '{node}'; declared: {known}")


def build_network(section, units: ModelUnits | None = None) -> Network:
    """Build the run's `Network` from the layout's "network" section.

    Shape faults are validation's job (`validate_network_structured`); this
    normalises and silently drops what cannot be built, so a model that passed
    validation gets exactly what it declared.
    """
    section = section or {}
    units = units or ModelUnits()
    fields = SECTION_FIELDS["network.segments"]
    length_factor = _number(section.get("length_factor"), 1.0)
    nodes: dict[str, tuple[float, float]] = {}
    for entry in section.get("nodes") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if name:
            nodes[name] = (_number(entry.get("x"), 0.0), _number(entry.get("y"), 0.0))

    segments = []
    for index, entry in enumerate(section.get("segments") or []):
        if not isinstance(entry, dict):
            continue
        a, b = str(entry.get("from", "")).strip(), str(entry.get("to", "")).strip()
        if a not in nodes or b not in nodes or a == b:
            continue
        length = entry.get("length")
        if length is None:
            length = _euclidean(nodes[a], nodes[b]) * length_factor
        else:
            length = scaled(float(length), field_factor(param(fields, "length"), entry, units,
                                                        fields))
        speed = entry.get("speed")
        if speed is not None:
            speed = scaled(float(speed), field_factor(param(fields, "speed"), entry, units, fields))
        capacity = entry.get("capacity")
        segments.append(Segment(a, b, float(length), bool(entry.get("two_way", True)),
                                None if speed is None else float(speed),
                                None if capacity is None else int(capacity), index))
    return Network(nodes, segments)


def _euclidean(p: tuple[float, float], q: tuple[float, float]) -> float:
    return math.hypot(q[0] - p[0], q[1] - p[1])


def _number(value, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def point_on_path(network: Network, path, progress: float) -> tuple[float, float]:
    """The point *progress* (0..1) of the way along *path*, by LENGTH.

    A mover crosses a path at one speed, so a fraction of the time is the same
    fraction of the distance: this is the whole of the playback interpolation
    (ARCHITECTURE D68, trace format), and it is a pure function of the trace and
    the network -- no engine state.

    The weights are the SEGMENTS' own lengths, not the drawn distances between
    the nodes: a segment declared longer than it looks (a ramp, a detour) really
    does take proportionally longer, so the marker crosses it more slowly. The
    two agree for every segment that takes its default length.

    The DIRECT case (W3-M7, D118 -- a gantry's hop, or any 2-node path with no
    segment joining them) needs no special branch: with no declared segment to
    weight by, the fallback below is the straight line between the two nodes'
    raw coordinates, which is exactly what a single-hop path should draw.
    """
    path = list(path)
    if not path:
        raise ValueError("point_on_path needs at least one node")
    if len(path) == 1:
        return network.point(path[0])
    segments = [(network.point(a), network.point(b)) for a, b in zip(path, path[1:])]
    declared = network.path_segments(path)
    lengths = ([seg.length for seg in declared] if len(declared) == len(segments)
               else [_euclidean(p, q) for p, q in segments])
    total = sum(lengths)
    if total <= 0:
        return network.point(path[0])
    want = min(max(progress, 0.0), 1.0) * total
    for (p, q), length in zip(segments, lengths):
        if want <= length or length <= 0:
            f = 0.0 if length <= 0 else want / length
            return (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f)
        want -= length
    return network.point(path[-1])


# -- where a block sits ------------------------------------------------------

def node_of_block(block: dict) -> str:
    """The network node a block is attached to, or "" -- the `node` param."""
    return str((block.get("params") or {}).get("node", "") or "").strip()


def block_nodes(layout: dict) -> dict[str, str]:
    """{every name a place answers to: its network node}.

    A STATION contributes its block name, its `station` alias (PHASE4 B6) and,
    through both, the node its `node` param names -- which is what lets
    `DISTANCE("Dock", "Picking")` be written with station names and what lets a
    ROUTE compute its own travel distance. A node is also a place in its own
    right, so a node name maps to itself unless a station claimed it first.
    """
    from nagasim.core.sequences import station_alias

    out: dict[str, str] = {}
    for block in layout.get("blocks", []) or []:
        node = node_of_block(block)
        if not node:
            continue
        name = block.get("name", "")
        if name:
            out[name] = node
        alias = station_alias(block) if str(block.get("type", "")).upper() == "STATION" else ""
        if alias:
            out[alias] = node
    for entry in (layout.get("network") or {}).get("nodes") or []:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            if name:
                out.setdefault(name, name)
    return out


def block_place(params: dict, layout_position, nodes: dict) -> tuple[float, float] | None:
    """Where a block sits in LAYOUT units, or None when it has no place.

    *params* and *layout_position* are the block's own; *nodes* is
    `{name: node record}` of the network section.

    Three sources, in order: the network node it is attached to (moving the
    node moves the block, which is the point of attaching it), the node a
    CONVEYOR runs between -- drawn at the middle of its belt -- and finally
    its own `layout_position`. One rule for the editor's floor plan and the
    HTML player's tape (PHASE7 7.7, D196).
    """
    node = nodes.get(str(params.get("node", "") or ""))
    if node is not None:
        return float(node.get("x", 0.0)), float(node.get("y", 0.0))
    ends = [nodes.get(str(params.get(key, "") or ""))
            for key in ("from_node", "to_node")]
    if all(end is not None for end in ends):
        return (sum(float(e.get("x", 0.0)) for e in ends) / 2,
                sum(float(e.get("y", 0.0)) for e in ends) / 2)
    return tuple(layout_position) if layout_position else None


# -- validation --------------------------------------------------------------

def validate_network_structured(layout: dict) -> tuple[list, set]:
    """Check the "network" section. Returns (list[Problem], {node names})."""
    problems: list = []
    section = layout.get("network") or {}
    if not isinstance(section, dict):
        problems.append(problem("E_NETWORK_SHAPE", "network: must be an object", "/network",
                                hint="write {\"nodes\": [...], \"segments\": [...]}"))
        return problems, set()

    factor = section.get("length_factor", 1.0)
    if not isinstance(factor, (int, float)) or isinstance(factor, bool) or factor <= 0:
        problems.append(problem("E_NETWORK_SHAPE", "network: length_factor must be a number > 0",
                                "/network/length_factor",
                                hint="1.0 is the straight-line distance; 1.2 adds 20%"))

    names: set[str] = set()
    for index, entry in enumerate(section.get("nodes") or []):
        at = f"/network/nodes/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_NETWORK_NODE", f"network node {index} is not an object", at,
                                    hint="a node is {\"name\": \"dock\", \"x\": 0, \"y\": 0}"))
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_NETWORK_NODE", "network node: name must not be empty",
                                    f"{at}/name", hint="give every node a unique name"))
            continue
        name = name.strip()
        if name in names:
            problems.append(problem("E_NETWORK_NODE", f"duplicate network node name: {name}",
                                    f"{at}/name", hint="rename one of the two nodes"))
        names.add(name)
        for axis in ("x", "y"):
            value = entry.get(axis, 0)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(problem(
                    "E_NETWORK_NODE", f"network node '{name}': {axis} must be a number",
                    f"{at}/{axis}", hint="layout coordinates, metres once a scale is set"))
        capacity = entry.get("capacity")
        if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool)
                                     or capacity < 1):
            problems.append(problem(
                "E_NETWORK_NODE", f"network node '{name}': capacity must be an integer >= 1 or null",
                f"{at}/capacity", hint="null means unlimited (the only mode modelled today)"))

    known = ", ".join(sorted(names)) or "(none)"
    for index, entry in enumerate(section.get("segments") or []):
        at = f"/network/segments/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_NETWORK_SEGMENT", f"network segment {index} is not an object",
                                    at, hint="a segment is {\"from\": \"a\", \"to\": \"b\"}"))
            continue
        a, b = entry.get("from", ""), entry.get("to", "")
        for side, value in (("from", a), ("to", b)):
            if not isinstance(value, str) or not value.strip():
                problems.append(problem(
                    "E_NETWORK_SEGMENT", f"network segment {index}: \"{side}\" names no node",
                    f"{at}/{side}", hint=f"name a declared node: {known}"))
            elif value.strip() not in names:
                problems.append(problem(
                    "E_NETWORK_SEGMENT",
                    f"network segment {index}: unknown node '{value}'", f"{at}/{side}",
                    hint=f"add a node named '{value}'; declared: {known}"))
        if isinstance(a, str) and isinstance(b, str) and a.strip() and a.strip() == b.strip():
            problems.append(problem(
                "E_NETWORK_SEGMENT", f"network segment {index}: '{a}' joins itself",
                at, hint="a segment joins two different nodes"))
        length = entry.get("length")
        if length is not None and (not isinstance(length, (int, float))
                                   or isinstance(length, bool) or length <= 0):
            problems.append(problem(
                "E_NETWORK_SEGMENT", f"network segment {index}: length must be a number > 0 or null",
                f"{at}/length", hint="null takes the straight-line distance x length_factor"))
        speed = entry.get("speed")
        if speed is not None and (not isinstance(speed, (int, float))
                                  or isinstance(speed, bool) or speed <= 0):
            problems.append(problem(
                "E_NETWORK_SEGMENT", f"network segment {index}: speed must be a number > 0 or null",
                f"{at}/speed", hint="a speed LIMIT on this segment; null means no limit"))
        capacity = entry.get("capacity")
        if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool)
                                     or capacity < 1):
            problems.append(problem(
                "E_NETWORK_SEGMENT",
                f"network segment {index}: capacity must be an integer >= 1 or null",
                f"{at}/capacity", hint="null means unlimited"))
        elif capacity == 1 and entry.get("two_way", True) is not False and _units(layout) > 1:
            # D117: any two vehicles -- one fleet or two -- can meet head-on here.
            problems.append(problem(
                "W_SEGMENT_DEADLOCK_RISK",
                f"network segment {index}: two-way with capacity 1 on a network "
                f"driven by more than one vehicle; two meeting head-on deadlock",
                f"{at}/capacity",
                hint="make it one-way, give it capacity 2, or add a passing place; "
                     "a run that locks up stops with a deadlock error"))
        if entry.get("two_way") is not None and not isinstance(entry.get("two_way"), bool):
            problems.append(problem(
                "E_NETWORK_SEGMENT", f"network segment {index}: two_way must be a boolean",
                f"{at}/two_way", hint="true (both directions) or false (from -> to only)"))

    problems.extend(_attachment_problems(layout, names, known))
    return problems, names


def _units(layout: dict) -> int:
    """Vehicles ON THE NETWORK, over every fleet (`units` replaces `count`).

    A gantry fleet (W3-M7, D118) never touches a segment, so it never
    contributes to the head-on risk this count feeds."""
    total = 0
    for spec in layout.get("fleets") or []:
        if isinstance(spec, dict) and str(spec.get("motion", "network") or "network") != "gantry":
            units = spec.get("units")
            count = len(units) if isinstance(units, list) and units else spec.get("count", 1)
            total += count if isinstance(count, int) and not isinstance(count, bool) else 1
    return total


def _attachment_problems(layout: dict, names: set, known: str) -> list:
    """A block's `node` param must name a declared node."""
    problems = []
    for index, block in enumerate(layout.get("blocks", []) or []):
        node = node_of_block(block)
        if node and node not in names:
            name = block.get("name", "<unnamed>")
            problems.append(problem(
                "E_NETWORK_NODE_UNKNOWN", f"{name}: unknown network node '{node}'",
                f"/blocks/{index}/params/node", block=name,
                hint=f"add a node named '{node}' under \"network\"; declared: {known}"))
    return problems


def unreachable_pairs(layout: dict, pairs) -> list[tuple[str, str]]:
    """The (from, to) place pairs of *pairs* that no path joins.

    *pairs* is whatever the model actually asks to travel between -- consecutive
    steps of a sequence, a TRANSPORT's declared destination, a literal
    `DISTANCE()` call. Places are resolved through `block_nodes`, so a pair
    naming something with no node at all is simply not a network question and is
    skipped here.
    """
    network = build_network(layout.get("network"), units_of(layout.get("simulation")))
    if not network:
        return []
    places = block_nodes(layout)
    out = []
    for a, b in pairs:
        na, nb = places.get(a), places.get(b)
        if na is None or nb is None or not network.has(na) or not network.has(nb):
            continue
        if math.isinf(network.distance(na, nb)):
            out.append((a, b))
    return out
