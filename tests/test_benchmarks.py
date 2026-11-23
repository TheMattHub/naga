"""The comparison with SimPy, salabim and Ciw (`benchmarks/`): the theory it checks against,
every implementation still agreeing with it, and the published table being the committed
results file's. The timings themselves differ per machine and are never regenerated here."""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"benchmarks_{name}", BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_theory_values_are_the_textbook_ones():
    models = _load("models")
    theory = {name: models.mean_time_in_system(stations) for name, (_title, stations) in models.MODELS.items()}
    # M/M/1: 1/(mu - lambda). The line: the sum of its three M/M/1 stations. M/M/3 at load
    # 2.5: Erlang C 0.7022, so 0.7022/(1.2 - 1) + 2.5. The job shop: tests/test_case_study.py.
    assert theory == pytest.approx({"mm1": 4.0, "mm3": 6.0112, "tandem": 7.0, "jackson": 8.376}, abs=5e-4)


def test_the_published_table_is_the_results_files():
    results = json.loads((BENCH / "results.json").read_text(encoding="utf-8"))
    assert (BENCH / "results.md").read_text(encoding="utf-8") == _load("compare").render(results)
    assert not results["quick"]
    assert all(row["seconds"] for entry in results["models"].values() for row in entry["rows"].values())


def test_no_engine_module_imports_a_benchmarked_library():
    found = [str(path.relative_to(ROOT)) for path in (ROOT / "nagasim").rglob("*.py")
             if re.search(r"^\s*(?:import|from) (?:salabim|ciw)\b", path.read_text(encoding="utf-8"), re.M)]
    assert not found, found


@pytest.mark.slow
def test_every_implementation_runs_and_agrees_with_theory():
    """`--quick` exits 1 when an implementation's interval misses the theory value."""
    for library in ("salabim", "greenlet", "ciw"):      # benchmarks/requirements.txt; not on CI
        pytest.importorskip(library)
    done = subprocess.run([sys.executable, str(BENCH / "compare.py"), "--quick"], capture_output=True,
                          text=True, timeout=300, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count("| Naga, logs and sampler off |") == 4


@pytest.mark.slow       # two processes that import the engine
def test_the_comparison_never_loads_the_users_trusted_plugins(tmp_path, monkeypatch):
    """Speed review step 1: `compare.py` imports `tools/_isolate.py` before the engine, so a timed
    Naga run loads the shipped registries only, not a plugin folder the machine's user trusted."""
    from nagasim.settings import trusted
    profile, folder, marker = tmp_path / "profile", tmp_path / "plug", tmp_path / "ran.txt"
    monkeypatch.setenv("NAGASIM_USER_DIR", str(profile / "Naga"))
    folder.mkdir()
    (folder / "mark.py").write_text(f"open({str(marker)!r}, 'a').write('x')\nNAGASIM_BLOCKS = []\n",
                                    encoding="utf-8")
    trusted.add("plugin_folder", folder)
    env = {k: v for k, v in os.environ.items() if k not in ("NAGASIM_USER_DIR", "NAGASIM_PLUGIN_PATH")}
    env.update(APPDATA=str(profile), XDG_DATA_HOME=str(profile), PYTHONPATH=f"{BENCH}{os.pathsep}{ROOT}")
    run = ("import runpy, sys; runpy.run_path(sys.argv[1]); import with_naga; "
           "with_naga.run([(1, 1.25, {})], 20, 2, 1, lean=True)")
    plain = "from nagasim import plugins; plugins.load_plugins()"
    assert subprocess.run([sys.executable, "-c", plain], env=env, timeout=120).returncode == 0
    assert marker.exists()                  # the profile is live: an engine run would load it
    marker.unlink()
    done = subprocess.run([sys.executable, "-c", run, str(BENCH / "compare.py")], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert not marker.exists()
