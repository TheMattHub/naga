"""Known answers a study can check a model against, and tail shares of waits.

Pure functions: no model slot, no I/O. `queue_formula` recognises a single arrival stream feeding
one resource and gives the textbook mean wait in queue; `wait_shares` reads the per-replication
share of entities that waited longer than x."""
from __future__ import annotations

import math

import numpy as np

_ALLOWED_TOP = {"$schema", "schema_version", "blocks", "links", "resources", "entities", "info",
                "simulation", "outputs", "scenarios"}
_CREATE_OK = {"distribution", "dist_params", "e_type", "arrival_mode", "entities_per_arrival",
              "max_arrivals", "start_time"}
_PROCESS_OK = {"distribution", "dist_params", "resource_name", "e_type", "action", "quantity", "priority"}
_RES_OK = {"name", "capacity", "discipline"}


def _num(params: dict, *keys: str) -> list[float]:
    vals = [params.get(k) for k in keys]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals):
        raise ValueError(f"the parameters {', '.join(keys)} are not plain numbers")
    return [float(v) for v in vals]


def _moments(dist: str, p: dict) -> tuple[float, float]:
    """(mean, variance) of a service time; ValueError when the family or its numbers do not fit."""
    if dist == "Exponential":
        (rate,) = _num(p, "rate")
        mean, var = 1 / rate, 1 / rate ** 2
    elif dist == "Constant":
        (mean,) = _num(p, "value")
        var = 0.0
    elif dist == "Uniform":
        lo, hi = _num(p, "low", "high")
        mean, var = (lo + hi) / 2, (hi - lo) ** 2 / 12
    elif dist == "Triangular":
        lo, hi, mode = _num(p, "low", "high", "mode")
        mean = (lo + hi + mode) / 3
        var = (lo * lo + hi * hi + mode * mode - lo * hi - lo * mode - hi * mode) / 18
    elif dist == "Normal":
        mean, sd = _num(p, "mean", "stddev")
        # The engine truncates at 0; the plain moments only hold when that cut is negligible.
        if mean < 4 * sd:
            raise ValueError("a Normal service time with mean < 4 stddev is truncated at 0")
        var = sd * sd
    else:
        raise ValueError(f"service distribution {dist} has no closed form here")
    if not mean > 0 or var < 0 or not math.isfinite(mean + var):
        raise ValueError("the service time has no positive mean")
    return mean, var


def _erlang_c(c: int, a: float) -> float:
    """P(an arrival waits) in M/M/c with offered load a = lambda/mu < c."""
    term, total = 1.0, 1.0
    for k in range(1, c):
        term *= a / k
        total += term
    top = term * a / c * c / (c - a)      # a^c / c! * c / (c - a)
    return top / (total + top)


def _analyse(layout: dict) -> dict:
    """The formula answer, or ValueError(why not)."""
    extra = set(layout) - _ALLOWED_TOP
    if extra:
        raise ValueError(f"the model has {sorted(extra)[0]}, which the formula does not cover")
    blocks = layout.get("blocks") or []
    kinds = sorted(b.get("type", "") for b in blocks)
    if kinds != ["CREATE", "DISPOSE", "PROCESS"]:
        raise ValueError("needs exactly one CREATE, one PROCESS and one DISPOSE")
    create, process, dispose = (next(b for b in blocks if b["type"] == t)
                                for t in ("CREATE", "PROCESS", "DISPOSE"))
    links = {(lk.get("source"), lk.get("target")) for lk in layout.get("links") or []}
    if links != {(create["name"], process["name"]), (process["name"], dispose["name"])}:
        raise ValueError("the blocks are not a single line CREATE -> PROCESS -> DISPOSE")
    cp, pp = create.get("params") or {}, process.get("params") or {}
    for p, ok, what in ((cp, _CREATE_OK, "CREATE"), (pp, _PROCESS_OK, "PROCESS")):
        if set(p) - ok:
            raise ValueError(f"the {what} block sets {sorted(set(p) - ok)[0]}, which the formula does not cover")
    if cp.get("arrival_mode", "Interarrival") != "Interarrival" or cp.get("entities_per_arrival", 1) != 1 \
            or cp.get("max_arrivals", 0) != 0 or cp.get("start_time", 0) != 0:
        raise ValueError("arrivals are not a plain unlimited one-at-a-time stream")
    if cp.get("distribution") != "Exponential":
        raise ValueError("interarrival times are not Exponential")
    (lam,) = _num(cp.get("dist_params") or {}, "rate")
    if pp.get("quantity", 1) != 1 or pp.get("priority", 0) != 0:
        raise ValueError("the PROCESS block seizes more than one unit or uses priorities")
    resources = layout.get("resources") or []
    if len(resources) != 1 or pp.get("resource_name") != resources[0].get("name"):
        raise ValueError("needs exactly one resource, the one the PROCESS block seizes")
    res = resources[0]
    if set(res) - _RES_OK or res.get("discipline", "FIFO") != "FIFO":
        raise ValueError("the resource has a schedule, failures or a non-FIFO discipline")
    c = res.get("capacity")
    if isinstance(c, bool) or not isinstance(c, int) or c < 1:
        raise ValueError("the resource capacity is not a whole number")
    mean, var = _moments(pp.get("distribution"), pp.get("dist_params") or {})
    mu, cs2, rho = 1 / mean, var / mean ** 2, lam * mean / c
    if rho >= 1:
        raise ValueError(f"utilisation {rho:.2f} is not below 1: no steady state")
    if pp["distribution"] == "Exponential":
        wq = _erlang_c(c, lam / mu) / (c * mu - lam)
        kind = "M/M/c"
    elif c == 1:
        wq = rho * (1 + cs2) / (2 * mu * (1 - rho))
        kind = "M/G/1"
    else:
        raise ValueError("more than one server with a non-exponential service time has no closed form")
    return {"kind": kind, "lam": lam, "mu": mu, "c": c, "cs2": cs2, "wq": wq}


