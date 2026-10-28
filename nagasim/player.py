"""The shareable HTML player (PHASE7 7.7, D196-D201): one self-contained file that
replays a recorded run in a browser, with no server and no editor.

    build_player(layout, {BASELINE: recorder}, results=results, title="Line", path="run.html")

The file leaves its author's machine, so what goes into it is decided here and nowhere
else (D199):

* It is a CONSTANT page (`_PAGE`, `CSS`, `SCRIPT`) plus one JSON data block. Text from
  a model (names, labels, types, statistics) is only ever in that block, escaped so that
  nothing in it can end the block, and the script puts it on the page as canvas text or
  `textContent`: never as markup, never as code.
* The data block holds the tape of each run (`core/tape.py`: block names, labels and
  places, the name tables, the sampled state), the title, the seed and replication
  count, and the `system/` and `user/` KPI rows. Nothing else of the model or of the
  machine: no parameter, expression, data source, binding, author or path, and no
  description except as the label of a block still named `submodel_N` (as the canvas shows it).
* The file names no address. A Content-Security-Policy with `default-src 'none'` makes
  the browser refuse every request, and pins the one script and the one style sheet by
  their hashes.
* Pictures are the caller's SVG text, each passed through `core/svgsafe.check_svg` and
  embedded as a `data:` image; the engine reads no picture from a model (D198).
* The look is the caller's *tokens* (D697, D703): a light and a dark set of colours, a
  font stack and a corner radius as plain data, each checked against a pattern
  (`check_tokens`), written into the one style sheet as CSS variables that the script's
  drawing reads back. The browser's light or dark setting picks the set, and a switch on
  the page overrides it (kept per browser by a second pinned script in the head). The
  product passes its theme's (`product/ui/theme.page_tokens`); the default is the
  console palette.
* The page is a console (D704): the line, "what happened when" (one lane per resource
  from the tape, D708) and the bottleneck's queue in the centre; the results (the
  model's dashboard results, `dashboard.resolve_dashboard`, each scenario's 95%
  interval against the baseline's with an arrow and a word) and "can I trust this?"
  (the law checks, warm-up and precision) at the side. A state or a verdict is never
  colour alone (D705).
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
from nagasim.core.timeunits import UNITS, units_of

__all__ = ["MAX_BYTES", "DEFAULT_TOKENS", "build_player",
           # the pieces the viewer and the monitor build their pages from
           "CSS", "PANELS", "SCRIPT", "THEME", "check_tokens", "csp_hash", "embed", "page_shell",
           "plain", "result_rows", "style_sheet", "trust_card"]

# An email attachment. Over it the tape is written again with half the samples, down to
# `tape.MIN_SAMPLES`; a run that still does not fit is refused.
MAX_BYTES = 10_000_000

# The console palette (D703, validated with the dataviz checks; the spec's table), light
# and slate-blue dark. page/panel/raised are surfaces by depth, line the rules; ink, muted
# and dim text; accent/accent_ink what is clickable and the text on it; yours/base/third
# the series (a scenario, the baseline, a third); run/blocked/down/idle the states, quiet
# when normal and loud when not, always with a word on the page. source/work/sink/block
# fill a drawn block's band by its place in the flow: Studio Light's, the editor's
# default theme (`product/ui/theme.page_tokens` gives these; a product test holds it).
# The font and the corner radius are the light set's alone: a theme does not change them.
_FILLS = {"source": "#7DD08A", "work": "#7EABF7", "sink": "#F28B82", "block": "#D5CFC3"}
DEFAULT_TOKENS = {
    "light": {"page": "#f4f6fa", "panel": "#ffffff", "raised": "#eef1f6", "line": "#dde2ea",
              "ink": "#1a1f2b", "muted": "#586377", "dim": "#6b7588", "accent": "#2f6fe0",
              "accent_ink": "#ffffff", "yours": "#2f6fe0", "base": "#c56a12", "third": "#0e8c7e",
              "run": "#0a8a78", "blocked": "#c99400", "down": "#d23f4a", "idle": "#6b7588",
              **_FILLS,
              "font": '"Segoe UI", -apple-system, Roboto, Helvetica, Arial, sans-serif',
              "radius": "6"},
    "dark": {"page": "#151b26", "panel": "#1c2331", "raised": "#242d3e", "line": "#313c51",
             "ink": "#e6ebf3", "muted": "#9aa6b8", "dim": "#76839a", "accent": "#2f6fe0",
             "accent_ink": "#ffffff", "yours": "#4a8af4", "base": "#d47a24", "third": "#16a08f",
             "run": "#2fa58f", "blocked": "#d39a2e", "down": "#ef5f57", "idle": "#76839a",
             **_FILLS},
}
_MODES = ("light", "dark")
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
# A font stack: names, each bare or wholly in one kind of quote, between commas (#836).
# Nothing that could end a rule or the style element.
_FONT_NAME = r"""(?:"[A-Za-z0-9 -]+"|'[A-Za-z0-9 -]+'|[A-Za-z0-9 -]+)"""
_FONT = re.compile(rf"{_FONT_NAME}(?: *, *{_FONT_NAME})*")
_RADIUS = re.compile(r"[0-9]{1,2}")


def check_tokens(tokens: dict | None) -> dict:
    """{"light": {...}, "dark": {...}}: *tokens* over `DEFAULT_TOKENS`, each checked, as
    they are written into the page's style sheet: a value that is not a colour, a font
    stack or a radius is refused. *tokens* is that shape, or one flat set (the light
    look, as before D703) worn over the default dark one."""
    tokens = tokens or {}
    if not (set(tokens) <= set(_MODES) and all(isinstance(v, dict) for v in tokens.values())):
        tokens = {"light": tokens}
    out = {}
    for mode in _MODES:
        merged = {**DEFAULT_TOKENS[mode], **(tokens.get(mode) or {})}
        unknown = sorted(set(merged) - set(DEFAULT_TOKENS[mode]))
        if unknown:
            raise ValueError(f"unknown page tokens: {', '.join(map(str, unknown))[:200]}")
        for key, value in merged.items():
            pattern = _FONT if key == "font" else _RADIUS if key == "radius" else _COLOR
            if not isinstance(value, str) or len(value) > 200 or not pattern.fullmatch(value):
                raise ValueError(f"page token {key} is not a valid value: {str(value)[:40]!r}")
        out[mode] = merged
    return out


def style_sheet(tokens: dict | None = None, rules: str | None = None) -> str:
    """The one style sheet of a page: the *tokens* as CSS variables (light on `:root`; dark
    when the browser prefers it and the page's switch does not say light, or when the
    switch says dark: D703), the rules the player and the viewer share (the console),
    then the page's own *rules* (default: the player's)."""
    checked = check_tokens(tokens)

    def names(mode):
        return "".join(f" --{key.replace('_', '-')}: {value}{'px' if key == 'radius' else ''};"
                       for key, value in checked[mode].items()) + f" color-scheme: {mode};"
    dark = names("dark")
    return (":root {" + names("light") + " }\n"
            "@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) {" + dark + " } }\n"
            ":root[data-theme=dark] {" + dark + " }\n" + _SHARED + (CSS if rules is None else rules))


# The look both pages share (D697, D704): page, type, tables and the console: panels in a grid of levers | centre | side (`.console.levers`; the player has
# no levers), one column below 900 px; the results rows and the trust card.
_SHARED = """* { box-sizing: border-box; }
body { font-family: var(--font); font-size: 14px; color: var(--ink); margin: 0; padding: 12px 16px;
       background: var(--page); }
h1 { font-size: 17px; margin: 0; overflow-wrap: anywhere; }
h2 { font-size: 13px; margin: 0; font-weight: 600; }
button, select, input { font: inherit; color: inherit; }
button, select { padding: 4px 10px; border: 1px solid var(--line); border-radius: var(--radius);
                 background: var(--raised); cursor: pointer; }
button:hover { border-color: var(--accent); }
button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
button:disabled { opacity: 0.5; cursor: default; }
.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 3px 6px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
.note { color: var(--muted); margin: 2px 0; }
summary { cursor: pointer; }
summary h2 { display: inline; }
/* Nothing moves when a value changes length (bug 841): fixed slots, an active tab no wider. */
#theme { min-width: 9.5em; }
.scroll { overflow-x: auto; max-width: 100%; }
.console { display: grid; grid-template-columns: minmax(0, 1fr) minmax(260px, 340px); gap: 12px;
           align-items: start; }
.console.levers { grid-template-columns: minmax(220px, 280px) minmax(0, 1fr) minmax(260px, 340px); }
.centre, .side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
         padding: 10px 12px; min-width: 0; }
.panel-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
              gap: 4px 12px; margin-bottom: 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 2px 10px; color: var(--muted); font-size: 12px; }
.key::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 4px;
               border-radius: 50%; background: var(--idle); }
.key.run::before { background: var(--run); }
.key.down::before { background: var(--down); }
.key.blocked::before { background: var(--blocked); }
.key.yours::before { background: var(--yours); }
.key.base::before { background: var(--base); }
.key.target::before { width: 0; height: 10px; border-radius: 0; background: none;
                      border-left: 2px dotted var(--ink); }
.result { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(56px, 1fr) auto; gap: 8px;
          align-items: center; padding: 6px 0; border-bottom: 1px solid var(--line); }
