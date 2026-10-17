"""Forecast mode: where is this system heading, and how sure are we
(ROADMAP 3.6).

The twin's answer to "what does the next shift look like". It starts from the
system as it is NOW (data bindings for the parameters, ROADMAP 3.4a; an initial
state for the work already in progress, ROADMAP 3.4b), runs a TERMINATING
horizon with no warm-up -- a warm-up would throw away exactly the transient the
forecast is about -- and reports what happened over many replications.

    from nagasim import forecast
    result = forecast.forecast(layout, horizon=480, replications=200,
                               events=[{"name": "queue above 8",
                                        "kpi": "timeseries/resource/Nurse/queue/max",
                                        "op": ">", "value": 8}])
    result.bands        # per series and sample time: mean, q05, q95
    result.kpis         # end-of-horizon KPI distribution per replication
    result.events       # probability of each event, with a Wilson interval

THE BAND IS A PREDICTION BAND, NOT A CONFIDENCE INTERVAL. At each sample time
it is the EMPIRICAL 5th and 95th percentile of the replication values at that
time: the range a single future day is likely to fall in. A confidence interval
of the mean answers a different question -- where the AVERAGE day sits -- and
shrinks towards zero width as replications grow, which this band does not and
must not. Both are useful; conflating them is how a forecast ends up promising
a precision it never had. The mean line is drawn through the middle for
reference, and `kpis` carries the Student-t CI of each end-of-horizon KPI mean
for whoever does want that.

EVENTS are user questions of the shape "KPI beyond a threshold": each is
evaluated once per replication, and the reported probability is the share of
replications where it held, with a WILSON interval (`stats.wilson_interval`)
rather than a normal one -- a forecast event probability is routinely 0 or 1,
where the normal interval collapses to zero width exactly when the answer is
least certain.

Beyond the per-replication `flatten_kpis` keys, an event or a KPI may name a
time-series extreme, `timeseries/<series>/max` and `/min`: the peak that series
reached during the horizon in that replication. "Was the queue ever above 8"
is a question about the peak, not the average.

SCHEDULED RE-RUNS are deliberately NOT a daemon here. A twin that refreshes
every hour is `naga forecast model.json --horizon 480 --out fc.xlsx` in the
OS scheduler (Task Scheduler, cron, systemd timer) -- which already handles
restarts, logging, overlap and failure mail, and is what an operations team
already runs. There is nothing for a bespoke scheduler in this package to add.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from nagasim import api, experiments, netguard
from nagasim._cli import add_model_argument, setup_cli
from nagasim.core.stats import ci95, wilson_interval

__all__ = ["ForecastResult", "forecast", "bands", "parse_event", "series_extreme_kpis",
           "order_outlook", "BAND_COLUMNS", "KPI_COLUMNS", "EVENT_COLUMNS", "ORDER_COLUMNS", "main"]

log = logging.getLogger(__name__)

BAND_COLUMNS = ["Series", "Time", "Mean", "Q05", "Q95", "N"]
ORDER_COLUMNS = ["Order", "Due", "Median Completion", "Q05", "Q95", "P(late)", "Wilson LB",
                 "Wilson UB", "Completed Reps", "Replications"]
KPI_COLUMNS = ["KPI", "Mean", "LB", "UB", "Q05", "Q95", "Min", "Max", "N"]
EVENT_COLUMNS = ["Event", "KPI", "Condition", "Probability", "Wilson LB", "Wilson UB",
                 "Replications"]
_OPS = {">": np.greater, ">=": np.greater_equal, "<": np.less, "<=": np.less_equal}
# Series worth a fan chart by default: system WIP and every resource queue.
_DEFAULT_SERIES_PREFIXES = ("system/",)


def series_extreme_kpis(results: dict) -> pd.DataFrame:
    """Per replication, the max and min each sampled series reached, as
    `timeseries/<series>/max` and `/min` rows in `flatten_kpis`' own
    Run / KPI / Value shape.

    Kept here rather than in `experiments.flatten_kpis` on purpose: every
    scenario comparison, DOE and optimisation run would otherwise grow a pile
    of extra KPI rows nobody asked for. A forecast is the one place a peak is
    the question.
    """
    df = results.get("timeseries")
    if df is None or df.empty:
        return pd.DataFrame(columns=["Run", "KPI", "Value"])
    grouped = df.groupby(["Run", "Series"])["Value"].agg(["max", "min"]).reset_index()
    rows = []
    for run, series, high, low in zip(grouped["Run"], grouped["Series"],
                                      grouped["max"], grouped["min"]):
        rows.append((run, f"timeseries/{series}/max", float(high)))
        rows.append((run, f"timeseries/{series}/min", float(low)))
    return pd.DataFrame(rows, columns=["Run", "KPI", "Value"])


def order_outlook(results: dict, quantiles=(0.05, 0.95), confidence: float = 0.95) -> pd.DataFrame:
    """"Will order X be late" (PHASE4 A3): per declared order, the spread of
    its completion time across the replications and the probability it misses
    its due date, with a WILSON interval -- the same reason events use one, and
    more pressing here: an order is routinely late in 0 or in all of the
    replications, exactly where the normal interval has zero width.

    The completion median and band are EMPIRICAL quantiles over the
    replications in which the order actually finished within the horizon, so
    they describe a finished order and never a censored one; `Completed Reps`
    says how many those were. An order that finished in NO replication still
    gets a row -- NaN times and P(late) 1.0, which is the honest answer when a
    horizon ends with the job still on the floor.
    """
    df = results.get("order_summary")
    if df is None or df.empty:
        return pd.DataFrame(columns=ORDER_COLUMNS)
    low, high = float(quantiles[0]), float(quantiles[1])
    replications = int(df["Run"].nunique())
    rows = []
    for order, group in df.groupby("Order", sort=True):
        completions = pd.to_numeric(group["Completion Time"], errors="coerce").dropna()
        due = pd.to_numeric(group["Due"], errors="coerce").dropna()
        due = float(due.iloc[0]) if not due.empty else np.nan
        # Unfinished counts as late: the order missed a due date inside the
        # horizon, or the horizon ended before anyone could tell it did not.
        late = int((pd.to_numeric(group["Late"], errors="coerce").fillna(1.0) > 0).sum())
        stat = wilson_interval(late, replications, confidence)
        values = completions.to_numpy(dtype=float)
        rows.append({
            "Order": str(order), "Due": due,
            "Median Completion": float(np.median(values)) if values.size else np.nan,
            "Q05": float(np.quantile(values, low)) if values.size else np.nan,
            "Q95": float(np.quantile(values, high)) if values.size else np.nan,
            "P(late)": stat["p"], "Wilson LB": stat["lb"], "Wilson UB": stat["ub"],
            "Completed Reps": int(values.size), "Replications": replications,
        })
    return pd.DataFrame(rows, columns=ORDER_COLUMNS)


@dataclass
class ForecastResult:
    """What `forecast` returns: the bands, the end-of-horizon KPI distribution,
    the event probabilities, and the raw frames behind them."""

    horizon: float
    replications: int
    bands: pd.DataFrame
    kpis: pd.DataFrame
    events: pd.DataFrame
    # PHASE4 A3: one row per declared order, empty for a model with none.
    orders: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=ORDER_COLUMNS))
    results: dict = field(default_factory=dict)
    bindings: object | None = None
    quantiles: tuple = (0.05, 0.95)

    def band(self, series: str) -> pd.DataFrame:
        """One series' band, ordered by sample time -- what a fan chart draws."""
        return self.bands[self.bands["Series"] == series].sort_values("Time")

    @property
    def series_names(self) -> list[str]:
        return sorted(self.bands["Series"].unique().tolist()) if not self.bands.empty else []


