"""The trusted code store (U1, D1115) and what reads it: plugin loading and a model's declared
plugin types (D1117). Headless; a plugin is loaded only in a subprocess (tests/test_plugins.py's
isolation rule)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nagasim import plugins
from nagasim.core import schema
from nagasim.core.block_registry import PLUGIN_BLOCK_TYPES
from nagasim.core.document import ModelDocument
from nagasim.settings import trusted

REPO_ROOT = Path(__file__).resolve().parents[1]
INSPECTION = REPO_ROOT / "examples" / "plugins" / "inspection_plugin"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path / "user"))
    monkeypatch.setenv("NAGASIM_PLUGIN_PATH", "")
    return tmp_path


def test_add_remove_enable_and_one_entry_per_place(store):
    folder = store / "plugs"
    folder.mkdir()
    assert trusted.entries() == []
    trusted.add("plugin_folder", folder, added_by="assistant", enabled=False)
    trusted.add("plugin_folder", str(folder) + os.sep)        # the same place, spelled otherwise
    [entry] = trusted.entries()
    assert entry.added_by == "user" and entry.enabled          # a person's add takes it over
    trusted.set_enabled("plugin_folder", folder, False)
    assert trusted.enabled_paths("plugin_folder") == []
    assert not trusted.is_trusted("plugin_folder", folder)
    trusted.add("policy", store / "p.py")
    assert trusted.is_trusted("policy", store / "p.py")
    assert not trusted.is_trusted("plugin_folder", store / "p.py")    # kinds never mix
    assert trusted.remove("plugin_folder", folder) and not trusted.remove("plugin_folder", folder)
    assert [e.kind for e in trusted.entries()] == ["policy"]
    with pytest.raises(ValueError):
        trusted.add("anything", folder)
    with pytest.raises(ValueError):
        trusted.add("policy", folder, added_by="model")


def test_an_assistant_add_never_changes_an_entry_a_person_made(store):
    """D1181: `propose_plugin_folder` adds switched off; on a listed place it must not switch
    the person's entry off, and every caller finds an entry through `find`."""
    folder = store / "plugs"
    folder.mkdir()
    trusted.add("plugin_folder", folder)
    trusted.add("plugin_folder", str(folder) + os.sep, added_by="assistant", enabled=False)
    [entry] = trusted.entries()
    assert entry.enabled and entry.added_by == "user"
    assert trusted.find("plugin_folder", folder) == entry and trusted.find("policy", folder) is None


def test_an_unreadable_store_is_empty_not_fatal(store):
    trusted.store_path().parent.mkdir(parents=True)
    trusted.store_path().write_text("{not json", encoding="utf-8")
    assert trusted.entries() == []


def test_search_paths_hold_the_enabled_trusted_folders_only(store):
    on, off = store / "on", store / "off"
    on.mkdir()
    off.mkdir()
    trusted.add("plugin_folder", on)
    trusted.add("plugin_folder", off, enabled=False)
    trusted.add("policy", store / "x.py")
    assert [p.resolve() for p in plugins.search_paths()] == [on.resolve()]


def test_a_trusted_folder_loads_its_blocks_and_reports_them(store):
    """In a subprocess: the inspection plugin trusted in the store (no environment variable)
    registers INSPECT, the report maps the folder to it, and a second load of the same
    folder imports nothing again."""
    trusted.add("plugin_folder", INSPECTION)
    script = (
        "from nagasim import plugins\n"
        "from nagasim.core.block_registry import PLUGIN_BLOCK_TYPES\n"
        "r = plugins.load_plugins()\n"
        "[(folder, types)] = r.folders.items()\n"
        "assert types == ['INSPECT'], r.folders\n"
        "assert 'INSPECT' in PLUGIN_BLOCK_TYPES\n"
        "n = len(r.loaded)\n"
        "assert plugins.load_plugin_folder(folder) is r and len(r.loaded) == n and not r.failed\n"
        "print('OK')\n")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.stdout.strip().endswith("OK"), proc.stdout + proc.stderr


def _plugin_model() -> dict:
    return {"schema_version": schema.CURRENT_VERSION,
            "blocks": [{"name": "Src", "type": "CREATE", "params": {}},
                       {"name": "Yard", "type": "MY_YARD"},
                       {"name": "Out", "type": "DISPOSE", "params": {}}],
            "links": [["Src", "Yard"], ["Yard", "Out"]]}


def test_a_saved_model_lists_its_plugin_types_and_names_them_when_missing(monkeypatch):
    doc = ModelDocument(schema.migrate(_plugin_model()))
    assert "requires_plugins" not in doc.to_layout().get("info", {})     # nothing loaded, nothing known
    monkeypatch.setattr(sys.modules["nagasim.core.document"], "PLUGIN_BLOCK_TYPES",
                        PLUGIN_BLOCK_TYPES | {"MY_YARD", "MY_UNUSED"})
    layout = doc.to_layout()
    assert layout["info"]["requires_plugins"] == ["MY_YARD"]           # used types only
    monkeypatch.undo()
    # Reopened where the plugin is not loaded: the type stays declared, and the problem says why.
    again = ModelDocument(json.loads(json.dumps(layout)))
    kept = again.to_layout()
    assert kept["info"]["requires_plugins"] == ["MY_YARD"]
    found = [p for p in schema.validate_model_structured(kept) if p.block == "Yard"]
    assert [p.code for p in found] == ["E_PLUGIN_NOT_LOADED"]
    assert found[0].message == "Yard: MY_YARD comes from a plugin that is not loaded"
    # A model that stops using the type drops the declaration.
    again.remove_blocks(["Yard"])
    assert "requires_plugins" not in again.to_layout().get("info", {})


def test_an_undeclared_unknown_type_is_still_the_bare_unknown_type():
    codes = {p.code for p in schema.validate_model_structured(_plugin_model()) if p.block == "Yard"}
    assert codes == {"E_UNKNOWN_BLOCK_TYPE"}
