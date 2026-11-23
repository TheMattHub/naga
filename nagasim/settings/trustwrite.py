"""The write side of the item trust store (`itemtrust`): a person's yes on exact code.

SECURITY: permission is the caller's, never the file's (`netguard`). Only a person's
surfaces import this module: `naga trust` at a terminal (`nagasim/trustcli.py`), the
editor's trust dialog (`product/ui/itemcode.py`) and its Settings page
(`product/ui/plugins_page.py`). `tests/test_itemtrust.py` pins that list by an AST
walk, so no agent tool, MCP server or assistant module reaches it. `trust_source` takes
the source and computes the fingerprint itself, so nobody can trust a bare hash string.
Nothing in a model file is ever read into the store. Stdlib only, no Qt.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from nagasim.settings import itemtrust
from nagasim.settings.trusted import StoreUnreadable, atomic_write_text

__all__ = ["trust_source", "revoke"]


def _load() -> list[itemtrust.ItemTrust]:
    found, problem = itemtrust._read()
    if problem:
        raise StoreUnreadable(f"the trusted item code store {itemtrust.store_path()} does not read "
                              f"({problem}); repair or remove it, nothing was changed")
    return found


def _save(found: list[itemtrust.ItemTrust]) -> None:
    atomic_write_text(itemtrust.store_path(), json.dumps([asdict(e) for e in found], indent=2))


def trust_source(source: str, *, label: str, by: str) -> str:
    """Trust *source* on this computer and return its fingerprint (an entry already there
    is kept as it is)."""
    if len(source) > itemtrust.MAX_SOURCE:
        raise ValueError(f"item code is longer than {itemtrust.MAX_SOURCE} characters")
    hidden = itemtrust.hidden_character(source)
    if hidden:
        raise ValueError(f"item code holds a character that shows otherwise than it runs ({hidden}); "
                         "remove it, or write it as an escape such as \\u202e, then trust it")
    fp = itemtrust.fingerprint(source)
    found = _load()
    if not any(e.fingerprint == fp for e in found):
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save([*found, itemtrust.ItemTrust(fp, str(label), str(by), at)])
    return fp


def revoke(prefix: str) -> list[str]:
    """Remove every entry whose hex starts with *prefix* (at least 12 hex characters, with
    or without `sha256:`); return the fingerprints removed."""
    hexpart = prefix.strip().lower().removeprefix("sha256:")
    if len(hexpart) < 12 or any(c not in "0123456789abcdef" for c in hexpart):
        raise ValueError("give at least 12 hex characters of the fingerprint")
    found = _load()
    gone = [e.fingerprint for e in found if e.fingerprint.removeprefix("sha256:").startswith(hexpart)]
    if gone:
        _save([e for e in found if e.fingerprint not in gone])
    return gone
