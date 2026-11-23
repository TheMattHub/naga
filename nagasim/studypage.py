"""The control room: one offline HTML page for a study, built from the study folder alone.

Same no-script pattern as the player: a Content-Security-Policy with `default-src 'none'` and
no external address. Flowcharts are re-rendered from each version's `model.json`, never read
from a stored SVG, so a hand-edited folder cannot inject markup. Every study string goes
through `esc`. The page embeds `study.json` as data so the page alone can restart a study."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from nagasim import flowchart, laws, pagefmt, player, study as study_mod
from nagasim.report import csp, esc
from nagasim.studychecks import goal_kpi


# #1883: at phone width a table is a stack of rows, each cell headed by its column's name (the
# `data-label` `_table` writes), so no word is split and nothing is wider than its box. The header
# row is kept (clipped to no height) for a screen reader instead of being display:none.
STACK_CSS = (
    "@media (max-width:700px){table,tbody,tr,th,td{display:block;width:auto!important}"
    "tr.h{height:0;overflow:hidden;padding:0;border:0}"
    "tr+tr{border-bottom:1px solid var(--line);padding:4px 0}"
    "td,th{border-bottom:0!important;padding:2px 6px!important}"
    "td[data-label]:before{content:attr(data-label);display:block;font-size:12px;color:var(--muted)}}")

# D1240: the reader's light or dark (the player's tokens, `player.token_css`); print stays light.
# A flowchart is a drawing in fixed colours, so it sits on its own light card in either theme.
_CSS = player.token_css() + """
body{font:14px/1.4 var(--font);color:var(--ink);background:var(--page);margin:0}
main{max-width:1100px;margin:0 auto;padding:0 16px 16px}
header{position:sticky;top:0;z-index:2;background:var(--page);max-height:40vh;overflow:auto;
 padding:8px 0;border-bottom:1px solid var(--line)}
header p{margin:2px 0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:8px 0}
.panel>summary{padding:6px 14px;font-weight:600;cursor:pointer}
.scroll{padding:0 14px 8px;overflow-x:auto}
h1{font-size:18px;margin:2px 0}h3{font-size:14px;margin:0}
.lead{font-size:15px;font-weight:600;margin:6px 0}.panel>h2{font-size:15px;margin:0;padding:6px 14px}
.banner{padding:4px 10px;border-radius:6px;font-weight:600;margin:4px 0}
.b-ok{background:var(--ok-bg);color:var(--ok-ink)}.b-wait{background:var(--wait-bg);color:var(--wait-ink)}
.b-open{background:var(--open-bg);color:var(--open-ink)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}
.chip{display:inline-flex;align-items:center;gap:6px;border:2px solid var(--dim);border-radius:14px;
 padding:1px 10px;background:var(--panel)}
