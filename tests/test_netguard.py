"""SSRF and path-confinement guards (`nagasim/netguard.py`).

No sockets and no network: `socket.getaddrinfo` is monkeypatched with a small
name -> addresses map, and the URL opener with a fake response, so every one of
these runs in microseconds and cannot reach anything real.
"""
import ipaddress
import json
import os
import socket

import pytest

from nagasim import netguard
from nagasim.netguard import NetworkPolicy

# What the fake resolver answers. Anything not listed resolves to a public
# address, so a test row only names a host when the ANSWER is the point.
HOSTS = {
    "localhost": ["127.0.0.1"],
    "metadata.internal": ["169.254.169.254"],
    "split.example.com": ["93.184.216.34", "10.0.0.7"],   # public AND private
    "intranet.example.com": ["192.168.1.10"],
    "cgnat.example.com": ["100.64.1.1"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "public.example.com": ["93.184.216.34"],
    "other.example.com": ["93.184.216.35"],
}


def _fake_resolve(host: str) -> list[str]:
    """A literal resolves to itself -- INCLUDING the decimal and hex forms a
    real resolver accepts (`0x7f000001` is 127.0.0.1), which is exactly the
    obfuscation the guard has to see through."""
    if host in HOSTS:
        return HOSTS[host]
    for parse in (ipaddress.ip_address, lambda h: ipaddress.ip_address(int(h, 0))):
        try:
            return [str(parse(host))]
        except ValueError:
            pass
    return ["93.184.216.34"]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
                for a in _fake_resolve(host)]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


ON = NetworkPolicy(allow_network=True)

# (url, policy, expected code or None when it must be allowed)
URL_CASES = [
    ("https://public.example.com/d.json", NetworkPolicy(), "E_DATA_NETWORK_REFUSED"),
    ("file:///etc/passwd", ON, "E_DATA_URL_SCHEME"),
    ("ftp://public.example.com/d.json", ON, "E_DATA_URL_SCHEME"),
    ("gopher://public.example.com/1", ON, "E_DATA_URL_SCHEME"),
    ("http://user:pw@public.example.com/d", ON, "E_DATA_CREDENTIALS"),
    ("http://127.0.0.1/d", ON, "E_DATA_URL_PRIVATE"),
    ("http://localhost/d", ON, "E_DATA_URL_PRIVATE"),
    ("http://[::1]/d", ON, "E_DATA_URL_PRIVATE"),
    ("http://169.254.169.254/latest/meta-data", ON, "E_DATA_URL_PRIVATE"),
    ("http://metadata.internal/latest/meta-data", ON, "E_DATA_URL_PRIVATE"),
    ("http://0x7f000001/d", ON, "E_DATA_URL_PRIVATE"),          # hex for 127.0.0.1
    ("http://2130706433/d", ON, "E_DATA_URL_PRIVATE"),          # decimal for 127.0.0.1
    ("http://mapped.example.com/d", ON, "E_DATA_URL_PRIVATE"),  # ::ffff:10.0.0.1
    ("http://split.example.com/d", ON, "E_DATA_URL_PRIVATE"),   # one private answer is enough
    ("http://cgnat.example.com/d", ON, "E_DATA_URL_PRIVATE"),
    ("http://intranet.example.com/d", ON, "E_DATA_URL_PRIVATE"),
    ("http://intranet.example.com/d",
     NetworkPolicy(allow_network=True, allow_private=True), None),
    ("http://localhost/d",                                      # loopback stays refused
     NetworkPolicy(allow_network=True, allow_private=True), "E_DATA_URL_PRIVATE"),
    ("https://other.example.com/d",
     NetworkPolicy(allow_network=True, allowed_hosts=frozenset({"public.example.com"})),
     "E_DATA_URL_HOST"),
    ("https://public.example.com/d",
     NetworkPolicy(allow_network=True, allowed_hosts=frozenset({"public.example.com"})), None),
    ("https://public.example.com/d.json", ON, None),
]


@pytest.mark.parametrize("url, policy, code", URL_CASES)
def test_check_url_refuses_everything_that_is_not_a_public_http_url(url, policy, code):
    if code is None:
        netguard.check_url(url, policy)
        return
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.check_url(url, policy)
    assert caught.value.code == code
    assert caught.value.hint                     # a refusal always tells the human what to do


