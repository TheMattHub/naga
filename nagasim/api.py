"""Headless entry point: run a model without an editor (D17).

    from nagasim.api import run_model
    results = run_model("models/single_server.json", replications=5, run_length=1000)

Also a CLI:  PY -m nagasim.api model.json --replications 5 --run-length 1000 --out results.xlsx
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, overload

from nagasim import __version__
from nagasim._cli import add_controllers_argument, add_model_argument, load_controllers, setup_cli
from nagasim.core import initial_state, logic_authoring, schema
from nagasim.core.calendar import CalendarError, model_calendar, run_horizon
from nagasim.core import loglevels
from nagasim.core.export import ARTIFACTS_KEY, LIVE_KEY, PROBLEMS_KEY, concat_runs
from nagasim.core.itemlogic import ItemCodeUntrusted
from nagasim.core.schema import ModelValidationError
from nagasim.core.sim_runner import Pacer, run_single_simulation
from nagasim.core.timeunits import UNITS, units_of
from nagasim.core.trace import TraceRecorder
from nagasim.plugins import load_plugins
from nagasim.results import ALWAYS_KEPT, Results


# The one "path or dict -> migrated layout" reader (Wave A2), re-exported
# for the top-level modules that spell it `api.load_layout`.
load_layout = schema.load_layout

log = logging.getLogger(__name__)


class _Collect(logging.Handler):
    """The warnings and errors one pool task logged, as picklable tuples."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append((record.name, record.levelno, record.getMessage()))
        except Exception:       # a bad format string must not fail the replication
            self.handleError(record)


@dataclass(frozen=True)
class _PoolTask:
    """One replication for a pool worker; a module-level dataclass, so it pickles."""
    layout: object
    seed: int
    run_len: float | None
    warmup: float | None
    controllers: object = None
    logic: str | None = None


def _pool_replication(task: _PoolTask):
    """One replication in a worker process: `(KPIs, what it logged)`.

    `task` is a `_PoolTask`. Module level and
    picklable on purpose (ROADMAP 1.6): a ProcessPoolExecutor pickles its
    callable by qualified name, so a closure or a bound method cannot be the
    pool task. `layout` may equally be a path -- both are what
    `run_single_simulation` accepts, and both pickle. *controllers*
    (ROADMAP 3.7 A) must pickle too -- `run_model` checks that before ever
    reaching a pool. *logic* is the caller's LOGIC module by NAME (D914): a
    module object does not pickle, so each worker imports it again.

    A worker process has no console of its own once the editor is started from
    an icon, and no handler of the parent's either, so its warnings travel back
    with the result and `run_jobs` logs them again in the parent."""
    logic = task.logic
    handler = _Collect()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        # ROADMAP 2.4 B: on Windows (spawn) each pool worker is a fresh
        # interpreter -- plugin blocks/distributions registered in the parent
        # process are not there until this runs again here.
        load_plugins()
        logic_module = (logic if isinstance(logic, ModuleType) or logic is None
                        else logic_authoring.resolve_logic(logic))
        kpis = run_single_simulation(task.layout, seed=task.seed, run_len=task.run_len,
                                     warmup=task.warmup, controllers=task.controllers,
                                     logic_module=logic_module)
        return kpis, handler.records
    finally:
        root.removeHandler(handler)


# What a process pool costs before it returns a single result: worker start-up
# plus the pickling of the first tasks. SPAWN (Windows, macOS) starts a fresh
# interpreter per worker that re-imports pandas, scipy and simpy: measured
# around 0.8 s for 2 workers and 2.5 s for 20; up to the cap of 8 that is about 1.5 s
# (BENCH-SCALE-2026-10-27: 3 s and a cap of 4 left `auto` at 2.6x where 4 workers gave
# 3.8x). FORK (the Linux default) copies the parent instead and re-imports
# nothing, which is roughly an order of magnitude cheaper -- charging it the
# spawn price kept small-but-real workloads sequential on Linux (PHASE4 T9).
_SPAWN_SETUP_COST = 1.5
_FORK_SETUP_COST = 0.3
# The most workers `auto` will ever ask for. No core is held back for the
# caller: while the pool runs, the parent only waits on results.
_MAX_AUTO_WORKERS = 8


def pool_setup_cost() -> float:
    """The start-up price of a process pool on THIS platform, in seconds.

    `allow_none=True` on purpose: asking for the resolved start method would
    fix it process-wide and break a caller that sets its own afterwards.
    """
    method = multiprocessing.get_start_method(allow_none=True)
    if method is None:
        # The platform's default is the first one listed. Asked, not assumed from the
        # system: Linux forks up to Python 3.13 and uses a fork server from 3.14, whose
        # workers import everything again as spawned ones do (#364).
        method = multiprocessing.get_all_start_methods()[0]
    return _FORK_SETUP_COST if method == "fork" else _SPAWN_SETUP_COST


def auto_workers(elapsed: float, remaining: int, cpus: int | None = None,
                 setup_cost: float | None = None) -> int:
    """ROADMAP 1.7 E: pool only when it is worth the process-pool start-up cost.

    *elapsed* is one task's measured time and *remaining* the number of tasks
    still to run -- since PHASE4 T3 that is (design points x replications), not
    replications alone, so a 200-point design of quarter-second runs pools
    although no single run is slow. The crossover is the TOTAL work left
    against the pool's own start-up cost.

    *cpus* and *setup_cost* default to this machine and this platform; they are
    arguments so the rule can be stated and tested without either (PHASE4 T9 --
    a test that asserted the rule through the machine's own core count failed
    on a 2-core CI runner, where the old "keep one core free" reserve left ONE
    worker and so never pooled at all).
    """
    cpus = cpus if cpus is not None else (os.cpu_count() or 2)
    setup_cost = pool_setup_cost() if setup_cost is None else setup_cost
    if remaining >= 3 and elapsed * remaining >= setup_cost:
        return max(1, min(_MAX_AUTO_WORKERS, cpus, remaining))
    return 1


def with_run_setting(path, key: str, value):
    """*path* as a LAYOUT DICT with `simulation.<key>` set (PHASE4 T7).

    A run setting rather than a call argument, so it reaches a pool worker with
    the layout and no task shape changes. The caller's dict is never mutated.
    """
    layout = dict(schema.read_layout(path))
    layout["simulation"] = {**(layout.get("simulation") or {}), key: value}
    return layout


class WarmupPastEndError(ValueError):
    """#1720's refusal, a ValueError with its own code so a tool does not read it as a bad KPI
    or check (#1752)."""
    code = "E_WARMUP_PAST_END"


def run_settings(path, replications=None, warmup=None, run_length=None):
    """The settings `_resolve_settings` gives, refused when the warm-up leaves no observed
    time (#1720): a run whose warm-up reaches its end reports only zeros. A count of
    replications under 1 is refused before a deterministic model forces it to 1 (#1899: 0 or -3
    used to run nothing and return None)."""
    # __index__: an int or a numpy integer, never a float (no import: api.<TAB> stays short, #1897)
    if replications is not None and not (hasattr(replications, "__index__") and replications >= 1):
        raise ValueError(f"replications must be a whole number of at least 1, not {replications!r}")
    replications, warmup, run_length = _resolve_settings(path, replications, warmup, run_length)
    if warmup and run_length and float(warmup) >= float(run_length):
        raise WarmupPastEndError(f"the warm-up {warmup:g} reaches the run length {run_length:g}, so nothing "
                         f"would be observed; shorten the warm-up or lengthen the run")
    return replications, warmup, run_length


