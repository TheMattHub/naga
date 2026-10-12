"""Unit checking (PHASE5 SD-3, `core/units.py`): the parser, AST inference and
`check()`'s warnings. Pure/stdlib, no simulation run -- keep this file fast."""
import pytest

from nagasim.core.units import (Env, Unit, UnitMismatchError, UnitParseError, WILDCARD,
                                check, infer, parse)


# ---------------------------------------------------------------- parse ----
def test_parse_simple():
    assert parse("people") == Unit((("people", 1),))


def test_parse_empty_and_dimensionless():
    assert parse("") == Unit()
    assert parse("dimensionless") == Unit()


def test_parse_canonical_equality_regardless_of_grouping():
    # kg*m/s^2 and m*kg/s/s are the same dimension, written two ways.
    assert parse("kg*m/s^2") == parse("m*kg/s/s")


def test_parse_division_chain():
    u = parse("kg/m/s")
    assert dict(u.dims) == {"kg": 1, "m": -1, "time": -1}


def test_parse_rate_unit():
    u = parse("people/day")
    assert dict(u.dims) == {"people": 1, "time": -1}


def test_parse_bare_literal_over_time():
    u = parse("1/day")
    assert dict(u.dims) == {"time": -1}


def test_parse_time_units_normalise_to_one_dimension():
    hour = parse("people/hour")
    day = parse("people/day")
    assert hour.dims == day.dims        # same dimension
    assert hour.scale != day.scale      # different time scale


@pytest.mark.parametrize("text", ["kg m", "kg**", "kg/", "/kg", "kg^", "kg^x", "kg!m"])
def test_parse_rejects_malformed_text(text):
    with pytest.raises(UnitParseError):
        parse(text)


# ---------------------------------------------------------------- infer ----
def _env(**kwargs):
    return Env(**kwargs)


def test_infer_numeric_literal_is_wildcard():
    assert infer("5", _env()) is WILDCARD
    assert infer("", _env()) is WILDCARD


def test_infer_addition_requires_equal_units():
    env = _env(levels={"a": parse("people"), "b": parse("kg")})
    with pytest.raises(UnitMismatchError):
        infer('LEVEL("a") + LEVEL("b")', env)


def test_infer_addition_with_matching_units_ok():
    env = _env(levels={"a": parse("people"), "b": parse("people")})
    assert infer('LEVEL("a") + LEVEL("b")', env).dims == parse("people").dims


def test_infer_addition_literal_is_wildcard_no_mismatch():
    env = _env(levels={"a": parse("people")})
    assert infer('LEVEL("a") + 5', env).dims == parse("people").dims


def test_infer_multiplication_combines():
    env = _env(levels={"rate": parse("people/day"), "price": parse("dollar")})
    u = infer('LEVEL("rate") * LEVEL("price")', env)
    assert dict(u.dims) == {"people": 1, "dollar": 1, "time": -1}


def test_infer_division_combines():
    env = _env(levels={"a": parse("people"), "b": parse("day")})
    u = infer('LEVEL("a") / LEVEL("b")', env)
    assert u.dims == parse("people/day").dims


def test_infer_level_lookup():
    env = _env(levels={"stock": parse("kg")})
    assert infer('LEVEL("stock")', env).dims == parse("kg").dims


def test_infer_aux_lookup_via_callable():
    env = _env(aux_lookup=lambda name: parse("people") if name == "pop" else WILDCARD)
    assert infer('AUX("pop")', env).dims == parse("people").dims


def test_infer_delay_lookup_via_callable():
    env = _env(delay_lookup=lambda name: parse("kg") if name == "d" else WILDCARD)
    assert infer('DELAY("d")', env).dims == parse("kg").dims


def test_infer_computed_ref_name_is_wildcard():
    env = _env(levels={"x": parse("kg")})
    assert infer('LEVEL("x" if TNOW > 0 else "x")', env) is WILDCARD


def test_infer_if_unifies_branches():
    env = _env(levels={"a": parse("people"), "b": parse("people")})
    u = infer('IF(TNOW > 0, LEVEL("a"), LEVEL("b"))', env)
    assert u.dims == parse("people").dims


