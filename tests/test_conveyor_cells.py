"""Multi-cell occupancy on typed conveyors (conveyor system A, Task 2b), written from the brief
before the code.

Rule under test: a TYPED belt has a small standard `cell` (param, metres, default 0.01); an item
of length L takes `cells_of = max(1, ceil((L + gap) / cell - 1e-9))` cells and so `cells_of * cell`
of belt, measured back from its nose. Queued items sit nose to tail: the nose of the item behind
is `cells_of(item ahead) * cell` behind the nose ahead. Untyped belts keep one item = one cell
of `spacing`.

API assumptions (from the brief, not from the implementation):
- CONVEYOR param `cell` (m); `Conveyor.cell_length`, `Conveyor.cells_of(entity)`,
  `Conveyor.capacity` (typed: floor(length / cell) cells), `Conveyor.item_length(entity)`.
- Same builders as tests/test_conveyor_types.py: `sized`, `entity_of`; an entity type is a row
  of the top-level `entities` list with `size.length`.
- The exit is held as in tests/test_conveyor_eyes.py: the belt's `exit_resource` "R" is held by
  a hog until 20, so the head item waits at the belt end and everything queues behind it. All
  exits are the DISPOSE times (`entity.deletion_time`) of the non-hog entities. A held queue
  releases in lock step: the item whose nose is `d` short of the end exits `d / speed` after
  the release.
- Eye events (kind "eye", state "front"/"back") are the observable for queued nose positions:
  a front eye at x fires when a nose passes x. Positions in the plane are never used.
- Entry rule detail the brief leaves open (is the 8th item admitted by `cells` in use or by the
  nose gap?) is not pinned: tests assert only what both readings give.
"""
import math

import pytest
from conftest import create, dispose, run_env
from test_conveyor_divert_spec import belt, hog, model, pick
from test_conveyor_types import entity_of, sized

from nagasim.core import conveyor_types
from nagasim.core.trace import TraceRecorder

HOLD = 20.0


def close(a, b):
    return math.isclose(a, b, abs_tol=1e-9)


def all_close(actual, expected):
    return len(actual) == len(expected) and all(close(a, e) for a, e in zip(actual, expected))


def typed_belt(*, length=3.0, speed=0.5, gap=0.0, cell=None, eyes=(), stops=(),
               accumulating=True, part_length=None, type_name="belt"):
    conveyor = belt(length=length, speed=speed, stops=stops, accumulating=accumulating)
    conveyor["params"].update({"type": type_name, "gap": gap, "eyes": list(eyes)})
    if cell is not None:
        conveyor["params"]["cell"] = cell
    if part_length is not None:
        conveyor["params"]["part_length"] = part_length
    return conveyor


def build(conveyor, entities, feeds, hold=None):
    """feeds: (e_type, start, count, interval) per CREATE -> belt -> (hold: pick) -> DISPOSE.
    With *hold*, a hog keeps the belt's exit resource R until that time."""
    blocks, links = [], []
    for i, (e_type, start, count, interval) in enumerate(feeds):
        blocks.append(create(name=f"c{i}", e_type=e_type, max_arrivals=count,
                             dist_params={"value": interval}, start_time=start))
        links.append({"source": f"c{i}", "target": conveyor["name"]})
    blocks += [conveyor, dispose()]
    resources = ()
    if hold is None:
        links.append({"source": conveyor["name"], "target": "dispose_1"})
    else:
        conveyor["params"]["exit_resource"] = "R"
        hog_blocks, hog_links = hog("R", hold)
        blocks += [pick("R"), *hog_blocks]
        links += [{"source": conveyor["name"], "target": "pick"},
                  {"source": "pick", "target": "dispose_1"}, *hog_links]
        resources = ("R",)
    doc = model(blocks, links, resources)
    doc["entities"] = list(entities)
    return doc


def exits_of(env):
    """Sorted DISPOSE times of everything except the hog."""
    return sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type != "HOG")


def eye_times(doc, run_len=100):
    recorder = TraceRecorder()
    env, model_ = run_env(doc, run_len, subscribers=[recorder])
    return sorted(e.t for e in recorder.events if e.kind == "eye"), env, model_


# -- case 1: a 0.41 m case takes 41 cells whatever else the model declares -------------------

