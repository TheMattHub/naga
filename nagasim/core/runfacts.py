"""Run facts (D678): what happened in one replication that a modeller would want to
know and nothing else reports, read from its trace. Each is a fact with numbers and
possible causes, never a verdict, and never a failed run: the discrete-event
counterpart of Open Industry Project's "flow facts".

A pure consumer of `TraceEvent`s, like `core/playback.py` and `core/bottleneck.py`:
it reads a trace and never touches a run, so a run is the same with or without it.
`Timeline` answers what the model looked like at one instant; facts need intervals
(how long, since when), so this is one walk of its own over the events.

Kinds, in the order they are listed:

  never_entered       a block no entity entered
  never_seized        a declared resource no entity seized
  idle_while_waiting  entities waited at a block for a resource while it had a unit
                      doing no work: free and up, or held by an entity that was itself
                      waiting at a block to seize something (a multi-resource seize)
  left_waiting        entities still at a block when the run or its trace ended
  truncated           the trace hit its cap, so the facts cover only its first part

ponytail: blocks and declared resources only. Fleets, levels and movers have no facts
yet; an entity waiting for a resource set counts only for the member it got (or, still
waiting at the end, for none); an entity in transit over a delayed link at the end is at
no block. Add them when a modeller asks.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from nagasim.core.block_registry import queueing_types
from nagasim.core.submodels import flatten

__all__ = ["Fact", "KINDS", "MAX_PER_KIND", "MIN_IDLE_SHARE", "facts"]

KINDS = ("never_entered", "never_seized", "idle_while_waiting", "left_waiting", "truncated")

# A reader acts on the first few facts of a kind and an agent pays for every line, so
# each kind lists its largest 20 and counts the rest in one "and N more" fact.
MAX_PER_KIND = 20

# A release and a grant in one instant, or a float apart, leave a unit free for no time
# worth naming: idle time is a fact only once it adds up to this share of the run.
MIN_IDLE_SHARE = 0.01

_INF = float("inf")


@dataclass(frozen=True, slots=True)
class Fact:
    """One fact. *block* and *resource* are flattened names ("" when the fact has
    none); *t* is when it starts (the first instant measured, the oldest arrival, the
    cut), None for a fact about the whole run; *count* the entities (or the facts an
    "and N more" stands for); *duration* the time measured, in the model's unit."""
    kind: str
    text: str
    block: str = ""
    resource: str = ""
    t: float | None = None
    count: int = 0
    duration: float = 0.0


def facts(events, layout: dict, *, end_time: float, truncated: bool = False) -> list[Fact]:
    """The facts of one replication: *events* its trace (`TraceRecorder.events`),
    *layout* the model it ran (flattened here when it has submodels), *end_time* the
    time the run ended (`ctx.sim_end_time`), *truncated* `TraceRecorder.truncated`.

    Sorted by kind (`KINDS` order), then by size (never_entered and never_seized keep the
    layout's order); at most `MAX_PER_KIND` per kind plus
    one "and N more" fact. Deterministic: the same trace gives the same list."""
    layout = flatten(layout or {})
    events = sorted(events, key=lambda e: e.t)   # stable: emit order within an instant
    t0 = events[0].t if events else 0.0
    # A cut trace says nothing past its last event, so every fact ends there.
    end = events[-1].t if truncated and events else float(end_time)
    unit = (layout.get("simulation") or {}).get("time_unit") or "minutes"
    blocks = [b for b in layout.get("blocks") or [] if b.get("name")]
    resources = [str(r["name"]) for r in layout.get("resources") or [] if r.get("name")]
    walk = _Walk(blocks, set(resources), end)
    for event in events:
        walk.apply(event)
    walk.finish()

    found = {kind: [] for kind in KINDS}
    found["never_entered"] = _never_entered(blocks, walk.entered, end)
    found["never_seized"] = _never_seized(resources, layout, walk, end)
    span = end - t0
    for name in resources:
        found["idle_while_waiting"] += _idle_while_waiting(name, walk, span, unit)
    found["idle_while_waiting"].sort(key=lambda f: (-f.duration, f.resource, f.block))
    found["left_waiting"] = _left_waiting(walk, blocks, end)
    if truncated:
        found["truncated"] = [Fact(
            "truncated", f"The trace hit its event cap at t={_n(end)}, so these facts cover "
            f"the run only up to t={_n(end)}, not to its end at t={_n(end_time)}. A shorter "
            "run length gives facts over the whole run.", t=end)]
    out = []
    for kind in KINDS:
        listed = found[kind]
        out += listed[:MAX_PER_KIND]
        if len(listed) > MAX_PER_KIND:
            more = len(listed) - MAX_PER_KIND
            out.append(Fact(kind, f"And {more} more {kind.replace('_', ' ')} facts.", count=more))
    return out


