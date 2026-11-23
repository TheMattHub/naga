"""Multi-cell occupancy on typed belts (D1355), the rules the spec tests leave open: merges,
a short section a long item overhangs, and states.

Hand derivations (belt 10.0 m, speed 0.5, merge at 5.0, CASE 0.41, PALLET 1.2, gap 0, cell
0.01 m, so an item takes its own length):
- One CASE boards at 0; its front is at 5.0 at 10 s and at 4.0 at 8 s. A merging part that
  arrives at 8 is 1.0 m ahead of it. A 0.41 m SIDE fits (1.0 >= 0.41): it joins at 8 and
  leaves the end at 8 + 5.0 / 0.5 = 18. A 1.2 m PALLET does not: it waits until the CASE
  has passed (10) and is its own 0.41 m past the point, 10 + 0.41 / 0.5 = 10.82, and
  leaves at 10.82 + 10 = 20.82. The CASE leaves at 20 either way.
- Main CASEs every 0.82 s with a PALLET type declared and a merge on the belt: each main
  part claims the point only within its own length, so the line keeps its 0.82 s headway:
  exits 20.0, 20.82, 21.64.
"""
import math

import pytest
from conftest import create, dispose, run_env
from test_conveyor_divert_spec import belt, model
from test_conveyor_types import sized

from nagasim import state


def close_all(actual, expected):
    return len(actual) == len(expected) and all(
        math.isclose(a, e, abs_tol=1e-9) for a, e in zip(actual, expected))


def merge_doc(side_type, side_start=8.0, mains=1, interval=1.0):
    conveyor = belt(length=10.0, speed=0.5, merges=[{"at": 5.0, "from": "side"}])
    conveyor["params"]["type"] = "belt"
    blocks = [create(e_type="CASE", max_arrivals=mains, dist_params={"value": interval}),
              conveyor, dispose(),
              create(name="side", e_type=side_type, max_arrivals=1, start_time=side_start)]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    doc = model(blocks, links)
    doc["entities"] = [sized("CASE", 0.41), sized("PALLET", 1.2), sized("SIDE", 0.41)]
    return doc


def exits(env, e_type):
    return sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == e_type)


@pytest.mark.parametrize("side_type, side_exit", [("SIDE", 18.0), ("PALLET", 20.82)])
def test_a_merging_item_waits_for_a_main_part_closer_than_its_own_length(side_type, side_exit):
    env, _model = run_env(merge_doc(side_type), 100)
    assert close_all(exits(env, "CASE"), [20.0])
    assert close_all(exits(env, side_type), [side_exit])


def test_main_parts_claim_a_merge_within_their_own_length_only():
    env, _model = run_env(merge_doc("PALLET", side_start=500.0, mains=3, interval=0.82), 100)
    assert close_all(exits(env, "CASE"), [20.0, 20.82, 21.64])


def test_a_long_item_overhangs_a_short_section():
    """Typed 3.0 m belt, speed 0.5, front eyes at 1.0 and 1.1 (a 0.1 m section). Two 0.41 m
    CASEs: no item is refused or stuck because the section is shorter than it;
    they leave the free belt at 6.0 and 6.82 (headway 0.41 / 0.5)."""
    conveyor = belt(length=3.0, speed=0.5)
    conveyor["params"].update({"type": "belt", "eyes": [{"at": 1.0}, {"at": 1.1}]})
    blocks = [create(e_type="CASE", max_arrivals=2, dist_params={"value": 0.1}), conveyor,
              dispose()]
    doc = model(blocks, [{"source": "create_1", "target": "belt"},
                         {"source": "belt", "target": "dispose_1"}])
    doc["entities"] = [sized("CASE", 0.41)]
    env, _model = run_env(doc, 100)
    assert close_all(exits(env, "CASE"), [6.0, 6.82])


