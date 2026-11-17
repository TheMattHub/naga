"""Study tools: the engine owns a study folder and tests every model step, so any assistant
can run a simulation study. The folder is the memory (stateless calls); `nagasim.study` and
`nagasim.studychecks` hold the logic, these tools only resolve paths, call `write_model` /
`run_model` / `export` through the registry and map `StudyError` to `ToolError`."""
from __future__ import annotations

import base64
import functools
import logging
import json
import math
import os
import re
import shutil
import statistics
import uuid
import time
from typing import Literal
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nagasim import api, experiments, flowchart, laws, studyinput, studypackage, studypage, studyroom
from nagasim.core.stats import ci95, paired_diff_ci, pitman_morgan
from nagasim import study as study_mod
from nagasim.studychecks import goal_kpi, kpi_entry, kpi_mean, scenario_checks, step_checks
from nagasim.studyformulas import TOTAL_WAIT, queue_formula, total_wait_shares, wait_shares, why_not
from nagasim.study import CHECKLIST, FIRST_PASS, LEVELS, MODES, PRECISION, SIZES, StudyError
from nagasim.core.schema import ModelValidationError
from nagasim.twin import write_json, write_text

from .core import (SLOTS, ToolError, _PROGRESS, _digest, _document_args, call, override_hint, report,
                   reporting, tool, workspace)

# The study's closed sets as enums in the schema (D1219), from the constants the study checks.
Mode = Literal[MODES]
Size = Literal[tuple(SIZES)]
from .runs import _run_laws

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
                       "{id, text, input, path?} (path: the model field the data feeds, e.g. "
                       "blocks.<name>.params.distribution); a validation entry is {level: data|known_answer|expert, "
                       "passed: true|false, text}, and yours reads \"reported, not checked\" (only "
                       "study_validate or the person makes it pass); an experiment is {id, text, result}.")
_document_args("study_update", **{k: f"Same as the \"{k}\" key of changes." for k in (
    "question", "mode", "size", "plan_approved", "precision", "goals", "target", "assumptions",
    "sources", "validation", "experiments", "skipped")})
_document_args("study_add_input", study=_ID, name="File name to store under inputs/ (no folders).",
               path="Workspace file to copy in; give this or text.",
               text="Text to store (a transcript, notes); give this or path.")
