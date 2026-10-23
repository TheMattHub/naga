"""Headless entry point: run a model without an editor (D17).

    from nagasim.api import run_model
    results = run_model("models/single_server.json", replications=5, run_length=1000)

Also a CLI:  PY -m nagasim.api model.json --replications 5 --run-length 1000 --out results.xlsx
"""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import ExitStack, contextmanager
from pathlib import Path

from nagasim import __version__
from nagasim._cli import add_model_argument, setup_cli
from nagasim.core import initial_state, schema
from nagasim.core.calendar import CalendarError, model_calendar, run_horizon
from nagasim.core.export import concat_runs
from nagasim.core.schema import ModelValidationError
from nagasim.core.sim_runner import Pacer, run_single_simulation
from nagasim.core.timeunits import UNITS, units_of
from nagasim.core.trace import TraceRecorder
from nagasim.plugins import load_plugins
from nagasim.results import Results


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


def _pool_replication(task):
    """One replication in a worker process: `(KPIs, what it logged)`.

    `task` is (layout, seed, run_len, warmup, controllers). Module level and
    picklable on purpose (ROADMAP 1.6): a ProcessPoolExecutor pickles its
    callable by qualified name, so a closure or a bound method cannot be the
    pool task. `layout` may equally be a path -- both are what
    `run_single_simulation` accepts, and both pickle. *controllers*
    (ROADMAP 3.7 A) must pickle too -- `run_model` checks that before ever
    reaching a pool.

    A worker process has no console of its own once the editor is started from
    an icon, and no handler of the parent's either, so its warnings travel back
    with the result and `run_jobs` logs them again in the parent."""
    layout, seed, run_len, warmup, controllers = task
    handler = _Collect()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        # ROADMAP 2.4 B: on Windows (spawn) each pool worker is a fresh
        # interpreter -- plugin blocks/distributions registered in the parent
        # process are not there until this runs again here.
        load_plugins()
        kpis = run_single_simulation(layout, seed=seed, run_len=run_len, warmup=warmup,
                                     controllers=controllers)
        return kpis, handler.records
    finally:
        root.removeHandler(handler)


# What a process pool costs before it returns a single result: worker start-up
# plus the pickling of the first tasks. SPAWN (Windows, macOS) starts a fresh
# interpreter per worker that re-imports pandas, scipy and simpy: measured
# around 0.8 s for 2 workers and 2.5 s for 20, so 3 s is the round number above
# both. FORK (the Linux default) copies the parent instead and re-imports
# nothing, which is roughly an order of magnitude cheaper -- charging it the
# spawn price kept small-but-real workloads sequential on Linux (PHASE4 T9).
_SPAWN_SETUP_COST = 3.0
_FORK_SETUP_COST = 0.3
# The most workers `auto` will ever ask for. No core is held back for the
# caller: while the pool runs, the parent only waits on results.
_MAX_AUTO_WORKERS = 4


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


def run_settings(path, replications=None, warmup=None, run_length=None):
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
    with ProcessPoolExecutor(max_workers=workers) as pool:
        yield pool


