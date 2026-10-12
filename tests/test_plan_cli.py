"""Tests for the `naga plan` CLI (Phase 6D W4 P5, ARCHITECTURE 5.29-5.32/D123).

A small two-order, one-resource model (the `test_plan_risk.py` precedent):
fast, headless, no `slow` mark. The `--propose` path is
`pytest.importorskip("ortools")`, per the plan text (P5's `ortools` is a lazy
optional dependency, D122).
"""
import json

import pytest

from conftest import chain, create, dispose, layout, process

from nagasim import planning

MODEL_ORDERS = [
    {"id": "J1", "release": 0, "due": 5, "quantity": 1, "attributes": {"svc": 3.0}},
    {"id": "J2", "release": 0, "due": 6, "quantity": 1, "attributes": {"svc": 4.0}},
]


def _two_order_model():
    create_blk = create(name="release", arrival_mode="Orders", max_arrivals=0)
    proc = process(resource="Server", value="entity.svc", distribution="Constant")
    dispose_blk = dispose()
    return layout(
        [create_blk, proc, dispose_blk], chain(create_blk, proc, dispose_blk),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        orders=MODEL_ORDERS,
        simulation={"time_limit": 30, "warmup": 0, "replications": 1},
    )


@pytest.fixture
def model_path(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps(_two_order_model()))
    return str(path)


def test_orders_csv_replaces_the_order_book(model_path, tmp_path, capsys):
    orders_csv = tmp_path / "orders.csv"
    orders_csv.write_text("id,release,due,quantity,svc\nJ3,0,20,1,2.0\n")

    assert planning.main([model_path, "--orders", str(orders_csv)]) == 0
    out = capsys.readouterr().out
    assert "J3" in out and "J1" not in out


def test_prints_the_schedule_log_then_the_order_risk_table(model_path, capsys):
    assert planning.main([model_path, "--risk", "3"]) == 0
    out = capsys.readouterr().out
    assert "Server" in out                                           # schedule log
    assert "P(On Time)" in out                                       # risk table
    assert "J1" in out and "J2" in out


def test_export_writes_the_plan_csv(model_path, tmp_path):
    out = tmp_path / "plan.csv"
    assert planning.main([model_path, "--export", str(out)]) == 0
    text = out.read_text()
    assert "Order,Operation,Resource,Planned Start,Planned End" in text
    assert "J1" in text and "J2" in text


def test_report_writes_the_gantt_and_risk_table(model_path, tmp_path):
    out = tmp_path / "report.html"
    assert planning.main([model_path, "--risk", "2", "--report", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "<html" in text.lower()
    assert "Schedule" in text
    assert "Plan risk" in text


def test_propose_reproduces_case2_and_exports_the_proposed_plan(tmp_path, capsys):
    """A minimal job-shop instance (one order, one station) so CP-SAT has
    something to plan: the proposed plan's own schedule (not the as-is one)
    is what --export/--risk end up carrying (--risk replays the Plan layout
    stochastically)."""
    pytest.importorskip("ortools")
    from test_sequences import _route, _station, _station_process, _step

    create_blk = create(name="release", arrival_mode="Orders", max_arrivals=0)
    route_blk = _route()
    station = _station("M1")
    proc = _station_process("p1", "R1")
    dispose_blk = dispose()
    links = [{"source": "release", "target": "route_1"},
             {"source": "route_1", "target": "dispose_1"},
             {"source": "M1", "target": "p1"}, {"source": "p1", "target": "route_1"}]
    sequences = [{"name": "seq", "steps": [_step("M1", 3.0)]}]
    orders = [{"id": "A", "release": 0.0, "due": 10.0, "quantity": 1, "entity_type": "A",
              "sequence": "seq", "priority": 1}]
    resources = [{"name": "R1", "capacity": 1, "discipline": "FIFO"}]
    model = layout([create_blk, route_blk, station, proc, dispose_blk], links,
                   resources=resources, sequences=sequences, orders=orders,
                   simulation={"time_limit": 30, "warmup": 0, "replications": 1})
    path = tmp_path / "job_shop.json"
    path.write_text(json.dumps(model))
    out = tmp_path / "plan.json"

    rc = planning.main([str(path), "--propose", "makespan", "--time-limit", "2",
                        "--risk", "2", "--export", str(out)])
    assert rc == 0
    assert "P(On Time)" in capsys.readouterr().out
    records = json.loads(out.read_text())
    assert records and records[0]["Order"] == "A"
    assert records[0]["Planned End"] == pytest.approx(3.0)


def test_propose_missing_ortools_exits_2_with_a_clear_message(model_path, monkeypatch, capsys):
    import sys

    monkeypatch.setitem(sys.modules, "ortools", None)   # forces `import ortools` to raise
    rc = planning.main([model_path, "--propose", "makespan"])
    assert rc == 2
    assert "ortools" in capsys.readouterr().err.lower()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
