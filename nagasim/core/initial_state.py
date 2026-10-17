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
import math

from nagasim.core import calendar as cal
from nagasim.core.battery import battery_of
from nagasim.core.block_registry import holding_types, queueing_types, service_types
from nagasim.core.network import block_nodes, build_network
from nagasim.core.orders import book_of as orders_book_of
from nagasim.core import motion as timing
from nagasim.core.timeunits import base_factor, units_of
from nagasim.core.problems import Problem, problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.utils import is_number, timeout_at
import simpy

from nagasim.core import blocking, movers, tending
from nagasim.core.seizing import acquire, preempt_release, release
from nagasim.core.sim_classes import Entity, spawn_entity

log = logging.getLogger(__name__)

__all__ = ["SECTION", "HOLDING_TYPES", "SERVICE_TYPES", "has_initial_state", "clock_of", "put_back",
           "validate_initial_state_structured", "apply", "default_warmup", "schedule_offset"]

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


def _number(value) -> bool:
    """A finite number. A state inside a model file is as untrusted as a state document,
    which `state.check` holds to the same: no time or count is Infinity or NaN."""
    try:
        return is_number(value) and math.isfinite(value)
    except OverflowError:           # an int too large for a float, 10**400 (#479)
        return False


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
    # #504: a key this engine does not know is refused, not ignored, so a state written by a
    # newer engine never runs here as if it said less (0.8.0 ignored `order_book`).
    for key in sorted(set(state) - KEYS, key=str):
        problems.append(problem(
            "E_INITIAL_KEY", f"initial_state: unknown key '{str(key)[:40]}'",
            f"/{SECTION}/{str(key)[:40]}",
            hint=f"a newer engine may have written it; known: {', '.join(sorted(KEYS))}"))

    blocks = {b.get("name"): b for b in layout.get("blocks", [])}
    declared_vars = {v.get("name") for v in layout.get("variables", [])}
    # A level threshold declares the variable it writes; a state taken from a run holds it.
    declared_vars |= {t.get("variable") for l in layout.get("levels", []) or []
                      if isinstance(l, dict) for t in l.get("thresholds") or []
                      if isinstance(t, dict) and t.get("variable")}
    declared_res = {r.get("name") for r in layout.get("resources", [])}
    declared_res |= {f.get("name") for f in layout.get("fleets") or []    # a fleet is one
                     if isinstance(f, dict)}
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
    ties: dict = {}         # (next, scheduled) -> [(name, rank or None)]: #474, ranked per group
    for name, spec in (arrivals.items() if isinstance(arrivals, dict) else [("", None)]):
        block = blocks.get(name)
        count = spec.get("count", 0) if isinstance(spec, dict) else None
        due = spec.get("next") if isinstance(spec, dict) else None
        drawn = spec.get("scheduled") if isinstance(spec, dict) else None
        rank = spec.get("rank") if isinstance(spec, dict) else None
        ranked = isinstance(spec, dict) and "rank" in spec
        bad_rank = ranked and (drawn is None or not isinstance(rank, int)
                               or isinstance(rank, bool) or rank < 0)
        # Only a drawn gap has a next time (what `_read` writes, #478): on a Schedule or
        # Orders CREATE `next` would overload its start_time.
        timed = due is not None or drawn is not None or ranked
        mode = ((block or {}).get("params") or {}).get("arrival_mode", "Interarrival")
        if bad_rank or block is None or block.get("type", "").upper() != "CREATE" \
                or (timed and mode != "Interarrival") \
                or not isinstance(count, int) or isinstance(count, bool) or count < 0 \
                or count > 2 ** 53 \
                or (due is not None and (not _number(due) or due < start)) \
                or (drawn is not None and (due is None or not _number(drawn)
                                           or not 0 <= drawn <= start)):
            problems.append(problem(
                "E_INITIAL_ARRIVALS", f"initial_state: arrivals of '{name}' must be a count >= 0 "
                f"and, for an Interarrival CREATE, optionally a next time at or after the "
                f"clock and when it was scheduled, at or before it", f"/{SECTION}/arrivals/{name}",
                hint="count is the arrivals already made, at most 2**53 (#475: an exact float, "
                     "what a deterministic schedule's walk counts in); next is the absolute "
                     "time of the next one; scheduled is the time it was drawn at; rank, a "
                     "whole number, orders the ones drawn for one instant at one instant"))
        elif drawn is not None:
            ties.setdefault((due, drawn), []).append((name, rank))
    for tied in ties.values():
        ranks = [rank for _name, rank in tied if rank is not None]
        # A rank on all of two or more, each its own, or on none (an older document: block
        # order). Partial ranks would read as an order they do not give (an unranked one first).
        if ranks and (len(tied) < 2 or len(ranks) < len(tied) or len(set(ranks)) < len(ranks)):
            for name, _rank in tied:
                problems.append(problem(
                    "E_INITIAL_ARRIVALS", f"initial_state: arrivals of '{name}' tie with "
                    f"{len(tied) - 1} other(s) at one next and scheduled time; their ranks must "
                    f"be on all of them and different", f"/{SECTION}/arrivals/{name}/rank",
                    hint="rank orders two or more drawn for one instant at one instant: "
                         "0, 1, ... within that group, or no rank on any of them"))

    # D484: a book replaces the model's orders; its entries are judged as that section's
    # (`core/schema/layout.py`), and here only what the section has no word for.
    book = state.get("order_book")
    if book is not None and not (isinstance(book, list) and all(
            isinstance(o, dict) and isinstance(o.get("held", False), bool) for o in book)):
        problems.append(problem(
            "E_INITIAL_ORDERS", "initial_state: order_book must be a list of orders, each "
            "\"held\" true or false", f"/{SECTION}/order_book",
            hint="the orders section's shape, plus \"held\": true for an order held back"))
    ids = {str(o.get("id", "")) for o in orders_book_of(layout) if isinstance(o, dict)}
    orders = state.get("orders") or {}
    for name, spec in (orders.items() if isinstance(orders, dict) else [("", None)]):
        made, done, at = ((spec.get(k) for k in ("created", "completed", "completed_at"))
                          if isinstance(spec, dict) else (None, None, None))
        counts = all(isinstance(n, int) and not isinstance(n, bool) for n in (made, done))
        if name not in ids or not isinstance(spec, dict) \
                or not set(spec) <= {"created", "completed", "completed_at"} \
                or not counts or made < 0 or done < 0 \
                or (at is None) != (done == 0) \
                or (at is not None and not (_number(at) and 0 <= at < start)):
            problems.append(problem(
                "E_INITIAL_ORDERS", f"initial_state: order '{name}' must be a declared order "
                f"with created and completed >= 0 and, once one is completed, completed_at "
                f"before the clock", f"/{SECTION}/orders/{name}",
                hint="the counters of its release and completion before the clock; a "
                     "DUPLICATE's copies are completed for the order, never created by it"))

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
    # ponytail: #451, #455 follow links and a TRANSPORT's named destination (#459) only. A
    # ROUTE, or a TRANSPORT with none, reaches a station by an attribute, so every station
    # would refuse every load (the warehouse's pallets); a lone entity a ROUTE sends to a
    # station before a SEPARATE still raises there.
    linked = _successors(layout, blocks, jumps=False)
    # walked back, every jump counts: a batch entity can be wherever any of them lands
    before = _predecessors(blocks, _successors(layout, blocks))
    created = {str((b.get("params") or {}).get("e_type") or "").upper()
               for b in blocks.values() if _kind(b) == "CREATE"}
    needs: dict = {}        # (block, a batch entity?) -> what an entity there must hold
    batches: dict = {}      # block -> the types only a batch entity has there (D440)
    used = state.get("used")
    # #446: what had a report row before the clock; a store is named after its block
    stores = tuple(f"{b}_" for b, spec in blocks.items()
                   if str(spec.get("type", "")).upper() in ("HOLD", "BATCH", "MATCH"))
    if used is not None and not (isinstance(used, list) and all(
            isinstance(n, str) and (n in declared_res or n in sets or n.startswith(stores))
            for n in used)):
        problems.append(problem(
            "E_INITIAL_RESOURCE", "initial_state: used must list resources, sets and stores of "
            "the model", f"/{SECTION}/used",
            hint="the ones that had anything happen to them before the clock"))
    for name, spec in (state.get("resource_sets") or {}).items():
        cursor = spec.get("cursor") if isinstance(spec, dict) else None
        if name not in sets or not isinstance(cursor, int) or isinstance(cursor, bool) \
                or not -1 <= cursor < len(sets[name]):
            problems.append(problem(
                "E_INITIAL_SET", f"initial_state: resource set '{name}' must be a declared "
                f"set with a cursor from -1 to its last member's index",
                f"/{SECTION}/resource_sets/{name}",
                hint="cursor is the index of the member a Cyclical set picked last"))
    # #447: a batch's timer runs only while parts wait there
    listed = state.get("entities") if isinstance(state.get("entities"), list) else []
    holding = {e["block"] for e in listed if isinstance(e, dict) and e.get("count")
               and isinstance(e.get("block"), str)}
    for name, spec in (state.get("blocks") or {}).items():
        block = blocks.get(name) or {}
        params, kind = block.get("params") or {}, str(block.get("type", "")).upper()
        fits = {}       # what each kind of block carries: its check
        if params.get("setup") or params.get("setup_cost"):         # D263
            fits["setup"] = lambda v: isinstance(v, (str, int, float)) and not isinstance(v, bool)
        if kind == "BATCH" and _waits(params.get("max_wait")) and name in holding:     # D433
            fits["due"] = lambda v: _number(v) and v >= start
        if kind == "CONVEYOR":                                      # D430
            fits["next_entry"] = _number
        if not (isinstance(spec, dict) and spec and set(spec) <= set(fits)
                and all(fits[key](value) for key, value in spec.items())):
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: blocks.{name} must be a block of this model "
                f"with what it carries: setup (a changeover), due (a BATCH's maximum wait, with "
                f"parts waiting there) or next_entry (a CONVEYOR)", f"/{SECTION}/blocks/{name}",
                hint="setup is the key the station's last job left; due and next_entry are "
                     "absolute times, due at or after the clock"))
    places, follow, ahead = block_nodes(layout), _successors(layout, blocks, jumps=False), {}

    def transported(name) -> bool:
        """A TRANSPORT reached from *name* before any STATION: it reads `entity.station`."""
        if name not in ahead:
            ahead[name] = bool(blocking.stops(
                name, follow, lambda n: _kind(blocks.get(n) or {}) == "TRANSPORT",
                lambda n: _kind(blocks.get(n) or {}) != "STATION"))
        return ahead[name]

    serving: dict = {}      # resource -> units the in-service entities hold
    waiting: dict = {}      # block -> entities queued in it
    flagged = set()         # resources an entry of its own already exceeds
    entries = state.get("entities") or []
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        # #420: everything below, and the run, reads each entry as an object
        return problems + [problem(
            "E_INITIAL_BLOCK", "initial_state: entities must be a list of objects, each "
            "naming its block", f"/{SECTION}/entities",
            hint="one object per entry: {\"block\": \"<name>\", \"count\": 1, ...}")]
    for index, entry in enumerate(entries):
        at = f"/{SECTION}/entities/{index}"
        name = entry.get("block", "")
        block = blocks.get(name) if isinstance(name, str) else None     # #420: a list is no name
        if block is None:
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: unknown block '{str(name)[:80]}'",
                f"{at}/block",
                hint="name a block of this model; a block inside a submodel is named "
                     "\"<instance>/<inner>\" once flattened"))
            continue
        btype = block.get("type", "").upper()
        # An entity on a link ARRIVES like any other: only one put inside needs a holder.
        # A load on its trip is at its TRANSPORT (`_validate_fleets`); every other check of
        # an entry holds for it too.
        riding = (btype == "TRANSPORT" and entry.get("trip") is not None) \
            or (btype == "CONVEYOR" and entry.get("belt") is not None) \
            or (btype == "TRANSFER" and entry.get("transfer") is not None)     # D439, below
        if btype not in HOLDING_TYPES and not riding \
            and (entry.get("arrives") is None or btype == "CREATE"):
            problems.append(problem(
                "E_INITIAL_BLOCK_TYPE",
                f"initial_state: block '{name}' is a {btype}, which cannot hold entities",
                f"{at}/block", block=name,
                hint="an entity waiting at a TRANSFER for its level carries transfer.amount, "
                     "which a plant does not say" if btype == "TRANSFER"
                else f"inject into one of: {', '.join(HOLDING_TYPES)}"))
            continue

        if not _plain_entity(entry):        # #419, #452, #453: each is set on the entity
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: block '{name}': e_type must be a text and "
                f"attributes an object of named values, none the engine's own and a cost a "
                f"number", f"{at}/attributes", block=name,
                hint="for example {\"priority\": 2}; not id, e_type, creation_time, req_params "
                     "or a name starting with _"))
        elif not riding and btype != "STATION" and transported(name) \
                and str((entry.get("attributes") or {}).get("station")) not in places:
            problems.append(problem(        # else the run fails when it gets there
                "E_INITIAL_BLOCK", f"initial_state: block '{name}': a TRANSPORT ahead leaves "
                f"from the station the entity is at, which attributes.station must name",
                f"{at}/attributes", block=name,
                hint="the STATION (or its station alias) the entity last went through"))

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
            # a "Delay Release" job holds its unit from an earlier block: its `holds` (D432)
            if btype not in blocking.PROCESS_TYPES or str((block.get("params") or {}).get(
                    "action", "Seize Delay Release")).startswith("Seize"):
                serving[resource] = serving.get(resource, 0) + in_service * quantity
            if entry.get("arrives") is None and entry.get("place") is not False:
                waiting[name] = waiting.get(name, 0) + count - in_service
            if capacity is not None and in_service * quantity > capacity:
                flagged.add(resource)
                problems.append(problem(
                    "E_INITIAL_IN_SERVICE",
                    f"initial_state: block '{name}': in_service {in_service} needs "
                    f"{in_service * quantity} units, above the resource's capacity {capacity}",
                    f"{at}/in_service", block=name,
                    hint="lower in_service, or raise the resource's capacity"))

        transfer = entry.get("transfer")
        if transfer is not None and not (
                btype == "TRANSFER" and bool((block.get("params") or {}).get("wait"))
                and isinstance(transfer, dict) and set(transfer) == {"amount"}
                and _number(transfer["amount"]) and 0 < transfer["amount"] <= _room(
                    layout, (block.get("params") or {}).get("level")) and count == 1
                and not in_service and entry.get("arrives") is None):
            problems.append(problem(        # D439
                "E_INITIAL_BLOCK", f"initial_state: block '{name}': transfer is the amount one "
                f"entity waits for at a TRANSFER with wait, no more than its level can hold",
                f"{at}/transfer", block=name,
                hint="{\"amount\": 3.0}, a number > 0, as the TRANSFER evaluated it; one above "
                     "the level's capacity less its minimum would wait for ever"))

        if btype == "MOTION" and in_service:
            _validate_cycle(layout, entry, block, start, at, problems)
        elif entry.get("cycle") is not None:
            problems.append(problem(
                "E_INITIAL_CYCLE", f"initial_state: block '{name}': a cycle is the job in service "
                f"at a MOTION block", f"{at}/cycle", block=name,
                hint="one entity with in_service 1 at a MOTION"))

        member = entry.get("member")
        if member is not None and entry.get("held_by") is None and member not in sets.get(
                (block.get("params") or {}).get("resource_name"), ()):
            problems.append(problem(
                "E_INITIAL_SET", f"initial_state: block '{name}': member '{member}' is not a "
                f"member of the resource set the block seizes", f"{at}/member", block=name,
                hint="member names which resource of the set serves the jobs in service"))

        if entry.get("holds") is not None:
            _validate_holds(layout, entry, block, blocks, sets, start, serving, at, problems)
        if entry.get("members") is not None:
            _validate_members(layout, entry, blocks, sets, start, serving, at, problems,
                              linked, before)
        elif count and name not in batches:     # one walk per block; None: a SEPARATE ahead
            batches[name] = None if _splits(name, blocks, linked) else \
                _batch_types(name, blocks, before, created)
        if entry.get("members") is None and count and (
                batches[name] is None or str(entry.get("e_type") or "").upper() in batches[name]):
            problems.append(problem(
                "E_INITIAL_BATCH", f"initial_state: block '{name}': the entry meets a SEPARATE "
                f"before any BATCH or MATCH, or has the type the BATCH before it gives its "
                f"batches, so it is a formed batch and needs its members",
                f"{at}/members", block=name,
                hint="one record per member (e_type, age, attributes, holds); a plant names no "
                     "batch's members, so a state from one cannot carry a formed batch"))
        key = (name, entry.get("members") is not None)
        if count and key not in needs:      # one walk per block
            needs[key] = _needs(name, blocks, linked, batch=key[1])
        held = set(entry["holds"]) if isinstance(entry.get("holds"), dict) else set()
        for released in sorted(needs[key] - held) if count else ():     # #449, #455
            problems.append(problem(
                "E_INITIAL_RESOURCE", f"initial_state: block '{name}': the entry gives "
                f"'{released}' back at a block it reaches before any seizes it, and does not "
                f"hold it", f"{at}/holds", block=name,
                hint="a job before a block that releases a resource holds it from an earlier "
                     "block: say so in holds"))

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

    _validate_places(layout, state, blocks, sets, serving, waiting, start, problems)
    _validate_fleets(layout, state, blocks, start, problems, serving)
    _validate_belts(layout, state, blocks, start, problems)

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
        room = _places(blocks[name])
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


