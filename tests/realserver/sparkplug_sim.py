"""A Sparkplug B edge node, as a third party would write one: for the realserver tests and
the hour-long twin run. It shares no code with nagasim.

    python sparkplug_sim.py --port 1883 burst      # births, data, a death; then exit
    python sparkplug_sim.py --port 1883 line       # a single-server line, live, until killed

The payloads are built by the `protobuf` package from the official schema
(eclipse/tahu sparkplug_b.proto, field numbers below), and values are set as Eclipse Tahu's
Python edge library sets them (a negative Int8 is `value + 2**8`, and so on). The MQTT
client is a few lines of raw socket: CONNECT with the NDEATH as its will, QoS 0 PUBLISH.
Binds nothing; connects to 127.0.0.1 only.
"""
import argparse
import random
import socket
import struct
import sys
import time

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

GROUP, NODE, DEVICE = "Plant1", "Edge1", "Line1"
# Sparkplug B datatypes (spec 3.0, section 6.4.16)
INT8, INT16, INT32, INT64, UINT32, FLOAT, DOUBLE, BOOLEAN, STRING, BYTES = 1, 2, 3, 4, 7, 9, 10, 11, 12, 17


def _schema():
    """Payload and Payload.Metric of sparkplug_b.proto (proto2), the scalar part."""
    F = descriptor_pb2.FieldDescriptorProto
    proto = descriptor_pb2.FileDescriptorProto(name="sparkplug_b.proto", syntax="proto2",
                                               package="org.eclipse.tahu.protobuf")
    payload = proto.message_type.add(name="Payload")
    metric = payload.nested_type.add(name="Metric")
    metric.oneof_decl.add(name="value")
    for name, number, kind, in_value in (
            ("name", 1, F.TYPE_STRING, False), ("alias", 2, F.TYPE_UINT64, False),
            ("timestamp", 3, F.TYPE_UINT64, False), ("datatype", 4, F.TYPE_UINT32, False),
            ("is_historical", 5, F.TYPE_BOOL, False), ("is_transient", 6, F.TYPE_BOOL, False),
            ("is_null", 7, F.TYPE_BOOL, False),
            ("int_value", 10, F.TYPE_UINT32, True), ("long_value", 11, F.TYPE_UINT64, True),
            ("float_value", 12, F.TYPE_FLOAT, True), ("double_value", 13, F.TYPE_DOUBLE, True),
            ("boolean_value", 14, F.TYPE_BOOL, True), ("string_value", 15, F.TYPE_STRING, True),
            ("bytes_value", 16, F.TYPE_BYTES, True)):
        field = metric.field.add(name=name, number=number, type=kind, label=F.LABEL_OPTIONAL)
        if in_value:
            field.oneof_index = 0
    for name, number, kind in (("timestamp", 1, F.TYPE_UINT64), ("seq", 3, F.TYPE_UINT64),
                               ("uuid", 4, F.TYPE_STRING), ("body", 5, F.TYPE_BYTES)):
        payload.field.add(name=name, number=number, type=kind, label=F.LABEL_OPTIONAL)
    payload.field.add(name="metrics", number=2, type=F.TYPE_MESSAGE, label=F.LABEL_REPEATED,
                      type_name=".org.eclipse.tahu.protobuf.Payload.Metric")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(proto)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("org.eclipse.tahu.protobuf.Payload"))


Payload = _schema()


def now_ms() -> int:
    return int(round(time.time() * 1000))


def add_metric(payload, name, alias, datatype, value, timestamp=None):
    """As Tahu's `addMetric`: name and/or alias, datatype, and the value in its field."""
    metric = payload.metrics.add()
    if name is not None:
        metric.name = name
    if alias is not None:
        metric.alias = alias
    metric.timestamp = timestamp if timestamp is not None else now_ms()
    metric.datatype = datatype
    if value is None:
        metric.is_null = True
    elif datatype in (INT8, INT16, INT32):
        metric.int_value = value + 2 ** {INT8: 8, INT16: 16, INT32: 32}[datatype] if value < 0 else value
    elif datatype == INT64:
        metric.long_value = value + 2 ** 64 if value < 0 else value
    elif datatype == UINT32:
        metric.int_value = value
    elif datatype == FLOAT:
        metric.float_value = value
    elif datatype == DOUBLE:
        metric.double_value = value
    elif datatype == BOOLEAN:
        metric.boolean_value = value
    elif datatype == STRING:
        metric.string_value = value
    elif datatype == BYTES:
        metric.bytes_value = value
    return metric


# ---------------------------------------------------------------- a minimal MQTT 3.1.1 publisher
def _str(text) -> bytes:
    data = text.encode() if isinstance(text, str) else text
    return struct.pack(">H", len(data)) + data


def _packet(first: int, body: bytes) -> bytes:
    out, n = bytearray([first]), len(body)
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out) + body


