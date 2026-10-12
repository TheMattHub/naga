"""A state as data (PHASE7 7.10, D231 to D238): what a model looks like at one instant.

A state is the `initial_state` section (`core/initial_state.py`) inside a document:

    {"format": 1,
     "model": {"name": "line", "hash": "<sha256>"},
     "taken": {"source": "run" | "observed", "at": 480.0, "seed": 3, "stamp": "<ISO 8601>"},
     "state": {"clock": 480.0, "entities": [...], "arrivals": {...}, ...},
     "hash": "<sha256>"}

* `take` runs a model to a time and reads its state out; `observe` reads it from a plant
  through the model's own data bindings; `start` puts one into a model, checked.
* THE REPRODUCTION RULE (D233). A run started from a state taken at t gives the results of
  the uninterrupted run with a warm-up of t: exactly when nothing random is drawn after t,
  in distribution otherwise. Stream positions are not state (a plant has none), entity ids
  are the new run's own, and nothing collected before t is carried.
* A state taken from a run is COMPLETE OR REFUSED (D234): what the vocabulary cannot carry
  is `E_STATE_UNSUPPORTED`, naming the kind. Never a silent partial start.
* `hash` is over the model's hash and the state, not over `taken`: the same state has one
  hash wherever it came from, which is what an advice record names (D228).

A state file is data: it is read like any other input and selects no code.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import copy
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from nagasim.core import initial_state, schema
from nagasim.core.block_registry import queueing_types
from nagasim.core.problems import Problem, problem
from nagasim.core.session import SimSession
from nagasim.live import stamp as stamp_seconds

__all__ = ["FORMAT", "MAX_BYTES", "MAX_ENTITIES", "StateError", "Tap", "take", "observe", "from_plant",
           "refused_kind",
           "document", "model_hash", "start", "check", "read", "main"]

FORMAT = 1
MAX_BYTES = 16 * 2 ** 20        # of a state's canonical JSON
MAX_ENTITIES = 100_000          # entity entries
# An entity waiting here is put back by arriving again: it queues on the resource. Any
# other block holds an entity for a time of its own or in a store (HOLD, BATCH, MATCH),
# which no entry can carry.
_QUEUEING = frozenset(queueing_types())
# ... or it sits in the block's store, which arriving again rebuilds in order (D260).
_STORED = frozenset({"HOLD", "BATCH", "MATCH"})
# What the engine keeps on an entity for itself; everything else public is the model's.
_ENGINE_ATTRIBUTES = frozenset({"name", "batch_elems", "in_batch", "req_params", "initial",
                                "agent"})


class StateError(Exception):
    """A coded refusal; `.problem` is the `Problem` a report carries."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    @property
    def problem(self) -> Problem:
        return problem(self.code, self.message, "/initial_state", hint=self.hint)


def _unsupported(kind: str, what: str) -> StateError:
    error = StateError("E_STATE_UNSUPPORTED", f"a state cannot carry {kind}: {what}",
                       "take the state at another time, or see the coverage table of the "
                       "state documentation")
    error.kind = kind
    return error


# ---------------------------------------------------------------- identity
def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model(layout) -> dict:
    """The migrated layout: what is hashed, and what a state is put into."""
    return schema.migrate(schema.read_layout(layout))


def model_hash(layout) -> str:
    """SHA-256 of the model without its `initial_state` (D237): a state belongs to the model
    file it was taken for, whatever state that file itself carries."""
    model = _model(layout)
    return _sha(_canonical({k: v for k, v in model.items() if k != initial_state.SECTION}))


def document(layout, state: dict, *, name: str | None = None, **taken) -> dict:
    """The document of *state* for the model *layout*; *taken* is recorded, not hashed."""
    model = _model(layout)
    digest = model_hash(model)
    return {"format": FORMAT,
            "model": {"name": name or (model.get("info") or {}).get("name") or "", "hash": digest},
            "taken": taken, "state": state, "hash": _sha(_canonical([digest, state]))}


