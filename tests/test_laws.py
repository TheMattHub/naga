"""Operational-law checks and guardrails (nagasim/laws.py, D684, D685)."""
import copy
import json
from pathlib import Path

import nagasim
from nagasim import api, experiments, laws

MODELS = Path(nagasim.__file__).parent / "models"


def _model(name="single_server.json", **sim):
    layout = json.loads((MODELS / name).read_text(encoding="utf-8"))
    layout["simulation"].update(sim)
    return layout


def _rate(layout, arrival=None, service=None):
    layout = copy.deepcopy(layout)
    if arrival is not None:
        layout["blocks"][0]["params"]["dist_params"] = {"rate": arrival}
    if service is not None:
        layout["blocks"][1]["params"]["dist_params"] = {"rate": service}
    return layout


def _laws(layout, **kw):
    return laws.checks(layout, api.run_model(layout, seed=0, **kw))


def _by_law(found):
    return {c["law"]: c for c in found}


def test_mm1_passes_every_law():
    found = _laws(_model(replications=10))
    assert {"utilisation", "little (resource)", "little (system)", "censoring"} <= set(_by_law(found))
    assert [c for c in found if c["verdict"] != "passed"] == []
    assert laws.summary(found) == {"passed": len(found), "partial": []}


def test_overloaded_model_names_offered_load_and_censoring():
    found = _laws(_rate(_model(replications=3, time_limit=2000), arrival=3.0))
    partial = {c["law"]: c for c in found if c["verdict"] == "partial"}
    assert {"offered load", "censoring"} <= set(partial)
    assert "unstable" in partial["offered load"]["why"]
    assert all(c["verdict"] in ("passed", "partial") for c in found)       # never "failed"
    assert sum(c["name"].endswith(": Server") for c in partial.values()) == 1  # one line per resource


def test_unit_mistake_is_caught():
    found = _laws(_rate(_model(replications=3), service=1.25 / 60))       # mean 48 instead of 0.8
    assert {"utilisation", "offered load"} & {c["law"] for c in found if c["verdict"] == "partial"}
    hours = _rate(_model(), service=75.0)                                # 75 an hour = 0.8 min
    hours["blocks"][1]["params"]["time_unit"] = "hours"
    assert abs(laws._service_mean(hours, "Server") - 0.8) < 1e-9


def test_system_little_holds_after_fix():
    assert _by_law(_laws(_model(replications=5)))["little (system)"]["verdict"] == "passed"


def test_laws_empty_for_a_pure_sd_model():
    assert _laws(_model("sd_population.json", replications=1)) == []


def test_laws_skip_when_service_mean_unknown():
    found = _laws(_model("two_teller_set.json", replications=2))
    assert not {"utilisation", "offered load"} & {c["law"] for c in found}


def test_laws_single_replication_uses_fixed_band():
    ok, _m, p, tol = laws._close([0.80], [0.78])
    assert ok and tol == laws.REL * 0.78
    assert laws._close([0.0], [0.0])[3] == laws.FLOOR
    found = _laws(_model(replications=1))
    assert _by_law(found)["utilisation"]["verdict"] == "passed"


def test_run_model_digest_and_naga_check_carry_the_laws(tmp_path, capsys):
    import shutil

    from nagasim import agent_tools as T
    from nagasim.cli import main as launcher_main
    shutil.copy(MODELS / "single_server.json", tmp_path / "mm1.json")
    T.use_workspace(T.Workspace(tmp_path))
    T.call("open_model", path="mm1.json", name="mm1")
    digest = T.call("run_model", model="mm1", replications=3, run_length=2000, warmup=0)
    assert digest["laws"]["passed"] >= 3 and digest["laws"]["partial"] == []
    over = _rate(_model(replications=2, time_limit=2000), arrival=3.0)
    (tmp_path / "over.json").write_text(json.dumps(over), encoding="utf-8")
    assert launcher_main(["check", str(tmp_path / "over.json")]) == 0    # warnings, not failures
    out = capsys.readouterr().out
    assert "[WARN] law" in out and "offered load: Server" in out and "laws: " in out


def test_initial_bias_flag():
    layout = _rate(_model(replications=5, time_limit=2000, warmup=0), arrival=1.2)
    results = api.run_model(layout, seed=0)
    bias = _by_law(laws.checks(layout, results))["initial bias"]
    assert bias["verdict"] == "partial" and "warm-up" in bias["why"]
    layout["simulation"]["warmup"] = experiments.suggest_warmup(results).warmup
    assert "initial bias" not in _by_law(_laws(layout))
