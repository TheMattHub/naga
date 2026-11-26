"""`nagasim.appearance`: the public calls an outside renderer draws a model with.

The last tests draw from the file alone, through `nagasim.appearance` and `ModelDocument`, and
compare with the two committed oracles: the item forms census and the kit colours fingerprint.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from nagasim import appearance
from nagasim.core import conveyor_look, conveyor_path, document, entity_forms, floor, look_materials
from nagasim.core.document import ModelDocument

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).parent / "data"


# The calls the module defines itself: read-only wrappers of the floor geometry (final review I1).
OWN = ("piece_start", "piece_segments", "piece_pose", "piece_length_m")


def _home(name):
    for module in (look_materials, conveyor_look, floor, entity_forms, conveyor_path, document):
        if name in getattr(module, "__all__", ()) or (module in (floor, conveyor_path, document)
                                                      and hasattr(module, name)):
            return module
    raise AssertionError(name)


def _doc(blocks, simulation=None) -> ModelDocument:
    data = {"blocks": blocks, "links": []}
    if simulation:
        data["simulation"] = simulation
    return ModelDocument(data)


def test_the_public_names_are_the_engines():
    assert "SLOTS" not in appearance.__all__
    assert {"linear_rgb", "is_hex", "look_of", "forms_of"} <= set(appearance.__all__)
    for name in appearance.__all__:
        if name in OWN:
            assert getattr(appearance, name).__module__ == "nagasim.appearance", name
        else:
            assert getattr(appearance, name) is getattr(_home(name), name), name


def test_the_module_is_listed_and_provisional():
    import nagasim
    assert "appearance" in nagasim._SUBMODULES
    assert "provisional" in appearance.__doc__ and "metres" in appearance.__doc__
    assert "Naga" not in appearance.__doc__


def test_public_docstrings_carry_no_ids():
    for name in appearance.__all__:
        doc = getattr(appearance, name).__doc__ or ""
        assert doc.strip() and not re.search(r"\bD\d{3,4}\b|#\d{4}\b|\bplan [A-Z]\b", doc), name


def _words(name) -> str:
    return " ".join((getattr(appearance, name).__doc__ or "").split())


def test_the_docstrings_tell_an_outside_renderer():
    for name in ("srgb", "linear_rgb", "material", "guide_material"):
        assert "sRGB" in _words(name) and "linear_rgb(srgb(" in _words(name), name
    # MATERIALS is a dict and has no docstring of its own: the module and material() say it
    assert "guide_material()" in _words("material")
    assert "guide_material()" in " ".join(appearance.__doc__.split())
    assert "ValueError" in _words("material") and "ValueError" in _words("guide_material")
    paints = _words("material")
    for words in ("rails, end plates, legs, guide posts and panel guides",
                  "belt bed, pulley and end plates", "roller tubes", "round and flat guide rails",
                  "cap"):
        assert words in paints, words
    assert "always paint" in paints and "#5E646B" in paints
    # the placement rules are written once in the engine, in look_of; default points there
    assert "look_of" in _words("default") and "beside the frame" not in _words("default")
    drive = _words("look_of")
    assert "legs" in drive and "beside the frame" in drive
    assert "more than 0.3 m past the last leg" in drive


def test_wrong_blocks_have_no_look():
    doc = _doc([{"name": "U", "type": "CONVEYOR", "params": {}},
                {"name": "P", "type": "PROCESS", "params": {}}])
    assert appearance.look_of(doc.blocks["U"].params) == {}
    assert appearance.exit_openings(doc, "P") == appearance.exit_openings(doc, "missing") == []


def test_material_refuses_an_unknown_value():
    with pytest.raises(ValueError, match="black_rubber"):
        appearance.material("belt_kind", "#123456")
    with pytest.raises(ValueError):
        appearance.guide_material("rope", "steel")


def test_colour_helpers():
    assert appearance.is_hex("#5e646b") and not appearance.is_hex("#abc")
    assert appearance.srgb("#FF0000") == (1.0, 0.0, 0.0)
    assert appearance.linear_rgb(appearance.srgb("#808080"))[0] == pytest.approx(0.2158605)
    assert appearance.material("frame_color", "#5E646B").metallic == 0.15


def test_a_piece_drawn_from_the_file_alone():
    # a 2000 mm roller piece in a millimetre model, an opening on the left from 500 to 900 mm
    doc = _doc([{"name": "R", "type": "CONVEYOR", "params": {
        "type": "roller", "path": [{"straight": 2000}],
        "look": {"guide_left": 0.1, "openings": [{"side": "left", "start": 500, "end": 900}]}}}],
        simulation={"length_unit": "millimeters"})
    params = doc.blocks["R"].params
    look = appearance.look_of(params, appearance.length_factor(doc, params))
    assert look["openings"] == [{"side": "left", "start": 0.5, "end": 0.9}]
    assert appearance.guide_runs(2.0, look["openings"], "left") == [(0.0, 0.5), (0.9, 2.0)]


def test_the_items_of_a_bundled_model_are_the_census():
    census = json.loads((DATA / "item_forms_census.json").read_text(encoding="utf-8"))
    want = census["conveyor_line.json"]
    doc = ModelDocument.load(ROOT / "nagasim/models/conveyor_line.json")
    forms = appearance.forms_of(doc)
    assert sorted(forms) == sorted(k for k in want if k != "fallback")
    for key, item in forms.items():
        size = None if item.size is None else [round(float(x), 9) for x in item.size]
        assert [item.form, size] == want[key][:2], key
    assert [round(x, 9) for x in appearance.default_carton_size(doc)] == want["fallback"]


def _srgb_channel(c: float) -> float:
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def test_the_kit_colours_of_a_bundled_model_from_the_file_alone():
    # Each piece of conveyor_line takes every recorded non-default choice of its type; the colour
    # the reference drawing gave it is in the fingerprint as linear RGB, with a painted frame's
    # metallic and roughness.
    oracle = json.loads((DATA / "pallet_kit_fingerprint.json").read_text(encoding="utf-8"))
    doc = ModelDocument.load(ROOT / "nagasim/models/conveyor_line.json")
    kinds = {b.params.get("type") for b in doc.blocks.values() if b.type == "CONVEYOR"}
    checked = 0
    for case, entries in oracle["colours"].items():
        kind, raw = case.split("|", 1)
        if kind not in kinds:
            continue
        block = next(b for b in doc.blocks.values()
                     if b.type == "CONVEYOR" and b.params.get("type") == kind)
        params = {**block.params, "look": json.loads(raw)}
        look = appearance.look_of(params, appearance.length_factor(doc, params))
        drawn = set()
        for field in ("frame_color", "belt_kind", "roller_finish"):
            if field in json.loads(raw):
                drawn.add(appearance.material(field, look[field]))
        if look["guide_style"] == "panel":
            drawn.add(appearance.guide_material("panel", look["frame_color"]))
        want = [json.loads(e) for e in entries]
        assert len(drawn) == len(want), case
        for mat in drawn:
            rgb = appearance.linear_rgb(appearance.srgb(mat.color))
            match = [e for e in want if all(abs(_srgb_channel(a) - _srgb_channel(b)) <= 0.5 / 255
                                            + 1e-9 for a, b in zip(rgb, e[1]))]
            assert match, (case, mat)
            if len(match[0]) > 2:
                assert match[0][2:] == [mat.metallic, mat.roughness], case
        checked += 1
    assert checked >= 20


def test_a_piece_is_placed_from_the_public_calls_alone():
    """An outside drawing places a piece with nagasim.appearance only: its start pose, its
    segments in metres, a pose along it and its length (final review I1)."""
    doc = appearance.ModelDocument({"simulation": {"length_unit": "millimeters"}, "blocks": [
        {"name": "R", "type": "CONVEYOR", "layout_position": [1.0, 2.0], "layout_rotation": 90,
         "params": {"type": "roller", "path": [
             {"straight": 2000}, {"arc": {"radius": 1000, "angle": 90, "turn": "left"}}]}},
        {"name": "Flat", "type": "CONVEYOR", "params": {"type": "roller", "length": 3}},
        {"name": "P", "type": "PROCESS", "params": {}}]})
    assert appearance.piece_start(doc, "R") == appearance.Pose(1.0, 2.0, 90.0)
    segments = appearance.piece_segments(doc, "R")
    assert [(s.kind, s.radius, s.turn) for s in segments] == [("straight", 0.0, ""), ("arc", 1.0, "left")]
    length = appearance.piece_length_m(doc, "R")
    assert length == pytest.approx(2.0 + math.pi / 2)
    assert appearance.piece_pose(doc, "R", 1.0) == pytest.approx(appearance.Pose(1.0, 3.0, 90.0))
    # a left turn from heading south (90) ends heading east (0), a radius further east
    end = appearance.piece_pose(doc, "R", length)
    assert tuple(end) == pytest.approx((2.0, 5.0, 0.0))
    assert appearance.piece_pose(doc, "R", 99) == end and appearance.piece_pose(doc, "R", -1) ==         appearance.piece_start(doc, "R")
    for name in ("Flat", "P", "missing"):       # no path, not a conveyor, no block
        assert appearance.piece_start(doc, name) is None
        assert appearance.piece_segments(doc, name) is None
        assert appearance.piece_pose(doc, name, 1.0) is None
        assert appearance.piece_length_m(doc, name) is None
    # the openings and guide runs line up with the same length
    look = appearance.look_of({"type": "roller", "look": {"openings": [
        {"side": "left", "start": 1, "end": 2}]}})
    assert appearance.guide_runs(length, look["openings"], "left")[-1] == (2, length)
