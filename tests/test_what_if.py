"""D685/D686: what_if and the run_scenarios guardrail columns."""
import json
import math
from pathlib import Path

import numpy as np
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


# ---------------------------------------------------------------- the input-data note (C3, D737)
SERVICE = "blocks.process_1.params.distribution"
SMALL = {"kpis": [TIME], "resamples": 3, "replications": 2, "run_length": 100, "warmup": 10}


@pytest.fixture
def ws(tmp_path):
    from nagasim import agent_tools as T
    rng = np.random.default_rng(4)
    (tmp_path / "service.csv").write_text(
        "minutes\n" + "\n".join(f"{v:.6f}" for v in rng.exponential(0.8, 40)) + "\n", encoding="utf-8")
    layout = api.load_layout(MODEL)
    layout["simulation"].update(time_limit=300, warmup=30)
    (tmp_path / "m.json").write_text(json.dumps(layout), encoding="utf-8")
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="m.json", name="m")
    return T


def _what_if(T):
    return T.call("what_if", model="m", overrides=cap(2), kpis=[TIME], goals={TIME: "min"}, cap=10)


def test_what_if_notes_the_recorded_input_spread_and_keeps_its_verdict(ws):
    T = ws
    plain = _what_if(T)
    assert "error" not in plain and "input_note" not in plain        # nothing recorded yet
    inputs = [{"path": SERVICE, "data": "service.csv"}]
    level = T.call("input_uncertainty", model="m", inputs=inputs, **SMALL)
    assert "error" not in level, level
    noted = _what_if(T)
    assert noted["kpis"] == plain["kpis"] and noted["n"] == plain["n"]      # the verdict is untouched
    note = noted["input_note"]
    assert note["measure"] == "level" and note["resamples"] == 3 and note["replications"] == 2
    assert "level" in note["text"] and "3 resamples" in note["text"]
    row = level["kpis"][0]
    assert note["kpis"] == [{"kpi": TIME, "mean": row["mean"], "input_interval": row["input_interval"],
                             "input_share": row["input_share"]}]
    # A difference measured for these overrides is quoted instead of the level, never merged with it.
    diff = T.call("input_uncertainty", model="m", inputs=inputs, overrides=cap(2), **SMALL)
    assert "error" not in diff and diff["measure"] == "difference"
    again = _what_if(T)
    assert again["kpis"] == plain["kpis"]
    assert again["input_note"]["measure"] == "difference"
    assert again["input_note"]["kpis"][0]["input_interval"] == diff["kpis"][0]["input_interval"]
    assert "difference" in again["input_note"]["text"]
    # Other overrides have no difference recorded: the level again.
    other = T.call("what_if", model="m", overrides=cap(3), kpis=[TIME], cap=10)
    assert other["input_note"]["measure"] == "level"
