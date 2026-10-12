"""Digital twin: data bindings, warm start, calibration, forecast
(ROADMAP 3.4-3.6).

Everything that can be checked against a hand-computed answer is: the
transforms against arithmetic done in the test, the warm start against D/D/1
departure times worked out on paper, the Wilson interval against the textbook
formula evaluated by hand, and calibration against a synthetic response with a
known optimum injected through `optimize.evaluation._evaluate_point` (the module's one
evaluation seam). Only the M/M/1 calibration and the band-coverage check are
statistical, and both are bounded well away from their assertion.
"""
import json
import sqlite3

import pandas as pd
import pytest

from nagasim import (api, calibrate as calibrate_mod, databind, forecast as forecast_mod,
                     netguard, optimize)
from nagasim.core import initial_state, schema
from nagasim.core.stats import wilson_interval
from nagasim.optimize import Factor

from conftest import chain, create, delay, dispose, layout as make_layout, process

SERVICE = [2.0, 4.0, 4.0, 6.0, 9.0]          # the tiny sample every transform test uses
TIMES = [0.0, 10.0, 20.0, 30.0, 40.0]        # arrival timestamps, in base (minute) units


# ============================================================ fixtures
def _model(**sections) -> dict:
    """An M/M/1-shaped layout with a Server resource, as a v13 dict."""
    blocks = [create(distribution="Exponential", dist_params={"rate": 0.5}),
              process("process_1", value=1.0, distribution="Exponential",
                      dist_params={"rate": 1.0}),
              dispose()]
    base = make_layout(blocks, chain(*blocks),
                       resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
                       simulation={"time_limit": 200, "warmup": 0, "replications": 1,
                                   "time_unit": "minutes"})
    migrated = schema.migrate(base)
    migrated.update(sections)
    return migrated


@pytest.fixture
def csv_source(tmp_path):
    """A 5-row visit log plus the layout that declares it as a source."""
    path = tmp_path / "visits.csv"
    rows = ["t,svc,clinic"] + [f"{t},{s},{'north' if i < 3 else 'south'}"
                               for i, (t, s) in enumerate(zip(TIMES, SERVICE))]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return tmp_path


def _bound(csv_dir, **binding):
    layout = _model(data_sources=[{"name": "visits", "kind": "csv", "location": "visits.csv"}],
                    data_bindings=[{"name": "b", "source": "visits", "enabled": True, **binding}])
    return databind.resolve_bindings(layout, csv_dir)


# ============================================================ 3.4a data bindings
# (transform, column, target, expected value) -- every expectation computed by
# hand from SERVICE = [2, 4, 4, 6, 9] / TIMES = [0, 10, 20, 30, 40].
TRANSFORM_CASES = [
    ("mean", "svc", "blocks.process_1.params.dist_params.rate", 5.0),          # 25 / 5
    ("median", "svc", "blocks.process_1.params.dist_params.rate", 4.0),
    ("sum", "svc", "blocks.process_1.params.dist_params.rate", 25.0),
    ("last", "svc", "blocks.process_1.params.dist_params.rate", 9.0),
    ("count", "svc", "simulation.replications", 5),
    ("value", "svc", "blocks.process_1.params.dist_params.rate", 2.0),
    ("quantile:0.5", "svc", "blocks.process_1.params.dist_params.rate", 4.0),
    ("quantile:1.0", "svc", "blocks.process_1.params.dist_params.rate", 9.0),
    # 5 timestamps spanning 40 minutes == 2/3 hour -> 7.5 per hour
    ("rate_per:hours", "t", "blocks.process_1.params.dist_params.rate", 7.5),
    # ... and 5 over 40 minutes is 0.125 per minute, the model's base unit
    ("rate_per:minutes", "t", "blocks.process_1.params.dist_params.rate", 0.125),
    ("mean", "svc", "resources.Server.capacity", 5),        # an int field rounds
]


@pytest.mark.parametrize("transform, column, target, expected", TRANSFORM_CASES)
def test_every_transform_writes_the_hand_computed_value(csv_source, transform, column, target,
                                                        expected):
    resolved, report = _bound(csv_source, column=column, transform=transform, target=target)
    assert report.ok, report.frame().to_string()
    record = report.records[0]
    assert record.value == pytest.approx(expected)
    assert type(record.value) is type(expected)     # an int target never gets a float
    assert record.rows == 5


