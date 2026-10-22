# Security

## Trust model

A model file (`.json`) is **untrusted data**, exactly like a document someone emailed you. Opening
one, validating it, running it, or letting an agent edit it must never make an outbound connection
or read a file the caller did not intend, on the strength of what the file itself says.

**One deliberate exception**: `core/custom_logic.py` (Logic classes, written by hand) and a loaded plugin are **trusted code** -- not because a model file
referenced them, but because a human explicitly installed the plugin or authored the Logic class.
That trust boundary has not changed; everything below exists because a model file can *name* an
outbound URL or file path (data bindings) and an outside caller (an MCP client, the assistant)
can drive a model.

## The rule

**Permission belongs to the caller, never to the file.** A data source's `allow_network` field, if
one is present in a model file, is stripped on load and ignored (`W_DATA_ALLOW_NETWORK_IGNORED`).
No agent tool accepts a permission as an argument; a `Workspace`'s network
policy is fixed once, by the human who started the server or the CLI command, and is invisible to
every tool call and every model file.

## What is refused by default, and why

| Refused unless a human opts in | Reason |
|---|---|
| Any network access for a `sql`/`json_url` data source | a model opened for review must not be able to phone home or scan the network on its own (SSRF) |
| A URL resolving to a loopback, link-local, multicast, reserved or unspecified address | closes the classic cloud-metadata-endpoint (`169.254.169.254`) and localhost-service attacks |
| A URL resolving to a private/unique-local/CGNAT address | closes intranet-scanning even when `--allow-network` is on, unless `--allow-private-network` is also given |
| A URL with embedded credentials | a credential belongs in an environment variable, never committed inside a model file. A model file may reference only variables whose names start with `NAGASIM_DATA_` (`${NAGASIM_DATA_DB_URL}`); a reference to any other name is refused (`E_DATA_ENV_NAME`) before the environment is read, so a file cannot read your API keys. A failure message, hint or log line about the source quotes the reference as written, not the variable's value, except a value shorter than four characters and a part of a value that a library extracts (the host of a URL held in a variable), which can still be shown. A `NAGASIM_DATA_` variable can be read as data by any model file you run (through a query, an option, a filter, or a location on a host you allowed): put there only what a model file may see |
| A live source (`rest`, `mqtt`, `opcua`, `modbus`) whose connection you did not define | a model names a connection and never an address: where it leads, and with which credential, is in a connections file only you pass (`--connections`). Without it no socket opens (`E_DATA_CONNECTION`) |
| A password or token over an unencrypted channel to another machine | `mqtt://`, `http://` and OPC UA without security carry it in clear text for anyone on that network; there is no setting that allows it (`E_DATA_PLAINTEXT_CREDENTIAL`). Anonymous plain reads and encrypted channels work |
| A TLS certificate that is not valid for the host | always verified, host name included; a private CA is named as `ca_file`; no key or flag turns the check off (`E_DATA_TLS`) |
| A redirect that downgrades https to http, or chains past 3 hops | the oldest SSRF trick there is -- a public URL redirecting to a private one |
| A file path outside an agent's or server's confined root | symlinks are resolved before the check; Windows UNC shares, device paths and alternate data streams are refused outright |
| A UNC source path (`\\server\share\...`) with no confined root | merely checking whether it exists makes Windows open an SMB connection, which can leak NTLM credentials -- so it needs `--allow-network` like a `sql`/`json_url` source, exactly as if it were one; a confined root refuses it outright regardless, per the row above |

Consent, once granted, is scoped as tightly as the surface it came from: a CLI flag lasts one
process and an MCP server's start arguments last its lifetime.

## How to grant it

- CLI: `--allow-network`, `--allow-private-network`, `--allow-host HOST` (repeatable) on `naga bind`,
  `naga forecast` and `naga mcp`.
- MCP: the same three flags, passed once in the server's `args` at start-up.
- Live sources: `--connections FILE` on `naga bind`, `naga forecast`, `naga watch`, `naga plant`,
  `naga twin` and `naga mcp`. The
  file is yours: it says where each named connection leads and where its secret is kept
  (an environment variable), and holds no secret
  itself. No environment variable names the file, and no agent tool takes one. See
  [Live data](live-data.md).

## Live readers

The three live readers are read-only by construction and share one way out of the machine:

