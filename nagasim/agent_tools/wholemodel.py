"""Whole-model tools: `get_model_format` (what a model file holds, in one compact text
generated from the registries and the JSON Schema) and `write_model` (the complete model
file in one call, answered with its problems).

Why they exist: the LLM case study (`benchmarks/llm_study/`) found that building a model
block by block costs about sixteen round trips, each re-reading the whole conversation, while
a script is written in one. An agent that writes the file in one go and fixes it from one
short answer gets the same validated model file in three to five calls.

A model written here is untrusted input exactly like a file on disk: it is migrated, read
and validated by the same functions `open_model` and `naga validate` use, and it can name no
permission (`netguard`): a data source's own `allow_network` is stripped by the migration and
reported, and nothing in a layout widens the workspace's access.
"""
from __future__ import annotations

import copy
import json

from nagasim import examples, modelfmt
from nagasim.core import layout_tools
from nagasim.core import schema as schema_mod
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import DISTRIBUTION_REGISTRY
from nagasim.core.document import ModelDocument
from nagasim.core.expressions import FUNCTION_NOTES, FUNCTIONS

from .core import (MAX_PROBLEMS, ToolError, _brief_problems, _document_args, _first_sentence,
                   _problems, tool, workspace)

# The sections an overview spells out field by field; any other is one get_model_format call
# away. Chosen by what the study's twelve tasks and the bundled examples use most.
_CORE_SECTIONS = ("resources", "schedules", "variables", "entities", "levels", "flows",
                  "auxiliaries", "timeseries_inputs", "agent_types", "populations")
_EXAMPLE = "single_server"
_SKIP_FIELDS = {"diagram", "description", "position", "graphic", "icon", "shape", "color"}


def _schema() -> dict:
    from nagasim.modelschema import build_json_schema
    return build_json_schema()


def _brief(name: str) -> bool:
    """What the overview leaves to a detail call: a unit next to its value (the model's own
    unit applies when it is left out) and what only the editor draws."""
    return (name.endswith("_unit") and name != "time_unit") or name in _SKIP_FIELDS


def _note(name: str) -> str:
    """A function's note for the overview: its first clause, none when it only names the
    Python function it is (the name already says so)."""
    note = _first_sentence(FUNCTION_NOTES.get(name, "").split(";")[0], 90)
    return "" if note.startswith(("math.", "plain Python", "1-2 args", "variadic")) else note


