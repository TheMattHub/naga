"""Designs, ranking and selection, and black-box search (ROADMAP 2.1).

Everything statistical is tested against a KNOWN answer: a deterministic model
whose response is exact, an M/M/c queue whose capacity effect has a known sign,
synthetic responses with known means injected through the module's one
evaluation seam (`optimize.evaluation._evaluate_point`), and -- marked slow -- a staffing
problem whose optimum the test computes itself from Erlang-C.
"""
import json

import numpy as np
import pandas as pd
import pytest

from nagasim import api, experiments, optimize
from nagasim.optimize import Factor

from test_experiments import SINGLE_SERVER, deterministic_layout

KPI = "system/Avg Throughput Time"
COST = "system/Total Cost"
QUEUE = "system/Avg Queue Length"
CAPACITY = "resources.Server.capacity"


# ---------------------------------------------------------------- factors
def test_factor_coerces_to_what_the_model_can_actually_take():
    integer = Factor("a", "integer", 1, 4)
    assert [integer.coerce(v) for v in (0.2, 2.4, 2.6, 99)] == [1, 2, 3, 4]

    stepped = Factor("b", "continuous", 0.0, 1.0, step=0.25)
    assert stepped.coerce(0.3) == pytest.approx(0.25)
    assert stepped.coerce(2.0) == pytest.approx(1.0)

    choice = Factor("c", "cat", levels=["FIFO", "LIFO"])
    assert choice.coerce("LIFO") == "LIFO"
    assert choice.coerce(0) == "FIFO"


def test_one_factor_can_drive_several_paths_with_the_same_value():
    factor = Factor(["resources.Agents.capacity", "simulation.replications"], "integer", 1, 9)
    assert factor.overrides(4) == [{"path": "resources.Agents.capacity", "value": 4},
                                   {"path": "simulation.replications", "value": 4}]
    assert factor.name == "resources.Agents.capacity"


def test_a_categorical_factor_without_levels_is_refused():
    with pytest.raises(ValueError):
        Factor("a", "categorical")
    with pytest.raises(ValueError):
        Factor("a", "nonsense", 0, 1)


# ---------------------------------------------------------------- designs
def _two_factors():
    return [Factor("x", "continuous", 0.0, 10.0), Factor("y", "integer", 1, 4)]


def test_full_factorial_shape_levels_and_bounds():
    factors = _two_factors()
    design = optimize.full_factorial(factors, 2)
    assert list(design.columns) == ["x", "y"]
    assert len(design) == 4
    assert sorted(set(design["x"])) == [0.0, 10.0]
    assert sorted(set(design["y"])) == [1, 4]

    three = optimize.full_factorial(factors, {"x": 3, "y": 2})
    assert len(three) == 6
    assert sorted(set(three["x"])) == [0.0, 5.0, 10.0]


def test_full_factorial_never_repeats_a_level_an_integer_factor_cannot_reach():
    """An integer factor over [1, 2] has two levels however many were asked."""
    design = optimize.full_factorial([Factor("y", "integer", 1, 2)], 5)
    assert sorted(design["y"]) == [1, 2]


def test_latin_hypercube_stratifies_every_factor_and_is_reproducible():
    factors = [Factor("x", "continuous", 0.0, 10.0), Factor("z", "continuous", -5.0, 5.0)]
    n = 8
    design = optimize.latin_hypercube(factors, n, seed=3)

    assert design.shape == (n, 2)
    for factor in factors:
        strata = sorted(min(int((v - factor.low) / (factor.high - factor.low) * n), n - 1)
                        for v in design[factor.name])
        assert strata == list(range(n))        # one point per stratum, per factor
        assert design[factor.name].between(factor.low, factor.high).all()

    pd.testing.assert_frame_equal(design, optimize.latin_hypercube(factors, n, seed=3))
    assert not design.equals(optimize.latin_hypercube(factors, n, seed=4))


def test_latin_hypercube_rounds_integer_and_indexes_categorical_factors():
    factors = [Factor("y", "integer", 1, 4), Factor("c", "cat", levels=["a", "b"])]
    design = optimize.latin_hypercube(factors, 6, seed=0)
    assert all(isinstance(v, int) and 1 <= v <= 4 for v in design["y"])
    assert set(design["c"]) <= {"a", "b"}


