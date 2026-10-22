"""check_draft: does a generated model reproduce the data it was generated from? (D687)

Runs the draft briefly, one traced replication per seed, and compares its cycle time,
throughput and per-activity service times with the source's own numbers (an event log read by
`discover.log_kpis`; tables read by `tablegen`: the process time every routing step of a station
agrees on). Adds the operational-law checks. Never says "valid": a short source says it cannot
tell the draft from a wrong one.
"""
from __future__ import annotations

import time

from nagasim import api, discover, laws, tablegen
from nagasim.core.distributions import distribution_mean
from nagasim.core.document import ModelDocument
from nagasim.core.stats import ci95
from nagasim.core.trace import TraceRecorder

from .core import ToolError, _document_args, _finite, _runnable_layout, tool, workspace

REL = 0.10          # a fitted draft is not exact: the draft's mean may sit this far from the source
MIN_CASES = 30      # fewer source cases cannot tell a right draft from a wrong one
MIN_SEEDS, MAX_SEEDS = 3, 10

_document_args("check_draft", model="Workspace name of the generated model.",
               source="The log file, tables folder or .xlsx workbook the model was generated from "
                      "(inside the workspace).",
               max_seconds="Wall-clock budget for the draft's runs (at least 3 are made).")


def _log_source(path, unit: str) -> tuple[dict, int]:
    """{kpi: source value} of an event log, and its number of cases."""
    kpis = discover.log_kpis(discover.read_log(path, time_unit=unit))
    want = {"cycle_time": kpis["cycle_time"], "throughput": kpis["throughput"]}
    for name, a in kpis["activities"].items():
        if a["service"] is not None:
            want[f"service:{discover._name(name, set())}"] = a["service"]
    return want, kpis["cases"]


def _table_source(path) -> tuple[dict, int]:
    tables = tablegen._read_tables(path)
    if "Stations" not in tables or "Routings" not in tables:
        raise ToolError("E_BAD_SOURCE", "tables need Stations and Routings", "pass the folder or workbook the model was generated from")
    stations = tablegen._add_stations(ModelDocument(), tables["Stations"])
    rows = tables["Routings"][1]
    want = {}
    for name, info in stations.items():
        times = {tablegen._num(r.get("process_time")) for r in rows if tablegen._str(r.get("station")) == name}
        if len(times) == 1 and None not in times:
            mean = times.pop()
        elif times == {None}:
            mean = distribution_mean(info["distribution"], info["dist_params"])
        else:
            continue            # products disagree: the station's mean depends on the mix
        if mean is not None:
            want[f"service:process_{name.lower()}"] = float(mean)
    return want, len(rows)


def _draft_values(layout: dict, deadline: float) -> tuple[dict, dict, int]:
    """{kpi: [value per seed]}, the first seed's results, and the seeds run."""
    seen: dict[str, list] = {}
    first = None
    seeds = 0
    stop = lambda: time.monotonic() > deadline      # noqa: E731  (#786: the budget reaches each run)
    while seeds < MAX_SEEDS and (seeds < MIN_SEEDS or time.monotonic() < deadline):
        trace = TraceRecorder()
        results = api.run_model(layout, replications=1, seed=seeds, trace=trace, should_stop=stop)
        if results is None:                            # cut by the budget: keep the seeds done
            break
        first = first or results
        seeds += 1
        got = discover.log_kpis(discover.log_from_trace(trace))
        values = {"cycle_time": got["cycle_time"], "throughput": got["throughput"]}
        values.update({f"service:{n}": a["service"] for n, a in got["activities"].items()})
        for k, v in values.items():
            if v is not None:
                seen.setdefault(k, []).append(float(v))
    return seen, first, seeds


@tool(read_only=True)
def check_draft(model: str, source: str | None = None, max_seconds: float = 20) -> dict:
    """Check a generated model against the data it came from, without claiming it is valid.

    Runs the model briefly (one traced replication per seed, at least 3) and compares cycle time,
    throughput and each activity's mean service time with the source's own numbers (event log:
    all three; tables: the process time of the stations whose routing rows agree), then adds the
    operational-law checks of `run_model`. A source with fewer than 30 cases cannot tell a right
    draft from a wrong one, so the verdict is at best "partial" and `note` says so.

    Returns {"verdict": "passed"|"partial"|"failed", "compared": [{kpi, source, draft, within}],
    "laws": {passed, partial}, "sample": {"source_cases", "draft_replications"}, "note"}.
    "failed" means a compared number is off by more than its tolerance (twice the draft's
    confidence half-width, at least 10%); "partial" means a law warns or the source is short.
    """
    ws = workspace()
    layout = _runnable_layout(ws.document(model), model)
    if not source:
        raise ToolError("E_NO_SOURCE", "check_draft needs the file the model was generated from",
                        "pass source: the log, tables folder or .xlsx")
    path = ws.resolve(source, must_exist=True)
    try:
        if path.is_dir() or path.suffix.lower() == ".xlsx":
            want, cases = _table_source(path)
        else:
            want, cases = _log_source(path, (layout.get("simulation") or {}).get("time_unit") or "minutes")
    except (discover.LogError, ValueError) as exc:
        raise ToolError("E_BAD_SOURCE", str(exc), "fix the source and try again") from None
    try:
        seen, first, seeds = _draft_values(layout, time.monotonic() + float(max_seconds))
    except RuntimeError as exc:
        raise ToolError("E_RUN_FAILED", str(exc), "call validate_model and fix the model") from exc
    if first is None:
        raise ToolError("E_RUN_FAILED", f"no replication finished within {max_seconds:g} s",
                        "raise max_seconds or shorten the model's run length")
    compared = []
    for kpi, theirs in want.items():
        values = seen.get(kpi)
        if not values or theirs is None:
            continue
        stat = ci95(values)
        hw = stat["ub"] - stat["mean"] if len(values) > 1 else 0.0
        within = abs(stat["mean"] - theirs) <= max(2 * hw, REL * abs(theirs))
        compared.append({"kpi": kpi, "source": _finite(theirs), "draft": _finite(stat["mean"]), "within": bool(within)})
    summary = laws.summary(laws.checks(layout, first))
    short = cases < MIN_CASES
    if not compared:
        verdict, note = "partial", "no number of the source could be matched to the draft"
    elif short:
        verdict = "partial"
        note = f"only {cases} source cases: the draft is not distinguishable from a wrong one"
    elif not all(c["within"] for c in compared):
        verdict, note = "failed", "a compared number is off by more than its tolerance"
    else:
        verdict, note = ("partial" if summary["partial"] else "passed"), ""
    return {"verdict": verdict, "compared": compared, "laws": summary,
            "sample": {"source_cases": cases, "draft_replications": seeds}, "note": note}
