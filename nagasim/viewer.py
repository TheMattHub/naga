"""The browser viewer (D320-D333): one HTML file in which this engine, unmodified, runs
in a browser and re-runs a model, for client staff to explore and show around. It is
not an operational screen (that is the Presenter and the monitor) and not a confidential
share (that is the player, `player.py`): whoever receives the file can read the model.

    viewer_problems(layout)                why a model cannot be a viewer, or []
    what_if(layout, values)                the run both sides make: the builder for the
                                           author's baseline, the page for every Run
    compare(record, baseline)              is the page's baseline the author's
    build_viewer(layout, path)             the page
    freeze_data(layout, base_dir)          the copy of a model with its data read once and
                                           written in, for a model fed from files

The inputs, results and limits are the model's `dashboard` definition
(`dashboard.resolve_dashboard`), so the page holds what the Presenter holds and nothing
the author did not expose.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import json
import math
import os
import re
import time

from nagasim import dashboard, experiments
from nagasim.core.problems import Problem, problem
from nagasim.core.schema.sections_validation import DASHBOARD_LIMITS
from nagasim.viewer_runtime import Cancelled

__all__ = ["MAX_MODEL_BYTES", "Cancelled", "engine_on_disk", "viewer_problems", "freeze_data",
           "what_if", "compare", "build_viewer"]

# The model text a page may carry. The largest bundled model is 0.25 MB.
MAX_MODEL_BYTES = 5_000_000
# Sections that do not change a run and may name a file of the author's machine.
_NOT_SHIPPED = ("view", "background")
_YOURS = "Yours"
# A run in a browser against the same run here (measured: 1.3 to 2.8 times).
_BROWSER_FACTOR = 2.5
_HINT = "share a recording instead: `naga run MODEL --player FILE.html`"


# -- what a model may not carry (D325) --------------------------------------------
def _has_key(node, key: str) -> bool:
    if isinstance(node, dict):
        return node.get(key) is not None or any(_has_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_has_key(item, key) for item in node)
    return False


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def viewer_problems(layout: dict) -> list[Problem]:
    """Why *layout* cannot be shared as a viewer: each reason names what to remove.
    Checked when a page is built and again in the page before every run."""
    from nagasim.plugins import load_plugins

    found: list[Problem] = []

    def refuse(message: str, path: str) -> None:
        found.append(problem("E_VIEWER_MODEL", message, path, hint=_HINT))

    for section in ("data_sources", "data_bindings"):
        names = [str(entry.get("name", "?"))[:40] for entry in layout.get(section) or []
                 if isinstance(entry, dict)]
        if names:
            refuse(f"{section} ({', '.join(names[:5])}): a viewer reads no data source; "
                   "freeze the data into the copy (`naga viewer --freeze`) or remove them",
                   f"/{section}")
    for entry in layout.get("agent_types") or []:
        if _has_key(entry, "python"):
            refuse(f"agent type '{str(entry.get('name', '?'))[:40]}' runs Python: a viewer "
                   "runs no code but the engine's", "/agent_types")
    for block in layout.get("blocks") or []:
        if isinstance(block, dict) and block.get("type") == "LOGIC":
            refuse(f"block '{str(block.get('name', '?'))[:40]}' is a LOGIC block: it runs a "
                   "class of custom_logic.py, which a viewer does not carry", "/blocks")
    plugged = {entry.partition(":")[2] for entry in load_plugins().loaded
               if entry.startswith(("blocks:", "distributions:"))}
    used = sorted(plugged.intersection(_strings(layout))) if plugged else []
    if used:
        refuse(f"{', '.join(used[:5])}: registered by a plugin, and a viewer loads no plugin", "")
    size = len(json.dumps(layout, separators=(",", ":")))
    if size > MAX_MODEL_BYTES:
        refuse(f"the model is {size} bytes, over the {MAX_MODEL_BYTES} a viewer carries", "")
    return found


def freeze_data(layout, base_dir=None, *, network=None) -> tuple[dict, list[str]]:
    """(the copy of *layout* with its data frozen in, the names of the sources read).

    A client's model is fed from files and databases, and a viewer reads none (D333).
    So at export the sources are read ONCE, exactly as `naga bind` reads them, with the
    caller's permission (*network*, a `netguard.NetworkPolicy`; default: none, so only
    files are read), and the values read are written into the copy as plain model data
    (`databind.resolve_bindings`, the transformation a run with `bind_data` applies).
    The copy then holds no `data_sources` and no `data_bindings`: no location, path,
    address, credential or connection.

    Refused by name (ValueError): a live source (rest, mqtt, opcua), which is a plant's
    feed and means nothing in a copy; a binding that could not be read; a copy over
    `MAX_MODEL_BYTES`; a source's location still written somewhere in the copy.
    """
    from nagasim import databind
    from nagasim.api import load_layout
    from nagasim.core.schema import CONNECTION_SOURCE_KINDS

    layout = load_layout(layout)
    sources = [s for s in layout.get("data_sources") or [] if isinstance(s, dict)]
    live = [str(s.get("name", "?"))[:40] for s in sources
            if s.get("kind") in CONNECTION_SOURCE_KINDS]
    if live:
        raise ValueError(f"data source {', '.join(live)}: a live source is a plant's feed and "
                         "is not frozen into a viewer; a twin is shown by the monitor")
    resolved, report = databind.resolve_bindings(layout, base_dir, network=network)
    if report.problems:
        raise ValueError("the data could not be read, so nothing was frozen: "
                         + "; ".join(p.message for p in report.problems[:5]))
    copy = {key: value for key, value in resolved.items()
            if key not in ("data_sources", "data_bindings")}
    text = json.dumps(copy)
    if len(text) > MAX_MODEL_BYTES:
        raise ValueError(f"the model with its data frozen in is {len(text)} bytes, over the "
                         f"{MAX_MODEL_BYTES} a viewer carries: bind less data")
    for source in sources:
        location = str(source.get("location") or "")
        # In any spelling a tool may have written it back in (`databind._spellings`: the
        # other slash, doubled backslashes, percent-encoded) and in any letter case.
        needles = {json.dumps(spelling)[1:-1].lower()
                   for spelling in databind._spellings(location)} if location else set()
        if any(needle in text.lower() for needle in needles):
            where = [key for key, value in copy.items()
                     if any(needle in json.dumps(value).lower() for needle in needles)]
            raise ValueError(f"data source {str(source.get('name', '?'))[:40]}: its location is "
                             f"also written in the model's {', '.join(where[:5])}; remove it "
                             "there, the copy must not name where its data came from")
    return copy, [str(s.get("name", "?")) for s in sources]


def _refused(problems: list[Problem]) -> ValueError:
    return ValueError("this model cannot be a viewer: "
                      + "; ".join(p.message for p in problems[:5]))


# -- the run both sides make (D326) -----------------------------------------------
def _number(value):
    """A finite float, else None (JSON has no NaN)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _versions() -> dict:
    import platform

    import numpy
    import pandas
    import scipy
    import simpy

    import nagasim
    return {"nagasim": nagasim.__version__, "python": platform.python_version(),
            "numpy": numpy.__version__, "pandas": pandas.__version__,
            "scipy": scipy.__version__, "simpy": simpy.__version__}


