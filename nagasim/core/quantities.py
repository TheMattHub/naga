"""Quantities of model fields beyond block params, and the one converter
(units spec, D159-D161).

`SECTION_FIELDS` describes the columns of the model file's sections with the
same `ParamSpec` a block param uses (quantity, kinds, bounds, unit key); keys
are section names, or "section.child" for a nested list/object
("network.segments", "conversions.substrates", "simulation.sd"), plus "links"
(a link's `delay`). Every engine site that turns a field into a number of the
engine's base goes through `field_factor`/`to_base` here; `scaled` returns a
value untouched when the factor is exactly 1.0, so a field already in base is
bit-identical to what the engine computed before units existed. No UI imports.
"""
from __future__ import annotations

import copy

from nagasim.core.block_spec import ParamSpec
from nagasim.core.distributions import rescale_distribution_params
from nagasim.core.expressions import SD_MACROS, ExpressionError, compile_expression
from nagasim.core.problems import problem
from nagasim.core.timeunits import (ModelUnits, base_factor, conversion_factor, default_unit,
                                     is_unit, tidy, units_of)

__all__ = ["SECTION_FIELDS", "param", "field_unit", "field_factor", "scaled",
           "to_base", "switch_unit", "fill_units", "check_units", "time_expressions",
           "TIME_READERS", "TIME_CALLS"]


def _p(name, type_, default, quantity, **extra) -> ParamSpec:
    return ParamSpec(name, type_, default, quantity=quantity, **extra)


def _unit(name, default="base") -> ParamSpec:
    return ParamSpec(name, str, default, quantity="none", kinds=("choice",))


_NUMEXPR = ("number", "expression")
_TICK = (ParamSpec("dt", float, None, quantity="time", min=0, unit_key="time_unit"),
         ParamSpec("phase", float, 0.0, quantity="time", min=0, unit_key="time_unit"),
         ParamSpec("time_unit", str, "base", quantity="none", kinds=("choice",)))
_COSTS = (_p("cost_busy", float, 0.0, "rate", min=0, unit_key="cost_time_unit"),
          _p("cost_idle", float, 0.0, "rate", min=0, unit_key="cost_time_unit"),
          _p("cost_per_use", float, 0.0, "money", min=0), _unit("cost_time_unit", "hours"))

