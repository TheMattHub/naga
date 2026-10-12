"""Independent oracle for the investment case (automation sub-project 4).

Written from docs/superpowers/specs/2026-10-04-automation-library-design.md
("Investment case") and the plan's "Spec clarifications" only; expected values
are hand derived, never read from the implementation. `nagasim.investment` is
imported INSIDE each test so the file collects before the module exists.

Model used by the table tests (hand derivation, hours, run length 100, no warm-up):
  CREATE every 1 h from t = 0.25; PROCESS on one server (capacity 1); DISPOSE.
  Fixed costs: "Rent" 100 (not capex), "Robot" capex flag, amount 0 in the
  baseline, 100 000 in scenario "Robot". Server cost_busy 40 /h (Robot: 30 /h),
  process time 1.5 h (Robot: 0.5 h). Investment: baseline "" (the saved model),
  horizon 5 y, rate 0.08, 4000 operating hours per year, throughput "".

  Baseline: the server is the bottleneck. Finishes at 0.25 + 1.5 k <= 100 gives
  k = 66 parts (99.25); job 67 runs 99.25..100 (0.75 h inside the window), so
  the server is busy 99 + 0.75 = 99.75 h. Cost 99.75 x 40 = 3 990, Total Cost
  3 990 + Rent 100 = 4 090.
  Robot: arrivals bound the line. Part j (arrival 0.25 + j) finishes at
  0.75 + j <= 100 gives j = 0..99, 100 parts; busy 100 x 0.5 = 50 h, cost
  50 x 30 = 1 500, Total Cost 1 500 + 100 + 100 000 = 101 600.
  (Both Total Cost values were also checked against the existing engine.)

  Window W = 100 h, scale = 4000 / 100 = 40.
  Baseline: Capex 0; Operating Cost per Year = 4 090 x 40 = 163 600; Parts per Year = 66 x 40 = 2 640.
  Robot: Capex 100 000; Operating = (101 600 - 100 000) x 40 = 64 000; Parts = 100 x 40 = 4 000.
  Extra Capex = 100 000; Saving = 163 600 - 64 000 = 99 600 (+ margin x (4 000 - 2 640)).

Assumptions where the spec is silent (stated for the coordinator):
  A1 Blank cells (no payback, no ROI, no interval) are NaN (None also accepted).
  A2 The observed window is "Simulation Time" of the system frame, here the run
     length 100 h because there is no warm-up and the model does not drain.
  A3 Throughput "" counts "Total Entities Completed".
  A4 Non-capex fixed costs (Rent) stay in the operating cost and are scaled with
     it, as the spec's formula (Total Cost - Capex) says.
  A5 The baseline row reports Extra Capex 0, Saving 0, Payback 0, Pays Back True,
     NPV 0 and a blank ROI (spec: "0 when Extra capex <= 0 and Saving >= 0").
  A6 Scenarios override the capex amount with a path ("fixed_costs.Robot.amount")
     because the override grammar edits existing entries only.
  A7 "Pays Back" is a bool-like cell compared with `is True` / `is False` after bool().
"""
import copy
import math

import pytest

from conftest import chain, create, dispose, layout, process
from nagasim import experiments

HORIZON, RATE = 5, 0.08
ANNUITY = sum(1 / (1 + RATE) ** t for t in range(1, HORIZON + 1))


