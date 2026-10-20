"""The study folder and its `study.json`: the state an AI-run simulation study keeps on
disk, so every tool call is stateless (the folder is the memory). Later steps extend
this module; `studychecks` is imported by it, never the reverse.

The checklist follows the steps of a simulation study as set out by Law (Simulation Modeling
and Analysis) and Banks et al. (Discrete-Event System Simulation): problem and goals, scope,
data, assumptions, model, verification, validation, experiments, conclusions. Titles are
paraphrased. Every verdict is computed from the study, never self-reported."""
from __future__ import annotations

import atexit
import json
import math
import os
import re
from importlib import resources
from datetime import datetime, timezone
from pathlib import Path

from nagasim import flowchart
from nagasim.modeldiff import diff
from nagasim.studychecks import goal_kpi, kpi_entry, kpi_mean, overall
from nagasim.twin import write_json, write_text

FORMAT = 1
MODES = ("one_go", "incremental")
# Caps per study size. ponytail: guessed caps, calibrate from usage data.
SIZES = {"quick": {"increments": 3, "scenarios": 2},
         "standard": {"increments": 8, "scenarios": 5},
         "deep": {"increments": 20, "scenarios": 12}}
# Replications come from a precision target, not from the size: the target (relative half-width of
# the 95% CI on every goal KPI) and the cap on replications per size. First pass: FIRST_PASS.
PRECISION = {"quick": {"relative": 0.10, "cap": 50},
             "standard": {"relative": 0.05, "cap": 200},
             "deep": {"relative": 0.02, "cap": 1000}}
FIRST_PASS = 10
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
                   '[{"path": "resources.<name>.capacity", "value": 2}]}]) and step it with study_step: the '
                   'scenarios run with every step and their goal results come back in the answer ("scenarios"). '
                   'Then record the comparison with '
                   'study_update(changes={"experiments": [{"id": "e1", "text": "...", "result": "..."}]}).',
    "conclusions": "Compare the last version's KPIs with the goals and adjust the model or the goals. "
                   "When the person is satisfied, build the deliverable with study_package.",
}
_MAP_BLOCKS = ('Build the components with study_step, then map each to the blocks that model it with '
               'study_update(changes={"target": [{"name": "...", "blocks": ["<block name>"]}]}), sending the '
               'full list (a list key is replaced whole).')
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


def version_id(study: dict, version: str | None = None) -> str:
    """The id of a recorded version (the last when `version` is None). Any other string is refused, so a
    version argument can never lead a path outside versions/."""
    ids = [v["id"] for v in study["versions"]]
    vid = version if version is not None else (ids[-1] if ids else None)
    if not isinstance(vid, str) or vid not in ids or not re.fullmatch(r"v\d{3,}", vid):
        raise StudyError("E_STUDY_NO_VERSION", f"no version {vid!r}", f"Pick an id from {ids}.")
    return vid


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


# --- one writer process per study ------------------------------------------------------------

WRITER = ".naga-writer"
_claimed: set[Path] = set()


