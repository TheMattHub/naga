"""The probe of `asyncua` (PHASE7 7.8, D223): what must be true of the library before an
OPC UA reader is built on it, and stays true of the version installed.

Three questions, one test each: its licence and what it pulls in; that its client dials
only the address it is given, whatever the server says its endpoint is; that the size of
what it accepts from a server can be asked for. The whole file skips where the optional
extra `nagasim[opcua]` is not installed; one CI job installs it.
"""
import asyncio
import re
from importlib import metadata

import pytest

asyncua = pytest.importorskip("asyncua", reason="the optional extra nagasim[opcua] is not installed")

# What the library may depend on. A name that is not here is a new dependency of the
# engine's optional extra and is reviewed before it is added to this list.
KNOWN_DEPENDENCIES = {"aiofiles", "aiosqlite", "anyio", "cryptography", "importlib-metadata",
                      "pyopenssl", "python-dateutil", "pytz", "sortedcontainers",
                      "typing-extensions", "wait-for2"}


def test_its_licence_is_one_an_optional_extra_of_an_apache_engine_may_have():
    """LGPL-3.0: used as an optional, separately installed, unmodified library. Anything
    else (GPL, a missing licence) is a decision for the owner, not for a reader."""
    found = metadata.metadata("asyncua")
    text = " ".join([found.get("License") or "", found.get("License-Expression") or "",
                     *(found.get_all("Classifier") or [])])
    assert "LGPL" in text or "Lesser General Public License" in text, text
    needed = {re.split(r"[ ;<>=!~\[(]", line, maxsplit=1)[0].lower().replace("_", "-")
              for line in metadata.requires("asyncua") or [] if "extra ==" not in line}
    assert needed <= KNOWN_DEPENDENCIES, sorted(needed - KNOWN_DEPENDENCIES)


def test_its_client_dials_only_the_address_it_is_given(plant_net):
    """A server that names another host as its endpoint is not followed there: the reader
    hands the client a vetted literal address, and `plant_net` fails the test if a socket
    goes anywhere else or the resolver is asked for the name the server gave."""
    import socket

    asked = []
    resolve = socket.getaddrinfo
    socket.getaddrinfo = lambda host, *args, **kwargs: asked.append(host) or resolve(
        host, *args, **kwargs)

    async def run():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = asyncua.Server()
        await server.init()
        server.set_endpoint(f"opc.tcp://elsewhere.plant.test:{port}/plant")
        server.socket_address = ("127.0.0.1", port)
        index = await server.register_namespace("urn:naga:test")
        node = await server.nodes.objects.add_variable(index, "Speed", 12.5)
        async with server:
            client = asyncua.Client(url=f"opc.tcp://127.0.0.1:{port}/plant", timeout=5)
            async with client:
                return await client.get_node(node.nodeid).read_value()

    try:
        assert asyncio.run(run()) == 12.5
    finally:
        socket.getaddrinfo = resolve
    assert "elsewhere.plant.test" not in asked


def test_the_size_of_what_it_accepts_can_be_asked_for_through_public_attributes():
    """The reader sets `max_messagesize` and `max_chunkcount` (sent in the Hello), and the
    session check compares what `get_node` parsed with what was approved."""
    client = asyncua.Client(url="opc.tcp://127.0.0.1:4840")
    assert client.max_messagesize == 0 and client.max_chunkcount == 0       # the default: none
    parsed = client.get_node("ns=2;s=Press.Speed").nodeid
    assert (parsed.NamespaceIndex, parsed.NodeIdType.name, parsed.Identifier) == (2, "String", "Press.Speed")
