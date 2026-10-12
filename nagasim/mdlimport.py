"""Vensim `.mdl` import (PHASE6 S5). `import_mdl(text_or_bytes) -> (layout, problems)`,
`load_mdl(path)` -- same shape and rules as `sdimport.import_xmile`/`load_xmile`.

APPROACH (decided here, overriding PHASE5_PLAN decision 3's "pysd's parser" default).
Two options: (a) `pysd.translators.vensim` parses `.mdl` into pysd's `AbstractModel`,
mapped onto our layout; (b) a small stdlib reader for the common Vensim equation
subset, reusing `sdimport`'s XMILE expression tokenizer/parser/emitter (Vensim's
expression syntax -- arithmetic, `= <> < <= > >=`, `IF THEN ELSE`, function calls --
is close enough to XMILE's to share one implementation). Chosen: (b). `pysd` is not
installed in this project's interpreter and CLAUDE.md forbids it becoming a hard (or
even soft-but-untested) requirement; its `translators` API is young and has moved
across pysd releases, so a lazy-imported mapping layer would carry real version risk
for something this module could instead own outright; and the documented common
subset (equations, INTEG, an inline lookup, DELAY1/3, subscript ranges; `:MACRO:`,
`GET XLS/DIRECT ...` and an unknown function refused with a code) is small enough
that a hand-written reader is both less code AND fully testable today, unlike a
`pytest.importorskip("pysd")` path that would never run in this environment. If a
real corpus later needs pysd's fuller Vensim coverage (macros, `GET ...`, subscript
equivalences), that is a separate, explicitly-scoped milestone (S11's SDXorg
corpus is the natural place to revisit the trade-off), not a silent widening of this
module.

ENCODING. Vensim's `{UTF-8}` first-line marker is only Vensim 6+ convention; a great
many `.mdl` files in the wild -- and every file from an older Vensim -- have no such
marker and are Windows-1252 (cp1252), the encoding Vensim used before it started
writing UTF-8. UTF-8 is tried first (it never mis-decodes valid cp1252 as something
else without raising) and cp1252 is the fallback (it never raises: every byte 0-255
maps to some character), not a security-checked primary path. A NUL byte in the
first 4 KB refuses outright, matching the XMILE reader's binary-file guard (#128).

SECURITY (ARCHITECTURE 7.4-style review, mirroring `sdimport.py`). `.mdl` has no XML
entity-expansion surface, but it has two comparable ones this module refuses instead
of acting on: a `:MACRO:...:END OF MACRO:` block (a user-defined function Vensim
would inline -- this reader has no macro expander, so every variable a macro
declares is left out, `E_SDIMPORT_UNSUPPORTED`) and `GET XLS/DIRECT ...` (Vensim's
spreadsheet/external-data reference) -- refused on sight, by regex, on raw
undecoded-argument text; this module never opens the file GET names, never even
inspects the argument as a path. `import_mdl` takes TEXT OR BYTES only and never
touches the filesystem or network; `load_mdl(path)` reads exactly the path it is
given. The agent tool `import_mdl` (`agent_tools/core.py`) resolves its path through
`Workspace.resolve` first, the same rule as `import_xmile` (no permission argument,
CLAUDE.md's netguard rule) -- there is no widening here.

MAPPING AND LIMITATIONS (the "common subset"; anything else is `E_SDIMPORT_UNSUPPORTED`
on the one variable, not a file-wide refusal -- only a pre-parse encoding/size/macro
issue is closer to a refusal, and even `:MACRO:` only drops what it declares).
* Vensim treats spaces, underscores and case in a variable name as one name (DEVLOG
  #164): every declared name is rewritten, throughout the equations, to its first
  spelling with underscores for spaces (`Stock A` -> `Stock_A`) before parsing. The four
  sim-control names (`FINAL TIME`, `INITIAL TIME`, `TIME STEP`, `SAVEPER`) are left alone.
* A stock is any equation whose value is `INTEG(rate, initial)`; `rate` must be a
  bare flow name, or a top-level `+`/`-` chain of bare flow names (`Births - Deaths`)
  -- each named flow needs its OWN equation elsewhere in the file. Anything else
  (a compound rate expression not reducible to signed flow names) is refused on the
  stock, since Naga's `flows` are named, single-purpose, two-ended records (like
  XMILE's), not Vensim's implicit "whatever INTEG's argument is" flows.
* Vensim never clips: every flow is `biflow=True` and every stock has `min=None`
  (a units-comment range like `[0,?]` is a slider bound, not a constraint).
* `Name[Dim]` on the LHS declares dims, exactly like XMILE's `<dimensions>`; a bracket
  naming neither a declared range nor an element is refused. PHASE6 S3: several
  subscripted equations for one name, an element in the LHS brackets (`x[s1] = ...`)
  or a number list (`x[Region] = 1, 2, 3`) are per-element equations (`_per_element`:
  each position runs over its root range, the other equations become `elements`
  overrides; per-element stocks must share one INTEG rate). In the RHS a bare
  reference or an explicit `Growth[Region]`/`Pop[n1]`/`SUM(Pop[North!])` goes through
  `sdimport._index` (a range of the owner or linked to it, an element, or a star).
* `WITH LOOKUP(expr, ([xmin,ymin]-[xmax,ymax],)? (x1,y1),(x2,y2),...)` becomes a
  `tables` entry plus a synthetic auxiliary `TABLE("<synthetic>", <expr>)`,
  substituted in place -- the same anonymous-lookup idea as XMILE's `<gf>`. A
  standalone Vensim "lookup variable" (declared as `Name(points...)`, called
  `Name(x)`) is a `tables` entry and each call `TABLE("Name", x)`.
* `DELAY1I`/`DELAY3I` rename to `DELAY1`/`DELAY3` (their 3-arg form is identical);
  `DELAY N` normalises to `DELAYN`; `ACTIVE INITIAL`/`SAMPLE IF TRUE` (Vensim's
  space-separated names) normalise to the engine's `ACTIVE_INITIAL`/`SAMPLE_IF_TRUE`.
  `ZIDZ`/`XIDZ`/`MODULO`/`INITIAL`/`TREND`/`FORECAST`/`NPV`/`NOISE_NORMAL`/`SMOOTH`/
  `SMOOTH3`/`DELAY1`/`DELAY3`/`DELAYN` pass through ONLY IF `core.expressions` still
  defines them under those exact names when this module loads (PHASE6 S2's function
  pack, developed concurrently) -- otherwise that one function is refused rather
  than emitting a call the engine cannot compile.
* A `:` declaration (`Region: East, West, South`) is a subscript range; a range name
  among its elements is expanded, and a range whose elements are a strict subset of
  another's is a subrange (`parent`, the largest superset). `-> Zone` or
  `-> (Zone: z3, z1)` is a mapping (`map_to`, PHASE6 S3); `<->`/`<-` equivalences,
  several mapping targets are refused; a numeric range `(a1-a3)` expands.
* `{...}` (Vensim's brace comment, used anywhere including the `{UTF-8}` marker) is
  stripped before anything else; the `\\\\---///` sketch-section marker truncates the
  rest of the file (view/diagram data, never modelling content).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from itertools import product
from pathlib import Path

from nagasim import sdimport
from nagasim.core import sd
from nagasim.core.expressions import FUNCTION_NAMES
from nagasim.core.problems import problem

__all__ = ["MDLError", "import_mdl", "load_mdl", "main"]

MAX_BYTES = sdimport.MAX_BYTES
_q = sdimport._q


class MDLError(ValueError):
    """A file refused before parsing: bad size/encoding, or -- since there is no
    partial layout worth returning for it -- malformed enough that no equation can
    be found. Content problems (an unsupported construct) are never raised; they are
    appended to the returned `problems` list instead, like `sdimport.XMILEError`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _LookupSyntax(Exception):
    """A `WITH LOOKUP(...)` call could not be split into (expr, points); the
    caller turns this into one `E_SDIMPORT_UNSUPPORTED` on the owning variable."""


