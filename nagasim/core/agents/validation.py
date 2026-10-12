"""Structured validation of the agent sections: `validate_structured` (what
`core.schema` calls) and the checks blocks and the UI reuse
(`target_problems`, `literal_problems`, `bad_aggregates`, `entered_blocks`,
`rewrite_state_references`). Agent Python is only parsed here (`python_check`)."""
from __future__ import annotations

import ast
import math

from nagasim.core.agents.common import (ACTIVATIONS, NETWORK_KINDS, TRIGGERS, _EDGES, _GRID_CALLS,
                                        _NEIGHBOURHOODS, _READ_ONLY, _SAMPLER_CALLS,
                                        _resolve_count, _table_column, rate_parts, timing)
from nagasim.core.agents.python import python_check
from nagasim.core.expressions import (RESERVED_NAMES, ExpressionError, compile_expression,
                                      parse_target)
from nagasim.core.levels import TRANSFER_MODES
from nagasim.core.problems import problem


def _self_reads(text: str, who: str = "self") -> set:
    return {n.attr for n in ast.walk(ast.parse(str(text), mode="eval"))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == who}


def _state_literals(text: str) -> set:
    """The string literals *text* compares `self.state` with (==/!=). K review:
    `self.state` is the LEAF name, so one naming a composite never matches."""
    out = set()
    for n in ast.walk(ast.parse(str(text), mode="eval")):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 \
                and isinstance(n.ops[0], (ast.Eq, ast.NotEq)):
            for a, b in ((n.left, n.comparators[0]), (n.comparators[0], n.left)):
                if isinstance(a, ast.Attribute) and a.attr == "state" and isinstance(a.value, ast.Name) \
                        and a.value.id == "self" and isinstance(b, ast.Constant) \
                        and isinstance(b.value, str):
                    out.add(b.value)
    return out


def _builtin_reads(kind) -> set:
    """The read-only built-ins `self.<x>` has on agent type *kind* (raw
    entry): state and id always, heading on a continuous space (A6), and the
    current position x, y on any space (PHASE6 A9) unless declared attributes."""
    space = (kind or {}).get("space")
    return set(_READ_ONLY) | ({"heading"} if _has_heading(kind) else set()) | \
        ({"x", "y"} if isinstance(space, dict) else set())


def _agent_reads(text: str) -> list:
    """(population, attribute) of every `AGENT("<literal>", i).<attr>` read."""
    return [(n.value.args[0].value, n.attr) for n in ast.walk(ast.parse(str(text), mode="eval"))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id == "AGENT"
            and n.value.args and isinstance(n.value.args[0], ast.Constant)
            and isinstance(n.value.args[0].value, str)]


def _attr_names(entry) -> set:
    return {a.get("name") for a in (entry or {}).get("attributes") or [] if isinstance(a, dict)}


def _has_heading(kind) -> bool:
    """Whether agent type *kind* (its raw JSON entry) has a built-in
    `heading` (PHASE6 A6, D104): only a continuous space, not a grid or
    network one -- a cell move or a path walk has no free-standing direction
    to steer. 0 degrees = east, counter-clockwise (NetLogo's own turtles use
    0 = north, clockwise -- the opposite convention; a ported model negates
    and offsets by 90)."""
    space = (kind or {}).get("space")
    return isinstance(space, dict) and space.get("kind") == "continuous"


def bad_aggregates(text: str) -> list:
    """The two- or three-argument SUM/MEAN calls in *text* whose arguments are
    not literal names (population, attribute[, state]): E_AGGREGATE_AGENTS."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError:
        return []
    return [n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("SUM", "MEAN") and len(n.args) >= 2
            and not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in n.args)]


# The literal argument of an agent lookup that names a state or an attribute
# of the population's type (DEVLOG #134): its position and what it names.
# COUNT is checked separately (`_count_problems`): its optional args are a
# category filter, COUNT(pop, attr, value) or COUNT(pop, state, attr, value)
# -- the smallest extension of the existing (population, state) shape rather
# than a second aggregate function, since SUM/MEAN take no expression
# argument to write a "self.attr == value" filter with (their 2nd/3rd args
# are already literal names, not expressions) -- but which meaning args[1:]
# has depends on how many there ARE, not a fixed position, so it needs its
# own dispatch rather than this table's one-position-per-key shape.
_NAMED_ARGS = {"NEAR": ((2, "state"),), "NEAREST": ((1, "state"),),
               "NEAR_HEADING": ((2, "state"),),        # PHASE6 A6: same shape as NEAR
               "SUM": ((1, "attribute"), (2, "state")), "MEAN": ((1, "attribute"), (2, "state")),
               # Neighbour aggregates (PHASE6 A3): pop, attr, r-or-k[, state] --
               # the same (attribute, state) positions as SUM/MEAN, one slot
               # later to make room for the radius/k argument.
               "NEAR_SUM": ((1, "attribute"), (3, "state")),
               "NEAR_MEAN": ((1, "attribute"), (3, "state")),
               "NEAR_MIN": ((1, "attribute"), (3, "state")),
               "NEAR_MAX": ((1, "attribute"), (3, "state")),
               "KNN_SUM": ((1, "attribute"), (3, "state")),
               "KNN_MEAN": ((1, "attribute"), (3, "state")),
               # Grid neighbourhoods (PHASE6 A4): no radius, SUM/MEAN's positions.
               "NEIGHBOURS": ((1, "state"),),
               "NEIGHBOUR_SUM": ((1, "attribute"), (2, "state")),
               "NEIGHBOUR_MEAN": ((1, "attribute"), (2, "state")),
               "LINKED": ((1, "state"),)}                           # PHASE6 A5
# Every aggregate whose "attribute" argument must be numeric, not categorical
# (PHASE6 A2's rule, extended to the neighbour aggregates, A3/A4).
_NUMERIC_AGGREGATES = frozenset({"SUM", "MEAN", "NEAR_SUM", "NEAR_MEAN", "NEAR_MIN", "NEAR_MAX",
                                 "KNN_SUM", "KNN_MEAN", "NEIGHBOUR_SUM", "NEIGHBOUR_MEAN"})


def _attr_entry(kind: dict, name: str) -> dict | None:
    return next((a for a in kind.get("attributes") or []
                if isinstance(a, dict) and a.get("name") == name), None)


def _count_problems(node: ast.Call, pops: dict) -> list:
    """COUNT's own literal checks (PHASE6 A2): the arg SHAPE, not a fixed
    position, says what args[1:] mean -- COUNT(pop[, state]) (unchanged) or
    COUNT(pop, attr, value[, state after attr]) is really COUNT(pop, attr,
    value) / COUNT(pop, state, attr, value), matching how many args are
    given, the same dual-meaning-by-arg-count idiom SUM/MEAN's own 2-vs-3-arg
    shape already uses (rather than a position that shifts what it means)."""
    args = node.args
    if len(args) < 2 or not isinstance(args[0], ast.Constant) or not isinstance(args[0].value, str):
        return []
    pop_name = args[0].value
    kind = pops.get(pop_name)
    if not isinstance(kind, dict):
        return []
    out = []
    n = len(args)
    if n in (2, 4):
        state = args[1]
        if isinstance(state, ast.Constant) and isinstance(state.value, str):
            declared = [st.get("name") for st in kind.get("states") or [] if isinstance(st, dict)]
            if state.value not in declared:
                return [("E_AGENT_STATE", f"COUNT(\"{pop_name}\", ...): '{kind.get('name')}' has "
                        f"no state '{state.value}'",
                        f"declared: {', '.join(map(str, declared)) or '(none)'}")]
    if n in (3, 4):
        at = 1 if n == 3 else 2
        attr_arg, value_arg = args[at], args[at + 1]
        if not (isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str)):
            return out
        attr = _attr_entry(kind, attr_arg.value)
        if attr is None or attr.get("kind") != "category":
            out.append(("E_AGENT_ATTRIBUTE", f"COUNT(\"{pop_name}\", ..., \"{attr_arg.value}\", "
                        f"...): '{attr_arg.value}' is not a categorical attribute of "
                        f"'{kind.get('name')}'", "kind: \"category\""))
        elif isinstance(value_arg, ast.Constant) and isinstance(value_arg.value, str) \
                and value_arg.value not in (attr.get("categories") or []):
            out.append(("E_AGENT_ATTRIBUTE", f"COUNT(\"{pop_name}\", ..., \"{attr_arg.value}\", "
                        f"\"{value_arg.value}\"): not one of the declared categories",
                        f"declared: {', '.join(attr.get('categories') or [])}"))
    return out


def literal_problems(text: str, pops: dict) -> list:
    """(code, message, hint) for each literal state/attribute argument of
    COUNT/NEAR/NEAREST/SUM/MEAN/NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX/
    KNN_SUM/KNN_MEAN in *text* that the named population's type does not
    declare (`state`/`id` are no attribute columns); a numeric aggregate
    (`_NUMERIC_AGGREGATES`, PHASE6 A3) of a categorical attribute is the
    same E_AGENT_ATTRIBUTE (PHASE6 A2, COUNT's
    own category-filter checks are `_count_problems`). *pops* maps a
    population to its agent type entry."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id == "COUNT":
            out.extend(_count_problems(node, pops))
            continue
        if node.func.id not in _NAMED_ARGS:
            continue
        args = node.args
        for at, what in _NAMED_ARGS[node.func.id]:
            if len(args) <= at or not all(isinstance(a, ast.Constant)
                                          and isinstance(a.value, str) for a in (args[0], args[at])):
                continue
            kind = pops.get(args[0].value)
            if not isinstance(kind, dict):
                continue
            declared = ([st.get("name") for st in kind.get("states") or []
                         if isinstance(st, dict)]
                        if what == "state" else sorted(_attr_names(kind)))
            if args[at].value not in declared:
                out.append(("E_AGENT_STATE" if what == "state" else "E_AGENT_ATTRIBUTE",
                            f"{node.func.id}(\"{args[0].value}\", ...): '{kind.get('name')}' has "
                            f"no {what} '{args[at].value}'",
                            f"declared: {', '.join(map(str, declared)) or '(none)'}"))
                continue
            if what == "attribute" and node.func.id in _NUMERIC_AGGREGATES:
                attr = _attr_entry(kind, args[at].value)
                if attr is not None and attr.get("kind") == "category":
                    out.append(("E_AGENT_ATTRIBUTE", f"{node.func.id}(\"{args[0].value}\", "
                                f"\"{args[at].value}\", ...): '{args[at].value}' is a categorical "
                                f"attribute, not a number",
                                f"use COUNT(\"{args[0].value}\", \"{args[at].value}\", "
                                f"\"<category>\") to count by category"))
    return out


