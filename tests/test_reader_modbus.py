"""The Modbus TCP reader (D693) against a fake server written here, sharing no code with it.
The fake records every function code it is sent: the reader may only ever send 1 to 4."""
import json
import socketserver
import struct
import threading

import pytest

from nagasim import live, netguard
from nagasim.readers import modbus
from nagasim.sources import ReadError

REGISTERS = {
    "temp": {"address": 0, "kind": "holding", "type": "uint16", "scale": 0.1},
    "level": {"address": 1, "kind": "input", "type": "int16"},
    "flow": {"address": 10, "kind": "holding", "type": "float32"},
    "flow_swapped": {"address": 12, "kind": "holding", "type": "float32", "word_order": "little"},
    "total": {"address": 20, "kind": "holding", "type": "uint32"},
    "running": {"address": 3, "kind": "coil", "type": "bool"},
    "alarm": {"address": 4, "kind": "discrete", "type": "bool"},
}
# float32 1.5 is 0x3FC00000: high word 0x3FC0, low word 0x0000
HOLDING = {0: 215, 10: 0x3FC0, 11: 0x0000, 12: 0x0000, 13: 0x3FC0, 20: 0x0001, 21: 0x0002}
INPUT = {1: 0xFFFE}                         # -2 as int16
BITS = {1: {3: 1}, 2: {4: 0}}


class _Fake:
    """A Modbus TCP server: reads only; anything else is answered 'illegal function'."""

    def __init__(self, behaviour=None):
        self.seen, self.behaviour = [], behaviour
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    while True:
                        head = self.request.recv(7)
                        if len(head) < 7:
                            return
                        tid, _proto, length, unit = struct.unpack(">HHHB", head)
                        pdu = self.request.recv(length - 1)
                        fake.seen.append(pdu[0])
                        if fake.behaviour:
                            reply = fake.behaviour(tid, unit, pdu)
                        else:
                            reply = fake.answer(tid, unit, pdu)
                        if not reply:
                            return                  # hang up
                        self.request.sendall(reply)
                except OSError:
                    pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, tid, unit, pdu):
        fc, address, count = struct.unpack(">BHH", pdu)
        if fc in (3, 4):
            table = HOLDING if fc == 3 else INPUT
            data = b"".join(struct.pack(">H", table.get(address + i, 0)) for i in range(count))
            body = bytes([unit, fc, len(data)]) + data
        elif fc in (1, 2):
            bit = BITS[fc].get(address, 0)
            body = bytes([unit, fc, 1, bit])
        else:
            body = bytes([unit, fc | 0x80, 1])
        return struct.pack(">HHH", tid, 0, len(body)) + body

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(plant_net):
    servers = []

    def start(behaviour=None):
        servers.append(_Fake(behaviour))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def _conn(port, **fields):
    base = {"name": "oven", "kind": "modbus", "scheme": "modbus", "host": "oven.plant.test",
            "port": port, "private": True,
            "registers": netguard._registers("oven", REGISTERS)}
    return netguard.Connection(**{**base, **fields})


def _source(location, **options):
    return {"name": "oven", "kind": "modbus", "connection": "oven", "location": location,
            "options": options}


def _rows(server, location, **fields):
    conn = _conn(server.port, **fields)
    return modbus.read(_source(location), conn, modbus.check(_source(location), conn))


def test_modbus_reads_holding_and_input_registers(fake):
    server = fake()
    rows = _rows(server, "temp\nlevel\ntotal")
    assert rows == [{"register": "temp", "value": pytest.approx(21.5)},
                    {"register": "level", "value": -2},
                    {"register": "total", "value": 0x10002}]
    assert set(server.seen) == {3, 4}


def test_modbus_float32_word_order(fake):
    rows = _rows(fake(), "flow\nflow_swapped")
    assert [r["value"] for r in rows] == [1.5, 1.5]     # same number, words in either order


def test_modbus_scale_and_bool_coil(fake):
    server = fake()
    rows = _rows(server, "temp\nrunning\nalarm")
    assert rows[0]["value"] == pytest.approx(21.5)
    assert rows[1:] == [{"register": "running", "value": True},
                        {"register": "alarm", "value": False}]
    assert set(server.seen) == {1, 2, 3}


def test_the_reader_only_ever_sends_read_function_codes():
    import inspect

    assert set(netguard.MODBUS_KINDS.values()) == {1, 2, 3, 4}
    text = inspect.getsource(modbus)
    # no write function (5, 6, 15, 16, 22, 23) can be built: the function comes from the map
    assert "pack(\">HHHBBHH\"" in text and "MODBUS_KINDS[" in text


def test_modbus_host_comes_from_connection_file_only(tmp_path):
    from nagasim.core import schema

    # a model that names a host is invalid, and a source option cannot carry one
    model = schema.migrate({"data_sources": [{"name": "oven", "kind": "modbus", "connection": "oven",
                                              "location": "modbus://10.0.0.5/temp"}]})
    assert "E_DATA_SOURCE_LOCATION" in [p.code for p in schema.validate_model_structured(model)]
    model = schema.migrate({"data_sources": [{"name": "oven", "kind": "modbus", "connection": "oven",
                                              "location": "temp", "options": {"host": "10.0.0.5",
                                                                              "address": 4}}]})
    assert [p.code for p in schema.validate_model_structured(model)] == ["E_DATA_SOURCE_OPTION"] * 2
    # a location may only name registers of the caller's map, never an address
    conn = _conn(502)
    for location in ("40001", "temp\n10.0.0.5:502", "nosuch", ""):
        with pytest.raises(ReadError) as caught:
            modbus.check(_source(location), conn)
        assert caught.value.code == "E_DATA_SOURCE_LOCATION"
    with pytest.raises(ReadError):
        modbus.check(_source("temp", host="10.0.0.5"), conn)
    # and the map and the host live in the connections file only
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"format": 1, "connections": {"oven": {
        "kind": "modbus", "url": "modbus://10.0.0.5", "private": True, "unit": 3,
        "registers": REGISTERS}}}), encoding="ascii")
    found = netguard.load_connections(path)["oven"]
    assert (found.host, found.port, found.unit, len(found.registers)) == ("10.0.0.5", 502, 3, 7)


