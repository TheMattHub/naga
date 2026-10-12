from __future__ import annotations

"""
System dynamics loop/tree analysis (PHASE6 C6/S4): feedback loops (`loops`)
and Vensim-style causes/uses trees (`causes`/`uses`) on the stock-and-flow
dependency graph.

Split out of core/sd.py (C3.4): that module stays the model-building side
(expand/flatten/validate); this one is the read-only analysis over an
already-built layout. `dependency_graph`/`refs`/`StubOutput`/
`elementary_circuits` are public (used by `nagasim/loopdominance.py`, D141,
for loop dominance over a real run instead of the polarity probe here).
"""
from dataclasses import dataclass
from types import SimpleNamespace

from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression, lookup_sections
from nagasim.core.sd import _flatten, _q, build_auxiliaries, split_name

# The same (section, field) map `ui/sdview.py` draws influence arrows from --
# duplicated (four lines) rather than imported, so this stays UI-free.
_READING_FIELDS = {"levels": ("initial",), "flows": ("rate",), "auxiliaries": ("expr",),
                   "delays": ("input", "delay_time")}
_FUNC_SECTION = lookup_sections("LEVEL", "AUX", "DELAY")
_NODE_SECTIONS_ALL = ("levels", "flows", "auxiliaries", "delays")
_LOOP_KIND = {"+": "R", "-": "B", "?": "?"}
_REL_STEP = 1e-3          # finite-difference step, relative to the probed value


@dataclass
class Loop:
    """One elementary feedback loop (`loops()`): *nodes* is `[(section,
    name), ...]` in cycle order (the edge back to `nodes[0]` closes it);
    *kind* is "R" (reinforcing), "B" (balancing) or "?" (a `?`-polarity link
    made the loop's own sign undecidable)."""

    id: str
    kind: str
    nodes: list
    length: int


def _active(layout: dict, section: str) -> list:
    return [r for r in layout.get(section) or [] if isinstance(r, dict) and r.get("name")
           and not r.get("hidden")]


def refs(text) -> dict:
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        return compile_expression(text).refs
    except ExpressionError:
        return {}


class StubOutput:
    """What `DELAY("name")` reads in the probe context: a constant standing
    in for the delay's t=0 output (its declared `initial`), not the real
    staged/pipeline machinery in `core/levels.py` -- rebuilding that here
    would need a live simpy run, which a standalone polarity probe does not
    have. Documented limitation: a delay's OWN reading edges (something that
    reads `DELAY("x")`) always probe against this constant, so a link into a
    delay's output is exact only when that link does not depend on the
    delay's internal dynamics -- fine for polarity, which only asks "does
    raising the input raise or lower what reads it"."""

    __slots__ = ("value",)

    def __init__(self, value: float):
        self.value = value

    def evaluate(self, _own):
        return self.value


def _initial_levels(layout: dict, auxes: dict) -> dict:
    """{level name: float} at t=0. A numeric `initial` is used as is; a
    string one (SD-2) is evaluated once against every OTHER level's own
    numeric-or-zero value -- exact for the common case (independent numeric
    initials, or one that reads only auxiliaries), approximate when two
    levels' initials read each other (rare; falls back to 0.0 on any
    evaluation error instead of chasing the dependency order)."""
    records = _active(layout, "levels")
    values = {}
    for record in records:
        initial = record.get("initial", 0.0)
        if isinstance(initial, (int, float)) and not isinstance(initial, bool):
            values[str(record["name"])] = float(initial)
    stub_levels = {str(r["name"]): SimpleNamespace(level=values.get(str(r["name"]), 0.0))
                   for r in records}
    ctx = SimpleNamespace(now=0.0, aux_memo={}, auxiliaries=auxes,
                          level_system=SimpleNamespace(levels=stub_levels, delays={},
                                                       _last_t=0.0, env=SimpleNamespace(now=0.0)))
    for record in records:
        name = str(record["name"])
        if name in values:
            continue
        try:
            expr = compile_expression(str(record.get("initial", 0.0)))
            values[name] = float(expr.evaluate(ExpressionScope(ctx=ctx)))
        except Exception:
            values[name] = 0.0
    return values