class _Walk:
    """One pass over the trace: where each entity is and since when, what it waits
    for, what it holds, and each resource's capacity and failures over time."""

    def __init__(self, blocks: list, resources: set, end: float):
        self.end = end
        queueing = set(queueing_types())
        # What an entity still waiting at a queueing block at the end waits for: that
        # block's own resource ("" when it names a set or nothing declared).
        self.queues: dict = {}
        for block in blocks:
            params = block.get("params") or {}
            if str(block.get("type", "")).upper() in queueing:
                wanted = str(params.get("resource_name") or "")
                self.queues[block["name"]] = wanted if wanted in resources else ""
        self.entered: set = set()
        self.seized: set = set()
        # entity -> [block, arrived, since (the last arrival, seize or release there),
        # seized there since it arrived]
        self.where: dict = {}
        self.stays: dict = {}           # block -> [(arrived, left or None)]
        self.waits: list = []           # (entity, block, resource or "", start, end)
        self.holds: dict = {}           # resource -> [(entity, start, end, units)]
        self._open: dict = {}           # (entity, resource) -> [(start, units)]
        self.capacity: dict = {}        # resource -> [(t, units in force)]
        self.down: dict = {}            # resource -> [(t, down)]

    def _leave(self, entity, t) -> None:
        loc = self.where.pop(entity, None)
        if loc is not None and loc[1] <= self.end:
            self.stays.setdefault(loc[0], []).append((loc[1], max(t, loc[1])))

    def apply(self, e) -> None:
        kind, entity, t = e.kind, e.entity_id, e.t
        if kind == "entity_created":
            self._leave(entity, t)
            self.where[entity] = [e.block, t, t, False]
            self.entered.add(e.block)
        elif kind == "entity_moved":
            self._leave(entity, t)
            if e.target:
                arrive = t + e.duration         # a delayed link: it arrives later
                self.where[entity] = [e.target, arrive, arrive, False]
                if arrive <= self.end:
                    self.entered.add(e.target)
        elif kind in ("entity_disposed", "entity_absorbed"):
            self._leave(entity, t)
        elif kind == "entity_seized":
            resource, loc = e.target or "", self.where.get(entity)
            if loc is not None and loc[0] == e.block:
                if t > loc[2]:
                    self.waits.append((entity, e.block, resource, loc[2], t))
                loc[2], loc[3] = t, True
            self.seized.add(resource)
            # The units this seize holds (#748); a fleet's seize carries none: one vehicle.
            self._open.setdefault((entity, resource), []).append((t, int(e.value) or 1))
        elif kind == "entity_released":
            resource, loc = e.target or "", self.where.get(entity)
            held = self._open.get((entity, resource))
            if held:
                start, units = held.pop(0)
                self.holds.setdefault(resource, []).append((entity, start, t, units))
            if loc is not None and loc[0] == e.block:
                loc[2], loc[3] = t, False       # a preempted job waits again from here
        elif kind == "resource_state":
            if e.state == "capacity":
                self.capacity.setdefault(e.resource, []).append((t, e.value))
            elif e.state in ("down", "up"):
                self.down.setdefault(e.resource, []).append((t, e.state == "down"))

    def finish(self) -> None:
        """Close what is still open at the end: holds, stays and waits."""
        end = self.end
        for (entity, resource), held in self._open.items():
            for start, units in held:
                self.holds.setdefault(resource, []).append((entity, start, end, units))
        self._open = {}
        self.present: dict = {}         # block -> [arrival of each entity still there]
        for entity, (block, arrived, since, seized_here) in self.where.items():
            if arrived > end:
                continue                # still in transit
            self.stays.setdefault(block, []).append((arrived, None))
            self.present.setdefault(block, []).append(arrived)
            if block in self.queues and not seized_here and since < end:
                self.waits.append((entity, block, self.queues[block], since, end))
        self.waits_of: dict = {}        # entity -> [(start, end, block)]
        self.waits_for: dict = {}       # resource -> [(entity, block, start, end)]
        for entity, block, resource, start, stop in self.waits:
            self.waits_of.setdefault(entity, []).append((start, stop, block))
            self.waits_for.setdefault(resource, []).append((entity, block, start, stop))


