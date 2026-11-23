"""D1332, #1959: every problem code a screen shows has a plain line filled from the problem's
own fields, by code. Checked here by reading the producers (no model, no screen):

- each template's code is a registered code, and its names are fields or `block`;
- each call `problem("CODE", ...)` / `Problem(code="CODE", ...)` of a code with a template
  gives, as literals, the `fields` (and the `case`) its template names, and `block=` when
  the template names {block};
- a code raised as an error object (ReadError, GuardError, MesReadError) reaches a problem
  through a call whose code is a variable; those calls give `source` only, so such a
  code's template names nothing else; a new call of that kind is reviewed here first;
- the codes without a line only go down (the count is banked by hand when it falls).
"""
from __future__ import annotations

import ast
import functools
import re
from collections import Counter
from pathlib import Path

import pytest

from nagasim.core import plainlines
from nagasim.core.problems import PROBLEM_CODES, Problem, problem

ROOT = Path(__file__).resolve().parents[1]
# The source scans below read the product too: in the exported engine repository there is
# no `product/` tree, so they belong to the monorepo only.
MONOREPO = pytest.mark.skipif(not (ROOT / "product").is_dir(),
                              reason="reads the product's source (monorepo only)")
FIELDS_OF_ROUTED = {"source", "table"}     # what the variable-code calls that give fields give
# The codes a call with a variable code carries out of a table or a chooser (`refs` in the
# expression check, `_mismatch_code`, the `simulation` unit loop), by hand, with that call's
# file: one of its variable-code calls gives the fields each of the code's lines names.
ROUTED_CODES = {
    **dict.fromkeys(("E_SERIES_UNKNOWN", "E_LEVEL_UNKNOWN", "E_FLOW_UNKNOWN", "E_CONVERSION_UNKNOWN",
                     "E_VARIABLE_UNKNOWN", "E_DISTANCE_UNKNOWN", "E_AUX_UNKNOWN", "E_DELAY_UNKNOWN",
                     "E_POWER_UNKNOWN", "E_POPULATION_UNKNOWN"), "nagasim/core/schema/common.py"),
    **dict.fromkeys(("W_UNIT_MISMATCH", "E_UNIT_MISMATCH", "W_UNIT_TIME", "E_UNIT_TIME"),
                    "nagasim/core/units.py"),
    **dict.fromkeys(("E_TIME_UNIT", "E_UNIT"), "nagasim/core/schema/layout.py"),
}
# The calls whose code is a variable, per file, each reviewed for the fields it gives: a new
# one is added here once it gives what the lines of the codes it carries name.
ROUTED_CALLS = {
    "nagasim/core/agents/validation.py": 1, "nagasim/core/conveyor_checks.py": 1,
    "nagasim/core/schema/common.py": 2,
    "nagasim/core/schema/layout.py": 1, "nagasim/core/submodels.py": 1, "nagasim/core/tending.py": 1,
    "nagasim/core/units.py": 4, "nagasim/databind.py": 1, "nagasim/live.py": 1, "nagasim/plant.py": 2,
    "nagasim/recommend.py": 1, "nagasim/state.py": 1, "product/libraries/geometry.py": 1,
    "product/mes/mapping.py": 2,
}
# Codes with no plain line yet: their screens show the message. Only goes down.
UNCOVERED_AT_MOST = 41
# D1334: the codes a screen can show (`tools/screen_codes.py` finds them from what feeds the
# Problems panel, the plant monitor, the live page and the MES twin) that have no plain line
# yet; they show the fallback (the message's first clause). Only goes down: a code that newly
# reaches a screen needs a line, and one given a line leaves this list.
# E_PLUGIN_NOT_LOADED stays on its message, which names the plugin and why it did not load.
SCREEN_CODES_WITHOUT_A_LINE = {"E_PLUGIN_NOT_LOADED"}


def _files():
    for package in ("nagasim", "product"):
        yield from (ROOT / package).rglob("*.py")


def _literal(node):
    return node.value if isinstance(node, ast.Constant) else None


@functools.lru_cache(maxsize=None)
def _calls() -> tuple:
    """(file, line, code or None, {keyword: node}) of every Problem-building call."""
    return tuple(_scan())


def _is_problem_call(node) -> bool:
    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
    return isinstance(node, ast.Call) and name in ("problem", "Problem")


