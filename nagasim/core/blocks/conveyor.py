"""CONVEYOR: the belt block, with its stops, diverts, merges, eyes, zones and release rules (PHASE4 E1).

The points and sections live in `_belt_points.py` (R37).

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

**Routing rules** (conveyor system A7, D1359): diverts that share a `rule_group` (or one
divert with a `rule`) are routed by one RouteRule (core/routing.py), given on the group's
first divert along the belt; its exits are the group's diverts in belt order, plus the
exit `out` (the belt's end, past them all) if it names one (R28). The rule runs once, as
a part's front reaches that first divert; the part then passes the group's other diverts
up to its own, which takes it as a divert with a true condition does. A part counts under
way to its exit until it reaches the exit's target piece (R5, `watch`), or leaves the
belts on its way there by a divert (R26). While every exit is at its limit the part keeps
its cell at the first divert, the belt behind packs as at a stop, and the rule runs again
when an exit's count drops; the hold is that divert's blocked share and one of its waits.

**Merges** (D854, accumulating belts only): the parts `from` names join at
`at`. A main part claims the point while its front is within a spacing of it;
a merging part joins with no claim, a spacing after the last part passed and
room in the section after it. Priority "belt" never makes a main part wait;
"merge" holds the main parts a spacing before the point until the join.

**Photo-eyes** (D1347): `eyes` are points that take nothing and never hold a part. An
eye fires when the stage rules above put a part's front at it: a front eye at its `at`, a
back eye at `at` + `part_length` (the front's place when the back clears `at`). A firing
counts, traces, raises the eye's signal, then runs its ASSIGN rows on the part, so a divert
downstream reads what the eye wrote. On an accumulating belt an eye bounds a section like
any point, except on a zoned belt (R33, D1363): there it bounds none, and each part fires it
from the zone chain's times (its free ride, its stand, its restart).

**Typed belts** (D1355): a belt with a `type` has a small standard `cell` (default 1 cm)
and an item takes `cells_of` = ceil((its length + `gap`) / cell) of them, measured back
from its front. Every rule above then reads the item's own occupied length where it read
`spacing`: the entry rhythm after a boarding item, the gap behind the part that last left
a point or the end, the cells the belt and each section hold. An untyped belt is one
item per cell of `spacing`, exactly as before.

**Zones** (D1356): `zones` {"count", "release_delay"} split the belt into `count` equal
zones, zone 1 at the exit, on an accumulating belt. Parts accumulate as above; only the
restart changes. When the end part was held at the exit and the exit frees at t0, every part
then standing in the end queue restarts at t0 + j * release_delay, j the zone of its front
(the end part itself: zone 1, keeping the exit unit; one handed to a next belt waits there
before that belt takes it, R23), and never comes nearer than its occupied length to the part
ahead; a part that packs in later keeps its own zone's stagger. The walk over the end queue
happens once per release: each standing part's front is the end minus the cells of the parts
packed ahead of it (`_zone_wave`). No stop or merge on a zoned belt (R24); eyes fire from the
chain's times (R33). Each zone's dieback
time (a part's front in it, standing) is kept in `zone_dieback_time`. A delay of 0 is the
belt without zones, to the float.

**Release rules** (D1357): `release` holds the end part, once it is at the end and free to
leave, before the end stop and the hand-over, so the parts behind pack as against a held exit.
Metering by `gap` (and `singulation`): it leaves once the part that left last has its tail
`gap` past the end, `_end_left` + (that part's item length + gap) / speed. By `interval`: front
to front, `_end_left` + max(interval, that part's occupied length) / speed. A `slug`: the lead
stands until `count` parts are queued or `timeout` has passed since it stood; a part is queued
once it stands at its accumulated place (R8), behind a point too (R25): its free arrival there
on the ride that reaches it, read from the rides each part records as it starts them, never
before the part ahead, so the count is event-exact with no per-cell event. A stop is on that way
only when the place is past it. The queued parts then leave nose to tail by the catch-up rules.
Accumulating belts without zones only; `release_waits` keeps each part's hold at the end (in a
slug, the lead's stand; the parts let go with it record 0).

**Reversing** (D1368): a `reversible` belt turns during a run when its `reverse_signal` is
raised or item code calls `item.reverse`. Each riding part's remaining distance becomes the
distance it has travelled; parts then leave at the start, by the exit `reverse_to` names, and
nothing gets on until the belt runs forward again (the piece feeding it holds its part). Such a
belt has no stops, diverts, merges, eyes, zones or release rules; its ride is
`_belt_reverse.ReverseRide`. Reversible belts linked end to start with nothing between are one
chain (D1392, `chain_to` at load): they turn together and parts cross the joints both ways.

**Positions** (D734): every stage change writes one `belt_stage` trace event
(where the part is, where it goes, how long it takes), and `playback.Timeline`
derives each part's position from them; tracing never touches results.
"""
import bisect
import logging
import math
from operator import itemgetter
from collections import deque

import simpy

from nagasim.core.arealink import real_target
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._belt_release import BeltRelease
from nagasim.core.blocks import _belt_ahead
from nagasim.core.blocks._belt_ahead import _ARR as _LEAF_ARRIVED, _FIRST as _LEAF_FIRST, \
    _FULL as _LEAF_FULL, _STALE as _LEAF_STALE, AheadIndex
from nagasim.core.blocks._belt_reverse import ReverseRide
from nagasim.core.blocks._belt_zones import BeltZones
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.blocks._routing import RoutePoint, rule_of
from nagasim.core.routing import divert_exits, divert_groups, divert_slots
from nagasim.core.blocks.assign import apply_rows, compile_rows
from nagasim.core.expressions import ExpressionError
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.blocking import Space, accept, arm, check_deadlock
from nagasim.core.seizing import acquire, release
from nagasim.core import beltpick
from nagasim.core.conveyor_types import TYPES
from nagasim.core import conveyor_path
from nagasim.core.conveyor_checks import route_taken, run_warning
from nagasim.core.conveyor_path import Pose, parse_path, path_length
from nagasim.core.network import normalise_rotation
from nagasim.core.stats import Tally, TimeWeighted
from nagasim.core.timeunits import conversion_factor
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Conveyor", "SPEC", "zone_hold_delay"]

# #2014: the stages of a part ahead that end only once a part further on moves (or the held end
# hands over): what the live deadlock check reads as "packed back from the end"
_UNTIMED = ("packed", "due", "blocked", "stop", "handover")
_SEQ = itemgetter(7)                    # a belt record's place in belt order
_FAR = 1e9                              # a gone part's ride: past any belt
# #2014 (R72): the stages a part holds where it has arrived, its record then exact (a wait to
# arrive, `packed` or `due`, names the place it heads for, an upper bound)
_ARRIVED = frozenset(("dwell", "wait", "blocked", "stop", "handover", "divert", "push",
                      "merge"))
