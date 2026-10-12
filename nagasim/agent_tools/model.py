"""Block/link editing tools plus model-level settings (F2: model/blocks/links).

Adding, wiring and removing blocks; resources; the calendar; entity types;
simulation and system-dynamics settings; `auto_layout`. Everything here
mutates through `.core._open`/`_edit_result`, exactly like the rest of the
package.
"""
from __future__ import annotations

from nagasim.core import layout_tools

from .core import ToolError, _document_args, _duration, _edit_result, _open, tool

_document_args("add_block",
               model="Workspace name of the model to edit.",
               type="Block type, e.g. \"CREATE\", \"PROCESS\", \"DISPOSE\" (see list_block_types).",
               name="Block name; auto-generated (create_1, process_2, ...) when omitted.",
               params="Parameter values to set on the block; the type's defaults fill the rest.",
               position="[x, y] canvas centre in scene units; auto-placed in a row when omitted.")


@tool()
def add_block(model: str, type: str, name: str | None = None, params: dict | None = None,
              position: list | None = None) -> dict:
    """Add one block to a model and optionally set its parameters in the same
    step (one undo entry).

    The normal way to build a model: add CREATE, the processing blocks, and
    DISPOSE, then `connect` them. Position is cosmetic -- call `auto_layout`
    once at the end instead of placing blocks by hand.

    Returns the usual edit answer plus {"block": <the name it got>}. A wrong
    param name or type comes back in "introduced" with code E_PARAM_UNKNOWN /
    E_PARAM_TYPE and a hint listing the params this type takes.
    """
    doc, before = _open(model)
    if position is None:
        position = [200.0 + 220.0 * len(doc.blocks), 300.0]
    with doc.transaction():
        created = doc.add_block(type, (float(position[0]), float(position[1])), name)
        if params:
            merged = {**doc.blocks[created].params, **params}
            doc.set_params(created, merged)
    return _edit_result(doc, before, {"block": created})


_document_args("update_block",
               model="Workspace name of the model to edit.",
               name="Current name of the block to change.",
               params="Parameters to merge into the block's existing params.",
               description="New canvas label.",
               position="New [x, y] centre.",
               new_name="Rename the block to this; every link follows the rename.")


@tool()
def update_block(model: str, name: str, params: dict | None = None,
                 description: str | None = None, position: list | None = None,
                 new_name: str | None = None) -> dict:
    """Change an existing block: merge parameters, rename it, move it, or
    relabel it -- all as one undo entry.

    `params` MERGES rather than replaces, so you can set one value without
    restating the rest. Renaming rewires every link automatically.

    Returns the usual edit answer plus {"block": <name after the edit>}.
    """
    doc, before = _open(model)
    if name not in doc.blocks:
        raise ToolError("E_UNKNOWN_BLOCK", f"no block named '{name}' in model '{model}'",
                        f"blocks: {', '.join(doc.blocks) or '(none)'}")
    final = name
    with doc.transaction():
        if params:
            doc.set_params(name, {**doc.blocks[name].params, **params})
        if description is not None:
            doc.set_description(name, description)
        if position is not None:
            doc.move_blocks({name: (float(position[0]), float(position[1]))})
        if new_name and new_name != name:
            doc.rename_block(name, new_name)
            final = new_name
    return _edit_result(doc, before, {"block": final})


_document_args("remove_blocks",
               model="Workspace name of the model to edit.",
               names="Block names to delete; their links go with them.")


@tool()
def remove_blocks(model: str, names: list) -> dict:
    """Delete blocks and every link touching them, as one undo entry.

    Unknown names are ignored rather than refused, so removing an already-gone
    block is not an error. Expect new problems if you cut the flow in half --
    the answer says which.

    Returns the usual edit answer plus {"removed": [names actually deleted]}.
    """
    doc, before = _open(model)
    removed = [n for n in names if n in doc.blocks]
    doc.remove_blocks(removed)
    return _edit_result(doc, before, {"removed": removed})


_document_args("connect",
               model="Workspace name of the model to edit.",
               source="Name of the block the entity leaves.",
               target="Name of the block the entity arrives at.",
               source_port="Out port name, only when the source is a SUBMODEL instance.",
               target_port="In port name, only when the target is a SUBMODEL instance.",
               delay="Optional transfer delay, e.g. {\"distribution\": \"Constant\", "
                     "\"dist_params\": {\"value\": 2}}.")


