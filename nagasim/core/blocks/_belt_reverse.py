"""A reversible CONVEYOR's ride (conveyor system A9, D1368).

A reversible belt has no stops, diverts, merges, eyes, zones or release rules (refused at
load), so its parts only ride, pack at the exit they head to, and leave by it. Its own ride
replaces the plain belt's per-part chain because a reversal needs every part's place.

Each riding part keeps an anchor: its remaining distance to the exit it heads to, `r0`, at
belt travel `s0` (the belt's odometer, which stands while a non-accumulating belt stands).
Its free remaining distance is r0 - (s - s0). The parts ride in one file, the one nearest the
exit first, and a part never comes nearer the part ahead than their spacing, so its true
remaining distance is max(free, the part ahead's + spacing): exact for parts at one speed
that only ever stand behind the part at the exit. The spacing of two neighbours is the
occupied length of the one farther from the entry (an item's body lies back from its front
toward the entry), so a queue keeps its spacing when it turns (nose to tail from the new
exit).

Events: only the part nearest the exit has a timer, the time it reaches the exit; a
reversal or a part leaving the exit plans it again. A reversal sets every part's remaining
distance to the distance it has travelled, from that instant, once per part (D675: no
stepping, no event per cell).

A part that has reached an exit belongs to it: it stands there (waiting for the exit stop or
for the next block to take it) and leaves by it even if the belt turns meanwhile, because the
next block already has it. While a part stands at an exit the parts heading there pack
behind it; a non-accumulating belt stands while a part held at the exit it runs to waits.

A chain (D1392): reversible belts linked end to start turn as one. At a joint a part crosses
in the instant it arrives, off one piece and on the next (`_hand_across`), riding the next
piece from its far end; with no room there it stands at the joint, uncommitted, so a turn
takes it back like any riding part. A joint is no exit: no hand-over token, no exit stop.
"""
from collections import deque

from nagasim.core.blocking import accept, arm, check_deadlock
from nagasim.core.blocks._belt_points import write_exposed
from nagasim.core.seizing import acquire
from nagasim.core.stats import TimeWeighted

__all__ = ["ReverseRide"]

_EPS = 1e-9


class _Item:
    # `dist`: the distance ridden before the last reversal; `rs`: the remaining distance then
    # (A12, R64: what it has ridden is dist + rs - its true remaining distance)
    __slots__ = ("entity", "cell", "entered", "occ", "r0", "s0", "arrived", "dist", "rs",
                 "stand")

    def __init__(self, entity, cell, entered, occ, r0, s0, arrived):
        self.entity, self.cell, self.entered, self.occ = entity, cell, entered, occ
        self.r0, self.s0, self.arrived = r0, s0, arrived
        self.dist, self.rs = 0.0, r0
        self.stand = None                   # D1392: the wait at a joint in force


