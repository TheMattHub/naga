"""Resources, resource sets and their statistics collectors (ROADMAP 1.2).

A resource is always a `simpy.PriorityResource` behind `LoggedSimpyResource`,
so a Seize/Process priority is always honoured and the queue discipline is a
property of the resource, not of its type: every request carries
`priority=(seize_priority, rank)` and SimPy breaks the remaining ties by
request time. `rank` is 0 (FIFO), -now (LIFO), the ranking expression's value
(LowestValueFirst) or its negation (HighestValueFirst). By default `rank` is
computed once, when the request joins the queue; a resource with `"rerank":
true` (D131) instead re-evaluates it, in the same scope, just before every
grant, so a waiting request's rank can change while it waits (`_RerankResource`
below).

A `ResourceSet` is pure routing: it picks one member per seize. It owns its own
queue/wait collectors and is reported as one pseudo-resource row, because an
entity waiting for "any member" sits in every member's SimPy queue and must be
counted exactly once (see `LoggedSimpyResource.request(counted=...)`).

The SimPy object underneath is a `PreemptiveResource` (PHASE4 A4), but every
request made here is `preempt=False` unless a PROCESS block declares
`preempt` -- with that flag off a PreemptiveResource behaves exactly like the
PriorityResource it replaced, so nothing else moved. Blockers in particular
always request with `preempt=False`: they outrank everyone, but the WAIT rule
says a job in service finishes (ARCHITECTURE D55).

Capacity schedules and failures (ROADMAP 1.4) never change a SimPy resource's
capacity -- that would strand requests already queued. The resource is built at
its MAXIMUM capacity and "blocker" requests, all owned by the resource's
`CapacityArbiter` (PHASE6 L3), take units out of service. A blocker outranks every entity request, so it is served
first, but it only takes a unit that is actually free: Arena's WAIT rule, where
a job in service always finishes. Blockers are invisible to every statistic
(`request_blocker`, not `request(counted=...)`); the scheduled capacity in
force is tracked in the `available` collector.
"""
import bisect
import math
from bisect import insort_right
from collections import deque
from operator import attrgetter

import numpy as np
import simpy
from simpy.resources.resource import Preempted, SortedQueue

from nagasim.core.base_process import deterministic_hash
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.problems import problem
from nagasim.core.stats import TimeWeighted, Tally
from nagasim.core.timeunits import ModelUnits, base_factor
from nagasim.core.utils import ResourceSnapshot, timeout_at
from nagasim.settings import config

import logging

log = logging.getLogger(__name__)

__all__ = ["ResourceManager", "LoggedSimpyResource", "ResourceSet", "PlanGates",
           "CapacityArbiter", "capacity_controller", "failure_controller",
           "DISCIPLINES", "RANKED_DISCIPLINES", "SELECTION_RULES", "ENERGY_STATES",
           "validate_resources_structured"]

DISCIPLINES = ("FIFO", "LIFO", "LowestValueFirst", "HighestValueFirst", "Plan")
# The two disciplines that need a ranking expression to order the queue.
RANKED_DISCIPLINES = ("LowestValueFirst", "HighestValueFirst")
SELECTION_RULES = ("PreferredOrder", "Cyclical", "Random",
                   "SmallestNumberBusy", "LargestRemainingCapacity")
# A resource/fleet's "power" states (PHASE6 W3 M1, D112): kW per unit, absent
# keys 0 except "blocked", which defaults to "busy" (a blocked unit is seized,
# D78). Busy excludes blocked so the five states partition unit-time.
ENERGY_STATES = ("busy", "idle", "blocked", "down", "off_shift")

# Priority of a blocker request: sorts before any entity, whose Seize/Process
# priority is a finite number. The second element keeps the tuple shape the
# rest of the module uses, (priority, discipline rank).
_BLOCKER_PRIORITY = (float("-inf"), 0)


def _planned_start(entity):
    """(station, planned start) of *entity* at its current station under the
    "Plan" discipline, or (station, None) when it has no plan there."""
    station = getattr(entity, "station", None)
    plan = getattr(entity, "plan", None)
    value = plan.get(station) if isinstance(plan, dict) else None
    return station, (None if value is None else float(value))


class _PlanRank:
    """Queue discipline "Plan" (PHASE6 W4 P4, D122) in the ranking-expression
    slot: the planned start at the entity's station, lowest first; an entity
    with no plan there ranks after every planned one (FIFO among themselves,
    SimPy's request-time tie break)."""

    @staticmethod
    def evaluate(scope):
        value = _planned_start(scope.entity)[1]
        return math.inf if value is None else value


_PLAN_RANK = _PlanRank()


class PlanGates:
    """Each station's dispatch list under "Plan" (D122): a planned entity joins
    a Plan resource's queue only once every entity planned EARLIER at that
    station has started there. Ranking alone cannot replay a plan that keeps a
    machine idle for a job still on its way (the CP-SAT optimum often does);
    the gate can, and it fixes only the ORDER, never a clock time, so a
    stochastic run still flows. Built once per replication from the order book
    (`GraphModel.load_from_json`, only when a resource uses "Plan").

    ponytail: a job waiting at the gate is not yet in the resource's queue
    statistics; count it there if queue KPIs under Plan ever matter.
    """

    def __init__(self, env, orders):
        self.env = env
        self.left = {}      # station -> sorted planned starts not yet started
        self.waiting = {}   # station -> [(planned start, event)]
        for order in orders.values():
            for station, start in (order.plan or {}).items():
                self.left.setdefault(station, []).extend([float(start)] * order.quantity)
        for starts in self.left.values():
            starts.sort()

    def _pending(self, entity):
        station, value = _planned_start(entity)
        if value is None or station in entity.__dict__.get("_plan_started", ()):
            return station, None      # unplanned here, or a re-visit / resumed job
        return station, value

    def wait(self, entity, patience=None):
        """Generator: hold *entity* until its turn at its station. Returns
        False when *patience* (None: for ever) ran out first (#210); the caller
        then gives the turn up with `started`. A turn arriving in the same
        instant as the expiry wins (the D54 tie rule `seizing._wait` uses)."""
        station, value = self._pending(entity)
        if value is None:
            return True
        deadline = None if patience is None else self.env.now + patience
        starts = self.left.get(station)
        while starts and value > starts[0]:
            event = self.env.event()
            self.waiting.setdefault(station, []).append((value, event))
            if deadline is None:
                yield event
                continue
            yield event | self.env.timeout(deadline - self.env.now)
            if not event.triggered:
                yield self.env.timeout(0)
            if not event.triggered:
                return False    # a stale event left in `waiting` fires into nothing
        return True

    def started(self, entity):
        """*entity* holds its unit, or reneged (#210): the next planned
        entity may queue."""
        station, value = self._pending(entity)
        if value is None:
            return
        entity.__dict__.setdefault("_plan_started", set()).add(station)
        self._remove(station, value)

    def drop(self, entity):
        """An entity leaving the model (Dispose, or absorbed by BATCH/MATCH,
        #206) gives back every station it never started, so a scrapped job
        cannot stall the jobs planned after it. The stations count as started
        from then on: a batch member a SEPARATE later frees passes ungated
        rather than removing another job's entry a second time."""
        plan = getattr(entity, "plan", None)
        if not isinstance(plan, dict):
            return
        done = entity.__dict__.setdefault("_plan_started", set())
        for station, value in plan.items():
            if station not in done and value is not None:
                done.add(station)
                self._remove(station, float(value))

    def _remove(self, station, value):
        starts = self.left.get(station)
        if not starts:
            return
        index = bisect.bisect_left(starts, value)
        if index < len(starts) and starts[index] == value:
            del starts[index]
        head = starts[0] if starts else math.inf
        waiting = self.waiting.get(station, [])
        self.waiting[station] = [(v, e) for v, e in waiting if v > head]
        for v, event in waiting:
            if v <= head:
                event.succeed()


