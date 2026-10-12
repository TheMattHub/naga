"""Run and analysis tools: run_model, scenarios, precision, reality checks,
loop dominance, warm-up, optimize, calibrate, forecast, policy evaluation,
surrogate fitting and export (F2: runs and studies).
"""
from __future__ import annotations

import numpy as np

from nagasim import api, experiments, investment, loopdominance, modelcheck
from nagasim import optimize as optimize_mod
from nagasim.exporters import EXPORTER_REGISTRY

from .core import (
    MAX_ROWS, ToolError, _budget, _document_args, _edit_result, _finite, _digest, _open,
    _records, _results_of, _rows, _runnable_layout, _upsert, tool, workspace,
)

_document_args("run_model", model="Workspace name of the model to run.",
               replications="Independent replications; defaults to the model's own setting.",
               run_length="Run length in base time units; defaults to the model's own setting.",
               warmup="Warm-up time before statistics start; defaults to the model's own setting.",
               seed="First replication's seed; replication r uses seed + r.",
               bind_data="Resolve series:/orders data bindings at run time first (default "
                         "false), like the UI/CLI's `--bind`. This does NOT write bulk data "
                         "into the model; it only affects this run.",
               max_seconds="Wall-clock budget; the run stops cleanly and returns what finished.")


@tool()
def run_model(model: str, replications: int | None = None, run_length: float | None = None,
              warmup: float | None = None, seed: int = 0, bind_data: bool = False,
              max_seconds: float | None = None) -> dict:
    """Run a model and return a COMPACT KPI digest -- never the raw frames.

    Validate first: a model with errors will not run. Every KPI comes back as a
    mean with the half-width of its 95% confidence interval over replications;
    a half-width near the mean means you need more replications (see
    `run_to_precision`). One replication gives no interval at all (null).

    `bind_data=true` resolves the model's `series:` (time-series input) and
    `orders` data bindings at RUN TIME, exactly as the UI/CLI's `--bind` does,
    within this workspace's own network permission and file confinement --
    there is no permission argument here, only whether to use it. Use
    `resolve_data_bindings` first to preview what a binding would read.

    The full frames stay in the workspace: page them out with `get_kpi_table`.

    Returns {"model", "replications", "system": {KPI: {mean, ci_half_width}},
    "resources": {name: {metric: {...}}}, "user_stats": {...},
    "frames": [names available to get_kpi_table]}.
    """
    ws = workspace()
    doc = ws.document(model)
    layout = _runnable_layout(doc, model)
    base_dir = doc.path.parent if doc.path is not None else ws.root
    results = api.run_model(layout, replications=replications, seed=seed, warmup=warmup,
                            run_length=run_length, bind_data=bind_data, base_dir=base_dir,
                            network=ws.network, confine_to=ws.root,
                            should_stop=_budget(max_seconds))
    if results is None:
        raise ToolError("E_NO_RESULTS", "the run produced no completed replication",
                        "raise max_seconds, or lower replications/run_length")
    ws.results[model] = results
    digest = _digest(results)
    digest.update({"model": model, "frames": sorted(results)})
    return digest


_document_args("get_kpi_table", model="Workspace name of a model that has been run.",
               frame="Which frame: system, resource_summary, entity_summary, user_summary, "
                     "store_summary, entity_log, resource_log, store_log or timeseries.",
               offset="Rows to skip.",
               limit="Rows to return (at most 200).")


@tool(read_only=True)
def get_kpi_table(model: str, frame: str, offset: int = 0, limit: int = 50) -> dict:
    """Page through one results frame of the last `run_model` for this model.

    Use it when the digest is not enough -- to see per-resource detail, the
    per-entity log, or a time series. Ask for the smallest window you need:
    `entity_log` and `timeseries` have thousands of rows.

    Returns {"model", "frame", "total", "offset", "count", "rows", "has_more",
    "next_offset"}.
    """
    results = _results_of(model)
    data = results.get(frame)
    if data is None:
        raise ToolError("E_UNKNOWN_FRAME", f"no results frame named '{frame}'",
                        f"use one of: {', '.join(sorted(results))}")
    return {"model": model, "frame": frame, **_rows(data, offset, limit)}