def test_a_sqlite_url_is_not_a_network_database_and_a_host_is_still_vetted():
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.check_sql_url("sqlite:///data/local.db", ON)
    assert caught.value.code == "E_DATA_URL_SCHEME"

    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.check_sql_url("postgresql://u@intranet.example.com/db", ON)
    assert caught.value.code == "E_DATA_URL_PRIVATE"

    netguard.check_sql_url("postgresql://u@public.example.com/db", ON)


class _Response:
    """The little of an http response `fetch` uses."""

    def __init__(self, body: bytes, length=None):
        self.body = body
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_redirect_is_re_vetted_per_hop_and_the_chain_is_capped():
    handler = netguard._CheckedRedirects(ON)
    request = type("R", (), {"type": "https", "full_url": "https://public.example.com/d"})()

    with pytest.raises(netguard.NetworkRefused) as caught:       # public -> metadata service
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://metadata.internal/latest/meta-data")
    assert caught.value.code == "E_DATA_URL_PRIVATE"

    with pytest.raises(netguard.NetworkRefused) as caught:       # https -> http downgrade
        handler.redirect_request(request, None, 302, "Found", {}, "http://public.example.com/d")
    assert caught.value.code == "E_DATA_URL_REDIRECT"

    handler.hops = netguard.MAX_REDIRECTS
    with pytest.raises(netguard.NetworkRefused) as caught:
        handler.redirect_request(request, None, 302, "Found", {},
                                 "https://public.example.com/again")
    assert caught.value.code == "E_DATA_URL_REDIRECT"


@pytest.mark.parametrize("body, declared", [(b"x" * 200, None), (b"", 10_000)])
def test_a_response_over_the_cap_is_refused(body, declared, monkeypatch):
    policy = NetworkPolicy(allow_network=True, max_bytes=100)
    monkeypatch.setattr(netguard, "_opener",
                        lambda _p: type("O", (), {"open": lambda *a, **k: _Response(body, declared)})())
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.fetch("https://public.example.com/big.json", policy)
    assert caught.value.code == "E_DATA_TOO_LARGE"


def test_confine_path_keeps_every_read_under_the_root(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "ok.csv").write_text("a", encoding="utf-8")
    outside = tmp_path / "secret.csv"
    outside.write_text("s", encoding="utf-8")

    assert netguard.confine_path("sub/ok.csv", root) == (root / "sub" / "ok.csv").resolve()

    refused = ["../secret.csv", "sub/../../secret.csv", str(outside),
               "\\\\server\\share\\secret.csv", "//server/share/secret.csv"]
    if os.name == "nt":
        refused += ["C:\\Windows\\win.ini", "sub/ok.csv:hidden"]
    for candidate in refused:
        with pytest.raises(netguard.PathRefused) as caught:
            netguard.confine_path(candidate, root)
        assert caught.value.code == "E_DATA_PATH_OUTSIDE_ROOT", candidate

    link = root / "escape.csv"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let this user create a symlink")
    with pytest.raises(netguard.PathRefused):
        netguard.confine_path(link, root)


# ============================================================ connections (D215, D216)
def _connections_file(tmp_path, connections, **top):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"format": 1, "connections": connections, **top}),
                    encoding="utf-8")
    return path


