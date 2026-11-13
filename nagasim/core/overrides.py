"""The override path grammar (ROADMAP 1.6): how a scenario, a data binding or an agent tool
addresses one value of a model by name ("resources.Server.capacity"), and how it is applied.

In core so the one validation pass checks every scenario's paths (D1222) without importing
the pandas-side `experiments`, which re-exports the public names.
"""
from __future__ import annotations

import copy

from nagasim.core import sections
from nagasim.core.distributions import DISTRIBUTION_REGISTRY
from nagasim.core.problems import Problem, problem
from nagasim.core.schema.migrate import ModelValidationError
from nagasim.core.schema.shape import fields_at
from nagasim.core.suggest import did_you_mean

__all__ = ["apply_overrides", "value_at", "list_override_paths", "override_targets",
           "override_problems"]


# The sections an override path addresses by entry key (core/sections.py's
# `overridable` rows): by "name" (blocks and most record sections; a block's
# params only), by "id" for orders (PHASE4 A3, "orders.WO-1001.due"), and
# links by "<source>-><target>" -- plus "#1" for the second and further
# PARALLEL links between one pair (D77).
_NAMED_SECTIONS = tuple(s.name for s in sections.SECTIONS if s.overridable and s.key == "name")
_ID_SECTIONS = tuple(s.name for s in sections.SECTIONS if s.overridable and s.key == "id")
_SECTIONS = _NAMED_SECTIONS + _ID_SECTIONS + ("links",)
# ... plus "submodels" (ROADMAP 2.3 part B), a dict keyed by definition name:
# "submodels.<Definition>.blocks.<block>.params.<field>" edits the definition,
# so EVERY instance of it moves together. An instance's own bindings are
# reached the ordinary way: "blocks.<instance>.params.arguments.<parameter>".
_SUBMODEL_SECTION = "submodels"


# ---------------------------------------------------------------- override paths
def _entry_key(section: str, entry: dict) -> str:
    if section == "links":
        return f"{entry.get('source')}->{entry.get('target')}"
    if section == "network.segments":     # PHASE4 E1, same "a->b" shape
        return f"{entry.get('from')}->{entry.get('to')}"
    if section in _ID_SECTIONS:
        return entry.get("id", "")
    return entry.get("name", "")


def _entry_keys(section: str, entries: list) -> list[str]:
    """One override key per entry, in list order. Repeated link pairs get a
    "#<index>" suffix from the second one on (D77), so both parallel links are
    addressable and the first one's path is unchanged."""
    keys = [_entry_key(section, entry) for entry in entries]
    if section != "links":
        return keys
    seen: dict[str, int] = {}
    out = []
    for key in keys:
        index = seen.get(key, 0)
        seen[key] = index + 1
        out.append(key if index == 0 else f"{key}#{index}")
    return out


def _fail(path: str, reason: str, hint: str = "") -> None:
    message = f"override path '{path}': {reason}"
    raise ModelValidationError([message], structured=[problem(
        "E_SCENARIO_OVERRIDE", message,
        hint=hint or "a path names a record, then its field: resources.Server.capacity")])


def _split_entry(section: str, entries: list, rest: str, path: str) -> tuple[dict, list[str]]:
    """Split "<entry name>[.<field>...]" against the section's ACTUAL names, so a
    resource called "Line 2.a" still resolves -- splitting on every dot would
    not. Longest name first, so "Agents" never shadows "Agents.night"."""
    keyed = sorted(zip(_entry_keys(section, entries), entries), key=lambda pair: -len(pair[0]))
    for key, entry in keyed:
        if not key:
            continue
        if rest == key:
            return entry, []
        if rest.startswith(key + "."):
            return entry, rest[len(key) + 1:].split(".")
    word = rest.split(".")[0]
    _fail(path, f"no {section} entry named '{word}'",
          did_you_mean(word, [key for key, _entry in keyed if key]))


