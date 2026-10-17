"""Warm start: a model that begins with work already in it (ROADMAP 3.4b).

An ordinary run starts empty and needs a warm-up to reach a representative
state. A digital twin does not: it knows what the real system looks like right
now, and the run should start from THAT. The `"initial_state"` section
(schema v13) describes it:

    "initial_state": {
      "time": 0.0,                       # the snapshot's own timestamp (see below)
      "variables": {"shift": 2},
      "entities": [
        {"block": "Triage", "count": 5, "e_type": "PATIENT", "age": 12.0,
         "attributes": {"priority": 1}, "in_service": 1, "remaining": [2.0]}
      ],
      "resources": {"Scanner": {"down_until": 30.0}}
    }

SEMANTICS, exactly as implemented (`apply`, called by `sim_runner` once the
model is built and before any CREATE process starts):

1. `variables` are written into `ctx.variables` over the values the model's own
   "variables" section just set. A name the model does not declare is a
   validation error, never a silently invented variable.

2. `entities`: at t=0, BEFORE any CREATE fires, each entry injects `count`
   entities INTO the named block, in list order, entry by entry, exactly as if
   they had just arrived there -- the block's own process runs for each of
   them, so a PROCESS block queues them on its resource, a HOLD block puts them
   in its store, and so on. Injection order is the order of the list, and
   within an entry the order they are created, so a FIFO queue ends up in
   exactly the order written.

3. `age` (in model base time units, default 0) sets `creation_time = -age`:
   the entity has already been in the system for `age` before the run starts,
   so its reported time in system includes that history. It still counts as
   created in the observed window (`stats.created_in_window` clamps a negative
   creation time at 0) and is flagged `initial=True`, which surfaces as the
   entity log's "Initial" column.

4. `in_service: k` (PROCESS, SEIZE and DELAY only) means the FIRST k entities
   of the entry are already being served, not queueing: each seizes
   immediately -- which succeeds at t=0 as long as k is within the resource's
   free capacity, checked at validation -- and then holds for `remaining[i]` if
   a remaining time is given for it, else for a freshly sampled service time
   from the block's own distribution.

   This is EXACT for an exponential service time (memorylessness: the residual
   of an in-progress exponential service is distributed exactly like a fresh
   one) and an APPROXIMATION for every other service distribution, where the
   residual of a partly finished service is NOT distributed like a fresh
   sample. Give `remaining` explicitly when the residual times are known -- a
   twin reading a real system usually knows them -- and the result is exact for
   any distribution.

   The k in-service entities do not queue: they hold the resource for their
   remaining time and then continue down the flow (a PROCESS releases, a SEIZE
   keeps holding until its RELEASE, a DELAY just forwards). Entities past the
   first k enter the block normally and queue behind them.

5. `resources`: `{"<name>": {"down_until": t}}` starts the resource failed --
   its free units are taken out of service by blocker requests at t=0 exactly
   as `resource_manager.failure_controller` does, and returned at `t`. Units
   already held by an in-service injected entity are NOT taken (same WAIT rule
   as a real failure: a job in service always finishes).

6. `time` is the timestamp of the snapshot the initial state came from. The
   engine's clock always starts at 0, so this field is recorded for the report
   and never shifts the run.

7. `schedule_offset` (PHASE4 T3, model base time units, default 0) is HOW FAR
   INTO ITS OWN CYCLE the calendar already is at model time 0. Every schedule
   -- capacity and arrival alike -- and every `"beyond": "repeat"` time-series
   input is then read at `t + schedule_offset`, folded by its cycle: a run
   warm-started three hours into a two-shift day changes capacity five hours
   in, not eight, and a price profile continues mid-day instead of restarting
   at midnight. Statistics are untouched: the offset moves WHEN the profile is
   read, never the clock, the warm-up or any window.

   ONE offset serves the whole model, because a snapshot has one wall clock.
   Two calendars with different epochs (a shift pattern starting Monday and a
   tariff starting on the first of the month) would need a per-schedule
   offset; nothing asks for that yet.

WARM-UP INTERPLAY. Warm-up discards statistics up to a cutoff, which is the
opposite of what a warm start is for: the initial state IS the history. So
`api.run_model` defaults the warm-up to 0 when an initial state is present and
no warm-up was passed explicitly, and setting both is a validation WARNING
(`W_INITIAL_STATE_WARMUP`), not an error -- a caller that really wants both
still gets it.

Engine module: stdlib + the engine's own imports only, no pandas, no Qt.
"""
from __future__ import annotations