def test_a_connections_file_defines_where_a_live_source_may_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("PLANT_MES_TOKEN", "t0k3n-Canary")
    monkeypatch.setenv("LINE_PASSWORD", "p4ss-Canary")
    (tmp_path / "plant-ca.pem").write_text("not read here", encoding="utf-8")
    found = netguard.load_connections(_connections_file(tmp_path, {
        "mes": {"kind": "rest", "url": "https://MES.plant.example/api/v2/", "auth": "bearer",
                "secret": {"env": "PLANT_MES_TOKEN"}, "root": "/orders"},
        "line": {"kind": "mqtt", "url": "mqtts://10.20.0.8", "private": True, "username": "twin",
                 "secret": {"env": "LINE_PASSWORD"}, "ca_file": "plant-ca.pem",
                 "root": "spBv1.0/Plant1/#", "limits": {"events": 500}},
        "press": {"kind": "opcua", "url": "opc.tcp://10.20.0.15", "private": True}}))
    mes, line, press = found["mes"], found["line"], found["press"]
    # The address is rebuilt from its parsed parts, the default port filled in.
    assert (mes.url, mes.root, mes.auth) == ("https://mes.plant.example:443/api/v2", "/orders",
                                             "bearer")
    assert (line.url, line.private, line.username) == ("mqtts://10.20.0.8:8883", True, "twin")
    assert line.ca_file == str(tmp_path / "plant-ca.pem")       # beside the file
    assert (line.limit("events"), line.limit("keys")) == (500, 10_000)
    assert (press.url, press.encrypted, mes.encrypted, press.use) == (
        "opc.tcp://10.20.0.15:4840", False, True, "read")
    assert mes.secret.reveal() == "t0k3n-Canary"


def test_a_secret_cannot_be_printed_pickled_copied_or_serialised(tmp_path, monkeypatch):
    import copy
    import pickle

    monkeypatch.setenv("PLANT_MES_TOKEN", "t0k3n-Canary")
    conn = netguard.load_connections(_connections_file(tmp_path, {
        "mes": {"kind": "rest", "url": "https://mes.plant.example", "auth": "basic",
                "username": "twin", "secret": {"env": "PLANT_MES_TOKEN"}}}))["mes"]
    policy = NetworkPolicy(connections={"mes": conn})
    for text in (repr(conn), str(conn), repr(policy), f"{conn.secret}", repr([conn.secret]),
                 netguard.describe_connection(conn)):
        assert "t0k3n" not in text
    assert "<secret>" in repr(conn)
    for leak in (lambda: pickle.dumps(policy), lambda: copy.deepcopy(conn),
                 lambda: json.dumps(conn.secret), lambda: json.dumps(vars(conn))):
        with pytest.raises(TypeError):
            leak()


# (what is wrong, the connection): each one a coded refusal that names the connection.
_BAD_CONNECTIONS = [
    ("unknown kind", {"kind": "kafka", "url": "https://h"}),
    ("verify cannot be switched off", {"kind": "rest", "url": "https://h", "verify": False}),
    ("another kind's key", {"kind": "mqtt", "url": "mqtts://h", "auth": "bearer"}),
    ("wrong scheme", {"kind": "rest", "url": "ftp://h/x"}),
    ("mqtt scheme on rest", {"kind": "rest", "url": "mqtts://h"}),
    ("no host", {"kind": "rest", "url": "https:///x"}),
    ("a password in the URL", {"kind": "rest", "url": "https://u:p@h/x"}),
    ("a user in the URL", {"kind": "mqtt", "url": "mqtts://u@h"}),
    ("a query in the URL", {"kind": "rest", "url": "https://h/x?key=1"}),
    ("a fragment in the URL", {"kind": "rest", "url": "https://h/x#f"}),
    ("a path on a broker", {"kind": "mqtt", "url": "mqtts://h/topic"}),
    ("a literal secret", {"kind": "rest", "url": "https://h", "auth": "bearer",
                          "secret": "hunter2-Canary"}),
    ("a secret that is not set", {"kind": "rest", "url": "https://h", "auth": "bearer",
                                  "secret": {"env": "NOT_SET_ANYWHERE"}}),
    ("a keyring secret without the product", {"kind": "rest", "url": "https://h",
                                              "auth": "bearer", "secret": {"keyring": "mes"}}),
    ("a secret from a file", {"kind": "rest", "url": "https://h", "auth": "bearer",
                              "secret": {"file": "token.txt"}}),
    ("auth without a secret", {"kind": "rest", "url": "https://h", "auth": "bearer"}),
    ("basic without a user", {"kind": "rest", "url": "https://h", "auth": "basic",
                              "secret": {"env": "SET"}}),
    ("a header that frames the request", {"kind": "rest", "url": "https://h", "auth": "header",
                                          "header": "Transfer-Encoding", "secret": {"env": "SET"}}),
    ("a header with a line break", {"kind": "rest", "url": "https://h", "auth": "header",
                                    "header": "X-Key\r\nHost", "secret": {"env": "SET"}}),
    ("a password without a user", {"kind": "mqtt", "url": "mqtts://h", "secret": {"env": "SET"}}),
    ("a CA that is not there", {"kind": "mqtt", "url": "mqtts://h", "ca_file": "nope.pem"}),
    ("a limit over its ceiling", {"kind": "mqtt", "url": "mqtts://h",
                                  "limits": {"packet": 4_194_305}}),
    ("an unknown limit", {"kind": "mqtt", "url": "mqtts://h", "limits": {"threads": 2}}),
    ("a limit that is not a count", {"kind": "mqtt", "url": "mqtts://h", "limits": {"rate": 0}}),
    ("security without certificates", {"kind": "opcua", "url": "opc.tcp://h",
                                       "security": "Basic256Sha256"}),
    ("an unknown security policy", {"kind": "opcua", "url": "opc.tcp://h",
                                    "security": "Basic128Rsa15"}),
    ("an unknown use", {"kind": "mqtt", "url": "mqtts://h", "use": "publish"}),
    # A secret pasted where a key or a limit's name belongs must not be echoed back (M8).
    ("a secret mistyped as a key", {"kind": "rest", "url": "https://h", "hunter2-Canary": True}),
    ("a secret mistyped as a limit", {"kind": "mqtt", "url": "mqtts://h",
                                      "limits": {"hunter2-Canary": 5}}),
    ("not an object", "https://h"),
]


