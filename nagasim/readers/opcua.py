"""OPC UA reader (PHASE7 7.8, D223): data changes of listed nodes, through `asyncua`.

    {"name": "press", "kind": "opcua", "connection": "press",
     "location": "ns=2;s=Press.Speed\\nns=2;s=Press.State",
     "options": {"interval_ms": 1000}}

`location` holds node ids, one per line, each starting with the connection's `root`. A key
is the node id; a value is the node's scalar Value with its SourceTimestamp; a Bad status
is a None value.

The protocol is too large to write, so this is the one reader built on a library, the
optional extra `nagasim[opcua]` (LGPL-3.0, imported lazily, never bundled). What keeps it
inside the rules of the other readers:

* the address is vetted by `netguard.vet_connection` and the library is handed the vetted
  address as a literal, so it resolves nothing; the endpoint URLs a server names are not
  followed;
* security is `None` with an anonymous user, or `Basic256Sha256` with `SignAndEncrypt`,
  the server's certificate pinned (the exact certificate, by the library), a client
  certificate, and three checks the library does not make unasked: both certificates'
  dates (ours, before dialling) and the server's application URI (its validator). A password is sent only inside
  the encrypted channel, or to this machine (`E_DATA_PLAINTEXT_CREDENTIAL` otherwise);
* this module calls `get_node` (which parses a text and asks the server nothing),
  `create_subscription`, `subscribe_data_change` and nothing else on a session: no write, no
  method call, no history read, no browse; and the node the library parsed must be the node
  that was checked against the root, or the session is closed;
* the connection's `packet` limit is sent to the server as the client's message limit (public
  attributes only). The library adopts whatever the server answers, so the cap holds for a
  server that honours the protocol; one that answers "no limit" or more is reported once
  (`W_LIVE_LIMIT`) and still read. A faulty or hostile server can then send more than the
  limit, which can stop the twin; it reads only, nothing is executed or written.

`tests/test_opcua_probe.py` holds what was checked about the library before this was built
on it (its licence, that it dials only the address it is given, that the public
message limit attributes exist); it runs where the extra is installed.
"""
from __future__ import annotations

import asyncio
import re
from datetime import timezone
from pathlib import Path

from nagasim import netguard, readers
from nagasim.sources import ReadError

__all__ = ["OPTIONS", "check", "subscribe"]

OPTIONS = {"interval_ms": (1000, (100, 3_600_000)), "settle": (2.0, (0.0, 10.0))}
MAX_NODES, MAX_NODE_BYTES = 1000, 1024
# One identifier per id. A string id runs to the end of the line (it may hold ";"); the other
# kinds stop at a ";", because the library reads "ns=2;i=5;ns=0;i=2258" as node 2258 of
# namespace 0: whatever follows the first identifier would replace it.
_NODE_ID = re.compile(r"^(?:ns=(\d+);)?(?:(s)=(\S.*)|([igb])=([^;\s]+))$")
_NAMESPACE = re.compile(r"^ns=(\d+);$")
SUPPORTED = ">=2.1,<3"      # the asyncua versions whose node-id parsing was read
_SCALARS = (bool, int, float, str, type(None))
_HINT = "check the connection and that the server lets this client read the nodes"


def _parts(node: str):
    """(namespace, kind, value) of a node id, or None when it is not one identifier."""
    found = _NODE_ID.match(node)
    if not found:
        return None
    kind = found.group(2) or found.group(4)
    value = found.group(3) if kind == "s" else found.group(5)
    if kind == "i":
        if not value.isascii() or not value.isdigit():
            return None
        value = int(value)
    return int(found.group(1) or 0), kind, value


def _inside(parts, root: str) -> bool:
    """Whether a parsed node id is under the connection's root: a whole namespace (`ns=2;`),
    the string ids that start with a prefix (`ns=2;s=Press.`), or one exact node of another
    kind. Compared part by part, never as text: `ns=2;i=1` is not a prefix of `ns=2;i=10`."""
    if not root:
        return True
    namespace = _NAMESPACE.match(root)
    if namespace:
        return parts[0] == int(namespace.group(1))
    wanted = _parts(root)
    if wanted is None or parts[:2] != wanted[:2]:
        return False
    return parts[2].startswith(wanted[2]) if parts[1] == "s" else parts[2] == wanted[2]


