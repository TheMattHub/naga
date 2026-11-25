"""A one-load belt piece (plan F1, D1439, D1440, #2054, #2055): a load boards, at the start or at
a merge, only when the piece is clear, and the piece is clear once the load on it has left and
its tail is past the piece's end: t_leave + (L - x + occ) / v. The belt calls `wait` before a
load takes its cells, `left` as a load leaves and `free_now` from `room_now`; nothing else on
the belt changes."""
from collections import deque


class BeltOneLoad:
    """The one-load gate of one `Conveyor` (only built when its `one_load` is set)."""

    def __init__(self, belt):
        self.belt = belt
        self.busy = False           # a load has boarded and the piece has not cleared since
        self.last = None            # the source that boarded last: -1 the start, else a merge
        self._waiting: dict = {}    # source -> deque of the events of its loads, FIFO
        self._decide = None         # the pending decision at a piece found idle

    def wait(self, entity, source: int):
        """Generator: *entity* at *source* (-1 the start, else the merge index) waits until the
        piece is its own. It stays in the upstream block meanwhile, as a load waiting for a
        cell does."""
        go = self.belt.env.event()
        self._waiting.setdefault(source, deque()).append(go)
        if not self.busy and self._decide is None:
            # Ruling 4: the grant waits one zero-delay event, so loads whose arrivals are
            # already queued at this instant (the CREATEs at t = 0) are in the same choice; a
            # later same-instant arrival at an idle piece follows event-queue order
            self._decide = self.belt.env.timeout(0)
            self._decide.callbacks.append(self._choose)
        yield go

    def left(self, entity, x: float) -> None:
        """*entity* left the piece at *x* now; one exact event when its tail is past the end
        (a timeout, not a poll, so the clear lands at that time)."""
        belt = self.belt
        delay = (belt._length - x + belt._occ(entity)) / belt.speed
        belt.env.timeout(delay).callbacks.append(self._cleared)

    def free_now(self) -> bool:
        return not self.busy and not self._waiting

    def _cleared(self, _event) -> None:
        self.busy = False
        self._choose(None)

    def _choose(self, _event) -> None:
        self._decide = None
        source = self._next()
        if source is None:
            return
        queue = self._waiting[source]
        go = queue.popleft()
        if not queue:
            del self._waiting[source]
        self.busy, self.last = True, source
        go.succeed()

    def _next(self):
        """Ruling 4: the source that boards next. Belt order is the start, then the merges by
        `at`. After a boarding, the next waiting source in belt order after the last; for the
        very first, a merge with priority "merge" (the first such in belt order), else the
        start, else the first waiting in belt order."""
        if not self._waiting:
            return None
        merges = self.belt._merges
        order = [-1, *sorted(range(len(merges)), key=lambda k: merges[k].at)]
        if self.last is None:
            first = [k for k in order[1:] if merges[k].priority == "merge"]
            order = [*first, *(k for k in order if k not in first)]
        else:
            at = order.index(self.last) + 1
            order = order[at:] + order[:at]
        return next(source for source in order if source in self._waiting)
