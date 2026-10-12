"""Schedule log, Gantt, plan export (PHASE6 W4 P2, ARCHITECTURE D120).

Fast set. The independent oracle (tests/test_schedule_log_oracle.py) covers the
by-hand flow-shop and changeover numbers, the opt-out frame shape, the Gantt
bar count and plan_export's columns; this file covers what it takes for
granted: the deterministic hand-off hook, warm-up reset, resource-set member
naming, a worker fleet's Unit, Gantt edge cases and plan_export's date columns.
"""
import json

import pandas as pd
import pytest

from conftest import chain, create, dispose, layout, process
from nagasim import api, planning, svgcharts
from nagasim.core import export
from nagasim.core.document import ModelDocument
from test_workers import model as worker_model, workers

SCHEDULE_LOG_COLS = ["Run", "Order", "Entity", "Entity Type", "Block", "Resource", "Unit",
                     "Start", "End", "Setup"]
SIM = {"time_limit": 20, "warmup": 0, "replications": 1}


# ---------------------------------------------------------------- opt-in gating
def test_simulation_deterministic_forces_schedule_on_too():
    """P1 (ARCHITECTURE D119) forces `outputs.schedule` on in deterministic
    mode; this tests only the hand-off hook `graph_model.py` exposes
    (`simulation.deterministic`), not P1's own determinism, which is out of
    this task's scope."""
    blocks = [create(max_arrivals=1), process(value=3.0), dispose()]
    model = layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                   simulation={**SIM, "deterministic": True})
    sched = api.run_model(model, replications=1, run_length=20)["schedule_log"]
    assert len(sched) == 1
    # A plain entity on a plain resource: no order, no fleet unit.
    assert pd.isna(sched.iloc[0]["Order"]) and pd.isna(sched.iloc[0]["Unit"])


# ---------------------------------------------------------------- resource sets
def test_resource_set_names_the_member_not_the_set():
    blocks = [create(max_arrivals=1), process(resource="Team", value=1.0), dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1}, {"name": "Other", "capacity": 1}],
                   resource_sets=[{"name": "Team", "members": ["Server", "Other"]}],
                   outputs={"schedule": True}, simulation=SIM)
    results = api.run_model(model, replications=1, run_length=20)
    sched = results["schedule_log"]
    assert len(sched) == 1
    assert sched.iloc[0]["Resource"] in ("Server", "Other")   # never "Team"


# ---------------------------------------------------------------- warm-up reset
def test_schedule_log_resets_at_warmup():
    """A hold fully served and released BEFORE warm-up is dropped, like every
    other counter (`sim_runner.reset_statistics`); one starting after it is
    kept."""
    blocks = [create(max_arrivals=2, dist_params={"value": 100.0}), process(value=1.0), dispose()]
    model = layout(blocks, chain(*blocks), outputs={"schedule": True},
                   resources=[{"name": "Server", "capacity": 1}],
                   simulation={"time_limit": 200, "warmup": 50, "replications": 1})
    results = api.run_model(model, replications=1, run_length=200, warmup=50)
    sched = results["schedule_log"]
    assert len(sched) == 1
    assert sched.iloc[0]["Start"] == pytest.approx(100.0)


# ---------------------------------------------------------------- worker fleets
def test_worker_fleet_hold_carries_a_unit_index():
    fleet = workers([{"name": "Ann", "skills": []}, {"name": "Bob", "skills": []}])
    blocks = [create(max_arrivals=1),
             process(resource="", value=2.0, worker_fleet="W", node="A"), dispose()]
    m = worker_model(blocks, chain(*blocks), fleet, outputs={"schedule": True})
    results = api.run_model(m, replications=1)
    sched = results["schedule_log"]
    assert len(sched) == 1
    assert sched.iloc[0]["Unit"] in (0, 1)


# ---------------------------------------------------------------- run dialog wiring
def test_document_set_schedule_output_round_trips_and_omits_when_off():
    doc = ModelDocument()
    assert doc.outputs == {}
    doc.set_schedule_output(True)
    assert doc.to_layout()["outputs"] == {"schedule": True}
    doc.set_schedule_output(False)
    assert doc.to_layout()["outputs"] == {}   # dropped, not written as false


# ---------------------------------------------------------------- gantt
def _rows():
    return [{"Resource": "R1", "Order": "J1", "Start": 0.0, "End": 3.0},
            {"Resource": "R1", "Order": "J2", "Start": 3.0, "End": 5.0},
            {"Resource": "R2", "Order": "J1", "Start": 3.0, "End": 5.0}]


def test_gantt_with_no_rows_draws_no_data():
    assert "No data" in svgcharts.gantt([], "resource")


def test_gantt_rejects_an_unknown_by():
    with pytest.raises(ValueError):
        svgcharts.gantt(_rows(), "station")


# ---------------------------------------------------------------- plan_export
def _plan_frame():
    rows = [{"Run": 0, "Order": "J1", "Entity": 1, "Entity Type": "WIDGET", "Block": "p1",
            "Resource": "R1", "Unit": None, "Start": 0.0, "End": 3.0, "Setup": 0.0}]
    return pd.DataFrame(rows, columns=SCHEDULE_LOG_COLS)


def test_plan_export_writes_only_the_given_path_never_a_second_one(tmp_path):
    """Netguard-style review (ARCHITECTURE 7.6): plan_export takes no path
    from the frame's own data, only from its caller's *path* argument."""
    frame = _plan_frame()
    frame["Order"] = "../escape"      # a hostile-looking cell, still just data
    path = tmp_path / "plan.csv"
    planning.plan_export(frame, str(path))
    assert list(tmp_path.iterdir()) == [path]


def test_with_datetimes_adds_date_columns_and_plan_export_forwards_them(tmp_path):
    """ARCHITECTURE 5.23/D81: a calendar model's schedule_log gains "Start
    (date)"/"End (date)" from `export.with_datetimes`, and plan_export turns
    them into Planned Start/End Date beside the numbers."""
    cal_layout = {"simulation": {"calendar": {"start": "2026-01-05T08:00:00",
                                              "timezone": "Europe/Rome"}}}
    dated = export.with_datetimes({"schedule_log": _plan_frame()},
                                   layout=cal_layout)["schedule_log"]
    assert "Start (date)" in dated.columns and "End (date)" in dated.columns

    path = tmp_path / "plan.json"
    planning.plan_export(dated, str(path))
    record = json.loads(path.read_text())[0]
    assert "Planned Start Date" in record and "Planned End Date" in record
