"""Block/link editing tools plus model-level settings (F2: model/blocks/links).

Adding, wiring and removing blocks; resources; the calendar; entity types;
simulation and system-dynamics settings; `auto_layout`. Everything here
mutates through `.core._open`/`_edit_result`, exactly like the rest of the
package.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Literal

from nagasim.core import conveyor_checks, conveyor_look, conveyor_path, floor, layout_tools
from nagasim.core.conveyor_types import TYPES
from nagasim.core.timeunits import LENGTH_UNITS, UNITS, units_of

from .core import (EDIT, ToolError, _document_args, _duration, _edit_result, _open, _problem_dicts,
                   _problems, tool, workspace)

_document_args("add_block",
               model="Workspace name of the model to edit.",
               type="Block type, e.g. \"CREATE\", \"PROCESS\", \"DISPOSE\" (see list_block_types).",
               name="Block name; auto-generated (create_1, process_2, ...) when omitted.",
               params="Parameter values to set on the block; the type's defaults fill the rest.",
               position="[x, y] canvas centre in scene units; auto-placed in a row when omitted.")


@tool(returns={**EDIT, "block": str})
def add_block(model: str, type: str, name: str | None = None, params: dict | None = None,
              position: list[float] | None = None) -> dict:
    """Add one block to a model and optionally set its parameters in the same
    step (one undo entry).

    The normal way to build a model: add CREATE, the processing blocks, and
    DISPOSE, then `connect` them. Position is cosmetic -- call `auto_layout`
    once at the end instead of placing blocks by hand.

    A wrong param name or type comes back in "introduced" with code E_PARAM_UNKNOWN /
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


_LOOK_DOC = ("Conveyor look fields to set, sizes in metres: width, height, frame_color, "
             "leg_spacing, drive_side, guide_left and guide_right [0: none], guide_style, "
             "roller_pitch, roller_diameter, roller_finish, belt_kind, pulley_diameter, "
             "chain_strands, strand_spacing; openings [{side, start, end}] in the belt's length "
             "unit.")
_SCOPE_DOC = ("\"chain\": the look spreads along the piece's line (place_piece: the line it "
              "joins), skipping other pieces' fields set by hand; \"piece\": this piece only, "
              "kept by hand until reset. Openings never spread.")

_document_args("update_block",
               model="Workspace name of the model to edit.",
               name="Current name of the block to change.",
               params="Parameters to merge into the block's existing params.",
               description="New canvas label.",
               position="New [x, y] centre.",
               new_name="Rename the block to this; every link follows the rename.",
               look=_LOOK_DOC,
               scope=_SCOPE_DOC,
               reset="Look fields of this piece only to put back in line: each takes the nearest "
                     "value upstream not set by hand, else downstream, else the default.")

_LOOK_RETURNS = {"look_changed": (list, "pieces the look changed"),
                 "look_skipped": (dict, "piece -> fields it keeps by hand")}


def _look(doc, name: str, look, scope: str, reset) -> dict:
    """Apply a look write inside the caller's transaction: the answer's two look keys, or
    E_LOOK naming the refused fields (the transaction then rolls the whole call back)."""
    if look is None and not reset:
        return {}
    try:
        done = conveyor_look.apply_look(doc, name, look, scope, reset or ())
    except ValueError as exc:
        fields = conveyor_look.fields_of(doc.blocks[name].params.get("type"))
        raise ToolError("E_LOOK", str(exc), f"settings: {', '.join(fields)}, openings"
                        if fields else "only a CONVEYOR with a type has a look")
    return {"look_changed": done.changed, "look_skipped": done.skipped}


@tool(returns={**EDIT, "block": str, **_LOOK_RETURNS})
def update_block(model: str, name: str, params: dict | None = None,
                 description: str | None = None, position: list[float] | None = None,
                 new_name: str | None = None, look: dict | None = None,
                 scope: Literal["chain", "piece"] = "chain",
                 reset: list[str] | None = None) -> dict:
    """Change an existing block: merge parameters, rename it, move it, or
    relabel it -- all as one undo entry.

    `params` MERGES rather than replaces, so you can set one value without
    restating the rest. Renaming rewires every link automatically. A conveyor's look
    is set with `look`, never in `params`; `get_ports` reads it back. Batch a large
    build through `apply_edits`.
    """
    doc, before = _open(model)
    if name not in doc.blocks:
        raise ToolError("E_UNKNOWN_BLOCK", f"no block named '{name}' in model '{model}'",
                        f"blocks: {', '.join(doc.blocks) or '(none)'}")
    # A shallow merge would replace the openings and skip the spreading.
    inside = [k for k in ("look", "look_set") if k in (params or {})]
    if inside:
        raise ToolError("E_LOOK", f"{', '.join(inside)} cannot be set inside params",
                        "set looks with look=..., scope=...")
    final = name
    with doc.transaction():
        if params:      # first: a type change decides which look fields exist
            doc.set_params(name, {**doc.blocks[name].params, **params})
        if description is not None:
            doc.set_description(name, description)
        if position is not None:
            doc.move_blocks({name: (float(position[0]), float(position[1]))})
        if new_name and new_name != name:
            doc.rename_block(name, new_name)
            final = new_name
        extra = _look(doc, final, look, scope, reset)     # after the rename: the answer names it
    return _edit_result(doc, before, {"block": final, **extra})


