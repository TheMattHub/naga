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
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from nagasim import pagefmt, pagetext, player
from nagasim.core import plainlines, tape
from nagasim.core.playback import Timeline
from nagasim.core.timeunits import UNITS
from nagasim.core.utils import is_finite_number
from nagasim.live import stamp

__all__ = ["KEPT", "PER_STEP", "PAGE_FILE", "APPROXIMATE", "IN_DISTRIBUTION", "COUNTED", "SCORED",
           "SAME_SEED", "NO_SCORE", "MISSED", "ALERTED", "REFITTED", "ADVICE", "ASSUMES", "NO_ADVICE", "WRITES", "Log", "Page", "summary",
           "state", "action_text", "setpoint_text"]

KEPT = 200              # event lines of one twin
PER_STEP = 100          # of one step; the rest is counted
PAGE_FILE = "monitor.html"
_KPIS, _POINTS, _TEXT, _SCORES, _ADVICE, _ALERTS = 12, 60, 400, 300, 20, 100

# The sentences of the forecast, alerts, scores and event lines are the page table's
# `twin.*` texts (#1762): English here, the page's language on a page.
_EN = pagetext.table("en")
APPROXIMATE = _EN["twin.approximate"]
COUNTED = _EN["twin.counted"]
SCORED = _EN["twin.scored"]
MISSED = _EN["twin.missed_note"]
ALERTED = _EN["twin.alerted"]
REFITTED = ("A refit calibrates the factors of the twin's --refit file on the readings under a "
            "held alert, then runs the candidate beside each later forecast on the same seed. It "
            "is proposed only when its pinball loss on those later readings is below the current "
            "model's, and adopted only by a person's \"taken\" reply; \"ended\" reverts it. The "
            "model file is never written.")
SAME_SEED = _EN["twin.same_seed"]
# "old": a result of a twin older than 7.14 has no `scores` at all.
NO_SCORE = {reason: _EN[f"twin.no_score_{reason}"]
            for reason in ("not_observed", "no_forecast", "none_due", "old")}
IN_DISTRIBUTION = _EN["twin.in_distribution"]
ADVICE = ("What this model says each action would change, compared with doing nothing on the "
          "same random numbers. Effect: the mean difference, with its 95% interval; confidence: "
          "the share of replications in which the action did better. Only an action whose "
          "interval excludes \"no effect\" is recommended. Each candidate is judged alone: of "
          "candidates that change nothing, about 1 in 20 pass by chance, so of N of them about "
          "N/20. Evidence in distribution from the model, not a promise about the plant.")
ASSUMES = _EN["twin.assumes"]
# D812: who writes a setpoint, said once wherever one is offered.
WRITES = ("A setpoint is never written by the twin or this monitor. Approving one writes one "
          "reply line to your outbox; only `naga plc-write`, run by a person as its own "
          "process, writes it to the PLC on this machine, and only with --write.")
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
# The age of a result, by the clock of whoever looks: {age} is filled in when it is looked at
# (the sentences are the page table's `monitor.updated`, `monitor.stale`, ...: D1088).
# D576: whether a plant has dates is decided once per twin, from its mapping: an epoch of
# 1970-01-01 with a plant clock that reads near it is a log written in model time
# (`plant.log_of`, the starter mapping's epoch), and no date is shown however long it runs.
# A clock past 1980 on such an epoch is a real one (a live plant read in Unix seconds).
# ponytail: a model-time log over 10 years long would read as dated from then on.
_NO_DATE, _WALL = 366 * 86400, 10 * 365 * 86400
_INSIDE = {True: "yes", False: "no", None: ""}     # data the page's script reads, and words
_WHY = ("skipped", "refused", "failed")


# ---------------------------------------------------------------- reading a result
def _table(value) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value) -> list:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value) -> str:
    return "" if value is None else str(value)[:_TEXT]


def _number(value):
    return value if is_finite_number(value) else None


def _g(value) -> str:
    return "-" if _number(value) is None else f"{value:g}"


def _clock(value) -> str:
    """A plant time as a person reads it: two decimals at most (1.80952 -> 1.81)."""
    return "-" if _number(value) is None else f"{round(value, 2):g}"


def date_cell(full) -> list | None:
    """An event's date as a table cell: [the local clock (`pagetext.clock`, the date in front
    when not today), the stamp as written] for its title (#1962); a stamp nobody can read is
    shown as written; None for no date."""
    if not full:
        return None
    moment = stamp(str(full))
    return [str(full) if moment is None else pagetext.clock(moment), str(full)]


def _dated(doc: dict) -> bool:
    """Whether the plant of this result has dates (D576): from its epoch, else its clock."""
    model = _table(doc.get("model"))
    epoch = stamp(model.get("epoch")) if isinstance(model.get("epoch"), str) else None
    unit, at = UNITS.get(model.get("time_unit")), _number(doc.get("at"))
    if epoch is None:
        return False
    return epoch >= _NO_DATE or (unit is not None and at is not None and epoch + at * unit >= _WALL)


def _say(words: dict, key: str, **values) -> str:
    return pagetext.say(words, key, **values)


def _lost_line(words: dict, sources: list, written: str) -> str | None:
    """#1908: the one sentence for a lost connection, or None while every source answers. It is
    the page's largest text, so a loss of data is the first thing a reader sees. D1333: it says
    when the data was last received (the result's stamp less the oldest age), a fixed time."""
    down = [row for row in sources if row.get("state") == "down"]
    if not down:
        return None
    names = ", ".join(_text(row.get("name")) for row in down)
    ages = [_number(row.get("age")) for row in down]
    if any(age is None for age in ages):        # a source never read: no time is true
        return _say(words, "monitor.lost_never", names=names)
    now = stamp(written)
    now = time.time() if now is None else now
    # The date shows when the data is not from today (the reader's day, not the result's).
    return _say(words, "monitor.lost", names=names, at=pagetext.clock(now - max(ages)))