def what_if(layout: dict, values: dict | None = None, *, replications: int | None = None,
            on_progress=None, should_stop=None) -> dict:
    """Run *layout* with the viewer's *values* ({input path: value}) beside its baseline
    and return one frozen record (D248), JSON-safe:

        inputs, replications, seed, seeds   what made it
        rows      per result KPI and scenario: mean, 95% interval, difference to the
                  baseline with its paired interval, significant
        cells     {scenario: {KPI: [the value of each replication]}}, every KPI
        versions, seconds

    The what-if is the Presenter's: `input_overrides` (which enforces each range), one
    scenario appended, `run_scenarios` with common random numbers. The seed is the
    model's own, never the viewer's. Raises ValueError for a model a viewer cannot
    carry, a value outside its input, or replications outside 1..max_replications;
    `Cancelled` when *should_stop* (asked between replications) said to stop.
    """
    problems = viewer_problems(layout)
    if problems:
        raise _refused(problems)
    values = dict(values or {})
    overrides = dashboard.input_overrides(layout, values)       # ValueError
    definition = dashboard.resolve_dashboard(layout)
    limits = definition["limits"]
    # The engine's own ceiling whatever the definition says: in a page the definition is
    # text of the file, and a file can be edited (M4).
    most = min(int(limits["max_replications"]), DASHBOARD_LIMITS["replications"])
    count = limits["replications"] if replications is None else replications
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= most:
        raise ValueError(f"replications must be a whole number from 1 to "
                         f"{most}, not {str(count)[:20]}")
    seed = int((layout.get("simulation") or {}).get("seed") or 0)
    stored = list(layout.get("scenarios") or [])
    name = _YOURS
    while any(s.get("name") == name for s in stored):
        name += "*"
    made = {**layout, "scenarios": stored + [{"name": name, "overrides": overrides}]}
    started = time.perf_counter()
    result = experiments.run_scenarios(made, [name], replications=count, seed=seed,
                                       workers=1, on_progress=on_progress,
                                       should_stop=should_stop)
    seconds = time.perf_counter() - started
    if not result.completed:
        raise Cancelled("the run was cancelled")

    labels = {entry["kpi"]: entry.get("label") or entry["kpi"] for entry in definition["results"]}
    rows = []
    if labels:
        frame = result.comparison(kpis=list(labels))
        order = {kpi: index for index, kpi in enumerate(labels)}
        for row in sorted(frame.to_dict("records"), key=lambda r: order.get(r["KPI"], 0)):
            rows.append({"kpi": row["KPI"], "label": labels.get(row["KPI"], row["KPI"]),
                         "scenario": _YOURS if row["Scenario"] == name else row["Scenario"],
                         "mean": _number(row["Mean"]), "lb": _number(row["LB"]),
                         "ub": _number(row["UB"]), "diff": _number(row["Diff vs baseline"]),
                         "diff_lb": _number(row["Diff LB"]), "diff_ub": _number(row["Diff UB"]),
                         "significant": bool(row["Significant"])})
    cells = {}
    for scenario, frames in result.results.items():
        flat = experiments.flatten_kpis(frames).sort_values(["KPI", "Run"], kind="stable")
        table: dict[str, list] = {}
        for kpi, value in zip(flat["KPI"], flat["Value"]):
            table.setdefault(str(kpi), []).append(_number(value))
        cells[_YOURS if scenario == name else scenario] = table
    return {"inputs": {path: item["value"] for path, item in
                       zip(values, overrides)},
            "replications": count, "seed": seed,
            "seeds": [int(s) for s in result.seeds.get(experiments.BASELINE, [])],
            "rows": rows, "cells": cells, "versions": _versions(), "seconds": seconds}


def compare(record: dict, baseline: dict, tolerance: float = 1e-9) -> dict:
    """Is the baseline of *record* (a run in a page) the author's *baseline* record?

    Replication r of every run has seed `seed + r`, so the first replications of two
    runs of one model are the same runs: every KPI cell of the replications both hold
    must be EQUAL. The interval bounds are compared only when both ran the same number
    of replications, and to *tolerance* (relative): they hold a Student-t quantile,
    which differs in the last digits between scipy versions (D328).
    Returns {"replications", "cells", "different", "bounds", "bounds_different"}."""
    mine = record["cells"].get(experiments.BASELINE, {})
    theirs = baseline["cells"].get(experiments.BASELINE, {})
    shared = min(record["replications"], baseline["replications"])
    cells = different = 0
    for kpi in sorted(set(mine) | set(theirs)):
        a, b = mine.get(kpi, [])[:shared], theirs.get(kpi, [])[:shared]
        cells += max(len(a), len(b))
        different += abs(len(a) - len(b)) + sum(1 for x, y in zip(a, b) if x != y)
    bounds = bounds_different = 0
    if record["replications"] == baseline["replications"]:
        theirs_rows = {row["kpi"]: row for row in baseline["rows"]
                       if row["scenario"] == experiments.BASELINE}
        for row in record["rows"]:
            other = theirs_rows.get(row["kpi"])
            if row["scenario"] != experiments.BASELINE or other is None:
                continue
            for key in ("mean", "lb", "ub"):
                x, y = row[key], other[key]
                bounds += 1
                if (x is None) != (y is None) or (x is not None and not math.isclose(
                        x, y, rel_tol=tolerance, abs_tol=tolerance * 1e-3)):
                    bounds_different += 1
    return {"replications": shared, "cells": cells, "different": different,
            "bounds": bounds, "bounds_different": bounds_different}


