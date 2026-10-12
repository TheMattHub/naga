"""XMILE import (PHASE5 SD-5, PHASE5_PLAN decision 3): read a Stella/iThink-style
`.xmile`/`.stmx` file into Naga's own JSON layout -- stdlib `xml.etree` only,
no external parser, no `pysd`. Vensim `.mdl` import is `nagasim/mdlimport.py`
(PHASE6 S5): a separate stdlib reader, not `pysd` (see its docstring for why),
that reuses this module's tokenizer/parser/emitter (`_tokenize`/`_Parser`/
`_emit`/`_translate`) since Vensim's expression syntax -- arithmetic,
comparisons, `IF THEN ELSE`, function calls -- is close enough to XMILE's own
to share one implementation, parameterised by `_emit`/`_translate`'s optional
`rename_funcs`/`passthrough_funcs` for Vensim's different function names.

SECURITY (ARCHITECTURE 7.4-style review). An XMILE file is untrusted content,
exactly like a model file: it is refused, before any XML parsing, if it is
not UTF-8 (a NUL byte in the first 4 KB, a UTF-16/32 BOM, invalid UTF-8), if
its decoded text -- the same text the parser then gets -- contains a `DOCTYPE`
or `ENTITY` declaration (external entity expansion, the classic XXE attack --
`xml.etree` has no defused-XML guard of its own), or if it exceeds a 20 MB cap.
This module never touches the filesystem or the network on
its own: `import_xmile` takes TEXT OR BYTES only, and `load_xmile(path)` reads
exactly the one path it is given. Every caller is already confined the way
every other file-reading tool in this codebase is -- the CLI path is typed by
a human, and `agent_tools.core.import_xmile` resolves its `path` argument through
`Workspace.resolve` (netguard's `confine_path`) before calling this module, the
same rule every other file-reading agent tool follows: no permission argument
here or there (`netguard.py`, CLAUDE.md security rule).

`import_xmile(text_or_bytes) -> (layout, problems)` is pure and never raises
for a CONTENT problem: a construct this module does not translate (a module, a
macro, a queue/oven/discrete/subscripted conveyor stock, a dimension mapping,
an unknown function) is reported as `E_SDIMPORT_UNSUPPORTED` on that one
variable and left out of the layout, rather than guessed at; a construct
translated with a known approximation (a bare `DELAY`, a non-euler/rk4 method,
a non-day/hour/minute/second time unit, a clock that does not start at 0, a
graphical function with no input equation) is `W_SDIMPORT_APPROX`. Only a
pre-parse security refusal or malformed XML raises `XMILEError` -- there is no
partial layout to return for either. The returned layout is ordinary `nagasim`
JSON (PHASE5 SD-1..SD-4: `levels`/`flows`/`auxiliaries`/`delays`/`tables`/
`subscripts`/`simulation.sd`); `ModelDocument(layout)` opens it directly.

EXPRESSION TRANSLATOR. XMILE equations reference every other stock, flow and
auxiliary by BARE NAME in one flat namespace (`r*Population*(1-Population/K)`);
Naga resolves them through `LEVEL("Population")`/`FLOW(...)`/`AUX(...)`. A
small hand-written tokenizer plus a recursive-descent (precedence-climbing)
parser turns the XMILE text into a tiny AST first, so bare names can be
resolved in a second pass once every stock/flow/aux name is known; a renderer
then emits Python-syntax text `compile_expression` already accepts (`**`,
`==`, `!=`, `and`/`or`/`not`). Precedence, low to high: `IF..THEN..ELSE`, `OR`,
`AND`, `NOT`, comparisons (`= <> < <= > >=`), `+ -`, `* /`, unary `- +`, `^`
(right-associative). Function names: `SMTH1/SMTH3` -> `SMOOTH/SMOOTH3`,
`LN/ABS/EXP/SQRT/INT/MIN/MAX` -> their lower-case equivalents,
`INIT` -> `INITIAL`, `SAFEDIV` -> `XIDZ` (PHASE6 S2: same argument order,
`SAFEDIV`'s optional third argument is `XIDZ`'s own default of 0),
`STEP/RAMP/PULSE/DELAY1/DELAY3/DELAYN/TREND/FORECAST/NPV/SIN/COS/TAN` unchanged (same
argument order as ours -- SIN/COS/TAN PHASE6 S11: `core/expressions.py` already has
them uppercase, radians; ARCSIN/ARCCOS/ARCTAN -> ASIN/ACOS/ATAN, SINH/COSH/TANH
unchanged), bare `DELAY(x, tau[, init])` -> `DELAYP` (XMILE's
pure pipeline delay; no init: `INITIAL(x)`), `TIME` -> `TNOW`, `PI` -> its literal value. Anything
else -- an unknown function, a token this tokenizer cannot classify, a name
naming no declared stock/flow/aux -- is `E_SDIMPORT_UNSUPPORTED` and the
owning variable is dropped. `ZIDZ/MODULO/PROD/VMIN/VMAX/ACTIVE INITIAL/
SAMPLE IF TRUE/NOISE_NORMAL` have no confirmed XMILE spelling and are left
unmapped (`E_SDIMPORT_UNSUPPORTED`) rather than guessed.

MAPPING (OASIS XMILE semantics). `<non_negative/>` present on a STOCK means
bounded at 0 (`min: 0`); absent means unbounded (`min: null`, SD-1's new
bound). The same tag on a FLOW means the flow may not go negative (our
default, `biflow: false`); absent means it may (`biflow: true` -- validated
against its ends the ordinary way, `E_FLOW_BIFLOW`). A `<gf>` (graphical
function) becomes a `tables` entry (D50, key_mode "linear") plus
`TABLE("<name>_gf", <the variable's own eqn, or TNOW with W_SDIMPORT_APPROX
when there is none>)`, replacing that variable's own expression -- the OASIS
reading of "y = f(x)" where x is the variable's equation. `<dimensions><dim
name="X"><elem name="a"/>...</dim></dimensions>` at the top level becomes
`subscripts`; a size-only `<dim name="X" size="3"/>` becomes elements
`"1".."3"`; a `<dim>` with a `<mapping>` child (an alignment between two
dimensions) is `E_SDIMPORT_UNSUPPORTED`, matching PHASE5_PLAN's "subscript
mapping refused" (OASIS XMILE 1.0 defines no mapping element; `<mapping>` is
a vendor extension this reader does not guess at). A variable's own
`<dimensions>` becomes `dims`, and a bare reference to an arrayed variable in
an equation over the same dims -- or dims linked to them by a subrange or
mapping (PHASE6 S3, `sd.related_dims`) -- reads the owner's element,
`LEVEL("Pop[Region]")`. An explicit index (`Pop[north]`, `Pop[Region]`,
`Pop[*]`, Vensim's `Pop[North!]` -> `Pop[North*]`) is kept when each token is
such a dim, an element or a star, and a one-argument SUM/MEAN/PROD/MIN/MAX
(Vensim VMIN/VMAX) over it is our `[*]` aggregate; any other arrayed reference
(a scalar owner, an unlinked dimension) is `E_SDIMPORT_UNSUPPORTED`.
Non-apply-to-all variables (`<element subscript="a, x"><eqn>`, PHASE6 S3)
become per-element `elements` overrides; a graphical function on one is
refused.

CONVEYORS (PHASE6 C2, `docs/history/STELLA-CONVEYOR-SEMANTICS-2026-09-27.md`).
A `<stock>` with a `<conveyor>` child becomes a D135 conveyor level: `<len>`
-> `transit_time`, `<capacity>`/`<in_limit>`/`<arrest>` -> their expressions,
`exponential_leak="true"` -> `leak_kind: "exponential"`. Its `<outflow>`
children, in DECLARED order, give the ONE primary outflow (the first with no
`<leak>` marker, rate `""`) and the rest leaks; a leak's fraction is
`<leak>expr</leak>`'s text when present, else the flow's own `<eqn>` (real
Stella's form, which puts `<eqn>0</eqn>` on the primary outflow instead);
`leak_start`/`leak_end` on `<leak>` become its `zone`. RK4 with a conveyor
present is imported as euler with a `W_SDIMPORT_APPROX` (Stella's slat model
has no RK substep of its own). Refused, each with its own message: `<queue>`,
`<oven>`, a subscripted conveyor (its own fields are never rewritten per
element), `discrete="true"`, a nonzero `<sample>`, `<leak_integers/>`, a
non-"beginning" `isee:spreadflow`, and a list (rather than scalar) initial.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from itertools import product
from pathlib import Path

from nagasim.core import sd
from nagasim.core.problems import problem
from nagasim.core.timeunits import UNITS

__all__ = ["XMILEError", "import_xmile", "load_xmile", "main"]

MAX_BYTES = 20_000_000
_ISEE_NS = "http://iseesystems.com/XMILE"   # C2: isee:spreadflow's namespace (declared at parse)
# Refused BEFORE parsing (checked on the decoded text the parser is given, not
# on what ElementTree would report): a DOCTYPE can declare an external or internal ENTITY that
# xml.etree happily expands, the classic XXE attack. No amount of "safe"
# parser configuration below this line is trusted instead of catching it here.
_DOCTYPE_RE = re.compile(r"<!\s*(doctype|entity)\b", re.IGNORECASE)
_RENAME_FUNCS = {"LN": "log", "ABS": "abs", "EXP": "exp", "SQRT": "sqrt", "INT": "int",
                 "MIN": "min", "MAX": "max", "SMTH1": "SMOOTH", "SMTH3": "SMOOTH3",
                 # PHASE6 S2: XMILE's INIT/SAFEDIV are our INITIAL/XIDZ, same argument
                 # order (SAFEDIV's optional third argument is XIDZ's own default).
                 "INIT": "INITIAL", "SAFEDIV": "XIDZ", "LOG10": "log10",
                 "ARCSIN": "ASIN", "ARCCOS": "ACOS", "ARCTAN": "ATAN"}
# TREND/FORECAST/NPV (PHASE6 S2): same name and argument order in both dialects.
# SIN/COS/TAN (PHASE6 S11, SDXorg corpus): already uppercase, 1-arg, radians in
# core/expressions.py's FUNCTIONS -- no rename needed, just not refused.
_PASSTHROUGH_FUNCS = {"STEP", "RAMP", "PULSE", "DELAY1", "DELAY3", "DELAYN",
                      "TREND", "FORECAST", "NPV", "SIN", "COS", "TAN", "SINH", "COSH", "TANH",
                      "PI"}


class XMILEError(ValueError):
    """A file refused before -- or because it cannot be -- parsed: a security
    refusal (DOCTYPE/ENTITY, the size cap) or malformed XML. Unlike an
    `E_SDIMPORT_UNSUPPORTED` problem there is no partial layout to return."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _Skip(Exception):
    """One variable's equation could not be translated; its record is
    dropped. A problem describing why has already been appended."""


