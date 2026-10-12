"""Time units (ROADMAP 1.5). `simulation.time_unit` (default "minutes") is the
model's base unit: run length, warm-up, CREATE start_time, HOLD scan_interval,
every reported time and rate are in it. Any duration-bearing field may instead
declare its own unit; "base" (the default) means "already in the model's base
unit", so an old model with no unit key behaves exactly as before. Stdlib only.

Units spec, D159-D161: tables for every quantity; the engine's internal base is
the model time unit for time and rates, metres, kg, kWh, kW, and metres per
model time unit for speed. A factor of exactly 1.0 means "already in base".

Weeks, months and years (DEVLOG #157) have FIXED lengths -- 7, 30 and 365
days -- so an SD model written per month runs in its own unit at scale 1 (the
importers never rescale); a calendar month or leap year is not modelled.
"""
from __future__ import annotations

import math

from typing import NamedTuple


__all__ = ["UNITS", "LENGTH_UNITS", "MASS_UNITS", "ENERGY_UNITS", "POWER_UNITS", "SPEED_UNITS",
           "TIME_ABBR", "LENGTH_ABBR", "SPEED_PATTERN", "QUANTITIES", "UNIT_QUANTITIES",
           "ModelUnits", "units_of", "default_unit", "unit_choices", "is_unit", "base_factor",
           "conversion_factor", "convert", "factor", "tidy"]

# Seconds per unit. Integers stay integers so every ratio that existed before
# milliseconds is computed exactly as it was (frame hashes).
UNITS = {"milliseconds": 0.001, "seconds": 1, "minutes": 60, "hours": 3600, "days": 86400,
         "weeks": 7 * 86400, "months": 30 * 86400, "years": 365 * 86400}
LENGTH_UNITS = {"millimeters": 0.001, "centimeters": 0.01, "meters": 1, "kilometers": 1000}
MASS_UNITS = {"g": 0.001, "kg": 1, "t": 1000}
ENERGY_UNITS = {"Wh": 0.001, "kWh": 1, "MWh": 1000}
POWER_UNITS = {"W": 0.001, "kW": 1, "MW": 1000}
SPEED_UNITS = ("m/s", "m/min", "km/h")          # the drop-down; any "<length>/<time>" parses
TIME_ABBR = {"milliseconds": "ms", "seconds": "s", "minutes": "min", "hours": "h",
             "days": "day", "weeks": "week", "months": "month", "years": "year"}
LENGTH_ABBR = {"millimeters": "mm", "centimeters": "cm", "meters": "m", "kilometers": "km"}
SPEED_PATTERN = (f"^(base|({'|'.join(LENGTH_ABBR.values())})/"
                 f"({'|'.join(TIME_ABBR.values())}))$")
QUANTITIES = ("time", "length", "speed", "mass", "energy", "power", "money", "rate", "count",
              "ratio", "none")
UNIT_QUANTITIES = ("time", "length", "speed", "mass", "energy", "power", "rate")
_SCALES = {"length": LENGTH_UNITS, "mass": MASS_UNITS, "energy": ENERGY_UNITS,
           "power": POWER_UNITS}
_BASE = {"length": "meters", "mass": "kg", "energy": "kWh", "power": "kW"}
_FROM_LENGTH_ABBR = {v: k for k, v in LENGTH_ABBR.items()}
_FROM_TIME_ABBR = {v: k for k, v in TIME_ABBR.items()}


class ModelUnits(NamedTuple):
    """The model's own units (D160): `simulation.time_unit`, `length_unit`, `currency`."""
    time: str = "minutes"
    length: str = "meters"
    currency: str = "EUR"


def units_of(simulation) -> ModelUnits:
    """*simulation* (the layout's section, or None) as ModelUnits, with today's defaults."""
    sim = simulation if isinstance(simulation, dict) else {}
    return ModelUnits(str(sim.get("time_unit") or "minutes"),
                      str(sim.get("length_unit") or "meters"), str(sim.get("currency") or "EUR"))


def _speed(unit: str) -> tuple[str, str]:
    """"m/min" -> ("meters", "minutes"); ValueError when either half is unknown."""
    length, _, time = str(unit).partition("/")
    if length not in _FROM_LENGTH_ABBR or time not in _FROM_TIME_ABBR:
        raise ValueError(f"unknown speed unit: {unit}")
    return _FROM_LENGTH_ABBR[length], _FROM_TIME_ABBR[time]


