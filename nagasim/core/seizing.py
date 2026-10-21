"""Seize/release mechanics shared by SEIZE, RELEASE and PROCESS (ROADMAP 1.2).

One place owns: resolving a name to a resource or a set, the queue discipline's
rank, the wait-for-any over a set's members, what the entity records as held,
and the `entity_seized` / `entity_released` events (always naming the member
actually seized, never the set).

PHASE4 A4 puts the three queue behaviours here too, because all three are
things that happen to a REQUEST: balking (never join), reneging (leave after a
patience) and the release half of a preemption. `QueueBehaviour` carries the
params and counters the two queueing blocks (SEIZE, PROCESS) share; the
semantics are in ARCHITECTURE D54/D55.
"""
from __future__ import annotations

import logging

from nagasim.core import blocking
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.resource_manager import ResourceSet
from nagasim.core.stats import Tally
from nagasim.core.block_spec import QUEUE_PARAMS
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.timeunits import base_factor
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["acquire", "acquire_worker", "release", "QueueBehaviour", "balks", "patience_of", "leave",
           "preempt_release", "ON_PREEMPT_MODES", "LEAVE_OUTPUT", "QUEUEING_BLOCK_TYPES"]

# The blocks that queue for a resource, and so carry the queue behaviours.
# Kept as a literal: blocks import this module, so it cannot import the block
# registry; tests/test_block_registry.py checks it equals the SPEC.queueing flags.
QUEUEING_BLOCK_TYPES = ("SEIZE", "PROCESS", "MOTION")

# Outgoing link a balking or reneging entity takes (ARCHITECTURE D54): branch
# index is link order, exactly as for DECIDE, and index 1 is "left without
# service". schema.validate_graph_structured requires it wherever either option is set.
LEAVE_OUTPUT = 1

# What happens to the remaining service of a preempted job (ARCHITECTURE D55).
ON_PREEMPT_MODES = ("resume", "restart")


class QueueBehaviour:
    """Balking/reneging params, counters and the leave path, mixed into the two
    blocks that queue for a resource (SEIZE and PROCESS). PROCESS adds
    `preempt`/`on_preempt` of its own -- only a block that owns the whole
    service can offer the interrupted job a resume (ARCHITECTURE D55)."""

    def init_queue_behaviour(self) -> None:
        self.balk_if = ""
        self.renege_after = ""
        self.renege_time_unit = "base"
        self._renege_factor = 1.0
        self.balked_count = 0
        self.reneged_count = 0
        self.preempted_count = 0
        self.renege_wait = Tally()

    @property
    def target_name(self) -> str:
        """The resource or set this block queues for (PROCESS keeps the name,
        SEIZE the resolved object)."""
        return getattr(self, "resource_name", "") or getattr(getattr(self, "resource", None), "name", "")

    @property
    def queue_behaviour(self) -> bool:
        """True once the block declares any of the three behaviours -- what
        earns it a row in the `queue_summary` frame, and what makes its second
        outgoing link mandatory."""
        return bool(self.balk_if or self.renege_after or getattr(self, "preempt", False))

    def queue_params_dict(self) -> dict:
        return {"balk_if": self.balk_if, "renege_after": self.renege_after,
                "renege_time_unit": self.renege_time_unit}

    def set_queue_params(self, param_dict: dict) -> None:
        self.balk_if = param_dict.get("balk_if", "")
        self.renege_after = param_dict.get("renege_after", "")
        self.renege_time_unit = param_dict.get("renege_time_unit", "base")
        self._renege_factor = field_factor(param(QUEUE_PARAMS, "renege_after"), param_dict,
                                           self.ctx.units, QUEUE_PARAMS)

    def reset_queue_stats(self) -> None:
        """Warm-up hook (called by sim_runner.reset_statistics)."""
        self.balked_count = 0
        self.reneged_count = 0
        self.preempted_count = 0
        self.renege_wait.reset()


def _queue_length(target) -> int:
    """Entities waiting on *target* right now -- a set counts an entity once,
    however many member queues its requests sit in."""
    if target is None:
        return 0
    if isinstance(target, ResourceSet):
        return int(target.queue_len.level)
    return target.waiting


