"""Where a place draws its waiting row, and the one layout check on it (D1089).

An asset's picture says where its queue row starts (`graphics.queue_anchor`, normalised over the
picture) and which way it runs (`graphics.queue_direction`). A placed instance may override both
with its own `queue_row` -- `{"anchor": [x, y], "direction": degrees}`, either key alone -- because
only the author knows which way a row has room to run on a given floor plan. `resolve` is the one
rule every renderer reads (editor canvas, HTML player, 3D view, film tracks); `problems` checks the
override's shape (`E_QUEUE_ROW`) and warns when a drawn row crosses another place
(`W_QUEUE_ROW_CROSSING`). Drawing and validation only: nothing here reaches a run.

Slots (D1113). Any block's `queue_row` may also give the row a fixed number of `slots` -- e.g. a
roller table that holds 3 -- with an optional `label` and the rule for when a slot frees: `frees`
"leave" (default: when the entity leaves the block) or "move_end" (when its timed move out of the
block ends, as a crane lift that holds its spot until the load is set down). The 2D player then
draws the slots, a "label n/N" counter, and an entity past the last slot as an overflow. One rule
counts both (`held_series`, `overflow_instants`): an overflow is an instant at which an entity
enters the block and, after every change of that instant, more entities hold slots than there
are -- the measure a model's own overflow counter usually takes.

The run counts them too, with no trace (E7, D1126): `SlotCounter` is a bus subscriber that
records the same (time, +1/-1) changes as `_changes` reads from a trace, and `overflow_count`
groups them into instants by the same `_grouped` rule, so the "slot_summary" frame and the
player always agree.
"""
from __future__ import annotations

import math

from nagasim.core import network
from nagasim.core.problems import Problem, problem

__all__ = ["KEY", "FREES", "slots_of", "held_series", "overflow_instants", "SlotCounter",
           "overflow_count", "DIRECTIONS", "ANCHOR_RANGE", "ROW_SPACING_M", "ROW_SHOWN", "override_problem",
           "parse_override", "direction_vector", "resolve", "default_anchor", "problems"]

KEY = "queue_row"
# Degrees clockwise on screen from +x (east), in the picture's own unturned frame: the frame
# `layout_rotation` and a seat's facing use, so a turned place turns its row with it.
DIRECTIONS = {"right": 0.0, "down": 90.0, "left": 180.0, "up": 270.0}
ANCHOR_RANGE = (-1.0, 2.0)      # normalised; one footprint beyond the picture each way, as seats
# The row the layout check measures, in metres: the 3D view's spacing and the most it draws.
ROW_SPACING_M = 0.8
ROW_SHOWN = 10
_MIN_OVERLAP_M = 0.05           # a row grazing a neighbour's edge is not a crossing
FREES = ("leave", "move_end")   # when a slot frees (D1113)
_GEOMETRY = {"anchor", "direction"}
_SLOTS = {"slots", "frees", "label"}


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def override_problem(value) -> str | None:
    """Why an instance's `queue_row` is refused, or None when it is fine."""
    if not isinstance(value, dict) or not value:
        return "must be an object with \"anchor\" and/or \"direction\" (or \"slots\")"
    unknown = sorted(set(value) - _GEOMETRY - _SLOTS)
    if unknown:
        return (f"has unknown key '{unknown[0]}' (it takes \"anchor\", \"direction\", \"slots\", "
                "\"frees\" and \"label\")")
    if "slots" in value and not (isinstance(value["slots"], int) and not isinstance(value["slots"], bool)
                                 and value["slots"] >= 1):
        return "slots must be a whole number, 1 or more"
    if ("frees" in value or "label" in value) and "slots" not in value:
        return "frees and label need slots"
    if "frees" in value and value["frees"] not in FREES:
        return f"frees must be one of {', '.join(FREES)}"
    if "label" in value and not isinstance(value["label"], str):
        return "label must be text"
    if "anchor" in value:
        anchor = value["anchor"]
        low, high = ANCHOR_RANGE
        if not (isinstance(anchor, list) and len(anchor) == 2 and all(_number(v) for v in anchor)):
            return "anchor must be [x, y], two numbers"
        if not all(low <= v <= high for v in anchor):
            return f"anchor x and y must lie in {low:g}..{high:g} (normalised over the picture)"
    if "direction" in value:
        direction = value["direction"]
        if not (_number(direction) and 0 <= direction < 360):
            return "direction must be a number of degrees, 0 or more and under 360"
    return None


