"""Tool search: find the tools that fit a free-text intent (or a guessed tool name).

Plain and deterministic on purpose: a BM25-style overlap of stemmed, synonym-folded tokens, with the
name weighing most, then the first sentence of the description, then the rest, then argument names.
It takes the catalogue as data (a list of {name, description, input_schema}), so it knows nothing of
the registry. `rerank` is the seam for a small model to reorder the best candidates later."""
from __future__ import annotations

import math
import re
from collections import Counter

_SUFFIXES = ("ation", "ion", "ing", "ed", "ses", "xes", "ches", "shes", "s")   # "files" -> "file", not "fil"
_STOP = frozenset("a an the to of for and or in on at by me my it its is are be this that with from "
                  "i we you do does how what if all one use make get show".split())
# Synonym groups (first word is the canonical form). Engine vocabulary only; extend when an eval miss asks.
_GROUPS = ("validate check verify validation", "scenario option alternative whatif",
           "goal target objective kpi", "run simulate execute step", "report writeup document paper",
           "chart plot figure image picture flowchart diagram", "slide deck presentation", "package deliverable export",
           "sensitivity assumption robust", "revert undo rollback", "accept approve", 
           "update add set change edit record", "start begin", "load open read", "list enumerate catalogue")
# Generic ranking, tuned on EVAL and checked on the frozen HELD_OUT_2 table (tests/test_toolsearch.py): a single
# shared word in a tool NAME ("record", "files") must not beat a tool that matches more of the question, so
# the name weighs little more than the summary, the exact-name bonus is small, argument docs are indexed and
# the score is scaled by the share of query terms matched.
_WEIGHTS = {"name": 2.0, "head": 2.0, "body": 1.0, "args": 1.0}
_K, _B = 1.2, 0.5


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


_CANON = {}
for _g in _GROUPS:
    _words = _g.split()
    for _w in _words:
        _CANON[_w] = _CANON[_stem(_w)] = _words[0]


def tokens(text: str) -> list[str]:
    text = re.sub(r"what[\s-]*if", "whatif", text.lower())
    out = []
    for word in re.findall(r"[a-z0-9]+", text):
        if word not in _STOP:
            out.append(_CANON.get(word) or _CANON.get(_stem(word)) or _stem(word))
    return out


def _summary(description: str) -> str:
    first = description.strip().split("\n\n")[0].replace("\n", " ")
    return re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]


class Index:
    def __init__(self, specs: list[dict]):
        self.docs = []
        for spec in specs:
            schema = spec.get("input_schema") or {}
            desc = spec.get("description") or ""
            head = _summary(desc)
            fields = {"name": tokens(spec["name"]), "head": tokens(head),
                      "body": tokens(desc[len(head):]), "args": tokens(" ".join(f"{k} {v.get('description', '')}" for k, v in (schema.get("properties") or {}).items()))}
            weighted = Counter()
            for field, toks in fields.items():
                for t in toks:
                    weighted[t] += _WEIGHTS[field]
            self.docs.append({"name": spec["name"], "summary": head, "weighted": weighted, "name_tokens": set(fields["name"]), "size": sum(weighted.values()),
                              "args": list(schema.get("required") or [])})
        self.avg = sum(d["size"] for d in self.docs) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in d["weighted"])
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}


def index(specs: list[dict]) -> Index:
    return Index(specs)


def search(index: Index, query: str, limit: int = 5, choices: set[str] | None = None, rerank=None) -> list[dict]:
    """The best `limit` tools for `query` as [{name, score, summary, args}], best first, ties by name.
    `choices` restricts the answer to those names; `rerank(query, candidates)` may reorder the top ten,
    or every allowed tool (a tool BM25 scored 0 has score 0) when it declares `pool = "all"`."""
    everything = getattr(rerank, "pool", "top") == "all"
    query_terms = set(tokens(query))
    if not query_terms:
        return []
    squashed = re.sub(r"[^a-z0-9]", "", query.lower())
    found = []
    for d in index.docs:
        if choices is not None and d["name"] not in choices:
            continue
        norm = _K * (1 - _B + _B * d["size"] / index.avg)    # BM25 length normalisation: a long description is not a better match
        hits = [t for t in query_terms if t in d["weighted"]]
        score = sum(index.idf[t] * d["weighted"][t] / (d["weighted"][t] + norm) for t in hits)
        score *= 1 + len(hits) / len(query_terms)     # coordination: matching more of the question beats one strong word
        score += 1.0 * len(query_terms & d["name_tokens"]) / max(len(query_terms), len(d["name_tokens"]))   # a name that IS the query
        if squashed == d["name"].replace("_", ""):
            score += 100.0
        if score > 0 or everything:
            found.append({"name": d["name"], "score": round(score, 3), "summary": d["summary"], "args": d["args"]})
    found.sort(key=lambda c: (-c["score"], c["name"]))
    top = found if everything else found[:max(limit, 10)]
    if rerank is not None:
        top = list(rerank(query, top))
    return top[:limit]
