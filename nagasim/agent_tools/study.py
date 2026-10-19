"""Study tools: the engine owns a study folder and tests every model step, so any assistant
can run a simulation study. The folder is the memory (stateless calls); `nagasim.study` and
`nagasim.studychecks` hold the logic, these tools only resolve paths, call `write_model` /
`run_model` / `export` through the registry and map `StudyError` to `ToolError`."""
from __future__ import annotations

import base64
import functools
import logging
import json
import os
import re
import shutil
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nagasim import api, experiments, flowchart, studyinput, studypackage, studypage, studyroom
from nagasim.core.stats import ci95
from nagasim import study as study_mod
from nagasim.studychecks import goal_kpi, kpi_entry, kpi_mean, step_checks
from nagasim.studyformulas import queue_formula, wait_shares, why_not
from nagasim.study import CHECKLIST, FIRST_PASS, LEVELS, MODES, PRECISION, SIZES, StudyError
from nagasim.twin import write_text

from .core import ToolError, _digest, _document_args, call, tool, workspace

_ID = "Study folder relative to the workspace root (the id study_start returned)."
_CH = "Keys to record in the same call, with the shapes of study_update's changes."
_document_args("study_start", question="The decision or question the study answers, in the person's words.",
               study="Folder for the study; omit to use naga-studies/<short name of the question>.",
               mode="one_go (plan approved first, then built) or incremental (built step by step).",
               size="quick, standard or deep: caps on replications, steps and scenarios.", changes=_CH)
_document_args("study_status", study=_ID)
_document_args("study_validate", study=_ID,
               kind="The known answer to check against; only queue_formula (M/M/c or M/G/1) exists.")
_document_args("study_update", study=_ID,
               changes="Optional if the keys are sent as parameters. Keys to set: question, decision, mode, size, plan_approved, goals, target, "
                       "assumptions, sources, validation, experiments, skipped. A list key is "
                       "replaced whole. goals and assumptions entries need a string id; a goal is "
                       "{id, text, kpi, low, high}; an assumption is {id, text, value, source}; a "
                       "target component is {name, purpose, blocks: [block names]}; a source is "
                       "{id, text, input}; a validation entry is {level: data|known_answer|expert, "
                       "passed: true|false, text}, e.g. {\"level\": \"known_answer\", \"passed\": true, "
                       "\"text\": \"M/G/1 formula vs model wait\"}; an experiment is {id, text, result}.")
_document_args("study_update", **{k: f"Same as the \"{k}\" key of changes." for k in (
    "question", "mode", "size", "plan_approved", "precision", "goals", "target", "assumptions",
    "sources", "validation", "experiments", "skipped")})
_document_args("study_add_input", study=_ID, name="File name to store under inputs/ (no folders).",
               path="Workspace file to copy in; give this or text.",
               text="Text to store (a transcript, notes); give this or path.")
_document_args("study_step", study=_ID, layout="The complete model file as one JSON object (as write_model).",
               note="One line: what changed in this step and why.",
               request_id="Any unique string; a retried call with the same one is not recorded twice.",
               seed="First replication's seed.", changes=_CH + " Recorded before the step.")
_document_args("study_view", study=_ID, version="Version id, e.g. v002; omit for the last.",
               format="png (image), svg or text.")
_document_args("study_report", study=_ID, version="Version id, e.g. v002; omit for the last.")
_document_args("study_package", study=_ID)
_document_args("study_show", study=_ID)
_document_args("study_page_html", study=_ID)
_document_args("study_accept", study=_ID, reason="The person's own words accepting the study.")
_document_args("study_revert", study=_ID, version="Id of an earlier version, e.g. v002.",
               note="Why the study goes back to it.", request_id="Any unique string, as for study_step.")


@contextmanager
def _study_errors():
    try:
        yield
    except StudyError as exc:
        raise ToolError(exc.code, str(exc), exc.hint or None) from None


def _bad(key: str, why: str) -> ToolError:
    return ToolError("E_STUDY_BAD_VALUE", f"{key}: {why}", f"fix {key} and send it again")


def _folder(study) -> Path:
    folder = workspace().resolve(study)
    if folder == workspace().root:
        raise _bad("study", "must be a folder inside the workspace, not the workspace root")
    return folder


def _open(study: str, write: bool = False) -> tuple[Path, dict]:
    """The folder and its study; `write` claims the study for this process first (E_STUDY_BUSY when
    another naga process holds it), so a refused call changes nothing."""
    folder = _folder(study)
    with _study_errors():
        s = study_mod.load(folder)
        if write:
            study_mod.claim(folder)
        return folder, s


def _sid(folder: Path) -> str:
    return folder.relative_to(workspace().root).as_posix()