def parse_override(value) -> dict:
    """An instance's `queue_row` as `{"anchor": (x, y), "direction": degrees}` (either key may be
    missing), or {} when it is absent or malformed -- a refused override draws the asset's own row,
    the same in every view."""
    if value is None or override_problem(value):
        return {}
    out = {}
    if "anchor" in value:
        out["anchor"] = (float(value["anchor"][0]), float(value["anchor"][1]))
    if "direction" in value:
        out["direction"] = float(value["direction"])
    return out


def slots_of(value) -> dict | None:
    """`{"slots": n, "frees": rule, "label": text}` of an instance's `queue_row`, or None when it
    gives no slots or is malformed (D1113)."""
    if not isinstance(value, dict) or "slots" not in value or override_problem(value):
        return None
    return {"slots": value["slots"], "frees": value.get("frees", "leave"), "label": value.get("label", "")}


def _changes(events, block: str, frees: str, until: float = math.inf) -> list[tuple[float, int]]:
    """(time, +1 entry / -1 slot freed) of *block*, from a trace's events, in time order: the
    trace replayed through `SlotCounter`, the run's own counter, so the player and the
    "slot_summary" frame cannot drift apart (D1180). A change after *until*, the run's end, is an
    entry dated at the end of a move the run did not finish (#1733). A trace does not say where
    the run ended (its last event may be earlier: an entity that waits emits nothing), so the
    bound is the caller's; the player shows nothing after the last event anyway."""
    counter = SlotCounter({block: {"slots": 0, "frees": frees, "label": ""}})
    for e in sorted(events, key=lambda e: e.t):
        counter.record(e.kind, e.block, e.entity_id, e.t, e.target, e.duration)
    return sorted((c for c in counter.changes[block] if c[0] <= until), key=lambda c: c[0])


def _instants(events, block: str, frees: str, eps: float, until: float = math.inf):
    return _grouped(_changes(events, block, frees, until), eps)


def _grouped(changes, eps: float, held: int = 0):
    """[(time, slots held after it, an entry in it)] per instant of *changes*, (time, +1/-1)
    pairs in time order, from *held* slots held at the start; changes within *eps* of each other
    are one instant, as a model that lets every event of an instant land first. Within an
    instant the order does not matter: the count after it is their sum."""
    rows = []
    for t, d in changes:
        if rows and t - rows[-1][3] <= eps:
            t0, _h, entry, _last = rows[-1]
            held += d
            rows[-1] = (t0, held, entry or d > 0, t)
        else:
            held += d
            rows.append((t, held, d > 0, t))
    return [(t, h, entry) for t, h, entry, _last in rows]


def held_series(events, block: str, frees: str = "leave", eps: float = 1e-9,
                until: float = math.inf) -> list[tuple[float, int]]:
    """[(time, slots held from then on)] of *block* (D1113) up to *until*: a step series for a
    counter."""
    return [(t, h) for t, h, _entry in _instants(events, block, frees, eps, until)]


def overflow_instants(events, block: str, slots: int, frees: str = "leave",
                      eps: float = 1e-9, until: float = math.inf) -> list[float]:
    """The instants up to *until* at which an entity enters *block* and more than *slots* are
    held after every change of that instant (D1113): one overflow each, however many enter at
    once."""
    return [t for t, h, entry in _instants(events, block, frees, eps, until) if entry and h > slots]


class SlotCounter:
    """The bus subscriber behind "slot_summary" (E7, D1126): per block with `slots`, the
    (time, +1/-1) changes of its slots, recorded as the events are emitted (`__call__`) or
    replayed from a trace (`record`, through `_changes`: one rule for the run and the player,
    D1180). An entity enters at the end of its move in and frees its slot when it leaves, or
    with `move_end` when its timed move out ends, so a change may lie ahead of the clock;
    `overflow_count` sorts them.

    A run started from a state holds the slots of what the state places (`placing`, set by
    `initial_state.apply`) from its start, in `held`: those entities did not enter (#1734). It
    is a count, not a change at the clock, because an arrival due at the clock is a real entry
    (#1754) and a clock of 0 is a state too (#1753)."""

    def __init__(self, rows: dict):
        self.rows = rows                                  # block name -> slots_of(...)
        self.changes = {name: [] for name in rows}
        self.held = {name: 0 for name in rows}
        self.placing = False
        self._inside = {name: set() for name in rows}

    def __call__(self, name, *, block=None, entity=None, t=0.0, target=None, duration=0.0, **_):
        self.record(name, getattr(block, "name", None), entity.id if entity is not None else -1,
                    t, target, duration)

    def record(self, name, here, eid, t, target, duration) -> None:
        if name == "entity_created":
            if here in self.rows:
                self._inside[here].add(eid)
                if self.placing:
                    self.held[here] += 1
                else:
                    self.changes[here].append((t, 1))
        elif name == "entity_moved":
            if not target or target == here:
                return
            if target in self.rows:
                self._inside[target].add(eid)
                self.changes[target].append((t + max(duration, 0.0), 1))
            if here in self.rows and eid in self._inside[here]:
                self._inside[here].discard(eid)
                held = self.rows[here]["frees"] == "move_end" and duration > 0
                self.changes[here].append((t + duration if held else t, -1))
        elif name in ("entity_disposed", "entity_absorbed"):
            if here in self.rows and eid in self._inside[here]:
                self._inside[here].discard(eid)
                self.changes[here].append((t, -1))

    def lifted(self, here, frees) -> None:
        """Slots of *here* held from the start by loads a state has on their way out of it,
        each freed when its timed move ends at a time of *frees* (`frees: move_end`, #1749)."""
        self.held[here] += len(frees)
        self.changes[here].extend((float(t), -1) for t in frees)


