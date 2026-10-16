"""`naga fmt`: a model file in its shortest canonical form (D471, D472).

The short form is the migrated model with every key left out that `schema.migrate` writes
back with the same value: a default parameter, a block's description equal to its name, a
unit that is the model's, an empty section. It is found, not listed: strip the candidates,
migrate, put back what came back different, until migrating the short form gives the model
again. So a short file and a long one are the same model to every reader, and `minimal`
refuses to return one that is not (`migrate(minimal(m)) == migrate(m)`).

Layout (positions, annotations, the network, the background) is kept where present: it is
not something migrate writes. Keys are put in one order: `$schema`, `schema_version`, the
sections in `sections.SECTIONS` order; a block's own keys, then its parameters in their
`ParamSpec` order.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

from nagasim.core import schema, sections
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.modelschema import SCHEMA_ID

__all__ = ["minimal", "dumps", "render", "main", "BLOCK_KEYS", "WIDTH"]

WIDTH = 100         # an array or object goes on one line when it fits in this many columns
_SECTION_ORDER = {s.name: i for i, s in enumerate(sections.SECTIONS)}
_BLOCK_ORDER = ("name", "type", "description", "position", "layout_position", "layout_rotation",
                "label_offset", "params")
# A block's own keys besides name, type and params (the builder takes them as keywords).
BLOCK_KEYS = _BLOCK_ORDER[2:-1]
_DEFINITION_ORDER = ("info", "parameters", "scoped", "extends", "remove", "blocks", "links",
                     "annotations")      # ModelDocument's order
_MISSING = object()


def _blocks(layout: dict):
    """Every block dict of the model and of its submodel definitions."""
    levels = [layout] + [d for d in (layout.get("submodels") or {}).values() if isinstance(d, dict)]
    return [b for level in levels for b in level.get("blocks") or () if isinstance(b, dict)]


def _strip_simulation(layout: dict) -> None:
    if isinstance(layout.get("simulation"), dict):
        layout["simulation"] = {}


def _strip_rest(layout: dict) -> None:
    for section in sections.SECTIONS:
        if section.default is not sections.MISSING and layout.get(section.name) == section.default:
            del layout[section.name]
    for block in _blocks(layout):
        block.pop("description", None)
        block.pop("params", None)
    for section in sections.DOCUMENT_LISTS:
        for record in layout.get(section.name) or ():
            for key in [k for k in record if k.endswith("unit")] if isinstance(record, dict) else ():
                del record[key]


def _same(a, b) -> bool:
    """Equal as JSON, type-exact: `0` is not `0.0` and `1` is not `true` (Python's `==` says
    they are, but the model hash, `json.dumps`, tells them apart: #498)."""
    if a is _MISSING or b is _MISSING:
        return a is b
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _restore(short, refilled, full) -> bool:
    """Put back into *short* what migrating it did not give back as *full* has it. Returns
    whether anything was put back. A dict left out entirely comes back empty first, its keys
    then one by one, so a section keeps only what it needs."""
    changed = False
    if isinstance(full, dict):
        for key, value in full.items():
            again = refilled.get(key, _MISSING) if isinstance(refilled, dict) else _MISSING
            if key not in short:
                if _same(again, value):
                    continue
                short[key] = {} if isinstance(value, dict) and value else copy.deepcopy(value)
                changed = True
                if not short[key] and isinstance(value, dict):
                    _restore(short[key], again, value)
            elif not _same(again, value) and isinstance(value, (dict, list)):
                changed |= _restore(short[key], again, value)
    elif isinstance(full, list) and isinstance(short, list) and isinstance(refilled, list) \
            and len(short) == len(full) == len(refilled):
        for mine, again, value in zip(short, refilled, full):
            changed |= _restore(mine, again, value)
    return changed


def _put_first(record: dict, keys) -> None:
    mine = {k: record.pop(k) for k in keys if k in record}
    rest = dict(record)
    record.clear()
    record.update(mine | rest)


def _ordered(layout: dict) -> dict:
    for block in _blocks(layout):
        spec = BLOCK_REGISTRY.get(str(block.get("type", "")).upper())
        if isinstance(block.get("params"), dict) and spec is not None:
            rank = {p.name: i for i, p in enumerate(spec.params)}
            block["params"] = dict(sorted(block["params"].items(),
                                          key=lambda kv: rank.get(kv[0], len(rank))))
        _put_first(block, _BLOCK_ORDER)
    for definition in (layout.get("submodels") or {}).values():
        if isinstance(definition, dict):
            _put_first(definition, _DEFINITION_ORDER)
    order = sorted(layout, key=lambda k: (-1 if k == "schema_version" else
                                          _SECTION_ORDER.get(k, len(_SECTION_ORDER))))
    return {"$schema": SCHEMA_ID, **{k: layout[k] for k in order}}


def minimal(layout) -> dict:
    """The shortest form of *layout* (a path or a dict) that migrates to the same model."""
    full = schema.migrate(schema.read_layout(layout))
    # A library or a mesmap migrates too (to an empty model with its own keys kept): the
    # schema's top level admits no key it does not name, so neither does fmt.
    unknown = sorted(set(full) - _SECTION_ORDER.keys() - {"$schema", "schema_version"})
    if unknown:
        raise ValueError(f"not a model file (top-level keys {unknown})")
    short = copy.deepcopy(full)
    # The simulation first: its units decide what every other unit is filled with.
    for strip in (_strip_simulation, _strip_rest):
        strip(short)
        while not _same(refilled := schema.migrate(short), full):
            if not _restore(short, refilled, full):
                raise ValueError("fmt: the short form does not migrate back to the model")
    return _ordered(short)


def _flat(value, leaf) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{leaf(k)}: {_flat(v, leaf)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flat(v, leaf) for v in value) + "]"
    if isinstance(value, float) and not math.isfinite(value):    # not JSON, no Python literal
        raise ValueError(f"non-finite number {value!r}")
    return leaf(value)


def render(value, indent: int = 0, start: int = 0, leaf=json.dumps, step: int = 2) -> str:
    """*value* as JSON (or, with `leaf=repr`, as a Python literal) starting at column
    *start*: on one line when it fits in WIDTH, else one item a line at *indent* + *step*."""
    flat = _flat(value, leaf)
    if not isinstance(value, (dict, list)) or not value or start + len(flat) <= WIDTH:
        return flat
    inner = indent + step
    pad = " " * inner
    if isinstance(value, dict):
        heads = [f"{pad}{leaf(k)}: " for k in value]
        body = [head + render(v, inner, len(head), leaf, step) for head, v in zip(heads, value.values())]
        return "{\n" + ",\n".join(body) + "\n" + " " * indent + "}"
    body = [pad + render(v, inner, len(pad), leaf, step) for v in value]
    return "[\n" + ",\n".join(body) + "\n" + " " * indent + "]"


def dumps(layout: dict) -> str:
    """Two-space JSON, ASCII, an array or object on one line when it fits in WIDTH."""
    return render(layout) + "\n"


def main(argv=None) -> int:
    """`naga fmt FILE... [--check]`."""
    parser = argparse.ArgumentParser(
        prog="naga fmt",
        description="Rewrite model files in their shortest canonical form: every value the "
                    "engine fills in anyway left out, keys in one order, \"$schema\" first. "
                    "The model, and every run of it, stays the same.")
    parser.add_argument("files", nargs="+", help="model JSON files, rewritten in place")
    parser.add_argument("--check", action="store_true",
                        help="change nothing; name each file not in that form and exit 1")
    args = parser.parse_args(argv)
    unformatted = 0
    for name in args.files:
        if format_file(name, check=args.check):
            unformatted += 1
            sys.stdout.write(f"{name}: {'not formatted' if args.check else 'formatted'}\n")
    return 1 if args.check and unformatted else 0


def format_file(path, *, check: bool = False) -> bool:
    """Rewrite the model file *path* in the short form (unless *check*); returns whether it
    was not in that form. Writes *path* and nothing else."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    short = dumps(minimal(json.loads(text)))
    if short == text:
        return False
    if not check:
        path.write_text(short, encoding="utf-8", newline="\n")
    return True
