"""ROADMAP 3.3, D179/D180: the assistant's tool-use loop, driven end to end with scripted
providers and a scripted Anthropic client (no network). Covers the loop's undo grouping, rollback
on a mid-turn failure, the round cap, cancellation, the tool lists per provider kind, the rule
that only an offered tool runs, D186 (a model's Python never runs here) and `naga assist`.
The product's side (key store, dock) is tests/test_assistant_product.py.
"""
import json
import os
from types import SimpleNamespace

import pytest

from nagasim import agent_tools, llm
from nagasim import assistant as asst
from nagasim.core.document import ModelDocument
from test_llm import Block


# ---------------------------------------------------------------- fake Anthropic client
def text_block(text):
    return Block(type="text", text=text)


def tool_block(call_id, name, arguments):
    return Block(type="tool_use", id=call_id, name=name, input=arguments)


def usage(input_tokens=10, output_tokens=5):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                           cache_read_input_tokens=0, cache_creation_input_tokens=0)


def response(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage())


class FakeStream:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        for block in self._resp.content:
            if block.type == "text":
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="text_delta", text=block.text))

    def get_final_message(self):
        return self._resp


class FakeMessages:
    """Scripted `.stream(...)` calls: each entry in *responses* is either a
    response object (consumed in order) or an Exception instance (raised)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeStream(item)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def anthropic_provider(client):
    return llm.build_provider(llm.Config("anthropic", "", "m", "k", "omit"), client=client)


# ---------------------------------------------------------------- scripted provider
class ScriptedProvider:
    """Replies in order; an Exception instance is raised instead."""

    def __init__(self, replies, kind="hosted"):
        self.replies, self.kind, self.calls = list(replies), kind, []

    def complete(self, system, tools, history, on_text=None):
        self.calls.append({"system": system, "tools": tools, "history": [dict(e) for e in history]})
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        if on_text is not None and item.text:
            on_text(item.text)
        return item


def calls_reply(*calls):
    return llm.Reply("", tuple(llm.ToolCall(*c) for c in calls), "tools", llm.Usage(10, 5))


def text_reply(text, stop="end"):
    return llm.Reply(text, (), stop, llm.Usage(10, 5))


def make_adapter(document=None):
    return asst.WorkspaceAdapter(document or ModelDocument())


# ---------------------------------------------------------------- the tool lists
def test_curated_tools_exclude_file_and_undo_tools():
    excluded = {"new_model", "open_model", "save_model", "list_models", "undo", "redo",
               "export", "get_model_json", "get_model_summary"}
    assert asst.CURATED_TOOL_NAMES.isdisjoint(excluded)
    assert asst.CURATED_TOOL_NAMES <= set(agent_tools.TOOLS)
    assert asst.COMPACT_TOOL_NAMES <= asst.CURATED_TOOL_NAMES


def test_tool_defs_strip_the_model_argument():
    defs = asst.tool_defs(asst.offered_tools("hosted"))
    names = [d["name"] for d in defs]
    assert names == sorted(names)
    assert set(names) == {n for n in asst.CURATED_TOOL_NAMES if n in agent_tools.TOOLS}
    for d in defs:
        assert "model" not in d["input_schema"].get("properties", {})
        assert "model" not in d["input_schema"].get("required", [])


def test_a_products_extra_tools_are_offered_to_a_hosted_model_only():
    hosted = asst.offered_tools("hosted", ("list_models", "no_such_tool"))
    assert "list_models" in hosted and "no_such_tool" not in hosted     # only what is registered
    assert "list_models" not in asst.offered_tools("local", ("list_models",))


def test_the_system_prompt_names_the_app():
    assert asst.system_prompt().startswith("You are the in-app assistant for Naga, ")
    assert asst.system_prompt("Acme").startswith("You are the in-app assistant for Acme, ")


def test_workspace_adapter_confines_root_to_model_folder(tmp_path):
    adapter = make_adapter()
    assert adapter.workspace.root.is_dir()  # a fresh temp dir for an untitled model
    assert adapter.workspace.models[asst.WorkspaceAdapter.MODEL_ID] is adapter.document

    saved_dir = tmp_path / "sub"
    saved_dir.mkdir()
    doc = ModelDocument()
    doc.save(saved_dir / "m.json")
    adapter2 = make_adapter(doc)
    assert adapter2.workspace.root == saved_dir


# ---------------------------------------------------------------- the tool-use loop
def _mm1_build_script():
    """add_block x3, connect x2, upsert_record, validate_model, run_model,
    then a final answer -- the scenario the design calls for."""
    return [
        response([
            tool_block("t1", "add_block", {"type": "CREATE", "name": "arrive",
                                           "params": {"distribution": "Exponential",
                                                      "dist_params": {"rate": 1.0},
                                                      "max_arrivals": 20}}),
            tool_block("t2", "add_block", {"type": "PROCESS", "name": "serve",
                                           "params": {"resource_name": "Server",
                                                      "distribution": "Exponential",
                                                      "dist_params": {"rate": 1.25}}}),
            tool_block("t3", "add_block", {"type": "DISPOSE", "name": "leave"}),
        ], "tool_use"),
        response([
            tool_block("t4", "connect", {"source": "arrive", "target": "serve"}),
            tool_block("t5", "connect", {"source": "serve", "target": "leave"}),
        ], "tool_use"),
        response([tool_block("t6", "upsert_record", {"section": "resources",
                                                    "record": {"name": "Server", "capacity": 1}})],
                "tool_use"),
        response([tool_block("t7", "validate_model", {})], "tool_use"),
        response([tool_block("t8", "run_model", {"replications": 2, "seed": 0})], "tool_use"),
        response([text_block("Built and validated an M/M/1 model; ran 2 replications.")],
                "end_turn"),
    ]


def test_full_turn_builds_a_valid_model_as_one_undo_step():
    doc = ModelDocument()
    adapter = make_adapter(doc)
    session = asst.AssistantSession(adapter, anthropic_provider(FakeClient(_mm1_build_script())))

    result = session.run_turn("Build an M/M/1 queue")

    assert result.error is None
    assert not result.cancelled and not result.hit_round_cap
    assert [c.name for c in result.tool_calls] == [
        "add_block", "add_block", "add_block", "connect", "connect",
        "upsert_record", "validate_model", "run_model"]
    assert all("error" not in c.result for c in result.tool_calls)
    validate_result = result.tool_calls[6].result
    assert validate_result["ok"] is True
    assert set(doc.blocks) == {"arrive", "serve", "leave"}
    assert session.usage.input_tokens == 10 * 6  # one call per scripted response

    # ONE undo step for the whole turn.
    assert doc.can_undo
    doc.undo()
    assert doc.blocks == {}
    assert not doc.can_undo


def test_the_same_conversation_through_both_providers_gives_the_same_model():
    """D180: one scripted M/M/1 build, once on the Anthropic wire and once on the
    OpenAI-compatible one: same tool calls, same model, one undo step each."""
    from test_llm import reply, serve
    script = _mm1_build_script()
    openai_script = [
        reply(text="".join(b.text for b in r.content if b.type == "text"),
              calls=[(b.id, b.name, json.dumps(b.input)) for b in r.content if b.type == "tool_use"])
        for r in script]
    results = {}
    with serve(openai_script) as (url, _seen):
        providers = {
            "anthropic": anthropic_provider(FakeClient(script)),
            "openai": llm.build_provider(llm.resolve_config(
                provider="openai", base_url=url, model="m", environ={})),
        }
        for name, provider in providers.items():
            doc = ModelDocument()
            session = asst.AssistantSession(make_adapter(doc), provider,
                                            select_tools=lambda kind, extra=(): asst.offered_tools("hosted"))
            result = session.run_turn("Build an M/M/1 queue")
            assert result.error is None, name
            results[name] = ([(c.name, c.input) for c in result.tool_calls], doc.to_layout())
            doc.undo()
            assert doc.blocks == {} and not doc.can_undo, name
    assert results["anthropic"] == results["openai"]


def test_failing_tool_call_returns_structured_error_and_continues():
    doc = ModelDocument()
    adapter = make_adapter(doc)
    script = [
        response([tool_block("t1", "add_block", {"type": "NOT_A_BLOCK_TYPE", "name": "x"})],
                "tool_use"),
        response([text_block("That block type does not exist; nothing was added.")], "end_turn"),
    ]
    session = asst.AssistantSession(adapter, anthropic_provider(FakeClient(script)))

    result = session.run_turn("Add a bogus block")

    assert result.error is None
    assert len(result.tool_calls) == 1
    assert "error" in result.tool_calls[0].result
    assert doc.blocks == {}  # the failing call never mutated anything
    # The failure is fed back as the tool result the model sees, not lost.
    tool_entry = session.history[-2]
    assert tool_entry["role"] == "tool" and tool_entry["is_error"] is True


def test_exception_mid_turn_rolls_back_every_edit_the_turn_made():
    doc = ModelDocument()
    adapter = make_adapter(doc)
    script = [
        response([tool_block("t1", "add_block", {"type": "CREATE", "name": "arrive"})],
                "tool_use"),
        RuntimeError("connection reset"),
    ]
    session = asst.AssistantSession(adapter, anthropic_provider(FakeClient(script)))

    result = session.run_turn("Start building")

    assert result.error == "RuntimeError: connection reset"
    assert doc.blocks == {}          # rolled back, including the successful add_block
    assert not doc.can_undo          # no undo entry for a turn that failed


def test_round_cap_stops_a_runaway_loop():
    doc = ModelDocument()
    adapter = make_adapter(doc)
    # More scripted rounds than the cap allows, so a bug in the cap would spin
    # through every one of these instead of stopping early.
    script = [response([tool_block(f"t{i}", "validate_model", {})], "tool_use")
             for i in range(6)]
    client = FakeClient(script)
    session = asst.AssistantSession(adapter, anthropic_provider(client), max_tool_rounds=3)

    result = session.run_turn("Keep going forever")

    assert result.hit_round_cap is True
    assert result.rounds == 4                 # cap exceeded on the 4th round
    assert len(client.messages.calls) == 4     # stopped calling the API, not spun through all 6


def test_cancellation_between_rounds_stops_promptly():
    doc = ModelDocument()
    adapter = make_adapter(doc)
    script = [response([tool_block("t1", "add_block", {"type": "CREATE", "name": "arrive"})],
                       "tool_use")] * 5
    client = FakeClient(script)
    session = asst.AssistantSession(adapter, anthropic_provider(client))
    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 1   # let the first check through, cancel on the second

    result = session.run_turn("Go", should_cancel=should_cancel)

    assert result.cancelled is True
    assert result.tool_calls == []             # cancelled before the first tool ran
    assert len(client.messages.calls) == 1     # only one API call was made
    assert doc.blocks == {}


# ---------------------------------------------------------------- only an offered tool runs
def test_a_local_model_gets_the_compact_list_and_cannot_call_outside_it():
    doc = ModelDocument()
    provider = ScriptedProvider([calls_reply(("c1", "save_model", {"path": "x.json"})),
                                 text_reply("ok")], kind="local")
    session = asst.AssistantSession(make_adapter(doc), provider)
    result = session.run_turn("save it")
    assert [t["name"] for t in provider.calls[0]["tools"]] == sorted(asst.COMPACT_TOOL_NAMES)
    assert result.tool_calls[0].result["error"]["code"] == "E_TOOL_NOT_OFFERED"
    assert "write_model" in result.tool_calls[0].result["error"]["hint"]


def test_a_hosted_model_cannot_call_a_registered_tool_the_list_leaves_out():
    adapter = make_adapter()
    provider = ScriptedProvider([calls_reply(("c1", "save_model", {"path": "x.json"})),
                                 text_reply("ok")])
    result = asst.AssistantSession(adapter, provider).run_turn("save it")
    assert result.tool_calls[0].result["error"]["code"] == "E_TOOL_NOT_OFFERED"
    assert not (adapter.workspace.root / "x.json").exists()


def test_an_adapter_runs_nothing_until_a_session_offers_tools():
    adapter = make_adapter()
    assert adapter.run_tool("validate_model", {})["error"]["code"] == "E_TOOL_NOT_OFFERED"
    adapter.offer(["validate_model"])
    assert "error" not in adapter.run_tool("validate_model", {})


def test_the_selector_is_a_seam_a_product_can_replace():
    seen = []

    def only_validate(kind, extra=()):
        seen.append((kind, extra))
        return ["validate_model"]

    provider = ScriptedProvider([text_reply("hi")], kind="local")
    asst.AssistantSession(make_adapter(), provider, extra_tools=("x",),
                          select_tools=only_validate).run_turn("hello")
    assert seen == [("local", ("x",))]
    assert [t["name"] for t in provider.calls[0]["tools"]] == ["validate_model"]


def test_the_history_entry_carries_the_providers_blocks_only_when_it_has_them():
    blocks = [{"type": "text", "text": "ok"}]
    provider = ScriptedProvider([llm.Reply("ok", (), "end", llm.Usage(10, 5), blocks),
                                 text_reply("plain")])
    session = asst.AssistantSession(make_adapter(), provider)
    session.run_turn("one")
    session.run_turn("two")
    with_blocks, without = session.history[1], session.history[3]
    assert with_blocks["blocks"] == blocks and "blocks" not in without


def test_arguments_that_are_not_an_object_come_back_as_a_tool_error():
    doc = ModelDocument()
    provider = ScriptedProvider([calls_reply(("c1", "add_block", None)), text_reply("sorry")])
    result = asst.AssistantSession(make_adapter(doc), provider).run_turn("go")
    assert result.tool_calls[0].result["error"]["code"] == "E_BAD_ARGUMENTS"
    assert doc.blocks == {}


@pytest.mark.parametrize("bad", [text_reply("cut", stop="length"), text_reply("no", stop="refusal"),
                                 text_reply(""), llm.LLMError("cannot reach http://x: start Ollama")])
def test_a_cut_refused_or_empty_reply_or_a_dead_server_rolls_the_turn_back(bad):
    doc = ModelDocument()
    provider = ScriptedProvider([calls_reply(("c1", "add_block", {"type": "DISPOSE", "name": "d"})), bad])
    result = asst.AssistantSession(make_adapter(doc), provider).run_turn("go")
    assert result.error and doc.blocks == {} and not doc.can_undo


def _unanswered(history):
    """Ids of tool calls in *history* that no later tool entry answers."""
    asked = {c["id"] for entry in history if entry["role"] == "assistant" for c in entry["tool_calls"]}
    return asked - {entry["id"] for entry in history if entry["role"] == "tool"}


def test_a_failed_turn_leaves_the_history_as_it_was_before_the_turn():
    doc = ModelDocument()
    provider = ScriptedProvider([
        calls_reply(("c1", "add_block", {"type": "CREATE", "name": "arrive"})), text_reply("Added."),
        calls_reply(("c2", "add_block", {"type": "DISPOSE", "name": "d"})),
        llm.LLMError("cannot reach http://x: start Ollama")])
    session = asst.AssistantSession(make_adapter(doc), provider)
    assert session.run_turn("first").error is None
    before = [dict(entry) for entry in session.history]
    assert len(before) == 4                                  # user, calls, tool result, text

    assert session.run_turn("second").error                  # rolled back ...
    assert doc.blocks.keys() == {"arrive"}
    assert session.history == before                         # ... and so is the record


@pytest.mark.parametrize("how", ["cap", "cancel"])
def test_a_turn_stopped_with_tool_calls_pending_ends_without_an_unanswered_call(how):
    """After a round cap or a cancel the last assistant entry asks for tools that never ran; the
    next turn would send it as it is, which both wire formats reject."""
    doc = ModelDocument()
    provider = ScriptedProvider([calls_reply(("c1", "add_block", {"type": "CREATE", "name": "a"})),
                                 calls_reply(("c2", "add_block", {"type": "DISPOSE", "name": "d"})),
                                 text_reply("go on")])
    checks = {"n": 0}

    def cancel_after_the_second_reply():
        checks["n"] += 1
        return checks["n"] >= 4         # top, after reply 1 (tools run), top, after reply 2

    session = asst.AssistantSession(make_adapter(doc), provider,
                                    max_tool_rounds=1 if how == "cap" else 12)
    result = session.run_turn("go", **({"should_cancel": cancel_after_the_second_reply}
                                       if how == "cancel" else {}))

    assert (result.hit_round_cap, result.cancelled) == ((True, False) if how == "cap" else (False, True))
    assert doc.blocks.keys() == {"a"} and result.error is None
    assert not _unanswered(session.history)
    assert session.history[-1]["role"] == "tool"
    doc.undo()                                                # one undo step for the whole turn
    assert doc.blocks == {} and not doc.can_undo
    # and the conversation can go on from that history
    provider_two = ScriptedProvider([text_reply("done")])
    session.provider = provider_two
    assert session.run_turn("continue").error is None
    assert not _unanswered(provider_two.calls[0]["history"])


def test_no_tool_takes_an_endpoint_a_key_or_a_provider():
    banned = {"provider", "base_url", "endpoint", "api_key", "llm"}
    for name, spec in agent_tools.TOOLS.items():
        assert not banned & set(spec.input_schema.get("properties", {})), name


# ---------------------------------------------------------------- D186: a model's Python never runs here
def test_the_adapter_never_runs_a_models_python_and_restores_the_callers_consent(monkeypatch, tmp_path):
    from nagasim.core import agents as A
    from test_agents_python import _ring
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    path = tmp_path / "py.json"
    path.write_text(json.dumps(_ring(python=True)), encoding="utf-8")
    adapter = make_adapter(ModelDocument.load(path))
    adapter.offer(["run_model"])
    out = adapter.run_tool("run_model", {"replications": 1})
    assert out["error"]["code"] == "E_AGENT_PYTHON_REFUSED"
    assert os.environ[A.PYTHON_CONSENT] == "1"


# ---------------------------------------------------------------- redaction
@pytest.mark.parametrize("text,key,expected", [
    ("connecting with sk-ant-secret123 now", "sk-ant-secret123", "connecting with [REDACTED] now"),
    ("no key here", None, "no key here"),
    ("", "sk-ant-secret123", ""),
    ("sk-ant-secret123 twice: sk-ant-secret123", "sk-ant-secret123", "[REDACTED] twice: [REDACTED]"),
])
def test_redact(text, key, expected):
    assert asst.redact(text, key) == expected


# ---------------------------------------------------------------- naga assist
def test_naga_assist_one_turn_prints_the_transcript_and_saves(tmp_path, capsys):
    model_path = tmp_path / "m.json"
    ModelDocument().save(model_path)
    provider = ScriptedProvider([calls_reply(("c1", "add_block", {"type": "CREATE", "name": "arrive"})),
                                 text_reply("Added a CREATE block.")])

    assert asst.main([str(model_path), "add a create block"], provider=provider) == 0

    out = capsys.readouterr().out
    assert "[tool] add_block -> ok" in out
    assert f"Saved: {model_path}" in out
    assert "arrive" in ModelDocument.load(model_path).blocks


def test_naga_assist_with_no_server_exits_1_and_has_dropped_consent(tmp_path, capsys, monkeypatch):
    from nagasim.core.agents import PYTHON_CONSENT
    monkeypatch.setenv(PYTHON_CONSENT, "1")
    path = tmp_path / "m.json"
    ModelDocument().save(path)
    assert asst.main([str(path), "hello", "--provider", "openai", "--model-id", "gemma4:12b"]) == 1
    err = capsys.readouterr().err
    assert "start Ollama or choose another provider" in err and "Traceback" not in err
    assert PYTHON_CONSENT not in os.environ             # D186


def test_naga_assist_never_prints_the_key_it_holds(tmp_path, capsys):
    path = tmp_path / "m.json"
    ModelDocument().save(path)
    echo = ScriptedProvider([text_reply("the key is sk-secret-1")])
    assert asst.main([str(path), "hi"], provider=echo, anthropic_key="sk-secret-1") == 0
    out = capsys.readouterr().out
    assert "sk-secret-1" not in out and "[REDACTED]" in out

    failing = ScriptedProvider([llm.LLMError("HTTP 401 for sk-secret-1")])
    assert asst.main([str(path), "hi"], provider=failing, anthropic_key="sk-secret-1") == 1
    shown = capsys.readouterr()
    assert "sk-secret-1" not in shown.out + shown.err and "[REDACTED]" in shown.err


def test_naga_assist_redacts_the_key_it_resolved_from_a_server_that_echoes_it(tmp_path, capsys, monkeypatch):
    """No `provider=` seam: the key comes from NAGASIM_LLM_API_KEY through resolve_config, and a
    server that answers 401 with the bearer token in its body must not get it onto the screen."""
    from test_llm import serve
    monkeypatch.setenv("NAGASIM_LLM_API_KEY", "sk-env-secret")
    path = tmp_path / "m.json"
    ModelDocument().save(path)
    with serve([(401, '{"error": {"message": "bad bearer sk-env-secret"}}')]) as (url, seen):
        code = asst.main([str(path), "hi", "--provider", "openai", "--base-url", url,
                          "--model-id", "m"])
    assert code == 1
    assert seen[0]["headers"]["Authorization"] == "Bearer sk-env-secret"       # it was sent ...
    shown = capsys.readouterr()
    assert "sk-env-secret" not in shown.out + shown.err                          # ... and never shown
    assert "HTTP 401" in shown.err and "[REDACTED]" in shown.err


def test_naga_assist_is_a_command_with_its_own_help(capsys):
    from nagasim import cli
    assert cli.COMMANDS["assist"][:2] == ("assistant", "main")
    assert "assist" not in cli._PRODUCT_COMMAND_NAMES
    with pytest.raises(SystemExit) as stop:
        cli.main(["assist", "--help"])
    assert stop.value.code == 0
    assert "usage: naga assist" in capsys.readouterr().out
