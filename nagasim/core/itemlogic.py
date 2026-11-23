"""Catalog items with their own Python logic: the checks of an item's code and its runtime.

A submodel definition may carry `code`: a class with up to five event hooks that reads its
instance's parameters. Checking it is a pure function of the file: `ast.parse` and a walk
of the tree, never an import, never a compile to bytecode, and the trust store is never
read. Whether code is trusted shows in the editor's code panel and at run time.

Running it: flattening hands the engine each instance's code (`item_code` of the flattened
layout); `build_items` hashes each source itself, refuses the build when any fingerprint
is not trusted on this computer (`ItemCodeUntrusted`, before anything is loaded), then
loads the same normalised bytes through `trusted.BytesLoader`, once per fingerprint per
model build and never into `sys.modules`, so no state crosses replications. The LOGIC block
calls on_entry and on_exit; the runtime itself calls on_timer, on_state and on_signal.

SECURITY: permission is the caller's, never the file's (`netguard`). The gate is the
person's item trust store and the install's bundled list (`settings/itemtrust`) alone: a
fingerprint in the file is never read, no environment variable unlocks item code, and the
agent-Python consent neither unlocks nor blocks it. Trusted code runs with full trust; it
is not sandboxed.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import MappingProxyType

import numpy as np

from nagasim.core.agents.python import PythonRefused
from nagasim.core.base_process import deterministic_hash
from nagasim.core.expressions import ExpressionScope, compile_expression
from nagasim.core.problems import Problem, problem
from nagasim.core.routing import uses_item_rule
from nagasim.settings import itemtrust, trusted
from nagasim.settings.itemtrust import MAX_SOURCE

__all__ = ["HOOKS", "HOOK_ARGS", "code_problems", "ItemCodeUntrusted", "ItemCodeError", "untrusted",
           "build_items", "ItemRuntime", "ItemApi", "code_blocks", "untrusted_blocks", "claim_line"]

# Positional parameters including self.
HOOKS: dict[str, int] = {"on_entry": 3, "on_exit": 3, "on_timer": 2, "on_state": 4, "on_signal": 3}
HOOK_ARGS: dict[str, str] = {"on_entry": "self, entity, item", "on_exit": "self, entity, item",
                             "on_timer": "self, item", "on_state": "self, variable, value, item",
                             "on_signal": "self, name, item"}
# The LOGIC block's `hook` value -> the hook it calls.
BLOCK_HOOKS = {"entry": "on_entry", "exit": "on_exit"}


def _code(definition: str, message: str, hint: str, fields: dict) -> Problem:
    return problem("E_ITEM_CODE", message, f"/submodels/{definition}/code", hint=hint,
                   fields={"definition": definition, **fields})


def _hook(definition: str, message: str, hint: str, fields: dict, block: str | None = None,
          path: str | None = None) -> Problem:
    return problem("E_ITEM_HOOK", message, path or f"/submodels/{definition}/code", block=block, hint=hint,
                   fields={"definition": definition, **fields})


def _class_hooks(tree: ast.Module, name: str) -> dict[str, ast.FunctionDef] | None:
    """The hooks the top-level class *name* defines, or None when there is no such class."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {f.name: f for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in HOOKS}
    return None