# ---------------------------------------------------------------- pre-parse text
_COMMENT_RE = re.compile(r"\{[^}]*\}", re.DOTALL)
_SKETCH_MARKER = r"\\\---///"
_MACRO_RE = re.compile(r":MACRO:.*?:END OF MACRO:", re.IGNORECASE | re.DOTALL)
_GET_RE = re.compile(r"\bGET\s+(XLS|DIRECT)\s+\w+", re.IGNORECASE)
_EQN_HEAD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*(\[[^\]]*\])?\s*=(.*)$", re.DOTALL)
_DIM_HEAD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*:\s*(.*)$", re.DOTALL)
_WITH_LOOKUP_RE = re.compile(r"WITH\s+LOOKUP\s*\(", re.IGNORECASE)
_NUMBERS_RE = re.compile(r"\s*[-+0-9.eE]+(?:\s*[,;]\s*[-+0-9.eE]+)+\s*;?\s*")
# Vensim spellings this reader normalises before tokenizing, to what the shared
# tokenizer/parser (`sdimport._Parser`, written for XMILE) already understands: its
# logical operators are colon-wrapped, not bare `AND`/`OR`/`NOT`. Vensim's conditional
# is a 3-arg function call, `IF THEN ELSE(cond, a, b)` -- NOT XMILE/our grammar's
# `IF..THEN..ELSE` KEYWORD form, which a bare "IF" would collide with (`_Parser._if`
# greedily treats a leading IF token as that keyword) -- so it becomes the synthetic
# call name "IFTE", renamed to "IF" at emit time (_VENSIM_RENAME) like DELAY1I/DELAY3I.
_MULTIWORD = {re.compile(r"\bIF\s+THEN\s+ELSE\b", re.IGNORECASE): "IFTE",
              re.compile(r"\bACTIVE\s+INITIAL\b", re.IGNORECASE): "ACTIVE_INITIAL",
              re.compile(r"\bSAMPLE\s+IF\s+TRUE\b", re.IGNORECASE): "SAMPLE_IF_TRUE",
              re.compile(r"\bDELAY\s+N\b", re.IGNORECASE): "DELAYN",
              re.compile(r"\bPULSE\s+TRAIN\b", re.IGNORECASE): "PULSE_TRAIN",
              re.compile(r":\s*NA\s*:", re.IGNORECASE): "(-6.490371073168535e+32)",  # -2^109
              re.compile(r":\s*AND\s*:", re.IGNORECASE): " AND ",
              re.compile(r":\s*OR\s*:", re.IGNORECASE): " OR ",
              re.compile(r":\s*NOT\s*:", re.IGNORECASE): " NOT "}
