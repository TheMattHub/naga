"""Pallet patterns: where each item of a pallet goes, layer by layer.

A pattern is a record of the model's `pallet_patterns` section: the pallet, the
nominal item, one or more named layer layouts, the order they repeat up the
stack, an optional sheet between layers, and the limits that close a pallet. A
carrier BATCH loads a pallet slot by slot in this order; a keep-mode SEPARATE
unloads it top layer first.

Frames: lengths are in the model's length unit and weights in kg. A slot's `x`
and `y` are the item's centre measured from the pallet's centre, `x` along the
pallet's length, `y` toward the floor plan's +y at rotation 0. `rotation` is in
degrees, clockwise from above as `layout_rotation`; at 0 the item's length runs
along the pallet's length. Stdlib only, but for a robot's cycle per slot (numpy
and the robot plans, imported where they are used).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from nagasim.core.problems import problem
from nagasim.core.utils import cache_key, is_finite_number

__all__ = ["TRANSFORMS", "PLACE_PER", "Slot", "slots", "pattern_records",
           "validate_pallet_patterns", "item_weight", "member_kg", "tare", "weight_limit",
           "sheet_time", "stack_height", "opens_layer", "set_load", "literal_pattern",
           "GENERATED_KINDS", "generate_layer", "interlock", "RESERVED_TARGETS", "slot_cycles",
           "robot_cycles", "robot_phases", "slot_phases", "slot_plans", "slot_meet"]

TRANSFORMS = ("rotate180", "mirror_x", "mirror_y")
PLACE_PER = ("item", "layer")       # a BATCH's or SEPARATE's `place_per`
_TOL = 1e-6
_LIMITS = ("layers", "items", "weight_kg", "height")


@dataclass(frozen=True)
class Slot:
    """One item's place on a pallet: `index` and `layer` 1-based, in load order; `z`
    the item's centre height above the floor under the pallet."""
    index: int
    layer: int
    x: float
    y: float
    z: float
    rotation: float


def _num(value) -> float | None:
    return float(value) if is_finite_number(value) else None


def _part(record: dict, key: str) -> dict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def _limit(record: dict, key: str) -> float | None:
    """A limit, or None when it is not set (a limit of 0 or less is a shape problem)."""
    value = _num(_part(record, "limits").get(key))
    return value if value is not None and value > 0 else None


def _sheet_thickness(record: dict) -> float:
    return max(_num(_part(record, "sheet").get("thickness")) or 0.0, 0.0)


def _transform(x: float, y: float, rotation: float, how: str | None) -> tuple[float, float, float]:
    # A rectangle is the same at r and r + 180, so a mirror is exact on the rotation too.
    if how == "rotate180":
        x, y, rotation = -x, -y, rotation + 180.0
    elif how == "mirror_x":
        x, rotation = -x, 180.0 - rotation
    elif how == "mirror_y":
        y, rotation = -y, -rotation
    return x + 0.0, y + 0.0, rotation % 360.0


def _layer_positions(record: dict, entry) -> list[tuple[float, float, float]]:
    """The (x, y, rotation) of one stack entry's layer, transformed; [] when it is not valid."""
    if not isinstance(entry, dict):
        return []
    layer = (record.get("layers") or {}).get(entry.get("layer")) if isinstance(
        record.get("layers"), dict) else None
    out = []
    for slot in layer if isinstance(layer, list) else []:
        if not isinstance(slot, dict):
            return []
        x, y, r = _num(slot.get("x")), _num(slot.get("y")), _num(slot.get("rotation", 0) or 0)
        if x is None or y is None or r is None:
            return []
        out.append(_transform(x, y, r, entry.get("transform")))
    return out


def slots(record: dict) -> list[Slot]:
    """Every slot of *record* in load order, up to its `layers`, `items` and `height`
    limits: a layer whose top would pass `height` is left out. Weight is the run's to
    check. [] for a record without a stack, an item height, or a limit that ends it."""
    stack = record.get("stack") if isinstance(record.get("stack"), list) else []
    height = _num(_part(record, "item").get("height"))
    deck = _num(_part(record, "pallet").get("deck_height", 0) or 0)
    layers_max, items_max, top_max = (_limit(record, k) for k in ("layers", "items", "height"))
    positions = [_layer_positions(record, entry) for entry in stack]
    if not stack or height is None or height <= 0 or deck is None \
            or not any(positions) or (layers_max is None and items_max is None and top_max is None):
        return []
    sheet = _sheet_thickness(record)
    out: list[Slot] = []
    layer = 0
    while True:
        layer += 1
        top = deck + layer * height + (layer - 1) * sheet
        if (layers_max is not None and layer > layers_max) or (
                top_max is not None and top > top_max + _TOL):
            return out
        z = deck + (layer - 1) * (height + sheet) + height / 2.0
        for x, y, r in positions[(layer - 1) % len(positions)]:
            if items_max is not None and len(out) >= items_max:
                return out
            out.append(Slot(len(out) + 1, layer, x, y, z, r))


