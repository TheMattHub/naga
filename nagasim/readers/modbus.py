"""Modbus TCP reader (PHASE8 H1, D693): polls registers over the caller's connection.

    {"name": "oven", "kind": "modbus", "connection": "oven", "location": "temp\\nrunning",
     "options": {"poll": 5}}

`location` holds register NAMES, one per line. What a name means (address, kind, type, scale,
word order) is the caller's register map in the connections file; a model chooses among the
registers the caller listed, never an address. Each poll is a row per name,
`{"register": name, "value": ...}`, and a changed value is one event of the feed.

The protocol is small enough to write, so this is stdlib only, with no optional extra: the
three or four request kinds are built here, over the socket `netguard.open_connection` pinned
(a library would resolve the host itself). What keeps it a reader:

* the only function codes in this file are 1, 2, 3 and 4 (the reads); a write cannot be built;
* the answer is judged before it is used: a declared length over the PDU maximum is refused
  before it is read, and the transaction, unit, function and byte count must match;
* one deadline covers a whole poll, a slow server cannot hold a cycle;
* a register is decoded to a scalar; a non-finite float is None; nothing read selects code.

`tests/realserver/modbus_server.py` is a pymodbus server (optional extra `nagasim[modbus]`) the
opt-in real-server test reads this module against.
"""
from __future__ import annotations

import math
import socket
import struct
import threading

from nagasim import netguard, readers
from nagasim.sources import ReadError

__all__ = ["OPTIONS", "check", "read", "decode"]

READ_TIMEOUT = 20.0         # the whole poll: a deadline, so a dripping server holds no cycle
OPTIONS = {"poll": (5.0, (1.0, 86_400.0))}
MAX_NAMES = 200             # registers one source may name (one request each)
MAX_PDU = 253               # Modbus TCP: unit id + function + data never exceed 253 bytes
_HINT = "check the connection, its unit id and register map"
_EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
               4: "device failure", 6: "device busy"}


def check(source: dict, conn: netguard.Connection) -> dict:
    """The source's options and the registers it names, or the refusal. No socket is opened."""
    options = readers.checked_options(source, OPTIONS)
    names = [line.strip() for line in str(source.get("location") or "").splitlines()
             if line.strip()]
    known, root = dict(conn.registers), conn.root.strip()
    if not names or len(names) > MAX_NAMES or any(
            n not in known or not n.startswith(root) for n in names):
        raise ReadError("E_DATA_SOURCE_LOCATION",
                        f"source '{source.get('name')}': its location must be register names, "
                        f"one per line, from the register map of connection '{conn.name}'",
                        "the register map is set by the human who owns the connections file")
    options["names"] = [(n, known[n]) for n in dict.fromkeys(names)]
    options["key"] = "register"
    return options


def decode(register: netguard.Register, data: bytes):
    """The value of a register from its raw bytes (two per word, high byte first) or, for a
    coil or discrete input, one bit (as a byte, 0 or 1)."""
    if register.type == "bool":
        return bool(data[0])
    if len(data) == 4 and register.word_order == "little":
        data = data[2:] + data[:2]
    kind = register.type
    if kind == "float32":
        value = struct.unpack(">f", data)[0]
    else:
        value = int.from_bytes(data, "big", signed=kind.startswith("int"))
    if register.scale != 1.0:
        value = value * register.scale
    return value if not isinstance(value, float) or math.isfinite(value) else None


def _recv(sock: socket.socket, n: int) -> bytes:
    chunks = []
    while n:
        chunk = sock.recv(n)
        if not chunk:
            raise ConnectionError("closed")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def _request(sock, conn: netguard.Connection, tid: int, register: netguard.Register) -> bytes:
    """The data bytes of one read. Whole or failed."""
    function = netguard.MODBUS_KINDS[register.kind]
    count = netguard.MODBUS_TYPES[register.type]
    sock.sendall(struct.pack(">HHHBBHH", tid, 0, 6, conn.unit, function, register.address, count))
    got_tid, protocol, length = struct.unpack(">HHH", _recv(sock, 6))
    if protocol != 0 or got_tid != tid or not 3 <= length <= MAX_PDU + 1:
        raise ValueError("a frame that is not the answer")      # judged before it is read
    body = _recv(sock, length)
    unit, got = body[0], body[1]
    if unit != conn.unit:
        raise ValueError("another unit answered")
    if got == function | 0x80:
        code = body[2]
        raise ReadError("E_DATA_READ", f"connection '{conn.name}': the device refused a read "
                        f"({_EXCEPTIONS.get(code, 'exception ' + str(code))})", _HINT)
    data = body[3:]
    wanted = 2 * count if function in (3, 4) else 1
    if got != function or body[2] != len(data) or len(data) != wanted:
        raise ValueError("an answer of another shape")
    return data


def read(source: dict, conn: netguard.Connection, options: dict) -> list[dict]:
    """One value per named register. Whole or failed: nothing partial is returned."""
    sock = netguard.open_connection(conn)
    expired = threading.Event()

    def abort():
        # The deadline of the WHOLE poll: shutting the socket wakes a blocked read, whatever
        # the other end does.
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(READ_TIMEOUT, abort)
    timer.daemon = True
    timer.start()
    try:
        sock.settimeout(READ_TIMEOUT)
        return [{"register": name, "value": decode(register, _request(sock, conn, tid, register))}
                for tid, (name, register) in enumerate(options["names"], 1)]
    except (OSError, ValueError, struct.error, IndexError) as exc:
        what = "gave no answer within the deadline" if expired.is_set() \
            else f"failed ({type(exc).__name__})"
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' {what}", _HINT) from None
    finally:
        timer.cancel()
        sock.close()
