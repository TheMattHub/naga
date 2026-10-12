"""Costing (ROADMAP 2.2): resource busy/idle/usage cost, entity cost
accumulation (resource share, usage share, WIP holding, ASSIGN-added), and the
new cost KPIs across system/resource/entity frames.

Runtime budget: fast tests stay a few seconds; the tight statistical check is
marked slow (run with `-m "slow or not slow"`).
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from nagasim.core import outputs
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, run_single_simulation, step_until


from conftest import chain, create, dispose, layout, metric_value, process, resource_row

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _entity_row(kpi, e_type="DEFAULT"):
    df = kpi["entity_summary"]
    return df[df["Entity Type"] == e_type].iloc[0]


# ---------------------------------------------------------------- D/D/1 exact case
def test_dd1_hand_computed_resource_entity_and_system_costs():
    """Deterministic D/D/1: 3 arrivals at t=0,10,20 (Constant interarrival 10),
    service 4 min each, capacity 1 -> no queueing, drains at t=24. Hand
    computed with cost_time_unit="hours" (conversion factor 60 against the
    model's minutes base) and a holding cost per minute in system."""
    blocks = [create(max_arrivals=3, dist_params={"value": 10.0}),
             process(value=4.0),
             dispose()]
    links = chain(*blocks)
    resources = [{"name": "Server", "capacity": 1, "discipline": "FIFO",
                 "cost_busy": 60.0, "cost_idle": 12.0, "cost_per_use": 2.0,
                 "cost_time_unit": "hours"}]
    entities = [{"name": "DEFAULT", "holding_cost": 0.5}]
    model = layout(blocks, links, resources=resources, entities=entities)

    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)

    # sim drains at t=24; busy_time=3*4=12min, available_area=1*24=24min,
    # idle_time=12min; conv(hours->minutes)=60
    row = resource_row(kpi, "Server")
    assert row["Busy Cost"] == pytest.approx(60.0 * 12 / 60, abs=1e-6)     # 12.0
    assert row["Idle Cost"] == pytest.approx(12.0 * 12 / 60, abs=1e-6)     # 2.4
    assert row["Usage Cost"] == pytest.approx(2.0 * 3, abs=1e-6)          # 6.0
    assert row["Total Cost"] == pytest.approx(12.0 + 2.4 + 6.0, abs=1e-6)  # 20.4

    # each entity: busy share 60*1*4/60=4, usage share 2, holding 0.5*4=2 -> 8 each
    ent = _entity_row(kpi)
    assert ent["Total Completed"] == 3
    assert ent["Avg Cost"] == pytest.approx(8.0, abs=1e-6)
    assert ent["Total Cost"] == pytest.approx(24.0, abs=1e-6)

    log = kpi["entity_log"]
    assert list(log["Cost"]) == pytest.approx([8.0, 8.0, 8.0], abs=1e-6)

    assert metric_value(kpi, "Total Resource Cost") == pytest.approx(20.4, abs=1e-6)
    assert metric_value(kpi, "Total Entity Holding Cost") == pytest.approx(6.0, abs=1e-6)
    assert metric_value(kpi, "Total Cost") == pytest.approx(26.4, abs=1e-6)
    assert metric_value(kpi, "Cost per Completed Entity") == pytest.approx(26.4 / 3, abs=1e-6)


def test_capacity_schedule_off_period_costs_nothing():
    """A resource off shift (capacity_schedule value 0) accrues neither busy
    nor idle cost for that period -- `available_area` excludes it entirely."""
    from nagasim.core.resource_manager import ResourceManager
    from nagasim.core.schedules import Schedule
    from nagasim.core.context import build_context

    ctx = build_context(seed=0)
    env = ctx.env
    env.ctx = ctx
    schedule = Schedule("shift", "capacity", [[10, 0], [10, 1]], repeat=False,
                        base_unit="minutes")
    manager = ctx.resource_manager
    res = manager.register("Server", capacity=1, cost_busy=60.0, cost_idle=12.0,
                           cost_time_unit="hours")
    from nagasim.core.resource_manager import capacity_controller
    env.process(capacity_controller(res, schedule))
    env.run(until=10)   # off for the first 10 minutes: no capacity available at all

    busy, idle, usage, total = res.cost_totals(env.now, "minutes")
    assert busy == pytest.approx(0.0, abs=1e-9)
    assert idle == pytest.approx(0.0, abs=1e-9)   # off time is not "idle", it costs nothing
    assert total == pytest.approx(0.0, abs=1e-9)

    env.run(until=20)   # on (capacity 1) for the next 10 minutes, never seized -> idle only
    busy, idle, usage, total = res.cost_totals(env.now, "minutes")
    assert busy == pytest.approx(0.0, abs=1e-9)
    assert idle == pytest.approx(12.0 * 10 / 60, abs=1e-6)   # 2.0