def _value(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _def_of(full: dict, node: dict) -> dict | None:
    ref = node.get("$ref") or (node.get("items") or {}).get("$ref")
    return full["$defs"].get(ref.rsplit("/", 1)[-1]) if ref else None


def _field(full: dict, name: str, node: dict, required: bool, detail: bool, depth: int = 0) -> str:
    """One field of a JSON Schema object as `name*=default (a|b)`, an array of objects with
    its own fields in braces one level deep, and in detail its description's first sentence."""
    text = name + ("*" if required else "")
    if "default" in node:
        text += "=" + _value(node["default"])
    if "enum" in node:
        text += " (" + "|".join(str(v) for v in node["enum"]) + ")"
    inner = _def_of(full, node)
    if inner is not None and depth == 0 and "properties" in inner:
        fields = _fields(full, inner, False, depth + 1)
        text += (" [{" if node.get("type") == "array" else " {") + fields + (
            "}]" if node.get("type") == "array" else "}")
    if detail and node.get("description"):
        text += " -- " + _first_sentence(node["description"])
    return text


def _fields(full: dict, definition: dict, detail: bool, depth: int = 0) -> str:
    required = set(definition.get("required") or ())
    return ", ".join(_field(full, name, node, name in required, detail, depth)
                     for name, node in definition.get("properties", {}).items()
                     if name not in _SKIP_FIELDS and (detail or not _brief(name)))


def _section_line(full: dict, section: str, detail: bool) -> str:
    node = full["properties"].get(section)
    if node is None:
        raise ToolError("E_UNKNOWN_SECTION", f"'{section}' is not a model file section",
                        "use one of: " + ", ".join(full["properties"]))
    definition = _def_of(full, node) or node
    if "properties" not in definition:
        return f"{section}: see the JSON Schema (resource naga://reference/schema)"
    shape = "[{" + _fields(full, definition, detail) + "}]" if node.get("type") == "array" \
        else "{" + _fields(full, definition, detail) + "}"
    return f"{section}: {shape}"


def _block_line(name: str, detail: bool) -> str:
    spec = BLOCK_REGISTRY[name]
    summary = _first_sentence(spec.cls.__doc__, 160)
    if not detail:
        params = ", ".join(p.name + ("*" if p.required else "") + "=" + _value(p.default)
                           for p in spec.params if not p.name.endswith("unit"))
        return f"{name}{' (branches)' if spec.multi_output else ''}: {summary} | {params}"
    lines = [f"{name}{' (branches)' if spec.multi_output else ''}: "
             + " ".join((spec.cls.__doc__ or "").split())]
    lines += [f"  {p.name}{'*' if p.required else ''}={_value(p.default)} ({p.type.__name__})"
              + (f" -- {_first_sentence(p.description)}" if p.description else "")
              for p in spec.params]
    return "\n".join(lines)


def _example() -> str:
    """A bundled model in `naga fmt`'s form, positions left out: what a minimal valid file
    looks like, never typed by hand."""
    layout = modelfmt.minimal(examples.path(_EXAMPLE))
    for block in layout["blocks"]:
        block.pop("position", None)
    return _value({k: layout[k] for k in ("blocks", "links", "resources", "simulation")
                   if k in layout})


def _overview() -> str:
    full = _schema()
    simulation = full["$defs"]["simulation"]
    distributions = "; ".join(f"{name}({', '.join(p.name for p in spec.params)})"
                              for name, spec in sorted(DISTRIBUTION_REGISTRY.items()))
    functions = "; ".join(
        f"{name}/{low if high == low else f'{low}..' + ('n' if high is None else str(high))}"
        + (f": {_note(name)}" if _note(name) else "")
        for name, (_fn, _ctx, low, high) in sorted(FUNCTIONS.items()))
    others = [s for s in full["properties"] if s not in ("schema_version", "blocks", "links",
                                                          "simulation", *_CORE_SECTIONS)]
    parts = [
        "A model file is ONE JSON object; write it whole with write_model, which answers with "
        "its problems (code, JSON path, hint). Every key is optional; '*' marks a required "
        "field, '=v' a default you may leave out.",
        "Example (an M/M/1 queue): " + _example(),
        "blocks: [{name*, type*, params}] -- params as below; a block may have one outgoing link, "
        "a '(branches)' type several.",
        "links: [{source*, target*}] in order: a DECIDE's links are its branches (branch i takes "
        "probs[i] or conditions[i]; one more link than probs/conditions is the else). A "
        "SEIZE/PROCESS with balk_if/renege_after sends the entities that leave by its second link.",
        "simulation: {" + _fields(full, simulation, False) + "}. time_limit is the run length in "
        "time_unit; durations and rates without their own unit are in it.",
        *(_section_line(full, s, False) for s in _CORE_SECTIONS),
        # The study's tank task: a TNOW test on a one-minute grid switched late every hour.
        "A flow rate that switches on a timetable (a pump on a timer) is exact read with "
        "TS(\"name\") from a timeseries_inputs profile, one cycle given once: on 20 of every 60 "
        "is {\"name\": \"pump\", \"times\": [0, 20], \"values\": [1, 0], \"beyond\": \"repeat\", "
        "\"period\": 60} (without period an irregular profile repeats every times[-1] - times[0]); "
        "a TNOW test on an sd grid switches late by up to dt each time.",
        "Other sections (fields: get_model_format(sections=[...])): " + ", ".join(others) + ".",
        "Block types (TYPE: what it does | param=default; detail: get_model_format(blocks=[...])):",
        *(_block_line(name, False) for name in sorted(BLOCK_REGISTRY)),
        "Distributions, by name with their dist_params keys (each a number or an expression "
        "string): " + distributions + ".",
        "Expressions: restricted Python (+ - * / // % **, comparisons, and/or/not, a if c else b), "
        "names entity.<attr>, TNOW (or now), model variables. Functions (name/arity: note): "
        + functions + ".",
    ]
    return "\n".join(parts)


_document_args("get_model_format",
               blocks="Block types to describe in full (every parameter with its meaning); "
                      "omit for the overview.",
               sections="Model file sections to describe field by field, e.g. "
                        "[\"levels\", \"schedules\"]; omit for the overview.")


@tool(read_only=True)
def get_model_format(blocks: list | None = None, sections: list | None = None) -> dict:
    """What a model file holds, compactly: call it ONCE before write_model.

    With no argument: a minimal example file, the top-level sections with their fields,
    every block type with its parameters and defaults, every distribution with its
    parameter names and every expression function with what it reads. With `blocks` or
    `sections`: those in full, each field with its meaning. Generated from the registries
    and the JSON Schema (resource naga://reference/schema), so it is never out of date.

    Returns {"format": text}.
    """
    if not blocks and not sections:
        return {"format": _overview()}
    full = _schema()
    lines = []
    for name in blocks or ():
        if str(name).upper() not in BLOCK_REGISTRY:
            raise ToolError("E_UNKNOWN_BLOCK_TYPE", f"unknown block type: {name}",
                            f"use one of: {', '.join(sorted(BLOCK_REGISTRY))}")
        lines.append(_block_line(str(name).upper(), True))
    lines += [_section_line(full, str(s), True) for s in sections or ()]
    return {"format": "\n".join(lines)}


# ---------------------------------------------------------------- write_model
def _unknown_keys(raw: dict) -> list:
    """W_UNKNOWN_KEY for every key the JSON Schema does not know, at the top level, in a
    section's records and in the objects inside them one level down (`simulation.sd`).
    The document keeps only what it knows, so a misspelt key would vanish without a word
    (`"run_length"` for `simulation.time_limit`, `"resource"` for `"resources"`). Block
    params are left to the validator, which reports them as E_PARAM_UNKNOWN."""
    full = _schema()
    found = []

    def check(value, node, at, depth):
        if node and "oneOf" in node and isinstance(value, dict):     # a block: by its type
            node = next((v for v in node["oneOf"] if v.get("title") == str(value.get("type", "")).upper()), None)
        if not node or not isinstance(value, dict) or "properties" not in node:
            return
        known = node["properties"]
        for key, inner in value.items():
            if key not in known:
                if node.get("additionalProperties") is False:
                    found.append(schema_mod.problem(
                        "W_UNKNOWN_KEY", f"{at or '/'}: unknown key '{key}' is ignored",
                        f"{at}/{key}", hint="keys here: " + ", ".join(known)))
            elif depth < 2 and key != "params":
                sub = _def_of(full, known[key]) if "$ref" in known[key] or "items" in known[key] \
                    else known[key]
                items = inner if isinstance(inner, list) and known[key].get("type") == "array" else None
                for index, item in enumerate(items if items is not None else [inner]):
                    check(item, sub, f"{at}/{key}" + (f"/{index}" if items is not None else ""), depth + 1)

    check(raw, full, "", 0)
    return found


_document_args("write_model",
               model="Workspace name for the model: a new one is created, an open one replaced.",
               layout="The complete model file as one JSON object (get_model_format shows its "
                      "shape): blocks, links, resources, simulation, ...")


@tool()
def write_model(model: str, layout: dict) -> dict:
    """Write a WHOLE model in one call: the complete model file as one JSON object.

    The fast way to build a model: call get_model_format once, write the file, fix what the
    answer lists with one more write_model, then run_model and save_model. The layout is
    read and validated exactly as a model file on disk is; it replaces an open model of the
    same name as one undo step. Blocks given no position are laid out automatically.

    Returns {"ok": true when nothing is an error, "model", "blocks", "errors", "warnings",
    "requires_run_length", "problems": [{"code", "path", "message", "hint"}]}.
    """
    if not isinstance(layout, dict):
        raise ToolError("E_BAD_ARGUMENT", "`layout` must be a JSON object",
                        "send the whole model file as one object")
    raw = copy.deepcopy(layout)
    # The author writes the current format; a file without a version is read as the oldest.
    raw.setdefault("schema_version", schema_mod.CURRENT_VERSION)
    try:
        migrated = schema_mod.migrate(copy.deepcopy(raw))
        fresh = ModelDocument(migrated)
        if not any(isinstance(b, dict) and b.get("position") for b in raw.get("blocks") or ()):
            fresh.move_blocks(layout_tools.auto_layout(fresh))
        problems = (schema_mod.consent_problems(raw) + _unknown_keys(raw) + _problems(fresh))
    except schema_mod.ModelValidationError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ToolError("E_MODEL_SHAPE", f"the model could not be read: {type(exc).__name__}: {exc}",
                        "blocks and links are lists of objects, every block has a string name "
                        "and type; compare with get_model_format's example") from None
    ws = workspace()
    doc = ws.models.get(model)
    if doc is None:
        ws.models[model] = doc = fresh
        doc.dirty = True
    else:
        doc.replace_layout(fresh.to_layout(), "Write model")
    ws.results.pop(model, None)
    ws.asked.pop(model, None)       # what was asked of the old model says nothing of the new one
    errors = sum(1 for p in problems if p.severity == "error")
    return {"ok": not errors, "model": model, "blocks": len(doc.blocks), "errors": errors,
            "warnings": len(problems) - errors,
            "requires_run_length": schema_mod.requires_run_length(doc.to_layout()),
            "problems": _brief_problems(problems[:MAX_PROBLEMS])}