def rewrite_state_literal(text: str, pop_names: frozenset, old: str, new: str) -> str:
    """*text* with every literal *old* state argument of a COUNT/NEAR/NEAREST/
    NEAR_HEADING/SUM/MEAN/NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX/KNN_SUM/
    KNN_MEAN/NEIGHBOURS/NEIGHBOUR_SUM/NEIGHBOUR_MEAN/LINKED call naming a
    population in *pop_names* rewritten to *new* -- the very call shapes and
    argument positions `literal_problems`/`_count_problems` above validate,
    read here instead of checked (a state rename's expression half, DEVLOG
    #245). *text* is left untouched if it isn't a single expression, or if
    nothing matches: a computed population/state name is as unprovable here
    as it is to those two checks, so it is never touched."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except (SyntaxError, ValueError):
        return text
    spans = []  # (start, end, quote, value), applied back to front by offset
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        args = node.args
        if not args or not (isinstance(args[0], ast.Constant) and args[0].value in pop_names):
            continue
        if node.func.id == "COUNT":
            state_args = [args[1]] if len(args) in (2, 4) else []
        else:
            state_args = [args[at] for at, what in _NAMED_ARGS.get(node.func.id, ())
                          if what == "state" and len(args) > at]
        for arg in state_args:
            if isinstance(arg, ast.Constant) and arg.value == old and arg.end_col_offset is not None:
                spans.append((arg.col_offset, arg.end_col_offset, text[arg.col_offset], new))
    if not spans:
        return text
    out = text
    for start, end, quote, value in sorted(spans, reverse=True):
        out = out[:start] + f"{quote}{value}{quote}" + out[end:]
    return out


def rewrite_state_references(value, pop_names: frozenset, old: str, new: str):
    """*value* (a JSON-shaped agent type, dashboard or block-params record)
    with every literal *old* state name rewritten to *new* wherever it
    provably names a state of a population in *pop_names*: a `{"spawn": pop,
    "state": old}` action (any agent type's entry/exit/on_tick/transition
    actions can spawn into a population of another type), or a COUNT-family
    literal argument (`rewrite_state_literal`). Returns (rewritten value,
    whether anything changed); the caller decides whether that is worth an
    upsert. DEVLOG #245: `rename_state` (product/ui/statechart.py) is the
    caller -- its own type's states/transitions/initial are simple field
    renames done there, not here."""
    if isinstance(value, dict):
        changed = False
        out = {}
        spawn = value.get("spawn")
        for key, sub in value.items():
            if key == "state" and isinstance(sub, str) and spawn in pop_names and sub == old:
                out[key] = new
                changed = True
                continue
            new_sub, sub_changed = rewrite_state_references(sub, pop_names, old, new)
            out[key] = new_sub
            changed = changed or sub_changed
        return out, changed
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            new_item, item_changed = rewrite_state_references(item, pop_names, old, new)
            out.append(new_item)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, str):
        new_value = rewrite_state_literal(value, pop_names, old, new)
        return new_value, new_value != value
    return value, False


def _link_problems(text: str, kind: dict) -> list:
    """(code, message, hint) for a literal attribute/state of LINK_SUM/
    LINK_MEAN("attr"[, "state"]) (PHASE6 A5) that self's own type *kind* does
    not declare, or a categorical attribute (A2's numeric-aggregate rule):
    links join agents of one population, so the neighbours are *kind* too."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError:
        return []
    states = [s.get("name") for s in kind.get("states") or [] if isinstance(s, dict)]
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("LINK_SUM", "LINK_MEAN")):
            continue
        args = [a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
                for a in node.args[:2]]
        attr = _attr_entry(kind, args[0]) if args and args[0] else None
        if args and args[0] and (attr is None or attr.get("kind") == "category"):
            out.append(("E_AGENT_ATTRIBUTE", f"{node.func.id}(\"{args[0]}\", ...): not a numeric "
                        f"attribute of '{kind.get('name')}'",
                        f"declared: {', '.join(sorted(map(str, _attr_names(kind)))) or '(none)'}"))
        if len(args) > 1 and args[1] and args[1] not in states:
            out.append(("E_AGENT_STATE", f"{node.func.id}(..., \"{args[1]}\"): '{kind.get('name')}' "
                        f"has no state '{args[1]}'", f"declared: {', '.join(map(str, states))}"))
    return out


def entered_blocks(layout: dict) -> set:
    """The blocks an agent `enter` action feeds: entity sources, like CREATE."""
    found = set()
    for kind in layout.get("agent_types") or []:
        if not isinstance(kind, dict):
            continue
        lists = [tr.get("actions") for tr in kind.get("transitions") or [] if isinstance(tr, dict)]
        lists += [s.get(key) for s in kind.get("states") or [] if isinstance(s, dict)
                  for key in ("entry", "exit")]
        found |= {a["enter"] for items in lists for a in items or []
                  if isinstance(a, dict) and isinstance(a.get("enter"), str)}
    return found


_TARGET_HINT = ('"self", an index, AGENT("Pop", i), entity.agent, "links", or {"population": '
                '"Pop" | "links": true[, "where": "<condition on self>"][, "random": true]}')


def target_problems(to, who: str, path: str, pops: dict, variables, agent: bool, expr,
                    check=None, own=None) -> list:
    """E_SEND_TARGET / E_POPULATION_UNKNOWN / the `where` of a send's `to`
    (PHASE5 AB-2), shared by the `send` action and the SEND block. *pops* maps
    a population to its agent type entry; *expr(text, path)* checks an
    expression written in the SENDER's scope; *check(who, text, names, path)*
    runs the model-wide literal checks on the `where` (DEVLOG #133). *own* is
    the sender's type entry: a `links` target (PHASE6 A5) reaches agents of
    the sender's own population, so its `where` reads that type."""
    out: list = []
    if to == "links" or isinstance(to, dict) and "links" in to:        # PHASE6 A5
        if not agent:
            return [problem("E_SEND_TARGET", f"{who}: \"links\" is a target only inside an "
                            f"agent type", path, hint=_TARGET_HINT)]
        if to == "links":
            return out
        if (to["links"] is not True or set(to) - {"links", "where", "random"}
                or not isinstance(to.get("random", False), bool)):
            return [problem("E_SEND_TARGET", f"{who}: to {to!r} is malformed", path,
                            hint='{"links": true, "where"?: "...", "random"?: true}')]
        where, kind = to.get("where"), own
    elif isinstance(to, dict):
        pop = to.get("population")
        if (not isinstance(pop, str) or set(to) - {"population", "where", "random"}
                or not isinstance(to.get("random", False), bool)):
            return [problem("E_SEND_TARGET", f"{who}: to {to!r} is malformed", path,
                            hint=_TARGET_HINT)]
        if pop not in pops:
            return [problem("E_POPULATION_UNKNOWN", f"{who}: unknown population '{pop}'",
                            f"{path}/population",
                            hint=f"declared: {', '.join(sorted(map(str, pops))) or '(none)'}")]
        where, kind = to.get("where"), pops[pop]
    if isinstance(to, dict):
        if where not in (None, "") and kind is not None:
            # `self` in a where is the RECIPIENT: its type's names apply;
            # `sender` (PHASE6 A9) is the sending agent, so only an agent has one.
            names = set(variables) | set(kind.get("parameters") or {}) | \
                ({"sender"} if agent else set())
            try:
                compiled = compile_expression(where, names)
                unknown = _self_reads(where) - _attr_names(kind) - _builtin_reads(kind)
                unknown_sender = _self_reads(where, "sender") - _attr_names(own) - \
                    _builtin_reads(own)
            except ExpressionError as exc:
                return [problem("E_AGENT_EXPRESSION", f"{who}: where {where}: {exc}",
                                f"{path}/where", hint="a condition on the recipient's self.<attr>")]
            # Evaluated once per candidate recipient, with neither an entity
            # nor a random stream (DEVLOG #132, #136): refusing a draw here
            # keeps every agent's own stream untouched by who sends to it.
            if "entity" in compiled.names or compiled.calls & _SAMPLER_CALLS:
                return [problem("E_AGENT_EXPRESSION", f"{who}: where {where}: a where reads "
                                f"only the recipient, with no entity and no random draw",
                                f"{path}/where", hint='pick one at random with "random": true, '
                                'or draw in the sender before the send')]
            out += [problem("E_AGENT_ATTRIBUTE", f"{who}: where: self.{a} is not an attribute "
                            f"of '{kind.get('name')}'", f"{path}/where") for a in sorted(unknown)]
            out += [problem("E_AGENT_ATTRIBUTE", f"{who}: where: sender.{a} is not an attribute "
                            f"of '{(own or {}).get('name')}'", f"{path}/where")
                    for a in sorted(unknown_sender)]
            if check is not None:
                check(who, where, names, f"{path}/where")
        return out
    if to == "self":
        if not agent:
            out.append(problem("E_SEND_TARGET", f"{who}: \"self\" is a target only inside an "
                               f"agent type", path, hint=_TARGET_HINT))
        return out
    if not agent and not isinstance(to, bool):
        # An index is "agent i of my own population": only an agent has one
        # (DEVLOG #138).
        try:
            float(to)
        except (TypeError, ValueError):
            pass
        else:
            return [problem("E_SEND_TARGET", f"{who}: to {to!r}: an index names an agent of "
                            f"the sender's own population, and a block is not an agent", path,
                            hint=_TARGET_HINT)]
    if (isinstance(to, str) and to.strip()) or (isinstance(to, int) and not isinstance(to, bool)):
        expr(str(to), path)
        return out
    return [problem("E_SEND_TARGET", f"{who}: to {to!r} is malformed", path, hint=_TARGET_HINT)]