import logging

from nagasim.core import calendar as cal
from nagasim.core.block_registry import holding_types, queueing_types, service_types
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.utils import is_finite_number, timeout_at
import simpy

from nagasim.core import blocking, tending
from nagasim.core.seizing import acquire, preempt_release, release
from nagasim.core.sim_classes import Entity, spawn_entity

log = logging.getLogger(__name__)

__all__ = ["SECTION", "HOLDING_TYPES", "SERVICE_TYPES", "has_initial_state", "clock_of", "put_back",
           "apply", "default_warmup", "schedule_offset"]


SECTION = "initial_state"
# Every key the section may have (#504): the JSON Schema's properties (`tended` among them,
# #509). A test keeps this and the schema in step.
KEYS = frozenset({"time", "time_unit", "clock", "schedule_offset", "arrivals", "orders",
                  "order_book", "variables", "levels", "resources", "used", "tended",
                  "resource_sets", "blocks", "fleets", "entities"})

# Block types that can hold an entity handed to them out of nowhere. Everything
# else either creates entities (CREATE), ends them (DISPOSE), routes them
# through instantly (DECIDE/ASSIGN/RECORD -- injecting there is the same as
# injecting into the block after it), needs the entity to already hold a
# resource (RELEASE), or never runs at all (the submodel editor types, removed
# by flattening: name the flattened inner block, "<instance>/<inner>").
# ARCH-REVIEW E2: derived from each block's own SPEC.holding/.service flag
# (nagasim.core.block_registry) instead of a hand-kept tuple here.
HOLDING_TYPES = holding_types()
# ... of those, the ones where `in_service`/`remaining` mean anything.
SERVICE_TYPES = service_types()
# ... and the ones that queue on a resource, where `since` backdates the wait.
_QUEUEING = frozenset(queueing_types()) | {"HOLD", "BATCH", "MATCH"}   # ... or in a store


def has_initial_state(layout: dict) -> bool:
    """True when the model declares anything a warm start would act on."""
    state = layout.get(SECTION) or {}
    return bool(state.get("entities") or state.get("variables") or state.get("resources")
                or state.get("levels") or state.get("clock") or state.get("arrivals")
                or state.get("fleets"))


def schedule_offset(layout: dict) -> float:
    """How far into its cycle the calendar already is at model time 0 (item 7
    of the module docstring); 0 when the model does not say."""
    value = (layout.get(SECTION) or {}).get("schedule_offset")
    return float(value) if is_finite_number(value) else 0.0


def clock_of(layout: dict) -> float:
    """Where the run's clock starts (D232): `initial_state.clock`, else 0."""
    state = layout.get(SECTION)
    value = state.get("clock") if isinstance(state, dict) else None
    return float(value) if is_finite_number(value) and value > 0 else 0.0


def default_warmup(layout: dict, warmup):
    """The effective warm-up: 0 for a warm-started model when the caller passed
    none, otherwise the model's own setting (see the module docstring)."""
    if warmup is not None:
        return warmup
    if has_initial_state(layout):
        return clock_of(layout)     # the window starts where the state was taken (D232)
    return (layout.get("simulation") or {}).get("warmup") or 0.0


# ---------------------------------------------------------------- injection
def _target_name(block) -> str:
    """The resource or set a PROCESS/SEIZE block seizes."""
    name = getattr(block, "resource_name", "")
    if name:
        return name
    resource = getattr(block, "resource", None)
    return getattr(resource, "name", "")