_CONTROL_NAMES = {"FINAL TIME", "INITIAL TIME", "TIME STEP", "SAVEPER"}
# A standalone lookup: `Name( [(0,0)-(1,1)], (0,0), (1,1) )`, no `=`, called `Name(x)`.
_LOOKUP_DEF_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_ ]*?)\s*\((.*)\)\s*$", re.DOTALL)
_CONTINUATION_RE = re.compile(r"\\[ \t]*\r?\n")

# Vensim's function set, layered on sdimport's XMILE one (_emit/_translate's optional
# rename_funcs/passthrough_funcs -- see sdimport.py's docstring).
_VENSIM_RENAME = dict(sdimport._RENAME_FUNCS)
_VENSIM_RENAME.update({"DELAY1I": "DELAY1", "DELAY3I": "DELAY3", "IFTE": "IF",
                       "SMOOTHI": "SMOOTH", "SMOOTH3I": "SMOOTH3",
                       # {arity: template} (sdimport._emit): Vensim's own argument orders.
                       "DELAYN": {4: "DELAYN({0}, {1}, {3}, {2})"},  # (in, tau, init, n)
                       "PULSE": {2: "PULSE(1, {0}, {1})"},            # (start, width)
                       "LOG": {2: "(log({0}) / log({1}))"},           # (x, base)
                       "GAME": {1: "({0})"},                          # no gaming in a run
                       "POWER": {2: "({0} ** {1})"}, "INTEGER": {1: "int({0})"},
                       "QUANTUM": {2: "IF({1} <= 0, {0}, {1} * int({0} / {1}))"},
                       # (start, width, interval, end): exact on the SD grid only
                       "PULSE_TRAIN": {4: "IF((TNOW >= {0}) and (TNOW <= {3}) and "
                                          "(MODULO(TNOW - {0}, {2}) < {1}), 1, 0)"}})
_VENSIM_EXTRA = ("ZIDZ", "XIDZ", "MODULO", "INITIAL", "ACTIVE_INITIAL", "SAMPLE_IF_TRUE",
                 "TREND", "FORECAST", "NPV", "NOISE_NORMAL", "SMOOTH", "SMOOTH3",
                 "DELAY1", "DELAY3", "DELAYN")
# Gated on core.expressions.FUNCTION_NAMES (PHASE6 S2, developed concurrently): a name
# not (or no longer) defined there is refused instead of emitted (module docstring).
_VENSIM_PASSTHROUGH = frozenset(sdimport._PASSTHROUGH_FUNCS) | frozenset(
    name for name in _VENSIM_EXTRA if name in FUNCTION_NAMES)


def _decode(raw: bytes) -> str:
    if b"\x00" in raw[:4096]:
        raise MDLError("E_SDIMPORT_MDL", "the file is not text (a NUL byte in the first 4 KB)")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")     # never raises (module docstring, ENCODING)


def _match_paren(text: str, open_idx: int):
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _top_level_comma(text: str):
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            return i
    return None


