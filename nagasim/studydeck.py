"""`naga study deck FOLDER`: the study's slides as a PDF, printed by the Chrome, Edge or Chromium on
this machine (D717).

The deck is `package/slides.html` (studywriteup.slides), which already prints one 16:9 slide per
page; a headless browser prints it to `package/slides.pdf`, so the PDF is the page and there is
no second renderer. Security (netguard-style review, D717): only a human starts this (a CLI command,
no agent tool), the browser is found on fixed paths or named by that human, the page's CSP loads
nothing (`default-src 'none'`, data images only) and the browser is also told to resolve no host,
and it runs in a throwaway profile under a hard timeout.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import study as study_mod, studywriteup

TIMEOUT_S = 120

# Command names first, then the usual install places (Windows, macOS).
_NAMES = ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge",
          "microsoft-edge")
_PLACES = (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
           r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
           "/Applications/Chromium.app/Contents/MacOS/Chromium")


class DeckError(RuntimeError):
    """A deck that could not be printed, with what to do instead."""


def find_browser() -> Path | None:
    """The first Chrome, Edge or Chromium found, or None: the usual install places first, then PATH
    without the current folder (on Windows `which` looks there first, so a browser planted in a
    study folder would run)."""
    placed = next((Path(p) for p in _PLACES if Path(p).is_file()), None)
    if placed:
        return placed
    here = Path.cwd().resolve()
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d.strip() not in ("", ".") and Path(d).resolve() != here]
    for name in _NAMES:
        found = shutil.which(name, path=os.pathsep.join(dirs))
        if found and here not in Path(found).resolve().parents:
            return Path(found)
    return None


def print_pdf(html: Path, pdf: Path, browser: Path) -> None:
    """Print *html* to *pdf* with *browser* (headless). Raises DeckError when no PDF comes out."""
    pdf.unlink(missing_ok=True)
    # A browser killed at the timeout can leave helpers holding the profile (#858): its
    # folder is left for the system to clear rather than raise over the DeckError.
    with tempfile.TemporaryDirectory(prefix="naga-deck-", ignore_cleanup_errors=True) as profile:
        args = [str(browser), "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-extensions", "--host-resolver-rules=MAP * ~NOTFOUND", f"--user-data-dir={profile}",
                "--no-pdf-header-footer", f"--print-to-pdf={pdf}", html.resolve().as_uri()]
        try:
            subprocess.run(args, capture_output=True, timeout=TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise DeckError(f"the browser did not finish within {TIMEOUT_S} s") from None
        except OSError as exc:
            raise DeckError(f"the browser could not start ({exc})") from None
    head = b""
    if pdf.is_file():
        with pdf.open("rb") as file:
            head = file.read(5)
    if head != b"%PDF-":
        raise DeckError("the browser wrote no PDF")


def deck(folder: Path, study: dict, browser: Path | None = None) -> Path:
    """Write the slides, print them, and return the PDF's path (`package/slides.pdf`)."""
    browser = browser or find_browser()
    if browser is None:
        raise DeckError("no Chrome, Edge or Chromium found; pass --browser PATH")
    html = studywriteup.slides(Path(folder), study)
    pdf = html.with_suffix(".pdf")
    print_pdf(html, pdf, Path(browser))
    return pdf


def run(folder: Path, browser: str | None) -> int:
    """The `naga study deck` command: 0 and the PDF's path, or 2 and why not."""
    try:
        pdf = deck(folder, study_mod.load(folder), Path(browser) if browser else None)
    except study_mod.StudyError as exc:
        sys.stderr.write(f"naga study deck: {exc}\n")
        return 2
    except DeckError as exc:
        sys.stderr.write(f"naga study deck: {exc}. The slides are in package{os.sep}slides.html; "
                         "open them in a browser and print to PDF.\n")
        return 2
    sys.stdout.write(f"{pdf}\n")
    return 0
