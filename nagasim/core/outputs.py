from __future__ import annotations

"""
KPI-calculation utilities to be used in controller. The Excel/CSV writer is
core/export.py (C3.3).
"""
import json
import numpy as np
import pandas as pd
from math import isfinite
from typing import Optional
from dataclasses import asdict, is_dataclass
from collections import Counter

from nagasim.core import blocking, bottleneck
from nagasim.core.levels import visible
from nagasim.core.resource_manager import ENERGY_STATES
from nagasim.core.stats import _t_quantile, ci95, created_in_window
from nagasim.core.timeunits import factor
from nagasim.core.utils import ResourceSnapshot, StoreSnapshot

import logging

log = logging.getLogger(__name__)

__all__ = [
    "observed_window", "calc_kpis", "consumed_entities", "counted_entities", "entity_totals",
    "summarize_entities", "utilization", "summarize_resources", "summarize_stores",
    "summarize_system", "summarize_user_stats", "summarize_orders", "summarize_due_dates",
    "summarize_queue_behaviour", "summarize_levels", "summarize_flows", "flow_totals",
    "summarize_costs", "lump_sum_total", "summarize_buffers", "summarize_energy",
    "summarize_material_flow", "summarize_bottleneck", "summarize_schedule",
    "summarize_agents",
    "summarize_agent_attributes",
    "summarize_timeseries", "timeseries_mean", "summarize_dashboard", "summarize_game_log",
    "ci95_by_object_long", "pct_stats", "mode_stats", "entity_cost_asof",
    "filter_warmup", "LOG_DERIVED_COLUMNS",
]

# ---------- convenience facade -------------------------------------
def observed_window(env, warmup: float) -> tuple[float, float]:
    """(start, end) of the window the KPIs describe.

    End is the run length when a time limit was set, else the drain time
    (`step_until` records it); start is the end of the warm-up (DEVLOG #22, #32).
    """
    end = env.ctx.sim_end_time
    return float(warmup), float(env.now if end is None else end)