def _extract_with_lookup(text: str, owner: str):
    """Replace each `WITH LOOKUP(expr, (points))` in *text* with a bare placeholder
    identifier, returning `(new_text, [(placeholder, expr_text, points_text)])`; a
    malformed call raises `_LookupSyntax`."""
    out, specs, pos = [], [], 0
    while True:
        m = _WITH_LOOKUP_RE.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        open_paren = m.end() - 1
        end = _match_paren(text, open_paren)
        if end is None:
            raise _LookupSyntax()
        inner = text[open_paren + 1:end]
        comma = _top_level_comma(inner)
        if comma is None:
            raise _LookupSyntax()
        expr_text = inner[:comma].strip()
        points_text = inner[comma + 1:].strip()
        if points_text.startswith("(") and points_text.endswith(")"):
            points_text = points_text[1:-1].strip()
        points_text = re.sub(r"^\[[^\]]*\]\s*,?\s*", "", points_text)   # optional axis-range hint
        placeholder = f"__lookup_{owner}_{len(specs)}__"
        specs.append((placeholder, expr_text, points_text))
        out.append(placeholder)
        pos = end + 1
    return "".join(out), specs


def _parse_points(points_text: str):
    pairs = re.findall(r"\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)", points_text)
    return [float(x) for x, _ in pairs], [float(y) for _, y in pairs]


# ---------------------------------------------------------------- statement handling
def _handle_subscript_range(stmt: str, dim_names: set, subscripts: list, problems: list) -> None:
    head = stmt.split("~", 1)[0]
    m = _DIM_HEAD_RE.match(head)
    if not m:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"cannot parse subscript range \"{head.strip()[:60]}\"", "/model"))
        return
    name, rest = m.group(1).strip(), m.group(2)
    if re.search(r"\s", name):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"dimension '{name}': names with a space are not translated", "/model"))
        return
    if "<-" in rest:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"dimension '{name}': a subscript equivalence ('<->'/'<-') is not translated "
            f"(a subrange is a range of a range's elements, a mapping '->')", "/model"))
        return
    rest, _, mapping = rest.partition("->")
    elements = [e.strip() for e in rest.split(",") if e.strip()]
    numeric = re.fullmatch(r"\(\s*([A-Za-z_]\w*?)(\d+)\s*-\s*\1(\d+)\s*\)",
                           elements[0]) if len(elements) == 1 else None
    if numeric:         # Vensim's numeric range `(a1-a40)`
        prefix, first, last = numeric.group(1), int(numeric.group(2)), int(numeric.group(3))
        elements = [f"{prefix}{i}" for i in range(first, last + 1)]
    if not elements:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED", f"dimension '{name}': no elements",
                                "/model"))
        return
    entry = {"name": name, "elements": elements}
    mapping = mapping.strip()
    if mapping:     # PHASE6 S3: `-> Zone` (by position) or `-> (Zone: z2, z1)`
        explicit = re.fullmatch(r"\(\s*([A-Za-z_]\w*)\s*:(.*)\)", mapping, re.DOTALL)
        if explicit:
            entry["map_to"] = {"dim": explicit.group(1), "elements": [
                e.strip() for e in explicit.group(2).split(",") if e.strip()]}
        elif re.fullmatch(r"[A-Za-z_]\w*", mapping):
            entry["map_to"] = {"dim": mapping}
        else:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"dimension '{name}': mapping '-> {mapping[:40]}' is not translated (one "
                f"target range, by position or as '(Range: e1, e2, ...)')", "/model"))
    subscripts.append(entry)
    dim_names.add(name)


def _link_ranges(subscripts: list) -> None:
    """Vensim writes a range's elements through other ranges (`Region: North,
    s1`) and marks a subrange only by its elements (PHASE6 S3): expand range
    names in element lists, then give each range whose elements are a strict
    subset of another range the largest such range as `parent`."""
    by_name = {entry["name"]: entry for entry in subscripts}
    for entry in subscripts:
        entry["elements"] = [e for token in entry["elements"]
                             for e in (by_name[token]["elements"]
                                       if token in by_name and token != entry["name"]
                                       else [token])]
    for entry in subscripts:
        own = set(entry["elements"])
        supersets = [other for other in subscripts if own < set(other["elements"])]
        if supersets:       # the largest, so nested subranges all share one root
            entry["parent"] = max(supersets, key=lambda other: len(other["elements"]))["name"]


def _is_range(head: str) -> bool:
    return "=" not in head and ":" in head


def _lookup_def(head: str):
    """(name, points text) of a standalone lookup definition, or None."""
    m = None if "=" in head or ":" in head else _LOOKUP_DEF_RE.match(head)
    return (m.group(1).strip(), m.group(2)) if m else None


def _lookups(statements: list, problems: list) -> dict:
    """{name: table record} of every standalone lookup (after `_canonical_names`)."""
    out = {}
    for stmt in statements:
        found = _lookup_def(stmt.split("~", 1)[0])
        if found:
            name, points = "_".join(found[0].split()), found[1]
            xs, ys = _parse_points(re.sub(r"^\s*\[[^\]]*\]\s*,?", "", points))
            if len(xs) < 2:
                problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                    f"{name}: a lookup needs at least two points", f"/model/variables/{name}"))
                continue
            out[name] = {"name": name, "keys": xs, "values": ys, "key_mode": "linear"}
    return out


