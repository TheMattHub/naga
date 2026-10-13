"""JSON Schema (draft 2020-12) for the model file format (ROADMAP 3.1 item 2).

Hand-written in `_defs()` for model sections; generated from live registries for
blocks and distributions, so a plugin's block or distribution appears in the
schema the moment `nagasim.plugins.load_plugins()` has run.

    from nagasim.modelschema import build_json_schema
    schema = build_json_schema()          # a plain dict, ready for json.dumps

    naga schema --out model.schema.json
    naga validate model.json --json

Pure Python (stdlib only, no pandas, no PySide6): a top-level module rather
than one under `core/` because it is a contract for outside consumers -- the
MCP server, an editor's autocomplete, a CI check -- not something the engine
reads. `core/schema/` stays the runtime validator; this file only describes
the file format, and the two cannot drift because both are generated from (or
check against) the same registries.

Deliberately structural: the schema checks shapes, names and enumerations, not
cross-references (a link naming a block that exists, a resource set's members).
Those are `schema.validate_model_structured`'s job and stay there -- JSON
Schema cannot express them, and duplicating the ones it could express would
give two sources of truth for one rule.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import argparse
import json
from pathlib import Path

from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.blocks.create import ARRIVAL_MODES
from nagasim.core.calendar import WEEKDAYS
from nagasim.core.distributions import DISTRIBUTION_REGISTRY
from nagasim.core.resource_manager import DISCIPLINES, ENERGY_STATES, SELECTION_RULES
from nagasim.core.schedules import SCHEDULE_KINDS
from nagasim.core.schema import (
    CURRENT_VERSION, DATA_SOURCE_KINDS, DATA_TRANSFORMS, DATA_TRANSFORM_PREFIXES,
)
from nagasim.core.schema.sections_validation import (DASHBOARD_ACTIVITIES,
                                                     DASHBOARD_INPUT_KINDS,
                                                     DASHBOARD_LIMITS as _DASH,
                                                     DASHBOARD_VIEW_MODES)
from nagasim.core.quantities import SECTION_FIELDS
from nagasim.core.sections import SECTIONS
from nagasim.core.stats import RECORD_KINDS
from nagasim.core.conversions import DEFAULT_STEPS, DEFAULT_TAIL
from nagasim.core.levels import DEFAULT_BLEND_TOLERANCE, SD_METHODS, THRESHOLD_DIRECTIONS
from nagasim.core.tables import LOOKUP_MODES
from nagasim.core.timeseries_inputs import BEYOND_MODES, INTERPOLATIONS
from nagasim.core.timeunits import LENGTH_UNITS, SPEED_PATTERN, UNITS, unit_choices

__all__ = ["build_json_schema", "SCHEMA_ID"]

SCHEMA_ID = "https://github.com/TheMattHub/naga/schema/model.json"
_DRAFT = "https://json-schema.org/draft/2020-12/schema"

# A number or an expression string: every numeric distribution parameter, and
# several block params, accept either (ARCHITECTURE section 5.7).
_NUMBER_OR_EXPRESSION = {
    "anyOf": [{"type": "number"}, {"type": "string"}],
    "description": "A number, or an expression string evaluated per sample "
                   "(e.g. \"2 * entity.size\").",
}

_PY_TYPES = {int: "integer", float: "number", str: "string", bool: "boolean",
             list: "array", dict: "object"}


def _unit_schema(quantity: str, description: str, default="base") -> dict:
    """A unit key's schema from the unit tables (units spec): an enum, or for a
    speed the "<length>/<time>" pattern; "base" always allowed (definitions,
    overrides)."""
    if quantity == "speed":
        return {"type": "string", "pattern": SPEED_PATTERN, "default": default,
                "description": description}
    return {"type": "string", "enum": ["base", *unit_choices(quantity)], "default": default,
            "description": description}


def _time_unit(description: str) -> dict:
    return {"type": "string", "enum": ["base", *UNITS], "default": "base",
            "description": description}


def _power_schema(per: str) -> dict:
    """"power" (PHASE6 W3 M1, D112): kW per *per* (unit/vehicle) in each state,
    all optional -- absent keys are 0, and "blocked" defaults to "busy"."""
    return {"type": "object",
            "description": f"kW per {per} while busy/idle/blocked/down/off_shift; absent keys "
                           f"are 0, \"blocked\" defaults to \"busy\" (a blocked {per} is seized).",
            "properties": {state: {"type": "number", "minimum": 0} for state in ENERGY_STATES},
            "additionalProperties": False}


def _param_schema(param, unit_quantity: str | None = None) -> dict:
    """One ParamSpec as a JSON Schema property, from its metadata (units spec):
    kinds pick the type, min/max the bounds, a unit key its unit table."""
    json_type = _PY_TYPES.get(param.type, "string")
    kinds = param.field_kinds
    if unit_quantity is not None:
        out = _unit_schema(unit_quantity, param.description or "Unit of its field.",
                           param.default)
    elif param.name == "distribution":
        out = {"type": "string", "enum": sorted(DISTRIBUTION_REGISTRY),
               "description": param.description or "Name of a registered distribution."}
    elif param.name == "dist_params":
        out = {"$ref": "#/$defs/dist_params"}
    elif {"number", "integer"} & set(kinds):
        number = {"type": "number" if "number" in kinds else "integer"}
        if param.min is not None:
            number["minimum"] = param.min
        if param.max is not None:
            number["maximum"] = param.max
        out = {"anyOf": [number, {"type": "string"}]} if "expression" in kinds else number
    else:
        # `object` is "any JSON value" (SEND's `to`: an expression or an object).
        out = {} if param.type is object else {"type": json_type}
    if param.quantity not in (None, "none"):
        out["x-quantity"] = param.quantity
        if param.unit_field:
            out["x-unit-key"] = param.unit_field
    if param.default is not None or param.type is not dict:
        out.setdefault("default", param.default)
    if param.description and "description" not in out:
        out["description"] = param.description
    return out


def _or_argument(schema: dict) -> dict:
    """A param value, or a structural argument (`{"$arg": ...}`, PHASE4 B6):
    inside a submodel definition any param may be bound to the instance's
    argument, and flattening substitutes it before the engine sees the block."""
    out = {"anyOf": [schema, {"$ref": "#/$defs/structural_argument"}]}
    for key in ("default", "description", "x-quantity", "x-unit-key"):
        if key in schema:
            out[key] = schema[key]
    return out


def _block_variant(type_name: str, spec) -> dict:
    """One block type as a `oneOf` branch, keyed by its `type`."""
    unit_of = {p.unit_field: p.quantity for p in spec.params if p.unit_field}
    properties = {p.name: _or_argument(_param_schema(p, unit_of.get(p.name)))
                  for p in spec.params}
    required = [p.name for p in spec.params if p.required]
    doc = " ".join((spec.cls.__doc__ or "").split()) or type_name
    return {
        "title": type_name,
        "description": doc,
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1,
                     "description": "Unique block name; the model's identifier for it."},
            "description": {"type": "string", "description": "Label shown on the canvas."},
            "type": {"const": type_name},
            "position": {"$ref": "#/$defs/point",
                         "description": "Block CENTRE in scene units."},
            "layout_position": {"$ref": "#/$defs/point",
                                "description": "Where the block sits on the FLOOR PLAN, in "
                                               "layout units. Absent for a block "
                                               "that has never been placed in layout mode, and "
                                               "unnecessary for one attached to a network node."},
            "layout_rotation": {"type": "number", "minimum": 0, "exclusiveMaximum": 360,
                                "description": "How the block is turned on the FLOOR PLAN, in "
                                               "degrees CLOCKWISE as seen on screen (+x east, "
                                               "+y south); 0 or absent means unturned. "
                                               "Layout view only -- the schematic is never "
                                               "rotated. An asset's own network nodes are stored "
                                               "already turned."},
            "label_offset": {"$ref": "#/$defs/point",
                             "description": "Where the block's NAME label was dragged to "
                                            ", in scene units from its default "
                                            "anchor, measured in the UNROTATED frame. Absent "
                                            "means the default position. Editor-only."},
            "locals": {"type": "object",
                       "description": "Submodel parameter bindings, written by flattening; "
                                      "not authored by hand."},
            "params": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False},
        },
        "required": ["name", "type"],
        "additionalProperties": False,
    }


def _distribution_variant(name: str, spec) -> dict:
    """One distribution as a `oneOf` branch of the `duration` object."""
    properties: dict = {}
    for param in spec.params:
        if param.kind == "list":
            properties[param.name] = {"type": "array", "items": {"type": "number"},
                                      "default": param.default}
        elif param.kind == "expression":
            properties[param.name] = {"type": "string", "default": param.default}
        else:
            properties[param.name] = dict(_NUMBER_OR_EXPRESSION, default=param.default)
        if param.hint:
            properties[param.name]["description"] = param.hint
    return {
        "title": name,
        "description": " ".join(spec.use_case.split()),
        "type": "object",
        "properties": {
            "distribution": {"const": name},
            "dist_params": {"type": "object", "properties": properties,
                            "additionalProperties": False},
            "time_unit": _time_unit("Unit the sampled duration is expressed in."),
        },
        "required": ["distribution"],
        "additionalProperties": False,
    }


# `dims` on a level, flow, auxiliary or delay (PHASE5 SD-3).
_DIMS = {"type": "array", "items": {"type": "string"}, "uniqueItems": True,
         "description": "Subscripts this record runs over: it is flattened into one record per "
                        "element, named \"Name[north]\" (\"Name[north,young]\" for two). "
                        "Inside, \"x[dim]\" in a LEVEL/FLOW/AUX/DELAY name reads the same "
                        "element, and SUM(x)/MEAN(x) aggregate over \"x[*]\"."}
# Per-element equations on a subscripted record (PHASE6 S3).
_ELEMENTS = {"type": "object", "additionalProperties": {"type": ["string", "number"]},
             "description": "Per-element equations: {\"north\": \"2\", \"a,x\": \"3\"} "
                            "replaces the record's first expression (a level's initial, a flow's "
                            "rate, an auxiliary's expr, a delay's input) for those elements; the "
                            "shared one covers the rest. Keys join one element per dim with a "
                            "comma."}


def _defs() -> dict:
    return {
        "point": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
        "structural_argument": {
            "type": "object",
            "description": "Inside a submodel definition only: bind this param to the instance's "
                           "argument of that name. Flattening replaces it with "
                           "the argument's value, coerced to the param's declared type, so the "
                           "engine never sees one.",
            "required": ["$arg"],
            "properties": {"$arg": {"type": "string",
                                    "description": "A parameter the definition declares."}},
            "additionalProperties": False,
        },
        # dist_params inside a BLOCK: which keys are legal depends on the sibling
        # "distribution", which JSON Schema can only express as a separate
        # oneOf; keeping it open here and closed in `duration` (below) is the
        # trade-off that keeps block variants readable.
        "dist_params": {
            "type": "object",
            "description": "Parameters of the sibling `distribution`, by name "
                           "(e.g. {\"rate\": 0.5} for Exponential). Each value is a number "
                           "or an expression string. See $defs/duration for the per-distribution "
                           "parameter lists.",
            "additionalProperties": {
                "anyOf": [{"type": "number"}, {"type": "string"},
                          {"type": "array", "items": {"type": "number"}}],
            },
        },
        "duration": {
            "description": "A duration spec: a distribution, its parameters and an optional unit. "
                           "Used by link delays and failure uptime/downtime.",
            "oneOf": [_distribution_variant(name, DISTRIBUTION_REGISTRY[name])
                      for name in sorted(DISTRIBUTION_REGISTRY)],
        },
        "block": {"oneOf": [_block_variant(name, BLOCK_REGISTRY[name])
                            for name in sorted(BLOCK_REGISTRY)]},
        "link": {
            "type": "object",
            "description": "A directed link. Array order is BRANCH order for a DECIDE/LOGIC. "
                           "Several links may join the same pair of blocks (two branches to one "
                           "target): they are separate entries, identified by source, target and "
                           "which of the parallel links between that pair they are.",
            "properties": {
                "source": {"type": "string", "description": "Name of the source block."},
                "target": {"type": "string", "description": "Name of the target block."},
                "delay": {"anyOf": [{"$ref": "#/$defs/duration"}, {"type": "null"}],
                          "description": "Optional transfer delay on this link."},
                "source_port": {"type": ["string", "null"],
                                "description": "Out port, only when the source is a SUBMODEL instance."},
                "target_port": {"type": ["string", "null"],
                                "description": "In port, only when the target is a SUBMODEL instance."},
            },
            "required": ["source", "target"],
            "additionalProperties": False,
        },
        "resource": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "capacity": {"type": "integer", "minimum": 0, "default": 1},
                "discipline": {"type": "string", "enum": list(DISCIPLINES), "default": "FIFO",
                               "description": "Queue order. LowestValueFirst/HighestValueFirst "
                                              "need a ranking_expr; Plan works each station's "
                                              "planned order (entity.plan, cpsat.propose)."},
                "ranking_expr": {"type": "string",
                                 "description": "Expression ranking the queue, for a ranked discipline."},
                "rerank": {"type": "boolean", "default": False,
                          "description": "Re-evaluate ranking_expr for every waiting request just "
                                         "before each grant, instead of once when it joins the "
                                         "queue; only meaningful with LowestValueFirst/"
                                         "HighestValueFirst."},
                "capacity_schedule": {"type": "string",
                                      "description": "Name of a \"capacity\" schedule staffing this resource."},
                "cost_busy": {"type": "number", "minimum": 0, "default": 0.0},
                "cost_idle": {"type": "number", "minimum": 0, "default": 0.0},
                "cost_per_use": {"type": "number", "minimum": 0, "default": 0.0},
                "cost_time_unit": _time_unit("Unit cost_busy/cost_idle are quoted per."),
                "power": _power_schema("unit"),
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "resource_set": {
            "type": "object",
            "description": "A named group of resources, seized as one pseudo-resource.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "members": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "rule": {"type": "string", "enum": list(SELECTION_RULES),
                         "default": "PreferredOrder"},
            },
            "required": ["name", "members"],
            "additionalProperties": False,
        },
        "entity_type": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "shape": {"type": "string"},
                "color": {"type": "string"},
                "icon": {"type": "string", "description": "Path to an animation icon image."},
                "graphic": {"type": "string",
                            "description": "Inline SVG token (24 x 24 artboard), usually copied "
                                           "from a library's entity preset; checked by "
                                           "core/svgsafe before it is drawn."},
                "holding_cost": {"type": "number", "minimum": 0, "default": 0.0,
                                 "description": "WIP cost per entity per base time unit."},
                "holding_cost_time_unit": _unit_schema("time", "Unit `holding_cost` is per."),
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "variable": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "initial": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "boolean"}],
                            "default": 0},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "schedule": {
            "type": "object",
            "description": "Piecewise-constant arrival rate or capacity calendar.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "kind": {"type": "string", "enum": list(SCHEDULE_KINDS), "default": "arrival"},
                "repeat": {"type": "boolean", "default": False},
                "time_unit": _time_unit("Unit the point durations are expressed in."),
                "points": {
                    "type": "array", "minItems": 1,
                    "items": {"type": "array", "minItems": 2, "maxItems": 2,
                              "items": {"type": "number"},
                              "description": "[duration, value]; value is a rate (arrival) or a "
                                             "whole number of units (capacity)."},
                },
                "weekly": {
                    "type": "object",
                    "description": "Weekday grid in LOCAL wall-clock time, instead of \"points\" "
                                   ". Needs \"simulation.calendar\". Keyed by weekday "
                                   "(mon..sun); each value is a list of [\"06:00\", \"14:00\", "
                                   "value] bands. A day with no bands is off.",
                    "propertyNames": {"enum": list(WEEKDAYS)},
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 3, "maxItems": 3},
                    },
                },
                "off_value": {"type": "number", "default": 0,
                              "description": "Value outside every band of a weekday schedule."},
                "exceptions": {
                    "type": "array",
                    "description": "Dated overrides of a weekday grid -- holidays, shutdowns. "
                                   "Whole LOCAL days, \"end\" inclusive; last match wins.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string",
                                      "description": "First day, ISO (2026-12-24)."},
                            "end": {"type": "string",
                                    "description": "Last day, inclusive; defaults to \"start\"."},
                            "value": {"type": "number", "default": 0,
                                      "description": "Value held all day; 0 = closed."},
                            "description": {"type": "string"},
                        },
                        "required": ["start"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "table": {
            "type": "object",
            "description": "A lookup table read by TABLE(\"name\", k1[, k2]) in any expression "
                           ". 1-D: \"keys\" plus a flat \"values\" list. 2-D: also "
                           "\"columns\", and \"values\" is one row per key. Keys are strings or "
                           "numbers; values are numbers.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "keys": {"type": "array", "minItems": 1,
                         "items": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                         "description": "Row keys (dimension 1)."},
                "columns": {"type": "array", "minItems": 1,
                            "items": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                            "description": "Column keys (dimension 2); omit for a 1-D table."},
                "values": {"type": "array",
                           "items": {"anyOf": [{"type": "number"},
                                               {"type": "array", "items": {"type": "number"}}]},
                           "description": "One number per key (1-D), or one row of numbers per "
                                          "key (2-D)."},
                "key_mode": {"type": "string", "enum": list(LOOKUP_MODES), "default": "exact",
                             "description": "exact (the key must be present), step (largest "
                                            "numeric key <= x) or linear (interpolate); step and "
                                            "linear clamp at both ends."},
                "column_mode": {"type": "string", "enum": list(LOOKUP_MODES), "default": "exact"},
                "default": {"type": ["number", "null"],
                            "description": "Value for a missing exact key; without one a miss is "
                                           "a run-time model error."},
            },
            "required": ["name", "keys", "values"],
            "additionalProperties": False,
        },
        "timeseries_input": {
            "type": "object",
            "description": "A named profile the model READS with TS(\"name\"[, t]) -- a PV curve, "
                           "a site load, a tariff by hour. Give either \"times\" "
                           "(irregular) or \"start\" + \"step\" (regular), plus \"values\".",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "values": {"type": "array", "minItems": 1, "items": {"type": "number"},
                           "description": "One number per sample."},
                "times": {"type": "array", "minItems": 1, "items": {"type": "number"},
                          "description": "Sample times, strictly increasing; exclusive with \"step\"."},
                "start": {"type": ["number", "string"], "default": 0,
                          "description": "Time of the first sample of a regular series. An ISO "
                                         "timestamp instead of a number dates the series against "
                                         "\"simulation.calendar\"."},
                "timestamps": {"type": "array", "minItems": 1, "items": {"type": "string"},
                               "description": "Real ISO sample instants instead of \"times\"; "
                                              "converted once at build."},
                "step": {"type": "number", "exclusiveMinimum": 0,
                         "description": "Spacing of a regular series; exclusive with \"times\"."},
                "time_unit": _time_unit("Unit the sample times are expressed in."),
                "interpolation": {"type": "string", "enum": list(INTERPOLATIONS), "default": "step",
                                  "description": "step: piecewise constant (exact for a flow rate). "
                                                 "linear: interpolate between samples."},
                "beyond": {"type": "string", "enum": list(BEYOND_MODES), "default": "hold",
                           "description": "Past the last sample: hold the last value, repeat the "
                                          "series with its period, or refuse."},
                "period": {"anyOf": [{"type": "number", "exclusiveMinimum": 0},
                                     {"type": "string", "enum": ["daily", "weekly", "yearly"]}],
                           "description": "Cycle length \"repeat\" wraps on; defaults to "
                                          "step * len(values), or times[-1] - times[0]. "
                                          "\"daily\"/\"weekly\"/\"yearly\" need a calendar."},
            },
            "required": ["name", "values"],
            "additionalProperties": False,
        },
        "level": {
            "type": "object",
            "description": "A continuous quantity with bounds -- battery energy, tank volume, an "
                           "inventory position. Read by LEVEL(\"name\"); moved by the "
                           "\"flows\" section and by TRANSFER blocks.",
            "properties": {
                "diagram": {"type": "object", "description": "Position in the stock-and-flow view; ignored by the engine."},
                "name": {"type": "string", "minLength": 1},
                "dims": _DIMS,
                "elements": _ELEMENTS,
                "description": {"type": "string"},
                "unit": {"type": "string", "description": "Label only, e.g. kWh or tonnes."},
                "capacity": {"type": ["number", "null"],
                             "description": "Upper bound; omit for an unbounded accumulator."},
                "min": {"type": ["number", "null"], "default": 0,
                        "description": "Lower bound; null for a stock unbounded below "
                                       "."},
                "initial": {"type": ["number", "string"], "default": 0,
                            "description": "Value at t=0, inside [min, capacity]; or an "
                                           "expression, evaluated once in dependency order "
                                           "."},
                "max_inflow": {"type": ["number", "null"], "minimum": 0,
                               "description": "Largest total rate that may be ADDED to the level "
                                              "(a charge-power limit)."},
                "max_outflow": {"type": ["number", "null"], "minimum": 0,
                                "description": "Largest total rate that may be REMOVED from it "
                                               "(a discharge-power limit)."},
                "rate_time_unit": _time_unit("Unit the two power limits are per."),
                "thresholds": {
                    "type": "array",
                    "description": "Level values that fire discrete logic when crossed.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "number"},
                            "direction": {"type": "string", "enum": list(THRESHOLD_DIRECTIONS),
                                          "default": "falling"},
                            "signal": {"type": "string",
                                       "description": "Signal succeeded at the crossing, releasing "
                                                      "every HOLD block waiting on it."},
                            "variable": {"type": "string",
                                         "description": "Model variable set to the level at the "
                                                        "crossing."},
                            "cost": {"type": "number", "default": 0,
                                     "description": "Lump charged once per crossing; it joins "
                                                    "Total Cost and the cost_summary frame."},
                            "cost_item": {"type": "string",
                                          "description": "Cost item the lump accrues into; "
                                                         "defaults to the record's own name."},
                            "category": {"type": "string",
                                         "description": "Grouping for that cost item."},
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
                "components": {
                    "type": "array",
                    "description": "Named components the level's content is tracked in: "
                                   "a gas holder's ch4. Each inflow carries a share, each "
                                   "outflow draws at the level's own blend, and the component "
                                   "is conserved exactly. Read by FRACTION(name, component).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "initial_fraction": {"type": "number", "minimum": 0, "maximum": 1,
                                                 "default": 0,
                                                 "description": "Share of the INITIAL content "
                                                                "that is this component."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "blend_tolerance": {"type": "number", "exclusiveMinimum": 0,
                                    "default": DEFAULT_BLEND_TOLERANCE,
                                    "description": "How far the fraction may drift from the "
                                                   "value the outflows are drawing at before the "
                                                   "engine re-evaluates. The crossing instant is "
                                                   "solved in closed form; the component is "
                                                   "conserved exactly whatever this is set to."},
                "conveyor": {
                    "type": "object",
                    "description": "Makes the stock a Stella conveyor: material "
                                   "rides round(transit_time/dt) slats of the SD grid (euler "
                                   "only), leaves as the one primary outflow (rate \"\") and "
                                   "may leak on the way through \"leak\" flows. The stock is "
                                   "the sum of the slats and changes only at grid points.",
                    "properties": {
                        "transit_time": {"type": ["string", "number"],
                                         "description": "Time on the belt, latched per batch at "
                                                        "entry."},
                        "time_unit": _time_unit("Unit `transit_time` is in."),
                        "capacity": {"type": ["string", "number"],
                                     "description": "Most the belt holds; limits equation-driven "
                                                    "inflow only."},
                        "in_limit": {"type": ["string", "number"],
                                     "description": "Most inflow per time unit (equation-driven "
                                                    "inflow only)."},
                        "arrest": {"type": ["string", "number"],
                                   "description": "While nonzero nothing moves, enters or "
                                                  "leaves."},
                        "leak_kind": {"type": "string", "enum": ["linear", "exponential"],
                                      "default": "linear",
                                      "description": "linear: a batch loses fraction * its "
                                                     "entry amount spread evenly over the zone; "
                                                     "exponential: content * fraction * dt per "
                                                     "DT."},
                    },
                    "required": ["transit_time"],
                    "additionalProperties": False,
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "flow": {
            "type": "object",
            "description": "A rate-based transfer between levels. The rate is an "
                           "expression re-evaluated only at events, so every level moves linearly "
                           "in between and every crossing is solved in closed form.",
            "properties": {
                "diagram": {"type": "object", "description": "Position in the stock-and-flow view; ignored by the engine."},
                "name": {"type": "string", "minLength": 1},
                "dims": _DIMS,
                "elements": _ELEMENTS,
                "description": {"type": "string"},
                "from": {"type": ["string", "null"],
                         "description": "Level drawn from; omit for a source outside the model."},
                "to": {"type": ["string", "null"],
                       "description": "Level fed; omit for a sink outside the model."},
                "rate": {"type": ["string", "number"],
                         "description": "Quantity per time unit: an expression over variables, TS(), "
                                        "TABLE(), LEVEL() and FLOW(). A negative value is clamped to "
                                        "0 -- declare the reverse flow instead, or set "
                                        "\"biflow\"."},
                "biflow": {"type": "boolean", "default": False,
                           "description": "Bi-directional: a negative rate runs "
                                          "the flow backwards instead of being clamped to 0. "
                                          "Both ends must be outside the model or levels with "
                                          "\"min\": null and no capacity or power limit."},
                "rate_time_unit": _time_unit("Unit the rate is per."),
                "efficiency": {"type": "number", "exclusiveMinimum": 0, "maximum": 1, "default": 1,
                               "description": "out = in * efficiency; the rest is a loss."},
                "price": {"type": ["string", "number"],
                          "description": "Money per unit moved; integrated exactly into the flow's "
                                         "Cost and the model's Total Cost."},
                "emission_factor": {"type": ["string", "number"],
                                    "description": "Impact per unit moved (emissions, water, waste), "
                                                   "integrated into Total Impact."},
                "tag": {"type": "string",
                        "description": "Bus tag: every flow carrying it is summed by "
                                       "FLOWSUM(\"tag\"), so a site's generation or demand is one "
                                       "number however many assets contribute to it."},
                "priority": {"type": "number", "default": 0,
                             "description": "Merit order within a tag: FLOWSUM(\"tag\", p) sums "
                                            "only the flows with a priority below p -- what the "
                                            "devices dispatched before this one actually took."},
                "peak_price": {"type": "number", "default": 0,
                               "description": "Demand charge: money per unit of the "
                                              "flow's Peak Rate, billed ONCE at the end of the "
                                              "run. A time-varying tariff is \"price\"."},
                "min_rate": {"type": "number", "minimum": 0,
                             "description": "Minimum load: the flow runs at 0 or at "
                                            "least this rate, never in between. The gate is "
                                            "applied to the rate the expression ASKED for, "
                                            "before saturation, so it adds no event of its own "
                                            "and cannot chatter."},
                "components": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "number"]},
                    "description": "Share of what this flow moves that is each component, "
                                   "a number or an expression. Omit a component to carry "
                                   "the SOURCE level's own blend.",
                },
                "leak": {
                    "type": "object",
                    "description": "A conveyor's leakage outflow; set \"rate\": "
                                   "\"\" and \"from\" the conveyor.",
                    "properties": {
                        "fraction": {"type": ["string", "number"],
                                     "description": "Linear: share of a batch lost over the "
                                                    "zone. Exponential: rate per time unit. Read "
                                                    "every DT."},
                        "zone": {"type": "array", "items": {"type": "number", "minimum": 0,
                                                            "maximum": 1},
                                 "minItems": 2, "maxItems": 2, "default": [0, 1],
                                 "description": "[start, end] fractions of the belt from the "
                                                "inflowing side."},
                    },
                    "required": ["fraction"],
                    "additionalProperties": False,
                },
                "thresholds": {
                    "type": "array",
                    "description": "Realised-rate values that fire discrete logic when crossed "
                                   ": a signal, a variable, and a lump cost per "
                                   "crossing -- a generator's start cost. Rising crossings are "
                                   "counted as the flow's Starts.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "number"},
                            "direction": {"type": "string", "enum": list(THRESHOLD_DIRECTIONS),
                                          "default": "falling"},
                            "signal": {"type": "string"},
                            "variable": {"type": "string"},
                            "cost": {"type": "number", "default": 0,
                                     "description": "Lump charged once per crossing."},
                            "cost_item": {"type": "string",
                                          "description": "Cost item it accrues into; defaults to "
                                                         "the flow's own name."},
                            "category": {"type": "string"},
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "rate"],
            "additionalProperties": False,
        },
        "conversion": {
            "type": "object",
            "description": "A delayed conversion with a yield curve: feed in, product "
                           "released over the following hours or days, per substrate. Feed is "
                           "grouped into cohorts closed at a fixed interval and each cohort "
                           "releases at a rate constant within each step of its curve, so the "
                           "product rate stays piecewise constant. Read by CONV(name) and "
                           "FRACTION(name, component); attach it to a level with an ordinary "
                           "flow whose rate is CONV(name).",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "unit": {"type": "string", "description": "Label for the product, e.g. Nm3."},
                "cohort_interval": {"type": "number", "exclusiveMinimum": 0, "default": 1,
                                    "description": "Feed accumulated over this interval closes "
                                                   "into one cohort and releases from its CLOSE, "
                                                   "so the mean delay is longer by up to half an "
                                                   "interval. Curve knots round onto the same "
                                                   "grid."},
                "time_unit": _time_unit("Unit the cohort interval and the curve ages are in."),
                "substrates": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "feed": {"type": ["string", "number"],
                                     "description": "Feed rate of this substrate: an expression, "
                                                    "usually FLOW of its feeder."},
                            "feed_time_unit": _time_unit("Unit the feed rate is per."),
                            "yield": {"type": "number", "minimum": 0,
                                      "description": "Product per unit of feed, e.g. 200 Nm3 of "
                                                     "biogas per tonne."},
                            "curve": {
                                "type": "array",
                                "description": "Release curve as [age, CUMULATIVE share] knots, "
                                               "ending at 1.0.",
                                "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                          "items": {"type": "number"}},
                            },
                            "half_life": {"type": "number", "exclusiveMinimum": 0,
                                          "description": "First-order release shortcut, "
                                                         "discretised into equal-width steps "
                                                         "instead of an explicit curve."},
                            "steps": {"type": "integer", "minimum": 1, "default": DEFAULT_STEPS,
                                      "description": "Steps the half-life curve is cut into."},
                            "tail": {"type": "number", "exclusiveMinimum": 0,
                                     "default": DEFAULT_TAIL,
                                     "description": "Half-lives the discretisation spans; the "
                                                    "remainder is renormalised in, so the yield "
                                                    "is conserved exactly."},
                            "components": {
                                "type": "object",
                                "additionalProperties": {"type": ["string", "number"]},
                                "description": "Share of this substrate's product that is each "
                                               "component (a feedstock's methane content). "
                                               "Evaluated once at run start.",
                            },
                            "initial_inventory": {"type": "number", "minimum": 0,
                                                  "description": "Feed already inside at t=0, "
                                                                 "released as one cohort."},
                            "initial_feed_rate": {"type": "number", "minimum": 0,
                                                  "description": "Start in the STEADY STATE for "
                                                                 "this feed rate: the output is "
                                                                 "feed x yield from t=0, so a "
                                                                 "year-long study needs no "
                                                                 "start-up transient."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "substrates"],
            "additionalProperties": False,
        },
        "fixed_cost": {
            "type": "object",
            "description": "A cost of the SCENARIO rather than of the run: capex, or "
                           "an annuity over a sized parameter. The amount is evaluated once at "
                           "run start, joins Total Cost and is itemised in the cost_summary "
                           "frame. It is never prorated to the observed window -- write the "
                           "share you want into the expression.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "amount": {"type": ["string", "number"],
                           "description": "A number, or an expression over the model's variables "
                                          "and data evaluated once at run start."},
                "category": {"type": "string",
                             "description": "Grouping shown in the cost_summary frame."},
                "capex": {"type": "boolean", "default": False,
                          "description": "An upfront investment: the investment case "
                                         "keeps it out of operating cost. Read by "
                                         "`nagasim.investment` only; Total Cost and every "
                                         "frame of a run are unchanged."},
            },
            "required": ["name", "amount"],
            "additionalProperties": False,
        },
        "investment": {
            "type": "object",
            "description": "OPTIONAL investment case: `naga invest` and the Studies "
                           "window report payback, NPV and ROI of every scenario against the "
                           "baseline. Per replication the run's Total Cost less its capex "
                           "fixed costs, and the throughput, are scaled from the observed "
                           "window to operating_hours_per_year.",
            "properties": {
                "baseline": {"type": "string", "default": "",
                             "description": "A scenario name; empty is the model as saved."},
                "horizon_years": {"type": "number", "exclusiveMinimum": 0, "default": 5,
                                  "description": "Years the saving is counted over; a "
                                                 "fraction counts its last year pro rata."},
                "discount_rate": {"type": "number", "default": 0.08,
                                  "description": "Per year, for the NPV."},
                "operating_hours_per_year": {"type": "number", "exclusiveMinimum": 0,
                                             "default": 4000,
                                             "description": "The observed window is scaled "
                                                            "to this."},
                "margin_per_part": {"type": "number", "default": 0,
                                    "description": "Money per extra good part; 0 is a pure "
                                                   "cost case."},
                "throughput": {"type": "string", "default": "",
                               "description": "A KPI key counting good parts (a "
                                              "`flatten_kpis` key such as \"user/Good "
                                              "parts\"); empty is the system's completed "
                                              "entities."},
            },
            "additionalProperties": False,
        },
        "subscript": {
            "type": "object",
            "description": "A dimension: a record with \"dims\": [name] is "
                           "flattened into one record per element when the model is built.",
            "properties": {
                "name": {"type": "string", "minLength": 1,
                         "description": "An identifier, written as \"[name]\" in a lookup."},
                "description": {"type": "string"},
                "elements": {"type": "array", "minItems": 1, "uniqueItems": True,
                             "items": {"type": ["string", "integer"]},
                             "description": "Element names, in output order."},
                "parent": {"type": "string",
                           "description": "Makes this a subrange of that dimension: "
                                          "its elements are a subset of the parent's. "
                                          "\"x[North]\" reads the owner's element inside an "
                                          "owner over North or its parent; SUM(x[North*]) sums "
                                          "the subset."},
                "map_to": {"type": "object",
                           "description": "Maps this dimension element-wise onto another "
                                          ": inside an owner over this one, "
                                          "\"x[dim]\" reads the corresponding element (and the "
                                          "reverse when it is one-to-one).",
                           "properties": {
                               "dim": {"type": "string", "minLength": 1},
                               "elements": {"type": "array",
                                            "items": {"type": ["string", "integer"]},
                                            "description": "The target element of each element, "
                                                           "in order; absent maps by position "
                                                           "(same size)."}},
                           "required": ["dim"], "additionalProperties": False},
            },
            "required": ["name", "elements"],
            "additionalProperties": False,
        },
        "auxiliary": {
            "type": "object",
            "description": "A named expression read as AUX(\"name\"): evaluated on "
                           "read, memoised per pass of the level engine. A flow reading an "
                           "auxiliary that reads LEVEL() or TNOW is re-integrated on the SD grid.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "dims": _DIMS,
                "elements": _ELEMENTS,
                "description": {"type": "string"},
                "expr": {"type": "string", "minLength": 1,
                         "description": "Any flow-rate expression; may call AUX() of another "
                                        "auxiliary (no cycles) and the inline delays."},
                "unit": {"type": "string", "description": "Label only."},
                "diagram": {"type": "object",
                            "description": "Position in the stock-and-flow view; ignored by "
                                           "the engine."},
            },
            "required": ["name", "expr"],
            "additionalProperties": False,
        },
        "delay": {
            "type": "object",
            "description": "A delay read as DELAY(\"name\"). "
                           "material: n stages each drained at S/(tau/n), the output is the "
                           "outflow; information (SMOOTH): n stages chasing the input, the output "
                           "is the last; both need simulation.sd. pipeline (DELAYP): the input "
                           "exactly tau later, no grid needed. sample (SAMPLE_IF_TRUE): holds the "
                           "last input read while condition was true, initial before the first "
                           "true reading -- \"input\"/\"delay_time\" do not apply, use "
                           "\"condition\"/\"input\". noise (NOISE_NORMAL): a normal draw clipped "
                           "to [lo, hi], redrawn once per re-evaluation from a stream fixed by this "
                           "delay's own name (CRN) -- use \"mean\"/\"sd\"/\"lo\"/\"hi\" instead of "
                           "\"input\"/\"delay_time\"; refused under RK4. Inline SMOOTH/SMOOTH3/"
                           "DELAY1/DELAY3/DELAYN/DELAYP/INITIAL/ACTIVE_INITIAL/SAMPLE_IF_TRUE/"
                           "TREND/FORECAST/NPV/NOISE_NORMAL calls in a level's initial value, a "
                           "flow rate, an auxiliary or a delay field are shorthand for these "
                           "records (INITIAL becomes a hidden level, not a delay).",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "dims": _DIMS,
                "elements": _ELEMENTS,
                "description": {"type": "string"},
                "input": {"type": ["string", "number"],
                          "description": "What is delayed: a rate for a material delay, a value "
                                         "otherwise; the sampled value for \"sample\" (required "
                                         "for every kind but \"noise\")."},
                "delay_time": {"type": ["string", "number"],
                               "description": "tau, in the model's base time unit (> 0); a "
                                              "pipeline reads it once at run start. Required for "
                                              "material/information/pipeline only."},
                "delay_time_unit": _unit_schema("time", "Unit `delay_time` is in."),
                "kind": {"type": "string",
                         "enum": ["material", "information", "pipeline", "sample", "noise"],
                         "default": "material"},
                "order": {"type": "integer", "minimum": 1, "default": 1,
                          "description": "Number of stages (material/information)."},
                "initial": {"type": ["string", "number"], "default": 0,
                            "description": "The output at t=0 (not \"noise\", which has none)."},
                "condition": {"type": ["string", "number"],
                              "description": "\"sample\" only: sampled again whenever true."},
                "mean": {"type": ["string", "number"], "description": "\"noise\" only."},
                "sd": {"type": ["string", "number"], "description": "\"noise\" only."},
                "lo": {"type": ["string", "number"], "description": "\"noise\" only: clip floor."},
                "hi": {"type": ["string", "number"], "description": "\"noise\" only: clip ceiling."},
                "unit": {"type": "string", "description": "Label only."},
                "diagram": {"type": "object",
                            "description": "Position in the stock-and-flow view; ignored by "
                                           "the engine."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        # Agents (PHASE5 AB-1, core/agents/, D93/D94).
        "agent_action": {"oneOf": [
            {"type": "object", "title": "set",
             "description": "Set self.<attribute> or a model variable to an expression's value.",
             "properties": {"target": {"type": "string", "minLength": 1},
                            "expr": {"type": ["string", "number"]}},
             "required": ["target", "expr"], "additionalProperties": False},
            {"type": "object", "title": "send",
             "description": "Send a message, delivered at the same instant (FIFO); the "
                            "receiving transition reads `payload`.",
             "properties": {"send": {"type": "string", "minLength": 1},
                            "to": {"$ref": "#/$defs/agent_target"},
                            "payload": {"type": ["string", "number"]}},
             "required": ["send", "to"], "additionalProperties": False},
            {"type": "object", "title": "enter",
             "description": "Create an entity at a block; entity.agent is this agent.",
             "properties": {"enter": {"type": "string", "minLength": 1},
                            "e_type": {"type": "string"}},
             "required": ["enter"], "additionalProperties": False},
            {"type": "object", "title": "transfer",
             "description": "Move an amount into or out of a level (the TRANSFER block's "
                            "instant move).",
             "properties": {"transfer": {"type": "string", "minLength": 1},
                            "amount": {"type": ["string", "number"]},
                            "mode": {"type": "string", "enum": ["remove", "add"]}},
             "required": ["transfer", "amount"], "additionalProperties": False},
            {"type": "object", "title": "signal",
             "description": "Raise a signal, releasing every HOLD (mode Signal) waiting on it.",
             "properties": {"signal": {"type": "string", "minLength": 1}},
             "required": ["signal"], "additionalProperties": False},
            {"type": "object", "title": "move_to",
             "description": "Move at the type's `speed` to a point, a network node or another "
                            "agent; arrival delivers an \"arrived\" message. A "
                            "literal [x, y] point, or an expression: a string constant "
                            "(\"'dock'\") for a node, [x, y] for a computed point, "
                            "AGENT(\"Pop\", i) for another agent.",
             "properties": {"move_to": {"anyOf": [
                 {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                 {"type": "string", "minLength": 1}]}},
             "required": ["move_to"], "additionalProperties": False},
            {"type": "object", "title": "spawn",
             "description": "Add agents to a population: count (default 1) and the "
                            "init expressions are evaluated in the spawner's scope; a newborn "
                            "starts in state (default its type's initial state), its own "
                            "attribute inits run first. More than the population's max_count "
                            "alive at once stops the run (E_POPULATION_FULL).",
             "properties": {"spawn": {"type": "string", "minLength": 1},
                            "count": {"type": ["string", "integer"]},
                            "state": {"type": "string"},
                            "init": {"type": "object",
                                     "additionalProperties": {"type": ["string", "number"]}}},
             "required": ["spawn"], "additionalProperties": False},
            {"type": "object", "title": "remove",
             "description": "Remove agents: \"self\" ends the firing there; any send "
                            "target works. A ref kept to a removed agent raises when used; "
                            "ALIVE(ref) tests it.",
             "properties": {"remove": {"$ref": "#/$defs/agent_target"}},
             "required": ["remove"], "additionalProperties": False},
            {"type": "object", "title": "step",
             "description": "Grid only: move by [dx, dy] whole cells, instantly; "
                            "off the edge of a non-torus grid the agent stays.",
             "properties": {"step": {"type": "array", "minItems": 2, "maxItems": 2,
                                     "items": {"type": ["integer", "string"]}}},
             "required": ["step"], "additionalProperties": False},
            {"type": "object", "title": "move_random",
             "description": "Grid only: to a neighbourhood cell drawn uniformly "
                            "from the agent's own stream; \"free\": only cells holding no "
                            "agent of its population (none: it stays).",
             "properties": {"move_random": {"type": "string", "enum": ["any", "free"]}},
             "required": ["move_random"], "additionalProperties": False},
            {"type": "object", "title": "link",
             "description": "Link self to each target (agents of its own "
                            "population; self is skipped).",
             "properties": {"link": {"$ref": "#/$defs/agent_target"}},
             "required": ["link"], "additionalProperties": False},
            {"type": "object", "title": "unlink",
             "description": "Drop self's links to each target (\"links\": all).",
             "properties": {"unlink": {"$ref": "#/$defs/agent_target"}},
             "required": ["unlink"], "additionalProperties": False},
            {"type": "object", "title": "python",
             "description": "Call fn(agent, api), a top-level def in the type's "
                            "`python`. Runs only with the caller's consent "
                            "(NAGASIM_ALLOW_AGENT_PYTHON=1); a model file cannot grant it.",
             "properties": {"python": {"type": "string", "minLength": 1}},
             "required": ["python"], "additionalProperties": False},
            {"type": "object", "title": "forward",
             "description": "Move `self.heading` degrees' worth forward by an "
                            "expression's distance; a continuous space only.",
             "properties": {"forward": {"type": ["string", "number"]}},
             "required": ["forward"], "additionalProperties": False},
            {"type": "object", "title": "turn",
             "description": "Add an expression's degrees to `self.heading`; a "
                            "continuous space only.",
             "properties": {"turn": {"type": ["string", "number"]}},
             "required": ["turn"], "additionalProperties": False},
            {"type": "object", "title": "face",
             "description": "Set `self.heading` toward a point, a network node or "
                            "another agent -- the same target shapes as move_to.",
             "properties": {"face": {"anyOf": [
                 {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                 {"type": "string", "minLength": 1}]}},
             "required": ["face"], "additionalProperties": False},
            {"type": "object", "title": "random_walk",
             "description": "Move forward by \"step\", after turning by "
                            "NORM(0, turn_sd) degrees (default 0, a straight walk).",
             "properties": {"random_walk": {
                 "type": "object",
                 "properties": {"step": {"type": ["string", "number"]},
                                "turn_sd": {"type": ["string", "number"]}},
                 "required": ["step"], "additionalProperties": False}},
             "required": ["random_walk"], "additionalProperties": False},
        ]},
        "agent_target": {
            "description": "\"self\", an index in the sender's population, an expression "
                           "giving an agent (AGENT(\"Pop\", i), entity.agent), or a population "
                           "with an optional condition on the recipient and a random pick; "
                           "\"links\" or {\"links\": true, ...}, self's linked "
                           "neighbours instead of a population.",
            "anyOf": [
                {"type": ["string", "integer"]},
                {"type": "object",
                 "properties": {"population": {"type": "string", "minLength": 1},
                                "where": {"type": "string"},
                                "random": {"type": "boolean"}},
                 "required": ["population"], "additionalProperties": False},
                {"type": "object",
                 "properties": {"links": {"const": True},
                                "where": {"type": "string"},
                                "random": {"type": "boolean"}},
                 "required": ["links"], "additionalProperties": False}],
        },
        "agent_tick": {
            "type": "object",
            "description": "A fixed-step schedule, ticks at phase + k * dt (k an "
                           "integer, strictly after the run start). synchronous: every agent "
                           "reads the tick-start state (on_tick for all, then tick transitions "
                           "for all); random: a fresh permutation per tick; sequential: index "
                           "order. A population's keys override its type's.",
            "properties": {"dt": {"type": "number", "exclusiveMinimum": 0},
                           "activation": {"type": "string", "enum": ["synchronous", "random",
                                                                     "sequential"],
                                          "default": "synchronous"},
                           "phase": {"type": "number", "minimum": 0, "default": 0},
                           "time_unit": _unit_schema("time", "Unit `dt` and `phase` are in.")},
            "additionalProperties": False,
        },
        "agent_state": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "entry": {"type": "array", "items": {"$ref": "#/$defs/agent_action"}},
                "exit": {"type": "array", "items": {"$ref": "#/$defs/agent_action"}},
                "parent": {"type": "string", "minLength": 1,
                           "description": "The composite state this one nests in; "
                                          "a state is composite iff another names it here."},
                "initial": {"type": "string", "minLength": 1,
                            "description": "Composite only: the child entered first "
                                           "(default: its first child in declaration order)."},
                "color": {"type": "string"},
                "diagram": {"type": "object", "description": "Editor position; ignored by the "
                                                             "engine."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "agent_transition": {
            "type": "object",
            "description": "Exactly one trigger: timeout (a duration from entry), rate (per "
                           "agent per time unit: shared, own or own * shared), condition "
                           "(re-checked after each agent event), message (a name, \"\" for "
                           "any; guard and actions read `payload`) or tick (a condition checked "
                           "at each of the population's ticks, \"\" for every tick).",
            "properties": {
                "from": {"type": "string"}, "to": {"type": "string"},
                "timeout": {"type": ["string", "number"]},
                "timeout_unit": _unit_schema("time", "Unit `timeout` is in."),
                "rate": {"type": ["string", "number"]},
                "rate_unit": _unit_schema("rate", "Time unit `rate` is per."),
                "condition": {"type": "string"},
                "message": {"type": "string"},
                "tick": {"type": "string"},
                "guard": {"type": "string"},
                "scan": {"type": "number", "exclusiveMinimum": 0,
                         "description": "Re-check a rate or condition every scan time units "
                                        "(for inputs that move outside the agents)."},
                "scan_unit": _unit_schema("time", "Unit `scan` is in."),
                "actions": {"type": "array", "items": {"$ref": "#/$defs/agent_action"}},
                "history": {"type": "string", "enum": ["shallow", "deep"],
                            "description": "Composite target only: re-enter at the "
                                           "child (shallow) or leaf (deep) it was left in."},
                "diagram": {"type": "object"},
            },
            "required": ["from", "to"],
            "additionalProperties": False,
        },
        "agent_type": {
            "type": "object",
            "description": "A statechart run for every agent of a population; parameters are "
                           "names in its expressions, self.<attribute> reads the agent's own "
                           "columns (plus self.state, self.id).",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "parameters": {"type": "object", "additionalProperties": {"type": "number"}},
                "attributes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1},
                                   "init": {"default": "0",
                                            "anyOf": [{"type": ["string", "number"]}, {
                                                "type": "object",
                                                "description": "Row i of the "
                                                    "population's own table for agent i.",
                                                "properties": {"column": {"type": "string",
                                                                          "minLength": 1}},
                                                "required": ["column"],
                                                "additionalProperties": False}]},
                                   "kind": {"type": "string", "enum": ["number", "category"],
                                            "default": "number",
                                            "description": "\"category\" stores init/"
                                                "set as one of \"categories\" (a closed set); "
                                                "self.<attr> reads back the category name."},
                                   "categories": {"type": "array", "items": {"type": "string",
                                                                             "minLength": 1},
                                                  "minItems": 2, "uniqueItems": True,
                                                  "description": "Required when kind is "
                                                      "\"category\"."}},
                    "required": ["name"], "additionalProperties": False}},
                "states": {"type": "array", "items": {"$ref": "#/$defs/agent_state"}},
                "transitions": {"type": "array", "items": {"$ref": "#/$defs/agent_transition"}},
                "initial_state": {"type": "string"},
                "space": {"anyOf": [{"type": "null"}, {
                    "type": "object",
                    "description": "Continuous 2D bounds, or the model's own "
                                   "\"network\" section; A grid of cells with "
                                   "patches.",
                    "properties": {
                        "kind": {"type": "string", "enum": ["continuous", "network", "grid"]},
                        "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4,
                                  "maxItems": 4,
                                  "description": "[x0, y0, x1, y1], continuous only."},
                        "edge": {"type": "string", "enum": ["none", "clamp", "wrap", "bounce"],
                                 "default": "none",
                                 "description": "Continuous only: what a move_to/"
                                     "forward/random_walk step does at the bounds -- pass "
                                     "through, clamp inside, wrap to the opposite edge, or "
                                     "bounce the heading."},
                        "size": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                 "minItems": 2, "maxItems": 2,
                                 "description": "Grid only: [w, h] cells."},
                        "cell": {"type": "number", "exclusiveMinimum": 0, "default": 1,
                                 "description": "Grid only: a cell's side; agents sit at cell "
                                                "centres ((cx + 0.5) * cell, ...)."},
                        "torus": {"type": "boolean", "default": False,
                                  "description": "Grid only: the edges wrap."},
                        "neighbourhood": {"type": "string", "enum": ["moore", "von_neumann"],
                                          "default": "moore",
                                          "description": "Grid only: the 8 or 4 cells "
                                              "NEIGHBOURS, move_random and diffuse use."},
                        "patches": {"type": "array", "description": "Grid only: per-cell "
                                    "values read by PATCH(\"name\"[, dx, dy]) and written "
                                    "by a set of \"patch.<name>\".",
                                    "items": {"type": "object", "properties": {
                                        "name": {"type": "string", "minLength": 1},
                                        "init": {"type": ["number", "string"], "default": 0,
                                                 "description": "A number, or an expression "
                                                     "of cx, cy (e.g. TABLE(...))."},
                                        "diffuse": {"type": "number", "minimum": 0,
                                                    "maximum": 1, "default": 0},
                                        "evaporate": {"type": "number", "minimum": 0,
                                                      "maximum": 1, "default": 0},
                                        "grow": {"type": "object", "description":
                                                 "Each patch tick a cell below max gains rate, "
                                                 "clipped at max (default: its initial value).",
                                                 "properties": {
                                                     "rate": {"type": "number", "minimum": 0},
                                                     "max": {"type": "number"}},
                                                 "required": ["rate"],
                                                 "additionalProperties": False}},
                                        "required": ["name"], "additionalProperties": False}},
                        "patch_tick": {"type": "number", "exclusiveMinimum": 0,
                                       "description": "Grid only: every patch diffuses, "
                                                      "evaporates, then grows at k * patch_tick."},
                        "time_unit": _time_unit("Unit `patch_tick` is in."),
                    },
                    "required": ["kind"], "additionalProperties": False}]},
                "speed": {"type": ["string", "number"], "default": "1",
                         "description": "An expression (self, parameters, constants) a move_to "
                                        "evaluates once, in the space's own units per time unit."},
                "speed_unit": _unit_schema("speed", "Unit `speed` is in."),
                "tick": {"$ref": "#/$defs/agent_tick"},
                "let": {"type": "object", "additionalProperties": {"type": ["string", "number"]},
                        "description": "Tick-local values, {name: expression}, "
                                       "evaluated in order once per agent per tick before "
                                       "on_tick; read as a bare name by on_tick and the tick "
                                       "transitions."},
                "on_tick": {"type": "array", "items": {"$ref": "#/$defs/agent_action"},
                            "description": "Actions every agent runs at each tick, "
                                           "before its tick transitions."},
                "python": {"type": "string",
                           "description": "Def statements only; {python: <fn>} "
                                          "actions call fn(agent, api). Parsed at validation, "
                                          "never run there; a run needs the caller's consent "
                                          "(NAGASIM_ALLOW_AGENT_PYTHON=1)."},
            },
            "required": ["name", "states"],
            "additionalProperties": False,
        },
        "population": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "type": {"type": "string", "minLength": 1},
                "count": {"anyOf": [{"type": "integer", "minimum": 0}, {
                    "type": "object",
                    "description": "The number of rows of a model table (one agent "
                                   "per row), or the sum of one of its columns (a group table).",
                    "properties": {"table": {"type": "string", "minLength": 1},
                                   "column": {"type": "string", "minLength": 1}},
                    "required": ["table"], "additionalProperties": False}]},
                "max_count": {"type": "integer", "minimum": 0,
                              "description": "The most agents alive at once when "
                                             "spawn actions add some (>= count); default count."},
                "tick": {"$ref": "#/$defs/agent_tick"},
                "network": {
                    "type": "object",
                    "description": "The initial links between this population's "
                                   "agents, drawn at run start from its own network stream. "
                                   "random: G(n, p) (p, or mean_degree = p (n - 1)); "
                                   "small_world: Watts-Strogatz ring of k neighbours, each link "
                                   "rewired with probability beta; scale_free: Barabasi-Albert, "
                                   "m links per new agent; table: a model table's \"from\"/"
                                   "\"to\" columns (agent indices); none: no initial links.",
                    "properties": {
                        "kind": {"type": "string", "enum": ["none", "random", "small_world",
                                                            "scale_free", "table"]},
                        "directed": {"type": "boolean", "default": False,
                                     "description": "random and table only."},
                        "link_on_spawn": {"type": "boolean", "default": False,
                                          "description": "A newborn starts linked to the agent "
                                                         "of this population that spawned it."},
                        "p": {"type": "number", "minimum": 0, "maximum": 1},
                        "mean_degree": {"type": "number", "minimum": 0},
                        "k": {"type": "integer", "minimum": 2},
                        "beta": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
                        "m": {"type": "integer", "minimum": 1},
                        "table": {"type": "string", "minLength": 1}},
                    "required": ["kind"], "additionalProperties": False},
                "color_by": {"type": "string", "minLength": 1,
                            "description": "A declared categorical attribute; "
                                "playback colours the population's tokens by its current "
                                "category instead of by state (2D and 3D)."},
                "colors": {"type": "object", "additionalProperties": {"type": "string"},
                          "description": "{category: colour name}, the palette color_by draws "
                              "from -- the same names a statechart state's own \"color\" uses. "
                              "Presentational only, like that field."},
                "initial": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"state": {"type": "string"},
                                   "count": {"type": "integer", "minimum": 0}},
                    "required": ["state", "count"], "additionalProperties": False},
                    "description": "The first agents by index start in these states, the rest "
                                   "in the type's initial state."},
                "placement": {
                    "type": "object",
                    "description": "Only meaningful when the type has a space: "
                                   "default {\"kind\": \"random\"} (uniform over the space's own "
                                   "bounds, or a uniform random network node, both from the "
                                   "agent's own stream).",
                    "properties": {
                        "kind": {"type": "string", "enum": ["random", "table"]},
                        "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4,
                                  "maxItems": 4,
                                  "description": "Overrides the space's own bounds (continuous, "
                                                 "kind random)."},
                        "points": {"type": "array", "description": "[[x, y], ...], one per agent "
                                  "by index, cycled (continuous, kind table); on a grid, cells "
                                  "[cx, cy].",
                                  "items": {"type": "array", "items": {"type": "number"},
                                            "minItems": 2, "maxItems": 2}},
                        "nodes": {"type": "array", "items": {"type": "string"},
                                 "description": "Network node names, one per agent by index, "
                                                "cycled (network, kind table)."},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["name", "type", "count"],
            "additionalProperties": False,
        },
        "sequence": {
            "type": "object",
            "description": "A routing: an ordered list of operations a product "
                           "follows through STATION blocks. A ROUTE block sends an entity whose "
                           "`sequence` attribute names this one to each step's station in turn, "
                           "assigning that step's \"values\" as entity attributes on the way.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "steps": {
                    "type": "array", "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "station": {"type": "string", "minLength": 1,
                                        "description": "Name of a STATION block; inside a submodel "
                                                       "instance that is the flattened "
                                                       "\"instance/station\" path."},
                            "values": {
                                "type": "object",
                                "additionalProperties": {"type": ["number", "string", "boolean"]},
                                "description": "Attributes assigned to the entity when it is sent "
                                               "to this step, e.g. {\"process_time\": 3}."},
                        },
                        "required": ["station"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "steps"],
            "additionalProperties": False,
        },
        "network": {
            "type": "object",
            "description": "The path network: nodes at real layout coordinates and "
                           "the segments joining them. Travel between two places is the shortest "
                           "path along the segments, so a distance is geometry, not a guess.",
            "properties": {
                "length_factor": {
                    "type": "number", "exclusiveMinimum": 0, "default": 1.0,
                    "description": "Multiplies the straight-line distance when a segment declares "
                                   "no explicit length. 1.0 is straight aisles."},
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "capacity": {"type": ["integer", "null"], "minimum": 1,
                                         "description": "Recorded, not enforced in this build."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from": {"type": "string", "minLength": 1},
                            "to": {"type": "string", "minLength": 1},
                            "length": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                       "description": "Null takes the straight-line distance x "
                                                      "length_factor."},
                            "length_unit": _unit_schema("length", "Unit `length` is in."),
                            "two_way": {"type": "boolean", "default": True},
                            "speed": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                      "description": "Speed LIMIT on this segment; it changes "
                                                     "travel time, never the route chosen."},
                            "speed_unit": _unit_schema("speed", "Unit `speed` is in."),
                            "capacity": {"type": ["integer", "null"], "minimum": 1,
                                         "description": "Vehicles allowed on it at once (zone "
                                                        "control, both directions of a two-way "
                                                        "segment together); null is unlimited "
                                                        "."},
                        },
                        "required": ["from", "to"],
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "motion_profile": {
            "type": "object",
            "description": "How something moves (automation sub-project 1): a robot, a "
                           "person or a mechanism, named by a MOTION block's `profile`. The rules "
                           "are the same for every kind.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "kind": {"type": "string", "enum": ["robot", "human", "mechanism"],
                         "default": "robot"},
                "speed": {"type": "number", "exclusiveMinimum": 0, "description": "m/s."},
                "speed_unit": _unit_schema("speed", "Unit `speed` and `collaborative_speed` "
                                           "are in.", "m/s"),
                "accel": {"type": "number", "exclusiveMinimum": 0, "description": "m/s^2."},
                "payload_max": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                "description": "kg; null or absent = no payload derating."},
                "payload_unit": _unit_schema("mass", "Unit `payload_max` is in.", "kg"),
                "payload_derate": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                   "maximum": 1, "default": 1.0,
                                   "description": "Speed fraction at payload_max, linear from "
                                                  "1.0 at 0 kg."},
                "collaborative_speed": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                        "description": "m/s; required when a block uses hrc "
                                                       "\"collaborative\" or \"separation\"."},
            },
            "required": ["name", "speed", "accel"],
            "additionalProperties": False,
        },
        "fleet": {
            "type": "object",
            "description": "A pool of vehicles that carry entities on the network. A "
                           "fleet IS a resource under its own name, so schedules, failures, "
                           "utilization and costs all work on it unchanged.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "default": 1},
                "kind": {"type": "string", "enum": ["vehicle", "worker"], "default": "vehicle",
                         "description": "\"worker\" lets a SEIZE/PROCESS summon a "
                                        "unit with `worker_fleet`; the pool is unchanged."},
                "motion": {"type": "string", "enum": ["network", "gantry"], "default": "network",
                          "description": "\"gantry\" travels directly between two "
                                         "nodes' coordinates (`speed_x`/`speed_y`/`hoist_time`), "
                                         "ignoring the path network and segment capacity; cannot "
                                         "be kind \"worker\"."},
                "speed_x": {"type": "number", "exclusiveMinimum": 0,
                           "description": "Gantry only: speed along x, distance per model time "
                                          "unit."},
                "speed_y": {"type": "number", "exclusiveMinimum": 0,
                           "description": "Gantry only: speed along y, distance per model time "
                                          "unit."},
                "hoist_time": {"type": "number", "minimum": 0, "default": 0,
                              "description": "Gantry only: fixed time added to every hop (raising "
                                             "and lowering the load), in `time_unit`."},
                "units": {
                    "type": "array", "minItems": 1,
                    "description": "Named units, replacing `count` when given.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "skills": {"type": "array", "items": {"type": "string"},
                                       "description": "What `worker_skill` may ask for."},
                            "home": {"type": "string",
                                     "description": "Node this unit starts at and parks at; "
                                                    "defaults to the fleet's `home`."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "loads": {"type": "integer", "minimum": 1, "default": 1,
                          "description": "Entities one vehicle may carry on a tour: up to "
                                         "loads - 1 waiting TRANSPORTs ride with the one granted "
                                         "the vehicle. 1 on a worker fleet."},
                "arm": {"type": "string",
                        "description": "Name of a `motion_profiles` record: the fleet is a "
                                       "mobile cobot, which loads and unloads a STATION that "
                                       "declares `tending` with its arm. Empty or "
                                       "absent: an ordinary fleet. Needs kind \"vehicle\", "
                                       "motion \"network\" and loads 1."},
                "exchange": {"type": "boolean", "default": False,
                             "description": "Arm fleets only: a second holding place, so "
                                            "a cobot arriving loaded at a machine that holds a "
                                            "finished part picks it, places its own and "
                                            "carries the finished one on."},
                "speed": {"type": "number", "exclusiveMinimum": 0,
                          "description": "Loaded speed, distance per model time unit."},
                "speed_unit": _unit_schema("speed", "Unit of every speed of the fleet."),
                "speed_empty": {"type": "number", "exclusiveMinimum": 0,
                                "description": "Speed when driving to a pick-up; defaults to "
                                               "`speed`."},
                "load_time": {"type": "string", "description": "Expression, in `time_unit`."},
                "unload_time": {"type": "string", "description": "Expression, in `time_unit`."},
                "time_unit": _time_unit("Unit the load and unload times are in."),
                "home": {"type": "string",
                         "description": "Node the vehicles start at, and return to under "
                                        "`park: \"home\"`."},
                "park": {"type": "string", "enum": ["stay", "home"], "default": "stay"},
                "capacity_schedule": {"type": "string",
                                      "description": "A \"capacity\"-kind schedule: how many "
                                                     "vehicles are on shift."},
                "dispatch": {"type": "string", "enum": ["fifo", "nearest"], "default": "fifo",
                             "description": "Whose request is served when a vehicle frees. "
                                            "Priority is the primary key under both."},
                "select": {"type": "string", "enum": ["nearest", "longest_idle", "cyclic"],
                           "default": "nearest",
                           "description": "Which idle vehicle serves a granted request."},
                "cost_busy": {"type": "number"},
                "cost_idle": {"type": "number"},
                "cost_per_use": {"type": "number"},
                "cost_time_unit": {"type": "string", "enum": list(UNITS)},
                "power": _power_schema("vehicle"),
                "graphic": {"type": "string",
                            "description": "Inline SVG vehicle picture (24 x 24 artboard, facing "
                                           "east) drawn for each unit on the floor plan; the "
                                           "editor's generic vehicle when absent."},
                "length_m": {"type": "number", "minimum": 0,
                             "description": "Vehicle length in metres for the floor-plan marker "
                                            "(default 2.4)."},
                "battery": {
                    "type": "object",
                    "description": "Each vehicle carries a battery: it drains at the "
                                   "fleet's `power` kW per state and recharges at a charger. "
                                   "State of charge (SoC) values are fractions, 0 to 1.",
                    "properties": {
                        "capacity_kwh": {"type": "number", "exclusiveMinimum": 0},
                        "initial": {"type": "number", "minimum": 0, "maximum": 1, "default": 1.0,
                                    "description": "SoC at the start of the run."},
                        "low": {"type": "number", "minimum": 0, "maximum": 1,
                                "description": "A vehicle freed below this SoC goes to charge."},
                        "target": {"type": "number", "minimum": 0, "maximum": 1, "default": 1.0,
                                   "description": "SoC charged to; above `low`."},
                        "strategy": {"type": "string",
                                     "enum": ["threshold", "opportunity", "swap", "scheduled"],
                                     "default": "threshold"},
                        "opportunity_below": {
                            "type": ["number", "null"], "minimum": 0, "maximum": 1,
                            "description": "Opportunity only: an idle vehicle below it charges "
                                           "when a point is free, and stays dispatchable."},
                        "schedule": {"type": ["string", "null"],
                                     "description": "Scheduled only: a `schedules` record; "
                                                    "vehicles freed while its value is > 0 "
                                                    "charge until `target` or the window's end."},
                        "chargers": {"type": "array", "items": {"type": "string"},
                                     "description": "Chargers the fleet may use; default every "
                                                    "charger of the kind its strategy needs."},
                    },
                    "required": ["capacity_kwh", "low"],
                    "additionalProperties": False,
                },
            },
            "required": ["name", "speed"],
            "additionalProperties": False,
        },
        "charger": {
            "type": "object",
            "description": "A charger or a battery-swap station at a network node (automation "
                           "sub-project 2), used by fleets that declare a `battery`.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "node": {"type": "string", "description": "The network node it stands at."},
                "kind": {"type": "string", "enum": ["charge", "swap"], "default": "charge"},
                "points": {"type": "integer", "minimum": 1, "default": 1,
                           "description": "Vehicles served at once."},
                "power_kw": {"type": "number", "exclusiveMinimum": 0,
                             "description": "kW per point; for \"swap\", the rate a removed "
                                            "pack recharges at."},
                "knee": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.8,
                         "description": "Above this SoC the power falls linearly."},
                "taper_floor": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                                "default": 0.1,
                                "description": "Share of `power_kw` left at 100 %."},
                "efficiency": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                               "default": 1.0,
                               "description": "Grid kWh = battery kWh / efficiency."},
                "swap_time": {"type": "string",
                              "description": "Swap only: expression, in `swap_time_unit`."},
                "swap_time_unit": _time_unit("Unit `swap_time` is in."),
                "spare_packs": {"type": ["integer", "null"], "minimum": 1,
                                "description": "Swap only: charged packs in stock at the start; "
                                               "a swap waits for one. Absent = unlimited."},
            },
            "required": ["name", "node", "power_kw"],
            "additionalProperties": False,
        },
        "failure": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "resources": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "uptime": {"$ref": "#/$defs/duration"},
                "downtime": {"$ref": "#/$defs/duration"},
            },
            "required": ["name", "resources", "uptime", "downtime"],
            "additionalProperties": False,
        },
        "scenario": {
            "type": "object",
            "description": "A named set of parameter overrides applied to the base model.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "overrides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string",
                                     "description": "Dotted override path, e.g. "
                                                    "\"resources.Server.capacity\" or "
                                                    "\"blocks.process_1.params.dist_params.rate\"."},
                            "value": {},
                        },
                        "required": ["path", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "reality_check": {
            "type": "object",
            "description": "A Vensim-style reality check: a test input imposed on "
                           "the model (a scenario-style override map), then a constraint the "
                           "model is expected to keep. Run by `nagasim.modelcheck`, one "
                           "deterministic replication per check.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "inputs": {
                    "type": "object",
                    "additionalProperties": {},
                    "description": "Override path -> value, the same grammar a scenario "
                                   "override's \"path\" uses (e.g. "
                                   "\"auxiliaries.Workforce.expr\": \"0\").",
                },
                "constraint": {"type": "string", "minLength": 1,
                               "description": "A boolean expression over LEVEL()/AUX()/FLOW() "
                                              "and TNOW, checked at every sampled instant the "
                                              "\"when\" below selects."},
                "when": {"type": "string", "default": "always",
                         "description": "\"always\" (every sample), \"final\" (the last sample "
                                        "only) or \"after <t>\" (samples at or after time t)."},
            },
            "required": ["name", "inputs", "constraint"],
            "additionalProperties": False,
        },
        "assertion": {
            "type": "object",
            "description": "A model assertion: pins one KPI (an "
                           "`experiments.flatten_kpis` key) against a comparison operator and "
                           "value, checked over its own replications by a Student-t confidence "
                           "interval. Run by `nagasim.modelcheck.check`, `naga check`.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "kpi": {"type": "string", "minLength": 1,
                       "description": "A KPI key from `experiments.flatten_kpis`, e.g. "
                                      "\"system/Throughput (per time unit)\" or "
                                      "\"resource/Server/Usage % (snapshot)\"."},
                "op": {"type": "string", "enum": ["<", "<=", ">", ">=", "=="],
                      "description": "Passes when the WHOLE confidence interval clears the "
                                     "bound (\"==\" means inside value +/- tolerance)."},
                "value": {"type": "number"},
                "replications": {"type": "integer", "minimum": 1, "default": 5,
                                 "description": "Assertions sharing a replication count are run "
                                                "in one batch."},
                "confidence": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1,
                              "default": 0.95},
                "tolerance": {"type": "number", "default": 0,
                             "description": "Half-width of the \"==\" band around value; "
                                            "ignored by the other operators."},
            },
            "required": ["name", "kpi", "op", "value"],
            "additionalProperties": False,
        },
        "dashboard_widget": {
            "type": "object",
            "description": "A dashboard widget: kpi/gauge read one "
                           "expression, line/bar read 1-4/1-8 labelled series, sampled at the "
                           "same instants as the timeseries sampler (`core/timeseries.py`). "
                           "\"row\"/\"col\"/\"span\" are layout hints for the editor's "
                           "dashboard panel only.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["kpi", "gauge", "line", "bar"]},
                "title": {"type": "string", "description": "Defaults to \"name\"."},
                "unit": {"type": "string"},
                "expr": {"type": "string",
                        "description": "kpi/gauge only: the expression sampled."},
                "series": {
                    "type": "array",
                    "description": "line/bar only: 1-4 series for a line widget, 1-8 for a bar.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1},
                            "expr": {"type": "string", "minLength": 1},
                        },
                        "required": ["label", "expr"],
                        "additionalProperties": False,
                    },
                    "minItems": 1, "maxItems": 8,
                },
                "min": {"type": "number", "description": "gauge only; default 0."},
                "max": {"type": "number", "description": "gauge only; required."},
                "thresholds": {"type": "array", "items": {"type": "number"},
                              "description": "gauge only: ascending values within "
                                             "[min, max]."},
                "row": {"type": "integer", "minimum": 0},
                "col": {"type": "integer", "minimum": 0},
                "span": {"type": "integer", "minimum": 1},
            },
            "required": ["name", "kind"],
            "additionalProperties": False,
        },
        "dashboard_control": {
            "type": "object",
            "description": "A game-mode control: a slider sets a resource's "
                           "capacity (within 0..its declared capacity), a variable or an SD "
                           "auxiliary over [min, max]; a decision answers the pending decision "
                           "of a Policy-mode DECIDE, one option label per outgoing link.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["slider", "decision"]},
                "title": {"type": "string", "description": "Defaults to \"name\"."},
                "target": {
                    "type": "object",
                    "description": "slider only: exactly one of capacity (a resource), "
                                   "variable, constant (an SD auxiliary).",
                    "properties": {"capacity": {"type": "string"},
                                   "variable": {"type": "string"},
                                   "constant": {"type": "string"}},
                    "minProperties": 1, "maxProperties": 1,
                    "additionalProperties": False,
                },
                "min": {"type": "number", "description": "slider only."},
                "max": {"type": "number", "description": "slider only; greater than min."},
                "step": {"type": "number", "exclusiveMinimum": 0, "default": 1,
                         "description": "slider only."},
                "policy": {"type": "string",
                           "description": "decision only: a Policy-mode DECIDE's policy."},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "decision only: one label per outgoing link."},
            },
            "required": ["name", "kind"],
            "additionalProperties": False,
        },
        "dashboard_input": {
            "type": "object",
            "description": "Present mode: one value a viewer may change before a "
                           "run, as an override of \"path\" checked by "
                           "`nagasim.dashboard.input_overrides`: a number within [min, max], "
                           "a switch (true/false) or one of the listed choices; never text. "
                           "Not `simulation.seed` or `simulation.replications`. Whether "
                           "the path addresses something of this kind in the model is "
                           "reported when the dashboard is used "
                           "(`nagasim.dashboard.check_inputs`, `E_DASHBOARD_INPUT`), not by "
                           "`naga validate`.",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": _DASH["text"],
                         "description": "An override path, e.g. resources.Server.capacity."},
                "label": {"type": "string", "maxLength": _DASH["label"]},
                "kind": {"type": "string", "enum": list(DASHBOARD_INPUT_KINDS)},
                "min": {"type": "number", "minimum": -_DASH["magnitude"],
                        "maximum": _DASH["magnitude"], "description": "number only."},
                "max": {"type": "number", "minimum": -_DASH["magnitude"],
                        "maximum": _DASH["magnitude"],
                        "description": "number only; greater than min, at most "
                                       f"{_DASH['steps']} steps away."},
                "step": {"type": "number", "exclusiveMinimum": 0, "default": 1,
                         "description": "number only; a hint for the control."},
                "unit": {"type": "string", "maxLength": _DASH["label"]},
                "choices": {
                    "type": "array", "minItems": 2, "maxItems": _DASH["choices"],
                    "description": "choice only.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1,
                                      "maxLength": _DASH["label"]},
                            "value": {"type": ["number", "boolean", "string"]},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "kind"],
            "additionalProperties": False,
        },
        "dashboard_result": {
            "type": "object",
            "description": "Present mode: a KPI to report first, by its "
                           "`experiments.flatten_kpis` key. The keys depend on the run, so only "
                           "the shape is checked.",
            "properties": {
                "kpi": {"type": "string", "minLength": 1, "maxLength": _DASH["text"]},
                "label": {"type": "string", "maxLength": _DASH["label"]},
                "unit": {"type": "string", "maxLength": _DASH["label"]},
                "better": {"type": "string", "enum": ["lower", "higher"]},
            },
            "required": ["kpi"],
            "additionalProperties": False,
        },
        "dashboard_page": {
            "type": "object",
            "description": "Present mode: one page of the Presenter. The item kinds "
                           "a page takes depend on its activity: "
                           + "; ".join(f"{activity}: {', '.join(kinds)}"
                                       for activity, kinds in DASHBOARD_ACTIVITIES.items())
                           + ". Positions are hints.",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": _DASH["text"]},
                "activity": {"type": "string", "enum": list(DASHBOARD_ACTIVITIES)},
                "items": {
                    "type": "array", "maxItems": _DASH["items"],
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string",
                                     "enum": sorted({kind for kinds in DASHBOARD_ACTIVITIES.values()
                                                     for kind in kinds})},
                            "ref": {"type": "string",
                                    "description": "input: its path; result: its kpi; widget "
                                                   "and control: its name."},
                            "row": {"type": "integer", "minimum": 0, "maximum": _DASH["grid"]},
                            "col": {"type": "integer", "minimum": 0, "maximum": _DASH["grid"]},
                            "span": {"type": "integer", "minimum": 1, "maximum": _DASH["grid"]},
                            "mode": {"type": "string", "enum": list(DASHBOARD_VIEW_MODES),
                                     "description": "view only."},
                            "names": {"type": "array", "maxItems": _DASH["names"],
                                      "items": {"type": "string"},
                                      "description": "scenarios only: the ones offered."},
                        },
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["name", "activity"],
            "additionalProperties": False,
        },
        "dashboard": {
            "type": "object",
            "description": "The dashboard: KPI tiles, gauges, time charts "
                           "and bars over the model's own expression language, shown during "
                           "playback and in the results. Since v29 also what Present "
                           "mode shows a person who did not build the model: \"inputs\", "
                           "\"results\", \"limits\" and \"pages\"; with no \"pages\" the "
                           "definition is generated (`nagasim.dashboard.default_dashboard`). "
                           "Absent for a model that declares none.",
            "properties": {
                "widgets": {"type": "array", "maxItems": _DASH["widgets"],
                            "items": {"$ref": "#/$defs/dashboard_widget"}},
                "controls": {"type": "array", "maxItems": _DASH["controls"],
                             "items": {"$ref": "#/$defs/dashboard_control"},
                             "description": "Game-mode controls."},
                "inputs": {"type": "array", "maxItems": _DASH["inputs"],
                           "items": {"$ref": "#/$defs/dashboard_input"}},
                "results": {"type": "array", "maxItems": _DASH["results"],
                            "items": {"$ref": "#/$defs/dashboard_result"}},
                "limits": {
                    "type": "object",
                    "description": "What a run on a viewer's behalf may cost.",
                    "properties": {
                        "replications": {"type": "integer", "minimum": 2},
                        "max_replications": {"type": "integer", "minimum": 2,
                                             "maximum": _DASH["replications"], "default": 30},
                    },
                    "additionalProperties": False,
                },
                "pages": {"type": "array", "maxItems": _DASH["pages"],
                          "items": {"$ref": "#/$defs/dashboard_page"}},
                "hidden": {"type": "array", "maxItems": _DASH["hidden"],
                           "items": {"type": "string", "maxLength": _DASH["text"]},
                           "description": "Generated entries the author removed "
                                          "(\"input:<path>\", \"result:<kpi>\", "
                                          "\"page:<name>\"): regenerating never adds them back."},
            },
            "required": ["widgets"],
            "additionalProperties": False,
        },
        "annotation": {
            "type": "object",
            "description": "A canvas note or frame; the engine ignores this section.",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": ["text", "frame"]},
                "position": {"$ref": "#/$defs/point"},
                "rect": {"type": "array", "items": {"type": "number"},
                         "minItems": 4, "maxItems": 4},
                "text": {"type": "string"},
                "title": {"type": "string"},
                "color": {"type": "string"},
                "font_size": {"type": "number"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["id", "kind"],
            "additionalProperties": False,
        },
        "submodel": {
            "type": "object",
            "description": "A reusable definition, instantiated by a SUBMODEL block and "
                           "flattened away before the engine sees the model.",
            "properties": {
                "info": {"type": "object"},
                "parameters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "default": {},
                            "description": {"type": "string"},
                            "type": {"type": "string",
                                     "description": "What the argument is. A string value is an "
                                                    "EXPRESSION unless the type is a literal one "
                                                    "(name/choice/string), where the word itself "
                                                    "is the value."},
                            "refers_to": {"type": "string",
                                          "description": "For type \"name\": the kind of record "
                                                         "the argument names (resource, "
                                                         "resource_set, schedule, table, "
                                                         "sequence, entity)."},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "scoped": {"type": "object",
                           "description": "Per-instance field overrides on `$`-scoped records."},
                "extends": {"type": "string", "minLength": 1,
                            "description": "Inherit another definition: "
                                           "this one is the parent's with its own blocks and "
                                           "parameters merged in by name (child wins), links "
                                           "by source/target/ports, `scoped` per record. "
                                           "Chains allowed; a cycle is E_EXTENDS_CYCLE."},
                "remove": {"type": "array", "items": {"type": "string"},
                           "description": "With `extends`: parent blocks to drop, with every "
                                          "parent link touching one; a `src->tgt` entry drops "
                                          "the parent links between those two blocks."},
                "blocks": {"type": "array", "items": {"$ref": "#/$defs/block"}},
                "links": {"type": "array", "items": {"$ref": "#/$defs/link"}},
                "annotations": {"type": "array", "items": {"$ref": "#/$defs/annotation"}},
            },
            "additionalProperties": False,
        },
        "data_source": {
            "type": "object",
            "description": "Where a data binding reads from. A relative `location` "
                           "resolves against the MODEL FILE's folder; `sql`/`json_url` are refused "
                           "unless the caller explicitly allows network access, and a literal "
                           "password in a URL is a validation error (use \"${NAGASIM_DATA_<NAME>}\").",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "enum": list(DATA_SOURCE_KINDS)},
                "location": {"type": "string", "minLength": 1,
                             "description": "Path relative to the model file, or a URL. For a "
                                            "live kind: what to read over the connection (a "
                                            "path), never a host."},
                "connection": {"type": "string",
                               "description": "Live kinds only: the NAME of a connection the "
                                              "person running the model defined in their own "
                                              "connections file. A model never holds an "
                                              "address or a credential."},
                "query": {"type": "string", "description": "SELECT, for the sqlite/sql kinds."},
                "sheet": {"type": "string", "description": "Worksheet name, for the xlsx kind."},
                "options": {"type": "object",
                            "description": "Extra keyword arguments for the pandas reader; "
                                           "for a live kind, that reader's own options."},
                "description": {"type": "string"},
            },
            "required": ["name", "kind", "location"],
            "additionalProperties": False,
        },
        "data_binding": {
            "type": "object",
            "description": "One model parameter read from a data source.",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                "column": {"type": "string"},
                "filter": {"type": "object",
                           "description": "{\"column\": value} or {\"column >=\": value}; "
                                          "comparisons only, never code."},
                "transform": {"type": "string",
                              "description": "One of " + ", ".join(DATA_TRANSFORMS) + ", or "
                                             + ", ".join(p + "<argument>"
                                                         for p in DATA_TRANSFORM_PREFIXES) + "."},
                "target": {"type": "string", "minLength": 1,
                           "description": "An override path, e.g. \"resources.Nurse.capacity\"."},
                "map": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "\"orders\" transform only: {order field: source column} "
                                       "for any field whose column is not already named after it."},
                "enabled": {"type": "boolean", "default": True},
                "description": {"type": "string"},
            },
            "required": ["name", "source", "transform", "target"],
            "additionalProperties": False,
        },
        "order": {
            "type": "object",
            "description": "A released job: what a CREATE block in \"Orders\" "
                           "arrival mode emits at `release`, carrying a due date the run reports "
                           "lateness and tardiness against. Times are in the model's BASE unit.",
            "properties": {
                "id": {"type": "string", "minLength": 1,
                       "description": "Unique; the KPI frames report the order under it."},
                "description": {"type": "string"},
                "release": {"type": ["number", "string"], "default": 0,
                            "description": "When the order is released into the model. An ISO "
                                           "timestamp instead of a number needs a calendar."},
                "due": {"type": ["number", "string", "null"],
                        "description": "Absolute due time, not a lead time; null = no due date. "
                                       "May be an ISO timestamp."},
                "time_unit": _time_unit("Unit `release` and `due` are in (a dated value "
                                        "ignores it)."),
                "quantity": {"type": "integer", "minimum": 0, "default": 1,
                             "description": "Entities this order releases."},
                "entity_type": {"type": "string",
                                "description": "Overrides the CREATE block's own e_type."},
                "sequence": {"type": "string",
                             "description": "Name of a declared routing the entities follow."},
                "priority": {"type": "number",
                             "description": "Carried as entity.priority."},
                "source": {"type": "string",
                           "description": "CREATE block that releases it; omit for the first "
                                          "CREATE in \"Orders\" mode."},
                "attributes": {
                    "type": "object",
                    "additionalProperties": {"type": ["number", "string", "boolean"]},
                    "description": "Further attributes assigned onto every entity of this order."},
                "plan": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                    "description": "{station: planned start}, carried as entity.plan and read by "
                                   "the \"Plan\" queue discipline (cpsat.propose writes it)."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "initial_state": {
            "type": "object",
            "description": "Warm start: what is already in the system at t=0.",
            "properties": {
                "time": {"type": "number",
                         "description": "Timestamp of the snapshot this state came from; recorded "
                                        "for the report, never shifts the clock."},
                "time_unit": _time_unit("Unit `time` is in."),
                "clock": {"type": "number", "minimum": 0,
                          "description": "The model time the run's clock starts at (default 0): "
                                         "the time a state was taken at. Statistics cover the "
                                         "run from there, as after a warm-up of that length."},
                "schedule_offset": {
                    "type": "number", "minimum": 0,
                    "description": "How far into its own cycle the calendar already is at model "
                                   "time 0, in base time units. Every schedule and "
                                   "every repeating time-series input is read at t + this, so a "
                                   "mid-shift warm start resumes the shift instead of "
                                   "restarting it."},
                "arrivals": {
                    "type": "object",
                    "description": "Per CREATE block: the arrivals already made and the "
                                   "absolute time of the next one.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"count": {"type": "integer", "minimum": 0},
                                       "next": {"type": "number"}},
                        "additionalProperties": False}},
                "resource_sets": {
                    "type": "object",
                    "description": "Per resource set: the index of the member a Cyclical set "
                                   "picked last (-1: none yet).",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"cursor": {"type": "integer", "minimum": -1}},
                        "additionalProperties": False}},
                "blocks": {
                    "type": "object",
                    "description": "Per block with a changeover: the setup key its last job "
                                   "left, so the next job pays a changeover or does not.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"setup": {"type": ["string", "number"]}},
                        "additionalProperties": False}},
                "variables": {"type": "object",
                              "description": "Model variable values to start from."},
                "fleets": {
                    "type": "object",
                    "description": "Where each vehicle of a fleet is: one record per unit, in "
                                   "unit order. Times are absolute.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "cursor": {"type": "integer", "minimum": -1,
                                       "description": "The unit a fleet with select "
                                                      "\"cyclic\" picked last, -1 for none."},
                            "vehicles": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "node": {"type": "string",
                                             "description": "The network node it stands at, or "
                                                            "left from when it drives."},
                                    "idle_since": {"type": "number"},
                                    "soc": {"type": "number", "maximum": 1,
                                            "description": "Its battery's state of charge."},
                                    "until": {"type": "number",
                                              "description": "When its loading or unloading "
                                                             "ends."},
                                    "drive": {"type": "object", "properties": {
                                        "to": {"type": "string"}, "started": {"type": "number"},
                                        "loaded": {"type": "boolean"}},
                                        "required": ["to", "started"],
                                        "additionalProperties": False},
                                    "charge": {"type": "object", "properties": {
                                        "charger": {"type": "string"},
                                        "held": {"type": "boolean"},
                                        "stage": {"enum": ["drive", "wait", "charge"]},
                                        "until": {"type": "number"},
                                        "wait": {"type": "integer", "minimum": 0}},
                                        "required": ["charger"],
                                        "additionalProperties": False}},
                                "required": ["node"], "additionalProperties": False}}},
                        "required": ["vehicles"], "additionalProperties": False}},
                "levels": {"type": "object", "additionalProperties": {"type": "number"},
                           "description": "Level values to start from: the state of "
                                          "charge of a battery, the stock on hand in a warehouse."},
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "block": {"type": "string", "minLength": 1,
                                      "description": "The block to inject into; a block inside a "
                                                     "submodel is \"<instance>/<inner>\"."},
                            "count": {"type": "integer", "minimum": 0},
                            "e_type": {"type": "string"},
                            "attributes": {"type": "object"},
                            "age": {"type": "number", "minimum": 0,
                                    "description": "Time already spent in the system before t=0."},
                            "age_unit": _time_unit("Unit `age` is in."),
                            "in_service": {"type": "integer", "minimum": 0},
                            "remaining": {"type": "array", "items": {"type": "number",
                                                                     "minimum": 0}},
                            "ends": {"type": "array", "items": {"type": "number"},
                                     "description": "Instead of `remaining`: the absolute time "
                                                    "each service in progress ends."},
                            "auto_cost": {"type": "number",
                                          "description": "The part of the entity's `cost` "
                                                         "attribute the engine charged itself."},
                            "holding_cost": {"type": "number",
                                             "description": "The holding cost within it."},
                            "member": {"type": "string",
                                       "description": "The member of the block's resource set "
                                                      "serving the entry's jobs in service."},
                            "deadline": {"type": "number",
                                         "description": "The absolute time the entry's waiting "
                                                        "entities give up (a block with "
                                                        "renege_after)."},
                            "since": {"type": "number",
                                      "description": "The absolute time the entry's waiting "
                                                     "entities joined the queue (at or before "
                                                     "the clock): their wait counts from it."},
                            "arrives": {"type": "number",
                                        "description": "The absolute time an entity on a link "
                                                       "reaches the block (count 1)."},
                            "held_by": {"type": "string",
                                        "description": "The block that still holds a waiting "
                                                       "entity: the BUFFER whose place it has, or "
                                                       "the station blocked after service that "
                                                       "keeps its unit until the entity is "
                                                       "accepted (count 1)."},
                            "held_since": {"type": "number",
                                           "description": "The absolute time the station in "
                                                          "held_by seized its unit."},
                            "place": {"type": "boolean",
                                      "description": "false: the entity waits for a place of the "
                                                     "block (a BUFFER, a queue_capacity) and has "
                                                     "none yet."},
                            "trip": {"type": "object",
                                     "description": "A load's trip, at its TRANSPORT block: the "
                                                    "origin and destination nodes, the target "
                                                    "STATION block and, once a vehicle has it, "
                                                    "its unit, the stage (pickup, load, carry, "
                                                    "unload) and the unload time already drawn.",
                                     "properties": {
                                         "origin": {"type": "string"},
                                         "destination": {"type": "string"},
                                         "target": {"type": "string"},
                                         "unit": {"type": "integer", "minimum": 0},
                                         "stage": {"enum": ["pickup", "load", "carry", "unload"]},
                                         "unload": {"type": "number", "minimum": 0}},
                                     "required": ["origin", "destination", "target"],
                                     "additionalProperties": False},
                        },
                        "required": ["block", "count"],
                        "additionalProperties": False,
                    },
                },
                "resources": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "down_until": {
                                "type": ["number", "string"],
                                "description": "Absolute time, or an ISO timestamp with a calendar."},
                            "busy": {"type": "integer", "minimum": 0,
                                     "description": "An observation: the units reported busy. It "
                                                    "must match the entities in service."},
                            "failures": {
                                "type": "object",
                                "description": "Per failure name: {\"next\": t}, the absolute time "
                                               "it fails, or {\"down_until\": t or null}, down "
                                               "by it now and repaired at t.",
                                "additionalProperties": {"type": "object"}}},
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "simulation": {
            "type": "object",
            "description": "Run settings; every one can be overridden per run.",
            "properties": {
                "time_limit": {"type": ["number", "null"],
                               "description": "Run length in base time units; null/0 means "
                                              "\"run until the model drains\", which only works "
                                              "for a model that can drain."},
                "warmup": {"type": "number", "minimum": 0, "default": 0,
                           "description": "Statistics are reset at this time."},
                "replications": {"type": "integer", "minimum": 1, "default": 1},
                "deterministic": {"type": "boolean", "default": False,
                                  "description": "Deterministic run mode: "
                                                 "every distribution samples its mean, "
                                                 "DECIDE-by-chance uses largest-remainder "
                                                 "allocation, a failure's TTF/TTR use their mean, "
                                                 "and replications are forced to 1. A mean-value "
                                                 "model, not a fluid limit; agent-based and "
                                                 "system-dynamics parts are unaffected."},
                "time_unit": {"type": "string", "enum": list(UNITS), "default": "minutes",
                              "description": "The model's BASE unit; every duration without its "
                                             "own unit is already in it."},
                "length_unit": {"type": "string", "enum": list(LENGTH_UNITS), "default": "meters",
                                "description": "The model's length unit: default unit of "
                                               "new length fields and of reported lengths."},
                "currency": {"type": "string", "default": "EUR",
                             "description": "Cost label only; never enters the arithmetic."},
                "energy_price": {"anyOf": [{"type": "number"}, {"type": "string"}], "default": 0,
                                 "description": "Money per kWh, a number or an expression, "
                                                "evaluated once at run start. "
                                                "Prices every resource/fleet's \"power\"."},
                "entity_log_limit": {"type": ["integer", "null"], "minimum": 0,
                                     "description": "Cap on retained entity-log rows."},
                "record_logs": {"type": "boolean", "default": True,
                                "description": "Record the per-EVENT entity/resource/store logs "
                                               ". False returns the three flat log "
                                               "frames empty and leaves every summary statistic "
                                               "exact -- what a big study wants."},
                "timeseries_points": {"type": ["integer", "null"], "minimum": 0,
                                      "description": "How many samples the time-series OUTPUT "
                                                     "sampler takes over a time-limited run "
                                                     "(default 200)."},
                "timeseries_interval": {"type": ["number", "null"], "exclusiveMinimum": 0,
                                        "description": "Fixed sampling interval for a draining "
                                                       "run, which has no horizon to divide."},
                "timeseries_counts": {"type": "array", "maxItems": 1000, "uniqueItems": True,
                                      "items": {"type": "string", "minLength": 1},
                                      "description": "Blocks whose departures the sampler "
                                                     "counts, as the series block/<name>/out "
                                                     "(flattened names). What a twin compares "
                                                     "a plant counter with; none by default."},
                "sd": {
                    "type": "object",
                    "description": "OPTIONAL system-dynamics step grid. "
                                   "Absent, rates change only at events. Present, every rate "
                                   "reading LEVEL(), FRACTION() or TNOW is re-integrated at "
                                   "each multiple of \"dt\"; static rates and events inside "
                                   "a step stay exact.",
                    "properties": {
                        "dt": {"type": "number", "exclusiveMinimum": 0,
                               "description": "Step, in \"time_unit\"."},
                        "method": {"type": "string", "enum": list(SD_METHODS),
                                   "default": "euler",
                                   "description": "euler, or rk4 as an effective rate over "
                                                  "each step (no random draws in a dynamic "
                                                  "rate)."},
                        "time_unit": _time_unit("Unit dt is in."),
                    },
                    "required": ["dt"],
                    "additionalProperties": False,
                },
                "calendar": {
                    "type": "object",
                    "description": "OPTIONAL real dates. Absent, the clock is a plain "
                                   "number and nothing changes. Present, model time 0 is "
                                   "\"start\" and weekday schedules, dated series and dated "
                                   "orders resolve against it.",
                    "properties": {
                        "start": {"type": "string",
                                  "description": "ISO 8601 instant model time 0 stands for. With "
                                                 "no offset it is read in \"timezone\", never in "
                                                 "the machine's local time."},
                        "timezone": {"type": "string", "default": "UTC",
                                     "description": "IANA name (Europe/Rome). The wall clock "
                                                    "weekday schedules and date labels use."},
                        "end": {"type": "string",
                                "description": "Run until this instant, instead of a "
                                               "\"time_limit\" length."},
                    },
                    "required": ["start"],
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    }


def _list_of(definition: str) -> dict:
    return {"type": "array", "items": {"$ref": f"#/$defs/{definition}"}}


_SECTION_PATHS = {"entities": ("entity_type",), "links": ("link",), "simulation": ("simulation",),
                  "investment": ("investment",), "simulation.sd": ("simulation", "sd"), "network": ("network",),
                  "network.segments": ("network", "segments", "items"),
                  "conversions.substrates": ("conversion", "substrates", "items"),
                  "agent_types.transitions": ("agent_transition",),
                  "agent_types.space": ("agent_type", "space", 1),
                  "levels.conveyor": ("level", "conveyor"), "initial_state": ("initial_state",),
                  "initial_state.entities": ("initial_state", "entities", "items"),
                  "agent_types.tick": ("agent_tick",), "populations.tick": ("agent_tick",)}


def _section_props(defs: dict, key: str) -> dict:
    path = _SECTION_PATHS.get(key) or (next(s.item for s in SECTIONS if s.name == key),)
    node = defs[path[0]]
    for part in path[1:]:
        node = (node["anyOf"][part] if isinstance(part, int) else node["items"]
                if part == "items" else node["properties"][part])
    return node.get("properties", {})


def _annotate_sections(defs: dict) -> None:
    """Section columns from `quantities.SECTION_FIELDS`: quantity, unit key,
    bounds, and each unit key's schema from the unit tables (units spec)."""
    for key, fields in SECTION_FIELDS.items():
        props = _section_props(defs, key)
        for spec in fields:
            prop = props.get(spec.name)
            if prop is None:
                continue
            if spec.quantity not in (None, "none"):
                prop["x-quantity"] = spec.quantity
                if spec.unit_field:
                    prop["x-unit-key"] = spec.unit_field
            for bound, name in ((spec.min, "minimum"), (spec.max, "maximum")):
                if bound is not None and "type" in prop and "exclusiveMinimum" not in prop:
                    prop.setdefault(name, bound)
            unit = spec.unit_field
            if unit and "." not in unit and unit in props and key != "simulation":
                sibling = next((p for p in fields if p.name == unit), None)
                own = getattr(sibling, "default", None)
                props[unit] = _unit_schema(spec.quantity, props[unit].get("description", ""),
                                           own if own not in (None, "", "base") else "base")


