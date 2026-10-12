"""Generic record tools, the path network, scenarios and CSV importers
(F2: records -- everything addressed by a keyed record section, plus the
network graph and the CSV readers that feed records).
"""
from __future__ import annotations

import math

from nagasim.core import network as network_mod
from nagasim.core import orders as orders_mod
from nagasim.core import sections
from nagasim.core import tables as tables_mod
from nagasim.core import timeseries_inputs as series_mod

from .core import ToolError, _document_args, _edit_result, _open, _upsert, tool, workspace

# -- generic record tools (D147) ---------------------------------------------
# One list/upsert/remove over every keyed record section replaced ~40 typed
# tools whose arguments restated the JSON Schema by hand and drifted from it.
# The record shape IS the schema's $def; get_record_schema hands it out.
RECORD_TOOL_SECTIONS = tuple(s.name for s in sections.SECTIONS
                             if s.record or s.name == "orders")
_SECTIONS_HINT = ("one of: " + ", ".join(RECORD_TOOL_SECTIONS) + ". Blocks, links, the network, "
                  "scenarios and the simulation settings have their own tools.")
_RECORD_DOC = {"model": "Workspace name of the model.",
               "section": "Record section, e.g. \"resources\", \"tables\", \"levels\", "
                          "\"agent_types\", \"orders\" (get_record_schema lists them all)."}


def _record_section(section: str) -> str:
    if section not in RECORD_TOOL_SECTIONS:
        raise ToolError("E_UNKNOWN_SECTION", f"'{section}' is not a record section",
                        _SECTIONS_HINT)
    return sections.KEY[section]


_document_args("get_record_schema", section=_RECORD_DOC["section"])


@tool(read_only=True)
def get_record_schema(section: str) -> dict:
    """The JSON Schema of one record of *section*: what `upsert_record` expects.

    The same `$defs/<item>` the model-file schema uses (MCP resource
    naga://reference/schema), with the `$defs` it references. Read it once
    before writing the first record of a section.

    Returns {"section", "key", "def", "schema", "$defs"}.
    """
    key = _record_section(section)
    from nagasim.modelschema import build_json_schema
    full = build_json_schema()
    name = full["properties"][section]["items"]["$ref"].rsplit("/", 1)[-1]
    defs, todo = {}, [name]
    while todo:
        current = todo.pop()
        if current in defs:
            continue
        defs[current] = full["$defs"][current]
        todo.extend(ref.rsplit("/", 1)[-1] for ref in _refs(defs[current]))
    return {"section": section, "key": key, "def": name, "schema": defs.pop(name),
            "$defs": defs}


def _refs(node):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


_document_args("list_records", **_RECORD_DOC)


@tool(read_only=True)
def list_records(model: str, section: str) -> dict:
    """List every record of one section of the model, as stored.

    Record sections: resources, resource_sets, schedules, failures, tables,
    sequences, variables, entities, timeseries_inputs, levels, flows,
    conversions, fixed_costs, fleets, motion_profiles, chargers, auxiliaries,
    delays, subscripts, agent_types, populations, reality_checks and orders
    (keyed by "id"; every other section by "name"). Record shape: schema
    $defs/<item>, see get_record_schema.

    Returns {"model", "section", "key", "records": [...]}.
    """
    key = _record_section(section)
    doc = workspace().document(model)
    return {"model": model, "section": section, "key": key,
            "records": [dict(r) for r in getattr(doc, section)]}


_document_args("upsert_record", **_RECORD_DOC,
               record="The whole record, shaped as get_record_schema(section) says; its key "
                      "field (\"name\", or \"id\" for orders) picks the record to replace.")


