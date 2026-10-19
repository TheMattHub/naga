"""Study tools: the engine owns a study folder and tests every model step, so any assistant
can run a simulation study. The folder is the memory (stateless calls); `nagasim.study` and
`nagasim.studychecks` hold the logic, these tools only resolve paths, call `write_model` /
`run_model` / `export` through the registry and map `StudyError` to `ToolError`."""
from __future__ import annotations

import base64
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

from nagasim import flowchart, studypackage, studypage
from nagasim import study as study_mod
from nagasim.studychecks import step_checks
from nagasim.study import CHECKLIST, LEVELS, MODES, SIZES, StudyError
from nagasim.twin import write_text

from .core import ToolError, _document_args, call, tool, workspace

_ID = "Study folder relative to the workspace root (the id study_start returned)."
_document_args("study_start", question="The decision or question the study answers, in the person's words.",
               study="Folder for the study; omit to use naga-studies/<short name of the question>.",
               mode="one_go (plan approved first, then built) or incremental (built step by step).",
               size="quick, standard or deep: caps on replications, steps and scenarios.")
_document_args("study_status", study=_ID)
_document_args("study_update", study=_ID,
               changes="Keys to set: question, decision, mode, size, plan_approved, goals, target, "
                       "assumptions, sources, validation, experiments, skipped. A list key is "
                       "replaced whole. goals and assumptions entries need a string id; a goal is "
                       "{id, text, kpi, low, high}; an assumption is {id, text, value, source}; a "
                       "target component is {name, purpose, blocks: [block names]}; a source is "
                       "{id, text, input}; validation is "
                       "{level: data|known_answer|expert, passed: bool, ...}.")
_document_args("study_add_input", study=_ID, name="File name to store under inputs/ (no folders).",
               path="Workspace file to copy in; give this or text.",
               text="Text to store (a transcript, notes); give this or path.")
_document_args("study_step", study=_ID, layout="The complete model file as one JSON object (as write_model).",
               note="One line: what changed in this step and why.",
               request_id="Any unique string; a retried call with the same one is not recorded twice.",
               seed="First replication's seed.")
_document_args("study_view", study=_ID, version="Version id, e.g. v002; omit for the last.",
               format="png (image), svg or text.")
_document_args("study_package", study=_ID)
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


def _open(study: str) -> tuple[Path, dict]:
    folder = _folder(study)
    with _study_errors():
        return folder, study_mod.load(folder)


def _sid(folder: Path) -> str:
    return folder.relative_to(workspace().root).as_posix()


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


def _store(folder: Path, vid: str) -> dict | None:
    """Export the version's report from the model slot; returns the error, if any."""
    report = call("export", model=_slot(folder), format="html",
                  path=f"{_sid(folder)}/versions/{vid}/report.html")
    return report.get("error")


def _page(folder: Path, study: dict) -> str | None:
    """Refresh the control room page after a recorded version; a failed write must not lose the
    version, so it answers None and the step still succeeds."""
    try:
        write_text(folder / "control-room.html", studypage.render(folder, study))
    except Exception:   # a hand-edited record that does not render must not fail a recorded step
        logging.getLogger(__name__).warning("control room page not written", exc_info=True)
        return None
    return f"{_sid(folder)}/control-room.html"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "study"