# ---------------------------------------------------------------- XML helpers
def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _children(elem, name: str) -> list:
    return [c for c in elem if _local(c.tag) == name] if elem is not None else []


def _child(elem, name: str):
    found = _children(elem, name)
    return found[0] if found else None


def _text(elem, name: str, default=None):
    child = _child(elem, name)
    if child is None or child.text is None:
        return default
    text = child.text.strip()
    return text if text else default


def _num(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _q(name: str) -> str:
    return json.dumps(name)


def _fold(name: str) -> str:
    """One spelling per variable name (DEVLOG #164): XMILE and Vensim treat
    spaces, underscores (and an escaped newline) and case as equivalent."""
    return " ".join(name.replace("\\n", " ").replace("_", " ").split()).lower()


class _Names(dict):
    """{display name: dims}, also found by any folded spelling (`_resolve`)."""

    def __init__(self, items):
        super().__init__(items)
        self.folded = {_fold(name): name for name in self}


def _ident(name: str) -> str:
    """A dimension or element name as one identifier: `One Dim` -> `One_Dim`."""
    return "_".join(name.split())


def _resolve(table: dict, ident: str):
    """The declared name *ident* spells in *table*, or None."""
    if ident in table:
        return ident
    folded = getattr(table, "folded", None)
    return None if folded is None else folded.get(_fold(ident))


def _refuse(problems: list, owner: str, message: str, severity_code="E_SDIMPORT_UNSUPPORTED"):
    problems.append(problem(severity_code, f"{owner}: {message}", f"/model/variables/{owner}"))
    raise _Skip()


# ---------------------------------------------------------------- expression translator
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+\.\d+(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)"
    # PHASE6 S10: `mod1.output` (one dot) is a module instance's port, read
    # in the OUTER model's own equations -- `_emit`'s `dotted` lookup.
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"|(?P<op><>|<=|>=|[-+*/^(),<>=])"
    r"|(?P<idx>\[[^\]]*\])"                  # PHASE6 S3: `Pop[north]`, `Pop[*]`, `Pop[North!]`
    r"|(?P<junk>\S))")


def _tokenize(text: str) -> list:
    return [(kind, m.group(kind)) for m in _TOKEN_RE.finditer(text)
            for kind in ("num", "id", "op", "idx", "junk") if m.group(kind) is not None]