def _same(parts, node_id) -> bool:
    """Whether the id the library parsed is the node that was approved: namespace, kind and
    value (a byte-string id by namespace and kind only: its text has two spellings). The root
    was judged on OUR reading of the text; this holds whatever a version of the library
    makes of the same text."""
    kind = {"String": "s", "Guid": "g", "ByteString": "b"}.get(node_id.NodeIdType.name, "i")
    if parts is None or (node_id.NamespaceIndex, kind) != parts[:2]:
        return False
    if kind == "b":
        return True
    if kind == "g":
        return str(node_id.Identifier).lower() == parts[2].lower()
    return node_id.Identifier == parts[2]


def check(source: dict, conn: netguard.Connection) -> dict:
    """The source's options and its node ids, or the refusal. No socket is opened."""
    options = readers.checked_options(source, OPTIONS)
    nodes = [line.strip() for line in str(source.get("location") or "").splitlines()
             if line.strip()]
    if not nodes or len(nodes) > MAX_NODES or any(
            _parts(node) is None or len(node.encode("utf-8", "replace")) > MAX_NODE_BYTES
            or not _inside(_parts(node), conn.root.strip()) for node in nodes):
        raise ReadError("E_DATA_SOURCE_LOCATION",
                        f"source '{source.get('name')}': its location must be node ids "
                        f"(ns=2;s=Name), one per line, inside what connection '{conn.name}' "
                        f"allows",
                        "the connection's \"root\" is set by the human who owns the "
                        "connections file")
    options["nodes"] = nodes
    return options


def subscribe(source: dict, conn: netguard.Connection, options: dict, sink, stop) -> None:
    """Hold one session with one subscription until *stop* is set or the link ends. The
    library's event loop lives and dies inside this call, on the feed's thread."""
    try:
        import asyncua  # noqa: F401
    except ImportError:
        raise ReadError("E_DATA_READ", "an opcua source needs the optional asyncua package",
                        "pip install \"nagasim[opcua]\"") from None
    address = netguard.vet_connection(conn)[0]
    host = f"[{address}]" if address.version == 6 else str(address)
    asyncio.run(_session(f"opc.tcp://{host}:{conn.port}{conn.path}", conn, options, sink, stop))


class _Changes:
    """The library's handler: one call per data change, on its event loop."""

    def __init__(self, sink):
        self.sink = sink

    def datachange_notification(self, node, value, data) -> None:
        item = data.monitored_item.Value
        key = node.nodeid.to_string()
        moment = item.SourceTimestamp
        if moment is not None:
            moment = moment.replace(tzinfo=moment.tzinfo or timezone.utc).timestamp()
        if item.StatusCode is not None and not item.StatusCode.is_good():
            return self.sink.accept(key, None, moment)
        if not isinstance(value, _SCALARS):
            return self.sink.count("type")      # an array, a structure, bytes: not read
        return self.sink.accept(key, value, moment)


def _agreed(client) -> tuple[int, int] | None:
    """(message size, chunk count) the server acknowledged, or None when that cannot be
    read. A private field, read for a diagnostic only: a library change turns the warning
    in `_session` into "cannot be confirmed", and never stops the reading."""
    try:
        limits = client.uaclient.protocol._connection._limits
        return int(limits.max_message_size), int(limits.max_chunk_count)
    except Exception:
        return None


def _application_uri(path: str, what: str, conn: netguard.Connection) -> str | None:
    """The application URI a certificate of the caller's carries, after checking that the
    certificate can be used today. The library pins the server's certificate (the exact one,
    and the channel is encrypted to its key) but looks at no date: an expired one would be
    used. Ours to check, on the caller's files, before anything is dialled."""
    from datetime import datetime

    from cryptography import x509

    try:
        data = Path(path).read_bytes()
        loaded = (x509.load_pem_x509_certificate if data.lstrip().startswith(b"-----")
                  else x509.load_der_x509_certificate)(data)
    except (OSError, ValueError):
        raise ReadError("E_DATA_TLS", f"connection '{conn.name}': its {what} cannot be read as "
                        f"a certificate", "name a PEM or DER certificate in the connections "
                        "file") from None
    now = datetime.now(timezone.utc)
    if not loaded.not_valid_before_utc <= now <= loaded.not_valid_after_utc:
        raise ReadError("E_DATA_TLS", f"connection '{conn.name}': its {what} is not valid today "
                        f"(valid {loaded.not_valid_before_utc:%Y-%m-%d} to "
                        f"{loaded.not_valid_after_utc:%Y-%m-%d})",
                        "renew the certificate and name the new file in the connections file")
    try:
        names = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None
    return next(iter(names.get_values_for_type(x509.UniformResourceIdentifier)), None)