- **The address that was judged is the address that is dialled.** A connection's host is
  resolved once, every address is vetted, and the socket goes to the vetted address, so a
  resolver that answers differently later changes nothing. No redirect is followed (a REST
  redirect is an error; an OPC UA server's endpoint addresses are ignored) and no proxy is used.
- **Nothing can be written.** The REST reader has one verb, GET. The MQTT client can build a
  CONNECT, a SUBSCRIBE, an acknowledgement, a ping and a DISCONNECT: there is no publish, no
  will and no persistent session, so it leaves nothing on a broker, and a Sparkplug rebirth
  request is never sent. The OPC UA reader subscribes to data changes and calls nothing else. The Modbus reader builds
  function codes 1 to 4 (reads) and no other; its register map, host and unit are yours.
- **Everything read is data.** Values are scalars; a text from a plant is never used as a path,
  an address, an expression or a name, and is written into a model only as an order's field.
- **Every bound has a ceiling** ([Live data](live-data.md#bounds)): what is over one is dropped
  and counted. An MQTT packet over the limit is skipped by its declared size without being
  held, and one within the limit that arrives byte by byte is given up after ten seconds; one
  being skipped as oversize gets ten seconds more per limit's worth of its declared size, at
  most 90 seconds (one and a half keep-alives), and holds no memory meanwhile. The OPC UA
  message limit is sent to the server and holds for a server that honours the protocol; a
  faulty or hostile one can still send more, which can stop the twin (it reads only; nothing
  is executed or written). Such a server is reported once (`W_LIVE_LIMIT`) and still read.
- **A root confines what a model reads.** A REST path is judged whole in every decoding (no
  `..`, no second `?`, no other host), an MQTT filter must lie inside the root filter, and an
  OPC UA node id is one identifier compared with the root by namespace, kind and value. The
  root is shown in the line you approve.
- **A secret shows nowhere.** It is revealed on the one line that builds a header or a packet;
  what a server answers is never quoted in a message.

## The action outbox

A twin started with `--candidates` recommends; started also with `--outbox file:<path>` it
appends each recommendation to that file. This is the one place the engine writes outside a
result directory you named, so:

- **Only you grant it.** The flag on `naga twin`. No model
  file, mapping, candidates file, tool argument or environment variable can name an outbox.
  Without the flag nothing is written outside `--out`.
- **It is a queue, never a control.** A record is data: an action from a closed vocabulary on a
  name your model declares, numbers, hashes, an expiry. No path, address, credential or free
  text. Nothing is sent to a plant or an MES.
- **The path is used as given and trusted for nothing.** It must be a plain file (not a folder
  or a link, and with no second name through a hard link) in a folder that exists; a UNC path
  and a `:` after the drive are refused; the file stops at 100 MB. A link or a junction in a
  parent folder is your own path and is followed.
- **Replies are hostile input.** `<path>.replies` is read in bounded pieces; a reply can only
  decide a recommendation the twin itself delivered, or end one that was taken, and can name
  no action, value or file.
  Its optional `by` and `note` are kept in the twin's ledger and shown to no AI assistant.
- **An AI assistant can read recommendations and nothing more.** No tool delivers, approves,
  rejects, names an outbox or addresses a record.
- **Residual.** A consumer that executes proposals without a human is the caller's system
  acting, on the caller's authority. Whoever can write your replies file can make a proposed
  action count as taken in the twin's later forecasts.

## A shared player file

A player file (`naga run --player`) leaves your machine, so it is built to carry
only what it shows and to ask for nothing: no parameter, expression, data source or path of the
model is in it, text from the model is shown as text and never read as markup, and a
Content-Security-Policy makes the browser refuse every request and every script but the page's
own.

## Residual risks

- **DNS rebinding window**: the address is vetted, then resolved again to connect; a hostile
  resolver could in principle answer differently in between. Bounded by per-hop redirect
  re-vetting, a response size cap, and the fact that only parsed data -- never the raw response --
  ever reaches the model.
- **A live connection's root**: a model file can read anything under a connection's `root` with
  your credential and show it in results, so keep each root narrow.
- **A plain channel can be forged**, and a forged value is a wrong forecast. A text from a plant
  can also try to instruct an LLM that reads a tool's answer; it gains no permission by it.
- **OPC UA** is read through the optional `asyncua` package (`nagasim[opcua]`, versions 2.1 to
  2.x), which is LGPL-3.0-or-later, installed by you and not shipped with the engine. Its own
  parsing is its risk surface. It is handed a vetted literal address, and its message limit is set through the library's
  public attributes. A server that answers "no limit" is reported, not refused: use an
  encrypted connection to a server you trust, or lower the server's own limits.
- **OPC UA, encrypted** (`Basic256Sha256`, sign and encrypt; tested against a server). The
  library pins the server's certificate: it must be exactly the file you named, and the channel
  is encrypted to its key, so a server without that key gets nothing. The library checks no
  date and no application URI unless asked, so the reader does: your server and client
  certificates must be inside their validity before anything is dialled, and the server's
  certificate must carry the application URI the server claims. All of it comes before the
  password is sent, and the password travels only inside the encrypted channel. There is no
  chain of authorities and no revocation list: the trust is the one certificate you named. The
  host name in a certificate is not checked (the pin is stronger). Your private key is read
  from the path you gave and is not copied.
- **A `sql` source runs with the database account you gave it.** The location may carry only a
  few URL parameters (`connect_timeout`, `application_name`, `charset`, and `sslmode` when it
  is `require`, `verify-ca` or `verify-full`); every other one is refused, because drivers let
  a URL parameter name another host, a credentials file, a local file or a plugin. A query
  must be one SELECT (or WITH ... SELECT) statement. What is NOT closed, because it happens
  inside the driver: a driver may still take a password or a default host from your own
  environment and files (`PGPASSWORD`, `~/.pgpass`, `PGSSLMODE`, a MySQL option file in your
  home, an ODBC DSN), and a SELECT can call whatever functions the account may call. So run a
  model you did not write only with a database account that can read what that model should
  read and nothing else, and do not keep database passwords in the environment of the process.
  None of this was run against a real driver: the checks are on the URL and the query text.
- **XLSX parsing** is openpyxl's own risk surface, not something this project adds to or mitigates.

## Fine print

- Logic classes and plugins execute arbitrary Python with the same trust as your own scripts --
  only use them with code you trust, and never exchange a model file that relies on a Logic block
  or a plugin with someone who should not run code on their machine.
- Every other expression path (Assign, Decide/Hold conditions, Record, distribution parameters) is
  compiled by a restricted-Python allow-list, never `eval`/`exec`, and this is enforced by a hygiene
  test over the whole codebase.