class _Parser:
    """Recursive-descent, precedence climbing from `IF..THEN..ELSE` (lowest)
    down to `^` (highest, right-associative). Builds a small tuple AST; a
    grammar error raises `_Skip` for `_translate` to report."""

    def __init__(self, tokens: list):
        self.tokens, self.pos = tokens, 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _peek_kw(self, *words) -> bool:
        kind, text = self._peek()
        return kind == "id" and text.upper() in words

    def _take(self):
        tok = self._peek()
        if tok[0] is None:
            raise _Skip()
        self.pos += 1
        return tok

    def _expect_kw(self, word: str) -> None:
        if not self._peek_kw(word):
            raise _Skip()
        self._take()

    def _expect_op(self, op: str) -> None:
        kind, text = self._peek()
        if kind != "op" or text != op:
            raise _Skip()
        self._take()

    def parse(self):
        node = self._if()
        if self.pos != len(self.tokens):
            raise _Skip()
        return node

    def _if(self):
        if self._peek_kw("IF"):
            self._take()
            cond = self._or()
            self._expect_kw("THEN")
            a = self._if()
            self._expect_kw("ELSE")
            b = self._if()
            return ("if", cond, a, b)
        return self._or()

    def _or(self):
        node = self._and()
        while self._peek_kw("OR"):
            self._take()
            node = ("bin", "or", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._peek_kw("AND"):
            self._take()
            node = ("bin", "and", node, self._not())
        return node

    def _not(self):
        if self._peek_kw("NOT"):
            self._take()
            return ("not", self._not())
        return self._cmp()

    _CMP_OPS = {"=": "==", "<>": "!=", "<=": "<=", ">=": ">=", "<": "<", ">": ">"}

    def _cmp(self):
        node = self._add()
        kind, text = self._peek()
        if kind == "op" and text in self._CMP_OPS:
            self._take()
            node = ("bin", self._CMP_OPS[text], node, self._add())
        return node

    def _add(self):
        node = self._mul()
        while self._peek()[0] == "op" and self._peek()[1] in ("+", "-"):
            op = self._take()[1]
            node = ("bin", op, node, self._mul())
        return node

    def _mul(self):
        node = self._unary()
        while self._peek()[0] == "op" and self._peek()[1] in ("*", "/"):
            op = self._take()[1]
            node = ("bin", op, node, self._unary())
        return node

    def _unary(self):
        kind, text = self._peek()
        if kind == "op" and text in ("-", "+"):
            self._take()
            inner = self._unary()
            return ("neg", inner) if text == "-" else inner
        return self._pow()

    def _pow(self):
        node = self._atom()
        if self._peek()[0] == "op" and self._peek()[1] == "^":
            self._take()
            return ("bin", "**", node, self._unary())     # right-assoc
        return node

    def _atom(self):
        kind, text = self._take()
        if kind == "num":
            return ("num", float(text))
        if kind == "op" and text == "(":
            node = self._if()
            self._expect_op(")")
            return node
        if kind == "id":
            if self._peek() == ("op", "("):
                self._take()
                args = []
                if self._peek() != ("op", ")"):
                    args.append(self._if())
                    while self._peek() == ("op", ","):
                        self._take()
                        args.append(self._if())
                self._expect_op(")")
                return ("call", text.upper(), args)
            if self._peek()[0] == "idx":
                index = self._take()[1][1:-1]
                return ("name", text, [_ident(token) for token in index.split(",")])
            return ("name", text)
        raise _Skip()


# PHASE6 S3: a one-argument array builtin -- XMILE's SUM/MEAN/PROD/MIN/MAX(x[*]),
# Vensim's SUM/PROD/VMIN/VMAX(x[D!]) -- is our [*]-unrolled aggregate.
_ARRAY_FUNCS = {"SUM": "SUM", "MEAN": "MEAN", "PROD": "PROD", "VMIN": "VMIN", "VMAX": "VMAX",
                "MIN": "VMIN", "MAX": "VMAX"}


def _index(ident: str, dims: list, index, owner_dims: list, subscripts: list):
    """The index text of an arrayed reference, or None when it cannot resolve
    inside *owner*: a bare name reads the owner's element through its own dims
    (DEVLOG #127) or a subrange/mapping link to them (PHASE6 S3); an explicit
    `[...]` may hold such a dim, an element, `*` or `D*` (Vensim's `D!`)."""
    tokens = list(dims) if index is None else [t[:-1] + "*" if t.endswith("!") else t
                                               for t in index]
    section = {"subscripts": subscripts}
    declared = sd.subscripts_of(section)
    reach = sd.related_dims(section, owner_dims)
    elements = {e for values in declared.values() for e in values}
    ok = len(tokens) == len(dims) and all(
        t in reach or (t not in declared and t in elements)
        or t == "*" or (t.endswith("*") and t[:-1] in declared) for t in tokens)
    return ",".join(tokens) if ok else None


def _emit(node, stocks: dict, flows: dict, auxes: dict, problems: list, owner: str,
          rename_funcs: dict | None = None, passthrough_funcs: frozenset | None = None,
          subscripts: list | None = None, dollar_prefix: str | None = None,
          dotted: dict | None = None) -> str:
    # rename_funcs/passthrough_funcs (PHASE6 S5): mdlimport.py reuses this emitter for
    # Vensim's function set (ZIDZ/XIDZ/DELAY1I/...), which differs from XMILE's; None
    # (every XMILE call site) keeps the original module-level sets, so behaviour here
    # is unchanged for XMILE. *subscripts* (the layout's section, PHASE6 S3) lets an
    # arrayed reference resolve through a subrange or mapping. *dollar_prefix* (PHASE6
    # S10) is set only while translating a MODULE's own body: every stock/flow/aux name
    # it resolves is emitted as "$<prefix><name>" instead of "<name>", so submodels.py's
    # `$` scoping clones it per module instance. *dotted* is the OUTER model's
    # {"instance.var": (func, "instance/var")} map for reading a module's own output.
    rename_funcs = _RENAME_FUNCS if rename_funcs is None else rename_funcs
    passthrough_funcs = _PASSTHROUGH_FUNCS if passthrough_funcs is None else passthrough_funcs

    def rec(child):
        return _emit(child, stocks, flows, auxes, problems, owner, rename_funcs,
                     passthrough_funcs, subscripts, dollar_prefix, dotted)
    kind = node[0]
    if kind == "num":
        return repr(node[1])
    if kind == "name":
        ident, index = node[1], (node[2] if len(node) > 2 else None)
        up = ident.upper()
        if up == "TIME" and index is None:
            return "TNOW"
        if up == "PI" and index is None:
            return repr(math.pi)
        if dotted and "." in ident:
            if index is not None:
                _refuse(problems, owner, f"module output '{ident}' cannot be indexed")
            info = dotted.get(ident)
            if info is None:
                _refuse(problems, owner, f"'{ident}' names no known module output")
            func, flat = info
            return f"{func}({_q(flat)})"
        for table, func in ((stocks, "LEVEL"), (flows, "FLOW"), (auxes, "AUX")):
            declared = _resolve(table, ident)
            if declared is None:
                continue
            ident = declared
            dims = table[ident]
            label = ident if dollar_prefix is None else f"${dollar_prefix}{ident}"
            if not dims and index is None:
                return f"{func}({_q(label)})"
            owner_dims = next((t[owner] for t in (stocks, flows, auxes) if owner in t), [])
            text = _index(ident, dims, index, owner_dims, subscripts or [])
            if text is None:
                _refuse(problems, owner, f"arrayed '{ident}' ({', '.join(dims) or 'scalar'}) is "
                                         f"read with an index its equation cannot resolve (not "
                                         f"its dims, a linked subrange/mapping, an element, "
                                         f"or [*] in SUM/MEAN)")
            return f"{func}({_q(f'{label}[{text}]')})"
        _refuse(problems, owner, f"'{ident}' names no declared stock, flow or auxiliary")
    if kind == "call":
        fname, call_args = node[1], node[2]
        if (fname in _ARRAY_FUNCS and len(call_args) == 1 and call_args[0][0] == "name"
                and len(call_args[0]) > 2):
            # XMILE's `MIN(x[Dim])` over a dim the owner does not run over: all of it, `x[*]`.
            owner_dims = next((t[owner] for t in (stocks, flows, auxes) if owner in t), [])
            declared = sd.subscripts_of({"subscripts": subscripts or []})
            _, ident, index = call_args[0]
            call_args = [("name", ident, ["*" if t in declared and t not in owner_dims else t
                                          for t in index])]
        args = [rec(a) for a in call_args]
        if fname == "DELAY" and len(args) in (2, 3):
            # XMILE's DELAY is a pure pipeline delay, its initial the input's own.
            init = args[2] if len(args) == 3 else f"INITIAL({args[0]})"
            return f"DELAYP({args[0]}, {args[1]}, {init})"
        if len(args) == 1 and fname in _ARRAY_FUNCS:
            fname = _ARRAY_FUNCS[fname]
        elif isinstance(rename_funcs.get(fname), dict):     # {arity: template}
            template = rename_funcs[fname].get(len(args))
            if template is None:
                _refuse(problems, owner, f"function '{fname}' with {len(args)} argument(s) "
                                         f"is not translated")
            return _check_order(template.format(*args), problems, owner)
        elif fname in rename_funcs:
            fname = rename_funcs[fname]
        elif fname not in passthrough_funcs:
            _refuse(problems, owner, f"function '{fname}' is not translated")
        return _check_order(f"{fname}({', '.join(args)})", problems, owner)
    if kind == "if":
        _, cond, a, b = node
        return f"IF({rec(cond)}, {rec(a)}, {rec(b)})"
    if kind == "bin":
        _, op, left, right = node
        return f"({rec(left)} {op} {rec(right)})"
    if kind == "not":
        return f"(not {rec(node[1])})"
    if kind == "neg":
        return f"(-{rec(node[1])})"
    raise _Skip()          # pragma: no cover -- every AST node kind is handled above


def _check_order(text: str, problems: list, owner: str) -> str:
    """*text*, unless it is a DELAYN whose order is not a number: the stage
    count is fixed when the model is built (E_SD_DELAY_ORDER), so refuse the
    one variable here rather than the whole model at run time."""
    if text.startswith("DELAYN("):
        call = ast.parse(text, mode="eval").body
        order = call.args[2] if len(call.args) > 2 else None
        if not (isinstance(order, ast.Constant) and isinstance(order.value, (int, float))):
            _refuse(problems, owner, "DELAY N's order must be a number (the stage count is "
                                     "fixed when the model is built)")
    return text


def _translate(text, stocks: dict, flows: dict, auxes: dict, problems: list, owner: str,
               rename_funcs: dict | None = None, passthrough_funcs: frozenset | None = None,
               subscripts: list | None = None, dollar_prefix: str | None = None,
               dotted: dict | None = None) -> str:
    text = (str(text) if text is not None else "0").strip() or "0"
    tokens = _tokenize(text)
    if any(kind == "junk" for kind, _ in tokens):
        _refuse(problems, owner, f"cannot tokenize \"{text}\"")
    try:
        node = _Parser(tokens).parse()
    except _Skip:
        _refuse(problems, owner, f"cannot parse \"{text}\"")
    except RecursionError:          # DEVLOG #162: the parser recurses ~10 frames per level
        _refuse(problems, owner, "expression nests too deep")
    return _emit(node, stocks, flows, auxes, problems, owner, rename_funcs, passthrough_funcs,
                 subscripts, dollar_prefix, dotted)


# ---------------------------------------------------------------- sim_specs / dimensions
def time_unit(raw: str, problems: list) -> str:
    """The engine time unit named *raw* ("Month", "days", ...). Never a
    rescale (DEVLOG #157): the equations are per the file's own unit, so the
    model runs in it at scale 1; an unknown name keeps scale 1 as minutes."""
    unit = (raw or "").strip().lower()
    if unit and not unit.endswith("s"):
        unit += "s"
    if unit in UNITS:
        return unit
    if unit:
        problems.append(problem("W_SDIMPORT_APPROX",
            f"unknown time unit '{raw}'; imported as minutes (values unscaled)",
            "/simulation/time_unit"))
    return "minutes"


def _sim_specs(root, problems: list) -> dict:
    specs = _child(root, "sim_specs")
    start = _num(_text(specs, "start"), 0.0) if specs is not None else 0.0
    stop = _num(_text(specs, "stop"), start + 1.0) if specs is not None else start + 1.0
    dt = _num(_text(specs, "dt"), 1.0) if specs is not None else 1.0
    dt_elem = _child(specs, "dt")
    if dt_elem is not None and (dt_elem.get("reciprocal") or "").strip().lower() == "true" and dt:
        dt = 1.0 / dt           # Stella's `<dt reciprocal="true">4</dt>` is dt = 1/4 (#126)
    method_raw = ((specs.get("method") if specs is not None else None) or "euler").strip().lower()
    if method_raw in ("rk4", "runge-kutta 4", "rungekutta4"):
        method = "rk4"
    elif method_raw == "euler":
        method = "euler"
    else:
        method = "euler"
        problems.append(problem("W_SDIMPORT_APPROX",
            f"sim_specs method '{method_raw}' is neither euler nor rk4; imported as euler",
            "/simulation/sd/method"))

    units = ((specs.get("time_units") if specs is not None else None)
             or _text(specs, "time_units") or "").strip().lower()
    unit_name = time_unit(units, problems)

    if start:
        problems.append(problem("W_SDIMPORT_APPROX",
            f"sim_specs start={start!r} shifted to 0 (Naga's clock always starts at 0)",
            "/simulation/time_limit"))
    return {"time_limit": stop - start, "time_unit": unit_name,
            "sd": {"dt": dt, "method": method, "time_unit": "base"}}


def _dimensions(root, problems: list):
    """(`subscripts` records, {declared dimension names}) from the top-level
    `<dimensions>`; a `<dim>` with a `<mapping>` child (an alignment between
    two dimensions) is refused rather than approximated."""
    subs, names = [], set()
    for dim in _children(_child(root, "dimensions"), "dim"):
        name = dim.get("name")
        if not name:
            continue
        if _child(dim, "mapping") is not None:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"dimension '{name}': subscript mapping is not translated", "/dimensions"))
            continue
        name = _ident(name)
        elements = [_ident(e.get("name")) for e in _children(dim, "elem") if e.get("name")]
        if not elements and dim.get("size"):
            elements = [str(i + 1) for i in range(int(dim.get("size")))]
        if not elements:
            continue
        subs.append({"name": name, "elements": elements})
        names.add(name)
    return subs, names


