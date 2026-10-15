"""Against servers we did not write (opt-in): Mosquitto, asyncua's OPC UA server, and a
Sparkplug B edge node built on the `protobuf` package from the official schema.

    NAGA_REALSERVER=1 python -m pytest -q -m realserver tests/test_realserver.py

Skipped unless NAGA_REALSERVER=1 and mosquitto is found (on PATH, or in NAGA_MOSQUITTO_DIR,
default C:\\Program Files\\mosquitto). Every server is a process of the test's own, bound to
127.0.0.1 on a free port, and killed at teardown; the Windows service is never used.
"""
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_readers import _eventually, _until
from nagasim import live, netguard, outbox

pytestmark = [pytest.mark.realserver, pytest.mark.skipif(
    os.environ.get("NAGA_REALSERVER") != "1", reason="set NAGA_REALSERVER=1 to run against real servers")]

HERE = Path(__file__).parent / "realserver"
_DIR = Path(os.environ.get("NAGA_MOSQUITTO_DIR", r"C:\Program Files\mosquitto"))


def _exe(name: str) -> str | None:
    found = shutil.which(name) or _DIR / f"{name}.exe"
    return str(found) if Path(found).is_file() else None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Mosquitto:
    """One mosquitto process: a plain listener and a TLS one, both on 127.0.0.1."""

    def __init__(self, folder: Path, tls_files):
        self.port, self.tls_port, self.folder = _free_port(), _free_port(), folder
        cert, key = (Path(p).as_posix() for p in tls_files)
        self.conf = folder / "mosquitto.conf"
        self.conf.write_text(
            f"allow_anonymous true\npersistence false\nlog_dest stdout\nlog_type all\n"
            f"listener {self.port} 127.0.0.1\n"
            f"listener {self.tls_port} 127.0.0.1\ncertfile {cert}\nkeyfile {key}\n", encoding="ascii")
        self.proc = None
        self.start()

    def start(self) -> None:
        log = open(self.folder / "mosquitto.log", "ab")
        self.proc = subprocess.Popen([_exe("mosquitto"), "-c", str(self.conf)], stdout=log,
                                     stderr=subprocess.STDOUT)
        log.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("mosquitto did not start; see " + str(self.folder / "mosquitto.log"))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def publish(self, topic: str, message: str, qos: int = 0, retain: bool = False) -> None:
        subprocess.run([_exe("mosquitto_pub"), "-h", "127.0.0.1", "-p", str(self.port), "-t", topic,
                        "-m", message, "-q", str(qos)] + (["-r"] if retain else []),
                       check=True, timeout=10)


@pytest.fixture
def mosquitto(tmp_path, tls_files, plant_net):
    if not (_exe("mosquitto") and _exe("mosquitto_pub") and _exe("mosquitto_sub")):
        pytest.skip("mosquitto is not installed")
    broker = Mosquitto(tmp_path, tls_files)
    yield broker
    broker.stop()


def _conn(port, **fields) -> netguard.Connection:
    return netguard.Connection(**{"name": "broker", "kind": "mqtt", "scheme": "mqtt",
                                  "host": "127.0.0.1", "port": port, "private": True, **fields})


def _feed(conn, location, **options):
    source = {"name": "plant", "kind": "mqtt", "connection": conn.name, "location": location,
              "options": {"settle": 0.3, **options}}
    return live.Feed([source], network=netguard.NetworkPolicy(connections={conn.name: conn}))


def _live(feed, source="plant", seconds=10.0):
    cursor = feed.changes(source).cursor
    _eventually(lambda: feed.status(source)["state"] == "live", seconds)
    return cursor


# ---------------------------------------------------------------- MQTT reader
def test_the_reader_subscribes_to_mosquitto_retained_and_at_each_qos(mosquitto):
    mosquitto.publish("plant/line1/speed", "3.5", qos=1, retain=True)
    with _feed(_conn(mosquitto.port), "plant/#", payload="json", time="ts", qos=1) as feed:
        cursor = _live(feed)
        events, cursor = _until(feed, "plant", 1, cursor)
        assert [(e.key, e.value) for e in events] == [("plant/line1/speed", 3.5)]   # retained
        mosquitto.publish("plant/line1/state", json.dumps(
            {"ts": "2026-10-15T10:00:00+00:00", "rate": 2, "ok": True}), qos=0)
        mosquitto.publish("plant/line1/count", "7", qos=1)
        mosquitto.publish("plant/line1/count", "8", qos=2)     # granted QoS 1: delivered at 1
        events, cursor = _until(feed, "plant", 4, cursor)
        got = [(e.key, e.value, e.source_time) for e in events]
        stamp = live.stamp("2026-10-15T10:00:00+00:00")
        assert got == [("plant/line1/state/rate", 2, stamp), ("plant/line1/state/ok", True, stamp),
                       ("plant/line1/count", 7, None), ("plant/line1/count", 8, None)]
        assert feed.status("plant")["dropped"] == {}


