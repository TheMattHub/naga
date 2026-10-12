"""The investment case (D172-D173): payback, NPV and ROI of each scenario
against a baseline, the `investment` section and the `capex` flag."""
import json

import numpy as np
import pandas as pd
import pytest

from conftest import chain, create, dispose, layout, problem_codes, process


def test_npv_payback_roi_by_hand():
    from nagasim.investment import npv, payback_years, roi_percent
    # 100 000 extra capex, 30 000 saved per year, 5 years.
    assert payback_years(100_000, 30_000) == pytest.approx(100_000 / 30_000)
    assert roi_percent(100_000, 30_000, 5) == pytest.approx(50.0)
    assert npv(100_000, 30_000, 5, 0.0) == pytest.approx(50_000)
    expected = -100_000 + sum(30_000 / 1.08 ** t for t in range(1, 6))
    assert npv(100_000, 30_000, 5, 0.08) == pytest.approx(expected)


def test_fractional_horizon_counts_its_last_year_pro_rata():
    from nagasim.investment import npv
    expected = -10 + 4 / 1.1 + 4 / 1.1 ** 2 + 0.5 * 4 / 1.1 ** 3
    assert npv(10, 4, 2.5, 0.1) == pytest.approx(expected)


def test_edges():
    from nagasim.investment import payback_years, roi_percent
    assert payback_years(100, 0) is None and payback_years(100, -5) is None   # never
    assert payback_years(0, 10) == 0 and payback_years(-50, 10) == 0
    assert payback_years(-50, -10) is None
    assert roi_percent(0, 10, 5) is None and roi_percent(-1, 10, 5) is None


# ---------------------------------------------------------------- the model
# A part every 2 minutes from t=0, 1 minute on Machine, 2 minutes with the
# Operator (30 per hour, busy or idle). Part k leaves at 2k + 3, so a run of
# 600.5 minutes (W = 600.5 / 60 h) completes k = 0..298: 299 parts, and costs
# 30 x W. Scaled to 4000 h: 120 000 a year and 299 x 4000 / W parts.
_ROBOT = [{"path": "resources.Operator.cost_busy", "value": 6.0},
          {"path": "resources.Operator.cost_idle", "value": 6.0},
          {"path": "fixed_costs.Robot cell.amount", "value": 50_000}]
W = 600.5 / 60


def line(per_hour: float = 1.0, **investment) -> dict:
    """*per_hour* 60 writes the same line in hours instead of minutes."""
    blocks = [create(dist_params={"value": 2.0 / per_hour}),
              process("process_1", "Machine", 1.0 / per_hour),
              process("process_2", "Operator", 2.0 / per_hour), dispose()]
    # "Fast robot": a part a minute, 1 minute at each station; part k leaves at
    # k + 2, so 599 parts.
    fast = _ROBOT + [{"path": f"blocks.{name}.params.dist_params.value", "value": 1.0 / per_hour}
                     for name in ("create_1", "process_2")]
    doc = layout(
        blocks, chain(*blocks),
        resources=[{"name": "Machine", "capacity": 1},
                   {"name": "Operator", "capacity": 1, "cost_busy": 30.0, "cost_idle": 30.0}],
        fixed_costs=[{"name": "Robot cell", "category": "Capital", "amount": 0, "capex": True}],
        scenarios=[{"name": "Robot", "overrides": _ROBOT}, {"name": "Fast robot", "overrides": fast}],
        investment=investment)
    doc["simulation"].update(time_limit=600.5 / per_hour,
                             time_unit="hours" if per_hour == 60 else "minutes")
    return doc


def table(doc, replications=1, **kwargs) -> pd.DataFrame:
    from nagasim.experiments import run_scenarios
    from nagasim.investment import investment_case
    return investment_case(run_scenarios(doc, replications=replications, **kwargs), doc)


def row(frame, scenario) -> pd.Series:
    return frame[frame["Scenario"] == scenario].iloc[0]


