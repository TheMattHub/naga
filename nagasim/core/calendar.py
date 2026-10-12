"""Real calendar dates (PHASE4 T10, ARCHITECTURE D81).

A model's clock is a number in the base time unit and always starts at 0. This
module is the OPTIONAL bridge from that number to a real instant, and the ONE
place in the codebase that converts between the two:

    "simulation": {"time_unit": "minutes",
                   "calendar": {"start": "2026-01-05T00:00:00",
                                "timezone": "Europe/Rome",
                                "end": "2026-01-12T00:00:00"}}

With no `calendar` the model behaves exactly as before, pays nothing, and every
frame, hash and axis is unchanged. With one:

* `start` is the instant model time 0 stands for. Written WITHOUT an offset it
  is read in `timezone` -- "06:00 at the plant", which is what a planner means;
  written with one (`...+01:00`, `...Z`) the offset wins. `timezone` is an IANA
  name, UTC by default. Never local time: the same model file must run the same
  on a laptop in Rome and a CI runner in UTC.
* `end`, when given, is an alternative to `simulation.time_limit`: the run
  length is `to_model(end)`.

WALL CLOCK, NOT ELAPSED TIME. A weekday schedule (`weekly`) is declared in
LOCAL wall-clock terms: "Mon-Fri 06:00-14:00" starts at 06:00 local every
working day, including the two days a year when a local day is 23 or 25 hours
long. Conversions are therefore done per local date and then reduced to
absolute instants, which is exact across a DST transition: the shift still
starts at 06:00 local, and the day it sits in is simply shorter or longer in
model time. Elapsed-time arithmetic (`to_model`/`to_datetime`) is plain UTC
difference and never guesses.

COMPILE ONCE. `compile_weekly` turns a weekday grid plus its dated exceptions
into the ordinary `[duration, value]` points `core/schedules.py` already
evaluates, once per run, over the run horizon. The engine never does datetime
arithmetic at event time; a model without a calendar never reaches this module
at all.

Stdlib only (datetime + zoneinfo), no numpy, no pandas: a leaf module beside
`timeunits.py`, imported by `core/schedules.py`, `core/timeseries_inputs.py`,
`core/orders.py`, `core/schema/` and -- as the shared time map (D71) --
`product/mes/`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nagasim.core.problems import problem
from nagasim.core.timeunits import UNITS, factor

__all__ = ["UTC", "WEEKDAYS", "CalendarError", "Calendar", "TimeMap", "parse_iso", "format_iso",
           "model_calendar", "calendar_spec", "run_horizon", "compile_weekly", "is_weekly",
           "date_ticks", "DEFAULT_HORIZON_WEEKS", "validate_calendar_structured",
           "validate_weekly_schedule", "to_model_time"]

UTC = timezone.utc

# Monday first, as ISO 8601 numbers them; the key names a weekday grid uses.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# A weekday schedule needs a window to compile over. With no run horizon (a
# model that runs until it drains) this is how far ahead it is built; past it
# the last value holds, exactly as a non-repeating points schedule does.
DEFAULT_HORIZON_WEEKS = 52


class CalendarError(ValueError):
    """A malformed date, time band or timezone. Raised at build time;
    `validate_calendar_structured` reports the same faults as Problems long
    before a run reaches here."""


def parse_iso(stamp, tz=UTC) -> tuple[datetime, bool]:
    """(aware datetime, was_naive) for an ISO 8601 stamp.

    A stamp with no offset is read in *tz* -- UTC by default, which is what
    `product/mes/` has always done, and the model's own timezone when a
    calendar passes one.
    """
    if isinstance(stamp, datetime):
        if stamp.tzinfo is None:
            return stamp.replace(tzinfo=tz), True
        return stamp, False
    text = str(stamp or "").strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise CalendarError(f"'{stamp}' is not an ISO 8601 timestamp "
                            f"(write it as 2026-09-21T06:00:00Z)") from None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz), True
    return moment, False


def format_iso(moment: datetime) -> str:
    """UTC, seconds precision, trailing Z -- what every stamp this codebase
    writes machine-side looks like."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone(name) -> ZoneInfo | timezone:
    if not name or str(name).upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CalendarError(f"unknown timezone '{name}' (use an IANA name, "
                            f"e.g. Europe/Rome)") from None


