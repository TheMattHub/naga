from __future__ import annotations

from nagasim.core.schema import ModelValidationError
from concurrent.futures import ProcessPoolExecutor
from nagasim import api, experiments
from nagasim.core.export import concat_runs
import math
import numpy as np
from nagasim.core import schema
import weakref

# ---------------------------------------------------------------- evaluation
class _OutOfBudget(Exception):
    """Raised inside the evaluator when a call would spend more replications
    than the caller's budget still allows. Never escapes this module."""


def _consecutive(seeds) -> list[tuple[int, int]]:
    """(start, count) blocks of consecutive seeds, so one `api.run_model` call
    covers a whole block instead of one call per replication."""
    blocks: list[tuple[int, int]] = []
    for seed in sorted(seeds):
        if blocks and seed == blocks[-1][0] + blocks[-1][1]:
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + 1)
        else:
            blocks.append((seed, 1))
    return blocks


def study_layout(layout, kpis=(), record_logs=None) -> dict:
    """*layout* with `simulation.record_logs` settled for a study: the ONE place
    every study entry point (optimize, DOE, selection, sobol, tornado,
    calibrate) resolves it.

    An explicit *record_logs* from the caller wins. Otherwise the per-event logs
    are off (PHASE4 T7: they cost 10-14% of a run, 9x the KPI computation and
    most of the result a worker sends back) unless one of the *kpis* the study
    names is computed from them (`experiments.needs_event_logs`) -- a study
    never gets a silently empty column. That overrides a model that has the
    logs off itself, because the study's need is the more specific request.
    """
    if record_logs is None:
        record_logs = experiments.needs_event_logs(kpis)
    return api.with_run_setting(layout, "record_logs", bool(record_logs))


def _candidate_layout(layout, overrides) -> dict:
    """The layout of one design point, refused if the overrides break it."""
    candidate = experiments.apply_overrides(layout, overrides)
    fatal = schema.fatal_problems(schema.validate_model_structured(candidate))  # by severity (#247)
    if fatal:
        raise ModelValidationError(schema.format_problems(fatal))
    return candidate


def _values_from_frames(frames, extra=None) -> dict[str, dict[int, float]]:
    """{KPI: {seed: value}} from one point's concatenated frames (Run == seed)."""
    values: dict[str, dict[int, float]] = {}
    flat = experiments.flatten_kpis(frames)
    for run, kpi, value in zip(flat["Run"], flat["KPI"], flat["Value"]):
        values.setdefault(kpi, {})[int(run)] = float(value)
    for kpi, by_seed in (extra(frames) if extra is not None else {}).items():
        values.setdefault(kpi, {}).update(by_seed)
    return values


def _evaluate_point(layout, overrides, seeds, *, run_length=None, warmup=None, workers=1,
                    extra=None, pool=None):
    """Run ONE point of the factor space on exactly *seeds*: {KPI: {seed: value}}.

    The single seam everything in this module evaluates through, so a test can
    swap the simulation for a synthetic response without touching the
    procedures being tested.

    *extra*, when given, is `fn(frames) -> {KPI: {seed: value}}`: KPI keys
    derived from the raw result frames rather than from `flatten_kpis`.
    `nagasim.calibrate` uses it to add its own objective and the per-replication
    KS statistics its distributional targets need, so calibration reuses this
    evaluator (and `optimize`'s whole search) instead of duplicating either.
    """
    candidate = _candidate_layout(layout, overrides)
    values: dict[str, dict[int, float]] = {}
    for start, count in _consecutive(seeds):
        frames = api.run_model(candidate, replications=count, seed=start,
                               run_length=run_length, warmup=warmup, workers=workers, pool=pool)
        if frames is None:
            continue
        for kpi, by_seed in _values_from_frames(frames, extra).items():
            values.setdefault(kpi, {}).update(by_seed)
    return values


