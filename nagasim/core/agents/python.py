"""Agent Python behaviour (PHASE6 A7, D105): the caller-consent gate, the
parse-only check validation uses, the one `exec` under nagasim/ and the two
views consented code sees (`AgentView`, `AgentApi`). A security review of
agent Python reads this file alone."""
from __future__ import annotations

import ast
import builtins
import math
import os

from nagasim.core.agents.common import _number
from nagasim.core.expressions import ExpressionError


# PHASE6 A7, D105. The gate is the CALLER's (netguard's rule): a model file's
# `python` is data until the process that runs it was started with this
# environment variable set to "1" -- never a model key, never a tool argument.
# The checks below are defence in depth against mistakes, NOT a sandbox:
# CPython cannot sandbox in-process code, so consented code is trusted code,
# the standing custom_logic.py and a plugin already have (ARCHITECTURE 7.6).
PYTHON_CONSENT = "NAGASIM_ALLOW_AGENT_PYTHON"
_PY_BUILTINS = {name: getattr(builtins, name) for name in (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "float", "int", "len", "list",
    "max", "min", "pow", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple",
    "zip")}
# Attribute ALLOW-list: the two views, math, and plain list/dict/set/str
# methods. Everything else (dunders, gi_frame, f_globals, str.format, ...) is
# refused at load, so no path from a value to its class, frame or globals.
_PY_ATTRS = frozenset(
    ("id", "population", "type", "state", "pos", "alive", "get", "set",            # AgentView
     "now", "payload", "params", "let", "random", "uniform", "normal", "integers", "count",  # AgentApi
     "sum", "mean", "agent", "near", "links", "send", "spawn", "remove",
     "append", "extend", "insert", "pop", "sort", "reverse", "index", "copy", "clear",
     "items", "keys", "values", "update", "setdefault", "add", "discard", "union",
     "intersection", "difference", "join", "split", "strip", "lower", "upper",
     "startswith", "endswith", "replace", "is_integer")
    + tuple(name for name in dir(math) if not name.startswith("_")))
_PY_REFUSED = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef,
               ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom, ast.With,
               ast.AsyncWith, ast.AsyncFor)


