"""The animated GIF of a study run (nagasim/studyanim.py, tool study_animation)."""
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from nagasim import agent_tools as T
import nagasim.agent_tools.study_anim  # noqa: F401  (registers study_animation)

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
GOAL = {"id": "g1", "text": "wait under 5", "kpi": "Avg Throughput Time", "high": 5}


@pytest.fixture
def study(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    ws = T.use_workspace(T.Workspace(tmp_path))
    model = json.loads((tmp_path / "single_server.json").read_text(encoding="utf-8"))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    T.call("study_update", study=sid, changes={"plan_approved": True, "goals": [GOAL]})
    assert "error" not in T.call("study_step", study=sid, layout=model, note="first")
    return ws, sid


@pytest.mark.skipif(importlib.util.find_spec("matplotlib") is None,
                    reason="the animation needs the optional nagasim[plot] extra")
@pytest.mark.slow
def test_gif_has_magic_frames_and_size_cap(study):
    ws, sid = study
    r = T.call("study_animation", study=sid)
    assert "error" not in r, r
    path = ws.root / r["path"]
    data = path.read_bytes()
    assert data[:6] in (b"GIF87a", b"GIF89a")
    assert len(data) <= 1_000_000
    from PIL import Image
    with Image.open(path) as im:
        assert im.n_frames == 40 and im.width <= 900
    assert r["frames"] == 40 and r["path"].endswith("versions/v001/animation.gif")


@pytest.mark.slow
def test_unknown_scenario_is_a_coded_error(study):
    _, sid = study
    assert T.call("study_animation", study=sid, scenario="nope")["error"]["code"] == "E_STUDY_BAD_VALUE"


@pytest.mark.slow
def test_without_matplotlib_the_error_is_coded(study, monkeypatch):
    _, sid = study
    monkeypatch.setitem(sys.modules, "matplotlib.figure", None)
    r = T.call("study_animation", study=sid)
    assert r["error"]["code"] == "E_STUDY_NEEDS_EXTRA" and "nagasim[plot]" in r["error"]["hint"]
