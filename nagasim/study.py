"""The study folder and its `study.json`: the state an AI-run simulation study keeps on
disk, so every tool call is stateless (the folder is the memory). Later steps extend
this module; `studychecks` is imported by it, never the reverse.

The checklist follows the steps of a simulation study as set out by Law (Simulation Modeling
and Analysis) and Banks et al. (Discrete-Event System Simulation): problem and goals, scope,
data, assumptions, model, verification, validation, experiments, conclusions. Titles are
paraphrased. Every verdict is computed from the study, never self-reported."""
from __future__ import annotations

import json
import re
from importlib import resources
from datetime import datetime, timezone
from pathlib import Path

from nagasim import flowchart
from nagasim.modeldiff import diff
from nagasim.studychecks import kpi_mean, overall
from nagasim.twin import write_json, write_text

FORMAT = 1
MODES = ("one_go", "incremental")
# Caps per study size. ponytail: guessed caps, calibrate from usage data.
SIZES = {"quick": {"replications": 5, "increments": 3, "scenarios": 2},
         "standard": {"replications": 10, "increments": 8, "scenarios": 5},
         "deep": {"replications": 30, "increments": 20, "scenarios": 12}}
VERDICTS = ("passed", "partial", "failed", "not_verified")
LEVELS = ("data", "known_answer", "expert")
CHECKLIST = (
    ("question", "The question is stated"),
    ("goals", "Goals are measurable"),
    ("scope", "Scope and components are fixed"),
    ("inputs", "Inputs come from named data"),
    ("assumptions", "Assumptions have sources"),
    ("model", "A model version exists"),
    ("verification", "The model does what was intended"),
    ("validation", "The model matches reality"),
    ("experiments", "Experiments were run"),
    ("conclusions", "Goals are answered"),
)
# What to do for the first open item; names the tool that moves it on.
# Every study_update mention names the real call shape, changes={...} (a test holds this).
_TODO = {
    "question": 'Record the question with study_update(changes={"question": "..."}).',
    "goals": 'Record measurable goals with study_update(changes={"goals": [{"id": "g1", "text": "...", '
             '"kpi": "<KPI name>", "low": <number>, "high": <number>}]}).',
    "scope": 'Record the target and its components with study_update(changes={"target": '
             '[{"name": "...", "purpose": "...", "blocks": ["<block name>"]}]}).',
    "inputs": 'Add input files with study_add_input and name them with study_update(changes={"sources": '
              '[{"id": "s1", "text": "...", "input": "inputs/<name>"}]}).',
    "assumptions": 'Record each assumption with its source using study_update(changes={"assumptions": '
                   '[{"id": "a1", "text": "...", "source": "..."}]}).',
    "model": "Build the first model version with study_step.",
    "verification": "Fix the model with study_step until its model checks (laws, reproducibility, "
                    "assumptions) pass; unmet goals are findings, not model errors.",
    "validation": 'Compare the model with reality or a known answer, then record it with '
                  'study_update(changes={"validation": [{"level": "data|known_answer|expert", '
                  '"passed": true, "text": "what was compared"}]}).',
    "experiments": 'Put the alternatives in the model as scenarios ("scenarios": [{"name": "...", "overrides": '
                   '[{"path": "resources.<name>.capacity", "value": 2}]}]) and step it with study_step; '
                   'compare them with run_scenarios(model="study:<study id>"), then record the result with '
                   'study_update(changes={"experiments": [{"id": "e1", "text": "...", "result": "..."}]}).',
    "conclusions": "Compare the last version's KPIs with the goals and adjust the model or the goals. "
                   "When the person is satisfied, build the deliverable with study_package.",
}
_SUBFOLDERS = ("inputs", "versions", "versions/drafts", "tests", "outputs")

# Required keys and their types; the rest are optional and filled by `load`.
_REQUIRED = {"format": int, "question": str, "mode": str, "size": str}
_LISTS = ("goals", "target", "assumptions", "sources", "validation", "experiments",
          "versions", "usage")

