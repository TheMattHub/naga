"""Advisory PLC writes (Phase 12, D742-D744): `naga plc-write`, a person's command that
writes the setpoints a person approved to a soft PLC on this machine, over Modbus TCP.

    naga plc-write --outbox file:q.jsonl --setpoints line.setpoints.json \\
                   --connections plc.json --audit writes.jsonl            # dry-run
    naga plc-write ... --write                                            # writes

The twin proposes a `setpoint` (`recommend`, D742) and delivers it to the outbox like any
record; it never writes to a plant (rule 12). This command, run by a person as its own
process, is the only writer (D743):

* WHAT MAY BE WRITTEN is the caller's SETPOINTS FILE, `{"format": 1, "kind":
  "naga-setpoints", "connection": NAME, "tags": {TAG: {"register", "path", "min", "max",
  "max_step", "min_interval"}}}`: per tag the register of the connection it is written to,
  the model path that simulates it (the twin's), its range, the largest change from the value
  the PLC holds now, and the shortest time between two writes. A record names a tag and a
  number; nothing of a record, a model or a reply selects a register, an address or a host.
* WHERE is a connection of the caller's connections file with `"use": "write"`, kind modbus,
  that resolves to this machine only (`netguard.vet_connection`, D744); every reader refuses
  it (`netguard.connection_for`).
* WHEN: only a record whose first decision is a `taken` reply with a person's `by`, not ended,
  not expired, never written before (its id is in the audit as written).
* HOW: dry-run unless `--write` (it reads the PLC, writes nothing but the audit); function codes
  6 and 16 only, to holding registers; the value read back must be the one written.
* THE AUDIT (`--audit FILE`, JSON lines, appended and read back) gets a line BEFORE anything is
  sent and one AFTER, for every attempt; a line that cannot be written stops the command
  before any later write. The audit is also what the interval and "written before" read.

Stdlib only. No agent tool imports this module (tests/test_plcwrite.py scans for one).
"""
from __future__ import annotations

import json
import math
import re
import socket
import struct
import threading
import time
from pathlib import Path

from nagasim import netguard, outbox, recommend, refit
from nagasim.live import iso, stamp
from nagasim.core.utils import is_finite_number

__all__ = ["FORMAT", "KIND", "AUDIT_KIND", "LIMITS", "FUNCTIONS", "Refused", "load", "encode",
           "run", "main"]

FORMAT = 1
KIND = "naga-setpoints"
AUDIT_KIND = "naga-plc-audit"
# Ceilings. The caller's setpoints file moves a limit under its ceiling; nothing else does.
LIMITS = {
    "tags": 100,                    # of one setpoints file
    "file_bytes": 2 ** 20,          # the setpoints file
    "path": 400,                    # characters of a tag's model path
    "value": recommend.LIMITS["setpoint"],  # the size of a min or a max, either way
    "interval": (1.0, 7 * 86400.0),     # seconds a min_interval may be, at least and at most
    "attempts": 100,                # records one run tries
    "pulls": 200,                   # reads of the replies file, 1 MiB each
}
FUNCTIONS = (6, 16)                 # write single register, write multiple registers: no other
WRITE_TIMEOUT = 20.0                # the whole write, a deadline
_TAG = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TAG_KEYS = frozenset({"register", "path", "min", "max", "max_step", "min_interval"})
_RANGES = {"uint16": (0, 2 ** 16 - 1), "int16": (-2 ** 15, 2 ** 15 - 1),
           "uint32": (0, 2 ** 32 - 1), "int32": (-2 ** 31, 2 ** 31 - 1)}
_EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
               4: "device failure", 6: "device busy"}
_HINT = "see the setpoints file in the twin's documentation"


class Refused(Exception):
    """A coded refusal; `.code` names it, `.found` keeps what was read before it."""

    def __init__(self, code: str, message: str, hint: str | None = None, **found):
        super().__init__(message)
        self.code, self.message, self.hint, self.found = code, message, hint, found


# ---------------------------------------------------------------- the setpoints file
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
        if Path(path).stat().st_size > LIMITS["file_bytes"]:
            raise Refused("E_PLC_SETPOINTS", f"the setpoints file is over "
                          f"{LIMITS['file_bytes']} bytes", _HINT)
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError) as exc:
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


def _writable(allow: dict, connections: dict) -> netguard.Connection:
    """The connection the setpoints file names, kept for writes, with every tag's register a
    holding register of its map; or `Refused`."""
    conn = connections.get(allow["connection"])
    if conn is None or conn.kind != "modbus" or conn.use != "write":
        raise Refused("E_PLC_CONNECTION", f"the connections file has no modbus connection "
                      f"'{allow['connection'][:80]}' with \"use\": \"write\"",
                      "only a connection kept for writes is written to; no reader opens it")
    registers = dict(conn.registers)
    for tag, spec in allow["tags"].items():
        register = registers.get(spec["register"])
        if register is None or register.kind != "holding" or register.type == "bool":
            raise Refused("E_PLC_REGISTER", f"tag '{tag}': '{spec['register']}' is no holding "
                          f"register of connection '{conn.name}'",
                          "a setpoint is written to a holding register of 16 or 32 bits")
    return conn