def test_the_table_equals_the_hand_values():
    from nagasim.investment import COLUMNS, npv
    frame = table(line())
    assert list(frame.columns) == COLUMNS
    assert list(frame["Scenario"]) == ["Baseline", "Robot", "Fast robot"]      # baseline first
    assert frame["Pays Back"].dtype == bool

    base, robot = row(frame, "Baseline"), row(frame, "Robot")
    assert base["Capex"] == 0 and base["Operating Cost per Year"] == pytest.approx(120_000)
    assert base["Parts per Year"] == pytest.approx(299 * 4000 / W)
    assert base["Extra Capex"] == 0 and base["Saving per Year"] == 0 and base["NPV"] == 0

    assert robot["Capex"] == robot["Extra Capex"] == 50_000
    assert robot["Operating Cost per Year"] == pytest.approx(24_000)      # 6 x W, scaled
    assert robot["Parts per Year"] == pytest.approx(299 * 4000 / W)
    assert robot["Saving per Year"] == pytest.approx(96_000)
    assert robot["Payback Years"] == pytest.approx(50_000 / 96_000) and robot["Pays Back"]
    assert robot["NPV"] == pytest.approx(-50_000 + sum(96_000 / 1.08 ** t for t in range(1, 6)))
    assert robot["NPV"] == pytest.approx(npv(50_000, 96_000, 5, 0.08))
    assert robot["ROI %"] == pytest.approx(100 * (5 * 96_000 - 50_000) / 50_000)
    # One replication: no interval (Review Focus 5).
    assert frame[["Saving CI Low", "Saving CI High", "NPV CI Low", "NPV CI High"]].isna().all(None)


def test_the_margin_values_the_extra_parts_of_a_faster_scenario():
    extra_parts = (599 - 299) * 4000 / W
    plain, valued = table(line()), table(line(margin_per_part=12.0, horizon_years=2.5,
                                              discount_rate=0.0))
    assert row(plain, "Fast robot")["Parts per Year"] == pytest.approx(599 * 4000 / W)
    assert row(plain, "Fast robot")["Saving per Year"] == pytest.approx(96_000)
    fast = row(valued, "Fast robot")
    assert fast["Saving per Year"] == pytest.approx(96_000 + 12 * extra_parts)
    assert fast["NPV"] == pytest.approx(-50_000 + 2.5 * fast["Saving per Year"])
    assert row(valued, "Robot")["Saving per Year"] == pytest.approx(96_000)   # no extra parts


def test_a_named_baseline_and_a_throughput_key():
    """Against "Robot" the saved model costs 96 000 a year more and saves the
    capex: nothing to pay back, and never worth it."""
    frame = table(line(baseline="Robot", throughput="entity/DEFAULT/Total Completed"))
    assert list(frame["Scenario"]) == ["Robot", "Baseline", "Fast robot"]
    saved = row(frame, "Baseline")
    assert saved["Extra Capex"] == -50_000 and saved["Saving per Year"] == pytest.approx(-96_000)
    assert np.isnan(saved["Payback Years"]) and not saved["Pays Back"] and np.isnan(saved["ROI %"])
    assert saved["Parts per Year"] == pytest.approx(299 * 4000 / W)


def test_each_scenario_is_scaled_by_its_own_window():
    """Review Focus 1: "Long" runs twice as long, so its total cost doubles
    and its yearly cost does not."""
    doc = line()
    doc["scenarios"].append({"name": "Long", "overrides": _ROBOT + [
        {"path": "simulation.time_limit", "value": 1200.5}]})
    long = row(table(doc), "Long")
    assert long["Operating Cost per Year"] == pytest.approx(24_000)
    assert long["Parts per Year"] == pytest.approx(599 * 4000 / (1200.5 / 60))   # 2k + 3 <= 1200.5


def test_an_empty_model_still_renders():
    """Review Focus 2: no cost, no parts and no observed time."""
    frame = table(layout([], [], investment={}))
    only = frame.iloc[0]
    assert len(frame) == 1 and only["Scenario"] == "Baseline"
    assert only["Operating Cost per Year"] == 0 and only["Parts per Year"] == 0
    assert only["Payback Years"] == 0 and only["Pays Back"] and np.isnan(only["ROI %"])