# ---------------------------------------------------------------- conservation
def _mm2_layout(*, cost_busy=10.0, cost_time_unit="minutes", max_arrivals=40):
    blocks = [create(distribution="Exponential", dist_params={"rate": 1.0}, max_arrivals=max_arrivals),
             process(distribution="Exponential", value=0.7),
             dispose()]
    links = chain(*blocks)
    resources = [{"name": "Server", "capacity": 2, "discipline": "FIFO",
                 "cost_busy": cost_busy, "cost_time_unit": cost_time_unit}]
    return layout(blocks, links, resources=resources)


def test_entity_cost_shares_sum_to_resource_busy_cost_when_drained():
    """M/M/2, only cost_busy set: with the model fully drained (no entity mid-
    service at report time), completed entities' cost shares sum exactly to
    the resource's own Busy Cost (== Total Cost here, idle/usage both 0)."""
    model = _mm2_layout()
    kpi = run_single_simulation(model, seed=1, run_len=0, warmup=0)

    resource_total = resource_row(kpi, "Server")["Total Cost"]
    entity_total = _entity_row(kpi)["Total Cost"]
    # resource_summary rounds to 4 decimals (write_sheet cosmetics apply later,
    # but summarize_resources itself already rounds); entity totals do not.
    assert entity_total == pytest.approx(resource_total, abs=5e-5)


def test_entity_cost_shares_sum_to_resource_busy_cost_mid_service():
    """Same conservation, but the run is cut short so some entities are still
    mid-service: report time's pending projection (outputs.entity_cost_asof)
    mirrors exactly how the resource's own `busy.area(t_end)` extends to
    t_end, so the sums still agree to within 1e-6 (documented: the projection,
    not a real accrual, is what keeps this exact)."""
    model = _mm2_layout(max_arrivals=0)   # unbounded: guarantees the run is cut mid-flight
    env = initialize_sim_env(seed=2)
    prep = prepare_model(env, model)
    for start in prep.start_blocks():
        start.self_run()
    step_until(env, run_len=60)   # short: several entities still queued/in service

    server = env.ctx.resource_manager.get("Server")
    resource_busy = server.cost_totals(env.now, env.ctx.time_unit)[0]
    entity_total = sum(outputs.entity_cost_asof(e, env.ctx, env.now)[0]
                       for e in env.ctx.all_entities if getattr(e, "in_batch", None) is None)
    # at least one entity must still be mid-service for this to be a real check
    assert any(getattr(e, "req_params", None) for e in env.ctx.live_entities)
    assert entity_total == pytest.approx(resource_busy, abs=1e-6)


# ---------------------------------------------------------------- ASSIGN-added cost
def test_assign_added_cost_appears_in_total_cost_exactly_once():
    """`entity.cost = entity.cost + 12.5` (no resource/holding cost declared):
    system Total Cost must equal N x 12.5, not double it via any resource-share
    bookkeeping (there is none here) or a stray re-add."""
    blocks = [
        create(max_arrivals=4, dist_params={"value": 1.0}),
        {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [1, 0],
         "params": {"assignments": [{"target": "entity.cost", "expr": "entity.cost + 12.5"}]}},
        dispose(),
    ]
    links = chain(*blocks)
    model = layout(blocks, links)

    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)

    assert metric_value(kpi, "Total Entities Completed") == 4
    assert metric_value(kpi, "Total Cost") == pytest.approx(4 * 12.5, abs=1e-9)
    assert _entity_row(kpi)["Total Cost"] == pytest.approx(4 * 12.5, abs=1e-9)