def _problem_cell(found: dict):
    """A problem's cell: its plain line, with the code and the engine's message as the title
    ([text, title], as the page's cells read); the table has no code column (D1334)."""
    code, message = _text(found.get("code")), _text(found.get("message"))
    return [_text(plainlines.plain(found)), f"{code}: {message}" if code else message]


def _n(text: str, words: dict) -> str:
    """A number's text (`_g`) with the language's decimal sign."""
    return pagetext.number(text, words)


def _worded(words: dict, area: str, value) -> str:
    """A value of a closed set (a source's state, a problem's severity, a forecast's status)
    in the table's language; one the table lacks as written. Display only."""
    text = _text(value)
    return words.get(area + text, text)


def _scores(doc: dict, unit: str, words: dict = _EN) -> dict:
    """The scores of a result as lines and rows: the basis always, or the reason for none."""
    scores = _table(doc.get("scores"))
    basis, rows = _table(scores.get("basis")), _rows(scores.get("rows"))
    if not rows:
        reason = _text(scores.get("reason")) if "scores" in doc else "old"
        return {"lines": [words[f"twin.no_score_{reason}"] if reason in NO_SCORE
                          else _say(words, "twin.no_score", reason=reason or words["twin.no_reason"])],
                "rows": [], "more": 0}
    first, last = _number(basis.get("first")), _number(basis.get("last"))
    counts = {key: _n(_g(basis.get(key)), words) for key in ("forecasts", "points", "missed", "lost")}
    lines = [_say(words, "twin.score_basis", since=(date_cell(_text(basis.get("since"))) or ["?"])[0], **counts)
             + ("" if first is None or last is None
                else _say(words, "twin.score_span", first=_n(f"{first:g}", words),
                          last=f"{_n(f'{last:g}', words)} {unit}".rstrip())) + ".", words["twin.scored"]]
    if _number(basis.get("missed")) or _number(basis.get("lost")):
        lines.append(words["twin.missed_note"])
    same = _number(basis.get("same_seed"))
    if same:
        lines.append(_say(words, "twin.same_seed", n=_n(f"{same:g}", words)))

    def share(row):
        value = _number(row.get("coverage"))
        return "" if value is None else f"{value:.0%}" + (" *" if row.get("whole") is True else "")
    return {"lines": lines, "more": max(len(rows) - _SCORES, 0),
            "rows": [[pagefmt.kpi_label(_text(r.get("series"))), _number(r.get("lead")), _number(r.get("points")),
                      _number(r.get("missed")), _number(r.get("lost")), share(r),
                      _number(r.get("bias")), _number(r.get("error")), _number(r.get("pinball")),
                      _text(r.get("series"))]
                     for r in rows[:_SCORES]]}


def _alerts(doc: dict, words: dict = _EN) -> dict | None:
    """The alerts of a result as lines: one an alerting side, then the basis and ALERTED.
    None for a result written before alerts existed."""
    if "alerts" not in doc:
        return None
    said = _table(doc.get("alerts"))
    basis, rows = _table(said.get("basis")), _rows(said.get("rows"))
    # An alert's message is the twin's own text, as a problem's is (English).
    lines = [_say(words, "twin.alert", message=_text(row.get("message"))) for row in rows[:_ALERTS]] or [
        _say(words, "twin.no_alert", k=_n(_g(said.get("k")), words), h=_n(_g(said.get("h")), words))]
    if len(rows) > _ALERTS:
        lines.append(_say(words, "twin.more_alerts", n=len(rows) - _ALERTS))
    lines += [_say(words, "twin.alert_basis", points=_n(_g(basis.get("points")), words),
                   since=(date_cell(_text(basis.get("since"))) or ["?"])[0], raised=_n(_g(basis.get("raised")), words))
              + (_say(words, "twin.alert_flat", flat=_n(_g(basis.get("flat")), words))
                 if _number(basis.get("flat")) else "") + ".", words["twin.alerted"]]
    return {"lines": lines, "alerting": min(len(rows), _ALERTS)}


def _overrides(value) -> str:
    return ", ".join(f"{_text(o.get('path'))[:120]} = {_g(o.get('value'))}" for o in _rows(value)[:8])


