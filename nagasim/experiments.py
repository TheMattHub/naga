"""Scenario manager, precision helper and warm-up helper (ROADMAP 1.6).

Top-level module beside `api.py`, deliberately NOT in `nagasim/core/`: it needs
pandas and it sits above the engine. Dependencies point downward only --
experiments -> api -> core, never the other way.

    from nagasim import experiments
    result = experiments.run_scenarios(layout, replications=20, workers=4)
    table = result.comparison()        # KPI x scenario, with paired-t CIs

A scenario is stored in the model file (schema v9):

    {"name": "One more agent", "description": "...",
     "overrides": [{"path": "resources.Agents.capacity", "value": 6}]}

The implicit scenario "Baseline" (no overrides) always exists.
"""
from __future__ import annotations

import argparse
import copy
import difflib
from contextlib import nullcontext
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from nagasim import api
from nagasim._cli import add_model_argument, setup_cli
from nagasim.core import schema
from nagasim.core.export import PROBLEMS_KEY, concat_runs
from nagasim.core.outputs import LOG_DERIVED_COLUMNS
from nagasim.core.schema import ModelValidationError
from nagasim.core.stats import ci95, paired_diff_ci, welch_diff_ci
from nagasim.results import Results
from nagasim.runchecks import RunChecks

__all__ = ["BASELINE", "apply_overrides", "value_at", "list_override_paths", "override_targets",
           "scenarios_of", "flatten_kpis", "needs_event_logs", "KPI_PREFIXES", "ExperimentResult",
           "run_scenarios", "what_if", "paired_until_clear", "statistic", "bootstrap_ci",
           "size_capacity", "SizingResult", "SIZING_COLS",
           "record_scenarios",
           "run_until_precision", "suggest_warmup", "WarmupSuggestion", "COMPARISON_COLS", "main"]

BASELINE = "Baseline"

# The override path grammar lives in core (D1222); these names are its public face here.
from nagasim.core.overrides import (_ID_SECTIONS, _NAMED_SECTIONS, _SECTIONS,  # noqa: E402,F401
                                    apply_overrides, list_override_paths, override_targets,
                                    value_at)


def scenarios_of(layout: dict) -> list[dict]:
    """The model's scenarios with the implicit "Baseline" first (unless the
    model declares one of its own by that name)."""
    stored = [copy.deepcopy(s) for s in layout.get("scenarios", [])]
    if not any(s.get("name") == BASELINE for s in stored):
        stored.insert(0, {"name": BASELINE, "overrides": []})
    return stored


# ---------------------------------------------------------------- KPI flattening
_OBJECT_FRAMES = (("resource_summary", "Resource", "resource"),
                  ("store_summary", "Store", "store"),
                  ("entity_summary", "Entity Type", "entity"),
                  # PHASE4 A3. Both are empty for a model with no orders and no
                  # due dates, so they add no KPI key to an existing model --
                  # and give `order/<id>/Tardiness` and
                  # `duedate/(all)/Service Level` to one that has them, which
                  # is what the paired-t scenario comparison then reads.
                  ("order_summary", "Order", "order"),
                  ("due_date_summary", "Entity Type", "duedate"),
                  # PHASE4 A4, same deal: `queue/<block>/Reneged` and friends
                  # for a model that declares a queue behaviour, nothing for
                  # one that does not.
                  ("queue_summary", "Block", "queue"),
                  # D156: `motion/<block>/<kind>/Mean Time` and friends ("cycle"
                  # for the whole cycle) -- nothing for a model without MOTION.
                  ("motion_summary", "Motion", "motion"),
                  # PHASE4 D1: `level/<name>/Average` and `flow/<name>/Total`
                  # for a model with continuous quantities, nothing for one
                  # without -- so the optimizer and the scenario comparison
                  # address them like any other KPI.
                  ("level_summary", "Level", "level"),
                  ("flow_summary", "Flow", "flow"),
                  # Phase 15 M8 (D850): `dispatch/<name>/Realised Cost` and friends --
                  # nothing for a model without a `dispatch` section.
                  ("dispatch_summary", "Dispatch", "dispatch"),
                  # Bio-1: `conversion/<name>/<substrate>/Produced` and
                  # `component/<level>/<component>/Avg Fraction` -- the id
                  # column already carries both halves of the key.
                  ("conversion_summary", "Conversion", "conversion"),
                  ("component_summary", "Component", "component"),
                  # PHASE4 D5: `cost/<item>/Amount`, so the optimizer can trade
                  # a capex against operating cost without a pseudo-flow.
                  ("cost_summary", "Item", "cost"),
                  # PHASE4 E1: `fleet/<name>/Distance Empty`,
                  # `segment/<a<->b>/Traversals` and `conveyor/<block>/Blocked %`
                  # -- nothing at all for a model with no network.
                  ("fleet_summary", "Fleet", "fleet"),
                  ("segment_summary", "Segment", "segment"),
                  ("conveyor_summary", "Conveyor", "conveyor"),
                  # D795: `stop/<belt>/<index>/Avg Wait`, `.../Parts`, `.../Busy %` --
                  # the frame exists only for a model with belt stops.
                  ("conveyor_stops", ("Conveyor", "Stop"), "stop"),
                  # D855: `divert/<belt>/<index>/Diverted` and friends, the same rule.
                  ("conveyor_diverts", ("Conveyor", "Divert"), "divert"),
                  ("conveyor_merges", ("Conveyor", "Merge"), "merge"),
                  # D1347: `eye/<belt>/<index>/Fired`, the same rule.
                  ("conveyor_eyes", ("Conveyor", "Eye"), "eye"),
                  # A12 (D1373-D1376): `conveyor/<belt>/Dieback Length` (beside the summary's
                  # columns, none shared), `zone/<belt>/<zone>/Dieback %`,
                  # `transfer/<module>/Busy %`, `route/<block>/<point>/Hold %` and
                  # `link/<source> -> <target>/Avg Occupancy`: only for a model with the feature.
                  ("conveyor_flow", "Conveyor", "conveyor"),
                  ("conveyor_zones", ("Conveyor", "Zone"), "zone"),
                  ("transfer_summary", "Transfer", "transfer"),
                  ("route_holds", ("Block", "Point"), "route"),
                  ("link_flow", "Link", "link"),
                  # D164-D165: `charger/<name>/Grid kWh` and
                  # `battery/<fleet>/Charging %` -- nothing without batteries.
                  ("charging_summary", "Charger", "charger"),
                  ("battery_summary", "Fleet", "battery"),
                  # D169: `tending/<station>/<fleet>/Avg Loaded Wait` -- two id
                  # columns, joined; nothing for a model no cobot tends in.
                  ("tending_summary", ("Station", "Fleet"), "tending"),
                  # PHASE4 T5: `buffer/<block>/Blocked %` and `.../Avg Content`,
                  # so a buffer-allocation study can optimize on them directly.
                  ("buffer_summary", "Block", "buffer"),
                  # PHASE6 W3 M1 (D112): `energy/<resource>/Total kWh` and
                  # `.../Energy Cost` -- nothing for a model with no "power".
                  ("energy_summary", "Resource", "energy"),
                  # PHASE6 W3 M2 (D113): `bottleneck/<resource>/Sole %` and
                  # friends -- nothing unless the model sets
                  # `outputs.bottleneck`. "material_flow_summary" is NOT here:
                  # its natural key is the (From, To) pair, not one id column,
                  # and a per-link entity count is not the kind of thing a
                  # scenario comparison or the optimizer addresses by name.
                  ("bottleneck_summary", "Resource", "bottleneck"),
                  # PHASE5 AB-1: `agent/<population>/<state>/Avg Count` and
                  # friends -- nothing for a model without populations.
                  ("agent_summary", "Agent State", "agent"),
                  # PHASE6 C2: `agent/<population>/<attribute>/<state>/End Mean`
                  # and friends ("All" for the population-wide row) -- nothing
                  # for a model whose agents declare no attributes.
                  ("agent_attributes", "Agent Attribute", "agent"),
                  # E7 (D1126): `slot/<block>/Overflows` and `.../Entered` -- nothing
                  # unless a block's queue_row gives slots.
                  ("slot_summary", "Block", "slot"))
