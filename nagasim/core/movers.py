"""Mover fleets: forklifts, AGVs, tugs (PHASE4 E1, ARCHITECTURE D68).

A fleet is a named pool of vehicles that drive on the path network
(`core/network.py`) and carry entities between places. The model section is
`fleets`:

    {"name": "Forklifts", "count": 3, "speed": 2.0, "speed_empty": 3.0,
     "load_time": "0.5", "unload_time": "0.3", "time_unit": "base",
     "home": "dock", "park": "stay", "dispatch": "nearest", "select": "nearest"}

**A fleet IS a resource.** It registers one `LoggedSimpyResource` under its own
name with `capacity = count`, so everything resources already do works with no
new code: a capacity schedule takes vehicles off shift, a failure breaks them
down, `NR("Forklifts")`/`IDLE("Forklifts")` read it in an expression, and
utilization, seize counts and the three cost fields report in the ordinary
resource frame. What a fleet adds on top is POSITION per unit -- which vehicle
is where -- and that is the only thing this module owns.

Two rule families, both deliberately small:

* **`dispatch`** -- whose request is served when a vehicle frees. `fifo` (the
  default) and `nearest` (the request whose pick-up is closest to an idle
  vehicle -- shortest empty travel). Request PRIORITY is always the primary key
  under both, because it is the SimPy request's own priority, and
  "longest waiting" IS fifo, so neither needs a rule of its own.
* **`select`** -- which idle vehicle serves a granted request: `nearest` (the
  default), `longest_idle` or `cyclic`.

Ceilings, recorded in ARCHITECTURE D68:

* no `keep the mover` seize: a vehicle is held for one TRANSPORT cycle.

`loads: k` (PHASE6 W3-M4, ARCHITECTURE D116) lifts "one entity per trip": the
request granted a vehicle LEADS, and up to k-1 further TRANSPORT requests still
waiting on the fleet attach to that vehicle's tour -- in dispatch order, priority
first -- holding no unit of their own. The fleet still counts VEHICLES.

A failure or a capacity schedule takes a NAMED vehicle since PHASE4 T7: the
blocker still waits for a free unit (the WAIT rule, so the down time is exactly
what it always was), and the unit it takes is the one that has been standing
still longest, at wherever it is standing. A blocker granted while every vehicle
is busy names none -- the count is already right, and the next to free stays
out.

`park: "home"` (PHASE4 T7) frees the vehicle the instant it is empty and drives
it home in its own INTERRUPTIBLE process: a request arriving mid-return takes
the vehicle from exactly where it has got to, leaving its segment by whichever
end reaches the pick-up soonest (the only end, on a one-way segment). The return
is empty travel, not idle time. `park: "stay"` (default) leaves it where it
dropped off.

A fleet with `kind: "worker"` (PHASE6 W3-M3, ARCHITECTURE D114) is the same
pool: its units may be named in `units` (each with `skills` and a `home`), a
SEIZE/PROCESS with `worker_fleet` summons one, and the only new rule sits in the
dispatch point -- a request needing a skill is granted only a unit that has it,
the first servable request in dispatch order winning (skip-over).

A segment's `capacity` binds every fleet's travel since PHASE6 W3 M5 (D117):
`SegmentTraffic` is zone control over the limited segments, and only a path
that crosses one leaves the one-timeout drive.

A fleet with `motion: "gantry"` (PHASE6 W3-M7, ARCHITECTURE D118) skips the
network path entirely: it takes `speed_x`/`speed_y` (independent axes) and a
`hoist_time`, and travel between two nodes is the direct Chebyshev hop
`max(|dx| / speed_x, |dy| / speed_y) + hoist_time` -- no segments, no
`SegmentTraffic`, no Dijkstra. `Mover.pos` holds the (x, y) an interrupted
park-home return left it at (linear interpolation, no "exits" to back off --
a direct hop has no segment to be mid-way ON); `distance_to` then answers in
the same TIME a gantry's own hop takes, which is what its `nearest`
dispatch/select rank by.
"""
from __future__ import annotations

import logging
import math

import numpy as np
import simpy
from simpy.core import BoundClass
from simpy.resources.resource import PriorityRequest, SortedQueue

from nagasim.core.base_process import deterministic_hash
from nagasim.core.battery import battery_of, drain_kw
from nagasim.core.block_spec import WORKER_BLOCKED_MODES
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.problems import problem
from nagasim.core.resource_manager import ENERGY_STATES, _Resource, capacity_controller
from nagasim.core.seizing import QUEUEING_BLOCK_TYPES
from nagasim.core.stats import TimeWeighted
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.timeunits import ModelUnits, conversion_factor
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Mover", "MoverFleet", "SegmentTraffic", "build_fleets", "DISPATCH_RULES", "SELECT_RULES",
           "validate_fleets_structured", "validate_workers_structured", "fleet_skills", "FLEET_KINDS",
           "FLEET_MOTIONS"]

DISPATCH_RULES = ("fifo", "nearest")
SELECT_RULES = ("nearest", "longest_idle", "cyclic")
PARK_RULES = ("stay", "home")
FLEET_KINDS = ("vehicle", "worker")
FLEET_MOTIONS = ("network", "gantry")

# Mover states, in the order the KPI frame reports their time shares.
# "charging" (D165) stays 0 for a fleet without a battery.
STATES = ("empty", "loaded", "handling", "idle", "charging")


class _TaggedRequest(PriorityRequest):
    """A fleet request that says, BEFORE SimPy first tries to grant it, where
    it is picked up and which skill it needs (W3-M3): `Put.__init__` runs the
    first grant attempt, so a tag stamped after construction would be too late
    for a skill check. A blocker is made with no tag and so needs no unit in
    particular."""

    def __init__(self, resource, priority=0, preempt=True):
        self.mover_tag, resource.pending_tag = resource.pending_tag, None
        self.mover = None          # the unit a worker fleet reserved at the grant
        self.job = None            # a TRANSPORT's trip, which a tour may attach (W3-M4)
        self.attached = False      # True once served by another request's vehicle
        super().__init__(resource, priority, preempt)


class _DispatchResource(_Resource):
    """A resource whose waiting queue is re-sorted just before each grant.

    SimPy fixes a request's place in the queue when the request is MADE, which
    is right for a priority and wrong for "nearest request first": how far a
    vehicle has to drive to a pick-up is only known once you know which vehicle
    freed. `_trigger_put` is the one place SimPy hands out units, so sorting
    there -- and nowhere else -- is the whole of live dispatching.

    A worker fleet (W3-M3, D114) also sets `grant`: a request may need a unit
    with a skill, so the FIRST request in dispatch order that some idle unit
    can serve is granted -- a request nobody idle qualifies for is skipped over,
    not waited behind -- and the unit is reserved right here, so two grants in
    one instant never count the same idle worker.
    """

    request = BoundClass(_TaggedRequest)
    # Stock SimPy queue, not _InsortQueue: the dispatch sort below reorders the
    # list by another key, and only SortedQueue.append's full re-sort puts a new
    # request back in `.key` order. Inserting by bisection into the dispatch
    # order would place it differently and change results.
    PutQueue = SortedQueue

    def __init__(self, env, capacity):
        super().__init__(env, capacity)
        self.dispatch_key = None
        self.pending_tag = None
        self.grant = None

    def _trigger_put(self, get_event):
        key = self.dispatch_key
        if self.grant is None:
            if key is not None and len(self.put_queue) > 1:
                self.put_queue.sort(key=key)
            return super()._trigger_put(get_event)
        while len(self.users) < self.capacity and self.put_queue:
            if key is not None and len(self.put_queue) > 1:
                self.put_queue.sort(key=key)
            request = next((r for r in self.put_queue if self.grant(r)), None)
            if request is None:
                return
            self.put_queue.remove(request)
            self.users.append(request)
            request.usage_since = self._env.now
            request.succeed()


