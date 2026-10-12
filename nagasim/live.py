"""What changed since (PHASE7 7.8, D218 to D220): the one contract for live data.

Live data is PULLED (ARCHITECTURE section 6, rule 12). A twin's cycle is `read, simulate,
report`; it asks a `Feed` what changed since a cursor and nothing ever pushes into a run:

    with Feed(layout["data_sources"], network=policy) as feed:
        first = feed.changes("line")                  # a snapshot: the latest value per key
        ...
        later = feed.changes("line", first.cursor)    # what arrived since, in receipt order

* An EVENT asserts a state, never an increment: "this key has this value", so applying one
  twice is harmless and delivery is at-least-once.
* A reader that holds a subscription (mqtt, opcua) runs on ONE daemon thread of the feed and
  answers from the feed's bounded buffer; a reader that can only poll (rest) is read inside
  `changes`, on the caller's thread, no sooner than its poll interval, and its rows are
  diffed into events. Both meet the same contract.
* Every bound has a ceiling in code (`netguard.LIMITS`); what is over one is dropped and
  COUNTED, never grown: `lost` is exact, `gap` says the stream broke and an unknown number
  was missed, and each drop has a reason in `status` and a `W_LIVE_*` problem.
* A feed belongs to whoever runs the cycle and is never pickled. Nothing under
  `nagasim/core/` imports this module or a reader, no reader calls into a run, and there is
  no event bus, streaming layer or callback into the simulation.

Stdlib only. The connection a source names comes from the caller's `NetworkPolicy`; a source
with none is refused (`E_DATA_CONNECTION`) and no socket opens.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import logging
import math
import random
import secrets
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone

from nagasim import netguard, readers
from nagasim.core.problems import Problem, problem
from nagasim.sources import ReadError

__all__ = ["Event", "Changes", "Feed", "stamp", "MAX_TEXT", "SKEW_SECONDS"]

log = logging.getLogger(__name__)

MAX_TEXT = 1024             # bytes of a key, and of a text value: longer is dropped
SKEW_SECONDS = 30.0         # source clock against ours, before W_LIVE_SKEW
MAX_BACKOFF = 60.0
STABLE_SECONDS = 30.0       # a link must stay up this long before its retry wait starts over
_SCALARS = (bool, int, float, str, type(None))


@dataclass(frozen=True)
class Event:
    source: str                 # the data source's name
    key: str                    # topic[/metric], node id, or the row's key
    value: object               # None, bool, int, float, str; a polled row: a dict of those
    source_time: float | None   # seconds since the epoch, UTC, as the source stamped it
    received: float             # the same, by this process's clock, at receipt
    seq: int                    # this feed's count for the source, +1 per event, never reused


@dataclass(frozen=True)
class Changes:
    events: tuple               # the Events since the cursor, in receipt order
    cursor: str                 # opaque: hand it back to get what follows
    lost: int                   # events this feed dropped since the caller's cursor (exact)
    gap: bool                   # the stream broke since the cursor: an unknown number missed
    state: str                  # "live" | "down"
    age: float | None           # seconds since the last event or good poll; None: never
    problems: tuple             # the Problems of this answer (W_LIVE_*, or why it is down)


def stamp(value) -> float | None:
    """Seconds since the epoch for a source's own time stamp: a number is taken as seconds,
    a text as ISO 8601 (a stamp with no offset is read as UTC). Anything else is None: a
    stamp is data, and a bad one is no reason to drop the value it came with."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if abs(value) <= sys.float_info.max else None  # NaN, inf, 10**400
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def _data(value) -> bool:
    """Whether *value* is something an event may carry: a scalar, or one row of scalars."""
    if isinstance(value, dict):
        return all(isinstance(k, str) and isinstance(v, _SCALARS) and _fits(v) and _finite(v)
                   for k, v in value.items())
    return isinstance(value, _SCALARS) and _finite(value)


