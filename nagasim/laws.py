"""Operational-law checks and statistical guardrails over one run's results (D684, D685).

`checks(layout, results)` turns the frames `api.run_model` returns into named verdicts,
`{"name", "verdict": "passed"|"partial", "why", "law"}`. A law that does not hold is a warning
("partial"), never a failure: saturation can be the point of a model. A law whose inputs the
model does not make computable (a service time that is an expression, a resource shared by a set,
balking) is left out, never guessed. At most one warning per resource; passed checks are only
counted by `summary`, which is what the `run_model` digest carries.

The laws: utilisation (measured vs seize rate x declared mean service / capacity), offered load
(arrival rate x mean service / capacity, >= 1 is unstable), Little per resource (time-average
queue vs every wait in the window / its length), Little for the system (`Avg WIP` vs throughput x
mean time in system), flow conservation (created = completed + still inside, run with no
warm-up). The guardrails: censoring (over 5% of created entities still inside at the end) and
initial bias (no warm-up, while MSER-5 finds one that moves the mean time in system).
"""
from __future__ import annotations

import numpy as np

from nagasim.core.distributions import distribution_mean
from nagasim.core.stats import ci95
from nagasim.core.block_spec import DIST_PARAMS
from nagasim.core.quantities import field_factor
from nagasim.core.schema import prepare
from nagasim.core.timeunits import units_of

_DIST_SPEC = next(s for s in DIST_PARAMS if s.name == "dist_params")   # its unit is `time_unit`

REL = 0.05          # relative band, and the whole band for one replication
FLOOR = 0.01        # absolute floor, in the law's own unit (a fraction, entities)
CENSORED_SHARE = 0.05
BIAS_SHARE = 0.05
_COUNT_CHANGERS = {"BATCH", "MATCH", "SEPARATE", "DUPLICATE"}


def _check(name: str, verdict: str, why: str, law: str) -> dict:
    return {"name": name, "verdict": verdict, "why": why, "law": law}


def _close(measured, predicted) -> tuple[bool, float, float, float]:
    """(holds, mean measured, mean predicted, tolerance): paired over replications, tolerance
    max(2 x CI half-width of the difference, REL x |predicted|, FLOOR); one replication has no
    half-width, so the fixed band alone."""
    m, p = np.asarray(measured, dtype=float), np.asarray(predicted, dtype=float)
    stat = ci95(m - p)
    hw = stat["ub"] - stat["mean"] if np.isfinite(stat["ub"]) else 0.0
    tol = max(2 * hw, REL * abs(float(p.mean())), FLOOR)
    return abs(stat["mean"]) <= tol, float(m.mean()), float(p.mean()), tol


def _system(results) -> dict:
    """{metric: {run: value}} of the system frame."""
    frame = results.get("system")
    if frame is None or frame.empty:
        return {}
    out: dict[str, dict] = {}
    for metric, value, run in zip(frame["Metric"], frame["Value"], frame["Run"]):
        out.setdefault(str(metric), {})[run] = value
    return out


def _numbers(by_run: dict) -> np.ndarray:
    return np.asarray([float(by_run[r]) for r in sorted(by_run)], dtype=float)


def _strings(node):
    """Every string value inside *node* (dict values and list items, recursively)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _service_mean(layout: dict, name: str) -> float | None:
    """The declared mean service time of resource *name*: only when the model names it exactly
    twice (its definition and one PROCESS "Seize Delay Release" block with a plain numeric
    distribution, one unit, no setup, preemption or worker), in the model's time unit. Else None."""
    if (sum(s == name for s in _strings(layout)) != 2
            or not any(r.get("name") == name for r in layout.get("resources") or [])):
        return None             # a set, or a resource something else uses too
    blocks = [b for b in layout.get("blocks") or [] if (b.get("params") or {}).get("resource_name") == name]
    if len(blocks) != 1 or blocks[0].get("type") != "PROCESS":
        return None
    p = blocks[0].get("params") or {}
    if (p.get("action", "Seize Delay Release") != "Seize Delay Release" or p.get("quantity", 1) != 1
            or p.get("setup") or p.get("preempt") or p.get("worker_fleet")):
        return None
    params = p.get("dist_params") or {}
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in params.values()):
        return None
    mean = distribution_mean(p.get("distribution", "Constant"), params)
    try:
        return None if mean is None else mean * field_factor(
            _DIST_SPEC, p, units_of(layout.get("simulation")))
    except ValueError:
        return None