_document_args("study_step", study=_ID, layout="The complete model file as one JSON object (get_model_format shows its shape).",
               model="Instead of layout: the name of a model open in the workspace, stepped as it is.",
               patch="Instead of layout: [{\"path\", \"value\"}] changes to the last version, in the "
                     "scenario override form, e.g. [{\"path\": \"resources.Server.capacity\", \"value\": 2}].",
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


@contextmanager
def _stage(index: int, count: int, message: str):
    """Stage *index* of *count* of a multi-run tool: the runs inside report their own
    done/total, which would restart from 0 at each stage; here they land in (index-1, index) so
    the tool's progress only grows. Reads the sink itself, since `reporting` below replaces it."""
    outer = _PROGRESS.get()
    if outer is None:
        yield
        return

    def send(*args):        # through `report`, so a failing sink never fails the step
        with reporting(outer):
            report(*args)

    def inner(done, total=None, _message=""):
        send(index - 1 + (min(done / total, 1.0) if total else 0.0), count, message)

    with reporting(inner):
        yield


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


def _log(study: dict, name: str, started: float, error: str | None = None) -> None:
    row = {"tool": name, "seconds": round(time.monotonic() - started, 3),
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if error:       # a step stored as a draft: why it took no time
        row["error"] = error
    study["usage"].append(row)


def _save(folder: Path, study: dict) -> None:
    study_mod.save(folder, study)       # OSError is mapped by @tool


def _label(study: dict) -> str:
    return study_mod.sufficiency(study)["label"]


def _slot(folder: Path) -> str:
    # Reserved name: a person's open model is never overwritten by a study step.
    return f"{_SLOT_PREFIX}{_sid(folder)}"


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
        if g["kpi"] == TOTAL_WAIT:
            shares = total_wait_shares(results, x)
            if shares is None:
                why.append(f"goal {g.get('id')}{where}: the run's entity log has no complete Total Wait "
                           f"column (empty or truncated), so the share over {x:g} is not verified")
                continue
        else:
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


def _scenario_fix(layout: dict, problem) -> str:
    """The corrected override for a scenario problem, when one follows from it (a capacity under
    its schedule's maximum), else the problem's own hint."""
    m = re.fullmatch(r"/resources/(\d+)/capacity", problem.path or "")
    least = re.search(r"at least (\d+)", problem.hint or "")
    resources = layout.get("resources") or []
    if problem.code == "E_RESOURCE_CAPACITY" and m and least and int(m[1]) < len(resources):
        fix = json.dumps({"path": f"resources.{resources[int(m[1])].get('name')}.capacity",
                          "value": int(least[1])})
        return f"add {fix} to its overrides and step again"
    return problem.hint or "fix its overrides and step again"


def _runnable(layout: dict, names: list[str]) -> tuple[list[str], list[str]]:
    """(the scenarios that validate, one sentence per scenario that does not) -- checked one by one,
    so one bad scenario does not lose the others (#1729)."""
    ok, bad = [], []
    for name in names:
        try:
            experiments._scenario_layouts(layout, [name], baseline=False)
        except ModelValidationError as exc:
            what = "; ".join(p.removeprefix(f"scenario '{name}': ") for p in exc.problems)
            fix = (_scenario_fix(layout, exc.structured[0]) if exc.structured
                   else "fix its overrides and step again")
            bad.append(f"Scenario '{name}' did not run: {what}; {fix}.")
        else:
            ok.append(name)
    return ok, bad


def _same_base_replications(folder: Path, s: dict, layout: dict) -> int:
    """The last version's replications when it is the same base model (its scenarios aside), else 0.
    A relaxed precision target must not make the next version of the same model run fewer
    replications and move every number (review S2-16): the two-pass estimate comes from a
    10-replication pilot and can fall short of what the last version needed."""
    if not s["versions"] or not isinstance(layout, dict):
        return 0
    last = s["versions"][-1]
    try:
        before = _read_layout(folder, last["id"])
    except (OSError, ValueError):
        return 0
    same = {k: v for k, v in before.items() if k != "scenarios"} == \
        {k: v for k, v in layout.items() if k != "scenarios"}
    return int(last.get("replications") or 0) if same else 0


def _series(results, layout: dict, s: dict) -> dict[str, dict[int, float]]:
    """{goal kpi: {seed: value}} per replication, read in `kpi_entry`'s order (system, user
    statistic, tail share, resource column). A run's "Run" is its seed, so two runs pair by seed,
    never by position. A tail share list has no seed: it pairs only when it has one value per run."""
    if results is None:
        return {}
    flat = experiments.flatten_kpis(results)
    seeds = sorted(int(x) for x in flat["Run"].unique()) if not flat.empty else []
    out: dict = {}
    for g in s["goals"]:
        kpi = goal_kpi(g) if g.get("kpi") else None
        if not kpi or kpi in out:
            continue
        for key in (f"system/{kpi}", f"user/{kpi}", None, f"resource/{kpi}"):
            if key is None:         # the tail share, rebuilt as `_tails` does
                if g.get("share_above") is None:
                    continue
                shares = (total_wait_shares(results, g["share_above"]) if g["kpi"] == TOTAL_WAIT
                          else wait_shares(results, g["kpi"].partition("/")[0], g["share_above"], layout))
                if shares is not None and len(shares) == len(seeds):
                    out[kpi] = dict(zip(seeds, map(float, shares)))
                    break
                continue
            rows = flat[flat["KPI"] == key]
            if not rows.empty:
                out[kpi] = {int(r): float(v) for r, v in zip(rows["Run"], rows["Value"])}
                break
    return out


def _paired(base: dict[int, float] | None, scen: dict[int, float] | None) -> dict | None:
    """D1263, D1265: an option against the Baseline over their common seeds -- the first n of
    each, n = min of the two counts -- as {"diff_vs_baseline": {mean, lo, hi, n} (the 95%
    t-interval of the per-replication differences), "spread": {n, sd, base_sd, mean, base_mean,
    p}} (p: two-sided Pitman-Morgan, absent when the test cannot run). None without pairing:
    other seeds, or fewer than 2 pairs."""
    if not base or not scen:
        return None
    b, o = sorted(base), sorted(scen)
    n = min(len(b), len(o))
    if n < 2 or b[:n] != o[:n]:
        return None
    x = [scen[k] for k in o[:n]]
    y = [base[k] for k in b[:n]]
    diff = paired_diff_ci(x, y)
    if not all(math.isfinite(diff[k]) for k in ("mean", "lb", "ub")):
        return None
    sd, base_sd = (float(statistics.stdev(v)) for v in (x, y))
    spread = {"n": n, "sd": sd, "base_sd": base_sd, "mean": statistics.fmean(x), "base_mean": statistics.fmean(y)}
    test = pitman_morgan(x, y)
    if test["p"] is not None:
        spread["p"] = test["p"]
    # Plain floats: a numpy scalar prints as np.float64(...) in a Python answer (#1862).
    return {"diff_vs_baseline": {"mean": float(diff["mean"]), "lo": float(diff["lb"]), "hi": float(diff["ub"]), "n": n},
            "spread": spread}


def _run_scenarios(layout: dict, s: dict, n: int, seed: int, base=None) -> tuple[dict | None, dict]:
    """Run the layout's scenarios (not the baseline: the step just ran it) with the step's
    replications and seed, from the layout itself so a fresh process needs no model slot.
    Answers ({name: {"kpis": {goal kpi: {mean, ci_half_width}}, "goals": {goal id: verdict}}} or
    None, extra answer keys). With the Baseline's results *base*, a goal KPI's entry also holds
    its paired difference and spread (`_paired`). A scenario whose law checks warned also holds
    "laws" ({passed, partial}); its goals are not_verified (#1880), or under censoring or initial
    bias alone those a low bias could reverse (D1316). A failing scenario run is
    reported, never fails the step; a scenario that does not validate is left out and named in
    "scenario_error" (#1729)."""
    names = [sc.get("name") for sc in layout.get("scenarios") or [] if isinstance(sc, dict) and sc.get("name")]
    extra: dict = {}
    cap = SIZES[s["size"]]["scenarios"]
    if len(names) > cap:
        extra["scenarios_skipped"] = (f"size {s['size']} runs at most {cap} scenarios; not run: "
                                      + ", ".join(names[cap:]))
        names = names[:cap]
    names, bad = _runnable(layout, names)
    if bad:
        extra["scenario_error"] = " ".join(bad)
    if not names:
        return None, extra
    try:
        run = experiments.run_scenarios(layout, names, replications=n, seed=seed, baseline=False, on_progress=report)
    except Exception as exc:    # a run error: the base step stands
        logging.getLogger(__name__).warning("study scenarios failed", exc_info=True)
        extra["scenario_error"] = " ".join(bad + [f"The scenarios did not run: {type(exc).__name__}: {exc}."])
        return None, extra
    out, why = {}, []
    base_series = _series(base, layout, s)
    checks = run.checks()
    for name, results in run.results.items():
        digest = _digest(results)
        why += _tails(s, digest, results, layout, f" in scenario {name}")
        mine = _series(results, layout, s) if base_series else {}
        # #1880: the option's own law checks, as the Baseline's run has them. A partial law makes its
        # numbers no result: every goal not_verified, so it is never a best option. D1316: censoring
        # or initial bias alone keep a verdict a low bias cannot reverse.
        invalid, low = laws.split(checks[name].partial)
        kpis, verdicts = {}, {}
        for i, g in enumerate(s["goals"]):
            e = kpi_entry(digest, goal_kpi(g)) if g.get("kpi") else None
            if e is not None:
                kpis[goal_kpi(g)] = {k: e[k] for k in ("mean", "ci_half_width") if e.get(k) is not None}
                if pair := _paired(base_series.get(goal_kpi(g)), mine.get(goal_kpi(g))):
                    kpis[goal_kpi(g)].update(pair)
            verdicts[g.get("id", f"g{i + 1}")] = study_mod.judge(g, None if invalid or not e else e["mean"],
                                                                 biased_low=bool(low))
        out[name] = {"kpis": kpis, "goals": verdicts}
        if checks[name].partial:    # only then, so a clean option's record is as it was
            out[name]["laws"] = {"passed": checks[name].passed, "partial": list(checks[name].partial)}
    if not run.completed:
        extra["scenario_error"] = " ".join(bad + ["The scenario run did not finish."])
    if why:
        extra["tail_note"] = "; ".join(why)
    return out or None, extra


def _options(folder: Path, s: dict) -> list[dict]:
    """The top three what-next options (D727), arguments filled for this study. A record that does
    not read must not fail the answer that carries them: none, and a warning."""
    try:
        return study_mod.next_options(s, folder, _sid(folder), 3)
    except Exception:
        logging.getLogger(__name__).warning("what-next options not computed", exc_info=True)
        return []


# --- the study's model slot ("study:<id>"): its last version, and what was asked of it ----------
# The model tools the what-next rules read back (nextoptions: explain_kpi, its suggested what_if,
# input_uncertainty, calibrate), kept per version in versions/<id>/asked.json.
_KEPT = frozenset({"explain_kpi", "what_if", "input_uncertainty", "calibrate"})
_ASKED_CAP = 100


def _slot_version(ws, name: str) -> tuple[Path, dict, str, Path]:
    sid = name[len(_SLOT_PREFIX):]
    folder = ws.resolve(sid)        # confined to the workspace root (netguard)
    if folder == ws.root:
        raise _bad("model", "a study slot names a study folder inside the workspace")
    with _study_errors():
        s = study_mod.load(folder)
        vid = study_mod.version_id(s)
    return folder, s, vid, folder / "versions" / vid / "model.json"


def _load_slot(ws, name: str):
    """The study's last version, for a model tool sent to "study:<id>" in a process that has not
    stepped the study."""
    from nagasim.core.document import ModelDocument
    from nagasim.core.schema import load_layout
    return ModelDocument(load_layout(str(_slot_version(ws, name)[3])))


def _keep_asked(ws, name: str, entry: dict) -> None:
    """Append *entry* to the last version's asked.json when the slot holds that version (after a
    failed step it holds the draft, which has no record); skipped when another process writes.
    The first kept entry claims the study, and the claim lasts for this process's life: a model tool
    on the study's slot makes this process the study's writer, as a study tool would."""
    if entry.get("tool") not in _KEPT:
        return
    from nagasim.core.document import ModelDocument
    from nagasim.core.schema import load_layout
    folder, _s, _vid, path = _slot_version(ws, name)
    if ws.models[name].to_layout() != ModelDocument(load_layout(str(path))).to_layout():
        return
    try:
        study_mod.claim(folder)
    except StudyError:
        return
    target = path.parent / "asked.json"
    try:
        rows = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = []
    rows = (rows if isinstance(rows, list) else []) + [entry]
    write_json(target, rows[-_ASKED_CAP:])


def _recorded(ws, name: str) -> list[dict]:
    """The asked.json rows of the slot's last version (none when unreadable)."""
    try:
        rows = json.loads((_slot_version(ws, name)[3].parent / "asked.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, ToolError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


_SLOT_PREFIX = "study:"
SLOTS[_SLOT_PREFIX] = {"load": _load_slot, "asked": _keep_asked, "recorded": _recorded}


def _validate_call(folder: Path, s: dict) -> str | None:
    """The ready-to-send study_validate call when the validation item is open and the last version is
    a queue the formula covers; None otherwise."""
    if not s["versions"] or any(study_mod.checked(v) for v in s["validation"]):
        return None
    layout = _read_layout(folder, s["versions"][-1]["id"])
    return f'study_validate(study="{_sid(folder)}")' if queue_formula(layout) else None


def _read_layout(folder: Path, version: str) -> dict:
    return json.loads((folder / "versions" / version / "model.json").read_text(encoding="utf-8"))


def _next(folder: Path, s: dict) -> str:
    """The next step with the study id filled in, so the assistant can send the call as written."""
    raw, sid = study_mod.next_step(s, folder), _sid(folder)
    text = raw.replace("study_update(changes=", f'study_update(study="{sid}", changes=')
    offer = _validate_call(folder, s)
    if offer and raw == study_mod._TODO["validation"]:     # compared before the id is filled in (#1891)
        text = f"Check the model against the textbook queue formula with {offer}, or record another validation."
    elif offer and text == study_mod._REPORTED:     # another recorded one would not close it (D1234)
        text = (f"The validation is reported, not checked: check it with {offer}, or show the person "
                "what was compared.")
    if text == study_mod._TODO["model"]:
        text = (f'Build the first model version with study_step(study="{sid}", layout=<the whole model file>, '
                'note="first model").')
    return text


def _patched(folder: Path, s: dict, patch: list) -> dict:
    """The last version's model with *patch* applied (S2-18): a one-line fix without resending the file."""
    if not s["versions"]:
        raise ToolError("E_STUDY_NO_VERSION", "the study has no version to patch",
                        "step the first version with layout or model")
    try:
        return experiments.apply_overrides(_read_layout(folder, s["versions"][-1]["id"]), patch)
    except ModelValidationError as exc:
        raise ToolError("E_BAD_ARGUMENT", f"`patch`: {'; '.join(exc.problems)}",
                        override_hint(exc)) from None


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
    notes += _study_kpi_names(changes)      # before the checks, so they see the stored name
    checked = {k: _SETTABLE[k](k, v) for k, v in changes.items()}     # all checked before any is set
    return checked, notes


def _study_kpi_names(changes: dict) -> list[str]:
    """Store a goal's or an assumption check's KPI given in the results.kpis() form under the name
    the study measures, so it is judged and not left not_verified forever (#1758)."""
    notes = []
    for key, field in (("goals", None), ("assumptions", "check")):
        recs = changes.get(key)
        for i, rec in enumerate(recs if isinstance(recs, list) else []):
            spec = (rec.get(field) if field else rec) if isinstance(rec, dict) else None
            kpi = spec.get("kpi") if isinstance(spec, dict) else None
            if isinstance(kpi, str) and (name := experiments.study_kpi_name(kpi)) != kpi:
                spec["kpi"] = name
                notes.append(f"{key}[{rec.get('id', i)}]: kpi {kpi!r} -> {name!r}")
    return notes


def _set(s: dict, changes: dict) -> list[str]:
    checked, notes = _check(changes)    # nothing is set if any key is refused
    if "validation" in checked:
        checked["validation"] = _sourced(s["validation"], checked["validation"])
    s.update(checked)
    return notes


def _vkey(v: dict) -> tuple:
    return v.get("level"), v.get("passed"), v.get("text")


def _sourced(old: list, new: list) -> list:
    """D1234: every entry this path writes is the assistant's, whatever its payload says (the source
    is the caller's, never the file's). A list key replaces the whole list, so an entry resent
    unchanged (level, passed and text equal to a stored one) keeps the stored source.
    D1259 (#1815): a person's entry is the person's alone. It is kept exactly as stored, whether
    the list resends it (with any field changed) or leaves it out; no tool edits or drops it."""
    person = [v for v in old if v.get("source") == "person"]
    stored: dict[tuple, list] = {}
    for v in old:
        if v.get("source") != "person":
            stored.setdefault(_vkey(v), []).append(v.get("source"))
    taken = {_vkey(v) for v in person}
    out = []
    for v in new:
        if _vkey(v) in taken:
            continue        # the person's entry, resent: the stored one stands (added below)
        kept = stored.get(_vkey(v)) or [None]
        out.append({**v, "source": kept.pop(0) or v.get("source") or "assistant"})
    return person + out


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


# The guide's sections a cold assistant needs before its first goal or scenario (D1218): what to
# ask, the record shapes with what a KPI name is, the scenario shape. Sliced from SKILL.md, the one
# source the prompt and the naga://guide/study resource serve whole.
_GUIDE_SECTIONS = ("Ask the person first", "Records", "Scenarios")


def _guide() -> str:
    text = study_mod.skill_text()
    return "\n".join(m.group(0).strip() + "\n" for h in _GUIDE_SECTIONS
                     if (m := re.search(rf"^## {re.escape(h)}\b.*?(?=^## |\Z)", text, re.S | re.M)))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "study"


@tool(returns={
    "study": str, "next_step": str, "plan_card": dict, "effort": dict, "guide": str,
    "options": list, "normalised": list, "page_url": str, "room_url": str})
@_linked
def study_start(question: str, study: str | None = None, mode: Mode = "one_go",
                size: Size = "standard", changes: dict | None = None) -> dict:
    """Start a simulation study: make its folder, say what to do first and which KPI names a goal can use.

    Use it for a decision question ("how many cranes do we need?"), then follow the
    next_step in every answer. The study keeps its state in the folder; pass the returned
    `study` to the other study tools.

    Send the plan card in the same call with `changes` (the same keys and shapes as
    study_update). Example: study_start(question="How many cranes do we need?",
    changes={"goals": [{"id": "g1", "text": "wait under 5", "kpi": "Avg Throughput Time", "high": 5}]}).

    "guide" is what to ask the person, the record shapes with what a KPI name is, and the
    scenario shape (once, here); "normalised" appears when a near-miss input was mapped.
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
    out = {"study": _sid(folder), "next_step": _next(folder, s), "options": _options(folder, s),
           "plan_card": study_mod.plan_card(s), "effort": study_mod.effort(s), "guide": _guide()}
    return {**out, "normalised": notes} if notes else out


@tool(read_only=True, returns={
    "study": str, "question": str, "mode": str, "size": str, "sufficiency": str,
    "why_not_sufficient": list, "goals": list, "checklist": list, "last_version": dict | None,
    "versions": int, "effort": dict, "next_step": str, "options": list, "plan_card": dict,
    "room_url": str, "page": str, "validate": str, "page_url": str})
@_linked
def study_status(study: str) -> dict:
    """Where a study stands, on one screen: sufficiency, goals, the ten-step checklist, the
    last version's KPIs with how confident they are (precision of the confidence intervals) and
    the next step. Call it first on an existing study folder.

    Plus "plan_card" until the plan is approved and "validate" (a ready-to-send study_validate
    call) while no validation has passed and the last version is a queue with a textbook
    answer.

    "options" (in every answer that has next_step) is what next, at most 3, ranked fix > missing >
    improve > extend from the study's recorded numbers: {"kind", "title", "why" (with its number),
    "tool", "arguments", "cost", "source"}, plus "needs" for arguments still to fill in (such as the
    whole layout). Offer them to the person; none runs unasked.
    """
    folder, s = _open(study)
    suff = study_mod.sufficiency(s)
    out = {"study": _sid(folder), "question": s["question"], "mode": s["mode"], "size": s["size"],
           "sufficiency": suff["label"], "why_not_sufficient": suff["why"],
           "goals": study_mod.goals_status(s),
           "checklist": [{k: c[k] for k in ("item", "verdict", "why")} for c in study_mod.checklist(s, folder)],
           "last_version": s["versions"][-1] if s["versions"] else None, "versions": len(s["versions"]),
           "effort": study_mod.effort(s), "next_step": _next(folder, s), "options": _options(folder, s)}
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
            if not (isinstance(g.get("kpi"), str) and ("/" in g["kpi"] or g["kpi"] == TOTAL_WAIT)):
                raise _bad(key, 'a goal with share_above needs kpi "<resource>/Time in Queue" or "Total Wait"')
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
    # D1234: every path that checks changes (study_start, study_update, study_step) is the
    # assistant's; `_set` gives an entry resent unchanged its stored source back.
    for v in value if isinstance(value, list) else []:
        v["source"] = "assistant"
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


@tool(returns={
    "ok": bool, "next_step": str, "options": list, "room_url": str, "page_url": str,
    "normalised": list})
@_linked
def study_update(study: str, changes: dict | None = None, question: str | None = None,
                 mode: Mode | None = None, size: Size | None = None,
                 plan_approved: bool | None = None, precision: dict | None = None,
                 goals: list[dict] | None = None, target: list[dict] | None = None, assumptions: list[dict] | None = None,
                 sources: list[dict] | None = None, validation: list[dict] | None = None,
                 experiments: list[dict] | None = None, skipped: list[str] | None = None) -> dict:
    """Record what the study has learned or the person decided (a goal such as at most 5% of people
    waiting more than 20 minutes; fewer or more replications:
    set precision): set any of question,
    decision, mode, size, plan_approved, precision, goals, target, assumptions, sources, validation,
    experiments, skipped, inside `changes` or (all but decision) as parameters of the same name
    (a key sent both ways is refused; decision is changes={"decision": ...} only). A list key replaces the whole list. Nothing here can mark the study
    accepted. Near-miss shapes (a goal with "range": [0, 5], "statement" for "text", one object
    for a list) are mapped to the canonical record and listed in "normalised"; a typo'd key is
    refused with a corrected example.
    Example: study_update(study="naga-studies/cranes", validation=[{"level": "known_answer",
    "passed": true, "text": "M/G/1 formula vs model wait"}]).

    "normalised" (a list of notes) appears when something was mapped.
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
    out = {"ok": True, "next_step": _next(folder, s), "options": _options(folder, s)}
    return {**out, "normalised": notes} if notes else out


@tool(returns={"input": str, "page_url": str, "room_url": str})
@_linked
def study_add_input(study: str, name: str, path: str | None = None, text: str | None = None) -> dict:
    """Keep a source of the study in its inputs/ folder: copy a workspace file, or store text
    (a call transcript, notes). Then name it with study_update(changes={"sources": [{"id",
    "text", "input": <the returned path>}]}) so the inputs step can check it.
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


@tool(returns={
    "id": str, "kind": str, "verdict": str, "checks": list, "changes": list, "kpis": dict,
    "replications": int, "precision": dict, "precision_note": str, "sufficiency": str,
    "next_step": str, "problems": list, "options": list, "room_url": str, "page": str,
    "page_url": str, "tail_note": str, "scenarios": dict, "normalised": list,
    "scenarios_skipped": str, "scenario_error": str})
@_linked
def study_step(study: str, layout: dict | None = None, note: str | None = None, request_id: str | None = None, seed: int = 0,
               changes: dict | None = None, model: str | None = None, patch: list[dict] | None = None) -> dict:
    """Test one model step: write the whole model, run it, check it, and store it as a new
    version (or as a draft when it fails). Send the complete model file as `layout`, or `model`
    (the name of a model open in the workspace), or `patch` (changes to the
    last version), and a one-line note. A model that does not validate or run is stored as a
    draft with its problems, not refused: read "problems", fix the model, step again.

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
    bounds the share of entities that waited over 15 at that resource (FIFO only), stored per
    replication as the KPI "<resource>/Share waiting over 15"; "kpi": "Total Wait" sums each
    entity's waits over every queue ("Share with total wait over 15"). Either is judged, sized and
    compared like any KPI; when it cannot be computed it stays not_verified and "tail_note" says why.

    A version has no report file by default (the answer is the light output); study_report
    writes it on request. With scenarios: "scenarios": {name: {"kpis": {goal KPI: {mean,
    ci_half_width}}, "goals": {goal id: met|not_met|not_verified}}}, and "scenario_error" or
    "scenarios_skipped" when a run failed or the cap cut some; "normalised" when an input was
    mapped. A scenario whose law checks warn carries "laws": {"passed", "partial"} and a partial
    "scenario <name>" check says why. A partial law: not valid, goals not_verified, why in place
    of its numbers. Censoring or initial bias only: a goal a low bias may flip is not_verified.
    """
    started = time.monotonic()
    if sum(x is not None for x in (layout, model, patch)) != 1:
        raise ToolError("E_BAD_ARGUMENT", "give one of `layout`, `model` and `patch`: a step runs a whole model",
                        "layout: the whole file; model: an open workspace model; patch: changes to the last "
                        "version. For record-only changes use study_update(changes=...)")
    if note is None:
        raise ToolError("E_BAD_ARGUMENT", "`note` is required", "send a one-line note saying what the step changes")
    folder, s = _open(study, write=True)
    if model is not None:
        layout = workspace().document(model).to_layout()
    elif patch is not None:
        layout = _patched(folder, s, patch)
    notes: list[str] = []
    if changes is not None:     # refused before anything runs, so a bad record never costs a step
        notes += _set(s, _merge(changes, {}))
    with _study_errors():
        layout, layout_notes = studyinput.normalise_layout(layout)
    notes += layout_notes
    if any(g.get("kpi") == TOTAL_WAIT and g.get("share_above") is not None for g in s["goals"])             and isinstance(layout, dict) and not (layout.get("simulation") or {}).get("entity_waits"):
        # D1232: only a run that asks keeps each entity's total wait; recorded in the version's
        # model, so a rerun of it reads the same column.
        layout = {**layout, "simulation": {**(layout.get("simulation") or {}), "entity_waits": True}}
        notes.append("simulation.entity_waits set to true: a goal reads each entity's Total Wait")
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
            with _stage(1, 3, "first pass"):
                digest = call("run_model", model=model, replications=n, seed=seed)
            # ponytail: same-seed rerun on the first version and every fifth halves the cost;
            # rerun every step if drift is ever seen.
            if "error" not in digest and len(s["versions"]) % 5 == 0:
                with _stage(1, 3, "first pass"):
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
                n = max(study_mod.replications_needed(status, FIRST_PASS, cap),
                        min(_same_base_replications(folder, s, layout), cap))
                if n > FIRST_PASS:
                    # Replication i uses seed + i, so the extra n - FIRST_PASS replications continue the
                    # first pass and the merge equals one run of n.
                    with _stage(2, 3, "more replications"):
                        more = call("run_model", model=model, replications=n - FIRST_PASS, seed=seed + FIRST_PASS)
                    if "error" in more:
                        problems, digest, rerun = [more["error"]], None, None
                    else:
                        res = workspace().results
                        res[model] = experiments._merge(first_results, res[model])
                        digest = {**_digest(res[model]), "model": model, "frames": sorted(res[model]),
                                  "laws": _run_laws(workspace().document(model).to_layout(), res[model])}
                        _tails(s, digest, res[model], layout)
                        status = study_mod.precision_status(s, digest)
            if digest is not None:
                with _stage(3, 3, "scenarios"):
                    scenarios, extra = _run_scenarios(layout, s, n, seed, workspace().results.get(model))
    checks = (step_checks(layout, digest, rerun, s, {"status": status, "n": n, "cap": cap}, first)
              + scenario_checks(scenarios) if digest is not None else [])
    _log(s, "study_step", started, study_mod.error_code(problems) if problems else None)
    run = {"replications": n, "seed": seed, "precision": status, "precision_note": study_mod.precision_sentence(n, status, cap)}
    if "scenario_error" in extra:   # marks the version, so next_step names it first (#1729)
        run["scenario_error"] = extra.pop("scenario_error")
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
    answer["options"] = _options(folder, s)
    if notes:
        answer["normalised"] = notes
    return answer


@tool(returns={
    "kind": str, "model": float, "formula": float, "rel_error": float, "passed": bool,
    "next_step": str, "options": list, "page_url": str, "room_url": str})
@_linked
def study_validate(study: str, kind: Literal["queue_formula"] = "queue_formula") -> dict:
    """Check the last model version against a textbook answer and record the validation.

    kind="queue_formula" applies when the model is one arrival stream (exponential interarrival
    times) feeding one FIFO resource: the engine runs the model 100 times longer than its own
    run length (warm-up 10% of that, 10 replications, seed 0), compares the mean wait in queue
    with M/M/c (Erlang C) or M/G/1 (Pollaczek-Khinchine) and records a known_answer validation
    that passes within 5% relative error. Any other model answers E_STUDY_NOT_APPLICABLE with the
    reason, so the person is asked for another validation.
    Example: study_validate(study="naga-studies/cranes").

    "model" and "formula" are the simulated and the formula's mean wait, "rel_error" their
    relative difference and "passed" whether it is within 5%.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    if not s["versions"]:
        raise ToolError("E_STUDY_NO_VERSION", "the study has no model version to validate",
                        "build a version with study_step first")
    layout = _read_layout(folder, s["versions"][-1]["id"])
    f = queue_formula(layout)
    if f is None:
        raise ToolError("E_STUDY_NOT_APPLICABLE", f"the queue formula does not apply: {why_not(layout)}",
                        "compare with data or an expert; the person must confirm it (the assistant's "
                        "own record reads \"reported, not checked\")")
    # ponytail: no time_limit in the model falls back to 1000; run through api (no logs) because the
    # 100x run would only fill the resource log.
    length = 100 * float((layout.get("simulation") or {}).get("time_limit") or 1000)
    ws = workspace()
    try:
        results = api.run_model(layout, replications=10, seed=0, run_length=length, warmup=0.1 * length,
                                record_logs=False, network=ws.network, confine_to=ws.root,
                                on_progress=report)     # D1262: the dock's Stop lands at a progress point
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
    # The engine's own write (D1234): not through _set, which marks what it writes as the assistant's.
    s["validation"] = s["validation"] + [{**entry, "source": "engine"}]
    _log(s, "study_validate", started)
    _save(folder, s)
    _page(folder, s)
    return {"kind": kind, "model": round(wait, 6), "formula": round(f["wq"], 6), "rel_error": round(rel, 6),
            "passed": passed, "next_step": _next(folder, s), "options": _options(folder, s)}


@tool(returns={"sufficiency": str, "page_url": str, "room_url": str})
@_linked
def study_accept(study: str, reason: str) -> dict:
    """Record that the person accepts the study as it stands. Call it only with the
    person's own words, after they have explicitly accepted; never on your own judgment. It
    changes the label, not the checks: open issues stay listed.
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


@tool(returns={
    "id": str, "kind": str, "verdict": str, "checks": list, "changes": list, "kpis": dict,
    "replications": int, "precision": dict, "page": str, "sufficiency": str, "next_step": str,
    "options": list, "page_url": str, "room_url": str})
@_linked
def study_revert(study: str, version: str, note: str, request_id: str | None = None) -> dict:
    """Go back to an earlier version: it is recorded again as a new version, so no history is
    lost. Use it when a later step made things worse.
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
    answer["options"] = _options(folder, s)
    return answer


@tool(returns={"path": str, "page_url": str, "room_url": str})
@_linked
def study_report(study: str, version: str | None = None) -> dict:
    """Write a version's full HTML report (KPIs with intervals, resources, charts) on request.
    Steps do not write it, to keep the default output light. It reruns the version's stored
    model with the step's stored replications and seed, so the numbers match the step.
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


@tool(returns={"files": list, "next_step": str, "page_url": str, "room_url": str})
@_linked
def study_package(study: str) -> dict:
    """Build the study's deliverable, the final files to hand over, in its package/ folder:
    the client page, the control room page, any current write-up and slides, an Excel
    workbook (goals, versions with KPIs, checklist, assumptions), the current model and
    RO-Crate metadata. Use it when the person is satisfied with the study.
    """
    started = time.monotonic()
    folder, s = _open(study, write=True)
    with _study_errors():
        names = studypackage.package(folder, s)
    _log(s, "study_package", started)
    _save(folder, s)
    return {"files": [f"{_sid(folder)}/{n}" for n in names],
            "next_step": "Give the person package/client.html (print it to PDF) and study.xlsx; "
                         "package/control-room.html is the modeller's page."}


# The parameters that say what a block does, as get_model_summary keeps them (S2-11).
_KEY_PARAMS = ("resource_name", "distribution", "dist_params", "mode", "probs", "conditions", "assignments",
               "stat_name", "kind", "expr", "max_arrivals", "batch_size", "capacity", "schedule")


def _parameters(layout: dict) -> str:
    """One line per block with its key parameters, per resource with its capacity, and per schedule
    (cut at 200 characters): what an assistant checks its model against, without get_model_json."""
    def short(value) -> str:
        text = json.dumps(value)
        return text if len(text) <= 200 else text[:200] + "..."
    lines = ["Parameters:"]
    for b in layout.get("blocks") or []:
        keep = {k: v for k, v in (b.get("params") or {}).items() if k in _KEY_PARAMS}
        if keep:
            lines.append(f"- {b.get('name')}: " + ", ".join(f"{k}={short(v)}" for k, v in keep.items()))
    for r in layout.get("resources") or []:
        lines.append(f"- resource {r.get('name')}: capacity={r.get('capacity')}"
                     + (f", capacity_schedule={r['capacity_schedule']}" if r.get("capacity_schedule") else ""))
    lines += [f"- schedule {sc.get('name')}: {short({k: v for k, v in sc.items() if k != 'name'})}"
              for sc in layout.get("schedules") or [] if isinstance(sc, dict)]
    return "\n".join(lines).encode("ascii", "replace").decode("ascii")


@tool(read_only=True, returns={
    "version": str, "format": str, "text": str, "image": dict, "page_url": str, "svg": str,
    "note": str, "room_url": str})
@_linked
def study_view(study: str, version: str | None = None, format: Literal["png", "svg", "text"] = "png") -> dict:
    """See a version's flowchart: built blocks, what this step changed and what of the agreed
    target is not built yet. Use it to check the model looks like the process.

    The text lists every block, state and link, then each block's key parameters, the resources
    and the schedules; "image" comes for png, "svg" for svg. Without matplotlib a png request
    answers with the text and a "note".
    """
    folder, s = _open(study)
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
    out = {"version": vid, "format": format, "text": flowchart.to_text(geom) + "\n" + _parameters(layout)}
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


@tool(read_only=True, returns={
    "study": str, "sufficiency": str, "goals_met": str, "versions": int, "page": str | None,
    "text": str, "page_url": str, "room_url": str})
@_linked
def study_show(study: str) -> dict:
    """Show the study's control room, its dashboard. In chats that support MCP Apps the page appears inline,
    with buttons to revert to a version or accept the study; elsewhere this is a short summary
    and the path of the page file.
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


@tool(read_only=True, returns={"html": str})
def study_page_html(study: str) -> dict:
    """The control room page as one HTML string. It exists for the in-chat page, which fetches
    it itself so the HTML never enters the conversation; assistants should call study_show instead.
    """
    folder, s = _open(study)
    return {"html": studypage.render(folder, s, study_id=_sid(folder))}
