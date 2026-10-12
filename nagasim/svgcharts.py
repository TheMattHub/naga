"""Minimal inline SVG chart helpers for nagasim/report.py (ROADMAP 1.7 B).

No external assets, no scripts, no new dependency: plain SVG shapes and
<text>, built with the standard library only (html, math). One restrained,
colour-blind-safe palette, used everywhere in the report.
"""
from __future__ import annotations

import html
import math
from typing import NamedTuple

__all__ = ["bar_chart", "stacked_bar_chart", "line_chart", "histogram", "scatter_chart",
           "sankey", "gantt", "nice_ticks", "fmt_tick", "PALETTE", "ChartPalette",
           "LIGHT_PALETTE"]

PALETTE = ["#2b6cb0", "#dd6b20", "#2c7a7b", "#4a5568"]   # blue, orange, teal, grey
_FONT = "-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif"
_AXIS = "#a0aec0"
_GRID = "#e2e8f0"
_TEXT = "#4a5568"
_TITLE = "#1a202c"


class ChartPalette(NamedTuple):
    """Colours `gantt`/`sankey` draw with (item 7): every other chart here is
    only ever rendered into the offline HTML report, which is its own fixed
    light document, but these two are also embedded live in the app (the plan
    Gantt, the results Sankey), where a fixed light palette read as broken on
    a dark theme. The other chart functions keep their module-level colours
    -- adding a palette argument to functions nothing calls with a dark
    palette would be speculative."""
    series: tuple = tuple(PALETTE)
    axis: str = _AXIS
    text: str = _TEXT
    title: str = _TITLE


LIGHT_PALETTE = ChartPalette()

# One consistent on-page size (ROADMAP 1.8 PART 0 a): every report chart is
# 460x300 CSS px in a two-column grid, so a lone chart in its row never
# stretches to fill the width. Explicit width/height (not just viewBox) pin
# that size even where the report CSS is stripped (e.g. a raw file:// view).
_W, _H = 460, 300
_MARGIN = {"left": 68, "right": 20, "top": 28, "bottom": 52}


