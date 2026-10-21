"""Playback model (M4e): turns a recorded trace into the state the animator
draws. Qt-free, so the whole thing is unit-testable headless
(UI_DESIGN.md rule 5: animation is trace playback, never live stepping).

`Timeline` is the one trace reducer (ARCHITECTURE section 6, rule 13). The
module functions after it are what a view derives from its state and from the
layout (PHASE7 7.7, D196): which block shows which resource's state, the unit
counts, the resource rows, the KPI snapshot.
"""
from __future__ import annotations

import dataclasses
import re
from bisect import bisect_right, insort_right

import nagasim.settings.color_palette as cp

__all__ = ["Timeline", "visible_name", "asset_state_sources", "asset_states", "unit_sources",
           "unit_counts", "resource_rows", "kpi_snapshot", "node_blocks", "state_colors"]


def visible_name(name: str | None, view: str = "") -> str | None:
    """Which block of the level being VIEWED an event on *name* belongs to
    (ROADMAP 2.3 part B): the flattened name of a block inside a submodel
    instance is `cell_1/drill`, so at the top level (`view == ""`) it is
    attributed to the instance `cell_1`, and with the instance open
    (`view == "cell_1"`) to its own inner block `drill`. None when *name*
    belongs to a different branch of the hierarchy entirely.
    """
    if not name:
        return None
    if view:
        if not name.startswith(view + "/"):
            return None
        name = name[len(view) + 1:]
    return name.split("/", 1)[0]


def _progress(t: float, t0: float, span: float) -> float:
    """Linear 0..1 over (t0, t0 + span], clamped; 1.0 for a zero-length span."""
    if span <= 0:
        return 1.0
    return min(max((t - t0) / span, 0.0), 1.0)


def _attribute(events, view: str) -> list:
    """Rewrite every event's block/target to the visible ancestor, dropping the
    ones that belong to another instance."""
    out = []
    for event in events:
        if event.kind in ("resource_state", "mover_moved", "level_changed", "variable_changed",
                          "agent_state",
                          "agent_moved", "agent_removed", "agent_linked", "agent_unlinked",
                          "battery", "charge_start", "charge_end", "tend"):
            # None is a block-hierarchy path: a resource, a fleet, a level and
            # a population name are visible at every level (such a name may
            # itself contain "/" from $-scoping, e.g. "cell_1/Machine").
            out.append(event)
            continue
        block = visible_name(event.block, view)
        if event.kind in ("entity_seized", "entity_released"):
            # #316: the target is the resource or fleet held, not a block; it
            # keeps its own name, also when the seizing block is out of view.
            out.append(dataclasses.replace(event, block=block or ""))
            continue
        target = visible_name(event.target, view) if event.target else None
        if block is None and target is None:
            continue
        out.append(dataclasses.replace(event, block=block or "", target=target))
    return out


