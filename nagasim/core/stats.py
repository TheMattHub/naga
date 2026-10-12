"""Statistics collectors and confidence intervals.

Engine-side module: numpy/scipy only, no pandas, no UI toolkit. Collectors are
attached to resources and stores (ARCHITECTURE 10.3 rule 4); block bodies never
compute statistics themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy import stats as _sp_stats

__all__ = ["TimeWeighted", "Tally", "Counter", "UserStat", "USER_STAT_KINDS", "RECORD_KINDS", "ci95",
           "paired_diff_ci", "welch_diff_ci", "wilson_interval", "EntityTypeStats",
           "DueDateStats", "account_and_retain", "account_due_date", "created_in_window"]


def created_in_window(entity, warmup: float) -> bool:
    """Whether *entity* counts as created in the observed window (DEVLOG #18, #22).

    A warm-started entity (ROADMAP 3.4b) carries a NEGATIVE `creation_time` --
    minus the age it already had when the run began -- yet it entered the model
    at t=0, so the test clamps at 0. An ordinary entity's creation time is never
    negative, so nothing else changes.
    """
    return entity.creation_time is not None and max(entity.creation_time, 0.0) >= warmup


class TimeWeighted:
    """Time-weighted accumulator for a piecewise-constant level (queue length,
    units in use, store size).

    `integral` is the area under the level curve since the last `reset`.
    `time_avg(t_end)` divides that area by the observed window.
    """

    def __init__(self, t: float = 0.0, level: float = 0.0):
        self.reset(t, level)

    def reset(self, t: float, level: float | None = None) -> None:
        """Restart the accumulators at time *t*, keeping the current level
        (warm-up is a statistics reset, not a state reset)."""
        if level is not None:
            self.level = level
        self.start = t
        self.last_t = t
        self.integral = 0.0
        self._max = 0.0

    @property
    def max(self) -> float:
        """Highest level actually held for a positive duration.

        A request that is enqueued and granted at the same instant never really
        made the queue one longer, so zero-width levels do not count; the level
        standing right now does, since it has been held since the last change.
        """
        return max(self._max, self.level)

    def _advance(self, t: float) -> None:
        dt = t - self.last_t
        if dt <= 0:
            return
        self.integral += self.level * dt
        if self.level > self._max:
            self._max = self.level
        self.last_t = t

    def set(self, level: float, t: float) -> None:
        self._advance(t)
        self.level = level

    def add(self, delta: float, t: float) -> None:
        # `set(self.level + delta, t)` with `_advance` written out: two calls
        # saved on every queue and busy change of every resource.
        level = self.level
        dt = t - self.last_t
        if dt > 0:
            self.integral += level * dt
            if level > self._max:
                self._max = level
            self.last_t = t
        self.level = level + delta

    def time_avg(self, t_end: float) -> float:
        """Mean level over [start, t_end]. Returns 0.0 for an empty window."""
        span = t_end - self.start
        if span <= 0:
            return 0.0
        return (self.integral + self.level * (t_end - self.last_t)) / span

    def area(self, t_end: float) -> float:
        """Integral extended to *t_end* at the current level."""
        return self.integral + self.level * (t_end - self.last_t)


class Tally:
    """Observation-based accumulator (waiting times, residence times)."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.values: list[float] = []

    def add(self, x: float) -> None:
        self.values.append(float(x))

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return float(np.mean(self.values)) if self.values else np.nan

    @property
    def min(self) -> float:
        return float(np.min(self.values)) if self.values else np.nan

    @property
    def max(self) -> float:
        return float(np.max(self.values)) if self.values else np.nan

    @property
    def std(self) -> float:
        """Sample standard deviation (ddof=1); NaN for fewer than 2 values."""
        if len(self.values) < 2:
            return np.nan
        return float(np.std(self.values, ddof=1))

    def percentile(self, q: float) -> float:
        return float(np.percentile(self.values, q)) if self.values else np.nan


class Counter:
    """Running total with a reset (RECORD block's "count" kind, ROADMAP 1.3).

    `total` is the sum of every increment, `n` how many times `add` was
    called -- so a RECORD with an expression increment reports both the
    total and the number of hits.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.total = 0.0
        self.n = 0

    def add(self, x: float = 1.0) -> None:
        self.total += float(x)
        self.n += 1


USER_STAT_KINDS = ("count", "tally", "time_between", "time_persistent")
# RECORD's own `kind` list: the four collector kinds above, plus "cost"
# (PHASE4 D5), which does not build a UserStat at all -- it accrues into
# `ctx.cost_items` (`core/costs.py`) and is reported in `cost_summary`.
RECORD_KINDS = USER_STAT_KINDS + ("cost",)


@dataclass
class UserStat:
    """One user-defined statistic (RECORD block, ROADMAP 1.3), shared by every
    RECORD writing to the same `name` -- core/schema/ rejects two blocks sharing a
    name with different `kind`s before the run starts, so this class trusts
    that the kind never changes.

    `collector` is a Counter ("count"), a Tally ("tally"/"time_between") or a
    TimeWeighted ("time_persistent"). `last_arrival` is time_between's own
    running state: not part of any collector, and not reset at warm-up (a
    still-running gap survives the reset instant, same as a resource that is
    mid-service when warm-up ends).
    """

    name: str
    kind: str
    collector: object
    last_arrival: float | None = None

    @staticmethod
    def new(name: str, kind: str, t: float = 0.0) -> "UserStat":
        if kind == "count":
            collector = Counter()
        elif kind in ("tally", "time_between"):
            collector = Tally()
        elif kind == "time_persistent":
            collector = TimeWeighted(t)
        else:
            raise ValueError(f"unknown user statistic kind: {kind!r}")
        return UserStat(name=name, kind=kind, collector=collector)

    def reset(self, t: float) -> None:
        """Warm-up hook (ROADMAP 1.3 point 3): a TimeWeighted keeps its
        current level, everything else starts clean."""
        if self.kind == "time_persistent":
            self.collector.reset(t)
        else:
            self.collector.reset()


@dataclass
class EntityTypeStats:
    """Streaming per-entity-type accumulator (ROADMAP 2.5, DEVLOG #62).

    Updated once, at the moment an entity's `deletion_time` is finalized
    (Dispose, or Separate destroying a batch parent -- see `account_and_retain`),
    independent of whether the entity itself stays resident in `all_entities`
    for the flat per-entity log. `created`/`completed` and the `times`/`costs`
    samples are therefore exact no matter how small `entity_log_limit` is --
    only the flat log and its percentile/mode cosmetics are capped.
    """

    created: int = 0
    completed: int = 0
    times: list = field(default_factory=list)
    costs: list = field(default_factory=list)
    holding_sum: float = 0.0
    assigned_sum: float = 0.0   # sum of (entity.cost - entity._auto_cost)


@dataclass
class DueDateStats:
    """Streaming due-date accumulator for one entity type (PHASE4 A3).

    Fed by `account_due_date` for every entity that carries a `due_date` and is
    disposed at or after the warm-up end -- the same window test the entity
    KPIs already use, so a job finished during warm-up never counts, however it
    got its due date. Sums only: `outputs.summarize_due_dates` divides.
    """

    completed: int = 0
    on_time: int = 0
    lateness_sum: float = 0.0
    tardiness_sum: float = 0.0
    tardiness_max: float = 0.0


def account_due_date(ctx, entity) -> None:
    """Record *entity*'s lateness against its `due_date`, and credit its order
    (PHASE4 A3). Called once per entity from `account_and_retain`, so both the
    order-level and the entity-level due-date KPIs see exactly the entities the
    entity KPIs see.

    An entity gets a due date from the order that released it (CREATE in
    "Orders" mode) or from an ASSIGN (`entity.due_date = now + 480`); neither
    path is privileged here. An entity with no due date costs one getattr.
    """
    order = ctx.orders.get(getattr(entity, "order_id", None)) if ctx.orders else None
    if order is not None:
        order.completed += 1
        if order.last_completion is None or entity.deletion_time > order.last_completion:
            order.last_completion = entity.deletion_time

    due = getattr(entity, "due_date", None)
    if due is None or entity.deletion_time is None or entity.deletion_time < ctx.warmup:
        return
    try:
        lateness = float(entity.deletion_time) - float(due)
    except (TypeError, ValueError):
        return
    acc = ctx.due_date_stats.setdefault(entity.e_type, DueDateStats())
    acc.completed += 1
    acc.lateness_sum += lateness
    if lateness > 0:
        acc.tardiness_sum += lateness
        acc.tardiness_max = max(acc.tardiness_max, lateness)
    else:
        acc.on_time += 1


def account_and_retain(ctx, entity) -> None:
    """Finalize *entity*'s contribution to `ctx.entity_stats` and decide
    whether it stays resident in `ctx.all_entities` for the flat per-entity
    log (ROADMAP 2.5, DEVLOG #62).

    Call this exactly once per entity, right after its `deletion_time` is set
    (Dispose is the normal case; Separate also finalizes a batch parent's
    `deletion_time` directly without routing it through Dispose). An entity
    that is neither "created" nor "completed" under the warm-up filter
    (DEVLOG #18, #22 -- the same test `outputs.counted_entities` used to run
    post-hoc) would never have appeared in a report either way, so it is
    simply dropped: no stats, no retention, no truncation.

    Only the FIRST `entity_log_limit` reportable entities are retained; every
    one after that is accounted for here and then dropped, and
    `entity_log_truncated` is set once that starts happening.
    """
    # Before the warm-up filter below: an order's completion is a fact about
    # the order, and `account_due_date` applies the window test itself.
    account_due_date(ctx, entity)
    if ctx.plan_gates is not None:          # PHASE6 W4 P4, D122
        ctx.plan_gates.drop(entity)

    warmup = ctx.warmup
    created = created_in_window(entity, warmup)
    completed = entity.deletion_time is not None and entity.deletion_time >= warmup
    if not created and not completed:
        _drop(ctx, entity)
        return

    acc = ctx.entity_stats.setdefault(entity.e_type, EntityTypeStats())
    if created:
        acc.created += 1
    if completed:
        acc.completed += 1
        acc.times.append(entity.throughput_time)
        acc.costs.append(entity.cost)
        acc.holding_sum += entity._holding_cost
        acc.assigned_sum += entity.cost - entity._auto_cost

    if not ctx.record_logs:
        # PHASE4 T7: the per-type totals above are the whole contribution of a
        # disposed entity, so with the flat log off it is dropped at once --
        # WIP is still exact, since a LIVE entity is never dropped.
        _drop(ctx, entity)
    elif ctx.entity_log_count < ctx.entity_log_limit:
        ctx.entity_log_count += 1
    else:
        ctx.entity_log_truncated = True
        _drop(ctx, entity)


def _drop(ctx, entity) -> None:
    if entity in ctx.all_entities:
        ctx.all_entities.remove(entity)


@lru_cache(maxsize=512)
def _t_quantile(confidence: float, dof: int) -> float:
    """`t.ppf` for one (confidence, degrees of freedom) pair.

    Cached because the callers ask for the SAME pair thousands of times in a
    row -- `outputs.timeseries_mean` alone calls `ci95` once per (series,
    sample time), every one of them with the replication count as n -- and
    scipy's generic `ppf` costs about 70 us each time. Same float, one call.
    """
    return float(_sp_stats.t.ppf(0.5 + confidence / 2.0, dof))


def ci95(values, confidence: float = 0.95, *, nonneg: bool = False) -> dict[str, float]:
    """Student-t confidence interval over replication values.

    Returns mean/sd/sem/lb/ub. sd, sem, lb and ub are NaN for n < 2 -- never a
    placeholder string (DEVLOG #34). lb <= ub always (DEVLOG #19).

    *nonneg* (#183): the Student-t interval is symmetric around the mean, so
    a metric with a small mean and enough spread gets a negative lower bound
    even when it physically cannot be one (a queue length, a count, a time, a
    utilisation, a cost). Left False everywhere by default -- a caller sets it
    only when it KNOWS the metric can't go negative; ci95 itself never
    assumes that, since plenty of callers pass differences (scenario deltas,
    OAT effects) that legitimately can.
    """
    arr = np.asarray([v for v in np.asarray(values, dtype=float).ravel() if np.isfinite(v)], dtype=float)
    n = arr.size
    if n == 0:
        return {"mean": np.nan, "sd": np.nan, "sem": np.nan, "lb": np.nan, "ub": np.nan}
    mean = float(arr.mean())
    if n < 2:
        return {"mean": mean, "sd": np.nan, "sem": np.nan, "lb": np.nan, "ub": np.nan}
    sd = float(arr.std(ddof=1))
    sem = sd / np.sqrt(n)
    half = _t_quantile(confidence, n - 1) * sem
    lb = mean - half
    if nonneg and lb < 0.0:
        lb = 0.0
    return {"mean": mean, "sd": sd, "sem": sem, "lb": lb, "ub": mean + half}


# ---------- difference of two means (ROADMAP 1.6 scenario comparison) --------
_NO_DIFF = {"mean": np.nan, "sem": np.nan, "lb": np.nan, "ub": np.nan, "df": np.nan}


def _finite(values) -> np.ndarray:
    arr = np.asarray(values, dtype=float).ravel()
    return arr[np.isfinite(arr)]


def paired_diff_ci(a, b, confidence: float = 0.95) -> dict[str, float]:
    """Student-t interval on the mean of the PAIRED differences a[i] - b[i].

    Use with common random numbers: replication i of both scenarios ran on the
    same seeds, so the difference is the observation and the shared sampling
    noise cancels -- normally a much narrower interval than Welch's on the same
    numbers. Callers must pass the two samples in the same replication order.

    Pairs are truncated to the shorter sample and any pair with a non-finite
    value is dropped. Returns mean/sem/lb/ub/df, NaN everywhere but the mean
    for fewer than 2 usable pairs (same contract as `ci95`). A zero-variance
    sample gives lb == ub == mean, which a "significant" test reads as
    "not significant" only when that mean is 0.
    """
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    n = min(a.size, b.size)
    d = _finite(a[:n] - b[:n])
    if d.size == 0:
        return dict(_NO_DIFF)
    mean = float(d.mean())
    if d.size < 2:
        return {**_NO_DIFF, "mean": mean}
    sem = float(d.std(ddof=1)) / np.sqrt(d.size)
    half = _t_quantile(confidence, d.size - 1) * sem
    return {"mean": mean, "sem": sem, "lb": mean - half, "ub": mean + half,
            "df": float(d.size - 1)}


def welch_diff_ci(a, b, confidence: float = 0.95) -> dict[str, float]:
    """Welch's interval on mean(a) - mean(b) for two INDEPENDENT samples.

    Unequal variances and unequal sizes are fine; the degrees of freedom come
    from the Welch-Satterthwaite approximation. Use when the scenarios were run
    on disjoint seeds (crn=False). Same return keys as `paired_diff_ci`; with
    both samples constant the standard error is 0, so the interval collapses to
    the point difference (df falls back to na + nb - 2, which the formula
    cannot produce from 0/0).
    """
    a, b = _finite(a), _finite(b)
    na, nb = a.size, b.size
    if na == 0 or nb == 0:
        return dict(_NO_DIFF)
    mean = float(a.mean() - b.mean())
    if na < 2 or nb < 2:
        return {**_NO_DIFF, "mean": mean}
    sa = float(a.var(ddof=1)) / na
    sb = float(b.var(ddof=1)) / nb
    sem = float(np.sqrt(sa + sb))
    if sem == 0:
        return {"mean": mean, "sem": 0.0, "lb": mean, "ub": mean, "df": float(na + nb - 2)}
    df = (sa + sb) ** 2 / (sa ** 2 / (na - 1) + sb ** 2 / (nb - 1))
    half = float(_sp_stats.t.ppf(0.5 + confidence / 2.0, df)) * sem
    return {"mean": mean, "sem": sem, "lb": mean - half, "ub": mean + half, "df": float(df)}


def wilson_interval(successes: int, n: int, confidence: float = 0.95) -> dict[str, float]:
    """Wilson score interval for a PROPORTION (ROADMAP 3.6 event probabilities).

    Used instead of the textbook normal interval because a forecast event
    probability is routinely 0 or 1 over a few hundred replications, where the
    normal interval collapses to a useless zero-width interval at exactly the
    point the answer is least certain. With z = the two-sided normal quantile,

        centre = (p + z^2/2n) / (1 + z^2/n),
        half   = z * sqrt(p(1-p)/n + z^2/4n^2) / (1 + z^2/n)

    Returns {p, lb, ub, n}; n == 0 gives p = NaN and the full [0, 1] interval.
    """
    n = int(n)
    if n <= 0:
        return {"p": float("nan"), "lb": 0.0, "ub": 1.0, "n": 0}
    p = successes / n
    z = float(_sp_stats.norm.ppf(0.5 + confidence / 2.0))
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * float(np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return {"p": float(p), "lb": max(centre - half, 0.0), "ub": min(centre + half, 1.0), "n": n}