def _loop(cases, direct=False):
    """Typed belts A -> B -> DECIDE -> A, 3.0 m each: 300 cells, seven 0.41 m CASEs (287);
    *direct*: B -> A with no DECIDE."""
    blocks = [create(e_type="CASE", max_arrivals=cases, dist_params={"value": 0.1}),
              belt("A", length=3.0, speed=0.5), belt("B", length=3.0, speed=0.5),
              {"name": "again", "description": "again", "type": "DECIDE", "position": [2, 0],
               "params": {"mode": "Condition", "conditions": ["1 == 1"]}}, dispose()]
    for conveyor in blocks[1:3]:
        conveyor["params"]["type"] = "belt"
    links = [{"source": "create_1", "target": "A"}, {"source": "A", "target": "B"},
             {"source": "B", "target": "again"}, {"source": "again", "target": "A"},
             {"source": "again", "target": "dispose_1"}]
    if direct:
        links = links[:2] + [{"source": "B", "target": "A"}]
        blocks = [b for b in blocks if b["name"] != "again"]
    doc = model(blocks, links)
    doc["simulation"]["time_limit"] = 500
    doc["entities"] = [sized("CASE", 0.41)]
    return doc


def test_a_loop_of_typed_belts_with_free_cells_but_no_room_is_a_deadlock_at_once():
    """Review I1: 14 CASEs fill both belts to 287 of 300 cells; 13 free cells are no room for
    a 41-cell CASE. The loop check names the loop as it closes (before: the belts read "not
    full" and only the end-of-run check fired, which never comes in a model whose sources
    keep running). 12 CASEs leave room and circulate. R60: closed through the DECIDE, which may
    send a part out, the loop is named when the run drains instead."""
    with pytest.raises(RuntimeError, match="every station and buffer in the loop A -> B"):
        run_env(_loop(14, direct=True), 500)
    with pytest.raises(RuntimeError, match="nothing is left to happen but A, B"):
        run_env(_loop(14), 500)
    run_env(_loop(12), 100)


def test_the_full_share_of_a_typed_belt_is_no_room_for_the_next_item():
    """Spec case 1 held: from the 8th CASE's arrival (0.7 s) the belt has 13 free cells and
    a CASE waiting: full, though not every cell is taken."""
    from test_conveyor_cells import build, typed_belt
    env, built = run_env(build(typed_belt(), [sized("CASE", 0.41)], [("CASE", 0, 9, 0.1)],
                               hold=20.0), 15)
    space = built.blocks["belt"].space
    assert space.content.level == 287 and space.waiting == 2
    assert space.full.level == 1.0 and not space.has_room()


def test_a_station_reserving_a_buffer_past_a_typed_belt_runs():
    """Review I2: CREATE -> PROCESS m1 (hold_until_space, space_of the buffer) -> typed belt
    -> BUFFER buf (2) -> PROCESS m2 (3 s) -> DISPOSE. The reservation is the buffer's and rides
    on with the part; the belt takes its cells as usual. Same exits as the untyped belt: m1
    takes 1 s, the belt 6 s, m2 3 s each: 10, 13; the third waits for the buffer: 17, 20."""
    from conftest import chain, layout, process
    from test_queue_behaviour import buffer
    for typed in (False, True):
        conveyor = belt(length=3.0, speed=0.5)
        if typed:
            conveyor["params"]["type"] = "belt"
        blocks = [create("src", dist_params={"value": 0.0}, max_arrivals=4, e_type="CASE"),
                  process("m1", resource="M1", value=1.0, hold_until_space=True, space_of="buf"),
                  conveyor, buffer("buf", 2), process("m2", resource="M2", value=3.0),
                  dispose("out")]
        doc = layout(blocks, chain(*blocks), resources=[{"name": "M1", "capacity": 1},
                                                        {"name": "M2", "capacity": 1}],
                     entities=[sized("CASE", 0.41)])
        env, _built = run_env(doc, 100)
        assert close_all(exits(env, "CASE"), [10.0, 13.0, 17.0, 20.0]), typed


def test_merge_passers_stay_bounded_by_items_not_cells():
    """Review I3 (a count, not a clock): sparse joins on a 200 m typed belt of 20,000 cells.
    The passers a join scans are those still on the belt, so the list stays at a handful."""
    conveyor = belt(length=200.0, speed=10.0, merges=[{"at": 100.0, "from": "side"}])
    conveyor["params"]["type"] = "belt"
    blocks = [create(e_type="CASE", max_arrivals=50, dist_params={"value": 5.0}), conveyor,
              dispose(), create(name="side", e_type="CASE", max_arrivals=50,
                                dist_params={"value": 5.0}, start_time=2.5)]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    doc = model(blocks, links)
    doc["entities"] = [sized("CASE", 0.41)]
    env, built = run_env(doc, 1000)
    conveyor = built.blocks["belt"]
    assert conveyor.capacity == 20000 and len(exits(env, "CASE")) == 100
    assert len(conveyor._merges[0].passers) <= 10


