"""Advisory PLC writes (Phase 12 E, D742-D744): the `setpoint` verb, `naga plc-write` against a
fake Modbus server written here (it shares no code with the writer and records every function
code it is sent), and the `use: "write"` connection every reader refuses."""
import json
import socketserver
import struct
import threading
import time
from pathlib import Path

import pytest

from nagasim import live, netguard, outbox, plcwrite, recommend
from nagasim.live import iso

KPI = "timeseries/block/p/out/max"
TAG = "line.cycle"
PATH = "blocks.p.params.dist_params.value"
REGISTERS = {"cycle": {"address": 0, "kind": "holding", "type": "uint16", "scale": 0.1},
             "temp": {"address": 10, "kind": "holding", "type": "float32"},
             "level": {"address": 1, "kind": "input", "type": "int16"}}


class _Fake:
    """Holding registers over Modbus TCP: function 3 reads, 6 and 16 write, anything else is
    'illegal function'. *skew* is added to every word written (a PLC that clamps or scales)."""

    def __init__(self, skew=0):
        self.seen, self.holding, self.skew = [], {0: 20, 10: 0x3FC0, 11: 0}, skew
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
                        body = fake.answer(unit, pdu)
                        self.request.sendall(struct.pack(">HHH", tid, 0, len(body)) + body)
                except OSError:
                    pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def answer(self, unit, pdu):
        fc = pdu[0]
        if fc == 3:
            address, count = struct.unpack(">HH", pdu[1:5])
            data = b"".join(struct.pack(">H", self.holding.get(address + i, 0)) for i in range(count))
            return bytes([unit, fc, len(data)]) + data
        if fc == 6:
            address, value = struct.unpack(">HH", pdu[1:5])
            self.holding[address] = (value + self.skew) & 0xFFFF
            return bytes([unit]) + pdu
        if fc == 16:
            address, count, _n = struct.unpack(">HHB", pdu[1:6])
            for i in range(count):
                self.holding[address + i] = struct.unpack(">H", pdu[6 + 2 * i:8 + 2 * i])[0]
            return bytes([unit]) + pdu[:5]
        return bytes([unit, fc | 0x80, 1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(plant_net):
    servers = []

    def start(**kw):
        servers.append(_Fake(**kw))
        return servers[-1]

    yield start
    for server in servers:
        server.close()


def files(tmp_path, port, host="127.0.0.1", tags=None, **conn):
    """(connections file, setpoints file) of a soft PLC on *port*."""
    connections = tmp_path / "plc.json"
    connections.write_text(json.dumps({"format": 1, "connections": {"softplc": {
        "kind": "modbus", "url": f"modbus://{host}:{port}", "use": "write",
        "registers": REGISTERS, **conn}}}), encoding="utf-8")
    setpoints = tmp_path / "line.setpoints.json"
    setpoints.write_text(json.dumps({"format": 1, "kind": "naga-setpoints", "connection": "softplc",
                                     "tags": tags or {TAG: {
                                         "register": "cycle", "path": PATH, "min": 1, "max": 5,
                                         "max_step": 1, "min_interval": "60s"}}}), encoding="utf-8")
    return connections, setpoints


def proposal(value, tag=TAG, expires=600.0, created=None):
    created = time.time() if created is None else created
    action = {"verb": "setpoint", "target": tag, "value": value}
    record = {"format": 1, "kind": "naga-proposal", "created": iso(created),
              "expires": iso(created + expires), "action": action,
              "expected": {"kpi": KPI, "better": "higher", "level": 0.95, "baseline": 1.0,
                           "with_action": 2.0, "effect": 1.0, "low": 0.5, "high": 1.5,
                           "q05": 1.0, "q95": 3.0},
              "confidence": 1.0, "rank": 1,
              "state": {"hash": "a" * 64, "at": 0.0, "stamp": None, "cursors": {}},
              "seed": 0, "replications": 2, "horizon": 12.0,
              "model": {"name": "line", "hash": "b" * 64}, "scenario": "Baseline",
              "mapping": None, "engine": "test"}
    record["id"] = recommend.record_id("b" * 64, "Baseline", "a" * 64, 0, action, KPI, 12.0)
    assert recommend.check(record)
    return record


def queued(tmp_path, *records, by="Matteo", decision="taken"):
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    for record in records:
        sink.deliver(record)
        if decision:
            sink.reply(record["id"], decision, by=by)
    return sink


def plc_write(tmp_path, connections, setpoints, *more):
    said = []
    code = plcwrite.run(f"file:{tmp_path / 'q.jsonl'}", setpoints, connections,
                        tmp_path / "audit.jsonl", out=said.append, **dict(more))
    return code, said


def audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()] if path.exists() else []


# ---------------------------------------------------------------- the writer
def test_a_dry_run_is_the_default_and_writes_nothing_but_the_audit(fake, tmp_path):
    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    record = proposal(2.5)
    queued(tmp_path, record)
    code, said = plc_write(tmp_path, connections, setpoints)
    assert code == 0 and "would be written (dry-run; the PLC holds 2)" in said[-1]
    assert set(server.seen) == {3} and server.holding[0] == 20     # read, never written
    before, after = audit(tmp_path)
    assert (before["phase"], before["mode"], before["id"], before["by"]) == (
        "before", "dry-run", record["id"], "Matteo")
    assert (after["phase"], after["outcome"], after["previous"], after["sent"]) == (
        "after", "dry-run", 2.0, False)


def test_write_writes_one_approved_value_and_reads_it_back(fake, tmp_path):
    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    queued(tmp_path, proposal(2.5))
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 0 and said[-1].endswith("written (was 2, read back 2.5)")
    assert server.holding[0] == 25 and server.seen == [3, 6, 3]   # read, write, read back
    assert [(e["phase"], e.get("outcome")) for e in audit(tmp_path)] == [
        ("before", None), ("after", "written")]
    # Written once: the same record is not written again, whatever the interval.
    server.seen.clear()
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 0 and said[-1] == "no setpoint of the outbox is left to write" and not server.seen


def test_a_32_bit_register_is_written_with_function_16(fake, tmp_path):
    server = fake()
    connections, setpoints = files(tmp_path, server.port, tags={"oven.temp": {
        "register": "temp", "path": PATH, "min": 0, "max": 10, "max_step": 2,
        "min_interval": "1s"}})
    queued(tmp_path, proposal(2.75, tag="oven.temp"))
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 0, said
    assert server.seen == [3, 16, 3] and struct.unpack(
        ">f", struct.pack(">HH", server.holding[10], server.holding[11]))[0] == 2.75


@pytest.mark.parametrize("case, code", [
    ("no reply", "E_PLC_NOT_TAKEN"), ("rejected", "E_PLC_NOT_TAKEN"), ("no by", "E_PLC_NO_BY"),
    ("ended", "E_PLC_ENDED"), ("expired", "E_PLC_EXPIRED"), ("tag", "E_PLC_TAG"),
    ("range", "E_PLC_RANGE"), ("step", "E_PLC_STEP"), ("off the register's step", "E_PLC_VALUE")])
def test_a_write_is_refused_by_name_and_nothing_is_sent(fake, tmp_path, case, code):
    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    record = {"tag": proposal(2.5, tag="line.other"), "range": proposal(9.0),
              "step": proposal(3.5), "off the register's step": proposal(2.55),
              "expired": proposal(2.5, expires=60.0, created=time.time() - 120.0)}.get(
        case, proposal(2.5))
    sink = queued(tmp_path, record, by="  " if case == "no by" else "Matteo",
                  decision={"no reply": None, "rejected": "rejected"}.get(case, "taken"))
    if case == "ended":
        sink.reply(record["id"], "ended", by="Matteo")
    result, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert result == 1 and f"refused {code}:" in said[-1]
    assert 6 not in server.seen and 16 not in server.seen and server.holding[0] == 20
    before, after = audit(tmp_path)         # audited before and after, refused or not
    assert (before["phase"], after["phase"], after["outcome"], after["code"]) == (
        "before", "after", "refused", code)


def test_a_second_write_to_a_tag_inside_its_interval_is_refused(fake, tmp_path):
    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    first, second = proposal(2.5), proposal(3.0)
    queued(tmp_path, first, second)
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 1 and said[-2].endswith("read back 2.5)") and "E_PLC_INTERVAL" in said[-1]
    assert server.holding[0] == 25 and server.seen.count(6) == 1
    # And in a later run too: the interval is read from the audit.
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 1 and "E_PLC_INTERVAL" in said[-1] and server.seen.count(6) == 1


def test_a_read_back_mismatch_is_refused_and_counts_as_a_write(fake, tmp_path):
    server = fake(skew=1)
    connections, setpoints = files(tmp_path, server.port)
    queued(tmp_path, proposal(2.5))
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 1 and "E_PLC_READBACK" in said[-1]
    after = audit(tmp_path)[-1]
    assert (after["code"], after["read_back"], after["sent"]) == ("E_PLC_READBACK", 2.6, True)
    # Not done, so a later run may try it again, but not inside the interval: it may have landed.
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert "E_PLC_INTERVAL" in said[-1] and server.seen.count(6) == 1


def test_a_host_that_is_not_this_machine_is_refused_before_anything(fake, tmp_path, plant_net):
    plant_net["plc.plant.test"] = ["10.0.0.5"]
    server = fake()
    for extra in ({}, {"private": True}):           # nothing in the file lifts it (D744)
        connections, setpoints = files(tmp_path, server.port, host="plc.plant.test", **extra)
        queued(tmp_path, proposal(2.5))
        code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
        assert code == 1 and said[-1].startswith("E_PLC_HOST:")
        assert not server.seen and not audit(tmp_path)


def test_the_audit_line_is_written_before_anything_is_sent(fake, tmp_path, monkeypatch):
    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    queued(tmp_path, proposal(2.5))
    def full(self, record):                 # the audit is at its ceiling
        raise outbox.Full("Full")

    monkeypatch.setattr(outbox.FileSink, "deliver", full)
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 1 and said[-1].startswith("E_PLC_AUDIT:") and not server.seen


@pytest.mark.parametrize("change, code", [
    ({"use": "read"}, "E_PLC_CONNECTION"),
    ({"registers": {"cycle": {"address": 0, "kind": "input", "type": "uint16"}}}, "E_PLC_REGISTER")])
def test_only_a_write_connections_holding_registers_are_written(fake, tmp_path, change, code):
    server = fake()
    connections, setpoints = files(tmp_path, server.port, **change)
    queued(tmp_path, proposal(2.5))
    result, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert result == 1 and said[-1].startswith(code) and not server.seen


def test_a_setpoints_file_is_closed(tmp_path):
    good = {"register": "cycle", "path": PATH, "min": 1, "max": 5, "max_step": 1,
            "min_interval": "60s"}
    path = tmp_path / "s.json"

    def refused(tags=None, **doc):
        path.write_text(json.dumps({"format": 1, "kind": "naga-setpoints", "connection": "softplc",
                                    "tags": tags or {TAG: good}, **doc}), encoding="utf-8")
        with pytest.raises(plcwrite.Refused) as caught:
            plcwrite.load(path)
        return caught.value.code

    assert plcwrite.load(files(tmp_path, 502)[1])["tags"][TAG]["min_interval"] == 60.0
    assert refused(host="10.0.0.5") == "E_PLC_SETPOINTS"          # no key of its own making
    for bad in ({"address": 4}, {"min": 6}, {"max_step": 0}, {"max_step": 9},
                {"min_interval": "0s"}, {"min_interval": "30d"}, {"min": True}, {"path": ""}):
        assert refused({TAG: {**good, **bad}}) == "E_PLC_SETPOINTS", bad
    assert refused({"a b": good}) == "E_PLC_SETPOINTS"


# ---------------------------------------------------------------- the verb (D742)
def test_a_setpoint_candidate_names_a_tag_of_the_callers_file(tmp_path):
    from test_score import slow_line

    model = slow_line()
    tags = plcwrite.load(files(tmp_path, 502)[1])["tags"]
    action = {"verb": "setpoint", "target": TAG, "value": 2.0}
    assert recommend.check_action(action, model, tags) == action
    assert recommend.applied(model, action, tags)["blocks"][1]["params"]["dist_params"]["value"] == 2.0

    def refused(action, setpoints=tags):
        with pytest.raises(recommend.Refused) as caught:
            recommend.check_action(action, model, setpoints)
        return caught.value.message

    assert "no setpoints file" in refused(action, None)
    assert "not in your setpoints file" in refused({**action, "target": "line.other"})
    assert "outside its range" in refused({**action, "value": 9})
    assert "is a number" in refused({**action, "value": True})
    assert "addresses nothing" in refused(action, {TAG: {**tags[TAG], "path": "blocks.nope.x"}})
    assert "not address a number" in refused(action, {TAG: {**tags[TAG], "path": "blocks.p.params.action"}})


def test_a_twins_setpoint_reaches_the_plc_only_through_a_persons_reply(fake, tmp_path):
    """Inside out: the twin proposes and delivers, writes nothing to the PLC; a person replies;
    `plc-write` writes; the twin then forecasts with the setpoint standing."""
    from test_score import cycle_on, playing, slow_line

    server = fake()
    connections, setpoints = files(tmp_path, server.port)
    tags = plcwrite.load(setpoints)["tags"]
    model = slow_line()
    model["simulation"]["timeseries_counts"] = ["p"]
    server.holding[0] = 30                              # the PLC holds the model's 3 s
    action = {"verb": "setpoint", "target": TAG, "value": 2.0}
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"format": 1, "kind": "naga-candidates", "kpi": KPI,
                                      "candidates": [action]}), encoding="utf-8")
    source, clock, _events, mapping, _log = playing(model, counters=True)
    sink = outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}")
    advisor = recommend.Advisor(
        recommend.load(candidates, model, tags), model=model, horizon=12.0, name="line",
        mapping=mapping, out_dir=tmp_path / "out", lifetime=600.0, sink=sink, setpoints=tags,
        wall=time.time, monotonic=lambda: clock.mono)
    cycle = cycle_on(source, clock, tmp_path / "out", advisor=advisor)
    clock.mono = 10.5
    won = cycle.step()["recommend"]["items"][0]
    assert (won["action"], won["verdict"], won["status"]) == (action, "better", "delivered")
    assert not server.seen                              # the twin never reaches the PLC
    sink.reply(won["id"], "taken", by="Matteo")
    # The record's dates are the played plant's (1970): the person writes in that time too.
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True), ("wall", lambda: 60.0))
    assert code == 0 and said[-1].endswith("written (was 3, read back 2)"), said
    assert server.holding[0] == 20
    clock.mono = 11.0
    assert cycle.step()["recommend"]["standing"][0]["action"] == action


