"""The belt's points and sections, moved out of conveyor.py (R37): stops, diverts, merges, eyes,
the left-part record, the section and cell-space classes and `zone_hold_delay`."""
import math
from collections import deque
from typing import NamedTuple

import simpy

from nagasim.core.blocking import _RESERVED, _TOKEN, Space, accept, check_deadlock, placed
from nagasim.core.stats import Tally, TimeWeighted


_EPS = 1e-9
# R20: a typed belt's cell when `cell` is unset, in metres whatever the model's length unit.
_DEFAULT_CELL = 0.01


class DivertSpec(NamedTuple):
    """One divert as declared (D851, D1343), `at` in the model's base length unit; `rule` and
    `rule_group` route it (conveyor system A7): a RouteRule dict on the group's first divert."""
    at: float
    condition: object
    time: object
    when_full: str = "wait"
    intercept: bool = False
    on_late: str = "wait"
    side: str = ""
    rule: dict | None = None
    rule_group: str = ""
    angle: object = 90          # D1388: floor geometry only, kept so get_params_dict keeps it


def _divert_params(d: DivertSpec) -> dict:
    """A divert's params as written back: a robot pick has no push time and no when_full, as
    validation says (#2003); `side`, `angle`, `rule` and `rule_group` only when set, so old belts' params
    stay as they were."""
    out = {"at": d.at, "condition": d.condition, "intercept": True, "on_late": d.on_late} \
        if d.intercept else {"at": d.at, "condition": d.condition, "time": d.time,
                             "when_full": d.when_full}
    if d.side:
        out["side"] = d.side
    if d.angle != 90:
        out["angle"] = d.angle
    if (d.rule is not None or d.rule_group) and not d.condition:
        del out["condition"]                # routed by its rule, not a condition (A7)
    if d.rule is not None:
        out["rule"] = dict(d.rule)
    if d.rule_group:
        out["rule_group"] = d.rule_group
    return out


class _Point:
    """What every stop, divert and merge keeps (D732, D851, D854): its place in `_points`
    (`slot`), the chain of the parts through it, and its waits."""

    code = ""                           # the problem code of a bad point

    def __init__(self, index: int, at: float):
        self.index, self.at = index, at
        self.slot = 0
        self.clear = None               # fires when the last part to reach it leaves it
        self.left = -math.inf           # when that part left
        self.occ = 0.0                  # the belt length it took: the gap the next keeps
        self.waits = Tally()
        # D1379: an exposed zoned belt's eye edges at this eye, (eye index, covers, item length
        # or None for every item), applied as a part's front gets here (`_zone_eye_run`)
        self.edges: list = []

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

    def __init__(self, env, index: int, at: float, condition, time, when_full: str,
                 intercept: bool = False, on_late: str = "wait", side: str = ""):
        super().__init__(index, at)
        self.side = side        # "left"/"right" of the heading, "" = unset (floor plan only)
        self.condition, self.time, self.when_full = condition, time, when_full
        # D1343: a robot on the divert's link picks the part on the move; `on_late` is read
        # at the call when that robot is busy.
        self.intercept, self.on_late = intercept, on_late
        self.exit = index + 1
        # A7 (D1359): a routed divert's group, belt order, its first divert `head`; the head
        # owns the RoutePoint, chosen there as a part's front reaches it. None: not routed.
        self.head = self.members = self.route = None
        self.diverted = self.passed = self.skipped = 0
        self.busy = TimeWeighted(env.now)       # a part pushes or waits there
        self.blocked = TimeWeighted(env.now)    # it waits for its target

    def reset_stats(self, t: float) -> None:
        super().reset_stats(t)
        self.diverted = self.passed = self.skipped = 0
        self.busy.reset(t)
        self.blocked.reset(t)


class _MeetPoint(_Point):
    """Where a robot called at an intercept divert meets its part (D1343): `at` the meeting
    point, from the robot's plan. A part the divert took leaves the belt here once both are
    there; any other passes it as a point that takes nothing."""

    code = "E_ROBOT_INTERCEPT"

    def __init__(self, divert, at: float):
        super().__init__(divert.index, at)
        self.divert = divert