def test_a_pallet_as_long_as_its_module_runs_with_a_gap():
    """Review M5: a 1.2 m typed module, gap 0.05, 1.2 m PALLETs. With the gap a pallet would
    need 125 cells of 120, but the gap is room to the next item: the pallet takes the belt.
    First boards 0, exits 1.2 / 0.5 = 2.4; the next boards after 1.25 / 0.5 = 2.5, exits 4.9.
    A 1.25 m pallet (125 cells by length alone) is refused."""
    def module(length):
        conveyor = belt(length=1.2, speed=0.5)
        conveyor["params"].update({"type": "pallet", "gap": 0.05})
        blocks = [create(e_type="PALLET", max_arrivals=2, dist_params={"value": 0.1}),
                  conveyor, dispose()]
        doc = model(blocks, [{"source": "create_1", "target": "belt"},
                             {"source": "belt", "target": "dispose_1"}])
        doc["entities"] = [sized("PALLET", length)]
        return doc
    env, _built = run_env(module(1.2), 100)
    assert close_all(exits(env, "PALLET"), [2.4, 4.9])
    with pytest.raises(RuntimeError, match="E_CONVEYOR_PARAM"):
        run_env(module(1.25), 100)


def test_a_typed_belt_is_refused_in_a_state_by_name():
    doc = merge_doc("SIDE")
    doc["simulation"]["time_limit"] = 30
    assert state.refused_kind(doc) == "a typed conveyor"


def test_a_part_on_or_held_by_a_typed_belt_in_an_initial_state_is_a_plain_limit():
    """M2: refused statically, with its own case and plain line (a documented limit, D1355),
    for a part on the belt and for one its end still hands over (held_by the belt)."""
    from conftest import problem_codes
    from nagasim.core.plainlines import plain
    from nagasim.core.schema import validate_model_structured
    on_belt = {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 7.0,
                                                    "entered": 1.0}}
    handed = {"block": "dispose_1", "count": 1, "held_by": "belt", "held_since": 1.0}
    for entry in (on_belt, handed):
        doc = merge_doc("SIDE")
        doc["initial_state"] = {"clock": 2.0, "entities": [entry]}
        assert "E_INITIAL_BELT" in problem_codes(doc)
        found = [p for p in validate_model_structured(doc) if p.code == "E_INITIAL_BELT"]
        assert found[0].fields["case"] == "typed", entry
        assert "typed belt" in plain(found[0])


def test_a_robot_pick_ends_before_the_first_back_eye_point_of_a_typed_belt():
    """Review M7: the gantry pick cell's 4 m belt (call at 0.5 m), a back eye at 1.0 m and
    SMALL 0.3 / LARGE 0.6 items. A typed belt's first back-eye point is the shortest item's,
    1.0 + 0.3 = 1.3, so the robot must meet the part before 1.3 m; the untyped belt keeps
    the spacing rule, 1.0 + 1.0 = 2.0."""
    from robotcases import gantry_pick_cell
    from nagasim.core import motion
    from nagasim.core.robotics.moves import places_of
    for typed, limit in ((False, 2.0), (True, 1.3)):
        cell = gantry_pick_cell()
        conveyor = next(b for b in cell["blocks"] if b["name"] == "belt")
        conveyor["params"]["eyes"] = [{"at": 1.0, "edge": "back"}]
        if typed:
            conveyor["params"]["type"] = "belt"
        cell["entities"] = [sized("SMALL", 0.3), sized("LARGE", 0.6)]
        robot = next(b for b in cell["blocks"] if b["name"] == "robot")
        pick = motion.belt_picks(cell, robot["params"]["targets"], places_of(cell))["box"]
        assert math.isclose(pick.limit, limit), typed