@tool()
def connect(model: str, source: str, target: str, source_port: str | None = None,
            target_port: str | None = None, delay: dict | None = None) -> dict:
    """Link two blocks so entities flow from *source* to *target*, optionally
    with a transfer delay.

    Links are ORDERED: the n-th link you add out of a DECIDE is its branch n,
    matching `probs[n]` / `conditions[n]`. Add branches in the order you want
    them, or fix the order afterwards with `reorder_branches`. Only DECIDE and
    LOGIC may have more than one outgoing link.

    A branching block may send TWO branches to the same target (a DECIDE where
    two cases end the same way): the second call returns `index` 1, which
    `disconnect` takes to tell them apart.

    Returns the usual edit answer plus {"link": "source -> target", "index": n}.
    """
    doc, before = _open(model)
    with doc.transaction():
        index = doc.add_link(source, target, source_port=source_port, target_port=target_port)
        if delay:
            doc.set_link_delay(source, target, _duration(delay, "delay"), index=index)
    return _edit_result(doc, before, {"link": f"{source} -> {target}", "index": index})


_document_args("disconnect", model="Workspace name of the model to edit.",
               source="Source block of the link to remove.",
               target="Target block of the link to remove.",
               index="Which of several parallel links between the pair, 0 first; "
                     "only needed when there is more than one.")


@tool()
def disconnect(model: str, source: str, target: str, index: int | None = None) -> dict:
    """Remove one link. A link that does not exist is a no-op, not an error.

    Use it to rewire a flow -- for example to insert a new block between two
    existing ones: disconnect a -> c, then connect a -> b and b -> c.

    When several parallel links join the pair, name which one with *index*
    (0 is the first); leaving it out is then an error listing the candidates.

    Returns the usual edit answer.
    """
    doc, before = _open(model)
    count = doc.link_count(source, target)
    if index is None and count > 1:
        branches = [doc.branch_index(source, target, index=i) for i in range(count)]
        raise ToolError("E_AMBIGUOUS_LINK",
                        f"{count} links join {source} -> {target} (branches {branches})",
                        f"set \"index\" to one of: {', '.join(str(i) for i in range(count))}")
    doc.remove_link(source, target, index=index or 0)
    return _edit_result(doc, before)


_document_args("reorder_branches", model="Workspace name of the model to edit.",
               source="The branching block (a DECIDE or LOGIC).",
               order="Its target block names in the branch order you want, branch 0 first.")


@tool()
def reorder_branches(model: str, source: str, order: list) -> dict:
    """Set the branch order of a block's outgoing links.

    Branch order is what binds a DECIDE's `probs[n]` / `conditions[n]` to a
    target, so getting it wrong sends entities down the wrong path without any
    validation error. *order* must list exactly the current targets of *source*,
    permuted.

    Returns the usual edit answer plus {"order": [...]}.
    """
    doc, before = _open(model)
    current = doc.out_links(source)
    if sorted(order) != sorted(current):
        raise ToolError("E_BAD_BRANCH_ORDER",
                        f"order {list(order)} is not a permutation of {source}'s targets {current}",
                        "list exactly the current targets, in the order you want them")
    with doc.transaction():
        for wanted_index, target in enumerate(order):
            present = doc.out_links(source)
            # Search from wanted_index on: the branches before it are already
            # placed, and two of them may name the same target (D77).
            doc.reorder_link(source, present.index(target, wanted_index), wanted_index)
    return _edit_result(doc, before, {"order": list(order)})


_document_args("set_resources", model="Workspace name of the model to edit.",
               resources="The complete resource list, each {\"name\", \"capacity\", "
                         "\"discipline\", cost fields...}.")


@tool()
def set_resources(model: str, resources: list) -> dict:
    """REPLACE the model's whole resource table.

    Use `upsert_record` (section "resources") for a single change; this one is
    for setting several resources at once, or for deleting one by leaving it
    out. A block's `resource_name` may name a resource or a resource set --
    they share one namespace.

    Returns the usual edit answer plus {"resources": [names]}.
    """
    doc, before = _open(model)
    doc.set_resources(list(resources))
    return _edit_result(doc, before, {"resources": [r.get("name") for r in doc.resources]})


_document_args("set_calendar", model="Workspace name of the model to edit.",
               start="ISO 8601 instant model time 0 stands for, e.g. \"2026-01-05T06:00:00\". "
                     "With no offset it is read in `timezone`, never in local time. "
                     "Pass null to remove the calendar.",
               timezone="IANA name such as UTC or Europe/Rome (default UTC). The wall clock "
                        "weekday schedules and every date label use.",
               end="Optional ISO instant the run ends at, instead of a run-length number.")


