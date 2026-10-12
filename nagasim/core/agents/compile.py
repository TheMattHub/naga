"""An agent type compiled for a run: action closures (AB-2), `compile_target`,
the vectorised expression paths (AB-4, `vectorise`) and `_Type`, the
compiled type the runtime holds."""
from __future__ import annotations

import ast
import operator
from types import MappingProxyType

import numpy as np

from nagasim.core.agents.common import (TRIGGERS, _GRID_CALLS, _LINK_CALLS, _number, _reads_self,
                                        rate_parts)
from nagasim.core.agents.python import _load_python, _python_action
from nagasim.core.agents.space import _Grid
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled, to_base
from nagasim.core.timeunits import ModelUnits
from nagasim.core.expressions import (FUNCTIONS, ExpressionError, ExpressionScope,
                                      compile_expression, functions_with, parse_target)


# ---------------------------------------------------------------- actions (AB-2)
# Each action compiles to one closure `(system, pop, i, scope) -> None`; the
# model passed `validate_structured` before it runs, so these trust the shape.
def _compile_action(action: dict, names, cats=None, in_tick=False, py=None):
    if "python" in action:                              # PHASE6 A7: py = (type, namespace)
        return _python_action(str(action["python"]), *py)
    if "link" in action or "unlink" in action:          # PHASE6 A5
        on = "link" in action
        resolve = compile_target(action["link" if on else "unlink"])
        return lambda system, pop, i, scope: system.link(pop, i, resolve(system, scope), on)
    if "target" in action and str(action["target"]).strip().startswith("patch."):  # A4
        patch = str(action["target"]).strip()[6:]
        value = compile_expression(action["expr"], names).closure()
        return lambda system, pop, i, scope: system.set_patch(pop, i, patch, value(scope))
    if "step" in action:                                # PHASE6 A4
        dx, dy = (compile_expression(str(v), names).closure() for v in action["step"])
        return lambda system, pop, i, scope: system.step(pop, i, dx(scope), dy(scope))
    if "move_random" in action:                         # PHASE6 A4
        free = action["move_random"] == "free"
        return lambda system, pop, i, scope: system.move_random(pop, i, free)
    if "target" in action:
        kind, name = parse_target(str(action["target"]).strip())
        value = compile_expression(action["expr"], names).closure()
        if kind == "self":
            def run(system, pop, i, scope):
                system.set_attr(pop, i, name, value(scope))
        else:
            def run(system, pop, i, scope):
                ctx = system.env.ctx
                ctx.variables[name] = value(scope)
                if ctx.watched:                 # an event-driven HOLD reads it (O4)
                    ctx.touch(("var", name))
                if ctx.level_system is not None:
                    ctx.level_system.trigger()
        return run
    if "send" in action:
        message, resolve = str(action["send"]), compile_target(action["to"])
        payload = action.get("payload")
        payload = None if payload in (None, "") else compile_expression(payload, names).closure()
        return lambda system, pop, i, scope: system.send(
            message, resolve(system, scope), payload(scope) if payload else None)
    if "enter" in action:
        block, e_type = str(action["enter"]), action.get("e_type")
        return lambda system, pop, i, scope: system.spawn(block, e_type, pop, i)
    if "transfer" in action:
        level, mode = str(action["transfer"]), action.get("mode", "remove")
        amount = compile_expression(action["amount"], names).closure()
        return lambda system, pop, i, scope: system.env.ctx.level_system.move(
            level, amount(scope), mode)
    if "move_to" in action:                             # AB-3
        target = action["move_to"]
        if isinstance(target, (list, tuple)):
            point = [float(v) for v in target]
            target_fn = lambda scope: point
        else:
            target_fn = compile_expression(str(target), names).closure()
        return lambda system, pop, i, scope: system.move_to(pop, i, target_fn(scope), scope)
    if "forward" in action:                             # PHASE6 A6
        distance = compile_expression(str(action["forward"]), names).closure()
        if in_tick:          # NetLogo style: an instant jump, no "arrived"
            return lambda system, pop, i, scope: system.forward_tick(pop, i, distance(scope))
        return lambda system, pop, i, scope: system.forward(pop, i, distance(scope), scope)
    if "turn" in action:                                 # PHASE6 A6
        degrees = compile_expression(str(action["turn"]), names).closure()
        return lambda system, pop, i, scope: system.turn(pop, i, degrees(scope))
    if "face" in action:                                 # PHASE6 A6
        target = action["face"]
        if isinstance(target, (list, tuple)):
            point = [float(v) for v in target]
            target_fn = lambda scope: point
        else:
            target_fn = compile_expression(str(target), names).closure()
        return lambda system, pop, i, scope: system.face(pop, i, target_fn(scope))
    if "random_walk" in action:                          # PHASE6 A6
        step = compile_expression(str(action["random_walk"]["step"]), names).closure()
        turn_sd = compile_expression(str(action["random_walk"].get("turn_sd", 0)),
                                     names).closure()
        if in_tick:
            return lambda system, pop, i, scope: system.random_walk_tick(
                pop, i, step(scope), turn_sd(scope), scope)
        return lambda system, pop, i, scope: system.random_walk(
            pop, i, step(scope), turn_sd(scope), scope)
    if "spawn" in action:                               # PHASE6 C5
        into, state = str(action["spawn"]), action.get("state")
        count = action.get("count")
        count = (lambda scope: 1) if count is None else \
            compile_expression(str(count), names).closure()
        inits = [(attr, compile_expression(str(text), names).closure())
                 for attr, text in (action.get("init") or {}).items()]

        def run(system, pop, i, scope):
            n = _number(count(scope), f"spawn \"{into}\": count")
            if not (n >= 0 and n == int(n)):
                raise ExpressionError(f"spawn \"{into}\": count = {n!r}, must be an integer >= 0")
            target = system.population(into)
            for _ in range(int(n)):
                system.add_agent(target, state, inits, scope)
        return run
    if "remove" in action:                              # PHASE6 C5
        resolve = compile_target(action["remove"])

        def run(system, pop, i, scope):
            for target, j in resolve(system, scope):
                system.remove_agent(target, j)
        return run
    name = str(action["signal"])
    return lambda system, pop, i, scope: system.env.ctx.raise_signal(name)


