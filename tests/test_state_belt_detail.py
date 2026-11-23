"""States refuse belts with photo-eyes or a robot pick by name (D1347, D1343): a part's stage
between an eye or a call and its meeting point is not something a state carries."""

import pytest

from conftest import problem_codes
from robotcases import gantry_pick_cell
from test_conveyor_eyes import eyed, plain_line

from nagasim import state
from nagasim.core.schema import validate_model_structured

KIND = "a conveyor with photo-eyes or a robot pick"


def _eye_model():
    doc = plain_line(eyed([{"at": 2.5}]), arrivals=6)
    doc["simulation"]["time_limit"] = 30
    return doc


def test_a_belt_with_an_eye_is_refused_in_a_state_by_name():
    doc = _eye_model()
    assert state.refused_kind(doc) == KIND
    with pytest.raises(state.StateError) as raised:
        state.take(doc, 3.0)
    assert "photo-eyes" in raised.value.kind


def test_a_belt_with_a_robot_pick_is_refused_in_a_state_by_name():
    doc = gantry_pick_cell()
    assert "photo-eyes" in (state.refused_kind(doc) or "")
    with pytest.raises(state.StateError) as raised:
        state.take(doc, 3.0)
    assert "robot pick" in raised.value.kind


def test_a_part_on_a_belt_with_an_eye_in_initial_state_is_refused():
    doc = _eye_model()
    doc["initial_state"] = {"clock": 2.0, "entities": [
        {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 7.0, "entered": 1.0}}]}
    assert "E_INITIAL_BELT" in problem_codes(doc)
    found = [p for p in validate_model_structured(doc) if p.code == "E_INITIAL_BELT"]
    assert found and found[0].fields["case"] == "refuse"