@dataclass(frozen=True)
class Calendar:
    """The one bridge between model time and real instants.

    `epoch` is the aware instant model time 0 stands for, `unit` the model's
    base time unit, `tz` the wall clock weekday schedules and labels use.
    Comparisons are absolute (UTC); only the wall-clock helpers touch `tz`.
    """

    epoch: datetime
    unit: str = "minutes"
    tz: object = field(default=UTC)

    def __post_init__(self):
        # The epoch is stored in UTC. Adding a timedelta to an aware datetime
        # is WALL-CLOCK arithmetic in Python (the offset is kept, not
        # recomputed), so an epoch carrying a DST-aware zone would silently
        # drift by an hour half the year.
        object.__setattr__(self, "tz", self.tz if hasattr(self.tz, "utcoffset")
                           else _zone(self.tz))
        epoch = self.epoch if self.epoch.tzinfo is not None else self.epoch.replace(tzinfo=self.tz)
        object.__setattr__(self, "epoch", epoch.astimezone(UTC))

    # -- construction -------------------------------------------------------
    @classmethod
    def from_spec(cls, spec: dict) -> "Calendar":
        """The MES mapping's `{"epoch": ..., "unit": ...}` shape (D71). A naive
        epoch is UTC here: a mapping file has no timezone field."""
        spec = spec or {}
        unit = str(spec.get("unit") or "minutes")
        if unit not in UNITS:
            raise CalendarError(f"unknown model time unit '{unit}' "
                                f"(use {', '.join(UNITS)})")
        return cls(epoch=parse_iso(spec.get("epoch") or "1970-01-01T00:00:00Z")[0], unit=unit)

    # -- conversions --------------------------------------------------------
    def to_model(self, stamp) -> float:
        """An ISO stamp (or a datetime) as model time, negative before the
        epoch. A naive stamp is read in this calendar's own timezone.

        Both sides are converted to UTC first: subtracting two datetimes that
        share one tzinfo OBJECT is defined to be naive wall-clock arithmetic in
        Python, which would quietly lose the hour at a DST transition.
        """
        moment = parse_iso(stamp, self.tz)[0].astimezone(UTC)
        return (moment - self.epoch).total_seconds() / factor(self.unit, "seconds")

    def to_datetime(self, t: float) -> datetime:
        """Model time *t* as an aware datetime in this calendar's timezone."""
        return (self.epoch + timedelta(seconds=float(t) * factor(self.unit, "seconds"))
                ).astimezone(self.tz)

    def to_iso(self, t: float) -> str:
        """Model time *t* as a UTC ISO stamp -- the machine-side form."""
        return format_iso(self.epoch + timedelta(seconds=float(t) * factor(self.unit, "seconds")))

    def label(self, t: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
        """Model time *t* as a local wall-clock string, for a report, an axis
        or the playback clock."""
        return self.to_datetime(t).strftime(fmt)

    def span(self, unit: str = "minutes") -> float:
        """How many model time units one *unit* is -- `span("hours")` is 60 in
        a minutes-based model."""
        return factor(unit, self.unit)

    def local(self, when: _date, clock: _time = _time(0, 0)) -> datetime:
        """A local date + wall-clock time as an aware instant in this
        calendar's timezone. The DST-correct half of every weekday schedule."""
        return datetime.combine(when, clock).replace(tzinfo=self.tz)


# D71's name for the same object: `product/mes/` keeps calling it TimeMap, so
# there is exactly one implementation and the twin and the editor agree.
TimeMap = Calendar


# ---------------------------------------------------------------- the model's
def calendar_spec(layout: dict) -> dict:
    return (layout.get("simulation") or {}).get("calendar") or {}


def model_calendar(layout: dict) -> Calendar | None:
    """The model's calendar, or None when it declares none (the default).

    Raises CalendarError on a malformed start or an unknown timezone;
    `validate_calendar_structured` reports both as Problems first.
    """
    spec = calendar_spec(layout)
    start = spec.get("start")
    if not start:
        return None
    tz = _zone(spec.get("timezone"))
    unit = str((layout.get("simulation") or {}).get("time_unit") or "minutes")
    if unit not in UNITS:
        raise CalendarError(f"unknown model time unit '{unit}' (use {', '.join(UNITS)})")
    return Calendar(epoch=parse_iso(start, tz)[0], unit=unit, tz=tz)


def run_horizon(layout: dict, calendar: "Calendar | None" = None) -> float | None:
    """The model's run length: `simulation.time_limit`, or `calendar.end` read
    as a date when the model gives one instead. None means "until it drains"."""
    sim = layout.get("simulation") or {}
    limit = sim.get("time_limit")
    if limit:
        return float(limit)
    end = calendar_spec(layout).get("end")
    if not end:
        return None
    calendar = calendar or model_calendar(layout)
    if calendar is None:
        return None
    length = calendar.to_model(end)
    return length if length > 0 else None


def to_model_time(value, calendar: "Calendar | None", what: str = "time"):
    """A layout field that may be a number OR an ISO stamp, as model time.

    The single conversion point item 3 of T10 asks for: orders, due dates and
    the absolute warm-start fields all run through it, so the engine keeps
    seeing plain numbers.
    """
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if calendar is None:
        raise CalendarError(f"{what}: '{value}' is a date, but the model has no "
                            f"\"simulation\": {{\"calendar\": ...}}")
    return calendar.to_model(value)


# ---------------------------------------------------------------- weekday grid
def is_weekly(entry: dict) -> bool:
    """True for a schedule authored as a weekday grid rather than points."""
    return bool((entry or {}).get("weekly"))


def _clock(text, what: str) -> tuple[int, int]:
    """"06:00" (or "24:00", the end of the day) as (hour, minute)."""
    parts = str(text or "").strip().split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        raise CalendarError(f"{what}: '{text}' is not a wall-clock time like \"06:00\"") from None
    if not (0 <= minute < 60 and (0 <= hour < 24 or (hour == 24 and minute == 0))):
        raise CalendarError(f"{what}: '{text}' is not a wall-clock time like \"06:00\"")
    return hour, minute


def _day_value(text, what: str) -> _date:
    try:
        return _date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        raise CalendarError(f"{what}: '{text}' is not a date like \"2026-12-25\"") from None


def _bands(entry: dict) -> dict[str, list]:
    """The grid as {weekday: [(start, end, value)]}, validated and sorted."""
    weekly = entry.get("weekly") or {}
    if not isinstance(weekly, dict):
        raise CalendarError("\"weekly\" is an object keyed by weekday, e.g. "
                            "{\"mon\": [[\"06:00\", \"14:00\", 3]]}")
    out: dict[str, list] = {}
    for day, bands in weekly.items():
        key = str(day).strip().lower()[:3]
        if key not in WEEKDAYS:
            raise CalendarError(f"'{day}' is not a weekday (use {', '.join(WEEKDAYS)})")
        rows = []
        for band in bands or []:
            if not isinstance(band, (list, tuple)) or len(band) != 3:
                raise CalendarError(f"{key}: a band is [\"06:00\", \"14:00\", value]")
            start, end, value = band
            lo, hi = _clock(start, key), _clock(end, key)
            if hi <= lo:
                raise CalendarError(f"{key}: band {start}-{end} does not run forwards "
                                    f"(a band that crosses midnight is two bands)")
            rows.append((lo, hi, float(value)))
        rows.sort()
        for (_, prev_end, _v), (next_start, _e, _w) in zip(rows, rows[1:]):
            if next_start < prev_end:
                raise CalendarError(f"{key}: the time bands overlap")
        out[key] = rows
    return out


def _exception_value(entry: dict, when: _date):
    """The value a dated exception forces on *when*, or None when none does.
    Whole local days: a holiday or a shutdown range, last match wins."""
    found = None
    for item in entry.get("exceptions") or []:
        if not isinstance(item, dict):
            raise CalendarError("an exception is {\"start\": \"2026-12-24\", "
                                "\"end\": \"2026-12-26\", \"value\": 0}")
        start = _day_value(item.get("start"), "exception")
        end = _day_value(item.get("end"), "exception") if item.get("end") else start
        if start <= when <= end:
            found = float(item.get("value") or 0.0)
    return found


def compile_weekly(entry: dict, calendar: Calendar, horizon: float | None = None) -> list[list]:
    """A weekday grid as ordinary `[duration, value]` points, in base time
    units, starting at model time 0.

    Walks LOCAL dates from the calendar's start, so every band edge is a real
    wall-clock instant and a DST day is 23 or 25 hours long in model time (see
    the module docstring). Consecutive equal values collapse, so a Mon-Fri
    pattern costs two points per working day and none per weekend.
    """
    grid = _bands(entry)
    off = float(entry.get("off_value") or 0.0)
    limit = float(horizon) if horizon and horizon > 0 else (
        calendar.span("days") * 7 * DEFAULT_HORIZON_WEEKS)

    start_local = calendar.to_datetime(0.0)
    day = start_local.date()
    changes: list[tuple[float, float]] = []
    while True:
        day_start = calendar.local(day)
        if calendar.to_model(day_start) > limit:
            break
        forced = _exception_value(entry, day)
        if forced is not None:
            changes.append((calendar.to_model(day_start), forced))
        else:
            edges = [(day_start, off)]
            for (lo_h, lo_m), (hi_h, hi_m), value in grid.get(WEEKDAYS[day.weekday()], []):
                edges.append((calendar.local(day, _time(lo_h, lo_m)), value))
                end = (calendar.local(day + timedelta(days=1)) if hi_h == 24
                       else calendar.local(day, _time(hi_h, hi_m)))
                edges.append((end, off))
            for moment, value in edges:
                t = calendar.to_model(moment)
                if t <= limit:
                    changes.append((t, value))
        day += timedelta(days=1)

    # Fold to the value in force at t=0, then collapse repeats.
    points: list[list] = []
    current = off
    for t, value in changes:
        if t <= 0:
            current = value
            continue
        if value == current:
            continue
        points.append([t, current])       # a boundary AT t, value held until then
        current = value
    if not points:
        return [[limit if limit > 0 else 1.0, current]]
    durations = [[points[0][0], points[0][1]]]
    for (t_prev, _v), (t, value) in zip(points, points[1:]):
        durations.append([t - t_prev, value])
    tail = limit - points[-1][0]
    durations.append([tail if tail > 0 else 1.0, current])
    return [[d, v] for d, v in durations if d > 0]


# ---------------------------------------------------------------- date axes
# (step in days, strftime format) ladder for a chart's x axis, coarsest last.
_TICK_STEPS = ((1 / 24, "%H:%M"), (2 / 24, "%H:%M"), (6 / 24, "%d %b %H:%M"),
               (1, "%d %b"), (2, "%d %b"), (7, "%d %b"), (14, "%d %b"),
               (28, "%b %Y"), (91, "%b %Y"), (365, "%Y"))


def date_ticks(calendar: Calendar, tmin: float, tmax: float,
               count: int = 6) -> list[tuple[float, str]]:
    """(model time, label) pairs for a date x axis over [tmin, tmax].

    Picks the coarsest step from a fixed hour/day/week/month/year ladder that
    fits *count* ticks, then anchors on local midnight (or the hour) so labels
    land on round wall-clock instants rather than on the run's start offset.
    """
    day = calendar.span("days")
    span = max(float(tmax) - float(tmin), 0.0)
    if span <= 0 or not math.isfinite(span):
        return [(float(tmin), calendar.label(tmin))]
    step_days, fmt = _TICK_STEPS[-1]
    for candidate, candidate_fmt in _TICK_STEPS:
        if span / (candidate * day) <= count:
            step_days, fmt = candidate, candidate_fmt
            break
    step = step_days * day
    first_local = calendar.to_datetime(tmin)
    if step_days >= 1:
        anchor = calendar.local(first_local.date())
    else:
        anchor = first_local.replace(minute=0, second=0, microsecond=0)
    t0 = calendar.to_model(anchor)
    if t0 < tmin:
        t0 += step * math.ceil((tmin - t0) / step)
    ticks = []
    t = t0
    while t <= tmax + 1e-9 and len(ticks) <= count * 2:
        ticks.append((t, calendar.label(t, fmt)))
        t += step
    return ticks or [(float(tmin), calendar.label(tmin))]


# ---------------------------------------------------------------- validation
def validate_calendar_structured(layout: dict) -> tuple[list, "Calendar | None"]:
    """Check `simulation.calendar`. Returns (list[Problem], Calendar or None)."""
    problems: list = []
    spec = calendar_spec(layout)
    if not spec:
        return problems, None
    if not isinstance(spec, dict):
        return [problem("E_CALENDAR_START", "simulation.calendar: must be an object",
                        "/simulation/calendar",
                        hint="{\"start\": \"2026-01-05T06:00:00\", \"timezone\": \"Europe/Rome\"}")], None
    if not spec.get("start"):
        problems.append(problem(
            "E_CALENDAR_START", "simulation.calendar: no \"start\"", "/simulation/calendar/start",
            hint="the ISO 8601 instant model time 0 stands for, e.g. \"2026-01-05T06:00:00\""))
        return problems, None
    try:
        _zone(spec.get("timezone"))
    except CalendarError as exc:
        problems.append(problem("E_CALENDAR_TIMEZONE", f"simulation.calendar: {exc}",
                                "/simulation/calendar/timezone",
                                hint="an IANA name such as UTC, Europe/Rome or America/New_York"))
        return problems, None
    try:
        calendar = model_calendar(layout)
    except CalendarError as exc:
        problems.append(problem("E_CALENDAR_START", f"simulation.calendar: {exc}",
                                "/simulation/calendar/start",
                                hint="write it as 2026-01-05T06:00:00 or 2026-01-05T05:00:00Z"))
        return problems, None
    end = spec.get("end")
    if end:
        try:
            length = calendar.to_model(end)
        except CalendarError as exc:
            problems.append(problem("E_CALENDAR_DATE", f"simulation.calendar: {exc}",
                                    "/simulation/calendar/end",
                                    hint="the run's end instant, e.g. \"2026-01-12T00:00:00\""))
            length = 1.0
        if length <= 0:
            problems.append(problem(
                "E_CALENDAR_DATE", "simulation.calendar: \"end\" is not after \"start\"",
                "/simulation/calendar/end", hint="the run ends after it starts"))
    return problems, calendar


def validate_weekly_schedule(entry: dict, at: str, calendar: "Calendar | None",
                             horizon: float | None) -> list:
    """Check one weekday-grid schedule entry (called from
    `core/schedules.py`, which owns the rest of the section's checks)."""
    problems: list = []
    name = entry.get("name", "")
    if calendar is None:
        return [problem(
            "E_SCHEDULE_NO_CALENDAR",
            f"schedule '{name}': a weekday schedule needs a model calendar",
            f"{at}/weekly",
            hint="set \"simulation\": {\"calendar\": {\"start\": \"2026-01-05T00:00:00\", "
                 "\"timezone\": \"Europe/Rome\"}}")]
    try:
        _bands(entry)
    except CalendarError as exc:
        return [problem("E_SCHEDULE_WEEKLY", f"schedule '{name}': {exc}", f"{at}/weekly",
                        hint="{\"mon\": [[\"06:00\", \"14:00\", 3]], \"sat\": []}")]
    limit = horizon
    for index, item in enumerate(entry.get("exceptions") or []):
        where = f"{at}/exceptions/{index}"
        if not isinstance(item, dict) or not item.get("start"):
            problems.append(problem(
                "E_SCHEDULE_EXCEPTION", f"schedule '{name}': exception {index} has no \"start\"",
                where, hint="{\"start\": \"2026-12-24\", \"end\": \"2026-12-26\", \"value\": 0}"))
            continue
        try:
            start = _day_value(item.get("start"), "exception")
            end = _day_value(item.get("end"), "exception") if item.get("end") else start
        except CalendarError as exc:
            problems.append(problem("E_SCHEDULE_EXCEPTION", f"schedule '{name}': {exc}", where,
                                    hint="dates are ISO, e.g. \"2026-12-25\""))
            continue
        if end < start:
            problems.append(problem(
                "E_SCHEDULE_EXCEPTION", f"schedule '{name}': exception {index} ends before it starts",
                where, hint="\"end\" is the last day of the range, inclusive"))
            continue
        if limit and limit > 0:
            first, last = calendar.to_model(calendar.local(start)), calendar.to_model(
                calendar.local(end + timedelta(days=1)))
            if last <= 0 or first >= limit:
                problems.append(problem(
                    "W_SCHEDULE_EXCEPTION_UNUSED",
                    f"schedule '{name}': exception {index} ({start}) falls outside the "
                    f"run", where,
                    hint="the run covers "
                         f"{calendar.label(0, '%Y-%m-%d')} to {calendar.label(limit, '%Y-%m-%d')}"))
    return problems
