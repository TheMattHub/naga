"""naga study rerun FOLDER (nagasim/studyrerun.py)."""
import json
import shutil
import sys
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


@pytest.mark.slow
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


# --- reproduce steps (P9, D1170) ---------------------------------------------------------------

WRITE = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"


def _package(tmp_path, steps):
    """A tiny package: no study.json, one script that writes argv[2] into the file argv[1]."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "write.py").write_text(WRITE, encoding="utf-8")
    (tmp_path / "scripts" / "fail.py").write_text("raise SystemExit(3)", encoding="utf-8")
    (tmp_path / "reproduce.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    return tmp_path


def _write(name, out, text, **extra):
    return {"name": name, "script": "scripts/write.py", "args": [out, text], "outputs": [out], **extra}


def test_steps_regenerate_identically(tmp_path, capsys):
    pkg = _package(tmp_path, [_write("a", "out/a.txt", "1"), _write("b", "out/b.txt", "2")])
    (pkg / "out").mkdir()
    (pkg / "out" / "a.txt").write_text("1", encoding="utf-8")       # the committed outputs
    (pkg / "out" / "b.txt").write_text("2", encoding="utf-8")
    assert studyrerun.main(["rerun", str(pkg), "--steps"]) == 0
    out = capsys.readouterr().out
    assert "== a: python scripts/write.py out/a.txt 1" in out
    assert "a: ok" in out and "b: ok" in out and "0 changed, 0 new, 0 gone, 1 identical" in out


def test_changed_output_is_reported(tmp_path, capsys):
    pkg = _package(tmp_path, [_write("a", "out/a.txt", "new")])
    (pkg / "out").mkdir()
    (pkg / "out" / "a.txt").write_text("old", encoding="utf-8")
    assert studyrerun.main(["rerun", str(pkg), "--steps"]) == 1
    assert "changed: out/a.txt" in capsys.readouterr().out
    res = studyrerun.run_steps(pkg, studyrerun.load_manifest(pkg), capture=True)
    assert res[0]["status"] == "ok" and res[0]["same"] == 1      # the second run finds its own file


def test_failing_step_stops_the_run(tmp_path, capsys):
    pkg = _package(tmp_path, [_write("a", "a.txt", "1"),
                              {"name": "boom", "script": "scripts/fail.py"},
                              _write("c", "c.txt", "3")])
    res = studyrerun.run_steps(pkg, studyrerun.load_manifest(pkg), capture=True)
    assert [r["status"] for r in res] == ["ok", "failed", "not_run"] and res[1]["code"] == 3
    assert res[0]["added"] == ["a.txt"] and not (pkg / "c.txt").exists()
    assert studyrerun.main(["rerun", str(pkg), "--steps"]) == 1
    assert "c: not run" in capsys.readouterr().out


def test_optional_step_and_time_limit(tmp_path):
    pkg = _package(tmp_path, [{"name": "later", "script": "scripts/not_yet.py", "optional": True},
                              {"name": "slow", "module": "time", "timeout": 0.01, "optional": True},
                              {"name": "sleep", "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                               "timeout": 0.5},
                              _write("d", "d.txt", "4")])
    res = studyrerun.run_steps(pkg, studyrerun.load_manifest(pkg), capture=True)
    assert [r["status"] for r in res][::2] == ["skipped", "timeout"] and res[3]["status"] == "not_run"
    assert not studyrerun.steps_ok(res)


def test_manifest_is_checked_and_never_runs_anything(tmp_path):
    pkg = _package(tmp_path, [{"name": "x", "command": "rm -rf /"}])
    with pytest.raises(studyrerun.study_mod.StudyError, match="list of strings"):
        studyrerun.load_manifest(pkg)
    (pkg / "reproduce.json").write_text(json.dumps({"steps": [{"script": "../outside.py"}]}), encoding="utf-8")
    m = studyrerun.load_manifest(pkg)                        # loading only reads
    res = studyrerun.run_steps(pkg, m, capture=True)
    assert res[0]["status"] == "failed" and "outside" in res[0]["tail"]
    assert studyrerun.main(["rerun", str(tmp_path / "nothing")]) == 2
