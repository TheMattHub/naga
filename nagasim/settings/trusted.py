"""The code a user allows to run: plugin folders and Python policy files (D1115).

One JSON file per user, `<user data dir>/trusted_code.json`
(`settings/userpaths.user_data_dir()`), a list of entries:

    {"kind": "plugin_folder" | "policy", "path": "<absolute>",
     "added_by": "user" | "assistant", "enabled": true,
     "digest": [["<file>.py", "<sha256>"], ...] | null}

`plugins.load_plugins` imports every ENABLED plugin folder listed here, besides
NAGASIM_PLUGIN_PATH; a dialog that runs a policy file asks `is_trusted` first.

Trust is pinned by content (D1194, #1738; policy files #1756): the person's tick (`add` by the
user, `set_enabled(..., True)`) stores the `digest` of what runs, a plugin folder's top-level *.py
files (the ones `load_plugins` imports) or the policy file itself (`code_files`). Code whose bytes
differ from that digest, or an entry with no digest (an older store, an assistant's proposal),
counts as changed: none of it runs until the person enables it again, and the loaders run the
bytes they compared, read once. Nothing else ever writes a digest.

SECURITY: permission is the caller's, never the file's (`netguard`). Only the
editor's Settings > Plugins page, its trust prompt and the PolicyPicker (a
person's click), or the documented agent tool `propose_plugin_folder` (which
can only add a DISABLED entry marked `added_by="assistant"`) write here.
Nothing in a model file is ever read into this store. Stdlib only, no Qt.

The store fails closed (#1736): an entry is enabled only when its `enabled` is the JSON `true`; a
write goes to a temporary file that replaces the store whole; a store that exists but does not
parse reads as empty and is never written over (`StoreUnreadable`), so the person can repair it.
"""
from __future__ import annotations

import hashlib
import importlib.abc
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from nagasim.settings.userpaths import user_data_dir

log = logging.getLogger(__name__)

__all__ = ["KINDS", "TrustedEntry", "StoreUnreadable", "store_path", "store_problem", "entries", "find",
           "add", "remove", "set_enabled", "enabled_paths", "is_trusted", "folder_files", "code_files", "digest", "changed", "BytesLoader",
           "atomic_write_text"]

KINDS = ("plugin_folder", "policy")
_ADDED_BY = ("user", "assistant")
_FILE = "trusted_code.json"


@dataclass(frozen=True)
class TrustedEntry:
    kind: str
    path: str
    added_by: str = "user"
    enabled: bool = True
    digest: tuple | None = None     # ((file name, sha256), ...) at the person's tick


class StoreUnreadable(OSError):
    """The store exists but does not parse: it is left for the person to repair, never written over."""


def store_path() -> Path:
    return user_data_dir() / _FILE


def _norm(path) -> str:
    # One spelling per place, so the same folder added twice is one entry.
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def folder_files(folder) -> list[tuple[Path, bytes]]:
    """The folder's top-level *.py files, sorted, with their bytes: what `load_plugins` imports, so
    the digest covers exactly the code that runs."""
    return [(f, f.read_bytes()) for f in sorted(Path(folder).glob("*.py")) if f.is_file()]


def code_files(kind: str, path) -> list[tuple[Path, bytes]]:
    """The files an entry of *kind* runs, with their bytes: a plugin folder's top-level *.py, or the
    policy file itself ([] when it is missing, so a file that appears later counts as changed)."""
    if kind == "plugin_folder":
        return folder_files(path)
    file = Path(path)
    return [(file, file.read_bytes())] if file.is_file() else []


def digest(files: list[tuple[Path, bytes]]) -> tuple:
    return tuple((f.name, hashlib.sha256(data).hexdigest()) for f, data in files)


def changed(entry: TrustedEntry, files: list[tuple[Path, bytes]] | None = None) -> bool:
    """Whether an entry's code differs from its digest (no digest counts as changed)."""
    if files is None:
        try:
            files = code_files(entry.kind, entry.path)
        except OSError:
            return True
    return entry.digest is None or digest(files) != entry.digest


def _parse_digest(raw) -> tuple | None:
    if isinstance(raw, list) and all(isinstance(x, list) and len(x) == 2 and all(isinstance(v, str) for v in x)
                                     for x in raw):
        return tuple((name, sha) for name, sha in raw)
    return None


def _pin(kind: str, path, enabled: bool) -> tuple | None:
    """The digest a person's tick stores: the entry's code now; nothing for an off entry."""
    return digest(code_files(kind, path)) if enabled else None


class BytesLoader(importlib.abc.SourceLoader):
    """Runs the bytes that were hashed, not what the file holds a moment later; no bytecode cache
    is read or written (SourceLoader without `path_stats`). Used by `plugins` and `callerpolicy`."""

    def __init__(self, path: Path, data: bytes):
        self.path, self.data = str(path), data

    def get_filename(self, fullname):
        return self.path

    def get_data(self, path):
        if path != self.path:
            raise OSError(f"{path}: trusted code reads only itself")
        return self.data


def _read() -> tuple[list[TrustedEntry], str | None]:
    """(entries, None), or ([], why) when the store exists and does not parse."""
    path = store_path()
    if not path.is_file():
        return [], None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("not a list of entries")
        return [TrustedEntry(str(e["kind"]), str(e["path"]), str(e.get("added_by", "user")),
                             e.get("enabled") is True,      # fail closed: "false", 1 or no key is off
                             _parse_digest(e.get("digest")))
                for e in raw if e.get("kind") in KINDS], None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


def store_problem() -> str | None:
    """Why the store does not read, or None: what Settings > Plugins shows above its list."""
    return _read()[1]


def entries() -> list[TrustedEntry]:
    """Every entry, in the order added. A missing or unreadable file is an empty store
    (logged): a broken store must not stop the application from starting."""
    found, problem = _read()
    if problem:
        log.warning("trusted code store %s unreadable: %s", store_path(), problem)
    return found


def _save(found: list[TrustedEntry]) -> None:
    path = store_path()
    problem = store_problem()
    if problem:
        raise StoreUnreadable(f"the trusted code store {path} does not read ({problem}); repair or remove "
                              "it, nothing was changed")
    atomic_write_text(path, json.dumps([asdict(e) for e in found], indent=2))


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* whole or not at all: both trust stores save through here."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # a temporary file beside the store, then one replace: a write cut short leaves the old store whole
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
        entry = TrustedEntry(kind, old.path, "user", enabled, _pin(kind, old.path, enabled))
        found[index] = entry
    else:
        place = str(Path(path).expanduser().resolve())
        # only a person's add pins the content; the assistant's proposal is off and holds no digest
        entry = TrustedEntry(kind, place, added_by, enabled, _pin(kind, place, enabled and added_by == "user"))
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
    # switching on is the person's tick (the page asks first): it pins what the folder holds now
    found[index] = TrustedEntry(old.kind, old.path, old.added_by, bool(enabled), _pin(old.kind, old.path, bool(enabled)))
    _save(found)


def enabled_paths(kind: str) -> list[Path]:
    return [Path(e.path) for e in entries() if e.kind == kind and e.enabled]


def is_trusted(kind: str, path) -> bool:
    """Whether *path* is an enabled entry of *kind*. It says nothing about content: code that runs also
    checks `changed` on the bytes it runs (`plugins._load_dev_path`, `callerpolicy.load`)."""
    entry = find(kind, path)
    return entry is not None and entry.enabled
