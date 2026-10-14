"""MQTT reader (PHASE7 7.8, D222): a subscribe-only MQTT 3.1.1 client, ours.

    {"name": "line", "kind": "mqtt", "connection": "line",
     "location": "plant/line1/+/speed\\nspBv1.0/Plant1/#",
     "options": {"payload": "json", "time": "ts", "qos": 1}}

`location` holds topic filters, one per line, each inside the connection's `root`.
`payload` says what a message holds:

* `json`: a scalar is the topic's value; an object gives one key per scalar field
  (`<topic>/<field>`), stamped by the field `time` names;
* `text`: the payload as text;
* `sparkplug_b`: Sparkplug B births, data and deaths with scalar metrics. A key is
  `<group>/<node>[/<device>]/<metric>`; a birth sets `<group>/<node>[/<device>]` to True
  and a death to False. A metric sent by alias before its birth was seen is dropped and
  counted (`unresolved`) until the node's next birth: asking for a rebirth would be a
  command to the plant, and it is never sent.

Why not a library: one would resolve the broker's name itself, read a packet of any
declared size whole, and carry a publish call. Here the socket is the one
`netguard.open_connection` pinned, a packet over the limit is passed over without being
held, and the packets this client can build are CONNECT, SUBSCRIBE, PUBACK, PINGREQ and
DISCONNECT: there is no PUBLISH encoder, no will and no persistent session, so a reader
leaves nothing on a plant's broker. The one PUBLISH is the outbox's (`outbox.MqttSink`, D408). Not supported: QoS 2, MQTT 5, WebSockets, `$` topics.
"""
from __future__ import annotations

import json
import secrets
import struct
import time

from nagasim import netguard, readers
from nagasim.live import stamp
from nagasim.readers import sparkplug
from nagasim.sources import ReadError

__all__ = ["OPTIONS", "check", "subscribe"]

OPTIONS = {"payload": ("json", frozenset({"json", "text", "sparkplug_b"})), "time": (None, str),
           "qos": (0, frozenset({0, 1})), "settle": (2.0, (0.0, 10.0))}
KEEPALIVE = 60              # seconds; no packet for 1.5 of them is a dead link
PACKET_TIMEOUT = 10.0       # a packet that started must finish within this
MAX_FILTERS, MAX_FILTER_BYTES = 100, 1024
OWNER_SHARE = 16            # a Sparkplug node or device keeps at most 1/16 of the `keys` limit in aliases
_CONNECT, _CONNACK, _PUBLISH, _PUBACK, _SUBSCRIBE, _SUBACK = 1, 2, 3, 4, 8, 9
_PINGREQ, _PINGRESP, _DISCONNECT = 12, 13, 14
_SCALARS = (bool, int, float, str, type(None))
_HINT = "check the connection and what the broker allows this client to read"


# ---------------------------------------------------------------- before any socket
def _inside(topic_filter: str, root: str) -> bool:
    """Whether everything *topic_filter* matches, *root* matches too."""
    asked, allowed = topic_filter.split("/"), root.split("/")
    for index, level in enumerate(allowed):
        if level == "#":
            return True
        if index >= len(asked) or asked[index] == "#":
            return False
        if level != "+" and asked[index] != level:
            return False
    return len(asked) == len(allowed)


def check(source: dict, conn: netguard.Connection) -> dict:
    """The source's options and its topic filters, or the refusal. No socket is opened."""
    options = readers.checked_options(source, OPTIONS)
    filters = [line.strip() for line in str(source.get("location") or "").splitlines()
               if line.strip()]
    for topic_filter in filters or [""]:
        levels = topic_filter.split("/")
        well_formed = topic_filter and "\x00" not in topic_filter \
            and len(topic_filter.encode("utf-8", "replace")) <= MAX_FILTER_BYTES \
            and all(level in ("+", "#") or not set(level) & {"+", "#"} for level in levels) \
            and "#" not in levels[:-1]
        if not well_formed or len(filters) > MAX_FILTERS or topic_filter.startswith("$") \
                or not _inside(topic_filter, conn.root or "#"):
            raise ReadError("E_DATA_SOURCE_LOCATION",
                            f"source '{source.get('name')}': its location must be topic "
                            f"filters, one per line, inside what connection '{conn.name}' "
                            f"allows",
                            "a filter may not start with '$'; the connection's \"root\" is set "
                            "by the human who owns the connections file")
    options["filters"] = filters
    return options


