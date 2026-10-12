"""Where Naga keeps a user's own files (ARCHITECTURE 10.1, "user config
discovery"; DEVLOG #107).

ONE resolver for every caller -- `plugins.py` (a frozen build's plugin drop
folder), `libraries.py` (installed asset libraries) and
`core/logic_authoring.py` (a frozen build's editable `custom_logic.py`) used to
each resolve their own, differently and wrongly off Windows: `%APPDATA%` was
trusted on POSIX, where it is never set (so `libraries.py` fell back to a
second convention and `plugins.py` gave a frozen Linux build no user folder at
all), and nothing could redirect any of them, so a test on POSIX wrote into the
real `$HOME`.

Stdlib only, no Qt: engine modules import this. The Qt UI's own QSettings and
`QStandardPaths` locations (window state, recent files, saved templates) are a
separate, Qt-owned store and are not routed through here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ENV_VAR", "user_data_dir"]

#: Overrides the directory on every OS. `tests/conftest.py` sets it for the
#: whole session (DEVLOG #273), inherited by any subprocess a test starts, so
#: no test run ever touches the real profile; a team can point it at a shared
#: folder the same way.
ENV_VAR = "NAGASIM_USER_DIR"

_NEW_NAME = "Naga"


def _base_dir(name: str) -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else Path.home()) / name
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / name


def user_data_dir() -> Path:
    """The per-user directory for the product/Naga's own files. Not created
    here -- a caller that writes makes it, a caller that reads tolerates its
    absence.

    `%APPDATA%/the product` on Windows, `$XDG_DATA_HOME/the product` (default
    `~/.local/share/the product`) elsewhere. `NAGASIM_USER_DIR` wins everywhere.
    An empty value counts as unset, so a blank variable cannot silently
    redirect writes to the current working directory.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return _base_dir(_NEW_NAME)
