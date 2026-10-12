"""Agent vocabulary shared by every layer of `core.agents`: trigger and
activation names, the expression-function sets the checks and the compiler
read, stream seeds (D107), the rate forms (D94) and the small value and
data-table readers both validation and the runtime use."""
from __future__ import annotations

import ast
import hashlib
import math

import numpy as np

from nagasim.core.base_process import deterministic_hash
from nagasim.core.expressions import ExpressionError, compile_expression, functions_with
from nagasim.core.tables import _is_number, fold_key


TRIGGERS = ("timeout", "rate", "condition", "message", "tick")
ACTIVATIONS = ("synchronous", "random", "sequential")      # PHASE6 A1
_TICK_TAG = deterministic_hash("tick")
_AGENT_TAG = deterministic_hash("agent")    # D107: the last word of every agent-system stream key
# The per-instant cap's floor; the run's cap is max(this, 100 x its agents)
# so a legitimate simultaneous firing of every agent never trips it (#131).
MAX_FIRINGS_PER_INSTANT = 10_000
_RUNAWAY = ("look for a condition that stays true across its own transition, a zero timeout "
            "loop or two agents messaging each other for ever")
# What an OWN rate factor may call: the same value for the same agent whatever
# else happens in the model (D94).
_PURE = frozenset(functions_with("pure"))
_READ_ONLY = ("state", "id")
# Reads of model state that moves outside the agents with no instant known
# ahead: held between agent events unless the transition sets `scan`.
_HELD_CALLS = frozenset(functions_with("held"))
_CLOCK = ("TNOW", "now")
# Draws from the rng: per agent and per firing.
_SAMPLER_CALLS = frozenset(functions_with("sampler"))
# Grid space (PHASE6 A4, D102): functions that read self's CELL without naming
# `self` -- per agent, so never vectorised as a shared scalar nor a SHARED rate.
_GRID_CALLS = frozenset(functions_with("grid"))
# Offsets in a fixed order: move_random draws an index into this list.
_NEIGHBOURHOODS = {"moore": ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)),
                   "von_neumann": ((0, -1), (-1, 0), (1, 0), (0, 1))}
_PATCH_TAG = deterministic_hash("patch")
# Links and networks (PHASE6 A5, D103): functions that read self's LINKS
# without naming `self` -- per agent, like the grid calls above.
_LINK_CALLS = frozenset(functions_with("link"))
NETWORK_KINDS = ("none", "random", "small_world", "scale_free", "table")
# Heading and steering (PHASE6 A6, D104): a continuous space's edge rule for
# forward/random_walk -- "none" (unbounded, move_to's own convention, D68) is
# the default, kept so a bare space stays exactly as before.
_EDGES = ("none", "clamp", "wrap", "bounce")
_NETWORK_TAG = deterministic_hash("network")


def stream_seed(seed: int, name: str, *words: int) -> np.random.SeedSequence:
    """An agent-system stream's SeedSequence (D107): `[seed, lo, hi, *words,
    _AGENT_TAG]`, (lo, hi) the first 64 bits of md5(name) as two 32-bit words.
    A block/level/resource key is `[seed, hash(name)]`, zero-padded to 4 words,
    so no key ending in the nonzero tag at index >= 3 can equal one (#149), and
    64 bits make two population names alike only by chance (2**-64)."""
    h = int.from_bytes(hashlib.md5(name.encode()).digest()[:8], "little")
    return np.random.SeedSequence([seed, h & 0xFFFFFFFF, h >> 32, *words, _AGENT_TAG])


# ---------------------------------------------------------------- rate forms
def _reads_self(node) -> bool:
    return any(isinstance(n, ast.Name) and (n.id == "self" or n.id in _GRID_CALLS
                                            or n.id in _LINK_CALLS)
               for n in ast.walk(node))


def _own_only(node, parameters) -> bool:
    """Only self.<attr>, parameters, constants and pure functions."""
    functions = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name) or n.func.id not in _PURE:
                return False
            functions.add(id(n.func))
    return all(n.id in ("self", "true", "false") or n.id in parameters
               for n in ast.walk(node) if isinstance(n, ast.Name) and id(n) not in functions)


def _factors(node, power: int, out: list) -> None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
        _factors(node.left, power, out)
        _factors(node.right, -power if isinstance(node.op, ast.Div) else power, out)
    else:
        out.append((node, power))


def _product(factors) -> str | None:
    if not factors:
        return None
    text = "*".join(f"({ast.unparse(n)})" for n, p in factors if p > 0) or "1"
    return text + "".join(f"/({ast.unparse(n)})" for n, p in factors if p < 0)


