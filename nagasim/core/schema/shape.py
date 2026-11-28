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

import json
import re

from nagasim.core.problems import Problem, problem
from nagasim.core.suggest import did_you_mean

__all__ = ["shape_problems", "asset_3d_problems", "type_problems_unsaid", "fields_at",
           "QUEUE_DISPLAYS", "DRAW_GUARD", "DRAW_GUARD_MAX", "VIEW_SETTINGS", "view_settings",
           "view_setting_faults", "view_setting_problems", "checked_draw_guard",
           "applied_draw_guard", "LAYER_KINDS", "checked_layers"]

_DEPTH = 2      # the top level is 0: records (1) and the objects inside them (2)
_JSON_TYPES = {"string": str, "array": list, "object": dict, "boolean": bool}
# The 3D display settings in `view` are editor-only: a bad value there is read as its default
# and said as the W_VIEW_SETTING warning (`view_setting_problems`), never as an E_FIELD_TYPE
# error, because an editor-only key must never block a run (re-review N2). The paths are the
# ones `view_setting_faults` names, plus a block's `layer` (D1513) and its queue_row `count_at`
# and `cell` (D1507, D1517). A delimiter after the name, so "/view/layers_x" is not one (K2).
_VIEW_SETTINGS = re.compile(r"/view/(queue_display|draw_guard|layers)(/.*)?"
                            r"|(/submodels/[^/]+)?/blocks/\d+/(layer|queue_row/(count_at|cell)(/.*)?)"
                            r"|/simulation/queue_limit(/.*)?")   # D1529: W_QUEUE_LIMIT_SETTING

# The model display settings in `view` (D1504, D1506), read by the 3D view and `set_view`. Unlike
# the rest of `view` (set on save, no undo step) they are edits: `set_view_settings`. They live
# here, beside the shape check, so `validate_model`, `naga validate` and the document all say a
# bad one (final review M1); `nagasim.core.document` re-exports them.
QUEUE_DISPLAYS = ("compact", "tall")
# D1526: limit_2d bounds the 2D canvas's sliding tokens; a 2D token is lighter than a 3D item.
DRAW_GUARD = {"on": True, "limit": 20000, "limit_2d": 50000}
VIEW_SETTINGS = ("queue_display", "draw_guard", "layers")
# D1512: the kinds of drawn object the 3D view shows or hides; a block's `layer` names a user
# layer (D1513). `view.layers` maps either to a bool, a missing name being shown (D1516).
LAYER_KINDS = ("equipment", "tiles", "names", "pads", "items", "floor", "building", "network")
DRAW_GUARD_MAX = 2_000_000_000     # the dialog's spin box holds a 32-bit int (checkpoint M6)
_LIMITS = ("limit", "limit_2d")


def view_setting_faults(view) -> list[tuple[str, str, str]]:
    """`[(path, setting, default as JSON)]` for each display setting of a `view` section that
    `view_settings` replaces by its default (a hand edit or `write_model` may leave any JSON
    there). An unknown key is not one: W_UNKNOWN_KEY names it, and nothing reads it."""
    return _read_view(view)[1]


def view_setting_problems(layout: dict) -> list[Problem]:
    """W_VIEW_SETTING for each fault `view_setting_faults` finds in *layout*'s `view`, and for
    a block whose `layer` is not text (D1513), which then shows on no user layer."""
    view = layout.get("view") if isinstance(layout, dict) else None
    out = [problem("W_VIEW_SETTING", f"view: {setting} is not valid; the view uses {default}",
                   path, fields={"case": "default", "setting": setting, "default": default})
           for path, setting, default in view_setting_faults(view)]
    if not isinstance(layout, dict):
        return out
    scopes = [("", layout.get("blocks"))] + [
        (f"/submodels/{name}", d.get("blocks")) for name, d in
        (layout.get("submodels") or {}).items() if isinstance(d, dict)]
    for at, blocks in scopes:
        for i, block in enumerate(blocks if isinstance(blocks, list) else ()):
            layer = block.get("layer") if isinstance(block, dict) else None
            if layer is not None and not isinstance(layer, str):
                out.append(problem(
                    "W_VIEW_SETTING", f"{block.get('name')}: layer is not text; the 3D view puts "
                    "the block on no layer", f"{at}/blocks/{i}/layer", block=block.get("name"),
                    hint='a layer name such as "Racks", or remove layer',
                    fields={"case": "layer", "block": block.get("name")}))
    return out