class ResourceManager:
    def __init__(self, env):
        self.env = env
        self.resources = {}
        self.sets = {}

    def register(self, name, capacity=1, discipline="FIFO", ranking_expr=None,
                 cost_busy=0.0, cost_idle=0.0, cost_per_use=0.0, cost_time_unit="hours",
                 core_cls=None, power=None, rerank=False):
        if name in self.resources:
            return self.resources[name]
        wrapped = LoggedSimpyResource(self.env, capacity=capacity, name=name,
                                      discipline=discipline, ranking_expr=ranking_expr,
                                      cost_busy=cost_busy, cost_idle=cost_idle,
                                      cost_per_use=cost_per_use, cost_time_unit=cost_time_unit,
                                      core_cls=core_cls, rerank=rerank)
        # "power" (PHASE6 W3 M1, D112) is a raw {state: kW} dict, or None for a
        # resource that declares none -- outputs.py's energy code applies the
        # ENERGY_STATES defaults at report time, so this class need not know them.
        wrapped.power = power
        self.resources[name] = wrapped
        return wrapped

    def register_set(self, name, members, rule="PreferredOrder"):
        if name in self.sets:
            return self.sets[name]
        missing = [m for m in members if m not in self.resources]
        if missing:
            raise ValueError(f"Resource set '{name}': unknown member(s) {missing}")
        rset = ResourceSet(self.env, name, [self.resources[m] for m in members], rule=rule)
        self.sets[name] = rset
        return rset

    def get(self, name):
        return self.resources.get(name)

    def resolve(self, name):
        """The resource OR set called *name* -- what a block's `resource_name`
        may refer to (they share one namespace)."""
        return self.resources.get(name) or self.sets.get(name)

    def exists(self, name):
        return name in self.resources or name in self.sets

    def all(self):
        """Everything that carries statistics: resources and sets alike, so the
        KPI code needs no special case for the set pseudo-resource rows."""
        return {**self.resources, **self.sets}

    def export_to_json(self):
        return [
            {"name": name, "capacity": res.capacity, "discipline": res.discipline,
             **({"ranking_expr": res.ranking_expr} if res.ranking_expr else {}),
             **({"rerank": True} if res.rerank else {}),
             "cost_busy": res.cost_busy, "cost_idle": res.cost_idle,
             "cost_per_use": res.cost_per_use, "cost_time_unit": res.cost_time_unit,
             **({"power": res.power} if getattr(res, "power", None) else {})}
            for name, res in self.resources.items()
        ]

    def load_from_json(self, resource_list, schedules=None):
        """Register every resource. A `capacity_schedule` makes `capacity` the
        MAXIMUM the resource can ever have (core/schema/ checks the schedule never
        asks for more); the schedule's own maximum stands in when `capacity` is
        omitted. Each scheduled resource gets one controller process."""
        schedules = schedules or {}
        for res in resource_list:
            name = res.get("name")
            schedule = schedules.get(res.get("capacity_schedule"))
            capacity = int(res.get("capacity", schedule.max_value if schedule else 1))
            discipline = res.get("discipline", "FIFO")
            wrapped = self.register(name, capacity=capacity, discipline=discipline,
                                    ranking_expr=res.get("ranking_expr"),
                                    cost_busy=float(res.get("cost_busy", 0.0)),
                                    cost_idle=float(res.get("cost_idle", 0.0)),
                                    cost_per_use=float(res.get("cost_per_use", 0.0)),
                                    cost_time_unit=res.get("cost_time_unit", "hours"),
                                    power=res.get("power"),
                                    rerank=bool(res.get("rerank", False)))
            log.info(f"Imported resource: {name} ({discipline}, capacity={capacity})")
            # Baseline "capacity" resource_state (ROADMAP 2.3 part C): the only
            # one a resource with no schedule/failure ever gets, so a trace
            # recorder can still show its capacity in the animation. A
            # scheduled resource's own controller overwrites this at t=0 with
            # the shift's actual opening value.
            wrapped.emit_state("capacity", capacity)
            if schedule is not None:
                wrapped.has_controller = True
                self.env.process(capacity_controller(wrapped, schedule))
                log.info(f"Resource {name}: capacity schedule '{schedule.name}'")

    def load_failures_from_json(self, failure_list, seed=0, base_unit="minutes"):
        """One controller process per (failure, resource) pair. `uptime`/`downtime`
        may each carry a `time_unit` (ROADMAP 1.5), converted here so the
        controller process (below) only ever sees base-unit samples."""
        for entry in failure_list:
            name = entry.get("name", "failure")
            uptime = _failure_distribution(entry.get("uptime", {}))
            downtime = _failure_distribution(entry.get("downtime", {}))
            for resource_name in entry.get("resources", []):
                resource = self.resources.get(resource_name)
                if resource is None:
                    raise ValueError(f"Failure '{name}': unknown resource '{resource_name}'")
                resource.has_controller = True
                rng = np.random.default_rng(
                    np.random.SeedSequence([seed, deterministic_hash(name + resource_name)]))
                self.env.process(failure_controller(resource, uptime, downtime, rng, name, base_unit))
                log.info(f"Resource {resource_name}: failure '{name}'")

    def load_sets_from_json(self, set_list):
        for entry in set_list:
            name = entry.get("name")
            self.register_set(name, entry.get("members", []), rule=entry.get("rule", "PreferredOrder"))
            log.info(f"Imported resource set: {name} ({entry.get('rule')}, "
                     f"{len(entry.get('members', []))} members)")


_request_key = attrgetter("key")


