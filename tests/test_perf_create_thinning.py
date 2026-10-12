"""CREATE in "Schedule" mode waits once per accepted arrival, not once per
thinning candidate (performance report 2026-10-07, C1): same random stream,
same arrival instants, fewer SimPy events, and a zero-rate stretch still ends.
"""
import numpy as np
import pytest

from nagasim.core.base_process import deterministic_hash
from nagasim.core.schedules import Schedule
from nagasim.core.sim_runner import run_single_simulation

from conftest import chain, create, dispose, layout

RUN_LEN = 400.0
# Busy, then closed for 300 time units (about 2,400 rejected candidates in a
# row at the maximum rate of 8), then open again.
POINTS = [[20.0, 8.0], [10.0, 2.0], [300.0, 0.0], [70.0, 5.0]]


def _model(points=POINTS):
    arrivals = create("nhpp", arrival_mode="Schedule", schedule_name="day", e_type="N")
    ticker = create("ticker", e_type="T")      # one every time unit, from t=0
    ticker["position"] = [0, 1]
    out = dispose("out")
    return layout([arrivals, ticker, out], chain(arrivals, out) + chain(ticker, out),
                  schedules=[{"name": "day", "kind": "arrival", "repeat": False, "points": points}])


def _thinning_by_hand(seed, points=POINTS):
    """(arrival times, candidates) of the block named "nhpp": one exponential
    and one uniform per candidate from the block's own stream, each candidate
    time the float sum SimPy's clock would have held."""
    rng = np.random.default_rng(np.random.SeedSequence([seed, deterministic_hash("nhpp")]))
    schedule = Schedule("day", "arrival", points)
    max_rate = schedule.max_value
    t, times, candidates = 0.0, [], 0
    while True:
        t += float(rng.exponential(1.0 / max_rate))
        accepted = rng.random() * max_rate <= schedule.value_at(t)
        if t > RUN_LEN:         # an event exactly at the run length still happens
            return times, candidates
        candidates += 1
        if accepted:
            times.append(t)


def _run(seed, points=POINTS):
    steps = 0

    def count_step():
        nonlocal steps
        steps += 1
        return False

    log = run_single_simulation(_model(points), seed=seed, run_len=RUN_LEN, warmup=0,
                                should_stop=count_step)["entity_log"]
    return log, steps


@pytest.mark.parametrize("seed", [0, 7, 11])
def test_arrivals_land_on_the_thinned_instants_bit_for_bit(seed):
    log, _ = _run(seed)
    expected, _ = _thinning_by_hand(seed)
    assert list(log[log["Type"] == "N"]["Created At"]) == expected
    assert not [t for t in expected if 30.0 <= t < 330.0]      # the closed stretch
    assert [t for t in expected if t >= 330.0]                 # and it reopens


def test_a_zero_rate_stretch_ends_and_starves_nobody():
    """Hundreds of candidates are rejected in a row without the clock moving
    on their account; the run still reaches its end and the other source
    creates its entity at every time unit of the stretch."""
    log, _ = _run(0)
    assert list(log[log["Type"] == "T"]["Created At"]) == [float(t) for t in range(int(RUN_LEN) + 1)]


def test_a_rejected_candidate_costs_no_event():
    """Open for one time unit, then closed: thousands of candidates, a handful
    of arrivals. Against the same run with the source off (a zero maximum
    rate stops it at once), the source adds a small fraction of one event per
    candidate; before, it added at least one each."""
    points = [[1.0, 8.0], [1000.0, 0.0]]
    _, steps = _run(0, points)
    _, baseline = _run(0, [[1.0, 0.0]])
    arrivals, candidates = _thinning_by_hand(0, points)
    assert arrivals and candidates > 3000
    assert steps - baseline < candidates / 10
