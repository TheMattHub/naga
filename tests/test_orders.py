"""Orders and due dates (PHASE4 A3).

Two levels, as in tests/test_tables.py and tests/test_sequences.py: exact
hand-computed behaviour for the mechanics (release, per-order completion,
lateness, service level, warm-up and truncation, validation codes) and one
analytical check for the forecast's "will order X be late".
"""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from nagasim import experiments, forecast as forecast_mod
from nagasim.api import run_model
from nagasim.core import orders as orders_mod
from nagasim.core.schema import validate_model_structured

from conftest import chain, create, delay, dispose, layout, process

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
ORDER_SHOP = MODELS_DIR / "order_due_dates.json"


# ---------------------------------------------------------------- the example
def test_the_order_shop_is_exact_end_to_end():
    """Three orders released at t=0 through Cut(FIFO, 1) then Pack(FIFO, 1),
    every process time a constant, so the whole timeline is arithmetic:

        cut   WO-1 0-2, WO-2a 2-4, WO-2b 4-6, WO-3 6-9
        pack  WO-1 2-3, WO-2a 4-5, WO-2b 6-7, WO-3 9-11

    so WO-1 completes at 3 (due 10), WO-2 at 7 -- its LAST entity (due 20) --
    and WO-3 at 11 against a due date of 8: late by exactly 3.
    """
    results = run_model(str(ORDER_SHOP), replications=1, seed=0)
    orders = results["order_summary"].set_index("Order")

    assert list(orders["Completion Time"]) == [3.0, 7.0, 11.0]
    assert list(orders["Lateness"]) == [-7.0, -13.0, 3.0]
    assert list(orders["Tardiness"]) == [0.0, 0.0, 3.0]
    assert list(orders["Late"]) == [0, 0, 1]
    assert list(orders["Status"]) == ["complete"] * 3
    assert orders.loc["WO-2", "Created"] == 2 and orders.loc["WO-2", "Completed"] == 2

    due = results["due_date_summary"].set_index("Entity Type")
    # 4 entities, one late -> 3/4 on time; lateness per entity is
    # -7, -15, -13, +3, so the model-wide mean is exactly -8.
    assert due.loc["(all)", "Service Level"] == 0.75
    assert due.loc["(all)", "Avg Lateness"] == -8.0
    assert due.loc["(all)", "Avg Tardiness"] == 0.75
    assert due.loc["(all)", "Max Tardiness"] == 3.0
    assert due.loc["WIDGET", "Service Level"] == 1.0
    assert due.loc["GADGET", "Service Level"] == 0.0

    # Both frames flatten into KPI keys a scenario comparison can name.
    flat = experiments.flatten_kpis(results).set_index("KPI")["Value"]
    assert flat["order/WO-3/Tardiness"] == 3.0
    assert flat["duedate/(all)/Service Level"] == 0.75


def test_the_example_model_validates_clean():
    problems = validate_model_structured(json.loads(ORDER_SHOP.read_text()))
    assert [p.code for p in problems] == []


def test_no_orders_leaves_both_frames_empty_with_their_columns(simple_model_path):
    """The frame-shape promise: a model with no orders and no due date gets
    both new frames empty -- so no existing model's KPI keys, workbook sheets
    or golden hash move."""
    from nagasim.core.outputs import _DUE_DATE_SUMMARY_COLS, _ORDER_SUMMARY_COLS

    results = run_model(simple_model_path, replications=1, run_length=20)
    assert results["order_summary"].empty and results["due_date_summary"].empty
    assert list(results["order_summary"].columns) == _ORDER_SUMMARY_COLS
    assert list(results["due_date_summary"].columns) == _DUE_DATE_SUMMARY_COLS
    assert experiments.flatten_kpis(results)["KPI"].str.startswith(("order/", "duedate/")).sum() == 0


# ---------------------------------------------------------------- due dates without orders
def _assign(name="assign_1", target="entity.due_date", expr="now + 5"):
    return {"name": name, "description": name, "type": "ASSIGN", "position": [1, 0],
            "params": {"assignments": [{"target": target, "expr": expr}]}}


