"""`nagasim.live.Feed`: what changed since a cursor (PHASE7 7.8, D218 to D220).

The contract, against two readers written here: one that polls a scripted table and one
that holds a scripted subscription. No socket is opened anywhere in this file; the real
readers have their own tests (`tests/test_readers.py`).
"""
import ast
import queue
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from nagasim import live, netguard, readers
from nagasim.sources import ReadError

ROOT = Path(__file__).resolve().parents[1]


def _conn(kind, **fields) -> netguard.Connection:
    return netguard.Connection(name="line", kind=kind, scheme="mqtts", host="plant.test",
                               port=8883, **fields)


class _Clock:
    """A clock a test moves by hand: the feed's wall clock and its monotonic one."""

    def __init__(self, now=20.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def scripted(monkeypatch):
    """Two fake kinds, `polled` and `pushed`, and a feed over them. `polled` answers
    `plant.table` (or raises `plant.failure`); `pushed` plays what a test puts on
    `plant.script`: ("up",), ("value", key, value, time), ("count", reason), ("drop",)."""
    plant = types.SimpleNamespace(table=[], failure=None, script=queue.Queue(), links=0,
                                  checked=[])

    def check(source, conn):
        plant.checked.append(source["name"])
        return {"key": "id", "time": "at", "poll": 5.0}

    def read(source, conn, options):
        if plant.failure is not None:
            raise plant.failure
        return [dict(row) for row in plant.table]

    def subscribe(source, conn, options, sink, stop):
        plant.links += 1
        while not stop.is_set():
            try:
                step = plant.script.get(timeout=0.01)
            except queue.Empty:
                continue
            if step[0] == "up":
                sink.connected()
            elif step[0] == "value":
                sink.accept(*step[1:])
            elif step[0] == "count":
                sink.count(step[1])
            elif step[0] == "drop":
                raise ReadError("E_DATA_READ", "connection 'line' was dropped", "it reconnects")

    modules = {"polled": types.SimpleNamespace(check=check, read=read),
               "pushed": types.SimpleNamespace(check=check, subscribe=subscribe)}
    monkeypatch.setattr(readers, "KINDS", ("polled", "pushed"))
    monkeypatch.setattr(readers, "load", lambda kind: modules[kind])
    plant.clock = _Clock()
    plant.sources = [{"name": "mes", "kind": "polled", "connection": "mes", "location": "orders"},
                     {"name": "line", "kind": "pushed", "connection": "line", "location": "a/#"},
                     {"name": "file", "kind": "csv", "location": "orders.csv"}]
    plant.policy = netguard.NetworkPolicy(connections={
        "mes": _conn("polled"), "line": _conn("pushed", limits=(("events", 5), ("keys", 3),
                                                                 ("rate", 1000)))})
    plant.feeds = []

    def feed(**kwargs):
        made = live.Feed(plant.sources, network=plant.policy, clock=plant.clock,
                         monotonic=plant.clock, **kwargs)
        plant.feeds.append(made)
        return made

    plant.feed = feed
    yield plant
    for made in plant.feeds:
        made.close()


def _play(plant, feed, *steps, source="line"):
    """Put *steps* on the script and wait until the reader has taken them all."""
    feed._open(source)
    for step in steps:
        plant.script.put(step)
    deadline = threading.Event()
    for _ in range(400):
        if plant.script.empty():
            break
        deadline.wait(0.005)
    deadline.wait(0.03)             # the last step is taken; let it land
    assert plant.script.empty()


def _seen(changes):
    return [(e.key, e.value) for e in changes.events]


# ---------------------------------------------------------------- the cursor
def test_no_cursor_is_a_snapshot_and_a_cursor_gives_what_followed(scripted):
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", 1, 10.0), ("value", "b", 2, 10.0),
          ("value", "a", 3, 11.0))
    first = feed.changes("line")
    assert _seen(first) == [("b", 2), ("a", 3)]         # the latest of every key, in order
    assert (first.lost, first.gap, first.state, first.problems) == (0, False, "live", ())
    assert isinstance(first.cursor, str) and feed.changes("line", first.cursor).events == ()

    _play(scripted, feed, ("value", "b", 5, 12.0), ("value", "c", True, None))
    later = feed.changes("line", first.cursor)
    assert _seen(later) == [("b", 5), ("c", True)]
    event = later.events[0]
    assert (event.source, event.source_time, event.received, event.seq) == ("line", 12.0, 20.0, 4)
    assert feed.changes("line", later.cursor).events == ()
    # Several callers read one feed, each with its own cursor; a limit continues exactly.
    assert _seen(feed.changes("line", first.cursor)) == [("b", 5), ("c", True)]
    one = feed.changes("line", first.cursor, limit=1)
    assert _seen(one) == [("b", 5)] and _seen(feed.changes("line", one.cursor)) == [("c", True)]