class ReverseRide:
    """The parts on one reversible belt and its direction."""

    def __init__(self, belt):
        self.belt, self.env = belt, belt.env
        self.length, self.speed = belt._length, belt.speed
        self.forward = True
        self.chain: list = []                       # riding parts, the nearest its exit first
        self.standing = {True: None, False: None}   # the part at each exit, leaving by it
        self._halts = {True: 0, False: 0}           # parts held at each exit
        self.held_since = {True: None, False: None}  # since when (R45)
        self.waits = 0                              # parts waiting for the next block
        self._start_waits = 0                       # of them, at the start (R45)
        self.joint_waits = {True: 0, False: 0}      # D1392: parts waiting at each joint
        self.running = True
        self._s, self._s_at = 0.0, self.env.now     # odometer at the last run change
        self._epoch = 0                             # the lead's timer in force
        self._entering = deque()                    # parts waiting to board (no cells, A2-R5)
        self.changed = self.env.event()             # an entry may look again
        self.turned = self.env.event()              # the next reversal
        self.turns = 0                              # since the warm-up (A12)
        self.turned_at = None
        self.backward = TimeWeighted(self.env.now)  # 1 while it runs backward (A12)

    def _neighbour(self, at_end: bool):
        """D1392: the ride of the chain piece beyond exit *at_end*, None at a chain end."""
        belt = self.belt.chain_next if at_end else self.belt.chain_prev
        if belt is None:
            return None
        belt.space                          # noqa: B018 -- builds its ride
        return belt._rev

    # -- geometry ---------------------------------------------------------------------
    def _odo(self) -> float:
        if not self.running:
            return self._s
        return self._s + (self.env.now - self._s_at) * self.speed

    @staticmethod
    def _free(item, s: float) -> float:
        return item.r0 - (s - item.s0)

    def _gap(self, ahead, behind) -> float:
        """Spacing of two neighbours: the occupied length of the one farther from the entry."""
        return ahead.occ if self.forward else behind.occ

    def _positions(self) -> list:
        """The true remaining distance of each riding part, nearest its exit first."""
        s, out = self._odo(), []
        prev, r_prev = self.standing[self.forward], 0.0
        for item in self.chain:
            floor = r_prev + self._gap(prev, item) if prev is not None else 0.0
            r = max(self._free(item, s), floor)
            out.append(r)
            prev, r_prev = item, r
        return out

    def _trace(self, stage: str = "ride", lead_only: bool = False) -> None:
        """Trace only (D734, R46): each riding part from its true place toward the exit it
        heads to ("ride"), or standing there ("halted"); playback keeps the rest packed."""
        belt = self.belt
        if not belt.ctx.bus._subscribers:
            return
        length, forward = self.length, self.forward
        for item, r in zip(self.chain[:1] if lead_only else self.chain, self._positions()):
            x = length - r if forward else r
            if stage == "halted":
                belt._at(item.entity, "halted", x)
            else:
                belt._at(item.entity, "ride", x, length if forward else 0.0, r / self.speed)

    # -- events -----------------------------------------------------------------------
    def _changes(self) -> None:
        old, self.changed = self.changed, self.env.event()
        old.succeed()

    def _plan(self) -> None:
        """The timer of the part nearest the exit; none while a part stands there (it is
        planned when that one leaves) or while the belt stands."""
        self._epoch += 1
        if not self.chain or not self.running or self.standing[self.forward] is not None:
            return
        epoch = self._epoch
        due = self.env.timeout(max(self._free(self.chain[0], self._odo()), 0.0) / self.speed)
        due.callbacks.append(lambda _event: self._arrive(epoch))

    def _arrive(self, epoch: int) -> None:
        if epoch != self._epoch:
            return                          # a reversal or a stand planned it again
        lead = self.chain.pop(0)
        if self.chain:
            # the next part's place now, from its own anchor and the lead at the exit
            s, nxt = self._odo(), self.chain[0]
            nxt.r0, nxt.s0 = max(self._free(nxt, s), self._gap(lead, nxt)), s
        self.standing[self.forward] = lead
        self._trace(lead_only=True)
        if self._neighbour(self.forward) is not None:
            self._cross(lead, self.forward)
        else:
            lead.arrived.succeed((self, lead, self.forward))

    def _departed(self, item, at_end: bool) -> None:
        """*item* left by the exit *at_end* (True: the end): the next part heading there
        restarts from where it stands."""
        if at_end == self.forward and self.chain:
            s, lead = self._odo(), self.chain[0]
            lead.r0, lead.s0 = max(self._free(lead, s), self._gap(item, lead)), s
        self.standing[at_end] = None
        if at_end == self.forward and self.running:
            self._trace(lead_only=True)
        if at_end == self.forward:
            self._plan()
        self._changes()                     # the entry is clear again, or a cell is free

    def _set_running(self, running: bool) -> None:
        if running == self.running:
            return
        self._s, self._s_at = self._odo(), self.env.now
        self.running = running
        if self.belt.expose:
            write_exposed(self.belt, "running", 1 if running else 0)    # R67, D1378
        self._plan()
        self._trace("ride" if running else "halted")
        self._changes()

    def _hold(self, at_end: bool, on: bool) -> None:
        """A part at exit *at_end* starts (*on*) or stops waiting there."""
        self._count_hold(at_end, on)
        if not self.belt.accumulating:
            self._set_running(not self._halts[self.forward])

    def _count_hold(self, at_end: bool, on: bool) -> None:
        self._halts[at_end] += 1 if on else -1
        self.held_since[at_end] = (self.env.now if on and self.held_since[at_end] is None
                                   else None if not self._halts[at_end]
                                   else self.held_since[at_end])
        self.belt.blocked.set(1.0 if self._halts[True] or self._halts[False] else 0.0,
                              self.env.now)

    # -- direction --------------------------------------------------------------------
    def turn(self, forward: bool) -> None:
        """Run the belt *forward* (True) or backward: each riding part's remaining distance
        becomes the distance it has travelled, from now, at the belt speed."""
        if forward == self.forward:
            return
        old = self.forward
        joint = self.standing[old] if self._neighbour(old) is not None else None
        s, length = self._odo(), self.length
        for item, r in zip(self.chain, self._positions()):
            item.r0, item.s0 = length - r, s
            item.dist += item.rs - r
            item.rs = length - r
        self.chain.reverse()
        if joint is not None:
            # D1392: a part waiting at a joint is no exit's: it rides back, the farthest out
            self.standing[old], joint.stand = None, None
            joint.r0, joint.s0 = length, s
            joint.dist += joint.rs
            joint.rs = length
            self.chain.append(joint)
            self.joint_waits[old] -= 1
            self._count_hold(old, False)
        self.forward = forward
        self.turns += 1
        self.turned_at = self.env.now
        self.backward.set(0.0 if forward else 1.0, self.env.now)
        belt = self.belt
        if belt.expose:
            write_exposed(belt, "forward", 1 if forward else 0)         # D1378
        if belt.ctx.bus._subscribers:
            # R46: playback turns the belt's rows round, then reads each part's new ride
            on = [*self.chain, *(item for item in self.standing.values() if item is not None)]
            if on:
                belt.emit("belt_stage", entity=on[0].entity, state="turn", x0=0.0, x1=0.0,
                          duration=0.0, value=1.0 if forward else 0.0, ahead=-1)
        if not belt.accumulating:
            self._set_running(not self._halts[forward])
        self._plan()
        self._trace("ride" if self.running else "halted")
        old, self.turned = self.turned, self.env.event()
        old.succeed()
        self._changes()

    def stood_now(self) -> float:
        """A12 (R64): sum over the parts on the belt of occupied length x time stood so far:
        their time on it less the distance ridden, from the true places (exact, see above)."""
        now, speed, total = self.env.now, self.speed, 0.0
        riding = list(zip(self.chain, self._positions()))
        riding += [(item, 0.0) for item in self.standing.values() if item is not None]
        for item, r in riding:
            stood = (now - item.entered) - (item.dist + item.rs - r) / speed
            if stood > _EPS:
                total += item.occ * stood
        return total

    def hold_at_end(self):
        """R45: (since when it runs backward, since when a part waits at its start) while
        it runs backward with a part held at the start it runs to, else None. D1392: a chain's
        running start is its first piece's; a later piece's start is a joint."""
        since = self.held_since[False]
        if self.forward or since is None or self.belt.chain_prev is not None:
            return None
        return self.turned_at, since

    def start_held(self) -> bool:
        """R45: a part at the start waits for the next block to take it; nothing boards until
        it leaves, whichever way the belt runs (`may_enter`), so no later reversal opens the
        entry: what the live deadlock check reads."""
        return self._start_waits > 0

    # -- entry ------------------------------------------------------------------------
    def may_enter(self, entity=None) -> bool:
        """A part may board now: the belt runs forward, nothing stands at the entry, and the
        last part on is at least its occupied length in."""
        if not self.forward or not self.running or self.standing[False] is not None \
                or self._entering:
            return False
        return self._entry_late() <= _EPS

    def _entry_late(self, occ: float | None = None) -> float:
        """Time until the last part on is its occupied length past the entry. Its free place
        is enough: a part held back by the queue stands at least that far in, since the
        queue's cells fit on the belt. D1392: boarding at the end while it runs backward, the
        gap is the boarding part's own *occ* (the one farther from the start)."""
        if not self.chain:
            return 0.0
        tail = self.chain[-1]
        x = self.length - self._free(tail, self._odo())
        gap = tail.occ if self.forward or occ is None else occ
        return (gap - x) / self.speed

    def board(self, entity):
        """Generator: a part gets on, first come first served, once the belt runs forward,
        its cells are free and the entry rhythm allows; then rides to an exit and leaves by
        it. A2-R5: it takes its cells only as it boards, so a part waiting while the belt
        runs backward leaves them to a part crossing a joint onto this piece's end."""
        belt, env, space = self.belt, self.env, self.belt.space
        check = getattr(space, "check_fits", None)
        if check is not None:
            check(entity)
        yield from belt._belt_zones._zone_hold(entity)
        self._entering.append(entity)
        entity._belt = ("entry", None, None, None)
        while True:
            if self._entering[0] is not entity or not self.forward or not self.running \
                    or self.standing[False] is not None:
                yield self.changed
                continue
            if not space.room_for(entity):
                check_deadlock(belt)                # it waits for cells: a loop may close
                yield self.changed
                continue
            late = self._entry_late()
            if late <= _EPS:
                break
            yield env.timeout(late) | self.changed
        self._entering.popleft()
        cell = space.take_now(entity)
        belt._check_entry(entity)
        accept(entity)                              # on the belt: the machine is free
        now = env.now
        belt.entered += 1
        belt.occupancy.add(1, now)
        item = _Item(entity, cell, now, belt._occ(entity), self.length, self._odo(),
                     env.event())
        self.chain.append(item)
        if len(self.chain) == 1:
            self._plan()
        entity._belt = ("ride", None, now, None)
        belt._at(entity, "ride", 0.0, self.length, self.length / self.speed)
        self._changes()                             # the next part waiting looks again
        ride, item, at_end = yield item.arrived    # D1392: maybe on a later chain piece
        yield from ride._leave(item, at_end)

    # -- joints (D1392) ---------------------------------------------------------------
    def joint_held(self, successor: str = "") -> bool:
        """A part waits at a joint for the chain piece *successor* (any, when not named): what
        the deadlock checks read, in the running direction."""
        for at_end in (True, False):
            if self.joint_waits[at_end]:
                after = self.belt.chain_next if at_end else self.belt.chain_prev
                if not successor or after.name == successor:
                    return True
        return False

    def join_late(self, entity, at_start: bool):
        """A part crossing a joint onto this piece at its start (*at_start*) or its end: the
        time until it may board, None while it may not (the piece runs the other way or
        stands, a part stands at that end, a part waits to board there, no cells)."""
        if self.forward != at_start or not self.running \
                or self.standing[not at_start] is not None \
                or (at_start and self._entering) or not self.belt.space.room_for(entity):
            return None
        return max(self._entry_late(self.belt._occ(entity)), 0.0)

    def _cross(self, item, at_end: bool) -> None:
        """*item* has reached the joint *at_end*: on the next piece now, or it waits there."""
        after = self._neighbour(at_end)
        late = after.join_late(item.entity, at_end)
        if late is not None and late <= _EPS:
            self._hand_across(item, at_end, after)
            return
        item.stand = stand = object()
        self.joint_waits[at_end] += 1
        self._hold(at_end, True)
        self.belt._at(item.entity, "handover", self.length if at_end else 0.0)
        check_deadlock(self.belt)
        self.env.process(self._wait_cross(item, at_end, stand))

    def _wait_cross(self, item, at_end: bool, stand):
        """Generator: *item* waits at the joint *at_end* until the next piece takes it, or a
        turn takes it back (`turn`)."""
        after, env = self._neighbour(at_end), self.env
        while item.stand is stand:
            late = after.join_late(item.entity, at_end)
            if late is not None and late <= _EPS:
                item.stand = None
                self.joint_waits[at_end] -= 1
                self._hold(at_end, False)
                self._hand_across(item, at_end, after)
                return
            wake = [after.changed, self.turned]
            if late is not None:
                wake.append(env.timeout(late))
            yield env.any_of(wake)

    def _hand_across(self, item, at_end: bool, after) -> None:
        """*item* leaves this piece by the joint *at_end* and gets on *after* in this instant."""
        belt, entity, now = self.belt, item.entity, self.env.now
        x = self.length if at_end else 0.0
        belt.exited += 1
        belt.occupancy.add(-1, now)
        belt.transit.add(now - item.entered)
        belt._left_reversible(entity, item.entered, item.dist + item.rs)
        belt.space.give_back(item.cell)
        belt._at(entity, "off", x)
        self._departed(item, at_end)
        if belt._reach or belt._offs:
            belt._reached(entity, off=not at_end)   # R5 at the end; R26 off by the start
        after._join(item, at_end)

    def _join(self, item, at_start: bool) -> None:
        """*item* of the piece before (or after) gets on this one at its start (or its end),
        keeping the event its generator waits on."""
        belt, entity, now = self.belt, item.entity, self.env.now
        cell = belt.space.take_now(entity)
        belt._check_entry(entity)
        belt.entered += 1
        belt.occupancy.add(1, now)
        new = _Item(entity, cell, now, belt._occ(entity), self.length, self._odo(),
                    item.arrived)
        self.chain.append(new)
        if len(self.chain) == 1:
            self._plan()
        entity._belt = ("ride", None, now, None)
        if not at_start and belt.ctx.bus._subscribers:
            # R46: playback draws an empty piece forward; this one runs backward
            belt.emit("belt_stage", entity=entity, state="turn", x0=0.0, x1=0.0,
                      duration=0.0, value=0.0, ahead=-1)
        x0 = 0.0 if at_start else self.length
        belt._at(entity, "ride", x0, self.length - x0, self.length / self.speed)
        self._changes()

    # -- exit -------------------------------------------------------------------------
    def _leave(self, item, at_end: bool):
        """Generator: *item* is at exit *at_end*: the exit stop, then the hand-over to the
        exit's next block (held while a next belt has no room), then off the belt."""
        belt, entity, env = self.belt, item.entity, self.env
        x = self.length if at_end else 0.0
        slot = belt.exit_slots[at_end]
        if belt.exit_resource:
            target = belt.ctx.resource_manager.resolve(belt.exit_resource)
            free_now = target is not None and getattr(target, "free", 0) >= 1
            entity._belt = ("stop", None, item.entered, None)
            belt._at(entity, "stop", x)
            if not free_now:
                self._hold(at_end, True)
            yield from acquire(belt, entity, belt.exit_resource)
            if not free_now:
                self._hold(at_end, False)
        if slot >= len(belt.next_process or []):
            self._hold(at_end, True)
            yield from belt.handed_to_nothing(entity, x)
        hands = belt.exit_blocks[at_end]
        if hands:
            token = arm(belt, entity)
            token.entered = item.entered
            belt.forward(entity, slot)
            if not token.triggered:
                self.waits += 1
                if not at_end:
                    self._start_waits += 1
                self._hold(at_end, True)
                belt._at(entity, "handover", x)
                check_deadlock(belt)
                yield token
                self.waits -= 1
                if not at_end:
                    self._start_waits -= 1
                self._hold(at_end, False)
        now = env.now
        belt.exited += 1
        belt.occupancy.add(-1, now)
        belt.transit.add(now - item.entered)
        belt._left_reversible(entity, item.entered, item.dist + item.rs)
        belt.space.give_back(item.cell)
        belt._at(entity, "off", x)
        self._departed(item, at_end)
        if belt._reach or belt._offs:
            belt._reached(entity, off=not at_end)   # R5 at the end; R26 off by the start
        if not hands:
            belt.forward(entity, slot)