def test_an_assign_gives_a_make_to_stock_model_the_same_due_date_kpis(tmp_path):
    """No orders at all: three entities arrive at 0, 1 and 2, each given a due
    date 5 later, and each delayed 6 -- so every one is late by exactly 1."""
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}), _assign(),
              delay(value=6.0), dispose()]
    model = layout(blocks, chain(*blocks))
    results = run_model(model, replications=1)

    assert results["order_summary"].empty          # no orders declared
    due = results["due_date_summary"].set_index("Entity Type")
    assert due.loc["(all)", "Completed"] == 3
    assert due.loc["(all)", "Service Level"] == 0.0
    assert due.loc["(all)", "Avg Lateness"] == 1.0
    assert due.loc["(all)", "Avg Tardiness"] == 1.0


# ---------------------------------------------------------------- warm-up and truncation
def _two_order_model(due_1=5.0, due_2=100.0, quantity_2=1):
    """One DELAY of 4, orders released at 0 and at 20, so a warm-up between
    the two completions splits them cleanly."""
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
              delay(value=4.0), dispose()]
    return layout(blocks, chain(*blocks), orders=[
        {"id": "EARLY", "release": 0, "due": due_1, "quantity": 1},
        {"id": "LATE", "release": 20, "due": due_2, "quantity": quantity_2},
    ])


def test_warmup_and_truncation_are_explicit_in_the_status_column():
    """An order completed BEFORE the warm-up end is reported with its real
    numbers but status "warmup"; one still in the system at run end is
    "incomplete" with NaN times, so no unfinished job is silently counted as
    on time. Neither reaches the entity-level due-date sums."""
    # EARLY completes at 4, LATE is released at 20 and completes at 24; a
    # warm-up of 10 discards the first, a run length of 22 truncates the second.
    warm = run_model(_two_order_model(), replications=1, warmup=10.0, run_length=30.0)
    rows = warm["order_summary"].set_index("Order")
    assert rows.loc["EARLY", "Status"] == "warmup"
    assert rows.loc["EARLY", "Completion Time"] == 4.0    # still reported: it is a fact
    assert rows.loc["LATE", "Status"] == "complete"
    # Only the post-warm-up entity feeds the entity-level KPIs.
    assert warm["due_date_summary"].set_index("Entity Type").loc["(all)", "Completed"] == 1

    cut = run_model(_two_order_model(), replications=1, warmup=0.0, run_length=22.0)
    rows = cut["order_summary"].set_index("Order")
    assert rows.loc["LATE", "Status"] == "incomplete"
    assert math.isnan(rows.loc["LATE", "Completion Time"])
    assert math.isnan(rows.loc["LATE", "Lateness"])
    assert rows.loc["LATE", "Created"] == 1 and rows.loc["LATE", "Completed"] == 0


def test_an_order_is_complete_only_when_its_last_entity_is_out():
    """Quantity 2 with a run length that lets exactly one of the two finish."""
    # LATE releases 2 entities at 20; the DELAY is 4 but they share nothing, so
    # both would end at 24 -- cut the run at 21 and neither is out.
    cut = run_model(_two_order_model(quantity_2=2), replications=1, run_length=21.0)
    row = cut["order_summary"].set_index("Order").loc["LATE"]
    assert (row["Created"], row["Completed"], row["Status"]) == (2, 0, "incomplete")


# ---------------------------------------------------------------- release rules
def test_a_source_picks_the_create_block_and_an_untagged_order_uses_the_first():
    """Two CREATE blocks in Orders mode: `source` decides, and an order with
    none goes to the first in layout order (ARCHITECTURE D53)."""
    first = create(name="release_a", arrival_mode="Orders", max_arrivals=0)
    second = create(name="release_b", arrival_mode="Orders", max_arrivals=0)
    end = dispose()
    model = layout([first, second, end],
                   [{"source": "release_a", "target": "dispose_1"},
                    {"source": "release_b", "target": "dispose_1"}],
                   orders=[{"id": "TAGGED", "release": 0, "source": "release_b"},
                           {"id": "UNTAGGED", "release": 0}])
    built = orders_mod.build_orders(model)
    assert built["TAGGED"].block == "release_b"
    assert built["UNTAGGED"].block == "release_a"
    assert run_model(model, replications=1)["order_summary"]["Completed"].tolist() == [1, 1]