SECTION_FIELDS: dict[str, tuple[ParamSpec, ...]] = {
    "links": (_p("delay", dict, None, "time", kinds=("distribution",),
                 unit_key="delay.time_unit"),),
    "resources": (_p("capacity", int, 1, "count", min=0), *_COSTS),
    "schedules": (_p("points", list, [], "time", kinds=("table",), unit_key="time_unit"),
                  _p("off_value", float, 0.0, "none"), _unit("time_unit")),
    "failures": (_p("uptime", dict, None, "time", kinds=("distribution",),
                    unit_key="uptime.time_unit"),
                 _p("downtime", dict, None, "time", kinds=("distribution",),
                    unit_key="downtime.time_unit")),
    "tables": (_p("default", float, None, "none"),),
    "variables": (_p("initial", object, 0, "none", kinds=("number", "text", "bool")),),
    "entities": (_p("holding_cost", float, 0.0, "rate", min=0,
                    unit_key="holding_cost_time_unit"), _unit("holding_cost_time_unit")),
    "timeseries_inputs": (_p("times", list, [], "time", kinds=("table",), unit_key="time_unit"),
                          _p("start", float, 0.0, "time", unit_key="time_unit"),
                          _p("step", float, None, "time", unit_key="time_unit"),
                          _p("period", float, None, "time", unit_key="time_unit"),
                          _unit("time_unit")),
    "levels": (_p("capacity", float, None, "none"), _p("min", float, 0.0, "none"),
               _p("initial", float, 0.0, "none", kinds=_NUMEXPR),
               _p("max_inflow", float, None, "rate", unit_key="rate_time_unit"),
               _p("max_outflow", float, None, "rate", unit_key="rate_time_unit"),
               _p("blend_tolerance", float, None, "ratio"), _unit("rate_time_unit")),
    "levels.conveyor": (_p("transit_time", str, None, "time", kinds=_NUMEXPR, min=0,
                           unit_key="time_unit"), _unit("time_unit")),
    "flows": (_p("rate", str, "0", "rate", kinds=_NUMEXPR, unit_key="rate_time_unit"),
              _p("efficiency", float, 1.0, "ratio"), _p("price", str, "0", "money", kinds=_NUMEXPR),
              _p("emission_factor", str, "0", "none", kinds=_NUMEXPR),
              _p("priority", int, 0, "none"), _p("peak_price", float, None, "money"),
              _p("min_rate", float, None, "rate", unit_key="rate_time_unit"),
              _unit("rate_time_unit")),
    "conversions": (_p("cohort_interval", float, 1.0, "time", min=0, unit_key="time_unit"),
                    _unit("time_unit")),
    "conversions.substrates": (_p("feed", str, "0", "rate", kinds=_NUMEXPR,
                                  unit_key="feed_time_unit"), _unit("feed_time_unit")),
    "fixed_costs": (_p("amount", str, "0", "money", kinds=_NUMEXPR),),
    # Years, a yearly rate and hours by name: none follows the model's units.
    "investment": (_p("horizon_years", float, 5, "none"),
                   _p("discount_rate", float, 0.08, "ratio", min=0),
                   _p("operating_hours_per_year", float, 4000, "none"),
                   _p("margin_per_part", float, 0.0, "money")),
    "fleets": (_p("count", int, 1, "count", min=1),
               *(_p(n, float, None, "speed", min=0, unit_key="speed_unit")
                 for n in ("speed", "speed_empty", "speed_x", "speed_y")), _unit("speed_unit"),
               _p("hoist_time", float, 0.0, "time", min=0, unit_key="time_unit"),
               _p("load_time", str, "0", "time", kinds=_NUMEXPR, unit_key="time_unit"),
               _p("unload_time", str, "0", "time", kinds=_NUMEXPR, unit_key="time_unit"),
               _unit("time_unit"), _p("loads", int, 1, "count", min=1),
               _p("length_m", float, None, "length", unit_key=""), *_COSTS),
    "motion_profiles": (_p("speed", float, 1.0, "speed", min=0, unit_key="speed_unit"),
                        _p("collaborative_speed", float, None, "speed", min=0,
                           unit_key="speed_unit"), _unit("speed_unit", "m/s"),
                        _p("accel", float, 1.0, "none", min=0),          # m/s^2, fixed (D157)
                        _p("payload_max", float, None, "mass", min=0, unit_key="payload_unit"),
                        _unit("payload_unit", "kg"),
                        _p("payload_derate", float, None, "ratio", min=0, max=1)),
    # power_kw is kW by name (unit_key ""); swap_time is in swap_time_unit.
    "chargers": (_p("points", int, 1, "count", min=1),
                 _p("power_kw", float, None, "power", min=0, unit_key=""),
                 _p("knee", float, 0.8, "ratio", min=0, max=1),
                 _p("taper_floor", float, 0.1, "ratio", min=0, max=1),
                 _p("efficiency", float, 1.0, "ratio", min=0, max=1),
                 _p("swap_time", str, None, "time", kinds=_NUMEXPR, min=0,
                    unit_key="swap_time_unit"), _unit("swap_time_unit"),
                 _p("spare_packs", int, None, "count", min=1)),
    "delays": (_p("delay_time", str, "1", "time", kinds=_NUMEXPR),
               _unit("delay_time_unit"), _p("input", str, "0", "none", kinds=_NUMEXPR),
               _p("initial", str, None, "none", kinds=_NUMEXPR),
               _p("condition", str, None, "none", kinds=_NUMEXPR),
               _p("order", int, 1, "count", min=1),
               *(_p(n, float, None, "none") for n in ("mean", "sd", "lo", "hi"))),
    "agent_types": (_p("speed", str, "1", "speed", kinds=_NUMEXPR), _unit("speed_unit")),
    # D160: statechart timing read the model unit, so each gets a unit key whose
    # missing/"base" value keeps that meaning. A rate's unit is a time unit
    # ("per hours"). Sparse: filled only where that trigger is present.
    "agent_types.transitions": (_p("timeout", str, None, "time", kinds=_NUMEXPR, min=0),
                                _unit("timeout_unit"),
                                _p("rate", str, None, "rate", kinds=_NUMEXPR, min=0),
                                _unit("rate_unit"),
                                _p("scan", float, None, "time", min=0), _unit("scan_unit")),
    "agent_types.space": (_p("patch_tick", float, None, "time", min=0, unit_key="time_unit"),
                          _unit("time_unit")),
    "agent_types.tick": _TICK,
    "populations.tick": _TICK,
    # A dated order or snapshot (ISO text) carries its own clock: only numbers scale.
    "orders": (_p("release", float, 0.0, "time", min=0, unit_key="time_unit"),
               _p("due", float, None, "time", unit_key="time_unit"), _unit("time_unit")),
    "initial_state": (_p("time", float, None, "time", unit_key="time_unit"), _unit("time_unit")),
    "initial_state.entities": (_p("age", float, 0.0, "time", min=0, unit_key="age_unit"),
                               _unit("age_unit")),
    "populations": (_p("count", int, 0, "count", min=0), _p("max_count", int, None, "count")),
    "network": (_p("length_factor", float, 1.0, "ratio", min=0),),
    "network.segments": (_p("length", float, None, "length", min=0), _unit("length_unit"),
                         _p("speed", float, None, "speed", min=0), _unit("speed_unit"),
                         _p("capacity", int, None, "count", min=1)),
    # The model's own unit: these convert when the model unit changes (D160).
    "simulation": (_p("time_limit", float, None, "time", min=0, unit_key="time_unit"),
                   _p("warmup", float, 0.0, "time", min=0, unit_key="time_unit"),
                   _p("timeseries_interval", float, None, "time", min=0, unit_key="time_unit"),
                   _p("replications", int, 1, "count", min=1),
                   _p("energy_price", float, 0, "money", kinds=_NUMEXPR),
                   _p("entity_log_limit", int, None, "count", min=0),
                   _p("timeseries_points", int, 200, "count", min=1)),
    "simulation.sd": (_p("dt", float, None, "time", min=0, unit_key="time_unit"),
                      _unit("time_unit")),
}