class SegmentTraffic:
    """Zone control on capacity-limited segments (PHASE6 W3 M5, D117).

    One `simpy.Resource` per segment that declares a `capacity` -- a two-way
    segment is ONE resource for both directions -- shared by every fleet on the
    network. A mover requests its NEXT limited segment at the end of the one it
    is on and releases what it holds only once granted (or on reaching its
    destination node), so a waiting mover stands at the segment end, still
    holding it. At the instant a mover blocks, the wait-for graph is checked:
    a set of waiting movers whose segments are held only by each other can
    never move again, and the run ends naming the chain (as D78 does).
    """

    def __init__(self, env, network):
        self.env = env
        self.resources = {seg: simpy.Resource(env, capacity=seg.capacity)
                          for seg in network.segments if seg.capacity}
        self.owner: dict = {}              # request -> Mover
        self.held: dict = {}               # Mover -> [request] it holds
        self.waiting: dict = {}            # Mover -> (segment, request, since)
        self.wait = dict.fromkeys(self.resources, 0.0)
        self.max_occupancy = dict.fromkeys(self.resources, 0)

    def limited(self, segments) -> bool:
        return any(seg in self.resources for seg in segments)

    def request(self, mover, seg):
        """Ask for *seg*; the caller yields the request. A request not granted
        at once is a BLOCK: recorded, then checked for a deadlock."""
        req = self.resources[seg].request()
        self.owner[req] = mover
        self.waiting[mover] = (seg, req, self.env.now)
        if not req.triggered:
            self._check_deadlock(mover)
        return req

    def entered(self, mover) -> None:
        """The awaited segment is granted: leave the one held behind."""
        seg, req, since = self.waiting.pop(mover)
        self.wait[seg] += self.env.now - since
        self.release_all(mover)
        self.held[mover] = [req]
        self.max_occupancy[seg] = max(self.max_occupancy[seg], len(self.resources[seg].users))

    def cancel(self, mover) -> None:
        """Drop a pending request (an interrupted return, T7), crediting its wait."""
        entry = self.waiting.pop(mover, None)
        if entry is None:
            return
        seg, req, since = entry
        self.wait[seg] += self.env.now - since
        self.owner.pop(req, None)
        if req.triggered:
            self.resources[seg].release(req)
        else:
            req.cancel()

    def release_all(self, mover) -> None:
        for req in self.held.pop(mover, ()):
            self.owner.pop(req, None)
            req.resource.release(req)

    def _check_deadlock(self, root) -> None:
        # Greatest fixed point: a waiting mover is stuck when every holder of
        # the segment it waits for is stuck too (capacity > 1 needs ALL of
        # them; for capacity 1 this is exactly a cycle). A holder that is
        # moving, or whose own request was granted this instant, is not.
        # ponytail: O(waiting^2) per block; fine for fleets of tens of movers.
        def holders(m):
            seg = self.waiting[m][0]
            return [self.owner[r] for r in self.resources[seg].users if r in self.owner]

        stuck = {m for m, (_s, req, _t) in self.waiting.items() if not req.triggered}
        changed = True
        while changed and root in stuck:
            changed = False
            for m in list(stuck):
                if any(h not in stuck for h in holders(m)):
                    stuck.discard(m)
                    changed = True
        if root not in stuck:
            return
        links, m, seen = [], root, set()
        while m not in seen:
            seen.add(m)
            holder = next(h for h in holders(m) if h in stuck)
            links.append(f"{m.name} waits for {self.waiting[m][0].name} held by {holder.name}")
            m = holder
        raise RuntimeError(
            f"deadlock at t={self.env.now:g}: segment traffic is locked -- "
            f"{'; '.join(links)}. Make a segment one-way, raise its capacity, or add a "
            f"passing place (ARCHITECTURE D117).")

    def reset_stats(self) -> None:
        """Warm-up: zero the waits; occupancy restarts from what is on it now."""
        for seg, res in self.resources.items():
            self.wait[seg] = 0.0
            self.max_occupancy[seg] = len(res.users)


class Mover:
    """One vehicle: where it is, what it is doing, and its own totals.

    A vehicle on its way home (PHASE4 T7) is available but not at a node:
    `homing` is the interruptible drive process and `trip` is
    `(started, path, segments, speed)`, from which its exact position at any
    instant is arithmetic.
    """

    __slots__ = ("fleet", "unit", "node", "state", "_since", "times",
                 "distance_empty", "distance_loaded", "trips", "idle_since",
                 "homing", "trip", "name", "skills", "home", "pos",
                 "soc", "min_soc", "depleted", "drain_kwh", "charge_trips", "charger", "session",
                 "leg", "until", "task", "watch")

    def __init__(self, fleet: "MoverFleet", unit: int, node: str, t: float,
                 name: str = "", skills=(), home: str = ""):
        self.fleet = fleet
        self.unit = unit
        self.node = node
        # A worker's identity (W3-M3): what it can do and where it parks.
        self.name = name or f"{fleet.name}#{unit}"
        self.skills = frozenset(skills)
        self.home = home or node
        self.state = "idle"
        self._since = t
        self.times = dict.fromkeys(STATES, 0.0)
        self.distance_empty = 0.0
        self.distance_loaded = 0.0
        self.trips = 0
        self.idle_since = t
        self.homing = None
        self.trip = None
        # W3-M7 (D118): a gantry's exact (x, y) while an interrupted park-home
        # return sits between two nodes; None everywhere else (including a
        # network-motion fleet, which is always exactly AT `node`).
        self.pos: tuple[float, float] | None = None
        # Battery (D164-D166), unused without one. `soc` is as of `_since`;
        # `charger` is the one the vehicle is driving to or standing at, and
        # `session` is `(the charger's session, its point)` while power flows.
        self.soc = self.min_soc = fleet.initial_soc
        self.depleted = 0
        self.drain_kwh = 0.0
        self.charge_trips = 0
        self.charger = None
        self.session = None
        # What a state reads (D383), stale once over: `leg` is `(started, to, loaded)`
        # while it drives a path in one go, `until` when its handling or charge ends,
        # `task` is `(stage, job, unload time)` of the TRANSPORT trip it is on.
        self.leg = self.until = self.task = None
        self.watch = 0         # #679: the idle trigger armed last; an older one is stale

    def __repr__(self):
        return f"<Mover {self.fleet.name}#{self.unit} at {self.node} {self.state}>"

    def progress(self) -> tuple | None:
        """Where an interruptible drive has got to right now:
        `(from, to, segment, distance driven along it, distance driven before
        it)`, or None when the vehicle is standing at `node`.

        A gantry (W3-M7, D118) has no segments: `segment` is None and the
        fourth/fifth values are instead the elapsed time and the hop's total
        duration, since a direct hop has no "distance before it" to share."""
        if self.trip is None:
            return None
        started, path, segments, speed = self.trip
        elapsed = self.fleet.env.now - started
        if self.fleet.motion == "gantry":
            a, b = path[0], path[-1]
            duration, _distance = self.fleet._gantry_leg(a, b)
            if elapsed < duration:
                return a, b, None, elapsed, duration
            return None                   # arrived (or later) this instant
        before = 0.0
        for a, b, seg in zip(path, path[1:], segments):
            limit = speed if seg.speed is None else min(speed, seg.speed)
            crossing = seg.length / limit
            if elapsed < crossing:
                return a, b, seg, elapsed * limit, before
            elapsed -= crossing
            before += seg.length
        return None                       # it arrived in this same instant

    def exits(self, network, target: str):
        """[(distance back to a node, that node)] this vehicle may leave its
        segment by, nearest to *target* first; empty when it is at a node.

        Network motion only: a gantry (D118) is never mid-segment (it has
        none) and never calls this -- see `distance_to`."""
        at = self.progress()
        if at is None:
            return []
        a, b, seg, along, _before = at
        options = [(seg.length - along, b)]
        if seg.two_way:
            options.append((along, a))
        return sorted(options, key=lambda o: o[0] + network.distance(o[1], target))

    def distance_to(self, network, target: str) -> float:
        """How far this vehicle really is from *target*, mid-segment included.

        A gantry (W3-M7, D118) has no segments to be mid of: this is instead
        its own travel TIME (Chebyshev + hoist) from wherever it is -- exactly
        what its `nearest` dispatch/select rank by, since axis speeds make a
        plain distance meaningless."""
        if self.fleet.motion == "gantry":
            point = self.pos if self.pos is not None else network.point(self.node)
            return self.fleet._gantry_time(point, network.point(target))
        exits = self.exits(network, target)
        if not exits:
            return network.distance(self.node, target)
        back, node = exits[0]
        return back + network.distance(node, target)

    def enter(self, state: str, t: float) -> None:
        self.settle(t)
        self.state = state
        if state == "idle":
            self.idle_since = t

    def settle(self, t: float) -> None:
        """Close the running interval: its time, and what the battery drained
        in it (D164) -- kW of the state it was in x the time, in closed form.
        A vehicle never stops mid-job: SoC may go below zero, and each
        crossing is one "depleted" event (D166)."""
        elapsed = max(t - self._since, 0.0)
        self.times[self.state] += elapsed
        self._since = t
        fleet = self.fleet
        if not fleet.capacity_kwh:
            return
        kwh = drain_kw(fleet.power, self.state) * elapsed * fleet.hours
        if kwh <= 0:
            return
        before = self.soc
        self.soc -= kwh / fleet.capacity_kwh
        self.drain_kwh += kwh
        self.min_soc = min(self.min_soc, self.soc)
        if before > 0 >= self.soc:
            self.depleted += 1
            fleet.warn_once("W_BATTERY_DEPLETED", f"a battery ran empty at t={t:g}; the vehicle "
                            "carries on with a negative charge")

    def reset_stats(self, t: float) -> None:
        """Warm-up: close the running interval, zero the totals, keep the state
        (and the state of charge)."""
        self.settle(t)
        self.times = dict.fromkeys(STATES, 0.0)
        self.distance_empty = 0.0
        self.distance_loaded = 0.0
        self.trips = 0
        # #418: of a vehicle on a charger, the charge it has got to, not the one it
        # plugged in with (`soc` is as of then until it unplugs).
        self.min_soc = (self.soc if self.session is None
                        else self.charger.soc(self.session[0], t))
        self.depleted = 0
        self.drain_kwh = 0.0
        self.charge_trips = 0

    def time_shares(self, t_end: float) -> dict[str, float]:
        """Seconds in each state up to *t_end*, the running interval included."""
        out = dict(self.times)
        out[self.state] = out.get(self.state, 0.0) + max(t_end - self._since, 0.0)
        return out


def _speeds(spec: dict, units: ModelUnits) -> tuple[float, float, float, float]:
    """(speed, speed_empty, speed_x, speed_y) in metres per model time unit; one
    `speed_unit` covers all four (units spec). speed_empty reuses speed when unset."""
    fields = SECTION_FIELDS["fleets"]
    f = field_factor(param(fields, "speed"), spec, units, fields)
    speed = scaled(float(spec.get("speed", 1.0)), f)
    empty = spec.get("speed_empty")
    return (speed, scaled(float(empty), f) if empty else speed,
            scaled(float(spec.get("speed_x") or 0.0), f),
            scaled(float(spec.get("speed_y") or 0.0), f))


