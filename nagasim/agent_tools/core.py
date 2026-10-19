"""Tool registry, workspace and the shared helpers every other `agent_tools`
module builds on (F2: split from the former single-file `agent_tools`).

Every `@tool` in this package pushes its `ToolSpec` into `TOOLS` here, and
every mutating tool goes through `_open`/`_edit_result` here, so this module
is the one every sibling module imports from. Nothing in this package imports
another sibling directly -- only `.core` -- which is what keeps the import
graph a star instead of a tangle.

See `nagasim/agent_tools/__init__.py` for the full design rules and the
public surface.
"""
from __future__ import annotations

import difflib
import inspect
import json
import os
import re
import threading
import time
import types
import typing
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nagasim import experiments, mdlimport, netguard, sdimport
from nagasim.core import schema as schema_mod
from nagasim.core.agents import PYTHON_CONSENT, PythonRefused
from nagasim.core.document import ModelDocument
from nagasim.core.stats import ci95

# Rows/problems a single answer may carry. An agent pays for every token of a
# tool result, so the caps are deliberately tight; `get_kpi_table` pages.
MAX_ROWS = 200
MAX_PROBLEMS = 50
# A page is capped in BYTES as well as rows: an agent pays for characters, and
# a wide frame (entity_log) blows a row budget long before a narrow one does.
MAX_PAGE_BYTES = 16_000
_RESOURCE_COLUMNS = ("Usage % (snapshot)", "Queue Length Avg", "Queue Length Max",
                     "Time in Queue Avg", "Total Seizes")


class ToolError(Exception):
    """A refusal an agent can act on: a code, a message and a concrete hint."""

    def __init__(self, code: str, message: str, hint: str | None = None,
                 did_you_mean: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.did_you_mean = did_you_mean

    def as_dict(self) -> dict:
        error = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.did_you_mean:
            error["did_you_mean"] = self.did_you_mean
        return {"error": error}


def _close(word, choices) -> list[str]:
    return difflib.get_close_matches(str(word), list(choices), n=3, cutoff=0.5)


def unknown_tool_error(name, choices=None) -> ToolError:
    """E_UNKNOWN_TOOL with the closest names among `choices` (default: every registered tool;
    the MCP server passes only the tools it exposes, so a hidden tool is never suggested)."""
    choices = TOOLS if choices is None else choices
    close = _close(name, choices)
    return ToolError("E_UNKNOWN_TOOL", f"no tool named '{name}'",
                     f"did you mean: {', '.join(close)}?" if close
                     else f"use one of: {', '.join(sorted(choices))}", close)


# ---------------------------------------------------------------- workspace
class Workspace:
    """Named in-memory models plus the last results of each, under one root.

    The root is the only directory `open_model`/`save_model`/`export`/
    `fit_distribution` and a model's data bindings may touch; a path that
    resolves outside it -- through `..`, an absolute path, a UNC share or a
    symlink -- is refused (`E_PATH_OUTSIDE_ROOT`), which is what keeps an agent
    from reading or writing the rest of the filesystem.

    `network` is the outbound permission the HUMAN gave at server start (the
    server's --allow-network flag). It is deliberately a property of the
    workspace and not an argument of any tool: a model under prompt injection
    must not be able to ask for network access and get it.

    `twin` is an optional session object that a product's tools read; the
    engine never looks inside it. Like `network`, it is chosen once by the
    human at start-up, never by a tool argument, and is None when nothing was
    configured. `twin_dir` is the result directory of a twin the human started
    (`naga twin run --out DIR`), on the same rule: the two twin tools read the
    result in it and it need not be under the root.
    """

    def __init__(self, root: str | Path | None = None, network=None, twin=None, twin_dir=None):
        self.root = Path(root or Path.cwd()).resolve()
        self.network = network or netguard.NetworkPolicy()
        self.twin = twin
        self.twin_dir = None if twin_dir is None else Path(twin_dir)
        self.models: dict[str, ModelDocument] = {}
        self.results: dict[str, dict] = {}
        self.debug: dict[str, dict] = {}    # model name -> its debug run (`debug.py`, D194)

    def resolve(self, path: str | Path, *, must_exist: bool = False) -> Path:
        try:
            full = netguard.confine_path(path, self.root)
        except netguard.PathRefused as exc:
            raise ToolError("E_PATH_OUTSIDE_ROOT", exc.message, exc.hint) from None
        if must_exist and not full.exists():
            raise ToolError("E_FILE_NOT_FOUND", f"no such file: {path}",
                            "call list_models, or give a path relative to the workspace root")
        return full

    def document(self, name: str) -> ModelDocument:
        doc = self.models.get(name)
        if doc is None:
            raise ToolError("E_UNKNOWN_MODEL", f"no model named '{name}' in the workspace",
                            f"open or create it first; loaded: "
                            f"{', '.join(sorted(self.models)) or '(none)'}")
        return doc


_WORKSPACE = Workspace()


def use_workspace(ws: Workspace) -> Workspace:
    """Install *ws* as the process-wide workspace (the server does this at
    start-up with its --root; tests do it per test)."""
    global _WORKSPACE
    _WORKSPACE = ws
    return ws


def workspace() -> Workspace:
    return _WORKSPACE


# State of the scoped drop below: windows open right now (any thread) and the value the first
# one removed. The consent comes back only when the last window closes, so overlapping windows
# never restore it under each other's feet.
_consent_lock = threading.Lock()
_consent_windows = 0
_consent_saved: str | None = None


def drop_python_consent() -> None:
    """Agent surfaces never run a model's Python (D186): forget the caller's consent for this
    process and for every worker it spawns from now on. Also cancels the restore of any scoped
    drop that is open, so a permanent drop is never undone."""
    global _consent_saved
    with _consent_lock:
        os.environ.pop(PYTHON_CONSENT, None)
        _consent_saved = None


@contextmanager
def python_consent_dropped():
    """The same for the length of one call, for a surface that shares its process with a
    person (the editor's assistant). Re-entrant and thread-safe: the consent returns when the
    last open window exits, never before.
    ponytail: a manual run started on another thread inside a window is refused once and works
    on retry; a per-run flag would have to travel to every run entry point's workers. A worker
    pool created BEFORE a window, or a fork server started before it (the Linux default start
    method from Python 3.14), keeps the older environment; today every tool runs with one
    worker, and `naga mcp` drops before any process exists."""
    global _consent_windows, _consent_saved
    with _consent_lock:
        # Popped on EVERY entry: a variable written while an earlier window is open must not
        # survive into this one. A popped value is the one to put back at the last exit.
        popped = os.environ.pop(PYTHON_CONSENT, None)
        if popped is not None:
            _consent_saved = popped
        _consent_windows += 1
    try:
        yield
    finally:
        with _consent_lock:
            _consent_windows -= 1
            if _consent_windows == 0 and _consent_saved is not None:
                os.environ[PYTHON_CONSENT] = _consent_saved
                _consent_saved = None


# ---------------------------------------------------------------- tool registry
@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: typing.Callable       # the safe wrapper: returns a dict, never raises
    description: str          # the docstring, written for an LLM
    input_schema: dict        # JSON Schema derived from the signature
    read_only: bool
    origin: str = ""          # the module that defined the tool; never part of tool_schemas()


TOOLS: dict[str, ToolSpec] = {}

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean",
               list: "array", dict: "object"}


