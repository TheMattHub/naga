"""What a run or a load logged, for the answer that reports it (D1520).

    with capture() as cap:
        ...                      # run or load
    cap.entries()   -> [{level, source, message, count}], at most MAX_ENTRIES
    cap.truncated   -> True when more kinds were logged than an answer carries

Captured: WARNING and above on the `nagasim` logger tree, and `warnings.warn`. Repeats of one
(level, source, message) fold into `count`; `source` is the logger name without `nagasim.`.

A capture only takes the records of the thread that opened it: the in-app assistant runs its
tools on a worker thread while the editor keeps logging on the GUI thread, and those records
belong to the editor, not to the tool's answer. A run's worker processes log in their own
process; `api.run_jobs` logs their records again on the calling thread, which is where they are
captured (once, as the pool already folds the repeats every replication makes). Captures nest:
a record reaches every capture open on its thread, the inner and the outer alike.

`warnings.warn` is caught through the process-wide `warnings.showwarning` hook: Python 3.12 has
no per-thread route (`logging.captureWarnings` swaps the same hook; context-aware warnings come
in 3.14). Another thread's `warnings.catch_warnings()` saves and restores that hook whenever its
block closes, so it can put `_show` back after the last capture closed. Hence the hook never
takes itself as the one to pass on to, `_show` never reenters itself, and the hook seen before
`_show` is kept so a later capture still ends with it restored.
"""
from __future__ import annotations

import logging
import threading
import warnings
from contextlib import contextmanager

MAX_ENTRIES = 20
_KEPT = 200             # kinds held while capturing; beyond it the oldest go (long runs of unique text)
_MESSAGE = 300          # characters of one message an answer carries
_PREFIX = "nagasim."

_lock = threading.Lock()
_active: dict[int, list["Capture"]] = {}      # thread id -> the captures open on it, outermost first
_shown = None                                 # the last hook seen before _show; never _show itself
_local = threading.local()                    # set while _show passes a warning on, on this thread
# The hook Python starts with, captured once (a private name, read here only as a fallback).
_ORIGINAL = getattr(warnings, "_showwarning_orig", None) or warnings.showwarning


class Capture:
    """The folded records of one capture block."""

    def __init__(self):
        self._kinds: dict[tuple, dict] = {}
        self._dropped = False

    def _add(self, level: str, source: str, message: str) -> None:
        key = (level, source, message)
        entry = self._kinds.get(key)
        if entry is not None:
            entry["count"] += 1
            return
        if len(self._kinds) >= _KEPT:
            del self._kinds[next(iter(self._kinds))]
            self._dropped = True
        self._kinds[key] = {"level": level, "source": source, "message": message, "count": 1}

    def entries(self) -> list[dict]:
        """The newest MAX_ENTRIES kinds, in the order they were first logged."""
        return [dict(e) for e in list(self._kinds.values())[-MAX_ENTRIES:]]

    @property
    def truncated(self) -> bool:
        return self._dropped or len(self._kinds) > MAX_ENTRIES

    def attach(self, answer) -> None:
        """Add `log` (and `log_truncated` when cut) to a dict answer; nothing when nothing was logged."""
        if isinstance(answer, dict) and self._kinds:
            answer["log"] = self.entries()
            if self.truncated:
                answer["log_truncated"] = True


def _deliver(thread: int, level: str, source: str, message: str) -> None:
    with _lock:
        captures = list(_active.get(thread, ()))
    if source.startswith(_PREFIX):
        source = source[len(_PREFIX):]
    message = message if len(message) <= _MESSAGE else message[:_MESSAGE - 3] + "..."
    for cap in captures:
        cap._add(level, source, message)


class _Handler(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = "error" if record.levelno >= logging.ERROR else "warning"
            _deliver(record.thread, level, record.name, record.getMessage())
        except Exception:       # a bad format string must not fail the run that logged it
            self.handleError(record)


_HANDLER = _Handler()


def _show(message, category, filename, lineno, file=None, line=None):
    if getattr(_local, "passing_on", False):     # a hook that wraps _show called back into it
        _ORIGINAL(message, category, filename, lineno, file, line)
        return
    _deliver(threading.get_ident(), "warning", f"warnings.{category.__name__}", str(message))
    # Passed on, so whoever showed or recorded warnings before still does.
    _local.passing_on = True
    try:
        (_shown or _ORIGINAL)(message, category, filename, lineno, file, line)
    finally:
        _local.passing_on = False


@contextmanager
def capture():
    """Capture what this thread logs at WARNING and above inside the block."""
    global _shown
    cap = Capture()
    thread = threading.get_ident()
    with _lock:
        if not _active:
            logging.getLogger("nagasim").addHandler(_HANDLER)
            if warnings.showwarning is not _show:     # else left by a catch_warnings exit
                _shown = warnings.showwarning
            warnings.showwarning = _show
        _active.setdefault(thread, []).append(cap)
    try:
        yield cap
    finally:
        with _lock:
            stack = _active[thread]
            stack.remove(cap)
            if not stack:
                del _active[thread]
            if not _active:
                logging.getLogger("nagasim").removeHandler(_HANDLER)
                # Restored only if still ours: something that wrapped it since keeps its own.
                if warnings.showwarning is _show:
                    warnings.showwarning = _shown or _ORIGINAL
