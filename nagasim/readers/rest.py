"""REST reader (PHASE7 7.8, D221): one GET per read, over the caller's connection.

    {"name": "orders", "kind": "rest", "connection": "mes", "location": "orders?open=1",
     "options": {"records": "items", "key": "id", "time": "updated", "poll": 5}}

The answer must be a JSON array of objects, or an object holding one under `records`. Each
object is a row; a cell that is not a scalar is left out. `key` and `time` name the columns
the feed diffs and stamps events by; `poll` is the least number of seconds between reads.

What this reader cannot do is not in the code: the only verb is GET, a redirect is an
error and never followed, no cookie is kept, no proxy is used, nothing is decompressed,
and the body is never quoted in a message. Stdlib only: `http.client` speaks over the
socket `netguard.open_connection` pinned, so nothing here resolves a name.
"""
from __future__ import annotations

import base64
import http.client
import json
import math
import re
import socket
import threading
from urllib.parse import unquote

from nagasim import netguard, readers
from nagasim.sources import ReadError

__all__ = ["OPTIONS", "check", "read"]

READ_TIMEOUT = 20.0         # the whole request: a deadline, so a dripping server holds no cycle
# name: (default, valid), as `readers.checked_options` reads them
OPTIONS = {"records": (None, str), "key": (None, str), "time": (None, str),
           "poll": (5.0, (1.0, 86_400.0))}
_SCALARS = (bool, int, float, str, type(None))
MAX_ROW_BYTES = 262_144     # characters of one row; more is refused before it is decoded
_HINT = "check the connection's address and the source's location"


def check(source: dict, conn: netguard.Connection) -> dict:
    """The source's options and its request target, or the refusal. No socket is opened."""
    options = readers.checked_options(source, OPTIONS)
    options["target"] = _target(source, conn)
    return options


def _target(source: dict, conn: netguard.Connection) -> str:
    """The path and query to GET. A location is a path under the connection's own: it may
    name no scheme, host, user, fragment or parent folder, however it is encoded, and the
    result must stay under the connection's `root`."""
    location = str(source.get("location") or "")
    # Split ONCE, as the server will: what follows the first "?" is the query, and a "?" that
    # only appears when the path is decoded ("%3F") must not end the judging of the path.
    raw_path, mark, query = location.partition("?")
    bad = any(ord(c) <= 32 or ord(c) == 127 for c in location)
    seen, text = raw_path, None
    while text != seen and not bad:     # "%252e" is "%2e" is ".": judge every decoding, whole
        text, seen = seen, unquote(seen)
        bad = "://" in text or text.startswith("//") or "\\" in text or "@" in text \
            or "#" in text or "?" in text \
            or any(segment.startswith("..") for segment in text.split("/"))   # "..", "..;x"
    seen, text = query, None
    while text != seen and not bad:
        text, seen = seen, unquote(seen)
        bad = "://" in text or "\\" in text or "#" in text
    if bad:
        raise ReadError("E_DATA_SOURCE_LOCATION",
                        f"source '{source.get('name')}': a rest location is a path under "
                        f"its connection, and this one names something else",
                        "write it as 'orders' or 'orders?open=1'; the host is the "
                        "connection's")
    full = f"{conn.path}/{raw_path.lstrip('/')}"
    allowed = conn.path + ("/" + conn.root.strip("/") if conn.root.strip("/") else "")
    if full != allowed and not full.startswith(allowed + "/"):
        raise ReadError("E_DATA_SOURCE_LOCATION",
                        f"source '{source.get('name')}': its location is outside what "
                        f"connection '{conn.name}' allows",
                        "the connection's \"root\" is set by the human who owns the "
                        "connections file")
    return full + mark + query


def _credential(conn: netguard.Connection) -> dict:
    """The one header the credential travels in; this is the only place it is revealed."""
    if conn.auth == "bearer":
        return {"Authorization": "Bearer " + conn.secret.reveal()}
    if conn.auth == "basic":
        pair = f"{conn.username}:{conn.secret.reveal()}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    if conn.auth == "header":
        return {conn.header: conn.secret.reveal()}
    return {}


def read(source: dict, conn: netguard.Connection, options: dict) -> list[dict]:
    """The rows of one GET. Whole or failed: nothing partial is returned."""
    sock = netguard.open_connection(conn)
    expired = threading.Event()

    def abort():
        # The deadline of the WHOLE request, headers included: waking a blocked read by
        # shutting the socket down is the one way that does not depend on how slowly the
        # other end drips.
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
        # The class only decides how the Host header is written (no ":443" over https); the
        # socket is the pinned one, already connected and wrapped, so http.client dials nothing.
        kind = http.client.HTTPSConnection if conn.scheme == "https" else http.client.HTTPConnection
        link = kind(conn.host, conn.port)
        link.sock = sock
        link.request("GET", options["target"], headers={
            "Accept": "application/json", "Accept-Encoding": "identity",
            "User-Agent": "naga", "Connection": "close", **_credential(conn)})
        response = link.getresponse()
        if 300 <= response.status < 400:
            raise ReadError("E_DATA_URL_REDIRECT",
                            f"connection '{conn.name}' answered with a redirect (HTTP "
                            f"{response.status}), which is never followed",
                            "point the connection at the final address", response.status)
        if response.status != 200:
            raise ReadError("E_DATA_READ", f"HTTP {response.status} from connection "
                            f"'{conn.name}'", _HINT, response.status)
        body = _body(response, conn)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        what = "gave no answer within the deadline" if expired.is_set() \
            else f"failed ({type(exc).__name__})"
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' {what}", _HINT) from None
    finally:
        timer.cancel()
        sock.close()
    if expired.is_set():
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' gave no answer within the "
                        f"deadline", _HINT)
    return _rows(body, source, conn, options)


