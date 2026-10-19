"""The control room: one offline HTML page for a study, built from the study folder alone.

Same no-script pattern as the player: a Content-Security-Policy with `default-src 'none'` and
no external address. Flowcharts are re-rendered from each version's `model.json`, never read
from a stored SVG, so a hand-edited folder cannot inject markup. Every study string goes
through `esc`. The page embeds `study.json` as data so the page alone can restart a study."""
from __future__ import annotations

import json
import math
from pathlib import Path

from nagasim import flowchart, study as study_mod
from nagasim.report import esc
from nagasim.studychecks import goal_kpi

_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'")
_CSS = """
body{font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#222;
 background:#f6f7f9;margin:0}
main{max-width:1100px;margin:0 auto;padding:0 16px 16px}
header{position:sticky;top:0;z-index:2;background:#f6f7f9;max-height:40vh;overflow:auto;
 padding:8px 0;border-bottom:1px solid #dde1e6}
header p{margin:2px 0}
.panel{background:#fff;border:1px solid #dde1e6;border-radius:8px;margin:8px 0}
.panel>summary{padding:6px 14px;font-weight:600;cursor:pointer}
.scroll{padding:0 14px 8px;overflow-x:auto}
h1{font-size:18px;margin:2px 0}h3{font-size:14px;margin:0}
.banner{padding:4px 10px;border-radius:6px;font-weight:600;margin:4px 0}
.b-ok{background:#d9f2e0;color:#14532d}.b-wait{background:#fdecc8;color:#7a4b00}
.b-open{background:#e3e5e8;color:#333}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}
.chip{display:inline-flex;align-items:center;gap:6px;border:2px solid #999;border-radius:14px;
 padding:1px 10px;background:#fff}
.chip.met{border-color:#2e7d32}.chip.not_met{border-color:#c62828}
.spark{vertical-align:middle}
table{border-collapse:collapse;width:100%;margin:6px 0}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid #eee;vertical-align:top}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#fff;background:#777}
.badge.passed,.badge.met{background:#2e7d32}.badge.partial{background:#c77700}
.badge.failed,.badge.not_met{background:#c62828}.badge.not_verified{background:#777}
.muted{color:#666;font-size:13px}.flow{overflow-x:auto}.flow svg{max-width:100%;height:auto;max-height:200px}
summary{cursor:pointer}
@media print{body{background:#fff}header{position:static;max-height:none;overflow:visible}
 details{display:block}
 details>*:not(summary){display:block!important;content-visibility:visible!important}
 .version{break-inside:avoid}}
"""


def _badge(verdict: str) -> str:
    return f'<span class="badge {esc(verdict)}">{esc(str(verdict).replace("_", " "))}</span>'