_AGENTS_MD = """\
This folder is a simulation study run by the Naga engine; the engine tests every model step.
Call the study_status tool with this folder first and follow its next_step.
The model versions are in versions/ (work in progress in versions/drafts/).
The study guide: the `run_simulation_study` prompt of the Naga MCP server, or `naga skill` for the Agent Skills folder.
Never edit study.json by hand: the engine writes it, and a hand edit can break the checks.
Inputs live in inputs/, test results in tests/, reports and tables in outputs/.
"""


def skill_dir():
    """The `simulation-study` Agent Skills folder shipped in the package. Read through
    importlib.resources so it works from a zip or wheel, not only from a source tree."""
    return resources.files("nagasim") / "skills" / "simulation-study"


def skill_text(name: str = "SKILL.md") -> str:
    """One file of the skill; `name` may be a path below the folder, e.g. references/interview.md."""
    node = skill_dir()
    for part in name.split("/"):
        node = node / part
    return node.read_text(encoding="utf-8")



class StudyError(ValueError):
    """A refused study operation: a stable `code`, and a `hint` for what to do next."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _check_choice(name: str, value: str, allowed: tuple) -> None:
    if value not in allowed:
        raise StudyError("E_STUDY_BAD_VALUE", f"{name} must be one of {list(allowed)}, not {value!r}",
                         f"Use one of {', '.join(allowed)}.")


def create(folder: Path, question: str, mode: str = "one_go", size: str = "standard",
           save_now: bool = True) -> dict:
    """Make the study folder with its sub-folders, `study.json` and `AGENTS.md`."""
    folder = Path(folder)
    _check_choice("mode", mode, MODES)
    _check_choice("size", size, tuple(SIZES))
    if (folder / "study.json").exists():
        raise StudyError("E_STUDY_EXISTS", f"{folder.name} already holds a study",
                         "Open it with study_status, or pick another folder.")
    for sub in _SUBFOLDERS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    study = {"format": FORMAT, "question": question, "decision": "", "mode": mode, "size": size,
             "plan_approved": False, "accepted": None, "requests": {}}
    study.update({key: [] for key in _LISTS})
    write_text(folder / "AGENTS.md", _AGENTS_MD)
    if save_now:     # study_start logs its usage row first, so it saves once
        save(folder, study)
    return study


def load(folder: Path) -> dict:
    """The study, with missing optional keys defaulted so an older file stays readable."""
    path = Path(folder) / "study.json"
    if not path.is_file():
        raise StudyError("E_STUDY_NOT_FOUND", f"no study.json in {Path(folder).name}",
                         "Create the study first.")
    try:
        study = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StudyError("E_STUDY_UNREADABLE", f"study.json cannot be read: {type(exc).__name__}",
                         "The file is left as it is; restore it from a backup or version control.") from None
    _restore = "The file is left as it is; restore it from a backup or version control."
    if not isinstance(study, dict):
        raise StudyError("E_STUDY_UNREADABLE", "study.json is not an object", _restore)
    for key, kind in _REQUIRED.items():
        if not isinstance(study.get(key), kind) or isinstance(study.get(key), bool):
            raise StudyError("E_STUDY_UNREADABLE", f"study.json key {key!r} is missing or the wrong type",
                             _restore)
    for key, allowed in (("mode", MODES), ("size", SIZES)):
        if study[key] not in allowed:
            raise StudyError("E_STUDY_UNREADABLE", f"study.json {key} {study[key]!r} is not one of {list(allowed)}",
                             _restore)
    if study["format"] < 1:
        raise StudyError("E_STUDY_UNREADABLE", f"study.json format {study['format']} is invalid", _restore)
    if study["format"] > FORMAT:
        raise StudyError("E_STUDY_FORMAT", f"study.json is format {study['format']}, this engine reads {FORMAT}",
                         "Update the engine.")
    study.setdefault("decision", "")
    study.setdefault("plan_approved", False)
    study.setdefault("accepted", None)
    study.setdefault("requests", {})
    for key in _LISTS:
        study.setdefault(key, [])
    return study


def save(folder: Path, study: dict) -> None:
    """Atomic: the old file stays if the write fails (OSError)."""
    write_json(Path(folder) / "study.json", study)


# --- checklist, verdicts, sufficiency -------------------------------------------------------

def _last(study: dict) -> dict | None:
    return study["versions"][-1] if study["versions"] else None


def _all_or_some(flags: list[bool]) -> str:
    return "passed" if flags and all(flags) else "partial" if any(flags) else "not_verified"


def goals_status(study: dict) -> list[dict]:
    """Each goal met / not_met / not_verified on the last version's KPI means. A goal without
    a KPI (text only) or without a version to look at stays not_verified."""
    last = _last(study)
    out = []
    for i, goal in enumerate(study["goals"]):
        kpi, mean = goal.get("kpi"), None
        if last is not None and kpi is not None:
            mean = (last.get("kpis") or {}).get(kpi)
        if mean is None:
            status = "not_verified"
        else:
            ok = ((goal.get("low") is None or mean >= goal["low"])
                  and (goal.get("high") is None or mean <= goal["high"]))
            status = "met" if ok else "not_met"
        out.append({"id": goal.get("id", f"g{i + 1}"), "text": goal.get("text") or kpi or "", "status": status})
    return out


def _item(study: dict, item: str, folder: Path | None) -> tuple[str, str]:
    last = _last(study)
    if item == "question":
        return ("passed", "") if study["question"].strip() else ("not_verified", "no question")
    if item == "goals":
        goals = study["goals"]
        if not goals:
            return "not_verified", "no goals"
        measurable = [g.get("kpi") is not None and (g.get("low") is not None or g.get("high") is not None)
                      for g in goals]
        return ("passed", "") if all(measurable) else ("partial", "some goals are text only")
    if item == "scope":
        if not study["target"]:
            return "not_verified", "no target components"
        flags = [bool(c.get("blocks")) for c in study["target"]]
        missing = [str(c.get("name", "?")) for c, f in zip(study["target"], flags) if not f]
        return _all_or_some(flags), "no blocks for: " + ", ".join(missing) if missing else ""
    if item == "inputs":
        if folder is None:
            return "not_verified", "folder not given"
        if not study["sources"]:
            return "not_verified", "no sources named"
        found = [bool(s.get("input")) and (Path(folder) / s["input"]).is_file()
                 for s in study["sources"]]
        return _all_or_some(found), "" if all(found) else "a named input file is missing"
    if item == "assumptions":
        if not study["assumptions"]:
            return "not_verified", "no assumptions recorded"
        flags = [bool(a.get("source")) for a in study["assumptions"]]
        n = flags.count(False)
        return _all_or_some(flags), f"{n} assumption(s) without a source" if n else ""
    if item == "model":
        return ("passed", "") if last else ("not_verified", "no model version yet")
    if item == "verification":
        if last is None:
            return "not_verified", "no model version yet"
        # Judged without the goal checks: an unmet goal is a finding, not a broken model.
        # An older record without "verification" falls back to the step's verdict.
        verdict = last.get("verification", last.get("verdict"))
        return (verdict if verdict in VERDICTS else "not_verified"), ""
    if item == "validation":
        # Validation is study-level and holds across versions: it compares the model's logic with
        # reality or a known answer, and a later step (a scenario, another capacity) seldom undoes
        # that. Each step re-runs the laws and assumption checks; re-validate when a step changes
        # the logic itself.
        results = [v.get("passed") for v in study["validation"]]
        if True in results:
            return "passed", ""
        return ("failed", "a validation check failed") if False in results else ("not_verified", "no validation yet")
    if item == "experiments":
        return ("passed", "") if study["experiments"] else ("not_verified", "no experiments yet")
    met = [g["status"] == "met" for g in goals_status(study)]  # conclusions
    return _all_or_some(met), "" if met and all(met) else "goals are not all met on the last version"


def checklist(study: dict, folder: Path | None = None) -> list[dict]:
    """The ten steps with a verdict and a reason each. `folder` lets the inputs step see whether
    the named files exist; without it that step is not_verified."""
    skipped = study.get("skipped", [])
    out = []
    for item, title in CHECKLIST:
        if item in skipped:
            verdict, why = "not_verified", f"skipped at size {study['size']}"
        else:
            verdict, why = _item(study, item, folder)
        out.append({"item": item, "title": title, "verdict": verdict, "why": why})
    return out


def sufficiency(study: dict) -> dict:
    """Sufficient iff verification passed, validation passed and every goal is met. A person's
    acceptance never makes this true; it only changes the label."""
    verdicts = {c["item"]: c["verdict"] for c in checklist(study)}
    goals = goals_status(study)
    why = []
    if verdicts["verification"] != "passed":
        why.append(f"verification is {verdicts['verification']}")
    if verdicts["validation"] != "passed":
        why.append(f"validation is {verdicts['validation']}")
    if not goals:
        why.append("no goals are recorded")
    why += [f"goal {g['id']} is {g['status']}" for g in goals if g["status"] != "met"]
    passed = {v.get("level") for v in study["validation"] if v.get("passed")}
    accepted = study.get("accepted")
    if not why:
        label = "sufficient for its goals"
    elif accepted:
        label = f"accepted with open issues: {accepted.get('reason', '')}"
    else:
        label = "not yet sufficient"
    return {"sufficient": not why, "label": label,
            "validation_level": next((lv for lv in LEVELS if lv in passed), None),
            "accepted": accepted, "why": why}


# --- effort, next step, plan card -----------------------------------------------------------

def effort(study: dict) -> dict:
    """A rough estimate. ponytail: from one measured small task (6 replies, 0.8 min); calibrate later."""
    n = SIZES[study["size"]]["increments"]
    return {"size": study["size"], "tool_calls": 6 + 4 * n, "minutes": 1 + 1.5 * n,
            "note": "An estimate only; adjust it to your own app and model."}


def next_step(study: dict, folder: Path | None = None) -> str:
    if study["mode"] == "one_go" and not study["plan_approved"]:
        return ("Show the person the plan card (goals, scope, assumptions, effort) and record their "
                "approval with study_update(changes={\"plan_approved\": true}).")
    if sufficiency(study)["sufficient"]:
        return "Report the result; offer extensions."
    if len(study["versions"]) >= SIZES[study["size"]]["increments"]:
        return "The size cap is reached: report, or ask the person to raise the size."
    skipped = study.get("skipped", [])
    for c in checklist(study, folder):
        # Data is optional: a study with no sources is not held up on inputs.
        if c["item"] == "inputs" and not study["sources"]:
            continue
        if c["verdict"] != "passed" and c["item"] not in skipped:
            return _TODO[c["item"]]
    return "Report the result; offer extensions."


def plan_card(study: dict) -> dict:
    return {"question": study["question"], "goals": study["goals"], "scope": study["target"],
            "assumptions": study["assumptions"], "effort": effort(study), "mode": study["mode"]}


# --- recording a step -----------------------------------------------------------------------

_MAX_CHANGES = 30


def _next_id(parent: Path, prefix: str) -> str:
    """Highest existing number + 1, so a removed folder never frees an id for reuse."""
    nums = [int(p.name[1:]) for p in parent.glob(f"{prefix}[0-9]*") if p.is_dir() and p.name[1:].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:03d}"


def request_key(tool: str, request_id: str | None) -> str | None:
    """Stored answers are keyed by tool, so a revert reusing a step's id is not a replay."""
    return None if request_id is None else f"{tool}:{request_id}"