def bands(results: dict, quantiles: tuple) -> pd.DataFrame:
    """Per (series, sample time): the across-replication mean and the empirical
    prediction band -- NOT a CI of the mean (see the module docstring).

    Public (PHASE5 SD-4) so `optimize.sensitivity` can build the same
    `BAND_COLUMNS` shape from a Latin-hypercube sweep's "replications" (one
    CRN run per design point) instead of a forecast's Monte Carlo ones -- the
    band is just an empirical quantile of *results["timeseries"]* either way.
    """
    df = results.get("timeseries")
    if df is None or df.empty:
        return pd.DataFrame(columns=BAND_COLUMNS)
    low, high = float(quantiles[0]), float(quantiles[1])
    rows = []
    for (series, time), group in df.groupby(["Series", "Time"], sort=True):
        values = pd.to_numeric(group["Value"], errors="coerce").dropna().to_numpy(dtype=float)
        if values.size == 0:
            continue
        rows.append({"Series": series, "Time": float(time), "Mean": float(values.mean()),
                     "Q05": float(np.quantile(values, low)),
                     "Q95": float(np.quantile(values, high)), "N": int(values.size)})
    return pd.DataFrame(rows, columns=BAND_COLUMNS)


def _kpi_table(flat: pd.DataFrame, wanted, quantiles: tuple) -> pd.DataFrame:
    """End-of-horizon distribution of each KPI across replications: the mean
    with its Student-t CI (the average day) AND the empirical quantiles and
    extremes (a single day)."""
    if flat.empty:
        return pd.DataFrame(columns=KPI_COLUMNS)
    keys = sorted(flat["KPI"].unique().tolist())
    if wanted is not None:
        keys = [k for k in keys if k in set(wanted)]
    low, high = float(quantiles[0]), float(quantiles[1])
    rows = []
    for key in keys:
        values = flat.loc[flat["KPI"] == key, "Value"].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if values.size == 0:
            continue
        stat = ci95(values)
        rows.append({"KPI": key, "Mean": stat["mean"], "LB": stat["lb"], "UB": stat["ub"],
                     "Q05": float(np.quantile(values, low)),
                     "Q95": float(np.quantile(values, high)),
                     "Min": float(values.min()), "Max": float(values.max()),
                     "N": int(values.size)})
    return pd.DataFrame(rows, columns=KPI_COLUMNS)