class _Eye(_Point):
    """One photo-eye's run-time state (D1347): a point that takes nothing. `sensor` is the
    declared place; `at` is where the part's front is when it fires (the sensor, plus the
    part length for a "back" edge). `rows` are its compiled ASSIGN rows."""

    code = "E_CONVEYOR_EYE"

    def __init__(self, index: int, sensor: float, at: float, edge: str, rows: list,
                 signal: str, offset: float = 0.0, ghost: bool = False):
        super().__init__(index, at)
        self.sensor, self.edge, self.rows, self.signal = sensor, edge, rows, signal
        # D1379: an eye an exposed zoned belt adds where an eye's other edge falls; it only
        # carries `edges` and never fires (no count, trace, signal, assign or handshake)
        self.ghost = ghost
        # A back eye has one point per item length that can occur (mixed sizes), each at
        # sensor + that length; an item fires only the point of exactly its own length. The
        # belt's `_eyes[index]` keeps the count and the lengths whose point was left out.
        self.offset = offset
        self.dropped: set = set()
        self.fired = 0

    def fires_for(self, length: float) -> bool:
        """A front eye fires for every item; a back eye's point for an item of its length."""
        return self.edge != "back" or round(length, 9) == self.offset

    def reset_stats(self, t: float) -> None:
        super().reset_stats(t)
        self.fired = 0


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
        # D1355, typed belts: the waiting merging part's occupied length, and the main parts
        # riding to their claim place, first first: [when it is there, its own length]
        self.joiner_occ = 0.0
        # #2014: False, or (when the part it lands behind is not yet past the point) the time
        # it will be, None for not before a stage change
        self.ahead_due = False
        self.behind = False         # #2016: the main part behind is in the joiner's place
        self.riders = deque()
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

    def has_eye_for(self, entity) -> bool:
        """R36: a part joining here passes only the eyes past the merge's point."""
        belt = self._belt
        belt.space                              # the points are built with the cells
        at = belt._merges[self._index].at
        length = belt.item_length(entity)
        return any(eye.at > at and eye.fires_for(length) for eye in belt._eye_points)


class _Left:
    """A section entrant that left before a state (D922): what `_Section.due` readers ask of
    its leave event, with nothing scheduled."""

    triggered = True

    def __init__(self, value: float):
        self.value = value


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

    def due_lag(self):
        """(the leave event a part reaching this section's start waits for, the lag after
        it), or (None, 0.0)."""
        if self.lag and len(self.entrants) == self.cells:
            return self.entrants[0], self.lag
        return None, 0.0

    def request(self, cells: int = 1):
        """A place in the section, one per part."""
        return self.pool.request()

    def full(self) -> bool:
        return self.pool.count >= self.cells

    def enter(self, env, cells: int = 1):
        """A part is in: its leave event, to succeed with the time it leaves the end."""
        out = env.event()
        self.entrants.append(out)
        return out


class _CellGate:
    """The room of a typed belt's section (D1355), first come first served: an item gets in
    while the items in it take fewer than `cells` cells (its front then fits past the start),
    and holds all its own cells, which may be more (its back hangs over the point)."""

    def __init__(self, env, cells: int):
        self.env, self.cells, self.used = env, cells, 0
        self.queue = deque()

    def request(self, cells: int):
        request = self.env.event()
        request.cells = cells
        self.queue.append(request)
        self._grant()
        return request

    def release(self, request) -> None:
        self.used -= request.cells
        self._grant()

    def _grant(self) -> None:
        while self.queue and self.used < self.cells:
            request = self.queue.popleft()
            self.used += request.cells
            request.succeed()


class _CellSection:
    """The belt between two points of a typed accumulating belt (D1355): `_Section` with
    items of their own lengths, in cells.

    A part reaches the start once the part ahead's front is its own occupied length past
    it. Packed from the end, that holds once enough entrants have left the end that the
    rest take no more than the section's length; `entrants` keeps only the entrants that
    can still decide it (oldest first), so the first is the one to wait for, and the rest
    then move on for the lag. Integer cells keep the sums exact over a long run."""

    def __init__(self, env, gap: float, cell: float, speed: float):
        self.gap, self.cell, self.speed = gap, cell, speed
        self.cells = max(math.ceil(gap / cell - _EPS), 1)
        self.pool = _CellGate(env, self.cells)
        self.entrants = deque()         # [leave event, cells], oldest first
        self._total = 0                 # their cells

    def due_lag(self):
        over = self._total * self.cell - self.gap
        if over > _EPS:
            return self.entrants[0][0], over / self.speed
        return None, 0.0

    def request(self, cells: int):
        return self.pool.request(cells)

    def full(self) -> bool:
        return self.pool.used >= self.cells

    def enter(self, env, cells: int):
        out = env.event()
        self.entrants.append((out, cells))
        self._total += cells
        while len(self.entrants) > 1 and \
                (self._total - self.entrants[0][1]) * self.cell - self.gap > _EPS:
            self._total -= self.entrants.popleft()[1]
        return out


