"""D685/D686: what_if and the run_scenarios guardrail columns."""
import math
from pathlib import Path

import pytest

from nagasim import api, experiments
from nagasim.core.schema import ModelValidationError

MODEL = Path(experiments.__file__).parent / "models" / "single_server.json"
TIME = "system/Avg Throughput Time"


@pytest.fixture
def mm1():
    layout = api.load_layout(MODEL)
    layout["simulation"].update(time_limit=300, warmup=30)
    return layout


def cap(n):
    return [{"path": "resources.Server.capacity", "value": n}]


def test_second_server_is_better_with_ci(mm1):
    out = experiments.what_if(mm1, cap(2), [TIME], goals={TIME: "min"})
    row = out["kpis"][0]
    assert row["verdict"] == "better" and row["diff_ub"] < 0 and out["met"]


def test_no_change_is_unchanged_at_once(mm1):
    out = experiments.what_if(mm1, [{"path": "blocks.process_1.params.dist_params.rate",
                                     "value": 1.25}], [TIME], cap=10)  # no change: diff 0
    assert out["n"] == 5 and out["met"]                                # #766
    assert out["kpis"][0]["verdict"] == "unchanged"


def test_what_if_unknown_override_is_an_error(mm1):
    with pytest.raises(ModelValidationError):
        experiments.what_if(mm1, [{"path": "resources.Nope.capacity", "value": 2}])


def test_replications_needed_when_not_significant(mm1):
    mm1["scenarios"] = [{"name": "Tiny", "overrides": [
        {"path": "blocks.process_1.params.dist_params.rate", "value": 1.26}]}]
    table = experiments.run_scenarios(mm1, replications=5, seed=0, crn=False).comparison(kpis=[TIME])
    row = table[table["Scenario"] == "Tiny"].iloc[0]
    assert not row["Significant"] and row["Replications Needed"] >= 5
    base = table[table["Scenario"] == "Baseline"].iloc[0]
    assert math.isnan(base["Replications Needed"])


def test_crn_ratio_above_one_for_paired_scenarios(mm1):
    mm1["scenarios"] = [{"name": "Two", "overrides": cap(2)}]
    table = experiments.run_scenarios(mm1, replications=8, seed=0).comparison(kpis=[TIME])
    assert table[table["Scenario"] == "Two"].iloc[0]["CRN Ratio"] > 1