def _events(flat: pd.DataFrame, events, confidence: float) -> pd.DataFrame:
    """Each event's probability over the replications, with a Wilson interval."""
    rows = []
    for index, event in enumerate(events or ()):
        kpi = event.get("kpi", "")
        op = event.get("op", ">")
        threshold = float(event.get("value", 0))
        compare = _OPS.get(op)
        if compare is None:
            raise ValueError(f"event '{kpi}': unknown operator {op!r} "
                             f"(expected one of {', '.join(_OPS)})")
        values = flat.loc[flat["KPI"] == kpi, "Value"].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if values.size == 0:
            raise ValueError(f"unknown forecast KPI: {kpi!r}")
        hits = int(compare(values, threshold).sum())
        stat = wilson_interval(hits, int(values.size), confidence)
        rows.append({"Event": event.get("name") or f"event {index + 1}", "KPI": kpi,
                     "Condition": f"{op} {threshold:g}", "Probability": stat["p"],
                     "Wilson LB": stat["lb"], "Wilson UB": stat["ub"],
                     "Replications": stat["n"]})
    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


def forecast(layout, *, horizon: float, replications: int = 100, kpis=None, events=(),
             bind_data: bool = True, base_dir=None, network=None, confine_to=None, seed: int = 0,
             workers: int | str = 1, confidence: float = 0.95, quantiles=(0.05, 0.95),
             on_progress=None, should_stop=None) -> ForecastResult:
    """Run a short terminating horizon from the system's current state.

    *horizon* is the run length in base time units and there is NO warm-up: the
    forecast is about the transient from the initial state, which a warm-up
    would discard. *bind_data* resolves the model's data bindings once before
    the replications (`api.run_model`), so the run uses today's parameters.

    *kpis* narrows the end-of-horizon table (default: every KPI, including the
    `timeseries/<series>/max` and `/min` peaks). *events* are
    `{"name", "kpi", "op", "value"}` dicts, each answered as a probability with
    a Wilson interval.
    """
    base = api.load_layout(layout)
    if not horizon or float(horizon) <= 0:
        raise ValueError("forecast needs a positive horizon")

    results = api.run_model(base, replications=replications, seed=seed, run_length=float(horizon),
                            warmup=0.0, workers=workers, on_progress=on_progress,
                            should_stop=should_stop, bind_data=bind_data, base_dir=base_dir,
                            network=network, confine_to=confine_to,
                            # PHASE4 T7: a forecast reads the summary frames, the
                            # order frame and the time series -- never a per-event log.
                            record_logs=False)
    if results is None:
        raise ValueError("the forecast produced no completed replication")

    flat = pd.concat([experiments.flatten_kpis(results), series_extreme_kpis(results)],
                     ignore_index=True)
    return ForecastResult(
        horizon=float(horizon), replications=int(results["system"]["Run"].nunique()),
        bands=bands(results, quantiles), kpis=_kpi_table(flat, kpis, quantiles),
        events=_events(flat, events, confidence),
        orders=order_outlook(results, quantiles, confidence), results=results,
        bindings=results.get("data_inputs"), quantiles=tuple(quantiles))