def _validate_holds(layout, entry, block, blocks, sets, start, serving, at, problems) -> None:
    """D432: `holds`, the resources one entity holds from an earlier block. Its units count
    into *serving*, so the capacity and `busy` checks of the caller see them."""
    holds = entry["holds"]
    resources = {r.get("name") for r in layout.get("resources", []) if isinstance(r, dict)}
    # What its own block, or the station that still holds it, seizes for it is not a hold.
    own = {_seized(block)}
    if isinstance(entry.get("held_by"), str):
        own.add(((blocks.get(entry["held_by"]) or {}).get("params") or {}).get("resource_name"))
    age = entry.get("age", 0)       # #460: not held before the entity was made
    made = start - age - 1e-9 * max(1.0, abs(start)) if _number(age) else -math.inf

    def fits(name, spec) -> bool:
        if not (isinstance(spec, dict) and set(spec) <= {"since", "units", "member"}
                and name not in own):
            return False
        member, units, since = spec.get("member"), spec.get("units", 1), spec.get("since")
        return ((name in resources if member is None else member in sets.get(name, ()))
                and isinstance(units, int) and not isinstance(units, bool) and units >= 1
                and (since is None or (_number(since) and made <= since <= start)))

    if not (isinstance(holds, dict) and holds and entry.get("count") == 1
            and all(fits(name, spec) for name, spec in holds.items())):
        problems.append(problem(
            "E_INITIAL_RESOURCE", f"initial_state: block '{entry.get('block')}': holds must name "
            f"resources of the model the entity holds from an earlier block, each with an "
            f"optional since, units and, of a set, its member", f"{at}/holds",
            hint="one entity per entry; since is at or before the clock and not before the "
                 "entity was made (the clock less its age); not the resource its own block or "
                 "the station in held_by seizes"))
        return
    for name, spec in holds.items():
        unit = spec.get("member") or name       # #448: two holds of one unit count twice
        serving[unit] = serving.get(unit, 0) + spec.get("units", 1)

    # #449: each hold is given back further on, by its own block or one the entity reaches
    after = _successors(layout, blocks)
    for name in holds:
        if _released(block) != name and not blocking.stops(
                entry["block"], after, lambda n: _released(blocks.get(n) or {}) == name,
                lambda n: True):
            problems.append(problem(
                "E_INITIAL_RESOURCE", f"initial_state: block '{entry['block']}': the entry "
                f"holds '{name}', which no block after it releases", f"{at}/holds/{name}",
                hint="a hold is given back by a RELEASE or a \"Delay Release\" further on"))


