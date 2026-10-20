"""Optional tool picker: a small re-ranking model that reorders find_tool's top ten (D671).

Off unless the person starting the server asks for it (`naga mcp --tool-picker gte`); no tool
argument and no model file can turn it on. The only picker is "gte", the zero-shot cross-encoder
Alibaba-NLP/gte-reranker-modernbert-base, which beat find_tool's own order on the frozen blind set
(top-1 14 vs 8 of 25, about 0.1 s on a 4 GB laptop GPU). Its first use downloads the weights from
Hugging Face: that download is the caller's consent, given by the flag. sentence-transformers is
the `picker` extra, imported here only."""
from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)

MODELS = {"gte": "Alibaba-NLP/gte-reranker-modernbert-base"}
INSTALL_HINT = "The tool picker needs sentence-transformers: pip install \"nagasim[picker]\""
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


def load(name: str, documents):
    """A `rerank(query, candidates)` for `toolsearch.search`, built from the model `name`.
    `documents(tool_name) -> str` gives each candidate's text. Raises PickerUnavailable at once
    (at startup) when the name is unknown or the package is missing."""
    if name not in MODELS:
        raise PickerUnavailable(f"Unknown tool picker '{name}'; choose one of: {', '.join(MODELS)}.")
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise PickerUnavailable(INSTALL_HINT) from exc
    try:
        model = CrossEncoder(MODELS[name])
    except Exception as exc:    # no network on the first use, a broken cache: say so at startup
        raise PickerUnavailable(f"Could not load the tool picker {MODELS[name]}: {type(exc).__name__}: {exc}") from exc
    return guarded(lambda query, docs: [float(s) for s in model.predict([(query, d) for d in docs])], documents)


def guarded(score, documents):
    """Wrap `score(query, docs) -> [float]` as a rerank that never breaks find_tool: any failure
    while ranking logs and keeps find_tool's own order."""
    def rerank(query: str, candidates: list[dict]) -> list[dict]:
        try:
            scores = score(query, [documents(c["name"]) for c in candidates])
            order = sorted(range(len(candidates)), key=lambda i: -scores[i])   # stable: ties keep find_tool's order
            return [candidates[i] for i in order]
        except Exception as exc:    # a picker is advisory; find_tool's own order is always a valid answer
            _LOG.warning("tool picker failed, keeping find_tool's order: %s", type(exc).__name__)
            return candidates
    return rerank