def _canonical_names(statements: list) -> list:
    """*statements* with every declared variable name, however it is spelt
    (spaces, underscores, case: one name to Vensim), rewritten to its first
    head's spelling with underscores for spaces -- one identifier token for
    the shared tokenizer (DEVLOG #164). Only the equation part (before the
    first `~`) is touched; sim-control names are left alone."""
    names: dict = {}
    for stmt in statements:
        head = stmt.split("~", 1)[0]
        m = _EQN_HEAD_RE.match(head) if "=" in head else None
        if m:
            raw = " ".join(m.group(1).split())
            if raw.upper() not in _CONTROL_NAMES:
                names.setdefault(sdimport._fold(raw), raw.replace(" ", "_"))
        elif _lookup_def(head):
            raw = " ".join(_lookup_def(head)[0].split())
            names.setdefault(sdimport._fold(raw), raw.replace(" ", "_"))
        elif _is_range(head):       # a range or element name with a space, too
            name, rest = head.split(":", 1)
            for raw in [name] + rest.split("->", 1)[0].split(","):
                raw = " ".join(raw.split())
                if " " in raw and re.fullmatch(r"[A-Za-z_][\w ]*", raw):
                    names.setdefault(sdimport._fold(raw), raw.replace(" ", "_"))
    if not names:
        return statements
    words = sorted(names, key=len, reverse=True)       # longest first: `a b` before `a`
    pattern = re.compile(r"(?<![\w.])(?:" + "|".join(
        r"[\s_]+".join(map(re.escape, name.split(" "))) for name in words) + r")(?!\w)",
        re.IGNORECASE)

    def canon(match):
        return names[sdimport._fold(match.group())]
    out = []
    for stmt in statements:
        eqn, sep, rest = stmt.partition("~")
        out.append(pattern.sub(canon, eqn) + sep + rest
                   if "=" in eqn or _is_range(eqn) else stmt)
    return out


def _handle_equation(stmt: str, subs: dict, control: dict, dims_of: dict, ast_by_name: dict,
                     lookup_specs: dict, order: list, problems: list, defs: dict) -> None:
    parts = stmt.split("~")
    eqn_text, units_text = parts[0], (parts[1].strip() if len(parts) > 1 else "")
    m = _EQN_HEAD_RE.match(eqn_text)
    if not m:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"cannot parse the equation head of \"{eqn_text.strip()[:60]}\"", "/model"))
        return
    name_raw, dims_bracket, rhs_text = m.group(1), m.group(2), m.group(3)
    key = re.sub(r"\s+", " ", name_raw.strip()).upper()
    if key in _CONTROL_NAMES:
        control[key] = (rhs_text.strip(), units_text)
        return
    name = name_raw.strip()
    # A second subscripted equation for the same name is a per-element one
    # (PHASE6 S3), gathered in `defs` and resolved by `_per_element`.
    continuation = name in ast_by_name
    if continuation and not (dims_bracket and name in defs):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: a duplicate equation is not translated", f"/model/variables/{name}"))
        return
    if _GET_RE.search(rhs_text):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: references external data (GET XLS/DIRECT ...); refused -- this reader "
            f"never opens another file", f"/model/variables/{name}"))
        return

    dims = []
    if dims_bracket:
        for tok in dims_bracket.strip("[]").split(","):
            tok = tok.strip()
            if tok not in subs and not any(tok in elements for elements in subs.values()):
                problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                    f"{name}: '[{tok}]' names no declared dimension or element",
                    f"/model/variables/{name}"))
                return
            if tok in dims:
                problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                    f"{name}: repeats the dimension '{tok}' (`x[A,A]`); not translated",
                    f"/model/variables/{name}"))
                return
            dims.append(tok)

    if dims_bracket and _NUMBERS_RE.fullmatch(rhs_text):
        try:            # `x[Region] = 1, 2, 3`: one constant per element (PHASE6 S3)
            node = ("list", [float(v) for v in re.split(r"[,;]", rhs_text) if v.strip()])
        except ValueError:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"{name}: cannot parse \"{rhs_text.strip()[:60]}\"", f"/model/variables/{name}"))
            return
    else:
        node = _parse_rhs(name, rhs_text, dims, subs, continuation, dims_of, lookup_specs,
                          order, problems)
        if node is None:
            return
    if continuation:
        defs[name].append((dims, node))
        return
    ast_by_name[name] = node
    dims_of[name] = dims
    order.append(name)
    defs[name] = [(dims, node)]