def python_check(code) -> tuple[dict, set, list]:
    """(functions, sends, errors) of an agent type's `python` WITHOUT running
    it: `ast.parse` only. *functions* maps each top-level def to whether it
    takes exactly (agent, api), None when the code does not parse; *sends* are the literal names passed to
    `api.send`; *errors* are "line N: ..." strings."""
    try:
        tree = ast.parse(str(code), "<agent python>")
    except SyntaxError as exc:
        return None, set(), [f"line {exc.lineno}: {exc.msg}"]
    except (ValueError, RecursionError, MemoryError) as exc:
        return None, set(), [f"does not parse: {type(exc).__name__}"]
    errors, functions, sends = [], {}, set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            a = node.args
            functions[node.name] = (len(a.posonlyargs) + len(a.args) == 2 and not a.defaults
                                    and not (a.vararg or a.kwonlyargs or a.kwarg))
        elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            errors.append(f"line {node.lineno}: only def statements may sit at the top level "
                          f"(nothing runs when the code loads)")
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, _PY_REFUSED):
            errors.append(f"line {line}: {type(node).__name__} is not allowed in agent Python")
        elif isinstance(node, ast.Attribute) and node.attr not in _PY_ATTRS:
            errors.append(f"line {line}: attribute '.{node.attr}' is not allowed (only the "
                          f"agent/api views, math and plain list/dict/str methods)")
        elif isinstance(node, ast.Name) and node.id.startswith("_"):
            errors.append(f"line {line}: name '{node.id}' is not allowed (no _-prefixed names)")
        elif isinstance(node, ast.FunctionDef) and node.decorator_list:
            errors.append(f"line {line}: decorators are not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr == "send" and node.args and \
                isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            sends.add(node.args[0].value)
    return functions, sends, errors


class PythonRefused(PermissionError):
    """A model's Python was not run: the caller did not consent (D105), or the caller is an
    agent surface, which never does (D186)."""


def _load_python(tname: str, code: str) -> dict:
    """The type's functions, compiled once per run. Refused before anything
    is compiled unless the caller consented (PYTHON_CONSENT)."""
    if os.environ.get(PYTHON_CONSENT) != "1":
        raise PythonRefused(
            f"agent type '{tname}' carries Python behaviour, which runs only with the caller's "
            f"consent: set {PYTHON_CONSENT}=1 in the environment of the process that runs the "
            f"model (a model file cannot grant it); only do so for a model whose code you trust")
    _functions, _sends, errors = python_check(code)
    if errors:
        raise ExpressionError(f"agent type '{tname}': python {errors[0]}")
    namespace = {"__builtins__": _PY_BUILTINS, "math": math}
    # The one exec under nagasim/ (D105): consented, checked code of def
    # statements only, so running the module body just binds the functions.
    exec(compile(code, _py_file(tname), "exec"), namespace)
    return namespace


def _py_file(tname: str) -> str:
    return f"<agent type {tname}>"


def _python_action(name: str, tname: str, namespace: dict):
    """`{python: "<fn>"}`: calls fn(agent, api); any exception becomes an
    ExpressionError naming the type, the function, the agent, t and the line."""
    fn, filename = namespace[name], _py_file(tname)

    def run(system, pop, i, scope):
        view, api = AgentView(system, pop, i, True), AgentApi(system, pop, i, scope)
        try:
            fn(view, api)
        except Exception as exc:
            tb, line = exc.__traceback__, ""
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == filename:
                    line = f", line {tb.tb_lineno}"
                tb = tb.tb_next
            raise ExpressionError(f"agent type '{tname}': python {name}() of {pop.name}[{i}] "
                                  f"at t={system.env.now:g}{line}: {type(exc).__name__}: "
                                  f"{exc}") from exc
        finally:
            view._sys = api._sys = api._scope = _EXPIRED     # #151: a stash is dead
    return run


class _Expired:
    """What an own view and an api hold once their call returned (#151): any
    use (a write, a draw, a send, a remove) raises; a stash reaches nothing."""

    __slots__ = ()

    def __getattr__(self, name):
        raise ExpressionError("an agent view or api was used after its python call returned "
                              "(kept across calls); each call gets fresh ones")


_EXPIRED = _Expired()


class AgentView:
    """An agent as agent Python sees it (A7): reads go through
    `_Population.read` (a synchronous tick's snapshot included), `set` through
    `AgentSystem.set_attr` (the set action's own validation) and only on the
    agent whose function is running. Stale once its agent is removed."""

    __slots__ = ("_sys", "_pop", "_i", "_gen", "_own")

    def __init__(self, system, pop, i: int, own: bool = False):
        self._sys, self._pop, self._i, self._own = system, pop, int(i), own
        self._gen = int(pop.gen[i])

    def __repr__(self):
        return f"{self._pop.name}[{self._i}]"

    def _live(self):
        pop, i = self._pop, self._i
        if pop.gen[i] != self._gen or pop.state[i] < 0:
            raise ExpressionError(f"{self!r} was removed")
        return pop, i

    @property
    def id(self) -> int:
        return self._i

    @property
    def population(self) -> str:
        return self._pop.name

    @property
    def type(self) -> str:
        return self._pop.type.name

    @property
    def alive(self) -> bool:
        return bool(self._pop.gen[self._i] == self._gen and self._pop.state[self._i] >= 0)

    @property
    def state(self) -> str:
        pop, i = self._live()
        return pop.read("state", i)

    @property
    def pos(self) -> tuple:
        pop, i = self._live()
        if pop.type.space is None:
            raise ExpressionError(f"{self!r}: agent type '{pop.type.name}' has no space")
        x, y = self._sys._position(pop, i, float(self._sys.env.now))
        return float(x), float(y)

    def get(self, name):
        pop, i = self._live()
        return pop.read(str(name), i)

    def set(self, name, value) -> None:
        if not self._own:
            raise ExpressionError(f"{self!r} is read-only here: an agent sets only its own "
                                  f"attributes (send the other one a message)")
        pop, i = self._live()
        self._sys.set_attr(pop, i, str(name), value)


class AgentApi:
    """The rest of what agent Python can reach (A7), and nothing else: the
    clock, the payload, the type's parameters, draws from the agent's OWN
    stream, population reads, other agents as read-only views (ascending
    index order), and send/spawn/remove through the engine's own paths."""

    __slots__ = ("_sys", "_pop", "_i", "_scope")

    def __init__(self, system, pop, i: int, scope):
        self._sys, self._pop, self._i, self._scope = system, pop, int(i), scope

    def _views(self, pop, idx) -> list:
        return [AgentView(self._sys, pop, j) for j in sorted(int(j) for j in idx)]

    @property
    def now(self) -> float:
        return float(self._sys.env.now)

    @property
    def payload(self):
        """The message's payload inside a message transition's actions, else None."""
        return (self._scope.locals or {}).get("payload")

    @property
    def params(self):
        return self._pop.type.py_params

    def let(self, name):
        """A `let` value of this tick (#153): the very value (draw included)
        the type's expressions read under that name."""
        values = self._scope.locals or {}
        if name not in self._pop.type.let_names or name not in values:
            raise ExpressionError(f"let({name!r}): no let value '{name}' here (lets exist in "
                                  f"on_tick and the tick transitions)")
        return values[name]

    def random(self) -> float:
        return float(self._scope.rng.random())

    def uniform(self, low, high) -> float:
        return float(self._scope.rng.uniform(low, high))

    def normal(self, mean, sd) -> float:
        return float(self._scope.rng.normal(mean, sd))

    def integers(self, low, high) -> int:
        return int(self._scope.rng.integers(low, high))

    def count(self, population, state=None) -> int:
        return self._sys.count(population, state)

    def _numeric(self, how, population, attr, state):
        if attr in self._sys.population(population).type.attr_cat:
            raise ExpressionError(f"{how.lower()}(\"{population}\", \"{attr}\"): a category "
                                  f"attribute has no {how.lower()}; use count")
        return self._sys.aggregate(how, population, attr, state)

    def sum(self, population, attr, state=None) -> float:
        return self._numeric("SUM", population, attr, state)

    def mean(self, population, attr, state=None) -> float:
        return self._numeric("MEAN", population, attr, state)

    def agent(self, population, index) -> AgentView:
        ref = self._sys.ref(population, index)
        return AgentView(self._sys, self._sys.population(ref[0]), ref[1])

    def near(self, population, r, state=None) -> list:
        return self._views(self._sys.population(population),
                           self._sys._neighbours(population, r, state, self._scope))

    def links(self, state=None) -> list:
        return self._views(self._pop, self._sys._linked(self._pop, self._i, state))

    def send(self, message, to, payload=None) -> None:
        if not isinstance(message, str) or not message:
            raise ExpressionError(f"send({message!r}, ...): a message is a non-empty name")
        views = [to] if isinstance(to, AgentView) else list(to)
        if not all(isinstance(v, AgentView) for v in views):
            raise ExpressionError(f"send(\"{message}\", {to!r}): to is an agent or a list of "
                                  f"agents (api.agent, api.near, api.links)")
        self._sys.send(message, [v._live() for v in views], payload)

    def spawn(self, population, count=1, state=None, init=None) -> list:
        target = self._sys.population(population)
        kind = target.type
        if state is not None and state not in kind.index:
            raise ExpressionError(f"spawn(\"{population}\"): no state '{state}'")
        init = dict(init or {})
        for name in init:
            if name not in target.attrs:
                raise ExpressionError(f"spawn(\"{population}\"): no attribute '{name}'")
        n = _number(count, f"spawn(\"{population}\"): count")
        if not (n >= 0 and n == int(n)):
            raise ExpressionError(f"spawn(\"{population}\"): count = {count!r}, must be an "
                                  f"integer >= 0")
        inits = [(name, lambda _scope, value=value: value) for name, value in init.items()]
        return [AgentView(self._sys, target, self._sys.add_agent(target, state, inits,
                                                                 self._scope))
                for _ in range(int(n))]

    def remove(self, target=None) -> None:
        views = [AgentView(self._sys, self._pop, self._i)] if target is None else \
            [target] if isinstance(target, AgentView) else list(target)
        for view in views:
            if not isinstance(view, AgentView):
                raise ExpressionError(f"remove({target!r}): not an agent")
            self._sys.remove_agent(*view._live())