def test_fit_and_empirical_write_both_halves_of_the_distribution_spec(csv_source):
    """A distribution-writing transform targets the `distribution` leaf and
    writes the sibling `dist_params` alongside it -- one binding, two fields."""
    resolved, report = _bound(csv_source, column="svc", transform="fit",
                              target="blocks.process_1.params.distribution")
    assert report.ok, report.frame().to_string()
    params = resolved["blocks"][1]["params"]
    assert params["distribution"] != "Exponential" or params["dist_params"] != {"rate": 1.0}
    assert set(report.records[0].value) == {"distribution", "dist_params"}
    json.dumps(resolved)        # numpy scalars would not survive this

    # `empirical` follows the sample's own type: this one is integer-valued,
    # so it becomes a point-mass spec rather than a piecewise-uniform one.
    resolved, report = _bound(csv_source, column="svc", transform="empirical",
                              target="blocks.process_1.params.distribution")
    spec = resolved["blocks"][1]["params"]
    assert spec["distribution"] == "EmpiricalDiscrete"
    assert spec["dist_params"]["values"] == [2, 4, 6, 9]
    assert spec["dist_params"]["probs"] == pytest.approx([0.2, 0.4, 0.2, 0.2])


def test_a_distribution_transform_must_target_a_distribution_field(csv_source):
    _resolved, report = _bound(csv_source, column="svc", transform="fit",
                               target="resources.Server.capacity")
    assert [p.code for p in report.problems] == ["E_DATA_BINDING_TARGET"]


def test_a_filter_selects_rows_before_the_transform(csv_source):
    """clinic == north keeps [2, 4, 4]; mean 10/3."""
    _resolved, report = _bound(csv_source, column="svc", transform="mean",
                               target="blocks.process_1.params.dist_params.rate",
                               filter={"clinic": "north"})
    assert report.records[0].rows == 3
    assert report.records[0].value == pytest.approx(10.0 / 3.0)

    _resolved, ge = _bound(csv_source, column="svc", transform="count",
                           target="simulation.replications", filter={"t >=": 20})
    assert ge.records[0].value == 3


def test_an_arrival_schedule_binding_writes_rate_points(csv_source):
    """Timestamps 0, 10, 20, 30, 40 in 20-minute bins fall 2, 2, 1 -- the last
    one opens a third bin -- so the rates are 0.1, 0.1 and 0.05 per minute."""
    resolved, report = _bound(csv_source, column="t", transform="arrival_schedule:20",
                              target="simulation.time_limit")
    assert not report.ok           # time_limit holds a number, not a list of points
    assert report.problems[0].code == "E_DATA_TYPE"

    layout = _model(data_sources=[{"name": "v", "kind": "csv", "location": "visits.csv"}],
                    data_bindings=[{"name": "s", "source": "v", "column": "t",
                                    "transform": "arrival_schedule:20",
                                    "target": "schedules.day.points"}],
                    schedules=[{"name": "day", "kind": "arrival", "points": [[1, 1]],
                                "repeat": False}])
    resolved, report = databind.resolve_bindings(layout, csv_source)
    assert report.ok, report.frame().to_string()
    assert resolved["schedules"][0]["points"] == [[20.0, 0.1], [20.0, 0.1], [20.0, 0.05]]


def test_a_table_binding_refreshes_a_declared_lookup_table(tmp_path):
    """PHASE4 A1: the DECLARED table fixes the shape, the data fills it in, and
    a source that misses one of its keys is a problem rather than a reshape."""
    path = tmp_path / "times.csv"
    path.write_text("product,station,minutes\nWIDGET,Cut,3\nWIDGET,Pack,2\n"
                    "GADGET,Cut,4\nGADGET,Pack,6\n", encoding="utf-8")
    table = {"name": "ProcessTime", "keys": ["WIDGET", "GADGET"], "columns": ["Cut", "Pack"],
             "values": [[1, 1], [1, 1]]}
    layout = _model(
        tables=[table],
        data_sources=[{"name": "t", "kind": "csv", "location": "times.csv"}],
        data_bindings=[{"name": "grid", "source": "t", "column": "minutes",
                        "transform": "table:product:station",
                        "target": "tables.ProcessTime.values"}])
    resolved, report = databind.resolve_bindings(layout, tmp_path)
    assert report.ok, report.frame().to_string()
    assert resolved["tables"][0]["values"] == [[3.0, 2.0], [4.0, 6.0]]

    layout["tables"] = [{**table, "keys": ["WIDGET", "SPROCKET"]}]
    _resolved, missing = databind.resolve_bindings(layout, tmp_path)
    assert missing.problems[0].code == "E_DATA_EMPTY"