def pattern_records(layout: dict) -> dict[str, dict]:
    """{name: record} of the `pallet_patterns` section, the first of a repeated name."""
    out: dict[str, dict] = {}
    for record in layout.get("pallet_patterns") or []:
        if isinstance(record, dict) and str(record.get("name", "")).strip():
            out.setdefault(str(record["name"]).strip(), record)
    return out


def item_weight(record: dict) -> float:
    """The pattern's nominal item weight in kg, 0 when it does not say."""
    return max(_num(_part(record, "item").get("weight_kg")) or 0.0, 0.0)


def member_kg(member, record: dict) -> float:
    """A loaded item's weight in kg: its own `weight` attribute, else the pattern's item's."""
    own = _num(getattr(member, "weight", None))
    return own if own is not None else item_weight(record)


def tare(record: dict) -> float:
    return max(_num(_part(record, "pallet").get("tare_kg")) or 0.0, 0.0)


def weight_limit(record: dict) -> float | None:
    return _limit(record, "weight_kg")


def sheet_time(record: dict) -> float:
    """The time to place a sheet between layers, in the model's time unit."""
    return max(_num(_part(record, "sheet").get("time")) or 0.0, 0.0)


def stack_height(record: dict, layers: int) -> float:
    """From the floor to the top of *layers* layers: the deck, the items, the sheets between."""
    height = _num(_part(record, "item").get("height")) or 0.0
    deck = _num(_part(record, "pallet").get("deck_height", 0) or 0) or 0.0
    return deck + layers * height + max(layers - 1, 0) * _sheet_thickness(record)


def opens_layer(places: list[Slot], slot: Slot) -> bool:
    """True when *slot* is the first of its layer in *places* (the pattern's slots)."""
    return slot.index == 1 or places[slot.index - 2].layer != slot.layer


