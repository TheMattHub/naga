"""The tape of a run (PHASE7 7.7, D197): what a view draws, sampled at a fixed step,
with every coordinate resolved.

A renderer that cannot run the engine (the HTML player) draws from a tape and from
nothing else. Everything in it is read from `Timeline.state_at` and the derivations
beside it (`core/playback.py`): this module reduces no trace event itself
(ARCHITECTURE section 6, rule 13). Between two samples the renderer may only hold a
discrete value and blend a position or a level, so a count is at most one step old.

    run = TapeWriter(layout, "schematic").run(recorder)
    run["scene"]      what does not move: blocks, links, segments, the name tables
    run["t"]          the sample times, duration * k / samples
    run["frames"]     one dict per sample (keys below); names are indices into the scene

Frame keys, each left out when empty:
    n, s    per drawn block: entities in it, and in service
    st      per drawn block: its lamp ("" for none)
    u       {block: [[label, units], ...]}              unit counts
    q       {block: [[type, ...], [type, ...]]}         first ROW waiting, first ROW serving
    r       [[resource, state, busy, capacity], ...]
    lv      {level: value}
    k       [created, completed]
    tok     [[id, type, x, y], ...]                     entities on a delayed link (tokx: the rest)
    veh     [[fleet, unit, x, y, heading, state], ...]
    ag      [[population, index, x, y, colour], ...]    layout view only (agx: the rest)
    mv      [[type, source block, target block, t], ...]   moves since the sample before,
            each at its own time t (mvx: their number, when over MAX_MOVES)
"""
from __future__ import annotations

import math
import re

import nagasim.settings.color_palette as cp
from nagasim.core import playback
from nagasim.core.network import block_place, build_network, point_on_path
from nagasim.core.playback import Timeline
from nagasim.core.seizing import QUEUEING_BLOCK_TYPES

__all__ = ["SAMPLES", "MIN_SAMPLES", "MAX_TOKENS", "MAX_MOVES", "ROW", "VIEWS", "TapeWriter",
           "default_view"]

SAMPLES = 2000          # steps of a tape; a 60 s playback shows about 33 a second
MIN_SAMPLES = 250       # the fewest the size limit may leave (`player.MAX_BYTES`)
MAX_TOKENS = 250        # moving entities, and agents, listed per frame; past it a count
MAX_MOVES = 200         # moves listed per step; past it the badges alone carry the step
ROW = 12                # entities shown in a queue or service row; past it "+N"
VIEWS = ("schematic", "layout")

# A block's box: the schematic's cell in canvas units, a name-tag sized place on the
# floor plan in layout units (metres once the model declares a scale).
_BOX = {"schematic": (120.0, 60.0), "layout": (6.0, 3.0)}
# `instantiate` names a whole-model asset "submodel_N"; its description reads better
# (the rule of the editor's canvas, which the engine cannot import).
_AUTO_NAME = re.compile(r"submodel_\d+")


_STORING = ("BUFFER", "STORE")


def _buffer(block: dict, layout: dict) -> tuple[bool, float | None]:
    """(is *block* one that only holds entities between two others, its capacity when a
    number says it): a BUFFER or STORE, or an asset whose one block past its entry and
    exit is one (its capacity an argument of the asset, or that argument's default)."""
    params = block.get("params") or {}
    kind = str(block.get("type", "")).upper()
    if kind == "SUBMODEL":
        definition = (layout.get("submodels") or {}).get(params.get("definition")) or {}
        inner = [b for b in definition.get("blocks") or ()
                 if str(b.get("type", "")).upper() not in ("SUB_ENTRY", "SUB_EXIT")]
        if len(inner) != 1:
            return False, None
        kind, capacity = str(inner[0].get("type", "")).upper(), (inner[0].get("params") or {}).get("capacity")
        if isinstance(capacity, dict) and "$arg" in capacity:
            defaults = {p.get("name"): p.get("default") for p in definition.get("parameters") or ()}
            capacity = (params.get("arguments") or {}).get(capacity["$arg"], defaults.get(capacity["$arg"]))
    else:
        capacity = params.get("capacity")
    numeric = isinstance(capacity, (int, float)) and not isinstance(capacity, bool) and capacity >= 0
    return kind in _STORING, float(capacity) if numeric else None


