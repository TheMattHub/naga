"""Routings: the "sequences" model section (PHASE4 A2).

A sequence is a named, ordered list of operations a product follows through
shared stations -- a job shop routing, a flexible line's process plan, an MES
work order. The JSON shape is the routing sheet a planner would write:

    {"name": "Widget",
     "steps": [{"station": "Mill",  "values": {"process_time": 3, "op": "rough"}},
               {"station": "Drill", "values": {"process_time": 2}}]}

A step names a STATION block (the station's name IS the block's name, see
ARCHITECTURE D51) -- or, since PHASE4 B6, that block's `station` ALIAS, which
is what lets a station inside a submodel or an asset carry a user-chosen name
instead of the flattened `instance/station` path. Both names reach the same
block; the alias is the one the station reports as `entity.station`. A step may
carry `values`: numbers or strings assigned to the
entity as attributes when ROUTE sends it to that step. That is how a process
time by product and operation reaches the station's PROCESS block -- either
straight from the step (`entity.process_time`) or through a lookup table
(`TABLE("ProcessTime", entity.e_type, entity.station)`).

`build_sequences` normalises the section once per run into `ctx.sequences`;
ROUTE then costs one list index per jump.

Stdlib only, a leaf module beside `tables.py`, with its own structured
validation so `core/schema/` just calls it.
"""
from __future__ import annotations

from nagasim.core.problems import problem

__all__ = ["build_sequences", "station_names", "station_blocks", "station_alias",
           "validate_sequences_structured"]


def build_sequences(section) -> dict[str, list]:
    """{name: [{"station", "values"}, ...]} for the layout's "sequences"
    section, built once per run. Shape faults are validation's job
    (`validate_sequences_structured`); this only normalises."""
    out = {}
    for entry in section or []:
        steps = []
        for step in entry.get("steps") or []:
            if not isinstance(step, dict):
                continue
            steps.append({"station": str(step.get("station", "")),
                          "values": dict(step.get("values") or {})})
        out[str(entry.get("name", ""))] = steps
    return out


def _stations(layout: dict):
    """(block name, alias) of every STATION block, in layout order."""
    return [(b.get("name", ""), station_alias(b)) for b in layout.get("blocks", [])
            if str(b.get("type", "")).upper() == "STATION"]


def station_alias(block: dict) -> str:
    """The block's `station` param, the user-facing name it answers to, or ""
    when it keeps its block name (D51's default, PHASE4 B6)."""
    return str((block.get("params") or {}).get("station", "") or "").strip()


def station_names(layout: dict) -> list[str]:
    """Every station's PRIMARY name, in layout order: its `station` alias when
    it has one, otherwise its block name. A station IS a block, so a station
    inside a submodel instance carries the `instance/station` name the flattener
    gives it unless the alias says otherwise (ARCHITECTURE D51, D59)."""
    return [alias or name for name, alias in _stations(layout)]


def station_blocks(layout: dict) -> dict[str, str]:
    """{name a sequence step may use: the STATION block serving it}. An aliased
    station answers to BOTH names, so a routing written before the alias still
    resolves."""
    out: dict[str, str] = {}
    for name, alias in _stations(layout):
        out[name] = name
        if alias:
            out[alias] = name
    return out


def validate_sequences_structured(layout: dict) -> tuple[list, dict]:
    """Check the layout's "sequences" section against its STATION blocks.

    Returns (list[Problem], {name: step count}); the names are what
    `core/schema/` checks a CREATE/ASSIGN `sequence` against.
    """
    problems: list = []
    steps_of: dict[str, int] = {}
    stations = set(station_blocks(layout))
    known = ", ".join(sorted(station_names(layout))) or "(none)"

    for index, entry in enumerate(layout.get("sequences", []) or []):
        at = f"/sequences/{index}"
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_SEQUENCE_NAME", "sequence: name must not be empty",
                                    f"{at}/name", hint="give every sequence a unique name"))
            continue
        if name in steps_of:
            problems.append(problem("E_SEQUENCE_NAME", f"duplicate sequence name: {name}",
                                    f"{at}/name", hint="rename one of the two sequences"))

        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            problems.append(problem("E_SEQUENCE_EMPTY", f"sequence '{name}': no steps",
                                    f"{at}/steps",
                                    hint="list at least one step: "
                                         "{\"station\": \"<a STATION block>\"}"))
            steps_of[name] = 0
            continue
        steps_of[name] = len(steps)

        for step_index, step in enumerate(steps):
            where = f"{at}/steps/{step_index}"
            if not isinstance(step, dict):
                problems.append(problem(
                    "E_SEQUENCE_STEP", f"sequence '{name}': step {step_index} is not an object",
                    where, hint="a step is {\"station\": \"<name>\", \"values\": {...}}"))
                continue
            station = step.get("station", "")
            if not isinstance(station, str) or not station.strip():
                problems.append(problem(
                    "E_SEQUENCE_STEP", f"sequence '{name}': step {step_index} names no station",
                    f"{where}/station", hint=f"name a STATION block: {known}"))
            elif station not in stations:
                problems.append(problem(
                    "E_SEQUENCE_STATION_UNKNOWN",
                    f"sequence '{name}': step {step_index}: unknown station '{station}'",
                    f"{where}/station",
                    hint=f"add a STATION block named '{station}'; declared: {known}"))
            problems.extend(_value_problems(name, step_index, step.get("values"), where))

    problems.extend(_usage_problems(layout, stations, steps_of))
    return problems, steps_of


