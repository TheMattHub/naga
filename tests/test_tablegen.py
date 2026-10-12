"""nagasim.tablegen (PHASE6 W7 Q5, ARCHITECTURE D128): models generated from
Stations/Products/Routings tables. The end-to-end contract (deterministic
throughput/flow-time numbers, CSV vs .xlsx parity, the CLI, the template
writer) is covered by the independent oracle, tests/test_tablegen_oracle.py --
this file covers unit-level behaviour that oracle does not: error messages,
the Exponential process-time path, and the optional Orders/Resources/
Schedules sheets."""
import csv
import json

import pytest

from nagasim import tablegen
from nagasim.core import schema

pytestmark = pytest.mark.filterwarnings("ignore")


def _fatal(problems):
    return [p for p in problems if p.severity != "warning"]


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="ascii") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_tables(tmp_path, *, stations=None, products=None, routings=None,
                  orders=None, resources=None, schedules=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if stations is not None:
        _write_csv(tmp_path / "Stations.csv", tablegen.STATIONS_COLUMNS, stations)
    if products is not None:
        _write_csv(tmp_path / "Products.csv", tablegen.PRODUCTS_COLUMNS, products)
    if routings is not None:
        _write_csv(tmp_path / "Routings.csv", tablegen.ROUTINGS_COLUMNS, routings)
    if orders is not None:
        from nagasim.core.orders import CSV_COLUMNS
        _write_csv(tmp_path / "Orders.csv", list(CSV_COLUMNS), orders)
    if resources is not None:
        _write_csv(tmp_path / "Resources.csv", tablegen.RESOURCES_COLUMNS, resources)
    if schedules is not None:
        _write_csv(tmp_path / "Schedules.csv", tablegen.SCHEDULES_COLUMNS, schedules)
    return str(tmp_path)


_ONE_STATION = [{"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Constant",
                "param1": 2, "param2": "", "node": "", "x": "", "y": ""}]
_ONE_PRODUCT = [{"entity_type": "A", "arrival_distribution": "Constant", "arrival_param1": 5,
                 "max_arrivals": 2, "start_time": 0}]
_ONE_ROUTING = [{"product": "A", "step": 1, "station": "S1", "process_time": ""}]


def test_generate_from_csv_folder_validates_clean(tmp_path):
    source = _write_tables(tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    layout = tablegen.generate(source)
    assert _fatal(schema.validate_model_structured(layout)) == []
    assert {b["name"] for b in layout["blocks"]} >= {"S1", "create_a"}
    assert layout["sequences"] == [{"name": "A", "steps": [
        {"station": "S1", "values": {"process_time": 2.0, "time_scale": 1.0}}]}]


def test_missing_required_table_names_it(tmp_path):
    source = _write_tables(tmp_path, stations=_ONE_STATION, routings=_ONE_ROUTING)
    with pytest.raises(ValueError, match="Products"):
        tablegen.generate(source)


def test_missing_required_column_names_it(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    _write_csv(tmp_path / "Stations.csv", ["name", "distribution", "param1"],
              [{"name": "S1", "distribution": "Constant", "param1": 2}])
    _write_csv(tmp_path / "Products.csv", tablegen.PRODUCTS_COLUMNS, _ONE_PRODUCT)
    _write_csv(tmp_path / "Routings.csv", tablegen.ROUTINGS_COLUMNS, _ONE_ROUTING)
    with pytest.raises(ValueError, match="capacity"):
        tablegen.generate(str(tmp_path))


def test_unknown_station_in_routing_raises_valueerror_naming_it(tmp_path):
    source = _write_tables(
        tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
        routings=[{"product": "A", "step": 1, "station": "S9", "process_time": ""}])
    with pytest.raises(ValueError, match="S9"):
        tablegen.generate(source)


def test_unknown_distribution_raises_valueerror(tmp_path):
    bad_station = [{**_ONE_STATION[0], "distribution": "NotADistribution"}]
    source = _write_tables(tmp_path, stations=bad_station, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    with pytest.raises(ValueError, match="NotADistribution"):
        tablegen.generate(source)


def test_routing_process_time_override_beats_station_default(tmp_path):
    source = _write_tables(
        tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
        routings=[{"product": "A", "step": 1, "station": "S1", "process_time": 9}])
    layout = tablegen.generate(source)
    assert layout["sequences"][0]["steps"][0]["values"]["process_time"] == 9.0


def test_exponential_station_drives_process_time_by_rate(tmp_path):
    """A station whose distribution is Exponential routes the per-visit
    process_time through the PROCESS block's `rate` (an "inverse" ScaleParam,
    module docstring), and a deterministic run resolves to that exact mean."""
    station = [{"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Exponential",
               "param1": 0.5, "param2": "", "node": "", "x": "", "y": ""}]  # mean 1/0.5 = 2
    source = _write_tables(tmp_path, stations=station, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    layout = tablegen.generate(source)
    assert _fatal(schema.validate_model_structured(layout)) == []
    process = next(b for b in layout["blocks"] if b["type"] == "PROCESS")
    assert process["params"]["distribution"] == "Exponential"
    assert process["params"]["dist_params"] == {"rate": "0.5 / entity.time_scale"}

    from nagasim import api
    results = api.run_model(layout, deterministic=True, run_length=100, warmup=0)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(2.0)


def test_other_distribution_keeps_its_own_literal_params(tmp_path):
    """A station distribution with no `scale_params` (a bounded family here --
    Beta's min/max are hard bounds, not scale params, module docstring) keeps
    its own param1/param2 literally when no Routings row overrides it."""
    station = [{"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Beta",
               "param1": 0.0, "param2": 1.0, "node": "", "x": "", "y": ""}]
    source = _write_tables(tmp_path, stations=station, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    layout = tablegen.generate(source)
    process = next(b for b in layout["blocks"] if b["type"] == "PROCESS")
    assert process["params"]["distribution"] == "Beta"
    assert process["params"]["dist_params"] == {"min": 0.0, "max": 1.0}


def test_uniform_station_scales_low_and_high_by_the_visit_factor(tmp_path):
    """Uniform has TWO scale params (low, high): a Routings override rescales
    both by the same per-visit factor, keeping the family, and a deterministic
    run resolves to exactly that overridden mean. `max_arrivals: 1` avoids any
    queueing (a second arrival would wait behind the first at capacity 1,
    which is a real effect of this station but not what this test checks)."""
    station = [{"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Uniform",
               "param1": 1, "param2": 3, "node": "", "x": "", "y": ""}]  # mean 2
    product = [{**_ONE_PRODUCT[0], "max_arrivals": 1}]
    source = _write_tables(
        tmp_path, stations=station, products=product,
        routings=[{"product": "A", "step": 1, "station": "S1", "process_time": 6}])  # factor 3
    layout = tablegen.generate(source)
    process = next(b for b in layout["blocks"] if b["type"] == "PROCESS")
    assert process["params"]["dist_params"] == {
        "low": "1.0 * entity.time_scale", "high": "3.0 * entity.time_scale"}

    from nagasim import api
    results = api.run_model(layout, deterministic=True, run_length=100, warmup=0)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(6.0)


def test_triangular_and_weibull_stations_deterministic_flow_time_matches_overrides(tmp_path):
    """Integration case for W7 Q5 review item 3: one product visits a
    Triangular station and a Weibull station, each with its own
    `process_time` override; a deterministic (D119) run's flow time is
    exactly the sum of the two overridden means -- Triangular's default
    (low=0, high=1, mode=0.5) has mean 0.5, overridden to 5 (factor 10);
    Weibull's default (scale=1, shape=1, i.e. Exponential-shaped) has mean 1,
    overridden to 4 (factor 4). `max_arrivals: 1` avoids queueing."""
    stations = [
        {"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Triangular",
         "param1": 0, "param2": 1, "node": "", "x": "", "y": ""},
        {"name": "S2", "resource": "S2", "capacity": 1, "distribution": "Weibull",
         "param1": 1, "param2": 1, "node": "", "x": "", "y": ""},
    ]
    product = [{**_ONE_PRODUCT[0], "max_arrivals": 1}]
    routings = [
        {"product": "A", "step": 1, "station": "S1", "process_time": 5},
        {"product": "A", "step": 2, "station": "S2", "process_time": 4},
    ]
    source = _write_tables(tmp_path, stations=stations, products=product, routings=routings)
    layout = tablegen.generate(source)
    assert _fatal(schema.validate_model_structured(layout)) == []

    triangular = next(b for b in layout["blocks"] if b["name"] == "process_s1")
    assert triangular["params"]["dist_params"] == {
        "low": "0.0 * entity.time_scale", "high": "1.0 * entity.time_scale",
        "mode": "0.5 * entity.time_scale"}
    weibull = next(b for b in layout["blocks"] if b["name"] == "process_s2")
    assert weibull["params"]["dist_params"] == {"scale": "1.0 * entity.time_scale", "shape": 1.0}

    from nagasim import api
    results = api.run_model(layout, deterministic=True, run_length=100, warmup=0)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(9.0)   # 5 (Triangular) + 4 (Weibull)


def test_unscalable_distribution_override_raises_valueerror_naming_it(tmp_path):
    station = [{"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Beta",
               "param1": 0.0, "param2": 1.0, "node": "", "x": "", "y": ""}]
    source = _write_tables(
        tmp_path, stations=station, products=_ONE_PRODUCT,
        routings=[{"product": "A", "step": 1, "station": "S1", "process_time": 5}])
    with pytest.raises(ValueError, match="S1.*A.*Beta|A.*S1.*Beta"):
        tablegen.generate(source)


def test_station_with_xy_gets_a_network_node(tmp_path):
    station = [{**_ONE_STATION[0], "x": 10, "y": 20}]
    source = _write_tables(tmp_path, stations=station, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    layout = tablegen.generate(source)
    assert layout["network"]["nodes"] == [{"name": "S1", "x": 10.0, "y": 20.0}]
    station_block = next(b for b in layout["blocks"] if b["name"] == "S1")
    assert station_block["params"]["node"] == "S1"


def test_orders_sheet_generates_one_orders_mode_create(tmp_path):
    orders = [{"id": "WO-1", "release": 0, "due": 100, "quantity": 1, "entity_type": "A",
              "sequence": "A", "priority": "", "source": ""}]
    source = _write_tables(tmp_path, stations=_ONE_STATION, routings=_ONE_ROUTING, orders=orders)
    layout = tablegen.generate(source)
    assert _fatal(schema.validate_model_structured(layout)) == []
    creates = [b for b in layout["blocks"] if b["type"] == "CREATE"]
    assert len(creates) == 1
    assert creates[0]["params"]["arrival_mode"] == "Orders"
    assert layout["orders"][0]["id"] == "WO-1"


def test_resources_sheet_overrides_station_resource(tmp_path):
    resources = [{"name": "S1", "capacity": 5, "discipline": "LIFO", "cost_busy": 1.5,
                 "cost_idle": "", "cost_per_use": "", "cost_time_unit": "", "capacity_schedule": ""}]
    source = _write_tables(tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING, resources=resources)
    layout = tablegen.generate(source)
    res = next(r for r in layout["resources"] if r["name"] == "S1")
    assert res["capacity"] == 5.0
    assert res["discipline"] == "LIFO"
    assert res["cost_busy"] == 1.5


def test_schedules_sheet_adds_a_capacity_schedule(tmp_path):
    schedules = [{"name": "Shift", "kind": "capacity", "points": "[[0, 1], [480, 0]]", "repeat": ""}]
    source = _write_tables(tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING, schedules=schedules)
    layout = tablegen.generate(source)
    assert layout["schedules"] == [{"name": "Shift", "kind": "capacity", "points": [[0, 1], [480, 0]]}]


def test_write_template_has_a_sheet_per_table(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "template.xlsx"
    tablegen.write_template(str(out))
    wb = openpyxl.load_workbook(str(out))
    assert set(wb.sheetnames) == {"Stations", "Products", "Routings", "Orders", "Resources",
                                  "Schedules"}


def test_cli_main_writes_json_and_returns_zero(tmp_path):
    source = _write_tables(tmp_path, stations=_ONE_STATION, products=_ONE_PRODUCT,
                           routings=_ONE_ROUTING)
    out = tmp_path / "model.json"
    code = tablegen.main([source, "-o", str(out)])
    assert code == 0
    layout = json.loads(out.read_text())
    assert _fatal(schema.validate_model_structured(layout)) == []


def test_cli_main_requires_source_or_template():
    with pytest.raises(SystemExit):
        tablegen.main([])
