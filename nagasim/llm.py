"""Language-model providers for the tool loop (D180, D187).

One turn record, two wire formats. History is a list of plain entries:

    {"role": "user", "text": str}
    {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "arguments"}], "blocks": [...]}
    {"role": "tool", "id": str, "name": str, "content": str, "is_error": bool}

Each provider converts it on every call. The conversion is deterministic, so a cached prefix
stays identical between rounds. An assistant entry has "blocks" only when the provider returned
its own content blocks (Anthropic, with its thinking blocks): that provider replays them verbatim,
and the other wire ignores them. The endpoint is the CALLER's configuration (flags, environment,
an application's saved settings): never a model file, never a tool argument.
"""
from __future__ import annotations

import dataclasses
import http.client
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

__all__ = ["Usage", "ToolCall", "Reply", "Config", "LLMError", "LOCAL_DEFAULT", "is_loopback",
           "parse_arguments", "resolve_config", "build_provider", "ANTHROPIC_MODELS",
           "DEFAULT_ANTHROPIC_MODEL", "estimate_cost"]

LOCAL_DEFAULT = "http://localhost:11434/v1"
MODELS_TIMEOUT_S, MODELS_MAX_BYTES = 15, 2_000_000     # a model list is small and quick
LOCAL_TIMEOUT_S, HOSTED_TIMEOUT_S = 1800, 600     # a local model may still be loading
# The claude-api skill: "For streaming requests, default to ~64000 ... don't lowball max_tokens".
# Every model in ANTHROPIC_MODELS allows at least this much output (Haiku 4.5: 64K).
MAX_TOKENS = 64000

# Prices are $/1M tokens, from the claude-api skill's model table (cached 2026-09-25). A model
# without prices has none we know: estimate_cost returns None for it (never guess a price).
# This is the offline fallback, newest first; the app asks the provider for its live list
# (`list_models`, D585). Never hard-code a model id anywhere else in the app.
ANTHROPIC_MODELS = {
    "claude-fable-5-1": {"label": "Claude Fable 5.1 (newest)"},
    "claude-opus-5-5": {"label": "Claude Opus 5.5 (default)",
                        "input_per_mtok": 4.00, "output_per_mtok": 20.00},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5 (cheaper)",
                          "input_per_mtok": 2.00, "output_per_mtok": 10.00},
    "claude-haiku-5-5": {"label": "Claude Haiku 5.5 (fastest, cheapest)"},
}
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


class LLMError(RuntimeError):
    """A failure a person can act on; the message is the whole advice."""


@dataclasses.dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, api_usage) -> None:
        for field in dataclasses.fields(self):
            setattr(self, field.name, getattr(self, field.name) + (getattr(api_usage, field.name, 0) or 0))

    merge = add     # another Usage has the same field names as an API usage object


def estimate_cost(usage: Usage, model: str) -> float | None:
    """Estimated $ for *usage* under *model*'s list price, or None for an unknown model
    (never guess a price). Anthropic's `input_tokens` excludes the cache tokens: a cache read
    costs 0.1x the input price and a cache write 1.25x."""
    prices = ANTHROPIC_MODELS.get(model)
    if prices is None or "input_per_mtok" not in prices:
        return None
    return (usage.input_tokens * prices["input_per_mtok"]
            + usage.cache_read_input_tokens * prices["input_per_mtok"] * 0.1
            + usage.cache_creation_input_tokens * prices["input_per_mtok"] * 1.25
            + usage.output_tokens * prices["output_per_mtok"]) / 1_000_000


@dataclasses.dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict | None      # None: what the model sent was not a JSON object


@dataclasses.dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: tuple
    stop: str                   # "end" | "tools" | "length" | "refusal"
    usage: Usage
    blocks: list | None = None  # the provider's own content blocks, JSON-ready, replayed verbatim
                                # by the same provider; None when the provider has none


@dataclasses.dataclass(frozen=True)
class Config:
    provider: str               # "anthropic" | "openai"
    base_url: str
    model: str
    api_key: str | None = dataclasses.field(repr=False)     # a log line must never carry it
    reasoning: str              # "none" | "low" | "medium" | "high" | "omit"


def is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Underscores are not legal in internet host names but docker-compose service names have them.
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _check_key(key: str | None) -> None:
    """A key with whitespace or a control character is a pasted line break, and it would be
    sent as (or rejected inside) an HTTP header. The message never holds the key."""
    if key and any(c.isspace() or not c.isprintable() for c in key):
        raise LLMError("the API key contains a space, a line break or a control character")


