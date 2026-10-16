"""D577/D578: a scenario's optional `goal` -- schema, validation, agent tool, bundled model."""
import json
from pathlib import Path

from nagasim import experiments
from nagasim.core.schema.prepare import validate_model_structured
from nagasim.modelschema import build_json_schema

MODEL = Path(experiments.__file__).parent / "models" / "automation_cell.json"


def _layout():
    return json.loads(MODEL.read_text(encoding="utf-8"))


def _goal_problems(layout):
    return [p for p in validate_model_structured(layout) if p.code == "E_SCENARIO_GOAL"]


def test_the_schema_offers_a_goal_string_with_a_ceiling():
    goal = build_json_schema()["$defs"]["scenario"]["properties"]["goal"]
    assert goal["type"] == "string" and goal["maxLength"] == 500


def test_a_goal_is_text_of_at_most_500_characters():
    layout = _layout()
    assert layout["scenarios"][0]["goal"] and not _goal_problems(layout)
    layout["scenarios"][0]["goal"] = 7
    assert [p.path for p in _goal_problems(layout)] == ["/scenarios/0/goal"]
    layout["scenarios"][0]["goal"] = "x" * 501
    assert _goal_problems(layout)


def test_scenarios_of_keeps_the_goal():
    assert experiments.scenarios_of(_layout())[1]["goal"].startswith("Does a robot arm")

