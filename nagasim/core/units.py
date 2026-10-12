"""Unit checking for system dynamics (PHASE5 SD-3, PHASE5_PLAN.md Phase 5A
Constructs row "Units"). Pure/stdlib: `parse` turns a unit string into a
canonical `Unit` (base symbol -> exponent), `infer` walks an expression's own
AST to guess its unit, `check` compares a layout's declared level/flow/aux
units against each other and against what their expressions compute.

`simulation.sd.units` (PHASE6 S9) picks the mode: "off" skips `check`
entirely (return `[]` before anything is built); "warn" (the default, and
today's only behaviour before S9) reports `W_UNIT_MISMATCH`/`W_UNIT_TIME`;
"strict" reports the same two conditions as `E_UNIT_MISMATCH`/`E_UNIT_TIME`
instead. A model with no units, or one this module cannot parse
(`W_UNIT_UNPARSED`, never promoted -- a malformed string is not a mismatch),
never blocks a run in "warn"; "strict" also warns (never errors, since
"missing" is not "wrong") on a level/flow with no declared unit at all
(`W_UNIT_MISSING`). `check` runs only when `simulation.sd` exists; wiring it
into the rest of validation is the coordinator's job.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from nagasim.core.expressions import SD_MACROS, functions_with
from nagasim.core.problems import problem
from nagasim.core.timeunits import UNITS as MODEL_TIME_UNITS

__all__ = ["Unit", "WILDCARD", "UnitParseError", "UnitMismatchError", "parse", "infer",
           "check", "Env"]

class UnitParseError(ValueError):
    """`parse` could not make sense of a unit string."""

class UnitMismatchError(ValueError):
    """`infer` found `+`/`-` combining two expressions with different units."""

# Time symbols normalise to one "time" dimension: `scale` is how many seconds
# one unit of "time" is, so two units with equal dims but a different scale
# are compatible (W_UNIT_TIME) rather than mismatched (W_UNIT_MISMATCH).
_TIME_SYMBOLS = {
    "s": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0,
    "week": 604800.0, "weeks": 604800.0,
    "month": 2592000.0, "months": 2592000.0,        # fixed 30/365 days (core/timeunits.py)
    "year": 31536000.0, "years": 31536000.0,
}
_TIME_NAMES = {1.0: "s", 60.0: "min", 3600.0: "h", 86400.0: "day", 604800.0: "week",
               2592000.0: "month", 31536000.0: "year"}

def _merge(dims_a, dims_b, sign=1):
    acc = dict(dims_a)
    for symbol, exponent in dims_b:
        acc[symbol] = acc.get(symbol, 0) + sign * exponent
    return tuple(sorted((s, e) for s, e in acc.items() if e != 0))

@dataclass(frozen=True)
class Unit:
    """A canonical dimension -> exponent mapping, e.g. `kg*m/s^2` is
    `(("kg", 1), ("m", 1), ("time", -2))`, `scale == 1.0` (seconds).
    `wildcard=True` (a numeric literal, or anything unresolvable) unifies
    with any other unit with no warning -- never a false positive."""

    dims: tuple = ()
    scale: float = 1.0
    wildcard: bool = False

    # A product/quotient touching a wildcard is a wildcard: in `r * LEVEL("P")`
    # the untyped `r` is usually a rate (1/day), so adopting P's unit would warn
    # on every canonical SD flow (DEVLOG #125). Only fully typed terms compare.
    def __mul__(self, other: "Unit") -> "Unit":
        if self.wildcard or other.wildcard:
            return WILDCARD
        return Unit(_merge(self.dims, other.dims, 1), self.scale * other.scale)

    def __truediv__(self, other: "Unit") -> "Unit":
        if self.wildcard or other.wildcard:
            return WILDCARD
        return Unit(_merge(self.dims, other.dims, -1), self.scale / other.scale)

    def __pow__(self, exponent: int) -> "Unit":
        if self.wildcard:
            return WILDCARD
        dims = tuple(sorted((s, e * exponent) for s, e in self.dims if e * exponent != 0))
        return Unit(dims, self.scale ** exponent)

    def __str__(self) -> str:
        if self.wildcard:
            return "*"
        if not self.dims:
            return "dimensionless"
        num, den = [], []
        for symbol, exponent in self.dims:
            name = symbol
            if symbol == "time":
                root = self.scale ** (1.0 / exponent)
                name = next((n for value, n in _TIME_NAMES.items()
                            if abs(value - root) < 1e-6 * max(value, 1.0)), "s")
            text = name if abs(exponent) == 1 else f"{name}^{abs(exponent)}"
            (num if exponent > 0 else den).append(text)
        head = "*".join(num) or "1"
        return head if not den else f"{head}/{'/'.join(den)}"

WILDCARD = Unit(wildcard=True)

_TOKEN_RE = re.compile(r"\*\*|[A-Za-z_]+|\d+|[*/^]")

def parse(text: str) -> Unit:
    """"people", "people/day", "kg*m/s^2", "1/day" into a canonical `Unit`.
    "" / "dimensionless" mean no dimension. No parentheses, no negative
    exponents (`m^-1`) -- not needed by any unit this codebase writes."""
    raw = text.strip()
    if raw == "" or raw.lower() == "dimensionless":
        return Unit()
    tokens = _TOKEN_RE.findall(raw)
    if "".join(tokens) != raw.replace(" ", ""):
        raise UnitParseError(f"cannot parse unit {text!r}")

    dims: dict = {}
    scale = 1.0
    sign = 1
    i, n = 0, len(tokens)
    expect_operand = True
    while i < n:
        tok = tokens[i]
        if expect_operand:
            if tok in ("*", "/", "^", "**"):
                raise UnitParseError(f"cannot parse unit {text!r}")
            if tok.isdigit():
                i += 1
            else:
                symbol = tok
                i += 1
                exponent = 1
                if i < n and tokens[i] in ("^", "**"):
                    i += 1
                    if i >= n or not tokens[i].isdigit():
                        raise UnitParseError(f"cannot parse unit {text!r}")
                    exponent = int(tokens[i])
                    i += 1
                e = sign * exponent
                if symbol in _TIME_SYMBOLS:
                    dims["time"] = dims.get("time", 0) + e
                    scale *= _TIME_SYMBOLS[symbol] ** e
                else:
                    dims[symbol] = dims.get(symbol, 0) + e
            sign = 1
            expect_operand = False
        else:
            if tok == "*":
                sign = 1
            elif tok == "/":
                sign = -1
            else:
                raise UnitParseError(f"cannot parse unit {text!r}")
            i += 1
            expect_operand = True
    if expect_operand:
        raise UnitParseError(f"cannot parse unit {text!r}")
    return Unit(tuple(sorted((s, e) for s, e in dims.items() if e != 0)), scale)

@dataclass
class Env:
    """What `infer` resolves a bare LEVEL/AUX/DELAY name against: a dict of
    level units, the model's base time unit, and a callable per lookup kind
    so `check` can memoise and cycle-guard self-referencing auxiliaries."""

    levels: dict = field(default_factory=dict)
    time_unit: Unit = field(default_factory=Unit)
    aux_lookup: object = None    # callable(name) -> Unit
    delay_lookup: object = None  # callable(name) -> Unit
    table_lookup: object = None  # callable(name) -> (unit_x, unit_y), PHASE6 S9
    series_lookup: object = None  # callable(name) -> Unit, PHASE6 S9

    def level_unit(self, name: str) -> Unit:
        return self.levels.get(name, WILDCARD)

    def aux_unit(self, name: str) -> Unit:
        return self.aux_lookup(name) if self.aux_lookup else WILDCARD

    def delay_unit(self, name: str) -> Unit:
        return self.delay_lookup(name) if self.delay_lookup else WILDCARD

    def table_unit(self, name: str) -> tuple:
        return self.table_lookup(name) if self.table_lookup else (WILDCARD, WILDCARD)

    def series_unit(self, name: str) -> Unit:
        return self.series_lookup(name) if self.series_lookup else WILDCARD

def infer(expression, env: Env) -> Unit:
    """The unit an expression's value carries, walked off its own AST (not
    `core.expressions.Expression`'s flattened summary -- `+`/`-` need the
    real tree). Accepts raw text or anything with a `.text` attribute.
    Raises `UnitMismatchError` only for a `+`/`-` of two differing units;
    every other unresolvable case is `WILDCARD`, never a false positive."""
    text = getattr(expression, "text", expression)
    text = "" if text is None else str(text)
    if not text.strip():
        return WILDCARD
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        return WILDCARD  # E_EXPRESSION already flags a bad expression elsewhere
    return _infer_node(tree, env)

def _combine_additive(a: Unit, b: Unit) -> Unit:
    if a.wildcard:
        return b
    if b.wildcard:
        return a
    if a.dims != b.dims:
        raise UnitMismatchError(f"{a} + {b}")
    return a

def _unify(a: Unit, b: Unit) -> Unit:
    """IF/min/max/ternary: adopt whichever side is known; a mismatch falls
    back to WILDCARD instead of raising -- softer than +/-, since a
    conditional's branches need not share a unit to be a legitimate model."""
    if a.wildcard:
        return b
    if b.wildcard:
        return a
    return a if a.dims == b.dims else WILDCARD