# ---------------------------------------------------------------- out of a run
class Tap:
    """Where each entity is, heard on the run's bus: {id: (block, when it gets there or
    None, since when it is there)}, in the order they got there, the order they queue in. The run keeps no
    such map itself (D235); only a caller that wants a state pays for one."""

    def __init__(self):
        self.where: dict = {}

    def __call__(self, name, *, block=None, entity=None, target=None, duration=None, **_):
        if name == "entity_created":
            # A warm-started entity has waited since before this run began (#348).
            since = entity._since
            self.where[entity.id] = (block.name, None,
                                     float(block.env.now if since is None else since))
        elif name == "entity_moved" and target is not None:
            self.where.pop(entity.id, None)         # re-inserted: last in the new block
            at = float(block.env.now + duration) if duration else None
            self.where[entity.id] = (target, at, float(block.env.now) if at is None else at)
        elif name == "entity_disposed":
            self.where.pop(entity.id, None)


def take(layout, at: float, seed: int = 0, *, name: str | None = None) -> dict:
    """The state document of *layout* at model time *at* of the run with *seed*.

    Raises `StateError` (`E_STATE_UNSUPPORTED`) for what a state cannot carry, an
    instant with an event due and a time at or after the end of the run."""
    tap = Tap()
    session = SimSession(layout, seed, subscribers=(tap,))
    reached = session.advance(to=float(at))
    if reached == "decision":
        raise _unsupported("a pending decision", f"a Policy DECIDE waits at t = {session.now:g}")
    if reached == "horizon" or session.now != float(at):
        raise _unsupported("the end of the run", f"the run ends at t = {session.now:g}; "
                           f"take the state before it")
    return document(layout, _read(session, tap), name=name, source="run", at=float(at),
                    seed=int(seed))


def _plain(value):
    return value.item() if getattr(value, "shape", None) == () else value


def _refuse_kinds(session) -> None:
    """The element kinds with no vocabulary: refused whole, by name."""
    ctx = session.ctx
    for kind, found in (("agent populations", ctx.agent_system is not None),
                        ("fleets and vehicles", ctx.fleets),
                        ("conveyors", ctx.conveyors),
                        ("chargers", ctx.chargers),
                        ("blocking and finite buffers", ctx.blocking_nodes),
                        ("a dispatch plan", ctx.plan_gates is not None)):
        if found:
            raise _unsupported(kind, "the model has some")
    for block in session.model.blocks.values():
        kind = block.block.block_type
        # A store's entities are carried by arriving again (D260); what runs beside them
        # on a clock of its own is not.
        if kind == "BATCH" and block._timer is not None:
            raise _unsupported("a batch's maximum wait", f"'{block.name}' has one running")
        if kind == "HOLD" and block.mode == "Condition" and block.waiting_entities \
                and block._wake_keys()[1]:
            raise _unsupported("a polled condition", f"HOLD '{block.name}' scans on a clock")
        if ctx.deterministic and getattr(block, "mode", None) == "Chance":
            raise _unsupported("a deterministic split's counts", f"DECIDE '{block.name}'")


def refused_kind(layout) -> str | None:
    """The first thing about the model *layout* that no state can carry (D234, D264), or
    None: asked before a start is made from a plant, which says nothing about a fleet and
    would otherwise start it as declared (D270). The model is built, not run."""
    try:
        _refuse_kinds(SimSession(layout, 0))
    except StateError as exc:
        return exc.kind
    return None


def _levels(session) -> dict:
    system, now = session.ctx.level_system, session.now
    if system is None:
        return {}
    if system.pipelines or system.stateful:
        raise _unsupported("a pipeline, sample or noise delay", "its history is not a value")
    if system.dt is not None and abs(now / system.dt - round(now / system.dt)) > 1e-9:
        raise _unsupported("a fixed-step model between two steps",
                           f"t = {now:g} is not a multiple of dt = {system.dt:g}")
    system.sync(now)
    out = {}
    for name, level in system.levels.items():
        if level.conveyor is not None or level.components or level.pending:
            raise _unsupported("a conveyor, a blended level or a waiting transfer", f"'{name}'")
        out[name] = level.level
    return out