def _alive(pid: int) -> bool:
    """Whether a process with this pid runs (a pid we cannot query but that exists counts as alive)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = k32.OpenProcess(0x1000, False, pid)    # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5         # access denied: it exists
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259           # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _release() -> None:
    for folder in _claimed:
        lock = folder / WRITER
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
                lock.unlink()
        except (OSError, ValueError):
            pass


def claim(folder: Path, **extra) -> None:
    """Make this process the study's only writer: the chat and the browser room share one process
    and one lock, but a second naga process must not interleave its saves with ours. Taken on the
    first write, kept for the life of the process, released at exit; a lock whose pid is gone is
    taken over. `extra` is stored in the lock too (`naga room` records its address there, so a second
    `naga room` on the study can print it). ponytail: read-then-write, so two processes claiming in the same instant can both
    win; an O_EXCL create closes that if it is ever seen."""
    folder = Path(folder).resolve()
    if folder in _claimed:
        return
    lock = folder / WRITER
    try:
        held = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(held.get("pid", 0)) if isinstance(held, dict) else 0
    except (OSError, ValueError, TypeError):
        pid = 0
    if pid and pid != os.getpid() and _alive(pid):
        raise StudyError("E_STUDY_BUSY", f"{folder.name} is open in naga process {pid}",
                         "Close that process, or use its room.")
    write_json(lock, {"pid": os.getpid(),
                      "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra})
    if not _claimed:
        atexit.register(_release)
    _claimed.add(folder)


# --- checklist, verdicts, sufficiency -------------------------------------------------------

def _last(study: dict) -> dict | None:
    return study["versions"][-1] if study["versions"] else None


def _all_or_some(flags: list[bool]) -> str:
    return "passed" if flags and all(flags) else "partial" if any(flags) else "not_verified"


def judge(goal: dict, mean: float | None) -> str:
    """met / not_met / not_verified for one goal against one KPI mean."""
    if mean is None:
        return "not_verified"
    ok = (goal.get("low") is None or mean >= goal["low"]) and (goal.get("high") is None or mean <= goal["high"])
    return "met" if ok else "not_met"


def goals_status(study: dict) -> list[dict]:
    """Each goal met / not_met / not_verified on the last version's KPI means. A goal without
    a KPI (text only) or without a version to look at stays not_verified."""
    last = _last(study)
    out = []
    for i, goal in enumerate(study["goals"]):
        kpi, mean = goal_kpi(goal), None
        if last is not None and kpi is not None:
            mean = (last.get("kpis") or {}).get(kpi)
        out.append({"id": goal.get("id", f"g{i + 1}"), "text": goal.get("text") or kpi or "",
                    "status": judge(goal, mean)})
    return out


def scenarios_meeting(study: dict) -> list[str]:
    """Scenarios of the last version that meet every goal that has a verdict there (at least one)."""
    last = _last(study)
    out = []
    for name, sc in ((last or {}).get("scenarios") or {}).items():
        verdicts = [v for v in (sc.get("goals") or {}).values() if v != "not_verified"]
        if verdicts and all(v == "met" for v in verdicts):
            out.append(name)
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
    if met and all(met):
        return "passed", ""
    if scen := scenarios_meeting(study):
        return "passed", "the base model misses a goal; met with: " + ", ".join(scen)
    return _all_or_some(met), "goals are not all met on the last version"


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
    missed = [f"goal {g['id']} is {g['status']}" for g in goals if g["status"] != "met"]
    # A scenario that meets every goal makes the study sufficient with that scenario, when the base
    # model misses goals and nothing else is open.
    scen = scenarios_meeting(study) if missed and not why else []
    if not scen:
        why += missed
    passed = {v.get("level") for v in study["validation"] if v.get("passed")}
    accepted = study.get("accepted")
    if not why:
        label = "sufficient for its goals" + (" with: " + ", ".join(scen) if scen else "")
    elif accepted:
        label = f"accepted with open issues: {accepted.get('reason', '')}"
    else:
        label = "not yet sufficient"
    return {"sufficient": not why, "label": label,
            "validation_level": next((lv for lv in LEVELS if lv in passed), None),
            "accepted": accepted, "why": why}


# --- precision: replications chosen from a target -----------------------------------------

def precision_target(study: dict, goal: dict, mean: float) -> float:
    """Absolute half-width wanted for the goal's KPI: the goal's own `precision`, else the study's
    (or size's) relative target on |mean|. Near zero a relative target would demand the cap, so
    a mean below 1% of the goal's bound uses 1% of the bound instead."""
    if goal.get("precision") is not None:
        return float(goal["precision"])
    rel = (study.get("precision") or {}).get("relative", PRECISION[study["size"]]["relative"])
    bounds = [abs(goal[e]) for e in ("low", "high") if goal.get(e) is not None]
    floor = 0.01 * max(bounds) if bounds else 0.0
    return floor if abs(mean) < floor else rel * abs(mean)


def precision_status(study: dict, digest: dict) -> dict:
    """{kpi: {half_width, target, met}} for the goal KPIs the digest has a CI for. Two goals on
    one KPI keep the stricter target."""
    out: dict = {}
    for goal in study["goals"]:
        kpi = goal_kpi(goal)
        e = kpi_entry(digest, kpi) if kpi else None
        h = e.get("ci_half_width") if e else None
        if h is None:
            continue
        target = precision_target(study, goal, e["mean"])
        if kpi not in out or target < out[kpi]["target"]:
            out[kpi] = {"half_width": round(h, 6), "target": round(target, 6), "met": h <= target,
                        "mean": round(e["mean"], 6)}
    return out


def replications_needed(status: dict, n1: int, cap: int) -> int:
    """Two-pass rule (Law, Simulation Modeling and Analysis, ch. 9): n >= n1 (h / target)^2 for
    the widest goal KPI, never below n1, never above the cap."""
    need = [n1] + [math.ceil(n1 * (v["half_width"] / v["target"]) ** 2) if v["target"] > 0 else cap
                   for v in status.values()]
    return min(max(need), cap)


def precision_sentence(n: int, status: dict, cap: int) -> str:
    if not status:
        return f"{n} replications (provisional: no goal KPI yet to set a precision target)."
    parts = ", ".join(f"{k} +-{v['half_width']:g} (target +-{v['target']:g}, {'met' if v['met'] else 'not met'})"
                      for k, v in status.items())
    capped = f" The cap of {cap} stopped it. {cap_hint(status)}" if n >= cap and not all(
        v["met"] for v in status.values()) else ""
    return f"{n} replications for the precision target at 95%: {parts}.{capped}"


def cap_hint(status: dict) -> str:
    """The two ways on when the replication cap stopped a step: relax the target (an assumption to
    record) or use the deep size. Only a suggestion; the person decides."""
    rels = [v["half_width"] / abs(v["mean"]) for v in status.values() if not v["met"] and v.get("mean")]
    relax = (f'relax the target to a relative half-width of {math.ceil(round(max(rels) * 100, 6)) / 100:g} and '
             f'record it as an assumption with study_update(changes={{"precision": {{"relative": '
             f'{math.ceil(round(max(rels) * 100, 6)) / 100:g}}}}}), or ') if rels else ""
    return f"Ways on: {relax}move to the deep size (a higher cap)."


# --- effort, next step, plan card -----------------------------------------------------------

def effort(study: dict) -> dict:
    """A rough estimate. Calls: 4 + 2 per increment, from the measured runs (dogfood 2026-10-19:
    10 calls for a 3-version one-go study). Minutes are still from one small task; calibrate later."""
    n = SIZES[study["size"]]["increments"]
    return {"size": study["size"], "tool_calls": 4 + 2 * n, "minutes": 1 + 1.5 * n,
            "note": "An estimate only; adjust it to your own app and model."}


def next_step(study: dict, folder: Path | None = None) -> str:
    text = _next_step(study, folder)
    last = _last(study)
    status = (last or {}).get("precision") or {}
    if last and status and not all(v["met"] for v in status.values())             and (last.get("replications") or 0) >= PRECISION[study["size"]]["cap"]:
        return f"{text} The replication cap stopped the precision target. {cap_hint(status)}"
    return text


def _next_step(study: dict, folder: Path | None = None) -> str:
    if study["mode"] == "one_go" and not study["plan_approved"]:
        if not study["goals"]:      # a plan card without goals is an empty card
            return _TODO["goals"]
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
            # Components already recorded: only their blocks are missing, do not ask for the target again.
            return _MAP_BLOCKS if c["item"] == "scope" and study["target"] else _TODO[c["item"]]
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


def error_code(problems) -> str:
    """The first error code of a draft's problems (a validation lists warnings too), for the panel
    and the usage log."""
    codes = [p.get("code", "") for p in problems or () if isinstance(p, dict)]
    return next((c for c in codes if c.startswith("E_")), codes[0] if codes else "error")


def request_key(tool: str, request_id: str | None) -> str | None:
    """Stored answers are keyed by tool, so a revert reusing a step's id is not a replay."""
    return None if request_id is None else f"{tool}:{request_id}"


def _record(folder: Path, study: dict, layout: dict, verdict: str, checks: list[dict],
            kpis: dict, note: str, request_key: str | None, problems: list[dict],
            scenarios: dict | None = None, run: dict | None = None) -> dict:
    """`run` is {"replications", "precision", "precision_note"} of the step's run, None for an old step."""
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
        "changes": lines[:_MAX_CHANGES], "problems": problems, **({"scenarios": scenarios} if scenarios else {}),
        **{k: run[k] for k in ("replications", "precision", "seed") if run and k in run},
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if keep:   # the flowchart shows this step's changes and what of the target is not built yet
        geom = flowchart.geometry(layout, changed,
                                  pending=[c for c in study["target"] if not c.get("blocks")])
        write_text(target / "flowchart.svg", flowchart.to_svg(geom, f"{vid}: {note}"))
        write_text(target / "flowchart.txt", flowchart.to_text(geom))
        study["versions"].append({"id": vid, "note": note, "verdict": verdict,
                                  "verification": overall([c for c in checks if c["kind"] not in ("goal", "precision")]),
                                  "kpis": kpis, "changes_count": len(lines),
                                  **({"scenarios": scenarios} if scenarios else {}),
                                  **{k: run[k] for k in ("replications", "precision") if run}})
    answer = {"id": vid, "kind": "version" if keep else "draft", "verdict": verdict, "checks": checks,
              "changes": lines[:_MAX_CHANGES], "kpis": kpis}
    if scenarios:
        answer["scenarios"] = scenarios
    if run:
        answer.update({k: v for k, v in run.items() if k != "seed"})   # the seed is for study_report
    if not keep:
        answer["problems"] = problems     # stored with the answer, so a re-issued call returns them
    if request_key is not None:
        study["requests"][request_key] = answer
    save(folder, study)
    return answer


def record_step(folder: Path, study: dict, layout: dict, digest: dict | None, checks: list[dict],
                note: str, request_id: str | None, problems: list[dict] | None = None,
                scenarios: dict | None = None, run: dict | None = None) -> dict:
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
        k = goal_kpi(goal)
        if digest is not None and k and k not in kpis and (m := kpi_mean(digest, k)) is not None:
            kpis[k] = m
    return _record(folder, study, layout, verdict, checks, kpis, note, key, problems or [], scenarios, run)


def revert(folder: Path, study: dict, version: str, note: str, request_id: str | None = None) -> dict:
    """Record the stored model of `version` as a new version; its history is never rewritten.
    The same `request_id` returns the first answer."""
    key = request_key("revert", request_id)
    if key is not None and key in study["requests"]:
        return study["requests"][key]
    old = Path(folder) / "versions" / version_id(study, version)
    if not (old / "step.json").is_file():
        raise StudyError("E_STUDY_NO_VERSION", f"no version {version!r}",
                         "Pick an id from the study's versions list.")
    step = json.loads((old / "step.json").read_text(encoding="utf-8"))
    layout = json.loads((old / "model.json").read_text(encoding="utf-8"))
    return _record(folder, study, layout, step["verdict"], step["checks"], step["kpis"], note, key, [],
                   step.get("scenarios"),
                   {k: step[k] for k in ("replications", "precision", "seed") if k in step} or None)