_document_args("remove_blocks",
               model="Workspace name of the model to edit.",
               names="Block names to delete; their links go with them.")


@tool(returns={**EDIT, "removed": list})
def remove_blocks(model: str, names: list[str]) -> dict:
    """Delete blocks and every link touching them, as one undo entry.

    Unknown names are ignored rather than refused, so removing an already-gone
    block is not an error. Expect new problems if you cut the flow in half --
    the answer says which.
    """
    doc, before = _open(model)
    removed = [n for n in names if n in doc.blocks]
    doc.remove_blocks(removed)
    return _edit_result(doc, before, {"removed": removed})


_document_args("connect",
               model="Workspace name of the model to edit.",
               source="Name of the block the entity leaves.",
               target="Name of the block the entity arrives at.",
               source_port="Out port name: a SUBMODEL instance's port, or a CONVEYOR's exit (\"out\", \"d<k>\").",
               target_port="In port name: a SUBMODEL instance's port, or a CONVEYOR's \"in\".",
               delay="Optional transfer delay, e.g. {\"distribution\": \"Constant\", "
                     "\"dist_params\": {\"value\": 2}}.",
               capacity="Parts a link with a delay holds at once (a full link holds its "
                        "feeder); 0 = no limit.")


@tool(returns={**EDIT, "link": str, "index": float})
def connect(model: str, source: str, target: str, source_port: str | None = None,
            target_port: str | None = None, delay: dict | None = None,
            capacity: int | None = None) -> dict:
    """Link two blocks so entities flow from *source* to *target*, optionally
    with a transfer delay.

    Links are ORDERED: the n-th link you add out of a DECIDE is its branch n,
    matching `probs[n]` / `conditions[n]`. Add branches in the order you want
    them, or fix the order afterwards with `reorder_branches`. Only DECIDE and
    LOGIC may have more than one outgoing link.

    A branching block may send TWO branches to the same target (a DECIDE where
    two cases end the same way): the second call returns `index` 1, which
    `disconnect` takes to tell them apart.
    """
    doc, before = _open(model)
    with doc.transaction():
        index = doc.add_link(source, target, source_port=source_port, target_port=target_port)
        if delay:
            doc.set_link_delay(source, target, _duration(delay, "delay"), index=index)
        if capacity is not None:
            doc.set_link_area(source, target, {"capacity": capacity}, index=index)
    return _edit_result(doc, before, {"link": f"{source} -> {target}", "index": index})


_document_args("disconnect", model="Workspace name of the model to edit.",
               source="Source block of the link to remove.",
               target="Target block of the link to remove.",
               index="Which of several parallel links between the pair, 0 first; "
                     "only needed when there is more than one.")


@tool(returns={**EDIT, })
def disconnect(model: str, source: str, target: str, index: int | None = None) -> dict:
    """Remove one link. A link that does not exist is a no-op, not an error.

    Use it to rewire a flow -- for example to insert a new block between two
    existing ones: disconnect a -> c, then connect a -> b and b -> c.

    When several parallel links join the pair, name which one with *index*
    (0 is the first); leaving it out is then an error listing the candidates.
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


@tool(returns={**EDIT, "order": list})
def reorder_branches(model: str, source: str, order: list[str]) -> dict:
    """Set the branch order of a block's outgoing links.

    Branch order is what binds a DECIDE's `probs[n]` / `conditions[n]` to a
    target, so getting it wrong sends entities down the wrong path without any
    validation error. *order* must list exactly the current targets of *source*,
    permuted.
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


@tool(returns={**EDIT, "resources": list})
def set_resources(model: str, resources: list[dict]) -> dict:
    """REPLACE the model's whole resource table.

    For one resource, `upsert_record`; this one sets several at once, or
    deletes one by leaving it out. A block's `resource_name` may name a resource or a resource set --
    they share one namespace.
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


@tool(returns={**EDIT, "calendar": dict | None})
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


@tool(returns={**EDIT, "entities": list})
def set_entity_types(model: str, entities: list[dict]) -> dict:
    """REPLACE the model's entity-type table.

    Entity types name the things flowing through the model (CREATE's `e_type`)
    and carry the per-type WIP `holding_cost` used by the cost KPIs. A model
    with no declared types still runs: everything is "DEFAULT".
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
               currency="Cost label only; never enters the arithmetic.",
               strict_checks="true: conveyor floor checks and run guards are errors, not warnings.")


