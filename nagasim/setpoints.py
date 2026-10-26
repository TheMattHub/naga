"""The caller's setpoints file (Phase 12 E, D742; Phase 15 FE, D811): what a `setpoint`
candidate may name, read and checked here, apart from the writer.

    tags = setpoints.load("line.setpoints.json")["tags"]

`{"format": 1, "kind": "naga-setpoints", "connection": NAME, "tags": {TAG: {"register",
"path", "min", "max", "max_step", "min_interval"}}}`: per tag the register of the connection
it is written to, the model path that simulates it, its range, the largest change from the
value the PLC holds now, and the shortest time between two writes.

Why its own module: the twin, the plant monitor and the editor need the file to check and
show a setpoint, and `plcwrite` (the only writer, D743) must stay a module that nothing of
the product or an agent imports (tests/test_plcwrite.py scans for one). Reading the file
writes nothing and opens no socket. The file is data: it selects no code; its connection is
a name of the connections file. Stdlib only.
"""
from __future__ import annotations

import json
import re

from nagasim import netguard, recommend, refit
from nagasim.core.utils import is_finite_number

__all__ = ["FORMAT", "KIND", "LIMITS", "Refused", "load"]

FORMAT = 1
KIND = "naga-setpoints"
# Ceilings. The caller's setpoints file moves a limit under its ceiling; nothing else does.
LIMITS = {
    "tags": 100,                    # of one setpoints file
    "file_bytes": 2 ** 20,          # the setpoints file
    "path": 400,                    # characters of a tag's model path
    "value": recommend.LIMITS["setpoint"],  # the size of a min or a max, either way
    "interval": (1.0, 7 * 86400.0),     # seconds a min_interval may be, at least and at most
}
_TAG = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TAG_KEYS = frozenset({"register", "path", "min", "max", "max_step", "min_interval"})
_HINT = "see the setpoints file in the twin's documentation"


class Refused(Exception):
    """A coded refusal; `.code` names it, `.found` keeps what was read before it (the
    writer's attempts use it too: `plcwrite.Refused` is this class)."""

    def __init__(self, code: str, message: str, hint: str | None = None, **found):
        super().__init__(message)
        self.code, self.message, self.hint, self.found = code, message, hint, found


def _number(value) -> bool:
    return is_finite_number(value) and not isinstance(value, bool) \
        and abs(value) <= LIMITS["value"]


def _tag(name, spec) -> dict:
    from nagasim.twin import parse_interval

    def refuse(text):
        return Refused("E_PLC_SETPOINTS", f"tag '{str(name)[:64]}': {text}", _HINT)

    if not (isinstance(spec, dict) and set(spec) == _TAG_KEYS):
        raise refuse(f"a tag is exactly {', '.join(sorted(_TAG_KEYS))}")
    register, path = spec["register"], spec["path"]
    if not (isinstance(register, str) and _TAG.match(register)):
        raise refuse("\"register\" names a register of the connection's map")
    if not (isinstance(path, str) and 0 < len(path) <= LIMITS["path"] and path.isprintable()):
        raise refuse("\"path\" is the model path that simulates the tag")
    if refit.forbidden(path):          # the refit file's rule, one check (#981)
        raise refuse(f"\"path\" '{path[:80]}' sets no state, run setting or data source: "
                     f"name a parameter of a block, a resource or a variable")
    low, high, step = spec["min"], spec["max"], spec["max_step"]
    if not (_number(low) and _number(high) and low < high):
        raise refuse(f"\"min\" is below \"max\", both numbers of at most {LIMITS['value']:g}")
    if not (_number(step) and 0 < step <= high - low):
        raise refuse("\"max_step\" is more than nothing and at most max - min")
    try:
        interval = parse_interval(spec["min_interval"])
    except ValueError:
        raise refuse("\"min_interval\" is an interval: 30s, 15m, 2h") from None
    least, most = LIMITS["interval"]
    if not least <= interval <= most:
        raise refuse(f"\"min_interval\" is from {least:g} s to {most:g} s")
    return {"register": register, "path": path, "min": low, "max": high, "max_step": step,
            "min_interval": interval}


def load(path) -> dict:
    """The caller's setpoints file: `{"connection", "tags": {tag: {...}}}`, or `Refused`. The
    file is data: it selects no code; its connection is a name of the connections file."""
    try:
        doc = json.loads(netguard.read_caller_file(path, max_bytes=LIMITS["file_bytes"],
                                                   what="the setpoints file"))
    except netguard.FileRefused as exc:
        raise Refused("E_PLC_SETPOINTS", exc.message, exc.hint or _HINT) from None
    except (ValueError, RecursionError) as exc:
        raise Refused("E_PLC_SETPOINTS", f"the setpoints file cannot be read "
                      f"({type(exc).__name__})", _HINT) from None
    if not (isinstance(doc, dict) and set(doc) == {"format", "kind", "connection", "tags"}
            and doc["format"] == FORMAT and doc["kind"] == KIND):
        raise Refused("E_PLC_SETPOINTS", f"this is not a setpoints file of format {FORMAT}",
                      f"its keys: format, kind (\"{KIND}\"), connection, tags")
    tags = doc["tags"]
    if not (isinstance(doc["connection"], str) and 0 < len(doc["connection"]) <= 200):
        raise Refused("E_PLC_SETPOINTS", "\"connection\" names a connection of your "
                      "connections file", _HINT)
    if not (isinstance(tags, dict) and 0 < len(tags) <= LIMITS["tags"]):
        raise Refused("E_PLC_SETPOINTS", f"\"tags\" is an object of 1 to {LIMITS['tags']} "
                      f"tags", _HINT)
    for name in tags:
        if not _TAG.match(name):
            raise Refused("E_PLC_SETPOINTS", f"a tag's name is 1 to 64 letters, digits or "
                          f"_.- ('{name[:64]}')", _HINT)
    return {"connection": doc["connection"],
            "tags": {name: _tag(name, spec) for name, spec in tags.items()}}
