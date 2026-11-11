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


@pytest.mark.slow       # three processes that import the agent tools
def test_repo_tools_run_without_user_plugins(tmp_path, monkeypatch):
    """Review S8-15: every repo tool that imports the engine imports `tools/_isolate.py` first, so a tool
    that lists the registries (gen_docs_coverage writes a committed file from them) never sees a plugin the
    machine's user trusted; `naga --no-plugins` loads none, in the process and its workers."""
    import ast
    import re
    tools = REPO_ROOT / "tools"
    for path in sorted(tools.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "_isolate.py" or not re.search(r"^\s*(from|import) (nagasim|product)\b", text, re.M):
            continue
        body = [n for n in ast.parse(text).body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
                and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
        assert isinstance(body[0], ast.Try) and "_isolate" in ast.unparse(body[0]), path.name
    # a profile whose store trusts a folder that leaves a mark: what the laptop of a person with a plugin has
    profile = tmp_path / "profile"
    monkeypatch.setenv("NAGASIM_USER_DIR", str(profile / "Naga"))
    folder, marker = tmp_path / "plug", tmp_path / "ran.txt"
    folder.mkdir()
    (folder / "mark.py").write_text(f"open({str(marker)!r}, 'a').write('x')\nNAGASIM_BLOCKS = []\n", encoding="utf-8")
    trusted.add("plugin_folder", folder)
    env = {k: v for k, v in os.environ.items() if k not in ("NAGASIM_USER_DIR", "NAGASIM_PLUGIN_PATH")}
    env.update(APPDATA=str(profile), XDG_DATA_HOME=str(profile), PYTHONPATH=str(REPO_ROOT))

    def run(code):
        return subprocess.run([sys.executable, "-c", code], cwd=str(tools), env=env, capture_output=True,
                              text=True, timeout=120)
    assert run("import nagasim.agent_tools").returncode == 0 and marker.exists()      # the profile is live
    marker.unlink()
    assert run("import _isolate, nagasim.agent_tools").returncode == 0 and not marker.exists()
    proc = run("from nagasim import cli, plugins; import sys; sys.argv = ['naga']; "
               "cli.main(['--no-plugins', 'validate', 'nothing.json']); assert plugins.load_plugins().folders == {}")
    assert proc.returncode == 0 and not marker.exists(), proc.stderr


def test_the_store_round_trips_and_survives_a_cut_short_write(store, monkeypatch):
    """Review gate (P04, #1736): what is written reads back the same, digests included, and a write cut
    short half way leaves the store as it was."""
    folder = store / "plugs"
    folder.mkdir()
    (folder / "a.py").write_text("NAGASIM_BLOCKS = []\n", encoding="utf-8")
    trusted.add("plugin_folder", folder)
    trusted.add("policy", store / "p.py", added_by="assistant", enabled=False)
    found = trusted.entries()
    assert found[0].digest and not trusted.changed(found[0]) and found[1].digest is None
    real = os.fdopen

    class Cut:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def write(self, text):
            self.fh.write(text[: len(text) // 2])
            raise OSError("power cut")
    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Cut(real(*a, **k)))
    with pytest.raises(OSError, match="power cut"):
        trusted.remove("policy", store / "p.py")
    monkeypatch.undo()
    monkeypatch.setenv("NAGASIM_USER_DIR", str(store / "user"))
    assert trusted.entries() == found and len(list(trusted.store_path().parent.iterdir())) == 1
