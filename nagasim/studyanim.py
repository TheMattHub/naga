"""An animated GIF of one replication of a study version, drawn on the flowchart geometry.

The run is recorded with the engine's own trace (a `TraceRecorder`, as `experiments.record_scenarios`
does) and read back through `playback.Timeline`, the same reader the HTML player uses. Needs
matplotlib (nagasim[plot]); Pillow comes with it.
"""
from __future__ import annotations

import io
import json
import os
import uuid
from pathlib import Path

from nagasim import api, experiments, flowchart
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder
from nagasim.study import StudyError, version_id

__all__ = ["gif", "MAX_BYTES", "MAX_WIDTH"]

MAX_BYTES, MAX_WIDTH = 1_000_000, 900
_PLOT = "pip install 'nagasim[plot]'"


def _imports():
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.patches import Rectangle
        from PIL import Image
    except ImportError:
        raise StudyError("E_STUDY_NEEDS_EXTRA", "the animation needs matplotlib", _PLOT) from None
    return FigureCanvasAgg, Figure, Rectangle, Image


def _record(layout: dict, scenario: str | None, seed: int) -> tuple[dict, TraceRecorder]:
    if scenario:
        names = [s.get("name") for s in layout.get("scenarios") or [] if isinstance(s, dict)]
        if scenario not in names:
            raise StudyError("E_STUDY_BAD_VALUE", f"no scenario {scenario!r}", f"Use one of {names}.")
        layout = experiments._scenario_layouts(layout, [scenario], baseline=False)[scenario]
    _n, length, warm = experiments._window(layout, 1, None, None)
    recorder = TraceRecorder()
    api.run_single_simulation(layout, seed=seed, run_len=length, warmup=warm, subscribers=(recorder,))
    return layout, recorder


def _sample(timeline: Timeline, layout: dict, times: list[float]) -> list[dict]:
    """Per time: {block: (waiting, in service, utilisation 0..1)}."""
    resource = {b["name"]: (b.get("params") or {}).get("resource_name") for b in layout.get("blocks", [])}
    out = []
    for t in times:
        state = timeline.state_at(t)
        resources = state.get("resources") or {}
        rows = {}
        for name, res in resource.items():
            n, serving = state["counts"].get(name, 0), state["in_service"].get(name, 0)
            info = resources.get(res) or {}
            util = (info.get("busy", 0) / info["capacity"]) if info.get("capacity") else min(serving, 1)
            rows[name] = (max(n - serving, 0), serving, min(util, 1.0))
        out.append(rows)
    return out


def _render(geom: dict, rows: dict, clock: str, width: int, parts) -> "Image":
    canvas, Figure, Rectangle, Image = parts
    w, h = max(geom["width"], 1), max(geom["height"], 1)
    height = max(int(width * h / w) + 24, 80)
    fig = Figure(figsize=(width / 100, height / 100), dpi=100)
    canvas(fig)
    ax = fig.add_axes((0, 0, 1, 1 - 24 / height))
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.axis("off")
    fig.text(0.01, 1 - 12 / height, clock, fontsize=8, va="center")
    by = {n["name"]: n for n in geom["nodes"]}
    for e in geom["edges"]:
        s, t = by[e["source"]], by[e["target"]]
        ax.annotate("", (t["x"], t["y"] + t["h"] / 2), (s["x"] + s["w"], s["y"] + s["h"] / 2),
                    arrowprops={"arrowstyle": "-|>", "color": "#555", "lw": 0.8})
    size = 6 if len(geom["nodes"]) > 40 else 8
    for n in geom["nodes"]:
        waiting, serving, util = rows.get(n["name"], (0, 0, 0.0))
        shade = 0.9 - 0.5 * util        # idle: light blue, fully busy: darker
        ax.add_patch(Rectangle((n["x"], n["y"]), n["w"], n["h"], fc=(shade * 0.8, shade * 0.9, 1.0),
                               ec="#4a6a8f"))
        ax.text(n["x"] + n["w"] / 2, n["y"] + n["h"] / 2, n["name"], ha="center", va="center", fontsize=size)
        if waiting or serving:
            ax.text(n["x"] + n["w"] - 3, n["y"] + 3, f"{waiting} wait / {serving} busy", ha="right",
                    va="top", fontsize=size - 1, color="#a30000")
    fig.canvas.draw()
    return Image.frombuffer("RGBA", fig.canvas.get_width_height(), bytes(fig.canvas.buffer_rgba()),
                            "raw", "RGBA", 0, 1).convert("P", palette=Image.ADAPTIVE)


def gif(folder, study: dict, version: str | None = None, scenario: str | None = None,
        frames: int = 40) -> tuple[Path, int]:
    """Write `versions/<id>/animation.gif` (<= MAX_BYTES, <= MAX_WIDTH px wide) and answer (its path, the frame count used).
    The run is the version's first replication (its stored seed), so the same folder gives the
    same animation."""
    parts = _imports()
    folder, frames = Path(folder), max(int(frames), 2)
    base = folder / "versions" / version_id(study, version)
    layout = json.loads((base / "model.json").read_text(encoding="utf-8"))
    seed = (json.loads((base / "step.json").read_text(encoding="utf-8"))).get("seed") or 0
    layout, recorder = _record(layout, scenario, seed)
    timeline = Timeline(recorder.events)
    times = [timeline.duration * k / (frames - 1) for k in range(frames)]
    rows = _sample(timeline, layout, times)
    geom = flowchart.geometry(layout)
    width = MAX_WIDTH
    while True:
        images = [_render(geom, r, f"t = {t:,.1f}", width, parts) for r, t in zip(rows, times)]
        buf = io.BytesIO()
        images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
        if buf.tell() <= MAX_BYTES or width < 300:
            break
        width = int(width * 0.75)       # a long model on a big canvas: shrink until it fits
    target = base / "animation.gif"
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_bytes(buf.getvalue())
    os.replace(temporary, target)
    return target, frames