# The first part of every `flatten_kpis` key.
KPI_PREFIXES = ("system", "user", "plugin",
                *dict.fromkeys(prefix for _f, _c, prefix in _OBJECT_FRAMES))


def flatten_kpis(results: dict) -> pd.DataFrame:
    """Flatten the scalar KPI frames of a results dict into one long table.

    Takes the system, resource, store, entity, user and cost frames, everything
    but ``"timeseries"`` (a curve rather than one number), numbers only. The
    keys are stable, so a scenario comparison or a precision target can name a
    KPI without knowing which frame it came from::

        system/<Metric>              system/Avg Throughput Time
        resource/<name>/<column>     resource/Agents/Usage % (snapshot)
        store/<name>/<column>
        entity/<type>/<column>
        user/<statistic>             the RECORD statistic's own Value
        plugin/<name>                a plugin's ctx.record_kpi value
        slot/<block>/<column>        a queue row's slots, e.g. slot/Bay/Overflows
        cost/<item>/Amount           a fixed, per-event or peak cost
        stop/<belt>/<index>/<column> a conveyor stop, e.g. stop/Line/0/Avg Wait
        divert/<belt>/<index>/<column> a conveyor divert, e.g. divert/Line/0/Diverted
        merge/<belt>/<index>/<column>  a conveyor merge, e.g. merge/Line/0/Merged

    Parameters
    ----------
    results : dict
        The result of `nagasim.api.run_model`.

    Returns
    -------
    pandas.DataFrame
        Columns ``Run``, ``KPI`` and ``Value``.

    See Also
    --------
    nagasim.api.run_model : Produces the results.
    nagasim.experiments.run_until_precision : Takes one of these keys as its target.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import api, experiments
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> results = api.run_model(str(path), replications=1, run_length=200, warmup=20, seed=0)
    >>> flat = experiments.flatten_kpis(results)
    >>> list(flat.columns)
    ['Run', 'KPI', 'Value']
    >>> "system/Total Entities Completed" in set(flat["KPI"])
    True
    """
    rows: list[tuple] = []

    sys_df = results.get("system")
    if sys_df is not None and not sys_df.empty:
        values = pd.to_numeric(sys_df["Value"], errors="coerce")
        for run, metric, value in zip(sys_df["Run"], sys_df["Metric"], values):
            if metric != "Run" and pd.notna(value):
                rows.append((run, f"system/{metric}", float(value)))

    for key, obj_col, prefix in _OBJECT_FRAMES:
        df = results.get(key)
        if df is None or df.empty:
            continue
        objects = (df[obj_col] if isinstance(obj_col, str)
                   else df[list(obj_col)].astype(str).agg("/".join, axis=1))
        for column in df.select_dtypes("number").columns:
            # An id column is part of the key, not a KPI (a stop's numeric index, D795).
            if column == "Run" or column in ((obj_col,) if isinstance(obj_col, str) else obj_col):
                continue
            for run, obj, value in zip(df["Run"], objects, df[column]):
                if pd.notna(value):
                    rows.append((run, f"{prefix}/{obj}/{column}", float(value)))

    user = results.get("user_summary")
    if user is not None and not user.empty:
        for run, name, value in zip(user["Run"], user["Statistic"],
                                    pd.to_numeric(user["Value"], errors="coerce")):
            if pd.notna(value):
                rows.append((run, f"user/{name}", float(value)))

    # E8 (D1119): what a plugin recorded with `ctx.record_kpi`, so a study can name it.
    plugin = results.get("plugin_kpis")
    if plugin is not None and not plugin.empty:
        for run, name, value in zip(plugin["Run"], plugin["Metric"],
                                    pd.to_numeric(plugin["Value"], errors="coerce")):
            if pd.notna(value):
                rows.append((run, f"plugin/{name}", float(value)))

    return pd.DataFrame(rows, columns=["Run", "KPI", "Value"])


def headline_keys(means: dict) -> list[str]:
    """The KPI keys of the few numbers that say what a run gave, from {key: mean}: the
    system's when entities flowed, the model's own RECORD statistics, its levels' averages
    and its agents' state counts. The docs gallery and the Results panel's tiles share it."""
    picked: list[str] = []
    if means.get("system/Total Entities Completed", 0.0) > 0:
        picked += ["system/Total Entities Completed", "system/Avg Throughput Time"]
        if any(m.startswith("resource/") for m in means):
            picked += ["system/Avg Queue Length", "system/Avg Resource Usage %"]
    picked += [m for m in means if m.startswith("user/")][:3]
    picked += [m for m in means if m.startswith("level/") and m.endswith("/Average")][:3]
    picked += [m for m in means if m.startswith("agent/") and m.endswith("/Avg Count")][:4]
    if means.get("system/Total Cost", 0.0) > 0:
        picked.append("system/Total Cost")
    return [m for m in picked if m in means and means[m] == means[m]]


def needs_event_logs(kpis) -> bool:
    """Whether any of the KPI keys *kpis* is computed from the per-event logs,
    so a study asking for it must not run with `record_logs` off.

    Keys are spelled as `flatten_kpis` writes them, plus the raw log columns a
    calibration target names (`entity_log/Throughput Time`). The column is the
    last `/`-separated part, because an object name may itself contain a `/`.
    """
    frames = {prefix: frame for frame, _col, prefix in _OBJECT_FRAMES}
    for kpi in kpis:
        prefix, _, rest = str(kpi).partition("/")
        if prefix in ("entity_log", "resource_log", "store_log"):
            return True
        if rest.rpartition("/")[2] in LOG_DERIVED_COLUMNS.get(frames.get(prefix), ()):
            return True
    return False


def statistic(name: str):
    """The statistic over replications *name* stands for: "mean", or "p<q>" for a quantile
    ("p95" is the 95th percentile, "p50" the median). A function of a 1-D array."""
    text = str(name).strip().lower()
    if text == "mean":
        return lambda x: float(np.nanmean(x))
    try:
        q = float(text[1:]) / 100.0 if text.startswith("p") else math.nan
    except ValueError:
        q = math.nan
    if not 0.0 < q < 1.0:
        raise ValueError(f"statistic must be 'mean' or a percentile such as 'p95', not {name!r}")
    return lambda x: float(np.nanquantile(x, q))


_statistic = statistic          # for `size_capacity`, whose parameter has the same name


def bootstrap_ci(stat, *samples, confidence: float = 0.95, n_boot: int = 1000,
                 seed: int = 0) -> dict[str, float]:
    """`{"mean", "lb", "ub"}`: *stat* of *samples* and its percentile bootstrap interval.

    Every sample is resampled with the SAME replication indices, so two arms run on common
    random numbers stay paired: `stat=lambda a, b: p95(a) - p95(b)` gives a paired quantile
    delta. A quantile has no t interval, hence the bootstrap; the fixed *seed* keeps a study's
    verdict reproducible."""
    arrays = [np.asarray(s, dtype=float) for s in samples]
    n = min(len(a) for a in arrays)
    arrays = [a[:n] for a in arrays]
    point = float(stat(*arrays))
    if n < 2:
        return {"mean": point, "lb": math.nan, "ub": math.nan}
    rng = np.random.default_rng(seed)
    draws = np.array([stat(*(a[idx] for a in arrays))
                      for idx in rng.integers(0, n, size=(int(n_boot), n))], dtype=float)
    tail = (1.0 - float(confidence)) / 2.0
    lb, ub = np.nanquantile(draws, [tail, 1.0 - tail])
    return {"mean": point, "lb": float(lb), "ub": float(ub)}


