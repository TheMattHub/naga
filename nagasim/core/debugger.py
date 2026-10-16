"""Run debugger: stops and readers over a `SimSession` (PHASE7 7.5, D189-D193).

Pure consumers, shared by the editor's game worker and the agent tools. Nothing
here steps the run or draws a random number: `Stops` is the predicate that
`SimSession.advance(until=)` asks between events, and the readers evaluate as
`SimSession.snapshot` does -- no rng, the levels peeked -- so a debugged run is
bit-identical to a plain one. Whoever owns the session calls these, on its thread.
"""
from __future__ import annotations

from nagasim.core.expressions import (FUNCTIONS, SD_MACROS, SD_VALUE_MACROS, Expression,
                                      ExpressionError, ExpressionScope, compile_expression,
                                      functions_with, name_sections)
from nagasim.core.sim_classes import Entity
from nagasim.core.submodels import SubmodelError, flatten
from nagasim.core.timeseries import peeking as _peeking

__all__ = ["ScopedStep", "Stops", "entity_view", "note_point", "readonly_expression",
           "watch_functions", "watch_names", "watch_values"]

_SAMPLERS = frozenset(functions_with("sampler"))
# RecursionError and MemoryError too: a text from a tool argument can nest just under the
# compile limit, or build a value too large to hold, and a reader reports, never raises.
_READ_ERRORS = (ExpressionError, TypeError, ValueError, ArithmeticError, RecursionError,
                MemoryError)
_SCALARS = (bool, int, float, str)
REPR_LIMIT = 80
TEXT_LIMIT = 200        # a text longer than this is cut when shown
MAX_BITS = 1024         # an int wider than this is not shown


def readonly_expression(text: str) -> Expression:
    """Compile a watch or a stop: the expression grammar, minus the functions that
    draw a random number (a reader has no stream and must not move one)."""
    expression = compile_expression(text)
    drawn = sorted(expression.calls & _SAMPLERS)
    if drawn:
        raise ExpressionError(f"{drawn[0]}() draws a random number; a watch or a stop only reads")
    return expression


def watch_functions() -> dict:
    """{name: call hint} of the functions worth offering for a watch or a stop: the
    registry, minus the samplers (refused above), the functions that read `self`
    (a watch has no agent in hand) and the system-dynamics macros (expanded only
    inside an SD field). `NQ("name")`: the first argument names a model record."""
    skipped = _SAMPLERS | set(functions_with("near", "grid", "link")) | set(SD_MACROS)         | set(SD_VALUE_MACROS)
    hints = {}
    for name in sorted(set(FUNCTIONS) - skipped, key=str.lower):
        _, _, low, high = FUNCTIONS[name]
        letters = ['"name"' if name_sections(name) else "a", *"bcdefgh"]
        text = ", ".join(letters[:low])
        if high is None:
            text += ", ..." if text else "..."
        else:
            text += "".join(f"[, {letter}]" for letter in letters[low:high])
        hints[name] = f"{name}({text})"
    return hints


_ALL_SECTIONS = tuple(dict.fromkeys(s for f in FUNCTIONS for s in name_sections(f)))


def watch_names(layout: dict, function: str | None = None) -> list:
    """[(name, section)] of the records the first argument of *function* may name
    in *layout*, in section order; every named record when the registry does not
    say what the function takes. Names are the flattened ones a run knows
    (`cell_1/Machine`)."""
    try:
        layout = flatten(layout)
    except SubmodelError:
        pass                    # a model that cannot be expanded still has its top level
    out = []
    for section in name_sections(function or "") or _ALL_SECTIONS:
        records = layout.get(section)
        for record in records if isinstance(records, list) else ():
            name = record.get("name") if isinstance(record, dict) else None
            if isinstance(name, str) and name:
                out.append((name, section))
    return out


def _plain(value):
    """A numpy scalar as the Python number it is; anything else unchanged."""
    return value.item() if getattr(value, "shape", None) == () else value


def _truth(expression: Expression, ctx, entity=None) -> bool:
    """A stop's reading, with *entity* in hand for a block stop's "when": a failed
    or NaN read is false."""
    try:
        value = expression.evaluate(ExpressionScope(entity=entity, ctx=ctx))
        return bool(value) and value == value
    except _READ_ERRORS:
        return False


def _inside(block: str, name: str) -> bool:
    """*block* (a flattened name) is *name*, or inside the instance *name*."""
    return block == name or block.startswith(name + "/")