def code_problems(definition: str, code: dict, blocks: list[dict], variables: set[str]) -> list[Problem]:
    """The problems of definition *definition*'s item code and of its wiring: *blocks* are the
    definition's blocks, *variables* the model's variable names. Parses only."""
    if not isinstance(code, dict):
        return []           # the shape check reports a code record that is not an object
    source, cls = code.get("source"), code.get("class")
    if not isinstance(source, str) or not isinstance(cls, str):
        return []           # the shape check reports a missing or non-text source or class
    if len(source) > MAX_SOURCE:
        return [_code(definition, f"Item {definition}: its code is longer than {MAX_SOURCE} characters",
                      "split the logic, or move data into the item's parameters", {"case": "size"})]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        line = exc.lineno or 1
        return [_code(definition, f"Item {definition}: its code does not parse at line {line}: {exc.msg}",
                      "fix the line in the item's code panel",
                      {"case": "syntax", "line": line, "message": exc.msg})]
    except (ValueError, RecursionError, MemoryError) as exc:     # a null byte, or nesting too deep
        return [_code(definition, f"Item {definition}: its code does not parse at line 1: {exc}",
                      "fix the item's code", {"case": "syntax", "line": 1, "message": str(exc)})]
    hooks = _class_hooks(tree, cls)
    if hooks is None:
        return [_code(definition, f"Item {definition}: its code has no class {cls}",
                      "name the class the code defines", {"case": "class", "class": cls})]
    found: list[Problem] = []
    for name, func in hooks.items():
        args = func.args
        if len(args.posonlyargs) + len(args.args) != HOOKS[name] or args.vararg or args.kwonlyargs:
            found.append(_code(definition, f"Item {definition}: {name} takes ({HOOK_ARGS[name]})",
                               f"define it as def {name}({HOOK_ARGS[name]})",
                               {"case": "hook_args", "hook": name, "wanted": HOOK_ARGS[name],
                                "line": func.lineno}))
    if not hooks:
        found.append(_code(definition, f"Item {definition}: class {cls} has none of the five hooks",
                           "define on_entry, on_exit, on_timer, on_state or on_signal",
                           {"case": "no_hook", "class": cls}))

    for index, block in enumerate(blocks or []):
        if not isinstance(block, dict) or str(block.get("type", "")).upper() != "LOGIC":
            continue
        wanted = BLOCK_HOOKS.get((block.get("params") or {}).get("hook"))
        if wanted and wanted not in hooks:
            name = str(block.get("name", ""))
            found.append(_hook(definition,
                               f"Item {definition}: block {name} calls {wanted}, which class {cls} does not define",
                               f"define {wanted} in class {cls}, or clear the block's hook",
                               {"case": "missing", "hook": wanted, "class": cls},
                               block=name, path=f"/submodels/{definition}/blocks/{index}/params/hook"))
    timer = code.get("timer")
    ticking = isinstance(timer, (int, float)) and not isinstance(timer, bool) and timer > 0
    if ticking != ("on_timer" in hooks):
        found.append(_hook(definition,
                           f"Item {definition}: on_timer needs a timer above zero, and a timer needs on_timer",
                           "set the timer and define on_timer, or neither", {"case": "timer"}))
    for variable in code.get("watch") or []:
        if variable not in variables:
            found.append(_hook(definition, f"Item {definition}: it watches {variable}, which is not a model variable",
                               "declare the variable, or remove it from watch",
                               {"case": "watch", "variable": str(variable)}))
    if bool(code.get("signals")) != ("on_signal" in hooks):
        found.append(_hook(definition, f"Item {definition}: on_signal and a list of signals go together",
                           "list the signals and define on_signal, or neither", {"case": "signals"}))
    return found


def hook_block_problems(layout: dict) -> list[Problem]:
    """E_ITEM_HOOK of the LOGIC blocks of a flattened layout: a hook value other than entry,
    exit or empty, and a hook on a block outside an item with code."""
    items = layout.get("item_code") or {}
    found = []
    for index, block in enumerate(layout.get("blocks") or []):
        btype = str(block.get("type", "")).upper() if isinstance(block, dict) else ""
        if uses_item_rule(btype, block.get("params")) and not (
                isinstance(block.get("item"), str) and isinstance(items, dict)
                and isinstance(items.get(block["item"]), dict)):
            # D1359 (M7): an "item" routing rule reads the code of the item the block is in
            name = str(block.get("name", ""))
            found.append(_hook("", f"Block {name}: an item routing rule needs an item with code",
                               "put the block in an item whose definition has code, or pick "
                               "another kind", {"case": "rule_no_code"}, block=name,
                               path=f"/blocks/{index}/params"))
            continue
        if btype != "LOGIC":
            continue
        value = (block.get("params") or {}).get("hook")
        if value in (None, ""):
            continue
        name = str(block.get("name", ""))
        at = f"/blocks/{index}/params/hook"
        item = block.get("item")
        record = items.get(item) if isinstance(items, dict) and isinstance(item, str) else None
        definition = str(record.get("definition", "")) if isinstance(record, dict) else ""
        if value not in BLOCK_HOOKS:
            found.append(_hook(definition, f"Block {name}: hook is entry, exit or empty, not {value}",
                               "set hook to entry or exit, or clear it", {"case": "hook_value", "value": str(value)},
                               block=name, path=at))
        elif not definition:
            found.append(_hook(definition, f"Block {name}: a LOGIC hook needs an item with code",
                               "give the item's definition code, or clear the block's hook", {"case": "no_code"},
                               block=name, path=at))
    return found


