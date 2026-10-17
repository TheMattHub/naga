"""Loop dominance, Loops That Matter (depth bucket L1, D141-D143): the refusals,
the frames' shape, a lone loop's +1 and the CLI/agent surfaces. The worked
examples of the semantics note are the oracle file's (test_loopdominance_oracles.py)."""
import json
from pathlib import Path

import pytest

from nagasim import cli as launcher
from nagasim import loopdominance

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _lone_loop(sd=True) -> dict:
    """Semantics note example A: S -> g = 0.1*S -> inflow -> S, Euler dt 1."""
    return {"blocks": [], "links": [],
            "simulation": {"time_limit": 3.0, **({"sd": {"dt": 1.0}} if sd else {})},
            "levels": [{"name": "S", "initial": 100, "min": None}],
            "flows": [{"name": "inflow", "rate": 'AUX("g")', "to": "S"}],
            "auxiliaries": [{"name": "g", "expr": '0.1 * LEVEL("S")'}]}


def test_without_the_sd_grid_it_is_refused_with_e_loop_grid():
    with pytest.raises(loopdominance.LoopDominanceError) as exc:
        loopdominance.analyse(_lone_loop(sd=False))
    assert exc.value.problem.code == "E_LOOP_GRID"


def test_a_lone_loop_scores_plus_one_at_every_step_from_t0_plus_dt():
    result = loopdominance.analyse(_lone_loop())
    steps, loops = result["loop_dominance"], result["loops"]
    assert list(steps.columns) == loopdominance.DOMINANCE_COLUMNS
    assert list(loops.columns) == loopdominance.LOOP_COLUMNS
    assert steps["Time"].tolist() == [1.0, 2.0, 3.0]            # no t0 row
    assert steps["Score"].tolist() == [1.0] * 3 and steps["Relative"].tolist() == [1.0] * 3
    assert steps["Dominant"].all()
    assert loops.to_dict(orient="records") == [
        {"Loop": "R1", "Kind": "R", "Partition": 1, "Length": 3, "Nodes": ["g", "inflow", "S"],
         "Total": 1.0}]
    assert result["truncated"] is False and result["problems"] == []


def test_sd_sir_hand_off_matches_the_tutorial_numbers():
    """Docs drift guard: the loop-dominance tutorial (site_docs/tutorials/loop-dominance.md)
    quotes R1 dominant at t=1, B1 at t=25, B2 at t=40 on the shipped sd_sir.json (rk4, dt=0.1) --
    pin the exact values so a change to the model or the scoring silently breaks the docs."""
    result = loopdominance.analyse(MODELS / "sd_sir.json")
    dom = result["loop_dominance"].set_index(["Time", "Loop"])["Relative"]
    assert dom[(1.0, "R1")] == pytest.approx(0.798936, abs=5e-6)
    assert dom[(25.0, "B1")] == pytest.approx(-0.730074, abs=5e-6)
    assert dom[(40.0, "B2")] == pytest.approx(-0.665685, abs=5e-6)


def test_a_model_with_des_blocks_warns_w_loop_hybrid():
    result = loopdominance.analyse(MODELS / "sd_supply_game.json", run_length=1.0)
    assert [p.code for p in result["problems"]] == ["W_LOOP_HYBRID"]


def test_cli_prints_the_loop_table_and_writes_both_csvs(tmp_path, capsys):
    model = tmp_path / "m.json"
    model.write_text(json.dumps(_lone_loop()), encoding="utf-8")
    assert launcher.main(["loops", str(model), "--csv", str(tmp_path / "out.csv")]) == 0
    assert "R1" in capsys.readouterr().out
    assert (tmp_path / "out.csv").is_file() and (tmp_path / "out_dominance.csv").is_file()


def test_agent_tool_returns_loops_with_dominant_intervals(tmp_path):
    from nagasim import agent_tools as T
    (tmp_path / "m.json").write_text(json.dumps(_lone_loop()), encoding="utf-8")
    (tmp_path / "flat.json").write_text(json.dumps(_lone_loop(sd=False)), encoding="utf-8")
    T.use_workspace(T.Workspace(tmp_path))
    T.call("open_model", path="m.json", name="m")
    T.call("open_model", path="flat.json", name="flat")
    out = T.call("run_loop_dominance", model="m")
    assert out["loop_count"] == 1 and out["truncated"] is False
    assert out["loops"][0]["loop"] == "R1" and out["loops"][0]["dominant"] == [[1.0, 3.0]]
    assert T.call("run_loop_dominance", model="flat")["error"]["code"] == "E_LOOP_GRID"


@pytest.mark.parametrize("path", sorted(
    p for p in MODELS.glob("*.json")
    if "sd" in (json.loads(p.read_text(encoding="utf-8")).get("simulation") or {})),
    ids=lambda p: p.stem)
def test_every_bundled_sd_model_analyses(path):
    """#678: FLOW() reads a flow's `per`, so the loop-dominance flow stand-ins carry one."""
    assert len(loopdominance.analyse(str(path))["loops"]) > 0