# ---------------------------------------------------------------- Modbus
def encode(register: netguard.Register, value) -> bytes:
    """The register's bytes for *value*, the inverse of `readers.modbus.decode`, or
    `Refused` when it holds no such value."""
    raw = value / register.scale
    if register.type == "float32":
        try:
            data = struct.pack(">f", raw)
        except (OverflowError, struct.error):
            data = b""
        if not data or not math.isfinite(struct.unpack(">f", data)[0]):
            raise Refused("E_PLC_VALUE", f"{value:g} does not fit a float32 register")
    else:
        whole = round(raw)
        low, high = _RANGES[register.type]
        if abs(whole - raw) > 1e-9 * max(1.0, abs(raw)) or not low <= whole <= high:
            raise Refused("E_PLC_VALUE", f"{value:g} is not a whole number of steps of "
                          f"{register.scale:g} that a {register.type} register holds")
        words = netguard.MODBUS_TYPES[register.type]
        data = int(whole).to_bytes(2 * words, "big", signed=register.type.startswith("int"))
    if len(data) == 4 and register.word_order == "little":
        data = data[2:] + data[:2]
    return data


def _recv(sock, n: int) -> bytes:
    chunks = []
    while n:
        chunk = sock.recv(n)
        if not chunk:
            raise ConnectionError("closed")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def _write(conn: netguard.Connection, register: netguard.Register, data: bytes) -> None:
    """One write of *data* to *register*, function 6 for one word and 16 for two; the answer
    judged before it is read and against what was sent. Whole or `Refused`."""
    words = len(data) // 2
    if words == 1:
        function = 6
        pdu = struct.pack(">BBH", conn.unit, function, register.address) + data
        echo = pdu
    else:
        function = 16
        pdu = struct.pack(">BBHHB", conn.unit, function, register.address, words, 2 * words) + data
        echo = struct.pack(">BBHH", conn.unit, function, register.address, words)
    assert function in FUNCTIONS
    sock = netguard.open_connection(conn)
    expired = threading.Event()

    def abort():
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(WRITE_TIMEOUT, abort)
    timer.daemon = True
    timer.start()
    try:
        sock.settimeout(WRITE_TIMEOUT)
        sock.sendall(struct.pack(">HHH", 1, 0, len(pdu)) + pdu)
        tid, protocol, length = struct.unpack(">HHH", _recv(sock, 6))
        if tid != 1 or protocol != 0 or not 3 <= length <= len(echo):
            raise ValueError("a frame that is not the answer")
        body = _recv(sock, length)
        if body[:2] == bytes([conn.unit, function | 0x80]) and length == 3:
            raise Refused("E_PLC_DEVICE", f"the PLC refused the write "
                          f"({_EXCEPTIONS.get(body[2], 'exception ' + str(body[2]))})")
        if body != echo:
            raise ValueError("an answer of another shape")
    except (OSError, ValueError, struct.error, IndexError) as exc:
        what = "gave no answer within the deadline" if expired.is_set() \
            else f"failed ({type(exc).__name__})"
        raise Refused("E_PLC_WRITE", f"the write to connection '{conn.name}' {what}") from None
    finally:
        timer.cancel()
        sock.close()


def _read(conn: netguard.Connection, name: str, register: netguard.Register):
    """The register's value now, by the reader's own code (codes 3 only, here)."""
    from nagasim.readers import modbus

    return modbus.read({"name": name}, conn, {"names": [(name, register)]})[0]["value"]


# ---------------------------------------------------------------- the outbox and the audit
def _lines(path: Path):
    """The lines of a JSON-lines file the caller named, bounded, or none when it is not there."""
    try:
        if path.stat().st_size > outbox.LIMITS["sink_bytes"]:
            raise Refused("E_PLC_OUTBOX", f"'{path.name[:80]}' is over its ceiling of "
                          f"{outbox.LIMITS['sink_bytes']} bytes")
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise Refused("E_PLC_OUTBOX", f"'{path.name[:80]}' cannot be read "
                      f"({type(exc).__name__})") from None
    found = []
    for line in data.split(b"\n"):
        if not line.strip() or len(line) > outbox.LIMITS["record_bytes"]:
            continue
        try:
            found.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    return found


