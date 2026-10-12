"""The models that ship in the package, by name.

    naga examples                 -> the names, with their titles
    naga run single_server        -> a command that takes a model path also takes a name

A name is the file's stem under `nagasim/models/`: letters, digits, `_` and `-` only, so
nothing that could be a path is ever looked up there. A path that exists always wins over a
bundled model of the same name.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

__all__ = ["DIR", "names", "path", "model_path", "main"]

DIR = Path(__file__).resolve().parent / "models"
_NAME = re.compile(r"[A-Za-z0-9_-]+")


def names() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.json"))


def path(name: str) -> Path:
    """The bundled model called *name*; ValueError names what there is."""
    found = DIR / f"{name}.json"
    if not _NAME.fullmatch(name or "") or not found.is_file():
        raise ValueError(f"no bundled example '{name}'; available: {', '.join(names())}")
    return found


def model_path(value: str) -> str:
    """What a command's `model` argument means: the path given, or, ONLY when nothing is at
    that path and it is a plain name, the bundled model of that name."""
    if not Path(value).exists() and _NAME.fullmatch(value) and (DIR / f"{value}.json").is_file():
        return str(DIR / f"{value}.json")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="naga examples",
        description="List the models that ship in the package. A command that takes a model "
                    "path also takes one of these names: naga run single_server.")
    parser.add_argument("--path", action="store_true", help="print the folder they are in instead")
    args = parser.parse_args(argv)
    if args.path:
        sys.stdout.write(f"{DIR}\n")
        return 0
    width = max(len(name) for name in names())
    for name in names():
        info = json.loads((DIR / f"{name}.json").read_text(encoding="utf-8")).get("info") or {}
        sys.stdout.write(f"{name:<{width}}  {info.get('title', '')}\n")
    return 0