def _series(flat: pd.DataFrame, kpi: str) -> np.ndarray:
    """The KPI's per-replication values ordered by Run, so two scenarios run on
    the same seeds line up element by element for the paired interval."""
    sub = flat[flat["KPI"] == kpi].sort_values("Run")
    return sub["Value"].to_numpy(dtype=float)


# ---------------------------------------------------------------- experiment run
COMPARISON_COLS = ["KPI", "Scenario", "Mean", "LB", "UB",
                   "Diff vs baseline", "Diff LB", "Diff UB", "Significant",
                   "Replications Needed", "CRN Ratio"]
# A study that needs more replications than this to separate two scenarios is
# reported as "this many or more", not as an exact (and absurd) count.
MAX_REPLICATIONS_NEEDED = 1000


@dataclass
class ExperimentResult:
    """What `run_scenarios` returns: the eight frames per scenario, plus the
    seeds each one used and whether every scenario finished."""

    results: dict[str, dict] = field(default_factory=dict)
    seeds: dict[str, list[int]] = field(default_factory=dict)
    warmups: dict[str, float] = field(default_factory=dict)   # the warm-up each scenario ran with (#1760)
    crn: bool = True
    completed: bool = True

    @property
    def scenarios(self) -> list[str]:
        return list(self.results)

    _REPR_ROWS = 8      # KPI rows of the short repr: a study has dozens of KPIs and eight frames per scenario

    def _summary(self) -> tuple[str, pd.DataFrame, int]:
        """(header, the first KPI rows of `comparison()`, rows left out) for `repr` and the
        notebook table (#1898): each non-baseline scenario against the baseline, the system KPIs
        first. A hand-built study with no baseline compares against its first scenario."""
        names = self.scenarios
        base = BASELINE if BASELINE in self.results else (names[0] if names else None)
        reps = len(self.seeds.get(base) or [])
        header = (f"ExperimentResult({len(names)} scenario{'s' if len(names) != 1 else ''}: "
                  f"{', '.join(names)}; {reps} replication{'s' if reps != 1 else ''}; "
                  f"{'common random numbers' if self.crn else 'independent runs'}"
                  + ("" if self.completed else "; stopped early") + ")")
        if base is None or len(names) < 2:
            return header, pd.DataFrame(), 0
        table = self.comparison(baseline=base)
        table = table[table["Scenario"] != base]
        system = table["KPI"].str.startswith("system/")
        table = table[system] if system.any() else table
        columns = ["KPI", "Scenario", "Mean", "Diff vs baseline", "Diff LB", "Diff UB", "Significant"]
        return header, table[columns].head(self._REPR_ROWS), max(0, len(table) - self._REPR_ROWS)

    def __repr__(self) -> str:
        """The scenarios and the first KPI differences, not the eight frames of every scenario
        (19 kB for two); `study.results`, `study.comparison()` and `study.kpis()` are unchanged."""
        header, shown, more = self._summary()
        if shown.empty:
            return header
        return (header + "\n" + shown.to_string(index=False, float_format=lambda v: f"{v:.4g}")
                + (f"\n... {more} more rows: study.comparison()" if more else ""))

    def _repr_html_(self) -> str:
        """Jupyter display: the same header and table as `repr`."""
        import html
        header, shown, more = self._summary()
        table = "" if shown.empty else shown.to_html(index=False, float_format=lambda v: f"{v:,.4g}",
                                                     na_rep="-")
        return (f"<p>{html.escape(header)}</p>\n{table}"
                + (f"\n<p>... {more} more rows: study.comparison()</p>" if more else ""))

    def checks(self) -> dict[str, RunChecks]:
        """{scenario: RunChecks}: each scenario's law checks, the same object a single run's
        `Results.checks()` gives (#1880, #1886), computed on first ask. Results put here by hand
        (a plain dict of frames) have no layout to judge: an empty `RunChecks` with a note."""
        return {name: res.checks() if isinstance(res, Results) else RunChecks((), note="no layout")
                for name, res in self.results.items()}

    def kpis(self) -> pd.DataFrame:
        """Every scenario's flattened KPIs in one frame, with a Scenario column."""
        frames = [flatten_kpis(res).assign(Scenario=name) for name, res in self.results.items()]
        return (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame(columns=["Run", "KPI", "Value", "Scenario"]))

    def comparison(self, baseline: str = BASELINE, kpis=None,
                   confidence: float = 0.95) -> pd.DataFrame:
        """One row per (KPI, scenario): the scenario's own mean with a Student-t
        CI, and the difference against *baseline* with its own CI.

        With common random numbers the difference interval is the PAIRED-t one
        on the per-replication differences (the shared noise cancels); without
        them the two samples are independent and it is Welch's. "Significant"
        is simply "the difference interval excludes 0", so the baseline's own
        row -- difference exactly 0 -- is never significant.
        """
        flat = {name: flatten_kpis(res) for name, res in self.results.items()}
        if baseline not in flat:
            raise ValueError(f"unknown baseline scenario: {baseline!r}")
        base_flat = flat[baseline]
        wanted = set(kpis) if kpis is not None else None

        names = sorted({k for frame in flat.values() for k in frame["KPI"].unique()})
        if wanted is not None:
            names = [k for k in names if k in wanted]

        diff_ci = paired_diff_ci if self.crn else welch_diff_ci
        rows = []
        for kpi in names:
            base_values = _series(base_flat, kpi)
            for scenario, frame in flat.items():
                values = _series(frame, kpi)
                own = ci95(values, confidence)
                diff = diff_ci(values, base_values, confidence)
                significant = bool(np.isfinite(diff["lb"]) and np.isfinite(diff["ub"])
                                   and (diff["lb"] > 0 or diff["ub"] < 0))
                needed = ratio = math.nan
                if scenario != baseline and np.isfinite(diff["lb"]) and np.isfinite(diff["ub"]):
                    half = (diff["ub"] - diff["lb"]) / 2
                    if not significant:
                        # n x (half-width / |diff|)^2: the half-width shrinks as 1/sqrt(n)
                        # (an estimate: the variance is the one seen so far).
                        needed = (MAX_REPLICATIONS_NEEDED if diff["mean"] == 0 else
                                  min(MAX_REPLICATIONS_NEEDED,
                                      math.ceil(len(values) * (half / abs(diff["mean"])) ** 2)))
                    if self.crn and half > 0:
                        # Welch (what independent runs would have given) over paired.
                        welch = welch_diff_ci(values, base_values, confidence)
                        ratio = (welch["ub"] - welch["lb"]) / 2 / half
                rows.append({"KPI": kpi, "Scenario": scenario, "Mean": own["mean"],
                             "LB": own["lb"], "UB": own["ub"],
                             "Diff vs baseline": diff["mean"], "Diff LB": diff["lb"],
                             "Diff UB": diff["ub"], "Significant": significant,
                             "Replications Needed": needed, "CRN Ratio": ratio})
        return pd.DataFrame(rows, columns=COMPARISON_COLS)