def _successors(layout, blocks, jumps=True):
    """The function giving the blocks an entity leaving a block reaches next: its links, the
    STATION a TRANSPORT names in its `destination` (#459) and, with *jumps*, every STATION
    from a ROUTE or a TRANSPORT with none, which reach theirs by an attribute."""
    links: dict = {}
    for link in layout.get("links") or []:
        if isinstance(link, dict):
            links.setdefault(link.get("source"), []).append(link.get("target"))
    named = {}      # a station is found by its block name or its `station` alias
    for n, b in blocks.items():
        if _kind(b) == "STATION":
            named[n] = named[str((b.get("params") or {}).get("station") or n)] = n
    stations = sorted(set(named.values())) if jumps else []

    def after(n):
        block = blocks.get(n) or {}
        if _kind(block) not in ("ROUTE", "TRANSPORT"):
            return links.get(n, [])
        destination = named.get(str((block.get("params") or {}).get("destination") or ""))
        return links.get(n, []) + ([destination] if destination and _kind(block) == "TRANSPORT"
                                   else stations)
    return after


def _predecessors(blocks, after) -> dict:
    """{block: the blocks whose *after* reaches it}: the walk of *after*, backwards."""
    before: dict = {}
    for n in blocks:
        for m in after(n):
            before.setdefault(m, []).append(n)
    return before


def _kind(block: dict) -> str:
    return str(block.get("type", "")).upper()


# What the engine keeps on an entity under a public name, its methods and properties too: an
# attribute may not be one (#453, #457). `cost` is the one a model sets; the last three are
# set on an entity later in a run.
_OWN = frozenset((set(dir(Entity)) | set(vars(Entity(0))) | {"req_params", "initial", "agent"})
                 - {"cost"})


def _plain_entity(entry: dict) -> bool:
    """An entry's or a member's `e_type` and `attributes` can be set on an entity as given."""
    attributes = entry.get("attributes")
    if attributes is None:
        attributes = {}
    return (isinstance(entry.get("e_type", ""), (str, type(None))) and isinstance(attributes, dict)
            and all(isinstance(k, str) and not k.startswith("_") and k not in _OWN
                    for k in attributes)
            and ("cost" not in attributes or _number(attributes["cost"])))


def _forms(block: dict) -> bool:
    """A block that makes a batch entity of the entities it gathers: a BATCH, or a MATCH with
    `batch_option` (one without sends each matched entity on alone)."""
    return _kind(block) == "BATCH" or (
        _kind(block) == "MATCH" and bool((block.get("params") or {}).get("batch_option")))


def _splits(name, blocks, after) -> bool:
    """#451: an entity at *name* meets a SEPARATE before any BATCH or MATCH, so only a
    formed batch can be there."""
    def kind(n) -> str:
        return _kind(blocks.get(n) or {})

    if _forms(blocks.get(name) or {}):
        return False
    return kind(name) == "SEPARATE" or "SEPARATE" in map(kind, blocking.stops(
        name, after, lambda n: kind(n) == "SEPARATE" or _forms(blocks.get(n) or {}),
        lambda n: True))


def _formed_by(name, blocks, before) -> list:
    """#456: the BATCHes and batching MATCHes whose batch entity can be at *name*: those
    reached walking back from it before any SEPARATE."""
    return [n for n in blocking.stops(
        name, lambda n: before.get(n, []),
        lambda n: _kind(blocks.get(n) or {}) == "SEPARATE" or _forms(blocks.get(n) or {}),
        lambda n: True) if _forms(blocks.get(n) or {})]


def _batch_types(name, blocks, before, created) -> set:
    """D440 where no SEPARATE follows: the types the BATCHes before *name* give their batch
    entities, which no CREATE also makes; an entity of one there is a formed batch."""
    types = {str((blocks[b].get("params") or {}).get("batched_entity_type") or "").upper()
             for b in _formed_by(name, blocks, before)}
    return types - created - {""}