# -- the page (D323, D324) ---------------------------------------------------------
# What goes into the file is decided here and nowhere else:
#
# * A CONSTANT page (`_PAGE`, `_CSS`, `_MAIN`, `_WORKER`) plus data blocks. A
#   Content-Security-Policy with `default-src 'none'` makes the browser refuse every
#   request; it pins the one script of the main thread and the one style sheet by their
#   hashes. Text from a model reaches the main thread only as JSON in a data block (the
#   player's `_embed`) and is put on the page with `textContent`.
# * The engine runs in a module worker made from a `data:` URL: an origin of its own,
#   which cannot read a file beside the page in any browser (measured: Firefox lets the
#   main thread and a `blob:` worker do so). The worker answers Pyodide's `fetch` from
#   the embedded bytes only.
# * Embedded, each as released: Pyodide's core files and wheels (`viewer_runtime`), the
#   author's installed simpy, openpyxl and et_xmlfile, and this package without its
#   example models and without `core/custom_logic.py` (the author's own code, which only
#   a LOGIC block imports, and a viewer refuses those).
_CSS = """
:root { --ink: #1a202c; --muted: #5a6475; --line: #d8dee8; --accent: #2b6cb0; --warn: #9c4221; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: var(--ink); margin: 0 auto; padding: 16px; background: #fff; max-width: 1100px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 20px 0 6px; }
p { margin: 4px 0; }
.note { color: var(--muted); font-size: 13px; }
.warn { color: var(--warn); }
#status { padding: 6px 10px; background: #f1f5f9; border-left: 3px solid var(--accent); margin: 12px 0; }
.input { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 4px 0; }
.input label { flex: 0 0 260px; overflow: hidden; text-overflow: ellipsis; }
.input input[type=range] { flex: 1 1 200px; }
.input input[type=number] { width: 110px; font: inherit; }
.input select { font: inherit; }
#bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; }
#bar button { font: inherit; padding: 6px 18px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 4px 8px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.stale td, .stale th { color: #a0a8b5; }
.sig { font-weight: 600; }
#foot { margin-top: 24px; }
pre { white-space: pre-wrap; font-size: 11px; max-height: 420px; overflow: auto;
      border: 1px solid var(--line); padding: 8px; }
iframe { width: 100%; height: 80vh; border: 1px solid var(--line); }
details { margin-top: 20px; }
.gone { display: none; }
"""

# What the engine is asked, in the worker's Python. MODEL and BASELINE are the texts of
# the data blocks, never read by the main thread's JavaScript on the way.
_BOOT = """
import json
from nagasim import viewer
_LAYOUT = json.loads(MODEL)
_BASE = json.loads(BASELINE)
def _run(args):
    args = json.loads(args)
    try:
        record = viewer.what_if(_LAYOUT, args["values"], replications=args["replications"],
                                on_progress=PROGRESS)
    except ValueError as exc:
        return json.dumps({"error": str(exc)[:500]})
    return json.dumps({"record": record, "same": viewer.compare(record, _BASE)}, allow_nan=False)
"""

_WORKER = """"use strict";
/* The engine's worker. It holds no address: Pyodide's requests are answered from the
   bytes the page handed over, and anything else is refused here and by the policy. */
var py = null;
var NOWHERE = "https:" + "/" + "/embedded.invalid/";
var BOOT = __BOOT__;

async function bytes(file) {
  var raw = Uint8Array.fromBase64 ? Uint8Array.fromBase64(file.b64)
    : Uint8Array.from(atob(file.b64), function (c) { return c.charCodeAt(0); });
  if (!file.gz) { return raw; }
  var plain = new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(plain).arrayBuffer());
}

async function boot(cfg) {
  var files = {}, i;
  postMessage(["stage", 1]);
  for (i = 0; i < cfg.files.length; i += 1) { files[cfg.files[i].name] = await bytes(cfg.files[i]); }
  self.fetch = async function (what) {
    var name = String(what && what.url || what).split("/").pop().split("?")[0];
    if (!Object.prototype.hasOwnProperty.call(files, name)) { throw new TypeError("this page holds no " + name); }
    return new Response(files[name], {headers: {"Content-Type":
      name.slice(-5) === ".wasm" ? "application/wasm" : "application/octet-stream"}});
  };
  var url = function (name) { return URL.createObjectURL(new Blob([files[name]], {type: "text/javascript"})); };
  postMessage(["stage", 2]);
  var mod = await import(url("pyodide.mjs"));
  var asm = await import(url("pyodide.asm.mjs"));
  py = await mod.loadPyodide({createPyodideModule: asm.default, indexURL: NOWHERE, packageBaseUrl: NOWHERE,
    lockFileContents: new TextDecoder().decode(files["pyodide-lock.json"])});
  postMessage(["stage", 3]);
  await py.loadPackage(cfg.packages, {messageCallback: function () {}});
  py.unpackArchive(files["engine.zip"], "zip",
    {extractDir: py.runPython("import site; site.getsitepackages()[0]")});
  postMessage(["stage", 4]);
  py.globals.set("MODEL", cfg.model);
  py.globals.set("BASELINE", cfg.baseline);
  py.globals.set("PROGRESS", function (done, total) { postMessage(["progress", done, total]); });
  py.runPython(BOOT);
  postMessage(["ready"]);
}

self.onmessage = async function (event) {
  var what = event.data[0];
  try {
    if (what === "boot") { await boot(event.data[1]); }
    if (what === "run") {
      py.globals.set("ARGS", event.data[1]);
      postMessage(["done", py.runPython("_run(ARGS)")]);
    }
  } catch (error) {
    postMessage(["failed", what, String(error && error.message || error).slice(-600)]);
  }
};
"""