def _parts(node):
    """(code node, {keyword: node}) of a Problem-building call."""
    keywords = {k.arg: k.value for k in node.keywords}
    code = node.args[0] if node.args else keywords.get("code")
    keywords["*args"] = ast.Tuple(elts=node.args[1:], ctx=ast.Load())
    return code, keywords


def _codes(node):
    """The codes a code node can be: a literal, both literals of `"E_A" if x else "E_B"`, or
    None (a variable)."""
    if isinstance(node, ast.IfExp) and _literal(node.body) and _literal(node.orelse):
        return [node.body.value, node.orelse.value]
    return [_literal(node)]


def _helpers(tree):
    """{function node: its Problem-building calls} of the functions whose call takes its code
    or its fields from the function's own parameters (D1334: `bad(code, text, fields=None)`):
    each call of such a helper is a producer, with what that call gives."""
    def own(func):          # the function's nodes, not those of the functions it defines
        todo = list(ast.iter_child_nodes(func))
        while todo:
            node = todo.pop()
            yield node
            if not isinstance(node, ast.FunctionDef):
                todo.extend(ast.iter_child_nodes(node))

    found = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        params = {a.arg for a in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs)}
        for node in own(func):
            if isinstance(node, ast.Call) and _is_problem_call(node):
                code, keywords = _parts(node)
                said = keywords.get("fields")
                unpacked = isinstance(said, ast.Dict) and any(k is None for k in said.keys)
                if (isinstance(code, ast.Name) and code.id in params) or unpacked or (
                        isinstance(said, ast.Name) and said.id in params):
                    found.setdefault(func, []).append(node)
    return found


def _callee(call):
    """The name a call calls: `bad(...)`, or `self._warn(...)` of the module's own class."""
    func = call.func
    if isinstance(func, ast.Attribute) and getattr(func.value, "id", None) == "self":
        return func.attr
    return getattr(func, "id", None)


def _bind(func, call) -> dict:
    """{parameter: node} of *call* to *func*: its arguments, else the parameters' defaults."""
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    bound = dict(zip([a.arg for a in positional][len(positional) - len(args.defaults):], args.defaults))
    positional = positional[isinstance(call.func, ast.Attribute):]       # self.helper(...)
    bound.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None})
    bound.update({a.arg: v for a, v in zip(positional, call.args)})
    bound.update({k.arg: k.value for k in call.keywords if k.arg})
    return bound


def _through(func, inner, call):
    """(code node, keywords) a call of helper *func* gives its Problem-building call *inner*."""
    bound = _bind(func, call)
    code, keywords = _parts(inner)
    if isinstance(code, ast.Name) and code.id in bound:
        code = bound[code.id]
    elif isinstance(code, ast.Name):
        code = None
    said = keywords.get("fields")
    if isinstance(said, ast.Name):
        said = bound.get(said.id)
        said = None if _literal(said) is None and isinstance(said, ast.Constant) else said
    elif isinstance(said, ast.Dict):
        keys, values = [], []
        for k, v in zip(said.keys, said.values):
            v = v.values[0] if isinstance(v, ast.BoolOp) else v         # **(fields or {})
            given = bound.get(v.id) if k is None and isinstance(v, ast.Name) else None
            if k is None and isinstance(given, ast.Dict):
                keys += given.keys
                values += given.values
            elif k is not None:
                keys.append(k)
                values.append(v)
        said = ast.Dict(keys=keys, values=values)
    keywords = {**keywords, "fields": said} if said is not None else {
        k: v for k, v in keywords.items() if k != "fields"}
    # What the call gives besides the fields counts as read by the message too.
    keywords["*site"] = ast.Tuple(elts=[*call.args, *(k.value for k in call.keywords
                                                      if k.arg != "fields")], ctx=ast.Load())
    return code, keywords


def _scopes(tree) -> dict:
    """{id(node): the functions it sits in, outermost first} of every node of *tree*."""
    found, todo = {}, [(tree, ())]
    while todo:
        node, inside = todo.pop()
        found[id(node)] = inside
        within = (*inside, node) if isinstance(node, ast.FunctionDef) else inside
        todo.extend((child, within) for child in ast.iter_child_nodes(node))
    return found