def _attributes(entity) -> dict:
    out = {}
    for key, value in vars(entity).items():
        if key.startswith("_") or key in _ENGINE_ATTRIBUTES:
            continue
        value = _plain(value)
        if key == "batch_info" and isinstance(value, dict):
            # SEPARATE's own record of the batch a child came from: nothing reads it back,
            # so its plain part is carried and the entity objects it lists are not.
            value = {k: _plain(v) for k, v in value.items()
                     if isinstance(_plain(v), (str, int, float, type(None)))}
        try:
            _canonical(value)
        except (TypeError, ValueError):
            raise _unsupported("an attribute that is not plain data",
                               f"entity {entity.id}.{key}") from None
        out[key] = value
    return out


_PURE_CALLS = frozenset({"IF", "MIN", "MAX", "ABS"})


def _rank_can_drift(session, target_name: str) -> bool:
    """A ranked queue freezes each request's rank when it is made. Put back, an entity is
    ranked at the clock: the same value only if the expression reads the entity alone, or
    the resource reranks at every grant anyway."""
    manager = session.ctx.resource_manager
    target = manager.resolve(target_name)
    for resource in getattr(target, "members", None) or [target]:
        ranking = getattr(resource, "_ranking", None)
        if ranking is None or resource.discipline in ("FIFO", "LIFO") or resource.rerank:
            continue
        if set(ranking.names) - {"entity"} or set(ranking.calls) - _PURE_CALLS:
            return True
    return False


def _place(block, entity) -> float:
    """Where *entity* stands in the block's own store (its own order, #349)."""
    if block.block.block_type == "MATCH":
        return next((seq for waiting in block._waiting.values() for queue in waiting.values()
                     for seq, held in queue if held is entity), math.inf)
    items = block.store.items
    return items.index(entity) if entity in items else math.inf


def _entities(session, tap: Tap) -> list:
    ctx, model, now = session.ctx, session.model, session.now
    serving, others = [], []        # in service first: each must seize before a queue forms
    queued, kept, stored = [], [], 0    # waiting for a resource; waiting in a store
    live = ctx.live_entities._items
    for entity_id, (block_name, arrives, since) in tap.where.items():
        entity = live.get(entity_id)
        if entity is None:
            continue                # inside a formed batch: found through the batch entity
        block = model.blocks.get(block_name)
        if entity.batch_elems or block is None:
            raise _unsupported("a formed batch", f"entity {entity.id}")
        kind = block.block.block_type
        held = set(getattr(entity, "req_params", None) or ())
        entry = {"block": block_name, "count": 1, "e_type": entity.e_type,
                 "age": float(now - entity.creation_time), "attributes": _attributes(entity)}
        if entity._auto_cost or entity._holding_cost:
            entry.update(auto_cost=entity._auto_cost, holding_cost=entity._holding_cost)
        if arrives is not None and arrives >= now:
            if held:
                raise _unsupported("a job that holds a resource between blocks",
                                   f"entity {entity.id} on its way to '{block_name}'")
            others.append({**entry, "arrives": float(arrives)})
        elif entity in block.active_entity and kind in initial_state.SERVICE_TYPES:
            ends = entity._ends
            target = initial_state._target_name(block)
            seized = {target} if getattr(block, "seizes", False) or kind == "SEIZE" else set()
            if seized and not held:     # it gave its unit up and waits to resume
                raise _unsupported("a job preempted, changing over or mid-cycle",
                                   f"entity {entity.id} in '{block_name}'")
            if held != seized:
                raise _unsupported("a job that holds a resource between blocks",
                                   f"entity {entity.id} in '{block_name}' holds "
                                   f"{', '.join(sorted(held)) or 'nothing'}")
            if ends is None or ends < now:
                raise _unsupported("a job preempted, changing over or mid-cycle",
                                   f"entity {entity.id} in '{block_name}'")
            entry.update(in_service=1, ends=[float(ends)])
            if held:                # when it seized: its busy cost runs from then
                entry["since"] = float(entity.req_params[target]["seized_at"])
                if entity.req_params[target]["set"] is not None:    # and on which member
                    entry["member"] = entity.req_params[target]["resource"].name
            serving.append(entry)
        elif kind in _QUEUEING and not held:
            if _rank_can_drift(session, initial_state._target_name(block)):
                raise _unsupported("a ranked queue whose ranks were fixed when each entity "
                                   "joined", f"'{block_name}': its ranking reads more than the "
                                   f"entity; set \"rerank\" on the resource")
            entry["since"] = since
            if getattr(block, "renege_after", ""):
                entry["deadline"] = float(entity._deadline)     # D261: none is drawn twice
            queued.append((getattr(entity, "_queued", -1), len(queued), entry))
        elif kind in _STORED and not held:
            if kind == "MATCH" and entity.e_type not in block.type_store_map:
                # Unmapped, a type goes to the first empty store: where it sits is history.
                raise _unsupported("a MATCH with no type_store_map",
                                   f"'{block_name}' holds a {entity.e_type}; map its types")
            stored += 1
            kept.append((block_name, _place(block, entity), len(kept), {**entry, "since": since}))
        else:
            raise _unsupported(
                "an entity where it is", f"entity {entity.id} in the {kind} '{block_name}'"
                + (f" holds {', '.join(sorted(held))}" if held else ""))
    held_in_stores = sum(len(store.items) for store in ctx.store_manager.all())
    if held_in_stores != stored:
        raise _unsupported("a store's content", f"{held_in_stores - stored} items are not "
                           f"entities waiting at a HOLD, a BATCH or a MATCH")
    # An entity a state put on a link is not heard of before it arrives (#338).
    for entity in live.values():
        if entity.id not in tap.where and getattr(entity, "_arrives", None):
            block_name, arrives = entity._arrives
            entry = {"block": block_name, "count": 1, "e_type": entity.e_type,
                     "age": float(now - entity.creation_time),
                     "attributes": _attributes(entity), "arrives": float(arrives)}
            if entity._auto_cost or entity._holding_cost:
                entry.update(auto_cost=entity._auto_cost, holding_cost=entity._holding_cost)
            others.append(entry)
    # #349: in the order of the queue or the store ITSELF, not the order each was heard
    # setting out: one that came over a timed link stands behind those that arrived
    # meanwhile, and two of one instant stand as the run's events put them. A queue: the
    # order its requests were made in. A store: its own items. Then those still on a link.
    others.sort(key=lambda e: e["arrives"])
    others = [e for *_key, e in sorted(queued, key=lambda q: q[:2])] \
        + [e for *_key, e in sorted(kept, key=lambda k: k[:3])] + others
    if len(live) != len(serving) + len(others):
        raise _unsupported("an entity with no known place", "one was made outside a block")
    return serving + others