def _scenario_layouts(base: dict, scenario_names, baseline: bool = True) -> dict[str, dict]:
    """Apply and validate every chosen scenario up front, so a typo in an
    override is an error before the first replication rather than after the
    last one."""
    wanted = None if scenario_names is None else list(scenario_names)
    # A caller's subset always gets the comparison baseline too (its own doc,
    # and the run_scenarios agent tool's, promise it): dropping it here is
    # what used to make ExperimentResult.comparison()'s default lookup raise
    # "unknown baseline scenario" (DEVLOG #275). `baseline=False` is the one
    # caller that holds a baseline already.
    if wanted is not None and baseline and BASELINE not in wanted:
        wanted = [BASELINE] + wanted
    layouts: dict[str, dict] = {}
    for scenario in scenarios_of(base):
        name = scenario.get("name") or "<unnamed>"
        if wanted is not None and name not in wanted:
            continue
        try:
            candidate = apply_overrides(base, scenario.get("overrides", []))
        except ModelValidationError as exc:
            raise ModelValidationError([f"scenario '{name}': {p}" for p in exc.problems],
                                       structured=exc.structured) from None    # its did-you-mean (#1773)
        fatal = schema.fatal_problems(schema.validate_model_structured(candidate))  # by severity (#247)
        if fatal:
            raise ModelValidationError(
                [f"scenario '{name}': {p}" for p in schema.format_problems(fatal)],
                structured=fatal)       # the codes, for a page that words them (D1088)
        layouts[name] = candidate
    if wanted is not None:
        missing = [n for n in wanted if n not in layouts]
        if missing:
            raise ModelValidationError([f"unknown scenario: {n}" for n in missing])
    return layouts


def _window(candidate: dict, replications, run_length, warmup) -> tuple:
    """(replications, run length, warm-up) one scenario runs with: its own
    `simulation.time_limit` and `warmup` unless the caller gave one (#315). One
    place, so the recording of a run (`record_scenarios`) has the window of that run."""
    sim = candidate.get("simulation", {})
    replications, warm, length = api.run_settings(
        candidate, replications,
        (sim.get("warmup") or 0.0) if warmup is None else warmup,
        (sim.get("time_limit") or 0.0) if run_length is None else run_length)
    return replications, length, warm


def run_scenarios(layout, scenario_names=None, *, replications: int | None = None, seed: int = 0,
                  run_length: float | None = None, warmup: float | None = None, crn: bool = True,
                  workers: int | str = 1, on_progress=None, should_stop=None,
                  pool=None, baseline: bool = True, logic=None,
                  controllers: dict | None = None, keep_empty: bool = False) -> ExperimentResult:
    """Run every named scenario (all of them by default) and return the results.

    Parameters
    ----------
    layout : dict
        The model, with a ``scenarios`` list of ``{"name", "overrides"}``.
    scenario_names : list of str, optional
        A subset to run. The baseline runs too, unless *baseline* is False.
    replications, run_length, warmup : optional
        Fall back to the model's own ``simulation`` section, as in
        `nagasim.api.run_model`; run length and warm-up fall back per scenario,
        so a scenario may override either.
    seed : int, default 0
        Base seed.
    crn : bool, default True
        Common random numbers: replication ``r`` of every scenario uses seed
        ``seed + r``, and each block draws from its own stream, so a block a
        scenario did not touch draws the same numbers in both runs. The
        comparison then uses the paired t interval. With False each scenario
        gets a disjoint seed block and the comparison uses Welch's interval.
    workers : int or "auto", default 1
        ``"auto"`` times the first replication of the first scenario once and
        reuses that worker count for every scenario.
    on_progress : callable, optional
        ``on_progress(done, total)`` counts replications across all scenarios.
    should_stop : callable, optional
        Stops between replications and leaves ``completed`` False on the result.
    pool : ProcessPoolExecutor, optional
        A pool the caller owns and reuses across studies (best built with
        `nagasim.api.new_pool`). Results are unchanged.
    baseline : bool, default True
        False skips the baseline, for a caller that keeps the baseline's results
        from an earlier run; the result then has nothing to compare against until
        the caller adds it.
    logic : str or module, optional
        The caller's LOGIC module, as `nagasim.api.run_model` takes it; every
        scenario's LOGIC blocks take their classes from it first.
    controllers : dict, optional
        ``{decision point: controller}`` for the Policy-mode DECIDE blocks of every
        scenario, as `nagasim.api.run_model` takes it; with *workers* not 1 each
        controller must pickle.
    keep_empty : bool, default False
        Also keep each scenario's frames with no rows, as `nagasim.api.run_model` (D1328):
        by default only frames with rows are kept, plus ``"system"`` always.

    Returns
    -------
    ExperimentResult
        Per-scenario results and ``comparison(kpis=...)`` against the baseline.

    Notes
    -----
    The unit of parallel work is one (scenario, replication), so five scenarios
    of one replication each fill five workers.

    See Also
    --------
    nagasim.api.run_model : One model, no scenarios.
    nagasim.optimize.optimize : Search the space instead of naming the points.

    Examples
    --------
    >>> import json
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import experiments
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> layout = json.loads(path.read_text(encoding="utf-8"))
    >>> layout["scenarios"] = [{"name": "Faster server", "overrides": [
    ...     {"path": "blocks.process_1.params.dist_params.rate", "value": 1.6}]}]
    >>> experiment = experiments.run_scenarios(layout, replications=3, run_length=200, warmup=20, seed=0)
    >>> sorted(experiment.results)
    ['Baseline', 'Faster server']
    """
    base = schema.load_layout(layout)
    if replications is None:
        replications = int(base.get("simulation", {}).get("replications") or 1)
    layouts = _scenario_layouts(base, scenario_names, baseline)
    # Tasks go straight to the pool, so the "model never ends on its own" guard
    # that `run_model` used to apply per scenario is applied here instead --
    # per scenario, each with its OWN simulation.time_limit and warmup unless
    # the caller gave one (#315).
    windows: dict[str, tuple] = {}
    for name, candidate in layouts.items():
        replications, length, warm = _window(candidate, replications, run_length, warmup)
        windows[name] = (length, warm)
    total = len(layouts) * replications
    result = ExperimentResult(crn=crn)
    api._check_controllers(controllers, workers, pool)
    logic_module, logic_name = api._logic_module(logic, workers)    # imported before any run

    if workers == "auto":
        # ponytail: this probe replication is thrown away rather than folded
        # into scenario 0's results (that would mean threading a partially-run
        # scenario through api.run_model's own replication loop). Upgrade path:
        # have api.run_model accept a pre-computed first replication if the
        # extra replication ever matters for a real study's compute budget.
        first_layout = next(iter(layouts.values()), None)
        if first_layout is None or (should_stop is not None and should_stop()):
            result.completed = False
            return result
        t0 = time.perf_counter()
        run_length, warmup = next(iter(windows.values()))
        probe = api.run_single_simulation(first_layout, seed=seed, run_len=run_length,
                                          warmup=warmup, should_stop=should_stop,
                                          controllers=controllers, logic_module=logic_module)
        elapsed = time.perf_counter() - t0
        if probe is None:
            result.completed = False
            return result
        workers = api.auto_workers(elapsed, max(total - 1, 0))

    def jobs():
        for index, (name, candidate) in enumerate(layouts.items()):
            offset = seed if crn else seed + index * replications
            for r in range(replications):
                yield (name, candidate, offset + r, *windows[name])

    # ONE process pool for the whole call (PHASE4 A4): a fresh pool per scenario
    # cost seconds of interpreter start-up each on Windows spawn. Serial runs
    # (and the "auto" crossover, resolved above) still start no process at all.
    # A caller running MANY studies passes its own `pool` and pays that spawn
    # once for all of them, exactly as `api.run_model` already allows.
    with (nullcontext(pool) if pool is not None else api.scenario_pool(workers)) as pool:
        collected = api.run_jobs(jobs(), pool=pool,
                                 workers=workers if isinstance(workers, int) else 1,
                                 on_progress=on_progress, should_stop=should_stop, total=total,
                                 controllers=controllers,
                                 logic=logic_module if pool is None and workers == 1 else logic_name)

    for name in layouts:
        pairs = collected.get(name) or []
        if len(pairs) < replications or not pairs:
            result.completed = False        # stopped, or nothing to report
            break
        frames = api.frames_with_rows(concat_runs([kpi for _, kpi in pairs]), keep_empty)   # D1328
        # D906: what `checks()` judges the scenario by, as `api.run_model` sets them for a run.
        frames._layout, frames._warmup = layouts[name], float(windows[name][1] or 0)
        # D1389 (final review M2): the scenario's run-time warnings, as a run's
        frames._run_problems = api._union_problems(kpi[PROBLEMS_KEY][1] for _, kpi in pairs
                                                   if PROBLEMS_KEY in kpi)
        result.results[name] = frames
        result.seeds[name] = [s for s, _ in pairs]
        result.warmups[name] = float(windows[name][1] or 0)
    return result


