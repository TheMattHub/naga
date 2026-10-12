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
from nagasim.core.problems import Problem, problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.utils import is_number as _number, timeout_at
import simpy

from nagasim.core.seizing import acquire, preempt_release, release
from nagasim.core.sim_classes import Entity, spawn_entity

log = logging.getLogger(__name__)

__all__ = ["SECTION", "HOLDING_TYPES", "SERVICE_TYPES", "has_initial_state", "clock_of",
           "validate_initial_state_structured", "apply", "default_warmup", "schedule_offset"]

SECTION = "initial_state"

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
                or state.get("levels") or state.get("clock") or state.get("arrivals"))


def schedule_offset(layout: dict) -> float:
    """How far into its cycle the calendar already is at model time 0 (item 7
    of the module docstring); 0 when the model does not say."""
    value = (layout.get(SECTION) or {}).get("schedule_offset")
    return float(value) if _number(value) else 0.0


def clock_of(layout: dict) -> float:
    """Where the run's clock starts (D232): `initial_state.clock`, else 0."""
    state = layout.get(SECTION)
    value = state.get("clock") if isinstance(state, dict) else None
    return float(value) if _number(value) and value > 0 else 0.0


def default_warmup(layout: dict, warmup):
    """The effective warm-up: 0 for a warm-started model when the caller passed
    none, otherwise the model's own setting (see the module docstring)."""
    if warmup is not None:
        return warmup
    if has_initial_state(layout):
        return clock_of(layout)     # the window starts where the state was taken (D232)
    return (layout.get("simulation") or {}).get("warmup") or 0.0


# ---------------------------------------------------------------- validation
def _capacity_of(layout: dict, block: dict) -> int | None:
    """The declared capacity of the resource a block seizes, or None when it
    seizes nothing or names a resource set (a set's capacity is the sum of
    whatever its members happen to have free, so there is nothing exact to
    check up front)."""
    name = (block.get("params") or {}).get("resource_name")
    if not name:
        return None
    for entry in layout.get("resources", []):
        if entry.get("name") == name:
            return int(entry.get("capacity", 1))
    return None


def _calendar(layout: dict, problems: list):
    """The model's calendar, or None -- a malformed one is already reported by
    `validate_calendar_structured`, so it is swallowed here."""
    try:
        return cal.model_calendar(layout)
    except cal.CalendarError:
        return None


