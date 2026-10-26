"""The action outbox (PHASE7 7.15, D227, D229, D305, D306): the twin's ONE write outside its
result directory, and the replies read back.

    sink = outbox.open_sink("file:proposals.jsonl")     # the caller's start flag, nothing else
    sink.deliver(record)                                # one JSON line, appended, read back
    got = sink.changes(outbox.REPLIES, cursor)          # what was answered since, like a feed
    sink.reply(record_id, "taken", by="editor")         # a person's approval from the screen

* A SINK IS THE CALLER'S. `open_sink` is called with the `--outbox` flag of `naga twin` and
  with the field a person fills in the editor: no model, mapping, candidates file, tool
  argument or environment variable reaches it. Built: `file:<path>` and
  `mqtt:<connection>`, a connection of the caller's own file kept for an outbox (D408).
* A queue the caller owns, never plant controls: what is written is a proposal, as data.
* THE PATH is used as given and trusted for nothing: what is opened must be a regular file
  with one name and no link (opened without following one where the system can), a UNC
  path and a ':' after the drive (a stream inside another file) are refused before any
  file call, no folder is made, and both files stop at a ceiling. A link or a junction in
  a PARENT folder is the caller's own path and is followed.
* THE REPLIES FILE (`<path>.replies`) is the caller's and still hostile input: bounded a call
  and a line, parsed defensively, and a reply is only `{"id", "decision", "at", "by",
  "note"}`, its decision one of `DECISIONS`. Whether an id is ours is the ledger's question (`recommend.Advisor`).
* Two twins on one sink: each reads its line back from the file's tail, so a write another
  writer tore is a failed delivery and is tried again; an id is a content hash, so a
  consumer keeps the first.

Stdlib only. A file is opened, used and closed at every call: no handle is held between
two. The MQTT sink holds one subscription, to its replies, and a connection per delivery.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from nagasim import netguard
from nagasim.core.problems import problem
from nagasim.live import Changes, Event, stamp

__all__ = ["REPLIES", "DECISIONS", "LIMITS", "SinkError", "Full", "FileSink", "MqttSink",
           "open_sink", "parse_reply"]

REPLIES = "outbox-replies"              # the name the replies answer to, as a source
# What a reply may say: the three answers to a proposal, and that an action which was
# taken stands no more (D307).
DECISIONS = ("taken", "rejected", "expired", "ended")
LIMITS = {
    "record_bytes": 16 * 2 ** 10,       # one record, as its line
    "sink_bytes": 100 * 2 ** 20,        # the sink file, and the replies file
    "line_bytes": 4 * 2 ** 10,          # one reply
    "pull_bytes": 2 ** 20,              # of the replies file read in one call
    "text": 200,                        # characters of a reply's `by` and `note`
}
_ID = re.compile(r"[0-9a-f]{64}")
_SAID = ("NotAPlainFile", "HardLink", "ShortWrite", "NotWritten")    # this module's own classes
_SLACK = 64 * 2 ** 10                   # of the tail read back beyond the line itself


class SinkError(Exception):
    """A coded refusal of a sink, before anything is written."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = "E_OUTBOX_SINK", message, hint


class Full(OSError):
    """The file is at its ceiling: nothing more is written to it."""


def open_sink(spec, connections=None):
    """The sink the caller's *spec* names, or `SinkError`. *connections* is the caller's
    own connections file, loaded (`netguard.load_connections`): an `mqtt:` sink names one."""
    scheme, _, rest = str(spec).partition(":")
    if scheme == "mqtt" and rest:
        return MqttSink(rest, connections)
    if scheme != "file" or not rest:
        raise SinkError(f"'{str(spec)[:80]}' names no sink",
                        "an outbox is file:<path> or mqtt:<connection>")
    return FileSink(rest)


def _open(path, flags: int) -> int:
    """A descriptor on *path* when it is a regular file and no link; else OSError.

    Not `netguard.read_caller_file` (review S5): the sink is opened to append and create,
    and the replies file (up to `sink_bytes`) is read from a cursor a bounded chunk at a
    time, where that reader reads a whole file. Its rules are the same, plus one name only:
    a write through a hard link would change the file's other name too."""
    # No link is followed where the system can refuse one, and a pipe is not waited for.
    extra = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags | extra, 0o644)
    try:
        opened, named = os.fstat(fd), os.lstat(path)
        if not (stat.S_ISREG(opened.st_mode) and stat.S_ISREG(named.st_mode)
                and os.path.samestat(opened, named)):
            raise OSError("NotAPlainFile")
        if opened.st_nlink > 1:         # the same file under another name, somewhere else
            raise OSError("HardLink")
    except OSError:
        os.close(fd)
        raise
    return fd


