"""study_sensitivity: which assumptions change the answer (nagasim.studysensitivity)."""
from __future__ import annotations

import time

from nagasim import studysensitivity
from nagasim.studychecks import goal_kpi, kpi_entry

from .core import _digest, _document_args, tool
from .study import _ID, _log, _open, _save, _sid, _study_errors, _tails

_document_args("study_sensitivity", study=_ID,
               version="Version id, e.g. v002; omit for the last.",
               factors="Paths to vary (as in an earlier answer); omit for the most relevant, at most 8.",
               delta="Relative change, 0.2 = +-20%; capacities change by +-1.")


@tool()
def study_sensitivity(study: str, version: str | None = None, factors: list[str] | None = None,
                      delta: float = 0.2) -> dict:
    """Vary one assumption at a time (arrival and service parameters, capacities) around a version,
    with common random numbers and the version's stored replications and seed, and rank them by
    how much each moves the goal KPIs. Answers {"file", "ranked": [{path, label, swing}],
    "changes_answer": [paths where a goal verdict flips]}; the full table and a swing chart per
    goal KPI are in the file. Rate parameters are varied as rates (a higher rate is a shorter time)."""
    started = time.monotonic()
    folder, s = _open(study)

    def kpis(results, layout):
        digest = _digest(results)
        _tails(s, digest, results, layout)      # tail-goal shares, as the study step computes them
        found = {goal_kpi(g): kpi_entry(digest, goal_kpi(g)) for g in s["goals"] if g.get("kpi")}
        return {k: v for k, v in found.items() if v is not None}

    with _study_errors():
        out = studysensitivity.run(folder, s, version, factors, delta, kpis=kpis)
    _log(s, "study_sensitivity", started)
    _save(folder, s)
    ranked = sorted(({"path": r["path"], "label": r["label"],
                      "swing": max((k["swing"] or 0 for k in r["kpis"].values()), default=0.0)}
                     for r in out["factors"]), key=lambda r: -r["swing"])
    return {"file": f"{_sid(folder)}/versions/{out['version']}/sensitivity.json", "ranked": ranked,
            "changes_answer": out["changes_answer"]}
