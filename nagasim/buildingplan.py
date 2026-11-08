"""The building's floor plan as SVG (Phase 20 P3, D1007; moved to the engine by D1051).

Pure text, no Qt: the product's canvas, `product building`, the HTML player and viewer all
draw the same plan, and the engine's own `naga run --player` and `naga viewer` draw it without
the product. The section's shape and wall geometry are `nagasim.core.building`'s.
"""
from __future__ import annotations

import math
from xml.sax.saxutils import escape

from nagasim.core import building as engine_building

__all__ = ["plan_svg", "hatch_segments", "marking_middle", "WALL_DEFAULTS", "GROUND", "FONT",
           "MARKING_DASH", "HATCH_STEP", "MARKING_TEXT"]

FONT = "IBM Plex Sans, Segoe UI, Arial, Helvetica, sans-serif"
WALL_DEFAULTS = {**engine_building.WALL_DEFAULTS, "color": "#4A4F57", "face_color": "#E9E7E2",
                 "opening_fill": "#ECE9E3", "ground_3d": "#B9BDB6"}
GROUND = "#DCDCD6"
_outline = engine_building.outline


def walls_style(building: dict) -> dict:
    """The building's wall style over the defaults."""
    return {**WALL_DEFAULTS, **(building.get("walls") or {})}


def colour(building: dict, value):
    """A palette token's colour, or *value* itself."""
    return (building.get("palette") or {}).get(value, value)


_walls, _colour = walls_style, colour


# ---------------------------------------------------------------- the plan
def _band(a, b, half):
    """The four corners of a band of half-width *half* along a -> b."""
    length = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    nx, ny = -uy * half, ux * half
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


def _centroid(pts):
    area = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return cx / (3 * area), cy / (3 * area)


MARKING_DASH = (0.5, 0.3)       # metres on, off: a dashed floor marking
HATCH_STEP = 0.4                # metres between a hatched marking's stripes
MARKING_TEXT = 0.8              # metres: a floor marking's label, as painted floor text is


def hatch_segments(points, step: float = HATCH_STEP) -> list[tuple[tuple, tuple]]:
    """The 45-degree stripes inside a closed polygon, as ((x0, y0), (x1, y1)) segments: the
    lines x + y = c every *step* along the diagonal, cut where they cross the outline."""
    points = [tuple(q) for q in points]
    sums = [x + y for x, y in points]
    out = []
    c = (math.floor(min(sums) / step) + 0.5) * step
    while c < max(sums):
        cuts = []
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            sa, sb = ax + ay - c, bx + by - c
            if (sa > 0) != (sb > 0):
                t = sa / (sa - sb)
                cuts.append((ax + (bx - ax) * t, ay + (by - ay) * t))
        cuts.sort()
        out += [(cuts[k], cuts[k + 1]) for k in range(0, len(cuts) - 1, 2)]
        c += step
    return out


def marking_middle(marking: dict) -> tuple[float, float]:
    """Where a floor marking's label goes: an area's centroid, a line's half-way point."""
    pts = [tuple(q) for q in marking["points"]]
    if marking.get("closed"):
        return _centroid(pts)
    half = sum(math.dist(a, b) for a, b in zip(pts, pts[1:])) / 2
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        if d >= half:
            return a[0] + (b[0] - a[0]) * half / d, a[1] + (b[1] - a[1]) * half / d
        half -= d
    return pts[-1]


