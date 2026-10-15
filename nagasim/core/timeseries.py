"""Time-series sampler (ROADMAP 1.7): periodic snapshots of WIP, per-resource
queue/in-service/capacity, per-store size and time-persistent user statistics.

Engine-side module: no pandas, no UI toolkit (ARCHITECTURE 10.3). Samples are
plain (Time, Series, Value, Warmup) tuples appended to `ctx.timeseries`;
outputs.py turns them into the "timeseries" DataFrame.

The sampler consumes no random numbers (so turning sampling on or off cannot
change any other block's draws) and never keeps a draining model alive: it
schedules nothing. `Sampler` only holds the grid; the run loop
(`sim_runner.step_until`) takes each tick between two events.
"""
from __future__ import annotations

import logging
import math
from contextlib import contextmanager

from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression

__all__ = ["sample_interval", "Sampler", "build_dashboard_widgets", "count_departures"]

log = logging.getLogger(__name__)


@contextmanager
def peeking(ctx):
    """While reading, LEVEL() and FRACTION() peek, never integrate (#229, #477): a read
    that synced the levels would split the flows' accruals where it read."""
    system = ctx.level_system
    if system is not None:
        system.peeking = True
    try:
        yield
    finally:
        if system is not None:
            system.peeking = False


def sample_interval(run_len: float, points: int, interval_setting: float | None) -> float:
    """The fixed sampling interval, or 0.0 to disable sampling entirely.

    A time-limited run divides the horizon into `points` samples; a draining
    model (no run length) has no horizon to divide, so it falls back to the
    model's own `timeseries_interval` and is off by default.
    """
    if run_len and run_len > 0:
        return (run_len / points) if points and points > 0 else 0.0
    return interval_setting or 0.0


def count_departures(ctx):
    """The bus subscriber behind `block/<name>/out` (PHASE7 7.14, D290): for each block
    `simulation.timeseries_counts` names, the items that left it, by moving on or by ending
    there. What a plant counter at that station counts, and what `plant.log_of` writes."""
    wanted, counts = frozenset(ctx.counted_blocks), ctx.block_out_counts

    def on(name, block=None, target=None, **_payload):
        if (name == "entity_disposed" or name == "entity_moved" and target is not None) \
                and (left := getattr(block, "name", None)) in wanted and target != left:
            counts[left] = counts.get(left, 0) + 1
    return on


def _take_sample(ctx, t: float, warmup: float) -> list[tuple]:
    """One (Time, Series, Value, Warmup) row per tracked series, at time *t*."""
    warm = t < warmup
    rows = [(t, "system/WIP", float(len(ctx.live_entities)), warm)]

    for name, res in ctx.resource_manager.resources.items():
        rows.append((t, f"resource/{name}/queue", float(res.waiting), warm))
        rows.append((t, f"resource/{name}/in_service", float(res.in_service), warm))
        rows.append((t, f"resource/{name}/capacity", float(res.scheduled_capacity), warm))

    for name, rset in ctx.resource_manager.sets.items():
        rows.append((t, f"resource/{name}/queue", float(rset.queue_len.level), warm))

    for name in ctx.counted_blocks:
        rows.append((t, f"block/{name}/out", float(ctx.block_out_counts.get(name, 0)), warm))

    for store in ctx.store_manager.all():
        rows.append((t, f"store/{store.name}/size", float(len(store.items)), warm))

    for name, stat in ctx.user_stats.items():
        if stat.kind == "time_persistent":
            rows.append((t, f"user/{name}", float(stat.collector.level), warm))

    # Level trajectories (PHASE4 D1): each level as of `t` (`peek`), so the curve
    # is the real one rather than the value as of the last engine event. Read
    # only: a `sync` here split the flows' accruals at the ticks, and a run
    # sampled on another grid (a started one, D233) summed them apart (#466).
    if ctx.level_system is not None:
        for name, level in ctx.level_system.levels.items():
            if not level.hidden:        # a delay stage (PHASE5 SD-2)
                rows.append((t, f"level/{name}", float(ctx.level_system.peek(level)), warm))
        # Flow trajectories (PHASE6 S8): the realised rate, same hidden-stage
        # filter as levels -- a reality check's FLOW() constraint needs a
        # sampled series to check exactly like LEVEL()/AUX() already have.
        for flow in ctx.level_system.flows:
            if not flow.hidden:
                rows.append((t, f"flow/{flow.name}", float(flow.actual), warm))

    # Agent counts per state (PHASE5 AB-1): piecewise constant, read as is.
    if ctx.agent_system is not None:
        for pop in ctx.agent_system.populations:
            for state, count, leaves in zip(pop.type.states, pop.counts, pop.type.leaves):
                if leaves is None:      # leaf states only (PHASE6 K1)
                    rows.append((t, f"agents/{pop.name}/{state}", float(count), warm))

    # Auxiliaries (PHASE5 SD-2), evaluated at `t`. The scope has no rng, so an
    # auxiliary that samples reads NaN here instead of shifting a stream.
    for name in ctx.auxiliaries:
        try:
            value = float(ctx.auxiliaries[name][0].evaluate(
                ExpressionScope(ctx=ctx, locals=ctx.auxiliaries[name][1])))
        except (ExpressionError, TypeError, ValueError):
            value = math.nan
        rows.append((t, f"aux/{name}", value, warm))

    return rows


