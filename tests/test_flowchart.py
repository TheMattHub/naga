"""Flowchart geometry, SVG, text and PNG (AI-run studies, seeing)."""
import json
import time
from pathlib import Path

from nagasim import flowchart

MODEL = Path(__file__).resolve().parent.parent / "nagasim" / "models" / "single_server.json"


def _layout():
    return json.loads(MODEL.read_text(encoding="utf-8"))


def _state(geom):
    return {n["name"]: n["state"] for n in geom["nodes"]}


def test_single_server_draws_three_built_nodes_and_two_edges():
    g = flowchart.geometry(_layout())
    assert [n["name"] for n in g["nodes"]] == ["create_1", "process_1", "dispose_1"]
    assert set(_state(g).values()) == {"built"}
    assert len(g["edges"]) == 2
    assert min(n["x"] for n in g["nodes"]) == 20 and min(n["y"] for n in g["nodes"]) == 20
    assert all(n["w"] == 140 and n["h"] == 48 for n in g["nodes"])


def test_changed_and_pending_states():
    pending = [{"name": "Packing", "purpose": "pack", "section": ""}]
    g = flowchart.geometry(_layout(), {"process_1"}, pending)
    assert _state(g) == {"create_1": "built", "process_1": "changed",
                         "dispose_1": "built", "Packing": "pending"}
    right = max(n["x"] + n["w"] for n in g["nodes"] if n["state"] != "pending")
    assert [n["x"] for n in g["nodes"] if n["state"] == "pending"][0] > right
    svg = flowchart.to_svg(g)
    assert "stroke-dasharray" in svg and "Packing" in svg


def test_blocks_without_positions_are_laid_out():
    lay = {"blocks": [{"name": "a", "type": "CREATE"}, {"name": "b", "type": "PROCESS"},
                      {"name": "c", "type": "DISPOSE"}],
           "links": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]}
    g = flowchart.geometry(lay)
    xs = [n["x"] for n in g["nodes"]]
    assert len(set(xs)) == 3 and xs == sorted(xs)


def test_names_are_escaped_in_svg():
    lay = {"blocks": [{"name": 'a<b>&"c"', "type": "CREATE", "position": [0, 0]}], "links": []}
    svg = flowchart.to_svg(flowchart.geometry(lay), title="x<y")
    assert "a<b>" not in svg and "a&lt;b&gt;&amp;" in svg and "x&lt;y" in svg


def test_link_to_missing_block_is_skipped():
    lay = _layout()
    lay["links"].append({"source": "process_1", "target": "ghost"})
    assert len(flowchart.geometry(lay)["edges"]) == 2


def test_svg_is_deterministic():
    g = flowchart.geometry(_layout(), {"create_1"})
    assert flowchart.to_svg(g, "t") == flowchart.to_svg(g, "t")
    assert flowchart.to_svg(g).startswith("<svg")


def test_text_lists_nodes_states_and_links():
    g = flowchart.geometry(_layout(), {"process_1"}, [{"name": "Packing", "purpose": "", "section": ""}])
    t = flowchart.to_text(g)
    for s in ("create_1", "process_1 (PROCESS) changed", "Packing", "pending", "create_1 -> process_1"):
        assert s in t
    assert t.isascii()


def test_png_is_a_png_or_none():
    png = flowchart.to_png(flowchart.geometry(_layout()), "t")
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        assert png is None
    else:
        assert png.startswith(b"\x89PNG")


def test_large_model_stays_small_and_fast():
    n = 200
    lay = {"blocks": [{"name": f"b{i}", "type": "PROCESS", "position": [(i % 20) * 180, (i // 20) * 80]}
                      for i in range(n)],
           "links": [{"source": f"b{i}", "target": f"b{i + 1}"} for i in range(n - 1)]}
    g = flowchart.geometry(lay)
    assert len(flowchart.to_svg(g)) < 300_000
    t0 = time.perf_counter()
    png = flowchart.to_png(g)
    assert png is None or time.perf_counter() - t0 < 3


def test_changed_blocks_parses_change_lines():
    ch = ["added blocks.q1", "changed blocks.p1.params.rate", "removed blocks.old", "changed info.x"]
    assert flowchart.changed_blocks(ch) == {"q1", "p1"}