class _InsortQueue(SortedQueue):
    """SimPy's sorted wait queue, inserting by bisection. `SortedQueue.append`
    appends and re-sorts the whole list on every request, which makes a
    congested resource quadratic in its queue length. The list is always
    sorted, `sort` is stable and `insort_right` places an equal key after its
    equals, so the order is the same."""

    def append(self, item) -> None:
        if self.maxlen is not None and len(self) >= self.maxlen:
            raise RuntimeError("Cannot append event. Queue is full.")
        insort_right(self, item, key=_request_key)


class _Resource(simpy.PreemptiveResource):
    """The SimPy resource every engine resource is built on."""

    PutQueue = _InsortQueue

    def _do_put(self, event):
        # SimPy's own choice of the job to preempt, but for holders of EQUAL key: SimPy
        # leaves that tie to its implementation, and 4.1.2 changed it (`sorted()[-1]`,
        # the last granted, became `max()`, the first). Decided here, as the last
        # granted, so a result does not depend on the SimPy version (#363).
        if len(self.users) >= self.capacity and event.preempt:
            victim = max(reversed(self.users), key=_request_key)
            if victim.key > event.key:
                self.users.remove(victim)
                victim.proc.interrupt(Preempted(by=event.proc, usage_since=victim.usage_since,
                                                resource=self))
        return simpy.Resource._do_put(self, event)


class _RerankResource(_Resource):
    """Discipline option "rerank" (D131): re-evaluate every waiting request's
    rank, in the scope it was requested with, just before each grant --
    `_trigger_put` is the one hand-out point, the same hook
    `core/movers.py`'s `_DispatchResource` sorts on for live dispatching. A
    blocker (`request_blocker`) carries no `rerank_scope` and is left
    untouched, so it keeps its priority and place in line unconditionally.

    ponytail: O(n log n) resort plus one expression evaluation per waiting
    request, on every single grant -- fine at the queue sizes a ranked
    discipline actually runs (tens of waiters); a resource with a huge queue
    reranked on every grant would want a cheaper trigger (e.g. a timer)
    if that ever shows up in profiling.
    """

    rank_of = None   # LoggedSimpyResource.rank_for, bound after construction

    def _trigger_put(self, get_event):
        if self.rank_of is not None and len(self.put_queue) > 1:
            for req in self.put_queue:
                scope = getattr(req, "rerank_scope", None)
                if scope is not None:
                    req.priority = (req.priority[0], self.rank_of(scope))
                    req.key = (req.priority, req.time, not req.preempt)
            self.put_queue.sort(key=lambda e: e.key)
        return super()._trigger_put(get_event)