class MoverFleet:
    """A pool of vehicles on the network, backed by one resource."""

    def __init__(self, env, spec: dict, network, resource_manager, model_units=None):
        self.env = env
        self.name = str(spec.get("name", ""))
        self.network = network
        # W3-M3: a worker fleet is a fleet whose units may be NAMED, each with
        # its own skills and home; `units` replaces `count` when given.
        self.kind = str(spec.get("kind", "vehicle") or "vehicle")
        units = [u for u in spec.get("units") or [] if isinstance(u, dict)]
        self.count = len(units) if units else max(int(spec.get("count", 1)), 1)
        model_units = model_units or ModelUnits()
        fields = SECTION_FIELDS["fleets"]
        self.speed, self.speed_empty, self.speed_x, self.speed_y = _speeds(spec, model_units)
        # W3-M7 (D118): a gantry crane ignores the network path entirely and
        # travels the two axes independently; `speed`/`speed_empty` above stay
        # required (validation) but are never read for a gantry's own travel.
        self.motion = str(spec.get("motion", "network") or "network")
        # DEVLOG #296: hoist_time is in the fleet's `time_unit`, as documented.
        self.hoist_time = scaled(float(spec.get("hoist_time") or 0.0),
                                 field_factor(param(fields, "hoist_time"), spec, model_units,
                                              fields))
        self.load_time = str(spec.get("load_time", "") or "0")
        self.unload_time = str(spec.get("unload_time", "") or "0")
        self.time_unit = str(spec.get("time_unit", "base"))
        self._time_factor = field_factor(param(fields, "load_time"), spec, model_units, fields)
        self.dispatch = str(spec.get("dispatch", "fifo"))
        self.select = str(spec.get("select", "nearest"))
        self.park = str(spec.get("park", "stay"))
        # W3-M4: loads a vehicle may carry on one tour; 1 is the D68 single trip.
        self.loads = max(int(spec.get("loads", 1) or 1), 1)
        # D168, D170: a fleet with an arm (a motion profile's name) is a mobile
        # cobot; `exchange` is its second holding place (core/tending.py).
        self.arm = str(spec.get("arm") or "").strip()
        self.exchange = bool(spec.get("exchange", False))
        home = str(spec.get("home", "") or "").strip()
        if not home:
            home = next(iter(network.nodes), "")
        self.home = home
        # Battery (D164-D166). `capacity_kwh` <= 0 means the fleet has none
        # (D601, `battery_of`), and nothing below is read. Vehicles drain at the fleet's `power` kW per
        # state; the GRID sees the chargers instead, so a battery fleet's
        # resource carries no power of its own (no double count).
        battery = battery_of(spec)
        self.power = spec.get("power")
        self.capacity_kwh = float(battery.get("capacity_kwh", 0.0))
        self.initial_soc = float(battery.get("initial", 1.0))
        self.low = float(battery.get("low", 0.0))
        self.target = float(battery.get("target", 1.0))
        self.strategy = battery.get("strategy", "threshold")
        self.opportunity_below = (battery.get("opportunity_below")
                                  if self.strategy == "opportunity" else None)
        self.charger_names = battery.get("chargers") or ()
        self.charge_schedule = (env.ctx.schedules.get(battery.get("schedule"))
                                if self.strategy == "scheduled" else None)
        self.hours = conversion_factor("time", model_units.time, "hours")  # per model time unit
        # A swap no block ordered (an idle unit's, #679) draws its swap time from here.
        self._rng = np.random.default_rng(np.random.SeedSequence(
            [env.ctx.seed, deterministic_hash(f"fleet:{self.name}")])) if battery else None
        self._warned: set[str] = set()
        if self.charge_schedule is not None:
            env.process(self._watch_windows())

        self.resource = resource_manager.register(
            self.name, capacity=self.count, discipline="FIFO",
            cost_busy=float(spec.get("cost_busy", 0.0)),
            cost_idle=float(spec.get("cost_idle", 0.0)),
            cost_per_use=float(spec.get("cost_per_use", 0.0)),
            cost_time_unit=spec.get("cost_time_unit", "hours"),
            core_cls=_DispatchResource, power=None if battery else self.power)
        self.resource.emit_state("capacity", self.count)
        if self.dispatch == "nearest":
            self.resource._core.dispatch_key = self._dispatch_key
        if self.kind == "worker":
            self.resource._core.grant = self._grant
        # A shift pattern is the same mechanism a machine uses: the controller
        # holds blockers, so vehicles go off shift as they finish (the WAIT
        # rule), never mid-trip.
        schedule = env.ctx.schedules.get(spec.get("capacity_schedule"))
        if schedule is not None:
            self.resource.has_controller = True
            env.process(capacity_controller(self.resource, schedule))

        self.resource.unit_owner = self      # a blocker takes a NAMED vehicle (T7)

        t = env.now
        if units:
            self.movers = [Mover(self, i, str(u.get("home") or "").strip() or self.home, t,
                                 name=str(u.get("name", "")), skills=u.get("skills") or ())
                           for i, u in enumerate(units)]
        else:
            self.movers = [Mover(self, i, self.home, t) for i in range(self.count)]
        self._free = list(self.movers)
        self._down: list[Mover] = []
        self._wants_charge: set[Mover] = set()     # #679: a held idle plan found no slot
        for mover in self.movers:
            self._watch_idle(mover, start=True)
        if self.capacity_kwh and drain_kw(self.power, "idle") > 0 and env.ctx.sim_horizon:
            # #679: idle timers may outlast the work; one no-op event AT the horizon makes
            # such a run end there, never at whichever stale timer happens to fire last.
            env.timeout(max(env.ctx.sim_horizon - t, 0.0))
        self._cursor = -1
        self._stood = 0            # requests a state put back, counted: their order (D385)
        # Per-segment traversals, keyed by the segment's reported name.
        self.segment_traversals: dict[str, int] = {}
        self.queue_len = TimeWeighted(t)      # requests waiting, for the fleet frame
        self.loads_carried = 0                # loads dispatched, for Loads per Trip
        self._compiled: dict[str, object] = {}
        # D117: the network's shared SegmentTraffic, set by build_fleets only
        # when a segment declares a capacity; None keeps the one-timeout drive.
        self.traffic: SegmentTraffic | None = None

    def __repr__(self):
        return f"<MoverFleet {self.name} x{self.count} speed={self.speed:g}>"

    # -- dispatching ---------------------------------------------------------
    def _dispatch_key(self, request):
        """Sort key for one waiting request under `nearest`.

        Priority first (a blocker's `-inf` keeps it at the head, exactly as
        under fifo), then the shortest empty drive from any idle vehicle. A
        request whose origin is not set yet -- the instant it is made, before
        the caller can stamp it -- sorts last; nothing can be granted at that
        instant anyway, because a free vehicle would have taken it already.
        """
        priority = request.priority[0] if isinstance(request.priority, tuple) else request.priority
        origin = getattr(request, "mover_origin", None)
        if origin is None:
            return (priority, float("inf"))
        tag = getattr(request, "mover_tag", None)
        best = float("inf")
        for mover in self._qualified(tag[1] if tag else None):
            best = min(best, mover.distance_to(self.network, origin))
        return (priority, best)

    def _qualified(self, skill) -> list[Mover]:
        """The idle units that can serve a request needing *skill* (None: any)."""
        if not skill:
            return self._free
        return [m for m in self._free if skill in m.skills]

    def _grant(self, request) -> bool:
        """Worker fleets (W3-M3): may *request* be granted now? If so, reserve
        the unit that serves it. A blocker (no tag) takes whichever unit
        `take_unit` names, exactly as on a vehicle fleet."""
        tag = request.mover_tag
        if tag is None:
            return True
        free = self._qualified(tag[1])
        if not free:
            return False
        request.mover = self._pick(tag[0], free)
        return True

    def _pick(self, origin: str, free: list | None = None) -> Mover:
        """Which idle vehicle (of *free*, default every idle one) serves a
        request picked up at *origin*."""
        if free is None:
            free = self._free
        if self.select == "longest_idle":
            chosen = min(free, key=lambda m: (m.idle_since, m.unit))
        elif self.select == "cyclic":
            start = (self._cursor + 1) % len(self.movers)
            order = sorted(free, key=lambda m: (m.unit - start) % len(self.movers))
            chosen = order[0]
            self._cursor = chosen.unit
        else:                                       # nearest
            chosen = min(free, key=lambda m: (m.distance_to(self.network, origin), m.unit))
        self._free.remove(chosen)
        return chosen

    def attach(self, mover: Mover) -> list:
        """W3-M4 (D116): the jobs of up to `loads - 1` waiting TRANSPORT
        requests, taken off the queue onto *mover*'s tour. Order is the
        fleet's dispatch rule, priority first; `nearest` measures from THIS
        vehicle. Blockers and worker summons carry no job and stay queued."""
        if self.loads <= 1:
            return []
        waiting = [r for r in self.resource._core.put_queue if r.job is not None]
        if not waiting:
            return []
        if self.dispatch == "nearest":
            waiting.sort(key=lambda r: (r.priority,
                                        mover.distance_to(self.network, r.mover_origin), r.time))
        else:
            waiting.sort(key=lambda r: r.key)          # priority, then request time
        taken = waiting[:self.loads - 1]
        for request in taken:
            request.attached = True
            self.resource.serve_without_unit(request)
        self.loads_carried += len(taken)
        return [r.job for r in taken]

    # -- out of service ------------------------------------------------------
    def take_unit(self) -> None:
        """A failure or a shift took one vehicle (PHASE4 T7). It is the one
        standing still that has been standing still longest -- the vehicle a
        yard really would take out of service -- and never one in the middle of
        a job, because a blocker waits for a free unit (the WAIT rule). The down
        TIME is the resource's, unchanged; naming the unit only keeps the
        fleet's POSITIONS right while it is out.

        A blocker granted with every vehicle busy names none: the unit count is
        already right, and the next vehicle to free is the one that stays out.
        """
        if not self._free:
            return
        chosen = min(self._free, key=lambda m: (m.homing is not None, m.idle_since, m.unit))
        if chosen.session is not None:         # an opportunity charge ends with it
            chosen.homing.interrupt()
            chosen.homing = None
            self._unplug(chosen)
        self._free.remove(chosen)
        self._down.append(chosen)

    def give_unit(self) -> None:
        """A repair finished or a shift started: the vehicle comes back where
        it was left."""
        if self._down:
            mover = self._down.pop(0)
            self._free.append(mover)
            # It may have crossed a trigger while out: below one, it plans at once.
            self._watch_idle(mover, start=True)
        if self._wants_charge:
            # `release_blocker` frees the slot only after this returns: retry one event later.
            self.env.timeout(0).callbacks.append(lambda _event: self._retry_wanted())

    # -- the trip ------------------------------------------------------------
    def acquire(self, block, entity, origin: str, priority: int = 0, skill: str | None = None,
                job=None, since=None):
        """Generator: wait for a vehicle (one with *skill*, when given) and
        return the (request, Mover) pair. With *job* (a TRANSPORT's trip) the
        request may instead be ATTACHED to another vehicle's tour (W3-M4): the
        pair is then (request, None) and the tour's lead carries the entity."""
        res = self.resource
        t0 = self.env.now
        self.queue_len.add(1, t0)
        res._core.pending_tag = (origin, skill or None)
        request = res.request(priority=priority, rank=0, since=since)
        if since is not None:       # put back by a state: it asked then, and queues as of then
            request.time = since
            request.key = (request.priority, since, not request.preempt)
            # ... and stands where the state found it (the entries come in the queue's own
            # order): after a grant under "nearest" that is the grant's distance order, not
            # key order, until the next request, and the next grant breaks a tie by it.
            request.stood = self._stood = self._stood + 1
            list.sort(res._core.put_queue, key=lambda r: getattr(r, "stood", math.inf))
        request.mover_origin = origin
        request.job = job
        yield request
        self.queue_len.add(-1, self.env.now)
        if request.attached:
            return request, None
        self.loads_carried += 1
        # A worker fleet reserved its unit at the grant; a vehicle fleet picks now.
        mover = request.mover or self._pick(origin)
        if mover.homing is not None:
            yield from self._divert(mover, origin, block)
        mover.trips += 1
        block.emit("entity_seized", entity=entity, resource=self.name)
        return request, mover

    def drive(self, mover: Mover, to_node: str, *, loaded: bool, block=None,
              interruptible: bool = False, started=None):
        """Generator: move *mover* to *to_node*, accruing distance and time.

        With *interruptible* the drive publishes `mover.trip`, from which
        `Mover.progress` reads the exact position at any instant, and an
        interrupt simply abandons it -- `_divert` has already settled where the
        vehicle is and what it drove. The uninterrupted arithmetic is untouched:
        one timeout for the whole path, as it always was -- unless the path
        crosses a capacity-limited segment (D117), which needs `_drive_zoned`,
        or the fleet is a gantry (D118), which needs neither -- see
        `_drive_gantry`. *started*: a state found the drive under way since then (D384);
        it ends when it was due and is counted as the uninterrupted run counts it.
        """
        if mover.node == to_node and mover.pos is None:
            return 0.0
        if self.motion == "gantry":
            return (yield from self._drive_gantry(mover, to_node, loaded, block, interruptible))
        speed = self.speed if loaded else self.speed_empty
        # The route is the QUICKEST one at this speed where a segment caps one
        # (PHASE4 T7), and the shortest by length where none does.
        path = self.network.path(mover.node, to_node, speed)
        if not path:
            raise RuntimeError(
                f"Fleet '{self.name}': no path from '{mover.node}' to '{to_node}'. "
                f"Add a segment joining them, or check the network is connected.")
        mover.enter("loaded" if loaded else "empty", self.env.now)
        segments = self.network.path_segments(path, speed)
        if self.traffic is not None and self.traffic.limited(segments):
            return (yield from self._drive_zoned(mover, path, segments, speed, loaded, block,
                                                 interruptible))
        duration = self.network.travel_time(path, speed)
        distance = self.network.path_length(path, speed)
        left = self.env.now if started is None else started
        self._moved(block, mover, path, duration if started is None
                    else max(started + duration - self.env.now, 0.0),
                    "loaded" if loaded else "empty")
        mover.leg = (left, to_node, loaded)
        if interruptible:
            # Counted on arrival instead (or, for a diverted vehicle, only the
            # segments it really crossed): `segment_summary` reports traversals
            # times length, so half a segment must not be one.
            mover.trip = (left, path, segments, speed)
        elif started is None:
            self._crossed(segments)
        try:
            yield (self.env.timeout(duration) if started is None
                   else timeout_at(self.env, started + duration))
        except simpy.Interrupt:
            if not interruptible:
                raise
            return 0.0             # `_divert` owns the position and the distance
        if interruptible:
            self._crossed(segments)
        mover.trip = mover.leg = None
        mover.node = to_node
        if loaded:
            mover.distance_loaded += distance
        else:
            mover.distance_empty += distance
        return duration

    def _drive_zoned(self, mover: Mover, path, segments, speed, loaded, block, interruptible):
        """Generator: *path* one segment at a time under zone control (D117).

        Before each limited segment the mover asks for it, standing at the end
        of the one it is on and still holding it; entering the next -- or
        arriving -- releases it. Position, distance and traversals advance
        per segment, and `mover.trip` covers only the segment being crossed
        (None while waiting at a node), so `Mover.progress` and `_divert` read
        the right place at any instant. One `mover_moved` per segment, plus a
        stationary one when a wait starts, so playback holds the mover still.
        """
        traffic, env = self.traffic, self.env
        state = "loaded" if loaded else "empty"
        started = env.now
        try:
            for a, b, seg in zip(path, path[1:], segments):
                if seg in traffic.resources:
                    request = traffic.request(mover, seg)
                    if not request.triggered:
                        self._moved(block, mover, (a,), 0.0, state)
                    yield request
                    traffic.entered(mover)
                else:
                    traffic.release_all(mover)
                crossing = seg.length / (speed if seg.speed is None else min(speed, seg.speed))
                mover.trip = (env.now, [a, b], [seg], speed)
                self._moved(block, mover, (a, b), crossing, state)
                yield env.timeout(crossing)
                mover.trip = None
                mover.node = b
                if loaded:
                    mover.distance_loaded += seg.length
                else:
                    mover.distance_empty += seg.length
                self._crossed((seg,))
        except simpy.Interrupt:
            if not interruptible:
                traffic.cancel(mover)
                traffic.release_all(mover)
                raise
            return 0.0             # `_divert` owns the position, the holds and the distance
        traffic.release_all(mover)
        return env.now - started

    def _drive_gantry(self, mover: Mover, to_node: str, loaded: bool, block, interruptible: bool):
        """Generator: one direct gantry hop (W3-M7, D118) -- node coordinates
        only, no path-finding and no segment traffic, since a gantry needs
        neither. `mover.pos`, when set, is where an interrupted park-home
        return left it (`_settle_gantry_divert`, linear interpolation);
        otherwise the hop starts from its node, exactly like any other fleet.
        """
        origin = mover.pos if mover.pos is not None else self.network.point(mover.node)
        dest = self.network.point(to_node)
        duration = self._gantry_time(origin, dest)
        distance = math.hypot(dest[0] - origin[0], dest[1] - origin[1])
        state = "loaded" if loaded else "empty"
        mover.enter(state, self.env.now)
        self._moved(block, mover, (mover.node, to_node), duration, state)
        if interruptible:
            mover.trip = (self.env.now, [mover.node, to_node], (), None)
        try:
            yield self.env.timeout(duration)
        except simpy.Interrupt:
            if not interruptible:
                raise
            return 0.0              # `_settle_gantry_divert` owns the position and the distance
        mover.trip = None
        mover.pos = None
        mover.node = to_node
        if loaded:
            mover.distance_loaded += distance
        else:
            mover.distance_empty += distance
        return duration

    def _gantry_time(self, p: tuple[float, float], q: tuple[float, float]) -> float:
        """Chebyshev travel time between two POINTS, plus the hoist (D118):
        the x and y axes move independently and simultaneously, so the hop is
        bounded by the SLOWER axis, never their combined (Euclidean) length."""
        dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
        return max(dx / self.speed_x, dy / self.speed_y) + self.hoist_time

    def _gantry_leg(self, a: str, b: str) -> tuple[float, float]:
        """(duration, straight-line distance) for the direct hop node *a* ->
        *b*: the time above, and the Euclidean distance for the honest
        `fleet_summary` total (D118 -- Chebyshev-free reporting: the crane
        does not really travel the diagonal, so the total is not inflated by
        pretending it did)."""
        p, q = self.network.point(a), self.network.point(b)
        return self._gantry_time(p, q), math.hypot(q[0] - p[0], q[1] - p[1])

    def leg(self, a: str, b: str, speed: float, pos=None) -> tuple[list, float]:
        """(path, travel time) for one hop at *speed*, worked out BEFORE the
        hop is actually driven -- what a TRANSPORT's `entity_moved` needs for
        the ride it is about to announce. The network's quickest route, or
        (D118) a gantry's direct hop, which has no path to find at all --
        from *pos* when a diverted gantry is mid-hop (`mover.pos`, #194)."""
        if self.motion == "gantry":
            if pos is not None:
                return [a, b], self._gantry_time(pos, self.network.point(b))
            duration, _distance = self._gantry_leg(a, b)
            return [a, b], duration
        path = self.network.path(a, b, speed)
        return path, self.network.travel_time(path, speed)

    def leg_distance(self, a: str, b: str, pos=None) -> float:
        """What a tour's nearest-neighbour ordering (D116) sorts stops by:
        network length, or (D118) a gantry's own travel time -- independent
        axis speeds make a plain length meaningless for "nearest"."""
        if self.motion == "gantry":
            return self.leg(a, b, 0.0, pos)[1]
        return self.network.distance(a, b)

    def _moved(self, block, mover: Mover, path, duration: float, state: str) -> None:
        """`mover_moved` for playback: the leg *mover* starts now. *block* is
        the one that ordered it, or None for a drive the fleet decided itself
        (a scheduled window's charge)."""
        bus = self.env.ctx.bus
        if bus.has_subscribers:
            bus.emit("mover_moved", block=block, t=self.env.now, resource=self.name,
                     unit=mover.unit, path=tuple(path), duration=duration, state=state)

    def _crossed(self, segments) -> None:
        for segment in segments:
            self.segment_traversals[segment.name] = self.segment_traversals.get(segment.name, 0) + 1

    def _divert(self, mover: Mover, origin: str, block=None):
        """Generator: stop a vehicle on its way home and bring it to the node it
        will route from -- whichever end of its segment reaches *origin*
        soonest, and only the end a one-way segment allows (PHASE4 T7). A
        gantry (D118) has no segment to back off: `_settle_gantry_divert`
        reads its exact position instead."""
        mover.homing.interrupt()
        mover.homing = mover.leg = None
        if mover.session is not None:           # dispatched off an opportunity charge
            self._unplug(mover)
        mover.charger = None
        if self.motion == "gantry":
            self._settle_gantry_divert(mover)
            return
        traffic = self.traffic
        if traffic is not None:
            # Now, not when the interrupt lands: this process may ask for a
            # segment before the homing drive hears it (D117).
            traffic.cancel(mover)
        at = mover.progress()
        exits = mover.exits(self.network, origin)
        trip, mover.trip = mover.trip, None
        if at is None or not exits:
            if trip is not None:            # it got home in this same instant
                _t, path, segments, _speed = trip
                mover.node = path[-1]
                mover.distance_empty += sum(s.length for s in segments)
                self._crossed(segments)
            if traffic is not None:
                traffic.release_all(mover)
            return
        a, _b, seg, along, before = at
        mover.node = a
        mover.distance_empty += before + along
        # Whole segments only: the one it is standing on was not crossed, and
        # the drive back over it below is not a traversal either.
        self._crossed(trip[2][:trip[1].index(a)])
        back, node = exits[0]
        mover.enter("empty", self.env.now)
        if back > 0:
            limit = self.speed_empty if seg.speed is None else min(self.speed_empty, seg.speed)
            self._moved(block, mover, (a, node), back / limit, "empty")
            yield self.env.timeout(back / limit)
            mover.distance_empty += back
        mover.node = node
        if traffic is not None:
            traffic.release_all(mover)     # it held its segment while backing off it

    def _settle_gantry_divert(self, mover: Mover) -> None:
        """Where a gantry's park-home return sits when it is interrupted
        (D118): linear interpolation between the two nodes of the hop it was
        on -- no segment to be mid-way ON, so no backing off, and no extra
        travel time either. The vehicle's own next `drive()` starts from
        `mover.pos` with no further delay, exactly as a real crane could set
        off again from wherever its bridge happens to be.

        Ceiling: the traced `path` of that NEXT hop still names the interrupted
        leg's departure node, not the true interpolated point -- KPI distance
        and time stay exact; only that one leg's drawn path is approximate
        (upgrade path: a synthetic point in the trace)."""
        at = mover.progress()
        trip, mover.trip = mover.trip, None
        if at is not None:
            a, b, _seg, elapsed, duration = at
            f = 0.0 if duration <= 0 else min(elapsed / duration, 1.0)
            pa, pb = self.network.point(a), self.network.point(b)
            mover.pos = (pa[0] + (pb[0] - pa[0]) * f, pa[1] + (pb[1] - pa[1]) * f)
            mover.distance_empty += math.hypot(pb[0] - pa[0], pb[1] - pa[1]) * f
        elif trip is not None:              # it got home in this same instant
            _t, path, _segments, _speed = trip
            _duration, distance = self._gantry_leg(path[0], path[-1])
            mover.node = path[-1]
            mover.distance_empty += distance
        mover.enter("empty", self.env.now)

    def park_home(self, mover: Mover, block=None) -> None:
        """Send a FREE vehicle back to its home node (`park: "home"`).

        The vehicle is already available: the drive is its own interruptible
        process, so a request arriving mid-return diverts it instead of waiting
        for it to finish going somewhere nobody asked for. The return is empty
        travel, not idle time (PHASE4 T7).
        """
        if self.park != "home" or mover.node == mover.home or mover.charger is not None:
            return                # (a vehicle off to charge parks when it is done)
        mover.homing = self.env.process(self._go_home(mover, block))

    def _go_home(self, mover: Mover, block=None, started=None):
        try:
            yield from self.drive(mover, mover.home, loaded=False, block=block,
                                  interruptible=True, started=started)
        except simpy.Interrupt:                 # interrupted before the drive began
            return
        if mover.homing is not None:            # it made it home undisturbed
            mover.homing = None
            mover.enter("idle", self.env.now)
            self._watch_idle(mover)

    def handle(self, mover: Mover, seconds: float, until=None):
        """Generator: load or unload for *seconds* (the vehicle is busy, still), or, put
        back by a state, until the absolute time *until* it was due to end at."""
        if seconds <= 0 and until is None:
            return
        now = self.env.now
        mover.enter("handling", now)
        mover.until = now + seconds if until is None else until
        self._moved(None, mover, (mover.node,), seconds if until is None
                    else max(until - now, 0.0), "handling")
        yield self.env.timeout(seconds) if until is None else timeout_at(self.env, until)

    def free(self, request, mover: Mover, block=None, entity=None) -> None:
        """Give the vehicle back. `park: "home"` has already driven it home.

        A battery vehicle (D164-D166) may go to charge instead: HELD (below
        `low`, or freed inside a scheduled window) it keeps its resource slot
        until it has charged, so the fleet never grants a vehicle it does not
        have; otherwise an opportunity charge leaves it free and dispatchable."""
        mover.enter("idle", self.env.now)
        mover.task = None
        self._wants_charge.discard(mover)
        plan = self._charge_plan(mover) if self.capacity_kwh else None
        held = plan is not None and plan[1]
        if not held:
            self._free.append(mover)
            self.resource.release(request)
            self._retry_wanted()
        if block is not None:
            block.emit("entity_released", entity=entity, resource=self.name)
        self._moved(block, mover, (mover.node,), 0.0, "idle")
        if self.capacity_kwh:
            self._emit("battery", mover, self.name)
        if plan is None:
            if self.capacity_kwh:
                self._watch_idle(mover)
            return
        mover.charger, _held, until = plan
        if held:
            self.env.process(self._charge_held(request, mover, until, block, entity))
        else:
            mover.homing = self.env.process(self._charge_free(mover, until, block))

    # -- battery and charging (D164-D166) -------------------------------------
    def warn_once(self, code: str, text: str) -> None:
        """One run-log line per fleet and code, however often it happens."""
        if code not in self._warned:
            self._warned.add(code)
            log.warning(f"{code}: fleet '{self.name}': {text}")

    def _emit(self, name: str, mover: Mover, resource: str) -> None:
        """`battery` (resource = the fleet) and `charge_start`/`charge_end`
        (resource = the charger), each with the vehicle's SoC as `value`."""
        bus = self.env.ctx.bus
        if bus.has_subscribers:
            bus.emit(name, block=None, t=self.env.now, fleet=self.name, unit=mover.unit,
                     resource=resource, value=mover.soc)

    def _charge_plan(self, mover: Mover):
        """(charger, held, until) for a vehicle that has just gone idle, or
        None when it does not charge. *until* is the end of the scheduled
        window the charge may not outlast (inf otherwise)."""
        now, until = self.env.now, math.inf
        schedule = self.charge_schedule
        if schedule is not None and schedule.value_at(now) > 0 and mover.soc < self.target:
            held, until = True, self._window_end(now)
        elif mover.soc < self.low:
            held = True
        elif (self.opportunity_below is not None
              and mover.soc < min(self.opportunity_below, self.target)):
            held = False
        else:
            return None
        charger = self._charger_for(mover, free_only=not held)
        return None if charger is None else (charger, held, until)

    def _window_end(self, t: float) -> float:
        """When the scheduled charging window open at *t* closes; adjacent
        points with a value > 0 are one window."""
        schedule = self.charge_schedule
        end = schedule.next_change_after(t)
        for _ in schedule.values:
            if end == math.inf or schedule.value_at(end) <= 0:
                break
            end = schedule.next_change_after(end)
        return end

    def _charger_for(self, mover: Mover, free_only: bool = False):
        """The charger *mover* goes to: of the kind its strategy needs, allowed
        and reachable, the least drive time plus expected wait -- its place in
        line behind the vehicles on a point, waiting for one or on their way
        (#313), times the mean stay, over the points -- ties by name.
        *free_only*: a point will be free when it gets there."""
        kind = "swap" if self.strategy == "swap" else "charge"
        best, reachable = None, False
        for name, charger in sorted(self.env.ctx.chargers.items()):
            if charger.kind != kind or (self.charger_names and name not in self.charger_names):
                continue
            path, drive = self.leg(mover.node, charger.node, self.speed_empty)
            if not path:
                continue
            reachable = True
            heading = sum(1 for m in self.movers if m.charger is charger and m.session is None
                          and m.node != charger.node)
            ahead = charger.points.count + len(charger.points.queue) + heading
            if free_only and ahead >= charger.capacity:
                continue
            # Before the first session ends, the stay is what this fleet's own
            # charge takes (a swap station's pack recharge: pessimistic, briefly).
            stay = (charger.stay.mean if charger.stay.count
                    else charger.duration(self.capacity_kwh, self.low, self.target))
            cost = drive + max(ahead - charger.capacity + 1, 0) * stay / charger.capacity
            if best is None or cost < best[0]:
                best = (cost, charger)
        if not reachable:
            self.warn_once("W_NO_CHARGER", f"no {kind} charger can be reached from "
                           f"'{mover.node}'; the vehicle stays free and does not charge")
        return best[1] if best else None

    def _plug(self, mover: Mover, point, arrived: float, until: float) -> float:
        """*mover* starts charging on *point* of its charger; returns how long
        the charge takes: up to `target`, or to *until* if that comes first."""
        charger, now = mover.charger, self.env.now
        charger.occupy(arrived)
        mover.enter("charging", now)
        mover.session = (charger.plug(self.capacity_kwh, mover.soc, self.target), point)
        self._emit("charge_start", mover, charger.name)
        lasts = min(charger.duration(self.capacity_kwh, mover.soc, self.target),
                    max(until - now, 0.0))
        mover.until = now + lasts
        return lasts

    def _unplug(self, mover: Mover) -> None:
        """The charge ends now, complete or cut short: SoC and grid energy are
        settled to this instant and the point is free."""
        charger = mover.charger
        session, point = mover.session
        mover.soc = charger.unplug(session)
        mover.session = mover.charger = None
        charger.leave(point, session[2])
        mover.enter("idle", self.env.now)
        self._emit("charge_end", mover, charger.name)

    def _charge_held(self, request, mover: Mover, until: float, block, entity, started=None):
        """Generator: drive to the charger, wait for a point, charge (or swap
        the pack), and only then give the vehicle back. Waiting is idle time
        at idle power; the drive is empty travel."""
        env, charger = self.env, mover.charger
        yield from self.drive(mover, charger.node, loaded=False, block=block, started=started)
        mover.charge_trips += 1
        mover.enter("idle", env.now)
        arrived = env.now
        charger.queue_len.add(1, arrived)
        point = charger.points.request()
        point.mover = mover                 # D383: who waits for a point, for a state
        yield point
        if charger.packs is not None:
            yield charger.packs.get(1)             # a swap waits for a charged pack
        charger.queue_len.add(-1, env.now)
        if charger.kind == "swap":
            started = env.now
            charger.occupy(arrived)
            mover.enter("charging", started)
            self._emit("charge_start", mover, charger.name)
            scope = (block.expression_scope(entity) if block is not None
                     else ExpressionScope(ctx=env.ctx, rng=self._rng))
            yield env.timeout(charger.swap_time(scope))
            env.process(charger.recharge(self.capacity_kwh, mover.soc))
            mover.soc, mover.charger = 1.0, None
            charger.leave(point, started)
            mover.enter("idle", env.now)
            self._emit("charge_end", mover, charger.name)
        else:
            yield env.timeout(self._plug(mover, point, arrived, until))
            self._unplug(mover)
        self._charged(request, mover, block)

    def _charged(self, request, mover: Mover, block) -> None:
        """A held charge is over: the vehicle is given back."""
        self._free.append(mover)
        self.resource.release(request)
        self._moved(block, mover, (mover.node,), 0.0, "idle")
        self.park_home(mover, block)
        self._watch_idle(mover)
        self._retry_wanted()

    # -- put back by a state (D384) --------------------------------------------
    def claim(self, mover: Mover):
        """The unit of a vehicle a state found busy: asked for now, with the fleet's units
        all free, so granted before anything queues. The caller yields it."""
        self._free.remove(mover)
        return self.resource.request(priority=0, rank=0)

    def restore(self, spec: dict, busy: set) -> None:
        """Put every vehicle where the state *spec* found it; the units in *busy* are on a
        trip, which `Transport.resume` carries on. Called before any entity is put back,
        the vehicles on a point first and those waiting for one in their order."""
        env, now, chargers = self.env, self.env.now, self.env.ctx.chargers
        if spec.get("cursor") is not None:
            self._cursor = int(spec["cursor"])
        waiting = []
        for mover, record in zip(self.movers, spec.get("vehicles") or []):
            mover.node = record["node"]
            if record.get("soc") is not None:
                mover.soc = mover.min_soc = float(record["soc"])
            drive, charge = record.get("drive"), record.get("charge")
            if charge is not None:
                mover.charger = chargers[charge["charger"]]
                stage = charge.get("stage", "charge")
                held = bool(charge.get("held", True))
                request = None
                if held:
                    env.ctx.settling += 1
                    request = self.claim(mover)
                if stage == "charge":
                    self._plug(mover, mover.charger.points.request(), now, math.inf)
                    mover.until = float(charge["until"])
                    work = (self._resume_charge(request, mover, None) if held
                            else self._resume_free_charge(mover))
                elif stage == "wait":
                    mover.enter("idle", now)
                    waiting.append((charge.get("wait", 0), mover.unit, mover, request,
                                    record.get("idle_since", now)))
                    continue
                elif held:
                    work = self._resume_drive_to_charge(request, mover, drive["started"])
                else:
                    work = self._charge_free(mover, math.inf, None, drive["started"])
                process = env.process(work)
                if not held:
                    mover.homing = process
            elif mover.unit not in busy and drive is not None:      # on its way home
                mover.homing = env.process(self._go_home(mover, None, drive["started"]))
            if record.get("idle_since") is not None:
                mover.idle_since = float(record["idle_since"])
        for _place, _unit, mover, request, arrived in sorted(waiting, key=lambda w: w[:2]):
            mover.charger.queue_len.add(1, now)
            point = mover.charger.points.request()
            point.mover = mover
            mover.idle_since = float(arrived)
            env.process(self._resume_charge(request, mover, point))
        for mover in self.movers:              # #679: as of the SoC the state put back
            # A busy unit never plans at once: `Transport.resume` claims it after this.
            self._watch_idle(mover, start=mover.unit not in busy)

    def _resume_drive_to_charge(self, request, mover: Mover, started: float):
        yield request
        self.env.ctx.settling -= 1
        yield from self._charge_held(request, mover, math.inf, None, None, started)

    def _resume_charge(self, request, mover: Mover, point):
        """Generator: a held charge a state found waiting for *point* (it arrived at
        `idle_since`), or with no *point* on one until `mover.until`: `_charge_held` from
        there."""
        env, charger = self.env, mover.charger
        yield request
        env.ctx.settling -= 1
        if point is not None:
            yield point
            charger.queue_len.add(-1, env.now)
            yield env.timeout(self._plug(mover, point, mover.idle_since, math.inf))
        else:
            yield timeout_at(env, mover.until)
        self._unplug(mover)
        self._charged(request, mover, None)

    def _resume_free_charge(self, mover: Mover):
        """Generator: an opportunity charge a state found on its point: the end of
        `_charge_free`."""
        try:
            yield timeout_at(self.env, mover.until)
            self._unplug(mover)
        except simpy.Interrupt:
            return
        mover.homing = mover.charger = None
        mover.enter("idle", self.env.now)
        self.park_home(mover, None)
        self._watch_idle(mover)

    def _charge_free(self, mover: Mover, until: float, block, started=None):
        """Generator: an opportunity charge. The vehicle is FREE throughout,
        like one driving home: a dispatch (`_divert`) or a blocker
        (`take_unit`) interrupts it, having itself settled where the vehicle
        is and what it charged. A point taken while it drove means no charge."""
        charger = mover.charger
        try:
            yield from self.drive(mover, charger.node, loaded=False, block=block,
                                  interruptible=True, started=started)
            if mover.homing is None:
                return                             # diverted on the way
            if mover in self._free and charger.points.count < charger.capacity:
                mover.charge_trips += 1
                yield self.env.timeout(self._plug(mover, charger.points.request(),
                                                  self.env.now, until))
                self._unplug(mover)
        except simpy.Interrupt:
            return
        mover.homing = mover.charger = None
        mover.enter("idle", self.env.now)
        self.park_home(mover, block)
        self._watch_idle(mover)

    def _watch_idle(self, mover: Mover, start: bool = False) -> None:
        """#679: a FREE unit standing idle plans a charge, as a release does
        (`_charge_plan`), at the exact instant its idle drain takes its SoC below a
        trigger: `low`, or `opportunity_below` (a scheduled window's opening is
        `_watch_windows`'s). One closed-form timer per idle stretch; a newer one, or
        the unit moving on, makes it a no-op. Only a trigger strictly below the SoC
        now is armed, so a unit that has just charged to it, or found no charger,
        never chases its own drain in zero time. *start*: the run's first instant,
        where a unit already below a trigger plans at once."""
        kw = drain_kw(self.power, "idle") if self.capacity_kwh else 0.0
        horizon = self.env.ctx.sim_horizon
        # ponytail: a run with no length (a model that drains on its own) arms nothing,
        # since an idle drain-and-charge cycle would never let it end.
        if kw <= 0 or horizon is None or mover.state != "idle":
            return
        mover.watch += 1
        now, token = self.env.now, mover.watch
        rate = kw * self.hours / self.capacity_kwh            # SoC per model time unit
        soc = mover.soc - rate * (now - mover._since)
        triggers = [self.low]
        if self.opportunity_below is not None:
            triggers.append(min(self.opportunity_below, self.target))
        ahead = [s for s in triggers if s < soc]
        if start and len(ahead) < len(triggers):
            trigger, delay = None, 0.0
        elif ahead:
            trigger = max(ahead)
            delay = (soc - trigger) / rate
        else:
            return
        if now + delay <= horizon:
            self.env.timeout(delay).callbacks.append(
                lambda _event: self._idle_due(mover, token, trigger))

    def _idle_due(self, mover: Mover, token: int, trigger) -> None:
        """An idle trigger fires: plan the charge a release would, if the unit has
        stood idle and free since it was armed."""
        if (mover.watch != token or mover.state != "idle" or mover.homing is not None
                or mover.charger is not None or mover not in self._free):
            return
        mover.settle(self.env.now)
        if trigger is not None:
            # Crossed right now: rounding must not leave it a hair above the trigger.
            before = mover.soc
            mover.soc = min(mover.soc, math.nextafter(trigger, -math.inf))
            mover.min_soc = min(mover.min_soc, mover.soc)
            if before > 0 >= mover.soc:        # low 0: this is the crossing `settle` counts
                mover.depleted += 1
                self.warn_once("W_BATTERY_DEPLETED", f"a battery ran empty at "
                               f"t={self.env.now:g}; the vehicle carries on with a negative "
                               "charge")
        plan = self._charge_plan(mover)
        if plan is None:
            self._watch_idle(mover)
            return
        mover.charger, held, until = plan
        if not held:
            mover.homing = self.env.process(self._charge_free(mover, until, None))
            return
        # Held, it keeps a slot as a released unit does: busy, but no seize and no wait.
        request = self.resource.request(priority=-math.inf, counted=False, charged=False)
        if not request.triggered:          # every free slot is taken this instant
            self.resource.cancel(request)
            mover.charger = None
            # Tried again when a slot frees, not at once: that could loop in one instant.
            self._wants_charge.add(mover)
            return
        self._free.remove(mover)
        self.env.process(self._charge_held(request, mover, until, None, None))

    def _retry_wanted(self) -> None:
        """A slot has freed: the units whose held idle plan found none try again."""
        for mover in sorted(self._wants_charge, key=lambda m: m.unit):
            self._wants_charge.discard(mover)
            self._idle_due(mover, mover.watch, None)

    def _watch_windows(self):
        """Generator (scheduled strategy): when a charging window opens, each
        vehicle standing idle below `target` starts an interruptible charge if
        a point is free. A vehicle freed DURING the window is `free`'s case."""
        schedule = self.charge_schedule
        while True:
            now = self.env.now
            if schedule.value_at(now) > 0:
                until = self._window_end(now)
                for mover in [m for m in self._free if m.homing is None]:
                    mover.settle(now)
                    charger = (self._charger_for(mover, free_only=True)
                               if mover.soc < self.target else None)
                    if charger is not None:
                        mover.charger = charger
                        mover.homing = self.env.process(self._charge_free(mover, until, None))
            change = schedule.next_change_after(now)
            if change == math.inf:
                return
            yield self.env.timeout(change - now)

    def duration(self, block, entity, expr: str) -> float:
        """A load/unload time expression, in the fleet's own time unit."""
        text = (expr or "").strip()
        if not text:
            return 0.0
        compiled = self._compiled.get(text)
        if compiled is None:
            compiled = self._compiled[text] = compile_expression(text)
        try:
            value = float(compiled.evaluate(block.expression_scope(entity)))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Fleet '{self.name}': {exc}") from None
        value = scaled(value, self._time_factor)
        return max(value, 0.0)

    # -- statistics ----------------------------------------------------------
    def reset_stats(self, t: float) -> None:
        for mover in self.movers:
            mover.reset_stats(t)
        self.segment_traversals.clear()
        self.queue_len.reset(t)
        if self.traffic is not None:
            self.traffic.reset_stats()
        self.loads_carried = 0

    def totals(self, t_end: float) -> dict:
        """What the `fleet_summary` frame reports."""
        shares = dict.fromkeys(STATES, 0.0)
        for mover in self.movers:
            for state, value in mover.time_shares(t_end).items():
                shares[state] += value
        span = sum(shares.values()) or 1.0
        wait = self.resource.wait
        trips = sum(m.trips for m in self.movers)
        return {
            "trips": trips,
            "loads_per_trip": self.loads_carried / trips if trips else float("nan"),
            "distance_empty": sum(m.distance_empty for m in self.movers),
            "distance_loaded": sum(m.distance_loaded for m in self.movers),
            "empty_pct": 100.0 * shares["empty"] / span,
            "loaded_pct": 100.0 * shares["loaded"] / span,
            "handling_pct": 100.0 * shares["handling"] / span,
            "idle_pct": 100.0 * shares["idle"] / span,
            "avg_wait": wait.mean,
            "max_wait": wait.max,
        }

    def battery_totals(self, t_end: float) -> dict:
        """What the `battery_summary` frame reports (D164, D166)."""
        movers = self.movers
        for mover in movers:
            mover.settle(t_end)                 # drain up to the report instant
        span = sum(sum(m.times.values()) for m in movers) or 1.0
        # A charge still running has got to where its closed form says.
        final = [m.charger.soc(m.session[0], t_end) if m.session else m.soc for m in movers]
        return {
            "charging_pct": 100.0 * sum(m.times["charging"] for m in movers) / span,
            "charge_trips": sum(m.charge_trips for m in movers),
            "min_soc": min(m.min_soc for m in movers),
            "final_soc": sum(final) / len(final),
            "depleted": sum(m.depleted for m in movers),
            "drain_kwh": sum(m.drain_kwh for m in movers),
        }