def _ascii(text: str) -> str:
    """Non-ASCII text becomes a character reference: the page stays ASCII and renders the same."""
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _page(title: str, css: str, body: str) -> str:
    """One HTML document, no script allowed by the CSP; `body` holds ready-made (escaped) markup."""
    return _ascii("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
                  f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1">'
                  f"<title>{esc(title[:80])}</title><style>{css}</style></head><body>{body}</body></html>\n")


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Rows hold ready-made (already escaped) cells."""
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


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


def _range(spec: dict) -> str:
    lo, hi = spec.get("low"), spec.get("high")
    return "" if lo is None and hi is None else f"{_num(lo) or '-inf'} to {_num(hi) or 'inf'}"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _validation_level(study: dict) -> str:
    passed = {v.get("level") for v in study["validation"] if v.get("passed")}
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


def _chip(study: dict, g: dict, spec: dict, steps: dict) -> str:
    kpi = goal_kpi(spec)
    vals = [v["kpis"][kpi] for v in study["versions"]
            if kpi and isinstance((v.get("kpis") or {}).get(kpi), (int, float))]
    val = ""
    if vals:
        v = study["versions"][-1]
        hw = (_prec(v, steps[v["id"]]).get(kpi) or {}).get("half_width")
        val = f' {_num(vals[-1])}' + (f" +- {_num(hw)}" if hw is not None else "")
    return (f'<span class="chip {esc(g["status"])}" title="{esc(g["status"].replace("_", " "))}">'
            f'<b>{esc(g["text"])}</b>{val} {_spark(vals)}</span>')


def _precision_line(study: dict, steps: dict) -> str:
    if not study["versions"]:
        return ""
    v = study["versions"][-1]
    n, prec = _reps(v, steps[v["id"]]), _prec(v, steps[v["id"]])
    parts = [f"{esc(k)} {_num(p.get('half_width'))} (target {_num(p.get('target'))})"
             for k, p in prec.items() if isinstance(p, dict)]
    if not n and not parts:
        return ""
    return (f'<p class="muted">{esc(n) + " replications" if n else ""}'
            + (", " if n and parts else "") + ("95% half-width: " + "; ".join(parts) if parts else "") + "</p>")


def _header(study: dict, goals: list[dict], steps: dict, folder: Path, study_id: str | None) -> str:
    suff = study_mod.sufficiency(study)
    cls = "b-ok" if suff["sufficient"] else "b-open" if study.get("accepted") else "b-wait"
    if study["versions"]:
        vid = study["versions"][-1]["id"]
        at = steps[vid].get("at")
        latest = f"Latest version: {esc(vid)}" + (f", {esc(_when(at))}" if at else "")
    else:
        latest = "no model yet"
    mark = f' data-study="{esc(study_id)}"' if study_id else ""
    chips = "".join(_chip(study, g, spec, steps) for g, spec in zip(goals, study["goals"]))
    return (f'<header id="cover"{mark}><h1>{esc(study["question"])}</h1>'
            + (f'<p class="muted">Decision: {esc(study["decision"])}</p>' if study["decision"] else "")
            + f'<div class="banner {cls}">{esc(suff["label"])}</div>'
            + (f'<div class="chips">{chips}</div>' if chips else "")
            + _precision_line(study, steps)
            + f'<p class="muted">{_open_item(study, folder)}{latest}.</p></header>')


def _open_item(study: dict, folder: Path) -> str:
    """The first checklist item still open, for the person (next_step is worded for the assistant)."""
    item = next((c for c in study_mod.checklist(study, folder) if c["verdict"] != "passed"), None)
    if item is None:
        return ""
    why = f" ({esc(item['why'])})" if item["why"] else ""
    return f"<b>Open:</b> {esc(item['title'])}{why}. "


def _panel(pid: str, summary: str, body: str, opened: bool) -> str:
    return (f'<details id="{pid}" class="panel"{" open" if opened else ""}><summary>{summary}</summary>'
            f'<div class="scroll">{body}</div></details>')


def _tally(items: list[str]) -> str:
    return ", ".join(f"{items.count(k)} {k.replace('_', ' ')}"
                     for k in ("passed", "partial", "failed", "not_verified") if k in items)


def _goals_panel(study: dict, goals: list[dict], folder: Path) -> str:
    last = (study["versions"][-1].get("kpis") or {}) if study["versions"] else {}
    rows = [[esc(g["text"]), esc(goal_kpi(spec) or ""), esc(_range(spec)), _num(last.get(goal_kpi(spec))),
             _badge(g["status"])] for spec, g in zip(study["goals"], goals)]
    cl = study_mod.checklist(study, folder)
    checks = [[esc(c["title"]), _badge(c["verdict"]), esc(c["why"])] for c in cl]
    verdicts = [c["verdict"] for c in cl]
    met = sum(g["status"] == "met" for g in goals)
    summary = (f"Goals: {met} of {len(goals)} met. Checklist: {esc(_tally(verdicts))}. "
               f"Validation: {esc(_validation_level(study))}")
    return _panel("goals", summary, _table(["Goal", "KPI", "Range", "Last value", "Status"], rows)
                  + _table(["Checklist", "Verdict", "Reason"], checks),
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


def _scenarios(study: dict, sc: dict) -> str:
    """Scenario x goal KPI table: mean +- CI and whether the goal is met."""
    rows = []
    for name, r in sc.items():
        cells = [esc(name)]
        for g in study["goals"]:
            e = (r.get("kpis") or {}).get(goal_kpi(g)) or {}
            ci = f" +- {_num(e['ci_half_width'])}" if e.get("ci_half_width") is not None else ""
            v = (r.get("goals") or {}).get(g.get("id"), "not_verified")
            cells.append(f"{_num(e.get('mean'))}{ci} {_badge(v)}" if e else _badge(v))
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
        parts.append(f"{esc(name)} meets {verdicts.count('met')} of {len(verdicts)}")
    return _panel("scenarios", "Scenarios: " + "; ".join(parts), _scenarios(study, sc), True)


def _version_detail(folder: Path, study: dict, v: dict, step: dict) -> str:
    vid = v["id"]
    changes = "".join(f"<li>{esc(c)}</li>" for c in step.get("changes") or [])
    return (f'<div class="version" id="{esc(vid)}" data-version="{esc(vid)}">'
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
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
                    f'<tr><td colspan="{len(head)}"><details>'
                    f"<summary>{esc(v['id'])}: changes, all KPIs, flowchart</summary>"
                    f"{_version_detail(folder, study, v, step)}</details></td></tr>")
    n = len(study["versions"])
    summary = f"Versions: {n}, latest {esc(study['versions'][-1]['id'])}" if n else "Versions: none yet"
    return _panel("versions", summary, f"<table><tr>{th}</tr>{''.join(rows)}</table>", True)


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
    data = _read(folder / "versions" / study["versions"][-1]["id"] / "sensitivity.json")
    rows = [(f, k, e) for f in (data or {}).get("factors") or [] for k, e in (f.get("kpis") or {}).items()]
    if not rows:
        return ""
    rows.sort(key=lambda r: -(r[2].get("swing") or 0))
    table = _table(["Assumption", "KPI", "Low", "High", "Swing", "Changes the answer"],
                   [[esc(f.get("label") or f.get("path") or ""), esc(k), _num(f.get("low")), _num(f.get("high")),
                     _num(e.get("swing")), "yes" if e.get("flips") else "no"] for f, k, e in rows])
    n = len((data or {}).get("changes_answer") or [])
    return _panel("sensitivity", f"Sensitivity: {n} assumption(s) change the answer", table, False)


def _on_request_panel() -> str:
    items = ["<code>study_report</code>: the full report of any version",
             "<code>study_view</code>: a PNG of the model or a result",
             "<code>study_package</code>: the deliverable folder",
             "<code>study_sensitivity</code>: which assumptions change the answer",
             "<code>study_extensions</code>: verified candidate modules",
             "<code>study_animation</code>: a GIF of the model running",
             "<code>study_writeup</code>: a paper-style report",
             "<code>study_slides</code>: a deck"]
    return _panel("on-request", "On request: report, view, package, sensitivity, extensions, animation, write-up, slides",
                  "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>", False)


def render(folder: Path, study: dict | None = None, study_id: str | None = None) -> str:
    """The page as one string; loads the study when none is given."""
    folder = Path(folder)
    study = study_mod.load(folder) if study is None else study
    steps = {v["id"]: _read(folder / "versions" / v["id"] / "step.json") or {} for v in study["versions"]}
    goals = study_mod.goals_status(study)
    # `<` as < keeps a note holding "</script>" from closing the data block.
    data = json.dumps(study, ensure_ascii=True).replace("<", "\\u003c")
    page = ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(study['question'][:80])}</title><style>{_CSS}</style></head><body><main>"
            + _header(study, goals, steps, folder, study_id) + '<div id="actions"></div>'
            + _goals_panel(study, goals, folder) + _model_panel(folder, study, steps)
            + _versions_panel(folder, study, steps) + _scenarios_panel(study, steps) + _sensitivity_panel(folder, study)
            + _assumptions_panel(study) + _on_request_panel()
            + f'<script type="application/json" id="study-json">{data}</script></main></body></html>\n')
    # Non-ASCII text becomes a character reference: the page stays ASCII and renders the same.
    return page.encode("ascii", "xmlcharrefreplace").decode("ascii")
