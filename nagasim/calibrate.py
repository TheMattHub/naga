"""Calibration: tune model parameters until the model matches reality
(ROADMAP 3.5).

A model is credible when it reproduces what the real system did. Calibration
takes measured KPIs -- "our average wait was 8.4 minutes, our utilization was
72%" -- and searches the parameters that make the simulation say the same.

    from nagasim import calibrate, optimize
    targets = [{"kpi": "system/Avg Throughput Time", "observed": 8.4, "tolerance": 0.5},
               {"kpi": "resource/Nurse/Usage % (snapshot)", "observed": 72.0, "weight": 2.0}]
    factors = [optimize.Factor("blocks.Consult.params.dist_params.rate", "continuous", 0.05, 0.5)]
    result = calibrate.calibrate(layout, targets, factors, budget=200)
    result.overrides          # "Apply as scenario": the tuned parameter values

OBJECTIVE. For a scalar target the error is the RELATIVE one,
`(simulated - observed) / observed` (absolute when observed is 0), squared and
weighted; relative, so a utilization in per cent and a wait in minutes count
equally without anyone having to pick a scale. A distributional target
(`observed_sample`) contributes the two-sample Kolmogorov-Smirnov statistic
between the observed sample and the replication's own simulated sample,
squared and weighted the same way: a number in [0, 1] that is 0 only when the
two empirical distributions coincide.

The objective is computed PER REPLICATION and the search minimises its mean.
That matters and is a deliberate choice: minimising `E[(x - observed)^2]`
penalises variance as well as bias, so between two parameter settings that hit
the observed mean equally well it prefers the more stable one. It is also what
lets the whole thing reuse `optimize.optimize` unchanged -- every one of its
paired-t comparisons, its no-revisit cache and its KN confirmation step work on
a per-replication response, and calibration is then only a different response
KPI, injected through the evaluator's `extra` hook.

SEARCH. `optimize.optimize` itself, with no duplicate implementation: the same
Latin-hypercube pilot, pattern search, CRN pairing and final ranking-and-
selection. Calibration only supplies the objective and reads the result.

TIME SERIES (PHASE6 S6). A `SeriesTarget` fits a whole trajectory -- a level,
an auxiliary, an agent state count -- to measured points with Vensim
Optimize's payoff: `sum over data points of w * (model(t) - data(t))^2`.
`model(t)` is the "timeseries" frame LINEARLY INTERPOLATED at the data times:
the sampler runs on its own fixed grid (`start + k * run_length /
timeseries_points`) and cannot be pointed at arbitrary times, so data on that
grid is read exactly and data between two samples is interpolated -- raise
`timeseries_points` if that matters. A numeric weight multiplies the squared
error; `"1/sd"` is Vensim's convention, the error measured in standard
deviations of the data (`((model - data) / sd)^2`), so series in different
units add up fairly. The payoff is per replication like every other term, then
averaged over the CRN seeds.

PASS / FAIL. A target with a `tolerance` passes when `|simulated mean -
observed| <= tolerance`. That is a point verdict on the mean, not a statistical
test; the per-target confidence interval is reported beside it so a "pass" with
an interval three times the tolerance is visible for what it is.
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from nagasim import api, experiments, optimize as optimize_mod
from nagasim._cli import add_model_argument, setup_cli
from nagasim.core import schema
from nagasim.core.stats import ci95

__all__ = ["CalibrationTarget", "SeriesTarget", "CalibrationResult", "OBJECTIVE_KPI",
           "RESIDUAL_COLUMNS", "calibrate", "series_residuals", "parse_target", "main"]

log = logging.getLogger(__name__)

# The synthetic response key the search minimises. Namespaced like every
# `flatten_kpis` key, and never produced by a run, so it cannot collide.
OBJECTIVE_KPI = "calibration/objective"
_KS_PREFIX = "calibration/ks/"
_SERIES_PREFIX = "calibration/series/"
RESIDUAL_COLUMNS = ["Time", "Data", "Model", "Residual", "Weight"]
# The entity-log column a distributional target names, as a flatten-style key.
SAMPLE_KPIS = {"entity_log/Throughput Time": "Throughput Time",
               "entity_log/Cost": "Cost"}

RESULT_COLUMNS = ["Target", "KPI", "Observed", "Simulated", "LB", "UB", "Error",
                  "Relative error", "Tolerance", "Pass"]


@dataclass
class CalibrationTarget:
    """One thing the model has to reproduce. Either a scalar KPI (`observed`)
    or a distribution to match by the KS statistic (`observed_sample`)."""

    kpi: str
    observed: float | None = None
    observed_sample: list | None = None
    weight: float = 1.0
    tolerance: float | None = None

    @property
    def distributional(self) -> bool:
        return self.observed_sample is not None

    @property
    def response(self) -> str:
        return _KS_PREFIX + self.kpi if self.distributional else self.kpi


@dataclass
class SeriesTarget:
    """A trajectory to reproduce (PHASE6 S6): the timeseries key *series*
    (`level/<name>`, `aux/<name>`, `agents/<pop>/<state>`, ...) against *data*,
    a DataFrame (*time_col*, *value_col*), a list of (t, value) pairs, or the
    name of a 1-D model table keyed by time. *weight*: None (1), a number, or
    "1/sd" (see the module docstring)."""

    series: str
    data: object
    weight: float | str | None = None
    time_col: str = "Time"
    value_col: str = "Value"
    times: np.ndarray = field(init=False, repr=False, default=None)
    values: np.ndarray = field(init=False, repr=False, default=None)
    point_weight: float = field(init=False, repr=False, default=1.0)
    tolerance = None            # a payoff has no natural pass/fail threshold

    @property
    def kpi(self) -> str:
        return self.series

    @property
    def response(self) -> str:
        return _SERIES_PREFIX + self.series

    def resolve(self, layout) -> "SeriesTarget":
        """Read *data* into sorted `times` / `values` and settle the weight."""
        data = self.data
        if isinstance(data, str):
            table = next((t for t in layout.get("tables") or [] if t.get("name") == data), None)
            if table is None or table.get("columns"):
                raise ValueError(f"series target '{self.series}': no 1-D model table {data!r}")
            pairs = list(zip(table["keys"], table["values"]))
        elif isinstance(data, pd.DataFrame):
            pairs = list(zip(data[self.time_col], data[self.value_col]))
        else:
            pairs = [tuple(p) for p in data]
        arr = np.array(sorted(pairs), dtype=float).reshape(-1, 2)
        if not len(arr) or not np.isfinite(arr).all():
            raise ValueError(f"series target '{self.series}': data needs finite (t, value) points")
        self.times, self.values = arr[:, 0], arr[:, 1]
        if self.weight == "1/sd":
            sd = float(np.std(self.values, ddof=1)) if len(self.values) > 1 else 0.0
            if not sd > 0:
                raise ValueError(f"series target '{self.series}': weight 1/sd needs data that varies")
            self.point_weight = 1.0 / (sd * sd)
        else:
            self.point_weight = 1.0 if self.weight is None else float(self.weight)
        return self


def series_residuals(results, target: SeriesTarget) -> pd.DataFrame:
    """Per replication and data point: Run, Time, Data, Model (the series
    interpolated at the data time), Residual (Model - Data), Weight. One
    replication's payoff is `sum(Weight * Residual**2)` over its rows."""
    df = results.get("timeseries")
    if df is None or df.empty:
        raise ValueError("series targets need the timeseries frame: sampling is off "
                         "(simulation.timeseries_points, or no run length)")
    rows = df[df["Series"] == target.series]
    if rows.empty:
        known = ", ".join(sorted(df["Series"].unique()))
        raise ValueError(f"unknown series {target.series!r}; the run sampled: {known}")
    frames = []
    for run, group in rows.groupby("Run", sort=True):
        group = group.sort_values("Time")
        t, v = group["Time"].to_numpy(dtype=float), group["Value"].to_numpy(dtype=float)
        if target.times[0] < t[0] or target.times[-1] > t[-1]:
            raise ValueError(f"series target '{target.series}': data spans "
                             f"[{target.times[0]:g}, {target.times[-1]:g}], the run sampled "
                             f"[{t[0]:g}, {t[-1]:g}]")
        model = np.interp(target.times, t, v)
        frames.append(pd.DataFrame({"Run": int(run), "Time": target.times, "Data": target.values,
                                    "Model": model, "Residual": model - target.values,
                                    "Weight": target.point_weight}))
    return pd.concat(frames, ignore_index=True)


