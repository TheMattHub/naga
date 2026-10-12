"""Deterministic run mode (PHASE6 W4 P1, ARCHITECTURE D119, docs/history/
PHASE6-6D-W4-PLAN-2026-09-25.md). `tests/test_deterministic_oracle.py` is the
independent oracle for the wire-level behaviour (D/D/1, DECIDE-by-chance exact
counts, replications forced to 1, failure TTF/TTR); this file covers the
supporting pieces: every distribution's `mean()` against its own sampler,
validation of a distribution with no finite mean, and the expression-level
RNG functions.
"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import chain, create, delay, dispose, layout, metric_value, run_layout

from nagasim import api
from nagasim.core.distributions import (DISTRIBUTION_REGISTRY, StochasticDistribution,
                                        distribution_mean, distribution_needs_finite_mean)
from nagasim.core.schema import ModelValidationError, validate_model_structured

# ---------------------------------------------------------- per-distribution mean
# One case per distribution that HAS a mean() (every registered one except
# CustomExpression and FixedSchedule -- see their own registry comments):
# params chosen away from a "no finite mean" boundary (LogLogistic/PearsonTypeV/
# PearsonTypeVI's own DEFAULT params sit exactly on that boundary; Cauchy never
# has one -- both covered separately below).
_MEAN_CASES = [
    ("Constant", {"value": 5.0}),
    ("Uniform", {"low": 2.0, "high": 10.0}),
    ("Exponential", {"rate": 0.5}),
    ("Normal", {"mean": 3.0, "stddev": 1.0}),
    ("BoundedNormal", {"mean": 5.0, "stddev": 2.0, "min": 0.0, "max": 10.0}),
    ("Lognormal", {"mu": 0.0, "sigma": 0.5}),
    ("Triangular", {"low": 2.0, "high": 10.0, "mode": 4.0}),
    ("Gamma", {"alpha": 2.0, "beta": 1.5}),
    ("Weibull", {"scale": 2.0, "shape": 2.0}),
    ("Beta", {"min": 0.0, "max": 1.0, "alpha": 2.0, "beta": 3.0}),
    ("Erlang", {"k": 3.0, "rate": 1.0}),
    ("InverseGaussian", {"mu": 2.0, "lambda": 3.0}),
    ("Rayleigh", {"sigma": 2.0}),
    ("ChiSquare", {"df": 4.0}),
    ("LogLogistic", {"c": 3.0, "scale": 2.0}),
    ("JohnsonSU", {"a": 0.0, "b": 1.0, "loc": 0.0, "scale": 1.0}),
    ("JohnsonSB", {"a1": 0.0, "a2": 1.0, "min": 0.0, "max": 1.0}),
    ("PearsonTypeV", {"a": 3.0, "scale": 2.0}),
    ("PearsonTypeVI", {"a": 2.0, "b": 3.0, "scale": 1.0}),
    ("Bernoulli", {"p": 0.3}),
    ("Binomial", {"n": 10.0, "p": 0.4}),
    ("Geometric", {"p": 0.3}),
    ("Poisson", {"lambda": 4.0}),
    ("NegativeBinomial", {"r": 4.0, "p": 0.3}),
    ("DiscreteUniform", {"low": 1.0, "high": 10.0}),
    ("Hypergeometric", {"N": 20.0, "K": 7.0, "n": 5.0}),
    ("EmpiricalContinuous", {"values": [0.0, 5.0, 10.0], "probs": [0.3, 0.7]}),
    ("EmpiricalDiscrete", {"values": [1, 2, 3], "probs": [0.2, 0.3, 0.5]}),
]
_N_DRAWS = 20_000


def test_every_case_covers_a_distinct_finite_mean_distribution():
    """The table above is meant to be exhaustive: every registered distribution
    with a `mean` (i.e. every one `distribution_needs_finite_mean` accepts)
    appears exactly once, except Cauchy -- checked, but its own mean() always
    returns None (see `test_cauchy_has_no_finite_mean`)."""
    expected = {n for n, spec in DISTRIBUTION_REGISTRY.items() if spec.mean is not None} - {"Cauchy"}
    assert {n for n, _ in _MEAN_CASES} == expected


@pytest.mark.parametrize("dist_type, params", _MEAN_CASES, ids=[c[0] for c in _MEAN_CASES])
def test_mean_matches_20000_sample_draws(dist_type, params):
    """mean(params) is the SAME closed form the sampler's own parameterisation
    implies: a large sample average must land close to it (seeded, so this is
    a fixed check, not a flaky one -- the observed deviations at this seed and
    n are all under 0.06 in absolute terms; the tolerance below has ample
    margin for a different seed at the same n)."""
    rng = np.random.default_rng(12345)
    dist = StochasticDistribution(dist_type, params)
    draws = np.array([dist.sample(rng) for _ in range(_N_DRAWS)], dtype=float)
    expected = distribution_mean(dist_type, params)
    assert expected is not None
    assert draws.mean() == pytest.approx(expected, rel=0.05, abs=0.2)


@pytest.mark.parametrize("dist_type, params", _MEAN_CASES, ids=[c[0] for c in _MEAN_CASES])
def test_sample_deterministic_returns_the_mean_exactly(dist_type, params):
    """StochasticDistribution.sample(deterministic=True) returns EXACTLY
    mean(params) -- no draw at all, whatever the rng or how many times it is
    called."""
    dist = StochasticDistribution(dist_type, params)
    expected = distribution_mean(dist_type, params)
    rng_a, rng_b = np.random.default_rng(1), np.random.default_rng(999)
    assert dist.sample(rng_a, deterministic=True) == expected
    assert dist.sample(rng_b, deterministic=True) == expected
    assert dist.sample(rng_a, deterministic=True) == expected   # calling it again changes nothing


# ---------------------------------------------------------- no finite mean
def test_cauchy_has_no_finite_mean():
    assert distribution_mean("Cauchy", {"x0": 0.0, "gamma": 1.0}) is None
    assert distribution_needs_finite_mean("Cauchy") is True   # checked, but always fails


def test_custom_expression_and_fixed_schedule_are_not_checked_for_a_mean():
    """These two are handled without a `mean()` at all (their own registry
    comments): a plain expression, or one with a random-drawing function, is
    already exact once THAT function is deterministic; FixedSchedule draws no
    random number in the first place."""
    assert distribution_needs_finite_mean("CustomExpression") is False
    assert distribution_needs_finite_mean("FixedSchedule") is False


def test_deterministic_model_with_a_no_finite_mean_distribution_is_a_validation_problem():
    """E_DETERMINISTIC_NO_MEAN (D119): a DELAY on Cauchy in a deterministic
    model is a fatal Problem, not a silent fallback to random sampling."""
    create_blk = create(max_arrivals=1)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                "params": {"distribution": "Cauchy", "dist_params": {"x0": 0.0, "gamma": 1.0}}}
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                  simulation={"time_limit": None, "warmup": 0, "replications": 1, "deterministic": True})

    problems = validate_model_structured(model)
    codes = [p.code for p in problems]
    assert "E_DETERMINISTIC_NO_MEAN" in codes
    assert all(p.severity == "error" for p in problems if p.code == "E_DETERMINISTIC_NO_MEAN")

    with pytest.raises(ModelValidationError):
        api.run_model(model, replications=1)


def test_the_same_model_off_deterministic_is_not_flagged():
    """The Cauchy DELAY above is perfectly legal OUTSIDE deterministic mode --
    only `simulation.deterministic` turns "no finite mean" into a problem."""
    create_blk = create(max_arrivals=1)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                "params": {"distribution": "Cauchy", "dist_params": {"x0": 0.0, "gamma": 1.0}}}
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk))
    codes = [p.code for p in validate_model_structured(model)]
    assert "E_DETERMINISTIC_NO_MEAN" not in codes


# ---------------------------------------------------------- expression RNG functions
def _custom_expression_delay_time(env, expression):
    create_blk = create(max_arrivals=1)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                "params": {"distribution": "CustomExpression", "dist_params": {"expression": expression}}}
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                  simulation={"time_limit": None, "warmup": 0, "replications": 1, "deterministic": True})
    run_layout(env, model, 0)
    entity = env.ctx.all_entities[0]
    return entity.deletion_time - entity.creation_time


@pytest.mark.parametrize("expression, expected", [
    ("RAND()", 0.5),
    ("EXPO(3)", 3.0),
    ("UNIF(2, 10)", 6.0),
    ("NORM(7, 2)", 7.0),
    ("TRIA(2, 3, 10)", 5.0),
    ("DISC(1, 0.25, 5, 0.75)", 1 * 0.25 + 5 * 0.75),
    ("2 + EXPO(3)", 5.0),
])
def test_expression_rng_functions_return_their_mean_when_deterministic(env, expression, expected):
    """PHASE6 W4 P1, D119: "expression functions that draw random numbers
    return their mean too" -- so a CustomExpression distribution built out of
    one is already exact in deterministic mode, with no `mean()` of its own."""
    assert _custom_expression_delay_time(env, expression) == pytest.approx(expected)


# ---------------------------------------------------------- api.run_model(deterministic=True)
def test_run_model_deterministic_kwarg_forces_replications_to_one():
    """`api.run_model(..., deterministic=True)` (a run override, not a model
    edit) has the same effect as `simulation.deterministic` in the model file:
    replications forced to 1, whatever the model or the caller asked for."""
    create_blk = create(max_arrivals=5, distribution="Exponential", dist_params={"rate": 1.0})
    delay_blk = delay(value=3.0)
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                  simulation={"time_limit": None, "warmup": 0, "replications": 5})

    results = api.run_model(model, replications=5, deterministic=True)

    assert results["system"]["Run"].nunique() == 1
    # A bare DELAY holds no shared resource, so each entity's time in system is
    # exactly the delay -- interarrival gaps (whether random or, here, forced
    # to their mean) never enter into it.
    assert metric_value(results, "Avg Throughput Time") == pytest.approx(3.0)


def test_run_model_deterministic_kwarg_off_leaves_the_model_alone():
    """`deterministic=None` (the default) does not touch `simulation.deterministic`
    -- an ordinary model's replications are honoured exactly as before."""
    create_blk = create(max_arrivals=3, dist_params={"value": 1.0})
    delay_blk = delay(value=1.0)
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                  simulation={"time_limit": None, "warmup": 0, "replications": 3})

    results = api.run_model(model, replications=3)

    assert results["system"]["Run"].nunique() == 3


# ---------------------------------------------------------- run notes (outputs.py)
def test_system_frame_notes_deterministic_only_when_it_is_on():
    create_blk = create(max_arrivals=1)
    delay_blk = delay(value=1.0)
    dispose_blk = dispose()

    det_model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                       simulation={"time_limit": None, "warmup": 0, "replications": 1,
                                  "deterministic": True})
    plain_model = layout([create_blk, delay_blk, dispose_blk], chain(create_blk, delay_blk, dispose_blk),
                         simulation={"time_limit": None, "warmup": 0, "replications": 1})

    det_sys = api.run_model(det_model, replications=1)["system"]
    plain_sys = api.run_model(plain_model, replications=1)["system"]

    assert metric_value({"system": det_sys}, "Deterministic") == 1.0
    assert "Deterministic" not in set(plain_sys["Metric"])


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