def _group(block: dict):
    """The number of entities *block* (a BATCH or MATCH) gathers into one batch, or None when
    it is no constant."""
    params = block.get("params") or {}
    size = params.get("batch_size") if _kind(block) == "BATCH" else \
        params.get("number_to_match", 2)
    return size if isinstance(size, int) and not isinstance(size, bool) else None


def _needs(name, blocks, after, *, batch=False) -> set:
    """#455: what an entity at *name* gives back, at a RELEASE or a "Delay Release" its own
    block is or that it reaches, before any block seizes it for it: what it must hold. A
    batch entity (*batch*) goes no further than a SEPARATE, where its members go on."""
    def takes(block) -> set:        # a belt seizes its end stop for the part (D431)
        return {_seized(block), _kind(block) == "CONVEYOR" and str(
            (block.get("params") or {}).get("exit_resource") or "").strip() or None}

    def stop(x):
        return lambda n: (x == _released(blocks.get(n) or {}) or x in takes(blocks.get(n) or {})
                          or (batch and _kind(blocks.get(n) or {}) == "SEPARATE"))

    own = blocks.get(name) or {}
    found = {_released(own)} - {None}
    for x in {_released(b) for b in blocks.values()} - {None} - found - takes(own):
        if any(_released(blocks.get(n) or {}) == x for n in blocking.stops(
                name, after, stop(x), lambda n: True)):
            found.add(x)
    return found


_MEMBER_KEYS = frozenset({"e_type", "age", "attributes", "holds"})


def _validate_members(layout, entry, blocks, sets, start, serving, at, problems,
                      after, before) -> None:
    """D437, D438: a formed batch's members, all in one entity's entry, at a block a BATCH or
    MATCH before it reaches with no BATCH or MATCH ahead of it (#456). Each is at least as old
    as the batch; they are no more than the largest group those BATCHes form; their holds are
    an entity's (D432), count into *serving*, and hold what the members give back once the
    batch splits (#455)."""
    members, age, name = entry["members"], entry.get("age", 0), entry["block"]
    age = age if _number(age) and age > 0 else 0.0
    formed = _formed_by(name, blocks, before)
    nested = _forms(blocks[name]) or any(_forms(blocks.get(n) or {}) for n in blocking.stops(
        name, after, lambda n: _kind(blocks.get(n) or {}) == "SEPARATE"
        or _forms(blocks.get(n) or {}), lambda n: True))
    largest = 0 if nested else max(
        [n for n in (_group(blocks[b]) for b in formed) if n], default=0)

    def fits(spec) -> bool:
        return (isinstance(spec, dict) and set(spec) <= _MEMBER_KEYS and _plain_entity(spec)
                and _number(spec.get("age", 0)) and spec.get("age", 0) >= age)

    if not (isinstance(members, list) and 1 <= len(members) <= largest and entry.get("count") == 1
            and all(fits(spec) for spec in members)):
        problems.append(problem(
            "E_INITIAL_BATCH", f"initial_state: block '{name}': members are the records of one "
            f"batch entity, at a block a BATCH or MATCH reaches with none ahead, from 1 to the "
            f"group it forms ({largest}), each with an optional e_type, age, attributes and "
            f"holds", f"{at}/members",
            hint="a member is at least as old as its batch; a batch inside a batch is not "
                 "carried"))
        return
    splits = [n for n in blocking.stops(name, after, lambda n: _kind(
        blocks.get(n) or {}) == "SEPARATE", lambda n: True)]
    needs = set().union(*(_needs(s, blocks, after) for s in splits))
    for index, spec in enumerate(members):
        if spec.get("holds") is not None:       # given back after the batch splits
            _validate_holds(layout, {"block": name, "count": 1, "holds": spec["holds"],
                                     "age": spec.get("age", 0)},
                            {}, blocks, sets, start, serving, f"{at}/members/{index}", problems)
        for x in sorted(needs - set(spec.get("holds") or {})):
            problems.append(problem(
                "E_INITIAL_RESOURCE", f"initial_state: block '{name}': member {index} gives "
                f"'{x}' back after the batch splits, and does not hold it",
                f"{at}/members/{index}/holds", block=name,
                hint="a member holds what it took before it was batched: say so in its holds"))


def _same(value, expected) -> bool:
    return abs(value - expected) <= 1e-9 * max(1.0, abs(expected))


def _fixed_seconds(layout, segment: dict, profile, slow: bool, time_unit) -> float | None:
    """The time, in model units, a MOTION or tending segment takes whatever is drawn: a move
    at *profile*'s speed (slowed when *slow*), a plain-number `time` in *time_unit* (as
    `sample_duration`). None when the model does not fix it: an expression (its draw is the
    state's, D444), or a move with no usable profile (the motion section's own problem)."""
    units = units_of(layout.get("simulation"))
    if segment.get("kind") == "move":
        try:
            return timing.move_seconds(profile, segment, slow) * base_factor("time", "seconds",
                                                                              units)
        except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return None
    literal = segment.get("time")
    try:
        literal = None if isinstance(literal, bool) else float(literal)
    except (TypeError, ValueError):
        return None
    unit = str(time_unit or "seconds")
    scale = 1.0 if unit in ("base", units.time) else base_factor("time", unit, units)
    return max(literal * scale, 0.0)


def _tending_seconds(layout, station: dict, which: str, arm) -> tuple:
    """#469: the durations a tending `pick` or `place` cycle (*which*) of *station* can take
    with the fleet's *arm* profile, as `tending.cycle_seconds` times it: moves slowed under
    hrc collaborative, either way under separation (the zone decides at the cycle's start).
    Empty when the model does not fix it: no such cycle (the fleet's flat time), or a
    segment that is an expression."""
    group = (station.get("params") or {}).get("tending") or {}
    segments = group.get(which) if isinstance(group, dict) else None
    if not isinstance(segments, list) or not segments \
            or not all(isinstance(s, dict) for s in segments):
        return ()
    profile = timing.build_motion_profiles(layout).get(str(arm or ""))
    hrc = group.get("hrc", "none")
    out = []
    for slow in {"none": (False,), "collaborative": (True,)}.get(hrc, (False, True)):
        parts = [_fixed_seconds(layout, s, profile, slow, group.get("time_unit"))
                 for s in segments]
        if None in parts:
            return ()
        out.append(sum(parts))
    return tuple(out)