def _never_entered(blocks: list, entered: set, end: float) -> list:
    out = []
    for block in blocks:
        name = block["name"]
        if name in entered:
            continue
        if str(block.get("type", "")).upper() == "CREATE":
            text = (f"{name} never created an entity in the run (to t={_n(end)}). Its start "
                    "time, its arrival schedule or orders, or a max_arrivals reached by "
                    "nothing can cause this.")
        else:
            text = (f"{name} was never entered: no entity reached it in the run (to "
                    f"t={_n(end)}). A branch never taken (a probability of 0, a condition "
                    "never true), a block upstream that keeps every entity, or a run too "
                    "short to get there can cause this.")
        out.append(Fact("never_entered", text, block=name))
    return out


def _never_seized(resources: list, layout: dict, walk: _Walk, end: float) -> list:
    out = []
    for name in resources:
        # A `$` template stays declared beside its per-instance clones and is never
        # seized by design (core/submodels.py, item 6).
        if name in walk.seized or any(other.endswith("/" + name) for other in resources):
            continue
        # The blocks that name it, directly or through a set it is a member of.
        names = {name} | {s.get("name") for s in layout.get("resource_sets") or []
                          if name in (s.get("members") or [])}
        seizers = [b["name"] for b in layout.get("blocks") or []
                   if (b.get("params") or {}).get("resource_name") in names]
        listed = ", ".join(seizers[:3]) + (", ..." if len(seizers) > 3 else "")
        capacity = [value for _t, value in walk.capacity.get(name, ())]
        if capacity and not any(capacity):
            cause = "Its capacity in force was 0 for the whole run."
        elif not seizers:
            cause = ("No block names it, nor a set it belongs to (an expression may still "
                     "read it).")
        elif not walk.entered.intersection(seizers):
            cause = f"No entity reached the blocks that name it ({listed})."
        else:
            cause = (f"Entities reached {listed} but none seized it: a set that always "
                     "picked another member, or a run that ended first, can cause this.")
        out.append(Fact("never_seized", f"{name} was never seized in the run (to "
                                        f"t={_n(end)}). {cause}", resource=name))
    return out


