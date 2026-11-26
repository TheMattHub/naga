"""The engine's material table for conveyor looks (plan I, Task 2)."""
import pytest

from nagasim.core import conveyor_look as cl
from nagasim.core import look_materials as lm


def test_every_choice_has_a_material():
    for field, names in (("frame_color", cl.FRAME_COLORS), ("belt_kind", cl.BELT_KINDS),
                         ("roller_finish", cl.ROLLER_FINISHES), ("guide_style", cl.GUIDE_STYLES)):
        assert set(lm.MATERIALS[field]) == set(names)
        assert all(lm.is_hex(m.color) and 0 <= m.metallic <= 1 and 0 <= m.roughness <= 1
                   for m in lm.MATERIALS[field].values())


def test_the_defaults_are_the_baked_slots():
    assert lm.MATERIALS["frame_color"]["steel"] == lm.Material("#5E646B", 0.85, 0.42)
    assert lm.MATERIALS["belt_kind"]["black_rubber"] == lm.Material("#1C1E21", 0.0, 0.88)
    assert lm.MATERIALS["roller_finish"]["zinc"] == lm.Material("#A7AEB4", 0.75, 0.5)


def test_coloured_frames_are_paint_steel_and_zinc_are_metal():
    for name in ("grey", "blue", "green", "yellow", "orange"):
        assert lm.MATERIALS["frame_color"][name].metallic == 0.15
    assert lm.material("frame_color", "#12abEF") == lm.Material("#12abEF", 0.15, 0.42)
    assert lm.MATERIALS["frame_color"]["steel"].metallic == 0.85
    assert lm.MATERIALS["roller_finish"]["zinc"].metallic == 0.75


def test_srgb_and_guides():
    assert lm.srgb("#FF0080") == (1.0, 0.0, 128 / 255)
    assert lm.guide_material("panel", "blue") == lm.MATERIALS["frame_color"]["blue"]
    assert lm.guide_material("panel", "#102030") == lm.Material("#102030", 0.15, 0.42)
    assert lm.guide_material("round", "blue") == lm.MATERIALS["guide_style"]["round"]


def test_linear_rgb_is_the_srgb_curve():
    assert lm.linear_rgb((0.0, 1.0, 0.5)) == pytest.approx((0.0, 1.0, 0.2140411), abs=1e-6)
    assert lm.linear_rgb((0.04045,))[0] == pytest.approx(0.04045 / 12.92)     # the toe
    assert lm.linear_rgb(lm.srgb("#5E646B")) == pytest.approx((0.1119324, 0.1274377, 0.1470273), abs=1e-6)


def test_the_first_choice_of_each_table_is_the_default():
    for field in ("frame_color", "belt_kind", "roller_finish", "guide_style"):
        assert next(iter(lm.MATERIALS[field])) == cl.default("belt", field), field


def test_refusals():
    with pytest.raises(ValueError, match="frame_color"):
        lm.material("height", "x")
    with pytest.raises(ValueError, match="panel"):
        lm.guide_material("bogus", "steel")
    with pytest.raises(ValueError, match="#RRGGBB"):
        lm.material("frame_color", "#abc")
    with pytest.raises(ValueError) as e:
        lm.material("belt_kind", "#123456")
    assert "#RRGGBB" not in str(e.value)
    with pytest.raises(ValueError):
        lm.srgb("5E646B")