def _linked(fn):
    """Every study answer carries the control room: `page_url` (the file page, once written) and
    `room_url` while a live room runs. Under @tool, so a refused call (a ToolError) carries neither."""
    @functools.wraps(fn)
    def run(**kwargs):
        out = fn(**kwargs)
        if not isinstance(out, dict):
            return out
        sid = out["study"] if isinstance(out.get("study"), str) else kwargs.get("study")
        if isinstance(sid, str):
            page = _folder(sid) / "control-room.html"
            if page.is_file():
                out["page_url"] = page.as_uri()
            if room := studyroom.url_for(sid):
                out["room_url"] = room
        return out
    return run


def _log(study: dict, name: str, started: float) -> None:
    study["usage"].append({"tool": name, "seconds": round(time.monotonic() - started, 3),
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def _save(folder: Path, study: dict) -> None:
    study_mod.save(folder, study)       # OSError is mapped by @tool


def _label(study: dict) -> str:
    return study_mod.sufficiency(study)["label"]


def _slot(folder: Path) -> str:
    # Reserved name: a person's open model is never overwritten by a study step.
    return f"study:{_sid(folder)}"


def _page(folder: Path, study: dict) -> str | None:
    """Refresh the control room page after a recorded version; a failed write must not lose the
    version, so it answers None and the step still succeeds."""
    try:
        write_text(folder / "control-room.html",
                   # the file may be shared: the room's address without its token
                   studypage.render(folder, study, room_url=(studyroom.url_for(_sid(folder)) or "").split("?")[0] or None))
    except Exception:   # a hand-edited record that does not render must not fail a recorded step
        logging.getLogger(__name__).warning("control room page not written", exc_info=True)
        return None
    return f"{_sid(folder)}/control-room.html"


def _tails(s: dict, digest: dict, results, layout: dict, where: str = "") -> list[str]:
    """Add each tail goal's share (over replications, with its 95% CI) to `digest["tails"]` under the
    name `goal_kpi` gives; answers the reasons for the goals whose waits cannot be rebuilt from the
    run's resource log (they stay not_verified)."""
    why = []
    for g in s["goals"]:
        x = g.get("share_above")
        if x is None or not g.get("kpi"):
            continue
        res = g["kpi"].partition("/")[0]
        shares = wait_shares(results, res, x, layout)
        if shares is None:
            why.append(f"goal {g.get('id')}{where}: the waits at {res} cannot be rebuilt from the run's "
                       "resource log (absent, truncated or not FIFO), so the share over "
                       f"{x:g} is not verified")
            continue
        stat = ci95(shares)
        half = float(stat["ub"] - stat["mean"])
        entry = {"mean": float(stat["mean"]), "ci_half_width": half if half == half else None}
        digest.setdefault("tails", {})[goal_kpi(g)] = entry
    return why


def _run_scenarios(layout: dict, s: dict, n: int, seed: int) -> tuple[dict | None, dict]:
    """Run the layout's scenarios (not the baseline: the step just ran it) with the step's
    replications and seed, from the layout itself so a fresh process needs no model slot.
    Answers ({name: {"kpis": {goal kpi: {mean, ci_half_width}}, "goals": {goal id: verdict}}} or
    None, extra answer keys). A failing scenario run is reported, never fails the step."""
    names = [sc.get("name") for sc in layout.get("scenarios") or [] if isinstance(sc, dict) and sc.get("name")]
    extra: dict = {}
    cap = SIZES[s["size"]]["scenarios"]
    if len(names) > cap:
        extra["scenarios_skipped"] = (f"size {s['size']} runs at most {cap} scenarios; not run: "
                                      + ", ".join(names[cap:]))
        names = names[:cap]
    if not names:
        return None, extra
    try:
        run = experiments.run_scenarios(layout, names, replications=n, seed=seed, baseline=False)
    except Exception as exc:    # an override that does not apply, a run error: the base step stands
        logging.getLogger(__name__).warning("study scenarios failed", exc_info=True)
        extra["scenario_error"] = f"{type(exc).__name__}: {exc}"
        return None, extra
    out, why = {}, []
    for name, results in run.results.items():
        digest = _digest(results)
        why += _tails(s, digest, results, layout, f" in scenario {name}")
        kpis, verdicts = {}, {}
        for i, g in enumerate(s["goals"]):
            e = kpi_entry(digest, goal_kpi(g)) if g.get("kpi") else None
            if e is not None:
                kpis[goal_kpi(g)] = {k: e[k] for k in ("mean", "ci_half_width") if e.get(k) is not None}
            verdicts[g.get("id", f"g{i + 1}")] = study_mod.judge(g, e["mean"] if e else None)
        out[name] = {"kpis": kpis, "goals": verdicts}
    if not run.completed:
        extra["scenario_error"] = "the scenario run did not finish"
    if why:
        extra["tail_note"] = "; ".join(why)
    return out or None, extra


def _validate_call(folder: Path, s: dict) -> str | None:
    """The ready-to-send study_validate call when the validation item is open and the last version is
    a queue the formula covers; None otherwise."""
    if not s["versions"] or any(v.get("passed") for v in s["validation"]):
        return None
    layout = _read_layout(folder, s["versions"][-1]["id"])
    return f'study_validate(study="{_sid(folder)}")' if queue_formula(layout) else None


def _read_layout(folder: Path, version: str) -> dict:
    return json.loads((folder / "versions" / version / "model.json").read_text(encoding="utf-8"))


def _next(folder: Path, s: dict) -> str:
    """The next step with the study id filled in, so the assistant can send the call as written."""
    text, sid = study_mod.next_step(s, folder), _sid(folder)
    text = text.replace("study_update(changes=", f'study_update(study="{sid}", changes=')
    offer = _validate_call(folder, s)
    if offer and text == study_mod._TODO["validation"]:
        text = f"Check the model against the textbook queue formula with {offer}, or record another validation."
    if text == study_mod._TODO["model"]:
        text = (f'Build the first model version with study_step(study="{sid}", layout=<the whole model file>, '
                'note="first model").')
    return text


def _merge(changes, top: dict) -> dict:
    """`changes` plus the record keys sent as top-level parameters; a key sent both ways is refused."""
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise _bad("changes", "must be an object with at least one key")
    top = {k: v for k, v in top.items() if v is not None}
    for k, v in top.items():
        if k in changes:
            raise ToolError("E_STUDY_BAD_VALUE", f"{k}: given twice: {v!r} as a parameter and {changes[k]!r} in changes",
                            f"send {k} once, either as a parameter or inside changes")
    return {**changes, **top}


def _check(changes: dict) -> tuple[dict, list[str]]:
    """Normalise and check record changes: (canonical changes, notes of what was mapped)."""
    if not changes:
        raise _bad("changes", "must be an object with at least one key")
    with _study_errors():
        changes, notes = studyinput.normalise(changes)
    unknown = [k for k in changes if k not in _SETTABLE]
    if unknown:
        raise _bad(unknown[0], f"cannot be set; settable keys: {', '.join(_SETTABLE)}")
    checked = {k: _SETTABLE[k](k, v) for k, v in changes.items()}     # all checked before any is set
    return checked, notes


def _set(s: dict, changes: dict) -> list[str]:
    checked, notes = _check(changes)    # nothing is set if any key is refused
    s.update(checked)
    return notes


def _squash(name) -> str:
    return re.sub(r"[ _-]+", "", str(name).lower())


def _map_targets(s: dict, layout: dict) -> list[str]:
    """A target component without blocks whose name matches one block name (ignoring case, spaces,
    underscores, hyphens) gets that block, so the assistant need not send a mapping call."""
    blocks = [b["name"] for b in layout.get("blocks") or [] if isinstance(b, dict) and isinstance(b.get("name"), str)]
    notes = []
    for c in s["target"]:
        if c.get("blocks") or not isinstance(c.get("name"), str):
            continue
        hit = [b for b in blocks if _squash(b) == _squash(c["name"])]
        if len(hit) == 1:
            c["blocks"] = hit
            notes.append(f"target[{c['name']}]: mapped to block {hit[0]}")
    return notes


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "study"


@tool()
@_linked
def study_start(question: str, study: str | None = None, mode: str = "one_go",
                size: str = "standard", changes: dict | None = None) -> dict:
    """Start a simulation study: make its folder and say what to do first.

    Use it for a decision question ("how many cranes do we need?"), then follow the
    next_step in every answer. The study keeps its state in the folder; pass the returned
    `study` to the other study tools.

    Send the plan card in the same call with `changes` (the same keys and shapes as
    study_update). Example: study_start(question="How many cranes do we need?",
    changes={"goals": [{"id": "g1", "text": "wait under 5", "kpi": "Avg Throughput Time", "high": 5}]}).

    Returns {"study", "next_step", "plan_card", "effort"}, plus "normalised" when a near-miss
    input was mapped.
    """
    started = time.monotonic()
    if not isinstance(question, str) or not question.strip():
        raise _bad("question", "give the question the study answers")
    ws = workspace()
    if study is None:
        base = f"naga-studies/{_slug(question)}"
        folder, n = ws.resolve(base), 1
        while (folder / "study.json").exists():     # an explicit path never gets this: it must be free
            n += 1
            folder = ws.resolve(f"{base}-{n}")
    else:
        folder = _folder(study)
    checked, notes = _check(_merge(changes, {})) if changes is not None else ({}, [])   # before the folder exists
    with _study_errors():
        s = study_mod.create(folder, question.strip(), mode, size, save_now=False)
        study_mod.claim(folder)
    s.update(checked)
    _log(s, "study_start", started)
    _save(folder, s)
    out = {"study": _sid(folder), "next_step": _next(folder, s),
           "plan_card": study_mod.plan_card(s), "effort": study_mod.effort(s)}
    return {**out, "normalised": notes} if notes else out


@tool(read_only=True)
@_linked
def study_status(study: str) -> dict:
    """Where a study stands, on one screen: sufficiency, goals, the ten-step checklist, the
    last version's KPIs with how confident they are (precision of the confidence intervals) and
    the next step. Call it first on an existing study folder.

    Returns {"study", "question", "mode", "size", "sufficiency", "why_not_sufficient", "goals",
    "checklist", "last_version", "versions", "effort", "next_step"}, plus "plan_card" until the
    plan is approved and "validate" (a ready-to-send study_validate call) while no validation has
    passed and the last version is a queue with a textbook answer.
    """
    folder, s = _open(study)
    suff = study_mod.sufficiency(s)
    out = {"study": _sid(folder), "question": s["question"], "mode": s["mode"], "size": s["size"],
           "sufficiency": suff["label"], "why_not_sufficient": suff["why"],
           "goals": study_mod.goals_status(s),
           "checklist": [{k: c[k] for k in ("item", "verdict", "why")} for c in study_mod.checklist(s, folder)],
           "last_version": s["versions"][-1] if s["versions"] else None, "versions": len(s["versions"]),
           "effort": study_mod.effort(s), "next_step": _next(folder, s)}
    if (folder / "control-room.html").is_file():
        out["page"] = f"{_sid(folder)}/control-room.html"
    if not s["plan_approved"]:
        out["plan_card"] = study_mod.plan_card(s)
    if offer := _validate_call(folder, s):
        out["validate"] = offer
    return out


def _dicts(key: str, value, *, need_id: bool = False) -> list:
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise _bad(key, "must be a list of objects")
    if need_id and not all(isinstance(v.get("id"), str) and v["id"] for v in value):
        raise _bad(key, "every entry needs a string id")
    return value


def _bounds(key: str, spec: dict) -> None:
    for end in ("low", "high"):
        v = spec.get(end)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise _bad(key, f"{end} must be a number")


def _check_goals(key, value):
    for g in _dicts(key, value, need_id=True):
        _bounds(key, g)
        x = g.get("share_above")
        if x is not None:
            if isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0:
                raise _bad(key, "share_above must be a number of at least 0 (the wait the share is counted above)")
            if not (isinstance(g.get("kpi"), str) and "/" in g["kpi"]):
                raise _bad(key, 'a goal with share_above needs kpi "<resource>/Time in Queue"')
        p = g.get("precision")
        if p is not None and (isinstance(p, bool) or not isinstance(p, (int, float)) or not p > 0):
            raise _bad(key, "precision must be a number above 0 (absolute half-width in the KPI's unit)")
    return value


def _check_precision(key, value):
    r = value.get("relative") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {"relative"} or isinstance(r, bool)
            or not isinstance(r, (int, float)) or not 0 < r < 1):
        raise _bad(key, 'must be {"relative": a number between 0 and 1}, e.g. 0.03 for +-3%')
    return value


def _check_assumptions(key, value):
    for a in _dicts(key, value, need_id=True):
        if a.get("check") is not None:
            if not isinstance(a["check"], dict):
                raise _bad(key, "check must be an object {kpi, low, high}")
            _bounds(key, a["check"])
    return value


_VALIDATION_HINT = ('each entry needs "level" (one of data, known_answer, expert) and "passed" (true or '
                    'false), e.g. {"level": "known_answer", "passed": true, "text": "M/G/1 formula vs model wait"}')


def _check_validation(key, value):
    # The checklist reads only level and passed: an entry without a boolean passed (say
    # "status": "passed") would be stored and silently ignored, so it is refused here.
    for v in _dicts(key, value):
        if v.get("level") not in LEVELS:
            raise ToolError("E_STUDY_BAD_VALUE", f"{key}: level must be one of {list(LEVELS)}", _VALIDATION_HINT)
        if not isinstance(v.get("passed"), bool):
            raise ToolError("E_STUDY_BAD_VALUE", f"{key}: passed must be true or false", _VALIDATION_HINT)
    return value


def _check_sources(key, value):
    for s in _dicts(key, value):
        inp = s.get("input")
        if inp is None:
            continue
        parts = inp.replace("\\", "/").split("/") if isinstance(inp, str) else []
        if not (parts[:1] == ["inputs"] and len(parts) > 1 and ".." not in parts and ":" not in inp
                and all(parts[1:])):
            raise _bad(key, "input must be a path inside the study like inputs/<name>")
    return value


def _check_choice(allowed):
    def check(key, value):
        if value not in allowed:
            raise _bad(key, f"must be one of {list(allowed)}")
        return value
    return check


def _check_type(kind, what):
    def check(key, value):
        if not isinstance(value, kind) or (kind is str and not value.strip()):
            raise _bad(key, f"must be {what}")
        return value
    return check


def _check_skipped(key, value):
    names = [c[0] for c in CHECKLIST]
    if not isinstance(value, list) or not all(v in names for v in value):
        raise _bad(key, f"must be a list of checklist items from {names}")
    return value


# The settable keys. "accepted" is deliberately absent: only study_accept sets it.
_SETTABLE = {
    "question": _check_type(str, "a non-empty string"), "decision": _check_type(str, "a string"),
    "mode": _check_choice(MODES), "size": _check_choice(tuple(SIZES)),
    "plan_approved": _check_type(bool, "true or false"), "precision": _check_precision,
    "goals": _check_goals, "assumptions": _check_assumptions, "validation": _check_validation,
    "target": lambda k, v: _dicts(k, v), "sources": _check_sources,
    "experiments": lambda k, v: _dicts(k, v), "skipped": _check_skipped,
}


@tool()
@_linked
def study_update(study: str, changes: dict | None = None, question: str | None = None,
                 mode: str | None = None, size: str | None = None,
                 plan_approved: bool | None = None, precision: dict | None = None,
                 goals: list | None = None, target: list | None = None, assumptions: list | None = None,
                 sources: list | None = None, validation: list | None = None,
                 experiments: list | None = None, skipped: list | None = None) -> dict:
    """Record what the study has learned or the person decided (fewer or more replications:
    set precision): set any of question,
    decision, mode, size, plan_approved, precision, goals, target, assumptions, sources, validation,
    experiments, skipped, inside `changes` or (all but decision) as parameters of the same name
    (a key sent both ways is refused; decision is changes={"decision": ...} only). A list key replaces the whole list. Nothing here can mark the study
    accepted. Near-miss shapes (a goal with "range": [0, 5], "statement" for "text", one object
    for a list) are mapped to the canonical record and listed in "normalised"; a typo'd key is
    refused with a corrected example.
    Example: study_update(study="naga-studies/cranes", validation=[{"level": "known_answer",
    "passed": true, "text": "M/G/1 formula vs model wait"}]).

    Returns {"ok", "next_step"}, plus "normalised" (a list of notes) when something was mapped.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    merged = _merge(changes, dict(question=question, mode=mode, size=size,
                                  plan_approved=plan_approved, precision=precision, goals=goals,
                                  target=target, assumptions=assumptions, sources=sources,
                                  validation=validation, experiments=experiments, skipped=skipped))
    notes = _set(s, merged)
    _log(s, "study_update", started)
    _save(folder, s)
    if s["versions"]:   # the page shows goals and assumptions too
        _page(folder, s)
    out = {"ok": True, "next_step": _next(folder, s)}
    return {**out, "normalised": notes} if notes else out


@tool()
@_linked
def study_add_input(study: str, name: str, path: str | None = None, text: str | None = None) -> dict:
    """Keep a source of the study in its inputs/ folder: copy a workspace file, or store text
    (a call transcript, notes). Then name it with study_update(changes={"sources": [{"id",
    "text", "input": <the returned path>}]}) so the inputs step can check it.

    Returns {"input": "inputs/<name>"}.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    if not isinstance(name, str) or not name or name in (".", "..") or re.search(r"[\\/:]", name):
        raise _bad("name", "must be a plain file name without folders")
    if (path is None) == (text is None):
        raise _bad("path", "give exactly one of path and text")
    target = folder / "inputs" / name
    if target.exists():
        raise ToolError("E_STUDY_INPUT_EXISTS", f"inputs/{name} already exists", "pick another name")
    if path is not None:
        source = workspace().resolve(path, must_exist=True)
        if not source.is_file():
            raise _bad("path", "must be a file")
        tmp = target.with_name(f"{name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        write_text(target, text)
    _log(s, "study_add_input", started)
    _save(folder, s)
    return {"input": f"inputs/{name}"}


@tool()
@_linked
def study_step(study: str, layout: dict | None = None, note: str | None = None, request_id: str | None = None, seed: int = 0,
               changes: dict | None = None) -> dict:
    """Test one model step: write the whole model, run it, check it, and store it as a new
    version (or as a draft when it fails). Always send the complete model file and a one-line
    note. A model that does not validate or run is stored as a draft with its problems, not
    refused: read "problems", fix the model, step again.

    The replications come from a precision target, not from the model's own
    simulation.replications: a first pass of 10, then, if a goal KPI's 95% confidence interval is
    wider than its target, one rerun (same seed) with the count the widest needs, up to the size's
    cap (quick 50, standard 200, deep 1000). The target is a relative half-width (quick 10%,
    standard 5%, deep 2%; set another with study_update precision={"relative": 0.03}) or an
    absolute one per goal ("precision": 0.5 in the KPI's unit). With no goal KPI the count is 10
    and provisional. A cap reached makes the "precision" check partial, never failed.

    The model's "scenarios" (a list of {"name", "overrides": [{"path":
    "resources.<name>.capacity", "value": 2}]}) run with the step, with the same replications
    and seed, up to the size's cap; their goal KPIs and goals met come back in "scenarios".

    `changes` (the same keys and shapes as study_update) is recorded first, in the same call, so
    a step that also records the goals or a validation needs one call, not two. Scenario
    overrides sent as {"resources.<name>.capacity": 2} are mapped to the list shape; a target
    component without blocks whose name matches a block name is mapped to it. Both are listed in
    "normalised". Example: study_step(study="naga-studies/cranes", layout={...the whole model...},
    note="add the second crane", changes={"assumptions": [{"id": "a1", "text": "...", "source": "..."}]}).

    A tail goal {"id", "text", "kpi": "<resource>/Time in Queue", "share_above": 15, "high": 0.05}
    bounds the share of entities that waited more than 15 at that resource. The engine rebuilds
    the waits from the run's resource log (FIFO resources only) and stores the share per
    replication as the KPI "<resource>/Share waiting over 15", with its confidence interval, so it is
    judged, sized (precision) and compared across scenarios like any KPI. When the waits cannot be
    rebuilt the goal stays not_verified and "tail_note" says why.

    Returns {"id", "kind": "version"|"draft", "verdict", "checks", "changes", "kpis",
    "replications", "precision": {goal KPI: {half_width, target, met}}, "precision_note" (one
    sentence), "sufficiency", "next_step"}. A version has no
    report file by default (the answer is the light output); study_report writes it on request. With scenarios: "scenarios": {name: {"kpis": {goal KPI: {mean,
    ci_half_width}}, "goals": {goal id: met|not_met|not_verified}}}, and "scenario_error" or
    "scenarios_skipped" when a run failed or the cap cut some; "normalised" when an input was mapped.
    """
    started = time.monotonic()
    if layout is None:
        raise ToolError("E_BAD_ARGUMENT", "`layout` is required: a step runs a whole model",
                        "for record-only changes use study_update(changes=...), or resend the whole layout to step")
    if note is None:
        raise ToolError("E_BAD_ARGUMENT", "`note` is required", "send a one-line note saying what the step changes")
    folder, s = _open(study, write=True)
    notes: list[str] = []
    if changes is not None:     # refused before anything runs, so a bad record never costs a step
        notes += _set(s, _merge(changes, {}))
    with _study_errors():
        layout, layout_notes = studyinput.normalise_layout(layout)
    notes += layout_notes
    key = study_mod.request_key("step", request_id)
    cached = key is not None and key in s["requests"]
    digest = rerun = scenarios = first = None
    extra: dict = {}
    tail_why: list[str] = []
    problems: list = []
    n = FIRST_PASS
    status: dict = {}
    cap = PRECISION[s["size"]]["cap"]
    if not cached:
        if isinstance(layout, dict):
            notes += _map_targets(s, layout)
        model = _slot(folder)
        written = call("write_model", model=model, layout=layout)
        if "error" in written:
            problems = [written["error"]]
        elif not written["ok"]:
            problems = written["problems"]
        else:
            digest = call("run_model", model=model, replications=n, seed=seed)
            # ponytail: same-seed rerun on the first version and every fifth halves the cost;
            # rerun every step if drift is ever seen.
            if "error" not in digest and len(s["versions"]) % 5 == 0:
                rerun = call("run_model", model=model, replications=n, seed=seed)
            for r in (digest, rerun):
                if r is not None and "error" in r:
                    problems, digest, rerun = [r["error"]], None, None
                    break
            first = digest
            if digest is not None:
                first_results = workspace().results[model]
                tail_why = _tails(s, digest, first_results, layout)
                status = study_mod.precision_status(s, digest)
                n = study_mod.replications_needed(status, FIRST_PASS, cap)
                if n > FIRST_PASS:
                    # Replication i uses seed + i, so the extra n - FIRST_PASS replications continue the
                    # first pass and the merge equals one run of n.
                    more = call("run_model", model=model, replications=n - FIRST_PASS, seed=seed + FIRST_PASS)
                    if "error" in more:
                        problems, digest, rerun = [more["error"]], None, None
                    else:
                        res = workspace().results
                        res[model] = experiments._merge(first_results, res[model])
                        digest = {**_digest(res[model]), "model": model, "frames": sorted(res[model])}
                        _tails(s, digest, res[model], layout)
                        status = study_mod.precision_status(s, digest)
            if digest is not None:
                scenarios, extra = _run_scenarios(layout, s, n, seed)
    checks = (step_checks(layout, digest, rerun, s, {"status": status, "n": n, "cap": cap}, first)
              if digest is not None else [])
    _log(s, "study_step", started)
    run = {"replications": n, "seed": seed, "precision": status, "precision_note": study_mod.precision_sentence(n, status, cap)}
    answer = dict(study_mod.record_step(folder, s, layout, digest, checks, note, request_id, problems, scenarios,
                                        run))
    if tail_why:
        extra["tail_note"] = "; ".join(tail_why + ([extra["tail_note"]] if "tail_note" in extra else []))
    answer.update({k: v for k, v in extra.items() if k not in answer})
    if answer["kind"] == "version" and not cached:
        if page := _page(folder, s):
            answer["page"] = page
    answer["sufficiency"] = _label(s)
    answer["next_step"] = _next(folder, s)
    if notes:
        answer["normalised"] = notes
    return answer


@tool()
@_linked
def study_validate(study: str, kind: str = "queue_formula") -> dict:
    """Check the last model version against a textbook answer and record the validation.

    kind="queue_formula" applies when the model is one arrival stream (exponential interarrival
    times) feeding one FIFO resource: the engine runs the model 100 times longer than its own
    run length (warm-up 10% of that, 10 replications, seed 0), compares the mean wait in queue
    with M/M/c (Erlang C) or M/G/1 (Pollaczek-Khinchine) and records a known_answer validation
    that passes within 5% relative error. Any other model answers E_STUDY_NOT_APPLICABLE with the
    reason, so the person is asked for another validation.
    Example: study_validate(study="naga-studies/cranes").

    Returns {"kind", "model", "formula", "rel_error", "passed", "next_step"}: the simulated and the
    formula's mean wait, their relative difference and whether it is within 5%.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    if kind != "queue_formula":
        raise _bad("kind", "must be queue_formula")
    if not s["versions"]:
        raise ToolError("E_STUDY_NO_VERSION", "the study has no model version to validate",
                        "build a version with study_step first")
    layout = _read_layout(folder, s["versions"][-1]["id"])
    f = queue_formula(layout)
    if f is None:
        raise ToolError("E_STUDY_NOT_APPLICABLE", f"the queue formula does not apply: {why_not(layout)}",
                        "validate against data or an expert and record it with study_update")
    # ponytail: no time_limit in the model falls back to 1000; run through api (no logs) because the
    # 100x run would only fill the resource log.
    length = 100 * float((layout.get("simulation") or {}).get("time_limit") or 1000)
    ws = workspace()
    try:
        results = api.run_model(layout, replications=10, seed=0, run_length=length, warmup=0.1 * length,
                                record_logs=False, network=ws.network, confine_to=ws.root)
    except RuntimeError as exc:
        raise ToolError("E_RUN_FAILED", str(exc), "fix the model with study_step") from exc
    res = layout["resources"][0]["name"]
    wait = kpi_mean(_digest(results), f"{res}/Time in Queue Avg") if results is not None else None
    if wait is None:
        raise ToolError("E_NO_RESULTS", "the long run gave no mean wait", "check the model with study_step")
    rel = (wait - f["wq"]) / f["wq"]
    passed = abs(rel) <= 0.05
    entry = {"level": "known_answer", "passed": passed,
             "text": f"{kind}: model {wait:.4g} vs formula {f['wq']:.4g} ({100 * rel:+.1f}%)"}
    _set(s, {"validation": s["validation"] + [entry]})      # the path study_update uses
    _log(s, "study_validate", started)
    _save(folder, s)
    _page(folder, s)
    return {"kind": kind, "model": round(wait, 6), "formula": round(f["wq"], 6), "rel_error": round(rel, 6),
            "passed": passed, "next_step": _next(folder, s)}


@tool()
@_linked
def study_accept(study: str, reason: str) -> dict:
    """Record that the person accepts the study as it stands. Call it only with the
    person's own words, after they have explicitly accepted; never on your own judgment. It
    changes the label, not the checks: open issues stay listed.

    Returns {"sufficiency": label}.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    if not isinstance(reason, str) or not reason.strip():
        raise _bad("reason", "give the person's own words")
    s["accepted"] = {"reason": reason.strip(), "by": "person (relayed by the assistant)"}
    _log(s, "study_accept", started)
    _save(folder, s)
    if s["versions"]:   # the banner changes with an acceptance
        _page(folder, s)
    return {"sufficiency": _label(s)}


@tool()
@_linked
def study_revert(study: str, version: str, note: str, request_id: str | None = None) -> dict:
    """Go back to an earlier version: it is recorded again as a new version, so no history is
    lost. Use it when a later step made things worse.

    Returns the new version, like study_step.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    _log(s, "study_revert", started)
    key = study_mod.request_key("revert", request_id)
    cached = key is not None and key in s["requests"]
    with _study_errors():
        answer = dict(study_mod.revert(folder, s, version, note, request_id))
    if not cached:     # the slot must hold the reverted layout, not the last (maybe broken) one
        layout = json.loads((folder / "versions" / answer["id"] / "model.json").read_text(encoding="utf-8"))
        call("write_model", model=_slot(folder), layout=layout)
        if page := _page(folder, s):
            answer["page"] = page
    answer["sufficiency"] = _label(s)
    answer["next_step"] = _next(folder, s)
    return answer


@tool()
@_linked
def study_report(study: str, version: str | None = None) -> dict:
    """Write a version's full HTML report (KPIs with intervals, resources, charts) on request.
    Steps do not write it, to keep the default output light. It reruns the version's stored
    model with the step's stored replications and seed, so the numbers match the step.

    Returns {"path": the report file relative to the workspace root}.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    with _study_errors():
        vid = study_mod.version_id(s, version)
    base = folder / "versions" / vid
    try:
        layout = json.loads((base / "model.json").read_text(encoding="utf-8"))
        step = json.loads((base / "step.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("E_STUDY_UNREADABLE", f"version {vid} files cannot be read",
                        "Record a new step.") from None
    model = _slot(folder)       # the report needs results, and write_model drops them: run fresh
    for name, args in (("write_model", {"layout": layout}),
                       ("run_model", {"replications": step.get("replications", FIRST_PASS),
                                      "seed": step.get("seed", 0)}),
                       ("export", {"format": "html", "path": f"{_sid(folder)}/versions/{vid}/report.html"})):
        out = call(name, model=model, **args)
        if "error" in out:
            raise ToolError(out["error"]["code"], out["error"]["message"], out["error"].get("hint"))
    _log(s, "study_report", started)
    _save(folder, s)
    return {"path": f"{_sid(folder)}/versions/{vid}/report.html"}


@tool()
@_linked
def study_package(study: str) -> dict:
    """Build the study's deliverable, the final files to hand over, in its package/ folder:
    the control room page, an Excel
    workbook (goals, versions with KPIs, checklist, assumptions), the current model and
    RO-Crate metadata. Use it when the person is satisfied with the study.

    Returns {"files": [paths relative to the workspace root], "next_step"}.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    with _study_errors():
        names = studypackage.package(folder, s)
    _log(s, "study_package", started)
    _save(folder, s)
    return {"files": [f"{_sid(folder)}/{n}" for n in names],
            "next_step": "Give the person package/control-room.html (print it to PDF) and study.xlsx."}


@tool(read_only=True)
@_linked
def study_view(study: str, version: str | None = None, format: str = "png") -> dict:
    """See a version's flowchart: built blocks, what this step changed and what of the agreed
    target is not built yet. Use it to check the model looks like the process.

    Returns {"version", "format", "text"} (the text lists every block, state and link), plus
    "image" for png or "svg" for svg. Without matplotlib a png request answers with the text
    and a "note".
    """
    folder, s = _open(study)
    if format not in ("png", "svg", "text"):
        raise _bad("format", "must be png, svg or text")
    with _study_errors():
        vid = study_mod.version_id(s, version)
    base = folder / "versions" / vid
    try:
        layout = json.loads((base / "model.json").read_text(encoding="utf-8"))
        step = json.loads((base / "step.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError("E_STUDY_UNREADABLE", f"version {vid} files cannot be read",
                        "Record a new step.") from None
    changed = set(step.get("changed") or flowchart.changed_blocks(step.get("changes", [])))
    geom = flowchart.geometry(layout, changed,
                              pending=[c for c in s["target"] if not c.get("blocks")])
    title = next(v["note"] for v in s["versions"] if v["id"] == vid)
    out = {"version": vid, "format": format, "text": flowchart.to_text(geom)}
    if format == "svg":
        out["svg"] = flowchart.to_svg(geom, f"{vid}: {title}")
    elif format == "png":
        png = flowchart.to_png(geom, f"{vid}: {title}")
        if png is None:
            out.update(format="text", note="install nagasim[plot] for images")
        else:
            out["image"] = {"mime_type": "image/png", "data": base64.b64encode(png).decode("ascii")}
            if geom["width"] > 2 * 1600:   # drawn at under half size: names are too small to read
                out["note"] = "the model is wide, so the image is scaled down; read the text for names"
    return out


@tool(read_only=True)
@_linked
def study_show(study: str) -> dict:
    """Show the study's control room, its dashboard. In chats that support MCP Apps the page appears inline,
    with buttons to revert to a version or accept the study; elsewhere this is a short summary
    and the path of the page file.

    Returns {"study", "sufficiency", "goals_met": "k of n", "versions", "page": path or None, "text"}.
    """
    folder, s = _open(study)
    goals = study_mod.goals_status(s)
    met = f"{sum(g['status'] == 'met' for g in goals)} of {len(goals)}"
    page = f"{_sid(folder)}/control-room.html" if (folder / "control-room.html").is_file() else None
    label = _label(s)
    text = (f"{s['question']} Status: {label}. Goals met: {met}. Versions: {len(s['versions'])}."
            + (f" Page: {page}." if page else ""))
    return {"study": _sid(folder), "sufficiency": label, "goals_met": met,
            "versions": len(s["versions"]), "page": page, "text": text}


@tool(read_only=True)
def study_page_html(study: str) -> dict:
    """The control room page as one HTML string. It exists for the in-chat page, which fetches
    it itself so the HTML never enters the conversation; assistants should call study_show instead.

    Returns {"html"}.
    """
    folder, s = _open(study)
    return {"html": studypage.render(folder, s, study_id=_sid(folder))}
