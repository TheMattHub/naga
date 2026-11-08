"""The code a user allows to run: plugin folders and Python policy files (D1115).

One JSON file per user, `<user data dir>/trusted_code.json`
(`settings/userpaths.user_data_dir()`), a list of entries:

    {"kind": "plugin_folder" | "policy", "path": "<absolute>",
     "added_by": "user" | "assistant", "enabled": true}

`plugins.load_plugins` imports every ENABLED plugin folder listed here, besides
NAGASIM_PLUGIN_PATH; a dialog that runs a policy file asks `is_trusted` first.

SECURITY: permission is the caller's, never the file's (`netguard`). Only the
editor's Settings > Plugins page, its trust prompt and the PolicyPicker (a
person's click), or the documented agent tool `propose_plugin_folder` (which
can only add a DISABLED entry marked `added_by="assistant"`) write here.
Nothing in a model file is ever read into this store. Stdlib only, no Qt.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from nagasim.settings.userpaths import user_data_dir

log = logging.getLogger(__name__)

__all__ = ["KINDS", "TrustedEntry", "store_path", "entries", "find", "add", "remove", "set_enabled",
           "enabled_paths", "is_trusted"]

KINDS = ("plugin_folder", "policy")
_ADDED_BY = ("user", "assistant")
_FILE = "trusted_code.json"


@dataclass(frozen=True)
class TrustedEntry:
    kind: str
    path: str
    added_by: str = "user"
    enabled: bool = True


def store_path() -> Path:
    return user_data_dir() / _FILE


def _norm(path) -> str:
    # One spelling per place, so the same folder added twice is one entry.
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def entries() -> list[TrustedEntry]:
    """Every entry, in the order added. A missing or unreadable file is an empty store
    (logged): a broken store must not stop the application from starting."""
    path = store_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [TrustedEntry(str(e["kind"]), str(e["path"]), str(e.get("added_by", "user")),
                             bool(e.get("enabled", True)))
                for e in raw if e.get("kind") in KINDS]
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("trusted code store %s unreadable: %s", path, exc)
        return []


def _save(found: list[TrustedEntry]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in found], indent=2), encoding="utf-8")


def _find(found: list[TrustedEntry], kind: str, path) -> int:
    key = _norm(path)
    return next((i for i, e in enumerate(found) if e.kind == kind and _norm(e.path) == key), -1)


def find(kind: str, path) -> TrustedEntry | None:
    """The entry of *kind* at *path* (however the place is spelled), or None: the one lookup
    every caller uses (D1181)."""
    found = entries()
    index = _find(found, kind, path)
    return found[index] if index >= 0 else None


def add(kind: str, path, *, added_by: str = "user", enabled: bool = True) -> TrustedEntry:
    """Record *path* as trusted code of *kind*. A person adding a place already listed takes
    it over (`added_by` "user") and sets `enabled`; the assistant adding one already listed
    changes nothing (D1181: it can only propose, never switch a person's entry off)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    if added_by not in _ADDED_BY:
        raise ValueError(f"added_by must be one of {_ADDED_BY}, not {added_by!r}")
    found = entries()
    index = _find(found, kind, path)
    if index >= 0:
        old = found[index]
        if added_by != "user":
            return old
        entry = TrustedEntry(kind, old.path, "user", enabled)
        found[index] = entry
    else:
        entry = TrustedEntry(kind, str(Path(path).expanduser().resolve()), added_by, enabled)
        found.append(entry)
    _save(found)
    return entry


def remove(kind: str, path) -> bool:
    found = entries()
    index = _find(found, kind, path)
    if index < 0:
        return False
    del found[index]
    _save(found)
    return True


def set_enabled(kind: str, path, enabled: bool) -> None:
    found = entries()
    index = _find(found, kind, path)
    if index < 0:
        raise KeyError(f"{kind} {path} is not in the trusted code store")
    old = found[index]
    found[index] = TrustedEntry(old.kind, old.path, old.added_by, bool(enabled))
    _save(found)


def enabled_paths(kind: str) -> list[Path]:
    return [Path(e.path) for e in entries() if e.kind == kind and e.enabled]


def is_trusted(kind: str, path) -> bool:
    """Whether *path* is an enabled entry of *kind*: what a caller asks before running it."""
    entry = find(kind, path)
    return entry is not None and entry.enabled