@tool(returns={**EDIT, "simulation": dict})
def set_simulation_settings(model: str, run_length: float | None = None,
                            warmup: float | None = None, replications: int | None = None,
                            time_unit: str | None = None, currency: str | None = None,
                            strict_checks: bool | None = None) -> dict:
    """Set the model's default run settings; only the arguments you pass change.

    ALWAYS set a run length for a model that cannot drain on its own -- one with
    an endless CREATE (max_arrivals 0), a failure, or a repeating capacity
    schedule. Without it a run raises rather than hanging.
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
    if strict_checks is not None:
        changes["strict_checks"] = bool(strict_checks)
    if changes:
        doc.set_simulation(**changes)
    return _edit_result(doc, before, {"simulation": doc.simulation})


_document_args("set_sd_settings", model="Workspace name of the model to edit.",
               dt="Step of the system-dynamics grid, > 0; null removes the grid.",
               method="euler (Stella/Vensim default) or rk4 (fourth order; no random draws "
                      "in a dynamic rate).",
               time_unit="Unit dt is in: base (the model's own) or seconds/minutes/hours/days.")


@tool(returns={**EDIT, "sd": dict | None})
def set_sd_settings(model: str, dt: float | None, method: str = "euler",
                    time_unit: str = "base") -> dict:
    """Turn on the system-dynamics step grid, change it, or remove it (dt null).

    Without it a flow rate is re-evaluated only at events, which is exact for
    rates that change only then. A rate that reads LEVEL(), FRACTION() or TNOW
    -- dL/dt = -k*L, logistic growth, an SIR model -- needs the grid: it is
    re-integrated every dt with Euler or RK4, the way Stella and Vensim do.
    Static rates are untouched, and events inside a step stay exact.
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


@tool(returns={**EDIT, "moved": int})
def auto_layout(model: str) -> dict:
    """Lay the model's blocks out left to right in a deterministic layered
    arrangement, one undo entry.

    Purely cosmetic -- it changes no behaviour and no KPI. Call it once after
    building a model so a human opening it in the editor sees a readable
    diagram instead of a pile of blocks.
    """
    doc, before = _open(model)
    positions = layout_tools.auto_layout(doc)
    doc.move_blocks(positions)
    return _edit_result(doc, before, {"moved": len(positions)})


_document_args("set_item_code", model="Workspace name of the model to edit.",
               definition="Name of the submodel definition whose code this is.",
               source="The whole Python source: a module defining the class.",
               class_name="The class in source the item runs, one instance per placed item.",
               timer="Run on_timer every this many model time units; null for no timer.",
               watch="Model variables whose writes trigger on_state.",
               signals="Signal names that trigger on_signal; $name is per instance.")


@tool(returns={**EDIT, "fingerprint": str, "trusted": bool, "problems": list})
def set_item_code(model: str, definition: str, source: str, class_name: str,
                  timer: float | None = None, watch: list[str] | None = None,
                  signals: list[str] | None = None) -> dict:
    """Write the Python class a submodel definition runs (its item code), one undo step.

    Hooks, each taking item last: on_entry gets entity and returns the output index (0 first);
    on_exit gets entity; on_timer; on_state gets variable and value; on_signal gets name.
    LOGIC blocks with hook entry or exit inside the definition run the first two, and
    item.params holds the instance's arguments.

    The code runs only after a person trusts this exact code on their computer, in the
    editor's code panel or with `naga trust`. You cannot trust code, and any change to it
    needs the person's yes again. Once trusted, your runs and experiments run it.

    "problems" lists what to fix in the code or its wiring.
    """
    from nagasim.core import plainlines
    from nagasim.settings import itemtrust

    doc, before = _open(model)
    if definition not in doc.submodels:
        raise ToolError("E_UNKNOWN_SUBMODEL", f"no submodel definition named '{definition}'",
                        "get_model_summary lists the definitions; write_model can add one")
    current = doc.definition_code(definition)
    code = {"class": class_name, "source": source, "timer": timer,
            "watch": list(watch or []), "signals": list(signals or [])}
    # Only library placing writes `placed`; an edit keeps it, so the edit shows as one.
    if current and isinstance(current.get("placed"), dict):
        code["placed"] = current["placed"]
    doc.set_definition_code(definition, code)
    fp = itemtrust.fingerprint(source)
    mine = [p for p in _problems(doc) if p.code in ("E_ITEM_CODE", "E_ITEM_HOOK")
            and ((p.fields or {}).get("definition") == definition
                 or p.path.startswith(f"/submodels/{definition}/"))]
    return _edit_result(doc, before, {"fingerprint": fp, "trusted": itemtrust.is_trusted(fp),
                                      "problems": [plainlines.plain(p) for p in mine]})