def test_an_overflow_is_an_exact_count_and_the_latest_values_stay_right(scripted):
    feed = scripted.feed()                              # this connection buffers 5 events
    _play(scripted, feed, ("up",), ("value", "a", 0, 1.0))
    start = feed.changes("line").cursor
    _play(scripted, feed, *[("value", "a", n, 1.0 + n) for n in range(1, 9)])
    after = feed.changes("line", start)
    assert after.lost == 3 and [e.value for e in after.events] == [4, 5, 6, 7, 8]
    assert [p.code for p in after.problems] == ["W_LIVE_LOST"]
    assert feed.rows("line")[0] == [{"key": "a", "value": 8, "source_time": 9.0}]
    assert feed.changes("line", after.cursor).lost == 0
    assert feed.status("line")["lost"] == 4 and feed.status("line")["buffered"] == 5


def test_a_reconnect_and_a_restart_are_gaps_not_silence(scripted):
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", 1, 1.0))
    cursor = feed.changes("line").cursor
    _play(scripted, feed, ("drop",))
    down = feed.changes("line", cursor)
    assert (down.state, down.gap, down.events) == ("down", True, ())
    assert [p.code for p in down.problems] == ["E_DATA_READ", "W_LIVE_GAP"]
    assert feed.rows("line")[0][0]["value"] == 1        # down, but it still has its values
    assert feed.changes("line", down.cursor).gap is False       # a gap is reported once

    scripted.clock.now += 5
    feed._stop.wait(1.5)                                # the first retry comes after about 1 s
    _play(scripted, feed, ("up",), ("value", "a", 2, 2.0))
    back = feed.changes("line", down.cursor)
    assert (back.state, back.gap, _seen(back), scripted.links) == ("live", False, [("a", 2)], 2)

    # A cursor from another feed (a restart of the process): a snapshot, flagged.
    feed.close()
    other = scripted.feed()
    _play(scripted, other, ("up",), ("value", "z", 9, 3.0))
    stale = other.changes("line", back.cursor)
    assert stale.gap is True and _seen(stale) == [("z", 9)]
    for junk in ("", "nonsense", f"{other.epoch}:line:999:0", f"{other.epoch}:line:x:0"):
        assert other.changes("line", junk).gap is True


# ---------------------------------------------------------------- delivery
def test_duplicates_order_and_what_is_not_data(scripted):
    feed = scripted.feed()                              # this connection keeps 3 keys
    _play(scripted, feed, ("up",),
          ("value", "a", 1, 10.0), ("value", "a", 1, 10.0),      # equal value and stamp
          ("value", "a", 1, None), ("value", "a", 1, None),      # no stamp: delivered again
          ("value", "a", True, 10.0),                            # True is not 1
          ("value", "b", 7, 20.0), ("value", "b", 6, 15.0),      # late: kept in receipt order
          ("value", "c", "x" * 1025, 1.0), ("value", "k" * 1025, 1, 1.0),
          ("value", "c", [1, 2], 1.0), ("value", "c", {"f": {"nested": 1}}, 1.0),
          ("value", "", 1, 1.0), ("value", "c", 1.5, "not a time"),
          ("value", "d", 1, 1.0),                                # a fourth key: over the cap
          ("count", "payload"))
    got = feed.changes("line", f"{feed.epoch}:line:0:0")
    assert _seen(got) == [("a", 1), ("a", 1), ("a", 1), ("a", True), ("b", 7), ("b", 6), ("c", 1.5)][-5:]
    assert got.lost == 2 and got.events[-1].source_time is None
    assert feed.status("line")["dropped"] == {"duplicate": 1, "oversize": 2, "type": 2, "key": 1,
                                              "keys": 1, "payload": 1}
    assert [p.code for p in got.problems] == ["W_LIVE_LOST", "W_LIVE_DROPPED"]
    assert "2 oversize" in got.problems[1].message
    # `rows` keeps, per key, the event with the newer source stamp.
    assert {r["key"]: r["value"] for r in feed.rows("line")[0]} == {"a": True, "b": 7, "c": 1.5}


