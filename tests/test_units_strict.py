"""Strict units (PHASE6 S9, `core/units.py`): `simulation.sd.units` modes,
table/time-series unit propagation, and the S2 function pack's unit rules.
Pure/stdlib, no simulation run -- keep this file fast."""
from nagasim.core.units import Env, Unit, WILDCARD, check, infer, parse


def _sd_layout(units_mode=None, **extra):
    sd = {"dt": 1.0}
    if units_mode is not None:
        sd["units"] = units_mode
    layout = {"blocks": [], "links": [], "simulation": {"sd": sd}}
    layout.update(extra)
    return layout


def _mismatched_layout(units_mode=None):
    return _sd_layout(
        units_mode,
        levels=[{"name": "L", "unit": "people"}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "kg/minutes"}])


# ---------------------------------------------------------------- modes ----
def test_warn_mode_is_default_and_only_warns():
    codes = {p.code for p in check(_mismatched_layout())}
    assert codes == {"W_UNIT_MISMATCH"}


def test_strict_mode_turns_mismatch_into_error():
    problems = check(_mismatched_layout("strict"))
    codes = {p.code for p in problems}
    assert "E_UNIT_MISMATCH" in codes
    assert "W_UNIT_MISMATCH" not in codes
    assert next(p for p in problems if p.code == "E_UNIT_MISMATCH").severity == "error"


def test_strict_mode_turns_time_scale_into_error():
    layout = _sd_layout(
        simulation={"sd": {"dt": 1.0, "units": "strict"}, "time_unit": "days"},
        levels=[{"name": "L", "unit": "people"}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "people/hour"}])
    codes = {p.code for p in check(layout)}
    assert "E_UNIT_TIME" in codes
    assert "W_UNIT_TIME" not in codes
    assert "E_UNIT_MISMATCH" not in codes


def test_off_mode_is_silent_even_on_a_real_mismatch():
    assert check(_mismatched_layout("off")) == []


def test_unrecognised_mode_falls_back_to_warn():
    codes = {p.code for p in check(_mismatched_layout("nonsense"))}
    assert codes == {"W_UNIT_MISMATCH"}


def test_strict_flags_missing_unit_as_a_warning_not_an_error():
    layout = _sd_layout(
        "strict",
        levels=[{"name": "L"}],
        flows=[{"name": "f", "to": "L", "rate": "10"}])
    codes = {p.code for p in check(layout)}
    assert codes == {"W_UNIT_MISSING"}
    assert "E_UNIT_MISMATCH" not in codes and "E_UNIT_MISSING" not in codes


def test_warn_mode_does_not_flag_missing_units():
    layout = _sd_layout(levels=[{"name": "L"}], flows=[{"name": "f", "to": "L", "rate": "10"}])
    assert check(layout) == []


def test_strict_canonical_rate_is_clean():
    # A textbook SD rate (level unit / model time unit): strict must not
    # invent a false positive on the very case the checker exists to accept.
    layout = _sd_layout(
        "strict",
        levels=[{"name": "L", "unit": "people", "initial": 0}],
        flows=[{"name": "in", "to": "L", "rate": "10", "unit": "people/minutes"}])
    assert check(layout) == []


# ---------------------------------------------------------------- tables/TS #
def test_table_unit_y_propagates_and_x_is_checked():
    env = Env(table_lookup=lambda n: (parse("people"), parse("dollar")))
    assert infer('TABLE("price", 5)', env).dims == parse("dollar").dims


def test_table_x_mismatch_raises():
    from nagasim.core.units import UnitMismatchError
    import pytest
    env = Env(levels={"kg_level": parse("kg")}, table_lookup=lambda n: (parse("people"), parse("dollar")))
    with pytest.raises(UnitMismatchError):
        infer('TABLE("price", LEVEL("kg_level"))', env)


def test_table_units_wired_through_check():
    layout = _sd_layout(
        "warn",
        levels=[{"name": "L", "unit": "kg"}],
        tables=[{"name": "T", "keys": [0, 1], "values": [0, 1],
                 "unit_x": "people", "unit_y": "dollar"}],
        auxiliaries=[{"name": "a", "expr": 'TABLE("T", LEVEL("L"))', "unit": "dollar"}])
    codes = {p.code for p in check(layout)}
    assert "W_UNIT_MISMATCH" in codes         # LEVEL("L") is kg, table wants people


def test_series_unit_propagates():
    env = Env(series_lookup=lambda n: parse("kWh"))
    assert infer('TS("load")', env).dims == parse("kWh").dims


def test_series_units_wired_through_check():
    layout = _sd_layout(
        "warn",
        levels=[{"name": "L", "unit": "kg"}],
        timeseries_inputs=[{"name": "s", "start": 0, "step": 1, "values": [1, 2], "unit": "kWh"}],
        auxiliaries=[{"name": "a", "expr": 'TS("s")', "unit": "kWh"}])
    assert check(layout) == []


# ---------------------------------------------------------------- S2 rules #
def _env(**kwargs):
    return Env(**kwargs)


def test_zidz_xidz_divide_units():
    env = _env(levels={"a": parse("people"), "b": parse("day")})
    assert infer('ZIDZ(LEVEL("a"), LEVEL("b"))', env).dims == parse("people/day").dims
    assert infer('XIDZ(LEVEL("a"), LEVEL("b"), 0)', env).dims == parse("people/day").dims


def test_modulo_keeps_first_arg_unit():
    env = _env(levels={"a": parse("people"), "b": parse("people")})
    assert infer('MODULO(LEVEL("a"), LEVEL("b"))', env).dims == parse("people").dims


def test_initial_keeps_x_unit():
    env = _env(levels={"x": parse("kg")})
    assert infer('INITIAL(LEVEL("x"))', env).dims == parse("kg").dims


def test_sample_if_true_keeps_x_unit():
    env = _env(levels={"x": parse("kg")})
    assert infer('SAMPLE_IF_TRUE(TNOW > 0, LEVEL("x"), 0)', env).dims == parse("kg").dims


def test_trend_is_one_over_time():
    env = _env(levels={"x": parse("people")}, time_unit=parse("day"))
    assert infer('TREND(LEVEL("x"), 5)', env).dims == (Unit() / parse("day")).dims


def test_forecast_keeps_x_unit():
    env = _env(levels={"x": parse("people")})
    assert infer('FORECAST(LEVEL("x"), 5, 10)', env).dims == parse("people").dims


def test_npv_keeps_stream_unit():
    env = _env(levels={"stream": parse("dollar/day")})
    assert infer('NPV(LEVEL("stream"), 0.1, 0, 1)', env).dims == parse("dollar/day").dims


def test_prod_vmin_vmax_keep_x_unit():
    env = _env(levels={"x": parse("kg")})
    for call in ('PROD(LEVEL("x"))', 'VMIN(LEVEL("x"))', 'VMAX(LEVEL("x"))'):
        assert infer(call, env).dims == parse("kg").dims


def test_noise_normal_keeps_mean_unit():
    env = _env(levels={"m": parse("people")})
    assert infer('NOISE_NORMAL(LEVEL("m"), 1, 0, 100)', env).dims == parse("people").dims
