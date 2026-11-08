"""FixedSchedule arrival jitter (E6, D1125): a shift per listed arrival, drawn per replication
from the CREATE block's own stream, kept in order by a running maximum, handled in the engine
so an arrival may come earlier than its listed time with no release lead."""
from __future__ import annotations

import numpy as np
import pytest

from conftest import chain, create, dispose, layout

from nagasim import api
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.schema import validate_model_structured
from nagasim.core.trace import TraceRecorder

PLAN = [0.0, 1.0, 1.5, 2.0, 10.0, 10.2, 30.0]


def _model(jitter=None, law=None, start_time=0, deterministic=False):
    params = {"schedule": PLAN}
    if jitter is not None:
        params["jitter"] = jitter
    if law is not None:
        params["jitter_law"] = law
    src = create(distribution="FixedSchedule", dist_params=params, start_time=start_time)
    out = dispose()
    return layout([src, out], chain(src, out),
                  simulation={"time_limit": 100.0, "warmup": 0, "replications": 1,
                              "deterministic": deterministic})


def _arrivals(model, seed=0):
    trace = TraceRecorder()
    api.run_model(model, replications=1, seed=seed, trace=trace)
    return [e.t for e in trace.events if e.kind == "entity_created"]


def test_jitter_keeps_order_and_count_and_moves_arrivals_both_ways():
    times = _arrivals(_model(jitter=2.0))
    assert len(times) == len(PLAN)
    assert times == sorted(times) and times[0] >= 0.0          # FIFO by the running maximum
    assert times != PLAN
    # an arrival earlier than its listed time needs no lead: the block waits less
    early = [t < p - 1e-9 for seed in range(4)
             for t, p in zip(_arrivals(_model(jitter=2.0), seed), PLAN)]
    assert any(early)


def test_each_arrival_is_the_running_maximum_of_the_jittered_plan():
    rng = np.random.default_rng(3)
    dist = StochasticDistribution("FixedSchedule", {"schedule": PLAN, "jitter": 2.0})
    got = []
    while (t := dist.sample(rng, current=0.0)) is not None:
        got.append(t)
    shifts = np.random.default_rng(3).normal(0.0, 2.0, len(PLAN))
    prev, want = 0.0, []
    for p, s in zip(PLAN, shifts):
        prev = max(prev, p + s)
        want.append(prev)
    assert got == want


def test_seeded_per_replication_and_reproducible():
    a, b, c = _arrivals(_model(jitter=1.0), 0), _arrivals(_model(jitter=1.0), 0), _arrivals(_model(jitter=1.0), 1)
    assert a == b and a != c


def test_uniform_law_has_the_same_spread_and_bounds():
    rng = np.random.default_rng(0)
    big = [float(t) for t in range(0, 200000, 100)]
    dist = StochasticDistribution("FixedSchedule", {"schedule": big, "jitter": 5.0, "jitter_law": "Uniform"})
    shifts = np.array([dist.sample(rng, current=0.0) for _ in big[:-1]]) - np.array(big[:-1])
    assert abs(shifts.max()) <= 5.0 * np.sqrt(3.0) + 1e-9 and shifts.std() == pytest.approx(5.0, rel=0.1)
    with pytest.raises(ValueError, match="jitter_law"):
        StochasticDistribution("FixedSchedule", {"schedule": [1.0], "jitter": 1.0,
                                                 "jitter_law": "Cauchy"}).sample(rng, current=0.0)


def test_default_off_draws_nothing_and_deterministic_mode_drops_the_jitter():
    assert _arrivals(_model()) == PLAN
    assert _arrivals(_model(jitter=0.0, law="Normal")) == PLAN        # explicit zero: still exact
    assert _arrivals(_model(jitter=3.0, deterministic=True)) == PLAN
    rng = np.random.default_rng(5)
    before = rng.bit_generator.state
    dist = StochasticDistribution("FixedSchedule", {"schedule": PLAN})
    while dist.sample(rng, current=0.0) is not None:
        pass
    assert rng.bit_generator.state == before                          # no draw without a jitter


def test_start_time_drops_listed_times_before_it_and_no_jittered_arrival_precedes_it():
    times = _arrivals(_model(jitter=3.0, start_time=5))
    assert len(times) == 3 and min(times) >= 5.0                     # 10, 10.2 and 30 kept (#1396)


def test_a_jittered_model_validates_with_its_law_word():
    problems = validate_model_structured(_model(jitter=1.0, law="Uniform"))
    assert not [p for p in problems if p.severity == "error"], problems