def _in_service_process(block, entity, remaining, end=None, since=None, member=None,
                        cycle=None, gate=None):
    """The block's own body with a FIXED service time: seize (if it seizes),
    hold *remaining*, release (if it releases), forward. Kept here rather than
    threaded through PROCESS/SEIZE/DELAY as an extra parameter, so the warm
    start adds nothing to the engine's per-entity hot path.

    A warm-started job can be preempted like any other (PHASE4 A4), so it
    carries the same resume loop PROCESS does -- without it the interrupt would
    kill this process and lose the entity.
    """
    seizes = getattr(block, "seizes", False) or block.block.block_type == "SEIZE"
    if seizes:
        yield from _seize(block, entity, since, member)
    block.ctx.settling -= 1         # it holds its unit: the controllers may act (#346)
    if gate is not None:            # a cobot still places it; then its job starts (D447)
        yield gate
    block.active_entity = entity
    if cycle is not None:           # a MOTION job: its own preemption loop
        # mid-cycle (D444), or with nothing said a whole cycle drawn afresh (#461)
        yield from block.resume(entity, cycle) if cycle else block._serve(entity)
    while cycle is None:
        started = block.env.now
        try:
            if end is not None:         # the very instant it was due (D232)
                entity._ends, remaining = end, max(end - started, 0.0)
                yield timeout_at(block.env, end)
            else:
                entity._ends = started + remaining
                yield block.env.timeout(remaining)
            break
        except simpy.Interrupt:
            entity._ends = end = None
            remaining = max(remaining - (block.env.now - started), 0.0)
            preempt_release(block, entity, _target_name(block))
            yield from acquire(block, entity, _target_name(block), getattr(block, "quantity", 1),
                               getattr(block, "priority", 0), resumed=True)
    block.remove_active_entity(entity)
    if getattr(block, "releases", False):
        if block.block_after_service and not block.hold_until_space:
            # #416: the station keeps the part and its unit until the part is accepted (D78)
            yield from blocking.hold_until_accepted(block, entity, 0)
            release(block, entity, _target_name(block))
            return
        release(block, entity, _target_name(block))
    block.forward(entity, 0)


def _seize(block, entity, since, member):
    """Generator: *block* seizes for *entity* as it had before the state was taken."""
    cost, auto = entity.cost, entity._auto_cost
    yield from acquire(block, entity, _target_name(block),
                       getattr(block, "quantity", 1), getattr(block, "priority", 0),
                       member=member, held_since=since)
    # #362, #468: `held_since` makes the requests hold their unit as of *since* when they
    # are made. Seized again at the clock, every warm-started job had one request time, and
    # a preempting request picked its victim among equals instead of the one that came
    # last, or a queued one put back in this instant preempted it. (The time it seized
    # stands in for the time it asked: on a FIFO resource the two order alike.)
    # #417: this use was charged before the state, where the uninterrupted run's warm-up
    # clips it; seized again at the clock it would be charged inside the window.
    entity.cost, entity._auto_cost = cost, auto
    if since is not None:       # busy cost runs from when it really seized
        entity.req_params[_target_name(block)]["seized_at"] = since


def _hold(block, entity, holds: dict) -> None:
    """D432: *entity* holds what it seized in an earlier block, taken for it at once, before
    anything of the state asks for a unit (validation keeps them within capacity). Not
    `acquire`: that would take the entity's own wait (`_since`) and accept a part a belt or a
    station still holds."""
    manager, now = block.ctx.resource_manager, block.env.now
    for name, spec in holds.items():
        target, since = manager.resolve(name), float(spec.get("since", now))
        units = int(spec.get("units", 1))
        rset = target if spec.get("member") is not None else None
        member = manager.resolve(spec["member"]) if rset is not None else target
        requests = [member.request(counted=rset is None, since=since) for _ in range(units)]
        if rset is not None:
            rset.granted(rset.enqueue(since), units)
        for request in requests:        # as `_seize`: it holds its units as of then (#362)
            request.time = since
            request.key = (request.priority, since, not request.preempt)
        if not hasattr(entity, "req_params"):
            entity.req_params = {}
        entity.req_params[name] = {"resource": member, "requests": requests, "set": rset,
                                   "seized_at": since}
        block.emit("entity_seized", entity=entity, resource=member.name)
        # The grants are counted when SimPy processes them: before the statistics restart.
        block.ctx.settling += 1
        block.env.process(_granted(block.ctx, requests))


def _absorb(block, batch, members: list) -> None:
    """D437: the members of a formed batch, made as they were and absorbed into *batch* as
    `make_batch_entity` absorbs them (out of WIP, `in_batch` set). No BATCH runs, and no
    listener hears of a member: it was absorbed before the clock."""
    ctx = block.ctx
    for spec in members:
        member = _make_entity(ctx, spec)
        if spec.get("holds"):
            _hold(block, member, spec["holds"])
        member.in_batch = batch.id
        ctx.live_entities.remove(member)
        batch.batch_elems.append(member)


def _granted(ctx, requests):
    yield ctx.env.all_of(requests)
    ctx.settling -= 1