def nice_ticks(vmin: float, vmax: float, count: int = 5) -> list[float]:
    """"Nice" round tick values spanning at least [vmin, vmax].

    Handles the degenerate cases a report chart can hit: non-finite bounds
    (all-NaN input), a single point, and a constant series -- all fall back to
    a small padded range instead of dividing by zero or looping forever.
    """
    if not (math.isfinite(vmin) and math.isfinite(vmax)):
        vmin, vmax = 0.0, 1.0
    if vmin > vmax:
        vmin, vmax = vmax, vmin
    if vmin == vmax:
        pad = abs(vmin) * 0.1 or 1.0
        vmin, vmax = vmin - pad, vmax + pad
    span = vmax - vmin
    raw_step = span / max(count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / magnitude
    step = magnitude * (10 if residual > 5 else 5 if residual > 2 else 2 if residual > 1 else 1)
    start = math.floor(vmin / step) * step
    end = math.ceil(vmax / step) * step
    n = int(round((end - start) / step)) + 1
    return [round(start + i * step, 10) for i in range(n)]


def fmt_tick(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.3g}"


def _svg_open(title: str) -> str:
    return (f'<svg width="{_W}" height="{_H}" viewBox="0 0 {_W} {_H}" role="img" '
            f'aria-label="{html.escape(title)}" xmlns="http://www.w3.org/2000/svg" '
            f'font-family="{_FONT}" font-size="12">')


def _title_text(title: str, color: str = _TITLE) -> str:
    return (f'<text x="{_W / 2}" y="16" text-anchor="middle" font-weight="600" '
            f'fill="{color}">{html.escape(title)}</text>')


def _empty_chart(title: str, palette: ChartPalette | None = None) -> str:
    p = palette or LIGHT_PALETTE
    return (_svg_open(title) + _title_text(title, p.title) +
            f'<text x="{_W / 2}" y="{_H / 2}" text-anchor="middle" fill="{p.text}">'
            f'No data</text></svg>')


def _y_axis(vmin: float, vmax: float, x0: float, x1: float, y0: float, y1: float,
           y_title: str):
    """Y axis line, gridlines and tick labels; returns (svg parts, y_of(value))."""
    ticks = nice_ticks(vmin, vmax)
    lo, hi = ticks[0], ticks[-1]

    def y_of(v: float) -> float:
        if hi == lo:
            return (y0 + y1) / 2
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    parts = [f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="{_AXIS}" />']
    for t in ticks:
        y = y_of(t)
        parts.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" '
                     f'stroke="{_GRID}" stroke-width="1" />')
        parts.append(f'<text x="{x0 - 8:.1f}" y="{y + 4:.1f}" text-anchor="end" '
                     f'fill="{_TEXT}">{fmt_tick(t)}</text>')
    if y_title:
        cy = (y0 + y1) / 2
        parts.append(f'<text x="16" y="{cy:.1f}" text-anchor="middle" fill="{_TEXT}" '
                     f'transform="rotate(-90 16 {cy:.1f})">{html.escape(y_title)}</text>')
    return parts, y_of


def _x_tick_labels(x_ticks, xmin: float, xmax: float, x_of, y1: float,
                   color: str = _TEXT) -> list[str]:
    """The x-axis tick labels: the numeric ladder, or explicit (value, label)
    pairs when a caller supplies them (PHASE4 T10 date axes)."""
    pairs = ([(t, fmt_tick(t)) for t in nice_ticks(xmin, xmax, 6)] if x_ticks is None
             else list(x_ticks))
    slant = x_ticks is not None and any(len(label) > 8 for _t, label in pairs)
    parts = []
    for value, label in pairs:
        if not (xmin <= value <= xmax):
            continue
        xt = x_of(value)
        transform = (f' transform="rotate(-30 {xt:.1f} {y1 + 16})"' if slant else "")
        anchor = "end" if slant else "middle"
        parts.append(f'<text x="{xt:.1f}" y="{y1 + 16}" text-anchor="{anchor}" '
                     f'fill="{color}"{transform}>{html.escape(str(label))}</text>')
    return parts


def bar_chart(title: str, categories: list[str], values: list[float], y_title: str,
             *, err: list[tuple[float, float]] | None = None) -> str:
    """One bar per category. `err[i]` (optional), a (lb, ub) pair, draws a CI
    whisker on bar *i*; `err` may be shorter than *values* or contain `None`."""
    n = len(categories)
    if n == 0:
        return _empty_chart(title)
    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    finite_vals = [v for v in values if math.isfinite(v)]
    err_bounds = [b for b in (err or []) if b and math.isfinite(b[0]) and math.isfinite(b[1])]
    lo_candidates = finite_vals + [b[0] for b in err_bounds]
    hi_candidates = finite_vals + [b[1] for b in err_bounds]
    vmin = min([0.0] + lo_candidates)
    vmax = max([1.0] + hi_candidates) if hi_candidates or finite_vals else 1.0
    axis_parts, y_of = _y_axis(vmin, vmax, x0, x1, y0, y1, y_title)

    parts = [_svg_open(title), _title_text(title)] + axis_parts
    band = (x1 - x0) / n
    bar_w = band * 0.6
    base_y = y_of(0) if vmin <= 0 <= vmax else y1

    for i, (cat, val) in enumerate(zip(categories, values)):
        cx = x0 + band * (i + 0.5)
        if math.isfinite(val):
            y = y_of(val)
            top, bot = (y, base_y) if y < base_y else (base_y, y)
            parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                         f'height="{max(bot - top, 0):.1f}" fill="{PALETTE[0]}" />')
            bounds = err[i] if err and i < len(err) else None
            if bounds and math.isfinite(bounds[0]) and math.isfinite(bounds[1]):
                lb_y, ub_y = y_of(bounds[0]), y_of(bounds[1])
                parts.append(f'<line x1="{cx:.1f}" y1="{lb_y:.1f}" x2="{cx:.1f}" y2="{ub_y:.1f}" '
                             f'stroke="{_TITLE}" stroke-width="1.5" />')
                for yy in (lb_y, ub_y):
                    parts.append(f'<line x1="{cx - 5:.1f}" y1="{yy:.1f}" x2="{cx + 5:.1f}" '
                                 f'y2="{yy:.1f}" stroke="{_TITLE}" stroke-width="1.5" />')
        parts.append(f'<text x="{cx:.1f}" y="{y1 + 16}" text-anchor="middle" '
                     f'fill="{_TEXT}">{html.escape(str(cat))}</text>')

    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_AXIS}" />')
    parts.append('</svg>')
    return "".join(parts)


