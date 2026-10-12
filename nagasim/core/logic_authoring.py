"""Where the LOGIC block's classes come from: `custom_logic.py`, TRUSTED CODE
the user edits with their own editor. The app only LISTS the classes it
defines (`list_logic_classes`) and shows the path; it never writes Python or
hands the file to the shell (DEVLOG #55/#56, closed in the hardening pass:
the "New..." stub writer and "Open file" were the last runtime code paths a
model could lead to). No Qt import here.

M5 (frozen build): `custom_logic.py` is user-editable, but a PyInstaller
bundle's package directory is read-only. `resolve_default_path()` and
`load_custom_logic_module()` redirect it to a user-writable copy when
`sys.frozen` is set; unfrozen (normal install/checkout) behaviour is
unchanged.
"""
from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
from pathlib import Path

from nagasim.settings.userpaths import user_data_dir

__all__ = ["DEFAULT_PATH", "resolve_default_path", "load_custom_logic_module",
           "list_logic_classes"]


def _bundled_path() -> Path:
    """Where custom_logic.py ships: next to this module in a normal install or
    checkout, or under sys._MEIPASS in a frozen build (packaging/product.spec
    lists it as a data file there -- PyInstaller does not expose bundled .py
    modules as real files, only data files). Keyed off _MEIPASS rather than
    sys.frozen so a test can simulate "frozen" (redirect DEFAULT_PATH) without
    also faking a full PyInstaller extraction directory."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "nagasim" / "core" / "custom_logic.py"
    return Path(__file__).resolve().with_name("custom_logic.py")


def resolve_default_path() -> Path:
    """Where the LOGIC block's custom_logic.py lives. Not frozen: the bundled
    copy (editable, version-controlled) -- unchanged behaviour. Frozen: a
    user-writable copy under `settings.userpaths.user_data_dir()`, seeded from
    the bundled default on first use."""
    if not getattr(sys, "frozen", False):
        return _bundled_path()
    target = user_data_dir() / "custom_logic.py"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_bundled_path(), target)
    return target


DEFAULT_PATH = resolve_default_path()


def load_custom_logic_module():
    """Import the live module the LOGIC block instantiates classes from
    (`nagasim.core.simple_processes.Logic`). Not frozen: the normal package
    module, sys.modules-cached like any import -- `ui/properties.py`'s
    `importlib.reload()` after editing keeps working exactly as before.
    Frozen: loaded by file path from DEFAULT_PATH (the user-writable copy)
    and registered under the same module name, so callers do not need to
    know which path it came from."""
    module_name = "nagasim.core.custom_logic"
    if not getattr(sys, "frozen", False):
        return importlib.import_module(module_name)
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, DEFAULT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

def _load_module(path: Path):
    """Load *path* as an isolated module (never sys.modules): callers that need
    the live nagasim.core.custom_logic module reload that one themselves."""
    spec = importlib.util.spec_from_file_location(f"_logic_authoring_probe_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def list_logic_classes(path: Path | str | None = None) -> list[str]:
    """Names of the plain classes defined in *path* (default: custom_logic.py)
    that look like a Logic block class: defined there (not imported), with an
    `execute` method."""
    module = _load_module(Path(path) if path is not None else DEFAULT_PATH)
    return sorted(
        cls_name for cls_name, cls in inspect.getmembers(module, inspect.isclass)
        if cls.__module__ == module.__name__ and hasattr(cls, "execute")
    )
