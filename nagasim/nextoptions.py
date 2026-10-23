"""What next (D727): ranked options computed by rules over recorded facts, never by a language model.

`for_study(study, folder)` reads what the study folder holds (study.json, the last version's step.json,
model.json, sensitivity.json and asked.json, the drafts, the package) and answers at most five options,
ranked fix > missing > improve > extend, then by the size of the gap (the measured spread over what
would settle the question; 1 for a rule that only sees a fact missing), then by rule order. The same
folder gives the same options. `for_model(layout, asked)` is the same for a model with no study, from
what the session asked of it (`agent_tools.Workspace.asked`).

An option is {"kind", "title", "why" (one sentence with its number), "tool", "arguments", "cost",
"source": "study numbers"} plus "needs" (the arguments the assistant still has to fill in, such as a
whole model layout) when the rule cannot fill them. With `study_id` the arguments also name the study
(or its model slot, "study:<id>") and data paths are made relative to the workspace root.
The assistant only chooses how to say them; nothing here runs a tool."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from nagasim import study as study_mod
from nagasim.studychecks import goal_kpi

KINDS = ("fix", "missing", "improve", "extend")
MAX_OPTIONS = 5
SOURCE = "study numbers"
SLOT = "study:"         # the study's model slot in a workspace (agent_tools.study._slot)
ASKED = "asked.json"    # versions/<id>/asked.json: the model tools run on that version
TABULAR = (".csv", ".tsv", ".xlsx")
# Costs the tools' own defaults give (what_if start 5 cap 50; explain_kpi 5; input_uncertainty
# (20 + 1) x 5; calibrate budget 120).
_WHAT_IF_COST = "paired runs, 5 doubling up to 50 replications"
_UNCERTAINTY_COST = "about 105 runs (21 resamples x 5 replications)"
_DIFFERENCE_COST = "about 210 runs (21 resamples x 5 replications x 2 models)"
WIDEST = 0.5            # the widest swing the narrowing rule asks a sensitivity to try (D738)
_EXPLAIN_COST = "one run of 5 replications"


def _g(x: float) -> str:
    return f"{x:.3g}"


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def option(kind: str, title: str, why: str, tool: str, arguments: dict | None = None,
           cost: str = "no runs", needs=(), gap: float = 1.0) -> dict:
    out = {"kind": kind, "title": title, "why": why, "tool": tool, "arguments": dict(arguments or {}),
           "cost": cost, "source": SOURCE, "_gap": float(gap)}
    if needs:
        out["needs"] = list(needs)
    return out


def rank(options: list[dict], limit: int = MAX_OPTIONS) -> list[dict]:
    """Sorted fix > missing > improve > extend, bigger gap first, then rule order; `_gap` dropped."""
    order = sorted(range(len(options)),
                   key=lambda i: (KINDS.index(options[i]["kind"]), -options[i]["_gap"], i))
    return [{k: v for k, v in options[i].items() if k != "_gap"} for i in order[:limit]]


def explain_name(goal: dict) -> str | None:
    """The KPI name explain_kpi takes for a goal's KPI, or None when it does not explain it (a user
    statistic). A tail goal is explained through the wait at its resource."""
    from nagasim import explain
    kpi = goal.get("kpi")
    if not isinstance(kpi, str) or not kpi:
        return None
    if goal.get("share_above") is not None:
        return f"resource/{kpi.partition('/')[0]}/{explain.WAIT}"
    system = (explain.TIME_KPI, *explain.WIP_KPIS, explain.THROUGHPUT)
    for name in (kpi, f"system/{kpi}"):
        if name in system:
            return name
    if "/" in kpi and not kpi.startswith(("system/", "resource/")):
        return f"resource/{kpi}"
    return kpi if kpi.startswith("resource/") and kpi.count("/") >= 2 else None


def _capacity(layout: dict, name: str):
    for r in layout.get("resources") or []:
        if isinstance(r, dict) and r.get("name") == name:
            c = r.get("capacity")
            return c if isinstance(c, int) and not isinstance(c, bool) else None
    return None


def law_option(check: dict, layout: dict, *, study: bool, step_cost: str = "") -> dict:
    """One law warning as an option. Offered load on a resource: a what_if with one more unit
    (a test, nothing saved). Initial bias: the warm-up the check names. Else a model change: in a
    study a study_step, on a model the place where its time goes (explain_kpi)."""
    name, why = str(check.get("name", "")), str(check.get("why", ""))
    res = name.partition(": ")[2] if name.startswith(("offered load: ", "utilisation: ")) else ""
    cap = _capacity(layout, res) if res else None
    sentence = f"Law check {name!r} warned: {why}."
    if cap is not None:
        return option("improve", f"Test one more unit of {res}", sentence, "what_if",
                      {"overrides": [{"path": f"resources.{res}.capacity", "value": cap + 1}]},
                      _WHAT_IF_COST, gap=1.5)
    warm = re.search(r"warm-up to about ([0-9.eE+-]+)", why) if name == "initial bias" else None
    if warm and not study:
        return option("improve", f"Set a warm-up of about {warm.group(1)}", sentence,
                      "set_simulation_settings", {"warmup": float(warm.group(1))}, gap=1.5)
    if study:
        title = f"Set a warm-up of about {warm.group(1)}" if warm else f"Deal with the {name} warning"
        return option("improve", title, sentence, "study_step", {"note": title}, step_cost,
                      needs=["layout (the last version with the change)"], gap=1.5)
    from nagasim import explain
    return option("improve", f"Find where the time goes ({name})", sentence, "explain_kpi",
                  {"kpi": explain.TIME_KPI}, _EXPLAIN_COST, gap=1.5)


# ---------------------------------------------------------------- a study
def _facts(study: dict, folder: Path) -> dict:
    last = study["versions"][-1] if study["versions"] else None
    f = {"last": last, "vid": None, "step": {}, "layout": {}, "sens": None, "asked": []}
    if last is not None:
        try:        # like every reader of versions/: a hand-edited id never leads outside it
            vid = study_mod.version_id(study)
        except study_mod.StudyError:
            return {**f, "last": None}
        vdir = folder / "versions" / vid
        f.update(vid=vid, step=_read(vdir / "step.json") or {}, layout=_read(vdir / "model.json") or {},
                 sens=_read(vdir / "sensitivity.json"))
        asked = _read(vdir / ASKED)
        f["asked"] = [a for a in asked if isinstance(a, dict)] if isinstance(asked, list) else []
    return f


def _newest_draft(folder: Path, after: str | None) -> tuple[str, dict] | None:
    """The newest draft when it was stored after the last version (its step failed), else None."""
    drafts = sorted((p for p in (folder / "versions" / "drafts").glob("d[0-9]*") if p.is_dir()),
                    key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else -1)
    if not drafts:
        return None
    step = _read(drafts[-1] / "step.json") or {}
    if after is not None and str(step.get("at", "")) <= after:
        return None
    return drafts[-1].name, step


def _draft_reason(step: dict) -> str:
    for p in step.get("problems") or []:
        if isinstance(p, dict) and p.get("message"):
            return f"{p.get('code', 'error')}: {p['message']}"
    for c in step.get("checks") or []:
        if c.get("verdict") == "failed" and c.get("kind") not in ("law", "goal", "precision"):
            return f"check {c.get('name')} failed: {c.get('why')}"
    return "it did not run"


_UPDATE_KEY = {"question": "question", "goals": "goals", "scope": "target", "assumptions": "assumptions",
               "validation": "validation", "experiments": "experiments"}


def _checklist_options(study: dict, folder: Path, facts: dict, step_cost: str) -> list[dict]:
    out = []
    for open_item in study_mod.open_items(study, folder):
        item = open_item["item"]
        if item == "plan_approval":
            if study["goals"]:      # without goals the goals step asks first
                out.append(option("missing", "Show the plan card and ask for approval",
                                  f"The plan ({len(study['goals'])} goal(s), {len(study['assumptions'])} "
                                  "assumption(s)) is not approved yet, and a one-go study builds only after "
                                  "approval.", "study_update", {"plan_approved": True}, gap=11))
            continue
        if item == "size_cap":
            increments = study_mod.SIZES[study["size"]]["increments"]
            bigger = {"quick": "standard", "standard": "deep"}.get(study["size"])
            out.append(option("fix", "Report, or raise the size", f"{len(study['versions'])} versions reached "
                              f"the {study['size']} size's cap of {increments}.", "study_update",
                              {"size": bigger} if bigger else {},
                              needs=() if bigger else ["the person's decision"]))
            continue
        i, c = open_item["index"], open_item["check"]
        why = f"Checklist step {i} of {len(study_mod.CHECKLIST)} ({c['title'].lower()}) is {c['verdict']}" + (
            f": {c['why']}." if c["why"] else ".")
        kind = "fix" if c["verdict"] == "failed" else "missing"
        gap = 11 - i
        if item in ("model", "verification", "conclusions"):
            out.append(option(kind, c["title"] if item != "conclusions" else "Change the model or the goals",
                              why, "study_step", {}, step_cost, needs=["layout", "note"], gap=gap))
        elif item == "inputs":
            out.append(option(kind, "Store and name the input files", why, "study_add_input", {},
                              needs=["name", "path or text"], gap=gap))
        elif item == "validation" and facts["layout"] and _queue(facts["layout"]):
            out.append(option(kind, "Check the model against the queue formula", why, "study_validate",
                              {"kind": "queue_formula"}, "10 replications at 100 times the run length",
                              gap=gap))
        else:
            key = _UPDATE_KEY.get(item, item)
            out.append(option(kind, c["title"], why, "study_update", {}, needs=[key], gap=gap))
    return out


def _queue(layout: dict) -> bool:
    from nagasim.studyformulas import queue_formula
    try:
        return queue_formula(layout) is not None
    except Exception:       # a hand-edited model the formula cannot read: no offer
        return False


def _precision_options(study: dict, facts: dict) -> list[dict]:
    last, out, crossing = facts["last"], [], set()
    status = last.get("precision") or {}
    n = int(last.get("replications") or study_mod.FIRST_PASS)
    cap = study_mod.PRECISION[study["size"]]["cap"]
    for gi, goal in enumerate(study["goals"]):
        kpi, st = goal_kpi(goal), status.get(goal_kpi(goal) or "")
        if not st or not st.get("half_width"):
            continue
        m, h = float(st.get("mean", 0.0)), float(st["half_width"])
        for end in ("low", "high"):
            b = goal.get(end)
            if b is None or abs(m - b) >= h:
                continue
            want = 0.9 * abs(m - b) if m != b else h / 2
            need = math.ceil(n * (h / want) ** 2)
            goals = [dict(g, precision=round(want, 6)) if j == gi else g for j, g in enumerate(study["goals"])]
            gid = goal.get("id", f"g{gi + 1}")
            out.append(option(
                "improve", f"Settle goal {gid} on {kpi}",
                f"{kpi} = {_g(m)} +-{_g(h)} spans goal {gid}'s bound {_g(b)}, so whether it is met is not "
                f"settled after {n} replications.", "study_update", {"goals": goals},
                f"the next study_step runs about {min(need, cap)} replications"
                + (f" (the cap of {cap} stops it short of {need})" if need > cap else ""),
                gap=h / max(abs(m - b), 1e-12)))
            crossing.add(kpi)
            break
    for kpi, st in status.items():
        if st.get("met") or kpi in crossing or not st.get("target"):
            continue
        h, t = float(st["half_width"]), float(st["target"])
        why = f"{kpi} is +-{_g(h)} against a precision target of +-{_g(t)} after {n} replications (cap {cap})."
        if n >= cap and study["size"] != "deep":
            out.append(option("improve", f"Narrow the interval on {kpi}", why, "study_update", {"size": "deep"},
                              f"the next study_step runs up to {study_mod.PRECISION['deep']['cap']} replications",
                              gap=h / t))
        elif n >= cap and st.get("mean"):
            rel = math.ceil(round(h / abs(float(st["mean"])) * 100, 6)) / 100
            out.append(option("improve", f"Relax the precision target on {kpi}", why, "study_update",
                              {"precision": {"relative": rel}}, gap=h / t))
        else:
            need = min(math.ceil(n * (h / t) ** 2), cap)
            out.append(option("improve", f"Narrow the interval on {kpi}", why, "study_step", {},
                              f"one study_step of about {need} replications", needs=["layout", "note"],
                              gap=h / t))
    return out


def _asked(facts: dict, tool: str) -> list[dict]:
    return [a for a in facts["asked"] if a.get("tool") == tool]


def _improve_options(study: dict, folder: Path, facts: dict, step_cost: str, data_prefix: str) -> list[dict]:
    vid, layout, out = facts["vid"], facts["layout"], []
    n = int(facts["last"].get("replications") or study_mod.FIRST_PASS)
    out += _precision_options(study, facts)
    for c in facts["step"].get("checks") or []:
        if c.get("kind") == "law" and c.get("verdict") == "partial":
            out.append(law_option(c, layout, study=True, step_cost=step_cost))
    goals = [g for g in study["goals"] if g.get("kpi")]
    if goals and facts["sens"] is None and layout:
        from nagasim import studysensitivity
        k = len(studysensitivity.factors(layout))
        if k:
            out.append(option("improve", "Find which assumptions change the answer",
                              f"No assumption has been varied on {vid}; {k} parameter(s) could move the goal KPIs.",
                              "study_sensitivity", {}, f"about {2 * k + 1} runs of {n} replications"))
    tab = [s for s in study["sources"] if isinstance(s.get("input"), str)
           and s["input"].lower().endswith(TABULAR) and (folder / s["input"]).is_file()]
    if tab and not _asked(facts, "input_uncertainty"):
        out += _uncertainty_options(tab, facts, data_prefix, n)
    out += spread_options(facts["asked"])
    levels = {v.get("level") for v in study["validation"] if v.get("passed")}
    if levels == {"known_answer"}:
        k = sum(1 for v in study["validation"] if v.get("passed"))
        out.append(option("improve", "Validate against data or an expert",
                          f"{k} validation(s) passed, all against a formula; none against data or an expert.",
                          "study_update", {}, needs=["validation"]))
    explained = {a.get("kpi") for a in _asked(facts, "explain_kpi")}
    main = next((g for g in sorted(goals, key=lambda g: study_mod.judge(g, (facts["last"].get("kpis") or {})
                                                                      .get(goal_kpi(g))) == "met")
                 if explain_name(g)), None)
    if main is not None and explain_name(main) not in explained:
        name, value = explain_name(main), (facts["last"].get("kpis") or {}).get(goal_kpi(main))
        out.append(option("improve", f"Explain what makes up {goal_kpi(main)}",
                          f"What makes up {goal_kpi(main)}"
                          + (f" ({_g(value)} on {vid})" if isinstance(value, (int, float)) else f" on {vid}")
                          + " has not been measured.", "explain_kpi", {"kpi": name}, _EXPLAIN_COST))
    tried = [a.get("overrides") for a in _asked(facts, "what_if")]
    for a in _asked(facts, "explain_kpi"):
        test = a.get("suggested_test")
        if isinstance(test, dict) and isinstance(test.get("arguments"), dict) \
                and test["arguments"].get("overrides") not in tried:
            out.append(option("improve", "Try the test explain_kpi suggested",
                              f"explain_kpi on {a.get('kpi')} suggested {test.get('text', 'a change')}; "
                              "it has not been tried.", "what_if", test["arguments"], _WHAT_IF_COST))
            tried.append(test["arguments"].get("overrides"))
    return out


def _varied_by(path, sens: dict) -> list[str]:
    """The sensitivity factors that vary input *path*: a fitted distribution's parameters. A
    schedule table, or a source with no model field, is varied by none."""
    if not isinstance(path, str) or not path.endswith(".distribution"):
        return []
    stem = path.rpartition(".")[0] + ".dist_params."
    return [r["path"] for r in sens["factors"] if isinstance(r, dict) and isinstance(r.get("path"), str)
            and r["path"].startswith(stem)]


def _uncertainty_options(tab: list[dict], facts: dict, data_prefix: str, n: int) -> list[dict]:
    """Measure the input data's uncertainty. With a sensitivity (C4, D738) only the inputs whose
    varied parameters flip a goal verdict are measured, and an input it did not vary is kept (it can
    not be ruled out); when every input it varied leaves the answer alone, the run is not offered and
    a wider swing is, the one way left to check that (one at a time misses interactions)."""
    vid, sens = facts["vid"], facts["sens"]
    sens = sens if isinstance(sens, dict) and isinstance(sens.get("factors"), list) else None
    keep, flipped, unvaried = list(tab), [], []
    if sens is not None:
        flips = {p for p in sens.get("changes_answer") or [] if isinstance(p, str)}
        keep = []
        for src in tab:
            varied = _varied_by(src.get("path"), sens)
            if not varied:
                unvaried.append(src)
            elif flips & set(varied):
                flipped.append(src)
            else:
                continue
            keep.append(src)
    if sens is not None and not keep:
        delta = float(sens.get("delta") or 0.2)
        if delta >= WIDEST:
            return []
        wider = min(round(2 * delta, 6), WIDEST)
        k = len([r for r in sens["factors"] if isinstance(r, dict)])
        return [option("extend", "Vary the assumptions wider before measuring the input data",
                       f"No input with data changes the answer on {vid} at +-{delta:.0%} one at a time (which "
                       f"misses interactions), so measuring the {len(tab)} input(s)' uncertainty is not worth its "
                       f"runs; +-{wider:.0%} checks how far that holds.", "study_sensitivity", {"delta": wider},
                       f"about {2 * k + 1} runs of {n} replications")]
    inputs = [{"path": s["path"], "data": data_prefix + s["input"]} if isinstance(s.get("path"), str)
              else {"data": data_prefix + s["input"]} for s in keep]
    missing = [f"inputs[{i}].path (the model field source {s.get('id', '?')} feeds)"
               for i, s in enumerate(keep) if not isinstance(s.get("path"), str)]
    if sens is None:
        why = (f"{len(tab)} data file(s) feed the model, and the spread that comes from fitting them "
               f"has not been measured on {vid}.")
    else:
        why = (f"{len(flipped)} of {len(tab)} data input(s) change the answer in the sensitivity on {vid} "
               f"(+-{float(sens.get('delta') or 0.2):.0%}, one at a time, which misses interactions)"
               + (f" and {len(unvaried)} were not varied by it" if unvaried else "")
               + "; only those are measured.")
    return [option("improve", "Measure how much the input data's uncertainty moves the KPIs", why,
                   "input_uncertainty", {"inputs": inputs}, _UNCERTAINTY_COST, needs=missing)]


_DECISIVE = ("higher", "lower", "better", "worse")


def spread_options(asked: list[dict]) -> list[dict]:
    """C3: a what_if verdict whose difference is smaller than the input spread recorded on the level
    (half its input interval) asks for the difference's own input interval, on the same resamples
    and seeds, unless it is already measured. One option per change, the KPI with the biggest ratio."""
    from nagasim.uncertainty import same_change
    asked = [a for a in asked or [] if isinstance(a, dict)]
    measured = [a for a in asked if a.get("tool") == "input_uncertainty" and a.get("kpis")]
    level = next((a for a in reversed(measured) if a.get("measure", "level") == "level"), None)
    if level is None:
        return []
    spread = {r["kpi"]: (r["input_interval"][1] - r["input_interval"][0]) / 2 for r in level["kpis"]
              if isinstance(r, dict) and isinstance(r.get("input_interval"), list)
              and len(r["input_interval"]) == 2 and None not in r["input_interval"]}
    out, seen = [], []
    for a in reversed([a for a in asked if a.get("tool") == "what_if"]):
        change = a.get("overrides")
        if not change or any(same_change(change, c) for c in seen):
            continue
        seen.append(change)
        if any(m.get("measure") == "difference" and same_change(m.get("overrides"), change) for m in measured):
            continue
        rows = [(spread[r["kpi"]] / abs(r["diff"]), r) for r in a.get("kpis") or []
                if isinstance(r, dict) and r.get("verdict") in _DECISIVE and r.get("kpi") in spread
                and isinstance(r.get("diff"), (int, float)) and r["diff"]
                and abs(r["diff"]) < spread[r["kpi"]]]
        if not rows:
            continue
        ratio, r = max(rows, key=lambda x: x[0])
        args = level.get("input_args")
        out.append(option(
            "improve", "Measure the change against the input data's uncertainty",
            f"what_if found {r['kpi']} {r['verdict']} by {_g(abs(r['diff']))}, smaller than the input spread of "
            f"+-{_g(spread[r['kpi']])} measured on the level over {level.get('resamples_used')} resamples; "
            "whether the verdict holds needs the difference's own interval on the same resamples.",
            "input_uncertainty", {**({"inputs": args} if args else {}), "overrides": change}, _DIFFERENCE_COST,
            needs=() if args else ["inputs"], gap=ratio))
    return out


def _neighbour(overrides: list, layout: dict) -> list | None:
    """The overrides with every whole value above 1 one lower, or None when there is none."""
    out, changed = [], False
    for o in overrides or []:
        v = o.get("value") if isinstance(o, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and v > 1:
            out.append({"path": o["path"], "value": v - 1})
            changed = True
        elif isinstance(o, dict):
            out.append(dict(o))
    return out if changed else None


def _extend_options(study: dict, folder: Path, facts: dict) -> list[dict]:
    vid, layout, out = facts["vid"], facts["layout"], []
    goals = study_mod.goals_status(study)
    meeting = study_mod.scenarios_meeting(study)
    best = None
    if meeting:
        sc = next((s for s in layout.get("scenarios") or [] if isinstance(s, dict) and s.get("name") == meeting[0]), {})
        if (near := _neighbour(sc.get("overrides"), layout)) is not None:
            best = (f"Scenario {meeting[0]} meets every goal with a verdict", near)
    elif goals and all(g["status"] == "met" for g in goals):
        caps = [{"path": f"resources.{r['name']}.capacity", "value": r["capacity"]}
                for r in layout.get("resources") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)
                and isinstance(r.get("capacity"), int) and not isinstance(r.get("capacity"), bool)]
        if (near := _neighbour(caps[:1], layout)) is not None:
            best = (f"The base model meets all {len(goals)} goal(s)", near)
    if best is not None:
        what = ", ".join(f"{o['path']} = {o['value']}" for o in best[1])
        out.append(option("extend", "Test one unit less around the best option",
                          f"{best[0]}; {what} may meet them at a lower cost.", "what_if",
                          {"overrides": best[1]}, _WHAT_IF_COST))
    if study["sources"] and not any(a.get("method") == "abc" for a in _asked(facts, "calibrate")):
        factors = next((a["factors"] for a in reversed(_asked(facts, "explain_kpi")) if a.get("factors")), None)
        out.append(option("extend", "Calibrate the uncertain parameters to the data (ABC)",
                          f"{len(study['sources'])} source(s) hold data; ABC gives each parameter a range that "
                          "reproduces it, not only one point.", "calibrate",
                          {"method": "abc", **({"factors": factors} if factors else {})},
                          "about 120 single replications (the budget)",
                          needs=["targets (the measured KPI values)"] + ([] if factors else ["factors"])))
    pkg = folder / "package" / "model.json"
    current = (folder / "versions" / vid / "model.json") if vid else None
    stale = pkg.is_file() and current is not None and current.is_file() and pkg.read_bytes() != current.read_bytes()
    if not pkg.is_file() or stale:
        out.append(option("extend", "Rebuild the deliverable" if stale else "Build the deliverable",
                          f"The package is of an older version than {vid}." if stale else
                          f"{len(study['versions'])} version(s) recorded and no deliverable built yet.",
                          "study_package", {}))
    return out


def _bind(opt: dict, study_id: str | None) -> dict:
    """The study's own argument: `study` for a study tool, the version slot as `model` for a model tool
    (every option tool that is not study_* takes a `model`; a test holds this against the registry)."""
    if study_id is not None:
        key, value = ("study", study_id) if opt["tool"].startswith("study_") else ("model", SLOT + study_id)
        opt["arguments"] = {key: value, **opt["arguments"]}
    return opt


def for_study(study: dict, folder: Path, study_id: str | None = None, limit: int = MAX_OPTIONS) -> list[dict]:
    """The study's ranked options (at most `limit`); see the module docstring."""
    folder = Path(folder)
    facts = _facts(study, folder)
    cap = study_mod.PRECISION[study["size"]]["cap"]
    step_cost = f"one study_step: {study_mod.FIRST_PASS} replications, up to {cap}"
    out = []
    draft = _newest_draft(folder, str(facts["step"].get("at", "")) if facts["last"] else None)
    if draft is not None:
        out.append(option("fix", f"Fix the model: step {draft[0]} was stored as a draft",
                          f"The last step {draft[0]} did not become a version: {_draft_reason(draft[1])}.",
                          "study_step", {}, step_cost, needs=["layout", "note"], gap=2))
    out += _checklist_options(study, folder, facts, step_cost)
    if facts["last"] is not None:
        out += _improve_options(study, folder, facts, step_cost, f"{study_id}/" if study_id else "")
        out += _extend_options(study, folder, facts)
    return [_bind(o, study_id) for o in rank(out, limit)]