def balks(block, entity) -> bool:
    """Whether *entity* turns away on arrival instead of joining the queue.

    `balk_if` sees the queue it would join as `queue_length`, on top of the
    entity's attributes, the model's variables and TABLE() -- the usual
    expression scope.
    """
    expr = getattr(block, "balk_if", "")
    if not expr:
        return False
    space = getattr(block, "space", None)
    if space is not None:
        # A finite input queue IS the queue this entity would join, so `queue_length`
        # is the places taken -- which is how a loss system is written (D78).
        length = int(space.content.level)
    else:
        length = _queue_length(block.ctx.resource_manager.resolve(block.target_name))
    scope = block.expression_scope(entity, extra={"queue_length": length})
    try:
        return bool(compile_expression(expr).evaluate(scope))
    except ExpressionError as exc:
        raise ExpressionError(f"Block '{block.name}': balk_if: {exc}") from None


def patience_of(block, entity):
    """How long *entity* is willing to wait, or None when it waits for ever.

    An expression in `renege_time_unit` (default the model's base unit), so
    EXPO(15), a per-entity attribute or TABLE("Patience", entity.e_type) all
    work -- the same shape as PROCESS's setup time (PHASE4 A2).
    """
    expr = getattr(block, "renege_after", "")
    if not expr:
        return None
    if "_renege_at" in entity.__dict__:
        return 0.0      # a warm-started wait: `acquire` keeps the deadline it carries (D261)
    try:
        value = float(compile_expression(expr).evaluate(block.expression_scope(entity)))
    except (ExpressionError, TypeError, ValueError) as exc:
        raise ExpressionError(f"Block '{block.name}': renege_after: {exc}") from None
    value = scaled(value, getattr(block, "_renege_factor", 1.0))
    return max(value, 0.0)


def leave(block, entity, reason: str) -> None:
    """Send *entity* out of the block's SECOND output, unserved (D54).

    `entity.left_reason` is an ordinary attribute, so a DECIDE downstream can
    branch on it and the entity log carries it like any other.
    """
    entity.left_reason = reason
    if block.space_aware:
        blocking.drop_reservation(entity)   # PHASE5 O4: a place it will never use
    if reason == "balk":
        block.balked_count += 1
    else:
        block.reneged_count += 1
    block.emit("entity_left_queue", entity=entity, resource=block.target_name, reason=reason)
    if block.ctx.debug:
        log.debug("Entity %s left '%s' (%s) at time %s.", entity.id, block.name, reason, block.env.now)
    try:
        block.forward(entity, LEAVE_OUTPUT)
    except RuntimeError:
        raise RuntimeError(
            f"Block '{block.name}': an entity {reason}ed but the block has no second outgoing "
            f"link; link it to where entities that leave without service go.") from None


def preempt_release(block, entity, target_name) -> None:
    """The units this entity held were taken by a higher-priority job: give
    them back and count the interruption (PHASE4 A4).

    Releasing here is what keeps utilization and busy cost exact -- the entity
    is charged for the time it actually held the resource, and the re-request
    that follows is marked `resumed` so `cost_per_use` is charged once per job.
    """
    block.preempted_count += 1
    block.emit("entity_preempted", entity=entity, resource=target_name)
    release(block, entity, target_name)


