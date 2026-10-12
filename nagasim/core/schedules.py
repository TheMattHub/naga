"""Piecewise-constant schedules (ROADMAP 1.4).

One structure serves both uses: an "arrival" schedule whose values are arrival
RATES per time unit (CREATE's non-homogeneous Poisson mode) and a "capacity"
schedule whose values are integer unit counts (a resource's shift calendar).

    {"name": "day", "kind": "arrival", "repeat": true,
     "points": [[60, 2], [60, 8], [60, 0]]}

A point is `[duration, value]`: the value holds for `duration` time units, then
the next point takes over. With `repeat` the pattern cycles for ever; without
it the last value holds for ever once the pattern runs out.

An `offset` (PHASE4 T3) starts the pattern PARTWAY THROUGH: a schedule
evaluated at model time t reads its profile at `t + offset`, folded by the
cycle when the pattern repeats. A run warm-started three hours into the morning
shift sets `initial_state.schedule_offset` to those three hours, and the shift
ends at its real hour instead of a whole shift after the snapshot.

A model with a calendar (PHASE4 T10, `core/calendar.py`) may instead author a
schedule as a WEEKDAY GRID in local wall-clock time, with dated exceptions:

    {"name": "shift", "kind": "capacity", "off_value": 0,
     "weekly": {"mon": [["06:00", "14:00", 3]], "sat": [["06:00", "12:00", 1]]},
     "exceptions": [{"start": "2026-12-24", "end": "2026-12-26", "value": 0}]}

`build_schedule` compiles that into the points above ONCE per run, over the run
horizon, so the engine still evaluates a plain piecewise-constant function and
a model without a calendar pays nothing. A weekday schedule is absolute in
calendar terms, so `schedule_offset` does NOT apply to it (ARCHITECTURE D81).

Stdlib only: no numpy, no simpy, no distributions. The processes that act on a
schedule live in `resource_manager.py` (capacity) and `blocks/create.py`
(arrivals); this module is pure value semantics plus its validation.
"""
from __future__ import annotations

import bisect
import csv
import math

from nagasim.core import calendar as cal
from nagasim.core.problems import problem
from nagasim.core.timeunits import ModelUnits, base_factor

__all__ = ["Schedule", "SCHEDULE_KINDS", "build_schedule",
           "validate_schedules_structured", "read_schedule_csv"]

SCHEDULE_KINDS = ("arrival", "capacity")


class Schedule:
    """A named piecewise-constant function of time.

    `time_unit` (ROADMAP 1.5, default "base") converts the point DURATIONS into
    *base_unit*; for an "arrival" schedule the VALUES are a rate per
    `time_unit`, so they convert with the inverse factor (10 per hour in a
    minutes-base model becomes 1/6 per minute). A "capacity" schedule's values
    are unit counts and never convert.
    """

    def __init__(self, name: str, kind: str = "arrival", points=(), repeat: bool = False,
                 time_unit: str = "base", base_unit: str = "minutes", offset: float = 0.0):
        if kind not in SCHEDULE_KINDS:
            raise ValueError(f"Schedule '{name}': unknown kind '{kind}'")
        self.name = name
        self.kind = kind
        self.repeat = bool(repeat)
        self.time_unit = time_unit
        self.offset = float(offset or 0.0)      # PHASE4 T3: start mid-cycle
        dur_factor = base_factor("time", time_unit, ModelUnits(base_unit))
        self.durations = [float(d) * dur_factor for d, _ in points]
        rate_factor = 1.0 / dur_factor if kind == "arrival" else 1.0
        self.values = [float(v) * rate_factor for _, v in points]
        if not self.durations:
            raise ValueError(f"Schedule '{name}': no points")
        if any(d <= 0 for d in self.durations):
            raise ValueError(f"Schedule '{name}': every duration must be > 0")
        if any(v < 0 for v in self.values):
            raise ValueError(f"Schedule '{name}': every value must be >= 0")
        # Cumulative segment ends: bounds[i] is when point i stops applying.
        self.bounds = []
        total = 0.0
        for d in self.durations:
            total += d
            self.bounds.append(total)
        self.cycle_length = total
        self.max_value = max(self.values)

    def __repr__(self):
        return (f"<Schedule name='{self.name}', kind={self.kind}, "
                f"points={len(self.values)}, repeat={self.repeat}>")

    def _index_at(self, t: float) -> int:
        if self.repeat:
            t = math.fmod(t, self.cycle_length)
            if t < 0:
                t += self.cycle_length
        # First bound strictly above t; past the pattern the last value holds.
        return min(bisect.bisect_right(self.bounds, t), len(self.values) - 1)

    def value_at(self, t: float) -> float:
        return self.values[self._index_at(t + self.offset)]

    def next_change_after(self, t: float) -> float:
        """The next time strictly after *t* at which `value_at` may change, or
        inf once the value is fixed for ever.

        "May": a repeating pattern whose last and first values are equal still
        reports its wrap as a boundary. A caller that wakes there simply finds
        nothing to do.
        """
        shifted = t + self.offset
        if self.repeat:
            cycles, into = divmod(shifted, self.cycle_length)
            for bound in self.bounds:
                if bound > into:
                    return cycles * self.cycle_length + bound - self.offset
            # shifted sat exactly on the wrap
            return (cycles + 1) * self.cycle_length - self.offset
        for bound in self.bounds[:-1]:   # the final bound changes nothing: the last value holds
            if bound > shifted:
                return bound - self.offset
        return math.inf