def _evaluate_points(layout, requests, *, run_length=None, warmup=None, workers=1,
                     extra=None, pool=None, on_progress=None, should_stop=None, total=None):
    """Run SEVERAL points as ONE wave of (point, replication) tasks (PHASE4 T3).

    *requests* is `[(key, overrides, seeds)]`; returns `{key: {KPI: {seed:
    value}}}`. Without a pool this is `_evaluate_point` per point -- which
    keeps that function the single seam a test can swap for a synthetic
    response -- and with one, every replication of every point is in flight
    together, so a design with ONE replication per point fills the workers just
    as a 20-replication one does. The numbers are the same either way: a
    replication is a pure function of (layout, seed).

    The layouts are built lazily, inside the generator `api.run_jobs` pulls
    from, so a 500-point Latin hypercube never holds 500 of them at once.

    A point that was cut short -- by `should_stop`, either way -- is LEFT OUT
    of the result rather than reported with holes in it, so a caller can tell a
    finished point from an abandoned one.
    """
    out: dict = {}
    if pool is None:
        count = 0
        for key, overrides, seeds in requests:
            if should_stop is not None and should_stop():
                break
            out[key] = _evaluate_point(layout, overrides, seeds, run_length=run_length,
                                       warmup=warmup, workers=workers, extra=extra)
            count += len(seeds)
            if on_progress is not None:
                on_progress(count, total)
        return out

    def jobs():
        for key, overrides, seeds in requests:
            candidate = _candidate_layout(layout, overrides)
            # Resolved per point: an override may move `simulation.time_limit`
            # itself, and a task goes straight to a worker without run_model.
            _reps, warm, run_len = api.run_settings(candidate, 1, warmup, run_length)
            for seed in seeds:
                yield (key, candidate, seed, run_len, warm)

    collected = api.run_jobs(jobs(), pool=pool, workers=workers, on_progress=on_progress,
                             should_stop=should_stop, total=total)
    for key, _overrides, seeds in requests:
        pairs = collected.get(key) or []
        if len(pairs) == len(seeds):
            out[key] = _values_from_frames(concat_runs([kpi for _, kpi in pairs]), extra)
    return out


# How far two replications of one point may differ, relative to their own
# size, and still count as the same number. Float replay noise, not randomness.
_DETERMINISM_EPS = 1e-12


def _seed_independent(values: dict) -> bool:
    """Whether every KPI of one point came out the SAME on every replication
    it was just run for (DEVLOG #96).

    A model with no random draw left in it -- a purely continuous plant, an
    inventory on a fixed demand profile, an energy site on measured series --
    answers the same thing whatever the seed, and replicating it is pure cost.
    One KPI that moved is enough to settle the question the other way, so this
    is strict in the safe direction.
    """
    for by_seed in values.values():
        series = list(by_seed.values())
        if len(series) < 2:
            continue
        first = series[0]
        for value in series[1:]:
            if value != first:
                scale = max(abs(first), abs(value))
                if not scale or abs(value - first) / scale > _DETERMINISM_EPS:
                    return False
    return True