def _resolve_settings(path, replications=None, warmup=None, run_length=None):
    """(replications, warmup, run_length), each falling back to the model's own
    "simulation" section (ROADMAP 1.5) when left None -- an explicit value (0
    included) always wins.

    Raises ModelValidationError if the effective run length ends up 0/None and
    the model has no other way to stop (`schema.requires_run_length`): such a
    model would otherwise hang forever (a never-ending CREATE, a failure, or a
    repeating capacity schedule all keep the event queue non-empty).

    Its own function since PHASE4 T3, because a task submitted straight to the
    pool skips `run_model` and must resolve the same settings from the same
    place -- per design point, since an override may move `simulation.time_limit`.

    A deterministic model (`simulation.deterministic`, PHASE6 W4 P1, D119)
    always comes back with replications forced to 1, whatever the model or
    the caller asked for -- every replication of a deterministic model is
    identical, so more than one is pure waste, never a different answer. The
    fast path below (every one of replications/warmup/run_length already
    given) skips the migration but still reads a FILE path's flag (#212).
    """
    raw = schema.read_layout(path)
    if not (replications is None or warmup is None or run_length is None
            or not (run_length or 0) > 0):
        if (raw.get("simulation") or {}).get("deterministic"):
            return 1, warmup, run_length
        return replications, warmup, run_length
    migrated = schema.migrate(raw)
    sim = migrated.get("simulation", {})
    if replications is None:
        replications = sim.get("replications") or 1
    if warmup is None:
        warmup = initial_state.default_warmup(migrated, None)
    if run_length is None:
        # PHASE4 T10: a model may give an end DATE instead of a length.
        run_length = run_horizon(migrated) or 0.0
    if not (run_length or 0) > 0 and schema.requires_run_length(migrated):
        raise ModelValidationError(["model never ends on its own; set a run length"])
    if sim.get("deterministic"):
        replications = 1
    return replications, warmup, run_length


# #1861 (D1293): a ProcessPoolExecutor starts its workers inside `submit`, on the caller's
# thread and holding the pool's shutdown lock. On Windows the start writes the job's
# set-up to the new child through a pipe of about 4 KB; when the set-up is larger (a long
# sys.path) and the child never reads it, that write blocks forever, and since the parent
# keeps the pipe's read end, even a child that died does not end it. After this many
# seconds a watchdog ends the child and reads the pipe empty itself, so the study fails
# with an error instead of hanging. Generous on purpose: a healthy child on a loaded
# machine can take tens of seconds to start.
_SPAWN_DEADLINE = 120.0


class _SpawnWatchdog:
    """Frees the caller's thread from a worker start that outlives _SPAWN_DEADLINE (Windows).

    One per pooled `run_jobs` call, armed around each submit. Cancelling the blocked
    write (CancelSynchronousIo) was tried first and dropped: under load the cancel itself
    hung about one time in ten. Reading the pipe's other end, which the parent holds
    anyway, always lets the write finish; the start then returns normally, the pool sees
    its terminated child and goes broken, and `_submit` raises.
    """

    def __init__(self, deadline: float):
        self.deadline = deadline
        self._fired = False
        self._armed_at: float | None = None
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.get_ident()
        threading.Thread(target=self._watch, name="naga-spawn-watchdog", daemon=True).start()

    def arm(self) -> None:
        with self._lock:
            self._armed_at = time.monotonic()

    def disarm(self) -> bool:
        """Whether the watchdog ended this submit's worker start."""
        with self._lock:
            self._armed_at = None
            fired, self._fired = self._fired, False
            return fired

    def close(self) -> None:
        self._closed.set()

    def _watch(self) -> None:
        # Polls rather than firing once: at the deadline the thread may still be in
        # CreateProcess, before there is a child to end.
        while not self._closed.wait(min(1.0, self.deadline / 4)):
            with self._lock:
                if self._armed_at is None or time.monotonic() - self._armed_at < self.deadline:
                    continue
                popen, rhandle = _blocked_spawn(self._thread)
                if popen is None or getattr(popen, "_handle", None) is None or rhandle is None:
                    continue
                self._fired = True
                try:
                    popen.terminate()          # first, so no live child reads alongside
                except OSError:
                    pass
                _drain(rhandle)


def _blocked_spawn(thread_id: int):
    """The Popen and pipe read handle of the worker start *thread_id* is inside, if any.
    multiprocessing keeps both as locals of its Windows Popen's __init__ until the write
    is done, so that frame is the only place to reach them."""
    frame = sys._current_frames().get(thread_id)
    while frame is not None:
        code = frame.f_code
        if code.co_name == "__init__" and code.co_filename.endswith("popen_spawn_win32.py"):
            names = frame.f_locals
            return names.get("self"), names.get("rhandle")
        frame = frame.f_back
    return None, None


def _drain(rhandle: int) -> None:
    """Reads the start pipe until its writer closes it (a broken pipe), so the blocked
    write, and any write after it, completes."""
    import _winapi                     # Windows only, as multiprocessing itself uses it
    try:
        while True:
            _winapi.ReadFile(rhandle, 65536)
    except OSError:
        pass


class _NotStarted(RuntimeError):
    """The #1861 start error; its own class only so `run_jobs` knows not to wait again."""


def _not_started(deadline: float) -> RuntimeError:
    return _NotStarted(f"a worker process did not start within {deadline:g} s; "
                       "try again, or run with workers=1")


def _submit(pool, task: _PoolTask, watchdog: _SpawnWatchdog | None):
    if watchdog is None:
        return pool.submit(_pool_replication, task)
    watchdog.arm()
    try:
        future = pool.submit(_pool_replication, task)
    finally:
        fired = watchdog.disarm()
    if fired:
        raise _not_started(watchdog.deadline)
    return future


# #1861 (D1294): a child that is alive but never starts, its set-up small enough to sit in the
# pipe, blocks no write: the parent then waits on it forever, in `wait` or when the pool's
# `with` joins it, and from outside it looks like a long replication. So every pool the
# engine builds gives its workers an initializer that signals "up" before any task, and
# `run_jobs` ends a pool whose worker has not signalled within _SPAWN_DEADLINE. A plain
# ProcessPoolExecutor a caller passes in has no check (nothing can be attached once it is
# built), so `new_pool` is the public way to build a checked one (D1296).
def _worker_started(signal) -> None:
    signal.release()


