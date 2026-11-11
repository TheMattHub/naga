"""`naga study rerun FOLDER`: every version of a study folder re-run from its stored model, then the
reproduce steps the folder declares in `reproduce.json`, if any.

Versions: reads only the folder and writes nothing. A version reproduces when each KPI stored in its
step.json matches a fresh run (same replications and seed) within a relative 1e-9.

Steps (P9, D1170): ordered commands, each a fresh process under a time limit, cwd the folder; the
run stops at the first failed required step. Every file a step's `outputs` globs name is hashed
before the run and after its step, so "regenerates identically" is one command on a clean checkout.
Steps run only when a PERSON asks for them: `--steps` or `--step NAME` here (D1195, #1737), or
Reproduce in the editor after its list. Without either the command reruns the versions and lists the
steps it did not run, since a folder received from someone else must not start a program unasked.
Loading the manifest never runs anything (permission is the caller's, never the file's)."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path

from . import api
from . import study as study_mod
from .digest import digest as _digest
from .netguard import GuardError, confine_path
from .studychecks import kpi_mean

TOLERANCE = 1e-9
MANIFEST = "reproduce.json"
DEFAULT_TIMEOUT = 600
_TAIL = 20      # output lines kept from a captured step, enough to see why it failed


def rerun_version(folder: Path, vid: str) -> tuple[bool, str]:
    """(reproduced, one ASCII detail string) for one version."""
    vdir = folder / "versions" / vid
    step = json.loads((vdir / "step.json").read_text(encoding="utf-8"))
    layout = json.loads((vdir / "model.json").read_text(encoding="utf-8"))
    n = step.get("replications") or 10      # old steps stored none: the first-pass count
    seed = step.get("seed", 0)
    digest = _digest(api.run_model(layout, replications=n, seed=seed, base_dir=folder))
    bad = []
    for kpi, stored in sorted((step.get("kpis") or {}).items()):
        now = kpi_mean(digest, kpi)
        if now is None or not math.isclose(now, stored, rel_tol=TOLERANCE, abs_tol=1e-12):
            bad.append(f"{kpi}: stored {stored!r}, now {now!r}")
    return (not bad), (f"{len(step.get('kpis') or {})} KPIs match" if not bad else "; ".join(bad))


# --- reproduce steps ---------------------------------------------------------------------------

def _bad(message: str) -> study_mod.StudyError:
    return study_mod.StudyError("E_REPRODUCE_MANIFEST", f"{MANIFEST}: {message}",
                                f"Fix {MANIFEST} in the study folder; nothing was run.")


def load_manifest(folder: Path) -> dict | None:
    """The folder's `reproduce.json` checked and defaulted, or None when there is none. Pure reading:
    no step runs here."""
    path = Path(folder) / MANIFEST
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _bad(f"cannot be read ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise _bad("is not an object")
    steps, names = [], set()
    for i, s in enumerate(data.get("steps") or []):
        if not isinstance(s, dict):
            raise _bad(f"step {i + 1} is not an object")
        kinds = [k for k in ("script", "module", "command") if s.get(k)]
        if len(kinds) != 1:
            raise _bad(f"step {i + 1} needs exactly one of script, module or command")
        if s.get("command") is not None and not (isinstance(s["command"], list) and s["command"]
                                                 and all(isinstance(a, str) for a in s["command"])):
            # a list, never a shell string: no shell parses what the file says
            raise _bad(f"step {i + 1}: command must be a list of strings")
        if kinds[0] != "command" and not isinstance(s[kinds[0]], str):
            raise _bad(f"step {i + 1}: {kinds[0]} must be a string")
        args = s.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _bad(f"step {i + 1}: args must be a list of strings")
        first = s["command"][0] if kinds[0] == "command" else s[kinds[0]]
        name = str(s.get("name") or Path(first).stem)
        if name in names:
            raise _bad(f"two steps are named {name!r}")
        names.add(name)
        timeout = s.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise _bad(f"step {name!r}: timeout must be a positive number of seconds")
        outputs = s.get("outputs") or []
        if not isinstance(outputs, list) or not all(isinstance(o, str) for o in outputs):
            raise _bad(f"step {name!r}: outputs must be glob strings")
        steps.append({"name": name, "kind": kinds[0], "target": s[kinds[0]], "args": list(args),
                      "timeout": float(timeout), "optional": bool(s.get("optional")),
                      "outputs": list(outputs)})
    data["steps"] = steps
    return data


def step_argv(folder: Path, step: dict) -> list[str]:
    """The process a step starts. A script must sit inside the folder (it is the package's own)."""
    if step["kind"] == "script":
        return [sys.executable, str(confine_path(step["target"], folder)), *step["args"]]
    if step["kind"] == "module":
        return [sys.executable, "-m", str(step["target"]), *step["args"]]
    return [*step["target"], *step["args"]]


def describe(step: dict) -> str:
    """The step's command as one line, for a person to read before running it."""
    head = {"script": ["python", step["target"]], "module": ["python", "-m", step["target"]]}
    return " ".join(head.get(step["kind"], step["target"]) + step["args"])


def _hashes(folder: Path, globs: list[str]) -> dict[str, str]:
    """sha256 of every file the globs match, keyed by its path relative to the folder."""
    out = {}
    for pattern in globs:
        for p in sorted(folder.glob(pattern)):
            try:
                p = confine_path(p, folder)
            except GuardError:          # a glob such as "../x" or a link out: not ours to read
                continue
            if p.is_file():
                out[p.relative_to(folder.resolve()).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run_steps(folder: Path, manifest: dict, only: list[str] | None = None, capture: bool = False,
              on_start=None, on_step=None, should_stop=None) -> list[dict]:
    """Run the manifest's steps in order; one result per step (`not_run` after a stop).

    Each result: name, optional, status (ok, failed, timeout, skipped, not_run), seconds, code, and
    the step's outputs compared with the files there before the run: changed, added, removed, same
    (a count). `capture` keeps the last output lines in `tail` (the editor) instead of passing them
    through (the console). `on_start(step)` and `on_step(result)` bracket each step that runs;
    `should_stop()` is asked between steps (a running step ends at its time limit)."""
    folder = Path(folder).resolve()
    steps = [s for s in manifest["steps"] if not only or s["name"] in only]
    # every snapshot before the first step: a later step must not see an earlier one's writes as "before"
    before = {s["name"]: _hashes(folder, s["outputs"]) for s in steps}
    results, stopped = [], False
    for step in steps:
        res = {"name": step["name"], "optional": step["optional"], "status": "not_run", "seconds": 0.0,
               "code": None, "changed": [], "added": [], "removed": [], "same": 0, "tail": ""}
        results.append(res)
        if stopped or (should_stop and should_stop()):
            stopped = True
            continue
        if on_start:
            on_start(step)
        try:
            argv = step_argv(folder, step)
            missing = step["kind"] == "script" and not Path(argv[1]).is_file()
        except GuardError as exc:
            argv, missing = None, str(exc)
        if argv is None or missing:
            # an optional step not written yet is skipped, as a study's own reproduce-all script would
            res.update(status="skipped" if step["optional"] else "failed",
                       tail=missing if isinstance(missing, str) else f"{step['target']} not found")
        else:
            t0 = time.perf_counter()
            try:
                proc = subprocess.run(argv, cwd=folder, timeout=step["timeout"], text=True,
                                      encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE if capture else None,
                                      stderr=subprocess.STDOUT if capture else None)
                res.update(code=proc.returncode, status="ok" if proc.returncode == 0 else "failed",
                           tail="\n".join((proc.stdout or "").splitlines()[-_TAIL:]))
            except subprocess.TimeoutExpired:
                res["status"] = "timeout"
                res["tail"] = f"over its {step['timeout']:g} s limit"
            except OSError as exc:          # the command does not exist or cannot start
                res.update(status="failed", tail=f"{type(exc).__name__}: {exc}")
            res["seconds"] = round(time.perf_counter() - t0, 3)
            was, now = before[step["name"]], _hashes(folder, step["outputs"])
            res["changed"] = sorted(k for k in now if k in was and now[k] != was[k])
            res["added"] = sorted(k for k in now if k not in was)
            res["removed"] = sorted(k for k in was if k not in now)
            res["same"] = sum(1 for k in now if was.get(k) == now[k])
        if res["status"] not in ("ok", "skipped") and not step["optional"]:
            stopped = True
        if on_step:
            on_step(res)
    return results


def steps_ok(results: list[dict]) -> bool:
    """True when every required step ran clean and no output differs from before the run."""
    return (all(r["status"] in ("ok", "skipped") or r["optional"] for r in results)
            and not any(r["changed"] or r["removed"] for r in results))


def step_line(r: dict) -> str:
    out = (f"{len(r['changed'])} changed, {len(r['added'])} new, {len(r['removed'])} gone, "
           f"{r['same']} identical")
    return f"{r['name']}: {r['status']} ({r['seconds']:.1f} s; outputs {out})"


def _print_steps(folder: Path, manifest: dict, only: list[str] | None) -> int:
    known = [s["name"] for s in manifest["steps"]]
    unknown = [n for n in only or [] if n not in known]
    if unknown:
        sys.stderr.write(f"naga study rerun: no step named {', '.join(unknown)} (steps: {', '.join(known)})\n")
        return 2

    def show(r):
        print(step_line(r), flush=True)
        for key in ("changed", "removed"):
            for name in r[key]:
                print(f"    {key}: {name}")
        if r["status"] not in ("ok", "skipped") and r["tail"]:
            print(f"    {r['tail']}")
    results = run_steps(folder, manifest, only, on_step=show,
                        on_start=lambda s: print(f"== {s['name']}: {describe(s)}", flush=True))
    for r in results:
        if r["status"] == "not_run":
            print(f"{r['name']}: not run (an earlier step failed)")
    return 0 if steps_ok(results) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="naga study", description="Checks on a study folder, and its slides as a PDF.")
    sub = p.add_subparsers(dest="action", required=True)
    r = sub.add_parser("rerun", help="re-run every version and compare the stored KPIs; with --steps, then run "
                                     "the reproduce.json steps and compare their outputs")
    r.add_argument("folder", help="the study folder (holds study.json and/or reproduce.json)")
    r.add_argument("--steps", action="store_true",
                   help="also run the reproduce.json steps: each starts a program on this machine, as you, "
                        "so read them first (without this flag they are only listed)")
    r.add_argument("--step", action="append", metavar="NAME",
                   help="run only this reproduce step (repeatable); the versions are then not rerun")
    n = sub.add_parser("numbers", help="check every tagged number of the write-ups against its source file")
    n.add_argument("folder", help="the study folder (holds reproduce.json)")
    d = sub.add_parser("deck", help="the slides as a PDF, printed by this machine's Chrome or Edge")
    d.add_argument("folder", help="the study folder (holds study.json)")
    d.add_argument("--browser", help="the browser to print with (default: the first Chrome, Edge or Chromium found)")
    args = p.parse_args(argv)
    folder = Path(args.folder)
    if args.action == "deck":
        from .studydeck import run
        return run(folder, args.browser)
    try:
        manifest = load_manifest(folder)
    except study_mod.StudyError as exc:
        sys.stderr.write(f"naga study {args.action}: {exc}\n")
        return 2
    if args.action == "numbers":
        from .studynumbers import main_numbers
        return main_numbers(folder, manifest)
    has_study = (folder / "study.json").is_file()
    if not has_study and manifest is None:
        sys.stderr.write(f"naga study rerun: no study.json or {MANIFEST} in {folder.name}\n")
        return 2
    failed = 0
    if has_study and not args.step:
        try:
            study = study_mod.load(folder)
        except study_mod.StudyError as exc:
            sys.stderr.write(f"naga study rerun: {exc}\n")
            return 2
        for v in study["versions"]:
            try:
                ok, text = rerun_version(folder, v["id"])
            except Exception as exc:    # a model that no longer loads or runs is a mismatch, not a crash
                ok, text = False, f"{type(exc).__name__}: {exc}"
            failed += not ok
            print(f"{v['id']}: {'ok' if ok else 'MISMATCH'} ({text})")
    if manifest and manifest["steps"] and not (args.steps or args.step):
        # D1195: listed, never started, unless the person asks with --steps or --step
        print("reproduce steps not run (each starts a program on this machine, as you; read them, then "
              "add --steps, or --step NAME):")
        for s in manifest["steps"]:
            print(f"  {s['name']}: {describe(s)}")
        if not has_study:
            return 2        # nothing was checked
    elif manifest and manifest["steps"]:
        code = _print_steps(folder, manifest, args.step)
        if code == 2:
            return 2
        failed += code
    return 1 if failed else 0
