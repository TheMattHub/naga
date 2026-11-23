"""Item code on the agent surfaces: an agent writes code and runs it once a person has trusted
it, never trusts it itself, and no tool path reaches the trust store (D1344, amending D186)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from _astscan import imports_module
from nagasim import agent_tools as T
from nagasim import api, modelcheck, viewer
from nagasim.agent_tools import ToolError
from nagasim.core.itemlogic import ItemCodeUntrusted
from nagasim.settings import itemtrust, trustwrite
from test_item_logic import ALTERNATE, model, routed

ROOT = Path(__file__).resolve().parents[1]
RAISING = ("class Gate:\n    def on_entry(self, entity, item):\n"
           "        return 1 / 0\n")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Sibling folders: the person's store under `user`, the agent's workspace under `ws`."""
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path / "user"))
    (tmp_path / "ws").mkdir()
    return T.use_workspace(T.Workspace(tmp_path / "ws"))


def trust(source):
    """The person's yes, which no tool can give."""
    return trustwrite.trust_source(source, label="test", by="test")


def opened(ws, layout, name="m"):
    (ws.root / f"{name}.json").write_text(json.dumps(layout), encoding="utf-8")
    out = T.call("open_model", path=f"{name}.json", name=name)
    assert "error" not in out, out
    return name


def code_of(out):
    return out.get("error", {}).get("code")


def test_agent_writes_code_and_gets_untrusted_back(ws):
    name = opened(ws, model())
    out = T.call("set_item_code", model=name, definition="Gate", source=ALTERNATE, class_name="Gate")
    assert out["ok"] and out["trusted"] is False and out["problems"] == []
    assert out["fingerprint"] == itemtrust.fingerprint(ALTERNATE)
    run = T.call("run_model", model=name, replications=1)
    assert code_of(run) == "E_ITEM_CODE_UNTRUSTED", run
    assert "Gate (" + itemtrust.short(out["fingerprint"]) + ")" in run["error"]["message"]
    assert "you cannot trust code yourself" in run["error"]["hint"]


def test_every_run_surface_refuses_untrusted_code_with_the_same_code(ws):
    name = opened(ws, model())
    for tool, extra in (("run_model", {"replications": 1}), ("suggest_warmup", {}),
                        ("debug_run", {}), ("run_scenarios", {})):
        assert code_of(T.call(tool, model=name, **extra)) == "E_ITEM_CODE_UNTRUSTED", tool


def test_set_item_code_reports_problems_and_keeps_placed(ws):
    layout = model()
    layout["submodels"]["Gate"]["code"]["placed"] = {"author": "Lib", "fingerprint": "sha256:" + "1" * 64}
    name = opened(ws, layout)
    out = T.call("set_item_code", model=name, definition="Gate", class_name="Gate",
                 source="class Gate:\n    def on_entry(self, entity, item)\n        return 0\n")
    assert out["ok"] and out["problems"] and "does not parse at line 2" in out["problems"][0]
    assert ws.document(name).definition_code("Gate")["placed"]["author"] == "Lib"
    assert code_of(T.call("set_item_code", model=name, definition="Nope", source=ALTERNATE,
                          class_name="Gate")) == "E_UNKNOWN_SUBMODEL"


def test_trusted_code_runs_inside_the_consent_drop(ws):
    name = opened(ws, model())
    trust(ALTERNATE)
    with T.python_consent_dropped():
        out = T.call("run_model", model=name, replications=1)
    assert "error" not in out, out
    counts = routed(ws.results[name])
    assert counts[("g1/route", "g1_a")] == 5 and counts[("g1/route", "g1_b")] == 5


def test_the_drop_still_refuses_agent_python(ws, monkeypatch):
    from nagasim.core import agents as A
    from test_agents_python import _ring
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    name = opened(ws, _ring(python=True), "py")
    with T.python_consent_dropped():
        assert code_of(T.call("run_model", model=name, replications=1)) == "E_AGENT_PYTHON_REFUSED"


def test_one_changed_character_needs_trust_again(ws):
    name = opened(ws, model())
    trust(ALTERNATE)
    assert T.call("set_item_code", model=name, definition="Gate", source=ALTERNATE,
                  class_name="Gate")["trusted"] is True
    out = T.call("set_item_code", model=name, definition="Gate", source=ALTERNATE + "# x\n",
                 class_name="Gate")
    assert out["trusted"] is False
    assert code_of(T.call("run_model", model=name, replications=1)) == "E_ITEM_CODE_UNTRUSTED"