def param(spec, name: str) -> ParamSpec:
    """The ParamSpec *name* of a BlockSpec (or of a SECTION_FIELDS tuple)."""
    fields = getattr(spec, "params", spec)
    return next(p for p in fields if p.name == name)


def unit_default(spec: ParamSpec, units: ModelUnits, fields=()) -> str | None:
    """What *spec*'s missing/"base" unit means (D159): the default of its sibling
    unit field when that is a real unit (cost_time_unit "hours", MOTION's
    "seconds", a profile's "m/s"), else the model's unit of the quantity."""
    key = spec.unit_field
    sibling = next((p for p in fields if p.name == key), None) if key else None
    own = getattr(sibling, "default", None)
    return own if own not in (None, "", "base") else default_unit(spec.quantity, units)


def field_unit(spec: ParamSpec, record: dict, units: ModelUnits, fields=()) -> str | None:
    """The unit *record*'s value of *spec* is in, resolved (never "base")."""
    key = spec.unit_field
    if not key:
        return None
    holder, _, leaf = key.rpartition(".")
    source = record.get(holder) if holder else record
    unit = source.get(leaf) if isinstance(source, dict) else None
    return unit_default(spec, units, fields) if unit in (None, "", "base") else unit


def field_factor(spec: ParamSpec, record: dict, units: ModelUnits, fields=()) -> float:
    """Multiply *record*'s value of *spec* by this to get the engine's base."""
    return base_factor(spec.quantity, field_unit(spec, record, units, fields), units)