def _is_distance(target) -> bool:
    """A target whose response is itself a distance to 0 (KS, series payoff)."""
    return isinstance(target, SeriesTarget) or target.distributional


def _targets(targets, layout=None) -> list:
    out = []
    for entry in targets:
        if isinstance(entry, dict) and "series" in entry:
            entry = SeriesTarget(**entry)
        if isinstance(entry, SeriesTarget):
            # One response key per series: a second target on it would overwrite
            # the first's payoff, and one target with both data sets is the same fit.
            if any(isinstance(t, SeriesTarget) and t.series == entry.series for t in out):
                raise ValueError(f"two series targets on '{entry.series}': merge their data")
            out.append(entry.resolve(layout or {}))
            continue
        if isinstance(entry, CalibrationTarget):
            out.append(entry)
            continue
        target = CalibrationTarget(
            kpi=entry["kpi"], observed=entry.get("observed"),
            observed_sample=(list(entry["observed_sample"])
                             if entry.get("observed_sample") is not None else None),
            weight=float(entry.get("weight", 1.0)),
            tolerance=(float(entry["tolerance"]) if entry.get("tolerance") is not None else None))
        if target.observed is None and not target.distributional:
            raise ValueError(f"target '{target.kpi}': needs an \"observed\" value or an "
                             f"\"observed_sample\"")
        if target.distributional and target.kpi not in SAMPLE_KPIS:
            raise ValueError(f"target '{target.kpi}': a distributional target must name one of "
                             f"{', '.join(sorted(SAMPLE_KPIS))}")
        out.append(target)
    return out


