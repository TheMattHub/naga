"""A belt's release rules, moved out of conveyor.py (conveyor system A2, task 1): metering by gap
or interval and the slug group (D1357, R25). The belt keeps the state (`_release_*`, `_slug_*`,
`_end_*`); this object keeps the rules."""
import math

from nagasim.core.blocks._belt_points import _EPS
from nagasim.core.utils import timeout_at


class BeltRelease:
    """The release rules of one `Conveyor`; every method reads and writes the belt's own fields."""

    def __init__(self, belt):
        self.belt = belt

    def _release_gate(self, entity, entered_at):
        """Generator: the end part, at the end and free to leave, waits for the release rule.
        Metering: until the part that left last is far enough past the end. A slug: until the
        rule lets the slug go. The parts behind pack against it as against a held exit."""
        now = self.belt.env.now
        self.belt.release_held.set(1.0, now)
        if self.belt._slug_n:
            yield from self._slug_hold(entity, entered_at)
        else:
            if self.belt._release_kind == "gap":
                # from the last part's tail (its item length), not its occupied length
                due = self.belt._end_left + (self.belt._end_len + self.belt._release_len) / self.belt.speed
            else:
                # lead to lead, never nearer than the last part's occupied length
                due = self.belt._end_left + max(self.belt._release_len, self.belt._end_occ) / self.belt.speed
            if due - now > _EPS:
                self._release_stand(entity, entered_at)
                yield timeout_at(self.belt.env, due)
        self.belt.release_waits.add(self.belt.env.now - now)
        self.belt.release_held.set(0.0, self.belt.env.now)

    def _release_stand(self, entity, entered_at) -> None:
        entity._belt = ("release", None, entered_at, None)
        self.belt._at(entity, "release", self.belt._length)

    def _slug_changes(self) -> None:
        changed = self.belt._slug_changed
        if changed is not None and not changed.triggered:
            changed.succeed()

    def _slug_join(self, entity, at=None) -> None:
        """A part gets on a slug belt (R25): at the entry, at the back of the group; at a merge
        at *at*, in front of the first part that has not passed that point. A part has passed it
        once it has started a ride from there or beyond (a ride never spans a point), so the
        group keeps belt order whichever merge a part came from."""
        entry = [entity, self.belt._occ(entity), []]
        self.belt._slug_of[entity.id] = entry
        group = self.belt._slug_group
        if at is None:
            group.append(entry)
        else:
            index = next((i for i, (_part, _occ, rides) in enumerate(group)
                          if not any(x0 >= at - _EPS for x0, _when, _x1 in rides)), len(group))
            group.insert(index, entry)
        self._slug_changes()

    def _slug_ride(self, entity, x0: float, x1: float) -> None:
        """A part of a slug belt starts a ride from *x0* to *x1*: from now on its free
        arrival anywhere on it is known."""
        entry = self.belt._slug_of.get(entity.id)
        if entry is not None:
            entry[2].append((x0, self.belt.env.now, x1))
            self._slug_changes()

    def _slug_leave(self, entity) -> None:
        """A part leaves a slug belt sideways: out of the group, or of the slug let go."""
        self.belt._slug_go.discard(entity.id)
        entry = self.belt._slug_of.pop(entity.id, None)
        if entry is not None and entry in self.belt._slug_group:
            self.belt._slug_group.remove(entry)
            self._slug_changes()

    def _slug_queued(self) -> list:
        """When each part of the group stands queued (R8, R25): the lead from its stand at
        the end; each other at its accumulated place p (the end less the occupied lengths of
        the parts ahead in the group), at its free arrival there on the ride that reaches p
        (from, when, to with from <= p <= to: when + (p - from) / speed), never before the part
        ahead. A part whose ride to p has not started is not queued yet (inf): it starts that
        ride before it could be, and the start wakes the lead. So a part counts wherever it
        stands, behind a point too. A point is on a part's way to p only when p is past it: a
        part whose place is behind a stop is queued without the stop's dwell (it dwells there
        after the release), one whose place is past the stop once it has dwelt."""
        out, ahead, prev = [], 0.0, self.belt._slug_lead
        length = self.belt._length
        for index, (_entity, occ, rides) in enumerate(self.belt._slug_group):
            if index:
                place = max(length - ahead, 0.0)
                free = next((when + (place - x0) / self.belt.speed for x0, when, x1 in rides
                             if x0 - _EPS <= place <= x1 + _EPS), math.inf)
                prev = max(free, prev)
            out.append(prev)
            ahead += occ
        return out

    def _slug_hold(self, entity, entered_at):
        """Generator: the end part of a slug belt. A part of a slug already let go passes. A
        lead stands until `count` parts are queued (`_slug_queued`) or `timeout` has passed
        since it stood, then lets go the parts queued by then, at most `count`; they leave
        nose to tail by the end's and the points' catch-up rules."""
        if entity.id in self.belt._slug_go:
            self.belt._slug_go.discard(entity.id)
            del self.belt._slug_of[entity.id]
            return
        group = self.belt._slug_group
        lead = self.belt._slug_of[entity.id]
        if group[0] is not lead:
            raise RuntimeError(f"Conveyor '{self.belt.name}': the slug lost the belt's order.")
        self.belt._slug_lead = self.belt.env.now
        stood = False
        while True:
            queued = self._slug_queued()
            due = self.belt._slug_lead + self.belt._slug_timeout
            if len(queued) >= self.belt._slug_n:
                due = min(due, queued[self.belt._slug_n - 1])
            if due - self.belt.env.now <= _EPS:
                break
            if not stood:
                self._release_stand(entity, entered_at)
                stood = True
            self.belt._slug_changed = self.belt.env.event()
            yield self.belt._slug_changed | timeout_at(self.belt.env, due)
        self.belt._slug_changed = None
        now = self.belt.env.now
        count = min(sum(1 for at in queued if at <= now + _EPS), self.belt._slug_n)
        count = max(count, 1)       # the lead itself
        self.belt._slug_go.update(part.id for part, _occ, _rides in group[1:count])
        del self.belt._slug_of[entity.id]
        del group[:count]
        self.belt._slug_lead = None
