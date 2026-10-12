"""Read-only local file sources, shared by data bindings (`databind`) and the
MES adapters (`mes.adapters`).

Both read files a model or an adapter spec names, so both get the same rules,
enforced here and nowhere else:

* `confined` -- when the CALLER passes *confine_to* (the UI, an agent surface),
  the path must resolve under it (`netguard.confine_path`: symlinks resolved
  first, UNC/device paths and alternate data streams refused). Permission is the
  caller's: nothing a file says can set or widen *confine_to*. A UNC path
  (`\\\\server\\share\\...`) is refused as NETWORK access -- unless the caller's
  `NetworkPolicy` allows it -- even with no *confine_to* at all: merely stat-ing
  one makes Windows open an SMB connection that can leak NTLM credentials, so
  opening an untrusted model that names one must not touch the filesystem
  first (DEVLOG #268).
* `open_sqlite` -- opens mode=ro AND lets a statement only read (an authorizer
  allow-list), because mode=ro alone still lets ATTACH reach any file on disk
  and VACUUM INTO write one (DEVLOG #266, #267).
* `csv_rows` -- one CSV file as dicts, confined per file, so a symlink inside a
  confined folder cannot point out of it.

Stdlib only: databind hands the connection or path to pandas itself, so the
pandas allow-list convention (CLAUDE.md) is untouched. No network kinds live
here; `sql` and `json_url` stay behind `netguard.check_sql_url`/`fetch` in
databind.
"""
from __future__ import annotations

import csv
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import quote

from nagasim import netguard

# Everything a plain or recursive SELECT needs; ATTACH, PRAGMA, VACUUM, DDL and
# DML are denied whatever the query text says.
_READ_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                           sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE})


class ReadError(Exception):
    """A coded failure of one read, raised by a reader that must not import `databind`
    (the live readers under `nagasim/readers/`). It becomes the binding's `Problem` exactly
    as a `netguard.GuardError` does. *status* is the HTTP status when the source gave one."""

    def __init__(self, code: str, message: str, hint: str | None = None,
                 status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.status = code, message, hint, status


def _read_only(action, *_):
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def confined(path, confine_to, network: netguard.NetworkPolicy | None = None) -> Path:
    """*path* as a Path, confined under *confine_to* when the caller gave one.

    Checked BEFORE any filesystem call: a UNC path (`is_unc_path`) is refused
    as network access unless *network* allows it, whether or not *confine_to*
    is set -- and when *confine_to* IS set, `confine_path` refuses a UNC path
    outright regardless of *network*, as it always has (a confined caller
    never reads outside its root, network or not).
    """
    text = str(path)
    if confine_to is None and netguard.is_unc_path(text) and not (network and network.allow_network):
        raise netguard.NetworkRefused(
            "E_DATA_NETWORK_REFUSED",
            f"'{text}' is a network path, so it was not read",
            "only a human can allow this: pass --allow-network, or point the source at a "
            "local path or a mapped drive")
    path = Path(path)
    return netguard.confine_path(path, confine_to) if confine_to is not None else path


@contextmanager
def open_sqlite(path, *, confine_to=None, network=None):
    """A read-only connection to an existing SQLite file, closed on exit (so a
    long-running reader never holds a lock on the file)."""
    path = confined(path, confine_to, network)
    # Percent-encoded so a '?', '#' or '%' in the file name cannot add URI
    # parameters (e.g. a second mode=rwc) to the ones set here.
    uri = "file:" + quote(str(path), safe=":/\\") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.set_authorizer(_read_only)
        yield conn


def csv_rows(path, *, confine_to=None, network=None) -> list[dict]:
    """Every row of one CSV file as a dict (header = keys; a BOM is dropped)."""
    with confined(path, confine_to, network).open(newline="", encoding="utf-8-sig") as handle:
        return [dict(r) for r in csv.DictReader(handle)]
