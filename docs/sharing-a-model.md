# Sharing a model: the browser viewer

A model can be shared as **one HTML file in which the engine itself runs**. Whoever opens
it changes the inputs you exposed, runs the model again in their browser and reads the results
beside your baseline. No installation, no account, no server, and no network: the page asks for
nothing.

**What it is for:** client staff, the model as the consultant left it, to explore and show
around. It is for demonstrating and sharing a model inside a client. It is not an operational
screen: for that there is the twin's monitor ([Digital twin](digital-twin.md)).

## Viewer or recording?

| | The viewer (this page) | The recording (`naga run --player`) |
|---|---|---|
| What the reader can do | change inputs, run again, compare | watch one run |
| What the reader can read | **the whole model** and the engine's source | names, layout and the sampled run; no parameter |
| Size | about 40 MB (30 MB zipped) | under 10 MB, usually far less |
| Send it by | a link or a shared drive | email |
| Use it for | a model the client may see | a confidential model |

**Anyone who receives a viewer file can read all of the model in it**: every parameter,
distribution, expression, schedule and cost. If the model is confidential, share a recording.
A viewer file also holds the engine's source.

## Make one

| From | How |
|---|---|
| The command line | `naga viewer model.json -o model.html` |
| Python | `nagasim.viewer.build_viewer(layout, "model.html")` |

The first time, the browser runtime (Pyodide 314.0.7, 29 MB) must be on your machine. It is
downloaded only when you ask, with `naga viewer --fetch`. It comes from github.com and cdn.jsdelivr.net and every file is checked
against a pinned hash. On a machine with no access to those, copy the folder the command names
from another machine and pass `--runtime DIR` (or set `NAGASIM_VIEWER_RUNTIME`). Making a viewer
never downloads anything by itself, and a viewer file never does.

<!-- no-run: needs the browser runtime, 29 MB, fetched from github.com and cdn.jsdelivr.net -->
```console
naga viewer --fetch
naga viewer single_server -o single_server.html
```

The export runs your model once, with the replications of its dashboard definition: that run is
the baseline the reader sees first. It also records the first replication, which the reader can
watch.

## What the reader sees

- **The inputs** of the model's dashboard definition (its `dashboard` section): the
  ones you wrote, or the ones generated from the model (variables, resource capacities, constant
  auxiliaries, submodel arguments). Each stays within its range: the engine refuses a value
  outside it. A model with no inputs says so and can still be run as it is.
- **Replications**, from 1 to the definition's `limits.max_replications`. The seed is the
  model's own and is shown. The run length is the model's unless you exposed it as an input.
- **Run** and **Cancel**. Cancel stops at once; the engine then starts again (about 10 s). Cancel also stops an engine that is still starting; Run starts it again.
- **Results**: for each result of the definition, the baseline's mean and 95% interval, the
  reader's, and the difference with its interval (paired: both runs use the same random
  numbers). A star marks a difference whose interval excludes 0. When an input changes, the
  table is greyed until the next run: numbers from two sets of inputs are never mixed.
- **"Same engine"**: on every run the page compares its own baseline with yours and says whether
  every value is equal.
- **Watch the author's recorded run**: the player (`naga run --player`) of your baseline's first
  replication. It replays what you recorded and does not follow the reader's inputs.
- The time one run takes, when it is more than a few seconds. The export warns you when the
  longest run a reader may ask for is estimated at over five minutes: lower
  `limits.max_replications`.

Starting takes about 10 s (the page says which step it is at); your baseline is on screen at once.

## A model fed from files or a database

A viewer reads no data source, so a model with data sources is refused by name. **Freeze the
data into the copy** instead: the sources are read once, when you export, and the values read
are written into the copy as plain model data.

`naga viewer model.json -o model.html --freeze`, with `--allow-network` and
the other flags of [`naga bind`](digital-twin.md) when a source is not a file.

The copy names no source, path, address, credential or connection, and the page says "Data as
read on" the date. The export is refused, naming the reason, when a source cannot be read, when
a source is a live one (REST, MQTT, OPC UA: a plant's feed has no meaning in a copy), when the
copy would pass 5 MB, and when a source's location is also written elsewhere in the model (in a
description, for instance): remove it there.

## What cannot be shared this way

Refused by name: agent types that run Python, LOGIC blocks, anything a plugin registered, and
data sources that were not frozen. A viewer runs no code but the engine's.

## The numbers

The page runs the same engine files as your installation, on Python in the browser. For the same
inputs and seed, every result of every replication was equal to the desktop's in every value:
measured on the bundled models, and checked again by the page itself, which compares its
baseline with yours on every run and says so.
The **interval bounds can differ in their last digits** (about the eleventh) when the browser's
scipy is not the version you have: they hold a Student-t quantile that scipy computes slightly
differently between versions. Two desktops with different scipy versions differ the same way.

## Browsers, and limits

- Chrome, Edge and Firefox on a computer. Chrome and Edge are tested automatically on every
  change; Firefox is checked by hand. **Safari, phones and tablets are not supported**: the page
  says so in place of starting.
- About 40 MB, whatever the model: almost all of it is Python and its numerical libraries.
- No step by step, no animation of the reader's own run, no scenarios, no saving.
- A run is 1.3 to 2.8 times slower than on the desktop.
- The page keeps nothing and sends nothing: a Content-Security-Policy makes the browser refuse
  every request, and the engine runs in a worker that cannot read the recipient's files, in all
  three browsers. Text from the model is shown as text. Open only viewer files from people you trust: a file someone edited
  is no longer the file the export made.
- The licences of the software inside the file are in the file, under "Licences of the software
  in this file".