@pytest.mark.parametrize("with_pallet", [False, True], ids=["cases_only", "pallet_declared"])
def test_1_a_case_takes_41_cells_and_a_declared_pallet_changes_nothing(with_pallet):
    """Typed 3.0 m belt, speed 0.5, gap 0, default cell 0.01, CASE 0.41; PALLET 1.2 is declared
    but never enters. cells_of(CASE) = ceil(0.41 / 0.01 - 1e-9) = 41 (nose to nose 0.41 m);
    cell_length 0.01; capacity floor(3.0 / 0.01) = 300 cells. Five CASEs offered every 0.1 s
    board a nose-gap 0.41 / 0.5 = 0.82 s apart (0, 0.82, ...) and each rides 3.0 / 0.5 = 6 s:
    exits 6.00, 6.82, 7.64, 8.46, 9.28. The old rule (cell = largest size in the model) gave
    1.2 m cells and 2.4 s headway with the PALLET declared."""
    entities = [sized("CASE", 0.41)] + ([sized("PALLET", 1.2)] if with_pallet else [])
    env, model_ = run_env(build(typed_belt(), entities, [("CASE", 0, 5, 0.1)]), 100)
    conveyor = model_.blocks["belt"]
    assert close(conveyor.cell_length, 0.01)
    assert conveyor.capacity == 300
    assert conveyor.cells_of(entity_of(env, "CASE")) == 41
    assert close(conveyor.item_length(entity_of(env, "CASE")), 0.41)
    assert all_close(exits_of(env), [6.0 + 0.82 * k for k in range(5)])


def test_1_seven_cases_fit_queued_and_the_eighth_waits_for_the_head_to_leave():
    """Same belt, 8 CASEs offered every 0.1 s, exit held until 20. CASE k boards at 0.82 k, its
    nose passes a front eye at 0.2 m at 0.82 k + 0.4 s (k = 0..6: 0.4, 1.22, 2.04, 2.86, 3.68,
    4.50, 5.32) and it queues with its nose at 3.00 - 0.41 k (3.00 ... 0.54; the 7th tail at
    0.13 m, 7 * 0.41 = 2.87 <= 3.0). An 8th would need 8 * 0.41 = 3.28 > 3.0: it cannot join
    before the head leaves, so its eye fires at 20 or later. Release at 20: the queued
    noses reach the end at 20 + 0.82 k, k = 0..6: 20.00 ... 24.92 (lock step: the item behind
    moves as soon as the one ahead does). The 8th item's own exit is not pinned (>= 24.92)."""
    conveyor = typed_belt(eyes=[{"at": 0.2}])
    doc = build(conveyor, [sized("CASE", 0.41), sized("PALLET", 1.2)],
                [("CASE", 0, 8, 0.1)], hold=HOLD)
    fired, env, _model = eye_times(doc)
    assert len(fired) == 8
    assert all_close(fired[:7], [0.4 + 0.82 * k for k in range(7)])
    assert fired[7] >= HOLD - 1e-9
    exits = exits_of(env)
    assert len(exits) == 8
    assert all_close(exits[:7], [HOLD + 0.82 * k for k in range(7)])
    assert exits[7] >= HOLD + 0.82 * 6 - 1e-9


# -- case 2: SMALL (35 cells), LARGE (65 cells), SMALL on one typed belt ------------------------

def _mixed_doc(eyes=()):
    """SMALL boards at 0, LARGE at 2, SMALL at 4 (single CREATEs): 2 s apart is far more than
    any entry rhythm needs (previous nose 1.0 m >= 0.65 m), so only the held end queues them."""
    conveyor = typed_belt(gap=0.05, cell=0.01, eyes=eyes)
    feeds = [("SMALL", 0, 1, 1.0), ("LARGE", 2, 1, 1.0), ("SMALL", 4, 1, 1.0)]
    return build(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60)], feeds, hold=HOLD)


def test_2_mixed_items_take_their_own_cells_and_leave_at_the_derived_times():
    """3.0 m, speed 0.5, cell 0.01, gap 0.05: SMALL (0.30 + 0.05) / 0.01 = 35 cells = 0.35 m;
    LARGE (0.60 + 0.05) / 0.01 = 65 cells = 0.65 m. SMALL1 nose reaches the end at 6 s (boards
    0), LARGE joins behind it with its nose at 3.00 - 0.35 = 2.65 (arrives 2 + 5.3 = 7.3), SMALL2
    behind that at 2.65 - 0.65 = 2.00 (arrives 4 + 4.0 = 8.0). Exit held until t0 = 20; then
    SMALL1 exits 20.0, LARGE 20 + 0.35 / 0.5 = 20.7, SMALL2 20.7 + 0.65 / 0.5 = 22.0
    (= 20 + 1.00 / 0.5)."""
    env, model_ = run_env(_mixed_doc(), 100)
    conveyor = model_.blocks["belt"]
    small, large = entity_of(env, "SMALL"), entity_of(env, "LARGE")
    assert close(conveyor.cell_length, 0.01)
    assert (conveyor.cells_of(small), conveyor.cells_of(large)) == (35, 65)
    assert close(conveyor.item_length(small), 0.30) and close(conveyor.item_length(large), 0.60)
    assert all_close(exits_of(env), [20.0, 20.7, 22.0])