# ---------------------------------------------------------------- Batch / Separate
def _batch_assign_layout(with_separate: bool):
    create_blk = create(max_arrivals=2, dist_params={"value": 1.0})
    assign_blk = {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [1, 0],
                 "params": {"assignments": [{"target": "entity.cost", "expr": "10"}]}}
    batch_blk = {"name": "batch_1", "description": "b", "type": "BATCH", "position": [2, 0],
                "params": {"batch_size": 2, "batched_entity_type": "BATCH"}}
    dispose_blk = dispose()
    blocks = [create_blk, assign_blk, batch_blk]
    if with_separate:
        separate_blk = {"name": "separate_1", "description": "s", "type": "SEPARATE", "position": [3, 0],
                        "params": {"mode": "Retain original entity values", "attrs": []}}
        blocks += [separate_blk, dispose_blk]
    else:
        blocks += [dispose_blk]
    links = chain(*blocks)
    return layout(blocks, links)


def test_permanent_batch_sums_member_costs():
    model = _batch_assign_layout(with_separate=False)
    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)
    log = kpi["entity_log"]
    batch_row = log[log["Type"] == "BATCH"].iloc[0]
    assert batch_row["Cost"] == pytest.approx(20.0, abs=1e-9)   # 10 + 10


def test_consumed_batch_member_cost_stays_frozen_after_absorption():
    """A member's own log row must not keep accruing pending holding cost
    after it is absorbed into a batch -- its holding cost moved into the
    batch entity the instant it was absorbed (`make_batch_entity`), and it
    never reaches Dispose itself to realize any more of its own."""
    create_blk = create(max_arrivals=1, entities_per_arrival=2, dist_params={"value": 0.0})
    assign_blk = {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [1, 0],
                 "params": {"assignments": [{"target": "entity.cost", "expr": "10"}]}}
    batch_blk = {"name": "batch_1", "description": "b", "type": "BATCH", "position": [2, 0],
                "params": {"batch_size": 2, "batched_entity_type": "BATCH"}}
    delay_blk = {"name": "delay_1", "description": "w", "type": "DELAY", "position": [3, 0],
                "params": {"distribution": "Constant", "dist_params": {"value": 20.0}}}
    dispose_blk = dispose()
    blocks = [create_blk, assign_blk, batch_blk, delay_blk, dispose_blk]
    links = chain(*blocks)
    entities = [{"name": "DEFAULT", "holding_cost": 0.5}]
    model = layout(blocks, links, entities=entities)

    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)

    log = kpi["entity_log"]
    consumed_rows = log[log["Type"] == "DEFAULT"]
    assert len(consumed_rows) == 2
    # frozen at the batch-time ASSIGN value (10), NOT growing with the 20 min
    # the batch entity then spent waiting before Dispose
    assert list(consumed_rows["Cost"]) == pytest.approx([10.0, 10.0], abs=1e-9)


def test_separate_splits_batch_cost_evenly_and_zeroes_the_parent():
    model = _batch_assign_layout(with_separate=True)
    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)
    log = kpi["entity_log"]

    batch_row = log[log["Type"] == "BATCH"].iloc[0]
    assert batch_row["Cost"] == pytest.approx(0.0, abs=1e-9)   # spent, split onto children

    default_rows = log[log["Type"] == "DEFAULT"]
    assert len(default_rows) == 2
    assert list(default_rows["Cost"]) == pytest.approx([10.0, 10.0], abs=1e-9)


# ---------------------------------------------------------------- warm-up
def test_warmup_excludes_pre_warmup_cost():
    """One entity, a single 10-minute service straddling warm-up at t=6: both
    the resource's own Busy Cost and the entity's share must reflect only the
    post-warm-up 4 minutes."""
    blocks = [create(max_arrivals=1, dist_params={"value": 0.0}),
             process(value=10.0),
             dispose()]
    links = chain(*blocks)
    resources = [{"name": "Server", "capacity": 1, "discipline": "FIFO",
                 "cost_busy": 60.0, "cost_time_unit": "hours"}]
    model = layout(blocks, links, resources=resources)

    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=6)

    expected = 60.0 * 4 / 60   # 4.0: only the post-warmup portion of the 10 min service
    assert resource_row(kpi, "Server")["Busy Cost"] == pytest.approx(expected, abs=1e-6)
    assert _entity_row(kpi)["Total Cost"] == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------- zero-cost models