def validate_initial_state_structured(layout: dict) -> list[Problem]:
    """Every problem the `"initial_state"` section can have, on a FLATTENED
    layout (so a block inside a submodel is addressed by its expanded
    `<instance>/<inner>` name, which is what a run actually has)."""
    problems: list[Problem] = []
    state = layout.get(SECTION) or {}
    if not state:
        return problems
    if not isinstance(state, dict):
        return [problem("E_INITIAL_BLOCK", "initial_state: must be an object", f"/{SECTION}",
                        hint="use {} for no initial state")]

    blocks = {b.get("name"): b for b in layout.get("blocks", [])}
    declared_vars = {v.get("name") for v in layout.get("variables", [])}
    # A level threshold declares the variable it writes; a state taken from a run holds it.
    declared_vars |= {t.get("variable") for l in layout.get("levels", []) or []
                      if isinstance(l, dict) for t in l.get("thresholds") or []
                      if isinstance(t, dict) and t.get("variable")}
    declared_res = {r.get("name") for r in layout.get("resources", [])}
    declared_levels = {l.get("name") for l in layout.get("levels", []) or []}
    conveyor_levels = {l.get("name") for l in layout.get("levels", []) or []
                       if isinstance(l, dict) and l.get("conveyor")}

    # Levels (PHASE4 D1): a warm start may set where each continuous quantity
    # begins, exactly as it sets a variable.
    for name, value in (state.get("levels") or {}).items():
        if name in conveyor_levels:
            # C2: a scalar overwrite of `level.level` is silently undone at the
            # conveyor's next grid step (its value is the sum of its cohorts).
            problems.append(problem(
                "E_CONVEYOR_WRITE", f"initial_state: '{name}' is a conveyor; its value cannot "
                f"be set from initial_state.levels", f"/{SECTION}/levels/{name}",
                hint="set the conveyor's own \"initial\" in \"levels\" instead"))
        elif name not in declared_levels:
            problems.append(problem(
                "E_LEVEL_UNKNOWN", f"initial_state: unknown level '{name}'",
                f"/{SECTION}/levels/{name}",
                hint=f"declare it under \"levels\" first; declared: "
                     f"{', '.join(sorted(str(v) for v in declared_levels)) or '(none)'}"))
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(problem(
                "E_LEVEL_BOUNDS", f"initial_state: level '{name}' must start at a number",
                f"/{SECTION}/levels/{name}", hint="a level's value is a number"))

    clock = state.get("clock")
    if clock is not None:
        try:
            limit = cal.run_horizon(layout) or 0
        except cal.CalendarError:       # reported by the calendar's own validation
            limit = 0
        if not _number(clock) or clock < 0 or (limit > 0 and clock >= limit):
            problems.append(problem(
                "E_INITIAL_CLOCK", "initial_state: clock must be a number >= 0 and before "
                "the end of the run", f"/{SECTION}/clock",
                hint="the model time the state was taken at, in the model's base time unit"))

    offset = state.get("schedule_offset")
    if offset is not None and (not _number(offset) or offset < 0):
        problems.append(problem(
            "E_INITIAL_SCHEDULE_OFFSET",
            "initial_state: schedule_offset must be a number >= 0",
            f"/{SECTION}/schedule_offset",
            hint="how far into its own cycle the calendar already is at model time 0, "
                 "in the model's base time unit"))

    for name in (state.get("variables") or {}):
        if name not in declared_vars:
            problems.append(problem(
                "E_INITIAL_VARIABLE", f"initial_state: unknown variable '{name}'",
                f"/{SECTION}/variables/{name}",
                hint=f"declare it under \"variables\" first; declared: "
                     f"{', '.join(sorted(str(v) for v in declared_vars)) or '(none)'}"))

    start = clock if _number(clock) and clock > 0 else 0.0
    arrivals = state.get("arrivals") or {}
    for name, spec in (arrivals.items() if isinstance(arrivals, dict) else [("", None)]):
        block = blocks.get(name)
        count = spec.get("count", 0) if isinstance(spec, dict) else None
        due = spec.get("next") if isinstance(spec, dict) else None
        if block is None or block.get("type", "").upper() != "CREATE" \
                or not isinstance(count, int) or isinstance(count, bool) or count < 0 \
                or (due is not None and (not _number(due) or due < start)):
            problems.append(problem(
                "E_INITIAL_ARRIVALS", f"initial_state: arrivals of '{name}' must be a count >= 0 "
                f"and, optionally, a next time at or after the clock, for a CREATE block",
                f"/{SECTION}/arrivals/{name}",
                hint="count is the arrivals already made; next is the absolute time of the "
                     "next one"))

    failing: dict = {}
    for entry in layout.get("failures", []) or []:
        for res in entry.get("resources", []) or []:
            failing.setdefault(res, set()).add(entry.get("name", "failure"))
    for name, spec in (state.get("resources") or {}).items():
        for fname, fstate in ((spec or {}).get("failures") or {}).items():
            ok = fname in failing.get(name, ()) and isinstance(fstate, dict) and (
                (set(fstate) == {"next"} and _number(fstate["next"]) and fstate["next"] >= start)
                or (set(fstate) == {"down_until"} and (fstate["down_until"] is None or (
                    _number(fstate["down_until"]) and fstate["down_until"] >= start))))
            if not ok:
                problems.append(problem(
                    "E_INITIAL_FAILURE", f"initial_state: resource '{name}': failure '{fname}' "
                    f"must be one of its failures, with a next or a down_until time",
                    f"/{SECTION}/resources/{name}/failures/{fname}",
                    hint=f"failures of '{name}': "
                         f"{', '.join(sorted(failing.get(name, ()))) or '(none)'}; times are "
                         f"absolute and not before the clock"))

    calendar = _calendar(layout, problems)
    for name, spec in (state.get("resources") or {}).items():
        until = (spec or {}).get("down_until")
        if isinstance(until, str):
            try:
                cal.to_model_time(until, calendar, "down_until")
            except cal.CalendarError as exc:
                problems.append(problem(
                    "E_CALENDAR_DATE", f"initial_state: resource '{name}': {exc}",
                    f"/{SECTION}/resources/{name}/down_until",
                    hint="write it as 2026-01-05T06:00:00, or use a number in the base unit"))
        if name not in declared_res:
            problems.append(problem(
                "E_INITIAL_RESOURCE", f"initial_state: unknown resource '{name}'",
                f"/{SECTION}/resources/{name}",
                hint=f"declare it under \"resources\" first; declared: "
                     f"{', '.join(sorted(str(r) for r in declared_res)) or '(none)'}"))

    sets = {s.get("name"): list(s.get("members") or []) for s in layout.get("resource_sets", [])}
    for name, spec in (state.get("resource_sets") or {}).items():
        cursor = spec.get("cursor") if isinstance(spec, dict) else None
        if name not in sets or not isinstance(cursor, int) or isinstance(cursor, bool) \
                or not -1 <= cursor < len(sets[name]):
            problems.append(problem(
                "E_INITIAL_SET", f"initial_state: resource set '{name}' must be a declared "
                f"set with a cursor from -1 to its last member's index",
                f"/{SECTION}/resource_sets/{name}",
                hint="cursor is the index of the member a Cyclical set picked last"))
    for name, spec in (state.get("blocks") or {}).items():
        params = (blocks.get(name) or {}).get("params") or {}
        setup = spec.get("setup") if isinstance(spec, dict) else None
        if not (params.get("setup") or params.get("setup_cost"))                 or not isinstance(setup, (str, int, float)) or isinstance(setup, bool):
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: blocks.{name}.setup must be a text or a "
                f"number, for a block of this model that has a changeover",
                f"/{SECTION}/blocks/{name}",
                hint="the setup key the station's last job left, e.g. its entity type"))
    serving: dict = {}      # resource -> units the in-service entities hold
    waiting: dict = {}      # block -> entities queued in it
    flagged = set()         # resources an entry of its own already exceeds
    for index, entry in enumerate(state.get("entities") or []):
        at = f"/{SECTION}/entities/{index}"
        name = entry.get("block", "")
        block = blocks.get(name)
        if block is None:
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: unknown block '{name}'", f"{at}/block",
                hint="name a block of this model; a block inside a submodel is named "
                     "\"<instance>/<inner>\" once flattened"))
            continue
        btype = block.get("type", "").upper()
        # An entity on a link ARRIVES like any other: only one put inside needs a holder.
        if btype not in HOLDING_TYPES and (entry.get("arrives") is None or btype == "CREATE"):
            problems.append(problem(
                "E_INITIAL_BLOCK_TYPE",
                f"initial_state: block '{name}' is a {btype}, which cannot hold entities",
                f"{at}/block", block=name,
                hint=f"inject into one of: {', '.join(HOLDING_TYPES)}"))
            continue

        count = entry.get("count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            problems.append(problem(
                "E_INITIAL_COUNT", f"initial_state: block '{name}': count must be an integer >= 0",
                f"{at}/count", block=name, hint="use 0 to inject nothing"))
            count = 0

        age = entry.get("age", 0)
        if not _number(age) or age < 0:
            problems.append(problem(
                "E_INITIAL_AGE", f"initial_state: block '{name}': age must be a number >= 0",
                f"{at}/age", block=name,
                hint="age is how long the entity has already been in the system, in base time units"))

        in_service = entry.get("in_service", 0)
        if not isinstance(in_service, int) or isinstance(in_service, bool) or in_service < 0:
            problems.append(problem(
                "E_INITIAL_IN_SERVICE",
                f"initial_state: block '{name}': in_service must be an integer >= 0",
                f"{at}/in_service", block=name, hint="use 0 when every entity is waiting"))
            in_service = 0
        elif in_service and btype not in SERVICE_TYPES:
            problems.append(problem(
                "E_INITIAL_IN_SERVICE",
                f"initial_state: block '{name}': a {btype} has no service to be in the middle of",
                f"{at}/in_service", block=name,
                hint=f"in_service applies to: {', '.join(SERVICE_TYPES)}"))
        elif in_service > count:
            problems.append(problem(
                "E_INITIAL_IN_SERVICE",
                f"initial_state: block '{name}': in_service {in_service} exceeds count {count}",
                f"{at}/in_service", block=name, hint="in_service counts entities of this entry"))
        else:
            capacity = _capacity_of(layout, block)
            quantity = int((block.get("params") or {}).get("quantity", 1) or 1)
            resource = (block.get("params") or {}).get("resource_name")
            if entry.get("member") is not None:     # of a set: the member holds the units
                resource = entry["member"]
            serving[resource] = serving.get(resource, 0) + in_service * quantity
            if entry.get("arrives") is None:
                waiting[name] = waiting.get(name, 0) + count - in_service
            if capacity is not None and in_service * quantity > capacity:
                flagged.add(resource)
                problems.append(problem(
                    "E_INITIAL_IN_SERVICE",
                    f"initial_state: block '{name}': in_service {in_service} needs "
                    f"{in_service * quantity} units, above the resource's capacity {capacity}",
                    f"{at}/in_service", block=name,
                    hint="lower in_service, or raise the resource's capacity"))

        member = entry.get("member")
        if member is not None and member not in sets.get(
                (block.get("params") or {}).get("resource_name"), ()):
            problems.append(problem(
                "E_INITIAL_SET", f"initial_state: block '{name}': member '{member}' is not a "
                f"member of the resource set the block seizes", f"{at}/member", block=name,
                hint="member names which resource of the set serves the jobs in service"))

        remaining = entry.get("remaining")
        if remaining is not None:
            if not isinstance(remaining, list) or not all(_number(r) and r >= 0 for r in remaining):
                problems.append(problem(
                    "E_INITIAL_REMAINING",
                    f"initial_state: block '{name}': remaining must be a list of numbers >= 0",
                    f"{at}/remaining", block=name,
                    hint="one remaining service time per in-service entity"))
            elif len(remaining) > max(in_service, 0):
                problems.append(problem(
                    "E_INITIAL_REMAINING",
                    f"initial_state: block '{name}': {len(remaining)} remaining times "
                    f"for {in_service} in-service entities",
                    f"{at}/remaining", block=name,
                    hint="remaining has at most in_service entries; the rest sample a fresh "
                         "service time"))

        # Absolute times (D232): the end of a service in progress, an arrival off a link.
        ends, arrives = entry.get("ends"), entry.get("arrives")
        if ends is not None and not (isinstance(ends, list) and len(ends) <= max(in_service, 0)
                                     and all(_number(e) and e >= start for e in ends)):
            problems.append(problem(
                "E_INITIAL_REMAINING",
                f"initial_state: block '{name}': ends must be a list of times at or after the "
                f"clock, at most one per in-service entity", f"{at}/ends", block=name,
                hint="the absolute time each service in progress ends"))
        deadline = entry.get("deadline")
        if deadline is not None and not (_number(deadline) and deadline >= start
                                         and (block.get("params") or {}).get("renege_after")):
            problems.append(problem(
                "E_INITIAL_REMAINING",
                f"initial_state: block '{name}': deadline must be a time at or after the "
                f"clock, at a block with renege_after", f"{at}/deadline", block=name,
                hint="the absolute time the entry's waiting entities give up"))
        since = entry.get("since")
        if since is not None and not (_number(since) and since <= start):
            problems.append(problem(
                "E_INITIAL_REMAINING",
                f"initial_state: block '{name}': since must be a time at or before the clock",
                f"{at}/since", block=name,
                hint="the absolute time the entry's waiting entities joined the queue"))
        if arrives is not None and not (_number(arrives) and arrives >= start and count == 1
                                        and not in_service):
            problems.append(problem(
                "E_INITIAL_REMAINING",
                f"initial_state: block '{name}': arrives must be a time at or after the clock, "
                f"on an entry of one entity that is not in service", f"{at}/arrives", block=name,
                hint="the absolute time an entity on a link reaches the block"))

    # Consistency over the whole state: a plant is where it goes wrong, and the engine does
    # not choose which observed entity to drop (D236).
    capacities = {r.get("name"): int(r.get("capacity", 1)) for r in layout.get("resources", [])}
    for resource, units in serving.items():
        if resource in capacities and resource not in flagged and units > capacities[resource]:
            problems.append(problem(
                "E_INITIAL_IN_SERVICE",
                f"initial_state: {units} units of '{resource}' are in service over all "
                f"entries, above its capacity {capacities[resource]}", f"/{SECTION}/entities",
                hint="lower in_service, or raise the resource's capacity"))
    for name, spec in (state.get("resources") or {}).items():
        busy = (spec or {}).get("busy")
        if busy is not None and busy != serving.get(name, 0):
            problems.append(problem(
                "E_INITIAL_BUSY",
                f"initial_state: resource '{name}' is reported busy with {busy} units, and the "
                f"entities in service on it hold {serving.get(name, 0)}",
                f"/{SECTION}/resources/{name}/busy",
                hint="every busy unit needs an in_service entity at a block that seizes it"))
    for name, queued in waiting.items():
        room = int((blocks[name].get("params") or {}).get("queue_capacity", 0) or 0)
        if room and queued > room:
            problems.append(problem(
                "E_INITIAL_QUEUE_CAPACITY",
                f"initial_state: {queued} entities wait at '{name}', whose queue holds {room}",
                f"/{SECTION}/entities", block=name,
                hint="the state shows more than the queue can hold; check the observation"))

    warmup = (layout.get("simulation") or {}).get("warmup") or 0
    if warmup and has_initial_state(layout):
        problems.append(problem(
            "W_INITIAL_STATE_WARMUP",
            "the model sets both an initial state and a warm-up; the warm-up discards "
            "the history the initial state supplies",
            "/simulation/warmup",
            hint="set simulation.warmup to 0 for a warm-started model"))
    return problems