from nagasim.core.blocks._belt_points import (  # noqa: E402,F401 (R37: moved, still importable here)
    _EPS, _BOUND_MARGIN, _DEFAULT_CELL, DivertSpec, _divert_params, _Stop, _Divert, _MeetPoint,
    _Eye, _Merge, _MergeEntry, _Left, _Section, _CellSection, _CellSpace, zone_hold_delay,
    write_exposed, _stage_reach, _stage_x)


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
        # R39 (D1377): each measured param and point `at` as written, with its unit when one is
        # given, so get_params_dict gives back what was set; the engine reads the scaled values
        self._given: dict = {}
        self._given_units: dict = {}
        self._given_at: dict = {}
        self.spacing = 1.0
        self.type = ""                  # a key of conveyor_types.TYPES; "" is the untyped belt
        self.gap = 0.0                  # base length unit, a typed belt's free length per item
        self.cell = 0.0                 # base length unit, a typed belt's cell; 0: 1 cm (R20)
        self._cells_by_type: dict = {}  # a typed belt's cells_of, per entity type
        # D1356: as declared ({} = none); the zone count and the delay in the model's time unit
        self.zones: dict = {}
        self._belt_zones = BeltZones(self)
        self._belt_release = BeltRelease(self)
        self._zone_count = 0
        self._zone_delay = 0.0
        # The parts past the last point, end part first (entries: see `_zone_walk`)
        self._zone_chain = deque()
        self._exit_since = None         # since when the end part stands at a held exit
        self._zone_dieback: list = []
        self._zone_covered: list = []   # per zone: the end of the dieback counted so far
        self._zone_since = 0.0          # the warm-up's end
        # R33: on a zoned belt the eyes are no points; each fires from the zone chain's times
        # (`_zone_eye_run`), and `_zone_moved` fires as a release sets the stands
        self._zone_eyes: list = []
        self._zone_moved = None
        # D1357: as declared ({} = none); the rule: "gap" or "interval" with its length in
        # the model's base unit, or a slug's count and timeout in the model's time unit
        self.release: dict = {}
        self._release_kind = ""
        self._release_len = 0.0
        self._slug_n = 0
        self._slug_timeout = 0.0
        # A slug belt's parts not yet let go, in belt order, lead first: [entity, its occupied
        # length, its rides so far as (from, when, to)] (R25); `_slug_of` finds them by id
        self._slug_group: list = []
        self._slug_of: dict = {}
        self._slug_lead = None          # when the lead stood at the end
        self._slug_changed = None       # fires when the group or a ride changes
        self._slug_go: set = set()      # ids of the parts let go, still on the belt
        self.release_waits = Tally()    # per part through the end: how long the rule held it
        self.accumulating = True
        self.exit_resource = ""
        self.from_node = ""
        self.to_node = ""
        self.stops: list = []           # as declared: [(at, time, resource)] (D732)
        self.diverts: list = []         # as declared: [DivertSpec] (D851, D1343, A7)
        self.merges: list = []          # as declared: [(at, source block, priority)] (D854)
        self.eyes: list = []            # as declared: [(at, edge, assign rows, signal)] (D1347)
        self._part_length = 0.0         # as declared; 0 means the spacing
        # D1368: as declared; the ride of a reversible belt, built with the cells
        self.reversible = False
        self.reverse_signal = ""
        self.reverse_to = ""
        self._rev = None
        self._signal_hooked = False
        self._exit_slots = None
        # D1392: the reversible pieces linked straight to its start and from its end, which
        # reverse with it as one chain (`chain_to`, at load)
        self.chain_prev = None
        self.chain_next = None
        # per exit (True: the end, False: the start), set by `blocking.analyse`: it hands over
        # with the D78 token, as the end does to a next belt (D115)
        self.exit_blocks = {True: False, False: False}
        # Conveyor system A: straight and arc pieces, as declared, in the unit of `length`;
        # `_path_factor` takes them to the model's base unit. Non-empty: the path sets the length.
        self.path: list = []
        self._path_factor = 1.0
        # Where the belt starts on the floor plan (the block's `layout_*`, set by the loader).
        self.layout_position = None
        self.layout_rotation = 0.0
        # Built lazily: the cell space needs the resolved length, which may come
        # from the network, which is only there once the model is loaded. The
        # stops are built with it, entry to end, and `_sections` are the cells of
        # each section between two stops (accumulating belts only).
        self._space = None
        self._length = None             # the resolved belt length, with the space
        self._stops: list = []
        self._diverts: list = []
        self._merges: list = []
        self._eyes: list = []
        self._eye_points: list = []     # every eye's points: a back eye has one per item length
        self._meets: list = []          # one meeting point per intercept divert (D1343)
        self._points: list = []         # stops, diverts, merges and eyes, entry to end
        self._on: set = set()           # ids of the parts on the belt (a merge's `ahead`)
        self._sections: list = []
        self._divert_waits = 0          # parts waiting at a divert for its target
        # A7 (D1359): per entity id, the callbacks waiting for it to reach this belt (R5: its
        # first eye here, else leaving it) and, on a belt a routed load only passes on its way
        # to its target, for it to leave by a divert (R26); a routed part's chosen divert, and
        # its hold when it diverts where it stood, counted in that divert's wait
        self._reach: dict = {}
        self._offs: dict = {}
        self._route_to: dict = {}
        self._route_held: dict = {}
        # R41: per load id, (the load, the CONVEYOR_TRANSFER) of a transfer waiting for the load
        # to reach an eye here (its handshake), what the live deadlock check reads
        self._handshakes: dict = {}
        # R58: a part stands at an exit with no next block; the belt is a blocked station then
        self.dead_end = False
        # D1378: `expose` writes the belt's state as model variables; `_covering`, per eye
        # index, the ids of the parts whose body lies across that eye (D1379)
        self.expose = False
        self._covering: dict = {}
        # D1390: elsewhere each part's edges come from its nose (`_eye_levels`); the times of
        # the wake-ups scheduled for that, so one instant gets one
        self._nose_eyes = False
        self._eye_due: list = []
        self._eye_rows: dict = {}       # A2-R6: per part id, [eyes cleared, eyes covered, next edge]
        self._eyes_along = None         # the eyes by sensor place
        self._next_entry = 0.0
        self._prev_clear = None
        self._end_left = -math.inf      # when the last part left the end (#950)
        self._end_occ = 0.0             # the belt length it took (D1355)
        self._end_len = 0.0             # its item length (metering by gap, D1357)
        # Non-accumulating belt state: a fresh `stop` event while it runs, a
        # fresh `resume` event while it is stopped. It stops while its end is
        # blocked or a part is at one of its stops (D732).
        self._stopped = False
        self._end_halt = False
        self._holds = 0
        self._stop_event = None
        self._resume_event = None
        # #2015: the entry rhythm is belt travel. How often the belt stopped, how long it stood
        # in all, since when it stands, and the entry slot still to run when it stopped
        self._halts = 0
        self._halted = 0.0
        self._halted_since = 0.0
        self._entry_owed = 0.0
        # The end part waits for a downstream belt to take it (D115); with a part
        # waiting at a divert (D852), what the deadlock check reads (`held_downstream`),
        # since `blocked` also covers the exit_resource wait.
        self._end_held = False
        self.entered = 0
        self.exited = 0
        self.occupancy = TimeWeighted(env.now)
        self.blocked = TimeWeighted(env.now, level=0.0)
        self.transit = Tally()
        # A12 (D1373, R63): the dieback, belt length x time held by standing parts. At one speed
        # a part rides or stands, so by time T it stood its time on the belt less its ride from
        # where it got on to where its nose is at T. `_dieback_extra` adds that, x its occupied
        # length, as a part leaves, less what each part on the belt had stood at the warm-up;
        # `dieback_area` adds it for each part still on the belt at the window's end
        # (`_stood_now`). `_parts`, per part id on a one-way belt: [occupied length, entered,
        # start, its last stage (when, x0, x1 or None standing, duration), its place in belt
        # order (`_seq`: a part boards last; a merged one between the part it lands behind and
        # the next), the part itself, the event a part waiting behind it waits on (#2014) or
        # None, True while it stands where it arrived (`_ARRIVED`), the place it last arrived
        # at or None (A2 task 2: its nose is no nearer since), and for the walk's index
        # (`_ahead`, speed review D) its leaf or None, its offset in it and its place in it]
        self._parts: dict = {}
        self._seq = 0.0
        self._dieback_extra = 0.0
        # #2014: on a belt with points, the stages also time the parts. `_gone` keeps, as
        # records of the same shape, the parts that just left by the end or a divert, riding on
        # until their back is past where they left (the gap the rules keep behind them);
        # `_reshaped` fires when a part leaves short of the end (the only change that can bring
        # a wait forward: the parts beyond it then need its length less)
        self._gone: list = []
        self._order: list = []          # `_parts`' records in belt order (`_ahead_due`)
        # speed review D: the index `_ahead_due` steps over runs of plainly clear parts by;
        # decided at the first part (only on a belt that walks), None once lengths turn mixed
        self._ahead = False
        self._reshaped = None
        # R65: 1 while the release rule holds the end part (D1357), for `Release Hold %`
        self.release_held = TimeWeighted(env.now)
        env.ctx.conveyors[self.name] = self

    # -- geometry ------------------------------------------------------------
    @property
    def belt_length(self) -> float:
        """The path's length when it has one, else the declared length, else the network
        distance between its endpoints."""
        if self.path:
            return path_length(parse_path(self.path)) * self._path_factor
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
    def start_pose(self) -> Pose:
        """Floor pose of the entry, in the floor-plan frame (D1354): the heading is the block's
        `layout_rotation`, as `belt_geometry` reads it from a block dict."""
        return _start_pose(self.layout_position, self.layout_rotation)

    @property
    def capacity(self) -> int:
        """Cells: floor(length / cell), at least 1. On an untyped belt a cell holds one item;
        on a typed belt an item takes `cells_of` cells (D1355)."""
        if self.type:
            # `//` would give 299 for 3.0 // 0.01: the cell count is a length ratio
            return max(math.floor(self.belt_length / self.cell_length + _EPS), 1)
        return max(int(self.belt_length // self.cell_length), 1)

    @property
    def cell_length(self) -> float:
        """Belt length of one cell. An untyped belt: `spacing`, one item per cell (a filled
        default cannot be told from a set value, so the rule keys on `type`). A typed belt:
        `cell`, unset meaning 1 cm (D1355, R20). Engine lengths are metres."""
        if not self.type:
            return self.spacing
        return self.cell if self.cell > 0 else _DEFAULT_CELL

    def cells_of(self, entity) -> int:
        """Cells one item takes: 1 on an untyped belt; on a typed belt
        ceil((item length + gap) / cell), at least 1 (D1355)."""
        if not self.type:
            return 1
        cells = self._cells_by_type.get(entity.e_type)
        if cells is None:
            length = self._type_length(self.ctx.entity_manager.get_type(entity.e_type))
            cells = max(math.ceil((length + self.gap) / self.cell_length - _EPS), 1)
            self._cells_by_type[entity.e_type] = cells
        return cells

    def _occ(self, entity) -> float:
        """Belt length one item takes, measured back from its front: the gap the part
        behind it keeps."""
        if not self.type:
            return self.spacing
        return self.cells_of(entity) * self.cell_length

    def item_length(self, entity) -> float:
        """Belt length along which one item lies: its entity type's `size.length`, else
        `part_length` when set, else a typed belt's type `zone_length`, else the cell."""
        return self._type_length(self.ctx.entity_manager.get_type(entity.e_type))

    def _back_offsets(self) -> list:
        """The distinct item lengths a back eye is laid out for, shortest first: every entity
        type's own, an unsized type's being `part_length`, else the cell. A model with no
        sizes has one length, so its belts keep one point."""
        types = self.ctx.entity_manager.all()
        offsets = sorted({round(self._type_length(t), 9) for t in types.values()})
        return offsets or [self.part_length]

    def _type_length(self, info) -> float:
        size = info.get("size", {}).get("length")
        if size:
            return size
        return self.part_length

    @property
    def part_length(self) -> float:
        """The length of one part along the belt when its entity type has no size:
        `part_length` when set, else a typed belt's type `zone_length`, else the cell."""
        if self._part_length > 0:
            return self._part_length
        return TYPES[self.type].zone_length if self.type else self.cell_length

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
            self._space = (_CellSpace if self.type else Space)(self.env, self.capacity, self)
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
        self._diverts = [_Divert(self.env, index, d.at, d.condition, d.time, d.when_full,
                                 intercept=d.intercept, on_late=d.on_late, side=d.side)
                         for index, d in enumerate(self.diverts)]
        self._build_routes()
        self._merges = [_Merge(self.env, index, *merge, self.capacity)
                        for index, merge in enumerate(self.merges)]
        if self._merges and not self.accumulating:
            raise RuntimeError(f"Conveyor '{self.name}': merges need an accumulating belt "
                               f"(E_CONVEYOR_MERGE).")
        if self._zone_count and not self.accumulating:
            raise RuntimeError(f"Conveyor '{self.name}': zones need an accumulating belt "
                               f"(E_CONVEYOR_PARAM).")
        if self._zone_count and (self._stops or self._merges):
            # R24: the cascade runs at the exit only; behind a stop or a merge is a follow-up
            raise RuntimeError(f"Conveyor '{self.name}': zones on a belt with stops or merges "
                               f"are not supported yet (E_CONVEYOR_PARAM).")
        if self._release_kind and (self._zone_count or not self.accumulating):
            # D1357: the hold packs the parts behind (accumulation), and zones own the end's
            # restart timing, so the two rules never meet on one belt
            raise RuntimeError(f"Conveyor '{self.name}': a release rule needs an accumulating "
                               f"belt without zones (E_CONVEYOR_RELEASE).")
        self._zone_dieback = [0.0] * self._zone_count
        self._zone_covered = [-math.inf] * self._zone_count
        if not self.type and self._part_length > self.cell_length + _EPS:
            raise RuntimeError(f"Conveyor '{self.name}': part_length {self._part_length:g} is "
                               f"longer than the cell {self.cell_length:g} (E_CONVEYOR_EYE).")
        self._meets = [self._meet_point(divert) for divert in self._diverts if divert.intercept]
        declared, backs = {}, []
        for index, (sensor, edge, rows, signal) in enumerate(self.eyes):
            if edge not in ("front", "back"):
                raise RuntimeError(f"Conveyor '{self.name}': eye {index} has edge '{edge}'; "
                                   f"it is \"front\" or \"back\" (E_CONVEYOR_EYE).")
            if edge == "back":
                backs.append((index, sensor, compile_rows(rows), signal))
            else:
                declared[index] = _Eye(index, sensor, sensor, edge, compile_rows(rows), signal)
        # A back eye's points stay clear of every other point: those built so far.
        fixed = [*self._stops, *self._diverts, *self._merges, *declared.values(), *self._meets]
        taken = [point.at for point in fixed]
        back_points = []
        for index, sensor, compiled, signal in backs:
            # A back eye fires when the back clears the sensor: the front is a part ahead.
            # One point per item length that can occur, at sensor + that length, so a longer
            # item stands, queues and is held by the same rules as a front eye (D1347).
            offsets = kept = self._back_offsets()
            if len(offsets) > 1:
                # A type that may never enter must not refuse the model: keep the points that
                # are on the belt and clear of every other point; an item whose own point
                # was dropped is refused as it enters (`_check_entry`).
                kept = [o for o in offsets if 0.0 < sensor + o < length
                        and all(abs(sensor + o - at) > _EPS for at in taken)] or offsets[:1]
            eyes = [_Eye(index, sensor, sensor + offset, "back", compiled, signal, offset)
                    for offset in kept]
            eyes[0].dropped = set(offsets) - set(kept)
            declared[index] = eyes[0]           # the declared eye: it keeps the count
            taken.extend(eye.at for eye in eyes)
            back_points.extend(eyes)
        self._eyes = [declared[index] for index in sorted(declared)]
        self._eye_points = [*(eye for eye in self._eyes if eye.edge != "back"), *back_points]
        # D1379: an exposed eye's two edges, on a zoned belt whose points are all eyes (R33),
        # which bound no section there: a point that never fires carries the other edge at the
        # zone chain's times. Elsewhere such a point would split a section and move the parts
        zone_eyes = bool(self._zone_count) and not (self._stops or self._diverts
                                                    or self._merges or self._meets)
        ghosts = self._eye_edges(length) if self.expose and zone_eyes else []
        # D1390: on every other belt the edges come from the part's nose and add no point
        self._nose_eyes = bool(self.expose and self._eyes and not zone_eyes
                               and eyes_blocked(self._zone_count, self._diverts))
        self._points = sorted([*self._stops, *self._diverts, *self._merges, *self._eye_points,
                               *self._meets, *ghosts], key=lambda p: p.at)
        # The gap behind the part that last left a point or the end (D1355): until one has,
        # it is read with a `left` of -inf or a state's, so the cell, as before.
        for point in self._points:
            point.occ = self.cell_length
        self._end_occ = self.cell_length
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
                if isinstance(before, _Eye):
                    code = before.code
                raise RuntimeError(f"Conveyor '{self.name}': two points share the place "
                                   f"{point.at:g}; a divert, a merge or an eye has one of its own "
                                   f"({code}).")
            point.slot = slot
        self._build_reverse()
        self._zone_eyes = []
        if self._zone_count and all(isinstance(point, _Eye) for point in self._points):
            # R33: an eye never holds a part, so on a zoned belt it bounds no section: every
            # part rides in the zone chain from its entry and the eyes fire from its times
            self._zone_eyes, self._points = self._points, []
        if self._points and self.accumulating:
            ends = [0.0, *(point.at for point in self._points), length]
            section = _CellSection if self.type else _Section
            self._sections = [section(self.env, b - a, self.cell_length, self.speed)
                              for a, b in zip(ends, ends[1:])]

    def _build_reverse(self) -> None:
        """D1368: a reversible belt has no points, zones or release rule (two-way lines are
        plain reversible pieces between mechanisms); its ride and its signal hook."""
        self._rev = None
        if not self.reversible:
            return
        found = [what for what, has in (
            ("stops", self.stops), ("diverts or robot picks", self.diverts),
            ("merges", self.merges), ("photo-eyes", self.eyes), ("zones", self._zone_count),
            ("a release rule", self._release_kind)) if has]
        if found:
            raise RuntimeError(f"Conveyor '{self.name}': a reversible belt cannot have "
                               f"{', '.join(found)} (E_CONVEYOR_REVERSE).")
        self._rev = ReverseRide(self)
        members = self.chain_members()
        # D1392: one hook per signal and chain, so one raise turns the chain once
        earlier = members[:members.index(self)]
        if self.reverse_signal and not self._signal_hooked                 and all(m.reverse_signal != self.reverse_signal for m in earlier):
            self.ctx.signal_hooks.setdefault(self.reverse_signal, []).append(
                self._reverse_signalled)
            self._signal_hooked = True

    def _eye_edges(self, length: float) -> list:
        """D1379, an exposed zoned belt whose points are all eyes: where each eye's two edges
        fall, front at the sensor and back at sensor + each item length, as each eye point's
        `edges`; the place of an edge no eye has gets a ghost eye (it never fires), so the
        zone chain gives both edges at exact times (`_zone_eye_run`). An edge off the belt has
        none (a part leaving clears its eyes); nothing is refused. Returns the ghosts."""
        fixed = [*self._stops, *self._diverts, *self._merges, *self._eye_points, *self._meets]
        ghosts = []
        for eye in self._eyes:
            index, sensor = eye.index, eye.sensor
            wanted = [(sensor, (index, True, None))]
            wanted += [(sensor + offset, (index, False, offset))
                       for offset in self._back_offsets()]
            for place, edge in wanted:
                point = next((p for p in (*fixed, *ghosts) if abs(p.at - place) <= _EPS), None)
                if point is None:
                    if not _EPS < place < length - _EPS:
                        continue
                    point = _Eye(index, sensor, place, "back" if edge[2] is not None
                                 else "front", [], "", edge[2] or 0.0, ghost=True)
                    ghosts.append(point)
                point.edges.append(edge)
        return ghosts

    def _reverse_signalled(self, name: str) -> None:
        if name == self.reverse_signal and self._rev is not None:
            self.set_direction(not self._rev.forward)

    def set_direction(self, forward: bool) -> None:
        """Run a reversible belt forward (True) or backward from now (D1368); the same
        direction again changes nothing. What `item.reverse` calls. D1392: every piece of its
        chain turns with it, in this instant."""
        if not self.reversible:
            raise ValueError(f"conveyor {self.name!r} is not reversible")
        for member in self.chain_members():
            member.space                        # noqa: B018 -- builds the ride
            member._rev.turn(bool(forward))

    def chain_to(self, after) -> None:
        """D1392, at load: *after*, a reversible belt this one's end links to directly, is the
        next piece of its chain. Only the chain's first piece names `reverse_to`; a ring has
        no first piece."""
        if after.reverse_to:
            raise RuntimeError(f"Conveyor '{after.name}': reverse_to belongs to the first piece "
                               f"of its chain; '{self.name}' feeds it (E_CONVEYOR_REVERSE).")
        head = self
        while head.chain_prev is not None:
            head = head.chain_prev
            if head is after:
                break
        if head is after or after is self:
            raise RuntimeError(f"Conveyor '{self.name}': reversible belts linked end to start "
                               f"in a ring have no chain start (E_CONVEYOR_REVERSE).")
        self.chain_next, after.chain_prev = after, self

    def chain_members(self) -> list:
        """D1392: the pieces of this belt's chain, first piece first (just this belt when it
        is linked to no other reversible belt)."""
        head = self
        while head.chain_prev is not None:
            head = head.chain_prev
        members = [head]
        while members[-1].chain_next is not None:
            members.append(members[-1].chain_next)
        return members

    def turn_event(self):
        """An event that fires at this belt's next reversal, None for a belt that cannot
        reverse: a CONVEYOR_TRANSFER's no-eye handshake ends there (the load no longer
        boards forward)."""
        return self._rev.turned if self._rev is not None else None

    @property
    def exit_slots(self) -> dict:
        """The outgoing link of each exit (True: the end, False: the start of a reversible
        belt): the end is link 0 and `reverse_to` link 1, the order the loader puts them in
        (`conveyor_path.belt_exit_links`); a single link takes both. Read once the links are
        wired (`blocking.analyse`), then kept."""
        if self._exit_slots is None:
            self._exit_slots = self._resolve_exits()
        return self._exit_slots

    def _resolve_exits(self) -> dict:
        if self.chain_prev is not None:
            return {True: 0, False: None}       # D1392: its start hands back to the piece before
        nexts = self.next_process or []
        back = 1 if len(nexts) > 1 else 0
        if self.reverse_to != "in" and back < len(nexts) \
                and getattr(real_target(nexts[back]), "name", None) != self.reverse_to:
            raise RuntimeError(f"Conveyor '{self.name}': reverse_to '{self.reverse_to}' is not "
                               f"an exit of the belt (E_CONVEYOR_REVERSE).")
        return {True: 0, False: back}

    def _build_routes(self) -> None:
        """A7 (D1359): the divert groups of routing rules. A group is its `rule_group` (a divert
        with a rule and no group is a group of one), in belt order; its first divert carries the
        rule, exit i leaves by the group's divert i, and exits left out are the diverts' ports."""
        # R38: the grouping validation makes, over the params the diverts were read from
        views = [{"at": spec.at, **({"rule": spec.rule} if spec.rule is not None else {}),
                  **({"rule_group": spec.rule_group} if spec.rule_group else {})}
                 for spec in self.diverts]
        for key, grouped in divert_groups(views).items():
            members = [(self._diverts[index], self.diverts[index]) for index, _view in grouped]
            diverts = [divert for divert, _spec in members]
            label = f"rule group '{key}'" if not key.startswith("#") \
                else f"divert {diverts[0].index}"
            if members[0][1].rule is None or any(s.rule is not None for _d, s in members[1:]):
                raise RuntimeError(f"Conveyor '{self.name}': {label} carries its rule on its "
                                   f"first divert along the belt, and only there "
                                   f"(E_CONVEYOR_DIVERT).")
            if any(d.intercept or d.when_full == "pass" for d in diverts):
                raise RuntimeError(f"Conveyor '{self.name}': a divert of {label} meets a robot "
                                   f"or passes when full; a routed divert does neither "
                                   f"(E_CONVEYOR_DIVERT).")
            rule = rule_of(self, members[0][1].rule, divert_exits(d.index for d in diverts))
            try:
                slots = divert_slots(rule.exits, len(diverts))
            except ValueError as exc:
                raise RuntimeError(f"Conveyor '{self.name}': {label}: {exc} "
                                   f"(E_CONVEYOR_DIVERT).") from None
            if self._zone_count and rule.can_hold():
                raise RuntimeError(f"Conveyor '{self.name}': {label} can hold a part, which "
                                   f"zones do not support yet (E_CONVEYOR_PARAM).")
            # per exit: the divert that takes the part, None for `out` (R28: past them all)
            chosen = [diverts[s] if s is not None else None for s in slots]
            for divert in diverts:
                divert.head, divert.members = diverts[0], chosen
            diverts[0].route = RoutePoint(self, rule, [d.exit if d is not None else 0
                                                       for d in chosen])

    def watch(self, entity, fn, off_only: bool = False) -> None:
        """Call fn(entity) once, as *entity* reaches this belt as a target piece (R5: when it
        next fires an eye here, the first along the belt that fires for it, or else leaves the
        belt, at its end or at a divert); *off_only*: only if it leaves by a divert (R26, a
        belt it passes on the way to its target)."""
        (self._offs if off_only else self._reach).setdefault(entity.id, []).append(fn)

    def unwatch(self, entity, fn) -> None:
        for watched in (self._reach, self._offs):
            fns = watched.get(entity.id)
            if fns and fn in fns:
                fns.remove(fn)
                if not fns:
                    del watched[entity.id]

    def reach_event(self, entity):
        """R5 (A7, D1359): an event that succeeds once *entity* reaches this belt (`watch`).
        Routing counts a load under way until then; the CONVEYOR_TRANSFER handshake reads it."""
        event = self.env.event()
        self.watch(entity, lambda _entity: event.triggered or event.succeed())
        return event

    def has_eye_for(self, entity) -> bool:
        """Some eye on this belt fires for *entity* (a front eye for every item, a back eye's
        point for an item of its length): what the CONVEYOR_TRANSFER handshake waits for."""
        self.space                              # the eye points are built with the cells
        length = self.item_length(entity)
        return any(eye.fires_for(length) for eye in self._eye_points)

    def _reached(self, entity, off: bool = False) -> None:
        fns = self._reach.pop(entity.id, [])
        if off:
            fns = [*fns, *self._offs.pop(entity.id, [])]
        for fn in fns:
            fn(entity)

    def _meet_point(self, divert) -> "_MeetPoint":
        """The meeting point of intercept *divert*: the plan's metres along the belt, in the
        model's length unit, of the robot block its link goes to."""
        nexts = self.next_process or []
        target = nexts[divert.exit] if divert.exit < len(nexts) else None
        meet = self.ctx.belt_meets.get(getattr(target, "name", None))
        if meet is None or not hasattr(target, "ready_now"):
            raise RuntimeError(f"Conveyor '{self.name}': divert {divert.index} meets a robot, so "
                               f"its link must go straight to a robot that picks from it "
                               f"(E_CONVEYOR_DIVERT).")
        return _MeetPoint(divert, meet.meet * conversion_factor("length", "meters",
                                                                self.ctx.units.length))

    # -- the live deadlock check (R41, R45) -----------------------------------
    def handshake(self, entity, transfer) -> None:
        """*transfer* waits for *entity* to reach an eye here (its handshake); None: no more."""
        if transfer is None:
            self._handshakes.pop(entity.id, None)
        else:
            self._handshakes[entity.id] = (entity, transfer)

    @property
    def zone_dieback_time(self) -> list:
        """Per zone, zone 1 at the exit: the time a part's front stood in it (D1356)."""
        return self._belt_zones.zone_dieback_time

    def _stands(self, entity) -> None:
        """R41: *entity* comes to a stand. When a transfer waits for it to reach an eye, the loop
        through that transfer may have closed only now, so it is checked here."""
        if entity.id in self._handshakes:
            check_deadlock(self)

    def handshake_stands(self, feeder: str) -> bool:
        """R41: the transfer *feeder* waits for a load to reach an eye here, and the load stands
        where only a part leaving this belt's held end lets it move: in an untimed wait short of
        the eye, with only eyes and stops between it and the end and every section past it full.
        The belt's free cells then take nothing from that transfer."""
        if not self._end_held:
            return False
        for entity, transfer in self._handshakes.values():
            if transfer.name == feeder and self._stands_short(entity):
                return True
        return False

    def _stands_short(self, entity) -> bool:
        stage, _until, _entered, slot = getattr(entity, "_belt", (None, None, None, None))
        if stage not in ("packed", "due", "blocked"):
            return False                # riding, moving up or dwelling: its time ends
        if slot is None:
            return True                 # packed at the end behind the held end part
        if not all(isinstance(point, (_Eye, _Stop)) for point in self._points[slot:]):
            return False                # a divert or a merge may move parts without the end
        if all(section.full() for section in self._sections[slot + 1:]):
            return True
        # #2014: a section with a free place need not be room. Every part ahead standing in an
        # untimed wait packs back from the held end just as well (the end part, held, already
        # carries the next block's stage)
        rec = self._parts.get(entity.id)
        if rec is None or not self._end_held:
            return False
        ahead = sorted((other for other in self._parts.values() if other[7] < rec[7]),
                       key=lambda other: other[7])
        return all(getattr(other[8], "_belt", ("",))[0] in _UNTIMED for other in ahead[1:])

    @property
    def entry_closed(self) -> bool:
        """R45: a reversible belt's part waits at its start for the next block to take it, so
        nothing boards, whichever way the belt runs, until that part leaves (`may_enter`)."""
        return self._rev is not None and self._rev.start_held()

    @property
    def held_downstream(self) -> bool:
        """A part waits for a downstream space: the end's (D115) or a divert's (D852)."""
        return self._end_held or self.dead_end or self._divert_waits > 0 or (
            self._rev is not None and (self._rev.waits > 0 or self._rev.joint_held()))

    def room_now(self, entity=None) -> bool:
        """A part handed to this belt now would be on it in this instant: a free cell nobody
        queues for (on a typed belt, *entity*'s cells), a first section with room, a running
        belt and an entry slot due. What an upstream divert in "pass" mode reads (D852)."""
        space, now = self.space, self.env.now
        cells = self.cells_of(entity) if entity is not None else 1
        if space.content.level + cells > space.capacity or space.waiting or self.entry_full:
            return False
        if self._rev is not None:
            return self._rev.may_enter()
        if (not self.accumulating and self._stopped) or self._next_entry > now + _EPS:
            return False
        if self._sections:
            due, lag = self._sections[0].due_lag()
            if due is not None and (not due.triggered or due.value + lag > now + _EPS):
                return False
            due = self._ahead_due(math.inf, 0.0)[0]     # #2014: the last part on is clear
            if due is None or due - now > _EPS:
                return False
        return True

    # -- the belt clock (non-accumulating only) ------------------------------
    @property
    def stopped(self) -> bool:
        """A non-accumulating belt stopped by its end takes nothing on (the deadlock
        check). A stop holding the belt is not read: its time ends on its own (D732)."""
        if self._rev is not None:
            return not self._rev.running
        return self._end_halt

    @property
    def entry_full(self) -> bool:
        """The first section of a belt with stops is full: free cells or not, nothing
        gets on until a part there moves on (the deadlock check, D732)."""
        return bool(self._sections) and self._sections[0].full()

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
        if self.expose:
            write_exposed(self, "running", 0)   # R67: the drive stops at its held exit
        self._sync_belt()

    def _start_belt(self) -> None:
        if not self._end_halt:
            return
        self._end_halt = False
        self.blocked.set(0.0, self.env.now)
        if self.expose:
            write_exposed(self, "running", 1)
        self._sync_belt()

    def _seed_exposed(self) -> None:
        """D1378: the exposed names exist from the start (VAR() of an unwritten name fails):
        running, forward on a reversible belt, each eye's count 0 and, where exposed, clear. A
        name already written (set again during a run) keeps its value."""
        variables = self.ctx.variables
        for key, value in exposed_keys(self.reversible, len(self.eyes),
                                       eyes_blocked(self._zone_count, self.diverts)):
            variables.setdefault(f"{self.name}.{key}", value)

    def _cover(self, index: int, pid, on: bool) -> None:
        """D1379: the body of the part with id *pid* starts (*on*) or stops lying across eye
        *index*."""
        parts = self._covering.setdefault(index, set())
        if on:
            parts.add(pid)
        else:
            parts.discard(pid)
        write_exposed(self, f"eye{index}.blocked", 1 if parts else 0)

    def _eye_levels(self, since: float | None = None, wake: bool = False) -> None:
        """D1390: each eye's level from the parts' noses, read again at every stage change and
        at the next time an edge falls under the stages as they are. A part covers eye k from
        its nose reaching the sensor (on it counts) until its nose is its item length past it.
        It only writes variables and schedules its own wake-ups: no point, no belt state."""
        now, tol = self.env.now, 1e-9
        # A2-R6 (final review I2): each part's reading is kept (`_eye_rows`: eyes it has
        # cleared, those it covers, its next edge) and read again only when it can change: a
        # stage change of it or of a part ahead (`since`: from that belt place back, since a
        # nose reads every part ahead), or its next edge falling due (within `tol`, as the full
        # re-read saw it, so the writes are the same). A nose only moves on, so an eye a
        # part's back has passed stays passed and is not read again.
        rows, along, order = self._eye_rows, self._eyes_along, self._order
        if along is None:
            along = self._eyes_along = sorted(self._eyes, key=lambda eye: eye.sensor)
        first = len(order) if wake else 0 if since is None else             bisect.bisect_left(order, since, key=_SEQ)
        for at, rec in enumerate(order):
            entity = rec[8]
            row = rows.get(entity.id)
            if row is None:
                row = rows[entity.id] = [0, (), math.inf]
            elif at < first and row[2] > now + tol:
                continue
            length = self.item_length(entity)
            cleared, covers, due = row[0], [], math.inf
            for k in range(cleared, len(along)):
                eye = along[k]
                front = self._nose_reach(rec, eye.sensor)
                if front > now + tol:
                    due = min(due, front)
                    break                           # the eyes beyond are reached later
                back = self._nose_reach(rec, eye.sensor + length)
                if back > now + tol:
                    covers.append(eye.index)
                    due = min(due, back)
                elif k == cleared:
                    cleared += 1
            row[:] = [cleared, covers, due]
        covering = {eye.index: set() for eye in self._eyes}
        due = math.inf
        for pid, row in rows.items():
            for index in row[1]:
                covering[index].add(pid)
            due = min(due, row[2])
        for index, parts in covering.items():
            self._covering[index] = parts
            write_exposed(self, f"eye{index}.blocked", 1 if parts else 0)
        if due < math.inf:
            self._eye_due = [t for t in self._eye_due if t > now + tol]
            if not any(t <= due + tol for t in self._eye_due):
                self._eye_due.append(due)
                timeout_at(self.env, due).callbacks.append(lambda _event: self._eye_levels(wake=True))

    def _nose_reach(self, rec, y: float) -> float:
        """D1390: when the nose of the part *rec* (the R63 rule: its own stage, never nearer
        than the parts ahead allow, `_ahead_due`) reaches *y* under the stages as they are: the
        clock once it has, inf when only a stage change can bring it there."""
        now = self.env.now
        t, x0, x1, duration = rec[3:7]
        if x1 is None or duration <= _EPS:
            own = now if (x0 if x1 is None else x1) >= y - _EPS else math.inf
        else:
            x = x0 + (x1 - x0) * min(1.0, (now - t) / duration)
            if x >= y - _EPS:
                own = now
            elif x1 >= y - _EPS:
                own = t + duration * (y - x0) / (x1 - x0)
            else:
                own = math.inf
        if own == math.inf:
            return own
        ahead, _by = self._ahead_due(rec[7], y)
        return max(own, math.inf if ahead is None else ahead)

    def _edges(self, point, entity) -> None:
        """D1379: *entity*'s front is at *point*: the eye edges that fall there for its length."""
        length = round(self.item_length(entity), 9)
        for index, on, size in point.edges:
            if size is None or size == length:
                self._cover(index, entity.id, on)

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
        now = self.env.now
        if stopped:
            self._halts += 1
            self._halted_since = now
            self._entry_owed = max(self._next_entry - now, 0.0)
        else:
            self._halted += now - self._halted_since
            if self._entry_owed > 0.0:
                # #2015: the slot still owed runs on belt time, not while the belt stood
                self._next_entry = now + self._entry_owed
                self._entry_owed = 0.0
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
        behind (D854). `Timeline` reads nothing else. The stage also counts in the
        dieback (A12): the part's last stage is where `_stood_now` finds its nose; a reversible
        belt reads its own ride instead."""
        if self._rev is None and stage != "off":
            rec = self._parts.get(entity.id)
            if rec is None:     # a part a state put back: entered as its stage says
                belt = getattr(entity, "_belt", None)
                entered = belt[2] if isinstance(belt, tuple) and belt[2] is not None \
                    else self.env.now
                self._seq += 1.0
                rec = self._parts[entity.id] = [self._occ(entity), entered, 0.0,
                                                0.0, 0.0, None, 0.0, self._seq, entity, None,
                                                False, None, None, 0.0, 0]
                self._order_add(rec)
            arrived = rec[10]
            rec[3:7] = [self.env.now, x0, x1, duration]
            rec[10] = stage in _ARRIVED
            if rec[10]:
                rec[11] = x0
            leaf = rec[12]
            if leaf is not None:                # speed review D: its leaf's summary is stale
                leaf[_LEAF_STALE] = True
                if rec[10] != arrived:
                    leaf[_LEAF_ARRIVED] += 1 if rec[10] else -1
            if rec[9] is not None:
                watch, rec[9] = rec[9], None
                watch.succeed()
            if self._nose_eyes:
                self._eye_levels(rec[7])
        self._trace_at(entity, stage, x0, x1, duration, ahead)

    def _trace_at(self, entity, stage: str, x0: float, x1: float | None = None,
                  duration: float = 0.0, ahead: int = -1) -> None:
        """The trace event alone: for a stage written only while tracing, which must not
        move a result."""
        self.emit("belt_stage", entity=entity, state=stage, x0=x0,
                  x1=x0 if x1 is None else x1, duration=duration, value=self._occ(entity),
                  ahead=ahead)

    def _on_belt(self, entity, entered: float, start: float = 0.0, ahead=None) -> None:
        """A12: *entity* gets on at *start* (a merge's place, else the entry) at *entered*;
        a merged part lands behind the part with id *ahead* (-1: none, it leads)."""
        if self._rev is not None:
            return
        if ahead is None:
            self._seq += 1.0
            seq = self._seq
        else:
            seq = self._join_seq(ahead)
            self._seq = max(self._seq, seq)
        old = self._parts.get(entity.id)
        if old is not None:                     # never reached: a part gets on once
            self._order_drop(old, self._order.index(old))
        rec = self._parts[entity.id] = [self._occ(entity), entered, start, entered, start,
                                        start, 0.0, seq, entity, None, False, None, None, 0.0, 0]
        self._order_add(rec)
        if self._nose_eyes:
            self._eye_levels(seq)

    def _order_add(self, rec) -> None:
        """*rec* joins the belt order (and the walk's index, speed review D)."""
        order = self._order
        bisect.insort(order, rec, key=_SEQ)
        idx = self._ahead
        if idx is False:
            idx = self._ahead = AheadIndex(self.speed) if self._sections or self._nose_eyes \
                else None
        if idx is None:
            return
        if not idx.accepts(rec[0], len(order)):
            # lengths the walk could not sum out of its order: the plain walk from now on
            self._ahead = None
            for other in order:
                other[12] = None
            return
        idx.added(order, bisect.bisect_left(order, rec[7], key=_SEQ), rec)

    def _order_drop(self, rec, at: int) -> None:
        """*rec*, at *at* in the belt order, leaves it."""
        if rec[12] is not None:
            self._ahead.removed(self._order, at, rec)
        del self._order[at]

    def _join_seq(self, ahead) -> float:
        """The place in belt order of a part merged behind the part with id *ahead* (-1: none,
        it leads): halfway to the next part, or past the last one."""
        seqs = sorted(rec[7] for rec in self._parts.values())
        lead = self._parts.get(ahead)
        if lead is None:
            return (seqs[0] if seqs else self._seq) - 1.0
        later = [x for x in seqs if x > lead[7]]
        return (lead[7] + later[0]) / 2 if later else lead[7] + 0.5

    def _left_belt(self, entity, entered_at: float, x: float) -> None:
        """A12: *entity* leaves at *x*: what it stood on the belt joins the dieback."""
        rec = self._parts.pop(entity.id, None)
        self._eye_rows.pop(entity.id, None)
        if rec is not None:
            at = bisect.bisect_left(self._order, rec[7], key=_SEQ)
            if not (at < len(self._order) and self._order[at] is rec):
                at = self._order.index(rec)     # never reached: one record per place
            self._order_drop(rec, at)
        if self._covering and not self._nose_eyes:
            # D1379: off the belt, its body covers no eye here (an eye nearer the end than the
            # part's length clears as the part leaves)
            for index, parts in self._covering.items():
                if entity.id in parts:
                    self._cover(index, entity.id, False)
        start = rec[2] if rec is not None else 0.0
        stood = (self.env.now - entered_at) - (x - start) / self.speed
        if stood > _EPS:
            self._dieback_extra += self._occ(entity) * stood
        if self._sections and rec is not None:
            # #2014: it rides on from *x* (out of the end or off sideways) as the rules behind
            # it take it, until its back is past *x*
            occ, now = rec[0], self.env.now
            # rides on for good (pruned once its back is past *x*), so it never stands short
            self._gone.append([occ, None, None, now, x, x + _FAR, _FAR / self.speed, rec[7],
                               None, None, False, None])
            if rec[9] is not None:
                watch, rec[9] = rec[9], None
                watch.succeed()
            if self._reshaped is not None and x < self._length - _EPS:
                reshaped, self._reshaped = self._reshaped, None
                reshaped.succeed()
        if self._nose_eyes:
            self._eye_levels(rec[7] if rec is not None else None)

    # -- #2014: a part never moves through the part ahead ----------------------------
    # Every rule above (a point's queue, a section's places and lag, the entry slot) times a
    # part from the moment the part ahead LEFT a place, as if it then rode on freely. A part
    # that left and stood again less than its own length on (packed, or held at the next
    # point) breaks that: the part behind would get through its body. So, on a belt with
    # points, a part also reaches a place only once the part directly ahead has its nose
    # (the R63 clamp over the stages, `_stood_now`) its occupied length past it, and moves
    # off a place only once that part is past it or moving on. Event-exact: each stage is a
    # stand or a ride at the belt speed, so the time it holds is read from the stages as they
    # are. A wait reads it again when the one record that decides it changes (a part standing
    # short), or at the time it gives (a stage changing later only makes that later), and
    # when a part leaves short of the end (`_reshaped`). So a stage change wakes only the
    # parts it holds up (R72), not every part waiting on the belt.
    def _watch(self, rec):
        """The event *rec*'s next stage change (or its leaving) fires."""
        if rec[9] is None:
            rec[9] = self.env.event()
        return rec[9]

    def _reshaped_event(self):
        if self._reshaped is None:
            self._reshaped = self.env.event()
        return self._reshaped

    def _ahead_due(self, seq: float, y: float, strict: bool = False):
        """(when, by): when the part directly ahead of belt place *seq* has its nose its
        occupied length past *y*, under the stages as they are (the clock when it has, a later
        time, or None: not before a stage changes), and the record deciding it (None when it
        holds now). The nose is the least, over each part j ahead, of j's stage place less the
        lengths from it back to *y*'s part, so each j must be that far past *y*; a part gone
        off the belt counts only for the parts behind it. *strict*: also not standing exactly
        there, so a part at *y* may move off it."""
        now = self.env.now
        gone = self._gone
        if gone:
            gone = self._gone = [g for g in gone if now - g[3] <= g[0] / self.speed + _EPS]
            gone = [g for g in gone if g[7] < seq]
            if len(gone) > 1:
                gone.sort(key=_SEQ)
        # A2 task 2: the hot loop of a packed belt, so it merges the gone parts in place (no copy
        # of the belt order) and reads `_stage_reach` inline; the same terms in the same order,
        # so the same floats (`test_a2_2_...` holds it to the `_stage_reach` walk)
        items = self._order
        i = start = bisect.bisect_left(items, seq, key=_SEQ) - 1
        g = len(gone) - 1 if gone else -1
        due, by, lengths = now, None, 0.0
        # A2 task 2: the walk also stops at a part that once arrived (`rec[11]`) at least
        # `_BOUND_MARGIN` past *z*: its nose is no nearer since, so, as at the break below, the
        # parts beyond it are past their terms now. Not on a belt with merges, where a merging
        # part can still land on a part (#2012, #2016 with priority "merge")
        bounded = not self._merges
        # speed review D: the walk steps over a leaf of the index (a run of parts next to each
        # other in belt order) when the index shows each of its terms plainly holds, the
        # lengths summed as the walk would have summed them. `edge` is where the next leaf
        # begins, going on; -1 when the index is off or paused (it did not pay: the walk is
        # then the plain one). The first leaf is walked part by part when it holds the
        # waiting part or a part behind it
        edge, idx = -1, self._ahead or None
        if idx is not None:
            if idx.pause:
                idx.pause -= 1
                idx = None
            elif idx.score < 0:
                idx.pause, idx.score, idx = _belt_ahead._PAUSE, _belt_ahead._SCORE_CAP, None
            elif i >= 0:
                idx.score -= _belt_ahead._CALL_COST
                leaf = items[i][12]
                if leaf is None:
                    pass
                elif i + 1 < len(items) and items[i + 1][12] is leaf:
                    if not leaf[_LEAF_FULL]:        # its front: [14] counts places in a leaf
                        edge = i - (items[i][14] - leaf[_LEAF_FIRST]) - 1
                else:
                    edge = i
        while True:
            # a gone part first at a tie in belt place: the stable sort the walk merged them by
            if g >= 0 and (i < 0 or gone[g][7] >= items[i][7]):
                rec = gone[g]
                g -= 1
                z = y + lengths + rec[0]            # gone: it takes no place of its own
            elif i > edge:
                rec = items[i]
                i -= 1
                lengths += rec[0]
                z = y + lengths
            elif i >= 0:                            # the back of a leaf
                rec = items[i]
                leaf = rec[12]
                bottom = i - leaf[0] + 1
                edge = bottom - 1
                # (a leaf with a part standing where it arrived ends the walk)
                if not leaf[_LEAF_ARRIVED] and \
                        _belt_ahead._MIN_SKIP <= leaf[0] <= _belt_ahead._MAX_SKIP and \
                        (g < 0 or gone[g][7] < items[bottom][7]):
                    after = idx.clear(items, bottom, i, leaf, y, lengths, start - bottom + 1, now,
                                      strict, bounded)
                    if after is not None:
                        i, lengths = edge, after
                        continue
                i -= 1
                lengths += rec[0]
                z = y + lengths
            else:
                break
            # most parts ahead are well past: where the stage started or ends is enough
            ze, held = z + _EPS, True
            x0, x1 = rec[4], rec[5]
            if x0 > ze or (x1 is not None and x1 > ze and now >= rec[3] + rec[6]):
                pass
            elif x1 is None or rec[6] <= _EPS:      # a stand
                x = x0 if x1 is None else x1
                if not (x > ze or (not strict and x >= z - _EPS)):
                    return None, rec
            else:                                   # a ride
                t, duration = rec[3], rec[6]
                r = (now - t) / duration
                x = x0 + (x1 - x0) * (r if r < 1.0 else 1.0)
                if x > ze or (x >= z - _EPS and (not strict or now < t + duration - _EPS)):
                    pass
                elif z < x1 - _EPS or (not strict and z <= x1 + _EPS):
                    held = False
                    at = t + duration * (z - x0) / (x1 - x0)
                    if at > due:
                        due, by = at, rec
                else:
                    return None, rec
            if rec[10]:
                # it stands where it arrived, no nearer than that: every part beyond it is at
                # least its own length further on, so their terms are no smaller than its own
                break
            if held and bounded and rec[11] is not None and z <= rec[11] - _BOUND_MARGIN:
                break
        return due, by

    def _late(self, seq: float, y: float, strict: bool = False):
        """`_ahead_due` when it does not hold yet, else False: what a caller reads before it
        builds a wait, so a part that need not wait yields nothing and a belt whose parts never
        catch a standing part keeps its event order."""
        due, by = self._ahead_due(seq, y, strict)
        return False if due is not None and due - self.env.now <= _EPS else (due, by)

    def _wake(self, late) -> list:
        """The events a wait on *late* (what `_late` gave) reads the rule again at."""
        due, by = late
        if due is None:
            return [self._reshaped_event(), self._watch(by)]
        return [self._reshaped_event(), self.env.timeout(due - self.env.now)]

    def _behind(self, seq: float, y: float, strict, late, stand=None):
        """Generator: wait, from what `_late` gave, until `_ahead_due` holds; *stand* runs
        first (the stage the part waits in)."""
        if stand is not None:
            stand()
        while late is not False:
            yield simpy.AnyOf(self.env, self._wake(late))
            late = self._late(seq, y, strict)

    def _seq_of(self, entity) -> float | None:
        rec = self._parts.get(entity.id)
        return None if rec is None else rec[7]

    def _stood_now(self) -> float:
        """A12 (R63): sum over the parts on the belt now of occupied length x time stood so
        far. A part's nose is where its last stage puts it, but never nearer than the part
        ahead's occupied length behind that part's nose: a packed part's stage names the point
        it packs at, and a part riding freely toward a queue has physically joined it. Exact
        while each part stands or rides at the belt speed; in a zone release's cascade a packed
        part's stage can name the end while its zone has not restarted yet."""
        if self._rev is not None:
            return self._rev.stood_now()
        now, speed = self.env.now, self.speed
        total, ahead = 0.0, None
        for rec in sorted(self._parts.values(), key=lambda rec: rec[7]):
            t, x0, x1, duration = rec[3:7]
            if x1 is None:
                x = x0
            elif duration > _EPS:
                x = x0 + (x1 - x0) * min(1.0, (now - t) / duration)
            else:
                x = x1
            if ahead is not None:
                x = min(x, ahead[0] - ahead[1])
            ahead = (x, rec[0])
            stood = (now - rec[1]) - (x - rec[2]) / speed
            if stood > _EPS:
                total += rec[0] * stood
        return total

    def dieback_area(self, t_end: float) -> float:
        """A12: belt length x time held by standing parts in the window: what the parts that
        left stood, less what the ones on the belt at the warm-up had stood then, plus what the
        ones on it now have stood (read at the summary, the window's end)."""
        return self._dieback_extra + self._stood_now()

    def _left_reversible(self, entity, entered_at: float, travelled: float) -> None:
        """A12 (R64): a part leaves a reversible belt having ridden *travelled* both ways."""
        stood = (self.env.now - entered_at) - travelled / self.speed
        if stood > _EPS:
            self._dieback_extra += self._occ(entity) * stood

    def _ride(self, distance_time: float, entity=None, x0: float = 0.0, stage: str = "ride"):
        """Generator: spend *distance_time* of BELT time, pausing while stopped,
        from *x0*; traced for *entity* when given (D734)."""
        x1 = x0 + distance_time * self.speed
        if entity is not None:
            self._at(entity, stage, x0, x1, distance_time)
            if self._slug_n and stage == "ride":
                self._belt_release._slug_ride(entity, x0, x1)
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
    # it ends or None, when it entered or None, the point it rides to or stands at or None),
    # for a state (D429, D921, the D383 pattern): before a timeout, `until` is `now + delay`,
    # the float SimPy schedules it at. `entity._section` is (index, leave event) of the
    # section place it holds and `entity._ask` the request it queues with (D923's order).
    # A state puts a part back by entering these steps at its stage (`resume`).
    def self_process(self, entity):
        entity._belt = ("cell", None, None, None)
        if self._rev is not None:
            # A2-R5: a reversible belt's entrant takes its cells as it boards, so one waiting
            # while the belt runs backward holds none a part crossing a joint needs.
            yield from self._rev.board(entity)
            return
        # A free cell on the belt. The upstream machine is NOT released yet:
        # the part is only off it once it is physically on the belt, which is
        # after the stopped-belt wait and the entry rhythm below (PHASE4 T7).
        cell = yield from self.space.take(entity, accepts=False)
        yield from self._enter(entity, cell)

    def _enter(self, entity, cell, request=None):
        """Generator: a part with a belt cell gets on: room in the first section (*request*:
        its request, made by `restore`), the stopped belt, the entry slot."""
        # D1355: the part behind keeps this one's occupied length (the spacing, untyped)
        spacing_time = self._occ(entity) / self.speed
        section, not_before = None, self.env.now
        if self._sections:
            # Room in the first section (D732): parts packed back to the entry stop it.
            first = self._sections[0]
            if request is None:
                request = first.request(self.cells_of(entity))
            entity._belt, entity._ask = ("entry", None, None, None), request
            yield request
            due, lag = first.due_lag()
            if due is not None:
                if not due.triggered:
                    yield due
                not_before = due.value + lag            # D853: boarding is its start
            section = (request, first.enter(self.env, self.cells_of(entity)))
            entity._section = (0, section[1])
        while not self.accumulating and self._stopped:
            yield self._resume_event                # a stopped belt takes nothing on
        # Reserve the entry slot BEFORE sleeping on it: two entities that both
        # read a slot 2 s away would otherwise both take it and ride side by
        # side, which is exactly what spacing forbids.
        slot = max(self._next_entry, self.env.now, not_before)
        self._next_entry = slot + spacing_time
        if slot - self.env.now > _EPS:
            halts, asked, ran = self._halts, self.env.now, self._belt_time()
            # not `slot`: the float the timeout lands on (a state taken again matches)
            entity._belt = ("slot", self.env.now + (slot - self.env.now), None, None)
            yield self.env.timeout(slot - self.env.now)
            if not self.accumulating and (self._halts != halts or self._stopped):
                # #2015: the belt stood meanwhile. The slot is belt travel, so the part boards
                # once the running belt has carried the one ahead the rest of it; a belt that
                # never stops keeps its one plain sleep, and with it its event order.
                yield from self._entry_travel(slot - asked - (self._belt_time() - ran))
        yield from self._board(entity, cell, section)

    def _belt_time(self) -> float:
        """#2015: how long a non-accumulating belt has run so far."""
        stood = self.env.now - self._halted_since if self._stopped else 0.0
        return self.env.now - self._halted - stood

    def _entry_travel(self, owed: float):
        """Generator, #2015: a part waits at a non-accumulating belt's entry until the running
        belt has run *owed* more."""
        while True:
            if self._stopped:
                yield self._resume_event
                continue
            if owed <= _EPS:
                return
            ran = self._belt_time()
            yield from self._ride(owed)
            owed -= self._belt_time() - ran

    def _check_entry(self, entity) -> None:
        """Refuse, as it gets on the belt, an item longer than the cell of an untyped belt
        (it would overlap the next one; on a typed belt it takes more cells, D1355) or one
        whose back-eye point was left out because it did not fit the belt (E_CONVEYOR_EYE)."""
        length = self.item_length(entity)
        if not self.type and length > self.cell_length + _EPS:
            raise RuntimeError(f"Conveyor '{self.name}': a {entity.e_type} item is {length:g} "
                               f"long, longer than the cell {self.cell_length:g} "
                               f"(E_CONVEYOR_EYE).")
        for eye in self._eyes:
            if round(length, 9) in eye.dropped:
                raise RuntimeError(f"Conveyor '{self.name}': a {entity.e_type} item is {length:g} "
                                   f"long and eye {eye.index}'s back edge for it is not on the "
                                   f"belt or shares a point (E_CONVEYOR_EYE).")

    def _board(self, entity, cell, section=None):
        yield from self._belt_zones._zone_hold(entity)
        if self._sections:
            # #2014: boarding puts the front at the entry: the last part on is its length on
            due = self._late(math.inf, 0.0)
            if due is not False:
                entity._belt = ("entry", None, None, None)
                yield from self._behind(math.inf, 0.0, False, due)
        self._check_entry(entity)
        accept(entity)                              # on the belt: the machine is free
        entered_at = self.env.now
        self.entered += 1
        self.occupancy.add(1, entered_at)
        if self._merges:
            self._on.add(entity.id)
        if self._slug_n:
            self._belt_release._slug_join(entity)
        self._on_belt(entity, entered_at)
        yield from self._carry(entity, cell, entered_at, section=section)

    def _carry(self, entity, cell, entered_at, end=None, section=None, slot=0, start=0.0,
               resume=None):
        """The ride to the end (until *end*, put back by a state), then the way off.
        *section*: the cells held in the first section of a belt with stops; *slot* and
        *start*: the first point still ahead and where the part is (a merged part);
        *resume*: where a state put it back at that point (D918)."""
        length = self._length
        if end is None:
            ride = (length - start) / self.speed
            if self._points:
                section, start = yield from self._through_points(entity, section, cell,
                                                                 entered_at, slot, start, resume)
                if start is None:
                    return                          # diverted off the belt (D851)
                ride = (length - start) / self.speed
            entity._belt = ("ride", self.env.now + ride, entered_at, None)
            if self._zone_count:
                entry = [entity, self.env.now + ride, None, None, self.env.now + ride]
                self._belt_zones._zone_join(entry)
                self._zone_chain.append(entry)
                if self._zone_eyes:
                    self.env.process(self._belt_zones._zone_eye_run(entry, self.env.now))
            yield from self._ride(ride, entity, start)
        else:
            entity._belt = ("ride", end, entered_at, None)
            self._at(entity, "ride", length - (end - self.env.now) * self.speed, length,
                     end - self.env.now)
            yield timeout_at(self.env, end)
        ahead = self._prev_clear
        clear = self.env.event()
        self._prev_clear = clear
        yield from self._leave(entity, cell, entered_at, ahead, clear, section=section)

    def _through_points(self, entity, section, cell, entered_at, slot=0, start=0.0, resume=None):
        """Generator: ride from *start* through every stop, divert and merge from *slot*
        on (D732, D851, D854), the first at the stage *resume* gives; returns the cells held
        in the last section and where the last point is, or (None, None) once a divert has
        taken the part off the belt."""
        at = start
        for point in self._points[slot:]:
            if isinstance(point, _Merge):
                section = yield from self._pass_merge(entity, point, at, section, entered_at,
                                                      resume)
            else:
                section, off = yield from self._pass_point(entity, point, at, section, cell,
                                                           entered_at, resume)
                if off:
                    return None, None
            resume, at = None, point.at
        return section, at

    def _wait_until(self, entity, stage, until, entered_at, slot, x0, x1=None):
        """Generator: a stage put back by a state, timed to the very instant it ended (D921)."""
        entity._belt = (stage, until, entered_at, slot)
        self._at(entity, stage, x0, x1, until - self.env.now)
        yield timeout_at(self.env, until)

    def _pass_point(self, entity, point, at, section, cell, entered_at, resume=None):
        """Generator: a part rides to a stop or a divert, packs behind the part there, waits
        for room in the section after it, and dwells, or is diverted or passes (D732, D851).
        Returns (the cells it then holds, True when diverted off the belt). *resume*: the
        stage a state put it back at; a wait for an event starts again at the clock."""
        slot = point.slot
        stage = resume["stage"] if resume else "ride"
        if stage == "ride":
            if resume is None:
                distance = (point.at - at) / self.speed
                entity._belt = ("ride", self.env.now + distance, entered_at, slot)
                yield from self._ride(distance, entity, at)
            else:
                until = resume["until"]
                yield from self._wait_until(entity, "ride", until, entered_at, slot,
                                            point.at - (until - self.env.now) * self.speed,
                                            point.at)
            ahead, clear = point.clear, self.env.event()
            point.clear = clear
            stage = "packed"
        else:
            ahead, clear = resume["ahead"], resume["clear"]
        at = point.at
        if stage == "packed":
            if self.accumulating:
                if ahead is not None and not ahead.triggered:
                    self._at(entity, "packed", at)  # Timeline puts it behind the one ahead
                    entity._belt = ("packed", None, entered_at, slot)
                    if self._handshakes:
                        self._stands(entity)
                    yield ahead
                    catch_up = point.occ / self.speed      # D1355: the gap the one ahead left
                else:
                    catch_up = point.left + point.occ / self.speed - self.env.now
                if catch_up > _EPS:
                    # held a spacing behind the part ahead until it left: the rest of the way
                    entity._belt = ("move", self.env.now + catch_up, entered_at, slot)
                    yield from self._ride(catch_up, entity, at - catch_up * self.speed, "move")
            stage = "due"
        elif stage == "move":
            until = resume["until"]
            yield from self._wait_until(entity, "move", until, entered_at, slot,
                                        at - (until - self.env.now) * self.speed, at)
            stage = "due"
        if stage == "due" and self.accumulating:
            yield from self._reach_section(entity, self._sections[slot + 1], at, entered_at,
                                           slot)
        request = resume.get("request") if resume and stage == "blocked" else None
        if not isinstance(point, _Stop):        # a divert, an eye or a robot's meeting point
            if isinstance(point, _MeetPoint) and getattr(entity, "_meet", None) is not None:
                yield from self._meet_off(entity, point, cell, entered_at, section, clear)
                return None, True
            if isinstance(point, _Eye):
                # D1347: once, as the front is due; of a back eye's points, the item's own
                if stage == "due" and point.fires_for(self.item_length(entity)):
                    self._sense(point, entity)
            elif isinstance(point, _Divert) and point.intercept:
                if stage == "due" and (yield from self._calls(point, entity)):
                    # D1343: the call. The robot sets off for the meeting point; the part
                    # rides on, with no token: it is on the belt until the robot has it.
                    entity._meet = beltpick.Meet(self.env)
                    self.forward(entity, point.exit)
            elif isinstance(point, _Divert) and point.head is not None:
                # A7: a routing rule, chosen at the group's first divert (D1359)
                takes = stage == "push"
                if stage == "due":
                    takes = yield from self._routes_it(entity, point, entered_at)
                if takes:
                    yield from self._divert(entity, point, cell, entered_at, section, clear,
                                            resume if stage == "push" else None)
                    return None, True
            elif isinstance(point, _Divert) and (
                    stage == "due" and self._diverts_it(point, entity) or stage == "push"):
                yield from self._divert(entity, point, cell, entered_at, section, clear,
                                        resume if stage == "push" else None)
                return None, True
            section = yield from self._room_ahead(entity, point, section, None, entered_at,
                                                  request)
            point.left, point.occ = self.env.now, self._occ(entity)
            clear.succeed()
            return section, False
        stop = point
        if stage == "due":
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
                entity._belt = ("wait", None, entered_at, slot)
                yield from acquire(self, entity, stop.resource)
                stop.blocked.set(0.0, self.env.now)
                stop.waits.add(self.env.now - asked)
            stage = "dwell?"
        elif stage == "wait":       # put back asking: `restore` set its levels (D918)
            asked = float(resume["since"])
            self._at(entity, "wait", at)
            entity._belt = ("wait", None, entered_at, slot)
            yield from acquire(self, entity, stop.resource)
            stop.blocked.set(0.0, self.env.now)
            stop.waits.add(self.env.now - asked)
            stage = "dwell?"
        if stage == "dwell?":
            duration = self._point_time(stop, entity, "stops")
            self._at(entity, "dwell", at, duration=duration)
            if duration > 0:
                entity._belt = ("dwell", self.env.now + duration, entered_at, slot)
                yield self.env.timeout(duration)
        elif stage == "dwell":
            yield from self._wait_until(entity, "dwell", resume["until"], entered_at, slot, at)
        if stage != "blocked" and stop.resource:
            release(self, entity, stop.resource)
        section = yield from self._room_ahead(entity, stop, section, stop.blocked, entered_at,
                                              request)
        stop.busy.set(0.0, self.env.now)
        stop.left, stop.occ = self.env.now, self._occ(entity)
        clear.succeed()
        if not self.accumulating:
            self._hold(-1)
        return section, False

    def _sense(self, eye, entity) -> None:
        """An eye fires for *entity* (D1347): count, trace, signal, then its assignments, in
        that order, so an item's `on_signal` can write what the same firing stamps."""
        if self.expose:
            # D1380 (R68): the eye's own event, exact in a packed queue since #2014 (D1381)
            name = f"{self.name}.eye{eye.index}.count"
            write_exposed(self, f"eye{eye.index}.count", self.ctx.variables.get(name, 0) + 1)
        self._eyes[eye.index].fired += 1
        self.emit("eye", entity=entity, state=eye.edge, value=float(eye.index))
        if eye.signal:
            self.ctx.raise_signal(eye.signal)
        if eye.rows:
            apply_rows(self, entity, eye.rows, f"Block '{self.name}': eyes[{eye.index}].assign")
        if self._reach:
            self._reached(entity)               # R5: its first eye here

    def _room_ahead(self, entity, point, section, blocked=None, entered_at=None, request=None):
        """Generator: a part leaving *point* on an accumulating belt takes a cell in the
        section after it (waiting there for one), and gives back the one it had; returns
        the cells it now holds. *blocked*: the point's blocked share, when it counts.
        *request*: the one a state made for it, in the order of the section's queue."""
        if section is None:
            return None
        nxt = self._sections[point.slot + 1]
        ahead_room = nxt.request(self.cells_of(entity)) if request is None else request
        waited = not ahead_room.triggered
        if waited:
            self._blocked_at(entity, point, entered_at, ahead_room, blocked)
            yield ahead_room
        seq = self._seq_of(entity)
        due = False if seq is None else self._late(seq, point.at, True)
        if due is not False:
            # #2014: a place in the section is not yet room: the part ahead may stand across it
            if not waited:
                self._blocked_at(entity, point, entered_at, ahead_room, blocked)
                waited = True
            yield from self._behind(seq, point.at, True, due)
        if waited and blocked is not None:
            blocked.set(0.0, self.env.now)
        self._leave_section(point.slot, section)
        out = nxt.enter(self.env, self.cells_of(entity))
        entity._section = (point.slot + 1, out)
        return ahead_room, out

    def _blocked_at(self, entity, point, entered_at, request, blocked) -> None:
        """*entity* stands at *point*, done there, until it may move on (`_room_ahead`)."""
        self._at(entity, "blocked", point.at)
        entity._belt, entity._ask = ("blocked", None, entered_at, point.slot), request
        if blocked is not None:
            blocked.set(1.0, self.env.now)
        if self._handshakes:
            self._stands(entity)

    def _routes_it(self, entity, divert, entered_at):
        """Generator: does routed *divert* take *entity* (A7, D1359)? At the group's first divert
        the rule picks the exit, holding the part there while every exit is full; the chosen
        divert takes it and the others of the group let it pass."""
        head = divert.head
        if divert is head:
            route = head.route
            index = route.choose(entity)
            if index is None:
                index = yield from self._route_hold(entity, head, route, entered_at)
            route.routed(entity, index)
            choice = head.members[index]
            if choice is not None:                  # None: `out`, past every divert (R28)
                self._route_to[entity.id] = choice
            held = self._route_held.pop(entity.id, None) if self._route_held else None
            if held is not None and choice is not head:
                head.waits.add(held)                # M3: the wait of the divert it stood at
            elif held is not None:
                self._route_held[entity.id] = held  # joins its wait there, one entry
        if self._route_to.get(entity.id) is divert:
            del self._route_to[entity.id]
            # A11: the exit chosen at the head may be one the rule would not choose now
            route_taken(self, head.route.rule, entity, head.members.index(divert))
            return True
        divert.passed += 1
        return False

    def _route_hold(self, entity, divert, route, entered_at):
        """Generator: every exit of *divert*'s rule is at its limit. The part keeps its cell at
        the divert and the belt behind packs as at a stop (a non-accumulating belt stops); the
        rule runs again each time an exit's count drops, never polled (D675). Returns the
        exit; the time held is a wait of this divert, the part's own if it leaves here."""
        since = self.env.now
        if not self.accumulating:
            self._hold(1)
        self._divert_waits += 1                 # held downstream: the deadlock checks read it
        divert.busy.set(1.0, since)
        divert.blocked.set(1.0, since)
        held = route.held(1)
        self._at(entity, "hold", divert.at)
        entity._belt = ("hold", None, entered_at, divert.slot)
        check_deadlock(self)
        index = None
        while index is None:
            yield route.freed()
            index = route.choose(entity)
        t = self.env.now
        self._divert_waits -= 1
        divert.blocked.set(0.0, t)
        divert.busy.set(0.0, t)
        route.held(-1, held)
        if not self.accumulating:
            self._hold(-1)
        self._route_held[entity.id] = t - since
        return index

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
        if divert.when_full == "pass" and not self._room_at(divert, entity):
            divert.skipped += 1
            return False
        return True

    def _calls(self, divert, entity):
        """Generator: does intercept *divert* call its robot for *entity* (D1343)? Its
        condition, then a robot ready or an `on_late` that waits. Unless the part always
        waits, readiness is read once every event of this instant is through, so a robot freed
        or taken in the very instant of the call is read the same whatever their order."""
        if not self._diverts_it(divert, entity):
            return False
        if divert.on_late == "wait":
            return True
        yield beltpick.InstantEnd(self.env)         # #2001
        if self.next_process[divert.exit].ready_now() or self._waits_late(divert, entity):
            return True
        divert.skipped += 1
        return False

    def _waits_late(self, divert, entity) -> bool:
        """Does *entity* wait at the meeting point for a busy robot (D1343)? `on_late`:
        "wait", "pass", or an expression read for the part, true meaning wait."""
        text = divert.on_late
        if text in ("wait", "pass"):
            return text == "wait"
        try:
            return bool(evaluate_expression(self, text, entity))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(
                f"Block '{self.name}': diverts[{divert.index}].on_late: {exc}") from None

    def _meet_off(self, entity, point, cell, entered_at, section, clear):
        """Generator: a called part's front is at the meeting point (D1343). It leaves the
        belt when its robot is there too; until then it stands, the parts behind pack, and
        a non-accumulating belt stops. A robot due within a nanosecond is no stand."""
        divert, meet = point.divert, entity._meet
        meet.part.succeed()
        arrived, stands = self.env.now, False
        if not meet.robot.triggered:
            due = meet.robot_at
            stands = due is None or due - arrived > _EPS
            if stands:
                if not self.accumulating:
                    self._hold(1)
                self._divert_waits += 1
                divert.busy.set(1.0, arrived)
                divert.blocked.set(1.0, arrived)
                self._at(entity, "handover", point.at)
                entity._belt = ("handover", None, entered_at, point.slot)
                check_deadlock(self)
            yield meet.robot
            if stands:
                self._divert_waits -= 1
                divert.blocked.set(0.0, self.env.now)
        entity._meet = None
        self._off(entity, point, divert, cell, entered_at, section, clear,
                  self.env.now - arrived if stands else 0.0, stands)

    def _off(self, entity, point, divert, cell, entered_at, section, clear, waited, held):
        """A part leaves the belt sideways at *point*: *divert* itself (D851) or its robot's
        meeting point (D1343), after *waited* there; *held*: it stopped a non-accumulating
        belt meanwhile."""
        t = self.env.now
        divert.waits.add(waited)
        divert.diverted += 1
        divert.busy.set(0.0, t)
        self.exited += 1
        self.occupancy.add(-1, t)
        self.transit.add(t - entered_at)
        self.space.give_back(cell)
        if section is not None:
            self._leave_section(point.slot, section)
        point.left, point.occ = t, self._occ(entity)
        clear.succeed()
        if held and not self.accumulating:
            self._hold(-1)
        self._on.discard(entity.id)
        if self._slug_n:
            self._belt_release._slug_leave(entity)
        self._left_belt(entity, entered_at, point.at)
        self._at(entity, "off", point.at)
        if self._reach or self._offs:
            self._reached(entity, off=True)     # R5, R26: off this belt by a divert

    def _room_at(self, divert, entity) -> bool:
        """The "pass" room check (D852) on the block divert's link goes straight to."""
        target = self.next_process[divert.exit]
        room = getattr(target, "room_now", None)
        if room is not None:
            return room(entity)
        space = getattr(target, "space", None)
        if space is None or space.capacity <= 0:
            raise RuntimeError(
                f"Conveyor '{self.name}': divert {divert.index} passes when full, so its link "
                f"must go straight to a CONVEYOR, a BUFFER or a station with queue_capacity; "
                f"'{getattr(target, 'name', target)}' has no places (E_CONVEYOR_DIVERT).")
        return space.content.level < space.capacity and not space.waiting

    def _divert(self, entity, divert, cell, entered_at, section, clear, resume=None):
        """Generator: push *entity* off at *divert* (its `time`, holding its cell), hand it
        over on the divert's link with the D78 token -- a full target keeps it at the
        divert -- then take it off the belt (D851, D852). *resume*: put back by a state
        pushing (`push`) or waiting for its target (`handover`, whose levels `restore` set)."""
        at, stage = divert.at, resume["stage"] if resume else None
        if stage is None:
            if not self.accumulating:
                self._hold(1)
            divert.busy.set(1.0, self.env.now)
            duration = self._point_time(divert, entity, "diverts")
            self._at(entity, "divert", at, duration=duration)
            if duration > 0:
                entity._belt = ("push", self.env.now + duration, entered_at, divert.slot)
                yield self.env.timeout(duration)
        elif stage == "push":
            yield from self._wait_until(entity, "push", resume["until"], entered_at,
                                        divert.slot, at)
        asked = self.env.now if stage != "handover" else float(resume["since"])
        token = arm(self, entity)
        # D430, D918: when it entered, where and since when it waits, for a state
        token.entered, token.point, token.asked = entered_at, divert.slot, asked
        if stage != "handover":
            self.forward(entity, divert.exit)
        if not token.triggered:
            if stage != "handover":
                self._divert_waits += 1
                divert.blocked.set(1.0, asked)
            self._at(entity, "handover", at)
            check_deadlock(self)
            yield token
            self._divert_waits -= 1
            divert.blocked.set(0.0, self.env.now)
        held = self._route_held.pop(entity.id, 0.0) if self._route_held else 0.0
        self._off(entity, divert, divert, cell, entered_at, section, clear,
                  self.env.now - asked + held, True)

    # -- merges (D854) -------------------------------------------------------
    def wire_merges(self, blocks: dict) -> None:
        """Called once every link is wired, before `blocking.analyse`: each merge's source
        links to a `_MergeEntry` instead of the belt, so its parts join at the merge."""
        for index, (_at, source, _priority) in enumerate(self.merges):
            block = blocks.get(source)
            nexts = getattr(block, "next_process", None) or []
            if not any(real_target(target) is self for target in nexts):
                raise RuntimeError(f"Conveyor '{self.name}': merge {index} names '{source}', "
                                   f"which does not link to it (E_CONVEYOR_MERGE).")
            block.next_process = [_MergeEntry(self, index) if target is self else target
                                  for target in nexts]
            for link in nexts:              # D1369: a part crosses an area link to the merge
                if link is not self and real_target(link) is self:
                    link.next_process = [_MergeEntry(self, index)]

    def _may_join(self, merge) -> bool:
        """A merging part may join now: no main part within a spacing of the point, a
        spacing since the last part passed it, and room in the section after it."""
        now = self.env.now
        merge.ahead_due, merge.behind = False, False
        if merge.claims or now < merge.left + merge.occ / self.speed - _EPS:
            return False
        if merge.riders and merge.priority != "merge":
            # D1355: the next main part is at least the merging part's length from the point
            # (it only comes closer: a claim ending wakes the merging part again)
            there, claim = merge.riders[0]
            if claim + max(there - now, 0.0) * self.speed < merge.joiner_occ - _EPS:
                return False
        section = self._sections[merge.slot + 1]
        if section.full() or section.pool.queue:
            return False
        due, lag = section.due_lag()
        if not (due is None or (due.triggered and due.value + lag <= now + _EPS)):
            return False
        # #2014: and the part it lands behind is its length past the point
        seq = self._join_seq(self._merge_ahead(merge))
        if merge.priority != "merge" and self._in_the_way(merge, merge.at - merge.joiner_occ):
            # #2016: nor may it land on the main part behind it, which `claims` sees only
            # within a spacing of the point: from a point nearer than that, it has its nose
            # in the merging part's place before it gets there. It passes first (priority
            # "belt"), or leaves the belt by a divert; either wakes the merging part
            merge.behind = True
            return False
        late = self._late(seq, merge.at)
        if late is False:
            return True
        merge.ahead_due = late                  # what `_join_wakeup` waits for
        return False

    def _merge_ahead(self, merge):
        """#2016: the id of the part a part merging at *merge* lands behind (-1: none, it
        leads): the rearmost part past the point, by the section it holds. `passers` would miss
        a part that joined at a merge further on, which never passed this one."""
        slot = merge.slot
        for rec in reversed(self._order):
            section = getattr(rec[8], "_section", None)
            if section is not None and section[0] > slot:
                return rec[8].id
        return -1

    def _in_the_way(self, merge, back: float) -> bool:
        """#2016: the main part right behind *merge*'s point (the front-most part not past it)
        has its nose (the R63 clamp) past *back*, so a part landing there with its back at
        *back* would land on it."""
        order, slot = self._order, merge.slot
        k = next((i for i, rec in enumerate(order)
                  if getattr(rec[8], "_section", (slot,))[0] <= slot), None)
        if k is None:
            return False
        rec, now = order[k], self.env.now
        x = _stage_x(rec, now)
        if x <= back + _EPS:
            return False
        items = order[:k]                       # the clamp of the parts ahead of it
        gone = [g for g in self._gone if g[7] < rec[7]]
        if gone:
            items = sorted(items + gone, key=_SEQ)
        lengths = 0.0
        for ahead in reversed(items):
            if ahead[1] is None:                # gone: it takes no place of its own
                x = min(x, _stage_x(ahead, now) - lengths - ahead[0])
            else:
                lengths += ahead[0]
                x = min(x, _stage_x(ahead, now) - lengths)
            if x <= back + _EPS:
                return False
            if ahead[10]:                       # where it arrived: exact, as in `_ahead_due`
                break
        return True

    def _join_wakeup(self, merge):
        """(the events after which a waiting merging part looks again, the time the earliest
        of its timers lands or None: what a state carries, D921)."""
        now, section = self.env.now, self._sections[merge.slot + 1]
        events, until = [merge.changed], None
        later = merge.left + merge.occ / self.speed - now
        if later > _EPS:
            events.append(self.env.timeout(later))
            until = now + later
        due, section_lag = section.due_lag()
        if due is not None:
            if not due.triggered:
                events.append(due)
            elif due.value + section_lag - now > _EPS:
                lag = due.value + section_lag - now
                events.append(self.env.timeout(lag))
                until = now + lag if until is None else min(until, now + lag)
        if merge.behind:                        # #2016: until the part behind passes or leaves
            events.append(self._reshaped_event())
        late = merge.ahead_due
        if late is not False:                   # #2014: the part ahead is not yet past it
            events.extend(self._wake(late))
            if late[0] is not None:
                until = late[0] if until is None else min(until, late[0])
        return simpy.AnyOf(self.env, events), until

    def _resumed_wakeup(self, merge, until):
        """The first wake-up of a merging part a state put back waiting (D921): its carried
        timer, not one timed again from the clock (an ulp away), then as `_join_wakeup`."""
        events = [merge.changed]
        if until is not None:
            events.append(timeout_at(self.env, until))
        due, _lag = self._sections[merge.slot + 1].due_lag()
        if due is not None and not due.triggered:
            events.append(due)
        return simpy.AnyOf(self.env, events)

    def _fire(self, merge, name: str) -> None:
        fired = getattr(merge, name)
        setattr(merge, name, self.env.event())
        fired.succeed()

    def _pass_merge(self, entity, merge, at, section, entered_at=None, resume=None):
        """Generator: a main part goes past a merge (D854). It rides to a spacing before
        the point, keeps a spacing behind the part that passed last, gives way to a
        merging part that may join (always, with priority "merge"), then claims the point
        while its front is within a spacing of it, and passes. *resume*: the stage a state
        put it back at (`claim`, `due`, `move`, `blocked` hold a claim `restore` counted)."""
        slot = merge.slot
        # D1355: a main part claims the point within its own occupied length (the spacing)
        pre = max(merge.at - self._occ(entity), at)
        stage = resume["stage"] if resume else "ride"
        if stage == "ride":
            if self.type and resume is None:
                # where it is, for a merging part longer than its claim (`_may_join`)
                merge.riders.append((self.env.now + (pre - at) / self.speed,
                                     merge.at - pre))
            if resume is not None:
                until = resume["until"]
                yield from self._wait_until(entity, "ride", until, entered_at, slot,
                                            pre - (until - self.env.now) * self.speed, pre)
            elif pre - at > _EPS:
                distance = (pre - at) / self.speed
                entity._belt = ("ride", self.env.now + distance, entered_at, slot)
                yield from self._ride(distance, entity, at)
            ahead, clear = merge.clear, self.env.event()
            merge.clear = clear
            stage = "packed"
        else:
            ahead, clear = resume["ahead"], resume["clear"]
        if stage == "packed":
            if ahead is not None and not ahead.triggered:
                self._at(entity, "packed", pre)
                entity._belt = ("packed", None, entered_at, slot)
                yield ahead
        elif stage == "gap":
            yield from self._wait_until(entity, "gap", resume["until"], entered_at, slot, pre)
        if stage in ("packed", "gap"):
            while True:
                if merge.waiting and (merge.priority == "merge" or self._may_join(merge)):
                    self._at(entity, "packed", pre)
                    entity._belt = ("packed", None, entered_at, slot)
                    yield merge.joined
                    continue
                late = merge.left + merge.occ / self.speed - (merge.at - pre) / self.speed \
                    - self.env.now
                if late <= _EPS:
                    break
                self._at(entity, "packed", pre)
                entity._belt = ("gap", self.env.now + late, entered_at, slot)
                yield self.env.timeout(late)
            merge.claims += 1
            if self.type:
                merge.riders.popleft()
            distance = (merge.at - pre) / self.speed
            entity._belt = ("claim", self.env.now + distance, entered_at, slot)
            yield from self._ride(distance, entity, pre)
        elif stage in ("claim", "move"):
            until = resume["until"]
            yield from self._wait_until(entity, stage, until, entered_at, slot,
                                        merge.at - (until - self.env.now) * self.speed,
                                        merge.at)
        if stage != "blocked":
            yield from self._reach_section(entity, self._sections[slot + 1], merge.at,
                                           entered_at, slot)
        section = yield from self._room_ahead(
            entity, merge, section, None, entered_at,
            resume.get("request") if resume and stage == "blocked" else None)
        merge.left, merge.occ = self.env.now, self._occ(entity)
        merge.passers.append(entity.id)
        merge.claims -= 1
        clear.succeed()
        self._fire(merge, "changed")
        return section

    def _merge_in(self, entity, index: int, resume=None):
        """Generator: a part from merge *index*'s source joins the belt at its point
        (D854): a belt cell, the merge's door (one waits there at a time), then the gap
        rule; it is on the belt -- and its source free -- once it joins. *resume*: put back
        by a state asking for a cell, at the door or waiting to join (D918)."""
        merge = self._merges[index]
        stage = resume["stage"] if resume else "cell"
        ready = self.env.now if resume is None else float(resume["since"])
        entity._ready = ready                   # its merge wait counts from then (a state)
        if stage == "cell":
            entity._belt = ("cell", None, None, merge.slot)
            cell = yield from self.space.take(entity, accepts=False)
            door = merge.door.request()
            stage = "door"
        else:
            cell, door = resume["cell"], resume["door"]
        if stage == "door":
            entity._belt, entity._ask = ("door", None, None, merge.slot), door
            yield from self._belt_zones._zone_hold(entity)     # R23: a zoned belt's part (D1356)
            yield door
            merge.waiting, merge.joiner_occ = True, self._occ(entity)
            merge.waiting_share.set(1.0, self.env.now)
        first = stage == "join"         # `restore` set `waiting` and its share
        while not self._may_join(merge):
            if first:
                until = resume.get("until")
                wake, first = self._resumed_wakeup(merge, until), False
            else:
                wake, until = self._join_wakeup(merge)
            entity._belt = ("join", until, None, merge.slot)
            yield wake
        self._check_entry(entity)
        nxt = self._sections[merge.slot + 1]
        request = nxt.request(self.cells_of(entity))    # free: `_may_join` saw room
        section = (request, nxt.enter(self.env, self.cells_of(entity)))
        entity._section = (merge.slot + 1, section[1])
        accept(entity)
        t = self.env.now
        if self.type:
            # D1355: `passers` is bounded by cells; drop those off the belt (they leave in
            # order), so a join scans the items on it, not the cells
            while merge.passers and merge.passers[0] not in self._on:
                merge.passers.popleft()
        ahead = self._merge_ahead(merge)            # #2016 (not `passers`: see there)
        merge.waiting = False
        merge.waiting_share.set(0.0, t)
        merge.waits.add(t - ready)
        merge.merged += 1
        merge.left, merge.occ = t, self._occ(entity)
        merge.passers.append(entity.id)
        self.entered += 1
        self.occupancy.add(1, t)
        self._on.add(entity.id)
        if self._slug_n:
            self._belt_release._slug_join(entity, merge.at)
        self._on_belt(entity, t, merge.at, ahead)
        self._at(entity, "merge", merge.at, ahead=ahead)
        merge.door.release(door)
        self._fire(merge, "joined")
        self._fire(merge, "changed")
        yield from self._carry(entity, cell, t, section=section, slot=merge.slot + 1,
                               start=merge.at)

    def _reach_section(self, entity, section, at: float, entered_at=None, slot=None):
        """Generator, D853: a part reaches the start of *section* (at *at*) no sooner than
        its lag after the part `cells` places ahead of it there left the section's end, and
        (#2014) once the part directly ahead is its own length past *at*."""
        due, lag = section.due_lag()
        if due is not None:
            if not due.triggered:
                self._at(entity, "packed", at)      # Timeline puts it behind the one ahead
                entity._belt = ("due", None, entered_at, slot)
                if self._handshakes:
                    self._stands(entity)
                yield due
            late = due.value + lag - self.env.now
            if late > _EPS:
                entity._belt = ("move", self.env.now + late, entered_at, slot)
                yield from self._ride(late, entity, at - late * self.speed, "move")
        seq = self._seq_of(entity)
        due = False if seq is None else self._late(seq, at)
        if due is not False:
            self._at(entity, "packed", at)
            entity._belt = ("due", None, entered_at, slot)
            if self._handshakes:
                self._stands(entity)
            yield from self._behind(seq, at, False, due)

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
        # D1356: a state never puts a part back on a belt with zones, so `stage` is None
        zoned = bool(self._zone_count)
        if zoned and self._zone_delay > 0:
            yield from self._belt_zones._zone_arrive(entity, entered_at, ahead)
        elif stage == "move":
            entity._belt = ("move", until, entered_at, None)
            self._at(entity, "move", length - (until - self.env.now) * self.speed, length,
                     until - self.env.now)
            yield timeout_at(self.env, until)
        elif ahead is not None and not ahead.triggered:
            # Packed against the entity in front: move up as it leaves.
            entity._belt = ("packed", None, entered_at, None)
            self._at(entity, "packed", length)
            if not self.accumulating:
                self._stop_belt()
            if self._handshakes:
                self._stands(entity)
            yield ahead
            if not self.accumulating:
                self._start_belt()
            gap = self._end_occ                     # D1355: what the one ahead took
            entity._belt = ("move", self.env.now + gap / self.speed, entered_at, None)
            yield from self._ride(gap / self.speed, entity, length - gap, "move")
        elif self.accumulating and stage in (None, "packed"):
            # #950: the stops' rule at the end (D732) -- a part reaches it no sooner than
            # spacing / speed after the one ahead left, even when that one has left already.
            # A non-accumulating belt keeps the gap on its own: it stood while the end did.
            catch_up = self._end_left + self._end_occ / self.speed - self.env.now
            if catch_up > _EPS:
                entity._belt = ("move", self.env.now + catch_up, entered_at, None)
                yield from self._ride(catch_up, entity, length - catch_up * self.speed, "move")
        if zoned and not self._zone_delay:
            # delay 0 keeps today's timing above; the stands are counted for the dieback
            self._belt_zones._zone_close(self._zone_chain[0])
        if self._release_kind:
            # D1357: a state never puts a part back on a belt with a release rule
            yield from self._belt_release._release_gate(entity, entered_at)

        if self.exit_resource and stage != "handover":
            blocking = self.ctx.resource_manager.resolve(self.exit_resource)
            # put back at the stop, it found none free (a state refuses any other case)
            free_now = stage != "stop" and blocking is not None \
                and getattr(blocking, "free", 0) >= 1
            entity._belt = ("stop", None, entered_at, None)
            self._at(entity, "stop", length)
            if not free_now:
                self._end_blocked(True)
                if zoned:
                    self._exit_since = self.env.now
            yield from acquire(self, entity, self.exit_resource)
            if not free_now:
                self._end_blocked(False)
                if zoned:
                    # the exit frees: the end part is in zone 1
                    yield from self._belt_zones._zone_release()

        if not self.next_process:
            yield from self.handed_to_nothing(entity, length)

        if self.block_after_service:
            # Back-pressure (PHASE6 W3 M6, D115), AFTER the end stop: the part
            # holds its exit unit and its cell until the next belt takes it.
            if zoned:
                self._exit_since = self.env.now
            token = yield from self._hand_over(entity, entered_at, forward=stage != "handover")
            if zoned:
                # R23: the next belt kept the end part its zone-1 delay before taking it
                yield from self._belt_zones._zone_release(getattr(token, "zone_waited", 0.0))
        self._end_occ = self._occ(entity)
        if self._release_kind == "gap":
            self._end_len = self.item_length(entity)
        if zoned:
            self._zone_chain.popleft()
        clear.succeed()
        t = self._end_left = self.env.now
        self.exited += 1
        self.occupancy.add(-1, t)
        self.transit.add(t - entered_at)
        self.space.give_back(cell)
        if section is not None:
            self._leave_section(len(self._sections) - 1, section)
        self._on.discard(entity.id)
        self._left_belt(entity, entered_at, length)
        self._at(entity, "off", length)
        if self._reach:
            self._reached(entity)               # R5: it left this belt's end
        if self.ctx.debug:
            log.debug("Entity %s left conveyor '%s' at time %s.", entity.id, self.name, t)
        if not self.block_after_service:
            self.forward(entity, 0)

    def handed_to_nothing(self, entity, x: float):
        """Generator, A11 (D1372, R58): an exit with no next block (validation refuses such a
        model; a run started without it gets here). Warned once, or the run stopped under
        strict_checks; the part stands at *x* for the rest of the run, holding its cell, and
        the belt is blocked there as a station held after service is (D78): its blocked share
        shows it, and a run that drains names it as a deadlock."""
        run_warning(self, "W_CONVEYOR_HANDED_TO_NOTHING",
                    "a part reached an exit that hands it to no block; it stands there")
        self._at(entity, "handover", x)
        self.dead_end = True
        if self._rev is None:
            self._end_blocked(True)             # a reversible belt's `_hold` has set it
        yield self.env.event()                  # never succeeds: no block will take it

    def _hand_over(self, entity, entered_at, forward=True):
        """Generator: forward *entity* and keep it on the belt -- its cell,
        its place at the end -- until the downstream belt accepts it (the D78
        token). A DECIDE in between is transparent, so a full CHOSEN branch
        blocks the belt; belts merging on one entry queue FIFO on its cells.
        *forward* False: a state put the part back where it waits (D430)."""
        token = arm(self, entity)
        token.entered = entered_at          # D430: when it entered this belt, for a state
        if self._zone_count:
            # R23: what the next belt reads to keep the part its zone-1 delay (`_zone_hold`)
            token.zone_delay, token.zone_asked = self._zone_delay, self.env.now
        if forward:
            self.forward(entity, 0)
        if token.triggered:
            return token
        self._end_held = True
        self._at(entity, "handover", self._length)
        self._end_blocked(True)
        check_deadlock(self)
        yield token
        self._end_held = False
        self._end_blocked(False)
        return token

    def self_run(self, entity):
        self.env.process(self.self_process(entity))

    # -- put back by a state (D429, D430, D918) ------------------------------
    def restore_history(self, spec: dict) -> None:
        """What the parts ahead left behind (D922): when the last one left each point and
        the end, and the recent leave times of each section; before any part is put back."""
        self.space         # noqa: B018 -- builds the points and the sections
        self._untyped_state()
        left = spec.get("left") or ()
        for point, value in zip(self._points, left):
            if value is not None:
                point.left = float(value)
        if left and left[-1] is not None:
            self._end_left = float(left[-1])         # #1336
        for section, values in zip(self._sections, spec.get("sections") or ()):
            section.entrants.extend(_Left(float(value)) for value in values)

    def merge_resume(self, entity, belt: dict):
        """Generator: a merging part a state put back asking for a cell at its merge (D918);
        it joins the queue of the belt's cells in the order of the waiting ones."""
        return self._merge_in(entity, self._points[belt["point"]].index, belt)

    def restore(self, parts) -> list:
        """The parts a state puts back on this belt, [(entity, {stage, until, entered})]; the
        one its end still hands over has stage "handover". Each takes its cell at once and
        the chain of the parts past the ride is rebuilt in the order they entered (the order
        of an accumulating belt). Returns [(generator, joins a queue)]: the part at the end
        stop asks for a unit, so the caller puts it back with the waiting ones."""
        self.space         # noqa: B018 -- builds the points
        self._untyped_state()
        if self._points:
            return self._restore_points(parts)
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

    def _untyped_state(self) -> None:
        """A state says nothing of an item's cells (D1355): only an untyped belt is put back."""
        if self.type:
            raise RuntimeError(f"Conveyor '{self.name}': a state cannot put items back on a "
                               f"typed belt (E_CONVEYOR_PARAM).")

    def _section_of(self, belt: dict):
        """The section whose place a part put back holds (D923), or None."""
        stage, point = belt["stage"], belt.get("point")
        if stage in ("cell", "entry", "door", "join"):
            return None
        if point is None:
            return 0 if stage == "slot" else len(self._sections) - 1
        return point

    def _seed_records(self, items) -> None:
        """#2014: the records of the parts a state puts back on the belt, in belt order (the
        state's, D923: by section from the end back, each section's entrants in order), before
        any of them runs: the rule a part waits by reads the parts ahead, which would otherwise
        get their records only at their first stage, in the order they happen to resume. Each
        starts as its stage says (riding to its place until `until`, else standing there, an
        upper bound the clamp of the parts ahead makes exact). A merge's `passers`, which a
        merging part lands behind, are the parts put back past it, in the order they passed."""
        now = self.env.now
        passed = []
        for entity, belt, section in items:
            stage, point, until = belt["stage"], belt.get("point"), belt.get("until")
            if stage in ("cell", "entry", "slot", "door", "join") or entity.id in self._parts:
                continue
            place = self._length if point is None else self._points[point].at
            if stage in ("ride", "move", "claim") and until is not None and until > now:
                x0, x1, duration = place - (until - now) * self.speed, place, until - now
            else:
                x0, x1, duration = place, None, 0.0
            entered = belt.get("entered")
            self._seq += 1.0
            rec = self._parts[entity.id] = [self._occ(entity),
                                            now if entered is None else float(entered), 0.0,
                                            now, x0, x1, duration, self._seq, entity, None,
                                            False, None, None, 0.0, 0]
            self._order_add(rec)
            passed.append((entity.id, section))
        for merge in self._merges:
            merge.passers.clear()
            merge.passers.extend(pid for pid, section in passed
                                 if section is not None and section > merge.slot)

    def _restore_points(self, parts) -> list:
        """D923: a belt with points. The parts in the state's order, but a part handed over
        leads its section; every cell and section place is claimed, then the queued requests
        are made in that order, before any part runs. The point chains, the counts and the
        levels the stages imply are set here; each part then resumes at its stage."""
        now = self.env.now
        holds = [self._section_of(belt) for _entity, belt in parts]
        order = sorted(range(len(parts)), key=lambda i: (
            1 if holds[i] is None else -holds[i] - 1,
            parts[i][1]["stage"] != "handover", i))
        items = [(parts[i][0], dict(parts[i][1]), holds[i]) for i in order]
        for entity, belt, s in items:
            belt["cell"] = self.space.claim()
            if s is not None:
                section = self._sections[s]
                belt["section"] = (section.pool.request(), section.enter(self.env))
                entity._section = (s, belt["section"][1])
        for wanted in (("entry", "blocked", "join"), ("door",)):
            for entity, belt, _s in items:
                stage, point = belt["stage"], belt.get("point")
                if stage not in wanted:
                    continue
                if stage == "entry":
                    belt["request"] = self._sections[0].pool.request()
                elif stage == "blocked":
                    belt["request"] = self._sections[point + 1].pool.request()
                else:
                    belt["door"] = self._points[point].door.request()
        self._seed_records(items)
        out = []
        for entity, belt, _s in items:
            stage, point = belt["stage"], belt.get("point")
            target = self._points[point] if point is not None else None
            merging = isinstance(target, _Merge) and stage in ("door", "join")
            arrived = (stage not in ("ride", "entry", "slot") and not merging) \
                if point is not None else stage in ("packed", "move", "stop", "handover")
            if arrived:
                if target is None:
                    belt["ahead"], self._prev_clear = self._prev_clear, self.env.event()
                    belt["clear"] = self._prev_clear
                else:
                    belt["ahead"], target.clear = target.clear, self.env.event()
                    belt["clear"] = target.clear
            if isinstance(target, _Merge):
                if stage in ("claim", "due", "move", "blocked"):
                    target.claims += 1
                elif stage == "join":
                    # #2016: the length it lands with (a door it passed set it)
                    target.waiting, target.joiner_occ = True, self._occ(entity)
                    target.waiting_share.set(1.0, now)
            elif isinstance(target, _Stop):
                if stage in ("wait", "dwell", "blocked"):
                    target.busy.set(1.0, now)
                if stage == "wait":
                    target.blocked.set(1.0, now)
            elif isinstance(target, _Divert):
                if stage in ("push", "handover"):
                    target.busy.set(1.0, now)
                if stage == "handover":
                    target.blocked.set(1.0, now)
                    self._divert_waits += 1
            if stage not in ("entry", "slot") and not merging:      # on the belt
                self.occupancy.add(1, now)
                if self._merges:
                    self._on.add(entity.id)
            out.append((self._resume_point(entity, belt, merging), stage in ("wait", "stop")))
        return out

    def _resume_point(self, entity, belt: dict, merging: bool):
        stage, until, entered, point = (belt["stage"], belt.get("until"), belt.get("entered"),
                                        belt.get("point"))
        cell, section = belt["cell"], belt.get("section")
        if merging:
            yield from self._merge_in(entity, self._points[point].index, belt)
        elif stage == "entry":
            yield from self._enter(entity, cell, belt["request"])
        elif stage == "slot":
            entity._belt = ("slot", until, None, None)
            yield timeout_at(self.env, until)
            yield from self._board(entity, cell, section)
        elif point is not None and stage == "handover":      # at a divert, for its target
            yield from self._divert(entity, self._points[point], cell, entered, section,
                                    belt["clear"], belt)
        elif point is not None:
            start = self._points[point - 1].at if point else 0.0
            yield from self._carry(entity, cell, entered, section=section, slot=point,
                                   start=start, resume=belt)
        elif stage == "ride":
            yield from self._carry(entity, cell, entered, until, section=section)
        else:
            yield from self._leave(entity, cell, entered, belt.get("ahead"), belt.get("clear"),
                                   stage, until, section=section)

    def _resume(self, entity, belt, cell, ahead, clear):
        stage, until, entered = belt["stage"], belt.get("until"), belt.get("entered")
        if stage == "slot":
            entity._belt = ("slot", until, None, None)
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
        self._zone_dieback = [0.0] * self._zone_count
        self._zone_since = t
        self.release_waits.reset()
        # R63: what the parts on the belt now have stood is before the window
        self._dieback_extra = -self._stood_now()
        self.release_held.reset(t)
        if self._rev is not None:
            self._rev.backward.reset(t)
            self._rev.turns = 0

    def _raw(self, name: str):
        """Param *name* as written (R39), else its engine value (a belt built in code)."""
        return self._given.get(name, getattr(self, name))

    def _raw_at(self, kind: str, index: int, at: float) -> float:
        given = self._given_at.get(kind)
        return given[index] if given is not None and index < len(given) else at

    def get_params_dict(self):
        out = {"length": self._raw("length"), "speed": self._raw("speed"),
               "spacing": self._raw("spacing"),
               "accumulating": self.accumulating, "exit_resource": self.exit_resource,
               "from_node": self.from_node, "to_node": self.to_node,
               "stops": [{"at": self._raw_at("stops", k, at), "time": time, "resource": resource}
                         for k, (at, time, resource) in enumerate(self.stops)],
               # #2003: a robot pick has no push time and no when_full, as validation says
               "diverts": [_divert_params(divert._replace(at=self._raw_at("diverts", k,
                                                                           divert.at)))
                           for k, divert in enumerate(self.diverts)],
               "merges": [{"at": self._raw_at("merges", k, at), "from": source,
                           "priority": priority}
                          for k, (at, source, priority) in enumerate(self.merges)]}
        # Only when set, so the params of a belt with no eye stay as they were.
        if self.eyes:
            out["eyes"] = [{"at": self._raw_at("eyes", k, at), "edge": edge,
                            **({"assign": [dict(row) for row in rows]} if rows else {}),
                            **({"signal": signal} if signal else {})}
                           for k, (at, edge, rows, signal) in enumerate(self.eyes)]
        if self._part_length > 0:
            out["part_length"] = self._raw("part_length")
        if self.path:
            out["path"] = [dict(piece) for piece in self.path]
        if self.zones:
            out["zones"] = dict(self.zones)
        if self.release:
            out["release"] = {form: dict(rule) for form, rule in self.release.items()}
        if self.type:
            out["type"] = self.type
            if self.gap:
                out["gap"] = self._raw("gap")
            if self.cell:
                out["cell"] = self._raw("cell")
        # R39 (D1377): a unit only when one other than "base" was given, and only beside its
        # value, so old params stay as they were
        for key, unit in self._given_units.items():
            if key[:-len("_unit")] in out:
                out[key] = unit
        # D1368, D1378: only when set, as written (R39)
        for key in ("reversible", "reverse_signal", "reverse_to", "expose"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    def _measured(self, param_dict: dict, name: str, default: float) -> float:
        """Param *name* in the engine's base; keeps it as written, with its unit (R39)."""
        raw = float(param_dict.get(name, default) or default)
        self._given[name] = raw
        unit = param_dict.get(f"{name}_unit")
        if unit not in (None, "", "base"):
            self._given_units[f"{name}_unit"] = unit
        return scaled(raw, field_factor(param(SPEC, name), param_dict, self.ctx.units,
                                        SPEC.params))

    def set_params_from_dict(self, param_dict):
        units = self.ctx.units
        self._given, self._given_units = {}, {}
        self.length = self._measured(param_dict, "length", 0.0)
        self.speed = self._measured(param_dict, "speed", 1.0)
        self.spacing = self._measured(param_dict, "spacing", 1.0)
        self.accumulating = bool(param_dict.get("accumulating", True))
        self.exit_resource = str(param_dict.get("exit_resource", "") or "").strip()
        self.from_node = str(param_dict.get("from_node", "") or "").strip()
        self.to_node = str(param_dict.get("to_node", "") or "").strip()
        # A stop's `at` is a length like `length` (its unit too); `time` stays an
        # expression, evaluated per part in the model's time unit.
        at_factor = field_factor(param(SPEC, "length"), param_dict, units, SPEC.params)
        self._given_at = {kind: [float(point.get("at", 0.0)) for point in
                                 param_dict.get(kind) or []]
                          for kind in ("stops", "diverts", "merges", "eyes")}
        self.stops = [(scaled(float(stop.get("at", 0.0)), at_factor), stop.get("time", 0.0) or 0.0,
                       str(stop.get("resource", "") or "").strip())
                      for stop in param_dict.get("stops") or []]
        # A divert's `at` likewise; its condition and time are read per part (D851).
        self.diverts = [DivertSpec(scaled(float(divert.get("at", 0.0)), at_factor),
                                   divert.get("condition", ""), divert.get("time", 0.0) or 0.0,
                                   str(divert.get("when_full", "wait") or "wait"),
                                   divert.get("intercept") is True,
                                   str(divert.get("on_late", "wait") or "wait").strip(),
                                   str(divert.get("side", "") or "").strip(),
                                   dict(divert["rule"]) if isinstance(divert.get("rule"), dict)
                                   else None,
                                   str(divert.get("rule_group", "") or "").strip(),
                                   angle=divert.get("angle", 90))
                        for divert in param_dict.get("diverts") or []]
        self.merges = [(scaled(float(merge.get("at", 0.0)), at_factor),
                        str(merge.get("from", "") or "").strip(),
                        str(merge.get("priority", "belt") or "belt"))
                       for merge in param_dict.get("merges") or []]
        # An eye's `at` is a length like a stop's; `part_length` has its own unit (D1347).
        self._part_length = self._measured(param_dict, "part_length", 0.0)
        self.eyes = [(scaled(float(eye.get("at", 0.0)), at_factor),
                      str(eye.get("edge", "front") or "front"),
                      [dict(row) for row in eye.get("assign") or []],
                      str(eye.get("signal", "") or "").strip())
                     for eye in param_dict.get("eyes") or []]
        self.path = [dict(piece) if isinstance(piece, dict) else piece
                     for piece in param_dict.get("path") or []]
        self._path_factor = at_factor
        self.type = str(param_dict.get("type", "") or "").strip().lower()
        if self.type and self.type not in TYPES:
            raise ValueError(f"Conveyor '{self.name}': type '{self.type}' is not one of "
                             f"{', '.join(sorted(TYPES))}.")
        self.gap = self._measured(param_dict, "gap", 0.0)
        # R20: 0 (unset) is 1 cm in any unit system; a set cell follows its unit
        self.cell = self._measured(param_dict, "cell", 0.0)
        if self.gap < 0 or self.cell < 0:
            raise ValueError(f"Conveyor '{self.name}': gap and cell must be 0 or more "
                             f"(E_CONVEYOR_PARAM).")
        self._cells_by_type = {}
        # D1356: `release_delay` is in the model's time unit, as a stop's time, unless the
        # zones say their own `time_unit`
        zones = param_dict.get("zones") or {}
        self.zones = dict(zones)
        self._zone_count = int(zones.get("count", 0) or 0)
        unit = str(zones.get("time_unit", "base") or "base")
        delay = float(zones.get("release_delay", 0.0) or 0.0)
        self._zone_delay = delay if unit == "base" else \
            delay * conversion_factor("time", unit, units.time)
        self._set_release(param_dict.get("release") or {}, at_factor, units)
        reversible = param_dict.get("reversible", False)
        if not isinstance(reversible, bool):
            # a "true" text would otherwise read as false
            raise ValueError(f"Conveyor '{self.name}': reversible must be true or false, not "
                             f"{reversible!r} (E_CONVEYOR_REVERSE).")
        self.reversible = reversible
        self._exit_slots = None
        self.reverse_signal = str(param_dict.get("reverse_signal", "") or "").strip()
        self.reverse_to = str(param_dict.get("reverse_to", "") or "").strip()
        self.expose = param_dict.get("expose", False) is True
        if self.expose:
            self._seed_exposed()
        self._space = None

    def _set_release(self, release: dict, length_factor: float, units) -> None:
        """D1357: one form; `gap` and `interval` in the unit of `length` (as a stop's `at`), a
        slug's `timeout` in the model's time unit unless its `time_unit` says another."""
        self.release = {form: dict(rule or {}) for form, rule in release.items()}
        self._release_kind, self._release_len = "", 0.0
        self._slug_n, self._slug_timeout = 0, 0.0
        if not release:
            return
        if len(release) != 1:
            raise ValueError(f"Conveyor '{self.name}': release takes one form, not "
                             f"{', '.join(sorted(release))} (E_CONVEYOR_RELEASE).")
        form, rule = next(iter(self.release.items()))
        if form == "slug":
            self._release_kind = "slug"
            self._slug_n = int(rule.get("count", 1) or 1)
            unit = str(rule.get("time_unit", "base") or "base")
            timeout = float(rule.get("timeout", 0.0) or 0.0)
            self._slug_timeout = timeout if unit == "base" else \
                timeout * conversion_factor("time", unit, units.time)
        elif form == "metering" and "interval" in rule:
            self._release_kind = "interval"
            self._release_len = scaled(float(rule["interval"] or 0.0), length_factor)
        elif form in ("metering", "singulation"):
            self._release_kind = "gap"
            self._release_len = scaled(float(rule.get("gap", 0.0) or 0.0), length_factor)
        else:
            raise ValueError(f"Conveyor '{self.name}': release form '{form}' is not metering, "
                             f"singulation or slug (E_CONVEYOR_RELEASE).")


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
                              "(it rides on), \"intercept\"?: true (a robot on link k + 1 picks "
                              "the part on the move; its first move targets this divert), "
                              "\"on_late\"?: what the part does when that robot is busy: "
                              "\"wait\" (default: it stands where the robot meets it), \"pass\" "
                              "(it rides on) or an expression, true meaning wait}. Divert k "
                              "leaves by outgoing link k + 1; link 0 is the belt's end. A link may "
                              "name its exit instead: source_port \"out\" (the end) or "
                              "\"d<k>\", each exit once. \"side\"?: \"left\" (default) or \"right\" of a "
                              "part travelling along the belt as seen on screen, where the "
                              "divert's exit port faces on the floor plan; \"angle\"?: degrees from the "
                              "belt's travel to that exit, above 0 and below 180 (90, the "
                              "default, is square; 45 leans downstream), floor geometry only. "
                              "\"rule\"?: a routing "
                              "rule in place of the condition, {\"kind\", \"exits\"?, "
                              "\"conditions\"?, \"limit\"?, \"attribute\"?} as a Rule-mode "
                              "DECIDE's, read once as the part reaches it; \"rule_group\"?: a name "
                              "the diverts sharing one rule all give, the rule on the first of "
                              "them along the belt only; the exits are the group's diverts in "
                              "belt order (names default to their ports), and an exit named "
                              "\"out\" passes them all to the belt's end. A part counts under way "
                              "until it reaches its exit's target piece or leaves a belt on its way "
                              "there by a divert; name an exit \"L1:L1\" to stop the count at "
                              "branch L1. While every exit is at its limit the part waits at the "
                              "group's first divert and the belt behind packs as at a stop. "
                              "Empty: none.",
                  quantity="none", kinds=("table",)),
        ParamSpec("merges", list, [],
                  description="Points where parts from another block join the belt: {\"at\": "
                              "<distance from the entry, in the unit of `length`>, \"from\": "
                              "<the block linking to this belt whose parts join there>, "
                              "\"priority\"?: \"belt\" (default: they wait for a gap of two "
                              "spacings) or \"merge\" (parts on the belt make room)}. "
                              "Accumulating belts only. Empty: none.",
                  quantity="none", kinds=("table",)),
        # D1347: photo-eyes and the part length a back edge reads.
        ParamSpec("eyes", list, [],
                  description="Photo-eyes, points that fire as a part passes: {\"at\": "
                              "<distance from the entry, in the unit of `length`>, \"edge\"?: "
                              "\"front\" (default: as the part's front reaches `at`) or "
                              "\"back\" (as its back clears `at`, a `part_length` later), "
                              "\"assign\"?: <rows {\"target\", \"expr\"} run on that part as "
                              "ASSIGN runs them>, \"signal\"?: <signal raised, for HOLD blocks "
                              "and item code>}. Each firing is counted and traced; a queue that "
                              "stands short of an eye delays it. Empty: none.",
                  quantity="none", kinds=("table",)),
        ParamSpec("path", list, [],
                  description="The belt as pieces laid end to end from its entry: {\"straight\": "
                              "<length>} or {\"arc\": {\"radius\": <r>, \"angle\": <degrees, up to "
                              "360>, \"turn\": \"left\" or \"right\" as seen on screen}}, lengths in the unit of "
                              "`length`. Their total is the belt's length, so set `length` to 0 "
                              "and give no nodes. Empty: a straight belt of `length`.",
                  quantity="none", kinds=("table",)),
        ParamSpec("type", str, "", quantity="none", kinds=("choice",),
                  description="Conveyor type: \"belt\", \"roller\", \"chain\", \"pallet\" or "
                              "\"mdr\". Empty: an untyped belt with `spacing`, one item per "
                              "cell. A typed belt has cells of `cell` and an item takes "
                              "ceil((its length + `gap`) / cell) of them; `spacing` is ignored "
                              "and the results' Capacity counts cells."),
        ParamSpec("gap", float, 0.0, quantity="length", min=0,
                  description="Free belt length kept between two items on a typed belt."),
        ParamSpec("gap_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `gap`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("cell", float, 0.0, quantity="length", min=0,
                  description="Length of one cell of a typed belt; an item takes as many as its "
                              "length plus `gap` needs. 0: 1 cm in any length unit. Ignored on "
                              "an untyped belt."),
        ParamSpec("cell_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `cell`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("zones", dict, {},
                  description="Accumulation zones: {\"count\": <zones of equal length, "
                              "zone 1 at the exit>, \"release_delay\"?: <time, in the model's "
                              "time unit unless \"time_unit\" says another>}. Parts accumulate "
                              "as without zones; when the held exit frees, a standing part "
                              "restarts release_delay times its front's zone later, never "
                              "nearer than its own length to the part ahead; the end part "
                              "keeps the exit unit through its delay, and a part handed to a "
                              "next belt waits it before that belt takes it. Delay 0 is a belt "
                              "without zones and releases the queue as one slug, as Emulate3D "
                              "does at 0; Emulate3D's staggered headways at a 1 s delay (1.26 s "
                              "and 0.96 s measured) differ, an open comparison. Accumulating "
                              "belts with no stop or merge only. Empty: none.",
                  quantity="none"),
        ParamSpec("release", dict, {},
                  description="How parts leave the end, one form: {\"metering\": {\"gap\": "
                              "<length>}} (a part leaves once the one before is that far past "
                              "the end, measured from its tail), {\"metering\": {\"interval\": "
                              "<length>}} (front to front, never nearer than the part before "
                              "takes on the belt), {\"singulation\": {\"gap\": <length>}} (the "
                              "same as metering by gap) or {\"slug\": {\"count\": <parts>, "
                              "\"timeout\": <time>, \"time_unit\"?}} (the end holds until "
                              "count parts are queued or timeout has passed since the first "
                              "stood there, then lets them go nose to tail). Lengths are in the "
                              "unit of `length` and travelled at the belt speed; the timeout is "
                              "in the model's time unit unless time_unit says another. Held "
                              "parts make the ones behind accumulate. A slug counts every part "
                              "standing in the queue, behind a photo-eye or a stop too; a part "
                              "whose place in the queue is behind a stop dwells there after the "
                              "release. Each part's hold at the end is kept (in a slug the "
                              "lead's; the parts let go with it hold 0). Accumulating belts "
                              "without zones only. Empty: parts leave as soon as they can.",
                  quantity="none"),
        # D1368: a belt that turns during a run
        ParamSpec("reversible", bool, False, quantity="none",
                  description="True: the belt can run backward during a run, turned by "
                              "`reverse_signal` or by item code (item.reverse). Each part on it "
                              "then rides back the distance it has travelled and leaves at the "
                              "start by `reverse_to`; nothing gets on until it runs forward "
                              "again. Reversible belts linked end to start, with nothing between "
                              "them, are one chain that turns as one: a part crosses each joint "
                              "the way the chain runs. A reversible belt has no stops, diverts, "
                              "merges, photo-eyes, zones or release rule."),
        ParamSpec("reverse_signal", str, "", quantity="none",
                  description="Signal that turns a reversible belt each time it is raised "
                              "(forward, backward, forward...), with its whole chain. Empty: "
                              "only item code turns it."),
        ParamSpec("reverse_to", str, "", quantity="none",
                  description="Exit of a reversible belt for parts leaving at its start: the "
                              "target block of one of its links, or \"in\" (the link whose "
                              "source_port is \"in\"); not a conveyor feeding this belt, "
                              "since nothing boards a belt by its end. In a chain, only the "
                              "first piece names it: a later piece hands back to the one "
                              "before."),
        ParamSpec("part_length", float, 0.0,
                  description="Length of one part along the belt, which a back-edge eye reads. "
                              "0: the spacing, or a typed belt's type zone length. On an "
                              "untyped belt it is no longer than the spacing.",
                  quantity="length", min=0),
        ParamSpec("part_length_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `part_length`: \"base\" (the model's) or one of its quantity's units."),
        # D1378: state for a later PLC connection (spec goal 14)
        ParamSpec("expose", bool, False, quantity="none",
                  description="True: the belt writes its state as model variables, read with "
                              "VAR(\"<belt>.running\") in a HOLD condition or item code: "
                              "`running` (0 only while a non-accumulating belt stands for a "
                              "part held at the exit it runs to), `forward` on a reversible "
                              "belt (0 while it runs backward), `eye<k>.count` (eye k's "
                              "firings so far) and `eye<k>.blocked` (1 while a part's body "
                              "lies across eye k, front edge to back edge; not on a belt with "
                              "both zones and diverts)."),
    ),
    factory=lambda env, block: Conveyor(env, block),
    family="move",
)


# -- exposed state (D1378) ------------------------------------------------------

def eyes_blocked(zones: int, diverts) -> bool:
    """D1390: an exposed belt writes its eyes' levels unless it has both zones and diverts:
    there a part can reach a point through the part ahead standing in the zone chain (#2018),
    so no stage gives its nose. D1379 on a zoned all-eye belt, the nose rule elsewhere."""
    return not (zones and diverts)


def exposed_keys(reversible: bool, eyes: int, blocked: bool = True) -> list:
    """D1378: (name after "<belt>.", start value) of each variable an exposed belt writes:
    every eye's count (D1380), its level where *blocked* (`eyes_blocked`)."""
    return [("running", 1), *((("forward", 1),) if reversible else ()),
            *((f"eye{k}.count", 0) for k in range(eyes)),
            *((f"eye{k}.blocked", 0) for k in range(eyes if blocked else 0))]


def exposed_variables(block: dict) -> list:
    """D1378: the variable names an exposed CONVEYOR or CONVEYOR_TRANSFER block dict writes
    (none when `expose` is not true), what validation reads VAR() names against."""
    params = block.get("params") or {}
    if params.get("expose") is not True:
        return []
    name, kind = block.get("name", ""), str(block.get("type", "")).upper()
    if kind == "CONVEYOR_TRANSFER":
        return [f"{name}.state"]
    if kind != "CONVEYOR":
        return []
    eyes, zones = params.get("eyes"), params.get("zones")
    count = zones.get("count", 0) if isinstance(zones, dict) else 0
    keys = exposed_keys(params.get("reversible") is True,
                        len(eyes) if isinstance(eyes, list) else 0,
                        eyes_blocked(count, params.get("diverts")))
    return [f"{name}.{key}" for key, _value in keys]


# -- floor geometry of a block dict ---------------------------------------------

def _start_pose(position, rotation) -> Pose:
    """The entry pose of a belt from its `layout_position` and `layout_rotation` (D1354: the
    heading is the rotation, folded into [0, 360) as D74 stores it)."""
    x, y = position or (0.0, 0.0)
    return Pose(float(x), float(y), normalise_rotation(rotation))


def belt_geometry(block: dict, units) -> tuple[Pose, list, list]:
    """A CONVEYOR block dict's floor geometry: (start pose, segments, diverts), with the
    segments' lengths and radii and the diverts' `at` in metres, the floor plan's unit. A belt
    with no `path` is one straight piece of its `length` (none when the length comes from
    network nodes). *units* is the model's `ModelUnits` (`timeunits.units_of(simulation)`);
    a path `conveyor_path.parse_path` refuses raises its ValueError. One rule for the agent
    tools, the static checks and the editor."""
    params = block.get("params") or {}
    path = params.get("path") or []
    length = params.get("length")
    if not path and isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0:
        path = [{"straight": float(length)}]
    # `length`, the path and the diverts' `at` are in the belt's length_unit; the floor is metres.
    factor = field_factor(param(SPEC, "length"), params, units, SPEC.params)
    segments = [conveyor_path.Segment(seg.kind, seg.length * factor, seg.radius * factor,
                                      seg.angle, seg.turn)
                for seg in parse_path(path)]
    diverts = [{**d, "at": float(d["at"]) * factor}
               if isinstance(d, dict) and isinstance(d.get("at"), (int, float))
               and not isinstance(d.get("at"), bool) else d
               for d in params.get("diverts") or []]
    return (_start_pose(block.get("layout_position"), block.get("layout_rotation")),
            segments, diverts)


def belt_ports(block: dict, units) -> list:
    """The ports (`conveyor_path.Port`) of a CONVEYOR block dict laid out on the floor plan."""
    start, segments, diverts = belt_geometry(block, units)
    return conveyor_path.ports(str(block.get("name", "")), start, segments, diverts)
