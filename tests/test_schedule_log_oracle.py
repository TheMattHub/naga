"""Independent oracle for Phase 6D W4 P2 (Schedule log, Gantt, plan export),
docs/history/PHASE6-6D-W4-PLAN-2026-09-25.md ("P2 Schedule log, Gantt, plan
export" and "Common rules") and ARCHITECTURE.md 5.16 (orders: CREATE
arrival_mode "Orders", sequences, order_id on entities).

Every expected number below is derived BY HAND from the spec text, never by
running the implementation: P2 does not exist yet (`nagasim/planning.py` is
absent, `svgcharts.gantt` is not in `svgcharts.__all__`, `outputs.schedule`
is not a property of the "outputs" JSON Schema object), and another agent may
be editing the packages for unrelated work while this file is written. Failures
naming a missing attribute/module, a schema `additionalProperties` rejection,
or a numeric mismatch are all EXPECTED until P2 lands -- and a mismatch may
equally be this oracle's own wire-format guess (module constants below), same
as the precedent set by `tests/test_segment_traffic_oracle.py`: the coordinator
reconciles wire format, not this file.

Wire format taken from the plan text plus the existing "outputs" opt-in
precedent (`core/graph_model.py`: top-level `layout["outputs"]["bottleneck"]`,
PHASE6 W3 M2) and from `tests/test_sequences.py` / `tests/test_orders.py`
(CREATE `arrival_mode="Orders"`, `sequence`/`step` shape, PROCESS `setup`
TABLE changeover). `svgcharts.sankey` takes plain tuples; `gantt` is guessed
here as plain dict rows (schedule_log's own column names as keys), matching
what `frame.to_dict("records")` would hand it -- flagged as a guess, not
asserted as the only legal shape.

Horizon (DEVLOG #32): KPIs use the time limit only when events remain
pending past it, else the drain time. Not load-bearing here: every case
below drains well inside its `run_length` (makespan 9 or 37, run_length 20
or 50), so no keeper CREATE/DISPOSE pair is required.

Entity types are upper-cased by `Entity.e_type`'s setter (DEVLOG #87); case 1
deliberately declares a lower-case order `entity_type` to exercise it.
"""
import json

import pandas as pd
import pytest

from conftest import create, dispose, layout

from nagasim import api, svgcharts
from test_sequences import _station, _station_process, _route, _step, _two_product_shop

# -- wire names (spec text, not yet in the codebase) --------------------------
SCHEDULE_LOG_COLS = ["Run", "Order", "Entity", "Entity Type", "Block", "Resource", "Unit",
                     "Start", "End", "Setup"]
PLAN_EXPORT_COLS = ["Order", "Operation", "Resource", "Planned Start", "Planned End"]


# ============================================================ Case 1: flow shop
def _flow_shop_model(run_length=20):
    """A 2-job, 2-machine permutation flow shop (both orders release at 0,
    both routed M1 -> M2 via their own sequence -- ARCHITECTURE 5.16 -- so
    the per-job times are NOT hard to express: each order just names its own
    sequence). J1 = (M1 3, M2 2), J2 = (M1 2, M2 4) -- the textbook 2x2
    example. Both machines are single-server FIFO, so with J1 declared (and
    therefore released) before J2 at the same instant, J1 goes first at
    every machine:

        M1 (R1): J1 0->3,  J2 3->5   (J2's own M1 time is 2)
        M2 (R2): J1 3->5,  J2 5->9   (J1 frees M2 at 5; J2 arrives at 5 too)

    No setup is declared on either PROCESS, so Setup is 0.0 throughout.
    """
    blocks = [
        create(name="release", arrival_mode="Orders", max_arrivals=0),
        _route(),
        _station("M1"), _station("M2"),
        _station_process("p1", "R1"), _station_process("p2", "R2"),
        dispose(),
    ]
    links = [{"source": "release", "target": "route_1"},
             {"source": "route_1", "target": "dispose_1"},
             {"source": "M1", "target": "p1"}, {"source": "M2", "target": "p2"},
             {"source": "p1", "target": "route_1"}, {"source": "p2", "target": "route_1"}]
    return layout(
        blocks, links,
        resources=[{"name": "R1", "capacity": 1, "discipline": "FIFO"},
                   {"name": "R2", "capacity": 1, "discipline": "FIFO"}],
        sequences=[{"name": "SeqJ1", "steps": [_step("M1", 3.0), _step("M2", 2.0)]},
                   {"name": "SeqJ2", "steps": [_step("M1", 2.0), _step("M2", 4.0)]}],
        orders=[{"id": "J1", "release": 0, "due": 100, "quantity": 1,
                 "entity_type": "widget", "sequence": "SeqJ1"},
                {"id": "J2", "release": 0, "due": 100, "quantity": 1,
                 "entity_type": "widget", "sequence": "SeqJ2"}],
        outputs={"schedule": True},
        simulation={"time_limit": run_length, "warmup": 0, "replications": 1})


def test_two_job_two_machine_flow_shop_schedule_log_is_exact_by_hand():
    results = api.run_model(_flow_shop_model(), replications=1, seed=0, run_length=20)
    sched = results["schedule_log"]
    assert list(sched.columns) == SCHEDULE_LOG_COLS

    rows = sched.set_index(["Order", "Block"])
    expected = {
        ("J1", "p1"): (0.0, 3.0), ("J2", "p1"): (3.0, 5.0),
        ("J1", "p2"): (3.0, 5.0), ("J2", "p2"): (5.0, 9.0),
    }
    for key, (start, end) in expected.items():
        row = rows.loc[key]
        assert (row["Start"], row["End"]) == pytest.approx((start, end))
        assert row["Setup"] == pytest.approx(0.0)
        assert row["Entity Type"] == "WIDGET"          # e_type upper-cased (DEVLOG #87)
    assert rows.loc[("J1", "p1")]["Resource"] == "R1"
    assert rows.loc[("J1", "p2")]["Resource"] == "R2"


