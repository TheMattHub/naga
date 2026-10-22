"""Law-check warnings in the Problems panel after a run (D684)."""
import json
from pathlib import Path

import pytest

import nagasim
from nagasim import api

MODEL = Path(nagasim.__file__).parent / "models" / "single_server.json"


def _texts(window):
    return [window.problems.item(i).text() for i in range(window.problems.count())]


def _run_overloaded(window, tmp_path):
    layout = json.loads(MODEL.read_text(encoding="utf-8"))
    layout["simulation"].update(replications=3, time_limit=2000)
    layout["blocks"][0]["params"]["dist_params"] = {"rate": 3.0}
    path = tmp_path / "overloaded.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    assert window.open_path(str(path))
    layout = window.document.to_layout()
    window._run_layout = layout
    res = api.run_model(layout, seed=0)
    window._on_run_finished(res, None, switch=False)


@pytest.mark.slow
def test_law_warnings_show_after_a_run_and_clear_on_edit(window, tmp_path):
    _run_overloaded(window, tmp_path)
    texts = _texts(window)
    assert any(t.startswith("warning: offered load") for t in texts)
    assert any("censoring" in t for t in texts)
    assert not window.problems.has_errors()
    assert not any(t.startswith("warning: offered load: offered load") for t in texts)
    assert window.problems_button.text() == f"{len(texts)} problems"      # the status bar says so
    window.document.set_simulation(seed=7)
    assert not any("offered load" in t for t in _texts(window))


def test_a_failing_law_check_shows_nothing(window, monkeypatch):
    from nagasim import laws
    monkeypatch.setattr(laws, "checks", lambda *a: 1 / 0)
    window._show_law_warnings({}, None)
    assert window.problems._run_warnings == []