def _parse_rhs(name, rhs_text, dims, subs, continuation, dims_of, lookup_specs, order,
               problems):
    """The parsed right-hand side, its WITH LOOKUPs registered; None after a
    problem."""
    for rx, repl in _MULTIWORD.items():
        rhs_text = rx.sub(repl, rhs_text)

    try:
        rhs_text, specs = _extract_with_lookup(rhs_text, name)
    except _LookupSyntax:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: WITH LOOKUP(...) is malformed", f"/model/variables/{name}"))
        return None
    if specs and (continuation or any(t not in subs for t in dims)):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: WITH LOOKUP in a per-element equation is not translated",
            f"/model/variables/{name}"))
        return None
    for placeholder, expr_text, points_text in specs:
        xs, ys = _parse_points(points_text)
        if len(xs) < 2:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"{name}: WITH LOOKUP has fewer than two points", f"/model/variables/{name}"))
            return None
        lookup_specs[placeholder] = (expr_text, xs, ys)
        dims_of[placeholder] = dims
        order.append(placeholder)

    tokens = sdimport._tokenize(rhs_text)
    if any(kind == "junk" for kind, _ in tokens):
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: cannot tokenize \"{rhs_text.strip()[:60]}\"", f"/model/variables/{name}"))
        return None
    try:
        return sdimport._Parser(tokens).parse()
    except sdimport._Skip:
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: cannot parse \"{rhs_text.strip()[:60]}\"", f"/model/variables/{name}"))
    except RecursionError:          # DEVLOG #162: a recursive-descent parser's limit
        problems.append(problem("E_SDIMPORT_UNSUPPORTED",
            f"{name}: expression nests too deep", f"/model/variables/{name}"))
    return None


def _per_element(defs: dict, subscripts: list, ast_by_name: dict, dims_of: dict, order: list,
                 problems: list) -> dict:
    """{name: {"a,x": node}} for every variable given per element (PHASE6 S3):
    several subscripted equations, an element in its LHS brackets, or a number
    list. Each LHS position runs over its root range (a subrange's parent, an
    element's range); the first element's node becomes the shared equation.
    Equations over different dims, or a number list of the wrong length, drop
    the variable."""
    subs = sd.subscripts_of({"subscripts": subscripts})
    parent = {entry["name"]: entry["parent"] for entry in subscripts if entry.get("parent")}
    out = {}
    for name, found in defs.items():
        if len(found) == 1 and found[0][1][0] != "list" and all(t in subs for t in found[0][0]):
            continue
        roots, covered = None, {}
        try:
            for tokens, node in found:
                axes = [(parent.get(t, t), subs[t]) if t in subs else
                        (next(d for d in subs if d not in parent and t in subs[d]), [t])
                        for t in tokens]
                if roots is None:
                    roots = [d for d, _ in axes]
                    if len(set(roots)) < len(roots):
                        raise ValueError(f"it repeats a dimension ({', '.join(roots)})")
                if [d for d, _ in axes] != roots:
                    raise ValueError("its equations run over different dimensions")
                keys = [",".join(combo) for combo in product(*(elements for _, elements in axes))]
                nodes = [("num", v) for v in node[1]] if node[0] == "list" else [node] * len(keys)
                if len(nodes) != len(keys):
                    raise ValueError(f"{len(nodes)} numbers for {len(keys)} elements")
                covered.update(zip(keys, nodes))
        except ValueError as exc:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"{name}: per-element equations not translated: {exc}", f"/model/variables/{name}"))
            order.remove(name)
            del ast_by_name[name]
            continue
        shared = next(iter(covered.values()))
        ast_by_name[name], dims_of[name] = shared, roots
        out[name] = {key: node for key, node in covered.items() if node is not shared}
    return out


# ---------------------------------------------------------------- INTEG / flow linkage
def _flow_terms(node):
    """A top-level `+`/`-` chain of bare names as `[(sign, name), ...]`, or `None`
    when *node* is not reducible to one (a compound rate expression)."""
    kind = node[0]
    if kind == "name":
        return [(1, node[1])]
    if kind == "neg":
        inner = _flow_terms(node[1])
        return None if inner is None else [(-s, n) for s, n in inner]
    if kind == "bin" and node[1] in ("+", "-"):
        left, right = _flow_terms(node[2]), _flow_terms(node[3])
        if left is None or right is None:
            return None
        sign = 1 if node[1] == "+" else -1
        return left + [(sign * s, n) for s, n in right]
    return None