class LoggedSimpyResource:
    """simpy resource wrapper that owns its statistics collectors.

    Blocks only call request()/release()/cancel(); queue length, units in use
    and waiting times are accounted here, at the real events (DEVLOG #21, #22).
    """

    def __init__(self, env, capacity=1, name="Unnamed", discipline="FIFO", ranking_expr=None,
                 cost_busy=0.0, cost_idle=0.0, cost_per_use=0.0, cost_time_unit="hours",
                 core_cls=None, rerank=False):
        self.env = env
        self.name = name
        # Costing (ROADMAP 2.2): money per unit per cost_time_unit (busy/idle)
        # or per seize (cost_per_use); converted to the model's base time unit
        # at report time via timeunits.factor, never here (this class does not
        # know the model's base unit).
        self.cost_busy = cost_busy
        self.cost_idle = cost_idle
        self.cost_per_use = cost_per_use
        self.cost_time_unit = cost_time_unit
        # `cost_time_unit` in the model's base unit, for the entity's share of
        # the busy cost: `seizing` fills it in at the first charge (it knows
        # the model's units) and reads it on every release after that.
        self.busy_cost_conv = None
        if discipline not in DISCIPLINES:
            raise ValueError(f"Resource '{name}': unknown queue discipline '{discipline}'")
        self.discipline = discipline
        self.ranking_expr = ranking_expr
        # "Plan" (PHASE6 W4 P4, D122) ranks through the same slot a ranking
        # expression uses, so FIFO/LIFO requests run exactly the code they did.
        self._ranking = (_PLAN_RANK if discipline == "Plan"
                         else compile_expression(ranking_expr) if ranking_expr else None)
        if discipline in RANKED_DISCIPLINES and self._ranking is None:
            raise ValueError(f"Resource '{name}': discipline '{discipline}' needs a ranking_expr")
        # Whether `rank_for` reads its scope at all: `seizing.acquire` builds
        # one per seize only then. The same test `rank_for` makes.
        self.needs_scope = self._ranking is not None and discipline not in ("FIFO", "LIFO")
        # D131: re-rank a waiting request just before every grant instead of
        # once at join time. Only meaningful on a ranked discipline -- FIFO's
        # rank is always 0 and LIFO's is -now (it would drift on every grant
        # for no reason), so both are left alone here; core/schema/'s
        # W_RERANK_UNRANKED tells the modeller rather than the engine
        # silently ignoring their flag. A mover fleet supplies its own
        # core_cls (`core/movers.py`'s `_DispatchResource`, which already
        # re-sorts for dispatching); rerank does not compose with that, so a
        # stray flag on a fleet-backed resource is ignored, not refused.
        self.rerank = bool(rerank) and core_cls is None and discipline in RANKED_DISCIPLINES
        if rerank and core_cls is not None:
            log.warning(f"Resource '{name}': rerank is ignored on a fleet-backed resource.")
        # *core_cls* (PHASE4 E1) lets a caller supply a PreemptiveResource
        # SUBCLASS instead -- a mover fleet re-sorts its waiting queue before
        # each grant (`core/movers.py`). Everything this wrapper does is
        # unchanged; only who hands out the units differs.
        resolved_cls = core_cls or (_RerankResource if self.rerank else _Resource)
        self._core = resolved_cls(env, capacity)
        if self.rerank:
            self._core.rank_of = self.rank_for
        # Snapshot deque feeds the flat Excel log sheet only; KPIs come from
        # the collectors below. `log_truncated` (ROADMAP 2.5, DEVLOG #62):
        # True once a run has produced more than LOG_HISTORY snapshots for
        # this resource, so the flat log/mode/percentile cosmetics it feeds
        # are no longer a complete history (the deque's own maxlen silently
        # drops the oldest entry with no signal otherwise).
        self.log = deque(maxlen=config.LOG_HISTORY)
        # Events that WOULD have been logged: `outputs._reportable` asks "did
        # anything happen to this resource", which must stay true with
        # `record_logs` off (PHASE4 T7).
        self.log_events = 0
        self.log_truncated = False
        self.seize_count = 0
        self.queue_len = TimeWeighted(env.now)
        self.busy = TimeWeighted(env.now)
        # Scheduled capacity actually in force: the maximum minus the units a
        # capacity schedule or a failure is currently holding (ROADMAP 1.4).
        # It is the denominator of utilization, so off-shift and down time do
        # not dilute it.
        self.available = TimeWeighted(env.now, level=float(capacity))
        self.down = TimeWeighted(env.now, level=0.0)   # 1 while a failure is active
        # Units a failure's blockers actually hold (#192): energy's `down`
        # unit-time. `down` above is a flag and stays what "Down %" reads.
        self.down_units = TimeWeighted(env.now, level=0.0)
        self._down_blockers = set()
        self.wait = Tally()
        self._enqueued = {}   # request -> time it entered the queue
        self._uncounted = set()  # requests whose queue stats belong to a ResourceSet
        self._blockers = set()   # requests held by a schedule/failure controller
        # Optional owner of the individual UNITS (PHASE4 T7): a mover fleet
        # sets this so a blocker takes a NAMED vehicle rather than an anonymous
        # unit, which is what makes the fleet's positions right while one of
        # them is off shift or broken down. None for every ordinary resource,
        # where a unit has no identity to name.
        self.unit_owner = None
        # Requests that must not count as a seize (PHASE4 A4): the re-request a
        # preempted entity makes to resume the service it already paid for, so
        # `cost_per_use` and `seize_count` are charged once per job, not once
        # per interruption.
        self._uncharged = set()
        # True once a capacity schedule or a failure drives this resource: it
        # then earns a KPI row even if no entity ever seizes it.
        self.has_controller = False
        # D235: per failure name, ("up", when it fails) or ("down", when it is repaired,
        # None until the repair is drawn); and what a state asked each to start from.
        self.failure_clock: dict = {}
        self.failure_start: dict = {}
        self.arbiter = CapacityArbiter(self)    # owns every blocker (PHASE6 L3)

    def __repr__(self):
        return f"<{self.__class__.__name__} name='{self.name}', capacity={self.capacity}, in_use={self.count}, queue={len(self.queue)}>"

    def rank_for(self, scope=None, since=None):
        """The discipline's tie-breaking rank for a request made right now, or, for a
        warm-started entity, at *since*: a backlog put back at one instant keeps the order
        it had (last in, first out by when each really came in)."""
        if self.discipline == "LIFO":
            return -(self.env.now if since is None else since)
        # A stray ranking_expr on FIFO (left from a discipline switched back)
        # must not reorder the queue (#209).
        if self._ranking is None or self.discipline == "FIFO":
            return 0
        value = float(self._ranking.evaluate(scope))
        return -value if self.discipline == "HighestValueFirst" else value

    def request(self, priority=0, rank=0, counted=True, preempt=False, charged=True, scope=None,
                since=None):
        """Queue a request. *counted* False means a ResourceSet is waiting for
        any of its members with this request and owns the queue statistics, so
        the entity is not counted twice (once here, once per other member).

        *preempt* (PHASE4 A4) lets this request interrupt the lowest-priority
        job in service when no unit is free; *charged* False marks a resumed
        request, which waits and is granted like any other but is not a new
        seize (ARCHITECTURE D55). *scope* (D131) is the expression scope this
        request was made in; kept on the request only when this resource
        reranks, so `_RerankResource` can re-evaluate `ranking_expr` in it
        before every grant.
        """
        req = self._core.request(priority=(priority, rank), preempt=preempt)
        if self.rerank and scope is not None:
            req.rerank_scope = scope
        t = self.env.now
        if counted:
            # *since*: a warm-started entity was already waiting (`initial_state` `since`).
            self._enqueued[req] = t if since is None else since
            self.queue_len.add(1, t)
        else:
            self._uncounted.add(req)
        if not charged:
            self._uncharged.add(req)
        self.log_usage(t, event_type="request")
        req.callbacks.append(self._on_granted)
        return req

    def _seized(self, req) -> None:
        """Count one grant, unless this request is a preempted job resuming."""
        self.busy.add(1, self.env.now)
        self._notify_bottleneck(self.env.now)
        # `if self._uncharged` keeps the common model (no preemption anywhere)
        # off the set lookup entirely.
        if self._uncharged and req in self._uncharged:
            self._uncharged.discard(req)
            return
        self.seize_count += 1

    def _on_granted(self, req):
        t = self.env.now
        if req in self._uncounted:
            self._uncounted.discard(req)
            self._seized(req)
            self.log_usage(t, event_type="seize")
            return
        enqueued_at = self._enqueued.pop(req, None)
        if enqueued_at is None:      # already accounted for (released while queued)
            return
        self.queue_len.add(-1, t)
        self._seized(req)
        self.wait.add(t - enqueued_at)
        self.log_usage(t, event_type="seize")

    def release(self, req):
        t = self.env.now
        self._uncounted.discard(req)
        if self._enqueued.pop(req, None) is not None:
            self.queue_len.add(-1, t)      # cancelled before it was ever granted
        else:
            self.busy.add(-1, t)
            self._notify_bottleneck(t)
        result = self._core.release(req)
        self.log_usage(t, event_type="release")
        return result

    def serve_without_unit(self, req):
        """A QUEUED request served by a unit another request holds (PHASE6 W3
        M4, D116: a load attached to a vehicle's tour). It leaves the queue and
        its wait is recorded, but it is no seize and holds no unit. The request
        event is still triggered so its waiter resumes; `_on_granted` then finds
        nothing to account for."""
        t = self.env.now
        enqueued_at = self._enqueued.pop(req, None)
        if enqueued_at is not None:
            self.queue_len.add(-1, t)
            self.wait.add(t - enqueued_at)
        self._core.put_queue.remove(req)
        self.log_usage(t, event_type="attach")
        req.succeed()

    def cancel(self, req):
        """Withdraw a request that may or may not have been granted yet -- the
        losing halves of a wait-for-any over a resource set. A request granted
        in the same instant is released; one still queued leaves the queue, so
        no dangling request is left behind and the collectors return to zero."""
        if req.triggered:
            return self.release(req)
        t = self.env.now
        self._uncounted.discard(req)
        self._uncharged.discard(req)
        if self._enqueued.pop(req, None) is not None:
            self.queue_len.add(-1, t)
        req.cancel()
        self.log_usage(t, event_type="cancel")

    # -- blockers (capacity schedules and failures, ROADMAP 1.4) -----------
    def request_blocker(self, down: bool = False):
        """Take one unit out of service. Invisible to every statistic: not a
        seize, not a queue entry, not a wait. The request outranks any entity
        but still waits for a free unit, so a job in service finishes first.
        *down*: a failure's blocker (counted in `down_units`), not a shift's."""
        # preempt=False: a blocker outranks every entity but never interrupts
        # one -- Arena's WAIT rule, unchanged by PHASE4 A4 (ARCHITECTURE D55).
        req = self._core.request(priority=_BLOCKER_PRIORITY, preempt=False)
        self._blockers.add(req)
        if down:
            self._down_blockers.add(req)
        req.callbacks.append(self._on_blocker_granted)
        return req

    def _on_blocker_granted(self, req):
        self.available.add(-1, self.env.now)
        if req in self._down_blockers:
            self.down_units.add(1, self.env.now)
        if self.unit_owner is not None:
            self.unit_owner.take_unit()
        self._wake_continuous()

    def release_blocker(self, req):
        """Put a blocked unit back in service; a blocker still queued when the
        shift turns back on is simply withdrawn."""
        self._blockers.discard(req)
        was_down = req in self._down_blockers
        self._down_blockers.discard(req)
        if req.triggered:
            self.available.add(1, self.env.now)
            if was_down:
                self.down_units.add(-1, self.env.now)
            if self.unit_owner is not None:
                self.unit_owner.give_unit()
            self._wake_continuous()
            return self._core.release(req)
        req.cancel()

    def _wake_continuous(self):
        """A flow rate may read `AVAILABLE("me")` (Bio-1 G3), so the level
        engine re-evaluates the instant this resource's capacity in force
        changes -- a failure taking a unit, a shift giving one back. Hooked
        here rather than on `emit_state` because THIS is where
        `scheduled_capacity` actually moves (a failure's blocker waits for a
        busy unit under the WAIT rule)."""
        system = getattr(self.env.ctx, "level_system", None)
        if system is not None:
            system.trigger()
        if self.env.ctx.watched:        # MR/AVAILABLE in a HOLD condition (PHASE5 O4)
            self.env.ctx.touch(("res", self.name))

    def _notify_bottleneck(self, t) -> None:
        """Feed the Roser tracker (PHASE6 W3 M2, ARCHITECTURE D113), when the
        model opted in -- a no-op `is not None` check otherwise."""
        tracker = self.env.ctx.bottleneck_tracker
        if tracker is not None:
            tracker.busy(self.name, self.busy.level, t)

    def _notify_bottleneck_down(self, is_down: bool, t) -> None:
        """`down` half of `_notify_bottleneck` -- repair counts as ACTIVE
        (PHASE6 W3 M2, ARCHITECTURE D113)."""
        tracker = self.env.ctx.bottleneck_tracker
        if tracker is not None:
            tracker.down(self.name, is_down, t)

    def emit_state(self, state, value):
        """Publish a `resource_state` bus event for future animation. There is
        no block behind a controller process, hence `block=None`."""
        self.env.ctx.bus.emit("resource_state", block=None, t=self.env.now,
                              resource=self.name, state=state, value=value)

    @property
    def _pending_blockers(self):
        """Blockers queued for a unit that is still busy (the WAIT rule).

        Fast path for the common case (no capacity schedule or failure ever
        touched this resource, so `_blockers` is always empty): skips building
        a generator on every `free`/`in_service`/`waiting` access, which
        profiling showed adds up on a plain M/M/1-style model (ROADMAP 2.5).
        """
        if not self._blockers:
            return 0
        return sum(1 for req in self._blockers if not req.triggered)

    @property
    def free(self):
        """Units an entity could seize right now: capacity minus the units in
        use and minus every blocker, queued ones included -- a queued blocker
        outranks each entity, so those units are already spoken for. Never
        negative: more blockers than free units still means none is seizable."""
        return max(self._core.capacity - self._core.count - self._pending_blockers, 0)

    @property
    def in_service(self):
        """Units held by entities. A blocked unit is out of service, not in use,
        so it is netted out of everything a model or a report can read."""
        return self._core.count - (len(self._blockers) - self._pending_blockers)

    @property
    def waiting(self):
        """Entities queued. A blocker waiting at the head of the queue is not
        an entity waiting for the resource."""
        return len(self._core.queue) - self._pending_blockers

    @property
    def scheduled_capacity(self):
        """Units in force right now: the maximum minus what is blocked."""
        return self.available.level

    def available_area(self, t_end):
        """Integral of the scheduled capacity: utilization's denominator."""
        return self.available.area(t_end)

    def scheduled_capacity_avg(self, t_end):
        return self.available.time_avg(t_end)

    def down_fraction(self, t_end):
        return self.down.time_avg(t_end)

    def cost_totals(self, t_end, base_unit):
        """(busy, idle, usage, total) cost over the observed window, ending at
        *t_end* (ROADMAP 2.2). Idle cost is scheduled-but-not-busy time, so a
        resource off shift or down (excluded from `available`) costs nothing;
        both areas are already warm-up aware because the underlying collectors
        reset there. `base_unit` is the model's time unit -- cost_busy/cost_idle
        are money per unit per cost_time_unit, converted here."""
        busy_time = self.busy.area(t_end)
        idle_time = self.available_area(t_end) - busy_time
        conv = base_factor("time", self.cost_time_unit, ModelUnits(base_unit))
        busy_cost = self.cost_busy * busy_time / conv
        idle_cost = self.cost_idle * idle_time / conv
        usage_cost = self.cost_per_use * self.seize_count
        return busy_cost, idle_cost, usage_cost, busy_cost + idle_cost + usage_cost

    @property
    def capacity(self):
        return self._core.capacity

    @property
    def count(self):
        return self._core.count

    @property
    def queue(self):
        return self._core.queue

    def log_usage(self, time, event_type=None):
        # `in_service`/`waiting`, not the raw SimPy counts: the flat log sheet
        # (and the percentile cosmetics it feeds) sees entities only.
        self.log_events += 1
        ctx = self.env.ctx
        if ctx.watched:                 # NQ/NR/IDLE in a HOLD condition (PHASE5 O4)
            ctx.touch(("res", self.name))
        if not ctx.record_logs:
            return                      # PHASE4 T7: no flat log for this run
        # Written out, not through a helper: this runs three times per
        # seize, and the keyword call plus two property chains cost more than
        # the append. Same record, same truncation rule; *time* is `env.now`
        # at every caller.
        buffer = self.log
        if buffer.maxlen and len(buffer) >= buffer.maxlen:
            self.log_truncated = True
        core = self._core
        if self._blockers:
            # `in_service` and `waiting`, with the blockers counted once
            # instead of once in each.
            pending = self._pending_blockers
            in_use = len(core.users) - (len(self._blockers) - pending)
            waiting = len(core.put_queue) - pending
        else:                          # no schedule or failure: SimPy's counts are the entities
            in_use, waiting = len(core.users), len(core.put_queue)
        buffer.append(ResourceSnapshot(time, in_use, waiting, event_type))

    def open_waits(self, t_end):
        """Waits of requests still queued at *t_end* (censored observations).

        `wait` only records a wait when the request is granted, so a saturated
        resource would otherwise report only the lucky short waits (DEVLOG #24).
        """
        return [max(t_end - t, 0.0) for t in self._enqueued.values()]

    def reset_stats(self, t):
        """Warm-up hook: zero the accumulators, keep the current state."""
        self.queue_len.reset(t)
        self.busy.reset(t)
        self.available.reset(t)
        self.down.reset(t)
        self.down_units.reset(t)
        self.wait.reset()
        self.seize_count = 0