def test_an_orders_binding_fills_the_whole_order_book_from_an_mes_export(tmp_path):
    """PHASE4 A3: orders are what an MES actually feeds a twin, so this is the
    one transform that REPLACES a section; `map` renames the source's columns
    onto the order fields, and an unmapped field falls back to its own name."""
    path = tmp_path / "wo.csv"
    path.write_text("WO_NUMBER,RELEASE,due,quantity\nWO-1,0,480,2\nWO-2,60,500,1\n",
                    encoding="utf-8")
    layout = _model(
        orders=[{"id": "STALE", "release": 0, "due": 1}],
        data_sources=[{"name": "mes", "kind": "csv", "location": "wo.csv"}],
        data_bindings=[{"name": "book", "source": "mes", "transform": "orders",
                        "target": "orders",
                        "map": {"id": "WO_NUMBER", "release": "RELEASE"}}])
    resolved, report = databind.resolve_bindings(layout, tmp_path)
    assert report.ok, report.frame().to_string()
    assert resolved["orders"] == [
        {"id": "WO-1", "release": 0.0, "due": 480.0, "quantity": 2},
        {"id": "WO-2", "release": 60.0, "due": 500.0, "quantity": 1}]

    layout["data_bindings"][0]["map"] = {}     # no id column under its own name
    _resolved, missing = databind.resolve_bindings(layout, tmp_path)
    assert missing.problems[0].code == "E_DATA_EMPTY"


def test_a_sqlite_source_is_local_and_needs_no_network_permission(tmp_path):
    db = tmp_path / "visits.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE visits (svc REAL)")
        conn.executemany("INSERT INTO visits VALUES (?)", [(s,) for s in SERVICE])
    layout = _model(data_sources=[{"name": "db", "kind": "sqlite", "location": "visits.db",
                                   "query": "SELECT svc FROM visits"}],
                    data_bindings=[{"name": "b", "source": "db", "column": "svc",
                                    "transform": "mean",
                                    "target": "blocks.process_1.params.dist_params.rate"}])
    resolved, report = databind.resolve_bindings(layout, tmp_path)
    assert report.ok, report.frame().to_string()
    assert resolved["blocks"][1]["params"]["dist_params"]["rate"] == pytest.approx(5.0)
    assert report.records[0].source_time is not None      # the file's own mtime


# (kind, location, query, allow_network, expected code)
REFUSAL_CASES = [
    ("json_url", "https://example.invalid/data.json", None, False, "E_DATA_NETWORK_REFUSED"),
    ("sql", "postgresql://user@host/db", "SELECT 1", False, "E_DATA_NETWORK_REFUSED"),
    # A literal password is refused even WITH permission: a model file never
    # carries a credential.
    ("sql", "postgresql://user:hunter2@host/db", "SELECT 1", True, "E_DATA_CREDENTIALS"),
    ("sql", "postgresql://user:${NAGASIM_DATA_NOT_SET_ANYWHERE}@host/db", "SELECT 1", True,
     "E_DATA_ENV"),
    # A model file names only the variables the caller named for it (DEVLOG #335).
    ("sql", "postgresql://user:${PATH}@host/db", "SELECT 1", True, "E_DATA_ENV_NAME"),
    # A sqlite URL is a file pretending to be a network source.
    ("sql", "sqlite:///C:/Windows/win.ini", "SELECT 1", True, "E_DATA_URL_SCHEME"),
]


def _remote_model(kind, location, query=None):
    source = {"name": "remote", "kind": kind, "location": location}
    if query:
        source["query"] = query
    return _model(data_sources=[source],
                  data_bindings=[{"name": "b", "source": "remote", "column": "svc",
                                  "transform": "mean",
                                  "target": "blocks.process_1.params.dist_params.rate"}])


@pytest.mark.parametrize("kind, location, query, allow, code", REFUSAL_CASES)
def test_network_and_credential_rules(kind, location, query, allow, code, tmp_path):
    layout = _remote_model(kind, location, query)
    _resolved, report = databind.resolve_bindings(
        layout, tmp_path, network=netguard.NetworkPolicy(allow_network=allow))
    assert [p.code for p in report.problems] == [code]
    # A refusal is a problem, never an exception, and never a partial write.
    assert report.overrides == []