class _StartCheck:
    """How many of one pool's workers have signalled they are up, and since when each was
    seen. Counts signals rather than matching pids: in a frozen build the pid a worker sees
    for itself need not be the one the parent holds."""

    def __init__(self, signal):
        self.signal = signal
        self.started = 0
        self.seen: dict[int, float] = {}

    def outstanding(self, pool, timeout: float = 0.0) -> int:
        """Workers yet to signal, after waiting up to *timeout* s for one more signal."""
        now = time.monotonic()
        for pid in list(getattr(pool, "_processes", None) or ()):
            self.seen.setdefault(pid, now)
        if self.started < len(self.seen) and self.signal.acquire(True, timeout):
            self.started += 1
        while self.started < len(self.seen) and self.signal.acquire(False):
            self.started += 1
        return len(self.seen) - self.started

    def late(self) -> bool:
        """Whether fewer workers have signalled than were seen _SPAWN_DEADLINE ago."""
        cutoff = time.monotonic() - _SPAWN_DEADLINE
        return self.started < sum(1 for since in self.seen.values() if since <= cutoff)


def _end_workers(pool, pending) -> None:
    """Ends every worker (the pool breaks at the first anyway) and cancels what is queued,
    so neither `wait` nor leaving the pool's `with` waits on a child that never started."""
    for future in pending:
        future.cancel()
    for process in list((getattr(pool, "_processes", None) or {}).values()):
        try:
            process.terminate()
        except (OSError, ValueError, AttributeError):
            pass


def _await_starts(pool, check: _StartCheck, pending, tick: float) -> None:
    """Waits for the workers yet to signal; once one is late, ends them all and raises."""
    while check.outstanding(pool, tick):
        if check.late():
            _end_workers(pool, pending)
            raise _not_started(_SPAWN_DEADLINE)


def new_pool(workers: int) -> ProcessPoolExecutor:
    """A process pool to pass as ``pool=`` to `run_model`, `run_jobs`, the studies and the
    optimisers: a `ProcessPoolExecutor` whose workers report they are up, so a worker that
    never starts ends the run with an error after a deadline instead of hanging it. A plain
    `ProcessPoolExecutor` works too but has no such check. Use it as a context manager."""
    context = multiprocessing.get_context()
    signal = context.Semaphore(0)
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=context,
                               initializer=_worker_started, initargs=(signal,))
    pool._naga_start_check = _StartCheck(signal)
    return pool


@contextmanager
def scenario_pool(workers: int | str):
    """One process pool for a whole multi-run study (ROADMAP 2.5, PHASE4 A4).

    `run_scenarios`, DOE and the optimisers call `run_model` many times; each
    call used to build and tear down its own `ProcessPoolExecutor`, which on
    Windows (spawn) costs seconds per scenario in interpreter start-up alone.
    Wrapping the whole study in this and passing the result as `run_model(...,
    pool=...)` reuses one set of workers throughout. Results are unchanged: a
    replication is seeded from its own seed and the frames are re-ordered by
    seed before they are concatenated, whoever ran them.

    Yields None for a serial study (workers <= 1), so the sequential path never
    starts a process at all.
    """
    if workers == "auto" or not isinstance(workers, int) or workers <= 1:
        yield None
        return
    with new_pool(workers) as pool:
        yield pool


def run_jobs(jobs, *, pool=None, workers: int = 1, on_progress=None, should_stop=None,
             done: int = 0, total: int | None = None, controllers=None,
             logic: str | None = None) -> dict[object, list]:
    """Run many `(tag, layout, seed, run_length, warmup)` TASKS on one pool.

    The seam the whole studies layer parallelises through (PHASE4 T3). A task
    is one replication of one design point, so N points x R replications fill
    the workers whatever R is -- before this, only the replications of one
    point were ever in flight and a one-replication-per-point DOE ran serially.

    Returns `{tag: [(seed, kpis), ...]}`, each list sorted by seed and the tags
    in the order they were first submitted. Both make the result independent of
    which worker finished when: a replication is a pure function of (layout,
    seed), so the frames a caller assembles from this are bit-identical to the
    sequential ones (ARCHITECTURE 6.5).

    *jobs* is consumed LAZILY and at most `4 x workers` tasks are in flight, so
    a 500-point Latin hypercube never materialises 500 layouts at once.

    `on_progress(done, total)` counts completed TASKS. `should_stop` is polled
    between waves and cancels what has not started; a task already running in a
    child process is left to finish, since nothing can hear the flag there.
    Serial (`pool is None`) it is also polled inside a run, as before.

    *logic*: the dotted name of the caller's LOGIC module (see `run_model`); serial, the
    module itself will do.
    """
    jobs = iter(jobs)
    collected: dict[object, list] = {}
    count = done

    if pool is None:
        logic_module = (logic if isinstance(logic, ModuleType) or logic is None
                        else logic_authoring.resolve_logic(logic))
        for tag, layout, seed, run_len, warm in jobs:
            if should_stop is not None and should_stop():
                break
            collected.setdefault(tag, [])
            kpi = run_single_simulation(layout, seed=seed, run_len=run_len, warmup=warm,
                                        should_stop=should_stop, controllers=controllers,
                                        logic_module=logic_module)
            if kpi is None:                    # cut short mid-replication
                break
            collected[tag].append((seed, kpi))
            count += 1
            if on_progress is not None:
                on_progress(count, total)
    else:
        window = max(4, 4 * int(workers or 1))
        futures: dict = {}
        pending: set = set()
        relogged: set = set()
        exhausted = False
        # Any submit may start a worker until the pool is full, so every one is guarded (#1861).
        watchdog = _SpawnWatchdog(_SPAWN_DEADLINE) if sys.platform == "win32" else None
        check = getattr(pool, "_naga_start_check", None)
        tick = min(1.0, _SPAWN_DEADLINE / 4)
        try:
            while True:
                while not exhausted and len(pending) < window:
                    task = next(jobs, None)
                    if task is None:
                        exhausted = True
                        break
                    tag, layout, seed, run_len, warm = task
                    collected.setdefault(tag, [])
                    try:
                        future = _submit(pool, _PoolTask(layout, seed, run_len, warm, controllers, logic),
                                         watchdog)
                    except RuntimeError:
                        for future in pending:      # so leaving the pool's `with` does not run them
                            future.cancel()
                        raise
                    futures[future] = (tag, seed)
                    pending.add(future)
                if not pending:
                    break
                # Only while a worker has yet to report does `wait` wake up to look (D1294).
                timeout = tick if check is not None and check.outstanding(pool) else None
                finished, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                if check is not None and check.late():
                    _end_workers(pool, pending)
                    raise _not_started(_SPAWN_DEADLINE)
                for future in finished:
                    tag, seed = futures.pop(future)
                    kpi, logged = future.result()
                    for record in logged:
                        # Every replication repeats the model's load-time warnings: once is enough.
                        if record not in relogged:
                            relogged.add(record)
                            logging.getLogger(record[0]).log(record[1], record[2])
                    if kpi is not None:
                        collected[tag].append((seed, kpi))
                        count += 1
                        if on_progress is not None:
                            on_progress(count, total)
                if should_stop is not None and should_stop():
                    for future in pending:
                        future.cancel()
                    break
            # A worker still starting when the work ran out is waited for here: one that never
            # starts would otherwise hang the pool's `with` on its way out (D1294).
            if check is not None:
                _await_starts(pool, check, (), tick)
        except Exception as exc:
            # A task error while a worker is still starting (D1296): wait for it as above, or
            # the pool's exit hangs on one that never starts. Workers that did start are left
            # alone, so a caller's reused pool stays usable. A start error or a broken pool
            # (a crashed or ended worker, which never signals) needs no wait.
            if check is None or isinstance(exc, _NotStarted) or getattr(pool, "_broken", False):
                raise
            try:
                _await_starts(pool, check, pending, tick)
            except _NotStarted as start_error:
                raise start_error from exc
            raise
        finally:
            if watchdog is not None:
                watchdog.close()
    return {tag: sorted(pairs, key=lambda pair: pair[0]) for tag, pairs in collected.items()}


