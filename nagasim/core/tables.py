"""Lookup tables: the "tables" model section and the TABLE() expression
function (PHASE4 A1).

A table is a small named grid of numbers keyed by one or two dimensions --
process time by product and operation, a setup matrix, a tariff by hour. The
JSON shape is the CSV a human would write, so the two round-trip:

    {"name": "ProcessTime",
     "keys": ["Widget", "Gadget"],                 # dimension 1 (row keys)
     "columns": ["Cut", "Pack"],                   # dimension 2; absent = 1-D
     "values": [[3, 2], [4, 6]],                   # 2-D: one row per key
     "key_mode": "exact", "column_mode": "exact",  # exact | step | linear
     "default": null}                              # used when an exact key misses

A 1-D table has no "columns" and its "values" is a flat list, one per key.
Keys are strings (an entity type, a station) or numbers; values are numbers.
STRING KEYS MATCH CASE-INSENSITIVELY (DEVLOG #87): `Entity.e_type` upper-cases
whatever it is given, so a table keyed `"widget"` would otherwise be invisible
to `TABLE("T", entity.e_type)`. Two string keys differing only by case are a
duplicate-key validation error.

Lookup modes, per dimension:

* `exact`  -- the key must be present. A miss uses `default` if the table has
              one, else it is a run-time model error naming table and key.
* `step`   -- numeric keys, the largest key <= x (a tariff band).
* `linear` -- numeric keys, linear interpolation between the two neighbours.

`step` and `linear` CLAMP at both ends (below the first key the first value,
above the last the last), so neither can miss; `exact` is the mode where a
missing key means something. Their keys are SORTED when the table is built --
a hand-written table need not be in order and the values move with their keys.

`TABLE_EXTRAPOLATE("name", ...)` (PHASE6 S2) is the same lookup with
`extrapolate=True`: a `linear` axis outside its range keeps following the
slope of its last segment instead of clamping (`_Axis._slope`); `step`/`exact`
axes have no slope, so they are unchanged.

`build_tables` turns the section into `Table` objects once per run
(`ctx.tables`), so a `TABLE("ProcessTime", entity.e_type, "Cut")` costs one
dict lookup (exact) or one bisect (step/linear) per dimension.

Stdlib only (no numpy, no pandas): a leaf module beside `schedules.py`, with
its own structured validation, so `core/schema/` just calls it.
"""
from __future__ import annotations

import bisect
import csv
import io

from nagasim.core.problems import problem
from nagasim.core.utils import is_number as _is_number

__all__ = ["Table", "TableError", "LOOKUP_MODES", "build_tables", "fold_key", "table_csv",
           "table_from_csv", "validate_tables_structured"]

LOOKUP_MODES = ("exact", "step", "linear")


class TableError(ValueError):
    """A malformed table, or a lookup with no value and no default. Raised at
    run time; the expression evaluator turns it into an ExpressionError naming
    the block."""


def fold_key(value):
    """The form a STRING key is indexed and looked up under (DEVLOG #87).

    `Entity.e_type` upper-cases whatever it is given, so a table keyed
    `"widget"` could never be hit by `TABLE("T", entity.e_type)` -- the lookup
    fell through to `default`, or raised, with nothing to say why. Case-folding
    both sides makes the two spellings one key. Numbers are untouched.
    """
    return value.casefold() if isinstance(value, str) else value