def _value_problems(name: str, step_index: int, values, where: str) -> list:
    """A step's `values` is a flat {attribute: number|string|bool} mapping --
    it is assigned straight onto the entity, so nothing nested is meaningful."""
    if values is None:
        return []
    if not isinstance(values, dict):
        return [problem("E_SEQUENCE_STEP",
                        f"sequence '{name}': step {step_index}: \"values\" must be an object",
                        f"{where}/values",
                        hint="write {\"process_time\": 3, \"operation\": \"mill\"}")]
    problems = []
    for key, value in values.items():
        if not str(key).isidentifier() or str(key).startswith("_"):
            problems.append(problem(
                "E_SEQUENCE_STEP",
                f"sequence '{name}': step {step_index}: '{key}' is not a valid attribute name",
                f"{where}/values/{key}",
                hint="an entity attribute is a Python identifier not starting with _"))
        elif not isinstance(value, (int, float, str, bool)):
            problems.append(problem(
                "E_SEQUENCE_STEP",
                f"sequence '{name}': step {step_index}: value of '{key}' must be a number, "
                f"string or boolean",
                f"{where}/values/{key}",
                hint="a step value is assigned straight onto the entity: keep it scalar"))
    return problems


def _usage_problems(layout: dict, stations: set, steps_of: dict) -> list:
    """The cross-section checks: a ROUTE block with nothing to route by, a
    station no sequence ever visits, and two stations answering to one name."""
    problems = []
    blocks = layout.get("blocks", [])
    routes = [b for b in blocks if str(b.get("type", "")).upper() == "ROUTE"]
    if routes and not (stations and steps_of):
        missing = "stations" if not stations else "sequences"
        first = routes[0].get("name", "<unnamed>")
        problems.append(problem(
            "E_ROUTE_NO_SEQUENCES",
            f"{first}: ROUTE needs {missing}: the model declares none",
            "/sequences" if missing == "sequences" else "/blocks", block=first,
            hint="declare a sequence under \"sequences\" and a STATION block per operation"))

    visited = {str(step.get("station", ""))
               for entry in layout.get("sequences", []) or []
               for step in entry.get("steps") or [] if isinstance(step, dict)}
    index_of = {b.get("name"): i for i, b in enumerate(blocks)}
    # A STATION bound to a path-network node is a PLACE that TRANSPORT and
    # CONVEYOR deliver to (PHASE4 E1); it needs no sequence to be in use, so
    # the "no sequence names it" warning is only for routing stations -- and
    # only in a model that routes by sequence at all (hardening pass).
    placed = {b.get("name") for b in blocks
              if isinstance(b.get("params"), dict) and b["params"].get("node")}
    routes_by_sequence = bool(layout.get("sequences"))
    owner: dict[str, str] = {}
    for name, alias in _stations(layout):
        for candidate in [name] + ([alias] if alias else []):
            if owner.setdefault(candidate, name) != name:
                problems.append(problem(
                    "E_DUPLICATE_STATION",
                    f"station name '{candidate}' is claimed by both '{owner[candidate]}' "
                    f"and '{name}'",
                    f"/blocks/{index_of.get(name, 0)}/params/station", block=name,
                    hint="a station name identifies one block: rename one of the two"))
        if routes_by_sequence and name not in placed and not visited & ({name, alias} - {""}):
            problems.append(problem(
                "W_STATION_UNUSED",
                f"station {alias or name} is named by no sequence",
                f"/blocks/{index_of.get(name, 0)}", block=name,
                hint="add it to a sequence's steps, or delete the STATION block"))
    return problems
