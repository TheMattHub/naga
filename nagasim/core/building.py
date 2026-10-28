"""The `building` section: rooms, walls, wall openings, floor markings, labels and plan fixtures
(Phase 20 P3, D1005-D1006; the building editor, D1033-D1034, D1041, D1044).

Layout-only data, like `background` and `annotations`: the engine validates and round-trips it and
never reads it to run, so a building can never change a result. Coordinates are metres in the
network's frame (x right, y down). The product draws it (`product/building.py`: the floor SVG,
the 3D film's `walls.json`, the floor texture). Walls are the outlines of the rooms with
`"walls": true` plus the free-standing `wall_lines`, cut by the openings: `wall_runs` derives them
here, once, for the product's writers and for the one geometric check, `W_WALL_CROSSING` (a path
segment through a wall with no opening). Stdlib only.
"""
from __future__ import annotations

import math
import re

from nagasim.core.problems import problem

__all__ = ["validate_building", "wall_runs", "wall_pieces", "wall_crossings", "outline",
           "wall_points", "opening_ends", "opening_problem", "attach_opening", "marking_colour",
           "MARKING_COLOURS", "MARKING_STYLES", "MARKING_DEFAULTS", "OPENING_KINDS", "ANCHORS", "WALL_FINISHES", "WALL_DEFAULTS"]

OPENING_KINDS = ("door", "curtain")
ANCHORS = ("start", "middle", "end")
# A wall's finish is one of these ids (the product draws each as a texture, `product/finishes.py`)
# or a "#RRGGBB" flat colour: an id from a fixed set, never a path (netguard, D1035).
WALL_FINISHES = ("plaster", "brick", "concrete", "tile", "wood")
WALL_DEFAULTS = {"height": 2.7, "thickness": 0.2}
LINE_TOL = 0.01         # metres: an opening this close to a wall line cuts it
# Floor markings (D1044): the RAL signal colours floor tape is sold in, by name, or "#RRGGBB".
MARKING_COLOURS = {"yellow": "#F9A800", "white": "#ECECE7", "red": "#A52019", "orange": "#D4652F",
                   "green": "#317F43", "blue": "#154889", "black": "#2B2B2C"}
MARKING_STYLES = ("solid", "dashed", "hatch")       # hatch: a closed marking's no-go fill
MARKING_DEFAULTS = {"color": "yellow", "style": "solid", "width": 0.1}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FIELDS = {"extent", "ground", "palette", "walls", "floor_tile", "rooms", "wall_lines", "openings",
           "labels", "fixtures", "markings"}