def compile_target(to):
    """A send's `to` -> `resolve(system, scope) -> [(population, index)]`,
    *scope* the sender's (an agent's, or a SEND block's with the entity):
    "self"; an expression giving an agent reference (AGENT("Pop", i),
    entity.agent) or, from an agent, an index in its own population; or
    {"population", "where"?, "random"?}: every member (index order), those
    whose `where` holds for them (self = the recipient), or one of those drawn
    uniformly from the SENDER's stream. "links" / {"links": true, ...}
    (PHASE6 A5): the same over the sender's live linked neighbours, in
    ascending index order."""
    if to == "self":
        return lambda system, scope: [scope.agent]
    if to == "links":
        to = {"links": True}
    if isinstance(to, dict):
        links = "links" in to
        name, pick = None if links else str(to["population"]), bool(to.get("random"))
        where = to.get("where")
        where = None if where in (None, "") else compile_expression(where).closure()

        # `where_vec` needs the RECIPIENT type's categorical attributes (PHASE6
        # A2) to keep its own arithmetic-on-a-category safety net; the
        # population is one literal name here, so it is resolved once, on the
        # first send, and cached (a `[]`/`[vec]` slot: no vec is a valid cache
        # hit too, so a plain flag would not tell "uncached" from "no vec").
        where_vec_cache: list = []

        def resolve(system, scope):
            if links:                           # A5: the sender's own population
                pop, idx = system.linked(scope)
            else:
                pop = system.population(name)
                idx = pop.members()             # live agents only (C5)
            if not where_vec_cache:
                where_vec_cache.append(
                    vectorise(to["where"], cats=pop.type.attr_cat) if where is not None else None)
            where_vec = where_vec_cache[0]
            members = range(pop.n) if pop.live == pop.n and not links else idx.tolist()
            # The where's own scope: the recipient's parameters, plus the SENDER
            # (PHASE6 A9) as a local -- sender.<attr> reads it, `payload`'s shape.
            hit = None
            if where is not None:
                probe = ExpressionScope(ctx=scope.ctx, locals=pop.type.params
                                        if scope.agent is None else
                                        {**pop.type.params, "sender": scope.agent})
                hit = _run_vec(where_vec, pop, idx, bool, probe) if where_vec else None
            if hit is not None:
                members = idx[hit].tolist()
            elif where is not None:
                # ponytail: an O(N) scan per filtered send for a where outside the
                # numpy subset (and/or/if/not/in); vectorise that subset if it binds.
                members = [j for j in members if where(_at(probe, pop, j))]
            if pick:
                if not members:
                    return []
                members = [members[int(scope.rng.integers(len(members)))]]
            return [(pop, j) for j in members]
        return resolve
    value = compile_expression(str(to)).closure()

    def resolve(system, scope):
        ref = value(scope)
        if isinstance(ref, tuple) and len(ref) == 2:
            return [system.deref(ref)]
        if scope.agent is not None and isinstance(ref, (int, float)) and not isinstance(ref, bool):
            pop = scope.agent[0]
            return [(pop, system.ref(pop.name, ref)[1])]
        raise ExpressionError(f"send to {ref!r}: not an agent; write self, an index, "
                              f"AGENT(\"population\", i) or entity.agent")
    return resolve