def stacked_bar_chart(title: str, categories: list[str], series: dict[str, list[float]],
                      y_title: str) -> str:
    """One stacked bar per category, one segment per `series` entry (ROADMAP
    2.2: busy/idle/usage cost per resource). Negative values are not
    supported (a cost never is); a category with no data draws no segments."""
    n = len(categories)
    if n == 0 or not series:
        return _empty_chart(title)
    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    totals = [sum(v[i] for v in series.values() if math.isfinite(v[i])) for i in range(n)]
    vmax = max([1.0] + [t for t in totals if math.isfinite(t)])
    axis_parts, y_of = _y_axis(0, vmax, x0, x1, y0, y1, y_title)

    parts = [_svg_open(title), _title_text(title)] + axis_parts
    band = (x1 - x0) / n
    bar_w = band * 0.6

    for i, cat in enumerate(categories):
        cx = x0 + band * (i + 0.5)
        running = 0.0
        for s, (label, values) in enumerate(series.items()):
            v = values[i] if i < len(values) else 0.0
            if not math.isfinite(v) or v <= 0:
                continue
            top, bot = y_of(running + v), y_of(running)
            parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                         f'height="{max(bot - top, 0):.1f}" fill="{PALETTE[s % len(PALETTE)]}" />')
            running += v
        parts.append(f'<text x="{cx:.1f}" y="{y1 + 16}" text-anchor="middle" '
                     f'fill="{_TEXT}">{html.escape(str(cat))}</text>')

    if len(series) > 1:
        for i, label in enumerate(series):
            ly = _MARGIN["top"] + 14 * i
            parts.append(f'<rect x="{x1 - 116:.1f}" y="{ly - 9:.1f}" width="10" height="10" '
                         f'fill="{PALETTE[i % len(PALETTE)]}" />')
            parts.append(f'<text x="{x1 - 102:.1f}" y="{ly:.1f}" fill="{_TITLE}">'
                         f'{html.escape(str(label))}</text>')

    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_AXIS}" />')
    parts.append('</svg>')
    return "".join(parts)


def line_chart(title: str, x: list[float], series: dict[str, list[float]], x_title: str,
              y_title: str, *, band: tuple[list[float], list[float]] | None = None,
              markers: list[float] | None = None,
              x_ticks: list[tuple[float, str]] | None = None) -> str:
    """One line per `series` entry (legend shown when there is more than one).
    `band` is an optional (lb, ub) pair aligned with *x*, drawn as a shaded
    area (e.g. a confidence band around the first/only series). `markers` are
    optional vertical reference lines (e.g. the warm-up cut-off).

    `x_ticks` replaces the numeric tick ladder with explicit (value, label)
    pairs -- the DATE axis of a model with a calendar (PHASE4 T10), whose
    labels come from `core/calendar.date_ticks`. Labels wider than about eight
    characters are slanted so they do not collide."""
    if not x or not series or not any(values for values in series.values()):
        return _empty_chart(title)
    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    all_y = [v for values in series.values() for v in values if math.isfinite(v)]
    if band:
        all_y += [v for v in list(band[0]) + list(band[1]) if math.isfinite(v)]
    vmin = min(all_y) if all_y else 0.0
    vmax = max(all_y) if all_y else 1.0
    xmin, xmax = min(x), max(x)
    axis_parts, y_of = _y_axis(vmin, vmax, x0, x1, y0, y1, y_title)

    def x_of(v: float) -> float:
        return x0 if xmax == xmin else x0 + (v - xmin) / (xmax - xmin) * (x1 - x0)

    parts = [_svg_open(title), _title_text(title)] + axis_parts

    if band is not None:
        lb, ub = band
        up = [(x_of(xv), y_of(v)) for xv, v in zip(x, ub) if math.isfinite(v)]
        down = [(x_of(xv), y_of(v)) for xv, v in zip(reversed(x), reversed(lb)) if math.isfinite(v)]
        if up and down:
            pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in up + down)
            parts.append(f'<polygon points="{pts}" fill="{PALETTE[0]}" fill-opacity="0.15" '
                         f'stroke="none" />')

    for i, (label, values) in enumerate(series.items()):
        colour = PALETTE[i % len(PALETTE)]
        pts = " ".join(f"{x_of(xv):.1f},{y_of(v):.1f}" for xv, v in zip(x, values)
                       if math.isfinite(v))
        if pts:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                         f'stroke-width="2" />')

    for m in markers or []:
        xm = x_of(m)
        parts.append(f'<line x1="{xm:.1f}" y1="{y0}" x2="{xm:.1f}" y2="{y1}" stroke="#e53e3e" '
                     f'stroke-dasharray="4 3" stroke-width="1" />')

    parts.extend(_x_tick_labels(x_ticks, xmin, xmax, x_of, y1))
    if x_title:
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{_H - 8}" text-anchor="middle" '
                     f'fill="{_TEXT}">{html.escape(x_title)}</text>')

    if len(series) > 1:
        for i, label in enumerate(series):
            ly = _MARGIN["top"] + 14 * i
            parts.append(f'<rect x="{x1 - 116:.1f}" y="{ly - 9:.1f}" width="10" height="10" '
                         f'fill="{PALETTE[i % len(PALETTE)]}" />')
            parts.append(f'<text x="{x1 - 102:.1f}" y="{ly:.1f}" fill="{_TITLE}">'
                         f'{html.escape(str(label))}</text>')

    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_AXIS}" />')
    parts.append('</svg>')
    return "".join(parts)