# A "unit_lookup" function's unit is Env.<name>_unit(<its literal first argument>).
_LITERAL_LOOKUPS = {name: f"{name.lower()}_unit" for name in functions_with("unit_lookup")}
_DELAY_MACROS = tuple(SD_MACROS)

def _infer_call(node: ast.Call, env: Env) -> Unit:
    if not isinstance(node.func, ast.Name):
        return WILDCARD
    name, args = node.func.id, node.args
    if name in _LITERAL_LOOKUPS:
        if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            return getattr(env, _LITERAL_LOOKUPS[name])(args[0].value)
        return WILDCARD  # a computed name -- cannot say which level/aux/delay
    if name == "IF" and len(args) == 3:
        return _unify(_infer_node(args[1], env), _infer_node(args[2], env))
    if name in ("min", "max") and args:
        result = _infer_node(args[0], env)
        for extra in args[1:]:
            result = _unify(result, _infer_node(extra, env))
        return result
    if name in ("SUM", "MEAN") and args:
        return _infer_node(args[0], env)
    if name in ("STEP", "PULSE") and args:
        return _infer_node(args[0], env)          # the height argument
    if name == "RAMP" and args:
        return _infer_node(args[0], env) * env.time_unit   # slope * time
    if name in _DELAY_MACROS and args:
        return _infer_node(args[0], env)          # x, the delayed/smoothed input
    # ---------------------------------------------------------- PHASE6 S9
    # S2's function pack, unit rules per docs/PHASE6_PLAN.md S9 (a's unit
    # unless noted): ZIDZ/XIDZ divide like `/` would; MODULO keeps a's unit,
    # matching the `%` operator below; INITIAL/FORECAST/NPV read the input
    # ("stream" for NPV) unchanged -- Vensim's NPV is the same unit as the
    # stream it discounts, not stream*time; SAMPLE_IF_TRUE holds x, its
    # second argument; TREND is a fractional rate of change, 1/time; PROD/
    # VMIN/VMAX reduce same-unit subscript elements, so (like SUM/MEAN above)
    # the first argument's unit stands in for the reduction; NOISE_NORMAL
    # takes its mean's unit.
    if name in ("ZIDZ", "XIDZ") and len(args) >= 2:
        return _infer_node(args[0], env) / _infer_node(args[1], env)
    if name == "MODULO" and args:
        return _infer_node(args[0], env)
    if name in ("INITIAL", "FORECAST", "NPV") and args:
        return _infer_node(args[0], env)
    if name == "SAMPLE_IF_TRUE" and len(args) >= 2:
        return _infer_node(args[1], env)
    if name == "TREND" and args:
        return Unit() / env.time_unit
    if name in ("PROD", "VMIN", "VMAX") and args:
        return _infer_node(args[0], env)
    if name == "NOISE_NORMAL" and args:
        return _infer_node(args[0], env)
    if name in ("TABLE", "TABLE_EXTRAPOLATE") and args:
        return _infer_table(args, env)
    if name == "TS" and args:
        if isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            return env.series_unit(args[0].value)
        return WILDCARD                      # a computed series name
    return WILDCARD                          # anything unregistered