def _append(path, line: bytes) -> None:
    """*line* as one more line of *path*, in one write, flushed and read back. Raises
    `Full` at the ceiling and OSError for anything else, the file then as it was or with a
    line a reader skips."""
    fd = _open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT)
    try:
        size = os.fstat(fd).st_size
        if size + len(line) + 1 > LIMITS["sink_bytes"]:
            raise Full("Full")
        if size:                        # on a line of its own, whatever was left there
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                line = b"\n" + line
        if os.write(fd, line) != len(line):
            raise OSError("ShortWrite")
        os.fsync(fd)
        end = os.fstat(fd).st_size
        os.lseek(fd, max(end - len(line) - _SLACK, 0), os.SEEK_SET)
        if line.strip() not in os.read(fd, len(line) + _SLACK).split(b"\n"):
            raise OSError("NotWritten")     # another writer's line went through it
    finally:
        os.close(fd)


def _text(value) -> str | None:
    """A reply's free text, cut and made printable: kept as data, shown to no model."""
    if not isinstance(value, str):
        return None
    return "".join(ch if ch.isprintable() else "?" for ch in value[:LIMITS["text"]])


def parse_reply(line: bytes) -> dict | None:
    """The reply in one line of a replies file, or None: `{"id", "decision", "at"
    (seconds since 1970, or None), "by", "note"}` and nothing else of what the line held."""
    try:
        doc = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not (isinstance(doc, dict) and isinstance(doc.get("id"), str) and _ID.fullmatch(doc["id"])
            and isinstance(doc.get("decision"), str) and doc["decision"] in DECISIONS):
        return None
    at = doc.get("at")
    return {"id": doc["id"], "decision": doc["decision"],
            "at": stamp(at) if isinstance(at, str) and len(at) <= 64 else None,
            "by": _text(doc.get("by")), "note": _text(doc.get("note"))}


class FileSink:
    """`file:<path>`: records appended to *path* as JSON lines, replies read from
    `<path>.replies` beside it."""

    kind = "file"

    def __init__(self, path):
        text = str(path)
        if netguard.is_unc_path(text):
            raise SinkError("the outbox is a UNC or device path",
                            "opening one is a network connection; use a local path")
        if ":" in os.path.splitdrive(text)[1]:
            raise SinkError("the outbox path holds a ':' after its drive",
                            "that names a stream inside another file, not a file")
        self.path = Path(os.path.abspath(text))
        self.replies = Path(str(self.path) + ".replies")
        if not self.path.parent.is_dir():
            raise SinkError("the folder of the outbox file does not exist",
                            "no folder is made: create it, or check the path")
        for target in (self.path, self.replies):
            if os.path.lexists(target) and not stat.S_ISREG(os.lstat(target).st_mode):
                raise SinkError(f"'{target.name[:80]}' is there and is not a plain file (a "
                                f"folder or a link)", "an outbox is a file of its own")
            if os.path.lexists(target) and os.lstat(target).st_nlink > 1:
                raise SinkError(f"'{target.name[:80]}' has another name elsewhere (a hard "
                                f"link)", "an outbox is a file of its own")
        self._seq = 0
        self._long = False              # inside a line over the bound, its end not yet read

    def deliver(self, record: dict) -> None:
        """One record, one line. Raises ValueError for a record that is no line under the
        bound (it never will be), `Full` at the ceiling, OSError (the class as its text)
        for a write that did not land."""
        line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8") + b"\n"
        if len(line) > LIMITS["record_bytes"]:
            raise ValueError("TooLarge")
        self._guarded(self.path, line)

    def reply(self, record_id: str, decision: str, by: str = "") -> None:
        """A person's answer, as one line of the reply channel (D310). The twin reads it
        back like any reply."""
        if not (isinstance(record_id, str) and _ID.fullmatch(record_id) and decision in DECISIONS):
            raise ValueError("a reply names a record's id and a decision")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._guarded(self.replies, json.dumps(
            {"id": record_id, "decision": decision, "at": now, "by": _text(by) or ""},
            sort_keys=True).encode("utf-8") + b"\n")

    @staticmethod
    def _guarded(path, line: bytes) -> None:
        try:
            _append(path, line)
        except Full:
            raise
        except OSError as exc:          # the class only: the text holds the path
            raise OSError(exc.args[0] if exc.args and exc.args[0] in _SAID
                          else type(exc).__name__) from None

    def changes(self, source: str = REPLIES, cursor: str | None = None,
                limit: int = 1000) -> Changes:
        """The replies after *cursor* (a byte offset, opaque to the caller), as a feed
        answers "what changed since" (D229): an event a reply, keyed by the record's id.
        `lost` counts the lines that were no reply; `gap` says the file shrank and was read
        again from its start. A missing file is no reply yet; one that cannot be read is
        "down", with why."""
        offset = int(cursor) if isinstance(cursor, str) and cursor.isdigit() else 0

        def answer(events=(), lost=0, gap=False, state="live", why=None):
            said = () if why is None else (problem(
                "W_OUTBOX", f"the replies file cannot be read ({why})", "/outbox",
                hint="it is read again at the next cycle; a reader must open, read and "
                     "close it"),)
            return Changes(tuple(events), str(offset), lost, gap, state, None, said)
        try:
            fd = _open(self.replies, os.O_RDONLY)
        except FileNotFoundError:
            return answer()
        except OSError as exc:
            return answer(state="down", why=exc.args[0] if exc.args and exc.args[0] in _SAID
                          else type(exc).__name__)
        try:
            size = os.fstat(fd).st_size
            if size > LIMITS["sink_bytes"]:
                return answer(state="down", why="Full")
            gap = offset > size
            if gap:
                offset, self._long = 0, False
            os.lseek(fd, offset, os.SEEK_SET)
            chunk = os.read(fd, LIMITS["pull_bytes"])
        except OSError as exc:
            return answer(state="down", why=type(exc).__name__)
        finally:
            os.close(fd)
        events, lost = [], 0
        while len(events) < limit:
            end = chunk.find(b"\n")
            if end < 0:
                # No whole line: it is still being written, or it is over the bound and is
                # dropped as it comes, its end included.
                if len(chunk) > LIMITS["line_bytes"]:
                    lost += not self._long
                    offset, self._long = offset + len(chunk), True
                break
            line, chunk, offset = chunk[:end], chunk[end + 1:], offset + end + 1
            if self._long:              # the end of a line already counted
                self._long = False
                continue
            reply = parse_reply(line) if len(line) <= LIMITS["line_bytes"] else None
            if reply is None:
                lost += bool(line.strip())
                continue
            self._seq += 1
            events.append(Event(source, reply["id"], reply, reply["at"], time.time(), self._seq))
        return answer(events, lost, gap)