def _var_dims(elem, dim_names: set, problems: list, owner: str) -> list:
    out = []
    for d in _children(_child(elem, "dimensions"), "dim"):
        name = _ident(d.get("name") or "")
        if name not in dim_names:
            _refuse(problems, owner, f"dimension '{name}' is not declared, or is a subscript "
                                     f"mapping (not translated)")
        out.append(name)
    return out


def _element_eqns(elem, owner: str, problems: list, translate) -> dict:
    """PHASE6 S3: a non-apply-to-all variable's `<element subscript="a, x">
    <eqn>` children as our per-element equations, `{"elements": {"a,x":
    expr}}` ({} when there are none). A graphical function per element, or
    beside element equations, is refused."""
    found = _children(elem, "element")
    if found and (_child(elem, "gf") is not None
                  or any(_child(child, "gf") is not None for child in found)):
        _refuse(problems, owner, "a graphical function on a non-apply-to-all variable is not "
                                 "translated")
    return {"elements": {",".join(_ident(t) for t in (child.get("subscript") or "").split(",")):
                         translate(_text(child, "eqn", "0"), owner) for child in found}
            } if found else {}


def _array_literal(text: str, dims: list, subscripts: list):
    """(first value, per-element record) of an arrayed variable's comma-separated
    number list (`<eqn>1, 2, 3</eqn>`, one per element, row-major), or None."""
    values = text.replace(";", ",").split(",")
    if not dims or len(values) < 2:
        return None
    try:
        values = [float(v) for v in values]
    except ValueError:
        return None
    declared = sd.subscripts_of({"subscripts": subscripts})
    keys = [",".join(combo) for combo in product(*(declared.get(d, []) for d in dims))]
    if len(keys) != len(values):
        return None
    return repr(values[0]), {"elements": {k: repr(v) for k, v in zip(keys, values)}}