_MAIN = """"use strict";
/* The page of a viewer. Everything from the model is data: it reaches the page as
   textContent, and the tag of every element made here is written here. */

var STAGES = ["", "reading the engine", "starting Python", "loading the libraries", "loading the model"];

/* ---- the pure half ---- */

function fmt(value) {
  if (value === null || value === undefined) { return "-"; }
  var size = Math.abs(value);
  if (size !== 0 && (size >= 1e7 || size < 1e-3)) { return value.toExponential(3); }
  return String(parseFloat(value.toPrecision(4)));
}

function span(low, high) {
  return (low === null || high === null || low === undefined) ? "-" : fmt(low) + " to " + fmt(high);
}

/* Only what the viewer moved: an untouched input stays the model's own value. */
function moved(initial, now) {
  var out = {}, path;
  for (path in now) {
    if (Object.prototype.hasOwnProperty.call(now, path) && now[path] !== initial[path]) { out[path] = now[path]; }
  }
  return out;
}

function seconds(value) {
  return value < 90 ? Math.max(1, Math.round(value)) + " s" : Math.round(value / 60) + " min";
}

/* One line per result: [label, baseline row, yours row or null]. */
function grouped(rows) {
  var order = [], byKpi = {};
  rows.forEach(function (row) {
    if (!Object.prototype.hasOwnProperty.call(byKpi, row.kpi)) { byKpi[row.kpi] = [row.label, null, null]; order.push(row.kpi); }
    byKpi[row.kpi][row.scenario === "Baseline" ? 1 : 2] = row;
  });
  return order.map(function (kpi) { return byKpi[kpi]; });
}

function sameText(same, here, there) {
  if (same.different === 0 && same.bounds_different === 0) {
    return "Same engine: this browser's baseline equals the author's run in all " + same.cells +
      " values compared (" + same.replications + " replications).";
  }
  return "This browser computed " + same.different + " of " + same.cells + " baseline values and " +
    same.bounds_different + " of " + same.bounds + " interval bounds differently from the author's run " +
    "(here: Python " + here.python + ", numpy " + here.numpy + ", scipy " + here.scipy +
    "; the author: Python " + there.python + ", numpy " + there.numpy + ", scipy " + there.scipy + ").";
}

/* ---- the page half ---- */

function boot() {
  var el = function (id) { return document.getElementById(id); };
  var D = JSON.parse(el("data").textContent);
  var modelText = el("model").textContent, baselineText = el("baseline").textContent;
  var author = JSON.parse(baselineText);
  var state = {worker: null, ready: false, starting: false, stopped: false, pending: false,
               running: false, shown: null, since: 0, clock: null, initial: {}, read: {}};

  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function add(tag, parent, text, name) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) { node.textContent = text; }
    if (name) { node.className = name; }
    parent.appendChild(node);
    return node;
  }
  function status(text, bad) { el("status").textContent = text; el("status").className = bad ? "warn" : ""; }
  function buttons() {
    el("run").disabled = state.running || state.starting || !(state.ready || state.stopped);
    el("cancel").disabled = !(state.running || state.starting);
  }

  /* -- inputs -- */
  function numberInput(entry, row) {
    var start = Number(D.values[entry.path]);
    var slide = add("input", row), box = add("input", row);
    slide.type = "range"; box.type = "number";
    [slide, box].forEach(function (node) {
      node.min = entry.min; node.max = entry.max; node.step = entry.step; node.value = start;
    });
    slide.setAttribute("aria-label", entry.label || entry.path);
    slide.addEventListener("input", function () { box.value = slide.value; changed(); });
    box.addEventListener("input", function () { slide.value = box.value; changed(); });
    state.initial[entry.path] = start;
    state.read[entry.path] = function () { return box.value === "" ? NaN : Number(box.value); };
  }
  function switchInput(entry, row) {
    var box = add("input", row);
    box.type = "checkbox"; box.checked = D.values[entry.path] === true;
    box.addEventListener("change", changed);
    state.initial[entry.path] = box.checked;
    state.read[entry.path] = function () { return box.checked; };
  }
  function choiceInput(entry, row) {
    var list = add("select", row), start = 0;
    entry.choices.forEach(function (choice, index) {
      var option = add("option", list, choice.label || String(choice.value));
      option.value = String(index);
      if (choice.value === D.values[entry.path]) { start = index; }
    });
    list.value = String(start);
    list.addEventListener("change", changed);
    state.initial[entry.path] = entry.choices[start].value;
    state.read[entry.path] = function () { return entry.choices[Number(list.value)].value; };
  }
  function inputs() {
    var box = el("inputs");
    D.inputs.forEach(function (entry) {
      var row = add("div", box, null, "input");
      add("label", row, (entry.label || entry.path) + (entry.unit ? " (" + entry.unit + ")" : ""));
      if (entry.kind === "switch") { switchInput(entry, row); }
      else if (entry.kind === "choice") { choiceInput(entry, row); }
      else { numberInput(entry, row); }
    });
    if (!D.inputs.length) { add("p", box, "This model exposes no inputs; you can run it as it is.", "note"); }
    if (D.frozen) { add("p", box, "Data as read on " + D.frozen + ".", "note"); }
    var reps = el("reps");
    reps.min = 1; reps.max = D.limits.max_replications; reps.value = D.limits.replications;
    reps.addEventListener("input", changed);
    el("reps-note").textContent = "1 to " + D.limits.max_replications + ". Seed " + author.seed +
      ", the model's own.";
  }
  function now() {
    var out = {}, path;
    for (path in state.read) { out[path] = state.read[path](); }
    return out;
  }
  function estimate() {
    var count = Number(el("reps").value) || 0, wait = D.estimate * count;
    el("estimate").textContent = wait >= 5 ? "A run of " + count + " replications takes about " +
      seconds(wait) + " in a browser." : "";
  }
  function changed() {
    estimate();
    if (state.shown) { el("results").className = "stale"; el("stale").className = "note warn"; }
  }

  /* -- results -- */
  function table(record, mine) {
    var head = el("results-head"), body = el("results-body");
    clear(head); clear(body);
    var titles = ["Result", "Baseline", "95% interval"];
    if (mine) { titles = titles.concat(["Yours", "95% interval", "Difference", "95% interval"]); }
    var top = add("tr", head);
    titles.forEach(function (title) { add("th", top, title); });
    grouped(record.rows).forEach(function (line) {
      var row = add("tr", body), base = line[1] || {}, yours = line[2] || {};
      add("td", row, line[0]);
      add("td", row, fmt(base.mean)); add("td", row, span(base.lb, base.ub));
      if (mine) {
        add("td", row, fmt(yours.mean)); add("td", row, span(yours.lb, yours.ub));
        add("td", row, fmt(yours.diff), yours.significant ? "sig" : "");
        add("td", row, span(yours.diff_lb, yours.diff_ub) + (yours.significant ? " *" : ""));
      }
    });
    if (!record.rows.length) {
      add("td", add("tr", body), "The author listed no results for this model.").colSpan = titles.length;
    }
    el("results").className = ""; el("stale").className = "gone";
  }
  function made(record, mine) {
    var parts = [], path, label = {};
    D.inputs.forEach(function (entry) { label[entry.path] = entry.label || entry.path; });
    for (path in record.inputs) { parts.push((label[path] || path) + " = " + record.inputs[path]); }
    var seeds = record.seeds;
    el("made").textContent = (mine ? "Yours: " + (parts.join(", ") || "the inputs as the model has them")
      : "The author's run of the model as it is") + ". " + record.replications + " replications, seeds " +
      seeds[0] + " to " + seeds[seeds.length - 1] + ", common random numbers; the model's own run length." +
      (mine ? " * the 95% interval of the difference excludes 0." : "");
  }
  function show(answer) {
    el("results-title").textContent = "Results of your run";
    table(answer.record, true); made(answer.record, true);
    el("same").textContent = sameText(answer.same, answer.record.versions, author.versions);
    el("same").className = answer.same.different || answer.same.bounds_different ? "note warn" : "note";
    el("record").textContent = JSON.stringify(answer);
    state.shown = answer;
  }

  /* -- the engine -- */
  function files() {
    var out = [], blocks = document.querySelectorAll('script[type="application/octet-stream"]'), i;
    for (i = 0; i < blocks.length; i += 1) {
      out.push({name: blocks[i].dataset.name, gz: blocks[i].dataset.gz === "1", b64: blocks[i].textContent});
    }
    return out;
  }
  function failed(text) {
    state.ready = false; state.starting = false; state.pending = false; state.running = false;
    clearInterval(state.clock); buttons();
    status("The engine could not start in this browser (" + text + "). Open this file in Chrome, Edge " +
      "or Firefox on a computer; Safari, phones and tablets are not supported.", true);
  }
  function heard(event) {
    var message = event.data, kind = message[0];
    if (kind === "stage") {
      status("Starting the engine in this browser: step " + message[1] + " of 4, " + STAGES[message[1]] +
        ". About 10 s in all.");
    } else if (kind === "ready") {
      state.ready = true; state.starting = false; buttons();
      status("Ready after " + seconds((performance.now() - state.since) / 1000) +
        ". The engine runs in this browser; nothing is sent anywhere.");
      if (state.pending) { state.pending = false; run(); }
    } else if (kind === "progress") {
      el("progress").textContent = "replication " + message[1] + " of " + message[2];
    } else if (kind === "done") {
      state.running = false; clearInterval(state.clock); buttons();
      var answer = JSON.parse(message[1]);
      el("progress").textContent = "";
      if (answer.error) { status("Not run: " + answer.error, true); return; }
      status("Run finished in " + seconds((performance.now() - state.since) / 1000) + ".");
      show(answer);
    } else if (kind === "failed") {
      if (message[1] === "boot") { failed(message[2]); return; }
      state.running = false; clearInterval(state.clock); buttons();
      status("The run stopped with an error: " + message[2], true);
    }
  }
  function start() {
    state.ready = false; state.starting = true; state.stopped = false;
    state.since = performance.now(); buttons();
    status("Starting the engine in this browser: step 1 of 4, " + STAGES[1] + ". About 10 s in all.");
    try {
      state.worker = new Worker("data:text/javascript;base64," + btoa(el("worker").textContent), {type: "module"});
    } catch (error) { failed(String(error && error.message || error)); return; }
    state.worker.onmessage = heard;
    state.worker.onerror = function (event) { failed(event.message || "the worker did not start"); };
    state.worker.postMessage(["boot", {files: files(), model: modelText, baseline: baselineText,
                                       packages: D.packages}]);
  }
  function run() {
    if (!state.ready) { state.pending = true; start(); return; }   /* stopped while starting */
    var count = Number(el("reps").value);
    state.running = true; state.since = performance.now(); buttons();
    el("progress").textContent = "";
    status("Running...");
    state.clock = setInterval(function () {
      status("Running: " + seconds((performance.now() - state.since) / 1000) + " so far.");
    }, 1000);
    state.worker.postMessage(["run", JSON.stringify({values: moved(state.initial, now()), replications: count})]);
  }
  /* A busy worker hears nothing, so Cancel ends it: the run and the interpreter are
     lost, the inputs and the last complete result stay. After a run a new engine starts
     at once; an engine stopped while it was starting starts again when Run is pressed. */
  function cancel() {
    var ran = state.running;
    state.worker.terminate(); state.worker = null;
    state.running = false; state.starting = false; state.ready = false; state.pending = false;
    clearInterval(state.clock);
    el("progress").textContent = "";
    if (ran) {
      start();
      status("Cancelled. Starting the engine again: about 10 s.");
    } else {
      state.stopped = true; buttons();
      status("The engine was stopped before it was ready. Press Run to start it and run.");
    }
  }
  /* The text of a compressed block, read once, when its section is first opened. */
  function unpack(section, block, then) {
    if (!el(section).open || !el(block).textContent) { return; }
    var raw = Uint8Array.from(atob(el(block).textContent), function (c) { return c.charCodeAt(0); });
    el(block).textContent = "";
    new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"))).text().then(then);
  }
  function notices() {
    unpack("notices", "notices-data", function (text) { el("notices-text").textContent = text; });
  }
  /* The player page of the author's run (the 7.7 player, whole), in a frame with an
     origin of its own. It replays what the author recorded: it runs nothing. */
  function watch() {
    unpack("watch", "player-data", function (text) { el("player").srcdoc = text; });
  }

  document.title = D.title;
  el("title").textContent = D.title;
  el("versions").textContent = "Naga engine " + author.versions.nagasim + " on Python " + D.python +
    " (Pyodide " + D.pyodide + "), in this file with everything it needs. This file contains the whole " +
    "model and the engine's source: anyone who has it can read both.";
  inputs(); estimate();
  table(author, false); made(author, false);
  el("run").addEventListener("click", run);
  el("cancel").addEventListener("click", cancel);
  el("notices").addEventListener("toggle", notices);
  if (el("player-data").textContent) {
    el("watch").className = "";
    el("watch-note").textContent = "One replication of the model as the author left it (seed " +
      author.seed + "), recorded when this file was made. It does not follow your inputs.";
    el("watch").addEventListener("toggle", watch);
  }
  buttons();
  /* The embedded files follow this script in the file: wait until they are parsed. */
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); }
  else { start(); }
}

if (typeof document !== "undefined") { boot(); }
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-__MAIN_HASH__' 'sha256-__PLAYER_HASH__' blob: 'wasm-unsafe-eval'; style-src 'sha256-__CSS_HASH__' 'sha256-__PLAYER_CSS_HASH__'; img-src data:; worker-src data:; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Naga viewer</title>
<style>__CSS__</style>
</head>
<body>
<h1 id="title"></h1>
<p class="note">Naga viewer: the model as its author left it. Change the inputs and run it again.</p>
<p id="status">This page needs JavaScript.</p>
<h2>Inputs</h2>
<div id="inputs"></div>
<div class="input"><label for="reps">Replications</label><input id="reps" type="number" step="1">
<span class="note" id="reps-note"></span></div>
<div id="bar"><button id="run" type="button" disabled>Run</button>
<button id="cancel" type="button" disabled>Cancel</button>
<span id="progress"></span><span class="note" id="estimate"></span></div>
<h2 id="results-title">Results of the author's run</h2>
<p class="gone" id="stale">The inputs changed since this run: press Run.</p>
<table id="results"><thead id="results-head"></thead><tbody id="results-body"></tbody></table>
<p class="note" id="made"></p>
<p class="note" id="same"></p>
<details id="watch" class="gone"><summary>Watch the author's recorded run</summary>
<p class="note" id="watch-note"></p>
<iframe id="player" sandbox="allow-scripts" title="The author's recorded run"></iframe></details>
<div id="foot">
<p class="note" id="versions"></p>
<details id="notices"><summary class="note">Licences of the software in this file</summary>
<pre id="notices-text">Reading...</pre></details>
</div>
<pre class="gone" id="record"></pre>
<script type="application/json" id="data">__DATA__</script>
<script type="application/json" id="model">__MODEL__</script>
<script type="application/json" id="baseline">__BASELINE__</script>
<script type="text/plain" id="worker">__WORKER__</script>
<script type="text/plain" id="player-data">__PLAYER__</script>
<script>__MAIN__</script>
<script type="text/plain" id="notices-data">__NOTICES__</script>
__FILES__
</body>
</html>
"""