def _probe_ctx(layout: dict):
    """A minimal stand-in for the runtime `ctx` `LEVEL()`/`AUX()`/`DELAY()`
    read (`core/expressions.py`): plain values instead of the live
    `LevelSystem`, so a reading expression can be evaluated at t=0 -- and
    re-evaluated with one input nudged -- without running the model."""
    auxes = build_auxiliaries(layout)
    levels = _initial_levels(layout, auxes)
    stub_levels = {name: SimpleNamespace(level=value) for name, value in levels.items()}
    stub_delays = {}
    for record in _active(layout, "delays"):
        initial = record.get("initial", 0.0)
        value = float(initial) if (isinstance(initial, (int, float))
                                   and not isinstance(initial, bool)) else 0.0
        stub_delays[str(record["name"])] = (StubOutput(value), None)
    return SimpleNamespace(now=0.0, aux_memo={}, auxiliaries=auxes,
                           level_system=SimpleNamespace(levels=stub_levels, delays=stub_delays,
                                                        _last_t=0.0, env=SimpleNamespace(now=0.0)))


def _base_value(ctx, section: str, name: str) -> float:
    if section == "levels":
        return ctx.level_system.levels[name].level
    if section == "delays":
        return ctx.level_system.delays[name][0].value
    return float(compile_expression(f'AUX({_q(name)})').evaluate(ExpressionScope(ctx=ctx)))


def _set_origin(ctx, section: str, name: str, value: float) -> None:
    if section == "levels":
        ctx.level_system.levels[name].level = value
    elif section == "delays":
        ctx.level_system.delays[name] = (StubOutput(value), None)
    else:
        ctx.aux_memo[name] = value


def _probe_polarity(ctx, origin_section: str, origin_name: str, target_text: str,
                    target_locals) -> str:
    """"+"/"-"/"?" for the link ORIGIN -> (the record whose *target_text*
    field reads it): a central finite difference of *target_text* around
    ORIGIN's t=0 value, ORIGIN nudged by +-0.1% of its own value (or of 1.0,
    at 0). "?" when the probe cannot be evaluated on either side, or the two
    sides differ by less than float noise -- a flat or undecidable link,
    which a single point probe cannot tell from a sign change elsewhere in
    the state space (HEURISTIC, documented on `loops`)."""
    try:
        target_expr = compile_expression(str(target_text))
    except ExpressionError:
        return "?"
    base = _base_value(ctx, origin_section, origin_name)
    step = max(abs(base), 1.0) * _REL_STEP

    def value_at(v):
        ctx.aux_memo = {}           # the perturbation may reach through other auxiliaries
        _set_origin(ctx, origin_section, origin_name, v)
        try:
            return float(target_expr.evaluate(ExpressionScope(ctx=ctx, locals=target_locals)))
        except Exception:
            return None

    hi, lo = value_at(base + step), value_at(base - step)
    _set_origin(ctx, origin_section, origin_name, base)
    ctx.aux_memo = {}
    if hi is None or lo is None:
        return "?"
    delta = hi - lo
    if abs(delta) < 1e-9 * max(abs(hi), abs(lo), 1.0):
        return "?"
    return "+" if delta > 0 else "-"


def elementary_circuits(graph: dict, limit: int):
    """Elementary circuits of *graph* (`{node: {neighbours}}`), each
    returned once as a tuple of nodes in visiting order. Johnson's (1975)
    reduction -- a circuit is only searched for from its OWN smallest node
    (`sorted(graph)` order), so it is never found twice -- without Johnson's
    blocked-node pruning: a plain DFS. ponytail: worse worst-case time on a
    dense graph, fine for one model's levels/flows/auxiliaries/delays, and
    *limit* (checked before every recursive step) bounds it regardless; add
    the blocking sets if a real model ever needs the full algorithm's
    complexity guarantee. Returns (circuits, truncated)."""
    order = {n: i for i, n in enumerate(sorted(graph))}
    circuits: list = []
    truncated = False

    def dfs(start, node, path, on_path):
        nonlocal truncated
        for nxt in sorted(graph.get(node, ())):
            if truncated:
                return
            if order[nxt] < order[start]:
                continue          # a smaller node would have started this circuit
            if nxt == start:
                circuits.append(tuple(path))
                if len(circuits) >= limit:
                    truncated = True
                    return
            elif nxt not in on_path:
                path.append(nxt)
                on_path.add(nxt)
                dfs(start, nxt, path, on_path)
                on_path.discard(nxt)
                path.pop()

    for start in sorted(graph, key=order.__getitem__):
        if truncated:
            break
        dfs(start, start, [start], {start})
    return circuits, truncated