class ResourceSet:
    """A named group of resources a block may seize from (ROADMAP 1.2).

    Routing only: `pick` chooses the member, `core/seizing.py` does the
    requesting. The set carries the queue and wait collectors for everything
    routed through it (members keep utilization and seize counts), so an entity
    queued on the set counts once however many member queues its requests sit in.
    """

    def __init__(self, env, name, members, rule="PreferredOrder"):
        if rule not in SELECTION_RULES:
            raise ValueError(f"Resource set '{name}': unknown selection rule '{rule}'")
        if not members:
            raise ValueError(f"Resource set '{name}': no members")
        self.env = env
        self.name = name
        self.members = list(members)
        self.rule = rule
        self._cursor = -1          # Cyclical: index of the last member picked
        self.log_events = 0
        self.log = deque(maxlen=config.LOG_HISTORY)
        self.log_truncated = False
        self.seize_count = 0
        self.queue_len = TimeWeighted(env.now)
        self.busy = TimeWeighted(env.now)
        self.wait = Tally()
        self._open = []            # enqueue times of entities still waiting

    def __repr__(self):
        return (f"<ResourceSet name='{self.name}', rule={self.rule}, "
                f"members={[m.name for m in self.members]}>")

    # -- routing -----------------------------------------------------------
    def pick(self, quantity, rng):
        """The member to seize *quantity* units from, or None when no single
        member has that many free right now. Ties go to member order."""
        # `free`, not capacity - count: a unit a capacity schedule or failure has
        # taken (or is queued to take) is not available to an entity.
        # ... and neither is a unit freed in this very instant while entities still wait
        # for the member: its queue head is granted when the release is processed, so the
        # unit is owed. A newcomer sent there would queue on that member alone, behind
        # them all (#352); with no candidate it queues on every member, as the others did.
        candidates = [m for m in self.members if m.free >= quantity
                      and len(m._core.put_queue) <= m._pending_blockers]
        if not candidates:
            return None
        if self.rule == "PreferredOrder":
            return candidates[0]
        if self.rule == "Random":
            return candidates[int(rng.integers(len(candidates)))]
        if self.rule == "Cyclical":
            start = self._cursor + 1
            order = self.members[start:] + self.members[:start]
            chosen = next(m for m in order if m in candidates)
            self._cursor = self.members.index(chosen)
            return chosen
        if self.rule == "SmallestNumberBusy":
            return min(candidates, key=lambda m: m.in_service)
        return max(candidates, key=lambda m: m.free)  # LargestRemainingCapacity

    # -- statistics --------------------------------------------------------
    @property
    def capacity(self):
        """The set's capacity right now: the sum of its members' SCHEDULED
        capacities, so a member that is off shift or down does not count."""
        return sum(m.available.level for m in self.members)

    @property
    def count(self):
        return sum(m.in_service for m in self.members)

    # What NQ, NR, MR, AVAILABLE and IDLE read, under a single resource's own names (#392).
    @property
    def waiting(self):
        """Entities queued on the set: each once, however many members it asked."""
        return len(self._open)

    in_service = count
    scheduled_capacity = capacity

    @property
    def free(self):
        return sum(m.free for m in self.members)

    def available_area(self, t_end):
        return sum(m.available_area(t_end) for m in self.members)

    def scheduled_capacity_avg(self, t_end):
        return sum(m.scheduled_capacity_avg(t_end) for m in self.members)

    def down_fraction(self, t_end):
        """Mean of the members' down fractions: "how much of the set is down"."""
        return sum(m.down_fraction(t_end) for m in self.members) / len(self.members)

    def cost_totals(self, t_end, base_unit):
        """A set carries no cost of its own -- its members do, and each already
        reports its own row (ROADMAP 2.2: "the set pseudo-row reports 0 cost")."""
        return 0.0, 0.0, 0.0, 0.0

    def enqueue(self, since=None):
        t = self.env.now
        self.queue_len.add(1, t)
        if since is not None:       # a warm-started entity's wait began then
            t = since
        bisect.insort(self._open, t)        # `_close` bisects: an earlier `since` goes in place (#351)
        self.log_usage(self.env.now, event_type="request")
        return t

    def granted(self, enqueued_at, units):
        t = self.env.now
        self.queue_len.add(-1, t)
        self.busy.add(units, t)
        self.seize_count += 1
        self.wait.add(t - enqueued_at)
        self._close(enqueued_at)
        self.log_usage(t, event_type="seize")

    def left(self, enqueued_at):
        """An entity gave up waiting for any member (PHASE4 A4 reneging): it
        leaves the set's queue without ever becoming busy. Balking never gets
        here -- a balking entity is never enqueued in the first place."""
        t = self.env.now
        self.queue_len.add(-1, t)
        self._close(enqueued_at)
        self.log_usage(t, event_type="cancel")

    def _close(self, enqueued_at):
        """Drop one open wait that began at *enqueued_at*, if there is one.
        `_open` is appended to at `env.now`, so it is in time order and the
        entry is found by bisection, not by a scan of everyone waiting; equal
        times are interchangeable, so which of them goes does not matter."""
        waits = self._open
        index = bisect.bisect_left(waits, enqueued_at)
        if index < len(waits) and waits[index] == enqueued_at:
            del waits[index]

    def released(self, units):
        t = self.env.now
        self.busy.add(-units, t)
        self.log_usage(t, event_type="release")

    def log_usage(self, time, event_type=None):
        self.log_events += 1
        if not self.env.ctx.record_logs:
            return                      # PHASE4 T7
        # Written out, as in `LoggedSimpyResource.log_usage`: same record, same
        # truncation rule; *time* is `env.now` at every caller.
        buffer = self.log
        if buffer.maxlen and len(buffer) >= buffer.maxlen:
            self.log_truncated = True
        buffer.append(ResourceSnapshot(time, self.count, int(self.queue_len.level), event_type))

    def open_waits(self, t_end):
        return [max(t_end - t, 0.0) for t in self._open]

    def reset_stats(self, t):
        self.queue_len.reset(t)
        self.busy.reset(t)
        self.wait.reset()
        self.seize_count = 0