def default_view(layout: dict) -> str:
    """"layout" for a model drawn on a floor plan (network nodes, or agents in a
    space), else "schematic"."""
    if (layout.get("network") or {}).get("nodes"):
        return "layout"
    if any(kind.get("space") for kind in layout.get("agent_types") or []):
        return "layout"
    return "schematic"


def _r(value: float) -> float:
    """A coordinate as written: 3 decimals (a millimetre on a floor plan)."""
    return round(float(value), 3)


class _Names:
    """A name table of the scene: frames hold the index, the scene the names."""

    def __init__(self):
        self.names: list = []
        self._index: dict = {}

    def index(self, name) -> int:
        found = self._index.get(name)
        if found is None:
            found = self._index[name] = len(self.names)
            self.names.append(name)
        return found


class TapeWriter:
    """Writes the tape of one run of *layout* as seen in *view*.

    Built once per run: the scene and the lookup maps come from the layout, the
    frames from a `Timeline` asked at rising times (a vehicle keeps the heading it
    last drove with, so the writer remembers one per vehicle)."""

    def __init__(self, layout: dict, view: str):
        if view not in VIEWS:
            raise ValueError(f"unknown view '{view}'; use one of: {', '.join(VIEWS)}")
        self.view = view
        self._floor = view == "layout"
        network_section = layout.get("network") or {}
        nodes = {node.get("name"): node for node in network_section.get("nodes") or []}
        self._network = build_network(network_section)
        width, height = _BOX[view]

        self._blocks: dict[str, int] = {}       # drawn block -> its index
        self._centres: list[tuple[float, float]] = []
        blocks = []
        lamps: dict[str, str] = {}
        for block in layout.get("blocks") or []:
            name = str(block.get("name", ""))
            params = block.get("params") or {}
            if self._floor:
                place = block_place(params, block.get("layout_position"), nodes)
            else:
                place = block.get("position") or (0.0, 0.0)
            if place is None:
                continue        # no place on the floor plan: not drawn
            description = str(block.get("description") or "")
            label = description if description and _AUTO_NAME.fullmatch(name) else name
            self._blocks[name] = len(blocks)
            self._centres.append((float(place[0]), float(place[1])))
            entry = {"name": name, "label": label, "x": _r(place[0]), "y": _r(place[1]),
                     "w": width, "h": height}
            if str(block.get("type", "")).upper() in QUEUEING_BLOCK_TYPES:
                entry["q"] = 1      # badge "serving|waiting"
            storing, capacity = _buffer(block, layout)
            if storing:             # drawn as a tray between its neighbours (#842)
                entry["buf"] = 1
                if capacity is not None:
                    entry["cap"] = capacity
            blocks.append(entry)
            if params.get("resource_name"):
                lamps[name] = str(params["resource_name"])

        links = []
        for link in layout.get("links") or []:
            source, target = ((link.get("source"), link.get("target")) if isinstance(link, dict)
                              else (link[0], link[1]))
            if source in self._blocks and target in self._blocks:
                links.append([self._blocks[source], self._blocks[target]])
        segments = []
        if self._floor:
            for segment in self._network.segments:
                (x0, y0), (x1, y1) = self._network.point(segment.a), self._network.point(segment.b)
                segments.append([_r(x0), _r(y0), _r(x1), _r(y1)])
        self._static = {"view": view, "blocks": blocks, "links": links, "segments": segments}

        self._sources = playback.asset_state_sources(layout)
        self._units = playback.unit_sources(layout, self._sources)
        self._lamps = lamps
        self._node_blocks = playback.node_blocks(layout)
        self._colors = playback.state_colors(layout)
        self._styles = {str(row.get("name", "")).upper():
                        (str(row.get("shape", "circle")).lower(),
                         cp.COLORS.get(str(row.get("color", "BLACK")).upper(), cp.BLACK).lower())
                        for row in layout.get("entities") or []}
        self._capacities = {str(level.get("name", "")): level.get("capacity")
                            for level in layout.get("levels") or []}
        self._types, self._resources, self._levels = _Names(), _Names(), _Names()
        self._fleets, self._pops, self._palette = _Names(), _Names(), _Names()
        self._headings: dict[tuple, float] = {}

    # -- one sample -------------------------------------------------------------------
    def frame(self, timeline: Timeline, t: float, t_prev: float | None = None) -> dict:
        """What to draw at *t*. *t_prev* is the sample before: its moves up to *t*
        are listed with their times, so a renderer can show each one leaving and
        arriving when it happened (#851)."""
        state = timeline.state_at(t)
        counts, serving = state["counts"], state["in_service"]
        names = list(self._blocks)
        frame = {"n": [counts.get(name, 0) for name in names],
                 "s": [serving.get(name, 0) for name in names],
                 "k": [timeline.created_until(t), timeline.disposed_until(t)]}

        resources = state.get("resources") or {}
        lamps = playback.asset_states(state, self._sources)
        shown = [lamps.get(name) or (resources.get(self._lamps.get(name)) or {}).get("state", "")
                 for name in names]
        if any(shown):
            frame["st"] = shown
        units = {str(self._blocks[block]): [[label, count] for label, count in rows]
                 for block, rows in playback.unit_counts(state, self._units).items()
                 if block in self._blocks}
        if units:
            frame["u"] = units
        rows = self._rows(state)
        if rows:
            frame["q"] = rows
        if resources:
            frame["r"] = [[self._resources.index(row["name"]), row["state"], row["busy"],
                           row["capacity"]] for row in playback.resource_rows(state)]
        levels = {str(self._levels.index(name)): round(value, 6)
                  for name, value in (state.get("levels") or {}).items() if value is not None}
        if levels:
            frame["lv"] = levels

        in_transit = state.get("in_transit") or {}
        self._capped(frame, "tok", [entry for entry in (
            self._token(entity, *transit) for entity, transit in sorted(in_transit.items()))
            if entry is not None])
        vehicles = [entry for entry in (
            self._vehicle(key, *mover) for key, mover in sorted((state.get("movers") or {}).items()))
            if entry is not None]
        if vehicles:
            frame["veh"] = vehicles
        if self._floor:
            self._capped(frame, "ag", [
                [self._pops.index(pop), index, _r(x), _r(y),
                 self._color(self._colors.get(category or status))]
                for (pop, index), (x, y, status, category)
                in sorted((state.get("agents") or {}).items())])
        if t_prev is not None:
            moves = [[self._type(e_type), self._blocks[source], self._blocks[target], when]
                     for entity, e_type, source, target, when, _branch
                     in timeline.moves_between(t_prev, t)
                     # A delayed link draws its entity itself (`tok`), not as a hop.
                     if entity not in in_transit and source in self._blocks
                     and target in self._blocks]
            if len(moves) > MAX_MOVES:
                frame["mvx"] = len(moves)
            elif moves:
                frame["mv"] = moves
        return frame

    def _capped(self, frame: dict, key: str, entries: list) -> None:
        if entries:
            frame[key] = entries[:MAX_TOKENS]
        if len(entries) > MAX_TOKENS:
            frame[key + "x"] = len(entries) - MAX_TOKENS

    def _type(self, e_type) -> int:
        return self._types.index(str(e_type).upper())

    def _color(self, color) -> int:
        return -1 if color is None else self._palette.index(color)

    def _rows(self, state: dict) -> dict:
        """{block: [waiting types, serving types]}, the first ROW of each."""
        queues = state.get("queues") or {}
        occupants: dict[str, list] = {}
        types: dict[int, str] = {}
        for entity, (block, e_type) in state["entities"].items():
            if block in self._blocks:
                occupants.setdefault(block, []).append(entity)
                types[entity] = e_type
        out = {}
        for block, inside in occupants.items():
            waiting = queues.get(block, [])
            queued = set(waiting)
            serving = [entity for entity in inside if entity not in queued]
            out[str(self._blocks[block])] = [
                [self._type(types[entity]) for entity in waiting[:ROW] if entity in types],
                [self._type(types[entity]) for entity in serving[:ROW]]]
        return out

    def _between(self, source, target, progress: float):
        """The point *progress* of the way from one drawn block to another."""
        a, b = self._blocks.get(source), self._blocks.get(target)
        if a is None and b is None:
            return None
        (x0, y0), (x1, y1) = self._centres[b if a is None else a], self._centres[a if b is None else b]
        share = min(max(progress, 0.0), 1.0)
        return x0 + (x1 - x0) * share, y0 + (y1 - y0) * share

    def _on_path(self, path, progress: float):
        path = [node for node in path if self._network.has(node)]
        return point_on_path(self._network, path, progress) if path else None

    def _token(self, entity, source, target, progress, e_type, path, _branch):
        point = self._on_path(path, progress) if (self._floor and path) else None
        if point is None:
            if source not in self._blocks or target not in self._blocks:
                return None     # the trace names a block this view does not draw
            point = self._between(source, target, progress)
        return [entity, self._type(e_type), _r(point[0]), _r(point[1])]

    def _vehicle(self, key, path, progress, mover_state):
        heading = None
        if self._floor:
            point = self._on_path(path, progress)
            if point is None:
                return None
            ahead = self._on_path(path, min(progress + 0.01, 1.0))
            if ahead != point:
                heading = math.degrees(math.atan2(ahead[1] - point[1], ahead[0] - point[0]))
        else:
            ends = [self._node_blocks.get(node) for node in (path[0], path[-1])] if path else []
            point = self._between(*ends, progress) if ends else None
            if point is None:
                return None
            a, b = self._between(*ends, 0.0), self._between(*ends, 1.0)
            if a != b and progress < 1.0:
                heading = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        # A vehicle standing still keeps the heading it arrived with.
        if heading is None:
            heading = self._headings.get(key, 0.0)
        self._headings[key] = heading
        return [self._fleets.index(key[0]), key[1], _r(point[0]), _r(point[1]),
                round(heading, 1), mover_state]

    # -- a whole run ------------------------------------------------------------------
    def run(self, trace, samples: int = SAMPLES) -> dict:
        """The tape of *trace* (a `TraceRecorder` or an event list): `samples` equal
        steps from 0 to the last event. A truncated trace ends at its last event."""
        events = getattr(trace, "events", trace)
        timeline = Timeline(events)
        duration = timeline.duration
        steps = max(int(samples), 1) if duration > 0 else 0
        times = [duration * k / steps for k in range(steps)] + [duration]
        frames, previous = [], None
        for t in times:
            frames.append(self.frame(timeline, t, previous))
            previous = t
        return {"scene": self.scene(frames), "t": times, "frames": frames,
                "truncated": bool(getattr(trace, "truncated", False)), "events": len(events)}

    def scene(self, frames: list) -> dict:
        """The scene with the name tables the frames filled, and the box that holds
        everything drawn in them."""
        xs, ys = [], []
        for block in self._static["blocks"]:
            xs += [block["x"] - block["w"] / 2, block["x"] + block["w"] / 2]
            ys += [block["y"] - block["h"] / 2, block["y"] + block["h"] / 2]
        for x0, y0, x1, y1 in self._static["segments"]:
            xs += [x0, x1]
            ys += [y0, y1]
        peaks: dict[str, float] = {}
        for frame in frames:
            for key in ("tok", "veh", "ag"):
                for entry in frame.get(key, ()):
                    xs.append(entry[2])
                    ys.append(entry[3])
            for level, value in frame.get("lv", {}).items():
                peaks[level] = max(peaks.get(level, 0.0), value)
        box = [min(xs), min(ys), max(xs), max(ys)] if xs else [0.0, 0.0, 1.0, 1.0]
        levels = []
        for index, name in enumerate(self._levels.names):
            capacity = self._capacities.get(name)
            numeric = isinstance(capacity, (int, float)) and not isinstance(capacity, bool)
            # A bar needs a full scale: the declared capacity, else the largest sample.
            levels.append({"name": name, "cap": float(capacity) if numeric and capacity > 0
                           else peaks.get(str(index), 0.0)})
        default = ("circle", cp.BLACK.lower())
        types = [dict(zip(("name", "shape", "color"), (name, *self._styles.get(name, default))))
                 for name in self._types.names]
        return {**self._static, "box": [_r(v) for v in box], "types": types,
                "resources": self._resources.names, "levels": levels,
                "fleets": self._fleets.names, "pops": self._pops.names,
                "colors": self._palette.names}