def _at(scope: ExpressionScope, pop, j: int) -> ExpressionScope:
    scope.agent = (pop, j)
    return scope


# ---------------------------------------------------------------- vectorised paths (AB-4)
# The array-safe subset, chosen when an expression is compiled: + - * /, a unary
# sign, ONE comparison, self.<attr>, self.id, `self.state ==/!= "<literal>"`,
# and self-free subexpressions (evaluated once, a scalar). No and/or/not/if/in
# (they short-circuit per agent), no chained comparison, no draw and no NEAR
# (they differ per agent without naming self). Anything else keeps the caller's
# `ponytail:` per-agent loop, which stays the reference: numpy runs under
# errstate(all="raise") and ANY exception -- a zero divisor, a string, an
# overflow Python would have let through -- hands the evaluation back to it, so
# results (and errors) are bit-identical either way.
_VEC_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv}
_VEC_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
            ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
_VEC_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_PER_AGENT = frozenset(functions_with("sampler", "grid", "link", "near"))
_NUMBER = (int, float, np.integer, np.floating)
# PHASE6 A9: calls vectorised element by element with the loop's own function.
_VEC_CALLS = {name: FUNCTIONS[name][0] for name in functions_with("vectorised")}


def _is_self(node, attr: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _inline_lets(text: str, lets: dict) -> str:
    """*text* with every bare name of an earlier `let` replaced by that let's
    own (already inlined) expression (#154), so a vectorisable let is part of
    the vectorised set that reads it."""
    if not lets:
        return text
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return text

    class _Inline(ast.NodeTransformer):
        def visit_Name(self, node):
            return ast.parse(lets[node.id], mode="eval").body if node.id in lets else node

        def visit_Call(self, node):         # a function's own name is never a let
            node.args = [self.visit(a) for a in node.args]
            node.keywords = [self.visit(k) for k in node.keywords]
            return node
    return ast.unparse(_Inline().visit(tree))


def vectorise(text: str, names=None, cats=None):
    """`fn(pop, idx, scope) -> array | scalar` evaluating *text* for the agents
    *idx* of *pop* at once (*scope* the population's shared scope), or None
    when *text* is outside the array-safe subset. *cats* (PHASE6 A2): {attr
    name: categories}, so `self.<cat-attr> ==/!= "<literal>"` vectorises as a
    code comparison, the same special case `self.state == "<literal>"` gets."""
    try:
        if compile_expression(text, names).calls & _PER_AGENT:
            return None
    except ExpressionError:
        return None
    return _vec(ast.parse(str(text), mode="eval").body, names, cats or {})


def _vec(node, names, cats):
    if not _reads_self(node):
        if isinstance(node, ast.Constant) and not isinstance(node.value, _NUMBER):
            return None
        scalar = compile_expression(ast.unparse(node), names).closure()

        def shared(pop, idx, scope):
            value = scalar(scope)
            if not isinstance(value, _NUMBER):
                raise TypeError("not a number")         # the loop decides
            return value
        return shared
    if _is_self(node, "id"):
        return lambda pop, idx, scope: idx.astype(float)
    if isinstance(node, ast.Attribute) and _is_self(node, node.attr) and node.attr != "state":
        attr = node.attr
        if attr in cats:
            # A category is never a plain number: reached only OUTSIDE the
            # Eq/NotEq-to-literal special case below (e.g. arithmetic on it),
            # which must fail to vectorise -- the loop then raises the same
            # ExpressionError a bare string always does (bit-identical errors,
            # the AB-4 rule this module already documents).
            return None
        if attr in ("x", "y", "heading"):   # A9: built-ins unless declared (read's order)
            return lambda pop, idx, scope: pop.view()[1][attr][idx] if attr in pop.attrs \
                else _vec_builtin(pop, idx, attr)
        return lambda pop, idx, scope: pop.view()[1][attr][idx]
    if isinstance(node, ast.Call) and node.func.id in _VEC_CALLS and not node.keywords:
        # PHASE6 A9: the same math function per element (np.frompyfunc), so a
        # value equals the loop's bit for bit -- a native np.sin may differ by
        # an ulp. ponytail: a Python call per element, native ufuncs if it binds.
        func = np.frompyfunc(_VEC_CALLS[node.func.id], len(node.args), 1)
        parts = [_vec(a, names, cats) for a in node.args]
        if any(p is None for p in parts):
            return None
        return lambda pop, idx, scope: np.asarray(
            func(*(_arith(p(pop, idx, scope)) for p in parts)), dtype=float)
    if isinstance(node, ast.BinOp) and type(node.op) in _VEC_BIN:
        op, left, right = _VEC_BIN[type(node.op)], _vec(node.left, names, cats), \
            _vec(node.right, names, cats)
        if left is None or right is None:
            return None
        return lambda pop, idx, scope: op(_arith(left(pop, idx, scope)),
                                          _arith(right(pop, idx, scope)))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _VEC_UNARY:
        op, operand = _VEC_UNARY[type(node.op)], _vec(node.operand, names, cats)
        return operand and (lambda pop, idx, scope: op(operand(pop, idx, scope)))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _VEC_CMP:
        op, left, right = _VEC_CMP[type(node.ops[0])], node.left, node.comparators[0]
        if isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            for a, b in ((left, right), (right, left)):
                if not (isinstance(b, ast.Constant) and isinstance(b.value, str)
                        and isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
                        and a.value.id == "self"):
                    continue
                attr = a.attr
                if attr == "state":
                    name = b.value      # an undeclared name matches no code
                    return lambda pop, idx, scope: op(pop.view()[0][idx],
                                                      pop.type.index.get(name, -1))
                if attr in cats:
                    codes, value = cats[attr], b.value
                    code = codes.index(value) if value in codes else -1
                    return lambda pop, idx, scope: op(pop.view()[1][attr][idx], code)
        left, right = _vec(left, names, cats), _vec(right, names, cats)
        if left is None or right is None:
            return None
        return lambda pop, idx, scope: op(left(pop, idx, scope), right(pop, idx, scope))
    return None


def _vec_builtin(pop, idx, attr):
    """`self.heading` / `self.x` / `self.y` of the agents *idx*, live, with
    `AgentSystem._position`'s own arithmetic (PHASE6 A9) -- an agent walking a
    network path is left to the loop (TypeError: the loop decides)."""
    if attr == "heading":
        if pop.heading is None:
            raise TypeError("no heading")
        return pop.heading[idx]
    if pop.type.space is None or pop.path:
        raise TypeError("no position column")
    t = float(pop.system.env.now)
    v0, v1 = (pop.x0[idx], pop.x1[idx]) if attr == "x" else (pop.y0[idx], pop.y1[idx])
    t0, t1 = pop.t0[idx], pop.t1[idx]
    out = np.where((t1 <= t0) | (t >= t1), v1, v0)
    moving = (t1 > t0) & (t < t1) & (t > t0)
    if moving.any():
        f = (t - t0[moving]) / (t1[moving] - t0[moving])
        out[moving] = v0[moving] + (v1[moving] - v0[moving]) * f
    return out


def _arith(value):
    """A comparison's bool array as 0/1 floats: numpy bool + bool is logical-or,
    Python's is 2 (DEVLOG #141)."""
    return value.astype(float) if getattr(value, "dtype", None) == bool else value


def _run_vec(fn, pop, idx, dtype, scope=None):
    """*fn* over *idx* as a *dtype* array, or None: let the loop decide.
    *scope* (default the population's shared one) evaluates the self-free parts."""
    try:
        with np.errstate(all="raise"):
            return np.broadcast_to(np.asarray(fn(pop, idx, scope or pop.shared_scope),
                                              dtype=dtype), idx.shape)
    except Exception:
        return None


class _Transition:
    __slots__ = ("k", "src", "dst", "kind", "value", "own", "shared", "guard", "actions",
                 "text", "scan", "vec", "depth", "keep", "enter", "history", "leaves",
                 "rate_factor")


def _speed_text(entry: dict, units: ModelUnits) -> str:
    """An agent type's speed expression in metres per model time unit (units spec)."""
    fields = SECTION_FIELDS["agent_types"]
    return str(to_base(param(fields, "speed"), entry, units, fields))


def _trigger_in_base(spec: dict, units: ModelUnits) -> tuple[str, float | None, float]:
    """A transition's trigger text, scan interval and rate factor in the model's
    base (units spec, D160). A timeout converts its text and a scan its number;
    a rate keeps its text -- `rate_parts` reads its own * shared shape, which a
    wrapper would hide -- and returns the factor its hazard weight is scaled by."""
    fields = SECTION_FIELDS["agent_types.transitions"]
    kind = next(key for key in TRIGGERS if key in spec)
    value = spec[kind]
    if kind == "timeout":
        value = scaled(value, field_factor(param(fields, "timeout"), spec, units, fields))
    scan = spec.get("scan")
    if scan is not None:
        scan = scaled(scan, field_factor(param(fields, "scan"), spec, units, fields))
    rate = field_factor(param(fields, "rate"), spec, units, fields) if kind == "rate" else 1.0
    return str(value), scan, rate


def _tick_in_base(tick: dict, units: ModelUnits) -> dict:
    """A tick spec with dt/phase in the model's base and its unit dropped, so a
    population's override merges with its type's in one unit (units spec)."""
    fields = SECTION_FIELDS["agent_types.tick"]
    out = {k: v for k, v in tick.items() if k != "time_unit"}
    for name in ("dt", "phase"):
        if out.get(name) is not None:
            out[name] = scaled(out[name], field_factor(param(fields, name), tick, units, fields))
    return out


class _Type:
    """One agent type compiled for a run."""

    def __init__(self, entry: dict, variables: set, units: ModelUnits | None = None):
        self.name = entry["name"]
        self.units = units or ModelUnits()
        self.params = {str(k): v for k, v in (entry.get("parameters") or {}).items()}
        names = set(variables) | set(self.params)
        # Python behaviour (PHASE6 A7, D105): consent-gated, compiled once per run.
        py = (self.name, _load_python(self.name, entry["python"])) if entry.get("python")             else None
        self.py_params = MappingProxyType(dict(self.params))
        # Space and movement (AB-3): validation already refused a bad space,
        # so this only ever runs on one that passed it.
        self.space = entry.get("space")
        self.grid = _Grid(self.space, names, self.name, self.params, self.units) \
            if self.space and self.space.get("kind") == "grid" else None      # PHASE6 A4
        self.speed_fn = compile_expression(_speed_text(entry, self.units), names).closure()
        self.attr_names = [a["name"] for a in entry.get("attributes") or []]
        inits = [a.get("init", "0") for a in entry.get("attributes") or []]
        # {"column": ...} (C3): a data-driven attribute reads its population's
        # own table at construction (`_Population.__init__`), never an
        # expression -- attr_inits holds None for it, skipped everywhere below.
        self.attr_columns = [i.get("column") if isinstance(i, dict) else None for i in inits]
        self.attr_inits = [None if isinstance(i, dict) else compile_expression(i, names)
                           for i in inits]
        # Categorical attributes (PHASE6 A2): name -> its closed category list,
        # stored as that code in the SAME float column a numeric attribute
        # uses (codes are exact in float64) -- one dict answers "is this
        # attribute categorical" everywhere below (read, set, aggregates).
        self.attr_cat = {a["name"]: list(a["categories"]) for a in entry.get("attributes") or []
                         if isinstance(a, dict) and a.get("kind") == "category"}
        self.states = [s["name"] for s in entry["states"]]
        self.index = {name: i for i, name in enumerate(self.states)}
        self._hierarchy(entry["states"])
        self.initial = self.leaf[self.index[entry.get("initial_state") or self.states[0]]]
        self.entry = [self._actions(s.get("entry"), names, self.attr_cat, py=py)
                      for s in entry["states"]]
        self.exit = [self._actions(s.get("exit"), names, self.attr_cat, py=py)
                     for s in entry["states"]]
        # Tick-local values (PHASE6 A9): `let` {name: expr}, evaluated in order
        # once per agent per tick, before its on_tick; each is a scope local (a
        # bare name, `payload`'s shape) for on_tick and the tick transitions.
        self.lets, ticked, inlined = [], set(names), {}
        for lname, text in (entry.get("let") or {}).items():
            self.lets.append((lname, compile_expression(str(text), ticked).closure()))
            ticked.add(lname)
            inlined[lname] = _inline_lets(str(text), inlined)
        self.let_names = frozenset(inlined)
        self.transitions: list[_Transition] = []
        self.out: list[list[_Transition]] = [[] for _ in self.states]
        for k, spec in enumerate(entry.get("transitions") or []):
            tr = _Transition()
            tr.k, tr.src, tr.dst = k, self.index[spec["from"]], self.index[spec["to"]]
            tr.kind = next(key for key in TRIGGERS if key in spec)
            tr.value = tr.own = tr.shared = tr.vec = None
            tr.text, tr.scan, tr.rate_factor = _trigger_in_base(spec, self.units)
            if tr.kind == "rate":
                own, shared = rate_parts(spec["rate"], self.params)
                tr.own = own and compile_expression(own, names).closure()
                tr.shared = shared and compile_expression(shared, names).closure()
            elif tr.kind == "message":      # a NAME, not an expression
                tr.value = tr.text
            elif tr.kind == "tick":         # A1: checked at each tick, "" = always
                tr.value = compile_expression(tr.text, ticked).closure() if tr.text.strip() \
                    else None
            else:
                # a timeout's text is already in the model's base (_trigger_in_base)
                compiled = compile_expression(tr.text if tr.kind == "timeout" else spec[tr.kind],
                                              names)
                tr.value = compiled.closure()
                # a condition checked per agent (a grid call reads self's cell, A4)
                tr.own = "self" in compiled.names or bool(compiled.calls & (_GRID_CALLS
                                                                            | _LINK_CALLS))
                if tr.own and tr.kind == "condition":
                    tr.vec = vectorise(tr.text, names, self.attr_cat)
            here = names | {"payload"} if tr.kind == "message" else \
                ticked if tr.kind == "tick" else names
            tr.guard = compile_expression(spec["guard"], here).closure() \
                if spec.get("guard") else None
            tr.actions = self._actions(spec.get("actions"), here, self.attr_cat, py=py)
            # K1 (D137): keep = how many outer levels the LCA leaves active
            # (the common prefix of both parents' paths); the move exits the
            # active leaf's path below it and enters `enter` (down to dst).
            src_up, dst_up = self.path[tr.src][:-1], self.path[tr.dst][:-1]
            keep = 0
            while keep < min(len(src_up), len(dst_up)) and src_up[keep] == dst_up[keep]:
                keep += 1
            tr.depth, tr.keep, tr.leaves = self.depth[tr.src], keep, self.leaves[tr.src]
            tr.enter, tr.history = self.path[tr.dst][keep:], spec.get("history")
            self.transitions.append(tr)
            self.out[tr.src].append(tr)
        # The transitions live in leaf s's configuration, innermost state first
        # (K1 priority, D138) -- `out` itself for a flat type.
        self.live_out = self.out if not self.nested else \
            [[tr for x in reversed(path) for tr in self.out[x]] for path in self.path]
        # Condition priority in one settle pass: innermost source first, then
        # declaration order -- rank == k for a flat type.
        order = sorted(self.transitions, key=lambda tr: (-tr.depth, tr.k))
        self.rank = [0] * len(order)
        for r, tr in enumerate(order):
            self.rank[tr.k] = r
        self.remember = sorted({tr.dst for tr in self.transitions if tr.history})
        # Column-wise initialisation (AB-4): every agent's attributes first,
        # attribute by attribute (numpy where `vectorise` allows), then entry
        # actions and arming agent by agent. It equals the agent-by-agent order
        # only when nothing evaluated at start reads ANOTHER agent's attribute
        # of a half-initialised population: no entry action (it may send, enter
        # a block or set a variable an init reads) and no SUM/MEAN/AGENT in an
        # init or a timeout (an own rate cannot hold one, D94). Each agent's
        # draws keep their order (its inits, then its arming): its own stream.
        texts = [i for i in inits if not isinstance(i, dict)]
        texts += [tr.text for tr in self.transitions if tr.kind == "timeout"]
        self.columns = not any(self.entry) and not any(
            compile_expression(text, names).calls & {"SUM", "MEAN", "AGENT", "LINK_SUM",
                                                     "LINK_MEAN"} for text in texts)
        # A categorical attribute's OWN init never vectorises (PHASE6 A2): the
        # columnar path below assigns the result straight into the column,
        # with no `_coerce_attr` check that it is one of the declared codes
        # -- so a bare number literal, invalid on a category, would otherwise
        # slip through uncaught. Reading one via self.<other-attr> inside
        # ANOTHER init is still safe: `_vec`'s own attr-in-cats guard refuses it.
        self.attr_vecs = [vectorise(init.text, names, self.attr_cat)
                          if self.columns and init is not None and name not in self.attr_cat
                          else None
                          for name, init in zip(self.attr_names, self.attr_inits)]
        # Ticks (PHASE6 A1, D99). `tick_vecs`: when every on_tick action is a
        # self set that vectorises, a synchronous tick runs them column-wise
        # (all reads are the tick-start snapshot, so agent order cannot matter).
        self.tick = entry.get("tick") if isinstance(entry.get("tick"), dict) else {}
        spec = entry.get("on_tick") or []
        self.on_tick = self._actions(spec, ticked, self.attr_cat, in_tick=True, py=py)
        self.tick_out = [[tr for tr in out if tr.kind == "tick"] for out in self.live_out]
        self.ticking = bool(self.on_tick) or any(self.tick_out)
        vecs = []
        for action in spec:
            target = str(action.get("target", "")).strip()
            kind, name = parse_target(target) if target and not target.startswith("patch.") \
                else (None, None)
            # A categorical TARGET (PHASE6 A2) always falls back to the loop:
            # the columnar path below assigns the vectorised result straight
            # into the column, with no `_coerce_attr` check that it is one of
            # the declared codes. `heading` (PHASE6 A6) is not a column of
            # `pop.attrs` either -- the same commit line would KeyError.
            vec = vectorise(_inline_lets(str(action["expr"]), inlined), names, self.attr_cat) \
                if kind == "self" and name not in self.attr_cat and name != "heading" else None
            if vec is None:
                vecs = []
                break
            vecs.append((name, vec))
        # #154: a let that vectorises is inlined into the sets reading it (the
        # same float ops as the loop's two steps) and computed once more on its
        # own, so its errors still reach the loop; a let that draws: the loop.
        let_vecs = [vectorise(text, names, self.attr_cat) for text in inlined.values()]
        self.tick_vecs = vecs if all(v is not None for v in let_vecs) else []
        self.let_vecs = let_vecs if self.tick_vecs else []

    def _hierarchy(self, specs: list) -> None:
        """Composite states (K1, D136): a state is composite iff another names
        it as `parent`. Per state: `parent` (-1 at the top), `path` (root ..
        itself), `depth`, `first` (a composite's initial child, else -1),
        `leaf` (where entering it lands through the `initial`s) and `leaves`
        (a composite's descendant leaves as an array, None for a leaf).
        Validation already refused unknown parents, cycles and bad `initial`s."""
        n = len(self.states)
        self.parent = [self.index[s["parent"]] if s.get("parent") else -1 for s in specs]
        self.nested = any(p >= 0 for p in self.parent)
        children = [[] for _ in range(n)]
        for s, p in enumerate(self.parent):
            if p >= 0:
                children[p].append(s)
        self.path = []
        for s in range(n):
            chain, x = [], s
            while x >= 0 and x not in chain:        # never loops, even unvalidated
                chain.append(x)
                x = self.parent[x]
            self.path.append(tuple(reversed(chain)))
        self.depth = [len(path) - 1 for path in self.path]
        self.levels = max(len(path) for path in self.path)
        self.first = [(self.index[spec["initial"]] if spec.get("initial") else kids[0])
                      if kids else -1 for spec, kids in zip(specs, children)]
        self.leaf = []
        for s in range(n):
            while self.first[s] >= 0:
                s = self.first[s]
            self.leaf.append(s)
        self.leaves = [np.array([x for x in range(n) if not children[x] and s in self.path[x]],
                                dtype=np.int16) if children[s] else None for s in range(n)]
        self.composites = [s for s in range(n) if children[s]]

    def mask(self, states: np.ndarray, s: int) -> np.ndarray:
        """Which of *states* (leaf codes) are in state s: s itself, or any leaf
        under a composite s (K1: COUNT/SUM/MEAN/NEAR... match descendants)."""
        leaves = self.leaves[s]
        return states == s if leaves is None else np.isin(states, leaves)

    @staticmethod
    def _actions(items, names, cats=None, in_tick=False, py=None) -> list:
        return [_compile_action(action, names, cats, in_tick, py) for action in items or []]