def acquire(block, entity, target_name, quantity=1, priority=0, *,
            patience=None, preempt=False, resumed=False, member=None, held_since=None):
    """Generator: hold *quantity* units of the resource or set *target_name*.

    Records what the entity actually holds under *target_name*, so a Release
    naming the same set releases the member that was chosen here.

    Returns True once the entity holds the units, False when *patience* ran out
    first and it left the queue with nothing (PHASE4 A4). *member*: of a set,
    the one a warm-started job in service already had (D262), taken without
    asking the set's rule. *preempt* lets the
    request interrupt a lower-priority job in service; *resumed* marks the
    re-request of a job that was itself preempted, which is not a new seize.
    *held_since*: a warm-started job in service holds its units as of then (#362), from
    the moment its requests are made: a preempting request made in the same instant must
    not pick it as its victim (#468).
    """
    target = block.ctx.resource_manager.resolve(target_name)
    if target is None:
        raise RuntimeError(f"Block '{block.name}' has no resource; set resource_name in its params.")

    # Read, do not create: `release` deletes the dict once it empties, and with
    # blocking after service that can happen WHILE this request is still queued
    # (the upstream machine is freed the moment this entity is accepted). The
    # dict is created below, right before it is written to.
    if target_name in getattr(entity, "req_params", {}):
        raise RuntimeError(f"Entity {entity.id} already requested resource '{target_name}'.")

    # Queue discipline "Plan" (PHASE6 W4 P4, D122): None unless a resource of
    # this model uses it, so every other model skips this in one test.
    gates = block.ctx.plan_gates
    if gates is not None and not _plans(target):
        gates = None
    if gates is not None:
        # The gate wait is part of the patience (#210): a renege, at the gate
        # or in the queue, gives the entity's turn up so the rest still run.
        joined = block.env.now
        if not (yield from gates.wait(entity, patience)):
            gates.started(entity)
            return False
        if patience is not None:
            patience = max(patience - (block.env.now - joined), 0.0)

    # Only a ranking resource reads the scope (a set always may: its members
    # decide), so a plain FIFO seize does not build one.
    if block.ctx.bus._subscribers:      # #349: the queue's own order, for a state
        entity._queued = next(block.ctx.queue_seq)
    until = None
    if patience is not None:
        # D261: when this wait gives up, for a state to read; a warm-started wait
        # brings its own and is timed to that very instant.
        until = entity.__dict__.pop("_renege_at", None)
        entity._deadline = block.env.now + patience if until is None else until
    scope = block.expression_scope(entity) if getattr(target, "needs_scope", True) else None
    since = entity._since           # None but for a warm-started entity's first wait
    if since is not None:
        entity._since = None
    if block.ctx.bus.has_subscribers:   # D383: when it asked, for a state (not when it arrived:
        entity._asked = block.env.now if since is None else since   # a place may come first)
    if isinstance(target, ResourceSet):
        member, requests = yield from _acquire_from_set(block, target, quantity, priority, scope,
                                                        patience, since, member, until,
                                                        held_since)
        granted = member is not None
    else:
        member = target
        requests = [target.request(priority=priority, rank=target.rank_for(scope, since),
                                   preempt=preempt, charged=not resumed, scope=scope,
                                   since=since)
                    for _ in range(quantity)]
        _as_of(target, requests, since if held_since is None else held_since)
        if patience is None:
            # Waited for here, not in `_wait`: the usual seize has no patience
            # and does not need a second generator to yield through.
            for request in requests:
                yield request
            granted = True
        else:
            granted = yield from _wait(block, target, requests, patience, until)
    if not granted:
        if gates is not None:
            gates.started(entity)
        return False
    if gates is not None:
        gates.started(entity)

    if block.space_aware:
        # The station has the part now, so whatever machine was still holding it
        # is free -- the zero-buffer case of blocking after service (D78).
        blocking.accept(entity)

    if not hasattr(entity, "req_params"):
        entity.req_params = {}
    entity.req_params[target_name] = {
        "resource": member,
        "requests": requests,
        "set": target if isinstance(target, ResourceSet) else None,
        "seized_at": block.env.now,   # ROADMAP 2.2: start of the busy-cost holding period
    }
    if not resumed:
        _accrue_usage_cost(block, entity, member, len(requests))
    if block.ctx.debug:
        log.debug("Entity %s acquired '%s' at time %s.", entity.id, member.name, block.env.now)
    block.emit("entity_seized", entity=entity, resource=member.name, value=len(requests))  # units (#748)
    if block.space_aware:
        # D169: the station holds the part, but a cobot still placing it
        # finishes its cycle before the job starts.
        yield from blocking.placed(entity)
    return True


def _as_of(resource, requests, since) -> None:
    """#362, #468: the requests of a warm-started entity keep, as their key's time, when it
    seized (a job in service) or asked (a waiting one), not the clock: a preempting request
    made in the same instant neither picks a job in service as its victim nor goes ahead of
    the waiting ones that asked before it, which SimPy's key would order by the preempt flag."""
    if since is None:
        return
    queue = resource._core.put_queue
    for request in requests:
        request.time = since
        request.key = (request.priority, since, not request.preempt)
        if request in queue:        # still waiting: back in its place by the new key
            queue.remove(request)
            queue.append(request)


def _plans(target) -> bool:
    """Whether *target* (a resource, or any member of a set) uses "Plan"."""
    members = target.members if isinstance(target, ResourceSet) else (target,)
    return any(member.discipline == "Plan" for member in members)