def overflow_count(changes, slots: int, since: float = 0.0, eps: float = 1e-9,
                   until: float = math.inf, held: int = 0) -> tuple[int, int]:
    """(entries, overflow instants) from *since* to *until*, both included, among *changes*, with
    *held* slots held from the start (`SlotCounter.held`): the `overflow_instants` rule on a
    `SlotCounter`'s record. A change after *until*, the run's end, is an entry dated at the end
    of a move the run did not finish (#1733)."""
    ordered = sorted((c for c in changes if c[0] <= until), key=lambda c: c[0])
    entries = sum(1 for t, d in ordered if d > 0 and t >= since)
    overflows = sum(1 for t, h, entry in _grouped(ordered, eps, held)
                    if entry and h > slots and t >= since)
    return entries, overflows


def direction_vector(degrees: float) -> tuple[float, float]:
    """The unit step of a row running *degrees* clockwise from +x on screen (+y south); exact on
    quarter turns, so the four words keep the steps they always had."""
    return network.rotate_offset(1.0, 0.0, degrees)


def resolve(graphics, override=None) -> tuple[tuple[float, float], float]:
    """`(anchor, direction degrees)` of a place's queue row: the instance's `queue_row` wins key
    by key over the graphic's `queue_anchor` / `queue_direction`. A row no one gives an anchor
    starts at `default_anchor` -- the one default every view and the layout check share (D1089)."""
    graphics = graphics if isinstance(graphics, dict) else {}
    own = parse_override(override)
    anchor = own.get("anchor")
    if anchor is None:
        raw = graphics.get("queue_anchor")
        if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(_number(v) for v in raw):
            anchor = (float(raw[0]), float(raw[1]))
    direction = own.get("direction")
    if direction is None:
        direction = DIRECTIONS.get(str(graphics.get("queue_direction", "left")), 180.0)
    return (default_anchor(direction) if anchor is None else anchor), direction


def default_anchor(direction: float) -> tuple[float, float]:
    """Where a row with no anchor starts: the middle of the edge it leaves by (the left edge for
    the usual `left`). `resolve` applies it, so no view keeps a default of its own."""
    dx, dy = direction_vector(direction)
    return 0.5 + dx / 2.0, 0.5 + dy / 2.0


# ---------------------------------------------------------------- the layout check
def _queueing_capacity(definition: dict, arguments: dict) -> int | None:
    """The largest literal `queue_capacity` of the definition's queueing blocks, None when any is
    unbounded or not a literal, 0 when the definition has no queueing block (it never queues).
    Only the definition's own `blocks` count: one built by `extends` or `repeat` shows none, so it
    reads as never queuing and its row is not checked.
    A capacity passed in as a parameter (`{"$arg": name}`) reads the instance's literal argument,
    else the parameter's default."""
    from nagasim.core.seizing import QUEUEING_BLOCK_TYPES   # local: keeps this module a leaf

    defaults = {str(p.get("name")): p.get("default") for p in definition.get("parameters") or ()
                if isinstance(p, dict)}
    caps = []
    for block in definition.get("blocks") or ():
        if isinstance(block, dict) and str(block.get("type", "")).upper() in QUEUEING_BLOCK_TYPES:
            cap = (block.get("params") or {}).get("queue_capacity", 0)
            if isinstance(cap, dict) and len(cap) == 1 and "$arg" in cap:
                name = str(cap["$arg"])
                cap = arguments.get(name, defaults.get(name))
            if isinstance(cap, float) and cap.is_integer():
                cap = int(cap)
            if not (isinstance(cap, int) and not isinstance(cap, bool) and cap > 0):
                return None
            caps.append(cap)
    return max(caps) if caps else 0


