"""Run debugger tools (PHASE7 7.5, D194): one stepped run per model, kept on the
workspace, with stops, watches and an inspector.

The run is a `SimSession`; everything here reads it through `core/debugger.py` and
the one trace reducer (`Timeline`). It binds no data and opens no file or socket,
so neither tool takes a path, an endpoint or a permission.
"""
from __future__ import annotations

import math
import sys

from nagasim.core.debugger import ScopedStep, Stops, entity_view, note_point, watch_values
from nagasim.core.playback import Timeline, visible_name
from nagasim.core.session import SimSession
from nagasim.core.trace import TraceRecorder

from .core import ToolError, _budget, _document_args, _runnable_layout, tool, workspace

# An agent pays for every token of an answer: a stop on a busy block can hit hundreds
# of times in one step, a queue can hold thousands of entities and a batch arriving
# at a Policy DECIDE can leave as many decisions waiting.
MAX_HITS = 50
MAX_IDS = 50
MAX_PENDING = 50
# What a call may send, because stops and watches are kept and answered at every later
# call: a long list or a long text would cost tokens for the life of the run.
MAX_ENTRIES = 50
MAX_TEXT = 500
# A long call (max_seconds has no ceiling) hands its trace to the timeline in chunks of
# about this many events, so it holds a bounded number at once; the timeline is live.
FLUSH_EVENTS = 10_000