_document_args("run_scenarios", model="Workspace name of the model to run.",
               scenarios="Scenario names to run; omit for all of them plus Baseline.",
               kpis="KPI keys to report, e.g. [\"system/Avg Throughput Time\"]; omit for the "
                    "system KPIs.",
               replications="Replications per scenario.",
               run_length="Run length in base time units.",
               warmup="Warm-up time.",
               seed="Base seed; with CRN every scenario uses the same seeds.",
               crn="Common random numbers (default true): pairs the scenarios so the comparison "
                   "uses the low-variance paired-t interval.",
               max_seconds="Wall-clock budget for the whole sweep.")


@tool()
def run_scenarios(model: str, scenarios: list | None = None, kpis: list | None = None,
                  replications: int | None = None, run_length: float | None = None,
                  warmup: float | None = None, seed: int = 0, crn: bool = True,
                  max_seconds: float | None = None) -> dict:
    """Run the model's scenarios and compare them against the baseline with
    confidence intervals and a significance verdict.

    This is the consultancy answer: for each KPI and scenario you get the
    scenario's own mean with its CI, the DIFFERENCE against the baseline with
    its own CI, and whether that difference interval excludes zero
    ("significant"). A non-significant difference means "not distinguishable at
    this replication count", not "no difference".

    Returns {"model", "scenarios", "crn", "comparison": [{"kpi", "scenario",
    "mean", "lb", "ub", "diff", "diff_lb", "diff_ub", "significant"}]}.
    """
    if scenarios is not None and not all(isinstance(s, str) for s in scenarios):
        raise ToolError("E_BAD_ARGUMENT", "scenarios must be a list of scenario names (strings)",
                        "pass scenario name strings, e.g. [\"Second server\"], or omit the "
                        "argument for all of them plus Baseline")
    doc = workspace().document(model)
    result = experiments.run_scenarios(doc.to_layout(), scenarios, replications=replications,
                                       seed=seed, run_length=run_length, warmup=warmup, crn=crn,
                                       should_stop=_budget(max_seconds))
    table = result.comparison(kpis=kpis)
    if kpis is None:
        table = table[table["KPI"].str.startswith("system/")]
    rows = _records(table.head(MAX_ROWS), {
        "kpi": "KPI", "scenario": "Scenario", "mean": ("Mean", _finite),
        "lb": ("LB", _finite), "ub": ("UB", _finite),
        "diff": ("Diff vs baseline", _finite), "diff_lb": ("Diff LB", _finite),
        "diff_ub": ("Diff UB", _finite), "significant": ("Significant", bool),
    })
    return {"model": model, "scenarios": result.scenarios, "crn": result.crn,
            "completed": result.completed, "comparison": rows}


_document_args("investment_case", model="Workspace name of the model to run; it needs an "
                                        "`investment` section.",
               replications="Independent replications (capped at 200, like run_to_precision); "
                            "defaults to the model's own setting.",
               seed="First replication's seed; replication r uses seed + r.")


@tool()
def investment_case(model: str, replications: int | None = None, seed: int = 0) -> dict:
    """Run the model's scenarios and price each against the baseline: extra
    capex, saving per year, payback, NPV and ROI over the `investment`
    section's horizon, with confidence intervals from several replications.

    Returns {"model", "settings", "table": [one record per scenario, baseline
    first, with the engine's column names; blanks are null]}. A model without
    an `investment` section answers E_NO_INVESTMENT.
    """
    layout = workspace().document(model).to_layout()
    if layout.get("investment") is None:
        raise ToolError("E_NO_INVESTMENT", f"model '{model}' has no investment section",
                        "add an `investment` section (and flag upfront fixed_costs with "
                        "capex: true), then call this again")
    if replications is not None:
        replications = max(1, min(int(replications), 200))
    result = experiments.run_scenarios(layout, replications=replications, seed=seed)
    try:
        table = investment.investment_case(result, layout)
    except ValueError as exc:
        raise ToolError("E_INVESTMENT", str(exc),
                        "check the investment section's baseline and throughput keys") from exc
    rows = _records(table.head(MAX_ROWS), {
        c: c if c == "Scenario" else (c, bool if c == "Pays Back" else _finite)
        for c in investment.COLUMNS})
    return {"model": model, "settings": investment.investment_settings(layout), "table": rows}