def _model_units(doc):
    """The model's units, or E_UNIT when its time or length unit is one this build does not
    know (#2011: `write_model` keeps a draft with a bad unit so its errors can be listed, and
    the floor tools then died on a KeyError)."""
    units = units_of(doc.simulation)
    for what, name, known in (("length_unit", units.length, LENGTH_UNITS),
                              ("time_unit", units.time, UNITS)):
        if name not in known:
            raise ToolError("E_UNIT", f"simulation.{what} '{name}' is not a unit this build knows",
                            f"set simulation.{what} to one of: {', '.join(known)}")
    return units


_document_args("place_piece",
               model="Workspace name of the model to edit.",
               name="Name of the new belt piece.",
               segments="The belt's pieces, entry to end (the CONVEYOR `path` param): {\"straight\": length} or {\"arc\": "
                    "{\"radius\": r, \"angle\": degrees, \"turn\": \"left\"|\"right\"}}.",
               type="Conveyor type: \"\" (untyped), \"belt\", \"roller\", \"chain\", \"pallet\" or "
                    "\"mdr\". A typed piece gets the type's datasheet speed in its params.",
               x="Floor-plan x of the belt's entry, in metres; ignored with snap_to.",
               y="Floor-plan y (south is +y), in metres; ignored with snap_to.",
               rotation="Direction of travel at the entry, degrees clockwise on screen (0 east, "
                        "90 south), like set_layout; ignored with snap_to.",
               snap_to="\"<block>.<port>\" of a belt or transfer exit (\"A.out\", \"A.d0\", "
                       "\"X.left\"): the piece starts at that port, facing its way, and the two "
                       "are linked.",
               speed="Belt speed, model length per time unit; default: the type's datasheet speed "
                     "(untyped: block default).",
               gap="With snap_to: metres of floor between the port and the piece's entry, along "
                   "the port's heading; above 0 it needs a delay (an area link).",
               delay="With snap_to: the link's transfer delay, as connect takes it; required "
                     "when gap is above 0.",
               capacity="With snap_to: parts the delayed link holds at once, as connect takes "
                        "it; 0 = no limit. Needs delay.",
               look=_LOOK_DOC,
               scope=_SCOPE_DOC)

_PATH_HINT = ("segments: [{\"straight\": 2.0}, "
              "{\"arc\": {\"radius\": 1.0, \"angle\": 90, \"turn\": \"left\"}}]")


def _piece_kind(type: str, speed, segments) -> str:
    """The conveyor type of a new belt piece, or the ToolError for a bad type, speed or path."""
    if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))
                              or not speed > 0):
        raise ToolError("E_PLACE_SPEED", f"speed {speed!r} is not a number above 0",
                        "give the speed in the model's length per time unit, or leave it out")
    kind = str(type or "").strip().lower()
    if kind and kind not in TYPES:
        raise ToolError("E_PLACE_TYPE", f"'{type}' is not a conveyor type",
                        f"type is \"\" or one of {', '.join(sorted(TYPES))}")
    try:
        conveyor_path.parse_path(segments)
    except ValueError as exc:
        raise ToolError("E_PLACE_PATH", str(exc), _PATH_HINT)
    return kind


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _merge_hint(trunk_params: dict) -> str:
    """The hint for a refused merge point: no `at` helps on a trunk that is zoned, reversible or
    not accumulating (the engine refuses a merge there), so say what to change instead."""
    if (trunk_params.get("zones") or trunk_params.get("reversible")
            or trunk_params.get("accumulating") is False):
        return ("no `at` works on this trunk: merge into another belt, or remove its zones or "
                "reversible flag, or make it accumulating")
    return "pick another at, strictly inside the trunk and off its other points"