def _infer_table(args, env: Env) -> Unit:
    """TABLE/TABLE_EXTRAPOLATE("name", x[, y]) (PHASE6 S9): the table's own
    `unit_y` is the call's unit; `x` is compared against the table's `unit_x`
    the same way `+`/`-` compares two operands (a real mismatch raises, a
    wildcard on either side never does) -- only the first key, matching
    `unit_x` being one unit for a possibly 2-D table."""
    if not (isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)):
        return WILDCARD                      # a computed table name
    unit_x, unit_y = env.table_unit(args[0].value)
    if len(args) >= 2 and not unit_x.wildcard:
        x_unit = _infer_node(args[1], env)
        if not x_unit.wildcard and x_unit.dims != unit_x.dims:
            raise UnitMismatchError(
                f'TABLE("{args[0].value}") x: declared {unit_x}, argument {x_unit}')
    return unit_y

def _infer_node(node: ast.AST, env: Env) -> Unit:
    if isinstance(node, ast.Constant):
        return WILDCARD                      # a bare number is a wildcard
    if isinstance(node, ast.Name):
        return env.time_unit if node.id in ("TNOW", "now") else WILDCARD
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, (ast.USub, ast.UAdd)):
            return _infer_node(node.operand, env)
        return WILDCARD
    if isinstance(node, ast.BinOp):
        left = _infer_node(node.left, env)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return _combine_additive(left, _infer_node(node.right, env))
        if isinstance(node.op, ast.Mult):
            return left * _infer_node(node.right, env)
        if isinstance(node.op, (ast.Div, ast.FloorDiv)):
            return left / _infer_node(node.right, env)
        if isinstance(node.op, ast.Mod):
            return left                      # a % b keeps a's unit
        if isinstance(node.op, ast.Pow):
            exponent = node.right
            if (isinstance(exponent, ast.Constant) and isinstance(exponent.value, int)
                    and not isinstance(exponent.value, bool)):
                return left ** exponent.value
            return WILDCARD                  # a non-literal exponent: cannot scale dims safely
        return WILDCARD
    if isinstance(node, ast.IfExp):
        return _unify(_infer_node(node.body, env), _infer_node(node.orelse, env))
    if isinstance(node, ast.Call):
        return _infer_call(node, env)
    return WILDCARD

