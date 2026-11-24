"""PROCESS: seize, delay and release in one block (ROADMAP 1.2).

The Arena workhorse. `action` selects which of the three steps run, so the same
block covers a self-contained service ("Seize Delay Release"), the start of a
hold across several blocks ("Seize Delay"), its end ("Delay Release") and a
plain "Delay".

PHASE4 A2 adds a sequence-dependent SETUP: an optional duration charged once,
after the seize and before the service, whenever the resource this block holds
changes over from the previous job's setup key to this one. PROCESS is the only
block that carries it -- it is the one block that owns both a resource and the
service delay at a station, and its "Seize Delay" action already covers what a
bare SEIZE does (ARCHITECTURE D52).

PHASE4 T3 adds `setup_cost` beside it: an expression in MONEY, charged once per
ACTUAL changeover (never per job) to a D63 event cost item, so a changeover
that costs scrap and a technician is in Total Cost without a RECORD block --
which could only charge every job, not every changeover.

PHASE4 A4 adds the queue behaviours: `balk_if`/`renege_after` (shared with
SEIZE) and `preempt`, which is PROCESS-only and only in the "Seize Delay
Release" action -- the one case where this block owns the whole service and can
therefore give the interrupted job its remaining time back (ARCHITECTURE D55).
"""
import logging

import simpy

from nagasim.core import blocking, costs
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import (BlockSpec, DIST_PARAMS, HOLD_SPACE_PARAMS, QUEUE_PARAMS,
                                     WORKER_BLOCKED_MODES, WORKER_PARAMS, WORKER_WHEN_BLOCKED,
                                     ParamSpec)
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import ExpressionError
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.seizing import (ON_PREEMPT_MODES, QueueBehaviour, _accrue_busy_cost,
                                  _accrue_usage_cost, acquire, acquire_plain, acquire_worker,
                                  balks, leave, patience_of, plain_target, preempt_release,
                                  release)
from nagasim.core.seizing import log as _seize_log
from nagasim.core.stats import TimeWeighted

log = logging.getLogger(__name__)

__all__ = ["Process", "SPEC"]

ACTIONS = ("Seize Delay Release", "Seize Delay", "Delay Release", "Delay")

_UNDECIDED = object()       # `Process._plain` before the block's first entity

# Where a changeover cost lands in `cost_summary` when the block names no
# category of its own.
_DEFAULT_SETUP_CATEGORY = "Changeover"


