"""Validation of CONVEYOR photo-eyes and intercept diverts (D1347), each case with its plain
line. A belt is 10 m long at 1 m/s with spacing 1 unless a test says otherwise."""

import copy

import pytest

from conftest import dispose, layout, problem_codes
from robotcases import gantry_pallet_cell, gantry_pick_cell
from test_conveyor_divert_spec import belt, plain_line, side_line

from nagasim.core import plainlines
from nagasim.core.schema import validate_model_structured


def doc(eyes=(), diverts=None, **belt_args):
    part = {k: belt_args.pop(k) for k in ("part_length",) if k in belt_args}
    conveyor = belt(diverts=diverts, **belt_args)
    conveyor["params"]["eyes"] = list(eyes)
    conveyor["params"].update(part)
    if diverts:
        blocks, links = side_line(conveyor, arrivals=3)
        return layout(blocks, links, variables=[{"name": "line", "initial": 0}])
    return {**plain_line(conveyor, arrivals=3), "variables": [{"name": "line", "initial": 0}]}


def found(document, code="E_CONVEYOR_EYE"):
    return [p for p in validate_model_structured(document) if p.code == code]


def errors(document):
    return [p for p in validate_model_structured(document) if p.severity == "error"]


def test_valid_eyes_raise_nothing():
    eyes = [{"at": 1}, {"at": 3, "edge": "back"},
            {"at": 5, "assign": [{"target": "entity.line", "expr": "2"},
                                 {"target": "line", "expr": "line + 1"}],
             "signal": "seen"}]
    assert errors(doc(eyes)) == []


CASES = [
    ({"at": 1, "colour": 1}, "a2_shape"),
    ({"at": 0}, "a2_off"),
    ({"at": 10}, "a2_off"),
    ({"at": 4}, "a2_taken"),
    ({"at": 9.5, "edge": "back"}, "a2_back"),
    ({"at": 2, "edge": "middle"}, "a2_edge"),
    ({"at": 2, "assign": [{"target": "", "expr": "1"}]}, "a2_assign"),
    ({"at": 2, "signal": 7}, "a2_signal"),
]


@pytest.mark.parametrize("eye, case", CASES)
def test_a_bad_eye_is_refused_by_case(eye, case):
    document = doc([eye], diverts=[{"at": 4, "condition": "True"}])
    cases = [p.fields["case"] for p in found(document)]
    assert cases == [case]
    assert plainlines.plain(found(document)[0])


def test_a_part_longer_than_its_spacing_is_refused():
    problems = found(doc([{"at": 2, "edge": "back"}], part_length=2))
    assert [p.fields["case"] for p in problems] == ["a2_length"]
    assert problems[0].path.endswith("/part_length")


def test_a_back_eye_is_judged_at_its_place_plus_the_part_length():
    ok = doc([{"at": 8.5, "edge": "back"}], part_length=1)
    assert found(ok) == []
    # Past the end by the part length, and the same place as a divert at 5 + 1.
    assert [p.fields["case"] for p in found(doc([{"at": 9.5, "edge": "back"}], part_length=1))] \
        == ["a2_back"]
    taken = doc([{"at": 4, "edge": "back"}], diverts=[{"at": 5, "condition": "True"}])
    assert [p.fields["case"] for p in found(taken)] == ["a2_taken"]


def test_a_bad_eye_expression_is_reported_at_its_row():
    document = doc([{"at": 2, "assign": [{"target": "entity.x", "expr": "nosuch + 1"}]}])
    assert any(p.path.endswith("/eyes/0/assign/0/expr") for p in validate_model_structured(document))
    undeclared = doc([{"at": 2, "assign": [{"target": "ghost", "expr": "1"}]}])
    assert "E_UNDECLARED_VARIABLE" in problem_codes(undeclared)


def test_the_gantry_pick_cells_validate():
    for cell in (gantry_pick_cell(), gantry_pick_cell("pass"),
                 gantry_pick_cell("entity.late == 1"), gantry_pallet_cell()):
        assert errors(cell) == []


def _divert(cell, **edits):
    cell = copy.deepcopy(cell)
    next(b for b in cell["blocks"] if b["name"] == "belt")["params"]["diverts"][0].update(edits)
    return cell


@pytest.mark.parametrize("edits, case", [
    ({"time": 1}, "a2_intercept"),
    ({"when_full": "pass"}, "a2_intercept"),
    ({"on_late": ""}, "a2_late"),
    ({"on_late": 3}, "a2_late"),
])
def test_a_bad_intercept_divert_is_refused(edits, case):
    cases = [p.fields["case"] for p in found(_divert(gantry_pick_cell(), **edits),
                                             "E_CONVEYOR_DIVERT")]
    assert cases == [case]


def test_an_intercept_divert_must_link_to_the_robot_that_picks_from_it():
    cell = gantry_pick_cell()
    away = copy.deepcopy(cell)
    away["blocks"].append(dispose(name="dispose_x"))
    next(link for link in away["links"] if link == {"source": "belt", "target": "robot"}
         )["target"] = "dispose_x"
    assert [p.fields["case"] for p in found(away, "E_CONVEYOR_DIVERT")] == ["a7_robot"]
    other = copy.deepcopy(cell)
    next(b for b in other["blocks"] if b["name"] == "robot")["params"]["targets"]["box"]["divert"] = 1
    assert [p.fields["case"] for p in found(other, "E_CONVEYOR_DIVERT")] == ["a7_robot"]


def test_on_late_without_intercept_is_a_wrong_setting():
    cell = _divert(gantry_pick_cell(), intercept=False)
    assert [p.fields["case"] for p in found(cell, "E_CONVEYOR_DIVERT")] == ["a2_shape"]


def test_an_expression_on_late_is_checked_at_its_path():
    cell = _divert(gantry_pick_cell(), on_late="nosuch(")
    assert any(p.path.endswith("/diverts/0/on_late") for p in validate_model_structured(cell))