_GZIPPED = (".wasm", ".mjs", ".json")
_LICENCE_NAME = ("licen", "copying", "notice")
# The pure-Python distributions a run imports besides the engine and Pyodide's wheels.
_PURE = ("simpy", "openpyxl", "et_xmlfile")


def _hash(text: str) -> str:
    import base64
    import hashlib
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


_NOT_ON_DISK = ("the engine's files are not on disk here (a frozen build), so a viewer cannot "
                "carry them: make the viewer with `naga viewer` from an installed engine "
                "(`pip install nagasim`)")


def engine_on_disk() -> bool:
    """Can a viewer be made here? It embeds this package's own files, which a frozen
    build keeps inside its executable."""
    import sys
    from pathlib import Path

    import nagasim
    return not getattr(sys, "frozen", False) and (Path(nagasim.__file__).parent / "viewer.py").is_file()


def _engine_zip() -> tuple[bytes, list[tuple[str, str]]]:
    """(one zip in site-packages layout, [(name, licence text)]): this package as
    installed, without `models/` and `core/custom_logic.py`, and `_PURE` as installed.
    Entries are sorted and carry one fixed date, so two builds give the same archive."""
    import importlib.metadata
    import io
    import zipfile
    from pathlib import Path

    import nagasim

    entries: dict[str, bytes] = {}
    licences: list[tuple[str, str]] = []
    root = Path(nagasim.__file__).parent
    for path in sorted(root.rglob("*")):
        inside = path.relative_to(root).as_posix()
        if not path.is_file() or "__pycache__" in path.parts or inside.startswith("models/") \
                or inside == "core/custom_logic.py":
            continue
        entries["nagasim/" + inside] = path.read_bytes()
    for name in _PURE:
        distribution = importlib.metadata.distribution(name)
        for item in distribution.files or []:
            inside = item.as_posix()
            if inside.startswith("..") or "__pycache__" in inside or inside.endswith(".pyc"):
                continue
            # Of its dist-info only what a licence asks for: RECORD, INSTALLER, REQUESTED
            # and direct_url.json say how and from where the AUTHOR installed it.
            if ".dist-info/" in inside and not inside.endswith(".dist-info/METADATA") \
                    and not any(word in inside.lower() for word in _LICENCE_NAME):
                continue
            data = Path(distribution.locate_file(item)).read_bytes()
            entries[inside] = data
            if ".dist-info/" in inside and any(w in inside.lower() for w in _LICENCE_NAME):
                licences.append((f"{name} {distribution.version}: {inside}",
                                 data.decode("utf-8", "replace")))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for inside in sorted(entries):
            info = zipfile.ZipInfo(inside, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[inside], compresslevel=9)
    return buffer.getvalue(), licences


