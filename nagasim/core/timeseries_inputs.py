"""Time-series INPUTS: the "timeseries_inputs" model section and the TS()
expression function (PHASE4 D2).

Not to be confused with `core/timeseries.py`, which SAMPLES model state into
the "timeseries" OUTPUT frame. This module is the other direction: a named
profile the model READS -- a PV generation curve, a site load, a tariff by
hour, a demand forecast.

Two authoring shapes, both round-tripping through `series_csv` /
`series_from_csv`:

    {"name": "pv", "start": 0, "step": 60, "values": [0, 0, 1.2, ...]}   # regular
    {"name": "pv", "times": [0, 60, 90], "values": [0, 1.2, 0.4]}        # irregular

plus, on either, `time_unit` (default "base"), `interpolation`
(`step` -- piecewise constant, the default -- or `linear`), `beyond` (what
happens past the last point: `hold`, `repeat` or `error`) and `period` (the
cycle length `repeat` uses; defaults to `step * len(values)` for the regular
shape and `times[-1] - times[0]` for the irregular one).

Below the first time the first value always applies: a profile that starts at
t=0 is the normal case and clamping is the only behaviour that cannot surprise.

`next_change(t)` is what makes `core/levels.py` exact: for a piecewise-constant
series it is the next instant the value can change, so a flow rate reading
TS() is re-evaluated exactly there and never in between. For a `linear` series
it returns the next KNOT -- the level engine then holds the rate constant
between knots, which is an approximation the `linear` choice opts into (see
ARCHITECTURE "Levels, flows and time series"); under `simulation.sd` a TS()
rate is also re-read at every grid point (DEVLOG #129).

Stdlib only (no numpy, no pandas): a leaf module beside `tables.py`, with its
own structured validation, so `core/schema/` just calls it.
"""
from __future__ import annotations

import bisect
import csv
import io
import math

from nagasim.core import calendar as cal
from nagasim.core.problems import problem
from nagasim.core.timeunits import ModelUnits, base_factor
from nagasim.core.utils import is_number as _is_number

__all__ = ["Series", "SeriesError", "INTERPOLATIONS", "BEYOND_MODES", "build_series",
           "build_series_table", "is_dated", "series_csv", "series_from_csv",
           "validate_timeseries_inputs_structured"]

INTERPOLATIONS = ("step", "linear")
BEYOND_MODES = ("hold", "repeat", "error")


class SeriesError(ValueError):
    """A malformed series, or a TS() read past the end of an `error` series.
    Raised at run time; the expression evaluator turns it into an
    ExpressionError naming the block."""


class Series:
    """A built time-series input: immutable, shareable, one per section entry.

    `times` are already in the model's base time unit, strictly increasing.
    """

    __slots__ = ("name", "times", "values", "interpolation", "beyond", "period", "offset")

    def __init__(self, name, times, values, interpolation="step", beyond="hold", period=None,
                 offset=0.0):
        self.name = name
        self.times = tuple(float(t) for t in times)
        self.values = tuple(float(v) for v in values)
        self.interpolation = interpolation
        self.beyond = beyond
        span = self.times[-1] - self.times[0]
        self.period = float(period) if period else (span if span > 0 else 0.0)
        # PHASE4 T3: a warm start resumes a REPEATING profile mid-cycle -- a
        # price curve continues mid-day. A series that holds or errors past its
        # end has no cycle to be partway through, so the offset does not apply.
        self.offset = float(offset or 0.0) if beyond == "repeat" else 0.0

    # -- reading ------------------------------------------------------------
    def _fold(self, t: float) -> float:
        """*t* mapped into the authored range, per the `beyond` rule."""
        last = self.times[-1]
        if t <= last:
            return t
        if self.beyond == "repeat" and self.period > 0:
            return self.times[0] + math.fmod(t - self.times[0], self.period)
        if self.beyond == "error":
            raise SeriesError(f"series '{self.name}': no value at t={t:g} "
                              f"(the series ends at t={last:g})")
        return last                                    # "hold"

    def value_at(self, t: float) -> float:
        t = self._fold(float(t) + self.offset)
        times = self.times
        if t <= times[0]:
            return self.values[0]
        index = bisect.bisect_right(times, t) - 1
        if self.interpolation == "step" or index >= len(times) - 1:
            return self.values[min(index, len(times) - 1)]
        span = times[index + 1] - times[index]
        weight = (t - times[index]) / span
        return self.values[index] * (1.0 - weight) + self.values[index + 1] * weight

    def next_change(self, t: float):
        """The next instant strictly after *t* at which this series' value can
        change, or None when it never changes again (`hold`/`error` past the
        end). The instant the level engine schedules (PHASE4 D1)."""
        times, n = self.times, len(self.times)
        shifted = t + self.offset
        if self.beyond == "repeat" and self.period > 0:
            base = times[0]
            cycles = math.floor((shifted - base) / self.period)
            local = shifted - cycles * self.period
            index = bisect.bisect_right(times, local)
            if index < n:
                return cycles * self.period + times[index] - self.offset
            # the wrap point
            return cycles * self.period + base + self.period - self.offset
        index = bisect.bisect_right(times, shifted)
        return times[index] - self.offset if index < n else None

    def __repr__(self):
        return f"<Series {self.name!r} points={len(self.times)} {self.interpolation}>"