def _idle_while_waiting(name: str, walk: _Walk, span: float, unit: str) -> list:
    """One fact per block where entities waited for *name* while it had a unit doing
    no work. A sweep over the instants anything about it changed: between two of them
    its capacity in force, failure, busy units, units held by waiting entities and
    the entities waiting at each block are all constant."""
    waits = walk.waits_for.get(name, [])
    if not waits or span <= 0:
        return []
    changes = []        # (t, what, value)
    for t, value in walk.capacity.get(name, ()):
        changes.append((t, "capacity", value))
    for t, is_down in walk.down.get(name, ()):
        changes.append((t, "down", is_down))
    held_at: dict = {}  # block where holders waited -> time they held a unit there
    for entity, start, stop, units in walk.holds.get(name, ()):
        changes += [(start, "busy", units), (stop, "busy", -units)]
        for wait_start, wait_stop, block in walk.waits_of.get(entity, ()):
            a, b = max(start, wait_start), min(stop, wait_stop)
            if b > a:
                changes += [(a, "held", units), (b, "held", -units)]
                held_at[block] = held_at.get(block, 0.0) + (b - a)
    for _entity, block, start, stop in waits:
        changes += [(start, "wait", (block, 1)), (stop, "wait", (block, -1))]
    changes.sort(key=lambda c: c[0])

    capacity, down, busy, held = None, False, 0, 0
    waiting: dict = {}
    segments: dict = {}     # block -> [(start, end)] while idle with entities waiting
    free_time: dict = {}    # block -> the part of that time a unit was free
    for i, (t, what, value) in enumerate(changes):
        if what == "capacity":
            capacity = value
        elif what == "down":
            down = value
        elif what == "busy":
            busy += value
        elif what == "held":
            held += value
        else:
            waiting[value[0]] = waiting.get(value[0], 0) + value[1]
        later = changes[i + 1][0] if i + 1 < len(changes) else t
        if later <= t or down or not capacity or capacity <= 0:
            continue
        free = capacity - busy > 0
        if not (free or held > 0):
            continue
        for block, n in waiting.items():
            if n > 0:
                spans = segments.setdefault(block, [])
                if spans and spans[-1][1] == t:
                    spans[-1] = (spans[-1][0], later)
                else:
                    spans.append((t, later))
                if free:
                    free_time[block] = free_time.get(block, 0.0) + (later - t)

    out = []
    where = max(held_at, key=lambda b: (held_at[b], b)) if held_at else ""
    for block, spans in segments.items():
        total = sum(b - a for a, b in spans)
        if total < MIN_IDLE_SHARE * span:
            continue
        starts = [a for a, _b in spans]
        entities = set()
        for entity, at, start, stop in waits:
            if at != block:
                continue
            i = bisect_right(starts, stop) - 1      # the last span starting before it ends
            while i >= 0 and spans[i][1] > start:
                if min(stop, spans[i][1]) > max(start, spans[i][0]):
                    entities.add(entity)
                    break
                i -= 1
        if not entities:
            continue
        free = free_time.get(block, 0.0)
        parts, causes = [], []
        if free > 0:
            parts.append(f"{_n(free)} of them with a unit free and up")
            causes.append("A seize of more units than are free, a selection rule or a Plan "
                          "queue, a schedule, or a wrong resource name can leave a unit free.")
        if total - free > 0:
            parts.append(f"{_n(total - free)} of them with a unit held by an entity that was "
                         f"itself waiting at {where}")
            causes.append(f"A multi-resource seize that takes {name} before a scarcer "
                          "resource, or blocking after service, can leave a unit held.")
        text = (f"{name} had a unit doing no work for {_n(total)} {unit} in total while "
                f"{_entities(len(entities))} waited for it at {block} (from t={_n(spans[0][0])}): "
                f"{'; '.join(parts)}. {' '.join(causes)}")
        out.append(Fact("idle_while_waiting", text, block=block, resource=name, t=spans[0][0],
                        count=len(entities), duration=total))
    return out


def _left_waiting(walk: _Walk, blocks: list, end: float) -> list:
    kinds = {b["name"]: str(b.get("type", "")).upper() for b in blocks}
    out = []
    for block, arrivals in walk.present.items():
        # Never empty since: the start of the last run of overlapping stays, a hand-over
        # in one instant counting as no gap (the count is 0 for no time at all).
        since, reach = None, -_INF
        for arrived, left in sorted((a, _INF if left is None else left)
                                    for a, left in walk.stays[block]):
            if arrived > reach:
                since = arrived
            reach = max(reach, left)
        if kinds.get(block) == "HOLD":
            cause = "A signal or condition that never comes keeps entities at a HOLD for ever."
        elif block in walk.queues:
            cause = ("Too little capacity, a failure, a schedule or a multi-resource seize can "
                     "keep a queue occupied; a run that ends mid-work also leaves some behind.")
        else:
            cause = ("A run that ends mid-work leaves entities behind; a block that never lets "
                     "them go keeps them.")
        n = len(arrivals)
        text = (f"{_entities(n)} {'was' if n == 1 else 'were'} still at {block} "
                f"at the end (t={_n(end)}); the oldest arrived at t={_n(min(arrivals))}, and "
                f"{block} was never empty since t={_n(since)}. {cause}")
        out.append(Fact("left_waiting", text, block=block, t=min(arrivals), count=n,
                        duration=end - since))
    out.sort(key=lambda f: (-f.count, -f.duration, f.block))
    return out


def _n(x: float) -> str:
    """A time or a duration as text: two decimals at most, no trailing zeros."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def _entities(n: int) -> str:
    return f"{n} entit{'y' if n == 1 else 'ies'}"