# -- controller processes (ROADMAP 1.4) -----------------------------------
#
# A failure controller runs for ever (a repeating capacity schedule too), so a
# model that has one never empties its event queue: it needs a run length, the
# same way a CREATE with max_arrivals=0 always did. `step_until` with no limit
# would otherwise never return.


class CapacityArbiter:
    """The one owner of a resource's blockers (PHASE6 L3): its capacity
    schedule, the player's cap (`SimSession.set_capacity`) and its failures.

    In force = min(S, C), or 0 while a failure is active: S the schedule's
    value (the declared capacity without one), C the cap (default: the
    declared capacity). Shift blockers keep min(S, C) in force -- a decrease
    waits for units to free (the WAIT rule), an increase gives the newest back
    at once. They sit in two lists, the schedule's (capacity - S) and the
    cap's (max(0, S - C)), so with C at its default the schedule's blockers
    are made and released exactly as before the cap existed: a run with no
    failure overlapping a shift change is byte-identical.

    A failure takes every unit in force at its start, except those a queued
    shift or cap blocker already claimed (#235: it would otherwise wait for
    them until the shift came back, and so would its downtime clock). A unit
    a shift or cap change hands back during a failure is kept down until the
    repair (#234, #227), so the player cannot bring a failed resource back and
    neither can the schedule.
    """

    def __init__(self, resource):
        self.resource = resource
        self.schedule = self.cap = resource.capacity
        self.failing = 0            # failures in progress
        self._sched: list = []
        self._cap: list = []
        self._held: list = []       # blockers kept down until the last repair

    def _apply(self) -> None:
        res = self.resource
        lists = ((self._sched, res.capacity - self.schedule),
                 (self._cap, max(self.schedule - self.cap, 0)))
        spare = []
        for blockers, wanted in lists:
            while len(blockers) > wanted:
                spare.append(blockers.pop())
        # A blocker moves between the lists rather than being released and
        # requested again (only a game's cap ever makes both change at once).
        for blockers, wanted in lists:
            while len(blockers) < wanted:
                blockers.append(spare.pop() if spare else res.request_blocker())
        for blocker in spare:       # units back in force, unless failed
            if self.failing:
                self._hold(blocker)
            else:
                res.release_blocker(blocker)
        res.emit_state("capacity", min(self.schedule, self.cap))

    def _hold(self, blocker) -> None:
        """Re-label a shift/cap blocker as a failure's: counted in `down_units`
        (now if it holds a unit, else when granted) and released at repair."""
        res = self.resource
        res._down_blockers.add(blocker)
        if blocker.triggered:
            res.down_units.add(1, res.env.now)
        self._held.append(blocker)

    def set_schedule(self, value) -> None:
        self.schedule = min(int(value), self.resource.capacity)
        self._apply()

    def set_cap(self, units: int) -> None:
        self.cap = int(units)
        self._apply()

    def take_down(self) -> list:
        """A failure starts: its blockers (the caller yields on all of them)."""
        res = self.resource
        self.failing += 1
        claimed = sum(1 for req in (*self._sched, *self._cap, *self._held)
                      if not req.triggered)
        # In force by the blockers GRANTED, not by `available`: that level drops only when
        # a grant is processed, and a shift closing in this very instant would be missed
        # (#347), leaving the failure to wait for a unit until the shift reopens.
        in_force = res.capacity - sum(1 for req in res._blockers if req.triggered)
        return [res.request_blocker(down=True) for _ in range(max(in_force - claimed, 0))]

    def repair(self, blockers: list) -> None:
        for blocker in blockers:
            self.resource.release_blocker(blocker)
        self.failing -= 1
        if not self.failing:
            for blocker in self._held:
                self.resource.release_blocker(blocker)
            self._held = []