def _refuse_due(session, tap: Tap) -> None:
    """An event of the model due at this very instant is neither before the state nor after
    it: which side it falls on would be decided by the order of that instant (D233). So no
    state is taken there. Checked on what a state carries a time for, and on the shifts; a
    fixed-step model's own grid point is not such an event (its state is taken ON the grid).
    The run's first instant is exempt: nothing has happened and the state is what it was
    given."""
    ctx, now = session.ctx, session.now
    if now <= ctx.clock:
        return
    live = ctx.live_entities._items
    due = [f"the service of entity {i}" for i, (_b, _a, _s) in tap.where.items()
           if i in live and live[i]._ends == now]
    due += [f"entity {i} reaching '{b}'" for i, (b, arrives, _s) in tap.where.items()
            if i in live and arrives == now]
    blocks = session.model.blocks       # a deadline counts only while its entity waits
    due += [f"the patience of entity {i}" for i, (b, _a, _s) in tap.where.items()
            if i in live and getattr(live[i], "_deadline", None) == now
            and getattr(blocks.get(b), "renege_after", "")
            and live[i] not in blocks[b].active_entity]
    due += [f"entity {e.id} reaching '{e._arrives[0]}'" for e in live.values()
            if getattr(e, "_arrives", None) and e._arrives[1] == now]
    due += [f"an arrival at '{name}'" for name, block in session.model.blocks.items()
            if getattr(block, "next_at", None) == now]
    due += [f"failure '{f}' of '{name}'" for name, r in ctx.resource_manager.resources.items()
            for f, (_phase, t) in r.failure_clock.items() if t == now]
    before = math.nextafter(now, -math.inf)
    due += [f"a change of schedule '{name}'" for name, schedule in ctx.schedules.items()
            if schedule.next_change_after(before) <= now]
    if due:
        raise _unsupported("an instant with an event due", f"{due[0]} at t = {now:g}; take "
                           f"the state at a time between two events")