# ---------------------------------------------------------------- injection
def _target_name(block) -> str:
    """The resource or set a PROCESS/SEIZE block seizes."""
    name = getattr(block, "resource_name", "")
    if name:
        return name
    resource = getattr(block, "resource", None)
    return getattr(resource, "name", "")


def _in_service_process(block, entity, remaining, end=None, since=None, member=None):
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
        yield from acquire(block, entity, _target_name(block),
                           getattr(block, "quantity", 1), getattr(block, "priority", 0),
                           member=member)
        if since is not None:       # busy cost runs from when it really seized
            held = entity.req_params[_target_name(block)]
            held["seized_at"] = since
            # #362: and it holds its unit as of then. Seized again at the clock, every
            # warm-started job had one request time, and a preempting request picked its
            # victim among equals instead of the one that came last. (The time it seized
            # stands in for the time it asked: on a FIFO resource the two order alike.)
            for request in held["requests"]:
                request.time = since
                request.key = (request.priority, since, not request.preempt)
    block.ctx.settling -= 1         # it holds its unit: the controllers may act (#346)
    block.active_entity = entity
    while True:
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
        release(block, entity, _target_name(block))
    block.forward(entity, 0)


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
    if _number(entry.get("auto_cost")):
        entity._auto_cost = float(entry["auto_cost"])
    if _number(entry.get("holding_cost")):
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

    system = ctx.level_system
    for name, value in (state.get("levels") or {}).items():
        level = None if system is None else system.levels.get(name)
        if level is not None:
            if level.conveyor is not None:      # C2: validation already refuses this
                raise ValueError(f"'{name}' is a conveyor; initial_state.levels cannot set it")
            level.level = float(value)
            level.reset_stats(env.now)          # the statistics start from the injected value
            system._before[level] = level.level

    for name, spec in (state.get("arrivals") or {}).items():
        block = model.blocks.get(name)
        if block is not None:
            block.counter_arrivals = int(spec.get("count", 0) or 0)
            if spec.get("next") is not None:
                # CREATE's own switch-on time: it waits for it, then arrives without a draw.
                block.start_time = block.next_at = float(spec["next"])

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

    for name, spec in (state.get("resource_sets") or {}).items():
        rset = ctx.resource_manager.sets.get(name)
        if rset is not None and (spec or {}).get("cursor") is not None:
            rset._cursor = int(spec["cursor"])     # a Cyclical set's turn (D262)

    for name, spec in (state.get("blocks") or {}).items():
        block = model.blocks.get(name)
        if block is not None and (spec or {}).get("setup") is not None:
            block._previous_key = spec["setup"]     # what the station is set up for (D263)

    injected, waiting = 0, []
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
            # #338: it is in the model from t = 0, so a listener hears of it as of any
            # other entity. Without this a trace showed its block empty, and a work in
            # progress that went negative when it left.
            if entry.get("arrives") is not None:
                env.process(_arrive(block, entity, float(entry["arrives"])))
                continue
            waits = (index >= in_service and entry.get("since") is not None
                     and block.block.block_type in _QUEUEING)
            if waits:       # before the listeners hear of it: a state reads it back (#348)
                entity._since = float(entry["since"])
            if index >= in_service and entry.get("deadline") is not None:
                entity._renege_at = float(entry["deadline"])     # read once, by its seize
            block.emit("entity_created", entity=entity)
            if index < in_service:
                ctx.settling += 1
                since, member = entry.get("since"), entry.get("member")
                if index < len(ends):
                    env.process(_in_service_process(block, entity, 0.0, float(ends[index]),
                                                    since, member))
                    continue
                hold = (remaining[index] if index < len(remaining)
                        else block.sample_duration(block.delay, entity))
                env.process(_in_service_process(block, entity, float(hold), None, since,
                                                member))
            else:
                waiting.append((block, entity))
    # #350: with jobs in service the controllers wait for them to seize, so an entity
    # put back now would find a unit free that is down or off shift. `setup_run` puts
    # them back once the controllers have acted. With none in service the controllers
    # act first anyway, and with no controller at all nothing can be taken: the old order
    # stands, so a warm start that needs none of this runs event for event as before.
    if ctx.settling and any(r.has_controller for r in ctx.resource_manager.resources.values()):
        ctx.put_back = waiting
    else:
        for block, entity in waiting:
            block.self_run(entity)
    log.info(f"Initial state: injected {injected} entities")
    return injected