def calc_kpis(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> dict[str, 'pd.DataFrame']:
    """
    Return **all** KPI DataFrames in a dict keyed by short names.
    Keeps outputs.py dead simple.
    """
    t0, t1 = observed_window(env, warmup)
    sim_time = max(t1 - t0, 0.0)

    # PHASE4 D1: integrate the levels and flows up to the reporting horizon
    # before anything reads them -- the stretch after the last event is still
    # real simulated time, over which every rate was constant.
    if env.ctx.level_system is not None:
        env.ctx.level_system.sync(t1)

    ent_sum, ent_log   = summarize_entities(env, warmup=warmup, run_id=run_id)
    res_sum, res_log   = summarize_resources(env, warmup=warmup, run_id=run_id)
    sto_sum, sto_log   = summarize_stores(env, warmup=warmup, sim_time=sim_time, run_id=run_id)
    sys_sum            = summarize_system(env, warmup=warmup, run_id=run_id)
    usr_sum            = summarize_user_stats(env, warmup=warmup, run_id=run_id)
    ts_sum             = summarize_timeseries(env, run_id=run_id)   # already carries "Run"
    dash_sum           = summarize_dashboard(env, run_id=run_id)    # PHASE6 G1
    agt_attr, agt_log  = summarize_agent_attributes(env, warmup=warmup, run_id=run_id)

    # -- tag logs with the replication index --------------------------
    if run_id is not None:
        ent_log["Run"] = run_id
        res_log["Run"] = run_id
        sto_log["Run"] = run_id
        sys_sum["Run"] = run_id

    return {
        "system":   sys_sum,
        "entity_summary": ent_sum,
        "entity_log":     ent_log,
        "resource_summary": res_sum,
        "resource_log":     res_log,
        "store_summary":    sto_sum,
        "store_log":        sto_log,
        "user_summary":     usr_sum,
        # PHASE4 A3. Both are EMPTY (with their fixed columns) for a model with
        # no orders and no due dates, so every existing model's frames -- and
        # its golden hash, which covers "system" and the entity log -- are
        # untouched by their arrival.
        "order_summary":    summarize_orders(env, warmup=warmup, run_id=run_id),
        "due_date_summary": summarize_due_dates(env, run_id=run_id),
        # PHASE4 A4, same rule: EMPTY with its fixed columns for a model whose
        # blocks declare no balking, reneging or preemption.
        "queue_summary":    summarize_queue_behaviour(env, run_id=run_id),
        # Automation sub-project 1 (D156), the A3/A4 rule: EMPTY with its fixed
        # columns for a model with no MOTION block, so no golden hash moves.
        "motion_summary":   summarize_motion(env, run_id=run_id),
        # PHASE4 D1, same rule again: EMPTY with their fixed columns for a model
        # that declares no levels and no flows.
        "level_summary":    summarize_levels(env, warmup=warmup, run_id=run_id),
        "flow_summary":     summarize_flows(env, warmup=warmup, run_id=run_id),
        # Bio-1, same rule again: EMPTY with their fixed columns for a model
        # with no delayed conversion and no blended level.
        "conversion_summary": summarize_conversions(env, warmup=warmup, run_id=run_id),
        "component_summary":  summarize_components(env, run_id=run_id),
        # PHASE4 D5, same rule once more: EMPTY with its fixed columns for a
        # model with no fixed costs, no RECORD kind="cost" and no demand charge.
        "cost_summary":     summarize_costs(env, warmup=warmup, run_id=run_id),
        # PHASE6 W3 M1 (D112), same rule again: EMPTY with its fixed columns
        # for a model with no resource or fleet declaring "power".
        "energy_summary":   summarize_energy(env, warmup=warmup, run_id=run_id),
        # PHASE4 E1, same rule once more: EMPTY with their fixed columns for a
        # model with no mover fleet, no path network and no conveyor, so no
        # existing model's frames -- or golden hash -- move.
        "fleet_summary":    summarize_fleets(env, warmup=warmup, run_id=run_id),
        "segment_summary":  summarize_segments(env, run_id=run_id),
        "conveyor_summary": summarize_conveyors(env, warmup=warmup, run_id=run_id),
        # Automation sub-project 2 (D164-D165), the same rule: EMPTY with their
        # fixed columns for a model with no charger and no battery fleet.
        "charging_summary": summarize_charging(env, warmup=warmup, run_id=run_id),
        "battery_summary":  summarize_batteries(env, warmup=warmup, run_id=run_id),
        # Automation sub-project 3 (D169): EMPTY with its fixed columns for a
        # model no cobot tends in.
        "tending_summary":  summarize_tending(env, run_id=run_id),
        # PHASE4 T5, the same rule again: EMPTY with its fixed columns for a
        # model with no BUFFER and no `queue_capacity`.
        "buffer_summary":   summarize_buffers(env, warmup=warmup, run_id=run_id),
        # PHASE6 W3 M2 (D113), the same rule once more: EMPTY with its fixed
        # columns for a model with no entity traversal (never happens once a
        # block has run, but a run with only unreachable blocks is legal).
        "material_flow_summary": summarize_material_flow(env, run_id=run_id),
        # PHASE6 W3 M2 (D113): EMPTY unless the model sets `outputs.bottleneck`
        # -- the active-interval collection is opt-in (it costs memory).
        "bottleneck_summary": summarize_bottleneck(env, warmup=warmup, run_id=run_id),
        # PHASE6 W4 P2 (D120): EMPTY with its fixed columns unless the model
        # sets `outputs.schedule` (or runs deterministic, which forces it on)
        # -- the A3/E1 precedent once more.
        "schedule_log": summarize_schedule(env, run_id=run_id),
        # PHASE5 AB-1, the same rule: EMPTY with its fixed columns for a model
        # without populations.
        "agent_summary":    summarize_agents(env, warmup=warmup, run_id=run_id),
        # PHASE6 C2, the same rule once more: EMPTY with their fixed columns
        # for a model with no agent attributes.
        "agent_attributes": agt_attr,
        "agent_attribute_log": agt_log,
        "timeseries":       ts_sum,
        # PHASE6 G1 (D132), same "EMPTY with fixed columns" rule once more:
        # empty for a model with no dashboard section.
        "dashboard":        dash_sum,
    }

def consumed_entities(env, warmup: float):
    """Entities a batch still represents at report time (`in_batch` set).

    A member consumed by a permanent batch is neither live nor completed: the
    batch entity stands for it in WIP, so counting it as created would leave it
    looking stuck in the system for ever (DEVLOG #29). Separate clears
    `in_batch` and the member counts again. Unaffected by the entity-log cap
    (DEVLOG #62): a consumed member is never disposed, so it always stays
    resident in `all_entities`.
    """
    return [e for e in env.ctx.all_entities
            if getattr(e, "in_batch", None) is not None and created_in_window(e, warmup)]


def _live_created_entities(ctx, warmup: float):
    """Still-resident entities (not yet disposed) that count as "created"
    under the warm-up filter (DEVLOG #18, #22). The only entities whose
    contribution to `entity_totals` is not already exact in
    `ctx.entity_stats` -- everything disposed was accounted for once, at
    Dispose/Separate, whether or not it is still retained (DEVLOG #62)."""
    return [e for e in ctx.all_entities
            if e.deletion_time is None and created_in_window(e, warmup)
            and getattr(e, "in_batch", None) is None]


def entity_totals(env, warmup: float) -> dict:
    """Per-entity-type totals (ROADMAP 2.5, DEVLOG #62): the streaming,
    exact contribution of every entity already disposed (`ctx.entity_stats`,
    unaffected by `entity_log_limit`) plus whatever is still resident and
    not yet disposed -- bounded by current WIP, not by how many entities have
    completed over the whole run.

    Returns {e_type: {"created", "completed", "times": [float], "costs":
    [float], "holding_sum", "assigned_sum"}}.
    """
    ctx = env.ctx
    totals = {e_type: {"created": acc.created, "completed": acc.completed,
                        "times": list(acc.times), "costs": list(acc.costs),
                        "holding_sum": acc.holding_sum, "assigned_sum": acc.assigned_sum}
              for e_type, acc in ctx.entity_stats.items()}
    for e in _live_created_entities(ctx, warmup):
        t = totals.setdefault(e.e_type, {"created": 0, "completed": 0, "times": [],
                                          "costs": [], "holding_sum": 0.0, "assigned_sum": 0.0})
        t["created"] += 1
    return totals


def counted_entities(env, warmup: float):
    """Entities that count towards the post-warm-up statistics, as entity
    objects (kept for callers -- tests included -- that want the objects
    themselves, e.g. to check identity).

    Created if it arrived at or after the warm-up end and is not currently
    consumed by a batch (see `consumed_entities`); completed if it was disposed
    at or after the warm-up end (DEVLOG #18: test `deletion_time is not None`,
    never `hasattr` -- it is a property and is always present).

    Only scans entities still resident in `ctx.all_entities` (DEVLOG #62): a
    completed entity beyond `entity_log_limit` was already accounted for in
    `ctx.entity_stats` and dropped, so it is exact only up to the limit --
    `entity_totals` below is what a caller after exact aggregate numbers
    regardless of the limit should use instead.
    """
    entities = env.ctx.all_entities
    created = [e for e in entities
               if created_in_window(e, warmup) and getattr(e, "in_batch", None) is None]
    completed = [e for e in entities
                 if e.deletion_time is not None and e.deletion_time >= warmup
                 and e.creation_time is not None]
    return created, completed

_ENTITY_SUMMARY_COLS = ["Run", "Entity Type", "Total Created", "Total Completed", "Avg Time",
                        "Min Time", "Max Time", "Mode Time", "Mode Time %", "P95 Time",
                        "P99 Time", "Std Time", "Avg Cost", "Total Cost"]
# The flat log frames keep their columns even when they are empty -- a run
# with `record_logs` off (PHASE4 T7) must hand back the same frame shape.
_RESOURCE_LOG_COLS = ["Resource", "time", "in_use", "queue_length", "event_type"]
_STORE_LOG_COLS = ["Store", "time", "items", "event_type"]
_ENTITY_LOG_COLS = ["Entity ID", "Type", "Created At", "Exited At", "Throughput Time", "In Batch",
                    "Cost", "Initial", "Agent"]

# The summary columns whose value is computed FROM the per-event log (the
# queue-length / store-size snapshots) and so is blank or different with
# `record_logs` off. Everything else comes from the run's collectors. Kept here,
# next to `summarize_resources` / `summarize_stores` which compute them, and
# held to the code by tests/test_perf_study_logs.py (exactly these columns
# differ between a logs-on and a logs-off run). A study that names one of them
# keeps the logs on (`experiments.needs_event_logs`).
LOG_DERIVED_COLUMNS = {
    "resource_summary": frozenset({"Queue Length Mode", "Queue Length Mode %", "Queue Len P95",
                                   "Queue Len P99", "Queue Len Std"}),
    "store_summary": frozenset({"Mode Size", "Mode Size %", "P95 Size", "P99 Size", "Std Size"}),
}


# ---------- costing (ROADMAP 2.2) -----------------------------------------
def _pending_resource_cost(entity, ctx, t_end: float) -> float:
    """Projected busy-cost share for units *entity* still holds at *t_end* --
    a pure read (never mutates the entity), symmetric with how a resource's
    own `busy.area(t_end)` extends to *t_end* without a reset. A currently
    consumed batch member (`in_batch` set) never projects: it stopped
    accruing its own share the instant it was absorbed (`make_batch_entity`
    already moved its frozen cost into the batch entity)."""
    if getattr(entity, "in_batch", None) is not None:
        return 0.0
    total = 0.0
    for info in getattr(entity, "req_params", {}).values():
        member = info.get("resource")
        rate = getattr(member, "cost_busy", 0.0)
        if not rate:
            continue
        start = max(info.get("seized_at", t_end), ctx.warmup)
        if t_end > start:
            conv = factor(getattr(member, "cost_time_unit", "hours"), ctx.time_unit)
            total += rate * len(info["requests"]) * (t_end - start) / conv
    return total


def _pending_holding_cost(entity, ctx, t_end: float) -> float:
    """Projected WIP holding cost for an entity still alive (not yet disposed)
    at *t_end* -- same rationale as `_pending_resource_cost`, including the
    consumed-batch-member guard (a member never reaches Dispose itself, so
    `deletion_time` alone cannot detect "no longer independently in the
    system" for it)."""
    if entity.deletion_time is not None or entity.creation_time is None:
        return 0.0
    if getattr(entity, "in_batch", None) is not None:
        return 0.0
    rate = ctx.entity_manager.get_type(entity.e_type).get("holding_cost", 0.0)
    if not rate:
        return 0.0
    start = max(entity.creation_time, ctx.warmup)
    return rate * (t_end - start) if t_end > start else 0.0


def entity_cost_asof(entity, ctx, t_end: float) -> tuple[float, float]:
    """(total cost, holding-cost component) as of *t_end* (ROADMAP 2.2): the
    entity's own persisted totals (accrued at seize/release/dispose) plus a
    pure projection for a resource it still holds or for still being alive --
    report time is a read here, exactly like every other KPI in this module."""
    pending_r = _pending_resource_cost(entity, ctx, t_end)
    pending_h = _pending_holding_cost(entity, ctx, t_end)
    return entity.cost + pending_r + pending_h, entity._holding_cost + pending_h

# ---------- the four summarize_* functions (actual kpi calculation) ----
def summarize_entities(env, *, warmup: float = 0.0, run_id: Optional[int] = None):
    """Per-type summary and the flat per-entity log (ROADMAP 2.5, DEVLOG #62):
    the by-type numbers come from `entity_totals` (exact, streamed at Dispose,
    unaffected by `entity_log_limit`); the flat log is one row per entity
    still resident in `ctx.all_entities` -- capped at `entity_log_limit`
    completed entities, everything still live or batch-consumed always shows.
    """
    ctx = env.ctx
    _, t1 = observed_window(env, warmup)
    totals = entity_totals(env, warmup)

    # A type that only ever shows up consumed (never independently created or
    # completed) still earned a zero/NaN summary row before this refactor
    # (every entity in the union fed `summary_by_type`); keep that so a
    # type's presence in the summary does not depend on `entity_log_limit`.
    for e in consumed_entities(env, warmup):
        totals.setdefault(e.e_type, {"created": 0, "completed": 0, "times": [],
                                     "costs": [], "holding_sum": 0.0, "assigned_sum": 0.0})

    records = []
    for e in ctx.all_entities:
        is_completed = e.deletion_time is not None and e.deletion_time >= warmup
        is_created = created_in_window(e, warmup) and getattr(e, "in_batch", None) is None
        is_consumed = (getattr(e, "in_batch", None) is not None
                       and created_in_window(e, warmup))
        if not (is_completed or is_created or is_consumed):
            continue
        t_total = (e.deletion_time - e.creation_time) if is_completed else None
        cost, _ = entity_cost_asof(e, ctx, t1)
        records.append({
            "Entity ID": getattr(e, "_id", "?"),
            "Type": getattr(e, "e_type", "Unknown"),
            "Created At": e.creation_time,
            "Exited At": e.deletion_time,
            "Throughput Time": t_total,
            "In Batch": getattr(e, "in_batch", None),
            "Cost": cost,
            # ROADMAP 3.4b: True for an entity the warm start injected at t=0.
            "Initial": bool(getattr(e, "initial", False)),
            # H2, D130: "<Pop>:<index>" for an entity with an agent (from
            # CREATE's `population` or an agent's own `enter`), else "" --
            # whether that agent is still alive is a separate question
            # (AGENT()/ALIVE() at report time), not this column's job.
            "Agent": f"{ref[0]}:{ref[1]}" if (ref := getattr(e, "agent", None)) is not None else "",
        })

    summary_rows = []
    for e_type, data in totals.items():
        times = data["times"]
        costs = data["costs"]
        stats = pct_stats(times) # for p95, p99 and std
        mode_t, mode_t_perc = mode_stats(times)

        summary_rows.append({
            "Run": run_id,
            "Entity Type": e_type,
            "Total Created": data["created"],
            "Total Completed": data["completed"],
            "Avg Time": np.mean(times) if times else np.nan,
            "Min Time": np.min(times) if times else np.nan,
            "Max Time": np.max(times) if times else np.nan,
            "Mode Time":       mode_t,
            "Mode Time %":     mode_t_perc,
            "P95 Time": stats["p95"],
            "P99 Time": stats["p99"],
            "Std Time": stats["std"],
            "Avg Cost": np.mean(costs) if costs else np.nan,
            "Total Cost": float(np.sum(costs)) if costs else 0.0,
        })

    # DEVLOG #9: an all-filtered-out frame has no "Entity Type" column to sort on
    summary_df = pd.DataFrame(summary_rows, columns=_ENTITY_SUMMARY_COLS)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("Entity Type").reset_index(drop=True)

    log_df = pd.DataFrame(records, columns=_ENTITY_LOG_COLS)
    # DEVLOG #99: "In Batch" is a batch NAME or None, so a replication in which
    # nothing was still batched at report time infers float64 (all-NaN) while
    # its neighbours infer object -- and concatenating the replications raised
    # a pandas FutureWarning, which the test suite treats as an error. The
    # column's type is a fact about the column, not about one replication.
    # DEVLOG #389: the same for the two times, None in every row of a replication in
    # which no entity left.
    return summary_df, log_df.astype({"In Batch": "object", "Exited At": "float64",
                                      "Throughput Time": "float64"})

def utilization(res, observed: float, t_end: float) -> float:
    """Busy unit-time over SCHEDULED capacity-time (ROADMAP 1.4).

    The denominator is the integral of the capacity actually in force, so time
    a resource spends off shift or broken down does not dilute its utilization.
    For a resource with neither a schedule nor a failure the available level is
    the constant capacity, so this is exactly `capacity x observed` as before.
    """
    capacity_time = res.available_area(t_end)
    if observed <= 0 or capacity_time <= 0 or not isfinite(capacity_time):
        return 0.0
    return res.busy.area(t_end) / capacity_time

def _reportable(res) -> bool:
    """A resource earns a summary row once something happened to it: an entity
    event in its log, or a capacity schedule / failure driving its state even
    while no entity ever seized it (ROADMAP 1.4)."""
    if getattr(res, "has_controller", False):
        return True
    # The COUNT, not the log itself: with `record_logs` off (PHASE4 T7) nothing
    # is appended, and a resource that was used all run still earns its row.
    return getattr(res, "log_events", 0) > 0


def summarize_resources(env, warmup: float = 0.0, run_id: int | None = None):
    logs = []
    summaries = []
    BOTTLE_THRESHOLD = 0.75   # utilization
    bottleneck_scores = {}
    t0, t1 = observed_window(env, warmup)
    observed = max(t1 - t0, 0.0)

    for name, res in env.ctx.resource_manager.all().items():
        if not _reportable(res):
            continue

        res_entries = filter_warmup(res.log, warmup)

        # Time-weighted KPIs come from the collectors, never from the event
        # snapshots (DEVLOG #21, #22); snapshots only feed the flat log sheet
        # and the mode/percentile cosmetics.
        util = utilization(res, observed, t1)
        avg_queue = res.queue_len.time_avg(t1) if observed > 0 else 0.0
        queue_lengths = [e.queue_length for e in res_entries]
        waits = res.wait.values
        # DEVLOG #24: waits are only recorded on acquisition, so a request still
        # queued at t1 is invisible. Its censored wait feeds a separate column;
        # the original one keeps its (completed-waits-only) definition.
        all_waits = waits + res.open_waits(t1)
        mode_len, mode_perc = mode_stats(queue_lengths)
        wait_mode, wait_perc = mode_stats(waits)
        bottleneck_score = round(util * avg_queue, 2)

        if util >= BOTTLE_THRESHOLD: # keep scores only if usage hits the 75 % threshold
            bottleneck_scores[name] = bottleneck_score

        q_stats = pct_stats(queue_lengths)
        busy_cost, idle_cost, usage_cost, total_cost = res.cost_totals(
            t1, getattr(env.ctx, "time_unit", "minutes"))

        summaries.append({
            "Run": run_id,
            "Resource": name,
            "Total Seizes": res.seize_count,
            "Usage % (snapshot)": round(util * 100, 2),
            "Idle %": round((1 - util) * 100, 2),
            "Scheduled Capacity Avg": round(res.scheduled_capacity_avg(t1), 4),
            "Down %": round(res.down_fraction(t1) * 100, 2),
            "Busy Cost": round(busy_cost, 4),
            "Idle Cost": round(idle_cost, 4),
            "Usage Cost": round(usage_cost, 4),
            "Total Cost": round(total_cost, 4),
            "Queue Length Avg": round(avg_queue, 4),
            "Queue Length Max": res.queue_len.max,
            "Queue Length Mode": mode_len,
            "Queue Length Mode %": mode_perc,
            "Queue Len P95": q_stats["p95"],
            "Queue Len P99": q_stats["p99"],
            "Queue Len Std": q_stats["std"],
            "Time in Queue Avg": round(res.wait.mean, 4) if waits else 0,
            "Time in Queue Avg (incl. waiting)": round(float(np.mean(all_waits)), 4) if all_waits else 0,
            "Time in Queue Max": res.wait.max if waits else 0,
            "Time in Queue Mode": wait_mode,
            "Time in Queue Mode %": wait_perc,
            "Wait P95": res.wait.percentile(95),
            "Wait P99": res.wait.percentile(99),
            "Wait Std": res.wait.std,
            "Bottleneck Score": bottleneck_score,
            "Bottleneck %": 0,   # overwritten below for the top scorer
        })

        for entry in res_entries:
            if type(entry) is ResourceSnapshot:     # asdict() deep-copies every field (perf K1)
                logs.append((name, entry.time, entry.in_use, entry.queue_length, entry.event_type))
            else:
                row = asdict(entry) if is_dataclass(entry) else entry
                logs.append(tuple({"Resource": name, **row}.get(c) for c in _RESOURCE_LOG_COLS))

    # -- decide ONE bottleneck (highest score) and flag it -------------
    if bottleneck_scores:
        top_name = max(bottleneck_scores, key=bottleneck_scores.get)
        for row in summaries:
            row["Bottleneck %"] = int(row["Resource"] == top_name)

    return pd.DataFrame(summaries), pd.DataFrame(logs, columns=_RESOURCE_LOG_COLS)

def summarize_stores(env, *, warmup: float = 0.0, run_id: int | None = None, sim_time: float):
    store_summary = []
    store_logs = []
    _, t1 = observed_window(env, warmup)

    for store in env.ctx.store_manager.all():
        # The event COUNT, not the log: with `record_logs` off (PHASE4 T7)
        # nothing is appended, and a store that was used still earns its row.
        if getattr(store, "log_events", 0) == 0:
            continue

        st_entries = filter_warmup(store.log, warmup)

        usage = [e.items for e in st_entries]
        residences = store.residence.values

        sz_stats = pct_stats(usage)
        mode_sz, mode_sz_perc = mode_stats(usage)
        mode_wt, mode_wt_perc = mode_stats(residences)

        capacity = getattr(store, "capacity", float("inf"))
        avg_size = store.size.time_avg(t1) if sim_time > 0 else 0.0

        util_pct = np.nan
        if capacity not in (0, float("inf")) and sim_time > 0:
            util_pct = round(100 * store.size.area(t1) / (capacity * sim_time), 2)

        store_summary.append({
            "Run": run_id,
            "Store": getattr(store, "name", "UnnamedStore"),
            "Capacity": capacity,
            "Average Size": round(avg_size, 4),
            "Max Size": store.size.max,
            "Mode Size": mode_sz,
            "Mode Size %": mode_sz_perc,
            "P95 Size": sz_stats["p95"],
            "P99 Size": sz_stats["p99"],
            "Std Size": sz_stats["std"],
            "Avg Time in Store": store.residence.mean if residences else 0,
            "Mode Time in Store": mode_wt,
            "Mode Time in Store %": mode_wt_perc,
            "P95 Time in Store": store.residence.percentile(95),
            "P99 Time in Store": store.residence.percentile(99),
            "Std Time in Store": store.residence.std,
            "Util % (time-weighted)": util_pct,
        })

        for entry in st_entries:
            if type(entry) is StoreSnapshot:        # same as the resource log above
                store_logs.append((store.name, entry.time, entry.items, entry.event_type))
            else:
                row = asdict(entry) if is_dataclass(entry) else entry
                store_logs.append(tuple({"Store": store.name, **row}.get(c) for c in _STORE_LOG_COLS))

    return pd.DataFrame(store_summary), pd.DataFrame(store_logs, columns=_STORE_LOG_COLS)

def summarize_system(env, *, warmup: float = 0.0, run_id: int | None = None):
    totals = entity_totals(env, warmup)
    created = sum(t["created"] for t in totals.values())
    completed = sum(t["completed"] for t in totals.values())
    throughput_times = [t for tt in totals.values() for t in tt["times"]]

    t0, t1 = observed_window(env, warmup)
    total_time = max(t1 - t0, 0.0)

    utils = []
    avg_queues = []
    max_queues = []
    # Utilization: real resources only. A set's own `busy` collector re-counts
    # its members' busy units under seizing.py's counted=False path (the set
    # tracks the seize too, for its own summary row), so folding the set's
    # pseudo-row into this average would double-count those units.
    for res in env.ctx.resource_manager.resources.values():
        if not _reportable(res):
            continue
        utils.append(utilization(res, total_time, t1))

    # Queue length: real resources AND sets. A set's queue is not reflected in
    # any member's queue (a member request routed through a set is made with
    # counted=False), so leaving sets out would silently drop that wait.
    for res in env.ctx.resource_manager.all().values():
        if not _reportable(res):
            continue
        avg_queues.append(res.queue_len.time_avg(t1))
        max_queues.append(res.queue_len.max)

    for store in env.ctx.store_manager.all():   # DEVLOG #10: this loop tested the leaked `res`
        if getattr(store, "log_events", 0) == 0:
            continue
        avg_queues.append(store.size.time_avg(t1))
        max_queues.append(store.size.max)

    if total_time <= 0:
        throughput = np.nan
        avg_wip    = np.nan
    else:
        throughput = completed / total_time
        avg_wip    = throughput * (np.mean(throughput_times) if throughput_times else 0)

    # Costing (ROADMAP 2.2). Resource cost comes straight from the resource
    # collectors (real resources only -- a set's own cost_totals is always
    # zero, so including it would add nothing but is never wrong either).
    # Holding cost and the model-assigned-only portion are entity-level: summed
    # over "created ∪ completed" (== every entity_summary row for this run),
    # excluding a currently-consumed batch member -- its cost already moved
    # into the batch entity (ROADMAP 2.2, mirrors DEVLOG #29's WIP rule).
    resource_cost_total = 0.0
    time_unit = getattr(env.ctx, "time_unit", "minutes")
    for res in env.ctx.resource_manager.resources.values():
        if not _reportable(res):
            continue
        resource_cost_total += res.cost_totals(t1, time_unit)[3]

    # Holding cost and the model-assigned-only portion (ROADMAP 2.2): the
    # completed share comes straight from `entity_totals` (exact, streamed at
    # Dispose -- DEVLOG #62); only entities still live need a per-entity
    # projection (bounded by current WIP, not by total throughput).
    holding_total = sum(t["holding_sum"] for t in totals.values())
    assigned_total = sum(t["assigned_sum"] for t in totals.values())
    for e in _live_created_entities(env.ctx, warmup):
        _, holding = entity_cost_asof(e, env.ctx, t1)
        holding_total += holding
        assigned_total += (e.cost - e._auto_cost)

    # Flow cost and impact (PHASE4 D1) join Total Cost the way resource cost
    # does. Both are exactly 0 for a model with no flows, and the two rows they
    # add below appear ONLY for a model that has some -- so no existing model's
    # system frame (or golden hash) moves.
    flow_cost_total, impact_total = flow_totals(env)
    # Lump-sum costs (PHASE4 D5): fixed, per-event and peak charges. Exactly 0
    # for a model declaring none, so every existing Total Cost is unchanged.
    lump_total = lump_sum_total(env)
    # Energy cost (PHASE6 W3 M1, D112): kWh x energy_price, summed over every
    # resource/fleet declaring "power". Exactly 0 for a model declaring
    # neither, so every existing Total Cost is unchanged.
    energy_total = sum(r["Energy Cost"] for r in _resource_energy_rows(env, t0, t1))
    total_cost = (resource_cost_total + holding_total + assigned_total + flow_cost_total
                  + lump_total + energy_total)
    cost_per_completed = round(total_cost / completed, 4) if completed else np.nan

    rows = [
        {"Metric": "Run",                 "Value": run_id},
        {"Metric": "Time Unit",           "Value": time_unit},
        {"Metric": "Simulation Time",     "Value": total_time},
        {"Metric": "Total Entities Created",   "Value": created},
        {"Metric": "Total Entities Completed", "Value": completed},
        {"Metric": "Avg Throughput Time", "Value": round(np.mean(throughput_times), 4) if throughput_times else np.nan},
        {"Metric": "Throughput (per time unit)",      "Value": round(throughput, 4)},
        {"Metric": "Avg WIP (Little)",    "Value": round(avg_wip, 4)},
        {"Metric": "Max Queue Length",    "Value": max(max_queues) if max_queues else 0},
        {"Metric": "Avg Queue Length",    "Value": round(float(np.sum(avg_queues)), 4) if avg_queues else 0},
        {"Metric": "Avg Resource Usage %", "Value": round(float(np.mean(utils)) * 100, 2) if utils else 0},
        {"Metric": "Total Resource Cost", "Value": round(resource_cost_total, 4)},
        *([{"Metric": "Total Flow Cost", "Value": round(flow_cost_total, 4)},
           {"Metric": "Total Impact", "Value": round(impact_total, 4)}]
          if env.ctx.level_system is not None else []),
        {"Metric": "Total Entity Holding Cost", "Value": round(holding_total, 4)},
        {"Metric": "Total Cost",          "Value": round(total_cost, 4)},
        {"Metric": "Cost per Completed Entity", "Value": cost_per_completed},
        # ROADMAP 2.5, DEVLOG #62: 1 once the flat entity log/entity_summary
        # percentiles started dropping completed entities beyond entity_log_limit
        # (counts/means/costs above stay exact either way).
        {"Metric": "Entity Log Truncated", "Value": int(env.ctx.entity_log_truncated)},
        # Same idea for the per-resource/store snapshot deques (config.LOG_HISTORY):
        # their own time-weighted/tally KPIs above are unaffected either way.
        {"Metric": "Resource/Store Log Truncated",
         "Value": int(any(getattr(r, "log_truncated", False)
                          for r in env.ctx.resource_manager.all().values())
                     or any(getattr(s, "log_truncated", False)
                           for s in env.ctx.store_manager.all()))},
        # PHASE6 W4 P1, D119: only for a deterministic run, so an ordinary
        # (stochastic) run's system frame stays byte-for-byte the same as
        # before -- the golden-hash tests rely on that.
        *([{"Metric": "Deterministic", "Value": 1},
           {"Metric": "Replications Forced To 1", "Value": 1}]
          if env.ctx.deterministic else []),
        # D232: only for a run whose clock started from a state, so nobody reads its
        # means as a full run's; every other system frame is unchanged.
        *([{"Metric": "Started From State At", "Value": env.ctx.clock}]
          if getattr(env.ctx, "clock", 0) else []),
    ]
    rows += _short_window_warning(env, warmup, total_time)
    return pd.DataFrame(rows)


# A run whose statistics cover less than this share of the horizon they were
# asked for is reported, not silently averaged over nothing.
SHORT_WINDOW_SHARE = 0.10


def _short_window_warning(env, warmup: float, total_time: float) -> list:
    """The "Short Statistics Window %" row, when there is one.

    A model whose arrivals run out (a finite `max_arrivals`) drains long before
    the time limit, and with a warm-up longer than the drain time every KPI
    above is computed over an empty window: zero created, zero completed, an
    "average" of nothing. That is a real defect in the MODEL, not in the run,
    and it is invisible in the numbers themselves -- so it goes out on the
    channel the truncated-log flags already use, the system frame, which the
    results window, the CLI and the HTML report all show.

    Only a run with a WARM-UP is checked: a terminating model with no warm-up
    that finishes early has simply finished, and its statistics are complete.
    The row appears ONLY when the warning fires -- it is a warning, not a KPI,
    so a healthy run gains neither an always-zero metric nor an always-present
    comparison key.
    """
    horizon = getattr(env.ctx, "sim_horizon", None)
    if horizon is None or warmup <= 0:   # no time limit, or nothing was discarded
        return []
    intended = max(float(horizon) - float(warmup), 0.0)
    if intended <= 0 or total_time >= SHORT_WINDOW_SHARE * intended:
        return []
    share = 100.0 * total_time / intended
    log.warning("statistics cover %.1f%% of the intended horizon: the run drained at t=%g with a "
                "warm-up of %g and a time limit of %g. Check the arrival blocks' max_arrivals, or "
                "lower the warm-up.", share, env.now, warmup, horizon)
    return [{"Metric": "Short Statistics Window %", "Value": round(share, 2)}]

# ---------- orders and due dates (PHASE4 A3) ------------------------------
_ORDER_SUMMARY_COLS = ["Run", "Order", "Entity Type", "Sequence", "Release", "Due", "Quantity",
                       "Created", "Completed", "Completion Time", "Lateness", "Tardiness",
                       "Late", "Status"]
_DUE_DATE_SUMMARY_COLS = ["Run", "Entity Type", "Completed", "On Time", "Service Level",
                          "Avg Lateness", "Avg Tardiness", "Max Tardiness", "Late"]
# The due-date frame's model-wide row. `Entity.e_type` upper-cases every type,
# so a lowercase sentinel can never collide with a real one.
DUE_DATE_ALL = "(all)"
# `order_summary`'s Status column, and what each one means for the aggregates:
ORDER_COMPLETE = "complete"        # finished at or after the warm-up end: counts
ORDER_WARMUP = "warmup"            # finished before it: reported, not counted
ORDER_INCOMPLETE = "incomplete"    # still in the system (or never released) at run end


def summarize_orders(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "order_summary" frame (PHASE4 A3): one row per declared order.

    An order COMPLETES when the last entity it released is disposed, so
    `Completion Time` is that entity's deletion time and `Lateness` is
    `completion - due` (signed), `Tardiness` its positive part. Three statuses,
    because a horizon cuts orders in half at both ends:

    * `complete`   -- completed at or after the warm-up end. The only status
                      the means in `flatten_kpis`/the report are taken over.
    * `warmup`     -- completed before the warm-up end. Its numbers are still
                      reported (they are facts) but it belongs to the discarded
                      transient, exactly like an entity disposed during warm-up.
    * `incomplete` -- still in the system, or never released, at run end.
                      Completion/Lateness/Tardiness are NaN: an unfinished job
                      has no completion time, and guessing one would bias every
                      mean towards "on time".
    """
    rows = []
    for order in env.ctx.orders.values():
        completion = order.last_completion if order.complete else None
        has_lateness = completion is not None and order.due is not None
        late = (completion - order.due) if has_lateness else np.nan
        if completion is None:
            status = ORDER_INCOMPLETE
        else:
            status = ORDER_COMPLETE if completion >= warmup else ORDER_WARMUP
        rows.append({
            "Run": run_id, "Order": order.id, "Entity Type": order.entity_type,
            "Sequence": order.sequence, "Release": order.release,
            "Due": np.nan if order.due is None else order.due, "Quantity": order.quantity,
            "Created": order.created, "Completed": order.completed,
            "Completion Time": np.nan if completion is None else completion,
            "Lateness": late,
            "Tardiness": max(late, 0.0) if has_lateness else np.nan,
            "Late": int(late > 0) if has_lateness else np.nan, "Status": status,
        })
    return pd.DataFrame(rows, columns=_ORDER_SUMMARY_COLS)


def summarize_due_dates(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "due_date_summary" frame (PHASE4 A3): one row per entity type whose
    entities completed carrying a `due_date`, plus a model-wide `(all)` row.

    Entity-level, so it covers a make-to-stock or service model that sets a due
    date with an ASSIGN and declares no orders at all. The warm-up filter is
    applied when each entity is accounted for (`stats.account_due_date`): only
    entities disposed at or after the warm-up end are in these sums.

    Service Level is the fraction on time (lateness <= 0), which is what a
    paired-t scenario comparison reads as `duedate/(all)/Service Level`.
    """
    rows = []
    stats = env.ctx.due_date_stats
    if stats:
        total = DueDateStatsTotal(stats.values())
        for e_type, acc in list(stats.items()) + [(DUE_DATE_ALL, total)]:
            rows.append({
                "Run": run_id, "Entity Type": e_type, "Completed": acc.completed,
                "On Time": acc.on_time,
                "Service Level": acc.on_time / acc.completed if acc.completed else np.nan,
                "Avg Lateness": acc.lateness_sum / acc.completed if acc.completed else np.nan,
                "Avg Tardiness": acc.tardiness_sum / acc.completed if acc.completed else np.nan,
                "Max Tardiness": acc.tardiness_max,
                "Late": acc.completed - acc.on_time,
            })
    df = pd.DataFrame(rows, columns=_DUE_DATE_SUMMARY_COLS)
    if not df.empty:
        df = df.sort_values("Entity Type").reset_index(drop=True)
    return df


class DueDateStatsTotal:
    """The `(all)` row's accumulator: the same five fields summed over every
    entity type, so `summarize_due_dates` has one loop and no special case."""

    __slots__ = ("completed", "on_time", "lateness_sum", "tardiness_sum", "tardiness_max")

    def __init__(self, accumulators):
        accumulators = list(accumulators)
        self.completed = sum(a.completed for a in accumulators)
        self.on_time = sum(a.on_time for a in accumulators)
        self.lateness_sum = sum(a.lateness_sum for a in accumulators)
        self.tardiness_sum = sum(a.tardiness_sum for a in accumulators)
        self.tardiness_max = max((a.tardiness_max for a in accumulators), default=0.0)


_QUEUE_SUMMARY_COLS = ["Run", "Block", "Resource", "Balked", "Reneged", "Preempted",
                       "Avg Wait Before Renege", "Max Wait Before Renege",
                       # PHASE4 T3: a changeover is the other per-block thing
                       # that happens at a resource, so it reports here rather
                       # than in a frame of its own.
                       "Changeovers", "Setup Time"]


def summarize_queue_behaviour(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "queue_summary" frame (PHASE4 A4): one row per SEIZE/PROCESS block
    that declares balking, reneging or preemption.

    Per block rather than per resource, because the behaviour is a property of
    the block (two blocks may queue for the same resource with different
    patience); the resource is a column, so a reader can still group by it.
    Counts reset at warm-up like every other collector, and the two wait
    columns describe how long the entities that gave up had waited.

    PHASE4 T3 adds the CHANGEOVER count and total setup time here, for a
    PROCESS that declares a setup or a setup cost -- D52 left them on the block
    object, reachable only from Python. A changeover is the same kind of thing
    as a balk: something that happened to this block at its resource. A model
    with neither still gets no rows.
    """
    rows = []
    for process in env.ctx.process_list:
        # Declared behaviour, or a block whose jobs were actually interrupted:
        # `preempt` sits on the block that INTERRUPTS, the count on the one
        # whose job was interrupted (ARCHITECTURE D55).
        setups = bool(getattr(process, "setup", "") or getattr(process, "setup_cost", ""))
        if not (getattr(process, "queue_behaviour", False)
                or getattr(process, "preempted_count", 0) or setups):
            continue
        waits = process.renege_wait
        rows.append({
            "Run": run_id, "Block": process.name, "Resource": process.target_name,
            "Balked": process.balked_count, "Reneged": process.reneged_count,
            "Preempted": process.preempted_count,
            "Avg Wait Before Renege": waits.mean, "Max Wait Before Renege": waits.max,
            "Changeovers": int(getattr(process, "setup_count", 0)),
            "Setup Time": float(getattr(process, "setup_time_total", 0.0)),
        })
    return pd.DataFrame(rows, columns=_QUEUE_SUMMARY_COLS)


_MOTION_SUMMARY_COLS = ["Run", "Motion", "Block", "Segment Kind", "Segments", "Mean Time",
                        "Total Time", "Cycle Share %", "Collaborative %"]


def summarize_motion(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "motion_summary" frame (D156): per MOTION block, one row per segment
    kind that ran, in cycle order, plus a "cycle" row. `Motion` is
    "<block>/<kind>" -- one id column for the KPI keys, as conversion_summary
    does. Completed cycles since the warm-up only; a cycle straddling it counts
    whole (the setup_time_total rule). Times are in the model's base unit."""
    rows = []
    for process in env.ctx.process_list:
        stats = getattr(process, "motion_stats", None)
        if stats is None or not process.cycles:
            continue
        cycle_total = process.cycle_time_total
        for kind, (count, total) in stats.items():
            rows.append({
                "Run": run_id, "Motion": f"{process.name}/{kind}", "Block": process.name,
                "Segment Kind": kind, "Segments": count, "Mean Time": total / count,
                "Total Time": total,
                "Cycle Share %": 100.0 * total / cycle_total if cycle_total > 0 else np.nan,
                "Collaborative %": (100.0 * process.collaborative_moves / process.moves
                                    if kind == "move" and process.moves else np.nan),
            })
        rows.append({"Run": run_id, "Motion": f"{process.name}/cycle", "Block": process.name,
                     "Segment Kind": "cycle", "Segments": process.cycles,
                     "Mean Time": cycle_total / process.cycles, "Total Time": cycle_total,
                     "Cycle Share %": 100.0, "Collaborative %": np.nan})
    return pd.DataFrame(rows, columns=_MOTION_SUMMARY_COLS)


# ---------- levels and flows (PHASE4 D1) ----------------------------------
_LEVEL_SUMMARY_COLS = ["Run", "Level", "Unit", "Average", "Min", "Max", "Final",
                       "Time Empty %", "Time Full %", "Total In", "Total Out",
                       "Spilled", "Unmet"]
_FLOW_SUMMARY_COLS = ["Run", "Flow", "From", "To", "Total", "Avg Rate", "Peak Rate",
                      "Time Limited %", "Cost", "Impact", "Starts"]
# Bio-1: one row per (conversion, substrate) and one per (level, component).
# Both are EMPTY with their fixed columns for a model that declares neither, so
# no existing model's frames or golden hashes move (the A3/A4/D1 precedent).
_CONVERSION_SUMMARY_COLS = ["Run", "Conversion", "Substrate", "Unit", "Total In", "Produced",
                            "Unreleased", "Avg Rate", "Peak Rate"]
_COMPONENT_SUMMARY_COLS = ["Run", "Component", "Level", "Avg Fraction", "Min Fraction",
                           "Max Fraction", "Final Fraction", "Total In", "Total Out",
                           "Final Content"]


def summarize_levels(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "level_summary" frame (PHASE4 D1): one row per declared level.

    `Average` is the true time-average of a PIECEWISE-LINEAR curve (trapezoid
    per interval, `levels.Level._accrue`), not the piecewise-constant average a
    queue length gets. `Spilled` is quantity a full level could not accept,
    `Unmet` quantity an empty one could not supply -- curtailed energy, lost
    sales, overflow.
    """
    system = env.ctx.level_system
    if system is None:
        return pd.DataFrame([], columns=_LEVEL_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = [{"Run": run_id, "Level": level.name, "Unit": level.unit,
             "Average": level.time_avg(t1), "Min": level.min, "Max": level.max,
             "Final": level.level,
             "Time Empty %": 100.0 * level.empty_fraction(t1),
             "Time Full %": 100.0 * level.full_fraction(t1),
             "Total In": level.inflow_total, "Total Out": level.outflow_total,
             "Spilled": level.spilled, "Unmet": level.unmet}
            for level in visible(system.levels.values())]     # no delay stages (SD-2)
    return pd.DataFrame(rows, columns=_LEVEL_SUMMARY_COLS)


def summarize_flows(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "flow_summary" frame (PHASE4 D1): one row per declared flow.

    `Total` is the exact integral of the rate (rate constant between events),
    `Cost` and `Impact` the same integral weighted by the flow's `price` and
    `emission_factor`. `Peak Rate` is the highest rate the flow actually HELD
    for some time -- a grid connection's peak power, which is what a demand
    charge is billed on. `Time Limited %` is how much of the window the flow
    ran below the rate its own expression asked for, because a bound or a power
    limit cut it.
    """
    system = env.ctx.level_system
    if system is None:
        return pd.DataFrame([], columns=_FLOW_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = [{"Run": run_id, "Flow": flow.name,
             "From": flow.src.name if flow.src else "", "To": flow.dst.name if flow.dst else "",
             "Total": flow.total, "Avg Rate": flow.avg_rate(t1), "Peak Rate": flow.peak,
             "Time Limited %": 100.0 * flow.limited_fraction(t1),
             "Cost": flow.cost, "Impact": flow.impact, "Starts": flow.starts}
            for flow in visible(system.flows)]
    return pd.DataFrame(rows, columns=_FLOW_SUMMARY_COLS)


def summarize_conversions(env, *, warmup: float = 0.0,
                          run_id: Optional[int] = None) -> pd.DataFrame:
    """The "conversion_summary" frame (Bio-1 G1): one row per substrate of each
    delayed conversion.

    `Conversion` is `<conversion>/<substrate>`, so the KPI key
    `conversion/Digester/maize/Produced` addresses one feedstock of one
    digester. `Unreleased` is the product still in the pipeline at the end of
    the window -- a STATE, so `Total In x yield = Produced + Unreleased` holds
    exactly for a run with no warm-up, and `Unreleased` stays right for one
    with a warm-up, where `Produced` restarted from zero.
    """
    system = env.ctx.level_system
    if system is None or not system.conversions:
        return pd.DataFrame([], columns=_CONVERSION_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = [{"Run": run_id, "Conversion": f"{conversion.name}/{substrate.name}",
             "Substrate": substrate.name, "Unit": conversion.unit,
             "Total In": substrate.fed, "Produced": substrate.produced,
             "Unreleased": substrate.unreleased, "Avg Rate": substrate.avg_rate(t1),
             "Peak Rate": substrate.peak}
            for conversion in system.conversions for substrate in conversion.substrates]
    return pd.DataFrame(rows, columns=_CONVERSION_SUMMARY_COLS)


def summarize_components(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "component_summary" frame (Bio-1 G2): one row per component of each
    blended level.

    `Avg Fraction` is MASS-weighted (content area / level area), which is the
    only average of a ratio that means anything here and is exact, both curves
    being piecewise linear. `Total In`/`Total Out` are the component's own
    conserved totals: what came in minus what went out is exactly the change in
    `Final Content`, whatever the zero-order hold was worth.
    """
    system = env.ctx.level_system
    if system is None:
        return pd.DataFrame([], columns=_COMPONENT_SUMMARY_COLS)
    rows = [{"Run": run_id, "Component": f"{level.name}/{name}", "Level": level.name,
             "Avg Fraction": level.avg_fraction(name), "Min Fraction": level.frac_min(name),
             "Max Fraction": level.frac_max(name), "Final Fraction": level.fraction(name),
             "Total In": level.comp_in[name], "Total Out": level.comp_out[name],
             "Final Content": level.content[name]}
            for level in system.levels.values() for name in level.components]
    return pd.DataFrame(rows, columns=_COMPONENT_SUMMARY_COLS)


def flow_totals(env) -> tuple[float, float]:
    """(cost, impact) summed over every flow -- what joins the model's own
    Total Cost, exactly the way resource costs do. The demand charge is NOT in
    here: it is a lump sum, itemised in `cost_summary` (PHASE4 D5)."""
    system = env.ctx.level_system
    if system is None:
        return 0.0, 0.0
    return (sum(f.cost for f in system.flows), sum(f.impact for f in system.flows))


# -- lump-sum and fixed costs (PHASE4 D5, ARCHITECTURE D63) ------------------
_FLEET_SUMMARY_COLS = ["Run", "Fleet", "Vehicles", "Trips", "Loads per Trip", "Distance Loaded",
                       "Distance Empty", "Travel Loaded %", "Travel Empty %", "Load/Unload %", "Idle %",
                       "Avg Request Wait", "Max Request Wait"]
_SEGMENT_SUMMARY_COLS = ["Run", "Segment", "Length", "Traversals", "Distance Driven",
                         "Wait Time", "Max Occupancy"]
_CONVEYOR_SUMMARY_COLS = ["Run", "Conveyor", "Length", "Capacity", "Entered", "Exited",
                          "Avg Occupancy", "Max Occupancy", "Blocked %", "Avg Transit Time"]
# PHASE4 T5 (ARCHITECTURE D78): one row per BUFFER block and per station that
# blocks after service or has a finite input queue. The first four columns
# describe the block's INPUT SPACE (NaN for a station with none); `Blocked %`
# and `Starved %` describe the STATION (NaN for a plain buffer). Blocked time is
# inside the resource's busy time, so Usage % = Working % + Blocked % and the
# three fractions plus Down % and off-shift time add up to the whole window.
_BUFFER_SUMMARY_COLS = ["Run", "Block", "Resource", "Capacity", "Avg Content", "Max Content",
                        "Time Full %", "Entered", "Blocked %", "Starved %"]


def summarize_fleets(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "fleet_summary" frame (PHASE4 E1): one row per mover fleet.

    The fleet's UTILIZATION, seize count and costs are already in
    `resource_summary` -- a fleet IS a resource (ARCHITECTURE D68). What lives
    only here is what a vehicle pool adds on top: how far it drove loaded versus
    empty, and how its time split between the four states. The four percentages
    sum to 100 across all the vehicles in the pool.
    """
    fleets = env.ctx.fleets
    if not fleets:
        return pd.DataFrame([], columns=_FLEET_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = []
    for fleet in fleets.values():
        totals = fleet.totals(t1)
        rows.append({"Run": run_id, "Fleet": fleet.name, "Vehicles": fleet.count,
                     "Trips": totals["trips"],
                     "Loads per Trip": totals["loads_per_trip"],
                     "Distance Loaded": totals["distance_loaded"],
                     "Distance Empty": totals["distance_empty"],
                     "Travel Loaded %": totals["loaded_pct"],
                     "Travel Empty %": totals["empty_pct"],
                     "Load/Unload %": totals["handling_pct"],
                     "Idle %": totals["idle_pct"],
                     "Avg Request Wait": totals["avg_wait"],
                     "Max Request Wait": totals["max_wait"]})
    return pd.DataFrame(rows, columns=_FLEET_SUMMARY_COLS)


_CHARGING_SUMMARY_COLS = ["Run", "Charger", "Kind", "Points", "Sessions", "Utilization %",
                          "Avg Wait", "Avg Queue", "Grid kWh", "Peak kW", "Energy Cost"]
_BATTERY_SUMMARY_COLS = ["Run", "Fleet", "Capacity kWh", "Charging %", "Charge Trips", "Min SoC",
                         "Mean Final SoC", "Depleted", "Drain kWh"]


def summarize_charging(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "charging_summary" frame (D165): one row per charger. Times are in
    the model's base unit; grid energy is what was drawn up to the report
    instant, a session still running included."""
    _, t1 = observed_window(env, warmup)
    price = float(getattr(env.ctx, "energy_price", 0.0) or 0.0)
    rows = [{"Run": run_id, "Charger": c.name, "Kind": c.kind, "Points": c.capacity,
             "Sessions": c.sessions,
             "Utilization %": 100.0 * c.busy.time_avg(t1) / c.capacity,
             "Avg Wait": c.wait.mean, "Avg Queue": c.queue_len.time_avg(t1),
             "Grid kWh": round(c.energy(t1), 4), "Peak kW": c.peak_kw,
             "Energy Cost": round(c.energy(t1) * price, 4)}
            for c in env.ctx.chargers.values()]
    return pd.DataFrame(rows, columns=_CHARGING_SUMMARY_COLS)


def summarize_batteries(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "battery_summary" frame (D164, D166): one row per fleet with a
    battery -- what charging costs the fleet in time, and how close it ran to
    empty. SoC values are fractions, 0 to 1."""
    _, t1 = observed_window(env, warmup)
    rows = []
    for fleet in env.ctx.fleets.values():
        if fleet.capacity_kwh:
            totals = fleet.battery_totals(t1)
            rows.append({"Run": run_id, "Fleet": fleet.name, "Capacity kWh": fleet.capacity_kwh,
                         "Charging %": totals["charging_pct"],
                         "Charge Trips": totals["charge_trips"], "Min SoC": totals["min_soc"],
                         "Mean Final SoC": totals["final_soc"], "Depleted": totals["depleted"],
                         "Drain kWh": round(totals["drain_kwh"], 4)})
    return pd.DataFrame(rows, columns=_BATTERY_SUMMARY_COLS)


_TENDING_SUMMARY_COLS = ["Run", "Station", "Fleet", "Picks", "Places", "Exchanges",
                         "Avg Pick Time", "Avg Place Time", "Avg Loaded Wait",
                         "Avg Blocked For Pick", "Avg Stay"]


def summarize_tending(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "tending_summary" frame (D169-D170): one row per station and arm
    fleet that tended it. Times are in the model's base unit; a mean with no
    observation is NaN."""
    rows = [{"Run": run_id, "Station": station, "Fleet": fleet, "Picks": s.picks,
             "Places": s.places, "Exchanges": s.exchanges,
             "Avg Pick Time": s.pick_time.mean, "Avg Place Time": s.place_time.mean,
             "Avg Loaded Wait": s.loaded_wait.mean,
             "Avg Blocked For Pick": s.blocked_for_pick.mean, "Avg Stay": s.stay.mean}
            for (station, fleet), s in sorted(env.ctx.tending.items())]
    return pd.DataFrame(rows, columns=_TENDING_SUMMARY_COLS)


def summarize_segments(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "segment_summary" frame (PHASE4 E1): one row per network segment a
    vehicle actually crossed, summed over every fleet.

    `Wait Time` (total time movers stood waiting to enter) and `Max
    Occupancy` come from the zone control (PHASE6 W3 M5, D117), so they exist
    only for a segment with a `capacity`: an unlimited one reports 0 wait and
    NaN occupancy, which nothing tracks.
    """
    network = env.ctx.network
    if network is None or not env.ctx.fleets:
        return pd.DataFrame([], columns=_SEGMENT_SUMMARY_COLS)
    counts: dict[str, int] = {}
    traffic = next(iter(env.ctx.fleets.values())).traffic
    for fleet in env.ctx.fleets.values():
        for name, n in fleet.segment_traversals.items():
            counts[name] = counts.get(name, 0) + n
    rows = [{"Run": run_id, "Segment": seg.name, "Length": seg.length,
             "Traversals": counts.get(seg.name, 0),
             "Distance Driven": seg.length * counts.get(seg.name, 0),
             "Wait Time": traffic.wait[seg] if traffic and seg in traffic.wait else 0.0,
             "Max Occupancy": (traffic.max_occupancy[seg] if traffic and seg in traffic.wait
                               else float("nan"))}
            for seg in network.segments]
    return pd.DataFrame(rows, columns=_SEGMENT_SUMMARY_COLS)


def summarize_conveyors(env, *, warmup: float = 0.0,
                        run_id: Optional[int] = None) -> pd.DataFrame:
    """The "conveyor_summary" frame (PHASE4 E1): one row per CONVEYOR block."""
    conveyors = env.ctx.conveyors
    if not conveyors:
        return pd.DataFrame([], columns=_CONVEYOR_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = [{"Run": run_id, "Conveyor": belt.name, "Length": belt.belt_length,
             "Capacity": belt.capacity, "Entered": belt.entered, "Exited": belt.exited,
             "Avg Occupancy": belt.occupancy.time_avg(t1),
             "Max Occupancy": belt.occupancy.max,
             "Blocked %": 100.0 * belt.blocked.time_avg(t1),
             "Avg Transit Time": belt.transit.mean}
            for belt in conveyors.values()]
    return pd.DataFrame(rows, columns=_CONVEYOR_SUMMARY_COLS)


def summarize_buffers(env, *, warmup: float = 0.0,
                      run_id: Optional[int] = None) -> pd.DataFrame:
    """The "buffer_summary" frame (PHASE4 T5): one row per finite buffer and
    per station that blocks after service.

    Empty with its fixed columns for a model with no BUFFER and no
    `queue_capacity`, so no existing model's frames or golden hashes move
    (the A3/A4/D1/E1 precedent).
    """
    nodes = env.ctx.blocking_nodes
    if not nodes:
        return pd.DataFrame([], columns=_BUFFER_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = []
    for name, node in nodes.items():
        if name in env.ctx.conveyors:
            continue        # a belt's cells report in conveyor_summary (T7)
        space = node.space
        resource_name = blocking.resource_name_of(node)
        res = env.ctx.resource_manager.resolve(resource_name) if resource_name else None
        # The same denominator utilization uses, so the fractions are comparable.
        capacity_time = res.available_area(t1) if res is not None else 0.0
        blocked = getattr(node, "blocked", None)
        rows.append({
            "Run": run_id, "Block": name, "Resource": resource_name,
            "Capacity": space.capacity if space is not None else np.nan,
            "Avg Content": round(space.content.time_avg(t1), 4) if space is not None else np.nan,
            "Max Content": space.content.max if space is not None else np.nan,
            "Time Full %": round(100.0 * space.full.time_avg(t1), 2) if space is not None else np.nan,
            "Entered": space.entered if space is not None else np.nan,
            "Blocked %": (round(100.0 * blocked.area(t1) / capacity_time, 2)
                          if blocked is not None and capacity_time > 0 else np.nan),
            "Starved %": (round(100.0 * max(capacity_time - res.busy.area(t1), 0.0) / capacity_time, 2)
                          if res is not None and capacity_time > 0 else np.nan),
        })
    return pd.DataFrame(rows, columns=_BUFFER_SUMMARY_COLS)


# ------------------------------------------------------- entity flow / bottleneck (M2, D113)
_MATERIAL_FLOW_SUMMARY_COLS = ["Run", "From", "To", "Entities"]
_BOTTLENECK_SUMMARY_COLS = ["Run", "Resource", "Sole %", "Shifting %", "Total %"]


def summarize_material_flow(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "material_flow_summary" frame (PHASE6 W3 M2, ARCHITECTURE D113): one
    row per link an entity actually crossed via `BaseProcess.forward`, from
    `ctx.link_flow_counts` -- a plain dict incremented there and reset at
    warm-up like every other counter (`sim_runner.reset_statistics`).
    `svgcharts.sankey` draws it; EMPTY with its fixed columns for a model with
    no traversals (the A3/E1 precedent).

    Not named "flow_summary": that name already belongs to the PHASE4 D1
    rate-based level-flow frame (D113).
    """
    rows = [{"Run": run_id, "From": frm, "To": to, "Entities": n}
            for (frm, to), n in env.ctx.link_flow_counts.items()]
    return pd.DataFrame(rows, columns=_MATERIAL_FLOW_SUMMARY_COLS)


def summarize_bottleneck(env, *, warmup: float = 0.0,
                         run_id: Optional[int] = None) -> pd.DataFrame:
    """The "bottleneck_summary" frame (PHASE6 W3 M2, ARCHITECTURE D113): the
    Roser active-period shares, one row per resource ever ACTIVE in the
    observed window. EMPTY with its fixed columns unless the model sets
    `outputs.bottleneck` (the intervals cost memory proportional to the event
    count, so collection is opt-in) -- the A3/E1 precedent. The existing
    `resource_summary` "Bottleneck Score"/"Bottleneck %" columns are untouched.
    """
    tracker = env.ctx.bottleneck_tracker
    if tracker is None:
        return pd.DataFrame([], columns=_BOTTLENECK_SUMMARY_COLS)
    t0, t1 = observed_window(env, warmup)
    periods = tracker.closed_periods(t1)
    shares = bottleneck.active_period_shares(periods, t0, t1)
    rows = [{"Run": run_id, "Resource": name, **vals} for name, vals in shares.items()]
    return pd.DataFrame(rows, columns=_BOTTLENECK_SUMMARY_COLS)


# ---------------------------------------------------------------- schedule (W4 P2, D120)
_SCHEDULE_LOG_COLS = ["Run", "Order", "Entity", "Entity Type", "Block", "Resource",
                      "Unit", "Start", "End", "Setup"]


def summarize_schedule(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "schedule_log" frame (PHASE6 W4 P2, ARCHITECTURE D120): one row per
    resource hold, appended in `core/seizing.py`'s `release` to
    `ctx.schedule_log`. EMPTY with its fixed columns unless the model sets
    `outputs.schedule` (or runs in deterministic mode, which forces it on) --
    the A3/E1 precedent again. `svgcharts.gantt` draws it.
    """
    log_ = env.ctx.schedule_log
    if not log_:
        return pd.DataFrame([], columns=_SCHEDULE_LOG_COLS)
    rows = [{"Run": run_id, **row} for row in log_]
    return pd.DataFrame(rows, columns=_SCHEDULE_LOG_COLS)


# ------------------------------------------------------------- energy (M1, D112)
_ENERGY_SUMMARY_COLS = ["Run", "Resource", "Busy kWh", "Idle kWh", "Blocked kWh", "Down kWh",
                        "Off Shift kWh", "Total kWh", "Avg kW", "Energy Cost"]


def _blocked_area_by_resource(env, t1: float) -> dict[str, float]:
    """Per-resource blocked-after/before-service unit-time, summed across every
    blocking node that holds that resource -- the same collector
    `summarize_buffers`'s "Blocked %" reads (D78)."""
    out: dict[str, float] = {}
    for node in getattr(env.ctx, "blocking_nodes", {}).values():
        name = blocking.resource_name_of(node)
        if name:
            out[name] = out.get(name, 0.0) + node.blocked.area(t1)
    return out


def _resource_energy_rows(env, t0: float, t1: float) -> list[dict]:
    """One dict per resource/fleet declaring "power" (M1, D112): the five
    states' unit-time partition, kWh and cost.

    A resource earns a row whenever it declares power, whether or not
    anything ever seized it -- idle standby power is real, unlike
    `_reportable`'s "something happened" rule for `resource_summary`. States:
    BUSY (seized, not blocked), IDLE (scheduled but not seized), BLOCKED
    (seized and held after/before service, D78), DOWN (a failure's blocker)
    and OFF_SHIFT (a capacity schedule's blocker) -- they partition unit-time
    exactly as `summarize_buffers`'s "working + blocked + starved + down +
    off-shift" identity does (ARCHITECTURE 5.22). `down` is the unit-time a
    failure's blockers hold (`down_units`, #192), so a capacity-n failure
    counts n units, and units still finishing a job (WAIT) are not down yet.
    """
    base_unit = getattr(env.ctx, "time_unit", "minutes")
    hours_per_base = 1.0 / factor("hours", base_unit)
    price = float(getattr(env.ctx, "energy_price", 0.0) or 0.0)
    blocked_by_res = _blocked_area_by_resource(env, t1)
    observed_hours = max(t1 - t0, 0.0) * hours_per_base

    rows = []
    for name, res in env.ctx.resource_manager.all().items():
        power = getattr(res, "power", None)
        if not power:
            continue
        capacity_time = res.available_area(t1)
        busy_all = res.busy.area(t1)
        blocked = min(blocked_by_res.get(name, 0.0), busy_all)
        busy = max(busy_all - blocked, 0.0)
        idle = max(capacity_time - busy_all, 0.0)
        unavailable = max(res.capacity * (t1 - t0) - capacity_time, 0.0)
        down = min(res.down_units.area(t1), unavailable)
        off_shift = max(unavailable - down, 0.0)

        state_time = {"busy": busy, "idle": idle, "blocked": blocked,
                     "down": down, "off_shift": off_shift}
        kwh = {}
        for state in ENERGY_STATES:
            default_kw = power.get("busy", 0.0) if state == "blocked" else 0.0
            kw = float(power.get(state, default_kw))
            kwh[state] = kw * state_time[state] * hours_per_base
        total_kwh = sum(kwh.values())
        rows.append({
            "Resource": name,
            "Busy kWh": round(kwh["busy"], 4), "Idle kWh": round(kwh["idle"], 4),
            "Blocked kWh": round(kwh["blocked"], 4), "Down kWh": round(kwh["down"], 4),
            "Off Shift kWh": round(kwh["off_shift"], 4),
            "Total kWh": round(total_kwh, 4),
            "Avg kW": round(total_kwh / observed_hours, 4) if observed_hours > 0 else 0.0,
            "Energy Cost": round(total_kwh * price, 4),
        })
    # D165: a charger's grid energy, all of it "busy" -- what the battery
    # fleets it serves draw from the grid (their own resources declare none).
    for charger in env.ctx.chargers.values():
        total_kwh = charger.energy(t1)
        kwh = round(total_kwh, 4)
        rows.append({
            "Resource": charger.name, "Busy kWh": kwh, "Idle kWh": 0.0, "Blocked kWh": 0.0,
            "Down kWh": 0.0, "Off Shift kWh": 0.0, "Total kWh": kwh,
            "Avg kW": round(total_kwh / observed_hours, 4) if observed_hours > 0 else 0.0,
            "Energy Cost": round(total_kwh * price, 4),
        })
    return rows


def summarize_energy(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "energy_summary" frame (M1, D112): one row per resource or fleet
    that declares "power". EMPTY with its fixed columns for a model that
    declares none, so no existing model's frames or golden hash move (the
    A3/E1/T5 precedent). Resets at warm-up like every other summary."""
    t0, t1 = observed_window(env, warmup)
    rows = _resource_energy_rows(env, t0, t1)
    if not rows:
        return pd.DataFrame([], columns=_ENERGY_SUMMARY_COLS)
    for row in rows:
        row["Run"] = run_id
    return pd.DataFrame(rows, columns=_ENERGY_SUMMARY_COLS)


_COST_SUMMARY_COLS = ["Run", "Item", "Category", "Kind", "Amount"]
# The Category a demand-charge row gets: a flow declares a `peak_price`, not a
# category, and one literal beats a field nobody would ever set differently.
PEAK_COST_CATEGORY = "Demand charge"


def summarize_costs(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "cost_summary" frame (PHASE4 D5): one row per non-RATE cost.

    Four kinds, in this order: the "fixed_costs" section ("fixed", evaluated
    once at run start and never prorated to the observed window), whatever a
    RECORD with kind="cost" accrued ("event", reset at the warm-up like every
    other statistic), a flow's demand charge ("peak", Peak Rate x its
    `peak_price`), and a resource's kWh x `energy_price` ("energy", M1,
    D112). Their sum joins the system frame's Total Cost.

    EMPTY, with its fixed columns, for a model that uses none of them -- so no
    existing model's frames or golden hashes move, the A3/A4/D1 precedent.
    """
    rows = [{"Run": run_id, "Item": item.name, "Category": item.category,
             "Kind": item.kind, "Amount": round(item.amount, 4)}
            for item in getattr(env.ctx, "cost_items", {}).values()]
    rows.sort(key=lambda r: (r["Kind"] != "fixed", ))   # fixed first, else declaration order
    system = env.ctx.level_system
    if system is not None:
        rows += [{"Run": run_id, "Item": flow.name, "Category": PEAK_COST_CATEGORY,
                  "Kind": "peak", "Amount": round(flow.peak_cost, 4)}
                 for flow in system.flows if flow.peak_price]
    # Energy (M1, D112): only when the model actually prices it, mirroring the
    # peak rows' "only when peak_price is set" rule -- kWh with no price is
    # informational (see energy_summary), not a cost line.
    if getattr(env.ctx, "energy_price", 0.0):
        t0, t1 = observed_window(env, warmup)
        rows += [{"Run": run_id, "Item": r["Resource"], "Category": "", "Kind": "energy",
                  "Amount": r["Energy Cost"]}
                 for r in _resource_energy_rows(env, t0, t1) if r["Energy Cost"]]
    return pd.DataFrame(rows, columns=_COST_SUMMARY_COLS)


def lump_sum_total(env) -> float:
    """Every `cost_summary` amount, summed -- what joins Total Cost."""
    total = sum(item.amount for item in getattr(env.ctx, "cost_items", {}).values())
    system = env.ctx.level_system
    if system is not None:
        total += sum(flow.peak_cost for flow in system.flows)
    return total


_AGENT_SUMMARY_COLS = ["Run", "Agent State", "Population", "State", "Avg Count", "Min", "Max",
                       "Final", "Entries", "Avg Sojourn", "Dropped Messages"]


def summarize_agents(env, *, warmup: float = 0.0, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "agent_summary" frame (PHASE5 AB-1): one row per (population,
    state). `Avg Count` is the exact time-average of the piecewise-constant
    count; `Entries` the transitions fired INTO the state (initial placement
    excluded); `Avg Sojourn` the mean stay of the agents that left it. A
    composite state (PHASE6 K1) has its own row, counting only crossings of
    its boundary."""
    system = getattr(env.ctx, "agent_system", None)
    if system is None:
        return pd.DataFrame([], columns=_AGENT_SUMMARY_COLS)
    _, t1 = observed_window(env, warmup)
    rows = [{"Run": run_id, "Agent State": f"{row['Population']}/{row['State']}", **row}
            for pop in system.populations for row in pop.summary(t1)]
    return pd.DataFrame(rows, columns=_AGENT_SUMMARY_COLS)


_AGENT_ATTR_COLS = ["Run", "Agent Attribute", "Population", "Attribute", "State", "Category",
                   "End Mean", "End P05", "End P50", "End P95", "End Min", "End Max",
                   "Time-Avg Mean"]
_AGENT_ATTR_LOG_COLS = ["Run", "Population", "Attribute", "Value"]
_ALL_STATES = "All"


def _attr_stats_row(run_id, pop_name: str, attr: str, state: str, values: np.ndarray,
                    tavg: float) -> dict:
    if len(values):
        p05, p50, p95 = (float(v) for v in np.percentile(values, [5, 50, 95]))
        mean, vmin, vmax = float(values.mean()), float(values.min()), float(values.max())
    else:
        p05 = p50 = p95 = mean = vmin = vmax = np.nan
    return {"Run": run_id, "Agent Attribute": f"{pop_name}/{attr}/{state}", "Population": pop_name,
            "Attribute": attr, "State": state, "Category": "", "End Mean": mean, "End P05": p05,
            "End P50": p50, "End P95": p95, "End Min": vmin, "End Max": vmax,
            "Time-Avg Mean": tavg}


def _attr_category_rows(run_id, pop_name: str, attr: str, categories: list, state: str,
                        codes: np.ndarray) -> list:
    """PHASE6 A2: one row per declared category, its SHARE (fraction, so "End
    Mean" of a 0/1 indicator is exactly that) of *codes* -- the categorical
    counterpart of `_attr_stats_row`; the other stat columns and Time-Avg Mean
    stay NaN (a percentile of a category is not meaningful, and only C2's
    numeric running sum tracks a time integral). The "Agent Attribute"/KPI key
    folds the category in (.../attr=<category>/state) so flatten_kpis's
    existing generic id/column scan needs no change to pick these up too."""
    n = len(codes)
    return [{"Run": run_id, "Agent Attribute": f"{pop_name}/{attr}={cat}/{state}",
             "Population": pop_name, "Attribute": attr, "State": state, "Category": cat,
             "End Mean": float((codes == code).sum()) / n if n else np.nan,
             "End P05": np.nan, "End P50": np.nan, "End P95": np.nan, "End Min": np.nan,
             "End Max": np.nan, "Time-Avg Mean": np.nan}
            for code, cat in enumerate(categories)]


def summarize_agent_attributes(env, *, warmup: float = 0.0,
                               run_id: Optional[int] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The "agent_attributes" frame (PHASE6 C2, categories PHASE6 A2): one row
    per (population, attribute, state-or-"All") for a NUMERIC attribute --
    End Mean/P05/P50/P95/Min/Max over the agents alive at t1 (run end, after
    warm-up), Time-Avg Mean the exact time average of the population-wide
    mean (`_Population.attr_time_avg`, the same piecewise-constant-between-
    agent-events treatment `agent_summary`'s own Avg Count gets, "All" row
    only: tracking it per state too would need a second running sum per state
    for a number the per-state End Mean already approximates well) -- or one
    row per (population, attribute, state-or-"All", category) for a
    CATEGORICAL one, "End Mean" holding that category's share instead
    (`_attr_category_rows`).

    Also returns "agent_attribute_log" (one row per alive agent's own end
    value -- a category NAME for a categorical attribute, "All" rows only):
    the raw data the results window's histogram reads, the same role
    `entity_log` plays for the entity throughput chart.
    """
    system = getattr(env.ctx, "agent_system", None)
    if system is None:
        return (pd.DataFrame([], columns=_AGENT_ATTR_COLS),
                pd.DataFrame([], columns=_AGENT_ATTR_LOG_COLS))
    _, t1 = observed_window(env, warmup)
    rows, log_rows = [], []
    for pop in system.populations:
        names = pop.type.attr_names
        if not names:
            continue
        members = pop.members()
        member_states = pop.state[members]
        for name in names:
            values = pop.attrs[name][members]
            categories = pop.type.attr_cat.get(name)
            if categories is not None:
                log_rows += [{"Run": run_id, "Population": pop.name, "Attribute": name,
                              "Value": categories[int(v)]} for v in values]
                rows += _attr_category_rows(run_id, pop.name, name, categories, _ALL_STATES,
                                            values)
                for s, state in enumerate(pop.type.states):
                    rows += _attr_category_rows(run_id, pop.name, name, categories, state,
                                                values[pop.type.mask(member_states, s)])
            else:
                log_rows += [{"Run": run_id, "Population": pop.name, "Attribute": name,
                              "Value": float(v)} for v in values]
                rows.append(_attr_stats_row(run_id, pop.name, name, _ALL_STATES, values,
                                            pop.attr_time_avg(name, t1)))
                for s, state in enumerate(pop.type.states):
                    rows.append(_attr_stats_row(run_id, pop.name, name, state,
                                                values[pop.type.mask(member_states, s)], np.nan))
    return (pd.DataFrame(rows, columns=_AGENT_ATTR_COLS),
            pd.DataFrame(log_rows, columns=_AGENT_ATTR_LOG_COLS))


_TIMESERIES_COLS = ["Run", "Time", "Series", "Value", "Warmup"]


def summarize_timeseries(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "timeseries" frame (ROADMAP 1.7): one row per sample the sampler
    process took, tagged with the replication index. Empty (but correctly
    shaped) when sampling was off for this run."""
    rows = [{"Run": run_id, "Time": t, "Series": series, "Value": value, "Warmup": warm}
            for t, series, value, warm in getattr(env.ctx, "timeseries", [])]
    return pd.DataFrame(rows, columns=_TIMESERIES_COLS)


_DASHBOARD_COLS = ["Run", "Time", "Widget", "Series", "Value", "Warmup"]


def summarize_dashboard(env, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "dashboard" frame (PHASE6 G1, D132): one row per widget series at
    each instant the timeseries sampler took, tagged with the replication
    index. Empty (but correctly shaped) for a model with no dashboard, or
    when sampling was off for this run."""
    rows = [{"Run": run_id, "Time": t, "Widget": widget, "Series": series, "Value": value,
            "Warmup": warm}
            for t, widget, series, value, warm in getattr(env.ctx, "dashboard", [])]
    return pd.DataFrame(rows, columns=_DASHBOARD_COLS)


_GAME_LOG_COLS = ["Run", "Time", "Step", "Action", "Target", "Value"]


def summarize_game_log(entries, *, run_id: Optional[int] = None) -> pd.DataFrame:
    """The "game_log" frame of a played `SimSession` (PHASE6 G3, D133): one row
    per edit or decision, in order. Value is JSON text, so a list of DECIDE
    probabilities and a number share one column and the row reads back."""
    rows = [{"Run": run_id, "Time": e["t"], "Step": e["step"], "Action": e["action"],
             "Target": e["target"], "Value": json.dumps(e["value"])} for e in entries]
    return pd.DataFrame(rows, columns=_GAME_LOG_COLS)


def timeseries_mean(results: dict) -> pd.DataFrame:
    """Across-replication mean of the "timeseries" frame, one row per
    (Series, Time), with a Student-t 95% CI (`outputs.ci95`) and the number of
    replications actually observed at that sample time. Feeds the Excel
    workbook's "Time Series" sheet and the report's time-series charts -- the
    full per-replication frame is what CSV export writes instead."""
    cols = ["Series", "Time", "Mean", "LB", "UB", "N"]
    df = results.get("timeseries")
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    # `ci95`'s arithmetic, one groupby instead of one call per (series, time):
    # the per-group loop was the most expensive thing in a results refresh
    # and a report (tens of thousands of groups). Non-finite values are
    # dropped from the statistics as `ci95` drops them; N still counts every
    # non-NaN value, as before.
    values = df["Value"].astype(float)
    keys = [df["Series"], df["Time"]]
    n_obs = values.groupby(keys, sort=True).count()
    finite = values.where(np.isfinite(values)).groupby(keys, sort=True)
    mean, sd, n = finite.mean(), finite.std(ddof=1), finite.count()
    t = n.map(lambda k: _t_quantile(0.95, int(k) - 1) if k >= 2 else np.nan)
    half = t * sd / np.sqrt(n)
    out = pd.DataFrame({"Mean": mean, "LB": mean - half, "UB": mean + half, "N": n_obs.astype(int)})
    out.index.names = ["Series", "Time"]
    return out.reset_index()[cols]


_USER_SUMMARY_COLS = ["Run", "Statistic", "Kind", "Count", "Value", "Min", "Max", "Std"]

def summarize_user_stats(env, *, warmup: float = 0.0, run_id: Optional[int] = None):
    """User-defined statistics (RECORD block, ROADMAP 1.3): one row per
    `ctx.user_stats` entry. Value is the collector's natural summary: a
    count's running total, a tally's (or time_between's) mean, or a
    time_persistent's time-average level. Count/Min/Max/Std come along for
    free where the collector tracks them; NaN where it does not (a running
    total has no min/max/std, a time-average has no discrete observations)."""
    _, t1 = observed_window(env, warmup)
    rows = []
    for name, stat in env.ctx.user_stats.items():
        c = stat.collector
        if stat.kind == "count":
            row = {"Count": c.n, "Value": c.total, "Min": np.nan, "Max": np.nan, "Std": np.nan}
        elif stat.kind == "time_persistent":
            row = {"Count": np.nan, "Value": c.time_avg(t1), "Min": np.nan, "Max": c.max, "Std": np.nan}
        else:  # tally, time_between
            row = {"Count": c.count, "Value": c.mean, "Min": c.min, "Max": c.max, "Std": c.std}
        rows.append({"Run": run_id, "Statistic": name, "Kind": stat.kind, **row})

    return pd.DataFrame(rows, columns=_USER_SUMMARY_COLS)

# ------------------------------------------------------------------
# 95 % CI helper - ***tall / tidy*** format
# ------------------------------------------------------------------
_CI_COLS = ["KPI", "Mean", "SD", "SEM", "LB", "UB"]

def ci95_by_object_long(df: pd.DataFrame, *, obj_col: str) -> pd.DataFrame:
    """
    Return one *tidy* row per  (object x KPI)  with
    Mean, SD, SEM, 95 % LB, 95 % UB, from the shared Student-t helper
    (DEVLOG #19, #34: ddof=1, t-quantile, LB <= UB, NaN instead of "N/A").

    Columns:  [obj_col, "KPI", "Mean", "SD", "SEM", "LB", "UB"]
    """
    cols = [obj_col] + _CI_COLS
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols)

    num_cols = [c for c in df.select_dtypes("number").columns if c not in ("Run", obj_col)]
    if not num_cols:
        return pd.DataFrame(columns=cols)

    rows = []
    for obj, grp in df.groupby(obj_col, sort=False):
        for kpi in num_cols:
            st = ci95(grp[kpi])
            scale = 100.0 if kpi == "Bottleneck %" else 1.0   # cosmetic: flag -> percentage
            rows.append({obj_col: str(obj), "KPI": kpi,
                         "Mean": st["mean"] * scale, "SD": st["sd"] * scale,
                         "SEM": st["sem"] * scale, "LB": st["lb"] * scale,
                         "UB": st["ub"] * scale})

    return pd.DataFrame(rows, columns=cols)

# ------------------------------------------------------------------
#  Generic stats helpers
# ------------------------------------------------------------------

def pct_stats(series: list[float]) -> dict[str, float]:
    if not series:
        return {"p95": np.nan, "p99": np.nan, "std": np.nan}
    arr = np.asarray(series, dtype=float)
    return {
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else np.nan,
    }

def mode_stats(seq: list[float | int]) -> tuple[float, float]:
    """
    Return (mode_value, mode_frequency %) or (nan, nan) if *seq* empty.

    - ties -> first value given by Counter.most_common
    - frequency % = occurrences / len(seq) x 100
    """
    if not seq:
        return (np.nan, np.nan)
    cnt = Counter(seq)
    mode_val, freq = cnt.most_common(1)[0]
    return (mode_val, 100.0 * freq / len(seq))

def filter_warmup(records, warmup: float):
    """Keep the log records stamped at or after the warm-up end.

    Only the flat Excel log sheets (and the mode/percentile cosmetics) use this;
    KPIs come from collectors that were reset at warm-up (DEVLOG #22).
    """
    if warmup <= 0:
        return records
    return [r for r in records if r.time >= warmup]