# ---------------------------------------------------------------- building
def _mode(entry: dict, key: str, allowed, default: str) -> str:
    value = entry.get(key) or default
    if value not in allowed:
        raise SeriesError(f"series '{entry.get('name', '')}': unknown {key} '{value}'")
    return value


def is_dated(entry: dict) -> bool:
    """True for a series authored against the real calendar (PHASE4 T10):
    `timestamps` instead of `times`, or an ISO `start` for the regular shape."""
    entry = entry or {}
    return bool(entry.get("timestamps")) or isinstance(entry.get("start"), str)


def series_times(entry: dict) -> list[float]:
    """The entry's sample times in ITS OWN time unit: `times` as authored, or
    `start + i * step` for the regular shape. A dated series has no such
    numbers until a calendar converts it (see `build_series`)."""
    if entry.get("times") is not None:
        return [float(t) for t in entry["times"]]
    step = float(entry.get("step") or 0.0)
    start = entry.get("start")
    start = 0.0 if isinstance(start, str) or start is None else float(start)
    return [start + i * step for i in range(len(entry.get("values") or []))]


def dated_times(entry: dict, calendar, base_unit: str = "minutes") -> list[float]:
    """A dated series' sample times as MODEL times, converted once here."""
    name = str(entry.get("name", ""))
    if calendar is None:
        raise SeriesError(f"series '{name}': real timestamps need a model calendar "
                          f"(\"simulation\": {{\"calendar\": {{\"start\": ...}}}})")
    try:
        if entry.get("timestamps"):
            return [calendar.to_model(s) for s in entry["timestamps"]]
        step = float(entry.get("step") or 0.0) * base_factor(
            "time", entry.get("time_unit", "base") or "base", ModelUnits(base_unit))
        first = calendar.to_model(entry.get("start"))
    except cal.CalendarError as exc:
        raise SeriesError(f"series '{name}': {exc}") from None
    return [first + i * step for i in range(len(entry.get("values") or []))]


def _calendar_period(entry: dict, calendar, base_unit: str):
    """`"period": "daily" | "weekly" | "yearly"` in real calendar terms."""
    period = entry.get("period")
    if not isinstance(period, str):
        return None
    name = str(entry.get("name", ""))
    if calendar is None:
        raise SeriesError(f"series '{name}': a \"{period}\" period needs a model calendar")
    days = {"daily": 1, "weekly": 7, "yearly": 365}.get(period.strip().lower())
    if days is None:
        raise SeriesError(f"series '{name}': unknown period '{period}' "
                          f"(use daily, weekly, yearly or a number)")
    return days * calendar.span("days")


