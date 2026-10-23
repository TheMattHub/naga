from __future__ import annotations

"""
Excel workbook and CSV folder writer for experiment results.

Split out of core/outputs.py (C3.3): that module stays the KPI-calculation
side, this one holds only the write path (openpyxl formatting, the sheet
layout, the CSV folder dump).
"""
import logging
from pathlib import Path

import pandas as pd

from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from nagasim.core.calendar import CalendarError, model_calendar
from nagasim.core.outputs import ci95_by_object_long, timeseries_mean
from nagasim.core.stats import ci95
from nagasim.core.timeunits import factor

log = logging.getLogger(__name__)

__all__ = ["write_sheet", "concat_runs", "calendar_of", "with_datetimes",
           "export_workbook", "export_csv_folder"]


def _run_first(df: pd.DataFrame) -> pd.DataFrame:
    if "Run" in df.columns:
        cols = ["Run"] + [c for c in df.columns if c != "Run"]
        return df[cols]
    return df

def _is_percent_column(col) -> bool:
    """A column whose values are already a 0-100 percentage, by its header
    (the same heuristic the old string-formatting code used): '%' in the
    name, or 'util'/'usage'/'perc'/'mode %' in it, case-insensitive."""
    if not isinstance(col, str):
        return False
    col_lower = col.lower()
    return ("%" in col or "util" in col_lower or "usage" in col_lower
            or "perc" in col_lower or col_lower.endswith("mode %"))


# A literal "%" suffix, not Excel's own "0.00%" format: that one multiplies
# the cell's stored value by 100 for display, which would double the KPI
# (already 0-100, not a 0-1 fraction). This shows "75.32%" for a value of
# 75.32 without touching it (T8 item 4: display only, never the value).
_PERCENT_FORMAT = '0.00"%"'
_INT_FORMAT = "0"
_FLOAT_FORMAT = "0.00"          # times and rates: 2 decimals, within the 2-4 asked for
_DATETIME_FORMAT = "yyyy-mm-dd hh:mm"   # PHASE4 T10 wall-clock columns


def _column_number_format(col, series: pd.Series, percent: bool) -> str | None:
    """The openpyxl `number_format` for one column, or None to leave the
    default (text columns, and log sheets that opt out of the percent guess
    with `format_percent=False`)."""
    if percent and _is_percent_column(col):
        return _PERCENT_FORMAT
    dtype = series.dtype
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return _DATETIME_FORMAT          # PHASE4 T10: a real datetime, shown as one
    if pd.api.types.is_bool_dtype(dtype):
        return None
    if pd.api.types.is_integer_dtype(dtype):
        return _INT_FORMAT
    if pd.api.types.is_float_dtype(dtype):
        return _FLOAT_FORMAT
    if dtype == object:
        # A pivoted/concatenated frame ("Run Detail", built from a long
        # Metric/Value table with strings in other rows) can lose its
        # per-column dtype to `object` although every value here is a plain
        # number. These frames are small (one row per replication), so one
        # scan is cheap -- unlike a big log sheet, which never reaches this
        # branch because pandas already gives it a real numeric dtype.
        numbers = [v for v in series if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and not (isinstance(v, float) and pd.isna(v))]
        if numbers and len(numbers) == int(series.notna().sum()):
            return _INT_FORMAT if all(float(n).is_integer() for n in numbers) else _FLOAT_FORMAT
    return None