def _finite(value) -> bool:
    """NaN and the infinities are not data: they would reach a float target of the model and
    a record file as invalid JSON."""
    return not isinstance(value, float) or math.isfinite(value)


def _fits(value) -> bool:
    return not isinstance(value, str) or len(value.encode("utf-8", "replace")) <= MAX_TEXT


class _Source:
    """One live source's connection, buffer and counters. Guarded by the feed's lock."""

    def __init__(self, spec: dict, conn: netguard.Connection, module, options: dict):
        self.spec, self.conn, self.module, self.options = spec, conn, module, options
        self.name = spec.get("name") or ""
        self.events: deque = deque()
        self.latest: dict[str, Event] = {}
        self.table: list[dict] | None = None        # a polled source's last good rows
        self.seq = self.evicted = self.gaps = 0
        self.dropped: Counter = Counter()
        self.state, self.error = "down", None
        self.last_good: float | None = None         # monotonic
        self.skew: float | None = None
        self.thread: threading.Thread | None = None
        self.up_since: float | None = None          # monotonic, when the link last came up
        self.next_poll = self.backoff = 0.0
        self.second = self.in_second = 0
        self.warning: tuple[str, str] | None = None     # (message, hint) a reader reported


class _Sink:
    """What a subscribing reader is handed: the only way a value enters a feed."""

    def __init__(self, feed: "Feed", src: _Source):
        self._feed, self._src = feed, src

    def connected(self) -> None:
        with self._feed._cond:
            self._src.state, self._src.error = "live", None
            self._src.last_good = self._src.up_since = self._feed._monotonic()

    def accept(self, key: str, value, source_time: float | None = None) -> None:
        self._feed._accept(self._src, key, value, source_time, paced=True)

    def count(self, reason: str, n: int = 1) -> None:
        with self._feed._cond:
            self._src.dropped[reason] += n

    def gap(self) -> None:
        with self._feed._cond:
            self._src.gaps += 1

    def warn(self, message: str, hint: str) -> None:
        """A condition that does not stop reading (a reader's one standing warning)."""
        with self._feed._cond:
            self._src.warning = (message, hint)