def build_series(entry: dict, base_unit: str = "minutes", offset: float = 0.0,
                 calendar=None) -> Series:
    """One section entry as a `Series`. Raises SeriesError on anything
    malformed -- `validate_timeseries_inputs_structured` reports the same
    faults as Problems long before a run reaches here."""
    name = str(entry.get("name", ""))
    values = entry.get("values") or []
    if not values:
        raise SeriesError(f"series '{name}': no values")
    dated = is_dated(entry)
    if not dated and entry.get("times") is None and not entry.get("step"):
        raise SeriesError(f"series '{name}': give either \"times\" or a \"step\"")
    # A dated series is converted to model time here, once; everything below
    # then works on plain numbers exactly as it always has.
    times = dated_times(entry, calendar, base_unit) if dated else series_times(entry)
    scale = 1.0 if dated else base_factor("time", entry.get("time_unit", "base") or "base",
                                          ModelUnits(base_unit))
    if len(times) != len(values):
        raise SeriesError(f"series '{name}': {len(times)} times but {len(values)} values")
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            raise SeriesError(f"series '{name}': times must increase "
                              f"({times[i - 1]:g} then {times[i]:g})")
    unit_scale = base_factor("time", entry.get("time_unit", "base") or "base",
                             ModelUnits(base_unit))
    period = _calendar_period(entry, calendar, base_unit)
    if period is None:
        authored = entry.get("period")
        if authored is None and entry.get("times") is None and not entry.get("timestamps"):
            authored = float(entry.get("step") or 0.0) * len(values)  # 24 hourly values = one day
        period = None if authored is None else float(authored) * unit_scale
    return Series(name, [t * scale for t in times],
                  [_number(name, v) for v in values],
                  _mode(entry, "interpolation", INTERPOLATIONS, "step"),
                  _mode(entry, "beyond", BEYOND_MODES, "hold"),
                  period, offset)


def _number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SeriesError(f"series '{name}': value {value!r} is not a number")
    return float(value)


def build_series_table(section, base_unit: str = "minutes", offset: float = 0.0,
                       calendar=None) -> dict[str, Series]:
    """{name: Series} for the layout's "timeseries_inputs" section, built once
    per run. *offset* is the warm start's `schedule_offset` (PHASE4 T3);
    *calendar* converts a dated series to model time (PHASE4 T10)."""
    return {str(entry.get("name", "")): build_series(entry, base_unit, offset, calendar)
            for entry in section or []}


# ---------------------------------------------------------------- validation
def _validate_dated(entry: dict, at: str, name: str, calendar, horizon) -> list:
    """The dated shape (PHASE4 T10): ISO `timestamps`, or an ISO `start` with a
    `step`. Converted once here so the errors are Problems, not exceptions."""
    problems: list = []
    if calendar is None:
        return [problem(
            "E_SERIES_NO_CALENDAR",
            f"time series '{name}': real timestamps need a model calendar", at,
            hint="set \"simulation\": {\"calendar\": {\"start\": \"2026-01-05T00:00:00\", "
                 "\"timezone\": \"Europe/Rome\"}}, or use plain numeric \"times\"")]
    timestamps = entry.get("timestamps")
    if timestamps is not None and not isinstance(timestamps, list):
        return [problem("E_SERIES_SHAPE",
                        f"time series '{name}': \"timestamps\" must be a list of ISO stamps",
                        f"{at}/timestamps", hint="[\"2026-01-05T00:00:00\", ...]")]
    if timestamps is not None and entry.get("times") is not None:
        return [problem("E_SERIES_SHAPE",
                        f"time series '{name}': \"times\" and \"timestamps\" are exclusive", at,
                        hint="numeric model times, or real timestamps -- not both")]
    if timestamps is None and not entry.get("step"):
        return [problem("E_SERIES_SHAPE",
                        f"time series '{name}': a dated \"start\" needs a \"step\"", at,
                        hint="the spacing between two samples, in \"time_unit\"")]
    if timestamps is not None and len(timestamps) != len(entry.get("values") or []):
        return [problem("E_SERIES_SHAPE",
                        f"time series '{name}': {len(timestamps)} timestamps but "
                        f"{len(entry.get('values') or [])} values", at,
                        hint="one timestamp per value")]
    try:
        times = dated_times(entry, calendar)
    except SeriesError as exc:
        return [problem("E_CALENDAR_DATE", str(exc), at,
                        hint="write each stamp as 2026-01-05T06:00:00")]
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            problems.append(problem(
                "E_SERIES_TIMES", f"time series '{name}': timestamps must increase",
                f"{at}/timestamps/{i}", hint="sort the samples; duplicates are not allowed"))
            break
    if times and times[0] < 0:
        problems.append(problem(
            "W_SERIES_BEFORE_START",
            f"time series '{name}': starts before the model's calendar start; "
            f"the first value applies until the run begins", at,
            hint=f"the run starts at {calendar.label(0)}"))
    if times and horizon and horizon > 0 and times[0] > horizon:
        problems.append(problem(
            "W_SERIES_BEFORE_START",
            f"time series '{name}': every sample falls after the end of the run", at,
            hint=f"the run ends at {calendar.label(horizon)}"))
    if isinstance(entry.get("period"), str):
        try:
            _calendar_period(entry, calendar, "minutes")
        except SeriesError as exc:
            problems.append(problem("E_SERIES_MODE", str(exc), f"{at}/period",
                                    hint="use daily, weekly, yearly or a number"))
    return problems


