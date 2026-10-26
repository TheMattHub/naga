"""CONVEYOR: a belt with a length, a speed and a spacing (PHASE4 E1).

A conveyor is not a DELAY: it holds a bounded number of entities, it will not
accept one before the entry zone has cleared, and it reacts to a blocked exit.
All three are exact, event-driven consequences of three numbers -- `length`,
`speed`, `spacing` -- and nothing here steps time or simulates positions.

    capacity        = floor(length / spacing) cells, at least 1
    travel time     = length / speed
    entry spacing   = consecutive entries are at least spacing / speed apart,
                      so the belt can never feed faster than speed / spacing

**What blocks the entry.** The cells ARE a finite space (PHASE4 T7,
ARCHITECTURE D78): a machine that has finished a part keeps holding it -- and
its resource -- until the belt has a cell free and the entry rhythm lets the
part on. A full or stopped belt therefore stops the station feeding it, instead
of queueing invisibly in front of it.

**What blocks the exit.** Two things, and a part leaves only once both hold.
A DOWNSTREAM BELT (PHASE6 W3 M6, D115): when the belt's walk through
transparent blocks (DECIDE, ASSIGN, RECORD, STATION, LOGIC) reaches another
CONVEYOR, its end blocks after service like a station (the D78 token) -- the
end part keeps its cell until the next belt physically takes it, a DECIDE
divert blocks on a full chosen branch, and belts merging on one entry queue
FIFO on its cells. Any other downstream block accepts on arrival, as before.
And the end stop, `exit_resource`, taken FIRST and held while the part waits
for the next belt. When set, an entity at the end must take one unit before
leaving, and it CARRIES it onward, so the natural pairing is a PROCESS with
action "Delay Release" (or a plain DELAY + RELEASE) on the same resource at the
far end. With neither, the exit never blocks and the conveyor is a delay with
a capacity and an entry rhythm, which is what a free-running belt is.

**The two modes**, once the exit is blocked:

* `accumulating` (default): the blocked entity stops at the end, the ones
  behind keep moving until they pack against it, each taking `spacing` of belt.
  A part reaches the end no sooner than `spacing / speed` after the one ahead
  left it -- the time to close the gap it left (#950: also when it arrives after).
* non-accumulating: the whole belt STOPS. Nobody moves, nothing new gets on,
  and every entity's remaining travel resumes exactly where it stopped.

The END blocks on a downstream belt only (ARCHITECTURE D68, kept by D115); a
divert along the belt blocks on any finite space (D852, below).

**Stops** (D732): `stops` are points along the belt -- `at` a distance from
the entry, `time` an expression, an optional `resource` seized for the stop --
where each part halts. A stop is one more place the lead part can wait, with
the end's rules. On an accumulating belt the parts behind pack against it: a
part reaches a stop no sooner than `spacing / speed` after the one before left
it, and each section between two stops holds ceil(gap / spacing) parts (at
least one; `_Section`, #1270), so a queue backs up past a stop only by holding
the part there. On
a non-accumulating belt the whole belt stops while a part is at a stop. The
resource is released when the stop's time ends, before the part waits for room
ahead. With no `stops` nothing here changes.

**Diverts** (D851, D852): a divert is a stop that releases sideways. Every part
reaches it by the stop chain; its `condition` is read once there. A part it
takes is pushed for `time` (holding its cell, so the parts behind pack against
it), then handed over on link k + 1 with the D78 token: a full target keeps it
at the divert ("wait"). In "pass" mode a target with no room lets it ride on. A
part it does not take passes with no dwell and no hold. A part reaches a divert by the
section rule of the section after it, taken or not: that section's last part stands
less than a spacing past the divert, so the part's front cannot be there sooner.

**Merges** (D854, accumulating belts only): the parts `from` names join at
`at`. A main part claims the point while its front is within a spacing of it;
a merging part joins with no claim, a spacing after the last part passed and
room in the section after it. Priority "belt" never makes a main part wait;
"merge" holds the main parts a spacing before the point until the join.

**Positions** (D734): every stage change writes one `belt_stage` trace event
(where the part is, where it goes, how long it takes), and `playback.Timeline`
derives each part's position from them; tracing never touches results.
"""
import logging
import math
from collections import deque

import simpy

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.expressions import ExpressionError
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.blocking import Space, accept, arm, check_deadlock
from nagasim.core.seizing import acquire, release
from nagasim.core.stats import Tally, TimeWeighted
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Conveyor", "SPEC"]

_EPS = 1e-9


class _Point:
    """What every stop, divert and merge keeps (D732, D851, D854): its place in `_points`
    (`slot`), the chain of the parts through it, and its waits."""

    code = ""                           # the problem code of a bad point

    def __init__(self, index: int, at: float):
        self.index, self.at = index, at
        self.slot = 0
        self.clear = None               # fires when the last part to reach it leaves it
        self.left = -math.inf           # when that part left
        self.waits = Tally()

    def reset_stats(self, t: float) -> None:
        self.waits.reset()


class _Stop(_Point):
    """One stop's run-time state (D732); `waits` are for its resource."""

    code = "E_CONVEYOR_STOP"

    def __init__(self, env, index: int, at: float, time, resource: str):
        super().__init__(index, at)
        self.time, self.resource = time, resource
        self.parts = 0
        self.busy = TimeWeighted(env.now)       # a part is at the stop
        self.blocked = TimeWeighted(env.now)    # it waits there: for the resource or room ahead

    def reset_stats(self, t: float) -> None:
        super().reset_stats(t)
        self.parts = 0
        self.busy.reset(t)
        self.blocked.reset(t)