def _gf_table(elem, owner: str, problems: list):
    """(table name, table record) for *owner*'s `<gf>`, or (None, None)."""
    gf = _child(elem, "gf")
    if gf is None:
        return None, None
    ypts_text = _text(gf, "ypts")
    if not ypts_text:
        _refuse(problems, owner, "<gf> has no <ypts>")
    ys = [float(v) for v in ypts_text.replace(";", ",").split(",") if v.strip()]
    xpts_text = _text(gf, "xpts")
    if xpts_text:
        xs = [float(v) for v in xpts_text.replace(";", ",").split(",") if v.strip()]
    else:
        scale = _child(gf, "xscale")
        lo = _num(scale.get("min"), 0.0) if scale is not None else 0.0
        hi = _num(scale.get("max"), 1.0) if scale is not None else 1.0
        n = len(ys)
        xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)] if n > 1 else [lo]
    if len(xs) != len(ys) or len(xs) < 2:
        _refuse(problems, owner, "<gf>'s x and y points do not line up")
    return f"{owner}_gf", {"name": f"{owner}_gf", "keys": xs, "values": ys, "key_mode": "linear"}


# ---------------------------------------------------------------- conveyors (PHASE6 C2)
def _conveyor_stock(belt, elem, name: str, dim_names: set, translate, problems: list) -> dict:
    """A `<stock>` with a `<conveyor>` child as a Naga conveyor level
    (D135): `<len>` -> transit_time, `<capacity>`/`<in_limit>`/`<arrest>` ->
    their expressions, `exponential_leak="true"` -> leak_kind "exponential".
    Refuses (each with its own message) what the engine's own slat model
    cannot do: a subscripted conveyor (C1 never rewrites a conveyor's own
    fields per element), `discrete="true"`, a nonzero `<sample>`, a non-
    "beginning" `isee:spreadflow`, and an explicit per-slat/per-time-unit
    initial (a comma list, instead of the scalar steady-state initial the
    engine spreads on its own)."""
    if _var_dims(elem, dim_names, problems, name):
        _refuse(problems, name, "a subscripted conveyor is not translated")
    if (belt.get("discrete") or "").strip().lower() == "true":
        _refuse(problems, name, "a discrete conveyor is not translated")
    sample_text = _text(belt, "sample")
    if sample_text is not None and _num(sample_text, 0.0) != 0.0:
        _refuse(problems, name, "a nonzero <sample> is not translated")
    spreadflow = belt.get(f"{{{_ISEE_NS}}}spreadflow")
    if spreadflow and spreadflow.strip().lower() != "beginning":
        _refuse(problems, name, f"isee:spreadflow \"{spreadflow}\" is not translated (only the "
                               f"default, all inflow at entry depth, is)")
    eqn_text = _text(elem, "eqn", "0")
    values = eqn_text.replace(";", ",").split(",")
    if len(values) > 1:
        try:
            [float(v) for v in values]
        except ValueError:
            pass
        else:
            _refuse(problems, name, "a list initial value is not translated (only a scalar "
                                    "steady-state initial)")
    len_text = _text(belt, "len")
    if len_text is None:
        _refuse(problems, name, "a conveyor needs a <len> (transit time)")
    conveyor = {"transit_time": translate(len_text, name)}
    for field in ("capacity", "in_limit", "arrest"):
        text = _text(belt, field)
        if text is not None:
            conveyor[field] = translate(text, name)
    if (belt.get("exponential_leak") or "").strip().lower() == "true":
        conveyor["leak_kind"] = "exponential"
    expr = translate(eqn_text, name)
    record = sd.Stock(name, initial=expr, unit=_text(elem, "units", ""), conveyor=conveyor)
    return record.to_dict()


def _conveyor_flow(elem, name: str, source: str, targets: list, is_primary: bool,
                   translate, problems: list) -> dict:
    """One outflow of a conveyor stock (D135): the primary outflow's rate is
    always "" (XMILE SS4.3: a conveyor outflow "MUST NOT have an equation" --
    honoured even though real Stella exports set `<eqn>0</eqn>` on it); a leak
    outflow's fraction comes from `<leak>expr</leak>` when it has text, else
    from the flow's own `<eqn>` (Stella's real form, SS3.3)."""
    if _child(elem, "leak_integers") is not None:
        _refuse(problems, name, "leak_integers is not translated")
    leak = None
    if not is_primary:
        leak_elem = _child(elem, "leak")
        leak_text = (leak_elem.text or "").strip() if leak_elem is not None else ""
        leak = {"fraction": translate(leak_text or _text(elem, "eqn", "0"), name)}

        def bound(key: str, default: float) -> float:
            # DEVLOG #237: Stella puts the zone on the <flow> element itself;
            # the <leak> attribute and child-element forms are read after it.
            for text in (elem.get(key), None if leak_elem is None else leak_elem.get(key),
                         _text(elem, key)):
                if text is not None:
                    return _num(text, default)
            return default
        start, end = bound("leak_start", 0.0), bound("leak_end", 1.0)
        if (start, end) != (0.0, 1.0):
            leak["zone"] = [start, end]
    record = sd.Flow(name, rate="", source=source, target=targets[0] if targets else None,
                     biflow=False, leak=leak)
    return record.to_dict()


