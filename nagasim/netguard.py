"""Guards for everything a MODEL FILE can point at: outbound URLs and file paths.

A model file is untrusted content (ARCHITECTURE 7.4). Its `data_sources` name
URLs and paths that some caller will eventually open, so every one of them goes
through this module first. Stdlib only -- `core/` and the CLI both import it,
and it must stay importable with no optional package installed.

Two halves, one rule each:

* `check_url` / `check_sql_url` / `fetch` -- outbound HTTP(S) and SQL. Network
  access is OFF unless a HUMAN turned it on (`NetworkPolicy.allow_network`,
  which only a CLI flag, a server start argument or a UI consent dialog sets).
  With it on, the URL must be http(s), carry no credentials, and resolve ONLY
  to public addresses: loopback, link-local (169.254.169.254 and friends),
  multicast, reserved and unspecified are always refused, and private /
  unique-local / CGNAT space is refused unless `allow_private`. That is the
  SSRF rule: an attacker who ships a model file must not be able to make the
  machine running it read a cloud metadata endpoint or an intranet host.
* `confine_path` -- every file a tool or a binding reads or writes on behalf of
  an agent stays under one root, symlinks resolved, with Windows UNC, device
  and alternate-data-stream paths refused outright.

RESIDUAL RISK (deliberate, documented rather than papered over):

* DNS rebinding. `check_url` resolves the host and vets every address, then
  `urlopen` resolves it AGAIN when it connects. Pinning the socket to the
  vetted address needs a custom `HTTPConnection` plus TLS hostname plumbing --
  far more than the few lines this is worth -- so a hostile resolver that
  answers differently within that window can still get one request through to
  an address this module would have refused. Redirects are re-vetted per hop,
  the response is size-capped, and nothing about the response reaches the model
  except parsed data, which bounds what such a request can achieve.
* A refusal is per address FAMILY of what the resolver returned at check time:
  a host answering with both a public and a private address is refused
  entirely, which is the safe direction.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

__all__ = ["NetworkPolicy", "GuardError", "NetworkRefused", "PathRefused", "check_url",
           "check_sql_url", "fetch", "confine_path", "is_unc_path", "host_of", "describe_url",
           "add_arguments", "policy_from_args", "Secret", "Connection", "ConnectionsError",
           "CONNECTION_KINDS", "LIMITS", "Register", "load_connections", "connection_for",
           "describe_connection", "vet_connection", "open_connection"]

MAX_REDIRECTS = 3
# The only parameters a `sql` location may carry in its query string (`check_sql_url`).
SQL_URL_PARAMETERS = frozenset({"connect_timeout", "application_name", "charset"})
_SQL_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
# Carrier-grade NAT. `is_private` covers it on some Python versions and not on
# others, so it is checked explicitly rather than trusted to the stdlib.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_HUMAN_HINT = ("only a human can allow this: pass --allow-network to the CLI or to "
               "`naga mcp`, or grant it in the editor's consent dialog")


class GuardError(Exception):
    """A coded refusal with a hint written for the HUMAN, never an instruction
    an agent could follow by itself."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


class NetworkRefused(GuardError):
    """An outbound request this policy does not permit."""


class PathRefused(GuardError):
    """A path outside the root it was confined to."""


@dataclass(frozen=True)
class NetworkPolicy:
    """What outbound access the CALLER grants. Never read from a model file."""

    allow_network: bool = False
    allow_private: bool = False
    allowed_hosts: frozenset[str] | None = None     # None: any public host
    max_bytes: int = 20_000_000
    timeout: float = 20.0
    # {name: Connection}: where a live source may connect (D215). Only `--connections FILE`
    # or the product's dialog fills it; a model names a connection and nothing else.
    connections: dict | None = None


# ---------------------------------------------------------------- URL checks
def _addresses(host: str, port: int | None) -> list:
    """Every address *host* resolves to, as `ipaddress` objects with IPv4-mapped
    IPv6 unwrapped -- so `::ffff:10.0.0.1` is checked as 10.0.0.1, and a decimal
    or hex host literal (`0x7f000001`) is checked as what it actually means."""
    try:
        infos = socket.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkRefused("E_DATA_URL_HOST", f"host '{host}' does not resolve ({exc})",
                             "check the source's URL") from None
    out = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).partition("%")[0])   # drop any zone id
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        out.append(ip)
    if not out:
        raise NetworkRefused("E_DATA_URL_HOST", f"host '{host}' resolves to no address",
                             "check the source's URL")
    return out