def _descend(container, fields: list[str], path: str, schema=None,
             write: bool = False) -> tuple[dict, str]:
    """Walk *fields* through nested dicts and return (parent, last key).

    Every key on the way must already exist: an override is a change to a value the model
    has, never a way to invent a parameter no block reads. The leaf may be absent when
    *write* and the schema has it there (D1222: a scenario gives a resource the capacity
    schedule its base leaves out); `schema(keys)` names the fields the schema allows below
    *keys*, which also gives a misspelt key its nearest name.
    """
    if not fields:
        _fail(path, "names no field")
    for depth, key in enumerate(fields):
        last = depth == len(fields) - 1
        if isinstance(container, dict) and key in container:
            if last:
                return container, key
            container = container[key]
            continue
        allowed = schema(fields[:depth]) if schema is not None else []
        if last and write and isinstance(container, dict) and key in allowed:
            return container, key
        options = list(container) if isinstance(container, dict) else []
        _fail(path, f"no such field '{key}'",
              did_you_mean(key, options + [a for a in allowed if a not in options]))
    return container, fields[-1]        # not reached: the loop returns or fails


def _schema_of(section: str, entry: dict):
    """`_descend`'s *schema* for a record of *section*: the JSON Schema's fields, and a
    `dist_params` object's from its distribution's registry entry."""
    def fields(keys: list[str]) -> list[str]:
        if keys and keys[-1] == "dist_params":
            holder = entry
            for key in keys[:-1]:
                holder = holder.get(key) if isinstance(holder, dict) else None
            spec = DISTRIBUTION_REGISTRY.get(holder.get("distribution")) \
                if isinstance(holder, dict) else None
            return [p.name for p in spec.params] if spec else []
        return fields_at(section, entry, keys)
    return fields


def _split_key(names, rest: str, path: str, what: str) -> tuple[str, str]:
    """Split "<name>.<remainder>" against actual *names* (longest first), so a
    definition or block whose name contains a dot still resolves."""
    for key in sorted(names, key=len, reverse=True):
        if rest == key:
            return key, ""
        if rest.startswith(key + "."):
            return key, rest[len(key) + 1:]
    _fail(path, f"no {what} named '{rest.split('.')[0]}'")


