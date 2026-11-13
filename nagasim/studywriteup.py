"""On-request documents from a study folder: a paper-style write-up (HTML plus a Markdown twin) and a
slide deck (one HTML file). One data builder (`facts`) and one list of plain-text blocks per document;
the HTML renderers escape every string and the pages carry no script, like the control room.
The sensitivity chart is redrawn from the stored numbers, never read from the stored SVG."""
from __future__ import annotations

import math
import re
from pathlib import Path

from nagasim import player, study as study_mod, studyextensions, studypage, svgcharts
from nagasim.report import esc
from nagasim.studychecks import goal_kpi
from nagasim.pagefmt import REFERENCE, kpi_label
from nagasim.studypage import (_first, _goal_words, _hw, _is_share, _n, _page, _ascii, _prec, _read, _reps,
                               _validation_level, _value_text, _when)
from nagasim.twin import write_json, write_text


def _finite(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _span(spec: dict, kpi=None) -> str:
    lo, hi = _finite(spec.get("low")), _finite(spec.get("high"))
    if lo is None and hi is None:
        return ""
    if lo is None:
        return f"<= {_n(hi, kpi)}"
    return f">= {_n(lo, kpi)}" if hi is None else f"{_n(lo, kpi)} to {_n(hi, kpi)}"


def _references() -> list[str]:
    """The bullets under '## Sources' of the skill's method.md, continuation lines joined."""
    items: list[str] = []
    text = study_mod.skill_text("references/method.md").partition("## Sources")[2]
    for line in text.splitlines():
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif line.strip() and items:
            items[-1] += " " + line.strip()
    return items


def facts(folder: Path, study: dict) -> dict:
    """Everything both documents say, as plain (unescaped) data, from the study folder alone."""
    folder = Path(folder)
    if not study["versions"]:
        raise study_mod.StudyError("E_STUDY_NO_VERSION", "the study has no model version yet",
                                   "Build a version with study_step first.")
    steps = {v["id"]: _read(folder / "versions" / v["id"] / "step.json") or {} for v in study["versions"]}
    last = study["versions"][-1]
    step = steps[last["id"]]
    suff = study_mod.sufficiency(study)
    prec = _prec(last, step)
    sc = _first(last.get("scenarios"), step.get("scenarios")) or {}
    best = study_mod.scenarios_meeting(study)
    goals = []
    for spec, g in zip(study["goals"], study_mod.goals_status(study)):
        kpi = goal_kpi(spec)
        hw = (prec.get(kpi) or {}).get("half_width")
        num = (last.get("kpis") or {}).get(kpi) if kpi else None
        entry = {"text": g["text"], "kpi": kpi or "", "range": _span(spec, kpi), "status": g["status"],
                 "limit": _goal_words(spec, kpi), "interval": _value_text(num, hw, kpi),
                 "value": _n(num, kpi) if kpi else "", "half_width": _hw(hw, kpi), "num": num, "option": None}
        # D1237: the recommended option leads; the Baseline is its comparison, never the headline.
        mine = ((sc.get(best[0]) or {}).get("kpis") or {}).get(kpi) if best and kpi else None
        if mine and _finite(mine.get("mean")) is not None:
            entry["option"] = {"name": best[0], "num": mine["mean"],
                               "interval": _value_text(mine["mean"], mine.get("ci_half_width"), kpi),
                               "status": ((sc[best[0]].get("goals") or {}).get(spec.get("id")) or "not_verified"),
                               # D1264, D1265: "" for a record from before them
                               "diff": studypage.diff_text(mine.get("diff_vs_baseline"), kpi),
                               "steadier": studypage.steadier_text(g["text"], best[0], kpi, mine.get("spread"))}
        goals.append(entry)
    scen_rows = []
    options: dict = {}
    for name, r in sc.items():
        cells = [name]
        for spec in study["goals"]:
            e = (r.get("kpis") or {}).get(goal_kpi(spec)) or {}
            k = goal_kpi(spec)
            v = (r.get("goals") or {}).get(spec.get("id"), "not_verified").replace("_", " ")
            cells.append(f"{_value_text(e.get('mean'), e.get('ci_half_width'), k)}: {v}" if e else v)
        scen_rows.append(cells)
        for spec in study["goals"]:
            k = goal_kpi(spec)
            mean = ((r.get("kpis") or {}).get(k) or {}).get("mean")
            if k and isinstance(mean, (int, float)):
                options.setdefault(k, []).append((name, mean))
    model = _read(folder / "versions" / last["id"] / "model.json") or {}
    checks = study_mod.checklist(study, folder)
    return {
        "banner": "b-ok" if suff["sufficient"] else "b-open" if study.get("accepted") else "b-wait",
        "options": options,
        "question": study["question"], "decision": study["decision"], "label": suff["label"],
        "best": best,
        "version": last["id"], "replications": _reps(last, step), "seed": step.get("seed"),
        "precision": [(k, _hw(p.get("half_width"), k), _hw(p.get("target"), k)) for k, p in prec.items()
                      if isinstance(p, dict)],
        "uncertainties": studypage.trust_rows(folder, study, steps),
        "simulation": model.get("simulation") or {}, "model": model,
        "target": [str(c.get("name", "")) for c in study["target"]],
        "target_pending": [str(c.get("name", "")) for c in study["target"] if not c.get("blocks")],
        "goals": goals, "goal_heads": [goal_kpi(g) or g.get("id", "") for g in study["goals"]],
        "versions": [{"id": v["id"], "note": v.get("note", ""), "verdict": v.get("verdict", ""),
                      "kpis": v.get("kpis") or {}, "reps": _reps(v, steps[v["id"]])}
                     for v in study["versions"]],
        "scenarios": scen_rows,
        "assumptions": [[a.get("text") or a.get("statement") or "",
                         _n(a.get("value")) or _span(a.get("check") or {}), a.get("source") or ""]
                        for a in study["assumptions"]],
        "sources": [[s.get("text") or s.get("id") or "",
                     Path(str(s["input"])).name if s.get("input") else ""] for s in study["sources"]],
        "validation": [[v.get("level", "").replace("_", " "), studypage.validation_result(v),
                        study_mod.validation_note(v)] for v in study["validation"]],
        "level": _validation_level(study) + studypage._reported(study),
        "checks": [[c["title"], c["verdict"].replace("_", " "),
                    " ".join(x for x in (c["why"], studypage.revalidate_note(study, folder, None)
                                         if c["item"] == "validation" else "") if x)] for c in checks],
        "open": [c for c in checks if c["verdict"] != "passed"],
        "sensitivity": _read(folder / "versions" / last["id"] / "sensitivity.json"),
        "extensions": [f"{m['label']}: {m['purpose']}" for m in studyextensions.MODULES.values()],
        "references": _references(),
    }


# --- blocks (plain text; the renderers escape): ["p", text], ["ul", items], ["table", head, rows],
# ["fig", kind, caption] with kind "flow" or a KPI name for its sensitivity (swing) chart. -----------

def _answer(f: dict) -> str:
    best = f" Best option: {', '.join(f['best'])}." if f["best"] else ""
    return f"The study is {f['label']}.{best} Validation: {f['level']}."


def _headline(f: dict) -> str:
    """D1237: the recommended option against the Baseline, then the goal; "" with no option. With a
    paired difference in the record (D1263) its interval (D1264), else the difference of the means."""
    lines = []
    for g in f["goals"]:
        o = g["option"]
        if o and g["interval"]:
            gap = _hw(abs(o["num"] - g["num"]), g["kpi"]) if isinstance(g["num"], (int, float)) else ""
            said = (f": {o['diff']}" if o.get("diff") else f" (a difference of the means of {gap})" if gap else "")
            lines.append(f"{g['text']}: {o['interval']} with {o['name']}, against {g['interval']} in the "
                         f"{REFERENCE}{said}" + (f"; goal: {g['limit']}" if g["limit"] else "") + ".")
    return " ".join(lines)


def _goal_table(f: dict) -> list:
    base = [[g["interval"] or g["value"], g["status"].replace("_", " ")] for g in f["goals"]]
    if f["best"] and any(g["option"] for g in f["goals"]):
        # D1252: the recommended option's value and verdict lead, the Baseline's is the comparison.
        return ["table", ["Goal", "KPI", "Target", f"{f['best'][0]} value", "Status", f"{REFERENCE} value"],
                [[g["text"], g["kpi"], g["limit"], g["option"]["interval"] if g["option"] else "-",
                  (g["option"]["status"] if g["option"] else "not_verified").replace("_", " "), f"{v}: {s}"]
                 for g, (v, s) in zip(f["goals"], base)]]
    return ["table", ["Goal", "KPI", "Target", f"{REFERENCE} value", "Status"],
            [[g["text"], g["kpi"], g["limit"], v, s] for g, (v, s) in zip(f["goals"], base)]]


def _steadier(f: dict) -> list:
    """D1265: the goal rows' steadier-KPI lines, under the goal table of the write-up only."""
    return [["p", g["option"]["steadier"]] for g in f["goals"] if (g["option"] or {}).get("steadier")]


def _scen_table(f: dict) -> list:
    return ["table", ["Scenario"] + f["goal_heads"], f["scenarios"]]


def _sens_blocks(f: dict, caption) -> list:
    s = f["sensitivity"]
    if not s:
        return [["p", "No sensitivity analysis was run."]]
    rows = [[r["label"], _n(r["low"]), _n(r["high"]), "yes" if r["changes_answer"] else "no"]
            for r in s.get("factors") or []]
    out = [["p", f"One assumption at a time, {_n(s.get('delta', 0) * 100)}% either way (capacities by one), "
                 f"{s.get('replications')} replications, common random numbers."],
           ["table", ["Assumption", "Low", "High", "Changes the answer"], rows]]
    kpis = sorted({k for r in s.get("factors") or [] for k in r.get("kpis") or {}})
    return out + [["fig", k, caption(f"swing of {k} by assumption")] for k in kpis]


def _next_steps(f: dict) -> list:
    if not f["open"]:
        return [["p", "No checklist item is open."]]
    return [["ul", [c["title"] + (f": {c['why']}" if c["why"] else "") for c in f["open"]]]]


def _writeup_sections(f: dict, discussion: str | None) -> list:
    n = [0]

    def caption(text: str) -> str:
        n[0] += 1
        return f"Figure {n[0]}: {text}"

    sim = f["simulation"]
    horizon = f"Run length {_n(sim.get('time_limit'))}, warm-up {_n(sim.get('warmup'))}." if sim else ""
    reps = f"{f['replications']} replications, seed {f['seed']}." if f["replications"] else ""
    prec = [["ul", [f"{kpi_label(k)}: 95% half-width {w} (target {t})" for k, w, t in f["precision"]]]] \
        if f["precision"] else []
    scope = ("In scope: " + ", ".join(f["target"]) + ".") if f["target"] else "The scope was not listed."
    pending = [["p", "Not built yet: " + ", ".join(f["target_pending"]) + "."]] if f["target_pending"] else []
    parts = [[str(b.get("name", "")), str(b.get("type", ""))] for b in f["model"].get("blocks") or []]
    goals = "; ".join(f"{g['text']}: {g['interval'] or g['value'] or 'n/a'}"
                      + f" ({g['status'].replace('_', ' ')})" for g in f["goals"])
    lead = _headline(f)
    return [
        ("Abstract", [["p", f"Question: {f['question']}"], ["p", _answer(f)]] + ([["p", lead]] if lead else [])
         + [["p", f"{REFERENCE}: {goals}"]]),
        ("1 Objective", [["p", f"Question: {f['question']}"],
                         ["p", f"Decision: {f['decision'] or 'not recorded'}"], _goal_table(f)]),
        ("2 Model logic", [["p", scope], ["fig", "flow", caption(f"flowchart of {f['version']}")]] + pending
         + [["table", ["Component", "Type"], parts]]),
        ("3 Data and assumptions", [["table", ["Assumption", "Value or range", "Source"], f["assumptions"]],
                                    ["table", ["Source", "Input file"], f["sources"]]]),
        ("4 Experimentation", [["p", " ".join(x for x in (reps, horizon) if x) or "No run recorded."]] + prec
         + ([_scen_table(f)] if f["scenarios"] else [["p", "No scenarios were run."]])),
        ("5 Verification and validation",
         [["p", f"Validation level: {f['level']}."], ["table", ["Level", "Result", "Note"], f["validation"]],
          ["table", ["Step", "Verdict", "Reason"], f["checks"]]]),
        ("6 Results", [_goal_table(f)] + _steadier(f) + [
                       ["table", ["Version", "Note", "Verdict"] + f["goal_heads"] + ["Replications"],
                        [[v["id"], v["note"], v["verdict"].replace("_", " ")]
                         + [_n(v["kpis"].get(k), k) for k in f["goal_heads"]] + [_n(v["reps"])]
                         for v in f["versions"]]]]
         + ([_scen_table(f)] if f["scenarios"] else []) + _sens_blocks(f, caption)),
        ("7 Limitations", _next_steps(f)
         + [["p", "Candidate extension modules the study can add (none is part of these results):"],
            ["ul", f["extensions"]]]),
        ("8 Discussion", [["p", discussion or "No discussion provided."]]),
        ("References", [["refs", f["references"]]]),
    ]


# --- renderers --------------------------------------------------------------------------------------

def _html_block(b: list, f: dict, folder: Path, study: dict) -> str:
    kind = b[0]
    if kind == "p":
        return f"<p>{esc(b[1])}</p>"
    if kind == "ul":
        return "<ul>" + "".join(f"<li>{esc(i)}</li>" for i in b[1]) + "</ul>"
    if kind == "refs":      # method.md marks titles *like this*; escape first, then emphasise
        return "<ul>" + "".join("<li>" + re.sub(r"\*([^*]+)\*", r"<em>\1</em>", esc(i)) + "</li>"
                                for i in b[1]) + "</ul>"
    if kind == "banner":
        return f'<div class="banner {b[1]}">{esc(b[2])}</div>'
    if kind == "tiles":
        return '<div class="tiles">' + "".join(_tile(g) for g in b[1]) + "</div>"
    if kind == "bars":
        return '<div class="charts">' + "".join(
            f'<div class="flow">{svgcharts.bar_chart(k, [n for n, _ in rows], [v for _, v in rows], "%" if sh else "")}</div>'
            for k, rows, sh in b[1]) + "</div>"
    if kind == "table":
        return studypage._table(b[1], [[esc(c) for c in r] for r in b[2]]) if b[2] else ""
    if b[1] == "flow":
        v = study["versions"][-1]
        svg = studypage._flow(folder, study, v, _read(folder / "versions" / v["id"] / "step.json") or {})
    else:
        rows = sorted(f["sensitivity"]["factors"],
                      key=lambda r: -((r["kpis"].get(b[1]) or {}).get("swing") or 0))
        svg = '<div class="flow">' + svgcharts.bar_chart(
            f"Swing of {b[1]} by assumption", [r["label"] for r in rows],
            [(r["kpis"].get(b[1]) or {}).get("swing") or 0.0 for r in rows], "swing") + "</div>"
    return f"<figure>{svg}<figcaption>{esc(b[2])}</figcaption></figure>"


def _split(text: str) -> tuple[str, str]:
    """"27.6% (95% interval 24.0 to 31.2)" -> ("27.6%", "(95% interval 24.0 to 31.2)")."""
    head, sep, rest = text.partition(" (")
    return head, (sep.strip() + rest) if sep else ""


def _tile(g: dict) -> str:
    """One goal's tile: the recommended option's value and verdict when there is one (D1237), the
    Baseline under it as the comparison; else the Baseline's."""
    o = g["option"]
    big, pm = _split((o or {}).get("interval") or g["interval"] or g["value"] or "n/a")
    out = (f'<div class="tile"><div class="big">{esc(big)}'
           + (f'<span class="pm"> {esc(pm)}</span>' if pm else "") + "</div>"
           + f'<div>{esc(g["text"])}' + (f", with {esc(o['name'])}" if o else "")
           + (f"; goal {esc(g['limit'])}" if g["limit"] else "") + "</div>"
           + studypage._badge(o["status"] if o else g["status"]))
    if o:
        out += (f'<div class="pm">{REFERENCE}: {esc(g["interval"] or g["value"] or "n/a")}, '
                f'{esc(g["status"].replace("_", " "))}</div>')
    return out + "</div>"


def _md_cell(c) -> str:
    return str(c).replace("|", "\\|").replace("\n", " ")


def _md_block(b: list) -> str:
    kind = b[0]
    if kind == "p":
        return b[1]
    if kind in ("ul", "refs"):
        return "\n".join(f"- {i}" for i in b[1])
    if kind == "table":
        if not b[2]:
            return ""
        head = "| " + " | ".join(_md_cell(h) for h in b[1]) + " |\n|" + "---|" * len(b[1])
        return head + "\n" + "\n".join("| " + " | ".join(_md_cell(c) for c in r) + " |" for r in b[2])
    return f"{b[2]} (drawn in the HTML version)"


# D1240: the reader's light or dark (`player.token_css`); print stays light; figures on a light card.
_DOC_CSS = player.token_css() + """
body{font:15px/1.5 Georgia,serif;color:var(--ink);margin:0;background:var(--panel)}
main{max-width:820px;margin:0 auto;padding:24px 16px}
h1{font-size:24px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid var(--line)}
table{border-collapse:collapse;width:100%;margin:8px 0;font:13px system-ui,Arial,sans-serif}
th,td{text-align:left;padding:3px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
figure{margin:12px 0}figcaption{font-size:13px;color:var(--muted)}.flow{overflow-x:auto;background:#fff;border-radius:6px}
.flow svg{max-width:100%;height:auto}
@media print{h2{break-after:avoid}figure,table{break-inside:avoid}body{background:#fff}}
"""


def _stamp(out: Path, key: str, study: dict) -> None:
    """Record which version a document was built from, so the package lists only current ones."""
    stamps = _read(out / "outputs.json") or {}
    stamps[key] = study["versions"][-1]["id"]
    write_json(out / "outputs.json", stamps)


def _package(folder: Path) -> Path:
    out = Path(folder) / "package"
    out.mkdir(exist_ok=True)
    return out


def writeup(folder: Path, study: dict, discussion: str | None = None) -> tuple[Path, Path]:
    """`package/writeup.html` and `package/writeup.md`: the same sections, figures in the HTML only.
    `discussion` is the assistant's own text for section 8, escaped like any study string."""
    folder = Path(folder)
    f = facts(folder, study)
    sections = _writeup_sections(f, discussion)
    html = f"<main><h1>{esc(f['question'])}</h1>" + "".join(
        f"<h2>{esc(h)}</h2>" + "".join(_html_block(b, f, folder, study) for b in blocks)
        for h, blocks in sections) + "</main>"
    md = f"# {f['question']}\n\n" + "\n\n".join(
        f"## {h}\n\n" + "\n\n".join(x for x in map(_md_block, blocks) if x) for h, blocks in sections) + "\n"
    out = _package(folder)
    write_text(out / "writeup.html", _page(f["question"], _DOC_CSS, html))
    write_text(out / "writeup.md", _ascii(md))
    _stamp(out, "writeup", study)
    return out / "writeup.html", out / "writeup.md"


# --- slides -----------------------------------------------------------------------------------------

OPTION_ROWS = 4        # table rows per "Options compared" slide: more would be cut by the card
OPTION_ROWS_WITH_CHART = 2   # the slide that also holds the bar chart (#1759: four ran 48 px past it)

# D1240: the reader's light or dark; print stays light. S4-18: under 700 px a 16:9 card would
# scale its type to 4 px, so the slides become a plain column of cards in readable sizes.
_SLIDE_CSS = player.token_css() + """
html{scroll-snap-type:y mandatory}
body{margin:0;background:var(--page);color:var(--ink);font-family:var(--font)}
main{padding:24px 16px}
.slide{container-type:inline-size;position:relative;box-sizing:border-box;scroll-snap-align:center;
 width:min(100%,1200px);aspect-ratio:16/9;margin:0 auto 24px;padding:3.2cqw 4.5cqw 6cqw;background:var(--panel);
 overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.18);font-size:2cqw;line-height:1.35}
.slide h1{font-size:3.7cqw;margin:6cqw 0 2cqw}.slide h2{font-size:3.7cqw;margin:0 0 1.6cqw}
.slide p{margin:.6cqw 0}
.slide table{font-size:1.5cqw;border-collapse:collapse;width:100%}
.slide th,.slide td{text-align:left;padding:.4cqw .8cqw;border-bottom:1px solid var(--line)}
.slide figure{margin:0}.slide .flow svg{width:100%;height:auto;max-height:34cqw}
.flow{background:#fff;border-radius:6px}
.slide figcaption{font-size:1.4cqw;color:var(--muted)}
.banner{padding:1.2cqw 2cqw;border-radius:.8cqw;font-weight:600;margin:1cqw 0}
.b-ok{background:var(--ok-bg);color:var(--ok-ink)}.b-wait{background:var(--wait-bg);color:var(--wait-ink)}
.b-open{background:var(--open-bg);color:var(--open-ink)}
.tiles{display:flex;flex-wrap:wrap;gap:1.4cqw;margin-top:1.4cqw}
.tile{flex:1 1 18cqw;border:1px solid var(--line);border-radius:.8cqw;padding:1.2cqw 1.6cqw;background:var(--raised)}
.big{font-size:4cqw;font-weight:700;line-height:1.1}.pm{font-size:1.8cqw;font-weight:400;color:var(--muted)}
.badge{font-size:1.4cqw}
.charts{display:flex;gap:1cqw}.charts .flow{flex:1;min-width:0}.charts svg{width:100%;height:auto;max-height:22cqw}
.foot{position:absolute;left:4.5cqw;right:4.5cqw;bottom:1.6cqw;display:flex;justify-content:space-between;
 gap:2cqw;font-size:1.3cqw;color:var(--muted)}
.foot span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media screen and (max-width:700px){html{scroll-snap-type:none}main{padding:12px}
 .slide{container-type:normal;aspect-ratio:auto;padding:16px;font-size:15px;overflow-x:auto;margin-bottom:12px}
 .slide h1,.slide h2{font-size:20px;margin:0 0 10px}.slide p{margin:6px 0}
 .slide table{font-size:13px}.slide th,.slide td{padding:4px 6px}
 .slide .flow svg,.charts svg{max-height:none}.slide figcaption{font-size:13px}
 .banner{padding:8px 12px;border-radius:6px;margin:8px 0}.tiles{gap:8px;margin-top:8px}
 .tile{flex-basis:100%;padding:10px 12px;border-radius:6px}.big{font-size:26px}.pm{font-size:14px}
 .badge{font-size:12px}.charts{flex-direction:column;gap:8px}
 .foot{position:static;margin-top:12px;font-size:12px;gap:8px}}
@page{size:16in 9in;margin:0}
@media print{html{scroll-snap-type:none}body{background:#fff}main{padding:0}
 .slide{width:16in;height:9in;max-width:none;margin:0;box-shadow:none;break-after:page;page-break-after:always}}
"""


def _options_slides(f: dict, charts: list, base: list) -> list:
    """The "Options compared" slide, continued on further slides every OPTION_ROWS rows: a card
    clips what does not fit (#1746), so no row is left to overflow."""
    scenarios = [[f"{i}. {r[0]}"] + r[1:] for i, r in enumerate(f["scenarios"], 1)] if charts else f["scenarios"]
    rows = [base] + scenarios
    head = ["Option"] + f["goal_heads"]
    first = OPTION_ROWS_WITH_CHART if charts else OPTION_ROWS
    out = [("Options compared", ([["bars", charts]] if charts else []) + [["table", head, rows[:first]]])]
    for at in range(first, len(rows), OPTION_ROWS):
        out.append(("Options compared (continued)", [["table", head, rows[at:at + OPTION_ROWS]]]))
    return out


def _slide_list(f: dict) -> list:
    base = [f"{REFERENCE} ({f['version']})"] + [f"{g['interval'] or g['value']}: {g['status'].replace('_', ' ')}"
                                                for g in f["goals"]]
    charts = []
    for g in f["goals"]:
        rows = f["options"].get(g["kpi"])
        if rows and isinstance(g["num"], (int, float)):
            sh = _is_share(g["kpi"])
            # the bars are labelled "base", "1", "2" ...: names do not fit under six or more bars; the
            # table beside them carries the same numbers
            charts.append((g["kpi"], [(n, v * 100 if sh and 0 <= v <= 1 else v)
                                      for n, v in zip([REFERENCE] + [str(i) for i in range(1, len(rows) + 1)],
                                                      [g["num"]] + [v for _, v in rows])], sh))
    lead = _headline(f)
    # A decision is free text recorded once; its numbers may be older than the last version (S2-08).
    dated = ([["p", f"Numbers written in the decision are as recorded then; the results of {f['version']} "
                    "follow."]] if re.search(r"\d", f["decision"] or "") else [])
    slides = [(f["question"], [["p", f"Decision: {f['decision'] or 'not recorded'}"]] + dated),
              ("The answer", [["banner", f["banner"], f"The study is {f['label']}."
                               + (f" Best option: {', '.join(f['best'])}." if f["best"] else "")],
                              ["tiles", f["goals"]]] + ([["p", lead]] if lead else [])
               + [["p", f"Validation: {f['level']}."]]),
              *_options_slides(f, charts, base),
              ("Goals and verdict", [_goal_table(f)]),
              ("The model", [["fig", "flow", f"Flowchart of {f['version']}"]]),
              ("Assumptions", [["table", ["Assumption", "Value or range", "Source"], f["assumptions"]]]),
              ("Validation and precision",
               [["p", f"Validation level: {f['level']}."], ["table", ["Level", "Result", "Note"], f["validation"]],
                ["p", f"{f['replications'] or 'n/a'} replications"
                      + "".join(f"; {kpi_label(k)}: 95% half-width {w} (target {t})" for k, w, t in f["precision"])],
                ["table", ["Uncertainty", "Basis"], f["uncertainties"]]])]
    if f["sensitivity"]:
        slides.append(("Sensitivity", _sens_blocks(f, lambda c: c.capitalize())))
    return slides + [("Next steps", _next_steps(f))]


def slides(folder: Path, study: dict) -> Path:
    """`package/slides.html`: one 16:9 card per slide; scroll-snap on screen, one slide per printed page."""
    folder = Path(folder)
    f = facts(folder, study)
    items = _slide_list(f)
    short = f["question"] if len(f["question"]) <= 70 else f["question"][:67] + "..."
    parts = [f'<section class="slide"><{"h1" if i == 0 else "h2"}>{esc(title)}</{"h1" if i == 0 else "h2"}>'
             + "".join(_html_block(b, f, folder, study) for b in blocks)
             + f'<div class="foot"><span>{esc(short)}</span><span>{i + 1} / {len(items)}</span></div></section>'
             for i, (title, blocks) in enumerate(items)]
    path = _package(folder) / "slides.html"
    write_text(path, _page(f["question"], _SLIDE_CSS, "<main>" + "".join(parts) + "</main>"))
    _stamp(path.parent, "slides", study)
    return path
