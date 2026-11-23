"""D1389 (conveyor A2 task 4): what a run warns while running reaches `Results.run_problems`, the
agent `run_model` answer and the CLI, once each, besides the log line."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import create, dispose, layout  # noqa: E402
from test_conveyor_divert_spec import belt  # noqa: E402
from test_conveyor_checks import _dead_end, _stale_route  # noqa: E402
from test_conveyor_reverse_rules import _two_belts  # noqa: E402

from nagasim import agent_tools as T
from nagasim import api
from nagasim.agent_tools import runs


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def _open(ws, tmp_path, doc, name="m"):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return T.call("open_model", path=str(path), name=name)


def _limited(doc, limit=50):
    doc["simulation"].update(time_limit=limit, replications=1)
    return doc


def _routing_hold():
    from test_conveyor_divert_spec import hog, pick
    hog_blocks, hog_links = hog("Op", 1000.0)
    return layout([create(name="c", max_arrivals=3, dist_params={"value": 1.0}, start_time=0),
                   belt("T", length=10.0, diverts=[{"at": 3, "rule": {"kind": "fixed", "limit": 1}}]),
                   belt("L", length=2.0, exit_resource="Op"), pick("Op"), dispose(), *hog_blocks],
                  [{"source": "c", "target": "T"}, {"source": "T", "target": "dispose_1"},
                   {"source": "T", "target": "L"}, {"source": "L", "target": "pick"},
                   {"source": "pick", "target": "dispose_1"}, *hog_links],
                  resources=[{"name": "Op", "capacity": 1}], simulation={
                      "time_limit": 50, "warmup": 0, "replications": 1, "time_unit": "seconds"})


def _dead_end_open():
    doc = _dead_end(False)
    doc["blocks"][0]["params"]["max_arrivals"] = 0     # parts for ever: the run is not drained
    return _limited(doc)


CASES = {
    "W_CONVEYOR_HANDED_TO_NOTHING": (_dead_end_open, "A"),
    "W_ROUTE_FORBIDDEN": (lambda: _limited(_stale_route(False)), "T"),
    "W_ROUTE_HELD_AT_END": (_routing_hold, "T"),
    "W_REVERSED_HELD_AT_END": (lambda: _two_belts(loop=False, time_limit=10), "belt"),
}


@pytest.fixture
def unchecked(monkeypatch):
    """What the validator refuses (a belt end linked to nothing) is let through, as a file the
    validator was not asked about is run."""
    from nagasim.core import graph_model
    monkeypatch.setattr(graph_model, "fatal_problems", lambda structured: [])
    monkeypatch.setattr(runs, "_runnable_layout", lambda doc, model, then="": doc.to_layout())


@pytest.mark.parametrize("code", sorted(CASES))
def test_a_run_warning_is_in_the_api_result_once(code, unchecked):
    make, block = CASES[code]
    results = api.run_model(make(), replications=1)
    found = [p for p in results.run_problems if p.code == code]
    assert len(found) == 1 and found[0].block == block and found[0].severity == "warning"
    assert found[0].hint and found[0].message


@pytest.mark.parametrize("code", sorted(CASES))
def test_a_run_warning_is_in_the_agent_run_answer(ws, tmp_path, unchecked, code):
    make, block = CASES[code]
    assert "error" not in _open(ws, tmp_path, make())
    answer = T.call("run_model", model="m")
    assert "error" not in answer, answer
    found = [p for p in answer["problems"] if p["code"] == code]
    assert len(found) == 1 and found[0]["block"] == block and found[0]["hint"]


def test_replications_combine_as_a_union_by_code_and_block():
    doc = _stale_route(False)
    doc["simulation"]["time_limit"] = 50
    results = api.run_model(doc, replications=3)
    assert [(p.code, p.block) for p in results.run_problems] == [("W_ROUTE_FORBIDDEN", "T")]


def test_a_run_with_nothing_to_warn_has_no_problems_and_no_answer_key(ws):
    assert api.run_model("single_server", replications=1, run_length=500, warmup=10).run_problems == []
    from test_agent_tools import build_mm1
    build_mm1()
    assert "problems" not in T.call("run_model", model="mm1", replications=1)


def test_the_cli_prints_a_run_warning(tmp_path, capsys):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_limited(_stale_route(False))), encoding="utf-8")
    assert api.main([str(path), "--replications", "1"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if x.startswith("WARN W_ROUTE_FORBIDDEN"))
    assert line.count("W_ROUTE_FORBIDDEN") == 1, line        # final review M1: the code once
    assert api.main([str(path), "--replications", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["problems"][0]["code"] == "W_ROUTE_FORBIDDEN"


def test_strict_checks_still_raise_and_leave_no_result():
    doc = _limited(_stale_route(True))
    with pytest.raises(RuntimeError, match="W_ROUTE_FORBIDDEN"):
        api.run_model(doc, replications=1)


def test_scenario_results_carry_the_run_problems():
    """Final review M2: `run_scenarios(...).results[name].run_problems` had none; each scenario's
    are the union over its replications, as a run's."""
    from nagasim.experiments import run_scenarios
    results = run_scenarios(_limited(_stale_route(False)), replications=2).results
    assert results and all([(p.code, p.block) for p in r.run_problems] == [("W_ROUTE_FORBIDDEN", "T")]
                           for r in results.values())