# ============================================================ Case 2: opt-out shape
def test_without_outputs_schedule_the_frame_is_empty_with_fixed_columns(simple_model_path):
    """The frame-shape promise (A3/E1/W3 M2 precedent, restated by "Common
    rules"): a model that never sets `outputs.schedule` gets the frame back
    EMPTY with its fixed columns, so no existing model's KPI keys, workbook
    sheet count or golden hash moves."""
    results = api.run_model(simple_model_path, replications=1, run_length=20)
    assert results["schedule_log"].empty
    assert list(results["schedule_log"].columns) == SCHEDULE_LOG_COLS


# ============================================================ Case 3: gantt SVG
def _plain_rows():
    """The schedule_log rows from case 1, as plain dicts -- what a
    `frame.to_dict("records")` of that frame would hand `svgcharts.gantt`."""
    base = {"Run": 0, "Entity": 0, "Entity Type": "WIDGET", "Unit": 0, "Setup": 0.0}
    return [
        {**base, "Order": "J1", "Block": "p1", "Resource": "R1", "Start": 0.0, "End": 3.0},
        {**base, "Order": "J2", "Block": "p1", "Resource": "R1", "Start": 3.0, "End": 5.0},
        {**base, "Order": "J1", "Block": "p2", "Resource": "R2", "Start": 3.0, "End": 5.0},
        {**base, "Order": "J2", "Block": "p2", "Resource": "R2", "Start": 5.0, "End": 9.0},
    ]


@pytest.mark.parametrize("by", ["resource", "order"])
def test_gantt_returns_ascii_svg_with_one_bar_per_row(by):
    rows = _plain_rows()
    svg = svgcharts.gantt(rows, by=by)
    assert isinstance(svg, str) and svg.isascii()
    assert svg.strip().startswith("<svg") and "</svg>" in svg
    # One bar (rect) per schedule row -- the property the plan text names
    # directly. A legend/axis rect, if any, would break this exact count;
    # that is a plausible wire-format guess for the coordinator to reconcile.
    assert svg.count("<rect") == len(rows)


# ============================================================ Case 4: plan_export
def _plan_frame():
    """A schedule_log-shaped frame built directly from case 1's hand-derived
    numbers (bypassing the run, since `planning.plan_export` is a pure
    function of a frame -- it does not need the engine to exist)."""
    rows = _plain_rows()
    for r in rows:
        r["Run"] = 0
    return pd.DataFrame(rows, columns=SCHEDULE_LOG_COLS)


def _expected_plan_rows(frame):
    return {(r.Order, r.Block, r.Resource, r.Start, r.End) for r in frame.itertuples()}


def test_plan_export_writes_csv_with_matching_rows_and_columns(tmp_path):
    from nagasim import planning
    frame = _plan_frame()
    path = tmp_path / "plan.csv"
    planning.plan_export(frame, str(path))

    out = pd.read_csv(path)
    assert list(out.columns) == PLAN_EXPORT_COLS
    assert len(out) == len(frame)
    # itertuples renames spaced columns, so compare by position instead.
    got = {(row.Order, row.Operation, row.Resource, row[3], row[4])
           for row in out.itertuples(index=False)}
    assert got == _expected_plan_rows(frame)


def test_plan_export_writes_json_with_matching_rows_and_columns(tmp_path):
    from nagasim import planning
    frame = _plan_frame()
    path = tmp_path / "plan.json"
    planning.plan_export(frame, str(path))

    records = json.loads(path.read_text())
    assert isinstance(records, list) and len(records) == len(frame)
    assert set(records[0].keys()) == set(PLAN_EXPORT_COLS)
    got = {(r["Order"], r["Operation"], r["Resource"], r["Planned Start"], r["Planned End"])
           for r in records}
    assert got == _expected_plan_rows(frame)


# ============================================================ Case 5: setup share
def test_a_changeover_setup_leaves_its_share_in_the_setup_column():
    """Reuses `tests/test_sequences.py`'s already hand-verified two-product
    shop (A -> S1(2) -> S2(3); B -> S2(4) -> S1(1) -> S3(5), a 7-unit
    changeover TABLE("Setups","A","B") charged at S1 because the station is
    still keyed "A" when B arrives): B's S2 hold (its first stop) is plain
    service, 20->24, Setup 0; its S1 hold pays the whole changeover FIRST,
    so it spans the setup AND the service, 24->32, of which the setup SHARE
    is exactly 7.0 (the service itself is 1)."""
    model = _two_product_shop(setup='TABLE("Setups", previous_key, key)')
    model["outputs"] = {"schedule": True}
    results = api.run_model(model, replications=1, seed=0, run_length=50)
    sched = results["schedule_log"]

    b_p1 = sched[(sched["Block"] == "p1") & ((sched["Start"] - 24.0).abs() < 1e-9)]
    assert len(b_p1) == 1
    row = b_p1.iloc[0]
    assert (row["Start"], row["End"]) == pytest.approx((24.0, 32.0))
    assert row["Setup"] == pytest.approx(7.0)

    a_p1 = sched[(sched["Block"] == "p1") & (sched["Start"].abs() < 1e-9)]
    assert a_p1.iloc[0]["Setup"] == pytest.approx(0.0)      # initial_key "A": no changeover