# ---------------------------------------------------------------- the boundary (D743)
def test_every_reader_refuses_a_write_connection(fake, tmp_path):
    server = fake()
    connections, _ = files(tmp_path, server.port)
    conns = netguard.load_connections(connections)
    source = {"name": "plc", "kind": "modbus", "connection": "softplc", "location": "cycle"}
    with pytest.raises(netguard.NetworkRefused, match="kept for writes"):
        netguard.connection_for(source, netguard.NetworkPolicy(connections=conns))
    with pytest.raises(netguard.NetworkRefused) as caught:
        live.Feed([source], network=netguard.NetworkPolicy(connections=conns)).rows("plc")
    assert caught.value.code == "E_DATA_CONNECTION" and not server.seen
    # Only a modbus connection is kept for writes.
    connections.write_text(json.dumps({"format": 1, "connections": {"b": {
        "kind": "mqtt", "url": "mqtts://broker.test", "use": "write"}}}), encoding="utf-8")
    with pytest.raises(netguard.ConnectionsError, match="modbus connection only"):
        netguard.load_connections(connections)


# Imports by name (`importlib.import_module`, `__import__`) whose name is not a literal,
# reviewed one by one: {(folder/file, the name expression's source): why it may stay}.
_DYNAMIC_IMPORTS_REVIEWED = {
    ("product/cli.py", 'f"product.{module}"'): "the person's command line, product commands",
    ("product/cli.py", 'f"nagasim.{module}"'): "the person's command line: `product "
                                                 "plc-write` is a person's command, as naga's",
}


