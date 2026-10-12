"""The shareable HTML player (PHASE7 7.7, D196-D201): one self-contained file that
replays a recorded run in a browser, with no server and no editor.

    build_player(layout, {BASELINE: recorder}, results=results, title="Line", path="run.html")

The file leaves its author's machine, so what goes into it is decided here and nowhere
else (D199):

* It is a CONSTANT page (`_PAGE`, `_CSS`, `_SCRIPT`) plus one JSON data block. Text from
  a model (names, labels, types, statistics) is only ever in that block, escaped so that
  nothing in it can end the block, and the script puts it on the page as canvas text or
  `textContent`: never as markup, never as code.
* The data block holds the tape of each run (`core/tape.py`: block names, labels and
  places, the name tables, the sampled state), the title, the seed and replication
  count, and the `system/` and `user/` KPI rows. Nothing else of the model or of the
  machine: no parameter, expression, data source, binding, author or path, and no
  description except as the label of a block still named `submodel_N` (as the canvas shows it).
* The file names no address. A Content-Security-Policy with `default-src 'none'` makes
  the browser refuse every request, and pins the one script by its hash.
* Pictures are the caller's SVG text, each passed through `core/svgsafe.check_svg` and
  embedded as a `data:` image; the engine reads no picture from a model (D198).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from pathlib import Path

from nagasim.core import tape
from nagasim.core.svgsafe import check_svg

__all__ = ["MAX_BYTES", "build_player"]

# An email attachment. Over it the tape is written again with half the samples, down to
# `tape.MIN_SAMPLES`; a run that still does not fit is refused.
MAX_BYTES = 10_000_000

_CSS = """
:root { --ink: #1a202c; --muted: #5a6475; --line: #d8dee8; --accent: #2b6cb0; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: var(--ink); margin: 0; padding: 16px; background: #fff; }
h1 { font-size: 20px; margin: 0 0 8px; }
h2 { font-size: 14px; margin: 16px 0 4px; }
#bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
#bar button, #bar select { font: inherit; padding: 4px 10px; }
#seek { flex: 1 1 240px; }
#clock { min-width: 220px; color: var(--muted); font-variant-numeric: tabular-nums; }
main { display: flex; flex-wrap: wrap; gap: 16px; }
#view { flex: 3 1 480px; min-width: 0; width: 100%; height: 70vh; border: 1px solid var(--line); }
aside { flex: 1 1 260px; font-size: 13px; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 2px 6px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.note { color: var(--muted); margin: 2px 0; }
.row { display: flex; gap: 6px; align-items: center; margin: 2px 0; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.name { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar { flex: 0 0 80px; height: 8px; background: var(--line); }
.bar div { height: 100%; background: var(--accent); }
#foot { color: var(--muted); font-size: 12px; margin-top: 12px; }
"""

_SCRIPT = """"use strict";
/* The drawer of a run player. It draws what the tape says and works nothing out
   itself: a frame's numbers ARE the state. Text from the model reaches the page only
   as canvas text or textContent. */

var LAMP = {idle: "#8a94a6", busy: "#2f9e44", blocked: "#e8590c", down: "#c92a2a",
            off: "#ced4da", loaded: "#2f9e44", handling: "#2f9e44", empty: "#e8590c"};
var SLIDE_MS = 250;
var MAX_SLIDES = 200;

/* A name from the data is a key of a table, never a member every object inherits
   (`constructor`, `__proto__`, `toString`): every lookup by name goes through here. */
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/* ---- the pure half: which sample, and the one thing allowed between two samples ---- */

function sampleIndex(times, t) {
  var last = times.length - 1, k;
  if (last < 1 || !(times[last] > 0)) { return 0; }
  k = Math.min(last, Math.max(0, Math.floor(t / times[last] * last)));
  while (k < last && times[k + 1] <= t) { k += 1; }
  while (k > 0 && times[k] > t) { k -= 1; }
  return k;
}

/* The time of the sample after t (dir 1) or before it (dir -1); t itself when there is
   none. A step is a sample: the tape keeps samples, not single events. */
function stepTime(times, t, dir) {
  var k = sampleIndex(times, t);
  if (dir > 0) {
    while (k < times.length - 1 && times[k] <= t) { k += 1; }
    return times[k] > t ? times[k] : t;
  }
  while (k > 0 && times[k] >= t) { k -= 1; }
  return times[k] < t ? times[k] : t;
}

/* The entries of this sample, each moved the share f of the way to its place in the
   next sample when it is there too. An entry is named by its first `named` fields;
   x and y are fields 2 and 3. One only in this sample stays put; one only in the next
   is not drawn yet. */
function blend(now, next, f, named) {
  var where = {}, out = [], i, entry, to;
  for (i = 0; next && i < next.length; i += 1) {
    where[next[i].slice(0, named).join(":")] = next[i];
  }
  for (i = 0; now && i < now.length; i += 1) {
    entry = now[i].slice();
    to = where[entry.slice(0, named).join(":")];
    if (to) {
      entry[2] += (to[2] - entry[2]) * f;
      entry[3] += (to[3] - entry[3]) * f;
    }
    out.push(entry);
  }
  return out;
}

function blendLevels(now, next, f) {
  var out = {}, key;
  for (key in (now || {})) {
    out[key] = (next && key in next) ? now[key] + (next[key] - now[key]) * f : now[key];
  }
  return out;
}

/* What to draw at time t: the discrete values of the sample at or before t, with
   positions and levels on the line to the next sample. */
function frameAt(run, t) {
  var times = run.t, last = times.length - 1, k = sampleIndex(times, t);
  var a = run.frames[k], b = run.frames[Math.min(k + 1, last)];
  var span = k < last ? times[k + 1] - times[k] : 0;
  var f = span > 0 ? Math.min(1, Math.max(0, (t - times[k]) / span)) : 0;
  return {k: k, f: f, n: a.n, s: a.s, st: a.st || [], u: a.u || {}, q: a.q || {},
          r: a.r || [], kpi: a.k, lv: blendLevels(a.lv, b.lv, f),
          tok: blend(a.tok, b.tok, f, 1), veh: blend(a.veh, b.veh, f, 2),
          ag: blend(a.ag, b.ag, f, 2), more: (a.tokx || 0) + (a.agx || 0), skipped: a.mvx || 0};
}

/* Where scene coordinates land on a canvas of w by h pixels: the middle of the box in
   the middle of the canvas, also when the box is a line or a single point. */
function fit(box, w, h) {
  var pad = 36, bw = Math.max(box[2] - box[0], 1e-6), bh = Math.max(box[3] - box[1], 1e-6);
  var scale = Math.min((w - 2 * pad) / bw, (h - 2 * pad) / bh);
  return {scale: scale, x: w / 2 - (box[0] + box[2]) / 2 * scale,
          y: h / 2 - (box[1] + box[3]) / 2 * scale};
}

function fmt(value) {
  if (value === null || value === undefined) { return "-"; }
  return String(Math.round(value * 1000) / 1000);
}

/* ---- the page half: runs only where there is a document ---- */

function boot() {
  var data = JSON.parse(document.getElementById("tape").textContent);
  var el = function (id) { return document.getElementById(id); };
  var canvas = el("view"), ctx = canvas.getContext("2d");
  var art = data.art || {blocks: {}, types: {}, fleets: {}, fills: {}};
  var pics = (data.pics || []).map(function (source) {
    var image = new Image();
    image.onload = function () { wake(); };
    image.src = source;
    return image;
  });
  var state = {name: data.scenarios[0], t: 0, playing: false, speed: 1, wall: null,
               k: -1, slides: [], busy: false, shown: -1};

  function run() { return own(data.runs, state.name); }
  function duration() { var times = run().t; return times[times.length - 1]; }
  function ready(index) {
    var image = pics[index];
    return image && image.complete && image.naturalWidth > 0 ? image : null;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  /* The one place that makes an element: the tag is always written here, in the
     script, never read from the tape. */
  function add(tag, parent, text, name) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    if (name) { node.className = name; }
    parent.appendChild(node);
    return node;
  }

  function token(x, y, type, radius) {
    var image = type ? ready(own(art.types, type.name)) : null;
    if (image) { ctx.drawImage(image, x - radius * 1.6, y - radius * 1.6, radius * 3.2, radius * 3.2); return; }
    ctx.beginPath();
    if (type && type.shape === "square") {
      ctx.rect(x - radius, y - radius, 2 * radius, 2 * radius);
    } else if (type && type.shape === "triangle") {
      ctx.moveTo(x, y - radius); ctx.lineTo(x + radius, y + radius); ctx.lineTo(x - radius, y + radius);
      ctx.closePath();
    } else {
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
    }
    ctx.fillStyle = type ? type.color : "#000000";
    ctx.fill();
    ctx.strokeStyle = "#1a202c";
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function draw(frame, wall) {
    var scene = run().scene, ratio = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight, view, i, j;
    if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    view = fit(scene.box, w, h);
    function px(x) { return view.x + x * view.scale; }
    function py(y) { return view.y + y * view.scale; }

    ctx.lineWidth = 2;
    ctx.strokeStyle = "#b8c2d0";
    scene.segments.forEach(function (s) {
      ctx.beginPath(); ctx.moveTo(px(s[0]), py(s[1])); ctx.lineTo(px(s[2]), py(s[3])); ctx.stroke();
    });
    ctx.lineWidth = 1;
    ctx.strokeStyle = "#5a6475";
    scene.links.forEach(function (link) {
      var a = scene.blocks[link[0]], b = scene.blocks[link[1]];
      ctx.beginPath(); ctx.moveTo(px(a.x), py(a.y)); ctx.lineTo(px(b.x), py(b.y)); ctx.stroke();
    });

    ctx.font = "11px sans-serif";
    ctx.textBaseline = "middle";
    scene.blocks.forEach(function (block, index) {
      var picture = own(art.blocks, block.name), lamp = frame.st[index] || "";
      var bw = (picture ? picture.w : block.w) * view.scale, bh = (picture ? picture.h : block.h) * view.scale;
      var x = px(block.x), y = py(block.y), left = x - bw / 2, top = y - bh / 2;
      var image = picture ? ready(own(picture.states, lamp) !== undefined ? own(picture.states, lamp) : own(picture.states, "idle")) : null;
      var n = frame.n[index], s = frame.s[index], rows = frame.q[index], chips = frame.u[index], text;
      if (image) {
        ctx.drawImage(image, left, top, bw, bh);
      } else {
        ctx.beginPath();
        ctx.rect(left, top, bw, bh);
        ctx.fillStyle = "#eef2f7";
        ctx.fill();
        ctx.strokeStyle = "#1a202c";
        ctx.stroke();
        if (lamp) {
          ctx.beginPath(); ctx.arc(left + 7, top + 7, 4, 0, 2 * Math.PI);
          ctx.fillStyle = own(LAMP, lamp) || "#8a94a6"; ctx.fill();
        }
      }
      ctx.fillStyle = "#1a202c";
      ctx.textAlign = "center";
      ctx.fillText(block.label, x, top + bh + 9, Math.max(bw * 1.6, 90));
      if (chips) {
        text = chips.map(function (pair) { return pair[0] + " " + pair[1]; }).join("   ");
        ctx.fillStyle = "#5a6475";
        ctx.fillText(text, x, top + bh + 22, Math.max(bw * 2, 140));
      }
      if (n > 0) {
        text = block.q ? s + "|" + (n - s) : String(n);
        ctx.beginPath(); ctx.arc(left + bw, top, 10, 0, 2 * Math.PI);
        ctx.fillStyle = "#2b6cb0"; ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.fillText(text, left + bw, top, 18);
      }
      if (rows) {
        for (j = 0; j < rows[0].length; j += 1) { token(left - 8 - j * 9, y, scene.types[rows[0][j]], 3.5); }
        if (n - s > rows[0].length) {
          ctx.fillStyle = "#1a202c"; ctx.textAlign = "right";
          ctx.fillText("+" + (n - s - rows[0].length), left - 8 - rows[0].length * 9, y);
        }
        for (j = 0; j < rows[1].length; j += 1) { token(left + 8 + j * 9, top + bh - 7, scene.types[rows[1][j]], 3.5); }
      }
    });

    scene.levels.forEach(function (level, index) {
      var fill = own(art.fills, level.name), block, value = frame.lv[index], share, x, y, bh;
      if (!fill || value === undefined) { return; }
      block = scene.blocks.filter(function (b) { return b.name === fill[0]; })[0];
      if (!block || !(fill[1] > 0)) { return; }
      share = Math.min(1, Math.max(0, value / fill[1]));
      bh = block.h * view.scale;
      x = px(block.x) + block.w * view.scale / 2 + 4;
      y = py(block.y) + bh / 2;
      ctx.fillStyle = "#d8dee8"; ctx.fillRect(x, y - bh, 5, bh);
      ctx.fillStyle = "#2b6cb0"; ctx.fillRect(x, y - bh * share, 5, bh * share);
    });

    frame.tok.forEach(function (entry) { token(px(entry[2]), py(entry[3]), scene.types[entry[1]], 5); });
    frame.veh.forEach(function (entry) {
      var image = ready(own(art.fleets, scene.fleets[entry[0]]));
      ctx.save();
      ctx.translate(px(entry[2]), py(entry[3]));
      ctx.rotate(entry[4] * Math.PI / 180);
      if (image) {
        ctx.drawImage(image, -11, -11, 22, 22);
      } else {
        ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-7, 6); ctx.lineTo(-7, -6); ctx.closePath();
        ctx.fillStyle = own(LAMP, entry[5]) || "#ced4da"; ctx.fill();
        ctx.strokeStyle = "#1a202c"; ctx.stroke();
      }
      ctx.restore();
    });
    frame.ag.forEach(function (entry) {
      ctx.beginPath(); ctx.arc(px(entry[2]), py(entry[3]), 3, 0, 2 * Math.PI);
      ctx.fillStyle = entry[4] >= 0 ? scene.colors[entry[4]] : "#2b6cb0"; ctx.fill();
    });

    /* A move of the step just entered slides from its source to its target. */
    state.slides = state.slides.filter(function (slide) { return wall - slide.start < SLIDE_MS; });
    state.slides.forEach(function (slide) {
      var a = scene.blocks[slide.move[1]], b = scene.blocks[slide.move[2]], f = (wall - slide.start) / SLIDE_MS;
      token(px(a.x + (b.x - a.x) * f), py(a.y + (b.y - a.y) * f), scene.types[slide.move[0]], 5);
    });
  }

  function panel(frame) {
    var scene = run().scene, t = state.t, text;
    text = "t = " + fmt(t) + " of " + fmt(duration());
    if (frame.more) { text += "  (+" + frame.more + " not drawn)"; }
    if (frame.skipped) { text += "  (" + frame.skipped + " moves not animated)"; }
    el("clock").textContent = text;
    if (document.activeElement !== el("seek")) {
      el("seek").value = duration() > 0 ? Math.round(1000 * t / duration()) : 0;
    }
    el("now-t").textContent = fmt(t);
    el("now-wip").textContent = String(frame.kpi[0] - frame.kpi[1]);
    el("now-in").textContent = String(frame.kpi[0]);
    el("now-out").textContent = String(frame.kpi[1]);
    scene.levels.forEach(function (level, index) {
      var value = frame.lv[index], bar = el("lev").children[index];
      if (!bar || value === undefined) { return; }
      bar.children[1].firstChild.style.width = (level.cap > 0 ? Math.min(100, Math.max(0, 100 * value / level.cap)) : 0) + "%";
      bar.children[2].textContent = fmt(value);
    });
    if (state.shown === frame.k) { return; }
    state.shown = frame.k;
    clear(el("res"));
    frame.r.forEach(function (row) {
      var line = add("div", el("res"), undefined, "row");
      add("span", line, undefined, "dot").style.background = own(LAMP, row[1]) || "#8a94a6";
      add("span", line, scene.resources[row[0]], "name");
      add("span", line, row[1] + " " + row[2] + " of " + fmt(row[3]));
    });
  }

  /* What does not move while a run plays: built when the run is chosen. */
  function chosen() {
    var current = run(), scene = current.scene, times = current.t, text;
    state.t = Math.min(state.t, duration());
    state.k = -1; state.shown = -1; state.slides = [];
    el("seed").textContent = current.seed === undefined ? "" :
      "Replication with seed " + current.seed + " (1 of " + current.of + ")";
    clear(el("lev"));
    scene.levels.forEach(function (level) {
      var line = add("div", el("lev"), undefined, "row");
      add("span", line, level.name, "name");
      add("div", add("span", line, undefined, "bar"));
      add("span", line, "");
    });
    clear(el("all"));
    el("allnote").textContent = "";
    if (data.kpis && current.of > 1) {
      el("allnote").textContent = "Mean and 95% interval over " + current.of + " replications";
      text = add("tr", el("all"));
      ["KPI", "Mean", "95% interval", "This run"].forEach(function (head) { add("th", text, head); });
      data.kpis.forEach(function (row) {
        var line;
        if (row[1] !== state.name) { return; }
        line = add("tr", el("all"));
        add("td", line, row[0]);
        add("td", line, fmt(row[2]));
        add("td", line, fmt(row[3]) + " to " + fmt(row[4]));
        add("td", line, fmt(row[5]));
      });
    }
    text = times.length > 1 ? "Sampled every " + fmt(times[1] - times[0]) + " time units (" +
      (times.length - 1) + " steps): between two samples a count is the earlier one, a position or a level is on the line between them." : "Nothing was recorded.";
    if (current.truncated) {
      text += " The recording stopped at its limit of " + current.events + " events: the run went on after t = " + fmt(duration()) + ".";
    }
    el("foot").textContent = text;
  }

  function tick(wall) {
    var current = run(), frame, step;
    if (state.playing && state.wall !== null) {
      state.t = Math.min(duration(), state.t + (wall - state.wall) / 60000 * duration() * state.speed);
      if (state.t >= duration()) { state.playing = false; el("play").textContent = "Play"; }
    }
    state.wall = wall;
    frame = frameAt(current, state.t);
    if (state.playing && state.k >= 0 && frame.k > state.k) {
      for (step = state.k + 1; step <= frame.k; step += 1) {
        (current.frames[step].mv || []).forEach(function (move) {
          if (state.slides.length < MAX_SLIDES) { state.slides.push({move: move, start: wall}); }
        });
      }
    }
    state.k = frame.k;
    draw(frame, wall);
    panel(frame);
    if (state.playing || state.slides.length) {
      window.requestAnimationFrame(tick);
    } else {
      state.busy = false;
      state.wall = null;
    }
  }

  function wake() {
    if (!state.busy) { state.busy = true; window.requestAnimationFrame(tick); }
  }

  document.title = data.title;
  el("title").textContent = data.title;
  data.scenarios.forEach(function (name) { add("option", el("scenario"), name).value = name; });
  el("scenario").hidden = data.scenarios.length < 2;
  el("play").addEventListener("click", function () {
    if (!state.playing && state.t >= duration()) { state.t = 0; state.k = -1; }
    state.playing = !state.playing;
    el("play").textContent = state.playing ? "Pause" : "Play";
    wake();
  });
  function step(dir) {
    state.playing = false;
    el("play").textContent = "Play";
    state.t = stepTime(run().t, state.t, dir);
    state.k = -1; state.slides = [];
    wake();
  }
  el("back").addEventListener("click", function () { step(-1); });
  el("step").addEventListener("click", function () { step(1); });
  el("seek").addEventListener("input", function () {
    state.t = duration() * el("seek").value / 1000;
    state.k = -1; state.slides = [];
    wake();
  });
  el("speed").addEventListener("change", function () { state.speed = Number(el("speed").value); });
  el("scenario").addEventListener("change", function () {
    state.name = el("scenario").value;
    chosen();
    wake();
  });
  window.addEventListener("resize", wake);
  chosen();
  /* A page with one sample, the present, and a panel of its own (the twin's monitor). */
  if (data.monitor) { state.t = duration(); monitor(data.monitor, el, add, clear); }
  wake();
}

if (typeof document !== "undefined") { boot(); }
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'sha256-__HASH__'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run player</title>
<style>__CSS__</style>
__HEAD__</head>
<body>
<h1 id="title"></h1>
<div id="bar">
<button id="play" type="button">Play</button>
<button id="back" type="button" aria-label="Step back">&lt;|</button>
<button id="step" type="button" aria-label="Step">|&gt;</button>
<input id="seek" type="range" min="0" max="1000" value="0" aria-label="Time">
<select id="speed" aria-label="Speed">
<option value="0.25">0.25x</option><option value="0.5">0.5x</option>
<option value="1" selected>1x</option><option value="2">2x</option>
<option value="4">4x</option><option value="8">8x</option>
</select>
<select id="scenario" aria-label="Scenario"></select>
<span id="clock"></span>
</div>
__TOP__<main>
<canvas id="view"></canvas>
<aside>
<h2 id="run-h">This run</h2>
<p class="note" id="seed"></p>
<table>
<tr><td>Time</td><td id="now-t"></td></tr>
<tr><td>In the system</td><td id="now-wip"></td></tr>
<tr><td>Created</td><td id="now-in"></td></tr>
<tr><td>Completed</td><td id="now-out"></td></tr>
</table>
<div id="res"></div>
<div id="lev"></div>
<h2 id="all-h">All replications</h2>
<p class="note" id="allnote"></p>
<table id="all"></table>
</aside>
</main>
__BOTTOM__<p id="foot"></p>
<script type="application/json" id="tape">__TAPE__</script>
<script>__SCRIPT__</script>
</body>
</html>
"""


def _shell(script: str = _SCRIPT, head: str = "", top: str = "", bottom: str = "") -> str:
    """The constant page: everything but the data block. The policy pins the script by
    its sha256, so the browser runs this script and no other. A page built on the player
    (the twin's monitor, `monitor.py`) gives its own constant *script* (the player's with
    a `monitor` function before it) and constant markup for the head and around the view."""
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    return (_PAGE.replace("__CSS__", _CSS).replace("__HASH__", digest).replace("__HEAD__", head)
            .replace("__TOP__", top).replace("__BOTTOM__", bottom).replace("__SCRIPT__", script))


def _plain(value):
    """*value* as JSON can hold it: a number that is not finite becomes null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _embed(data) -> str:
    """*data* as the text of the data block. Pure ASCII, and no `<`, `>`, `&` or `/`
    as itself: no name can close the block, open a tag or spell an address."""
    text = json.dumps(_plain(data), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("/", "\\/"))


_SVG_TAG = re.compile(r"<svg\b[^>]*>")


def _art(pictures: dict | None) -> tuple[list, dict]:
    """The caller's pictures as (the `data:` images, what each one is for).

    An SVG that `check_svg` refuses, or whose root carries no `width` and `height`
    (a browser cannot size it on a canvas), is left out: the generic shape stays."""
    pictures = pictures or {}
    images: list[str] = []
    seen: dict[str, int] = {}

    def image(svg) -> int | None:
        if not isinstance(svg, str) or check_svg(svg):
            return None
        root = _SVG_TAG.search(svg)
        if root is None or not all(re.search(rf"\b{side}\s*=", root.group(0))
                                   for side in ("width", "height")):
            return None
        if svg not in seen:
            seen[svg] = len(images)
            images.append("data:image/svg+xml;base64,"
                          + base64.b64encode(svg.encode("utf-8")).decode("ascii"))
        return seen[svg]

    def one(table) -> dict:
        found = {str(name): image(svg) for name, svg in (table or {}).items()}
        return {name: index for name, index in found.items() if index is not None}

    blocks = {}
    for name, entry in (pictures.get("blocks") or {}).items():
        states = one((entry or {}).get("states"))
        size = (entry or {}).get("size") or ()
        if states and len(size) == 2:
            blocks[str(name)] = {"w": float(size[0]), "h": float(size[1]), "states": states}
    fills = {str(level): [str(block), float(capacity)]
             for level, (block, capacity) in (pictures.get("fills") or {}).items()}
    return images, {"blocks": blocks, "types": one(pictures.get("entities")),
                    "fleets": one(pictures.get("fleets")), "fills": fills}


def _replications(results, experiment, names) -> tuple[dict, list | None]:
    """({scenario: (seed of the recorded replication, replications)}, the KPI rows).

    A row is [KPI, scenario, mean, lower, upper, the recorded replication's value]
    for the `system/` and `user/` KPIs: the mean and 95% interval over all
    replications (`ci95`, the numbers of `ExperimentResult.comparison`) beside the
    one the tape shows. No rows for a scenario with a single replication."""
    from nagasim.core.stats import ci95
    from nagasim.experiments import flatten_kpis

    if experiment is not None:
        frames, runs = experiment.results, experiment.seeds
    elif results is not None:
        # A plain run: `api.run_model` records its smallest seed, and a Run is its seed.
        frames = {names[0]: results}
        runs = {names[0]: sorted({int(run) for run in results["system"]["Run"]})}
    else:
        return {}, None
    seeds = {name: (int(found[0]), len(found)) for name, found in runs.items() if found}
    rows = []
    for name, (seed, count) in seeds.items():
        if count < 2 or name not in frames:
            continue
        flat = flatten_kpis(frames[name])
        flat = flat[flat["KPI"].str.startswith(("system/", "user/"))]
        for kpi, group in flat.groupby("KPI", sort=True):
            interval = ci95(group.sort_values("Run")["Value"].to_numpy(dtype=float))
            own = group.loc[group["Run"] == seed, "Value"]
            rows.append([kpi, name, interval["mean"], interval["lb"], interval["ub"],
                         float(own.iloc[0]) if len(own) else None])
    return seeds, rows or None


def build_player(layout: dict, recordings: dict, *, results=None, experiment=None,
                 title: str | None = None, view: str | None = None, pictures: dict | None = None,
                 samples: int = tape.SAMPLES, path=None) -> str:
    """Return the player page of *recordings*; write it to *path* when given.

    *recordings* is `{scenario name: TraceRecorder or event list}`; a plain run is
    `{BASELINE: recorder}`. A scenario is drawn on the layout with its own overrides
    applied (a scenario may move a node or change a fleet's size). *results* (one
    run's frames) or *experiment* (an `ExperimentResult`) adds the seed of each
    recording and, for more than one replication, the "all replications" table.
    *view* is "schematic" or "layout" (default: `tape.default_view`). *pictures* is
    `{"blocks": {name: {"size": (w, h), "states": {state: svg}}}, "entities":
    {type: svg}, "fleets": {fleet: svg}, "fills": {level: (block, capacity)}}`.

    Over `MAX_BYTES` the tapes are written again with half the *samples*, down to
    `tape.MIN_SAMPLES`; a ValueError says so when even that does not fit.
    """
    from nagasim.experiments import apply_overrides, scenarios_of

    if not recordings:
        raise ValueError("the player needs at least one recorded run")
    view = view or tape.default_view(layout)
    names = list(recordings)
    overrides = {scenario.get("name"): scenario.get("overrides") or []
                 for scenario in scenarios_of(layout)}
    seeds, rows = _replications(results, experiment, names)
    images, art = _art(pictures)
    shell = _shell()
    samples = max(int(samples), 1)
    while True:
        runs = {}
        for name, trace in recordings.items():
            changed = overrides.get(name)
            run = tape.TapeWriter(apply_overrides(layout, changed) if changed else layout,
                                  view).run(trace, samples)
            if name in seeds:
                run["seed"], run["of"] = seeds[name]
            runs[name] = run
        data = {"title": title or "Run", "scenarios": names, "runs": runs, "kpis": rows,
                "pics": images, "art": art}
        page = shell.replace("__TAPE__", _embed(data))
        if len(page) <= MAX_BYTES:
            break
        if samples <= tape.MIN_SAMPLES:
            raise ValueError(f"the player file is {len(page)} bytes at {samples} samples, over "
                             f"the {MAX_BYTES} byte limit and cannot take fewer samples: use a smaller "
                             "model, fewer scenarios or no pictures")
        samples = max(samples // 2, tape.MIN_SAMPLES)
    if path is not None:
        # Bytes, so the file is the same on every system and every build.
        Path(path).write_bytes(page.encode("ascii"))
    return page