def build_json_schema() -> dict:
    """The current model file format as a draft 2020-12 JSON Schema.

    Built fresh on each call from BLOCK_REGISTRY / DISTRIBUTION_REGISTRY and the
    enumerations in `resource_manager`, `schedules`, `stats` and `timeunits`, so
    plugin blocks and distributions are included automatically.
    """
    record_kinds = ", ".join(RECORD_KINDS)
    defs = _defs()
    _annotate_sections(defs)
    arrival_modes = ", ".join(ARRIVAL_MODES)
    # Top-level properties from the section table (core/sections.py): a list of
    # the row's `$def` (named after its item, else its singular) unless shaped here.
    shaped = {
        "entities": _list_of("entity_type"),
        "network": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/network"}]},
        "dashboard": {"$ref": "#/$defs/dashboard"},
        "view": {
            "type": "object",
            "description": "Editor view state, written by the editor on save; "
                           "ignored by the engine. Absent keys are the defaults: Process tab, "
                           "schematic, every tab fitted to its contents.",
            "properties": {
                "tab": {"enum": ["process", "stock_and_flow", "statechart"]},
                "layout": {"type": "boolean"},
                "view_3d": {"type": "boolean"},
                "canvases": {
                    "type": "object",
                    "propertyNames": {"enum": ["process", "stock_and_flow", "statechart"]},
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"zoom": {"type": "number", "exclusiveMinimum": 0},
                                       "center": {"$ref": "#/$defs/point"}},
                        "additionalProperties": False},
                },
            },
            "additionalProperties": False,
        },
        "submodels": {"type": "object", "additionalProperties": {"$ref": "#/$defs/submodel"}},
        "background": {
            "anyOf": [
                {"type": "null"},
                {"type": "object",
                 "properties": {
                     "path": {"type": "string", "minLength": 1},
                     "position": {"$ref": "#/$defs/point"},
                     "scale": {"type": "number", "exclusiveMinimum": 0},
                     "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                     "locked": {"type": "boolean"},
                     "units_per_metre": {"type": ["number", "null"], "exclusiveMinimum": 0},
                 },
                 "required": ["path"],
                 "additionalProperties": False},
            ],
            "description": "Optional floor-plan image behind the canvas; ignored by the engine.",
        },
        "info": {
            "type": "object",
            "properties": {"description": {"type": "string"}, "author": {"type": "string"},
                           "notes": {"type": "string"},
                           # Editor view settings remembered with the model (PHASE4 G2, D82).
                           "labels": {"enum": ["all", "assets", "none"]},
                           "node_names": {"enum": ["hover", "always"]}},
            "additionalProperties": True,
        },
        "initial_state": {"$ref": "#/$defs/initial_state"},
        "simulation": {"$ref": "#/$defs/simulation"},
        "investment": {"$ref": "#/$defs/investment"},
        "outputs": {
            "type": "object",
            "description": "OPTIONAL extra output collection whose cost is opt-in "
                           ". Absent, none of it runs.",
            "properties": {
                "bottleneck": {
                    "type": "boolean", "default": False,
                    "description": "Collect per-resource ACTIVE intervals for the Roser "
                                   "bottleneck analyser (\"bottleneck_summary\"). Off by "
                                   "default: the intervals cost memory proportional to the "
                                   "event count.",
                },
                "schedule": {
                    "type": "boolean", "default": False,
                    "description": "Record one row per resource hold (\"schedule_log\") for "
                                   "the schedule Gantt and the plan export. "
                                   "Off by default; forced on in "
                                   "deterministic mode (\"simulation.deterministic\").",
                },
            },
            "additionalProperties": False,
        },
    }
    return {
        "$schema": _DRAFT,
        "$id": SCHEMA_ID,
        "title": f"Naga model, schema version {CURRENT_VERSION}",
        "description": (
            "A discrete-event model: blocks, the links between them, the resources they seize, "
            "and the run settings. Generated from the live registries "
            f"(nagasim.modelschema). RECORD kinds: {record_kinds}. CREATE arrival modes: "
            f"{arrival_modes}."),
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "maximum": CURRENT_VERSION,
                               "default": CURRENT_VERSION,
                               "description": "Older files migrate on load; a newer one is refused."},
            **{s.name: shaped.get(s.name) or _list_of(s.item or s.name[:-1])
               for s in SECTIONS},
        },
        "additionalProperties": False,
        "$defs": defs,
    }


