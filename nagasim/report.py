"""Client-facing HTML report (ROADMAP 1.7 B).

Top-level module beside `api.py`/`experiments.py`: it needs pandas and sits
above the engine, and it must stay free of PySide6/matplotlib (no new
dependency at all -- charts are plain inline SVG from `nagasim.svgcharts`).

    from nagasim.report import build_report
    html_text = build_report(layout, results, title="My model", path="report.html")

*layout* is a migrated model dict (schema.migrate output); *results* is a
`nagasim.api.run_model`-shaped dict of DataFrames; *experiment*, when given, is
a `nagasim.experiments.ExperimentResult` and adds the scenario section.

Every model-provided string (block/resource/entity/statistic names,
descriptions, expressions, validation messages) is HTML-escaped before it goes
into the page -- see `esc`, used everywhere such a string is embedded.
"""
from __future__ import annotations

import datetime
import html
import math
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from nagasim import __version__
from nagasim.core import outputs, schema
from nagasim.core.export import calendar_of
from nagasim.core.calendar import date_ticks, run_horizon
from nagasim.core.stats import ci95
from nagasim import svgcharts

__all__ = ["build_report", "has_costs", "esc", "fmt", "table"]     # the last three: for `extra_sections`

_HIST_BINS = 20
_MAX_QUEUE_CHARTS = 6


def esc(value) -> str:
    return html.escape(str(value))