_document_args("run_to_precision", model="Workspace name of the model to run.",
               kpi="The KPI key to target, e.g. \"system/Avg Throughput Time\".",
               relative="Target half-width as a fraction of the mean (default 0.05 = 5%).",
               absolute="Target half-width in the KPI's own units; overrides `relative`.",
               max_replications="Hard cap on replications.",
               run_length="Run length in base time units.",
               warmup="Warm-up time.",
               seed="Base seed.",
               max_seconds="Wall-clock budget.")


@tool()
def run_to_precision(model: str, kpi: str, relative: float = 0.05,
                     absolute: float | None = None, max_replications: int = 200,
                     run_length: float | None = None, warmup: float | None = None,
                     seed: int = 0, max_seconds: float | None = None) -> dict:
    """Keep adding replications until one KPI's confidence interval is narrow
    enough, then report how many it took.

    Use it instead of guessing a replication count: "how many runs do I need to
    call this number to within 5%". If `met` comes back false, the cap or the
    time budget was hit first and the reported half-width is what you actually
    have.

    Returns {"model", "kpi", "replications", "mean", "half_width", "target",
    "met"}.
    """
    ws = workspace()
    doc = ws.document(model)
    results, report = experiments.run_until_precision(
        doc.to_layout(), kpi, relative=relative, absolute=absolute,
        max_replications=max_replications, run_length=run_length, warmup=warmup, seed=seed,
        should_stop=_budget(max_seconds))
    if results is not None:
        ws.results[model] = results
    return {"model": model, **{k: (_finite(v) if isinstance(v, float) else v)
                               for k, v in report.items()}}


_document_args("run_reality_checks", model="Workspace name of the model to run.",
               run_length="Run length in base time units; defaults to the model's own setting.",
               warmup="Warm-up time before statistics start; defaults to the model's own setting.",
               seed="Seed shared by every check's deterministic run (CRN across checks).",
               max_seconds="Wall-clock budget for the whole set of checks.")


@tool()
def run_reality_checks(model: str, run_length: float | None = None, warmup: float | None = None,
                       seed: int = 0, max_seconds: float | None = None) -> dict:
    """Run the model's Vensim-style reality checks: an input imposed on the
    model (e.g. "workforce = 0") paired with a constraint expected to hold
    given it (e.g. "then production = 0"). Each check is one deterministic
    replication; a check with no violation passes.

    Returns {"model", "checks": [{"check", "passed", "violation_time",
    "snapshot"}]} -- "violation_time" and "snapshot" (the LEVEL/AUX/FLOW values
    at that instant) are null for a check that passed.
    """
    doc = workspace().document(model)
    layout = _runnable_layout(doc, model)
    try:
        table = modelcheck.run_reality_checks(layout, seed=seed, run_length=run_length,
                                                 warmup=warmup, should_stop=_budget(max_seconds))
    except ValueError as exc:
        raise ToolError("E_REALITY_CHECK", str(exc),
                        "fix the reality_checks section, or the model it overrides into") from None
    checks = [{"check": r["Check"], "passed": bool(r["Passed"]),
              "violation_time": _finite(r["Violation time"]),
              "snapshot": ({k: _finite(v) for k, v in r["Snapshot"].items()}
                          if r["Snapshot"] is not None else None)}
             for r in table.to_dict(orient="records")]
    return {"model": model, "checks": checks}


_document_args("run_loop_dominance", model="Workspace name of an SD model (needs simulation.sd).",
               run_length="Run length in base time units; defaults to the model's own setting.",
               seed="Seed of the one replication the analysis runs.",
               top="How many loops to return, strongest mean relative score first.")
# Dominant intervals a loop may list; an oscillation would otherwise list hundreds.
_MAX_INTERVALS = 20