def rate_parts(text: str, parameters=()) -> tuple[str | None, str | None]:
    """(own, shared) expression texts of a rate (D94), None for an absent
    part. Raises ValueError for a factor that mixes `self` with anything that
    is not a parameter, a constant or a pure function."""
    tree = ast.parse(str(text), mode="eval").body
    factors: list = []
    _factors(tree, 1, factors)
    own = [(n, p) for n, p in factors if _reads_self(n)]
    for node, _power in own:
        if not _own_only(node, parameters):
            raise ValueError(f"'{ast.unparse(node)}' mixes self with shared state")
    return _product(own), _product([(n, p) for n, p in factors if not _reads_self(n)])


def timing(text: str, linear=frozenset()) -> tuple:
    """(instants, held, continuous) of a rate or condition: the times at which
    it can change (None if a STEP/PULSE time is computed), the held reads, and
    what varies continuously with time (a bare TNOW, RAMP, a TS in *linear*)."""
    compiled = compile_expression(text)
    instants = None if compiled.instants is None else set(compiled.instants)
    held = {"a computed STEP/PULSE time"} if instants is None else set()
    continuous: set = set()
    tree = ast.parse(str(text), mode="eval")
    solved = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            for clock, value in ((n.left, n.comparators[0]), (n.comparators[0], n.left)):
                if (isinstance(clock, ast.Name) and clock.id in _CLOCK
                        and isinstance(value, ast.Constant) and not isinstance(value.value, bool)
                        and isinstance(value.value, (int, float))):
                    solved.add(id(clock))
                    if instants is not None:
                        c = float(value.value)
                        instants.update((c, math.nextafter(c, math.inf)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in _CLOCK and id(n) not in solved:
            continuous.add("TNOW")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id == "RAMP":
                continuous.add("RAMP")
            elif n.func.id in _HELD_CALLS:
                held.add(n.func.id)
    continuous.update(f'TS("{name}")' for name in compiled.refs.get("TS", ()) if name in linear)
    if "TS" in compiled.computed_refs:
        held.add("TS with a computed name")
    return instants, held, continuous


def _norm360(degrees: float) -> float:
    """*degrees* folded into [0, 360): plain `% 360.0` can round a tiny
    negative value UP to the float 360.0 itself (its ULP there is ~4e-14),
    which would print as "360", not "0" -- fold that one case back."""
    h = degrees % 360.0
    return 0.0 if h == 360.0 else h


def _table_column(table: dict, column, categories=None) -> tuple[list | None, str | None]:
    """Table *table*'s column *column* (case-folded, D50) as one float per
    row, in row order -- what an attribute's `{"column": ...}` init (C3) and
    a grouped count's column sum both read. Tables stay numeric-only (the same
    contract TABLE()/schedules already rely on, enforced again when
    `core.tables.build_table` turns the SAME section into lookup tables): with
    *categories* (PHASE6 A2), a cell is the category's CODE (its 0-based
    position in the declared list), not its name. (values, None) on success,
    else (None, message)."""
    name = table.get("name", "")
    columns = table.get("columns")
    if not isinstance(columns, list):
        return None, f"table '{name}' has no \"columns\" to read from"
    index = {fold_key(c): i for i, c in enumerate(columns)}
    idx = index.get(fold_key(column))
    if idx is None:
        return None, (f"table '{name}' has no column '{column}' (declared: "
                      f"{', '.join(map(str, columns))})")
    out = []
    for row in table.get("values") or []:
        if not isinstance(row, list) or idx >= len(row) or not _is_number(row[idx]):
            return None, f"table '{name}': column '{column}' is not numeric for every row"
        cell = row[idx]
        if categories is not None and not (float(cell).is_integer()
                                           and 0 <= int(cell) < len(categories)):
            return None, (f"table '{name}': column '{column}' has value {cell!r}, not a valid "
                          f"category code (0..{len(categories) - 1})")
        out.append(float(cell))
    return out, None


def _resolve_count(value: dict, tables: dict) -> tuple[int | None, str | None]:
    """A population's `count` (C3): `{"table": name}` is the number of rows
    (one agent per row); `{"table": name, "column": col}` is the SUM of that
    column (a group table, e.g. a headcount per region). (count, None) on
    success, else (None, message)."""
    table_name = value.get("table")
    table = tables.get(table_name) if isinstance(table_name, str) else None
    if table is None:
        return None, f"unknown table '{table_name}'"
    column = value.get("column")
    if column is None:
        return len(table.get("keys") or []), None
    values, error = _table_column(table, column)
    if error:
        return None, error
    total = sum(values)
    if total != int(total) or total < 0:
        return None, f"column '{column}' sums to {total}, not a whole number of agents >= 0"
    return int(total), None


def _number(value, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ExpressionError(f"{what} = {value!r}: must be a number") from None
