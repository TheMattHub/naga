# Live data

A model can read what a plant is saying right now: a REST service, an MQTT broker (plain topics
or Sparkplug B), an OPC UA server or a Modbus TCP device. To see what a plant sends in the views of a run, see
[A plant as a timeline](plant-timeline.md). Four more kinds of [data source](digital-twin.md) do it,
`rest`, `mqtt`, `opcua` and `modbus`, and they obey one rule the file kinds do not need:

> **A model names a connection. You define where it leads.**

A live source holds no address and no credential, only the *name* of a connection. The
connection itself lives in a file of yours that you pass when you run the model. A model you
received from someone else therefore cannot choose where your machine connects, or where your
token is sent. Everything here is **read-only**: no reader can publish, post, write a node or
call a method, because the code for it does not exist.

## The connections file

```json
{"format": 1, "connections": {
  "mes":   {"kind": "rest",  "url": "https://mes.plant.example/api/v2",
            "auth": "bearer", "secret": {"env": "PLANT_MES_TOKEN"}, "root": "/orders"},
  "line":  {"kind": "mqtt",  "url": "mqtts://10.20.0.8:8883", "private": true,
            "username": "twin", "secret": {"env": "LINE_PASSWORD"},
            "ca_file": "plant-ca.pem", "root": "spBv1.0/Plant1/#"},
  "press": {"kind": "opcua", "url": "opc.tcp://10.20.0.15:4840", "private": true}}}
```