class _Divert(_Point):
    """One divert's run-time state (D851): a stop that releases sideways. Divert *index*
    (declared order) leaves by outgoing link index + 1; link 0 is the belt's end. `waits`:
    a diverted part's, from the end of the push to acceptance."""

    code = "E_CONVEYOR_DIVERT"

    def __init__(self, env, index: int, at: float, condition, time, when_full: str):
        super().__init__(index, at)
        self.condition, self.time, self.when_full = condition, time, when_full
        self.exit = index + 1
        self.diverted = self.passed = self.skipped = 0
        self.busy = TimeWeighted(env.now)       # a part pushes or waits there
        self.blocked = TimeWeighted(env.now)    # it waits for its target

    def reset_stats(self, t: float) -> None:
        super().reset_stats(t)
        self.diverted = self.passed = self.skipped = 0
        self.busy.reset(t)
        self.blocked.reset(t)


class _Merge(_Point):
    """One merge's run-time state (D854): parts from `source` join the belt at `at`.

    A main part claims the point while its front is within a spacing of it; a merging
    part joins with no claim and a spacing after the last part passed. `door` lets one
    merging part at a time wait there. `changed` fires when a claim ends, a part joins or
    one leaves the section after it (what the join rule reads), `joined` when a merging
    part is on. `waits`: from arriving at the merge to joining."""

    code = "E_CONVEYOR_MERGE"

    def __init__(self, env, index: int, at: float, source: str, priority: str, cells: int):
        super().__init__(index, at)
        self.source, self.priority = source, priority
        self.claims = 0
        self.door = simpy.Resource(env, 1)
        self.waiting = False
        self.changed = env.event()
        self.joined = env.event()
        self.passers = deque(maxlen=max(cells, 1))   # ids, in the order they passed `at`
        self.merged = 0
        self.waiting_share = TimeWeighted(env.now)      # a merging part waits there

    def reset_stats(self, t: float) -> None:
        super().reset_stats(t)
        self.merged = 0
        self.waiting_share.reset(t)


class _MergeEntry:
    """What the merge's `source` block links to instead of the belt (D854): the belt in
    every respect (name, space, token rules, via `__getattr__`), but a part handed to it
    joins at the merge."""

    def __init__(self, belt, index: int):
        self._belt, self._index = belt, index

    def __getattr__(self, name):
        return getattr(self._belt, name)

    def self_run(self, entity):
        belt = self._belt
        belt.env.process(belt._merge_in(entity, self._index))


class _Section:
    """The belt between two points of an accumulating belt (D732, D853).

    It holds ceil(gap / spacing) parts, the number of fronts that fit in it in free flow
    (#1270: floor() throttled a belt nothing stopped). When that is not a whole number of
    spacings, a full section's last part stands less than a spacing past the start, so a
    part reaches the start no sooner than `lag` after the part `cells` places ahead of it
    left the section's end. `entrants` are the leave events of the last `cells` parts in,
    each succeeded with the time it left, so a tie in event order cannot change the rule."""

    def __init__(self, env, gap: float, spacing: float, speed: float):
        self.cells = max(math.ceil(gap / spacing - _EPS), 1)
        self.pool = simpy.Resource(env, self.cells)
        lag = (self.cells * spacing - gap) / speed
        self.lag = lag if lag > _EPS else 0.0
        self.entrants = deque(maxlen=self.cells)

    def due(self):
        """The leave event a part reaching this section's start waits for, or None."""
        if self.lag and len(self.entrants) == self.cells:
            return self.entrants[0]
        return None

    def enter(self, env):
        """A part is in: its leave event, to succeed with the time it leaves the end."""
        out = env.event()
        self.entrants.append(out)
        return out


