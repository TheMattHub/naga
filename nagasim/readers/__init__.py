"""Live readers (PHASE7 7.8): one module per kind of live data source.

A live source names a CONNECTION the caller defined (`netguard.Connection`), never a host,
and is read through `nagasim.live.Feed`, which is the only caller of these modules. A reader
module is plain functions:

    check(source, conn) -> options     refuse what the source asks for, before any socket;
                                       the source's options, checked, with their defaults
    read(source, conn, options) -> list[dict]             a reader that polls (rest), or
    subscribe(source, conn, options, sink, stop) -> None  one that holds a link open: it
        calls sink.connected() once the link is up, sink.accept(key, value, source_time) per
        value, sink.count(reason) per thing it dropped, and returns or raises when the link
        ends or *stop* is set

A reader opens its socket with `netguard.open_connection` and nothing else, raises
`sources.ReadError` or a `netguard.GuardError`, has no request that writes, and treats
everything it reads as data: scalars, never a path, an address, a name that selects code.

Stdlib only, and importing this package (or `databind`, or `live`) loads no reader: `load`
imports one on first use. There is no entry-point group (D214): a reader runs with the
caller's network consent, so the product adds one by import and nobody else does.
"""
from __future__ import annotations

import importlib

from nagasim.sources import ReadError

__all__ = ["KINDS", "load", "checked_options"]

# The live kinds, in the order `schema.DATA_SOURCE_KINDS` lists them.
KINDS: tuple[str, ...] = ("rest", "mqtt", "opcua")


def load(kind: str):
    """The reader module of *kind*, imported now."""
    if kind not in KINDS:
        raise ReadError("E_DATA_SOURCE_KIND", f"'{kind}' is not a live source kind",
                        f"live kinds: {', '.join(KINDS) or '(none)'}")
    return importlib.import_module(f"{__name__}.{kind}")


def checked_options(source: dict, allowed: dict) -> dict:
    """The source's options against *allowed*, with the defaults filled in.

    *allowed* is {name: (default, valid)}; `valid` is `str` (any text), a (least, most)
    pair of numbers, or a frozenset of choices. A live source's options are OURS: an
    unknown one is an error and none is passed through to a library
    (`E_DATA_SOURCE_OPTION`)."""
    given = source.get("options") or {}
    out = {name: default for name, (default, _valid) in allowed.items() if default is not None}
    for name, value in (given.items() if isinstance(given, dict) else [("options", given)]):
        if name not in allowed:
            raise ReadError("E_DATA_SOURCE_OPTION",
                            f"source '{source.get('name')}': unknown option '{name}' for a "
                            f"{source.get('kind')} source", f"its options: {', '.join(allowed)}")
        valid = allowed[name][1]
        if valid is str:
            fits, wanted = isinstance(value, str) and bool(value), "a text"
        elif isinstance(valid, frozenset):
            fits = isinstance(value, (str, int)) and not isinstance(value, bool) and value in valid
            wanted = "one of " + ", ".join(sorted(map(str, valid)))
        else:
            fits = isinstance(value, (int, float)) and not isinstance(value, bool) \
                and valid[0] <= value <= valid[1]
            wanted = f"a number from {valid[0]} to {valid[1]}"
        if not fits:
            raise ReadError("E_DATA_SOURCE_OPTION",
                            f"source '{source.get('name')}': option '{name}' has a value it "
                            f"cannot take", f"'{name}' is {wanted}")
        out[name] = value
    return out