def _body(response, conn: netguard.Connection) -> bytes:
    """The body, never more than the connection's byte limit of it in memory."""
    limit = conn.limit("bytes")
    declared = response.getheader("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ReadError("E_DATA_TOO_LARGE", f"connection '{conn.name}' declares {declared} "
                        f"bytes, over the {limit} byte cap", "ask the source for a smaller extract")
    chunks, total = [], 0
    while True:
        chunk = response.read1(65_536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ReadError("E_DATA_TOO_LARGE", f"connection '{conn.name}' sent more than the "
                            f"{limit} byte cap", "ask the source for a smaller extract")
        chunks.append(chunk)


_MARK = re.compile(r'["\[\]{}]')
_SCALAR_END = re.compile(r"[,\]}\s]")
_SPACE = re.compile(r"\s*")
_STRING = json.decoder.scanstring


def _skip(text: str, pos: int) -> int:
    """The index after the JSON value that starts at *pos*, found WITHOUT building it: a
    scan that knows strings and counts brackets, so its work is the value's length and its
    memory nothing. ValueError when the value does not end."""
    if text[pos:pos + 1] not in ('"', "[", "{"):
        end = _SCALAR_END.search(text, pos)
        end = end.start() if end else len(text)
        json.loads(text[pos:end])           # a number, true, false or null, or it is not JSON
        return end
    depth = 0
    while True:
        found = _MARK.search(text, pos)
        if found is None:
            raise ValueError("a value that does not end")
        pos = found.start()
        if found.group() == '"':
            _text, pos = _STRING(text, pos + 1)
        else:
            depth += 1 if found.group() in "[{" else -1
            pos += 1
        if depth == 0:
            return pos
        if depth < 0:
            raise ValueError("a bracket too many")


def _rows(body: bytes, source: dict, conn: netguard.Connection, options: dict) -> list[dict]:
    """The rows of an answer, decoded ONE AT A TIME up to the row limit.

    Parsing the body whole and counting afterwards lets a server choose our memory: 20 MB of
    `{},` is millions of objects. Here nothing is built but a row at a time; a value the
    reader does not want (the other fields of a wrapping object) is skipped by a scan; a row
    is judged by its size before it is decoded; and the first row over the limit ends the
    read."""
    def space(at: int) -> int:
        return _SPACE.match(text, at).end()

    not_rows = ReadError("E_DATA_READ",
                         f"source '{source.get('name')}': the answer is not a JSON array of "
                         f"objects", "name the array's field with the \"records\" option")
    limit, rows = conn.limit("rows"), []
    try:
        text = body.decode("utf-8")
        pos = space(0)
        if options.get("records"):
            # {"<any>": <skipped>, ..., "<records>": [ ... the rows ...
            if text[pos:pos + 1] != "{":
                _skip(text, pos)
                raise not_rows
            pos = space(pos + 1)
            while True:
                if text[pos:pos + 1] != '"':
                    if text[pos:pos + 1] == "}":
                        raise not_rows
                    raise ValueError("a key was expected")
                key, pos = _STRING(text, pos + 1)
                pos = space(pos)
                if text[pos:pos + 1] != ":":
                    raise ValueError("a colon was expected")
                pos = space(pos + 1)
                if key == options["records"]:
                    break
                pos = space(_skip(text, pos))
                if text[pos:pos + 1] == "}":
                    raise not_rows
                if text[pos:pos + 1] != ",":
                    raise ValueError("a comma was expected")
                pos = space(pos + 1)
        if text[pos:pos + 1] != "[":
            _skip(text, pos)
            raise not_rows
        pos = space(pos + 1)
        while text[pos:pos + 1] != "]" or rows:
            end = _skip(text, pos)
            if text[pos] != "{":
                raise not_rows
            if end - pos > MAX_ROW_BYTES:
                raise ReadError("E_DATA_TOO_LARGE", f"connection '{conn.name}' sent one row of "
                                f"more than {MAX_ROW_BYTES} characters",
                                "ask the source for flatter rows")
            if len(rows) >= limit:
                raise ReadError("E_DATA_TOO_LARGE", f"connection '{conn.name}' sent more rows "
                                f"than the {limit} row cap", "ask the source for a smaller extract")
            row = json.loads(text[pos:end])
            # Data, not code: a row keeps its scalars and nothing else. NaN and the infinities
            # are valid to this JSON reader and to nothing after it, so they are left out too.
            rows.append({str(key): value for key, value in row.items() if isinstance(value, _SCALARS)
                         and not (isinstance(value, float) and not math.isfinite(value))})
            pos = space(end)
            if text[pos:pos + 1] == "]":
                break
            if text[pos:pos + 1] != ",":
                raise ValueError("a comma was expected")
            pos = space(pos + 1)
            if text[pos:pos + 1] == "]":
                raise ValueError("a row was expected")
        if not options.get("records") and space(pos + 1) != len(text):
            raise ValueError("something follows the array")
    except (ValueError, RecursionError, IndexError):
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' did not answer with JSON",
                        _HINT) from None
    return rows