def scatter_chart(title: str, x: list[float], y: list[float], x_title: str,
                  y_title: str, *, diagonal: bool = False) -> str:
    """One point per (x[i], y[i]) pair (ROADMAP 3.7 B item 8: predicted vs
    simulated surrogate points). `diagonal` draws the y = x reference line
    over the combined data range -- what a perfect surrogate would sit on."""
    pairs = [(xv, yv) for xv, yv in zip(x, y) if math.isfinite(xv) and math.isfinite(yv)]
    if not pairs:
        return _empty_chart(title)
    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    vmin, vmax = min(xs + ys), max(xs + ys)
    axis_parts, y_of = _y_axis(min(ys), max(ys), x0, x1, y0, y1, y_title)
    xmin, xmax = min(xs), max(xs)

    def x_of(v: float) -> float:
        return x0 if xmax == xmin else x0 + (v - xmin) / (xmax - xmin) * (x1 - x0)

    parts = [_svg_open(title), _title_text(title)] + axis_parts
    if diagonal:
        parts.append(f'<line x1="{x_of(vmin):.1f}" y1="{y_of(vmin):.1f}" '
                     f'x2="{x_of(vmax):.1f}" y2="{y_of(vmax):.1f}" stroke="{_AXIS}" '
                     f'stroke-dasharray="4 3" />')
    for xv, yv in pairs:
        parts.append(f'<circle cx="{x_of(xv):.1f}" cy="{y_of(yv):.1f}" r="3.5" '
                     f'fill="{PALETTE[0]}" fill-opacity="0.75" />')
    for t in nice_ticks(xmin, xmax, 6):
        if xmin <= t <= xmax:
            xt = x_of(t)
            parts.append(f'<text x="{xt:.1f}" y="{y1 + 16}" text-anchor="middle" '
                         f'fill="{_TEXT}">{fmt_tick(t)}</text>')
    if x_title:
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{_H - 8}" text-anchor="middle" '
                     f'fill="{_TEXT}">{html.escape(x_title)}</text>')
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_AXIS}" />')
    parts.append('</svg>')
    return "".join(parts)