def _check_endpoint(url: str, key: str | None) -> str:
    """The address as it will be contacted, or LLMError. Checked and rebuilt from its parsed
    host and port, so the host that was judged is the host that is contacted (D187). A message
    never quotes the raw address (it may carry a password) or the key."""
    _check_key(key)
    if any(c == "\\" or c.isspace() or not c.isprintable() for c in url):
        raise LLMError("the address must not contain a backslash, whitespace or control "
                       "characters")      # parsers disagree about these, so they never get repaired
    invalid = "the address is not a valid http or https address"
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
        userinfo = parts.username is not None or parts.password is not None
    except ValueError:      # urlsplit's own messages quote the address
        raise LLMError(invalid) from None
    if scheme not in ("http", "https"):
        raise LLMError("the address must start with http:// or https://")
    if userinfo:
        raise LLMError("the address must not carry a user name or a password")
    # The rebuilt address has neither, so one is refused (even an empty one), not silently dropped.
    if "?" in url or "#" in url:
        raise LLMError("the address must not carry a query string or a fragment")
    if not host:
        raise LLMError("the address has no host name")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    # A scoped IPv6 address (%) names an interface, and parsers disagree about what follows it.
    if "%" in host or (literal is None and not _HOSTNAME.match(host)):
        raise LLMError(invalid)
    netloc = f"[{host}]" if literal is not None and literal.version == 6 else host
    if port is not None:
        netloc += f":{port}"
    # urlsplit tolerates text around a bracketed host ("evil.example[::1]"), so the parsed host
    # must account for all of the original text (an empty port, "localhost:", is the one slack).
    if parts.netloc.lower().removesuffix(":") != netloc:
        raise LLMError(invalid)
    rebuilt = urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))
    if key and scheme == "http" and not is_loopback(rebuilt):
        raise LLMError(f"refusing to send an API key over plain http to {host}: use https")
    return rebuilt


_ARGUMENT_TRIES = 16     # brackets tried as the start of a tool call's arguments


def parse_arguments(raw) -> dict | None:
    """A tool call's arguments as a dict, or None when they are not a JSON object. Local
    servers send a JSON string, an object, an empty string, or JSON inside prose or fences."""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if not isinstance(raw, str):
        return None
    decoder = json.JSONDecoder()
    tries = 0
    for start, char in enumerate(raw):
        if char in "{[":
            # Each failed try reads to the end of what it opened, so a try per bracket is
            # quadratic in an answer that only opens brackets: minutes on Python 3.14,
            # whose parser no longer gives up on depth (#370). Prose has few brackets.
            tries += 1
            if tries > _ARGUMENT_TRIES:
                return None
            try:
                value, _end = decoder.raw_decode(raw, start)
            except RecursionError:      # nested too deep to read: not an object we can use
                return None             # (and no retry from the next bracket, it would be slow)
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
    return None


def resolve_config(*, provider=None, base_url=None, model=None, api_key=None, reasoning=None,
                   environ=None, anthropic_key=None) -> Config:
    """Flags, then the environment, then defaults.

    *anthropic_key* is a key an application keeps for Anthropic (its credential store): it is
    the Anthropic provider's alone, ranked just after *api_key* and before `ANTHROPIC_API_KEY`,
    and no other provider ever receives it. It also makes Anthropic the default provider, as the
    environment variable does."""
    env = os.environ if environ is None else environ

    def pick(flag, name):
        return flag or env.get(f"NAGASIM_LLM_{name}") or ""

    held_anthropic = anthropic_key or env.get("ANTHROPIC_API_KEY") or ""
    generic_key = env.get("NAGASIM_LLM_API_KEY") or ""
    chosen = pick(provider, "PROVIDER") or ("anthropic" if held_anthropic else "openai")
    if chosen not in ("anthropic", "openai"):
        raise LLMError(f"unknown provider '{chosen}': use anthropic or openai")
    if chosen == "anthropic":
        key = api_key or held_anthropic or generic_key or None
        _check_key(key)
        return Config("anthropic", "", pick(model, "MODEL") or DEFAULT_ANTHROPIC_MODEL, key, "omit")
    key = api_key or generic_key or None    # ANTHROPIC_API_KEY is never sent to another server
    url = _check_endpoint(pick(base_url, "BASE_URL") or LOCAL_DEFAULT, key)
    name = pick(model, "MODEL")
    if not name:
        raise LLMError("no model chosen: pass --model-id or set NAGASIM_LLM_MODEL "
                       "(for Ollama, a name from `ollama list`)")
    return Config("openai", url, name, key,
                  pick(reasoning, "REASONING") or ("none" if is_loopback(url) else "omit"))