# ---------------------------------------------------------------- the five packets we send
def _text(value: str) -> bytes:
    data = value.encode("utf-8")
    return struct.pack(">H", len(data)) + data


def _framed(kind_and_flags: int, body: bytes) -> bytes:
    length, out = len(body), bytearray([kind_and_flags])
    if length >= 268_435_456:           # MQTT's remaining length has four bytes at most
        raise ValueError("a packet of 256 MB or more")
    while True:
        length, low = divmod(length, 128)
        out.append(low | (0x80 if length else 0))
        if not length:
            return bytes(out) + body


def _connect_packet(client_id: str, conn: netguard.Connection) -> bytes:
    """CONNECT: protocol level 4, a clean session, no will. This is the one place the
    connection's password is revealed."""
    flags, payload = 0x02, _text(client_id)
    if conn.username:
        flags |= 0x80
        payload += _text(conn.username)
        if conn.secret is not None:
            flags |= 0x40
            payload += _text(conn.secret.reveal())
    return _framed(_CONNECT << 4, _text("MQTT") + bytes([4, flags]) + struct.pack(">H", KEEPALIVE)
                   + payload)


def _subscribe_packet(packet_id: int, filters: list[str], qos: int) -> bytes:
    return _framed(_SUBSCRIBE << 4 | 0x02, struct.pack(">H", packet_id)
                   + b"".join(_text(f) + bytes([qos]) for f in filters))