def _validate_cycle(layout, entry, block, start, at, problems) -> None:
    """D444: a MOTION job in service. With no `cycle` (a plant's) it runs a whole cycle drawn
    afresh (#461), so `ends` and `remaining`, which would cut it short, are refused. A `cycle`
    is one job's: the segment in progress (`stage`) ends at `until`, after the clock and
    within that segment's time of it; `times` has one duration per segment, each started one
    and each timed one after it (a move after it is null: it is timed when it starts); a move
    took what the profile gives it, slowed (`slow`) as the block's hrc allows; and the cycle
    began after the job was made."""
    params, cycle = block.get("params") or {}, entry.get("cycle")
    segments = [s for s in params.get("segments") or [] if isinstance(s, dict)]
    mode = params.get("hrc", "none")

    def move(i) -> bool:
        return segments[i].get("kind") == "move"

    ok = entry.get("ends") is None and entry.get("remaining") is None
    if ok and isinstance(cycle, dict) and "since" in cycle:
        # D464: a plant's: the segment its controller says and when that segment began,
        # at or before the clock and not before the job was made (when its age is given)
        stage, since, age = cycle.get("stage"), cycle.get("since"), entry.get("age")
        eps = 1e-9 * max(1.0, abs(start))
        ok = (set(cycle) == {"stage", "since"} and entry.get("count") == 1
              and entry.get("in_service") == 1 and isinstance(stage, int)
              and not isinstance(stage, bool) and 0 <= stage < len(segments)
              and _number(since) and since <= start + eps
              and (age is None or (_number(age) and since >= start - age - eps)))
    elif ok and cycle is not None:
        ok = (isinstance(cycle, dict) and set(cycle) <= {"stage", "until", "times", "slow"}
              and entry.get("count") == 1 and entry.get("in_service") == 1)
        stage, until, times, slow = (cycle.get(k) for k in ("stage", "until", "times", "slow")) \
            if ok else (None, None, None, None)
        slow = [] if slow is None else slow
        ok = (ok and isinstance(stage, int) and not isinstance(stage, bool)
              and 0 <= stage < len(segments) and isinstance(times, list)
              and len(times) == len(segments) and all(
                  (_number(t) and t >= 0) if i <= stage or not move(i) else t is None
                  for i, t in enumerate(times))
              and isinstance(slow, list) and len(set(map(str, slow))) == len(slow)
              and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i <= stage
                      and move(i) for i in slow))
        moved = [i for i in range(stage + 1) if move(i)] if ok else []
        ok = ok and (mode == "separation" or set(slow) == (set(moved) if mode == "collaborative"
                                                           else set()))
        eps = 1e-9 * max(1.0, abs(start))
        age = entry.get("age", 0)
        made = start - (age if _number(age) else 0.0)
        ok = (ok and _number(until) and start <= until <= start + times[stage] + eps
              and until - sum(times[:stage + 1]) >= made - eps)
        profile = timing.build_motion_profiles(layout).get(str(params.get("profile") or ""))
        for i, segment in enumerate(segments if ok else ()):
            if times[i] is not None:
                fixed = _fixed_seconds(layout, segment, profile, i in slow, params.get("time_unit"))
                ok = ok and (fixed is None or _same(times[i], fixed))
    if not ok:
        problems.append(problem(
            "E_INITIAL_CYCLE", f"initial_state: block '{entry.get('block')}': a MOTION job in "
            f"service carries its cycle, not ends or remaining: one entity, the segment in "
            f"progress (stage) ending at until, and the time of each segment", f"{at}/cycle",
            hint="until is at or after the clock and no further than that segment's time; times "
                 "has one value per segment (null for a move not started), a move's the "
                 "profile's; slow lists the moves timed slowed; the job's age covers the "
                 "segments up to until (the cycle began after it was made); a plant's cycle "
                 "is the segment (stage) and when it began (since, at or before the clock); "
                 "with no cycle the job runs a whole cycle drawn afresh"))


def _room(layout, level) -> float:
    """#458: the most a level can ever hold above its minimum: its capacity less its `min`
    when both are numbers, else no bound (a `min` of None is none below)."""
    spec = next((l for l in layout.get("levels") or [] if isinstance(l, dict)
                 and l.get("name") == level), {})
    capacity, minimum = spec.get("capacity"), spec.get("min", 0.0)
    return capacity - minimum if _number(capacity) and _number(minimum) else math.inf


def _released(block: dict) -> str | None:
    """The resource *block* gives back from what an entity holds: a RELEASE's, or a PROCESS's
    that releases without seizing ("Delay Release"); None for any other block."""
    params, kind = block.get("params") or {}, str(block.get("type", "")).upper()
    action = str(params.get("action", "Seize Delay Release"))
    if kind == "RELEASE" or (kind in blocking.PROCESS_TYPES and action.endswith("Release")
                             and not action.startswith("Seize")):
        return params.get("resource_name") or None
    return None


def _seized(block: dict) -> str | None:
    """The resource *block* seizes for an entity: a SEIZE's, or a PROCESS's whose action
    seizes; None for any other block."""
    params, kind = block.get("params") or {}, str(block.get("type", "")).upper()
    if kind == "SEIZE" or (kind in blocking.PROCESS_TYPES and str(
            params.get("action", "Seize Delay Release")).startswith("Seize")):
        return params.get("resource_name") or None
    return None


def _waits(max_wait) -> bool:
    """A BATCH's maximum wait that can run (D433): a number > 0, or an expression."""
    try:
        return float(max_wait) > 0
    except (TypeError, ValueError):
        return bool(str(max_wait or "").strip())


def _places(block: dict) -> int:
    """The places of a block's own finite space: a BUFFER's capacity, a station's
    queue_capacity; 0 when it has none."""
    params = block.get("params") or {}
    key = "capacity" if str(block.get("type", "")).upper() == "BUFFER" else "queue_capacity"
    value = params.get(key, 1 if key == "capacity" else 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _validate_places(layout, state, blocks, sets, serving, waiting, start, problems) -> None:
    """Blocking and finite buffers (D381, D382): `held_by`, `held_since` and `place` of the
    waiting entries. Adds the units a blocked station keeps to *serving* and the places a
    buffer's parts hold to *waiting*, so the capacity checks of the caller count them."""
    out_links: dict = {}
    for link in layout.get("links") or []:
        if isinstance(link, dict):
            out_links.setdefault(link.get("source"), []).append(link.get("target"))
    kind = {name: str(b.get("type", "")).upper() for name, b in blocks.items()}
    blockers = blocking.layout_blocking_graph(list(blocks.values()), out_links)[0]

    def through(n) -> bool:
        # what a held part crosses without leaving its holder: the transparent blocks and
        # a buffer with no place
        return kind.get(n) in blocking.TRANSPARENT_TYPES \
            or (kind.get(n) == "BUFFER" and not _places(blocks[n]))

    ends: dict = {}         # holder -> where a part it still holds can wait, walked once

    def reaches(holder, target) -> bool:
        if holder not in ends:
            ends[holder] = blocking.stops(holder, lambda n: out_links.get(n, []),
                                          lambda n: not through(n), through)
        return target in ends[holder]

    picked = _picked(layout, blocks)    # a machine a cobot picks from blocks too (D446)

    for index, entry in enumerate(state.get("entities") or []):
        if not isinstance(entry.get("block"), str) or entry["block"] not in blocks:
            continue
        at, name = f"/{SECTION}/entities/{index}", entry["block"]
        place, holder = entry.get("place"), entry.get("held_by")
        if place is not None and (not isinstance(place, bool) or entry.get("in_service")
                                  or entry.get("arrives") is not None
                                  or (place is False and not _places(blocks[name]))):
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: block '{name}': place must be true or false, "
                f"on waiting entities, and false only at a block that has places",
                f"{at}/place", block=name,
                hint="false says the entity waits for a place of a BUFFER or of a queue_capacity"))
        if holder is None:
            continue
        known = isinstance(holder, str) and holder in blocks
        params = (blocks[holder].get("params") or {}) if known else {}
        buffer = known and kind[holder] == "BUFFER" and _places(blocks[holder]) > 0
        station = (known and kind[holder] in blocking.PROCESS_TYPES
                   and params.get("action", "Seize Delay Release") == "Seize Delay Release"
                   and bool(params.get("resource_name"))
                   and (holder in blockers or (reaches(holder, name) and name in picked)))
        # a belt whose end hands its parts to another belt (D430)
        belt = known and kind[holder] == "CONVEYOR" and params.get("accumulating", True) \
            is not False and any(kind.get(t) == "CONVEYOR" for t in blockers.get(holder, ()))
        # A part that has a place of the block it waits at was accepted there: nothing
        # holds it any more.
        if not (buffer or station or belt) or entry.get("count") != 1 or entry.get("in_service") \
                or entry.get("arrives") is not None or not reaches(holder, name) \
                or (_places(blocks[name]) and place is not False):
            problems.append(problem(
                "E_INITIAL_BLOCK", f"initial_state: block '{name}': held_by must name a BUFFER "
                f"with places, or a station that blocks after service, from which one waiting "
                f"entity that has no place here can have come", f"{at}/held_by", block=name,
                hint="the block that still holds the part: the buffer whose place it has, or "
                     "the station that keeps its unit until the part is accepted"))
            continue
        if buffer:
            waiting[holder] = waiting.get(holder, 0) + 1
            continue
        since, member = entry.get("held_since"), entry.get("member")
        if belt:            # its cell is counted with the belt's (`_validate_belts`)
            if not (_number(since) and since <= start) or member is not None:
                problems.append(problem(
                    "E_INITIAL_REMAINING", f"initial_state: block '{name}': held_since must be "
                    f"the time the part entered the belt in held_by, at or before the clock",
                    f"{at}/held_since", block=name, hint="a belt's part has no member"))
            continue
        age = entry.get("age", 0)
        made = start - (age if _number(age) else 0.0) - 1e-9 * max(1.0, abs(start))
        if since is not None and not (_number(since) and made <= since <= start):
            problems.append(problem(      # not before the part was made either (#460's class)
                "E_INITIAL_REMAINING", f"initial_state: block '{name}': held_since must be a "
                f"time at or before the clock, and after the part was made", f"{at}/held_since",
                block=name, hint="the absolute time the station in held_by seized its unit"))
        if member is not None and member not in sets.get(params.get("resource_name"), ()):
            problems.append(problem(
                "E_INITIAL_SET", f"initial_state: block '{name}': member '{member}' is not a "
                f"member of the resource set the station in held_by seizes", f"{at}/member",
                block=name, hint="member names which resource of the set holds the part"))
            continue
        resource = member if member is not None else params.get("resource_name")
        quantity = params.get("quantity", 1)
        serving[resource] = serving.get(resource, 0) + (quantity if isinstance(quantity, int)
                                                        else 1)