class _CellSpace(Space):
    """A typed belt's cells as its `blocking.Space` (D1355): `capacity` cells, of which an
    item takes `cells_of`, first come first served, so a long item waits for its room whole.
    `content` counts cells; `full` and the deadlock check read `has_room`: no item waits for
    cells (one that does is the first that does not fit) and a cell is free."""

    def __init__(self, env, capacity: int, owner):
        super().__init__(env, capacity, owner)
        self._cells = simpy.Container(env, capacity=self.capacity, init=self.capacity)

    def take(self, entity, accepts: bool = True):
        held = getattr(entity, _RESERVED, None)
        if held is not None and held[0] is self:
            # Never reached: a belt is not a reserve target (validation); a reservation of
            # a block downstream rides on with the part, as `Space.take` leaves it.
            raise RuntimeError(f"Conveyor '{self.owner.name}': a place reserved before service "
                               f"on a typed belt is not supported (E_CONVEYOR_PARAM).")
        self.check_fits(entity)
        owner = self.owner
        # `gap` is room kept to the next item: an item that fits without it takes the belt
        cells = min(owner.cells_of(entity), self.capacity)
        get = self._cells.get(cells)
        get.entity = entity
        if not get.triggered:
            # It waits: the belt may now close a loop the station feeding it already checked
            # before this get was queued.
            self._sync()
            check_deadlock(owner)
        yield get
        self._granted(cells)
        if accepts:
            accept(entity)
            yield from placed(entity)
        return cells

    def check_fits(self, entity) -> None:
        owner = self.owner
        if math.ceil(owner.item_length(entity) / owner.cell_length - _EPS) > self.capacity:
            raise RuntimeError(f"Conveyor '{owner.name}': a {entity.e_type} item is longer than "
                               f"the belt's {self.capacity} cells (E_CONVEYOR_PARAM).")

    def room_for(self, entity) -> bool:
        cells = min(self.owner.cells_of(entity), self.capacity)
        return not self._cells.get_queue and self._cells.level >= cells

    def take_now(self, entity):
        cells = min(self.owner.cells_of(entity), self.capacity)
        self._cells.get(cells)              # granted at once: `room_for` said so
        self._granted(cells)
        return cells

    def claim(self):
        raise RuntimeError(f"Conveyor '{self.owner.name}': a state cannot put items back on a "
                           f"typed belt (E_CONVEYOR_PARAM).")

    def _granted(self, cells: int = 1) -> None:
        self.entered += 1
        self.content.add(cells, self.env.now)
        self._sync()

    def give_back(self, cells) -> None:
        self._cells.put(cells)
        self.content.add(-cells, self.env.now)
        self._sync()

    def has_room(self) -> bool:
        return not self._cells.get_queue and self._cells.level > 0

    @property
    def waiting(self) -> int:
        return len(self._cells.get_queue)


def write_exposed(block, key: str, value: int) -> None:
    """D1378: model variable `<block>.<key>` = *value*, on a change only, waking every HOLD
    condition that reads it (`ctx.wrote_variable`); signals stay edge events, as before."""
    name = f"{block.name}.{key}"
    variables = block.ctx.variables
    if variables.get(name) == value:
        return
    variables[name] = value
    block.ctx.wrote_variable(name)


def zone_hold_delay(entity, now: float) -> float:
    """R23: how long a piece taking *entity* now keeps it before it takes it, the zone-1 delay
    of the zoned belt handing it over when that belt's exit was held (0 otherwise); records the
    wait on the token for that belt's release. A belt (`_zone_hold`) and a CONVEYOR_TRANSFER
    read this one rule."""
    token = getattr(entity, _TOKEN, None)
    delay = getattr(token, "zone_delay", 0.0)
    if delay > 0 and now - token.zone_asked > _EPS:
        token.zone_waited = delay
        return delay
    return 0.0


def _stage_reach(rec, z: float, now: float, strict: bool = False):
    """#2014: when the stage of belt record *rec* (`Conveyor._parts`: its last stage at
    [3:7], when, x0, x1 or None standing, duration) puts its nose at *z* or past: *now* when it
    is there already, a later time, or None when this stage stands short of it. *strict*: past
    *z*, or at it and riding on."""
    t, x0, x1, duration = rec[3:7]
    if x1 is None or duration <= _EPS:
        x = x0 if x1 is None else x1
        return now if x > z + _EPS or (not strict and x >= z - _EPS) else None
    x = x0 + (x1 - x0) * min(1.0, (now - t) / duration)
    if x > z + _EPS or (x >= z - _EPS and (not strict or now < t + duration - _EPS)):
        return now
    if z < x1 - _EPS or (not strict and z <= x1 + _EPS):
        return t + duration * (z - x0) / (x1 - x0)
    return None


def _stage_x(rec, now: float) -> float:
    """#2016: where the stage of belt record *rec* puts its nose at *now* (an upper bound)."""
    t, x0, x1, duration = rec[3:7]
    if x1 is None:
        return x0
    return x0 + (x1 - x0) * min(1.0, (now - t) / duration) if duration > _EPS else x1