def build_fleets(env, layout: dict, network, resource_manager) -> dict[str, MoverFleet]:
    """{name: MoverFleet} for the layout's "fleets" section, once per run."""
    out: dict[str, MoverFleet] = {}
    traffic = (SegmentTraffic(env, network)
               if any(seg.capacity for seg in network.segments) else None)
    for spec in layout.get("fleets", []) or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name", "")).strip()
        if not name:
            continue
        out[name] = MoverFleet(env, spec, network, resource_manager, env.ctx.units)
        out[name].traffic = traffic
        log.info(f"Imported mover fleet: {name} ({out[name].count} vehicles, "
                 f"dispatch={out[name].dispatch})")
    return out


# -- validation --------------------------------------------------------------

def validate_fleets_structured(layout: dict, nodes: set,
                               schedule_kinds: dict | None = None) -> tuple[list, set]:
    """Check the "fleets" section. Returns (list[Problem], {fleet names})."""
    problems: list = []
    names: set[str] = set()
    schedule_kinds = schedule_kinds or {}
    declared = {str(r.get("name", "")) for r in layout.get("resources", []) or []}
    declared |= {str(s.get("name", "")) for s in layout.get("resource_sets", []) or []}
    known_nodes = ", ".join(sorted(nodes)) or "(none)"

    for index, spec in enumerate(layout.get("fleets", []) or []):
        at = f"/fleets/{index}"
        if not isinstance(spec, dict):
            problems.append(problem("E_FLEET_FIELD", f"fleet {index} is not an object", at,
                                    hint="a fleet is {\"name\": ..., \"count\": 2, \"speed\": 1.5}"))
            continue
        name = spec.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_FLEET_NAME", "fleet: name must not be empty", f"{at}/name",
                                    hint="give every fleet a unique name"))
            continue
        name = name.strip()
        if name in names:
            problems.append(problem("E_FLEET_NAME", f"duplicate fleet name: {name}", f"{at}/name",
                                    hint="rename one of the two fleets"))
        if name in declared:
            problems.append(problem(
                "E_FLEET_NAME", f"fleet '{name}' clashes with a resource of the same name",
                f"{at}/name",
                hint="a fleet IS a resource: it must not be declared twice"))
        names.add(name)

        count = spec.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            problems.append(problem("E_FLEET_FIELD", f"fleet '{name}': count must be an integer >= 1",
                                    f"{at}/count", hint="how many vehicles the pool holds"))
        kind = spec.get("kind", "vehicle")
        if kind not in FLEET_KINDS:
            problems.append(problem("E_FLEET_FIELD", f"fleet '{name}': unknown kind '{kind}'",
                                    f"{at}/kind", hint=f"one of: {', '.join(FLEET_KINDS)}"))
        loads = spec.get("loads", 1)
        if not isinstance(loads, int) or isinstance(loads, bool) or loads < 1:
            problems.append(problem("E_FLEET_LOADS", f"fleet '{name}': loads must be an integer "
                                    ">= 1", f"{at}/loads",
                                    hint="how many entities one vehicle carries on a tour"))
        elif loads > 1 and kind == "worker":
            # D116: a worker is summoned to a station one job at a time, and the
            # skill check reserves a unit per request; no case asks for more.
            problems.append(problem("E_FLEET_LOADS", f"fleet '{name}': a worker fleet carries "
                                    "one load at a time", f"{at}/loads",
                                    hint="drop loads, or use a fleet of kind \"vehicle\""))
        problems.extend(_validate_motion(spec, name, at, kind))
        problems.extend(_validate_units(spec, name, at, nodes, known_nodes))
        for field in ("speed", "speed_empty"):
            value = spec.get(field)
            if field == "speed" and value is None:
                problems.append(problem("E_FLEET_FIELD", f"fleet '{name}': speed is required",
                                        f"{at}/speed",
                                        hint="distance per time unit, in the network's own units"))
            elif value is not None and (not isinstance(value, (int, float))
                                        or isinstance(value, bool) or value <= 0):
                problems.append(problem("E_FLEET_FIELD",
                                        f"fleet '{name}': {field} must be a number > 0",
                                        f"{at}/{field}",
                                        hint="distance per time unit; omit speed_empty to reuse speed"))
        for field, allowed in (("dispatch", DISPATCH_RULES), ("select", SELECT_RULES),
                               ("park", PARK_RULES)):
            value = spec.get(field)
            if value is not None and value not in allowed:
                problems.append(problem(
                    "E_FLEET_FIELD", f"fleet '{name}': unknown {field} rule '{value}'",
                    f"{at}/{field}", hint=f"one of: {', '.join(allowed)}"))
        schedule_name = spec.get("capacity_schedule")
        if schedule_name and schedule_kinds.get(schedule_name) != "capacity":
            problems.append(problem(
                "E_FLEET_FIELD", f"fleet '{name}': unknown capacity schedule '{schedule_name}'",
                f"{at}/capacity_schedule",
                hint="declare a schedule with kind \"capacity\" under \"schedules\""))
        home = spec.get("home")
        if home is not None and str(home).strip() and str(home).strip() not in nodes:
            problems.append(problem("E_FLEET_NODE", f"fleet '{name}': unknown home node '{home}'",
                                    f"{at}/home",
                                    hint=f"name a declared network node: {known_nodes}"))
        if not nodes:
            problems.append(problem(
                "E_FLEET_NODE", f"fleet '{name}': the model declares no path network",
                "/network",
                hint="add a \"network\" section with nodes and segments for the vehicles to "
                     "drive on"))
        power = spec.get("power")
        if power is not None:
            if not isinstance(power, dict):
                problems.append(problem(
                    "E_FLEET_POWER", f"fleet '{name}': power must be an object",
                    f"{at}/power", hint="{\"busy\": 5.0, \"idle\": 1.0} -- kW per vehicle"))
            else:
                for state, value in power.items():
                    if state not in ENERGY_STATES:
                        problems.append(problem(
                            "E_FLEET_POWER_STATE", f"fleet '{name}': unknown power state '{state}'",
                            f"{at}/power/{state}", hint=f"one of: {', '.join(ENERGY_STATES)}"))
                    elif not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                        problems.append(problem(
                            "E_FLEET_POWER", f"fleet '{name}': power.{state} must be a number >= 0",
                            f"{at}/power/{state}", hint="kW for one vehicle in that state"))
    return problems, names


