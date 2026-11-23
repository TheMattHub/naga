"""Pallet patterns: the record, its slot poses, its checks and the layer generator."""
import copy
import json

import pytest

from robotcases import ROOT

from nagasim.core import schema
from nagasim.core.pallets import Slot, generate_layer, interlock, pattern_records, slots


def pattern(**changes) -> dict:
    record = {"name": "P1",
              "pallet": {"length": 1.2, "width": 0.8, "deck_height": 0.144, "tare_kg": 25},
              "item": {"length": 0.4, "width": 0.3, "height": 0.25, "weight_kg": 12},
              "layers": {"A": [{"x": -0.2, "y": 0, "rotation": 0}, {"x": 0.2, "y": 0, "rotation": 90}]},
              "stack": [{"layer": "A"}, {"layer": "A", "transform": "rotate180"}],
              "sheet": {"thickness": 0.002, "time": 4},
              "limits": {"layers": 3, "items": None, "weight_kg": None, "height": None}}
    for path, value in changes.items():
        *parents, last = path.split(".")
        here = record
        for part in parents:
            here = here[part]
        here[last] = value
    return record


def with_patterns(*records) -> dict:
    doc = json.loads((ROOT / "nagasim" / "models" / "single_server.json").read_text(encoding="utf-8"))
    doc["pallet_patterns"] = copy.deepcopy(list(records))
    return doc


def pallet_problems(layout) -> list:
    return [p for p in schema.validate_model_structured(layout) if p.code.startswith("E_PALLET")]


def test_slots_repeat_the_stack_with_transforms():
    out = slots(pattern())
    assert len(out) == 6
    assert [s.index for s in out] == [1, 2, 3, 4, 5, 6]
    assert [s.layer for s in out] == [1, 1, 2, 2, 3, 3]
    first_of_two = out[2]
    assert (first_of_two.x, first_of_two.y, first_of_two.rotation) == pytest.approx((0.2, 0, 180))
    assert out[0].z == pytest.approx(0.144 + 0.125)
    assert out[2].z == pytest.approx(0.144 + 0.25 + 0.002 + 0.125)
    assert out[4].rotation == pytest.approx(0)              # the stack repeats from A
    assert isinstance(out[0], Slot)


def test_height_limit_drops_the_layer_that_would_pass_it():
    record = pattern(**{"limits.layers": None, "limits.height": 0.7, "sheet": None})
    assert max(s.layer for s in slots(record)) == 2


def test_items_limit_stops_mid_layer():
    assert len(slots(pattern(**{"limits.items": 5}))) == 5


def test_mirrors_turn_the_rotation_too():
    record = pattern(stack=[{"layer": "A", "transform": "mirror_x"}], **{"limits.layers": 1})
    assert [(s.x, s.y, s.rotation) for s in slots(record)] == pytest.approx(
        [(0.2, 0, 180), (-0.2, 0, 90)])
    record = pattern(stack=[{"layer": "A", "transform": "mirror_y"}], **{"limits.layers": 1})
    assert [(s.x, s.y, s.rotation) for s in slots(record)] == pytest.approx(
        [(-0.2, 0, 0), (0.2, 0, 270)])


def test_a_valid_pattern_has_no_problem():
    assert pallet_problems(with_patterns(pattern())) == []
    assert set(pattern_records(with_patterns(pattern(), pattern(**{"limits.layers": 9})))) == {"P1"}
    assert pattern_records(with_patterns(pattern(), pattern(**{"limits.layers": 9})))["P1"][
        "limits"]["layers"] == 3        # the first of a repeated name


@pytest.mark.parametrize("record, code", [
    (pattern(**{"layers.A": [{"x": 0, "y": 0}, {"x": 0.1, "y": 0}]}), "E_PALLET_OVERLAP"),
    (pattern(**{"layers.A": [{"x": 0.45, "y": 0}]}), "E_PALLET_OVERHANG"),
    (pattern(stack=[]), "E_PALLET_EMPTY"),
    (pattern(**{"limits.layers": None, "limits.height": 0.2}), "E_PALLET_EMPTY"),
    (pattern(**{"limits.layers": None}), "E_PALLET_EMPTY"),
    (pattern(**{"pallet.length": 0}), "E_PALLET_SHAPE"),
    (pattern(stack=[{"layer": "B"}]), "E_PALLET_SHAPE"),
    (pattern(stack=[{"layer": "A", "transform": "flip"}]), "E_PALLET_SHAPE"),
])
def test_overlap_overhang_and_empty_are_coded(record, code):
    found = pallet_problems(with_patterns(record))
    assert [p.code for p in found] == [code]
    assert not found[0].block
    assert found[0].path.startswith("/pallet_patterns/0") and "P1" in found[0].message


