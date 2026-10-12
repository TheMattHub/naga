"""The assistant's tool-use loop (ROADMAP 3.3, D179/D180): a language model is offered some of
`nagasim.agent_tools`, calls them to edit and run ONE open `ModelDocument` -- never a copy --
and the whole turn is one undo step. Pure Python, no Qt: an application's dock is a thin shell
around `AssistantSession`, `naga assist` the headless one. The providers (a local OpenAI-
compatible server, Anthropic's API) are `nagasim.llm`; the loop sees only its plain turn record.

`anthropic` is an OPTIONAL extra (`pip install "nagasim[assistant]"`), imported lazily by
`nagasim.llm`, so the rest of the engine works without it.

Design (docs/ROADMAP.md 3.3):

* `WorkspaceAdapter` maps one live `ModelDocument` into an `agent_tools.Workspace`
  under a FIXED model id, confined to the model's own folder (or a fresh temp
  dir for an untitled model). The "model" argument every agent_tools mutator
  takes is stripped from what the model sees and re-injected here, so the model
  never needs to know the workspace's internal naming. It runs ONLY the tools a
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
  the same thread that runs the loop.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import argparse
import copy
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

from nagasim import agent_tools, llm
from nagasim.core.document import ModelDocument

__all__ = [
    "CURATED_TOOL_NAMES", "COMPACT_TOOL_NAMES", "offered_tools", "system_prompt", "tool_defs",
    "ToolCallLog", "TurnResult", "WorkspaceAdapter", "AssistantSession", "redact", "main",
]

# ---------------------------------------------------------------- tool lists
# ROADMAP 3.3 item 2: reference, editing, validate, run_model, get_kpi_table,
# scenarios, run_to_precision, suggest_warmup, fit_distribution, auto_layout.
# Explicitly excluded: undo/redo (the UI/CLI owns undo) and workspace
# open/save/list/export (file access stays with the application, confined to one
# already-open model). get_model_summary is not exposed as a callable tool
# either -- the session injects it into every user turn itself (item 3).
# A product adds its own through `extra_tools`.
CURATED_TOOL_NAMES = frozenset({
    "list_block_types", "describe_block_type", "list_distributions",
    "describe_distribution", "get_expression_help",
    "add_block", "update_block", "remove_blocks", "connect", "disconnect",
    "reorder_branches", "set_resources", "set_entity_types",
    # D147: one generic record surface (resources, schedules, tables, orders, ...).
    "get_record_schema", "list_records", "upsert_record", "remove_record",
    "list_sequences", "list_orders",
    "set_simulation_settings", "set_calendar", "auto_layout",
    "validate_model",
    "run_model", "get_kpi_table",
    "upsert_scenario", "run_scenarios", "investment_case",
    "run_to_precision", "suggest_warmup",
    "fit_distribution",
})

# What a small local model is offered (D179; tuned by the PHASE7 7.4c pilot).
COMPACT_TOOL_NAMES = frozenset({
    "list_block_types", "describe_block_type", "list_distributions", "add_block", "update_block",
    "connect", "upsert_record", "set_simulation_settings", "validate_model", "run_model",
    "get_kpi_table", "auto_layout"})


def offered_tools(kind: str, extra: tuple = ()) -> list[str]:
    """The tool names a session offers: the compact list for a local model, the curated one
    plus a product's *extra* otherwise, limited to what is registered. A selector with the
    same signature can replace it (`AssistantSession(select_tools=...)`)."""
    names = COMPACT_TOOL_NAMES if kind == "local" else CURATED_TOOL_NAMES | frozenset(extra)
    return sorted(name for name in names if name in agent_tools.TOOLS)


def tool_defs(names) -> list[dict]:
    """{name, description, input_schema} per tool, with the internal "model" argument
    stripped: the adapter always injects it."""
    defs = []
    for name in names:
        spec = agent_tools.TOOLS[name]
        schema = copy.deepcopy(spec.input_schema)
        if "model" in schema.get("properties", {}):
            schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "model"}
            schema["required"] = [r for r in schema.get("required", []) if r != "model"]
        defs.append({"name": name, "description": spec.description, "input_schema": schema})
    return defs


# Guidance condensed from nagasim/mcp_server.py's build_model_from_description and
# analyse_results prompts (ROADMAP 3.2), so the two agent surfaces teach the
# same workflow.
_PROMPT_BODY = """\
You build, edit, run and explain models for the ONE model currently open in the \
editor, through the tools provided -- you never see or touch any other file.