class Conveyor(BaseProcess):
    """Conveyor belt that carries parts from one end to the other at a set speed, holds a limited number, and blocks the machine before it when full."""

    # The token rides through (PHASE4 T7): a belt accepts the part when it is
    # physically on it, not when it is offered -- so a full or stopped belt
    # leaves the upstream machine blocked (ARCHITECTURE D78).
    space_passthrough = True

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.length = 10.0
        self.speed = 1.0
        self.spacing = 1.0
        self.accumulating = True
        self.exit_resource = ""
        self.from_node = ""
        self.to_node = ""
        self.stops: list = []           # as declared: [(at, time, resource)] (D732)
        # as declared: [(at, condition, time, when_full)] (D851)
        self.diverts: list = []
        self.merges: list = []          # as declared: [(at, source block, priority)] (D854)
        # Built lazily: the cell space needs the resolved length, which may come
        # from the network, which is only there once the model is loaded. The
        # stops are built with it, entry to end, and `_sections` are the cells of
        # each section between two stops (accumulating belts only).
        self._space = None
        self._length = None             # the resolved belt length, with the space
        self._stops: list = []
        self._diverts: list = []
        self._merges: list = []
        self._points: list = []         # stops, diverts and merges, entry to end
        self._on: set = set()           # ids of the parts on the belt (a merge's `ahead`)
        self._sections: list = []
        self._divert_waits = 0          # parts waiting at a divert for its target
        self._next_entry = 0.0
        self._prev_clear = None
        self._end_left = -math.inf      # when the last part left the end (#950)
        # Non-accumulating belt state: a fresh `stop` event while it runs, a
        # fresh `resume` event while it is stopped. It stops while its end is
        # blocked or a part is at one of its stops (D732).
        self._stopped = False
        self._end_halt = False
        self._holds = 0
        self._stop_event = None
        self._resume_event = None
        # The end part waits for a downstream belt to take it (D115); with a part
        # waiting at a divert (D852), what the deadlock check reads (`held_downstream`),
        # since `blocked` also covers the exit_resource wait.
        self._end_held = False
        self.entered = 0
        self.exited = 0
        self.occupancy = TimeWeighted(env.now)
        self.blocked = TimeWeighted(env.now, level=0.0)
        self.transit = Tally()
        env.ctx.conveyors[self.name] = self

    # -- geometry ------------------------------------------------------------
    @property
    def belt_length(self) -> float:
        """The declared length, or the network distance between its endpoints."""
        if self.length and self.length > 0:
            return float(self.length)
        network = self.ctx.network
        places = self.ctx.places
        a, b = places.get(self.from_node, self.from_node), places.get(self.to_node, self.to_node)
        if network and a and b and network.has(a) and network.has(b):
            return network.distance(a, b)
        raise RuntimeError(f"Conveyor '{self.name}': no length. Set `length`, or give it "
                           f"`from_node`/`to_node` on the path network.")

    @property
    def capacity(self) -> int:
        return max(int(self.belt_length // self.spacing), 1)

    @property
    def travel_time(self) -> float:
        return self.belt_length / self.speed

    @property
    def space(self):
        """The belt's cells as a `blocking.Space` (PHASE4 T7): a full belt is a
        finite space, so the machine feeding it blocks after service exactly as
        it does on a full BUFFER (ARCHITECTURE D78). Built on first use, which
        is `blocking.analyse` -- by then the network that may give the belt its
        length is built."""
        if self._space is None:
            self._space = Space(self.env, self.capacity, self)
            self._build_stops()
        return self._space

    def _build_stops(self) -> None:
        """The points along the belt, stops and diverts, from the entry on, and on an
        accumulating belt the cells of each section between two of them (D732, D851). A
        point off the belt, or two at one place, is refused."""
        length = self._length = self.belt_length
        self._stops, self._sections = [], []
        for index, (at, time, resource) in enumerate(sorted(self.stops, key=lambda s: s[0])):
            self._stops.append(_Stop(self.env, index, at, time, resource))
        self._diverts = [_Divert(self.env, index, *divert)
                         for index, divert in enumerate(self.diverts)]
        self._merges = [_Merge(self.env, index, *merge, self.capacity)
                        for index, merge in enumerate(self.merges)]
        if self._merges and not self.accumulating:
            raise RuntimeError(f"Conveyor '{self.name}': merges need an accumulating belt "
                               f"(E_CONVEYOR_MERGE).")
        self._points = sorted([*self._stops, *self._diverts, *self._merges], key=lambda p: p.at)
        for slot, point in enumerate(self._points):
            code = point.code
            if not 0.0 < point.at < length:
                raise RuntimeError(
                    f"Conveyor '{self.name}': a point at {point.at:g} is not on the belt, which "
                    f"is {length:g} long; `at` is between 0 and the length, both excluded "
                    f"({code}).")
            before = self._points[slot - 1] if slot else None
            # Two stops at one place stay legal (D732); a divert shares its place with none.
            if before is not None and abs(point.at - before.at) <= _EPS \
                    and not (isinstance(point, _Stop) and isinstance(before, _Stop)):
                raise RuntimeError(f"Conveyor '{self.name}': two points share the place "
                                   f"{point.at:g}; a divert or a merge has one of its own "
                                   f"({code}).")
            point.slot = slot
        if self._points and self.accumulating:
            ends = [0.0, *(point.at for point in self._points), length]
            self._sections = [_Section(self.env, b - a, self.spacing, self.speed)
                              for a, b in zip(ends, ends[1:])]

    @property
    def held_downstream(self) -> bool:
        """A part waits for a downstream space: the end's (D115) or a divert's (D852)."""
        return self._end_held or self._divert_waits > 0

    def room_now(self) -> bool:
        """A part handed to this belt now would be on it in this instant: a free cell nobody
        queues for, a first section with room, a running belt and an entry slot due. What
        an upstream divert in "pass" mode reads (D852)."""
        space, now = self.space, self.env.now
        if space.content.level >= space.capacity or space.waiting or self.entry_full:
            return False
        if (not self.accumulating and self._stopped) or self._next_entry > now + _EPS:
            return False
        if self._sections:
            first = self._sections[0]
            due = first.due()
            if due is not None and (not due.triggered or due.value + first.lag > now + _EPS):
                return False
        return True

    # -- the belt clock (non-accumulating only) ------------------------------
    @property
    def stopped(self) -> bool:
        """A non-accumulating belt stopped by its end takes nothing on (the deadlock
        check). A stop holding the belt is not read: its time ends on its own (D732)."""
        return self._end_halt

    @property
    def entry_full(self) -> bool:
        """The first section of a belt with stops is full: free cells or not, nothing
        gets on until a part there moves on (the deadlock check, D732)."""
        return bool(self._sections) and self._sections[0].pool.count >= self._sections[0].cells

    def _end_blocked(self, on: bool) -> None:
        """The end part cannot leave: an accumulating belt records it, a
        non-accumulating one stops (and records it)."""
        if self.accumulating:
            self.blocked.set(1.0 if on else 0.0, self.env.now)
        elif on:
            self._stop_belt()
        else:
            self._start_belt()

    def _stop_belt(self) -> None:
        if self._end_halt:
            return
        self._end_halt = True
        self.blocked.set(1.0, self.env.now)
        self._sync_belt()

    def _start_belt(self) -> None:
        if not self._end_halt:
            return
        self._end_halt = False
        self.blocked.set(0.0, self.env.now)
        self._sync_belt()

    def _hold(self, change: int) -> None:
        """A part reaches (+1) or leaves (-1) a stop of a non-accumulating belt."""
        self._holds += change
        self._sync_belt()

    def _sync_belt(self) -> None:
        """Stop or restart the belt when its reasons to stand change."""
        stopped = self._end_halt or self._holds > 0
        if stopped == self._stopped:
            return
        self._stopped = stopped
        if stopped:
            self._resume_event = self.env.event()
            if self._stop_event is not None and not self._stop_event.triggered:
                self._stop_event.succeed()
        else:
            self._stop_event = self.env.event()
            if self._resume_event is not None and not self._resume_event.triggered:
                self._resume_event.succeed()

    def _at(self, entity, stage: str, x0: float, x1: float | None = None,
            duration: float = 0.0, ahead: int = -1) -> None:
        """One trace event per belt stage (D734): from *x0* to *x1* (standing when None)
        in *duration*, distances from the entry; *ahead*, the part a merged one lands
        behind (D854). `Timeline` reads nothing else."""
        self.emit("belt_stage", entity=entity, state=stage, x0=x0,
                  x1=x0 if x1 is None else x1, duration=duration, value=self.spacing,
                  ahead=ahead)

    def _ride(self, distance_time: float, entity=None, x0: float = 0.0, stage: str = "ride"):
        """Generator: spend *distance_time* of BELT time, pausing while stopped,
        from *x0*; traced for *entity* when given (D734)."""
        x1 = x0 + distance_time * self.speed
        if entity is not None:
            self._at(entity, stage, x0, x1, distance_time)
        if self.accumulating:
            yield self.env.timeout(distance_time)
            return
        if self._stop_event is None:
            self._stop_event = self.env.event()
        remaining = distance_time
        while remaining > _EPS:
            if self._stopped:
                if entity is not None:
                    self._at(entity, "halted", x1 - remaining * self.speed)
                yield self._resume_event
                if entity is not None:
                    self._at(entity, stage, x1 - remaining * self.speed, x1, remaining)
                continue
            started, gate = self.env.now, self._stop_event
            yield self.env.timeout(remaining) | gate
            remaining -= self.env.now - started

    # -- the ride ------------------------------------------------------------
    # Each stage writes where the part is on the belt as `entity._belt` = (stage, the time
    # it ends or None, when it entered or None), for a state (D429, the D383 pattern).
    def self_process(self, entity):
        spacing_time = self.spacing / self.speed
        entity._belt = ("cell", None, None)
        # A free cell on the belt. The upstream machine is NOT released yet:
        # the part is only off it once it is physically on the belt, which is
        # after the stopped-belt wait and the entry rhythm below (PHASE4 T7).
        cell = yield from self.space.take(entity, accepts=False)
        section, not_before = None, self.env.now
        if self._sections:
            # Room in the first section (D732): parts packed back to the entry stop it.
            first = self._sections[0]
            request = first.pool.request()
            yield request
            due = first.due()
            if due is not None:
                if not due.triggered:
                    yield due
                not_before = due.value + first.lag      # D853: boarding is its start
            section = (request, first.enter(self.env))
        if not self.accumulating and self._stopped:
            yield self._resume_event                # a stopped belt takes nothing on
        # Reserve the entry slot BEFORE sleeping on it: two entities that both
        # read a slot 2 s away would otherwise both take it and ride side by
        # side, which is exactly what spacing forbids.
        slot = max(self._next_entry, self.env.now, not_before)
        self._next_entry = slot + spacing_time
        if slot - self.env.now > _EPS:
            # not `slot`: the float the timeout lands on (a state taken again matches)
            entity._belt = ("slot", self.env.now + (slot - self.env.now), None)
            yield self.env.timeout(slot - self.env.now)
        yield from self._board(entity, cell, section)

    def _board(self, entity, cell, section=None):
        accept(entity)                              # on the belt: the machine is free
        entered_at = self.env.now
        self.entered += 1
        self.occupancy.add(1, entered_at)
        if self._merges:
            self._on.add(entity.id)
        yield from self._carry(entity, cell, entered_at, section=section)

    def _carry(self, entity, cell, entered_at, end=None, section=None, slot=0, start=0.0):
        """The ride to the end (until *end*, put back by a state), then the way off.
        *section*: the cells held in the first section of a belt with stops; *slot* and
        *start*: the first point still ahead and where the part is (a merged part)."""
        length = self._length
        if end is None:
            ride = (length - start) / self.speed
            if self._points:
                section, start = yield from self._through_points(entity, section, cell,
                                                                 entered_at, slot, start)
                if start is None:
                    return                          # diverted off the belt (D851)
                ride = (length - start) / self.speed
            entity._belt = ("ride", self.env.now + ride, entered_at)
            yield from self._ride(ride, entity, start)
        else:
            entity._belt = ("ride", end, entered_at)
            self._at(entity, "ride", length - (end - self.env.now) * self.speed, length,
                     end - self.env.now)
            yield timeout_at(self.env, end)
        ahead = self._prev_clear
        clear = self.env.event()
        self._prev_clear = clear
        yield from self._leave(entity, cell, entered_at, ahead, clear, section=section)

    def _through_points(self, entity, section, cell, entered_at, slot=0, start=0.0):
        """Generator: ride from *start* through every stop, divert and merge from *slot*
        on (D732, D851, D854); returns the cells held in the last section and where the
        last point is, or (None, None) once a divert has taken the part off the belt."""
        gap, at = self.spacing / self.speed, start
        for point in self._points[slot:]:
            if isinstance(point, _Merge):
                section = yield from self._pass_merge(entity, point, at, section)
                at = point.at
                continue
            yield from self._ride((point.at - at) / self.speed, entity, at)
            at = point.at
            ahead, clear = point.clear, self.env.event()
            point.clear = clear
            if self.accumulating:
                if ahead is not None and not ahead.triggered:
                    self._at(entity, "packed", at)      # Timeline puts it behind the one ahead
                    yield ahead
                    catch_up = gap
                else:
                    catch_up = point.left + gap - self.env.now
                if catch_up > _EPS:
                    # held a spacing behind the part ahead until it left: the rest of the way
                    yield from self._ride(catch_up, entity, at - catch_up * self.speed, "move")
                yield from self._reach_section(entity, self._sections[point.slot + 1], at)
            if isinstance(point, _Divert):
                if self._diverts_it(point, entity):
                    yield from self._divert(entity, point, cell, entered_at, section, clear)
                    return None, None
                section = yield from self._room_ahead(entity, point, section)
                point.left = self.env.now
                clear.succeed()
                continue
            stop = point
            if not self.accumulating:
                self._hold(1)
            stop.parts += 1
            stop.busy.set(1.0, self.env.now)
            if stop.resource:
                target = self.ctx.resource_manager.resolve(stop.resource)
                asked = self.env.now
                if target is None or getattr(target, "free", 0) < 1:
                    self._at(entity, "wait", at)
                    stop.blocked.set(1.0, asked)
                yield from acquire(self, entity, stop.resource)
                stop.blocked.set(0.0, self.env.now)
                stop.waits.add(self.env.now - asked)
            duration = self._point_time(stop, entity, "stops")
            self._at(entity, "dwell", at, duration=duration)
            if duration > 0:
                yield self.env.timeout(duration)
            if stop.resource:
                release(self, entity, stop.resource)
            section = yield from self._room_ahead(entity, stop, section, stop.blocked)
            stop.busy.set(0.0, self.env.now)
            stop.left = self.env.now
            clear.succeed()
            if not self.accumulating:
                self._hold(-1)
        return section, at

    def _room_ahead(self, entity, point, section, blocked=None):
        """Generator: a part leaving *point* on an accumulating belt takes a cell in the
        section after it (waiting there for one), and gives back the one it had; returns
        the cells it now holds. *blocked*: the point's blocked share, when it counts."""
        if section is None:
            return None
        nxt = self._sections[point.slot + 1]
        ahead_room = nxt.pool.request()
        if not ahead_room.triggered:
            self._at(entity, "blocked", point.at)
            if blocked is not None:
                blocked.set(1.0, self.env.now)
            yield ahead_room
            if blocked is not None:
                blocked.set(0.0, self.env.now)
        self._leave_section(point.slot, section)
        return ahead_room, nxt.enter(self.env)

    def _diverts_it(self, divert, entity) -> bool:
        """Does *divert* take *entity* (D851)? Its condition, read once as the part's front
        reaches it; in "pass" mode a target with no room lets the part ride on (D852)."""
        value = divert.condition
        if isinstance(value, str):
            try:
                value = evaluate_expression(self, value, entity)
            except (ExpressionError, TypeError, ValueError) as exc:
                raise ExpressionError(
                    f"Block '{self.name}': diverts[{divert.index}].condition: {exc}") from None
        if not value:
            divert.passed += 1
            return False
        if divert.when_full == "pass" and not self._room_at(divert):
            divert.skipped += 1
            return False
        return True

    def _room_at(self, divert) -> bool:
        """The "pass" room check (D852) on the block divert's link goes straight to."""
        target = self.next_process[divert.exit]
        room = getattr(target, "room_now", None)
        if room is not None:
            return room()
        space = getattr(target, "space", None)
        if space is None or space.capacity <= 0:
            raise RuntimeError(
                f"Conveyor '{self.name}': divert {divert.index} passes when full, so its link "
                f"must go straight to a CONVEYOR, a BUFFER or a station with queue_capacity; "
                f"'{getattr(target, 'name', target)}' has no places (E_CONVEYOR_DIVERT).")
        return space.content.level < space.capacity and not space.waiting

    def _divert(self, entity, divert, cell, entered_at, section, clear):
        """Generator: push *entity* off at *divert* (its `time`, holding its cell), hand it
        over on the divert's link with the D78 token -- a full target keeps it at the
        divert -- then take it off the belt (D851, D852)."""
        at = divert.at
        if not self.accumulating:
            self._hold(1)
        divert.busy.set(1.0, self.env.now)
        duration = self._point_time(divert, entity, "diverts")
        self._at(entity, "divert", at, duration=duration)
        if duration > 0:
            yield self.env.timeout(duration)
        asked = self.env.now
        token = arm(self, entity)
        token.entered = entered_at
        self.forward(entity, divert.exit)
        if not token.triggered:
            self._divert_waits += 1
            divert.blocked.set(1.0, asked)
            self._at(entity, "handover", at)
            check_deadlock(self)
            yield token
            self._divert_waits -= 1
            divert.blocked.set(0.0, self.env.now)
        t = self.env.now
        divert.waits.add(t - asked)
        divert.diverted += 1
        divert.busy.set(0.0, t)
        self.exited += 1
        self.occupancy.add(-1, t)
        self.transit.add(t - entered_at)
        self.space.give_back(cell)
        if section is not None:
            self._leave_section(divert.slot, section)
        divert.left = t
        clear.succeed()
        if not self.accumulating:
            self._hold(-1)
        self._on.discard(entity.id)
        self._at(entity, "off", at)

    # -- merges (D854) -------------------------------------------------------
    def wire_merges(self, blocks: dict) -> None:
        """Called once every link is wired, before `blocking.analyse`: each merge's source
        links to a `_MergeEntry` instead of the belt, so its parts join at the merge."""
        for index, (_at, source, _priority) in enumerate(self.merges):
            block = blocks.get(source)
            nexts = getattr(block, "next_process", None) or []
            if not any(target is self for target in nexts):
                raise RuntimeError(f"Conveyor '{self.name}': merge {index} names '{source}', "
                                   f"which does not link to it (E_CONVEYOR_MERGE).")
            block.next_process = [_MergeEntry(self, index) if target is self else target
                                  for target in nexts]

    def _may_join(self, merge) -> bool:
        """A merging part may join now: no main part within a spacing of the point, a
        spacing since the last part passed it, and room in the section after it."""
        now = self.env.now
        if merge.claims or now < merge.left + self.spacing / self.speed - _EPS:
            return False
        section = self._sections[merge.slot + 1]
        if section.pool.count >= section.cells or section.pool.queue:
            return False
        due = section.due()
        return due is None or (due.triggered and due.value + section.lag <= now + _EPS)

    def _join_wakeup(self, merge):
        """The events after which a waiting merging part looks again."""
        now, section = self.env.now, self._sections[merge.slot + 1]
        events = [merge.changed]
        later = merge.left + self.spacing / self.speed - now
        if later > _EPS:
            events.append(self.env.timeout(later))
        due = section.due()
        if due is not None:
            if not due.triggered:
                events.append(due)
            elif due.value + section.lag - now > _EPS:
                events.append(self.env.timeout(due.value + section.lag - now))
        return simpy.AnyOf(self.env, events)

    def _fire(self, merge, name: str) -> None:
        fired = getattr(merge, name)
        setattr(merge, name, self.env.event())
        fired.succeed()

    def _pass_merge(self, entity, merge, at, section):
        """Generator: a main part goes past a merge (D854). It rides to a spacing before
        the point, keeps a spacing behind the part that passed last, gives way to a
        merging part that may join (always, with priority "merge"), then claims the point
        while its front is within a spacing of it, and passes."""
        gap = self.spacing / self.speed
        pre = max(merge.at - self.spacing, at)
        if pre - at > _EPS:
            yield from self._ride((pre - at) / self.speed, entity, at)
        ahead, clear = merge.clear, self.env.event()
        merge.clear = clear
        if ahead is not None and not ahead.triggered:
            self._at(entity, "packed", pre)
            yield ahead
        while True:
            if merge.waiting and (merge.priority == "merge" or self._may_join(merge)):
                self._at(entity, "packed", pre)
                yield merge.joined
                continue
            late = merge.left + gap - (merge.at - pre) / self.speed - self.env.now
            if late <= _EPS:
                break
            self._at(entity, "packed", pre)
            yield self.env.timeout(late)
        merge.claims += 1
        yield from self._ride((merge.at - pre) / self.speed, entity, pre)
        yield from self._reach_section(entity, self._sections[merge.slot + 1], merge.at)
        section = yield from self._room_ahead(entity, merge, section)
        merge.left = self.env.now
        merge.passers.append(entity.id)
        merge.claims -= 1
        clear.succeed()
        self._fire(merge, "changed")
        return section

    def _merge_in(self, entity, index: int):
        """Generator: a part from merge *index*'s source joins the belt at its point
        (D854): a belt cell, the merge's door (one waits there at a time), then the gap
        rule; it is on the belt -- and its source free -- once it joins."""
        merge, ready = self._merges[index], self.env.now
        entity._belt = ("cell", None, None)
        cell = yield from self.space.take(entity, accepts=False)
        door = merge.door.request()
        yield door
        merge.waiting = True
        merge.waiting_share.set(1.0, self.env.now)
        while not self._may_join(merge):
            yield self._join_wakeup(merge)
        nxt = self._sections[merge.slot + 1]
        request = nxt.pool.request()        # free: `_may_join` saw room
        section = (request, nxt.enter(self.env))
        accept(entity)
        t = self.env.now
        ahead = next((pid for pid in reversed(merge.passers) if pid in self._on), -1)
        merge.waiting = False
        merge.waiting_share.set(0.0, t)
        merge.waits.add(t - ready)
        merge.merged += 1
        merge.left = t
        merge.passers.append(entity.id)
        self.entered += 1
        self.occupancy.add(1, t)
        self._on.add(entity.id)
        self._at(entity, "merge", merge.at, ahead=ahead)
        merge.door.release(door)
        self._fire(merge, "joined")
        self._fire(merge, "changed")
        yield from self._carry(entity, cell, t, section=section, slot=merge.slot + 1,
                               start=merge.at)

    def _reach_section(self, entity, section, at: float):
        """Generator, D853: a part reaches the start of *section* (at *at*) no sooner than
        its lag after the part `cells` places ahead of it there left the section's end."""
        due = section.due()
        if due is None:
            return
        if not due.triggered:
            self._at(entity, "packed", at)          # Timeline puts it behind the one ahead
            yield due
        late = due.value + section.lag - self.env.now
        if late > _EPS:
            yield from self._ride(late, entity, at - late * self.speed, "move")

    def _leave_section(self, index: int, section) -> None:
        """A part leaves section *index*'s end: its cell back, its leave time recorded."""
        request, out = section
        part = self._sections[index]
        part.pool.release(request)
        out.succeed(self.env.now)
        # Section *index* starts at point index - 1; a merge there reads its room (D854).
        if index and isinstance(self._points[index - 1], _Merge):
            self._fire(self._points[index - 1], "changed")

    def _point_time(self, point, entity, field: str) -> float:
        """A stop's or a divert's `time` for *entity*, in the model's time unit; a negative
        one is 0."""
        value = point.time
        if isinstance(value, str):
            try:
                value = evaluate_expression(self, value, entity)
            except (ExpressionError, TypeError, ValueError) as exc:
                raise ExpressionError(
                    f"Block '{self.name}': {field}[{point.index}].time: {exc}") from None
        return max(float(value), 0.0)

    def _leave(self, entity, cell, entered_at, ahead, clear, stage=None, until=None,
               section=None):
        """From the end of the ride off the belt. *stage* is where a state put the part back
        (D429): moving up until *until*, at the end stop, or handed over (D430)."""
        length = self._length
        if stage == "move":
            entity._belt = ("move", until, entered_at)
            self._at(entity, "move", length - (until - self.env.now) * self.speed, length,
                     until - self.env.now)
            yield timeout_at(self.env, until)
        elif ahead is not None and not ahead.triggered:
            # Packed against the entity in front: move up as it leaves.
            entity._belt = ("packed", None, entered_at)
            self._at(entity, "packed", length)
            if not self.accumulating:
                self._stop_belt()
            yield ahead
            if not self.accumulating:
                self._start_belt()
            entity._belt = ("move", self.env.now + self.spacing / self.speed, entered_at)
            yield from self._ride(self.spacing / self.speed, entity, length - self.spacing, "move")
        elif self.accumulating and stage in (None, "packed"):
            # #950: the stops' rule at the end (D732) -- a part reaches it no sooner than
            # spacing / speed after the one ahead left, even when that one has left already.
            # A non-accumulating belt keeps the gap on its own: it stood while the end did.
            catch_up = self._end_left + self.spacing / self.speed - self.env.now
            if catch_up > _EPS:
                entity._belt = ("move", self.env.now + catch_up, entered_at)
                yield from self._ride(catch_up, entity, length - catch_up * self.speed, "move")

        if self.exit_resource and stage != "handover":
            blocking = self.ctx.resource_manager.resolve(self.exit_resource)
            # put back at the stop, it found none free (a state refuses any other case)
            free_now = stage != "stop" and blocking is not None \
                and getattr(blocking, "free", 0) >= 1
            entity._belt = ("stop", None, entered_at)
            self._at(entity, "stop", length)
            if not free_now:
                self._end_blocked(True)
            yield from acquire(self, entity, self.exit_resource)
            if not free_now:
                self._end_blocked(False)

        if self.block_after_service:
            # Back-pressure (PHASE6 W3 M6, D115), AFTER the end stop: the part
            # holds its exit unit and its cell until the next belt takes it.
            yield from self._hand_over(entity, entered_at, forward=stage != "handover")
        clear.succeed()
        t = self._end_left = self.env.now
        self.exited += 1
        self.occupancy.add(-1, t)
        self.transit.add(t - entered_at)
        self.space.give_back(cell)
        if section is not None:
            self._leave_section(len(self._sections) - 1, section)
        self._on.discard(entity.id)
        self._at(entity, "off", length)
        if self.ctx.debug:
            log.debug("Entity %s left conveyor '%s' at time %s.", entity.id, self.name, t)
        if not self.block_after_service:
            self.forward(entity, 0)

    def _hand_over(self, entity, entered_at, forward=True):
        """Generator: forward *entity* and keep it on the belt -- its cell,
        its place at the end -- until the downstream belt accepts it (the D78
        token). A DECIDE in between is transparent, so a full CHOSEN branch
        blocks the belt; belts merging on one entry queue FIFO on its cells.
        *forward* False: a state put the part back where it waits (D430)."""
        token = arm(self, entity)
        token.entered = entered_at          # D430: when it entered this belt, for a state
        if forward:
            self.forward(entity, 0)
        if token.triggered:
            return
        self._end_held = True
        self._at(entity, "handover", self._length)
        self._end_blocked(True)
        check_deadlock(self)
        yield token
        self._end_held = False
        self._end_blocked(False)

    def self_run(self, entity):
        self.env.process(self.self_process(entity))

    # -- put back by a state (D429, D430) ------------------------------------
    def restore(self, parts) -> list:
        """The parts a state puts back on this belt, [(entity, {stage, until, entered})]; the
        one its end still hands over has stage "handover". Each takes its cell at once and
        the chain of the parts past the ride is rebuilt in the order they entered (the order
        of an accumulating belt). Returns [(generator, joins a queue)]: the part at the end
        stop asks for a unit, so the caller puts it back with the waiting ones."""
        out = []
        for entity, belt in sorted(parts, key=lambda p: (p[1].get("entered") is None,
                                                         p[1].get("entered") or 0.0)):
            stage, cell = belt["stage"], self.space.claim()
            ahead = clear = None
            if stage in ("packed", "move", "stop", "handover"):
                ahead, clear = self._prev_clear, self.env.event()
                self._prev_clear = clear
            out.append((self._resume(entity, belt, cell, ahead, clear), stage == "stop"))
        return out

    def _resume(self, entity, belt, cell, ahead, clear):
        stage, until, entered = belt["stage"], belt.get("until"), belt.get("entered")
        if stage == "slot":
            entity._belt = ("slot", until, None)
            yield timeout_at(self.env, until)
            yield from self._board(entity, cell)
            return
        self.occupancy.add(1, self.env.now)
        if stage == "ride":
            yield from self._carry(entity, cell, entered, until)
        else:
            yield from self._leave(entity, cell, entered, ahead, clear, stage, until)

    def reset_stats(self, t: float) -> None:
        """Warm-up hook, called from `sim_runner.reset_statistics`."""
        self.entered = 0
        self.exited = 0
        self.occupancy.reset(t)
        self.blocked.reset(t)
        self.transit.reset()
        if self._space is not None:
            self._space.reset_stats(t)
        for point in self._points:
            point.reset_stats(t)

    def get_params_dict(self):
        return {"length": self.length, "speed": self.speed, "spacing": self.spacing,
                "accumulating": self.accumulating, "exit_resource": self.exit_resource,
                "from_node": self.from_node, "to_node": self.to_node,
                "stops": [{"at": at, "time": time, "resource": resource}
                          for at, time, resource in self.stops],
                "diverts": [{"at": at, "condition": condition, "time": time,
                             "when_full": when_full}
                            for at, condition, time, when_full in self.diverts],
                "merges": [{"at": at, "from": source, "priority": priority}
                           for at, source, priority in self.merges]}

    def set_params_from_dict(self, param_dict):
        units = self.ctx.units
        self.length = scaled(float(param_dict.get("length", 0.0) or 0.0),
                             field_factor(param(SPEC, "length"), param_dict, units, SPEC.params))
        self.speed = scaled(float(param_dict.get("speed", 1.0) or 1.0),
                            field_factor(param(SPEC, "speed"), param_dict, units, SPEC.params))
        self.spacing = scaled(float(param_dict.get("spacing", 1.0) or 1.0),
                              field_factor(param(SPEC, "spacing"), param_dict, units, SPEC.params))
        self.accumulating = bool(param_dict.get("accumulating", True))
        self.exit_resource = str(param_dict.get("exit_resource", "") or "").strip()
        self.from_node = str(param_dict.get("from_node", "") or "").strip()
        self.to_node = str(param_dict.get("to_node", "") or "").strip()
        # A stop's `at` is a length like `length` (its unit too); `time` stays an
        # expression, evaluated per part in the model's time unit.
        at_factor = field_factor(param(SPEC, "length"), param_dict, units, SPEC.params)
        self.stops = [(scaled(float(stop.get("at", 0.0)), at_factor), stop.get("time", 0.0) or 0.0,
                       str(stop.get("resource", "") or "").strip())
                      for stop in param_dict.get("stops") or []]
        # A divert's `at` likewise; its condition and time are read per part (D851).
        self.diverts = [(scaled(float(divert.get("at", 0.0)), at_factor),
                         divert.get("condition", ""), divert.get("time", 0.0) or 0.0,
                         str(divert.get("when_full", "wait") or "wait"))
                        for divert in param_dict.get("diverts") or []]
        self.merges = [(scaled(float(merge.get("at", 0.0)), at_factor),
                        str(merge.get("from", "") or "").strip(),
                        str(merge.get("priority", "belt") or "belt"))
                       for merge in param_dict.get("merges") or []]
        self._space = None


SPEC = BlockSpec(
    type_name="CONVEYOR", cls=Conveyor,
    params=(
        ParamSpec("length", float, 0.0,
                  description="Belt length in layout units. 0 takes the network distance between "
                              "`from_node` and `to_node`.", quantity="length", min=0),
        ParamSpec("length_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `length`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("speed", float, 1.0, required=True,
                  description="Belt speed, length units per model time unit.", quantity="speed", min=0),
        ParamSpec("speed_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `speed`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("spacing", float, 1.0, required=True,
                  description="Belt length one entity occupies; capacity is "
                              "floor(length / spacing) and the entry rhythm is spacing / speed.", quantity="length", min=0),
        ParamSpec("spacing_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `spacing`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("accumulating", bool, True,
                  description="True: a blocked entity stops and the others pack behind it. "
                              "False: the whole belt stops while the exit is blocked.", quantity="none"),
        ParamSpec("exit_resource", str, "",
                  description="Resource an entity must take to leave the belt -- the end stop "
                              "that makes accumulation happen. It carries the resource onward, so "
                              "release it downstream (a PROCESS with action \"Delay Release\"). "
                              "Empty: no end stop. Either way, a belt that feeds another belt "
                              "keeps its end part until that belt has room (back-pressure).", quantity="none"),
        ParamSpec("from_node", str, "",
                  description="Path-network node the belt starts at (drawing, and the length when "
                              "`length` is 0).", quantity="none"),
        ParamSpec("to_node", str, "",
                  description="Path-network node the belt ends at.", quantity="none"),
        ParamSpec("stops", list, [],
                  description="Points where each part halts on the belt: {\"at\": <distance from "
                              "the entry, in the unit of `length`, between 0 and the length>, "
                              "\"time\": <number or expression>, \"resource\"?: <seized for the "
                              "stop>}. Parts behind pack against a stop on an accumulating belt; "
                              "a non-accumulating belt stops while a part is at one. Empty: none.",
                  quantity="none", kinds=("table",)),
        ParamSpec("diverts", list, [],
                  description="Points where a part may leave the belt sideways: {\"at\": "
                              "<distance from the entry, in the unit of `length`>, "
                              "\"condition\": <expression, read once as the part reaches it>, "
                              "\"time\"?: <push time, number or expression>, \"when_full\"?: "
                              "\"wait\" (default: the part waits there for room) or \"pass\" "
                              "(it rides on)}. Divert k leaves by outgoing link k + 1; link 0 "
                              "is the belt's end. Empty: none.",
                  quantity="none", kinds=("table",)),
        ParamSpec("merges", list, [],
                  description="Points where parts from another block join the belt: {\"at\": "
                              "<distance from the entry, in the unit of `length`>, \"from\": "
                              "<the block linking to this belt whose parts join there>, "
                              "\"priority\"?: \"belt\" (default: they wait for a gap of two "
                              "spacings) or \"merge\" (parts on the belt make room)}. "
                              "Accumulating belts only. Empty: none.",
                  quantity="none", kinds=("table",)),
    ),
    factory=lambda env, block: Conveyor(env, block),
    family="move",
)