class Timeline:
    """Block occupancy over simulated time, rebuilt from a `TraceEvent` list.

    Event semantics, straight from the emit sites in `core/simple_processes.py`:

      entity_created(t, block=B)          the entity enters B at t
      entity_moved(t, block=B, target=C)  it leaves the block it is in and
                                          enters C at t (transfer is instant)
      entity_disposed(t)                  its life ends at t
      entity_absorbed(t)                  it stops being a part of its own (a
                                          BATCH or MATCH took it into a batch, a
                                          SEPARATE split the batch): it leaves
                                          its block and is not a disposal
      entity_seized(t, block=B)           it holds a resource from t while
                                          inside B (still inside B; queue and
                                          service look the same block occupancy
                                          otherwise). Any block can emit it --
                                          Seize and Process both do -- so
                                          `in_service` needs no block-type list.
      entity_released(t, block=B)         recorded for the trace; Release is a
                                          different block from the Seize that
                                          holds the entity, so it does not
                                          affect this block's occupancy

    Blocks that hold an entity for a while (Seize queue+service, Delay, Hold,
    Batch/Match stores) need no special case: the entity simply has no event
    between entering and leaving, so it stays counted there.

    `state_at` keeps an incremental cursor instead of rescanning, so sweeping a
    whole run forwards costs one pass in total; a backward seek replays from the
    start: about 20 ms at 50k events, 100-200 ms at the recorder's 250k-event cap.
    """

    def __init__(self, events, view: str = "", *, live: bool = False):
        # Submodel hierarchy (ROADMAP 2.3 part B): every event is attributed to
        # the block VISIBLE at this level before anything else happens, so the
        # rest of this class never hears about `cell_1/drill`.
        self.view = view
        # G4/L1: a live game's canvas Timeline (`live=True`) never keeps the
        # event log at all -- `append` applies each event immediately via
        # `_apply` and discards it, so memory and per-chunk time depend on how
        # many entities/resources are concurrently alive, never on how long
        # the game has run. See `_append_live`/`_fire_arrivals`.
        self.live = live
        events = list(events)
        self._attributed = bool(view) or any("/" in (e.block or "") for e in events)
        if self._attributed:
            events = _attribute(events, view)
        # Stable sort: within one timestamp the emit order is the causal order
        # (created before the move that leaves the creating block).
        self._events = sorted(events, key=lambda e: e.t)
        self._times = [e.t for e in self._events]
        # A move between two blocks of the same instance is invisible from
        # outside it: both ends attribute to the instance itself.
        self._moves = [(e.entity_id, e.e_type, e.block, e.target, e.t, e.branch)
                       for e in self._events
                       if e.kind == "entity_moved" and e.target and e.target != e.block]
        self._move_times = [m[4] for m in self._moves]
        self._created_times = [e.t for e in self._events if e.kind == "entity_created"]
        self._disposed_times = [e.t for e in self._events if e.kind == "entity_disposed"]
        # Link transfer delays (ROADMAP 1.5): a move with duration > 0 puts the
        # entity "in transit" at its departure event and only ENTERS `target`
        # at t + duration -- a synthetic arrival with no event of its own, so
        # it is precomputed here and replayed on its own sorted cursor.
        self._arrivals = sorted(
            ((e.t + e.duration, e.entity_id, e.e_type, e.target) for e in self._events
             if e.kind == "entity_moved" and e.target and e.duration > 0
             and e.target != e.block),
            key=lambda a: a[0])
        self._arrival_times = [a[0] for a in self._arrivals]
        # Level trajectories (PHASE4 T2, D75): {name: ([t], [value])}, the
        # recorded BREAKPOINTS in time order. Built once, read by bisect, so a
        # frame costs one lookup per level and a model with no levels pays one
        # empty dict. Two breakpoints may share a timestamp -- that is a
        # TRANSFER lump, and `level_at` then takes the later one, so the jump
        # stays a jump instead of being smeared over the segment before it.
        self._levels: dict[str, tuple[list, list]] = {}
        for event in self._events:
            if event.kind == "level_changed" and event.resource:
                times, values = self._levels.setdefault(event.resource, ([], []))
                times.append(event.t)
                values.append(event.value)
        self._rewind()

    def append(self, events) -> None:
        """Add trace events that arrive in time order (G4 game mode: the live
        run streams its trace in chunks). Afterwards `state_at` answers as a
        Timeline built from all the events at once would; an event earlier
        than the last one held raises ValueError."""
        events = list(events)
        # A live timeline answers `moves_between` with the chunk just applied (L1). A
        # chunk with nothing to show has no moves: the last one's may not be answered
        # again (#337).
        self._live_moves = []
        if not events:
            return
        if self.live:
            self._append_live(events)
            return
        last = self._times[-1] if self._times else float("-inf")
        for event in events:
            if event.t < last:
                raise ValueError(f"Timeline.append: event at t={event.t} is earlier than "
                                 f"the last one held (t={last})")
            last = event.t
        if not self._attributed and any("/" in (e.block or "") for e in events):
            # The first submodel event: attribute everything, as __init__ does
            # (attribution is idempotent on the events already held).
            self.__init__(self._events + events, self.view)
            return
        if self._attributed:
            events = _attribute(events, self.view)
        if events and events[0].t <= self._last_applied_time:
            # The cursor already applied that instant: events and synthetic
            # arrivals at one t would interleave differently from a one-pass
            # build, so replay from the start (never happens when the caller
            # only reads up to the last event it appended).
            self._rewind()
        for e in events:
            self._events.append(e)
            self._times.append(e.t)
            if e.kind == "entity_moved" and e.target and e.target != e.block:
                self._moves.append((e.entity_id, e.e_type, e.block, e.target, e.t, e.branch))
                self._move_times.append(e.t)
                if e.duration > 0:
                    # Later than every arrival already applied (e.t > the
                    # cursor's time), so it lands after the arrival cursor.
                    arrival = (e.t + e.duration, e.entity_id, e.e_type, e.target)
                    insort_right(self._arrivals, arrival, key=lambda a: a[0])
                    insort_right(self._arrival_times, arrival[0])
            elif e.kind == "entity_created":
                self._created_times.append(e.t)
            elif e.kind == "entity_disposed":
                self._disposed_times.append(e.t)
            elif e.kind == "level_changed" and e.resource:
                times, values = self._levels.setdefault(e.resource, ([], []))
                times.append(e.t)
                values.append(e.value)

    def _append_live(self, events: list) -> None:
        """Bounded-memory sibling of `append` for the live game canvas: applies
        every event straight away via `_apply` and keeps no event log, so a
        chunk costs time/memory proportional to what is CURRENTLY alive, not
        to the game's length. Reuses `_apply`/`_enter`/`_leave` -- only the
        chunking/flush control below is new.

        Every arrival due at or before an event fires before it (#231, as in
        `state_at`), so a chunk boundary inside one instant needs no undo.
        """
        last = self._last_applied_time
        for event in events:
            if event.t < last:
                raise ValueError(f"Timeline.append: event at t={event.t} is earlier than "
                                 f"the last one held (t={last})")
            last = event.t
        if not self._attributed and any("/" in (e.block or "") for e in events):
            # Safe with no replay: view=="" started unattributed only because
            # nothing with a "/" had been seen yet, so whatever is already
            # applied has none either -- the very first offending event is
            # always caught in THIS call, on its own events.
            self._attributed = True
        if self._attributed:
            events = _attribute(events, self.view)
            if not events:
                # The whole chunk belonged to a currently invisible branch:
                # nothing to apply or flush.
                return
        live_moves = []
        for e in events:
            self._fire_arrivals(e.t)
            self._apply(e)
            if e.kind == "entity_moved" and e.target and e.target != e.block:
                live_moves.append((e.entity_id, e.e_type, e.block, e.target, e.t, e.branch))
            elif e.kind == "level_changed" and e.resource:
                # Live mode never interpolates a level's past trajectory (a
                # canvas query is always "now"), so the latest breakpoint is
                # all `level_at` needs -- kept, not appended, to stay O(1).
                self._levels[e.resource] = ([e.t], [e.value])
            self._last_applied_time = e.t
        self._live_moves = live_moves
        self._live_count += len(events)
        self._fire_arrivals(self._last_applied_time)

    def _fire_arrivals(self, t: float) -> None:
        """Enter every delayed-link arrival due at or before *t*. Called before
        each event is applied (#231): an entity arrives before anything it
        does at that instant -- its own departure from the target included,
        which would otherwise find it absent and leave it counted there."""
        if self.live:
            # No arrival log in live mode: scan what is in transit now.
            for entity_id, transit in list(self._in_transit.items()):
                _block, target, t0, _travel, e_type, _path, _branch, duration = transit
                if t0 + duration <= t:     # the arrival rule of a recorded timeline (#340)
                    del self._in_transit[entity_id]
                    self._enter(entity_id, e_type, target)
            return
        while (self._arrival_cursor < len(self._arrivals)
               and self._arrival_times[self._arrival_cursor] <= t):
            arrival_t, entity_id, e_type, block = self._arrivals[self._arrival_cursor]
            self._last_applied_time = max(self._last_applied_time, arrival_t)
            self._arrival_cursor += 1
            # The transit this arrival ends may be over already: the entity moved on
            # at an instant the arrival, recomputed here as t + duration, lands a
            # float after (#336). It then has no transit, or a later one, which
            # cannot end before its own travel has passed.
            transit = self._in_transit.get(entity_id)
            # Its target too: leg 2's arrival, due a float before leg 1's, is not leg 1's.
            if transit is not None and transit[1] == block and transit[2] + transit[3] <= arrival_t:
                del self._in_transit[entity_id]
                self._enter(entity_id, e_type, block)

    def __len__(self) -> int:
        return self._live_count if self.live else len(self._events)

    @property
    def duration(self) -> float:
        if self.live:
            return 0.0 if self._last_applied_time == float("-inf") else self._last_applied_time
        return self._times[-1] if self._times else 0.0

    # -- state ---------------------------------------------------------------
    def state_at(self, t: float) -> dict:
        """`{"entities": {id: (block, e_type)}, "counts": {block: n},
        "in_service": {block: n}, "in_transit": {id: (src, dst, progress, e_type)},
        "resources": {name: {"state", "busy", "capacity"}},
        "queues": {block: [entity ids, arrival order, excluding in-service]},
        "levels": {name: value}}` at time t.

        `in_service[block]` counts entities in `block` that have an
        `entity_seized` there not yet followed by their leaving it (queued
        entities in a Seize or Process block are in `counts` but not
        `in_service`). `in_transit` (ROADMAP 1.5) holds an entity moving over a
        delayed link between its departure and arrival: `progress` is linear,
        0 at departure and 1 at arrival; such an entity is in no block's
        `counts` for that window.

        `chargers` is `{charger: occupied points}` and `charging` the set of
        `(fleet, unit)` vehicles on a point right now (D164-D166).

        `phases` (D175) is `{block: (segment kind, index, progress)}` for the
        cycle segment a MOTION block is in, and `tending` `{(fleet, unit):
        (station, "pick" | "place", progress)}`; both only while their time runs.
        `handling` is `{(fleet, unit): (unloading, progress)}` for a vehicle
        standing still to load or unload; `placed` counts the `place` segments
        a block has begun (a palletiser's stack). `phases_all` is `phases` for
        every unit at work in a block: `{block: {slot: (kind, index, progress)}}`,
        a part keeping its slot for its whole cycle.

        `levels` (PHASE4 T2, D75) is each level's value at t, interpolated
        linearly between its recorded breakpoints -- see `level_at`.
        `variables` (D608) is the last value of each variable an asset's
        picture follows (flattening marks them `trace`).

        `resources` (ROADMAP 2.3 part C) is built from `entity_seized`/
        `entity_released` (busy count) and `resource_state` events (capacity/
        down/up/blocked): "down" wins over everything, else "off" when the
        scheduled capacity is 0, else "blocked" when a unit is holding a
        finished part with nowhere to put it (PHASE4 T5), else "busy" when at
        least one unit is held, else "idle". A resource never mentioned by any event (no seize, no
        schedule, no failure -- unreachable in practice, `load_from_json`
        emits a baseline capacity event for every declared resource) is
        simply absent, the same way an empty block is absent from `counts`.

        `queues` is generic, not SEIZE/PROCESS-specific: any block's entities
        minus its `in_service` ones, in arrival order -- for a block that
        never seizes (DELAY, HOLD, BATCH, MATCH) that is just its full
        occupant list, which is exactly a HOLD/BATCH/MATCH store's contents.

        `entity_agents` (PHASE6 H4) is `{id: (state, category)}` for a live
        entity created with `entity.agent` set -- read straight from the
        agent's own `agent_state` trace (the same one `agents["category"]`
        below uses), so it works for an agent with no `space` too, which
        never appears in `agents` (that map needs an x, y `agent_moved`
        segment; an entity token needs neither). Absent for an entity with
        no agent.

        Inclusive of events exactly at t. All dicts are copies, so a caller may
        keep them across frames.
        """
        if t < self._last_applied_time:
            if self.live:
                # No event log to replay from -- forward only, by design (L1).
                raise ValueError("Timeline (live): state_at cannot seek backward")
            self._rewind()  # seeking backwards: only a full replay is correct
        while self._cursor < len(self._events) and self._times[self._cursor] <= t:
            self._fire_arrivals(self._times[self._cursor])
            self._apply(self._events[self._cursor])
            self._last_applied_time = max(self._last_applied_time, self._times[self._cursor])
            self._cursor += 1
        self._fire_arrivals(t)
        in_transit = {
            entity_id: (src, dst, _progress(t, t0, travel), e_type, path, branch)
            for entity_id, (src, dst, t0, travel, e_type, path, branch, _duration)
            in self._in_transit.items()
        }
        return {"entities": dict(self._entities), "counts": dict(self._counts),
                "in_service": dict(self._in_service_counts), "in_transit": in_transit,
                "resources": self._resources_state(), "queues": self._queues(),
                "movers": self._movers_state(t),
                "soc": dict(self._soc), "chargers": {c: len(u) for c, u in self._charging.items()},
                "charging": {unit for units in self._charging.values() for unit in units},
                "phases": {block: (kind, index, _progress(t, t0, span))
                           for block, (t0, span, kind, index, _eid) in self._phases.items()
                           if t < t0 + span},
                "handling": {unit: (unloading, _progress(t, t0, span))
                             for unit, (t0, span, unloading) in self._handling.items()
                             if t < t0 + span},
                "placed": dict(self._placed),
                "phases_all": {block: {slot: (kind, index, _progress(t, t0, span))
                                       for t0, span, kind, index, slot in arms.values()
                                       if t < t0 + span}
                               for block, arms in self._arms.items() if arms},
                "tending": {unit: (station, which, _progress(t, t0, span))
                            for unit, (t0, span, station, which) in self._tends.items()
                            if t < t0 + span},
                "levels": {name: self.level_at(name, t) for name in self._levels},
                "variables": dict(self._variables),
                "agents": self._agents_state(t),
                "agent_links": sorted(self._agent_links),
                "entity_agents": self._entity_agents_state()}

    def level_at(self, name: str, t: float) -> float | None:
        """A level's value at *t*, interpolated LINEARLY between the recorded
        breakpoints (PHASE4 T2, D75) -- which is exact, because a level is
        piecewise linear between two re-evaluations by construction (D60).

        Before the first breakpoint and after the last one the nearest value
        holds. None for a level the trace never mentioned."""
        recorded = self._levels.get(name)
        if recorded is None:
            return None
        times, values = recorded
        index = bisect_right(times, t) - 1
        if index < 0:
            return values[0]
        if index >= len(times) - 1:
            return values[-1]
        span = times[index + 1] - times[index]
        if span <= 0:
            return values[index]
        share = (t - times[index]) / span
        return values[index] + (values[index + 1] - values[index]) * share

    def moves_between(self, t0: float, t1: float) -> list:
        """Transfers in the window (t0, t1] as (id, e_type, src, dst, t, branch).

        *branch* is which outgoing link of *src* was taken (-1 when unknown).

        Half-open so consecutive windows tile the run without double counting.

        Live mode keeps no move log: the only caller in that mode
        (`TracePlayer.follow`) always asks for exactly the window just
        applied, which is `_live_moves` (PHASE6 L1).
        """
        if self.live:
            return self._live_moves
        return self._moves[bisect_right(self._move_times, t0):bisect_right(self._move_times, t1)]

    def created_until(self, t: float) -> int:
        if self.live:
            # A running counter, not a bisect over history (L1): every live
            # query is "now", so *t* is always the same as _last_applied_time.
            return self._created_count
        return bisect_right(self._created_times, t)

    def disposed_until(self, t: float) -> int:
        if self.live:
            return self._disposed_count
        return bisect_right(self._disposed_times, t)

    def next_time(self, t: float) -> float | None:
        """Time of the first event strictly after t, or None past the end."""
        i = bisect_right(self._times, t)
        return self._times[i] if i < len(self._times) else None

    # -- incremental replay --------------------------------------------------
    def _rewind(self) -> None:
        self._cursor = 0
        self._arrival_cursor = 0
        self._last_applied_time = float("-inf")
        self._entities: dict[int, tuple[str, str]] = {}
        self._counts: dict[str, int] = {}
        self._in_service: set[int] = set()
        self._in_service_counts: dict[str, int] = {}
        # entity -> (source, target, left at, travel, type, path, branch, duration)
        self._in_transit: dict[int, tuple[str, str, float, float, str, tuple, int, float]] = {}
        # PHASE4 E1: (fleet, unit) -> (started, travel, path, state), the last
        # leg each vehicle was given. Purely trace-derived, like everything here.
        self._movers: dict[tuple, tuple] = {}
        # D164-D166: (fleet, unit) -> the last SoC the trace reported (stepped), and
        # charger -> the (fleet, unit) vehicles on its points right now.
        self._soc: dict[tuple, float] = {}
        self._charging: dict[str, set] = {}
        # D175: block -> (started, duration, kind, index, entity) of its current
        # cycle segment; (fleet, unit) -> (started, duration, station, which).
        self._phases: dict[str, tuple] = {}
        self._tends: dict[tuple, tuple] = {}
        self._arms: dict[str, dict[int, tuple]] = {}    # block -> entity -> (..., slot)
        self._placed: dict[str, int] = {}           # block -> `place` segments begun so far
        self._handling: dict[tuple, tuple] = {}     # (fleet, unit) -> (started, duration, unloading)
        # Per-block occupants in arrival order (dict: insertion-ordered, O(1)
        # removal) -- queues() filters out the in-service ones on read.
        self._block_order: dict[str, dict[int, None]] = {}
        # Resource state (ROADMAP 2.3 part C), keyed by resource name.
        self._resource_busy: dict[str, int] = {}
        self._resource_capacity: dict[str, float] = {}
        self._resource_down: dict[str, bool] = {}
        # PHASE4 T5: units held by a finished part with nowhere to go (D78).
        self._resource_blocked: dict[str, int] = {}
        # PHASE5 AB-3: (pop, index) -> its current motion segment / state,
        # replayed the same incremental way as `_movers` above.
        self._agent_moves: dict[tuple, tuple] = {}
        self._agent_states: dict[tuple, str] = {}
        self._agent_categories: dict[tuple, str] = {}   # PHASE6 A2: color_by's current value
        self._agent_links: set[tuple] = set()           # PHASE6 A5: (population, i, j)
        self._entity_agents: dict[int, tuple] = {}      # PHASE6 H4: entity id -> (population, index)
        self._variables: dict[str, float] = {}          # D608: a traced variable's last value
        # Live mode only (PHASE6 L1): running totals instead of the sorted
        # timestamp lists `created_until`/`disposed_until` bisect over, this
        # chunk's moves, and a raw-event counter for `__len__` -- see
        # `_append_live`.
        self._created_count = 0
        self._disposed_count = 0
        self._live_moves: list = []
        self._live_count = 0

    def _apply(self, e) -> None:
        if e.kind == "entity_created":
            self._enter(e.entity_id, e.e_type, e.block)
            if self.live:
                self._created_count += 1
            if e.target is not None:      # PHASE6 H4: entity.agent rode target/value (trace.py)
                self._entity_agents[e.entity_id] = (e.target, int(e.value))
        elif e.kind == "mover_moved":
            # PHASE4 E1: one vehicle's leg. `travel` (the driving part of
            # `duration`) is what the interpolation uses, so a mover standing
            # still to load is not drawn creeping forward.
            key = (e.target, e.entity_id)
            previous = self._movers.get(key)
            self._handling.pop(key, None)
            if e.state == "handling":
                # Standing still to load or unload: it stays at the end of the
                # path it came by, and the leg before says which of the two.
                self._handling[key] = (e.t, e.duration,
                                       previous is not None and previous[3] == "loaded")
                self._movers[key] = (e.t, 0.0, previous[2] if previous else tuple(e.path),
                                     "handling")
            elif (e.state == "idle" and len(e.path) == 1 and previous is not None
                  and previous[2] and previous[2][-1] == e.path[0]):
                # Parked where it arrived: the same rule, so it keeps its heading.
                self._movers[key] = (e.t, 0.0, previous[2], e.state or "idle")
            else:
                self._movers[key] = (e.t, e.travel or e.duration, tuple(e.path),
                                     e.state or "idle")
        elif e.kind in ("battery", "charge_start", "charge_end"):
            self._soc[(e.target, e.entity_id)] = e.value
            if e.kind != "battery":
                units = self._charging.setdefault(e.resource, set())
                (units.add if e.kind == "charge_start" else units.discard)((e.target, e.entity_id))
        elif e.kind == "cycle_phase":
            self._phases[e.block] = (e.t, e.duration, e.state, int(e.value), e.entity_id)
            # Several units at work in one block: each part keeps the lowest free
            # slot from its first segment until it leaves, so an arm is not swapped.
            arms = self._arms.setdefault(e.block, {})
            slot = arms[e.entity_id][4] if e.entity_id in arms else min(
                set(range(len(arms) + 1)) - {entry[4] for entry in arms.values()})
            arms[e.entity_id] = (e.t, e.duration, e.state, int(e.value), slot)
            if e.state == "place":
                self._placed[e.block] = self._placed.get(e.block, 0) + 1
        elif e.kind == "variable_changed":
            self._variables[e.resource] = e.value
        elif e.kind == "tend":
            self._tends[(e.target, e.entity_id)] = (e.t, e.duration, e.resource, e.state)
        elif e.kind == "agent_moved":
            # PHASE5 AB-3: one motion segment; a zero-length one (placement, or
            # a move_to landing exactly at its own start) is a stationary
            # point, `_progress`'s span<=0 rule already covers it.
            self._agent_moves[(e.target, e.entity_id)] = (e.t, e.t + e.duration,
                                                          e.x0, e.y0, e.x1, e.y1)
        elif e.kind == "agent_state":
            self._agent_states[(e.target, e.entity_id)] = e.state
            self._agent_categories[(e.target, e.entity_id)] = e.resource or ""  # PHASE6 A2
        elif e.kind == "agent_removed":
            # PHASE6 C5: the token disappears; a newborn in the same slot comes
            # back with its own agent_moved/agent_state.
            self._agent_moves.pop((e.target, e.entity_id), None)
            self._agent_states.pop((e.target, e.entity_id), None)
            self._agent_categories.pop((e.target, e.entity_id), None)
            # PHASE6 A5: the engine drops a dead agent's links without an event.
            self._agent_links = {link for link in self._agent_links
                                 if link[0] != e.target or e.entity_id not in link[1:]}
        elif e.kind == "agent_linked":
            self._agent_links.add((e.target, e.entity_id, int(e.value)))
        elif e.kind == "agent_unlinked":
            self._agent_links.discard((e.target, e.entity_id, int(e.value)))
        elif e.kind == "entity_moved":
            if e.target == e.block:
                # A move inside the instance being viewed from outside. The part has
                # left the inner block it was cycling in, so it gives up its arm.
                self._arms.get(e.block, {}).pop(e.entity_id, None)
                if self._phases.get(e.block, (None,) * 5)[4] == e.entity_id:
                    del self._phases[e.block]
                return
            self._leave(e.entity_id)
            if e.target and e.duration > 0:
                self._in_transit[e.entity_id] = (e.block, e.target, e.t,
                                                 e.travel or e.duration, e.e_type, tuple(e.path),
                                                 e.branch, e.duration)
            elif e.target:
                self._enter(e.entity_id, e.e_type, e.target)
        elif e.kind == "entity_disposed":
            self._leave(e.entity_id)
            if self.live:
                self._disposed_count += 1
            self._entity_agents.pop(e.entity_id, None)   # PHASE6 H4: token gone for good
        elif e.kind == "entity_absorbed":
            # #319: gone into a batch (or a batch split up): out of its block,
            # without counting as a disposal.
            self._leave(e.entity_id)
            self._entity_agents.pop(e.entity_id, None)
        elif e.kind == "entity_seized":
            if e.block:     # "" = seized by a block outside the level being viewed
                self._mark_in_service(e.entity_id, e.block)
            # The units the seize holds (#748); an older trace carries none, so 1.
            self._resource_busy[e.target] = self._resource_busy.get(e.target, 0) + (int(e.value) or 1)
        elif e.kind == "entity_released":
            # entity_released changes no block occupancy: it fires on the
            # Release block, not the Seize block the entity is still counted
            # in. It DOES free the resource the busy count is keyed on.
            if e.target in self._resource_busy:
                self._resource_busy[e.target] = max(self._resource_busy[e.target] - (int(e.value) or 1), 0)
        elif e.kind == "resource_state":
            if e.state == "capacity":
                self._resource_capacity[e.resource] = e.value
            elif e.state == "down":
                self._resource_down[e.resource] = True
            elif e.state == "up":
                self._resource_down[e.resource] = False
            elif e.state == "blocked":
                # A count, not a flag: a station with two machines can have one
                # blocked and one working (PHASE4 T5).
                held = self._resource_blocked.get(e.resource, 0) + (1 if e.value else -1)
                self._resource_blocked[e.resource] = max(held, 0)

    def _enter(self, entity_id: int, e_type: str, block: str) -> None:
        self._entities[entity_id] = (block, e_type)
        self._counts[block] = self._counts.get(block, 0) + 1
        self._block_order.setdefault(block, {})[entity_id] = None

    def _leave(self, entity_id: int) -> None:
        # #336: an entity that moves on or ends is on no link any more, whatever a
        # recomputed arrival time says.
        self._in_transit.pop(entity_id, None)
        previous = self._entities.pop(entity_id, None)
        if previous is None:
            return
        block = previous[0]
        if self._phases.get(block, (None,) * 5)[4] == entity_id:
            del self._phases[block]         # the part left mid-segment
        self._arms.get(block, {}).pop(entity_id, None)
        remaining = self._counts.get(block, 0) - 1
        # Empty blocks drop out of `counts` so the animator only has to draw the
        # badges it is given.
        if remaining > 0:
            self._counts[block] = remaining
        else:
            self._counts.pop(block, None)
        order = self._block_order.get(block)
        if order is not None:
            order.pop(entity_id, None)
            if not order:
                del self._block_order[block]
        if entity_id in self._in_service:
            self._in_service.discard(entity_id)
            left = self._in_service_counts.get(block, 0) - 1
            if left > 0:
                self._in_service_counts[block] = left
            else:
                self._in_service_counts.pop(block, None)

    def _mark_in_service(self, entity_id: int, block: str) -> None:
        if entity_id in self._in_service:
            return
        self._in_service.add(entity_id)
        self._in_service_counts[block] = self._in_service_counts.get(block, 0) + 1

    # -- resources and queues (ROADMAP 2.3 part C) ---------------------------
    def _resources_state(self) -> dict:
        names = (set(self._resource_capacity) | set(self._resource_busy)
                 | set(self._resource_down) | set(self._resource_blocked))
        out = {}
        for name in names:
            capacity = self._resource_capacity.get(name)
            busy = self._resource_busy.get(name, 0)
            if self._resource_down.get(name, False):
                state = "down"
            elif capacity == 0:
                state = "off"
            elif self._resource_blocked.get(name, 0) > 0:
                # Blocked beats busy: the unit IS held, but the interesting
                # thing about it is that it cannot let go (PHASE4 T5, D78).
                state = "blocked"
            elif busy > 0:
                state = "busy"
            else:
                state = "idle"
            out[name] = {"state": state, "busy": busy, "capacity": capacity}
        return out

    def _movers_state(self, t: float) -> dict:
        """`{(fleet, unit): (path, progress, state)}` at *t* (PHASE4 E1).

        Coordinate-free on purpose: this module knows nothing about the network,
        so the caller turns `(path, progress)` into a point with
        `network.point_on_path` -- the same pure function the tests use.
        """
        out = {}
        for key, (t0, travel, path, state) in self._movers.items():
            if state == "handling":
                # Its handling time is over and no leg followed (a cobot that
                # stays at its machine, a trace cut at the cap): it stands idle.
                started, span, _unloading = self._handling.get(key, (t0, 0.0, False))
                if t >= started + span:
                    state = "idle"
            out[key] = (path, _progress(t, t0, travel), state)
        return out

    def _agents_state(self, t: float) -> dict:
        """`{(population, index): (x, y, state, category)}` at *t* (ARCHITECTURE
        5.25, PHASE5 AB-3): linear between the segment's own endpoints -- exact
        for a continuous mover; a network mover's on-screen token takes the
        same straight chord between its departure and arrival points (the
        engine's own NEAR/NEAREST stay exact off the real polyline via
        `AgentSystem._position`, which this playback layer does not need).
        *category* (PHASE6 A2) is the population's `color_by` attribute's
        current value, or "" without one."""
        out = {}
        for key, (t0, t1, x0, y0, x1, y1) in self._agent_moves.items():
            f = _progress(t, t0, t1 - t0)
            out[key] = (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, self._agent_states.get(key, ""),
                       self._agent_categories.get(key, ""))
        return out

    def _entity_agents_state(self) -> dict:
        """`{entity id: (state, category)}` (PHASE6 H4) for every live entity
        that has an agent -- `_agent_states`/`_agent_categories` are the same
        `agent_state`-trace maps `_agents_state` reads, so this works with no
        `agent_moved` segment (no `space`) at all."""
        return {eid: (self._agent_states.get(ref, ""), self._agent_categories.get(ref, ""))
                for eid, ref in self._entity_agents.items()}

    def _queues(self) -> dict:
        out = {}
        for block, order in self._block_order.items():
            waiting = [eid for eid in order if eid not in self._in_service]
            if waiting:
                out[block] = waiting
        return out