Building a model from a description:
1. Read what ARRIVES (and how often), what HAPPENS to it, what it COMPETES for \
(staff, machines, space), and what LEAVES. Ask ONE clarifying question if this \
is ambiguous, rather than guessing.
2. Call list_block_types and list_distributions once so you use real names.
3. Add blocks: one CREATE, the processing blocks (PROCESS is seize-delay-release \
in one block -- prefer it), DISPOSE at the end. upsert_record into "resources" for anything \
competed for. connect them in flow order -- a DECIDE's outgoing links are its \
branches in the order you add them.
4. set_simulation_settings with a run length UNLESS the model drains on its own \
(validate_model's requires_run_length tells you). Interarrival times are usually \
Exponential; service times are rarely Normal (it can go negative) -- prefer \
Lognormal, Gamma or BoundedNormal.
5. validate_model after each batch of edits; fix errors using each problem's hint.
6. auto_layout once you are done.

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


def system_prompt(app: str = "Naga") -> str:
    return (f"You are the in-app assistant for {app}, a discrete-event simulation editor. "
            + _PROMPT_BODY)


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

    def __init__(self, document: ModelDocument):
        self.document = document
        root = (document.path.parent if document.path is not None
                else Path(tempfile.mkdtemp(prefix="naga_assistant_")))
        self.workspace = agent_tools.Workspace(root)
        self.workspace.models[self.MODEL_ID] = document
        self.offered = frozenset()
        self._cm = None

    def offer(self, names) -> None:
        self.offered = frozenset(names)

    def begin_turn(self) -> None:
        self._cm = self.document.transaction()
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

    def run_tool(self, name: str, arguments: dict | None) -> dict:
        if name not in self.offered:
            return agent_tools.ToolError(
                "E_TOOL_NOT_OFFERED", f"'{name}' is not a tool of this session",
                f"use one of: {', '.join(sorted(self.offered))}").as_dict()
        if arguments is None:
            return agent_tools.ToolError(
                "E_BAD_ARGUMENTS", f"the arguments of '{name}' were not a JSON object",
                "send the arguments as one JSON object matching the tool's input schema").as_dict()
        agent_tools.use_workspace(self.workspace)
        if "model" in agent_tools.TOOLS[name].input_schema.get("properties", {}):
            arguments = {**arguments, "model": self.MODEL_ID}
        with agent_tools.python_consent_dropped():      # D186
            return agent_tools.call(name, **arguments)

    def model_summary(self) -> dict:
        agent_tools.use_workspace(self.workspace)
        return agent_tools.TOOLS["get_model_summary"].fn(model=self.MODEL_ID)


# ---------------------------------------------------------------- session
_STOPPED = {"length": "the model's reply was cut at its length limit",
            "refusal": "the model refused to answer"}


class AssistantSession:
    """One conversation against one `WorkspaceAdapter`. State lives in `history` (the plain
    turn record of `nagasim.llm`) and `usage`; the provider is stateless."""

    def __init__(self, workspace_adapter, provider, *, app: str = "Naga", extra_tools: tuple = (),
                 select_tools=offered_tools, max_tool_rounds: int = 12):
        self.adapter = workspace_adapter
        self.provider = provider
        self.system = system_prompt(app)
        self.max_tool_rounds = max_tool_rounds
        self.history: list[dict] = []
        self.usage = llm.Usage()
        names = select_tools(provider.kind, extra_tools)
        self.adapter.offer(names)
        self._tool_defs = tool_defs(names)

    def run_turn(self, user_text: str, *, on_text_delta=None, should_cancel=None) -> TurnResult:
        """Run one user turn to completion (or cancellation, or the round cap): ask, execute
        any requested tools, feed results back, repeat. The whole turn is one undo step; an
        exception or an unusable reply anywhere in the loop rolls every edit the turn made
        back out."""
        summary = self.adapter.model_summary()
        start = len(self.history)       # where this turn's entries begin
        self.history.append({"role": "user", "text": f"[Current model]\n"
                             f"{json.dumps(summary, separators=(',', ':'))}\n\n{user_text}"})
        tool_calls, text_out = [], []
        rounds, hit_cap, cancelled, error = 0, False, False, None
        self.adapter.begin_turn()
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                reply = self.provider.complete(self.system, self._tool_defs, self.history,
                                               on_text_delta)
                self.usage.merge(reply.usage)
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
                                         "content": json.dumps(result),
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
                          hit_round_cap=hit_cap, rounds=rounds, error=error)


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
    parser.add_argument("model", type=_bundled, help="path to the model .json file, or a bundled model's name (naga examples)")
    parser.add_argument("prompt", help="what to ask the assistant to do")
    parser.add_argument("--provider", choices=("anthropic", "openai"), default=None,
                        help="anthropic, or openai for any OpenAI-compatible server (Ollama, "
                             "LM Studio); default: anthropic when ANTHROPIC_API_KEY is set, "
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