| Key | Meaning |
|---|---|
| `kind`, `url` | `rest`: `https://` or `http://`; `mqtt`: `mqtts://` or `mqtt://`; `opcua`: `opc.tcp://`; `modbus`: `modbus://` (port 502). No user name, password, query or fragment in the URL |
| `private` | `true` when the address is inside your network (or on this machine). Without it only public addresses are reached; link-local and multicast addresses never are |
| `secret` | `{"env": "VARIABLE"}`. A secret written in the file itself is refused |
| `auth` (rest) | `bearer`, `basic` (with `username`) or `header` (with `header`, the header's name) |
| `username` (mqtt, opcua) | the user the `secret` is the password of |
| `ca_file` | trust only this certificate authority instead of the system's. A path beside the file |
| `root` | what a model may ask for over this connection: a path prefix, a topic filter, a namespace or node-id prefix. Keep it narrow; it is shown when you approve the connection |
| `security`, `server_cert`, `client_cert`, `client_key` (opcua) | `"Basic256Sha256"` with the three files: sign and encrypt. `server_cert` is the one certificate you trust (the server must present exactly it); both certificates must be inside their validity, and the server's must carry the server's application URI. A password is then sent inside the encrypted channel |
| `unit`, `registers` (modbus) | the unit id (0 to 255, default 1) and the register map, see "Modbus" below |
| `limits` | move a bound, under its ceiling (below) |
| `use` | `"read"` (the default). `"outbox"` is reserved: such a connection is never given to a reader |

An unknown key is an error, so nothing is silently ignored: there is no key that turns
certificate checks off, and a password is never sent over an unencrypted channel to another
machine (`mqtt://`, `http://`, OPC UA without security). Read anonymously, or use the encrypted
scheme.

Pass the file with `--connections FILE` on `naga bind`, `naga forecast`, `naga watch`, `naga twin`
and `naga mcp`.

## The four kinds

```jsonc
"data_sources": [
  {"name": "orders", "kind": "rest", "connection": "mes", "location": "orders?open=1",
   "options": {"records": "items", "key": "id", "time": "updated", "poll": 5}},
  {"name": "line", "kind": "mqtt", "connection": "line",
   "location": "spBv1.0/Plant1/#", "options": {"payload": "sparkplug_b"}},
  {"name": "press", "kind": "opcua", "connection": "press",
   "location": "ns=2;s=Press.Speed\nns=2;s=Press.State"},
  {"name": "oven", "kind": "modbus", "connection": "oven", "location": "temp\nrunning",
   "options": {"poll": 5}}
]
```

| Kind | `location` | Options | A binding reads |
|---|---|---|---|
| `rest` | a path (and query) under the connection's URL | `records` (the field holding the array), `key`, `time` (column names), `poll` (seconds, at least 1) | the rows of one GET: a JSON array of objects |
| `mqtt` | topic filters, one per line | `payload` (`json`, `text`, `sparkplug_b`), `time` (a JSON field), `qos` (0 or 1), `settle` (seconds) | rows `key`, `value`, `source_time`: the latest value per topic or metric |
| `opcua` | node ids, one per line | `interval_ms` (at least 100), `settle` | rows `key`, `value`, `source_time`: the latest value per node |
| `modbus` | register names of the connection's map, one per line | `poll` (seconds, at least 1) | rows `register`, `value`: one per name, read each poll |

`opcua` needs the optional package: `pip install "nagasim[opcua]"`. It installs `asyncua`
(2.1 to 2.x), which is licensed LGPL-3.0-or-later and is not shipped with the engine.

## Modbus

Modbus TCP is read by polling, with the standard library only (no package to install). The
register map is yours, in the connections file; a model names registers from it and never an
address, so a model you received cannot read a register you did not list.

```json
"oven": {"kind": "modbus", "url": "modbus://10.20.0.30", "private": true, "unit": 1,
         "registers": {
  "temp":    {"address": 0,  "kind": "holding",  "type": "uint16",  "scale": 0.1},
  "flow":    {"address": 10, "kind": "holding",  "type": "float32", "word_order": "big"},
  "running": {"address": 3,  "kind": "coil",     "type": "bool"}}}
```

| Register key | Meaning |
|---|---|
| `address` | 0 to 65535, as the device documents it (the first register is 0) |
| `kind` | `holding`, `input`, `coil` or `discrete` (function codes 3, 4, 1 and 2) |
| `type` | `uint16`, `int16`, `uint32`, `int32`, `float32` or `bool` (coils and discrete inputs, and only they) |
| `scale` | the raw number is multiplied by it (optional, default 1) |
| `word_order` | for a 32-bit type: `big` (high word first, default) or `little` (the words swapped) |

The reader sends reads only: no write function code can be built. Modbus TCP has no login and
no encryption, so a modbus connection takes no `secret`, `ca_file` or `security`; keep the
device on a network you control. A non-finite float is read as no value.

A root confines what a model may read. For `rest` it is a path prefix; for `mqtt` a topic
filter the source's filters must lie inside; for `opcua` a whole namespace (`ns=2;`), a prefix
of string ids (`ns=2;s=Press.`) or one exact node (`ns=2;i=1`). A node id names one identifier:
`ns=2;i=5;ns=0;i=2258` is refused.

A value from a plant is data. A binding writes a number or a true/false value into the model, and
texts only into an order book (the `orders` transform); a text is never written into another
field. `${NAME}` is not expanded in a live source.

With Sparkplug B, a key is `<group>/<node>[/<device>]/<metric>`; a birth sets
`<group>/<node>[/<device>]` to true and a death to false; a node's death sets its devices to false
too. A metric that arrives by alias before
its birth was seen is counted and dropped until the node's next birth: asking for a rebirth would
be a command to the plant, and it is never sent. So start the twin before the edge node, or
restart the edge node once the twin listens. The aliases remembered for a connection are
bounded by its `keys` limit, and one node or device keeps at most a sixteenth of it (625 by
default), so a single device cannot leave the others with none.

## Watching

<!-- no-run: needs a plant and your connections file -->
```console
naga watch model.json --connections my-connections.json --for 30 --record events.jsonl
naga watch model.json --connections my-connections.json --check
```

`watch` prints each event as it arrives and a line per source at the end (state, events, lost,
gaps, channel). `--record` writes one JSON object per line, with a `{"lost": n}` or
`{"gap": true}` line wherever the log has a hole. `--check` prints each connection, connects once
and stops.

## What changed since

From Python, a `Feed` answers one question for every kind of live source:

<!-- no-run: needs a plant and your connections file -->
```python
from nagasim import live, netguard

policy = netguard.NetworkPolicy(connections=netguard.load_connections("my-connections.json"))
with live.Feed(layout["data_sources"], network=policy) as feed:
    first = feed.changes("line")                 # a snapshot: the latest value per key
    later = feed.changes("line", first.cursor)   # what arrived since, in order
    rows, stamp = feed.rows("line")              # the latest values, as a binding sees them
```

An event is `(source, key, value, source_time, received, seq)` and asserts a state ("this key
has this value"), never an increment, so reading one twice is harmless. `changes` also returns
`lost` (events dropped before you read them, an exact count), `gap` (the connection broke: an
unknown number is missing), `state` (`live` or `down`), `age` and the problems of the answer.
`api.run_model(..., bind_data=True, feed=feed)` binds from an open feed.

## Tested against

Beside the in-process stand-ins of the normal tests, opt-in tests (`NAGA_REALSERVER=1`,
`tests/test_realserver.py`) run the readers against servers written by others, on this machine:

| Server | Proven |
|---|---|
| Mosquitto 2.1.2 | the MQTT reader: retained messages, QoS 0, 1 and 2 (delivered at 1), reconnecting after the broker restarts (the outage reported as a gap), TLS with a private CA; the MQTT outbox: a record published and heard by `mosquitto_sub`, replies sent by `mosquitto_pub` (retained too) read back |
| A Sparkplug B edge node built on `protobuf` 6.33 from the official schema, with Eclipse Tahu's encoding of values | births, data by alias and deaths over Mosquitto; signed integers of every width; bytes counted and not read |
| pymodbus 3.16.1's Modbus TCP server | the Modbus reader: holding and input registers, a float32, a 32-bit integer, a coil and a discrete input |
| asyncua 2.1.0's OPC UA server | data changes of doubles, integers, true/false values and texts with their stamps; a Bad status as no value; a server that goes away as a gap |
| `naga twin run --monitor` | against the edge node over Mosquitto for an hour (`tests/realserver/twin/run.sh`): 721 cycles with no gap, 30 forecasts scored, a recommendation delivered to the MQTT outbox and its reply read back |

Not proven: a real plant, a real PLC's Modbus implementation, an MES, a plant counter, another broker (HiveMQ, EMQX) or OPC UA
server (open62541, a PLC's own), and a commercial edge node (Ignition).

## Bounds

| Bound | Default | Ceiling |
|---|---|---|
| `events` buffered per source | 10 000 | 100 000 |
| `keys` per source | 10 000 | 100 000 |
| `rate`, events per second (above it the reader waits) | 1 000 | 10 000 |
| `bytes` of one REST answer | 20 MB | 100 MB |
| `rows` of one REST answer (decoded one at a time: the read stops at the limit) | 100 000 | 1 000 000 |
| one row of a REST answer | 256 KiB | fixed |
| `packet`: one MQTT packet, one OPC UA message | 256 KiB | 4 MiB |

A key or a text value longer than 1 KiB is dropped. Whatever is over a bound is dropped and
counted, never kept; the counts are in `naga watch` and in `Feed.status`. A message that is
dropped still counts against its source's rate, so a flood of junk under a connection's root
can slow that source's good values; it shows in the `dropped` counts. The OPC UA message limit is
sent to the server, and the cap holds for a server that honours the protocol; one that answers
"no limit" gets a `W_LIVE_LIMIT` warning and is still read. A faulty or hostile server can send
more than the limit, which can stop the twin (it reads only; nothing is executed or written).
See
[Security](security.md) for what is refused and why.