def _check_host(host: str | None, port: int | None, policy: NetworkPolicy) -> list:
    """The host half of every check: allow-list, then EVERY resolved address."""
    if not host:
        raise NetworkRefused("E_DATA_URL_HOST", "the URL has no hostname",
                             "write the source's location as http(s)://host/path")
    name = host.strip("[]").lower()
    if policy.allowed_hosts is not None and name not in {h.lower() for h in policy.allowed_hosts}:
        raise NetworkRefused("E_DATA_URL_HOST", f"host '{name}' is not one of the allowed hosts",
                             "the human running Naga can add it with --allow-host")
    addresses = _addresses(name, port)
    for ip in addresses:
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast \
                or ip.is_reserved:
            raise NetworkRefused(
                "E_DATA_URL_PRIVATE",
                f"host '{name}' resolves to {ip}, which is not a public address",
                "a data source may not reach this machine, the link-local metadata "
                "address or a multicast group")
        if not policy.allow_private and (ip.is_private or ip in _CGNAT):
            raise NetworkRefused(
                "E_DATA_URL_PRIVATE",
                f"host '{name}' resolves to the private address {ip}",
                "if that intranet host really is the one you meant, re-run with "
                "--allow-private-network")
    return addresses


def check_url(url: str, policy: NetworkPolicy) -> None:
    """Refuse *url* unless *policy* permits fetching it. Returns None or raises."""
    if not policy.allow_network:
        raise NetworkRefused("E_DATA_NETWORK_REFUSED",
                             f"network access is off, so '{url}' was not fetched", _HUMAN_HINT)
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError as exc:
        raise NetworkRefused("E_DATA_URL_HOST", f"'{url}' is not a usable URL ({exc})",
                             "write the source's location as http(s)://host/path") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise NetworkRefused(
            "E_DATA_URL_SCHEME", f"'{scheme or url}' is not an http(s) URL",
            "only http and https are fetched; a local file belongs in a csv/xlsx/sqlite source")
    if parts.username or parts.password:
        raise NetworkRefused("E_DATA_CREDENTIALS", f"the URL '{url}' embeds credentials",
                             "never store a credential in a model file; use a header-free "
                             "public URL, or a sql source with a ${NAGASIM_DATA_<NAME>} location")
    _check_host(hostname, port or (443 if scheme == "https" else 80), policy)


def check_sql_url(url: str, policy: NetworkPolicy) -> None:
    """The same host rules for a SQLAlchemy URL. A sqlite URL is refused here:
    it is a FILE, and files belong in the `sqlite` kind, which is path-confined."""
    if not policy.allow_network:
        raise NetworkRefused("E_DATA_NETWORK_REFUSED",
                             f"network access is off, so '{url}' was not connected to",
                             _HUMAN_HINT)
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError as exc:
        raise NetworkRefused("E_DATA_URL_HOST", f"'{url}' is not a usable database URL ({exc})",
                             "write it as driver://host/database") from None
    backend = parts.scheme.lower().partition("+")[0]
    if backend in ("", "sqlite", "file"):
        raise NetworkRefused("E_DATA_URL_SCHEME", f"'{url}' is a local file, not a network database",
                             "declare it as a \"sqlite\" source instead, whose path is confined "
                             "like any other file")
    # A driver reads far more of a URL than its host: `host=` and `hostaddr=` go somewhere
    # else than the host vetted below, `service=`, `passfile=` and `read_default_file=` take
    # credentials the caller never gave this model, `local_infile=` and `sslrootcert=` read
    # local files, `plugin=` loads code, `sslmode=disable` takes the encryption away. So a
    # parameter is refused unless it is one of the few that can do none of that. Counted,
    # never quoted: a value may be a credential.
    refused = [key for key, value in parse_qsl(parts.query, keep_blank_values=True)
               if key not in SQL_URL_PARAMETERS
               and not (key == "sslmode" and value in _SQL_SSL_MODES)]
    if refused:
        raise NetworkRefused(
            "E_DATA_URL_PARAMETER",
            f"the database URL carries {len(refused)} parameter(s) a model file may not set",
            f"allowed: {', '.join(sorted(SQL_URL_PARAMETERS))}, and sslmode="
            f"{'|'.join(sorted(_SQL_SSL_MODES))}; anything else belongs to the person who runs "
            f"the model (a database account, a service file), not to the model")
    _check_host(hostname, port, policy)