def _proposals(sink: outbox.FileSink) -> tuple:
    """(the setpoint records of the outbox, the first decision on each id, the ids ended)."""
    records = {}
    for record in _lines(sink.path):
        if recommend.check(record) and record["action"]["verb"] == "setpoint":
            records.setdefault(record["id"], record)
    decided, ended, cursor = {}, set(), None
    for _ in range(LIMITS["pulls"]):
        got = sink.changes(outbox.REPLIES, cursor, 1000)
        if got.state != "live":
            raise Refused("E_PLC_OUTBOX", "the replies file cannot be read")
        for event in got.events:
            reply = event.value
            if reply["decision"] == "ended":
                ended.add(reply["id"])
            else:                       # the first decision wins, as the twin's ledger has it
                decided.setdefault(reply["id"], reply)
        if got.cursor == cursor:
            break
        cursor = got.cursor
    return list(records.values()), decided, ended


def _history(lines: list) -> tuple:
    """(the ids written, {tag: the times of its writes}) from the audit. A write sent, or
    begun and never ended, counts for the interval (it may have landed), not as done."""
    after = {(e.get("id"), e.get("began")): e for e in lines
             if isinstance(e, dict) and e.get("kind") == AUDIT_KIND and e.get("phase") == "after"}
    written, times = set(), {}
    for entry in lines:
        if not (isinstance(entry, dict) and entry.get("kind") == AUDIT_KIND
                and entry.get("phase") == "before" and entry.get("mode") == "write"):
            continue
        at = stamp(entry.get("at")) if isinstance(entry.get("at"), str) else None
        end = after.get((entry.get("id"), entry.get("at")))
        if end is not None and end.get("outcome") == "written":
            written.add(entry.get("id"))
        if at is not None and (end is None or end.get("sent") is not False) \
                and isinstance(entry.get("tag"), str):
            times.setdefault(entry["tag"], []).append(at)
    return written, times


# ---------------------------------------------------------------- one attempt
def _attempt(record, reply, ended, spec, conn, *, write, last, now) -> dict:
    """The outcome of one record: `{"outcome", "code", "message", "previous", "read_back"}`."""
    tag, value = record["action"]["target"], record["action"]["value"]
    if reply is None or reply["decision"] != "taken":
        raise Refused("E_PLC_NOT_TAKEN", "no person's \"taken\" reply decided it" + (
            "" if reply is None else f" (it was {reply['decision']})"))
    if not (reply["by"] or "").strip():
        raise Refused("E_PLC_NO_BY", "its \"taken\" reply names nobody in \"by\"",
                      "a write is a person's act: the reply says who")
    if record["id"] in ended:
        raise Refused("E_PLC_ENDED", "an \"ended\" reply says the action stands no more")
    if now >= stamp(record["expires"]):
        raise Refused("E_PLC_EXPIRED", f"it expired at {record['expires']}")
    if spec is None:
        raise Refused("E_PLC_TAG", f"the tag '{tag[:80]}' is not in your setpoints file")
    if not spec["min"] <= value <= spec["max"]:
        raise Refused("E_PLC_RANGE", f"{value:g} is outside {spec['min']:g} to {spec['max']:g}")
    register = dict(conn.registers)[spec["register"]]
    data = encode(register, value)
    if last is not None and now - last < spec["min_interval"]:
        raise Refused("E_PLC_INTERVAL", f"'{tag[:80]}' was written {now - last:.0f} s ago, "
                      f"under its min_interval of {spec['min_interval']:g} s")
    previous = _read(conn, spec["register"], register)
    if previous is None or abs(value - previous) > spec["max_step"] * (1 + 1e-9):
        raise Refused("E_PLC_STEP", f"{value:g} is more than max_step {spec['max_step']:g} from "
                      f"the value the PLC holds" + ("" if previous is None else f", {previous:g}"),
                      previous=previous)
    if not write:
        return {"outcome": "dry-run", "previous": previous}
    from nagasim.readers import modbus

    try:
        _write(conn, register, data)
        back = _read(conn, spec["register"], register)
    except Refused as exc:
        exc.found.update(previous=previous, sent=True)
        raise
    except Exception as exc:            # the read back: the reader's coded refusal
        raise Refused("E_PLC_READBACK", f"the value could not be read back "
                      f"({getattr(exc, 'code', type(exc).__name__)})",
                      previous=previous, sent=True) from None
    if back != modbus.decode(register, data):
        raise Refused("E_PLC_READBACK", f"the PLC holds {back} after the write, not {value:g}",
                      previous=previous, read_back=back, sent=True)
    return {"outcome": "written", "previous": previous, "read_back": back, "sent": True}


