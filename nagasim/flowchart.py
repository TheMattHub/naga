"""Flowchart of a model for AI-run studies: geometry once, then SVG, text or PNG.

Pure (no Qt, no file I/O). Three node states: "built", "changed" (added or changed
in this step) and "pending" (a target component with no blocks yet).
"""
from __future__ import annotations

import html
import io

from nagasim.core.document import ModelDocument
from nagasim.core.layout_tools import auto_layout

__all__ = ["geometry", "to_svg", "to_text", "to_png", "changed_blocks"]

NODE_W, NODE_H, MARGIN, SECTION_PAD = 140, 48, 20, 16
PENDING_GAP, PENDING_STEP = 60, 72


def changed_blocks(changes: list[str]) -> set[str]:
    """Block names from change lines "<kind> blocks.<name>[...]"; removed blocks are not drawn."""
    out = set()
    for line in changes:
        kind, _, path = line.partition(" ")
        if kind in ("added", "changed") and path.startswith("blocks."):
            out.add(path[len("blocks."):].split(".")[0].split("[")[0])
    return out


def geometry(layout: dict, changed=frozenset(), pending=()) -> dict:
    blocks = [b for b in layout.get("blocks", []) if b.get("name")]
    names = {b["name"] for b in blocks}
    edges = [{"source": l["source"], "target": l["target"]} for l in layout.get("links", [])
             if l.get("source") in names and l.get("target") in names]

    pos = {b["name"]: tuple(b["position"]) for b in blocks if b.get("position")}
    if len(pos) < len(blocks):
        # Only when something lacks a position: the layout engine wants a ModelDocument, and
        # a model whose links all resolve, so hand it the cleaned copy.
        auto = auto_layout(ModelDocument({**layout, "blocks": blocks, "links": edges}))
        for n in names:
            pos.setdefault(n, auto.get(n, (0.0, 0.0)))

    minx = min((p[0] for p in pos.values()), default=0)
    miny = min((p[1] for p in pos.values()), default=0)
    section_of = {}
    for sname, sub in (layout.get("submodels") or {}).items():
        for b in sub.get("blocks", []):
            section_of.setdefault(b.get("name"), sname)

    nodes = [{"name": b["name"], "type": b.get("type", ""),
              "x": round(pos[b["name"]][0] - minx + MARGIN), "y": round(pos[b["name"]][1] - miny + MARGIN),
              "w": NODE_W, "h": NODE_H, "state": "changed" if b["name"] in changed else "built",
              "section": section_of.get(b["name"], "")} for b in blocks]

    right = max((n["x"] + n["w"] for n in nodes), default=0)
    for i, c in enumerate(pending):
        nodes.append({"name": c["name"], "type": "", "x": right + PENDING_GAP,
                      "y": MARGIN + i * PENDING_STEP, "w": NODE_W, "h": NODE_H,
                      "state": "pending", "section": c.get("section", "")})

    sections = []
    for sname in dict.fromkeys(n["section"] for n in nodes if n["section"]):
        mem = [n for n in nodes if n["section"] == sname]
        x0 = min(n["x"] for n in mem) - SECTION_PAD
        y0 = min(n["y"] for n in mem) - SECTION_PAD
        sections.append({"name": sname, "x": x0, "y": y0,
                         "w": max(n["x"] + n["w"] for n in mem) + SECTION_PAD - x0,
                         "h": max(n["y"] + n["h"] for n in mem) + SECTION_PAD - y0})

    return {"width": max((n["x"] + n["w"] for n in nodes), default=0) + MARGIN,
            "height": max((n["y"] + n["h"] for n in nodes), default=0) + MARGIN,
            "nodes": nodes, "edges": edges, "sections": sections}


def _ends(geom):
    by = {n["name"]: n for n in geom["nodes"]}
    for e in geom["edges"]:
        s, t = by[e["source"]], by[e["target"]]
        yield (s["x"] + s["w"], s["y"] + s["h"] / 2, t["x"], t["y"] + t["h"] / 2)


