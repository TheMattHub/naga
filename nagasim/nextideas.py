"""What next, layer 2 (D728): a strong hosted model adds up to three ideas of its own on top of the
rule options (`nagasim.nextoptions`, D727).

It runs only when the person's provider is hosted and its model id is on the allow-list: the engine's
`IDEAS_MODELS` (the current Claude Opus) plus ids the caller's configuration adds with the
`NAGASIM_IDEAS_MODELS` environment variable (comma-separated). A model file never extends it, and a
local model never runs it (the layer is not worth its tokens there yet). One extra call gets the
study's facts and the rule options and must answer JSON {"ideas": [{title, why, tool, arguments}]}.
Each idea is checked: its tool is one the session offers, its arguments fit the tool's schema with the
study's own argument filled in, and any overrides apply to the last version. An idea that does not
check out is dropped and counted. A kept idea is labelled source "assistant" (the rule options say
"study numbers"). Nothing here runs a tool: the person picks. A time budget and a token cap bound the
call, and a late answer is dropped."""
from __future__ import annotations

import inspect
import json
import os
import threading

from nagasim import llm

IDEAS_MODELS = frozenset({llm.DEFAULT_ANTHROPIC_MODEL})     # the current Claude Opus
ENV = "NAGASIM_IDEAS_MODELS"
SOURCE = "assistant"
MAX_IDEAS = 3
BUDGET_S = 20.0
MAX_TOKENS = 2000
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list,
          "object": dict, "null": type(None)}

_SYSTEM = """\
You add ideas to a simulation study's "what next" list. The facts and the options already offered \
(computed from the study's numbers) are in the message. Suggest at most {n} MORE things worth doing \
that the options miss; repeat none of them. Each idea is one call of a tool from this list:
{tools}
Answer with JSON only, no prose: {{"ideas": [{{"title": "...", "why": "one sentence citing a number \
from the facts", "tool": "<name>", "arguments": {{...}}}}]}}. Leave out the "study" and "model" \
arguments: they are filled in. An empty list is a fine answer."""


def allowed_models(environ=None) -> frozenset:
    env = os.environ if environ is None else environ
    extra = {m.strip() for m in (env.get(ENV) or "").split(",") if m.strip()}
    return IDEAS_MODELS | extra


def enabled(provider, environ=None) -> bool:
    """Hosted and allow-listed; off for a local provider whatever its name."""
    model = getattr(getattr(provider, "cfg", None), "model", None)
    return getattr(provider, "kind", "local") == "hosted" and model in allowed_models(environ)


def _type_ok(value, spec: dict) -> bool:
    kinds = spec.get("type")
    if kinds is None:
        return True
    kinds = [kinds] if isinstance(kinds, str) else kinds
    for k in kinds:
        t = _TYPES.get(k)
        if t is None or (isinstance(value, t) and not (isinstance(value, bool) and k in ("integer", "number"))):
            return True
    return False


def check(idea, tools: dict, study_id: str | None, layout: dict | None) -> tuple[dict | None, str]:
    """(the idea as shown, "") or (None, why it was dropped). `tools` is the session's offered tools
    as {name: input JSON schema}, from the caller: the engine does not import the tool layer."""
    from nagasim import experiments
    from nagasim.core.schema import load_layout
    if not isinstance(idea, dict) or not all(isinstance(idea.get(k), str) and idea[k].strip()
                                             for k in ("title", "why", "tool")):
        return None, "not an idea with a title, a why and a tool"
    tool, args = idea["tool"], idea.get("arguments") or {}
    if tool not in tools:
        return None, f"{tool!r} is not a tool of this session"
    if not isinstance(args, dict):
        return None, "arguments are not an object"
    schema = tools[tool]
    props = schema.get("properties", {})
    args = {k: v for k, v in args.items() if k not in ("study", "model")}
    # The study's own argument: `study`. No `model`: the session's schemas have it stripped
    # (assistant.tool_defs) and the room runs a model tool on the study's last version anyway.
    if study_id is not None and "study" in props:
        args = {"study": study_id, **args}
    unknown = [k for k in args if k not in props]
    if unknown and not schema.get("additionalProperties"):
        return None, f"unknown argument {unknown[0]}"
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return None, f"argument {missing[0]} is required"
    bad = [k for k, v in args.items() if not _type_ok(v, props.get(k) or {})]
    if bad:
        return None, f"argument {bad[0]} has the wrong type"
    if "overrides" in args:
        try:
            experiments.apply_overrides(load_layout(layout or {}), list(args["overrides"] or []))
        except Exception as exc:        # a path that does not resolve, a value of the wrong kind
            return None, f"overrides do not apply: {type(exc).__name__}"
    return {"title": idea["title"].strip()[:120], "why": idea["why"].strip()[:300], "tool": tool,
            "arguments": args, "source": SOURCE}, ""