def validate_structured(layout: dict, variables: set, check=None) -> list:
    """Problems of the `agent_types` and `populations` sections. *check*
    (who, text, names, path) runs the model-wide literal-name checks
    (TABLE/LEVEL/COUNT...) on an expression that already compiled."""
    problems: list = []
    types: dict = {}
    entries = {e.get("name"): e for e in layout.get("agent_types") or []
               if isinstance(e, dict) and isinstance(e.get("name"), str)}
    tables = {t.get("name"): t for t in layout.get("tables") or [] if isinstance(t, dict)}
    blocks = [b for b in layout.get("blocks") or [] if isinstance(b, dict)]
    # What the AB-2 actions refer to, known before any type is walked.
    model = {
        "linear": frozenset(s.get("name") for s in layout.get("timeseries_inputs") or []
                            if isinstance(s, dict) and s.get("interpolation") == "linear"),
        "blocks": {b.get("name"): b.get("type") for b in blocks},
        "levels": {lv.get("name") for lv in layout.get("levels") or [] if isinstance(lv, dict)},
        # C2: a conveyor's value is its cohorts' sum -- a transfer action
        # writing it directly would be silently overwritten at the next step.
        "conveyors": {lv.get("name") for lv in layout.get("levels") or []
                     if isinstance(lv, dict) and lv.get("conveyor")},
        "pops": {p.get("name"): entries.get(p.get("type")) for p in layout.get("populations") or []
                 if isinstance(p, dict) and isinstance(p.get("name"), str)},
        "sent": {b.get("params", {}).get("message") for b in blocks if b.get("type") == "SEND"},
        "listeners": [],
        # Declared network node names (AB-3): a network space needs at least
        # one, and a network placement/move_to node is checked against these
        # only at RUN time (a literal name check here would duplicate
        # `validate_network_structured`'s own E_NETWORK_* problems).
        "nodes": {n.get("name") for n in (layout.get("network") or {}).get("nodes") or []
                 if isinstance(n, dict) and isinstance(n.get("name"), str)},
    }
    for index, entry in enumerate(layout.get("agent_types") or []):
        at = f"/agent_types/{index}"
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or name in types:
            problems.append(problem(
                "E_AGENT_TYPE_NAME", f"agent type '{name}': empty or duplicate name", f"{at}/name",
                hint="give every agent type a unique, non-empty name"))
            continue
        types[name] = entry
        _TypeCheck(problems, entry, at, variables, check, model).run()
    for who, path, message in model["listeners"]:
        if message and message not in model["sent"]:
            problems.append(problem("W_AGENT_MESSAGE_UNUSED", f"{who}: no send action "
                                    f"or SEND block sends \"{message}\"", path,
                                    hint="send it somewhere, or remove the transition"))
    seen: set = set()
    for index, entry in enumerate(layout.get("populations") or []):
        at = f"/populations/{index}"
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name or name in seen:
            problems.append(problem(
                "E_POPULATION_NAME", f"population '{name}': empty or duplicate name", f"{at}/name",
                hint="give every population a unique, non-empty name"))
            continue
        seen.add(name)
        kind = types.get(entry.get("type"))
        if kind is None:
            problems.append(problem(
                "E_POPULATION_TYPE", f"population '{name}': unknown agent type "
                f"'{entry.get('type')}'", f"{at}/type",
                hint=f"declare it under \"agent_types\"; declared: "
                     f"{', '.join(sorted(types)) or '(none)'}"))
        _check_counts(problems, name, entry, kind, at, tables)
        _check_placement(problems, name, entry, kind, at)
        _check_population_tick(problems, name, entry, kind, at)
        _check_network(problems, name, entry, at, tables)                # PHASE6 A5
        if kind is not None:
            _check_population_data(problems, name, entry, kind, at, tables)
            _check_color_by(problems, name, entry, kind, at)
    return problems


def _compiles(text) -> bool:
    try:
        compile_expression(text)
        return True
    except ExpressionError:
        return False


