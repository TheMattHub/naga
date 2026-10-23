"""Event log to model (`naga discover`, D546-D559).

A plant or an office already records what happened: which case went through which activity,
when, and by whom. `discover` reads that event log (CSV, or XES) and drafts a model from it:
one CREATE fitted to the case arrivals, one block per activity with its fitted time, routing
from the observed transitions, resources from the log's resource column. It also returns a
report of what was inferred, what was assumed and what was dropped. The draft is a first
draft for a modeller; the report says where it is weak.

    layout, report = discover("orders.csv")
    events = read_log("orders.xes")            # one Event per row
    log_kpis(events)                           # the log's own throughput, cycle and wait
    log_from_trace(recorder)                   # a run's trace as an event log

SECURITY (D554, D555). A log is untrusted text. Size, row and name-count ceilings are checked
before anything is built. The readers are not streaming: a log is read whole into memory, and
the ceilings are what bound it (a log at the ceilings peaked at about 300 MB, measured; see
D554). A name from the log is folded to `[A-Za-z0-9_]` before it names a block
or a resource, and nothing from the log becomes an expression or a path. An XES file with a
DOCTYPE or ENTITY declaration is refused before parsing (the Expat bundled with Python may be
older than the one that resists entity expansion), and the parser refuses one too. This module
reads the one path it is given; the agent tool confines that path to its workspace.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.parsers import expat

import numpy as np

__all__ = ["Event", "LogError", "read_log", "log_kpis", "log_from_trace",
           "discover", "check_log", "report_text", "check_text", "sample_path", "main",
           "MAX_BYTES", "MAX_EVENTS", "MAX_ACTIVITIES", "MAX_RESOURCES", "MAX_CELL"]

# D554: ceilings, checked before anything is built. Past one the log is refused, never cut:
# half a log would bias every number drawn from it. The log is held in memory (a few copies
# of the file, then an object per event), so these are memory ceilings: chosen from a measured
# peak (the spec, D554), to stay well under 500 MB on a path an agent can trigger.
MAX_BYTES = 50_000_000
MAX_EVENTS = 200_000
MAX_ACTIVITIES = 200
MAX_RESOURCES = 1000
MAX_CELL = 1000             # characters in one cell; a longer cell drops its row
MAX_VARIANTS = 20           # D550
FIT_SAMPLE = 5000           # ponytail: values fitted per sample, raise when fit time allows
# The family's parameters come from at most this many values (a fixed-seed sample past it):
# the mean of a time with a CV of 1 is then within about 0.45% (one sd), a third of the 1.4%
# that a 5000 sample cost (#562), and an optimiser-fitted family (Weibull) refits in seconds.
REFIT_SAMPLE = 50_000
_ROUTINGS = ("auto", "markov", "variants")

# D546: the usual header names of each column, compared without case.
COLUMN_NAMES = {
    "case": ("case", "case_id", "caseid", "case id", "case:concept:name"),
    "activity": ("activity", "activity_name", "activity name", "concept:name", "task"),
    "timestamp": ("timestamp", "time:timestamp", "time", "event_time"),
    "lifecycle": ("lifecycle", "lifecycle:transition", "transition", "event_type"),
    "resource": ("resource", "org:resource", "user", "machine"),
    "start": ("start", "start_time", "start_timestamp", "started"),
    "complete": ("complete", "end", "end_time", "complete_time", "complete_timestamp",
                 "completed"),
}
# XES lifecycle (IEEE 1849): schedule/assign mark when the work became ready. A missing
# transition is "complete", as XES's own default is. suspend/resume bracket a pause in the
# work, taken out of its service time (D631). An abort or a withdraw ends its instance,
# which is dropped and counted (#641: left open, it took the next instance's events).
_LIFECYCLE = {"schedule": "ready", "assign": "ready", "start": "start", "complete": "complete",
              "suspend": "suspend", "resume": "resume", "ate_abort": "abort",
              "pi_abort": "abort", "abort": "abort", "withdraw": "withdraw", "": "complete"}
# suspend and resume share a rank: at one instant the file's order says which came first
# (#642: either fixed order lost a pause when a resume and a re-suspend shared a second).
_RANK = {"ready": 0, "start": 1, "suspend": 2, "resume": 2, "abort": 3, "withdraw": 3,
         "complete": 4}
_DOCTYPE_RE = re.compile(r"<!\s*(doctype|entity)\b", re.IGNORECASE)   # as sdimport's
_TIME_UNITS = ("seconds", "minutes", "hours", "days")
_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


class LogError(ValueError):
    """A log refused outright: over a ceiling, malformed, or with nothing to model."""


@dataclass(frozen=True, slots=True)
class Event:
    case: str
    activity: str
    time: float             # model time units since the log's first instant
    lifecycle: str          # "ready", "start", "suspend", "resume", "abort", "withdraw"
                            # or "complete"
    resource: str = ""


# ============================================================ reading

class _Reader:
    """Turns raw cells into Events and counts what it drops, by reason."""

    def __init__(self):
        self.raw: list[tuple] = []          # (case, activity, seconds or number, lifecycle, res)
        self.dropped: Counter = Counter()
        self.kind = None                    # "date" or "number", set by the first instant
        self.aware: set = set()
        self.rows = 0
        self.columns: dict = {}             # {role: the header or XES key it was read from}
        self.origin = None                  # the first instant, in seconds, for a dated log

    def instant(self, text: str):
        text = (text or "").strip()
        if not text:
            return None
        try:
            value = float(text)
            kind = "number"
        except ValueError:
            try:
                stamp = datetime.fromisoformat(text)
            except ValueError:
                return None
            self.aware.add(stamp.tzinfo is not None)
            if len(self.aware) > 1:
                raise LogError("the log mixes instants with and without a UTC offset; "
                               "give every timestamp an offset, or none")
            value = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
            kind = "date"
        if not math.isfinite(value):
            return None
        if self.kind is None:
            self.kind = kind
        return value if kind == self.kind else None

    def add(self, case, activity, when, lifecycle, resource) -> None:
        self.rows += 1
        if self.rows > MAX_EVENTS:
            raise LogError(f"the log has more than {MAX_EVENTS} events, the ceiling")
        cells = (case, activity, when, lifecycle, resource)
        if any(c is not None and len(c) > MAX_CELL for c in cells):
            self.dropped["a cell over 1000 characters"] += 1
            return
        case, activity = (case or "").strip(), (activity or "").strip()
        if not case or not activity:
            self.dropped["no case or activity"] += 1
            return
        stage = _LIFECYCLE.get((lifecycle or "").strip().lower())
        if stage is None:
            self.dropped["lifecycle not one that is read"] += 1
            return
        t = self.instant(when)
        if t is None:
            self.dropped["timestamp unreadable"] += 1
            return
        self.raw.append((case, activity, t, stage, (resource or "").strip()))

    def events(self, time_unit: str) -> list[Event]:
        if not self.raw:
            raise LogError("no usable event in the log"
                           + (f" (dropped: {dict(self.dropped)})" if self.dropped else ""))
        t0 = min(r[2] for r in self.raw)
        self.origin = t0 if self.kind == "date" else None
        scale = _SECONDS[time_unit] if self.kind == "date" else 1.0
        return [Event(c, a, (t - t0) / scale, s, r) for c, a, t, s, r in self.raw]


def _read_bytes(path) -> bytes:
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise LogError(f"'{path.name}' is {size} bytes, over the {MAX_BYTES} byte ceiling")
    return path.read_bytes()


def _role_columns(header: list[str], columns: dict | None) -> dict[str, int]:
    """{role: column index}, by the flags first, then by the usual names (D546)."""
    lowered = [h.strip().lower() for h in header]
    found = {}
    for role, names in COLUMN_NAMES.items():
        wanted = (columns or {}).get(role)
        if wanted:
            if wanted.strip().lower() not in lowered:
                raise LogError(f"no column '{wanted}' for the {role}; the columns are "
                               f"{[h[:40] for h in header[:30]]}")
            found[role] = lowered.index(wanted.strip().lower())
            continue
        index = next((lowered.index(n) for n in names if n in lowered), None)
        if index is not None:
            found[role] = index
    for role in ("case", "activity"):
        if role not in found:
            raise LogError(f"no {role} column (looked for {', '.join(COLUMN_NAMES[role][:4])}; "
                           f"name it with --{role})")
    if "timestamp" not in found and not ("start" in found or "complete" in found):
        raise LogError("no timestamp column, and no start/complete columns "
                       "(name one with --timestamp)")
    return found


def _read_csv(data: bytes, columns, reader: _Reader) -> None:
    try:
        head = data[:4096].decode("utf-8-sig", errors="replace")
        delimiter = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    # Decoded as it is read: a whole decoded copy (and StringIO's 4-byte-a-character buffer)
    # tripled the peak; the bytes themselves are still held whole (D547, D554).
    text = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", errors="replace", newline="")
    rows = csv.reader(text, delimiter=delimiter)
    # One fate for an over-long cell: its row is dropped and counted (`_Reader.add`). The csv
    # module's own limit (131072) would refuse the whole log instead, so it is lifted for the
    # read; the file is already under MAX_BYTES. ponytail: a process-wide setting, restored.
    limit = csv.field_size_limit(min(MAX_BYTES, 2**31 - 1))     # a C long on Windows
    try:
        header = next(rows, None)
        if not header:
            raise LogError("the CSV is empty")
        roles = _role_columns(header, columns)
        reader.columns = {role: header[i].strip()[:40] for role, i in roles.items()}

        def cell(row, role):
            i = roles.get(role)
            return row[i] if i is not None and i < len(row) else None

        # Disco's shape (start and complete columns on one row) when there is no
        # timestamp column: one row is one activity instance, two events.
        per_instance = "timestamp" not in roles
        for row in rows:
            if not any(c.strip() for c in row):
                continue
            case, activity, resource = cell(row, "case"), cell(row, "activity"), cell(row, "resource")
            if per_instance:
                start, end = cell(row, "start"), cell(row, "complete")
                if start and start.strip():
                    reader.add(case, activity, start, "start", resource)
                if end and end.strip():
                    reader.add(case, activity, end, "complete", resource)
                elif not (start and start.strip()):
                    reader.add(case, activity, "", "complete", resource)  # counted as unreadable
            else:
                reader.add(case, activity, cell(row, "timestamp"), cell(row, "lifecycle"), resource)
    except csv.Error as exc:
        raise LogError(f"malformed CSV: {exc}") from None
    finally:
        csv.field_size_limit(limit)


def _refuse(*_args):
    raise LogError("a DOCTYPE or ENTITY declaration is refused (no entity is ever expanded)")


def _read_xes(data: bytes, reader: _Reader) -> None:
    """XES through Expat (D547), the whole text in memory as the CSV's is. Only attributes
    directly under a <trace> or an <event> are read; nested ones (lists, containers) and
    <global> defaults are not."""
    try:
        if b"\x00" in data[:4096]:
            raise UnicodeError
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise LogError("the XES file is not UTF-8 (re-save it as UTF-8)") from None
    if _DOCTYPE_RE.search(text):
        _refuse()
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse
    parser.EntityDeclHandler = _refuse
    stack: list[str] = []
    trace: dict = {}
    event: dict = {}
    events: list[dict] = []
    keys = {"concept:name", "time:timestamp", "lifecycle:transition", "org:resource"}
    roles = {"lifecycle:transition": "lifecycle", "org:resource": "resource"}

    def start(tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        parent = stack[-1] if stack else ""
        stack.append(tag)
        if tag == "trace":
            trace.clear()
            events.clear()
        elif tag == "event" and parent == "trace":
            event.clear()
        elif parent in ("trace", "event") and "key" in attrs:
            target = trace if parent == "trace" else event
            if len(target) < 64:
                target[attrs["key"]] = attrs.get("value", "")

    def end(tag):
        tag = tag.rsplit(":", 1)[-1]
        stack.pop()
        if tag == "event" and stack and stack[-1] == "trace":
            events.append(dict(event))
            if len(events) > MAX_EVENTS:
                raise LogError(f"a trace has more than {MAX_EVENTS} events, the ceiling")
        elif tag == "trace":
            case = trace.get("concept:name", "")
            for e in events:
                for key in keys.intersection(e):
                    reader.columns.setdefault(roles.get(key, "activity" if key == "concept:name"
                                                        else "timestamp"), key)
                reader.add(case, e.get("concept:name"), e.get("time:timestamp"),
                           e.get("lifecycle:transition"), e.get("org:resource"))
            events.clear()

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        raise LogError(f"not well-formed XML: {exc}") from None


def _check_time_unit(time_unit: str) -> None:
    if time_unit not in _TIME_UNITS:
        raise LogError(f"time unit must be one of {', '.join(_TIME_UNITS)}")


def read_log(path, *, time_unit: str = "minutes", columns: dict | None = None,
             fmt: str | None = None, info: dict | None = None) -> list[Event]:
    """The events of the log at *path* (CSV, or XES by its suffix or `fmt="xes"`), times in
    *time_unit* since the log's first instant (a numeric timestamp is taken as already in
    that unit). *columns* names a column per role (`{"case": "Order"}`); the rest are found
    by their usual names. *info*, when given, is filled with the format, rows and drops."""
    _check_time_unit(time_unit)
    unknown = sorted(set(columns or {}) - set(COLUMN_NAMES))
    if unknown:
        raise LogError(f"no column role {', '.join(unknown)[:200]}; the roles are "
                       f"{', '.join(COLUMN_NAMES)}")
    data = _read_bytes(path)
    fmt = fmt or ("xes" if str(path).lower().endswith(".xes") else "csv")
    reader = _Reader()
    if fmt == "xes":
        _read_xes(data, reader)
    elif fmt == "csv":
        _read_csv(data, columns, reader)
    else:
        raise LogError(f"unknown log format '{fmt}' (csv or xes)")
    events = reader.events(time_unit)
    if info is not None:
        info.update({"format": fmt, "rows": reader.rows, "events": len(events),
                     "timestamps": reader.kind, "dropped": dict(reader.dropped),
                     "columns": ({"case": "concept:name (trace)"} if fmt == "xes" else {})
                     | reader.columns, "origin": reader.origin,
                     "utc_offsets": reader.aware == {True}})
    return events


def log_from_trace(trace, *, ready: bool = True) -> list[Event]:
    """The event log of a run's trace (a `TraceRecorder` or its event list): per seize, a
    `start` at the seize and a `complete` at the release, the block as the activity and the
    seized resource; with *ready*, a `ready` (XES `schedule`) when the entity reached the
    block. A seize with no release before the trace ends is left out."""
    events = getattr(trace, "events", trace)
    arrived: dict = {}
    open_: dict = {}
    out: list[Event] = []
    for e in events:
        if e.kind == "entity_moved" and e.target:
            arrived[(e.entity_id, e.target)] = e.t + (e.duration or 0.0)
        elif e.kind == "entity_seized":
            open_[(e.entity_id, e.block)] = (e.t, e.target or "")
        elif e.kind == "entity_released":
            begun = open_.pop((e.entity_id, e.block), None)
            if begun is None:
                continue
            case = str(e.entity_id)
            at = arrived.get((e.entity_id, e.block))
            if ready and at is not None and at <= begun[0]:
                out.append(Event(case, e.block, at, "ready", begun[1]))
            out.append(Event(case, e.block, begun[0], "start", begun[1]))
            out.append(Event(case, e.block, e.t, "complete", begun[1]))
    out.sort(key=lambda ev: (ev.time, _RANK[ev.lifecycle]))
    return out


# ============================================================ instances

@dataclass(slots=True)
class _Instance:
    activity: str
    resource: str = ""
    ready: float | None = None
    start: float | None = None
    end: float | None = None
    paused: float = 0.0             # suspended time, between start and end (D631)
    since: float | None = None      # when the current suspension began

    @property
    def first(self) -> float:
        return min(t for t in (self.ready, self.start, self.end) if t is not None)


def _cases(events) -> tuple[dict[str, list[_Instance]], Counter]:
    """Per case, its activity instances in order (D548), and what was dropped. Per (case,
    activity) three FIFO queues, ready-but-not-started, started and suspended, so pairing is
    O(1) an event (#563): a start takes the oldest ready instance, a suspend the oldest
    started one, a resume the oldest suspended one (back to started), a complete the oldest
    started one, else suspended, else ready, else a new one. An abort ends the oldest
    suspended instance, else started, else ready; a withdraw the oldest ready one, else
    started, else suspended; either is dropped and counted. A suspend or resume with nothing
    to pair is ignored and counted, as is a suspend never resumed (its time stays in the
    service time, D631)."""
    by_case: dict[str, list[tuple]] = defaultdict(list)
    for i, e in enumerate(events):
        by_case[e.case].append((e.time, _RANK[e.lifecycle], i, e))
    cases, dropped = {}, Counter()
    for case, rows in by_case.items():
        rows.sort(key=lambda r: r[:3])
        pending: dict[str, deque] = defaultdict(deque)
        started: dict[str, deque] = defaultdict(deque)
        suspended: dict[str, deque] = defaultdict(deque)
        seen: set = set()
        done: list[_Instance] = []
        for _t, _r, _i, e in rows:
            if e in seen:       # an exact duplicate row, as joined exports make (#564)
                dropped["duplicate event"] += 1
                continue
            seen.add(e)
            a = e.activity
            if e.lifecycle == "ready":
                pending[a].append(_Instance(a, e.resource, ready=e.time))
            elif e.lifecycle == "start":
                inst = pending[a].popleft() if pending[a] else _Instance(a)
                inst.start, inst.resource = e.time, e.resource or inst.resource
                started[a].append(inst)
            elif e.lifecycle == "suspend":
                if not started[a]:
                    dropped["suspend with no running instance (ignored)"] += 1
                    continue
                inst = started[a].popleft()
                inst.since = e.time
                suspended[a].append(inst)
            elif e.lifecycle == "resume":
                if not suspended[a]:
                    dropped["resume with no suspended instance (ignored)"] += 1
                    continue
                inst = suspended[a].popleft()
                inst.paused += e.time - inst.since
                inst.since = None
                started[a].append(inst)
            elif e.lifecycle in ("abort", "withdraw"):
                if e.lifecycle == "abort":
                    order = (suspended, started, pending)
                else:
                    order = (pending, started, suspended)
                queue = next((q[a] for q in order if q[a]), None)
                if queue:
                    queue.popleft()
                    dropped["activity instance aborted or withdrawn"] += 1
                else:
                    dropped["abort or withdraw with nothing to end (ignored)"] += 1
            else:
                queue = started[a] or suspended[a] or pending[a]
                inst = queue.popleft() if queue else _Instance(a)
                if inst.since is not None:
                    dropped["suspend never resumed (its time kept in the service)"] += 1
                    inst.since = None
                inst.end, inst.resource = e.time, inst.resource or e.resource
                done.append(inst)
        left = sum(len(q) for qs in (pending, started, suspended) for q in qs.values())
        if left:
            dropped["activity instance never completed"] += left
        if done:
            done.sort(key=lambda x: x.first)
            cases[case] = done
    return cases, dropped


def _case_arrival(instances: list[_Instance]) -> float:
    return min(x.first for x in instances)


def _samples(cases) -> dict:
    """Per activity: service, wait, sojourn and gap samples, and the overlap count."""
    out = defaultdict(lambda: {"service": [], "wait": [], "sojourn": [], "gap": [],
                               "suspended": [], "count": 0})
    overlaps = 0
    for instances in cases.values():
        prev_end = None
        for inst in instances:
            s = out[inst.activity]
            s["count"] += 1
            if prev_end is not None and inst.first < prev_end - 1e-9:
                overlaps += 1
            if inst.start is not None:
                s["service"].append(max(0.0, inst.end - inst.start - inst.paused))
                if inst.paused > 0:
                    s["suspended"].append(inst.paused)
                origin = inst.ready if inst.ready is not None else (
                    prev_end if prev_end is not None else inst.start)
                s["wait"].append(max(0.0, inst.start - origin))
            else:
                origin = inst.ready if inst.ready is not None else (
                    prev_end if prev_end is not None else inst.end)
                s["sojourn"].append(max(0.0, inst.end - origin))
            if inst.ready is not None and prev_end is not None:
                s["gap"].append(max(0.0, inst.ready - prev_end))
            prev_end = inst.end if prev_end is None else max(prev_end, inst.end)
    return out, overlaps


def _mean(values) -> float | None:
    return float(np.mean(values)) if len(values) else None


def log_kpis(events) -> dict:
    """The log's own KPIs: cases, span, throughput (cases per time unit), mean cycle time
    (first instant to last completion of a case), and per activity its count, mean service
    and mean wait. The round trip compares a draft's run with its log on exactly these."""
    return _kpis(_cases(events)[0])


def _kpis(cases) -> dict:
    if not cases:
        raise LogError("no completed activity instance in the log")
    arrivals = [_case_arrival(v) for v in cases.values()]
    ends = [max(x.end for x in v) for v in cases.values()]
    span = max(ends) - min(arrivals)
    samples, _ = _samples(cases)
    return {
        "cases": len(cases), "span": span,
        "throughput": len(cases) / span if span > 0 else None,
        "cycle_time": float(np.mean(np.subtract(ends, arrivals))),
        "activities": {a: {"count": s["count"], "service": _mean(s["service"]),
                           "wait": _mean(s["wait"]), "sojourn": _mean(s["sojourn"])}
                       for a, s in sorted(samples.items())},
    }


# ============================================================ fitting (D551)

_SMALL_FAMILIES = ["Exponential", "Gamma", "Lognormal", "Triangular"]


def _tidy(value):
    if isinstance(value, float):
        return float(f"{value:.6g}") if math.isfinite(value) else None     # the report is JSON
    if isinstance(value, list):
        return [_tidy(v) for v in value]
    return value


def _fit(values) -> dict:
    """{"distribution", "dist_params", "n", "mean", "basis", "ks_p"} for a duration sample."""
    from nagasim import inputfit
    from nagasim.core.distributions import distribution_mean

    data = np.asarray(values, dtype=float)
    n, mean = int(data.size), float(np.mean(data))
    if n < 3 or float(np.ptp(data)) <= 1e-12 * max(1.0, abs(mean)):
        return {"distribution": "Constant", "dist_params": {"value": _tidy(mean)}, "n": n,
                "mean": mean, "basis": "constant: fewer than 3 values, or all equal", "ks_p": None}
    full = data if n <= REFIT_SAMPLE else \
        np.random.default_rng(1).choice(data, REFIT_SAMPLE, replace=False)
    if n > FIT_SAMPLE:
        data = np.random.default_rng(0).choice(data, FIT_SAMPLE, replace=False)
    # Durations: the continuous families with a non-negative support (Normal left out),
    # whatever the data's type: times in whole minutes would otherwise get a counting family.
    families = _SMALL_FAMILIES if n < 30 else [f for f in inputfit.CONTINUOUS_FAMILIES
                                               if f != "Normal"]
    for fit in inputfit.fit_candidates(data, candidates=families):
        if full is not data:
            # The sample picks the family; its parameters come from every value (up to
            # REFIT_SAMPLE), or the sample's own mean (about 1.4% off at 5000 values)
            # biases the whole draft (#562).
            refit = inputfit.fit_candidates(full, candidates=[fit.distribution])
            if not refit:
                continue
            fit = refit[0]
        # A duration is never negative: Beta and Triangular pad their bound 1% below the
        # data's minimum, so on times starting at 0 they would sample negative ones (#574).
        if min(fit.dist_params.get("min", 0.0), fit.dist_params.get("low", 0.0)) < 0:
            continue
        fitted = distribution_mean(fit.distribution, fit.dist_params)
        if fitted is not None and math.isfinite(fitted) and abs(fitted - mean) <= 0.05 * abs(mean):
            return {"distribution": fit.distribution,
                    "dist_params": {k: _tidy(v) for k, v in fit.dist_params.items()},
                    "n": n, "mean": mean, "ks_p": _tidy(fit.ks_pvalue),
                    "basis": f"best AIC of {len(families)} families with a mean within 5%"
                             + ("; KS p-value is optimistic (parameters from the same sample)")}
    spec = inputfit.empirical_spec(data)
    return {"distribution": spec["distribution"],
            "dist_params": {k: _tidy(list(v)) for k, v in spec["dist_params"].items()},
            "n": n, "mean": mean, "ks_p": None, "basis": "empirical: no family kept the mean"}


# ============================================================ names (D555)

def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _name(text: str, used: set) -> str:
    """*text* as a name of the draft: `[A-Za-z0-9_]`, starting with a letter, at most 40
    characters, not in *used* (then added to it)."""
    base = re.sub(r"[^A-Za-z0-9]+", "_", _fold(text)).strip("_") or "item"
    if not base[0].isalpha():
        base = "a_" + base
    base = base[:40].rstrip("_")
    name, k = base, 2
    while name in used:
        suffix = f"_{k}"
        name, k = base[:40 - len(suffix)] + suffix, k + 1
    used.add(name)
    return name


def _label(text: str) -> str:
    return " ".join("".join(c for c in _fold(text) if c.isprintable()).split())[:80]


# ============================================================ routing (D549, D550)

def _history_test(sequences) -> dict:
    """Does the next activity depend on the one before (beyond first order)? The weighted
    total variation distance between P(next | previous, activity) and P(next | activity)
    over pairs seen at least 20 times, and the smallest chi-square p-value over activities."""
    from scipy.stats import chi2_contingency

    first = defaultdict(Counter)
    second = defaultdict(Counter)
    for seq in sequences:
        tokens = ["<start>", *seq, "<end>"]
        for prev, act, nxt in zip(tokens, tokens[1:], tokens[2:]):
            first[act][nxt] += 1
            second[(prev, act)][nxt] += 1
    total = weighted = 0.0
    for (prev, act), counts in second.items():
        n = sum(counts.values())
        if n < 20:
            continue
        base = first[act]
        nb = sum(base.values())
        tv = 0.5 * sum(abs(counts[k] / n - base[k] / nb) for k in set(base) | set(counts))
        weighted += n * tv
        total += n
    p_min = 1.0
    for act in first:
        rows = [c for (p, a), c in second.items() if a == act and sum(c.values()) >= 20]
        cols = sorted({k for c in rows for k in c})
        if len(rows) >= 2 and len(cols) >= 2:
            table = [[c[k] for k in cols] for c in rows]
            p_min = min(p_min, float(chi2_contingency(table)[1]))
    tv = weighted / total if total else 0.0
    return {"tv": tv, "p": p_min, "depends": tv > 0.1 and p_min < 0.001}


def _choose_routing(sequences, routing: str) -> dict:
    variants = Counter(tuple(s) for s in sequences)
    kept = [(v, n) for v, n in variants.most_common() if n >= 2][:MAX_VARIANTS]
    coverage = sum(n for _, n in kept) / len(sequences)
    test = _history_test(sequences)
    if routing == "auto":
        mode = "variants" if test["depends"] and coverage >= 0.95 else "markov"
        why = (f"the next activity depends on the previous one (TV {test['tv']:.3f}, "
               f"p {test['p']:.2g}) and {len(kept)} variants cover {coverage:.1%} of cases"
               if mode == "variants" else
               f"first-order transitions explain the log (TV {test['tv']:.3f}, p {test['p']:.2g})"
               if not test["depends"] else
               f"history matters (TV {test['tv']:.3f}) but {len(kept)} variants seen twice or "
               f"more cover only {coverage:.1%} of cases: first-order kept")
    else:                               # checked by `discover` before the log is read
        mode, why = routing, "chosen with --routing"
    if mode == "variants" and not kept:
        raise LogError("no variant is seen twice: route with --routing markov")
    return {"mode": mode, "reason": why, "variants": len(variants), "history": test,
            "kept": kept if mode == "variants" else [],
            "coverage": coverage if mode == "variants" else 1.0}


# ============================================================ resources (D553)

def _peak(intervals) -> int:
    marks = sorted([(s, 1) for s, e in intervals] + [(e, -1) for s, e in intervals],
                   key=lambda m: (m[0], m[1]))
    level = peak = 0
    for _t, step in marks:
        level += step
        peak = max(peak, level)
    return peak


def _resources(cases, activities, span) -> tuple[list, list, dict, set]:
    """The draft's resources (D553): one per log resource, and one per activity whose
    instances name none, its capacity the observed peak in service. Returns the resources,
    their report, {activity: the names it seizes} and the names taken."""
    wanted = set(activities)
    intervals: dict[tuple, list] = defaultdict(list)
    paused: Counter = Counter()
    keys_of: dict[str, set] = defaultdict(set)
    for v in cases.values():
        for x in v:
            if x.start is None or x.activity not in wanted:
                continue
            # Keyed by kind and name, so a resource named like an activity stays its own.
            key = ("resource", x.resource) if x.resource else ("activity", x.activity)
            intervals[key].append((x.start, x.end))
            paused[key] += x.paused
            keys_of[x.activity].add(key)
    used: set = set()
    name_of: dict[tuple, str] = {}
    resources, report = [], []
    for key, spans in intervals.items():
        own = key[0] == "activity"
        name = name_of[key] = _name(key[1], used)
        # ponytail: the peak counts a suspended instance as in service; split its interval
        # if that ever sets a capacity too high.
        cap = _peak(spans)
        busy = sum(e - s for s, e in spans) - paused[key]
        resources.append({"name": name, "capacity": cap, "discipline": "FIFO"})
        report.append({"name": name, "original": key[1],
                       "capacity": cap, "basis": "observed peak in service (a lower bound)"
                       + ("; no resource column: one per activity" if own else ""),
                       "utilisation": busy / (cap * span) if span > 0 and cap else None})
    names_of = {a: [name_of[k] for k in sorted(keys)] for a, keys in keys_of.items()}
    return resources, report, names_of, used


# ============================================================ the draft's pieces

def _activity_blocks(activities, samples, block_of, names_of, used_res):
    """One block per activity: a PROCESS seizing its resource (or a set of them) when the
    log has its starts, else a DELAY of its sojourn. Returns blocks, sets and the report."""
    blocks, resource_sets, report = [], [], []
    for a in activities:
        s = samples[a]
        block = {"name": block_of[a], "description": _label(a) or block_of[a]}
        entry = {"original": a, "block": block_of[a], "count": s["count"],
                 "mean_wait": _mean(s["wait"]), "mean_gap": _mean(s["gap"])}
        if s["suspended"]:
            entry["suspended"] = {"instances": len(s["suspended"]),
                                  "mean": _mean(s["suspended"])}
        if s["service"]:
            fit = _fit(s["service"])
            names = names_of[a]
            if len(names) == 1:
                seized = names[0]
            else:
                seized = _name(f"{block_of[a]}_pool", used_res)
                resource_sets.append({"name": seized, "members": names,
                                      "rule": "SmallestNumberBusy"})
            block.update(type="PROCESS", params={
                "action": "Seize Delay Release", "distribution": fit["distribution"],
                "dist_params": fit["dist_params"], "resource_name": seized})
            entry.update(lifecycle="start/complete", service=fit, resource=seized,
                         sojourn_only=len(s["sojourn"]))
        else:
            fit = _fit(s["sojourn"])
            block.update(type="DELAY", params={"distribution": fit["distribution"],
                                               "dist_params": fit["dist_params"]})
            entry.update(lifecycle="complete only", sojourn=fit, resource=None)
        blocks.append(block)
        report.append(entry)
    return blocks, resource_sets, report


def _transfers(cases, block_of) -> tuple[dict, dict]:
    """Transfer gaps: a completion to the next instance's schedule (only a log with schedule
    events shows them; without, the gap is part of the wait). Returns the gaps per (from,
    to) block pair and a fitted delay per pair whose mean is not zero."""
    gaps_of: dict[tuple, list] = defaultdict(list)
    for v in cases.values():
        for prev, inst in zip(v, v[1:]):
            if inst.ready is not None and prev.activity in block_of \
                    and inst.activity in block_of:
                gaps_of[(block_of[prev.activity], block_of[inst.activity])].append(
                    max(0.0, inst.ready - prev.end))
    transfer: dict[tuple, dict] = {}
    for key, values in gaps_of.items():
        if float(np.mean(values)) > 1e-9:
            fit = _fit(values)
            transfer[key] = {"distribution": fit["distribution"],
                             "dist_params": fit["dist_params"]}
    return gaps_of, transfer


def _branch(blocks, links, source_block: str, counts: Counter, decide: str,
            delays=None) -> None:
    """Links from *source_block* to each target in *counts*, through a DECIDE by chance
    when there is more than one; the least frequent target is the ELSE branch."""
    ordered = [t for t, _ in counts.most_common()]
    delays = delays or {}

    def link(src, tgt):
        entry = {"source": src, "target": tgt}
        if tgt in delays:
            entry["delay"] = delays[tgt]
        links.append(entry)

    if len(ordered) == 1:
        link(source_block, ordered[0])
        return
    total = sum(counts.values())
    blocks.append({"name": decide, "type": "DECIDE",
                   "params": {"probs": [_tidy(counts[t] / total) for t in ordered[:-1]]}})
    links.append({"source": source_block, "target": decide})
    for t in ordered:
        link(decide, t)


def _wire_markov(sequences, activities, block_of, transfer, used_blocks, blocks, links):
    """First-order routing (D549): after each block a DECIDE to the observed next ones."""
    nxt: dict[str, Counter] = defaultdict(Counter)
    for seq in sequences:
        path = ["arrivals", *[block_of[a] for a in seq], "done"]
        for here, there in zip(path, path[1:]):
            nxt[here][there] += 1
    _branch(blocks, links, "arrivals", nxt["arrivals"], "first_step")
    for a in activities:
        b = block_of[a]
        if nxt[b]:
            _branch(blocks, links, b, nxt[b], _name(f"after_{b}", used_blocks),
                    {t: transfer[(b, t)] for t in nxt[b] if (b, t) in transfer})


def _wire_variants(kept, activities, block_of, gaps_of, used_blocks, blocks, links) -> list:
    """One sequence per kept variant (D550), picked by chance after the CREATE and followed
    through a ROUTE with one pooled travel time. Returns the sequences section."""
    station = {a: _name(f"at_{block_of[a]}", used_blocks) for a in activities}
    pooled = [g for values in gaps_of.values() for g in values]
    travel = {}
    if pooled and float(np.mean(pooled)) > 1e-9:
        fit = _fit(pooled)
        travel = {"distribution": fit["distribution"], "dist_params": fit["dist_params"]}
    blocks.append({"name": "route", "type": "ROUTE", "params": travel})
    links.append({"source": "route", "target": "done"})
    sequences, shares = [], Counter()
    for k, (variant, n) in enumerate(kept, start=1):
        name = f"V{k}"
        assign = _name(f"variant_{k}", used_blocks)
        sequences.append({"name": name, "steps": [{"station": station[a]} for a in variant]})
        blocks.append({"name": assign, "type": "ASSIGN", "params": {
            "assignments": [{"target": "entity.sequence", "expr": f"'{name}'"}]}})
        links.append({"source": assign, "target": "route"})
        shares[assign] = n
    _branch(blocks, links, "arrivals", shares, "pick_variant")
    for a in activities:
        blocks.append({"name": station[a], "type": "STATION"})
        links.append({"source": station[a], "target": block_of[a]})
        links.append({"source": block_of[a], "target": "route"})
    return sequences


MAX_RULES_PER_KIND = 4      # D731: per rule kind, so a long log cannot fill the section


def _route_rules(counts: Counter, block_of: dict) -> tuple[list, int]:
    """The log's route invariants over the cases the draft routes (*counts*: activity sequence
    -> cases), as `path_checks` rules (D731), and how many more were found than kept:

    * `always`: an activity every routed case visits;
    * `order`: two activities seen together in at least two cases, the first one's first visit
      before the other's in every such case (judged on first visits, as `pathcheck` does);
      a before c is left out when a before b and b before c both hold (checked through b);
    * `at_most`: the most visits one case paid an activity, for the activities a case
      visits more than once first (rework), then the most visited.

    At most MAX_RULES_PER_KIND of each kind, the best supported first. Each says it comes
    from the log in its description; `naga check` runs them on one seed (D722)."""
    total = sum(counts.values())
    acts = sorted({a for seq in counts for a in seq})
    always = [a for a in acts if all(a in seq for seq in counts)]
    always.sort(key=lambda a: min(seq.index(a) for seq in counts))
    together, before, seen = Counter(), Counter(), Counter()
    for seq, n in counts.items():
        first = {}
        for i, a in enumerate(seq):
            first.setdefault(a, i)
        for a, i in first.items():
            seen[a] += n
            for b, j in first.items():
                if a != b:
                    together[(a, b)] += n
                    before[(a, b)] += n if i < j else 0
    pairs = {p: n for p, n in together.items() if n >= 2 and before[p] == n}
    after = defaultdict(set)
    for a, b in pairs:
        after[a].add(b)
    order = sorted(((a, b) for a, b in pairs if not any((m, b) in pairs for m in after[a])),
                   key=lambda p: (-pairs[p], p))
    most = {a: max(seq.count(a) for seq in counts) for a in acts}
    at_most = sorted(acts, key=lambda a: (most[a] < 2, -seen[a], a))
    rules = [{"name": f"log_always_{block_of[a]}", "rule": "always", "block": block_of[a],
              "description": f"from the log: all {total} routed cases visit it"}
             for a in always[:MAX_RULES_PER_KIND]]
    rules += [{"name": f"log_order_{block_of[a]}_{block_of[b]}", "rule": "order",
               "first": block_of[a], "then": block_of[b],
               "description": f"from the log: first in each of the {pairs[(a, b)]} routed "
                              "cases that visit both"}
              for a, b in order[:MAX_RULES_PER_KIND]]
    rules += [{"name": f"log_at_most_{block_of[a]}", "rule": "at_most", "block": block_of[a],
               "count": most[a],
               "description": f"from the log: no routed case visits it more than {most[a]} "
                              f"time{'s' if most[a] > 1 else ''}"}
              for a in at_most[:MAX_RULES_PER_KIND]]
    left = (max(0, len(always) - MAX_RULES_PER_KIND) + max(0, len(order) - MAX_RULES_PER_KIND)
            + max(0, len(at_most) - MAX_RULES_PER_KIND))
    return rules, left


def _arrivals(cases, assumptions) -> tuple[list, dict]:
    """The case arrivals, sorted, and the interarrival fit (D552)."""
    arrivals = sorted(_case_arrival(v) for v in cases.values())
    if arrivals[-1] <= arrivals[0]:
        raise LogError("every case arrives at the same instant: the log has no arrival rate "
                       "to fit (are its timestamps dates without a time?)")
    if not any(x.ready is not None for v in cases.values() for x in v):
        assumptions.append("no schedule events: a case arrives at its first instant, so the "
                           "wait before its first activity is not in the log and the draft's "
                           "arrivals are that activity's service starts")
    assumptions.append("arrivals are stationary: one interarrival distribution for the whole "
                       "log (a time-varying rate is not inferred)")
    return arrivals, _fit(np.diff(arrivals))


def _long_cycle(cases) -> float:
    """The 99th-percentile cycle time: a case that arrived within it of the log's end may
    still be running, one within it of the log's start may have begun before."""
    return float(np.quantile([max(x.end for x in v) - _case_arrival(v) for v in cases.values()],
                             0.99))


def _routed(cases, log_end, dropped, assumptions) -> dict:
    """The cases the routing is drawn from. A case that arrived within the last
    99th-percentile cycle time of the log's end may still be running, its tail not yet in
    the log: such cases are left out (chosen by arrival time, not by path, so the routing is
    not biased by it)."""
    horizon = log_end - _long_cycle(cases)
    routed = {c: v for c, v in cases.items() if _case_arrival(v) <= horizon}
    if len(routed) < len(cases) / 2:
        assumptions.append("the log is too short to tell running cases from finished ones: "
                           "a case still running counts as leaving after its last activity")
        return cases
    if len(routed) < len(cases):
        dropped["cases maybe still running at the log's end (routing only)"] += \
            len(cases) - len(routed)
    return routed


# ============================================================ is the log good enough? (D592)

def _busiest_share(seconds, period: float, bins: int, window: int) -> float:
    """The largest share of the instants *seconds* in *window* consecutive bins of *bins* per
    *period*, wrapping, so the time zone or the weekday the epoch fell on does not matter."""
    counts = np.bincount((np.asarray(seconds) % period * bins / period).astype(int) % bins,
                         minlength=bins)
    ring = np.concatenate([counts, counts[:window - 1]])
    return float(np.convolve(ring, np.ones(window), "valid").max() / counts.sum())


def _idle_waits(cases, samples, span) -> list[tuple[float, str]]:
    """(mean wait, activity), longest first, for each activity that waits long (over twice
    its service) at resources busy under half the time. Such a wait is not a queue:
    calendars, deliberate scheduling, priorities or waiting on the customer. The draft's
    resources make waits only by being busy, so it reproduces none of it (BPI 2012, D593)."""
    _res, res_report, names_of, _used = _resources(cases, list(samples), span)
    busy = {r["name"]: r["utilisation"] or 0.0 for r in res_report}
    return sorted(
        ((_mean(s["wait"]), a) for a, s in samples.items()
         if s["service"] and s["wait"] and _mean(s["wait"]) > 2 * _mean(s["service"])
         and max(busy[name] for name in names_of[a]) < 0.5), reverse=True)


def _idle_wait_delays(flagged, samples, block_of, used_blocks, blocks, links) -> list:
    """D630: a DELAY before each activity the check flagged (*flagged*, from `_idle_waits`),
    fitted to its observed waits, every link into the activity redirected through it.
    Returns the report entries.

    ponytail: the DELAY takes the whole observed wait, and the queue behind it still adds
    its own; at resources under half busy that is small. Fit the DELAY to the wait less a
    queue's own if a draft ever runs these resources busy."""
    report = []
    for _wait, a in sorted(flagged, key=lambda w: w[1]):
        if a not in block_of:               # not in the routing: no block to wait before
            continue
        b = block_of[a]
        name = _name(f"idle_wait_{b}", used_blocks)
        fit = _fit(samples[a]["wait"])
        for link in links:
            if link["target"] == b:
                link["target"] = name
        blocks.append({"name": name, "type": "DELAY",
                       "description": f"Modelled wait before {b}, not a queue: fitted to "
                                      "the log's waits at a mostly idle resource",
                       "params": {"distribution": fit["distribution"],
                                  "dist_params": fit["dist_params"]}})
        links.append({"source": name, "target": b})
        report.append({"activity": a, "block": b, "delay": name, "wait": fit})
    return report


def _ramp_up(cases, arrivals, cycle) -> float | None:
    """D632: mean work in progress over the log's first mean cycle time, as a share of that
    over the next one; None when it cannot tell: fewer than 30 cases arrive in that first
    cycle time (a small count swings the ratio from 0.1 to 2, seed to seed, measured), the
    log's arrivals stop before two cycle times (the cases then drain, not ramp), or a case
    has fewer than 5 activity instances on average (#643: a running case enters the log at
    its first event, so with two or three a case a log begun running read about 0.55; with
    five, 0.85, measured). A log begun with the system running is near 1, one begun empty
    about 0.5 (0.5 for equal cycle times, 0.48 for exponential ones)."""
    t0, last = float(arrivals[0]), float(arrivals[-1])
    if cycle <= 0 or t0 + 2 * cycle > last or np.searchsorted(arrivals, t0 + cycle, "right") < 30 \
            or sum(map(len, cases.values())) < 5 * len(cases):
        return None
    a = np.array([_case_arrival(v) for v in cases.values()])
    e = np.array([max(x.end for x in v) for v in cases.values()])

    def wip(t1, t2):            # the time-average of cases open in [t1, t2]
        return float(np.clip(np.minimum(e, t2) - np.maximum(a, t1), 0, None).sum()) / (t2 - t1)

    later = wip(t0 + cycle, t0 + 2 * cycle)
    return wip(t0, t0 + cycle) / later if later > 0 else None


def _check(cases, info, dropped, time_unit) -> tuple[dict, list]:
    """What the log holds and what each gap means for the draft: columns, lifecycle coverage,
    cases, span, censoring, arrivals over the day and the week, and the rows dropped. Also
    returns the activities flagged for idle waits, so the draft uses the check's own list."""
    instances = [x for v in cases.values() for x in v]
    n, k = len(instances), len(cases)
    started = sum(x.start is not None for x in instances)
    ready = sum(x.ready is not None for x in instances)
    named = sum(bool(x.resource) for x in instances)
    arrivals = np.sort([_case_arrival(v) for v in cases.values()])
    log_end = max(x.end for x in instances)
    span = log_end - float(arrivals[0])
    cycle = float(np.mean([max(x.end for x in v) - _case_arrival(v) for v in cases.values()]))
    long = _long_cycle(cases)
    at_end = int(np.sum(arrivals > log_end - long))
    # A case near the start that begins with an activity later cases rarely begin with was
    # probably already running when the log began (left-censored).
    later = Counter(v[0].activity for v in cases.values() if _case_arrival(v) > arrivals[0] + long)
    usual = sum(later.values())
    at_start = sum(1 for v in cases.values() if usual and _case_arrival(v) <= arrivals[0] + long
                   and later[v[0].activity] < 0.05 * usual)
    counts = Counter(x.activity for x in instances)
    samples, overlaps = _samples(cases)
    idle_waits = _idle_waits(cases, samples, span)
    findings: list[dict] = []

    def note(code, level, what, means):
        findings.append({"code": code, "level": level, "what": what, "means": means})

    if k < 30:
        note("few_cases", "weak", f"{k} cases",
             "every fit rests on a handful of values: the draft's times are rough guesses")
    elif k < 200:
        note("few_cases", "warn", f"{k} cases", "the fits are rough; a few hundred cases firm them")
    thin = sorted(a for a, c in counts.items() if c < 30)
    if thin and k >= 30:
        note("thin_activities", "warn", f"{len(thin)} of {len(counts)} activities seen fewer than "
             f"30 times ({', '.join(_label(a)[:30] for a in thin[:4])}"
             + (", ..." if len(thin) > 4 else "") + ")",
             "their times rest on few values (fewer than 3: a constant)")
    if not started:
        note("complete_only", "weak", "complete events only: nothing shows when work started",
             "each activity is a DELAY of the time since the case's previous event: the draft "
             "gives back cycle times, not queues, resources or behaviour under more load")
    elif started < n:
        note("partial_start", "warn", f"{started / n:.0%} of activity instances have a start",
             "those activities get a service time and a resource; the others are DELAYs of "
             "their whole sojourn")
    if started and not ready:
        note("no_schedule", "warn", "no schedule events",
             "the wait before a case's first activity is not in the log: the case is taken to "
             "arrive when that activity starts, so the draft under-states the queue there")
    if started and not named:
        note("no_resource", "warn", "no resource named",
             "each activity gets a resource of its own, capacity its observed peak of "
             "concurrent work; who does what is not known")
    if idle_waits:
        wait, a = idle_waits[0]
        note("idle_waits", "warn", f"{len(idle_waits)} "
             + ("activity waits" if len(idle_waits) == 1 else "activities wait")
             + f" long while the resources are mostly idle (the longest: {_label(a)[:40]}, mean wait "
             f"{wait:.3g}, service {_mean(samples[a]['service']):.3g})",
             "the waits are not a queue for a busy resource: the draft's resources are free, "
             "so it reproduces none of them, and its cycle time comes out short")
    if cycle > 0 and span < 20 * cycle:
        note("short_log", "warn", f"the log spans {span / cycle:.1f} mean cycle times",
             "cases cut at its ends weigh more, and the rate and the routing rest on few cycles")
    if at_end:
        note("censored_end", "warn" if at_end > 0.05 * k else "ok",
             f"{at_end} cases arrived within the log's last 99th-percentile cycle time",
             "they may still be running: left out of the routing, kept for the times")
    if at_start:
        note("censored_start", "warn" if at_start > 0.05 * k else "ok",
             f"{at_start} cases near the log's start begin with an activity that is rarely first",
             "they were probably running when the log began: each arrives at its first event, "
             "a few extra arrivals with short cycles")
    ramp = _ramp_up(cases, arrivals, cycle) if k >= 50 else None
    if ramp is not None and ramp < 0.6:
        weight = cycle / float(arrivals[-1] - arrivals[0])
        note("ramp_up", "warn" if weight >= 0.05 else "ok",
             f"the log starts empty: work in progress over its first mean cycle time is "
             f"{ramp:.0%} of that over the next one",
             "its start is a ramp-up, not the steady state: the draft's first window (and a "
             "draft from the log's first part) under-states the load")
    gaps = np.diff(arrivals)
    if k >= 50 and arrivals[-1] > arrivals[0]:
        mid = (arrivals[0] + arrivals[-1]) / 2
        first, second = int(np.sum(arrivals < mid)), int(np.sum(arrivals >= mid))
        if abs(first / second - 1) > 0.25:
            note("arrival_trend", "warn", f"arrivals change over the log: {first} cases arrive in "
                 f"its first half, {second} in its second",
                 "one stationary rate is fitted, the average: draft from a steady stretch, or "
                 "give the CREATE a schedule")
        if float(np.mean(gaps <= 1e-9)) > 0.1:
            note("batches", "warn", f"{np.mean(gaps <= 1e-9):.0%} of cases arrive at the same "
                 "instant as the one before",
                 "they come in batches (or the clock is coarse): the draft fits gaps of 0 into "
                 "one distribution, not a batch size")
    origin = info.get("origin")
    scale = _SECONDS[time_unit]
    if origin is not None and k >= 50 and span * scale >= 2 * 86400:
        day = _busiest_share(origin + arrivals * scale, 86400, 24, 12)
        if day >= 0.8:
            note("arrival_hours", "warn", f"{day:.0%} of cases arrive in the busiest 12 hours of "
                 "the day", "the draft spreads them over all 24: its nights are too busy and its "
                 "days too quiet")
        work = [x.start if x.start is not None else x.end for x in instances]
        busy = _busiest_share(origin + np.asarray(work) * scale, 86400, 24, 12)
        if busy >= 0.9:
            note("shifts", "warn", f"{busy:.0%} of the work happens in the busiest 12 hours of "
                 "the day", "work comes in shifts, but the draft's resources work around the "
                 "clock: its waits come out shorter")
        if span * scale >= 14 * 86400:
            week = _busiest_share(origin + arrivals * scale, 7 * 86400, 7, 5)
            if week >= 0.95:
                note("weekly", "warn", f"{week:.0%} of cases arrive on the busiest 5 days of the "
                     "week", "the draft spreads them over all 7")
    paused = [v for s in samples.values() for v in s["suspended"]]
    if paused:
        note("suspended", "ok", f"{len(paused)} activity instances were suspended (mean "
             f"{_mean(paused):.3g} suspended)",
             "the suspended time is left out of their service time and not modelled: the "
             "draft's cycle times come out shorter by it")
    if overlaps:
        note("overlap", "warn" if overlaps > 0.05 * n else "ok",
             f"{overlaps} activity instances overlap the previous one in their case",
             "parallel work: the draft runs them one after the other, so its cycles are longer")
    lost = sum(dropped.values())
    if lost:
        note("dropped", "warn" if lost > 0.05 * (info.get("rows") or n) else "ok",
             f"{lost} dropped (" + "; ".join(f"{v} {why}" for why, v in dropped.items())[:300]
             + ")", "the draft is drawn from what is left")
    levels = {f["level"] for f in findings}

    def when(t):
        if origin is None:
            return _tidy(float(t))
        stamp = datetime.fromtimestamp(origin + t * scale, timezone.utc)
        if not info.get("utc_offsets"):
            stamp = stamp.replace(tzinfo=None)      # as the log wrote it, without an offset
        return stamp.isoformat(timespec="seconds")

    return {"verdict": "weak" if "weak" in levels else
                       "usable with care" if "warn" in levels else "good",
            "columns": dict(info.get("columns") or {}), "cases": k, "events": info.get("events"),
            "activities": len(counts),
            "resources": len({x.resource for x in instances if x.resource}),
            "first": when(arrivals[0]), "last": when(log_end), "span": _tidy(span),
            "mean_cycle_time": _tidy(cycle),
            "instances": {"total": n, "with_start": started, "with_schedule": ready,
                          "with_resource": named},
            "censored": {"at_start": at_start, "at_end": at_end},
            "findings": findings}, idle_waits


# What to do about a finding, in the draft (D591).
_ACTIONS = {
    "few_cases": "draft again from a longer log before trusting a time",
    "no_schedule": "time the queue before the first activity some other way (or export the "
                   "log with schedule events): the draft has none there",
    "complete_only": "give the activities that queue a resource and a service time from "
                     "another source (a time study, a machine's cycle); they are DELAYs now",
    "partial_start": "the DELAYs among the activities have no resource: give the ones that "
                     "queue one",
    "no_resource": "replace the one-resource-per-activity guess with the real resources",
    "idle_waits": "put the long waits in front of mostly idle resources into the model "
                  "yourself (a DELAY before the activity, or the calendar that causes them): "
                  "no queue in the draft will make them",
    "short_log": "draft again from a log spanning many cycle times",
    "ramp_up": "give the draft a warm-up (simulation.warmup) of a few mean cycle times before "
               "comparing it with a steady stretch, or draft from a later part of the log: "
               "its start is a ramp-up from empty",
    "censored_start": "cut the log's first cycle time off, or the draft gets a few extra "
                      "arrivals with short cycles",
    "arrival_trend": "draft from a steady stretch of the log, or give the CREATE a schedule "
                     "(inputfit.arrival_rates fits one)",
    "arrival_hours": "give the CREATE a schedule by hour of day (inputfit.arrival_rates)",
    "weekly": "give the CREATE a weekly schedule, or model the busy days alone",
    "shifts": "add the shift pattern to the resources as a schedule",
    "batches": "if cases come in batches, a CREATE with a batch size fits them better",
    "overlap": "where the overlapping activities are really parallel, model a SEPARATE and a "
               "MATCH",
}

_IDLE_MODELLED = ("check the idle_wait_* DELAYs against what causes the waits (a calendar, a "
                  "choice, the customer): they are fitted to the log and do not grow with the "
                  "load, as a queue would")


def _next_steps(check, mode, idle_delays: bool = False, rules: int = 0) -> dict:
    """What to check in the draft, and what a log never tells, tailored to this log (D591)."""
    codes = {f["code"] for f in check["findings"]}
    steps = ["run the draft (naga run) and compare its throughput and cycle time with the "
             "report's `observed`: a gap comes from one of the points here",
             "set each resource's real capacity: the draft's is the most the log ever had busy "
             "at once, a lower bound"]
    if rules:
        steps.append(f"run naga check: the draft carries {rules} route rules from the log "
                     "(path_checks, one seed); a failed one is a route the log kept and the "
                     "draft breaks" + (", as first-order routing can" if mode == "markov" else ""))
    steps.append("check the kept variants against the real products or case types"
                 if mode == "variants" else
                 "read each DECIDE's chances: they are the log's frequencies, not the rule "
                 "that chose")
    actions = _ACTIONS | ({"idle_waits": _IDLE_MODELLED} if idle_delays else {})
    steps += [actions[f["code"]] for f in check["findings"]
              if f["code"] in actions and f["level"] != "ok"]
    never = [
        ("shifts: the log shows when work happened, not the shift pattern or the breaks"
         if "shifts" in codes else
         "shifts: the log shows no quiet hours; if the real resources have them, it cannot "
         "tell"),
        "priorities: the log shows the order cases were served in, not the rule (urgent "
        "first, due date) that chose it",
        ("batching: cases arrive together, but the log does not say by what rule (a size, a "
         "timer)" if "batches" in codes else
         "batching: work done in batches looks like single cases ending close together"),
        "decisions by data: a choice made on a case's amount, type or due date reads as a "
        "chance",
        ("parallel work: activities overlap in their case; the log does not say which wait "
         "for each other" if "overlap" in codes else
         "parallel work: none seen; a step that waits for two others looks like a sequence"),
    ]
    return {"check": steps, "never": never}


def _load(source, time_unit, columns, fmt) -> tuple[list, dict]:
    if isinstance(source, (str, Path)):
        info: dict = {}
        return read_log(source, time_unit=time_unit, columns=columns, fmt=fmt, info=info), info
    events = list(source)
    return events, {"format": "events", "rows": len(events), "events": len(events),
                    "dropped": {}}


def check_log(source, *, time_unit: str = "minutes", columns: dict | None = None,
              fmt: str | None = None) -> dict:
    """Is the log good enough? What `discover` would draft from, without drafting: columns
    found, lifecycle coverage, cases, span, censoring, and per finding what it means."""
    _check_time_unit(time_unit)
    events, info = _load(source, time_unit, columns, fmt)
    cases, dropped = _cases(events)
    if not cases:
        raise LogError("no completed activity instance in the log")
    dropped.update(info.get("dropped") or {})
    return _check(cases, info, dropped, time_unit)[0]


# ============================================================ the draft

def discover(source, *, time_unit: str = "minutes", routing: str = "auto",
             columns: dict | None = None, fmt: str | None = None,
             title: str | None = None, idle_waits: bool = False) -> tuple[dict, dict]:
    """A draft model and its report from an event log: *source* is a log's path or a list
    of `Event`s (`log_from_trace`). With *idle_waits*, each activity the check flags as
    waiting long at mostly idle resources gets a DELAY before it, fitted to its waits
    (D630); off, the draft has none. Raises `LogError` for a log refused outright. The log
    is read whole into memory, bounded by the ceilings (D554)."""
    from nagasim.core import schema
    from nagasim.core.document import ModelDocument
    from nagasim.core.layout_tools import auto_layout
    from nagasim.modelfmt import minimal

    _check_time_unit(time_unit)
    if routing not in _ROUTINGS:
        raise LogError("routing must be auto, markov or variants")
    events, info = _load(source, time_unit, columns, fmt)
    if isinstance(source, (str, Path)):
        title = title or f"Draft from {_label(Path(source).name)}"
    cases, dropped = _cases(events)
    if len(cases) < 2:
        raise LogError("fewer than two cases with a completed activity: nothing to fit")
    seen = {x.activity for v in cases.values() for x in v}
    if len(seen) > MAX_ACTIVITIES:
        raise LogError(f"{len(seen)} activities, over the {MAX_ACTIVITIES} ceiling")
    resources_seen = {x.resource for v in cases.values() for x in v if x.resource}
    if len(resources_seen) > MAX_RESOURCES:
        raise LogError(f"{len(resources_seen)} resources, over the {MAX_RESOURCES} ceiling")
    dropped.update(info.get("dropped") or {})
    check, flagged = _check(cases, info, dropped, time_unit)  # before routing drops its own
    assumptions: list[str] = []

    arrivals, arrival_fit = _arrivals(cases, assumptions)
    log_end = max(max(x.end for x in v) for v in cases.values())
    routed = _routed(cases, log_end, dropped, assumptions)
    sequences = [[x.activity for x in v] for v in routed.values()]
    route = _choose_routing(sequences, routing)
    routes = Counter(tuple(seq) for seq in sequences)     # the cases the draft routes (D731)
    if route["mode"] == "variants":
        kept = {v for v, _ in route["kept"]}
        left_out = [c for c, v in routed.items() if tuple(x.activity for x in v) not in kept]
        if left_out:
            dropped["cases of variants left out"] += len(left_out)
        sequences = [list(v) for v, _ in route["kept"]]
        routes = Counter(dict(route["kept"]))
    # An activity no routed case reaches would be a block nothing links to (#570).
    activities = sorted({a for seq in sequences for a in seq})
    if len(activities) < len(seen):
        dropped["activities seen only in cases left out of the routing"] += \
            len(seen) - len(activities)

    samples, overlaps = _samples(cases)
    if overlaps:
        assumptions.append(f"{overlaps} activity instances overlap the previous one in their "
                           "case (parallel work): the draft runs them one after the other")
    if any(s["suspended"] for s in samples.values()):
        assumptions.append("a suspended activity's service time is its start to complete less "
                           "the suspended time; the suspended time itself is reported, not "
                           "modelled, so the draft's cycle times are shorter by it")
    used_blocks = {"arrivals", "first_step", "done", "pick_variant", "route"}
    block_of = {a: _name(a, used_blocks) for a in activities}
    span = log_end - arrivals[0]
    resources, res_report, names_of, used_res = _resources(cases, activities, span)
    if any(r["basis"].endswith("activity") for r in res_report):
        assumptions.append("an activity with no resource in the log gets its own resource, "
                           "capacity its peak of concurrent instances")
    assumptions.append("resources are available all the time: shifts, breaks and failures "
                       "are not inferred")
    act_blocks, resource_sets, act_report = _activity_blocks(
        activities, samples, block_of, names_of, used_res)
    if any(e["lifecycle"] == "complete only" for e in act_report):
        assumptions.append("a complete-only activity is a DELAY of its sojourn (waiting and "
                           "service together, no resource); the first one of a case has no "
                           "observable time")
    gaps_of, transfer = _transfers(cases, block_of)
    if transfer:
        assumptions.append("the time from a completion to the next activity's schedule is a "
                           "transfer: a link delay per transition (first-order), or one "
                           "pooled travel time on the ROUTE (variants)")

    blocks = [{"name": "arrivals", "type": "CREATE",
               "params": {"distribution": arrival_fit["distribution"],
                          "dist_params": arrival_fit["dist_params"]}},
              *act_blocks, {"name": "done", "type": "DISPOSE"}]
    links: list[dict] = []
    sequences_section: list[dict] = []
    if route["mode"] == "markov":
        _wire_markov(sequences, activities, block_of, transfer, used_blocks, blocks, links)
    else:
        sequences_section = _wire_variants(route["kept"], activities, block_of, gaps_of,
                                           used_blocks, blocks, links)
        transfer = {}               # per transition is first-order's; reported as travel
    waits = _idle_wait_delays(flagged, samples, block_of, used_blocks, blocks,
                              links) if idle_waits else []
    if waits:
        assumptions.append("a long wait at a mostly idle resource is a modelled DELAY before "
                           "its activity (idle_wait_*), fitted to the log's waits: it stands "
                           "for a calendar, a choice or the customer, not a queue, so it does "
                           "not grow with the load")

    layout = {"blocks": blocks, "links": links, "resources": resources,
              "info": {"title": _label(title or "Draft from an event log"),
                       "description": "Drafted by naga discover from an event log: arrivals, "
                                      "activity times, routing and resources as the log showed "
                                      "them. Read its report before trusting a number."},
              "simulation": {"time_limit": _tidy(float(span)), "warmup": 0, "replications": 5,
                             "time_unit": time_unit}}
    if resource_sets:
        layout["resource_sets"] = resource_sets
    if sequences_section:
        layout["sequences"] = sequences_section
    doc = ModelDocument(layout)
    doc.move_blocks(auto_layout(doc))
    layout = minimal(doc.to_layout())
    rules, rules_left = _route_rules(routes, block_of)
    if rules:
        layout["path_checks"] = rules
    assumptions.append("the draft starts empty and runs for the log's span with no warm-up; "
                       "the log started wherever the real system was")

    problems = schema.validate_model_structured(schema.migrate(layout))
    report = {
        "source": {**info, "cases": len(cases), "span": span, "time_unit": time_unit},
        "check": check,
        "routing": {k: v for k, v in route.items() if k != "kept"} | {
            "variants_kept": [{"name": f"V{k}", "cases": n,
                               "activities": [block_of[a] for a in v]}
                              for k, (v, n) in enumerate(route["kept"], start=1)]},
        "arrivals": arrival_fit,
        "activities": act_report,
        "resources": res_report,
        "resource_sets": resource_sets,
        "transfers": [{"from": a, "to": b, **spec} for (a, b), spec in sorted(transfer.items())],
        "assumptions": assumptions,
        "dropped": dict(dropped),
        "observed": _kpis(cases),
        "problems": [p.as_dict() for p in problems],
        "next_steps": _next_steps(check, route["mode"], bool(waits), len(rules)),
        "path_checks": {"rules": len(rules), "left_out": rules_left,
                        "from": f"the log's {sum(routes.values())} routed cases"},
    }
    if idle_waits:
        report["idle_waits"] = waits
    return layout, report


# ============================================================ report and CLI

def _num(value) -> str:
    return "-" if value is None else f"{value:.4g}"


def check_text(check: dict, time_unit: str) -> str:
    """The check as a few lines a person reads."""
    cols = ", ".join(f"{role} '{name}'" for role, name in check["columns"].items()) or "-"
    inst = check["instances"]
    lines = [f"check: {check['verdict']}",
             f"  columns: {cols}",
             f"  {check['cases']} cases, {check['events']} events, {check['activities']} "
             f"activities, {check['resources']} resources, from {check['first']} to "
             f"{check['last']} ({_num(check['span'])} {time_unit}; mean cycle time "
             f"{_num(check['mean_cycle_time'])})",
             f"  {inst['total']} activity instances: {inst['with_start']} with a start, "
             f"{inst['with_schedule']} with a schedule, {inst['with_resource']} with a resource"]
    lines += [f"  [{f['level']}] {f['what']}: {f['means']}" for f in check["findings"]]
    return "\n".join(lines)


def report_text(report: dict) -> str:
    """The report as a few lines a person reads."""
    src, obs = report["source"], report["observed"]
    lines = [f"log: {src.get('events')} events, {src['cases']} cases over {_num(src['span'])} "
             f"{src['time_unit']} ({src.get('format')})",
             check_text(report["check"], src["time_unit"]),
             f"observed: throughput {_num(obs['throughput'])} cases per time unit, "
             f"mean cycle time {_num(obs['cycle_time'])}",
             f"routing: {report['routing']['mode']} ({report['routing']['reason']})",
             f"arrivals: {report['arrivals']['distribution']} {report['arrivals']['dist_params']}"]
    for a in report["activities"]:
        fit = a.get("service") or a.get("sojourn")
        lines.append(f"  {a['block']}: {a['count']} times, {a['lifecycle']}, "
                     f"{fit['distribution']} (mean {_num(fit['mean'])}), "
                     f"mean wait {_num(a['mean_wait'])}"
                     + (f", seizes {a['resource']}" if a.get("resource") else ""))
    for r in report["resources"]:
        lines.append(f"  resource {r['name']}: capacity {r['capacity']} (observed peak), "
                     f"utilisation {_num(r['utilisation'])}")
    lines += [f"idle wait: {w['delay']} before {w['block']}, {w['wait']['distribution']} "
              f"(mean {_num(w['wait']['mean'])})" for w in report.get("idle_waits", [])]
    rules = report.get("path_checks") or {}
    if rules.get("rules"):
        lines.append(f"route rules from the log: {rules['rules']} in path_checks, from "
                     f"{rules['from']} (naga check runs them on one seed)")
    lines += [f"assumed: {a}" for a in report["assumptions"]]
    lines += [f"dropped: {n} ({why})" for why, n in report["dropped"].items()]
    lines += [f"{p['severity']}: {p['code']} {p['message']}" for p in report["problems"]]
    lines += ["next steps:", *(f"  - {s}" for s in report["next_steps"]["check"]),
              "what a log cannot tell:", *(f"  - {s}" for s in report["next_steps"]["never"])]
    return "\n".join(lines)


SAMPLES = Path(__file__).resolve().parent / "models" / "data"


def sample_path(value: str) -> str:
    """What the CLI's LOG means: the path given, or, ONLY when nothing is at that path and it
    is a plain name, the bundled sample log of that name (as `examples.model_path` does for
    models). The agent tool never calls this: its path stays confined to the workspace."""
    if not Path(value).exists() and re.fullmatch(r"[A-Za-z0-9_-]+", value) \
            and (SAMPLES / f"{value}.csv").is_file():
        return str(SAMPLES / f"{value}.csv")
    return value


def main(argv=None) -> int:
    """`naga discover LOG [-o model.json] [--report report.json] [--check] [--idle-waits]`."""
    from nagasim._cli import setup_cli
    from nagasim.modelfmt import dumps

    parser = argparse.ArgumentParser(
        prog="naga discover",
        description="Draft a model from an event log (CSV, or XES): arrivals, activity times, "
                    "routing and resources as the log shows them, with a report of what was "
                    "inferred, assumed and dropped.")
    parser.add_argument("log", help="the event log: a CSV file, an .xes file, or the name of "
                                    "a bundled sample (job_shop_log)")
    parser.add_argument("--check", action="store_true",
                        help="only check the log: what it holds and what that means for a "
                             "draft; no model is written")
    parser.add_argument("-o", "--output", help="write the draft model here (default: stdout)")
    parser.add_argument("--report", help="write the report as JSON here")
    parser.add_argument("--format", choices=("csv", "xes"),
                        help="the log's format (default: by the file's suffix)")
    parser.add_argument("--time-unit", default="minutes", choices=_TIME_UNITS,
                        help="the draft's time unit; a numeric timestamp is taken as in it")
    parser.add_argument("--routing", default="auto", choices=("auto", "markov", "variants"),
                        help="markov: chances to the next activity; variants: one routing "
                             "per observed sequence; auto picks by a test on the log")
    parser.add_argument("--idle-waits", action="store_true",
                        help="give each activity that waits long at mostly idle resources a "
                             "DELAY before it, fitted to its waits (off: no such delay)")
    for role in COLUMN_NAMES:
        parser.add_argument(f"--{role}", metavar="COLUMN",
                            help=f"the CSV column holding the {role} (default: found by name)")
    args = parser.parse_args(argv)
    setup_cli()
    columns = {role: getattr(args, role) for role in COLUMN_NAMES if getattr(args, role)}
    log = sample_path(args.log)
    if args.check:
        check = check_log(log, time_unit=args.time_unit, columns=columns, fmt=args.format)
        if args.report:
            Path(args.report).write_text(json.dumps(check, indent=2), encoding="utf-8")
        sys.stdout.write(check_text(check, args.time_unit) + "\n")
        return 0
    layout, report = discover(log, time_unit=args.time_unit, routing=args.routing,
                              columns=columns, fmt=args.format, idle_waits=args.idle_waits)
    text = dumps(layout)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    (sys.stdout if args.output else sys.stderr).write(report_text(report) + "\n")
    return 1 if any(p["severity"] == "error" for p in report["problems"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