@pytest.mark.parametrize("what, spec", _BAD_CONNECTIONS, ids=[c[0] for c in _BAD_CONNECTIONS])
def test_a_connections_file_that_is_wrong_is_refused_whole(what, spec, tmp_path, monkeypatch):
    monkeypatch.setenv("SET", "s3t-Canary")
    with pytest.raises(netguard.ConnectionsError) as caught:
        netguard.load_connections(_connections_file(tmp_path, {"bad": spec}))
    error = caught.value
    assert error.code == "E_CONN_FORMAT" and "'bad'" in error.message and error.hint
    assert "Canary" not in error.message + error.hint
    assert isinstance(error, ValueError) and isinstance(error, netguard.GuardError)


def test_a_file_that_is_not_a_connections_file_is_refused(tmp_path):
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "v2.json").write_text(json.dumps({"format": 2, "connections": {}}),
                                      encoding="utf-8")
    for path in (tmp_path / "list.json", tmp_path / "broken.json", tmp_path / "missing.json",
                 tmp_path / "v2.json", _connections_file(tmp_path, {}, verify=False),
                 _connections_file(tmp_path, [])):
        with pytest.raises(netguard.ConnectionsError) as caught:
            netguard.load_connections(path)
        assert caught.value.code == "E_CONN_FORMAT"


def test_the_products_store_resolves_a_secret_the_engine_cannot(tmp_path):
    asked = []

    def store(where, key):
        asked.append((where, key))
        return {"mes-token": "fr0m-store"}.get(key)

    spec = {"kind": "rest", "url": "https://h", "auth": "bearer", "secret": {"keyring": "mes-token"}}
    conn = netguard.load_connections(_connections_file(tmp_path, {"mes": spec}), store)["mes"]
    assert conn.secret.reveal() == "fr0m-store" and asked == [("keyring", "mes-token")]
    spec["secret"] = {"keyring": "absent"}
    with pytest.raises(netguard.ConnectionsError):
        netguard.load_connections(_connections_file(tmp_path, {"mes": spec}), store)


