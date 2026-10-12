"""Investment case (D172-D173): payback, NPV and ROI of each scenario against a
baseline. Post-processing over `experiments.run_scenarios`, not an engine
feature: one run cannot know its baseline, and every input is a scenario KPI.

    result = experiments.run_scenarios(layout, replications=10)
    table = investment.investment_case(result, layout)

The model's `investment` section holds the settings; a `fixed_costs` entry
flagged `capex: true` is an upfront investment. The flag is read here only:
`Total Cost` and every frame of a run are the same with and without it.

Per scenario and replication, with W the observed window in hours (the system
frame's "Simulation Time", so each scenario is scaled by its own):

    Capex               = the flagged fixed costs' amounts in `cost_summary`
    Operating cost/year = (Total Cost - Capex) x operating_hours_per_year / W
    Parts/year          = throughput x operating_hours_per_year / W

and against the baseline, replication by replication:

    Saving/year = Operating cost(baseline) - Operating cost
                  + margin_per_part x (Parts - Parts(baseline))
    NPV         = -Extra capex + Saving discounted over the horizon

Top-level beside `experiments.py` because it needs pandas.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import argparse
import math
import sys

import numpy as np
import pandas as pd

from nagasim import api
from nagasim._cli import setup_cli
from nagasim.core.problems import problem
from nagasim.core.stats import paired_diff_ci, welch_diff_ci
from nagasim.core.submodels import flatten
from nagasim.core.timeunits import conversion_factor
from nagasim.experiments import (BASELINE, KPI_PREFIXES, ExperimentResult, apply_overrides,
                                 flatten_kpis, run_scenarios, scenarios_of)

__all__ = ["COLUMNS", "DEFAULTS", "THROUGHPUT", "npv", "payback_years", "roi_percent",
           "investment_settings", "investment_case", "validate_investment", "main"]

COLUMNS = ["Scenario", "Capex", "Extra Capex", "Operating Cost per Year", "Parts per Year",
           "Saving per Year", "Saving CI Low", "Saving CI High", "Payback Years", "Pays Back",
           "NPV", "NPV CI Low", "NPV CI High", "ROI %"]
# baseline "" is the model as saved (`experiments.BASELINE`); throughput "" is THROUGHPUT.
DEFAULTS = {"baseline": "", "horizon_years": 5, "discount_rate": 0.08,
            "operating_hours_per_year": 4000, "margin_per_part": 0.0, "throughput": ""}
THROUGHPUT = "system/Total Entities Completed"


# ---------------------------------------------------------------- arithmetic
def npv(extra_capex: float, saving: float, horizon_years: float, rate: float) -> float:
    """-extra_capex plus *saving* at the end of each year, discounted at *rate*;
    a fractional horizon counts its last year pro rata."""
    years = int(horizon_years)
    total = sum(saving / (1 + rate) ** t for t in range(1, years + 1))
    return total + (horizon_years - years) * saving / (1 + rate) ** (years + 1) - extra_capex


def payback_years(extra_capex: float, saving: float) -> float | None:
    """Undiscounted. 0 when there is nothing to pay back and the saving is not
    negative; None ("never") when the saving cannot pay."""
    if extra_capex <= 0:
        return 0 if saving >= 0 else None
    return extra_capex / saving if saving > 0 else None


def roi_percent(extra_capex: float, saving: float, horizon_years: float) -> float | None:
    """Net gain over the horizon per unit invested; None without an investment."""
    if extra_capex <= 0:
        return None
    return 100 * (horizon_years * saving - extra_capex) / extra_capex


# ---------------------------------------------------------------- the table
def investment_settings(layout: dict) -> dict:
    """The `investment` section with the defaults filled (all defaults when
    the model has none)."""
    section = layout.get("investment")
    return {**DEFAULTS, **(section if isinstance(section, dict) else {})}


def _per_run(frame: pd.DataFrame, runs) -> np.ndarray:
    """A Run/Value frame as one number per replication, 0 where it has no row."""
    return frame.groupby("Run")["Value"].sum().reindex(runs, fill_value=0.0).to_numpy(dtype=float)


def _yearly(frames: dict, flat: pd.DataFrame, capex_names: set, settings: dict,
            throughput: str):
    """(capex, operating cost per year, parts per year), one value per replication."""
    system = frames["system"]
    runs = sorted(system["Run"].unique())

    def kpi(key):
        return _per_run(flat[flat["KPI"] == key], runs)

    costs = frames["cost_summary"]
    costs = costs[(costs["Kind"] == "fixed") & costs["Item"].isin(capex_names)]
    capex = _per_run(costs.rename(columns={"Amount": "Value"}), runs)
    unit = system.loc[system["Metric"] == "Time Unit", "Value"].iloc[0]
    hours = kpi("system/Simulation Time") * conversion_factor("time", unit, "hours")
    # Nothing observed (an empty model): nothing to scale, not a division by zero.
    scale = np.divide(settings["operating_hours_per_year"], hours,
                      out=np.zeros_like(hours), where=hours > 0)
    return capex, (kpi("system/Total Cost") - capex) * scale, kpi(throughput) * scale


def investment_case(result: ExperimentResult, layout) -> pd.DataFrame:
    """One row per scenario of *result*, the baseline first (COLUMNS).

    Saving and NPV are means over the replications with the scenario
    comparison's interval (paired under common random numbers, Welch's
    otherwise; NaN with one replication). Payback and ROI come from the means
    and are NaN where they do not apply; `Pays Back` is False for "never".
    The baseline's own row compares it with itself: zeros.

    Raises ValueError when the model has no `investment` section, the baseline
    was not run, or no scenario reports the throughput KPI.
    """
    base = api.load_layout(layout)
    if base.get("investment") is None:
        raise ValueError("the model has no investment section")
    settings = investment_settings(base)
    baseline = settings["baseline"] or BASELINE
    if baseline not in result.results:
        raise ValueError(f"investment baseline scenario {baseline!r} was not run")
    throughput = settings["throughput"] or THROUGHPUT

    horizon, margin = settings["horizon_years"], settings["margin_per_part"]
    annuity = npv(0.0, 1.0, horizon, settings["discount_rate"])     # NPV is linear in the saving
    overrides = {s.get("name"): s.get("overrides", []) for s in scenarios_of(base)}
    capex, value, rows, known = {}, {}, {}, False
    for name, frames in result.results.items():
        flat = flatten_kpis(frames)
        known = known or bool((flat["KPI"] == throughput).any())
        # Which amounts are capex is read from the scenario's own flattened
        # layout: a library asset's clone is "<instance>/<name>".
        flagged = {c.get("name") for c in flatten(apply_overrides(base, overrides.get(name)))
                   .get("fixed_costs") or [] if c.get("capex")}
        capex[name], cost, parts = _yearly(frames, flat, flagged, settings, throughput)
        value[name] = margin * parts - cost         # what a year is worth, up to a constant
        rows[name] = {"Scenario": name, "Capex": capex[name].mean(),
                      "Operating Cost per Year": cost.mean(), "Parts per Year": parts.mean()}
    if not known:
        raise ValueError(f"unknown investment throughput KPI: {throughput!r}")

    for name, row in rows.items():
        # A scenario against itself is paired whatever the seeds were.
        diff_ci = paired_diff_ci if result.crn or name == baseline else welch_diff_ci
        saving = diff_ci(value[name], value[baseline])
        worth = diff_ci(annuity * value[name] - capex[name],
                        annuity * value[baseline] - capex[baseline])
        extra = row["Capex"] - rows[baseline]["Capex"]
        payback = payback_years(extra, saving["mean"])
        roi = roi_percent(extra, saving["mean"], horizon)
        row.update({"Extra Capex": extra, "Saving per Year": saving["mean"],
                    "Saving CI Low": saving["lb"], "Saving CI High": saving["ub"],
                    "Payback Years": math.nan if payback is None else payback,
                    "Pays Back": payback is not None, "NPV": worth["mean"],
                    "NPV CI Low": worth["lb"], "NPV CI High": worth["ub"],
                    "ROI %": math.nan if roi is None else roi})
    ordered = [rows[baseline]] + [row for name, row in rows.items() if name != baseline]
    return pd.DataFrame(ordered, columns=COLUMNS)


# ---------------------------------------------------------------- validation
def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and abs(value) <= sys.float_info.max        # an int too large for a float is none (#373)


def validate_investment(layout: dict, problems: list) -> None:
    """The `investment` section (a `core/sections.py` hook); nothing without one."""
    section = layout.get("investment")
    if section is None:
        return

    def bad(code, field, text, hint):
        problems.append(problem(code, f"investment: {text}", f"/investment/{field}".rstrip("/"),
                                hint=hint))

    if not isinstance(section, dict):
        bad("E_INVESTMENT", "", "must be an object", "{\"baseline\": \"\", \"horizon_years\": 5}")
        return
    settings = investment_settings(layout)
    scenarios = [s for s in layout.get("scenarios") or [] if isinstance(s, dict)]
    names = {BASELINE, *(s.get("name") for s in scenarios)}
    baseline = settings["baseline"] or BASELINE
    if not isinstance(baseline, str) or baseline not in names:
        bad("E_INVESTMENT", "baseline", f"unknown baseline scenario '{baseline}'",
            "a scenario name, or \"\" for the model as saved")
    for field, ok, rule in (("horizon_years", lambda v: v > 0, "> 0"),
                            ("operating_hours_per_year", lambda v: v > 0, "> 0"),
                            ("discount_rate", lambda v: v >= 0, ">= 0"),
                            ("margin_per_part", lambda v: True, "")):
        if not (_number(settings[field]) and ok(settings[field])):
            bad("E_INVESTMENT", field, f"{field} must be a number {rule}".rstrip(),
                f"default {DEFAULTS[field]}")
    key = settings["throughput"]
    kind, _, rest = key.partition("/") if isinstance(key, str) else ("", "", "")
    if key != "" and not (kind in KPI_PREFIXES and rest):
        bad("E_INVESTMENT", "throughput", f"unknown throughput KPI '{key}'",
            f"a KPI key counting good parts, e.g. \"user/Good parts\"; \"\" is {THROUGHPUT}")

    if not (any(isinstance(c, dict) and c.get("capex") for c in layout.get("fixed_costs") or [])
            or any(str(o.get("path", "")).startswith("fixed_costs.")
                   and str(o.get("path")).endswith(".capex") and o.get("value")
                   for s in scenarios for o in s.get("overrides") or [] if isinstance(o, dict))):
        bad("W_INVESTMENT_NO_CAPEX", "", "no fixed cost is flagged capex",
            "set \"capex\": true on the fixed costs that are the investment")
    if all(name == baseline for name in names):
        bad("W_INVESTMENT_ONE_SCENARIO", "", "there is no scenario to compare with the baseline",
            "add a scenario")


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    """`naga invest model.json`: run every scenario and print the investment table."""
    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga invest",
        description="Run every scenario and print payback, NPV and ROI against the baseline "
                    "of the model's investment section.")
    parser.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    parser.add_argument("--replications", type=int, default=None,
                        help="default: the model's own 'simulation.replications'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--csv", help="write the table here as CSV")
    args = parser.parse_args(argv)

    layout = api.load_layout(args.model)
    if layout.get("investment") is None:
        raise ValueError("the model has no investment section")      # before the runs
    table = investment_case(run_scenarios(layout, replications=args.replications,
                                          seed=args.seed), layout)
    print(table.to_string(index=False))
    if args.csv:
        table.to_csv(args.csv, index=False)
        print(f"Investment table written to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