def test_no_tool_can_trust():
    assert not [name for name in T.TOOLS if "trust" in name]
    for spec in T.TOOLS.values():
        props = set(spec.input_schema.get("properties", {}))
        assert not props & {"trusted", "fingerprint", "trust"}, spec.name


def test_no_agent_surface_imports_the_trust_writer():
    """Only a person's dialog and terminal command write the store; an agent tool module, the MCP
    server or the assistant importing the writer would be a path for a model to trust code."""
    files = [*sorted((ROOT / "nagasim" / "agent_tools").glob("*.py")),
             ROOT / "nagasim" / "mcp_server.py", ROOT / "nagasim" / "assistant.py"]
    assert len(files) > 10
    assert not [p.name for p in files if imports_module(p, "trustwrite")]


def test_store_is_outside_every_tool_path(ws, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(ws.root / "user"))
    with pytest.raises(ToolError) as refused:
        T.workspace().resolve("user/trusted_item_code.json")
    assert refused.value.code == "E_PATH_OUTSIDE_ROOT"
    with pytest.raises(ToolError):
        T.workspace().resolve("USER/../user/trusted_code.json")
    assert T.workspace().resolve("users.json") == ws.root / "users.json"   # a sibling name is fine
    out = T.call("save_model", model=opened(ws, model()), path="user/trusted_item_code.json")
    assert code_of(out) == "E_PATH_OUTSIDE_ROOT"
    assert not (ws.root / "user" / "trusted_item_code.json").exists()


def test_viewer_refuses_item_code():
    assert any("does not run item code" in p.message for p in viewer.viewer_problems(model()))
    assert not any("item code" in p.message for p in viewer.viewer_problems(
        {k: v for k, v in model().items() if k != "submodels"}))


def test_write_model_with_code_is_untrusted_until_the_person_says_yes(ws):
    out = T.call("write_model", name="w", layout=model())
    assert "error" not in out, out
    assert code_of(T.call("run_model", model="w", replications=1)) == "E_ITEM_CODE_UNTRUSTED"
    trust(ALTERNATE)
    assert "error" not in T.call("run_model", model="w", replications=1)


def test_a_raising_hook_reaches_the_agent_as_an_answer(ws):
    name = opened(ws, model(RAISING))
    trust(RAISING)
    out = T.call("run_model", model=name, replications=1)
    assert code_of(out) == "E_RUN_FAILED", out
    assert "Gate.on_entry, line 3" in out["error"]["message"]


def test_modelcheck_gate_refuses_untrusted_code(ws):
    path = ws.root / "m.json"
    path.write_text(json.dumps(model()), encoding="utf-8")
    with pytest.raises(OSError):
        modelcheck.open_and_run(path)


def test_a_refusal_rebuilt_without_its_list_still_names_the_item(ws):
    """A worker pool may hand the exception back from its message alone."""
    from nagasim.agent_tools.core import _untrusted_error
    fp = itemtrust.fingerprint(ALTERNATE)
    exc = ItemCodeUntrusted(f"item code is not trusted on this computer: Gate ({itemtrust.short(fp)}); "
                            "a person trusts it in the item's code panel or with `naga trust MODEL`")
    err = _untrusted_error(exc)
    assert err.code == "E_ITEM_CODE_UNTRUSTED"
    assert err.message.endswith(f": Gate ({itemtrust.short(fp)})")


@pytest.mark.slow
def test_experiment_workers_run_trusted_code_and_refuse_untrusted(ws):
    """No agent tool fans replications out to workers today, so this drives the engine's worker
    path (`api.run_model(workers=2)`) in the agent's consent window. Each worker re-reads the
    store: a pool started while the code was trusted refuses it once it is revoked."""
    fp = trust(ALTERNATE)
    layout = model()
    with T.python_consent_dropped():
        serial = api.run_model(layout, replications=2, seed=0, workers=1)
        with api.new_pool(2) as pool:
            parallel = api.run_model(layout, replications=2, seed=0, workers=2, pool=pool)
            assert routed(parallel, 0) == routed(serial, 0)
            assert routed(parallel, 1) == routed(serial, 1)
            trustwrite.revoke(itemtrust.short(fp))
            with pytest.raises(ItemCodeUntrusted):
                api.run_model(layout, replications=2, seed=0, workers=2, pool=pool)