def _validate_motion(spec: dict, name: str, at: str, kind: str) -> list:
    """A fleet's `motion` (W3-M7, D118): "network" (default) or "gantry",
    which trades the path network for `speed_x`/`speed_y`/`hoist_time` and
    cannot be a worker fleet (a worker walks the network to its station;
    "walks" makes no sense for a bridge crane)."""
    motion = spec.get("motion", "network") or "network"
    if motion not in FLEET_MOTIONS:
        return [problem("E_FLEET_MOTION", f"fleet '{name}': unknown motion '{motion}'",
                        f"{at}/motion", hint=f"one of: {', '.join(FLEET_MOTIONS)}")]
    if motion != "gantry":
        return []
    out = []
    if kind == "worker":
        out.append(problem("E_FLEET_MOTION", f"fleet '{name}': a gantry crane cannot be a worker "
                           "fleet", f"{at}/motion", hint="drop motion, or use kind \"vehicle\""))
    for field in ("speed_x", "speed_y"):
        value = spec.get(field)
        if value is None or not isinstance(value, (int, float)) or isinstance(value, bool) \
                or value <= 0:
            out.append(problem("E_FLEET_MOTION", f"fleet '{name}': {field} must be a number > 0",
                               f"{at}/{field}", hint="axis speed, distance per model time unit"))
    hoist = spec.get("hoist_time", 0)
    if hoist is not None and (not isinstance(hoist, (int, float)) or isinstance(hoist, bool)
                              or hoist < 0):
        out.append(problem("E_FLEET_MOTION", f"fleet '{name}': hoist_time must be a number >= 0",
                           f"{at}/hoist_time", hint="fixed time added to every gantry hop"))
    return out