# -- derivations (PHASE7 7.7, D196) ---------------------------------------------------
# What every view of a run reads besides `state_at`: the editor's 2D and 3D views and
# the HTML player's tape (`core/tape.py`). They take the layout as saved
# (`ModelDocument.to_layout()` or a loaded file), never a document or a widget, so one
# copy serves a view with no editor behind it.

def _level_blocks(layout: dict, level: str = "") -> list:
    """The blocks of the level being viewed: the model's own, or those of the
    definition named *level* (the editor with a submodel open)."""
    if level:
        return ((layout.get("submodels") or {}).get(level) or {}).get("blocks") or []
    return layout.get("blocks") or []


def asset_state_sources(layout: dict, owns=None, *, level: str = "") -> dict:
    """{instance block -> (flattened source name, own chargers or None)} for
    every submodel instance whose asset graphic nominates a `state_source`.

    The source is the asset's own resource, charger, fleet or variable, told
    apart by what the name is among the records the definition owns (a
    variable carries None, like a resource, and is read from `variables`). `$Machine`
    inside a definition flattens to `<instance>/Machine` (core/submodels.py
    item 6), which is exactly the name the trace reports it under -- so the
    mapping is built once here and a frame costs one lookup per asset.
    A resource carries no chargers (None); a charger carries itself; a fleet
    carries the chargers of the same instance.

    *owns*(definition) gives the records a definition owns; the default reads
    the definition's `info["owns"]`. The editor passes the document's own
    lookup, which also asks the library for a file older than that field."""
    definitions = layout.get("submodels") or {}
    out: dict[str, tuple[str, tuple | None]] = {}
    for block in _level_blocks(layout, level):
        if block.get("type") != "SUBMODEL":
            continue
        name = block.get("name")
        definition = (block.get("params") or {}).get("definition", "")
        meta = definitions.get(definition) or {}
        info = meta.get("info") or {}
        source = (info.get("graphics") or {}).get("state_source")
        if not source:
            continue
        owned = (owns(definition) if owns is not None else info.get("owns")) or {}
        scoped = meta.get("scoped") or {}
        chargers = {*owned.get("chargers", ()), *(scoped.get("chargers") or {})}
        fleets = {*owned.get("fleets", ()), *(scoped.get("fleets") or {})}
        own = ((source,) if source in chargers
               else tuple(sorted(chargers)) if source in fleets else None)
        out[name] = (f"{name}/{source}",
                     None if own is None else tuple(f"{name}/{c}" for c in own))
    return out