def _gap_link(snap_to: str, gap, delay, capacity):
    """(gap, delay, capacity) of `place_piece`'s link, checked, or the E_PLACE_GAP saying why
    not. A gap needs a delay: an undelayed link between pieces that do not touch would move
    parts across the gap in no time (plan B: a delayed link is never a joint)."""
    if not _is_number(gap) or gap < 0:
        raise ToolError("E_PLACE_GAP", f"gap {gap!r} is not a number of metres of 0 or more",
                        "give the gap in metres, or leave it at 0")
    if (gap > 0 or delay is not None or capacity is not None) and not snap_to:
        raise ToolError("E_PLACE_GAP", "gap, delay and capacity set the snapped link, and no "
                        "snap_to is given", "give snap_to \"<block>.<port>\" of the belt end "
                        "the piece follows")
    if capacity is not None and not delay:
        raise ToolError("E_PLACE_GAP", "capacity limits a delayed link, and no delay is given",
                        "give delay too, or leave capacity out")
    if gap > 0 and not delay:
        raise ToolError("E_PLACE_GAP", f"a gap of {gap:g} m needs a delay to carry parts "
                        "across it", "give delay, e.g. {\"distribution\": \"Constant\", "
                        "\"dist_params\": {\"value\": 2}}")
    if capacity is not None and (not isinstance(capacity, int) or isinstance(capacity, bool)
                                 or capacity < 0):
        raise ToolError("E_PLACE_GAP", f"capacity {capacity!r} is not a whole number of 0 or "
                        "more", "give the parts the link holds at once; 0 = no limit")
    return float(gap), (_duration(delay, "delay") if delay else None), capacity


@tool(returns={**EDIT, "block": str, "layout_position": list, "layout_rotation": float,
               **_LOOK_RETURNS})
def place_piece(model: str, name: str, segments: list[dict], type: str = "", x: float = 0.0,
                y: float = 0.0, rotation: float = 0.0, snap_to: str = "",
                speed: float | None = None, gap: float = 0.0, delay: dict | None = None,
                capacity: int | None = None, look: dict | None = None,
                scope: Literal["chain", "piece"] = "chain") -> dict:
    """Add one belt piece on the floor plan: a CONVEYOR whose `path` is *segments*, starting at
    (x, y) and travelling at *rotation*.

    With *snap_to* (\"A.out\", \"A.d0\", or a transfer exit \"X.left\") the piece starts at that
    port, facing its way, and a link with `source_port` / `target_port` is added, so no
    coordinates are computed by hand. One undo entry. Segment lengths and radii are in the
    model's length unit. A snapped piece takes the look of the piece it snaps to, then *look*.
    Batch a large build through `apply_edits`.
    """
    doc, before = _open(model)
    _model_units(doc)
    kind = _piece_kind(type, speed, segments)
    gap, delay, capacity = _gap_link(snap_to, gap, delay, capacity)
    source = None
    if snap_to:
        source = _snap_port(doc, model, snap_to)
        x, y, rotation = source.pose
        x += gap * math.cos(math.radians(rotation))
        y += gap * math.sin(math.radians(rotation))
    x, y = round(float(x), 6), round(float(y), 6)
    with doc.transaction():
        created = floor.add_belt_piece(doc, segments, kind, (x, y, rotation), source, speed,
                                          name)
        if delay:
            doc.set_link_delay(source.block, created, delay)
        if capacity is not None:
            doc.set_link_area(source.block, created, {"capacity": capacity})
        extra = _look(doc, created, look, scope, None)
    return _edit_result(doc, before, {
        "block": created, "layout_position": [x, y],
        "layout_rotation": doc.blocks[created].layout_rotation, **extra})


_document_args("place_feeder",
               model="Workspace name of the model to edit.",
               name="Name of the new feeder belt.",
               segments="The feeder's pieces, entry to end, in the model's length unit, as "
                        "place_piece takes them; its end lands on the trunk.",
               trunk="The CONVEYOR (drawn on the floor) the feeder merges into.",
               at="Where on the trunk, from its entry, in the trunk's length unit (the merge "
                  "entry's `at`).",
               side="\"left\" or \"right\" of the trunk's direction of travel: the "
                    "side the feeder comes from.",
               angle="Degrees between the trunk's travel there and the feeder's, in (0, 90]: 90 "
                     "is square on, less leans the feeder downstream, as a divert's angle.",
               type="Conveyor type of the feeder, as place_piece takes it.",
               speed="Feeder speed, as place_piece takes it.")