def _validate_units(spec: dict, name: str, at: str, nodes: set, known_nodes: str) -> list:
    """A fleet's named `units` (W3-M3): a list of {"name", "skills", "home"},
    unique names, and no `count` saying something else."""
    units = spec.get("units")
    if units is None:
        return []
    if not isinstance(units, list) or not units:
        return [problem("E_FLEET_UNITS", f"fleet '{name}': units must be a non-empty list",
                        f"{at}/units", hint="[{\"name\": \"Ann\", \"skills\": [\"weld\"]}]")]
    out = []
    count = spec.get("count")
    if count is not None and count != len(units):
        out.append(problem(
            "E_FLEET_UNITS", f"fleet '{name}': count {count} but {len(units)} units",
            f"{at}/count", hint="units replaces count: drop count, or make them agree"))
    seen: set[str] = set()
    for i, unit in enumerate(units):
        where = f"{at}/units/{i}"
        if not isinstance(unit, dict):
            out.append(problem("E_FLEET_UNITS", f"fleet '{name}': unit {i} is not an object",
                               where, hint="{\"name\": \"Ann\", \"skills\": [\"weld\"]}"))
            continue
        unit_name = unit.get("name")
        if not isinstance(unit_name, str) or not unit_name.strip():
            out.append(problem("E_FLEET_UNITS", f"fleet '{name}': unit {i} has no name",
                               f"{where}/name", hint="give every unit a unique name"))
        elif unit_name in seen:
            out.append(problem("E_FLEET_UNITS", f"fleet '{name}': duplicate unit name "
                               f"'{unit_name}'", f"{where}/name", hint="rename one of the two"))
        else:
            seen.add(unit_name)
        skills = unit.get("skills", [])
        if not isinstance(skills, list) or not all(isinstance(s, str) and s for s in skills):
            out.append(problem("E_FLEET_UNITS", f"fleet '{name}': unit {i} skills must be a list "
                               f"of names", f"{where}/skills", hint="[\"weld\", \"paint\"]"))
        home = unit.get("home")
        if home is not None and str(home).strip() and str(home).strip() not in nodes:
            out.append(problem("E_FLEET_NODE", f"fleet '{name}': unit {i} has unknown home node "
                               f"'{home}'", f"{where}/home",
                               hint=f"name a declared network node: {known_nodes}"))
    return out