def test_minutes_and_hours_give_the_same_table():
    """Review Focus 3."""
    pd.testing.assert_frame_equal(table(line()), table(line(per_hour=60)), rtol=1e-9)


def _stochastic() -> dict:
    doc = line()
    for block in doc["blocks"][1:3]:
        block["params"].update(distribution="Exponential", dist_params={"rate": 1.0})
    doc["resources"][1]["cost_idle"] = 0.0          # the cost follows the busy time
    doc["scenarios"] = [{"name": "Robot", "overrides": [_ROBOT[0], _ROBOT[2]]}]
    return doc


@pytest.mark.parametrize("crn", [True, False])
def test_the_intervals_are_the_comparison_ones(crn):
    """Saving and NPV per replication, recomputed here from the system frames,
    with the paired interval under common random numbers and Welch's without."""
    from nagasim.core.stats import paired_diff_ci, welch_diff_ci
    from nagasim.experiments import run_scenarios
    from nagasim.investment import investment_case, npv

    doc = _stochastic()
    result = run_scenarios(doc, replications=4, crn=crn)

    def yearly_cost(scenario, capex):
        system = result.results[scenario]["system"]
        cost = system[system["Metric"] == "Total Cost"]["Value"].to_numpy(dtype=float)
        return (cost - capex) * 4000 / W

    base, robot = yearly_cost("Baseline", 0), yearly_cost("Robot", 50_000)
    assert base.std() > 0
    saving = (paired_diff_ci if crn else welch_diff_ci)(base, robot)
    found = row(investment_case(result, doc), "Robot")
    assert found["Saving per Year"] == pytest.approx(saving["mean"])
    assert found["Saving CI Low"] == pytest.approx(saving["lb"])
    assert found["Saving CI High"] == pytest.approx(saving["ub"])
    assert found["Saving CI Low"] < found["Saving per Year"] < found["Saving CI High"]
    # NPV is linear in the saving: the same interval through the same formula.
    for column, value in (("NPV", "mean"), ("NPV CI Low", "lb"), ("NPV CI High", "ub")):
        assert found[column] == pytest.approx(npv(50_000, saving[value], 5, 0.08))


def test_scoped_capex_counts_once_per_instance():
    """Review Focus 4: an asset placed twice clones its capex twice."""
    def instance(name):
        return {"name": name, "description": name, "type": "SUBMODEL", "position": [0, 0],
                "params": {"definition": "Cell", "arguments": {}}}

    doc = layout([instance("a"), instance("b")], [], investment={},
                 fixed_costs=[{"name": "Arm", "amount": 700, "capex": True},
                              {"name": "Rent", "amount": 5}],
                 submodels={"Cell": {"parameters": [], "blocks": [], "links": [],
                                     "scoped": {"fixed_costs": {"Arm": {}}}}})
    assert table(doc).iloc[0]["Capex"] == 1400       # a/Arm + b/Arm; Rent is operating cost


def test_the_capex_flag_changes_no_frame():
    from nagasim.api import run_model
    flagged, plain = line(), line()
    for doc in (flagged, plain):
        doc["fixed_costs"][0]["amount"] = 50_000
        del doc["investment"]
    del plain["fixed_costs"][0]["capex"]
    a, b = (run_model(doc, replications=1) for doc in (flagged, plain))
    assert set(a) == set(b)
    for key in ("system", "cost_summary"):
        pd.testing.assert_frame_equal(a[key], b[key])
    assert float(a["cost_summary"]["Amount"].sum()) == 50_000


def test_no_section_or_no_baseline_is_a_value_error():
    from nagasim.experiments import run_scenarios
    from nagasim.investment import investment_case
    doc = line()
    result = run_scenarios(doc, ["Robot"], replications=1)
    with pytest.raises(ValueError, match="investment"):
        investment_case(result, {k: v for k, v in doc.items() if k != "investment"})
    with pytest.raises(ValueError, match="Fast robot"):
        investment_case(result, {**doc, "investment": {"baseline": "Fast robot"}})
    with pytest.raises(ValueError, match="user/Nope"):
        investment_case(result, {**doc, "investment": {"throughput": "user/Nope"}})