def _is_number(value) -> bool:
    """A number, or text that reads as one ("1.5"); never a bool."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def scaled(value, factor: float):
    """*value* times *factor*; untouched when the factor is exactly 1.0 (so a
    field in base stays bit-identical) and when it is empty/None/a bool. A
    number multiplies; a numeric string stays a string ("5" -> "300.0"); other
    text is an expression and is wrapped as "(text) * f"."""
    if factor == 1.0 or value is None or value == "" or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value * factor
    if isinstance(value, str):
        return repr(float(value) * factor) if _is_number(value) else f"({value}) * {factor!r}"
    return value


def to_base(spec: ParamSpec, record: dict, units: ModelUnits, fields=()):
    """*record*'s value of *spec* (its default when absent) in the engine's base."""
    return scaled(record.get(spec.name, spec.default), field_factor(spec, record, units, fields))


def switch_unit(record: dict, fields, unit_key: str, new_unit: str,
                units: ModelUnits) -> tuple[dict, list[str]]:
    """D161: a copy of *record* with *unit_key* set to *new_unit* and every field
    measured in it converted so its meaning is kept (1 min -> 60 s): numbers
    multiply, expression text is wrapped, a distribution's scale parameters
    are rescaled. Returns (record, names of fields left as they were because
    their distribution has no scale parameter, or "segments[i]" for a MOTION
    segment whose time is not a plain number -- the caller warns)."""
    out, kept = copy.deepcopy(record), []
    holder, _, leaf = unit_key.rpartition(".")
    for spec in fields:
        if spec.unit_field != unit_key:
            continue
        f = conversion_factor(spec.quantity, field_unit(spec, record, units, fields), new_unit)
        box = out if spec.name == "dist_params" else out.get(spec.name) if holder else None
        if isinstance(box, dict) and ("dist_params" in box or spec.name == "dist_params"):
            rescaled = rescale_distribution_params(box.get("distribution", "Constant"),
                                                   box.get("dist_params") or {}, f)
            if rescaled is None:
                kept.append(spec.name)
            else:
                box["dist_params"] = rescaled
        elif isinstance(out.get(spec.name), list):
            # MOTION segments: a plain-number `time` converts; an expression or
            # distribution string keeps its value and is reported (editor note).
            for i, item in enumerate(out[spec.name]):
                if not isinstance(item, dict) or item.get("time") in (None, ""):
                    continue
                if _is_number(item["time"]):
                    item["time"] = tidy(scaled(item["time"], f), item["time"])
                else:
                    kept.append(f"{spec.name}[{i}]")
        elif (spec.name in out and not isinstance(out[spec.name], dict)
              and (_is_number(out[spec.name]) or "expression" in spec.field_kinds
                   or not isinstance(out[spec.name], str))):      # a date (text) keeps its clock
            out[spec.name] = tidy(scaled(out[spec.name], f), out[spec.name])
    target = out.setdefault(holder, {}) if holder else out
    target[leaf] = new_unit
    return out, kept


def _records(layout: dict, key: str) -> list:
    """The dicts a SECTION_FIELDS key names: "fleets" -> layout["fleets"],
    "network.segments" -> layout["network"]["segments"], "simulation.sd" -> [its sd]."""
    head, _, tail = key.partition(".")
    found = layout.get(head)
    items = found if isinstance(found, list) else [found]
    if tail:
        nested = []
        for item in items:
            value = item.get(tail) if isinstance(item, dict) else None
            nested.extend(value if isinstance(value, list) else [value])
        items = nested
    return [item for item in items if isinstance(item, dict)]


# One trigger each, or an optional block: fill only where a field is present.
_SPARSE = ("agent_types.transitions", "agent_types.space", "orders", "initial_state",
           "initial_state.entities", "chargers")


def _fill(record: dict, fields, units, sparse: bool = False) -> None:
    for spec in fields:
        key = spec.unit_field
        if not key or (sparse and spec.name not in record):
            continue
        holder, _, leaf = key.rpartition(".")
        target = record.get(holder) if holder else record
        if isinstance(target, dict) and target.get(leaf) in (None, "", "base"):
            target[leaf] = unit_default(spec, units, fields)


def fill_units(layout: dict) -> dict:
    """D159: write every unit a top-level record leaves missing or "base" as the
    unit it already meant -- the field's own default, else the model's. Runs on
    every load (idempotent). Submodel definitions are templates and scenario
    overrides are applied later: both keep "base", which the converter still
    resolves to the host model's unit. A dated time series carries its own clock."""
    from nagasim.core.block_registry import BLOCK_REGISTRY     # blocks import this module

    units = units_of(layout.get("simulation"))
    for block in layout.get("blocks") or []:
        if not isinstance(block, dict):
            continue                    # validation's to report
        spec = BLOCK_REGISTRY.get(str(block.get("type", "")).upper())
        if spec is not None and isinstance(block.get("params"), dict):
            _fill(block["params"], spec.params, units)
    for key, fields in SECTION_FIELDS.items():
        if key == "simulation":
            continue                    # its time_unit IS the model unit
        for record in _records(layout, key):
            if not (key == "timeseries_inputs" and record.get("timestamps")):
                _fill(record, fields, units, sparse=key in _SPARSE)
    _pin_override_units(layout, units)
    return layout


