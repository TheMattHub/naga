"""`naga skill`: where the simulation-study guide (an Agent Skills folder) is, or a copy of it."""
from __future__ import annotations

import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path

from nagasim import study


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="naga skill",
        description="Show where the simulation-study guide is, or copy it so an assistant "
                    "that reads Agent Skills (a skills folder) can use it.")
    parser.add_argument("--copy", metavar="DIR",
                        help="copy the folder to DIR/simulation-study (writes only there)")
    parser.add_argument("--force", action="store_true",
                        help="replace DIR/simulation-study if it exists")
    args = parser.parse_args(argv)
    if not args.copy:
        sys.stdout.write(f"{study.skill_dir()}\n"
                         "Copy it into an assistant's skills folder with: naga skill --copy DIR\n")
        return 0
    dest = Path(args.copy) / "simulation-study"
    if dest.exists() and not args.force:
        sys.stderr.write(f"naga skill: {dest} exists; use --force to replace it\n")
        return 1
    if dest.exists():
        shutil.rmtree(dest)
    with resources.as_file(study.skill_dir()) as src:
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    sys.stdout.write(f"{dest}\n")
    return 0
