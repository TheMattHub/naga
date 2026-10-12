"""Tests for `planning.plan_risk` (Phase 6D W4 P3, ARCHITECTURE 5.31/D121).

The hand-derived numbers (constant and exponential service, two FIFO orders,
the frame's columns) live in tests/test_plan_risk_oracle.py; DEVLOG #208 (no due
date) in test_regressions.py. This file keeps the edges: no orders, workers.
"""
from conftest import chain, create, dispose, layout, process

from nagasim import planning

RISK_COLS = ["Order", "Due", "Planned End", "P(On Time)", "Mean Lateness",
             "P90 Lateness", "Mean End"]


def _one_order_model(distribution, value, due, resource="Server"):
    create_blk = create(name="release", arrival_mode="Orders", max_arrivals=0)
    proc = process(resource=resource, value=value, distribution=distribution)
    dispose_blk = dispose()
    return layout(
        [create_blk, proc, dispose_blk], chain(create_blk, proc, dispose_blk),
        resources=[{"name": resource, "capacity": 1, "discipline": "FIFO"}],
        orders=[{"id": "WO-1", "release": 0, "due": due, "quantity": 1}],
    )


def test_no_orders_returns_empty_frame_with_fixed_columns():
    create_blk = create(name="c1", max_arrivals=3)
    proc = process()
    dispose_blk = dispose()
    model = layout([create_blk, proc, dispose_blk], chain(create_blk, proc, dispose_blk),
                   resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
                   simulation={"replications": 1, "run_length": 20, "warmup": 0})

    frame = planning.plan_risk(model, replications=3, seed=0)

    assert list(frame.columns) == RISK_COLS
    assert frame.empty


def test_workers_greater_than_one_matches_serial():
    model = _one_order_model("Constant", 3.0, due=10.0)
    serial = planning.plan_risk(model, replications=4, seed=0, workers=1)
    pooled = planning.plan_risk(model, replications=4, seed=0, workers=2)

    serial_row = serial.set_index("Order").loc["WO-1"]
    pooled_row = pooled.set_index("Order").loc["WO-1"]
    assert serial_row["Mean End"] == pooled_row["Mean End"]
    assert serial_row["P(On Time)"] == pooled_row["P(On Time)"]


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