def fmt(value) -> str:
    """The one number formatter used by every table and the header (ROADMAP 1.8
    PART 0 b): an integer or an integer-valued float (e.g. a replication-mean
    count that happens to land exactly on a whole number) prints with a
    thousands separator and no decimals ("5", "2,400"); any other finite
    number prints with 3 decimals and a thousands separator. NaN/None/
    non-finite as '-'."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    try:
        xf = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if not math.isfinite(xf):
        return "-"
    if xf == int(xf) and abs(xf) < 1e15:
        return f"{int(xf):,}"
    return f"{xf:,.3f}"


def _kpi_label(kpi: str) -> str:
    """Readable label for a `flatten_kpis` key (ROADMAP 1.8 PART 0 c):
        system/Avg Queue Length          -> "Avg Queue Length (system)"
        user/time_in_system              -> "time_in_system (user)"
        resource/Agents/Usage % (snapshot) -> "Usage % (snapshot) - Agents"
    Falls back to the raw key for anything else."""
    prefix, sep, rest = kpi.partition("/")
    if not sep:
        return kpi
    if prefix in ("system", "user"):
        return f"{rest} ({prefix})"
    if prefix in ("resource", "store", "entity"):
        obj, sep2, column = rest.partition("/")
        return f"{column} - {obj}" if sep2 else kpi
    return kpi


def _kpi_cell(kpi: str) -> str:
    """Table cell showing the readable label with the raw key as a tooltip."""
    return f'<span title="{esc(kpi)}">{esc(_kpi_label(kpi))}</span>'


def table(headers: list[str], rows: list[list[str]], *, empty="No data") -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    if not rows:
        return (f'<table><thead><tr>{head}</tr></thead><tbody><tr>'
                f'<td colspan="{len(headers)}">{esc(empty)}</td></tr></tbody></table>')
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


# ---------------------------------------------------------------- data extraction
def _system_rows(results: dict) -> list[tuple]:
    df = results.get("system")
    if df is None or df.empty:
        return []
    rows = []
    for metric in df.loc[df["Metric"] != "Run", "Metric"].drop_duplicates():
        values = pd.to_numeric(df.loc[df["Metric"] == metric, "Value"], errors="coerce")
        st = ci95(values)
        half = (st["ub"] - st["mean"]) if math.isfinite(st["ub"]) and math.isfinite(st["mean"]) else float("nan")
        rows.append((metric, st["mean"], half, float(values.min()), float(values.max())))
    return rows


def _pivot_means(frame: Optional[pd.DataFrame], obj_col: str, kpi_names: list[str]) -> list[tuple]:
    """One row per object: its name plus the across-replication Mean for each
    of *kpi_names* (from `outputs.ci95_by_object_long`'s tidy KPI column)."""
    if frame is None or frame.empty:
        return []
    ci_long = outputs.ci95_by_object_long(frame, obj_col=obj_col)
    rows = []
    for obj, grp in ci_long.groupby(obj_col, sort=False):
        means = dict(zip(grp["KPI"], grp["Mean"]))
        rows.append((obj, *(means.get(k, float("nan")) for k in kpi_names)))
    return rows


_RESOURCE_KPIS = ["Usage % (snapshot)", "Scheduled Capacity Avg", "Down %",
                  "Queue Length Avg", "Queue Length Max", "Time in Queue Avg (incl. waiting)"]
_STORE_KPIS = ["Average Size", "Max Size", "Avg Time in Store", "Util % (time-weighted)"]
_ENTITY_KPIS = ["Total Created", "Total Completed", "Avg Time", "P95 Time", "Std Time"]
_USER_KPIS = ["Value", "Count", "Min", "Max", "Std"]

# ---------------------------------------------------------------- costs (ROADMAP 2.2)
_RESOURCE_COST_KPIS = ["Busy Cost", "Idle Cost", "Usage Cost", "Total Cost"]
_ENTITY_COST_KPIS = ["Avg Cost", "Total Cost"]
_SYSTEM_COST_METRICS = ["Total Resource Cost", "Total Entity Holding Cost", "Total Cost",
                        "Cost per Completed Entity"]


def has_costs(results: dict) -> bool:
    """Any cost input non-zero anywhere in the run (ROADMAP 2.2): the section
    is worth showing only then, same rule the report already applies to
    validation warnings (nothing to say, nothing shown)."""
    res_sum = results.get("resource_summary")
    if res_sum is not None and "Total Cost" in res_sum.columns and (res_sum["Total Cost"] != 0).any():
        return True
    ent_sum = results.get("entity_summary")
    if ent_sum is not None and "Total Cost" in ent_sum.columns and (ent_sum["Total Cost"] != 0).any():
        return True
    # Lump-sum costs (PHASE4 D5): a purely continuous model has neither a
    # resource nor an entity, and its whole cost study lives in this frame.
    costs = results.get("cost_summary")
    return costs is not None and not costs.empty


def _cost_kinds(results: dict) -> dict:
    df = results.get("cost_summary")
    if df is None or df.empty:
        return {}
    return dict(df.drop_duplicates("Item")[["Item", "Kind"]].to_records(index=False))


def _user_kinds(results: dict) -> dict:
    df = results.get("user_summary")
    if df is None or df.empty:
        return {}
    return dict(df.drop_duplicates("Statistic")[["Statistic", "Kind"]].to_records(index=False))


# ---------------------------------------------------------------- model summary
def _model_summary_html(layout: dict) -> str:
    parts = []

    counts: dict[str, int] = {}
    for block in layout.get("blocks", []):
        btype = block.get("type", "?")
        counts[btype] = counts.get(btype, 0) + 1
    block_rows = [[esc(t), str(n)] for t, n in sorted(counts.items())]
    parts.append("<h3>Blocks</h3>" + table(["Type", "Count"], block_rows))

    res_rows = []
    for r in layout.get("resources", []):
        res_rows.append([esc(r.get("name", "")), fmt(r.get("capacity")),
                         esc(r.get("discipline", "FIFO")), esc(r.get("capacity_schedule") or "-")])
    parts.append("<h3>Resources</h3>" +
                table(["Name", "Capacity", "Discipline", "Capacity schedule"], res_rows))

    sched_rows = [[esc(s.get("name", "")), esc(s.get("kind", "")),
                  str(bool(s.get("repeat"))), str(len(s.get("points", [])))]
                 for s in layout.get("schedules", [])]
    if sched_rows:
        parts.append("<h3>Schedules</h3>" +
                    table(["Name", "Kind", "Repeats", "Points"], sched_rows))

    fail_rows = [[esc(f.get("name", "")), esc(", ".join(f.get("resources", []))),
                 esc(f.get("uptime", {}).get("distribution", "-")),
                 esc(f.get("downtime", {}).get("distribution", "-"))]
                for f in layout.get("failures", [])]
    if fail_rows:
        parts.append("<h3>Failures</h3>" +
                    table(["Name", "Resources", "Uptime", "Downtime"], fail_rows))

    var_rows = [[esc(v.get("name", "")), esc(v.get("initial", 0))]
               for v in layout.get("variables", [])]
    if var_rows:
        parts.append("<h3>Variables</h3>" + table(["Name", "Initial value"], var_rows))

    return "".join(parts)


def _validation_html(layout: dict) -> str:
    # By severity, not the message text (#247): two warning codes carry no
    # "warning: " text prefix, which used to drop them from this section.
    warnings = [p for p in schema.validate_model_structured(layout) if p.severity == "warning"]
    if not warnings:
        return ""
    items = "".join(f"<li>{esc(m)}</li>" for m in schema.format_problems(warnings))
    return f'<section><h2>Validation warnings</h2><ul class="warnings">{items}</ul></section>'


# ---------------------------------------------------------------- charts
def time_axis(calendar, times) -> dict:
    """The x-axis keyword arguments for a time chart: a DATE axis when the
    model has a calendar (PHASE4 T10), the plain numeric one otherwise.

    One helper, shared with `ui/results.py`, so a report chart and the results
    window label their axes identically.
    """
    values = [float(t) for t in times if t is not None]
    if calendar is None or not values:
        return {"x_title": "Time", "x_ticks": None}
    return {"x_title": f"Date ({getattr(calendar.tz, 'key', 'UTC')})",
            "x_ticks": date_ticks(calendar, min(values), max(values))}


def _wip_chart(mean_df: "pd.DataFrame", warmup: float, calendar=None) -> str:
    wip = mean_df[mean_df["Series"] == "system/WIP"].sort_values("Time")
    if wip.empty:
        return svgcharts.line_chart("WIP over time", [], {}, "Time", "WIP")
    markers = [warmup] if warmup and warmup > 0 else None
    axis = time_axis(calendar, wip["Time"])
    return svgcharts.line_chart("WIP over time", wip["Time"].tolist(), {"WIP": wip["Mean"].tolist()},
                                axis["x_title"], "Work in process",
                                band=(wip["LB"].tolist(), wip["UB"].tolist()), markers=markers,
                                x_ticks=axis["x_ticks"])


def _queue_charts(mean_df: "pd.DataFrame", calendar=None) -> tuple[list[str], list[str]]:
    """(chart svgs, overflow resource names) for resources with a queue series,
    capped at `_MAX_QUEUE_CHARTS`. Both chart helpers take the ALREADY-averaged
    frame: `timeseries_mean` is the most expensive thing in a report, and
    building it twice per report bought nothing."""
    names = sorted({s.split("/")[1] for s in mean_df["Series"].unique()
                    if s.startswith("resource/") and s.endswith("/queue")})
    shown, overflow = names[:_MAX_QUEUE_CHARTS], names[_MAX_QUEUE_CHARTS:]
    charts = []
    for name in shown:
        rows = mean_df[mean_df["Series"] == f"resource/{name}/queue"].sort_values("Time")
        axis = time_axis(calendar, rows["Time"])
        charts.append(svgcharts.line_chart(f"Queue length: {name}", rows["Time"].tolist(),
                                           {"Queue": rows["Mean"].tolist()}, axis["x_title"],
                                           "Queue length", x_ticks=axis["x_ticks"]))
    return charts, overflow


def _throughput_histogram(results: dict) -> str:
    log_df = results.get("entity_log")
    if log_df is None or log_df.empty:
        return svgcharts.histogram("Entity throughput time", [], [], "Throughput time", "Count")
    times = pd.to_numeric(log_df["Throughput Time"], errors="coerce").dropna()
    if times.empty:
        return svgcharts.histogram("Entity throughput time", [], [], "Throughput time", "Count")
    counts, edges = np.histogram(times, bins=_HIST_BINS)
    return svgcharts.histogram("Entity throughput time", edges.tolist(), counts.tolist(),
                               "Throughput time", "Count")


def _utilization_chart(results: dict) -> str:
    res_sum = results.get("resource_summary")
    if res_sum is None or res_sum.empty:
        return svgcharts.bar_chart("Resource utilization", [], [], "Utilization %")
    ci = outputs.ci95_by_object_long(res_sum, obj_col="Resource")
    util = ci[ci["KPI"] == "Usage % (snapshot)"]
    categories = [str(r) for r in util["Resource"]]
    values = [float(v) for v in util["Mean"]]
    err = [(float(lb), float(ub)) for lb, ub in zip(util["LB"], util["UB"])]
    return svgcharts.bar_chart("Resource utilization %", categories, values, "Utilization %", err=err)


def _material_flow_chart(results: dict) -> str:
    """PHASE6 W3 M2, ARCHITECTURE D113: the Sankey of entity traversals,
    across-replication mean per (From, To) link -- `svgcharts.sankey` drops
    the zero-count ones and any replication where a link was never used."""
    frame = results.get("material_flow_summary")
    if frame is None or frame.empty:
        rows = []
    else:
        agg = frame.groupby(["From", "To"], as_index=False)["Entities"].mean()
        rows = list(agg.itertuples(index=False, name=None))
    return svgcharts.sankey(rows, "Entity flow")


def _schedule_html(results: dict) -> str:
    """PHASE6 W4 P2, ARCHITECTURE D120: the schedule Gantt, by resource and by
    order, shown only for a model that set `outputs.schedule` (or ran
    deterministic, which forces it on) -- "" otherwise, matching
    `_bottleneck_html`. One replication only (the first Run present):
    overlaying several runs' holds on one timeline is not a plan, it is
    noise -- `planning.plan_risk` (P3) is where the across-replication
    question belongs."""
    frame = results.get("schedule_log")
    if frame is None or frame.empty:
        return ""
    rows = frame[frame["Run"] == frame["Run"].min()].to_dict("records")
    return ("<section><h2>Schedule</h2>"
           '<div class="chart-grid">'
           f'<div class="chart">{svgcharts.gantt(rows, "resource", "Schedule by resource")}</div>'
           f'<div class="chart">{svgcharts.gantt(rows, "order", "Schedule by order")}</div>'
           "</div></section>")


def _plan_risk_html(df: pd.DataFrame) -> str:
    """PHASE6 W4 P3/P5, ARCHITECTURE D121/D123: `planning.plan_risk`'s
    "order_risk" frame, one row per order -- the plan CLI's `--risk N`."""
    def pct(v):
        return "-" if pd.isna(v) else f"{v:.2f}"

    rows = [[esc(r["Order"]), fmt(r["Due"]), fmt(r["Planned End"]), pct(r["P(On Time)"]),
             fmt(r["Mean Lateness"]), fmt(r["P90 Lateness"]), fmt(r["Mean End"])]
            for _, r in df.iterrows()]
    return ("<section><h2>Plan risk</h2>" +
           table(["Order", "Due", "Planned End", "P(On Time)", "Mean Lateness", "P90 Lateness",
                   "Mean End"], rows) + "</section>")


_BOTTLENECK_KPIS = ["Sole %", "Shifting %", "Total %"]


def _bottleneck_html(results: dict) -> str:
    """PHASE6 W3 M2, ARCHITECTURE D113: the Roser active-period shares, shown
    only for a model that set `outputs.bottleneck` -- "" otherwise, so a
    model without it gets no empty section."""
    frame = results.get("bottleneck_summary")
    if frame is None or frame.empty:
        return ""
    rows = [[esc(name), *[fmt(v) for v in vals]]
            for name, *vals in _pivot_means(frame, "Resource", _BOTTLENECK_KPIS)]
    return ("<section><h2>Bottleneck analysis</h2>"
           '<p class="note">Roser active-period method: a resource is ACTIVE while working, in '
           "changeover or under repair. Sole is time it alone holds the longest active period; "
           "Shifting is the wall-clock overlap with the resource that takes over next, credited to "
           "both.</p>"
           + table(["Resource"] + _BOTTLENECK_KPIS, rows) + "</section>")


# ---------------------------------------------------------------- costs section
def _costs_html(results: dict, currency: str) -> str:
    parts = [f"<section><h2>Costs ({esc(currency)})</h2>"]

    sys_rows_by_metric = dict((m, (mean, half)) for m, mean, half, _, _ in _system_rows(results))
    totals_rows = [[esc(m), fmt(sys_rows_by_metric.get(m, (float("nan"), float("nan")))[0]),
                   fmt(sys_rows_by_metric.get(m, (float("nan"), float("nan")))[1])]
                  for m in _SYSTEM_COST_METRICS]
    parts.append("<h3>System totals</h3>" +
                table(["Metric", "Mean", "95% CI half-width"], totals_rows))

    res_sum = results.get("resource_summary")
    resource_rows = [[esc(name), *[fmt(v) for v in vals]]
                     for name, *vals in _pivot_means(res_sum, "Resource", _RESOURCE_COST_KPIS)]
    parts.append("<h3>Cost per resource</h3>" +
                table(["Resource"] + _RESOURCE_COST_KPIS, resource_rows))

    if res_sum is not None and not res_sum.empty:
        means = _pivot_means(res_sum, "Resource", ["Busy Cost", "Idle Cost", "Usage Cost"])
        names = [str(r[0]) for r in means]
        chart = svgcharts.stacked_bar_chart(
            "Cost per resource", names,
            {"Busy": [r[1] for r in means], "Idle": [r[2] for r in means],
             "Usage": [r[3] for r in means]}, "Cost")
        parts.append(f'<div class="chart-grid"><div class="chart">{chart}</div></div>')

    entity_rows = [[esc(name), *[fmt(v) for v in vals]]
                  for name, *vals in _pivot_means(results.get("entity_summary"),
                                                   "Entity Type", _ENTITY_COST_KPIS)]
    parts.append("<h3>Cost per entity type</h3>" +
                table(["Entity type"] + _ENTITY_COST_KPIS, entity_rows))

    # Lump-sum, fixed and peak costs (PHASE4 D5): shown only when the model has
    # some, and as the mean over replications -- a fixed amount is the same in
    # every one, an event total is not.
    lump = _pivot_means(results.get("cost_summary"), "Item", ["Amount"])
    if lump:
        kinds = _cost_kinds(results)
        parts.append("<h3>Fixed and lump-sum costs</h3>" +
                    table(["Item", "Kind", "Amount"],
                           [[esc(str(name)), esc(kinds.get(name, "")), fmt(vals[0])]
                            for name, *vals in lump]))

    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- scenario section
_CURATED_PREFIXES = ("system/",)


def _curated(kpi: str) -> bool:
    return (kpi.startswith(_CURATED_PREFIXES) or kpi.startswith("user/")
            or (kpi.startswith("resource/") and kpi.endswith("/Usage % (snapshot)")))


def _experiment_html(layout: dict, experiment) -> str:
    parts = ["<section><h2>Scenarios</h2>"]

    scenarios = layout.get("scenarios", [])
    # D578: the goal column exists only when a scenario has one, so reports without goals do not change.
    with_goal = any(str(e.get("goal") or "").strip() for e in scenarios)
    scenario_rows = []
    for entry in scenarios:
        name = entry.get("name", "")
        goal = str(entry.get("goal") or "").strip()[:500]
        overrides = entry.get("overrides", []) or [{}]
        for i, ov in enumerate(overrides):
            row = [esc(name) if i == 0 else "", esc(ov.get("path", "-")), esc(ov.get("value", "-"))]
            if with_goal:
                row.append(esc(goal) if i == 0 else "")
            scenario_rows.append(row)
    parts.append("<h3>Scenario definitions</h3>" +
                table(["Scenario", "Override path", "Value"] + (["Goal"] if with_goal else []),
                       scenario_rows, empty="No overrides declared (Baseline only)"))

    comparison = experiment.comparison() if experiment.results else pd.DataFrame()
    curated = comparison[comparison["KPI"].apply(_curated)] if not comparison.empty else comparison
    comp_rows = []
    for _, row in curated.iterrows():
        marker = " *" if row["Significant"] else ""
        comp_rows.append([_kpi_cell(row["KPI"]), esc(row["Scenario"]) + marker, fmt(row["Mean"]),
                          fmt(row["LB"]), fmt(row["UB"]), fmt(row["Diff vs baseline"]),
                          fmt(row["Diff LB"]), fmt(row["Diff UB"])])
    parts.append("<h3>Comparison</h3>"
                "<p class=\"note\">* marks a difference whose confidence interval excludes zero "
                f"({'paired, common random numbers' if experiment.crn else 'independent samples'}).</p>" +
                table(["KPI", "Scenario", "Mean", "LB", "UB", "Diff vs baseline", "Diff LB", "Diff UB"],
                       comp_rows))

    charts = []
    if not curated.empty:
        for kpi in sorted(curated[curated["KPI"].str.startswith("system/")]["KPI"].unique()):
            rows = curated[curated["KPI"] == kpi]
            categories = [str(s) for s in rows["Scenario"]]
            values = [float(v) for v in rows["Mean"]]
            err = [(float(lb), float(ub)) for lb, ub in zip(rows["LB"], rows["UB"])]
            label = _kpi_label(kpi)
            charts.append((kpi, svgcharts.bar_chart(label, categories, values, label, err=err)))
    if charts:
        parts.append("<h3>System KPIs by scenario</h3><div class=\"chart-grid\">" +
                    "".join(f'<div class="chart" title="{esc(kpi)}">{c}</div>' for kpi, c in charts) +
                    "</div>")

    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- DOE / optimization (2.1)
_MAX_REPORT_RESPONSES = 3       # a DOE run can carry every system KPI; a report needs a few
_MAX_HISTORY_ROWS = 60


def _frame_table(frame: pd.DataFrame, columns=None, limit: int | None = None) -> str:
    """A DataFrame straight into the report's own table style."""
    columns = list(columns or frame.columns)
    rows = [[fmt(row[c]) if isinstance(row[c], (int, float)) else esc(row[c]) for c in columns]
            for _, row in (frame.head(limit) if limit else frame).iterrows()]
    return table(columns, rows)


def _main_effects_chart(effects: pd.DataFrame, factor: str, response: str) -> str:
    """One line per factor: the mean response at each of its levels.

    Numeric levels keep their own scale; categorical ones fall back to their
    position, since a line chart needs an x axis either way.
    """
    rows = effects[effects["Factor"] == factor]
    labels = list(rows["Level"])
    try:
        x = [float(v) for v in labels]
    except (TypeError, ValueError):
        x = list(range(len(labels)))
    return svgcharts.line_chart(f"{factor}", x, {_kpi_label(response): [float(v) for v in rows["Mean"]]},
                                factor, _kpi_label(response),
                                band=([float(v) for v in rows["LB"]], [float(v) for v in rows["UB"]]))


def _design_html(design) -> str:
    """ROADMAP 2.1 F: the design table, then per response the main-effects
    table and one chart per factor."""
    parts = ["<section><h2>Design of experiments</h2>"]
    factor_names = design.factor_names
    parts.append('<p class="note">'
                 f'{len(design.design)} design points x {design.replications} replications'
                 f'{", common random numbers" if design.crn else ", independent seed blocks"}.'
                 "</p>")
    parts.append("<h3>Design points</h3>" + _frame_table(design.table))

    for response in design.responses[:_MAX_REPORT_RESPONSES]:
        effects = design.main_effects(response)
        parts.append(f"<h3>Main effects: {esc(_kpi_label(response))}</h3>"
                     + _frame_table(effects))
        charts = [_main_effects_chart(effects, name, response) for name in factor_names]
        parts.append('<div class="chart-grid">'
                     + "".join(f'<div class="chart">{c}</div>' for c in charts) + "</div>")
    if len(design.responses) > _MAX_REPORT_RESPONSES:
        rest = design.responses[_MAX_REPORT_RESPONSES:]
        parts.append(f'<p class="note">Also measured: {esc(", ".join(rest))}</p>')
    parts.append("</section>")
    return "".join(parts)


def _surrogate_html(surrogate, design) -> str:
    """ROADMAP 3.7 B item 8: coefficients (linear/quadratic only), a
    predicted-vs-simulated scatter, and a 1-D effect curve per factor with
    every other factor held at its centre."""
    lack_of_fit = (" -- LACK OF FIT: the surface misses structure the replication noise "
                   "does not explain" if surrogate.lack_of_fit else "")
    parts = ["<section><h2>Response surface</h2>",
            f'<p class="note">Surrogate: {esc(surrogate.kind)} for '
            f'{esc(_kpi_label(surrogate.response))}. R2={fmt(surrogate.r2)}, '
            f'adjusted R2={fmt(surrogate.r2_adj)}, k-fold CV RMSE={fmt(surrogate.cv_rmse)}'
            f'{lack_of_fit}.</p>']
    if len(surrogate.coefficients):
        parts.append("<h3>Coefficients</h3>" + _frame_table(surrogate.coefficients))

    rows = design.table
    rows = rows[rows["Response"] == surrogate.response]
    points = rows.to_dict(orient="records")
    simulated = [r["Mean"] for r in points]
    predicted, _std = surrogate.predict(points) if points else ([], None)
    scatter = svgcharts.scatter_chart(f"Predicted vs simulated: {_kpi_label(surrogate.response)}",
                                      list(simulated), list(predicted), "Simulated", "Predicted",
                                      diagonal=True)
    parts.append(f'<h3>Predicted vs simulated</h3><div class="chart-grid">'
                f'<div class="chart">{scatter}</div></div>')

    curves = []
    for factor in surrogate.factors:
        xs, ys = surrogate.effect_curve(factor.name)
        x_numeric = xs if factor.kind != "categorical" else list(range(len(xs)))
        curves.append(svgcharts.line_chart(f"Effect of {factor.name}", x_numeric,
                                           {surrogate.kind: ys}, factor.name,
                                           _kpi_label(surrogate.response)))
    parts.append("<h3>Effect curves (other factors at their centre)</h3>"
                 '<div class="chart-grid">'
                 + "".join(f'<div class="chart">{c}</div>' for c in curves) + "</div>")
    parts.append("</section>")
    return "".join(parts)


def _optimization_html(optimization) -> str:
    """ROADMAP 2.1 F: the winning point with its CI, the best-so-far
    trajectory against the replications spent, and the selection table."""
    parts = ["<section><h2>Optimization</h2>"]
    goal = "minimise" if optimization.goal == "min" else "maximise"
    parts.append(f'<p class="note">Goal: {goal} '
                 f'{esc(_kpi_label(optimization.response))}. '
                 f'{optimization.budget_used} replications spent; '
                 f'{"converged" if optimization.converged else "stopped on the budget or step limit"}'
                 f'{"" if optimization.feasible else "; NO FEASIBLE POINT FOUND"}.</p>')

    best_rows = [[esc(name), fmt(value)] for name, value in optimization.best.items()]
    best_rows.append([esc(_kpi_label(optimization.response)), fmt(optimization.best_mean)])
    parts.append("<h3>Best point</h3>" + table(["Factor", "Value"], best_rows))

    history = optimization.history
    if not history.empty:
        trajectory = optimization.trajectory()
        parts.append('<div class="chart-grid"><div class="chart">'
                     + svgcharts.line_chart(
                         "Best so far", [float(v) for v in trajectory["Budget used"]],
                         {"Best": [float(v) for v in trajectory["Best so far"]]},
                         "Replications spent", _kpi_label(optimization.response))
                     + "</div></div>")
        parts.append("<h3>Evaluated points</h3>" + _frame_table(history, limit=_MAX_HISTORY_ROWS))
        if len(history) > _MAX_HISTORY_ROWS:
            parts.append(f'<p class="note">{len(history) - _MAX_HISTORY_ROWS} further '
                         "evaluations not listed.</p>")

    if optimization.selection is not None:
        selection = optimization.selection
        capped = ("" if selection.guaranteed else "; stopped on the replication cap, so the "
                  "probability statement does not apply")
        parts.append("<h3>Ranking and selection</h3>"
                     f'<p class="note">KN (Kim and Nelson 2001) at {selection.confidence:.0%} '
                     f'confidence with an indifference zone of {fmt(selection.indifference)}'
                     f'{capped}.</p>'
                     + _frame_table(selection.table))
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- digital twin (3.4-3.6)
_DATA_INPUT_COLUMNS = ["Binding", "Source", "Kind", "Location", "Column", "Transform", "Target",
                       "Value", "Rows", "Source time", "Problem"]


def _data_inputs_html(results: dict) -> str:
    """ROADMAP 3.4a: what each data binding read, from where, and when. Shown
    only when the run actually resolved bindings -- a study model has none and
    gets no empty section."""
    frame = results.get("data_inputs")
    if frame is None or getattr(frame, "empty", True):
        return ""
    rows = [[esc(row[c]) if c != "Rows" else fmt(row[c]) for c in _DATA_INPUT_COLUMNS]
            for _, row in frame.iterrows()]
    failed = int((frame["Problem"].astype(str) != "").sum()) if "Problem" in frame else 0
    note = ('<p class="note">Every parameter below was read from data at run time; a binding with '
            'a Problem was not applied and the model\'s own stored value was used instead.</p>'
            if failed else
            '<p class="note">Every parameter below was read from data at run time.</p>')
    return ("<section><h2>Data inputs</h2>" + note +
            table(_DATA_INPUT_COLUMNS, rows) + "</section>")


def _calibration_html(calibration) -> str:
    """ROADMAP 3.5: the tuned parameters, the per-target verdict, and an
    observed-against-simulated bar chart."""
    parts = ["<section><h2>Calibration</h2>"]
    verdict = "every target within tolerance" if calibration.passed else "NOT every target met"
    parts.append(f'<p class="note">Objective {fmt(calibration.objective)} '
                 f'(weighted sum of squared relative errors and series payoffs, per replication); '
                 f'{calibration.budget_used} replications spent; {verdict}.</p>')

    best_rows = [[esc(name), fmt(value)] for name, value in calibration.best.items()]
    parts.append("<h3>Fitted parameters</h3>" + table(["Factor", "Value"], best_rows))

    fit_table = calibration.table
    rows = []
    for _, row in fit_table.iterrows():
        passed = row["Pass"]
        rows.append([_kpi_cell(row["KPI"]), fmt(row["Observed"]), fmt(row["Simulated"]),
                     fmt(row["LB"]), fmt(row["UB"]), fmt(row["Error"]),
                     fmt(row["Tolerance"]),
                     "-" if passed is None else ("pass" if passed else "FAIL")])
    parts.append("<h3>Targets</h3>" +
                 table(["KPI", "Observed", "Simulated", "LB", "UB", "Error", "Tolerance",
                         "Verdict"], rows))

    if not fit_table.empty:
        # One chart with the pair interleaved, not two charts side by side: a
        # shared y axis is what makes "observed against simulated" readable,
        # and two independently scaled charts would not be a comparison at all.
        categories, values, err = [], [], []
        for _, row in fit_table.iterrows():
            label = _kpi_label(str(row["KPI"]))
            categories += [f"{label} obs", f"{label} sim"]
            values += [float(row["Observed"]), float(row["Simulated"])]
            err += [(float(row["Observed"]), float(row["Observed"])),
                    (float(row["LB"]), float(row["UB"]))]
        chart = svgcharts.bar_chart("Observed against simulated", categories, values, "Value",
                                    err=err)
        parts.append(f'<div class="chart-grid"><div class="chart">{chart}</div></div>')
    # PHASE6 S6: each series target as model against data, residual beneath.
    charts = [svgcharts.line_chart(
        f"{_kpi_label(series)}: model against data", res["Time"].tolist(),
        {"Data": res["Data"].tolist(), "Model": res["Model"].tolist(),
         "Residual": res["Residual"].tolist()}, "Time", "Value")
        for series, res in getattr(calibration, "residuals", {}).items()]
    if charts:
        parts.append('<div class="chart-grid">' +
                     "".join(f'<div class="chart">{c}</div>' for c in charts) + "</div>")
    parts.append("</section>")
    return "".join(parts)


_MAX_FAN_CHARTS = 6
# Which series earn a fan chart when a model has many (DEVLOG #100): the same
# curated prefix `forecast` samples by default. It used to be read from
# `forecast._DEFAULT_SERIES_PREFIXES` by name only -- the import was never
# there -- so every report carrying a forecast raised NameError.
_FAN_SERIES_PREFIXES = ("system/",)


def _fan_chart(result, series: str) -> str:
    """One series' prediction band: the mean line inside the 5-95% band."""
    band = result.band(series)
    return svgcharts.line_chart(
        series, band["Time"].tolist(), {"Mean": band["Mean"].tolist()}, "Time", series,
        band=(band["Q05"].tolist(), band["Q95"].tolist()))


def _forecast_html(result) -> str:
    """ROADMAP 3.6: fan charts, the end-of-horizon KPI distribution and the
    event table."""
    low, high = result.quantiles
    parts = ["<section><h2>Forecast</h2>",
             f'<p class="note">{result.replications} replications over a terminating horizon of '
             f'{fmt(result.horizon)} base time units, no warm-up. The shaded band is the '
             f'EMPIRICAL {low:.0%}-{high:.0%} range across replications -- a prediction band for '
             f'a single future run, NOT a confidence interval of the mean (that interval is in '
             f'the KPI table\'s LB/UB columns and narrows with more replications; this band does '
             f'not).</p>']

    names = [s for s in result.series_names if s.startswith(_FAN_SERIES_PREFIXES)] or \
        result.series_names
    charts = [_fan_chart(result, s) for s in names[:_MAX_FAN_CHARTS]]
    if charts:
        parts.append('<div class="chart-grid">'
                     + "".join(f'<div class="chart">{c}</div>' for c in charts) + "</div>")

    kpis = result.kpis
    curated = kpis[kpis["KPI"].apply(_curated)] if not kpis.empty else kpis
    rows = [[_kpi_cell(row["KPI"]), fmt(row["Mean"]), fmt(row["LB"]), fmt(row["UB"]),
             fmt(row["Q05"]), fmt(row["Q95"]), fmt(row["Min"]), fmt(row["Max"])]
            for _, row in curated.iterrows()]
    parts.append("<h3>End of horizon</h3>" +
                 table(["KPI", "Mean", "CI LB", "CI UB", f"Q{low:.0%}", f"Q{high:.0%}",
                         "Min", "Max"], rows))

    event_rows = [[esc(row["Event"]), _kpi_cell(row["KPI"]), esc(row["Condition"]),
                   f'{row["Probability"]:.1%}' if math.isfinite(row["Probability"]) else "-",
                   f'{row["Wilson LB"]:.1%}', f'{row["Wilson UB"]:.1%}',
                   fmt(row["Replications"])]
                  for _, row in result.events.iterrows()]
    parts.append('<h3>Events</h3><p class="note">Share of replications in which the condition '
                 'held, with a Wilson score interval.</p>' +
                 table(["Event", "KPI", "Condition", "Probability", "LB", "UB", "Replications"],
                        event_rows, empty="No events declared"))

    orders = getattr(result, "orders", None)
    if orders is not None and not orders.empty:
        order_rows = [[esc(row["Order"]), fmt(row["Due"]), fmt(row["Median Completion"]),
                       fmt(row["Q05"]), fmt(row["Q95"]),
                       f'{row["P(late)"]:.1%}' if math.isfinite(row["P(late)"]) else "-",
                       f'{row["Wilson LB"]:.1%}', f'{row["Wilson UB"]:.1%}',
                       fmt(row["Completed Reps"])]
                      for _, row in orders.iterrows()]
        parts.append('<h3>Orders</h3><p class="note">Per order: the empirical spread of its '
                     'completion time over the replications where it finished, and the '
                     'probability it misses its due date, with a Wilson score interval. An '
                     'order still unfinished at the end of the horizon counts as late.</p>' +
                     table(["Order", "Due", "Median completion", f"Q{low:.0%}", f"Q{high:.0%}",
                             "P(late)", "LB", "UB", "Completed reps"], order_rows))
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- orders (PHASE4 A3)
_ORDER_REPORT_COLS = ["Order", "Entity Type", "Release", "Due", "Quantity", "Completion Time",
                      "Lateness", "Tardiness", "Status"]
_DUE_DATE_REPORT_COLS = ["Entity Type", "Completed", "On Time", "Service Level", "Avg Lateness",
                         "Avg Tardiness", "Max Tardiness", "Late"]
_MAX_REPORT_ORDERS = 100


def _orders_html(results: dict) -> str:
    """The order book and the due-date KPIs, shown only when the run has them.

    One row per (order, replication) -- a single run is the normal case for an
    order book, and averaging a completion time over replications would hide
    exactly the spread a due date is about; the forecast section is where that
    spread is summarised properly.
    """
    orders = results.get("order_summary")
    due_dates = results.get("due_date_summary")
    if (orders is None or orders.empty) and (due_dates is None or due_dates.empty):
        return ""
    parts = ["<section><h2>Orders and due dates</h2>"]
    if due_dates is not None and not due_dates.empty:
        parts.append('<p class="note">Service level is the fraction of due-dated entities '
                     'completed on time; lateness is signed (completion - due), tardiness its '
                     'positive part. Entities finished during warm-up are excluded.</p>')
        parts.append(_frame_table(due_dates, _DUE_DATE_REPORT_COLS))
    if orders is not None and not orders.empty:
        note = (f'<p class="note">First {_MAX_REPORT_ORDERS} of {len(orders)} order rows.</p>'
                if len(orders) > _MAX_REPORT_ORDERS else "")
        parts.append(f"<h3>Orders</h3>{note}"
                     + _frame_table(orders, _ORDER_REPORT_COLS, limit=_MAX_REPORT_ORDERS))
    parts.append("</section>")
    return "".join(parts)


# ---------------------------------------------------------------- header / footer
def _header_html(layout: dict, results: dict, title: str) -> str:
    sim = layout.get("simulation", {})
    sys_df = results.get("system")
    replications = int(sys_df["Run"].nunique()) if sys_df is not None and not sys_df.empty else 0
    seed = int(sys_df["Run"].min()) if sys_df is not None and not sys_df.empty else "-"
    calendar = calendar_of(layout)
    run_length = run_horizon(layout, calendar)
    run_length_text = fmt(run_length) if run_length else "until the model drains"

    rows = [
        ("Model", esc(title)),
        ("Naga version", esc(__version__)),
        ("Date", esc(datetime.date.today().isoformat())),
        ("Base time unit", esc(sim.get("time_unit", "minutes"))),
        ("Replications", str(replications)),
        ("Run length", run_length_text),
        ("Warm-up", fmt(sim.get("warmup", 0))),
        ("Base seed", str(seed)),
    ]
    if calendar is not None:
        # PHASE4 T10: the study's real window, which is what a client reads.
        window = calendar.label(0, "%a %d %b %Y %H:%M")
        if run_length:
            window += " to " + calendar.label(run_length, "%a %d %b %Y %H:%M")
        rows.insert(4, ("Calendar", f"{esc(window)} ({esc(getattr(calendar.tz, 'key', 'UTC'))})"))
    items = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    description = layout.get("info", {}).get("description", "")
    desc_html = f'<p class="description">{esc(description)}</p>' if description else ""
    return f'<header><h1>{esc(title)}</h1>{desc_html}<dl class="meta">{items}</dl></header>'


_METHODS_NOTE = (
    "Confidence intervals are Student-t, across replications, at the 95% level. "
    "Scenario differences use the paired-t interval on common-random-number replications "
    "when CRN was used, otherwise Welch's interval on independent samples. "
    "Warm-up excludes each collector's statistics up to the warm-up time (the model state "
    "itself is never reset). Resource utilization is busy time over the SCHEDULED capacity "
    "in force (a resource off shift or down does not dilute it)."
)

_CSS = """
:root { --ink: #1a202c; --muted: #4a5568; --line: #e2e8f0; --accent: #2b6cb0; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: var(--ink); margin: 0; padding: 24px; background: #fff; line-height: 1.45; }
header h1 { margin-bottom: 8px; }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0 0 24px; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; }
h2 { border-bottom: 2px solid var(--line); padding-bottom: 6px; margin-top: 40px; }
h3 { color: var(--muted); margin-top: 24px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 20px; font-size: 13px; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
thead th { background: #f7fafc; }
.warnings { color: #9c4221; }
.note { color: var(--muted); font-size: 13px; }
.chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 460px)); gap: 16px; }
.chart { max-width: 460px; }
.chart svg { display: block; width: 100%; height: auto; max-width: 100%; }
footer { margin-top: 48px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--muted);
         font-size: 12px; }
@media print {
  h2 { page-break-before: always; }
  body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
"""


def build_report(layout: dict, results: dict, *, experiment=None, design=None, optimization=None,
                 calibration=None, forecast=None, surrogate=None, extra_sections=(), plan_risk=None,
                 title: str | None = None, path=None) -> str:
    """Return one self-contained HTML report; write it to *path* when given.

    *experiment* is an `experiments.ExperimentResult`, *design* an
    `optimize.DesignResult` and *optimization* an `optimize.OptimizationResult`;
    each adds its own section (ROADMAP 1.7 D, 2.1 F). *calibration* is a
    `calibrate.CalibrationResult` and *forecast* a `forecast.ForecastResult`
    (ROADMAP 3.5, 3.6). *surrogate* is a `surrogate.Surrogate` (needs *design*
    alongside it, for the simulated means the "Response surface" section
    compares its predictions against, ROADMAP 3.7 B item 8). A run whose
    results carry a "data_inputs" frame
    (`api.run_model(bind_data=True)`, ROADMAP 3.4a) also gets a "Data inputs"
    section in the header: what was read, from where, and when -- the
    provenance a twin's answer has to carry. *extra_sections* are finished HTML
    sections a caller adds after the others (the product's MES twin cycle, built
    with `esc`, `fmt` and `table`). *plan_risk* is an "order_risk" DataFrame
    (`planning.plan_risk`, PHASE6 W4 P3/P5, ARCHITECTURE D121/D123): the plan
    CLI's `--risk N` result, shown beside the Schedule section that
    `results["schedule_log"]` already draws (D120).
    """
    title = title or "Naga simulation report"
    sim = layout.get("simulation", {})
    warmup = float(sim.get("warmup") or 0.0)

    sys_rows = [[esc(m), fmt(mean), fmt(half), fmt(mn), fmt(mx)]
               for m, mean, half, mn, mx in _system_rows(results)]

    resource_rows = [[esc(name), *[fmt(v) for v in vals]]
                     for name, *vals in _pivot_means(results.get("resource_summary"),
                                                      "Resource", _RESOURCE_KPIS)]
    store_rows = [[esc(name), *[fmt(v) for v in vals]]
                 for name, *vals in _pivot_means(results.get("store_summary"), "Store", _STORE_KPIS)]
    entity_rows = [[esc(name), *[fmt(v) for v in vals]]
                  for name, *vals in _pivot_means(results.get("entity_summary"),
                                                   "Entity Type", _ENTITY_KPIS)]
    kinds = _user_kinds(results)
    user_rows = [[esc(name), esc(kinds.get(name, "-")), *[fmt(v) for v in vals]]
                for name, *vals in _pivot_means(results.get("user_summary"), "Statistic", _USER_KPIS)]

    timeseries_mean = outputs.timeseries_mean(results)   # once per report
    calendar = calendar_of(layout)               # None unless the model is dated
    queue_charts, overflow = _queue_charts(timeseries_mean, calendar)
    overflow_note = (f'<p class="note">Also queued: {esc(", ".join(overflow))}</p>'
                     if overflow else "")

    body = [
        _header_html(layout, results, title),
        _data_inputs_html(results),
        f'<section><h2>Model summary</h2>{_model_summary_html(layout)}</section>',
        _validation_html(layout),
        "<section><h2>System KPIs</h2>" +
        table(["Metric", "Mean", "95% CI half-width", "Min", "Max"], sys_rows) + "</section>",
        "<section><h2>Resources</h2>" +
        table(["Resource"] + _RESOURCE_KPIS, resource_rows) + "</section>",
        "<section><h2>Stores</h2>" + table(["Store"] + _STORE_KPIS, store_rows) + "</section>",
        "<section><h2>Entities</h2>" +
        table(["Entity type"] + _ENTITY_KPIS, entity_rows) + "</section>",
        "<section><h2>User statistics</h2>" +
        table(["Statistic", "Kind"] + _USER_KPIS, user_rows) + "</section>",
        "<section><h2>Charts</h2>"
        f'<div class="chart-grid"><div class="chart">{_utilization_chart(results)}</div>'
        f'<div class="chart">{_wip_chart(timeseries_mean, warmup, calendar)}</div></div>'
        '<div class="chart-grid">'
        + "".join(f"<div class='chart'>{c}</div>" for c in queue_charts) + "</div>"
        f"{overflow_note}"
        f'<div class="chart-grid"><div class="chart">{_throughput_histogram(results)}</div>'
        f'<div class="chart">{_material_flow_chart(results)}</div></div>'
        "</section>",
    ]
    body.append(_orders_html(results))          # "" unless the model has orders/due dates
    body.append(_bottleneck_html(results))       # "" unless outputs.bottleneck was set
    body.append(_schedule_html(results))         # "" unless outputs.schedule was set
    if plan_risk is not None and not plan_risk.empty:
        body.append(_plan_risk_html(plan_risk))
    if has_costs(results):
        body.append(_costs_html(results, sim.get("currency", "EUR")))
    if experiment is not None:
        body.append(_experiment_html(layout, experiment))
    if design is not None:
        body.append(_design_html(design))
    if surrogate is not None and design is not None:
        body.append(_surrogate_html(surrogate, design))
    if optimization is not None:
        body.append(_optimization_html(optimization))
    if calibration is not None:
        body.append(_calibration_html(calibration))
    if forecast is not None:
        body.append(_forecast_html(forecast))
    body += extra_sections
    body.append(f"<footer><h2>Methods</h2><p>{esc(_METHODS_NOTE)}</p></footer>")

    head = (f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, '
           f'initial-scale=1"><title>{esc(title)}</title><style>{_CSS}</style>')
    doc = f"<!doctype html><html lang=\"en\"><head>{head}</head><body>{''.join(body)}</body></html>"

    if path is not None:
        Path(path).write_text(doc, encoding="utf-8")
    return doc
