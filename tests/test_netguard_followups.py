"""Phase 15 FA: `netguard.read_caller_file` (one reader of a caller's file) and the
pinned HTTP(S) fetch (DNS rebinding, ARCHITECTURE section 8 #31). No real network."""
import json
import os
import socket

import pytest

from nagasim import netguard, plcwrite, refit


# ============================================================ read_caller_file
def test_a_caller_file_is_read_whole_and_under_its_cap(tmp_path):
    path = tmp_path / "f.json"
    path.write_bytes(b"x" * 10)
    assert netguard.read_caller_file(path, max_bytes=10) == b"x" * 10
    with pytest.raises(netguard.FileRefused, match="the refit file is over 9 bytes") as caught:
        netguard.read_caller_file(path, max_bytes=9, what="the refit file")
    assert caught.value.code == "E_FILE_REFUSED"


def test_a_missing_file_says_so_with_its_own_code(tmp_path):
    with pytest.raises(netguard.FileRefused) as caught:
        netguard.read_caller_file(tmp_path / "none.json", max_bytes=10)
    assert caught.value.code == "E_FILE_MISSING"


def test_a_folder_or_a_device_is_not_a_plain_file(tmp_path):
    for path in (tmp_path, "NUL" if os.name == "nt" else "/dev/null"):
        with pytest.raises(netguard.FileRefused) as caught:
            netguard.read_caller_file(path, max_bytes=10)
        assert caught.value.code == "E_FILE_REFUSED", path


def test_a_unc_path_or_a_stream_is_refused_before_anything_is_opened(monkeypatch):
    monkeypatch.setattr(os, "open", lambda *a, **k: pytest.fail("a refused path was opened"))
    refused = ["\\\\server\\share\\f.json", "//server/share/f.json", "/\\server\\share\\f"]
    if os.name == "nt":
        refused.append("C:\\data\\f.json:hidden")
    for path in refused:
        with pytest.raises(netguard.FileRefused, match="UNC|after its drive"):
            netguard.read_caller_file(path, max_bytes=10)