def note_point(points: list, session, *, keep_edits: bool) -> None:
    """Add where *session* stands, (steps, t, game_log length), to *points*: the
    targets of a step back (D598). A point equal to the last is not repeated. One
    at the same (steps, t) with a longer log replaces the last when *keep_edits*
    (the editor: an edit made at a pause stays when stepping back to it), else it
    is a point of its own (an agent: `back` undoes the answers of its last call)."""
    here = (session._steps, session.now, len(session.game_log))
    if points and (points[-1] == here or (keep_edits and points[-1][:2] == here[:2])):
        points[-1] = here
    else:
        points.append(here)


class ScopedStep:
    """A step that ends at the next trace event of a block inside *scope* (a submodel
    instance, or one block): the editor's F11 and `debug_run(scope=)` share it.

    `seen(events)` reads the events added to *events* since the last call, each
    once; call it before handing the list over and emptying it, then `emptied()`."""

    def __init__(self, scope: str):
        self.scope = scope
        self._read = 0

    def _in(self, name) -> bool:
        return isinstance(name, str) and _inside(name, self.scope)

    def seen(self, events) -> bool:
        new, self._read = events[self._read:], len(events)
        return any(self._in(e.block) or self._in(e.target) for e in new)

    def emptied(self) -> None:
        self._read = 0


class Stops:
    """The stops of one session, reduced to the predicate `advance(until=)` takes (D191).

    A stop is data. {"block": name} fires when an entity enters that block: created
    in it, or moved into it -- at departure when the link has a transfer delay, the
    trace having no arrival event. A submodel instance's name means anything inside
    it, entered from outside. {"expr": text} fires when the expression goes from
    false to true, read before each event. Both fire on a change, so continuing from
    a stop always moves.

    A block stop may narrow (D597): "entity" (an id) fires for that entity only,
    "when" (an expression) only when it holds for the entering entity, read with
    that entity in hand (`entity.e_type == "Red"`, `entity.<attribute>`; a bare
    `type` is no name and never holds). {"entity": id} alone fires whenever that
    entity is created or moves, wherever it goes; its hit names the block entered.

    Raises ValueError for a stop that is none of these or names no block,
    ExpressionError (a ValueError) for an expression outside the grammar or one
    that samples."""

    _BLOCK_KEYS = frozenset(("block", "entity", "when"))

    def __init__(self, session, stops: list[dict] | None):
        self._ctx = session.ctx
        self._hits: list[dict] = []
        # (stop index, block name or None, entity id or None, "when" expression or None)
        self._blocks: list[tuple] = []
        self._exprs: list[list] = []                # [stop index, text, expression, last truth]
        for index, stop in enumerate(stops or []):
            keys = set(stop) if isinstance(stop, dict) else set()
            if not (keys == {"expr"} or (keys and keys <= self._BLOCK_KEYS
                                         and keys & {"block", "entity"})):
                raise ValueError(f"stop {index}: expected {{\"block\": name}}, "
                                 f"{{\"entity\": id}} (either with \"when\") or "
                                 f"{{\"expr\": text}}, got {_shown(stop)}")
            if keys != {"expr"}:
                name, entity, when = stop.get("block"), stop.get("entity"), stop.get("when")
                if "block" in stop and not (
                        isinstance(name, str)
                        and any(_inside(block, name) for block in session.model.blocks)):
                    raise ValueError(f"stop {index}: no block named '{_shown(name)}'")
                if "entity" in stop and (isinstance(entity, bool) or not isinstance(entity, int)):
                    raise ValueError(f"stop {index}: an entity is an id, got {_shown(entity)}")
                if "when" in stop:
                    if not isinstance(when, str):
                        raise ValueError(f"stop {index}: a condition is text, got {_shown(when)}")
                    try:
                        when = readonly_expression(when)
                    except ExpressionError as exc:
                        raise ExpressionError(f"stop {index}: {stop['when']}: {exc}") from None
                self._blocks.append((index, name, entity, when))
            else:
                text = stop["expr"]
                if not isinstance(text, str):       # str() would turn a list into a literal
                    raise ValueError(f"stop {index}: an expression is text, got {_shown(text)}")
                try:
                    self._exprs.append([index, text, readonly_expression(text), False])
                except ExpressionError as exc:
                    raise ExpressionError(f"stop {index}: {text}: {exc}") from None
        with _peeking(self._ctx):
            for entry in self._exprs:
                entry[3] = _truth(entry[2], self._ctx)      # first taken when armed
        # One listener, and only with a block stop: an expression stop costs the bus nothing.
        self._listening = bool(self._blocks)
        if self._listening:
            self._ctx.bus.subscribe(self._on_event)

    def _on_event(self, name, *, block=None, entity=None, target=None, **_):
        if name == "entity_created":
            source, entered = None, getattr(block, "name", None)
        elif name == "entity_moved":
            source, entered = getattr(block, "name", None), target
        else:
            return
        if entered is None or entity is None:
            return
        for index, stop, only, when in self._blocks:
            if stop is not None and not (
                    _inside(entered, stop) and not (source is not None and _inside(source, stop))):
                continue
            if only is not None and entity.id != only:
                continue
            if when is not None:
                with _peeking(self._ctx):
                    if not _truth(when, self._ctx, entity):
                        continue
            self._hits.append({"stop": index, "block": entered if stop is None else stop,
                               "entity_id": entity.id, "entity_type": entity.e_type})

    def __call__(self) -> bool:
        """The `until=` predicate: read every expression stop (no short circuit, so
        each keeps its own last value), then say whether a hit waits."""
        if self._exprs:
            with _peeking(self._ctx):
                for entry in self._exprs:
                    now = _truth(entry[2], self._ctx)
                    if now and not entry[3]:
                        self._hits.append({"stop": entry[0], "expr": entry[1]})
                    entry[3] = now
        return bool(self._hits)

    def take_hits(self) -> list[dict]:
        """The hits since the last call, oldest first; they are then forgotten."""
        hits, self._hits = self._hits, []
        return hits

    def close(self) -> None:
        """Leave the bus as it was found. Safe to call twice."""
        if self._listening:
            self._listening = False
            self._ctx.bus.unsubscribe(self._on_event)


