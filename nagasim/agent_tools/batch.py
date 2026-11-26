"""`apply_edits`: many model edits in one call, one undo entry and one validation (D1451).

Each edit of the batch runs through `core.call` exactly as a single call would, with the
whole-model validation of `_open`/`_edit_result` deferred (`core._DEFERRED`); the batch checks
the model once before and once after. That is what keeps a build of thousands of pieces linear
instead of quadratic.
"""
from __future__ import annotations

import json
from itertools import islice

from . import core
from .core import EDIT, ToolError, _document_args, _edit_result, _open, tool

__all__ = ["BATCHABLE", "MAX_EDITS", "apply_edits"]

# The edits a batch may carry: each changes only the document `model` names, reads and writes no
# file and answers nothing a batch answer would hide. Out on purpose: `set_layout` (its answer's
# dropped_links/dropped_merges), `set_item_code` (trust, D1344), the CSV importers (file reads),
# document and undo tools, `apply_edits` itself.
BATCHABLE = frozenset({
    "add_block", "update_block", "remove_blocks", "connect", "disconnect", "reorder_branches",
    "set_resources", "set_calendar", "set_entity_types", "set_simulation_settings",
    "set_sd_settings", "set_model_info", "auto_layout", "place_piece", "place_feeder",
    "place_transfer", "upsert_record", "remove_record", "upsert_node", "remove_node",
    "upsert_segment", "remove_segment", "upsert_scenario"})

# Bounds one answer and one rollback snapshot; a generator sends larger builds in chunks.
MAX_EDITS = 1000

_SHAPE = 'each edit is {"tool": "<tool name>", "args": {...}}'


class _EditFailed(Exception):
    """An edit answered an error: raised inside the transaction so it rolls the batch back."""

    def __init__(self, index: int, name: str, error: dict):
        super().__init__(name)
        self.index, self.name, self.error = index, name, error


def _refusal(index: int, name: str, error: dict) -> dict:
    hint = error.get("hint") or "fix this edit and send the batch again"
    return {"error": {**error, "message": f"edit {index} ({name}): {error.get('message', '')}",
                      "hint": f"nothing was applied; {hint}"}}


def _checked(model: str, edits: list) -> list[tuple[str, str, dict]]:
    """(name as sent, tool, args) per edit, or the refusal of the first bad one. Everything is
    checked before any edit runs, so a refused batch never half-applies."""
    if not edits:
        raise ToolError("E_BAD_ARGUMENT", "`edits` is empty", _SHAPE)
    if len(edits) > MAX_EDITS:
        raise ToolError("E_BAD_ARGUMENT", f"{len(edits)} edits; at most {MAX_EDITS} per call",
                        f"send the edits in chunks of at most {MAX_EDITS}")
    out = []
    for i, edit in enumerate(edits):
        sent = edit.get("tool")
        args = edit.get("args")
        if args is None:      # null is unset, as the adapter reads it
            args = {}
        if not isinstance(sent, str) or set(edit) - {"tool", "args"}:
            raise ToolError("E_BAD_ARGUMENT", f"edit {i} is not a tool call", _SHAPE)
        if isinstance(args, str):     # some clients send an object argument as JSON text
            try:
                args = json.loads(args)
            except ValueError:
                pass
        if not isinstance(args, dict):
            raise ToolError("E_BAD_ARGUMENT", f"edit {i} ({sent}): `args` must be an object", _SHAPE)
        name, args = core.resolve_alias(sent, dict(args))
        # Published first: a tool the session hides is unknown, never "not batchable".
        if not core.published(name):
            offered = [n for n in core.TOOLS if core.published(n)]
            err = core.unknown_tool_error(sent, offered)
            raise ToolError(err.code, f"edit {i}: {err.message}", err.hint, err.did_you_mean)
        if name not in BATCHABLE:
            raise ToolError("E_NOT_BATCHABLE", f"edit {i} ({sent}): {name} cannot go in a batch",
                            f"call {name} on its own; a batch takes: "
                            f"{', '.join(sorted(n for n in BATCHABLE if core.published(n)))}")
        given = args.pop("model", None)
        if given is not None and given != model:     # null is unset, as the adapter reads it
            raise ToolError("E_BAD_ARGUMENT", f"edit {i} ({sent}) names model '{given}'",
                            f"a batch edits one model, '{model}': leave `model` out of `args`")
        out.append((sent, name, args))
    return out


_document_args("apply_edits",
               model="Workspace name of the model to edit.",
               edits='The edits in order, each {"tool": "<tool name>", "args": {...}} as that tool '
                     f'takes them; at most {MAX_EDITS:,}.')


def _with_limit(fn):
    """Put MAX_EDITS into the docstring, so the published text cannot drift from the check."""
    fn.__doc__ = fn.__doc__.replace("{max}", f"{MAX_EDITS:,}")
    return fn


@tool(returns={**EDIT, "applied": int, "added": list,
               "looks": (dict, "edit index -> look_changed, look_skipped")})
@_with_limit
def apply_edits(model: str, edits: list[dict]) -> dict:
    """Apply a list of model edits in one call, as one undo entry; use it for any large build.

    Each edit is {"tool": name, "args": {...}}: the tool's own arguments, `model` left out.
    Atomic: the first edit that fails stops the batch, nothing is applied, and the error names
    that edit, counted from 0. The answer is the usual edit answer for the whole batch, plus
    "applied" and "added" (the blocks created, in order, named as created). A look write's
    changed and skipped pieces are in "looks", by edit index.

    Batchable: add_block, update_block, remove_blocks, connect, disconnect, reorder_branches,
    set_resources, set_calendar, set_entity_types, set_simulation_settings, set_sd_settings,
    set_model_info, auto_layout, place_piece, place_feeder, place_transfer, upsert_record,
    remove_record. Also the path network and scenarios: upsert_node, remove_node,
    upsert_segment, remove_segment, upsert_scenario. At most {max} edits per call.
    """
    steps = _checked(model, edits)
    doc, before = _open(model)
    added: list[str] = []
    looks: dict[str, dict] = {}
    token = core._DEFERRED.set(True)
    try:
        # A savepoint: inside the assistant's open turn, a failure undoes only this batch (D1453).
        with doc.transaction(f"Apply {len(steps)} edits", savepoint=True):
            for i, (sent, name, args) in enumerate(steps):
                count = len(doc.blocks)
                answer = core.call(name, model=model, **args)
                if "error" in answer:
                    raise _EditFailed(i, sent, answer["error"])
                # The spec's tool answer names the pieces a look write skipped; keep it per edit.
                if answer.get("look_changed") or answer.get("look_skipped"):
                    looks[str(i)] = {k: answer[k] for k in ("look_changed", "look_skipped")}
                # A new block is appended to the dict, so the created names are its last ones;
                # a count, not a name diff, so a rename is not taken for a creation.
                grown = len(doc.blocks) - count
                if grown > 0:
                    added.extend(reversed(list(islice(reversed(doc.blocks), grown))))
    except _EditFailed as failed:
        return _refusal(failed.index, failed.name, failed.error)
    finally:
        core._DEFERRED.reset(token)
    return _edit_result(doc, before, {"applied": len(steps), "added": added,
                                      **({"looks": looks} if looks else {})})