def _check_timing(problems: list, tname: str, where: str, tr: dict, kind: str, here: str,
                  linear) -> None:
    """`scan`, a time-continuous rate, and held reads without `scan`."""
    scan = tr.get("scan")
    if scan is not None and (isinstance(scan, bool) or not isinstance(scan, (int, float))
                             or not scan > 0):
        problems.append(problem("E_AGENT_TRANSITION", f"agent type '{tname}': {where}: scan "
                                f"must be a number > 0", f"{here}/scan",
                                hint="the re-check interval, e.g. \"scan\": 0.5"))
    _instants, held, continuous = timing(tr[kind], linear)
    if kind == "rate" and continuous:
        problems.append(problem("E_AGENT_RATE_FORM", f"agent type '{tname}': {where}: the rate "
                                f"varies continuously with time ({', '.join(sorted(continuous))})",
                                f"{here}/rate",
                                hint="write the time dependence with STEP/PULSE, a literal TNOW "
                                     "comparison or a step TS: piecewise constant is exact"))
    elif (held or continuous) and scan is None:
        problems.append(problem("W_AGENT_SCAN", f"agent type '{tname}': {where}: reads "
                                f"{', '.join(sorted(held | continuous))}, re-evaluated only at "
                                f"agent events", f"{here}/{kind}",
                                hint="add \"scan\": <interval> to re-check it on a grid"))


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_real(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


_TICK_HINT = '"tick": {"dt": 1, "activation": "synchronous"}'


def _tick_problems(tick, who: str, path: str) -> list:
    """The shape of one `tick` (A1); dt may come from the type or the
    population, so the merged spec is checked by `_check_population_tick`."""
    if not isinstance(tick, dict) or set(tick) - {"dt", "activation", "phase", "time_unit"}:
        return [problem("E_AGENT_TICK", f"{who}: tick is {{dt, activation?, phase?}}", path,
                        hint=_TICK_HINT)]
    out = []
    if "dt" in tick and not (_is_real(tick["dt"]) and tick["dt"] > 0):
        out.append(problem("E_AGENT_TICK", f"{who}: tick.dt must be a number > 0", f"{path}/dt",
                           hint=_TICK_HINT))
    if tick.get("activation", "synchronous") not in ACTIVATIONS:
        out.append(problem("E_AGENT_TICK", f"{who}: tick.activation must be one of "
                           f"{', '.join(ACTIVATIONS)}", f"{path}/activation", hint=_TICK_HINT))
    if "phase" in tick and not (_is_real(tick["phase"]) and tick["phase"] >= 0):
        out.append(problem("E_AGENT_TICK", f"{who}: tick.phase must be a number >= 0",
                           f"{path}/phase", hint="an offset in [0, dt): ticks at phase + k * dt"))
    return out


def _check_population_tick(problems: list, name: str, entry: dict, kind: dict | None,
                           at: str) -> None:
    """The population's own tick, then the merged spec (type keys overridden
    by the population's): a dt, phase < dt, and a spec wherever the type has
    tick behaviour (on_tick or a "tick" transition) that would never run."""
    own = entry.get("tick")
    if own is not None:
        found = _tick_problems(own, f"population '{name}'", f"{at}/tick")
        problems.extend(found)
        if found:
            return
    kind = kind if isinstance(kind, dict) else {}
    base = kind.get("tick") if isinstance(kind.get("tick"), dict) else {}
    spec = {**base, **(own or {})}
    behaviour = bool(kind.get("on_tick")) or any(
        isinstance(tr, dict) and "tick" in tr for tr in kind.get("transitions") or [])
    if not spec and not behaviour:
        return
    dt, phase = spec.get("dt"), spec.get("phase", 0)
    if dt is None:
        problems.append(problem("E_AGENT_TICK", f"population '{name}': " + (
            "no tick.dt on the population or its type" if spec else
            f"type '{kind.get('name')}' has on_tick or \"tick\" transitions but no tick"),
            f"{at}/tick", hint=_TICK_HINT))
    elif _is_real(dt) and dt > 0 and _is_real(phase) and phase >= dt:
        problems.append(problem("E_AGENT_TICK", f"population '{name}': tick.phase ({phase:g}) "
                                f"must be < dt ({dt:g})", f"{at}/tick",
                                hint="an offset in [0, dt): ticks at phase + k * dt"))


def _check_counts(problems: list, name: str, entry: dict, kind: dict | None, at: str,
                  tables: dict) -> None:
    count = entry.get("count", 0)
    resolved, ok = count, _is_count(count)
    if isinstance(count, dict):
        resolved, error = _resolve_count(count, tables)
        ok = error is None
        if error:
            problems.append(problem("E_POPULATION_COUNT", f"population '{name}': count: {error}",
                                    f"{at}/count", hint='"count": {"table": "People"}'))
    elif not ok:
        problems.append(problem("E_POPULATION_COUNT", f"population '{name}': count must be an "
                                f"integer >= 0, or {{\"table\": \"<name>\"}}", f"{at}/count",
                                hint="e.g. \"count\": 1000"))
    initial = entry.get("initial") or []
    placed = 0
    if not isinstance(initial, list):
        initial = []
        problems.append(problem("E_POPULATION_COUNT", f"population '{name}': \"initial\" must be "
                                f"a list of {{state, count}}", f"{at}/initial",
                                hint='[{"state": "I", "count": 10}]'))
    states = {s.get("name") for s in (kind or {}).get("states") or [] if isinstance(s, dict)}
    for i, item in enumerate(initial):
        if not isinstance(item, dict) or not _is_count(item.get("count", 0)):
            problems.append(problem("E_POPULATION_COUNT", f"population '{name}': initial count "
                                    f"must be an integer >= 0", f"{at}/initial/{i}",
                                    hint='[{"state": "I", "count": 10}]'))
            continue
        placed += item.get("count", 0)
        if kind is not None and item.get("state") not in states:
            problems.append(problem("E_AGENT_STATE", f"population '{name}': unknown state "
                                    f"'{item.get('state')}'", f"{at}/initial/{i}/state",
                                    hint=f"declared: {', '.join(sorted(map(str, states)))}"))
    if ok and placed > resolved:
        problems.append(problem("E_POPULATION_COUNT", f"population '{name}': the initial states "
                                f"place {placed} agents, the population has {resolved}",
                                f"{at}/initial", hint="raise count or lower the initial counts"))
    most = entry.get("max_count")
    if most is not None and not (_is_count(most) and (not ok or most >= resolved)):
        problems.append(problem("E_POPULATION_COUNT", f"population '{name}': max_count must be "
                                f"an integer >= count" + (f" ({resolved})" if ok else ""),
                                f"{at}/max_count",
                                hint="the most agents alive at once, births included"))


def _check_population_data(problems: list, name: str, entry: dict, kind: dict, at: str,
                           tables: dict) -> None:
    """An attribute init shaped `{"column": ...}` (C3) reads row i of the
    POPULATION's own table -- the one its "count" names, undecorated by
    "column" (a row-per-agent table, not the group-sum form)."""
    count = entry.get("count", 0)
    table_name = count.get("table") if isinstance(count, dict) else None
    grouped = isinstance(count, dict) and count.get("column") is not None
    table = tables.get(table_name) if isinstance(table_name, str) else None
    for attribute in kind.get("attributes") or []:
        if not isinstance(attribute, dict) or not isinstance(attribute.get("init"), dict):
            continue
        column = attribute["init"].get("column")
        aname = attribute.get("name")
        if table_name is None or grouped:
            problems.append(problem("E_AGENT_ATTRIBUTE", f"population '{name}': attribute "
                                    f"'{aname}' reads a column, but count is not "
                                    f"{{\"table\": \"<name>\"}} without \"column\" (a "
                                    f"row-per-agent table)", f"{at}/count",
                                    hint='"count": {"table": "People"}'))
        elif table is not None:      # else _check_counts already reported the unknown table
            cats = attribute.get("categories") if attribute.get("kind") == "category" else None
            _, error = _table_column(table, column, cats)
            if error:
                problems.append(problem("E_AGENT_ATTRIBUTE", f"population '{name}': attribute "
                                        f"'{aname}': {error}", f"{at}/count",
                                        hint="the column must be one of the table's declared "
                                             "columns"))


def _check_color_by(problems: list, name: str, entry: dict, kind: dict, at: str) -> None:
    """`population.color_by` (PHASE6 A2): playback colours a token by this
    categorical attribute's value instead of its state; `colors` (the
    category -> colour name map) is presentational only, the same leniency a
    statechart state's own `color` already gets."""
    color_by = entry.get("color_by")
    if color_by is None:
        return
    attrs = {a.get("name"): a for a in kind.get("attributes") or [] if isinstance(a, dict)}
    attr = attrs.get(color_by) if isinstance(color_by, str) else None
    if attr is None or attr.get("kind") != "category":
        declared = sorted(a for a, v in attrs.items() if v.get("kind") == "category")
        problems.append(problem("E_AGENT_ATTRIBUTE", f"population '{name}': color_by "
                                f"'{color_by}' is not a declared categorical attribute of "
                                f"'{kind.get('name')}'", f"{at}/color_by",
                                hint=f"declared: {', '.join(declared) or '(none)'}"))


# PHASE6 A5: each network kind's own keys, beside kind/directed/link_on_spawn.
_NETWORK_KEYS = {"none": set(), "random": {"p", "mean_degree"}, "small_world": {"k", "beta"},
                 "scale_free": {"m"}, "table": {"table"}}
_NETWORK_HINT = ('{"kind": "random", "mean_degree": 4} | {"kind": "small_world", "k": 4, '
                 '"beta": 0.1} | {"kind": "scale_free", "m": 2} | {"kind": "table", "table": '
                 '"Edges"} | {"kind": "none"}')


def _check_network(problems: list, name: str, entry: dict, at: str, tables: dict) -> None:
    """`population.network` (PHASE6 A5, D103): the kind's own parameters, and
    against a literal count the ones that need n (k < n, m < n, mean degree <=
    n - 1); a count from a table is checked when the network is built."""
    net = entry.get("network")
    if net is None:
        return
    here = f"{at}/network"

    def bad(message, hint=_NETWORK_HINT):
        problems.append(problem("E_AGENT_NETWORK", f"population '{name}': network: {message}",
                                here, hint=hint))
    kind = net.get("kind", "none") if isinstance(net, dict) else None
    if kind not in NETWORK_KINDS:
        return bad(f"kind must be one of {', '.join(NETWORK_KINDS)}")
    extra = set(net) - {"kind", "directed", "link_on_spawn"} - _NETWORK_KEYS[kind]
    if extra:
        return bad(f"unknown keys {sorted(extra)} for kind \"{kind}\"")
    for key in ("directed", "link_on_spawn"):
        if not isinstance(net.get(key, False), bool):
            bad(f"{key} must be true or false")
    if net.get("directed") and kind in ("small_world", "scale_free"):
        bad(f"a {kind} network is undirected", '"directed" only with "random" or "table"')
    n = entry.get("count") if _is_count(entry.get("count")) else None
    if kind == "random":
        p, degree = net.get("p"), net.get("mean_degree")
        if (p is None) == (degree is None):
            bad("give exactly one of p or mean_degree", '{"kind": "random", "p": 0.05}')
        elif p is not None and not (_is_real(p) and 0 <= p <= 1):
            bad("p must be a number in [0, 1]")
        elif degree is not None and not (_is_real(degree) and 0 <= degree
                                         and (n is None or degree <= max(n - 1, 0))):
            bad("mean_degree must be a number in [0, count - 1]")
    elif kind == "small_world":
        k, beta = net.get("k"), net.get("beta", 0)
        if not (_is_count(k) and k >= 2 and k % 2 == 0 and (n is None or k < n)):
            bad("k (each agent's ring neighbours) must be an even integer >= 2 and < count",
                '{"kind": "small_world", "k": 4, "beta": 0.1}')
        if not (_is_real(beta) and 0 <= beta <= 1):
            bad("beta (the rewiring probability) must be a number in [0, 1]")
    elif kind == "scale_free":
        m = net.get("m")
        if not (_is_count(m) and m >= 1 and (n is None or m < n)):
            bad("m (links per new agent) must be an integer >= 1 and < count",
                '{"kind": "scale_free", "m": 2}')
    elif kind == "table":
        table = tables.get(net.get("table")) if isinstance(net.get("table"), str) else None
        error = None if table is not None else f"unknown table '{net.get('table')}'"
        for column in ("from", "to") if table is not None else ():
            error = error or _table_column(table, column)[1]
        if error:
            bad(error, 'a model table with columns "from" and "to" (agent indices)')


_PLACEMENT_KINDS = ("random", "table")


def _check_placement(problems: list, name: str, entry: dict, kind: dict | None, at: str) -> None:
    """`population.placement` (AB-3): only meaningful once the type has a
    valid space, so a bad or absent space says nothing twice (E_AGENT_SPACE
    already covers it)."""
    placement = entry.get("placement")
    if placement is None or not isinstance(kind, dict) or kind.get("space") is None:
        return
    space = kind["space"]
    pkind = placement.get("kind", "random") if isinstance(placement, dict) else None
    if not isinstance(placement, dict) or pkind not in _PLACEMENT_KINDS:
        problems.append(problem("E_AGENT_PLACEMENT", f"population '{name}': placement.kind must "
                                f"be \"random\" or \"table\"", f"{at}/placement",
                                hint='{"kind": "random", "bounds": [0, 0, 100, 100]}'))
        return
    network_space = isinstance(space, dict) and space.get("kind") == "network"
    if isinstance(space, dict) and space.get("kind") == "grid":         # PHASE6 A4
        size = space.get("size")
        cells = placement.get("points") if pkind == "table" else None
        if pkind == "random" and "bounds" in placement or pkind == "table" and not (
                isinstance(cells, list) and cells and isinstance(size, list) and len(size) == 2
                and all(isinstance(p, (list, tuple)) and len(p) == 2
                        and all(_is_count(v) and v < n for v, n in zip(p, size)) for p in cells)):
            problems.append(problem("E_AGENT_PLACEMENT", f"population '{name}': on a grid, "
                                    f"placement is {{\"kind\": \"random\"}} (every cell) or a "
                                    f"table of cells [cx, cy] inside the grid",
                                    f"{at}/placement", hint='{"kind": "table", "points": [[0, 0]]}'))
        return
    if pkind == "table":
        key = "nodes" if network_space else "points"
        table = placement.get(key)
        ok = isinstance(table, list) and bool(table) and (
            all(isinstance(v, str) and v for v in table) if network_space else
            all(isinstance(p, (list, tuple)) and len(p) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)
                for p in table))
        if not ok:
            problems.append(problem("E_AGENT_PLACEMENT", f"population '{name}': a table "
                                    f"placement needs a non-empty \"{key}\" list",
                                    f"{at}/placement/{key}",
                                    hint='["a", "b"]' if network_space else '[[0, 0], [10, 5]]'))
    elif not network_space:
        bounds = placement.get("bounds")
        if bounds is not None and not (isinstance(bounds, (list, tuple)) and len(bounds) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bounds)):
            problems.append(problem("E_AGENT_PLACEMENT", f"population '{name}': bounds must be "
                                    f"[x0, y0, x1, y1]", f"{at}/placement/bounds"))