def _run_pool(path, seeds, run_length, warmup, workers, on_progress, should_stop, total, done,
             controllers=None, pool=None, logic=None):
    """Run *seeds* across a process pool; returns (seed, kpis) pairs.

    *pool*, when given, is a caller-owned `ProcessPoolExecutor` (see
    `scenario_pool`) that outlives this call; otherwise one is built here and
    shut down at the end, exactly as before. The submission itself is
    `run_jobs` with one tag.
    """
    with ExitStack() as stack:
        if pool is None:
            pool = stack.enter_context(new_pool(workers))
        out = run_jobs(((None, path, s, run_length, warmup) for s in seeds), pool=pool,
                       workers=workers, on_progress=on_progress, should_stop=should_stop,
                       done=done, total=total, controllers=controllers, logic=logic)
    return out.get(None, [])


def _check_controllers(controllers, workers, pool=None) -> None:
    """Refuse, before any run, a controller that cannot travel to a worker process: with
    *workers* not 1, or a caller's *pool* (used whatever *workers* says, #1706), each pooled
    replication needs its own pickled copy. Shared by `run_model` and the studies
    (`run_scenarios`, `optimize`, ...), so one rule holds for all of them."""
    if controllers and (workers != 1 or pool is not None):
        import pickle
        for name, controller in controllers.items():
            try:
                pickle.dumps(controller)
            except Exception as exc:
                raise ValueError(
                    f"controller '{name}' is not picklable, but workers={workers!r} may spread "
                    f"replications across processes (each needs its own copy): run with "
                    f"workers=1, or make the controller picklable ({exc})") from exc


def _logic_module(logic, workers):
    """(module, name) for `run_model(logic=...)`, imported before the first run so a
    bad name fails at once. Pool workers re-import by name, so a module object
    must be importable under its `__name__` when *workers* is not 1."""
    if logic is None:
        return None, None
    if isinstance(logic, str):
        return logic_authoring.resolve_logic(logic), logic
    if not isinstance(logic, ModuleType):
        raise TypeError(f"logic must be a module name or a module, not {type(logic).__name__}")
    name = getattr(logic, "__name__", None)
    if workers != 1:
        try:
            importable = name not in (None, "__main__") and importlib.import_module(name) is logic
        except ImportError:
            importable = False
        if not importable:
            raise ValueError(
                f"logic module {name!r} cannot be imported again by name, but workers={workers!r} "
                f"may spread replications across processes (each imports it by name): pass "
                f"its dotted name from an importable file, or run with workers=1")
    return logic, name


# The loggers whose records every replication repeats word for word: a model's load-time
# warnings and a block's first negative sample (S1-14). The pool says them once already.
_REPEATED_LOGGERS = ("nagasim.core.graph_model", "nagasim.core.base_process")


@contextmanager
def _said_once():
    """While a run's inline replications run, a record already logged is not logged again."""
    seen = set()

    def first(record) -> bool:
        key = (record.name, record.levelno, record.getMessage())
        if key in seen:
            return False
        seen.add(key)
        return True

    loggers = [logging.getLogger(name) for name in _REPEATED_LOGGERS]
    for logger in loggers:
        logger.addFilter(first)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(first)


# Without `should_stop` the answer is never None, so a type checker lets `.kpis()` follow the call
# (review S1-08); the same options as the definition below, which is the one that runs.
@overload
def run_model(
    path: str | os.PathLike[str] | dict[str, Any], *, replications: int | None = None,
    seed: int = 0, warmup: float | None = None, run_length: float | None = None,
    workers: int | str = 1, on_progress: Any = None, should_stop: None = None, trace: Any = None,
    bind_data: bool = False, base_dir: Any = None, network: Any = None, confine_to: Any = None,
    feed: Any = None, controllers: dict | None = None, pool: Any = None,
    logic: str | ModuleType | None = None, record_logs: bool | None = None,
    deterministic: bool | None = None, pace: float | None = None,
    keep_empty: bool = False, log_level: str | None = None) -> Results: ...
@overload
def run_model(
    path: str | os.PathLike[str] | dict[str, Any], *, replications: int | None = None,
    seed: int = 0, warmup: float | None = None, run_length: float | None = None,
    workers: int | str = 1, on_progress: Any = None, should_stop: Callable[[], bool], trace: Any = None,
    bind_data: bool = False, base_dir: Any = None, network: Any = None, confine_to: Any = None,
    feed: Any = None, controllers: dict | None = None, pool: Any = None,
    logic: str | ModuleType | None = None, record_logs: bool | None = None,
    deterministic: bool | None = None, pace: float | None = None,
    keep_empty: bool = False, log_level: str | None = None) -> Results | None: ...