def _helper_called(call, by_name, scopes, defined):
    """The helper *call* calls: the one of its name defined in a function the call sits in
    (or at module level), the innermost and then the last defined before the call (a `bad`
    per check); else, when the name is a parameter of a function the call sits in (a
    `report` handed on), the module's only function of that name, when it is a helper."""
    funcs = by_name.get(_callee(call), [])
    inside = scopes[id(call)]
    seen = [f for f in funcs if (scopes[id(f)][-1:] or [None])[0] in (None, *inside)]
    if seen:
        depth = max(len(scopes[id(f)]) for f in seen)
        seen = [f for f in seen if len(scopes[id(f)]) == depth]
        return max([f for f in seen if f.lineno < call.lineno] or seen, key=lambda f: f.lineno)
    params = {a.arg for f in inside for a in (*f.args.posonlyargs, *f.args.args, *f.args.kwonlyargs)}
    return funcs[0] if defined[_callee(call)] == 1 and funcs and _callee(call) in params else None


def _scan():
    for path in _files():
        text = path.read_text(encoding="utf-8")
        if "roblem(" not in text:       # most files build none: not parsed
            continue
        tree = ast.parse(text)
        where = path.relative_to(ROOT).as_posix()
        helpers = _helpers(tree)
        inner = {id(call) for calls in helpers.values() for call in calls}
        by_name = {}
        for func in helpers:
            by_name.setdefault(func.name, []).append(func)
        scopes = _scopes(tree)
        defined = Counter(f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = _helper_called(node, by_name, scopes, defined)
            if _is_problem_call(node) and id(node) not in inner:
                made = [_parts(node)]
            elif func is not None:
                made = [_through(func, call, node) for call in helpers[func]]
            else:
                continue
            for code, keywords in made:
                if code is None:
                    continue
                for literal in _codes(code):
                    yield where, node.lineno, literal, keywords


def _given(keywords) -> tuple[set, str | None] | None:
    """(field names, case) a call gives, or None when its fields are not a literal dict."""
    said = keywords.get("fields")
    if said is None:
        return set(), None
    if not isinstance(said, ast.Dict) or not all(isinstance(k, ast.Constant) for k in said.keys):
        return None
    names = {k.value for k in said.keys}
    # A case read from a variable ("case": why) may be any of the code's cases: "*".
    case = next(((_literal(v) or "*") for k, v in zip(said.keys, said.values) if k.value == "case"), None)
    return names, case


def test_every_template_belongs_to_a_code_and_names_only_fields():
    for code, said in plainlines.PLAIN.items():
        assert code in PROBLEM_CODES, code
        for template in (said.values() if isinstance(said, dict) else [said]):
            assert template and len(template.split()) <= 12, (code, template)
            assert not template.endswith("."), (code, template)        # one short line


def test_every_producer_gives_the_fields_its_codes_line_names():
    wrong = []
    for where, line, code, keywords in _calls():
        if code is None or code not in plainlines.PLAIN:
            continue
        given = _given(keywords)
        if given is None:
            wrong.append(f"{where}:{line} {code}: fields is not a literal dict")
            continue
        names, case = given
        if case == plainlines.SAID:
            continue                    # the message's own first clause is the line
        said = plainlines.PLAIN[code]
        templates = list(said.values()) if case == "*" and isinstance(said, dict) else [
            plainlines.template_for(code, case)]
        if None in templates:
            wrong.append(f"{where}:{line} {code}: no template for case {case!r}")
            continue
        for template in templates:
            missing = plainlines.names_in(template) - names - {"block"}
            if missing:
                wrong.append(f"{where}:{line} {code}: fields lack {sorted(missing)}")
            if "block" in plainlines.names_in(template) and "block" not in keywords:
                wrong.append(f"{where}:{line} {code}: the line names the block, the call gives none")
    assert not wrong, "\n".join(wrong)


# A line that ends on one of these words stops mid-phrase ("using the model's own", #1960).
DANGLING = {"a", "an", "the", "of", "to", "in", "at", "on", "by", "for", "from", "with", "and",
            "or", "own", "its", "their", "is", "are", "than", "as", "into", "model's", "using"}


def _fills():
    """(code, template, filled line) for every producer of a templated code, its literal
    fields as given and every other field a stand-in name; a code reached only through a
    variable is filled with stand-ins alone."""
    seen = set()
    for _where, _line, code, keywords in _calls():
        given = _given(keywords) if code in plainlines.PLAIN else None
        if given is None or given[1] in ("*", plainlines.SAID):
            continue
        said = keywords.get("fields")
        values = {k.value: (v.value if isinstance(v, ast.Constant) else "Cutting")
                  for k, v in zip(said.keys, said.values)} if said is not None else {}
        seen.add((code, given[1]))
        yield code, plainlines.template_for(code, given[1]), _render(code, values)
    for code, said in plainlines.PLAIN.items():
        for case, template in (said.items() if isinstance(said, dict) else [(None, said)]):
            if (code, case) not in seen:
                values = {name: "Cutting" for name in plainlines.names_in(template)}
                yield code, template, _render(code, {**values, "case": case})


def _render(code, values):
    return plainlines.plain(problem(code, "the engine's message", block="Cutting",
                                    fields={**values}))


def test_every_line_renders_whole():
    """#1960: each line, filled as its producers fill it, is filled (no "{name}" left, not
    the message) and ends on a word that closes a phrase; its own words name no code and
    no model setting (snake_case)."""
    wrong = []
    for code, template, line in _fills():
        words = re.sub(r"\{[a-z_]+\}", "", template)
        if line == "the engine's message" or "{" in line:
            wrong.append(f"{code}: not filled: {template!r}")
        elif line.split()[-1].lower().strip(";:,") in DANGLING:
            wrong.append(f"{code}: ends mid-phrase: {line!r}")
        if re.search(r"\b[EW]_[A-Z_]+\b|\b[a-z]+_[a-z_]+\b", words):
            wrong.append(f"{code}: a code or setting name in {template!r}")
    assert not wrong, "\n".join(wrong)


@MONOREPO
def test_codes_reached_only_through_a_variable_name_only_what_those_calls_give():
    literal = {code for _w, _l, code, _k in _calls() if code}
    gives = {}          # file -> [(case, names) each variable-code call there gives]
    for where, _l, code, keywords in _calls():
        given = _given(keywords) if code is None else None
        if given:
            gives.setdefault(where, []).append(
                (given[1], given[0] | ({"block"} if "block" in keywords else set())))
    for code, said in plainlines.PLAIN.items():
        if code in literal and code not in ROUTED_CODES:
            continue
        cases = said.items() if isinstance(said, dict) else [(None, said)]
        for case, template in cases:
            need = plainlines.names_in(template)
            if code not in ROUTED_CODES:
                assert need <= FIELDS_OF_ROUTED, (code, template)
                continue
            calls = gives[ROUTED_CODES[code]]
            if case is not None:
                calls = [(c, names) for c, names in calls if c == case]
                if not calls:
                    continue            # a case only calls with a literal code give
            assert any(need <= names for _c, names in calls), (code, template)


@MONOREPO
def test_the_calls_with_a_variable_code_are_the_reviewed_ones():
    routed = Counter(where for where, _l, code, _k in _calls() if code is None)
    assert dict(routed) == ROUTED_CALLS


def test_codes_without_a_line_only_go_down():
    uncovered = set(PROBLEM_CODES) - set(plainlines.PLAIN)
    assert len(uncovered) <= UNCOVERED_AT_MOST, sorted(uncovered)
    if len(uncovered) < UNCOVERED_AT_MOST:
        print(f"bank it: UNCOVERED_AT_MOST = {len(uncovered)}")


def test_the_line_is_filled_by_code_and_falls_back_to_the_message():
    thin = problem("W_MES_THIN_DATA", "warning: Parts[BRACKET][Drilling]: only 0 observation(s) in "
                   "the last 14d, below min_samples 20; the model's own value 35.0 stands",
                   fields={"case": "few_value", "what": "BRACKET times at Drilling",
                           "days": "14 days", "kept": "35.0"})
    assert plainlines.plain(thin) == "Too few BRACKET times at Drilling in 14 days; using the model's 35.0"
    assert plainlines.plain({**thin.as_dict(), "fields": thin.fields}) == plainlines.plain(thin)
    # D1334: a result written before the fields, an unknown code, a missing field: the
    # message's first clause, without "warning: " or a leading "CODE: ".
    assert plainlines.plain(thin.as_dict()) == ("Parts[BRACKET][Drilling]: only 0 observation(s) in "
                                                "the last 14d, below min_samples 20")
    assert plainlines.plain({"code": "W_X", "message": "warning: hello. More."}) == "hello."
    assert plainlines.plain({"code": "W_X", "message": "W_X: hello; more"}) == "hello"
    assert plainlines.plain(problem("W_LIVE_GAP", "m", fields={})) == "m"
    assert plainlines.plain({"code": "W_X", "message": "the mean is 35.0 here"}) == "the mean is 35.0 here"
    assert plainlines.plain({"code": "E_X", "message": "no zone (e.g. Europe/Rome). Fix"}) == "no zone (e.g. Europe/Rome)."
    # Fields do not change what a problem is.
    assert Problem("W_X", "warning", "m", fields={"a": 1}) == Problem("W_X", "warning", "m")
    assert "fields" not in thin.as_dict()


@MONOREPO
@pytest.mark.slow
def test_every_code_a_screen_can_show_has_a_line():
    """D1334: found by `tools/screen_codes.py` from each screen's producers, not by hand."""
    from tools import screen_codes
    reach = set().union(*screen_codes.screen_codes().values())
    assert set(plainlines.PLAIN) <= reach, sorted(set(plainlines.PLAIN) - reach)   # the finder finds
    bare = reach - set(plainlines.PLAIN)
    assert not bare - SCREEN_CODES_WITHOUT_A_LINE, f"reach a screen with no line: {sorted(bare - SCREEN_CODES_WITHOUT_A_LINE)}"
    assert not SCREEN_CODES_WITHOUT_A_LINE - bare, f"have a line now, drop them: {sorted(SCREEN_CODES_WITHOUT_A_LINE - bare)}"


# What a `fields` value may call besides reading the call's own names: helpers that cannot
# raise on what the message already formatted.
PURE = {"str", "len", "round", "int", "float", "sorted", "list", "repr", "min", "max", "_days",
        "join", "get", "upper", "lower", "strip", "removeprefix", "replace", "_g", "_plural"}


def _read(node) -> set:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def test_a_lines_fields_read_only_what_the_message_already_read():
    """D1334: filling `fields` cannot make a producer raise where it did not: each value reads
    only names the call's other arguments read (or a comprehension's own), and calls only
    helpers that cannot fail on them."""
    wrong = []
    for where, line, code, keywords in _calls():
        said = keywords.get("fields")
        if code is None or not isinstance(said, ast.Dict):
            continue
        call_names = set().union(*(_read(v) for k, v in keywords.items() if k != "fields"))
        for value in said.values:
            own = {t.id for comp in ast.walk(value) if isinstance(comp, ast.comprehension)
                   for t in ast.walk(comp.target) if isinstance(t, ast.Name)}
            called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                      for c in ast.walk(value) if isinstance(c, ast.Call)}
            names = _read(value) - own - called
            if names - call_names or called - PURE:
                wrong.append(f"{where}:{line} {code}: reads {sorted(names - call_names)}, "
                             f"calls {sorted(called - PURE)}")
    assert not wrong, "\n".join(wrong)


def test_lines_reached_through_a_recode_or_a_table_render():
    """D1334: what the static checks cannot follow, rendered from a real validation: an
    expression problem recoded to E_DASHBOARD_EXPR keeps its case, and the `refs` table's
    unknown names and the `simulation` unit loop give their fields."""
    from conftest import layout
    from nagasim.core import schema
    widgets = [{"name": "a", "kind": "kpi", "expr": "NOT_A_NAME"},
               {"name": "b", "kind": "kpi", "expr": 'FLOW("nope") + LEVEL("nope")'}]
    doc = layout([], [], dashboard={"widgets": widgets})
    doc["simulation"]["time_unit"] = "fortnights"
    lines = {p.code: plainlines.plain(p) for p in schema.validate_model_structured(doc)}
    assert lines["E_DASHBOARD_EXPR"] == "dashboard widget a: NOT_A_NAME is not a valid expression"
    assert lines["E_FLOW_UNKNOWN"] == "dashboard widget b: no flow named nope"
    assert lines["E_LEVEL_UNKNOWN"] == "dashboard widget b: no level named nope"
    assert lines["E_TIME_UNIT"] == "The model's time unit fortnights is not known"
