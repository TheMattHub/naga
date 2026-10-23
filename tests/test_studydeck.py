"""naga study deck FOLDER (nagasim/studydeck.py, D717): the slides printed to PDF by a local browser."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import studydeck, studyrerun

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"


@pytest.fixture
def folder(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    layout = json.loads((tmp_path / "single_server.json").read_text(encoding="utf-8"))
    assert T.call("study_step", study=sid, layout=layout, note="first model")["kind"] == "version"
    return tmp_path / sid


def test_the_browser_prints_the_slides_page_offline_in_a_throwaway_profile(folder, monkeypatch, capsys):
    seen = {}

    def fake(args, **kwargs):
        seen["args"], seen["timeout"] = args, kwargs.get("timeout")
        out = next(a for a in args if a.startswith("--print-to-pdf=")).split("=", 1)[1]
        Path(out).write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(studydeck.subprocess, "run", fake)
    assert studyrerun.main(["deck", str(folder), "--browser", "C:/my/chrome.exe"]) == 0
    pdf = folder / "package" / "slides.pdf"
    assert capsys.readouterr().out.strip() == str(pdf) and pdf.read_bytes().startswith(b"%PDF-")
    args = seen["args"]
    assert Path(args[0]) == Path("C:/my/chrome.exe") and "--headless" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in args and seen["timeout"] == studydeck.TIMEOUT_S
    assert args[-1] == (folder / "package" / "slides.html").resolve().as_uri()
    assert any(a.startswith("--user-data-dir=") for a in args)


def test_no_browser_or_no_pdf_says_what_to_do(folder, monkeypatch, capsys):
    monkeypatch.setattr(studydeck, "find_browser", lambda: None)
    assert studyrerun.main(["deck", str(folder)]) == 2
    err = capsys.readouterr().err
    assert "no Chrome, Edge or Chromium found" in err and "slides.html" in err
    monkeypatch.setattr(studydeck.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1))
    assert studyrerun.main(["deck", str(folder), "--browser", "x"]) == 2
    assert "the browser wrote no PDF" in capsys.readouterr().err


def test_a_folder_that_is_not_a_study_is_refused(tmp_path, capsys):
    assert studyrerun.main(["deck", str(tmp_path)]) == 2
    assert "naga study deck:" in capsys.readouterr().err


@pytest.mark.slow
@pytest.mark.skipif(studydeck.find_browser() is None, reason="no Chrome, Edge or Chromium installed")
def test_a_real_browser_prints_one_page_per_slide(folder):
    import re
    from nagasim import study
    data = studydeck.deck(folder, study.load(folder)).read_bytes()
    html = (folder / "package" / "slides.html").read_text(encoding="utf-8")
    assert data.startswith(b"%PDF-")
    assert len(re.findall(rb"/Type\s*/Page(?!s)", data)) == html.count('<section class="slide">') > 5


def test_a_browser_planted_in_the_current_folder_is_never_found(tmp_path, monkeypatch):
    """CWE-427: on Windows `which` looks in the current folder first; a chrome.exe planted in a
    study folder must not be the one that runs."""
    for name in ("chrome", "chrome.exe", "chrome.bat"):
        (tmp_path / name).write_text("planted")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(studydeck, "_PLACES", ())
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(tmp_path), ""]))
    monkeypatch.setattr(studydeck, "_NAMES", ("chrome",))
    assert studydeck.find_browser() is None


def test_858_a_browser_that_hangs_is_a_message_not_a_traceback(folder, monkeypatch, capsys):
    """#858: at the timeout the browser's helpers can hold the profile folder; its cleanup
    raised PermissionError over the DeckError. Cleanup errors are now ignored."""
    def hangs(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
    monkeypatch.setattr(studydeck.subprocess, "run", hangs)
    made = []
    real = studydeck.tempfile.TemporaryDirectory
    monkeypatch.setattr(studydeck.tempfile, "TemporaryDirectory", lambda **k: made.append(k) or real(**k))
    assert studyrerun.main(["deck", str(folder), "--browser", "x"]) == 2
    assert "did not finish within" in capsys.readouterr().err
    assert made[0]["ignore_cleanup_errors"] is True