def build_dashboard_widgets(layout: dict) -> list[tuple[str, list[tuple[str, object]]]]:
    """[(widget name, [(series label, compiled expression), ...]), ...] from
    the layout's "dashboard" section (PHASE6 G1, D132), same
    compile-once-per-replication pattern as `sd.build_auxiliaries`.

    A kpi/gauge widget normalises to one ("", expr) pair -- the empty label is
    what `outputs.summarize_dashboard` writes to its "Series" column -- so
    `_take_dashboard_sample` below needs no kind-specific branch."""
    widgets = []
    for entry in (layout.get("dashboard") or {}).get("widgets") or []:
        name = str(entry["name"])
        if entry.get("kind") in ("line", "bar"):
            series = [(str(s["label"]), compile_expression(str(s["expr"])))
                     for s in entry.get("series") or []]
        else:
            series = [("", compile_expression(str(entry.get("expr", "0"))))]
        widgets.append((name, series))
    return widgets


def _take_dashboard_sample(ctx, t: float, warmup: float) -> list[tuple]:
    """One (Time, Widget, Series, Value, Warmup) row per widget series, at
    time *t* -- the same instants `_take_sample` samples, and the same
    "no rng, so sampling changes nothing else" rule (no ExpressionScope.rng).
    Validation refuses what could never evaluate (DEVLOG #228); a read that
    fails now and then (a stale agent ref) is NaN, logged once per series."""
    warm = t < warmup
    rows = []
    for name, series in ctx.dashboard_widgets:
        for label, expr in series:
            try:
                value = float(expr.evaluate(ExpressionScope(ctx=ctx)))
            except (ExpressionError, TypeError, ValueError) as exc:
                value = math.nan
                if (name, label) not in ctx.dashboard_failed:
                    ctx.dashboard_failed.add((name, label))
                    log.warning("dashboard widget '%s'%s reads NaN at t=%g: %s", name,
                                f" series '{label}'" if label else "", t, exc)
            rows.append((t, name, label, value, warm))
    return rows


class Sampler:
    """The sampling grid of one run: a tick every *interval*, starting now.

    Not a SimPy process: one that shadowed every event time to avoid
    extending a draining run cost about half as many events again as the
    model's own. The state between two events is constant, so whoever steps
    the run (`sim_runner.step_until`, and `SimSession` when it moves the
    clock) calls `take()` once every event at or before `next_tick` is done
    and a later one, inside the run length, is still to come. That rule is
    what keeps a drained run from being extended.
    """

    __slots__ = ("env", "start", "interval", "warmup", "k", "next_tick")

    def __init__(self, env, interval: float, warmup: float):
        self.env, self.interval, self.warmup = env, interval, warmup
        # The k-th tick is start + k*interval from the integer k (DEVLOG #145):
        # accumulating `+= interval` drifts, and each sample syncs the levels.
        self.start, self.k = env.now, 0
        self.next_tick = self.start

    def take(self) -> float:
        """Sample at `next_tick`; returns the tick after it. The clock moves
        to the tick first, as an event stamped there would move it, so TNOW,
        LEVEL() and whoever reads the clock after the run see the tick."""
        env, t = self.env, self.next_tick
        ctx = env.ctx
        env._now = t
        # An auxiliary or a widget that reads LEVEL()/FRACTION() peeks too (#477): a sync
        # here would split the accruals at ticks a started run does not share (#466).
        with peeking(ctx):
            ctx.timeseries.extend(_take_sample(ctx, t, self.warmup))
            if ctx.dashboard_widgets:
                ctx.dashboard.extend(_take_dashboard_sample(ctx, t, self.warmup))
        self.k += 1
        self.next_tick = self.start + self.k * self.interval
        return self.next_tick