def fleet_skills(layout: dict) -> dict[str, tuple[str, set]]:
    """{fleet name: (kind, every skill some unit has)} -- what a block's
    `worker_fleet`/`worker_skill` is checked against (W3-M3)."""
    out = {}
    for spec in layout.get("fleets", []) or []:
        if isinstance(spec, dict) and str(spec.get("name", "")).strip():
            skills = {s for u in spec.get("units") or [] if isinstance(u, dict)
                      for s in (u.get("skills") or []) if isinstance(s, str)}
            out[str(spec["name"]).strip()] = (spec.get("kind", "vehicle"), skills)
    return out


def validate_workers_structured(layout: dict) -> list:
    """A SEIZE/PROCESS `worker_fleet` (PHASE6 W3-M3, D114): a declared worker
    fleet, a skill some unit has, a `node` to walk to, and no preempt (moved
    from core/schema, C2)."""
    problems: list = []
    fleets = fleet_skills(layout)
    workers = ", ".join(sorted(n for n, (kind, _s) in fleets.items() if kind == "worker"))
    for index, block in enumerate(layout.get("blocks", []) or []):
        params = block.get("params") or {}
        name = block.get("name", "<unnamed>")
        at = f"/blocks/{index}/params"
        mode = params.get("worker_when_blocked") or WORKER_BLOCKED_MODES[0]
        if mode not in WORKER_BLOCKED_MODES:
            problems.append(problem(
                "E_WORKER_WHEN_BLOCKED", f"{name}: unknown worker_when_blocked '{mode}'",
                f"{at}/worker_when_blocked", block=name,
                hint=f"use one of: {', '.join(WORKER_BLOCKED_MODES)}"))
        fleet = str(params.get("worker_fleet", "") or "").strip()
        if not fleet or str(block.get("type", "")).upper() not in QUEUEING_BLOCK_TYPES:
            continue
        kind, skills = fleets.get(fleet, (None, set()))
        if kind != "worker":
            problems.append(problem(
                "E_WORKER_FLEET", f"{name}: '{fleet}' is not a worker fleet", f"{at}/worker_fleet",
                block=name, hint=f"declare it under \"fleets\" with kind \"worker\"; "
                                 f"declared: {workers or '(none)'}"))
            continue
        skill = str(params.get("worker_skill", "") or "").strip()
        if skill and skill not in skills:
            problems.append(problem(
                "E_WORKER_SKILL_UNKNOWN", f"{name}: no unit of '{fleet}' has skill '{skill}'",
                f"{at}/worker_skill", block=name,
                hint=f"skills in '{fleet}': {', '.join(sorted(skills)) or '(none)'}"))
        if not str(params.get("node", "") or "").strip():
            problems.append(problem(
                "E_WORKER_NO_NODE", f"{name}: worker_fleet is set but the block sits on no node",
                f"{at}/node", block=name,
                hint="set `node` to the network node the worker walks to"))
    _validate_worker_preempt(layout, problems)
    return problems