# ---------------------------------------------------------------- fetching
class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-vets every hop: a public URL that redirects to 169.254.169.254 is the
    oldest SSRF trick there is, and the stdlib would follow it silently."""

    def __init__(self, policy: NetworkPolicy):
        self.policy = policy
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > MAX_REDIRECTS:
            raise NetworkRefused("E_DATA_URL_REDIRECT",
                                 f"more than {MAX_REDIRECTS} redirects from '{req.full_url}'",
                                 "point the source at the final URL")
        if req.type == "https" and urlsplit(newurl).scheme.lower() != "https":
            raise NetworkRefused("E_DATA_URL_REDIRECT",
                                 f"the redirect to '{newurl}' downgrades an https connection",
                                 "point the source at the final https URL")
        check_url(newurl, self.policy)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(policy: NetworkPolicy):
    """A fresh opener per fetch: the redirect handler counts hops, so it is
    stateful and must not be shared. The test seam, too."""
    return urllib.request.build_opener(_CheckedRedirects(policy))


def fetch(url: str, policy: NetworkPolicy) -> bytes:
    """Vet *url*, fetch it with redirects re-vetted per hop, return the body.

    `Accept-Encoding: identity` plus a hard read cap is what stops a
    compression bomb: nothing is decompressed, and at most `max_bytes` is read.
    """
    check_url(url, policy)
    request = urllib.request.Request(
        url, headers={"Accept-Encoding": "identity", "User-Agent": "Naga"})
    with _opener(policy).open(request, timeout=policy.timeout) as response:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > policy.max_bytes:
            raise NetworkRefused("E_DATA_TOO_LARGE",
                                 f"'{url}' declares {declared} bytes, over the "
                                 f"{policy.max_bytes} byte cap",
                                 "point the source at a smaller extract")
        payload = response.read(policy.max_bytes + 1)
    if len(payload) > policy.max_bytes:
        raise NetworkRefused("E_DATA_TOO_LARGE",
                             f"'{url}' returned more than the {policy.max_bytes} byte cap",
                             "point the source at a smaller extract")
    return payload


# ---------------------------------------------------------------- paths
def is_unc_path(text: str) -> bool:
    """True for a UNC share (`\\\\server\\share\\...`, `//server/share/...`) or the
    Windows device-namespace spelling of one (`\\\\?\\UNC\\server\\share\\...`,
    which also starts with two backslashes). Merely stat-ing one makes Windows
    open an SMB connection that can leak NTLM credentials (DEVLOG #268), so it
    counts as network access wherever a path is read, confined or not.
    Separators are normalised first: Windows reads `/\\server\\share` as UNC
    too (DEVLOG #269)."""
    return text.replace("/", "\\").startswith("\\\\")


def confine_path(candidate, root) -> Path:
    """*candidate* resolved under *root*, or `PathRefused`.

    Symlinks are resolved BEFORE the containment test, so a link inside the
    root pointing out of it is refused too. UNC shares, Windows device paths
    and alternate data streams are refused whatever they point at -- a
    confined caller never reads outside its root, network access or not.
    """
    text = str(candidate)
    root = Path(root).resolve()
    if is_unc_path(text):
        raise PathRefused("E_DATA_PATH_OUTSIDE_ROOT",
                          f"'{text}' is a UNC or device path", f"use a path under {root}")
    path = Path(text)
    full = (path if path.is_absolute() else root / path).resolve()
    # Windows alternate data stream: "file.csv:hidden" reads a second, invisible
    # stream of the same file. Legal in a POSIX filename, so only checked on nt.
    if os.name == "nt" and any(":" in part for part in full.parts[1:]):
        raise PathRefused("E_DATA_PATH_OUTSIDE_ROOT",
                          f"'{text}' names an alternate data stream", f"use a path under {root}")
    if full != root and root not in full.parents:
        raise PathRefused("E_DATA_PATH_OUTSIDE_ROOT", f"'{text}' is outside {root}",
                          f"use a path under {root}")
    return full


# ---------------------------------------------------------------- connections (D215, D216)
# A live source (rest, mqtt, opcua, modbus) never names a host: it names a CONNECTION, and a
# connection exists only in a file the caller passed (`--connections FILE`) or the product's
# dialog confirmed. Defining one is the consent for that host and port, and for nothing else.
CONNECTION_KINDS = {"rest": ("https", "http"), "mqtt": ("mqtts", "mqtt"), "opcua": ("opc.tcp",),
                    "modbus": ("modbus",)}
# "write" (D743, D744): a Modbus connection `naga plc-write` writes setpoints over, to this
# machine only (`vet_connection`); every reader refuses it (`connection_for`).
CONNECTION_USES = ("read", "outbox", "write")
_PORTS = {"https": 443, "http": 80, "mqtts": 8883, "mqtt": 1883, "opc.tcp": 4840, "modbus": 502}
_TLS_SCHEMES = ("https", "mqtts")
# Every bound a source could otherwise choose for us: name -> (default, ceiling). The
# caller's file may move one under its ceiling (`"limits"`); a model or a tool may not (D220).
LIMITS = {
    "events": (10_000, 100_000),            # buffered events per source
    "keys": (10_000, 100_000),              # distinct keys per source
    "rate": (1_000, 10_000),                # events accepted per second per source
    "bytes": (20_000_000, 100_000_000),     # one REST response
    "rows": (100_000, 1_000_000),           # rows of one REST response
    "packet": (262_144, 4_194_304),         # one MQTT packet, one OPC UA message
}
_COMMON_KEYS = frozenset({"kind", "url", "private", "secret", "ca_file", "root", "limits", "use"})
_KIND_KEYS = {"rest": frozenset({"auth", "username", "header"}),
              "mqtt": frozenset({"username"}),
              "opcua": frozenset({"username", "security", "server_cert", "client_cert",
                                  "client_key"}),
              "modbus": frozenset({"unit", "registers"})}
_AUTHS = ("bearer", "basic", "header")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
_OPCUA_SECURITY = ("None", "Basic256Sha256")
_CONNECTIONS_HINT = ("only a human can define a connection: it lives in the file passed as "
                     "--connections, or chosen in the editor's connections dialog")


class ConnectionsError(GuardError, ValueError):
    """A connections file that cannot be used. Also a ValueError, so a command line
    answers it in one line instead of a traceback."""


class Secret:
    """A credential, wrapped so it cannot leave by accident: `repr` and `str` print
    `<secret>`, pickling and JSON refuse it, and `reveal()` is called on the one line that
    builds a header or a packet."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a secret is never pickled or copied")


@dataclass(frozen=True)
class Connection:
    """One place the caller allows a live source to connect to, and with which credential."""

    name: str
    kind: str                           # a key of CONNECTION_KINDS
    scheme: str
    host: str
    port: int
    path: str = ""                      # rest: the base path; opcua: the endpoint path
    private: bool = False               # the caller says this address is inside their network
    auth: str = ""                      # rest: one of _AUTHS; "" is anonymous
    header: str = ""                    # rest, auth "header": the header's name
    username: str = ""
    secret: Secret | None = None
    ca_file: str | None = None          # trust ONLY this CA instead of the system store
    root: str = ""                      # what a model may ask for: path prefix, topic filter, node prefix
    security: str = "None"              # opcua: one of _OPCUA_SECURITY
    server_cert: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    limits: tuple = ()                  # ((name, value), ...) of LIMITS the caller moved
    unit: int = 1                       # modbus: the unit (slave) id
    registers: tuple = ()               # modbus: the caller's register map, ((name, Register), ...)
    use: str = "read"                   # "outbox" or "write": never a source (`connection_for`)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}{self.path}"

    @property
    def encrypted(self) -> bool:
        return self.scheme in _TLS_SCHEMES or self.security != "None"

    def limit(self, name: str) -> int:
        return dict(self.limits).get(name, LIMITS[name][0])


# Modbus: the register map is the caller's. A model names registers, never addresses.
MODBUS_KINDS = {"holding": 3, "input": 4, "coil": 1, "discrete": 2}     # kind -> read function
MODBUS_TYPES = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "bool": 1}
_REGISTER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_REGISTERS = 1000


@dataclass(frozen=True)
class Register:
    """One entry of a connection's register map: where a value is, and how to decode it."""

    address: int
    kind: str                           # a key of MODBUS_KINDS
    type: str                           # a key of MODBUS_TYPES; coils and discrete inputs are "bool"
    scale: float = 1.0
    word_order: str = "big"             # a 32-bit value: "big" has the high word first


def _registers(name: str, spec) -> tuple:
    if not isinstance(spec, dict) or not 0 < len(spec) <= MAX_REGISTERS:
        raise _bad(name, f'"registers" must be an object of 1 to {MAX_REGISTERS} registers')
    out = []
    for reg, entry in spec.items():
        known = {"address", "kind", "type", "scale", "word_order"}
        if not _REGISTER_NAME.match(str(reg)) or not isinstance(entry, dict) \
                or set(entry) - known or not {"address", "kind", "type"} <= set(entry):
            raise _bad(name, "a register is a name of letters, digits and _.- with an object of "
                             "address, kind, type and optionally scale and word_order")
        address, kind, kind_type = entry["address"], entry["kind"], entry["type"]
        scale, order = entry.get("scale", 1), entry.get("word_order", "big")
        words = MODBUS_TYPES.get(kind_type, 0)
        if type(address) is not int or address < 0 or address + words > 65536 or not words:
            raise _bad(name, f"register '{reg}': address 0 to 65535 and a type of "
                             f"{', '.join(MODBUS_TYPES)}")
        if kind not in MODBUS_KINDS or (kind in ("coil", "discrete")) != (kind_type == "bool"):
            raise _bad(name, f"register '{reg}': kind is one of {', '.join(MODBUS_KINDS)}, and "
                             f"coil and discrete go with type bool, only they do")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale != scale \
                or abs(scale) == float("inf") or order not in ("big", "little"):
            raise _bad(name, f"register '{reg}': scale is a finite number, word_order big or little")
        out.append((str(reg), Register(address, kind, kind_type, float(scale), order)))
    return tuple(out)


def _bad(name: str, message: str, hint: str = _CONNECTIONS_HINT) -> ConnectionsError:
    return ConnectionsError("E_CONN_FORMAT", f"connection '{name}': {message}", hint)


def _beside(base: Path, name: str, key: str, value) -> str:
    """A path the file names, resolved beside the file, and there."""
    if not isinstance(value, str) or not value:
        raise _bad(name, f'"{key}" must be a path')
    path = Path(value)
    path = path if path.is_absolute() else base / path
    if not path.is_file():
        raise _bad(name, f'"{key}" names a file that does not exist')
    return str(path)


def _secret(name: str, spec, secrets) -> Secret:
    """The value a `{"env": NAME}` or `{"keyring": NAME}` reference stands for. A literal
    secret is refused: the file is the caller's, but it is a file, and it gets shared."""
    if not isinstance(spec, dict) or len(spec) != 1 or not isinstance(next(iter(spec.values())), str):
        raise _bad(name, 'a literal secret is refused; write "secret": {"env": "VARIABLE"}')
    (where, key), = spec.items()
    if where == "env":
        value = os.environ.get(key)
    elif secrets is not None:
        value = secrets(where, key)
    else:
        value = None
        if where != "keyring":
            raise _bad(name, f'a secret comes from "env" or "keyring", not "{where}"')
    if not value:
        raise _bad(name, f"its secret ({where} '{key}') is not set",
                   "set it where the connections file says it is, then run again")
    return Secret(value)


def _connection(name: str, spec, base: Path, secrets) -> Connection:
    if not isinstance(spec, dict):
        raise _bad(name, "must be an object")
    kind = spec.get("kind")
    if kind not in CONNECTION_KINDS:
        raise _bad(name, f"\"kind\" must be one of {', '.join(CONNECTION_KINDS)}")
    unknown = sorted(set(spec) - _COMMON_KEYS - _KIND_KEYS[kind])
    if unknown:
        # No key is ignored: `"verify": false` must be an error, not a no-op.
        # Counted, never named: a secret pasted where a key belongs must not be echoed.
        raise _bad(name, f"{len(unknown)} unknown key(s) for a {kind} connection (known: "
                         f"{', '.join(sorted(_COMMON_KEYS | _KIND_KEYS[kind]))})")
    try:
        parts = urlsplit(str(spec.get("url") or ""))
        host, port = parts.hostname, parts.port
    except ValueError:
        raise _bad(name, '"url" is not a usable URL') from None
    scheme = parts.scheme.lower()
    if scheme not in CONNECTION_KINDS[kind] or not host:
        raise _bad(name, f"\"url\" must be {' or '.join(s + '://host[:port]' for s in CONNECTION_KINDS[kind])}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise _bad(name, '"url" may hold no user name, password, query or fragment')
    if kind == "mqtt" and parts.path not in ("", "/"):
        raise _bad(name, 'an mqtt "url" has no path')
    if not isinstance(spec.get("private", False), bool):
        raise _bad(name, '"private" must be true or false')
    use = spec.get("use", "read")
    if use not in CONNECTION_USES:
        raise _bad(name, f"\"use\" must be one of {', '.join(CONNECTION_USES)}")
    if use == "write" and kind != "modbus":
        raise _bad(name, '"use": "write" is for a modbus connection only',
                   "naga plc-write writes Modbus holding registers, and nothing else")
    auth, header, username = spec.get("auth", ""), spec.get("header", ""), spec.get("username", "")
    root, security = spec.get("root", ""), spec.get("security", "None")
    if not all(isinstance(v, str) for v in (auth, header, username, root, security)):
        raise _bad(name, "auth, header, username, root and security must be text")
    secret = _secret(name, spec["secret"], secrets) if "secret" in spec else None
    if kind == "rest":
        if auth not in ("",) + _AUTHS:
            raise _bad(name, f"\"auth\" must be one of {', '.join(_AUTHS)}")
        if bool(auth) != (secret is not None):
            raise _bad(name, '"auth" and "secret" go together')
        if (auth == "basic") != bool(username):
            raise _bad(name, '"username" goes with "auth": "basic", and only with it')
        if (auth == "header") != bool(header):
            raise _bad(name, '"header" goes with "auth": "header", and only with it')
        if header and (not _HEADER_NAME.match(header) or header.lower() in _FORBIDDEN_HEADERS):
            raise _bad(name, '"header" is not a header name a credential may travel in')
    elif secret is not None and not username:
        raise _bad(name, '"secret" is a password, so "username" is needed')
    unit = spec.get("unit", 1)
    if kind == "modbus":
        # Modbus TCP has no login and no encryption: nothing here can hold a credential.
        if secret is not None or "ca_file" in spec or security != "None":
            raise _bad(name, "a modbus connection has no secret, ca_file or security")
        if type(unit) is not int or not 0 <= unit <= 255:
            raise _bad(name, '"unit" must be a whole number from 0 to 255')
        if "registers" not in spec:
            raise _bad(name, 'a modbus connection needs the "registers" map')
    if security not in _OPCUA_SECURITY:
        raise _bad(name, f"\"security\" must be one of {', '.join(_OPCUA_SECURITY)}")
    certs = {key: _beside(base, name, key, spec[key])
             for key in ("server_cert", "client_cert", "client_key") if key in spec}
    if (security != "None") != (len(certs) == 3):
        raise _bad(name, '"security": "Basic256Sha256" needs server_cert, client_cert and '
                         'client_key, and they need it')
    limits = spec.get("limits", {})
    if not isinstance(limits, dict):
        raise _bad(name, '"limits" must be an object')
    for key, value in limits.items():
        if key not in LIMITS:
            raise _bad(name, f"an unknown limit (known: {', '.join(LIMITS)})")
        if type(value) is not int or not 1 <= value <= LIMITS[key][1]:
            raise _bad(name, f"limit '{key}' must be a whole number from 1 to {LIMITS[key][1]}")
    return Connection(
        name=name, kind=kind, scheme=scheme, host=host.lower(), port=port or _PORTS[scheme],
        path=parts.path.rstrip("/"), private=spec.get("private", False), auth=auth, header=header,
        username=username, secret=secret,
        ca_file=_beside(base, name, "ca_file", spec["ca_file"]) if "ca_file" in spec else None,
        root=root, security=security, limits=tuple(sorted(limits.items())), use=use,
        unit=unit if kind == "modbus" else 1,
        registers=_registers(name, spec["registers"]) if kind == "modbus" else (), **certs)


def load_connections(path, secrets=None) -> dict[str, Connection]:
    """The connections a caller's file defines: {name: Connection}.

    The file is the caller's, never a model's: `{"format": 1, "connections": {name: {...}}}`.
    Paths in it resolve beside it. *secrets*, when given, is `(where, key) -> value | None`
    for a secret that is not in the environment (the product's OS store); the engine imports
    no credential store itself. Every failure is a `ConnectionsError` (`E_CONN_FORMAT`) that
    names the connection and never quotes a value.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConnectionsError("E_CONN_FORMAT", f"the connections file '{path}' cannot be read "
                               f"({type(exc).__name__})", _CONNECTIONS_HINT) from None
    if not isinstance(data, dict) or data.get("format") != 1 or set(data) != {"format", "connections"} \
            or not isinstance(data["connections"], dict):
        raise ConnectionsError("E_CONN_FORMAT", f"'{path}' is not a connections file",
                               'write it as {"format": 1, "connections": {"<name>": {...}}}')
    return {str(name): _connection(str(name), spec, path.parent, secrets)
            for name, spec in data["connections"].items()}


def connection_for(source: dict, policy: NetworkPolicy) -> Connection:
    """The caller's connection a live source names, or the refusal. The ONE lookup every
    reader goes through: an unknown name, another kind's connection and a connection kept
    for an outbox are all `E_DATA_CONNECTION`, and no socket is opened."""
    name, kind = source.get("connection") or "", source.get("kind")
    conn = (policy.connections or {}).get(name)
    if conn is None:
        raise NetworkRefused("E_DATA_CONNECTION",
                             f"source '{source.get('name')}': no connection named '{name}' is "
                             f"defined, so nothing was opened", _CONNECTIONS_HINT)
    if conn.kind != kind or conn.use != "read":
        what = f"a {conn.kind} connection" if conn.use == "read" \
            else "kept for writes" if conn.use == "write" else "kept for an outbox"
        raise NetworkRefused("E_DATA_CONNECTION",
                             f"source '{source.get('name')}': connection '{name}' is {what}, "
                             f"not a {kind} source's", _CONNECTIONS_HINT)
    return conn


def describe_connection(conn: Connection) -> str:
    """"name: scheme://host:port -> addresses, TLS|plain, credential, root": what a human needs
    to see BEFORE consenting, and what `naga watch --check` prints."""
    try:
        found = ", ".join(sorted({str(ip) for ip in _addresses(conn.host, conn.port)}))
    except NetworkRefused:
        found = "does not resolve"
    credential = conn.auth or ("password" if conn.secret is not None else "anonymous")
    # The root is part of what is approved: it is all that confines what a model reads.
    root = f"root {conn.root}" if conn.root else "no root (anything it serves)"
    return (f"{conn.name}: {conn.url} -> {found}, {'TLS' if conn.encrypted else 'plain'}, "
            f"{credential}, {root}")


# ---------------------------------------------------------------- the pinned socket (D217)
CONNECT_TIMEOUT = 10.0


def vet_connection(conn: Connection) -> list:
    """Resolve the connection's host ONCE and judge every address; the addresses a socket
    may then be opened to. Link-local, multicast, reserved and unspecified are always
    refused; loopback, private and CGNAT space unless the caller's file says `private`. A
    credential over a channel that is not encrypted is refused unless every address is this
    machine: nothing turns that off. A connection kept for writes reaches this machine only
    (D744: a plant's safety system owns real writes), whatever else its file says."""
    addresses = _addresses(conn.host, conn.port)
    if conn.use == "write" and not all(ip.is_loopback for ip in addresses):
        raise NetworkRefused(
            "E_PLC_HOST",
            f"connection '{conn.name}' is kept for writes and '{conn.host}' is not this "
            f"machine: setpoints are written to a soft PLC on this machine only",
            "a plant consumer reads the outbox itself; nothing here writes to a plant")
    for ip in addresses:
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or \
                (ip.is_reserved and not ip.is_loopback):
            raise NetworkRefused(
                "E_DATA_URL_PRIVATE",
                f"connection '{conn.name}': '{conn.host}' resolves to {ip}, which no "
                f"connection may reach",
                "a connection may not reach the link-local metadata address or a multicast "
                "group")
        if not conn.private and conn.use != "write" \
                and (ip.is_loopback or ip.is_private or ip in _CGNAT):
            raise NetworkRefused(
                "E_DATA_URL_PRIVATE",
                f"connection '{conn.name}': '{conn.host}' resolves to the private address {ip}",
                "if that host really is inside your network, the human who owns the "
                "connections file adds \"private\": true to this connection")
    if conn.secret is not None and not conn.encrypted \
            and not all(ip.is_loopback for ip in addresses):
        raise NetworkRefused(
            "E_DATA_PLAINTEXT_CREDENTIAL",
            f"connection '{conn.name}' would send its credential over {conn.scheme}, which "
            f"anyone on that network can read",
            "use the encrypted scheme (https, mqtts, an OPC UA security policy), or read "
            "anonymously; there is no setting that allows this")
    return addresses


def open_connection(conn: Connection, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """A connected socket to *conn*: the address that was judged is the address that is
    dialled (no second resolution, so no DNS rebinding), TLS is verified against the host
    name with no way to turn it off, and no proxy is involved. A reconnect calls this
    again, which resolves and vets again."""
    addresses = vet_connection(conn)
    sock, failure = None, None
    for ip in addresses:
        sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                             socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((str(ip), conn.port))      # a literal address: nothing resolves again
            break
        except OSError as exc:
            sock.close()
            sock, failure = None, exc
    if sock is None:
        raise NetworkRefused("E_DATA_READ",
                             f"connection '{conn.name}' could not be reached "
                             f"({type(failure).__name__})",
                             "check the address in the connections file and that the service "
                             "is up")
    if conn.scheme not in _TLS_SCHEMES:
        return sock
    # With a `ca_file` ONLY that CA is trusted; without one, the system store.
    context = ssl.create_default_context(cafile=conn.ca_file)
    try:
        return context.wrap_socket(sock, server_hostname=conn.host)
    except (ssl.SSLError, OSError) as exc:
        sock.close()
        reason = getattr(exc, "verify_message", None) or getattr(exc, "reason", None) \
            or type(exc).__name__
        raise NetworkRefused("E_DATA_TLS",
                             f"connection '{conn.name}': the TLS handshake with '{conn.host}' "
                             f"failed ({reason})",
                             "the certificate must be valid for that host name; for a private "
                             "CA, name it as \"ca_file\" in the connections file") from None


# ---------------------------------------------------------------- consent helpers
def host_of(url: str) -> str:
    """The hostname a location names, for a consent prompt and an allow-list."""
    try:
        return (urlsplit(url).hostname or url).lower()
    except ValueError:
        return url.lower()


def describe_url(url: str) -> str:
    """"host -> 1.2.3.4, 5.6.7.8": what a human needs to see BEFORE consenting.
    Resolution failure is not fatal here -- the fetch itself still vets."""
    host = host_of(url)
    try:
        found = sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except OSError:
        found = []
    return f"{host} -> {', '.join(found)}" if found else f"{host} (does not resolve)"


def add_arguments(parser) -> None:
    """The human-only consent flags, shared by every CLI that can fetch."""
    parser.add_argument("--allow-network", action="store_true",
                        help="permit sql / json_url data sources (refused by default: opening a "
                             "model must not make outbound connections on its own)")
    parser.add_argument("--allow-private-network", action="store_true",
                        help="also permit private/intranet addresses (refused even with "
                             "--allow-network)")
    parser.add_argument("--allow-host", action="append", default=None, metavar="HOST",
                        help="restrict network sources to this hostname; may repeat")
    parser.add_argument("--connections", default=None, metavar="FILE",
                        help="the connections a live source (rest, mqtt, opcua) may name: a "
                             "JSON file of yours, never the model's; without it every live "
                             "source is refused")


def policy_from_args(args) -> NetworkPolicy:
    """The NetworkPolicy those flags describe."""
    hosts = getattr(args, "allow_host", None)
    connections = getattr(args, "connections", None)
    return NetworkPolicy(allow_network=bool(getattr(args, "allow_network", False)),
                         allow_private=bool(getattr(args, "allow_private_network", False)),
                         allowed_hosts=frozenset(hosts) if hosts else None,
                         connections=load_connections(connections) if connections else None)