def _declared(text: str, kind: str, name: str, path: str, problems: list) -> Unit:
    if not text:
        return WILDCARD
    try:
        return parse(text)
    except UnitParseError:
        problems.append(problem("W_UNIT_UNPARSED",
                                f'{kind} "{name}": unit "{text}" could not be parsed',
                                path, block=name))
        return WILDCARD

def _mismatch_code(strict: bool) -> str:
    """W_UNIT_MISMATCH/W_UNIT_TIME become E_UNIT_MISMATCH/E_UNIT_TIME under
    `simulation.sd.units == "strict"` (PHASE6 S9); every other unit code
    (UNPARSED, MISSING) always stays a warning."""
    return "E_UNIT_MISMATCH" if strict else "W_UNIT_MISMATCH"

def _time_code(strict: bool) -> str:
    return "E_UNIT_TIME" if strict else "W_UNIT_TIME"

def _compare(problems: list, path: str, block: str, subject: str,
             a_label: str, a: Unit, b_label: str, b: Unit, strict: bool = False) -> None:
    if a.dims != b.dims:
        problems.append(problem(_mismatch_code(strict),
                                f"{subject}: {a_label} {a}, {b_label} {b}", path, block=block))
    elif abs(a.scale - b.scale) > 1e-9 * max(abs(a.scale), abs(b.scale), 1.0):
        problems.append(problem(_time_code(strict),
                                f"{subject}: {a_label} {a}, {b_label} {b} (different time scale)",
                                path, block=block))

def _resolve(section: str, kind: str, field_name: str, by_name: dict, memo: dict,
             resolving: set, name: str, env: Env, problems: list, strict: bool = False) -> Unit:
    """The declared-or-inferred unit of one auxiliary/delay: memoised and
    cycle-safe (a real cycle is E_AUX_CYCLE's job elsewhere; here it is
    just WILDCARD so this checker never recurses forever)."""
    if name in memo:
        return memo[name]
    if name not in by_name or name in resolving:
        return WILDCARD
    resolving.add(name)
    index, entry = by_name[name]
    declared = _declared(entry.get("unit") or "", kind, name, f"/{section}/{index}/unit", problems)
    try:
        inferred = infer(entry.get(field_name) or "", env)
    except UnitMismatchError as exc:
        problems.append(problem(_mismatch_code(strict), f'{kind} "{name}": {exc}',
                                f"/{section}/{index}/{field_name}", block=name))
        inferred = WILDCARD
    if not declared.wildcard and not inferred.wildcard:
        _compare(problems, f"/{section}/{index}/unit", name, f'{kind} "{name}"',
                "declared", declared, "computed", inferred, strict)
    resolving.discard(name)
    memo[name] = declared if not declared.wildcard else inferred
    return memo[name]

