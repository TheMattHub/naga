"""The monitor of a twin (PHASE7 7.13, D278 to D287): what a result says to a person, the
event lines of a plant, and the page a browser beside a running `naga twin` reloads.

    page = monitor.Page(layout, "twin_out/monitor.html", every=5.0, title="line")
    page.write(cycle.step(), cycle.events)

* `summary(doc)` turns a result (`twin.json`, D267) into what is shown: headline, notes,
  sources, problems, why a forecast is missing, the forecast against what was observed, what
  the plant's counters counted and how the stored forecasts scored (7.14). The page, the
  editor's monitor and `naga twin status` render it and decide nothing (D281), so a limit is
  worded once: approximate counts, evidence in distribution, a forecast refused by name, a
  score with its basis or the reason it has none.
* `Log` keeps the last event lines of one twin: an item entering, moving or leaving, a
  resource going down or up, a source changing state, the forecast changing status, a
  problem code appearing (D285). Bounded; what is not listed is counted.
* `Page` is the 7.7 player's page with one sample, the present, and the two above in its
  data block. It is rewritten whole at every step and reloads itself (D278): a page opened
  from disk can read no file beside it, and re-reads itself on a refresh.

The page leaves its author's machine like a player file: `player.py` says what may be in
it and how (D199). Nothing here reads a file, opens a socket or is steered by a result.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from nagasim import player
from nagasim.core import tape
from nagasim.core.playback import Timeline
from nagasim.core.timeunits import UNITS
from nagasim.live import stamp

__all__ = ["KEPT", "PER_STEP", "PAGE_FILE", "APPROXIMATE", "IN_DISTRIBUTION", "COUNTED", "SCORED",
           "SAME_SEED", "NO_SCORE", "MISSED", "ADVICE", "ASSUMES", "NO_ADVICE", "Log", "Page", "summary",
           "state"]

KEPT = 200              # event lines of one twin
PER_STEP = 100          # of one step; the rest is counted
PAGE_FILE = "monitor.html"
_KPIS, _POINTS, _TEXT, _SCORES, _ADVICE = 12, 60, 400, 300, 20

APPROXIMATE = ("Counts are approximate: this plant names no items, so the numbers of items in "
               "a block are estimates, and item events in the list are inferred from those "
               "counts. What a counter signal counts is observed, not estimated.")
COUNTED = ("Counted by the plant: what left each block since the twin first saw its counter. "
           "Observed, not estimated. A counter that went back lost count there (resets), and its "
           "total is a lower bound from then on.")
SCORED = ("How the stored forecasts held, by how far ahead they looked. Coverage is the share of "
          "readings inside the 5% to 95% band (about 90% when the model is right); bias is "
          "observed minus forecast mean; the pinball loss is lower for a band that is narrow and "
          "right. A row marked * holds whole numbers only (units in service, items, counts): a "
          "right model covers more than 90% of those, so compare its pinball loss, not its "
          "coverage. Readings overlap in time and share one plant history: evidence in "
          "distribution, not a test.")
MISSED = ("Missed: the twin was not there in time, or a source was not ready. Not compared: a "
          "counter lost count, or was first seen, after the forecast started.")
SAME_SEED = ("{n} of these forecasts ran on the seed of the one before (a twin stepped by a "
             "scheduler): they are not independent samples.")
NO_SCORE = {"not_observed": "No score: this twin's source gives no observed series to set a "
                            "forecast against.",
            "no_forecast": "No score yet: no forecast has finished.",
            "none_due": "No score yet: no stated time of a stored forecast has come.",
            # A result of a twin older than 7.14 has no `scores` at all.
            "old": "No score: this result was written before scores existed."}
IN_DISTRIBUTION = ("Evidence in distribution, not a replay: work in progress is drawn afresh at "
                   "each forecast, so a point inside its 5% to 95% band agrees with the forecast "
                   "and one outside it is a signal, not a proof.")
ADVICE = ("What this model says each action would change, compared with doing nothing on the "
          "same random numbers. Effect: the mean difference, with its 95% interval; confidence: "
          "the share of replications in which the action did better. Only an action whose "
          "interval excludes \"no effect\" is recommended. Each candidate is judged alone: of "
          "candidates that change nothing, about 1 in 20 pass by chance, so of N of them about "
          "N/20. Evidence in distribution from the model, not a promise about the plant.")
ASSUMES = "This forecast assumes {n} taken action(s): see Recommendations."
NO_ADVICE = {"no_forecast": "No recommendation: no forecast was made at the last attempt (see "
                            "Forecast for why).",
             "none_better": "No recommendation: no candidate's interval excludes \"no effect\" on "
                            "the better side.",
             "kpi": "No recommendation: the forecast has no KPI of that name.",
             "replications": "No recommendation: an interval needs at least 2 replications.",
             "failed": "No recommendation: the candidates could not be compared."}
_VERDICT = {"better": "better", "no_effect": "no effect", "worse": "worse", "failed": "failed",
            "standing": "taken: it stands"}
_EFFECT = {"waiting": "being measured", "measured": "measured", "missed": "missed: the twin "
           "was not there in time", "lost": "not compared: the counter lost count, or the "
           "twin was started again", "not_observable": "not observable: the plant counts no "
           "such figure"}
_LIMIT = "Stated limit: this model is monitored, not forecast. "
# The age of a result, by the clock of whoever looks: {age} is filled in when it is looked at.
_FRESH = "Written {age} s ago, by this computer's clock ({written}); "
_STALE = ("STALE: the twin last wrote {age} s ago, judged by this computer's clock ({written}); "
          "its cadence is {every} s. What is shown is old.")
_INSIDE = {True: "yes", False: "no", None: ""}
_WHY = {"skipped": "No forecast was started: ", "refused": "The forecast was refused: ",
        "failed": "The latest forecast failed: "}


# ---------------------------------------------------------------- reading a result
def _table(value) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value) -> list:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value) -> str:
    return "" if value is None else str(value)[:_TEXT]


def _number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) \
        and abs(value) <= 1.7976931348623157e308    # finite, and an int a float can hold (#373)
    return value if ok else None


def _g(value) -> str:
    return "-" if _number(value) is None else f"{value:g}"


def _scores(doc: dict, unit: str) -> dict:
    """The scores of a result as lines and rows: the basis always, or the reason for none."""
    scores = _table(doc.get("scores"))
    basis, rows = _table(scores.get("basis")), _rows(scores.get("rows"))
    if not rows:
        reason = _text(scores.get("reason")) if "scores" in doc else "old"
        return {"lines": [NO_SCORE.get(reason, f"No score: {reason or 'no reason was given'}.")],
                "rows": [], "more": 0}
    first, last = _number(basis.get("first")), _number(basis.get("last"))
    lines = [f"{_g(basis.get('forecasts'))} forecasts, {_g(basis.get('points'))} readings scored, "
             f"{_g(basis.get('missed'))} missed, {_g(basis.get('lost'))} not compared, since "
             f"{_text(basis.get('since')) or '?'}"
             + ("" if first is None or last is None
                else f" (plant time {first:g} to {last:g} {unit}".rstrip() + ")") + ".", SCORED]
    if _number(basis.get("missed")) or _number(basis.get("lost")):
        lines.append(MISSED)
    same = _number(basis.get("same_seed"))
    if same:
        lines.append(SAME_SEED.replace("{n}", f"{same:g}"))

    def share(row):
        value = _number(row.get("coverage"))
        return "" if value is None else f"{value:.0%}" + (" *" if row.get("whole") is True else "")
    return {"lines": lines, "more": max(len(rows) - _SCORES, 0),
            "rows": [[_text(r.get("series")), _number(r.get("lead")), _number(r.get("points")),
                      _number(r.get("missed")), _number(r.get("lost")), share(r),
                      _number(r.get("bias")), _number(r.get("error")), _number(r.get("pinball"))]
                     for r in rows[:_SCORES]]}


def _action(value) -> str:
    said = _table(value)
    value = said.get("value")       # a capacity, or the block a reroute names (D406)
    shown = _text(value)[:80] if isinstance(value, str) else _g(value)
    return f"{_text(said.get('verb'))} {_text(said.get('target'))[:80]} = {shown}"


def _recommend(doc: dict) -> dict | None:
    """The recommendations of a result in two parts (D358), or None for a twin that was asked
    for none. `lead`: what stands now, the recommended action, the outbox -- always at least
    the outbox line. `fold`: the basis and the explanation (KPI line, ADVICE, effects). Rows:
    rank, action, effect, low, high, confidence, verdict, the record's status, the first
    characters of its id. No sink, no path, no reply text."""
    if "recommend" not in doc:
        return None
    said = _table(doc.get("recommend"))
    box, items = _table(said.get("outbox")), _rows(said.get("items"))
    lead = []
    for standing in _rows(said.get("standing"))[:_ADVICE]:
        lead.append(f"Taken and standing in every forecast since {_text(standing.get('since'))}: "
                    f"{_action(standing.get('action'))}.")
    for ended in _rows(said.get("ended"))[:_ADVICE]:
        lead.append(f"Ended {_text(ended.get('ended'))}: {_action(ended.get('action'))} (taken "
                    f"{_text(ended.get('since'))}); later forecasts no longer assume it.")
    if said.get("status") != "ok":
        reason = _text(said.get("reason"))
        lead.append(NO_ADVICE.get(reason, f"No recommendation: {reason or 'no reason was given'}."))
    else:
        better = [i for i in items if i.get("verdict") == "better"      # not yet answered
                  and i.get("status") in ("delivered", "proposed")]
        if better:
            top = better[0]
            lead.append(
                f"Recommended: {_action(top.get('action'))}, effect {_g(top.get('effect'))} "
                f"(95% interval {_g(top.get('low'))} to {_g(top.get('high'))})"
                + (f"; {len(better) - 1} more in the table" if len(better) > 1 else "") + ".")
    if said.get("reproducible") is False:
        lead.append("This set's state could not be kept: it does not reproduce, and none of "
                    "it is delivered.")
    records = _table(box.get("records"))
    if box.get("sink"):
        lead.append(
            f"Outbox ({_text(box.get('sink'))[:20]}): {_g(records.get('delivered'))} delivered "
            f"and unanswered, {_g(records.get('proposed'))} waiting, {_g(records.get('taken'))} "
            f"taken, {_g(records.get('rejected'))} rejected, {_g(records.get('expired'))} "
            f"expired, {_g(records.get('dropped'))} dropped"
            + ("" if box.get("state") in ("ok", None)
               else f"; {_text(box.get('state'))[:20]} ({_text(box.get('why'))[:40]})") + ".")
        dropped = {key: _number(value) for key, value in _table(box.get("replies")).items()
                   if key in ("unknown", "repeated", "early", "late", "not_standing",
                              "malformed") and _number(value)}
        if dropped:
            lead.append("Replies dropped: " + ", ".join(
                f"{count:g} {kind}" for kind, count in sorted(dropped.items())) + ".")
    else:
        lead.append("No outbox: the recommendations are shown here and written nowhere else.")
    fold = [f"On {_text(said.get('kpi'))[:200]} ({_text(said.get('better'))} is better)"
            + (f", from state {_text(said.get('state'))[:12]}, seed {_g(said.get('seed'))}, "
               f"{_g(said.get('replications'))} replications" if said.get("state") else "") + ".",
            ADVICE]
    for effect in _rows(said.get("effects"))[:_ADVICE]:
        status = _text(effect.get("status"))
        line = f"Effect of {_action(effect.get('action'))}: {_EFFECT.get(status, status)}"
        if status == "measured":
            line += (f", {_g(effect.get('observed'))} observed against a band of "
                     f"{_g(effect.get('q05'))} to {_g(effect.get('q95'))} ("
                     + ("inside" if effect.get("inside_interval") is True else "outside") + ")")
        fold.append(line + ".")
    if said.get("measurable") is False:
        fold.append("The effect of an action taken cannot be measured on this KPI: a plant "
                    "shows only what a counted block let out.")
    return {"lead": lead, "fold": fold, "more": max(len(items) - _ADVICE, 0),
            "rows": [[_number(i.get("rank")), _action(i.get("action")), _number(i.get("effect")),
                      _number(i.get("low")), _number(i.get("high")), _number(i.get("confidence")),
                      _VERDICT.get(_text(i.get("verdict")), _text(i.get("verdict"))[:20]),
                      _text(i.get("status"))[:20], _text(i.get("id"))[:12]]
                     for i in items[:_ADVICE]]}


def state(shown: dict, now: float) -> tuple:
    """(how old the result of a `summary` is at *now*, in its one sentence; whether that is
    stale). Stale is over two cadences with no result (D283): a healthy result is up to one
    cadence old plus the step. Never for a twin a scheduler steps (*every* null), and the
    age is "?" for a stamp nobody can read. A page works the same out in its script."""
    at = stamp(shown["written"])
    age = None if at is None else now - at
    old = age is not None and shown["state"]["stale"] is not None and age > 2 * shown["every"]
    text = shown["state"]["stale" if old else "fresh"]
    return text.replace("{age}", "?" if age is None else f"{age:.0f}"), old


def summary(doc) -> dict:
    """What a monitor shows of one result, as plain data. Read defensively: the document may
    come from a file, and is then only known to be format 1."""
    doc = _table(doc)
    model, view, forecast = _table(doc.get("model")), _table(doc.get("view")), _table(doc.get("forecast"))
    unit = _text(model.get("time_unit"))
    problems = _rows(doc.get("problems"))
    head = f"cycle {_text(doc.get('cycle'))}: plant at t = {_g(doc.get('at'))} {unit}".rstrip()
    if doc.get("stamp"):
        head += f" ({_text(doc.get('stamp'))})"
    status, reason = _text(forecast.get("status")) or "none", _text(forecast.get("reason"))
    record = isinstance(forecast.get("state"), str)
    lines = [f"Status: {status}"]
    if status == "none":
        lines.append("No forecast yet.")
    elif status == "running":
        lines.append("A forecast is running.")
    elif status in _WHY:
        why = [_text(p.get("message")) for p in problems
               if reason and p.get("code") == reason] or [reason or "no reason was given"]
        lead = _LIMIT if reason == "E_TWIN_STATE" else _WHY[status]
        lines += [lead + text for text in why]
    if record:
        lines.append(
            ("" if status == "ok" else "Shown: the last finished forecast. ")
            + f"From t = {_g(forecast.get('from'))} {unit}".rstrip()
            + (f" ({_text(forecast.get('stamp'))})" if forecast.get("stamp") else "")
            + f", {_g(forecast.get('replications'))} replications over "
              f"{_g(forecast.get('horizon'))} {unit}".rstrip()
            + f", seed {_g(forecast.get('seed'))}, state {_text(forecast.get('state'))[:12]}.")
    assumed = forecast.get("actions") if record else None
    if isinstance(assumed, list) and assumed:       # D307: said wherever a forecast is
        lines.append(ASSUMES.replace("{n}", str(len(assumed))))
    points = _rows(forecast.get("points")) if record else []
    compared = _rows(doc.get("comparison"))
    if compared:
        lines.append(f"{len(compared)} compared points over the stored forecasts, "
                     f"{sum(1 for row in compared if row.get('inside') is True)} inside their "
                     f"5% to 95% band.")
    if points or compared:
        lines.append(IN_DISTRIBUTION)

    def inside(point):
        low, high, seen = (_number(point.get(key)) for key in ("q05", "q95", "observed"))
        if point.get("missed"):
            return "missed"
        return _INSIDE[None if None in (low, high, seen) else low <= seen <= high]

    written = _text(doc.get("written"))
    every = _number(_table(doc.get("cadence")).get("every"))
    every = every if every and every > 0 else None
    fresh = _FRESH.replace("{written}", written) + (
        "stepped by a scheduler: not watched for staleness." if every is None
        else f"the twin's cadence is {every:g} s.")
    return {
        "head": head, "written": written, "started": _text(doc.get("started")), "every": every,
        "state": {"fresh": fresh, "stale": None if every is None else _STALE.replace(
            "{written}", written).replace("{every}", f"{every:g}")},
        "notes": [APPROXIMATE] if view.get("approximate") or forecast.get("approximate") else [],
        "sources": [[_text(row.get("name")), _text(row.get("state")), _number(row.get("age")),
                     _number(row.get("lost")), _number(row.get("gaps"))]
                    for row in _rows(doc.get("sources"))],
        "problems": [[_text(p.get("severity")), _text(p.get("code")), _text(p.get("message")),
                      _text(p.get("hint"))] for p in problems],
        # What the plant counted (D288): block, total, since (model time), resets.
        "counters": [[_text(name), _number(said.get("total")), _number(said.get("since")),
                      _number(said.get("resets"))]
                     for name, said in list(_table(view.get("counters")).items())[:_POINTS]
                     if isinstance(said, dict)],
        "counted": COUNTED,
        "scores": _scores(doc, unit),
        "recommend": _recommend(doc),
        "forecast": {
            "status": status, "lines": lines,
            "kpis": [[_text(k.get("kpi")), _number(k.get("mean")), _number(k.get("q05")),
                      _number(k.get("q95"))] for k in _rows(forecast.get("kpis"))[:_KPIS]]
            if record else [],
            "points": [[_text(p.get("series")), _number(p.get("at")), _number(p.get("mean")),
                        _number(p.get("q05")), _number(p.get("q95")), _number(p.get("observed")),
                        inside(p)] for p in points[:_POINTS]],
            "more": max(len(points) - _POINTS, 0)}}


# ---------------------------------------------------------------- event lines
def _item(event) -> str:
    # The type of an entity nothing gives one, in a trace and in a plant mapping.
    return "an item" if event.e_type in ("", "Unknown", "DEFAULT", None) else str(event.e_type)


def _said(event) -> str | None:
    """One trace event as an operator reads it, or None for what the drawing carries."""
    kind = event.kind
    if kind == "entity_created":
        return f"{_item(event)} entered at {event.block}"
    if kind == "entity_moved":
        return f"{_item(event)}: {event.block} to {event.target}" if event.block \
            else f"{_item(event)} back at {event.target}"
    if kind == "entity_disposed":
        return f"{_item(event)} left from {event.block}"
    if kind == "resource_state":
        if event.state in ("down", "up"):
            return f"{event.resource} {event.state}"
        if event.state == "capacity":
            return f"{event.resource} capacity {event.value:g}"
        if event.state == "blocked":
            return f"{event.resource} blocked" if event.value else f"{event.resource} no longer blocked"
    return None


class Log:
    """The event lines of one twin, oldest first: `rows` is `[t, stamp or None, text]`, at
    most `KEPT`; `more` counts what a step had over `PER_STEP`."""

    def __init__(self):
        self.rows: deque = deque(maxlen=KEPT)
        self.more = 0
        self._was: tuple = ({}, None, set())

    def add(self, doc, events) -> None:
        doc = _table(doc)
        model = _table(doc.get("model"))
        epoch, unit = stamp(model.get("epoch")), UNITS.get(model.get("time_unit"))

        def instant(t):
            try:
                return datetime.fromtimestamp(epoch + t * unit, timezone.utc).isoformat(
                    timespec="seconds")
            except (TypeError, OverflowError, OSError, ValueError):
                return None

        said = [(event.t, text) for event in events if (text := _said(event)) is not None]
        self.more += max(len(said) - PER_STEP, 0)
        self.rows.extend([t, instant(t), text[:_TEXT]] for t, text in said[-PER_STEP:])
        # What changed about the twin itself since the step before.
        sources = {_text(row.get("name")): _text(row.get("state")) for row in _rows(doc.get("sources"))}
        forecast = _table(doc.get("forecast"))
        status = (_text(forecast.get("status")), _text(forecast.get("reason")))
        codes = {(_text(p.get("code")), _text(p.get("message"))) for p in _rows(doc.get("problems"))}
        was_sources, was_status, was_codes = self._was
        own = [f"source {name}: {state}" if name not in was_sources
               else f"source {name}: {was_sources[name]} to {state}"
               for name, state in sources.items() if was_sources.get(name) != state]
        if status != was_status and status[0]:
            own.append(f"forecast: {status[0]}" + (f" ({status[1]})" if status[1] else ""))
        seen = {code for code, _message in was_codes}
        own += [f"{code}: {message}" for code, message in sorted(codes) if code not in seen]
        at = _number(doc.get("at"))
        self.rows.extend([at, doc.get("stamp") if isinstance(doc.get("stamp"), str) else None,
                          text[:_TEXT]] for text in own[:PER_STEP])
        self._was = (sources, status, codes)


# ---------------------------------------------------------------- the page
# Constant markup and script around the player's own (D199): nothing below is built from a
# result. The script is given the player's `el`, `add` and `clear`, so every text it shows
# goes through `textContent`.
_STYLE = """<style>
.stale { color: #c92a2a; font-weight: bold; }
.warn { color: #9c4a00; }
table.text th, table.text td { text-align: left; }
section { margin-top: 8px; font-size: 13px; }
#view { height: 45vh; }
</style>
"""
_TOP = """<p id="m-head"></p>
<p id="m-state" class="note"></p>
<div id="m-notes"></div>
"""
_BOTTOM = """<section><h2>Sources</h2><table id="m-sources" class="text"></table></section>
<section><h2>Problems</h2><table id="m-problems" class="text"></table></section>
<section><h2>Forecast</h2><div id="m-forecast"></div>
<table id="m-kpis"></table><table id="m-points"></table></section>
<section id="m-advice-box"><h2>Recommendations</h2><div id="m-advice"></div>
<details><summary>Basis, explanation and the candidates compared</summary>
<div id="m-advice-fold"></div><table id="m-advice-rows" class="text"></table></details></section>
<section><h2>Scores</h2><div id="m-scores"></div><table id="m-score-rows"></table></section>
<section id="m-counted-box"><h2>Counted by the plant</h2><p class="note" id="m-counted"></p>
<table id="m-counters"></table></section>
<section><h2>Events</h2><p class="note" id="m-more"></p>
<table id="m-events" class="text"></table></section>
"""
_SCRIPT = """/* The panel of a twin's monitor: it shows the summary and the event lines it is given
   and works out one thing, how old the result is now. */
function monitor(m, el, add, clear) {
  var s = m.summary, f = s.forecast, age = (Date.now() - Date.parse(s.written)) / 1000;
  var old = s.state.stale !== null && age > 2 * s.every;
  function rows(id, head, list, empty, cells) {
    var table = el(id), line;
    clear(table);
    line = add("tr", table);
    if (!list.length) { add("td", line, empty); return; }
    head.forEach(function (name) { add("th", line, name); });
    list.forEach(function (item) {
      line = add("tr", table);
      cells(item).forEach(function (cell) { add("td", line, cell); });
    });
  }
  ["bar", "all-h", "allnote", "all", "seed"].forEach(function (id) { el(id).style.display = "none"; });
  el("run-h").textContent = "The plant now";
  el("m-head").textContent = s.head;
  /* The sentence is the summary's; only the age is worked out here, by this clock. */
  if (old) { el("m-state").className = "stale"; }
  el("m-state").textContent = (old ? s.state.stale : s.state.fresh).replace(
    "{age}", isFinite(age) ? String(Math.round(age)) : "?") + " " + m.tail;
  clear(el("m-notes"));
  s.notes.forEach(function (note) { add("p", el("m-notes"), note, "warn"); });
  rows("m-sources", ["Source", "State", "Age (s)", "Lost", "Gaps"], s.sources,
       "No source has been read.", function (r) {
    return [r[0], r[1], fmt(r[2]), fmt(r[3]), fmt(r[4])];
  });
  rows("m-problems", ["Severity", "Code", "Problem", "Hint"], s.problems, "No problems.",
       function (r) { return r; });
  clear(el("m-forecast"));
  f.lines.forEach(function (line, index) { add("p", el("m-forecast"), line, index ? "note" : ""); });
  if (f.more) { add("p", el("m-forecast"), "+" + f.more + " more points not listed", "note"); }
  clear(el("m-kpis"));
  if (f.kpis.length) {
    rows("m-kpis", ["KPI at the end of the horizon", "Mean", "5%", "95%"], f.kpis, "", function (r) {
      return [r[0], fmt(r[1]), fmt(r[2]), fmt(r[3])];
    });
  }
  clear(el("m-points"));
  if (f.points.length) {
    rows("m-points", ["Series", "At", "Forecast mean", "5%", "95%", "Observed", "Inside the band"],
         f.points, "", function (r) {
      return [r[0], fmt(r[1]), fmt(r[2]), fmt(r[3]), fmt(r[4]), r[5] === null ? "" : fmt(r[5]), r[6]];
    });
  }
  /* Read-only: a page opened from disk can send nothing, so it cannot approve. */
  el("m-advice-box").style.display = s.recommend ? "" : "none";
  clear(el("m-advice"));
  clear(el("m-advice-fold"));
  clear(el("m-advice-rows"));
  if (s.recommend) {
    s.recommend.lead.forEach(function (line) { add("p", el("m-advice"), line, ""); });
    s.recommend.fold.forEach(function (line) { add("p", el("m-advice-fold"), line, "note"); });
    if (s.recommend.more) { add("p", el("m-advice-fold"), "+" + s.recommend.more + " more not listed", "note"); }
    if (s.recommend.rows.length) {
      rows("m-advice-rows", ["Rank", "Action", "Effect", "Low", "High", "Confidence", "Verdict",
                             "Status", "Id"], s.recommend.rows, "", function (r) {
        return [r[0] === null ? "" : fmt(r[0]), r[1], fmt(r[2]), fmt(r[3]), fmt(r[4]), fmt(r[5]),
                r[6], r[7], r[8]];
      });
    }
  }
  clear(el("m-scores"));
  s.scores.lines.forEach(function (line, index) { add("p", el("m-scores"), line, index ? "note" : ""); });
  if (s.scores.more) { add("p", el("m-scores"), "+" + s.scores.more + " more rows not listed", "note"); }
  clear(el("m-score-rows"));
  if (s.scores.rows.length) {
    rows("m-score-rows", ["Series", "Lead", "Readings", "Missed", "Not compared", "Coverage", "Bias",
                          "Abs. error", "Pinball loss"], s.scores.rows, "", function (r) {
      return [r[0], fmt(r[1]), fmt(r[2]), fmt(r[3]), fmt(r[4]), r[5], fmt(r[6]), fmt(r[7]), fmt(r[8])];
    });
  }
  el("m-counted-box").style.display = s.counters.length ? "" : "none";
  el("m-counted").textContent = s.counted;
  rows("m-counters", ["Block", "Counted", "Since", "Resets"], s.counters, "", function (r) {
    return [r[0], fmt(r[1]), fmt(r[2]), fmt(r[3])];
  });
  el("m-more").textContent = m.more ? "+" + m.more + " more not listed (newest first)" : "Newest first";
  rows("m-events", ["Time", "Instant", "Event"], m.events, "No events yet.", function (r) {
    return [fmt(r[0]), r[1] === null ? "" : r[1], r[2]];
  });
  el("foot").textContent = "The drawing is the plant as the twin read it at its last step, in " +
    "generic shapes. It does not move between two reloads.";
}
"""


class Page:
    """The monitor page of one twin (D278, D280): `write(doc, events)` at every step.

    It keeps its own live timeline of the events it is handed (rule 13: `Timeline` is the
    one reducer, `TapeWriter` the one frame), so the one frame it draws is the instant the
    result's `view` was read at. *every* is the twin's cadence in seconds (None for one
    stepped by a scheduler); the page reloads at half of it (D284)."""

    def __init__(self, layout, path, *, every: float | None = None, title: str | None = None):
        self.path = Path(path)
        self._refused: str | None = None        # what the last write was refused with
        self.refresh = 30 if not every else int(min(max(every / 2, 1), 30))
        self.shell = player._shell(
            _SCRIPT + player._SCRIPT,
            head=f'<meta http-equiv="refresh" content="{self.refresh}">\n' + _STYLE,
            top=_TOP, bottom=_BOTTOM)
        # The model's own name, or a constant: never a result's (it may be a file's, D282).
        info = layout.get("info") if isinstance(layout, dict) else None
        self._title = title or str((info or {}).get("name") or "") or "Plant monitor"
        self._writer = tape.TapeWriter(layout, tape.default_view(layout))
        self._timeline = Timeline([], live=True)
        self._log = Log()

    def build(self, doc, events) -> str:
        """The page of this step, as text."""
        events = list(events)
        self._timeline.append(events)
        self._log.add(doc, events)
        shown = summary(doc)
        name = self._title
        t = self._timeline.duration
        frame = self._writer.frame(self._timeline, t)
        data = {"title": name, "scenarios": [name], "kpis": None, "pics": [],
                "art": {"blocks": {}, "types": {}, "fleets": {}, "fills": {}},
                "runs": {name: {"scene": self._writer.scene([frame]), "t": [t], "frames": [frame],
                                "truncated": False, "events": 0}},
                "monitor": {"summary": shown, "events": [list(row) for row in reversed(self._log.rows)],
                            "more": self._log.more,
                            "tail": f"Twin started {shown['started']}. This page reloads every "
                                    f"{self.refresh} s."}}
        return self.shell.replace("__TAPE__", player._embed(data))

    def write(self, doc, events) -> str | None:
        """Replace the page with this step's, or leave it as it was. Returns what to say
        about the file when that CHANGED, else None: a refusal once, not at every step for
        as long as a reader holds the file, and once that it is written again."""
        from nagasim import twin        # late: `twin` starts this module from its command

        text, name = self.build(doc, events), self.path.name
        try:
            twin.write_text(self.path, text,
                            level=logging.WARNING if self._refused is None else logging.DEBUG)
        except OSError as exc:
            was, self._refused = self._refused, str(exc)
            return None if was == self._refused else (
                f"{name} could not be written ({exc}): a program may be holding it open (a "
                f"reader must open, read and close it), or the directory refuses. It is tried "
                f"again at every cycle and this is said once.")
        if self._refused is None:
            return None
        self._refused = None
        return f"{name} is written again"