# ---------------------------------------------------------------- modules (PHASE6 S10)
def _module_named(vars_elem) -> tuple:
    """{name: []} for a module template's own stock/flow/aux -- no subscripts:
    they are deferred inside a submodel definition (ARCHITECTURE 5.24), so
    `_var_dims` below refuses any `<dimensions>` it finds (no dim is ever
    declared for it to belong to)."""
    def named(tag):
        return _Names({e.get("name", ""): [] for e in _children(vars_elem, tag)})
    return named("stock"), named("flow"), named("aux")


def _build_module_definition(model_elem, instance_name: str, problems: list):
    """One non-root `<model>` used as a module template (PHASE6 S10): its own
    stock/flow/aux become `$`-scoped records of a submodel definition
    (`core/submodels.py`'s blockless, continuous-asset shape -- `definition`
    has no blocks, only `scoped`), named "<instance>::<name>" at the layout's
    top level so two instances of the same template never collide.

    Returns (definition, template_records, inputs, outputs): *template_records*
    is {"levels"/"flows"/"auxiliaries": [...]} for the caller to add to the
    layout's own sections; *inputs* is {var: parameter name} for every
    access="input" auxiliary (fed only through `<connect>`, PHASE6 S10);
    *outputs* is {var: (func, qualified name)} -- "LEVEL"/"FLOW"/"AUX" and the
    template record's own top-level name -- for every access="output"
    variable, read from outside as that instance's own port.

    None on refusal: nested modules, a stock or flow marked access="input"
    (only an auxiliary input is translated), a graphical function anywhere
    inside the module, or anything `_var_dims`/the expression translator
    itself refuses (a subscripted variable, an untranslatable equation)."""
    vars_elem = _child(model_elem, "variables")
    if _children(vars_elem, "module"):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"module '{instance_name}': nested modules are not translated",
            "/model/variables"))
        return None

    stocks_e, flows_e, auxes_e = (_children(vars_elem, "stock"), _children(vars_elem, "flow"),
                                  _children(vars_elem, "aux"))
    stock_names, flow_names, aux_names = _module_named(vars_elem)
    prefix = f"{instance_name}::"

    inflow_of, outflow_of = {}, {}
    for elem in stocks_e:
        name = elem.get("name", "")
        for tag, table in (("inflow", inflow_of), ("outflow", outflow_of)):
            for child in _children(elem, tag):
                table.setdefault(_fold(child.text or ""), []).append(prefix + name)

    def translate(text, owner):
        return _translate(text, stock_names, flow_names, aux_names, problems, owner,
                          dollar_prefix=prefix)

    levels, flows, auxiliaries = [], [], []
    scoped: dict = {"levels": {}, "flows": {}, "auxiliaries": {}}
    parameters: list = []
    inputs, outputs = {}, {}

    for elem in stocks_e:
        name = elem.get("name", "")
        access = (elem.get("access") or "").strip().lower()
        try:
            if access == "input":
                _refuse(problems, name, "a stock marked access=\"input\" is not translated")
            if _child(elem, "conveyor") is not None or _child(elem, "queue") is not None:
                _refuse(problems, name, "conveyor/queue stocks are not translated")
            if _child(elem, "gf") is not None:
                _refuse(problems, name, "a graphical function on a stock is not translated")
            _var_dims(elem, set(), problems, name)     # empty dim_names: refuses <dimensions>
            expr = translate(_text(elem, "eqn", "0"), name)
            non_negative = _child(elem, "non_negative") is not None
            record = sd.Stock(prefix + name, initial=expr, min=0.0 if non_negative else None,
                              unit=_text(elem, "units", ""))
            levels.append(record.to_dict())
            scoped["levels"][prefix + name] = {}
            if access == "output":
                outputs[name] = ("LEVEL", prefix + name)
        except _Skip:
            continue

    for elem in flows_e:
        name = elem.get("name", "")
        access = (elem.get("access") or "").strip().lower()
        try:
            if access == "input":
                _refuse(problems, name, "a flow marked access=\"input\" is not translated")
            if _child(elem, "gf") is not None:
                _refuse(problems, name, "a graphical function inside a module is not translated")
            _var_dims(elem, set(), problems, name)
            sources, targets = outflow_of.get(_fold(name), []), inflow_of.get(_fold(name), [])
            if len(sources) > 1 or len(targets) > 1:
                _refuse(problems, name, "feeds or drains more than one stock "
                                        "(Naga's flows join exactly two ends)")
            expr = translate(_text(elem, "eqn", "0"), name)
            biflow = _child(elem, "non_negative") is None
            record = sd.Flow(prefix + name, rate=expr, source=sources[0] if sources else None,
                             target=targets[0] if targets else None, biflow=biflow)
            flows.append(record.to_dict())
            scoped["flows"][prefix + name] = {}
            if access == "output":
                outputs[name] = ("FLOW", prefix + name)
        except _Skip:
            continue

    for elem in auxes_e:
        name = elem.get("name", "")
        access = (elem.get("access") or "").strip().lower()
        try:
            if _child(elem, "gf") is not None:
                _refuse(problems, name, "a graphical function inside a module is not translated")
            _var_dims(elem, set(), problems, name)
            if access == "input":
                parameters.append({"name": name, "type": "string", "default": "0"})
                record = sd.Aux(prefix + name, expr="0", unit=_text(elem, "units", ""))
                auxiliaries.append(record.to_dict())
                scoped["auxiliaries"][prefix + name] = {"expr": {"$arg": name}}
                inputs[name] = name
                continue
            expr = translate(_text(elem, "eqn", "0"), name)
            record = sd.Aux(prefix + name, expr=expr, unit=_text(elem, "units", ""))
            auxiliaries.append(record.to_dict())
            scoped["auxiliaries"][prefix + name] = {}
            if access == "output":
                outputs[name] = ("AUX", prefix + name)
        except _Skip:
            continue

    definition = {"blocks": [], "links": [], "scoped": scoped, "parameters": parameters}
    template_records = {"levels": levels, "flows": flows, "auxiliaries": auxiliaries}
    return definition, template_records, inputs, outputs


def _resolve_connect_from(text: str, stock_names: dict, flow_names: dict, aux_names: dict,
                          module_instances: dict):
    """The RHS of a `<connect from="...">` as an expression reading it live:
    a bare name in the OUTER (root) model's own stock/flow/aux, or another
    module instance's "instance.var" OUTPUT (PHASE6 S10, read through the
    same "instance/var" scoped name the flattener gives its clone). None when
    *text* names neither."""
    text = (text or "").strip()
    if not text:
        return None
    inst, dot, var = text.partition(".")
    if not dot:
        for table, func in ((stock_names, "LEVEL"), (flow_names, "FLOW"), (aux_names, "AUX")):
            if text in table:
                return f"{func}({_q(text)})"
        return None
    meta = module_instances.get(inst)
    if meta is None or var not in meta.get("outputs", {}):
        return None
    func, qualified = meta["outputs"][var]
    return f'{func}({_q(f"{inst}/{qualified}")})'