def _relative_error(simulated: float, observed: float) -> float:
    """(simulated - observed) / observed, falling back to the absolute error
    when observed is 0 (nothing to be relative to)."""
    if not np.isfinite(simulated):
        return float("inf")
    return (simulated - observed) / observed if observed else simulated - observed


def _ks_by_run(frames, column: str, observed_sample) -> dict[int, float]:
    """The two-sample KS statistic per replication, between *observed_sample*
    and that replication's own values of *column* in the entity log."""
    log_df = frames.get("entity_log")
    if log_df is None or log_df.empty or column not in log_df.columns:
        return {}
    reference = np.asarray(observed_sample, dtype=float)
    out: dict[int, float] = {}
    for run, group in log_df.groupby("Run", sort=True):
        values = pd.to_numeric(group[column], errors="coerce").dropna().to_numpy(dtype=float)
        out[int(run)] = (float(sp_stats.ks_2samp(values, reference).statistic)
                         if values.size else 1.0)
    return out


def _extra_kpis(targets: list):
    """The evaluator's `extra` hook: per replication, the KS statistic of each
    distributional target, the payoff of each series target, plus the weighted
    objective every target feeds."""

    def extra(frames) -> dict[str, dict[int, float]]:
        flat = experiments.flatten_kpis(frames)
        by_kpi: dict[str, dict[int, float]] = {}
        for run, kpi, value in zip(flat["Run"], flat["KPI"], flat["Value"]):
            by_kpi.setdefault(kpi, {})[int(run)] = float(value)

        out: dict[str, dict[int, float]] = {}
        for target in targets:
            if isinstance(target, SeriesTarget):
                res = series_residuals(frames, target)
                payoff = (res["Weight"] * res["Residual"] ** 2).groupby(res["Run"]).sum()
                out[target.response] = {int(r): float(v) for r, v in payoff.items()}
            elif target.distributional:
                out[target.response] = _ks_by_run(frames, SAMPLE_KPIS[target.kpi],
                                                  target.observed_sample)

        # Every replication the frames describe: a model whose only target is
        # distributional contributes no `flatten_kpis` rows of its own, so the
        # KS dictionaries are part of the union, not an afterthought.
        runs = {int(r) for r in flat["Run"]} if not flat.empty else set()
        runs.update(run for by_run in out.values() for run in by_run)
        objective: dict[int, float] = {}
        for run in sorted(runs):
            total = 0.0
            for target in targets:
                if isinstance(target, SeriesTarget):   # already weighted and squared
                    total += out[target.response].get(run, float("inf"))
                    continue
                if target.distributional:
                    error = out.get(target.response, {}).get(run, 1.0)
                else:
                    simulated = by_kpi.get(target.kpi, {}).get(run)
                    if simulated is None:
                        raise ValueError(f"unknown calibration KPI: {target.kpi!r}")
                    error = _relative_error(simulated, target.observed)
                total += target.weight * error * error
            objective[run] = total
        out[OBJECTIVE_KPI] = objective
        return out

    return extra