def _arms(layout) -> set:
    """The fleets with an arm, in a model with a tending station (else none tends, D169)."""
    tends = any(_kind(b) == "STATION" and isinstance((b.get("params") or {}).get("tending"), dict)
                and (b.get("params") or {}).get("tending") for b in layout.get("blocks") or [])
    return {f.get("name") for f in layout.get("fleets") or [] if isinstance(f, dict)
            and str(f.get("arm") or "").strip()} if tends else set()


def _picked(layout, blocks) -> set:
    """The TRANSPORT blocks of an arm fleet: a part's machine holds it until the cobot
    picks it, when its station tends (D169, D446)."""
    arms = _arms(layout)
    return {n for n, b in blocks.items() if _kind(b) == "TRANSPORT"
            and (b.get("params") or {}).get("fleet") in arms}


def _tended(blocks) -> dict:
    """{station name (its alias, as `entity.station` has it): block name} of the STATIONs
    that declare tending."""
    out = {}
    for name, block in blocks.items():
        params = block.get("params") or {}
        if _kind(block) == "STATION" and isinstance(params.get("tending"), dict) \
                and params["tending"]:
            out[str(params.get("station") or name)] = name
    return out


def _machine(blocks, layout, station: str):
    """The one PROCESS or MOTION a part placed at *station* (a block name) is worked by:
    the first block after it that is not transparent (`tending.machine_of`); None when
    that is not one block that seizes a resource with no worker."""
    links: dict = {}
    for link in layout.get("links") or []:
        if isinstance(link, dict):
            links.setdefault(link.get("source"), []).append(link.get("target"))
    found = blocking.stops(station, lambda n: links.get(n, []),
                           lambda n: _kind(blocks.get(n) or {}) not in blocking.TRANSPARENT_TYPES,
                           lambda n: _kind(blocks.get(n) or {}) in blocking.TRANSPARENT_TYPES)
    block = blocks.get(found[0]) if len(found) == 1 else None
    params = (block or {}).get("params") or {}
    # #470: a finite input queue takes the placed part before its unit: it holds none
    return block if (block is not None and _seized(block) and _released(block) is None
                     and str(params.get("action", "Seize Delay Release")).endswith("Release")
                     and _kind(block) in blocking.PROCESS_TYPES
                     and not params.get("worker_fleet") and not _places(block)
                     and not params.get("hold_until_space")) else None


def _tends_fits(entry, trip, record, blocks, layout, start, arm, tended, serving) -> bool:
    """D446, D447: what a cobot's trip carries at a tending station, and none of it
    elsewhere. Picked from one (its `station` tends), the part is held_by that station's
    machine until the pick ends; the pick (`load`) and the place (`unload`) in progress carry
    their `cycle`, which the vehicle's `until` ends within of the clock and which is the
    group's time when the model fixes it (#469); a part carried to one unloads nothing (the
    place is the cobot's). A placed part has entered the station and holds a unit of its
    machine, counted into *serving*. *arm*: the fleet's arm profile, falsy for no arm."""
    attributes = entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {}
    here = attributes.get("station") if isinstance(attributes.get("station"), str) else None
    picking = arm and here in tended
    placing = arm and trip["target"] in tended.values()
    stage = trip.get("stage") if trip.get("unit") is not None else None
    cycle, until = trip.get("cycle"), record.get("until")
    holder = entry.get("held_by")
    if picking and stage in (None, "pickup", "load"):
        # the machine after its station (`_validate_places` checks it blocks there)
        if not isinstance(holder, str) or not blocking.stops(
                tended[here], _successors(layout, blocks, jumps=False),
                lambda n: n == holder, lambda n: True):
            return False
    elif holder is not None:
        return False
    if not ((stage == "load" and picking) or (stage == "unload" and placing)):
        return cycle is None and not (placing and stage == "carry" and trip.get("unload"))
    eps = 1e-9 * max(1.0, abs(start))
    age = entry.get("age", 0)
    if not (_number(cycle) and cycle >= 0 and _number(until) and until <= start + cycle + eps
            and until - cycle >= start - (age if _number(age) else 0.0) - eps):
        return False        # it ends within its cycle of the clock, begun after the part was made
    station = blocks[tended[here]] if stage == "load" else blocks[trip["target"]]
    fixed = _tending_seconds(layout, station, "pick" if stage == "load" else "place", arm)
    if fixed and not any(_same(cycle, x) for x in fixed):
        return False        # #469: a cycle the model fixes is that time (as D444's segments)
    if stage == "load":
        return True
    machine = _machine(blocks, layout, trip["target"])
    if machine is None or tended.get(here) != trip["target"]:
        return False
    params = machine.get("params") or {}
    quantity = params.get("quantity", 1)
    serving[params["resource_name"]] = serving.get(params["resource_name"], 0) + (
        quantity if isinstance(quantity, int) and not isinstance(quantity, bool) else 1)
    return True


_STAGES = ("pickup", "load", "carry", "unload")


