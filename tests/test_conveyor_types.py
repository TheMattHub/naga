"""Conveyor types and item sizes (conveyor system A, Task 2), written from the spec before the code.

API assumptions (from the brief, not from the implementation):
- A CONVEYOR takes params `type` (str, "" = untyped) and `gap` (float, m).
- An entity type is a row of the model's top-level `entities` list; its optional `size` is
  {"length": m, "width": m, "height": m}. Entity type names are upper-cased by the engine.
- `run_env(doc, t)` returns (env, model); `model.blocks[name]` is the Conveyor, which has
  `cell_length`, `capacity` and `item_length(entity)`; `env.ctx.all_entities` keeps every entity
  made, deleted ones included.
- Eye events (kind "eye", state "front"/"back") fire a part's front at `at` and its back
  `item_length / speed` later.
"""
import math

from conftest import create, run_env
from test_conveyor_divert_spec import approx_all, belt, plain_line

from nagasim.core import conveyor_types
from nagasim.core.trace import TraceRecorder


def sized(name, length, width=0.3, height=0.2):
    return {"name": name, "size": {"length": length, "width": width, "height": height}}


def typed(type_name="belt", gap=0.0, eyes=(), **belt_args):
    extra = {key: belt_args.pop(key) for key in ("part_length",) if key in belt_args}
    conveyor = belt(**belt_args)
    conveyor["params"].update({"type": type_name, "gap": gap, "eyes": list(eyes), **extra})
    return conveyor


def two_type_doc(conveyor, entities, e_types, starts=None):
    """CREATE per entity type (one part each) -> *conveyor* -> DISPOSE."""
    starts = starts or [0] * len(e_types)
    creates = [create(name=f"c_{t}", e_type=t, max_arrivals=1, start_time=s)
               for t, s in zip(e_types, starts)]
    doc = plain_line(conveyor, arrivals=1)
    doc["blocks"] = creates + doc["blocks"][1:]
    doc["links"] = ([{"source": c["name"], "target": conveyor["name"]} for c in creates]
                    + doc["links"][1:])
    doc["entities"] = entities
    return doc


def entity_of(env, e_type):
    return next(e for e in env.ctx.all_entities if e.e_type == e_type)


def exits_of(env):
    return sorted(e.deletion_time for e in env.ctx.all_entities)


def test_types_table_has_the_five_types_and_fields():
    """Spec goal 5 / brief: exactly belt, roller, chain, pallet, mdr; each a ConveyorType
    with the six fields in this order. Values come from a datasheet, so only their kind is
    checked here: positive numbers for speed, width, height, zone_length; gap >= 0."""
    fields = ("speed", "width", "height", "zone_length", "roller_pitch", "gap")
    assert set(conveyor_types.TYPES) == {"belt", "roller", "chain", "pallet", "mdr"}
    assert tuple(conveyor_types.ConveyorType._fields) == fields
    for entry in conveyor_types.TYPES.values():
        assert isinstance(entry, conveyor_types.ConveyorType)
        assert all(isinstance(v, (int, float)) for v in entry)
        assert min(entry.speed, entry.width, entry.height, entry.zone_length) > 0
        assert entry.gap >= 0


def test_belt_roller_and_mdr_stand_at_knuckle_height():
    """D1434 (owner choice, not a vendor value): these three tops are 0.762 m (30 in), the
    knuckle height for loading by hand, not the vendors' 0.152 m minimum elevation. Chain
    (Interroll TOC mid) and pallet (TOR) keep their datasheet heights."""
    for kind in ("belt", "roller", "mdr"):
        assert conveyor_types.TYPES[kind].height == 0.762
    assert conveyor_types.TYPES["chain"].height == 0.775
    assert conveyor_types.TYPES["pallet"].height == 0.095


def test_cell_from_item_size():
    """Entity type CASE has size.length 0.41; typed belt, length 3.0, gap 0. Superseded by
    the owner decision of Task 2b (D1355, owner-approved values): cell_length is the default
    cell 0.01 m, not the largest size; capacity floor(3.0 / 0.01) = 300 cells. `spacing`
    (default 1.0) is ignored on a typed belt."""
    doc = two_type_doc(typed(length=3.0), [sized("CASE", 0.41)], ["CASE"])
    _env, model = run_env(doc, 20)
    conveyor = model.blocks["belt"]
    assert math.isclose(conveyor.cell_length, 0.01)
    assert conveyor.capacity == 300


def test_typed_belt_ignores_a_set_spacing():
    """Same as above but spacing 5.0 is set and gap 0.10: the rule keys on `type`, not on
    `spacing`. Superseded by Task 2b (D1355, owner-approved values): cell 0.01 m (was
    0.41 + 0.10 = 0.51), capacity 300 cells (was 5)."""
    doc = two_type_doc(typed(gap=0.10, length=3.0, spacing=5.0), [sized("CASE", 0.41)], ["CASE"])
    _env, model = run_env(doc, 20)
    assert math.isclose(model.blocks["belt"].cell_length, 0.01)
    assert model.blocks["belt"].capacity == 300