@pytest.mark.parametrize("at, expected", [
    (2.64, [5.28, 7.28, 21.28]),
    (2.66, [5.32, 20.02, 21.32]),
], ids=["eye_just_below_2.65", "eye_just_above_2.65"])
def test_2_queued_noses_sit_at_3_00_2_65_and_2_00(at, expected):
    """A front eye pins where the queue's noses wait (the queue forms at t < 8, release at 20).
    SMALL1 (nose 3.00) passes x at boarding 0 + x / 0.5. LARGE (boards 2) waits at nose 2.65:
    an eye at 2.64 is passed on the way in (2 + 2.64 / 0.5 = 7.28); an eye at 2.66 only after
    the release (20 + (2.66 - 2.65) / 0.5 = 20.02). SMALL2 waits at nose 2.00 and passes x
    after the release: 20 + (x - 2.00) / 0.5 = 21.28 for 2.64, 21.32 for 2.66. Eye times
    sorted: eye 2.64 -> 5.28, 7.28, 21.28; eye 2.66 -> 5.32, 20.02, 21.32."""
    fired, _env, _model = eye_times(_mixed_doc(eyes=[{"at": at}]))
    assert all_close(fired, expected)


# -- case 3: rounding up to whole cells ----------------------------------------------------

def test_3_a_cell_that_does_not_divide_the_item_rounds_up():
    """Cell 0.05, CASE 0.41, gap 0: 0.41 / 0.05 = 8.2 -> 9 cells = 0.45 m nose to nose.
    Capacity floor(3.0 / 0.05) = 60 cells. Four CASEs offered every 0.1 s board 0.45 / 0.5 =
    0.9 s apart and ride 6 s: exits 6.0, 6.9, 7.8, 8.7."""
    doc = build(typed_belt(cell=0.05), [sized("CASE", 0.41)], [("CASE", 0, 4, 0.1)])
    env, model_ = run_env(doc, 100)
    conveyor = model_.blocks["belt"]
    assert close(conveyor.cell_length, 0.05)
    assert conveyor.cells_of(entity_of(env, "CASE")) == 9
    assert conveyor.capacity == 60
    assert all_close(exits_of(env), [6.0, 6.9, 7.8, 8.7])


# -- case 4: untyped belts are unchanged, and a typed belt has no "too long" item ------------

def test_4_an_untyped_belt_keeps_one_item_per_spacing_cell_and_ignores_cell():
    """Untyped, 10 m, speed 1, spacing 0.5, a `cell` of 0.01 set (ignored). cell_length is the
    spacing 0.5, cells_of is 1 for a 0.3 m item and for any other, capacity 10 / 0.5 = 20. Three
    items offered every 0.1 s board 0.5 s apart (spacing / speed) and ride 10 s: exits 10.0,
    10.5, 11.0. A declared 0.8 m BIG that never enters (R10) is no problem."""
    conveyor = belt(length=10.0, speed=1.0, spacing=0.5)
    conveyor["params"]["cell"] = 0.01
    doc = build(conveyor, [sized("BIG", 0.8), sized("SMALL", 0.3)], [("SMALL", 0, 3, 0.1)])
    env, model_ = run_env(doc, 100)
    untyped = model_.blocks["belt"]
    assert close(untyped.cell_length, 0.5) and untyped.capacity == 20
    assert untyped.cells_of(entity_of(env, "SMALL")) == 1
    assert all_close(exits_of(env), [10.0, 10.5, 11.0])


def test_4_an_untyped_belt_still_refuses_an_item_longer_than_its_spacing():
    """Untyped spacing 0.5, CASE 0.8 enters: refused at entry (Task 2 R10)."""
    doc = build(belt(spacing=0.5), [sized("CASE", 0.8)], [("CASE", 0, 1, 1.0)])
    with pytest.raises(RuntimeError, match="E_CONVEYOR_EYE"):
        run_env(doc, 100)