def _validate_worker_preempt(layout: dict, problems: list) -> None:
    """E_WORKER_PREEMPT: no `preempt` block may seize a resource a worker
    block also seizes -- the worker block itself or a sibling (#193); the
    interrupt would land during the worker's walk. A resource set counts as
    its members."""
    members = {str(s.get("name", "")): {str(m) for m in s.get("members") or []}
               for s in layout.get("resource_sets", []) or []}

    def physical(params) -> set:
        res = str(params.get("resource_name", "") or "").strip()
        return members.get(res, {res}) if res else set()

    blocks = [(i, b, b.get("params") or {}) for i, b in enumerate(layout.get("blocks", []) or [])
              if str(b.get("type", "")).upper() in QUEUEING_BLOCK_TYPES]
    workers = [(b.get("name", "<unnamed>"), physical(p))
               for _i, b, p in blocks if str(p.get("worker_fleet", "") or "").strip()]
    for index, block, params in blocks:
        if not params.get("preempt"):
            continue
        name = block.get("name", "<unnamed>")
        for worker, theirs in workers:
            shared = physical(params) & theirs
            if worker != name and not shared:
                continue
            message = (f"{name}: preempt cannot be combined with worker_fleet" if worker == name
                       else f"{name}: preempt on '{', '.join(sorted(shared))}', which worker "
                            f"block '{worker}' also seizes")
            problems.append(problem(
                "E_WORKER_PREEMPT", message, f"/blocks/{index}/params/preempt", block=name,
                hint="an interrupted job would keep its worker while it waits for the machine"))
            break