def test_the_order_carries_its_own_entity_type_priority_and_attributes():
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0, e_type="DEFAULT"),
              delay(value=1.0), dispose()]
    model = layout(blocks, chain(*blocks), entities=[{"name": "WIDGET"}],
                   orders=[{"id": "WO-1", "release": 0, "due": 9, "quantity": 1,
                            "entity_type": "widget", "priority": 2,
                            "attributes": {"customer": "ACME"}}])
    results = run_model(model, replications=1)
    # `entity_type` is matched case-insensitively at validation and upper-cased
    # by Entity.e_type, exactly like a lookup table's string key (DEVLOG #87).
    assert results["due_date_summary"]["Entity Type"].tolist() == ["(all)", "WIDGET"]
    assert not validate_model_structured(model)


# ---------------------------------------------------------------- warm start
def test_a_warm_started_job_counts_against_its_order():
    """A twin's snapshot puts one of an order's two jobs already in a block;
    its order_id/due_date/sequence ride along as ordinary attributes and the
    order's release counter picks it up, so it is not reported unreleased."""
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
              delay(value=3.0), dispose()]
    model = layout(blocks, chain(*blocks),
                   orders=[{"id": "WO-1", "release": 0, "due": 20, "quantity": 1}],
                   initial_state={"entities": [
                       {"block": "delay_1", "count": 1, "age": 2.0,
                        "attributes": {"order_id": "WO-1", "due_date": 20.0}}]})
    row = run_model(model, replications=1)["order_summary"].set_index("Order").loc["WO-1"]
    assert (row["Created"], row["Completed"], row["Status"]) == (2, 2, "complete")


# ---------------------------------------------------------------- validation
_GOOD = {"id": "WO-1", "release": 0, "due": 5, "quantity": 1}


@pytest.mark.parametrize("orders, code", [
    ([_GOOD, dict(_GOOD)], "E_ORDER_ID"),
    ([{"id": "", "release": 0}], "E_ORDER_ID"),
    ([{**_GOOD, "quantity": "two"}], "E_ORDER_FIELD"),
    ([{**_GOOD, "release": -1}], "E_ORDER_FIELD"),
    ([{**_GOOD, "attributes": {"bad name": 1}}], "E_ORDER_FIELD"),
    ([{**_GOOD, "sequence": "Nope"}], "E_SEQUENCE_UNKNOWN"),
    ([{**_GOOD, "entity_type": "Nope"}], "E_ORDER_ENTITY_TYPE"),
    ([{**_GOOD, "source": "delay_1"}], "E_ORDER_SOURCE"),
    ([{**_GOOD, "due": 0, "release": 5}], "W_ORDER_DUE_BEFORE_RELEASE"),
])
def test_validation_codes(orders, code):
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
              delay(value=1.0), dispose()]
    model = layout(blocks, chain(*blocks), entities=[{"name": "WIDGET"}], orders=orders)
    assert code in {p.code for p in validate_model_structured(model)}


def test_orders_with_no_orders_mode_create_and_the_other_way_round_are_warnings():
    blocks = [create(), delay(value=1.0), dispose()]
    unconsumed = layout(blocks, chain(*blocks), orders=[_GOOD])
    assert "W_ORDERS_NOT_CONSUMED" in {p.code for p in validate_model_structured(unconsumed)}

    empty_blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
                    delay(value=1.0), dispose()]
    starved = layout(empty_blocks, chain(*empty_blocks))
    assert "W_CREATE_NO_ORDERS" in {p.code for p in validate_model_structured(starved)}