def view_settings(view: dict | None) -> dict:
    """`{"queue_display", "draw_guard": {"on", "limit", "limit_2d"}, "layers": {name: bool}}` of a
    `view` section, absent keys at their defaults, so a model saved before them opens compact with
    the guard on at 20,000 (3D) and 50,000 (2D) and every layer shown (a name missing from `layers` is shown).
    Total (checkpoint I3): a value `set_view_settings` would refuse is read as its default, each
    key on its own, so a bad file opens and draws; `view_setting_faults` names what was replaced."""
    return _read_view(view)[0]


def _read_view(view) -> tuple[dict, list[tuple[str, str, str]]]:
    view = view if isinstance(view, dict) else {}
    faults = []
    shown = view.get("queue_display", QUEUE_DISPLAYS[0])
    if not isinstance(shown, str) or shown not in QUEUE_DISPLAYS:
        faults.append(("/view/queue_display", "queue_display", json.dumps(QUEUE_DISPLAYS[0])))
        shown = QUEUE_DISPLAYS[0]
    guard = dict(DRAW_GUARD)
    raw = view.get("draw_guard", {})
    if not isinstance(raw, dict):
        faults.append(("/view/draw_guard", "draw_guard", json.dumps(DRAW_GUARD)))
        raw = {}
    for key in DRAW_GUARD:
        if key not in raw:
            continue
        try:
            guard[key] = checked_draw_guard({key: raw[key]})[key]
        except ValueError:
            faults.append((f"/view/draw_guard/{key}", f"draw_guard {key}", json.dumps(DRAW_GUARD[key])))
    raw = view.get("layers", {})
    if not isinstance(raw, dict):
        faults.append(("/view/layers", "layers", "{}"))
        raw = {}
    layers = {}
    for name, value in raw.items():
        if isinstance(value, bool):
            layers[name] = value
        else:
            faults.append((f"/view/layers/{name}", f"layers {name}", "true"))
    return {"queue_display": shown, "draw_guard": guard, "layers": layers}, faults


def checked_layers(layers) -> dict:
    """*layers* (`{name: bool}`, a kind of `LAYER_KINDS` or a user layer) or ValueError."""
    if not isinstance(layers, dict) or not all(isinstance(k, str) for k in layers):
        raise ValueError("layers must be an object of layer names")
    bad = [k for k, v in layers.items() if not isinstance(v, bool)]
    if bad:
        raise ValueError(f"layers {bad[0]} must be true or false")
    return dict(layers)


def applied_draw_guard(view, allowed: dict | None = None) -> tuple[dict, bool]:
    """(the draw guard the 3D view and the 2D canvas apply, whether the model's own waits for the
    user). D1519, D1527: permission is the caller's, never the file's, so a model file (or the
    set_view tool) may tighten the guard but not loosen it past the default or past *allowed*, the
    guard the user confirmed in the app; a looser one gives way to that ceiling until confirmed.
    Each limit is held on its own, so a file that raises one keeps the other it tightens."""
    guard = view_settings(view)["draw_guard"]
    ceiling = dict(DRAW_GUARD)
    if allowed is not None:
        ceiling = {"on": ceiling["on"] and allowed.get("on", True),
                   **{k: max(ceiling[k], allowed.get(k, ceiling[k])) for k in _LIMITS}}
    if ceiling["on"] and (not guard["on"] or any(guard[k] > ceiling[k] for k in _LIMITS)):
        return {"on": True, **{k: min(guard[k], ceiling[k]) for k in _LIMITS}}, True
    return guard, False