@tool()
def upsert_record(model: str, section: str, record: dict) -> dict:
    """Add a record to a section, or REPLACE the one with the same key.

    The record is stored as given (record shape: schema $defs/<item>, see
    get_record_schema) and the answer reports the validation problems the edit
    introduced or fixed -- a missing field, a wrong type, a reference to
    something undeclared. To change one field, list_records, edit, upsert.

    Typical first records: a "resources" entry {"name", "capacity"} for a
    PROCESS/SEIZE to seize; a "variables" entry {"name", "initial"} before an
    expression reads it; a "tables" entry for TABLE("name", k); a "levels" and
    a "flows" entry for continuous quantities. An agent type's `python` cannot
    be written here: code needs the human's consent.

    Returns the usual edit answer plus {"section", "record": <key value>}.
    """
    key = _record_section(section)
    if not isinstance(record, dict) or not isinstance(record.get(key), str) or not record[key]:
        raise ToolError("E_BAD_RECORD", f"a {section} record needs a non-empty string \"{key}\"",
                        "call get_record_schema for the shape")
    doc, before = _open(model)
    if section == "agent_types":
        # #156: agent Python runs on the CALLER's consent (NAGASIM_ALLOW_AGENT_PYTHON),
        # granted for code a human wrote -- so an agent may carry an existing
        # type's `python` through an edit unchanged, never author or alter it.
        old = next((r for r in doc.agent_types if r.get("name") == record[key]), {})
        if record.get("python") != old.get("python"):
            raise ToolError("E_PYTHON_NOT_AGENT_SETTABLE",
                            "an agent type's `python` cannot be set or changed through a tool",
                            "leave `python` as it is; a human edits agent Python in the editor")
    doc.upsert_record(section, dict(record))
    return _edit_result(doc, before, {"section": section, "record": record[key]})


_document_args("remove_record", **_RECORD_DOC,
               key="The record's key: its \"name\" (its \"id\" for orders).")


@tool()
def remove_record(model: str, section: str, key: str) -> dict:
    """Delete one record. References to it elsewhere (TABLE("name"), a flow's
    "from", a block's resource_name, ...) come back as problems in the answer,
    so fix them in the same batch.

    Returns the usual edit answer plus {"section", "removed": key}.
    """
    field = _record_section(section)
    doc, before = _open(model)
    present = [r.get(field) for r in getattr(doc, section)]
    if key not in present:
        raise ToolError("E_UNKNOWN_RECORD", f"no {section} record with {field} '{key}'",
                        f"existing: {', '.join(map(str, present)) or '(none)'}")
    doc.remove_record(section, key)
    return _edit_result(doc, before, {"section": section, "removed": key})


_document_args("upsert_table_from_csv", model="Workspace name of the model to edit.",
               name="Table name to create or replace.",
               path="CSV file under the workspace root.",
               key_mode="Lookup mode for the row keys: exact, step or linear.",
               column_mode="Lookup mode for the column keys.",
               default="Value for a missing exact key.")


@tool()
def upsert_table_from_csv(model: str, name: str, path: str, key_mode: str = "exact",
                          column_mode: str = "exact", default: float | None = None) -> dict:
    """Read a CSV under the workspace root into a lookup table.

    Two shapes: a 1-D table is `key,value` under a `key,value` header; a 2-D
    table is a header row of column keys whose first cell is EMPTY, then one
    row per key. The file must be under the workspace root -- a path that
    escapes it is refused, and there is no argument that widens that.

    Returns the usual edit answer plus {"table": name, "rows", "columns"}.
    """
    doc, before = _open(model)
    full = workspace().resolve(path, must_exist=True)
    try:
        entry = tables_mod.table_from_csv(full.read_text(encoding="utf-8"), name,
                                          key_mode=key_mode, column_mode=column_mode,
                                          default=default)
    except tables_mod.TableError as exc:
        raise ToolError("E_TABLE_CSV", str(exc),
                        "1-D is 'key,value' with a header; 2-D is a header row of column keys "
                        "whose first cell is empty") from None
    doc.upsert_table(entry)
    return _edit_result(doc, before, {"table": name, "rows": len(entry["keys"]),
                                      "columns": entry.get("columns")})


# ---------------------------------------------------------------- PHASE4 D1/D2
_document_args("list_continuous", model="Workspace name of the model to inspect.")