WHAT_IF = "What-if"      # the temporary scenario's name; never stored in the model


class UnknownKPIError(ValueError):
    """KPI keys the results do not have; `nearest` holds the closest keys they do have."""

    def __init__(self, unknown: list[str], nearest: list[str]):
        self.unknown, self.nearest = unknown, nearest
        super().__init__("unknown KPI " + ", ".join(repr(k) for k in unknown)
                         + ": the results have no such key")


# The study tools print a KPI without its frame ("Avg Throughput Time", "Share over 20 min",
# "Server/Time in Queue Avg"); these frames are tried in order before any other.
_BARE_FRAMES = ("system", "user", "resource")


def match_kpi(key: str, known) -> str | None:
    """The results key *key* names: itself, or a bare name under one frame; None when it names none."""
    if key in known:
        return key
    for frame in _BARE_FRAMES:
        if f"{frame}/{key}" in known:
            return f"{frame}/{key}"
    tails = [k for k in sorted(known) if k.partition("/")[2] == key]
    return tails[0] if len(tails) == 1 else None


def study_kpi_name(key: str) -> str:
    """The name the study tools measure for results key *key*: `match_kpi`'s frames dropped
    ("system/Avg Queue Length" -> "Avg Queue Length"); any other name comes back as is (#1758)."""
    frame, _, rest = key.partition("/")
    return rest if frame in _BARE_FRAMES and rest else key


def resolve_kpis(layout, kpis, scenario_names=None, *, seed: int = 0, run_length: float | None = None,
                 warmup: float | None = None, should_stop=None) -> dict[str, str] | None:
    """{asked: results key} for *kpis* (None stays None), learned from one replication of the
    chosen scenarios before the real run, so a typo costs one replication and not the whole
    sweep (#1728). Raises UnknownKPIError, with the nearest keys, for a key no result has; a
    stopped probe resolves nothing and passes the keys through."""
    if kpis is None:
        return None
    probe = run_scenarios(layout, scenario_names, replications=1, seed=seed, run_length=run_length,
                          warmup=warmup, should_stop=should_stop)
    known = set(probe.kpis()["KPI"])
    if not probe.completed or not known:
        return {k: k for k in kpis}
    found = {k: match_kpi(k, known) for k in kpis}
    unknown = [k for k, v in found.items() if v is None]
    if unknown:
        tails = {}
        for full in sorted(known):
            tails.setdefault(full.partition("/")[2], full)
        near = difflib.get_close_matches(unknown[0], list(known) + list(tails), n=3, cutoff=0.5)
        raise UnknownKPIError(unknown, list(dict.fromkeys(tails.get(k, k) for k in near)))
    return found


def paired_until_clear(layout, overrides: list[dict], kpis: list[str] | None = None, *,
                       start: int = 5, cap: int = 50, seed: int = 0,
                       run_length: float | None = None, warmup: float | None = None,
                       should_stop=None) -> tuple:
    """The runs behind `what_if`: the baseline against *overrides* on common random
    numbers, replications doubled (start, 2 x start, ... up to *cap*) until every KPI
    in *kpis* (default: the system KPIs) is separated or unchanged.

    Returns (n, met, result, table): the `ExperimentResult` of the last batch (None
    when stopped) and its comparison rows for the changed model. `nagasim.explain`
    reads the per-replication frames of the same result.
    """
    base = schema.load_layout(layout)
    base["scenarios"] = [{"name": WHAT_IF, "overrides": list(overrides)}]
    n = max(2, start)
    # ponytail: each doubling reruns from replication 0 (same seeds, so the
    # first n results repeat) -- about 2x the final cost, instead of threading
    # extra replications through run_scenarios. Upgrade if studies get long.
    while True:
        result = run_scenarios(base, [WHAT_IF], replications=n, seed=seed, run_length=run_length,
                               warmup=warmup, should_stop=should_stop)
        if not result.completed:
            return n, False, None, None
        table = result.comparison(kpis=kpis)
        table = table[table["Scenario"] == WHAT_IF]
        if kpis is None:
            table = table[table["KPI"].str.startswith("system/")]
        # #766: a KPI identical in every paired replication (a counter under CRN) is unchanged,
        # not undecided, so it does not hold the doubling back.
        unchanged = (table["Diff LB"] == 0) & (table["Diff UB"] == 0)
        met = bool(len(table)) and bool((table["Significant"] | unchanged).all())
        if met or n >= cap:
            return n, met, result, table
        n = min(cap, n * 2)


def what_if(layout, overrides: list[dict], kpis: list[str] | None = None, *, start: int = 5,
            cap: int = 50, seed: int = 0, run_length: float | None = None,
            warmup: float | None = None, goals: dict | None = None, should_stop=None) -> dict:
    """Does this change matter? Run the baseline against *overrides* on common
    random numbers, doubling the replications (start, 2 x start, ... up to *cap*)
    until every KPI's paired difference interval excludes zero.

    *kpis* default to the system KPIs. *goals* maps a KPI to "min" or "max"; with
    one the verdict is "better"/"worse", without it "higher"/"lower". Returns
    {"n", "met" (every KPI separated or unchanged), "completed", "kpis": [{"kpi", "baseline",
    "scenario", "diff", "diff_lb", "diff_ub", "verdict"}]}. An override naming
    something the model does not have raises ModelValidationError before any run, and a KPI
    key the results do not have UnknownKPIError after one probe replication (`resolve_kpis`).
    """
    goals = goals or {}
    if kpis is not None:        # resolved before any replication of the sweep (#1728)
        base = schema.load_layout(layout)
        base["scenarios"] = [{"name": WHAT_IF, "overrides": list(overrides)}]
        names = resolve_kpis(base, kpis, [WHAT_IF], seed=seed, run_length=run_length, warmup=warmup,
                             should_stop=should_stop)
        goals = {names.get(k, k): v for k, v in goals.items()}
        kpis = list(dict.fromkeys(names.values()))
    n, met, result, table = paired_until_clear(layout, overrides, kpis, start=start, cap=cap,
                                               seed=seed, run_length=run_length, warmup=warmup,
                                               should_stop=should_stop)
    if result is None:
        return {"n": n, "met": False, "completed": False, "kpis": []}
    rows = []
    for _, r in table.iterrows():
        diff = float(r["Diff vs baseline"])
        goal = goals.get(r["KPI"])
        if r["Diff LB"] == 0 and r["Diff UB"] == 0:
            verdict = "unchanged"
        elif not r["Significant"]:
            verdict = "not distinguishable"
        elif goal in ("min", "max"):
            verdict = "better" if (diff < 0) == (goal == "min") else "worse"
        else:
            verdict = "higher" if diff > 0 else "lower"
        rows.append({"kpi": r["KPI"], "baseline": float(r["Mean"]) - diff, "scenario": float(r["Mean"]),
                     "diff": diff, "diff_lb": float(r["Diff LB"]), "diff_ub": float(r["Diff UB"]),
                     "verdict": verdict})
    return {"n": n, "met": met, "completed": True, "kpis": rows}