class Process(QueueBehaviour, BaseProcess):
    """Machine, workstation or service step: a part, order or customer waits for a resource (machine, operator, server), is processed for a time, then frees it.

    Seize, delay and release fused in one block; `action` selects which of
    the three steps run ("Seize Delay Release", "Seize Delay", "Delay Release",
    "Delay"), so the same block type covers a self-contained service, the
    start or end of a hold split across two blocks, or a plain timed delay.
    The most-used block for a simple resource-backed service step."""

    ACTIONS = ACTIONS

    def __init__(self, env, block, next_process=None, action=ACTIONS[0],
                 resource_name="", quantity=1, delay=0, priority=0):
        super().__init__(env, block, next_process)
        self.action = action
        self.resource_name = resource_name
        self.quantity = quantity
        self.priority = priority
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )
        # Sequence-dependent setup (PHASE4 A2). `setup` is the duration
        # expression, `setup_key` what identifies a job's setup (default: the
        # entity type), `initial_key` what the resource is already set up for at
        # time 0 ("" = no setup for the first job).
        self.setup = ""
        self._setup_factor = 1.0
        self.setup_key = ""
        self.initial_key = ""
        # PHASE4 T3: money per changeover, charged where the setup time is.
        self.setup_cost = ""
        self.setup_cost_category = ""
        self._previous_key = None
        # Reported on the block, not in the KPI frames: the setup happens while
        # the resource is held, so it is already inside its busy time and
        # utilization (ARCHITECTURE D52).
        self.setup_time_total = 0.0
        self.setup_count = 0
        # Queue behaviours (PHASE4 A4): balk_if/renege_after come from the
        # mixin; preemption is this block's own, and only ever armed in the
        # "Seize Delay Release" action (core/schema/ rejects the rest).
        self.init_queue_behaviour()
        self.preempt = False
        self.on_preempt = ON_PREEMPT_MODES[0]
        self.resample = False   # PHASE5 O4: "restart" draws a fresh service time
        # Blocking after service (PHASE4 T5, ARCHITECTURE D78): `space` is this
        # station's own finite input queue (None = unlimited, today's default),
        # `blocked` the time-weighted "holding a finished part" collector.
        self.queue_capacity = 0
        self.blocked = TimeWeighted(env.now, level=0.0)
        # A worker for the service (PHASE6 W3-M3, D114).
        self.worker_fleet = ""
        self.worker_skill = ""
        self.node = ""
        self.worker_when_blocked = WORKER_BLOCKED_MODES[0]
        # The resource this block seizes down `seizing.acquire_plain`, or None for the full
        # path (speed review B); decided at the first entity (`plain_target`).
        self._plain = _UNDECIDED
        # Whether a visit runs as the one generator `_visit_plain` (speed review R4); decided
        # at the first entity that is not warm-started (`_plain_visit`).
        self._visit = _UNDECIDED

    @property
    def space_passthrough(self) -> bool:
        """A station accepts the part when it SEIZES, not when it arrives --
        which is what makes a zero-place buffer pure blocking after service."""
        return self.seizes

    @property
    def can_block(self) -> bool:
        return self.releases

    @property
    def cost_item_name(self) -> str:
        """The `cost_summary` item a changeover cost accrues to: named after
        the block, so two stations never share one line."""
        return f"Changeover {self.name}"

    @property
    def action(self) -> str:
        return self._action

    @action.setter
    def action(self, value) -> None:
        # `seizes` and `releases` are read several times per entity and only
        # change with the action: worked out here, wherever it is set (the
        # constructor, the loaded params), so the two can never disagree.
        self._action = value
        self.seizes = value.startswith("Seize")
        self.releases = value.endswith("Release")
        self._visit = _UNDECIDED        # the one-generator visit (R4) is asked again

    def self_process(self, entity):
        if self.seizes or self.releases:
            if not self.resource_name and not self.worker_fleet:
                raise RuntimeError(f"Process block '{self.name}' has no resource; "
                                   f"set resource_name in its params.")

        slot = None
        if self.seizes:
            # `balk_if` / `renege_after` tested here: most stations set neither,
            # and each helper is a call per entity to find that out.
            if self.balk_if and entity._since is None and balks(self, entity):
                leave(self, entity, "balk")
                return
            # `_now`: the clock without the property, here and below (speed review R3)
            joined_at = self.env._now if entity._since is None else entity._since
            if self.space is not None:
                # A finite input queue (PHASE4 T5): take a place first, which is
                # what frees whatever machine is still holding this part.
                slot = yield from self.space.take(entity)
            plain = self._plain
            if plain is _UNDECIDED:
                plain = self._plain = plain_target(self) if self.resource_name else None
            if plain is not None and entity._since is None:
                yield from acquire_plain(self, entity, self.resource_name, plain, self.priority)
            elif self.resource_name and not (yield from acquire(
                    self, entity, self.resource_name, self.quantity, self.priority,
                    patience=patience_of(self, entity) if self.renege_after else None,
                    preempt=self.preempt)):
                if slot is not None:
                    self.space.give_back(slot)   # a reneging part frees its place
                self.renege_wait.add(self.env.now - joined_at)
                leave(self, entity, "renege")
                return
            if slot is not None:
                self.space.give_back(slot)       # in service now, no longer waiting
        if self.hold_until_space:
            # Blocking BEFORE service (PHASE5 O4): no job starts until the part
            # has a place downstream, reserved here and carried there.
            yield from blocking.reserve(self, entity)
        if self.worker_fleet:
            # D114: the machine first, then the worker, so none idles waiting.
            yield from acquire_worker(self, entity, self.priority)

        # `active_entity` and `remove_active_entity` written out (speed review A): a
        # PROCESS has no `_resource`, so the setter's capacity check never applies.
        active = self._active_entity
        active.append(entity)
        yield from self._serve(entity)
        try:
            active.remove(entity)
        except ValueError:
            pass
        worker = bool(self.worker_fleet) and self.action != "Seize Delay"
        stays = worker and self.worker_when_blocked == "stays"
        # A part with a reserved place is accepted downstream on arrival, so a
        # station that blocked BEFORE service never blocks after it (O4). A
        # worker that stays makes even a station with no machine hold its part.
        holds = (self.releases and self.block_after_service and not self.hold_until_space
                 and bool(self.resource_name or stays))
        if worker and not (holds and stays):
            # D114, D171 "leaves": the worker leaves when the WORK ends -- a
            # machine blocked after service holds the part, not the operator.
            release(self, entity, self.worker_fleet)

        if holds:
            # ARCHITECTURE D78: the part is finished but the machine is not free
            # until something downstream has room for it.
            yield from blocking.hold_until_accepted(self, entity, 0)
            if stays:
                release(self, entity, self.worker_fleet)     # D171: it waited with the part
            if self.resource_name:
                release(self, entity, self.resource_name)
            return

        if self.releases and self.resource_name:
            release(self, entity, self.resource_name)

        self.forward(entity, 0)

    def _serve(self, entity):
        """The service phase: the changeover, then the service, inside the
        preemption resume loop. MOTION (D156) overrides only this method, so
        seizing, workers, blocking and release stay this block's code."""
        service = self.sample_duration(self.delay, entity)
        started = None          # set once the SERVICE itself starts
        try:
            # Inside the try since DEVLOG #105: a preemption landing during
            # the CHANGEOVER used to escape as an uncaught simpy.Interrupt
            # and crash the whole run. Since PHASE4 T7 the setup phase obeys
            # `on_preempt` like the service does: "resume" keeps the minutes
            # already spent changing over, "restart" pays the whole
            # changeover again (ARCHITECTURE D55).
            if (self.setup or self.setup_cost) and self.seizes:
                yield from self._changeover(entity)
            started = self.env._now
            entity._ends = started + service        # D235: what a state reads
            yield self.env.timeout(service)
        except simpy.Interrupt:
            yield from self._preempted(entity, service, service, started)

    def _preempted(self, entity, service, remaining, started):
        """The service after a preemption (the loop `_serve` ran until review R4, from its
        `except`): the unit is given back, seized again, and the job resumed or restarted,
        as often as it is preempted. *started* is None when the interrupt came during the
        changeover."""
        while True:
            entity._ends = None
            # Preempted: a higher-priority entity took the unit. "resume"
            # keeps the time already served, "restart" charges the SAME
            # sample again (the job did not change, only its luck) -- or,
            # with `resample` (PHASE5 O4), a fresh draw from this block's
            # own stream. An interrupt during the changeover leaves
            # `remaining` alone -- the service had not started.
            if started is not None:
                if self.on_preempt != "restart":
                    remaining = max(remaining - (self.env.now - started), 0.0)
                elif self.resample:
                    remaining = service = self.sample_duration(self.delay, entity)
                else:
                    remaining = service
            preempt_release(self, entity, self.resource_name)
            yield from acquire(self, entity, self.resource_name, self.quantity,
                               self.priority, preempt=self.preempt, resumed=True)
            started = None
            try:
                if (self.setup or self.setup_cost) and self.seizes:
                    yield from self._changeover(entity)
                started = self.env._now
                entity._ends = started + remaining
                yield self.env.timeout(remaining)
                return
            except simpy.Interrupt:
                continue

    def _changeover(self, entity):
        """Charge the setup time when this block's resource switches jobs.

        The previous key is kept PER BLOCK, not per resource unit: the resource
        layer has no unit identity (a simpy request is granted a unit, never a
        named one), so with capacity > 1 two concurrent jobs share one "what the
        station is set up for" slot. Exact for the usual single-server station;
        documented in ARCHITECTURE D52, where per-unit tracking is the upgrade
        path if a multi-server station with setups ever needs it.

        An interrupted changeover is resumed, not repeated, under
        `on_preempt="resume"` (PHASE4 T7): `entity._changeover` carries what is
        still owed, so the minutes already spent count towards it. Under
        "restart" the whole changeover is paid -- and counted and costed --
        again, which is what a station taken over mid-setup really does.

        ponytail: per-block previous key; per-unit if capacity > 1 with setups
        ever matters.
        """
        pending = getattr(entity, "_changeover", None)
        if pending is None:
            try:
                key = (evaluate_expression(self, self.setup_key, entity) if self.setup_key
                       else getattr(entity, "e_type", ""))
            except ExpressionError as exc:
                raise ExpressionError(f"Process block '{self.name}': setup_key: {exc}") from None
            previous, self._previous_key = self._previous_key, key
            if previous is None or previous == key:
                return          # first job on this resource, or no changeover
            # The changeover HAPPENED. It is counted and costed here, before the
            # duration is even looked at, so a zero-time changeover that still
            # costs money (scrap, a technician) is charged exactly once (T3).
            self.setup_count += 1
            scope = {"previous_key": previous, "key": key}
            if self.setup_cost:
                try:
                    amount = float(evaluate_expression(self, self.setup_cost, entity, extra=scope))
                except (ExpressionError, TypeError, ValueError) as exc:
                    raise ExpressionError(
                        f"Process block '{self.name}': setup_cost: {exc}") from None
                costs.accrue(self.ctx, self.cost_item_name, amount, self.setup_cost_category
                             or _DEFAULT_SETUP_CATEGORY)
            duration = 0.0
            if self.setup:
                try:
                    duration = float(evaluate_expression(self, self.setup, entity, extra=scope))
                    duration = scaled(duration, self._setup_factor)
                except (ExpressionError, TypeError, ValueError) as exc:
                    raise ExpressionError(f"Process block '{self.name}': setup: {exc}") from None
            if duration <= 0:
                return
            self.emit("setup_started", entity=entity, duration=duration,
                      previous_key=previous, key=key)
            pending = (duration, previous, key)

        left, previous, key = pending
        entity._changeover = pending
        began = self.env.now
        try:
            yield self.env.timeout(left)
        except simpy.Interrupt:
            # Only the time actually spent changing over is ever reported.
            self.setup_time_total += self.env.now - began
            self._accrue_schedule_setup(entity, self.env.now - began)
            # The changeover did not finish, so the station is NOT set up for
            # this job: put the previous key back, so whatever takes the unit
            # over changes over from where the station really is (DEVLOG #105).
            self._previous_key = previous
            if self.on_preempt == "restart":
                del entity._changeover            # the whole changeover again
            else:
                entity._changeover = (max(left - (self.env.now - began), 0.0), previous, key)
            raise
        self.setup_time_total += self.env.now - began
        self._accrue_schedule_setup(entity, self.env.now - began)
        self._previous_key = key
        del entity._changeover

    def _accrue_schedule_setup(self, entity, duration: float) -> None:
        """This HOLD's changeover share, for the "schedule_log" frame's Setup
        column (PHASE6 W4 P2, ARCHITECTURE D120) -- added to the hold's own
        `req_params` entry (read back by `seizing.release`), not a plain
        entity attribute, so two resources one entity holds at once never
        share a counter. A no-op unless `outputs.schedule` is set."""
        if self.ctx.schedule_log is None:
            return
        info = entity.req_params.get(self.resource_name)
        if info is not None:
            info["setup"] = info.get("setup", 0.0) + duration

    def self_run(self, entity):
        if entity._since is None:
            visit = self._visit
            if visit is _UNDECIDED:
                visit = self._visit = self._plain_visit()
            if visit:
                return self.env.process(self._visit_plain(entity))
        return self.env.process(self.self_process(entity))

    def _plain_visit(self) -> bool:
        """Whether this block's visits may run as `_visit_plain` (speed review R4): a Seize
        Delay Release on a plain resource (`plain_target`), with no balking, finite space,
        blocking, worker or changeover, and `self_process`/`_serve` not overridden (MOTION
        overrides `_serve`)."""
        if (type(self).self_process is not Process.self_process
                or type(self)._serve is not Process._serve
                or not (self.seizes and self.releases) or not self.resource_name
                or self.worker_fleet or self.balk_if or self.space is not None
                or self.hold_until_space or self.block_after_service
                or self.setup or self.setup_cost):
            return False
        plain = self._plain
        if plain is _UNDECIDED:
            plain = self._plain = plain_target(self)
        return plain is not None

    def _visit_plain(self, entity):
        """One visit as one generator (speed review R4): `self_process` for the blocks
        `_plain_visit` admits, with `seizing.acquire_plain`, the first service attempt of
        `_serve` and `seizing.release` written out. The same statements in the same order,
        so the same request, collectors, record, costs, events and draws; a preemption by
        another block's request goes on in `_preempted`, as in `_serve`."""
        env, ctx = self.env, self.ctx
        resource, target_name = self._plain, self.resource_name
        # -- seizing.acquire_plain
        attrs = entity.__dict__          # `req_params` is an instance attribute or absent
        held = attrs["req_params"] if "req_params" in attrs else None
        if held is not None and target_name in held:
            raise RuntimeError(f"Entity {entity.id} already requested resource '{target_name}'.")
        asked = env._now
        if ctx.bus._subscribers:
            entity._queued = next(ctx.queue_seq)      # #349
            entity._asked = asked                     # D383
        request = resource.request_plain(self.priority)
        yield request
        now = env._now
        if ctx.entity_waits:      # D1232
            entity._total_wait = getattr(entity, "_total_wait", 0.0) + now - asked
        held = attrs["req_params"] if "req_params" in attrs else None
        if held is None:
            held = entity.req_params = {}
        held[target_name] = {"resource": resource, "requests": [request], "set": None,
                             "seized_at": now}
        if resource.cost_per_use:
            _accrue_usage_cost(self, entity, resource, 1)
        if ctx.debug:       # seizing's logger, as `acquire_plain` and `release` log through
            _seize_log.debug("Entity %s acquired '%s' at time %s.", entity.id, resource.name, now)
        if ctx.bus._subscribers:
            self.emit("entity_seized", entity=entity, resource=resource.name, value=1)
        # -- self_process and the first attempt of _serve
        active = self._active_entity
        active.append(entity)
        service = self.sample_duration(self.delay, entity)
        try:
            started = env._now
            entity._ends = started + service        # D235: what a state reads
            yield env.timeout(service)
        except simpy.Interrupt:
            yield from self._preempted(entity, service, service, started)
        try:
            active.remove(entity)
        except ValueError:
            pass
        # -- seizing.release written out for a hold of the plain shape (the four keys
        # `acquire_plain` writes, no set), which a job seized again after a preemption also has:
        # the full `acquire` writes the same record. Anything else (no hold, a set, a vehicle,
        # a worker, a schedule-log row or its setup share) goes through `seizing.release`.
        held = attrs["req_params"] if "req_params" in attrs else None
        info = held.get(target_name) if held is not None else None
        if (ctx.schedule_log is not None or info is None or info["set"] is not None
                or len(info) != 4):
            release(self, entity, target_name)
        else:
            member, units = info["resource"], info["requests"]
            if member.cost_busy:
                _accrue_busy_cost(self, entity, member, len(units), info["seized_at"])
            for unit in units:
                member.release(unit)
            if ctx.bus._subscribers:
                self.emit("entity_released", entity=entity, resource=member.name,
                          value=len(units))   # units (#748)
            if ctx.debug:
                _seize_log.debug("Entity %s released resource '%s' at time %s.", entity.id,
                          member.name, env.now)
            del held[target_name]
            if not held:
                del entity.req_params
        self.forward(entity, 0)

    def get_params_dict(self):
        return {
            "action": self.action,
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
            "priority": self.priority,
            "quantity": self.quantity,
            "resource_name": self.resource_name,
            "setup": self.setup,
            "setup_key": self.setup_key,
            "initial_key": self.initial_key,
            "setup_cost": self.setup_cost,
            "setup_cost_category": self.setup_cost_category,
            "preempt": self.preempt,
            "on_preempt": self.on_preempt,
            "resample": self.resample,
            "queue_capacity": self.queue_capacity,
            "hold_until_space": self.hold_until_space,
            "space_of": self.space_of,
            "worker_fleet": self.worker_fleet,
            "worker_skill": self.worker_skill,
            "node": self.node,
            "worker_when_blocked": self.worker_when_blocked,
            **self.queue_params_dict(),
        }

    def set_params_from_dict(self, param_dict):
        self.action = param_dict.get("action", ACTIONS[0])
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)

        self.priority = param_dict.get("priority", 0)
        self.quantity = param_dict.get("quantity", 1)
        self.resource_name = param_dict.get("resource_name", "")

        self.setup = param_dict.get("setup", "")
        self._setup_factor = field_factor(param(SPEC, "setup"), param_dict, self.ctx.units,
                                          SPEC.params)
        self.setup_key = param_dict.get("setup_key", "")
        self.initial_key = param_dict.get("initial_key", "")
        self.setup_cost = param_dict.get("setup_cost", "")
        self.setup_cost_category = param_dict.get("setup_cost_category", "")
        self._previous_key = self.initial_key or None
        if self.setup_cost:
            # Created eagerly, like RECORD's cost item: a block that never
            # changes over still reports its (zero) changeover cost.
            costs.accrue(self.ctx, self.cost_item_name, 0.0,
                         self.setup_cost_category or _DEFAULT_SETUP_CATEGORY)

        self.set_queue_params(param_dict)
        self._plain = _UNDECIDED        # the params decide it: asked again at the next entity
        self._visit = _UNDECIDED
        self.preempt = bool(param_dict.get("preempt", False))
        self.on_preempt = param_dict.get("on_preempt", ON_PREEMPT_MODES[0])
        self.resample = bool(param_dict.get("resample", False))
        self.queue_capacity = max(int(param_dict.get("queue_capacity", 0) or 0), 0)
        self.space = (blocking.Space(self.env, self.queue_capacity, self)
                      if self.queue_capacity > 0 else None)
        self.hold_until_space = bool(param_dict.get("hold_until_space", False))
        self.space_of = str(param_dict.get("space_of", "") or "")
        self.worker_fleet = str(param_dict.get("worker_fleet", "") or "").strip()
        self.worker_skill = str(param_dict.get("worker_skill", "") or "").strip()
        self.node = str(param_dict.get("node", "") or "").strip()
        self.worker_when_blocked = (param_dict.get("worker_when_blocked")
                                    or WORKER_BLOCKED_MODES[0])

        if self.resource_name and not self.ctx.resource_manager.exists(self.resource_name):
            # Same fail-soft as Seize (DEVLOG #17): the layout declares resources
            # first, so an unknown name here was never declared.
            log.warning(f"Resource '{self.resource_name}' is not declared in the model; "
                        f"registering it with capacity 1.")
            self.ctx.resource_manager.register(name=self.resource_name, capacity=1)