@tool()
def run_loop_dominance(model: str, run_length: float | None = None, seed: int = 0,
                       top: int = 10) -> dict:
    """Which feedback loop drives an SD model's behaviour, and when (Loops That
    Matter, as in Stella). Runs the model once on its SD grid and scores every
    loop at every step: a loop's relative score is its share of the change in
    its stocks, signed (+ reinforcing, - balancing); the dominant loops at a
    step are the fewest that explain at least half of it.

    Returns {"model", "step", "truncated", "loop_count", "loops": [{"loop",
    "kind" (R/B from the run, U if its sign flips, D if never active),
    "partition", "length", "nodes", "total" (mean relative score), "dominant":
    [[start, end], ...] (at most 20 intervals)}], "warnings"} -- the *top*
    loops by |total|. Refused with E_LOOP_GRID without simulation.sd.
    """
    doc = workspace().document(model)
    layout = _runnable_layout(doc, model)
    try:
        result = loopdominance.analyse(layout, seed=seed, run_length=run_length)
    except loopdominance.LoopDominanceError as exc:
        raise ToolError(exc.problem.code, exc.problem.message, exc.problem.hint) from None
    loops, steps = result["loops"], result["loop_dominance"]
    ranked = loops.reindex(loops["Total"].abs().sort_values(ascending=False, kind="stable").index)
    out = []
    for row in ranked.head(max(1, min(int(top), MAX_ROWS))).to_dict(orient="records"):
        own = steps[steps["Loop"] == row["Loop"]]
        flags, times = own["Dominant"].to_numpy(), own["Time"].to_numpy()
        starts = np.flatnonzero(flags & ~np.r_[False, flags[:-1]])
        ends = np.flatnonzero(flags & ~np.r_[flags[1:], False])
        out.append({"loop": row["Loop"], "kind": row["Kind"], "partition": int(row["Partition"]),
                    "length": int(row["Length"]), "nodes": list(row["Nodes"]),
                    "total": _finite(row["Total"]),
                    "dominant": [[float(times[a]), float(times[b])]
                                 for a, b in zip(starts, ends)][:_MAX_INTERVALS]})
    return {"model": model, "step": result["step"], "truncated": result["truncated"],
            "loop_count": len(loops), "loops": out,
            "warnings": [p.as_dict() for p in result["problems"]]}


_document_args("suggest_warmup", model="Workspace name of the model.",
               replications="Replications to run for the estimate.",
               run_length="Run length in base time units.",
               max_seconds="Wall-clock budget.")


@tool()
def suggest_warmup(model: str, replications: int = 5, run_length: float | None = None,
                   max_seconds: float | None = None) -> dict:
    """Estimate how much of the run to discard as warm-up, using MSER-5 on the
    throughput-time series.

    A steady-state model starts empty, which biases early observations low; the
    warm-up is the time to reset statistics at. If `reliable` is false the run
    was too short to tell -- lengthen it rather than trusting the number.

    Returns {"model", "warmup", "reliable", "observations"}.
    """
    doc = workspace().document(model)
    results = api.run_model(doc.to_layout(), replications=replications, run_length=run_length,
                            warmup=0.0, should_stop=_budget(max_seconds))
    if results is None:
        raise ToolError("E_NO_RESULTS", "the warm-up probe produced no completed replication",
                        "raise max_seconds or run_length")
    suggestion = experiments.suggest_warmup(results)
    return {"model": model, "warmup": _finite(suggestion.warmup),
            "reliable": bool(suggestion.reliable), "observations": int(suggestion.index)}


_document_args("optimize", model="Workspace name of the model.",
               factors="[{\"path\": \"resources.Server.capacity\", \"kind\": \"integer\", "
                       "\"low\": 1, \"high\": 5}], kind is continuous/integer/categorical.",
               response="KPI key to optimise, e.g. \"system/Avg Throughput Time\".",
               goal="\"min\" or \"max\".",
               constraints="[[\"<kpi key>\", \"<=\", value], ...] KPI constraints.",
               budget="Total replications the search may spend.",
               replications_per_point="Replications per candidate point.",
               run_length="Run length in base time units.",
               seed="Base seed.",
               max_seconds="Wall-clock budget.")


@tool()
def optimize(model: str, factors: list, response: str, goal: str = "min",
             constraints: list | None = None, budget: int = 120,
             replications_per_point: int = 5, run_length: float | None = None,
             seed: int = 0, max_seconds: float | None = None) -> dict:
    """Search model parameters for the best value of one KPI within a
    replication budget, then confirm the winner statistically.

    Every factor is an override path plus a range, exactly like a scenario
    override but varying. The budget is a HARD cap on replications, so start
    small (a few hundred) and raise it if `converged` is false. Constraints are
    judged on confidence intervals, so a point may come back "unknown" rather
    than feasible or infeasible.

    Returns {"model", "best", "best_mean", "converged", "budget_used",
    "feasible", "history": [{"point", "mean", "feasibility"}] (last 20)}.
    """
    doc = workspace().document(model)
    try:
        specs = [optimize_mod.Factor(**f) for f in factors]
    except TypeError as exc:
        raise ToolError("E_BAD_FACTOR", str(exc),
                        "a factor is {\"path\", \"kind\", \"low\", \"high\"} or "
                        "{\"path\", \"kind\": \"categorical\", \"levels\": [...]}") from None
    result = optimize_mod.optimize(doc.to_layout(), specs, response=response, goal=goal,
                                   constraints=[tuple(c) for c in (constraints or ())],
                                   budget=int(budget),
                                   replications_per_point=int(replications_per_point),
                                   run_length=run_length, seed=seed,
                                   should_stop=_budget(max_seconds))
    history = _records(result.history.tail(20),
                       {"point": "Point", "mean": ("Mean", _finite), "feasibility": "Feasibility"})
    return {"model": model, "best": result.best, "best_mean": _finite(result.best_mean),
            "converged": bool(result.converged), "budget_used": int(result.budget_used),
            "response": result.response, "goal": result.goal, "feasible": bool(result.feasible),
            "history": history}


