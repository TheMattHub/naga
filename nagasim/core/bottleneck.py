"""Roser active-period bottleneck analyser (PHASE6 W3 M2, ARCHITECTURE D113).

A resource is ACTIVE while working, in changeover or under repair -- the
states that already keep it seized (`resource_manager`'s `busy` collector
covers working AND changeover, since a changeover happens between acquire
and release) or down (`down`); it is INACTIVE while idle, blocked or off
shift. `Tracker` turns the three existing per-resource signals (busy level,
blocked level, down flag) into closed [start, end) active intervals, ONLY
when a model opts in (`outputs.bottleneck: true`) -- the memory this costs
(one tuple per active/inactive transition) is not worth paying by default.

`active_period_shares` is the pure Roser algorithm: momentary-bottleneck
identification (the resource in the longest CURRENT active period wins each
instant) plus the sole/shifting split at each hand-off, over a plain
{resource: [(start, end), ...]} input -- no simulation involved, so it is
unit-tested on its own (tests/test_sankey_bottleneck.py).

ponytail: resource-level granularity (a capacity > 1 station counts as
ACTIVE once ANY unit is busy or down), not per-unit; a per-unit state
machine is the upgrade path if a multi-server station's bottleneck picture
ever needs it.
"""
from __future__ import annotations

__all__ = ["Tracker", "active_period_shares"]


class Tracker:
    """Per-resource ACTIVE-interval collector, built only when
    `outputs.bottleneck` is set (`core.graph_model`)."""

    def __init__(self):
        self._busy: dict[str, float] = {}
        # {resource: {blocking node: units it holds blocked}}: several blocking
        # blocks may share one resource, each reporting its own level (#190).
        self._blocked: dict[str, dict[str, float]] = {}
        self._down: dict[str, bool] = {}
        self._since: dict[str, float | None] = {}
        self.periods: dict[str, list[tuple[float, float]]] = {}

    def _active(self, name: str) -> bool:
        blocked = sum(self._blocked.get(name, {}).values())
        return (self._busy.get(name, 0) - blocked) > 0 or self._down.get(name, False)

    def _touch(self, name: str, t: float) -> None:
        active = self._active(name)
        since = self._since.get(name)
        if active and since is None:
            # A release and a re-seize in the same instant (the next job
            # starting) is no idle gap: continue the period that just closed,
            # or every job would restart the "current active period" (#195).
            periods = self.periods.get(name)
            self._since[name] = periods.pop()[0] if periods and periods[-1][1] == t else t
        elif not active and since is not None:
            self._since[name] = None
            if t > since:
                self.periods.setdefault(name, []).append((since, t))

    def busy(self, name: str, level: float, t: float) -> None:
        self._busy[name] = level
        self._touch(name, t)

    def blocked(self, name: str, level: float, t: float, node: str = "") -> None:
        """*level*: the units blocking node *node* holds blocked on *name*."""
        self._blocked.setdefault(name, {})[node] = level
        self._touch(name, t)

    def down(self, name: str, is_down: bool, t: float) -> None:
        self._down[name] = is_down
        self._touch(name, t)

    def reset(self, t: float) -> None:
        """Warm-up: drop closed periods (a statistics reset, DEVLOG #22); an
        in-progress active period restarts at *t*, exactly like every other
        collector here keeps its current level across the reset."""
        self.periods = {}
        for name, since in self._since.items():
            if since is not None:
                self._since[name] = t

    def closed_periods(self, t_end: float) -> dict[str, list[tuple[float, float]]]:
        """Every resource's active intervals up to *t_end*, including one
        still open at report time -- a pure read, never mutates the tracker."""
        out = {name: list(intervals) for name, intervals in self.periods.items()}
        for name, since in self._since.items():
            if since is not None and t_end > since:
                out.setdefault(name, []).append((since, t_end))
        return out


def active_period_shares(intervals_by_resource: dict, t0: float, t1: float) -> dict:
    """Roser's momentary-bottleneck method over already-clipped ACTIVE
    intervals: {resource: [(start, end), ...]}.

    At each instant the resource in the longest CURRENT active period (the
    interval it happens to be inside) is the momentary bottleneck; when the
    identity hands off from one resource to another, the wall-clock overlap
    between the two resources' raw active intervals, clipped to the longer
    side's holding run, is SHIFTING for BOTH of them (shared responsibility,
    so it is not subtracted from the other side); everything else a resource is the momentary bottleneck for is
    SOLE.

    Returns {resource: {"Sole %", "Shifting %", "Total %"}} -- percentages of
    the window (t1 - t0); a resource never a bottleneck gets all zeros, and
    every resource in *intervals_by_resource* gets a row even if it was never
    active at all.
    """
    span = max(t1 - t0, 0.0)
    resources = list(intervals_by_resource)
    intervals = []
    for name, periods in intervals_by_resource.items():
        for s, e in periods:
            s, e = max(s, t0), min(e, t1)
            if e > s:
                intervals.append((name, s, e))

    sole = {name: 0.0 for name in resources}
    shifting = {name: 0.0 for name in resources}

    if intervals and span > 0:
        points = sorted({p for _, s, e in intervals for p in (s, e)})
        # holders: consecutive (resource, interval_start, interval_end,
        # segment_start, segment_end) runs where that resource's OWN active
        # interval is the argmax by duration over the segment.
        holders: list[tuple] = []
        for a, b in zip(points, points[1:]):
            if b <= a:
                continue
            mid = (a + b) / 2
            covering = [iv for iv in intervals if iv[1] <= mid < iv[2]]
            if not covering:
                continue
            # Longest current active period wins; ties broken by name for a
            # deterministic result (an exact tie is a modelling coincidence,
            # not a real ordering).
            owner = max(covering, key=lambda iv: (iv[2] - iv[1], iv[0]))
            if holders and holders[-1][:3] == owner:
                r, s, e, seg_s, _ = holders[-1]
                holders[-1] = (r, s, e, seg_s, b)
            else:
                holders.append((*owner, a, b))

        for r, _istart, _iend, seg_s, seg_e in holders:
            sole[r] += seg_e - seg_s
        for h1, h2 in zip(holders, holders[1:]):
            (r1, s1, e1, *_), (r2, s2, e2, *_) = h1, h2
            if r1 == r2:
                continue
            # The overlap is shifting only where one of the two actually HELD
            # the title: the longer side (the sweep's own argmax), and only
            # inside that side's holding run -- a third resource may have held
            # the rest of the raw overlap (#191).
            winner = h1 if (e1 - s1, r1) >= (e2 - s2, r2) else h2
            overlap = min(e1, e2, winner[4]) - max(s1, s2, winner[3])
            if overlap > 0:
                sole[winner[0]] -= overlap
                shifting[r1] += overlap
                shifting[r2] += overlap

    return {name: {"Sole %": 100.0 * sole[name] / span if span else 0.0,
                    "Shifting %": 100.0 * shifting[name] / span if span else 0.0,
                    "Total %": 100.0 * (sole[name] + shifting[name]) / span if span else 0.0}
            for name in resources}