def _blocked_process(block, entity, since, member):
    """A finished part its station still holds (D381): the station seizes with the jobs in
    service, then keeps its unit until the part is accepted where it was put back. The
    part's own wait (`_since`) is not this seize's: it is kept aside meanwhile, and the
    waiting entities are put back only once every such station holds its unit."""
    waited, entity._since = entity._since, None
    yield from _seize(block, entity, since, member)
    entity._since = waited
    block.ctx.settling -= 1
    yield from blocking.hold_until_accepted(block, entity, forward=False)
    release(block, entity, _target_name(block))


def _arrive(block, entity, at: float):
    """An entity that was on a link: it enters *block* at the time it was due."""
    entity._arrives = (block.name, at)      # for a state taken while it is on its way
    yield timeout_at(block.env, at)
    entity._arrives = None
    block.emit("entity_created", entity=entity)     # in the block from now, not before (#338)
    block.self_run(entity)


def _make_entity(ctx, entry: dict) -> Entity:
    fields = SECTION_FIELDS["initial_state.entities"]
    age = scaled(float(entry.get("age", 0) or 0), field_factor(param(fields, "age"), entry,
                                                              ctx.units, fields))
    entity = spawn_entity(ctx, entry.get("e_type"), entry.get("attributes"), ctx.clock - age)
    entity.initial = True
    # The part of `attributes.cost` the engine itself charged before the state (busy,
    # usage and holding cost): without it a report would count it as model-assigned.
    if is_finite_number(entry.get("auto_cost")):
        entity._auto_cost = float(entry["auto_cost"])
    if is_finite_number(entry.get("holding_cost")):
        entity._holding_cost = float(entry["holding_cost"])
    # An in-progress job of a declared order (PHASE4 A3): `order_id`,
    # `due_date`, `sequence` and `step` are ordinary attributes and need
    # nothing special, but the order's own release counter does -- without it
    # the order would report zero entities created and stay "incomplete" for
    # ever, however many of its jobs the snapshot put on the floor.
    order = ctx.orders.get(getattr(entity, "order_id", None)) if ctx.orders else None
    if order is not None:
        order.created += 1
    return entity


def _down_until(resource, until: float):
    """Start *resource* failed until *until*, the same way a real failure does:
    blocker requests take the units that are free right now (a job already in
    service finishes), and they are released at *until*."""
    env = resource.env
    if env.ctx.settling:        # #346: a job in service seizes first, and so finishes
        yield env.timeout(0)
    resource.down.set(1.0, env.now)
    resource.emit_state("down", 1)
    resource._notify_bottleneck_down(True, env.now)
    blockers = resource.arbiter.take_down()
    yield env.all_of(blockers)
    yield env.timeout(max(float(until) - env.now, 0.0))
    resource.arbiter.repair(blockers)
    resource.down.set(0.0, env.now)
    resource.emit_state("up", 0)
    resource._notify_bottleneck_down(False, env.now)


def put_back(env, waiting: list) -> None:
    """The waiting entities join where they waited, in the order of the state: each
    arrives at its block, a load asks its fleet for a vehicle again."""
    for item in waiting:
        if not isinstance(item, tuple):     # at a belt's end stop (D429), a transfer (D439)
            env.process(item)
            continue
        block, entity, *trip = item
        if trip:
            env.process(block.resume(entity, trip[0], None, trip[1]))
        else:
            block.self_run(entity)