def dependency_graph(layout: dict, probe: bool = True) -> tuple[set, dict, dict]:
    """Nodes, edges and the origin -> targets adjacency of the stock-and-flow
    dependency graph (PHASE6 C6): levels, flows, auxiliaries and delays as
    nodes, shared by `loops()`, `causes()` and `uses()` (PHASE6 S4).

    EDGES. A flow's own `from`/`to` (exact, no probe): flow -> the level it
    fills ("+", an inflow always raises the target) and flow -> the level it
    drains ("-", an outflow always lowers the source). Every LEVEL()/AUX()/
    DELAY() a level's `initial`, a flow's `rate`, an auxiliary's `expr` or a
    delay's `input`/`delay_time` reads: origin -> the reading record, polarity
    from `_probe_polarity` -- a finite-difference probe of the reading
    expression around the model's INITIAL state.

    HEURISTIC AND ITS LIMITS. The probe is a single point (t=0): a link whose
    sign is genuinely constant is read correctly, but one that changes sign
    elsewhere in the state space (a saturating MIN/MAX, a sign flip past some
    threshold) is reported at its initial-state sign only, and a link that
    happens to be flat exactly at t=0 (or fails to evaluate -- an unresolved
    TABLE/RESOURCE/rng call, say) comes back "?". `DELAY()` reads its
    declared `initial` rather than the real staged output.

    *probe* False skips the probe (every read edge is "?"): loop dominance
    (`nagasim/loopdominance.py`, D141) reads polarity from a run instead.

    Returns (`nodes` as `{(section, name), ...}`, `edges` as `{(origin,
    target): "+"/"-"/"?"}`, `graph` as `{node: {target, ...}}`, every node
    a key even with no outgoing edge)."""
    if layout.get("subscripts"):
        layout = _flatten(layout)
    nodes = {(section, str(r["name"])) for section in _NODE_SECTIONS_ALL
             for r in _active(layout, section)}

    edges: dict = {}       # (origin, target) -> "+"/"-"/"?"
    for record in _active(layout, "flows"):
        target = ("flows", str(record["name"]))
        for side, sign in (("to", "+"), ("from", "-")):
            level_name = record.get(side)
            if isinstance(level_name, str) and ("levels", level_name) in nodes:
                edges[(target, ("levels", level_name))] = sign

    ctx = _probe_ctx(layout) if probe else None
    for section, fields in _READING_FIELDS.items():
        for record in _active(layout, section):
            target = (section, str(record["name"]))
            for field in fields:
                text = record.get(field)
                text_refs = refs(text)
                for func, origin_section in _FUNC_SECTION.items():
                    for raw_name in text_refs.get(func, ()):
                        # A flattened element ("Pop[north]") is a node under
                        # its own name; only an unflattened read maps to its base.
                        name = raw_name if (origin_section, raw_name) in nodes                             else split_name(raw_name)[0]
                        origin = (origin_section, name)
                        if origin == target or origin not in nodes:
                            continue
                        key = (origin, target)
                        if key in edges:
                            continue
                        edges[key] = "?" if ctx is None else _probe_polarity(
                            ctx, origin_section, name, text, record.get("locals"))

    graph: dict = {node: set() for node in nodes}
    for origin, target in edges:
        graph[origin].add(target)
    return nodes, edges, graph