def _read(session, tap: Tap) -> dict:
    """The `initial_state` section of a paused session."""
    _refuse_kinds(session)
    _refuse_due(session, tap)
    ctx, now = session.ctx, session.now
    state: dict = {"clock": float(now)}
    if ctx.variables:
        state["variables"] = {name: _plain(value) for name, value in ctx.variables.items()}
    levels = _levels(session)
    if levels:
        state["levels"] = levels
    arrivals = {}
    for name, block in session.model.blocks.items():
        if block.block.block_type != "CREATE":
            continue
        arrivals[name] = {"count": block.counter_arrivals}
        # A thinned (Schedule) arrival is memoryless and an order has its own release time:
        # only a drawn gap and a fixed schedule have a next time to hand over.
        if block.arrival_mode == "Interarrival" and block.next_at is not None \
                and block.next_at >= now:
            arrivals[name]["next"] = float(block.next_at)
    if arrivals:
        state["arrivals"] = arrivals
    resources = {}
    for name, resource in ctx.resource_manager.resources.items():
        failures = {fname: ({"next": float(t)} if phase == "up"
                            else {"down_until": None if t is None else float(t)})
                    for fname, (phase, t) in resource.failure_clock.items()}
        if failures:
            resources[name] = {"failures": failures}
        elif resource.down.level:
            raise _unsupported("a resource taken down by an earlier state", f"'{name}'")
    if resources:
        state["resources"] = resources
    cursors = {name: {"cursor": rset._cursor}
               for name, rset in ctx.resource_manager.sets.items() if rset.rule == "Cyclical"}
    if cursors:
        state["resource_sets"] = cursors
    setups = {name: {"setup": _plain(block._previous_key)}
              for name, block in session.model.blocks.items()
              if (getattr(block, "setup", "") or getattr(block, "setup_cost", ""))
              and block._previous_key is not None}
    for name, spec in setups.items():
        if isinstance(spec["setup"], bool) or not isinstance(spec["setup"], (str, int, float)):
            raise _unsupported(f"the setup of station '{name}'",
                               "its setup key is neither a text nor a number")
    if setups:              # D263: what each station is set up for; a changeover under
        state["blocks"] = setups        # way is refused with the job that is in it
    entities = _entities(session, tap)
    if entities:
        state["entities"] = entities
    try:
        _canonical(state)
    except (TypeError, ValueError):
        raise _unsupported("a value that is not plain data", "a variable or a level") from None
    return state


# ---------------------------------------------------------------- from a plant
# (kind, the model has it, the key of the state that speaks of it or None: no vocabulary)
_KINDS = (
    ("entities in blocks", lambda m: any(b.get("type", "").upper() in initial_state.HOLDING_TYPES
                                         for b in m.get("blocks") or []), "entities"),
    ("arrivals", lambda m: any(b.get("type", "").upper() == "CREATE"
                               for b in m.get("blocks") or []), "arrivals"),
    ("variables", lambda m: m.get("variables"), "variables"),
    ("resources and failures", lambda m: m.get("failures"), "resources"),
    ("levels", lambda m: m.get("levels"), "levels"),
    ("fleets and vehicles", lambda m: m.get("fleets"), None),
    ("chargers", lambda m: m.get("chargers"), None),
    ("agent populations", lambda m: m.get("populations"), None),
)


def observe(layout, base_dir=None, *, network=None, confine_to=None, feed=None,
            name: str | None = None):
    """(document, problems): the state a plant reports NOW, read through the model's own
    data bindings into `initial_state.*` (D236), with the caller's policy, connections and
    feed. The latest values only: no state is rebuilt from a history (that is 7.11).

    A binding that fails is its own problem and then nothing is written: half an
    observation is not a state. A kind the model has and the state says nothing about is
    `W_STATE_NOT_COVERED` and starts as the model declares it. `taken.stamp` is the oldest
    stamp among the sources read, None when one has none."""
    from nagasim import databind        # pandas: loaded only when a plant is read

    model = _model(layout)
    resolved, report = databind.resolve_bindings(model, base_dir, network=network,
                                                 confine_to=confine_to, feed=feed)
    mine = [r for r in report.records if str(r.target).startswith(initial_state.SECTION + ".")]
    problems = [r.problem for r in mine if r.problem is not None]
    stamps = [stamp_seconds(r.source_time) for r in mine]
    state, stamp = {}, None
    if not problems:
        state = resolved.get(initial_state.SECTION) or {}
        if stamps and None not in stamps:
            stamp = datetime.fromtimestamp(min(stamps), timezone.utc).isoformat(
                timespec="seconds")
    for kind, has, key in _KINDS:
        if has(model) and (key is None or not state.get(key)):
            problems.append(problem(
                "W_STATE_NOT_COVERED", f"the observed state says nothing about {kind}: "
                f"they start as the model declares them", f"/{initial_state.SECTION}",
                hint="bind an observed value into initial_state, or accept the model's start"))
    return document(model, state, name=name, source="observed", stamp=stamp), problems