def test_a_model_file_cannot_grant_itself_network_access(tmp_path):
    """The permission is the CALLER's, never the file's: the key is stripped on
    load, warned about, and grants nothing on any path that reads it."""
    layout = _remote_model("json_url", "https://example.invalid/data.json")
    layout["data_sources"][0]["allow_network"] = True

    assert [p.code for p in schema.consent_problems(layout)] == ["W_DATA_ALLOW_NETWORK_IGNORED"]
    assert "allow_network" not in schema.migrate(layout)["data_sources"][0]

    _resolved, report = databind.resolve_bindings(layout, tmp_path)
    assert [p.code for p in report.problems] == ["E_DATA_NETWORK_REFUSED"]

    results = api.run_model(layout, replications=1, run_length=20, bind_data=True,
                            base_dir=tmp_path)
    assert results["data_inputs"]["Problem"].iloc[0].startswith("binding 'b'")


def test_a_password_in_a_model_file_is_a_validation_error_but_an_env_reference_is_not():
    with_password = _model(data_sources=[{"name": "db", "kind": "sql", "query": "SELECT 1",
                                          "location": "postgresql://u:secret@host/db"}])
    codes = [p.code for p in schema.validate_model_structured(with_password)]
    assert "E_DATA_CREDENTIALS" in codes

    from_env = _model(data_sources=[{"name": "db", "kind": "sql", "query": "SELECT 1",
                                     "location": "postgresql://u:${NAGASIM_DATA_DB_PASSWORD}@host/db"}])
    assert [p.code for p in schema.validate_model_structured(from_env)] == []


def test_env_expansion_reads_the_environment(monkeypatch, csv_source):
    monkeypatch.setenv("NAGASIM_DATA_CLINIC_FILE", "visits.csv")
    layout = _model(data_sources=[{"name": "v", "kind": "csv", "location": "${NAGASIM_DATA_CLINIC_FILE}"}],
                    data_bindings=[{"name": "b", "source": "v", "column": "svc",
                                    "transform": "mean",
                                    "target": "blocks.process_1.params.dist_params.rate"}])
    _resolved, report = databind.resolve_bindings(layout, csv_source)
    assert report.ok and report.records[0].value == pytest.approx(5.0)


def test_resolve_bindings_never_mutates_the_input_layout(csv_source):
    layout = _model(data_sources=[{"name": "v", "kind": "csv", "location": "visits.csv"}],
                    data_bindings=[{"name": "b", "source": "v", "column": "svc",
                                    "transform": "mean", "target": "resources.Server.capacity"}])
    before = json.dumps(layout, sort_keys=True)
    resolved, report = databind.resolve_bindings(layout, csv_source)
    assert json.dumps(layout, sort_keys=True) == before
    assert layout["resources"][0]["capacity"] == 1 and resolved["resources"][0]["capacity"] == 5


def test_a_bound_run_reports_its_data_inputs_in_the_report_header(csv_source):
    from nagasim.report import build_report

    layout = _model(data_sources=[{"name": "v", "kind": "csv", "location": "visits.csv"}],
                    data_bindings=[{"name": "service_rate", "source": "v", "column": "svc",
                                    "transform": "quantile:0.5",
                                    "target": "blocks.process_1.params.dist_params.rate"}])
    results = api.run_model(layout, replications=1, run_length=50, bind_data=True,
                            base_dir=csv_source)
    assert "data_inputs" in results and len(results["data_inputs"]) == 1

    html = build_report(layout, results, title="bound")
    assert "Data inputs" in html and "service_rate" in html and "visits.csv" in html


# ============================================================ 3.4b warm start
def _dd1(**state):
    """D/D/1 with no arrivals inside the horizon and a constant 5-minute
    service, so every departure time is arithmetic."""
    blocks = [create(distribution="Constant", dist_params={"value": 1000.0},
                     max_arrivals=1, start_time=1000),
              process("process_1", value=5.0),
              dispose()]
    layout = schema.migrate(make_layout(
        blocks, chain(*blocks),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        simulation={"time_limit": 50, "warmup": 0, "replications": 1, "time_unit": "minutes"}))
    layout["initial_state"] = state
    return layout


def test_warm_start_departure_times_are_exactly_hand_computed():
    """3 patients at the PROCESS block, the first 2 minutes from finishing.
    Service is 5 minutes, so the departures are 2, 2+5=7 and 7+5=12, and with
    an age of 4 every time in system is its departure time plus 4."""
    layout = _dd1(entities=[{"block": "process_1", "count": 3, "age": 4.0,
                             "in_service": 1, "remaining": [2.0]}])
    results = api.run_model(layout, replications=1, run_length=50, warmup=0)
    log = results["entity_log"].sort_values("Entity ID")

    assert list(log["Exited At"]) == [2.0, 7.0, 12.0]
    assert list(log["Throughput Time"]) == [6.0, 11.0, 16.0]
    assert list(log["Created At"]) == [-4.0, -4.0, -4.0]
    assert list(log["Initial"]) == [True, True, True]
    # Injected entities count as created even though their creation time is
    # negative: they entered the model at t=0.
    system = results["system"].set_index("Metric")["Value"]
    assert system["Total Entities Created"] == 3
    assert system["Total Entities Completed"] == 3


