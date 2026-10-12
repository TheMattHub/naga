"""Independent oracle for Phase 6D W4 P3 (Plan risk),
docs/history/PHASE6-6D-W4-PLAN-2026-09-25.md ("P3 Plan risk" and "Common
rules") and ARCHITECTURE.md 5.16 (orders, due dates, order_summary).

Every expected number below is derived BY HAND (or from a closed-form
statistical formula applied to a by-hand true value), never by running the
implementation: `nagasim/planning.py` has no `plan_risk` yet (P1's
deterministic mode and P2's schedule log are being built by other agents in
parallel -- P3 is scheduled strictly after both). An ImportError/AttributeError
right now is EXPECTED; the coordinator reconciles any real wire mismatch
against what P3 actually ships.

Wire format taken from the plan text, `tests/test_deterministic_oracle.py` (the
`simulation.deterministic` wire) and `tests/test_schedule_log_oracle.py` (frame/
column guessing precedent) plus `tests/test_orders.py` /
`nagasim/models/order_due_dates.json` for the `orders` section shape (a
simple, single-station order needs no `sequence`/ROUTE/STATION at all).

Two explicit readings this file commits to, since the plan text is silent on
both:

1. LATENESS. `core/outputs.py`'s `summarize_orders` defines "Lateness" as
   `completion - due`, SIGNED (earliness is negative), and "Tardiness" as its
   clipped positive part -- two different, deliberately named columns. The P3
   spec text says "Mean Lateness" / "P90 Lateness", never "Tardiness", so this
   oracle reads it as the SAME signed quantity `end - due` (module constant
   `LATENESS_IS_SIGNED = True` below), not clipped at 0. "On time" mirrors
   order_summary's `Late = int(lateness > 0)`: `end <= due` counts as on time
   (a job finishing exactly at its due date is not late).
2. RETURN SHAPE. `plan_risk` "returns frame `order_risk`" (singular, no
   mention of a dict of frames the way `api.run_model` returns one). This
   oracle's primary reading is that it returns the `order_risk` DataFrame
   directly (matching `plan_export(frame, path)`'s plain-frame convention,
   P2). `_order_risk_frame()` below also accepts a `{"order_risk": df}`
   dict/Results in case the implementer instead follows `api.run_model`'s
   dict-of-frames convention -- flagged as a guess, not asserted as the only
   legal shape (same precedent as P2's `svgcharts.gantt` row-shape guess).
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from conftest import chain, create, dispose, layout, process

# ---------------------------------------------------------------- wire names
ORDER_RISK_FRAME = "order_risk"
ORDER_RISK_COLS = ["Order", "Due", "Planned End", "P(On Time)", "Mean Lateness",
                   "P90 Lateness", "Mean End"]
LATENESS_IS_SIGNED = True   # end - due, matching order_summary's "Lateness" (see module docstring)


def _order_risk_frame(result):
    """Accepts either reading 2's plain-DataFrame return or a dict/Results
    keyed by ORDER_RISK_FRAME -- see the module docstring."""
    if isinstance(result, pd.DataFrame):
        return result
    return result[ORDER_RISK_FRAME]


def _one_order_model(distribution, value, due, resource="Server"):
    create_blk = create(name="release", arrival_mode="Orders", max_arrivals=0)
    proc = process(resource=resource, value=value, distribution=distribution)
    dispose_blk = dispose()
    return layout(
        [create_blk, proc, dispose_blk], chain(create_blk, proc, dispose_blk),
        resources=[{"name": resource, "capacity": 1, "discipline": "FIFO"}],
        orders=[{"id": "WO-1", "release": 0, "due": due, "quantity": 1}],
    )


# ---- case 1: single exponential order, P(on time)/Mean End against closed form
@pytest.mark.slow
def test_case1_exponential_single_order_matches_closed_form():
    """One order (quantity 1), one machine, Exponential service rate mu=1,
    due d=1, release 0. No queueing is possible (one order, one entity), so
    the completion time IS the raw service draw:

      Planned End (deterministic: every distribution returns its mean,
      D119)             = 1/mu = 1.0
      True P(on time)   = P(service <= d) = CDF_Exp(d) = 1 - exp(-mu*d)
      True Mean End     = E[service] = 1/mu = 1.0
      True Mean Lateness = Mean End - d = 0.0 (linearity: Lateness_i = end_i - d
                            for every replication, so the sample mean is exactly
                            Mean End - d)

    N=300 replications with a binomial 4-sigma tolerance on P(on time) and a
    CLT 4-sigma tolerance (population std of Exponential(mu) is 1/mu) on the
    two means -- both formulas applied to the analytically exact true values,
    not read off a run."""
    from nagasim import planning

    mu, d, n = 1.0, 1.0, 300
    model = _one_order_model("Exponential", mu, d)

    result = planning.plan_risk(model, replications=n, seed=0)
    row = _order_risk_frame(result).set_index("Order").loc["WO-1"]

    assert row["Due"] == pytest.approx(d)
    assert row["Planned End"] == pytest.approx(1.0 / mu)

    true_p = 1.0 - math.exp(-mu * d)
    p_tol = 4.0 * math.sqrt(true_p * (1.0 - true_p) / n)
    assert row["P(On Time)"] == pytest.approx(true_p, abs=p_tol)

    true_mean_end = 1.0 / mu
    mean_tol = 4.0 * (1.0 / mu) / math.sqrt(n)   # population std of Exp(mu) is 1/mu
    assert row["Mean End"] == pytest.approx(true_mean_end, abs=mean_tol)
    assert row["Mean Lateness"] == pytest.approx(true_mean_end - d, abs=mean_tol)

    # Sanity, not a hand derivation: Exponential is right-skewed, so its P90
    # sits above its mean -- P90 Lateness should exceed Mean Lateness.
    assert row["P90 Lateness"] > row["Mean Lateness"]


# ---- case 2: constant service, P(on time) exactly 0 or 1
def test_case2_constant_service_gives_exact_on_time_probability():
    """No randomness anywhere (Constant distribution), so every one of the N
    replications produces the identical completion time = the service value:
    P(on time) is then exactly 1.0 or 0.0, no tolerance needed. Two
    independent single-order models (own resource each, `_one_order_model`)
    isolate the two directions: service 3 under due 10 (on time), service 3
    over due 1 (late)."""
    from nagasim import planning

    ontime = planning.plan_risk(_one_order_model("Constant", 3.0, due=10.0), replications=5, seed=0)
    late = planning.plan_risk(_one_order_model("Constant", 3.0, due=1.0), replications=5, seed=0)

    row_ontime = _order_risk_frame(ontime).set_index("Order").loc["WO-1"]
    row_late = _order_risk_frame(late).set_index("Order").loc["WO-1"]

    assert row_ontime["Planned End"] == pytest.approx(3.0)
    assert row_ontime["P(On Time)"] == pytest.approx(1.0)
    assert row_late["Planned End"] == pytest.approx(3.0)
    assert row_late["P(On Time)"] == pytest.approx(0.0)


# ---- case 3 + 4: two orders, one machine, FIFO; Planned End by hand; columns exact
def _two_order_fifo_model():
    """J1 and J2 both release at 0 through the SAME PROCESS/resource (one
    station, so no per-order routing is needed): `build_orders` keeps the
    `orders` section's declaration order (a plain dict, ARCHITECTURE 5.16 /
    `core/orders.py`), and both release at the same instant, so J1 -- listed
    first -- is released first and wins FIFO. Each order's own service time
    rides along as an ordinary released-entity attribute (ARCHITECTURE 5.16:
    "the order's free `attributes`"), read back by the PROCESS's Constant
    `dist_params.value = "entity.svc"` (an expression string, same mechanism
    `order_due_dates.json` uses for `entity.process_time`):

        Server: J1 0->3 (service 3), J2 3->7 (service 4, queued 0->3)

    Planned End: J1 = 3, J2 = 7 (deterministic mode: Constant has no
    randomness to average over, so this is also every replication's actual
    end). due J1=5 -> on time (3<=5); due J2=6 -> late (7>6)."""
    create_blk = create(name="release", arrival_mode="Orders", max_arrivals=0)
    proc = process(resource="Server", value="entity.svc", distribution="Constant")
    dispose_blk = dispose()
    return layout(
        [create_blk, proc, dispose_blk], chain(create_blk, proc, dispose_blk),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        orders=[{"id": "J1", "release": 0, "due": 5, "quantity": 1, "attributes": {"svc": 3.0}},
                {"id": "J2", "release": 0, "due": 6, "quantity": 1, "attributes": {"svc": 4.0}}],
    )


def test_case3_two_orders_fifo_planned_end_and_on_time_by_hand():
    from nagasim import planning

    result = planning.plan_risk(_two_order_fifo_model(), replications=5, seed=0)
    frame = _order_risk_frame(result).set_index("Order")

    assert frame.loc["J1", "Planned End"] == pytest.approx(3.0)
    assert frame.loc["J1", "P(On Time)"] == pytest.approx(1.0)
    assert frame.loc["J1", "Mean Lateness"] == pytest.approx(3.0 - 5.0)

    assert frame.loc["J2", "Planned End"] == pytest.approx(7.0)
    assert frame.loc["J2", "P(On Time)"] == pytest.approx(0.0)
    assert frame.loc["J2", "Mean Lateness"] == pytest.approx(7.0 - 6.0)


def test_case4_frame_columns_exact_one_row_per_order():
    from nagasim import planning

    result = planning.plan_risk(_two_order_fifo_model(), replications=5, seed=0)
    frame = _order_risk_frame(result)

    assert list(frame.columns) == ORDER_RISK_COLS
    assert len(frame) == 2
    assert set(frame["Order"]) == {"J1", "J2"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