class _Axis:
    """One dimension: its keys and how a lookup key is matched against them."""

    __slots__ = ("mode", "keys", "index")

    def __init__(self, keys, mode: str):
        self.mode = mode
        self.keys = tuple(keys)
        self.index = ({fold_key(k): i for i, k in enumerate(self.keys)}
                      if mode == "exact" else {})

    def weights(self, key, extrapolate=False):
        """[(index, weight)] for *key*, or None when an exact key is missing.
        Raises ValueError/TypeError for a non-numeric key on an interpolated
        axis; `Table.lookup` turns that into a named TableError. With
        *extrapolate*, a `linear` axis outside its range follows the slope of
        its last segment (`_slope`) instead of clamping (PHASE6 S2)."""
        if self.mode == "exact":
            index = self.index.get(fold_key(key))
            return None if index is None else ((index, 1.0),)
        x = float(key)
        last = len(self.keys) - 1
        position = bisect.bisect_right(self.keys, x) - 1
        if self.mode == "step":
            return ((min(max(position, 0), last), 1.0),)
        if position < 0:                        # below the first key
            return self._slope(x, 0, 1) if extrapolate and last >= 1 else ((0, 1.0),)
        if position >= last:                    # above the last key
            return self._slope(x, last - 1, last) if extrapolate and last >= 1 else ((last, 1.0),)
        span = self.keys[position + 1] - self.keys[position]
        weight = (x - self.keys[position]) / span
        return ((position, 1.0 - weight), (position + 1, weight))

    def _slope(self, x, i, j):
        """Linear weights for *x* OUTSIDE `[keys[i], keys[j]]` -- the same
        two-point formula an interior lookup uses, just with a weight outside
        [0, 1], so the value keeps following that segment's slope."""
        span = self.keys[j] - self.keys[i]
        weight = (x - self.keys[i]) / span
        return ((i, 1.0 - weight), (j, weight))


class Table:
    """A built lookup table: immutable, shareable, one per model table."""

    __slots__ = ("name", "dims", "default", "_axes", "_values")

    def __init__(self, name: str, axes, values, default=None):
        self.name = name
        self._axes = tuple(axes)
        self._values = values
        self.dims = len(self._axes)
        self.default = default

    def lookup(self, *keys, extrapolate=False):
        if len(keys) != self.dims:
            raise TableError(f"table '{self.name}' takes {self.dims} key(s), got {len(keys)}")
        picks = []
        for axis, key in zip(self._axes, keys):
            try:
                pick = axis.weights(key, extrapolate)
            except (TypeError, ValueError):
                raise TableError(f"table '{self.name}': lookup mode '{axis.mode}' needs a "
                                 f"numeric key, got {key!r}") from None
            if pick is None:
                if self.default is None:
                    raise TableError(f"table '{self.name}': no entry for key {key!r} "
                                     f"(declared: {', '.join(repr(k) for k in axis.keys)})")
                return self.default
            picks.append(pick)
        if self.dims == 1:
            return sum(weight * self._values[i] for i, weight in picks[0])
        return sum(wr * wc * self._values[r][c]
                   for r, wr in picks[0] for c, wc in picks[1])

    def __repr__(self):
        return f"<Table {self.name!r} dims={self.dims}>"


# ---------------------------------------------------------------- building
def _mode(entry: dict, key: str) -> str:
    mode = entry.get(key, "exact")
    if mode not in LOOKUP_MODES:
        raise TableError(f"table '{entry.get('name', '')}': unknown {key} '{mode}'")
    return mode


def _ordered(keys, mode: str):
    """(keys, order): an interpolated axis is sorted by key; `exact` keeps the
    authored order. `order` maps the new position back to the authored one, so
    the values can follow."""
    if mode == "exact":
        return list(keys), list(range(len(keys)))
    order = sorted(range(len(keys)), key=lambda i: float(keys[i]))
    return [float(keys[i]) for i in order], order


def build_table(entry: dict) -> Table:
    """One section entry as a `Table`. Raises TableError on anything malformed
    -- `validate_tables_structured` reports the same faults as Problems long
    before a run reaches here."""
    name = str(entry.get("name", ""))
    keys = list(entry.get("keys") or [])
    columns = entry.get("columns")
    values = entry.get("values") or []
    if not keys:
        raise TableError(f"table '{name}': no keys")
    if len(values) != len(keys):
        raise TableError(f"table '{name}': {len(keys)} keys but {len(values)} value rows")

    key_order_keys, key_order = _ordered(keys, _mode(entry, "key_mode"))
    axes = [_Axis(key_order_keys, _mode(entry, "key_mode"))]
    if columns is None:
        data = [_number(name, values[i]) for i in key_order]
    else:
        column_keys, column_order = _ordered(list(columns), _mode(entry, "column_mode"))
        axes.append(_Axis(column_keys, _mode(entry, "column_mode")))
        data = []
        for i in key_order:
            row = values[i]
            if not isinstance(row, (list, tuple)) or len(row) != len(columns):
                found = len(row) if isinstance(row, (list, tuple)) else 0
                raise TableError(f"table '{name}': row {i} has {found} values, "
                                 f"expected {len(columns)}")
            data.append([_number(name, row[j]) for j in column_order])
    default = entry.get("default")
    return Table(name, axes, data, None if default is None else _number(name, default))