@tool()
def set_calendar(model: str, start: str | None = None, timezone: str = "UTC",
                 end: str | None = None) -> dict:
    """Give the model REAL DATES, or remove them.

    A calendar is optional: without one a model's clock is a plain number in the
    base time unit, which is the right choice for an abstract study. Set one
    when the study is about real weeks -- weekday shifts, a holiday shutdown, a
    tariff or a campaign pinned to dates, or a client report that must say
    "Wednesday 10:00". Once set, schedules may be authored as weekday grids,
    time series and orders may carry ISO timestamps, and results, charts,
    playback and exports all show wall-clock time.

    Returns the usual edit answer plus {"calendar": {...} or null}.
    """
    doc, before = _open(model)
    if not start:
        doc.set_simulation(calendar=None)
    else:
        spec = {"start": start, "timezone": timezone}
        if end:
            spec["end"] = end
        doc.set_simulation(calendar=spec)
    return _edit_result(doc, before, {"calendar": doc.simulation.get("calendar")})


_document_args("set_entity_types", model="Workspace name of the model to edit.",
               entities="The complete entity-type list, each {\"name\", \"shape\"?, \"color\"?, "
                        "\"holding_cost\"?}.")


@tool()
def set_entity_types(model: str, entities: list) -> dict:
    """REPLACE the model's entity-type table.

    Entity types name the things flowing through the model (CREATE's `e_type`)
    and carry the per-type WIP `holding_cost` used by the cost KPIs. A model
    with no declared types still runs: everything is "DEFAULT".

    Returns the usual edit answer plus {"entities": [names]}.
    """
    doc, before = _open(model)
    doc.set_entities(list(entities))
    return _edit_result(doc, before, {"entities": [e.get("name") for e in doc.entities]})


_document_args("set_simulation_settings", model="Workspace name of the model to edit.",
               run_length="Run length in base time units; 0/null means \"until the model drains\". "
                          "With a calendar you can set an end DATE instead, with set_calendar.",
               warmup="Time at which statistics are reset.",
               replications="How many independent replications a run does by default.",
               time_unit="The model's BASE unit: seconds, minutes, hours or days.",
               currency="Cost label only; never enters the arithmetic.")


@tool()
def set_simulation_settings(model: str, run_length: float | None = None,
                            warmup: float | None = None, replications: int | None = None,
                            time_unit: str | None = None, currency: str | None = None) -> dict:
    """Set the model's default run settings; only the arguments you pass change.

    ALWAYS set a run length for a model that cannot drain on its own -- one with
    an endless CREATE (max_arrivals 0), a failure, or a repeating capacity
    schedule. Without it a run raises rather than hanging.

    Returns the usual edit answer plus {"simulation": {...}}.
    """
    doc, before = _open(model)
    changes = {}
    if run_length is not None:
        changes["time_limit"] = run_length
    if warmup is not None:
        changes["warmup"] = warmup
    if replications is not None:
        changes["replications"] = int(replications)
    if time_unit is not None:
        changes["time_unit"] = time_unit
    if currency is not None:
        changes["currency"] = currency
    if changes:
        doc.set_simulation(**changes)
    return _edit_result(doc, before, {"simulation": doc.simulation})


_document_args("set_sd_settings", model="Workspace name of the model to edit.",
               dt="Step of the system-dynamics grid, > 0; null removes the grid.",
               method="euler (Stella/Vensim default) or rk4 (fourth order; no random draws "
                      "in a dynamic rate).",
               time_unit="Unit dt is in: base (the model's own) or seconds/minutes/hours/days.")


@tool()
def set_sd_settings(model: str, dt: float | None, method: str = "euler",
                    time_unit: str = "base") -> dict:
    """Turn on the system-dynamics step grid, change it, or remove it (dt null).

    Without it a flow rate is re-evaluated only at events, which is exact for
    rates that change only then. A rate that reads LEVEL(), FRACTION() or TNOW
    -- dL/dt = -k*L, logistic growth, an SIR model -- needs the grid: it is
    re-integrated every dt with Euler or RK4, the way Stella and Vensim do.
    Static rates are untouched, and events inside a step stay exact.

    Returns the usual edit answer plus {"sd": {...} or null}.
    """
    doc, before = _open(model)
    spec = None
    if dt is not None:
        spec = {"dt": float(dt)}
        if method != "euler":
            spec["method"] = method
        if time_unit != "base":
            spec["time_unit"] = time_unit
    doc.set_sd_settings(spec)
    return _edit_result(doc, before, {"sd": doc.simulation.get("sd")})


@tool()
def auto_layout(model: str) -> dict:
    """Lay the model's blocks out left to right in a deterministic layered
    arrangement, one undo entry.

    Purely cosmetic -- it changes no behaviour and no KPI. Call it once after
    building a model so a human opening it in the editor sees a readable
    diagram instead of a pile of blocks.

    Returns the usual edit answer plus {"moved": <block count>}.
    """
    doc, before = _open(model)
    positions = layout_tools.auto_layout(doc)
    doc.move_blocks(positions)
    return _edit_result(doc, before, {"moved": len(positions)})