def _wheel_licences(name: str, data: bytes) -> list[tuple[str, str]]:
    """Every licence file a wheel carries, as text."""
    import io
    import zipfile

    found = []
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        for inside in sorted(wheel.namelist()):
            if any(word in inside.lower() for word in _LICENCE_NAME) and not inside.endswith("/") \
                    and not inside.endswith((".py", ".pyc", ".so", ".h", ".c")):
                found.append((f"{name}: {inside}", wheel.read(inside).decode("utf-8", "replace")))
    return found


def _notices(licences: list[tuple[str, str]]) -> str:
    from pathlib import Path

    text = (Path(__file__).parent / "templates" / "viewer_notices.txt").read_text(encoding="utf-8")
    bar = "=" * 78
    return text + "".join(f"\n{bar}\n{title}\n{bar}\n\n{body.strip()}\n" for title, body in licences)


def _recording(model: dict, baseline: dict, title: str, should_stop=None) -> str:
    """The player page (`player.build_player`) of the author's baseline: its first
    replication run again with its seed and a recorder. "" when it cannot be made (a
    tape over the player's size limit): the viewer is then written without it."""
    from nagasim.api import run_model
    from nagasim.core.trace import TraceRecorder
    from nagasim.player import build_player

    recorder = TraceRecorder()
    try:
        results = run_model(model, replications=baseline["replications"],
                            seed=baseline["seed"], trace=recorder, should_stop=should_stop)
        return build_player(model, {experiments.BASELINE: recorder}, results=results, title=title)
    except ValueError:
        return ""


