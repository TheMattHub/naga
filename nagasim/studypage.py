"""The control room: one offline HTML page for a study, built from the study folder alone.

Same no-script pattern as the player: a Content-Security-Policy with `default-src 'none'` and
no external address. Flowcharts are re-rendered from each version's `model.json`, never read
from a stored SVG, so a hand-edited folder cannot inject markup. Every study string goes
through `esc`. The page embeds `study.json` as data so the page alone can restart a study."""
from __future__ import annotations

import json
from pathlib import Path

from nagasim import flowchart, study as study_mod
from nagasim.report import esc
from nagasim.svgcharts import line_chart

_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'none'")
_CSS = """
body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#222;
 background:#f6f7f9;margin:0}
main{max-width:1100px;margin:0 auto;padding:16px}
section{background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:12px 20px;margin:16px 0}
h1{font-size:22px;margin:8px 0}h2{font-size:18px;margin:6px 0 10px}h3{font-size:15px;margin:0}
.banner{padding:10px 14px;border-radius:6px;font-weight:600;margin:10px 0}
.b-ok{background:#d9f2e0;color:#14532d}.b-wait{background:#fdecc8;color:#7a4b00}
.b-open{background:#e3e5e8;color:#333}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:13px;color:#fff;background:#777}
.passed,.met{background:#2e7d32}.partial{background:#c77700}
.failed,.not_met{background:#c62828}.not_verified{background:#777}
.version{border-top:1px solid #dde1e6;padding:10px 0;break-inside:avoid}
.muted{color:#666;font-size:13px}.flow{overflow-x:auto}.flow svg{max-width:100%;height:auto}
summary{cursor:pointer;color:#345}
@media print{body{background:#fff}section{border:0}#timeline{break-before:page}
 .version{break-inside:avoid}}
"""


def _badge(verdict: str) -> str:
    return f'<span class="badge {esc(verdict)}">{esc(str(verdict).replace("_", " "))}</span>'


def _table(head: list[str], rows: list[list[str]]) -> str:
    """Rows hold ready-made (already escaped) cells."""
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


