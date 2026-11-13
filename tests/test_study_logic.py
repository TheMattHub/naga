"""D1062: a caller's LOGIC module reaches DOE, optimisation and calibration, so a study
runs the same logic as the single run it explores (the module goes to workers by name)."""
import os
import sys
from pathlib import Path

import pytest

from nagasim import api, calibrate, experiments
from nagasim.optimize import Factor, full_factorial, optimize, run_design
from nagasim.optimize import cli as optimize_cli

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).resolve().parents[1]
KPI = "system/Avg Throughput Time"
RATE = "blocks.process_1.params.dist_params.rate"


def _model():
    """my_logic.RouteBig sends every entity down link 1 (the slow PROCESS branch);
    the default SampleLogic takes link 0 (straight out), so the logic is visible."""
    return {
        "blocks": [
            {"name": "create_1", "type": "CREATE", "position": [0, 0],
             "params": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}},
            {"name": "logic_1", "type": "LOGIC", "position": [1, 0],
             "params": {"logic_class": "RouteBig"}},
            {"name": "out_a", "type": "DISPOSE", "position": [2, 0]},
            {"name": "process_1", "type": "PROCESS", "position": [2, 1],
             "params": {"distribution": "Constant", "dist_params": {"value": 4.0}, "resource_name": "Server"}},
            {"name": "out_b", "type": "DISPOSE", "position": [3, 1]},
        ],
        "links": [{"source": "create_1", "target": "logic_1"},
                  {"source": "logic_1", "target": "out_a"},
                  {"source": "logic_1", "target": "process_1"},
                  {"source": "process_1", "target": "out_b"}],
        "resources": [{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        "simulation": {"time_limit": 40, "replications": 1},
    }


@pytest.fixture
def on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(DATA))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT), str(DATA)]))
    yield
    sys.modules.pop("my_logic", None)


def _single(value, logic, seed=0, reps=2):
    layout = experiments.apply_overrides(_model(), [{"path": "blocks.process_1.params.dist_params.value",
                                                      "value": value}])
    frames = api.run_model(layout, replications=reps, seed=seed, logic=logic)
    flat = experiments.flatten_kpis(frames)
    return [float(v) for k, v in zip(flat["KPI"], flat["Value"]) if k == KPI]


FACTOR = Factor("blocks.process_1.params.dist_params.value", "continuous", 2.0, 6.0)


@pytest.mark.loadsensitive     # #1861: under -n 4 load the process pool's spawned child hung before reading its job
@pytest.mark.parametrize("workers", [1, 2])
def test_doe_matches_single_runs_with_the_logic(on_path, workers):
    design = full_factorial([FACTOR], levels=2)
    with_logic = run_design(_model(), [FACTOR], design, responses=[KPI], replications=2,
                            workers=workers, logic="my_logic").table
    for value, mean in zip(design.iloc[:, 0], with_logic.groupby(FACTOR.name)["Mean"].mean()):
        singles = _single(float(value), "my_logic")
        assert mean == pytest.approx(sum(singles) / len(singles))
        assert mean >= value        # every entity took the PROCESS branch: the logic ran

def test_no_logic_is_unchanged():
    design = full_factorial([FACTOR], levels=2)
    m = _model()
    m["blocks"][1]["params"]["logic_class"] = "SampleLogic"
    a = run_design(m, [FACTOR], design, responses=[KPI], replications=2).table
    b = run_design(m, [FACTOR], design, responses=[KPI], replications=2, logic=None).table
    assert a.equals(b)


@pytest.mark.parametrize("workers", [1, 2])
def test_optimize_runs_the_logic(on_path, workers):
    result = optimize(_model(), [FACTOR], response=KPI, goal="min", budget=24,
                      replications_per_point=3, method="pattern", workers=workers,
                      logic="my_logic")
    # With the logic every entity waits for the PROCESS: the best point is the shortest one.
    assert result.best[FACTOR.name] < 3.0
    assert result.best_mean > 1.0       # without the logic the mean would be ~0


def test_calibrate_runs_the_logic(on_path):
    fit = calibrate.calibrate(_model(), [calibrate.CalibrationTarget(KPI, 4.0)],
                              [Factor("blocks.process_1.params.dist_params.value", "continuous", 1.0, 8.0)],
                              budget=30, replications_per_point=3, logic="my_logic")
    assert 2.0 < fit.best["blocks.process_1.params.dist_params.value"] < 6.0


def test_bad_module_fails_at_once(on_path):
    with pytest.raises(ImportError, match="no_such_mod"):
        run_design(_model(), [FACTOR], full_factorial([FACTOR], levels=2), responses=[KPI],
                   logic="no_such_mod")


def test_cli_flags(on_path, tmp_path, capsys):
    import json
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_model()), encoding="utf-8")
    base = [str(path), "--factor", f"{FACTOR.paths[0]}=continuous:2:6", "--response", KPI,
            "--replications", "2"]
    assert optimize_cli.main(base + ["--design", "full:2", "--logic", "my_logic"], "doe") == 0
    assert "no_such_mod" not in capsys.readouterr().out
    assert optimize_cli.main(base + ["--design", "full:2", "--logic", "no_such_mod"], "doe") == 2
    assert "--logic" in capsys.readouterr().out
    assert calibrate.main([str(path), "--factor", f"{FACTOR.paths[0]}=continuous:1:8",
                           "--target", f"{KPI}=4", "--logic", "no_such_mod"]) == 2
    assert "no_such_mod" in capsys.readouterr().out