SIZING_COLS = ["Level", "Next", "N", "Stat", "Stat next", "Delta", "Delta LB", "Delta UB",
               "Half width", "Half width next", "Guard", "Guard next", "Pays", "Reason"]
_SIZING = "Sizing"          # the temporary scenario's name; never stored in the model
MAX_SIZING_STEPS = 20


@dataclass
class SizingResult:
    """What `size_capacity` returns. `answer` is the smallest level whose next step does not
    pay, None when *stop* came first (or the study was stopped); `steps` one row per step
    tried (SIZING_COLS)."""

    path: str
    kpi: str
    statistic: str
    answer: object
    steps: pd.DataFrame
    completed: bool = True


def size_capacity(layout, path: str, *, kpi: str, goal: str = "min", statistic: str = "p95",
                  tolerance: float = 0.0, guard: str | None = None, guard_goal: str = "min",
                  guard_tolerance: float = 0.0, start=None, step=1, stop=None,
                  replications: int = 20, max_replications: int = 320,
                  precision: float | None = None, seed: int = 0, confidence: float = 0.95,
                  run_length: float | None = None, warmup: float | None = None,
                  workers: int | str = 1, on_progress=None, should_stop=None, logic=None,
                  controllers: dict | None = None) -> SizingResult:
    """How much of a capacity is enough: raise the value at *path* by *step* from *start*
    (default: the model's own) until one more step does not pay.

    *stop* is the highest level tried (default: *start* + MAX_SIZING_STEPS steps). Each step
    compares level L with L + step on common random numbers (replication r of
    both on seed ``seed + r``), the same pairing `what_if` uses. Replications start at
    *replications* and double, up to *max_replications*, until each arm's 95% interval of
    the *kpi* mean has a half-width under *precision* (default: 5% of that arm's mean); an arm
    whose values are all equal is precise as it is.

    The next step does not pay when BOTH hold:

    * the gain in *statistic* (``"mean"`` or a percentile such as ``"p95"``) of *kpi*, in
      the *goal*'s direction, is not above *tolerance* (in the KPI's units), so a step that
      changes nothing does not pay;
    * the *guard* KPI, when given (e.g. a count of overflows, `slot/<block>/Overflows`),
      does not improve in its mean by more than *guard_tolerance* in *guard_goal*'s
      direction -- so a step that removes counted events still pays.

    Each row of ``steps`` carries the paired delta of the statistic (next minus level) with
    a bootstrap interval on the shared seeds (`bootstrap_ci`). Levels already run are reused,
    so each step pays only for the new level. *logic* and *controllers* are the caller's
    policies, as `run_scenarios` takes them.
    """
    base = schema.load_layout(layout)
    stat_of = _statistic(statistic)       # the parameter shadows the function here
    better = -1.0 if goal == "min" else 1.0
    guard_better = -1.0 if guard_goal == "min" else 1.0
    level = value_at(base, path) if start is None else start
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        raise ValueError(f"size_capacity: {path} is {level!r}, not a number")
    if not step > 0:
        raise ValueError("size_capacity: step must be above 0")
    if stop is None:            # a step that always pays must not run forever
        stop = level + MAX_SIZING_STEPS * step
    runs: dict = {}         # level -> long KPI frame (Run, KPI, Value) of the seeds run so far

    def ensure(value, n: int) -> bool:
        have = runs.get(value)
        count = 0 if have is None else int(have["Run"].nunique())
        if count >= n:
            return True
        model = dict(base, scenarios=[{"name": _SIZING, "overrides": [{"path": path, "value": value}]}])
        result = run_scenarios(model, [_SIZING], replications=n - count, seed=seed + count,
                               run_length=run_length, warmup=warmup, workers=workers,
                               on_progress=on_progress, should_stop=should_stop, baseline=False,
                               logic=logic, controllers=controllers)
        if not result.completed:
            return False
        flat = flatten_kpis(result.results[_SIZING])
        runs[value] = flat if have is None else pd.concat([have, flat], ignore_index=True)
        return True

    def values(value, key: str, n: int) -> np.ndarray:
        found = _series(runs[value], key)[:n]
        if not found.size:
            raise ValueError(f"unknown KPI: {key!r}")
        return found

    def tight(arr) -> bool:
        # An arm whose values are all equal (a wait of 0 in every replication) is as precise as it
        # gets; 5% of a mean of 0 is a target no interval can meet (#1730).
        if arr.size and np.all(arr == arr[0]):
            return True
        stat = ci95(arr, confidence)
        h = stat["ub"] - stat["mean"]
        return h < (precision if precision is not None else 0.05 * abs(stat["mean"]))

    def half(arr) -> float:
        stat = ci95(arr, confidence)
        return stat["ub"] - stat["mean"]

    rows, answer, completed = [], None, True
    while level + step <= stop:
        nxt = level + step
        # The level's earlier doubling stands: never judge it again on fewer runs.
        have = runs.get(level)
        n = max(2, int(replications), 0 if have is None else int(have["Run"].nunique()))
        while True:
            if not (ensure(level, n) and ensure(nxt, n)):
                completed = False
                break
            arms = [values(v, kpi, n) for v in (level, nxt)]
            if all(tight(a) for a in arms) or n >= max_replications:
                break
            n = min(int(max_replications), 2 * n)
        if not completed:
            break
        a, b = values(level, kpi, n), values(nxt, kpi, n)
        delta = bootstrap_ci(lambda x, y: stat_of(y) - stat_of(x), a, b, confidence=confidence,
                             seed=seed)
        gain = better * delta["mean"]
        row = {"Level": level, "Next": nxt, "N": n, "Stat": stat_of(a), "Stat next": stat_of(b),
               "Delta": delta["mean"], "Delta LB": delta["lb"], "Delta UB": delta["ub"],
               "Half width": half(a), "Half width next": half(b),
               "Guard": math.nan, "Guard next": math.nan}
        guarded = False
        if guard:
            g_a, g_b = values(level, guard, n), values(nxt, guard, n)
            row["Guard"], row["Guard next"] = float(np.nanmean(g_a)), float(np.nanmean(g_b))
            guarded = guard_better * (row["Guard next"] - row["Guard"]) > guard_tolerance
        # Strictly above (#1730): on common random numbers a step that changes nothing has a
        # delta of exactly 0, which must not pay at the default tolerance of 0.
        pays = gain > tolerance or guarded
        row["Pays"] = bool(pays)
        row["Reason"] = (f"{statistic} gain {gain:.4g} > tolerance {tolerance:g}" if gain > tolerance
                         else f"{statistic} gain {gain:.4g} <= tolerance, but {guard} improves" if guarded
                         else f"{statistic} gain {gain:.4g} <= tolerance"
                              + (f" and {guard} does not improve" if guard else ""))
        rows.append(row)
        if not pays:
            answer = level
            break
        level = nxt
    return SizingResult(path=path, kpi=kpi, statistic=str(statistic), answer=answer,
                        steps=pd.DataFrame(rows, columns=SIZING_COLS), completed=completed)