def set_load(carrier, record: dict, load_weight: float) -> None:
    """The pallet's running attributes, after each load or unload."""
    carrier.items = len(carrier.batch_elems)
    carrier.layers = max((getattr(m, "layer", 0) or 0 for m in carrier.batch_elems), default=0)
    carrier.load_weight = load_weight
    carrier.load_height = stack_height(record, carrier.layers)


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def literal_pattern(text: str, names=()) -> str | None:
    """The record a BATCH's `pattern` names outright: a quoted name, a record's name, or a
    bare word (no expression gives a name from a bare word); None for an expression, which
    chooses the pattern per pallet."""
    text = str(text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" and text[0] not in text[1:-1]:
        return text[1:-1]
    return text if text in names or _WORD.match(text) else None


# -- geometry of the checks --------------------------------------------------------------
def _corners(x: float, y: float, rotation: float, length: float, width: float):
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    a, b = length / 2.0, width / 2.0
    return [(x + c * dx - s * dy, y + s * dx + c * dy)
            for dx, dy in ((a, b), (-a, b), (-a, -b), (a, -b))]


def _overlap(p: list, q: list) -> bool:
    """Two convex quadrilaterals overlap by more than the tolerance (separating axes)."""
    for poly in (p, q):
        for i in range(4):
            (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % 4]
            nx, ny = y0 - y1, x1 - x0
            norm = math.hypot(nx, ny)
            nx, ny = nx / norm, ny / norm
            a = [nx * x + ny * y for x, y in p]
            b = [nx * x + ny * y for x, y in q]
            if min(max(a), max(b)) - max(min(a), min(b)) <= _TOL:
                return False
    return True


def _past_edge(corners: list, length: float, width: float) -> float:
    """How far *corners* reach past the pallet's edge (0 when inside)."""
    return max(0.0, max(abs(x) for x, _ in corners) - length / 2.0,
               max(abs(y) for _, y in corners) - width / 2.0)


# -- the section's checks ----------------------------------------------------------------
def validate_pallet_patterns(layout: dict, problems: list) -> None:
    """The `pallet_patterns` section's hook (core/sections.py): each record's shape,
    then per layer its overlaps and overhangs, then whether the stack can close."""
    seen: set[str] = set()
    for index, record in enumerate(layout.get("pallet_patterns") or []):
        at = f"/pallet_patterns/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""
        if not name or name in seen:
            problems.append(problem("E_PALLET_SHAPE", f"pallet pattern {index}: empty or "
                                    f"duplicate name '{name}'", f"{at}/name",
                                    hint="give every pallet pattern its own name",
                                    fields={"case": "name", "index": index, "name": name}))
            continue
        seen.add(name)
        if _shape_problems(record, name, at, problems):
            continue
        _layout_problems(record, name, at, problems)
        _close_problems(record, name, at, problems)
    _block_problems(layout, problems)


def _block_problems(layout: dict, problems: list) -> None:
    """A carrier BATCH's pattern named outright must be a record (an expression is the run's);
    a robot BATCH's or SEPARATE's cycle is planned per slot."""
    records = pattern_records(layout)
    for index, block in enumerate(layout.get("blocks") or []):
        params = block.get("params") if isinstance(block, dict) else None
        kind = str(block.get("type", "")).upper() if isinstance(block, dict) else ""
        if kind == "SEPARATE" and isinstance(params, dict) and params.get("robot"):
            _robot_problems(layout, block, index, records, problems)
        if kind != "BATCH" or not isinstance(params, dict) \
                or not str(params.get("carrier_type") or "").strip():
            continue
        if params.get("robot"):
            _robot_problems(layout, block, index, records, problems)
        name = literal_pattern(params.get("pattern"), records)
        if name is not None and name not in records:
            who = str(block.get("name", ""))
            problems.append(problem(
                "E_PALLET_UNKNOWN", f"{who}: unknown pallet pattern '{name}'",
                f"/blocks/{index}/params/pattern", block=who,
                hint="declare it under \"pallet_patterns\"; declared: "
                     f"{', '.join(sorted(records)) or '(none)'}",
                fields={"case": "unknown", "pattern": name}))


def _robot_problems(layout: dict, block: dict, index: int, records: dict, problems: list) -> None:
    """A robot palletizer's (or depalletizer's) checks: the pattern named, the reserved
    targets, its segments, its robot, then each slot's plan (the first failing slot per code)."""
    from nagasim.core import motion
    who, at = str(block.get("name", "")), f"/blocks/{index}/params"
    params = block.get("params") or {}

    def say(code, text, path, hint, fields=None):
        problems.append(problem(code, f"{who}: {text}", f"{at}/{path}", block=who, hint=hint,
                                fields=fields))

    name = literal_pattern(params.get("pattern"), records)
    if name is None:
        say("E_PALLET_UNKNOWN", "a robot's cycle is planned per slot before the run, so its "
            "pattern must be named, not chosen per pallet", "pattern",
            "name one record of \"pallet_patterns\"", fields={"case": "robot"})
        return
    if name not in records:
        if str(block.get("type", "")).upper() == "SEPARATE":       # a BATCH says it below
            say("E_PALLET_UNKNOWN", f"unknown pallet pattern '{name}'", "pattern",
                "declare it under \"pallet_patterns\"; declared: "
                f"{', '.join(sorted(records)) or '(none)'}",
                fields={"case": "unknown", "pattern": name})
        return
    targets = params.get("targets") if isinstance(params.get("targets"), dict) else {}
    for reserved in RESERVED_TARGETS:
        if reserved in targets:
            say("E_ROBOT_TARGET", f"target '{reserved}' is the item's own slot and is not "
                "defined in `targets`", f"targets/{reserved}", "rename the target; the cycle "
                "names slot and slot_above for each slot", fields={"case": "reserved",
                                                                   "target": reserved})
    segments = [s for s in params.get("segments") or [] if isinstance(s, dict)]

    if not segments:
        say("E_MOTION_SEGMENT", "the robot's cycle has no segments", "segments",
            "add at least one segment")
    for k, segment in enumerate(segments):
        motion.validate_segment(segment, k, None, say, robot_kinds=True)
    if not motion.robot_named(say, str(params.get("robot") or ""), motion.robot_records(layout),
                              segments):
        return
    cycles, errors = slot_cycles(layout, block)
    first: dict = {}
    for slot, err in errors:
        first.setdefault(err.code, (slot, err))
    for slot, err in first.values():
        motion.plan_problems(say, [err], prefix=f"slot {slot}: ")
    if not errors and any(c is None for c in cycles):
        say("E_MOTION_SEGMENT", "a timed segment of the robot's cycle cannot be evaluated "
            "before the run", "segments", "give pick, place and the other timed segments a "
            "number or an expression that reads no entity")


def _shape_problems(record: dict, name: str, at: str, problems: list) -> bool:
    """Missing or non-positive dimensions, a bad slot, an unknown layer or transform."""
    before = len(problems)

    def bad(path, text, hint):
        problems.append(problem("E_PALLET_SHAPE", f"pallet pattern '{name}': {text}",
                                f"{at}/{path}", hint=hint, fields={"case": "said"}))

    for part, fields in (("pallet", ("length", "width")), ("item", ("length", "width", "height"))):
        for field in fields:
            value = _num(_part(record, part).get(field))
            if value is None or value <= 0:
                bad(f"{part}/{field}", f"{part}.{field} must be a number > 0",
                    "in the model's length unit")
    for part, field in (("pallet", "deck_height"), ("pallet", "tare_kg"), ("item", "weight_kg"),
                        ("sheet", "thickness"), ("sheet", "time")):
        value = _part(record, part).get(field)
        if value is not None and (_num(value) is None or value < 0):
            bad(f"{part}/{field}", f"{part}.{field} must be a number >= 0", "clear it, or give "
                "a number of 0 or more")
    if record.get("overhang") is not None and (_num(record.get("overhang")) is None
                                               or record["overhang"] < 0):
        bad("overhang", "overhang must be a number >= 0", "how far an item may pass the "
            "pallet's edge, in the model's length unit")
    for key in _LIMITS:
        value = _part(record, "limits").get(key)
        if value is not None and (_num(value) is None or value <= 0):
            bad(f"limits/{key}", f"limits.{key} must be a number > 0", "clear it for no limit")
    layers = record.get("layers")
    if not isinstance(layers, dict) or not layers:
        bad("layers", "layers must name at least one layer", "e.g. {\"A\": [{\"x\": 0, \"y\": 0, "
            "\"rotation\": 0}]}")
        layers = {}
    for layer, positions in layers.items():
        ok = isinstance(positions, list) and all(
            isinstance(s, dict) and _num(s.get("x")) is not None and _num(s.get("y")) is not None
            and _num(s.get("rotation", 0) or 0) is not None for s in positions)
        if not ok:
            bad(f"layers/{layer}", f"layer '{layer}' must be a list of slots {{x, y, rotation?}}",
                "x and y in the model's length unit from the pallet's centre, rotation in degrees")
    stack = record.get("stack")
    for k, entry in enumerate(stack if isinstance(stack, list) else []):
        layer = entry.get("layer") if isinstance(entry, dict) else None
        if layer not in layers:
            problems.append(problem("E_PALLET_SHAPE", f"pallet pattern '{name}': stack entry {k} "
                                    f"names no layer '{layer}'", f"{at}/stack/{k}/layer",
                                    hint=f"one of: {', '.join(map(str, layers)) or '(none)'}",
                                    fields={"case": "layer", "name": name, "layer": str(layer)}))
        elif entry.get("transform") is not None and entry["transform"] not in TRANSFORMS:
            problems.append(problem("E_PALLET_SHAPE", f"pallet pattern '{name}': stack entry {k} "
                                    f"has an unknown transform '{entry['transform']}'",
                                    f"{at}/stack/{k}/transform",
                                    hint=f"one of: {', '.join(TRANSFORMS)}",
                                    fields={"case": "transform", "name": name,
                                            "transform": str(entry["transform"])}))
    return len(problems) > before


def _layout_problems(record: dict, name: str, at: str, problems: list) -> None:
    pallet, item = _part(record, "pallet"), _part(record, "item")
    length, width = float(pallet["length"]), float(pallet["width"])
    allowed = _num(record.get("overhang")) or 0.0
    for layer, positions in record["layers"].items():
        rects = [_corners(float(s["x"]), float(s["y"]), float(s.get("rotation", 0) or 0),
                          float(item["length"]), float(item["width"])) for s in positions]
        for i, a in enumerate(rects):
            b = next((j for j in range(i + 1, len(rects)) if _overlap(a, rects[j])), None)
            if b is not None:
                problems.append(problem(
                    "E_PALLET_OVERLAP", f"pallet pattern '{name}': layer '{layer}' slots {i + 1} "
                    f"and {b + 1} overlap", f"{at}/layers/{layer}/{b}",
                    hint="move or turn one of them; the item is item.length along x at rotation 0",
                    fields={"name": name, "layer": str(layer), "a": i + 1, "b": b + 1}))
                break
        for i, corners in enumerate(rects):
            by = _past_edge(corners, length, width)
            if by > allowed + _TOL:
                problems.append(problem(
                    "E_PALLET_OVERHANG", f"pallet pattern '{name}': layer '{layer}' slot {i + 1} "
                    f"passes the pallet's edge by {by:.4g}, more than the overhang "
                    f"{allowed:g}", f"{at}/layers/{layer}/{i}",
                    hint="move the slot in, or allow more overhang",
                    fields={"name": name, "layer": str(layer), "slot": i + 1,
                            "by": round(by, 4)}))
                break


def _close_problems(record: dict, name: str, at: str, problems: list) -> None:
    if not record.get("stack"):
        problems.append(problem("E_PALLET_EMPTY", f"pallet pattern '{name}': the stack is empty",
                                f"{at}/stack", hint="list the layers up the pallet, e.g. "
                                "[{\"layer\": \"A\"}]", fields={"case": "stack", "name": name}))
    elif all(_limit(record, k) is None for k in ("layers", "items", "height")):
        problems.append(problem("E_PALLET_EMPTY", f"pallet pattern '{name}': no limit of layers, "
                                f"items or height closes the pallet", f"{at}/limits",
                                hint="set limits.layers, limits.items or limits.height",
                                fields={"case": "limit", "name": name}))
    elif not slots(record):
        problems.append(problem("E_PALLET_EMPTY", f"pallet pattern '{name}': no slot fits under "
                                f"the limits", f"{at}/limits", hint="raise the height limit, or "
                                "give the layers slots", fields={"case": "none", "name": name}))
    else:
        weight = weight_limit(record)
        if weight is not None and tare(record) + item_weight(record) > weight + _TOL:
            problems.append(problem("E_PALLET_EMPTY", f"pallet pattern '{name}': the tare and "
                                    f"one item already pass the weight limit {weight:g} kg",
                                    f"{at}/limits/weight_kg", hint="raise limits.weight_kg",
                                    fields={"case": "weight", "name": name}))


# -- the layer generator -----------------------------------------------------------------
GENERATED_KINDS = ("column", "block", "best")


def _fit(extent: float, side: float, gap: float) -> int:
    """How many items of *side* fit in a row of *extent*, *gap* between two."""
    return max(int(math.floor((extent + gap) / (side + gap) + 1e-9)), 0) if side > 0 else 0


def _grid(x0: float, y0: float, nx: int, ny: int, dx: float, dy: float, gap: float,
          rotation: float) -> list[dict]:
    """*nx* by *ny* items of footprint *dx* x *dy* from the corner (x0, y0), row by row."""
    return [{"x": x0 + dx / 2 + i * (dx + gap), "y": y0 + dy / 2 + j * (dy + gap),
             "rotation": rotation} for j in range(ny) for i in range(nx)]


def _span(n: int, side: float, gap: float) -> float:
    return n * side + max(n - 1, 0) * gap if n else 0.0


def _layout_result(kind: str, placed: list[dict], length: float, width: float,
                   item_length: float, item_width: float) -> dict:
    over = max((_past_edge(_corners(s["x"], s["y"], s["rotation"], item_length, item_width),
                           length, width) for s in placed), default=0.0)
    for s in placed:
        s["x"], s["y"] = round(s["x"], 9) + 0.0, round(s["y"], 9) + 0.0
    return {"kind": kind, "slots": placed, "items": len(placed),
            "area_use": round(len(placed) * item_length * item_width / (length * width), 9),
            "overhang": round(over, 9)}


def _column(L: float, W: float, a: float, b: float, gap: float) -> list[list[dict]]:
    """The two plain grids, centred: item length along x (0 degrees), then along y (90)."""
    out = []
    for dx, dy, rotation in ((a, b, 0.0), (b, a, 90.0)):
        nx, ny = _fit(L, dx, gap), _fit(W, dy, gap)
        out.append(_grid(-_span(nx, dx, gap) / 2, -_span(ny, dy, gap) / 2, nx, ny, dx, dy, gap,
                         rotation) if nx and ny else [])
    return out


def _blocks(L: float, W: float, a: float, b: float, gap: float) -> list[list[dict]]:
    """Two regions split once across x or across y at every multiple of an item side,
    the items at 0 degrees in one and 90 in the other; each layout centred."""
    out = []
    feet = ((a, b, 0.0), (b, a, 90.0))
    for first, second in (feet, feet[::-1]):
        # Split across x: the first region's columns, then the second's.
        for n1 in range(_fit(L, first[0], gap) + 1):
            used1 = _span(n1, first[0], gap)
            rest = L - used1 - (gap if n1 else 0.0)
            n2 = _fit(rest, second[0], gap)
            m1, m2 = _fit(W, first[1], gap), _fit(W, second[1], gap)
            total = used1 + (gap if n1 and n2 else 0.0) + _span(n2, second[0], gap)
            x0 = -total / 2
            placed = _grid(x0, -_span(m1, first[1], gap) / 2, n1, m1, first[0], first[1], gap,
                           first[2]) if n1 and m1 else []
            x1 = x0 + used1 + (gap if n1 and n2 else 0.0)
            placed += _grid(x1, -_span(m2, second[1], gap) / 2, n2, m2, second[0], second[1],
                            gap, second[2]) if n2 and m2 else []
            out.append(placed)
        # Split across y: the first region's rows, then the second's.
        for m1 in range(_fit(W, first[1], gap) + 1):
            used1 = _span(m1, first[1], gap)
            rest = W - used1 - (gap if m1 else 0.0)
            m2 = _fit(rest, second[1], gap)
            n1, n2 = _fit(L, first[0], gap), _fit(L, second[0], gap)
            total = used1 + (gap if m1 and m2 else 0.0) + _span(m2, second[1], gap)
            y0 = -total / 2
            placed = _grid(-_span(n1, first[0], gap) / 2, y0, n1, m1, first[0], first[1], gap,
                           first[2]) if n1 and m1 else []
            y1 = y0 + used1 + (gap if m1 and m2 else 0.0)
            placed += _grid(-_span(n2, second[0], gap) / 2, y1, n2, m2, second[0], second[1],
                            gap, second[2]) if n2 and m2 else []
            out.append(placed)
    return out


def generate_layer(pallet_length: float, pallet_width: float, item_length: float,
                   item_width: float, *, overhang: float = 0.0, gap: float = 0.0,
                   kind: str = "best") -> dict:
    """One layer of equal items on a pallet, as a pattern's slots (centred on the pallet).

    "column": the better of the two plain grids (item length along the pallet's length, or
    across it). "block": the pallet split once, along its length or its width, with the
    items turned 90 degrees in one part, which packs more when neither grid fills it.
    "best": the layout with the most items, ties to the smaller overhang, then to column.
    *overhang* is how far an item may pass the pallet's edge, *gap* the space between two
    items. Returns {"kind", "slots", "items", "area_use", "overhang"}."""
    if kind not in GENERATED_KINDS:
        raise ValueError(f"kind must be one of {', '.join(GENERATED_KINDS)}, not {kind!r}")
    sizes = (pallet_length, pallet_width, item_length, item_width)
    if any(_num(v) is None or v <= 0 for v in sizes) or _num(overhang) is None or overhang < 0 \
            or _num(gap) is None or gap < 0:
        raise ValueError("the pallet and item sizes must be numbers > 0, overhang and gap >= 0")
    L, W = pallet_length + 2 * overhang, pallet_width + 2 * overhang
    a, b = item_length, item_width
    makers = {"column": _column, "block": _blocks}
    results = [_layout_result(k, placed, pallet_length, pallet_width, a, b)
               for k in (("column", "block") if kind == "best" else (kind,))
               for placed in makers[k](L, W, a, b, gap)]
    # Most items, then the smaller overhang; min() keeps the first of a tie, column first.
    return min(results, key=lambda r: (-r["items"], r["overhang"]))


def interlock(slots: list[dict]) -> list[dict]:
    """The `stack` of an interlocked pallet of layer "A": A, then A turned 180 degrees; or A
    mirrored across x when turning it gives the same layer (a symmetric grid)."""
    def key(placed):
        return sorted((round(s["x"], 6) + 0.0, round(s["y"], 6) + 0.0,
                       round(float(s.get("rotation", 0) or 0) % 180.0, 6)) for s in placed)

    turned = [dict(zip(("x", "y", "rotation"), _transform(
        s["x"], s["y"], float(s.get("rotation", 0) or 0), "rotate180"))) for s in slots]
    how = "mirror_x" if key(turned) == key(slots) else "rotate180"
    return [{"layer": "A"}, {"layer": "A", "transform": how}]


# -- a robot's cycle per slot ------------------------------------------------------------
# The targets a palletizing or depalletizing cycle names for the item's own slot, per slot.
RESERVED_TARGETS = ("slot", "slot_above")
_APPROACH_M = 0.3
# Canonical inputs -> (cycles, errors, meet, phases, plans); the plans are deterministic. Kept here as well as in
# motion's per-cycle plan cache, whose 256 entries a few large patterns would churn; bounded
# so an editing session that moves blocks does not grow it.
_CYCLES: dict = {}
_CYCLES_KEPT = 64


def robot_block_pattern(layout: dict, block: dict) -> str | None:
    """The pattern a robot BATCH or SEPARATE is planned for: its `pattern` named outright."""
    return literal_pattern((block.get("params") or {}).get("pattern"), pattern_records(layout))


def robot_at_offset(params: dict) -> tuple[float, float, float]:
    """A robot BATCH's `robot_at` as (dx, dy, rotation) in its block's own frame, in the
    model's length unit; anything but three finite numbers is no offset. The engine's one
    reading, which the 3D view shares."""
    at = (params or {}).get("robot_at") or [0, 0, 0]
    if isinstance(at, (list, tuple)) and len(at) == 3 and all(_num(v) is not None for v in at):
        return float(at[0]), float(at[1]), float(at[2])
    return 0.0, 0.0, 0.0


def _robot_frame(layout: dict, block: dict, to_m: float):
    """The robot's cell pose: the block's place moved by `robot_at` ([dx, dy, rotation] in
    the block's own frame, in the model's length unit); None when the block has no place."""
    import numpy as np
    from nagasim.core.robotics.moves import cell_pose, robot_base
    base = robot_base(layout, str(block.get("name", "")))
    if base is None:
        return None
    dx, dy, turn = robot_at_offset(block.get("params") or {})
    return base @ np.asarray(cell_pose(dx * to_m, dy * to_m, turn), dtype=float)


def slot_cycles(layout: dict, block: dict) -> tuple[list, list]:
    """A robot BATCH's (or SEPARATE's) cycle time in SECONDS per slot of its pattern, and
    the plan errors as [(slot index, RobotMoveError)]: its `segments` planned once per
    slot, the targets `slot` and `slot_above` (`approach` higher) on that slot's top face,
    the robot at `robot_at` from the block. A move without its own `payload_kg` carries the
    pattern's item weight (else the robot's rated payload). A cycle is None when a timed
    segment cannot be evaluated before the run. ([], []) without a robot or a pattern.

    A BATCH whose segment 0 picks from a moving belt meets the part at one point for every
    slot: its cycle ends at a fixed target, and it places one box at a time."""
    cycles, errors = _slot_plans(layout, block)[:2]
    return cycles, errors


def slot_meet(layout: dict, block: dict) -> tuple[float, float] | None:
    """(approach seconds, meeting point in metres along the belt) of a robot BATCH whose
    segment 0 picks from a moving belt, from its first slot's plan; None otherwise or when
    the plan has errors."""
    return _slot_plans(layout, block)[2]


def slot_phases(layout: dict, block: dict) -> list:
    """Per slot of `slot_cycles`, the ((segment kind, seconds), ...) of that slot's plan, the
    seconds summing to its cycle; None for a slot whose cycle is None."""
    return _slot_plans(layout, block)[3]


def slot_plans(layout: dict, block: dict) -> list:
    """Per slot of `slot_cycles`, in slot order, that slot's planned cycle
    (`robotics.moves.CyclePlan`), which a viewer poses the robot from; None for a slot whose
    plan has errors. [] without a robot or a pattern."""
    return _slot_plans(layout, block)[4]


def _slot_plans(layout: dict, block: dict) -> tuple[list, list, tuple | None, list, list]:
    from nagasim.core import motion
    from nagasim.core.beltpick import first_belt_target
    from nagasim.core.robotics.moves import at_segment, intercept_error, places_of
    from nagasim.core.robotics.records import robot_from_record
    from nagasim.core.timeunits import conversion_factor
    params = block.get("params") or {}
    records, robots = pattern_records(layout), motion.robot_records(layout)
    record = records.get(robot_block_pattern(layout, block) or "")
    robot = robots.get(str(params.get("robot") or ""))
    if record is None or robot is None:
        return [], [], None, [], []
    sim = layout.get("simulation") or {}
    try:
        to_m = conversion_factor("length", str(sim.get("length_unit") or "meters"), "meters")
        robot_from_record(robot)
    except (KeyError, ValueError, TypeError):
        return [], [], None, [], []  # an unknown unit or a bad robot: their own checks say why
    name = str(block.get("name", ""))
    places = places_of(layout)
    base = _robot_frame(layout, block, to_m)
    targets = params.get("targets") if isinstance(params.get("targets"), dict) else {}
    # Depalletizing puts boxes onto belts, not off them: a SEPARATE has no belt pick, so its
    # plan refuses a belt target (E_ROBOT_INTERCEPT case `belt`).
    belts = {} if str(block.get("type", "")).upper() == "SEPARATE" else motion.belt_picks(
        layout, targets, places, name.rsplit("/", 1)[0] + "/" if "/" in name else "")
    key = cache_key([record, robot, params, name, sim.get("time_unit"),
                             sim.get("length_unit"), None if base is None else base.tolist(),
                             {k: [p.x, p.y, p.rotation, p.length] for k, p in places.items()},
                             motion.belts_key(belts)])
    if key in _CYCLES:
        return _CYCLES[key]
    try:
        approach = float(params.get("approach")) * to_m
    except (TypeError, ValueError):
        approach = math.nan
    if not math.isfinite(approach):
        approach = _APPROACH_M          # empty, or not a number

    payload = item_weight(record) or robot_from_record(robot).payload_max or 0.0
    segments = [{**s, "payload_kg": payload} if s.get("kind") in motion.ROBOT_KINDS
                and "payload_kg" not in s else s
                for s in params.get("segments") or [] if isinstance(s, dict)]
    height = float(_part(record, "item")["height"])
    unit = "seconds"        # a robot BATCH's or SEPARATE's segment times are in seconds
    model_unit = str(sim.get("time_unit") or "minutes")
    cycles, errors, meets, phases, plans = [], [], [], [], []
    pick = None if str(block.get("type", "")).upper() == "SEPARATE" else first_belt_target(params)
    picks, belt = pick is not None, (pick or {}).get("belt")
    first = slots(record)[:1]
    if picks and first:
        moves = [k for k, s in enumerate(segments) if s.get("kind") in motion.ROBOT_KINDS]
        if moves and segments[moves[-1]].get("to") in RESERVED_TARGETS:
            # Each slot's cycle would start somewhere else, and the meeting point move with it.
            errors.append((first[0].index, at_segment(intercept_error(
                "start", "a cycle that picks from a moving belt must end at a fixed target, "
                "not at slot or slot_above", belt), moves[-1], segments[moves[-1]].get("to"))))
        if str(params.get("place_per") or "item") == "layer":
            errors.append((first[0].index, at_segment(intercept_error(
                "layer", "a pick from a moving belt places one box at a time; place_per must "
                "be item", belt), 0, segments[0].get("to"))))
    for slot in slots(record):
        top = (slot.z + height / 2.0) * to_m
        here = {"on": name, "offset": [slot.x * to_m, slot.y * to_m, top], "yaw_deg": slot.rotation}
        own = {**targets, "slot": here,
               "slot_above": {**here, "offset": [slot.x * to_m, slot.y * to_m, top + approach]}}
        plan = motion.block_robot_plan(
            layout, {"name": name, "params": {"robot": params.get("robot"), "targets": own,
                                              "segments": segments, "time_unit": unit}},
            robots, places, base=base, belts=belts)
        errors += [(slot.index, err) for err in plan.errors]
        plans.append(None if plan.errors else plan)
        cycles.append(motion.nominal_cycle_seconds(segments, None, unit, model_unit, plan)
                      if not plan.errors else None)
        seconds = motion.nominal_segment_seconds(segments, None, unit, model_unit, plan) \
            if cycles[-1] is not None else None
        phases.append(None if seconds is None else tuple(
            (str(s.get("kind")), t) for s, t in zip(segments, seconds)))
        if picks and not plan.errors and plan.moves.get(0) is not None:
            meets.append((plan.moves[0].seconds, plan.moves[0].meet))
    meet = meets[0] if picks and not errors and meets else None
    if meet is not None and any(abs(a - meet[0]) > 1e-6 or abs(m - meet[1]) > 1e-6
                                for a, m in meets):
        errors.append((first[0].index, at_segment(intercept_error(
            "start", "every slot's cycle must start from one pose to meet the part at one point",
            belt), 0, segments[0].get("to"))))
        meet = None
    if len(_CYCLES) >= _CYCLES_KEPT:
        _CYCLES.clear()
    _CYCLES[key] = (cycles, errors, meet, phases, plans)
    return cycles, errors, meet, phases, plans


def _robot_slot_plans(layout: dict) -> dict:
    """{block name: (cycles, phases)} of every BATCH and SEPARATE with a robot and a cycle,
    each block's slots planned once."""
    out = {}
    for block in layout.get("blocks") or []:
        if isinstance(block, dict) and str(block.get("type", "")).upper() in ("BATCH", "SEPARATE") \
                and (block.get("params") or {}).get("robot"):
            cycles, _errors, _meet, phases, _plans = _slot_plans(layout, block)
            if cycles:
                out[str(block.get("name"))] = (cycles, phases)
    return out


def robot_cycles(layout: dict) -> dict:
    """{block name: [seconds per slot]} of every BATCH and SEPARATE with a robot, for the run."""
    return {name: cycles for name, (cycles, _phases) in _robot_slot_plans(layout).items()}


def robot_phases(layout: dict) -> dict:
    """{block name: per slot, ((segment kind, seconds), ...) or None} of every block
    `robot_cycles` has: what a robot BATCH traces as its cycle phases."""
    return {name: phases for name, (_cycles, phases) in _robot_slot_plans(layout).items()}