def _complete(provider, system: str, history: list):
    kwargs = {"max_tokens": MAX_TOKENS} if "max_tokens" in inspect.signature(provider.complete).parameters else {}
    return provider.complete(system, [], history, None, **kwargs)


def ideas(provider, facts: dict, options: list, tools: list[dict], *, study_id: str | None = None,
          layout: dict | None = None, budget_s: float = BUDGET_S) -> dict:
    """{"ideas": [checked ideas], "dropped": n, "note": text}. `tools` is the session's offered
    tools as {"name", "description", "input_schema"}. Never raises: a failed or late call answers
    no ideas and says why in the note."""
    schemas = {t["name"]: t["input_schema"] for t in tools}
    listing = "\n".join(f"- {t['name']}: {(t['description'].strip().splitlines() or [''])[0]}"
                        for t in sorted(tools, key=lambda t: t["name"]))
    system = _SYSTEM.format(n=MAX_IDEAS, tools=listing)
    history = [{"role": "user", "text": json.dumps({"facts": facts, "options": options}, default=str)}]
    box: dict = {}

    def work():
        try:
            box["reply"] = _complete(provider, system, history)
        except Exception as exc:
            box["error"] = type(exc).__name__
    worker = threading.Thread(target=work, name="naga-ideas", daemon=True)
    worker.start()
    worker.join(budget_s)
    if worker.is_alive():
        return {"ideas": [], "dropped": 0, "note": f"no ideas within {budget_s:g} s"}
    if "error" in box:
        return {"ideas": [], "dropped": 0, "note": f"the ideas call failed ({box['error']})"}
    text = box["reply"].text or ""
    try:
        found = json.loads(text[text.index("{"):text.rindex("}") + 1]).get("ideas")
    except (ValueError, AttributeError):
        found = None
    if not isinstance(found, list):
        return {"ideas": [], "dropped": 0, "note": "the answer was not the JSON asked for"}
    kept, dropped = [], max(0, len(found) - MAX_IDEAS)
    for idea in found[:MAX_IDEAS]:
        ok, _why = check(idea, schemas, study_id, layout)
        if ok is None:
            dropped += 1
        else:
            kept.append(ok)
    note = f"{dropped} idea(s) dropped: they did not check out against the tools or the model" if dropped else ""
    return {"ideas": kept, "dropped": dropped, "note": note}


def study_facts(study: dict) -> dict:
    """What the ideas call is told: the question, the goals with their status, the last version's
    KPIs and precision, the checklist verdicts and sufficiency. No file contents, no paths."""
    from nagasim import study as study_mod
    last = study["versions"][-1] if study["versions"] else {}
    return {"question": study["question"], "size": study["size"],
            "goals": study_mod.goals_status(study), "goal_specs": study["goals"],
            "sufficiency": study_mod.sufficiency(study)["label"],
            "checklist": {c["item"]: c["verdict"] for c in study_mod.checklist(study)},
            "last_version": {k: last.get(k) for k in ("id", "kpis", "precision", "replications", "scenarios")
                             if k in last},
            "assumptions": [{k: a.get(k) for k in ("id", "text", "value")} for a in study["assumptions"]]}
