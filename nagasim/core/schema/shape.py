"""The model file's shape against its published JSON Schema (D1220): one walk that every
surface reads (`validate`, the document, the agent tools and the edit guard), through
`validate_model_structured` / `ModelDocument.problems`.

A field holding a value of a JSON type its schema does not allow (`"capacity": "one"`) is
E_FIELD_TYPE (#1766, D1221): the run would die on it with a library message.

A key the schema does not have, at any level it closes (the top level, `simulation`, a
section's records, a block's or link's own keys, and the objects one level inside them), is
a W_UNKNOWN_KEY warning with its nearest name, never an error: the document keeps it
verbatim through load and save (D1193 at the top level, D1220 below), so a file a newer
build wrote loses nothing. Block `params` are left to the block validator (E_PARAM_UNKNOWN).
"""
from __future__ import annotations

from nagasim.core.problems import Problem, problem
from nagasim.core.suggest import did_you_mean

__all__ = ["shape_problems", "type_problems_unsaid", "fields_at"]

_DEPTH = 2      # the top level is 0: records (1) and the objects inside them (2)
_JSON_TYPES = {"string": str, "array": list, "object": dict, "boolean": bool}


# The schema, built once per registry state: built on every call it cost about 4% of a
# 30-replication run of a small model (one build per unprepared replication, two per
# `ModelDocument.problems`). The rest of the schema comes from module constants.
_CACHE: tuple[tuple, dict] | None = None


def _registered() -> tuple:
    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.core.distributions import DISTRIBUTION_REGISTRY
    return tuple(BLOCK_REGISTRY.items()), tuple(DISTRIBUTION_REGISTRY.items())


def _same(a: tuple, b: tuple) -> bool:
    """The same specs under the same names, by identity: a plugin's block or distribution, or one
    replaced, rebuilds the schema. The cache holds the specs, so an id is never reused meanwhile."""
    return all(len(x) == len(y) and all(n == m and s is r for (n, s), (m, r) in zip(x, y))
               for x, y in zip(a, b))


def _json_schema() -> dict:
    """The model file's JSON Schema, shared: this module only reads it, and gives none of it out."""
    global _CACHE
    # The schema is generated from the registries by a top-level module that imports
    # core.schema, so it is imported here, when a model is checked (as quantities.py does
    # with experiments).
    from nagasim.modelschema import build_json_schema
    key = _registered()
    cached = _CACHE
    if cached is None or not _same(cached[0], key):
        cached = _CACHE = (key, build_json_schema())
    return cached[1]


def _target(full: dict, node: dict) -> dict | None:
    ref = node.get("$ref") or (node.get("items") or {}).get("$ref")
    if ref:
        return full["$defs"].get(ref.rsplit("/", 1)[-1])
    return node.get("items") if node.get("type") == "array" else node


def _variant(node: dict | None, value) -> dict | None:
    """A block's schema is one variant per type: the one *value*'s `type` names."""
    if node and "oneOf" in node and isinstance(value, dict):
        kind = str(value.get("type", "")).upper()
        return next((v for v in node["oneOf"] if v.get("title") == kind), None)
    return node


def fields_at(section: str, record, keys) -> list[str]:
    """The field names the schema allows at *keys* inside a record of *section* (a block by
    its type; `simulation` is the record itself), or [] when the schema states none there:
    what a scenario override may set although the base leaves it out (D1222)."""
    full = _json_schema()
    node = full["properties"].get(section)
    node = _target(full, node) if node else None
    for key in keys:
        node = _variant(node, record)
        if not node or key not in node.get("properties", {}):
            return []
        node = _target(full, node["properties"][key])
        record = record.get(key) if isinstance(record, dict) else None
    return list((_variant(node, record) or {}).get("properties", ()))


def _label(value: dict, at: str) -> str:
    name = value.get("name")
    section = at.split("/")[1] if at.count("/") >= 2 else at.strip("/")
    return f"{section} '{name}'" if isinstance(name, str) and name else at.strip("/") or "model"