def test_infer_if_mismatch_falls_back_to_wildcard_not_error():
    env = _env(levels={"a": parse("people"), "b": parse("kg")})
    assert infer('IF(TNOW > 0, LEVEL("a"), LEVEL("b"))', env) is WILDCARD


def test_infer_min_max_unify():
    env = _env(levels={"a": parse("people"), "b": parse("people")})
    assert infer('min(LEVEL("a"), LEVEL("b"))', env).dims == parse("people").dims


def test_infer_step_pulse_use_height():
    env = _env(levels={"h": parse("people")})
    assert infer('STEP(LEVEL("h"), 5)', env).dims == parse("people").dims
    assert infer('PULSE(LEVEL("h"), 5, 1)', env).dims == parse("people").dims


def test_infer_ramp_is_slope_times_time():
    env = _env(levels={"slope": parse("people/day")}, time_unit=parse("day"))
    u = infer('RAMP(LEVEL("slope"), 0)', env)
    assert u.dims == parse("people").dims


def test_infer_smooth_delay_macros_use_input_unit():
    env = _env(levels={"x": parse("kg")})
    assert infer('SMOOTH(LEVEL("x"), 3)', env).dims == parse("kg").dims
    assert infer('DELAYN(LEVEL("x"), 3, 2)', env).dims == parse("kg").dims


def test_infer_ts_table_are_wildcard():
    env = _env()
    assert infer('TS("profile")', env) is WILDCARD
    assert infer('TABLE("t", 1)', env) is WILDCARD


def test_infer_unknown_function_is_wildcard_never_false_positive():
    env = _env(levels={"a": parse("people")})
    assert infer('NORM(LEVEL("a"), 1)', env) is WILDCARD


def test_infer_tnow_is_time():
    env = _env(time_unit=parse("day"))
    assert infer("TNOW", env).dims == parse("day").dims


# ---------------------------------------------------------------- check ----
def _sd_layout(**extra):
    layout = {"blocks": [], "links": [], "simulation": {"sd": {"dt": 1.0}}}
    layout.update(extra)
    return layout


def test_check_no_sd_section_returns_nothing():
    layout = {"blocks": [], "links": [],
              "levels": [{"name": "L", "unit": "kg"}],
              "flows": [{"name": "f", "from": "L", "rate": '1', "unit": "people"}]}
    assert check(layout) == []


def test_check_consistent_units_no_warnings():
    layout = _sd_layout(
        levels=[{"name": "L", "unit": "people", "initial": 0}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "people/minutes"}])
    assert check(layout) == []


def test_check_flow_unit_mismatches_level():
    layout = _sd_layout(
        levels=[{"name": "L", "unit": "people"}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "kg/minutes"}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_MISMATCH" in codes


def test_check_flow_time_scale_mismatch():
    layout = _sd_layout(
        simulation={"sd": {"dt": 1.0}, "time_unit": "days"},
        levels=[{"name": "L", "unit": "people"}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "people/hour"}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_TIME" in codes
    assert "W_UNIT_MISMATCH" not in codes


def test_check_unparsed_unit():
    layout = _sd_layout(levels=[{"name": "L", "unit": "kg m"}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_UNPARSED" in codes


def test_check_auxiliary_declared_vs_inferred_mismatch():
    layout = _sd_layout(
        levels=[{"name": "L", "unit": "kg"}],
        auxiliaries=[{"name": "a", "expr": 'LEVEL("L")', "unit": "people"}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_MISMATCH" in codes


def test_check_auxiliary_consistent_no_warning():
    layout = _sd_layout(
        levels=[{"name": "L", "unit": "kg"}],
        auxiliaries=[{"name": "a", "expr": 'LEVEL("L")', "unit": "kg"}])
    assert check(layout) == []


def test_check_internal_addition_mismatch_in_rate():
    layout = _sd_layout(
        levels=[{"name": "a", "unit": "people"}, {"name": "b", "unit": "kg"}],
        flows=[{"name": "f", "from": "a", "rate": 'LEVEL("a") + LEVEL("b")'}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_MISMATCH" in codes


def test_check_no_declared_units_no_warnings():
    layout = _sd_layout(
        levels=[{"name": "L"}],
        flows=[{"name": "f", "to": "L", "rate": "10"}])
    assert check(layout) == []
