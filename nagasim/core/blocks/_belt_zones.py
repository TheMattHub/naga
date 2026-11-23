"""A belt's zones, moved out of conveyor.py (conveyor system A2, task 1): the chain of the parts
past the last point, their stands and restarts, the zone eyes and the dieback time (D1356). The
belt keeps the state (`_zone_*`, `_exit_since`); this object keeps the rules."""
import math

from nagasim.core.blocks._belt_points import _EPS, zone_hold_delay
from nagasim.core.utils import timeout_at


class BeltZones:
    """The zone rules of one `Conveyor`; every method reads and writes the belt's own fields."""

    def __init__(self, belt):
        self.belt = belt

    # A chain entry: [entity, when it is at the end moving freely from now (its stands
    # included), its open stand or None, its last closed stand, that time without the open
    # stand]. A stand is (front, since, restart, t0): it stands with its front at `front` from
    # `since` to `restart`, set by the release at t0.
    def _zone_of(self, front: float) -> int:
        """The zone a front at *front* is in: 1 at the exit, (L - L/count, L]."""
        width = self.belt._length / self.belt._zone_count
        return min(math.floor((self.belt._length - front) / width + _EPS) + 1, self.belt._zone_count)

    def _zone_walk(self, t0: float, since: float, restart0: float):
        """The parts behind the end part, after a release at *t0* of the end part that stood
        since *since* and restarts at *restart0*: (entry, kind, stand). "held": it stands since
        before t0 for an earlier release, and restarts at the later of the two; "stand": it packs
        nose to tail behind a standing part before that one restarts (at t0, or later: it
        packs in after the release), its front the end minus the cells packed ahead (integer
        cells, no drift), and restarts with its own zone's stagger, never before the part
        ahead; "free": it never stands. Read only: `_zone_wave` applies it."""
        length, speed, cell, delay = self.belt._length, self.belt.speed, self.belt.cell_length, self.belt._zone_delay
        chain = iter(self.belt._zone_chain)
        lead = next(chain)[0]
        base, cells = length, self.belt.cells_of(lead)
        packing, p_since, p_restart, p_t0 = True, since, restart0, t0
        for entry in chain:
            stand = entry[2]
            if stand is not None and stand[1] < t0 - _EPS and stand[2] > t0 + _EPS:
                restart, wave = stand[2], stand[3]
                own = t0 + self._zone_of(stand[0]) * delay
                if own > restart:
                    restart, wave = own, t0
                yield entry, "held", (stand[0], stand[1], restart, wave)
                base, cells = stand[0], self.belt.cells_of(entry[0])
                packing, p_since, p_restart, p_t0 = True, stand[1], restart, wave
                continue
            if packing:
                front = base - cells * cell
                # an earlier stand over by t0 is in its free time; a planned one is not
                free = entry[1] if stand is not None and stand[1] < t0 - _EPS else entry[4]
                reached = free - (length - front) / speed
                if reached < p_restart - _EPS:
                    p_since = max(reached, p_since)
                    p_restart = max(p_restart, p_t0 + self._zone_of(front) * delay)
                    yield entry, "stand", (front, p_since, p_restart, p_t0)
                    cells += self.belt.cells_of(entry[0])
                    continue
            packing = False
            yield entry, "free", None

    def _zone_release(self, back: float = 0.0):
        """Generator: the exit frees for the end part, which stood there since `_exit_since`
        (a hold of no time is none), at t0 = now - *back* (*back*: the zone-1 delay a next
        belt already kept it before taking it, R23). It restarts at t0 + release_delay (zone
        1), holding the exit unit meanwhile; every part behind it restarts by its zone
        (`_zone_wave`)."""
        since, now = self.belt._exit_since, self.belt.env.now
        self.belt._exit_since = None
        if since is None or now - since <= _EPS:
            self._zone_moves()
            return
        t0 = now - back
        restart = t0 + self.belt._zone_delay
        entry = self.belt._zone_chain[0]
        entry[2] = (self.belt._length, since, restart, t0)
        self._zone_wave(t0, since, restart)
        self._zone_moves()
        if restart - now > _EPS:
            yield self.belt.env.timeout(restart - now)
        self._zone_close(entry)

    def _zone_wave(self, t0: float, since: float, restart0: float) -> None:
        """Apply `_zone_walk`: an earlier stand over by now is counted, a planned one that no
        longer holds is dropped; with a delay, the time a part can be at the end follows its
        restart (D1356)."""
        for entry, kind, stand in list(self._zone_walk(t0, since, restart0)):
            old = entry[2]
            if kind == "free":
                if old is not None and old[1] >= t0 - _EPS:
                    entry[2], entry[1] = None, entry[4]
                continue
            if kind == "stand" and old is not None:
                if old[1] < t0 - _EPS:
                    self._zone_close(entry)
                else:
                    entry[1] = entry[4]
            self._zone_set(entry, stand)

    def _zone_set(self, entry, stand) -> None:
        """*entry* stands as *stand*; with a delay it is at the end no sooner than its restart
        plus the rest of the way, and a trace stands it at its front then moves it from there."""
        entry[2] = stand
        if not self.belt._zone_delay:
            return
        front, since, restart, _t0 = stand
        entry[1] = max(entry[4], restart + (self.belt._length - front) / self.belt.speed)
        if self.belt.ctx.bus._subscribers:
            now = self.belt.env.now
            for at, stage in ((since, "packed"), (restart, "move")):
                if at - now > _EPS:
                    event = self.belt.env.timeout(at - now)
                    event.callbacks.append(lambda _e, entry=entry, stand=stand, stage=stage:
                                           self._zone_trace(entry, stand, stage))
                elif stage == "packed":
                    self._zone_trace(entry, stand, stage)

    def _zone_trace(self, entry, stand, stage: str) -> None:
        """Trace only: a part stands at its front, or restarts from it, unless its stand
        changed meanwhile."""
        if entry[2] is not stand:
            return
        front = stand[0]
        if stage == "packed":
            self.belt._trace_at(entry[0], "packed", front)
        else:
            self.belt._trace_at(entry[0], "move", front, self.belt._length,
                           (self.belt._length - front) / self.belt.speed)

    def _zone_join(self, entry) -> None:
        """A part starting its last ride packs behind the last part on it if that one stands
        and it gets there before that one restarts (a release already set that stand)."""
        if not self.belt._zone_chain:
            return
        ahead = self.belt._zone_chain[-1]
        stand = ahead[2]
        if stand is None or stand[2] - self.belt.env.now <= _EPS:
            return
        front = stand[0] - self.belt.cells_of(ahead[0]) * self.belt.cell_length
        reached = entry[4] - (self.belt._length - front) / self.belt.speed
        if reached < stand[2] - _EPS:
            restart = max(stand[2], stand[3] + self._zone_of(front) * self.belt._zone_delay)
            self._zone_set(entry, (front, max(reached, stand[1]), restart, stand[3]))

    def _zone_close(self, entry) -> None:
        """A part's stand is over: its dieback counted, kept as its last stand."""
        stand = entry[2]
        if stand is None:
            return
        front, since, restart, _t0 = stand
        zone = self._zone_of(front) - 1
        start = max(since, self.belt._zone_covered[zone], self.belt._zone_since)
        if restart > start:
            self.belt._zone_dieback[zone] += restart - start
        self.belt._zone_covered[zone] = max(self.belt._zone_covered[zone], restart)
        entry[2], entry[3], entry[4] = None, stand, entry[1]

    def _zone_arrive(self, entity, entered_at, ahead):
        """Generator, a belt with a zone delay: the end part's way to the end once it rode
        there freely. It packs behind the part ahead, stands in its zone until its restart,
        and reaches the end no sooner than the part ahead's occupied length after it left."""
        length, speed = self.belt._length, self.belt.speed
        if ahead is not None and not ahead.triggered:
            stand = self.belt._zone_chain[0][2]
            entity._belt = ("packed", None, entered_at, None)
            self.belt._at(entity, "packed", stand[0] if stand is not None else length)
            yield ahead
        entry = self.belt._zone_chain[0]
        stand = entry[2]
        if stand is not None:
            if stand[2] - self.belt.env.now > _EPS:
                entity._belt = ("packed", None, entered_at, None)
                yield timeout_at(self.belt.env, stand[2])
            self._zone_close(entry)
        due = max(self.belt._end_left + self.belt._end_occ / speed, entry[1]) - self.belt.env.now
        if due > _EPS:
            entity._belt = ("move", self.belt.env.now + due, entered_at, None)
            yield from self.belt._ride(due, entity, length - due * speed, "move")

    def _zone_hold(self, entity):
        """Generator, R23: a part a zoned belt hands over after its exit was held stands
        there its zone-1 delay before this belt takes it, this belt's cell and entry slot
        kept; the token tells the zoned belt how long it waited."""
        delay = zone_hold_delay(entity, self.belt.env.now)
        if delay > 0:
            self.belt._next_entry = max(self.belt._next_entry,
                                   self.belt.env.now + delay + self.belt._occ(entity) / self.belt.speed)
            yield self.belt.env.timeout(delay)

    def _zone_moves(self) -> None:
        moved = self.belt._zone_moved
        if moved is not None and not moved.triggered:
            self.belt._zone_moved = None
            moved.succeed()

    def _zone_standing(self, entry):
        """The front at which *entry*'s part stands now, None while it moves: a stand a release
        set, or, while the exit is held, the stand the next release will set (`_zone_walk` read
        at now, as `zone_dieback_time` reads it)."""
        now = self.belt.env.now
        if self.belt._exit_since is not None and self.belt._zone_chain:
            if self.belt._zone_chain[0] is entry:
                return None                 # the end part, at the end
            for other, kind, stand in self._zone_walk(now, self.belt._exit_since, now):
                if other is entry:
                    return stand[0] if kind != "free" and stand[1] <= now + _EPS else None
            return None
        stand = entry[2]
        if stand is not None and stand[1] <= now + _EPS and stand[2] > now + _EPS:
            return stand[0]
        return None

    def _zone_crossing(self, entry, boarded: float, x: float) -> float:
        """When *entry*'s front reaches *x* moving on from where it last stood (a stand begun by
        now), else from the entry at *boarded*; now when it is past *x* already."""
        now = self.belt.env.now
        stands = [s for s in (entry[3], entry[2]) if s is not None and s[1] <= now + _EPS]
        if not stands:
            return boarded + x / self.belt.speed
        front, _since, restart, _t0 = max(stands, key=lambda s: s[0])
        if front > x + _EPS:
            return now
        return restart + (x - front) / self.belt.speed

    def _zone_eye_run(self, entry, boarded: float):
        """Generator, R33: a part on a zoned belt fires each eye it passes as its front gets
        there by the zone chain: its free ride, a stand a release sets, the restart from it.
        It wakes at the time its front would reach the eye and fires only if the front is
        there, else waits for its restart, or for the release that sets it (D675: one event
        per eye and stand, none per cell)."""
        entity = entry[0]
        length = self.belt.item_length(entity)
        size = round(length, 9)
        for eye in self.belt._zone_eyes:
            senses = not eye.ghost and eye.fires_for(length)
            if not senses and not any(e[2] is None or e[2] == size for e in eye.edges):
                continue
            while True:
                if not any(other is entry for other in self.belt._zone_chain):
                    return                      # off the belt
                due = self._zone_crossing(entry, boarded, eye.at)
                if due - self.belt.env.now > _EPS:
                    yield timeout_at(self.belt.env, due)
                    continue
                front = self._zone_standing(entry)
                if front is None or front > eye.at - _EPS:
                    break
                stand = entry[2]
                if self.belt._exit_since is None and stand is not None and stand[0] == front:
                    yield timeout_at(self.belt.env, stand[2])
                else:
                    if self.belt._zone_moved is None:
                        self.belt._zone_moved = self.belt.env.event()
                    yield self.belt._zone_moved
            if eye.edges:
                self.belt._edges(eye, entity)        # D1379
            if senses:
                self.belt._sense(eye, entity)

    @property
    def zone_dieback_time(self) -> list:
        """Per zone, zone 1 at the exit: the time a part's front stood in it (D1356), since
        the warm-up, the stands still open counted to now."""
        out = list(self.belt._zone_dieback)
        if not self.belt._zone_count or not self.belt._zone_chain:
            return out
        now = self.belt.env.now
        open_ = [entry[2][:3] for entry in self.belt._zone_chain if entry[2] is not None]
        if self.belt._exit_since is not None and now - self.belt._exit_since > _EPS:
            open_.append((self.belt._length, self.belt._exit_since, now))
            open_.extend((stand[0], stand[1], now) for _entry, kind, stand
                         in self._zone_walk(now, self.belt._exit_since, now) if kind != "free")
        covered = list(self.belt._zone_covered)
        for front, since, restart in sorted(open_, key=lambda stand: stand[1]):
            zone = self._zone_of(front) - 1
            end = min(restart, now)
            start = max(since, covered[zone], self.belt._zone_since)
            if end > start:
                out[zone] += end - start
            covered[zone] = max(covered[zone], end)
        return out