def build_provider(cfg: Config, *, client=None):
    return _Anthropic(cfg, client) if cfg.provider == "anthropic" else _OpenAICompat(cfg)


def _openai_messages(system: str, history: list) -> list:
    out = [{"role": "system", "content": system}]
    for entry in history:
        if entry["role"] == "user":
            out.append({"role": "user", "content": entry["text"]})
        elif entry["role"] == "assistant":
            message = {"role": "assistant", "content": entry["text"] or None}
            if entry["tool_calls"]:
                message["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"] or {})}}
                    for c in entry["tool_calls"]]
            out.append(message)
        else:
            out.append({"role": "tool", "tool_call_id": entry["id"], "content": entry["content"]})
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # a 3xx then surfaces as HTTPError: the key never follows a redirect


class _OpenAICompat:
    """`POST {base_url}/chat/completions`: Ollama, LM Studio, vLLM and the hosted services
    that speak the same format. No output-token limit is sent (providers disagree on its
    name), and no token streaming yet: text arrives per round."""

    def __init__(self, cfg: Config):
        # Checked here as well as in resolve_config: a hand-built Config must not skip the guard.
        self.cfg = dataclasses.replace(cfg, base_url=_check_endpoint(cfg.base_url, cfg.api_key))
        self.kind = "local" if is_loopback(self.cfg.base_url) else "hosted"
        # No proxy for a local server (the request and its key would go to the proxy).
        self._opener = urllib.request.build_opener(
            _NoRedirect(), *([urllib.request.ProxyHandler({})] if self.kind == "local" else []))

    def complete(self, system, tools, history, on_text=None) -> Reply:
        body = {"model": self.cfg.model, "messages": _openai_messages(system, history)}
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        if self.cfg.reasoning != "omit":
            body["reasoning_effort"] = self.cfg.reasoning
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        timeout = LOCAL_TIMEOUT_S if self.kind == "local" else HOSTED_TIMEOUT_S
        where = self.cfg.base_url
        try:
            request = urllib.request.Request(where + "/chat/completions",
                                             json.dumps(body).encode("utf-8"), headers)
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise LLMError(f"{where} answered HTTP {exc.code}: redirects are not followed; "
                               "set the final address") from None
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except (OSError, http.client.HTTPException):    # the error body itself was cut off
                detail = ""
            hint = " (does the address end in /v1?)" if exc.code == 404 else ""
            problem = f"{where} answered HTTP {exc.code}{hint}" + (f": {detail}" if detail else "")
            if exc.code == 400 and "reasoning_effort" in body:
                problem += " (if the server rejects reasoning_effort, set NAGASIM_LLM_REASONING=omit)"
            raise LLMError(problem) from None
        except http.client.HTTPException as exc:    # before OSError: RemoteDisconnected is both
            raise LLMError(f"{where} closed the connection or sent a broken answer "
                           f"({type(exc).__name__})") from None
        except (urllib.error.URLError, OSError) as exc:
            advice = ("start Ollama or choose another provider" if self.kind == "local"
                      else "check the address and the network")
            raise LLMError(f"cannot reach {where}: {advice} "
                           f"({getattr(exc, 'reason', exc)})") from None
        except ValueError:      # the request could not be encoded; the message would quote the key
            raise LLMError(f"{where}: the request could not be sent; check the address and "
                           "the key for unusual characters") from None
        try:
            reply = _openai_reply(json.loads(raw), len(history))
        except (ValueError, RecursionError, AttributeError, TypeError, KeyError, IndexError):
            # ValueError: not JSON (or not UTF-8). RecursionError: nested too deep for the parser.
            # The rest: JSON of the wrong shape at some depth.
            raise LLMError(f"{where} did not answer with a chat completion: "
                           f"{raw.decode('utf-8', 'replace')[:300]}") from None
        if on_text is not None and reply.text:
            on_text(reply.text)
        return reply

    def list_models(self) -> list[str]:
        """`GET {base_url}/models` on the configured address alone (same checks, no redirect,
        no proxy for a local server). LLMError when it cannot be read; the message holds
        neither the key nor the server's body."""
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        where = self.cfg.base_url
        try:
            request = urllib.request.Request(where + "/models", headers=headers)
            with self._opener.open(request, timeout=MODELS_TIMEOUT_S) as response:
                return _model_ids(json.loads(response.read(MODELS_MAX_BYTES)))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"{where} answered HTTP {exc.code} to the model list") from None
        except (ValueError, RecursionError, http.client.HTTPException, OSError):
            raise LLMError(f"cannot read the model list from {where}") from None