def run_model(path, *, replications: int | None = None, seed: int = 0, warmup: float | None = None,
              run_length: float | None = None, workers: int | str = 1, on_progress=None,
              should_stop=None, trace=None, bind_data: bool = False, base_dir=None,
              network=None, confine_to=None, feed=None,
              controllers: dict | None = None, pool=None,
              logic: str | ModuleType | None = None, record_logs: bool | None = None, deterministic: bool | None = None,
              pace: float | None = None, keep_empty: bool = False,
              log_level: str | None = None) -> Results | None:
    """Run replications of a model and return its KPI frames.

    Replication ``r`` is seeded with ``seed + r``, so a run is reproducible.

    Parameters
    ----------
    path : str or dict
        A model file path, the name of a bundled model (``"single_server"``; a file at that
        path wins), or an already-loaded layout dict (nothing is written to
        disk for a dict).
    replications, warmup, run_length : optional
        Fall back to the model's own ``simulation`` section when left None; an
        explicit value (0 included) always wins. A warm-started model (a non-empty
        ``initial_state``) defaults its warm-up to 0, because the initial state is
        the history a warm-up would otherwise build. A model with no run length
        and no other way to stop raises `ModelValidationError`, as it would
        otherwise run forever.
    seed : int, default 0
        Seed of replication 0.
    workers : int or "auto", default 1
        More than 1 spreads the replications over a process pool; the output is
        identical to a sequential run. ``"auto"`` times replication 0 first and
        switches to a pool only when at least 3 more replications remain and that
        one took 1 s or more.
    on_progress : callable, optional
        ``on_progress(done, total)`` after each completed replication.
    should_stop : callable, optional
        Polled between replications and during one; on an interruption only the
        completed replications are returned.
    trace : TraceRecorder, optional
        Subscribed to replication 0 only, which then runs in this process.
    bind_data : bool, default False
        Resolve the model's data bindings once, before the first replication, so
        every replication runs on the same bound values. The `BindingReport`
        comes back as the extra ``"data_inputs"`` frame; a binding that fails is
        a row with its problem, never an exception.
    base_dir : path, optional
        What a source's relative ``location`` resolves against (default: the
        model file's folder).
    network : NetworkPolicy, optional
        The caller's `netguard.NetworkPolicy`. Without one a ``sql`` or
        ``json_url`` source is refused, whatever the model file says.
    confine_to : path, optional
        The one directory a file-backed source may be read from.
    feed : Feed, optional
        The caller's open `live.Feed`; live sources are answered from it, in this
        process.
    controllers : dict, optional
        ``{policy name: Controller}`` for every policy-mode DECIDE block. With
        ``workers != 1`` they must be picklable.
    logic : str or module, optional
        The module LOGIC blocks take their classes from first, by dotted name (on
        ``sys.path``) or as an imported module; a class it lacks still comes from
        ``nagasim.core.custom_logic``. Only the caller names it, never the model
        file. With ``workers != 1`` each worker imports it again by name.
    pool : ProcessPoolExecutor, optional
        A pool the caller owns and reuses across calls (see `scenario_pool`), so
        a study pays the process start-up once. Results are identical. Build it
        with `new_pool`, so a worker that never starts raises instead of hanging.
    record_logs : bool, optional
        False switches the per-event logs off: the log frames have no rows, so
        they are left out (see *keep_empty*), and every summary statistic is
        untouched. None keeps the model's own setting (default: on).
    deterministic : bool, optional
        True makes every distribution sample its mean, chance branches use
        largest-remainder allocation and replications are forced to 1. None keeps
        the model's own ``simulation.deterministic`` (default: off).
    pace : float, optional
        Keep pace with the wall clock: *pace* model seconds per wall second
        (1 = real time, 60 = one model minute per second), converted from the
        model's time unit. An event already behind the wall clock runs at once
        and is counted, never raised. The results are an unpaced run's; the
        result's ``.pace`` is ``{"paced", "late_events", "max_lag_s"}``.
        Replications run one after another (``workers`` must be 1). Nothing is
        fed into or read out of the running model.
    keep_empty : bool, default False
        Also return the frames with no rows (D1321): by default only the frames
        that have rows come back, plus ``"system"`` always and ``"data_inputs"``
        when *bind_data* asked for it. True returns every frame the engine
        builds, each with its columns; the KPI reference generator uses it.
    log_level : str, optional
        What the run records (D1395): ``"kpi"`` (the summary frames only), ``"series"`` (plus
        ``"timeseries"`` and ``"dashboard"``) or ``"full"`` (plus the entity, resource and store
        logs), or a profile: ``"study"`` (kpi), ``"explore"`` and ``"replay"`` (full). It beats the
        model's own ``simulation.log_level``, ``record_logs`` and ``timeseries_points`` (never
        raising the latter). A frame it leaves out raises `NotRecordedError` naming the level to
        re-run at. None (the default) keeps the model's settings: full unless it says otherwise.
        Not together with *record_logs*.

    Returns
    -------
    Results or None
        A `Results` (a ``dict[str, pandas.DataFrame]``) of the KPI frames
        concatenated over the completed replications, each tagged with a ``Run``
        column, with ``.kpis()``, ``.kpi(key)``, ``.plot(key)`` and
        ``.to_excel(path)`` on top. Only frames with rows are returned (a single
        server gets about 7), so test for an optional one with
        ``"store_summary" in results`` or ``results.get("store_summary")``.
        None if no replication completed.

    See Also
    --------
    nagasim.experiments.run_scenarios : Several variants of one model, compared.
    nagasim.experiments.run_until_precision : Replicate until an interval is tight enough.
    nagasim.experiments.flatten_kpis : The KPI frames as one long table.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import api
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> results = api.run_model(str(path), replications=2, run_length=200, warmup=20, seed=0)
    >>> sorted(int(r) for r in results["system"]["Run"].unique())
    [0, 1]
    >>> results.kpi("system/Total Entities Completed") > 0
    True
    """
    # ROADMAP 2.4 B: a library caller that never went through launcher.main()
    # (a script, a notebook, an MCP tool) still gets plugin blocks/
    # distributions registered on its first run_model() call; a no-op after
    # the first time in this process.
    load_plugins()

    if isinstance(path, str):       # a bundled model's name, as every command takes it (S1-15)
        from nagasim import examples
        path = examples.model_path(path)
        if near := examples.bundled_hint(path):        # #1899: a misspelt name names the nearest
            raise FileNotFoundError(f"no model file '{path}'{near}")

    pacer = None
    if pace is not None:
        if workers != 1:
            raise ValueError(f"a paced run keeps one clock, so its replications run one "
                             f"after another: workers must be 1, not {workers!r}")
        unit = units_of((path if isinstance(path, dict) else load_layout(path)).get("simulation")).time
        pacer = Pacer(pace, UNITS.get(unit, 1))

    _check_controllers(controllers, workers)
    logic_module, logic_name = _logic_module(logic, workers)

    if log_level is not None:
        # D1395: a level is a name for record_logs and timeseries_points, applied in one place.
        if record_logs is not None:
            raise ValueError("give record_logs or log_level, not both")
        path = loglevels.apply_level(path, loglevels.resolve(log_level))
    elif record_logs is not None:
        # PHASE4 T7. A run setting, so it travels to a worker with the layout
        # and no task shape changes: `simulation.record_logs` is read once per
        # replication, exactly like `entity_log_limit`. The caller's switch beats a level the
        # model names (D1395: the caller is the more specific request).
        path = with_run_setting(path, "record_logs", bool(record_logs))
        path["simulation"].pop("log_level", None)

    if deterministic is not None:
        # PHASE6 W4 P1, D119: same pattern as record_logs above.
        path = with_run_setting(path, "deterministic", bool(deterministic))

    bindings = None
    if bind_data:
        migrated = load_layout(path)
        if base_dir is None and not isinstance(path, dict):
            base_dir = Path(path).parent
        # Lazy: databind imports this module at load time.
        from nagasim.databind import resolve_bindings
        # network=/confine_to= (netguard) apply to the DATA SOURCES this
        # resolves, not to the model file already read above.
        path, bindings = resolve_bindings(migrated, base_dir, network=network,
                                          confine_to=confine_to, feed=feed)

    replications, warmup, run_length = run_settings(path, replications, warmup, run_length)

    with _said_once():      # S1-14: what every replication logs alike is said once
        seeds = [seed + i for i in range(replications)]
        collected: list[tuple[int, dict]] = []

        if workers == "auto":
            if should_stop is not None and should_stop():
                return None
            subscribers = (trace,) if trace is not None else ()
            t0 = time.perf_counter()
            kpi0 = run_single_simulation(path, seed=seeds[0], run_len=run_length, warmup=warmup,
                                         should_stop=should_stop, subscribers=subscribers,
                                         controllers=controllers, logic_module=logic_module)
            elapsed = time.perf_counter() - t0
            if kpi0 is None:
                return None
            collected.append((seeds[0], kpi0))
            if on_progress is not None:
                on_progress(1, replications)
            seeds = seeds[1:]
            workers = auto_workers(elapsed, len(seeds))

        if workers <= 1:
            inline = seeds
        else:
            inline = seeds[:1] if trace is not None else []
        pooled = [s for s in seeds if s not in set(inline)]

        stopped = False
        for s in inline:
            if should_stop is not None and should_stop():
                stopped = True
                break
            subscribers = (trace,) if trace is not None and s == seed else ()
            kpi = run_single_simulation(path, seed=s, run_len=run_length,
                                         warmup=warmup, should_stop=should_stop, subscribers=subscribers,
                                         controllers=controllers, pacer=pacer,
                                         logic_module=logic_module)
            if kpi is None:  # cut short mid-replication
                stopped = True
                break
            collected.append((s, kpi))
            if on_progress is not None:
                on_progress(len(collected), replications)

        if pooled and not stopped:
            collected.extend(_run_pool(path, pooled, run_length, warmup, workers,
                                       on_progress, should_stop, replications, len(collected),
                                       controllers=controllers, pool=pool, logic=logic_name))

    collected.sort(key=lambda pair: pair[0])   # pool order is arrival order, not seed order
    if not collected:
        return None
    frames = concat_runs([kpi for _, kpi in collected])
    # D1321: a table with no rows answers nothing, so it is left out. Empty means no rows
    # (all-NaN rows still count); judged after the concatenation, as a frame empty in one
    # replication can have rows in another. "system" is kept whatever it holds.
    results = frames_with_rows(frames, keep_empty)
    results.live_at_end = live_counts(kpi for _, kpi in collected)
    # E8: a plugin's objects, by run id; what it recorded as numbers is the "plugin_kpis" frame.
    results._artifacts = dict(kpi[ARTIFACTS_KEY] for _, kpi in collected if ARTIFACTS_KEY in kpi)
    # D1389: the run-time warnings, unioned over the replications by (code, block).
    results._run_problems = _union_problems(kpi[PROBLEMS_KEY][1] for _, kpi in collected
                                            if PROBLEMS_KEY in kpi)
    # D906: what `Results.checks()` judges the run by; read_layout returns a
    # dict as given and re-reads a file path (already read once above).
    results._layout = schema.read_layout(path)
    results._warmup = warmup
    label(results, results._layout.get("simulation"))      # D1395
    if pacer is not None:
        results.pace = pacer.summary()
        log.info("Paced at %gx: %d late events, max lag %.3f s", pacer.pace,
                 pacer.late_events, pacer.max_lag_s)
    if bindings is not None:
        results["data_inputs"] = bindings.frame()
    return results