def record_scenarios(layout, result: ExperimentResult, *, run_length: float | None = None,
                     warmup: float | None = None) -> dict:
    """`{scenario: TraceRecorder}`: the first replication of every scenario in
    *result*, recorded (PHASE7 7.7, D200).

    `run_scenarios` records nothing: its replications may run in a process pool,
    and a recorder cannot come back from a child. A replication is a function of
    its layout and its seed, so each first one is run AGAIN here, in this process,
    with that seed and a recorder: it is the replication whose numbers *result*
    holds. Pass the *run_length* and *warmup* the study was run with (None: each
    scenario's own settings, as in `run_scenarios`). With common random numbers
    the scenarios share their arrivals, so the recordings compare frame by frame.

    *layout* must be the model the study ran on: a scenario it no longer has is a
    ModelValidationError.
    """
    from nagasim.core.trace import TraceRecorder

    layouts = _scenario_layouts(schema.load_layout(layout), list(result.results))
    recordings = {}
    for name in result.results:
        _replications, length, warm = _window(layouts[name], 1, run_length, warmup)
        recorder = TraceRecorder()
        api.run_single_simulation(layouts[name], seed=result.seeds[name][0], run_len=length,
                                  warmup=warm, subscribers=(recorder,))
        recordings[name] = recorder
    return recordings


# ---------------------------------------------------------------- precision
def _merge(a: dict, b: dict) -> dict:
    return {key: pd.concat([a[key], b[key]], ignore_index=True) for key in a}


def run_until_precision(layout, kpi: str, *, relative: float | None = 0.05,
                        absolute: float | None = None, confidence: float = 0.95,
                        n0: int = 10, max_replications: int = 200, seed: int = 0,
                        run_length: float | None = None, warmup: float | None = None,
                        workers: int = 1, on_progress=None, should_stop=None):
    """Keep adding replications until the confidence interval on a KPI is tight enough.

    After the first *n0* replications the needed count is the textbook
    ``n >= (t * s / h)**2`` from the current sample. Each step runs at most as
    many replications again as have run so far (never more than doubling), so a
    wildly optimistic first estimate cannot blow the budget in one go.

    Parameters
    ----------
    layout : dict
        The model.
    kpi : str
        A `flatten_kpis` key, e.g. ``"system/Avg Throughput Time"``.
    relative : float, default 0.05
        Target half-width as a fraction of ``|mean|``; used when *absolute* is
        not given.
    absolute : float, optional
        Target half-width in the KPI's own unit; wins over *relative*.
    confidence : float, default 0.95
        Confidence level of the interval.
    n0 : int, default 10
        Replications of the first pass.
    max_replications : int, default 200
        Hard cap.
    seed : int, default 0
        Seed of replication 0.
    run_length, warmup : float, optional
        Fall back to the model's own settings.
    workers : int, default 1
        Process-pool size.
    on_progress, should_stop : callable, optional
        As in `nagasim.api.run_model`.

    Returns
    -------
    tuple
        ``(results, report)``. The report carries ``replications``, the achieved
        ``half_width``, the ``target`` and ``met``.

    See Also
    --------
    nagasim.api.run_model : A fixed number of replications.
    nagasim.experiments.flatten_kpis : Where the KPI keys come from.

    Examples
    --------
    >>> import json
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import experiments
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> layout = json.loads(path.read_text(encoding="utf-8"))
    >>> results, report = experiments.run_until_precision(
    ...     layout, "system/Avg Queue Length", relative=0.5, n0=3, run_length=200, warmup=20, seed=0)
    >>> report["met"] or report["replications"] == 200
    True
    """
    base = schema.load_layout(layout)
    sim = base.get("simulation", {})
    if run_length is None:
        run_length = sim.get("time_limit") or 0.0
    if warmup is None:
        warmup = sim.get("warmup") or 0.0

    max_replications = max(2, int(max_replications))
    n = max(2, min(int(n0), max_replications))
    results = api.run_model(base, replications=n, seed=seed, run_length=run_length,
                            warmup=warmup, workers=workers, on_progress=on_progress,
                            should_stop=should_stop)

    half = target = mean = float("nan")
    while results is not None:
        values = _series(flatten_kpis(results), kpi)
        if values.size == 0:
            raise ValueError(f"unknown KPI: {kpi!r}")
        stat = ci95(values, confidence)
        mean, half = stat["mean"], stat["ub"] - stat["mean"]
        target = float(absolute) if absolute is not None else float(relative or 0.0) * abs(mean)
        if half <= target or n >= max_replications or target <= 0:
            break
        if should_stop is not None and should_stop():
            break
        # (t * s / h)^2, capped at doubling and at the replication budget
        t = float(sp_stats.t.ppf(0.5 + confidence / 2.0, n - 1))
        needed = int(math.ceil((t * stat["sd"] / target) ** 2))
        nxt = min(max(needed, n + 1), 2 * n, max_replications)
        extra = api.run_model(base, replications=nxt - n, seed=seed + n, run_length=run_length,
                              warmup=warmup, workers=workers, on_progress=on_progress,
                              should_stop=should_stop)
        if extra is None:
            break
        results = _merge(results, extra)
        n = nxt

    report = {"kpi": kpi, "replications": n, "mean": mean, "half_width": half,
              "target": target, "confidence": confidence,
              "met": bool(np.isfinite(half) and np.isfinite(target) and half <= target)}
    return results, report


# ---------------------------------------------------------------- warm-up (MSER-5)
@dataclass
class WarmupSuggestion:
    """`suggest_warmup`'s answer: a truncation time in model time units, the
    MSER curve it was picked from, and whether that pick can be trusted."""

    warmup: float
    curve: np.ndarray
    reliable: bool
    index: int = 0          # observation index the truncation time comes from


def _throughput_series(source) -> tuple[np.ndarray, np.ndarray]:
    """(exit times, throughput times) averaged across replications.

    Each replication's completed entities are ordered by exit time, then the
    k-th completion of every replication is averaged with the k-th of the
    others -- the across-replication average MSER-5 is meant to be run on.
    Replications are truncated to the shortest one so every averaged point has
    the same number of contributors.
    """
    log_df = source.get("entity_log") if isinstance(source, dict) else source   # absent when empty (D1321)
    if log_df is None or len(log_df) == 0:
        return np.array([]), np.array([])
    df = log_df.dropna(subset=["Exited At", "Throughput Time"])
    if "Run" not in df.columns:
        df = df.assign(Run=0)
    runs = [grp.sort_values("Exited At") for _, grp in df.groupby("Run", sort=True)]
    runs = [g for g in runs if len(g)]
    if not runs:
        return np.array([]), np.array([])
    n = min(len(g) for g in runs)
    times = np.mean([g["Exited At"].to_numpy(dtype=float)[:n] for g in runs], axis=0)
    values = np.mean([g["Throughput Time"].to_numpy(dtype=float)[:n] for g in runs], axis=0)
    return times, values


def _mser(values: np.ndarray, batch: int) -> tuple[int, np.ndarray, bool]:
    """MSER-d on a series; returns (batches to drop, curve, reliable)."""
    k = values.size // batch
    if k < 2:
        return 0, np.array([]), False
    means = values[:k * batch].reshape(k, batch).mean(axis=1)
    curve = np.full(k, np.inf)
    for d in range(k - 1):          # at least two batch means must survive
        tail = means[d:]
        curve[d] = float(((tail - tail.mean()) ** 2).sum()) / (tail.size ** 2)
    best = int(np.argmin(curve))
    return best, curve, bool(best < k / 2)


