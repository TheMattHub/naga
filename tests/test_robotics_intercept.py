"""A robot pick from a moving belt, planned before the run: segment 0's ptp to a belt
target is met by the intercept solver from the cycle's closing pose, for a MOTION and a
pattern BATCH (plan 2026-11-19-p3-conveyor-detail, Task 2).

Hand facts (tests/robotics/test_oracle_moves.py): the gantry's x axis runs 1 m/s at
2 m/s^2, so a move of d >= 0.5 m takes d + 0.5 s. The cell's belt starts at x = 0 and
runs +x at 0.5 m/s; the call is at 0.5 m, so from x = 0 the robot meets the box when
0.5 + 0.5t + 0.5 = t: t = 2.0 s at 1.5 m.
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from robotcases import gantry_pallet_cell, gantry_pick_cell

from nagasim.core import motion, pallets
from nagasim.core.beltpick import BeltMeet, belt_meets
from nagasim.core.network import rotate_offset
from nagasim.core.robotics.moves import Place, belt_line, places_of


def _block(layout, name):
    return next(b for b in layout["blocks"] if b["name"] == name)


def _plan(layout, name="robot"):
    return motion.block_robot_plan(layout, _block(layout, name))


def test_belt_line_takes_the_grip_height():
    line = belt_line(Place(2.0, 0.0, 0.0, 4.0), 0.5, 0.5, z=2.5)
    assert np.allclose(line.origin, [0, 0, 2.5])
    assert np.allclose(line.direction, [1, 0, 0])
    assert line.s0 == 0.5 and line.speed == 0.5


def test_the_gantry_pick_meets_the_part_where_the_oracle_does():
    cell = gantry_pick_cell()
    plan = _plan(cell)
    assert plan.errors == ()
    assert plan.moves[0].seconds == pytest.approx(2.0, abs=1e-5)
    assert plan.moves[0].meet == pytest.approx(1.5, abs=1e-5)
    assert plan.moves[2].seconds == pytest.approx(2.0)            # 1.5 m back to x = 0
    segments = _block(cell, "robot")["params"]["segments"]
    assert motion.nominal_cycle_seconds(segments, None, plan=plan) == pytest.approx(6.0, abs=1e-5)


def test_a_rotated_cell_meets_at_the_same_time_and_distance():
    plain = _plan(gantry_pick_cell()).moves[0]
    cell = gantry_pick_cell()
    robot, belt = _block(cell, "robot"), _block(cell, "belt")
    robot["layout_position"], robot["layout_rotation"] = [10.0, 5.0], 90.0
    dx, dy = rotate_offset(2.0, 0.0, 90)
    belt["layout_position"], belt["layout_rotation"] = [10.0 + dx, 5.0 + dy], 90.0
    plan = _plan(cell)
    assert plan.errors == ()
    assert plan.moves[0].seconds == pytest.approx(plain.seconds, abs=1e-5)
    assert plan.moves[0].meet == pytest.approx(plain.meet, abs=1e-5)


def test_belt_speed_is_read_in_metres_per_second():
    cell = gantry_pick_cell()
    cell["simulation"]["time_unit"] = "minutes"
    _block(cell, "belt")["params"]["speed"] = 30.0               # 30 m/min
    targets = _block(cell, "robot")["params"]["targets"]
    pick = motion.belt_picks(cell, targets, places_of(cell))["box"]
    assert pick.line.speed == pytest.approx(0.5)
    assert pick.line.s0 == pytest.approx(0.5)


def _reorder(cell):
    segments = _block(cell, "robot")["params"]["segments"]
    segments[0], segments[2] = segments[2], segments[0]


def _seg0_lin(cell):
    _block(cell, "robot")["params"]["segments"][0]["kind"] = "lin"


def _divert_3(cell):
    _block(cell, "robot")["params"]["targets"]["box"]["divert"] = 3


def _no_flag(cell):
    del _block(cell, "belt")["params"]["diverts"][0]["intercept"]


def _fast_belt(cell):
    _block(cell, "belt")["params"]["speed"] = 2.0


def _short_belt(cell):
    # 1.2 m from x = 0: the meeting point 1.5 m is past the end.
    belt = _block(cell, "belt")
    belt["params"]["length"], belt["layout_position"] = 1.2, [0.6, 0.0]


def _stop_at_1(cell):
    _block(cell, "belt")["params"]["stops"] = [{"at": 1.0, "time": 0}]


@pytest.mark.parametrize("edit, case, limit", [
    (_reorder, "first", None), (_seg0_lin, "ptp", None), (_divert_3, "belt", None),
    (_no_flag, "belt", None), (_fast_belt, "reach", None), (_short_belt, "past", 1.2),
    (_stop_at_1, "past", 1.0)])
def test_a_bad_belt_pick_is_refused_by_case(edit, case, limit):
    cell = gantry_pick_cell()
    edit(cell)
    plan = _plan(cell)
    assert plan.errors[0].code == "E_ROBOT_INTERCEPT"
    assert plan.errors[0].fields["case"] == case
    if limit is not None:
        assert plan.errors[0].fields["limit"] == pytest.approx(limit)
        assert plan.errors[0].fields["meet"] == pytest.approx(1.5, abs=1e-3)


def test_a_second_belt_pick_in_one_cycle_is_refused():
    cell = gantry_pick_cell()
    params = _block(cell, "robot")["params"]
    params["targets"]["again"] = copy.deepcopy(params["targets"]["box"])
    params["segments"][2]["to"] = "again"
    assert [(e.code, e.fields["case"]) for e in _plan(cell).errors] == [("E_ROBOT_INTERCEPT", "two")]


def test_a_palletizer_meets_the_part_as_the_motion_robot_does():
    layout = gantry_pallet_cell()
    cycles, errors = pallets.slot_cycles(layout, _block(layout, "palletizer"))
    assert errors == [] and len(cycles) == 2 and None not in cycles
    got = belt_meets(layout)["palletizer"]
    assert got == BeltMeet(pytest.approx(2.0, abs=1e-5), pytest.approx(1.5, abs=1e-5))
    motion_meet = belt_meets(gantry_pick_cell())["robot"]
    assert got.approach == pytest.approx(motion_meet.approach, abs=1e-9)
    assert got.meet == pytest.approx(motion_meet.meet, abs=1e-9)


def _drop_home(params):
    params["segments"].pop()


def _per_layer(params):
    params["place_per"] = "layer"


@pytest.mark.parametrize("edit, case", [(_drop_home, "start"), (_per_layer, "layer")])
def test_a_palletizer_belt_pick_is_refused_by_case(edit, case):
    layout = gantry_pallet_cell()
    block = _block(layout, "palletizer")
    edit(block["params"])
    _cycles, errors = pallets.slot_cycles(layout, block)
    assert errors[0][1].code == "E_ROBOT_INTERCEPT"
    assert errors[0][1].fields["case"] == case


def test_a_depalletizer_has_no_belt_pick():
    layout = gantry_pallet_cell()
    block = _block(layout, "palletizer")
    block["type"] = "SEPARATE"
    _cycles, errors = pallets.slot_cycles(layout, block)
    assert (errors[0][1].code, errors[0][1].fields["case"]) == ("E_ROBOT_INTERCEPT", "belt")


def test_a_model_without_a_belt_pick_has_no_meets():
    assert belt_meets({"blocks": []}) == {}
    cell = gantry_pick_cell()
    _block(cell, "robot")["params"]["targets"]["box"] = {"joints": [1.0, 0, 0]}
    assert belt_meets(cell) == {}