def to_svg(geom: dict, title: str = "") -> str:
    esc = html.escape
    dy = 24 if title else 0
    w, h = geom["width"], geom["height"] + dy
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
           f'font-family="sans-serif" font-size="12">',
           f'<defs><marker id="a" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="8" markerHeight="8" '
           f'orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#555"/></marker></defs>',
           f'<rect width="{w}" height="{h}" fill="#ffffff"/>']
    if title:
        out.append(f'<text x="{MARGIN}" y="16" font-size="14" fill="#222">{esc(title)}</text>')
    for s in geom["sections"]:
        out.append(f'<rect x="{s["x"]}" y="{s["y"] + dy}" width="{s["w"]}" height="{s["h"]}" fill="none" '
                   f'stroke="#999" stroke-dasharray="2 4"/><text x="{s["x"] + 4}" y="{s["y"] + dy + 12}" '
                   f'fill="#777">{esc(s["name"])}</text>')
    for x1, y1, x2, y2 in _ends(geom):
        out.append(f'<line x1="{x1:g}" y1="{y1 + dy:g}" x2="{x2:g}" y2="{y2 + dy:g}" stroke="#555" marker-end="url(#a)"/>')
    for n in geom["nodes"]:
        style = {"built": 'fill="#dbe7f5" stroke="#4a6a8f"',
                 "changed": 'fill="#dbe7f5" stroke="#e07b00" stroke-width="3"',
                 "pending": 'fill="#f4f4f4" stroke="#aaa" stroke-dasharray="6 4" opacity="0.7"'}[n["state"]]
        cx = n["x"] + n["w"] / 2
        out.append(f'<rect x="{n["x"]}" y="{n["y"] + dy}" width="{n["w"]}" height="{n["h"]}" rx="6" {style}/>'
                   f'<text x="{cx:g}" y="{n["y"] + dy + 20}" text-anchor="middle" '
                   f'fill="#222">{esc(n["name"])}</text>'
                   f'<text x="{cx:g}" y="{n["y"] + dy + 36}" text-anchor="middle" '
                   f'fill="#666" font-size="10">{esc(n["type"] or n["state"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def to_text(geom: dict) -> str:
    lines = ["Nodes:"]
    for n in geom["nodes"]:
        typ = f" ({n['type']})" if n["type"] else ""
        sec = f" [{n['section']}]" if n["section"] else ""
        lines.append(f"- {n['name']}{typ} {n['state']}{sec}")
    lines.append("Links:")
    lines += [f"- {e['source']} -> {e['target']}" for e in geom["edges"]] or ["- none"]
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


def to_png(geom: dict, title: str = "") -> bytes | None:
    """PNG through matplotlib (nagasim[plot]); None when it is not installed. Figure +
    FigureCanvasAgg directly, so a backend the caller already chose is left alone."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.patches import Rectangle
    except ImportError:
        return None
    w, h = max(geom["width"], 1), max(geom["height"], 1)
    px_w = min(w, 1600)
    draw_h = max(h * px_w / w, 100)
    top = 30 if title else 0      # pixels kept for the title, so a flat drawing does not clip it
    fig = Figure(figsize=(px_w / 100, (draw_h + top) / 100), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, draw_h / (draw_h + top)))
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.axis("off")
    if title:
        fig.text(0.01, 1 - 15 / (draw_h + top), title, fontsize=9, va="center")
    for s in geom["sections"]:
        ax.add_patch(Rectangle((s["x"], s["y"]), s["w"], s["h"], fill=False, ec="#999", ls=":"))
    for x1, y1, x2, y2 in _ends(geom):
        ax.annotate("", (x2, y2), (x1, y1), arrowprops={"arrowstyle": "-|>", "color": "#555", "lw": 0.8})
    size = 6 if len(geom["nodes"]) > 40 else 8
    for n in geom["nodes"]:
        st = n["state"]
        ax.add_patch(Rectangle((n["x"], n["y"]), n["w"], n["h"],
                               fc="#f4f4f4" if st == "pending" else "#dbe7f5",
                               ec={"built": "#4a6a8f", "changed": "#e07b00", "pending": "#aaa"}[st],
                               lw=2.5 if st == "changed" else 1, ls="--" if st == "pending" else "-",
                               alpha=0.7 if st == "pending" else 1))
        ax.text(n["x"] + n["w"] / 2, n["y"] + n["h"] / 2, n["name"], ha="center", va="center", fontsize=size)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    return buf.getvalue()