def test_above_the_rate_the_reader_waits_instead_of_dropping(scripted):
    scripted.policy = netguard.NetworkPolicy(connections={
        "line": _conn("pushed", limits=(("rate", 2),))})
    feed = scripted.feed()
    waits = []
    feed._stop = types.SimpleNamespace(is_set=lambda: False, set=lambda: None,
                                       wait=lambda seconds: waits.append(seconds) and False)
    scripted.clock.now = 1000.25
    _play(scripted, feed, ("up",), *[("value", "a", n, float(n)) for n in range(4)])
    assert waits == [0.75, 0.75] and feed.status("line")["seq"] == 4
    assert feed.status("line")["dropped"] == {}


def test_a_clock_that_disagrees_is_reported_never_acted_on(scripted):
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", 1, scripted.clock.now - 45))
    got = feed.changes("line")
    assert [p.code for p in got.problems] == ["W_LIVE_SKEW"] and _seen(got) == [("a", 1)]
    assert feed.status("line")["skew"] == 45.0


def test_a_value_is_returned_within_two_seconds_without_sleeping(scripted):
    """The exit check of the package: an event is seen within seconds. `wait` is the
    cycle's sleep and returns as soon as something arrives."""
    import time

    feed = scripted.feed()
    _play(scripted, feed, ("up",))
    assert feed.wait(0.05) is False
    started = time.monotonic()
    threading.Timer(0.2, scripted.script.put, [("value", "a", 42, 1.0)]).start()
    assert feed.wait(2.0) is True and time.monotonic() - started < 2.0
    assert _seen(feed.changes("line")) == [("a", 42)]
    feed.close()
    assert feed.wait(5.0) is False and time.monotonic() - started < 3.0
    assert not feed._sources["line"].thread.is_alive()


# ---------------------------------------------------------------- a poller
def test_a_poller_meets_the_same_contract_by_diffing_its_rows(scripted):
    feed = scripted.feed()
    scripted.table = [{"id": "WO-1", "state": "run", "at": "2026-10-08T06:00:00Z"},
                      {"id": "WO-2", "state": "idle", "at": 1_700_000_000}]
    first = feed.changes("mes")
    assert [e.key for e in first.events] == ["WO-1", "WO-2"] and first.state == "live"
    assert first.events[0].value == scripted.table[0]
    assert first.events[0].source_time == 1_791_439_200.0 and first.events[1].source_time == 1.7e9
    assert feed.rows("mes")[0] == scripted.table

    # Not read again before its interval; then a changed row, a new one and one gone.
    scripted.table = [{"id": "WO-1", "state": "down", "at": "2026-10-08T06:05:00"},
                      {"id": "WO-3", "state": "run", "at": None}]
    assert feed.changes("mes", first.cursor).events == ()
    scripted.clock.now += 5
    later = feed.changes("mes", first.cursor)
    assert [(e.key, e.value and e.value["state"]) for e in later.events] == [
        ("WO-1", "down"), ("WO-3", "run"), ("WO-2", None)]
    assert later.events[0].source_time == 1_791_439_500.0       # no offset: read as UTC
    scripted.clock.now += 5
    assert feed.changes("mes", later.cursor).events == ()       # nothing changed: no event
    assert feed.wait(0.01) is True and feed.wait(0.01) is False     # polled events count too


def test_a_poller_that_fails_is_down_keeps_its_rows_and_backs_off(scripted):
    feed = scripted.feed()
    scripted.failure = ReadError("E_DATA_READ", "HTTP 503 from connection 'mes'", "later")
    with pytest.raises(ReadError) as caught:
        feed.rows("mes")                                # no value yet: the failure itself
    assert caught.value.message == "HTTP 503 from connection 'mes'"
    scripted.failure, scripted.table = None, [{"id": "WO-1", "state": "run"}]
    assert feed.changes("mes").state == "down"          # still inside the 1 s backoff
    scripted.clock.now += 1
    cursor = feed.changes("mes").cursor
    assert feed.status("mes")["state"] == "live"

    scripted.failure = RuntimeError("the server said: S3cr3t-Canary")
    scripted.clock.now += 5
    down = feed.changes("mes", cursor)
    assert (down.state, down.events, down.gap) == ("down", (), False)
    assert [p.code for p in down.problems] == ["E_DATA_READ"]
    assert "RuntimeError" in down.problems[0].message and "Canary" not in down.problems[0].message
    assert feed.rows("mes")[0] == [{"id": "WO-1", "state": "run"}]      # the last good rows
    scripted.failure = None
    scripted.clock.now += 0.5
    assert feed.changes("mes", cursor).state == "down"
    scripted.clock.now += 0.5
    assert feed.changes("mes", cursor).state == "live" and down.age == 5.0