def _block(name: str, data: bytes) -> str:
    import base64
    import gzip

    packed = name.endswith(_GZIPPED)
    if packed:
        data = gzip.compress(data, 9, mtime=0)
    return ('<script type="application/octet-stream" data-name="%s"%s>%s</script>'
            % (name, ' data-gz="1"' if packed else "", base64.b64encode(data).decode("ascii")))


def build_viewer(layout, path, *, runtime=None, title: str | None = None,
                 baseline: dict | None = None, frozen: str | None = None,
                 recording: str | None = None, on_progress=None, should_stop=None) -> dict:
    """Write the viewer page of *layout* (a model dict or file) to *path* and return
    {"bytes", "estimate", "warnings", "baseline"}.

    *runtime* is the folder of the browser runtime (`viewer_runtime.resolve_dir`); this
    function never downloads. The author's baseline is run here, with the definition's
    own replications, unless *baseline* (a `what_if` record of this model) is given.
    *frozen* is the date the model's data was read on, when it was frozen into the copy.
    *recording* is the player page of the author's run (default: the first baseline
    replication is run again here with a recorder, D200, and `player.build_player`
    makes it; "" for none): the page shows it in a frame, as the author's run.
    "estimate" is the seconds one replication takes in a browser; a warning says when
    the longest run a viewer may ask for passes five minutes, and when the definition
    lists no input or no result.
    `on_progress(done, 5)` is told each step (the model checked, the runtime read, the
    baseline run, the recording made, the file written); *should_stop* is asked between
    steps and between the baseline's replications, and a yes raises `Cancelled`. The
    file is written last, whole, under a temporary name: a cancel or a failure leaves
    none.
    Raises ValueError where the engine's files are not on disk (checked first, before
    anything is read, run or downloaded), for a model a viewer cannot carry and for a
    missing runtime.
    """
    import base64
    import gzip
    from pathlib import Path

    from nagasim import player, viewer_runtime
    from nagasim.api import load_layout
    from nagasim.player import _embed

    if not engine_on_disk():
        raise ValueError(_NOT_ON_DISK)

    def step(done: int) -> None:
        if should_stop is not None and should_stop():
            raise Cancelled("the export was cancelled")
        if on_progress is not None:
            on_progress(done, 5)

    full = load_layout(layout)
    model = {key: value for key, value in full.items() if key not in _NOT_SHIPPED}
    problems = viewer_problems(model) + [p for p in dashboard.check_inputs(model)
                                         if p.severity == "error"]
    if problems:
        raise _refused(problems)
    step(1)
    files = viewer_runtime.load(runtime)                       # before the run: fail early
    step(2)
    definition = dashboard.resolve_dashboard(model)
    if baseline is None:
        baseline = what_if(model, should_stop=should_stop)
    step(3)
    limits = definition["limits"]
    estimate = round(_BROWSER_FACTOR * baseline["seconds"] / baseline["replications"], 3)
    warnings = []
    longest = estimate * limits["max_replications"]
    if longest > 300:
        warnings.append(problem(
            "W_VIEWER_LONG_RUN", f"a run of {limits['max_replications']} replications is "
            f"estimated at {longest / 60:.0f} minutes in a browser", "/dashboard/limits",
            hint="lower dashboard.limits.max_replications"))
    for key, what in (("inputs", "nothing to change"), ("results", "no result to show")):
        if not definition[key]:
            warnings.append(problem("W_VIEWER_EMPTY", f"the dashboard definition lists no "
                                    f"{key}: the viewer has {what}", f"/dashboard/{key}"))

    engine, licences = _engine_zip()
    files = {**files, "engine.zip": engine}
    for name, data in files.items():
        if name.endswith(".whl"):
            licences.extend(_wheel_licences(name, data))
    lock = json.loads(files["pyodide-lock.json"])
    data = {
        "title": str(title or (model.get("info") or {}).get("name") or "Model")[:80],
        "inputs": definition["inputs"], "limits": limits, "estimate": estimate,
        "values": {entry["path"]: experiments.value_at(model, entry["path"])
                   for entry in definition["inputs"]},
        "packages": list(viewer_runtime.PACKAGES), "pyodide": viewer_runtime.VERSION,
        "python": str(lock["info"]["python"]), "frozen": frozen,
    }
    worker = _WORKER.replace("__BOOT__", json.dumps(_BOOT))
    if not (worker.isascii() and _MAIN.isascii()) or "</script" in worker + _MAIN:
        raise AssertionError("the viewer's scripts must be ASCII and hold no closing tag")
    notices = base64.b64encode(gzip.compress(_notices(licences).encode("utf-8"), 9, mtime=0))
    if recording is None:
        recording = _recording(model, baseline, data["title"], should_stop)
    step(4)
    packed = b""
    if recording:
        packed = base64.b64encode(gzip.compress(recording.encode("ascii"), 9, mtime=0))
    if not recording:
        warnings.append(problem("W_VIEWER_EMPTY", "the author's run could not be recorded for "
                                "this model: the viewer has no run to watch", ""))
    # The baseline's time is rounded: it is the one thing in the page that is measured.
    shipped = {**baseline, "seconds": round(baseline["seconds"], 2)}
    parts = {"MAIN_HASH": _hash(_MAIN), "CSS_HASH": _hash(_CSS), "CSS": _CSS, "DATA": _embed(data),
             "MODEL": _embed(model), "BASELINE": _embed(shipped), "WORKER": worker, "MAIN": _MAIN,
             "NOTICES": notices.decode("ascii"), "PLAYER": packed.decode("ascii"),
             "PLAYER_HASH": _hash(player._SCRIPT), "PLAYER_CSS_HASH": _hash(player._CSS),
             "FILES": "\n".join(_block(name, files[name]) for name in sorted(files))}
    # One pass over the constant page: a part is never searched for another part's mark.
    page = re.sub("__([A-Z_]+)__", lambda found: parts[found.group(1)], _PAGE)
    step(4)
    # Bytes, so the file is the same on every system; whole or not at all.
    temporary = Path(str(path) + ".part")
    temporary.write_bytes(page.encode("ascii"))
    os.replace(temporary, path)
    if on_progress is not None:
        on_progress(5, 5)
    return {"bytes": len(page), "estimate": estimate, "warnings": warnings, "baseline": baseline}


