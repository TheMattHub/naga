"""The assistant's tool-use loop (ROADMAP 3.3, D179/D180): a language model is offered some of
`nagasim.agent_tools`, calls them to edit and run ONE open `ModelDocument` -- an edit never goes
to a copy -- and the whole turn is one undo step. Pure Python, no Qt: an application's dock is a
thin shell around `AssistantSession`, `naga assist` the headless one. The providers (a local OpenAI-
compatible server, Anthropic's API) are `nagasim.llm`; the loop sees only its plain turn record.

`anthropic` is an OPTIONAL extra (`pip install "nagasim[assistant]"`), imported lazily by
`nagasim.llm`, so the rest of the engine works without it.

Design (docs/history/ROADMAP.md 3.3):

* `WorkspaceAdapter` maps one live `ModelDocument` into an `agent_tools.Workspace`
  under a FIXED model id, confined to the model's own folder (or a fresh temp
  dir for an untitled model). The "model" argument every agent_tools mutator
  takes is stripped from what the model sees and filled in here, so the model
  never needs to know the workspace's internal naming; a call that names another
  model is refused, never redirected (#1803). It runs ONLY the tools a
  session offered (`offer`): a name outside the list is an error the model reads,
  not a call.
* `AssistantSession` drives the loop: one provider call per round, tool calls
  executed one at a time through the adapter, results fed back in order, repeat
  until the model stops asking for tools or `max_tool_rounds` is hit. The WHOLE
  turn -- every edit any tool made -- is wrapped by `adapter.begin_turn()`/
  `commit_turn()`, so it lands as ONE undo step; any failure mid-turn (an
  exception, a reply cut at its length limit, a refusal, an empty reply) calls
  `rollback_turn()` instead, undoing every edit the turn made so far
  (`ModelDocument.transaction` already gives us this; the adapter just holds it
  open across several tool calls instead of one).
* An agent surface never runs a model's Python (D186): `naga assist` drops the
  caller's consent for its whole process, and every tool call of the adapter runs
  with it dropped, so an application that shares its process with a person gets it
  back as soon as the call returns.
* An application marshals `WorkspaceAdapter`'s calls onto the thread that owns its
  document if the loop runs elsewhere; the headless command calls it in-place on
  the same thread that runs the loop. A tool that does not change the document
  (`OFF_GUI`, D1261) may instead run on the loop's thread against a `snapshot`,
  so a long run leaves the application's window free.
"""
from __future__ import annotations

from nagasim._cli import add_model_argument

import argparse
import contextlib
import copy
import dataclasses
import json
import re
import shutil
import sys
import tempfile
import weakref
from pathlib import Path

from nagasim import agent_tools, llm, modeldiff, nextoptions
from nagasim.core.document import ModelDocument

__all__ = [
    "APP_ONLY_TOOLS", "NOT_IN_DOCK", "ON_GUI", "OFF_GUI", "offered_tools", "system_prompt", "tool_defs",
    "ToolCallLog", "TurnResult", "WorkspaceAdapter", "AssistantSession", "redact", "main",
    "REVIEW_NOTES", "turn_label", "review_lines", "guard_lines",
]

# ---------------------------------------------------------------- tool lists
# S2-15, D1245: a session offers every engine tool, to a hosted and a local model alike, after the
# overlap audit (docs/history/tool-overlap-2026-11-12.md) merged the near-duplicates. Left out:
# the page's own call (`study_page_html`, which an MCP host shows to the app only, never to the
# model), and (D1250) the tools that cannot do their job in a turn, `NOT_IN_DOCK`. A product adds
# its own tools through `extra_tools` (the engine cannot know which of
# them can run inside the editor's process). The adapter keeps the one open model: a call with no
# "model" gets it, one naming another model is refused (#1803), it refuses to save that model
# behind the editor (#1794) or to write over an existing file (#1801), and a picture reaches a
# text-only provider as a note, not as base64 text (#1796).
APP_ONLY_TOOLS = frozenset({"study_page_html"})
# D1250 (narrows D1245): offered, they only cost the model's context (about 6 KB a turn). Four make
# a new model under a new name, which the turn may not name afterwards (#1803); save_model is
# refused (#1794), undo and redo wait for the turn to end (#1795). MCP keeps all seven;
# tests/test_assistant.py holds the two classes, so an eighth such tool fails there.
NOT_IN_DOCK = frozenset({"new_model", "open_model", "import_event_log", "import_sd_model",
                         "save_model", "undo", "redo"})