def from_plant(layout, seen: dict, *, name: str | None = None):
    """(document, problems): the state of a plant's OBSERVED TIMELINE now (PHASE7 7.11,
    D258). *seen* is `plant.Mapper.snapshot()`: where each entity is and in which order,
    what holds each resource, the levels, and the model time `at` they were last told.
    Unlike `observe` it has a clock, and entities one by one.

    It carries what 7.10's vocabulary holds and no more. The plant does not say when a
    service ends, so one in progress is drawn afresh; a fleet, a vehicle or an agent starts
    as declared (`W_STATE_NOT_COVERED`); what the model cannot hold is refused by `start`
    with the section's own code. Units reported busy with nobody named in service: when ONE
    block seizes the resource its longest-waiting entities are the ones in service
    (`W_PLANT_SERVICE_INFERRED`); otherwise nothing is chosen and `start` refuses."""
    model = _model(layout)
    flat = schema.prepare(model)[0]
    blocks = {b.get("name"): b for b in flat.get("blocks") or []}
    seizes: dict[str, list] = {}
    for block_name, block in blocks.items():
        target = (block.get("params") or {}).get("resource_name")
        if target:
            seizes.setdefault(target, []).append(block_name)
    clock, problems = float(seen.get("at") or 0.0), []
    entities = [dict(entity) for entity in seen.get("entities") or []]
    for entity in entities:
        target = (blocks.get(entity["block"], {}).get("params") or {}).get("resource_name")
        entity["serving"] = bool(target) and target in entity["holds"]
    resources: dict[str, dict] = {}
    declared = {r.get("name") for r in flat.get("resources") or []}
    for rname, said in (seen.get("resources") or {}).items():
        if rname not in declared:
            continue                    # a fleet: no vocabulary (the warning below says so)
        at = seizes.get(rname, [])
        if at:
            resources[rname] = {"busy": said["busy"]}
            named = sum(e["serving"] for e in entities if e["block"] in at)
            waiting = [e for e in entities if e["block"] in at and not e["serving"]]
            # Only units NOBODY is named for are inferred. A job whose entity is elsewhere,
            # or seen nowhere, is the plant contradicting itself: nothing is chosen, `busy`
            # then differs from the entities in service and `start` refuses (E_INITIAL_BUSY).
            units = said.get("units", 0)
            if 0 < units <= len(waiting) and said["busy"] - named == units and len(at) == 1:
                for entity in waiting[:units]:
                    entity["serving"] = True
                problems.append(problem(
                    "W_PLANT_SERVICE_INFERRED", f"the plant reports {said['busy']} units of "
                    f"'{rname}' busy and names {named} jobs: the longest-waiting entities of "
                    f"'{at[0]}' are taken as in service", f"/{initial_state.SECTION}/entities",
                    hint="map the station's job (\"job\") to say which"))
        if said["down"]:
            failures = [f.get("name") for f in flat.get("failures") or []
                        if rname in (f.get("resources") or [])]
            if len(failures) == 1:
                resources.setdefault(rname, {})["failures"] = {failures[0]: {"down_until": None}}
            else:
                problems.append(problem(
                    "W_STATE_NOT_COVERED", f"resource '{rname}' is down and the model has "
                    f"{len(failures)} failures for it: it starts up",
                    f"/{initial_state.SECTION}/resources/{rname}",
                    hint="a resource with one failure starts down, its repair drawn afresh"))

    def entry(entity):
        out = {"block": entity["block"], "count": 1}
        if entity["e_type"]:
            out["e_type"] = entity["e_type"]
        if entity["serving"]:
            out["in_service"] = 1
        elif entity["since"] is not None and entity["since"] <= clock and str(
                blocks.get(entity["block"], {}).get("type", "")).upper() in _QUEUEING:
            out["since"] = entity["since"]
        return out
    # In service first: each must seize before a queue forms behind it (as `_entities`).
    state: dict = {"clock": clock}
    listed = [entry(e) for e in entities if e["serving"]] + [entry(e) for e in entities
                                                              if not e["serving"]]
    levels = {l.get("name") for l in flat.get("levels") or []}
    for key, value in (("entities", listed), ("resources", resources),
                       ("levels", {k: v for k, v in (seen.get("levels") or {}).items()
                                   if k in levels})):
        if value:
            state[key] = value
    for kind, has, key in _KINDS:
        if has(model) and (key is None or not state.get(key)):
            problems.append(problem(
                "W_STATE_NOT_COVERED", f"the observed timeline says nothing about {kind}: "
                f"they start as the model declares them", f"/{initial_state.SECTION}",
                hint="map a signal for it, or accept the model's start"))
    return document(model, state, name=name, source="observed", at=clock,
                    stamp=seen.get("stamp")), problems