# ---------------------------------------------------------------- CSV
def test_csv_round_trip_keeps_fields_and_free_attributes():
    section = [{"id": "WO-1", "release": 0.0, "due": 480.0, "quantity": 2,
                "entity_type": "WIDGET", "sequence": "Fast", "priority": 1.0,
                "attributes": {"customer": "ACME"}},
               {"id": "WO-2", "release": 60.0, "due": 500.0, "quantity": 1}]
    back = orders_mod.orders_from_csv(orders_mod.orders_csv(section))
    assert back[0]["id"] == "WO-1" and back[0]["quantity"] == 2
    assert back[0]["attributes"] == {"customer": "ACME"}
    assert back[1] == {"id": "WO-2", "release": 60.0, "due": 500.0, "quantity": 1}


def test_a_csv_without_an_id_column_is_refused():
    with pytest.raises(orders_mod.OrderError):
        orders_mod.orders_from_csv("release,due\n0,5\n")


# ---------------------------------------------------------------- forecast
def test_the_forecast_late_probability_matches_the_analytical_one():
    """One order, one exponential operation, no contention: its completion time
    IS Exp(mean 10), so P(late) against a due date of 10 is exactly exp(-1).
    The horizon is 20 means long, so truncation contributes ~1e-9 and the
    Wilson interval has to cover the analytical value.

    Replications 400 -> 150 (suite reduction, M3D-5): at seed 0 the Wilson CI
    is [0.306, 0.460] against analytical 0.3679 -- 0.06 clear of each bound,
    about 1.6x the width at 400 reps (CI width ~ 1/sqrt(n)). The median-completion
    check keeps more margin than it needs (5.5% error against a 15% tolerance)
    at this sample size; it starts failing under 150.
    """
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
              process(resource="Machine", distribution="Exponential", value=0.1), dispose()]
    model = layout(blocks, chain(*blocks), resources=[{"name": "Machine", "capacity": 1}],
                   orders=[{"id": "WO-1", "release": 0, "due": 10, "quantity": 1}])

    result = forecast_mod.forecast(model, horizon=200.0, replications=150, seed=0)
    row = result.orders.set_index("Order").loc["WO-1"]
    analytical = math.exp(-1.0)
    assert row["Wilson LB"] <= analytical <= row["Wilson UB"]
    assert row["Replications"] == 150
    # The median completion is the empirical median of an Exp(10): 10*ln(2).
    assert row["Median Completion"] == pytest.approx(10.0 * math.log(2.0), rel=0.15)


def test_an_order_that_never_finishes_inside_the_horizon_is_reported_late():
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0),
              delay(value=50.0), dispose()]
    model = layout(blocks, chain(*blocks),
                   orders=[{"id": "WO-1", "release": 0, "due": 5, "quantity": 1}])
    result = forecast_mod.forecast(model, horizon=10.0, replications=5, seed=0)
    row = result.orders.set_index("Order").loc["WO-1"]
    assert row["P(late)"] == 1.0 and row["Completed Reps"] == 0
    assert np.isnan(row["Median Completion"])


# ---------------------------------------------------------------- real dates (PHASE4 T10)
def test_iso_release_and_due_dates_become_the_right_model_times():
    """An order may be written in real dates when the model has a calendar;
    the engine still sees base-unit numbers (here: minutes from 06:00 local)."""
    raw = json.loads(ORDER_SHOP.read_text())
    raw.setdefault("simulation", {})["calendar"] = {"start": "2026-01-05T06:00:00",
                                                    "timezone": "Europe/Rome"}
    raw["orders"] = [{"id": "WO-DATED", "release": "2026-01-05T08:00:00",
                      "due": "2026-01-05T14:30:00", "quantity": 1}]
    assert not [p for p in validate_model_structured(raw) if p.severity == "error"]

    from nagasim.core.calendar import model_calendar
    built = orders_mod.build_orders(raw, model_calendar(raw))
    assert built["WO-DATED"].release == 120.0 and built["WO-DATED"].due == 510.0


def test_a_dated_order_without_a_calendar_is_a_coded_problem():
    raw = json.loads(ORDER_SHOP.read_text())
    raw["orders"] = [{"id": "WO-1", "release": 0, "due": "2026-01-05T14:30:00", "quantity": 1}]
    codes = [p.code for p in validate_model_structured(raw) if p.severity == "error"]
    assert codes == ["E_CALENDAR_DATE"]