def _little_ok(layout: dict, name: str) -> bool:
    """Waits and queue describe the same requests: a plain resource (not in a set), every block
    seizing it asks one unit and no request leaves the queue unserved (balking, reneging,
    preemption)."""
    if not any(r.get("name") == name for r in layout.get("resources") or []):
        return False
    if any(name in (s.get("members") or []) for s in layout.get("resource_sets") or []):
        return False
    users = [b.get("params") or {} for b in layout.get("blocks") or []
             if (b.get("params") or {}).get("resource_name") == name]
    return bool(users) and all(p.get("quantity", 1) == 1 and not p.get("balk_if")
                               and not p.get("renege_after") and not p.get("preempt") for p in users)


def _resource_checks(layout: dict, results, span: dict) -> list[dict]:
    frame = results.get("resource_summary")
    if frame is None or frame.empty or "Queue Length at End" not in frame:
        return []
    out = []
    for name, rows in frame.groupby("Resource", sort=False):
        rows = rows.sort_values("Run")
        t = np.asarray([float(span.get(r, 0)) for r in rows["Run"]])
        if not np.all(t > 0):
            continue
        seizes = rows["Total Seizes"].to_numpy(float)
        waiting = rows["Queue Length at End"].to_numpy(float)
        capacity = rows["Scheduled Capacity Avg"].to_numpy(float)
        found = None
        mean_s = _service_mean(layout, name)
        if mean_s is not None and np.all(capacity > 0):
            offered = (seizes + waiting) / t * mean_s / capacity
            if offered.mean() >= 1:
                found = _check(f"offered load: {name}", "partial",
                               f"offered load {offered.mean():.2f} >= 1 on {name}: unstable by design, "
                               "the queue grows for the whole run", "offered load")
            else:
                ok, m, p, tol = _close(rows["Usage % (snapshot)"].to_numpy(float) / 100,
                                       seizes / t * mean_s / capacity)
                verdict = "passed" if ok else "partial"
                found = _check(f"utilisation: {name}", verdict,
                               f"utilisation {100 * m:.1f}% vs {100 * p:.1f}% from seize rate x mean "
                               f"service / capacity" + ("" if ok else
                               " (held longer than its service: blocking, setups or a unit mistake)"),
                               "utilisation")
            out.append(found)
        if (found is None or found["verdict"] == "passed") and _little_ok(layout, name):
            lq = rows["Queue Length Avg"].to_numpy(float)
            ok, m, p, tol = _close(lq, (seizes + waiting) * rows["Time in Queue Avg (incl. waiting)"]
                                   .to_numpy(float) / t)
            out.append(_check(f"little (resource): {name}", "passed" if ok else "partial",
                              f"queue {m:.3g} vs arrival rate x mean wait {p:.3g}", "little (resource)"))
    return out


def _inside(results, runs: int) -> np.ndarray | None:
    """Per replication, entities created in the window still inside at the end (not batched),
    from the entity log; None when the log was not kept."""
    log = results.get("entity_log")
    if log is None or log.empty or "Run" not in log:
        return None
    live = log[log["Exited At"].isna() & log["In Batch"].isna()]
    counts = live.groupby("Run").size()
    keys = sorted(log["Run"].unique())
    return np.asarray([counts.get(k, 0) for k in keys], dtype=float) if len(keys) == runs else None