# -- naga viewer -------------------------------------------------------------------
_SAYS = ("The file holds the whole model and the engine's source: whoever receives it can "
         "read both. For a confidential model share a recording instead "
         "(`naga run MODEL --player FILE.html`).")


def main(argv=None) -> int:
    """`naga viewer MODEL -o FILE.html` and `naga viewer --fetch`."""
    import argparse
    import datetime
    import sys
    from pathlib import Path

    from nagasim import netguard, viewer_runtime

    parser = argparse.ArgumentParser(
        prog="naga viewer",
        description="Write one HTML file in which the engine runs in a browser and re-runs "
                    "the model with the inputs its dashboard definition exposes. " + _SAYS)
    parser.add_argument("model", nargs="?", type=_bundled,
                        help="the model file, or a bundled model's name (naga examples)")
    parser.add_argument("-o", "--output", help="the HTML file to write")
    parser.add_argument("--title", help="the page's title (default: the model file's name)")
    parser.add_argument("--runtime", metavar="DIR",
                        help="the folder of the browser runtime (default: "
                             f"${viewer_runtime.ENV_VAR}, else the user's own copy)")
    parser.add_argument("--fetch", action="store_true",
                        help=f"download the browser runtime (Pyodide {viewer_runtime.VERSION}, "
                             "29 MB, from github.com and cdn.jsdelivr.net) into that folder; "
                             "nothing is downloaded without this flag")
    parser.add_argument("--freeze", action="store_true",
                        help="read the model's data sources once, now, and write the values "
                             "read into the copy as plain model data: the file then names no "
                             "source, path or address (without it a model with data sources "
                             "is refused)")
    netguard.add_arguments(parser)
    args = parser.parse_args(argv)
    out = sys.stdout.write
    if args.fetch:
        folder = viewer_runtime.fetch(args.runtime, say=lambda text: out(f"fetching {text}\n"))
        viewer_runtime.load(folder)
        out(f"the browser runtime is in {folder}\n")
        if not args.model:
            return 0
    if not args.model or not args.output:
        parser.error("a model and -o FILE are needed (or --fetch alone)")
    layout, frozen = args.model, None
    if args.freeze:
        layout, read = freeze_data(args.model, Path(args.model).parent,
                                   network=netguard.policy_from_args(args))
        frozen = datetime.date.today().isoformat()
        out(f"data read once from {len(read)} source(s) ({', '.join(read) or 'none'}) and "
            "written into the copy, which names no source\n")
    made = build_viewer(layout, args.output, runtime=args.runtime,
                        title=args.title or Path(args.model).stem, frozen=frozen)
    for warning in made["warnings"]:
        sys.stderr.write(f"warning {warning.code}: {warning.message}\n")
    out(f"wrote {args.output}: {made['bytes'] / 1e6:.1f} MB; one replication takes about "
        f"{made['estimate']:.2f} s in a browser\n{_SAYS}\n")
    return 0