def suggest_warmup(source, *, batch: int = 5) -> WarmupSuggestion:
    """Suggest a warm-up length from a run's entity log with MSER-5.

    MSER-5 (White 1997) groups the output series into batches of *batch*
    observations and picks the truncation point ``d`` that minimises
    ``sum_{i>d} (m_i - mean_{i>d})**2 / (k - d)**2`` over the batch means
    ``m_1..m_k``: the head to drop that leaves the most precise estimate of the
    steady-state mean. The series is the entity throughput time ordered by exit
    time and averaged across replications.

    Parameters
    ----------
    source : dict or DataFrame
        A results dict from `nagasim.api.run_model`, or an entity-log frame with
        the columns ``Run``, ``Exited At`` and ``Throughput Time``.
    batch : int, default 5
        Observations per batch.

    Returns
    -------
    WarmupSuggestion
        ``warmup`` is the exit time of the first observation that survives the
        truncation, in model time units, ready for a run's warm-up setting.
        ``reliable`` is False when the minimum falls in the second half of the
        batch means. With few batches left the denominator dominates, so a series
        still drifting always looks best when truncated near the end; read such a
        suggestion as "the run is too short to tell".

    See Also
    --------
    nagasim.api.run_model : Produces the results this reads.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import api, experiments
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> results = api.run_model(str(path), replications=2, run_length=300, warmup=0, seed=0)
    >>> suggestion = experiments.suggest_warmup(results)
    >>> suggestion.warmup >= 0 and suggestion.reliable in (True, False)
    True
    """
    times, values = _throughput_series(source)
    best, curve, reliable = _mser(values, batch)
    index = best * batch
    warmup = float(times[index]) if index < times.size and best > 0 else 0.0
    return WarmupSuggestion(warmup=warmup, curve=curve, reliable=reliable, index=index)


# ---------------------------------------------------------------- CLI (ROADMAP 1.7 C)
def _workers_type(value: str):
    return value if value == "auto" else int(value)


def _compact(table: pd.DataFrame) -> str:
    """The short comparison (D1227): Mean, the difference to the baseline with its half-width, and
    whether it is significant. The baseline's own rows show no difference."""
    base = table["Diff vs baseline"].eq(0) & ~table["Significant"]
    out = pd.DataFrame({"KPI": table["KPI"], "Scenario": table["Scenario"],
                        "Mean": table["Mean"].map("{:.4g}".format),
                        "Diff": table["Diff vs baseline"].map("{:+.4g}".format).where(~base, ""),
                        "+/-": ((table["Diff UB"] - table["Diff LB"]) / 2).map(
                            "{:.4g}".format).where(~base, ""),
                        "Significant": table["Significant"].map({True: "yes", False: "no"})
                        .where(~base, "")})
    return out.to_string(index=False)


def main(argv=None) -> int:
    """`naga scenarios model.json ...`: run named scenarios headless and
    print/export the comparison table. Dispatched from `nagasim.cli`
    without importing Qt."""
    setup_cli()

    parser = argparse.ArgumentParser(prog="naga scenarios",
                                     description="Run named scenarios headless and compare them.")
    add_model_argument(parser)
    parser.add_argument("--scenario", action="append", dest="scenarios", default=None,
                        help="scenario name to run (default: all); may repeat")
    parser.add_argument("--replications", type=int, default=None,
                        help="default: the model's own 'simulation.replications'")
    parser.add_argument("--run-length", type=float, default=None,
                        help="default: the model's own 'simulation.time_limit'")
    parser.add_argument("--warmup", type=float, default=None,
                        help="default: the model's own 'simulation.warmup'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=_workers_type, default=1,
                        help="replications to run in parallel processes, or 'auto' (default: 1)")
    parser.add_argument("--no-crn", action="store_true",
                        help="disjoint seeds per scenario (Welch's interval) instead of paired CRN")
    parser.add_argument("--kpi", action="append", dest="kpis", default=None, metavar="KEY",
                        help="show this KPI (a key such as system/Avg Throughput Time or "
                             "cost/Robot/Amount); may repeat. Default: the system KPIs `naga run` prints")
    parser.add_argument("--all", action="store_true",
                        help="show every KPI of every family (the table --out writes)")
    parser.add_argument("--out", help="write the comparison table here (.xlsx or .csv); "
                                      "always every KPI")
    parser.add_argument("--report", help="write an HTML report here")
    parser.add_argument("--player", metavar="PATH",
                        help="write a shareable HTML player here: the first replication of "
                             "every scenario, run again with its seed and recorded, in one "
                             "self-contained file with a scenario switch")
    from nagasim import netguard
    from nagasim.viewer import ASKED_HELP, read_asked     # late: viewer builds on this module
    parser.add_argument("--asked", metavar="FILE", help="with --player: " + ASKED_HELP)
    parser.add_argument("--lang", choices=("en", "it"), default="en",
                        help="with --player: the language of the page (default en)")
    args = parser.parse_args(argv)
    asked = None
    if args.asked:
        if not args.player:
            print("--asked: it feeds a player's trust card: pass --player PATH too")
            return 2
        try:            # read before the runs, so a refused file costs none
            asked = read_asked(args.asked)
        except netguard.GuardError as exc:
            print(f"--asked: {exc.code}: {exc.message}")
            return 2

    result = run_scenarios(args.model, args.scenarios, replications=args.replications,
                           seed=args.seed, run_length=args.run_length, warmup=args.warmup,
                           crn=not args.no_crn, workers=args.workers)
    comparison = result.comparison() if result.results else None
    if comparison is not None:
        shown = comparison
        if not args.all:        # D1227: the short table; `--out` below still gets everything
            keys = args.kpis or [k for k in comparison["KPI"].unique() if k.startswith("system/")]
            shown = comparison[comparison["KPI"].isin(keys)]
            if shown.empty:
                print("no such KPI; --all lists every key")
                return 2
        print(shown.to_string(index=False) if args.all else _compact(shown))
    else:
        print("no scenarios completed")

    if args.out:
        out = Path(args.out)
        if comparison is None:
            print("nothing to write")
        elif out.suffix.lower() == ".csv":
            comparison.to_csv(out, index=False)
            print(f"Comparison written to {out}")
        else:
            from nagasim import investment          # it imports this module
            layout = api.load_layout(args.model)
            with pd.ExcelWriter(out) as writer:
                comparison.to_excel(writer, index=False, sheet_name="Comparison")
                # D172: only for a model with the section whose baseline was run.
                if (layout.get("investment") is not None and
                        (investment.investment_settings(layout)["baseline"] or BASELINE)
                        in result.results):
                    investment.investment_case(result, layout).to_excel(
                        writer, index=False, sheet_name="Investment")
            print(f"Comparison written to {out}")

    if args.report:
        from nagasim.report import build_report
        base_layout = api.load_layout(args.model)
        base_results = result.results.get(BASELINE) or next(iter(result.results.values()), None)
        if base_results is None:
            print("no scenarios completed; report not written")
        else:
            build_report(base_layout, base_results, experiment=result,
                         title=Path(args.model).stem, path=args.report)
            print(f"Report written to {args.report}")

    if args.player:
        if not result.results:
            print("no scenarios completed; player not written")
        else:
            from nagasim.player import build_player, disk_pictures, page_title
            layout = api.load_layout(args.model)
            recordings = record_scenarios(layout, result, run_length=args.run_length,
                                          warmup=args.warmup)
            try:
                build_player(layout, recordings, experiment=result,
                             title=page_title(layout, Path(args.model).stem),
                             path=args.player, asked=asked, pictures=disk_pictures(layout, args.model),
                             language=args.lang)
            except ValueError as exc:       # over the size limit
                print(f"--player: {exc}")
                return 1
            print(f"Player written to {args.player}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
