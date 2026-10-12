"""D180/D187: the turn record's two wire formats and where the endpoint may come from."""
import dataclasses
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from nagasim import llm


# ---------------------------------------------------------------- fakes
# Kept in this file, not in a helper module or another test: the engine's export copies only
# the engine's own test_*.py files.
def reply(text="", calls=(), finish="stop"):
    """One /chat/completions answer. *calls* is [(id, name, arguments)]; arguments are sent
    as given, so a test can pass a string, a dict or garbage."""
    message = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [{"id": i, "type": "function",
                                  "function": {"name": n, "arguments": a}} for i, n, a in calls]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@contextmanager
def serve(script):
    """Yield (base_url, requests): *script* items are a reply dict, (status, body_text),
    (status, body_text, headers_dict), or None to hang up without answering."""
    pending, seen = list(script), []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append({"path": self.path, "headers": dict(self.headers),
                         "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            item = pending.pop(0)
            if item is None:
                return
            status, body, headers = ((item + ({},))[:3] if isinstance(item, tuple)
                                     else (200, json.dumps(item), {}))
            data = body.encode("utf-8")
            self.send_response(status)
            # An item's headers win, so a test can promise more bytes than it sends.
            for name, value in {"Content-Type": "application/json",
                                "Content-Length": str(len(data)), **headers}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class Block(SimpleNamespace):
    """A content block of the SDK's final message: its fields as attributes, and `to_dict()`
    as the SDK's models have it."""

    def to_dict(self):
        return dict(vars(self))


class FakeAnthropic:
    """The slice of the Anthropic SDK the provider uses: `messages.stream(**kw)` as a context
    manager that yields one text delta and then the final message (*content*, when given, is
    its list of blocks)."""

    def __init__(self, text="done", stop_reason="end_turn", content=None):
        usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.text = text
        self.response = SimpleNamespace(content=content or [Block(type="text", text=text)],
                                        stop_reason=stop_reason, usage=usage)
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="content_block_delta",
                              delta=SimpleNamespace(type="text_delta", text=self.text))

    def get_final_message(self):
        return self.response


def closed_port():
    """A loopback port nothing listens on (not the conftest guard port, which is shared)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


TOOLS = [{"name": "add_block", "description": "Add a block.",
          "input_schema": {"type": "object", "properties": {"type": {"type": "string"}}}}]
HISTORY = [
    {"role": "user", "text": "build"},
    {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "add_block",
                                                      "arguments": {"type": "CREATE"}}]},
    {"role": "tool", "id": "c1", "name": "add_block", "content": '{"ok": true}', "is_error": False},
]
HI = [{"role": "user", "text": "hi"}]
SECRET = "sk-secret-123"


def local(url, **kw):
    return llm.resolve_config(provider="openai", base_url=url, model="gemma4:12b", environ={}, **kw)


# ---------------------------------------------------------------- the OpenAI-compatible wire format
def test_openai_wire_format_and_reply():
    with serve([reply(calls=[("c2", "add_block", '{"type": "DISPOSE"}')])]) as (url, seen):
        out = llm.build_provider(local(url)).complete("SYS", TOOLS, HISTORY)
    body = seen[0]["body"]
    assert seen[0]["path"] == "/v1/chat/completions"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"type": "CREATE"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}
    assert body["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert "max_tokens" not in body and body["reasoning_effort"] == "none"
    assert "Authorization" not in seen[0]["headers"]
    assert out.stop == "tools" and out.tool_calls[0] == llm.ToolCall("c2", "add_block", {"type": "DISPOSE"})
    assert out.usage.input_tokens == 10 and out.usage.output_tokens == 5


@pytest.mark.parametrize("raw, expected", [
    ('{"a": 1}', {"a": 1}), ({"a": 1}, {"a": 1}), ("", {}), (None, {}),
    ('Sure:\n```json\n{"a": 1}\n```', {"a": 1}), ("[1, 2]", None), ("not json", None), (3, None),
    pytest.param("[" * 100_000, None, id="nested-too-deep")])
def test_arguments_in_the_shapes_local_servers_send(raw, expected):
    assert llm.parse_arguments(raw) == expected


def test_a_server_that_is_down_says_what_to_do():
    cfg = local(f"http://127.0.0.1:{closed_port()}/v1")
    with pytest.raises(llm.LLMError, match="start Ollama or choose another provider"):
        llm.build_provider(cfg).complete("SYS", [], HI)


def test_an_address_without_v1_and_an_http_error_are_explained():
    with serve([(404, "page not found"), (500, "x" * 1000)]) as (url, _seen):
        provider = llm.build_provider(local(url))
        with pytest.raises(llm.LLMError, match=r"HTTP 404 \(does the address end in /v1\?\)"):
            provider.complete("SYS", [], HI)
        with pytest.raises(llm.LLMError) as err:
            provider.complete("SYS", [], HI)
    assert "HTTP 500" in str(err.value) and len(str(err.value)) < 500


def test_an_error_body_that_is_cut_off_still_gives_the_status():
    with serve([(500, "abc", {"Content-Length": "100"})]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="HTTP 500"):
            llm.build_provider(local(url)).complete("SYS", [], HI)


def test_a_400_while_reasoning_is_sent_names_the_setting():
    with serve([(400, "unknown field"), (400, "unknown field")]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="NAGASIM_LLM_REASONING=omit"):
            llm.build_provider(local(url)).complete("SYS", [], HI)
        with pytest.raises(llm.LLMError) as err:
            llm.build_provider(local(url, reasoning="omit")).complete("SYS", [], HI)
    assert "NAGASIM_LLM_REASONING" not in str(err.value)


def test_length_refusal_and_missing_ids():
    with serve([reply(text="cut", finish="length"), reply(text="no", finish="content_filter"),
                reply(calls=[(None, "add_block", "{}")]),
                reply(calls=[(7, "add_block", "{}")])]) as (url, _seen):
        provider = llm.build_provider(local(url))
        assert provider.complete("SYS", [], HI).stop == "length"
        assert provider.complete("SYS", [], HI).stop == "refusal"
        assert provider.complete("SYS", TOOLS, HISTORY).tool_calls[0].id == "call_3_0"
        assert provider.complete("SYS", [], HI).tool_calls[0].id == "call_1_0"    # an id that is not text


@pytest.mark.parametrize("body, shown", [('{"error": {"message": "boom"}}', "boom"),
                                         ("<html>oops</html>", "oops"), ("[1, 2]", "[1, 2]")])
def test_a_200_that_is_not_a_chat_completion_is_an_llm_error(body, shown):
    with serve([(200, body)]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="did not answer with a chat completion") as err:
            llm.build_provider(local(url)).complete("SYS", [], HI)
    assert shown in str(err.value)


@pytest.mark.parametrize("body", [
    {"choices": [{"message": "text"}]},                      # message is not an object
    {"choices": [{"message": {"tool_calls": ["x"]}}]},       # a tool call is not an object
    {"choices": [{"message": {"content": 5}}]},              # the text is not a string
    {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": 5}}]}}]},   # nor a name
    {"choices": ["x"]}])
def test_a_completion_of_the_wrong_shape_is_an_llm_error(body):
    with serve([(200, json.dumps(body))]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="did not answer with a chat completion"):
            llm.build_provider(local(url)).complete("SYS", [], HI)


def test_answers_nested_too_deep_for_the_json_parser_are_handled():
    with serve([(200, "[" * 100_000), reply(calls=[("c1", "add_block", "[" * 100_000)])]) as (url, _seen):
        provider = llm.build_provider(local(url))
        with pytest.raises(llm.LLMError, match="did not answer with a chat completion"):
            provider.complete("SYS", [], HI)
        assert provider.complete("SYS", [], HI).tool_calls[0].arguments is None


def test_a_wrong_shaped_usage_costs_nothing_but_the_count():
    body = {"choices": [{"message": {"content": "hi"}}], "usage": "n/a"}
    with serve([(200, json.dumps(body))]) as (url, _seen):
        out = llm.build_provider(local(url)).complete("SYS", [], HI)
    assert out.text == "hi" and out.usage == llm.Usage()


def test_a_connection_cut_mid_answer_is_an_llm_error():
    with serve([(200, "abc", {"Content-Length": "100"})]) as (url, _seen):
        with pytest.raises(llm.LLMError, match=r"broken answer \(IncompleteRead\)"):
            llm.build_provider(local(url)).complete("SYS", [], HI)


def test_a_dropped_connection_is_not_read_as_a_server_that_is_down():
    with serve([None]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="closed the connection or sent a broken answer") as err:
            llm.build_provider(local(url)).complete("SYS", [], HI)
    assert "RemoteDisconnected" in str(err.value) and "Ollama" not in str(err.value)


def test_openai_text_reaches_on_text():
    streamed = []
    with serve([reply(text="hello")]) as (url, _seen):
        out = llm.build_provider(local(url)).complete("SYS", [], HI, on_text=streamed.append)
    assert streamed == ["hello"] and out.text == "hello"


# ---------------------------------------------------------------- where the endpoint may point
def test_where_the_endpoint_may_come_from():
    env = {"NAGASIM_LLM_PROVIDER": "openai", "NAGASIM_LLM_BASE_URL": "http://localhost:1234/v1/",
           "NAGASIM_LLM_MODEL": "m"}
    cfg = llm.resolve_config(environ=env)
    assert (cfg.base_url, cfg.model, cfg.reasoning) == ("http://localhost:1234/v1", "m", "none")
    assert llm.resolve_config(model="flag", environ=env).model == "flag"
    assert llm.resolve_config(environ={"ANTHROPIC_API_KEY": "k"}).provider == "anthropic"
    assert llm.resolve_config(model="m", environ={}).base_url == llm.LOCAL_DEFAULT
    with pytest.raises(llm.LLMError, match="no model chosen"):
        llm.resolve_config(environ={})
    with pytest.raises(llm.LLMError, match="plain http"):
        llm.resolve_config(provider="openai", base_url="http://example.com/v1", model="m",
                           api_key="k", environ={})
    hosted = llm.resolve_config(provider="openai", base_url="https://example.com/v1", model="m",
                                api_key="k", environ={})
    assert hosted.reasoning == "omit" and llm.build_provider(hosted).kind == "hosted"


@pytest.mark.parametrize("url", [
    "HTTP://example.com/v1",                 # the scheme is compared lower-cased
    " http://example.com/v1",                # whitespace never rescues a value
    "example.invalid/v1",                    # no scheme
    "ftp://x/v1",
    "http://user:hunter2@localhost:11434/v1",
    "http://localhost:11434/v1?api_key=hunter2",    # a query or a fragment is refused,
    "http://localhost:11434/v1#hunter2", "http://localhost:11434/v1?",     # not silently dropped
    "http://localhost\\@example.com/v1",     # some parsers read a backslash as a slash
    "http://localhost:notaport/v1",
    "http://[::1/v1", "http://[zz]/v1",      # urlsplit itself raises on these
    "http://user:hunter2@exa\u2100mple.com/v1",
    "http://evil.example[::1]/v1",           # the parsed host must be the whole host text
    "http://[::1]evil.example/v1", "http://[::1]evil.example:80/v1",
    "http://[::1%40evil.example]/v1"])
def test_a_bad_endpoint_is_refused_without_echoing_secrets(url):
    with pytest.raises(llm.LLMError) as err:
        llm.resolve_config(provider="openai", base_url=url, model="m", api_key=SECRET, environ={})
    for secret in ("hunter2", "evil", SECRET):
        assert secret not in str(err.value)


def test_host_names_follow_letters_digits_dots_hyphens_and_underscores():
    with pytest.raises(llm.LLMError, match="not a valid http or https address"):
        local("http://exa!mple.com/v1")         # no key involved: the name rule itself refuses it
    service = local("http://my_service:8000/v1")        # a docker-compose service name
    assert service.base_url == "http://my_service:8000/v1" and not llm.is_loopback(service.base_url)
    with pytest.raises(llm.LLMError, match="plain http"):
        local("http://exa_mple.com/v1", api_key=SECRET)


def test_the_contacted_host_and_port_are_the_parsed_ones():
    assert local("http://localhost:/v1").base_url == "http://localhost/v1"
    cfg = local("http://[::1]:11434/v1")
    assert cfg.base_url == "http://[::1]:11434/v1" and llm.is_loopback(cfg.base_url)
    assert llm.build_provider(cfg).kind == "local"


@pytest.mark.parametrize("tail", ["\n", " ", "\x07"])
def test_a_key_with_a_line_break_space_or_control_character_is_refused(tail):
    with pytest.raises(llm.LLMError) as err:
        local("http://localhost:11434/v1", api_key=SECRET + tail)
    assert SECRET not in str(err.value)


def test_the_anthropic_key_gets_the_same_character_check():
    with pytest.raises(llm.LLMError) as err:
        llm.resolve_config(environ={"ANTHROPIC_API_KEY": SECRET + "\n"})
    assert SECRET not in str(err.value)
    with pytest.raises(llm.LLMError) as err:
        llm.build_provider(llm.Config("anthropic", "", "m", SECRET + " ", "omit"), client=FakeAnthropic())
    assert SECRET not in str(err.value)


def test_a_key_the_wire_cannot_carry_is_an_llm_error_not_a_crash():
    cyrillic = "\u043a\u043b\u044e\u0447"      # printable, but not latin-1 (HTTP header encoding)
    with serve([]) as (url, _seen):
        with pytest.raises(llm.LLMError, match="could not be sent") as err:
            llm.build_provider(local(url, api_key=cyrillic)).complete("SYS", [], HI)
    assert cyrillic not in str(err.value)


def test_a_hand_built_config_is_checked_and_its_repr_hides_the_key():
    with pytest.raises(llm.LLMError, match="plain http"):
        llm.build_provider(llm.Config("openai", "http://example.com/v1", "m", SECRET, "omit"))
    cfg = llm.Config("openai", "https://example.com/v1", "m", SECRET, "omit")
    assert cfg.api_key == SECRET and SECRET not in repr(cfg)


def test_the_address_is_rebuilt_from_its_parsed_parts():
    cfg = llm.resolve_config(provider="openai", base_url="HTTP://localhost:1234/v1/",
                             model="m", environ={})
    assert cfg.base_url == "http://localhost:1234/v1"


def test_a_key_goes_to_a_loopback_server_even_with_a_proxy_set(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{closed_port()}")
    with serve([reply(text="ok")]) as (url, seen):
        out = llm.build_provider(local(url, api_key=SECRET)).complete("SYS", [], HI)
    assert out.text == "ok" and seen[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_a_redirect_is_never_followed_so_the_key_stays_put():
    with serve([]) as (url_b, seen_b):
        with serve([(302, "", {"Location": url_b + "/chat/completions"})]) as (url_a, _seen):
            with pytest.raises(llm.LLMError, match="HTTP 302"):
                llm.build_provider(local(url_a, api_key=SECRET)).complete("SYS", [], HI)
    assert seen_b == []


def test_the_key_comes_from_the_providers_own_variable_first():
    both = {"ANTHROPIC_API_KEY": "anthropic-key", "NAGASIM_LLM_API_KEY": "nagasim-key"}
    assert llm.resolve_config(environ=both).api_key == "anthropic-key"
    assert llm.resolve_config(provider="anthropic", environ={"NAGASIM_LLM_API_KEY": "n"}).api_key == "n"
    assert llm.resolve_config(provider="openai", model="m", environ=both).api_key == "nagasim-key"
    only_anthropic = {"ANTHROPIC_API_KEY": "anthropic-key"}
    assert llm.resolve_config(provider="openai", model="m", environ=only_anthropic).api_key is None


def test_a_key_an_application_stores_for_anthropic_goes_to_anthropic_only():
    """`anthropic_key` (an application's credential store) is never sent to an OpenAI-compatible
    endpoint, whether the provider is chosen by a flag, by the environment or by default."""
    stored = "stored-anthropic-key"
    for chosen in ({"provider": "openai"}, {"environ": {"NAGASIM_LLM_PROVIDER": "openai"}}):
        env = {"environ": {}, **chosen}
        cfg = llm.resolve_config(model="m", anthropic_key=stored, **env)
        assert cfg.provider == "openai" and cfg.api_key is None
    both = llm.resolve_config(provider="openai", model="m", anthropic_key=stored,
                              environ={"NAGASIM_LLM_API_KEY": "nagasim-key"})
    assert both.api_key == "nagasim-key"
    # ... and it is an Anthropic key: it picks Anthropic by default and outranks the variable
    # (after an explicit api_key).
    assert llm.resolve_config(anthropic_key=stored, environ={}).provider == "anthropic"
    assert llm.resolve_config(anthropic_key=stored, environ={}).api_key == stored
    both_keys = {"ANTHROPIC_API_KEY": "variable", "NAGASIM_LLM_API_KEY": "nagasim-key"}
    assert llm.resolve_config(anthropic_key=stored, environ=both_keys).api_key == stored
    assert llm.resolve_config(anthropic_key=stored, api_key="flag", environ=both_keys).api_key == "flag"
    assert llm.resolve_config(provider="anthropic", environ=both_keys).api_key == "variable"
    with pytest.raises(llm.LLMError, match="control character"):
        llm.resolve_config(provider="anthropic", anthropic_key="bad key", environ={})


# ---------------------------------------------------------------- the Anthropic wire format
def test_anthropic_wire_format_keeps_the_cache_prefix():
    client = FakeAnthropic("done", "end_turn")
    cfg = llm.Config("anthropic", "", "m", "k", "omit")
    streamed = []
    out = llm.build_provider(cfg, client=client).complete("SYS", TOOLS, HISTORY, on_text=streamed.append)
    call = client.calls[0]
    assert call["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"][1]["content"] == [{"type": "tool_use", "id": "c1", "name": "add_block",
                                               "input": {"type": "CREATE"}}]
    assert call["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "c1", "content": '{"ok": true}', "is_error": False}]}
    assert out.text == "done" and out.stop == "end" and streamed == ["done"]


def test_anthropic_replays_its_own_content_blocks_verbatim():
    """The assistant turn that carries a tool_use goes back unchanged, thinking and its
    signature included (the API can reject a tool round that lost them)."""
    blocks = [Block(type="thinking", thinking="plan", signature="sig-1"),
              Block(type="text", text="Adding."),
              Block(type="tool_use", id="c1", name="add_block", input={"type": "CREATE"})]
    client = FakeAnthropic(content=blocks, stop_reason="tool_use")
    provider = llm.build_provider(llm.Config("anthropic", "", "m", "k", "omit"), client=client)
    first = provider.complete("SYS", TOOLS, HI)
    # the entries the session appends: the assistant turn with the reply's blocks, then the result
    history = HI + [
        {"role": "assistant", "text": first.text, "blocks": first.blocks, "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in first.tool_calls]},
        {"role": "tool", "id": "c1", "name": "add_block", "content": "{}", "is_error": False}]
    provider.complete("SYS", TOOLS, history)
    content = client.calls[1]["messages"][1]["content"]
    assert content == [b.to_dict() for b in blocks]
    assert content[0] == {"type": "thinking", "thinking": "plan", "signature": "sig-1"}


def test_an_assistant_entry_without_blocks_is_rebuilt_from_text_and_calls():
    """What the other provider (or older code) wrote has no `blocks`."""
    entry = {"role": "assistant", "text": "Adding.",
             "tool_calls": [{"id": "c1", "name": "add_block", "arguments": {"type": "CREATE"}}]}
    assert llm._anthropic_messages([entry])[0]["content"] == [
        {"type": "text", "text": "Adding."},
        {"type": "tool_use", "id": "c1", "name": "add_block", "input": {"type": "CREATE"}}]


def test_the_openai_wire_ignores_the_blocks_of_the_other_provider():
    entry = {**HISTORY[1], "blocks": [{"type": "thinking", "thinking": "x", "signature": "s"}]}
    assert llm._openai_messages("SYS", [HISTORY[0], entry, HISTORY[2]]) == \
        llm._openai_messages("SYS", HISTORY)


def test_anthropic_refusal_is_a_stop():
    cfg = llm.Config("anthropic", "", "m", "k", "omit")
    out = llm.build_provider(cfg, client=FakeAnthropic("", "refusal")).complete("SYS", [], HI)
    assert out.stop == "refusal"


def test_anthropic_with_no_tools_sends_no_tools_key():
    """Like the other wire: an empty list is left out (a plain question, "Test connection")."""
    client = FakeAnthropic("pong", "end_turn")
    llm.build_provider(llm.Config("anthropic", "", "m", "k", "omit"), client=client).complete(
        "SYS", [], HI)
    assert "tools" not in client.calls[0]
    llm.build_provider(llm.Config("anthropic", "", "m", "k", "omit"), client=client).complete(
        "SYS", TOOLS, HI)
    assert client.calls[1]["tools"][0]["name"] == "add_block"


def test_anthropic_without_a_key_or_the_sdk_is_a_clear_message(monkeypatch):
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.build_provider(llm.Config("anthropic", "", "m", None, "omit"))
    monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
    with pytest.raises(llm.LLMError, match=r"nagasim\[assistant\]"):
        llm.build_provider(llm.Config("anthropic", "", "m", "k", "omit"))


# ---------------------------------------------------------------- usage and cost
def test_usage_merge_and_cost():
    total = llm.Usage(1, 2)
    total.merge(llm.Usage(10, 20, 3, 4))
    assert dataclasses.astuple(total) == (11, 22, 3, 4)
    prices = llm.ANTHROPIC_MODELS[llm.DEFAULT_ANTHROPIC_MODEL]
    assert llm.estimate_cost(llm.Usage(1_000_000, 1_000_000), llm.DEFAULT_ANTHROPIC_MODEL) == \
        pytest.approx(prices["input_per_mtok"] + prices["output_per_mtok"])
    assert llm.estimate_cost(llm.Usage(1, 1), "no-such-model") is None
    # Cache reads cost 0.1x and cache writes 1.25x the input price (`input_tokens` excludes both).
    # Sonnet 5.5, $2 in / $10 out per Mtok, 1M tokens of each kind:
    # 1M * $2 + 1M * $10 + 1M * $2 * 0.1 + 1M * $2 * 1.25 = 2.00 + 10.00 + 0.20 + 2.50 = $14.70
    assert llm.estimate_cost(llm.Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000),
                             "claude-sonnet-5-5") == pytest.approx(14.70)