class Feed:
    """The live sources of one model, read for one owner of a cycle. A context manager."""

    def __init__(self, sources, *, network: netguard.NetworkPolicy | None = None,
                 clock=time.time, monotonic=time.monotonic):
        self.epoch = secrets.token_hex(4)
        self._policy = network or netguard.NetworkPolicy()
        self._specs = {s.get("name"): s for s in sources or []}
        self._sources: dict[str, _Source] = {}
        self._clock, self._monotonic = clock, monotonic
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._arrived = self._seen = 0

    def __enter__(self) -> "Feed":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __reduce__(self):
        raise TypeError("a feed holds connections and is never pickled")

    # ------------------------------------------------------------ opening
    def _open(self, name: str) -> _Source:
        """The source, connected on first use. Refusals come from here, before any socket:
        the caller's connection (`netguard.connection_for`), then the reader's own `check`."""
        with self._cond:
            src = self._sources.get(name)
        if src is not None:
            return src
        spec = self._specs.get(name)
        if spec is None or spec.get("kind") not in readers.KINDS:
            raise ReadError("E_DATA_SOURCE_KIND", f"'{name}' is not a live source of this model",
                            f"only these kinds answer what changed: "
                            f"{', '.join(readers.KINDS) or '(none)'}")
        conn = netguard.connection_for(spec, self._policy)
        module = readers.load(spec["kind"])
        src = _Source(spec, conn, module, module.check(spec, conn))
        with self._cond:
            if name in self._sources:               # another caller opened it meanwhile
                return self._sources[name]
            self._sources[name] = src
            if hasattr(module, "subscribe"):
                src.thread = threading.Thread(target=self._run, args=(src,), daemon=True,
                                              name=f"naga-feed-{name}")
                src.thread.start()
        return src

    # ------------------------------------------------------------ the one way in
    def _accept(self, src: _Source, key, value, source_time, paced: bool = False) -> None:
        now = self._clock()
        with self._cond:
            if paced:
                # Above the rate the reader's thread stops reading until the next second,
                # so the source's own slow-client rule applies: nothing is dropped here.
                # Counted BEFORE anything is judged: a flood of values that are all dropped
                # is paced like one that is kept.
                if int(now) != src.second:
                    src.second, src.in_second = int(now), 0
                src.in_second += 1
                wait = int(now) + 1 - now if src.in_second > src.conn.limit("rate") else 0.0
        if paced and wait and self._stop.wait(wait):
            return None
        if not isinstance(key, str) or not key:
            return self._count(src, "key")
        if not _fits(key) or not _fits(value):
            return self._count(src, "oversize")
        if not _data(value):
            return self._count(src, "type")
        if not isinstance(source_time, (int, float)) or isinstance(source_time, bool) \
                or not abs(source_time) < math.inf:     # an int too large for a float too (#373)
            source_time = None
        with self._cond:
            last = src.latest.get(key)
            if last is not None and source_time is not None \
                    and last.source_time == source_time \
                    and (type(last.value), last.value) == (type(value), value):
                src.dropped["duplicate"] += 1
                return None
            if last is None and len(src.latest) >= src.conn.limit("keys"):
                src.dropped["keys"] += 1
                return None
            src.seq += 1
            event = Event(src.name, key, value, source_time, self._clock(), src.seq)
            src.events.append(event)
            if len(src.events) > src.conn.limit("events"):
                src.events.popleft()
                src.evicted += 1
            # `rows` keeps, per key, the event with the newer source stamp; the buffer keeps
            # receipt order and reorders nothing.
            if last is None or source_time is None or last.source_time is None \
                    or source_time >= last.source_time:
                src.latest[key] = event
            if source_time is not None:
                src.skew = event.received - source_time
            src.state, src.error, src.last_good = "live", None, self._monotonic()
            self._arrived += 1
            self._cond.notify_all()
        return None

    def _count(self, src: _Source, reason: str) -> None:
        with self._cond:
            src.dropped[reason] += 1

    # ------------------------------------------------------------ a subscription's thread
    def _run(self, src: _Source) -> None:
        sink, backoff = _Sink(self, src), 1.0
        while not self._stop.is_set():
            try:
                src.module.subscribe(src.spec, src.conn, src.options, sink, self._stop)
                failure = ReadError("E_DATA_READ", f"connection '{src.conn.name}' was closed",
                                    "the feed connects again by itself")
            except (ReadError, netguard.GuardError) as exc:
                failure = exc
            except Exception as exc:        # a reader's own failure mode
                # The class only: the text may be whatever the other end sent.
                failure = ReadError("E_DATA_READ", f"connection '{src.conn.name}' failed "
                                    f"({type(exc).__name__})", "the feed connects again by itself")
            if self._stop.is_set():
                break
            with self._cond:
                was_live = src.state == "live"
                stable = was_live and src.up_since is not None \
                    and self._monotonic() - src.up_since >= STABLE_SECONDS
                src.state, src.error, src.up_since = "down", failure, None
                if was_live:
                    src.gaps += 1           # whatever was sent while we are away is missed
                self._cond.notify_all()
            # 1 s, then 2, 4 ... up to a minute. The wait starts over only after a link that
            # STAYED up: one that is accepted and dropped at once is not sent a CONNECT (with
            # its password) every second for ever.
            if stable:
                backoff = 1.0
            delay, backoff = backoff, min(MAX_BACKOFF, backoff * 2)
            log.warning("live source '%s': %s", src.name, failure.message)
            self._stop.wait(delay * (1 + 0.25 * random.random()))

    # ------------------------------------------------------------ a poller, on the caller's thread
    def _poll(self, src: _Source) -> None:
        now = self._monotonic()
        with self._cond:
            if now < src.next_poll:
                return
            src.next_poll = now + src.options.get("poll", 5.0)     # also keeps a second caller out
        try:
            rows = src.module.read(src.spec, src.conn, src.options)
        except (ReadError, netguard.GuardError) as exc:
            failure = exc
        except Exception as exc:            # a reader's own failure mode
            failure = ReadError("E_DATA_READ", f"connection '{src.conn.name}' failed "
                                f"({type(exc).__name__})", "check the connection and the source")
        else:
            failure = None
        if failure is not None:
            with self._cond:
                # The last good rows are kept; the next poll waits 1 s doubling to 60 s.
                src.state, src.error = "down", failure
                src.backoff = min(MAX_BACKOFF, max(1.0, src.backoff * 2))
                src.next_poll = now + src.backoff
            return
        key_field, time_field = src.options.get("key"), src.options.get("time")
        keyed, kept = {}, []
        for index, row in enumerate(rows):
            key = str(row.get(key_field, "")) if key_field else str(index)
            if not _fits(key) or not all(_fits(cell) for cell in row.values()):
                # Over the bound of a text: in neither the events nor the rows a binding reads.
                self._count(src, "oversize")
                continue
            keyed[key] = row
            kept.append(row)
        rows = kept
        with self._cond:
            before = {key: event.value for key, event in src.latest.items()}
            src.table, src.backoff = rows, 0.0
            src.state, src.error, src.last_good = "live", None, self._monotonic()
        for key, row in keyed.items():
            if before.get(key) != row:      # a new or a changed row is an event
                self._accept(src, key, row, stamp(row.get(time_field)) if time_field else None)
        for key in before.keys() - keyed.keys():
            if before[key] is not None:     # a row that is gone: its key now holds nothing
                self._accept(src, key, None, None)

    # ------------------------------------------------------------ the contract
    def changes(self, source: str, cursor: str | None = None, limit: int = 1000) -> Changes:
        """What *source* said since *cursor*, at most *limit* events, in receipt order.

        With no cursor, or one this feed did not give (another process, a restart: then
        `gap` is set), the answer is a SNAPSHOT: the latest event of every key, whatever
        *limit* says, and the cursor to continue from. Several callers may read one feed,
        each with its own cursor."""
        src = self._open(source)
        if src.thread is None:
            self._poll(src)
        with self._cond:
            # "<epoch>:<source>:<seq>:<gaps>". Whatever is not exactly that, for THIS source
            # of THIS feed, is an unknown cursor: a snapshot with `gap`, never an error.
            # A cursor comes back from outside (a cycle's JSON, a tool): any type, any length.
            epoch, _, rest = (cursor if isinstance(cursor, str) else "").partition(":")
            parts = rest.rsplit(":", 2)
            known = epoch == self.epoch and len(parts) == 3 and parts[0] == src.name \
                and all(part.isascii() and part.isdigit() and len(part) <= 18
                        for part in parts[1:]) \
                and int(parts[1]) <= src.seq
            if not known:
                events = sorted(src.latest.values(), key=lambda event: event.seq)
                after, lost, gap = src.seq, 0, cursor is not None
            else:
                since = int(parts[1])
                first = src.events[0].seq if src.events else src.seq + 1
                lost = max(0, first - 1 - since)
                events = [event for event in src.events if event.seq > since][:max(1, limit)]
                after = events[-1].seq if events else src.seq
                gap = src.gaps > int(parts[2])
            return Changes(tuple(events), f"{self.epoch}:{src.name}:{after}:{src.gaps}", lost, gap,
                           src.state, self._age(src), tuple(self._problems(src, lost, gap)))

    def rows(self, source: str, settle: bool = False) -> tuple[list[dict], str | None]:
        """(the latest values, their stamp): a polled source's last good rows as they came;
        a subscription's latest event per key as `{"key", "value", "source_time"}` rows.
        A source that is down but has values still answers; one with none is a coded
        failure. *settle*: first give a subscription its `settle` seconds (2, at most 10)
        to deliver what the other end holds: for a feed opened for one read."""
        src = self._open(source)
        if src.thread is None:
            self._poll(src)
        elif settle:
            self._stop.wait(src.options.get("settle", 2.0))
        with self._cond:
            if src.table is not None:
                rows = [dict(row) for row in src.table]
            else:
                rows = [{"key": e.key, "value": e.value, "source_time": e.source_time}
                        for e in sorted(src.latest.values(), key=lambda event: event.key)]
            if src.last_good is None:
                failure = src.error or ReadError(
                    "E_DATA_READ", f"source '{source}' has sent no value yet",
                    "check the source's location against what the connection publishes")
                # A fresh copy each time: the stored one must not grow a traceback per caller.
                if isinstance(failure, ReadError):
                    raise ReadError(failure.code, failure.message, failure.hint, failure.status)
                raise type(failure)(failure.code, failure.message, failure.hint)
            newest = self._clock() - (self._monotonic() - src.last_good)
            return rows, datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")

    def wait(self, timeout: float) -> bool:
        """Sleep up to *timeout* seconds; True as soon as something arrived since the last
        call. A cycle's sleep: a feed of pollers only just sleeps."""
        with self._cond:
            arrived = self._cond.wait_for(
                lambda: self._arrived > self._seen or self._stop.is_set(), timeout)
            self._seen = self._arrived
            return bool(arrived) and not self._stop.is_set()

    def status(self, source: str) -> dict:
        """Counters for a monitor. Plain data. Asking opens nothing: a source nobody has
        read yet answers `{"source", "state": "unopened"}`."""
        with self._cond:
            src = self._sources.get(source)
        if src is None:
            if source not in self._specs or self._specs[source].get("kind") not in readers.KINDS:
                raise ReadError("E_DATA_SOURCE_KIND", f"'{source}' is not a live source of this model",
                                f"live kinds: {', '.join(readers.KINDS) or '(none)'}")
            return {"source": source, "state": "unopened"}
        with self._cond:
            return {"source": src.name, "connection": src.conn.name,
                    "channel": "tls" if src.conn.encrypted else "plain",
                    "state": src.state, "age": self._age(src), "seq": src.seq,
                    "buffered": len(src.events), "keys": len(src.latest),
                    "lost": src.evicted, "gaps": src.gaps, "dropped": dict(src.dropped),
                    "skew": src.skew, "problem": src.error.message if src.error else None}

    def close(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
            threads = [src.thread for src in self._sources.values() if src.thread]
        for thread in threads:
            thread.join(2.0)

    # ------------------------------------------------------------ helpers
    def _age(self, src: _Source) -> float | None:
        return None if src.last_good is None else max(0.0, self._monotonic() - src.last_good)

    def _problems(self, src: _Source, lost: int, gap: bool) -> list[Problem]:
        at, found = f"/data_sources/{src.name}", []
        if src.error is not None:
            found.append(problem(src.error.code, f"source '{src.name}': {src.error.message}", at,
                                 hint=src.error.hint))
        if lost:
            found.append(problem("W_LIVE_LOST", f"source '{src.name}': {lost} events were "
                                 f"dropped before they were read", at,
                                 hint="read more often, or raise the connection's \"events\" limit"))
        if gap:
            found.append(problem("W_LIVE_GAP", f"source '{src.name}': the stream broke, so an "
                                 f"unknown number of events is missing", at,
                                 hint="the latest values are right again; the history has a hole"))
        if src.skew is not None and abs(src.skew) > SKEW_SECONDS:
            found.append(problem("W_LIVE_SKEW", f"source '{src.name}': its clock and this "
                                 f"machine's differ by {src.skew:.0f} s", at,
                                 hint="synchronise the clocks; both stamps travel with every event"))
        if src.warning:
            found.append(problem("W_LIVE_LIMIT", src.warning[0], at, hint=src.warning[1]))
        if src.dropped:
            counts = ", ".join(f"{n} {reason}" for reason, n in sorted(src.dropped.items()))
            found.append(problem("W_LIVE_DROPPED", f"source '{src.name}' dropped since the feed "
                                 f"opened: {counts}", at,
                                 hint="see the bounds of live data in the documentation"))
        return found


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    """`naga watch MODEL --connections FILE [--source NAME] [--for SECONDS] [--record FILE]
    [--check]`: print what a model's live sources send, as it arrives."""
    import argparse
    import json
    from pathlib import Path

    from nagasim import api
    from nagasim._cli import setup_cli

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga watch",
        description="Print what a model's live data sources (rest, mqtt, opcua) send, as it "
                    "arrives, with a summary per source. Read-only: nothing is sent to a plant.")
    parser.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    parser.add_argument("--connections", required=True, metavar="FILE",
                        help="your connections file: where each connection a source names may "
                             "connect, and with which credential")
    parser.add_argument("--source", action="append", default=None, metavar="NAME",
                        help="watch only this source; may repeat (default: every live source)")
    parser.add_argument("--for", dest="seconds", type=float, default=30.0, metavar="SECONDS",
                        help="how long to watch (default 30; 0: until Ctrl+C)")
    parser.add_argument("--record", default=None, metavar="FILE",
                        help="also write every event as one JSON object per line, with a "
                             "{\"lost\": n} or {\"gap\": true} line where the log has a hole")
    parser.add_argument("--check", action="store_true",
                        help="print each connection the model uses, connect once and stop")
    args = parser.parse_args(argv)

    connections = netguard.load_connections(args.connections)
    layout = api.load_layout(Path(args.model))
    specs = [s for s in layout.get("data_sources", []) if s.get("kind") in readers.KINDS
             and (not args.source or s.get("name") in args.source)]
    if not specs:
        print("no live data source to watch")
        return 1
    for name in sorted({s.get("connection") for s in specs} & set(connections)):
        print(netguard.describe_connection(connections[name]))

    record = open(args.record, "w", encoding="utf-8") if args.record else None
    cursors: dict[str, str | None] = {s["name"]: None for s in specs}
    totals: Counter = Counter()
    failed = False
    try:
        with Feed(specs, network=netguard.NetworkPolicy(connections=connections)) as feed:
            deadline = time.monotonic() + (5.0 if args.check else args.seconds or float("inf"))
            while True:
                for name in cursors:
                    try:
                        got = feed.changes(name, cursors[name])
                    except (ReadError, netguard.GuardError) as exc:
                        print(f"{name}: {exc.code}: {exc.message}")
                        failed = True
                        continue
                    cursors[name] = got.cursor
                    totals[name] += len(got.events)
                    for event in got.events:
                        # Both as data (`!r`): a key or a value from a plant may hold an
                        # escape sequence, and this goes to somebody's terminal.
                        print(f"{name}  {event.key!r} = {event.value!r}")
                    if record is not None:
                        for hole in ([{"source": name, "lost": got.lost}] if got.lost else []) \
                                + ([{"source": name, "gap": True}] if got.gap else []):
                            record.write(json.dumps(hole) + "\n")
                        for event in got.events:
                            record.write(json.dumps(vars(event)) + "\n")
                answered = [status for status in map(feed.status, cursors)
                            if status["state"] != "unopened"]
                if failed or time.monotonic() >= deadline or (args.check and all(
                        status["state"] == "live" or status["problem"] for status in answered)):
                    break
                feed.wait(min(1.0, max(0.0, deadline - time.monotonic())))
            for name in cursors:
                status = feed.status(name)
                if status["state"] == "unopened":
                    continue
                print(f"{name}: {status['state']}, {totals[name]} events, {status['lost']} lost, "
                      f"{status['gaps']} gaps, channel {status['channel']}"
                      + (f"; {status['problem']}" if status["problem"] else ""))
                failed = failed or status["state"] != "live"
    except KeyboardInterrupt:
        pass
    finally:
        if record is not None:
            record.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