def _blank(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def _model(unit="hours", k=1.0, capex=True, investment=True, margin=0.0, robot_busy=30.0):
    """k scales every time (1 for hours, 60 for minutes); costs stay per hour."""
    blocks = [create(distribution="Constant", dist_params={"value": 1.0 * k}, start_time=0.25 * k),
              process(value=1.5 * k), dispose()]
    robot = {"name": "Robot", "category": "Capital", "amount": 0}
    if capex:
        robot["capex"] = True
    doc = layout(blocks, chain(*blocks))
    doc["resources"] = [{"name": "Server", "capacity": 1, "cost_busy": 40.0,
                         "cost_time_unit": "hours"}]
    doc["fixed_costs"] = [{"name": "Rent", "category": "Site", "amount": 100}, robot]
    doc["simulation"] = {"time_limit": 100 * k, "warmup": 0, "replications": 1, "time_unit": unit}
    doc["scenarios"] = [{"name": "Robot", "overrides": [
        {"path": "blocks.process_1.params.dist_params.value", "value": 0.5 * k},
        {"path": "resources.Server.cost_busy", "value": robot_busy},
        {"path": "fixed_costs.Robot.amount", "value": 100000}]}]
    if investment:
        doc["investment"] = {"baseline": "", "horizon_years": HORIZON, "discount_rate": RATE,
                             "operating_hours_per_year": 4000, "margin_per_part": margin,
                             "throughput": ""}
    return doc


def _table(doc):
    from nagasim.investment import investment_case
    result = experiments.run_scenarios(doc, replications=1)
    return investment_case(result, doc).set_index("Scenario")


# ---------------------------------------------------------------- pure arithmetic
def test_npv_without_and_with_discounting():
    """-100 000 + 5 x 30 000 = 50 000 at rate 0; at 8 %: -100 000 + 30 000 x sum_{t=1..5} 1.08^-t."""
    from nagasim.investment import npv
    assert npv(100_000, 30_000, 5, 0.0) == pytest.approx(50_000)
    assert npv(100_000, 30_000, 5, 0.08) == pytest.approx(-100_000 + 30_000 * ANNUITY)
    assert -100_000 + 30_000 * ANNUITY == pytest.approx(19_781.3, abs=0.1)   # 3.99271 x 30 000


def test_fractional_horizon_counts_last_year_pro_rata():
    """2.5 years at 10 %: -10 + 4/1.1 + 4/1.1^2 + 0.5 x 4/1.1^3 (the half year is discounted as year 3)."""
    from nagasim.investment import npv
    assert npv(10, 4, 2.5, 0.1) == pytest.approx(-10 + 4 / 1.1 + 4 / 1.21 + 0.5 * 4 / 1.331)
    # at rate 0 it is simply -10 + 2.5 x 4 = 0
    assert npv(10, 4, 2.5, 0.0) == pytest.approx(0.0)


def test_simple_payback():
    """100 000 / 30 000 = 3.3333 years, undiscounted; a 5 000 capex with 20 000 saving: 0.25."""
    from nagasim.investment import payback_years
    assert payback_years(100_000, 30_000) == pytest.approx(10 / 3)
    assert payback_years(5_000, 20_000) == pytest.approx(0.25)


def test_payback_never_and_zero_cases():
    """Saving <= 0 with capex > 0: never (None, A1); capex <= 0 with saving >= 0: 0 years."""
    from nagasim.investment import payback_years
    assert payback_years(100, 0) is None
    assert payback_years(100, -5) is None
    assert payback_years(0, 10) == 0
    assert payback_years(-50, 10) == 0
    assert payback_years(0, 0) == 0           # capex <= 0 and saving >= 0


def test_roi_and_its_blank_case():
    """100 x (5 x 30 000 - 100 000) / 100 000 = 50 %; 100 x (2 x 10 - 20) / 20 = 0 %; capex <= 0: blank."""
    from nagasim.investment import roi_percent
    assert roi_percent(100_000, 30_000, 5) == pytest.approx(50.0)
    assert roi_percent(20, 10, 2) == pytest.approx(0.0)
    assert roi_percent(100, -10, 5) == pytest.approx(100 * (-50 - 100) / 100)   # -150 %
    assert _blank(roi_percent(0, 10, 5)) and _blank(roi_percent(-1, 10, 5))


# ---------------------------------------------------------------- model level
def test_two_scenario_table_by_hand():
    """Margin 0, see the module docstring: every cell is the hand value."""
    t = _table(_model())
    assert list(t.index) == ["Baseline", "Robot"]
    b, r = t.loc["Baseline"], t.loc["Robot"]

    assert b["Capex"] == pytest.approx(0)
    assert b["Operating Cost per Year"] == pytest.approx(4_090 * 40)       # 163 600
    assert b["Parts per Year"] == pytest.approx(66 * 40)                   # 2 640
    assert b["Extra Capex"] == pytest.approx(0) and b["Saving per Year"] == pytest.approx(0)
    assert b["Payback Years"] == 0 and bool(b["Pays Back"]) is True        # A5
    assert b["NPV"] == pytest.approx(0) and _blank(b["ROI %"])

    assert r["Capex"] == pytest.approx(100_000)
    assert r["Extra Capex"] == pytest.approx(100_000)
    assert r["Operating Cost per Year"] == pytest.approx(1_600 * 40)       # 64 000
    assert r["Parts per Year"] == pytest.approx(100 * 40)                  # 4 000
    assert r["Saving per Year"] == pytest.approx(163_600 - 64_000)         # 99 600
    assert r["Payback Years"] == pytest.approx(100_000 / 99_600)           # 1.004016
    assert bool(r["Pays Back"]) is True
    assert r["NPV"] == pytest.approx(-100_000 + 99_600 * ANNUITY)          # about 297 674
    assert r["ROI %"] == pytest.approx(100 * (5 * 99_600 - 100_000) / 100_000)   # 398.0
    # one replication: no interval (A1)
    for col in ("Saving CI Low", "Saving CI High", "NPV CI Low", "NPV CI High"):
        assert _blank(r[col])


def test_margin_pays_for_the_extra_parts():
    """Margin 10: Saving = 99 600 + 10 x (4 000 - 2 640) = 113 200; payback 100 000 / 113 200;
    NPV = -100 000 + 113 200 x annuity; ROI = 100 x (5 x 113 200 - 100 000) / 100 000 = 466 %.
    Margin 0 gives the 99 600 of the previous test (same capex and parts)."""
    zero = _table(_model(margin=0.0)).loc["Robot"]
    paid = _table(_model(margin=10.0)).loc["Robot"]
    assert zero["Saving per Year"] == pytest.approx(99_600)
    assert paid["Saving per Year"] == pytest.approx(113_200)
    assert paid["Payback Years"] == pytest.approx(100_000 / 113_200)
    assert paid["NPV"] == pytest.approx(-100_000 + 113_200 * ANNUITY)
    assert paid["ROI %"] == pytest.approx(466.0)


def test_scenario_that_costs_more_never_pays_back():
    """Robot busy rate 100 /h: cost 50 h x 100 = 5 000 + Rent 100 = 5 100, operating
    5 100 x 40 = 204 000; Saving = 163 600 - 204 000 = -40 400 <= 0 with capex 100 000 > 0:
    blank payback, Pays Back false; ROI = 100 x (-202 000 - 100 000) / 100 000 = -302 %."""
    r = _table(_model(robot_busy=100.0)).loc["Robot"]
    assert r["Saving per Year"] == pytest.approx(-40_400)
    assert _blank(r["Payback Years"]) and bool(r["Pays Back"]) is False
    assert r["ROI %"] == pytest.approx(-302.0)
    assert r["NPV"] == pytest.approx(-100_000 - 40_400 * ANNUITY)


def test_model_in_minutes_gives_the_same_table():
    """Same model, every time x 60 (window 6 000 min); 4 000 h per year = 240 000 min,
    so the scale is still 240 000 / 6 000 = 40 and every cell equals the hours table."""
    hours = _table(_model("hours", 1))
    minutes = _table(_model("minutes", 60))
    assert list(minutes.index) == list(hours.index)
    for scenario in hours.index:
        for col in ("Capex", "Extra Capex", "Operating Cost per Year", "Parts per Year",
                    "Saving per Year", "Payback Years", "NPV", "ROI %"):
            a, b = hours.loc[scenario, col], minutes.loc[scenario, col]
            assert (_blank(a) and _blank(b)) or a == pytest.approx(b), (scenario, col)


def test_capex_flag_leaves_total_cost_unchanged():
    """Total Cost is 4 090 (baseline) and 101 600 (Robot) with the flag, without it, and
    with the investment section removed: the flag is read by the investment case only."""
    def totals(doc):
        kpis = experiments.run_scenarios(doc, replications=1).kpis()
        kpis = kpis[kpis["KPI"] == "system/Total Cost"]
        return dict(zip(kpis["Scenario"], kpis["Value"]))

    flagged = totals(_model(capex=True))
    assert flagged == pytest.approx({"Baseline": 4_090, "Robot": 101_600})
    assert totals(_model(capex=False)) == pytest.approx(flagged)
    assert totals(_model(capex=False, investment=False)) == pytest.approx(flagged)