@tool(returns={**EDIT, "block": str, "layout_position": list, "layout_rotation": float})
def place_feeder(model: str, name: str, segments: list[dict], trunk: str, at: float,
                 side: str = "left", angle: float = 90.0, type: str = "",
                 speed: float | None = None) -> dict:
    """Add a belt piece whose end merges into belt *trunk*'s side at *at*: a CONVEYOR placed so
    that its path ends on the trunk's centreline there, plus the trunk's merge entry and the
    link from the feeder, in one undo entry.

    The start pose is solved from *segments*, so no coordinates are computed by hand.
    """
    doc, before = _open(model)
    units = _model_units(doc)
    found = floor.floor_geometry(doc, trunk)
    if found is None:
        names = [n for n, b in doc.blocks.items() if b.type == "CONVEYOR"
                 and floor.floor_geometry(doc, n) is not None]
        raise ToolError("E_UNKNOWN_BLOCK", f"no CONVEYOR named '{trunk}' is drawn on the floor "
                        f"of model '{model}'", f"belts: {', '.join(names[:5]) or '(none)'}")
    kind = _piece_kind(type, speed, segments)
    if name in doc.blocks:
        raise ToolError("E_PLACE_NAME", f"a block named '{name}' already exists in model "
                        f"'{model}'", "give the feeder a name no block has")
    if not segments:
        raise ToolError("E_PLACE_PATH", "a feeder needs at least one segment", _PATH_HINT)
    if not _is_number(angle) or not 0.0 < angle <= 90.0:
        raise ToolError("E_PLACE_ANGLE", f"angle {angle!r} is not above 0 and at most 90",
                        "90 is square on; less leans the feeder downstream")
    if side not in ("left", "right"):
        raise ToolError("E_PLACE_ANGLE", f"side {side!r} is not \"left\" or \"right\"",
                        "the side of the trunk's travel the feeder comes from")
    if not _is_number(at):
        raise ToolError("E_PLACE_MERGE", f"at {at!r} is not a number", "a distance from the "
                        "trunk's entry, in its length unit")
    reason = floor.point_refusal(doc, trunk, "merges", float(at), source=name or None)
    if reason is not None:
        raise ToolError("E_PLACE_MERGE", reason, _merge_hint(doc.blocks[trunk].params or {}))
    there = floor.merge_point(doc, trunk, float(at))
    # The feeder travels *angle* off the trunk's tangent, towards it from *side*: from the left
    # its heading is the trunk's plus the angle (a left divert's exit, leaving the trunk, is
    # the trunk's minus it).
    heading = there.heading + (angle if side == "left" else -angle)
    # Its path laid from the origin heading 0 ends at (ex, ey, eh): turn it by heading - eh and
    # shift it so that end lands on the merge point.
    from nagasim.core.blocks.conveyor import belt_geometry
    _origin, own, _ = belt_geometry({"params": {"path": segments}}, units)
    end = conveyor_path.end_pose(conveyor_path.Pose(0.0, 0.0, 0.0), own)
    h0 = heading - end.heading
    c, s = math.cos(math.radians(h0)), math.sin(math.radians(h0))
    x = round(there.x - (end.x * c - end.y * s), 6)
    y = round(there.y - (end.x * s + end.y * c), 6)
    with doc.transaction():
        created = floor.add_belt_piece(doc, segments, kind, (x, y, round(h0, 6)), None,
                                          speed, name)
        params = doc.blocks[trunk].params or {}
        doc.set_params(trunk, {**params, "merges": list(params.get("merges") or [])
                               + [{"at": float(at), "from": created}]})
        # No target port: a merge's link enters at the merge point, not "in" (as the editor).
        doc.add_link(created, trunk, source_port="out")
    return _edit_result(doc, before, {
        "block": created, "layout_position": [x, y],
        "layout_rotation": doc.blocks[created].layout_rotation})


_document_args("set_layout",
               model="Workspace name of the model to edit.",
               block="Name of the block to place.",
               x="Floor-plan x, in metres.",
               y="Floor-plan y (south is +y), in metres.",
               rotation="Degrees clockwise on screen (0 east, 90 south); a belt travels this "
                        "way from its entry.")


@tool(returns={**EDIT, "block": str, "dropped_links": list, "dropped_merges": list})
def set_layout(model: str, block: str, x: float, y: float, rotation: float) -> dict:
    """Place a block on the floor plan: its position and rotation, one undo entry. Cosmetic for
    most blocks; a CONVEYOR's path starts from this pose. A belt piece or transfer moves as in
    the editor: the joins and merges it no longer meets are dropped and listed."""
    doc, before = _open(model)
    if block not in doc.blocks:
        raise ToolError("E_UNKNOWN_BLOCK", f"no block named '{block}' in model '{model}'",
                        f"blocks: {', '.join(doc.blocks) or '(none)'}")
    piece = conveyor_checks.floor_piece(doc.blocks[block].type, doc.blocks[block].params)
    links_before, merges_before = Counter(_link_pairs(doc)), _merge_rows(doc)
    with doc.transaction():
        # The editor's one move rule (O10), without capture as a nudge: the pose is the agent's.
        floor.move_floor(doc, {block: (float(x), float(y))}, {block: float(rotation)})
    if not piece:
        return _edit_result(doc, before, {"block": block})
    # Read off the file, not off the rule's steps: a join dropped and made again at the same
    # ports is no change. A dropped merge's feeder link is listed with its merge only.
    merges_after = _merge_rows(doc)
    merges = [m for m in merges_before if m not in merges_after]
    gone = {(m["from"], m["trunk"]) for m in merges}
    lost = links_before - Counter(_link_pairs(doc))
    return _edit_result(doc, before, {
        "block": block, "dropped_merges": merges,
        "dropped_links": [f"{s} -> {t}" for (s, t), n in lost.items() if (s, t) not in gone
                          for _ in range(n)]})