def validate_timeseries_inputs_structured(layout: dict, calendar=None,
                                          horizon: float | None = None) -> tuple[list, set]:
    """Check the layout's "timeseries_inputs" section. Returns
    (list[Problem], {declared names}); the names are what `core/schema/`
    checks a literal `TS("name")` call against."""
    problems: list = []
    names: set = set()
    for index, entry in enumerate(layout.get("timeseries_inputs", []) or []):
        at = f"/timeseries_inputs/{index}"
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_SERIES_NAME", "time series: name must not be empty",
                                    f"{at}/name", hint="give every time series a unique name"))
            continue
        if name in names:
            problems.append(problem("E_SERIES_NAME", f"duplicate time series name: {name}",
                                    f"{at}/name", hint="rename one of the two series"))
        names.add(name)

        values = entry.get("values")
        if not isinstance(values, list) or not values:
            problems.append(problem("E_SERIES_SHAPE",
                                    f"time series '{name}': \"values\" must be a non-empty list",
                                    f"{at}/values",
                                    hint="list one number per sample, e.g. [0, 1.2, 3.4]"))
            continue
        bad = next((v for v in values if not _is_number(v)), None)
        if bad is not None:
            problems.append(problem("E_SERIES_SHAPE",
                                    f"time series '{name}': value {bad!r} is not a number",
                                    f"{at}/values", hint="every sample of a series is a number"))
            continue

        if is_dated(entry):                            # PHASE4 T10 dated series
            problems.extend(_validate_dated(entry, at, name, calendar, horizon))
            continue

        times, step = entry.get("times"), entry.get("step")
        if times is None and not step:
            problems.append(problem(
                "E_SERIES_SHAPE", f"time series '{name}': give either \"times\" or a \"step\"",
                at, hint="a regular profile is \"start\" + \"step\" + \"values\"; an irregular "
                        "one is \"times\" + \"values\""))
            continue
        if times is not None and step:
            problems.append(problem(
                "E_SERIES_SHAPE", f"time series '{name}': \"times\" and \"step\" are exclusive",
                at, hint="drop \"step\" for an irregular series, or \"times\" for a regular one"))
            continue
        if times is not None and (not isinstance(times, list)
                                  or not all(_is_number(t) for t in times)):
            problems.append(problem("E_SERIES_SHAPE",
                                    f"time series '{name}': \"times\" must be a list of numbers",
                                    f"{at}/times", hint="one time per value, increasing"))
            continue
        if times is not None and len(times) != len(values):
            problems.append(problem(
                "E_SERIES_SHAPE",
                f"time series '{name}': {len(times)} times but {len(values)} values",
                at, hint="\"times\" has exactly one entry per value"))
            continue
        if step and not _is_number(step):
            problems.append(problem("E_SERIES_SHAPE",
                                    f"time series '{name}': \"step\" must be a number",
                                    f"{at}/step", hint="the spacing between two samples"))
            continue
        if step and float(step) <= 0:
            problems.append(problem("E_SERIES_TIMES",
                                    f"time series '{name}': \"step\" must be positive",
                                    f"{at}/step", hint="samples run forwards in time"))

        authored = series_times(entry)
        for i in range(1, len(authored)):
            if authored[i] <= authored[i - 1]:
                problems.append(problem(
                    "E_SERIES_TIMES",
                    f"time series '{name}': times must increase "
                    f"({authored[i - 1]:g} then {authored[i]:g})",
                    f"{at}/times/{i}", hint="sort the samples by time; duplicates are not allowed"))
                break

        for key, allowed in (("interpolation", INTERPOLATIONS), ("beyond", BEYOND_MODES)):
            value = entry.get(key)
            if value is not None and value not in allowed:
                problems.append(problem(
                    "E_SERIES_MODE", f"time series '{name}': unknown {key} '{value}'",
                    f"{at}/{key}", hint=f"use one of: {', '.join(allowed)}"))
        period = entry.get("period")
        if isinstance(period, str):
            # PHASE4 T10: a calendar cycle -- daily/weekly/yearly -- instead of a number.
            try:
                _calendar_period(entry, calendar, "minutes")
            except SeriesError as exc:
                problems.append(problem("E_SERIES_MODE", str(exc), f"{at}/period",
                                        hint="use daily, weekly, yearly or a number"))
        elif period is not None and not _is_number(period):
            problems.append(problem("E_SERIES_SHAPE",
                                    f"time series '{name}': \"period\" must be a number",
                                    f"{at}/period",
                                    hint="the cycle length \"beyond\": \"repeat\" wraps on"))
    return problems, names