# ---------------------------------------------------------------- CLI
_EVENT_HELP = ("an event to report the probability of, as '<kpi key><op><value>' with op one of "
               "> >= < <=. May repeat. Example: 'timeseries/system/WIP/max>8'")


def parse_event(spec: str) -> dict:
    """`<kpi key><op><value>`, longest operator first so '>=' beats '>'."""
    for op in (">=", "<=", ">", "<"):
        kpi, sep, value = spec.partition(op)
        if sep and kpi.strip() and value.strip():
            return {"name": f"{kpi.strip()} {op} {value.strip()}", "kpi": kpi.strip(),
                    "op": op, "value": float(value)}
    raise ValueError(f"event {spec!r}: expected '<kpi key><op><value>' with op one of "
                     f">, >=, <, <=")


def main(argv=None) -> int:
    """`naga forecast model.json --horizon 480 --replications 200`: a
    short-horizon forecast with uncertainty bands, headless. Dispatched from
    `nagasim.cli` without importing Qt."""
    setup_cli()

    parser = argparse.ArgumentParser(
        prog="naga forecast",
        description="Forecast a short horizon from the model's current state, with prediction "
                    "bands and event probabilities. Schedule repeated runs with the OS scheduler "
                    "(Task Scheduler, cron); this command is the unit of work, not a daemon.")
    add_model_argument(parser)
    parser.add_argument("--horizon", type=float, required=True,
                        help="run length in base time units")
    parser.add_argument("--replications", type=int, default=100)
    parser.add_argument("--event", action="append", dest="events", default=None, help=_EVENT_HELP)
    parser.add_argument("--kpi", action="append", dest="kpis", default=None,
                        help="KPI key to report; may repeat (default: all)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1,
                        help="replications to run in parallel processes (default: 1)")
    parser.add_argument("--no-bind", dest="bind", action="store_false",
                        help="skip the model's data bindings (they resolve by default here)")
    netguard.add_arguments(parser)
    parser.add_argument("--out", help="write the forecast tables here (.xlsx or .csv)")
    parser.add_argument("--report", help="write an HTML report here")
    args = parser.parse_args(argv)

    path = Path(args.model)
    result = forecast(str(path), horizon=args.horizon, replications=args.replications,
                      kpis=args.kpis, events=[parse_event(s) for s in (args.events or [])],
                      bind_data=args.bind, base_dir=path.parent,
                      network=netguard.policy_from_args(args), seed=args.seed,
                      workers=args.workers)

    print(result.kpis.to_string(index=False))
    if not result.orders.empty:
        print()
        print(result.orders.to_string(index=False))
    if not result.events.empty:
        print()
        print(result.events.to_string(index=False))
    if args.out:
        _write(result, args.out)
    if args.report:
        from nagasim.report import build_report

        layout = api.load_layout(path)
        build_report(layout, result.results, forecast=result, title=path.stem, path=args.report)
        print(f"Report written to {args.report}")
    return 0


def _write(result: ForecastResult, out: str) -> None:
    path = Path(out)
    if path.suffix.lower() == ".csv":
        result.kpis.to_csv(path, index=False)
    else:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            result.kpis.to_excel(writer, sheet_name="KPIs", index=False)
            result.bands.to_excel(writer, sheet_name="Bands", index=False)
            result.events.to_excel(writer, sheet_name="Events", index=False)
            if not result.orders.empty:
                result.orders.to_excel(writer, sheet_name="Orders", index=False)
    print(f"Written to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
