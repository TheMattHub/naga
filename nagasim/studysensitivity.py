"""One-at-a-time sensitivity of a study version: which assumptions change the answer.
Works from the study folder alone (model.json, step.json: stored replications and seed)."""
from __future__ import annotations

import json
from pathlib import Path

from nagasim import experiments, study as study_mod, svgcharts
from nagasim.core.schema import ModelValidationError
from nagasim.study import StudyError
from nagasim.studychecks import goal_kpi
from nagasim.twin import write_json

MAX_FACTORS = 8


def factors(layout: dict) -> list[dict]:
    """Numeric inputs worth varying, at most MAX_FACTORS: arrival parameters, then service
    parameters, then resource capacities."""
    kinds = {b.get("name"): b.get("type") for b in layout.get("blocks") or []}
    groups: dict[int, list[dict]] = {0: [], 1: [], 2: []}
    for path, value in experiments.list_override_paths(layout):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts = path.split(".")
        if parts[0] == "blocks" and "dist_params" in parts:
            rank, what = (0, "arrival") if kinds.get(parts[1]) == "CREATE" else (1, "service")
            label = f"{what} {parts[-1]} of {parts[1]}"
        elif parts[0] == "resources" and parts[-1] == "capacity":
            rank, label = 2, f"capacity of {parts[1]}"
        else:
            continue
        groups[rank].append({"path": path, "value": value, "label": label})
    return (groups[0] + groups[1] + groups[2])[:MAX_FACTORS]


def _ends(f: dict, delta: float) -> tuple[float, float]:
    v = f["value"]
    if f["path"].endswith(".capacity"):
        return max(1, v - 1), v + 1
    return v * (1 - delta), v * (1 + delta)


def _requested(layout: dict, only: list[str]) -> tuple[list[dict], list[dict]]:
    """(factors, not varied) for the paths a caller named: any numeric override path is a factor
    (#1727: variables and DECIDE probabilities too, not only what `factors` would pick)."""
    known = {f["path"]: f for f in factors(layout)}
    numeric = {p: v for p, v in experiments.list_override_paths(layout)
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    found, skipped = [], []
    for path in dict.fromkeys(only):
        if path in numeric:
            found.append(known.get(path) or {"path": path, "value": numeric[path], "label": path})
        else:
            skipped.append({"path": path, "reason": "not a numeric input of this model"
                            if path not in dict(experiments.list_override_paths(layout))
                            else "not a number"})
    return found, skipped


def _problem(layout: dict, path: str, value) -> str | None:
    """Why the model with path=value cannot run, or None (checked before any replication)."""
    probe = dict(layout, scenarios=[{"name": "probe", "overrides": [{"path": path, "value": value}]}])
    try:
        experiments._scenario_layouts(probe, ["probe"], baseline=False)
    except ModelValidationError as exc:
        return "; ".join(p.removeprefix("scenario 'probe': ") for p in exc.problems)
    return None


def run(folder: Path, study: dict, version: str | None = None, only: list[str] | None = None,
        delta: float = 0.2, *, kpis) -> dict:
    """Each factor low and high (+-delta, capacities +-1, never below 1) against the baseline, with
    common random numbers. `kpis(results, layout)` answers {kpi: {"mean", "ci_half_width"}} for the
    goal KPIs of one scenario's results (the tool layer owns the digest and tail shares). Stores versions/<id>/sensitivity.json and answers its content."""
    vid = study_mod.version_id(study, version)
    vdir = Path(folder) / "versions" / vid
    if not 0 < delta < 1:
        raise StudyError("E_STUDY_BAD_VALUE", "delta must be between 0 and 1", "e.g. 0.2")
    layout = json.loads((vdir / "model.json").read_text(encoding="utf-8"))
    step = json.loads((vdir / "step.json").read_text(encoding="utf-8"))
    n, seed = step.get("replications") or study_mod.FIRST_PASS, step.get("seed") or 0
    found, not_varied = (factors(layout), []) if only is None else _requested(layout, only)
    goals = [g for g in study["goals"] if g.get("kpi")]
    scen, ends, notes = [], {}, []
    moved = []
    for f in found:
        ends[f["path"]] = lo, hi = _ends(f, delta)
        if lo == hi == f["value"]:          # 0 +-20% is 0: not varied, rather than a swing of 0
            not_varied.append({"path": f["path"], "reason": f"its value {f['value']!r} does not move by +-{delta:g}"})
            continue
        ran = []
        for end, value in (("low", lo), ("high", hi)):
            # An end the model cannot run with (a capacity under its schedule's maximum, a
            # probability over 1) is skipped and that side reads as the baseline (#1727).
            why = _problem(layout, f["path"], value) if value != f["value"] else None
            if why:
                notes.append(f"{f['path']} {end} {value} not run: {why}")
                continue
            ran.append({"name": f"{f['path']}={end}", "overrides": [{"path": f["path"], "value": value}]})
        if not ran:
            not_varied.append({"path": f["path"], "reason": "neither end can be run"})
            continue
        scen += ran
        moved.append(f)
    found = moved
    res = experiments.run_scenarios(dict(layout, scenarios=scen), [s["name"] for s in scen],
                                    replications=n, seed=seed, baseline=True)
    digests = {k: kpis(v, layout) for k, v in res.results.items()}
    for f in found:
        for end in ("low", "high"):
            digests.setdefault(f"{f['path']}={end}", digests[experiments.BASELINE])

    def point(entries: dict, g: dict) -> dict:
        e = entries.get(goal_kpi(g))
        mean = e["mean"] if e else None
        return {"mean": mean, "ci_half_width": e.get("ci_half_width") if e else None,
                "verdict": study_mod.judge(g, mean)}

    rows = []
    for f in found:
        lo, hi = ends[f["path"]]
        row = {"path": f["path"], "label": f["label"], "value": f["value"], "low": lo, "high": hi,
               "kpis": {}, "changes_answer": False}
        for g in goals:
            a, b = point(digests[f"{f['path']}=low"], g), point(digests[f"{f['path']}=high"], g)
            base = point(digests[experiments.BASELINE], g)["verdict"]
            swing = abs(b["mean"] - a["mean"]) if a["mean"] is not None and b["mean"] is not None else None
            flips = a["verdict"] != base or b["verdict"] != base
            row["kpis"][goal_kpi(g)] = {"low": a, "high": b, "swing": swing, "flips": flips}
            row["changes_answer"] |= flips
        rows.append(row)
    charts = {}
    for k in sorted({goal_kpi(g) for g in goals}):
        ranked = sorted(rows, key=lambda r: -(r["kpis"][k]["swing"] or 0))
        charts[k] = svgcharts.bar_chart(f"Swing of {k} by assumption", [r["label"] for r in ranked],
                                        [r["kpis"][k]["swing"] or 0.0 for r in ranked], "swing")
    out = {"version": vid, "delta": delta, "replications": n, "seed": seed, "factors": rows, "charts": charts,
           "changes_answer": [r["path"] for r in rows if r["changes_answer"]],
           "not_varied": not_varied, "notes": notes}
    write_json(vdir / "sensitivity.json", out)
    return out