SPEC = BlockSpec(
    type_name="PROCESS", cls=Process,
    params=(
        ParamSpec("action", str, ACTIONS[0],
                  description="Which of seize/delay/release run: \"Seize Delay Release\", \"Seize Delay\", "
                              "\"Delay Release\" or \"Delay\".", quantity="none"),
        *DIST_PARAMS,
        ParamSpec("priority", int, 0, description="Lower draws first when the resource's queue is contested. One number for the block (an entity attribute is not read): to rank entity types, route each to its own block on the shared resource.", quantity="none"),
        ParamSpec("quantity", int, 1, description="Number of resource units requested/released.", quantity="count", min=1),
        ParamSpec("resource_name", str, "",
                  description="Name of the resource or resource set; required unless action is "
                              "\"Delay\" or a worker_fleet does the work alone.", quantity="none"),
        ParamSpec("setup", str, "",
                  description="Sequence-dependent setup time: an expression charged "
                              "after the seize when this block's resource changes over, e.g. "
                              "TABLE(\"Setups\", previous_key, key). `previous_key` and `key` are "
                              "in scope here only. Empty = no setup.", quantity="time", kinds=("number", "expression"), min=0),
        ParamSpec("setup_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `setup`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("setup_key", str, "",
                  description="Expression identifying what a job is set up for; empty uses the "
                              "entity type. A setup is charged only when it differs from the "
                              "previous job's key at this block.", quantity="none"),
        ParamSpec("initial_key", str, "",
                  description="What the resource is already set up for at time 0; empty means the "
                              "first job is charged no setup.", quantity="none"),
        ParamSpec("setup_cost", str, "",
                  description="Money charged ONCE PER CHANGEOVER, not per job -- an "
                              "expression like TABLE(\"Changeover cost\", previous_key, key), "
                              "with `previous_key` and `key` in scope exactly as in \"setup\". "
                              "It becomes an event cost item named after the block, joins Total "
                              "Cost and resets at warm-up. Empty = free changeovers.", quantity="money", kinds=("number", "expression")),
        ParamSpec("setup_cost_category", str, "",
                  description="Cost category the changeover cost is grouped under in the "
                              "cost_summary frame (default \"Changeover\").", quantity="none"),
        *QUEUE_PARAMS,
        ParamSpec("preempt", bool, False,
                  description="When no unit is free, interrupt the lowest-priority job "
                              "in service on this resource (a strictly higher priority wins). Only "
                              "with action \"Seize Delay Release\", and not on a resource set.", quantity="none"),
        ParamSpec("queue_capacity", int, 0,
                  description="Places in this station's own input queue. 0 (the "
                              "default) is an unlimited queue; with a positive value a part "
                              "takes a place on arrival and frees it when service starts, so "
                              "an upstream machine with nowhere to put its finished part stays "
                              "blocked. `balk_if` sees the places taken as `queue_length`, which "
                              "is how a LOSS system is written instead of a blocking one.", quantity="count", min=0),
        *HOLD_SPACE_PARAMS,
        *WORKER_PARAMS,
        WORKER_WHEN_BLOCKED,
        ParamSpec("on_preempt", str, ON_PREEMPT_MODES[0],
                  description="What THIS block's job does when something interrupts it and it gets "
                              "the resource back: \"resume\" (finish the remaining time) or "
                              "\"restart\" (the whole service again, same sample). Set on the block "
                              "being interrupted, not on the one doing the interrupting.", quantity="none"),
        ParamSpec("resample", bool, False,
                  description="With on_preempt \"restart\" only: draw a FRESH service "
                              "time from this block's own random stream on each restart, instead "
                              "of repeating the same sample. Off (the default) repeats it.", quantity="none"),
    ),
    factory=lambda env, block: Process(env, block, delay=1),
    family="work", shape="double",
    duration=True, holding=True, service=True, queueing=True, uses_resource=True,
)