def test_overhang_within_the_allowance_is_fine():
    record = pattern(overhang=0.06, **{"layers.A": [{"x": 0.45, "y": 0}]})
    assert pallet_problems(with_patterns(record)) == []


def test_rotated_overlap_uses_the_rotated_rectangle():
    # 0.4 x 0.3 items: at rotation 0 the length runs along the pallet's length (x).
    def layer(*second):
        return pallet_problems(with_patterns(pattern(overhang=1.0, **{"layers.A": [
            {"x": 0, "y": 0, "rotation": 0}, dict(zip(("x", "y", "rotation"), second))]})))
    assert [p.code for p in layer(0.36, 0, 0)] == ["E_PALLET_OVERLAP"]     # 0.2 + 0.2 > 0.36
    assert [p.code for p in layer(0.34, 0, 90)] == ["E_PALLET_OVERLAP"]    # 0.2 + 0.15 > 0.34
    assert layer(0.36, 0, 90) == []                                       # 0.2 + 0.15 < 0.36
    assert [p.code for p in layer(0.3, 0.2, 45)] == ["E_PALLET_OVERLAP"]
    assert layer(0.45, 0, 45) == []


# -- the layer generator -----------------------------------------------------------------
def test_column_picks_the_better_orientation():
    out = generate_layer(1.2, 0.8, 0.4, 0.3, kind="column")
    assert (out["kind"], out["items"], out["area_use"], out["overhang"]) == ("column", 8, 1.0, 0.0)
    assert {s["rotation"] for s in out["slots"]} == {90.0}


def test_block_beats_column_when_neither_grid_fills():
    assert generate_layer(1.2, 1.0, 0.4, 0.3, kind="column")["items"] == 9
    block, best = (generate_layer(1.2, 1.0, 0.4, 0.3, kind=k) for k in ("block", "best"))
    assert block["items"] == best["items"] == 10 and best["kind"] == "block"
    assert {s["rotation"] for s in best["slots"]} == {0.0, 90.0}
    record = {"name": "G", "pallet": {"length": 1.2, "width": 1.0},
              "item": {"length": 0.4, "width": 0.3, "height": 0.2},
              "layers": {"A": best["slots"]}, "stack": interlock(best["slots"]),
              "limits": {"layers": 4}}
    assert pallet_problems(with_patterns(record)) == []
    assert len(slots(record)) == 40


def test_overhang_lets_one_more_row_fit():
    assert generate_layer(1.2, 0.8, 0.42, 0.3)["items"] == 6
    out = generate_layer(1.2, 0.8, 0.42, 0.3, overhang=0.03)
    assert out["items"] == 8 and 0 < out["overhang"] <= 0.03 + 1e-9


def test_gap_spaces_the_items():
    assert generate_layer(1.2, 0.8, 0.4, 0.3, kind="column", gap=0.01)["items"] == 4


def test_a_tie_goes_to_column():
    assert generate_layer(1.2, 0.8, 0.4, 0.3)["kind"] == "column"


def test_interlock_mirrors_a_symmetric_grid():
    grid = generate_layer(1.2, 0.8, 0.4, 0.3)["slots"]
    assert interlock(grid) == [{"layer": "A"}, {"layer": "A", "transform": "mirror_x"}]
    lopsided = generate_layer(1.2, 1.0, 0.4, 0.3)["slots"]
    assert interlock(lopsided) == [{"layer": "A"}, {"layer": "A", "transform": "rotate180"}]


def test_bad_sizes_are_refused():
    with pytest.raises(ValueError):
        generate_layer(1.2, 0, 0.4, 0.3)
    with pytest.raises(ValueError):
        generate_layer(1.2, 0.8, 0.4, 0.3, kind="spiral")


def test_the_agent_tool_returns_the_generators_answer():
    from nagasim import agent_tools as T
    answer = T.call("generate_pallet_layer", pallet_length=1.2, pallet_width=1.0, item_length=0.4,
                    item_width=0.3)
    assert answer == generate_layer(1.2, 1.0, 0.4, 0.3)
    assert T.call("generate_pallet_layer", pallet_length=1.2, pallet_width=1.0, item_length=0.4,
                  item_width=0.3, kind="spiral")["error"]["code"] == "E_BAD_ARGUMENT"
