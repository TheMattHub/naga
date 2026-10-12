"""PHASE6 C2: agent attribute statistics (`core/outputs.summarize_agent_attributes`,
ARCHITECTURE 5.25). Exact values on a deterministic model: two agents both
start with `self.x = 1`, a timeout at t=5 sets `self.x = 10` (held to the end),
so at t1=20 both are in state "B" with x=10 and the population mean was 1 for
[0, 5) and 10 for [5, 20] -- Time-Avg Mean = (1*5 + 10*15) / 20 = 7.75 exactly.
"""
from pathlib import Path

import numpy as np
import pytest

from nagasim import api
from nagasim.core import agents as A
from nagasim.core.sim_runner import run_single_simulation
from nagasim.experiments import flatten_kpis

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"

XWORKER = A.AgentType(
    "Worker", attributes=[A.Attribute("x", "1")], states=["A", "B"], initial="A",
    transitions=[A.Transition("A", "B", "timeout", "5", actions=[A.Action.set("self.x", "10")])])


def _model(*records, time_limit=20):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def test_agent_attributes_exact_end_and_time_avg():
    layout = _model(XWORKER, A.Population("People", "Worker", 2))
    results = run_single_simulation(layout, seed=0, run_len=20, warmup=0)
    frame = results["agent_attributes"].set_index("Agent Attribute")

    all_row = frame.loc["People/x/All"]
    assert all_row["End Mean"] == pytest.approx(10.0)
    assert all_row["End P05"] == pytest.approx(10.0)
    assert all_row["End P50"] == pytest.approx(10.0)
    assert all_row["End P95"] == pytest.approx(10.0)
    assert all_row["End Min"] == pytest.approx(10.0)
    assert all_row["End Max"] == pytest.approx(10.0)
    assert all_row["Time-Avg Mean"] == pytest.approx(7.75)

    # State "A" is empty at t1 (both agents moved to "B" at t=5): NaN end stats
    # and NaN Time-Avg Mean (only the "All" row tracks a time average, C2 scope).
    a_row = frame.loc["People/x/A"]
    assert np.isnan(a_row["End Mean"])
    assert np.isnan(a_row["Time-Avg Mean"])

    b_row = frame.loc["People/x/B"]
    assert b_row["End Mean"] == pytest.approx(10.0)
    assert np.isnan(b_row["Time-Avg Mean"])

    log = results["agent_attribute_log"]
    assert sorted(log["Value"]) == [10.0, 10.0]


def test_agent_attributes_excludes_dead_agents():
    """C5: an agent removed before run end never counts toward End stats, but
    its alive span still counts toward Time-Avg Mean."""
    worker = A.AgentType(
        "Worker", attributes=[A.Attribute("x", "2")], states=["A"], initial="A",
        transitions=[A.Transition("A", "A", "timeout", "5", actions=[A.Action.remove("self")])])
    layout = _model(worker, A.Population("People", "Worker", 1), time_limit=10)
    results = run_single_simulation(layout, seed=0, run_len=10, warmup=0)
    frame = results["agent_attributes"].set_index("Agent Attribute")

    all_row = frame.loc["People/x/All"]
    assert np.isnan(all_row["End Mean"])            # no agent alive at t1
    assert all_row["Time-Avg Mean"] == pytest.approx(1.0)   # (2*5 + 0*5) / 10

    assert results["agent_attribute_log"].empty


def test_agent_attributes_frame_empty_without_agent_attributes():
    """Absent-in-spirit: fixed, empty columns for a model with no populations,
    and for a population whose type declares no attributes."""
    results = api.run_model(str(MODELS_DIR / "single_server.json"), replications=1, seed=0,
                            warmup=0, run_length=50)
    assert results["agent_attributes"].empty
    assert results["agent_attribute_log"].empty
    assert list(results["agent_attributes"].columns) == [
        "Run", "Agent Attribute", "Population", "Attribute", "State", "Category", "End Mean",
        "End P05", "End P50", "End P95", "End Min", "End Max", "Time-Avg Mean"]

    plain = A.AgentType("Plain", states=["A", "B"],
                        transitions=[A.Transition("A", "B", "timeout", "1")])
    layout = _model(plain, A.Population("Plains", "Plain", 5), time_limit=5)
    results = run_single_simulation(layout, seed=0, run_len=5, warmup=0)
    assert results["agent_attributes"].empty          # Plain declares no attributes
    assert not results["agent_summary"].empty          # agent_summary is unaffected


def test_agent_attributes_export_sheet_present(tmp_path):
    layout = _model(XWORKER, A.Population("People", "Worker", 2))
    results = api.run_model(layout, replications=1, seed=0, warmup=0, run_length=20)
    from nagasim.core.export import export_workbook
    path = export_workbook(results, tmp_path / "out.xlsx")

    import openpyxl
    assert "Agent attributes" in openpyxl.load_workbook(path).sheetnames


def test_agent_attributes_kpi_flatten_keys():
    layout = _model(XWORKER, A.Population("People", "Worker", 2))
    results = api.run_model(layout, replications=1, seed=0, warmup=0, run_length=20)
    flat = flatten_kpis(results)
    keys = set(flat["KPI"])
    assert "agent/People/x/All/End Mean" in keys
    assert "agent/People/x/All/Time-Avg Mean" in keys