def test_a_live_source_gets_only_the_connection_the_caller_defined_for_its_kind(tmp_path):
    found = netguard.load_connections(_connections_file(tmp_path, {
        "line": {"kind": "mqtt", "url": "mqtts://broker.plant.example"},
        "advice": {"kind": "mqtt", "url": "mqtts://broker.plant.example", "use": "outbox"}}))
    policy = NetworkPolicy(connections=found)
    source = {"name": "s", "kind": "mqtt", "connection": "line", "location": "a/#"}
    assert netguard.connection_for(source, policy) is found["line"]
    for change, said in (({"connection": "nope"}, "no connection named 'nope'"),
                         ({"connection": ""}, "no connection named ''"),
                         ({"kind": "rest"}, "is a mqtt connection"),
                         ({"connection": "advice"}, "kept for an outbox")):
        with pytest.raises(netguard.NetworkRefused) as caught:
            netguard.connection_for({**source, **change}, policy)
        assert caught.value.code == "E_DATA_CONNECTION" and said in caught.value.message
    with pytest.raises(netguard.NetworkRefused):        # the widest old policy defines none
        netguard.connection_for(source, NetworkPolicy(allow_network=True, allow_private=True))


def test_the_connections_flag_is_the_only_way_a_policy_gets_connections(tmp_path, monkeypatch):
    import argparse

    parser = argparse.ArgumentParser()
    netguard.add_arguments(parser)
    assert netguard.policy_from_args(parser.parse_args([])).connections is None
    path = _connections_file(tmp_path, {"line": {"kind": "mqtt", "url": "mqtts://h"}})
    # No environment variable names the file: nothing ambient reaches an agent surface.
    for name in ("NAGASIM_CONNECTIONS", "NAGASIM_DATA_CONNECTIONS"):
        monkeypatch.setenv(name, str(path))
    assert netguard.policy_from_args(parser.parse_args([])).connections is None
    policy = netguard.policy_from_args(parser.parse_args(["--connections", str(path)]))
    assert list(policy.connections) == ["line"] and not policy.allow_network
    with pytest.raises(ValueError):
        netguard.policy_from_args(parser.parse_args(["--connections", str(tmp_path / "no")]))


# ============================================================ the pinned socket (D217)
@pytest.fixture
def listener(tls_files):
    """`listener(tls=False) -> port`: a server on 127.0.0.1 that greets every client with
    b"hi", over TLS (the `plant.test` certificate) when asked. Closed at teardown."""
    import ssl
    import threading

    servers = []

    def serve(server, context):
        while True:
            try:
                client, _peer = server.accept()
            except OSError:
                return
            try:
                if context is not None:
                    client = context.wrap_socket(client, server_side=True)
                client.sendall(b"hi")
            except OSError:
                pass
            finally:
                client.close()

    def start(tls=False) -> int:
        context = None
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls_files)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        servers.append(server)
        threading.Thread(target=serve, args=(server, context), daemon=True).start()
        return server.getsockname()[1]

    yield start
    for server in servers:
        server.close()


def _conn(**fields) -> netguard.Connection:
    base = {"name": "line", "kind": "mqtt", "scheme": "mqtt", "host": "plant.test", "port": 1883}
    return netguard.Connection(**{**base, **fields})


def test_the_address_that_was_judged_is_the_address_that_is_dialled(listener, monkeypatch):
    """The pin. The resolver is asked once; if it would answer differently a second time
    (DNS rebinding), nobody asks, and the socket goes to the address that was vetted."""
    port = listener()
    asked = []

    def rebinding(host, port_, *args, **kwargs):
        asked.append(host)
        address = "127.0.0.1" if len(asked) == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port_ or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    with netguard.open_connection(_conn(port=port, private=True)) as sock:
        assert sock.recv(2) == b"hi" and sock.getpeername() == ("127.0.0.1", port)
    assert asked == ["plant.test"]


# (the host's addresses, private?, expected code): every address is judged, none dialled.
_ADDRESS_CASES = [
    (["127.0.0.1"], False, "E_DATA_URL_PRIVATE"),
    (["192.168.1.10"], False, "E_DATA_URL_PRIVATE"),
    (["100.64.1.1"], False, "E_DATA_URL_PRIVATE"),
    (["93.184.216.34", "10.0.0.7"], False, "E_DATA_URL_PRIVATE"),      # public AND private
    (["169.254.169.254"], True, "E_DATA_URL_PRIVATE"),                 # never, even "private"
    (["224.0.0.1"], True, "E_DATA_URL_PRIVATE"),
    (["0.0.0.0"], True, "E_DATA_URL_PRIVATE"),
    (["::ffff:169.254.169.254"], True, "E_DATA_URL_PRIVATE"),
]


