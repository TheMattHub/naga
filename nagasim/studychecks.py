"""Step checks for a study: turn a run's KPI digest into named verdicts.

Pure functions over the digest dict that `agent_tools.runs.run_model` returns. Laws are warnings
(a violated law is "partial", never "failed", since saturation can be the point of a model);
reproducibility, assumption and goal checks can fail. The laws (utilisation, offered load, Little
per resource and for the system against the measured `Avg WIP`, flow conservation, censoring,
initial bias) are computed by `nagasim.laws` from the run's frames; the digest carries them.
"""
from __future__ import annotations



def _check(name: str, verdict: str, why: str, kind: str) -> dict:
    return {"name": name, "verdict": verdict, "why": why, "kind": kind}


def _mean(entry) -> float | None:
    return entry.get("mean") if isinstance(entry, dict) else None


def goal_kpi(spec: dict) -> str | None:
    """The KPI name a goal is judged on: its `kpi`, or for a tail goal (`share_above` x on
    "<resource>/Time in Queue") the share name the study step stores, "<resource>/Share waiting over x"."""
    kpi, x = spec.get("kpi"), spec.get("share_above")
    if kpi and x is not None:
        return f"{kpi.partition('/')[0]}/Share waiting over {x:g}"
    return kpi


def kpi_entry(digest: dict, kpi: str) -> dict | None:
    """KPI {mean, ci_half_width} from `system`, then `user_stats`, then `tails` (tail-goal shares the
    study step adds), then "<resource>/<column>"."""
    for section in ("system", "user_stats", "tails"):
        e = (digest.get(section) or {}).get(kpi)
        if _mean(e) is not None:
            return e
    res, _, col = kpi.partition("/")
    e = ((digest.get("resources") or {}).get(res) or {}).get(col)
    return e if _mean(e) is not None else None


def kpi_mean(digest: dict, kpi: str) -> float | None:
    return _mean(kpi_entry(digest, kpi))


def _laws(digest: dict) -> list[dict]:
    """The run's law checks (`nagasim.laws`, D684) as the `run_model` digest carries them: each
    warning in full, the passed ones as one count."""
    laws = digest.get("laws") or {}
    out = [_check(c["name"], "partial", c["why"], "law") for c in laws.get("partial") or []]
    if laws.get("passed"):
        out.append(_check("laws", "passed", f"{laws['passed']} law checks passed", "law"))
    return out


def _range_check(name: str, kind: str, spec: dict, digest: dict) -> dict | None:
    kpi, lo, hi = goal_kpi(spec), spec.get("low"), spec.get("high")
    if not kpi or (lo is None and hi is None):
        return None
    m = kpi_mean(digest, kpi)
    if m is None:
        return _check(name, "not_verified", f"KPI '{kpi}' is not in the run digest", kind)
    ok = (lo is None or m >= lo) and (hi is None or m <= hi)
    return _check(name, "passed" if ok else "failed",
                  f"{kpi} = {m:g}, expected within [{lo}, {hi}]", kind)


def precision_check(status: dict, n: int, cap: int) -> dict:
    """Never fails: partial when a goal KPI's CI is still wider than its target (the cap stopped it)."""
    missed = {k: v for k, v in status.items() if not v["met"]}
    if not missed:
        return _check("precision", "passed", f"{n} replications met the precision target on every goal KPI",
                      "precision")
    why = "; ".join(f"{k} +-{v['half_width']:g} against target +-{v['target']:g}" for k, v in missed.items())
    return _check("precision", "partial", f"{n} replications (cap {cap}): {why}", "precision")


def step_checks(layout: dict, digest: dict, rerun_digest: dict | None, study: dict,
                precision: dict | None = None, first_pass: dict | None = None) -> list[dict]:
    """All checks for one run. `layout` is accepted for the caller's convenience and unused so far.
    `precision` is {"status", "n", "cap"}; `first_pass` is the digest the same-seed rerun is compared
    with when `digest` has more replications."""
    checks = _laws(digest)
    if precision and precision["status"]:
        checks.append(precision_check(precision["status"], precision["n"], precision["cap"]))
    if rerun_digest is not None:
        a, b = (first_pass or digest).get("system") or {}, rerun_digest.get("system") or {}
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
    if any(c["verdict"] == "failed" and c["kind"] not in ("law", "goal", "precision") for c in checks):
        return "failed"
    if any(c["verdict"] in ("partial", "not_verified") or (c["verdict"] == "failed" and c["kind"] == "goal")
           for c in checks):
        return "partial"
    return "passed"