def _json_safe(value):
    """NaN and inf become null (they are not JSON), through dicts and lists."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _pending(session) -> dict:
    """The decisions waiting, oldest first, capped, with how many there are in all."""
    waiting = session.pending()
    return {"pending": waiting[:MAX_PENDING], "pending_total": len(waiting)}


def _edited(doc, run: dict) -> bool:
    """The model is no longer what the run was built from: an edit, or another file
    opened under the same name."""
    return doc.to_layout() != run["layout"]


def _debug_run_of(model: str) -> dict:
    run = workspace().debug.get(model)
    if run is None:
        raise ToolError("E_NO_DEBUG_RUN", f"model '{model}' has no debug run",
                        "call debug_run first")
    return run


def _refuse(message: str) -> ToolError:
    return ToolError("E_BAD_ARGUMENT", message,
                     f"stops and watches are lists of at most {MAX_ENTRIES} entries, each text "
                     f"at most {MAX_TEXT} characters; `to` is a finite number")


def _check_arguments(stops, watches, to) -> None:
    """Refuse a malformed argument before anything changes (a start, a restart, a
    decision, the old stops): a string would become one watch per letter, and a
    non-list would close the armed stops before it failed."""
    for name, items in (("stops", stops), ("watches", watches)):
        if items is None:
            continue
        if not isinstance(items, list):
            raise _refuse(f"{name} must be a list, not {type(items).__name__}")
        if len(items) > MAX_ENTRIES:
            raise _refuse(f"{name} has {len(items)} entries; at most {MAX_ENTRIES}")
        for item in items:
            if name == "watches" and not isinstance(item, str):
                raise _refuse("watches must be a list of texts")
            texts = item.values() if isinstance(item, dict) else [item]
            if any(isinstance(text, str) and len(text) > MAX_TEXT for text in texts):
                raise _refuse(f"{name} holds a text of more than {MAX_TEXT} characters")
    # Compared, not math.isfinite: an int too large for a float would raise there.
    if to is not None and not (isinstance(to, (int, float)) and not isinstance(to, bool)
                               and -sys.float_info.max <= to <= sys.float_info.max):
        raise _refuse("to must be a finite number")


def _arm(session, stops: list) -> Stops | None:
    try:
        return Stops(session, stops) if stops else None
    except ValueError as exc:       # ExpressionError is one
        raise ToolError("E_BAD_STOP", str(exc),
                        "a stop is {\"block\": a block or submodel instance name}, "
                        "{\"entity\": an entity id} (either may add \"when\": an expression "
                        "read with the entering entity) or {\"expr\": an expression}, none "
                        "with random draws; blocks: "
                        + ", ".join(session.model.blocks)) from None


_document_args("debug_run", model="Workspace name of the model to debug.",
               stops="Replaces the stops (a list of at most 50, each text at most 500 "
                     "characters): [{\"block\": name}] stops when an entity enters that block "
                     "(a submodel instance name: anything inside it), [{\"entity\": id}] when "
                     "that entity is created or moves, [{\"expr\": text}] when the expression "
                     "becomes true. A block or entity stop may add \"entity\" (that entity only) "
                     "and \"when\" (an expression read with the entering entity, e.g. "
                     "'entity.e_type == \"Red\"'). Omit to keep them, [] to clear.",
               watches="Replaces the watch expressions, read at every return (a list of at most "
                       "50 texts of at most 500 characters). Omit to keep, [] to clear.",
               to="Model time to run to at most (a finite number); omit for no time limit.",
               step="True: stop at the next event the trace shows (one entity move, one "
                    "resource change).",
               decisions="Answers to pending decisions: [{\"policy\", \"option\", \"request_id\"}] "
                         "(request_id optional: the policy's oldest). Applied after the checks of "
                         "the other arguments and after a start or `restart`, before the run moves.",
               restart="True: drop the run and start again from t = 0 on the model as it is "
                       "now, keeping the stops and watches unless new ones are given.",
               seed="Seed of the run; read only when the run starts or restarts.",
               scope="With `step`: a submodel instance (or a block); the step goes on to the "
                     "next event of a block inside it, so it stays where you are looking.",
               back="True: go back to where the previous call left the run, exactly (it is "
                    "replayed from the seed and the decisions made), and return without "
                    "moving. The replay runs from t = 0 within max_seconds; when it does not "
                    "fit, E_BUDGET and the run stays where it was. Not with `to`, `step`, "
                    "`scope`, `decisions` or `restart`.",
               max_seconds="Wall-clock budget for this call; the run stays where it got to. 0 "
                           "means no budget: the call runs until a stop, `to`, a decision or "
                           "the end.")


@tool()
def debug_run(model: str, stops: list | None = None, watches: list | None = None,
              to: float | None = None, step: bool = False, decisions: list | None = None,
              restart: bool = False, seed: int = 0, max_seconds: float = 10.0,
              back: bool = False, scope: str | None = None) -> dict:
    """Run one replication of a model under a debugger: start it, or continue it,
    until a stop, one step, a model time, a pending decision or the end.

    The run is kept between calls, one per model, so the usual loop is: set stops
    and watches, call again and again, and read `debug_inspect` whenever it
    returns. A stop fires on a change -- an entity entering a block, an expression
    going from false to true -- so calling again always moves on. Nothing here
    changes the results: a run that is stopped, stepped and inspected ends with the
    same numbers as `run_model` with one replication and the same seed.

    `back` undoes the previous call's move: the run is rebuilt where that call
    found it, by replaying the same seed and decisions, so nothing is approximated.

    `state` says why it returned: "back", "stop" (see `hits`), "step", "time" (reached
    `to`), "decision" (a Policy DECIDE waits: answer with `decisions`; the clock
    cannot move until you do), "budget" (max_seconds ran out; call again),
    "horizon" or "drained" (the run is over; `restart` to debug again). `hits`
    lists every stop that fired during the call and is read at every return, so
    it can be non-empty beside any state, not only "stop".

    A call is refused (E_BAD_ARGUMENT) before anything changes when `stops` or
    `watches` is not a list, has more than 50 entries or holds a text of more than
    500 characters, or `to` is not a finite number; a bad stop likewise
    (E_BAD_STOP). Then a start or `restart` happens (with the new stops and watches,
    if given), then `decisions` are applied, then on a continuing run the new stops
    and watches take effect, then the run moves. A refused
    decision answers with an error before the run moves: a continuing run keeps its
    stops and watches as they were, but the decisions listed before the refused one
    stay applied. A start or `restart` is not undone by a later error in the same
    call.

    Returns {"model", "t", "state", "hits": [{"stop": index, "block", "entity_id",
    "entity_type"} (an entity stop's "block" is the block entered) or {"stop": index,
    "expr"}] (the first 50) and "hits_total": how
    many fired, "watches": {text: value or "error: ..."} for the watches and the
    stop expressions, "pending": [{"id", "policy", "block", "t", "entity_id",
    "options"}] (the first 50) and "pending_total": how many wait, "model_edited":
    true when the model changed since this run began}.
    """
    _check_arguments(stops, watches, to)
    if back and (to is not None or step or decisions or restart or scope is not None):
        raise _refuse("back goes alone: not with to, step, scope, decisions or restart")
    if scope is not None and not (step and isinstance(scope, str) and len(scope) <= MAX_TEXT):
        raise _refuse("scope is a block or submodel instance name, given with step=True")
    ws = workspace()
    doc = ws.document(model)
    run = ws.debug.get(model)
    if back:
        return _step_back(model, doc, _debug_run_of(model), stops, watches, max_seconds)
    continuing = run is not None and not restart
    # Arming takes each expression stop's baseline anew, which loses a flip the last event
    # made and nothing has read yet: the same list sent again keeps the armed stops (as the
    # editor's worker does). Built before anything changes, so a bad stop refuses the call
    # whole (D194).
    changed = continuing and stops is not None and stops != run["stop_specs"]
    new_stops = _arm(run["session"], stops) if changed else None
    if not continuing:
        layout = _runnable_layout(doc, model, "call validate_model and fix the errors; a debug "
                                              "run already started is kept as it was")
        recorder = TraceRecorder(max_events=float("inf"))   # drained after every advance
        session = SimSession(layout, seed=seed, trace=recorder)
        # None keeps: a restart arms the previous run's stops and watches again.
        specs = (run["stop_specs"] if run else []) if stops is None else stops
        texts = (run["watches"] if run else []) if watches is None else watches
        fresh = {"layout": layout, "session": session, "recorder": recorder,
                 "timeline": Timeline([], live=True), "stops": _arm(session, specs),
                 "stop_specs": [dict(stop) for stop in specs],
                 "watches": [str(text) for text in texts],
                 "positions": [(session._steps, session.now, 0)]}     # `back` can reach t = 0
        fresh["timeline"].append(recorder.events)           # what building the run emitted
        recorder.events = []
        if run is not None and run["stops"] is not None:
            run["stops"].close()
        ws.debug[model] = run = fresh
    session, recorder = run["session"], run["recorder"]
    if scope is not None and not any(name == scope or name.startswith(scope + "/")
                                     for name in session.model.blocks):
        if new_stops is not None:
            new_stops.close()
        raise ToolError("E_UNKNOWN_BLOCK", f"no block or instance named '{scope}' in this "
                        "debug run", f"blocks: {', '.join(session.model.blocks) or '(none)'}")
    try:
        for answer in decisions or []:
            session.decide(answer["policy"], answer["option"], answer.get("request_id"))
    except Exception:
        if new_stops is not None:
            new_stops.close()           # leave the bus as it was found
        raise
    if continuing:
        if changed:
            if run["stops"] is not None:
                run["stops"].close()
            run["stops"], run["stop_specs"] = new_stops, [dict(stop) for stop in stops]
        if watches is not None:
            run["watches"] = [str(text) for text in watches]

    armed, budget, late = run["stops"], _budget(max_seconds), False
    scoped = ScopedStep(scope) if scope is not None else None

    def until() -> bool:
        nonlocal late
        # The budget first: a flip the last event made stays unread for the next call.
        if budget is not None and budget():
            late = True
            return True
        hit = armed() if armed is not None else False       # read even when stepping
        # #619: the newest events are read before they are handed over below.
        stepped = step and (scoped.seen(recorder.events) if scoped is not None
                            else bool(recorder.events))
        # A plain step is over as soon as the recorder holds an event; a scoped one
        # hands what it has read over, as a run does.
        if (scoped is not None or not step) and len(recorder.events) > FLUSH_EVENTS:
            run["timeline"].append(recorder.events)
            recorder.events = []
            if scoped is not None:
                scoped.emptied()
        return hit or stepped

    state = session.advance(to=to, until=until)
    events, recorder.events = recorder.events, []
    run["timeline"].append(events)
    hits = armed.take_hits() if armed is not None else []
    if state == "until":
        state = "stop" if hits else "budget" if late else "step"
    return _answer(model, doc, run, state, hits)


def _answer(model, doc, run, state, hits) -> dict:
    session = run["session"]
    # D598: where each call left the run, the targets of `back`; #621: a call that only
    # answered decisions is a point of its own, so `back` returns to the unanswered state.
    note_point(run.setdefault("positions", []), session, keep_edits=False)
    texts = run["watches"] + [stop["expr"] for stop in run["stop_specs"] if "expr" in stop]
    return _json_safe({"model": model, "t": session.now, "state": state, "hits": hits[:MAX_HITS],
                       "hits_total": len(hits), "watches": watch_values(session, texts),
                       **_pending(session), "model_edited": _edited(doc, run)})


class _OverBudget(Exception):
    pass


class _Replay(TraceRecorder):
    """The recorder of a `back` replay (#620): it hands its events to the timeline in
    chunks, as a forward call does, and stops the replay once the budget runs out."""

    def __init__(self, timeline, budget):
        super().__init__(max_events=float("inf"))
        self.timeline, self.budget = timeline, budget

    def __call__(self, name, **event):
        super().__call__(name, **event)
        if self.timeline is not None and len(self.events) > FLUSH_EVENTS:
            self.timeline.append(self.events)
            self.events = []
        if self.budget is not None and self.budget():
            raise _OverBudget


def _step_back(model, doc, run, stops, watches, max_seconds) -> dict:
    """`debug_run(back=True)`: rebuild the run where the previous call left it. The
    run is left as it was when the replay is refused or runs out of time."""
    positions = run.get("positions", [])
    if len(positions) < 2:
        raise _refuse("back: the run is at its start")
    specs = run["stop_specs"] if stops is None else stops
    old = run["session"]
    step, t, n = positions[-2]
    timeline = Timeline([], live=True)
    recorder = _Replay(timeline, _budget(max_seconds))
    try:
        session = SimSession.resumed(run["layout"], old.seed, old.game_log[:n], step, t,
                                     trace=recorder)
    except _OverBudget:
        raise ToolError("E_BUDGET", f"back replays the run from t = 0 to t = {t:g}, which "
                        f"takes longer than max_seconds", "call again with a larger "
                        "max_seconds, or 0 for no limit; the run is where it was") from None
    # The replay is done: from here the calls flush it themselves, after reading it.
    recorder.budget = recorder.timeline = None
    armed = _arm(session, specs)                    # a bad stop refuses before anything changes
    positions.pop()
    if run["stops"] is not None:
        run["stops"].close()
    timeline.append(recorder.events)
    recorder.events = []
    run.update(session=session, recorder=recorder, timeline=timeline, stops=armed,
               stop_specs=[dict(stop) for stop in specs])
    if watches is not None:
        run["watches"] = [str(text) for text in watches]
    return _answer(model, doc, run, "back", [])


_document_args("debug_inspect", model="Workspace name of a model with a debug run.",
               block="A block or submodel instance to look into; omit for the overview only.",
               entity="Id of an entity to read (from a hit, a queue or `pending`).")


@tool(read_only=True)
def debug_inspect(model: str, block: str | None = None, entity: int | None = None) -> dict:
    """Read the state of a model's debug run where it stands, without moving it.

    Call it after `debug_run` returns. With no argument it is the overview: how
    many entities sit in each block, every resource's state, every level's value
    and the decisions waiting. `block` adds that block's queue and entities;
    `entity` adds one live entity's attributes, the model's own included.

    Counts are per block of the top level: a block inside a submodel instance is
    counted with its instance, and `block` then answers for the instance (see the
    "name" it returns).

    Returns {"model", "t", "pending" (the first 50), "pending_total", "counts":
    {block: entities}, "resources": {name: {"state", "busy", "capacity"}},
    "levels": {name: value}, "model_edited": true when the model changed since the
    run began, in which case the run still answers for the model it was built
    from}; with `block`: "block": {"name", "count", "in_service", "queue": [entity
    ids waiting, oldest first], "entities": [ids]} (50 ids at most each); with
    `entity`: "entity": {"id", "type", "created", "attributes"}, or null once it
    is disposed or inside a batch.
    """
    run = _debug_run_of(model)
    session = run["session"]
    state = run["timeline"].state_at(session.now)
    out = {"model": model, "t": session.now, **_pending(session),
           "counts": state["counts"], "resources": state["resources"], "levels": state["levels"],
           "model_edited": _edited(workspace().document(model), run)}
    if block is not None:
        if not any(name == block or name.startswith(block + "/") for name in session.model.blocks):
            raise ToolError("E_UNKNOWN_BLOCK", f"no block named '{block}' in this debug run",
                            f"blocks: {', '.join(session.model.blocks) or '(none)'}")
        shown = visible_name(block)
        out["block"] = {
            "name": shown, "count": state["counts"].get(shown, 0),
            "in_service": state["in_service"].get(shown, 0),
            "queue": state["queues"].get(shown, [])[:MAX_IDS],
            "entities": [eid for eid, (where, _type) in state["entities"].items()
                         if where == shown][:MAX_IDS]}
    if entity is not None:
        out["entity"] = entity_view(session, entity)
    return _json_safe(out)
