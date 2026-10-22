"""The outbox's MQTT sink (D408-D411): the engine's first write to a network.

Against the scripted broker of the reader's tests, which shares no code with the client.
"""
import json
import socket
import time

import pytest

from test_readers import _eventually, _mqtt, broker  # noqa: F401  (the broker fixture)
from test_recommend import KPI, MORE, SPARE, asked, counted
from test_score import cycle_on, playing, slow_line
from nagasim import netguard, outbox, plant as plants, recommend, twin

AN_ID = "a" * 64


def sink_on(port, **fields):
    conn = _mqtt(port, **{"use": "outbox", "root": "naga/line1", **fields})
    return outbox.open_sink("mqtt:line", {"line": conn})


def replies_until(sink, wanted, cursor=None, seconds=5.0):
    events, lost, deadline = [], 0, time.monotonic() + seconds
    while len(events) < wanted and time.monotonic() < deadline:
        got = sink.changes(outbox.REPLIES, cursor)
        cursor, lost = got.cursor, lost + got.lost
        events += got.events
        time.sleep(0.05)
    return events, lost


def test_a_record_is_one_acknowledged_publish_under_the_callers_root(broker):
    plant = broker()
    sink = sink_on(plant.port)
    record = {"id": AN_ID, "kind": "naga-proposal", "x": 1.5}
    sink.deliver(record)
    (sent,) = plant.published
    # QoS 1, not retained, not a duplicate; the topic is the root, a fixed level and the id.
    assert (sent["topic"], sent["flags"]) == (f"naga/line1/proposals/{AN_ID}", 0x02)
    assert sent["payload"] == json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    _eventually(lambda: (14, 0) in plant.packets)
    # One connection a record: CONNECT (clean session, no will), PUBLISH, DISCONNECT.
    assert [kind for kind, _flags in plant.packets] == [1, 3, 14]
    assert plant.connects[0]["flags"] == 0x02 and not plant.subscribes
    # A record over the bound, or one with no id of ours, never leaves.
    for bad in ({"id": AN_ID, "pad": "x" * outbox.LIMITS["record_bytes"]},
                {"id": "naga/#", "x": 1}, {"x": 1}):
        with pytest.raises(ValueError):
            sink.deliver(bad)
    assert len(plant.published) == 1
    sink.close()


def test_only_the_callers_outbox_connection_is_a_sink(broker):
    """D411: the caller grants it in their own file; a read connection is no sink, an outbox
    connection no source, and a root that is not a plain topic is refused."""
    port = broker().port
    for connections, said in ((None, "no connection named"), ({}, "no connection named"),
                              ({"line": _mqtt(port, root="naga/line1")}, "kept for an outbox")):
        with pytest.raises(outbox.SinkError, match=said):
            outbox.open_sink("mqtt:line", connections)
    for root in ("", "naga/#", "naga/+/x", "$SYS/naga", "/naga", "naga/", "naga//x",
                 "naga\x00x", "n" * 513):
        with pytest.raises(outbox.SinkError, match="plain topic"):
            sink_on(port, root=root) if root else outbox.open_sink(
                "mqtt:line", {"line": _mqtt(port, use="outbox")})
    rest = netguard.Connection(name="line", kind="rest", scheme="https", host="mes.test",
                               port=443, use="outbox")
    with pytest.raises(outbox.SinkError, match="not an mqtt connection"):
        outbox.open_sink("mqtt:line", {"line": rest})
    # A model's source naming the outbox's connection: refused by the readers' one lookup.
    policy = netguard.NetworkPolicy(connections={"line": _mqtt(port, use="outbox", root="naga/line1")})
    with pytest.raises(netguard.NetworkRefused, match="kept for an outbox"):
        netguard.connection_for({"name": "s", "kind": "mqtt", "connection": "line"}, policy)


def test_replies_are_read_back_through_the_readers_subscription(broker):
    """D409: `changes(source, cursor)` as for the file; only a reply is an event."""
    plant = broker()
    sink = sink_on(plant.port)
    cursor = sink.changes().cursor      # the twin keeps its cursor: no snapshot merges replies
    assert plant.ready.wait(5)
    assert plant.subscribes == [[("naga/line1/replies", 1)]]
    plant.publish("naga/line1/replies", b"not json", qos=1)
    plant.publish("naga/line1/replies", json.dumps(
        {"id": AN_ID, "decision": "taken", "by": "Ada", "exec": "rm", "path": "C:/x"}).encode(), qos=1)
    events, lost = replies_until(sink, 1, cursor)
    (event,) = events
    assert (event.key, event.value["decision"], event.value["by"], lost) == (AN_ID, "taken", "Ada", 1)
    assert set(event.value) == {"id", "decision", "at", "by", "note"}
    sink.close()