def write_sheet(writer, df: pd.DataFrame, sheet_name: str, format_percent: bool = True, index: bool = False) -> None:
    """
    Helper to write a DataFrame to an Excel sheet with:
        - rounding of float columns to 2 decimals
        - a per-column Excel number format (counts as integers, times/rates to
          2 decimals, a likely percentage column with a literal "%" suffix
          that does not rescale the value, T8 item 4) -- display only, the
          written VALUES are exactly the frame's
        - freeze top row
        - bold headers
        - auto-fit column widths
    """
    df = df.copy()

    # Round all float columns to 2 decimals (preserves numeric type)
    float_cols = df.select_dtypes(include=['float', 'float64']).columns
    df[float_cols] = df[float_cols].round(2)

    # Write to and access the actual worksheet
    df.to_excel(writer, sheet_name=sheet_name, index=index)
    ws: Worksheet = writer.sheets[sheet_name]

    #Freeze top row
    ws.freeze_panes = "A2"

    # Bold the header row
    for cell in next(ws.iter_rows(min_row=1, max_row=1)):
        cell.font = Font(bold=True)

    # Number formats, decided ONCE per column (not recomputed per cell, so a
    # big log sheet pays one decision per column and a flat assignment per
    # cell -- the per-cell column-name lookup this replaces was the slow part).
    if ws.max_row > 1:
        for col_idx, col in enumerate(df.columns, start=1):
            fmt = _column_number_format(col, df[col], format_percent)
            if fmt is None:
                continue
            for column_cells in ws.iter_cols(min_col=col_idx, max_col=col_idx,
                                             min_row=2, max_row=ws.max_row):
                for cell in column_cells:
                    cell.number_format = fmt

    # Auto-adjust column widths, capped so one long cell cannot blow out the sheet
    for col_idx, column_cells in enumerate(ws.columns, start=1):
        max_len = max((len(str(cell.value)) if cell.value is not None else 0) for cell in column_cells)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 60)