@pytest.mark.parametrize("addresses, private, code", _ADDRESS_CASES)
def test_a_connection_is_refused_by_address_before_any_socket_opens(addresses, private, code,
                                                                    monkeypatch):
    monkeypatch.setitem(HOSTS, "plant.test", addresses)
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *_args: pytest.fail("a refused address was dialled"))
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.open_connection(_conn(private=private))
    assert caught.value.code == code and "'line'" in caught.value.message


def test_a_credential_never_travels_in_plain_text_to_another_machine(listener, monkeypatch):
    """Matteo, 2026-10-08: refused, with no override. Anonymous plain reads work, and so
    does a credential to this machine."""
    secret = netguard.Secret("p4ss-Canary")
    monkeypatch.setitem(HOSTS, "plant.test", ["10.20.0.8"])
    dialled = []
    monkeypatch.setattr(socket.socket, "connect", lambda _sock, address: dialled.append(address))
    for conn in (_conn(private=True, username="twin", secret=secret),
                 _conn(kind="rest", scheme="http", port=80, private=True, auth="bearer",
                       secret=secret),
                 _conn(kind="opcua", scheme="opc.tcp", port=4840, private=True, username="twin",
                       secret=secret)):
        with pytest.raises(netguard.NetworkRefused) as caught:
            netguard.open_connection(conn)
        assert caught.value.code == "E_DATA_PLAINTEXT_CREDENTIAL"
        assert "no setting" in caught.value.hint and "Canary" not in caught.value.message
    assert dialled == []
    netguard.open_connection(_conn(private=True)).close()              # anonymous: allowed
    assert dialled == [("10.20.0.8", 1883)]
    # An encrypted OPC UA channel carries its password inside the channel.
    assert netguard.vet_connection(_conn(kind="opcua", scheme="opc.tcp", private=True,
                                         username="twin", secret=secret,
                                         security="Basic256Sha256"))
    monkeypatch.undo()
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))])
    port = listener()
    with netguard.open_connection(_conn(port=port, private=True, username="twin",
                                        secret=secret)) as sock:
        assert sock.recv(2) == b"hi"                                   # loopback: allowed


def test_tls_is_verified_against_the_host_name_and_cannot_be_turned_off(listener, tls_files,
                                                                        monkeypatch):
    port = listener(tls=True)
    for name in ("plant.test", "other.test"):
        monkeypatch.setitem(HOSTS, name, ["127.0.0.1"])
    good = _conn(scheme="mqtts", port=port, private=True, ca_file=tls_files[0])
    with netguard.open_connection(good) as sock:
        assert sock.recv(2) == b"hi" and sock.version().startswith("TLS")
    # The same server under a name its certificate does not carry; then with no private CA
    # named, so the system store decides, and it has never heard of this certificate.
    for conn in (_conn(scheme="mqtts", host="other.test", port=port, private=True,
                       ca_file=tls_files[0]),
                 _conn(scheme="mqtts", port=port, private=True)):
        with pytest.raises(netguard.NetworkRefused) as caught:
            netguard.open_connection(conn)
        assert caught.value.code == "E_DATA_TLS" and caught.value.__cause__ is None
    # A TLS client that meets a server speaking plain text is refused the same way.
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.open_connection(_conn(scheme="mqtts", port=listener(), private=True,
                                       ca_file=tls_files[0]))
    assert caught.value.code == "E_DATA_TLS"
    # Nothing a connection can say turns verification off (`_BAD_CONNECTIONS` has the file's
    # half), and the Connection has no field for it.
    assert not [f for f in netguard.Connection.__dataclass_fields__
                if "verify" in f or "insecure" in f or "proxy" in f]


def test_a_connection_nobody_answers_is_a_coded_failure(monkeypatch):
    monkeypatch.setitem(HOSTS, "plant.test", ["127.0.0.1"])
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()                                      # nothing listens there now
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.open_connection(_conn(port=port, private=True), timeout=2)
    assert caught.value.code == "E_DATA_READ" and "'line'" in caught.value.message