# ---------------------------------------------------------------- CSV
def series_csv(entry: dict) -> str:
    """One series as `time,value` CSV, one row per sample. `series_from_csv`
    reads back exactly this -- as the irregular (`times`/`timestamps`) shape,
    which is the same series either way."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    dated = is_dated(entry)
    writer.writerow(["timestamp" if dated else "time", "value"])
    times = entry.get("timestamps") if dated else series_times(entry)
    writer.writerows(zip(times or [], entry.get("values") or []))
    return out.getvalue()


def series_from_csv(text: str, name: str, calendar=None, **fields) -> dict:
    """A section entry from `time,value` CSV text. Extra *fields*
    (time_unit, interpolation, beyond, period, description) are copied onto the
    entry, so an import keeps the series' settings.

    A first column that does not read as a number is taken to be REAL
    TIMESTAMPS (PHASE4 T10) and imported as `timestamps`: that is what a CSV
    exported from a plant historian or a tariff sheet actually looks like.
    """
    rows = [row for row in csv.reader(io.StringIO(text)) if any(str(c).strip() for c in row)]
    if not rows:
        raise SeriesError(f"series '{name}': the CSV is empty")
    if len(rows[0]) < 2:
        raise SeriesError(f"series '{name}': the CSV needs two columns, time and value")
    body = rows[1:] if not _is_csv_number(rows[0][0]) else rows
    if not body:
        raise SeriesError(f"series '{name}': the CSV has no data rows")
    dated = not _is_csv_number(body[0][0])
    entry = {"name": name, "values": [_cell(name, row[1]) for row in body]}
    if dated:
        entry["timestamps"] = [str(row[0]).strip() for row in body]
        entry.pop("time_unit", None)
    else:
        entry["times"] = [_cell(name, row[0]) for row in body]
    entry.update({k: v for k, v in fields.items() if v is not None})
    if dated:
        entry.pop("time_unit", None)      # timestamps carry their own unit
    build_series(entry, calendar=calendar)   # refuse a ragged or non-increasing import here
    return entry


def _is_csv_number(cell) -> bool:
    try:
        float(str(cell).strip())
    except ValueError:
        return False
    return True


def _cell(name: str, cell) -> float:
    try:
        return float(str(cell).strip())
    except (ValueError, IndexError):
        raise SeriesError(f"series '{name}': {cell!r} is not a number") from None