def _clip_length(a, b, half_w: float, half_h: float) -> float:
    """How long the segment a-b runs inside the box |x| <= half_w, |y| <= half_h (Liang-Barsky)."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 + half_w), (dx, half_w - x0), (-dy, y0 + half_h), (dy, half_h - y0)):
        if p == 0:
            if q < 0:
                return 0.0
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return 0.0
    return (t1 - t0) * math.hypot(dx, dy)


def problems(layout: dict) -> list[Problem]:
    """E_QUEUE_ROW for a malformed `queue_row` on any top-level block, and W_QUEUE_ROW_CROSSING
    for a placed asset whose drawn row -- up to its queue capacity, at most `ROW_SHOWN` places
    `ROW_SPACING_M` apart (seats count against the capacity) -- runs through another placed asset's footprint.
    Top-level blocks only: a place inside a submodel definition is neither checked nor a neighbour."""
    out: list[Problem] = []
    for i, block in enumerate(layout.get("blocks") or ()):
        if isinstance(block, dict) and KEY in block:
            reason = override_problem(block[KEY])
            if reason:
                out.append(problem("E_QUEUE_ROW", f"{block.get('name')}: queue_row {reason}",
                                   f"/blocks/{i}/{KEY}", block=block.get("name"),
                                   hint="{\"anchor\": [0, 0.5], \"direction\": 90}, or remove it "
                                        "to use the asset's own row"))
    definitions = layout.get("submodels") if isinstance(layout.get("submodels"), dict) else {}
    nodes = {n.get("name"): n for n in ((layout.get("network") or {}).get("nodes") or ())
             if isinstance(n, dict)}
    places = []         # (index, name, centre, half sizes, rotation, graphics, definition, override)
    for i, block in enumerate(layout.get("blocks") or ()):
        if not isinstance(block, dict) or str(block.get("type", "")).upper() != "SUBMODEL":
            continue
        params = block.get("params") or {}
        definition = definitions.get(str(params.get("definition", "")))
        if not isinstance(definition, dict):
            continue
        graphics = (definition.get("info") or {}).get("graphics")
        size = graphics.get("size_m") if isinstance(graphics, dict) else None
        if not (isinstance(size, (list, tuple)) and len(size) == 2 and all(_number(v) and v > 0
                                                                            for v in size)):
            continue
        try:
            place = network.block_place(params, block.get("layout_position"), nodes)
        except (TypeError, ValueError):
            continue
        if place is None:
            continue
        places.append((i, str(block.get("name")), (float(place[0]), float(place[1])),
                       (float(size[0]) / 2, float(size[1]) / 2),
                       network.normalise_rotation(block.get("layout_rotation")), graphics,
                       definition, block.get(KEY)))
    for i, name, centre, (hw, hh), rotation, graphics, definition, override in places:
        arguments = (layout["blocks"][i].get("params") or {}).get("arguments")
        capacity = _queueing_capacity(definition, arguments if isinstance(arguments, dict) else {})
        shown = ROW_SHOWN if capacity is None else min(ROW_SHOWN, capacity)
        if capacity is not None:
            seats = graphics.get("queue_slots")
            shown = min(shown, capacity - (len(seats) if isinstance(seats, list) else 0))
        if shown <= 0:
            continue
        anchor, direction = resolve(graphics, override)
        u, v = anchor
        ox, oy = network.rotate_offset((u - 0.5) * 2 * hw, (v - 0.5) * 2 * hh, rotation)
        sx, sy = network.rotate_offset(*direction_vector(direction), rotation)
        start = (centre[0] + ox, centre[1] + oy)
        length = ROW_SPACING_M * shown
        end = (start[0] + sx * length, start[1] + sy * length)
        for _j, other, o_centre, (o_hw, o_hh), o_rotation, *_rest in places:
            if other == name:
                continue
            # A place standing on another (both on one node, not laid out yet) is not a row
            # problem: the pictures already cover each other.
            cx, cy = network.rotate_offset(centre[0] - o_centre[0], centre[1] - o_centre[1],
                                           -o_rotation)
            if abs(cx) < o_hw and abs(cy) < o_hh:
                continue
            # The row in the other place's own frame: there its footprint is an upright box,
            # taken in by the tolerance so a row along a neighbour's edge does not count.
            a, b = ((network.rotate_offset(p[0] - o_centre[0], p[1] - o_centre[1], -o_rotation))
                    for p in (start, end))
            if _clip_length(a, b, o_hw - _MIN_OVERLAP_M, o_hh - _MIN_OVERLAP_M) > _MIN_OVERLAP_M:
                out.append(problem(
                    "W_QUEUE_ROW_CROSSING",
                    f"the queue row of {name} runs through {other} on the floor plan",
                    f"/blocks/{i}", block=name,
                    hint="select the place in the layout view and drag the handle at the end of "
                         "its queue row to turn it, or set Queue row in Properties; the run is "
                         "not affected"))
    return out