def test_one_at_a_time_holds_every_other_factor_at_the_baseline():
    factors = _two_factors()
    design = optimize.one_at_a_time(factors, {"x": 5.0, "y": 2})

    assert design.iloc[0].to_dict() == {"x": 5.0, "y": 2}
    for _, row in design.iloc[1:].iterrows():
        moved = [name for name in ("x", "y") if row[name] != {"x": 5.0, "y": 2}[name]]
        assert len(moved) == 1                  # exactly one factor off the baseline
    assert len(design) == 5                     # baseline + 2 levels x 2 factors


# ---------------------------------------------------------------- run_design
def _delay_layout():
    """The deterministic model of test_experiments, with the DELAY as a factor:
    with Constant everything the throughput time IS the delay, exactly."""
    return deterministic_layout(max_arrivals=20)


def test_run_design_on_a_deterministic_model_returns_the_exact_response():
    factor = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    design = optimize.full_factorial([factor], 2)

    result = optimize.run_design(_delay_layout(), [factor], design, responses=[KPI],
                                 replications=3, run_length=100)

    table = result.table.set_index(factor.name)
    assert table.loc[2.0, "Mean"] == pytest.approx(2.0)
    assert table.loc[4.0, "Mean"] == pytest.approx(4.0)
    assert table.loc[2.0, "LB"] == pytest.approx(2.0)      # zero variance
    assert list(table["N"]) == [3, 3]
    assert result.completed is True


def test_run_design_defaults_to_the_system_kpis_and_records_its_seeds():
    factor = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    result = optimize.run_design(_delay_layout(), [factor],
                                 optimize.full_factorial([factor], 2), replications=2,
                                 run_length=100)
    assert all(k.startswith("system/") for k in result.responses)
    assert KPI in result.responses
    assert result.seeds == {0: [0, 1], 1: [0, 1]}          # CRN: the same seeds everywhere


def test_run_design_stops_when_asked():
    factor = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    result = optimize.run_design(_delay_layout(), [factor],
                                 optimize.full_factorial([factor], 2), responses=[KPI],
                                 replications=2, run_length=100, should_stop=lambda: True)
    assert result.completed is False
    assert result.values == {}


def test_run_design_stops_between_points_and_keeps_the_ones_it_finished():
    """PHASE4 T3: a wave is abandoned point by point, and a point that never
    ran is left out rather than reported with holes."""
    seen = []
    factor = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    result = optimize.run_design(_delay_layout(), [factor],
                                 optimize.full_factorial([factor], 3), responses=[KPI],
                                 replications=2, run_length=100,
                                 should_stop=lambda: bool(seen.append(1)) or len(seen) > 2)

    assert result.completed is False
    assert list(result.values) == [0]                      # only the first point finished


@pytest.mark.slow
def test_a_doe_over_design_points_is_the_same_run_in_parallel(worker_pool):
    """PHASE4 T3: ONE replication per point -- the shape that used to leave
    every worker but one idle -- must give exactly the sequential numbers."""
    factors = [Factor(CAPACITY, "integer", 1, 2),
               Factor("simulation.warmup", "continuous", 0.0, 10.0, name="w")]
    layout = api.load_layout(SINGLE_SERVER)
    design = optimize.full_factorial(factors, 2)

    serial = optimize.run_design(layout, factors, design, responses=[KPI], replications=1,
                                 run_length=200)
    parallel = optimize.run_design(layout, factors, design, responses=[KPI], replications=1,
                                   run_length=200, workers=4, pool=worker_pool)

    assert parallel.seeds == serial.seeds
    for index, by_kpi in serial.values.items():            # bit-identical, not just close
        for kpi, values in by_kpi.items():
            np.testing.assert_array_equal(parallel.values[index][kpi], values)
    pd.testing.assert_frame_equal(parallel.table, serial.table)