@tool()
def study_start(question: str, study: str | None = None, mode: str = "one_go",
                size: str = "standard") -> dict:
    """Start a simulation study: make its folder and say what to do first.

    Use it for a decision question ("how many cranes do we need?"), then follow the
    next_step in every answer. The study keeps its state in the folder; pass the returned
    `study` to the other study tools.

    Returns {"study", "next_step", "plan_card", "effort"}.
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
    with _study_errors():
        s = study_mod.create(folder, question.strip(), mode, size, save_now=False)
    _log(s, "study_start", started)
    _save(folder, s)
    return {"study": _sid(folder), "next_step": study_mod.next_step(s, folder),
            "plan_card": study_mod.plan_card(s), "effort": study_mod.effort(s)}


@tool(read_only=True)
def study_status(study: str) -> dict:
    """Where a study stands, on one screen: sufficiency, goals, the ten-step checklist, the
    last version's KPIs and the next step. Call it first on an existing study folder.

    Returns {"study", "question", "mode", "size", "sufficiency", "why_not_sufficient", "goals",
    "checklist", "last_version", "versions", "effort", "next_step"}, plus "plan_card" until the
    plan is approved.
    """
    folder, s = _open(study)
    suff = study_mod.sufficiency(s)
    out = {"study": _sid(folder), "question": s["question"], "mode": s["mode"], "size": s["size"],
           "sufficiency": suff["label"], "why_not_sufficient": suff["why"],
           "goals": study_mod.goals_status(s),
           "checklist": [{k: c[k] for k in ("item", "verdict", "why")} for c in study_mod.checklist(s, folder)],
           "last_version": s["versions"][-1] if s["versions"] else None, "versions": len(s["versions"]),
           "effort": study_mod.effort(s), "next_step": study_mod.next_step(s, folder)}
    if (folder / "control-room.html").is_file():
        out["page"] = f"{_sid(folder)}/control-room.html"
    if not s["plan_approved"]:
        out["plan_card"] = study_mod.plan_card(s)
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
    return value


def _check_assumptions(key, value):
    for a in _dicts(key, value, need_id=True):
        if a.get("check") is not None:
            if not isinstance(a["check"], dict):
                raise _bad(key, "check must be an object {kpi, low, high}")
            _bounds(key, a["check"])
    return value


def _check_validation(key, value):
    for v in _dicts(key, value):
        if v.get("level") not in LEVELS:
            raise _bad(key, f"level must be one of {list(LEVELS)}")
        if "passed" in v and not isinstance(v["passed"], bool):
            raise _bad(key, "passed must be true or false")
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
    "plan_approved": _check_type(bool, "true or false"),
    "goals": _check_goals, "assumptions": _check_assumptions, "validation": _check_validation,
    "target": lambda k, v: _dicts(k, v), "sources": _check_sources,
    "experiments": lambda k, v: _dicts(k, v), "skipped": _check_skipped,
}


@tool()
def study_update(study: str, changes: dict) -> dict:
    """Record what the study has learned or the person decided: set any of question,
    decision, mode, size, plan_approved, goals, target, assumptions, sources, validation,
    experiments, skipped. A list key replaces the whole list. Nothing here can mark the study
    accepted.

    Returns {"ok", "next_step"}.
    """
    started = time.monotonic()
    folder, s = _open(study)
    if not isinstance(changes, dict) or not changes:
        raise _bad("changes", "must be an object with at least one key")
    unknown = [k for k in changes if k not in _SETTABLE]
    if unknown:
        raise _bad(unknown[0], f"cannot be set; settable keys: {', '.join(_SETTABLE)}")
    checked = {k: _SETTABLE[k](k, v) for k, v in changes.items()}     # all checked before any is set
    s.update(checked)
    _log(s, "study_update", started)
    _save(folder, s)
    if s["versions"]:   # the page shows goals and assumptions too
        _page(folder, s)
    return {"ok": True, "next_step": study_mod.next_step(s, folder)}


@tool()
def study_add_input(study: str, name: str, path: str | None = None, text: str | None = None) -> dict:
    """Keep a source of the study in its inputs/ folder: copy a workspace file, or store text
    (a call transcript, notes). Then name it with study_update(sources=[{"id", "text",
    "input": <the returned path>}]) so the inputs step can check it.

    Returns {"input": "inputs/<name>"}.
    """
    started = time.monotonic()
    folder, s = _open(study)
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
def study_step(study: str, layout: dict, note: str, request_id: str | None = None, seed: int = 0) -> dict:
    """Test one model step: write the whole model, run it, check it, and store it as a new
    version (or as a draft when it fails). Always send the complete model file and a one-line
    note. A model that does not validate or run is stored as a draft with its problems, not
    refused: read "problems", fix the model, step again.

    Returns {"id", "kind": "version"|"draft", "verdict", "checks", "changes", "kpis",
    "sufficiency", "next_step"}; a version also has its report at versions/<id>/report.html.
    """
    started = time.monotonic()
    folder, s = _open(study)
    key = study_mod.request_key("step", request_id)
    cached = key is not None and key in s["requests"]
    digest = rerun = None
    problems: list = []
    if not cached:
        model = _slot(folder)
        written = call("write_model", model=model, layout=layout)
        if "error" in written:
            problems = [written["error"]]
        elif not written["ok"]:
            problems = written["problems"]
        else:
            n = SIZES[s["size"]]["replications"]
            digest = call("run_model", model=model, replications=n, seed=seed)
            # ponytail: same-seed rerun on the first version and every fifth halves the cost;
            # rerun every step if drift is ever seen.
            if "error" not in digest and len(s["versions"]) % 5 == 0:
                rerun = call("run_model", model=model, replications=n, seed=seed)
            for r in (digest, rerun):
                if r is not None and "error" in r:
                    problems, digest, rerun = [r["error"]], None, None
                    break
    checks = step_checks(layout, digest, rerun, s) if digest is not None else []
    _log(s, "study_step", started)
    answer = dict(study_mod.record_step(folder, s, layout, digest, checks, note, request_id, problems))
    if answer["kind"] == "version" and not cached:
        err = _store(folder, answer["id"])
        if err:
            answer["report_error"] = err
        if page := _page(folder, s):
            answer["page"] = page
    answer["sufficiency"] = _label(s)
    answer["next_step"] = study_mod.next_step(s, folder)
    return answer


@tool()
def study_accept(study: str, reason: str) -> dict:
    """Record that the person accepts the study as it stands. Call it only with the
    person's own words, after they have explicitly accepted; never on your own judgment. It
    changes the label, not the checks: open issues stay listed.

    Returns {"sufficiency": label}.
    """
    started = time.monotonic()
    folder, s = _open(study)
    if not isinstance(reason, str) or not reason.strip():
        raise _bad("reason", "give the person's own words")
    s["accepted"] = {"reason": reason.strip(), "by": "person (relayed by the assistant)"}
    _log(s, "study_accept", started)
    _save(folder, s)
    if s["versions"]:   # the banner changes with an acceptance
        _page(folder, s)
    return {"sufficiency": _label(s)}


@tool()
def study_revert(study: str, version: str, note: str, request_id: str | None = None) -> dict:
    """Go back to an earlier version: it is recorded again as a new version, so no history is
    lost. Use it when a later step made things worse.

    Returns the new version, like study_step.
    """
    started = time.monotonic()
    folder, s = _open(study)
    _log(s, "study_revert", started)
    key = study_mod.request_key("revert", request_id)
    cached = key is not None and key in s["requests"]
    with _study_errors():
        answer = dict(study_mod.revert(folder, s, version, note, request_id))
    if not cached:     # the slot must hold the reverted layout, not the last (maybe broken) one
        layout = json.loads((folder / "versions" / answer["id"] / "model.json").read_text(encoding="utf-8"))
        call("write_model", model=_slot(folder), layout=layout)
        # export needs results and write_model drops them: the revert's report comes from a fresh run
        run = call("run_model", model=_slot(folder), replications=SIZES[s["size"]]["replications"], seed=0)
        err = run.get("error") or _store(folder, answer["id"])
        if err:
            answer["report_error"] = err
        if page := _page(folder, s):
            answer["page"] = page
    answer["sufficiency"] = _label(s)
    answer["next_step"] = study_mod.next_step(s, folder)
    return answer


@tool()
def study_package(study: str) -> dict:
    """Build the study's deliverable in its package/ folder: the control room page, an Excel
    workbook (goals, versions with KPIs, checklist, assumptions), the current model and
    RO-Crate metadata. Use it when the person is satisfied with the study.

    Returns {"files": [paths relative to the workspace root], "next_step"}.
    """
    started = time.monotonic()
    folder, s = _open(study)
    with _study_errors():
        names = studypackage.package(folder, s)
    _log(s, "study_package", started)
    _save(folder, s)
    return {"files": [f"{_sid(folder)}/{n}" for n in names],
            "next_step": "Give the person package/control-room.html (print it to PDF) and study.xlsx."}


@tool(read_only=True)
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
    ids = [v["id"] for v in s["versions"]]
    vid = version if version is not None else (ids[-1] if ids else None)
    if vid is None or vid not in ids or not re.fullmatch(r"v\d{3,}", vid):
        raise ToolError("E_STUDY_NO_VERSION", f"no version {vid!r}",
                        f"Pick an id from the study's versions list: {ids}.")
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