_SPACE_KINDS = ("continuous", "network", "grid")


def _check_space(problems: list, tname: str, at: str, space, model: dict) -> None:
    """`agent_type.space` (AB-3): a valid kind, `bounds` for a continuous one,
    a network with declared nodes for the other; a grid's geometry (A4; its
    patches are `_TypeCheck`'s, their inits need the type's names)."""
    if not isinstance(space, dict) or space.get("kind") not in _SPACE_KINDS:
        problems.append(problem("E_AGENT_SPACE", f"agent type '{tname}': space.kind must be "
                                f"\"continuous\", \"network\" or \"grid\"", f"{at}/space",
                                hint='"space": {"kind": "continuous", "bounds": [0, 0, 100, 100]}'
                                     ' or {"kind": "network"}'))
        return
    if space["kind"] == "grid":                                         # PHASE6 A4
        size, cell = space.get("size"), space.get("cell", 1)
        bad = [key for key, ok in (
            ("size", isinstance(size, list) and len(size) == 2
             and all(_is_count(v) and v > 0 for v in size)),
            ("cell", _is_real(cell) and cell > 0),
            ("torus", isinstance(space.get("torus", False), bool)),
            ("neighbourhood", space.get("neighbourhood", "moore") in _NEIGHBOURHOODS)) if not ok]
        if bad:
            problems.append(problem("E_AGENT_SPACE", f"agent type '{tname}': a grid space needs "
                                    f"\"size\": [w, h] (integers > 0), \"cell\" > 0, \"torus\" "
                                    f"true/false and \"neighbourhood\" \"moore\" or "
                                    f"\"von_neumann\"; bad: {', '.join(bad)}", f"{at}/space",
                                    hint='{"kind": "grid", "size": [50, 50], "torus": true}'))
    elif space["kind"] == "continuous":
        bounds = space.get("bounds")
        if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bounds)
                and bounds[2] > bounds[0] and bounds[3] > bounds[1]):
            problems.append(problem("E_AGENT_SPACE", f"agent type '{tname}': a continuous space "
                                    f"needs \"bounds\": [x0, y0, x1, y1] with x1 > x0 and "
                                    f"y1 > y0", f"{at}/space/bounds", hint="e.g. [0, 0, 100, 100]"))
        if space.get("edge", "none") not in _EDGES:              # PHASE6 A6
            problems.append(problem("E_AGENT_SPACE", f"agent type '{tname}': space.edge must be "
                                    f"\"none\", \"clamp\", \"wrap\" or \"bounce\"",
                                    f"{at}/space/edge", hint='"edge": "wrap"'))
    elif not model["nodes"]:
        problems.append(problem("E_AGENT_SPACE", f"agent type '{tname}': a network space needs "
                                f"the model's \"network\" section to declare nodes", f"{at}/space",
                                hint="add nodes and segments under \"network\", or use "
                                     "{\"kind\": \"continuous\", \"bounds\": [...]}"))


_ACTION_SHAPES = ("{target, expr} (set), {send, to, payload?}, {enter, e_type?}, "
                  "{transfer, amount, mode?}, {signal}, {move_to}, {spawn, count?, state?, "
                  "init?}, {remove}, {step}, {move_random}, {link}, {unlink}, {forward}, "
                  "{turn}, {face}, {random_walk} or {python}")
_VERBS = frozenset({"target", "send", "enter", "transfer", "signal", "move_to", "spawn", "remove",
                    "step", "move_random", "link", "unlink", "forward", "turn", "face",
                    "random_walk", "python"})
# Heading actions (PHASE6 A6) need a continuous space, like a grid action needs a grid one.
_HEADING_VERBS = frozenset({"forward", "turn", "face", "random_walk"})
_PATCH_KEYS = frozenset({"name", "init", "diffuse", "evaporate", "grow"})


def _patch_reads(text: str) -> set:
    """The literal patch names of every `PATCH("<name>", ...)` in *text*."""
    return {n.args[0].value for n in ast.walk(ast.parse(str(text), mode="eval"))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "PATCH"
            and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)}


def _hierarchy_problems(problems: list, tname: str, at: str, specs: list, states: list) -> set:
    """Composite states (PHASE6 K1): a `parent` names a declared state and
    never leads back to itself; `initial` sits only on a composite and names
    one of its children. Returns the composite names."""
    parents = {}
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or spec.get("parent") is None:
            continue
        if spec["parent"] not in states:
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': state "
                                    f"'{spec.get('name')}': unknown parent '{spec['parent']}'",
                                    f"{at}/states/{i}/parent",
                                    hint=f"declared: {', '.join(states) or '(none)'}"))
        else:
            parents.setdefault(spec.get("name"), spec["parent"])
    for i, spec in enumerate(specs):
        name = spec.get("name") if isinstance(spec, dict) else None
        seen, x = set(), parents.get(name)
        while x is not None and x != name and x not in seen:
            seen.add(x)
            x = parents.get(x)
        if x is not None and x == name:
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': state '{name}' "
                                    f"is its own ancestor (a parent cycle)",
                                    f"{at}/states/{i}/parent", hint="break the parent cycle"))
    children: dict = {}
    for name, parent in parents.items():
        children.setdefault(parent, []).append(name)
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or spec.get("initial") is None:
            continue
        name = spec.get("name")
        if spec["initial"] not in children.get(name, ()):
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': state '{name}': "
                                    f"initial '{spec['initial']}' is not one of its children "
                                    f"(only a composite state has an initial)",
                                    f"{at}/states/{i}/initial",
                                    hint=f"children: {', '.join(children.get(name, [])) or '(none)'}"))
    return set(children)