def test_the_reader_connects_again_after_the_broker_restarts(mosquitto):
    with _feed(_conn(mosquitto.port), "plant/#", qos=1) as feed:
        cursor = _live(feed)
        mosquitto.stop()
        _eventually(lambda: feed.status("plant")["state"] == "down", 10)
        mosquitto.start()
        _eventually(lambda: feed.status("plant")["state"] == "live", 20)
        mosquitto.publish("plant/line1/count", "9", qos=1)
        events, _ = _until(feed, "plant", 1, cursor)
        assert events[-1].value == 9
        assert feed.changes("plant", cursor).gap is True    # the outage is a hole, said


def test_the_reader_and_the_sink_work_over_mosquittos_tls_listener(mosquitto, tls_files):
    plant_tls = _conn(mosquitto.tls_port, scheme="mqtts", host="plant.test", ca_file=tls_files[0])
    with _feed(plant_tls, "plant/#") as feed:
        cursor = _live(feed)
        assert feed.status("plant")["channel"] == "tls"
        mosquitto.publish("plant/line1/count", "5")
        events, _ = _until(feed, "plant", 1, cursor)
        assert events[0].value == 5
    sink = outbox.open_sink("mqtt:broker", {"broker": _conn(
        mosquitto.tls_port, scheme="mqtts", host="plant.test", ca_file=tls_files[0],
        use="outbox", root="naga/line1")})
    sink.deliver({"id": "c" * 64, "kind": "naga-proposal"})       # PUBACK over TLS
    sink.close()
    # Without the caller's CA the broker's certificate is not trusted: down, coded.
    with _feed(_conn(mosquitto.tls_port, scheme="mqtts", host="plant.test"), "plant/#") as feed:
        feed.changes("plant")
        _eventually(lambda: feed.status("plant")["problem"] is not None, 10)
        assert "TLS handshake" in feed.status("plant")["problem"]