def test_main_effect_of_server_capacity_on_an_mmc_queue_has_the_right_sign():
    """One more server cannot make the queue slower, and with CRN the paired
    effect interval says so at 95%."""
    layout = api.load_layout(SINGLE_SERVER)
    factor = Factor(CAPACITY, "integer", 1, 2)

    result = optimize.run_design(layout, [factor], optimize.full_factorial([factor], 2),
                                 responses=[KPI], replications=4, run_length=400, warmup=50)
    effects = result.main_effects(KPI).set_index("Level")

    assert effects.loc[2, "Mean"] < effects.loc[1, "Mean"]
    assert effects.loc[1, "Effect"] < 0
    assert effects.loc[1, "Effect UB"] < 0                 # the interval excludes 0


def test_interaction_reports_cell_means_and_a_two_factor_effect():
    """Two constant delays in series: the throughput time is their sum, so the
    interaction is exactly zero and every cell mean is exact."""
    layout = deterministic_layout(max_arrivals=20)
    layout["blocks"].insert(2, {"name": "delay_2", "type": "DELAY", "position": [2, 0],
                                "params": {"distribution": "Constant",
                                           "dist_params": {"value": 1.0}}})
    layout["links"] = [{"source": "create_1", "target": "delay_1"},
                       {"source": "delay_1", "target": "delay_2"},
                       {"source": "delay_2", "target": "dispose_1"}]
    factors = [Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0, name="a"),
               Factor("blocks.delay_2.params.dist_params.value", "continuous", 1.0, 3.0, name="b")]

    result = optimize.run_design(layout, factors, optimize.full_factorial(factors, 2),
                                 responses=[KPI], replications=2, run_length=200)
    frame = result.interaction(KPI, "a", "b")
    cells = frame.set_index(["a", "b"])

    assert len(frame) == 4
    assert cells.loc[(2.0, 1.0), "Mean"] == pytest.approx(3.0)
    assert cells.loc[(4.0, 3.0), "Mean"] == pytest.approx(7.0)
    assert frame.attrs["effect"] == pytest.approx(0.0)      # additive delays: no interaction


# ---------------------------------------------------------------- selection (KN)
def _synthetic(means, *, sd=1.0, common=1.0, trial=0):
    """A fake `_evaluate_point`: candidate i (its first override's value) has
    true mean means[i], a shared per-seed component (so common random numbers
    are real) and its own noise."""
    def fake(layout, overrides, seeds, **kwargs):
        index = int(overrides[0]["value"])
        out = {KPI: {}}
        for seed in seeds:
            shared = np.random.default_rng([trial, seed]).normal(0.0, common)
            own = np.random.default_rng([trial, seed, index]).normal(0.0, sd)
            out[KPI][seed] = means[index] + shared + own
        return out
    return fake


def _candidates(n):
    return [{"name": f"c{i}", "overrides": [{"path": CAPACITY, "value": i}]} for i in range(n)]


def test_kn_selects_the_true_best_at_the_stated_confidence(monkeypatch):
    """The guarantee, measured: with the best system exactly `indifference`
    better than every other, 100 seeded trials must select it at least 95
    times at a confidence of 0.95."""
    means = [10.0, 11.0, 11.0, 11.0, 12.0]          # a minimum at c0, gap == delta == 1
    correct = 0
    for trial in range(100):
        monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic(means, trial=trial))
        result = optimize.select_best(None, _candidates(len(means)), response=KPI, goal="min",
                                      confidence=0.95, indifference=1.0, n0=10,
                                      max_replications=500)
        correct += result.best == "c0"
    assert correct >= 95


def test_kn_screens_out_a_clearly_inferior_candidate_early(monkeypatch):
    """c2 is hopeless and stops costing replications at the first screen; c1 is
    close enough that separating it takes several more rounds."""
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic([10.0, 10.8, 40.0]))
    result = optimize.select_best(None, _candidates(3), response=KPI, goal="min",
                                  indifference=0.5, n0=10, max_replications=200)

    assert result.best == "c0"
    assert result.guaranteed is True
    assert result.replications["c2"] == 10
    assert result.replications["c0"] > 10
    assert result.table.set_index("Candidate").loc["c2", "Eliminated at"] == 10
    assert result.survivors[0] == ["c0", "c1", "c2"] and result.survivors[-1] == ["c0"]