def test_without_remaining_an_in_service_entity_samples_a_fresh_service_time():
    """Same model, no `remaining`: the entity in service holds a full 5-minute
    sample, so the departures shift to 5, 10, 15 (the documented approximation
    for a non-exponential service)."""
    layout = _dd1(entities=[{"block": "process_1", "count": 3, "in_service": 1}])
    results = api.run_model(layout, replications=1, run_length=50, warmup=0)
    assert list(results["entity_log"].sort_values("Entity ID")["Exited At"]) == [5.0, 10.0, 15.0]


def test_initial_variables_and_a_failed_resource_start_the_run_as_declared():
    layout = _dd1(entities=[{"block": "process_1", "count": 1}],
                  variables={"shift": 2}, resources={"Server": {"down_until": 8.0}})
    layout["variables"] = [{"name": "shift", "initial": 0}]
    results = api.run_model(layout, replications=1, run_length=50, warmup=0)
    # The server is down until t=8, so the single 5-minute service ends at 13.
    assert results["entity_log"]["Exited At"].iloc[0] == pytest.approx(13.0)
    assert results["resource_summary"]["Down %"].iloc[0] == pytest.approx(16.0)


def test_warmup_defaults_to_zero_for_a_warm_started_model():
    layout = _dd1(entities=[{"block": "process_1", "count": 1}])
    layout["simulation"]["warmup"] = 20.0
    assert initial_state.default_warmup(layout, None) == 0.0
    assert initial_state.default_warmup(layout, 7.0) == 7.0          # explicit still wins
    # ... and setting both is a warning, not an error.
    problems = schema.validate_model_structured(layout)
    assert [p.code for p in problems] == ["W_INITIAL_STATE_WARMUP"]
    assert all(p.severity == "warning" for p in problems)

    # The default reaches the run: statistics are NOT reset at t=20.
    results = api.run_model(layout, replications=1, run_length=50)
    assert results["system"].set_index("Metric")["Value"]["Total Entities Completed"] == 1


# (initial_state entities entry, expected problem code)
VALIDATION_CASES = [
    ({"block": "nope", "count": 1}, "E_INITIAL_BLOCK"),
    ({"block": "create_1", "count": 1}, "E_INITIAL_BLOCK_TYPE"),
    ({"block": "dispose_1", "count": 1}, "E_INITIAL_BLOCK_TYPE"),
    ({"block": "process_1", "count": -1}, "E_INITIAL_COUNT"),
    ({"block": "process_1", "count": 1, "age": -2}, "E_INITIAL_AGE"),
    ({"block": "process_1", "count": 1, "in_service": 2}, "E_INITIAL_IN_SERVICE"),
    ({"block": "process_1", "count": 5, "in_service": 3}, "E_INITIAL_IN_SERVICE"),  # > capacity 1
    ({"block": "delay_1", "count": 1, "in_service": 1, "remaining": [1.0, 2.0]},
     "E_INITIAL_REMAINING"),
    ({"block": "process_1", "count": 1, "remaining": ["soon"]}, "E_INITIAL_REMAINING"),
]


@pytest.mark.parametrize("entry, code", VALIDATION_CASES)
def test_initial_state_validation_names_the_problem(entry, code):
    blocks = [create(), process("process_1", value=5.0), delay("delay_1"), dispose()]
    layout = schema.migrate(make_layout(
        blocks, chain(blocks[0], blocks[1], blocks[2], blocks[3]),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}]))
    layout["initial_state"] = {"entities": [entry]}
    assert code in [p.code for p in schema.validate_model_structured(layout)]


def test_initial_state_refuses_unknown_variables_and_resources():
    layout = _dd1(variables={"nope": 1}, resources={"Missing": {"down_until": 5}})
    codes = [p.code for p in schema.validate_model_structured(layout)]
    assert sorted(codes) == ["E_INITIAL_RESOURCE", "E_INITIAL_VARIABLE"]