def _imports_plcwrite(path: Path) -> bool:
    """Whether *path* imports `nagasim.plcwrite`, by an AST walk (a word in a comment or a
    string is not an import; nothing is imported). An import by name counts too: one whose
    literal name ends in plcwrite, or whose name is not a literal at all, unless that call
    is in `_DYNAMIC_IMPORTS_REVIEWED` (Phase 15 FA)."""
    import ast

    source = path.read_text("utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == "plcwrite" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and ((node.module or "").split(".")[-1] == "plcwrite"
                                                 or any(a.name == "plcwrite" for a in node.names)):
            return True
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called not in ("import_module", "__import__"):
            continue
        name = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "name"), None)
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            if name.value.split(".")[-1] == "plcwrite":
                return True
        else:
            where = f"{path.parent.name}/{path.name}"
            if (where, ast.get_source_segment(source, name) if name else "") \
                    not in _DYNAMIC_IMPORTS_REVIEWED:
                return True
    return False


def test_the_scan_sees_an_import_by_name(tmp_path):
    """An import by name reaches the writer when it names it, or when its name is computed
    (it could be anything); a literal other name does not."""
    cases = {"import importlib\nimportlib.import_module('nagasim.plcwrite')\n": True,
             "__import__('nagasim.plcwrite')\n": True,
             "from importlib import import_module\nimport_module(name='plcwrite')\n": True,
             "import importlib\nm = 'plc' + 'write'\nimportlib.import_module(m)\n": True,
             "__import__(f'nagasim.{x}')\n": True,
             "import importlib\nimportlib.import_module('nagasim.readers.modbus')\n": False,
             "import importlib\nimportlib.reload(importlib)\n": False}
    for text, reaches in cases.items():
        probe = tmp_path / "probe.py"
        probe.write_text(text, encoding="utf-8")
        assert _imports_plcwrite(probe) is reaches, text