class _TypeCheck:
    """One agent type's checks, one method per part. `run` calls them in a
    fixed order, and the problems' order is part of the contract; each part
    leaves the names the later ones read (attrs, states, grid, ...) on self."""

    # Every verb of _VERBS -> the method checking an action with that verb.
    _ACTION_CHECKS = {"target": "_set_action", "send": "_send_action", "enter": "_enter_action",
                      "transfer": "_transfer_action", "signal": "_signal_action",
                      "spawn": "_spawn_action", "remove": "_remove_action",
                      "link": "_link_action", "unlink": "_link_action",
                      "python": "_py_action", "step": "_grid_action",
                      "move_random": "_grid_action", "move_to": "_move_to_action",
                      **dict.fromkeys(_HEADING_VERBS, "_heading_action")}

    def __init__(self, problems: list, entry: dict, at: str, variables: set, check, model: dict):
        self.problems, self.entry, self.at = problems, entry, at
        self.variables, self.check, self.model = variables, check, model
        self.tname = entry["name"]

    def run(self) -> None:
        entry, at = self.entry, self.at
        self.check_parameters()
        self.check_attributes()
        self.check_states()
        self.check_space()
        if entry.get("tick") is not None:                   # PHASE6 A1
            self.problems.extend(_tick_problems(entry["tick"], f"agent type '{self.tname}'",
                                                f"{at}/tick"))
        self.check_python()
        self.expr(entry.get("speed", "1"), f"{at}/speed", "speed")
        if self.grid is not None:                                       # PHASE6 A4
            self.check_patches()
        self.check_attribute_inits()
        for i, state in enumerate(entry.get("states") or []):
            if isinstance(state, dict):
                for key in ("entry", "exit"):
                    self.actions(state.get(key), f"{at}/states/{i}/{key}",
                                 f"state {state.get('name')}")
        self.check_lets()
        on_tick = entry.get("on_tick")
        if on_tick is not None and not isinstance(on_tick, list):
            self.bad_action("on_tick", f"{at}/on_tick", "on_tick is a list of actions")
        else:
            self.actions(on_tick, f"{at}/on_tick", "on_tick", self.let_names)
        self.check_transitions()

    # -- parameters and attributes ----------------------------------------------
    def check_parameters(self) -> None:
        problems, tname, at = self.problems, self.tname, self.at
        params = self.entry.get("parameters") or {}
        if not isinstance(params, dict):
            params = {}
            problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': parameters must be "
                                    f"an object of name: number", f"{at}/parameters",
                                    hint='"parameters": {"beta": 0.3}'))
        for key, value in params.items():
            if not str(key).isidentifier() or key in RESERVED_NAMES or \
                    not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': parameter "
                                        f"'{key}' must be an identifier with a number (payload is "
                                        f"reserved)", f"{at}/parameters/{key}",
                                        hint='"parameters": {"beta": 0.3}'))
        self.params = params
        self.names = set(self.variables) | set(params)

    def check_attributes(self) -> None:
        problems, tname, at = self.problems, self.tname, self.at
        attrs: set = set()
        for i, attribute in enumerate(self.entry.get("attributes") or []):
            aname = attribute.get("name") if isinstance(attribute, dict) else None
            if (not isinstance(aname, str) or not aname.isidentifier() or aname.startswith("_")
                    or aname in _READ_ONLY or aname == "heading" or aname in attrs):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': attribute "
                                        f"'{aname}' is not a unique identifier (state, id and "
                                        f"heading are read-only built-ins)", f"{at}/attributes/{i}/name",
                                        hint="rename the attribute"))
                continue
            attrs.add(aname)
            akind = attribute.get("kind", "number")
            cats = attribute.get("categories")
            if akind not in ("number", "category"):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': attribute "
                                        f"'{aname}': kind must be \"number\" or \"category\"",
                                        f"{at}/attributes/{i}/kind", hint='"kind": "category"'))
            elif akind == "category" and (
                    not isinstance(cats, list) or len(cats) < 2
                    or not all(isinstance(c, str) and c for c in cats) or len(set(cats)) != len(cats)):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': attribute "
                                        f"'{aname}': categories must be a list of >= 2 unique, "
                                        f"non-empty names", f"{at}/attributes/{i}/categories",
                                        hint='"categories": ["red", "blue"]'))
        self.attrs = attrs

    def check_attribute_inits(self) -> None:
        problems, tname, at = self.problems, self.tname, self.at
        for i, attribute in enumerate(self.entry.get("attributes") or []):
            if not isinstance(attribute, dict) or "init" not in attribute:
                continue
            init, path = attribute["init"], f"{at}/attributes/{i}/init"
            if isinstance(init, dict):      # {"column": ...} (C3); the column itself is
                if set(init) != {"column"} or not isinstance(init.get("column"), str) \
                        or not init["column"]:
                    problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': attribute "
                                            f"'{attribute.get('name')}': init must be an expression "
                                            f"or {{\"column\": \"<col>\"}}", path,
                                            hint='"init": {"column": "age"}'))
                # checked against the POPULATION's table, in _check_population_data.
            else:
                self.expr(init, path, f"attribute {attribute.get('name')}")
                if attribute.get("kind") == "category" and isinstance(attribute.get("categories"), list):
                    try:
                        literal = ast.parse(str(init), mode="eval").body
                    except SyntaxError:
                        literal = None
                    # A non-literal init (a table column read elsewhere, self.<other
                    # attr>, IF(...)) is checked at run time (_coerce_attr) instead:
                    # only a bare literal can be caught here, at compile time.
                    if isinstance(literal, ast.Constant) and (
                            not isinstance(literal.value, str)
                            or literal.value not in attribute["categories"]):
                        problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': "
                                                f"attribute '{attribute.get('name')}': init "
                                                f"{literal.value!r} is not a declared category", path,
                                                hint=f"declared: {', '.join(attribute['categories'])}"))

    # -- states and hierarchy -----------------------------------------------------
    def check_states(self) -> None:
        problems, tname, at, entry = self.problems, self.tname, self.at, self.entry
        states: list = []
        for i, state in enumerate(entry.get("states") or []):
            sname = state.get("name") if isinstance(state, dict) else None
            if not isinstance(sname, str) or not sname or sname in states:
                problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': state '{sname}' has "
                                        f"an empty or duplicate name", f"{at}/states/{i}/name",
                                        hint="give every state a unique name"))
                continue
            states.append(sname)
        if not states:
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': no states",
                                    f"{at}/states", hint='"states": [{"name": "S"}, {"name": "I"}]'))
        elif entry.get("initial_state", states[0]) not in states:
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': initial state "
                                    f"'{entry.get('initial_state')}' is not declared",
                                    f"{at}/initial_state", hint=f"one of: {', '.join(states)}"))
        self.states = states
        self.composites = _hierarchy_problems(problems, tname, at, entry.get("states") or [], states)

    # -- space, grid and patches ----------------------------------------------------
    def check_space(self) -> None:
        entry = self.entry
        space = entry.get("space")
        if space is not None:
            _check_space(self.problems, self.tname, self.at, space, self.model)
        grid = space if isinstance(space, dict) and space.get("kind") == "grid" else None
        patches = (grid.get("patches") or []) if grid is not None else []
        self.grid, self.patches = grid, patches
        self.patch_names = {p.get("name") for p in patches if isinstance(p, dict)} \
            if isinstance(patches, list) else set()
        self.has_heading = _has_heading(entry)                    # PHASE6 A6
        self.read_only = _builtin_reads(entry)                   # PHASE6 A6, A9

    def check_patches(self) -> None:
        problems, tname, at = self.problems, self.tname, self.at
        grid, patches, names = self.grid, self.patches, self.names

        def bad_patch(message, here, hint=None):
            problems.append(problem("E_AGENT_PATCH", f"agent type '{tname}': {message}", here,
                                    hint=hint))
        seen, flows = set(), False
        for i, patch in enumerate(patches if isinstance(patches, list) else [None]):
            here = f"{at}/space/patches/{i}"
            pname = patch.get("name") if isinstance(patch, dict) else None
            if not isinstance(pname, str) or not pname.isidentifier() or pname in seen \
                    or set(patch) - _PATCH_KEYS:
                bad_patch(f"patch '{pname}' is not {{name, init?, diffuse?, evaporate?, grow?}} "
                          f"with a unique identifier name", here, '{"name": "food", "init": 0}')
                continue
            seen.add(pname)
            for key in ("diffuse", "evaporate"):
                rate = patch.get(key, 0)
                if not (_is_real(rate) and 0 <= rate <= 1):
                    bad_patch(f"patch '{pname}': {key} must be a number in [0, 1]",
                              f"{here}/{key}")
                flows = flows or bool(rate)
            grow = patch.get("grow")                                    # PHASE6 A9
            if grow is not None:
                if not (isinstance(grow, dict) and set(grow) <= {"rate", "max"}
                        and _is_real(grow.get("rate")) and grow["rate"] >= 0
                        and (grow.get("max") is None or _is_real(grow["max"]))):
                    bad_patch(f"patch '{pname}': grow is {{rate >= 0, max?}} (max defaults to "
                              f"each cell's initial value)", f"{here}/grow",
                              '"grow": {"rate": 1, "max": 30}')
                flows = True
            init = patch.get("init", 0)
            if isinstance(init, str):
                if self.expr(init, f"{here}/init", f"patch {pname}", ("cx", "cy")):
                    compiled = compile_expression(init, names | {"cx", "cy"})
                    if "self" in compiled.names or compiled.calls & _GRID_CALLS:
                        bad_patch(f"patch '{pname}': init runs per cell, with no agent: no "
                                  f"self, PATCH or neighbour count", f"{here}/init")
            elif not _is_real(init):
                bad_patch(f"patch '{pname}': init is a number or an expression of cx, cy",
                          f"{here}/init", '"init": "TABLE(\\"food0\\", cx, cy)"')
        dt = grid.get("patch_tick")
        if dt is not None and not (_is_real(dt) and dt > 0):
            bad_patch("patch_tick must be a number > 0", f"{at}/space/patch_tick")
        elif flows and dt is None:
            bad_patch("diffuse/evaporate/grow need \"patch_tick\": the step they run at",
                      f"{at}/space", '"patch_tick": 1')

    # -- python -----------------------------------------------------------------
    def check_python(self) -> None:
        self.py_functions = None                                 # PHASE6 A7: parsed, never run here
        self.has_python = self.entry.get("python") is not None
        if self.has_python:
            code, py_hint = self.entry["python"], '"python": "def step(agent, api): ..."'
            errors = ["must be a non-empty string of def statements"]
            if isinstance(code, str) and code.strip():
                self.py_functions, sends, errors = python_check(code)
                self.model["sent"] |= sends
            for error in errors:
                self.problems.append(problem("E_AGENT_PYTHON", f"agent type '{self.tname}': python "
                                             f"{error}", f"{self.at}/python", hint=py_hint))

    # -- expressions ----------------------------------------------------------------
    def expr(self, text, path, where, extra=()):
        """Compile an agent expression; E_AGENT_EXPRESSION or unknown self.x
        / AGENT("pop", i).x. *extra* is `payload` in a message transition."""
        problems, tname, names, model = self.problems, self.tname, self.names, self.model
        try:
            compiled = compile_expression(text, names | set(extra))
        except ExpressionError as exc:
            problems.append(problem(
                "E_AGENT_EXPRESSION", f"agent type '{tname}': {where}: {text}: {exc}", path,
                hint="payload is readable only in a message transition's guard and actions"
                     if "'payload'" in str(exc) else
                     "names allowed: self.<attribute>, the type's parameters, model variables, "
                     "TNOW and the built-in functions"))
            return False
        if "entity" in compiled.names:          # DEVLOG #136: no entity in an agent
            problems.append(problem(
                "E_AGENT_EXPRESSION", f"agent type '{tname}': {where}: {text}: an agent type has "
                f"no entity", path, hint="read self.<attribute>; entity.agent is for a SEND "
                                         "block, where an entity carries the reference"))
            return False
        for attr in sorted(_self_reads(text) - self.attrs - self.read_only):
            problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': {where}: "
                                    f"self.{attr} is not a declared attribute", path,
                                    hint=f"declared: {', '.join(sorted(self.attrs)) or '(none)'}"))
        for name in sorted(_state_literals(text) & self.composites):          # K review
            problems.append(problem("W_AGENT_STATE_COMPOSITE", f"agent type '{tname}': "
                                    f"{where}: self.state == \"{name}\" is never true: self.state "
                                    f"is the leaf, '{name}' is a composite", path,
                                    hint=f"compare with one of its leaves, or use "
                                         f"COUNT(\"<population>\", \"{name}\") for the group"))
        for pop, attr in _agent_reads(text):
            kind = model["pops"].get(pop)
            if kind is not None and attr not in _attr_names(kind) | _builtin_reads(kind):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': {where}: "
                                        f"AGENT(\"{pop}\", ...).{attr}: '{kind.get('name')}' "
                                        f"has no such attribute", path))
        grid_calls = compiled.calls & _GRID_CALLS                       # PHASE6 A4
        if grid_calls and self.grid is None:
            problems.append(problem("E_AGENT_GRID", f"agent type '{tname}': {where}: "
                                    f"{', '.join(sorted(grid_calls))} need a grid space", path,
                                    hint='"space": {"kind": "grid", "size": [50, 50]}'))
        elif grid_calls:
            for pname in sorted(_patch_reads(text) - self.patch_names):
                problems.append(problem("E_AGENT_PATCH", f"agent type '{tname}': {where}: "
                                        f"PATCH(\"{pname}\"): no such patch", path,
                                        hint=f"declared: {', '.join(sorted(map(str, self.patch_names))) or '(none)'}"))
        for code, message, hint in _link_problems(text, self.entry):         # PHASE6 A5
            problems.append(problem(code, f"agent type '{tname}': {where}: {message}", path,
                                    hint=hint))
        if self.check is not None:
            self.check(f"agent type '{tname}'", text, names | set(extra), path)
        return True

    # -- lets and transitions ---------------------------------------------------------
    def check_lets(self) -> None:
        problems, tname, at, names = self.problems, self.tname, self.at, self.names
        lets = self.entry.get("let")                             # PHASE6 A9
        let_names: tuple = ()
        if lets is not None and not isinstance(lets, dict):
            problems.append(problem("E_AGENT_TICK", f"agent type '{tname}': let is an object of "
                                    f"name: expression", f"{at}/let",
                                    hint='"let": {"slow": "UNIF(0, 1) < p"}'))
        for lname, text in (lets or {}).items() if isinstance(lets, dict) else ():
            if not str(lname).isidentifier() or lname in names or lname in RESERVED_NAMES or \
                    lname in ("cx", "cy") or isinstance(text, bool) or \
                    not isinstance(text, (str, int, float)):
                problems.append(problem("E_AGENT_TICK", f"agent type '{tname}': let '{lname}' needs "
                                        f"a new identifier (not a parameter, variable or reserved "
                                        f"name) and an expression", f"{at}/let/{lname}",
                                        hint='"let": {"slow": "UNIF(0, 1) < p"}'))
                continue
            self.expr(str(text), f"{at}/let/{lname}", f"let {lname}", let_names)
            let_names += (lname,)
        self.let_names = let_names

    def check_transitions(self) -> None:
        problems, tname, at, model = self.problems, self.tname, self.at, self.model
        expr, states, let_names = self.expr, self.states, self.let_names
        for i, tr in enumerate(self.entry.get("transitions") or []):
            here = f"{at}/transitions/{i}"
            if not isinstance(tr, dict):
                continue
            where = f"transition {tr.get('from')} -> {tr.get('to')}"
            for key in ("from", "to"):
                if tr.get(key) not in states:
                    problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': {where}: unknown "
                                            f"state '{tr.get(key)}'", f"{here}/{key}",
                                            hint=f"declared: {', '.join(states) or '(none)'}"))
            history = tr.get("history")                     # PHASE6 K1
            if history is not None and (history not in ("shallow", "deep")
                                        or tr.get("to") not in self.composites):
                problems.append(problem("E_AGENT_TRANSITION", f"agent type '{tname}': {where}: "
                                        f"history must be \"shallow\" or \"deep\" and needs a "
                                        f"composite target", f"{here}/history",
                                        hint="drop history, or target a state other states name "
                                             "as their parent"))
            triggers = [key for key in TRIGGERS if key in tr]
            extra = ()
            if len(triggers) != 1:
                problems.append(problem("E_AGENT_TRANSITION", f"agent type '{tname}': {where}: needs "
                                        f"exactly one trigger of timeout, rate, condition, message or "
                                        f"tick, has {triggers or 'none'}", here,
                                        hint="exactly one of timeout/rate/condition/message/tick"))
            elif triggers[0] == "tick":                     # PHASE6 A1
                if not isinstance(tr["tick"], str):
                    problems.append(problem("E_AGENT_TRANSITION", f"agent type '{tname}': {where}: "
                                            f"tick must be a condition (\"\" fires at every tick)",
                                            f"{here}/tick", hint='"tick": "self.energy > 0"'))
                elif tr["tick"].strip():
                    expr(tr["tick"], f"{here}/tick", where, let_names)
                extra = let_names                           # PHASE6 A9: guard and actions too
            elif triggers[0] == "message":
                extra = ("payload",)
                if not isinstance(tr["message"], str):
                    problems.append(problem("E_AGENT_TRANSITION", f"agent type '{tname}': {where}: "
                                            f"message must be a name (\"\" matches any message)",
                                            f"{here}/message", hint='"message": "infect"'))
                else:
                    model["listeners"].append((f"agent type '{tname}': {where}", f"{here}/message",
                                               tr["message"]))
            else:
                kind = triggers[0]
                if expr(tr[kind], f"{here}/{kind}", where) and kind == "rate":
                    try:
                        rate_parts(tr[kind], self.params)
                    except ValueError as exc:
                        problems.append(problem("E_AGENT_RATE_FORM", f"agent type '{tname}': "
                                                f"{where}: {exc}", f"{here}/rate",
                                                hint="write it as (own) * (shared): own reads only "
                                                     "self, parameters and constants"))
                if kind in ("rate", "condition") and _compiles(tr[kind]):
                    _check_timing(problems, tname, where, tr, kind, here, model["linear"])
                    # Re-checked after every batch in the system, so a draw would
                    # depend on unrelated events; self-free, it has no stream (#142).
                    if kind == "condition" and compile_expression(tr[kind]).calls & _SAMPLER_CALLS:
                        problems.append(problem(
                            "E_AGENT_EXPRESSION", f"agent type '{tname}': {where}: a condition "
                            f"cannot draw random numbers", f"{here}/condition",
                            hint="use a rate: a random firing time with an exact hazard"))
            if tr.get("guard"):
                expr(tr["guard"], f"{here}/guard", where, extra)
            self.actions(tr.get("actions"), f"{here}/actions", where, extra)

    # -- actions ------------------------------------------------------------------------
    def bad_action(self, where, here, text, hint=None):
        self.problems.append(problem("E_AGENT_ACTION", f"agent type '{self.tname}': {where}: {text}",
                                     here, hint=hint))

    def actions(self, items, path, where, extra=()):
        for i, action in enumerate(items or []):
            here = f"{path}/{i}"
            verbs = set(action) & _VERBS if isinstance(action, dict) else ()
            if len(verbs) != 1:
                self.bad_action(where, here, f"an action is exactly one of {_ACTION_SHAPES}",
                                '{"target": "self.x", "expr": "1"}')
                continue
            verb = verbs.pop()
            getattr(self, self._ACTION_CHECKS[verb])(verb, action, here, where, extra)

    def _target(self, target, what, here, where, extra):
        """target_problems for a send/remove/link/unlink target."""
        self.problems.extend(target_problems(
            target, f"agent type '{self.tname}': {where}", f"{here}/{what}", self.model["pops"],
            self.variables, True, lambda text, p: self.expr(text, p, where, extra), self.check,
            own=self.entry))

    def _set_action(self, verb, action, here, where, extra):
        problems, tname = self.problems, self.tname
        if "expr" not in action or set(action) - {"target", "expr"}:
            self.bad_action(where, here, "a set action is {target, expr}",
                            '{"target": "self.x", "expr": "1"}')
            return
        target = str(action["target"]).strip()
        if target.startswith("patch."):                         # PHASE6 A4
            if self.grid is None:
                problems.append(problem("E_AGENT_GRID", f"agent type '{tname}': {where}: "
                                        f"'{target}' needs a grid space", f"{here}/target"))
            elif target[6:] not in self.patch_names:
                problems.append(problem("E_AGENT_PATCH", f"agent type '{tname}': {where}: "
                                        f"'{target}': no such patch", f"{here}/target",
                                        hint=f"declared: {', '.join(sorted(map(str, self.patch_names))) or '(none)'}"))
            self.expr(action["expr"], f"{here}/expr", where, extra)
            return
        try:
            kind, aname = parse_target(target)
        except ExpressionError as exc:
            kind, aname = "error", str(exc)
        if kind == "self" and aname not in self.attrs and not (aname == "heading"
                and self.has_heading) or kind in ("entity", "agent", "error") or \
                kind == "variable" and aname not in self.variables:
            self.bad_action(where, f"{here}/target", f"'{target}' is neither "
                            f"self.<declared attribute> nor a declared variable",
                            f"declared attributes: {', '.join(sorted(self.attrs)) or '(none)'}")
        self.expr(action["expr"], f"{here}/expr", where, extra)

    def _send_action(self, verb, action, here, where, extra):
        message = action["send"]
        if not isinstance(message, str) or not message or "to" not in action or \
                set(action) - {"send", "to", "payload"}:
            self.bad_action(where, here, "a send action is {send: <message name>, to, "
                            "payload?}", '{"send": "infect", "to": {"population": "People", '
                                         '"random": true}}')
            return
        self.model["sent"].add(message)
        self._target(action["to"], "to", here, where, extra)
        if action.get("payload") not in (None, ""):
            self.expr(action["payload"], f"{here}/payload", where, extra)

    def _enter_action(self, verb, action, here, where, extra):
        block, model = action["enter"], self.model
        if not isinstance(block, str) or set(action) - {"enter", "e_type"} or \
                not isinstance(action.get("e_type", ""), str):
            self.bad_action(where, here, "an enter action is {enter: <block name>, e_type?}",
                            '{"enter": "Admit", "e_type": "Patient"}')
        elif model["blocks"].get(block, "CREATE") == "CREATE":
            self.bad_action(where, f"{here}/enter", f"'{block}' is not a block an entity can "
                            f"enter", f"declared: {', '.join(sorted(map(str, model['blocks'])))}"
                            " (not a CREATE)")

    def _transfer_action(self, verb, action, here, where, extra):
        problems, tname, model = self.problems, self.tname, self.model
        level = action["transfer"]
        if not isinstance(level, str) or "amount" not in action or \
                set(action) - {"transfer", "amount", "mode"}:
            self.bad_action(where, here, "a transfer action is {transfer: <level>, amount, "
                            "mode?}", '{"transfer": "Stock", "amount": "5"}')
            return
        if level in model["conveyors"]:
            problems.append(problem("E_CONVEYOR_WRITE", f"agent type '{tname}': {where}: "
                                    f"'{level}' is a conveyor; a transfer action cannot "
                                    f"move material into or out of it", f"{here}/transfer"))
        elif level not in model["levels"]:
            problems.append(problem("E_LEVEL_UNKNOWN", f"agent type '{tname}': {where}: "
                                    f"unknown level '{level}'", f"{here}/transfer",
                                    hint="declare it under \"levels\""))
        if action.get("mode", "remove") not in TRANSFER_MODES:
            problems.append(problem("E_TRANSFER_MODE", f"agent type '{tname}': {where}: "
                                    f"mode must be \"remove\" or \"add\"", f"{here}/mode"))
        self.expr(action["amount"], f"{here}/amount", where, extra)

    def _signal_action(self, verb, action, here, where, extra):
        if not isinstance(action["signal"], str) or not action["signal"] or \
                set(action) - {"signal"}:
            self.bad_action(where, here, "a signal action is {signal: <name>}",
                            '{"signal": "Ready"}')

    def _spawn_action(self, verb, action, here, where, extra):
        """`{spawn: "<Pop>", count?, state?, init?}` (PHASE6 C5): count and the
        init expressions are written in the SPAWNER's scope, the state and the
        init keys name the spawned population's own type."""
        problems, tname, model = self.problems, self.tname, self.model
        pop, init = action["spawn"], action.get("init") or {}
        if not isinstance(pop, str) or not pop or set(action) - {"spawn", "count", "state", "init"} \
                or not isinstance(init, dict) or not isinstance(action.get("state", ""), str):
            self.bad_action(where, here, "a spawn action is {spawn: <population>, count?, state?, "
                            "init?: {attribute: expression}}", '{"spawn": "People", "count": 1}')
            return
        if pop not in model["pops"]:
            problems.append(problem("E_POPULATION_UNKNOWN", f"agent type '{tname}': {where}: "
                                    f"unknown population '{pop}'", f"{here}/spawn",
                                    hint=f"declared: {', '.join(sorted(map(str, model['pops'])))}"))
            return
        kind = model["pops"][pop] or {}
        count = action.get("count")
        if isinstance(count, str):
            self.expr(count, f"{here}/count", where, extra)
        elif count is not None and not (_is_count(count) or isinstance(count, float)
                                        and count >= 0 and count.is_integer()):
            self.bad_action(where, f"{here}/count", "count must be an integer >= 0 or an expression",
                            '"count": 2')
        states = [s.get("name") for s in kind.get("states") or [] if isinstance(s, dict)]
        if "state" in action and kind and action["state"] not in states:
            problems.append(problem("E_AGENT_STATE", f"agent type '{tname}': {where}: spawn: "
                                    f"'{kind.get('name')}' has no state '{action['state']}'",
                                    f"{here}/state", hint=f"declared: {', '.join(map(str, states))}"))
        for attr, text in init.items():
            if kind and attr not in _attr_names(kind):
                problems.append(problem("E_AGENT_ATTRIBUTE", f"agent type '{tname}': {where}: "
                                        f"spawn: '{kind.get('name')}' has no attribute '{attr}'",
                                        f"{here}/init/{attr}",
                                        hint=f"declared: "
                                             f"{', '.join(sorted(map(str, _attr_names(kind))))}"))
            if isinstance(text, bool) or not isinstance(text, (str, int, float)):
                self.bad_action(where, f"{here}/init/{attr}", "an init value is an expression")
            else:
                self.expr(str(text), f"{here}/init/{attr}", where, extra)

    def _remove_action(self, verb, action, here, where, extra):     # PHASE6 C5
        target = action["remove"]
        if set(action) - {"remove"} or isinstance(target, bool) or \
                not isinstance(target, (str, int, dict)) or target == "":
            self.bad_action(where, here, "a remove action is {remove: \"self\" | an index | "
                            "an agent expression | {population, where?, random?}}",
                            '{"remove": "self"}')
            return
        self._target(target, "remove", here, where, extra)

    def _link_action(self, verb, action, here, where, extra):       # PHASE6 A5
        target = action[verb]
        if set(action) - {verb} or isinstance(target, bool) or \
                not isinstance(target, (str, int, dict)) or target == "":
            self.bad_action(where, here, f"a {verb} action is {{{verb}: an index | an agent "
                            f"expression | \"links\" | {{population | links, where?, "
                            f"random?}}}}", f'{{"{verb}": {{"population": "People", '
                                            f'"random": true}}}}')
            return
        self._target(target, verb, here, where, extra)

    def _py_action(self, verb, action, here, where, extra):     # PHASE6 A7
        fname, py_functions = action["python"], self.py_functions
        if set(action) - {"python"} or not isinstance(fname, str):
            self.bad_action(where, here, "a python action is {python: <function name>}",
                            '{"python": "step"}')
        elif not (py_functions or {}).get(fname) and (py_functions is not None
                                                      or not self.has_python):
            why = ("the type declares no python code" if py_functions is None else
                   f"{fname} must take exactly (agent, api)" if fname in py_functions
                   else f"no top-level def {fname}(agent, api) in the type's python")
            self.problems.append(problem("E_AGENT_PYTHON", f"agent type '{self.tname}': {where}: "
                                         f"{{python: \"{fname}\"}}: {why}", f"{here}/python",
                                         hint=f"defined: {', '.join(sorted(py_functions or ())) or '(none)'}"))

    def _heading_action(self, verb, action, here, where, extra):    # PHASE6 A6
        value = action[verb]
        if set(action) - {verb}:
            self.bad_action(where, here, f"a {verb} action is {{{verb}: ...}} alone")
        elif not self.has_heading:
            self.bad_action(where, f"{here}/{verb}", f"{verb} needs the agent type to "
                            f"declare a continuous space",
                            '"space": {"kind": "continuous", "bounds": [0, 0, 100, 100]}')
        else:
            self.model["sent"].add("arrived")    # forward/random_walk: a move_to, D95's own
            if verb in ("forward", "turn"):
                self.expr(str(value), f"{here}/{verb}", where, extra)
            elif verb == "face":
                if isinstance(value, (list, tuple)) and len(value) == 2 and all(
                        isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in value):
                    pass
                elif isinstance(value, str) and value.strip():
                    self.expr(value, f"{here}/face", where, extra)
                else:
                    self.bad_action(where, f"{here}/face", f"face {value!r} is neither "
                                    f"[x, y], an expression giving a point, nor an agent "
                                    f"reference")
            elif not (isinstance(value, dict) and "step" in value
                      and set(value) <= {"step", "turn_sd"}):
                self.bad_action(where, f"{here}/random_walk", "random_walk is {step, "
                                "turn_sd?}", '{"random_walk": {"step": 1, "turn_sd": 20}}')
            else:
                self.expr(str(value["step"]), f"{here}/random_walk/step", where, extra)
                self.expr(str(value.get("turn_sd", 0)), f"{here}/random_walk/turn_sd",
                          where, extra)

    def _grid_action(self, verb, action, here, where, extra):       # PHASE6 A4
        value = action[verb]
        if set(action) - {verb}:
            self.bad_action(where, here, f"a {verb} action is {{{verb}: ...}} alone")
        elif self.grid is None:
            self.problems.append(problem("E_AGENT_GRID", f"agent type '{self.tname}': {where}: "
                                         f"{verb} needs a grid space", f"{here}/{verb}",
                                         hint='"space": {"kind": "grid", "size": [50, 50]}'))
        elif verb == "move_random" and value not in ("any", "free"):
            self.bad_action(where, f"{here}/move_random", "move_random is \"any\" or "
                            "\"free\"", '{"move_random": "free"}')
        elif verb == "step" and not (isinstance(value, list) and len(value) == 2):
            self.bad_action(where, f"{here}/step", "step is [dx, dy], integers or "
                            "expressions", '{"step": [1, 0]}')
        elif verb == "step":
            for j, v in enumerate(value):
                if isinstance(v, str) and v.strip():
                    self.expr(v, f"{here}/step/{j}", where, extra)
                elif not (_is_real(v) and float(v).is_integer()):
                    self.bad_action(where, f"{here}/step/{j}", f"step {v!r} is not an "
                                    f"integer or an expression")

    def _move_to_action(self, verb, action, here, where, extra):    # AB-3
        target = action["move_to"]
        # The runtime delivers "arrived" itself on arrival (D95's own
        # machinery, no new trigger kind): a move_to anywhere in the
        # type is as good as a send for W_AGENT_MESSAGE_UNUSED. A grid
        # move is instantaneous and sends none (A4).
        if self.grid is None:
            self.model["sent"].add("arrived")
        if set(action) - {"move_to"}:
            self.bad_action(where, here, "a move_to action is {move_to: [x, y] | \"<node>\" | "
                            "<expression>}", '{"move_to": [10, 20]}')
        elif self.entry.get("space") is None:
            self.bad_action(where, f"{here}/move_to", "move_to needs the agent type to "
                            "declare \"space\"",
                            '"space": {"kind": "continuous", "bounds": [0, 0, 100, 100]}')
        elif isinstance(target, (list, tuple)) and len(target) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in target):
            if self.grid is not None and not all(float(v).is_integer() for v in target):
                self.bad_action(where, f"{here}/move_to", "on a grid, move_to [cx, cy] "
                                "names a cell: integers")         # PHASE6 A4
        elif isinstance(target, str) and target.strip():
            self.expr(target, f"{here}/move_to", where, extra)
        else:
            self.bad_action(where, f"{here}/move_to", f"move_to {target!r} is neither [x, y], "
                            f"a node name, nor an expression giving a point/node/agent")