def test_modbus_respects_network_policy(fake):
    server = fake()
    source = _source("temp")
    # off by default: no connection is defined, so nothing is opened
    with pytest.raises(netguard.NetworkRefused) as caught:
        live.Feed([source], network=netguard.NetworkPolicy()).rows("oven")
    assert caught.value.code == "E_DATA_CONNECTION" and not server.seen
    # a non-private address is refused unless the caller's file says private
    with pytest.raises(netguard.NetworkRefused):
        netguard.open_connection(_conn(server.port, private=False))
    assert not server.seen
    # with the connection defined it is read through the feed
    policy = netguard.NetworkPolicy(connections={"oven": _conn(server.port)})
    with live.Feed([source], network=policy) as feed:
        rows, _stamp = feed.rows("oven", settle=True)
    assert rows == [{"register": "temp", "value": pytest.approx(21.5)}]


@pytest.mark.parametrize("bad", [
    {"registers": {"a": {"address": 70000, "kind": "holding", "type": "uint16"}}},
    {"registers": {"a": {"address": 65535, "kind": "holding", "type": "uint32"}}},   # runs off the end
    {"registers": {"a": {"address": 1, "kind": "coil", "type": "uint16"}}},
    {"registers": {"a": {"address": 1, "kind": "holding", "type": "bool"}}},
    {"registers": {"a": {"address": 1, "kind": "write", "type": "uint16"}}},
    {"registers": {"a": {"address": 1, "kind": "holding", "type": "uint16", "scale": "x"}}},
    {"registers": {"a b": {"address": 1, "kind": "holding", "type": "uint16"}}},
    {"registers": {"a": {"address": 1, "kind": "holding", "type": "uint16", "write": True}}},
    {"registers": {}},
    {"unit": 256},
    {"secret": {"env": "X"}, "username": "u"},
    {"ca_file": "x.pem"},
])
def test_a_modbus_connection_is_checked(tmp_path, bad):
    spec = {"kind": "modbus", "url": "modbus://127.0.0.1", "private": True,
            "registers": {"a": {"address": 1, "kind": "holding", "type": "uint16"}}, **bad}
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"format": 1, "connections": {"x": spec}}), encoding="ascii")
    with pytest.raises(netguard.ConnectionsError):
        netguard.load_connections(path)


@pytest.mark.slow
def test_a_hostile_server_is_refused_not_believed(fake):
    def reply(tid, unit, pdu, *, length=None, body=None, head_tid=None):
        body = body if body is not None else bytes([unit, pdu[0], 2, 0, 1])
        return struct.pack(">HHH", tid if head_tid is None else head_tid, 0,
                           len(body) if length is None else length) + body

    cases = {
        "exception": lambda t, u, p: reply(t, u, p, body=bytes([u, p[0] | 0x80, 2])),
        "wrong transaction": lambda t, u, p: reply(t, u, p, head_tid=t + 5),
        "wrong unit": lambda t, u, p: reply(t, u + 1, p),
        "declared huge": lambda t, u, p: reply(t, u, p, length=60_000),
        "wrong byte count": lambda t, u, p: reply(t, u, p, body=bytes([u, p[0], 4, 0, 1])),
        "wrong function": lambda t, u, p: reply(t, u, p, body=bytes([u, 6, 2, 0, 1])),
        "closed": lambda t, u, p: b"",
        "junk": lambda t, u, p: b"\xff" * 7,
    }
    for name, behaviour in cases.items():
        with pytest.raises(ReadError) as caught:
            _rows(fake(behaviour), "temp")
        assert caught.value.code == "E_DATA_READ", name


def test_a_non_finite_float_is_no_value():
    register = netguard.Register(0, "holding", "float32")
    assert modbus.decode(register, struct.pack(">f", float("nan"))) is None
    assert modbus.decode(register, struct.pack(">f", float("inf"))) is None


def test_random_bytes_through_the_decoder_never_raise_anything_else():
    import random

    rng = random.Random(7)
    for kind in ("uint16", "int16", "uint32", "int32", "float32"):
        words = netguard.MODBUS_TYPES[kind]
        for order in ("big", "little"):
            register = netguard.Register(0, "holding", kind, 0.5, order)
            for _ in range(200):
                value = modbus.decode(register, rng.randbytes(2 * words))
                assert value is None or isinstance(value, (int, float))


def test_the_deadline_covers_the_whole_poll(fake, monkeypatch):
    monkeypatch.setattr(modbus, "READ_TIMEOUT", 0.3)
    silent = threading.Event()
    server = fake(lambda t, u, p: (silent.wait(3), b"")[1])
    with pytest.raises(ReadError) as caught:
        _rows(server, "temp")
    silent.set()
    assert caught.value.code == "E_DATA_READ"