@dataclass
class CalibrationResult:
    """What `calibrate` returns.

    `table` is one row per target: observed against the simulated mean with its
    Student-t CI, the error, and the tolerance verdict (a KS or series target
    reports its distance, observed 0). `residuals` maps each series target to
    the best point's `RESIDUAL_COLUMNS` frame, averaged over its replications.
    `overrides` is the
    "Apply as scenario" answer -- hand it to `document.set_scenarios` or
    `experiments.apply_overrides` to keep the tuned values.
    """

    best: dict
    overrides: list
    table: pd.DataFrame
    objective: float
    history: pd.DataFrame
    budget_used: int
    converged: bool
    search: object | None = None
    targets: list = field(default_factory=list)
    residuals: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when every target that declared a tolerance is inside it."""
        rows = self.table[self.table["Tolerance"].notna()]
        return bool(rows.empty or rows["Pass"].all())

    def as_scenario(self, name: str = "Calibrated") -> dict:
        return {"name": name, "description": "Parameters fitted to observed data",
                "overrides": [dict(o) for o in self.overrides]}


def calibrate(layout, targets, factors, *, budget: int = 200, replications_per_point: int = 5,
              seed: int = 0, workers: int = 1, run_length=None, warmup=None,
              confidence: float = 0.95, evaluator=None, on_progress=None,
              should_stop=None, record_logs=None) -> CalibrationResult:
    """Tune factors until the model reproduces observed targets.

    Every target's term is summed into one objective, which `nagasim.optimize`
    minimises.

    Parameters
    ----------
    layout : dict or str
        The model, or a path to it.
    targets : list
        ``CalibrationTarget`` objects or dicts: ``{"kpi", "observed", "weight"?,
        "tolerance"?}``; ``{"kpi": "entity_log/Throughput Time",
        "observed_sample": [...]}`` for a distribution matched by the KS
        statistic; or ``SeriesTarget`` objects (``{"series", "data", "weight"?}``)
        for a trajectory.
    factors : list of Factor
        What to tune, addressing the model through the same override paths that
        scenarios use.
    budget : int, default 200
        Hard cap on replications.
    replications_per_point : int, default 5
        Replications of each evaluated point.
    seed : int, default 0
        Seed of replication 0.
    workers : int, default 1
        Process-pool size.
    run_length, warmup : optional
        Fall back to the model's own settings.
    confidence : float, default 0.95
        Level of the intervals used by the search.
    evaluator : optional
        The evaluation seam, for a caller or a test that supplies its own. The
        default builds the optimizer's evaluator with the calibration objective
        wired in and *budget* as its cap.
    on_progress, should_stop : callable, optional
        As in `nagasim.api.run_model`.
    record_logs : bool, optional
        As in `nagasim.api.run_model`.

    Returns
    -------
    CalibrationResult
        The fitted factor values and the fit of each target.

    See Also
    --------
    nagasim.optimize.optimize : The search it is built on.
    nagasim.inputfit.best_fit : Fit one input distribution to a sample.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim.calibrate import calibrate
    >>> from nagasim.optimize import Factor
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> factor = Factor(path="blocks.process_1.params.dist_params.rate",
    ...                 kind="continuous", low=1.1, high=2.0, name="rate")
    >>> target = {"kpi": "system/Avg Queue Length", "observed": 2.0}
    >>> fit = calibrate(str(path), [target], [factor], budget=12,
    ...                 replications_per_point=2, run_length=200, warmup=20, seed=0)
    >>> type(fit).__name__
    'CalibrationResult'
    """
    base = api.load_layout(layout)
    wanted = _targets(targets, base)
    if not wanted:
        raise ValueError("calibrate needs at least one target")
    factors = list(factors)

    evaluator = evaluator or optimize_mod._Evaluator(
        base, run_length=run_length, warmup=warmup, workers=workers, budget=int(budget),
        extra=_extra_kpis(wanted), record_logs=record_logs,
        kpis=[t.kpi for t in wanted if isinstance(t, CalibrationTarget)])

    search = optimize_mod.optimize(
        base, factors, response=OBJECTIVE_KPI, goal="min", budget=int(budget),
        replications_per_point=replications_per_point, seed=seed, confidence=confidence,
        workers=workers, run_length=run_length, warmup=warmup, on_progress=on_progress,
        should_stop=should_stop, evaluator=evaluator)

    # The winner's own replications, free from the evaluator's cache.
    key = optimize_mod._point_key(factors, search.best)
    seeds = [seed + r for r in range(replications_per_point)]
    values = evaluator.values(key, search.best_overrides, seeds)

    rows = []
    for index, target in enumerate(wanted):
        series = values.get(target.response)
        stat = ci95(series if series is not None else [])
        simulated = stat["mean"]
        if _is_distance(target):
            observed, error = 0.0, simulated       # the KS statistic / payoff IS the distance
        else:
            observed = float(target.observed)
            error = simulated - observed
        tolerance = target.tolerance
        rows.append({
            "Target": f"target {index + 1}", "KPI": target.kpi, "Observed": observed,
            "Simulated": simulated, "LB": stat["lb"], "UB": stat["ub"], "Error": error,
            "Relative error": (simulated if _is_distance(target)
                               else _relative_error(simulated, observed)),
            "Tolerance": tolerance,
            "Pass": bool(abs(error) <= tolerance) if tolerance is not None else None,
        })

    # The residual frames need the winner's timeseries, which the evaluator's
    # cache does not keep (only per-replication numbers): one more run of it.
    residuals = {}
    series_targets = [t for t in wanted if isinstance(t, SeriesTarget)]
    if series_targets:
        reps = 1 if getattr(evaluator, "deterministic", None) else replications_per_point
        frames = api.run_model(experiments.apply_overrides(base, search.best_overrides),
                               replications=reps, seed=seed, run_length=run_length,
                               warmup=warmup, workers=workers)
        for target in series_targets:
            res = series_residuals(frames, target)
            residuals[target.series] = (res.groupby("Time", sort=True)[RESIDUAL_COLUMNS[1:]]
                                        .mean().reset_index()[RESIDUAL_COLUMNS])

    objective = float(np.nanmean(values.get(OBJECTIVE_KPI, [np.nan])))
    return CalibrationResult(
        best=dict(search.best), overrides=list(search.best_overrides),
        table=pd.DataFrame(rows, columns=RESULT_COLUMNS), objective=objective,
        history=search.history, budget_used=search.budget_used, converged=search.converged,
        search=search, targets=wanted, residuals=residuals)


# ---------------------------------------------------------------- CLI
_TARGET_HELP = ("an observed KPI, as '<kpi key>=<value>[:<tolerance>]'. May repeat. Example: "
                "'system/Avg Throughput Time=8.4:0.5'")


def parse_target(spec: str) -> dict:
    """`<kpi key>=<value>[:<tolerance>]`."""
    kpi, sep, rest = spec.partition("=")
    if not sep or not kpi.strip() or not rest.strip():
        raise ValueError(f"target {spec!r}: expected '<kpi key>=<value>[:<tolerance>]'")
    value, _, tolerance = rest.partition(":")
    out = {"kpi": kpi.strip(), "observed": float(value)}
    if tolerance.strip():
        out["tolerance"] = float(tolerance)
    return out


def main(argv=None) -> int:
    """`naga calibrate model.json --factor ... --target ...`: fit model
    parameters to observed KPIs headless. Dispatched from `nagasim.cli`
    without importing Qt."""
    setup_cli()

    parser = argparse.ArgumentParser(
        prog="naga calibrate",
        description="Tune model parameters until the model reproduces observed KPIs.")
    add_model_argument(parser)
    parser.add_argument("--factor", action="append", dest="factors", required=True,
                        help=optimize_mod._FACTOR_HELP)
    parser.add_argument("--target", action="append", dest="targets", required=True,
                        help=_TARGET_HELP)
    parser.add_argument("--budget", type=int, default=200,
                        help="total replications the search may spend (default: 200)")
    parser.add_argument("--replications", type=int, default=5,
                        help="replications per candidate point (default: 5)")
    parser.add_argument("--run-length", type=float, default=None,
                        help="default: the model's own 'simulation.time_limit'")
    parser.add_argument("--warmup", type=float, default=None,
                        help="default: the model's own 'simulation.warmup'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1,
                        help="replications to run in parallel processes (default: 1)")
    parser.add_argument("--out", help="write the target table here (.xlsx or .csv)")
    parser.add_argument("--report", help="write an HTML report here")
    parser.add_argument("--write-scenario",
                        help="write the model back here with the tuned values as a scenario")
    args = parser.parse_args(argv)

    layout = api.load_layout(args.model)
    result = calibrate(layout, [parse_target(s) for s in args.targets],
                       [optimize_mod.parse_factor(s) for s in args.factors],
                       budget=args.budget, replications_per_point=args.replications,
                       seed=args.seed, workers=args.workers, run_length=args.run_length,
                       warmup=args.warmup)

    print(f"Best point: {optimize_mod._fmt_point(result.best)}")
    print(f"objective = {result.objective:.6g} ({result.budget_used} replications)")
    print()
    print(result.table.to_string(index=False))
    if args.out:
        optimize_mod._write_table(result.table, args.out, "Calibration")
    if args.write_scenario:
        layout["scenarios"] = [s for s in layout.get("scenarios", [])
                               if s.get("name") != "Calibrated"] + [result.as_scenario()]
        Path(args.write_scenario).write_text(json.dumps(layout, indent=2))
        print(f"Model with the 'Calibrated' scenario written to {args.write_scenario}")
    if args.report:
        from nagasim.report import build_report

        results = api.run_model(experiments.apply_overrides(layout, result.overrides),
                                replications=args.replications, seed=args.seed,
                                run_length=args.run_length, warmup=args.warmup)
        build_report(schema.migrate(layout), results, calibration=result,
                     title=Path(args.model).stem, path=args.report)
        print(f"Report written to {args.report}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