def run_jobs(jobs, *, pool=None, workers: int = 1, on_progress=None, should_stop=None,
             done: int = 0, total: int | None = None, controllers=None) -> dict[object, list]:
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
    """
    jobs = iter(jobs)
    collected: dict[object, list] = {}
    count = done

    if pool is None:
        for tag, layout, seed, run_len, warm in jobs:
            if should_stop is not None and should_stop():
                break
            collected.setdefault(tag, [])
            kpi = run_single_simulation(layout, seed=seed, run_len=run_len, warmup=warm,
                                        should_stop=should_stop, controllers=controllers)
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
        while True:
            while not exhausted and len(pending) < window:
                task = next(jobs, None)
                if task is None:
                    exhausted = True
                    break
                tag, layout, seed, run_len, warm = task
                collected.setdefault(tag, [])
                future = pool.submit(_pool_replication,
                                     (layout, seed, run_len, warm, controllers))
                futures[future] = (tag, seed)
                pending.add(future)
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
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
    return {tag: sorted(pairs, key=lambda pair: pair[0]) for tag, pairs in collected.items()}


def _run_pool(path, seeds, run_length, warmup, workers, on_progress, should_stop, total, done,
             controllers=None, pool=None):
    """Run *seeds* across a process pool; returns (seed, kpis) pairs.

    *pool*, when given, is a caller-owned `ProcessPoolExecutor` (see
    `scenario_pool`) that outlives this call; otherwise one is built here and
    shut down at the end, exactly as before. The submission itself is
    `run_jobs` with one tag.
    """
    with ExitStack() as stack:
        if pool is None:
            pool = stack.enter_context(ProcessPoolExecutor(max_workers=workers))
        out = run_jobs(((None, path, s, run_length, warmup) for s in seeds), pool=pool,
                       workers=workers, on_progress=on_progress, should_stop=should_stop,
                       done=done, total=total, controllers=controllers)
    return out.get(None, [])


def run_model(path, *, replications: int | None = None, seed: int = 0, warmup: float | None = None,
              run_length: float | None = None, workers: int | str = 1, on_progress=None,
              should_stop=None, trace=None, bind_data: bool = False, base_dir=None,
              network=None, confine_to=None, feed=None,
              controllers: dict | None = None, pool=None,
              record_logs: bool | None = None, deterministic: bool | None = None,
              pace: float | None = None) -> Results | None:
    """Run replications of a model and return its KPI frames.

    Replication ``r`` is seeded with ``seed + r``, so a run is reproducible.

    Parameters
    ----------
    path : str or dict
        A model file path or an already-loaded layout dict (nothing is written to
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
    pool : ProcessPoolExecutor, optional
        A pool the caller owns and reuses across calls (see `scenario_pool`), so
        a study pays the process start-up once. Results are identical.
    record_logs : bool, optional
        False switches the per-event logs off: the log frames keep their columns
        and have no rows, and every summary statistic is untouched. None keeps
        the model's own setting (default: on).
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

    Returns
    -------
    Results or None
        A `Results` (a ``dict[str, pandas.DataFrame]``) of the KPI frames
        concatenated over the completed replications, each tagged with a ``Run``
        column, with ``.kpis()``, ``.kpi(key)``, ``.plot(key)`` and
        ``.to_excel(path)`` on top. None if no replication completed.

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

    pacer = None
    if pace is not None:
        if workers != 1:
            raise ValueError(f"a paced run keeps one clock, so its replications run one "
                             f"after another: workers must be 1, not {workers!r}")
        unit = units_of((path if isinstance(path, dict) else load_layout(path)).get("simulation")).time
        pacer = Pacer(pace, UNITS.get(unit, 1))

    if controllers and workers != 1:
        import pickle
        for name, controller in controllers.items():
            try:
                pickle.dumps(controller)
            except Exception as exc:
                raise ValueError(
                    f"controller '{name}' is not picklable, but workers={workers!r} may spread "
                    f"replications across processes (each needs its own copy): run with "
                    f"workers=1, or make the controller picklable ({exc})") from exc

    if record_logs is not None:
        # PHASE4 T7. A run setting, so it travels to a worker with the layout
        # and no task shape changes: `simulation.record_logs` is read once per
        # replication, exactly like `entity_log_limit`.
        path = with_run_setting(path, "record_logs", bool(record_logs))

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

    seeds = [seed + i for i in range(replications)]
    collected: list[tuple[int, dict]] = []

    if workers == "auto":
        if should_stop is not None and should_stop():
            return None
        subscribers = (trace,) if trace is not None else ()
        t0 = time.perf_counter()
        kpi0 = run_single_simulation(path, seed=seeds[0], run_len=run_length, warmup=warmup,
                                     should_stop=should_stop, subscribers=subscribers,
                                     controllers=controllers)
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
                                     controllers=controllers, pacer=pacer)
        if kpi is None:  # cut short mid-replication
            stopped = True
            break
        collected.append((s, kpi))
        if on_progress is not None:
            on_progress(len(collected), replications)

    if pooled and not stopped:
        collected.extend(_run_pool(path, pooled, run_length, warmup, workers,
                                   on_progress, should_stop, replications, len(collected),
                                   controllers=controllers, pool=pool))

    collected.sort(key=lambda pair: pair[0])   # pool order is arrival order, not seed order
    if not collected:
        return None
    results = Results(concat_runs([kpi for _, kpi in collected]))
    if pacer is not None:
        results.pace = pacer.summary()
        log.info("Paced at %gx: %d late events, max lag %.3f s", pacer.pace,
                 pacer.late_events, pacer.max_lag_s)
    if bindings is not None:
        results["data_inputs"] = bindings.frame()
    return results


def main(argv=None) -> int:
    # KPI labels and log messages are kept ASCII-only (tests/test_hygiene.py), but a
    # plain Windows console still defaults to cp1252 for anything a dependency prints
    # (e.g. a Unicode path). Tests force PYTHONIOENCODING=utf-8 (see conftest.py), but
    # a real user running the CLI -- especially the frozen build -- may not have that set.
    setup_cli()

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
    parser.add_argument("--state", default=None, metavar="FILE",
                        help="start from this state document (`naga state take`): "
                             "the run's clock starts where the state was taken and its "
                             "statistics cover the run from there")
    parser.add_argument("--no-logs", action="store_true",
                        help="do not record the per-event entity/resource/store logs: "
                             "faster and much smaller on a long run, and every summary statistic "
                             "is unchanged -- only the three flat log sheets come back empty")
    args = parser.parse_args(argv)

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
    # D201: an exporter drawn from the run's trace needs the first replication recorded.
    # (`python -m nagasim.api` loads no plugins before this: a plugin's needs_trace is not seen)
    recorder = TraceRecorder() if _needs_trace(exports) else None
    results = run_model(model, replications=args.replications, seed=args.seed,
                        warmup=args.warmup, run_length=args.run_length, workers=args.workers,
                        record_logs=False if args.no_logs else None, trace=recorder,
                        pace=args.pace)
    print(results["system"].to_string(index=False))
    if results.pace is not None:
        print(f"paced at {results.pace['paced']:g}x: {results.pace['late_events']} late events, "
              f"max lag {results.pace['max_lag_s']:.3f} s")
    layout = model if isinstance(model, dict) else None
    for spec in exports:
        try:
            _run_export(spec, results, args.model, layout, recorder)
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


def _needs_trace(specs) -> bool:
    """Whether a `--export FORMAT:PATH` entry names an exporter drawn from the run's
    recorded trace (D201)."""
    from nagasim.exporters import EXPORTER_REGISTRY

    found = (EXPORTER_REGISTRY.get(spec.partition(":")[0]) for spec in specs)
    return any(exporter is not None and exporter.needs_trace for exporter in found)


def _run_export(spec: str, results, model_path: str, layout=None, trace=None) -> None:
    """One `--export FORMAT:PATH` entry (ROADMAP 2.4 C): FORMAT is a key in
    `nagasim.exporters.EXPORTER_REGISTRY` (built-in or plugin-added)."""
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
    exporter.write(results, out_path, layout=_layout(layout, model_path),
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