def _pin_override_units(layout: dict, units: ModelUnits) -> None:
    """A scenario override that sets a unit key to "base" would follow a later
    model-unit change; write the unit it means now, as for a field (D159). Only
    a path that addresses a unit key of a block param or a top-level section."""
    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.experiments import override_targets        # experiments imports core

    overrides = [o for s in layout.get("scenarios") or [] if isinstance(s, dict)
                 for o in s.get("overrides") or [] if isinstance(o, dict)
                 and o.get("value") == "base"]
    if not overrides:
        return
    types = {b.get("name"): str(b.get("type", "")).upper() for b in layout.get("blocks") or []
             if isinstance(b, dict)}
    where = {path: (section, key, field) for path, section, key, field in override_targets(layout)}
    for override in overrides:
        section, key, field = where.get(override.get("path"), (None, None, None))
        if section == "blocks" and field.startswith("params."):
            block = BLOCK_REGISTRY.get(types.get(key, ""))
            fields, field = (block.params if block else ()), field[len("params."):]
        else:
            fields = SECTION_FIELDS.get(section, ())
        spec = next((p for p in fields if p.unit_field == field), None)
        if spec is not None:
            override["value"] = unit_default(spec, units, fields)


def check_units(layout: dict, problems: list) -> None:
    """Every unit a field names must be one of its quantity's (E_TIME_UNIT for
    time and rates, E_UNIT otherwise; the message names the unit key, e.g.
    "<block>: unknown time_unit 'x'" as the old per-site checks said).
    `simulation.sd` keeps its own E_SD_SETTINGS check. A unit key shared by
    several fields (cost_busy/cost_idle) is reported once."""
    from nagasim.core.block_registry import BLOCK_REGISTRY

    def check(record, fields, context, at, block=None):
        seen = set()
        for spec in fields:
            key = spec.unit_field
            if not key or key in seen:
                continue
            seen.add(key)
            holder, _, leaf = key.rpartition(".")
            source = record.get(holder) if holder else record
            unit = source.get(leaf) if isinstance(source, dict) else None
            if is_unit(spec.quantity, unit):
                continue
            timed = spec.quantity in ("time", "rate")
            problems.append(problem(
                "E_TIME_UNIT" if timed else "E_UNIT",
                f"{context}: unknown {leaf} '{unit}'",
                f"{at}/{key.replace('.', '/')}", block=block,
                hint=f"use \"base\" (the model's own unit) or one of its "
                     f"{spec.quantity} units"))

    for i, block in enumerate(layout.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        spec = BLOCK_REGISTRY.get(str(block.get("type", "")).upper())
        if spec is not None and isinstance(block.get("params"), dict):
            name = block.get("name")
            check(block["params"], spec.params, name, f"/blocks/{i}/params", name)
    for key, fields in SECTION_FIELDS.items():
        if key in ("simulation", "simulation.sd"):
            continue
        for j, record in enumerate(_records(layout, key)):
            check(record, fields, f"{key} '{record.get('name', j)}'",
                  f"/{key.replace('.', '/')}/{j}")


# What reads time in the model unit and cannot be converted safely (D160's
# stated limitation): the clock names, and functions whose time arguments are
# literals in the model unit.
TIME_READERS = frozenset({"TNOW", "now", "HORIZON"})
TIME_CALLS = frozenset({"STEP", "PULSE", "RAMP", *SD_MACROS})
_SKIP = ("view", "annotations", "info", "background", "data_sources")


def time_expressions(layout: dict) -> list[str]:
    """"<path>: <text>" for every expression in *layout* that reads TNOW/now/
    HORIZON or calls a function with time arguments (STEP, PULSE, RAMP, the SD
    delay/smooth macros): its numbers are in the model's time unit, so a change
    of that unit changes what it means. Listed, never rewritten."""
    found: list[str] = []
    words = TIME_READERS | TIME_CALLS

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}/{i}")
        elif isinstance(node, str) and any(word in node for word in words):
            try:
                compiled = compile_expression(node)
            except ExpressionError:
                return
            if compiled.names & TIME_READERS or compiled.calls & TIME_CALLS:
                found.append(f"{path}: {node}")

    walk({k: v for k, v in layout.items() if k not in _SKIP}, "")
    return found