def _process_connects(module_elem, instance_name: str, inputs: dict, stock_names: dict,
                      flow_names: dict, aux_names: dict, module_instances: dict,
                      problems: list):
    """This module instance's `<connect>` children as the SUBMODEL block's
    `arguments` (PHASE6 S10), or None on refusal: every declared input needs
    exactly one connect naming it (`to="<instance>.<input>"`), from a
    variable `_resolve_connect_from` can read."""
    arguments: dict = {}
    seen: set = set()
    for connect in _children(module_elem, "connect"):
        to_attr = (connect.get("to") or "").strip()
        to_inst, dot, to_var = to_attr.partition(".")
        if not dot or to_inst != instance_name or to_var not in inputs:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"module '{instance_name}': <connect to=\"{to_attr}\"> does not name one of its "
                f"own access=\"input\" variables", "/model/variables"))
            return None
        from_attr = connect.get("from", "")
        ref = _resolve_connect_from(from_attr, stock_names, flow_names, aux_names,
                                    module_instances)
        if ref is None:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"module '{instance_name}': <connect from=\"{from_attr}\"> names no known "
                f"variable or module output", "/model/variables"))
            return None
        arguments[inputs[to_var]] = ref
        seen.add(to_var)
    missing = sorted(set(inputs) - seen)
    if missing:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"module '{instance_name}': input(s) {', '.join(missing)} have no <connect>",
            "/model/variables"))
        return None
    return arguments


# ---------------------------------------------------------------- top level
def import_xmile(text_or_bytes) -> tuple:
    """Translate an XMILE document (str or bytes) into `(layout, problems)`.

    Pure: no file or network access, and never raises for a construct this
    module cannot translate -- only for a pre-parse security refusal or
    malformed XML (`XMILEError`)."""
    raw = text_or_bytes if isinstance(text_or_bytes, bytes) else text_or_bytes.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise XMILEError("E_SDIMPORT_XML", f"the document is {len(raw)} bytes, over the "
                                           f"{MAX_BYTES} byte cap")
    # UTF-8 (or ASCII) only (DEVLOG #128): a UTF-16/32 document would carry its
    # DOCTYPE past a check on the raw bytes. The check runs on the decoded
    # text, and that same text is what the parser gets (a str is parsed as
    # UTF-8 whatever its XML declaration says).
    try:
        if b"\x00" in raw[:4096]:
            raise UnicodeError
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise XMILEError("E_SDIMPORT_XML", "the document is not UTF-8 (UTF-16/32 and other "
                                           "encodings are refused before parsing); re-save it "
                                           "as UTF-8") from None
    if _DOCTYPE_RE.search(text):
        raise XMILEError("E_SDIMPORT_XML", "a DOCTYPE or ENTITY declaration is refused before "
                                           "parsing (no external entity expansion, ever)")
    if "isee:" in text and "xmlns:isee" not in text:
        # xmutil writes `isee:` elements without declaring the prefix (DEVLOG
        # #164); declared here, after the DOCTYPE check, on the same text.
        text = re.sub(r"<xmile\b", '<xmile xmlns:isee="http://iseesystems.com/XMILE"', text,
                      count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise XMILEError("E_SDIMPORT_XML", f"not well-formed XML: {exc}") from None

    problems: list = []
    models = _children(root, "model")
    vars_elem = _child(models[0], "variables") if models else None
    module_models = {m.get("name"): m for m in models[1:] if m.get("name")}

    subscripts, dim_names = _dimensions(root, problems)
    simulation = _sim_specs(root, problems)

    stocks_e, flows_e, auxes_e = (_children(vars_elem, "stock"), _children(vars_elem, "flow"),
                                  _children(vars_elem, "aux"))
    # {name: its declared dims}: what `_emit` resolves a bare name against.
    def named(elems):
        return _Names({e.get("name", ""): [_ident(d.get("name") or "")
                                           for d in _children(_child(e, "dimensions"), "dim")]
                       for e in elems})
    stock_names, flow_names, aux_names = named(stocks_e), named(flows_e), named(auxes_e)

    inflow_of, outflow_of = {}, {}
    # C2: a conveyor's own `<outflow>` children, in DECLARED ORDER -- the
    # first one with no `<leak>` marker is the primary outflow (the research
    # note's "first listed outflow is primary" convention).
    stock_outflows: dict = {}
    for elem in stocks_e:
        name = elem.get("name", "")
        for tag, table in (("inflow", inflow_of), ("outflow", outflow_of)):
            for child in _children(elem, tag):
                table.setdefault(_fold(child.text or ""), []).append(name)
        outs = [c.text or "" for c in _children(elem, "outflow")]
        if outs:
            stock_outflows[name] = outs

    # PHASE6 S10: `<module>` instances. A module naming a model this file does
    # not have keeps the old, blunt per-instance refusal; a model no
    # `<module>` ever claims stays reported as "beyond the main one", exactly
    # as before modules were translated at all.
    levels, flows, auxiliaries, tables = [], [], [], []
    blocks: list = []
    submodels_section: dict = {}
    module_instances: dict = {}
    consumed_models: set = set()
    for elem in _children(vars_elem, "module"):
        inst_name = elem.get("name", "")
        template = module_models.get(inst_name)
        if template is None:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"module instance '{inst_name}' is not translated", "/model/variables"))
            continue
        built = _build_module_definition(template, inst_name, problems)
        if built is None:
            continue
        definition, template_records, inputs, outputs = built
        arguments = _process_connects(elem, inst_name, inputs, stock_names, flow_names,
                                      aux_names, module_instances, problems)
        if arguments is None:
            continue
        submodels_section[inst_name] = definition
        blocks.append({"name": inst_name, "description": inst_name, "type": "SUBMODEL",
                       "position": [0, 0],
                       "params": {"definition": inst_name, "arguments": arguments}})
        levels.extend(template_records["levels"])
        flows.extend(template_records["flows"])
        auxiliaries.extend(template_records["auxiliaries"])
        module_instances[inst_name] = {"outputs": outputs}
        consumed_models.add(inst_name)

    extra = [m.get("name") for m in models[1:] if m.get("name") not in consumed_models]
    if extra:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{len(extra)} module/macro model(s) beyond the main one are not translated",
            "/model"))

    # {"instance.var": (func, "instance/instance::var")}: an OUTER equation's
    # own read of a module's output, by the literal scoped name the flattener
    # gives it (a clone's name is "<scope>/<template record name>", and the
    # template record's own name is already "<instance>::<var>").
    dotted = {f"{inst}.{var}": (func, f"{inst}/{qualified}")
             for inst, meta in module_instances.items()
             for var, (func, qualified) in meta["outputs"].items()}

    # A standalone <gf> is a lookup called like a function, `name(x)`: TABLE("name_gf", x).
    rename = None
    for gf in _children(vars_elem, "gf"):
        holder = ET.Element("aux")
        holder.append(gf)
        try:
            table_name, table = _gf_table(holder, gf.get("name", ""), problems)
        except _Skip:
            continue
        tables.append(table)
        rename = rename or dict(_RENAME_FUNCS)
        rename[_ident(gf.get("name", "")).upper()] = {1: f"TABLE({_q(table_name)}, {{0}})"}

    def translate(text, owner):
        return _translate(text, stock_names, flow_names, aux_names, problems, owner,
                          rename_funcs=rename, subscripts=subscripts, dotted=dotted)

    conveyor_stocks: set = set()
    for elem in stocks_e:
        name = elem.get("name", "")
        try:
            if _child(elem, "queue") is not None:
                _refuse(problems, name, "queue stocks are not translated")
            if _child(elem, "oven") is not None:
                _refuse(problems, name, "oven stocks are not translated")
            if _child(elem, "gf") is not None:
                _refuse(problems, name, "a graphical function on a stock is not translated")
            belt = _child(elem, "conveyor")
            if belt is not None:
                levels.append(_conveyor_stock(belt, elem, name, dim_names, translate, problems))
                conveyor_stocks.add(name)
                continue
            dims = _var_dims(elem, dim_names, problems, name)
            per_element = _element_eqns(elem, name, problems, translate)
            expr = translate(_text(elem, "eqn", "0"), name)
            non_negative = _child(elem, "non_negative") is not None
            record = sd.Stock(name, initial=expr, min=0.0 if non_negative else None,
                              unit=_text(elem, "units", ""), dims=dims)
            levels.append({**record.to_dict(), **per_element})
        except _Skip:
            continue

    if conveyor_stocks and simulation["sd"]["method"] == "rk4":
        # A slat model is defined per-DT; RK4's substeps have no meaning for a
        # belt that advances once per full DT (research note SS3, simlin SS9.4).
        simulation["sd"]["method"] = "euler"
        problems.append(problem("W_SDIMPORT_APPROX",
            "sim_specs method \"RK4\" is not used for conveyors (Stella itself computes them "
            "discretely); imported as euler", "/simulation/sd/method"))

    # C2: the first `<outflow>` of a conveyor stock, in DECLARED order, with no
    # `<leak>` marker of its own is its ONE primary outflow; every other
    # outflow (marked or not) is a leak.
    primary_of: dict = {}
    for stock in conveyor_stocks:
        for out_name in stock_outflows.get(stock, []):
            out_elem = next((e for e in flows_e if _fold(e.get("name", "")) == _fold(out_name)),
                            None)
            if out_elem is not None and _child(out_elem, "leak") is None:
                primary_of[stock] = out_elem.get("name", "")
                break

    for elem in flows_e:
        name = elem.get("name", "")
        try:
            sources, targets = outflow_of.get(_fold(name), []), inflow_of.get(_fold(name), [])
            if len(sources) > 1 or len(targets) > 1:
                _refuse(problems, name, "feeds or drains more than one stock "
                                        "(Naga's flows join exactly two ends)")
            if sources and sources[0] in conveyor_stocks:
                flows.append(_conveyor_flow(elem, name, sources[0], targets,
                                            primary_of.get(sources[0]) == name, translate,
                                            problems))
                continue
            dims = _var_dims(elem, dim_names, problems, name)
            per_element = _element_eqns(elem, name, problems, translate)
            has_eqn = _child(elem, "eqn") is not None
            expr = translate(_text(elem, "eqn", "0"), name)
            gf_name, gf_table = _gf_table(elem, name, problems)
            if gf_name:
                tables.append(gf_table)
                expr = f'TABLE({_q(gf_name)}, {expr if has_eqn else "TNOW"})'
                if not has_eqn:
                    problems.append(problem("W_SDIMPORT_APPROX",
                        f"{name}: <gf> has no input equation; TIME used", f"/model/variables/{name}"))
            biflow = _child(elem, "non_negative") is None
            record = sd.Flow(name, rate=expr, source=sources[0] if sources else None,
                             target=targets[0] if targets else None, biflow=biflow, dims=dims)
            flows.append({**record.to_dict(), **per_element})
        except _Skip:
            continue

    for elem in auxes_e:
        name = elem.get("name", "")
        try:
            dims = _var_dims(elem, dim_names, problems, name)
            per_element = _element_eqns(elem, name, problems, translate)
            has_eqn = _child(elem, "eqn") is not None
            literal = _array_literal(_text(elem, "eqn", ""), dims, subscripts)
            if literal:
                expr, per_element = literal
            else:
                expr = translate(_text(elem, "eqn", "0"), name)
            gf_name, gf_table = _gf_table(elem, name, problems)
            if gf_name:
                tables.append(gf_table)
                expr = f'TABLE({_q(gf_name)}, {expr if has_eqn else "TNOW"})'
                if not has_eqn:
                    problems.append(problem("W_SDIMPORT_APPROX",
                        f"{name}: <gf> has no input equation; TIME used", f"/model/variables/{name}"))
            record = sd.Aux(name, expr=expr, unit=_text(elem, "units", ""), dims=dims)
            auxiliaries.append({**record.to_dict(), **per_element})
        except _Skip:
            continue

    layout = {"levels": levels, "flows": flows, "auxiliaries": auxiliaries, "tables": tables,
              "subscripts": subscripts, "simulation": simulation}
    if blocks:                     # PHASE6 S10: only when a module was actually translated
        layout["blocks"] = blocks
        layout["submodels"] = submodels_section
    return layout, problems