@tool(read_only=True)
def list_continuous(model: str) -> dict:
    """List the model's time-series inputs, levels and flows in one answer.

    A time series is a profile read by TS("name") -- a PV curve, a site load, a
    tariff by hour. A level is a continuous quantity with bounds (battery
    energy, tank volume, stock on hand), read by LEVEL("name"). A flow moves
    quantity between levels at a rate that is an expression, read by
    FLOW("name"). Call this before writing any of those expressions.

    Auxiliaries (AUX("name")) and delays (DELAY("name")) are the system-dynamics
    records that go with them.

    Subscripts are the dimensions records run over. Edit any of these with
    upsert_record / remove_record.

    Returns {"model", "timeseries_inputs": [{"name", "points", "interpolation",
    "beyond"}], "levels": [...], "flows": [...], "auxiliaries": [...],
    "delays": [...], "subscripts": [...], "sd": {...} or null}.
    """
    doc = workspace().document(model)
    return {
        "model": model,
        "sd": doc.simulation.get("sd"),
        "timeseries_inputs": [{"name": s.get("name"), "points": len(s.get("values") or []),
                               "interpolation": s.get("interpolation", "step"),
                               "beyond": s.get("beyond", "hold")}
                              for s in doc.timeseries_inputs],
        "levels": [dict(level) for level in doc.levels],
        "flows": [dict(flow) for flow in doc.flows],
        "auxiliaries": [dict(aux) for aux in doc.auxiliaries],
        "delays": [dict(delay) for delay in doc.delays],
        "subscripts": [dict(entry) for entry in doc.subscripts],
    }


_document_args("upsert_timeseries_input_from_csv", model="Workspace name of the model to edit.",
               name="Series name to create or replace.",
               path="CSV file under the workspace root, two columns: time and value.",
               interpolation="step or linear.",
               beyond="hold, repeat or error.",
               time_unit="Unit the time column is in.")


@tool()
def upsert_timeseries_input_from_csv(model: str, name: str, path: str,
                                     interpolation: str = "step", beyond: str = "hold",
                                     time_unit: str = "base") -> dict:
    """Read a `time,value` CSV under the workspace root into a time-series input.

    The header row is optional. The file must be under the workspace root -- a
    path that escapes it is refused, and there is no argument that widens that.

    Returns the usual edit answer plus {"series": name, "points": n}.
    """
    doc, before = _open(model)
    full = workspace().resolve(path, must_exist=True)
    try:
        entry = series_mod.series_from_csv(full.read_text(encoding="utf-8"), name,
                                           interpolation=interpolation, beyond=beyond,
                                           time_unit=time_unit)
    except series_mod.SeriesError as exc:
        raise ToolError("E_SERIES_CSV", str(exc),
                        "two columns, time then value, with times strictly increasing") from None
    doc.upsert_timeseries_input(entry)
    return _edit_result(doc, before, {"series": name, "points": len(entry["values"])})


# -- path network, movers and conveyors (PHASE4 E1, ARCHITECTURE D68) -------
_document_args("get_network", model="Workspace name of the model to inspect.")


@tool(read_only=True)
def get_network(model: str) -> dict:
    """The model's path network: the nodes assets sit on and the segments
    vehicles and walkers drive along, with the shortest-path distance between
    every pair of places.

    Coordinates are layout coordinates -- metres once the model's background
    image declares a scale. Travel time anywhere in the model is
    `distance / speed` along this graph, so this is what to read before
    reasoning about how long anything takes to move.

    Returns {"model", "length_factor", "nodes": [...], "segments": [...],
    "places": {name: node}, "unreachable": [[a, b], ...]}.
    """
    doc = workspace().document(model)
    layout = doc.to_layout()
    places = network_mod.block_nodes(layout)
    net = network_mod.build_network(layout.get("network"))
    unreachable = sorted({(a, b) for a in places for b in places
                          if a != b and net.has(places[a]) and net.has(places[b])
                          and math.isinf(net.distance(places[a], places[b]))})
    return {"model": model,
            "length_factor": (doc.network or {}).get("length_factor", 1.0),
            "nodes": [dict(n) for n in doc.network_nodes()],
            "segments": [dict(s) for s in doc.network_segments()],
            "places": places,
            "unreachable": [list(pair) for pair in unreachable]}


_document_args("upsert_node", model="Workspace name of the model to edit.",
               name="Node name; a STATION's `node` param, a fleet's `home` and "
                    "DISTANCE(\"...\", ...) all address it by this.",
               x="Layout x coordinate (metres once the background declares a scale).",
               y="Layout y coordinate.")