def top_level_problems(layout: dict) -> list[Problem]:
    """E_ITEM_HOOK when a model file holds `item_code` at its top: only flattening writes it."""
    if "item_code" not in layout:
        return []
    return [_hook("", "Item code belongs on a submodel definition, not at the top of the file",
                  "move the code into a submodel definition's code", {"case": "top_level"}, path="/item_code")]


# -- the runtime ---------------------------------------------------------------------------
class ItemCodeUntrusted(PythonRefused):
    """A model's item code is not trusted on this computer: nothing of it was loaded."""

    def __init__(self, message: str, untrusted: list | None = None):
        super().__init__(message)
        # Rebuilt from its message alone when SimPy or a worker pool copies it.
        self.untrusted = list(untrusted or [])


class ItemCodeError(RuntimeError):
    """An item's hook raised, or answered something its block cannot use."""


def untrusted(item_code: dict) -> list[dict]:
    """[{"definition", "fingerprint"}] of each distinct fingerprint in a flattened `item_code`
    map that is not trusted, each source hashed here (a `fingerprint` in the record is
    ignored). A source that is not text or is too long is untrusted. The store is read once."""
    trusted_now = itemtrust.trusted_fingerprints()
    found, seen = [], set()
    for record in (item_code or {}).values():
        record = record if isinstance(record, dict) else {}
        source = record.get("source")
        ok = isinstance(source, str) and len(source) <= MAX_SOURCE
        fp = itemtrust.fingerprint(source) if ok else "sha256:" + "0" * 64
        if fp in seen:
            continue
        seen.add(fp)
        if not ok or fp not in trusted_now:
            found.append({"definition": str(record.get("definition", "")), "fingerprint": fp})
    return found


def untrusted_message(found: list[dict]) -> str:
    named = ", ".join(f"{u['definition']} ({itemtrust.short(u['fingerprint'])})" for u in found)
    text = (f"item code is not trusted on this computer: {named}; a person trusts it in the item's "
            "code panel or with `naga trust MODEL`")
    unreadable = itemtrust.store_problem()
    if unreadable:
        text += f" (the trust store {itemtrust.store_path()} does not read: {unreadable})"
    return text


def _load(definition: str, source: str, fp: str):
    """The module of trusted *source*, executed now from its normalised bytes; never kept in
    `sys.modules`. Returns (module, the filename its frames carry)."""
    loader = trusted.BytesLoader(Path(f"<item {definition} {itemtrust.short(fp)}>"),
                                 itemtrust.code_bytes(source))
    spec = importlib.util.spec_from_loader(f"nagasim_item_{itemtrust.short(fp)}", loader, origin=loader.path)
    module = importlib.util.module_from_spec(spec)
    try:
        loader.exec_module(module)
    except Exception as exc:
        raise ItemCodeError(_error_text(definition, "", "<module>", exc, loader.path)) from exc
    return module, loader.path


def _error_line(exc: BaseException, filename: str) -> int | None:
    """The line of the innermost frame of the item's own code on *exc*'s traceback."""
    line, tb = None, exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _error_text(instance: str, cls: str, hook: str, exc: BaseException, filename: str) -> str:
    line = _error_line(exc, filename)
    where = f"item '{instance}' {cls}.{hook}" if cls else f"item code of {instance}"
    return f"{where}{f', line {line}' if line else ''}: {type(exc).__name__}: {exc}"