# ---------------------------------------------------------------- the MQTT sink (D408-D410)
_ROOT_BYTES = 512
_DOWN = 1.0         # seconds after a failed delivery in which the others fail at once (D410)
_DEADLINE = 5.0     # seconds from the first connect to the PUBACK, one delivery (D410)


class MqttSink:
    """`mqtt:<connection>`: a record is one PUBLISH at QoS 1, not retained, to
    `<root>/proposals/<id>`; replies are read from the one topic `<root>/replies`. Both over ONE
    connection of the caller's file, `"use": "outbox"`, whose `root` is a plain topic.

    The topic is the caller's root, a fixed level and the record's id (64 hex characters):
    nothing of a record's text, a model or a plant reaches it. Each delivery is a
    connection of its own (CONNECT with a clean session and no will, PUBLISH, the PUBACK
    awaited, DISCONNECT), so nothing is left on the broker between two. The replies are a
    subscription of the reader's own code (`readers/mqtt.py`), in a feed of this sink's."""

    kind = "mqtt"

    def __init__(self, name: str, connections):
        import dataclasses

        from nagasim import live

        conn = (connections or {}).get(name)
        if conn is None:
            raise SinkError(f"no connection named '{name[:80]}' is defined",
                            "an mqtt outbox names a connection of your --connections file")
        if conn.kind != "mqtt" or conn.use != "outbox":
            raise SinkError(f"connection '{name[:80]}' is not an mqtt connection kept for an "
                            f"outbox", 'give it "kind": "mqtt" and "use": "outbox" in your '
                            'connections file; a connection a source reads is never a sink')
        root = conn.root
        try:
            plain = bool(root) and len(root.encode("utf-8")) <= _ROOT_BYTES
        except UnicodeEncodeError:      # a lone surrogate
            plain = False
        if not plain or root.startswith("$") \
                or set(root) & {"+", "#", "\x00"} or "" in root.split("/"):
            raise SinkError(f"connection '{name[:80]}': an outbox's \"root\" is a plain topic",
                            "such as plant/line1/naga: no + or #, no leading $ or /, at most "
                            f"{_ROOT_BYTES} bytes")
        try:                            # a mistake of the file is refused at start (D410)
            netguard.vet_connection(conn)
        except netguard.NetworkRefused as exc:
            raise SinkError(f"connection '{name[:80]}' is refused: {exc.code}",
                            getattr(exc, "hint", None)) from None
        self.conn, self.name, self.root = conn, conn.name, root
        self._down_until, self._why = 0.0, "Down"
        self._seq = 0
        # The reader's subscription, confined to the replies: ONE topic, the id is in the
        # payload, so the feed's limit on keys can never silence a later reply (D409).
        replies = f"{root}/replies"
        self._feed = live.Feed(
            [{"name": REPLIES, "kind": "mqtt", "connection": conn.name, "location": replies,
              "options": {"payload": "text", "qos": 1}}],
            network=netguard.NetworkPolicy(connections={conn.name: dataclasses.replace(
                conn, use="read", root=replies)}))

    def topic(self, record_id: str) -> str:
        return f"{self.root}/proposals/{record_id}"

    def deliver(self, record: dict) -> None:
        """One record, one PUBLISH, acknowledged. Raises ValueError for a record that is no
        payload under the bound (it never will be), OSError (a code or a class as its text)
        for one the broker did not acknowledge."""
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        ident = record.get("id")
        if len(payload) > LIMITS["record_bytes"]:
            raise ValueError("TooLarge")
        if not (isinstance(ident, str) and _ID.fullmatch(ident)):
            raise ValueError("NoId")
        if time.monotonic() < self._down_until:
            raise OSError(self._why)
        try:
            self._publish(self.topic(ident), payload)
        except Exception as exc:        # a code or the class only: the text may be the broker's
            self._down_until = time.monotonic() + _DOWN
            self._why = str(getattr(exc, "code", "") or type(exc).__name__)[:40]
            raise OSError(self._why) from None

    def _publish(self, topic: str, payload: bytes) -> None:
        import secrets
        import struct
        import threading

        from nagasim.readers import mqtt

        # One deadline for the whole delivery: the link's stop event is set when it passes,
        # and the handshake waits (`expect`) end within a socket timeout of it.
        stop = threading.Event()
        timer = threading.Timer(_DEADLINE, stop.set)
        timer.daemon = True
        timer.start()
        sock = None
        try:
            sock = netguard.open_connection(self.conn, timeout=_DEADLINE)
            sock.settimeout(0.5)
            link = mqtt._Link(sock, self.conn.limit("packet"), stop, self.name)
            sock.sendall(mqtt._connect_packet("naga-" + secrets.token_hex(8), self.conn))
            answer = link.expect(mqtt._CONNACK, "CONNACK")
            if len(answer) != 2 or answer[1] != 0:
                raise link._bad("the broker refused the connection")
            # The one packet the reader cannot build: QoS 1, not retained, not a duplicate.
            sock.sendall(mqtt._framed(mqtt._PUBLISH << 4 | 0x02, mqtt._text(topic)
                                      + struct.pack(">H", 1) + payload))
            if link.expect(mqtt._PUBACK, "PUBACK") != b"\x00\x01":
                raise link._bad("the broker acknowledged another packet")
        finally:
            timer.cancel()
            if sock is not None:
                try:
                    sock.sendall(bytes([mqtt._DISCONNECT << 4, 0]))
                except OSError:
                    pass
                sock.close()

    def changes(self, source: str = REPLIES, cursor: str | None = None,
                limit: int = 1000) -> Changes:
        """The replies after *cursor*, as the file sink answers them: an event a reply,
        keyed by the record's id; `lost` counts the messages that were no reply."""
        try:
            got = self._feed.changes(REPLIES, cursor, limit)
        except Exception as exc:        # a refusal before any socket: its code or class
            return Changes((), cursor or "", 0, False, "down", None, (problem(
                "W_OUTBOX", f"the replies cannot be read "
                f"({str(getattr(exc, 'code', '') or type(exc).__name__)[:40]})", "/outbox"),))
        events, lost = [], got.lost
        for event in got.events:
            reply = parse_reply(event.value.encode("utf-8")) if isinstance(event.value, str) \
                and len(event.value) <= LIMITS["line_bytes"] else None
            if reply is None:
                lost += 1
                continue
            self._seq += 1
            events.append(Event(source, reply["id"], reply, reply["at"], event.received, self._seq))
        return Changes(tuple(events), got.cursor, lost, got.gap, got.state, got.age, got.problems)

    def close(self) -> None:
        self._feed.close()