def test_settings_fill_the_defaults():
    from nagasim.investment import investment_settings
    assert investment_settings(line(horizon_years=3)) == {
        "baseline": "", "horizon_years": 3, "discount_rate": 0.08,
        "operating_hours_per_year": 4000, "margin_per_part": 0.0, "throughput": ""}


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("investment", [
    {"baseline": "Nobody"}, {"horizon_years": 0}, {"operating_hours_per_year": 0},
    {"discount_rate": -0.1}, {"throughput": "nonsense"}, {"horizon_years": "five"},
    {"margin_per_part": None}, "yes"])
def test_bad_settings_are_e_investment(investment):
    doc = line()
    doc["investment"] = investment
    assert problem_codes(doc) == {"E_INVESTMENT"}


def test_warnings_for_no_capex_and_nothing_to_compare():
    assert problem_codes(line(), severity=None) == set()
    assert problem_codes(line(baseline="Robot", throughput="user/Good parts"),
                         severity=None) == set()
    no_capex = line()
    del no_capex["fixed_costs"][0]["capex"]
    assert problem_codes(no_capex, severity=None) == {"W_INVESTMENT_NO_CAPEX"}
    alone = line()
    alone["scenarios"] = []
    assert problem_codes(alone, severity=None) == {"W_INVESTMENT_ONE_SCENARIO"}
    plain = line()
    del plain["investment"]             # no section: none of this is checked
    del plain["fixed_costs"][0]["capex"]
    plain["scenarios"] = []
    assert problem_codes(plain, severity=None) == set()


def test_the_section_is_written_only_when_present():
    from nagasim.core.document import ModelDocument
    from nagasim.core.schema import migrate
    plain = line()
    del plain["investment"]
    assert "investment" not in migrate(plain)
    document = ModelDocument(migrate(plain))
    assert "investment" not in document.to_layout()

    document.set_investment({"baseline": "Robot"})
    saved = document.to_layout()
    assert saved["investment"] == {"baseline": "Robot"}
    assert saved["fixed_costs"][0]["capex"] is True
    assert ModelDocument(saved).to_layout()["investment"] == {"baseline": "Robot"}
    document.undo()
    assert "investment" not in document.to_layout()
    document.set_investment({})
    document.set_investment(None)
    assert "investment" not in document.to_layout()


# ---------------------------------------------------------------- CLI, export
def test_naga_invest_prints_and_writes_the_table(tmp_path, capsys):
    from nagasim.cli import main
    model, out = tmp_path / "line.json", tmp_path / "invest.csv"
    model.write_text(json.dumps(line()), encoding="utf-8")
    assert main(["invest", str(model), "--replications", "1", "--csv", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Payback Years" in printed and "Fast robot" in printed
    written = pd.read_csv(out)
    assert list(written["Scenario"]) == ["Baseline", "Robot", "Fast robot"]
    assert written["Extra Capex"][1] == 50_000

    plain = line()
    del plain["investment"]
    model.write_text(json.dumps(plain), encoding="utf-8")
    assert main(["invest", str(model)]) == 2                 # no section: a message, no traceback
    assert "investment" in capsys.readouterr().err


def test_the_scenario_workbook_has_an_investment_sheet(tmp_path):
    from nagasim.cli import main
    model, out = tmp_path / "line.json", tmp_path / "study.xlsx"
    model.write_text(json.dumps(line()), encoding="utf-8")
    assert main(["scenarios", str(model), "--replications", "1", "--out", str(out)]) == 0
    sheets = pd.read_excel(out, sheet_name=None)
    assert list(sheets) == ["Comparison", "Investment"]
    assert list(sheets["Investment"]["Scenario"]) == ["Baseline", "Robot", "Fast robot"]

    plain = line()
    del plain["investment"]
    model.write_text(json.dumps(plain), encoding="utf-8")
    assert main(["scenarios", str(model), "--replications", "1", "--out", str(out)]) == 0
    assert list(pd.read_excel(out, sheet_name=None)) == ["Comparison"]