# ============================================================ 3.5 calibration
def test_the_objective_weights_squared_relative_errors_and_ks_distance():
    """Hand arithmetic: observed 10 against a simulated 12 is a relative error
    of 0.2, squared 0.04, weighted 3 -> 0.12; a second target adds its own."""
    targets = calibrate_mod._targets([
        {"kpi": "system/Avg Throughput Time", "observed": 10.0, "weight": 3.0},
        {"kpi": "system/Avg Queue Length", "observed": 4.0},
    ])
    frames = {"system": pd.DataFrame([
        {"Run": 0, "Metric": "Avg Throughput Time", "Value": 12.0},
        {"Run": 0, "Metric": "Avg Queue Length", "Value": 5.0},
    ])}
    values = calibrate_mod._extra_kpis(targets)(frames)
    assert values[calibrate_mod.OBJECTIVE_KPI][0] == pytest.approx(3 * 0.2 ** 2 + 0.25 ** 2)


def test_a_distributional_target_uses_the_two_sample_ks_statistic():
    targets = calibrate_mod._targets([{"kpi": "entity_log/Throughput Time",
                                       "observed_sample": [1.0, 2.0, 3.0, 4.0]}])
    frames = {"system": pd.DataFrame(columns=["Run", "Metric", "Value"]),
              "entity_log": pd.DataFrame([{"Run": 0, "Throughput Time": v}
                                          for v in (1.0, 2.0, 3.0, 4.0)])}
    values = calibrate_mod._extra_kpis(targets)(frames)
    # Identical samples: KS statistic 0, so the objective is 0 too.
    assert values["calibration/ks/entity_log/Throughput Time"][0] == pytest.approx(0.0)
    assert values[calibrate_mod.OBJECTIVE_KPI][0] == pytest.approx(0.0)


def _synthetic_response(true_x=3.0):
    """A fake `optimize.evaluation._evaluate_point`: the KPI is 10 + (x - true_x), so the
    factor value that makes it equal an observed 10.0 is exactly true_x."""
    def fake(layout, overrides, seeds, **kwargs):
        x = float(next(o["value"] for o in overrides if o["path"] == "simulation.time_limit"))
        simulated = 10.0 + (x - true_x)
        objective = ((simulated - 10.0) / 10.0) ** 2
        return {"system/Avg Throughput Time": {s: simulated for s in seeds},
                calibrate_mod.OBJECTIVE_KPI: {s: objective for s in seeds}}
    return fake


def test_calibration_recovers_a_known_parameter_and_reports_pass_and_fail(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic_response(true_x=3.0))
    result = calibrate_mod.calibrate(
        {"schema_version": 9},
        [{"kpi": "system/Avg Throughput Time", "observed": 10.0, "tolerance": 0.2}],
        [Factor("simulation.time_limit", "continuous", -5.0, 5.0, name="x")],
        budget=200, replications_per_point=3)

    assert result.best["x"] == pytest.approx(3.0, abs=0.1)
    assert result.objective == pytest.approx(0.0, abs=1e-3)
    row = result.table.iloc[0]
    assert row["Observed"] == 10.0 and row["Simulated"] == pytest.approx(10.0, abs=0.1)
    assert bool(row["Pass"]) and result.passed
    assert result.overrides == [{"path": "simulation.time_limit", "value": result.best["x"]}]
    assert result.as_scenario()["name"] == "Calibrated"

    # The same run against an observation the model cannot reach fails loudly.
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic_response(true_x=99.0))
    missed = calibrate_mod.calibrate(
        {"schema_version": 9},
        [{"kpi": "system/Avg Throughput Time", "observed": 10.0, "tolerance": 0.2}],
        [Factor("simulation.time_limit", "continuous", -5.0, 5.0, name="x")],
        budget=100, replications_per_point=3)
    assert not bool(missed.table.iloc[0]["Pass"]) and not missed.passed


def test_calibrating_a_service_rate_to_an_observed_utilization():
    """The one real-simulation calibration: arrivals every 2 min, so an
    observed utilization of 50% means a service rate of exactly 1.0/min
    (rho = lambda / mu). The search itself is proven on a synthetic response
    above; this checks the plumbing into a real model's distribution and KPI.

    Deterministic run mode (W4 P1; suite pass 4): the M/M/1 version spent
    60 x 3 noisy replications (~11 s) proving rho = lambda / mu again, which
    the M/M/1 oracle already does; with every sample at its mean the model is
    a D/D/1, one run per point, and budget 20 lands within 0.013 of 1.0 for
    seeds 42, 7 and 123 (tolerance 0.05)."""
    layout = _model()
    layout["simulation"]["deterministic"] = True
    result = calibrate_mod.calibrate(
        layout, [{"kpi": "resource/Server/Usage % (snapshot)", "observed": 50.0,
                  "tolerance": 3.0}],
        [Factor("blocks.process_1.params.dist_params.rate", "continuous", 0.6, 1.6,
                name="mu")],
        budget=20, replications_per_point=1, run_length=1000, seed=42)

    assert result.best["mu"] == pytest.approx(1.0, abs=0.05)
    assert bool(result.table.iloc[0]["Pass"])