def _union_problems(lists) -> tuple:
    """Each distinct run problem once over the replications (D1389): by code and block, the
    first replication's text kept (a replication keeps one per code and block already)."""
    found: dict = {}
    for problems in lists:
        for item in problems:
            found.setdefault((item.code, item.block), item)
    return tuple(found.values())


def label(results: Results, sim_settings) -> Results:
    """*results* with the level it was recorded at (D1395): the named level, else the one its two
    switches amount to; with a named level, what it left out is listed so a lookup says which
    level to re-run at."""
    named = loglevels.named_level(sim_settings)
    results.log_level = loglevels.label_of(sim_settings)
    results._not_recorded = loglevels.not_recorded(named) if named else {}
    return results


def session_results(frames: dict, layout) -> Results:
    """A `SimSession.finish()` dict (a game, a replayed game) as a labelled Results, as
    `run_model` gives one (D1395): a model that names its own level below full then says what
    its run did not record instead of a bare KeyError. The session lives in core, which
    imports nothing above it, so its callers label it here."""
    frames = dict(frames)
    live = frames.pop(LIVE_KEY, None)
    results = Results(frames)
    if live is not None:
        results.live_at_end = dict([live])
    sim = (layout.get("simulation") if isinstance(layout, dict) else None) or {}
    # The law checks read the layout (its own warm-up when _warmup is None): without it a
    # game would show no law warnings in Problems.
    results._layout = layout
    results._warmup = None
    return label(results, sim)


def live_counts(collected) -> dict | None:
    """The per-replication count of entities still inside, gathered from the replications' KPI
    dicts (D1395), or None when no replication carried one (no named level below full)."""
    found = dict(kpi[LIVE_KEY] for kpi in collected if LIVE_KEY in kpi)
    return found or None


def frames_with_rows(frames: dict, keep_empty: bool = False) -> Results:
    """The result frames `run_model` and `run_scenarios` return (D1321, D1328): only those with
    rows, "system" always; *keep_empty* keeps every frame. `Results._dropped` names what was left out."""
    dropped = () if keep_empty else tuple(
        name for name, frame in frames.items() if not len(frame) and name not in ALWAYS_KEPT)
    results = Results({name: frame for name, frame in frames.items() if name not in dropped})
    results._dropped = dropped
    return results


def summary_rows(results, *, hide_zero: bool = False) -> list[dict]:
    """One row per system KPI: ``metric``, ``key`` (``system/<metric>``), ``mean``, ``half_width``
    and ``ci95`` (the same 95% half-width under two names; ``half_width`` goes at the next minor
    release) and ``n``.

    *hide_zero* skips the rows `outputs.hidden_row` hides (D1251: the run's own facts, and a
    truncation flag or cost that is 0 in every replication; the printed summary; ``--json``
    keeps them all).

    ``n`` is the number of finite replication values behind the mean;
    ``half_width`` is NaN when that is below 2.
    """
    from nagasim.core.outputs import hidden_row
    from nagasim.core.stats import ci95
    from nagasim.experiments import flatten_kpis     # late: experiments imports this module

    flat = flatten_kpis(results)
    flat = flat[flat["KPI"].str.startswith("system/")]
    rows = []
    for kpi, grp in flat.groupby("KPI", sort=False):
        metric = kpi[len("system/"):]
        if hide_zero and hidden_row(metric, grp["Value"].tolist()):
            continue
        st = ci95(grp["Value"])
        # `key` and `ci95` are the names Python uses (D1226); `metric` and `half_width` stay until
        # the next minor release.
        rows.append({"metric": metric, "key": kpi, "mean": st["mean"],
                     "half_width": st["ub"] - st["mean"], "ci95": st["ub"] - st["mean"],
                     "n": int(grp["Value"].map(math.isfinite).sum())})
    return rows


def replications_needed(mean: float, half_width: float, n: int, rel: float = 0.05):
    """Replications for a 95% half-width of *rel* x |mean|, scaling this run's
    half-width by 1/sqrt(n); None when the half-width is NaN or the mean is 0.
    (A first-order estimate: the t quantile also shrinks as n grows.)"""
    if math.isnan(half_width) or math.isnan(mean) or mean == 0:
        return None
    return math.ceil(n * (half_width / (rel * abs(mean))) ** 2)


def _precision_target(rows: list[dict]):
    """The row the 'replications for +/-5%' line is about: Avg Throughput Time,
    else the first row with a usable half-width. None when there is none."""
    usable = [r for r in rows
              if replications_needed(r["mean"], r["half_width"], r["n"]) is not None]
    return next((r for r in usable if r["metric"] == "Avg Throughput Time"),
                usable[0] if usable else None)