def test_kn_respects_the_replication_cap_and_drops_the_guarantee(monkeypatch):
    """Two candidates with the same true mean can never be separated; the cap
    stops the procedure and the result says the guarantee does not apply."""
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic([10.0, 10.0], sd=1.0))
    result = optimize.select_best(None, _candidates(2), response=KPI, goal="min",
                                  indifference=0.01, n0=5, max_replications=20)

    assert result.guaranteed is False
    assert max(result.replications.values()) <= 20
    assert set(result.replications) == {"c0", "c1"}


def test_kn_maximises_when_asked(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic([10.0, 20.0], sd=0.2))
    result = optimize.select_best(None, _candidates(2), response=KPI, goal="max",
                                  indifference=1.0, n0=8, max_replications=100)
    assert result.best == "c1"
    assert result.table.set_index("Candidate").loc["c1", "Mean"] == pytest.approx(20.0, abs=0.5)


# ---------------------------------------------------------------- optimisation
def _quadratic(optimum=(3.0, -1.0), noise=0.05, calls=None, constraint=None, offset=0.0):
    """A noisy quadratic in two factors driven through two override paths.
    `calls` collects (point, seed) pairs so a test can prove the cache works.
    *offset* raises the whole surface, which makes every gain negligible
    relative to the objective's own size (D80)."""
    def fake(layout, overrides, seeds, **kwargs):
        by_path = {o["path"]: float(o["value"]) for o in overrides}
        x, y = by_path["simulation.time_limit"], by_path["simulation.warmup"]
        value = (x - optimum[0]) ** 2 + (y - optimum[1]) ** 2 + 1.0 + offset
        out = {KPI: {}, QUEUE: {}}
        for seed in seeds:
            if calls is not None:
                calls.append(((x, y), seed))
            jitter = np.random.default_rng([7, seed, abs(int(x * 1e6)),
                                            abs(int(y * 1e6))]).normal(0.0, noise)
            out[KPI][seed] = value + jitter
            out[QUEUE][seed] = (x if constraint is None else constraint(x, y)) + jitter * 0.01
        return out
    return fake


def _xy_factors(kind="continuous", low=-5.0, high=5.0):
    return [Factor("simulation.time_limit", kind, low, high, name="x"),
            Factor("simulation.warmup", kind, low, high, name="y")]


def test_optimizer_finds_the_noisy_quadratic_optimum_within_the_budget(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())
    result = optimize.optimize({"schema_version": 9}, _xy_factors(), response=KPI, goal="min",
                               budget=300, replications_per_point=5, seed=0)

    assert result.budget_used <= 300
    assert abs(result.best["x"] - 3.0) <= 0.1 * 10.0      # within 10% of the factor range
    assert abs(result.best["y"] + 1.0) <= 0.1 * 10.0
    assert result.best_mean == pytest.approx(1.0, abs=0.1)
    assert not result.history.empty
    assert result.selection is not None
    assert result.converged is True


def test_optimizer_never_exceeds_a_tiny_budget(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())
    result = optimize.optimize({"schema_version": 9}, _xy_factors(), response=KPI, goal="min",
                               budget=20, replications_per_point=5, seed=0)
    assert result.budget_used <= 20


def test_the_integer_cache_never_evaluates_the_same_rounded_point_twice(monkeypatch):
    calls: list = []
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic(calls=calls))
    optimize.optimize({"schema_version": 9}, _xy_factors("integer", -5, 5), response=KPI,
                      goal="min", budget=200, replications_per_point=4, seed=0)

    assert len(calls) == len(set(calls))                   # no (point, seed) run twice
    assert all(float(x).is_integer() and float(y).is_integer() for (x, y), _seed in calls)