# #1801: the tools that write a path the call names, by that argument. In the editor they write
# new files only; every other path argument of an offered tool is read (tests/test_assistant.py
# keeps the two lists whole). A product adds the entries of its own tools that write.
WRITES = {"export": "path", "study_start": "study"}
# #1813: a tool whose optional "model" is one of several ways to name its input; the open model is
# filled in only when the call named none of them.
_MODEL_ALTERNATIVES = {"study_step": ("layout", "patch")}

# D1261: where an editor's assistant runs each engine tool. ON_GUI: it changes the open model, or
# a process-wide store the window also reads (the plugin trust list, the loaded plugin registries),
# so it runs on the GUI thread as before. OFF_GUI: it only reads the model, or not at all, so it
# runs on the turn's own thread against a copy taken on the GUI thread (`snapshot`); a tool that
# never reads the model pays only that copy (milliseconds), and a tool put here by mistake changes
# the copy, which `run_tool` refuses (E_NOT_APPLIED). A product classes its own extra tools.
# tests/test_ui_assistant_thread.py classes every offered tool, so a new tool without a class fails.
ON_GUI = frozenset({
    "add_block", "apply_edits", "auto_layout", "calibrate", "check_model_opens", "connect", "disconnect",
    "place_feeder", "place_piece", "place_transfer", "propose_plugin_folder", "remove_blocks", "remove_node", "remove_record", "remove_segment",
    "reorder_branches", "resolve_data_bindings", "set_calendar", "set_entity_types",
    "set_initial_state", "set_item_code", "set_layout", "set_model_info", "set_resources", "set_view", "set_sd_settings", "set_simulation_settings",
    "update_block", "upsert_node", "upsert_orders_from_csv", "upsert_record", "upsert_scenario",
    "upsert_segment", "upsert_table_from_csv", "upsert_timeseries_input_from_csv", "write_model"})
OFF_GUI = frozenset({
    "check_draft", "check_layout", "debug_inspect", "debug_run", "describe_block_type", "describe_distribution",
    "evaluate_policy", "explain_kpi", "export", "find_tool", "fit_distribution", "fit_surrogate",
    "forecast", "generate_pallet_layer", "get_distance", "get_expression_help", "get_kpi_table", "get_model_format",
    "get_model_json", "get_model_summary", "get_network", "get_ports", "get_plant_timeline", "get_record_schema",
    "get_twin_forecast", "get_twin_monitor", "get_twin_recommendations", "input_uncertainty",
    "investment_case", "list_block_types", "list_continuous", "list_distributions", "list_models",
    "list_records", "optimize", "run_facts", "run_loop_dominance", "run_model", "run_reality_checks",
    "run_scenarios", "run_to_precision", "study_accept", "study_add_input", "study_animation",
    "study_extensions", "study_package", "study_report", "study_revert", "study_sensitivity",
    "study_show", "study_slides", "study_start", "study_status", "study_step", "study_update",
    "study_validate", "study_view", "study_writeup", "suggest_warmup", "validate_model",
    "view_model", "what_if"})


class _Stopped(BaseException):
    """The person stopped the turn: raised from the progress sink at a long tool's next progress
    point. A BaseException, so neither `report` (which logs a failing sink's Exception) nor a
    tool's own `except Exception` (a scenario run's) turns it into an answer."""


def _stop_point(should_cancel):
    def sink(*_progress):
        if should_cancel():
            raise _Stopped
    return sink


class _ToolFailed(Exception):
    """Carries a tool's error answer out of its savepoint scope (see `WorkspaceAdapter._call`)."""

    def __init__(self, answer: dict):
        super().__init__("tool answered an error")
        self.answer = answer


_CANCELLED = agent_tools.ToolError(
    "E_CANCELLED", "the person stopped the turn: this call did not finish and changed nothing",
    "stop here and say what was done before the stop")


def offered_tools(kind: str, extra: tuple = ()) -> list[str]:
    """The tool names a session offers: every engine tool but the app-only ones and those that
    cannot work in a turn (`NOT_IN_DOCK`), plus a product's *extra*, limited to what is
    registered, whatever the provider *kind*. A selector with the same signature can replace it
    (`AssistantSession(select_tools=...)`); the adapter still refuses what it offers wrongly."""
    names = {n for n, s in agent_tools.TOOLS.items() if s.origin.startswith("nagasim.")} | set(extra)
    return sorted(n for n in names - APP_ONLY_TOOLS - NOT_IN_DOCK if n in agent_tools.TOOLS)