def _num(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x:.4g}"


def _summary_text(rows: list[dict]) -> list[str]:
    width = max([len("Metric"), *(len(r["metric"]) for r in rows)])
    lines = [f"{'Metric':<{width}}  {'Mean':>12}  {'+/-95%':>12}  {'n':>4}"]
    lines += [f"{r['metric']:<{width}}  {_num(r['mean']):>12}  {_num(r['half_width']):>12}  "
              f"{r['n']:>4}" for r in rows]
    return lines


def _json_safe(value):
    """NaN has no JSON spelling: null, which a strict reader accepts."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def main(argv=None) -> int:
    # KPI labels and log messages are kept ASCII-only (tests/test_hygiene.py), but a
    # plain Windows console still defaults to cp1252 for anything a dependency prints
    # (e.g. a Unicode path). Tests force PYTHONIOENCODING=utf-8 (see conftest.py), but
    # a real user running the CLI -- especially the frozen build -- may not have that set.
    setup_cli()
    from nagasim import netguard
    from nagasim.viewer import ASKED_HELP, read_asked     # late: viewer builds on this module

    parser = argparse.ArgumentParser(prog="naga run", description="Run a Naga model headless.")
    parser.add_argument("--version", action="version", version=f"nagasim {__version__}")
    add_model_argument(parser)
    parser.add_argument("--replications", type=int, default=None,
                        help="default: the model's own 'simulation.replications'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--warmup", type=float, default=None,
                        help="default: the model's own 'simulation.warmup'")
    parser.add_argument("--run-length", type=float, default=None,
                        help="0 = run until the queue drains; default: the model's own "
                             "'simulation.time_limit'")
    parser.add_argument("--start", default=None, metavar="ISO",
                        help="real instant model time 0 stands for, e.g. 2026-01-05T06:00:00 "
                             "; overrides the model's own 'simulation.calendar.start'")
    parser.add_argument("--until", default=None, metavar="ISO",
                        help="run until this real instant instead of a length; needs a calendar "
                             "start, from the model or from --start")
    parser.add_argument("--timezone", default=None, metavar="IANA",
                        help="IANA timezone for --start/--until and for weekday schedules "
                             "(default: the model's own, else UTC)")
    parser.add_argument("--workers", type=_workers_type, default=1,
                        help="replications to run in parallel processes, or 'auto' (default: 1)")
    parser.add_argument("--logic", default=None, metavar="MODULE",
                        help="take LOGIC block classes from this importable module first "
                             "(a dotted name on PYTHONPATH), then from nagasim.core.custom_logic")
    add_controllers_argument(parser)
    parser.add_argument("--pace", type=_pace_type, default=None, metavar="FACTOR",
                        help="keep pace with the wall clock: FACTOR model seconds per wall "
                             "second, converted from the model's time unit (1 = real time, "
                             "60 = one model minute per second); late events are counted, "
                             "and the results are the unpaced run's")
    parser.add_argument("--out", help="write the 10-sheet results workbook here")
    parser.add_argument("--report", help="write an HTML report here")
    parser.add_argument("--export", action="append", default=[], metavar="FORMAT:PATH",
                        help="write with a registered exporter, e.g. "
                             "json:out.json; repeatable. See nagasim.exporters.EXPORTER_REGISTRY "
                             "for the built-in formats (xlsx/csv/html/json/player) plus any "
                             "plugin ones")
    parser.add_argument("--player", metavar="PATH",
                        help="write a shareable HTML player of the first replication here: one "
                             "self-contained file that replays the run in a browser, with no "
                             "server (the same as --export player:PATH)")
    parser.add_argument("--asked", metavar="FILE",
                        help="with --player or --export player:PATH: " + ASKED_HELP)
    parser.add_argument("--lang", choices=("en", "it"), default="en",
                        help="with --player or --export player:PATH: the language of the page (default en)")
    parser.add_argument("--state", default=None, metavar="FILE",
                        help="start from this state document (`naga state take`): "
                             "the run's clock starts where the state was taken and its "
                             "statistics cover the run from there")
    parser.add_argument("--log-level", choices=loglevels.NAMES, default=None,
                        help="what the run records: kpi (the summary frames), series "
                             "(plus the time series), full (plus the per-event logs, the "
                             "default), or a profile: study = kpi, explore = full, replay = full "
                             "with the trace of the first replication recorded for --player and "
                             "--export; a frame below the level is left out, not empty")
    parser.add_argument("--no-logs", action="store_true",
                        help="do not record the per-event entity/resource/store logs: "
                             "faster and much smaller on a long run, and every summary statistic "
                             "is unchanged -- only the three flat log sheets come back empty "
                             "(not with --log-level, which leaves frames out instead)")
    parser.add_argument("--raw", action="store_true",
                        help="print the per-replication system frame instead of the summary")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print {kpis, checks, replications, log_level} as JSON instead of the "
                             "summary")
    parser.add_argument("--no-checks", action="store_true",
                        help="skip the law checks (the WARN lines)")
    args = parser.parse_args(argv)
    if args.no_logs and args.log_level:
        print("--no-logs and --log-level both set what the run records: give one of them")
        return 2
    level = args.log_level      # --no-logs stays the legacy switch, no level named (O4)

    try:        # imported before the run, so a bad module costs no run (as `naga doe` does)
        controllers = load_controllers(args.controllers)
    except ValueError as exc:
        print(f"--controllers: {exc}")
        return 2
    try:
        model = _with_calendar(args)
    except CalendarError as exc:
        print(f"--start/--until: {exc}")
        return 2
    if args.state:
        from nagasim import state as states     # late: `state` builds on the session
        doc, problems = states.read(args.state)
        if not problems:
            model, problems = states.start(model, doc)
        for found in problems:
            print(f"--state: {found.code}: {found.message}")
        if problems:
            return 1
    # --out, --report and --player are the registry's xlsx, html and player writers by name.
    exports = [*([f"xlsx:{args.out}"] if args.out else []),
               *([f"html:{args.report}"] if args.report else []), *args.export,
               *([f"player:{args.player}"] if args.player else [])]
    unknown = _unknown_exports(exports)         # before the run: a mistyped format costs no run (S7-11)
    if unknown:
        print(unknown)
        return 2
    asked = None
    if args.asked:
        if not any(spec.startswith("player:") for spec in exports):
            print("--asked: it feeds a player's trust card: pass --player PATH too")
            return 2
        try:            # read before the run, so a refused file costs no run
            asked = read_asked(args.asked)
        except netguard.GuardError as exc:
            print(f"--asked: {exc.code}: {exc.message}")
            return 2
    # D201: an exporter drawn from the run's trace needs the first replication recorded; the
    # replay profile records it whatever the exports (D1395, O2).
    recorder = TraceRecorder() if _needs_trace(exports) or level == "replay" else None
    try:
        results = run_model(model, replications=args.replications, seed=args.seed,
                            warmup=args.warmup, run_length=args.run_length, workers=args.workers,
                            record_logs=False if args.no_logs else None, log_level=level,
                            trace=recorder,
                            pace=args.pace, logic=args.logic, controllers=controllers)
    except (logic_authoring.LogicClassNotFound, ImportError) as exc:
        if isinstance(exc, ImportError) and not args.logic:
            raise       # only the --logic module's import is this command's to explain
        print(f"--logic: {exc}" if args.logic else str(exc))
        return 2
    except ItemCodeUntrusted as exc:        # D1344: a person trusts it first, nothing ran
        print(f"naga run: {exc}", file=sys.stderr)
        return 1
    if results is None:         # cut short by should_stop: nothing to show
        print("naga run: stopped, no results", file=sys.stderr)
        return 1
    if args.raw and not args.as_json:
        print(results["system"].to_string(index=False))
    else:
        from nagasim.runchecks import RunChecks
        rows = summary_rows(results, hide_zero=not args.as_json)
        target = _precision_target(rows)
        needed = None if target is None else replications_needed(
            target["mean"], target["half_width"], target["n"])
        found = RunChecks((), note="skipped") if args.no_checks else results.checks()
        if args.as_json:
            print(json.dumps(_json_safe({
                "kpis": rows, "checks": found.as_dict(), "log_level": results.log_level,
                **({"problems": [p.as_dict() for p in results.run_problems]}
                   if results.run_problems else {}),
                "replications": None if target is None else {
                    "metric": target["metric"], "n": target["n"], "needed": needed}}),
                indent=2, default=str))
        else:
            print("\n".join(_summary_text(rows)))
            # A fact of the run, not a result row (D1251): said once, in words, when the run has it.
            system = results["system"]
            started = system.loc[system["Metric"] == "Started From State At", "Value"]
            if len(started):
                print(f"started from the state taken at {started.iloc[0]:g}")
            for line in found.lines():
                print(f"WARN {line}")
            for item in results.run_problems:            # D1389: what the run raised while running
                # the code once: the kept message ends with it, as the log line does
                print(f"WARN {item.code}: {item.message.removesuffix(f' ({item.code})')}")
            if target is not None:
                print(f"replications for +/-5% on {target['metric']}: {needed}")
    if results.pace is not None:
        print(f"paced at {results.pace['paced']:g}x: {results.pace['late_events']} late events, "
              f"max lag {results.pace['max_lag_s']:.3f} s",
              file=sys.stderr if args.as_json else sys.stdout)
    # Last, and only for a NAMED level below full (it left something out): a legacy --no-logs or
    # record_logs: false run prints what it printed before levels existed (O4).
    if not (args.as_json or args.raw) and results._not_recorded:
        out = "the per-event logs" + (" and the time series" if "timeseries" in results._not_recorded else "")
        print(f"recorded at level {results.log_level}: {out} left out (--log-level full records them)")
    layout = model if isinstance(model, dict) else None
    for spec in exports:
        try:
            # "<fmt> written to" must not land inside the --json document
            with redirect_stdout(sys.stderr) if args.as_json else ExitStack():
                _run_export(spec, results, args.model, layout, recorder, asked, args.lang)
        except ValueError as exc:       # e.g. a player over its size limit
            print(f"--export: {exc}")
            return 1
    return 0


def _layout(layout, model_path):
    return layout if layout is not None else load_layout(model_path)


def _with_calendar(args):
    """The model to run: the path as given, or -- when `--start`/`--until`/
    `--timezone` are used (PHASE4 T10) -- a layout with its
    `simulation.calendar` overridden. Raises CalendarError on a bad date."""
    if not (args.start or args.until or args.timezone):
        return args.model
    layout = load_layout(args.model)
    sim = layout.setdefault("simulation", {})
    spec = dict(sim.get("calendar") or {})
    for key, value in (("start", args.start), ("end", args.until),
                       ("timezone", args.timezone)):
        if value:
            spec[key] = value
    sim["calendar"] = spec
    if args.until and not spec.get("start"):
        raise CalendarError("--until needs a calendar start (--start, or "
                            "simulation.calendar.start in the model)")
    if model_calendar(layout) is None:
        raise CalendarError("--timezone needs a calendar start (--start, or "
                            "simulation.calendar.start in the model)")
    if args.until:
        sim["time_limit"] = None       # the end date is the horizon now
    return layout


def _unknown_exports(specs) -> str:
    """The message for the first `--export` entry that is not FORMAT:PATH for a known exporter, or ""."""
    from nagasim.exporters import EXPORTER_REGISTRY
    from nagasim.plugins import load_plugins

    load_plugins()      # `python -m nagasim.api` has loaded none yet; a plugin's exporter is a format too
    for spec in specs:
        fmt, _, out_path = spec.partition(":")
        if fmt not in EXPORTER_REGISTRY or not out_path:
            return (f"--export: '{spec}' is not FORMAT:PATH for a known exporter "
                    f"({', '.join(sorted(EXPORTER_REGISTRY))})")
    return ""


def _needs_trace(specs) -> bool:
    """Whether a `--export FORMAT:PATH` entry names an exporter drawn from the run's
    recorded trace (D201)."""
    from nagasim.exporters import EXPORTER_REGISTRY

    found = (EXPORTER_REGISTRY.get(spec.partition(":")[0]) for spec in specs)
    return any(exporter is not None and exporter.needs_trace for exporter in found)


def _run_export(spec: str, results, model_path: str, layout=None, trace=None, asked=None,
                language=None) -> None:
    """One `--export FORMAT:PATH` entry (ROADMAP 2.4 C): FORMAT is a key in
    `nagasim.exporters.EXPORTER_REGISTRY` (built-in or plugin-added). *asked* (`--asked`)
    reaches the player only, and so does *language* (`--lang`, D1056)."""
    from nagasim.exporters import EXPORTER_REGISTRY

    fmt, _, out_path = spec.partition(":")
    exporter = EXPORTER_REGISTRY.get(fmt)
    if exporter is None or not out_path:
        print(f"--export: '{spec}' is not FORMAT:PATH for a known exporter "
             f"({', '.join(sorted(EXPORTER_REGISTRY))})")
        return
    # Only an exporter that asks for the trace is given it (D201): a plugin's writer
    # may take no keyword it does not know.
    extra = {"trace": trace} if exporter.needs_trace else {}
    if asked is not None and fmt == "player":
        extra["asked"] = asked
    layout = _layout(layout, model_path)
    if fmt == "player":
        from nagasim.player import disk_pictures     # D1052: the person's own command reads the plan
        extra["pictures"] = disk_pictures(layout, model_path)
        if language:
            extra["language"] = language
    exporter.write(results, out_path, layout=layout,
                   title=Path(model_path).stem, **extra)
    print(f"{exporter.name} written to {out_path}")


def _pace_type(value: str) -> float:
    try:
        pace = float(value)
    except ValueError:
        pace = 0.0
    if not 0 < pace < float("inf"):
        raise argparse.ArgumentTypeError(f"must be a positive number, not {value}")
    return pace


def _workers_type(value: str) -> int | str:
    return value if value == "auto" else int(value)


if __name__ == "__main__":
    raise SystemExit(main())