def _resolve_submodel(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"submodels.<Definition>.blocks.<block>.params.<field>"."""
    definitions = layout.get(_SUBMODEL_SECTION) or {}
    def_name, remainder = _split_key(definitions, rest, path, "submodel definition")
    definition = definitions[def_name]
    inner, _, block_rest = remainder.partition(".")
    if inner != "blocks":
        _fail(path, "only a submodel definition's blocks can be overridden "
                    "(submodels.<name>.blocks.<block>.params....)")
    entry, fields = _split_entry("blocks", definition.get("blocks", []), block_rest, path)
    if fields[:1] != ["params"]:
        _fail(path, "only a block's params can be overridden")
    return _descend(entry, fields, path)


def _resolve_network(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"network.nodes.<name>.<field>" / "network.segments.<a>-><b>.<field>"."""
    network = layout.get("network") or {}
    kind, _, remainder = rest.partition(".")
    if kind == "length_factor":
        return _descend(network, ["length_factor"], path)
    if kind not in ("nodes", "segments"):
        _fail(path, "only network.nodes, network.segments and network.length_factor "
                    "can be overridden")
    entry, fields = _split_entry(f"network.{kind}", network.get(kind) or [], remainder, path)
    return _descend(entry, fields, path)


# What a plant can report of a state (PHASE7 7.10, D236), and what each starts from.
_STATE_FIELDS = {"resources": {"down_until": 0.0, "busy": 0},
                 "entities": {"count": 0, "in_service": 0}}


def _resolve_state(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"initial_state.variables.<name>", ".levels.<name>", ".resources.<name>.down_until"
    or ".busy", ".entities.<block>.count" or ".in_service".

    The one place a path may address a value the model does not hold yet: a state is what
    a plant reports, and a model file usually stores none. The name is not checked here;
    the section's own validation refuses an unknown one, with its code."""
    state = layout.setdefault("initial_state", {})
    kind, _, remainder = rest.partition(".")
    if kind in ("variables", "levels") and remainder:
        declared = {e.get("name"): e.get("value" if kind == "variables" else "initial", 0)
                    for e in layout.get(kind, []) or []}
        container = state.setdefault(kind, {})
        container.setdefault(remainder, declared.get(remainder, 0))
        return container, remainder
    name, _, leaf = remainder.rpartition(".")
    if kind not in _STATE_FIELDS or not name or leaf not in _STATE_FIELDS[kind]:
        _fail(path, "of a state only variables.<name>, levels.<name>, "
                    "resources.<name>.down_until or .busy and entities.<block>.count or "
                    ".in_service can be written")
    if kind == "resources":
        entry = state.setdefault("resources", {}).setdefault(name, {})
    else:
        entries = state.setdefault("entities", [])
        entry = next((e for e in entries if e.get("block") == name), None)
        if entry is None:
            entry = {"block": name, "count": 0}
            entries.append(entry)
    entry.setdefault(leaf, _STATE_FIELDS[kind][leaf])
    return entry, leaf


def _resolve(layout: dict, path: str, write: bool = False) -> tuple[dict, str]:
    """(container, key) the override path addresses inside *layout*; with *write*, the key
    may be a schema field the record leaves out (D1222)."""
    if not path:
        _fail("", "empty path")
    # The one whole-section path (PHASE4 A3): an MES binding replaces the order
    # book outright, which is not a change to a value the model already has and
    # so has no leaf to address. Deliberately only "orders" -- every other
    # section stays leaf-addressable, an override being an edit, not a rewrite.
    if path == "orders":
        layout.setdefault("orders", [])
        return layout, "orders"
    section, _, rest = path.partition(".")
    # The path network (PHASE4 E1) is one OBJECT holding two named lists, so it
    # gets its own two-level split: "network.nodes.<node>.x" moves a dock,
    # "network.segments.<a>-><b>.length" lengthens an aisle. Those are the
    # geometry knobs a layout study actually turns.
    if section == "network":
        return _resolve_network(layout, rest, path)
    if section == "simulation":
        simulation = layout.get("simulation", {})
        return _descend(simulation, rest.split(".") if rest else [], path,
                        _schema_of("simulation", simulation), write)
    if section == "initial_state":
        return _resolve_state(layout, rest, path)
    if section == _SUBMODEL_SECTION:
        return _resolve_submodel(layout, rest, path)
    if section not in _SECTIONS:
        _fail(path, f"unknown section '{section}' (expected one of "
                    f"{', '.join(_SECTIONS + ('simulation', 'network', _SUBMODEL_SECTION))})")
    entry, fields = _split_entry(section, layout.get(section, []), rest, path)
    if section == "agent_types" and "python" in fields:
        # PHASE6 A7 (D105): a scenario, a data binding or an agent tool must
        # never rewrite code a caller consented to run.
        _fail(path, "an agent type's python code cannot be overridden")
    if section == "blocks" and fields[:1] != ["params"]:
        _fail(path, "only a block's params can be overridden")
    if section == "links" and fields[:1] != ["delay"]:
        _fail(path, "only a link's delay can be overridden")
    return _descend(entry, fields, path, _schema_of(section, entry), write)


def apply_overrides(layout: dict, overrides) -> dict:
    """Return a NEW layout with every override applied; *layout* is untouched.

    Raises ModelValidationError naming the path when it addresses nothing in
    this model. The result is not validated here -- `run_scenarios` does that
    once per scenario, so a caller building a layout by hand can still apply an
    override to an intermediate state.
    """
    new = copy.deepcopy(layout)
    for override in overrides or ():
        container, key = _resolve(new, override.get("path", ""), write=True)
        container[key] = copy.deepcopy(override.get("value"))
    return new


def override_problems(layout: dict) -> list[Problem]:
    """W_SCENARIO_PATH for each scenario override whose path addresses nothing in *layout*
    (review S2-05, D1222), at the override's JSON pointer with the nearest name: read on the
    layout as saved, so a `submodels.<definition>...` path resolves. A warning, so the base
    model still runs (a study step keeps a bad scenario apart); running that scenario fails.
    An override without a path or a value is `_validate_scenarios`' to report."""
    scenarios = [s for s in layout.get("scenarios") or [] if isinstance(s, dict)]
    if not any(s.get("overrides") for s in scenarios):
        return []
    scratch = copy.deepcopy(layout)     # resolving a state path creates the entry it names
    found = []
    for i, scenario in enumerate(layout.get("scenarios") or []):
        if not isinstance(scenario, dict) or not isinstance(scenario.get("overrides"), list):
            continue
        for j, override in enumerate(scenario["overrides"]):
            if not isinstance(override, dict) or not isinstance(override.get("path"), str) \
                    or not override["path"] or "value" not in override:
                continue
            try:
                _resolve(scratch, override["path"], write=True)
            except ModelValidationError as exc:
                said = exc.structured[0] if exc.structured else None
                found.append(problem(
                    "W_SCENARIO_PATH", f"scenario '{scenario.get('name', i)}': {exc}",
                    f"/scenarios/{i}/overrides/{j}/path", hint=said.hint if said else None))
    return found


def value_at(layout: dict, path: str):
    """The value *path* currently addresses in *layout*.

    The read half of the override grammar: `nagasim.databind` (ROADMAP 3.4a)
    needs it to type-check a binding's result against the field it is about to
    overwrite. Raises ModelValidationError naming the path when it addresses
    nothing, exactly as `apply_overrides` does.
    """
    if path.startswith("initial_state."):
        # Resolving one creates the entry it addresses: on a copy, a read changes nothing.
        layout = {**layout, "initial_state": copy.deepcopy(layout.get("initial_state") or {})}
    container, key = _resolve(layout, path)
    return container[key]


def _walk_override_targets(layout: dict) -> list[tuple[str, str, str, str, object]]:
    """Every leaf the path grammar can address, as (path, section, key, field,
    current value) -- one walk shared by `list_override_paths` (path, value)
    and `override_targets` (path, section, key, field), so both stay in step
    with the same grammar instead of two copies of it.

    A leaf is anything that is not a dict, so a schedule's `points` list
    appears as one addressable value (replacing the whole calendar is the
    normal way to change a shift). An entry's own `name` is left out:
    renaming a resource through an override would silently break every block
    that refers to it. *field* is the leaf's path under the entry's own root
    ("params.capacity" for a block's capacity, "" for a section-level leaf
    such as "simulation.seed" or "network.length_factor").
    """
    out: list[tuple[str, str, str, str, object]] = []

    def walk(root: str, section: str, key: str, field: str, value) -> None:
        if isinstance(value, dict):
            for sub_key, sub in value.items():
                if sub_key not in ("name", "id", "python"):  # "id": an order's key; python: A7
                    walk(root, section, key, f"{field}.{sub_key}" if field else sub_key, sub)
        else:
            out.append((f"{root}.{field}" if field else root, section, key, field, value))

    for section in _SECTIONS:
        entries = layout.get(section, [])
        for key, entry in zip(_entry_keys(section, entries), entries):
            if not key:
                continue
            root = f"{section}.{key}"
            if section == "blocks":
                walk(root, section, key, "params", entry.get("params", {}))
            elif section == "links":
                if entry.get("delay"):
                    walk(root, section, key, "delay", entry["delay"])
            else:
                walk(root, section, key, "", entry)
    # Submodel definitions (ROADMAP 2.3 part B): one path per block param
    # inside each definition. An instance's `arguments` already appear above,
    # under "blocks.<instance>.params.arguments.<parameter>".
    for def_name, definition in (layout.get(_SUBMODEL_SECTION) or {}).items():
        for entry in definition.get("blocks", []):
            if entry.get("name"):
                key = f"{def_name}.blocks.{entry['name']}"
                walk(f"{_SUBMODEL_SECTION}.{key}", _SUBMODEL_SECTION, key, "params",
                     entry.get("params", {}))
    # The path network (PHASE4 E1): its nodes and segments are the geometry a
    # layout study varies, so each one's fields are addressable.
    network = layout.get("network") or {}
    for kind in ("nodes", "segments"):
        section = f"network.{kind}"
        for entry in network.get(kind) or []:
            key = _entry_key(section, entry)
            if key:
                walk(f"{section}.{key}", section, key, "", entry)
    if "length_factor" in network:
        out.append(("network.length_factor", "network", "", "length_factor",
                    network["length_factor"]))
    walk("simulation", "simulation", "", "", layout.get("simulation", {}))
    return out


def list_override_paths(layout: dict) -> list[tuple[str, object]]:
    """Every leaf the path grammar can address, as (path, current value).
    Feeds the UI's path picker."""
    return [(path, value) for path, _section, _key, _field, value
            in _walk_override_targets(layout)]


def override_targets(layout: dict) -> list[tuple[str, str, str, str]]:
    """Every leaf the path grammar can address, as (path, section, key,
    field): the section it belongs to, the entry key inside that section (by
    name, id, or "source->target" for a link; "" for a section-level path
    such as "simulation.seed" or "network.length_factor"), and the field path
    under that entry's root. The public seam UI code (`param_labels` in
    `product/ui/experiments.py`) uses to build parameter labels, instead of
    reaching into `_entry_key`/`_entry_keys`/`_SECTIONS` directly.
    """
    return [(path, section, key, field) for path, section, key, field, _value
            in _walk_override_targets(layout)]