def _classify(order: list, ast_by_name: dict, problems: list):
    """(stock names, `{flow_name: {"source"?, "target"?}}`): a stock is any `INTEG`
    equation whose rate reduces to signed bare flow names (see module docstring)."""
    stock_names: set = set()
    flow_link: dict = {}
    for name in order:
        node = ast_by_name.get(name)
        if node is None or node[0] != "call" or node[1] != "INTEG":
            continue
        args = node[2]
        if len(args) != 2:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"{name}: INTEG needs exactly (rate, initial)", f"/model/variables/{name}"))
            continue
        terms = _flow_terms(args[0])
        if terms is None:
            problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                f"{name}: INTEG's rate is not a plain flow name or a sum/difference of flow "
                f"names (Naga flows must be named, single-purpose rates)",
                f"/model/variables/{name}"))
            continue
        ok = True
        for sign, flow_name in terms:
            if flow_name not in ast_by_name:
                problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                    f"{name}: INTEG references '{flow_name}', which has no equation of its own",
                    f"/model/variables/{name}"))
                ok = False
                break
            link = flow_link.setdefault(flow_name, {})
            end = "target" if sign > 0 else "source"
            if end in link:
                problems.append(problem("E_SDIMPORT_UNSUPPORTED",
                    f"{flow_name}: feeds or drains more than one stock (Naga's flows join "
                    f"exactly two ends)", f"/model/variables/{flow_name}"))
                ok = False
                break
            link[end] = name
        if ok:
            stock_names.add(name)
    return stock_names, flow_link


# ---------------------------------------------------------------- sim_specs (.Control)
def _sim_specs(control: dict, problems: list) -> dict:
    def resolve(key, default, seen=()):
        if key not in control:
            return default
        text = control[key][0].strip()
        try:
            return float(text)
        except ValueError:
            pass
        ref = re.sub(r"\s+", " ", text).upper()
        if ref in control and ref not in seen:
            return resolve(ref, default, seen + (key,))
        problems.append(problem("W_SDIMPORT_APPROX",
            f"{key} = '{text}' is not a plain number; using {default}",
            "/simulation/time_limit"))
        return default

    start = resolve("INITIAL TIME", 0.0)
    stop = resolve("FINAL TIME", start + 1.0)
    dt = resolve("TIME STEP", 1.0)
    unit_text = (control.get("FINAL TIME") or control.get("TIME STEP") or ("", ""))[1]
    unit_name = sdimport.time_unit(unit_text, problems)
    if start:
        problems.append(problem("W_SDIMPORT_APPROX",
            f"INITIAL TIME={start!r} shifted to 0 (Naga's clock always starts at 0)",
            "/simulation/time_limit"))
    return {"time_limit": stop - start, "time_unit": unit_name,
            "sd": {"dt": dt, "method": "euler", "time_unit": "base"}}


# ---------------------------------------------------------------- layout assembly
def _build_layout(order, ast_by_name, dims_of, lookup_specs, stock_names, flow_link,
                  subscripts, problems, simulation, overrides, lookups) -> dict:
    stock_dims = {name: dims_of.get(name, []) for name in stock_names}
    flow_dims = {name: dims_of.get(name, []) for name in flow_link if name in ast_by_name}
    aux_dims = {name: dims_of.get(name, []) for name in order
               if name not in stock_names and name not in flow_dims}

    rename = {**_VENSIM_RENAME, **{name.upper(): {1: f"TABLE({_q(name)}, {{0}})"}
                                   for name in lookups}}

    def emit(node, name):
        return sdimport._emit(node, stock_dims, flow_dims, aux_dims, problems, name,
                              rename, _VENSIM_PASSTHROUGH, subscripts)

    def per_element(name, pick=lambda node: node) -> dict:
        found = overrides.get(name)
        return {"elements": {key: emit(pick(node), name) for key, node in found.items()}
                } if found else {}

    levels, flows, auxiliaries, tables = [], [], [], list(lookups.values())

    for name in order:
        if name not in stock_names:
            continue
        init_ast = ast_by_name[name][2][1]
        terms = _flow_terms(ast_by_name[name][2][0])
        try:
            initial = emit(init_ast, name)
            if any(node[:2] != ("call", "INTEG") or len(node[2]) != 2
                   or _flow_terms(node[2][0]) != terms for node in overrides.get(name, {}).values()):
                sdimport._refuse(problems, name, "per-element INTEG equations must share one "
                                                 "rate (the same flows)")
            extra = per_element(name, lambda node: node[2][1])
        except sdimport._Skip:
            continue
        levels.append({**sd.Stock(name, initial=initial, min=None,
                                  dims=stock_dims[name]).to_dict(),
                       **extra})

    for name in order:
        if name not in flow_dims:
            continue
        link = flow_link.get(name, {})
        source, target = link.get("source"), link.get("target")
        if (source and source not in stock_names) or (target and target not in stock_names):
            continue        # the stock it belonged to was dropped (already problem'd)
        try:
            rate = emit(ast_by_name[name], name)
            extra = per_element(name)
        except sdimport._Skip:
            continue
        # Unbounded, both ways (module docstring). sd.Flow has no `unit` field.
        flows.append({**sd.Flow(name, rate=rate, source=source, target=target, biflow=True,
                                dims=flow_dims[name]).to_dict(), **extra})

    for name in order:
        if name in stock_names or name in flow_dims:
            continue
        dims = aux_dims[name]
        if name in lookup_specs:
            expr_text, xs, ys = lookup_specs[name]
            try:
                driving = sdimport._translate(expr_text, stock_dims, flow_dims, aux_dims,
                                              problems, name, rename, _VENSIM_PASSTHROUGH,
                                              subscripts)
            except sdimport._Skip:
                continue
            tables.append({"name": name, "keys": xs, "values": ys, "key_mode": "linear"})
            expr, extra = f'TABLE({_q(name)}, {driving})', {}
        else:
            try:
                expr, extra = emit(ast_by_name[name], name), per_element(name)
            except sdimport._Skip:
                continue
        auxiliaries.append({**sd.Aux(name, expr=expr, dims=dims).to_dict(), **extra})

    return {"levels": levels, "flows": flows, "auxiliaries": auxiliaries, "tables": tables,
            "subscripts": subscripts, "simulation": simulation}