_MARKING_FIELDS = {"points", "closed", "color", "style", "width", "label"}
_STYLE_FIELDS = ("height", "thickness", "finish")       # a room's or a wall line's own wall style
_WALL_FIELDS = {"color", "face_color", "opening_fill", "ground_3d", *_STYLE_FIELDS}
_ROOM_FIELDS = {"name", "rect", "polygon", "fill", "walls", "label", *_STYLE_FIELDS}
_LINE_FIELDS = {"name", "points", *_STYLE_FIELDS}
_LABEL_FIELDS = {"text", "at", "size", "weight", "color", "anchor"}
# An opening is either attached to a wall (D1041): "wall" = {"room": name} or {"line": index},
# plus "edge" k (from point k to k + 1, a room's last edge closing it), "at" = where its centre
# sits along that edge (0..1) and "width" in metres, so it follows the wall when the wall moves;
# or free, "a" and "b" (two points on a wall line), the older form, still read.
_OPENING_FIELDS = {"a", "b", "wall", "at", "width", "kind", "plan"}
_FIXTURE_FIELDS = {"rect", "line", "fill", "stroke", "stroke_width", "radius", "dash"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _index(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _point(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(c) for c in v)


def _rect(v) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 4 and all(_num(c) for c in v)
            and v[0] < v[2] and v[1] < v[3])


def validate_building(layout: dict, problems: list) -> None:
    """Shape of the optional `building` section (a `core/sections.py` hook); absent or null is
    no building."""
    section = layout.get("building")
    if section is None:
        return

    first = len(problems)

    def bad(text, path="/building", hint=None):
        problems.append(problem("E_BUILDING", f"building: {text}", path, hint=hint))

    if not isinstance(section, dict):
        bad("must be an object", hint='{"extent": [0, 0, 50, 30], "rooms": [...]}')
        return
    for field in sorted(set(section) - _FIELDS):
        bad(f"unknown field '{field}'", f"/building/{field}")
    if not _rect(section.get("extent")):
        bad("extent must be [x0, y0, x1, y1] in metres with x0 < x1 and y0 < y1",
            "/building/extent", hint="the plan's canvas, e.g. [0, 0, 52, 32]")
    palette = section.get("palette", {})
    if not isinstance(palette, dict):
        bad("palette must be an object of name -> \"#RRGGBB\"", "/building/palette")
        palette = {}
    for name, value in palette.items():
        if not (isinstance(value, str) and _HEX.match(value)):
            bad(f"palette '{name}' must be a \"#RRGGBB\" colour", f"/building/palette/{name}")

    def colour(value, path, tokens=True):
        ok = isinstance(value, str) and (_HEX.match(value) or (tokens and value in palette))
        if not ok:
            bad(f"{path.rsplit('/', 1)[-1]} must be a \"#RRGGBB\" colour"
                + (" or a palette name" if tokens else ""), path)

    def positive(value, path):
        if not (_num(value) and value > 0):
            bad(f"{path.rsplit('/', 1)[-1]} must be a number > 0", path)

    def style(record, path):
        for field in ("height", "thickness"):
            if field in record:
                positive(record[field], f"{path}/{field}")
        finish = record.get("finish")
        if "finish" in record and not (finish in WALL_FINISHES
                                       or (isinstance(finish, str) and _HEX.match(finish))):
            bad(f"finish must be one of {', '.join(WALL_FINISHES)} or a \"#RRGGBB\" colour",
                f"{path}/finish", hint='"finish": "brick" or "finish": "#D8D2C4"')

    if "ground" in section:
        colour(section["ground"], "/building/ground")
    if section.get("floor_tile") is not None:
        positive(section["floor_tile"], "/building/floor_tile")
    walls = section.get("walls", {})
    if not isinstance(walls, dict):
        bad("walls must be an object", "/building/walls")
        walls = {}
    for field in sorted(set(walls) - _WALL_FIELDS):
        bad(f"unknown walls field '{field}'", f"/building/walls/{field}")
    style(walls, "/building/walls")
    for field in ("color", "face_color", "opening_fill", "ground_3d"):
        if field in walls:
            colour(walls[field], f"/building/walls/{field}")

    def check_label(label, path, need_text):
        if not isinstance(label, dict):
            bad("a label must be an object", path)
            return
        for field in sorted(set(label) - _LABEL_FIELDS):
            bad(f"unknown label field '{field}'", f"{path}/{field}")
        if need_text or "text" in label:
            if not (isinstance(label.get("text"), str) and label["text"]):
                bad("a label's text must be a non-empty string", f"{path}/text")
        if need_text or "at" in label:
            if not _point(label.get("at")):
                bad("a label's at must be [x, y] in metres", f"{path}/at")
        if "size" in label:
            positive(label["size"], f"{path}/size")
        if "weight" in label and not (isinstance(label["weight"], int)
                                      and not isinstance(label["weight"], bool)
                                      and 100 <= label["weight"] <= 900):
            bad("a label's weight must be an integer 100..900", f"{path}/weight")
        if "color" in label:
            colour(label["color"], f"{path}/color")
        if "anchor" in label and label["anchor"] not in ANCHORS:
            bad(f"a label's anchor must be one of {', '.join(ANCHORS)}", f"{path}/anchor")

    def entries(field):
        value = section.get(field, [])
        if not isinstance(value, list):
            bad(f"{field} must be a list", f"/building/{field}")
            return []
        return list(enumerate(value))

    names = set()
    for i, room in entries("rooms"):
        path = f"/building/rooms/{i}"
        if not isinstance(room, dict):
            bad("a room must be an object", path)
            continue
        for field in sorted(set(room) - _ROOM_FIELDS):
            bad(f"unknown room field '{field}'", f"{path}/{field}")
        name = room.get("name")
        if not (isinstance(name, str) and name):
            bad("a room needs a name", f"{path}/name")
        elif name in names:
            bad(f"room '{name}' is declared twice", f"{path}/name")
        names.add(name)
        if ("rect" in room) == ("polygon" in room):
            bad("a room needs exactly one of rect or polygon", path,
                hint="\"rect\": [x0, y0, x1, y1] or \"polygon\": [[x, y], ...]")
        elif "rect" in room and not _rect(room["rect"]):
            bad("a room's rect must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1", f"{path}/rect")
        elif "polygon" in room and not (isinstance(room["polygon"], list)
                                        and len(room["polygon"]) >= 3
                                        and all(_point(p) for p in room["polygon"])):
            bad("a room's polygon must be at least three [x, y] points", f"{path}/polygon")
        if "fill" in room:
            colour(room["fill"], f"{path}/fill")
        if "walls" in room and not isinstance(room["walls"], bool):
            bad("a room's walls must be true or false", f"{path}/walls")
        if "label" in room and room["label"] is not False:
            check_label(room["label"], f"{path}/label", need_text=False)
        style(room, path)
    for i, line in entries("wall_lines"):
        path = f"/building/wall_lines/{i}"
        if not isinstance(line, dict):
            bad("a wall line must be an object", path)
            continue
        for field in sorted(set(line) - _LINE_FIELDS):
            bad(f"unknown wall line field '{field}'", f"{path}/{field}")
        if "name" in line and not (isinstance(line["name"], str) and line["name"]):
            bad("a wall line's name must be a non-empty string", f"{path}/name")
        points = line.get("points")
        if not (isinstance(points, list) and len(points) >= 2 and all(_point(p) for p in points)
                and all(math.dist(p, q) > 0 for p, q in zip(points, points[1:]))):
            bad("a wall line's points must be at least two distinct [x, y] points",
                f"{path}/points", hint='"points": [[0, 0], [10, 0], [10, 6]]')
        style(line, path)
    for i, opening in entries("openings"):
        path = f"/building/openings/{i}"
        if not isinstance(opening, dict):
            bad("an opening must be an object", path)
            continue
        for field in sorted(set(opening) - _OPENING_FIELDS):
            bad(f"unknown opening field '{field}'", f"{path}/{field}")
        if "wall" in opening or "at" in opening or "width" in opening:
            wall = opening.get("wall")
            if "a" in opening or "b" in opening:
                bad("an opening has either wall, at and width, or a and b, not both", path)
            if not (isinstance(wall, dict) and set(wall) in ({"room", "edge"}, {"line", "edge"})
                    and _index(wall.get("edge"))
                    and (_index(wall["line"]) if "line" in wall
                         else isinstance(wall.get("room"), str) and wall["room"])):
                bad("an opening's wall must be {\"room\": name, \"edge\": k} or "
                    "{\"line\": index, \"edge\": k}", f"{path}/wall",
                    hint='"wall": {"room": "Store", "edge": 1}: edge k runs from point k to k + 1')
            if not (_num(opening.get("at")) and 0 <= opening["at"] <= 1):
                bad("an opening's at must be a number 0..1 (its centre along the edge)",
                    f"{path}/at")
            if not (_num(opening.get("width")) and opening["width"] > 0):
                bad("an opening's width must be a number > 0 (metres)", f"{path}/width")
        else:
            a, b = opening.get("a"), opening.get("b")
            if not (_point(a) and _point(b) and math.dist(a, b) > 0):
                bad("an opening needs two distinct points a and b on a wall, or wall, at and "
                    "width", path)
        if opening.get("kind", "door") not in OPENING_KINDS:
            bad(f"an opening's kind must be one of {', '.join(OPENING_KINDS)}", f"{path}/kind")
        if "plan" in opening and not isinstance(opening["plan"], bool):
            bad("an opening's plan must be true or false", f"{path}/plan")
    for i, marking in entries("markings"):
        path = f"/building/markings/{i}"
        if not isinstance(marking, dict):
            bad("a floor marking must be an object", path)
            continue
        for field in sorted(set(marking) - _MARKING_FIELDS):
            bad(f"unknown floor marking field '{field}'", f"{path}/{field}")
        points = marking.get("points")
        closed = marking.get("closed", False)
        if not isinstance(closed, bool):
            bad("a floor marking's closed must be true or false", f"{path}/closed")
        if not (isinstance(points, list) and len(points) >= (3 if closed is True else 2)
                and all(_point(p) for p in points)
                and all(math.dist(p, q) > 0 for p, q in zip(points, points[1:]))):
            bad("a floor marking's points must be at least two distinct [x, y] points (three "
                "when closed)", f"{path}/points", hint='"points": [[0, 0], [10, 0]]')
        color = marking.get("color", MARKING_DEFAULTS["color"])
        if not (color in MARKING_COLOURS or (isinstance(color, str) and _HEX.match(color))):
            bad(f"a floor marking's color must be one of {', '.join(MARKING_COLOURS)} or a "
                "\"#RRGGBB\" colour", f"{path}/color")
        style = marking.get("style", MARKING_DEFAULTS["style"])
        if style not in MARKING_STYLES:
            bad(f"a floor marking's style must be one of {', '.join(MARKING_STYLES)}",
                f"{path}/style")
        elif style == "hatch" and closed is not True:
            bad("a hatched floor marking must be closed", f"{path}/style",
                hint='"closed": true: hatch fills the area inside it')
        if "width" in marking:
            positive(marking["width"], f"{path}/width")
        if "label" in marking and not (isinstance(marking["label"], str) and marking["label"]):
            bad("a floor marking's label must be a non-empty string", f"{path}/label")
    for i, label in entries("labels"):
        check_label(label, f"/building/labels/{i}", need_text=True)
    for i, fixture in entries("fixtures"):
        path = f"/building/fixtures/{i}"
        if not isinstance(fixture, dict):
            bad("a fixture must be an object", path)
            continue
        for field in sorted(set(fixture) - _FIXTURE_FIELDS):
            bad(f"unknown fixture field '{field}'", f"{path}/{field}")
        if ("rect" in fixture) == ("line" in fixture):
            bad("a fixture needs exactly one of rect or line", path)
        elif "rect" in fixture and not _rect(fixture["rect"]):
            bad("a fixture's rect must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1",
                f"{path}/rect")
        elif "line" in fixture and not (isinstance(fixture["line"], list)
                                        and len(fixture["line"]) == 2
                                        and all(_point(p) for p in fixture["line"])):
            bad("a fixture's line must be two [x, y] points", f"{path}/line")
        for field in ("fill", "stroke"):
            if field in fixture and fixture[field] != "none":
                colour(fixture[field], f"{path}/{field}")
        for field in ("stroke_width", "radius"):
            if field in fixture and not (_num(fixture[field]) and fixture[field] >= 0):
                bad(f"a fixture's {field} must be a number >= 0", f"{path}/{field}")
        if "dash" in fixture and not (_point(fixture["dash"]) and min(fixture["dash"]) > 0):
            bad("a fixture's dash must be [on, off] lengths > 0", f"{path}/dash")
    if len(problems) == first:          # the geometry is only read once the shapes are right
        for i, opening in enumerate(section.get("openings", [])):
            reason = opening_problem(section, opening)
            if reason:
                problems.append(problem(
                    "W_OPENING_OFF_WALL", f"building: opening {i} {reason}, so it cuts no wall",
                    f"/building/openings/{i}",
                    hint="put it on a wall (the editor's Opening tool) or delete it"))
        for index, a, b, at in wall_crossings(section, layout.get("network")):
            problems.append(problem(
                "W_WALL_CROSSING",
                f"network segment {a} -> {b} crosses a wall with no opening at "
                f"({at[0]:.2f}, {at[1]:.2f}) m", f"/network/segments/{index}",
                hint="add an opening (a door) where the path passes, or move the path; walls "
                     "never change routing, so the run is not affected"))


# ---------------------------------------------------------------- wall geometry (D1034)
def outline(room: dict) -> list[tuple[float, float]]:
    """A room's corners in order (a rect's clockwise from its top-left)."""
    if "rect" in room:
        x0, y0, x1, y1 = room["rect"]
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [tuple(p) for p in room["polygon"]]


def _line(p, q):
    """The wall line through p and q as (key, t_p, t_q): key = (angle, offset) sorts horizontal
    lines (by y) before vertical ones (by x), as the healthcare project's make_floor.py did; t
    runs along the line. Axis lines keep their coordinates exact, so integer plans stay
    integers."""
    if p[1] == q[1]:
        return (0.0, p[1]), p[0], q[0]
    if p[0] == q[0]:
        return (90.0, p[0]), p[1], q[1]
    angle = math.degrees(math.atan2(q[1] - p[1], q[0] - p[0])) % 180.0
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    offset = p[0] * -dy + p[1] * dx
    return (round(angle, 6), round(offset, 6)), p[0] * dx + p[1] * dy, q[0] * dx + q[1] * dy


def _at(key, t):
    angle, offset = key
    if angle == 0.0:
        return [t, offset]
    if angle == 90.0:
        return [offset, t]
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return [round(-dy * offset + dx * t, 6), round(dx * offset + dy * t, 6)]


def _style(walls: dict, record: dict) -> tuple:
    """(height, thickness, finish) of *record*'s walls: its own, else the building's, else the
    defaults; finish None is the building's flat face colour."""
    return tuple(record.get(f, walls.get(f, WALL_DEFAULTS.get(f))) for f in _STYLE_FIELDS)


def wall_runs(building: dict) -> list[dict]:
    """Every wall as a merged collinear run with its opening gaps: `{"a", "b", "gaps"?, "height",
    "thickness", "finish"}`, gaps in metres along a -> b. Edges of one style on one line merge
    (two rooms sharing a wall give one run); a different style on the same line is its own run."""
    walls = building.get("walls") or {}
    lines: dict = {}
    edges = []
    for room in building.get("rooms", []):
        if room.get("walls", True):
            pts = outline(room)
            edges += [(p, q, _style(walls, room)) for p, q in zip(pts, pts[1:] + pts[:1])]
    for line in building.get("wall_lines", []):
        pts = [tuple(p) for p in line["points"]]
        edges += [(p, q, _style(walls, line)) for p, q in zip(pts, pts[1:])]
    for p, q, style in edges:
        key, ta, tb = _line(p, q)
        lines.setdefault((key, style), []).append((min(ta, tb), max(ta, tb)))
    cuts = []
    for opening in building.get("openings", []):
        ends = opening_ends(building, opening)
        if ends is not None:
            key, ta, tb = _line(*ends)
            cuts.append((key, min(ta, tb), max(ta, tb)))
    runs = []
    for key, style in sorted(lines, key=lambda k: (k[0], k[1][0], k[1][1], str(k[1][2] or ""))):
        spans = sorted(lines[(key, style)])
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        mine = [(c0, c1) for k, c0, c1 in cuts
                if k[0] == key[0] and abs(k[1] - key[1]) <= LINE_TOL]
        for a, b in merged:
            gaps = sorted([max(a, c0) - a, min(b, c1) - a] for c0, c1 in mine if c1 > a and c0 < b)
            gaps = [[round(s0, 3), round(s1, 3)] for s0, s1 in gaps]
            runs.append({"a": _at(key, a), "b": _at(key, b), **({"gaps": gaps} if gaps else {}),
                         "height": style[0], "thickness": style[1], "finish": style[2]})
    return runs


def marking_colour(marking: dict) -> str:
    """A floor marking's colour as "#RRGGBB"."""
    color = marking.get("color", MARKING_DEFAULTS["color"])
    return MARKING_COLOURS.get(color, color).upper()


# ---------------------------------------------------------------- openings on walls (D1041)
def wall_points(building: dict, wall) -> tuple[list[tuple[float, float]], bool] | None:
    """The points of the wall an opening names ({"room": name} or {"line": index}) and whether
    it is closed (a room), or None when there is no such wall."""
    if not isinstance(wall, dict):
        return None
    if "room" in wall:
        room = next((r for r in building.get("rooms", []) if r.get("name") == wall["room"]), None)
        return None if room is None else (outline(room), True)
    lines = building.get("wall_lines", [])
    i = wall.get("line")
    if not (_index(i) and i < len(lines)):
        return None
    return [tuple(p) for p in lines[i]["points"]], False


def _edge(building: dict, opening: dict):
    """(p, q) of the edge an attached opening names, or a reason it has none."""
    wall = opening["wall"]
    found = wall_points(building, wall)
    if found is None:
        return (f"names room '{wall['room']}', which does not exist" if "room" in wall
                else f"names wall line {wall.get('line')}, which does not exist")
    points, closed = found
    k = wall["edge"]
    if k >= (len(points) if closed else len(points) - 1):
        return f"names edge {k} of a wall with {len(points) - (0 if closed else 1)} edges"
    return points[k], points[(k + 1) % len(points)]


def opening_ends(building: dict, opening: dict):
    """An opening's two ends ((ax, ay), (bx, by)) in metres, or None when it cannot be placed.
    An attached opening's centre is clamped so the opening stays on its edge (a wall made shorter
    keeps its door, D1041); one wider than its edge has no place."""
    if "wall" not in opening:
        return tuple(opening["a"]), tuple(opening["b"])
    edge = _edge(building, opening)
    if isinstance(edge, str):
        return None
    p, q = edge
    length, width = math.dist(p, q), opening["width"]
    if width > length + 1e-9:
        return None
    ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
    s = min(max(opening["at"] * length, width / 2), length - width / 2)
    return ((round(p[0] + ux * (s - width / 2), 6) + 0.0, round(p[1] + uy * (s - width / 2), 6) + 0.0),
            (round(p[0] + ux * (s + width / 2), 6) + 0.0, round(p[1] + uy * (s + width / 2), 6) + 0.0))


def opening_problem(building: dict, opening: dict) -> str | None:
    """Why an opening cuts no wall (for `W_OPENING_OFF_WALL`), or None when it cuts one."""
    if "wall" in opening:
        edge = _edge(building, opening)
        if isinstance(edge, str):
            return edge
        length = math.dist(*edge)
        if opening["width"] > length + 1e-9:
            return f"is {opening['width']:g} m wide but its wall edge is {length:.2f} m long"
        if "room" in opening["wall"]:
            room = next(r for r in building.get("rooms", []) if r.get("name") == opening["wall"]["room"])
            if not room.get("walls", True):
                return f"is on room '{room['name']}', which has no walls"
        return None
    a, b = tuple(opening["a"]), tuple(opening["b"])
    key, ta, tb = _line(a, b)
    lo, hi = min(ta, tb), max(ta, tb)
    for run in wall_runs({k: v for k, v in building.items() if k != "openings"}):
        rkey, r0, r1 = _line(tuple(run["a"]), tuple(run["b"]))
        if rkey[0] == key[0] and abs(rkey[1] - key[1]) <= LINE_TOL \
                and min(r0, r1) < hi and max(r0, r1) > lo:
            return None
    return "is not on a wall line"


def attach_opening(building: dict, opening: dict) -> dict | None:
    """The attached form of a free opening ("a"/"b"): the first wall edge both its ends lie on
    (within LINE_TOL; walled rooms first, then free walls, then unwalled rooms), its centre's
    place along it and its width; None when no edge holds it. An attached opening is returned
    as it is."""
    if "wall" in opening:
        return dict(opening)
    a, b = tuple(opening["a"]), tuple(opening["b"])
    candidates = []
    for walled in (True, False):
        for room in building.get("rooms", []):
            if room.get("walls", True) == walled:
                pts = outline(room)
                candidates += [({"room": room["name"], "edge": k}, p, q)
                               for k, (p, q) in enumerate(zip(pts, pts[1:] + pts[:1]))]
        if walled:
            for i, line in enumerate(building.get("wall_lines", [])):
                pts = [tuple(p) for p in line["points"]]
                candidates += [({"line": i, "edge": k}, p, q)
                               for k, (p, q) in enumerate(zip(pts, pts[1:]))]
    for wall, p, q in candidates:
        length = math.dist(p, q)
        if length <= 0:
            continue
        ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
        along = [(c[0] - p[0]) * ux + (c[1] - p[1]) * uy for c in (a, b)]
        off = [abs((c[0] - p[0]) * -uy + (c[1] - p[1]) * ux) for c in (a, b)]
        if max(off) <= LINE_TOL and min(along) >= -LINE_TOL and max(along) <= length + LINE_TOL:
            out = {"wall": wall, "at": round(min(max(sum(along) / 2 / length, 0.0), 1.0), 6),
                   "width": round(math.dist(a, b), 6)}
            out.update({k: v for k, v in opening.items() if k in ("kind", "plan")})
            return out
    return None


def wall_pieces(run: dict) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """The solid centre-line pieces of one run, its gaps cut out."""
    (ax, ay), (bx, by) = run["a"], run["b"]
    length = math.dist((ax, ay), (bx, by))
    if length <= 0:
        return []
    ux, uy = (bx - ax) / length, (by - ay) / length
    pieces, at = [], 0.0
    for g0, g1 in sorted(run.get("gaps", [])):
        if g0 > at:
            pieces.append((at, g0))
        at = max(at, g1)
    if length > at:
        pieces.append((at, length))
    return [((ax + ux * s0, ay + uy * s0), (ax + ux * s1, ay + uy * s1)) for s0, s1 in pieces]


def _cross(p, q, a, b):
    """Where segment p-q properly crosses segment a-b (each strictly between its ends), or None:
    touching a wall at a path's end, or running along it, is not a crossing."""
    def orient(o, s, t):
        return (s[0] - o[0]) * (t[1] - o[1]) - (s[1] - o[1]) * (t[0] - o[0])
    eps = 1e-9
    d1, d2, d3, d4 = orient(a, b, p), orient(a, b, q), orient(p, q, a), orient(p, q, b)
    if d1 * d2 < -eps and d3 * d4 < -eps:
        t = d1 / (d1 - d2)
        return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
    return None


def wall_crossings(building: dict, network: dict | None) -> list[tuple[int, str, str, tuple]]:
    """(segment index, from, to, crossing point) for each path-network segment that crosses a
    solid wall piece, the first crossing per segment. Routes run along segments, so this covers
    every route."""
    nodes = {}
    for node in (network or {}).get("nodes") or []:
        if isinstance(node, dict) and _num(node.get("x", 0.0)) and _num(node.get("y", 0.0)):
            nodes[node.get("name")] = (float(node.get("x", 0.0)), float(node.get("y", 0.0)))
    segments = (network or {}).get("segments") or []
    if not nodes or not segments:
        return []
    pieces = [piece for run in wall_runs(building) for piece in wall_pieces(run)]
    out = []
    for index, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        p, q = nodes.get(seg.get("from")), nodes.get(seg.get("to"))
        if p is None or q is None:
            continue
        bx0, bx1, by0, by1 = min(p[0], q[0]), max(p[0], q[0]), min(p[1], q[1]), max(p[1], q[1])
        for a, b in pieces:
            if max(a[0], b[0]) < bx0 or min(a[0], b[0]) > bx1 or max(a[1], b[1]) < by0 \
                    or min(a[1], b[1]) > by1:
                continue
            at = _cross(p, q, a, b)
            if at is not None:
                out.append((index, str(seg.get("from")), str(seg.get("to")), at))
                break
    return out