# ---------------------------------------------------------------- into a run
def check(layout, doc, *, max_age: float | None = None, now: float | None = None) -> list:
    """The problems that refuse *doc* as a state of the model *layout*; [] when it may start.
    *max_age* (seconds) is the caller's: an observed state older than it is stale."""
    def refused(code, message, hint):
        return [problem(code, message, "/initial_state", hint=hint)]

    if not (isinstance(doc, dict) and doc.get("format") == FORMAT
            and isinstance(doc.get("state"), dict) and isinstance(doc.get("model"), dict)
            and isinstance(doc.get("hash"), str) and isinstance(doc["model"].get("hash"), str)):
        return refused("E_STATE_FORMAT", "this is not a state document of format 1",
                       "write one with `naga state take` or `naga state observe`")
    state = doc["state"]
    entities = state.get("entities")
    try:
        text = _canonical(state)
    except (TypeError, ValueError, RecursionError):
        return refused("E_STATE_FORMAT", "the state holds a value that is not plain data",
                       "numbers must be finite; no value may nest without end")
    counts = [e.get("count", 0) for e in entities if isinstance(e, dict)] \
        if isinstance(entities, list) else []
    if len(text) > MAX_BYTES or len(counts) > MAX_ENTITIES \
            or sum(c for c in counts if isinstance(c, int) and c > 0) > MAX_ENTITIES:
        return refused("E_STATE_SIZE", f"the state is over the bound of {MAX_BYTES} bytes or "
                       f"{MAX_ENTITIES} entities",
                       "a state this large is not one a run can start from")
    if doc["hash"] != _sha(_canonical([doc["model"]["hash"], state])):
        return refused("E_STATE_HASH", "the state does not match its own hash",
                       "it was edited after it was written; take or observe it again")
    mine = model_hash(layout)
    if doc["model"]["hash"] != mine:
        return refused("E_STATE_MODEL", f"the state was taken for model "
                       f"'{str(doc['model'].get('name', ''))[:80]}' ({doc['model']['hash'][:12]}), and "
                       f"this model is {mine[:12]}",
                       "a state belongs to the exact model file it was taken for; take it again")
    taken = doc.get("taken") if isinstance(doc.get("taken"), dict) else {}
    if max_age is not None and taken.get("source") == "observed":
        seen = stamp_seconds(taken.get("stamp"))
        age = None if seen is None else (time.time() if now is None else now) - seen
        if age is None or age > max_age:
            return refused("E_STATE_STALE", "the observed state has no time stamp" if age is None
                           else f"the observed state is {age:.0f} s old, over the {max_age:g} s "
                                f"asked for", "observe it again, or allow an older state")
    return []