# ---------- replication aggregation & workbook export ---------------
def concat_runs(all_kpis: list[dict]) -> dict[str, pd.DataFrame]:
    """Concatenate the per-replication `calc_kpis` dicts into one frame per key.
    Log sheets get the Run column first. Shared by the controller and api.run_model."""
    logs = {"entity_log", "resource_log", "store_log"}
    return {key: (_run_first(df) if key in logs else df)
            for key in all_kpis[0]
            for df in [_concat([k[key] for k in all_kpis])]}


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Row-wise concat that leaves EMPTY frames out (a replication with nothing
    to report for this key). pandas deprecates letting them take part in the
    result dtypes, and the FutureWarning it raised on every run was displayed
    from whatever thread ran the simulation -- the GUI's worker QThread
    included, where the warnings machinery is not safe (DEVLOG #94)."""
    full = [f for f in frames if not f.empty]
    if not full:
        return frames[0].copy()
    return pd.concat(full, ignore_index=True)


# Real dates on the way OUT only (PHASE4 T10, ARCHITECTURE D81). The frames
# `calc_kpis`/`concat_runs` build stay numeric, so a golden hash and every
# statistic are untouched; a datetime column is added here, at export and
# report time, and only when the model declares a calendar.
_DATE_COLUMNS = {"timeseries": {"Time": "Datetime"},
                 "resource_log": {"time": "datetime"},
                 "store_log": {"time": "datetime"},
                 "entity_log": {"Created At": "Created At (date)",
                                "Exited At": "Exited At (date)"},
                 # PHASE6 W4 P2 (D120): `planning.plan_export` looks for these
                 # two by name and turns them into "Planned Start/End Date".
                 "schedule_log": {"Start": "Start (date)", "End": "End (date)"}}


def calendar_of(layout: dict | None):
    """The model's calendar, or None -- a malformed one is already a validation
    problem, so an export never fails over it."""
    try:
        return model_calendar(layout or {})
    except CalendarError:
        return None


def with_datetimes(results: dict[str, pd.DataFrame], layout: dict | None = None,
                   calendar=None) -> dict[str, pd.DataFrame]:
    """*results* with a wall-clock datetime column beside each time column.

    Returns the input unchanged when the model has no calendar. The added
    values are LOCAL and timezone-naive: that is what a spreadsheet cell and a
    CSV reader can hold, and the model's timezone is already in the report.
    """
    if calendar is None and layout is not None:
        calendar = calendar_of(layout)
    if calendar is None:
        return results
    seconds = factor(calendar.unit, "seconds")
    epoch = pd.Timestamp(calendar.epoch)
    out = dict(results)
    for key, columns in _DATE_COLUMNS.items():
        frame = results.get(key)
        if frame is None or frame.empty:
            continue
        frame = frame.copy()
        for source, target in columns.items():
            if source not in frame.columns:
                continue
            stamps = epoch + pd.to_timedelta(
                pd.to_numeric(frame[source], errors="coerce") * seconds, unit="s")
            frame.insert(frame.columns.get_loc(source) + 1, target,
                         stamps.dt.tz_convert(calendar.tz).dt.tz_localize(None))
        out[key] = frame
    return out


def export_workbook(results: dict[str, pd.DataFrame], out_path, layout: dict | None = None) -> Path:
    """Write the 10-sheet experiment workbook from `concat_runs` output (plus
    an Orders and a Due Dates sheet when the model has them, PHASE4 A3).

    *layout*, when given and carrying a calendar, adds the wall-clock datetime
    columns (PHASE4 T10)."""
    calendar = calendar_of(layout)
    results = with_datetimes(results, calendar=calendar)
    sys_df = results["system"]
    ent_sum, res_sum, sto_sum, usr_sum = (results["entity_summary"], results["resource_summary"],
                                          results["store_summary"], results["user_summary"])

    # DEVLOG #8: a model with no SEIZE has no resource summary at all
    if "Bottleneck %" in res_sum.columns:
        bn_names = (                                # string of names per run
            res_sum[res_sum["Bottleneck %"] == 1]
                   .groupby("Run", sort=False)["Resource"]
                   .apply(lambda s: ", ".join(sorted(s)))
                   .rename("Bottleneck Resources")
        )
    else:
        bn_names = pd.Series(dtype=object, name="Bottleneck Resources").rename_axis("Run")

    res_ci = ci95_by_object_long(res_sum, obj_col="Resource")
    sto_ci = ci95_by_object_long(sto_sum, obj_col="Store")
    ent_ci = ci95_by_object_long(ent_sum, obj_col="Entity Type")
    usr_ci = ci95_by_object_long(usr_sum, obj_col="Statistic")

    run_wide = (
        sys_df[sys_df["Metric"] != "Run"]
        .pivot(index="Run", columns="Metric", values="Value")
        .rename_axis(None, axis=1)
        .reset_index()
    )
    run_wide = run_wide.merge(bn_names, on="Run", how="left")

    desired = ["Run", "Simulation Time", "Throughput (per time unit)",
           "Avg WIP (Little)", "Avg Throughput Time",
           "Avg Resource Usage %", "Avg Queue Length",
           "Max Queue Length", "Total Entities Created",
           "Total Entities Completed", "Bottleneck Resources"
    ]
    run_wide = run_wide.reindex(columns=desired)

    # Student-t CI from the shared helper: ddof=1, t-quantile, LB <= UB (DEVLOG #19)
    num_cols = run_wide.select_dtypes("number").columns.drop("Run", errors="ignore")
    ci_rows = []
    for metric in num_cols:
        st = ci95(run_wide[metric])
        ci_rows.append({"Metric": metric, "mean": st["mean"], "std": st["sd"],
                        "std_error": st["sem"], "95ci_LB": st["lb"], "95ci_UB": st["ub"]})
    ci_transposed = pd.DataFrame(ci_rows,
                                 columns=["Metric", "mean", "std", "std_error", "95ci_LB", "95ci_UB"])

    top_bn_row = res_ci[res_ci["KPI"] == "Bottleneck %"].sort_values("Mean", ascending=False).head(1)
    if not top_bn_row.empty:
        top_bn_name = top_bn_row.iloc[0]["Resource"]
        top_bn_val = top_bn_row.iloc[0]["Mean"]
        usage_snapshot = res_sum[res_sum["Resource"] == top_bn_name]["Usage % (snapshot)"].mean()
        top_bn_df = pd.DataFrame([
            {"Metric": "Top Bottleneck", "mean": top_bn_name},
            {"Metric": "Bottleneck Occurrence Rate", "mean": f"{top_bn_val:.2f}%"},
            {"Metric": "Usage % of Bottleneck (snapshot)", "mean": f"{usage_snapshot:.2f}"}
        ])
    else:
        top_bn_df = pd.DataFrame([
            {"Metric": "Top Bottleneck", "mean": "-"},
            {"Metric": "Bottleneck Occurrence Rate", "mean": "-"},
            {"Metric": "Usage % of Bottleneck (snapshot)", "mean": "-"}
        ])
    ci_transposed = pd.concat([ci_transposed, top_bn_df], ignore_index=True)
    ci_transposed.index = range(1, len(ci_transposed) + 1)  # Start Run index at 1
    ci_transposed.index.name = "Run"  # Name the index column 'Run'

    out_path = Path(out_path)
    with pd.ExcelWriter(out_path, engine="openpyxl") as wr:
        write_sheet(wr, ci_transposed, "System Summary")
        write_sheet(wr, run_wide, "Run Detail")
        write_sheet(wr, ent_ci, "Entity Summary")
        write_sheet(wr, res_ci, "Resource Summary")
        write_sheet(wr, sto_ci, "Store Summary")
        write_sheet(wr, usr_ci, "User Statistics")
        # Across-replication mean only (ROADMAP 1.7): the full per-sample-time
        # frame can be large, so it stays out of the workbook; CSV export
        # writes the "timeseries" key in full instead.
        write_sheet(wr, with_datetimes({"timeseries": timeseries_mean(results)},
                                       calendar=calendar)["timeseries"], "Time Series")
        # Orders and due dates (PHASE4 A3): two sheets only when the model has
        # them, so a workbook exported from a model without orders is exactly
        # the 10-sheet one it has always been.
        for key, sheet in (("order_summary", "Orders"), ("due_date_summary", "Due Dates"),
                           ("queue_summary", "Queue Behaviour"),
                           ("motion_summary", "Motion"),
                           ("charging_summary", "Charging"),
                           ("battery_summary", "Batteries"),
                           ("tending_summary", "Tending"),
                           ("level_summary", "Levels"), ("flow_summary", "Flows"),
                           ("conversion_summary", "Conversions"),
                           ("component_summary", "Components"),
                           ("cost_summary", "Costs"),
                           ("fleet_summary", "Fleets"), ("segment_summary", "Segments"),
                           ("conveyor_summary", "Conveyors"),
                           ("conveyor_stops", "Conveyor stops"),
                           ("buffer_summary", "Buffers"),
                           ("energy_summary", "Energy"),
                           ("material_flow_summary", "Material Flow"),
                           ("bottleneck_summary", "Bottleneck"),
                           ("schedule_log", "Schedule"),
                           ("agent_summary", "Agents"),
                           ("agent_attributes", "Agent attributes"),
                           ("dashboard", "Dashboard"),
                           ("game_log", "Game log")):
            frame = results.get(key)
            if frame is not None and not frame.empty:
                write_sheet(wr, frame, sheet)
        write_sheet(wr, results["entity_log"], "Entities", format_percent=False)
        write_sheet(wr, results["resource_log"], "Resources", format_percent=False)
        write_sheet(wr, results["store_log"], "Stores", format_percent=False)

    log.info(f"Experiment exported to {out_path}")
    return out_path


def export_csv_folder(results: dict[str, pd.DataFrame], directory) -> Path:
    """Write one CSV per result frame into *directory* (ROADMAP 2.4 C): the
    shared implementation behind both `ui/results.py`'s CSV export action and
    the `csv` entry in `nagasim.exporters.EXPORTER_REGISTRY`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for key, df in results.items():
        df.to_csv(directory / f"{key}.csv", index=False)
    return directory