.result:last-child { border-bottom: 0; }
.r-label { display: block; overflow-wrap: anywhere; }
.r-unit { display: block; color: var(--muted); font-size: 11px; }
.r-track { position: relative; height: 20px; }
.r-range { position: absolute; height: 4px; border-radius: 2px; opacity: 0.5; }
.r-dot { position: absolute; width: 9px; height: 9px; margin-left: -4px; border-radius: 50%; }
.r-range.yours { top: 4px; background: var(--yours); }
.r-dot.yours { top: 2px; background: var(--yours); }
.r-range.base { top: 14px; background: var(--base); }
.r-dot.base { top: 12px; background: var(--base); }
.r-target { position: absolute; top: 0; bottom: 0; border-left: 2px dotted var(--ink); }
.r-num { text-align: right; font-variant-numeric: tabular-nums; }
.r-value { display: block; font-weight: 600; font-size: 15px; }
.r-delta { display: block; height: 1.3em; line-height: 1.3em; font-size: 11px; color: var(--muted); white-space: nowrap; }
.r-arrow { font-weight: 700; }
.r-arrow.better { color: var(--run); }
.r-arrow.worse { color: var(--down); }
.trust td { border: 0; padding: 3px 0; }
.trust td:last-child { font-weight: 600; overflow-wrap: anywhere; }
.mark-ok { color: var(--run); }
.mark-warn { color: var(--blocked); }
.topbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 8px 12px;
          background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
.ident { flex: 1 1 200px; min-width: 0; }
.tabs { display: flex; flex-wrap: wrap; gap: 4px; }
.tab { background: transparent; border-color: transparent; }
.tab.on { background: var(--raised); border-color: var(--line); box-shadow: inset 0 -2px 0 var(--accent); }
.tab::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 6px;
               border-radius: 50%; background: var(--yours); }