def capacity_controller(resource, schedule):
    """Keep the units in service equal to *schedule*'s current value, through
    the resource's `CapacityArbiter` (the WAIT rule on a decrease, an
    immediate release on an increase)."""
    env = resource.env
    if env.ctx.settling:        # #346: a warm-started job in service seizes first
        yield env.timeout(0)
    while True:
        resource.arbiter.set_schedule(schedule.value_at(env.now))

        next_change = schedule.next_change_after(env.now)
        if next_change == float("inf"):
            return                      # the value holds for ever: nothing left to do
        yield env.timeout(next_change - env.now)


def _failure_distribution(spec: dict) -> StochasticDistribution:
    """Build a failure's uptime/downtime sampler; `time_unit` (ROADMAP 1.5,
    default "base") is carried on the distribution so `_sample_scaled` below
    can convert it -- there is no BaseProcess.sample_duration here, a
    controller process is not a block."""
    return StochasticDistribution(spec.get("distribution", "Constant"), spec.get("dist_params", {}),
                                  time_unit=spec.get("time_unit", "base"))


def _sample_scaled(dist: StochasticDistribution, rng, base_unit: str, deterministic: bool = False) -> float:
    value = max(dist.sample(rng, deterministic=deterministic), 0.0)
    unit = dist.time_unit
    return value * base_factor("time", unit, ModelUnits(base_unit)) if unit != "base" else value


def failure_controller(resource, uptime, downtime, rng, name, base_unit="minutes"):
    """Fail *resource* every `uptime` (clock time, not busy time) for
    `downtime`; the units taken are the `CapacityArbiter`'s call."""
    env = resource.env
    # PHASE6 W4 P1, D119: TTF and TTR use their mean in deterministic mode,
    # the same substitution `sample_duration` makes for every block.
    deterministic = env.ctx.deterministic
    # A state (`initial_state.resources.<r>.failures`, D232) fixes the first cycle only:
    # the failure due at `next`, or one in progress, repaired at `down_until`.
    first = resource.failure_start.get(name) or {}
    if env.ctx.settling:        # #346: a warm-started job in service seizes first
        yield env.timeout(0)
    clock = resource.failure_clock
    while True:
        if "down_until" not in first:
            if first.get("next") is not None:
                clock[name] = ("up", float(first["next"]))
                yield timeout_at(env, float(first["next"]))
            else:
                wait = _sample_scaled(uptime, rng, base_unit, deterministic)
                clock[name] = ("up", env.now + wait)
                yield env.timeout(wait)
        clock[name] = ("down", None if first.get("down_until") is None
                       else float(first["down_until"]))
        resource.down.set(1.0, env.now)     # down from the moment it is triggered
        resource.emit_state("down", 1)
        resource._notify_bottleneck_down(True, env.now)
        blockers = resource.arbiter.take_down()
        yield env.all_of(blockers)          # WAIT: jobs in service finish first
        if first.get("down_until") is not None:
            clock[name] = ("down", float(first["down_until"]))
            yield timeout_at(env, float(first["down_until"]))
        else:
            wait = _sample_scaled(downtime, rng, base_unit, deterministic)
            clock[name] = ("down", env.now + wait)
            yield env.timeout(wait)
        first = {}
        resource.arbiter.repair(blockers)
        resource.down.set(0.0, env.now)
        resource.emit_state("up", 0)
        resource._notify_bottleneck_down(False, env.now)
        if env.ctx.debug:
            log.debug(f"Resource {resource.name}: failure '{name}' repaired at {env.now}")