@tool()
def upsert_node(model: str, name: str, x: float, y: float) -> dict:
    """Add a network node, or move the one with this name.

    A node is a PLACE: a dock door, an aisle junction, a pick face. Attach a
    block to it with the STATION `node` param and travel to and from that
    station becomes the shortest path along the segments.

    Returns the usual edit answer plus {"node": name}.
    """
    doc, before = _open(model)
    doc.upsert_node({"name": name, "x": float(x), "y": float(y)})
    return _edit_result(doc, before, {"node": name})


_document_args("remove_node", model="Workspace name of the model to edit.",
               name="Node to delete; every segment touching it goes with it.")


@tool()
def remove_node(model: str, name: str) -> dict:
    """Delete a network node AND the segments that used it (a segment with a
    dangling end is not a thing a model can mean). Blocks whose `node` still
    names it come back as E_NETWORK_NODE_UNKNOWN problems.

    Returns the usual edit answer plus {"removed": name}.
    """
    doc, before = _open(model)
    doc.remove_node(name)
    return _edit_result(doc, before, {"removed": name})


_document_args("upsert_segment", model="Workspace name of the model to edit.",
               source="Node the segment starts at.",
               target="Node it ends at.",
               length="Length in layout units. Omit for the straight-line distance times the "
                      "network's length_factor -- set it for a ramp, a curve or a detour.",
               two_way="False makes it one-way from source to target.",
               speed="Optional speed LIMIT on this segment. It changes travel TIME; the route "
                     "chosen is still the shortest by length.",
               capacity="How many vehicles may be on it at once (zone control); omit "
                        "for unlimited. Two-way with capacity 1 can deadlock head-on.")


@tool()
def upsert_segment(model: str, source: str, target: str, length: float | None = None,
                   two_way: bool = True, speed: float | None = None,
                   capacity: int | None = None) -> dict:
    """Add a drivable segment between two nodes, or replace the one already
    joining that ordered pair.

    Returns the usual edit answer plus {"segment": "source->target"}.
    """
    doc, before = _open(model)
    entry = {"from": source, "to": target, "two_way": bool(two_way)}
    for key, value in (("length", length), ("speed", speed), ("capacity", capacity)):
        if value is not None:
            entry[key] = value
    doc.upsert_segment(entry)
    return _edit_result(doc, before, {"segment": f"{source}->{target}"})


_document_args("remove_segment", model="Workspace name of the model to edit.",
               source="Node the segment starts at.", target="Node it ends at.")


@tool()
def remove_segment(model: str, source: str, target: str) -> dict:
    """Delete one segment. Check `get_network`'s "unreachable" afterwards: a
    removal that strands a routing is an E_NETWORK_UNREACHABLE error.

    Returns the usual edit answer plus {"removed": "source->target"}.
    """
    doc, before = _open(model)
    doc.remove_segment(source, target)
    return _edit_result(doc, before, {"removed": f"{source}->{target}"})


_document_args("get_distance", model="Workspace name of the model to inspect.",
               source="A node, a STATION block or a station alias.",
               target="Where the trip ends, named the same way.")


@tool(read_only=True)
def get_distance(model: str, source: str, target: str) -> dict:
    """Shortest path between two places on the model's network.

    The same number `DISTANCE("source", "target")` returns in an expression, so
    use it to sanity-check a travel time before writing one: a trip at speed v
    takes `distance / v`.

    Returns {"model", "from", "to", "distance", "path": [node, ...],
    "reachable"}. `distance` is null when no path joins them.
    """
    doc = workspace().document(model)
    layout = doc.to_layout()
    places = network_mod.block_nodes(layout)
    net = network_mod.build_network(layout.get("network"))
    a, b = places.get(source, source), places.get(target, target)
    for given, node in ((source, a), (target, b)):
        if not net.has(node):
            raise ToolError("E_NETWORK_NODE_UNKNOWN", f"unknown place '{given}'",
                            hint=f"known places: {', '.join(sorted(places)) or '(none)'}")
    distance = net.distance(a, b)
    reachable = not math.isinf(distance)
    return {"model": model, "from": source, "to": target,
            "distance": distance if reachable else None,
            "path": net.path(a, b), "reachable": reachable}