def _initial_bias(results) -> dict | None:
    from nagasim import experiments      # experiments imports a lot; only this guardrail needs it
    try:
        s = experiments.suggest_warmup(results)
    except (ValueError, KeyError):
        return None
    if not s.reliable or s.warmup <= 0:
        return None
    log = results["entity_log"].dropna(subset=["Throughput Time"])
    full = log["Throughput Time"].mean()
    later = log.loc[log["Exited At"] >= s.warmup, "Throughput Time"].mean()
    if not (np.isfinite(full) and np.isfinite(later) and later > 0):
        return None
    shift = abs(full - later) / later
    if shift <= BIAS_SHARE:
        return _check("initial bias", "passed", f"no warm-up; MSER-5 suggests {s.warmup:.4g}, which moves "
                      f"the mean time in system by {100 * shift:.1f}%", "initial bias")
    return _check("initial bias", "partial",
                  f"the run starts empty with no warm-up: its first {s.warmup:.4g} time units move the "
                  f"mean time in system by {100 * shift:.0f}%; set the warm-up to about {s.warmup:.4g}",
                  "initial bias")


def checks(layout: dict, results: dict) -> list[dict]:
    """Every computable law and guardrail for one run (see the module docstring). *layout* is the
    model as run (its `simulation.warmup` is the one the run used); [] when there is nothing to
    check (no entities, no resources)."""
    if not results:
        return []
    layout = prepare(layout)[0]         # as run: migrated, submodels flattened (#762); on a fatal
                                        # error only migrated, so its resources find no service
    system = _system(results)
    if "Simulation Time" not in system:
        return []
    out = _resource_checks(layout, results, system["Simulation Time"])
    created = _numbers(system.get("Total Entities Created", {}))
    if created.size == 0 or created.sum() <= 0:
        return out
    completed = _numbers(system["Total Entities Completed"])
    inside = _inside(results, created.size)
    warmup = float((layout.get("simulation") or {}).get("warmup", 0) or 0)

    if inside is not None and warmup == 0:
        lost = created - completed - inside
        out.append(_check("flow conservation", "passed" if not np.any(lost) else "partial",
                          f"{created.mean():.0f} created = {completed.mean():.0f} completed + "
                          f"{inside.mean():.0f} inside" if not np.any(lost) else
                          f"{created.mean():.0f} created but {completed.mean():.0f} completed and "
                          f"{inside.mean():.0f} inside: {lost.mean():.0f} unaccounted for", "flow conservation"))

    still = inside if inside is not None else np.maximum(created - completed, 0)
    share = float(still.sum() / created.sum())
    censored = share > CENSORED_SHARE
    out.append(_check("censoring", "partial" if censored else "passed",
                      f"{100 * share:.0f}% of created entities are still inside at the end: time-based "
                      "KPIs are biased low (unstable system or run too short)" if censored else
                      f"{100 * share:.1f}% of created entities still inside at the end", "censoring"))

    kinds = {b.get("type") for b in layout.get("blocks") or []}
    if (not censored and not kinds & _COUNT_CHANGERS and "Avg WIP" in system and completed.sum() > 0
            and _numbers(system["Avg WIP (Little)"]).mean() > 0):   # #763: nothing spends time inside
        ok, m, p, tol = _close(_numbers(system["Avg WIP"]), _numbers(system["Avg WIP (Little)"]))
        out.append(_check("little (system)", "passed" if ok else "partial",
                          f"Avg WIP {m:.3g} vs throughput x mean time in system {p:.3g}" + ("" if ok else
                          ": entities that stay inside (a hold, a loop, or from before the warm-up) "
                          "count in WIP but not in throughput; read Avg WIP, not Avg WIP (Little)"),
                          "little (system)"))

    if warmup == 0 and inside is not None:
        bias = _initial_bias(results)
        if bias is not None:
            out.append(bias)
    return out


def summary(found: list[dict]) -> dict:
    """What the run digest carries: the count of passed checks and the warnings in full."""
    return {"passed": sum(c["verdict"] == "passed" for c in found),
            "partial": [c for c in found if c["verdict"] != "passed"]}
