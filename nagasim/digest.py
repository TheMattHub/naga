"""A run's KPI digest, shared by the agent tools and the study modules (engine layer: nothing here
imports nagasim.agent_tools)."""
from __future__ import annotations

import numpy as np

from nagasim import experiments
from nagasim.core.stats import ci95

_RESOURCE_COLUMNS = ("Usage % (snapshot)", "Queue Length Avg", "Queue Length Max",
                     "Time in Queue Avg", "Total Seizes")


def finite(value) -> float | None:
    """A JSON-safe number: NaN and inf become null rather than invalid JSON."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


# What else the digest carries, when the model has it: the numbers a question about entity
# types, levels, leaving customers or agents is usually after, so answering it takes no
# get_kpi_table call (the LLM study's priority, tank, car park and repair tasks).
_DIGEST_EXTRA = {"entity": ("entities", ("Avg Time", "Total Completed")),
                 "level": ("levels", ("Average", "Final", "Time Empty %", "Time Full %")),
                 "queue": ("queues", ("Balked", "Reneged")),
                 "agent": ("agents", ("Avg Count",))}


def digest(results) -> dict:
    """A run's KPI digest: every system KPI with its CI half-width, a compact
    per-resource row, the user statistics and, when the model has them, a few
    columns per entity type, level, queue behaviour and agent state. Never the
    frames themselves."""
    if results is None:
        return {"replications": 0, "system": {}, "resources": {}, "user_stats": {}}
    flat = experiments.flatten_kpis(results)
    runs = sorted(flat["Run"].unique().tolist()) if not flat.empty else []
    system: dict[str, dict] = {}
    resources: dict[str, dict] = {}
    user_stats: dict[str, dict] = {}
    extra: dict[str, dict] = {}
    for key, group in flat.groupby("KPI"):
        stat = ci95(group["Value"].to_numpy(dtype=float))
        entry = {"mean": finite(stat["mean"]),
                 "ci_half_width": finite(stat["ub"] - stat["mean"])}
        section, _, rest = key.partition("/")
        name, _, column = rest.rpartition("/")
        if section == "system":
            system[rest] = entry
        elif section == "user":
            user_stats[rest] = entry
        elif section == "resource":
            if column in _RESOURCE_COLUMNS:
                resources.setdefault(name, {})[column] = entry
        elif section in _DIGEST_EXTRA and column in _DIGEST_EXTRA[section][1]:
            extra.setdefault(_DIGEST_EXTRA[section][0], {}).setdefault(name, {})[column] = entry
    if len(extra.get("entities", {})) < 2:      # one type: the system KPIs already say it
        extra.pop("entities", None)
    return {"replications": len(runs), "system": system, "resources": resources,
            "user_stats": user_stats, **extra}
