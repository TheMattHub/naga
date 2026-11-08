"""Shared CLI plumbing for nagasim's headless `main()` entry points
(ARCH-REVIEW-PLAN B4): the utf-8 stdout + logging setup they all repeated.
Each module still declares its own argparse options, so a reader never has to
cross-reference this file to know what a flag does there.
"""
from __future__ import annotations

import logging
import sys

from nagasim.examples import model_path

__all__ = ["setup_cli", "add_model_argument", "add_logic_argument", "logic_import_error",
           "add_controllers_argument", "load_controllers", "CONTROLLERS_NAME"]


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


def add_model_argument(parser, help="path to the model JSON, or a bundled model's name (naga examples)",
                       **kwargs) -> None:
    """The `model` argument of a command: a path, or the name of a bundled model."""
    parser.add_argument("model", type=model_path, help=help, **kwargs)


def add_logic_argument(parser) -> None:
    """`--logic MODULE` for a study command (D914, D1062). The caller's flag only:
    no model key or agent tool can name it."""
    parser.add_argument("--logic", default=None, metavar="MODULE",
                        help="take LOGIC block classes from this importable module first "
                             "(a dotted name on PYTHONPATH), then from nagasim.core.custom_logic")


def logic_import_error(name) -> str | None:
    """None when *name* is None or imports; else the message to print (exit code 2),
    so a bad `--logic` fails at once and not on the first run."""
    if name is None:
        return None
    from nagasim.core import logic_authoring

    try:
        logic_authoring.resolve_logic(name)
    except ImportError as exc:
        return f"--logic: {exc}"
    return None


# The name `--controllers MODULE` takes when it names no object (D1121): a module-level dict.
CONTROLLERS_NAME = "CONTROLLERS"


def add_controllers_argument(parser) -> None:
    """`--controllers MODULE[:NAME]` (D1120). The caller's flag only, like `--logic`: no model
    key, storyboard key or agent tool can name it."""
    parser.add_argument("--controllers", default=None, metavar="MODULE[:NAME]",
                        help="drive the Policy-mode DECIDE blocks with the dict {decision point: "
                             "controller} NAME (default CONTROLLERS) of this importable module "
                             "(a dotted name on PYTHONPATH)")


def load_controllers(spec: str | None) -> dict | None:
    """The `{policy name: controller}` dict *spec* (`MODULE[:NAME]`) names, or None for None.
    A module that does not import, or a NAME that is missing or not a dict with string keys,
    raises ValueError with the message to print after `--controllers: ` (exit code 2)."""
    if spec is None:
        return None
    import importlib

    module_name, _, name = spec.partition(":")
    name = name or CONTROLLERS_NAME
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"module '{module_name}' does not import: {exc}") from exc
    found = getattr(module, name, None)
    if found is None:
        raise ValueError(f"module '{module_name}' has no '{name}'")
    if not isinstance(found, dict) or not all(isinstance(k, str) for k in found):
        raise ValueError(f"'{module_name}:{name}' must be a dict {{decision point name: controller}}, "
                         f"not {type(found).__name__}")
    return found