# ---------------------------------------------------------------- a model with no study
def for_model(layout: dict, asked: list | None = None, model: str | None = None, limit: int = 3) -> list[dict]:
    """Model-level options: the last run's law warnings, no scenarios, the main KPI never explained,
    and starting a study. `asked` is what this session ran on the model (Workspace.asked)."""
    from nagasim import explain
    asked = [a for a in asked or [] if isinstance(a, dict)]
    out = []
    runs = [a for a in asked if a.get("tool") == "run_model"]
    for c in (runs[-1].get("warnings") or []) if runs else []:
        out.append(law_option(c, layout, study=False))
    if not any(a.get("tool") == "explain_kpi" and a.get("kpi") == explain.TIME_KPI for a in asked) \
            and layout.get("blocks"):
        out.append(option("improve", "Explain what makes up the time in system",
                          "What makes up the time in system has not been measured in this session.",
                          "explain_kpi", {"kpi": explain.TIME_KPI}, _EXPLAIN_COST))
    out += spread_options(asked)
    if not layout.get("scenarios"):
        caps = [(r["name"], r["capacity"]) for r in layout.get("resources") or []
                if isinstance(r, dict) and isinstance(r.get("name"), str)
                and isinstance(r.get("capacity"), int) and not isinstance(r.get("capacity"), bool)]
        args = {"name": f"{caps[0][0]} +1", "overrides": [{"path": f"resources.{caps[0][0]}.capacity",
                                                           "value": caps[0][1] + 1}]} if caps else {}
        out.append(option("extend", "Compare an alternative as a scenario",
                          f"The model has 0 scenarios, so no alternative has been compared"
                          + (f"; one more unit of {caps[0][0]} is a first one." if caps else "."),
                          "upsert_scenario", args, "no runs (run_scenarios runs it)",
                          needs=() if caps else ["name", "overrides"]))
    if not (model or "").startswith(SLOT):      # a study's own slot has its study
        out.append(option("extend", "Start a study around a decision",
                          f"No study is open for this model; a study checks its {len(study_mod.CHECKLIST)} "
                          "steps and says when the answer is good enough.", "study_start", {},
                          needs=["question"]))
    opts = rank(out, limit)
    if model is not None:
        for o in opts:
            if not o["tool"].startswith("study_"):
                o["arguments"] = {"model": model, **o["arguments"]}
    return opts