def _mentions(text: str, tool: str) -> bool:
    # "one undo entry" is plain English, not a call to the `undo` tool.
    return re.search(r"\b%s\b(?!\s+(?:entry|step))" % re.escape(tool), text) is not None


def _only_offered(description: str, offered: set) -> str:
    """#1830: MCP's text may send the model to a tool the dock does not offer (`save_model`,
    `open_model`). Cut that mention at tool_defs time, so MCP keeps the whole text: a list item
    (", x" / " and x") goes alone, otherwise the sentence goes."""
    for tool in sorted(set(agent_tools.TOOLS) - offered):
        if not _mentions(description, tool):
            continue
        item = r"(?:,| and)\s+`?%s`?" % re.escape(tool)
        trimmed = re.sub(item, "", description)
        if not _mentions(trimmed, tool):
            description = trimmed
            continue
        sentences = re.split(r"(?<=[.!?])\s+", description)
        description = " ".join(s for s in sentences if not _mentions(s, tool))
    return description


def tool_defs(names) -> list[dict]:
    """{name, description, input_schema} per tool, with the internal "model" argument
    stripped (the adapter always injects it) and no mention of a tool the dock does not offer."""
    defs = []
    offered = set(names)
    for name in names:
        spec = agent_tools.TOOLS[name]
        schema = copy.deepcopy(spec.input_schema)
        if "model" in schema.get("properties", {}):
            schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "model"}
            schema["required"] = [r for r in schema.get("required", []) if r != "model"]
        defs.append({"name": name, "description": _only_offered(spec.description, offered),
                     "input_schema": schema})
    return defs


# Guidance condensed from nagasim/mcp_server.py's build_model_from_description and
# analyse_results prompts (ROADMAP 3.2), so the two agent surfaces teach the
# same workflow.
_PROMPT_BODY = """\
You build, edit, run and explain models for the ONE model currently open in the \
editor, through the tools provided: every "model" argument is that model, files you \
write (exports, studies) stay in its folder, and the person saves it in the editor.

Building a model from a description:
1. Read what ARRIVES (and how often), what HAPPENS to it, what it COMPETES for \
(staff, machines, space), and what LEAVES. Ask ONE clarifying question if this \
is ambiguous, rather than guessing.
2. Call get_model_format once: the model file's shape, every block type with its \
parameters, the distributions' parameter names and the expression functions.
3. write_model with the WHOLE model in one call: one CREATE, the processing blocks \
(PROCESS is seize-delay-release in one block -- prefer it), DISPOSE at the end, the \
links in flow order (a DECIDE's links are its branches in order), a resource for \
anything competed for, and simulation.time_limit unless the model drains on its own.
4. The answer lists every problem with its path and a hint: fix them all in ONE more \
write_model. Interarrival times are usually Exponential (a rate, 1 / the mean); \
service times are rarely Normal (it can go negative) -- prefer Lognormal, Gamma or \
BoundedNormal.
5. run_model; its digest carries the KPIs. For a small change to an open model, \
update_block is enough.
6. Before you say a model you built or edited is done, call check_model_opens: it must \
pass. A type that comes from a plugin that is not loaded needs the person to trust its \
folder in Settings > Plugins; propose_plugin_folder can only suggest one, switched off.

Reading results honestly:
- Every KPI comes back with `ci_half_width`, the half-width of its 95% CI over \
replications. ALWAYS report both. A half-width that is a large fraction of the \
mean means the answer is not yet precise -- say so, and suggest run_to_precision.
- One replication gives no interval (null); never present it as a settled answer.
- Never invent a KPI the tools did not return.
- To compare options use upsert_scenario + run_scenarios, which reports the \
DIFFERENCE with its own interval and a significance verdict -- "not significant" \
means "not distinguishable at this replication count", not "no difference".
- If no warm-up was set and the question is about steady state, call \
suggest_warmup.

Keep answers short. A compact JSON summary of the current model is included at \
the start of each of your turns -- use it instead of asking the user to repeat \
the model's state.
"""