def sankey(rows: list[tuple[str, str, float]], title: str = "Entity flow",
          *, palette: ChartPalette = LIGHT_PALETTE) -> str:
    """Entity flow between blocks (PHASE6 W3 M2, ARCHITECTURE D113): one band
    per (from, to, entities) row, width proportional to entities. Zero-count
    links are dropped first, so a model that never traversed a link draws
    nothing for it.

    Nodes are placed in columns by longest path from a source (a node with no
    incoming link); a cycle just stops a column assignment from improving
    further, since a report chart does not need to solve one. No layout
    solver beyond that -- a real branch/rejoin structure may cross a link or
    two, the same shortcut every other report chart here takes.

    *palette* (item 7): defaults to the fixed light report colours; a caller
    embedding this live in the app passes the active UI theme's colours.
    """
    rows = [(str(frm), str(to), float(n)) for frm, to, n in rows if float(n) > 0]
    if not rows:
        return _empty_chart(title, palette)

    nodes: list[str] = []
    seen: set[str] = set()
    incoming: dict[str, list] = {}
    outgoing: dict[str, list] = {}
    for frm, to, n in rows:
        for name in (frm, to):
            if name not in seen:
                seen.add(name)
                nodes.append(name)
                incoming[name] = []
                outgoing[name] = []
        outgoing[frm].append((to, n))
        incoming[to].append((frm, n))

    # Longest path from a source, cycle-safe: relaxing len(nodes) times is
    # enough for any DAG, and a cycle simply stops improving once relaxed.
    level = {name: 0 for name in nodes}
    for _ in range(len(nodes)):
        changed = False
        for frm, to, _n in rows:
            if level[to] < level[frm] + 1:
                level[to] = level[frm] + 1
                changed = True
        if not changed:
            break

    columns: dict[int, list[str]] = {}
    for name in nodes:
        columns.setdefault(level[name], []).append(name)

    total_in = {name: sum(n for _, n in incoming[name]) for name in nodes}
    total_out = {name: sum(n for _, n in outgoing[name]) for name in nodes}
    weight = {name: max(total_in[name], total_out[name], 1e-9) for name in nodes}

    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]
    n_cols = max(columns) + 1
    col_x = {c: (x0 if n_cols == 1 else x0 + c * (x1 - x0) / (n_cols - 1)) for c in range(n_cols)}
    node_w = 10.0
    gap = 6.0

    # Vertical placement: every column scales against the HEAVIEST column's
    # total weight, so node heights stay comparable across columns instead of
    # each column separately stretching to fill the chart.
    max_col_weight = max(sum(weight[n] for n in names) for names in columns.values())
    node_rect: dict[str, tuple[float, float, float, float]] = {}
    for c, names in columns.items():
        names = sorted(names, key=lambda n: -weight[n])
        col_weight = sum(weight[n] for n in names)
        scale = ((y1 - y0) - gap * max(len(names) - 1, 0)) / max_col_weight if max_col_weight > 0 else 0.0
        cy = y0 + ((y1 - y0) - (col_weight * scale + gap * max(len(names) - 1, 0))) / 2
        for name in names:
            h = max(weight[name] * scale, 2.0)
            node_rect[name] = (col_x[c], cy, node_w, h)
            cy += h + gap

    parts = [_svg_open(title), _title_text(title, palette.title)]

    # Links: a cubic-bezier band between the source's right edge and the
    # target's left edge, stacked within each node's height in row order.
    out_cursor = {name: 0.0 for name in nodes}
    in_cursor = {name: 0.0 for name in nodes}
    for i, (frm, to, n) in enumerate(rows):
        fx, fy, fw, fh = node_rect[frm]
        tx, ty, _tw, th = node_rect[to]
        f_scale = fh / weight[frm] if weight[frm] else 0.0
        t_scale = th / weight[to] if weight[to] else 0.0
        band_f, band_t = n * f_scale, n * t_scale
        sy0 = fy + out_cursor[frm]
        out_cursor[frm] += band_f
        dy0 = ty + in_cursor[to]
        in_cursor[to] += band_t
        sx, dx = fx + fw, tx
        mx = (sx + dx) / 2
        colour = palette.series[i % len(palette.series)]
        path = (f'M{sx:.1f},{sy0:.1f} C{mx:.1f},{sy0:.1f} {mx:.1f},{dy0:.1f} {dx:.1f},{dy0:.1f} '
                f'L{dx:.1f},{dy0 + band_t:.1f} C{mx:.1f},{dy0 + band_t:.1f} {mx:.1f},'
                f'{sy0 + band_f:.1f} {sx:.1f},{sy0 + band_f:.1f} Z')
        parts.append(f'<path d="{path}" fill="{colour}" fill-opacity="0.45" stroke="none">'
                     f'<title>{html.escape(frm)} -&gt; {html.escape(to)}: {n:g}</title></path>')

    for name, (nx, ny, nw, nh) in node_rect.items():
        parts.append(f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{nw:.1f}" height="{nh:.1f}" '
                     f'fill="{palette.title}" />')
        left_half = nx < (x0 + x1) / 2
        label_x = nx + nw + 4 if left_half else nx - 4
        anchor = "start" if left_half else "end"
        parts.append(f'<text x="{label_x:.1f}" y="{ny + nh / 2 + 4:.1f}" text-anchor="{anchor}" '
                     f'fill="{palette.text}">{html.escape(name)}</text>')

    parts.append('</svg>')
    return "".join(parts)