def _record(folder: Path, study: dict, layout: dict, verdict: str, checks: list[dict],
            kpis: dict, note: str, request_key: str | None, problems: list[dict]) -> dict:
    folder = Path(folder)
    keep = verdict in ("passed", "partial")
    last = _last(study)
    before = {}
    if last is not None:  # changes are against the last VERSION, never a draft
        before = json.loads((folder / "versions" / last["id"] / "model.json").read_text(encoding="utf-8"))
    lines = [f"{c.kind} {c.path}" for c in diff(before, layout, include_moved=False)] if last else []
    vid = _next_id(folder / "versions", "v") if keep else _next_id(folder / "versions" / "drafts", "d")
    target = folder / "versions" / vid if keep else folder / "versions" / "drafts" / vid
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "model.json", layout)
    changed = flowchart.changed_blocks(lines)    # all of them: "changes" below is capped
    write_json(target / "step.json", {
        "id": vid, "changed": sorted(changed), "note": note, "verdict": verdict, "checks": checks, "kpis": kpis,
        "changes": lines[:_MAX_CHANGES], "problems": problems,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if keep:   # the flowchart shows this step's changes and what of the target is not built yet
        geom = flowchart.geometry(layout, changed,
                                  pending=[c for c in study["target"] if not c.get("blocks")])
        write_text(target / "flowchart.svg", flowchart.to_svg(geom, f"{vid}: {note}"))
        write_text(target / "flowchart.txt", flowchart.to_text(geom))
        study["versions"].append({"id": vid, "note": note, "verdict": verdict,
                                  "verification": overall([c for c in checks if c["kind"] != "goal"]),
                                  "kpis": kpis, "changes_count": len(lines)})
    answer = {"id": vid, "kind": "version" if keep else "draft", "verdict": verdict, "checks": checks,
              "changes": lines[:_MAX_CHANGES], "kpis": kpis}
    if not keep:
        answer["problems"] = problems     # stored with the answer, so a re-issued call returns them
    if request_key is not None:
        study["requests"][request_key] = answer
    save(folder, study)
    return answer


def record_step(folder: Path, study: dict, layout: dict, digest: dict | None, checks: list[dict],
                note: str, request_id: str | None, problems: list[dict] | None = None) -> dict:
    """Store a step as a version (passed/partial) or a draft (failed). The same `request_id`
    returns the first answer, so a retried call does not record twice."""
    key = request_key("step", request_id)
    if key is not None and key in study["requests"]:
        return study["requests"][key]
    verdict = "failed" if digest is None else overall(checks)
    kpis = {k: v["mean"] for k, v in ((digest or {}).get("system") or {}).items()
            if isinstance(v, dict) and v.get("mean") is not None}
    # A goal on a user statistic or a resource column is stored too, with the value its goal check
    # used, so goals_status (the cover, the checklist) agrees with the step's goal checks.
    for goal in study["goals"]:
        k = goal.get("kpi")
        if digest is not None and k and k not in kpis and (m := kpi_mean(digest, k)) is not None:
            kpis[k] = m
    return _record(folder, study, layout, verdict, checks, kpis, note, key, problems or [])


def revert(folder: Path, study: dict, version_id: str, note: str, request_id: str | None = None) -> dict:
    """Record the stored model of `version_id` as a new version; its history is never rewritten.
    The same `request_id` returns the first answer."""
    key = request_key("revert", request_id)
    if key is not None and key in study["requests"]:
        return study["requests"][key]
    old = Path(folder) / "versions" / version_id
    # The id must look like one: a hand-edited study.json must not lead outside versions/.
    if (not re.fullmatch(r"v\d{3,}", str(version_id)) or not any(v["id"] == version_id for v in study["versions"])
            or not (old / "step.json").is_file()):
        raise StudyError("E_STUDY_NO_VERSION", f"no version {version_id!r}",
                         "Pick an id from the study's versions list.")
    step = json.loads((old / "step.json").read_text(encoding="utf-8"))
    layout = json.loads((old / "model.json").read_text(encoding="utf-8"))
    return _record(folder, study, layout, step["verdict"], step["checks"], step["kpis"], note, key, [])
