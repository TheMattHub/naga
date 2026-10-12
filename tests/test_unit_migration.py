"""v28: every unit a record leaves missing or "base" is written out (units spec, D159)."""
import re
from pathlib import Path

import pytest

import nagasim
from conftest import chain, create, delay, dispose, layout, metric_value, problem_codes
from nagasim import api
from nagasim.core.schema.migrate import CURRENT_VERSION, migrate


def _model(time_unit="days"):
    blocks = [create(max_arrivals=5), delay(value=0.5),
              {"name": "m", "type": "MOTION", "position": [2, 0], "params": {"segments": []}},
              dispose()]
    lay = layout(blocks, chain(*blocks),
                 simulation={"time_limit": 10, "warmup": 0, "replications": 1,
                             "time_unit": time_unit},
                 fleets=[{"name": "F", "count": 1, "speed": 2.0, "cost_busy": 1}],
                 levels=[{"name": "L", "initial": 0}],
                 submodels={"D": {"blocks": [delay("inner")], "links": []}})
    lay["blocks"][0]["params"]["time_unit"] = "base"
    return lay


def test_base_and_missing_units_are_filled_with_what_they_meant():
    out = migrate(_model())
    params = {b["name"]: b["params"] for b in out["blocks"]}
    assert CURRENT_VERSION >= 28 and out["schema_version"] == CURRENT_VERSION
    assert out["simulation"]["length_unit"] == "meters"
    assert params["create_1"]["time_unit"] == "days"
    assert params["create_1"]["start_time_unit"] == "days"
    assert params["delay_1"]["time_unit"] == "days"
    assert params["m"]["time_unit"] == "seconds"                 # MOTION's own default
    assert out["fleets"][0]["speed_unit"] == "m/day"
    assert out["fleets"][0]["cost_time_unit"] == "hours"         # the field's own default
    assert out["levels"][0]["rate_time_unit"] == "days"
    assert migrate(out) == out                                   # idempotent


def test_agent_timing_units_are_filled_only_where_a_trigger_is_present():
    lay = layout([], [], simulation={"time_unit": "hours"},
                 agent_types=[{"name": "P", "states": [{"name": "A"}, {"name": "B"}],
                               "initial_state": "A", "tick": {"dt": 1},
                               "transitions": [{"from": "A", "to": "B", "timeout": "2"},
                                               {"from": "B", "to": "A", "condition": "true"}]}])
    agent = migrate(lay)["agent_types"][0]
    first, second = agent["transitions"]
    assert first["timeout_unit"] == "hours" and "scan_unit" not in first and "rate_unit" not in first
    assert set(second) == {"from", "to", "condition"}
    assert agent["tick"] == {"dt": 1, "time_unit": "hours"}


def test_a_filled_model_runs_identically():
    before, after = _model("minutes"), migrate(_model("minutes"))
    for lay in (before, after):
        lay.pop("fleets"), lay.pop("levels"), lay.pop("submodels")
        lay["blocks"] = [b for b in lay["blocks"] if b["name"] != "m"]
        lay["links"] = chain(*lay["blocks"])
    a = api.run_model(before, replications=1, seed=0)
    b = api.run_model(after, replications=1, seed=0)
    assert a["system"].equals(b["system"])


def test_base_in_a_definition_and_a_scenario_override_resolves_to_the_model_unit():
    out = migrate(_model())
    assert "time_unit" not in out["submodels"]["D"]["blocks"][0]["params"]   # a template
    blocks = [create(max_arrivals=3), delay(value=0.5), dispose()]
    runs = []
    for unit in ("base", "hours"):
        lay = layout(blocks, chain(*blocks), simulation={"time_limit": 10, "warmup": 0,
                                                         "replications": 1, "time_unit": "hours"})
        lay["blocks"][1]["params"]["time_unit"] = unit           # as an override writes it
        runs.append(metric_value(api.run_model(lay, replications=1, seed=0),
                                 "Avg Throughput Time"))
    assert runs[0] == runs[1] == pytest.approx(0.5)


def test_unknown_units_are_reported_by_quantity():
    belt = {"name": "belt", "type": "CONVEYOR", "position": [1, 0],
            "params": {"length": 5, "speed": 1, "speed_unit": "m/fortnight", "spacing": 1}}
    blocks = [create(start_time_unit="fortnights"), belt, dispose()]
    codes = problem_codes(layout(blocks, chain(*blocks)))
    assert {"E_TIME_UNIT", "E_UNIT"} <= codes


def test_every_field_conversion_goes_through_the_converter():
    allowed = {"core/timeunits.py", "core/quantities.py", "core/calendar.py", "core/export.py",
               "core/outputs.py", "core/motion.py", "databind.py"}
    call = re.compile(r"(?<![\w.])(?:unit_)?factor\(")
    root = Path(nagasim.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if call.search(code) and not code.lstrip().startswith("def "):
                offenders.append(f"{rel}:{n}")
    assert offenders == []