def test_a_symbolic_link_is_refused(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let this user create a symlink")
    with pytest.raises(netguard.FileRefused, match="symbolic link"):
        netguard.read_caller_file(link, max_bytes=100)


@pytest.mark.skipif(hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW refuses the link itself")
def test_without_o_nofollow_a_link_is_refused_by_lstat(tmp_path, monkeypatch):
    path = tmp_path / "f.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(os.path, "islink", lambda p: True)
    with pytest.raises(netguard.FileRefused, match="symbolic link"):
        netguard.read_caller_file(path, max_bytes=100)


def test_refit_plcwrite_and_connections_read_through_it(tmp_path, monkeypatch):
    """The three callers keep their own codes; the rules are the one reader's."""
    seen = []
    real = netguard.read_caller_file
    monkeypatch.setattr(netguard, "read_caller_file",
                        lambda path, **kw: seen.append(kw["what"]) or real(path, **kw))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (2 ** 21))
    with pytest.raises(refit.Refused, match="over"):
        refit._read(big)
    with pytest.raises(plcwrite.Refused, match="over") as caught:
        plcwrite.load(big)
    assert caught.value.code == "E_PLC_SETPOINTS"
    with pytest.raises(netguard.ConnectionsError, match="over") as caught:
        netguard.load_connections(big)
    assert caught.value.code == "E_CONN_FORMAT"
    assert plcwrite._lines(tmp_path / "no-audit.jsonl") == []        # missing: empty
    (tmp_path / "audit.jsonl").write_text(json.dumps({"a": 1}) + "\n", encoding="utf-8")
    assert plcwrite._lines(tmp_path / "audit.jsonl") == [{"a": 1}]
    assert seen[:3] == ["the refit file", "the setpoints file", f"the connections file '{big}'"]
    assert len(seen) == 5


# ============================================================ DNS pinning (section 8 #31)
ON = netguard.NetworkPolicy(allow_network=True)
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"


def _resolver(monkeypatch, answers: dict) -> list:
    """A fake resolver: the n-th lookup of a host gets its n-th answer (the last repeats),
    so a host can answer public to the check and private to the connection. A host not
    listed is a literal and resolves to itself. Returns the hosts asked, in order."""
    asked = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        chain = answers.get(host, [host])
        address = chain[min(asked.count(host), len(chain)) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return asked


@pytest.fixture
def dialled(monkeypatch):
    """What `_dial` is asked to connect to; it hands back one end of a socketpair whose
    other end (`peer`) has already answered `_OK`."""
    seen = {"addresses": []}

    def dial(addresses, port, timeout):
        seen["addresses"].append(([str(ip) for ip in addresses], port))
        ours, seen["peer"] = socket.socketpair()
        seen["peer"].sendall(_OK)
        return ours

    monkeypatch.setattr(netguard, "_dial", dial)
    yield seen
    if "peer" in seen:
        seen["peer"].close()


@pytest.mark.parametrize("scheme, rebound", [("http", "169.254.169.254"),
                                             ("https", "10.0.0.7"), ("http", "127.0.0.1")])
def test_a_rebinding_answer_between_check_and_connect_is_refused(scheme, rebound, monkeypatch):
    """Public to `check_url`, then a refused address to the connection: refused, and no
    socket is opened to anything."""
    asked = _resolver(monkeypatch, {"rebind.example.com": ["93.184.216.34", rebound]})
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("a socket was dialled"))
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.fetch(f"{scheme}://rebind.example.com/d.json", ON)
    assert caught.value.code == "E_DATA_URL_PRIVATE" and rebound in caught.value.message
    assert asked == ["rebind.example.com", "rebind.example.com"]


def test_the_socket_goes_to_the_judged_address_and_the_request_names_the_host(
        monkeypatch, dialled):
    _resolver(monkeypatch, {"public.example.com": ["93.184.216.34"]})
    assert netguard.fetch("http://public.example.com:8080/d.json", ON) == b"ok"
    assert dialled["addresses"] == [(["93.184.216.34"], 8080)]
    dialled["peer"].settimeout(1)
    sent = dialled["peer"].recv(65536)
    assert sent.startswith(b"GET /d.json HTTP/1.1\r\n")
    assert b"\r\nHost: public.example.com:8080\r\n" in sent


def test_https_keeps_the_host_name_for_sni_and_the_certificate(monkeypatch):
    """The pinned HTTPS connection dials the judged address and wraps that socket for the
    HOST NAME; the handler's context verifies certificates."""
    _resolver(monkeypatch, {"public.example.com": ["93.184.216.34"]})
    ours, theirs = socket.socketpair()
    dials = []
    monkeypatch.setattr(netguard, "_dial",
                        lambda addresses, port, timeout: dials.append((addresses, port)) or ours)
    wrapped = {}

    class Context:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.update(sock=sock, name=server_hostname)
            return sock

    conn = netguard._PinnedHTTPS("public.example.com", context=Context(), policy=ON)
    conn.connect()
    assert wrapped == {"sock": ours, "name": "public.example.com"}
    assert [(str(ip), port) for addresses, port in dials for ip in addresses] == \
        [("93.184.216.34", 443)]
    ours.close()
    theirs.close()
    request = netguard.urllib.request.Request("https://public.example.com/")
    seen = {}
    monkeypatch.setattr(netguard._PinnedHTTPSHandler, "do_open",
                        lambda self, cls, req, **kw: seen.update(kw))
    netguard._PinnedHTTPSHandler(ON).https_open(request)
    context = seen["context"]
    assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname


def test_a_redirect_hop_is_pinned_like_the_first(monkeypatch):
    """Each hop opens its own pinned connection: a target that rebinds after its
    `check_url` passed is refused at its connect."""
    _resolver(monkeypatch, {"next.example.com": ["93.184.216.35", "127.0.0.1"]})
    netguard.check_url("http://next.example.com/", ON)
    with pytest.raises(netguard.NetworkRefused, match="127.0.0.1"):
        netguard._PinnedHTTP("next.example.com", policy=ON).connect()


def test_dial_connects_to_a_literal_address_and_resolves_nothing(monkeypatch):
    import ipaddress

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("resolved again"))
    port = server.getsockname()[1]
    with netguard._dial([ipaddress.ip_address("127.0.0.1")], port, 2.0) as sock:
        assert sock.getpeername() == ("127.0.0.1", port)
    server.close()


def test_the_pinned_handlers_replace_urllibs_own():
    handlers = netguard._opener(ON).handlers
    assert any(isinstance(h, netguard._PinnedHTTPHandler) for h in handlers)
    assert not any(type(h) is netguard.urllib.request.HTTPHandler for h in handlers)