def _number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TableError(f"table '{name}': value {value!r} is not a number")
    return float(value)


def build_tables(section) -> dict[str, Table]:
    """{name: Table} for the layout's "tables" section, built once per run."""
    return {str(entry.get("name", "")): build_table(entry) for entry in section or []}


# ---------------------------------------------------------------- validation
def validate_tables_structured(layout: dict) -> tuple[list, dict]:
    """Check the layout's "tables" section. Returns (list[Problem],
    {name: dimensions}); the dimension count is what `core/schema/` checks a
    `TABLE("name", ...)` call's key count against."""
    problems: list = []
    dims: dict[str, int] = {}
    for index, entry in enumerate(layout.get("tables", []) or []):
        at = f"/tables/{index}"
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_TABLE_NAME", "table: name must not be empty",
                                    f"{at}/name", hint="give every table a unique name"))
            continue
        if name in dims:
            problems.append(problem("E_TABLE_NAME", f"duplicate table name: {name}",
                                    f"{at}/name", hint="rename one of the two tables"))

        keys = entry.get("keys")
        columns = entry.get("columns")
        values = entry.get("values")
        dims[name] = 1 if columns is None else 2

        if not isinstance(keys, list) or not keys:
            problems.append(problem("E_TABLE_SHAPE", f"table '{name}': \"keys\" must be a "
                                                     f"non-empty list", f"{at}/keys",
                                    hint="list the key of every row, e.g. [\"Widget\", \"Gadget\"]"))
            continue
        if len(set(map(_hashable, keys))) != len(keys):
            problems.append(problem("E_TABLE_SHAPE", f"table '{name}': duplicate key",
                                    f"{at}/keys", hint="every key appears once"))
        if not isinstance(values, list) or len(values) != len(keys):
            problems.append(problem(
                "E_TABLE_SHAPE",
                f"table '{name}': {len(keys)} keys but "
                f"{len(values) if isinstance(values, list) else 0} value rows",
                f"{at}/values", hint="\"values\" has one entry per key"))
            continue

        for mode_key, axis_keys in (("key_mode", keys), ("column_mode", columns)):
            if mode_key == "column_mode" and columns is None:
                continue
            mode = entry.get(mode_key, "exact")
            if mode not in LOOKUP_MODES:
                problems.append(problem(
                    "E_TABLE_MODE", f"table '{name}': unknown {mode_key} '{mode}'",
                    f"{at}/{mode_key}", hint=f"use one of: {', '.join(LOOKUP_MODES)}"))
            elif mode != "exact" and not all(_is_number(k) for k in axis_keys):
                problems.append(problem(
                    "E_TABLE_MODE",
                    f"table '{name}': {mode_key} '{mode}' needs numeric keys",
                    f"{at}/{mode_key}",
                    hint="use \"exact\" for string keys; step/linear interpolate numbers"))

        if columns is not None and (not isinstance(columns, list) or not columns):
            problems.append(problem("E_TABLE_SHAPE",
                                    f"table '{name}': \"columns\" must be a non-empty list",
                                    f"{at}/columns",
                                    hint="drop \"columns\" for a 1-D table"))
            continue
        if columns is not None and len(set(map(_hashable, columns))) != len(columns):
            problems.append(problem("E_TABLE_SHAPE", f"table '{name}': duplicate column key",
                                    f"{at}/columns", hint="every column key appears once"))

        problems.extend(_value_problems(name, columns, values, at))
        default = entry.get("default")
        if default is not None and not _is_number(default):
            problems.append(problem("E_TABLE_VALUE",
                                    f"table '{name}': default {default!r} is not a number",
                                    f"{at}/default",
                                    hint="use a number, or drop \"default\" to make a missing "
                                         "key an error"))
    return problems, dims