def acquire_worker(block, entity, priority=0):
    """Generator (PHASE6 W3-M3, D114): a worker from *block*'s `worker_fleet`
    -- one with `worker_skill` when set -- walks EMPTY on the network to the
    block's `node` and stays there, 'handling', until `release` names the fleet.

    Requested only AFTER the block's own resource, so no worker ever idles at a
    station waiting for its machine. The hold is recorded like any seize, so
    busy cost runs from the grant (the walk is part of the job) and a RELEASE
    naming the fleet ends it.
    """
    fleet = block.ctx.fleets.get(block.worker_fleet)
    if fleet is None:
        raise RuntimeError(f"Block '{block.name}': unknown worker fleet '{block.worker_fleet}'. "
                           f"Declare it under \"fleets\" with kind \"worker\".")
    if not block.node:
        raise RuntimeError(f"Block '{block.name}': a worker needs the block's `node` to walk to.")
    if fleet.name in getattr(entity, "req_params", {}):
        raise RuntimeError(f"Entity {entity.id} already requested resource '{fleet.name}'.")
    request, mover = yield from fleet.acquire(block, entity, block.node, priority,
                                              skill=block.worker_skill or None)
    seized_at = block.env.now
    if block.space_aware:
        blocking.accept(entity)      # a worker-only station takes the part here (D78)
        yield from blocking.placed(entity)    # ... once a cobot has put it down (D169)
    _accrue_usage_cost(block, entity, fleet.resource, 1)
    yield from fleet.drive(mover, block.node, loaded=False, block=block)
    mover.enter("handling", block.env.now)
    if not hasattr(entity, "req_params"):
        entity.req_params = {}
    entity.req_params[fleet.name] = {"resource": fleet.resource, "requests": [request],
                                     "set": None, "seized_at": seized_at,
                                     "mover": mover, "fleet": fleet}


def _wait(block, resource, requests, patience, until=None):
    """Wait for every request, giving up after *patience* (a number: `acquire`
    waits for ever itself).

    Tie rule (ARCHITECTURE D54): a grant that lands in the SAME instant as the
    patience expiry wins, which is what the zero-delay re-check buys -- without
    it the winner would depend on which of the two events SimPy happened to
    schedule first.

    ponytail: a patience timer that is never needed stays in SimPy's event
    queue as a no-op (SimPy has no cancel), so a run with no time limit can
    drain up to one patience later than it otherwise would. Cheap to avoid
    where it matters (the timer is only armed when the entity actually waits);
    give a reneging model a run length.
    """
    env = block.env
    if _granted(requests):       # a free unit: granted in this instant, no wait at all
        yield env.all_of(requests)
        return True
    yield env.all_of(requests) | (env.timeout(patience) if until is None
                                  else timeout_at(env, until))
    if not _granted(requests):
        yield env.timeout(0)     # the tie rule: let this instant finish first
    if _granted(requests):
        # A FRESH condition every time: when the timer wins the race above,
        # SimPy strips the old one's callbacks off the requests recursively
        # (Condition._remove_check_callbacks), so reusing it would wait for an
        # event that can no longer fire.
        yield env.all_of(requests)
        return True
    for request in requests:
        resource.cancel(request)
    return False


def _granted(requests) -> bool:
    """Whether every request has been given its unit.

    The REQUESTS, not the AllOf over them: a request that SimPy granted earlier
    in this same instant is already `triggered`, while the condition only fires
    once that request is processed. Waiting for the condition here would make
    the tie rule depend on the order SimPy happened to schedule two events at
    the same timestamp (ARCHITECTURE D54).
    """
    return all(request.triggered for request in requests)


def _acquire_from_set(block, rset, quantity, priority, scope, patience=None, since=None,
                      forced=None, until=None, held_since=None):
    """Pick a member with enough free units; if there is none, queue on ALL of
    them and take the first that comes through.

    The set owns the queue statistics for both paths (member requests are made
    with counted=False), so an entity queued on three members is one entity in
    one queue, and the losing requests leave no trace: a request granted in the
    same instant as the winner is released immediately, one still queued is
    cancelled through the wrapper, which un-counts it.

    ponytail: with quantity > 1 the wait-for-any holds partially granted units
    of the losing members until the winner completes; two entities each holding
    half of two members can stall. Per-member atomic reservation is the upgrade
    path if a model ever needs it.

    Returns (None, []) when *patience* ran out while the entity was pending on
    every member (PHASE4 A4): all the pending halves are cancelled, so a renege
    against a set leaves no more trace than a losing half does.
    """
    enqueued_at = rset.enqueue(since)
    # *forced* (D262): neither the rule, its cursor nor its random draw is touched.
    member = (rset.pick(quantity, block.rng) if forced is None
              else next(m for m in rset.members if m.name == forced))

    if member is not None:
        requests = [member.request(priority=priority, rank=member.rank_for(scope, since),
                                   counted=False,
                                   scope=scope)
                    for _ in range(quantity)]
        _as_of(member, requests, held_since)
        for request in requests:
            yield request
    else:
        pending = {m: [m.request(priority=priority, rank=m.rank_for(scope, since), counted=False,
                                 scope=scope)
                       for _ in range(quantity)]
                   for m in rset.members}
        groups = {m: block.env.all_of(reqs) for m, reqs in pending.items()}
        if patience is None:
            yield block.env.any_of(list(groups.values()))
        else:
            yield block.env.any_of(list(groups.values()) + [
                block.env.timeout(patience) if until is None else timeout_at(block.env, until)])
            if not any(_granted(reqs) for reqs in pending.values()):
                yield block.env.timeout(0)      # same tie rule as _wait
            if not any(_granted(reqs) for reqs in pending.values()):
                for loser, reqs in pending.items():
                    for request in reqs:
                        loser.cancel(request)
                rset.left(enqueued_at)
                return None, []
        # Member order breaks a tie when two members come free in the same instant.
        member = next(m for m in rset.members if _granted(pending[m]))
        requests = pending.pop(member)
        for loser, reqs in pending.items():
            for request in reqs:
                loser.cancel(request)

    rset.granted(enqueued_at, quantity)
    return member, requests