def test_the_consent_line_says_what_a_model_may_ask_for_and_the_hint_names_a_real_place():
    """M9: the line a human approves shows the connection's root, or that it has none. G4:
    the hint of a connection refusal names the editor's dialog; there is no Preferences."""
    base = {"name": "line", "kind": "mqtt", "scheme": "mqtts", "host": "localhost", "port": 8883}
    assert netguard.describe_connection(netguard.Connection(**base, root="spBv1.0/Plant1/#")) == \
        "line: mqtts://localhost:8883 -> 127.0.0.1, TLS, anonymous, root spBv1.0/Plant1/#"
    assert netguard.describe_connection(netguard.Connection(**base)).endswith(
        "anonymous, no root (anything it serves)")
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.connection_for({"name": "s", "kind": "mqtt", "connection": "x"}, NetworkPolicy())
    assert "Preferences" not in caught.value.hint and "connections dialog" in caught.value.hint
    assert not hasattr(netguard, "scrub")               # had no caller: a body is never quoted


# ============================================================ the sql reader's URL (review of ambient credentials)
# A database driver reads far more of a URL than its host. Each of these makes a driver do
# something the caller never allowed THIS model: go to another host than the one that was
# vetted, take credentials from a service or password file, read a local file, load code.
_SQL_PARAMETERS_REFUSED = [
    "postgresql://u@public.example.com/db?host=10.0.0.5",
    "postgresql://u@public.example.com/db?hostaddr=169.254.169.254",
    "postgresql://u@public.example.com/db?service=prod",
    "postgresql://u@public.example.com/db?passfile=/home/me/.pgpass",
    "postgresql://u@public.example.com/db?sslmode=disable",
    "postgresql://u@public.example.com/db?sslmode=verify-full&sslrootcert=/tmp/ca.pem",
    "postgresql://u@public.example.com/db?options=-c%20search_path%3Dx",
    "mysql+pymysql://u@public.example.com/db?read_default_file=/etc/mysql/my.cnf",
    "mysql+pymysql://u@public.example.com/db?read_default_group=client",
    "mysql+pymysql://u@public.example.com/db?local_infile=1",
    "mysql+pymysql://u@public.example.com/db?unix_socket=/var/run/mysqld/mysqld.sock",
    "mssql+pyodbc://u@public.example.com/db?odbc_connect=DSN%3Dprod",
    "mssql+pyodbc://u@public.example.com/db?driver=ODBC+Driver+18&TrustServerCertificate=yes",
    "postgresql://u@public.example.com/db?plugin=geoalchemy2",
    "postgresql://u@public.example.com/db?connect_timeout=5&HOST=10.0.0.5",
    "postgresql://u@public.example.com/db?s3cr3t-Canary-Value=1",
]


@pytest.mark.parametrize("url", _SQL_PARAMETERS_REFUSED)
def test_a_sql_url_carries_no_parameter_the_caller_did_not_allow(url):
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.check_sql_url(url, ON)
    assert caught.value.code == "E_DATA_URL_PARAMETER"
    assert "Canary" not in caught.value.message + caught.value.hint


def test_the_few_sql_url_parameters_that_cannot_redirect_or_weaken_are_allowed():
    for url in ("postgresql://u@public.example.com/db",
                "postgresql://u@public.example.com:5432/db?connect_timeout=5&application_name=twin",
                "postgresql+psycopg2://u@public.example.com/db?sslmode=verify-full",
                "postgresql://u@public.example.com/db?sslmode=require",
                "mysql+pymysql://u@public.example.com/db?charset=utf8mb4"):
        netguard.check_sql_url(url, ON)
    # ... and what was refused before still is: no host (the driver would take one from the
    # environment or a local socket), a list of hosts, a file.
    for url, code in (("postgresql:///db", "E_DATA_URL_HOST"), ("postgresql://u@/db", "E_DATA_URL_HOST"),
                      ("postgresql://localhost/db", "E_DATA_URL_PRIVATE"),
                      ("sqlite:///x.db?mode=rwc", "E_DATA_URL_SCHEME")):
        with pytest.raises(netguard.NetworkRefused) as caught:
            netguard.check_sql_url(url, ON)
        assert caught.value.code == code, url