def test_constraints_pick_the_feasible_optimum(monkeypatch):
    """The unconstrained optimum sits at x = 9, outside the feasible x <= 5."""
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point",
                        _quadratic(optimum=(9.0, 0.0), noise=0.01))
    result = optimize.optimize({"schema_version": 9}, _xy_factors(low=0.0, high=10.0),
                               response=KPI, goal="min", constraints=[(QUEUE, "<=", 5.0)],
                               budget=400, replications_per_point=5, seed=0)

    assert result.feasible is True
    assert result.best["x"] <= 5.05                        # the constraint binds
    assert result.best["x"] >= 4.0                         # and the search pushed up to it
    assert (result.history["Feasibility"] == "infeasible").any()


# ------------------------------------------------- step scaling by effect (D80)
#
# The biogas sizing study's weakness, reduced to three factors: the search moved
# the factor worth a rounding error four times, DOUBLING its step each time
# (every gain was paired-t significant, because common random numbers make the
# differences exact), and never refined the factor worth 30 times as much.

def _kinked(calls=None):
    """x: the factor that matters, a kinked optimum at 100 of a 0..800 range.
    y: a consistent slope worth 0.6 over its whole range, on a base of 1000.
    z: nothing at all. The noise depends on the SEED only -- perfect CRN, so a
    paired difference of any size is significant."""
    def fake(layout, overrides, seeds, **kwargs):
        by_path = {o["path"]: float(o["value"]) for o in overrides}
        x, y = by_path["simulation.time_limit"], by_path["simulation.warmup"]
        if calls is not None:
            calls.append((x, y, by_path["simulation.replications"]))
        value = 1000.0 + 3.0 * max(0.0, 100.0 - x) + 0.25 * max(0.0, x - 100.0) - 0.0002 * y
        return {KPI: {s: value + 25.0 * ((s % 5) - 2) for s in seeds},
                QUEUE: {s: 0.0 for s in seeds}}
    return fake


def test_the_search_refines_the_factor_that_matters_not_the_one_that_moves(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _kinked())
    factors = [Factor("simulation.time_limit", "integer", 0, 800, name="x"),
               Factor("simulation.warmup", "integer", 0, 3000, name="y"),
               Factor("simulation.replications", "integer", 0, 500, name="z")]
    result = optimize.optimize({"schema_version": 9}, factors, response=KPI, goal="min",
                               budget=180, replications_per_point=3, seed=0)

    # Before D80 the tiny y gains doubled the one global step every iteration,
    # so x was only ever tried 200, 400 and 800 away and ended 27 off.
    assert abs(result.best["x"] - 100) <= 8            # 1% of the x range
    assert result.budget_used <= 180


def test_no_replications_are_bought_to_settle_a_negligible_difference(monkeypatch):
    """Every gain here is under 0.1% of the objective, so nothing is worth a
    top-up; with `min_improvement=0` the same search buys them."""
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic(noise=0.5, offset=100_000.0))
    kwargs = dict(response=KPI, goal="min", budget=150, replications_per_point=4, seed=0)
    thrifty = optimize.optimize({"schema_version": 9}, _xy_factors(), **kwargs)
    loose = optimize.optimize({"schema_version": 9}, _xy_factors(),
                              min_improvement=0.0, **kwargs)

    assert (thrifty.history["N"] > 4).sum() == 0
    assert (loose.history["N"] > 4).sum() > 0


def test_an_unknown_response_or_constraint_key_is_refused(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())
    with pytest.raises(ValueError):
        optimize.optimize({"schema_version": 9}, _xy_factors(), response="system/Nope",
                          budget=40, replications_per_point=4)
    with pytest.raises(ValueError):
        optimize.optimize({"schema_version": 9}, _xy_factors(), response=KPI,
                          constraints=[("system/Nope", "<=", 1.0)], budget=40,
                          replications_per_point=4)


def test_optuna_method_says_so_clearly_when_optuna_is_missing(monkeypatch):
    try:
        import optuna  # noqa: F401
    except ImportError:
        monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())
        with pytest.raises(ImportError, match="optuna"):
            optimize.optimize({"schema_version": 9}, _xy_factors(), response=KPI,
                              method="optuna", budget=20, replications_per_point=5)
    else:
        pytest.skip("optuna is installed; the missing-dependency path cannot be exercised")