def test_mixed_sizes_use_the_standard_cell_and_each_item_blocks_its_own_length():
    """SMALL 0.30 and LARGE 0.60, gap 0.05, speed 0.5. Superseded by Task 2b (D1355,
    owner-approved values): the cell is the default 0.01 m, not max(0.30, 0.60) + 0.05 = 0.65;
    the eye times and item lengths below are unchanged. One part each, SMALL boarding at 0 and LARGE at 20 (no interaction). Eyes at 1.0 m, front
    and back. Front reaches 1.0 m at boarding + 1.0 / 0.5 = +2.0 s; the back clears it
    item_length / speed later: SMALL 0.30 / 0.5 = 0.6 s, LARGE 0.60 / 0.5 = 1.2 s.
    Events in time order: 2.0 front, 2.6 back, 22.0 front, 23.2 back. item_length is each
    type's own size.length (0.30, 0.60), not the cell."""
    conveyor = typed(gap=0.05, length=5.0, speed=0.5,
                     eyes=[{"at": 1.0}, {"at": 1.0, "edge": "back"}])
    doc = two_type_doc(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60)],
                       ["SMALL", "LARGE"], starts=[0, 20])
    recorder = TraceRecorder()
    env, model = run_env(doc, 100, subscribers=[recorder])
    fired = sorted((e for e in recorder.events if e.kind == "eye"), key=lambda e: e.t)
    assert [e.t for e in fired] == approx_all([2.0, 2.6, 22.0, 23.2])
    assert [e.state for e in fired] == ["front", "back", "front", "back"]
    belt_ = model.blocks["belt"]
    assert math.isclose(belt_.cell_length, 0.01)
    assert math.isclose(belt_.item_length(entity_of(env, "SMALL")), 0.30)
    assert math.isclose(belt_.item_length(entity_of(env, "LARGE")), 0.60)


def test_untyped_belt_is_unchanged():
    """No `type`, spacing 1.0, length 10, speed 1, 6 parts every 1 from 0: cell_length 1.0,
    capacity 10, exits 10..15 (a part boards at k and leaves 10 later; the same numbers as
    tests/test_conveyor_eyes.py)."""
    env, model = run_env(plain_line(belt(), arrivals=6), 100)
    assert math.isclose(model.blocks["belt"].cell_length, 1.0)
    assert model.blocks["belt"].capacity == 10
    assert exits_of(env) == approx_all([10, 11, 12, 13, 14, 15])


def test_untyped_belt_ignores_entity_sizes_for_its_cell():
    """Untyped, spacing 2.0, length 10, and a sized entity type CASE 0.41: cell stays the
    spacing 2.0 (capacity floor(10 / 2.0) = 5); the item itself still reports its own size
    0.41 for item_length (order step 1)."""
    doc = two_type_doc(belt(spacing=2.0), [sized("CASE", 0.41)], ["CASE"])
    env, model = run_env(doc, 100)
    belt_ = model.blocks["belt"]
    assert math.isclose(belt_.cell_length, 2.0)
    assert belt_.capacity == 5
    assert math.isclose(belt_.item_length(entity_of(env, "CASE")), 0.41)


def test_item_length_size_beats_part_length():
    """Untyped belt with part_length 0.4 and a CASE of size 0.25: step 1 wins, 0.25."""
    conveyor = belt()
    conveyor["params"]["part_length"] = 0.4
    env, model = run_env(two_type_doc(conveyor, [sized("CASE", 0.25)], ["CASE"]), 100)
    assert math.isclose(model.blocks["belt"].item_length(entity_of(env, "CASE")), 0.25)


def test_item_length_unsized_uses_part_length_then_cell():
    """Entity types with no size. Untyped, spacing 2.0: part_length 0.4 > 0 gives 0.4
    (step 2); part_length 0 gives the cell_length, the spacing 2.0 (step 3)."""
    for part_length, expected in ((0.4, 0.4), (0.0, 2.0)):
        conveyor = belt(spacing=2.0)
        conveyor["params"]["part_length"] = part_length
        env, model = run_env(two_type_doc(conveyor, [{"name": "BOX"}], ["BOX"]), 100)
        assert math.isclose(model.blocks["belt"].item_length(entity_of(env, "BOX")), expected)


def test_typed_belt_without_sized_entity_uses_zone_length():
    """Typed "roller", no entity type has a size: an unsized item's item_length (no
    part_length) is TYPES["roller"].zone_length (the datasheet value is not asserted);
    spacing 7.0 set on the belt is ignored. Superseded by Task 2b (D1355, owner-approved
    values): cell_length is the default 0.01 m (was the zone length) and capacity
    floor(3.0 / 0.01) = 300 cells (was floor(3.0 / zone_length))."""
    zone = conveyor_types.TYPES["roller"].zone_length
    conveyor = typed("roller", length=3.0, spacing=7.0)
    env, model = run_env(two_type_doc(conveyor, [{"name": "BOX"}], ["BOX"]), 100)
    belt_ = model.blocks["belt"]
    assert math.isclose(belt_.cell_length, 0.01)
    assert belt_.capacity == 300
    assert math.isclose(belt_.item_length(entity_of(env, "BOX")), zone)


def test_typed_belt_with_one_sized_and_one_unsized_entity_type():
    """Typed belt, gap 0.10; CASE size 0.41, BOX unsized. Superseded by Task 2b (D1355,
    owner-approved values): the cell is the default 0.01 m (was 0.41 + 0.10 = 0.51), and BOX's
    item_length falls to step 3, the type's zone length (was the cell 0.51); with part_length
    0.3 on the belt it is 0.3 (step 2); CASE is 0.41 (step 1) either way."""
    zone = conveyor_types.TYPES["belt"].zone_length
    for part_length, box_expected in ((0.0, zone), (0.3, 0.3)):
        conveyor = typed(gap=0.10, length=3.0, part_length=part_length)
        doc = two_type_doc(conveyor, [sized("CASE", 0.41), {"name": "BOX"}], ["CASE", "BOX"])
        env, model = run_env(doc, 100)
        belt_ = model.blocks["belt"]
        assert math.isclose(belt_.cell_length, 0.01)
        assert math.isclose(belt_.item_length(entity_of(env, "CASE")), 0.41)
        assert math.isclose(belt_.item_length(entity_of(env, "BOX")), box_expected)