@pytest.mark.slow
def test_a_broker_that_is_down_fails_the_delivery_and_the_rest_of_that_step_at_once(
        broker, monkeypatch):
    """D410: the error is a code or a class, never the broker's text; after one failure the
    others fail without a handshake each."""
    from nagasim.readers import mqtt

    monkeypatch.setattr(mqtt, "PACKET_TIMEOUT", 0.5)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    sink = sink_on(port)
    with pytest.raises(OSError, match="^E_DATA_READ$"):
        sink.deliver({"id": AN_ID})
    started = time.monotonic()
    with pytest.raises(OSError, match="^E_DATA_READ$"):    # M2: the real class, not "Down"
        sink.deliver({"id": "b" * 64})
    assert time.monotonic() - started < 0.2
    # A broker that refuses, and one that never acknowledges.
    for there in (broker(connack=5), broker()):
        there.puback = False
        with pytest.raises(OSError, match="^E_DATA_READ$"):
            sink_on(there.port).deliver({"id": AN_ID})


def test_a_twin_delivers_to_an_mqtt_outbox_and_reads_its_replies(broker, tmp_path):
    """The loop of 7.15 over a broker: delivered once, answered, the action stands."""
    plant = broker()
    model = counted(slow_line())
    source, clock, _events, mapping, _log = playing(model, counters=True)
    sink = sink_on(plant.port)
    advisor = recommend.Advisor(
        recommend.load(asked(tmp_path, MORE, SPARE), model), model=model, horizon=12.0,
        name="line", mapping=mapping, out_dir=tmp_path / "out", sink=sink,
        wall=lambda: clock.wall, monotonic=lambda: clock.mono)
    cycle = cycle_on(source, clock, tmp_path / "out", advisor=advisor)
    clock.mono = 10.5
    said = cycle.step()["recommend"]
    won = said["items"][0]
    assert (won["action"], won["status"], said["outbox"]["sink"]) == (MORE, "delivered", "mqtt")
    (sent,) = plant.published
    record = json.loads(sent["payload"])
    assert recommend.check(record) and sent["topic"] == f"naga/line1/proposals/{won['id']}"
    assert record["expected"]["kpi"] == KPI
    assert plant.ready.wait(5)
    plant.publish("naga/line1/replies",
                  json.dumps({"id": won["id"], "decision": "taken"}).encode(), qos=1)

    def taken():
        clock.mono += 0.1
        return cycle.step()["recommend"]["standing"] != []
    _eventually(taken)
    assert len(plant.published) == 1                # delivered once
    sink.close()


def test_naga_twin_refuses_an_mqtt_outbox_without_connections(tmp_path, capsys):
    model = counted(slow_line())
    _source, _clock, _events, mapping, log = playing(model, counters=True)
    paths = [tmp_path / name for name in ("line.json", "line.plantmap.json", "plant.jsonl")]
    for path, data in zip(paths, (model, mapping)):
        path.write_text(json.dumps(data), encoding="utf-8")
    plants.write_log(paths[2], log[:5])
    assert twin.main(["once", str(paths[0]), "--map", str(paths[1]), "--log", str(paths[2]),
                      "--out", str(tmp_path / "out"), "--horizon", "12",
                      "--candidates", str(asked(tmp_path, MORE)), "--outbox", "mqtt:line"]) == 1
    assert "E_OUTBOX_SINK: an mqtt outbox needs --connections" in capsys.readouterr().out


def _advisor(tmp_path, sink):
    model = counted(slow_line())
    _source, clock, _events, mapping, _log = playing(model, counters=True)
    return recommend.Advisor(
        recommend.load(asked(tmp_path, MORE), model), model=model, horizon=12.0, name="line",
        mapping=mapping, out_dir=tmp_path / "out", sink=sink, wall=lambda: clock.wall,
        monotonic=lambda: clock.mono)