def asset_states(state: dict, sources: dict) -> dict[str, str]:
    """{instance block -> picture state} for one playback state; *sources* is
    `asset_state_sources`.

    A resource source gives its own `resources` state (busy while in use,
    down while failed, off while its capacity schedule is zero). A charger
    is busy while a point is occupied. A fleet is down/off with its own
    resource state, else busy only while a vehicle charges at the fleet
    asset's OWN charger -- vehicles out on jobs are not "busy" here. A
    variable (D608) is busy while above zero, else idle."""
    resources = state.get("resources") or {}
    occupied = state.get("chargers") or {}
    variables = state.get("variables") or {}
    out: dict[str, str] = {}
    for block, (source, chargers) in sources.items():
        if source in variables:
            out[block] = "busy" if variables[source] > 0 else "idle"
            continue
        info = resources.get(source)
        if chargers is None:
            if info is not None:
                out[block] = info["state"]
        elif info is not None and info["state"] in ("down", "off"):
            out[block] = info["state"]
        else:
            out[block] = "busy" if any(occupied.get(c) for c in chargers) else "idle"
    return out


def unit_sources(layout: dict, state_sources: dict, *, level: str = "") -> dict:
    """{flattened fleet / resource / charger name -> (kind, block, units)}:
    what each block of this level counts units of.

    The block is the visible one naming the record in the FLATTENED model
    (a TRANSPORT's `fleet`, a PROCESS/SEIZE's `resource_name`), so a placed
    asset and a hand-written block are found the same way; a fleet or a
    charger no block names belongs to its instance. One source per block:
    its fleet, else the asset's state source (*state_sources*), else the
    first resource it names, else its charger. A resource's units are its
    capacity at the frame (0 here); a fleet or charger with one unit is
    left out. Empty when the submodels do not flatten."""
    # Lazy: flattening pulls in the block registry, which a Timeline alone never needs.
    from nagasim.core import submodels

    try:
        flat = submodels.flatten(layout)
    except submodels.SubmodelError:
        return {}
    top = {block.get("name") for block in _level_blocks(layout, level)}
    named: dict[str, dict[str, str]] = {"fleet": {}, "resource_name": {}}
    for block in flat.get("blocks") or []:
        owner = str(block.get("name", "")).split("/", 1)[0]
        if owner not in top:
            continue
        for key, found in named.items():
            record = (block.get("params") or {}).get(key)
            if record:
                found.setdefault(record, owner)

    def owner_of(name, found=None):
        instance = name.split("/", 1)[0] if "/" in name else None
        return (found or {}).get(name) or (instance if instance in top else None)

    preferred = {source for source, _ in state_sources.values()}
    resources = [str(r.get("name", "")) for r in flat.get("resources") or []]
    candidates = [
        *(("fleet", str(f.get("name", "")), named["fleet"], f.get("count"))
          for f in flat.get("fleets") or []),
        *(("resource", name, named["resource_name"], 0)
          for name in sorted(resources, key=lambda name: name not in preferred)),
        *(("charger", str(c.get("name", "")), None, c.get("points", 1))
          for c in flat.get("chargers") or [])]
    out: dict[str, tuple[str, str, int]] = {}
    taken: set[str] = set()
    for kind, name, found, units in candidates:
        block = owner_of(name, found)
        if block is None or block in taken:
            continue
        if kind != "resource" and not (isinstance(units, int) and units > 1):
            continue
        taken.add(block)
        out[name] = (kind, block, units)
    return out