_document_args("calibrate", model="Workspace name of the model to calibrate.",
               targets="[{\"kpi\", \"observed\", \"weight\"?, \"tolerance\"?}] -- the measured "
                       "values the model must reproduce.",
               factors="[{\"path\", \"kind\", \"low\", \"high\"}] -- the parameters to tune.",
               budget="Total replications the search may spend.",
               replications_per_point="Replications per candidate point.",
               seed="Base seed.",
               apply="Store the fitted values as a \"Calibrated\" scenario on the model.",
               max_seconds="Wall-clock budget.")


@tool()
def calibrate(model: str, targets: list, factors: list, budget: int = 120,
              replications_per_point: int = 5, seed: int = 0, apply: bool = False,
              max_seconds: float | None = None) -> dict:
    """Tune model parameters until the model reproduces measured KPIs.

    Give what the real system did (`targets`) and which parameters may move
    (`factors`, the same shape `optimize` takes). The objective is the weighted
    sum of squared RELATIVE errors, so KPIs on different scales count equally.
    A target with a `tolerance` gets a pass/fail verdict; read the per-target
    confidence interval too -- a pass whose interval is far wider than the
    tolerance has not really been demonstrated.

    Returns {"model", "best", "objective", "passed", "budget_used", "converged",
    "overrides", "targets": [{"kpi", "observed", "simulated", "lb", "ub",
    "error", "tolerance", "pass"}]}.
    """
    doc, before = _open(model)
    from nagasim import calibrate as calibrate_mod

    try:
        specs = [optimize_mod.Factor(**f) for f in factors]
    except TypeError as exc:
        raise ToolError("E_BAD_FACTOR", str(exc),
                        "a factor is {\"path\", \"kind\", \"low\", \"high\"}") from None
    result = calibrate_mod.calibrate(doc.to_layout(), targets, specs, budget=int(budget),
                                     replications_per_point=int(replications_per_point),
                                     seed=seed, should_stop=_budget(max_seconds))
    rows = _records(result.table, {
        "kpi": "KPI", "observed": ("Observed", _finite), "simulated": ("Simulated", _finite),
        "lb": ("LB", _finite), "ub": ("UB", _finite), "error": ("Error", _finite),
        "tolerance": ("Tolerance", _finite),
        "pass": ("Pass", lambda v: None if v is None else bool(v)),
    })
    out = {"model": model, "best": result.best, "objective": _finite(result.objective),
           "passed": bool(result.passed), "budget_used": int(result.budget_used),
           "converged": bool(result.converged), "overrides": list(result.overrides),
           "targets": rows}
    if not apply:
        return out
    doc.set_scenarios(_upsert(doc.scenarios, result.as_scenario()))
    return _edit_result(doc, before, out)


_document_args("forecast", model="Workspace name of the model to forecast.",
               horizon="Run length in base time units -- a shift, a day.",
               replications="Replications; a prediction band needs a few hundred to be stable.",
               events="[{\"name\", \"kpi\", \"op\": \">\"|\"<\", \"value\"}] questions to answer "
                      "as probabilities.",
               kpis="KPI keys to report; omit for the curated system ones.",
               bind_data="Resolve the model's data bindings first (default true).",
               seed="Base seed.",
               max_seconds="Wall-clock budget.")