class ItemApi:
    """The `item` argument of every hook: the instance's name and parameters, the clock,
    expressions in the item's scope, model variables, signals and its own random stream."""

    def __init__(self, name: str, locals_: dict, ctx):
        self.name = name
        self.params = MappingProxyType(dict(locals_))
        self._locals = dict(locals_)
        self._ctx = ctx
        self._compiled: dict[str, object] = {}
        # Its own stream, made as a block's is (seed, name).
        self._rng = np.random.default_rng(np.random.SeedSequence([ctx.seed, deterministic_hash(name)]))

    @property
    def now(self) -> float:
        return float(self._ctx.env.now)

    def evaluate(self, text: str, entity=None):
        """*text* evaluated in the item's scope: its parameters, the model, *entity*."""
        expression = self._compiled.get(text)
        if expression is None:
            expression = self._compiled[text] = compile_expression(text)
        return expression.evaluate(ExpressionScope(entity=entity, ctx=self._ctx, rng=self._rng,
                                                   locals=self._locals))

    def get(self, variable: str) -> float:
        if variable not in self._ctx.variables:
            raise KeyError(f"no model variable {variable!r}")
        return float(self._ctx.variables[variable])

    def set(self, variable: str, value: float) -> None:
        ctx = self._ctx
        if variable not in ctx.variables:
            raise KeyError(f"no model variable {variable!r}")
        ctx.variables[variable] = value
        ctx.wrote_variable(variable)
        # What ASSIGN does after a write: rates and agent conditions may read the variable.
        if ctx.level_system is not None:
            ctx.level_system.trigger()
        if ctx.agent_system is not None:
            ctx.agent_system.trigger()

    def signal(self, name: str) -> None:
        self._ctx.raise_signal(name)

    def random(self) -> float:
        return float(self._rng.random())

    def reverse(self, belt: str, forward: bool) -> None:
        """Run reversible CONVEYOR *belt* forward (True) or backward from now (D1368); the
        direction it already runs changes nothing."""
        conveyor = self._ctx.conveyors.get(belt)
        if conveyor is None:
            raise KeyError(f"no conveyor {belt!r}")
        conveyor.set_direction(bool(forward))


class ItemRuntime:
    """One item instance: its class instance and the hooks the blocks and the runtime call."""

    def __init__(self, instance: str, record: dict, cls, filename: str, ctx):
        self.instance, self.filename, self.ctx = instance, filename, ctx
        self.cls_name = str(record.get("class", ""))
        self.api = ItemApi(instance, record.get("locals") or {}, ctx)
        self._in_state = False
        self.obj = self._call("__init__", cls)

    def _call(self, hook: str, fn, *args):
        try:
            return fn(*args)
        except ItemCodeError:
            raise
        except Exception as exc:
            raise ItemCodeError(_error_text(self.instance, self.cls_name, hook, exc, self.filename)) from exc

    def entry(self, entity, outputs: int) -> int:
        answer = self._call("on_entry", self.obj.on_entry, entity, self.api)
        if answer is None:
            return 0
        if isinstance(answer, bool) or not isinstance(answer, (int, np.integer)) or not 0 <= answer < outputs:
            raise ItemCodeError(f"item '{self.instance}' {self.cls_name}.on_entry returned {answer!r}, "
                                f"but its block has {outputs} outputs (0 to {outputs - 1})")
        return int(answer)

    def exit(self, entity) -> None:
        self._call("on_exit", self.obj.on_exit, entity, self.api)

    def timer(self, every: float):
        env = self.ctx.env
        while True:
            yield env.timeout(every)
            self._call("on_timer", self.obj.on_timer, self.api)

    def state(self, variable: str) -> None:
        if self._in_state:          # its own write inside on_state does not call it again
            return
        self._in_state = True
        try:
            self._call("on_state", self.obj.on_state, variable, self.ctx.variables.get(variable), self.api)
        finally:
            self._in_state = False

    def signal(self, name: str) -> None:
        self._call("on_signal", self.obj.on_signal, name, self.api)