def _link_pairs(doc) -> list[tuple[str, str]]:
    return [(link.source, link.target) for link in doc.link_records()]


def _merge_rows(doc) -> list[dict]:
    return [{"trunk": name, "from": m.get("from"), "at": m.get("at")}
            for name, b in doc.blocks.items() if b.type == "CONVEYOR"
            for m in (b.params or {}).get("merges") or [] if isinstance(m, dict)]


def _snap_port(doc, model: str, snap_to: str):
    """The out port a `snap_to` of "<block>.<port>" names (a belt's out or divert, a transfer's
    exit), or the ToolError saying why not."""
    block, _, port = snap_to.rpartition(".")
    form = "snap_to is \"<block>.<port>\", e.g. \"A.out\" or \"X.left\""
    if not block or not port:
        raise ToolError("E_UNKNOWN_PORT", f"snap_to {snap_to!r} is not <block>.<port>", form)
    if block not in doc.blocks:
        raise ToolError("E_UNKNOWN_BLOCK", f"snap_to: no block named '{block}' in model "
                        f"'{model}'", form)
    kind = doc.blocks[block].type
    if kind in ("CONVEYOR", "CONVEYOR_TRANSFER"):
        ports = floor.piece_ports(doc, block)
    else:
        raise ToolError("E_UNKNOWN_BLOCK", f"snap_to: block '{block}' is a {kind}, not a "
                        "CONVEYOR or CONVEYOR_TRANSFER", form)
    found = [p for p in ports if p.name == port and p.role == "out"]
    if not found:
        names = [p.name for p in ports if p.role == "out"]
        raise ToolError("E_UNKNOWN_PORT", f"snap_to: '{block}' has no out port '{port}'",
                        f"out ports: {', '.join(names)}")
    return found[0]


_document_args("place_transfer",
               model="Workspace name of the model to edit.",
               name="Name of the new transfer module.",
               mechanism="\"popup\" (default), \"turntable\" or \"lift\".",
               exits="The module's exits, in exit order: [{\"port\": name, \"angle\": degrees from "
                     "the entry heading, clockwise on screen}]. Empty: one straight exit \"out\".",
               x="Floor x, metres; ignored with snap_to.",
               y="Floor y (south +), metres; ignored with snap_to.",
               rotation="Heading of the entry, degrees clockwise on screen (0 east, 90 south); "
                        "ignored with snap_to.",
               snap_to="\"<belt>.<port>\" (\"A.out\"): the module takes that port's pose and "
                       "the belt is linked to it.")


@tool(returns={**EDIT, "block": str, "layout_position": list, "layout_rotation": float})
def place_transfer(model: str, name: str, mechanism: str = "popup", exits: list[dict] | None = None,
                   x: float = 0.0, y: float = 0.0, rotation: float = 0.0,
                   snap_to: str = "") -> dict:
    """Add a CONVEYOR_TRANSFER (moves each load onto one of its exits) on the floor plan, in one
    undo entry.

    Its exits are ports: `place_piece` the belt that takes each with snap_to "<module>.<exit>",
    or link an existing one with `connect(source=<module>, target=<belt>, source_port=<exit>)`.
    The belt feeding the module is linked by *snap_to* or `connect`. `get_ports` lists the ports.
    """
    doc, before = _open(model)
    _model_units(doc)
    from nagasim.core.blocks import transfer_piece
    kind = str(mechanism or "popup").strip().lower()
    if kind not in transfer_piece.MECHANISMS:
        raise ToolError("E_PLACE_TYPE", f"'{mechanism}' is not a transfer mechanism",
                        f"mechanism is one of {', '.join(transfer_piece.MECHANISMS)}")
    listed = exits or [{"port": "out", "angle": 0.0}]
    ports = [e.get("port") if isinstance(e, dict) else None for e in listed]
    if any(not isinstance(p, str) or not p.strip() or ":" in p for p in ports) \
            or len(set(ports)) != len(ports) \
            or any(isinstance(e.get("angle"), bool) or not isinstance(e.get("angle"), (int, float))
                   for e in listed):
        raise ToolError("E_PLACE_EXITS", f"exits {listed!r} is not a list of distinct "
                        "{port, angle} entries", "exits: [{\"port\": \"left\", \"angle\": -90}, "
                        "{\"port\": \"straight\", \"angle\": 0}]")
    source = None
    if snap_to:
        source = _snap_port(doc, model, snap_to)
        x, y, rotation = source.pose
    x, y = round(float(x), 6), round(float(y), 6)
    created = floor.add_transfer_piece(doc, kind, listed, (x, y, rotation), source, name)
    return _edit_result(doc, before, {
        "block": created, "layout_position": [x, y],
        "layout_rotation": doc.blocks[created].layout_rotation})