def _type_error(value, node: dict) -> str | None:
    """What is wrong with *value*'s JSON type for *node*, or None. Only a node that states
    its `type` is judged; None is left to the field's own check (a cleared field)."""
    allowed = node.get("type")
    if allowed is None or value is None or isinstance(value, dict) and "$arg" in value:
        return None         # no stated type, a cleared field, or a submodel argument
    allowed = [allowed] if isinstance(allowed, str) else list(allowed)
    if isinstance(value, bool):
        ok = "boolean" in allowed
    elif isinstance(value, int):
        ok = bool({"integer", "number"} & set(allowed))
    elif isinstance(value, float):
        ok = "number" in allowed or ("integer" in allowed and value.is_integer())
    else:
        ok = any(isinstance(value, _JSON_TYPES[t]) for t in allowed if t in _JSON_TYPES)
    return None if ok else " or ".join("an expression" if t == "string" else
                                       {"integer": "a whole number", "number": "a number",
                                        "array": "a list", "object": "an object",
                                        "boolean": "true or false", "null": "null"}.get(t, t)
                                       for t in allowed)


def _record(path: str) -> str:
    """'/resources/0/capacity' -> '/resources/0'; '/simulation/time_limit' -> '/simulation'."""
    parts = path.split("/")
    return "/".join(parts[:3] if len(parts) > 3 and parts[2].isdigit() else parts[:2])


def type_problems_unsaid(found: list[Problem], said: list[Problem]) -> list[Problem]:
    """*found* without an E_FIELD_TYPE on a record a section's own validator has already
    refused, so one bad record is not said twice (as `quantities.check_bounds` does)."""
    refused = {_record(p.path) for p in said if p.severity == "error" and p.path}
    return [p for p in found if p.code != "E_FIELD_TYPE" or _record(p.path) not in refused]


def shape_problems(layout: dict) -> list[Problem]:
    """W_UNKNOWN_KEY for every key *layout* holds that the JSON Schema does not, at every
    level the schema closes, with the nearest valid name in the hint; E_FIELD_TYPE for a
    value of a JSON type its field does not take."""
    if not isinstance(layout, dict):
        return []
    full = _json_schema()
    found: list[Problem] = []

    def check(value, node, at: str, depth: int, block: str | None) -> None:
        node = _variant(node, value)
        if not node or not isinstance(value, dict) or "properties" not in node:
            return
        known = node["properties"]
        for key, inner in value.items():
            if key in known:
                wanted = _type_error(inner, known[key]) if depth else None
                if wanted:
                    found.append(problem(
                        "E_FIELD_TYPE", f"{_label(value, at)}: {key} must be {wanted}; got "
                        f"{inner!r}"[:300], f"{at}/{key}", block=block,
                        hint=f"write {key} as {wanted}"))
                    continue
                if depth < _DEPTH and key != "params":
                    sub = _target(full, known[key])
                    many = isinstance(inner, list) and known[key].get("type") == "array"
                    for index, item in enumerate(inner if many else [inner]):
                        where = f"{at}/{key}" + (f"/{index}" if many else "")
                        named = item.get("name") if at == "" and key == "blocks" \
                            and isinstance(item, dict) else block
                        check(item, sub, where, depth + 1, named)
                continue
            if node.get("additionalProperties") is not False:
                continue
            said = did_you_mean(key, known)
            if depth == 0:      # D1193's wording, which the top level has always had
                found.append(problem(
                    "W_UNKNOWN_KEY", f"unknown top-level key '{key}': this build does not use it "
                    "and keeps it as it is", f"/{key}",
                    hint=said or "a section of a newer version, or a misspelt one"))
                continue
            found.append(problem(
                "W_UNKNOWN_KEY", f"{_label(value, at)}: unknown key '{key}': this build does "
                "not use it and keeps it as it is", f"{at}/{key}", block=block,
                hint=(f"{said} " if said else "") + "keys here: " + ", ".join(known)))

    check(layout, full, "", 0, None)
    return found