def _accrue_usage_cost(block, entity, member, quantity):
    """Entity's share of `cost_per_use` (ROADMAP 2.2, one unit per granted
    request, mirroring how the resource's own `seize_count` counts). Clipped to
    the post-warm-up period the same way the resource's own counter is (it
    resets to 0 at warm-up), so the entity shares stay in conservation with it."""
    rate = getattr(member, "cost_per_use", 0.0)
    if rate and block.env.now >= block.ctx.warmup:
        entity.add_cost(rate * quantity)


def _accrue_busy_cost(block, entity, member, quantity, seized_at):
    """Entity's share of `cost_busy` for the time it held *quantity* units of
    *member*, from *seized_at* (ROADMAP 2.2). Clipped to start no earlier than
    warm-up, exactly like the resource's own `busy` collector (reset at
    warm-up, keeping its level) -- so entity shares sum to the resource's own
    Busy Cost when no entity is still mid-service at report time."""
    rate = getattr(member, "cost_busy", 0.0)
    if not rate:
        return
    start = max(seized_at, block.ctx.warmup)
    end = block.env.now
    if end <= start:
        return
    # The conversion is fixed once the model is built (the resource's
    # `cost_time_unit` is set when it is created, the model's units before
    # that), so it is worked out at the resource's first charge and kept.
    conv = member.busy_cost_conv
    if conv is None:
        conv = member.busy_cost_conv = base_factor("time", member.cost_time_unit, block.ctx.units)
    entity.add_cost(rate * quantity * (end - start) / conv)


def release(block, entity, target_name):
    """Release everything the entity holds under *target_name*."""
    info = getattr(entity, "req_params", {}).get(target_name)
    if info is None:
        raise ValueError(f"Entity {entity.id} has no resource '{target_name}' to be released.")

    member = info["resource"]
    _accrue_busy_cost(block, entity, member, len(info["requests"]), info.get("seized_at", block.env.now))
    mover = info.get("mover")
    if mover is not None:
        # A worker (W3-M3): its fleet puts it back among the idle and parks it.
        fleet = info["fleet"]
        fleet.free(info["requests"][0], mover, block=block, entity=entity)
        fleet.park_home(mover, block=block)
    else:
        for request in info["requests"]:
            member.release(request)
        if info["set"] is not None:
            info["set"].released(len(info["requests"]))
        block.emit("entity_released", entity=entity, resource=member.name,
                   value=len(info["requests"]))   # units (#748)
    if block.ctx.debug:
        log.debug("Entity %s released resource '%s' at time %s.", entity.id, member.name, block.env.now)

    if block.ctx.schedule_log is not None:
        # PHASE6 W4 P2, ARCHITECTURE D120: one row per hold, recorded here so
        # SEIZE, PROCESS and a worker fleet's release (D114) all feed the same
        # log through the one place a hold actually ends. *member* is always
        # the resolved resource, so a set names the member, never the set
        # (matching `entity_seized`/`entity_released`); "Setup" is 0.0 unless
        # PROCESS's `_changeover` stashed this hold's changeover share on
        # `info["setup"]`.
        block.ctx.schedule_log.append({
            "Order": getattr(entity, "order_id", None),
            "Entity": entity.id,
            "Entity Type": entity.e_type,
            "Block": block.name,
            "Resource": member.name,
            "Unit": mover.unit if mover is not None else None,
            "Start": info.get("seized_at", block.env.now),
            "End": block.env.now,
            "Setup": info.get("setup", 0.0),
        })

    del entity.req_params[target_name]
    if not entity.req_params:      # remove the dict entirely once it is empty
        del entity.req_params