class _Evaluator:
    """Cached, budget-counting access to a point's per-replication KPI values.

    Keyed on the caller's own point key (the coerced factor values, or a
    candidate name), so the optimiser's no-revisit cache and the final
    selection share one set of replications: asking again for seeds already run
    costs nothing and spends no budget. `budget` is a hard cap in replications
    -- a call that would exceed it raises `_OutOfBudget` instead of running.

    DETERMINISTIC MODELS (DEVLOG #96). The first point evaluated settles, from
    its own replications, whether this model's outputs depend on the seed at
    all. If they do not, every later point is simulated ONCE and its value
    stands for every replication asked for -- so the budget buys design points
    instead of identical repeats, and the procedures above keep working
    unchanged on a sample whose variance is exactly 0. `deterministic` is None
    until that first answer, and is never revisited: a model either draws
    random numbers or it does not.
    """

    def __init__(self, layout, *, run_length=None, warmup=None, workers=1, budget=None,
                 extra=None, pool=None, record_logs=None, kpis=()):
        self.layout = study_layout(layout, kpis, record_logs)
        self.run_length = run_length
        self.warmup = warmup
        self.workers = workers
        self.budget = budget
        self.extra = extra          # see `_evaluate_point`
        self.used = 0
        self.deterministic = None
        self._cache: dict = {}
        self._pool = pool           # a caller-owned pool is never shut down here

    def pool(self):
        """ONE process pool for the whole search (PHASE4 A4), built on first
        use unless the caller passed its own: a DOE or an optimisation submits
        every (design point, replication) task through it, and a fresh pool per
        call costs seconds of interpreter start-up each on Windows spawn.

        ponytail: the pool is shut down by a `weakref.finalize` when this
        evaluator is collected -- which under CPython is when the search
        function that owns it returns. Making the four call sites context
        managers instead would re-indent four long function bodies for the same
        effect.
        """
        if self._pool is None and isinstance(self.workers, int) and self.workers > 1:
            self._pool = ProcessPoolExecutor(max_workers=self.workers)
            weakref.finalize(self, self._pool.shutdown, wait=False)
        return self._pool

    @property
    def remaining(self) -> float:
        return math.inf if self.budget is None else max(self.budget - self.used, 0)

    def missing(self, key, seeds) -> list[int]:
        """What asking for *seeds* would actually cost, in replications -- so
        at most one of them on a model whose outputs do not depend on the seed,
        because that is all `values` would simulate."""
        _store, have = self._cache.get(key, ({}, set()))
        todo = [s for s in seeds if s not in have]
        return todo[:1] if self.deterministic else todo

    def _held(self, key, seeds) -> dict[str, np.ndarray]:
        store, _have = self._cache.setdefault(key, ({}, set()))
        return {kpi: np.array([by_seed.get(s, np.nan) for s in seeds], dtype=float)
                for kpi, by_seed in store.items()}

    def _wave(self, requests, out, **kwargs) -> bool:
        """Run, as ONE wave of tasks, the points of *requests* the budget still
        pays for -- in order, stopping at the first that does not fit. Fills
        *out* with `{key: {KPI: array}}` for the points it settled and returns
        whether all of them fitted.

        A key that appears twice in one wave -- a Latin hypercube proposing two
        points that round to the same integer grid cell, say -- is planned ONCE
        for the union of its seeds, so the no-revisit guarantee holds inside a
        wave exactly as the cache gives it between waves.
        """
        plan: dict = {}             # key -> [overrides, todo, run]
        priced: list = []           # (key, seeds) per request, in request order
        projected = 0
        complete = True
        for key, overrides, seeds in requests:
            _store, have = self._cache.setdefault(key, ({}, set()))
            entry = plan.get(key)
            already = have if entry is None else have.union(entry[1])
            todo = [s for s in seeds if s not in already]
            run = todo[:1] if (self.deterministic and entry is None) else todo
            if projected + len(run) > self.remaining:
                complete = False
                break
            projected += len(run)
            if entry is None:
                plan[key] = [overrides, list(todo), list(run)]
            else:
                entry[1].extend(todo)
                entry[2].extend(run)
            priced.append((key, seeds))

        fresh = _evaluate_points(self.layout,
                                 [(key, overrides, run)
                                  for key, (overrides, _todo, run) in plan.items() if run],
                                 run_length=self.run_length, warmup=self.warmup,
                                 workers=self.workers, extra=self.extra, pool=self.pool(),
                                 **kwargs)
        settled = set()
        for index, (key, (_overrides, todo, run)) in enumerate(plan.items()):
            store, have = self._cache[key]
            found = fresh.get(key)
            if run and not found:
                complete = False        # cut short: this point and the rest never ran
                break
            found = found or {}
            if index == 0 and self.deterministic is None and len(run) > 1:
                self.deterministic = _seed_independent(found)
            for kpi, by_seed in found.items():
                held = store.setdefault(kpi, {})
                held.update(by_seed)
                if self.deterministic and by_seed:
                    repeated = next(iter(by_seed.values()))
                    for seed in todo:
                        held.setdefault(seed, repeated)
            have.update(todo)
            self.used += len(run)
            settled.add(key)
        for key, seeds in priced:
            if key in settled:
                out[key] = self._held(key, seeds)
        return complete

    def values(self, key, overrides, seeds) -> dict[str, np.ndarray]:
        """{KPI: values in *seeds* order}; runs only the seeds not already held
        -- and, on a model whose outputs do not depend on the seed, only one of
        them, repeated for the rest."""
        out: dict = {}
        if not self._wave([(key, overrides, seeds)], out):
            raise _OutOfBudget(f"{len(self.missing(key, seeds))} replications needed, "
                               f"{self.remaining} left")
        return out[key]

    def values_many(self, requests, **kwargs) -> dict:
        """`values` for SEVERAL independent points at once (PHASE4 T3).

        `[(key, overrides, seeds)] -> {key: {KPI: array}}`, leaving out the
        points the budget could not pay for -- the cap is as hard as ever, it
        is just applied to the whole wave, in the order given.

        The FIRST point of a search gets a wave of its own, because whether
        this model depends on the seed at all (DEVLOG #96) is settled from it
        and decides how many replications every later point costs.
        """
        requests = list(requests)
        out: dict = {}
        if self.deterministic is None and requests:
            if not self._wave(requests[:1], out, **kwargs):
                return out
            requests = requests[1:]
        self._wave(requests, out, **kwargs)
        return out


def _response(values: dict, key: str) -> np.ndarray:
    arr = values.get(key)
    if arr is None:
        raise ValueError(f"unknown response KPI: {key!r}")
    return arr