def plan_svg(building: dict, px: float = 40, *, texture: bool = False, walls: bool = True,
             markings: bool = True) -> str:
    """The floor plan as SVG, *px* pixels per metre. texture=True is the 3D floor: fills, door
    thresholds and floor markings plus tile joints, without walls, curtains, fixtures or
    labels. walls=False leaves out the walls and openings and markings=False the floor
    markings: the editor draws those as items over it (D1036, D1044)."""
    def p(v):
        return round(v * px, 1)

    def marking_shapes():
        out = []
        for m in building.get("markings", []) if markings else []:
            colour = engine_building.marking_colour(m)
            style = m.get("style", "solid")
            width = m.get("width", engine_building.MARKING_DEFAULTS["width"])
            pts = " ".join(f"{p(x)},{p(y)}" for x, y in m["points"])
            dash = (f' stroke-dasharray="{p(MARKING_DASH[0])},{p(MARKING_DASH[1])}"'
                    if style == "dashed" else "")
            tag = "polygon" if m.get("closed") else "polyline"
            out.append(f'<{tag} points="{pts}" fill="none" stroke="{colour}" '
                       f'stroke-width="{p(width)}" stroke-linejoin="miter"{dash}/>')
            if style == "hatch":
                out += [line(a, b, colour, width) for a, b in hatch_segments(m["points"])]
            if m.get("label"):
                x, y = marking_middle(m)
                words = text(x, y + MARKING_TEXT * 0.35, m["label"], MARKING_TEXT, 700, colour,
                             "middle")
                halo = f'fill="#FFFFFF" stroke="#FFFFFF" stroke-width="{p(0.12)}"'
                out += [w.replace(f'fill="{colour}"', halo) for w in words] + words
        return out

    def shape(pts, fill, stroke="none", sw=0.0, rx=0.0, extra=""):
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        if len(pts) == 4 and len(set(xs)) == 2 and len(set(ys)) == 2:     # axis rectangle
            return (f'<rect x="{p(min(xs))}" y="{p(min(ys))}" width="{p(max(xs) - min(xs))}" '
                    f'height="{p(max(ys) - min(ys))}" rx="{p(rx)}" fill="{fill}" stroke="{stroke}" '
                    f'stroke-width="{p(sw)}"{extra}/>')
        points = " ".join(f"{p(x)},{p(y)}" for x, y in pts)
        return f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="{p(sw)}"{extra}/>'

    def line(a, b, stroke, width, dash=None):
        dashes = f' stroke-dasharray="{p(dash[0])},{p(dash[1])}"' if dash else ""
        return (f'<line x1="{p(a[0])}" y1="{p(a[1])}" x2="{p(b[0])}" y2="{p(b[1])}" stroke="{stroke}" '
                f'stroke-width="{p(width)}"{dashes}/>')

    def text(x, y, s, size, weight, fill, anchor):
        out = []
        for k, part in enumerate(s.split("\n")):
            # Plan typography: a straight apostrophe is drawn as the typographic one.
            part = escape(part).replace("'", "&#8217;")
            out.append(f'<text x="{p(x)}" y="{p(y + k * size * 1.2)}" font-family="{FONT}" '
                       f'font-size="{p(size)}" font-weight="{weight}" fill="{fill}" '
                       f'text-anchor="{anchor}">{part}</text>')
        return out

    x0, y0, x1, y1 = building["extent"]
    w = _walls(building)
    t = w["thickness"]
    ink = _colour(building, w["color"])
    width, height = (x1 - x0) * px, (y1 - y0) * px
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{round(width)}" height="{round(height)}" '
           f'viewBox="{p(x0)} {p(y0)} {round(width)} {round(height)}">',
           shape([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], _colour(building, building.get("ground", GROUND)))]
    rooms = building.get("rooms", [])
    for room in rooms:
        if room.get("fill"):
            out.append(shape(_outline(room), _colour(building, room["fill"])))
    openings = [(o, engine_building.opening_ends(building, o))
                for o in building.get("openings", []) if o.get("plan", True)]
    openings = [(o, a, b) for o, ends in openings if ends is not None for a, b in [ends]]
    fill = _colour(building, w["opening_fill"])
    if texture:
        for o, a, b in openings:
            if o.get("kind", "door") == "door":
                out.append(shape(_band(a, b, t / 2 + 0.02), fill))
        step = building.get("floor_tile", 0.6)
        walled = [q for room in rooms if room.get("walls", True) for q in _outline(room)]
        if step and walled:
            # Joints one pixel wide, 4 % darker than the floor beneath (black at 4 % opacity).
            bx0, by0 = round(min(q[0] for q in walled) * px), round(min(q[1] for q in walled) * px)
            bx1, by1 = round(max(q[0] for q in walled) * px), round(max(q[1] for q in walled) * px)
            joint = round(step * px)
            for x in range(bx0, bx1, joint):
                out.append(f'<rect x="{x}" y="{by0}" width="1" height="{by1 - by0}" fill="#000000" '
                           f'fill-opacity="0.04"/>')
            for y in range(by0, by1, joint):
                out.append(f'<rect x="{bx0}" y="{y}" width="{bx1 - bx0}" height="1" fill="#000000" '
                           f'fill-opacity="0.04"/>')
        out += marking_shapes()             # tape on the tiles
        out.append("</svg>")
        return "\n".join(out) + "\n"
    out += marking_shapes()
    if walls:
        for room in rooms:
            if room.get("walls", True):
                out.append(shape(_outline(room), "none", ink, room.get("thickness", t)))
        for wall in building.get("wall_lines", []):
            points = " ".join(f"{p(x)},{p(y)}" for x, y in wall["points"])
            out.append(f'<polyline points="{points}" fill="none" stroke="{ink}" '
                       f'stroke-width="{p(wall.get("thickness", t))}" stroke-linejoin="miter" '
                       f'stroke-linecap="square"/>')
        for o, a, b in openings:
            out.append(shape(_band(a, b, t / 2 + 0.02), fill))
            if o.get("kind") == "curtain":
                out.append(line(a, b, ink, 0.05, (0.15, 0.12)))
    for f in building.get("fixtures", []):
        stroke = _colour(building, f.get("stroke", "none"))
        if "line" in f:
            out.append(line(f["line"][0], f["line"][1], stroke, f.get("stroke_width", 0.05), f.get("dash")))
        else:
            fx0, fy0, fx1, fy1 = f["rect"]
            out.append(shape([(fx0, fy0), (fx1, fy0), (fx1, fy1), (fx0, fy1)],
                             _colour(building, f.get("fill", "none")), stroke, f.get("stroke_width", 0),
                             f.get("radius", 0)))
    labels = []
    for room in rooms:
        label = room.get("label", {})
        if label is False:
            continue
        labels.append({"text": room["name"], "at": _centroid(_outline(room)), **label})
    for label in labels + list(building.get("labels", [])):
        out += text(label["at"][0], label["at"][1], label["text"], label.get("size", 0.55),
                    label.get("weight", 600), _colour(building, label.get("color", ink)),
                    label.get("anchor", "middle"))
    out.append("</svg>")
    return "\n".join(out) + "\n"