# ---------------------------------------------------------------- reading packets
class _Link:
    """Packets off one socket, never holding more than the limit of one."""

    def __init__(self, sock, limit: int, stop, name: str):
        self.sock, self.limit, self.stop, self.name = sock, limit, stop, name
        self.buffer = b""
        self.started, self.allowance = 0.0, PACKET_TIMEOUT

    def _bad(self, what: str) -> ReadError:
        return ReadError("E_DATA_READ", f"connection '{self.name}': {what}", _HINT)

    def _fill(self, size: int, idle_ok: bool = False) -> bool:
        """Until the buffer holds *size* bytes. False (only with *idle_ok*): the link is
        idle, nothing of a packet has arrived."""
        while len(self.buffer) < size:
            # A packet is under way (or must be): its deadline and the stop are judged before
            # EVERY read, not only when a read times out, so bytes that keep dripping in
            # cannot hold the link, the dead-link check or `close`.
            if (self.buffer or not idle_ok) and (
                    self.stop.is_set() or time.monotonic() - self.started > self.allowance):
                raise self._bad("a packet stopped half way")
            try:
                chunk = self.sock.recv(65_536)
            except TimeoutError:
                if idle_ok and not self.buffer:
                    return False
                continue
            if not chunk:
                raise self._bad("the broker closed the link")
            self.buffer += chunk
        return True

    def packet(self):
        """(type, flags, body) of the next packet; None when the link is idle; a body of
        None for a packet over the limit, which was passed over without being held."""
        self.started, self.allowance = time.monotonic(), PACKET_TIMEOUT
        if not self._fill(1, idle_ok=True):
            return None
        self.started = time.monotonic()     # the packet's own clock starts with its first byte
        length = shift = 0
        index = 1
        while True:
            self._fill(index + 1)
            byte = self.buffer[index]
            index += 1
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if index > 4:
                raise self._bad("a malformed packet length")
        first, self.buffer = self.buffer[0], self.buffer[index:]
        if length > self.limit:
            # Passing over a large packet honestly takes longer than reading a small one: one
            # more allowance per limit's worth, never past the dead-link time.
            self.allowance = min(1.5 * KEEPALIVE, PACKET_TIMEOUT * (1 + length // self.limit))
            left = length - len(self.buffer)
            self.buffer = self.buffer[length:]
            while left > 0:             # dropped by its declared size, before it is read
                self._fill(1)
                left, self.buffer = left - len(self.buffer), self.buffer[left:]
            return first >> 4, first & 0x0F, None
        self._fill(length)
        body, self.buffer = self.buffer[:length], self.buffer[length:]
        return first >> 4, first & 0x0F, body

    def expect(self, kind: int, what: str) -> bytes:
        """The body of the handshake packet that must come next."""
        began = time.monotonic()
        while True:
            got = self.packet()
            if got is None:
                if self.stop.is_set() or time.monotonic() - began > PACKET_TIMEOUT:
                    raise self._bad(f"no {what} from the broker")
                continue
            if got[0] != kind or got[2] is None:
                raise self._bad(f"the broker sent something else than {what}")
            return got[2]


# ---------------------------------------------------------------- the subscription
def subscribe(source: dict, conn: netguard.Connection, options: dict, sink, stop) -> None:
    """Hold one subscription until *stop* is set or the link ends (then raise or return:
    the feed reconnects, with a clean session, and counts the gap)."""
    sock = netguard.open_connection(conn)
    try:
        sock.settimeout(0.5)            # how soon *stop* is seen
        link = _Link(sock, conn.limit("packet"), stop, conn.name)
        sock.sendall(_connect_packet("naga-" + secrets.token_hex(8), conn))
        answer = link.expect(_CONNACK, "CONNACK")
        if len(answer) != 2 or answer[1] != 0:
            raise link._bad(f"the broker refused the connection (code {answer[-1:].hex()})")
        sock.sendall(_subscribe_packet(1, options["filters"], options["qos"]))
        granted = link.expect(_SUBACK, "SUBACK")[2:]
        if len(granted) != len(options["filters"]) or 0x80 in granted:
            raise link._bad("the broker refused a subscription")
        sink.connected()
        state: dict = {"aliases": {}, "seq": {}, "limit": conn.limit("keys")}
        last_in = last_out = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if now - last_out >= KEEPALIVE / 2:
                sock.sendall(bytes([_PINGREQ << 4, 0]))
                last_out = now
            if now - last_in > 1.5 * KEEPALIVE:
                raise link._bad("the link went silent")
            got = link.packet()
            if got is None:
                continue
            last_in = time.monotonic()
            kind, flags, body = got
            if body is None:
                sink.count("oversize")
            elif kind == _PUBLISH:
                _publish(sock, link, flags, body, options, sink, state)
            elif kind != _PINGRESP:
                raise link._bad(f"an unexpected packet (type {kind})")
    finally:
        try:
            sock.sendall(bytes([_DISCONNECT << 4, 0]))
        except OSError:
            pass
        sock.close()


def _publish(sock, link: _Link, flags: int, body: bytes, options: dict, sink, state) -> None:
    """One PUBLISH from the broker: acknowledge it (QoS 1) and hand its values on."""
    if len(body) < 2 or 2 + struct.unpack(">H", body[:2])[0] > len(body):
        raise link._bad("a malformed PUBLISH")
    size = struct.unpack(">H", body[:2])[0]
    topic, pos, qos = body[2:2 + size].decode("utf-8", "replace"), 2 + size, (flags >> 1) & 3
    if qos:
        if qos != 1 or pos + 2 > len(body):
            raise link._bad("a PUBLISH at a quality of service that was not asked for")
        sock.sendall(bytes([_PUBACK << 4, 2]) + body[pos:pos + 2])
        pos += 2
    deliver(topic, body[pos:], options, sink, state)


def deliver(topic: str, payload: bytes, options: dict, sink, state: dict) -> None:
    """What one message says, as `sink.accept` calls. Never raises: a message that is not
    what `payload` promised is dropped and counted, and the link stays up."""
    if topic.startswith("$") or not topic:
        return sink.count("topic")
    kind = options["payload"]
    try:
        if kind == "text":
            return sink.accept(topic, payload.decode("utf-8"), None)
        if kind == "json":
            value = json.loads(payload.decode("utf-8"))
            if isinstance(value, dict):
                moment = stamp(value.get(options["time"])) if options.get("time") else None
                for name, cell in value.items():
                    if isinstance(cell, _SCALARS) and name != options.get("time"):
                        sink.accept(f"{topic}/{name}", cell, moment)
                return None
            if isinstance(value, _SCALARS):
                return sink.accept(topic, value, None)
            return sink.count("payload")
        return _sparkplug(topic, payload, sink, state)
    except (ValueError, RecursionError, ReadError):
        return sink.count("payload")


def _sparkplug(topic: str, payload: bytes, sink, state: dict) -> None:
    parts = topic.split("/")
    if len(parts) not in (4, 5) or parts[0] != "spBv1.0" or parts[2] not in (
            "NBIRTH", "DBIRTH", "NDATA", "DDATA", "NDEATH", "DDEATH"):
        return sink.count("topic")      # STATE, a command, another namespace: not for us
    message, node = parts[2], f"{parts[1]}/{parts[3]}"
    owner = node + (f"/{parts[4]}" if len(parts) == 5 else "")
    timestamp, seq, metrics = sparkplug.decode(payload)
    moment = timestamp / 1000.0 if timestamp is not None else None
    # One sequence per edge node, 0 to 255, restarting at its birth: a jump is a hole.
    last = state["seq"].get(node)
    if message != "NBIRTH" and message != "NDEATH" and seq is not None and last is not None \
            and seq != (last + 1) % 256:
        sink.gap()
    # What is remembered per node lives as long as the link: it is bounded by the
    # connection's `keys` limit, like the keys of the feed itself.
    limit = state.get("limit", netguard.LIMITS["keys"][0])
    if seq is not None and message != "NDEATH" and (node in state["seq"] or len(state["seq"]) < limit):
        state["seq"][node] = seq
    if message.endswith("DEATH"):
        if message == "NDEATH":
            for known in [key for key in state["aliases"] if key == node
                          or key.startswith(node + "/")]:
                del state["aliases"][known]
            state["seq"].pop(node, None)
        else:
            state["aliases"].pop(owner, None)
        return sink.accept(owner, False, moment)
    if message.endswith("BIRTH"):
        born = {m.alias: (m.name, m.datatype) for m in metrics if m.alias is not None and m.name}
        state["aliases"].pop(owner, None)
        room = max(0, limit - sum(len(held) for held in state["aliases"].values())) \
            if len(state["aliases"]) < limit else 0
        # One owner keeps at most a sixteenth of the budget: a single birth of thousands of
        # aliases cannot leave every device born after it with none.
        room = min(room, max(1, limit // OWNER_SHARE))
        if len(born) > room:
            sink.count("keys", len(born) - room)        # over the limit: counted, not kept
            born = dict(list(born.items())[:room])
        if born:
            state["aliases"][owner] = born
        sink.accept(owner, True, moment)
    known = state["aliases"].get(owner, {})
    for metric in metrics:
        name, datatype = metric.name, metric.datatype
        if not name and metric.alias in known:
            name, born_as = known[metric.alias]
            datatype = datatype if datatype is not None else born_as
        if not name:
            sink.count("unresolved")
        elif metric.skipped:
            sink.count("type")
        else:
            sink.accept(f"{owner}/{name}", sparkplug.signed(metric.value, datatype),
                        metric.timestamp / 1000.0 if metric.timestamp is not None else moment)
    return None