# ------------------------------------------------- deterministic models (#96)
#
# A model with no random draw left in it -- a continuous plant, an inventory on
# a fixed demand profile -- is a first-class case. Before the fix it cost the
# search `replications_per_point` identical runs per point, and two candidates
# it could not tell apart bought replications until the cap: 120 of them for a
# question that one run answers.

def _counting_seam(monkeypatch, fake):
    """Wrap a `_evaluate_point` stand-in and count the replications it runs."""
    spent: list = []

    def counted(layout, overrides, seeds, **kwargs):
        seeds = list(seeds)
        spent.extend(seeds)
        return fake(layout, overrides, seeds, **kwargs)

    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", counted)
    return spent


def test_a_deterministic_point_is_simulated_once_however_many_replications_are_asked():
    layout = deterministic_layout()
    evaluator = optimize._Evaluator(layout)

    first = evaluator.values("a", [], [0, 1, 2, 3, 4])
    assert evaluator.deterministic is True
    assert evaluator.used == 5                     # the first point settles the question
    assert list(first[KPI]) == [2.0] * 5

    slower = [{"path": "blocks.delay_1.params.dist_params.value", "value": 4.0}]
    second = evaluator.values("b", slower, [0, 1, 2, 3, 4])
    assert evaluator.used == 6                     # and every later point costs ONE run
    assert list(second[KPI]) == [4.0] * 5          # standing for every replication asked for


def test_a_stochastic_model_is_never_put_on_the_deterministic_path(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _synthetic([10.0, 11.0], sd=1.0))
    evaluator = optimize._Evaluator({"schema_version": 9})
    evaluator.values("c0", [{"path": CAPACITY, "value": 0}], [0, 1, 2, 3])

    assert evaluator.deterministic is False
    evaluator.values("c1", [{"path": CAPACITY, "value": 1}], [0, 1, 2, 3])
    assert evaluator.used == 8                     # all four replications, both points


def test_kn_does_not_loop_on_an_exact_tie_and_keeps_its_guarantee():
    """Two candidates with an identical response can never screen each other
    out: the variance of their difference is 0, so the tolerance is 0 too.
    They are equally best, so the first one offered wins and the procedure
    stops at the first stage instead of buying `max_replications` of nothing."""
    layout = deterministic_layout()
    result = optimize.select_best(
        layout, [{"name": "a", "overrides": []}, {"name": "b", "overrides": []}],
        response=KPI, goal="min", indifference=0.01, n0=5, max_replications=200)

    assert result.best == "a"                      # the first candidate, deterministically
    assert result.guaranteed is True
    assert result.survivors[-1] == ["a"]
    assert max(result.replications.values()) == 5  # n0 and no more
    # n0 runs of the first candidate settle that the model is deterministic;
    # the second candidate then costs a single run. Before the fix: 120.
    assert result.budget_used == 6


def test_a_deterministic_optimum_is_found_within_the_budget_and_the_budget_is_a_hard_cap(
        monkeypatch):
    """The known optimum of a deterministic objective, found by the search
    itself, with every simulated replication counted at the module's one
    evaluation seam."""
    spent = _counting_seam(monkeypatch, _quadratic(noise=0.0))
    result = optimize.optimize({"schema_version": 9}, _xy_factors("integer", -5, 5),
                               response=KPI, goal="min", budget=60,
                               replications_per_point=5, seed=0)

    assert result.best == {"x": 3, "y": -1}        # the quadratic's own optimum
    assert result.best_mean == pytest.approx(1.0)
    assert result.converged is True
    assert len(spent) <= 60                        # the seam never ran more than the budget
    assert result.budget_used == len(spent)        # and the budget counts what actually ran
    # The whole point: the budget bought POINTS, not repeats of the same point.
    assert len(result.history) > len(spent) // 5