def _validate_fleets(layout, state, blocks, start, problems, serving) -> None:
    """Fleets and vehicles (D384, D385): `initial_state.fleets` and the `trip` of each load.
    Everything `MoverFleet.restore` and `Transport.resume` read is checked here, so neither
    can meet a name the model does not have or a path the network does not. A cobot's
    pick and place (D446, D447) and the tending rows (D448) too; a placed part's unit of
    its machine counts into *serving*."""
    said = state.get("fleets")
    trips = [(i, e) for i, e in enumerate(state.get("entities") or [])
             if isinstance(e, dict) and e.get("trip") is not None]
    arms, tended = _arms(layout), _tended(blocks)

    def bad(text, at, hint="see the state page of the documentation: fleets and vehicles"):
        problems.append(problem("E_INITIAL_FLEET", f"initial_state: {text}",
                                f"/{SECTION}/{at}", hint=hint))

    rows = state.get("tended")
    if rows is not None and not (isinstance(rows, list) and all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(n, str) for n in r)
            and r[0] in tended and r[1] in arms for r in rows)
            and len({tuple(r) for r in rows}) == len(rows)):
        bad("tended must list [station, fleet] pairs, each once: a STATION that declares "
            "tending and a fleet with an arm", "tended",
            hint="the pairs a cobot tended before the clock")
    if said is None and not trips:
        return

    def time(value, past: bool) -> bool:
        return _number(value) and (value <= start if past else value >= start)

    if not isinstance(said, dict):
        return bad("fleets must be an object of fleets, one record per vehicle", "fleets")
    units = units_of(layout.get("simulation"))
    try:
        network = build_network(layout.get("network"), units)
    except Exception:       # a broken network is the network section's own problem
        return bad("the fleets need the model's network, which cannot be built", "fleets")

    def path(a, b) -> bool:
        return a == b or bool(network.path(a, b))

    def overdue(fleet, record, loaded) -> bool:
        """A drive that was due to end before the clock: the state contradicts itself."""
        try:
            speed = movers._speeds(fleet, units)[0 if loaded else 1]
            route = network.path(record["node"], record["drive"]["to"], speed)
            return record["drive"]["started"] + network.travel_time(route, speed) < start
        except (TypeError, ValueError):     # a broken speed is the fleets section's own problem
            return False

    declared = {f.get("name"): f for f in layout.get("fleets") or [] if isinstance(f, dict)}
    chargers = {c.get("name"): c for c in layout.get("chargers") or [] if isinstance(c, dict)}
    zoned = any(isinstance(s, dict) and s.get("capacity") is not None
                for s in (layout.get("network") or {}).get("segments") or ())
    plugged: dict = {}
    for name, spec in said.items():
        at, fleet = f"fleets/{name}", declared.get(name)
        vehicles = spec.get("vehicles") if isinstance(spec, dict) else None
        count = fleet and (len([u for u in fleet.get("units") or [] if isinstance(u, dict)])
                           or max(int(fleet.get("count", 1)), 1))
        if fleet is None or not isinstance(vehicles, list) or len(vehicles) != count \
                or not all(isinstance(v, dict) for v in vehicles):
            bad(f"'{name}' must be a fleet of the model with one record per vehicle", at)
            said = {k: v for k, v in said.items() if k != name}
            continue
        cursor = spec.get("cursor")
        if cursor is not None and (not isinstance(cursor, int) or isinstance(cursor, bool)
                                   or not -1 <= cursor < count):
            bad(f"fleet '{name}': cursor must be -1 or the index of a vehicle", f"{at}/cursor")
        # D386: what no vehicle record says is refused here as it is when a state is taken.
        battery = battery_of(fleet)
        kind = ("a gantry" if fleet.get("motion") == "gantry"
                else "a worker fleet" if fleet.get("kind") == "worker"
                else "a cobot fleet that exchanges parts" if name in arms and fleet.get("exchange")
                else "scheduled charging" if battery.get("strategy") == "scheduled"
                else "a drive under way on a network with segment capacities"
                if zoned and any(v.get("drive") is not None for v in vehicles) else None)
        if kind:
            bad(f"fleet '{name}': a state cannot carry {kind}", at,
                hint="see the coverage table of the state documentation")
            said = {k: v for k, v in said.items() if k != name}
            continue
        for unit, record in enumerate(vehicles):
            here, node = f"{at}/vehicles/{unit}", record.get("node")
            drive, charge, until = record.get("drive"), record.get("charge"), record.get("until")
            ok = isinstance(node, str) and network.has(node)
            ok = ok and (record.get("idle_since") is None or time(record["idle_since"], True))
            soc = record.get("soc")
            ok = ok and (soc is None or (_number(soc) and soc <= 1 and bool(battery)))
            ok = ok and (until is None or (time(until, False) and drive is None))
            if ok and drive is not None:
                ok = (isinstance(drive, dict) and isinstance(drive.get("to"), str)
                      and network.has(drive["to"]) and drive["to"] != node
                      and path(node, drive["to"]) and time(drive.get("started"), True))
            if ok and charge is not None:
                station = chargers.get(charge["charger"]) if isinstance(charge, dict) \
                    and isinstance(charge.get("charger"), str) else None
                stage = charge.get("stage", "charge") if station else None
                held, place = charge.get("held", True) if station else None, \
                    charge.get("wait", 0) if station else None
                ok = (station is not None and station.get("kind", "charge") == "charge"
                      and bool(battery) and isinstance(held, bool) and until is None
                      and stage in ("drive", "wait", "charge")
                      and (stage == "drive") == (drive is not None)
                      and (stage != "drive" or drive["to"] == station.get("node"))
                      and (stage == "drive" or node == station.get("node"))
                      and (stage != "charge" or time(charge.get("until"), False))
                      and (stage != "wait" or (held and isinstance(place, int)
                                               and not isinstance(place, bool) and place >= 0)))
                if ok and stage == "charge":
                    plugged[charge["charger"]] = plugged.get(charge["charger"], 0) + 1
            if not ok:
                bad(f"fleet '{name}', vehicle {unit}: a record is a node of the network, and "
                    f"optionally idle_since, soc, and one of drive (to, started), until or "
                    f"charge (charger, held, stage, until, wait)", here,
                    hint="times are absolute: started and idle_since at or before the clock, "
                         "until at or after it; soc is at most 1; a drive needs a path")
                said = {k: v for k, v in said.items() if k != name}
                break
    for name, used in plugged.items():
        points = chargers[name].get("points", 1)
        if not isinstance(points, int) or used > points:
            bad(f"{used} vehicles charge at '{name}', which has {points} points",
                "fleets", hint="the state shows more than the charger holds")

    taken: dict = {}
    for index, entry in trips:
        at, name, trip = f"entities/{index}/trip", entry.get("block"), entry["trip"]
        block = blocks.get(name) if isinstance(name, str) else None
        fleet = ((block or {}).get("params") or {}).get("fleet")
        block = block or {}
        ok = (str(block.get("type", "")).upper() == "TRANSPORT" and isinstance(trip, dict)
              and entry.get("count") == 1 and not entry.get("in_service")
              and entry.get("arrives") is None and fleet in said)
        ok = ok and all(isinstance(trip.get(k), str) and network.has(trip[k])
                        for k in ("origin", "destination"))
        ok = ok and path(trip["origin"], trip["destination"])
        ok = ok and isinstance(trip.get("target"), str) \
            and str(blocks.get(trip["target"], {}).get("type", "")).upper() == "STATION"
        unit = trip.get("unit") if ok else None
        if ok and unit is None:
            ok = trip.get("stage") is None
        elif ok:
            vehicles = said[fleet]["vehicles"]
            ok = isinstance(unit, int) and not isinstance(unit, bool) \
                and 0 <= unit < len(vehicles) and (fleet, unit) not in taken
            record = vehicles[unit] if ok else {}
            stage, drive = trip.get("stage", "carry"), record.get("drive")
            goal = trip["origin"] if stage in ("pickup", "load") else trip["destination"]
            ok = (ok and stage in _STAGES and record.get("charge") is None
                  and (trip.get("unload") is None or (_number(trip["unload"])
                                                      and trip["unload"] >= 0))
                  and (drive is None or (stage in ("pickup", "carry") and drive["to"] == goal))
                  and (record.get("until") is None or stage in ("load", "unload"))
                  and path(record["node"], goal)
                  and (stage in ("pickup", "carry") or record["node"] == goal))
            taken[(fleet, unit)] = stage
        arm = fleet in arms and str(declared[fleet]["arm"]).strip()
        if ok and not _tends_fits(entry, trip, record if unit is not None else {}, blocks,
                                  layout, start, arm, tended, serving):
            bad(f"block '{name}': a cobot's trip at a tending station: picked, the part is "
                f"held_by its machine until the pick ends; the pick (load) and the place "
                f"(unload) in progress carry their cycle; a part carried there unloads nothing",
                at, hint="the vehicle's until ends the cycle, at most cycle after the clock; "
                         "the station's machine is one PROCESS or MOTION with a free unit")
            continue
        if not ok:
            bad(f"block '{name}': a trip is at a TRANSPORT block whose fleet the state lists, "
                f"for one entity, with an origin and a destination node joined by a path, a "
                f"target STATION and, once a vehicle has it, its unit and stage", at,
                hint=f"stage is one of: {', '.join(_STAGES)}; a vehicle carries one trip")
    for name, spec in said.items():
        fleet = declared[name]
        homes = [u.get("home") for u in fleet.get("units") or [] if isinstance(u, dict)]
        for unit, record in enumerate(spec["vehicles"]):
            here, drive, stage = f"fleets/{name}/vehicles/{unit}", record.get("drive"), \
                taken.get((name, unit))
            idle = stage is None and record.get("charge") is None
            home = str((homes[unit] if homes else "") or fleet.get("home") or "").strip() \
                or next(iter(network.nodes), "")
            if idle and (record.get("until") is not None or (drive or {}).get("loaded")):
                bad(f"fleet '{name}', vehicle {unit} is handling or loaded and no entity's "
                    f"trip names it", here, hint="give the entity it carries a trip with this unit")
            elif idle and drive is not None and not (fleet.get("park") == "home"
                                                     and drive["to"] == home):
                bad(f"fleet '{name}', vehicle {unit} drives to '{drive['to']}' for no trip and "
                    f"no charge, which is only its return to its home node under park \"home\"",
                    here, hint="give the entity it drives for a trip with this unit")
            elif drive is not None and overdue(fleet, record, stage == "carry"):
                bad(f"fleet '{name}', vehicle {unit}: its drive to '{drive['to']}' was due to end "
                    f"before the clock", here,
                    hint="a drive under way arrives at or after the clock; one that has arrived "
                         "is a vehicle at that node")