# D727: the options are computed by rules over recorded numbers (`nagasim.nextoptions`); the
# assistant only says them. Both prompts carry this routine (the editor's and the study room's).
WHAT_NEXT = """\
What next: the {where} carries "{field}", ranked options computed from the recorded numbers \
(kind, title, why, tool, arguments, cost; "needs" lists arguments you still fill in). When the \
person asks "what next?" (or anything like it), or at the end of a turn when nothing is pending, \
offer the top option as ONE question with its reason and its cost, then name up to two others by \
their title. Never run an option before the person picks it. "yes", "do it", "the first" or "go \
ahead" carry on with the option you offered first; "the second" or "the third" with that one: \
call its tool with its arguments, filling in what it needs. Never invent a number for a reason; \
an idea of your own is said as yours, not as a measured fact.
"""
VOICE_NOTE = ("[Voice mode: the answer is read aloud. At most three short sentences; no tables, "
              "lists or Markdown.]")


def system_prompt(app: str = "Naga") -> str:
    return (f"You are the in-app assistant for {app}, a discrete-event simulation editor. "
            + _PROMPT_BODY + "\n" + WHAT_NEXT.format(where="model summary", field="next_options"))


# ---------------------------------------------------------------- results
@dataclasses.dataclass
class ToolCallLog:
    """One executed tool call, for the UI's tool-call cards: name, the
    arguments the model gave (model id already stripped by the adapter), and the
    full agent_tools result (its "introduced"/"fixed" problems included)."""

    name: str
    input: dict
    result: dict


@dataclasses.dataclass
class TurnResult:
    text: str
    tool_calls: list[ToolCallLog]
    cancelled: bool = False
    hit_round_cap: bool = False
    rounds: int = 0
    error: str | None = None
    notices: list[str] = dataclasses.field(default_factory=list)   # e.g. a cut prompt (#1804)