def build_schedule(entry: dict, base_unit: str = "minutes", offset: float = 0.0,
                   calendar=None, horizon: float | None = None) -> Schedule:
    """One "schedules" entry as a `Schedule` -- the single place a section
    entry becomes a runnable object, whether it is authored as points or as a
    weekday grid (PHASE4 T10)."""
    kind = entry.get("kind", "arrival")
    if not cal.is_weekly(entry):
        return Schedule(entry.get("name", ""), kind, entry.get("points", []),
                        entry.get("repeat", False), time_unit=entry.get("time_unit", "base"),
                        base_unit=base_unit, offset=offset)
    if calendar is None:
        raise ValueError(f"Schedule '{entry.get('name', '')}': a weekday schedule needs "
                         f"simulation.calendar")
    points = cal.compile_weekly(entry, calendar, horizon)
    # The compiled durations are already in the base unit; only an arrival
    # schedule's VALUES still carry the entry's own rate unit.
    rate_scale = (base_factor("rate", entry.get("time_unit", "base") or "base",
                              ModelUnits(base_unit))
                  if kind == "arrival" else 1.0)
    return Schedule(entry.get("name", ""), kind,
                    [[d, v * rate_scale] for d, v in points], False,
                    time_unit="base", base_unit=base_unit, offset=0.0)


def validate_schedules_structured(layout: dict, calendar=None,
                                  horizon: float | None = None) -> tuple[list, dict]:
    """Check the layout's "schedules" section (ROADMAP 3.1 item 1).
    Returns (list[Problem], {name: kind})."""
    problems: list = []
    kinds: dict[str, str] = {}
    for index_s, entry in enumerate(layout.get("schedules", [])):
        at = f"/schedules/{index_s}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_SCHEDULE_NAME", "schedule: name must not be empty",
                                    f"{at}/name", hint="give every schedule a unique name"))
            continue
        if name in kinds:
            problems.append(problem("E_DUPLICATE_SCHEDULE", f"duplicate schedule name: {name}",
                                    f"{at}/name", hint="rename one of the two schedules"))
        kind = entry.get("kind", "arrival")
        if kind not in SCHEDULE_KINDS:
            problems.append(problem(
                "E_SCHEDULE_KIND",
                f"schedule '{name}': unknown kind '{kind}' "
                f"(expected one of {', '.join(SCHEDULE_KINDS)})",
                f"{at}/kind", hint=f"use one of: {', '.join(SCHEDULE_KINDS)}"))
            continue
        kinds[name] = kind

        if cal.is_weekly(entry):                       # PHASE4 T10 weekday grid
            problems.extend(cal.validate_weekly_schedule(entry, at, calendar, horizon))
            continue

        points = entry.get("points", [])
        if not points:
            problems.append(problem("E_SCHEDULE_POINTS", f"schedule '{name}': no points",
                                    f"{at}/points",
                                    hint="add at least one [duration, value] point"))
            continue
        for index, point in enumerate(points):
            where = f"{at}/points/{index}"
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                problems.append(problem(
                    "E_SCHEDULE_POINT", f"schedule '{name}': point {index} is not [duration, value]",
                    where, hint="each point is a two-element [duration, value] array"))
                continue
            duration, value = point
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                problems.append(problem(
                    "E_SCHEDULE_POINT", f"schedule '{name}': point {index} duration must be > 0",
                    f"{where}/0", hint="a point holds for a positive number of time units"))
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                problems.append(problem(
                    "E_SCHEDULE_POINT", f"schedule '{name}': point {index} value must be >= 0",
                    f"{where}/1", hint="an arrival rate or a capacity is never negative"))
            elif kind == "capacity" and float(value) != int(value):
                problems.append(problem(
                    "E_SCHEDULE_POINT",
                    f"schedule '{name}': point {index} capacity value must be a whole number",
                    f"{where}/1", hint="a capacity is a whole number of resource units"))
    return problems, kinds


def read_schedule_csv(path) -> list[float]:
    """Arrival times from the first numeric column of a CSV, sorted.

    Feeds the FixedSchedule distribution's `schedule` parameter; the values are
    stored in the model file, so an imported table travels with it.
    """
    times: list[float] = []
    column = None
    with open(path, "r", newline="") as handle:
        for row in csv.reader(handle):
            if column is None:
                column = _first_numeric_column(row)
                if column is None:
                    continue          # a header row (or blank): look at the next one
            if column < len(row):
                try:
                    times.append(float(row[column]))
                except ValueError:
                    continue          # a stray non-numeric cell (trailing header, footer)
    return sorted(times)


def _first_numeric_column(row) -> int | None:
    for index, cell in enumerate(row):
        try:
            float(cell)
        except ValueError:
            continue
        return index
    return None