def test_4_a_typed_belt_has_no_too_long_item_it_takes_more_cells():
    """Typed 3.0 m belt, speed 0.5, cell 0.01, a 1.2 m PALLET (longer than any old cell) boards
    at 0: no refusal; cells_of = ceil(1.2 / 0.01 - 1e-9) = 120; it rides 6 s: exit 6.0. A second
    PALLET offered at once boards when the first is 1.2 m in (2.4 s): exit 8.4."""
    doc = build(typed_belt(cell=0.01), [sized("PALLET", 1.2)], [("PALLET", 0, 2, 0.1)])
    env, model_ = run_env(doc, 100)
    assert model_.blocks["belt"].cells_of(entity_of(env, "PALLET")) == 120
    assert all_close(exits_of(env), [6.0, 8.4])


# -- case 5: an item with no size and no part_length --------------------------------------

def test_5_an_unsized_item_on_a_typed_belt_is_one_zone_long_in_cells():
    """Typed "belt", no entity size, no part_length: item_length = TYPES["belt"].zone_length Z
    (datasheet value, read from the table, not hard-coded); cells_of = max(1, ceil(Z / 0.05 -
    1e-9)) with cell 0.05 and gap 0 (Z = 0.61 would give 13). Dense 3 items: headway
    cells * 0.05 / 0.5 = cells * 0.1 s, the first exit at 5.0 / 0.5 = 10 s."""
    zone = conveyor_types.TYPES["belt"].zone_length
    cells = max(1, math.ceil(zone / 0.05 - 1e-9))
    doc = build(typed_belt(length=5.0, cell=0.05), [{"name": "BOX"}], [("BOX", 0, 3, 0.05)])
    env, model_ = run_env(doc, 100)
    conveyor = model_.blocks["belt"]
    box = entity_of(env, "BOX")
    assert close(conveyor.item_length(box), zone)
    assert conveyor.cells_of(box) == cells
    assert all_close(exits_of(env), [10.0 + 0.1 * cells * k for k in range(3)])


def test_5_part_length_beats_the_zone_length_for_an_unsized_item():
    """Same, with part_length 0.3 on the belt: item_length 0.3 (step 2), cells_of =
    ceil(0.3 / 0.05 - 1e-9) = 6 (float noise in 0.3 / 0.05 must not give 7), headway 6 * 0.05 / 0.5 = 0.6 s: exits 10.0, 10.6, 11.2."""
    doc = build(typed_belt(length=5.0, cell=0.05, part_length=0.3), [{"name": "BOX"}],
                [("BOX", 0, 3, 0.05)])
    env, model_ = run_env(doc, 100)
    box = entity_of(env, "BOX")
    assert close(model_.blocks["belt"].item_length(box), 0.3)
    assert model_.blocks["belt"].cells_of(box) == 6
    assert all_close(exits_of(env), [10.0, 10.6, 11.2])


# -- case 6: back eyes of mixed sizes stay exact under multi-cell occupancy -------------------

def _back_eye_doc(feeds, accumulating):
    conveyor = typed_belt(cell=0.01, accumulating=accumulating, stops=[{"at": 1.5, "time": 10}],
                          eyes=[{"at": 1.0, "edge": "back"}])
    return build(conveyor, [sized("SMALL", 0.30), sized("LARGE", 0.60)], feeds)


@pytest.mark.parametrize("accumulating", [True, False])
@pytest.mark.parametrize("e_type, expected", [("SMALL", 2.6), ("LARGE", 13.2)])
def test_6_back_eye_times_of_small_and_large_with_a_stop(e_type, expected, accumulating):
    """Belt 3.0 m, speed 0.5, back eye at 1.0, a 10 s stop at 1.5 (the Task 2 fix numbers,
    cell 0.01 now). SMALL: its back clears 1.0 when the front is at 1.3, before the stop:
    1.3 / 0.5 = 2.6. LARGE: front at the stop at 3.0, leaves 13.0, back clears 1.0 with the
    front at 1.6, 0.1 m past the stop: 13.0 + 0.1 / 0.5 = 13.2."""
    fired, _env, _model = eye_times(_back_eye_doc([(e_type, 0, 1, 1.0)], accumulating))
    assert all_close(fired, [expected])


def test_6_both_sizes_in_one_run_fire_their_own_back_edges():
    """SMALL boards 0: its back clears 1.0 at 2.6, before the stop (it dwells at 1.5 until
    13.0, after that edge). LARGE boards 20, reaches the stop at 20 + 3.0 = 23.0, leaves 33.0,
    back edge 33.0 + 0.1 / 0.5 = 33.2."""
    fired, _env, model_ = eye_times(
        _back_eye_doc([("SMALL", 0, 1, 1.0), ("LARGE", 20, 1, 1.0)], True))
    assert all_close(fired, [2.6, 33.2])
    assert len(model_.blocks["belt"]._eyes) == 1 and model_.blocks["belt"]._eyes[0].fired == 2