@tool()
def forecast(model: str, horizon: float, replications: int = 100, events: list | None = None,
             kpis: list | None = None, bind_data: bool = True, seed: int = 0,
             max_seconds: float | None = None) -> dict:
    """Forecast a short horizon from the model's CURRENT state, with
    uncertainty bands and event probabilities.

    Starts from the model's data bindings and initial state, runs a terminating
    horizon with no warm-up, and answers "where is this heading". The band is
    the empirical 5-95% range across replications -- where a single future run
    is likely to land -- NOT a confidence interval of the mean, which is the
    narrower `lb`/`ub` pair beside it.

    An event's `kpi` may be any run KPI or a time-series peak,
    `timeseries/<series>/max`, which is how you ask "was the queue EVER above
    8" rather than "was it on average".

    Returns {"model", "horizon", "replications", "kpis": [{"kpi", "mean", "lb",
    "ub", "q05", "q95", "min", "max"}], "events": [{"event", "kpi", "condition",
    "probability", "wilson_lb", "wilson_ub"}], "series": [names]}.
    """
    from nagasim import forecast as forecast_mod

    ws = workspace()
    doc = ws.document(model)
    base_dir = doc.path.parent if doc.path is not None else ws.root
    result = forecast_mod.forecast(doc.to_layout(), horizon=float(horizon),
                                   replications=int(replications), kpis=kpis,
                                   events=events or (), bind_data=bind_data, base_dir=base_dir,
                                   network=ws.network, confine_to=ws.root,
                                   seed=seed, should_stop=_budget(max_seconds))
    ws.results[model] = result.results
    table = result.kpis
    if kpis is None:
        table = table[table["KPI"].str.startswith(("system/", "timeseries/system/"))]
    return {
        "model": model, "horizon": result.horizon, "replications": result.replications,
        "kpis": _records(table.head(MAX_ROWS), {
            "kpi": "KPI", "mean": ("Mean", _finite), "lb": ("LB", _finite),
            "ub": ("UB", _finite), "q05": ("Q05", _finite), "q95": ("Q95", _finite),
            "min": ("Min", _finite), "max": ("Max", _finite),
        }),
        "events": _records(result.events, {
            "event": "Event", "kpi": "KPI", "condition": "Condition",
            "probability": ("Probability", _finite), "wilson_lb": ("Wilson LB", _finite),
            "wilson_ub": ("Wilson UB", _finite),
        }),
        "series": result.series_names[:MAX_ROWS],
    }


_document_args("export", model="Workspace name of a model that has been run.",
               format="Exporter name: xlsx, csv, html, json, or a plugin's own.",
               path="Destination under the workspace root (a directory for csv).")


@tool()
def export(model: str, format: str, path: str) -> dict:
    """Write the last run's results to a file: an Excel workbook, CSV folder,
    a self-contained HTML report, or JSON.

    Use `html` for the client-facing deliverable (it embeds the charts) and
    `json` when another program will read the numbers. Run the model first.

    Returns {"ok", "format", "path"}.
    """
    exporter = EXPORTER_REGISTRY.get(format)
    # D201: a format drawn from a run's recorded trace is not this tool's to write: it
    # keeps the results of a run, never its trace.
    offered = sorted(name for name, entry in EXPORTER_REGISTRY.items() if not entry.needs_trace)
    if exporter is None or exporter.needs_trace:
        reason = (f"no exporter named '{format}'" if exporter is None else
                  f"'{format}' is drawn from a run's recorded trace, which this tool does not keep")
        raise ToolError("E_UNKNOWN_FORMAT", reason, f"use one of: {', '.join(offered)}")
    ws = workspace()
    doc = ws.document(model)
    results = _results_of(model)
    target = ws.resolve(path)
    exporter.write(results, str(target), layout=doc.to_layout(), title=model)
    return {"ok": True, "format": format, "path": str(target)}


_document_args("evaluate_policy", model="Workspace name of the model.",
               policy="Decision-point name -- must match a Policy-mode DECIDE's `policy` param.",
               controller="Built-in controller to evaluate: \"random\", \"shortest\" or \"roundrobin\".",
               episodes="Independent episodes to average over; episode i uses seed + i.",
               horizon="Episode length in base time units; the episode truncates there if the "
                       "model has not drained first.",
               reward="\"neg_wip\", \"throughput\", \"neg_time_in_system\", or an expression string "
                      "evaluated over model state at each decision (no `entity` in scope).",
               seed="Base seed.")