def gantt(rows: list[dict], by: str = "resource", title: str = "Schedule",
         *, palette: ChartPalette = LIGHT_PALETTE) -> str:
    """The "schedule_log" frame (PHASE6 W4 P2, ARCHITECTURE D120) as a Gantt
    chart: one horizontal lane per resource (`by="resource"`) or per order
    (`by="order"`), one `<rect>` per row spanning `[Start, End]`. *rows* are
    plain dicts with the schedule_log column names -- pandas-free, like every
    other chart helper here. A row missing the lane column (no `Order` on a
    model with no order book, when `by="order"`) is dropped.

    Bars are coloured by the OTHER dimension (the order, when grouped by
    resource, and vice versa), so which job holds which lane is visible at a
    glance without a legend.

    *palette* (item 7): see `sankey`.

    ponytail: lane height shrinks with the number of lanes and is unreadable
    past a couple of dozen; a scrollable/paged view is the upgrade path for a
    model with many resources or orders.
    """
    if by not in ("resource", "order"):
        raise ValueError(f'gantt: by must be "resource" or "order", got {by!r}')
    lane_col = "Resource" if by == "resource" else "Order"
    other_col = "Order" if by == "resource" else "Resource"

    rows = [r for r in rows if r.get(lane_col) is not None
           and r.get("Start") is not None and r.get("End") is not None]
    if not rows:
        return _empty_chart(title, palette)

    lanes = sorted({str(r[lane_col]) for r in rows})
    others = sorted({str(r.get(other_col)) for r in rows})
    colour_of = {name: palette.series[i % len(palette.series)] for i, name in enumerate(others)}

    xmin = min(float(r["Start"]) for r in rows)
    xmax = max(float(r["End"]) for r in rows)

    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    def x_of(v: float) -> float:
        return x0 if xmax == xmin else x0 + (v - xmin) / (xmax - xmin) * (x1 - x0)

    band = (y1 - y0) / len(lanes)
    bar_h = band * 0.6
    lane_y = {name: y0 + band * i for i, name in enumerate(lanes)}

    parts = [_svg_open(title), _title_text(title, palette.title)]
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="{palette.axis}" />')
    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{palette.axis}" />')
    parts += _x_tick_labels(None, xmin, xmax, x_of, y1, palette.text)

    for name, ly in lane_y.items():
        parts.append(f'<text x="{x0 - 8:.1f}" y="{ly + band / 2 + 4:.1f}" text-anchor="end" '
                     f'fill="{palette.text}">{html.escape(name)}</text>')

    for r in rows:
        lane, other = str(r[lane_col]), str(r.get(other_col))
        sx, ex = x_of(float(r["Start"])), x_of(float(r["End"]))
        top = lane_y[lane] + (band - bar_h) / 2
        w = max(ex - sx, 1.0)
        title_attr = f"{lane_col} {lane}, {other_col} {other}: {r['Start']:g} - {r['End']:g}"
        parts.append(f'<rect x="{sx:.1f}" y="{top:.1f}" width="{w:.1f}" height="{bar_h:.1f}" '
                     f'fill="{colour_of[other]}" fill-opacity="0.85">'
                     f'<title>{html.escape(title_attr)}</title></rect>')

    parts.append('</svg>')
    return "".join(parts)


def histogram(title: str, edges: list[float], counts: list[int], x_title: str,
             y_title: str) -> str:
    """A pre-binned histogram: *edges* has len(counts) + 1 bin boundaries."""
    if not counts or len(edges) < 2:
        return _empty_chart(title)
    x0, x1 = _MARGIN["left"], _W - _MARGIN["right"]
    y0, y1 = _MARGIN["top"], _H - _MARGIN["bottom"]

    vmax = max(counts) if counts else 1
    axis_parts, y_of = _y_axis(0, vmax, x0, x1, y0, y1, y_title)
    xmin, xmax = edges[0], edges[-1]

    def x_of(v: float) -> float:
        return x0 if xmax == xmin else x0 + (v - xmin) / (xmax - xmin) * (x1 - x0)

    parts = [_svg_open(title), _title_text(title)] + axis_parts
    for i, c in enumerate(counts):
        left, right = x_of(edges[i]), x_of(edges[i + 1])
        top = y_of(c)
        parts.append(f'<rect x="{left:.1f}" y="{top:.1f}" width="{max(right - left - 1, 0):.1f}" '
                     f'height="{max(y1 - top, 0):.1f}" fill="{PALETTE[2]}" />')

    for t in nice_ticks(xmin, xmax, 6):
        if xmin <= t <= xmax:
            xt = x_of(t)
            parts.append(f'<text x="{xt:.1f}" y="{y1 + 16}" text-anchor="middle" '
                         f'fill="{_TEXT}">{fmt_tick(t)}</text>')
    if x_title:
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{_H - 8}" text-anchor="middle" '
                     f'fill="{_TEXT}">{html.escape(x_title)}</text>')

    parts.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_AXIS}" />')
    parts.append('</svg>')
    return "".join(parts)