# ---------------------------------------------------------------- Sparkplug B
def test_sparkplug_b_births_data_and_death_from_an_independent_edge_node(mosquitto):
    import importlib.util

    spec = importlib.util.spec_from_file_location("sparkplug_sim", HERE / "sparkplug_sim.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    with _feed(_conn(mosquitto.port), "spBv1.0/Plant1/#", payload="sparkplug_b") as feed:
        cursor = _live(feed)
        subprocess.run([sys.executable, str(HERE / "sparkplug_sim.py"), "--port",
                        str(mosquitto.port), "burst"], check=True, timeout=30)
        said: dict = {}

        def dead():
            nonlocal cursor
            got = feed.changes("plant", cursor)
            cursor = got.cursor
            for event in got.events:
                said.setdefault(event.key, []).append(event.value)
            return said.get("Plant1/Edge1", [])[-1:] == [False]
        _eventually(dead, 10)
        assert said["Plant1/Edge1"] == [True, False]                 # NBIRTH, then NDEATH
        assert said["Plant1/Edge1/bdSeq"] == [0]
        assert said["Plant1/Edge1/Node Control/Rebirth"] == [False]
        assert said["Plant1/Edge1/Line1"] == [True, False]          # its node's death (#443)
        for name, _alias, _type, born, data in sim.BURST:
            if name != "Blob":
                assert said[f"Plant1/Edge1/Line1/{name}"] == [born, data], name
        status = feed.status("plant")
        assert status["dropped"].get("type") == 2 and "unresolved" not in status["dropped"]
        assert status["gaps"] == 0                                    # bytes: not read, counted


# ---------------------------------------------------------------- OPC UA
@pytest.fixture
def opcua_server(plant_net):
    pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(HERE / "opcua_server.py"), str(port)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        line = proc.stdout.readline()
        assert line.startswith("READY"), line
        yield port, int(line.split()[1]), proc
    finally:
        proc.kill()
        proc.wait(10)


def test_the_opcua_reader_against_an_asyncua_server_process(opcua_server):
    port, ns, proc = opcua_server
    conn = netguard.Connection(name="press", kind="opcua", scheme="opc.tcp", host="127.0.0.1",
                               port=port, path="/plant", private=True, root=f"ns={ns};s=Press.")
    nodes = [f"ns={ns};s=Press.{name}" for name in ("Speed", "Count", "Running", "Recipe", "Fault")]
    source = {"name": "press", "kind": "opcua", "connection": "press", "location": "\n".join(nodes),
              "options": {"interval_ms": 100, "settle": 0.5}}
    with live.Feed([source], network=netguard.NetworkPolicy(connections={"press": conn})) as feed:
        cursor = _live(feed, "press", 20)
        events, cursor = _until(feed, "press", 20, cursor, seconds=10)
        said: dict = {}
        for event in events:
            said.setdefault(event.key.split(".")[-1], []).append(event.value)
            assert event.source_time is not None or event.key.endswith("Fault")
        assert all(isinstance(v, float) for v in said["Speed"]) and said["Speed"] == sorted(said["Speed"])
        assert all(isinstance(v, int) and v < 0 for v in said["Count"])
        assert {type(v) for v in said["Running"]} == {bool}
        assert all(v.startswith("R-") for v in said["Recipe"])
        assert said["Fault"] == [None]                  # a Bad status is a None value
        proc.kill()
        _eventually(lambda: feed.status("press")["state"] == "down", 20)
        assert feed.changes("press", cursor).gap is True


# ---------------------------------------------------------------- the MQTT outbox sink
def test_the_outbox_publishes_to_mosquitto_and_reads_replies_from_its_clients(mosquitto):
    record_id = "d" * 64
    connections = {"broker": _conn(mosquitto.port, use="outbox", root="naga/line1")}
    # A retained reply is there before the twin connects: it is read when it subscribes.
    mosquitto.publish("naga/line1/replies", json.dumps({"id": "e" * 64, "decision": "rejected"}),
                      qos=1, retain=True)
    # mosquitto_sub 2.1.2 on Windows crashes now and then (0xC0000005) with -d when its stdout
    # is a pipe, so its subscription is confirmed by a probe it hears instead of by SUBACK.
    watcher = subprocess.Popen([_exe("mosquitto_sub"), "-h", "127.0.0.1", "-p", str(mosquitto.port),
                                "-t", "naga/line1/proposals/#", "-v", "-W", "30"],
                               stdout=subprocess.PIPE, text=True)
    heard: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [heard.put(one) for one in watcher.stdout], daemon=True).start()

    def hear(prefix):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if prefix.endswith("probe"):
                mosquitto.publish(prefix, "probe")
            try:
                while not (one := heard.get(timeout=0.5)).startswith(prefix):
                    pass
                return one.rstrip("\n")
            except queue.Empty:
                pass
        raise AssertionError(f"mosquitto_sub heard nothing on {prefix}")
    try:
        hear("naga/line1/proposals/probe")
        sink = outbox.open_sink("mqtt:broker", connections)
        sink.deliver({"id": record_id, "kind": "naga-proposal", "x": 1.5})
        line = hear(f"naga/line1/proposals/{record_id}")
    finally:
        watcher.kill()
        watcher.wait(10)
    topic, payload = line.split(" ", 1)
    assert topic == f"naga/line1/proposals/{record_id}"
    assert json.loads(payload) == {"id": record_id, "kind": "naga-proposal", "x": 1.5}
    try:
        cursor = sink.changes().cursor
        _eventually(lambda: sink._feed.status(outbox.REPLIES)["state"] == "live", 10)
        mosquitto.publish("naga/line1/replies", json.dumps(
            {"id": record_id, "decision": "taken", "by": "Ada"}), qos=1)
        events, deadline = [], time.monotonic() + 10
        while len(events) < 2 and time.monotonic() < deadline:
            got = sink.changes(outbox.REPLIES, cursor)
            cursor, events = got.cursor, events + list(got.events)
            time.sleep(0.05)
        assert [(e.key, e.value["decision"]) for e in events] == [
            ("e" * 64, "rejected"), (record_id, "taken")]
    finally:
        sink.close()