@tool()
def evaluate_policy(model: str, policy: str, controller: str, episodes: int = 20,
                    horizon: float = 500.0, reward: str = "neg_wip", seed: int = 0) -> dict:
    """Evaluate a built-in controller against a Policy-mode DECIDE.

    Runs `episodes` episodes of `nagasim.rl.SimEnv`, driving every pending
    decision at the named `policy` with `controller`. Use this to compare
    "shortest" against "random" (or "roundrobin") before ever training
    anything, or to sanity-check a model's Policy-mode DECIDE wiring.

    Returns {"model", "policy", "controller", "reward", "mean", "lb", "ub",
    "episodes", "returns": [per-episode return] (capped)}.
    """
    from nagasim import rl

    doc = workspace().document(model)
    result = rl.evaluate_policy(doc.to_layout(), controller, int(episodes), int(seed),
                                policy=policy, reward=reward, horizon=float(horizon))
    return {"model": model, "policy": policy, "controller": controller, "reward": reward,
            "mean": _finite(result["mean"]), "lb": _finite(result["lb"]),
            "ub": _finite(result["ub"]), "episodes": result["episodes"],
            "returns": [_finite(v) for v in result["returns"][:MAX_ROWS]]}


_document_args("fit_surrogate", model="Workspace name of the model.",
               factors="[{\"path\": \"resources.Server.capacity\", \"kind\": \"integer\", "
                       "\"low\": 1, \"high\": 5}], same shape as the `optimize` tool's factors.",
               response="KPI key to fit a surface for, e.g. \"system/Avg Throughput Time\".",
               design="full[:levels] (default full:2), lhs:<n>, or oat -- same grammar as "
                      "`naga doe --design`.",
               kind="\"linear\", \"quadratic\", \"auto\" (picks by CV RMSE), or -- only if "
                    "scikit-learn is installed -- \"gp\"/\"forest\".",
               replications="Replications per design point.",
               run_length="Run length in base time units.",
               seed="Base seed.",
               max_seconds="Wall-clock budget for the whole design.")


@tool()
def fit_surrogate(model: str, factors: list, response: str, design: str = "full:2",
                  kind: str = "quadratic", replications: int = 5, run_length: float | None = None,
                  seed: int = 0, max_seconds: float | None = None) -> dict:
    """Run a design of experiments and fit a response-surface surrogate over it
    -- a fast stand-in for the simulation, for optimisation or
    what-if exploration without spending more replications.

    Every factor is swept per `design` and simulated `replications` times; the
    surrogate is then fit on the point means, weighted by their precision.
    Check `lack_of_fit` before trusting `suggest`-style extrapolation: it means
    the model misses structure the replication noise does not explain, so
    "quadratic" or "auto" (or "gp"/"forest" with scikit-learn) may do better
    than "linear".

    Returns {"model", "response", "kind", "n_points", "r2", "r2_adj",
    "cv_rmse", "lack_of_fit", "coefficients": [{"term", "coefficient",
    "std_error"}] (empty for gp/forest)}.
    """
    from nagasim import surrogate as surrogate_mod
    from nagasim.optimize import build_design

    doc = workspace().document(model)
    try:
        specs = [optimize_mod.Factor(**f) for f in factors]
    except TypeError as exc:
        raise ToolError("E_BAD_FACTOR", str(exc),
                        "a factor is {\"path\", \"kind\", \"low\", \"high\"} or "
                        "{\"path\", \"kind\": \"categorical\", \"levels\": [...]}") from None
    design_frame = build_design(design, specs, seed)
    design_result = optimize_mod.run_design(doc.to_layout(), specs, design_frame,
                                            responses=[response], replications=int(replications),
                                            seed=int(seed), run_length=run_length,
                                            should_stop=_budget(max_seconds))
    model_fit = surrogate_mod.fit_surrogate(design_result, response, kind=kind)
    coefficients = [{"term": t, "coefficient": _finite(c), "std_error": _finite(s)}
                    for t, c, s in zip(model_fit.terms, model_fit.coef, model_fit.se)]
    return {"model": model, "response": response, "kind": model_fit.kind,
            "n_points": len(design_result.design), "r2": _finite(model_fit.r2),
            "r2_adj": _finite(model_fit.r2_adj), "cv_rmse": _finite(model_fit.cv_rmse),
            "lack_of_fit": bool(model_fit.lack_of_fit), "coefficients": coefficients}