def queue_formula(layout: dict) -> dict | None:
    """{"kind": "M/M/c"|"M/G/1", "lam", "mu", "c", "cs2", "wq"} (wq = mean wait in queue), or None."""
    try:
        return _analyse(layout)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def why_not(layout: dict) -> str:
    """The reason `queue_formula` gives None ("" when it applies)."""
    try:
        _analyse(layout)
    except ValueError as exc:
        return str(exc)
    except (KeyError, TypeError, AttributeError):
        return "the model is not laid out as one arrival stream feeding one resource"
    return ""


def share_above(waits: list[float], x: float) -> float:
    """Share of `waits` strictly above x (0 for no waits)."""
    return float(np.mean(np.asarray(waits, dtype=float) > x)) if len(waits) else 0.0


def wait_shares(results, resource: str, x: float, layout: dict | None = None) -> list[float] | None:
    """Per replication, the share of entities whose wait at `resource` exceeded x.

    The engine keeps no per-entity wait frame. The resource log does keep "request" and "seize"
    events, which pair in order under FIFO, so the waits are rebuilt from it. The share is over
    the entities that requested the resource after the warm-up and were served before the run
    ended (the log holds nothing else). FIFO is assumed: pass `layout` and a resource with
    another discipline gives None. Also None when the log is absent or truncated, or a
    cancel/attach breaks the pairing."""
    if layout is not None:
        res = next((r for r in layout.get("resources") or [] if r.get("name") == resource), None)
        if res is None or res.get("discipline", "FIFO") != "FIFO":
            return None
    try:
        log = results["resource_log"]
        system = results["system"]
    except (KeyError, TypeError):
        return None
    if log is None or len(log) == 0 or "Run" not in log:
        return None
    # ponytail: one flag covers resource and store logs; only ever too cautious.
    flag = system[system["Metric"] == "Resource/Store Log Truncated"]["Value"]
    if len(flag) and flag.max() > 0:
        return None
    mine = log[log["Resource"] == resource]
    if mine.empty:
        return None
    shares = []
    for _, rows in mine.groupby("Run", sort=True):
        if rows["event_type"].isin(["cancel", "attach"]).any():
            return None
        req = rows.loc[rows["event_type"] == "request", "time"].to_numpy(dtype=float)
        seize = rows.loc[rows["event_type"] == "seize", "time"].to_numpy(dtype=float)
        # The leading seizes belong to requests made before the warm-up ended (not in the log);
        # requests still queued at the end (the last queue_length of them) have no seize yet.
        k = len(seize) - (len(req) - int(rows["queue_length"].iloc[-1]))
        if k < 0 or k >= len(seize):
            return None
        shares.append(share_above((seize[k:] - req[:len(seize) - k]).tolist(), x))
    return shares
