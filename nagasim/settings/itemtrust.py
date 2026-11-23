"""Which item code a user allows to run: the read side of the item trust store.

An item's code (the `code` record of a submodel definition) is known by its fingerprint,
`sha256:` plus the hex digest of its normalised source (`fingerprint`). The fingerprint is
always computed from the source about to run; one written in a file is never read for trust.

The person's store is `<user data dir>/trusted_item_code.json`, a JSON list of
`{"fingerprint", "label", "by", "at"}`. It is a separate file from `trusted_code.json`
because an older build's `trusted._read` drops unknown kinds and would erase item entries
on its next save. Besides the store, the fingerprints in the installed package's own
`bundled_item_code.json` count as trusted: code that ships with the install is the
caller's choice, and no model file, library file or agent tool can add to that list.

SECURITY: permission is the caller's, never the file's (`netguard`). This module only
reads. Writes happen in `trustwrite` alone, which only a person's surfaces import (the
editor's trust dialog and Settings page, and `naga trust` at a terminal); a test pins that
list. Nothing in a model file is ever read into the store, and no environment variable
changes what is trusted.

The store fails closed: a store that exists but does not parse trusts nothing, says why
(`store_problem`) and is never written over. Stdlib only, no Qt.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from nagasim.settings.userpaths import user_data_dir

log = logging.getLogger(__name__)

__all__ = ["MAX_SOURCE", "ItemTrust", "normalise", "code_bytes", "hidden_character", "fingerprint", "short", "store_path",
           "store_problem", "entries", "bundled_fingerprints", "is_trusted", "trusted_fingerprints"]

MAX_SOURCE = 200_000
_FILE = "trusted_item_code.json"
_BUNDLED = Path(__file__).with_name("bundled_item_code.json")
_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Controls besides tab and newline, format characters (bidi controls, zero-width marks), line
# and paragraph separators, lone surrogates: what a person cannot see as Python reads it.
_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


@dataclass(frozen=True)
class ItemTrust:
    fingerprint: str
    label: str
    by: str
    at: str                 # ISO-8601 UTC


def normalise(source: str) -> str:
    """The text that is hashed and run: line endings made `\\n`, trailing whitespace at the
    end stripped, one final newline. A model saved with CRLF keeps its trust."""
    return source.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def code_bytes(source: str) -> bytes:
    """The bytes hashed and run, one function for both. A lone surrogate (legal in JSON as an
    escape) is kept, not raised: such code never parses and is never trusted."""
    return normalise(source).encode("utf-8", "surrogatepass")      # #1998


def hidden_character(source: str) -> str | None:
    """Where *source* first holds a character that shows otherwise than Python reads it
    ("line 3, U+202E"), or None. Such code is never trusted: a terminal escape can erase the
    line that runs, and a bidi control can reorder it on screen."""
    for number, line in enumerate(normalise(source).split("\n"), 1):      # #1997
        for ch in line:
            if ch != "\t" and unicodedata.category(ch) in _HIDDEN:
                return f"line {number}, U+{ord(ch):04X}"
    return None


def fingerprint(source: str) -> str:
    return "sha256:" + hashlib.sha256(code_bytes(source)).hexdigest()


def short(fp: str) -> str:
    """The first 12 hex characters: what a person reads and types."""
    return fp.removeprefix("sha256:")[:12]


def store_path() -> Path:
    return user_data_dir() / _FILE


def _valid(fp) -> bool:
    return isinstance(fp, str) and bool(_SHAPE.match(fp))


def _read() -> tuple[list[ItemTrust], str | None]:
    """(entries, None), or ([], why) when the store exists and does not parse."""
    path = store_path()
    if not path.is_file():
        return [], None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("not a list of entries")
        found = []
        for e in raw:
            if not isinstance(e, dict) or not _valid(e.get("fingerprint")):
                continue        # a forged or malformed entry trusts nothing
            if not all(isinstance(e.get(k), str) for k in ("label", "by", "at")):
                continue
            found.append(ItemTrust(e["fingerprint"], e["label"], e["by"], e["at"]))
        return found, None
    except (OSError, ValueError, TypeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


def store_problem() -> str | None:
    """Why the store does not read, or None."""
    return _read()[1]


def entries() -> list[ItemTrust]:
    """Every valid entry, in the order added; an unreadable store is empty (logged)."""
    found, problem = _read()
    if problem:
        log.warning("trusted item code store %s unreadable: %s", store_path(), problem)
    return found


@functools.cache
def bundled_fingerprints() -> frozenset[str]:
    """The fingerprints of the item code shipped with this install (read once)."""
    try:
        raw = json.loads(_BUNDLED.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("bundled item code list %s unreadable: %s", _BUNDLED, exc)
        return frozenset()
    return frozenset(fp for fp in raw if _valid(fp)) if isinstance(raw, list) else frozenset()


def trusted_fingerprints() -> frozenset[str]:
    """Every fingerprint trusted now (the person's store and the bundled set), read once:
    for a caller that checks many."""
    return frozenset(e.fingerprint for e in entries()) | bundled_fingerprints()


def is_trusted(fp: str) -> bool:
    return fp in trusted_fingerprints()