def start(layout, doc, *, max_age: float | None = None, now: float | None = None,
          until: float | None = None):
    """(the model with *doc*'s state as its `initial_state`, []) or (None, the problems).
    The section's own validation then runs with the model's, as for any model.

    *until* is the end of the run the caller is about to make, as its run length: a twin's
    clock is the plant's and is soon past the length the model file was studied over."""
    problems = check(layout, doc, max_age=max_age, now=now)
    if problems:
        return None, problems
    model = _model(layout)
    model[initial_state.SECTION] = copy.deepcopy(doc["state"])
    if until is not None:
        model["simulation"] = dict(model.get("simulation") or {}, time_limit=float(until))
    # The state already holds what these observed: a later bind must not write over it.
    model["data_bindings"] = [b for b in model.get("data_bindings") or []
                              if not str(b.get("target", "")).startswith("initial_state.")]
    # A plant is where a state goes wrong: what the model cannot hold is refused here,
    # with the section's own code, before anything runs.
    _flat, found = schema.prepare(model)
    refused = [p for p in schema.fatal_problems(found)
               if str(p.path).startswith("/" + initial_state.SECTION)]
    return (None, refused) if refused else (model, [])


# ---------------------------------------------------------------- files and the CLI
def read(path) -> tuple:
    """(the document in the file *path*, or None; the problems of reading it). Bounded
    before it is parsed; what it holds is then judged by `check`."""
    try:
        if Path(path).stat().st_size > 2 * MAX_BYTES:
            return None, [problem("E_STATE_SIZE", f"the state file is over {2 * MAX_BYTES} bytes",
                                  "/initial_state", hint="a state holds at most 16 MiB")]
        return json.loads(Path(path).read_text(encoding="utf-8")), []
    except (OSError, ValueError, RecursionError) as exc:
        return None, [problem("E_STATE_FORMAT", f"the state file cannot be read "
                              f"({type(exc).__name__})", "/initial_state",
                              hint="write one with `naga state take` or `naga state observe`")]


def _report(problems) -> int:
    """Print each problem; 1 when one refuses, 0 for warnings only."""
    for found in problems:
        print(f"{found.code}: {found.message}")
    return 1 if any(found.severity == "error" for found in problems) else 0


def _write(doc: dict, out) -> None:
    text = json.dumps(doc, indent=1)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"state {doc['hash']} written to {out}")
    else:
        print(text)


def main(argv=None) -> int:
    """`naga state take | observe | check`."""
    import argparse

    from nagasim import netguard
    from nagasim._cli import setup_cli

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga state",
        description="A model's state as a document: taken from a run, observed from a plant "
                    "through the model's data bindings, and checked before a run starts "
                    "from it (naga run MODEL --state FILE). Read-only.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    take_ = verbs.add_parser("take", help="run the model to a time and write its state")
    take_.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    take_.add_argument("--at", type=float, required=True, metavar="T",
                       help="the model time, in the model's base time unit")
    take_.add_argument("--seed", type=int, default=0)
    take_.add_argument("--out", default=None, metavar="FILE", help="default: print it")
    observe_ = verbs.add_parser("observe", help="read the state a plant reports now, through "
                                               "the model's bindings into initial_state")
    observe_.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    observe_.add_argument("--connections", default=None, metavar="FILE",
                          help="your connections file, for a model with live sources")
    observe_.add_argument("--out", default=None, metavar="FILE", help="default: print it")
    check_ = verbs.add_parser("check", help="say whether a run of this model may start from "
                                           "this state; exit 1 with the reasons when not")
    check_.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    check_.add_argument("state", help="path to the state document")
    check_.add_argument("--max-age", type=float, default=None, metavar="SECONDS",
                        help="refuse an observed state older than this")
    args = parser.parse_args(argv)

    name = Path(args.model).stem
    if args.verb == "take":
        try:
            doc = take(args.model, args.at, args.seed, name=name)
        except StateError as exc:
            return _report([exc.problem])
        _write(doc, args.out)
        return 0
    if args.verb == "observe":
        policy = netguard.NetworkPolicy(connections=netguard.load_connections(args.connections)) \
            if args.connections else None
        doc, problems = observe(args.model, Path(args.model).parent, network=policy, name=name)
        failed = _report(problems)
        if not failed:
            _write(doc, args.out)
        return failed
    doc, problems = read(args.state)
    problems = problems or start(args.model, doc, max_age=args.max_age)[1]
    if not problems:
        print(f"state {doc['hash']} may start this model")
    return _report(problems)


if __name__ == "__main__":
    raise SystemExit(main())