class Edge:
    def __init__(self, port: int, bd_seq: int = 0):
        self.seq, self.bd_seq = 0, bd_seq
        death = Payload(timestamp=now_ms())
        add_metric(death, "bdSeq", None, INT64, bd_seq)
        will_topic = f"spBv1.0/{GROUP}/NDEATH/{NODE}"
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        # clean session, will QoS 1 (as the spec asks for NDEATH), keepalive 60
        flags = 0x02 | 0x04 | 0x08
        self.sock.sendall(_packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
                                  + _str(f"sim-{NODE}-{random.getrandbits(32):08x}")
                                  + _str(will_topic) + _str(death.SerializeToString())))
        connack = self.sock.recv(4)
        if connack[:1] != b"\x20" or connack[3:4] != b"\x00":
            sys.exit(f"CONNACK refused: {connack.hex()}")
        self.last = time.monotonic()

    def publish(self, kind: str, payload, device: str | None = None) -> None:
        topic = f"spBv1.0/{GROUP}/{kind}/{NODE}" + (f"/{device}" if device else "")
        if kind != "NDEATH":
            payload.seq = self.seq
            self.seq = (self.seq + 1) % 256
        self.sock.sendall(_packet(0x30, _str(topic) + payload.SerializeToString()))
        self.last = time.monotonic()

    def ping_if_idle(self) -> None:
        if time.monotonic() - self.last > 20:
            self.sock.sendall(b"\xc0\x00")
            self.last = time.monotonic()

    def death(self) -> None:
        payload = Payload(timestamp=now_ms())
        add_metric(payload, "bdSeq", None, INT64, self.bd_seq)
        self.publish("NDEATH", payload)
        self.sock.sendall(b"\xe0\x00")
        self.sock.close()

    def nbirth(self) -> None:
        self.seq = 0
        payload = Payload(timestamp=now_ms())
        add_metric(payload, "bdSeq", None, INT64, self.bd_seq)
        add_metric(payload, "Node Control/Rebirth", None, BOOLEAN, False)
        self.publish("NBIRTH", payload)


# ---------------------------------------------------------------- burst: what the tests check
BURST = [  # name, alias, datatype, birth value, data value
    ("Temp", 1, DOUBLE, 20.5, 21.25),
    ("Speed", 2, FLOAT, 1.5, 2.5),
    ("Small", 3, INT8, -1, -128),
    ("Medium", 4, INT16, -300, 32767),
    ("Large", 5, INT32, -70000, -2147483648),
    ("Huge", 6, INT64, -5000000000, 7),
    ("Running", 7, BOOLEAN, True, False),
    ("Recipe", 8, STRING, "A-1", "B-2"),
    ("Blob", 9, BYTES, b"\x00\x01", b"\x02"),
    ("Folder/Count", 10, UINT32, 4000000000, 5),
]


def burst(port: int) -> None:
    edge = Edge(port)
    edge.nbirth()
    birth = Payload(timestamp=now_ms())
    for name, alias, datatype, value, _ in BURST:
        add_metric(birth, name, alias, datatype, value)
    edge.publish("DBIRTH", birth, DEVICE)
    data = Payload(timestamp=now_ms())
    for _, alias, datatype, _, value in BURST:     # by alias only, as after a birth
        add_metric(data, None, alias, datatype, value)
    edge.publish("DDATA", data, DEVICE)
    edge.death()


# ---------------------------------------------------------------- line: a plant for the twin
# One server, arrivals Exponential(mean 10 s), service Exponential(mean 12 s): the queue grows,
# so a second server pays. Metric names are the plant mapping's keys under the device.
LINE = [("block/c/count", 1), ("block/p/count", 2), ("block/d/count", 3),
        ("resource/Server/busy", 4), ("resource/Server/capacity", 5),
        ("resource/Server/down", 6), ("resource/Server/blocked", 7), ("block/p/out", 8)]


def line(port: int, seed: int, interarrival: float, service: float) -> None:
    rng = random.Random(seed)
    edge = Edge(port, bd_seq=seed % 256)
    edge.nbirth()
    state = {"block/c/count": 0, "block/p/count": 0, "block/d/count": 0,
             "resource/Server/busy": 0, "resource/Server/capacity": 1,
             "resource/Server/down": False, "resource/Server/blocked": 0, "block/p/out": 0}
    types = {"resource/Server/down": BOOLEAN}
    birth = Payload(timestamp=now_ms())
    for name, alias in LINE:
        add_metric(birth, name, alias, types.get(name, INT64), state[name])
    edge.publish("DBIRTH", birth, DEVICE)
    aliases = dict(LINE)
    next_arrival = time.time() + rng.expovariate(1 / interarrival)
    end_service = None
    while True:
        now = time.time()
        changed = []
        if now >= next_arrival:
            state["block/p/count"] += 1
            changed.append("block/p/count")
            next_arrival += rng.expovariate(1 / interarrival)
        if end_service is not None and now >= end_service:
            state["block/p/count"] -= 1
            state["block/p/out"] += 1
            state["resource/Server/busy"] = 0
            changed += ["block/p/count", "block/p/out", "resource/Server/busy"]
            end_service = None
        if end_service is None and state["block/p/count"] > 0:
            state["resource/Server/busy"] = 1
            changed.append("resource/Server/busy")
            end_service = now + rng.expovariate(1 / service)
        if changed:
            data = Payload(timestamp=now_ms())
            for name in dict.fromkeys(changed):
                add_metric(data, None, aliases[name], types.get(name, INT64), state[name])
            edge.publish("DDATA", data, DEVICE)
        edge.ping_if_idle()
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--interarrival", type=float, default=10.0)
    parser.add_argument("--service", type=float, default=12.0)
    parser.add_argument("mode", choices=("burst", "line"))
    args = parser.parse_args()
    if args.mode == "burst":
        burst(args.port)
    else:
        line(args.port, args.seed, args.interarrival, args.service)


if __name__ == "__main__":
    main()