def check(layout: dict) -> list:
    """Unit warnings (or, under "strict", errors) for one layout, only when
    `simulation.sd` is set (SD-1). Levels/flows/auxiliaries with no unit
    anywhere involved produce nothing in "warn"/"off" -- opt-in, like the
    rest of PHASE5's unit checking; "strict" (PHASE6 S9) additionally flags a
    level/flow with NO declared unit at all, as a warning (a missing unit
    cannot itself be wrong)."""
    if not isinstance(layout, dict) or not (layout.get("simulation") or {}).get("sd"):
        return []
    sd_settings = (layout.get("simulation") or {}).get("sd")
    mode = sd_settings.get("units", "warn") if isinstance(sd_settings, dict) else "warn"
    if mode not in ("off", "warn", "strict"):
        mode = "warn"                          # an unrecognised value fails open, silently
    if mode == "off":
        return []
    strict = mode == "strict"
    problems: list = []

    model_time_name = (layout.get("simulation") or {}).get("time_unit") or "minutes"
    model_scale = MODEL_TIME_UNITS.get(model_time_name, MODEL_TIME_UNITS["minutes"])
    time_unit = Unit((("time", 1),), model_scale)

    level_units: dict = {}
    for index, entry in enumerate(layout.get("levels") or []):
        name = entry.get("name")
        if not name:
            continue
        text = entry.get("unit") or ""
        level_units[name] = _declared(text, "level", name, f"/levels/{index}/unit", problems)
        if strict and not text:
            problems.append(problem("W_UNIT_MISSING", f'level "{name}": no unit declared',
                                    f"/levels/{index}/unit", block=name,
                                    hint='add a "unit" (e.g. "people", "kg") so mismatches can be checked'))

    # Tables' unit_x/unit_y and time series' unit (PHASE6 S9): parsed here so
    # TABLE()/TS() calls anywhere below (a flow rate, an auxiliary, a delay
    # input) can carry them through `infer` via `Env.table_unit`/`series_unit`.
    table_units: dict = {}
    for index, entry in enumerate(layout.get("tables") or []):
        name = entry.get("name")
        if not name:
            continue
        unit_x = _declared(entry.get("unit_x") or "", "table", name,
                           f"/tables/{index}/unit_x", problems)
        unit_y = _declared(entry.get("unit_y") or "", "table", name,
                           f"/tables/{index}/unit_y", problems)
        table_units[name] = (unit_x, unit_y)

    series_units: dict = {}
    for index, entry in enumerate(layout.get("timeseries_inputs") or []):
        name = entry.get("name")
        if not name:
            continue
        series_units[name] = _declared(entry.get("unit") or "", "time series", name,
                                       f"/timeseries_inputs/{index}/unit", problems)

    aux_by_name = {e["name"]: (i, e) for i, e in enumerate(layout.get("auxiliaries") or [])
                  if e.get("name")}
    delay_by_name = {e["name"]: (i, e) for i, e in enumerate(layout.get("delays") or [])
                     if e.get("name")}
    aux_memo, aux_resolving = {}, set()
    delay_memo, delay_resolving = {}, set()

    def aux_unit(name):
        return _resolve("auxiliaries", "auxiliary", "expr", aux_by_name, aux_memo,
                        aux_resolving, name, env, problems, strict)

    def delay_unit(name):
        return _resolve("delays", "delay", "input", delay_by_name, delay_memo,
                        delay_resolving, name, env, problems, strict)

    env = Env(level_units, time_unit, aux_unit, delay_unit,
              lambda n: table_units.get(n, (WILDCARD, WILDCARD)),
              lambda n: series_units.get(n, WILDCARD))
    for name in aux_by_name:
        aux_unit(name)
    for name in delay_by_name:
        delay_unit(name)

    for index, entry in enumerate(layout.get("flows") or []):
        name = entry.get("name") or f"#{index}"
        path = f"/flows/{index}"
        source = level_units.get(entry.get("from"), WILDCARD)
        target = level_units.get(entry.get("to"), WILDCARD)
        level_u = target if source.wildcard else source
        expected = level_u / time_unit if not level_u.wildcard else WILDCARD
        text = entry.get("unit") or ""
        declared = _declared(text, "flow", name, f"{path}/unit", problems)
        if strict and not text:
            problems.append(problem("W_UNIT_MISSING", f'flow "{name}": no unit declared',
                                    f"{path}/unit", block=name,
                                    hint='add a "unit" (e.g. "people/day") so mismatches can be checked'))
        try:
            inferred = infer(entry.get("rate") or "0", env)
        except UnitMismatchError as exc:
            problems.append(problem(_mismatch_code(strict), f'flow "{name}": {exc}',
                                    f"{path}/rate", block=name))
            inferred = WILDCARD

        candidates = []
        if not expected.wildcard:
            candidates.append(("level unit / time", expected))
        if not declared.wildcard:
            candidates.append(("declared unit", declared))
        if not inferred.wildcard:
            candidates.append(("rate expression", inferred))
        if len(candidates) >= 2:
            pivot_label, pivot = candidates[0]
            for label, unit in candidates[1:]:
                _compare(problems, f"{path}/unit", name, f'flow "{name}"',
                        pivot_label, pivot, label, unit, strict)

    return problems