# ---------------------------------------------------------------- who may connect
def test_a_source_without_the_callers_connection_is_refused_and_no_reader_loads(scripted):
    loaded = []
    scripted_load = readers.load
    readers.load = lambda kind: loaded.append(kind) or scripted_load(kind)
    feed = live.Feed(scripted.sources)                  # the default policy: no connection
    for source in ("mes", "line"):
        for call in (feed.changes, feed.rows):
            with pytest.raises(netguard.NetworkRefused) as caught:
                call(source)
            assert caught.value.code == "E_DATA_CONNECTION"
    for name in ("file", "nope"):                       # only live kinds answer
        with pytest.raises(ReadError) as caught:
            feed.changes(name)
        assert caught.value.code == "E_DATA_SOURCE_KIND"
    assert loaded == [] and scripted.checked == [] and scripted.links == 0
    feed.close()


def test_a_feed_is_never_pickled(scripted):
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(scripted.feed())


# ---------------------------------------------------------------- where it may live
def test_the_core_imports_neither_the_feed_nor_a_reader():
    """Rule 12: nothing under `nagasim/core/` imports `live` or a reader, so no reader can
    call into a run and there is no streaming layer in the core."""
    offenders = []
    for path in (ROOT / "nagasim" / "core").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [f"{node.module}.{a.name}" for a in node.names] if isinstance(node, ast.ImportFrom) \
                and node.module else []
            offenders += [(path.name, name) for name in names
                          if name.startswith(("nagasim.live", "nagasim.readers"))]
    assert offenders == []


def test_importing_the_bindings_or_the_feed_loads_no_reader_and_stays_light():
    code = ("import sys; import nagasim.live; "
            "print(sorted(m for m in sys.modules if m.startswith('nagasim.readers.')), "
            "'pandas' in sys.modules); import nagasim.databind; "
            "print(sorted(m for m in sys.modules if m.startswith('nagasim.readers.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                         check=True).stdout.split("\n")
    assert out[0] == "[] False" and out[1] == "[]"


# ---------------------------------------------------------------- fix round (review)
def test_a_cursor_belongs_to_its_source_and_is_never_trusted(scripted):
    """G6, M3: a cursor names its source; one of another source, or one that is not ours in
    any way (a digit that is not ASCII), is unknown: a snapshot with `gap`, never an error."""
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", 1, 10.0))
    scripted.table = [{"id": "WO-1", "state": "run"}]
    line, mes = feed.changes("line").cursor, feed.changes("mes").cursor
    assert line != mes and feed.changes("line", line).gap is False
    crossed = feed.changes("line", mes)
    assert crossed.gap is True and _seen(crossed) == [("a", 1)]
    for junk in (f"{feed.epoch}:line:٣:0", f"{feed.epoch}:line:0:²", f"{feed.epoch}:line:0",
                 f"{feed.epoch}:0:0", ":::"):
        assert feed.changes("line", junk).gap is True


def test_status_opens_nothing(scripted):
    """G8: asking how a source is doing must not connect to it."""
    feed = scripted.feed()
    assert feed.status("line") == {"source": "line", "state": "unopened"}
    assert feed.status("mes")["state"] == "unopened" and scripted.links == 0 and scripted.checked == []
    with pytest.raises(ReadError):
        feed.status("nope")
    feed.changes("mes")
    assert feed.status("mes")["state"] == "live" and feed.status("line")["state"] == "unopened"


def test_a_value_that_is_not_a_finite_number_is_dropped_and_counted(scripted):
    """M4: NaN and infinity are not data: they would reach a float target and a record file
    as invalid JSON."""
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", float("nan"), 10.0), ("value", "a", float("inf"), 10.0),
          ("value", "a", {"x": float("-inf")}, 10.0), ("value", "a", 1.5, float("nan")),
          ("value", "b", 2.5, float("inf")))
    got = feed.changes("line")
    assert [(e.key, e.value, e.source_time) for e in got.events] == [("a", 1.5, None), ("b", 2.5, None)]
    assert feed.status("line")["dropped"] == {"type": 3}


