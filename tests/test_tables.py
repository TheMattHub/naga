"""Lookup tables (PHASE4 A1): lookup semantics, validation codes, the CSV
round trip, and one hand-computed engine run.

Exact tests only -- a table is deterministic arithmetic, so nothing here needs
a statistical oracle (ARCHITECTURE D48).
"""
import json
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core import schema as S
from nagasim.core.tables import (
    TableError, build_table, build_tables, table_csv, table_from_csv,
)

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"

# 1-D, string keys, no default.
PRICE = {"name": "Price", "keys": ["A", "B"], "values": [10, 20]}
# 1-D, numeric keys out of order, both interpolated modes below.
BAND = {"name": "Band", "keys": [20, 0, 10], "values": [2.0, 0.0, 1.0]}
# 2-D, the shape the example model uses.
GRID = {"name": "Grid", "keys": ["WIDGET", "GADGET"], "columns": ["Cut", "Pack"],
        "values": [[3, 2], [4, 6]]}


@pytest.mark.parametrize("entry, keys, expected", [
    # exact, 1-D and 2-D
    (PRICE, ("A",), 10.0),
    (PRICE, ("B",), 20.0),
    (GRID, ("WIDGET", "Pack"), 2.0),
    (GRID, ("GADGET", "Cut"), 4.0),
    # a default stands in for a missing exact key
    ({**PRICE, "default": 7.5}, ("Z",), 7.5),
    # step: the largest key <= x, sorted at build time, clamped at both ends
    ({**BAND, "key_mode": "step"}, (10,), 1.0),
    ({**BAND, "key_mode": "step"}, (19.9,), 1.0),
    ({**BAND, "key_mode": "step"}, (-5,), 0.0),
    ({**BAND, "key_mode": "step"}, (999,), 2.0),
    # linear: interpolate between the neighbours, clamp outside
    ({**BAND, "key_mode": "linear"}, (5,), 0.5),
    ({**BAND, "key_mode": "linear"}, (15,), 1.5),
    ({**BAND, "key_mode": "linear"}, (-1,), 0.0),
    ({**BAND, "key_mode": "linear"}, (100,), 2.0),
    # numeric exact keys match whatever numeric type the expression produced
    ({"name": "N", "keys": [1, 2], "values": [5, 6]}, (2.0,), 6.0),
    # a 2-D table may interpolate one dimension and match the other exactly
    ({"name": "Mixed", "keys": [0, 10], "columns": ["a", "b"], "key_mode": "linear",
      "values": [[0, 100], [10, 200]]}, (5, "b"), 150.0),
])
def test_lookup_semantics(entry, keys, expected):
    assert build_table(entry).lookup(*keys) == pytest.approx(expected)


def test_a_missing_key_without_a_default_is_a_model_error_naming_table_and_key():
    with pytest.raises(TableError) as excinfo:
        build_table(PRICE).lookup("Z")
    assert "Price" in str(excinfo.value) and "'Z'" in str(excinfo.value)


def test_the_wrong_number_of_keys_is_refused_at_lookup_time():
    with pytest.raises(TableError, match="takes 2 key"):
        build_table(GRID).lookup("WIDGET")


def test_an_interpolated_dimension_refuses_a_non_numeric_key():
    with pytest.raises(TableError, match="numeric key"):
        build_table({**BAND, "key_mode": "linear"}).lookup("A")


def test_build_tables_keys_every_table_by_name():
    built = build_tables([PRICE, GRID])
    assert sorted(built) == ["Grid", "Price"]
    assert built["Grid"].dims == 2 and built["Price"].dims == 1


# -- validation ---------------------------------------------------------------

def _layout(tables, blocks=()):
    return {"schema_version": S.CURRENT_VERSION, "blocks": list(blocks), "links": [],
            "resources": [], "resource_sets": [], "entities": [], "variables": [],
            "schedules": [], "failures": [], "tables": tables,
            "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}


def _delay(expr):
    return [{"name": "delay_1", "type": "DELAY", "position": [0, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": expr}}}]


@pytest.mark.parametrize("tables, blocks, code", [
    # invalid name
    ([{**PRICE, "name": ""}], (), "E_TABLE_NAME"),
    ([PRICE, PRICE], (), "E_TABLE_NAME"),
    # malformed: ragged rows, a key/value mismatch, a duplicate key
    ([{**GRID, "values": [[3, 2], [4]]}], (), "E_TABLE_SHAPE"),
    ([{**PRICE, "values": [1]}], (), "E_TABLE_SHAPE"),
    ([{**PRICE, "keys": ["A", "A"]}], (), "E_TABLE_SHAPE"),
    # non-numeric values and defaults
    ([{**PRICE, "values": [1, "two"]}], (), "E_TABLE_VALUE"),
    ([{**PRICE, "default": "cheap"}], (), "E_TABLE_VALUE"),
    # lookup modes
    ([{**PRICE, "key_mode": "nearest"}], (), "E_TABLE_MODE"),
    ([{**PRICE, "key_mode": "linear"}], (), "E_TABLE_MODE"),   # string keys
    # expression references
    ([PRICE], _delay("TABLE('Nope', 1)"), "E_TABLE_UNKNOWN"),
    ([GRID], _delay("TABLE('Grid', entity.e_type)"), "E_TABLE_KEYS"),
])
def test_validation_codes(tables, blocks, code):
    problems = S.validate_layout_structured(_layout(tables, blocks))
    assert code in {p.code for p in problems}, [p.message for p in problems]
    for problem in problems:
        assert problem.path and problem.hint


def test_a_correct_table_and_reference_validate_clean():
    layout = _layout([GRID], _delay("TABLE('Grid', entity.e_type, 'Cut')"))
    assert S.validate_layout_structured(layout) == []


# -- CSV ----------------------------------------------------------------------

@pytest.mark.parametrize("entry", [PRICE, GRID, {**BAND, "key_mode": "step"}])
def test_csv_round_trip(entry):
    back = table_from_csv(table_csv(entry), entry["name"],
                          key_mode=entry.get("key_mode"), default=entry.get("default"))
    assert back["keys"] == entry["keys"]
    assert back.get("columns") == entry.get("columns")
    assert build_table(back)._values == build_table(entry)._values


def test_csv_import_refuses_a_non_numeric_cell():
    with pytest.raises(TableError, match="not a number"):
        table_from_csv("key,value\nA,one\n", "Price")


# -- engine -------------------------------------------------------------------

def test_the_example_model_runs_the_times_its_table_declares():
    """`table_process_times.json`: a WIDGET (cut 3 + pack 2) at t=0 and a GADGET
    (cut 4 + pack 6) at t=100, neither ever queueing -- so the throughput times
    are 5 and 10, and every KPI below is exact rather than estimated."""
    results = api.run_model(str(MODELS_DIR / "table_process_times.json"),
                            replications=1, seed=0)
    system = results["system"].set_index("Metric")["Value"]
    assert float(system["Total Entities Completed"]) == 2
    assert float(system["Avg Throughput Time"]) == 7.5
    assert float(system["Simulation Time"]) == 110.0
    times = dict(zip(results["entity_log"]["Type"], results["entity_log"]["Throughput Time"]))
    assert times == {"WIDGET": 5.0, "GADGET": 10.0}


def test_a_missing_table_entry_stops_the_run_with_a_message_naming_the_table():
    layout = json.loads((MODELS_DIR / "table_process_times.json").read_text())
    layout["tables"][0]["keys"] = ["WIDGET", "SPROCKET"]     # no row for GADGET
    with pytest.raises(Exception, match="ProcessTime"):
        api.run_model(layout, replications=1, seed=0)