_BELT_STAGES = ("cell", "slot", "ride", "packed", "move", "stop")
_AT_THE_END = ("move", "stop", "handover")


def _validate_belts(layout, state, blocks, start, problems) -> None:
    """Conveyors (D429 to D431): the `belt` of each part on one, and what each belt holds.
    Everything `Conveyor.restore` reads is checked here: a part's cell it takes at once, so
    the parts that have one fit the belt's cells, and the chain it rebuilds by entry time is
    one part at the end and the ones packed behind it."""
    def kind(name) -> str:
        return str((blocks.get(name) or {}).get("type", "")).upper() if isinstance(name, str) \
            else ""

    on = {}                 # belt -> [(entered, stage, until)] of the parts with a cell
    queued: dict = {}       # end stop -> parts at it, and jobs waiting to seize it (D431)
    stops = []              # (belt, end stop) of each part at one
    for entry in state.get("entities") or []:
        block = blocks.get(entry.get("block")) if isinstance(entry, dict) \
            and isinstance(entry.get("block"), str) else None
        waits = block and entry.get("arrives") is None and entry.get("held_by") is None \
            and isinstance(entry.get("count"), int) and isinstance(entry.get("in_service", 0), int)
        if waits and _seized(block):
            queued[_seized(block)] = queued.get(_seized(block), 0) \
                + entry["count"] - entry.get("in_service", 0)
    for index, entry in enumerate(state.get("entities") or []):
        if not isinstance(entry, dict):
            continue
        name, belt, holder = entry.get("block"), entry.get("belt"), entry.get("held_by")
        if kind(holder) == "CONVEYOR":      # handed over by its end (`_validate_places`)
            on.setdefault(holder, []).append((entry.get("held_since"), "handover", None))
        if belt is None and kind(name) != "CONVEYOR":
            continue
        params = (blocks.get(name) or {}).get("params") or {}
        stage = belt.get("stage") if isinstance(belt, dict) else None
        until = belt.get("until") if isinstance(belt, dict) else None
        entered = belt.get("entered") if isinstance(belt, dict) else None
        timed, ridden = stage in ("slot", "ride", "move"), stage in ("ride", "packed", "move", "stop")
        ok = (kind(name) == "CONVEYOR" and params.get("accumulating", True) is not False
              and isinstance(belt, dict) and set(belt) <= {"stage", "until", "entered"}
              and stage in _BELT_STAGES and entry.get("count") == 1
              and not entry.get("in_service") and entry.get("arrives") is None
              and entry.get("trip") is None and entry.get("place") is None
              and ((_number(until) and until >= start) if timed else until is None)
              and ((_number(entered) and entered <= start) if ridden else entered is None)
              and (stage != "stop" or bool(str(params.get("exit_resource") or "").strip()))
              and (holder is None or stage in ("cell", "slot")))
        if not ok:
            problems.append(problem(
                "E_INITIAL_BELT", f"initial_state: block '{str(name)[:80]}': a part on a belt is "
                f"one entity at an accumulating CONVEYOR with its belt stage, until for slot, "
                f"ride and move, and entered for ride and after", f"/{SECTION}/entities/{index}",
                hint=f"stage is one of: {', '.join(_BELT_STAGES)}; until is at or after the "
                     f"clock, entered at or before it; stop needs the belt's exit_resource"))
        elif stage != "cell":
            on.setdefault(name, []).append((entered, stage, until))
            if stage == "stop":
                stop = str(params["exit_resource"]).strip()
                queued[stop] = queued.get(stop, 0) + 1
                stops.append((name, stop))
    for name, stop in stops:
        if queued[stop] > 1:
            problems.append(problem(
                "E_INITIAL_BELT", f"initial_state: belt '{name}': a part at its end stop "
                f"'{stop}', which other jobs queue for, is not carried (D431)",
                f"/{SECTION}/entities", block=name,
                hint="take the state at another time"))
    if not on:
        return
    units = units_of(layout.get("simulation"))
    try:
        network = build_network(layout.get("network"), units)
    except Exception:       # a broken network is the network section's own problem
        network = None
    for name, parts in on.items():
        at = f"/{SECTION}/entities"
        if kind(name) != "CONVEYOR":
            continue
        chain = sorted((p for p in parts if p[1] not in ("slot", "ride")),
                       key=lambda p: p[0] if _number(p[0]) else math.inf)
        if any(p[1] in _AT_THE_END for p in chain[1:]) or (chain and chain[0][1] == "packed"):
            problems.append(problem(
                "E_INITIAL_BELT", f"initial_state: belt '{name}': one part is at its end and "
                f"the others past the ride are packed behind it, in the order they entered",
                at, block=name, hint="move, stop and a part handed over are at the end"))
        geometry = _geometry((blocks.get(name) or {}).get("params") or {}, units, network)
        if geometry is None:
            continue
        cells, gap, travel = geometry
        if len(parts) > cells:
            problems.append(problem(
                "E_INITIAL_QUEUE_CAPACITY", f"initial_state: {len(parts)} parts have a cell of "
                f"belt '{name}', which has {cells}", at, block=name,
                hint="the parts from slot on, and the one its end hands over, each have a cell"))
        spec = (state.get("blocks") or {}).get(name)
        if not _times_fit(parts, spec.get("next_entry") if isinstance(spec, dict) else None,
                          start, cells, gap, travel):
            problems.append(problem(
                "E_INITIAL_BELT", f"initial_state: belt '{name}': its times do not fit it: a "
                f"ride lasts {travel:g}, the parts entered and board at least {gap:g} apart, "
                f"and next_entry follows the last slot", at, block=name,
                hint="ride ends at entered + the ride; a part past the ride entered at least "
                     "the ride before the clock; move ends within one spacing of it"))


def _geometry(params: dict, units, network) -> tuple | None:
    """(cells, entry gap, ride) of a belt from its layout, as `Conveyor.capacity`,
    `spacing / speed` and `travel_time` count them; None when its length cannot be known
    here (its own validation says why)."""
    from nagasim.core.blocks.conveyor import SPEC

    def value(key, default):
        return scaled(float(params.get(key, default) or default),
                      field_factor(param(SPEC, key), params, units, SPEC.params))

    try:
        length, spacing, speed = value("length", 0.0), value("spacing", 1.0), value("speed", 1.0)
        if not length > 0:
            length = network.distance(params["from_node"], params["to_node"])
        return max(int(length // spacing), 1), spacing / speed, length / speed
    except Exception:       # no network, no path, a unit it does not know
        return None


def _times_fit(parts, next_entry, start, cells, gap, travel) -> bool:
    """D234 for a belt's times, [(entered, stage, until)] of its parts with a cell: each
    ride lasts the ride, a part past it entered a ride before the clock, entries (those made,
    the slots, the next) are a gap apart, and nothing waits beyond the belt's own time."""
    entered = sorted(p[0] for p in parts if p[0] is not None)
    slots = sorted(p[2] for p in parts if p[1] == "slot")
    times = entered + slots + ([next_entry] if next_entry is not None else [])
    if not all(_number(t) for t in times):
        return True         # refused by the check of its own key
    eps = 1e-9 * max(1.0, abs(start) + travel)
    return (all(t >= 0 for t in entered)
            and all(b - a >= gap - eps for a, b in zip(times, times[1:]))
            and all(t <= start + (cells + 1) * gap + eps for t in times)
            and (next_entry is not None or not slots)
            and all(abs(e + travel - u) <= eps if s == "ride" else e + travel <= start + eps
                    for e, s, u in parts if e is not None)
            and all(u <= start + gap + eps for e, s, u in parts if s == "move"))


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