_document_args("list_sequences", model="Workspace name of the model to inspect.")


@tool(read_only=True)
def list_sequences(model: str) -> dict:
    """List the model's routing sequences and the stations they visit.

    A sequence is the operation order a product follows through shared
    stations: a ROUTE block sends an entity whose `sequence`
    attribute names one to each step's STATION block in turn. Call this
    before writing a CREATE `sequence` or an ASSIGN to entity.sequence, so
    the name and the stations are ones the model actually has.

    Returns {"model", "sequences": [{"name", "steps": [{"station", "values"}]}],
    "stations": [names of the model's STATION blocks]}.
    """
    doc = workspace().document(model)
    return {"model": model,
            "sequences": [{"name": s.get("name"), "steps": s.get("steps", [])}
                          for s in doc.sequences],
            "stations": sorted(b.name for b in doc.blocks.values() if b.type == "STATION")}


_document_args("list_orders", model="Workspace name of the model to inspect.")


@tool(read_only=True)
def list_orders(model: str) -> dict:
    """List the model's orders: released jobs with a due date.

    An order is "make `quantity` of `entity_type` on routing `sequence`,
    released at `release`, due at `due`", both times in the model's BASE time
    unit. A CREATE block in arrival mode "Orders" releases them; the run then
    reports per-order lateness and tardiness and a model-wide service level.

    Returns {"model", "orders": [...], "releases": [names of the CREATE blocks
    in Orders mode]}.
    """
    doc = workspace().document(model)
    return {"model": model, "orders": [dict(o) for o in doc.orders],
            "releases": orders_mod.orders_mode_creates(doc.to_layout())}


_document_args("upsert_orders_from_csv", model="Workspace name of the model to edit.",
               path="CSV file to read, inside the workspace root.",
               replace="True (default) replaces the whole order book; False merges by id.")


@tool()
def upsert_orders_from_csv(model: str, path: str, replace: bool = True) -> dict:
    """REPLACE (or extend) the model's orders from a CSV release list.

    Columns: `id` (required), then any of release, due, quantity, entity_type,
    sequence, priority, source; every other column becomes a free attribute
    assigned onto the order's entities. This is the shape an MES work-order
    export already has.

    The file is read through the same root confinement as every other file
    tool: a path that escapes it is refused, and there is no argument that
    widens that.

    Returns the usual edit answer plus {"orders": <count>}.
    """
    doc, before = _open(model)
    full = workspace().resolve(path, must_exist=True)
    try:
        entries = orders_mod.orders_from_csv(full.read_text(encoding="utf-8"))
    except orders_mod.OrderError as exc:
        raise ToolError("E_ORDER_CSV", str(exc),
                        "the CSV needs a header row with an \"id\" column; release/due/quantity "
                        "are numbers in the model's base time unit") from None
    if replace:
        doc.set_orders(entries)
    else:
        with doc.transaction():
            for entry in entries:
                doc.upsert_order(entry)
    return _edit_result(doc, before, {"orders": len(doc.orders)})


_document_args("upsert_scenario", model="Workspace name of the model to edit.",
               name="Scenario name; \"Baseline\" is implicit and always present.",
               overrides="[{\"path\": \"resources.Server.capacity\", \"value\": 2}, ...].",
               description="Free-text note about what this scenario represents.")


@tool()
def upsert_scenario(model: str, name: str, overrides: list, description: str | None = None) -> dict:
    """Add or update a named scenario: a set of parameter overrides applied to
    the base model before a run.

    An override path addresses the layout by name, not by index -- for example
    "resources.Server.capacity", "blocks.process_1.params.dist_params.rate" or
    "simulation.replications". Scenarios are how you answer "A vs B, how sure
    are we"; run them with `run_scenarios`.

    Returns the usual edit answer plus {"scenario": name}.
    """
    doc, before = _open(model)
    entry = {"name": name, "overrides": [dict(o) for o in overrides]}
    if description:
        entry["description"] = description
    doc.set_scenarios(_upsert(doc.scenarios, entry))
    return _edit_result(doc, before, {"scenario": name})