def unit_counts(state: dict, sources: dict) -> dict[str, list[tuple[str, int]]]:
    """{block -> [(state, units), ...]} for one playback state; *sources* is
    `unit_sources`. A fleet's vehicles as idle / working / charging / out, a
    multi-unit resource's units as busy / free (out while it is down), a
    charger's points as occupied / free. Zero counts and single-unit blocks
    are left out, as is a fleet or resource the trace has not mentioned.

    The per-vehicle state is walked once, whatever the number of blocks."""
    if not sources:
        return {}
    resources = state.get("resources") or {}
    occupied = state.get("chargers") or {}
    charging = state.get("charging") or ()
    seen: set[str] = set()
    working: dict[str, int] = {}
    for (fleet, unit), (_path, _progress, mover_state) in (state.get("movers") or {}).items():
        seen.add(fleet)
        if mover_state != "idle" and (fleet, unit) not in charging:
            working[fleet] = working.get(fleet, 0) + 1
    plugged: dict[str, int] = {}
    for fleet, _unit in charging:
        plugged[fleet] = plugged.get(fleet, 0) + 1

    out: dict[str, list[tuple[str, int]]] = {}
    for name, (kind, block, units) in sources.items():
        info = resources.get(name)
        if kind == "charger":
            used = min(occupied.get(name, 0), units)
            rows = [("occupied", used), ("free", units - used)]
        elif kind == "fleet":
            if info is None and name not in seen and name not in plugged:
                continue
            capacity = info["capacity"] if info is not None else None
            if info is not None and info["state"] in ("down", "off"):
                away = units
            elif isinstance(capacity, (int, float)):
                away = min(max(units - int(capacity), 0), units)
            else:
                away = 0
            charge = min(plugged.get(name, 0), units - away)
            work = min(working.get(name, 0), units - away - charge)
            rows = [("idle", units - away - charge - work), ("working", work),
                    ("charging", charge), ("out", away)]
        else:
            capacity = info["capacity"] if info is not None else None
            if not isinstance(capacity, (int, float)) or capacity <= 1:
                continue
            busy = min(info["busy"], int(capacity))
            rows = [("busy", busy),
                    ("out" if info["state"] == "down" else "free", int(capacity) - busy)]
        out[block] = [(label, count) for label, count in rows if count > 0]
    return out