# ---------------------------------------------------------------- top level
def import_mdl(text_or_bytes) -> tuple:
    """Translate a Vensim `.mdl` document (str or bytes) into `(layout, problems)`.

    Pure: no file or network access, and never raises for a construct this module
    cannot translate -- only for a pre-parse security refusal (`MDLError`)."""
    raw = text_or_bytes if isinstance(text_or_bytes, bytes) else text_or_bytes.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise MDLError("E_SDIMPORT_MDL",
                       f"the document is {len(raw)} bytes, over the {MAX_BYTES} byte cap")
    text = _decode(raw)
    text = _COMMENT_RE.sub(" ", text)
    text = _CONTINUATION_RE.sub(" ", text)      # a `\` line continuation
    sketch = text.find(_SKETCH_MARKER)
    if sketch != -1:
        text = text[:sketch]

    problems: list = []
    text = _MACRO_RE.sub(lambda m: problems.append(problem(
        "E_SDIMPORT_UNSUPPORTED", "a :MACRO: definition is not translated (no macro expansion; "
                                  "every variable it declares is left out)", "/model")) or " ",
                         text)

    statements = [s.strip() for s in text.split("|")]
    statements = [s for s in statements if s]

    statements = _canonical_names(statements)
    dim_names: set = set()
    subscripts: list = []
    for stmt in statements:
        head = stmt.split("~", 1)[0]
        if _is_range(head):
            _handle_subscript_range(stmt, dim_names, subscripts, problems)
    _link_ranges(subscripts)
    subs = sd.subscripts_of({"subscripts": subscripts})
    lookups = _lookups(statements, problems)

    control: dict = {}
    dims_of: dict = {}
    ast_by_name: dict = {}
    lookup_specs: dict = {}
    order: list = []
    defs: dict = {}
    for stmt in statements:
        head = stmt.split("~", 1)[0]
        if "=" in head:
            _handle_equation(stmt, subs, control, dims_of, ast_by_name,
                             lookup_specs, order, problems, defs)
    overrides = _per_element(defs, subscripts, ast_by_name, dims_of, order, problems)

    simulation = _sim_specs(control, problems)
    stock_names, flow_link = _classify(order, ast_by_name, problems)
    layout = _build_layout(order, ast_by_name, dims_of, lookup_specs, stock_names,
                           flow_link, subscripts, problems, simulation, overrides, lookups)
    return layout, problems


def load_mdl(path) -> tuple:
    """`import_mdl` on a file: the size cap is checked from `stat()`, before the file
    is even read. *path* is the caller's to confine -- this function trusts it, like
    `Path.read_bytes` trusts any path it is given (same rule as `sdimport.load_xmile`)."""
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise MDLError("E_SDIMPORT_MDL", f"'{p}' is {size} bytes, over the {MAX_BYTES} byte cap")
    return import_mdl(p.read_bytes())


def main(argv=None) -> int:
    """`naga sdimport model.mdl [-o out.json]`: translate a Vensim file into an
    Naga model JSON and print every import problem."""
    parser = argparse.ArgumentParser(
        prog="naga sdimport", description="Translate a Vensim (.mdl) file into a Naga model JSON.")
    parser.add_argument("source", help="path to the .mdl file")
    parser.add_argument("-o", "--out", help="write the model JSON here instead of stdout")
    args = parser.parse_args(argv)

    try:
        layout, problems = load_mdl(args.source)
    except (MDLError, OSError) as exc:
        code = getattr(exc, "code", "E_SDIMPORT_MDL")
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