def build_items(item_code: dict, ctx) -> dict[str, ItemRuntime]:
    """The runtimes of a flattened model's items, once its trust is checked: the build is
    refused whole (`ItemCodeUntrusted`) before anything loads when any fingerprint is not
    trusted. Each distinct fingerprint's module is loaded once per call."""
    found = untrusted(item_code)
    if found:
        raise ItemCodeUntrusted(untrusted_message(found), found)
    modules: dict[str, tuple] = {}
    runtimes: dict[str, ItemRuntime] = {}
    for instance, record in item_code.items():
        source, definition = record["source"], str(record.get("definition", ""))
        # The bytes hashed are the bytes run: hashed again from the very string loaded.
        fp = itemtrust.fingerprint(source)
        if fp not in modules:
            modules[fp] = _load(definition, source, fp)
        module, filename = modules[fp]
        cls = getattr(module, str(record.get("class", "")), None)
        if not isinstance(cls, type):
            raise ItemCodeError(f"item '{instance}': its code has no class {record.get('class')}")
        runtime = runtimes[instance] = ItemRuntime(instance, record, cls, filename, ctx)
        every = record.get("timer")
        if isinstance(every, (int, float)) and not isinstance(every, bool) and every > 0 \
                and hasattr(runtime.obj, "on_timer"):
            ctx.env.process(runtime.timer(float(every)))
        if hasattr(runtime.obj, "on_state"):
            for variable in record.get("watch") or []:
                ctx.variable_hooks.setdefault(str(variable), []).append(runtime.state)
        if hasattr(runtime.obj, "on_signal"):
            for name in record.get("signals") or []:
                ctx.signal_hooks.setdefault(str(name), []).append(runtime.signal)
    return runtimes


# -- what a person is asked to trust --------------------------------------------------------
def code_blocks(layout: dict) -> list[dict]:
    """One entry per definition with code in an unflattened *layout* (`extends` resolved):
    {"definition", "class", "source", "fingerprint", "library", "author", "edited"}. The
    fingerprint is computed here; `library` and `author` are what the file claims, and
    `edited` says the source differs from the fingerprint recorded when it was placed."""
    from nagasim.core import submodels    # late: submodels is a heavier import

    definitions, _problems = submodels.resolve_extends(layout.get("submodels") or {})
    found = []
    for name, definition in definitions.items():
        code = definition.get("code") if isinstance(definition, dict) else None
        if not isinstance(code, dict) or not isinstance(code.get("source"), str):
            continue
        fp = itemtrust.fingerprint(code["source"])
        placed = code.get("placed") if isinstance(code.get("placed"), dict) else {}
        library = (definition.get("info") or {}).get("library")
        found.append({"definition": name, "class": str(code.get("class", "")), "source": code["source"],
                      "fingerprint": fp, "library": library if isinstance(library, dict) else None,
                      "author": placed.get("author") or None,
                      "edited": bool(placed.get("fingerprint")) and placed.get("fingerprint") != fp})
    return found


def untrusted_blocks(layout: dict) -> list[dict]:
    """The `code_blocks` entries not trusted on this computer, one per fingerprint."""
    trusted_now = itemtrust.trusted_fingerprints()
    found, seen = [], set()
    for entry in code_blocks(layout):
        if entry["fingerprint"] not in trusted_now and entry["fingerprint"] not in seen:
            seen.add(entry["fingerprint"])
            found.append(entry)
    return found


def claim_line(entry: dict) -> str:
    """Where the model file says a block of code comes from: the file's claim, not a fact."""
    library = entry.get("library")
    if library:
        text = (f"The model file says it comes from library {library.get('library', '?')} "
                f"{library.get('version', '?')}" + (f" by {entry['author']}" if entry.get("author") else "") + ".")
    else:
        text = "The model file names no library: it was written in this model."
    return text + (" It was changed after it was placed." if entry.get("edited") else "")
