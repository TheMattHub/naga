"""Battery arithmetic for electric fleets (automation sub-project 2, D164-D165).

Everything is closed form, in hours and kWh; callers convert to the model's
time unit. Charging power is flat up to the knee, then falls linearly to
`floor x power` at 100 %, so a full charge takes a finite time.
"""
from __future__ import annotations

import math

_BUSY_STATES = ("empty", "loaded", "handling")


def drain_kw(power: dict | None, state: str) -> float:
    """kW a vehicle draws in *state*, from its fleet's resource `power` map."""
    if not power or state == "charging":
        return 0.0
    key = "busy" if state in _BUSY_STATES else "idle"
    return float(power.get(key, 0.0))


def _g(s: float, knee: float, floor: float) -> float:
    return 1.0 - (1.0 - floor) * (s - knee) / (1.0 - knee)


def _tapered(knee: float, floor: float) -> bool:
    return knee < 1.0 and floor < 1.0


def power_at(s: float, power: float, knee: float = 0.8, floor: float = 0.1) -> float:
    if not _tapered(knee, floor) or s <= knee:
        return power
    return power * _g(min(s, 1.0), knee, floor)


def charge_hours(capacity: float, s0: float, s1: float, power: float,
                 knee: float = 0.8, floor: float = 0.1) -> float:
    """Hours to charge from SoC *s0* to *s1* (s0 <= s1 <= 1)."""
    if s1 <= s0:
        return 0.0
    if not _tapered(knee, floor):
        return capacity * (s1 - s0) / power
    hours = capacity * max(min(s1, knee) - s0, 0.0) / power
    a = max(s0, knee)
    if s1 > a:
        hours += (capacity * (1.0 - knee) / (power * (1.0 - floor))
                  * math.log(_g(a, knee, floor) / _g(s1, knee, floor)))
    return hours


def soc_after(capacity: float, s0: float, hours: float, power: float,
              knee: float = 0.8, floor: float = 0.1, cap: float = 1.0) -> float:
    """SoC after charging *hours* from *s0*, never above *cap*."""
    if hours <= 0:
        return s0
    if not _tapered(knee, floor):
        return min(s0 + power * hours / capacity, cap)
    s = s0
    if s < knee:
        to_knee = capacity * (knee - s) / power
        if hours <= to_knee:
            return min(s + power * hours / capacity, cap)
        hours -= to_knee
        s = knee
    g = _g(s, knee, floor) * math.exp(-hours * power * (1.0 - floor) / (capacity * (1.0 - knee)))
    return min(knee + (1.0 - g) * (1.0 - knee) / (1.0 - floor), cap)