def test_what_is_dropped_is_paced_like_what_is_kept(scripted):
    """I4: the rate wait used to come after the early returns, so a flood of values that are
    all dropped (not data, over a bound) was never slowed down."""
    scripted.policy = netguard.NetworkPolicy(connections={
        "line": _conn("pushed", limits=(("rate", 2),))})
    feed = scripted.feed()
    waits = []
    feed._stop = types.SimpleNamespace(is_set=lambda: False, set=lambda: None,
                                       wait=lambda seconds: waits.append(seconds) and False)
    scripted.clock.now = 1000.25
    _play(scripted, feed, ("up",), *[("value", "a", [n], 1.0) for n in range(4)])
    assert waits == [0.75, 0.75] and feed.status("line")["dropped"] == {"type": 4}


def test_a_link_that_keeps_dropping_is_retried_ever_more_slowly(scripted, monkeypatch):
    """M2, G9: the first retry waits 1 s, then 2, 4, 8 ... up to a minute, and the wait starts
    over only after a link stayed up for `STABLE_SECONDS`: a broker that accepts and drops is
    not sent a CONNECT (and a password) every second for ever."""
    monkeypatch.setattr(live.random, "random", lambda: 0.0)
    feed = scripted.feed()
    waits, stayed = [], [0.0] * 6 + [live.STABLE_SECONDS, 0.0]

    def wait(seconds):
        waits.append(seconds)
        return len(waits) >= 9

    def subscribe(source, conn, options, sink, stop):
        sink.connected()
        scripted.clock.now += stayed[len(waits)] if len(waits) < len(stayed) else 0.0
        raise ReadError("E_DATA_READ", "connection 'line' was dropped", "it reconnects")

    monkeypatch.setattr(readers.load("pushed"), "subscribe", subscribe)
    feed._stop = types.SimpleNamespace(is_set=lambda: len(waits) >= 9, set=lambda: None, wait=wait)
    feed._open("line").thread.join(5)
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 1.0, 2.0, 4.0]
    assert live.MAX_BACKOFF == 60.0 and live.STABLE_SECONDS >= 5


def test_a_cursor_of_any_shape_is_a_snapshot_never_an_error(scripted):
    """M3, the rest of it: a numeric part thousands of digits long (`int()` refuses it), and a
    cursor that is not a text at all."""
    feed = scripted.feed()
    _play(scripted, feed, ("up",), ("value", "a", 1, 10.0))
    good = feed.changes("line").cursor
    digits = "9" * 5000
    for junk in (f"{feed.epoch}:line:{digits}:0", f"{feed.epoch}:line:0:{digits}",
                 f"{feed.epoch}:line:{'0' * 19}:0", 7, 1.5, b"x:line:0:0", ["x"], {"a": 1}, True):
        got = feed.changes("line", junk)
        assert got.gap is True and _seen(got) == [("a", 1)], junk
    assert feed.changes("line", good).gap is False


def test_a_polled_row_over_a_bound_is_in_neither_the_events_nor_the_rows(scripted):
    """The 1 KiB bound of a text used to hold for `changes` only: `rows`, which a binding
    reads, kept a polled row with a cell of any length."""
    feed = scripted.feed()
    scripted.table = [{"id": "WO-1", "note": "ok"}, {"id": "WO-2", "note": "x" * 1025},
                      {"id": "k" * 1025, "note": "ok"}, {"id": "WO-3", "note": "ok"}]
    first = feed.changes("mes")
    assert [e.key for e in first.events] == ["WO-1", "WO-3"]
    assert [row["id"] for row in feed.rows("mes")[0]] == ["WO-1", "WO-3"]
    assert feed.status("mes")["dropped"] == {"oversize": 2}
    scripted.clock.now += 5
    feed.changes("mes", first.cursor)
    assert feed.status("mes")["dropped"] == {"oversize": 4}     # counted per read, as it is dropped


def test_a_reader_warning_is_one_w_live_limit_problem_and_reading_goes_on():
    feed = live.Feed([])
    src = live._Source({"name": "press"}, None, None, {})
    live._Sink(feed, src).warn("connection 'press': no limit", "lower the server's limits")
    found = feed._problems(src, 0, False)
    assert [(p.code, p.severity, p.hint) for p in found] == [
        ("W_LIVE_LIMIT", "warning", "lower the server's limits")]
