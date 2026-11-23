"""Conveyor types, fix round 1: back eyes of mixed sizes follow the belt's own rules, the
type table follows the model's length unit, and bad sizes and types are refused.

Hand derivations (speed 0.5, SMALL 0.30, LARGE 0.60, back eye at 1.0, a 10 s stop at 1.5):
- LARGE boards at 0; its front reaches the stop at 1.5 / 0.5 = 3.0 s and dwells 10 s, so it
  leaves at 13.0 s; its back clears the 1.0 m sensor when the front is at 1.6 m, 0.1 m past
  the stop: 13.0 + 0.1 / 0.5 = 13.2 s.
- SMALL: the back clears 1.0 when the front is at 1.3, before the stop: 1.3 / 0.5 = 2.6 s.
"""
import math

import pytest
from conftest import run_env
from test_conveyor_divert_spec import belt
from test_conveyor_types import entity_of, sized, two_type_doc, typed

from nagasim.core.schema import validate_model_structured
from nagasim.core.trace import TraceRecorder


def eye_times(e_type, accumulating):
    conveyor = typed(length=3.0, speed=0.5, accumulating=accumulating,
                     stops=[{"at": 1.5, "time": 10}], eyes=[{"at": 1.0, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60)], [e_type])
    recorder = TraceRecorder()
    run_env(doc, 100, subscribers=[recorder])
    return [e.t for e in recorder.events if e.kind == "eye"]


@pytest.mark.parametrize("accumulating", [True, False])
def test_a_longer_items_back_eye_waits_for_its_stop(accumulating):
    assert eye_times("LARGE", accumulating) == [pytest.approx(13.2)]


@pytest.mark.parametrize("accumulating", [True, False])
def test_a_shorter_items_back_eye_fires_before_the_stop(accumulating):
    assert eye_times("SMALL", accumulating) == [pytest.approx(2.6)]


def test_the_eyes_frame_counts_one_row_per_declared_eye():
    conveyor = typed(length=3.0, speed=0.5, eyes=[{"at": 1.0, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60)],
                       ["SMALL", "LARGE"], starts=[0, 20])
    env, model = run_env(doc, 100)
    assert len(model.blocks["belt"]._eyes) == 1 and model.blocks["belt"]._eyes[0].fired == 2


def test_a_millimetre_model_keeps_the_type_table_and_sizes_in_metres():
    """The engine works in metres. Belt 3000 mm = 3.0 m. Superseded by Task 2b (D1355,
    owner-approved values): both rows have the default cell, 0.01 m in a millimetre model too
    (R20: an unset cell is 1 cm in every unit system), and capacity floor(3.0 / 0.01) = 300
    cells (was: unsized roller 0.61 m and 4; CASE 410 mm + gap 50 mm 0.46 m and 6). The
    size and gap still convert to metres: a CASE takes (0.41 + 0.05) / 0.01 = 46 cells, an
    unsized BOX (0.61 + 0.05) / 0.01 = 66."""
    for entities, cell, capacity, cells in (([{"name": "BOX"}], 0.01, 300, 66),
                                            ([sized("CASE", 410.0)], 0.01, 300, 46)):
        conveyor = typed("roller", gap=50.0, length=3000.0)
        doc = two_type_doc(conveyor, entities, [entities[0]["name"]])
        doc["simulation"]["length_unit"] = "millimeters"
        env, model = run_env(doc, 100)
        belt_ = model.blocks["belt"]
        assert math.isclose(belt_.cell_length, cell) and belt_.capacity == capacity
        assert belt_.cells_of(entity_of(env, entities[0]["name"])) == cells


def test_an_untyped_belt_refuses_a_sized_item_longer_than_its_spacing_as_it_enters():
    doc = two_type_doc(belt(spacing=0.5), [sized("CASE", 0.8)], ["CASE"])
    with pytest.raises(RuntimeError, match="E_CONVEYOR_EYE"):
        run_env(doc, 100)


def test_a_large_type_that_never_enters_the_untyped_belt_is_no_problem():
    """BIG (0.8 m) is declared but only SMALL (0.3 m) is made: spacing 0.5 loads and runs."""
    doc = two_type_doc(belt(spacing=0.5), [sized("BIG", 0.8), sized("SMALL", 0.3)], ["SMALL"])
    env, _model = run_env(doc, 100)
    assert len(env.ctx.all_entities) == 1


def test_an_unsized_items_back_eye_is_at_its_own_length():
    """Typed belt, gap 0, SMALL 0.30 and LARGE 0.60 sized and BOX unsized with part_length
    0.5 (the old "cell 0.60" is gone with Task 2b, D1355: it no longer matters). BOX
    is 0.5 long (part_length). Speed 0.5, back eye at 1.0: BOX fires at (1.0 + 0.5) / 0.5 =
    3.0 s, SMALL at 2.6 s (boards 0 and 20, so SMALL 22.6 s)."""
    conveyor = typed(length=5.0, speed=0.5, part_length=0.5,
                     eyes=[{"at": 1.0, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60), {"name": "BOX"}],
                       ["BOX", "SMALL"], starts=[0, 20])
    recorder = TraceRecorder()
    run_env(doc, 100, subscribers=[recorder])
    assert [e.t for e in recorder.events if e.kind == "eye"] == [
        pytest.approx(3.0), pytest.approx(22.6)]