_document_args("get_ports",
               model="Workspace name of the model.",
               block="Name of a CONVEYOR or CONVEYOR_TRANSFER.")


@tool(read_only=True, returns={"block": str, "type": str, "ports": list, "look": dict,
                               "look_set": list, "chain": list})
def get_ports(model: str, block: str) -> dict:
    """The ports of a belt piece or transfer module: each one's name, role (in/out), floor
    pose (x, y metres, heading as in set_layout) and `linked_to`, the "Block.port" ends it is
    linked to.

    A belt has "in", "out" and "d<k>" (divert k's exit); a pose faces the way a part leaves. A
    transfer has no footprint: an exit's pose is the module's position, heading = entry plus
    the exit's angle. Pass an out port to `place_piece` as snap_to. A belt's answer also holds
    `look`, every field resolved; `look_set`, the fields set by hand; and `chain`, the pieces a
    look spreads along.
    """
    doc = workspace().document(model)
    _model_units(doc)
    layout = doc.to_layout()
    entry = next((b for b in layout["blocks"] if b.get("name") == block), None)
    if entry is None:
        names = [b["name"] for b in layout["blocks"]]
        more = f", and {len(names) - 5} more" if len(names) > 5 else ""
        raise ToolError("E_UNKNOWN_BLOCK", f"no block named '{block}' in model '{model}'",
                        f"blocks: {', '.join(names[:5]) or '(none)'}{more}")
    kind = str(entry.get("type", "")).upper()
    if kind not in ("CONVEYOR", "CONVEYOR_TRANSFER"):
        raise ToolError("E_NOT_A_PIECE", f"block '{block}' is a {kind}, not a CONVEYOR or "
                        "CONVEYOR_TRANSFER", "get_ports reads belt pieces and transfer modules")
    params = entry.get("params") or {}
    links = [k for k in layout.get("links") or [] if isinstance(k, dict)]
    taken = conveyor_checks.exit_links(entry, [k for k in links if k.get("source") == block])
    merges = {m.get("from") for m in params.get("merges") or [] if isinstance(m, dict)}
    by_name = {b.get("name"): b.get("params") or {} for b in layout["blocks"]}
    feeders = sorted(f"{k['source']}.{k.get('source_port') or 'out'}" for k in links
                     if k.get("target") == block and k.get("source") not in merges
                     and k.get("target_port") in (None, "", "in"))

    def joined(port: str, role: str) -> list[str]:
        if role == "in":
            return feeders
        link = taken.get(port)
        if not link:
            return []
        # A merge link enters the trunk's side at a point, not at its "in" (D1423).
        merge = next((m for m in by_name.get(link["target"], {}).get("merges") or []
                      if isinstance(m, dict) and m.get("from") == block), None)
        if merge is not None:
            return [f"{link['target']} (merge at {merge.get('at')})"]
        return [f"{link['target']}.{link.get('target_port') or 'in'}"]

    found = (floor.piece_ports(doc, block) if kind == "CONVEYOR" else
             conveyor_path.transfer_ports(block, entry.get("layout_position"),
                                          entry.get("layout_rotation"), params))
    out = {"block": block, "type": kind, "ports": [
        {"name": p.name, "role": p.role, "x": round(p.pose.x, 6), "y": round(p.pose.y, 6),
         "heading": round(p.pose.heading, 6) % 360.0, "linked_to": joined(p.name, p.role)}
        for p in found]}
    if kind == "CONVEYOR":
        own = doc.blocks[block].params
        out.update(look=conveyor_look.look_of(own), look_set=conveyor_look.look_set_of(own),
                   chain=conveyor_look.chain_of(doc, block))
    return out


@tool(read_only=True, returns={"model": str, "ok": bool, "warnings": int, "problems": list,
                               "lines": list})
def check_layout(model: str) -> dict:
    """Check the floor layout of belt pieces and transfer modules: open ports
    (W_CONVEYOR_OPEN_PORT), overlaps (W_CONVEYOR_OVERLAP), linked ports that do not meet
    (W_CONVEYOR_SNAP) and items under three roller pitches long (W_CONVEYOR_ROLLER_PITCH,
    never an error).

    The same problems `validate_model` lists for them (errors under `strict_checks`), with a
    plain sentence each in `lines`. `ok` is true when there are none.
    """
    from nagasim.core import plainlines
    doc = workspace().document(model)
    _model_units(doc)
    found = conveyor_checks.static_checks(doc.to_layout())
    return {"model": model, "ok": not found,
            "warnings": sum(1 for p in found if p.severity == "warning"),
            "problems": _problem_dicts(found), "lines": [plainlines.plain(p) for p in found]}