# ---------------------------------------------------------------- the command
def run(outbox_spec, setpoints_path, connections_path, audit_path, *, write=False, ids=None,
        wall=time.time, out=print) -> int:
    """`naga plc-write`: 0 when every attempt was written (or, dry-run, would be), 1 when one
    was refused or failed, or nothing could start."""
    try:
        allow = load(setpoints_path)
        conn = _writable(allow, netguard.load_connections(connections_path))
        netguard.vet_connection(conn)           # this machine only (D744), before anything
        scheme, _, rest = str(outbox_spec).partition(":")
        if scheme != "file" or not rest:
            raise Refused("E_PLC_OUTBOX", "plc-write reads a file outbox, file:<path>",
                          "the twin's --outbox of the same run")
        sink, audit = outbox.FileSink(rest), outbox.FileSink(audit_path)
        records, decided, ended = _proposals(sink)
        written, times = _history(_lines(audit.path))
    except (Refused, netguard.GuardError, outbox.SinkError) as exc:
        out(f"{exc.code}: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))
        return 1
    out(netguard.describe_connection(conn))
    mode = "write" if write else "dry-run"
    todo = [r for r in records if r["id"] not in written and (not ids or any(
        isinstance(i, str) and i and r["id"].startswith(i) for i in ids))]
    if not todo:
        out("no setpoint of the outbox is left to write")
        return 0
    if len(todo) > LIMITS["attempts"]:
        out(f"{len(todo)} setpoints: the first {LIMITS['attempts']} are tried in this run")
        todo = todo[:LIMITS["attempts"]]
    failed = 0
    for record in todo:
        tag, value = record["action"]["target"], record["action"]["value"]
        reply = decided.get(record["id"])
        began = iso(wall())
        entry = {"kind": AUDIT_KIND, "id": record["id"], "tag": tag, "value": value,
                 "by": None if reply is None else reply["by"], "mode": mode}
        try:
            audit.deliver({**entry, "phase": "before", "at": began})
        except (OSError, ValueError) as exc:
            out(f"E_PLC_AUDIT: the audit could not be written ({exc}): nothing more is tried")
            return 1
        now = stamp(began)
        try:
            spec = allow["tags"].get(tag)
            last = max(times.get(tag, []), default=None)
            found = _attempt(record, reply, ended, spec, conn, write=write, last=last, now=now)
        except Refused as exc:
            found = {"outcome": "refused", "code": exc.code, "message": exc.message, **exc.found}
        except Exception as exc:        # a read's coded refusal, or anything: the after line
            found = {"outcome": "failed", "code": getattr(exc, "code", type(exc).__name__),
                     "message": str(getattr(exc, "message", ""))[:200]}
        found.setdefault("sent", False)
        if found["sent"]:                   # it may have landed: the interval holds
            times.setdefault(tag, []).append(now)
        try:
            audit.deliver({**entry, "phase": "after", "began": began, "at": iso(wall()),
                           **found})
        except (OSError, ValueError) as exc:
            out(f"E_PLC_AUDIT: the audit could not be written ({exc}): nothing more is tried")
            return 1
        name = f"{record['id'][:12]} setpoint {tag} = {value:g}"
        if found["outcome"] == "written":
            out(f"{name}: written (was {found['previous']:g}, read back {found['read_back']:g})")
        elif found["outcome"] == "dry-run":
            out(f"{name}: would be written (dry-run; the PLC holds {found['previous']:g})")
        else:
            failed += 1
            out(f"{name}: {found['outcome']} {found['code']}: {found['message']}")
    return int(bool(failed))


def main(argv=None) -> int:
    import argparse

    from nagasim._cli import setup_cli

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga plc-write",
        description="Write the setpoints a person approved in a twin's outbox to a soft PLC on "
                    "this machine, over Modbus TCP. Dry-run unless --write: it then reads the "
                    "PLC and writes only the audit. Only a record with a \"taken\" reply that "
                    "names who took it, inside your setpoints file's range, step and interval, "
                    "is written, and it is read back. Every attempt is audited before and after.")
    parser.add_argument("--outbox", required=True, metavar="SPEC",
                        help="the twin's outbox, file:<path>; replies are read from "
                             "<path>.replies beside it")
    parser.add_argument("--setpoints", required=True, metavar="FILE",
                        help="your setpoints file (format 1, kind naga-setpoints): the tags "
                             "that may be written, each with its register, model path, range, "
                             "max_step and min_interval, and the connection they are written to")
    parser.add_argument("--connections", required=True, metavar="FILE",
                        help="your connections file: the setpoints file's connection, kind "
                             "modbus with \"use\": \"write\", on this machine")
    parser.add_argument("--audit", required=True, metavar="FILE",
                        help="the audit, JSON lines appended: a line before and after every "
                             "attempt; also what the interval and \"written before\" are read from")
    parser.add_argument("--write", action="store_true",
                        help="write; without it nothing is sent to the PLC but reads")
    parser.add_argument("--id", action="append", default=None, metavar="ID",
                        help="only this record (its id or the first characters of it); may "
                             "repeat (default: every setpoint not yet written)")
    args = parser.parse_args(argv)
    return run(args.outbox, args.setpoints, args.connections, args.audit, write=args.write,
               ids=args.id)
