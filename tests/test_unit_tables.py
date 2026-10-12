"""Unit tables and conversion primitives (units spec, D159-D161)."""
import math

import pytest

from nagasim.core.distributions import rescale_distribution_params
from nagasim.core.timeunits import (
    UNITS, ModelUnits, base_factor, convert, default_unit, factor, is_unit, unit_choices,
    units_of,
)


def test_milliseconds_join_the_time_table_without_moving_the_others():
    assert factor("milliseconds", "seconds") == 0.001
    assert factor("minutes", "hours") == 60 / 3600
    assert list(UNITS)[:2] == ["milliseconds", "seconds"]


@pytest.mark.parametrize("quantity, unit, expected", [
    ("length", "millimeters", 0.001), ("length", "kilometers", 1000.0),
    ("mass", "g", 0.001), ("mass", "t", 1000.0),
    ("energy", "Wh", 0.001), ("energy", "MWh", 1000.0),
    ("power", "W", 0.001), ("power", "MW", 1000.0),
    ("time", "seconds", 1 / 60), ("time", "hours", 60.0),
    ("rate", "hours", 1 / 60), ("speed", "m/s", 60.0), ("speed", "km/h", 1000 / 60),
])
def test_base_factor_per_quantity_in_a_minutes_model(quantity, unit, expected):
    assert base_factor(quantity, unit, ModelUnits("minutes")) == pytest.approx(expected)


def test_the_default_unit_is_exactly_the_engine_base():
    units = ModelUnits("hours", "meters")
    for quantity in ("time", "rate", "length", "speed", "mass", "energy", "power"):
        assert base_factor(quantity, default_unit(quantity, units), units) == 1.0
    assert default_unit("speed", units) == "m/h"
    assert base_factor("time", "base", units) == 1.0
    assert base_factor("money", "EUR", units) == 1.0
    km = ModelUnits(length="kilometers")        # lengths run in metres whatever the model says
    assert base_factor("length", default_unit("length", km), km) == 1000.0


def test_conversion_keeps_meaning_both_ways():
    assert convert(1, "time", "minutes", "seconds") == 60
    assert convert(60, "time", "seconds", "minutes") == pytest.approx(1)
    assert convert(10, "rate", "hours", "minutes") == pytest.approx(10 / 60)
    assert convert(1, "speed", "m/s", "km/h") == pytest.approx(3.6)
    there = convert(2.5, "length", "meters", "millimeters")
    assert convert(there, "length", "millimeters", "meters") == pytest.approx(2.5)
    assert convert(5, "money", "EUR", "EUR") == 5
    assert convert(3, "time", "base", "seconds", ModelUnits("minutes")) == 180


def test_unit_choices_and_validity():
    assert unit_choices("speed", ModelUnits("hours")) == ("m/s", "m/min", "km/h", "m/h")
    assert unit_choices("length") == ("millimeters", "centimeters", "meters", "kilometers")
    assert unit_choices("money") == () and unit_choices("count") == ()
    assert is_unit("speed", "cm/day") and not is_unit("speed", "m/fortnight")
    assert is_unit("time", "milliseconds") and not is_unit("time", "fortnights")
    assert is_unit("mass", "t") and not is_unit("mass", "lb")


def test_units_of_reads_the_simulation_section_with_todays_defaults():
    assert units_of({}) == ModelUnits("minutes", "meters", "EUR")
    assert units_of(None) == ModelUnits()
    assert units_of({"time_unit": "hours", "length_unit": "kilometers", "currency": "USD"}) \
        == ModelUnits("hours", "kilometers", "USD")


def test_rescale_distribution_params_keeps_the_shape_and_refuses_unscalable_families():
    assert rescale_distribution_params("Constant", {"value": 2}, 60.0) == {"value": 120.0}
    assert rescale_distribution_params("Exponential", {"rate": 0.5}, 60.0)["rate"] \
        == pytest.approx(0.5 / 60)
    assert rescale_distribution_params("Weibull", {"scale": 2.0, "shape": 1.5}, 60.0) \
        == {"scale": 120.0, "shape": 1.5}
    assert rescale_distribution_params("Lognormal", {"mu": 0.0, "sigma": 1.0}, 60.0)["mu"] \
        == pytest.approx(math.log(60))
    assert rescale_distribution_params("Uniform", {"low": "entity.a", "high": 3}, 60.0) \
        == {"low": "(entity.a) * 60.0", "high": 180.0}
    assert rescale_distribution_params("CustomExpression", {"expression": "EXPO(3)"}, 60.0) \
        == {"expression": "(EXPO(3)) * 60.0"}
    assert rescale_distribution_params("Bernoulli", {"p": 0.5}, 60.0) is None
    assert rescale_distribution_params("Exponential", {"rate": 0.5}, 1.0) == {"rate": 0.5}