def load_xmile(path) -> tuple:
    """`import_xmile` on a file: the 20 MB cap is checked from `stat()`, before
    the file is even read. *path* is the caller's to confine -- this function
    trusts it, like `Path.read_bytes` trusts any path it is given."""
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise XMILEError("E_SDIMPORT_XML", f"'{p}' is {size} bytes, over the {MAX_BYTES} byte cap")
    return import_xmile(p.read_bytes())


def main(argv=None) -> int:
    """`naga sdimport model.xmile|model.mdl [-o out.json]`: translate an XMILE or
    Vensim file into a Naga model JSON and print every import problem (a
    construct left untranslated is always reported, never silently dropped).
    Dispatches on the file extension (PHASE6 S5): `.mdl` goes to `mdlimport`,
    everything else is read as XMILE, exactly as before."""
    parser = argparse.ArgumentParser(
        prog="naga sdimport",
        description="Translate an XMILE (.xmile/.stmx) or Vensim (.mdl) file into a Naga model JSON.")
    parser.add_argument("source", help="path to the .xmile/.stmx/.mdl file")
    parser.add_argument("-o", "--out", help="write the model JSON here instead of stdout")
    args = parser.parse_args(argv)

    from nagasim import mdlimport   # local: mdlimport imports this module (avoid a load cycle)
    is_mdl = Path(args.source).suffix.lower() == ".mdl"
    try:
        layout, problems = mdlimport.load_mdl(args.source) if is_mdl else load_xmile(args.source)
    except (XMILEError, mdlimport.MDLError, OSError) as exc:
        code = getattr(exc, "code", "E_SDIMPORT_MDL" if is_mdl else "E_SDIMPORT_XML")
        print(f"naga sdimport: {code}: {exc}", file=sys.stderr)
        return 1

    for p in problems:
        print(f"{p.severity.upper()} {p.code} {p.path or '/'}: {p.message}")
    text = json.dumps(layout, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 1 if any(p.severity == "error" for p in problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
