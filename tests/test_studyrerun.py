"""naga study rerun FOLDER (nagasim/studyrerun.py)."""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import studyrerun

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"


@pytest.fixture
def folder(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    layout = json.loads((tmp_path / "single_server.json").read_text(encoding="utf-8"))
    r = T.call("study_step", study=sid, layout=layout, note="first model")
    assert r["kind"] == "version", r
    return tmp_path / sid


def test_clean_folder_reproduces(folder, capsys):
    assert studyrerun.main(["rerun", str(folder)]) == 0
    assert "v001: ok" in capsys.readouterr().out


def test_edited_model_is_reported(folder, capsys):
    path = folder / "versions" / "v001" / "model.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    m["blocks"][1]["params"]["dist_params"]["rate"] = 3.0
    path.write_text(json.dumps(m), encoding="utf-8")
    assert studyrerun.main(["rerun", str(folder)]) == 1
    assert "v001: MISMATCH" in capsys.readouterr().out
