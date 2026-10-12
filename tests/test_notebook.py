"""PHASE5 O6: the notebook API. Two things are covered here -- `Results` as a
unit (`nagasim.results`), and `docs/notebooks/quickstart.ipynb` actually
running end to end, without jupyter/nbformat (stdlib json + exec, per
PHASE5_PLAN O6: "nbformat-free stdlib, or skipped without jupyter")."""
import json
import time
from pathlib import Path

import pandas as pd
import pytest

from nagasim import api
from nagasim.results import Results

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "docs" / "notebooks" / "quickstart.ipynb"
MODEL = ROOT / "nagasim" / "models" / "single_server.json"


@pytest.fixture(scope="module")
def results() -> Results:
    return api.run_model(str(MODEL), replications=3, run_length=200, warmup=20)


def test_run_model_returns_a_results_dict_subclass(results):
    assert isinstance(results, Results)
    assert isinstance(results, dict)
    assert results["system"] is results.frame("system")


def test_kpis_is_a_tidy_frame_with_ci95(results):
    df = results.kpis()
    assert list(df.columns) == ["metric", "mean", "std", "ci95"]
    assert "system/Avg Queue Length" in set(df["metric"])
    assert not df.empty


def test_repr_html_names_the_kpis_and_replications(results):
    html = results._repr_html_()
    assert "3 replications" in html
    assert "system/Avg Queue Length" in html


def test_plot_returns_an_axes_or_raises_the_install_hint(results):
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    ax = results.plot("system/Avg Queue Length")
    assert ax.__class__.__name__ == "Axes"


def test_plot_raises_key_error_for_an_unknown_kpi(results):
    pytest.importorskip("matplotlib")
    with pytest.raises(KeyError):
        results.plot("system/Not A Real KPI")


def test_to_excel_delegates_to_the_xlsx_exporter(results, tmp_path):
    out = tmp_path / "out.xlsx"
    results.to_excel(out)
    assert out.is_file()


def test_run_model_results_are_picklable():
    """`Results` must be built from plain, picklable per-replication dicts
    (PHASE5 O6 checklist). The pool path returns the same frames as the
    sequential one (`test_experiments.test_parallel_equals_sequential_and_
    reports_every_replication`), so a sequential run is enough here."""
    import pickle  # round-trips our own in-process object, never untrusted data

    r = api.run_model(str(MODEL), replications=4, run_length=200, warmup=20)
    assert isinstance(r, Results)
    r2 = pickle.loads(pickle.dumps(r))
    assert isinstance(r2, Results) and set(r2) == set(r)


# ---------------------------------------------------------------- the notebook
def _code_cells(notebook: dict):
    for cell in notebook["cells"]:
        if cell["cell_type"] != "code":
            continue
        if "skip-test" in (cell.get("metadata", {}).get("tags") or []):
            continue
        yield "".join(cell["source"])


def test_quickstart_notebook_runs_end_to_end(tmp_path, monkeypatch):
    """Executes every non-skipped code cell in order, in a fresh namespace and
    a temp cwd -- so a cell that (wrongly) assumed the repo root as its
    working directory would fail here even though `pytest` itself runs from
    the repo root."""
    pytest.importorskip("matplotlib")  # the plot cell is not tagged skip-test: it IS installed (PHASE5_PLAN O6)
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)

    namespace: dict = {}
    t0 = time.perf_counter()
    for source in _code_cells(notebook):
        exec(compile(source, str(NOTEBOOK), "exec"), namespace)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"quickstart notebook took {elapsed:.2f}s, budget is 5s"

    assert isinstance(namespace["results"], Results)
    kpis = namespace["results"].kpis()
    assert isinstance(kpis, pd.DataFrame) and not kpis.empty

    comparison = namespace["comparison"]
    assert {"Faster server", "Baseline"} <= set(comparison["Scenario"])

    opt = namespace["opt"]
    assert "service_rate" in opt.best

    doc_results = namespace["doc_results"]
    assert isinstance(doc_results, Results)
    sys_df = doc_results["system"]
    completed = sys_df.loc[sys_df["Metric"] == "Total Entities Completed", "Value"]
    assert completed.sum() > 0
