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

The page is a console: the study's question and the scenario tabs on top, the levers on the
left, the line in the middle, and the answer, the results and the trust card on the right. It
follows the browser's light or dark setting, and a switch on the page overrides it. Below 900 px
the panels stack in one column.

- **The question** at the top: the dashboard's `question`, else the model's title.
- **Levers**: the inputs of the model's dashboard definition (its `dashboard` section), the ones
  you wrote or the ones generated from the model. Each shows its range, a mark at its default,
  "default N" once moved, and your `note` (for instance "+48 k EUR each"). The engine refuses a
  value outside the range. "N changed" counts the moved levers; **Reset to defaults** puts them
  back. **Replications** go from 1 to the definition's `limits.max_replications`; the seed is
  the model's own.
- **Scenario tabs**: Baseline, each of your scenarios whose overrides set only the levers, Yours
  (what the levers say now), and the ones the reader saved. A tab puts its values on the levers.
- **Run my version**, and **Cancel**. A run goes on until its answer is clear: while it is "not
  clear yet" it runs again with the replications the comparison says it needs, up to
  `limits.max_replications`. Cancel stops at once; the engine then starts again (about 10 s).
- **The answer** for the first result: "Yes, very likely", "No" or "Not clear yet". With a
  `target` and a direction (`better`, or the KPI's known one) it says whether the whole 95%
  interval is on the right side of the target; without a target, whether the result is better
  than the baseline. It is worked out from the numbers, never shown by colour alone.
- **Results**: each result's 95% interval as a bar, the baseline's below it, the target as a
  dotted line, and the change with an arrow and a word ("better", "worse", "no clear change").
  "All numbers" holds the table: means, intervals and the paired difference. When a lever
  moves, the results go grey until the next run.
- **Can I trust this?**: the law checks, the warm-up removed, the replications and the precision,
  and what was measured on the input data and the parameters, or "not measured" (see below).
- **The line**: the recorded run of your baseline, then, after a run, yours beside the baseline,
  both with the model's seed and recorded in the browser.
- **The footer**: **Save as scenario** keeps the levers as a tab in this browser; **Picture for
  the report** draws the question, the answer and the results as a picture to save as PNG;
  **Compare two scenarios** runs one tab against another. Nothing leaves the page.

Starting takes about 10 s (the page says which step it is at); your baseline is on screen at
once, and the time a run takes is shown before it starts.

## Measured rows on the trust card

The trust card's input-data and parameter rows quote measurements, never estimates: the latest
`input_uncertainty` answer and the latest ABC `calibrate` answer. A study version keeps them in
`versions/<id>/asked.json`; `naga viewer model.json -o model.html --asked asked.json` (and
`--asked` on `naga run --player` and `naga scenarios --player`) quotes such a file. A file that is
not a list of answers, or holds an answer whose numbers the card cannot read, is refused with
`E_ASKED_MALFORMED` before anything runs. The page carries only the fields the card quotes, and
the file is the caller's word: nothing checks that it was measured on this model.

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
- Saved scenarios stay in the reader's browser: another browser or a cleared one does not have them.
- A run is 1.3 to 2.8 times slower than on the desktop.
- The page keeps nothing and sends nothing: a Content-Security-Policy makes the browser refuse
  every request, and the engine runs in a worker that cannot read the recipient's files, in all
  three browsers. Text from the model is shown as text. Open only viewer files from people you trust: a file someone edited
  is no longer the file the export made.
- The licences of the software inside the file are in the file, under "Licences of the software
  in this file".
