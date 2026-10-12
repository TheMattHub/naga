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
