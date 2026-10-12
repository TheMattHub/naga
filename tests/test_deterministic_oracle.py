"""Independent oracle for Phase 6D W4 P1 (deterministic run mode),
docs/history/PHASE6-6D-W4-PLAN-2026-09-25.md ("P1 Deterministic run mode").
Every expected number here is derived BY HAND from the spec, from
`nagasim/core/distributions.py`'s own sampler parameterisations, and from the
DECIDE/failure/CREATE wire format `tests/conftest.py` already uses elsewhere
-- never by running the (not yet written) implementation. Failures because
`simulation.deterministic` does nothing yet, or `api.run_model` rejects an
unknown `deterministic=` kwarg, are EXPECTED until P1 lands; the coordinator
reconciles any real wire mismatch against what P1 actually ships.

Wire format assumed from the plan text:
  - `simulation.deterministic: true` (model-level key, the form every case
    below uses -- it survives regardless of whether an `api.run_model(...,
    deterministic=True)` override is also added).
  - Every distribution returns its mean: Exponential(rate) -> 1/rate,
    Uniform(low, high) -> (low + high) / 2, Triangular(low, high, mode) ->
    (low + high + mode) / 3, Normal(mean, stddev) -> mean (stddev unused).
    Derived straight from each sampler in distributions.py (`_sample_*`),
    not assumed.
  - Failures (`uptime`/`downtime`, each a duration spec) use their mean TTF
    and TTR the same way.
  - DECIDE `mode: "Chance"` by-chance allocation uses largest-remainder
    rounding of n * p per branch; replications are forced to 1 regardless of
    what `simulation.replications` (or `run_model(replications=...)`) asks
    for.

Horizon rule (DEVLOG #32, `core/sim_runner.step_until`): the KPI window ends
at the time limit only when the event queue still has something pending past
it (`reached_limit`); otherwise it ends at the actual drain time. Cases here
either keep an infinite arrival/failure stream alive past their time_limit
(no trick needed) or let the model drain naturally with no time_limit at all
-- except case 6, whose only "keep the model valid" block is a keeper CREATE
(`start_time=10**9, max_arrivals=1`) that never fires inside the horizon.
"""
from __future__ import annotations

import pytest

from conftest import chain, create, delay, dispose, layout, metric_value, process, resource_row, run_layout

from nagasim import api

# ---------------------------------------------------------------- wire names
DETERMINISTIC_KEY = "deterministic"          # simulation.deterministic (plan text, D119)
SYSTEM_FRAME = "system"
ENTITY_SUMMARY_FRAME = "entity_summary"
RESOURCE_SUMMARY_FRAME = "resource_summary"


def _det_sim(**extra):
    base = {"time_limit": None, "warmup": 0, "replications": 1, DETERMINISTIC_KEY: True}
    base.update(extra)
    return base


def _decide_chance(probs, name="decide_1"):
    return {"name": name, "description": name, "type": "DECIDE", "position": [1, 0],
            "params": {"mode": "Chance", "probs": probs, "conditions": []}}


def _delay_block(name, distribution, **dist_params):
    return {"name": name, "description": name, "type": "DELAY", "position": [1, 0],
            "params": {"distribution": distribution, "dist_params": dist_params}}


# -- case 1: M/M/1 becomes D/D/1 --------------------------------------------

def test_case1_dd1_exact_counts_and_zero_wait():
    """Interarrival Exponential(rate=0.5) -> mean 2; service Exponential(rate=1)
    -> mean 1. Deterministic: arrivals land at t=0,2,4,6,8 (CREATE's first
    arrival is at start_time itself, ARCHITECTURE/create.py); each entity is
    served in exactly 1 (the resource is always free by the time the next one
    arrives, since 1 < 2), so every entity's time in system is exactly 1 and
    the queue never forms -- a D/D/1 line, not an M/M/1 one. time_limit=9
    keeps the last (5th) dispose (at t=9) inside the window while the next
    arrival (t=10) stays pending past it, so the KPI horizon is the time
    limit itself (DEVLOG #32), not a drain time.

    Also exercises DEVLOG #87 (Entity.e_type upper-cases): e_type="default"
    here must show up as "DEFAULT" in entity_summary.
    """
    create_blk = create(e_type="default", distribution="Exponential", dist_params={"rate": 0.5},
                        max_arrivals=0, start_time=0)
    process_blk = process(resource="Server", value=1.0, distribution="Exponential")
    dispose_blk = dispose()
    model = layout(
        [create_blk, process_blk, dispose_blk], chain(create_blk, process_blk, dispose_blk),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        simulation=_det_sim(time_limit=9),
    )

    results = api.run_model(model, replications=1)

    assert metric_value(results, "Total Entities Created") == 5
    assert metric_value(results, "Total Entities Completed") == 5
    assert metric_value(results, "Avg Throughput Time") == pytest.approx(1.0)

    ent = results[ENTITY_SUMMARY_FRAME]
    row = ent[ent["Entity Type"] == "DEFAULT"].iloc[0]
    assert row["Total Created"] == 5
    assert row["Min Time"] == pytest.approx(1.0)
    assert row["Max Time"] == pytest.approx(1.0)

    res_row = resource_row(results, "Server")
    assert res_row["Total Seizes"] == 5


# -- case 2: single-entity delay means, by distribution ----------------------

def _one_entity_delay_time(env, distribution, dist_params):
    create_blk = create(max_arrivals=1)
    delay_blk = _delay_block("delay_1", distribution, **dist_params)
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                   simulation=_det_sim())
    run_layout(env, model, 0)
    entity = env.ctx.all_entities[0]
    return entity.deletion_time - entity.creation_time