# ============================================================ 3.6 forecast
# (successes, n, expected p, expected lb, expected ub) -- Wilson at 95%, the
# textbook formula evaluated by hand for the two cases that matter.
WILSON_CASES = [
    # p = 0.5, n = 4, z = 1.959964: centre 0.5, half = z*sqrt(.0625+.0600)/(1+.9604)
    (2, 4, 0.5, 0.15003, 0.84997),
    # p = 1.0, n = 20: the normal interval would be [1, 1]; Wilson is not.
    (20, 20, 1.0, 0.83891, 1.0),
    (0, 10, 0.0, 0.0, 0.27753),
]


@pytest.mark.parametrize("successes, n, p, lb, ub", WILSON_CASES)
def test_wilson_interval_matches_the_hand_computed_values(successes, n, p, lb, ub):
    stat = wilson_interval(successes, n)
    assert stat["p"] == pytest.approx(p)
    assert stat["lb"] == pytest.approx(lb, abs=1e-4)
    assert stat["ub"] == pytest.approx(ub, abs=1e-4)
    assert wilson_interval(0, 0)["lb"] == 0.0 and wilson_interval(0, 0)["ub"] == 1.0


def test_the_band_is_a_prediction_band_that_contains_most_replication_paths():
    result = forecast_mod.forecast(_model(), horizon=200, replications=20, bind_data=False,
                                   seed=3)
    band = result.band("system/WIP")
    assert not band.empty
    assert (band["Q05"] <= band["Q95"]).all()

    # At a sampled time, at least 80% of the replication values fall inside the
    # 5-95% band -- the property that makes it a prediction band.
    raw = result.results["timeseries"]
    time = float(band["Time"].iloc[len(band) // 2])
    row = band[band["Time"] == time].iloc[0]
    values = raw[(raw["Series"] == "system/WIP") & (raw["Time"] == time)]["Value"]
    inside = ((values >= row["Q05"]) & (values <= row["Q95"])).mean()
    assert inside >= 0.8
    # The CI of the mean is a different, narrower thing -- and it is reported.
    kpi = result.kpis.set_index("KPI").loc["system/Avg Throughput Time"]
    assert kpi["LB"] >= kpi["Q05"] - 1e-9 and kpi["UB"] <= kpi["Q95"] + 1e-9


def test_a_forecast_is_terminating_starts_from_the_initial_state_and_answers_events():
    """Deterministic model: 3 warm-started patients, 5-minute services, no
    arrivals. Every replication is identical, so an event is certain or
    impossible and the probability is exactly 1.0 or 0.0."""
    layout = _dd1(entities=[{"block": "process_1", "count": 3}])
    result = forecast_mod.forecast(
        layout, horizon=50, replications=5, bind_data=False,
        events=[{"name": "any queue", "kpi": "timeseries/resource/Server/queue/max",
                 "op": ">", "value": 1},
                {"name": "impossible", "kpi": "timeseries/resource/Server/queue/max",
                 "op": ">", "value": 99}])

    assert result.horizon == 50 and result.replications == 5
    probabilities = dict(zip(result.events["Event"], result.events["Probability"]))
    assert probabilities == {"any queue": 1.0, "impossible": 0.0}
    # Terminating: the horizon is the observed window, with no warm-up cut.
    assert result.kpis.set_index("KPI").loc["system/Total Entities Completed", "Mean"] == 3.0
    assert result.kpis.set_index("KPI").loc["system/Simulation Time", "Mean"] == 50.0


def test_an_unknown_event_kpi_is_refused():
    with pytest.raises(ValueError, match="unknown forecast KPI"):
        forecast_mod.forecast(_model(), horizon=50, replications=2, bind_data=False,
                              events=[{"name": "x", "kpi": "system/Nope", "op": ">", "value": 1}])
    assert forecast_mod.parse_event("system/Avg Queue Length>=5") == {
        "name": "system/Avg Queue Length >= 5", "kpi": "system/Avg Queue Length",
        "op": ">=", "value": 5.0}


# ============================================================ wiring
def test_the_twin_agent_tools_answer_with_digests_and_the_shared_error_shape(tmp_path,
                                                                             csv_source):
    from nagasim import agent_tools

    agent_tools.use_workspace(agent_tools.Workspace(csv_source))
    layout = _model(data_sources=[{"name": "v", "kind": "csv", "location": "visits.csv"}],
                    data_bindings=[{"name": "b", "source": "v", "column": "svc",
                                    "transform": "mean", "target": "resources.Server.capacity"}])
    (csv_source / "m.json").write_text(json.dumps(layout))
    assert agent_tools.call("open_model", path="m.json", name="m").get("ok")

    bound = agent_tools.call("resolve_data_bindings", model="m", apply=True)
    assert bound["bindings"][0]["value"] == 5 and bound["problems"] == 0
    assert agent_tools.workspace().document("m").resources[0]["capacity"] == 5

    warm = agent_tools.call("set_initial_state", model="m",
                            entities=[{"block": "process_1", "count": 2}])
    assert warm["entities"] == 2 and warm["errors"] == 0

    forecast_digest = agent_tools.call("forecast", model="m", horizon=50, replications=4,
                                       bind_data=False)
    assert forecast_digest["replications"] == 4 and forecast_digest["kpis"]
    assert all(k["kpi"].startswith(("system/", "timeseries/system/"))
               for k in forecast_digest["kpis"])

    # Every tool answers an unknown model with the same structured error.
    for name in ("resolve_data_bindings", "set_initial_state", "forecast", "calibrate"):
        arguments = {"model": "missing"}
        if name == "forecast":
            arguments["horizon"] = 10
        if name == "calibrate":
            arguments.update(targets=[], factors=[])
        assert agent_tools.call(name, **arguments)["error"]["code"] == "E_UNKNOWN_MODEL"


def test_the_bundled_clinic_twin_example_binds_and_runs():
    """The example is the end-to-end proof: bindings resolve against its own
    CSV, the warm start injects, and the run completes."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "clinic_twin.json"
    layout = schema.migrate(json.loads(path.read_text()))
    assert schema.validate_model(layout) == []

    resolved, report = databind.resolve_bindings(layout, path.parent)
    assert report.ok, report.frame().to_string()
    assert resolved["blocks"][2]["params"]["distribution"] != layout["blocks"][2]["params"]["distribution"] \
        or resolved["blocks"][2]["params"]["dist_params"] != layout["blocks"][2]["params"]["dist_params"]
    assert len(resolved["schedules"][0]["points"]) == 8       # one per hour of the day

    results = api.run_model(str(path), replications=2, run_length=480, bind_data=True)
    log = results["entity_log"]
    assert int(log["Initial"].sum()) == 10                    # 5 injected x 2 replications
    completed = results["system"].set_index("Metric")["Value"]["Total Entities Completed"]
    assert (completed > 0).all()


def test_a_warm_started_entity_is_announced_like_any_other():
    """#338: every injected entity gets its `entity_created`, in the block it is put
    in and before it seizes anything, so a trace of the run starts with the model as
    the snapshot left it. The results are those of a run nobody listens to."""
    from pathlib import Path

    from nagasim.core.playback import Timeline
    from nagasim.core.trace import TraceRecorder

    model = str(Path(__file__).resolve().parents[1] / "nagasim" / "models" / "clinic_twin.json")
    settings = {"replications": 1, "seed": 0, "warmup": 0, "run_length": 100}
    recorder = TraceRecorder()
    heard = api.run_model(model, trace=recorder, **settings)
    unheard = api.run_model(model, **settings)
    assert heard["system"].equals(unheard["system"])
    assert heard["entity_log"].equals(unheard["entity_log"])

    first = [(event.kind, event.entity_id, event.block) for event in recorder.events
             if event.kind.startswith("entity")][:8]
    assert first[:5] == [("entity_created", 1, "Consult"), ("entity_created", 2, "Consult"),
                         ("entity_created", 3, "Consult"), ("entity_created", 4, "Consult"),
                         ("entity_created", 5, "Reception")]
    assert {kind for kind, _entity, _block in first[5:]} == {"entity_seized"}
    timeline = Timeline(recorder.events)
    assert timeline.created_until(0.0) == 5 and timeline.state_at(0.0)["counts"] == {
        "Consult": 4, "Reception": 1}
