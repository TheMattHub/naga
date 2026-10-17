"""Planning and scheduling (Phase 6D W4, `docs/history/PHASE6-6D-W4-PLAN-2026-09-25.md`).

`plan_export` is the MES hand-off (ARCHITECTURE D120): it turns a "schedule_log"
frame -- one row per resource hold, ARCHITECTURE 5.16/5.23's D81 dates
included when the frame carries them -- into the plain CSV or JSON a planning
system imports. D71 stays read-only: this module only ever WRITES a file the
caller asked for, never reaches into a model or a plant.

`plan_risk` (ARCHITECTURE D121) answers the planner's second question, "how
risky is the plan": one deterministic run (D119) gives the plan, N stochastic
replications of the SAME layout score it.

Top-level module beside `report.py`/`exporters.py` (pandas allowed, no
PySide6): `ui -> planning -> core`, nothing in `core` imports it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from nagasim._cli import add_model_argument, setup_cli

__all__ = ["plan_export", "plan_risk", "main"]

# "order_risk" columns (D121): Planned End is the deterministic plan's
# Completion Time; the rest summarize the stochastic replications' own
# Completion Time for that order.
ORDER_RISK_COLS = ["Order", "Due", "Planned End", "P(On Time)", "Mean Lateness",
                   "P90 Lateness", "Mean End"]

# schedule_log column -> plan_export column. "Block" becomes "Operation": the
# planning vocabulary for what a station does, `schedule_log`'s for where it
# happens -- same value, different name for a different reader.
_COLUMNS = {"Order": "Order", "Block": "Operation", "Resource": "Resource",
           "Start": "Planned Start", "End": "Planned End"}
# Added only when the input frame already carries them (`export.with_datetimes`,
# ARCHITECTURE 5.23/D81) -- beside the numeric columns, never replacing them.
_DATE_COLUMNS = {"Start (date)": "Planned Start Date", "End (date)": "Planned End Date"}


def plan_export(frame: pd.DataFrame, path) -> Path:
    """Write *frame* (a "schedule_log"-shaped DataFrame) to *path* as the plan
    an MES imports: columns Order, Operation, Resource, Planned Start, Planned
    End, plus Planned Start/End Date when *frame* already has the "Start
    (date)"/"End (date)" columns `core.export.with_datetimes` adds for a
    model with a calendar (ARCHITECTURE D81).

    CSV or JSON by *path*'s extension (JSON: one record per row, ISO
    timestamps for the date columns -- pandas' own `to_json` date handling).

    Security (netguard-style review, ARCHITECTURE 7.6): writes ONLY *path*,
    the caller's own argument -- a model file has no key that could steer it.
    """
    path = Path(path)
    out = pd.DataFrame({dst: frame[src] for src, dst in _COLUMNS.items() if src in frame.columns})
    for src, dst in _DATE_COLUMNS.items():
        if src in frame.columns:
            out[dst] = frame[src]

    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(out.to_dict(orient="records"), indent=2, default=str))
    else:
        out.to_csv(path, index=False)
    return path


def plan_risk(layout, replications: int, *, seed: int = 0, workers: int | str = 1) -> pd.DataFrame:
    """The "order_risk" frame (ARCHITECTURE D121): the deterministic plan for
    *layout* (D119) plus *replications* ordinary stochastic replications of
    the SAME layout, scored against it -- "how risky is the plan".

    One row per order (from the deterministic plan's `order_summary`, ARCHITECTURE
    5.16): Order, Due, Planned End (the plan's own Completion Time), P(On Time),
    Mean Lateness, P90 Lateness, Mean End -- the last four read off that order's
    Completion Time across the stochastic replications' `order_summary`.
    Lateness is SIGNED (`end - due`, `core/outputs.py`'s "Lateness", never the
    clipped "Tardiness"); an order is on time when `end <= due`. An order left
    `incomplete` in a replication (ARCHITECTURE 5.16) has no Completion Time
    (NaN): it counts as LATE for P(On Time) (`NaN <= due` is `False`, exactly
    the wanted reading) but is excluded, like any NaN, from the Mean/P90
    Lateness and Mean End averages -- there is no end to average in. An order
    with no `due` has P(On Time) and both lateness columns NaN (#208).

    Stochastic replications keep the model's own randomness and queue
    discipline: only the plan run is forced deterministic. *workers* > 1
    spreads them over a process pool (`api.run_model`'s own machinery, as any
    other experiment helper). A model with no orders returns an EMPTY frame
    with the fixed columns (the A3/E1 precedent).
    """
    from nagasim import api

    plan = api.run_model(layout, replications=1, seed=seed, deterministic=True)
    plan_orders = plan["order_summary"] if plan is not None else pd.DataFrame()
    if plan_orders.empty:
        return pd.DataFrame(columns=ORDER_RISK_COLS)

    stochastic = api.run_model(layout, replications=replications, seed=seed,
                                workers=workers, deterministic=False)
    runs = stochastic["order_summary"] if stochastic is not None else pd.DataFrame()

    rows = []
    for _, plan_row in plan_orders.iterrows():
        order_id = plan_row["Order"]
        due = plan_row["Due"]
        ends = runs.loc[runs["Order"] == order_id, "Completion Time"].to_numpy(dtype=float) \
            if not runs.empty else np.array([])

        p_on_time = mean_late = p90_late = mean_end = np.nan
        if ends.size and not np.all(np.isnan(ends)):
            mean_end = float(np.nanmean(ends))
        if ends.size and not pd.isna(due):          # no due date: nothing to be late for (#208)
            p_on_time = float(np.mean(ends <= due))   # NaN end compares False: unfinished = late
            lateness = ends - due
            if not np.all(np.isnan(lateness)):
                mean_late = float(np.nanmean(lateness))
                p90_late = float(np.nanpercentile(lateness, 90))

        rows.append({
            "Order": order_id, "Due": due, "Planned End": plan_row["Completion Time"],
            "P(On Time)": p_on_time, "Mean Lateness": mean_late,
            "P90 Lateness": p90_late, "Mean End": mean_end,
        })
    return pd.DataFrame(rows, columns=ORDER_RISK_COLS)


def main(argv=None) -> int:
    """`naga plan model.json [--orders orders.csv] [--propose OBJECTIVE]
    [--risk N] [--export plan.csv] [--report out.html]` (PHASE6 W4 P5,
    ARCHITECTURE D123): answers a planner's three questions in one command --
    the deterministic plan (D119/D120), optionally a CP-SAT proposal to
    replace it (D122), optionally the proposal's (or the as-is plan's) risk
    (D121) -- then exports the plan and/or writes an HTML report with the
    Gantt and the risk table. Dispatched from `nagasim.cli` without
    importing Qt. All paths are the caller's own arguments, never read from
    the model file (ARCHITECTURE 7.6)."""
    setup_cli(level=None)

    from nagasim import cpsat  # noqa: F401 -- import fails only inside .propose() itself (D122)

    parser = argparse.ArgumentParser(
        prog="naga plan",
        description="What is the plan, is there a better one, and how risky is it -- the "
                    "deterministic schedule, an optional CP-SAT proposal, and optional risk.")
    add_model_argument(parser)
    parser.add_argument("--orders", help="CSV replacing the model's \"orders\" section")
    parser.add_argument("--propose", choices=cpsat.OBJECTIVES, default=None,
                        help="propose a plan with CP-SAT instead of the as-is layout "
                             "(needs the optional 'ortools' package: pip install \"nagasim[plan]\")")
    parser.add_argument("--time-limit", type=float, default=10.0, dest="time_limit",
                        help="CP-SAT time limit in seconds (default: 10)")
    parser.add_argument("--risk", type=int, default=None, metavar="N",
                        help="score the plan with N stochastic replications")
    parser.add_argument("--workers", type=int, default=1, help="risk replications run in parallel")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--export", help="write the plan here: .csv or .json by extension")
    parser.add_argument("--report", help="write an HTML report (Gantt + risk table) here")
    args = parser.parse_args(argv)

    from nagasim import api
    from nagasim.core.orders import orders_from_csv

    path = Path(args.model)
    layout = api.load_layout(path)
    if args.orders:
        layout["orders"] = orders_from_csv(Path(args.orders).read_text(encoding="utf-8"))

    results = api.run_model(layout, replications=1, seed=args.seed, deterministic=True)
    print((results["schedule_log"] if results is not None else pd.DataFrame()).to_string(index=False))

    if args.propose:
        try:
            proposal = cpsat.propose(layout, objective=args.propose, time_limit_s=args.time_limit,
                                     seed=args.seed)
        except ImportError as exc:
            print(f"--propose: {exc}", file=sys.stderr)
            return 2
        print(f"\nProposed ({proposal.status}): {args.propose} = {proposal.objective:g}, "
             f"makespan = {proposal.makespan:g}")
        for reason in proposal.refused:
            print(f"  refused: {reason}")
        layout = proposal.layout
        results = api.run_model(layout, replications=1, seed=args.seed, deterministic=True)
        print()
        print(results["schedule_log"].to_string(index=False))

    risk = None
    if args.risk:
        risk = plan_risk(layout, args.risk, seed=args.seed, workers=args.workers)
        print()
        print(risk.to_string(index=False))

    if args.export:
        out = plan_export(results["schedule_log"], args.export)
        print(f"\nPlan written to {out}")

    if args.report:
        from nagasim.report import build_report

        build_report(layout, results, plan_risk=risk, title=path.stem, path=args.report)
        print(f"Report written to {args.report}")
    return 0
