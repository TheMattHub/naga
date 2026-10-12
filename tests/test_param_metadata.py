"""Field metadata on every block parameter and section column, and the one
converter (units spec, D159, D161)."""
import pytest

from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.block_spec import KINDS, ParamSpec
from nagasim.core.quantities import (
    SECTION_FIELDS, field_factor, scaled, switch_unit, to_base,
)
from nagasim.core.sections import SECTIONS
from nagasim.core.sim_runner import initialize_sim_env
from nagasim.core.timeunits import QUANTITIES, ModelUnits
from nagasim.modelschema import build_json_schema

BUILTIN = {t: s for t, s in BLOCK_REGISTRY.items()
           if s.cls.__module__.startswith("nagasim.core.blocks")}


def test_every_block_parameter_declares_its_quantity():
    missing = sorted(f"{t}.{p.name}" for t, s in BUILTIN.items() for p in s.params
                     if p.quantity is None)
    assert missing == []


def test_block_metadata_is_well_formed():
    for type_name, spec in BUILTIN.items():
        names = {p.name for p in spec.params}
        for p in spec.params:
            assert p.quantity in QUANTITIES, (type_name, p.name)
            assert set(p.field_kinds) <= set(KINDS), (type_name, p.name)
            if p.unit_field:
                assert p.unit_field in names, (type_name, p.name, p.unit_field)


def test_kinds_and_unit_key_defaults():
    assert ParamSpec("a", bool, False, quantity="none").field_kinds == ("bool",)
    assert ParamSpec("a", int, 0, quantity="count").field_kinds == ("integer",)
    assert ParamSpec("a", float, 0.0, quantity="time").unit_field == "a_unit"
    assert ParamSpec("a", float, 0.0, quantity="count").unit_field is None
    assert ParamSpec("a", float, 0.0, quantity="length", unit_key="").unit_field == ""


def _numeric(schema) -> bool:
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return bool({"number", "integer"} & set(kinds)) or any(
        _numeric(s) for s in schema.get("anyOf", []) + schema.get("oneOf", []))


def _section_properties(defs):
    for section in SECTIONS:
        if section.record:
            name = {"entities": "entity_type"}.get(section.name, section.item)
            yield section.name, defs[name]["properties"]
    yield "simulation", defs["simulation"]["properties"]
    yield "investment", defs["investment"]["properties"]
    yield "simulation.sd", defs["simulation"]["properties"]["sd"]["properties"]
    yield "network", defs["network"]["properties"]
    yield "network.segments", defs["network"]["properties"]["segments"]["items"]["properties"]
    yield "agent_types.transitions", defs["agent_transition"]["properties"]
    yield "agent_types.tick", defs["agent_tick"]["properties"]


def test_every_numeric_section_column_has_metadata():
    for key, props in _section_properties(build_json_schema()["$defs"]):
        declared = {p.name for p in SECTION_FIELDS.get(key, ())}
        missing = sorted(n for n, s in props.items() if _numeric(s) and n not in declared)
        assert missing == [], key
    for key, fields in SECTION_FIELDS.items():
        for p in fields:
            assert p.quantity in QUANTITIES, (key, p.name)


def test_to_base_leaves_a_base_unit_value_and_expression_untouched():
    units = ModelUnits("minutes")
    speed = ParamSpec("speed", str, "", quantity="speed", kinds=("number", "expression"))
    for record in ({"speed": "entity.v", "speed_unit": "m/min"}, {"speed": "entity.v"},
                   {"speed": "entity.v", "speed_unit": "base"}):
        assert to_base(speed, record, units) == "entity.v"
    assert to_base(speed, {"speed": "entity.v", "speed_unit": "m/s"}, units) == "(entity.v) * 60.0"
    rate = ParamSpec("max_inflow", float, None, quantity="rate", unit_key="rate_time_unit")
    assert field_factor(rate, {"rate_time_unit": "minutes"}, units) == 1.0
    value = 0.1 + 0.2
    assert scaled(value, 1.0) is value
    assert scaled("5", 60.0) == "300.0" and scaled("5", 1.0) == "5"
    assert to_base(ParamSpec("t", float, 0.0, quantity="time"), {"t": 3, "t_unit": "hours"},
                   units) == 180.0


def test_switch_unit_converts_every_field_measured_in_the_key():
    units = ModelUnits("minutes")
    delay = BLOCK_REGISTRY["DELAY"].params
    out, kept = switch_unit({"distribution": "Exponential", "dist_params": {"rate": 0.5},
                             "time_unit": "minutes"}, delay, "time_unit", "seconds", units)
    assert out["time_unit"] == "seconds" and kept == []
    assert out["dist_params"]["rate"] == pytest.approx(0.5 / 60)
    out, kept = switch_unit({"distribution": "Bernoulli", "dist_params": {"p": 0.5}}, delay,
                            "time_unit", "seconds", units)
    assert out["dist_params"] == {"p": 0.5} and kept == ["dist_params"]
    out, _ = switch_unit({"length": 2.0, "length_unit": "meters", "spacing": 1.0},
                         BLOCK_REGISTRY["CONVEYOR"].params, "length_unit", "millimeters", units)
    assert out == {"length": 2000.0, "length_unit": "millimeters", "spacing": 1.0}
    failure = {"name": "f", "uptime": {"distribution": "Constant", "dist_params": {"value": 2},
                                       "time_unit": "hours"}}
    out, _ = switch_unit(failure, SECTION_FIELDS["failures"], "uptime.time_unit", "minutes", units)
    assert out["uptime"] == {"distribution": "Constant", "dist_params": {"value": 120.0},
                             "time_unit": "minutes"}


def test_switch_unit_converts_plain_motion_segment_times_and_reports_the_rest():
    segments = [{"kind": "move", "distance_m": 1}, {"kind": "pick", "time": 2},
                {"kind": "place", "time": "1.5"}, {"kind": "dwell", "time": "TRIA(1,2,3)"}]
    out, kept = switch_unit({"segments": segments, "time_unit": "seconds"},
                            BLOCK_REGISTRY["MOTION"].params, "time_unit", "milliseconds",
                            ModelUnits("minutes"))
    assert [s.get("time") for s in out["segments"]] == [None, 2000.0, "1500.0", "TRIA(1,2,3)"]
    assert kept == ["segments[3]"] and out["time_unit"] == "milliseconds"


def test_the_run_context_carries_the_model_units():
    assert initialize_sim_env(seed=0).ctx.units == ModelUnits()