def _type_schema(annotation) -> dict:
    """One parameter annotation as a JSON Schema fragment. Only the shapes the
    tools below actually use; anything unknown accepts any value."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) != 1:
            return {}
        inner = _type_schema(args[0])
        kind = inner.get("type")
        return {**inner, "type": [kind, "null"]} if isinstance(kind, str) else inner
    if origin in (list, tuple):
        return {"type": "array"}
    if origin is dict:
        return {"type": "object"}
    return {"type": _JSON_TYPES[annotation]} if annotation in _JSON_TYPES else {}


def _input_schema(fn) -> dict:
    hints = typing.get_type_hints(fn)
    properties, required = {}, []
    for pname, param in inspect.signature(fn).parameters.items():
        prop = _type_schema(hints.get(pname))
        doc_line = _ARG_DOCS.get((fn.__name__, pname))
        if doc_line:
            prop["description"] = doc_line
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        elif param.default is not None and not isinstance(param.default, (list, dict)):
            prop["default"] = param.default
        properties[pname] = prop
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


# Per-argument one-liners, kept beside the tools rather than parsed out of the
# docstrings: an agent reads them in the input schema, a human in the docstring.
_ARG_DOCS: dict[tuple[str, str], str] = {}


def _document_args(tool_name, /, **docs) -> None:
    for arg, text in docs.items():
        _ARG_DOCS[(tool_name, arg)] = text


_JSON_KINDS = {"object": dict, "array": list}


def _parse_json_arguments(schema: dict, kwargs: dict) -> None:
    """Small local models send an object/array argument as a JSON string (#408). Parse it
    when the schema declares object/array, so the call works and a bad string is blamed
    on the argument, not on its content. json.loads only; nothing else is coerced."""
    for key, value in kwargs.items():
        kinds = (schema["properties"].get(key) or {}).get("type", [])
        kind = next((k for k in ([kinds] if isinstance(kinds, str) else kinds) if k in _JSON_KINDS), None)
        if kind is None or not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if not isinstance(parsed, _JSON_KINDS[kind]):
            raise ToolError("E_BAD_ARGUMENT",
                            f"`{key}` must be an {kind}, not a string",
                            f"send `{key}` as a JSON {kind}, not as text")
        kwargs[key] = parsed


def _require_arguments(schema: dict, kwargs: dict) -> None:
    """A required argument left out is named, and said to be inside an object argument
    when it was put there (#409: `type` sent inside `params`); Python's own TypeError
    text is nothing a model can act on."""
    known = schema["properties"]
    for key in kwargs:
        if key not in known and not schema.get("additionalProperties"):
            close = _close(key, known)
            raise ToolError("E_BAD_ARGUMENT", f"unknown argument `{key}`",
                            f"did you mean: {', '.join(close)}?" if close
                            else f"this tool takes: {', '.join(known) or 'no arguments'}", close)
    for key in schema["required"]:
        if key in kwargs:
            continue
        inside = next((name for name, value in kwargs.items()
                       if isinstance(value, dict) and key in value), None)
        raise ToolError("E_BAD_ARGUMENT", f"`{key}` is required",
                        f"it was sent inside `{inside}`: put `{key}` beside `{inside}`, not in it"
                        if inside else "see this tool's input schema")


def tool(read_only: bool = False):
    """Register a function as a tool: wrap it so it never raises, derive its
    input schema from the signature, keep the docstring as the description."""

    def decorate(fn):
        schema = _input_schema(fn)

        def safe(**kwargs):
            try:
                _parse_json_arguments(schema, kwargs)
                _require_arguments(schema, kwargs)
                return fn(**kwargs)
            except ToolError as exc:
                return exc.as_dict()
            except PythonRefused:
                return ToolError(
                    "E_AGENT_PYTHON_REFUSED",
                    "this model carries Python behaviour, which agent tools never run",
                    "ask the person to run it themselves with `naga run`; you can still edit "
                    "and validate the model").as_dict()
            except schema_mod.ModelValidationError as exc:
                return ToolError("E_INVALID_MODEL", "; ".join(exc.problems),
                                 "call validate_model for the structured problems").as_dict()
            except (ValueError, KeyError, TypeError, OSError) as exc:
                return ToolError(f"E_{type(exc).__name__.upper()}", str(exc),
                                 "check the arguments against this tool's input schema").as_dict()

        safe.__name__ = fn.__name__
        safe.__doc__ = fn.__doc__
        safe.__signature__ = inspect.signature(fn)
        safe.__annotations__ = dict(getattr(fn, "__annotations__", {}))
        TOOLS[fn.__name__] = ToolSpec(name=fn.__name__, fn=safe,
                                      description=inspect.getdoc(fn) or "",
                                      input_schema=schema, read_only=read_only,
                                      origin=fn.__module__)
        return safe

    return decorate


def call(tool_name, /, **kwargs) -> dict:
    """Invoke a tool by name; unknown names answer with a structured error.
    Positional-only first argument, so a tool taking its own `name` argument
    (new_model, ...) is not shadowed."""
    spec = TOOLS.get(tool_name)
    if spec is None:
        return unknown_tool_error(tool_name).as_dict()
    return spec.fn(**kwargs)


def tool_schemas() -> list[dict]:
    """Every tool as {name, description, input_schema, read_only} -- what a
    non-MCP caller needs to build its own tool definitions."""
    return [{"name": s.name, "description": s.description, "input_schema": s.input_schema,
             "read_only": s.read_only} for s in TOOLS.values()]


# ---------------------------------------------------------------- helpers
def _problems(doc: ModelDocument) -> list[schema_mod.Problem]:
    return schema_mod.validate_model_structured(doc.to_layout())


def _open(model: str) -> tuple[ModelDocument, list]:
    """The document a mutating tool edits and its problems before the edit --
    the first line of every such tool (`_edit_result` reports what changed)."""
    doc = _WORKSPACE.document(model)
    return doc, _problems(doc)


def _problem_dicts(problems, limit: int = MAX_PROBLEMS) -> list[dict]:
    return [p.as_dict() for p in problems[:limit]]


def _first_sentence(text: str, limit: int = 0) -> str:
    """The first sentence (an "e.g." does not end one), cut at *limit* characters."""
    text = " ".join(str(text or "").split())
    found = re.search(r"(?<!e\.g)(?<!i\.e)\. ", text)
    text = text if found is None else text[:found.start() + 1]
    return text if not limit or len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def _brief_problems(problems) -> list[dict]:
    """Problems as an edit answer carries them: code, path, message and hint. The severity
    is the code's prefix and the block is in the path, so neither is repeated; an agent
    reads every byte of every answer again on each later turn."""
    return [{"code": p.code, "path": p.path, "message": p.message,
             **({"hint": p.hint} if p.hint else {})} for p in problems]


def _edit_result(doc: ModelDocument, before: list, extra: dict | None = None) -> dict:
    """The answer every mutating tool gives: what the edit broke, what it
    fixed, and where the model stands now."""
    after = _problems(doc)
    before_keys = {p.message for p in before}
    after_keys = {p.message for p in after}
    out = {
        "ok": True,
        "introduced": _brief_problems([p for p in after if p.message not in before_keys][:MAX_PROBLEMS]),
        "fixed": [p.message for p in before if p.message not in after_keys][:MAX_PROBLEMS],
        "errors": sum(1 for p in after if p.severity == "error"),
        "warnings": sum(1 for p in after if p.severity == "warning"),
    }
    out.update(extra or {})
    return out


def _runnable_layout(doc: ModelDocument, model: str,
                     hint: str = "call validate_model and fix the errors before running") -> dict:
    """The document's layout, or E_INVALID_MODEL when it has validation errors: the
    first check of `run_model`, `run_reality_checks`, `run_loop_dominance` and
    `debug_run`."""
    layout = doc.to_layout()
    errors = [p for p in schema_mod.validate_model_structured(layout) if p.severity == "error"]
    if errors:
        raise ToolError("E_INVALID_MODEL",
                        f"model '{model}' has {len(errors)} validation error(s); "
                        f"first: {errors[0].message}", hint)
    return layout


def _budget(max_seconds: float | None):
    """A `should_stop` callable for a time budget, or None for no budget."""
    if not max_seconds or max_seconds <= 0:
        return None
    deadline = time.monotonic() + float(max_seconds)
    return lambda: time.monotonic() > deadline


def _finite(value) -> float | None:
    """A JSON-safe number: NaN and inf become null rather than invalid JSON."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


# What else the digest carries, when the model has it: the numbers a question about entity
# types, levels, leaving customers or agents is usually after, so answering it takes no
# get_kpi_table call (the LLM study's priority, tank, car park and repair tasks).
_DIGEST_EXTRA = {"entity": ("entities", ("Avg Time", "Total Completed")),
                 "level": ("levels", ("Average", "Final", "Time Empty %", "Time Full %")),
                 "queue": ("queues", ("Balked", "Reneged")),
                 "agent": ("agents", ("Avg Count",))}


def _digest(results) -> dict:
    """A run's KPI digest: every system KPI with its CI half-width, a compact
    per-resource row, the user statistics and, when the model has them, a few
    columns per entity type, level, queue behaviour and agent state. Never the
    frames themselves."""
    if results is None:
        return {"replications": 0, "system": {}, "resources": {}, "user_stats": {}}
    flat = experiments.flatten_kpis(results)
    runs = sorted(flat["Run"].unique().tolist()) if not flat.empty else []
    system: dict[str, dict] = {}
    resources: dict[str, dict] = {}
    user_stats: dict[str, dict] = {}
    extra: dict[str, dict] = {}
    for key, group in flat.groupby("KPI"):
        stat = ci95(group["Value"].to_numpy(dtype=float))
        entry = {"mean": _finite(stat["mean"]),
                 "ci_half_width": _finite(stat["ub"] - stat["mean"])}
        section, _, rest = key.partition("/")
        name, _, column = rest.rpartition("/")
        if section == "system":
            system[rest] = entry
        elif section == "user":
            user_stats[rest] = entry
        elif section == "resource":
            if column in _RESOURCE_COLUMNS:
                resources.setdefault(name, {})[column] = entry
        elif section in _DIGEST_EXTRA and column in _DIGEST_EXTRA[section][1]:
            extra.setdefault(_DIGEST_EXTRA[section][0], {}).setdefault(name, {})[column] = entry
    if len(extra.get("entities", {})) < 2:      # one type: the system KPIs already say it
        extra.pop("entities", None)
    return {"replications": len(runs), "system": system, "resources": resources,
            "user_stats": user_stats, **extra}


def _rows(frame, offset: int, limit: int) -> dict:
    """One results frame as paged, JSON-safe rows, under both caps."""
    total = int(len(frame))
    limit = max(1, min(int(limit), MAX_ROWS))
    offset = max(0, int(offset))
    window = frame.iloc[offset:offset + limit]
    records = json.loads(window.to_json(orient="records", date_format="iso"))
    while len(records) > 1 and len(json.dumps(records)) > MAX_PAGE_BYTES:
        records = records[: len(records) // 2]
    end = offset + len(records)
    return {"total": total, "offset": offset, "count": len(records), "rows": records,
            "has_more": end < total, "next_offset": end if end < total else None}


def _records(frame, cols: dict) -> list[dict]:
    """A results frame as a list of renamed dicts: `cols` maps the output key
    to either a column name (copied as-is) or a `(column, transform)` pair
    (e.g. `("Mean", _finite)`). Slice/order *frame* before calling this --
    it does not page or cap rows itself, since callers already do (`.head()`,
    `.tail()`) in whatever shape the tool needs."""
    out = []
    for row in frame.to_dict(orient="records"):
        rec = {}
        for key, spec in cols.items():
            column, transform = spec if isinstance(spec, tuple) else (spec, None)
            rec[key] = row[column] if transform is None else transform(row[column])
        out.append(rec)
    return out


def _results_of(name: str):
    results = _WORKSPACE.results.get(name)
    if results is None:
        raise ToolError("E_NO_RESULTS", f"model '{name}' has not been run in this session",
                        "call run_model first")
    return results


def _upsert(entries: list, entry: dict) -> list:
    """Replace the entry with the same "name", or append it."""
    out = [dict(e) for e in entries if e.get("name") != entry["name"]]
    out.append(entry)
    return out


def _duration(spec: dict | None, what: str) -> dict:
    if not isinstance(spec, dict) or not spec.get("distribution"):
        raise ToolError("E_BAD_DURATION", f"{what} must be a duration object",
                        "e.g. {\"distribution\": \"Exponential\", \"dist_params\": {\"rate\": 0.5}} "
                        "-- call list_distributions for the catalogue")
    return spec


# ============================================================ workspace tools
_document_args("new_model", name="Workspace name for the new model (your handle for it later).")


@tool()
def new_model(name: str) -> dict:
    """Create an empty model in the workspace under *name*.

    This is the starting point for building a model from a description: create
    it, add blocks, connect them, then validate. Nothing is written to disk
    until you call `save_model`. An empty model validates and runs, and
    answers nothing: the first problems appear with the first block.

    Returns the usual edit answer: {"ok", "introduced", "fixed", "errors",
    "warnings", "model"}.
    """
    if name in _WORKSPACE.models:
        raise ToolError("E_MODEL_EXISTS", f"a model named '{name}' is already open",
                        "pick another name, or edit the existing one")
    doc = ModelDocument()
    _WORKSPACE.models[name] = doc
    return _edit_result(doc, [], {"model": name})


_document_args("open_model",
               path="Model .json path, relative to the workspace root.",
               name="Workspace name to load it under; defaults to the file stem.")


@tool()
def open_model(path: str, name: str | None = None) -> dict:
    """Load a model file from the workspace root into the workspace.

    Use it to inspect, modify or run an existing model, including the bundled
    examples once they have been copied into the root. Older schema versions
    migrate on load, so what you get back is always the current format.

    Returns {"ok", "model", "path", "blocks", "errors", "warnings", "problems"}.
    """
    full = _WORKSPACE.resolve(path, must_exist=True)
    doc = ModelDocument.load(full)
    handle = name or full.stem
    _WORKSPACE.models[handle] = doc
    problems = _problems(doc)
    return {"ok": True, "model": handle, "path": str(full), "blocks": len(doc.blocks),
            "errors": sum(1 for p in problems if p.severity == "error"),
            "warnings": sum(1 for p in problems if p.severity == "warning"),
            "problems": _problem_dicts(problems)}


_document_args("import_xmile",
               path="XMILE (.xmile/.stmx) file, relative to the workspace root.",
               name="Workspace name to load it under; defaults to the file stem.")


@tool()
def import_xmile(path: str, name: str | None = None) -> dict:
    """Translate an XMILE (Stella/iThink) system-dynamics file into a new model.

    Reads stocks, flows, auxiliaries, graphical functions (as lookup tables)
    and dimensions (as subscripts) with a small expression translator, into
    the workspace under *name*, exactly like `open_model` does for a model
    JSON file. The file must be under the workspace root -- a path that
    escapes it is refused, and there is no argument that widens that.

    A construct this cannot translate (a module, a macro, a conveyor/queue
    stock, a dimension mapping, an unknown function or name) is reported in
    `import_problems` (code `E_SDIMPORT_UNSUPPORTED`) and left out of the model rather than guessed at; an approximation (a bare
    DELAY, a non-euler/rk4 method, ...) is `W_SDIMPORT_APPROX`. Vensim `.mdl`
    is not supported.

    Returns {"ok", "model", "path", "import_problems": [{"code", "severity",
    "message", "path", "hint"}], "errors", "warnings", "problems"} -- the last
    three are the usual model-validation answer, over what DID translate.
    """
    full = _WORKSPACE.resolve(path, must_exist=True)
    try:
        layout, import_problems = sdimport.load_xmile(full)
    except sdimport.XMILEError as exc:
        raise ToolError(exc.code, str(exc),
                        "an XMILE file with a DOCTYPE/ENTITY or over 20 MB is refused outright; "
                        "fix the file and try again") from None
    handle = name or full.stem
    if handle in _WORKSPACE.models:
        raise ToolError("E_MODEL_EXISTS", f"a model named '{handle}' is already open",
                        "pick another name, or edit the existing one")
    doc = ModelDocument(layout)
    _WORKSPACE.models[handle] = doc
    problems = _problems(doc)
    return {"ok": True, "model": handle, "path": str(full),
            "import_problems": _problem_dicts(import_problems),
            "errors": sum(1 for p in problems if p.severity == "error"),
            "warnings": sum(1 for p in problems if p.severity == "warning"),
            "problems": _problem_dicts(problems)}


_document_args("import_mdl",
               path="Vensim (.mdl) file, relative to the workspace root.",
               name="Workspace name to load it under; defaults to the file stem.")


@tool()
def import_mdl(path: str, name: str | None = None) -> dict:
    """Translate a Vensim system-dynamics file into a new model.

    Reads equations, INTEG stocks, an inline `WITH LOOKUP` (as a lookup table) and
    subscript ranges with a small stdlib reader (no `pysd`), into the workspace under
    *name*, exactly like `open_model` does for a model JSON file. The file must be
    under the workspace root -- a path that escapes it is refused, and there is no
    argument that widens that.

    A construct this cannot translate (a `:MACRO:`, `GET XLS/DIRECT ...` external
    data, a variable name with a space, an unknown function or name, an INTEG rate
    that is not a plain flow name or sum/difference of flow names, a subscript
    equivalence `<->`) is reported in `import_problems` (code `E_SDIMPORT_UNSUPPORTED`) and
    left out of the model rather than guessed at.

    Returns {"ok", "model", "path", "import_problems": [{"code", "severity",
    "message", "path", "hint"}], "errors", "warnings", "problems"} -- the last
    three are the usual model-validation answer, over what DID translate.
    """
    full = _WORKSPACE.resolve(path, must_exist=True)
    try:
        layout, import_problems = mdlimport.load_mdl(full)
    except mdlimport.MDLError as exc:
        raise ToolError(exc.code, str(exc),
                        "a .mdl file that is not decodable text or over 20 MB is refused "
                        "outright; fix the file and try again") from None
    handle = name or full.stem
    if handle in _WORKSPACE.models:
        raise ToolError("E_MODEL_EXISTS", f"a model named '{handle}' is already open",
                        "pick another name, or edit the existing one")
    doc = ModelDocument(layout)
    _WORKSPACE.models[handle] = doc
    problems = _problems(doc)
    return {"ok": True, "model": handle, "path": str(full),
            "import_problems": _problem_dicts(import_problems),
            "errors": sum(1 for p in problems if p.severity == "error"),
            "warnings": sum(1 for p in problems if p.severity == "warning"),
            "problems": _problem_dicts(problems)}


_document_args("import_event_log",
               path="Event log (CSV, or .xes), relative to the workspace root.",
               name="Workspace name to open the draft under; defaults to the file stem.",
               routing="\"auto\" (a test on the log picks), \"markov\" (chances to the next "
                       "activity) or \"variants\" (one routing per observed sequence).",
               time_unit="The draft's time unit: seconds, minutes, hours or days. A numeric "
                         "timestamp is taken as already in it.",
               idle_waits="true: each activity that waits long at mostly idle resources gets "
                          "a DELAY before it, fitted to its waits (a modelled wait, not a "
                          "queue); false (default): no such delay.")


@tool()
def import_event_log(path: str, name: str | None = None, routing: str = "auto",
                   time_unit: str = "minutes", idle_waits: bool = False) -> dict:
    """Draft a model from an event log: what happened to each case, activity by activity.

    The log needs a case id, an activity and a timestamp per row (CSV), or XES traces;
    lifecycle (schedule/start/complete) and resource columns make the draft better. The
    draft has one CREATE fitted to the case arrivals, one block per activity with its fitted
    time, routing from the observed transitions and resources from the resource column,
    and opens in the workspace under *name* like `open_model`. Nothing is written to disk.

    It is a first draft: read `assumptions` (what the log could not tell, such as shifts or
    the wait before a case's first activity) and `dropped` before trusting its numbers.
    The file must be under the workspace root; a log over a size ceiling, or an XES file
    with a DOCTYPE, is refused outright, and there is no argument that changes either.

    Returns {"ok", "model", "routing", "activities": [{"block", "original", "count",
    "lifecycle", "distribution", "mean", "mean_wait", "resource"}], "resources",
    "activities_total", "resources_total", "assumptions", "dropped", "check": {"verdict",
    "findings": [{"level", "what", "means"}]} (is the log good enough, and what each gap
    means for the draft), "next_steps": {"check", "never"} (what to check in the draft, and
    what this log cannot tell), "observed", "errors", "warnings", "problems"}, and with
    *idle_waits* "idle_waits": [{"block", "delay"}] (the activities that got a modelled wait).
    The two lists are cut to fit one reply; the totals say how many there are, and the open
    model has them all.
    """
    from nagasim import discover

    full = _WORKSPACE.resolve(path, must_exist=True)
    handle = name or full.stem
    if handle in _WORKSPACE.models:
        raise ToolError("E_MODEL_EXISTS", f"a model named '{handle}' is already open",
                        "pick another name, or edit the existing one")
    try:
        layout, report = discover.discover(full, routing=routing, time_unit=time_unit,
                                           idle_waits=bool(idle_waits))
    except discover.LogError as exc:
        raise ToolError("E_DISCOVER_LOG", str(exc),
                        "fix the log (or name its columns) and try again") from None
    doc = ModelDocument(layout)
    _WORKSPACE.models[handle] = doc
    problems = _problems(doc)
    activities = []
    for a in report["activities"]:
        fit = a.get("service") or a.get("sojourn")
        activities.append({"block": a["block"], "original": a["original"][:200],
                           "count": a["count"], "lifecycle": a["lifecycle"],
                           "distribution": fit["distribution"], "mean": _finite(fit["mean"]),
                           "mean_wait": _finite(a["mean_wait"]), "resource": a["resource"]})
    reply = {"ok": True, "model": handle,
             "routing": {"mode": report["routing"]["mode"],
                         "reason": report["routing"]["reason"]},
             "activities": activities,
             "resources": [{**r, "original": r["original"][:200]}
                           for r in report["resources"][:MAX_ROWS]],
             "activities_total": len(activities), "resources_total": len(report["resources"]),
             "assumptions": report["assumptions"], "dropped": report["dropped"],
             "check": {"verdict": report["check"]["verdict"],
                       "findings": [{k: f[k] for k in ("level", "what", "means")}
                                    for f in report["check"]["findings"]]},
             "next_steps": report["next_steps"],
             **({"idle_waits": [{k: w[k] for k in ("block", "delay")}
                                for w in report["idle_waits"][:MAX_ROWS]]}
                if idle_waits else {}),
             "observed": {k: report["observed"][k] for k in ("cases", "throughput",
                                                              "cycle_time")},
             "errors": sum(1 for p in problems if p.severity == "error"),
             "warnings": sum(1 for p in problems if p.severity == "warning"),
             "problems": _problem_dicts(problems)}
    # Under the page's byte cap as `_rows` is (#568): the lists halve until the reply fits.
    while len(json.dumps(reply)) > MAX_PAGE_BYTES and \
            (len(reply["activities"]) > 1 or len(reply["resources"]) > 1):
        for key in ("activities", "resources"):
            reply[key] = reply[key][:max(1, len(reply[key]) // 2)]
    return reply


_document_args("save_model",
               model="Workspace name of the model to save.",
               path="Destination path under the workspace root; defaults to where it was opened.")


@tool()
def save_model(model: str, path: str | None = None) -> dict:
    """Write a model in the workspace to disk as JSON.

    Call it once the model validates. A model created with `new_model` has no
    path of its own, so the first save needs one.

    Returns {"ok", "path", "errors", "warnings"}.
    """
    doc = _WORKSPACE.document(model)
    target = _WORKSPACE.resolve(path) if path else doc.path
    if target is None:
        raise ToolError("E_NO_PATH", f"model '{model}' has never been saved",
                        "pass a path, e.g. save_model(model=..., path='mymodel.json')")
    doc.save(_WORKSPACE.resolve(target))
    problems = _problems(doc)
    return {"ok": True, "path": str(doc.path),
            "errors": sum(1 for p in problems if p.severity == "error"),
            "warnings": sum(1 for p in problems if p.severity == "warning")}


@tool(read_only=True)
def list_models() -> dict:
    """List the models currently open in the workspace, and the model files
    available under the workspace root.

    Use it to find out what you can `open_model` and which handles `get_model_summary`
    and `run_model` accept.

    Returns {"open": [{"name", "path", "blocks", "dirty", "has_results"}],
    "files": [relative paths]}.
    """
    files = sorted(str(p.relative_to(_WORKSPACE.root))
                   for p in _WORKSPACE.root.rglob("*.json"))[:MAX_ROWS]
    return {
        "root": str(_WORKSPACE.root),
        "open": [{"name": name, "path": str(doc.path) if doc.path else None,
                  "blocks": len(doc.blocks), "dirty": doc.dirty,
                  "has_results": name in _WORKSPACE.results}
                 for name, doc in sorted(_WORKSPACE.models.items())],
        "files": files,
    }


@tool(read_only=True)
def get_model_summary(model: str) -> dict:
    """A compact picture of a model: its blocks with their key parameters, its
    links in branch order, its resources, and how many problems it has.

    This is the tool to call before editing an existing model, and after a
    batch of edits to check the shape is what you intended. It is deliberately
    much smaller than `get_model_json`; reach for that only when you need every
    field.

    Returns {"model", "blocks", "links", "resources", "resource_sets",
    "variables", "tables", "timeseries_inputs", "levels", "flows", "auxiliaries",
    "delays", "schedules",
    "failures", "scenarios", "simulation",
    "errors", "warnings"}. Each link is "source -> target"; the ORDER of a block's
    outgoing links is its branch order (branch 0 first).
    """
    doc = _WORKSPACE.document(model)
    layout = doc.to_layout()
    blocks = []
    for block in layout["blocks"]:
        params = block.get("params", {})
        keep = {k: v for k, v in params.items()
                if k in ("resource_name", "distribution", "dist_params", "mode", "probs",
                         "conditions", "assignments", "stat_name", "kind", "max_arrivals",
                         "definition", "signal_name", "batch_size", "capacity")}
        blocks.append({"name": block["name"], "type": block["type"], "params": keep})
    problems = _problems(doc)
    return {
        "model": model,
        "blocks": blocks,
        "links": [f"{l['source']} -> {l['target']}" for l in layout["links"]],
        "resources": layout["resources"],
        "resource_sets": layout["resource_sets"],
        "variables": layout["variables"],
        "tables": [t.get("name") for t in layout["tables"]],
        # PHASE4 D1/D2: names only, like tables -- `list_continuous` has the detail.
        "timeseries_inputs": [s.get("name") for s in layout["timeseries_inputs"]],
        "levels": [l.get("name") for l in layout["levels"]],
        "flows": [f.get("name") for f in layout["flows"]],
        "auxiliaries": [a.get("name") for a in layout["auxiliaries"]],     # PHASE5 SD-2
        "delays": [d.get("name") for d in layout["delays"]],
        "schedules": [s.get("name") for s in layout["schedules"]],
        "failures": [f.get("name") for f in layout["failures"]],
        "scenarios": [s.get("name") for s in layout["scenarios"]],
        "submodels": sorted(layout["submodels"]),
        "simulation": layout["simulation"],
        "errors": sum(1 for p in problems if p.severity == "error"),
        "warnings": sum(1 for p in problems if p.severity == "warning"),
    }


@tool(read_only=True)
def get_model_json(model: str) -> dict:
    """The complete model layout as the JSON that would be saved to disk.

    Use it only when `get_model_summary` is not enough -- a full layout is
    large. It is also what you would hand to another tool that speaks the model
    file format (see the `naga://reference/schema` resource).

    Returns {"model", "layout"}.
    """
    return {"model": model, "layout": _WORKSPACE.document(model).to_layout()}


# ============================================================ history
_document_args("undo", model="Workspace name of the model.")


@tool()
def undo(model: str) -> dict:
    """Undo the last edit to a model.

    Every tool that changes a model pushes exactly one undo entry, so one
    `undo` reverses one tool call. Use it to back out an edit whose returned
    problems show it was wrong.

    Returns the usual edit answer plus {"can_undo", "can_redo", "label": the
    undone edit's short name (e.g. "Add block"), or "Edit" for one with none}.
    """
    doc, before = _open(model)
    if not doc.can_undo:
        raise ToolError("E_NOTHING_TO_UNDO", f"model '{model}' has no edit to undo",
                        "there is nothing before the current state")
    label = doc.undo()
    return _edit_result(doc, before,
                        {"can_undo": doc.can_undo, "can_redo": doc.can_redo, "label": label})


_document_args("redo", model="Workspace name of the model.")


@tool()
def redo(model: str) -> dict:
    """Redo the edit that `undo` reversed.

    Only available while nothing new has been edited since the undo: any fresh
    mutation clears the redo stack, exactly like the editor.

    Returns the usual edit answer plus {"can_undo", "can_redo", "label": the
    redone edit's short name}.
    """
    doc, before = _open(model)
    if not doc.can_redo:
        raise ToolError("E_NOTHING_TO_REDO", f"model '{model}' has nothing to redo",
                        "a new edit clears the redo stack")
    label = doc.redo()
    return _edit_result(doc, before,
                        {"can_undo": doc.can_undo, "can_redo": doc.can_redo, "label": label})
