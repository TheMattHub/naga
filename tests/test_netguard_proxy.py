"""`netguard.fetch` through the CALLER's proxy: https only, as a CONNECT tunnel to the
target's name, the target judged here first. A tiny CONNECT proxy and a TLS target run
on 127.0.0.1; a fake resolver makes the target names public. No real network."""
import base64
import socket
import ssl
import threading

import pytest

from nagasim import netguard

ON = netguard.NetworkPolicy(allow_network=True)
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "no_proxy", "all_proxy")


def _head(sock) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _pipe(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (a, b):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Proxy:
    """Answers CONNECT by relaying to *target_port* on 127.0.0.1, whatever name was asked
    (so a name the client judged public is reached on loopback); `refuse` answers 403.
    `heads` keeps every request head it was sent."""

    def __init__(self, target_port: int, refuse: bool = False):
        self.target_port, self.refuse, self.heads = target_port, refuse, []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            self.heads.append(_head(client).decode("latin-1"))
            if self.refuse:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                client.close()
                continue
            upstream = socket.create_connection(("127.0.0.1", self.target_port))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


@pytest.fixture
def target(tls_files):
    """The port of a TLS server on 127.0.0.1 with a certificate for `plant.test`."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*tls_files)
    server = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    _head(tls)
                    tls.sendall(_OK)
            except OSError:
                pass            # a client that refused the certificate

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def env(monkeypatch, tls_files):
    """No proxy variable but the ones a test sets; the test certificate trusted; target
    names resolve to public addresses unless a test maps them elsewhere."""
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    real_context = ssl.create_default_context
    monkeypatch.setattr(netguard.ssl, "create_default_context",
                        lambda *a, **k: real_context(cafile=tls_files[0]))
    answers = {"plant.test": "93.184.216.34", "other.test": "93.184.216.35"}
    real_resolve = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host in answers:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port or 0))]
        return real_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return answers


def test_https_goes_through_the_callers_proxy_as_a_tunnel_to_the_name(
        monkeypatch, env, target):
    proxy = _Proxy(target)
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:secret@127.0.0.1:{proxy.port}")
    assert netguard.fetch(f"https://plant.test:{target}/d.json", ON) == b"ok"
    head = proxy.heads[0]
    assert head.startswith(f"CONNECT plant.test:{target} HTTP/1.1\r\n")
    assert f"Proxy-Authorization: Basic {base64.b64encode(b'user:secret').decode()}" in head


def test_inside_the_tunnel_the_certificate_is_checked_by_the_target_name(
        monkeypatch, env, target):
    """The residual-risk defence: whatever the proxy reached, a server without a valid
    certificate for the asked name is not read."""
    proxy = _Proxy(target)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
    with pytest.raises(OSError, match="CERTIFICATE_VERIFY_FAILED|match"):
        netguard.fetch(f"https://other.test:{target}/d.json", ON)
    assert proxy.heads[0].startswith(f"CONNECT other.test:{target} ")


def test_no_proxy_takes_the_pinned_direct_path(monkeypatch, env, target):
    proxy = _Proxy(target)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
    monkeypatch.setenv("NO_PROXY", "plant.test")
    dials = []

    def dial(addresses, port, timeout):
        dials.append(([str(ip) for ip in addresses], port))
        raise ConnectionRefusedError("stub")

    monkeypatch.setattr(netguard, "_dial", dial)
    with pytest.raises(OSError):
        netguard.fetch(f"https://plant.test:{target}/d.json", ON)
    assert dials == [(["93.184.216.34"], target)] and proxy.heads == []


@pytest.mark.parametrize("answers", [["10.0.0.5"], ["93.184.216.36", "127.0.0.1"]])
def test_a_target_judged_private_is_refused_before_the_proxy_is_contacted(
        monkeypatch, env, target, answers):
    """Private outright, or public to `check_url` and loopback at the tunnel's connect."""
    proxy = _Proxy(target)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
    asked = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        address = answers[min(len(asked), len(answers)) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.fetch("https://intranet.test/d.json", ON)
    assert caught.value.code == "E_DATA_URL_PRIVATE" and answers[-1] in caught.value.message
    assert proxy.heads == []


def test_a_proxy_that_refuses_the_tunnel_is_named_without_its_credentials(
        monkeypatch, env, target):
    proxy = _Proxy(target, refuse=True)
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:secret@127.0.0.1:{proxy.port}")
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.fetch(f"https://plant.test:{target}/d.json", ON)
    message = caught.value.message
    assert caught.value.code == "E_DATA_PROXY"
    assert f"the proxy 127.0.0.1:{proxy.port}" in message and "403" in message
    assert "secret" not in message and "user" not in message


def test_plain_http_through_a_proxy_is_refused(monkeypatch, env, target):
    """The proxy would read and answer it, with no certificate to check: refused, and
    the proxy is never contacted."""
    proxy = _Proxy(target)
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
    with pytest.raises(netguard.NetworkRefused) as caught:
        netguard.fetch("http://plant.test/d.json", ON)
    assert caught.value.code == "E_DATA_PROXY" and "NO_PROXY" in caught.value.hint
    assert proxy.heads == []
