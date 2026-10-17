"""EN1 `berth_shore_power` (Energy phase 3, task 4): every scenario validates, shore power cuts
CO2, the hotel loads sit in the published benchmarks, a bigger connection never supplies
less, the investment case pays back, and the four weeks run in under a minute."""
import csv
import time
from pathlib import Path

import pytest

from nagasim import api, experiments, investment
from nagasim.core.schema import validate_layout_structured
from nagasim.core.submodels import flatten
from nagasim.optimize.cli import build_design, parse_factor
from nagasim.optimize.doe import run_design

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
BERTH = MODELS / "berth_shore_power.json"
SHORE = "shore_power_1/Nrg shore"
AUX = "shore_power_1/Nrg auxiliary"
SCENARIOS = ["Shore power, 2 MW", "Shore power, 6 MW", "Shore power, every ship ready"]


@pytest.fixture(scope="module")
def en1():
    """The four scenarios of the model's own four weeks, one seed."""
    layout = api.load_layout(BERTH)
    return layout, experiments.run_scenarios(layout, replications=1, seed=0)


def _flows(result, name):
    return result.results[name]["flow_summary"].set_index("Flow")


def _share(result, name):
    flows = _flows(result, name)
    shore, aux = flows.loc[SHORE, "Total"], flows.loc[AUX, "Total"]
    return shore / (shore + aux)


def test_en1_every_scenario_validates():
    layout = api.load_layout(BERTH)
    names = ["Baseline"] + [s["name"] for s in layout["scenarios"]]
    assert names == ["Baseline"] + SCENARIOS
    overrides = {s["name"]: s["overrides"] for s in layout["scenarios"]}
    for name in names:
        flat = flatten(experiments.apply_overrides(layout, overrides.get(name, [])))
        assert validate_layout_structured(flat) == [], name


def test_en1_shore_power_cuts_co2(en1):
    _, result = en1

    def tonnes(name):
        return result.results[name]["system"].set_index("Metric")["Value"]["Total Impact"] / 1000

    for name in SCENARIOS:
        assert tonnes(name) < tonnes("Baseline"), name


def test_en1_container_hotel_load_in_benchmark():
    with (MODELS / "data" / "berth_calls.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert 55 <= len(rows) <= 65
    container = [float(r["hotel_kw"]) for r in rows if r["ship_type"].startswith("Container")]
    assert 60 <= sum(container) / len(container) <= 3800
    feeder = [float(r["hotel_kw"]) for r in rows if r["ship_type"].startswith("Container feeder")]
    assert feeder and all(300 <= kw <= 360 for kw in feeder)
    assert {r["ship_type"].split(" (")[0] for r in rows} >= {"Container feeder", "Container large",
                                                            "Tanker", "Ro-ro"}


def test_en1_bigger_connection_never_supplies_less(en1):
    _, result = en1
    assert _share(result, "Shore power, 6 MW") >= _share(result, "Shore power, 2 MW")
    assert _share(result, "Shore power, 2 MW") > 0
    assert _flows(result, "Baseline").loc[SHORE, "Total"] == 0


def test_en1_like_for_like_calls(en1):
    """Every scenario serves the same calls: berth energy (shore + auxiliary) is the same,
    only who supplies it differs."""
    _, result = en1
    totals = {n: _flows(result, n).loc[SHORE, "Total"] + _flows(result, n).loc[AUX, "Total"]
              for n in ["Baseline"] + SCENARIOS}
    assert max(totals.values()) == pytest.approx(min(totals.values()), rel=1e-9)


def test_en1_investment_case_has_payback(en1):
    layout, result = en1
    row = investment.investment_case(result, layout).set_index("Scenario").loc["Shore power, 6 MW"]
    assert row["Extra Capex"] > 0 and row["Pays Back"]
    assert 8 < row["Payback Years"] < 16          # the tutorial quotes 12.6 years


def test_en1_doe_connection_and_readiness_raise_the_shore_share():
    """A 3 x 2 design over the connection and the ready share (seed 0): the shore share is the
    shore energy over the berth energy, and neither factor lowers it."""
    layout = api.load_layout(BERTH)
    factors = [parse_factor("blocks.shore_power_1.params.arguments.connection_kw=cat:2000|4000|6000"),
               parse_factor("variables.ready_share.initial=cat:0.6|1.0")]
    responses = ["flow/shore_power_1/Nrg shore/Total", "flow/shore_power_1/Nrg auxiliary/Total"]
    table = run_design(layout, factors, build_design("full:3", factors, 0),
                       responses=responses, replications=1).table
    assert len(table) == 12
    cols = ["blocks.shore_power_1.params.arguments.connection_kw", "variables.ready_share.initial"]
    share = {}
    for key, rows in table.groupby(cols):
        by = rows.set_index("Response")["Mean"]
        share[key] = by[responses[0]] / (by[responses[0]] + by[responses[1]])
    for ready in {k[1] for k in share}:
        series = [share[k] for k in sorted(share) if k[1] == ready]
        assert series == sorted(series)
    for conn in {k[0] for k in share}:
        low, high = sorted(k for k in share if k[0] == conn)
        assert share[high] >= share[low]


def test_en1_runs_under_a_minute():
    start = time.perf_counter()
    api.run_model(BERTH, replications=1, seed=0)
    assert time.perf_counter() - start < 60


def test_en1_ready_ships_at_60_percent_are_nested_in_the_full_set():
    layout = api.load_layout(BERTH)
    draws = [o["attributes"]["ready_u"] for o in layout["orders"]]
    ready60 = {u for u in draws if u < 0.6}
    assert ready60 and ready60 <= {u for u in draws if u < 1.0} == set(draws)


def test_en1_sensitivity_dearer_auxiliary_energy_shortens_the_payback():
    """The tutorial's sensitivity table: at 0.30 EUR/kWh the 2 MW case pays back in about 1.8 years."""
    layout = api.load_layout(BERTH)
    for block in layout["blocks"]:
        if block["name"] == "shore_power_1":
            block["params"]["arguments"]["aux_fuel_price_per_kwh"] = 0.30
    result = experiments.run_scenarios(layout, ["Shore power, 2 MW"], replications=1, seed=0)
    row = investment.investment_case(result, layout).set_index("Scenario").loc["Shore power, 2 MW"]
    assert 1.5 < row["Payback Years"] < 2.2