def test_an_unknown_type_is_a_problem_with_a_plain_line():
    doc = two_type_doc(typed("zigzag"), [{"name": "BOX"}], ["BOX"])
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
    assert [p.fields["case"] for p in found] == ["type"]


def test_a_typed_belt_with_a_default_point_off_the_end_still_builds():
    """3.0 m, gap 0.1, back eye at 2.395, SMALL 0.3 and LARGE 0.6. Re-derived for Task 2b
    (D1355, review M6): the unsized DEFAULT is the type's zone length 0.61 (the old cell 0.7 is
    gone), so its point would be at 2.395 + 0.61 = 3.005, off the belt, and is left out;
    SMALL's (2.695) and LARGE's (2.995) stay. LARGE fires its own at 2.995 / 0.5 = 5.99 s."""
    conveyor = typed(gap=0.1, length=3.0, speed=0.5, eyes=[{"at": 2.395, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.3), sized("LARGE", 0.6)], ["LARGE"])
    recorder = TraceRecorder()
    _env, model = run_env(doc, 100, subscribers=[recorder])
    assert [round(p.at, 4) for p in model.blocks["belt"]._points] == [2.695, 2.995]
    assert [e.t for e in recorder.events if e.kind == "eye"] == [pytest.approx(5.99)]


def test_an_unmade_long_type_whose_point_is_off_the_belt_is_left_out():
    """Untyped spacing 0.5, 3.0 m, back eye at 2.4, BIG 0.8 never made: its point (3.2) is off
    the belt and dropped; SMALL 0.3 fires at (2.4 + 0.3) / 1 = 2.7 s."""
    conveyor = belt(spacing=0.5, length=3.0)
    conveyor["params"]["eyes"] = [{"at": 2.4, "edge": "back"}]
    doc = two_type_doc(conveyor, [sized("BIG", 0.8), sized("SMALL", 0.3)], ["SMALL"])
    recorder = TraceRecorder()
    run_env(doc, 100, subscribers=[recorder])
    assert [e.t for e in recorder.events if e.kind == "eye"] == [pytest.approx(2.7)]


def test_an_item_whose_back_eye_point_was_left_out_is_refused_as_it_boards():
    """Untyped spacing 0.5, part_length 0.1, 3.0 m, back eye at 2.6 (validates: 2.7 < 3.0).
    CASE 0.5 would put its point at 3.1, off the belt, so it is dropped and a CASE is refused
    on boarding; SMALL 0.3 (2.9) is fine."""
    conveyor = belt(spacing=0.5, length=3.0)
    conveyor["params"].update({"eyes": [{"at": 2.6, "edge": "back"}], "part_length": 0.1})
    entities = [sized("CASE", 0.5), sized("SMALL", 0.3)]
    run_env(two_type_doc(conveyor, entities, ["SMALL"]), 100)
    with pytest.raises(RuntimeError, match="E_CONVEYOR_EYE"):
        run_env(two_type_doc(conveyor, entities, ["CASE"]), 100)


def test_an_over_long_item_is_refused_when_it_merges_onto_an_untyped_belt():
    """Main parts (DEFAULT, cell 0.5) ride; SIDE is 0.8 m and joins at the merge at 5."""
    from conftest import create, dispose
    from test_conveyor_divert_spec import model
    blocks = [create(max_arrivals=1, dist_params={"value": 1.0}, start_time=0),
              belt(spacing=0.5, merges=[{"at": 5, "from": "side"}]), dispose(),
              create(name="side", e_type="Side", max_arrivals=1, start_time=1)]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    doc = model(blocks, links)
    doc["entities"] = [sized("SIDE", 0.8)]
    with pytest.raises(RuntimeError, match="E_CONVEYOR_EYE"):
        run_env(doc, 100)


def test_a_back_eye_on_a_typed_belt_validates_without_the_spacing():
    """A typed 3.0 m belt with a back eye at 2.3: its item lengths come from the entity
    types, not the (ignored) spacing 1.0, so the static check keeps quiet."""
    conveyor = typed(gap=0.1, length=3.0, eyes=[{"at": 2.3, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.3)], ["SMALL"])
    assert [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_EYE"] == []


def test_a_bool_is_not_a_size_number():
    """`true` is not a length (checkpoint T3 minor): the entity manager keeps only real numbers,
    so a stray bool never becomes 1.0 m."""
    from nagasim.core.entity_manager import EntityManager
    manager = EntityManager()
    manager.load_from_json([{"name": "CASE", "size": {"length": True, "width": 0.3}}])
    assert manager.all()["CASE"]["size"] == {"width": 0.3}