def _num(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "" if v is None else esc(v)
    return f"{v:.4g}"


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


def _scen_line(study: dict) -> str:
    """'scenarios: A meets 1 of 2; ...' from the last version's scenario results, or ''."""
    sc = ((study["versions"][-1].get("scenarios") or {}) if study["versions"] else {})
    parts = []
    for name, r in sc.items():
        verdicts = list((r.get("goals") or {}).values())
        parts.append(f"{esc(name)} meets {verdicts.count('met')} of {len(verdicts)}")
    return f" Scenarios: {'; '.join(parts)}." if parts else ""


def _cover(study: dict, goals: list[dict], steps: dict, study_id: str | None = None) -> str:
    suff = study_mod.sufficiency(study)
    cls = "b-ok" if suff["sufficient"] else "b-open" if study.get("accepted") else "b-wait"
    met = sum(g["status"] == "met" for g in goals)
    if study["versions"]:
        vid = study["versions"][-1]["id"]
        at = steps[vid].get("at")
        latest = f"Latest version: {esc(vid)}" + (f", {esc(_when(at))}" if at else "")
    else:
        latest = "no model yet"
    mark = f' data-study="{esc(study_id)}"' if study_id else ""
    return (f'<section id="cover"{mark}><h1>{esc(study["question"])}</h1>'
            + (f'<p>Decision: {esc(study["decision"])}</p>' if study["decision"] else "")
            + f'<div class="banner {cls}">{esc(suff["label"])}</div>'
            f'<p>Goals met: {met} of {len(goals)}. Validation level reached: '
            f'{esc(_validation_level(study))}. {latest}.{_scen_line(study)}</p></section>')


def _status(study: dict, goals: list[dict], folder: Path) -> str:
    last = (study["versions"][-1].get("kpis") or {}) if study["versions"] else {}
    rows = [[esc(g["text"]), esc(spec.get("kpi") or ""), esc(_range(spec)), _num(last.get(spec.get("kpi"))),
             _badge(g["status"])] for spec, g in zip(study["goals"], goals)]
    checks = [[esc(c["title"]), _badge(c["verdict"]), esc(c["why"])]
              for c in study_mod.checklist(study, folder)]
    return ('<section id="status"><h2>Status</h2>'
            + _table(["Goal", "KPI", "Range", "Last value", "Status"], rows)
            + _table(["Checklist", "Verdict", "Reason"], checks) + "</section>")


def _target(study: dict) -> str:
    rows = [[esc(c.get("name", "")),
             esc("built: " + ", ".join(map(str, c["blocks"]))) if c.get("blocks") else "pending"]
            for c in study["target"]]
    return ('<section id="target"><h2>Target</h2>'
            + (_table(["Component", "State"], rows) if rows else "<p>No target components recorded.</p>")
            + "</section>")


def _when(at) -> str:
    """'2026-10-19T00:09:27+00:00' -> '2026-10-19 00:09 UTC'."""
    return f"{at[:10]} {at[11:16]} UTC" if isinstance(at, str) and len(at) >= 16 else ""


def _vnum(vid: str) -> int:
    return int(vid[1:]) if vid[1:].isdigit() else 0


def _charts(study: dict) -> str:
    out = []
    for kpi in dict.fromkeys(g["kpi"] for g in study["goals"] if g.get("kpi")):
        pts = [(_vnum(v["id"]), v["kpis"][kpi]) for v in study["versions"] if kpi in (v.get("kpis") or {})]
        if pts:   # versions without this KPI are skipped, not drawn as zero
            out.append(line_chart(kpi, [p[0] for p in pts], {kpi: [p[1] for p in pts]}, "version", kpi,
                                  x_ticks=[(n, f"v{n:03d}") for n, _ in pts]))
    return "".join(f'<div class="flow">{c}</div>' for c in out)


def _scenarios(study: dict, sc: dict) -> str:
    """Scenario x goal KPI table: mean +- CI and whether the goal is met."""
    rows = []
    for name, r in sc.items():
        cells = [esc(name)]
        for g in study["goals"]:
            e = (r.get("kpis") or {}).get(g.get("kpi")) or {}
            ci = f" +- {_num(e['ci_half_width'])}" if e.get("ci_half_width") is not None else ""
            v = (r.get("goals") or {}).get(g.get("id"), "not_verified")
            cells.append(f"{_num(e.get('mean'))}{ci} {_badge(v)}" if e else _badge(v))
        rows.append(cells)
    return _table(["Scenario"] + [g.get("kpi") or g.get("id", "") for g in study["goals"]], rows)


def _version(folder: Path, study: dict, v: dict, step: dict) -> str:
    vid = v["id"]
    model = _read(folder / "versions" / vid / "model.json")
    if model is None:
        flow = '<p class="muted">The model file of this version is missing.</p>'
    else:
        geom = flowchart.geometry(model, set(step.get("changed") or []),
                                  pending=[c for c in study["target"] if not c.get("blocks")])
        flow = f'<div class="flow">{flowchart.to_svg(geom, vid)}</div>'
    changes = "".join(f"<li>{esc(c)}</li>" for c in step.get("changes") or [])
    all_kpis = v.get("kpis") or {}
    kpis = (f"<details><summary>All KPIs ({len(all_kpis)})</summary>"   # no script: collapsed by the browser
            + _table(["KPI", "Mean"], [[esc(k), _num(x)] for k, x in all_kpis.items()]) + "</details>")
    return (f'<div class="version" id="{esc(vid)}" data-version="{esc(vid)}"><h3>{esc(vid)} {_badge(v.get("verdict", "not_verified"))}</h3>'
            f'<p>{esc(v.get("note", ""))} <span class="muted">{esc(_when(step.get("at")))}</span></p>'
            + (f"<ul>{changes}</ul>" if changes else "") + kpis
            + (_scenarios(study, step["scenarios"]) if step.get("scenarios") else "") + flow
            + f'<p class="muted">To go back, ask: "revert to {esc(vid)}"</p></div>')


def _timeline(folder: Path, study: dict, steps: dict) -> str:
    body = "".join(_version(folder, study, v, steps[v["id"]]) for v in reversed(study["versions"]))
    return f'<section id="timeline"><h2>Timeline</h2>{_charts(study)}{body}</section>'


def _assumptions(study: dict) -> str:
    rows = []
    for a in study["assumptions"]:
        value = _num(a.get("value")) or esc(_range(a.get("check") or {}))
        rows.append([esc(a.get("text") or a.get("statement") or ""), value, esc(a.get("source") or "")])
    src = [[esc(s.get("text") or s.get("id") or ""), esc(Path(str(s["input"])).name if s.get("input") else "")]
           for s in study["sources"]]
    return ('<section id="assumptions"><h2>Assumptions and sources</h2>'
            + _table(["Assumption", "Value or range", "Source"], rows)
            + _table(["Source", "Input file"], src) + "</section>")


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
            + _cover(study, goals, steps, study_id) + '<div id="actions"></div>' + _status(study, goals, folder) + _target(study)
            + _timeline(folder, study, steps) + _assumptions(study)
            + f'<script type="application/json" id="study-json">{data}</script></main></body></html>\n')
    # Non-ASCII text becomes a character reference: the page stays ASCII and renders the same.
    return page.encode("ascii", "xmlcharrefreplace").decode("ascii")
