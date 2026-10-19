"""Step checks for a study: turn a run's KPI digest into named verdicts.

Pure functions over the digest dict that `agent_tools.runs.run_model` returns. Laws are warnings
(a violated law is "partial", never "failed", since saturation can be the point of a model);
reproducibility, assumption and goal checks can fail. Little's law is deliberately not checked:
the engine computes "Avg WIP (Little)" from it, so a check would be circular.
"""
from __future__ import annotations



def _check(name: str, verdict: str, why: str, kind: str) -> dict:
    return {"name": name, "verdict": verdict, "why": why, "kind": kind}


def _mean(entry) -> float | None:
    return entry.get("mean") if isinstance(entry, dict) else None


def kpi_entry(digest: dict, kpi: str) -> dict | None:
    """KPI {mean, ci_half_width} from `system`, then `user_stats`, then "<resource>/<column>"."""
    for section in ("system", "user_stats"):
        e = (digest.get(section) or {}).get(kpi)
        if _mean(e) is not None:
            return e
    res, _, col = kpi.partition("/")
    e = ((digest.get("resources") or {}).get(res) or {}).get(col)
    return e if _mean(e) is not None else None


def kpi_mean(digest: dict, kpi: str) -> float | None:
    return _mean(kpi_entry(digest, kpi))


def _laws(digest: dict) -> list[dict]:
    out = []
    for name, cols in (digest.get("resources") or {}).items():
        u = _mean((cols or {}).get("Usage % (snapshot)"))
        if u is None:
            continue
        label = f"utilisation: {name}"
        if u > 100.5:
            out.append(_check(label, "partial", f"utilisation {u:.1f}% exceeds 100%", "law"))
        elif u > 95:
            out.append(_check(label, "partial", f"saturated at {u:.1f}%, results depend on run length", "law"))
        else:
            out.append(_check(label, "passed", f"utilisation {u:.1f}%", "law"))
    created = _mean((digest.get("system") or {}).get("Total Entities Created"))
    done = _mean((digest.get("system") or {}).get("Total Entities Completed"))
    if created is not None and done is not None:
        if created - done > 0.1 * created:
            out.append(_check("flow balance", "partial",
                              f"{created:.0f} created, {done:.0f} completed: work is piling up: "
                              "unstable system or run too short", "law"))
        else:
            out.append(_check("flow balance", "passed", f"{created:.0f} created, {done:.0f} completed", "law"))
    return out


def _range_check(name: str, kind: str, spec: dict, digest: dict) -> dict | None:
    kpi, lo, hi = spec.get("kpi"), spec.get("low"), spec.get("high")
    if not kpi or (lo is None and hi is None):
        return None
    m = kpi_mean(digest, kpi)
    if m is None:
        return _check(name, "not_verified", f"KPI '{kpi}' is not in the run digest", kind)
    ok = (lo is None or m >= lo) and (hi is None or m <= hi)
    return _check(name, "passed" if ok else "failed",
                  f"{kpi} = {m:g}, expected within [{lo}, {hi}]", kind)


def step_checks(layout: dict, digest: dict, rerun_digest: dict | None, study: dict) -> list[dict]:
    """All checks for one run. `layout` is accepted for the caller's convenience and unused so far."""
    checks = _laws(digest)
    if rerun_digest is not None:
        a, b = digest.get("system") or {}, rerun_digest.get("system") or {}
        diff = sorted(k for k in set(a) | set(b) if _mean(a.get(k)) != _mean(b.get(k)))
        checks.append(_check("reproducibility", "failed" if diff else "passed",
                             ("same-seed rerun differs on " + ", ".join(diff)) if diff
                             else "same-seed rerun matches on every system KPI", "reproducibility"))
    for kind, key in (("assumption", "assumptions"), ("goal", "goals")):
        for item in study.get(key) or []:
            spec = item.get("check") or {} if kind == "assumption" else item
            c = _range_check(f"{kind} {item['id']}", kind, spec, digest)
            if c:
                checks.append(c)
    return checks


def overall(checks: list[dict]) -> str:
    """failed if any assumption/other check failed; partial if a law warned, a goal is unmet or a
    check is unverified. An unmet goal is a finding about the system, not a broken model."""
    if not checks:
        return "not_verified"
    if any(c["verdict"] == "failed" and c["kind"] not in ("law", "goal") for c in checks):
        return "failed"
    if any(c["verdict"] in ("partial", "not_verified") or (c["verdict"] == "failed" and c["kind"] == "goal")
           for c in checks):
        return "partial"
    return "passed"
