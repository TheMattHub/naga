"""Model settings (D160): changing a model unit never changes behaviour."""
import pytest

from conftest import chain, create, delay, dispose, layout, metric_value
from nagasim import api
from nagasim.core.document import ModelDocument
from nagasim.core.quantities import time_expressions
from nagasim.core.schema.migrate import migrate


def _doc():
    blocks = [create(max_arrivals=10), delay(value=0.5), dispose()]
    return ModelDocument(migrate(layout(blocks, chain(*blocks), simulation={
        "time_limit": 60, "warmup": 6, "replications": 1, "time_unit": "minutes"})))


def test_changing_the_model_time_unit_keeps_behaviour_and_converts_the_run_length():
    doc = _doc()
    params = doc.blocks["delay_1"].params
    doc.set_params("delay_1", {**params, "time_unit": "base"})   # a field added this session
    before = api.run_model(doc.to_layout(), replications=1, seed=0)
    depth = len(doc._undo_stack)
    doc.set_model_units(time_unit="hours", length_unit="kilometers", currency="USD")
    assert len(doc._undo_stack) == depth + 1
    sim = doc.simulation
    assert (sim["time_unit"], sim["length_unit"], sim["currency"]) == ("hours", "kilometers", "USD")
    assert sim["time_limit"] == 1.0 and sim["warmup"] == pytest.approx(0.1)
    assert doc.blocks["delay_1"].params["time_unit"] == "minutes"
    after = api.run_model(doc.to_layout(), replications=1, seed=0)
    assert metric_value(after, "Total Entities Completed") \
        == metric_value(before, "Total Entities Completed")
    assert metric_value(after, "Avg Throughput Time") == pytest.approx(
        metric_value(before, "Avg Throughput Time") / 60, abs=1e-4)
    doc.undo()
    assert doc.simulation["time_unit"] == "minutes" and doc.simulation["time_limit"] == 60


def test_expressions_that_read_time_are_listed_and_returned_by_the_change():
    hold = {"name": "h", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Condition", "conditions": ["TNOW > 480"]}}
    blocks = [create(max_arrivals=1), hold, dispose()]
    lay = migrate(layout(blocks, chain(*blocks), simulation={"time_unit": "minutes"},
                         auxiliaries=[{"name": "a", "expr": "PULSE(1, 10, 5)"},
                                      {"name": "b", "expr": "2 * 3"}]))
    expected = {"/blocks/1/params/conditions/0: TNOW > 480",
                "/auxiliaries/0/expr: PULSE(1, 10, 5)"}
    assert set(time_expressions(lay)) == expected
    doc = ModelDocument(lay)
    assert set(doc.set_model_units(time_unit="hours")) == expected
    assert doc.set_model_units(currency="USD") == []           # no time change: nothing to say


def test_the_same_units_are_a_no_op():
    doc = _doc()
    depth = len(doc._undo_stack)
    doc.set_model_units(time_unit="minutes")
    assert len(doc._undo_stack) == depth


def test_a_flow_threshold_keeps_its_rate_through_a_model_unit_change():
    """#689: a threshold value is per the flow's rate_time_unit. A missing one is pinned
    to the old model unit, an explicit one is kept, and the values stay as written."""
    blocks = [create(max_arrivals=1), dispose()]
    up = {"value": 30, "direction": "rising", "signal": "high"}
    doc = ModelDocument(migrate(layout(
        blocks, chain(*blocks), simulation={"time_unit": "hours", "time_limit": 10},
        levels=[{"name": "S", "initial": 0}],
        flows=[{"name": "a", "rate": "60", "to": "S", "thresholds": [up]},
               {"name": "b", "rate": "60", "to": "S", "rate_time_unit": "minutes",
                "thresholds": [up]}])))
    doc.set_model_units(time_unit="days")
    assert [(f["rate_time_unit"], f["rate"], f["thresholds"][0]["value"])
            for f in doc.flows] == [("hours", "60", 30), ("minutes", "60", 30)]