def checked_draw_guard(guard) -> dict:
    """*guard* (`{"on": bool, "limit": 1..DRAW_GUARD_MAX, "limit_2d": 1..DRAW_GUARD_MAX}`, any
    keys) or ValueError naming the fault. A whole float limit (20000.0, which the JSON Schema's
    integer allows, final review M3) is taken as its int."""
    if not isinstance(guard, dict) or set(guard) - set(DRAW_GUARD):
        raise ValueError(f"draw_guard takes only {', '.join(DRAW_GUARD)}")
    if "on" in guard and not isinstance(guard["on"], bool):
        raise ValueError("draw_guard on must be true or false")
    out = dict(guard)
    for key in _LIMITS:
        if key not in out:
            continue
        limit = out[key]
        if isinstance(limit, float) and limit.is_integer():
            limit = int(limit)
        # bool is an int in Python; a limit of True is a mistake, not 1.
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= DRAW_GUARD_MAX:
            raise ValueError(f"draw_guard {key} must be a whole number from 1 to {DRAW_GUARD_MAX:,}")
        out[key] = limit
    return out


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


# A block's `asset_3d`: "library/asset". Public: `ModelDocument.set_asset_3d` stores only this form.
ASSET_3D_RE = re.compile(r"[a-z0-9_]+/[a-z0-9_]+")


def asset_3d_problems(layout: dict) -> list[Problem]:
    """E_ASSET_3D for a block's `asset_3d` that is a string but not "library/asset" (lower-case
    letters, digits and underscores each side). A value of another JSON type is E_FIELD_TYPE's.
    The JSON Schema states the same pattern, but nothing else enforces a pattern."""
    out: list[Problem] = []
    scopes = [("/blocks", layout.get("blocks"))] + [
        (f"/submodels/{name}/blocks", d.get("blocks")) for name, d in
        (layout.get("submodels") or {}).items() if isinstance(d, dict)]
    for at, blocks in scopes:
        for i, block in enumerate(blocks if isinstance(blocks, list) else ()):
            value = block.get("asset_3d") if isinstance(block, dict) else None
            if isinstance(value, str) and not ASSET_3D_RE.fullmatch(value):
                out.append(problem(
                    "E_ASSET_3D", f"{block.get('name')}: asset_3d {value!r} is not \"library/asset\""[:300],
                    f"{at}/{i}/asset_3d", block=block.get("name"),
                    hint='write it like "automation/irb6700", or remove it for the default shape',
                    fields={"case": "form", "value": value[:80]}))
    return out


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
                said_as_warning = _VIEW_SETTINGS.fullmatch(f"{at}/{key}") is not None
                wanted = _type_error(inner, known[key]) if depth and not said_as_warning else None
                if wanted:
                    found.append(problem(
                        "E_FIELD_TYPE", f"{_label(value, at)}: {key} must be {wanted}; got "
                        f"{inner!r}"[:300], f"{at}/{key}", block=block,
                        hint=f"write {key} as {wanted}",
                        fields={"case": "type", "key": key, "wanted": wanted}))
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
                    hint=said or "a section of a newer version, or a misspelt one",
                    fields={"case": "top", "key": key}))
                continue
            found.append(problem(
                "W_UNKNOWN_KEY", f"{_label(value, at)}: unknown key '{key}': this build does "
                "not use it and keeps it as it is", f"{at}/{key}", block=block,
                hint=(f"{said} " if said else "") + "keys here: " + ", ".join(known),
                fields={"case": "inner", "key": key}))

    check(layout, full, "", 0, None)
    found += view_setting_problems(layout)
    sim = layout.get("simulation") if isinstance(layout, dict) else None
    if isinstance(sim, dict) and "queue_limit" in sim:      # lazy: see graph_model
        from nagasim.core.queuelimit import setting_problems
        found += setting_problems(layout)
    return found