def resource_rows(state: dict) -> list[dict]:
    """One row per resource the trace has mentioned up to this state: name,
    state, busy count and capacity, sorted by name."""
    return [{"name": name, "state": info["state"], "busy": info["busy"],
             "capacity": info["capacity"]}
            for name, info in sorted((state.get("resources") or {}).items())]


def kpi_snapshot(timeline: Timeline, t: float) -> dict:
    """WIP, created, completed and elapsed sim time at *t* -- everything
    computed from the Timeline, no engine access."""
    created = timeline.created_until(t)
    completed = timeline.disposed_until(t)
    return {"t": t, "wip": created - completed, "created": created, "completed": completed}


def node_blocks(layout: dict, *, level: str = "") -> dict[str, str]:
    """Which schematic block stands for a network node: an instance block
    for a node the instance's geometry contributed (`<instance>/<node>`,
    D69), else a block whose own `node` parameter names it."""
    blocks = _level_blocks(layout, level)
    names = {block.get("name") for block in blocks}
    out: dict[str, str] = {}
    for block in blocks:
        for key in ("node", "from_node", "to_node"):
            node = (block.get("params") or {}).get(key)
            if isinstance(node, str) and node and "$" not in node:
                out.setdefault(node, block.get("name"))
    for node in (layout.get("network") or {}).get("nodes") or []:
        node_name = str(node.get("name", ""))
        head = node_name.rsplit("/", 1)[0] if "/" in node_name else ""
        while head and head not in names:
            head = head.rsplit("/", 1)[0] if "/" in head else ""
        if head:
            out.setdefault(node_name, head)
    return out