async def _session(url: str, conn: netguard.Connection, options: dict, sink, stop) -> None:
    from asyncua import Client, ua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

    client = Client(url=url, timeout=netguard.CONNECT_TIMEOUT)
    # The public attributes go to the server in the Hello. The library adopts what the server
    # answers, a 0 ("no limit") included, so the cap holds only for a server that honours it;
    # `_agreed` below says when it did not.
    client.max_messagesize = conn.limit("packet")
    client.max_chunkcount = max(1, client.max_messagesize // 65_535 + 1)
    if conn.security == "Basic256Sha256":
        from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions

        _application_uri(conn.server_cert, "server certificate", conn)
        # A server judges the client's certificate by the application URI in it.
        client.application_uri = _application_uri(conn.client_cert, "client certificate", conn) \
            or client.application_uri
        # The library's own checks of what the SERVER presents, which it runs only when asked:
        # the dates again, and that the certificate's URI is the application the server says
        # it is. Both come before the session is activated, which is when a password is sent.
        client.certificate_validator = CertificateValidator(
            CertificateValidatorOptions.TIME_RANGE | CertificateValidatorOptions.URI
            | CertificateValidatorOptions.PEER_SERVER)
    try:
        if conn.security == "Basic256Sha256":
            await client.set_security(SecurityPolicyBasic256Sha256, certificate=conn.client_cert,
                                      private_key=conn.client_key,
                                      server_certificate=conn.server_cert,
                                      mode=ua.MessageSecurityMode.SignAndEncrypt)
        if conn.username:
            client.set_user(conn.username)
            if conn.secret is not None:
                client.set_password(conn.secret.reveal())   # the one place it is revealed
        await client.connect()
    except Exception as exc:
        # The class only: the text is the server's, or names a file of the caller's.
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' could not open a session "
                        f"({type(exc).__name__})", _HINT) from None
    agreed = _agreed(client)
    if agreed is None:
        # Not silence: a limit nobody can confirm is not one to rely on.
        sink.warn(f"connection '{conn.name}': this version of asyncua does not say what the "
                  f"server agreed to, so the message limit cannot be confirmed",
                  f"use an encrypted connection to a server you trust; the versions this "
                  f"reader was checked on: asyncua{SUPPORTED}")
    elif not (0 < agreed[0] <= client.max_messagesize and 0 < agreed[1] <= client.max_chunkcount):
        sink.warn(f"connection '{conn.name}': the server did not accept the message limit, so "
                  f"the size of what it sends is not bounded",
                  "use an encrypted connection to a server you trust, or lower the server's "
                  "own limits")
    nodes = [client.get_node(node) for node in options["nodes"]]
    strayed = [text for text, node in zip(options["nodes"], nodes)
               if not _same(_parts(text), node.nodeid)]
    if strayed:
        # Before anything is read or subscribed: the library read another node in the text.
        try:
            await client.disconnect()
        except Exception:
            pass
        raise ReadError("E_DATA_SOURCE_LOCATION",
                        f"connection '{conn.name}': this version of asyncua reads "
                        f"'{strayed[0]}' as another node than the one that was checked",
                        f"install a supported version: pip install \"asyncua{SUPPORTED}\"")
    try:
        subscription = await client.create_subscription(options["interval_ms"], _Changes(sink))
        await subscription.subscribe_data_change(nodes)
        sink.connected()
        while not stop.is_set():
            await asyncio.sleep(0.5)
            await client.check_connection()
    except Exception as exc:
        raise ReadError("E_DATA_READ", f"connection '{conn.name}' lost its session "
                        f"({type(exc).__name__})", _HINT) from None
    finally:
        try:
            await client.disconnect()
        except Exception:               # the link is already gone
            pass
