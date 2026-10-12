"""Shared CLI plumbing for nagasim's headless `main()` entry points
(ARCH-REVIEW-PLAN B4): the utf-8 stdout + logging setup they all repeated.
Each module still declares its own argparse options, so a reader never has to
cross-reference this file to know what a flag does there.
"""
from __future__ import annotations

import logging
import sys

__all__ = ["setup_cli"]


def setup_cli(level=logging.WARNING) -> None:
    """utf-8 stdout + basic logging for a headless `main()`.

    A plain Windows console defaults to cp1252 for anything a dependency
    prints (e.g. a Unicode path); tests force PYTHONIOENCODING=utf-8
    (conftest.py), but a real user -- especially the frozen build -- may not
    have that set. `level=None` skips the logging setup, for a module that
    never configured it.
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if level is not None:
        logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