def _refit(doc: dict) -> list | None:
    """The refit of a result as lines (D740, D741): what it is doing, the candidate, the
    layer adopted, then REFITTED. None for a twin given no --refit file."""
    if "refit" not in doc:
        return None
    said = _table(doc.get("refit"))
    status, series = _text(said.get("status")), _text(said.get("series"))
    window, scores = _table(said.get("window")), _table(said.get("scores"))
    message = _text(said.get("message"))
    if status == "proposed":
        outbox = _text(said.get("outbox"))
        lines = [f"Proposed: {message}" + (f" (outbox: {outbox}, id {_text(said.get('id'))[:12]})"
                                          if outbox else "")]
    elif status == "not_better":
        lines = [f"Not proposed: {message}."]
    elif status == "waiting":
        lines = [f"Waiting: {_g(window.get('readings'))} of {_g(window.get('needed'))} readings of "
                 f"{series} under its alert since t = {_g(said.get('since'))} (a shorter window "
                 f"chases noise)."]
    elif status == "calibrating":
        factors = said.get("factors") if isinstance(said.get("factors"), list) else []
        lines = [f"Calibrating {len(factors)} factors on {_g(window.get('readings'))} readings of {series}."]
    elif status == "scoring":
        lines = [f"Scoring the candidate against the current model: {_g(scores.get('readings'))} "
                 f"of {_g(said.get('readings'))} readings of {series}."]
    elif status in ("failed", "unscored", "dropped"):
        lines = [f"No refit: it {'was ' if status == 'dropped' else ''}{status.replace('_', ' ')} "
                 f"({_text(said.get('reason')) or 'no reason was given'})."]
    else:
        lines = ["No refit: no alert on a refitted series has held long enough."]
    if said.get("candidate") and status in ("scoring", "proposed", "not_better"):
        lines.append(f"Candidate: {_overrides(said.get('candidate'))}.")
    adopted = _table(said.get("adopted"))
    lines.append(f"Adopted: refit {_text(adopted.get('id'))[:12]} since "
                 f"{_text(adopted.get('since')) or '?'}: {_overrides(adopted.get('overrides'))}; "
                 f"the score and the alerts started again then." if adopted
                 else "No refit is adopted: the forecasts are of the model file.")
    return lines + [REFITTED]


def setpoint_text(action, setpoints=None) -> str | None:
    """What approving a `setpoint` action would lead `naga plc-write` to write (D812): the
    tag, the value and the tag's range, step and interval from the caller's setpoints file
    (`setpoints.load(...)["tags"]`, or None when the caller gave none). None for any other
    action. The file's numbers only: nothing of a result selects a tag's limits."""
    said = _table(action)
    if said.get("verb") != "setpoint":
        return None
    tag = _text(said.get("target"))[:80]
    head = f"Setpoint {tag} = {_g(said.get('value'))}"
    target = said.get("target")
    # #1134: a result's target is data; only a string names a tag (a list is unhashable).
    spec = setpoints.get(target) if isinstance(setpoints, dict) and isinstance(target, str) else None
    if not isinstance(spec, dict):
        return head + (": its range, step and interval are in your setpoints file, which "
                       "this monitor was not given.")
    return (head + f": range {_g(spec.get('min'))} to {_g(spec.get('max'))}, at most "
            f"{_g(spec.get('max_step'))} from the value the PLC holds, at most once every "
            f"{_g(spec.get('min_interval'))} s.")


def action_text(value) -> str:
    """An action as a person reads it: `hold WO-1`, `resequence A before B`, `reroute s = f`."""
    said = _table(value)
    value = said.get("value")       # a capacity, a priority, a block or an order (D406, D486)
    head = f"{_text(said.get('verb'))} {_text(said.get('target'))[:80]}"
    if value is True:               # hold, release: the order is all there is to say
        return head
    if said.get("verb") == "resequence":
        return f"{head} before {_text(value)[:80]}"
    shown = _text(value)[:80] if isinstance(value, str) else _g(value)
    return f"{head} = {shown}"