def test_replies_share_one_topic_so_the_keys_limit_cannot_silence_them(broker):
    """I1: ids in the payload, one key; five replies are read with only 3 keys allowed."""
    plant = broker()
    sink = sink_on(plant.port, limits=(("keys", 3),))
    cursor = sink.changes().cursor
    assert plant.ready.wait(5)
    ids = [c * 64 for c in "abcde"]
    for ident in ids:
        plant.publish("naga/line1/replies",
                      json.dumps({"id": ident, "decision": "taken"}).encode(), qos=1)
    events, lost = replies_until(sink, 5, cursor)
    assert [e.key for e in events] == ids and lost == 0
    sink.close()


def test_a_drop_or_loss_of_the_reply_feed_reaches_the_result_while_it_is_live(tmp_path):
    from nagasim.core.problems import problem
    from nagasim.live import Changes

    class Sink:
        def changes(self, source, cursor, limit):
            return Changes((), "1", 0, False, "live", 0.0,
                           (problem("W_LIVE_DROPPED", "dropped", "/x"),))

    advisor = _advisor(tmp_path, Sink())
    advisor._replies({}, 0.0)
    assert [p.code for p in advisor._said] == ["W_LIVE_DROPPED"]


def test_a_step_spends_a_time_budget_on_delivery_and_leaves_the_rest_proposed(
        broker, tmp_path, monkeypatch):
    """I2: records over the budget wait for the next step."""
    sink = sink_on(broker().port)
    advisor = _advisor(tmp_path, sink)
    sent = []
    monkeypatch.setattr(sink, "deliver", lambda record: (time.sleep(0.2), sent.append(record)))
    monkeypatch.setattr(recommend, "_BUDGET", 0.3)
    advisor._entries.extend({"record": {"id": c * 64}, "status": "proposed", "kept": True,
                             "attempts": 0} for c in "bcd")
    advisor._deliver(0.0)
    assert [e["status"] for e in advisor._entries] == ["delivered", "delivered", "proposed"]
    advisor._deliver(0.0)
    assert len(sent) == 3


def test_a_silent_broker_costs_one_delivery_no_more_than_its_deadline(plant_net, monkeypatch):
    """I2: one deadline from connect to PUBACK (a broker that accepts and says nothing)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    monkeypatch.setattr(outbox, "_DEADLINE", 0.6)
    sink = sink_on(server.getsockname()[1])
    started = time.monotonic()
    with pytest.raises(OSError, match="^E_DATA_READ$"):
        sink.deliver({"id": AN_ID})
    assert time.monotonic() - started < 2.0
    server.close()


def test_a_connection_with_a_mistake_is_refused_at_start(plant_net):
    plant_net["broker.plant.test"] = ["8.8.8.8"]
    secret = netguard.Secret("p4ss")
    with pytest.raises(outbox.SinkError, match="E_DATA_PLAINTEXT_CREDENTIAL"):
        sink_on(1883, username="twin", secret=secret)
    plant_net["broker.plant.test"] = ["10.1.2.3"]
    with pytest.raises(outbox.SinkError, match="E_DATA_URL_PRIVATE"):
        sink_on(1883, private=False)


def test_the_sink_works_over_mqtts(broker, tls_files):
    plant = broker(tls=True)
    conn = _mqtt(plant.port, scheme="mqtts", host="plant.test", ca_file=tls_files[0],
                 use="outbox", root="naga/line1")
    outbox.open_sink("mqtt:line", {"line": conn}).deliver({"id": AN_ID})
    assert plant.published[0]["topic"] == f"naga/line1/proposals/{AN_ID}"


def test_small_hardening_points(broker):
    from nagasim.readers import mqtt

    assert outbox.parse_reply(json.dumps({"id": AN_ID + chr(10), "decision": "taken"}).encode()) is None
    with pytest.raises(outbox.SinkError, match="plain topic"):
        sink_on(broker().port, root="naga/" + chr(0xD800))

    class Huge(bytes):
        def __len__(self):
            return 268_435_456

    with pytest.raises(ValueError):
        mqtt._framed(0x30, Huge())


def test_a_target_over_200_characters_is_refused_when_the_candidates_load():
    with pytest.raises(recommend.Refused, match="over 200 characters"):
        recommend.check_action({"verb": "set_capacity", "target": "x" * 201, "value": 3}, {})
