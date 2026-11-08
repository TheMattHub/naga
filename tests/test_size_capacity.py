"""E4 (D1137): `experiments.size_capacity` -- raise a capacity step by step until the next step
does not pay, on paired replications, doubling them until each arm's interval is tight. Small
real runs of the bundled single-server model (capacity 1 is overloaded, 3 is plenty)."""
import json
from pathlib import Path

import numpy as np
import pytest

import nagasim
from nagasim import experiments

MODEL = json.loads((Path(nagasim.__file__).parent / "models" / "single_server.json").read_text())
CAP = "resources.Server.capacity"
KPI = "system/Avg Throughput Time"
QUEUE = "system/Avg Queue Length"
RUN = {"run_length": 150, "warmup": 10}


def _size(**kwargs):
    args = {"kpi": KPI, "tolerance": 0.05, "replications": 3, "max_replications": 12, **RUN}
    return experiments.size_capacity(MODEL, CAP, **{**args, **kwargs})


def test_it_stops_at_the_first_step_that_does_not_pay():
    result = _size()
    steps = result.steps
    assert result.completed and result.answer == 3
    assert list(steps["Level"]) == [1, 2, 3] and list(steps["Pays"]) == [True, True, False]
    assert list(steps.columns) == experiments.SIZING_COLS
    last = steps.iloc[-1]
    assert -last["Delta"] < 0.05 and last["Reason"].startswith("p95 gain")
    # the delta is next minus level of the statistic, paired on the same seeds, inside its interval
    assert (steps["Delta"] == steps["Stat next"] - steps["Stat"]).all()
    assert (steps["Delta LB"] <= steps["Delta"]).all() and (steps["Delta"] <= steps["Delta UB"]).all()
    # a level is never judged again on fewer runs than it had
    assert steps["N"].is_monotonic_increasing


def test_a_guard_that_still_improves_keeps_the_step_paying_until_stop():
    result = _size(tolerance=1e9, guard=QUEUE, stop=4)
    assert result.answer is None and list(result.steps["Next"]) == [2, 3, 4]
    assert all("improves" in reason for reason in result.steps["Reason"])
    assert (result.steps["Guard next"] < result.steps["Guard"]).all()
    # with a guard tolerance the tiny last improvement no longer counts
    held = _size(tolerance=1e9, guard=QUEUE, guard_tolerance=0.5, stop=4)
    assert held.answer == 2 and "does not improve" in held.steps["Reason"].iloc[-1]


def test_replications_double_until_each_arm_is_tight():
    loose = _size(precision=1e9, stop=2)
    assert list(loose.steps["N"]) == [3]
    tight = _size(precision=1e-9, stop=2)
    assert list(tight.steps["N"]) == [12]
    assert (tight.steps[["Half width", "Half width next"]] >= 1e-9).all().all()


def test_the_mean_statistic_and_a_max_goal():
    mean = _size(statistic="mean", stop=3)
    a = mean.steps.iloc[0]
    assert a["Stat"] > a["Stat next"]
    served = _size(kpi="system/Total Entities Completed", goal="max", tolerance=1.0, stop=3,
                   statistic="p50")
    assert served.answer is not None and served.answer <= 2


def test_bad_input_is_refused():
    with pytest.raises(ValueError, match="not a number"):
        experiments.size_capacity(MODEL, "resources.Server.discipline", kpi=KPI, **RUN)
    with pytest.raises(ValueError, match="statistic"):
        _size(statistic="p0")
    with pytest.raises(ValueError, match="unknown KPI"):
        _size(kpi="system/Nope", stop=2)


def test_bootstrap_ci_pairs_the_samples():
    a = np.arange(20.0)
    got = experiments.bootstrap_ci(lambda x, y: np.mean(y - x), a, a + 2.0)
    assert got == {"mean": 2.0, "lb": 2.0, "ub": 2.0}        # paired: the shift has no noise