# ---------------------------------------------------------------- workspace adapter
class WorkspaceAdapter:
    """Maps one live `ModelDocument` into an `agent_tools.Workspace` under a
    fixed handle, and holds one `document.transaction()` open across an entire
    assistant turn (begin_turn/commit_turn/rollback_turn) rather than the one
    tool call at a time `agent_tools` normally groups.

    File tools (fit_distribution) are confined to the model's own folder, or a
    fresh temp directory for an untitled model -- never the whole filesystem.

    The workspace's network policy is left at its default, which allows
    nothing, and no offered tool reaches the network: the assistant cannot
    fetch a URL, and nothing it says can change that. Granting network access
    stays with the human, through the application's own consent dialog.

    Nothing runs until `offer` names the tools of the session (a model that
    calls any other name gets an error it can read; a tool that was not
    offered never runs).
    """

    MODEL_ID = "current"

    def __init__(self, document: ModelDocument, root: Path | None = None):
        self.document = document
        if root is None and document.path is not None:
            root = document.path.parent
        if root is None:
            # #1808: a folder of its own goes with the adapter. A caller whose files must outlive
            # one adapter (the dock, across turns) passes its own *root*.
            root = Path(tempfile.mkdtemp(prefix="naga_assistant_"))
            weakref.finalize(self, shutil.rmtree, root, ignore_errors=True)
        self.workspace = agent_tools.Workspace(root)
        self.workspace.models[self.MODEL_ID] = document
        self.offered = frozenset()
        self._cm = None
        self._written: set[Path] = set()    # files this adapter's tools wrote (#1801)

    def offer(self, names) -> None:
        self.offered = frozenset(names)

    def begin_turn(self, label: str | None = None) -> None:
        self._cm = self.document.transaction(label)
        self._cm.__enter__()

    def commit_turn(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = None

    def rollback_turn(self, exc: BaseException | None = None) -> None:
        if self._cm is None:
            return
        exc = exc or RuntimeError("assistant turn failed")
        self._cm.__exit__(type(exc), exc, exc.__traceback__)
        self._cm = None

    def snapshot(self) -> ModelDocument:
        """A copy of the open model, for a tool that runs off the editor's GUI thread (D1261):
        taken on the thread that owns the document, then read only by the tool's thread."""
        live = self.document
        out = ModelDocument(live.to_layout())
        out.path, out.kept, out.written_by = live.path, dict(live.kept), live.written_by
        return out

    def run_tool(self, name: str, arguments: dict | None, *, document: ModelDocument | None = None,
                 should_cancel=None) -> dict:
        """Run one offered tool on the open model. With *document* (a `snapshot`) the call sees
        that copy under the open model's handle instead, and a change to it is refused, not kept
        (D1261). With *should_cancel*, a call starts only while it answers False, and a long tool
        stops at its next progress point once it answers True: E_CANCELLED, and the workspace's
        models and results are as they were before the call (D1262)."""
        if arguments is not None:
            name, arguments = agent_tools.core.resolve_alias(name, arguments)
        if name not in self.offered:
            return agent_tools.ToolError(
                "E_TOOL_NOT_OFFERED", f"'{name}' is not a tool of this session",
                f"use one of: {', '.join(sorted(self.offered))}").as_dict()
        if arguments is None:
            return agent_tools.ToolError(
                "E_BAD_ARGUMENTS", f"the arguments of '{name}' were not a JSON object",
                "send the arguments as one JSON object matching the tool's input schema").as_dict()
        if name == "save_model":
            # #1794: the open model is the person's; a save in a turn would write it before they
            # keep or revert the turn, and a path would re-point the editor at another file.
            return agent_tools.ToolError(
                "E_SAVE_IN_EDITOR", "the assistant does not save the model open in the editor",
                "say the model is ready; the person saves it in the editor (File > Save), after "
                "keeping or reverting this turn").as_dict()
        if should_cancel is not None and should_cancel():
            return _CANCELLED.as_dict()     # a call later in the round the person stopped
        # The editor's process installs no other workspace (the room and `naga mcp` are their own
        # processes), and a turn's calls run one at a time, so the global holds for the call.
        agent_tools.use_workspace(self.workspace)
        refused = self._other_model(name, arguments) or self._over_a_file(name, arguments)
        if refused is not None:
            return refused.as_dict()
        if ("model" in agent_tools.TOOLS[name].input_schema.get("properties", {})
                and arguments.get("model") is None
                and all(arguments.get(k) is None for k in _MODEL_ALTERNATIVES.get(name, ()))):
            arguments = {**arguments, "model": self.MODEL_ID}
        ws, shown = self.workspace, document if document is not None else self.document
        copied = document.to_layout() if document is not None else None
        before = dict(ws.models), dict(ws.results)
        ws.models[self.MODEL_ID] = shown
        stops = (agent_tools.core.reporting(_stop_point(should_cancel)) if should_cancel is not None
                 else contextlib.nullcontext())
        try:
            # D186; a hint names only the tools offered here (#1773).
            with agent_tools.python_consent_dropped(), agent_tools.core.offering(self.offered), stops:
                answer = self._call(name, arguments, document)
        except _Stopped:
            # A stopped study step has written no version; what it put in the workspace (its
            # slot's model, a partial run's results) goes too, so a later call sees the earlier state.
            for kept, now in zip(before, (ws.models, ws.results)):
                now.clear()
                now.update(kept)
            return _CANCELLED.as_dict()
        finally:
            replaced = ws.models.get(self.MODEL_ID) is not shown
            ws.models[self.MODEL_ID] = self.document
        if copied is not None and document.to_layout() != copied:
            return agent_tools.ToolError(
                "E_NOT_APPLIED", f"'{name}' changed a copy of the model, so nothing changed in the editor",
                "this tool's thread class is wrong (nagasim.assistant.ON_GUI); say the change was not "
                "made").as_dict()
        if (arguments.get(WRITES.get(name, "")) is not None and isinstance(answer, dict)
                and "error" not in answer):
            self._written.add(self.workspace.resolve(arguments[WRITES[name]]))
        if replaced:
            # #1797: a tool that opens or creates a model under this handle (open_model with
            # name="current") would send the rest of the turn to a model the editor does not show.
            return agent_tools.ToolError(
                "E_LIVE_MODEL", f"'{self.MODEL_ID}' is the model open in the editor; nothing replaced it",
                "edit the open model itself, or open the file under another name").as_dict()
        return answer

    def _call(self, name: str, arguments: dict, document: ModelDocument | None):
        """`agent_tools.call`, and on the live model inside a turn a failed edit undoes itself.
        #2065: a tool's own transaction only nests in the turn's, so one that edited and then
        failed (or answered an error) left its half-edit in the turn. The savepoint is here, once
        per call, and never in `core.call` or the tools, so a batch pays this snapshot and its own, not one per edit."""
        if (document is not None or self._cm is None
                or agent_tools.TOOLS[name].read_only):
            return agent_tools.call(name, **arguments)
        try:
            with self.document.transaction(savepoint=True):
                answer = agent_tools.call(name, **arguments)
                if isinstance(answer, dict) and "error" in answer:
                    # An error answer raises nothing, so raise to roll back; caught just outside.
                    raise _ToolFailed(answer)
        except _ToolFailed as failed:
            return failed.answer
        return answer

    def _other_model(self, name: str, arguments: dict):
        """#1803: a model other than the open one is refused, never redirected to it (the person
        sees only the open model, and a workspace model of the dock lasts one turn). A study's
        slot ("study:<id>", its last version on disk) is a named target of its own and passes."""
        if "model" not in agent_tools.TOOLS[name].input_schema.get("properties", {}):
            return None
        for key in ("model", "name") if name == "write_model" else ("model",):
            value = arguments.get(key)
            if value is None or value == self.MODEL_ID or (isinstance(value, str)
                                                           and value.startswith(nextoptions.SLOT)):
                continue
            return agent_tools.ToolError(
                "E_OTHER_MODEL", f"'{value}' is not the model open in the editor: in the editor the "
                "assistant works on the open model only",
                "leave the model argument out to work on the open model (write_model puts a whole "
                "model into it); to work on another model the person opens it in the editor")
        return None

    def _over_a_file(self, name: str, arguments: dict):
        """#1801: in the editor a tool that writes a path the call names writes a new file only
        (or one it wrote itself in this adapter): never the open model's file nor another file
        of its folder, which the turn's undo would not bring back."""
        value = arguments.get(WRITES.get(name, ""))
        if value is None:
            return None
        try:
            target = self.workspace.resolve(value)
        except Exception:       # a path the tool refuses itself: it says why
            return None
        own = self.document.path is not None and target == Path(self.document.path).resolve()
        if not own and (target in self._written or not target.exists()):
            return None
        return agent_tools.ToolError(
            "E_FILE_EXISTS", f"'{value}' already exists: in the editor the assistant writes new "
            "files only", "pick a name no file or folder has yet")

    def model_summary(self) -> dict:
        agent_tools.use_workspace(self.workspace)
        return agent_tools.TOOLS["get_model_summary"].fn(model=self.MODEL_ID)


# ---------------------------------------------------------------- review of a turn (D680)
# The line a turn's user text starts with after the person answered the review of the turn
# before: the agent learns Keep or Revert and does not build on reverted work.
REVIEW_NOTES = {"kept": "[The person kept your last changes]",
                "reverted": "[The person reverted your last changes]"}


def turn_label(prompt: str) -> str:
    """The undo step's name for a turn, as the History menu shows it: the prompt's first 40
    characters, its line breaks folded to spaces."""
    return "Assistant: " + " ".join(prompt.split())[:40]


def review_lines(before: dict, after: dict, *, limit: int = 100, width: int = 200) -> list[str]:
    """What a turn changed, one line per change as `naga diff` prints it (added, removed,
    changed with old and new value), from the layout before the turn to the layout after it.
    Canvas moves come last, as ONE line with their count. A line is cut at *width* characters
    and the list at *limit* lines: a turn that rewrites the model must not flood the panel."""
    changes = modeldiff.diff(before, after)
    moved = {c.path.rpartition(".")[0] for c in changes if c.kind == "moved"}
    lines = [modeldiff.change_line(c) for c in changes if c.kind != "moved"]
    lines = [line if len(line) <= width else line[:width - 3] + "..." for line in lines]
    if len(lines) > limit:
        lines[limit:] = [f"... and {len(lines) - limit} more changes"]
    if moved:
        lines.append(f"{len(moved)} item{'s' if len(moved) > 1 else ''} moved on the canvas")
    return lines


def _short_run(layout: dict, deadline: float):
    """(facts, law checks) of one fixed-seed replication; None when the deadline cut it, () when
    the model cannot run (validation already says why). Two runs (trace for the facts, frames for the laws): ponytail, one
    shared run when the guard is too slow."""
    import time
    from nagasim import api, factscli, laws
    stop = lambda: time.monotonic() > deadline      # noqa: E731
    try:
        # The agent wrote this layout: its runs never use the person's Python consent (D186).
        with agent_tools.python_consent_dropped():
            found = factscli.run(layout, seed=0, should_stop=stop)
            results = None if found is None else api.run_model(layout, replications=1, seed=0, should_stop=stop)
    except Exception:                                # a model that does not run has no guard to give
        return ()
    if results is None:
        return None
    return ({(f["kind"], f["block"], f["resource"]): f["text"] for f in found["facts"]},
            {c["name"]: c["why"] for c in laws.checks(layout, results) if c["verdict"] != "passed"})


def guard_lines(before: dict, after: dict, budget_s: float = 3.0) -> list[str]:
    """What a turn's edit changed in how the model RUNS (D688): facts and law warnings that are
    new or gone between a short fixed-seed run of *before* and of *after*, one line each. Both
    runs share *budget_s*; over it, one line says the guard was skipped. [] when nothing
    differs. Facts, not a verdict: the person still chooses Keep or Revert."""
    import time
    deadline = time.monotonic() + budget_s
    old = _short_run(before, deadline)
    new = _short_run(after, deadline) if old != () else ()
    if old == () or new == ():
        return []
    if old is None or new is None:
        return ["Run check skipped: the model did not run within "
                f"{budget_s:g} s so no before/after comparison."]
    lines = []
    for label, a, b in (("fact", old[0], new[0]), ("law warning", old[1], new[1])):
        lines += [f"New {label}: {b[k]}" for k in b if k not in a]
        lines += [f"Gone {label}: {a[k]}" for k in a if k not in b]
    return lines


# ---------------------------------------------------------------- session
def _for_provider(result):
    """A tool answer as the provider reads it: a picture's base64 data becomes a note (#1796; the
    providers' tool results carry text only here), the rest unchanged. The call's log keeps it."""
    if isinstance(result, dict) and isinstance(result.get("image"), dict):
        return {**result, "image": "(a picture this conversation cannot carry; format=\"text\" "
                                   "lists what it shows)"}
    return result


_STOPPED = {"length": "the model's reply was cut at its length limit",
            "refusal": "the model refused to answer"}


class AssistantSession:
    """One conversation against one `WorkspaceAdapter`. State lives in `history` (the plain
    turn record of `nagasim.llm`) and `usage`; the provider is stateless."""

    def __init__(self, workspace_adapter, provider, *, app: str = "Naga", extra_tools: tuple = (),
                 select_tools=offered_tools, max_tool_rounds: int = 12, system: str | None = None):
        self.adapter = workspace_adapter
        self.provider = provider
        # *system* replaces the model-editing prompt for an adapter that is not about one model
        # (the study room's assistant runs the study tools, with the study guide as its prompt).
        self.system = system if system is not None else system_prompt(app)
        self.max_tool_rounds = max_tool_rounds
        self.history: list[dict] = []
        self.usage = llm.Usage()
        names = select_tools(provider.kind, extra_tools)
        self.adapter.offer(names)
        self._tool_defs = tool_defs(names)

    def run_turn(self, user_text: str, *, label: str | None = None, on_text_delta=None,
                 should_cancel=None, attachment: dict | None = None) -> TurnResult:
        """Run one user turn to completion (or cancellation, or the round cap): ask, execute
        any requested tools, feed results back, repeat. The whole turn is one undo step, named
        *label* when given (`turn_label`); an exception or an unusable reply anywhere in the
        loop rolls every edit the turn made back out. *attachment* ({"pdf": base64 text}) goes
        with the user text to a provider that reads documents (`nagasim.llm`)."""
        summary = self.adapter.model_summary()
        start = len(self.history)       # where this turn's entries begin
        self.history.append({"role": "user", "text": f"[Current model]\n"
                             f"{json.dumps(summary, separators=(',', ':'))}\n\n{user_text}",
                             **({"attachment": attachment} if attachment else {})})
        tool_calls, text_out, notices = [], [], []
        rounds, hit_cap, cancelled, error = 0, False, False, None
        # Only a caller that names the turn passes a label: another adapter (the study room's)
        # has a begin_turn() with no parameter.
        if label:
            self.adapter.begin_turn(label)
        else:
            self.adapter.begin_turn()
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                reply = self.provider.complete(self.system, self._tool_defs, self.history,
                                               on_text_delta)
                self.usage.merge(reply.usage)
                notice = getattr(reply, "notice", "")
                if notice and notice not in notices:
                    notices.append(notice)
                if reply.stop in _STOPPED or not (reply.text or reply.tool_calls):
                    raise llm.LLMError(_STOPPED.get(reply.stop, "the model returned an empty reply"))
                entry = {"role": "assistant", "text": reply.text, "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments} for c in reply.tool_calls]}
                if reply.blocks:
                    entry["blocks"] = reply.blocks
                self.history.append(entry)
                text_out.append(reply.text)
                if not reply.tool_calls:
                    break
                rounds += 1
                if rounds > self.max_tool_rounds:
                    hit_cap = True
                    break
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                for call in reply.tool_calls:
                    result = self.adapter.run_tool(call.name, call.arguments)
                    tool_calls.append(ToolCallLog(name=call.name, input=dict(call.arguments or {}),
                                                  result=result))
                    self.history.append({"role": "tool", "id": call.id, "name": call.name,
                                         "content": json.dumps(_for_provider(result)),
                                         "is_error": isinstance(result, dict) and "error" in result})
        except Exception as exc:
            self.adapter.rollback_turn(exc)
            del self.history[start:]    # the edits are undone, so the record of them goes too
            error = f"{type(exc).__name__}: {exc}"
        else:
            self.adapter.commit_turn()
            last = self.history[-1]
            if last["role"] == "assistant" and last["tool_calls"]:
                # A round cap or a cancel left calls that never ran; both wire formats reject a
                # history with an unanswered tool call, so the next turn would fail on it.
                self.history.pop()
        return TurnResult(text="".join(text_out), tool_calls=tool_calls, cancelled=cancelled,
                          hit_round_cap=hit_cap, rounds=rounds, error=error, notices=notices)


# ---------------------------------------------------------------- redaction
def redact(text: str, key: str | None) -> str:
    """*text* with every occurrence of *key* replaced -- apply this to any
    exception message, log line or transcript export that might echo the raw
    API key back."""
    if not text or not key:
        return text
    return text.replace(key, "[REDACTED]")


# ---------------------------------------------------------------- headless CLI
def main(argv=None, *, provider=None, app: str = "Naga", extra_tools: tuple = (),
         anthropic_key: str | None = None, prog: str = "naga assist") -> int:
    """`naga assist model.json "prompt"`: one turn, no Qt. *provider* is a testing seam; a
    product passes its *app* name, its *extra_tools* and the Anthropic key it stores
    (*anthropic_key*: it goes to the Anthropic provider only, never to another endpoint)."""
    agent_tools.drop_python_consent()       # D186
    parser = argparse.ArgumentParser(
        prog=prog, description="Run one assistant turn against a model file, with a local "
                               "model or your own API key.")
    add_model_argument(parser, help="path to the model .json file, or a bundled model's name (naga examples)")
    parser.add_argument("prompt", help="what to ask the assistant to do")
    parser.add_argument("--provider", choices=("anthropic", "openai"), default=None,
                        help="anthropic, or openai for any OpenAI-compatible server (Ollama, "
                             "LM Studio); default: anthropic when a key is set (ANTHROPIC_API_KEY, "
                             "or stored for your Windows user) and no --base-url is given, "
                             "else a local Ollama")
    parser.add_argument("--base-url", default=None, help=f"default {llm.LOCAL_DEFAULT}")
    parser.add_argument("--model-id", default=None, dest="model_id",
                        help="the provider's model name (for Ollama, one from `ollama list`)")
    parser.add_argument("--reasoning", default=None,
                        choices=("none", "low", "medium", "high", "omit"))
    parser.add_argument("--max-tool-rounds", type=int, default=12)
    args = parser.parse_args(argv)

    path = Path(args.model)
    if not path.is_file():
        print(f"no such model file: {path}", file=sys.stderr)
        return 1
    doc = ModelDocument.load(path)
    key = anthropic_key     # what an error line is redacted with; the resolved key replaces it
    try:
        if provider is None:
            cfg = llm.resolve_config(provider=args.provider, base_url=args.base_url,
                                     model=args.model_id, reasoning=args.reasoning,
                                     anthropic_key=anthropic_key)
            key = cfg.api_key
            provider = llm.build_provider(cfg)
    except llm.LLMError as exc:
        print(redact(str(exc), key), file=sys.stderr)
        return 1

    session = AssistantSession(WorkspaceAdapter(doc), provider, app=app, extra_tools=extra_tools,
                               max_tool_rounds=args.max_tool_rounds)
    # Everything printed from a provider's answer goes through redact with the key that was
    # actually resolved: a transcript must never carry it.
    result = session.run_turn(args.prompt,
                              on_text_delta=lambda t: print(redact(t, key), end="", flush=True))
    print()
    for notice in result.notices:
        print(redact(notice, key), file=sys.stderr)
    if result.error:
        print(redact(f"error: {result.error}", key), file=sys.stderr)
        return 1
    for call in result.tool_calls:
        status = "error" if isinstance(call.result, dict) and "error" in call.result else "ok"
        print(redact(f"  [tool] {call.name} -> {status}", key))
    print(f"(tokens: {session.usage.input_tokens} in / {session.usage.output_tokens} out)")
    if result.hit_round_cap:
        print("(stopped: tool round cap reached)")
    if doc.dirty:
        doc.save(path)
        print(f"Saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
