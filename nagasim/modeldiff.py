"""Semantic diff for model files (PHASE6 W7 Q1, ARCHITECTURE D124).

A plain `git diff` on two model JSON files is a wall of reordered list noise:
the property panel writes blocks/resources/etc. back in whatever order the
editor's internal structures happen to hold them, so saving a model with no
real edit can still produce a huge textual diff. `diff(a, b)` instead matches
every record by its IDENTITY (a block by name, a link by source/target/port,
...), not by list position, and reports only what actually changed.

Engine side, per O8 (ARCHITECTURE section 10.1): stdlib only, no pandas, no
PySide6 -- `packages/engine/build_wheel.py`'s ENGINE list carries this file so
the CI job that installs the engine wheel alone still runs `naga diff`.

Identity keys are DISCOVERED from `nagasim.modelschema.build_json_schema()`,
not hard-coded per section: every top-level array property is a "record
list", and its identity is `name` or `id` when required, `(source, target[,
source_port, target_port])` when the item requires `source` and `target`
(links), `(from, to)` when it requires them (network segments), else there is
no identity key and records are matched by POSITION instead (documented
ceiling -- no current section falls into this case, but a future one that
does still gets a sane diff instead of a crash). `network` is a nested single
object rather than a list itself; its own `nodes`/`segments` get the same
treatment by name, one level down (ARCHITECTURE, W7 plan Q1).

Canvas-only fields -- geometry the editor writes for the user to look at, the
engine never reads -- are reported as kind "moved" instead of "changed", and
`naga diff` hides them unless asked with `--all`: `position`,
`layout_position`, `layout_rotation`, `label_offset` (block/segment/node
geometry, ARCHITECTURE 5.21/6.18/6.20) and `diagram` (an SD record's or an
agent state's diagram position, ARCHITECTURE 5.25 -- an agent type's `states`
are matched by name for this, `_NESTED_RECORDS`). Everything else -- including an
annotation's own `rect`/`color`/`text`, which are user CONTENT even though
they live on the canvas -- diffs as an ordinary "changed".

Nested dicts diff by key, recursively. A list that is not one of the record
sections above (a block's `assignments`, a resource_set's `members`, a
submodel definition's own `blocks`/`links`, ...) is a LEAF: it compares by
value, whole-list old vs new, with no attempt to match its items -- the plan's
"scalar or list leaves compare by value" rule. That is a real, documented
ceiling for a submodel definition's `blocks`/`links` specifically (they look
like the top-level sections but are one level too deep for the identity
lookup below); lifting it would mean generalising identity lookup to
arbitrary depth for no case that has come up yet.

`merge(base, ours, theirs)` (W7 Q2, ARCHITECTURE D125) is the three-way
merge on the same identity paths, so git can call `naga merge %O %A %B`
as a merge driver. The rules, per path: changed on one side takes that side,
changed identically on both is fine, changed differently is a Conflict
(merged keeps OURS); delete-vs-modify is a Conflict (merged keeps ours'
choice); canvas-only fields never conflict (ours wins, theirs' move applies
when ours left the field alone); a record added on both sides with different
content is a Conflict. List order: ours' order, then theirs' additions in
theirs' relative order. Merging is value-level only -- two edits of the same
expression string conflict, they are never merged inside the string.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from itertools import zip_longest
from pathlib import Path
from typing import Any

from nagasim.core import schema as schema_mod
from nagasim.core import submodels
from nagasim.modelschema import build_json_schema

__all__ = ["Change", "Conflict", "diff", "merge", "textconv", "main", "merge_main"]

# Purely visual fields: absent means "wherever the engine put it" / "default
# spot", the engine reads none of them, and their only effect is where
# something is drawn.
_CANVAS_FIELDS = {"position", "layout_position", "layout_rotation", "label_offset", "diagram"}

# Record lists one level inside a record, matched by identity like a section
# (DEVLOG #219): an agent type's statechart states by name, so dragging a
# state is a `moved` on that state, not a change of the whole list.
_NESTED_RECORDS = {"agent_types": {"states": ("name",)}}


@dataclass(frozen=True)
class Change:
    kind: str  # "added" | "removed" | "changed" | "moved"
    path: str
    old: Any = None
    new: Any = None


# --------------------------------------------------------------- identity ---

def _identity_fields(item_def: dict) -> tuple[str, ...] | None:
    """The record-identity field names for one $defs item schema, or None if
    it has no identity key at all (position fallback)."""
    required = set(item_def.get("required") or [])
    properties = dict(item_def.get("properties") or {})
    if not required and "oneOf" in item_def:
        # A discriminated union (blocks, keyed by "type"): every branch
        # requires its own fields, but "name" is common to all of them.
        branches = item_def["oneOf"]
        required = set.intersection(*(set(b.get("required", [])) for b in branches)) if branches else set()
        for branch in branches:
            properties.update(branch.get("properties") or {})
    if "name" in required:
        return ("name",)
    if "id" in required:
        return ("id",)
    if {"source", "target"} <= required:
        fields = ["source", "target"]
        fields.extend(p for p in ("source_port", "target_port") if p in properties)
        return tuple(fields)
    if {"from", "to"} <= required:
        return ("from", "to")
    return None


def _item_def(items_schema: dict | None, defs: dict) -> dict | None:
    """The item schema an array property's "items" describes -- a $ref into
    $defs, or (network.nodes/segments) an inline object schema."""
    if not items_schema:
        return None
    ref = items_schema.get("$ref")
    return defs[ref.rsplit("/", 1)[-1]] if ref else items_schema


@lru_cache(maxsize=1)
def _section_identities() -> dict:
    """{top-level section name: identity fields or None}, one entry per
    array-valued top-level property of the model schema."""
    schema = build_json_schema()
    defs = schema["$defs"]
    out = {}
    for key, spec in schema["properties"].items():
        if spec.get("type") != "array":
            continue
        item_def = _item_def(spec.get("items"), defs)
        if item_def is not None:
            out[key] = _identity_fields(item_def)
    return out


@lru_cache(maxsize=1)
def _dict_record_sections() -> set:
    """Top-level sections shaped as {name: record} rather than a list of
    records -- currently just "submodels", discovered the same way: any
    property that is an object keyed by an arbitrary name with a $ref value
    schema. The dict key IS the identity, so a whole submodel appearing or
    disappearing is one added/removed record, not a flood of field-level
    changes against {}."""
    out = set()
    for key, spec in build_json_schema()["properties"].items():
        additional = spec.get("additionalProperties") if spec.get("type") == "object" else None
        if isinstance(additional, dict) and "$ref" in additional:
            out.add(key)
    return out


@lru_cache(maxsize=1)
def _network_identities() -> dict:
    """{"nodes"|"segments": identity fields}, one level under "network"."""
    defs = build_json_schema()["$defs"]
    net_def = defs["network"]
    out = {}
    for key in ("nodes", "segments"):
        prop = (net_def.get("properties") or {}).get(key) or {}
        item_def = _item_def(prop.get("items"), defs)
        if item_def is not None:
            out[key] = _identity_fields(item_def)
    return out


def _record_key(record: dict, fields: tuple[str, ...]) -> tuple:
    return tuple(record.get(f) for f in fields)


def _record_label(key: tuple, fields: tuple[str, ...]) -> str:
    if fields[:2] == ("source", "target"):
        source, target, *ports = key
        label = f"{source}->{target}"
        if any(ports):
            label += f"[{ports[0] or ''}->{ports[1] if len(ports) > 1 and ports[1] else ''}]"
        return label
    if fields == ("from", "to"):
        return f"{key[0]}->{key[1]}"
    return ".".join(str(k) for k in key)


def _group_by_identity(records: list, fields: tuple[str, ...]) -> dict:
    groups: dict = {}
    for record in records:
        groups.setdefault(_record_key(record, fields), []).append(record)
    return groups


# ------------------------------------------------------------------ diff ----

def _leaf_change(path: str, old, new) -> Change:
    return Change("changed", path, old, new)


def _objects(o, n) -> bool:
    """Diff key by key only when both sides are objects, or one is absent; a
    dict turned into a list (or a number) is one leaf change (DEVLOG #218)."""
    return (isinstance(o, dict) or o is None) and (isinstance(n, dict) or n is None)


def _record_lists(o, n) -> bool:
    return all(isinstance(v, list) and all(isinstance(x, dict) for x in v) for v in (o, n))


def _diff_dict(path: str, old: dict, new: dict, nested=None) -> list:
    changes = []
    for key in sorted(set(old) | set(new)):
        child_path = f"{path}.{key}" if path else key
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        if key in _CANVAS_FIELDS:
            changes.append(Change("moved", child_path, o, n))
        elif nested and key in nested and _record_lists(o, n):
            changes.extend(_diff_records(child_path, o, n, nested[key]))
        elif _objects(o, n):
            changes.extend(_diff_dict(child_path, o or {}, n or {}))
        else:
            changes.append(_leaf_change(child_path, o, n))
    return changes


def _diff_records(section: str, old_list: list, new_list: list, fields) -> list:
    changes = []
    if fields is None:
        # No identity key at all: fall back to position (documented ceiling).
        for index, (o, n) in enumerate(zip_longest(old_list, new_list)):
            record_path = f"{section}.{index}"
            if o is None:
                changes.append(Change("added", record_path, None, n))
            elif n is None:
                changes.append(Change("removed", record_path, o, None))
            elif o != n:
                changes.extend(_diff_dict(record_path, o, n))
        return changes

    groups_old = _group_by_identity(old_list, fields)
    groups_new = _group_by_identity(new_list, fields)
    nested = _NESTED_RECORDS.get(section)
    for key in sorted(set(groups_old) | set(groups_new), key=str):
        label = _record_label(key, fields)
        for o, n in zip_longest(groups_old.get(key, []), groups_new.get(key, [])):
            record_path = f"{section}.{label}"
            if o is None:
                changes.append(Change("added", record_path, None, n))
            elif n is None:
                changes.append(Change("removed", record_path, o, None))
            elif o != n:
                changes.extend(_diff_dict(record_path, o, n, nested))
    return changes


def _diff_dict_of_records(section: str, old: dict, new: dict) -> list:
    """A {name: record} section (see _dict_record_sections): the dict key is
    the identity, so a whole record appearing/disappearing is one added/
    removed change, matching the list-record sections' rule."""
    changes = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        record_path = f"{section}.{key}"
        if o is None:
            changes.append(Change("added", record_path, None, n))
        elif n is None:
            changes.append(Change("removed", record_path, o, None))
        else:
            changes.extend(_diff_dict(record_path, o, n))
    return changes


def _diff_network(old, new) -> list:
    old, new = old or {}, new or {}
    identities = _network_identities()
    changes = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        if key in identities:
            changes.extend(_diff_records(f"network.{key}", o or [], n or [], identities[key]))
        elif _objects(o, n):
            changes.extend(_diff_dict(f"network.{key}", o or {}, n or {}))
        else:
            changes.append(_leaf_change(f"network.{key}", o, n))
    return changes


def diff(a: dict, b: dict, *, include_moved: bool = True) -> list:
    """Semantic differences between two model layouts (dicts), matched by
    record identity rather than list position. Both are migrated to the
    current schema first, so an older file diffs cleanly against a newer one.
    """
    la = schema_mod.migrate(copy.deepcopy(a))
    lb = schema_mod.migrate(copy.deepcopy(b))
    identities = _section_identities()

    changes = []
    for key in sorted(set(la) | set(lb)):
        old, new = la.get(key), lb.get(key)
        if old == new:
            continue
        if key == "network":
            changes.extend(_diff_network(old, new))
        elif key in identities:
            changes.extend(_diff_records(key, old or [], new or [], identities[key]))
        elif key in _dict_record_sections():
            changes.extend(_diff_dict_of_records(key, old or {}, new or {}))
        elif _objects(old, new):
            changes.extend(_diff_dict(key, old or {}, new or {}))
        else:
            changes.append(_leaf_change(key, old, new))

    if not include_moved:
        changes = [c for c in changes if c.kind != "moved"]
    return sorted(changes, key=lambda c: (c.path, c.kind))


# ----------------------------------------------------------------- merge ----

# "Absent on this side": distinct from a stored None (`time_limit: null`).
_MISSING = object()


@dataclass(frozen=True)
class Conflict:
    """One path the merge could not decide. `base`/`ours`/`theirs` are the
    three values there, None where that side has no value (deleted or never
    added). `reason`: "changed" | "delete-modify" | "added" | "validation";
    `detail` carries the validation message for the last one."""
    path: str
    base: Any = None
    ours: Any = None
    theirs: Any = None
    reason: str = "changed"
    detail: str = ""


def _conflict(path, b, o, t, reason, detail="") -> Conflict:
    def value(v):
        return None if v is _MISSING else v
    return Conflict(path, value(b), value(o), value(t), reason, detail)


def _only_moved(a, b) -> bool:
    """True when two versions of a record differ in canvas fields alone."""
    return (isinstance(a, dict) and isinstance(b, dict)
            and all(c.kind == "moved" for c in _diff_dict("", a, b)))


def _ordered_keys(o, t) -> list:
    keys = list(o) if isinstance(o, dict) else []
    if isinstance(t, dict):
        keys.extend(k for k in t if k not in keys)
    return keys


def _merge_value(path, b, o, t, conflicts, canvas=False):
    if o == t:
        return o
    if b == o:
        return t
    if b == t:
        return o
    if canvas:
        return o
    if all(isinstance(v, dict) for v in (b, o, t)):
        return _merge_mapping(path, b, o, t, conflicts)
    reason = "added" if b is _MISSING else "delete-modify" if _MISSING in (o, t) else "changed"
    conflicts.append(_conflict(path, b, o, t, reason))
    return o


def _merge_record(path, b, o, t, conflicts, lists=None):
    """One whole record (a block, a link, a submodel definition, ...); `lists`
    names its nested record lists (_NESTED_RECORDS)."""
    if o == t:
        return o
    if b == o:
        return t
    if b == t:
        return o
    if o is _MISSING or t is _MISSING:
        if _only_moved(b, t if o is _MISSING else o):
            return _MISSING     # a move never conflicts, not even with a delete
        conflicts.append(_conflict(path, b, o, t, "delete-modify"))
        return o
    if b is _MISSING:
        if _only_moved(o, t):
            return o
        conflicts.append(_conflict(path, b, o, t, "added"))
        return o
    if not (isinstance(b, dict) and isinstance(o, dict) and isinstance(t, dict)):
        return _merge_value(path, b, o, t, conflicts)
    return _merge_mapping(path, b, o, t, conflicts, lists=lists)


def _keyed(records: list, fields) -> dict:
    """{(identity, n): record}, list order kept; n pairs up records that share
    one identity by their order, the same rule `diff` uses."""
    out, seen = {}, {}
    for record in records:
        key = _record_key(record, fields) if isinstance(record, dict) else (repr(record),)
        n = seen[key] = seen.get(key, -1) + 1
        out[(key, n)] = record
    return out


def _merge_records(section, bl, ol, tl, fields, conflicts) -> list:
    if fields is None:
        # No identity key: by position (documented ceiling, as in diff).
        merged = (_merge_record(f"{section}.{i}", b, o, t, conflicts)
                  for i, (b, o, t) in enumerate(zip_longest(bl, ol, tl, fillvalue=_MISSING)))
        return [v for v in merged if v is not _MISSING]
    kb, ko, kt = _keyed(bl, fields), _keyed(ol, fields), _keyed(tl, fields)
    out = []
    for key in list(ko) + [k for k in kt if k not in ko]:
        v = _merge_record(f"{section}.{_record_label(key[0], fields)}",
                          kb.get(key, _MISSING), ko.get(key, _MISSING), kt.get(key, _MISSING),
                          conflicts, _NESTED_RECORDS.get(section))
        if v is not _MISSING:
            out.append(v)
    return out


def _merge_mapping(path, b, o, t, conflicts, lists=None, record_dicts=()):
    """Key-by-key merge of three dicts; `lists` names record-list children
    ({key: identity fields}), `record_dicts` the {name: record} children."""
    b = b if isinstance(b, dict) else {}
    out = {}
    for key in _ordered_keys(o, t):
        bv, ov, tv = b.get(key, _MISSING), o.get(key, _MISSING), t.get(key, _MISSING)
        child = f"{path}.{key}" if path else key
        # Structured children need the same shape on both sides; anything
        # else (a section added/removed wholesale, `network: null`) is a value.
        if lists and key in lists:
            shape = list
        elif key in record_dicts or (key == "network" and not path):
            shape = dict
        else:
            shape = None
        if shape and isinstance(ov, shape) and isinstance(tv, shape):
            bs = bv if isinstance(bv, shape) else shape()
            if shape is list:
                value = _merge_records(child, bs, ov, tv, lists[key], conflicts)
            elif key in record_dicts:
                value = {}
                for name in _ordered_keys(ov, tv):
                    rec = _merge_record(f"{child}.{name}", bs.get(name, _MISSING),
                                        ov.get(name, _MISSING), tv.get(name, _MISSING), conflicts)
                    if rec is not _MISSING:
                        value[name] = rec
            else:
                value = _merge_mapping(child, bs, ov, tv, conflicts, lists=_network_identities())
        else:
            value = _merge_value(child, bv, ov, tv, conflicts, canvas=key in _CANVAS_FIELDS)
        if value is not _MISSING:
            out[key] = value
    return out


def _record_at(layout: dict, pointer: str):
    """(identity path, section, identity key) of the record a Problem's JSON
    pointer lands in (`/links/3/source` -> links.a->b), or None when the
    pointer does not name a record of `layout`."""
    parts = [p for p in pointer.split("/") if p]
    if len(parts) >= 2 and parts[0] in _dict_record_sections():
        if parts[1] in (layout.get(parts[0]) or {}):
            return f"{parts[0]}.{parts[1]}", parts[0], parts[1]
        return None
    lists, container, prefix = _section_identities(), layout, ""
    if parts[:1] == ["network"]:
        lists, container, prefix, parts = _network_identities(), layout.get("network") or {}, "network.", parts[1:]
    if len(parts) < 2 or not parts[1].isdigit() or lists.get(parts[0]) is None:
        return None
    records = container.get(parts[0]) or []
    index = int(parts[1])
    if index >= len(records):
        return None
    fields = lists[parts[0]]
    key = _record_key(records[index], fields)
    return f"{prefix}{parts[0]}.{_record_label(key, fields)}", prefix + parts[0], key


def _find_record(layout: dict, section: str, key):
    if section in _dict_record_sections():
        return (layout.get(section) or {}).get(key, _MISSING)
    container = layout
    if section.startswith("network."):
        container, section = layout.get("network") or {}, section.split(".", 1)[1]
        fields = _network_identities()[section]
    else:
        fields = _section_identities()[section]
    for record in container.get(section) or []:
        if _record_key(record, fields) == key:
            return record
    return _MISSING


def _replace_record(layout: dict, section: str, key, new) -> None:
    """Put `new` (or nothing, for _MISSING) where record `key` sits."""
    if section in _dict_record_sections():
        if new is _MISSING:
            layout[section].pop(key, None)
        else:
            layout[section][key] = new
        return
    container = layout
    if section.startswith("network."):
        container, section = layout["network"], section.split(".", 1)[1]
        fields = _network_identities()[section]
    else:
        fields = _section_identities()[section]
    records = container[section]
    for i, record in enumerate(records):
        if _record_key(record, fields) == key:
            if new is _MISSING:
                del records[i]
            else:
                records[i] = new
            return


def _fatal(layout: dict) -> dict:
    return {(p.code, p.message): p for p in schema_mod.validate_model_structured(layout)
            if p.severity != "warning"}


def _validation_conflicts(base, ours, theirs, merged, conflicts) -> None:
    """Each fatal problem of `merged` that neither ours nor theirs has alone
    is a Conflict (reason "validation") on the record it points at, and that
    record goes back to ours' version (dropped if ours has none) -- "merged
    keeps ours". Problems match by (code, message), which name records, not
    list indices, so they line up across the three layouts."""
    before = set(_fatal(ours)) | set(_fatal(theirs))
    created = [p for k, p in _fatal(merged).items() if k not in before]
    by_path: dict = {}
    flattened = submodels.has_submodels(merged)
    for p in created:
        # ponytail: pointers into a flattened/expanded layout (submodels, SD
        # delays) do not index `merged`; those report the pointer, no revert.
        # An E_SUBMODEL problem is raised while flattening the UNflattened
        # layout, so its pointer does index `merged` (`/links/3`, DEVLOG #216).
        at = None if flattened and p.code != "E_SUBMODEL" else _record_at(merged, p.path)
        if at is None:
            path = ".".join(s for s in p.path.split("/") if s) or "model"
            by_path.setdefault(path, _conflict(path, _MISSING, _MISSING, _MISSING,
                                               "validation", p.message))
            continue
        path, section, key = at
        if path in by_path:
            prev = by_path[path]
            by_path[path] = Conflict(prev.path, prev.base, prev.ours, prev.theirs,
                                     "validation", f"{prev.detail}; {p.message}")
            continue
        b, o, t = (_find_record(x, section, key) for x in (base, ours, theirs))
        by_path[path] = _conflict(path, b, o, t, "validation", p.message)
        _replace_record(merged, section, key, o if o is _MISSING else copy.deepcopy(o))
    # ponytail: one pass; a revert that itself creates a new problem is not re-checked.
    conflicts.extend(by_path.values())


def merge(base: dict, ours: dict, theirs: dict) -> tuple[dict, list]:
    """Three-way merge of model layouts by record identity (module docstring
    has the rules). Returns (merged layout, [Conflict]); on a conflict the
    merged layout holds ours' value there, so it is always a usable file."""
    lb, lo, lt = (schema_mod.migrate(copy.deepcopy(x)) for x in (base, ours, theirs))
    conflicts: list = []
    merged = _merge_mapping("", lb, lo, lt, conflicts, lists=_section_identities(),
                            record_dicts=_dict_record_sections())
    # Unshare from lo/lt: the validation reverts edit `merged` in place and
    # still look records up in ours/theirs afterwards.
    merged = copy.deepcopy(merged)
    _validation_conflicts(lb, lo, lt, merged, conflicts)
    return merged, conflicts


# --------------------------------------------------------------- textconv ---

def _record_lines(section: str, records: list, fields) -> list:
    if fields is None:
        return [f"{section}.{i} = {json.dumps(r, sort_keys=True)}" for i, r in enumerate(records)]
    ordered = sorted(records, key=lambda r: tuple(str(v) for v in _record_key(r, fields)))
    return [f"{section}.{_record_label(_record_key(r, fields), fields)} = "
            f"{json.dumps(r, sort_keys=True)}" for r in ordered]


def textconv(layout_doc: dict) -> str:
    """A stable, line-per-record rendering of one model file: identical for
    two layouts that differ only in list order, so `git diff` (wired through
    `.gitattributes`) shows only real content changes. Sorted by section then
    by identity, never by the file's own list order."""
    migrated = schema_mod.migrate(copy.deepcopy(layout_doc))
    identities = _section_identities()
    lines = []
    for key in sorted(migrated):
        value = migrated[key]
        if key == "network" and isinstance(value, dict):
            net_identities = _network_identities()
            for subkey in sorted(value):
                subval = value[subkey]
                if subkey in net_identities and isinstance(subval, list):
                    lines.extend(_record_lines(f"network.{subkey}", subval, net_identities[subkey]))
                else:
                    lines.append(f"network.{subkey} = {json.dumps(subval, sort_keys=True)}")
        elif key in identities and isinstance(value, list):
            lines.extend(_record_lines(key, value, identities[key]))
        elif key in _dict_record_sections() and isinstance(value, dict):
            for name in sorted(value):
                lines.append(f"{key}.{name} = {json.dumps(value[name], sort_keys=True)}")
        else:
            lines.append(f"{key} = {json.dumps(value, sort_keys=True)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- CLI --

def change_line(c: Change) -> str:
    if c.kind in ("added", "removed"):
        return f"{c.kind} {c.path}"
    return f"{c.kind} {c.path}: {c.old!r} -> {c.new!r}"


def _change_dict(c: Change) -> dict:
    return {"kind": c.kind, "path": c.path, "old": c.old, "new": c.new}


def main(argv=None) -> int:
    """`naga diff a.json b.json [--json] [--all]` and `naga diff
    --textconv file.json` (a git textconv driver, see site_docs). Exit 0 when
    there is no visible change, 1 when there is, 2 on error."""
    parser = argparse.ArgumentParser(
        prog="naga diff",
        description="Semantic diff between two model files, matched by record "
                    "identity rather than list position.")
    parser.add_argument("files", nargs="+", help="two model files, or one with --textconv")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the changes as a JSON array")
    parser.add_argument("--all", action="store_true", dest="show_all",
                        help="also report canvas-only 'moved' changes (hidden by default)")
    parser.add_argument("--textconv", action="store_true",
                        help="print one file's canonical text form (a git textconv driver)")
    args = parser.parse_args(argv)

    if args.textconv:
        if len(args.files) != 1:
            parser.error("--textconv takes exactly one file")
        try:
            raw = json.loads(Path(args.files[0]).read_text(encoding="utf-8"))
            print(textconv(raw), end="")
        except (OSError, json.JSONDecodeError, schema_mod.ModelValidationError) as exc:
            print(f"naga diff: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:      # a bug must not read as "differs" (exit 1)
            print(f"naga diff: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        return 0

    if len(args.files) != 2:
        parser.error("diff takes exactly two model files")
    try:
        a = json.loads(Path(args.files[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.files[1]).read_text(encoding="utf-8"))
        changes = diff(a, b, include_moved=args.show_all)
    except (OSError, json.JSONDecodeError, schema_mod.ModelValidationError) as exc:
        print(f"naga diff: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:          # a bug must not read as "differs" (exit 1)
        print(f"naga diff: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps([_change_dict(c) for c in changes], indent=2))
    else:
        for c in changes:
            print(change_line(c))
        if not changes:
            print("no changes")
    return 1 if changes else 0


def _conflict_line(c: Conflict) -> str:
    if c.reason == "validation":
        return f"conflict {c.path} (validation): {c.detail}"
    return f"conflict {c.path} ({c.reason}): ours {c.ours!r}, theirs {c.theirs!r}"


def merge_main(argv=None) -> int:
    """`naga merge BASE OURS THEIRS [-o OUT] [--json]`, a git merge driver
    (`naga merge %O %A %B`): writes the merged model to OUT (default: over
    OURS, git's %A), exit 0 clean, 1 on conflicts, 2 on error. OUT is written
    in both of the first two cases; on a conflict it holds ours' value."""
    parser = argparse.ArgumentParser(
        prog="naga merge",
        description="Three-way merge of model files by record identity; usable as a git "
                    "merge driver (naga merge %%O %%A %%B).")
    parser.add_argument("base", help="the common ancestor (git %%O)")
    parser.add_argument("ours", help="our version (git %%A); overwritten unless -o is given")
    parser.add_argument("theirs", help="their version (git %%B)")
    parser.add_argument("-o", "--output", help="write the merged model here instead of over OURS")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="also print the conflicts as a JSON array on stdout")
    args = parser.parse_args(argv)
    try:
        docs = [json.loads(Path(p).read_text(encoding="utf-8"))
                for p in (args.base, args.ours, args.theirs)]
        merged, conflicts = merge(*docs)
        Path(args.output or args.ours).write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError, schema_mod.ModelValidationError) as exc:
        print(f"naga merge: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:          # git reads exit 1 as "conflicts", not "crashed"
        print(f"naga merge: internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for c in conflicts:
        print(_conflict_line(c), file=sys.stderr)
    if conflicts:
        print(f"naga merge: {len(conflicts)} conflict(s); the merged file keeps ours there",
              file=sys.stderr)
    if args.as_json:
        print(json.dumps([asdict(c) for c in conflicts], indent=2, default=str))
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