def loops(layout: dict, limit: int = 200) -> tuple[list, bool]:
    """Feedback loops on the stock-and-flow graph (PHASE6 C6): levels, flows,
    auxiliaries and delays as nodes. See `dependency_graph` for how an edge
    (and its polarity) is derived, and the heuristic's limits.

    ELEMENTARY CIRCUITS up to *limit* (`elementary_circuits`); a model with
    more of them is truncated rather than enumerated in full (many SD models
    have combinatorially many cycles through a handful of hub auxiliaries).
    A loop's polarity is the product of its edges': an even number of "-"
    edges is reinforcing ("R"), odd is balancing ("B"); any "?" edge along it
    makes the whole loop "?". Ids are assigned shortest-loop-first (R1 is the
    shortest reinforcing loop, and so on) so they read the way a modeller
    would name them by hand.

    Returns (`[Loop, ...]`, truncated).
    """
    _nodes, edges, graph = dependency_graph(layout)
    circuits, truncated = elementary_circuits(graph, max(int(limit), 1))
    found = [Loop(id="", kind=_LOOP_KIND[_loop_polarity(circuit, edges)], nodes=list(circuit),
                 length=len(circuit)) for circuit in circuits]
    found.sort(key=lambda l: (l.length, l.nodes))
    counters = {"R": 0, "B": 0, "?": 0}
    for loop in found:
        counters[loop.kind] += 1
        loop.id = f"{loop.kind}{counters[loop.kind]}"
    return found, truncated


def _loop_polarity(circuit: tuple, edges: dict) -> str:
    polarity = "+"
    n = len(circuit)
    for i in range(n):
        sign = edges.get((circuit[i], circuit[(i + 1) % n]), "?")
        if sign == "?":
            return "?"
        if sign == "-":
            polarity = "-" if polarity == "+" else "+"
    return polarity


# ---------------------------------------------------------------- causes/uses trees (PHASE6 S4)
KIND = {"levels": "level", "flows": "flow", "auxiliaries": "auxiliary", "delays": "delay"}


@dataclass
class TreeNode:
    """One node of a `causes()`/`uses()` tree (Vensim's Causes Tree / Uses
    Tree): *kind* is the singular section name (level/flow/auxiliary/delay);
    *cycle* marks a node that loops back to one of its own ancestors -- shown
    once, not expanded further, so the tree always terminates."""

    name: str
    kind: str
    children: list
    cycle: bool = False


def _find_node(nodes: set, name: str):
    for section in _NODE_SECTIONS_ALL:
        if (section, name) in nodes:
            return (section, name)
    return None


def _reverse_graph(graph: dict) -> dict:
    reverse: dict = {node: set() for node in graph}
    for origin, targets in graph.items():
        for target in targets:
            reverse[target].add(origin)
    return reverse


def _walk_tree(node: tuple, neighbours: dict, depth: int, ancestors: frozenset) -> TreeNode:
    section, name = node
    cycle = node in ancestors
    if cycle or depth <= 0:
        return TreeNode(name=name, kind=KIND[section], children=[], cycle=cycle)
    children = [_walk_tree(child, neighbours, depth - 1, ancestors | {node})
               for child in sorted(neighbours.get(node, ()))]
    return TreeNode(name=name, kind=KIND[section], children=children)


def causes(layout: dict, name: str, depth: int = 3) -> TreeNode | None:
    """Vensim's Causes Tree: what *name* (a level/flow/auxiliary/delay)
    depends on, recursively, down to *depth* levels -- the predecessors of
    `dependency_graph`'s edges (an edge origin -> target reads as "origin is
    a cause of target"). Returns None if *name* names no node."""
    nodes, _edges, graph = dependency_graph(layout)
    node = _find_node(nodes, name)
    if node is None:
        return None
    return _walk_tree(node, _reverse_graph(graph), max(int(depth), 0), frozenset())


def uses(layout: dict, name: str, depth: int = 3) -> TreeNode | None:
    """Vensim's Uses Tree: what reads *name*, recursively, down to *depth*
    levels -- the successors of `dependency_graph`'s edges. See `causes` for
    the cycle marker and the return value."""
    nodes, _edges, graph = dependency_graph(layout)
    node = _find_node(nodes, name)
    if node is None:
        return None
    return _walk_tree(node, graph, max(int(depth), 0), frozenset())