def test_zero_cost_model_keeps_cost_columns_at_zero(simple_model_path):
    kpi = run_single_simulation(simple_model_path, seed=0, run_len=0, warmup=0)

    row = kpi["resource_summary"]
    assert row.empty   # simple_model_path has no SEIZE/PROCESS at all

    ent = _entity_row(kpi, "DEFAULT")
    assert ent["Avg Cost"] == 0.0
    assert ent["Total Cost"] == 0.0
    assert list(kpi["entity_log"]["Cost"]) == [0.0] * len(kpi["entity_log"])

    assert metric_value(kpi, "Total Resource Cost") == 0.0
    assert metric_value(kpi, "Total Entity Holding Cost") == 0.0
    assert metric_value(kpi, "Total Cost") == 0.0
    assert metric_value(kpi, "Cost per Completed Entity") == 0.0


def test_zero_cost_resource_row_present_with_zero_columns():
    """A model that DOES seize a resource, but declares no cost fields: the
    columns are present (not missing), reading 0.0."""
    model = _mm2_layout(cost_busy=0.0)
    kpi = run_single_simulation(model, seed=0, run_len=0, warmup=0)
    row = resource_row(kpi, "Server")
    for col in ("Busy Cost", "Idle Cost", "Usage Cost", "Total Cost"):
        assert row[col] == 0.0


# ---------------------------------------------------------------- migration
def test_v9_to_v10_migration_leaves_single_server_kpis_unchanged():
    """single_server.json as it shipped at v9, with no cost fields (a frozen
    copy: the bundled file is re-saved at the current schema since the
    architecture review's D3). Migrating it to v10 (currency defaults to "EUR",
    every cost input defaults to 0) must not change any pre-existing KPI value
    -- only add the new cost columns."""
    raw = json.loads((Path(__file__).parent / "data" / "single_server_v9.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 9

    sim = raw["simulation"]
    kpi_from_v9 = run_single_simulation(raw, seed=42, run_len=sim["time_limit"], warmup=sim["warmup"])

    v10_equivalent = {**raw, "schema_version": 10,
                      "simulation": {**sim, "currency": "EUR"}}
    kpi_from_v10 = run_single_simulation(v10_equivalent, seed=42, run_len=sim["time_limit"],
                                         warmup=sim["warmup"])

    cost_cols = {"Busy Cost", "Idle Cost", "Usage Cost", "Total Cost", "Avg Cost", "Cost"}
    for key in kpi_from_v9:
        df1 = kpi_from_v9[key].drop(columns=[c for c in cost_cols if c in kpi_from_v9[key].columns])
        df2 = kpi_from_v10[key].drop(columns=[c for c in cost_cols if c in kpi_from_v10[key].columns])
        pd.testing.assert_frame_equal(df1, df2)

    # the new system-level cost metrics are present, zero, and additive-only
    sys_df = kpi_from_v10["system"]
    for metric in ("Total Resource Cost", "Total Entity Holding Cost", "Total Cost"):
        assert float(sys_df.loc[sys_df["Metric"] == metric, "Value"].iloc[0]) == 0.0


# ---------------------------------------------------------------- costing rates
def test_busy_cost_is_the_rate_times_busy_time_exactly():
    """The statistical M/M/1 cost check this replaces proved nothing the exact
    D/D/1 ledger above does not already prove; what it did add was that the
    per-unit-time rate is applied to the BUSY time only, which is deterministic
    and so is asserted deterministically here: 4 jobs x 3 minutes of service in
    a 100-minute run at 1 EUR/minute is exactly 12 EUR, not 100."""
    blocks = [create(dist_params={"value": 10.0}, max_arrivals=4), process(value=3.0), dispose()]
    resources = [{"name": "Server", "capacity": 1, "discipline": "FIFO",
                 "cost_busy": 1.0, "cost_idle": 0.0, "cost_time_unit": "minutes"}]
    model = layout(blocks, chain(*blocks), resources=resources)

    kpi = run_single_simulation(model, seed=0, run_len=100, warmup=0)

    assert float(resource_row(kpi, "Server")["Busy Cost"]) == pytest.approx(12.0, abs=1e-9)