def default_unit(quantity: str, units: ModelUnits = ModelUnits()) -> str | None:
    """The unit a field of *quantity* means when it names none (D159): the model's own
    time/length, or the base of a quantity the model does not set; None when unitless."""
    if quantity in ("time", "rate"):
        return units.time
    if quantity == "length":
        return units.length
    if quantity == "speed":
        return f"{LENGTH_ABBR[units.length]}/{TIME_ABBR[units.time]}"
    return _BASE.get(quantity)


def unit_choices(quantity: str, units: ModelUnits = ModelUnits()) -> tuple[str, ...]:
    """What a unit drop-down offers for *quantity*; () for a unitless one."""
    if quantity in ("time", "rate"):
        return tuple(UNITS)
    if quantity == "speed":
        own = default_unit("speed", units)
        return SPEED_UNITS + (() if own in SPEED_UNITS else (own,))
    return tuple(_SCALES.get(quantity, ()))


def is_unit(quantity: str, unit) -> bool:
    """True when *unit* is a valid unit of *quantity* ("base" and None always are)."""
    if unit in (None, "base"):
        return True
    if quantity in ("time", "rate"):
        return unit in UNITS
    if quantity == "speed":
        try:
            _speed(unit)
        except ValueError:
            return False
        return True
    return unit in _SCALES.get(quantity, {unit: 1})


def base_factor(quantity: str, unit, units: ModelUnits = ModelUnits()) -> float:
    """Multiply a value of *quantity* in *unit* by this to get the engine's base.

    None/""/"base" and a unitless quantity give exactly 1.0. A rate keeps the
    `1.0 / factor(...)` form every rate site used before, so their results are
    bit-identical; a speed is length x factor(model time, its time)."""
    if unit in (None, "", "base") or quantity not in UNIT_QUANTITIES:
        return 1.0
    if quantity == "time":
        return factor(unit, units.time)
    if quantity == "rate":
        return 1.0 / factor(unit, units.time)
    if quantity == "speed":
        length, time = _speed(unit)
        return LENGTH_UNITS[length] * factor(units.time, time)
    table = _SCALES[quantity]
    if unit not in table:
        raise ValueError(f"unknown {quantity} unit: {unit}")
    return table[unit] / table[_BASE[quantity]]


def conversion_factor(quantity: str, from_unit: str, to_unit: str) -> float:
    """Multiply a value in *from_unit* by this to express it in *to_unit* (D161)."""
    if from_unit == to_unit or quantity not in UNIT_QUANTITIES:
        return 1.0
    if quantity == "time":
        return factor(from_unit, to_unit)
    if quantity == "rate":
        return factor(to_unit, from_unit)        # 10 per hour = 10 * (60/3600) per minute
    if quantity == "speed":
        (l1, t1), (l2, t2) = _speed(from_unit), _speed(to_unit)
        return LENGTH_UNITS[l1] / LENGTH_UNITS[l2] * factor(t2, t1)
    table = _SCALES[quantity]
    return table[from_unit] / table[to_unit]


def tidy(value, original):
    """A converted number without float noise (#304; the editor path only, never
    a load): 15 significant digits, so 0.7 min -> ms -> min reads 0.7 again,
    and an int when *original* was one and the result is whole (1 min -> 60 s)."""
    if isinstance(value, float) and math.isfinite(value):
        value = float(f"{value:.15g}")
        if isinstance(original, int) and not isinstance(original, bool) and value.is_integer():
            return int(value)
    return value


def convert(value, quantity: str, from_unit, to_unit, units: ModelUnits = ModelUnits()):
    """*value* in *from_unit* expressed in *to_unit* (D161, tidied); "base"/None
    mean the model's unit."""
    def resolve(u):
        return default_unit(quantity, units) if u in (None, "", "base") else u
    return tidy(value * conversion_factor(quantity, resolve(from_unit), resolve(to_unit)), value)


def factor(unit: str, base: str) -> float:
    """Multiply a value expressed in *unit* by this to get it in *base*.

    `unit == "base"` (or `unit == base`) needs no conversion. Raises ValueError
    on an unknown unit name, checked at validation time too (`schema.layout`, #306) so a
    typo is a load-time problem, not a run-time one.
    """
    if unit == "base" or unit == base:
        return 1.0
    if unit not in UNITS:
        raise ValueError(f"unknown time unit: {unit}")
    if base not in UNITS:
        raise ValueError(f"unknown time unit: {base}")
    return UNITS[unit] / UNITS[base]