# ---------------------------------------------------------------- CLI helpers
def main_schema(argv=None) -> int:
    """`naga schema [--out FILE]`: print or write the JSON Schema."""
    parser = argparse.ArgumentParser(
        prog="naga schema",
        description="Print the JSON Schema (draft 2020-12) of the model file format.")
    parser.add_argument("--out", help="write the schema here instead of printing it")
    args = parser.parse_args(argv)

    text = json.dumps(build_json_schema(), indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Schema written to {args.out}")
    else:
        print(text)
    return 0


def main_validate(argv=None) -> int:
    """`naga validate model.json [--json]`: print structured problems.
    Exit code 1 when any of them is an error."""
    from nagasim.core import schema as schema_mod

    parser = argparse.ArgumentParser(
        prog="naga validate",
        description="Validate a model file and report structured problems "
                    "(code, severity, path, hint).")
    parser.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the problems as a JSON array instead of one line each")
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.model).read_text(encoding="utf-8"))
    try:
        layout = schema_mod.migrate(raw)
    except schema_mod.ModelValidationError as exc:
        problems = [schema_mod.problem("E_SCHEMA_VERSION", message, "/schema_version",
                                       hint="save the file from this build, or upgrade Naga")
                    for message in exc.problems]
    else:
        # Against the RAW file as well: migrate() strips a data source's
        # "allow_network", and the point of the warning is to say it did nothing.
        problems = (schema_mod.consent_problems(raw)
                    + schema_mod.validate_model_structured(layout))

    if args.as_json:
        print(json.dumps([p.as_dict() for p in problems], indent=2))
    elif not problems:
        print(f"{args.model}: OK")
    else:
        for p in problems:
            print(f"{p.severity.upper()} {p.code} {p.path or '/'}: {p.message}"
                  + (f"\n    hint: {p.hint}" if p.hint else ""))
    return 1 if any(p.severity == "error" for p in problems) else 0