# ---------------------------------------------------------------- end to end (slow)
def staffing_layout(run_length=2000.0, servers=4):
    """M/M/c with costs (ROADMAP 2.2): arrivals at 1/hour, service 0.4/hour per
    server, 1 per server-hour of capacity and 1 per entity-hour in system. The
    reported `system/Total Cost` is then run_length x (c + L)."""
    return {
        "schema_version": 10,
        "blocks": [
            {"name": "create_1", "type": "CREATE", "position": [0, 0],
             "params": {"e_type": "DEFAULT", "distribution": "Exponential",
                        "dist_params": {"rate": 1.0}, "entities_per_arrival": 1,
                        "max_arrivals": 0, "start_time": 0}},
            {"name": "process_1", "type": "PROCESS", "position": [1, 0],
             "params": {"action": "Seize Delay Release", "distribution": "Exponential",
                        "dist_params": {"rate": 0.4}, "priority": 0, "quantity": 1,
                        "resource_name": "Server"}},
            {"name": "dispose_1", "type": "DISPOSE", "position": [2, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
        ],
        "links": [{"source": "create_1", "target": "process_1"},
                  {"source": "process_1", "target": "dispose_1"}],
        "resources": [{"name": "Server", "capacity": servers, "discipline": "FIFO",
                       "cost_busy": 1.0, "cost_idle": 1.0, "cost_time_unit": "hours"}],
        "resource_sets": [], "entities": [{"name": "DEFAULT", "holding_cost": 1.0}],
        "variables": [], "schedules": [], "failures": [], "scenarios": [],
        "simulation": {"time_limit": run_length, "warmup": 0, "replications": 1,
                       "time_unit": "hours"},
    }


def erlang_c_cost(c: int, lam: float = 1.0, mu: float = 0.4) -> float:
    """Server cost + waiting cost per time unit for an M/M/c queue: c + L,
    with L = Lq + lam/mu and Lq from Erlang-C."""
    import math
    a = lam / mu
    rho = a / c
    assert rho < 1, "unstable"
    head = sum(a ** n / math.factorial(n) for n in range(c))
    tail = a ** c / (math.factorial(c) * (1 - rho))
    prob_wait = tail / (head + tail)
    return c + prob_wait * rho / (1 - rho) + a


@pytest.mark.slow
def test_the_classic_staffing_problem_lands_on_the_erlang_c_optimum(worker_pool):
    theory = {c: erlang_c_cost(c) for c in range(3, 9)}
    best_by_theory = min(theory, key=theory.get)
    assert best_by_theory == 4                 # guards the test's own setup

    run_length = 2000.0
    factor = Factor(CAPACITY, "integer", 3, 8, name="servers")
    result = optimize.optimize(staffing_layout(run_length), [factor], response=COST, goal="min",
                               budget=120, replications_per_point=6, seed=0,
                               run_length=run_length, warmup=0.0,
                               indifference=0.25 * run_length,
                               workers=6, pool=worker_pool)   # PHASE5 O7: same numbers, D76

    assert result.best["servers"] == best_by_theory
    assert result.budget_used <= 120
    assert result.best_mean / run_length == pytest.approx(theory[best_by_theory], rel=0.05)


# ---------------------------------------------------------------- CLI
def test_doe_cli_writes_a_table_and_a_report(tmp_path):
    model = tmp_path / "model.json"
    model.write_text(json.dumps(deterministic_layout()))
    out, report = tmp_path / "doe.xlsx", tmp_path / "doe.html"

    rc = optimize.main([str(model),
                        "--factor", "blocks.delay_1.params.dist_params.value=float:2:4",
                        "--design", "full:2", "--response", KPI,
                        "--replications", "2", "--run-length", "100",
                        "--out", str(out), "--report", str(report)], command="doe")

    assert rc == 0
    assert out.exists()
    assert "Design of experiments" in report.read_text(encoding="utf-8")


def test_optimize_cli_runs_on_a_tiny_budget(tmp_path, capsys):
    out = tmp_path / "search.csv"
    rc = optimize.main([str(SINGLE_SERVER), "--factor", f"{CAPACITY}=int:1:3",
                        "--response", KPI, "--goal", "min", "--budget", "24",
                        "--replications", "3", "--run-length", "200", "--warmup", "20",
                        "--constraint", "resource/Server/Usage % (snapshot)<=99",
                        "--out", str(out)], command="optimize")

    assert rc == 0
    assert "Best point" in capsys.readouterr().out
    assert out.exists()


def test_the_launcher_dispatches_doe_and_optimize_without_qt(monkeypatch):
    from nagasim import cli as launcher

    seen = []
    monkeypatch.setattr(optimize, "main", lambda argv, command: seen.append((argv, command)) or 0)
    assert launcher.main(["doe", "m.json"]) == 0
    assert launcher.main(["optimize", "m.json"]) == 0
    assert [c for _argv, c in seen] == ["doe", "optimize"]


def test_factor_spec_grammar():
    for spec, expected in [
        ("a.b=int:3:6", ("integer", 3.0, 6.0, None)),
        ("a.b=float:0.2:0.4", ("continuous", 0.2, 0.4, None)),
        ("a.b=float:0:1:0.25", ("continuous", 0.0, 1.0, 0.25)),
    ]:
        factor = optimize.parse_factor(spec)
        assert (factor.kind, factor.low, factor.high, factor.step) == expected, spec

    factor = optimize.parse_factor("a.b,c.d=cat:FIFO|LIFO")
    assert factor.kind == "categorical"
    assert factor.levels == ["FIFO", "LIFO"]
    assert factor.paths == ["a.b", "c.d"]

    for spec in ("nonsense", "a.b=int:3", "a.b=cat:", "=int:1:2"):
        with pytest.raises(ValueError, match="factor"):
            optimize.parse_factor(spec)


def test_constraint_spec_grammar():
    assert optimize.parse_constraint("resource/Agents/Usage % (snapshot)<=85") == (
        "resource/Agents/Usage % (snapshot)", "<=", 85.0)
    assert optimize.parse_constraint("system/Throughput (per time unit)>=0.9") == (
        "system/Throughput (per time unit)", ">=", 0.9)
    with pytest.raises(ValueError, match="constraint"):
        optimize.parse_constraint("system/Nope")


# ---------------------------------------------------------------- tornado (PHASE6 C6)
def test_tornado_swings_are_exact_sorted_and_crn_reproducible():
    """A known-linear KPI (`_delay_layout`'s throughput time IS the delay
    value): the swept factor's swing is exact, the row order is by swing
    (largest first), and CRN makes two runs on the same seed identical."""
    delay = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    warmup = Factor("simulation.warmup", "continuous", 0.0, 1.0)   # no effect: same swing 0

    result = optimize.tornado(_delay_layout(), [delay, warmup], KPI, seed=0, replications=1)

    assert list(result["Factor"]) == [delay.name, warmup.name]     # larger swing first
    row = result.set_index("Factor")
    values = row.loc[delay.name, ["Low value", "High value", "KPI at low", "KPI at high",
                                  "Base KPI", "Swing"]].to_numpy(dtype=float)
    assert values == pytest.approx([2.0, 4.0, 2.0, 4.0, 3.0, 2.0])
    assert row.loc[warmup.name, "Swing"] == pytest.approx(0.0)

    again = optimize.tornado(_delay_layout(), [delay, warmup], KPI, seed=0, replications=1)
    pd.testing.assert_frame_equal(result, again)


# ---------------------------------------------------------------- report section
def test_report_carries_the_design_and_optimization_sections(monkeypatch, tmp_path):
    from nagasim import api
    from nagasim.report import build_report

    layout = _delay_layout()
    factor = Factor("blocks.delay_1.params.dist_params.value", "continuous", 2.0, 4.0)
    design = optimize.run_design(layout, [factor], optimize.full_factorial([factor], 2),
                                 responses=[KPI], replications=2, run_length=100)
    results = api.run_model(layout, replications=1, run_length=100)

    html = build_report(layout, results, design=design, title="doe")
    assert "Design of experiments" in html
    assert "Main effects" in html
    assert "<svg" in html

    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _quadratic())
    search = optimize.optimize({"schema_version": 9}, _xy_factors(), response=KPI, goal="min",
                               budget=120, replications_per_point=5, seed=0)
    html = build_report(layout, results, optimization=search, title="opt",
                        path=tmp_path / "opt.html")
    assert "Optimization" in html
    assert "Best so far" in html
    assert "Ranking and selection" in html
    assert (tmp_path / "opt.html").exists()