.chip.met{border-color:var(--run)}.chip.not_met{border-color:var(--down)}
.spark{vertical-align:middle}.spark polyline{stroke:var(--ink)}.spark circle{fill:var(--ink)}
table{border-collapse:collapse;width:100%;margin:6px 0}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.badge{font-size:12px}
.muted{color:var(--muted);font-size:13px}.flow{overflow-x:auto;background:#fff;border-radius:6px}
.flow svg{max-width:100%;height:auto;max-height:200px}
summary{cursor:pointer}
.naga-bar{padding:4px 0}.naga-bar button{margin-right:6px}
.naga-err{color:var(--down);font-weight:600;margin-left:8px}
#actions input{width:40%;padding:4px 8px;margin:0 8px}
button,input{font:inherit;color:inherit}
button{cursor:pointer;padding:3px 10px;margin:2px 4px 2px 0;background:var(--raised);border:1px solid var(--line);
 border-radius:6px}button:disabled{cursor:default}
img.naga-flow{display:block;max-width:100%;margin:6px 0;background:#fff}
@media (max-width:700px){button,input,summary{min-height:40px}summary{padding-block:10px}
 header{position:static;max-height:none}}
@media print{body{background:#fff}header{position:static;max-height:none;overflow:visible}
 details{display:block}
 details>*:not(summary){display:block!important;content-visibility:visible!important}
 .version{break-inside:avoid}}
""" + STACK_CSS
# The study page's one style block. The room shell frames the page with srcdoc, which inherits the shell's
# policy as well as the page's own, so the shell admits this hash too (#1757). The naga-* and button rules
# style the controls the shell adds into the framed page: no other sheet reaches them there.
PAGE_CSS = _CSS


def _badge(verdict: str) -> str:
    return f'<span class="badge {esc(verdict)}">{esc(str(verdict).replace("_", " "))}</span>'


def _ascii(text: str) -> str:
    """Non-ASCII text becomes a character reference: the page stays ASCII and renders the same."""
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _page(title: str, css: str, body: str) -> str:
    """One HTML document, no script allowed by the CSP; `body` holds ready-made (escaped) markup."""
    return _ascii("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                  f'<meta http-equiv="Content-Security-Policy" content="{csp(css)}">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1">'
                  f"<title>{esc(title[:80])}</title><style>{css}</style></head><body>{body}</body></html>\n")


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Rows hold ready-made (already escaped) cells."""
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f'<td data-label="{esc(h)}">{c}</td>' for h, c in zip(head, r)) + "</tr>"
                   for r in rows)
    return f'<table><tr class="h">{th}</tr>{body}</table>'


def _sig(x: float, digits: int) -> str:
    """`digits` significant digits, plain notation (never 2.3e-05), trailing zeros dropped."""
    if x == 0 or not math.isfinite(x):
        return "0" if x == 0 else str(x)
    dec = max(0, digits - 1 - math.floor(math.log10(abs(x))))
    out = f"{round(x, dec):.{dec}f}"
    return out.rstrip("0").rstrip(".") if "." in out else out


def _is_share(kpi) -> bool:
    return "share" in str(kpi or "").lower()


def _pct(x: float) -> str:
    return _sig(x * 100, 3 if abs(x) >= 0.1 else 2)


def _n(v, kpi=None) -> str:
    """A value for people: a share KPI in [0, 1] as a percentage, else 4 significant digits."""
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if _is_share(kpi) and 0 <= v <= 1:
        return _pct(v) + "%"
    return _sig(v, 4)


def _hw(v, kpi=None) -> str:
    """A half-width or target; for a share KPI it is in percentage points."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and _is_share(kpi):
        return _pct(v) + " points"
    return _n(v)


def _num(v, kpi=None) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "" if v is None else esc(v)
    return _n(v, kpi)


def _real(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _value_text(mean, half_width=None, kpi=None, lang: str | None = None) -> str:
    """A mean with its 95% interval in the one wording of every page (D1236); "" without a mean."""
    if not _real(mean):
        return ""
    lo, hi = (mean - half_width, mean + half_width) if _real(half_width) else (None, None)
    share = _is_share(kpi) and 0 <= mean <= 1
    if share and lo is not None:
        # A share cannot leave [0, 1]; the t-interval around a mean near 0 can (0.02% +- 0.03 points).
        lo, hi = max(lo, 0.0), min(hi, 1.0)
    return pagefmt.interval_text(mean, lo, hi, share=share, lang=lang)


def diff_text(diff, kpi=None) -> str:
    """D1264: an option's paired difference from the Baseline (`diff_vs_baseline`), "a difference
    of +22.2 points (95% interval +18.0 to +26.4)", or "no clear difference: ..." when the
    interval holds zero; "" for a record without one (the caller keeps D1237's wording)."""
    if not isinstance(diff, dict) or not all(_real(diff.get(k)) for k in ("mean", "lo", "hi")):
        return ""
    scale, unit = (100.0, "points") if _is_share(kpi) else (1.0, "")
    text = pagefmt.interval_text(diff["mean"] * scale, diff["lo"] * scale, diff["hi"] * scale, unit, signed=True)
    return f"no clear difference: {text}" if diff["lo"] <= 0 <= diff["hi"] else f"a difference of {text}"


# D1265: the paired test's level, and the share of the Baseline's replications at or under which
# the option's steadiness is worth a line.
STEADIER_P = 0.05
STEADIER_SHARE = 0.5


def steadier_text(goal: str, option: str, kpi, spread) -> str:
    """D1265: one modest line when an option made a goal KPI markedly steadier, else "". Both must
    hold: the Pitman-Morgan test (valid for common-random-number pairs) finds the option's
    variance lower at 5%, and the option reaches the same half-width with at most half the
    Baseline's replications, n ~ sd^2 under the precision rule with the target held fixed.
    When the coefficient of variation did not fall, the spread fell with the mean, and it says so."""
    if not isinstance(spread, dict) or not all(_real(spread.get(k)) for k in ("p", "sd", "base_sd")):
        return ""
    sd, base = spread["sd"], spread["base_sd"]
    if not (spread["p"] < STEADIER_P and 0 <= sd < base):
        return ""
    share = (sd / base) ** 2
    if share > STEADIER_SHARE:
        return ""
    mean, base_mean = spread.get("mean"), spread.get("base_mean")
    scaled = _real(mean) and _real(base_mean) and mean != 0 and base_mean != 0
    with_mean = scaled and sd / abs(mean) >= base / abs(base_mean)
    return (f"Run-to-run variation of {goal} was lower with {option}"
            + (", in proportion to its lower mean" if with_mean else "")
            + f" (standard deviation {_hw(sd, kpi)} against {_hw(base, kpi)} in the {pagefmt.REFERENCE}): "
            f"it would reach the same precision with about {max(1, round(100 * share))}% of the replications. "
            "A statistical observation, not a measured cause.")


def _goal_words(spec: dict, kpi=None) -> str:
    """A goal's range as a person says it: "at most 10%", "at least 5", "2 to 3" (never "-inf")."""
    lo, hi = (spec.get(k) if _real(spec.get(k)) else None for k in ("low", "high"))
    if lo is None and hi is None:
        return ""
    if lo is None:
        return f"at most {_n(hi, kpi)}"
    return f"at least {_n(lo, kpi)}" if hi is None else f"{_n(lo, kpi)} to {_n(hi, kpi)}"


def _range(spec: dict) -> str:
    lo, hi = spec.get("low"), spec.get("high")
    return "" if lo is None and hi is None else f"{_num(lo) or '-inf'} to {_num(hi) or 'inf'}"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# One rule for every page (D1234, D1253): the engine's, under the names the pages import.
REPORTED = study_mod.REPORTED
validation_result = study_mod.validation_result


def revalidate_note(study: dict, folder: Path | None, study_id: str | None) -> str:
    """D1253: the call that re-checks a reported validation, when the engine can check it now
    (`study.can_revalidate`); "" otherwise. Plain text; the room adds its button beside it."""
    if not study_mod.can_revalidate(study, folder):
        return ""
    if study_id:
        return f'Re-validate: study_validate(study="{study_id}")'
    # The id is workspace-relative and unknown here: name the tool and the room, not a guess.
    return f"Re-validate: study_validate on this study, or in the live room (naga room {Path(folder).name})"


def _check_why(c: dict, study: dict, folder: Path | None, study_id: str | None) -> str:
    """A checklist row's reason as HTML, with the re-validate call on the validation row."""
    note = revalidate_note(study, folder, study_id) if c["item"] == "validation" else ""
    return esc(c["why"]) + (f' <span class="muted" data-revalidate>{esc(note)}</span>' if note else "")


def _reported(study: dict) -> str:
    """", or "; N reported, not checked" when some validation is the assistant's word alone."""
    n = sum(validation_result(v) == REPORTED for v in study["validation"])
    return f"; {n} {REPORTED}" if n else ""


def _validation_level(study: dict) -> str:
    passed = {v.get("level") for v in study["validation"] if study_mod.checked(v)}
    # The strongest level wins: study_mod.LEVELS is ordered weakest first.
    return next((lv for lv in reversed(study_mod.LEVELS) if lv in passed), "none recorded").replace("_", " ")


def _when(at) -> str:
    """'2026-10-19T00:09:27+00:00' -> '2026-10-19 00:09 UTC'."""
    return f"{at[:10]} {at[11:16]} UTC" if isinstance(at, str) and len(at) >= 16 else ""


def _first(*vals):
    return next((v for v in vals if v), None)


def _prec(v: dict, step: dict) -> dict:
    p = _first(v.get("precision"), step.get("precision"))
    return p if isinstance(p, dict) else {}


def _reps(v: dict, step: dict):
    return _first(v.get("replications"), step.get("replications"))


def _spark(vals: list[float]) -> str:
    """80x20 trend with no axes; one value is a dot."""
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = [(2 + (76 * i / (n - 1) if n > 1 else 38), 18 - 16 * (x - lo) / span) for i, x in enumerate(vals)]
    shape = ('<polyline fill="none" stroke="#345" stroke-width="1.5" points="'
             + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>') if n > 1 else ""
    x, y = pts[-1]
    return (f'<svg class="spark" width="80" height="20" viewBox="0 0 80 20" role="img" aria-label="trend">'
            f'{shape}<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#345"/></svg>')


def _chip(study: dict, g: dict, spec: dict, steps: dict, option: dict | None = None) -> str:
    """A goal's chip: its value and verdict across versions, or (D1252) with a recommended
    *option* (`studywriteup.facts`' goal entry), the option's value and verdict and the
    Baseline's as the comparison."""
    kpi = goal_kpi(spec)
    vals = [v["kpis"][kpi] for v in study["versions"]
            if kpi and isinstance((v.get("kpis") or {}).get(kpi), (int, float))]
    val = ""
    if vals:
        v = study["versions"][-1]
        hw = (_prec(v, steps[v["id"]]).get(kpi) or {}).get("half_width")
        val = f" {esc(_value_text(vals[-1], hw, kpi))}"
    if option:
        status = option["status"].replace("_", " ")
        mean = f" {_value_text(vals[-1], None, kpi)}" if vals else ""     # a chip holds one interval
        base = f"{pagefmt.REFERENCE}{mean}: {g['status'].replace('_', ' ')}"
        return (f'<span class="chip {esc(option["status"])}" title="{esc(status)} with {esc(option["name"])}">'
                f'<b>{esc(g["text"])}</b> {esc(option["interval"])} with {esc(option["name"])}'
                f' <span class="muted">({esc(base)})</span></span>')
    return (f'<span class="chip {esc(g["status"])}" title="{esc(g["status"].replace("_", " "))}">'
            f'<b>{esc(g["text"])}</b>{val} {_spark(vals)}</span>')


def _noise_parts(prec: dict) -> list[str]:
    """Run noise per KPI in the pages' one wording (D1236): the KPI's plain name, a share's
    half-width and target in percentage points. Plain text; the caller escapes for HTML."""
    return [f"{pagefmt.kpi_label(k)} {_hw(p.get('half_width'), k)} (target {_hw(p.get('target'), k)})"
            for k, p in prec.items() if isinstance(p, dict)]


def _precision_line(study: dict, steps: dict) -> str:
    if not study["versions"]:
        return ""
    v = study["versions"][-1]
    n, prec = _reps(v, steps[v["id"]]), _prec(v, steps[v["id"]])
    parts = [esc(part) for part in _noise_parts(prec)]
    if not n and not parts:
        return ""
    return (f'<p class="muted">{esc(n) + " replications" if n else ""}'
            + (", " if n and parts else "") + ("95% half-width: " + "; ".join(parts) if parts else "") + "</p>")


def gloss_terms(html: str) -> str:
    """D1324: the first use of each word of `studywriteup.WORDS` in the page's text gets a hover
    title with its meaning (`term (meaning)`, as the write-up's Words used reads); tags, attributes,
    scripts and styles are left alone; inside a drawing the gloss is an SVG <title> on a <tspan>, since
    an <abbr> inside an SVG's text is not drawn (#1950)."""
    from nagasim import studywriteup
    seen, out, skip, svg = set(), [], False, 0
    for part in re.split(r"(<[^>]*>)", html):
        if part.startswith("<"):
            tag = re.match(r"<(/?)(script|style|title|svg)\b", part, re.I)
            if tag and tag.group(2).lower() == "svg":
                svg += -1 if tag.group(1) else (0 if part.rstrip().endswith("/>") else 1)
            elif tag:
                skip = not tag.group(1)
            out.append(part)
            continue
        found = []
        for term, meaning in ([] if skip else studywriteup.WORDS):
            m = None if term in seen else re.search(r"\b" + re.escape(term) + r"\w*", part,
                                                    0 if term.isupper() else re.I)
            if m:
                found.append((m.start(), m.end(), term, meaning))
        at = 0
        for start, end, term, meaning in sorted(found):
            if start < at:          # overlaps a word already glossed here: its turn comes later
                continue
            seen.add(term)
            tip = esc(f"{term} ({meaning})")
            out += [part[at:start], f'<tspan>{part[start:end]}<title>{tip}</title></tspan>' if svg > 0
                    else f'<abbr title="{tip}">{part[start:end]}</abbr>']
            at = end
        out.append(part[at:])
    return "".join(out)


def _header(study: dict, goals: list[dict], steps: dict, folder: Path, study_id: str | None,
            room_url: str | None = None, in_room: bool = False, lead: dict | None = None,
            answer: str = "") -> str:
    suff = study_mod.sufficiency(study)
    cls = "b-ok" if suff["sufficient"] else "b-open" if study.get("accepted") else "b-wait"
    if study["versions"]:
        vid = study["versions"][-1]["id"]
        at = steps[vid].get("at")
        latest = f"Latest version: {esc(vid)}" + (f", {esc(_when(at))}" if at else "")
    else:
        latest = "no model yet"
    mark = (f' data-study="{esc(study_id)}"' if study_id else "") + (
        "" if study["plan_approved"] else " data-plan-pending") + (" data-accepted" if study.get("accepted") else "")
    room = ('' if in_room else  # the room's own page needs no address, and the url holds the token
            f'<p class="muted">Live room: <a href="{esc(room_url)}">{esc(room_url)}</a></p>' if room_url
            else f'<p class="muted">For the live room run: naga room {esc(folder.name)}</p>')
    options = [o["option"] for o in lead["goals"]] if lead else [None] * len(goals)
    chips = "".join(_chip(study, g, spec, steps, o) for g, spec, o in zip(goals, study["goals"], options))
    headline = _headline(lead)
    return (f'<header id="cover"{mark}><h1>{esc(study["question"])}</h1>'
            + (f'<p class="muted">Decision: {esc(study["decision"])}</p>' if study["decision"] else "")
            + (f'<p class="lead" id="answer">{esc(answer)}</p>' if answer else "")
            + f'<div class="banner {cls}">{esc(suff["label"])}</div>'
            + (f'<p class="lead">{esc(headline)}</p>' if headline else "")
            + (f'<div class="chips">{chips}</div>' if chips else "")
            + _precision_line(study, steps)
            + f'<p class="muted">{_open_item(study, folder, study_id)}{latest}.</p>{room}</header>')


def _lead(folder: Path, study: dict) -> dict | None:
    """D1237, D1252: the deck's facts when an option is recommended (it leads the header, the
    chips and the Goals table, the Baseline its comparison); None without one."""
    from nagasim import studywriteup      # it builds on this module's helpers
    if not study["versions"] or len(study_mod.scenarios_meeting(study)) != 1:
        return None                     # D1348: two or more meeting every goal: none leads
    return studywriteup.facts(folder, study)


def _answer_line(folder: Path, study: dict, lead: dict | None) -> str:
    """D1324: the write-up's plain answer line (`studywriteup.facts`' verdict), the same on the page, the
    write-up and the deck; "" before the first version."""
    from nagasim import studywriteup
    if lead is not None:
        return lead["verdict"]
    return studywriteup.facts(folder, study)["verdict"] if study["versions"] else ""


def _headline(facts: dict | None) -> str:
    from nagasim import studywriteup
    return studywriteup._headline(facts) if facts else ""


def _open_item(study: dict, folder: Path, study_id: str | None = None) -> str:
    """The first checklist item still open, for the person (next_step is worded for the assistant),
    in the order next_step takes them, the modeller's bookkeeping left out (#1884)."""
    item = next((i["check"] for i in study_mod.open_items(study, folder)
                 if "check" in i and i["item"] not in study_mod.MODELLER_ITEMS), None)
    if item is None:
        return ""
    why = f" ({_check_why(item, study, folder, study_id)})" if item["why"] else ""
    return f"<b>Open:</b> {esc(item['title'])}{why}; "


def _panel(pid: str, summary: str, body: str, opened: bool) -> str:
    return (f'<details id="{pid}" class="panel"{" open" if opened else ""}><summary>{summary}</summary>'
            f'<div class="scroll">{body}</div></details>')


def _tally(items: list[str]) -> str:
    return ", ".join(f"{items.count(k)} {k.replace('_', ' ')}"
                     for k in ("passed", "partial", "failed", "not_verified") if k in items)


def _steadier_line(g: dict) -> str:
    """D1265: the muted steadier-KPI line under a goal (`studywriteup.facts`' goal entry), or ""."""
    note = (g.get("option") or {}).get("steadier")
    return f'<div class="muted">{esc(note)}</div>' if note else ""


def _goals_panel(study: dict, goals: list[dict], folder: Path, study_id: str | None = None,
                 lead: dict | None = None) -> str:
    last = (study["versions"][-1].get("kpis") or {}) if study["versions"] else {}
    head = ["Goal", "KPI", "Target", "Last value", "Status"]
    rows = [[esc(g["text"]), esc(goal_kpi(spec) or ""), esc(_goal_words(spec, goal_kpi(spec))),
             _num(last.get(goal_kpi(spec)), goal_kpi(spec)), _badge(g["status"])]
            for spec, g in zip(study["goals"], goals)]
    if lead and any(g["option"] for g in lead["goals"]):
        # D1252: the recommended option's value and verdict lead, the Baseline's is the comparison.
        head = ["Goal", "KPI", "Target", f"{lead['best'][0]} value", "Status", f"{pagefmt.REFERENCE} value"]
        rows = [[esc(g["text"]) + _steadier_line(f), esc(f["kpi"]), esc(f["limit"]),
                 esc(f["option"]["interval"]) if f["option"] else "-",
                 _badge(f["option"]["status"] if f["option"] else "not_verified"),
                 esc(f"{f['interval'] or f['value']}: {f['status'].replace('_', ' ')}")]
                for g, f in zip(goals, lead["goals"])]
    cl = study_mod.checklist(study, folder)
    checks = [[esc(c["title"]), _badge(c["verdict"]), _check_why(c, study, folder, study_id)] for c in cl]
    verdicts = [c["verdict"] for c in cl]
    met = sum(g["status"] == "met" for g in goals)
    best = lead["best"][0] if lead else ""
    with_best = sum((g["option"] or {}).get("status") == "met" for g in lead["goals"]) if lead else 0
    summary = (f"Goals: {met} of {len(goals)} met"
               + (f" in the {pagefmt.REFERENCE}, {with_best} with {esc(best)}" if lead else "")
               + f"; Checklist: {esc(_tally(verdicts))}; "
               f"Validation: {esc(_validation_level(study) + _reported(study))}")
    # D1259: where the room draws the person's "I checked this" form, below the table so it has the
    # panel's width on a phone (the static page and the chat shell ignore the mark).
    form = study["versions"] and any(c["item"] == "validation" and c["verdict"] != "passed" for c in cl)
    return _panel("goals", summary, _table(head, rows)
                  + _table(["Checklist", "Verdict", "Reason"], checks)
                  + ('<div data-confirm></div>' if form else ""),
                  "failed" in verdicts)    # partial items are common; the chips already show the goals


def _flow(folder: Path, study: dict, v: dict, step: dict) -> str:
    model = _read(folder / "versions" / v["id"] / "model.json")
    if model is None:
        return '<p class="muted">The model file of this version is missing.</p>'
    geom = flowchart.geometry(model, set(step.get("changed") or []),
                              pending=[c for c in study["target"] if not c.get("blocks")])
    return f'<div class="flow">{flowchart.to_svg(geom, v["id"])}</div>'


def _model_panel(folder: Path, study: dict, steps: dict) -> str:
    if not study["versions"]:
        return _panel("model", "Model: none yet", "<p>No model yet.</p>", True)
    v = study["versions"][-1]
    pending = sum(1 for c in study["target"] if not c.get("blocks"))
    summary = f"Model: {esc(v['id'])}" + (f", {pending} target part(s) pending" if pending else "")
    return _panel("model", summary, _flow(folder, study, v, steps[v["id"]]), True)


def invalid_text(partial) -> str:
    """What a page writes in place of a run's numbers when a law warned (#1880): "not valid: " and
    why, in the trust card's plain sentences; "" when none did (censoring and initial bias alone
    leave the numbers, with `caveat_text`, D1316)."""
    invalid, _ = laws.split(partial)
    return ("not valid: " + " ".join(pagefmt.law_reasons(invalid))) if invalid else ""


def caveat_text(partial) -> str:
    """What a page writes beside a run's numbers when only censoring or initial bias warned (D1316),
    "may be biased low: ..."; "" otherwise (a run that is not valid shows no numbers)."""
    invalid, low = laws.split(partial)
    return "" if invalid else laws.caveat(low)


def _option_partial(r: dict) -> list:
    """The warnings of one stored scenario ({"kpis", "goals", "laws"?}): the study step stores
    "laws" only for an option whose law checks warned."""
    return (r.get("laws") or {}).get("partial") or []


def _base_partial(step: dict) -> list:
    """The warnings of a step's own run: its law checks that did not pass."""
    return [c for c in step.get("checks") or [] if c.get("kind") == "law" and c.get("verdict") != "passed"]


def option_invalid(r: dict) -> str:
    """`invalid_text` of one stored scenario."""
    return invalid_text(_option_partial(r))


def option_caveat(r: dict) -> str:
    """`caveat_text` of one stored scenario."""
    return caveat_text(_option_partial(r))


def base_invalid(step: dict) -> str:
    """`invalid_text` of a step's own run."""
    return invalid_text(_base_partial(step))


def base_caveat(step: dict) -> str:
    """`caveat_text` of a step's own run."""
    return caveat_text(_base_partial(step))


def _scenarios(study: dict, sc: dict) -> str:
    """Scenario x goal KPI table: mean +- CI and whether the goal is met; an option whose run is
    not valid says so in place of its numbers (#1880), one that may be biased low says so under
    its name (D1316)."""
    rows = []
    for name, r in sc.items():
        if why := option_invalid(r):
            rows.append([esc(name)] + ([esc(why)] + ["-"] * (len(study["goals"]) - 1) if study["goals"] else []))
            continue
        cav = option_caveat(r)
        cells = [esc(name) + (f'<div class="muted">{esc(cav)}</div>' if cav else "")]
        for g in study["goals"]:
            e = (r.get("kpis") or {}).get(goal_kpi(g)) or {}
            v = (r.get("goals") or {}).get(g.get("id"), "not_verified")
            cells.append(f"{esc(_value_text(e.get('mean'), e.get('ci_half_width'), goal_kpi(g)))} {_badge(v)}"
                         if e else _badge(v))
        rows.append(cells)
    return _table(["Scenario"] + [goal_kpi(g) or g.get("id", "") for g in study["goals"]], rows)


def _scenarios_panel(study: dict, steps: dict) -> str:
    if not study["versions"]:
        return ""
    v = study["versions"][-1]
    sc = _first(v.get("scenarios"), steps[v["id"]].get("scenarios"))
    if not sc:
        return ""
    parts = []
    for name, r in sc.items():
        verdicts = list((r.get("goals") or {}).values())
        parts.append(f"{esc(name)} is not valid" if option_invalid(r)
                     else f"{esc(name)} meets {verdicts.count('met')} of {len(verdicts)}"
                     + (" (may be biased low)" if option_caveat(r) else ""))
    return _panel("scenarios", "Scenarios: " + "; ".join(parts), _scenarios(study, sc), True)


def _version_detail(folder: Path, study: dict, v: dict, step: dict) -> str:
    vid = v["id"]
    changes = "".join(f"<li>{esc(c)}</li>" for c in step.get("changes") or [])
    latest = " data-latest" if v is study["versions"][-1] else ""
    return (f'<div class="version" id="{esc(vid)}" data-version="{esc(vid)}"{latest}>'
            + (f"<ul>{changes}</ul>" if changes else "")
            + _table(["KPI", "Mean"], [[esc(k), _num(x)] for k, x in (v.get("kpis") or {}).items()])
            + _flow(folder, study, v, step)
            + f'<p class="muted">To go back, ask: "revert to {esc(vid)}"</p></div>')


def _versions_panel(folder: Path, study: dict, steps: dict) -> str:
    kpis = list(dict.fromkeys(goal_kpi(g) for g in study["goals"] if g.get("kpi")))
    head = ["Version", "Note", "Verdict"] + kpis + ["Replications", "When"]
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    rows = []
    for v in reversed(study["versions"]):
        step = steps[v["id"]]
        vals = [_num((v.get("kpis") or {}).get(k)) for k in kpis]
        cells = ([esc(v["id"]), esc(v.get("note", "")), _badge(v.get("verdict", "not_verified"))] + vals
                 + [esc(_reps(v, step) or ""), esc(_when(step.get("at")))])
        rows.append("<tr>" + "".join(f'<td data-label="{esc(h)}">{c}</td>' for h, c in zip(head, cells)) + "</tr>"
                    f'<tr><td colspan="{len(head)}"><details>'
                    f"<summary>{esc(v['id'])}: changes, all KPIs, flowchart</summary>"
                    f"{_version_detail(folder, study, v, step)}</details></td></tr>")
    n = len(study["versions"])
    summary = f"Versions: {n}, latest {esc(study['versions'][-1]['id'])}" if n else "Versions: none yet"
    return _panel("versions", summary, f'<table><tr class="h">{th}</tr>{"".join(rows)}</table>', True)


def trust_rows(folder: Path, study: dict, steps: dict) -> list[list[str]]:
    """The three uncertainties of the last version, [[label, plain text]], each with its own basis
    and "not measured" when absent; never summed (D739). The input-data and parameter rows quote
    the version's asked.json, so a study from before them reads "not measured"."""
    from nagasim import uncertainty
    if not study["versions"]:
        return []
    v = study["versions"][-1]
    step = steps.get(v["id"]) or {}
    n, prec = _reps(v, step), _prec(v, step)
    parts = _noise_parts(prec)
    noise = ", ".join(([f"{n} replications"] if n else []) + (["95% half-width: " + "; ".join(parts)] if parts else []))
    try:        # a list, unlike `_read`'s objects; unreadable reads as nothing asked
        asked = json.loads((folder / "versions" / v["id"] / "asked.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        asked = []
    texts = uncertainty.trust_texts(asked if isinstance(asked, list) else [])
    return [["Run noise", noise or uncertainty.NOT_MEASURED], ["Input data", texts["input"]],
            ["Parameters", texts["params"]]]


def _trust_panel(folder: Path, study: dict, steps: dict) -> str:
    rows = trust_rows(folder, study, steps)
    if not rows:
        return ""
    return _panel("trust", "Can I trust this: three uncertainties, kept apart",
                  _table(["Uncertainty", "Basis"], [[esc(a), esc(b)] for a, b in rows]), False)


def _assumptions_panel(study: dict) -> str:
    rows = []
    for a in study["assumptions"]:
        value = _num(a.get("value")) or esc(_range(a.get("check") or {}))
        rows.append([esc(a.get("text") or a.get("statement") or ""), value, esc(a.get("source") or "")])
    src = [[esc(s.get("text") or s.get("id") or ""), esc(Path(str(s["input"])).name if s.get("input") else "")]
           for s in study["sources"]]
    return _panel("assumptions", f"Assumptions: {len(rows)}, sources: {len(src)}",
                  _table(["Assumption", "Value or range", "Source"], rows)
                  + _table(["Source", "Input file"], src), False)


def _sensitivity_panel(folder: Path, study: dict) -> str:
    """Ranked factors of the last version's sensitivity.json, redrawn from its numbers (stored SVG is never embedded)."""
    if not study["versions"]:
        return ""
    latest = study["versions"][-1]["id"]
    data = _read(folder / "versions" / latest / "sensitivity.json")
    rows = [(f, k, e) for f in (data or {}).get("factors") or [] for k, e in (f.get("kpis") or {}).items()]
    if not rows:
        # #1789: an older version's run is named, so the page never says "not run" beside it.
        older = _made(folder, study, ["versions/{v}/sensitivity.json"])
        said = (f"not run for {latest}; last run for {older.parent.name}" if older is not None
                else "not run yet")
        return (f'<details id="sensitivity" class="panel" data-empty><summary>Sensitivity: {esc(said)}</summary>'
                f'<div class="scroll"><p class="muted">Which assumptions change the answer: '
                f'{esc(said)}.</p></div></details>')
    rows.sort(key=lambda r: -(r[2].get("swing") or 0))
    table = _table(["Assumption", "KPI", "Low", "High", "Swing", "Changes the answer"],
                   [[esc(f.get("label") or f.get("path") or ""), esc(k), _num(f.get("low")), _num(f.get("high")),
                     _num(e.get("swing")), "yes" if e.get("flips") else "no"] for f, k, e in rows])
    n = len((data or {}).get("changes_answer") or [])
    return _panel("sensitivity", f"Sensitivity: {n} assumption(s) change the answer", table, False)


# What each deliverable is, in plain words, and where its file lands (newest version first);
# view and extensions write no file, so they are only ever "on request".
_DELIVERABLES = (
    ("report", "Report of a version", ["versions/{v}/report.html"]),
    ("view", "Flowchart image", []),
    ("package", "Deliverable package", ["package/control-room.html"]),
    ("sensitivity", "Sensitivity of the answer", ["versions/{v}/sensitivity.json"]),
    ("extensions", "Candidate extensions", []),
    ("animation", "Animation", ["versions/{v}/animation.gif"]),
    ("writeup", "Write-up", ["package/writeup.html"]),
    ("slides", "Slides", ["package/slides.html"]),
)


def _made(folder: Path, study: dict, patterns: list[str]) -> Path | None:
    """The newest existing file of a deliverable: the latest version that has it."""
    ids = [v["id"] for v in reversed(study["versions"])]
    paths = [folder / p.format(v=i) for p in patterns for i in (ids if "{v}" in p else [""])]
    return next((p for p in paths if p.is_file()), None)


def _deliverables_panel(folder: Path, study: dict, packaged: bool = False) -> str:
    items, made = [], 0
    for kind, label, patterns in _DELIVERABLES:
        path = _made(folder, study, patterns)
        if kind == "package" and packaged:
            # #1789: this page is written into the package, before the package is complete.
            made += 1
            items.append(f'<li data-deliverable="{kind}" data-path="package/control-room.html">'
                         f'{esc(label)}: this package</li>')
            continue
        if path is not None:
            made += 1
            # #1789: UTC, as every other time on the page.
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(path.stat().st_mtime))
            rel = path.relative_to(folder).as_posix()
            old = rel.split("/")[1] if rel.startswith("versions/") else None
            latest = study["versions"][-1]["id"]
            state = f"made for {old}, {when}" if old and old != latest else f"made {when}"
        else:
            state, rel = ("on request" if not patterns else "not made yet"), ""
        items.append(f'<li data-deliverable="{kind}" data-path="{esc(rel)}">{esc(label)}: {esc(state)}</li>')
    return _panel("deliverables", f"Deliverables: {made} made", "<ul>" + "".join(items) + "</ul>", False)


def render(folder: Path, study: dict | None = None, study_id: str | None = None,
           room_url: str | None = None, in_room: bool = False, packaged: bool = False) -> str:
    """The page as one string; loads the study when none is given. `room_url` is the live room's
    address while one runs (the header links it); `in_room` is the room's own view, which omits the line;
    `packaged` is the copy written into the package (it lists the package as this one)."""
    folder = Path(folder)
    study = study_mod.load(folder) if study is None else study
    steps = {v["id"]: _read(folder / "versions" / v["id"] / "step.json") or {} for v in study["versions"]}
    goals = study_mod.goals_status(study)
    lead = _lead(folder, study)
    answer = _answer_line(folder, study, lead)
    # `<` as < keeps a note holding "</script>" from closing the data block.
    data = json.dumps(study, ensure_ascii=True).replace("<", "\\u003c")
    page = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f'<meta http-equiv="Content-Security-Policy" content="{csp(_CSS)}">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(study['question'][:80])}</title><style>{_CSS}</style></head><body><main>"
            + _header(study, goals, steps, folder, study_id, room_url, in_room or packaged, lead, answer) + '<div id="actions"></div>'
            + _goals_panel(study, goals, folder, study_id, lead) + _model_panel(folder, study, steps)
            + _versions_panel(folder, study, steps) + _scenarios_panel(study, steps) + _sensitivity_panel(folder, study) + _trust_panel(folder, study, steps)
            + _assumptions_panel(study) + _deliverables_panel(folder, study, packaged)
            + f'<script type="application/json" id="study-json">{data}</script></main></body></html>\n')
    page = gloss_terms(page)
    # Non-ASCII text becomes a character reference: the page stays ASCII and renders the same.
    return page.encode("ascii", "xmlcharrefreplace").decode("ascii")


# --- the client variant (D1238, S4-03 ruling (b)) ------------------------------------------------
# The page the person who asked the question reads: what was found, how far to trust it, what the
# package holds. No commands, paths, internal KPI names, bookkeeping numbers, model diffs or
# assistant instructions; every number in the one wording of the pages (D1236).

_PLAIN_VERDICT = {"passed": "done", "partial": "in part", "failed": "failed", "not_verified": "not checked"}
_PLAIN_LEVEL = {"data": "compared with measured data", "known_answer": "checked against a known result",
                "expert": "reviewed by an expert"}


def _plain_badge(verdict: str) -> str:
    return f'<span class="badge {esc(verdict)}">{esc(_PLAIN_VERDICT.get(verdict, verdict.replace("_", " ")))}</span>'


def _section(title: str, body: str) -> str:
    return f'<section class="panel"><h2>{esc(title)}</h2><div class="scroll">{body}</div></section>'


def _client_trust(f: dict) -> list[list[str]]:
    """Run noise per goal in words, then input data and parameters as the trust card says them."""
    names = {g["kpi"]: g["text"] for g in f["goals"]}
    noise = "; ".join(f"{names.get(k, k)}: the 95% interval reaches {w} either side (wanted {t})"
                      for k, w, t in f["precision"] if k in names)
    runs = f"{f['replications']} replications" if f["replications"] else ""
    rows = [["Run noise", ", ".join(x for x in (runs, noise) if x) or "not measured"]]
    rows += [row for row in f["uncertainties"] if row[0] != "Run noise"]
    return rows


def render_client(folder: Path, study: dict, files: list[str]) -> str:
    """The client page of the package; *files* are the names the package holds, listed as written."""
    from nagasim import studywriteup      # its facts are the deck's: one reading of the folder
    folder = Path(folder)
    f = studywriteup.facts(folder, study)
    lead = studywriteup._headline(f)
    heads = [g["text"] for g in f["goals"]]
    options = [[f"{pagefmt.REFERENCE}"] + [f"{g['interval'] or g['value']}: {g['status'].replace('_', ' ')}"
                                           for g in f["goals"]]] + f["scenarios"]
    goals = [[esc(g["text"]), esc(g["limit"]), esc(g["interval"] or g["value"]), _badge(g["status"])]
             for g in f["goals"]]
    goal_head = ["Goal", "Target", f"{pagefmt.REFERENCE} value", "Status"]
    if f["best"] and any(g["option"] for g in f["goals"]):
        # D1252: the recommended option's value and verdict, the Baseline's as the comparison.
        goal_head = ["Goal", "Target", f"{f['best'][0]} value", "Status", f"{pagefmt.REFERENCE} value"]
        goals = [[esc(g["text"]) + _steadier_line(g), esc(g["limit"]),
                  esc(g["option"]["interval"]) if g["option"] else "-",
                  _badge(g["option"]["status"] if g["option"] else "not_verified"),
                  esc(f"{g['interval'] or g['value']}: {g['status'].replace('_', ' ')}")]
                 for g in f["goals"]]
    validation = [[esc(_PLAIN_LEVEL.get(v.get("level", ""), str(v.get("level", "")).replace("_", " "))),
                   esc(validation_result(v)), esc(study_mod.validation_note(v))]
                  for v in study["validation"]]
    sens = f["sensitivity"]
    if sens and sens.get("factors"):
        sens_body = (f"<p>Each assumption moved {esc(_n(sens.get('delta', 0) * 100))}% either way, one at a time.</p>"
                     + _table(["Assumption", "Changes the answer"],
                              [[esc(r.get("label") or ""), "yes" if r.get("changes_answer") else "no"]
                               for r in sens["factors"]]))
    else:
        sens_body = "<p>No sensitivity analysis was run for these results.</p>"
    steps = [[esc(c[0]), _plain_badge(c[1].replace(" ", "_")), esc(c[2])] for c in f["checks"]]
    when = next((s.get("at") for s in [_read(folder / "versions" / f["version"] / "step.json") or {}]), None)
    body = (f'<header id="cover"><h1>{esc(f["question"])}</h1>'
            + (f'<p class="muted">Decision: {esc(f["decision"])}</p>' if f["decision"] else "")
            + f'<div class="banner {f["banner"]}">The study is {esc(f["label"])}.</div>'
            + (f'<p class="lead">{esc(lead)}</p>' if lead else "") + "</header>"
            + _section("Options compared", _table(["Option"] + heads, [[esc(c) for c in r] for r in options]))
            + _section("Goals", _table(goal_head, goals))
            + _section("How far to trust it", _table(["Check", "Result", "Note"], validation)
                       + _table(["Uncertainty", "Basis"], [[esc(a), esc(b)] for a, b in _client_trust(f)]))
            + _section("Assumptions", _table(["Assumption", "Value or range", "Source"],
                                             [[esc(c) for c in r] for r in f["assumptions"]])
                       if f["assumptions"] else "<p>No assumptions were recorded.</p>")
            + _section("Which assumptions change the answer", sens_body)
            + _section("Steps of the study", _table(["Step", "State", "Note"], steps))
            + _section("In this package", "<ul>" + "".join(f"<li>{esc(n)}</li>" for n in files) + "</ul>")
            + f'<p class="muted">Results of version {esc(f["version"])}'
            + (f", {esc(str(when)[:10])}" if when else "") + ".</p>")
    return _page(f["question"], _CSS, f"<main>{body}</main>")