.tab.base::before { background: var(--base); }
.toggle { color: var(--muted); font-size: 12px; cursor: pointer; }
@media (max-width: 900px) { .console, .console.levers { grid-template-columns: minmax(0, 1fr); } }
"""

CSS = """.transport { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
#clock { display: inline-block; text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
#play { min-width: 5.5em; }
#seek { display: block; width: 100%; margin: 8px 0 12px; accent-color: var(--accent); }
#view { display: block; width: 100%; height: 40vh; }
#lanes { display: block; width: 100%; height: 60px; cursor: ew-resize; touch-action: none; }
.callout { margin: 8px 0 0; padding: 6px 10px; background: var(--raised); border-left: 3px solid var(--down);
           border-radius: var(--radius); }
.callout-head { font-weight: 600; margin-right: 6px; }
.side { font-size: 13px; }
.row { display: flex; gap: 6px; align-items: center; margin: 2px 0; }
.row > span:last-child { min-width: 9ch; text-align: right; font-variant-numeric: tabular-nums; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar { flex: 0 0 80px; height: 8px; background: var(--raised); }
.bar div { height: 100%; background: var(--third); }
summary { font-weight: 600; }
#foot { margin-top: 6px; }
/* In the viewer's frame (`class="framed"` on the root): the line and the lanes only; the
   viewer draws the answer, the results and the trust card itself. */
.framed body { padding: 0; background: var(--panel); }
.framed .ident, .framed #theme, .framed .side { display: none; }
.framed .console { grid-template-columns: minmax(0, 1fr); }
.framed .panel { border: 0; padding: 4px 0; }
"""

# Runs in the head, before the page is drawn, so a reader's choice of light or dark
# shows from the first frame; storage that throws (a sandboxed frame, a locked-down
# browser) leaves the browser's own setting (D703).
THEME = """try { var chosen = window.localStorage.getItem("naga-theme");
  if (chosen === "light" || chosen === "dark") { document.documentElement.setAttribute("data-theme", chosen); }
} catch (e) { /* no storage here: the browser's setting stands */ }"""

# The results panel, the trust card (D704) and the theme switch (D703): the player's and
# the viewer's alike, so one text in both scripts. It needs `fmt` of the script it is put in.
PANELS = """/* Signs beside the words; the file is ASCII and the script holds no escape. */
var SIGN = {up: String.fromCharCode(0x25b2), down: String.fromCharCode(0x25bc), warn: String.fromCharCode(0x26a0),
            tick: String.fromCharCode(0x2713), plusMinus: String.fromCharCode(0xb1)};
/* A percentage with one decimal and its sign. */
function percent(value) { return (value > 0 ? "+" : "") + (Math.round(value * 10) / 10) + "%"; }

/* The results panel (Goal 6): per row, yours and the baseline as 95% interval bars on
   one scale, the target as a dotted line, the value, and the change as an arrow and a
   word. A row is {kpi, label, unit, yours: {mean, lo, hi}, base, target, better,
   delta_pct, verdict}; `add` makes an element (text only ever as textContent). Returns
   {base, target}: whether any row drew one, so the legend names only what is drawn. */
function resultsPanel(rows, parent, add) {
  var seen = {base: false, target: false};
  rows.forEach(function (row) {
    var line = add("div", parent, undefined, "result"), name = add("div", line);
    var track = add("div", line, undefined, "r-track"), num = add("div", line, undefined, "r-num");
    var lo = Infinity, hi = -Infinity, span, change, delta, has = function (v) { return v !== null && v !== undefined; };
    add("span", name, row.label, "r-label");
    if (row.unit) { add("span", name, row.unit, "r-unit"); }
    [row.yours, row.base].forEach(function (side) {
      if (!side) { return; }
      [side.mean, side.lo, side.hi].forEach(function (v) { if (has(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    });
    if (has(row.target)) { lo = Math.min(lo, row.target); hi = Math.max(hi, row.target); }
    span = hi - lo;
    if (!(span > 0)) { span = Math.abs(hi) || 1; lo -= span / 2; } else { lo -= span * 0.08; span *= 1.16; }
    function at(v) { return Math.min(100, Math.max(0, 100 * (v - lo) / span)) + "%"; }
    function mark(side, look) {
      if (!side || !has(side.mean)) { return; }
      if (has(side.lo) && has(side.hi)) {
        var bar = add("div", track, undefined, "r-range " + look);
        bar.style.left = at(side.lo);
        bar.style.width = Math.min(100, 100 * (side.hi - side.lo) / span) + "%";
      }
      add("div", track, undefined, "r-dot " + look).style.left = at(side.mean);
    }
    mark(row.base, "base");
    mark(row.yours, "yours");
    if (has(row.target)) { add("div", track, undefined, "r-target").style.left = at(row.target); seen.target = true; }
    if (row.base && has(row.base.mean)) { seen.base = true; }
    track.title = "yours " + fmt(row.yours.mean) + (has(row.yours.lo) ? " (" + fmt(row.yours.lo) + " to " + fmt(row.yours.hi) + ")" : "") +
      (row.base ? ", baseline " + fmt(row.base.mean) : "") + (has(row.target) ? ", target " + fmt(row.target) : "");
    add("span", num, fmt(row.yours.mean), "r-value");
    delta = add("span", num, undefined, "r-delta");     /* its line kept when empty: no shift */
    if (row.verdict) {
      change = row.base && has(row.base.mean) ? row.yours.mean - row.base.mean : 0;
      add("span", delta, row.verdict === "no clear change" ? "~" : change > 0 ? SIGN.up : SIGN.down,
          "r-arrow " + row.verdict.replace(/ /g, "-"));
      add("span", delta, " " + (has(row.delta_pct) ? percent(row.delta_pct) + " " : "") + row.verdict);
    }
  });
  return seen;
}

/* The trust card (Goal 8): {laws_pass, laws_warn, warmup, replications, half_width_pct}
   as label and word rows; *unit* is the model's time unit. */
function trustPanel(trust, parent, add, unit) {
  var laws = trust.laws_warn ? [SIGN.warn + " ", trust.laws_warn + (trust.laws_warn === 1 ? " warning" : " warnings"), "mark-warn"] :
    trust.laws_pass ? [SIGN.tick + " ", "pass", "mark-ok"] : ["", "none to check", ""];
  [["Flow and Little's law checks", laws],
   ["Warm-up removed", ["", trust.warmup > 0 ? fmt(trust.warmup) + " " + unit : "none", ""]],
   ["Replications, precision", ["", trust.replications + (trust.half_width_pct !== null && trust.half_width_pct !== undefined ?
     ", " + SIGN.plusMinus + (Math.round(trust.half_width_pct * 10) / 10) + "%" : ""), ""]],
   /* Three uncertainties, three rows with their own basis; never summed (D739). Old data
      without the texts reads as not measured. */
   ["Input data", ["", typeof trust.input_text === "string" ? trust.input_text : "not measured", ""]],
   ["Parameters", ["", typeof trust.params_text === "string" ? trust.params_text : "not measured", ""]]].forEach(function (pair) {
    var line = add("tr", parent), cell;
    add("td", line, pair[0]);
    cell = add("td", line);
    if (pair[1][0]) { add("span", cell, pair[1][0], pair[1][2]); }
    add("span", cell, pair[1][1]);
  });
}

/* The theme switch (D703) on *button*: the browser's setting, light, or dark; kept per
   browser when storage works, and the page still switches when it throws. *onChange*
   redraws what the page draws itself. */
function themeSwitch(button, onChange) {
  var THEMES = ["auto", "light", "dark"], root = document.documentElement;
  function label() { button.textContent = "Theme: " + (root.dataset.theme || "auto"); }
  button.addEventListener("click", function () {
    var next = THEMES[(THEMES.indexOf(root.dataset.theme || "auto") + 1) % THEMES.length];
    try {
      if (next === "auto") { window.localStorage.removeItem("naga-theme"); } else { window.localStorage.setItem("naga-theme", next); }
    } catch (e) { /* no storage: the choice lasts until the page is closed */ }
    if (next === "auto") { delete root.dataset.theme; } else { root.dataset.theme = next; }
    label();
    onChange();
  });
  label();
}

"""

SCRIPT = """"use strict";
/* The drawer of a run player. It draws what the tape says and works nothing out
   itself: a frame's numbers ARE the state. Text from the model reaches the page only
   as canvas text or textContent. */

/* A lamp's state as one of four looks (D705): running is quiet, idle neutral, and only
   down and blocked are loud, each with a word. */
var KIND = {busy: "run", loaded: "run", handling: "run", blocked: "blocked", down: "down"};
var WORD = {blocked: "BLOCKED", down: "DOWN"};
var MAX_LANES = 8;
/* The waiting entities drawn one by one before a block; the queue badge says how many in all. */
var FEW = 6;
/* On a floor plan, people and fleets are drawn this many times their size on the schematic (D967,
   as the video's film look does, D956): at the plan's scale a person is a few pixels. */
var PEOPLE = 2;
var SLIDE_MS = 250;
var MAX_SLIDES = 200;
/* The speeds (D711): model seconds per wall second, so 1x is real time in any time unit. */
var SPEEDS = [1, 10, 60, 300, 600, 1800, 3600, 7200, 21600, 86400, 604800, 2592000, 31536000];
var PER = [[31536000, "year"], [2592000, "month"], [604800, "week"], [86400, "day"], [3600, "h"], [60, "min"], [1, "s"]];

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

/* A speed as its plant time per second: "60x (1 min/s)", in the largest unit it fills. */
function speedLabel(speed) {
  var i = 0;
  while (i < PER.length - 1 && speed % PER[i][0]) { i += 1; }
  var per = speed / PER[i][0] + " " + PER[i][1] + "/s";
  return speed > 3600 ? per : speed + "x (" + per + ")";
}

/* The slowest speed that plays a run of *seconds* in two minutes at most. */
function defaultSpeed(seconds) {
  var i = 0;
  while (i < SPEEDS.length - 1 && seconds / SPEEDS[i] > 120) { i += 1; }
  return SPEEDS[i];
}

/* How long a move slides, in wall ms: the editor's rule (0.25 s at one model unit a
   second, shorter when faster, never under 50 ms). A move takes no model time: the
   slide only shows where the part went. */
function slideMs(speed, unitSeconds) { return Math.max(50, SLIDE_MS / Math.max(speed / unitSeconds, 1)); }

/* The moves of the tape that happened in (from, to] (#851): the moves of step k are
   those of (t[k-1], t[k]], each with its own time. */
function movesBetween(run, from, to) {
  var times = run.t, out = [], k;
  for (k = Math.max(1, sampleIndex(times, from)); to > from && k < times.length && times[k - 1] < to; k += 1) {
    (run.frames[k].mv || []).forEach(function (move) {
      if (move[3] > from && move[3] <= to) { out.push(move); }
    });
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
function fit(box, w, h, pad) {
  pad = pad === undefined ? 36 : pad;
  var bw = Math.max(box[2] - box[0], 1e-6), bh = Math.max(box[3] - box[1], 1e-6);
  var scale = Math.min((w - 2 * pad) / bw, (h - 2 * pad) / bh);
  return {scale: scale, x: w / 2 - (box[0] + box[2]) / 2 * scale,
          y: h / 2 - (box[1] + box[3]) / 2 * scale};
}

function fmt(value) {
  if (value === null || value === undefined) { return "-"; }
  return String(Math.round(value * 1000) / 1000);
}

/* The smallest centre-to-centre distance between two blocks on one row (same y), in
   scene units; null when no row holds two. A label is never wider than this (#846). */
function pitch(blocks) {
  var rows = {}, best = null, y, xs, i;
  blocks.forEach(function (block) { (rows[block.y] = rows[block.y] || []).push(block.x); });
  for (y in rows) {
    xs = rows[y].sort(function (a, b) { return a - b; });
    for (i = 1; i < xs.length; i += 1) {
      if (xs[i] > xs[i - 1] && (best === null || xs[i] - xs[i - 1] < best)) { best = xs[i] - xs[i - 1]; }
    }
  }
  return best;
}

/* A resource or level of an asset ("station_1/Mfg Machine") by its block alone; the
   page puts the full name on hover. */
function short(name) {
  var cut = String(name).indexOf("/");
  return cut > 0 ? String(name).slice(0, cut) : String(name);
}

/* A time on the clock, with as many decimals as a run of length *end* needs and always
   that many, so the clock keeps its width (#841). */
function clockTime(t, end) { return t.toFixed(end >= 100 ? 0 : end >= 10 ? 1 : 2); }

/* Per block, for a buffer (`buf`), its nearest blocks on its row that are not buffers,
   [left, right] (-1 for none): the tray is drawn in the room between them (#842).
   null for any other block. */
function trays(blocks) {
  return blocks.map(function (block) {
    var left = -1, right = -1;
    if (!block.buf) { return null; }
    blocks.forEach(function (other, j) {
      if (other.buf || other.y !== block.y) { return; }
      if (other.x < block.x && (left < 0 || other.x > blocks[left].x)) { left = j; }
      if (other.x > block.x && (right < 0 || other.x < blocks[right].x)) { right = j; }
    });
    return [left, right];
  });
}

/* A tray of at most *width* in the free span lo..hi (the gaps to its neighbours already
   taken off), centred on *x* when it fits and moved or shrunk to stay inside: [centre,
   width], or null when under 8 px are left (#842). */
function fitTray(x, lo, hi, width) {
  var w = Math.min(width, hi - lo);
  return w < 8 ? null : [Math.min(Math.max(x, lo + w / 2), hi - w / 2), w];
}

/* The spacing of a tray's *places* in *room* px: 5 px, closer when the tray is narrow,
   and null under 3 px, where the tray shows its count instead. */
function trayPitch(places, room) {
  var pitch = Math.min(5, room / Math.max(places, 1));
  return pitch >= 3 ? pitch : null;
}

/* *text* cut to *width* as *measure* (text to width) has it, ending "..." when cut: a
   label is shortened, never squeezed into glyphs that run together. */
function clip(text, width, measure) {
  var cut = text.length;
  if (measure(text) <= width) { return text; }
  while (cut > 0 && measure(text.slice(0, cut) + "...") > width) { cut -= 1; }
  return cut > 0 ? text.slice(0, cut) + "..." : "";
}

/* The place of each block in the flow, from the links alone (the tape holds no block
   type): nothing comes in, a source; nothing goes out, a sink; one that queues, work. */
function roles(blocks, links) {
  var ins = {}, outs = {};
  links.forEach(function (link) { outs[link[0]] = true; ins[link[1]] = true; });
  return blocks.map(function (block, index) {
    if (!ins[index] && outs[index]) { return "source"; }
    if (ins[index] && !outs[index]) { return "sink"; }
    return block.q ? "work" : "block";
  });
}

function kindOf(lamp) { return own(KIND, lamp) || (lamp ? "idle" : ""); }

/* Per block, its working share of the time up to each sample (the time its lamp said
   running over the time so far), shown inside the block; null for a block with no lamp. */
function utilisation(run) {
  var times = run.t, count = run.scene.blocks.length, busy = [], lit = [], out = [], i, k, lamps;
  for (i = 0; i < count; i += 1) { busy.push(0); lit.push(false); out.push([0]); }
  for (k = 1; k < times.length; k += 1) {
    lamps = run.frames[k - 1].st || [];
    for (i = 0; i < count; i += 1) {
      if (lamps[i]) { lit[i] = true; }
      if (kindOf(lamps[i]) === "run") { busy[i] += times[k] - times[k - 1]; }
      out[i].push(times[k] > 0 ? busy[i] / times[k] : 0);
    }
  }
  return out.map(function (shares, index) { return lit[index] ? shares : null; });
}

/* One lane per resource of the tape (D708): its state over time as [from, to, kind]
   spans, the next span starting where the last ended, and the start of each failure.
   A frame without a row for a resource leaves its state as it was (#840). The
   bottleneck's resource (`run.neck.res`) is moved up into the lanes shown. */
function lanes(run) {
  var times = run.t, res = run.neck ? run.neck.res : undefined, out = run.scene.resources.map(function (name) {
    return {name: name, spans: [], fails: []};
  });
  run.frames.forEach(function (frame, k) {
    var from = times[k], to = k + 1 < times.length ? times[k + 1] : times[k], seen = {};
    (frame.r || []).forEach(function (row) {
      var lane = out[row[0]], kind = kindOf(row[1]), last;
      if (!lane) { return; }
      seen[row[0]] = true;
      last = lane.spans[lane.spans.length - 1];
      if (last && last[2] === kind && last[1] === from) { last[1] = to; return; }
      if (kind === "down") { lane.fails.push(from); }
      lane.spans.push([from, to, kind]);
    });
    out.forEach(function (lane, i) {
      var last = lane.spans[lane.spans.length - 1];
      if (!seen[i] && last && last[1] === from) { last[1] = to; }
    });
  });
  if (typeof res === "number" && res >= MAX_LANES && res < out.length) {
    out.splice(MAX_LANES - 1, 0, out.splice(res, 1)[0]);
  }
  return out;
}

""" + PANELS + """/* ---- the page half: runs only where there is a document ---- */

function boot() {
  var data = JSON.parse(document.getElementById("tape").textContent);
  var el = function (id) { return document.getElementById(id); };
  var canvas = el("view"), ctx = canvas.getContext("2d");
  var strip = el("lanes"), pen = strip.getContext("2d");
  var art = data.art || {blocks: {}, types: {}, fleets: {}, fills: {}};
  var unit = data.unit || "time units", unitS = data.unit_s > 0 ? data.unit_s : 1;
  var pics = (data.pics || []).map(function (source) {
    var image = new Image();
    image.onload = function () { wake(); };
    image.src = source;
    return image;
  });
  var state = {name: own(data.runs, data.start) !== undefined ? data.start : data.scenarios[0], t: 0, playing: false, speed: 1, wall: null,
               slides: [], busy: false, shown: -1, roles: [], lanes: [], queues: [], seeking: false,
               use: [], pitch: null, trays: [], stock: [], fed: {}, overlay: false};
  /* The page's tokens, as the style sheet holds them for the theme shown: the drawing
     wears the page's look, and reads it again when the theme changes. */
  var INK, MUTED, LINE, RAISED, PANEL, FONT, RADIUS, FILL, STATE, YOURS, BASE, THIRD;
  function readLook() {
    var look = window.getComputedStyle(document.documentElement);
    function tok(name) { return look.getPropertyValue("--" + name).trim(); }
    INK = tok("ink"); MUTED = tok("muted"); LINE = tok("line"); RAISED = tok("raised"); PANEL = tok("panel");
    FONT = tok("font"); RADIUS = parseFloat(tok("radius")) || 0; YOURS = tok("yours"); BASE = tok("base"); THIRD = tok("third");
    FILL = {source: tok("source"), work: tok("work"), sink: tok("sink"), block: tok("block")};
    STATE = {run: tok("run"), blocked: tok("blocked"), down: tok("down"), idle: tok("idle")};
  }
  readLook();
  var root = document.documentElement, framed = !!(root.classList && root.classList.contains("framed"));
  /* On a floor plan the look is the plan, the people and the fleets (D967): the network, links,
     block boxes, tags and counters are the model's wiring, brought back by "Show model blocks". */
  function clean() { return !!art.floor && !state.overlay; }

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
  /* Ink that reads on a filled colour: dark on a light one, white on a dark one. */
  function onColour(hex) {
    var n = parseInt(String(hex).slice(1), 16);
    if (isNaN(n)) { return "#ffffff"; }
    return 0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255) > 150 ? "#1a1f2b" : "#ffffff";
  }
  function sized(node) {
    var ratio = window.devicePixelRatio || 1, w = node.clientWidth, h = node.clientHeight, c = node.getContext("2d");
    if (node.width !== Math.round(w * ratio) || node.height !== Math.round(h * ratio)) {
      node.width = Math.round(w * ratio);
      node.height = Math.round(h * ratio);
    }
    c.setTransform(ratio, 0, 0, ratio, 0, 0);
    c.clearRect(0, 0, w, h);
    return {w: w, h: h};
  }
  function pill(text, x, y, fill) {
    var w = ctx.measureText(text).width + 10;
    ctx.beginPath();
    if (ctx.roundRect) { ctx.roundRect(x - w / 2, y - 8, w, 16, 8); } else { ctx.rect(x - w / 2, y - 8, w, 16); }
    ctx.fillStyle = fill; ctx.fill();
    ctx.fillStyle = onColour(fill);
    ctx.textAlign = "center";
    ctx.fillText(text, x, y, w);
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
    ctx.fillStyle = type ? type.color : INK;
    ctx.fill();
    ctx.strokeStyle = INK;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  /* How long block *index* has been down at sample k: back to the sample it went down.
     ponytail: a scan per down block per frame, fine for a tape's 2000 samples; keep a
     per-run table if a tape ever grows much longer. */
  function downFor(index, k) {
    var frames = run().frames, j = k;
    while (j > 0 && (frames[j - 1].st || [])[index] === "down") { j -= 1; }
    return state.t - run().t[j];
  }

  /* The scene's box, grown to hold the floor plan: the whole plan shows, not only the part with blocks. */
  function boxOf(scene) {
    var f = art.floor, b = scene.box;
    return f ? [Math.min(b[0], f.x), Math.min(b[1], f.y), Math.max(b[2], f.x + f.w), Math.max(b[3], f.y + f.h)] : b;
  }
  function draw(frame, wall) {
    var scene = run().scene, box = boxOf(scene), size, w, h, view, j;
    /* As tall as the scene needs at this width (a long thin line gets a short panel). */
    var gap = art.floor ? 8 : 36;     /* a plan runs nearly to the panel's edges */
    var tall = (canvas.clientWidth - 2 * gap) * (box[3] - box[1]) / Math.max(box[2] - box[0], 1e-6) + (art.floor ? 2 * gap : 110);
    /* A floor plan keeps its aspect at the panel's width (D968); in the viewer's frame the page
       outside sizes the frame to it, elsewhere it stops at most of the window. */
    var want = Math.round(Math.max(160, art.floor ? Math.min(framed ? 1e6 : (window.innerHeight || 800) * 0.9, tall)
      : Math.min((window.innerHeight || 800) * 0.6, tall)));
    if (canvas.clientHeight !== want) { canvas.style.height = want + "px"; }
    size = sized(canvas); w = size.w; h = size.h;
    if (!(w > 80 && h > 80)) { return; }        /* not laid out yet: a fit would turn the scene inside out */
    view = fit(box, w, h, gap);
    function px(x) { return view.x + x * view.scale; }
    function py(y) { return view.y + y * view.scale; }
    var plan = art.floor ? ready(art.floor.i) : null, people = clean() ? PEOPLE : 1;
    if (plan) {
      ctx.globalAlpha = art.floor.a;
      ctx.drawImage(plan, px(art.floor.x), py(art.floor.y), art.floor.w * view.scale, art.floor.h * view.scale);
      ctx.globalAlpha = 1;
    }
    /* A label or chip line is no wider than the gap to the next block's centre (#846). */
    var room = state.pitch === null ? Infinity : state.pitch * view.scale - 6;
    function measure(text) { return ctx.measureText(text).width; }
    /* A block's size on the canvas: its picture's, else the tape's box. */
    function sizeOf(b) {
      var p = own(art.blocks, b.name);
      return [(p ? p.w : b.w) * view.scale, (p ? p.h : b.h) * view.scale];
    }
    /* A buffer (#842): a tray at most half a block wide in the room between the blocks
       either side of it, a gap from each, never on top of them (it shrinks first); FULL
       when full and an arrow towards the block it feeds. Inside, its places with the
       parts that stand in them, head first from its right end (#851), when the parts are
       exactly its count and the places fit; else the count and its fill as a bar. The
       parts of a tray that counts a level are the ones waiting at the block it feeds
       (they hold its places), else its own. Returns the tray's centre x, or null when
       there is no room to draw it. */
    function tray(block, index, n) {
      var sides = state.trays[index], size = sizeOf(block), x = px(block.x), y = py(block.y);
      var a = scene.blocks[sides[0]], b = scene.blocks[sides[1]];
      var lo = a ? px(a.x) + sizeOf(a)[0] / 2 + 8 : x - size[0] / 2;
      var hi = b ? px(b.x) - sizeOf(b)[0] / 2 - 8 : x + size[0] / 2;
      var box = fitTray(x, lo, hi, size[0] / 2), h = Math.min(size[1] / 2, 26), cx, w, left, full, i, pitch;
      var rows = own(frame.q, String(state.stock[index] >= 0 ? sides[1] : index)) || [[], []];
      var parts = state.stock[index] >= 0 ? rows[0] : rows[0].concat(rows[1]), places;
      if (!box) { return null; }
      cx = box[0]; w = box[1]; left = cx - w / 2;
      full = block.cap > 0 && n >= block.cap;
      ctx.beginPath();
      if (ctx.roundRect) { ctx.roundRect(left, y - h / 2, w, h, Math.min(RADIUS, h / 2)); } else { ctx.rect(left, y - h / 2, w, h); }
      ctx.fillStyle = RAISED; ctx.fill();
      ctx.strokeStyle = full ? STATE.blocked : MUTED; ctx.lineWidth = full ? 2 : 1; ctx.stroke();
      ctx.lineWidth = 1;
      places = block.cap > 0 ? block.cap : n;
      pitch = trayPitch(places, w - 6);
      if (parts.length === n && places >= n && pitch) {
        for (i = 0; i < places; i += 1) {
          if (i < n) { token(left + w - 6 - pitch * i, y, scene.types[parts[i]], pitch * 0.4); continue; }
          ctx.beginPath(); ctx.arc(left + w - 6 - pitch * i, y, pitch * 0.3, 0, 2 * Math.PI); ctx.strokeStyle = MUTED; ctx.stroke();
        }
      } else {
        if (block.cap > 0) {
          ctx.fillStyle = full ? STATE.blocked : THIRD;
          ctx.fillRect(left + 3, y + h / 2 - 6, (w - 6) * Math.min(1, n / block.cap), 3);
        }
        ctx.font = "bold 10px " + FONT;
        ctx.fillStyle = INK;
        ctx.textAlign = "center";
        ctx.fillText(clip(n + (block.cap !== undefined ? "/" + fmt(block.cap) : ""), w - 4, measure), cx, y - 2);
      }
      ctx.beginPath(); ctx.moveTo(cx + w / 2 + 7, y); ctx.lineTo(cx + w / 2 + 2, y - 4); ctx.lineTo(cx + w / 2 + 2, y + 4);
      ctx.closePath(); ctx.fillStyle = MUTED; ctx.fill();
      if (full) { pill("FULL", cx, y - h / 2 - 9, STATE.blocked); }
      ctx.font = "11px " + FONT;
      return cx;
    }

    ctx.lineWidth = 2;
    ctx.strokeStyle = LINE;
    (clean() ? [] : scene.segments).forEach(function (s) {
      ctx.beginPath(); ctx.moveTo(px(s[0]), py(s[1])); ctx.lineTo(px(s[2]), py(s[3])); ctx.stroke();
    });
    ctx.lineWidth = 1;
    ctx.strokeStyle = MUTED;
    (clean() ? [] : scene.links).forEach(function (link) {
      var a = scene.blocks[link[0]], b = scene.blocks[link[1]];
      ctx.beginPath(); ctx.moveTo(px(a.x), py(a.y)); ctx.lineTo(px(b.x), py(b.y)); ctx.stroke();
    });

    ctx.font = "11px " + FONT;
    ctx.textBaseline = "middle";
    /* The head of the queue stands before the block, the people in service on it (k: larger on a plan). */
    function waiting(rows, index, left, y, bottom, k) {
      var j;
      if (!rows) { return; }
      for (j = 0; !state.fed[index] && j < Math.min(FEW, rows[0].length); j += 1) { token(left - 8 * k - j * 9 * k, y, scene.types[rows[0][j]], 3.5 * k); }
      for (j = 0; j < rows[1].length; j += 1) { token(left + 8 * k + j * 9 * k, bottom - 7 * k, scene.types[rows[1][j]], 3.5 * k); }
    }
    scene.blocks.forEach(function (block, index) {
      var picture = own(art.blocks, block.name), lamp = frame.st[index] || "", kind = kindOf(lamp);
      var bw = (picture ? picture.w : block.w) * view.scale, bh = (picture ? picture.h : block.h) * view.scale;
      var x = px(block.x), y = py(block.y), left = x - bw / 2, top = y - bh / 2;
      var image = picture ? ready(own(picture.states, lamp) !== undefined ? own(picture.states, lamp) : own(picture.states, "idle")) : null;
      var n = frame.n[index], s = frame.s[index], rows = frame.q[index], chips = frame.u[index], text;
      if (clean()) {
        /* The plan shows the block's own picture and the people waiting at it, no box, tag or count. */
        if (image) { ctx.drawImage(image, left, top, bw, bh); }
        waiting(rows, index, left, y, top + bh, people);
        return;
      }
      if (state.trays[index]) {
        j = state.stock[index];
        text = tray(block, index, j >= 0 && frame.lv[j] !== undefined ? Math.round(frame.lv[j]) : n);
        if (text !== null) {
          ctx.fillStyle = INK;
          ctx.textAlign = "center";
          ctx.fillText(clip(block.label, Math.min(Math.max(bw * 1.6, 90), room), measure), text, y + bh / 2 + 9);
        }
        return;
      }
      if (image) {
        ctx.drawImage(image, left, top, bw, bh);
      } else {
        /* The default picture: a raised box with its family's band, outlined by its state
           (quiet when it runs, loud only when it is down or blocked). */
        ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(left, top, bw, bh, Math.min(RADIUS, bw / 2, bh / 2)); }
        else { ctx.rect(left, top, bw, bh); }
        ctx.fillStyle = RAISED;
        ctx.fill();
        ctx.fillStyle = FILL[state.roles[index]] || FILL.block;
        ctx.fillRect(left + 1, top + 1, Math.min(5, bw / 4), bh - 2);
        ctx.strokeStyle = STATE[kind] || LINE;
        ctx.lineWidth = kind === "down" || kind === "blocked" ? 2.5 : kind === "run" ? 1.5 : 1;
        ctx.stroke();
        ctx.lineWidth = 1;
      }
      if (state.use[index] && bh >= 28) {
        ctx.font = "bold 12px " + FONT;
        ctx.fillStyle = INK;
        ctx.textAlign = "center";
        ctx.fillText(Math.round(100 * state.use[index][frame.k]) + "%", x, own(WORD, kind) ? y - 5 : y, bw - 8);
      }
      if (own(WORD, kind)) {
        ctx.font = "bold 10px " + FONT;
        pill(kind === "down" ? WORD.down + " " + fmt(downFor(index, frame.k)) : WORD[kind], x, top + bh - 9, STATE[kind]);
        ctx.font = "11px " + FONT;
      }
      ctx.fillStyle = INK;
      ctx.textAlign = "center";
      ctx.fillText(clip(block.label, Math.min(Math.max(bw * 1.6, 90), room), measure), x, top + bh + 9);
      if (chips) {
        text = chips.map(function (pair) { return pair[0] + " " + pair[1]; }).join("   ");
        ctx.fillStyle = MUTED;
        ctx.fillText(clip(text, Math.min(Math.max(bw * 2, 140), room), measure), x, top + bh + 22);
      }
      /* The queue badge: how many wait here, where anything is served (or how many are in). */
      text = block.q || lamp ? (n - s > 0 ? "Q " + (n - s) : "") : (n > 0 ? String(n) : "");
      if (text) {
        ctx.font = "bold 10px " + FONT;
        pill(text, left + bw, top, PANEL);
        ctx.strokeStyle = LINE; ctx.stroke();
        ctx.font = "11px " + FONT;
      }
      waiting(rows, index, left, y, top + bh, 1);
    });

    scene.levels.forEach(function (level, index) {
      var fill = own(art.fills, level.name), block, value = frame.lv[index], share, x, y, bh;
      if (clean() || !fill || value === undefined) { return; }
      block = scene.blocks.filter(function (b) { return b.name === fill[0]; })[0];
      if (!block || !(fill[1] > 0)) { return; }
      share = Math.min(1, Math.max(0, value / fill[1]));
      bh = block.h * view.scale;
      x = px(block.x) + block.w * view.scale / 2 + 4;
      y = py(block.y) + bh / 2;
      ctx.fillStyle = LINE; ctx.fillRect(x, y - bh, 5, bh);
      ctx.fillStyle = YOURS; ctx.fillRect(x, y - bh * share, 5, bh * share);
    });

    frame.tok.forEach(function (entry) { token(px(entry[2]), py(entry[3]), scene.types[entry[1]], 5 * people); });
    frame.veh.forEach(function (entry) {
      var image = ready(own(art.fleets, scene.fleets[entry[0]]));
      ctx.save();
      ctx.translate(px(entry[2]), py(entry[3]));
      ctx.rotate(entry[4] * Math.PI / 180);
      if (people !== 1) { ctx.scale(people * 0.75, people * 0.75); }   /* on a plan a fleet's picture is larger than a person; elsewhere it is as before D967 */
      if (image) {
        ctx.drawImage(image, -11, -11, 22, 22);
      } else {
        ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-7, 6); ctx.lineTo(-7, -6); ctx.closePath();
        ctx.fillStyle = STATE[kindOf(entry[5]) || "idle"]; ctx.fill();
        ctx.strokeStyle = INK; ctx.stroke();
      }
      ctx.restore();
    });
    frame.ag.forEach(function (entry) {
      ctx.beginPath(); ctx.arc(px(entry[2]), py(entry[3]), 3 * people, 0, 2 * Math.PI);
      ctx.fillStyle = entry[4] >= 0 ? scene.colors[entry[4]] : YOURS; ctx.fill();
    });

    /* A move the clock just passed slides from its source to its target. */
    j = slideMs(state.speed, unitS);
    state.slides = state.slides.filter(function (slide) { return wall - slide.start < j; });
    state.slides.forEach(function (slide) {
      var a = scene.blocks[slide.move[1]], b = scene.blocks[slide.move[2]], f = (wall - slide.start) / j;
      token(px(a.x + (b.x - a.x) * f), py(a.y + (b.y - a.y) * f), scene.types[slide.move[0]], 5 * people);
    });
  }

  /* "What happened when": the lanes, the bottleneck's queue (this scenario's solid, the
     baseline's dashed) and the playhead. */
  var LEFT = 92, ROW = 18;
  function drawLanes() {
    var size = sized(strip), w = size.w, end = duration(), right = w - 8, shown = state.lanes.slice(0, MAX_LANES);
    var top = 6 + shown.length * ROW, peak = 0;
    function tx(t) { return LEFT + (right - LEFT) * (end > 0 ? Math.min(1, t / end) : 0); }
    pen.font = "11px " + FONT;
    pen.textBaseline = "middle";
    pen.textAlign = "left";
    shown.forEach(function (lane, i) {
      var y = 6 + i * ROW;
      pen.fillStyle = MUTED;
      pen.fillText(clip(short(lane.name), LEFT - 6, function (text) { return pen.measureText(text).width; }), 0, y + 6);
      pen.fillStyle = RAISED;
      pen.fillRect(LEFT, y + 2, right - LEFT, 8);
      lane.spans.forEach(function (span) {
        if (span[2] === "idle") { return; }
        pen.fillStyle = STATE[span[2]];
        pen.fillRect(tx(span[0]), y + 2, Math.max(1, tx(span[1]) - tx(span[0])), 8);
      });
      pen.fillStyle = STATE.down;
      lane.fails.forEach(function (t) {
        pen.beginPath(); pen.moveTo(tx(t), y + 1); pen.lineTo(tx(t) - 4, y - 4); pen.lineTo(tx(t) + 4, y - 4);
        pen.closePath(); pen.fill();
      });
    });
    if (state.lanes.length > shown.length) {
      pen.fillStyle = MUTED;
      pen.fillText("+" + (state.lanes.length - shown.length) + " more", 0, top + 2);
      top += ROW;
    }
    state.queues.forEach(function (queue) { queue.v.forEach(function (v) { peak = Math.max(peak, v); }); });
    if (state.queues.length && peak > 0) {
      pen.fillStyle = MUTED;
      pen.fillText("queue " + state.queues[0].label, 0, top + 22, LEFT - 6);
      state.queues.forEach(function (queue, i) {
        pen.beginPath();
        queue.t.forEach(function (t, k) {
          var y = top + 40 - 34 * queue.v[k] / peak;
          if (k) { pen.lineTo(tx(t), y); } else { pen.moveTo(tx(t), y); }
        });
        pen.strokeStyle = i ? BASE : YOURS;
        pen.lineWidth = 1.5;
        if (pen.setLineDash) { pen.setLineDash(i ? [4, 3] : []); }
        pen.stroke();
      });
      if (pen.setLineDash) { pen.setLineDash([]); }
    }
    pen.strokeStyle = INK;
    pen.lineWidth = 1.5;
    pen.beginPath(); pen.moveTo(tx(state.t), 0); pen.lineTo(tx(state.t), size.h); pen.stroke();
  }

  function panel(frame) {
    var scene = run().scene, t = state.t, text = "";
    el("clock").textContent = clockTime(t, duration()) + " / " + clockTime(duration(), duration()) + " " + unit;
    if (frame.more) { text += "+" + frame.more + " not drawn. "; }
    if (frame.skipped) { text += frame.skipped + " moves not animated."; }
    el("clock").title = text;
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
    drawLanes();
    if (state.shown === frame.k) { return; }
    state.shown = frame.k;
    clear(el("res"));
    frame.r.forEach(function (row) {
      var line = add("div", el("res"), undefined, "row"), kind = kindOf(row[1]);
      add("span", line, undefined, "dot").style.background = STATE[kind] || STATE.idle;
      add("span", line, short(scene.resources[row[0]]), "name").title = scene.resources[row[0]];
      add("span", line, (own(WORD, kind) || row[1]) + " " + row[2] + " of " + fmt(row[3]));
    });
  }

  /* The queue over time of block *index* of *current*, as {label, t, v}. */
  function queueOf(current, index) {
    return {label: current.scene.blocks[index].label, t: current.t,
            v: current.frames.map(function (frame) { return ((frame.n || [])[index] || 0) - ((frame.s || [])[index] || 0); })};
  }

  /* What does not move while a run plays: built when the run is chosen. */
  function chosen() {
    var current = run(), scene = current.scene, times = current.t, text, neck = current.neck, base, match;
    state.t = Math.min(state.t, duration());
    state.shown = -1; state.slides = [];
    state.roles = roles(scene.blocks, scene.links);
    state.use = utilisation(current);
    state.pitch = pitch(scene.blocks);
    state.trays = trays(scene.blocks);
    /* A tray counts its asset's level when it has one ("buffer_1/..."), else the entities
       in it; the block a tray feeds draws no row of waiting tokens over it. */
    state.stock = scene.blocks.map(function (block) {
      var found = -1;
      if (block.buf) { scene.levels.forEach(function (level, i) { if (found < 0 && level.name.indexOf(block.name + "/") === 0) { found = i; } }); }
      return found;
    });
    state.fed = {};
    state.trays.forEach(function (sides) { if (sides && sides[1] >= 0) { state.fed[sides[1]] = true; } });
    /* The clock's slot fits its longest text, so the transport never moves (#841). */
    el("clock").style.minWidth = (2 * clockTime(duration(), duration()).length + 1.3 * unit.length + 3) + "ch";
    Array.prototype.forEach.call(el("tabs").children, function (tab) {
      tab.className = "tab" + (tab.value === data.scenarios[0] ? " base" : "") + (tab.value === state.name ? " on" : "");
    });
    /* Results and trust: the author's results for this scenario, against the baseline's. */
    clear(el("results"));
    var seen = resultsPanel(own(data.results || {}, state.name) || [], el("results"), add);
    el("key-base").hidden = !seen.base;
    el("key-target").hidden = !seen.target;
    el("results-box").hidden = !el("results").children.length;
    clear(el("trust"));
    if (own(data.trust || {}, state.name)) { trustPanel(own(data.trust, state.name), el("trust"), add, unit); }
    el("trust-box").hidden = !el("trust").children.length;
    /* The bottleneck of this recorded run, when anything waited. */
    clear(el("neck"));
    el("neck").hidden = !neck;
    state.queues = [];
    if (neck) {
      add("span", el("neck"), "Bottleneck: " + scene.blocks[neck.block].label, "callout-head");
      text = (Math.round(neck.wait * 10) / 10) + " waiting on average";
      if (neck.down >= 0.005) { text += "; failures cause " + Math.round(100 * neck.down) + "% of the waiting"; }
      if (neck.blocked >= 0.005) { text += "; being blocked, " + Math.round(100 * neck.blocked) + "%"; }
      add("span", el("neck"), text + ".");
      state.queues.push(queueOf(current, neck.block));
      base = state.name === data.scenarios[0] ? null : own(data.runs, data.scenarios[0]);
      match = base ? base.scene.blocks.map(function (b) { return b.name; }).indexOf(scene.blocks[neck.block].name) : -1;
      if (match >= 0) { state.queues.push(queueOf(base, match)); }
    }
    state.lanes = lanes(current);
    strip.style.height = (12 + Math.min(state.lanes.length, MAX_LANES + 1) * ROW + (state.queues.length ? 48 : 0)) + "px";
    el("lanes-box").hidden = !state.lanes.length && !state.queues.length;
    el("seed").textContent = current.seed === undefined ? "" :
      "Replication with seed " + current.seed + " (1 of " + current.of + ")";
    clear(el("lev"));
    scene.levels.forEach(function (level) {
      var line = add("div", el("lev"), undefined, "row");
      add("span", line, short(level.name), "name").title = level.name;
      add("div", add("span", line, undefined, "bar"));
      add("span", line, "");
    });
    clear(el("all"));
    el("allnote").textContent = "";
    el("all-box").hidden = !(data.kpis && current.of > 1);
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
    text = times.length > 1 ? "Sampled every " + fmt(times[1] - times[0]) + " " + unit + " (" +
      (times.length - 1) + " steps): between two samples a count is the earlier one, a position or a level is on the line between them, and a move shows when it happened." : "Nothing was recorded.";
    if (current.truncated) {
      text += " The recording stopped at its limit of " + current.events + " events: the run went on after t = " + fmt(duration()) + ".";
    }
    el("foot").textContent = text;
  }

  function tick(wall) {
    var current = run(), frame, from = state.t;
    if (state.playing && state.wall !== null) {
      /* Real time (D711): *speed* model seconds a wall second, in the model's unit. */
      state.t = Math.min(duration(), state.t + (wall - state.wall) / 1000 * state.speed / unitS);
      if (state.t >= duration()) { state.playing = false; el("play").textContent = "Play"; }
      movesBetween(current, from, state.t).forEach(function (move) {
        if (state.slides.length < MAX_SLIDES) { state.slides.push({move: move, start: wall}); }
      });
    }
    state.wall = wall;
    frame = frameAt(current, state.t);
    draw(frame, wall);
    panel(frame);
    tell();
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

  themeSwitch(el("theme"), function () { readLook(); wake(); });
  /* In the viewer's frame, the page's theme arrives as a message from the page; the
     replay goes on. Only the three theme words are taken. */
  window.addEventListener("message", function (event) {
    var root = document.documentElement, word = event && event.data && event.data[1];
    if (!event || event.source !== window.parent || window.parent === window || !Array.isArray(event.data) ||
        event.data[0] !== "theme") { return; }
    if (word === "light" || word === "dark") { root.dataset.theme = word; } else if (word === "auto") { delete root.dataset.theme; } else { return; }
    readLook(); wake();
  });
  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) { scheme.addEventListener("change", function () { readLook(); wake(); }); }
  }

  document.title = data.title;
  el("title").textContent = data.title;
  if (art.floor) {
    /* A plan is a place, not a line (D969); the model's wiring is one tick away. */
    el("line-h").textContent = "Layout";
    el("overlay-box").hidden = false;
    el("overlay").addEventListener("change", function () { state.overlay = el("overlay").checked; wake(); });
  }
  data.scenarios.forEach(function (name) { add("button", el("tabs"), name, "tab").value = name; });
  el("tabs").hidden = data.scenarios.length < 2;
  el("tabs").addEventListener("click", function (event) {
    var name = event && event.target ? event.target.value : undefined;
    if (typeof name !== "string" || own(data.runs, name) === undefined || name === state.name) { return; }
    state.name = name;
    chosen();
    wake();
  });
  el("play").addEventListener("click", function () {
    if (!state.playing && state.t >= duration()) { state.t = 0; }
    state.playing = !state.playing;
    el("play").textContent = state.playing ? "Pause" : "Play";
    wake();
  });
  function step(dir) {
    state.playing = false;
    el("play").textContent = "Play";
    state.t = stepTime(run().t, state.t, dir);
    state.slides = [];
    wake();
  }
  el("back").addEventListener("click", function () { step(-1); });
  el("step").addEventListener("click", function () { step(1); });
  function seek(t) {
    state.t = Math.min(duration(), Math.max(0, t));
    state.slides = [];
    wake();
  }
  el("seek").addEventListener("input", function () { seek(duration() * el("seek").value / 1000); });
  /* Dragging on the lanes seeks: x is time. Over a lane's label, its full name shows. */
  function drag(event) {
    var w = strip.clientWidth - 8 - LEFT;
    var lane = event.offsetX < LEFT ? state.lanes.slice(0, MAX_LANES)[Math.floor((event.offsetY - 6) / ROW)] : null;
    strip.title = lane ? lane.name : "";
    if (state.seeking && w > 0) { seek(duration() * (event.offsetX - LEFT) / w); }
  }
  strip.addEventListener("pointerdown", function (event) {
    state.seeking = true;
    if (strip.setPointerCapture) { strip.setPointerCapture(event.pointerId); }
    drag(event);
  });
  strip.addEventListener("pointermove", drag);
  strip.addEventListener("pointerup", function () { state.seeking = false; });
  SPEEDS.forEach(function (speed) { add("option", el("speed"), speedLabel(speed)).value = String(speed); });
  state.speed = defaultSpeed(duration() * unitS);
  el("speed").value = String(state.speed);
  el("speed").addEventListener("change", function () { state.speed = Number(el("speed").value) || state.speed; });
  window.addEventListener("resize", wake);
  /* In the viewer's frame the page outside cannot see how tall the plan wants to be: say so
     (a height only; a sandboxed frame has no origin to name, so the parent checks the sender). */
  function tell() {
    if (!(framed && art.floor && window.parent !== window)) { return; }
    var box = document.body.getBoundingClientRect(), height = Math.ceil(box.top + box.height) + 8;
    if (height !== state.told) { state.told = height; window.parent.postMessage(["height", height], "*"); }
  }
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
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'sha256-__CSS_HASH__'; script-src 'sha256-__THEME_HASH__' 'sha256-__HASH__'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run player</title>
<style>__CSS__</style>
<script id="theme-pick">__THEME__</script>
__HEAD__</head>
<body>
<header class="topbar">
<div class="ident"><h1 id="title"></h1></div>
<div id="tabs" class="tabs" aria-label="Scenario"></div>
<div id="bar" class="transport">
<button id="play" type="button" class="primary">Play</button>
<button id="back" type="button" aria-label="Step back">&lt;|</button>
<button id="step" type="button" aria-label="Step">|&gt;</button>
<select id="speed" aria-label="Speed, plant time per second"></select>
<span id="clock"></span>
</div>
<button id="theme" type="button" aria-label="Light or dark">Theme: auto</button>
</header>
<input id="seek" type="range" min="0" max="1000" value="0" aria-label="Time">
__TOP__<main class="console">
<section class="centre">
<div class="panel">
<div class="panel-head"><h2 id="line-h">The line</h2><label id="overlay-box" class="toggle" hidden><input id="overlay" type="checkbox"> Show model blocks</label><span class="legend"><span class="key run">working</span><span class="key">idle</span><span class="key down">down</span><span class="key blocked">blocked</span></span></div>
<canvas id="view"></canvas>
<p id="neck" class="callout" hidden></p>
</div>
<div class="panel" id="lanes-box">
<div class="panel-head"><h2>What happened when</h2><span class="note">drag to seek</span></div>
<canvas id="lanes"></canvas>
</div>
</section>
<aside class="side">
<div class="panel" id="results-box">
<div class="panel-head"><h2>Results</h2><span class="legend"><span class="key yours">scenario</span><span class="key base" id="key-base">baseline</span><span class="key target" id="key-target">target</span></span></div>
<div id="results"></div>
</div>
<div class="panel" id="trust-box"><div class="panel-head"><h2>Can I trust this?</h2></div><table id="trust" class="trust"></table></div>
<details class="panel" id="run-box"><summary><h2 id="run-h">This run</h2></summary>
<table>
<tr><td>Time</td><td id="now-t"></td></tr>
<tr><td>In the system</td><td id="now-wip"></td></tr>
<tr><td>Created</td><td id="now-in"></td></tr>
<tr><td>Completed</td><td id="now-out"></td></tr>
</table>
<div id="res"></div>
<div id="lev"></div>
<p class="note" id="seed"></p>
<p class="note" id="foot"></p>
</details>
<details class="panel" id="all-box"><summary id="all-h">All replications</summary>
<p class="note" id="allnote"></p>
<div class="scroll"><table id="all"></table></div>
</details>
</aside>
</main>
__BOTTOM__
<script type="application/json" id="tape">__TAPE__</script>
<script>__SCRIPT__</script>
</body>
</html>
"""


def csp_hash(text: str) -> str:
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def page_shell(script: str = SCRIPT, head: str = "", top: str = "", bottom: str = "",
           tokens: dict | None = None, rules: str | None = None) -> str:
    """The constant page: everything but the data block. The policy pins the script and
    the style sheet by their sha256, so the browser applies these and no other. A page
    built on the player (the twin's monitor, `monitor.py`) gives its own constant
    *script* (the player's with a `monitor` function before it), style *rules* (the
    player's with its own after them) and constant markup for the head and around the
    view. *tokens* are the page's look (`check_tokens`)."""
    css = style_sheet(tokens, rules)
    parts = {"CSS": css, "CSS_HASH": csp_hash(css), "HASH": csp_hash(script), "HEAD": head,
             "TOP": top, "BOTTOM": bottom, "SCRIPT": script, "THEME": THEME,
             "THEME_HASH": csp_hash(THEME)}
    # One pass: a part is never searched for another part's mark; __TAPE__ stays.
    return re.sub("__([A-Z_]+)__", lambda found: parts.get(found.group(1), found.group(0)), _PAGE)


def plain(value):
    """*value* as JSON can hold it: a number that is not finite becomes null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def embed(data) -> str:
    """*data* as the text of the data block. Pure ASCII, and no `<`, `>`, `&` or `/`
    as itself: no name can close the block, open a tag or spell an address."""
    text = json.dumps(plain(data), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
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
    art = {"blocks": blocks, "types": one(pictures.get("entities")),
           "fleets": one(pictures.get("fleets")), "fills": fills}
    floor = _floor_plan(pictures.get("background"), image, images)
    if floor is not None:
        art["floor"] = floor
    return images, art


# The floor plan as a raster: a data URI of one of these types, no larger than this (the
# player's own cap, MAX_BYTES, still applies to the whole page).
_RASTER = re.compile(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}")
MAX_FLOOR_BYTES = 3_000_000


def _floor_plan(entry, image, images: list) -> dict | None:
    """The model's `background` as the layout view draws it, or None: {"i": the picture's
    index, "x", "y", "w", "h": its rectangle in layout units, "a": opacity}.

    *entry* is the caller's `{"svg": text}` (checked like every picture, D198) or
    `{"image": "data:image/png|jpeg|webp;base64,..."}`, with `x`, `y`, `w`, `h` (layout units)
    and `opacity`. The engine reads no file: the caller did (the model names one on its
    machine). A picture refused, over `MAX_FLOOR_BYTES`, or with a rectangle that is not
    finite and positive is left out: the layout is drawn on the plain page."""
    if not isinstance(entry, dict):
        return None
    try:
        x, y, w, h = (float(entry[key]) for key in ("x", "y", "w", "h"))
        opacity = min(max(float(entry.get("opacity", 1.0)), 0.0), 1.0)
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h, opacity)) or w <= 0 or h <= 0:
        return None
    index = image(entry.get("svg")) if "svg" in entry else None
    raster = entry.get("image")
    if index is None and isinstance(raster, str) and len(raster) <= MAX_FLOOR_BYTES \
            and _RASTER.fullmatch(raster):
        images.append(raster)
        index = len(images) - 1
    if index is None:
        return None
    return {"i": index, "x": x, "y": y, "w": w, "h": h, "a": opacity}


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


# The direction a generated result is better in (spec: "the KPI's known direction"); a
# result not here, and without the author's `better`, gets no better/worse word.
_BETTER = {"system/Avg Throughput Time": "lower", "system/Throughput (per time unit)": "higher",
           "system/Avg WIP (Little)": "lower", "system/Total Cost": "lower"}


def result_rows(layout, results, experiment, names) -> dict | None:
    """{scenario: [row, ...]} for the results panel: per result of the model's dashboard
    (its author's labels; a model without one gets the generated results,
    `dashboard.default_dashboard`) {kpi, label, unit, yours: {mean, lo, hi}, base (the
    baseline's; None for the baseline itself and for a plain run), target, better,
    delta_pct, verdict}. The numbers and the significance are
    `ExperimentResult.comparison`'s (a plain run: `ci95` over its replications); the
    verdict is "better"/"worse" when the direction is known, else "higher"/"lower", and
    "no clear change" when the difference interval holds 0 (D705: always a word)."""
    from nagasim.core.stats import ci95
    from nagasim.dashboard import resolve_dashboard
    from nagasim.experiments import BASELINE, flatten_kpis

    if results is None and experiment is None:
        return None
    entries = [entry for entry in resolve_dashboard(layout)["results"] if entry.get("kpi")]
    order = [entry["kpi"] for entry in entries]
    if not order:
        return None
    stats = {}          # (kpi, scenario) -> the numbers of that cell
    if experiment is not None:
        for row in experiment.comparison(kpis=order).to_dict("records"):
            stats[row["KPI"], row["Scenario"]] = {
                "mean": row["Mean"], "lo": row["LB"], "hi": row["UB"],
                "diff": row["Diff vs baseline"], "sig": bool(row["Significant"])}
    else:
        flat = flatten_kpis(results)
        for kpi, group in flat[flat["KPI"].isin(order)].groupby("KPI"):
            interval = ci95(group["Value"].to_numpy(dtype=float))
            stats[kpi, names[0]] = {"mean": interval["mean"], "lo": interval["lb"],
                                    "hi": interval["ub"], "diff": None, "sig": False}
    out = {}
    for name in names:
        rows = []
        for entry in entries:
            kpi = entry["kpi"]
            mine = stats.get((kpi, name))
            if mine is None:
                continue
            base = stats.get((kpi, BASELINE)) if name != BASELINE and mine["diff"] is not None else None
            better = entry.get("better") or _BETTER.get(kpi)
            verdict = pct = None
            if base is not None and math.isfinite(mine["diff"]):
                diff = mine["diff"]
                if not mine["sig"]:
                    verdict = "no clear change"
                elif better in ("higher", "lower"):
                    verdict = "better" if (diff > 0) == (better == "higher") else "worse"
                else:
                    verdict = "higher" if diff > 0 else "lower"
                pct = 100 * diff / abs(base["mean"]) if base["mean"] else None
            rows.append({"kpi": kpi, "label": entry.get("label") or kpi.partition("/")[2] or kpi,
                         "unit": entry.get("unit") or "",
                         "yours": {key: mine[key] for key in ("mean", "lo", "hi")},
                         "base": None if base is None else {key: base[key] for key in ("mean", "lo", "hi")},
                         "target": entry.get("target"), "better": better, "delta_pct": pct,
                         "verdict": verdict})
        if rows:
            out[name] = rows
    return out or None


def trust_card(layout, results, experiment, names, overrides, rows, seeds, asked=None) -> dict | None:
    """{scenario: {laws_pass, laws_warn, warmup, replications, half_width_pct, input_text,
    params_text}} for the trust card: the run's law checks (`laws`, D684), the model's warm-up,
    the replications, and the half-width of the first result's 95% interval as a share of its
    mean; `input_text` and `params_text` quote the recorded input-uncertainty and ABC answers
    in *asked*, else "not measured" (D739). None without results."""
    from nagasim import laws, uncertainty
    from nagasim.experiments import apply_overrides

    if experiment is not None:
        frames = experiment.results
    else:
        frames = {names[0]: results} if results is not None else {}
    warmup = float((layout.get("simulation") or {}).get("warmup") or 0)
    texts = uncertainty.trust_texts(asked)
    out = {}
    for name in names:
        if name not in frames:
            continue
        model = apply_overrides(layout, overrides[name]) if overrides.get(name) else layout
        found = laws.summary(laws.checks(model, frames[name]))
        first = ((rows or {}).get(name) or [None])[0]
        half = None
        if first:
            mean, lo, hi = (first["yours"][key] for key in ("mean", "lo", "hi"))
            if mean and all(v is not None and math.isfinite(v) for v in (lo, hi)):
                half = 50 * (hi - lo) / abs(mean)
        out[name] = {"laws_pass": found["passed"], "laws_warn": len(found["partial"]),
                     "warmup": warmup, "replications": seeds.get(name, (None, 1))[1],
                     "half_width_pct": half, "input_text": texts["input"], "params_text": texts["params"]}
    return out or None


# A lamp that says the block serves something (the script's KIND "run").
_SERVING = ("busy", "loaded", "handling", "held")


def _neck(run: dict, layout: dict | None = None) -> dict | None:
    """The bottleneck of a recorded run, from its tape (D705): of the blocks that serve
    (their lamp said busy at some sample; a buffer's contents all wait, so a buffer never
    is, #848), the one where entities waited most (entities in it but not in service, x
    time; an asset's station counts its own queue), as {block, wait (the time-average
    number waiting), down and blocked (the shares of that waiting while the block's own
    lamp said down or blocked), res (its resource's index in the tape, when *layout*
    names one: its `resource_name`, or one of an asset's `block/` resources)}. None when
    nothing waited at a serving block."""
    times, frames = run["t"], run["frames"]
    best = None
    for index, _block in enumerate(run["scene"]["blocks"]):
        wait = down = blocked = 0.0
        serves = False
        for k in range(len(times) - 1):
            frame = frames[k]
            n, s, lamps = frame.get("n") or (), frame.get("s") or (), frame.get("st") or ()
            waiting = (n[index] if index < len(n) else 0) - (s[index] if index < len(s) else 0)
            area = waiting * (times[k + 1] - times[k])
            lamp = lamps[index] if index < len(lamps) else ""
            serves = serves or lamp in _SERVING
            wait += area
            down += area if lamp == "down" else 0.0
            blocked += area if lamp == "blocked" else 0.0
        if serves and wait > 0 and (best is None or wait > best[0]):
            best = (wait, index, down, blocked)
    if best is None:
        return None
    wait, index, down, blocked = best
    out = {"block": index, "wait": wait / times[-1], "down": down / wait, "blocked": blocked / wait}
    if layout is not None:
        name = run["scene"]["blocks"][index]["name"]
        own = next(((block.get("params") or {}).get("resource_name") for block in layout.get("blocks") or ()
                    if block.get("name") == name), None)
        found = [res for res, resource in enumerate(run["scene"].get("resources") or ())
                 if resource == own or resource.startswith(name + "/")]
        if found:
            out["res"] = found[0]
    return out


def build_player(layout: dict, recordings: dict, *, results=None, experiment=None,
                 title: str | None = None, view: str | None = None, pictures: dict | None = None,
                 samples: int = tape.SAMPLES, tokens: dict | None = None, start: str | None = None,
                 path=None, asked=None) -> str:
    """Return the player page of *recordings*; write it to *path* when given. *asked* is the
    recorded tool answers of the model (`Workspace.asked`): the trust card's input-data and
    parameter rows quote them, else say "not measured" (D739).

    *recordings* is `{scenario name: TraceRecorder or event list}`; a plain run is
    `{BASELINE: recorder}`. A scenario is drawn on the layout with its own overrides
    applied (a scenario may move a node or change a fleet's size). *results* (one
    run's frames) or *experiment* (an `ExperimentResult`) adds the seed of each
    recording and, for more than one replication, the "all replications" table.
    *view* is "schematic" or "layout" (default: `tape.default_view`). *pictures* is
    `{"blocks": {name: {"size": (w, h), "states": {state: svg}}}, "entities":
    {type: svg}, "fleets": {fleet: svg}, "fills": {level: (block, capacity)}}`.
    *tokens* is the page's look (`DEFAULT_TOKENS` for the keys and the default); a
    value that is not a colour, a font stack or a radius is a ValueError. The first
    recording is the one the others are compared with; *start* is the one shown first
    (default: the first).

    Over `MAX_BYTES` the tapes are written again with half the *samples*, down to
    `tape.MIN_SAMPLES`; a ValueError says so when even that does not fit.
    """
    from nagasim.experiments import apply_overrides, scenarios_of

    if not recordings:
        raise ValueError("the player needs at least one recorded run")
    shell = page_shell(tokens=tokens)               # a bad token: refused before any work
    view = view or tape.default_view(layout)
    names = list(recordings)
    overrides = {scenario.get("name"): scenario.get("overrides") or []
                 for scenario in scenarios_of(layout)}
    seeds, rows = _replications(results, experiment, names)
    rows_by_scenario = result_rows(layout, results, experiment, names)
    trust = trust_card(layout, results, experiment, names, overrides, rows_by_scenario, seeds, asked)
    images, art = _art(pictures)
    unit = units_of(layout.get("simulation")).time
    samples = max(int(samples), 1)
    while True:
        runs = {}
        for name, trace in recordings.items():
            changed = overrides.get(name)
            model = apply_overrides(layout, changed) if changed else layout
            run = tape.TapeWriter(model, view).run(trace, samples)
            if name in seeds:
                run["seed"], run["of"] = seeds[name]
            neck = _neck(run, model)
            if neck is not None:
                run["neck"] = neck
            runs[name] = run
        data = {"title": title or "Run", "scenarios": names, "runs": runs, "kpis": rows,
                "results": rows_by_scenario, "trust": trust,
                "unit": unit, "unit_s": UNITS.get(unit, 1), "pics": images, "art": art,
                "start": start}
        page = shell.replace("__TAPE__", embed(data))
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
