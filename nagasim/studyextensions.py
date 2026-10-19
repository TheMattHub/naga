"""Candidate extension modules for a study: layout transforms (shifts, failures, an arrival peak)
that a study can add to its model. Each is a pure function `apply(layout, **params) -> layout`;
`candidates` offers only the ones that validate, run briefly and pass the law checks.
The tool never steps the study itself: the assistant reviews the layout and sends it to study_step."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from nagasim import api
from nagasim import study as study_mod
from nagasim.studychecks import goal_kpi, kpi_mean, step_checks


def _horizon(layout: dict) -> float:
    # ponytail: a model without time_limit falls back to 1000, as the study's own queue check does.
    return float((layout.get("simulation") or {}).get("time_limit") or 1000)


def _resource(layout: dict, name) -> dict:
    res = layout.get("resources") or []
    if name is None:
        if not res:
            raise ValueError("the model has no resource")
        return res[0]
    for r in res:
        if r.get("name") == name:
            return r
    raise ValueError(f"no resource named {name}")


def _add(layout: dict, key: str, item: dict) -> None:
    layout[key] = [x for x in layout.get(key) or [] if x.get("name") != item["name"]] + [item]


def _fractions(start: float, length: float) -> None:
    if not (0 < start and 0 < length and start + length < 1):
        raise ValueError("the start and the length are fractions of the run, together under 1")


def shifts(layout: dict, resource=None, break_start: float = 0.45, break_length: float = 0.1) -> dict:
    """The resource works the whole run except one break: capacity 0 for `break_length` of the run
    starting at `break_start` (both fractions of the run length)."""
    out = copy.deepcopy(layout)
    r, t = _resource(out, resource), _horizon(out)
    _fractions(break_start, break_length)
    cap = int(r.get("capacity") or 1)
    name = f"{r['name']}_shifts"
    _add(out, "schedules", {"name": name, "kind": "capacity", "points": [
        [break_start * t, cap], [break_length * t, 0], [t, cap]]})
    r["capacity_schedule"] = name
    return out


def failures(layout: dict, resource=None, mtbf: float | None = None, mttr: float | None = None) -> dict:
    """The resource fails after an exponential uptime of mean `mtbf` and is repaired after an
    exponential downtime of mean `mttr` (default: a fifth and a fiftieth of the run)."""
    out = copy.deepcopy(layout)
    r, t = _resource(out, resource), _horizon(out)
    mtbf, mttr = mtbf or t / 5, mttr or t / 50
    if mtbf <= 0 or mttr <= 0:
        raise ValueError("mtbf and mttr must be positive")
    _add(out, "failures", {"name": f"{r['name']}_failure", "resources": [r["name"]],
                           "uptime": {"distribution": "Exponential", "dist_params": {"rate": 1 / mtbf}},
                           "downtime": {"distribution": "Exponential", "dist_params": {"rate": 1 / mttr}}})
    return out


def arrival_peak(layout: dict, factor: float = 2.0, peak_start: float = 0.4, peak_length: float = 0.2) -> dict:
    """The first Exponential arrival stream runs `factor` times faster for `peak_length` of the run
    from `peak_start` (fractions of the run length), at its normal rate before and after."""
    out = copy.deepcopy(layout)
    t = _horizon(out)
    if not factor > 0:
        raise ValueError("factor must be positive")
    _fractions(peak_start, peak_length)
    for b in out.get("blocks") or []:
        p = b.get("params") or {}
        if b.get("type") == "CREATE" and p.get("distribution") == "Exponential" and (p.get("dist_params") or {}).get("rate"):
            rate = float(p["dist_params"]["rate"])
            _add(out, "schedules", {"name": "arrival_peak", "kind": "arrival", "points": [
                [peak_start * t, rate], [peak_length * t, factor * rate], [t, rate]]})
            p["arrival_mode"], p["schedule_name"] = "Schedule", "arrival_peak"
            return out
    raise ValueError("needs an arrival stream with an Exponential interarrival time")


MODULES = {
    "shifts": {"fn": shifts, "label": "Shifts with a break",
               "purpose": "the resource stops for one break instead of working without pause", "effort": "one step"},
    "failures": {"fn": failures, "label": "Resource failures",
                 "purpose": "the resource breaks down now and then (MTBF and MTTR)", "effort": "one step"},
    "arrival_peak": {"fn": arrival_peak, "label": "Arrival peak",
                     "purpose": "arrivals rush for part of the run instead of coming at one steady rate",
                     "effort": "one step"},
}


def apply(name: str, layout: dict, **params) -> dict:
    """The layout with module `name` added; ValueError when it does not fit or `params` are wrong."""
    if name not in MODULES:
        raise ValueError(f"unknown module {name}; known: {', '.join(MODULES)}")
    try:
        return MODULES[name]["fn"](layout, **params)
    except TypeError as exc:
        raise ValueError(str(exc)) from None


def _short_run(layout: dict) -> dict:
    from nagasim.digest import digest as _digest
    return _digest(api.run_model(layout, replications=1, seed=0, record_logs=False))


def candidates(folder, study: dict | None = None) -> list[dict]:
    """The modules that validate, run one short replication on the last version and pass the law
    checks, each with the goal KPIs' short-run values against the base ("indicative")."""
    folder = Path(folder)
    study = study if study is not None else study_mod.load(folder)
    if not study["versions"]:
        return []
    path = folder / "versions" / study["versions"][-1]["id"] / "model.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    kpis = list(dict.fromkeys(k for k in (goal_kpi(g) for g in study["goals"] if g.get("kpi")) if k))
    try:
        base = _short_run(layout)
    except Exception:
        logging.getLogger(__name__).warning("extensions: the base model did not run", exc_info=True)
        return []
    # goal and assumption ranges are findings about the answer, not defects of the module
    laws_only = {**study, "goals": [], "assumptions": []}
    out = []
    for name, m in MODULES.items():
        try:
            new = apply(name, layout)
            digest = _short_run(new)
            if any(c["verdict"] == "failed" for c in step_checks(new, digest, None, laws_only)):
                continue
        except Exception:       # a module that does not fit, validate or run is simply not offered
            logging.getLogger(__name__).info("extension %s not offered", name, exc_info=True)
            continue
        out.append({"name": name, "label": m["label"], "purpose": m["purpose"], "effort": m["effort"],
                    "indicative": {k: {"base": kpi_mean(base, k), "with_module": kpi_mean(digest, k)}
                                   for k in kpis}})
    return out