def _resolve_state_colors(states: list) -> dict:
    """name -> its own `color`, or (PHASE6 K2) the nearest ancestor's --
    computed once per agent type so a leaf that declares none still shows
    its composite's colour. One shared resolution: `state_colors` reads it
    for every view of a run."""
    by_name = {s.get("name"): s for s in states if isinstance(s, dict) and s.get("name")}
    resolved: dict = {}

    def resolve(name, seen):
        if name in resolved:
            return resolved[name]
        state = by_name.get(name)
        color = state.get("color") if state else None
        if not color and state and state.get("parent") and state["parent"] not in seen:
            color = resolve(state["parent"], seen | {name})
        resolved[name] = color
        return color

    for name in by_name:
        resolve(name, frozenset())
    return resolved


def _hex_color(color) -> str | None:
    """A palette name, or the `#rrggbb` the statechart editor saves, as
    `#rrggbb`; None for anything else, so a declared colour is never passed on
    as free text."""
    if not color:
        return None
    text = str(color)
    if text.upper() in cp.COLORS:
        return cp.COLORS[text.upper()].lower()
    return text.lower() if re.fullmatch(r"#[0-9A-Fa-f]{6}", text) else None


def state_colors(layout: dict, parse=None) -> dict:
    """state or category name -> its declared colour, read off the layout's
    `agent_types` (a state's own `color`, else its nearest ancestor's) and
    `populations` (`colors`: category -> colour, PHASE6 A2). Keyed by name
    alone (not per type): `Timeline.state_at` names only the state, and two
    types sharing a state name sharing its colour too is a fine
    simplification for a token this small.

    *parse* turns a declared colour into what the caller draws with, or None
    to leave that name uncoloured: `_hex_color` by default, a QColor parser in
    the editor."""
    parse = parse or _hex_color
    out = {}
    for kind in layout.get("agent_types") or []:
        for name, color in _resolve_state_colors(kind.get("states") or []).items():
            if (parsed := parse(color)) is not None:
                out[name] = parsed
    for pop in layout.get("populations") or []:
        for category, color in (pop.get("colors") or {}).items():
            if (parsed := parse(color)) is not None:
                out[category] = parsed
    return out