def validate_resources_structured(layout: dict,
                                 schedule_kinds: dict | None = None) -> list:
    """The "resources" and "resource_sets" sections (moved from core/schema, C2):
    queue disciplines, ranking expressions, capacity schedules, and resource
    sets. Resources and sets share one namespace: a block's `resource_name` may
    name either."""
    problems: list = []
    schedule_kinds = schedule_kinds or {}
    max_values = {s.get("name"): max((p[1] for p in s.get("points", [])
                                      if isinstance(p, (list, tuple)) and len(p) == 2), default=0)
                  for s in layout.get("schedules", [])}
    capacity_schedules = [n for n, k in schedule_kinds.items() if k == "capacity"]
    names: set[str] = set()
    for index, entry in enumerate(layout.get("resources", [])):
        at = f"/resources/{index}"
        name = entry.get("name", "")
        if name in names:
            problems.append(problem("E_DUPLICATE_RESOURCE", f"duplicate resource name: {name}",
                                    f"{at}/name", hint="rename one of the two resources"))
        names.add(name)

        schedule_name = entry.get("capacity_schedule")
        if schedule_name:
            if schedule_kinds.get(schedule_name) != "capacity":
                problems.append(problem(
                    "E_RESOURCE_SCHEDULE",
                    f"resource '{name}': capacity_schedule '{schedule_name}' "
                    f"is not a declared schedule of kind 'capacity'",
                    f"{at}/capacity_schedule",
                    hint=f"declare it under \"schedules\" with \"kind\": \"capacity\"; declared: "
                         f"{', '.join(capacity_schedules) or '(none)'}"))
            elif "capacity" in entry and entry["capacity"] < max_values.get(schedule_name, 0):
                problems.append(problem(
                    "E_RESOURCE_CAPACITY",
                    f"resource '{name}': capacity {entry['capacity']} is below the "
                    f"maximum {max_values[schedule_name]} that schedule "
                    f"'{schedule_name}' asks for",
                    f"{at}/capacity",
                    hint=f"raise capacity to at least {max_values[schedule_name]}"))

        for cost_key in ("cost_busy", "cost_idle", "cost_per_use"):
            if cost_key in entry and (not isinstance(entry[cost_key], (int, float))
                                      or isinstance(entry[cost_key], bool) or entry[cost_key] < 0):
                problems.append(problem("E_RESOURCE_COST",
                                        f"resource '{name}': {cost_key} must be a number >= 0",
                                        f"{at}/{cost_key}", hint="use 0 for no cost"))

        power = entry.get("power")
        if power is not None:
            if not isinstance(power, dict):
                problems.append(problem(
                    "E_RESOURCE_POWER", f"resource '{name}': power must be an object",
                    f"{at}/power", hint="{\"busy\": 5.0, \"idle\": 1.0} -- kW per unit"))
            else:
                for state, value in power.items():
                    if state not in ENERGY_STATES:
                        problems.append(problem(
                            "E_RESOURCE_POWER_STATE",
                            f"resource '{name}': unknown power state '{state}'",
                            f"{at}/power/{state}", hint=f"one of: {', '.join(ENERGY_STATES)}"))
                    elif not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                        problems.append(problem(
                            "E_RESOURCE_POWER", f"resource '{name}': power.{state} must be a "
                                                f"number >= 0",
                            f"{at}/power/{state}", hint="kW for one unit in that state"))

        discipline = entry.get("discipline", "FIFO")
        if discipline not in DISCIPLINES:
            problems.append(problem(
                "E_RESOURCE_DISCIPLINE",
                f"resource '{name}': unknown discipline '{discipline}' "
                f"(expected one of {', '.join(DISCIPLINES)})",
                f"{at}/discipline", hint=f"use one of: {', '.join(DISCIPLINES)}"))
            continue
        ranking = entry.get("ranking_expr", "")
        if discipline in RANKED_DISCIPLINES and not ranking:
            problems.append(problem(
                "E_RANKING_REQUIRED",
                f"resource '{name}': discipline '{discipline}' needs a ranking_expr",
                f"{at}/ranking_expr",
                hint="add an expression to rank the queue by, e.g. \"entity.priority\""))
        elif ranking:
            try:
                compile_expression(ranking)
            except ExpressionError as exc:
                problems.append(problem("E_EXPRESSION",
                                        f"resource '{name}': ranking_expr: {exc}",
                                        f"{at}/ranking_expr",
                                        hint="see the expression reference for the allowed grammar"))
        if entry.get("rerank") and discipline not in RANKED_DISCIPLINES:
            problems.append(problem(
                "W_RERANK_UNRANKED",
                f"resource '{name}': rerank has no effect on discipline '{discipline}'",
                f"{at}/rerank",
                hint="rerank only matters with LowestValueFirst/HighestValueFirst"))

    for index, entry in enumerate(layout.get("resource_sets", [])):
        at = f"/resource_sets/{index}"
        name = entry.get("name", "")
        if name in names:
            problems.append(problem(
                "E_DUPLICATE_RESOURCE",
                f"resource set '{name}': name already used by a resource or set",
                f"{at}/name", hint="resources and sets share one namespace: rename one"))
        names.add(name)
        rule = entry.get("rule", "PreferredOrder")
        if rule not in SELECTION_RULES:
            problems.append(problem(
                "E_SET_RULE",
                f"resource set '{name}': unknown rule '{rule}' "
                f"(expected one of {', '.join(SELECTION_RULES)})",
                f"{at}/rule", hint=f"use one of: {', '.join(SELECTION_RULES)}"))
        members = entry.get("members", [])
        if not members:
            problems.append(problem("E_SET_EMPTY", f"resource set '{name}': no members",
                                    f"{at}/members",
                                    hint="list at least one declared resource as a member"))
        declared = {r.get("name") for r in layout.get("resources", [])}
        for member in members:
            if member not in declared:
                problems.append(problem(
                    "E_SET_MEMBER", f"resource set '{name}': unknown member '{member}'",
                    f"{at}/members",
                    hint=f"declare it under \"resources\" first; declared: "
                         f"{', '.join(sorted(str(d) for d in declared)) or '(none)'}"))
    return problems
