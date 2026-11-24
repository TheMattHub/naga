""" Set of utility functions for the simulation """

from __future__ import annotations

import hashlib
import json
import logging
import sys

from array import array
from dataclasses import dataclass
from heapq import heappush

from simpy.events import NORMAL

log = logging.getLogger(__name__)

__all__ = ["ResourceSnapshot", "ResourceLog", "StoreSnapshot", "timeout_at", "is_number", "is_finite_number",
           "canonical_json", "cache_key", "sha256_text"]

# Last-used paths are stored via QSettings in the Qt UI (product/ui/main_window.py),
# not here -- this file used to hold a JSON-file equivalent for the original editor.


# -------- shared validation predicate --------
def is_number(value) -> bool:
    """True for a plain int or float, excluding bool (a bool IS an int in
    Python, but a checkbox value should never pass a numeric-field check).
    C3.5: folds five byte-identical `_is_number` copies (core/conversions.py,
    core/levels.py, core/orders.py, core/tables.py, core/timeseries_inputs.py)
    plus core/initial_state.py's identically-bodied `_number`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_number(value) -> bool:
    """`is_number` and finite: no NaN, no infinity, and an int a float can hold
    (`math.isfinite` raises on a larger one, #373). The one copy of what a
    dozen modules spelled out for a value read from a file."""
    return is_number(value) and abs(value) <= sys.float_info.max


def canonical_json(value) -> str:
    """*value* as the one JSON text two equal values share (sorted keys, no spaces,
    no NaN): what a hash is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cache_key(value) -> str:
    """`canonical_json` for an in-memory cache key: a NaN, or a value JSON cannot hold (as
    its str), is keyed rather than refused, because a model file may carry either. Never
    persisted, so it need not be a hash's input."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    """The sha256 of the bytes of the file at *path* (OSError when it cannot be read)."""
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()

# -------- generic log structures --------
@dataclass(slots=True)
class ResourceSnapshot:      # LoggedSimpyResource.log
    time: float
    in_use: int
    queue_length: int
    event_type: str | None   = None

class ResourceLog:
    """A resource's usage log, packed (D1061): one row per request/seize/release
    in four parallel arrays (17 B a row) instead of a `ResourceSnapshot` object
    and its float (about 100 B). Reads as the deque of snapshots it replaces:
    `len`, iteration and indexing give `ResourceSnapshot`s, oldest first, and
    once `maxlen` rows are held each new one drops the oldest.

    The event-type code also records whether the time was an int, so a model
    on an integer clock gets back the very `int` it logged (the frame's dtype
    depends on it)."""

    __slots__ = ("maxlen", "_time", "_in_use", "_queue", "_code", "_codes", "_kinds", "_head")

    def __init__(self, maxlen: int | None = None):
        self.maxlen = maxlen
        self._time = array("d")
        self._in_use = array("i")
        self._queue = array("i")
        self._code = array("B")
        self._codes = ({}, {})    # [time is int][event_type] -> code
        self._kinds = []          # code -> (event_type, time is int)
        self._head = 0            # index of the oldest row once the log is full

    def add(self, time, in_use: int, queue_length: int, event_type=None) -> bool:
        """Log one row. Lean on purpose (#1561): this runs on every request,
        seize and release. True when the log was full (`maxlen` rows), so the row
        replaced the oldest: what a caller's truncation flag reads (speed review R9)."""
        is_int = type(time) is int
        codes = self._codes[is_int]
        code = codes[event_type] if event_type in codes else None      # no `get` call (R9)
        if code is None:
            code = codes[event_type] = len(self._kinds)
            self._kinds.append((event_type, is_int))
        times = self._time
        maxlen = self.maxlen
        if maxlen is None or len(times) < maxlen:
            try:
                times.append(time)
                self._in_use.append(in_use)
                self._queue.append(queue_length)
                self._code.append(code)
            except BaseException:
                # A value the typed arrays refuse (a float in_use) raises mid-row: the
                # shortest array is the row count before it, so cut every array back
                # to it and the log stays readable (#1560).
                arrays = (times, self._in_use, self._queue, self._code)
                n = min(len(a) for a in arrays)
                for a in arrays:
                    del a[n:]
                raise
            return False
        if not maxlen:
            return False
        i = self._head              # full: overwrite the oldest row
        old = times[i], self._in_use[i], self._queue[i], self._code[i]
        try:
            times[i], self._in_use[i], self._queue[i], self._code[i] = time, in_use, queue_length, code
        except BaseException:
            times[i], self._in_use[i], self._queue[i], self._code[i] = old
            raise
        self._head = (i + 1) % maxlen
        return True

    def append(self, snapshot: ResourceSnapshot) -> None:
        self.add(snapshot.time, snapshot.in_use, snapshot.queue_length, snapshot.event_type)

    def _row(self, i: int) -> ResourceSnapshot:
        event_type, is_int = self._kinds[self._code[i]]
        t = self._time[i]
        return ResourceSnapshot(int(t) if is_int else t, self._in_use[i], self._queue[i], event_type)

    def __len__(self) -> int:
        return len(self._time)

    def __iter__(self):
        # The wrap and the int-time handling live in `columns` only; `map` builds the
        # snapshots with no generator frame per row (#1561).
        return map(ResourceSnapshot, *self.columns())

    def columns(self) -> tuple[list, list, list, list]:
        """The log as four lists (times, in use, queue lengths, event types),
        oldest row first, each value what a snapshot would carry: the result
        readers take these instead of building a snapshot per row (#1561)."""
        columns = (self._time, self._in_use, self._queue, self._code)
        head = self._head
        if head:                    # full and wrapped: oldest row first
            columns = tuple(c[head:] + c[:head] for c in columns)
        times, in_use, queue, codes = (c.tolist() for c in columns)
        kinds = self._kinds
        if any(is_int for _event_type, is_int in kinds):
            times = [int(t) if kinds[code][1] else t for t, code in zip(times, codes)]
        event_types = [event_type for event_type, _is_int in kinds]
        return times, in_use, queue, list(map(event_types.__getitem__, codes))

    def __getitem__(self, index: int) -> ResourceSnapshot:
        n = len(self._time)
        if not -n <= index < n:
            raise IndexError("ResourceLog index out of range")
        return self._row((self._head + index % n) % n)


@dataclass(slots=True)
class StoreSnapshot:         # LoggedFilterStore.log
    time: float
    items: int
    event_type: str | None   = None


def timeout_at(env, t: float):
    """An event firing EXACTLY at *t*, as a SimPy timeout would: the agent
    system (CRN) and CREATE's thinned arrivals both need the exact
    instant. SimPy's own `timeout` adds a delay to now, and for some pairs no
    delay makes that sum land on *t* (DEVLOG #341), so this pushes the event on
    SimPy's heap at the absolute time instead: the one place the engine writes
    SimPy internals (D207; `tests/regressions/` pins the behaviour). A
    *t* already past fires in the current instant; the clock never runs back."""
    event = env.event()
    event._ok = True
    event._value = None
    heappush(env._queue, (max(t, env.now), NORMAL, next(env._eid), event))
    return event