def watch_values(session, texts: list[str]) -> dict[str, object]:
    """{text: value} for each watch: a number, a truth value or a text as the model
    has it now, else "error: ..." (outside the grammar, a sampler, a failed read).
    No entity is in hand, so `entity.<attr>` is such an error."""
    out: dict[str, object] = {}
    with _peeking(session.ctx):
        for text in texts:
            text = str(text)
            try:
                value = _plain(readonly_expression(text).evaluate(ExpressionScope(ctx=session.ctx)))
            except _READ_ERRORS as exc:
                out[text] = f"error: {exc}" if str(exc) else f"error: {type(exc).__name__}"
                continue
            out[text] = _bounded(value) if isinstance(value, _SCALARS) else \
                f"error: {type(value).__name__} is not a number, a truth value or a text"
    return out


def _bounded(value):
    """A scalar as an answer may carry it: a huge int is refused (its repr, and so its
    JSON, can raise past Python's digit limit) and a long text is cut, its length said."""
    if isinstance(value, int) and value.bit_length() > MAX_BITS:
        return "error: number too large to show"
    if isinstance(value, str) and len(value) > TEXT_LIMIT:
        return f"{value[:TEXT_LIMIT]}...[cut, {len(value)} characters]"
    return value


def _shown(value):
    """An attribute for display: a scalar as it is, an entity as its id, else a short repr."""
    value = _plain(value)
    if value is None or isinstance(value, _SCALARS):
        return _bounded(value)
    if isinstance(value, Entity):
        return value.id
    try:
        text = repr(value)
    except _READ_ERRORS:        # a huge int inside, or nesting past the stack
        return f"error: {type(value).__name__} cannot be shown"
    return text if len(text) <= REPR_LIMIT else text[:REPR_LIMIT - 3] + "..."


def entity_view(session, entity_id: int) -> dict | None:
    """{"id", "type", "created", "attributes"} of a live entity -- found as
    `SimSession.finish` finds one -- or None once it is disposed or inside a batch.
    `attributes` are its public ones, the model's own included."""
    if isinstance(entity_id, bool) or not isinstance(entity_id, int):
        return None         # True and 1.0 would find entity 1, a list would raise
    entity = session.ctx.live_entities._items.get(entity_id)
    if entity is None:
        return None
    return {"id": entity.id, "type": entity.e_type, "created": entity.creation_time,
            "attributes": {name: _shown(value) for name, value in vars(entity).items()
                           if not name.startswith("_")}}