def test_case2_uniform_mean(env):
    """Uniform(low, high): mean = (low + high) / 2 (_sample_uniform)."""
    assert _one_entity_delay_time(env, "Uniform", {"low": 2.0, "high": 10.0}) == pytest.approx(6.0)


def test_case2_triangular_mean(env):
    """Triangular(low, high, mode): mean = (low + high + mode) / 3 (_sample_triangular,
    numpy's (left, mode, right) order -- the mean formula is order-independent)."""
    assert _one_entity_delay_time(env, "Triangular",
                                  {"low": 2.0, "high": 10.0, "mode": 3.0}) == pytest.approx(5.0)


def test_case2_normal_mean(env):
    """Normal(mean, stddev): mean = mean, stddev plays no role once deterministic."""
    assert _one_entity_delay_time(env, "Normal", {"mean": 7.0, "stddev": 2.0}) == pytest.approx(7.0)


# -- case 3: DECIDE by chance, largest-remainder allocation -------------------

def _decide_chance_counts(env, probs, n):
    create_blk = create(max_arrivals=n, dist_params={"value": 1.0})
    decide_blk = _decide_chance(probs)
    dispose_a, dispose_b = dispose("dispose_a"), dispose("dispose_b")
    links = chain(create_blk, decide_blk) + [
        {"source": "decide_1", "target": "dispose_a"},
        {"source": "decide_1", "target": "dispose_b"},
    ]
    model = layout([create_blk, decide_blk, dispose_a, dispose_b], links, simulation=_det_sim())
    mdl = run_layout(env, model, 0)
    return mdl.blocks["dispose_a"].disposed_entities, mdl.blocks["dispose_b"].disposed_entities


def test_case3_by_chance_exact_30_70_over_ten(env):
    """probs=[0.3, 0.7], 10 entities: 10*0.3=3 and 10*0.7=7 are already
    integers, so largest-remainder rounding gives exactly 3/7 -- no tie to
    read either way."""
    a, b = _decide_chance_counts(env, [0.3, 0.7], 10)
    assert (a, b) == (3, 7)


def test_case3_by_chance_tie_50_50_over_three(env):
    """probs=[0.5, 0.5], 3 entities: n*p = [1.5, 1.5] for both branches, an
    exact .5/.5 tie. The plan text ("counts per branch are the rounded n*p")
    pins the total (3) and bounds each branch's deviation from 1.5 to < 1
    (i.e. every valid reading gives counts (1, 2) or (2, 1)); it does not by
    itself say which branch gets the extra unit on an exact tie, so this
    oracle asserts only what the spec text actually commits to -- the
    implementer's tie order (first-come, branch index, ...) is free to pick
    either split.
    """
    a, b = _decide_chance_counts(env, [0.5, 0.5], 3)
    assert a + b == 3
    assert abs(a - 1.5) < 1
    assert abs(b - 1.5) < 1


# -- case 4: replications forced to 1 -----------------------------------------

def test_case4_replications_forced_to_one():
    create_blk = create(max_arrivals=5, dist_params={"value": 1.0})
    delay_blk = delay(value=1.0)
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                   simulation=_det_sim(replications=5))

    results = api.run_model(model, replications=5)

    assert results[SYSTEM_FRAME]["Run"].nunique() == 1


# -- case 5: seed has no effect ------------------------------------------------

def test_case5_different_seeds_give_identical_results():
    create_blk = create(distribution="Exponential", dist_params={"rate": 0.5}, max_arrivals=6, start_time=0)
    process_blk = process(resource="Server", value=1.0, distribution="Exponential")
    dispose_blk = dispose()
    model = layout(
        [create_blk, process_blk, dispose_blk], chain(create_blk, process_blk, dispose_blk),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        simulation=_det_sim(),
    )

    results_a = api.run_model(model, replications=1, seed=1)
    results_b = api.run_model(model, replications=1, seed=999)

    assert results_a[SYSTEM_FRAME].equals(results_b[SYSTEM_FRAME])
    assert results_a[ENTITY_SUMMARY_FRAME].equals(results_b[ENTITY_SUMMARY_FRAME])


# -- case 6: failure with exponential TTF/TTR ---------------------------------

def test_case6_failure_mean_ttf_ttr_down_percentage():
    """uptime Exponential(rate=1/3) -> mean TTF 3; downtime Exponential(rate=1)
    -> mean TTR 1. The resource is never seized (no PROCESS references it),
    so each trip down starts the instant it triggers (nothing busy to wait
    out, matching `failure_controller`'s own "jobs in service finish first"
    wait resolving in zero time when nothing is in service). By hand: first
    trigger at t=3, repaired at t=4; second trigger at t=4+3=7, repaired at
    t=8. Down intervals [3,4] and [7,8] inside an 8-unit window -> 2/8 = 25 %.

    `create_1` is a keeper (start_time=10**9, max_arrivals=1) purely to
    satisfy "every model needs a CREATE"; it never fires inside time_limit=8,
    and the failure's own infinite loop keeps the event queue non-empty past
    the limit anyway, so the horizon is the time limit either way (DEVLOG #32).
    """
    create_blk = create(start_time=10**9, max_arrivals=1)
    dispose_blk = dispose()
    model = layout(
        [create_blk, dispose_blk], chain(create_blk, dispose_blk),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        failures=[{"name": "breakdown", "resources": ["Server"],
                  "uptime": {"distribution": "Exponential", "dist_params": {"rate": 1.0 / 3.0}},
                  "downtime": {"distribution": "Exponential", "dist_params": {"rate": 1.0}}}],
        simulation=_det_sim(time_limit=8),
    )

    results = api.run_model(model, replications=1, run_length=8)

    row = resource_row(results, "Server")
    assert row["Down %"] == pytest.approx(25.0)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