def apply(env, model, state: dict) -> int:
    """Inject *state* into a prepared model at t=0; returns the entity count.

    Called by `sim_runner.run_single_simulation` after `prepare_model` and
    before the CREATE blocks start, so every injected entity is already in
    place when the first arrival happens.
    """
    if not state:
        return 0
    ctx = env.ctx

    for name, value in (state.get("variables") or {}).items():
        ctx.variables[name] = value         # validation refused an undeclared name
        ctx.wrote_variable(name)

    system = ctx.level_system
    for name, value in (state.get("levels") or {}).items():
        level = None if system is None else system.levels.get(name)
        if level is not None:
            if level.conveyor is not None:      # C2: validation already refuses this
                raise ValueError(f"'{name}' is a conveyor; initial_state.levels cannot set it")
            level.level = float(value)
            level.reset_stats(env.now)          # the statistics start from the injected value
            system._before[level] = level.level

    if "orders" in state or "order_book" in state:      # D485: released by identity
        ctx.released_orders = {name for name, spec in (state.get("orders") or {}).items()
                               if (spec or {}).get("created", 0) > 0}

    for name, spec in (state.get("arrivals") or {}).items():
        block = model.blocks.get(name)
        if block is not None:
            block.counter_arrivals = int(spec.get("count", 0) or 0)
            if spec.get("next") is not None:
                # CREATE's own switch-on time: it waits for it, then arrives without a draw.
                block.start_time = block.next_at = float(spec["next"])
            if spec.get("scheduled") is not None:     # orders the starts (#467)
                block.scheduled_at = float(spec["scheduled"])
            block.rank = spec.get("rank")             # and breaks a tie on it (#472)

    for name, spec in (state.get("resources") or {}).items():
        resource = ctx.resource_manager.get(name)
        if resource is not None and (spec or {}).get("failures"):
            resource.failure_start = dict(spec["failures"])   # each controller's first cycle
        # PHASE4 T10: an absolute time may be written as a real date.
        until = cal.to_model_time((spec or {}).get("down_until"), ctx.calendar,
                                  f"initial_state.resources.{name}.down_until")
        if resource is not None and until is not None and float(until) > env.now:
            resource.has_controller = True     # it earns a summary row even if nothing seizes it
            env.process(_down_until(resource, float(until)))

    for name in state.get("used") or ():       # it keeps its report row (#446)
        found = ctx.resource_manager.all().get(name) or ctx.store_manager.get(name)
        if found is not None:
            found.log_events = max(found.log_events, 1)
    for station, fleet in state.get("tended") or ():    # and a tending row (D448)
        ctx.tending.setdefault((station, fleet), tending.TendingStats())

    for name, spec in (state.get("resource_sets") or {}).items():
        rset = ctx.resource_manager.sets.get(name)
        if rset is not None and (spec or {}).get("cursor") is not None:
            rset._cursor = int(spec["cursor"])     # a Cyclical set's turn (D262)

    for name, spec in (state.get("blocks") or {}).items():
        block = model.blocks.get(name)
        if block is None:
            continue
        if spec.get("setup") is not None:
            block._previous_key = spec["setup"]     # what the station is set up for (D263)
        if spec.get("due") is not None:
            block.resume_timer(float(spec["due"]))  # before its parts arrive again (D433)
        if spec.get("next_entry") is not None:
            block._next_entry = float(spec["next_entry"])   # the belt's next slot (D430)

    fleets = state.get("fleets") or {}
    for name, spec in fleets.items():           # before any load: where each vehicle is
        fleet = ctx.fleets.get(name)
        if fleet is not None:
            fleet.restore(spec, {e["trip"].get("unit") for e in state.get("entities") or []
                                 if e.get("trip") and e.get("block") in model.blocks
                                 and model.blocks[e["block"]].fleet_name == name})

    injected, waiting, blocked = 0, [], False
    belts: dict = {}                # conveyor -> the parts on it (D429, D430)
    for entry in state.get("entities") or []:
        block = model.blocks.get(entry.get("block"))
        if block is None:
            continue                            # validation already reported it
        count = max(int(entry.get("count", 0) or 0), 0)
        in_service = max(int(entry.get("in_service", 0) or 0), 0)
        remaining = list(entry.get("remaining") or [])
        ends = list(entry.get("ends") or [])
        for index in range(count):
            entity = _make_entity(ctx, entry)
            injected += 1
            if entry.get("members"):
                _absorb(block, entity, entry["members"])
            if entry.get("holds"):
                _hold(block, entity, entry["holds"])
            # #338: it is in the model from t = 0, so a listener hears of it as of any
            # other entity. Without this a trace showed its block empty, and a work in
            # progress that went negative when it left.
            if entry.get("arrives") is not None:
                env.process(_arrive(block, entity, float(entry["arrives"])))
                continue
            trip = entry.get("trip")
            if trip is not None:        # a load: its TRANSPORT carries on at its stage (D385)
                block.emit("entity_created", entity=entity)
                holder = model.blocks.get(entry.get("held_by"))
                if holder is not None:      # its machine holds it until the cobot picks (D446)
                    ctx.settling += 1
                    blocked = True
                    env.process(_blocked_process(holder, entity, entry.get("held_since"),
                                                 entry.get("member")))
                record, gate = None, None
                if trip.get("unit") is not None:
                    ctx.settling += 1
                    record = fleets[block.fleet_name]["vehicles"][trip["unit"]]
                    if trip.get("stage") == "unload" and trip.get("cycle") is not None:
                        # a cobot places it: its machine holds it and starts on it after
                        # (D447), seized when the place began (the machine let it in then)
                        gate = env.event()
                        ctx.settling += 1
                        env.process(_in_service_process(
                            tending.machine_of(model.blocks[trip["target"]]), entity, 0.0,
                            since=float(record["until"]) - float(trip["cycle"]), cycle={},
                            gate=gate))
                    env.process(block.resume(entity, trip, record, gate=gate))
                else:
                    waiting.append((block, entity, trip, entry.get("since")))
                continue
            waits = (index >= in_service and entry.get("since") is not None
                     and (block.block.block_type in _QUEUEING     # or at a belt's end stop
                          or (entry.get("belt") or {}).get("stage") == "stop"
                          or entry.get("transfer")))              # or for its level (D439)
            if waits:       # before the listeners hear of it: a state reads it back (#348)
                entity._since = float(entry["since"])
            if index >= in_service and entry.get("deadline") is not None:
                entity._renege_at = float(entry["deadline"])     # read once, by its seize
            block.emit("entity_created", entity=entity)
            if index < in_service:
                ctx.settling += 1
                since, member = entry.get("since"), entry.get("member")
                if block.block.block_type == "MOTION":     # its cycle (D444, #461)
                    env.process(_in_service_process(block, entity, 0.0, None, since, member,
                                                    entry.get("cycle") or {}))
                    continue
                if index < len(ends):
                    env.process(_in_service_process(block, entity, 0.0, float(ends[index]),
                                                    since, member))
                    continue
                hold = (remaining[index] if index < len(remaining)
                        else block.sample_duration(block.delay, entity))
                env.process(_in_service_process(block, entity, float(hold), None, since,
                                                member))
            else:
                holder = model.blocks.get(entry.get("held_by"))
                belt = entry.get("belt") or {}
                if holder is not None and holder.block.block_type == "BUFFER":
                    holder.hold(entity)             # it has a place there (D381)
                elif holder is not None and holder.block.block_type == "CONVEYOR":
                    belts.setdefault(holder, []).append(        # its end hands it over (D430)
                        (entity, {"stage": "handover", "entered": entry.get("held_since")}))
                elif holder is not None:
                    ctx.settling += 1
                    blocked = True
                    env.process(_blocked_process(holder, entity, entry.get("held_since"),
                                                 entry.get("member")))
                if belt.get("stage", "cell") != "cell":     # on the belt, past its cell (D429)
                    belts.setdefault(block, []).append((entity, belt))
                    continue
                if entry.get("transfer"):       # joins its level's queue as it was (D439)
                    waiting.append(block.resume(entity, float(entry["transfer"]["amount"])))
                    continue
                if block.block.block_type != "CONVEYOR" and block.space is not None \
                        and block.space.capacity and entry.get("place") is not False:
                    block.space.occupy(entity)      # the place it had, before any other (D382)
                waiting.append((block, entity))
    # Every cell is taken before a part waiting for one asks; the part at an end stop joins
    # that queue with the other waiting ones.
    for belt in sorted(belts, key=lambda b: b.name):
        for process, queues in belt.restore(belts[belt]):
            if queues:
                waiting.append(process)
            else:
                env.process(process)
    # #350: with jobs in service the controllers wait for them to seize, so an entity
    # put back now would find a unit free that is down or off shift. `setup_run` puts
    # them back once the controllers have acted. With none in service the controllers
    # act first anyway, and with no controller at all nothing can be taken: the old order
    # stands, so a warm start that needs none of this runs event for event as before.
    # #463: a carried order's counters are the state's, which count its jobs on the floor
    # too: set after them, so `_make_entity`'s count for a job of an order is replaced.
    for name, spec in (state.get("orders") or {}).items():
        order = ctx.orders.get(name)
        if order is not None:
            order.created, order.completed = int(spec["created"]), int(spec["completed"])
            done_at = spec.get("completed_at")
            order.last_completion = None if done_at is None else float(done_at)
    if ctx.settling and (blocked or any(r.has_controller
                                        for r in ctx.resource_manager.resources.values())):
        ctx.put_back = waiting
    else:
        put_back(env, waiting)
    log.info(f"Initial state: injected {injected} entities")
    return injected
