"""The open-and-run gate (U5, D1118): `naga check` passes a model only if it opens with no error
using this user's enabled plugins and runs one replication; the agent tools that build models
run it, and can only PROPOSE a plugin folder (switched off) for the person to trust."""
import json
from pathlib import Path

import pytest

import nagasim
from nagasim import agent_tools, api, modelcheck
from nagasim.core import schema
from nagasim.settings import trusted

SINGLE = Path(nagasim.__file__).parent / "models" / "single_server.json"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path / "user"))
    return tmp_path


def _declared_plugin_model() -> dict:
    layout = json.loads(SINGLE.read_text(encoding="utf-8"))
    layout["blocks"].append({"name": "Yard", "type": "MY_YARD"})
    layout.setdefault("info", {})["requires_plugins"] = ["MY_YARD"]
    return layout


def test_a_good_model_passes_and_a_missing_plugin_says_where_to_fix_it():
    assert modelcheck.open_and_run(str(SINGLE)).status == "pass"
    gate = modelcheck.open_and_run(_declared_plugin_model())
    assert gate.status == "fail" and gate.kind == "gate"
    assert "Yard: MY_YARD comes from a plugin that is not loaded" in gate.message
    assert "Settings > Plugins" in gate.message


def test_a_model_that_raises_in_its_run_fails_the_gate(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("the yard controller has no crane")
    monkeypatch.setattr(api, "run_model", boom)
    gate = modelcheck.open_and_run(str(SINGLE))
    assert gate.status == "fail"
    assert gate.message == "does not run: RuntimeError: the yard controller has no crane"


def test_the_cli_stops_at_the_gate_and_says_why(tmp_path, capsys):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_declared_plugin_model()), encoding="utf-8")
    assert modelcheck.main(["check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] gate" in out and "MY_YARD comes from a plugin that is not loaded" in out
    assert modelcheck.main(["check", str(path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["gate"]["status"] == "fail"
    assert modelcheck.main(["check", str(SINGLE)]) == 0
    assert "[PASS] gate" in capsys.readouterr().out


def test_the_agent_gate_tool(tmp_path):
    agent_tools.use_workspace(agent_tools.Workspace(tmp_path))
    agent_tools.call("write_model", model="m", layout=_declared_plugin_model())
    found = agent_tools.call("check_model_opens", model="m")
    assert found["passed"] is False and "MY_YARD comes from a plugin" in found["message"]


def test_the_agent_can_only_propose_a_folder_inside_its_workspace_switched_off(store):
    root = store / "ws"
    plugs = root / "plugins"
    plugs.mkdir(parents=True)
    outside = store / "elsewhere"
    outside.mkdir()
    agent_tools.use_workspace(agent_tools.Workspace(root))
    agent_tools.call("write_model", model="m", layout=json.loads(SINGLE.read_text(encoding="utf-8")))
    refused = agent_tools.call("propose_plugin_folder", model="m", folder=str(outside))
    assert refused["error"]["code"] == "E_PATH_OUTSIDE_ROOT"
    assert trusted.entries() == []
    made = agent_tools.call("propose_plugin_folder", model="m", folder="plugins")
    assert (made["state"], made["enabled"]) == ("proposed", False)
    [entry] = trusted.entries()
    assert (entry.added_by, entry.enabled) == ("assistant", False)
    # The person enables it; a second proposal leaves the person's choice alone.
    trusted.set_enabled("plugin_folder", plugs, True)
    again = agent_tools.call("propose_plugin_folder", model="m", folder="plugins")
    assert (again["state"], again["enabled"]) == ("listed", True)
    assert trusted.is_trusted("plugin_folder", plugs)
    # No argument of the tool can switch an entry on (permission is the caller's).
    assert set(agent_tools.TOOLS["propose_plugin_folder"].input_schema["properties"]) == {"model", "folder"}


def test_check_model_opens_stops_at_its_budget(tmp_path):
    """Review S8-12: the gate ran the model's whole run length with no budget. `max_seconds` stops it, and a
    run stopped by the budget is neither a pass nor a fail: it opens with no errors and says when it stopped."""
    layout = json.loads(SINGLE.read_text(encoding="utf-8"))
    layout["simulation"] = {**layout["simulation"], "time_limit": 1e9, "warmup": 0}     # far longer than a call
    gate = modelcheck.open_and_run(layout, max_seconds=0.2)
    assert gate.status == "inconclusive" and not gate.passed
    assert gate.message == "opens with no errors; the run was stopped after 0.2 s"
    agent_tools.use_workspace(agent_tools.Workspace(tmp_path))
    agent_tools.call("write_model", model="m", layout=layout)
    found = agent_tools.call("check_model_opens", model="m", max_seconds=0.2)
    assert (found["passed"], found["status"]) == (False, "stopped") and "stopped after 0.2 s" in found["message"]
