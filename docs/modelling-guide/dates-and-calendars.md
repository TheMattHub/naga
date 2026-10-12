# Dates and calendars

A model's clock is a plain number in the base time unit and always starts at 0. That is the right
choice for an abstract study: "minute 4320" needs no timezone and no leap year.

It is the wrong choice when the study is about real weeks. A shift starts at 06:00 every Monday,
a plant shuts for Christmas, a tariff is dated, and a client report should say "Wednesday 10:00",
not "minute 4320". For that, give the model a **calendar**.

A calendar is **optional**. Without one nothing changes: the same numbers, the same results, the
same exports. With one, the numbers stay exactly as they are -- the engine never learns about
dates -- and everything that *shows* a time shows a real instant.

## Setting one

**Run > Run experiment...**, tick **Model runs on real dates**, and pick a start instant and a
timezone. In the model file:

```jsonc
"simulation": {
  "time_unit": "minutes",
  "calendar": {
    "start": "2026-01-05T08:00:00",   // the real instant model time 0 stands for
    "timezone": "Europe/Rome",        // IANA name; UTC by default
    "end": "2026-01-12T08:00:00"      // optional: a run END DATE instead of a length
  }
}
```

`start` written **without** an offset is read in `timezone` -- "08:00 at the plant", which is what
a planner means. Written with one (`2026-01-05T07:00:00Z`, `...+01:00`) the offset wins. It is
never read in the machine's local time, so the same file runs identically on a laptop in Rome and
on a build server in UTC.

`end`, when you give it, replaces `simulation.time_limit`: the run length becomes the distance
from `start` to `end`.

On the command line:

```console
naga run model.json --start 2026-01-05T08:00:00 --timezone Europe/Rome --until 2026-01-12T08:00:00
```

## Weekday schedules

With a calendar, a capacity or arrival schedule can be written the way a roster is written: days
of the week and wall-clock bands, instead of a list of durations.

```jsonc
{
  "name": "agent_shifts", "kind": "capacity", "time_unit": "hours",
  "off_value": 0,
  "weekly": {
    "mon": [["08:00", "12:00", 4], ["12:00", "16:00", 5]],
    "tue": [["08:00", "16:00", 4]],
    "sat": [["08:00", "12:00", 2]]
  },
  "exceptions": [
    {"start": "2026-12-24", "end": "2026-12-26", "value": 0, "description": "Christmas"}
  ]
}
```

- A day with no entry, and any hour outside every band, takes `off_value` (0 by default).
- Bands run forwards within one day; a night shift is two bands (`22:00`-`24:00` and
  `00:00`-`06:00` the next day).
- `exceptions` override whole **local days**, `end` inclusive, last match wins -- holidays and
  shutdowns. Give `value` to run reduced rather than closed.
- For an `"arrival"` schedule the band value is a rate per the entry's `time_unit`, exactly as a
  points schedule's is.
- `repeat` does not apply: a weekday grid repeats by the calendar.

### Wall clock, not elapsed time

A weekday schedule means **local wall-clock time**. A 06:00 shift starts at 06:00 local on both
sides of a daylight-saving change -- which is what a plant means, and is why the transition day is
simply 23 or 25 hours long in model time. Elapsed-time arithmetic (a duration, a service time, the
run length) is plain absolute time and never shifts.

### It costs nothing at run time

A weekday grid is compiled **once per run**, over the run horizon, into the same
piecewise-constant `[duration, value]` points the engine has always evaluated. No datetime
arithmetic ever happens during a run, and a model without a calendar never touches any of this.

A run with no horizon at all compiles 52 weeks ahead; past that the last value holds.

!!! note "Precedence over `schedule_offset`"
    A warm start's `initial_state.schedule_offset` says how far into its own cycle a *points*
    schedule already is. A weekday schedule is absolute in calendar terms, so the offset does not
    apply to it -- move the calendar `start` instead. Points schedules and repeating time series
    keep honouring the offset exactly as before.

## Dated time series

A `timeseries_inputs` entry can carry real instants instead of numbers:

```jsonc
{"name": "tariff", "timestamps": ["2026-01-05T00:00:00", "2026-01-05T08:00:00"],
 "values": [0.15, 0.31], "beyond": "repeat", "period": "daily"}
```

- `timestamps` replaces `times` (irregular shape); an ISO `start` with a `step` replaces a numeric
  `start` (regular shape).
- `period` may be `"daily"`, `"weekly"` or `"yearly"` instead of a number.
- Everything is converted to model time **once**, when the model is built.

## Dated orders, due dates and warm start

Anywhere an absolute model time is accepted, a calendar model also accepts an ISO timestamp:

```jsonc
"orders": [{"id": "WO-1001", "release": "2026-01-05T08:00:00",
            "due": "2026-01-06T14:30:00", "quantity": 2}],
"initial_state": {"resources": {"Scanner": {"down_until": "2026-01-05T09:30:00"}}}
```

Durations (`age`, `remaining`, a service time) stay numbers: they are lengths, not instants.

## What you get back

| Where | With a calendar |
|---|---|
| Report header | a **Calendar** row: the run's real window and its timezone |
| Report and results charts | date axes -- hours, days, weeks, months or years, picked from the span |
| Playback clock | `Mon 05 Jan 08:30` instead of `t = 30.0` |
| Excel export | a real datetime column beside every time column, with a date format |
| CSV and JSON export | the same wall-clock column |
| MES twin | unchanged: a sync sets the model start to the snapshot instant |

The result **frames** themselves stay numeric, so a golden hash, a KPI and a scenario comparison
are identical whether or not the model is dated. The wall-clock columns are added on the way out.

## Validation

| Code | Meaning |
|---|---|
| `E_CALENDAR_START` | no start instant, or it is not ISO 8601 |
| `E_CALENDAR_TIMEZONE` | the timezone is not an IANA name |
| `E_CALENDAR_DATE` | a field written as a date is not an ISO date or timestamp |
| `E_SCHEDULE_NO_CALENDAR` | a weekday schedule, but the model has no calendar |
| `E_SCHEDULE_WEEKLY` | unknown day, malformed band, or overlapping bands |
| `E_SCHEDULE_EXCEPTION` | an exception has no start date, or an inverted range |
| `W_SCHEDULE_EXCEPTION_UNUSED` | an exception falls entirely outside the run |
| `E_SERIES_NO_CALENDAR` | a dated series, but the model has no calendar |
| `W_SERIES_BEFORE_START` | a dated series starts before, or ends after, the run window |

## Worked examples

- the bundled model `call_centre_day` -- weekday opening hours, a Saturday morning
  and a dated holiday.
- the bundled model `biogas_plant_library` -- a maize
  campaign in real autumn weeks, dated spreading windows and a weekly power price.
- the bundled model `energy_site_library` -- a dated week
  with daily profiles.