def _model_ids(data) -> list[str]:
    """Model ids from a `{"data": [{"id": ...}]}` list, in the order sent; ValueError (which
    `list_models` turns into LLMError) when the body is not that shape."""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("no model list")
    return [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _openai_call(call: dict, fallback_id: str) -> ToolCall:
    """One tool call. A missing or non-text id gets *fallback_id* (some local servers send none);
    a name that is not text is a wrong-shaped answer (TypeError)."""
    function = call.get("function") or {}
    name = function.get("name") or ""
    if not isinstance(name, str):
        raise TypeError("tool name is not text")
    call_id = call.get("id")
    return ToolCall(call_id if isinstance(call_id, str) and call_id else fallback_id, name,
                    parse_arguments(function.get("arguments")))


def _openai_reply(data, n_history: int) -> Reply:
    """A chat completion as a Reply. Raises AttributeError, TypeError, KeyError or IndexError
    when *data* is not shaped like one; a wrong-shaped usage block is read as zero (the answer
    is worth more than its token count)."""
    choice = data["choices"][0]
    message = choice.get("message") or {}
    calls = tuple(_openai_call(c, f"call_{n_history}_{i}")
                  for i, c in enumerate(message.get("tool_calls") or []))
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise TypeError("content is not text")
    stop = "tools" if calls else {"length": "length", "content_filter": "refusal"}.get(
        choice.get("finish_reason"), "end")
    usage = data.get("usage")
    counts = [usage.get(name) if isinstance(usage, dict) else None
              for name in ("prompt_tokens", "completion_tokens")]
    return Reply(text, calls, stop, Usage(*[n if isinstance(n, int) else 0 for n in counts]))


def _anthropic_messages(history: list) -> list:
    out = []
    for entry in history:
        if entry["role"] == "user":
            out.append({"role": "user", "content": entry["text"]})
        elif entry["role"] == "assistant":
            # The model's own blocks when this provider wrote them (thinking and its signature
            # must come back with the tool_use); else rebuilt from the neutral record.
            blocks = entry.get("blocks")
            if not blocks:
                blocks = [{"type": "text", "text": entry["text"]}] if entry["text"] else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"],
                            "input": c["arguments"] or {}} for c in entry["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        else:
            block = {"type": "tool_result", "tool_use_id": entry["id"],
                     "content": entry["content"], "is_error": entry["is_error"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


class _Anthropic:
    kind = "hosted"

    def __init__(self, cfg: Config, client=None):
        _check_key(cfg.api_key)     # a hand-built Config must not skip the check
        self.cfg = cfg
        if client is None:
            if not cfg.api_key:
                raise LLMError("no Anthropic API key: set ANTHROPIC_API_KEY, or choose the "
                               "openai provider for a local model")
            try:
                import anthropic
            except ImportError:
                raise LLMError('the anthropic package is not installed: '
                               'pip install "nagasim[assistant]"') from None
            client = anthropic.Anthropic(api_key=cfg.api_key)
        self.client = client

    def list_models(self) -> list[str]:
        """The account's models, newest first (the SDK's call to the one Anthropic host).
        LLMError when it cannot be read; only the error's class is quoted, never its text."""
        try:
            return [m.id for m in self.client.models.list(limit=100, timeout=MODELS_TIMEOUT_S).data]
        except Exception as exc:
            raise LLMError(f"cannot read the Anthropic model list ({type(exc).__name__})") from None

    def complete(self, system, tools, history, on_text=None) -> Reply:
        defs = [dict(t) for t in tools]
        if defs:        # system + tools are static for a session: the one prefix worth caching
            defs[-1] = {**defs[-1], "cache_control": {"type": "ephemeral"}}
        # No empty `tools` list: like the other wire, a plain question sends none at all.
        with self.client.messages.stream(
                model=self.cfg.model, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=_anthropic_messages(history), **({"tools": defs} if defs else {})) as stream:
            for event in stream:
                delta = getattr(event, "delta", None)
                if (event.type == "content_block_delta" and on_text is not None
                        and getattr(delta, "type", None) == "text_delta"):
                    on_text(delta.text)
            response = stream.get_final_message()
        usage = Usage()
        usage.add(response.usage)
        calls = tuple(ToolCall(b.id, b.name, dict(b.input)) for b in response.content
                      if b.type == "tool_use")
        stop = {"tool_use": "tools", "max_tokens": "length", "refusal": "refusal"}.get(
            response.stop_reason, "end")
        return Reply("".join(b.text for b in response.content if b.type == "text"), calls, stop, usage,
                     [b.to_dict() for b in response.content])
