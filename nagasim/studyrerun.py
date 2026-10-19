"""`naga study rerun FOLDER`: every version of a study folder re-run from its stored model.

Reads only the folder and writes nothing. A version reproduces when each KPI stored in its
step.json matches a fresh run (same replications and seed) within a relative 1e-9."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from . import api
from . import study as study_mod
from .agent_tools.core import _digest
from .studychecks import kpi_mean

TOLERANCE = 1e-9


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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="naga study", description="Checks on a study folder.")
    sub = p.add_subparsers(dest="action", required=True)
    r = sub.add_parser("rerun", help="re-run every version and compare the stored KPIs")
    r.add_argument("folder", help="the study folder (holds study.json)")
    folder = Path(p.parse_args(argv).folder)
    try:
        study = study_mod.load(folder)
    except study_mod.StudyError as exc:
        sys.stderr.write(f"naga study rerun: {exc}\n")
        return 2
    failed = 0
    for v in study["versions"]:
        try:
            ok, text = rerun_version(folder, v["id"])
        except Exception as exc:    # a model that no longer loads or runs is a mismatch, not a crash
            ok, text = False, f"{type(exc).__name__}: {exc}"
        failed += not ok
        print(f"{v['id']}: {'ok' if ok else 'MISMATCH'} ({text})")
    return 1 if failed else 0