def test_the_writer_sends_only_codes_6_and_16_and_the_reader_only_reads(fake, tmp_path):
    from nagasim.readers import modbus

    server = fake()
    connections, setpoints = files(tmp_path, server.port, tags={
        TAG: {"register": "cycle", "path": PATH, "min": 1, "max": 5, "max_step": 1, "min_interval": "1s"},
        "oven.temp": {"register": "temp", "path": PATH, "min": 0, "max": 10, "max_step": 2,
                      "min_interval": "1s"}})
    queued(tmp_path, proposal(2.5), proposal(2.75, tag="oven.temp"))
    code, said = plc_write(tmp_path, connections, setpoints, ("write", True))
    assert code == 0, said
    # What the PLC was sent: reads (3) and the two write codes, one of each kind of register.
    assert set(server.seen) == {3, 6, 16}
    assert set(netguard.MODBUS_KINDS.values()) == {1, 2, 3, 4}
    assert not _imports_plcwrite(Path(modbus.__file__))


def test_no_agent_tool_reaches_the_writer(tmp_path):
    """D743: no tool, no assistant and no MCP server imports `plcwrite` or takes a setpoint."""
    from nagasim import agent_tools as T

    root = Path(plcwrite.__file__).parents[1]
    surfaces = [*(root / "nagasim" / "agent_tools").rglob("*.py"), root / "nagasim" / "mcp_server.py",
                root / "nagasim" / "assistant.py", *(root / "product").rglob("*.py")]
    reached = [p.name for p in surfaces if p.exists() and _imports_plcwrite(p)]
    assert reached == []
    probe = tmp_path / "probe.py"                 # the walk finds an import when there is one
    probe.write_text("from nagasim import plcwrite" + chr(10), encoding="utf-8")
    assert _imports_plcwrite(probe)
    for tool in T.tool_schemas():
        names = set(tool["input_schema"].get("properties") or {})
        assert not names & {"setpoints", "setpoint", "register", "write"}, tool["name"]
        assert "plc" not in tool["name"] and "setpoint" not in tool["name"]


def test_naga_plc_write_and_the_twin_flag_refuse_a_bad_setpoints_file(tmp_path, capsys):
    from nagasim import cli, twin

    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert cli.main(["plc-write", "--outbox", f"file:{tmp_path / 'q.jsonl'}", "--setpoints",
                     str(bad), "--connections", str(bad), "--audit", str(tmp_path / "a.jsonl")]) == 1
    assert capsys.readouterr().out.startswith("E_PLC_SETPOINTS:")
    model = Path(__import__("nagasim").__file__).parent / "models" / "line_with_buffers.json"
    assert twin.main(["reproduce", str(tmp_path), str(model), "--setpoints", str(bad)]) == 1
    assert "E_PLC_SETPOINTS" in capsys.readouterr().out