def _value_problems(name: str, columns, values, at: str) -> list:
    problems = []
    for row_index, row in enumerate(values):
        if columns is None:
            if not _is_number(row):
                problems.append(problem("E_TABLE_VALUE",
                                        f"table '{name}': value {row!r} is not a number",
                                        f"{at}/values/{row_index}",
                                        hint="a 1-D table's \"values\" is a flat list of numbers"))
            continue
        if not isinstance(row, list) or len(row) != len(columns):
            problems.append(problem(
                "E_TABLE_SHAPE",
                f"table '{name}': row {row_index} has "
                f"{len(row) if isinstance(row, list) else 0} values, expected {len(columns)}",
                f"{at}/values/{row_index}", hint="every row has one value per column key"))
            continue
        for column_index, cell in enumerate(row):
            if not _is_number(cell):
                problems.append(problem(
                    "E_TABLE_VALUE", f"table '{name}': value {cell!r} is not a number",
                    f"{at}/values/{row_index}/{column_index}",
                    hint="every cell of a table is a number"))
    return problems


def _hashable(value):
    """The value a duplicate check compares. Keys come from JSON, so a list
    would be unhashable and its repr is enough; a string is case-folded, so
    two keys differing only by case are reported as the duplicate they now are
    (DEVLOG #87)."""
    if isinstance(value, str):
        return value.casefold()
    return value if isinstance(value, (int, float, bool)) else repr(value)


# ---------------------------------------------------------------- CSV
def table_csv(entry: dict) -> str:
    """One table as CSV text: 1-D is `key,value` under a `key,value` header;
    2-D is a header row of column keys (its first cell empty) and one row per
    key. `table_from_csv` reads back exactly this."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    keys = list(entry.get("keys") or [])
    columns = entry.get("columns")
    values = entry.get("values") or []
    if columns is None:
        writer.writerow(["key", "value"])
        writer.writerows([[k, v] for k, v in zip(keys, values)])
    else:
        writer.writerow(["", *columns])
        writer.writerows([[key, *row] for key, row in zip(keys, values)])
    return out.getvalue()


def table_from_csv(text: str, name: str, **fields) -> dict:
    """A section entry from CSV text (see `table_csv` for the two shapes).
    Extra *fields* (key_mode, column_mode, default, description) are copied
    onto the entry, so an import can keep the table's settings."""
    rows = [row for row in csv.reader(io.StringIO(text)) if any(str(c).strip() for c in row)]
    if not rows:
        raise TableError(f"table '{name}': the CSV is empty")
    header = rows[0]
    two_d = len(header) > 2 or not str(header[0]).strip()
    entry = {"name": name}
    if two_d:
        entry["columns"] = [_cell_key(c) for c in header[1:]]
        entry["keys"] = [_cell_key(row[0]) for row in rows[1:]]
        entry["values"] = [[_cell_value(name, c) for c in row[1:1 + len(entry["columns"])]]
                           for row in rows[1:]]
    else:
        entry["keys"] = [_cell_key(row[0]) for row in rows[1:]]
        entry["values"] = [_cell_value(name, row[1]) for row in rows[1:]]
    entry.update({k: v for k, v in fields.items() if v is not None})
    build_table(entry)          # refuse a ragged or non-numeric import here
    return entry


def _cell_key(cell):
    """A CSV key cell: a number when it reads as one, else the trimmed text."""
    text = str(cell).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number == int(number) and "." not in text else number


def _cell_value(name: str, cell) -> float:
    try:
        return float(str(cell).strip())
    except (ValueError, IndexError):
        raise TableError(f"table '{name}': value {cell!r} is not a number") from None
