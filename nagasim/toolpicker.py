"""Optional tool picker: a model that reorders find_tool's answer (D671, D672).

Off unless the person starting the server asks for it (`naga mcp --tool-picker gte|haiku`); no tool
argument and no model file can turn it on. Two pickers:

- "gte", the local zero-shot cross-encoder Alibaba-NLP/gte-reranker-modernbert-base, re-ranks
  find_tool's top ten (top-1 14 vs 8 of 25 on the frozen blind set, about 0.1 s on a 4 GB laptop GPU).
  Its first use downloads the weights from Hugging Face; sentence-transformers is the `picker` extra.
- "haiku", Claude Haiku on the Anthropic API, chooses among every tool the server exposes (top-1 22
  of 25, about 0.9 s). The query and the tool catalogue go to api.anthropic.com with the caller's
  ANTHROPIC_API_KEY; the `anthropic` package is the `assistant` extra.

Either way the flag is the caller's consent; both packages are imported here only."""
from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)

MODELS = {"gte": "Alibaba-NLP/gte-reranker-modernbert-base", "haiku": "claude-haiku-5-5"}
INSTALL_HINT = "The tool picker needs sentence-transformers: pip install \"nagasim[picker]\""
HAIKU_TIMEOUT_S = 10.0     # a slow API must not stall find_tool long; on timeout its own order stands
QUESTION = "Which tool should the assistant call for this request?"     # the bench's wording
# What find_tool's answer says when a picker ordered it. A plain retry, no nudge: on the bench,
# telling the model what the rejected pick does hurt, and taking the next one equals the top-3.
TRIES = 3
GUIDANCE = (f"Try these in order: if a call fails because it was the wrong tool, call the next one, "
            f"at most the first {TRIES}.")


class PickerUnavailable(RuntimeError):
    pass


def document(spec: dict) -> str:
    """The text a picker reads for one tool: its name, the summary and the argument docs find_tool
    indexes (the same text the bench measured)."""
    from nagasim.toolsearch import _summary
    props = (spec.get("input_schema") or {}).get("properties") or {}
    args = "; ".join(f"{k}: {v.get('description', '')}".strip() for k, v in props.items())
    return f"{spec['name']}: {_summary(spec.get('description') or '')}" + (f" Arguments: {args}" if args else "")


def check(name: str) -> None:
    """The quick part of `load`, run before the server answers its client: an unknown name, a
    missing package or (haiku) a missing key raise PickerUnavailable. The slow part, importing
    torch or anthropic and loading the model, then runs in the background (#746)."""
    import importlib.util
    if name not in MODELS:
        raise PickerUnavailable(f"Unknown tool picker '{name}'; choose one of: {', '.join(MODELS)}.")
    if name == "gte" and importlib.util.find_spec("sentence_transformers") is None:
        raise PickerUnavailable(INSTALL_HINT)
    if name == "haiku":
        from nagasim import llm
        if importlib.util.find_spec("anthropic") is None:
            raise PickerUnavailable("The haiku tool picker needs the anthropic package: "
                                    "pip install \"nagasim[assistant]\"")
        try:
            key = llm.resolve_config(provider="anthropic", model=MODELS[name]).api_key
        except llm.LLMError as exc:     # its message never holds the key
            raise PickerUnavailable(f"The haiku tool picker: {exc}") from exc
        if not key:
            raise PickerUnavailable("The haiku tool picker needs ANTHROPIC_API_KEY in the environment.")


def load(name: str, documents):
    """A `rerank(query, candidates)` for `toolsearch.search`, built from the model `name`.
    `documents(tool_name) -> str` gives each candidate's text. Raises PickerUnavailable at once
    (at startup) when the name is unknown or the package is missing."""
    if name not in MODELS:
        raise PickerUnavailable(f"Unknown tool picker '{name}'; choose one of: {', '.join(MODELS)}.")
    if name == "haiku":
        return guarded(_haiku(MODELS[name]), documents, pool="all")
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise PickerUnavailable(INSTALL_HINT) from exc
    try:
        model = CrossEncoder(MODELS[name])
    except Exception as exc:    # no network on the first use, a broken cache: say so at startup
        raise PickerUnavailable(f"Could not load the tool picker {MODELS[name]}: {type(exc).__name__}: {exc}") from exc
    return guarded(lambda query, docs: [float(s) for s in model.predict([(query, d) for d in docs])], documents)


def _haiku(model: str):
    """`score(query, docs)` asking Claude for its three best tools in one forced tool call (the
    bench's prompt shape). Only those three names are used from the reply: they score 3, 2, 1 and
    every other tool 0, so the rest keep find_tool's order (guarded's sort is stable)."""
    from nagasim import llm
    try:
        key = llm.resolve_config(provider="anthropic", model=model).api_key
    except llm.LLMError as exc:     # its message never holds the key
        raise PickerUnavailable(f"The haiku tool picker: {exc}") from exc
    if not key:
        raise PickerUnavailable("The haiku tool picker needs ANTHROPIC_API_KEY in the environment.")
    try:
        import anthropic
    except ImportError as exc:
        raise PickerUnavailable("The haiku tool picker needs the anthropic package: "
                                "pip install \"nagasim[assistant]\"") from exc
    client = anthropic.Anthropic(api_key=key, timeout=HAIKU_TIMEOUT_S, max_retries=0)

    def score(query: str, docs: list[str]) -> list[float]:
        names = [d.split(": ", 1)[0] for d in docs]     # document() starts with the tool's name
        # Sorted, so the system block and the enum are the same for every query: the cache prefix holds.
        listing = "\n".join(f"- {d}" for d in sorted(docs))
        choose = {"name": "choose", "description": "Give the best options, best first.",
                  "input_schema": {"type": "object", "required": ["ranking"], "properties": {
                      "ranking": {"type": "array", "minItems": 1, "maxItems": TRIES,
                                  "items": {"type": "string", "enum": sorted(names)},
                                  "description": "Up to three option names, best first."}}}}
        reply = client.messages.create(
            model=model, max_tokens=200, tools=[choose], tool_choice={"type": "tool", "name": "choose"},
            system=[{"type": "text", "text": f"{QUESTION}\nOptions (name: what it is):\n{listing}",
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": query}])
        ranking = next(b.input["ranking"] for b in reply.content if b.type == "tool_use")
        picks = [n for n in ranking if isinstance(n, str) and n in names][:TRIES]
        return [float(TRIES - picks.index(n)) if n in picks else 0.0 for n in names]
    return score


def guarded(score, documents, pool: str = "top"):
    """Wrap `score(query, docs) -> [float]` as a rerank that never breaks find_tool: any failure
    while ranking logs and keeps find_tool's own order. `pool` tells toolsearch.search what to hand
    it: "top" = its ten best, "all" = every allowed tool."""
    def rerank(query: str, candidates: list[dict]) -> list[dict]:
        try:
            scores = score(query, [documents(c["name"]) for c in candidates])
            order = sorted(range(len(candidates)), key=lambda i: -scores[i])   # stable: ties keep find_tool's order
            return [candidates[i] for i in order]
        except Exception as exc:    # a picker is advisory; find_tool's own order is always a valid answer
            _LOG.warning("tool picker failed, keeping find_tool's order: %s", type(exc).__name__)
            return candidates
    rerank.pool = pool
    return rerank