def _recommend(doc: dict, setpoints=None) -> dict | None:
    """The recommendations of a result in two parts (D358), or None for a twin that was asked
    for none. `lead`: what stands now, the recommended action, the outbox -- always at least
    the outbox line. `fold`: the basis and the explanation (KPI line, ADVICE, effects). Rows:
    rank, action, effect, low, high, confidence, verdict, the record's status, the first
    characters of its id. No sink, no path, no reply text. A setpoint waiting for an
    answer gets a lead line of what it would write (*setpoints*, D812)."""
    if "recommend" not in doc:
        return None
    said = _table(doc.get("recommend"))
    box, items = _table(said.get("outbox")), _rows(said.get("items"))
    lead = []
    for standing in _rows(said.get("standing"))[:_ADVICE]:
        lead.append(f"Taken and standing in every forecast since {_text(standing.get('since'))}: "
                    f"{action_text(standing.get('action'))}.")
    for ended in _rows(said.get("ended"))[:_ADVICE]:
        lead.append(f"Ended {_text(ended.get('ended'))}: {action_text(ended.get('action'))} (taken "
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
                f"Recommended: {action_text(top.get('action'))}, effect {_g(top.get('effect'))} "
                f"(95% interval {_g(top.get('low'))} to {_g(top.get('high'))})"
                + (f"; {len(better) - 1} more in the table" if len(better) > 1 else "") + ".")
        offered = [setpoint_text(i.get("action"), setpoints) for i in better[:_ADVICE]]
        if any(offered):
            lead += [text for text in offered if text] + [WRITES]
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
        line = f"Effect of {action_text(effect.get('action'))}: {_EFFECT.get(status, status)}"
        if status == "measured":
            line += (f", {_g(effect.get('observed'))} observed against a band of "
                     f"{_g(effect.get('q05'))} to {_g(effect.get('q95'))} ("
                     + ("inside" if effect.get("inside_interval") is True else "outside") + ")")
        fold.append(line + ".")
    if said.get("measurable") is False:
        fold.append("The effect of an action taken cannot be measured on this KPI: a plant "
                    "shows only what a counted block let out.")
    return {"lead": lead, "fold": fold, "more": max(len(items) - _ADVICE, 0),
            "rows": [[_number(i.get("rank")), action_text(i.get("action")), _number(i.get("effect")),
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


def summary(doc, setpoints=None, language: str | None = None) -> dict:
    """What a monitor shows of one result, as plain data. Read defensively: the document may
    come from a file, and is then only known to be format 1. *setpoints*: the caller's
    setpoints file, loaded (`setpoints.load(...)["tags"]`), for what a setpoint would write.
    *language* (a page's, `pagetext.LANGUAGES`) words the headline, the age sentences, the
    forecast, alerts and scores, a problem's severity and a source's state (D1088, #1762);
    the recommendations and the refit stay English, as do the twin's and the engine's own
    messages (a problem's, an alert's, a refusal's reason)."""
    words = pagetext.table(language)
    doc = _table(doc)
    model, view, forecast = _table(doc.get("model")), _table(doc.get("view")), _table(doc.get("forecast"))
    unit = pagetext.unit(_text(model.get("time_unit")), words)
    problems = _rows(doc.get("problems"))
    # The plant's date as a local clock, like the lost line beside it (D1333); its own stamp
    # is the head's title. A stamp nobody can read is shown as written.
    full = _text(doc.get("stamp")) if _dated(doc) else ""
    moment = stamp(full) if full else None
    date = (full if moment is None else pagetext.clock(moment)) if full else words["monitor.no_date"]
    head = (f"{words['monitor.plant_time']} {pagetext.number(_clock(doc.get('at')), words)} {unit}".rstrip()
            + (f" ({date})" if date else "") + f"; {words['monitor.cycle']} {_text(doc.get('cycle'))}")
    status, reason = _text(forecast.get("status")) or "none", _text(forecast.get("reason"))
    record = isinstance(forecast.get("state"), str)
    lines = [_say(words, "twin.status", status=_worded(words, "twin.status_", status))]
    if status == "none":
        lines.append(words["twin.no_forecast_yet"])
    elif status == "running":
        lines.append(words["twin.running"])
    elif status in _WHY:
        why = [_text(plainlines.plain(p)) for p in problems
               if reason and p.get("code") == reason] or [reason or words["twin.no_reason"]]
        lead = words["twin.limit" if reason == "E_TWIN_STATE" else f"twin.why_{status}"]
        lines += [lead + text for text in why]
    if record:
        def span(key):          # "40 seconds"; for a model with no unit, the number alone
            return f"{_n(_g(forecast.get(key)), words)} {unit}".rstrip()
        lines.append(
            ("" if status == "ok" else words["twin.shown_last"])
            + _say(words, "twin.from", t=span("from"))
            + (f" ({date_cell(_text(forecast.get('stamp')))[0]})" if forecast.get("stamp") and _dated(doc) else "")
            + _say(words, "twin.over", n=_n(_g(forecast.get("replications")), words), horizon=span("horizon"))
            + _say(words, "twin.seed", seed=_n(_g(forecast.get("seed")), words),
                   state=_text(forecast.get("state"))[:12]))
    assumed = forecast.get("actions") if record else None
    if isinstance(assumed, list) and assumed:       # D307: said wherever a forecast is
        lines.append(_say(words, "twin.assumes", n=len(assumed)))
    points = _rows(forecast.get("points")) if record else []
    compared = _rows(doc.get("comparison"))
    if compared:
        lines.append(_say(words, "twin.compared", n=len(compared),
                          inside=sum(1 for row in compared if row.get("inside") is True)))
    if points or compared:
        lines.append(words["twin.in_distribution"])

    def inside(point):
        low, high, seen = (_number(point.get(key)) for key in ("q05", "q95", "observed"))
        if point.get("missed"):
            return "missed"
        return _INSIDE[None if None in (low, high, seen) else low <= seen <= high]

    written = _text(doc.get("written"))
    every = _number(_table(doc.get("cadence")).get("every"))
    every = every if every and every > 0 else None
    shown_every = pagetext.number(f"{every:g}", words) if every is not None else ""
    fresh = words["monitor.updated"] + (
        words["monitor.scheduled"] if every is None
        else pagetext.say(words, "monitor.watches", every=shown_every))
    return {
        "head": head, "stamp": full, "written": written, "started": _text(doc.get("started")), "every": every,
        "state": {"fresh": fresh, "stale": None if every is None
                  else pagetext.say(words, "monitor.stale", every=shown_every)},
        "notes": [words["twin.approximate"]] if view.get("approximate") or forecast.get("approximate") else [],
        "lost": _lost_line(words, _rows(doc.get("sources")), written),
        "sources": [[_text(row.get("name")), _worded(words, "twin.state_", row.get("state")),
                     _number(row.get("age")),
                     _number(row.get("lost")), _number(row.get("gaps"))]
                    for row in _rows(doc.get("sources"))],
        # D1332: the code's plain line, with the engine's message as the cell's title.
        "problems": [[_worded(words, "twin.severity_", p.get("severity")), _problem_cell(p),
                      _text(p.get("hint"))] for p in problems],
        # What the plant counted (D288): block, total, since (model time), resets.
        "counters": [[_text(name), _number(said.get("total")), _number(said.get("since")),
                      _number(said.get("resets"))]
                     for name, said in list(_table(view.get("counters")).items())[:_POINTS]
                     if isinstance(said, dict)],
        "counted": words["twin.counted"],
        "scores": _scores(doc, unit, words),
        "alerts": _alerts(doc, words),
        "refit": _refit(doc),
        "recommend": _recommend(doc, setpoints),
        "forecast": {
            "status": status, "lines": lines,
            # How to read the lines (the last of them, when there is evidence): a page shows it
            # on hover over the line before it (D1331).
            "about": words["twin.in_distribution"] if points or compared else "",
            # S4-09: a row reads by its label (`pagefmt.kpi_label`), its key the cell's title; the
            # bookkeeping rows (the state the run started from, a cost of zero) are left out.
            "kpis": [[pagefmt.kpi_label(_text(k.get("kpi"))), _number(k.get("mean")), _number(k.get("q05")),
                      _number(k.get("q95")), _text(k.get("kpi"))] for k in _rows(forecast.get("kpis"))
                     if not pagefmt.is_bookkeeping(_text(k.get("kpi")),
                                                   [k.get(q) for q in ("mean", "q05", "q95")])][:_KPIS]
            if record else [],
            "points": [[pagefmt.kpi_label(_text(p.get("series"))), _number(p.get("at")), _number(p.get("mean")),
                        _number(p.get("q05")), _number(p.get("q95")), _number(p.get("observed")),
                        inside(p), _text(p.get("series"))] for p in points[:_POINTS]],
            "more": max(len(points) - _POINTS, 0)}}


# ---------------------------------------------------------------- event lines
def _item(event, words: dict) -> str:
    # The type of an entity nothing gives one, in a trace and in a plant mapping.
    return words["twin.item"] if event.e_type in ("", "Unknown", "DEFAULT", None) else str(event.e_type)


def _said(event, words: dict = _EN) -> str | None:
    """One trace event as an operator reads it, or None for what the drawing carries."""
    kind, item = event.kind, _item(event, words)
    if kind == "entity_created":
        return _say(words, "twin.entered", item=item, block=event.block)
    if kind == "entity_moved":
        if not event.block:
            return _say(words, "twin.back", item=item, target=event.target)
        # Inside one asset (`pick_a/waited` to `pick_a/pick`) its steps; between two, the assets.
        here, _, step = str(event.block).rpartition("/")
        there, _, then = str(event.target).rpartition("/")
        if here and here == there:
            return _say(words, "twin.within", item=item, here=here, step=step, then=then)
        return _say(words, "twin.moved", item=item, here=here or step, there=there or then)
    if kind == "entity_disposed":
        return _say(words, "twin.left", item=item, block=event.block)
    if kind == "resource_state":
        if event.state in ("down", "up"):
            return _say(words, f"twin.{event.state}", resource=event.resource)
        if event.state == "capacity":
            return _say(words, "twin.capacity", resource=event.resource, value=_n(f"{event.value:g}", words))
        if event.state == "blocked":
            return _say(words, "twin.blocked" if event.value else "twin.unblocked", resource=event.resource)
    return None


def _folded(events) -> list:
    """The events with each item's moves at one instant made one: from where it was to
    where it came to (`pick_a/at_pick` ... `pick_a/pick` is one step of pick_a)."""
    out, open_at = [], {}
    for event in events:
        key = (event.t, event.entity_id)
        if event.kind == "entity_moved" and event.block:
            if key in open_at:
                out[open_at[key]] = replace(out[open_at[key]], target=event.target)
                continue
            open_at[key] = len(out)
        else:
            open_at.pop(key, None)
        out.append(event)
    return out


class Log:
    """The event lines of one twin, oldest first: `rows` is `[t, stamp or None, text]`, at
    most `KEPT` (a problem's text is `[plain line, "CODE: message"]`, a cell and its title); `more` counts what a step had over `PER_STEP`."""

    def __init__(self, language: str | None = None):
        self._words = pagetext.table(language)  # the lines' language (#1762); English by default
        self.rows: deque = deque(maxlen=KEPT)
        self.more = 0
        self._was: tuple = ({}, None, set())
        self.dated: bool | None = None          # decided at the first result (D576)

    def add(self, doc, events) -> None:
        doc = _table(doc)
        model = _table(doc.get("model"))
        epoch, unit = stamp(model.get("epoch")), UNITS.get(model.get("time_unit"))
        if self.dated is None:
            self.dated = _dated(doc)

        def instant(t):
            if not self.dated:
                return None
            try:
                return datetime.fromtimestamp(epoch + t * unit, timezone.utc).isoformat(
                    timespec="seconds")
            except (TypeError, OverflowError, OSError, ValueError):
                return None

        # One line per thing that happened at an instant: an item's steps through the model
        # in no time are one move, and the same line N times is said once.
        counted: dict = {}
        for event in _folded(events):
            if (text := _said(event, self._words)) is not None:
                counted[(event.t, text)] = counted.get((event.t, text), 0) + 1
        said = [(t, text if n == 1 else f"{n} x {text}") for (t, text), n in counted.items()]
        self.more += max(len(said) - PER_STEP, 0)
        self.rows.extend([t, instant(t), text[:_TEXT]] for t, text in said[-PER_STEP:])
        # What changed about the twin itself since the step before.
        sources = {_text(row.get("name")): _text(row.get("state")) for row in _rows(doc.get("sources"))}
        forecast = _table(doc.get("forecast"))
        status = (_text(forecast.get("status")), _text(forecast.get("reason")))
        problems = {(_text(p.get("code")), _text(p.get("message"))): p for p in _rows(doc.get("problems"))}
        codes = set(problems)
        was_sources, was_status, was_codes = self._was
        words = self._words

        def shown(state):
            return _worded(words, "twin.state_", state)
        own = [_say(words, "twin.source", name=name, state=shown(state)) if name not in was_sources
               else _say(words, "twin.source_changed", name=name, was=shown(was_sources[name]), state=shown(state))
               for name, state in sources.items() if was_sources.get(name) != state]
        if status != was_status and status[0]:
            said = _say(words, "twin.forecast_event", status=_worded(words, "twin.status_", status[0]))
            # D1334: the reason's code is the line's title, not its text.
            own.append([said, f"{said} ({status[1]})"] if status[1] else said)
        seen = {code for code, _message in was_codes}
        # D1332: a problem reads as its code's plain line, "CODE: message" its title.
        own += [[_text(plainlines.plain(problems[key])), f"{key[0]}: {key[1]}"]
                for key in sorted(codes) if key[0] not in seen]
        at = _number(doc.get("at"))
        self.rows.extend([at, _text(doc.get("stamp")) or None if self.dated else None,
                          [text[0][:_TEXT], text[1][:_TEXT]] if isinstance(text, list) else text[:_TEXT]]
                         for text in own[:PER_STEP])
        self._was = (sources, status, codes)


# ---------------------------------------------------------------- the page
# Constant markup and script around the player's own (D199): nothing below is built from a
# result. The script is given the player's `el`, `add` and `clear`, so every text it shows
# goes through `textContent`.
# Its own rules, after the player's in the one style sheet the policy pins.
_STYLE = """
/* Text, so 4.5:1: in light a darker step of --down (5.5:1 on the page, dataviz contrast()); dark passes (5.3). */
.stale, #m-lost { color: color-mix(in srgb, var(--down) 85%, black); font-weight: bold; }
@media (prefers-color-scheme: dark) { :root:not([data-theme=light]) .stale, :root:not([data-theme=light]) #m-lost { color: var(--down); } }
:root[data-theme=dark] .stale, :root[data-theme=dark] #m-lost { color: var(--down); }
/* bug 1908: a lost connection is the loudest thing on the page, and what it left behind looks old. */
#m-lost { font-size: 18px; margin: 0 0 8px; padding: 8px 12px; border: 2px solid var(--down);
          border-radius: var(--radius); }
body:has(#m-lost:not([hidden])) :is(.centre, .side > *, #m-forecast-box, #m-advice-box) { opacity: 0.6; filter: grayscale(1); }
.warn { color: var(--down); }
/* A cell wraps between words only (bug 1761): a word is never split. */
table.text th, table.text td { text-align: left; overflow-wrap: normal; hyphens: manual; }
#m-events.dated td:nth-child(2) { white-space: nowrap; }     /* a date is not broken at its "-" */
/* A wide table scrolls inside the page, so 360 px has no sideways page scroll (#1747). */
section table { display: block; max-width: 100%; overflow-x: auto; }
summary { cursor: pointer; font-weight: bold; }
summary h2 { display: inline; }
section { margin-top: 8px; font-size: 13px; }
#view { height: 45vh; }
/* bug 1905: one column reads now, next, what to do, then the rest. The console's boxes join the page's
   own column (display: contents) so the forecast and the advice can come before the player's side
   panels. */
@media (max-width: 900px) {
  body { display: flex; flex-direction: column; gap: 12px; }
  main.console, .side { display: contents; }
  .centre { order: 1; }
  #m-forecast-box { order: 2; }
  #m-advice-box { order: 3; }
  .side > * { order: 4; }
  body > section { order: 6; }
  #m-forecast-box, #m-advice-box { margin-top: 0; }
  #view { height: 30vh; }
}
"""
_TOP = """<p id="m-lost" role="alert" hidden></p>
<p id="m-head"></p>
<p id="m-state" class="note"></p>
<div id="m-notes"></div>
"""
# The headings are {{keys}} of the page table (`pagetext.markup`, D1088).
_BOTTOM = """<section><h2>{{monitor.sources}}</h2><table id="m-sources" class="text"></table></section>
<section><h2>{{monitor.problems}}</h2><table id="m-problems" class="text"></table></section>
<section id="m-forecast-box"><h2>{{monitor.forecast}}</h2><div id="m-forecast"></div>
<details id="d-m-kpis"><summary>{{monitor.col_kpi_end}}</summary><table id="m-kpis"></table></details>
<details id="d-m-points"><summary>{{monitor.col_series}}</summary><table id="m-points"></table></details></section>
<section id="m-advice-box"><h2>{{monitor.advice}}</h2><div id="m-advice"></div>
<details><summary>{{monitor.advice_fold}}</summary>
<div id="m-advice-fold"></div><table id="m-advice-rows" class="text"></table></details></section>
<section id="m-alerts-box"><h2>{{monitor.alerts}}</h2><div id="m-alerts"></div></section>
<section id="m-refit-box"><h2>{{monitor.refit}}</h2><div id="m-refit"></div></section>
<section><h2>{{monitor.scores}}</h2><div id="m-scores"></div><table id="m-score-rows"></table></section>
<section id="m-counted-box"><h2>{{monitor.counted}}</h2><p class="note" id="m-counted"></p>
<table id="m-counters"></table></section>
<section><details id="d-m-events"><summary><h2>{{monitor.events}}</h2></summary><p class="note" id="m-more"></p>
<table id="m-events" class="text"></table></details></section>
"""
_SCRIPT = """/* The panel of a twin's monitor: it shows the summary and the event lines it is given
   and works out one thing, how old the result is now. */
function monitor(m, el, add, clear) {
  var s = m.summary, f = s.forecast, age = (Date.now() - Date.parse(s.written)) / 1000;
  var old = s.state.stale !== null && age > 2 * s.every;
  /* D1331: what stays on the page is the first sentence (or clause before a ";"); the whole
     text is the line's title, read on hover. */
  function brief(box, text) {
    var cut = text.search(/[.;] (?=\\S)/);
    box.textContent = cut > 0 ? text.slice(0, cut + 1).replace(/;$/, "") : text;
    box.title = text;
    return box;
  }
  function say(box, text, cls) { return brief(add("p", box, "", cls), text); }
  function rows(id, head, list, empty, cells) {
    var table = el(id), line;
    clear(table);
    line = add("tr", table);
    if (!list.length) { add("td", line, empty); return; }
    head.forEach(function (name) { add("th", line, name); });
    list.forEach(function (item) {
      line = add("tr", table);
      /* A cell [text, title] reads as its label with the key in its title (S4-09). */
      cells(item).forEach(function (cell) {
        if (Array.isArray(cell)) { add("td", line, cell[0]).title = cell[1]; } else { add("td", line, cell); }
      });
    });
  }
  /* A fold the reader set stays set across the page's own reloads; else it is open only when
     something is wrong. A private window can refuse the storage: then the default stands. */
  function fold(id, open) {
    var box = el(id), kept = null;
    try { kept = sessionStorage.getItem("fold:" + id); } catch (e) { kept = null; }
    box.open = kept === null ? open : kept === "1";
    box.dataset.set = box.open ? "1" : "0";
    box.ontoggle = function () {
      if (String(+box.open) === box.dataset.set) { return; }      /* our own change, not the reader's */
      box.dataset.set = String(+box.open);
      try { sessionStorage.setItem("fold:" + id, box.dataset.set); } catch (e) { /* storage refused: the fold is per load */ }
    };
  }
  ["bar", "seek", "all-box", "seed", "lanes-box"].forEach(function (id) { el(id).style.display = "none"; });
  el("run-h").textContent = tr("monitor.now");
  el("run-box").open = true;           /* the present is what this page is for */
  el("m-head").textContent = s.head;
  el("m-head").title = s.stamp;
  el("m-lost").hidden = !s.lost;
  el("m-lost").textContent = s.lost || "";
  el("d-m-kpis").style.display = f.kpis.length ? "" : "none";
  el("d-m-points").style.display = f.points.length ? "" : "none";
  fold("d-m-kpis", false);
  fold("d-m-points", f.points.some(function (r) { return r[6] === "no" || r[6] === "missed"; }));
  fold("d-m-events", s.problems.length > 0 || !!(s.alerts && s.alerts.alerting > 0));
  /* The sentence is the summary's; only the age is worked out here, by this clock. */
  if (old) { el("m-state").className = "stale"; }
  brief(el("m-state"), (old ? s.state.stale : s.state.fresh).replace(
    "{age}", isFinite(age) ? String(Math.round(age)) : "?") + " " + m.tail);
  clear(el("m-notes"));
  s.notes.forEach(function (note) { say(el("m-notes"), note, "warn"); });
  rows("m-sources", [tr("monitor.col_source"), tr("monitor.col_state"), tr("monitor.col_age"),
                      tr("monitor.col_lost"), tr("monitor.col_gaps")], s.sources,
       tr("monitor.no_sources"), function (r) {
    return [r[0], r[1], fmt(r[2]), fmt(r[3]), fmt(r[4])];
  });
  rows("m-problems", [tr("monitor.col_severity"), tr("monitor.col_problem"),
                      tr("monitor.col_hint")], s.problems, tr("monitor.no_problems"),
       function (r) { return r; });
  clear(el("m-forecast"));
  var said = null;
  f.lines.forEach(function (line, index) {
    if (said && line === f.about) { said.title += "\\n" + line; return; }
    said = say(el("m-forecast"), line, index ? "note" : "");
  });
  if (f.more) { add("p", el("m-forecast"), tr("monitor.more_points", {n: f.more}), "note"); }
  clear(el("m-kpis"));
  if (f.kpis.length) {
    rows("m-kpis", [tr("monitor.col_kpi_end"), tr("monitor.col_mean"), "5%", "95%"], f.kpis, "", function (r) {
      return [[r[0], r[4]], fmt(r[1]), fmt(r[2]), fmt(r[3])];
    });
  }
  clear(el("m-points"));
  if (f.points.length) {
    rows("m-points", [tr("monitor.col_series"), tr("monitor.col_at"), tr("monitor.col_forecast_mean"),
                      "5%", "95%", tr("monitor.col_observed"), tr("monitor.col_inside")],
         f.points, "", function (r) {
      return [[r[0], r[7]], fmt(r[1]), fmt(r[2]), fmt(r[3]), fmt(r[4]), r[5] === null ? "" : fmt(r[5]),
              r[6] ? tr("monitor.inside_" + r[6]) : ""];
    });
  }
  /* Read-only: a page opened from disk can send nothing, so it cannot approve. */
  el("m-advice-box").style.display = s.recommend ? "" : "none";
  clear(el("m-advice"));
  clear(el("m-advice-fold"));
  clear(el("m-advice-rows"));
  if (s.recommend) {
    s.recommend.lead.forEach(function (line) { say(el("m-advice"), line, ""); });     /* #1964 */
    s.recommend.fold.forEach(function (line) { add("p", el("m-advice-fold"), line, "note"); });
    if (s.recommend.more) { add("p", el("m-advice-fold"), tr("monitor.more_listed", {n: s.recommend.more}), "note"); }
    if (s.recommend.rows.length) {
      rows("m-advice-rows", [tr("monitor.col_rank"), tr("monitor.col_action"), tr("monitor.col_effect"),
                             tr("monitor.col_low"), tr("monitor.col_high"), tr("monitor.col_confidence"),
                             tr("monitor.col_verdict"), tr("monitor.col_status"), "Id"],
                             s.recommend.rows, "", function (r) {
        return [r[0] === null ? "" : fmt(r[0]), r[1], fmt(r[2]), fmt(r[3]), fmt(r[4]), fmt(r[5]),
                r[6], r[7], r[8]];
      });
    }
  }
  el("m-alerts-box").style.display = s.alerts ? "" : "none";
  clear(el("m-alerts"));
  if (s.alerts) {
    /* The last line says what an alert is: the title of the basis line before it. */
    var about = s.alerts.lines[s.alerts.lines.length - 1], last = null;
    s.alerts.lines.slice(0, -1).forEach(function (line, index) {
      last = say(el("m-alerts"), line, index < s.alerts.alerting ? "warn" : "note");
    });
    if (last) { last.title += "\\n" + about; }
  }
  el("m-refit-box").style.display = s.refit ? "" : "none";
  clear(el("m-refit"));
  if (s.refit) {
    s.refit.forEach(function (line, index) {
      add("p", el("m-refit"), line, index === s.refit.length - 1 ? "note" : "");
    });
  }
  clear(el("m-scores"));
  s.scores.lines.forEach(function (line, index) { say(el("m-scores"), line, index ? "note" : ""); });
  if (s.scores.more) { add("p", el("m-scores"), tr("monitor.more_rows", {n: s.scores.more}), "note"); }
  clear(el("m-score-rows"));
  if (s.scores.rows.length) {
    rows("m-score-rows", [tr("monitor.col_series"), tr("monitor.col_lead"), tr("monitor.col_readings"),
                          tr("monitor.col_missed"), tr("monitor.col_not_compared"), tr("monitor.col_coverage"),
                          "Bias", tr("monitor.col_abs_error"), "Pinball loss"], s.scores.rows, "", function (r) {
      return [[r[0], r[9]], fmt(r[1]), fmt(r[2]), fmt(r[3]), fmt(r[4]), r[5], fmt(r[6]), fmt(r[7]), fmt(r[8])];
    });
  }
  el("m-counted-box").style.display = s.counters.length ? "" : "none";
  brief(el("m-counted"), s.counted);
  rows("m-counters", [tr("monitor.col_block"), tr("monitor.col_counted"), tr("monitor.col_since"),
                       tr("monitor.col_resets")], s.counters, "", function (r) {
    return [r[0], fmt(r[1]), fmt(r[2]), fmt(r[3])];
  });
  el("m-more").textContent = m.more ? tr("monitor.more_events", {n: m.more}) : tr("monitor.newest");
  /* A recorded log without dates has no Date column (D20 of the review). */
  var dated = m.events.some(function (r) { return r[1] !== null; });
  el("m-events").className = dated ? "text dated" : "text";
  rows("m-events", dated ? [tr("monitor.plant_time"), tr("monitor.col_date"), tr("monitor.col_event")]
                         : [tr("monitor.plant_time"), tr("monitor.col_event")], m.events,
       tr("monitor.no_events"), function (r) {
    var t = typeof r[0] === "number" ? String(Math.round(r[0] * 100) / 100) : "";
    return dated ? [t, r[1] === null ? "" : r[1], r[2]] : [t, r[2]];
  });
  brief(el("foot"), tr("monitor.foot"));
}
"""


class Page:
    """The monitor page of one twin (D278, D280): `write(doc, events)` at every step.

    It keeps its own live timeline of the events it is handed (rule 13: `Timeline` is the
    one reducer, `TapeWriter` the one frame), so the one frame it draws is the instant the
    result's `view` was read at. *every* is the twin's cadence in seconds (None for one
    stepped by a scheduler); the page reloads at half of it (D284)."""

    def __init__(self, layout, path, *, every: float | None = None, title: str | None = None,
                 setpoints=None, language: str | None = None):
        self.path = Path(path)
        self.setpoints = setpoints              # the caller's, loaded: what a setpoint writes
        self.language = language                # the page's (`pagetext.LANGUAGES`, D1088)
        self._words = pagetext.table(language)  # a bad language is refused here
        self._refused: str | None = None        # what the last write was refused with
        self.refresh = 30 if not every else int(min(max(every / 2, 1), 30))
        self.shell = player.page_shell(
            _SCRIPT + player.SCRIPT,
            head=f'<meta http-equiv="refresh" content="{self.refresh}">\n',
            top=_TOP, bottom=_BOTTOM, rules=player.CSS + _STYLE, language=language,
            areas=("monitor.",))
        # The model's own name, or a constant: never a result's (it may be a file's, D282).
        info = layout.get("info") if isinstance(layout, dict) else None
        self._title = title or str((info or {}).get("name") or "") or self._words["monitor.title"]
        self._writer = tape.TapeWriter(layout, tape.default_view(layout))
        self._timeline = Timeline([], live=True)
        self._log = Log(language)

    def build(self, doc, events) -> str:
        """The page of this step, as text."""
        events = list(events)
        self._timeline.append(events)
        self._log.add(doc, events)
        shown = summary(doc, self.setpoints, self.language)
        name = self._title
        t = self._timeline.duration
        frame = self._writer.frame(self._timeline, t)
        data = {"title": name, "scenarios": [name], "kpis": None, "pics": [],
                "art": {"blocks": {}, "types": {}, "fleets": {}, "fills": {}},
                "runs": {name: {"scene": self._writer.scene([frame]), "t": [t], "frames": [frame],
                                "truncated": False, "events": 0}},
                "monitor": {"summary": shown, "events": [[t, date_cell(at), text]                  # #1962
                                       for t, at, text in reversed(self._log.rows)],
                            "more": self._log.more,
                            "tail": pagetext.say(self._words, "monitor.tail",
                                                 started=shown["started"], n=self.refresh)}}
        return self.shell.replace("__TAPE__", player.embed(data))

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

