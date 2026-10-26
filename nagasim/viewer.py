"""The browser viewer (D320-D333): one HTML file in which this engine, unmodified, runs
in a browser and re-runs a model, for client staff to explore and show around. It is
not an operational screen (that is the Presenter and the monitor) and not a confidential
share (that is the player, `player.py`): whoever receives the file can read the model.

    viewer_problems(layout)                why a model cannot be a viewer, or []
    what_if(layout, values)                the run both sides make: the builder for the
                                           author's baseline, the page for every Run
    answer(row, replications)              the answer card's words for a result (D707)
    page_run(layout, baseline, args)       what a Run in the page answers
    compare(record, baseline)              is the page's baseline the author's
    build_viewer(layout, path)             the page
    freeze_data(layout, base_dir)          the copy of a model with its data read once and
                                           written in, for a model fed from files

The inputs, results and limits are the model's `dashboard` definition
(`dashboard.resolve_dashboard`), so the page holds what the Presenter holds and nothing
the author did not expose.
"""
from __future__ import annotations

from nagasim._cli import add_model_argument

import json
import math
import os
import re
import time

from nagasim import dashboard, experiments
from nagasim.core.problems import Problem, problem
from nagasim.core.schema.sections_validation import DASHBOARD_LIMITS
from nagasim.core.timeunits import units_of
from nagasim.core.utils import is_finite_number
from nagasim import player
from nagasim.viewer_runtime import Cancelled

__all__ = ["MAX_MODEL_BYTES", "Cancelled", "engine_on_disk", "viewer_problems", "freeze_data",
           "what_if", "answer", "page_run", "compare", "build_viewer", "read_asked"]

# The model text a page may carry. The largest bundled model is 0.25 MB.
MAX_MODEL_BYTES = 5_000_000
# Sections that do not change a run and may name a file of the author's machine.
_NOT_SHIPPED = ("view", "background")
# A run in a browser against the same run here (measured: 1.3 to 2.8 times).
_BROWSER_FACTOR = 2.5
_HINT = "share a recording instead: `naga run MODEL --player FILE.html`"
_QUOTED = 10        # recorded answers a page carries: the latest of each kind is what is read
# An `--asked` file: a study version's asked.json holds at most 100 rows of a few hundred bytes.
ASKED_BYTES = 1_000_000
# One help text for every command that takes `--asked FILE` (viewer, run --player, experiment).
ASKED_HELP = ("recorded answers for the trust card: a study version's asked.json, or a file of "
              "the editor's answers store; its input-data and parameter rows quote them "
              "instead of 'not measured'")


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
            on_progress=None, should_stop=None, asked=None) -> dict:
    """Run *layout* with the viewer's *values* ({input path: value}) beside its baseline
    and return one frozen record (D248), JSON-safe. *asked*: the recorded tool answers of
    the model (`agent_tools.core.recorded`), quoted by the trust card and the input note:

        inputs, replications, seed, seeds   what made it
        rows      per result KPI and scenario: mean, 95% interval, difference to the
                  baseline with its paired interval, significant
        cells     {scenario: {KPI: [the value of each replication]}}, every KPI
        results   {scenario: the results panel's rows} (`player.result_rows`: target,
                  direction, verdict word)
        trust     {scenario: the trust card} (`player.trust_card`: law checks, warm-up,
                  input data, parameters)
        answer    the answer card for the first result (`answer`), or None
        input_note  (when *asked* measured the input data) the answer card's input-data
                  line, `uncertainty.what_if_note`: quoted beside the verdict, never merged
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
    made, name = dashboard.what_if_layout(layout, values)       # ValueError
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
    started = time.perf_counter()
    result = experiments.run_scenarios(made, [name], replications=count, seed=seed,
                                       workers=1, on_progress=on_progress,
                                       should_stop=should_stop)
    seconds = time.perf_counter() - started
    if not result.completed:
        raise Cancelled("the run was cancelled")

    labels = {entry["kpi"]: entry.get("label") or entry["kpi"] for entry in definition["results"]}
    rows, needed, diff = [], None, None
    if labels:
        frame = result.comparison(kpis=list(labels))
        first = frame[(frame["KPI"] == next(iter(labels))) & (frame["Scenario"] == name)]
        if len(first):
            needed = _number(first["Replications Needed"].iloc[0])
            diff = (_number(first["Diff LB"].iloc[0]), _number(first["Diff UB"].iloc[0]))
        order = {kpi: index for index, kpi in enumerate(labels)}
        for row in sorted(frame.to_dict("records"), key=lambda r: order.get(r["KPI"], 0)):
            rows.append({"kpi": row["KPI"], "label": labels.get(row["KPI"], row["KPI"]),
                         "scenario": dashboard.YOURS if row["Scenario"] == name else row["Scenario"],
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
        cells[dashboard.YOURS if scenario == name else scenario] = table
    # The console's panels (D704, D707), from the same comparison the rows are.
    names = [name, experiments.BASELINE]
    panel = player.result_rows(made, None, result, names) or {}
    trust = player.trust_card(made, None, result, names, {name: made["scenarios"][-1]["overrides"]},
                              panel, {scenario: (seed, count) for scenario in names}, asked) or {}
    shown = panel.get(name if values else experiments.BASELINE) or [None]
    from nagasim import uncertainty

    note = uncertainty.what_if_note(asked, made["scenarios"][-1]["overrides"], list(labels)) \
        if values else None
    return {**({"input_note": player.plain(note)} if note else {}),
            "inputs": {path: item["value"] for path, item in
                       zip(values, made["scenarios"][-1]["overrides"])},
            "replications": count, "seed": seed,
            "seeds": [int(s) for s in result.seeds.get(experiments.BASELINE, [])],
            "rows": rows, "cells": cells,
            "results": player.plain({_renamed(key, name): value for key, value in panel.items()}),
            "trust": player.plain({_renamed(key, name): value for key, value in trust.items()}),
            "answer": answer(player.plain(shown[0]), count, needed, diff),
            "versions": _versions(), "seconds": seconds}


def _renamed(scenario: str, name: str) -> str:
    return dashboard.YOURS if scenario == name else scenario


_WORDS = {"yes": "Yes, very likely", "no": "No", "unclear": "Not clear yet",
          "same": "No change you would notice"}
_FROM_VERDICT = {"better": "yes", "worse": "no", "no clear change": "unclear"}
# D710: a difference whose whole 95% interval is within this share of the baseline mean
# is no change a client would notice, and running on would not change that.
_UNNOTICED = 0.01


def answer(row: dict | None, replications: int, needed: float | None = None,
           diff: tuple | None = None) -> dict | None:
    """The answer card (D707) for *row*, a results panel row (`player.result_rows`), as
    {"word": "yes" | "no" | "unclear" | "same", "headline", "detail", "needed"}: computed
    from the numbers, never from a colour. With a target and a direction: "yes" when the
    whole 95% interval is on the better side of the target, "no" when it is all on the
    worse side, else "unclear". Without a target: the verdict against the baseline
    (better, worse, no clear change), and "same" (final, D710) in place of "unclear" when
    *diff*, the 95% interval (low, high) of the difference, is within +/-1% of the
    baseline mean. None when neither says anything (no direction, or no target and no
    baseline). *needed* is the comparison's `Replications Needed`; for a target it is
    worked out the same way, n x (half-width / distance)^2, uncapped (the cap is
    `_until_clear`'s, #849)."""
    if not row or row["yours"]["mean"] is None:
        return None
    mean, low, high = (row["yours"][key] for key in ("mean", "lo", "hi"))
    target, better, sure = row.get("target"), row.get("better"), low is not None and high is not None
    if target is not None and better in ("higher", "lower"):
        sign = 1 if better == "higher" else -1
        worst, best = (low, high) if sign > 0 else (high, low)
        word = "unclear"
        if sure and sign * (worst - target) >= 0:
            word = "yes"
        elif sure and sign * (best - target) < 0:
            word = "no"
        needed = None
        if word == "unclear" and sure and mean != target:
            needed = math.ceil(replications * ((high - low) / 2 / abs(mean - target)) ** 2)
        side = "above" if mean > target else "below" if mean < target else "at"
        said = f"; {side} the {target:.4g} target"
    elif row.get("base") is not None and row.get("verdict") in _FROM_VERDICT:
        word, said = _FROM_VERDICT[row["verdict"]], ""
        base = row["base"]["mean"]
        if (word == "unclear" and base and diff and None not in diff
                and max(abs(diff[0]), abs(diff[1])) <= _UNNOTICED * abs(base)):
            word, said = "same", f"; the difference is within {100 * _UNNOTICED:g}% of the baseline"
    else:
        return None
    unit = f" {row['unit']}" if row.get("unit") else ""
    detail = f"{row['label']} {mean:.4g}{unit}"
    if sure:
        detail += f" (95% interval {low:.4g} to {high:.4g})"
    if row.get("delta_pct") is not None:
        detail += f", {row['delta_pct']:+.1f}% against the baseline ({row['verdict']})"
    return {"word": word, "headline": f"{_WORDS[word]}: {mean:.4g}{unit}", "detail": detail + said + ".",
            "needed": int(needed) if word == "unclear" and needed is not None else None}


def _until_clear(layout: dict, values: dict, count, on_progress, say, asked=None) -> dict:
    """`what_if`, run again with more replications while its answer is "not clear yet"
    (the comparison's `Replications Needed`, D685), up to the definition's most: the one
    cap on replications (#849). Each round re-runs all replications, not only the new
    ones: common random numbers make adding to a finished run awkward."""
    limits = dashboard.resolve_dashboard(layout)["limits"]
    most = min(int(limits["max_replications"]), DASHBOARD_LIMITS["replications"])
    record = what_if(layout, values, replications=count, on_progress=on_progress, asked=asked)
    while record["answer"] and record["answer"]["word"] == "unclear" and record["replications"] < most:
        done = record["replications"]
        needed = record["answer"]["needed"] or 2 * done
        count = min(most, max(done + 1, needed))
        say(f"Not clear yet after {done} replications: running {count} for a clear answer" if needed <= most
            else f"Not clear yet after {done} replications: running {count}, the most this page runs, "
                 "to settle it")
        record = what_if(layout, values, replications=count, on_progress=on_progress, asked=asked)
    if record["answer"] and record["answer"]["word"] == "unclear":
        record["answer"]["detail"] += (f" Still not clear after {record['replications']} "
                                       "replications, the most this page runs.")
    return record


def page_run(layout: dict, baseline: dict, args: dict, *, title: str = "Model",
             tokens: dict | None = None, on_progress=None, say=None, asked=None) -> dict:
    """What a Run in the page answers (the worker's `_BOOT`): {"record", "same",
    "player"}, or {"error"} for a value the engine refuses. *asked*: the recorded answers
    the page carries (`build_viewer`); no input note against another scenario, since a
    difference measured against the author's baseline is not this one's.

    *args* is {"values", "replications"} and, to compare two of the page's scenarios,
    "against" (the values the other one is run with, in place of the author's baseline)
    and "names" (the two scenarios' names for the drawing). The run goes on until its
    answer is clear (`_until_clear`); *say* is told what happens. Then yours and the
    other are recorded for the player (`watch_page`): that is an extra, and a failure
    there (a browser tab out of memory, #833) keeps the finished result."""
    say = say or (lambda text: None)
    values, against = dict(args.get("values") or {}), dict(args.get("against") or {})
    names = [str(name)[:40] for name in args.get("names") or (dashboard.YOURS, experiments.BASELINE)][:2]
    try:
        base = (experiments.apply_overrides(layout, dashboard.input_overrides(layout, against))
                if against else layout)
        record = _until_clear(base, values, args.get("replications"), on_progress, say, asked)
        if against:
            record.pop("input_note", None)
    except ValueError as exc:
        return {"error": str(exc)[:500]}
    say("Recording your run")
    try:
        watch = watch_page(base, values, title=title, tokens=tokens, names=names)
    except Exception:                               # noqa: BLE001 (#833: any engine error)
        watch = ""
    return {"record": record, "same": None if against else compare(record, baseline), "player": watch}


def watch_page(layout: dict, values: dict | None = None, *, title: str = "Model",
               tokens: dict | None = None, names=None) -> str:
    """The player page (`player.build_player`) of the viewer's what-if beside the author's
    baseline: one replication of each with the model's own seed, recorded where this runs
    (in a page: the browser) and drawn as two scenarios, *names* (default "Yours" and
    "Baseline"): the other first, as the one compared with, and yours shown first. ""
    when the recording is over the player's size limit.
    Raises ValueError as `what_if` does."""
    from nagasim.api import run_model
    from nagasim.core.trace import TraceRecorder

    yours, other = names or (dashboard.YOURS, experiments.BASELINE)
    overrides = dashboard.input_overrides(layout, dict(values or {}))
    # Drawn with only yours as a stored scenario, so a name cannot pick up another's overrides.
    drawn = {**layout, "scenarios": [{"name": yours, "overrides": overrides}]}
    seed = int((layout.get("simulation") or {}).get("seed") or 0)
    recordings = {}
    for scenario, model in ((other, layout), (yours, experiments.apply_overrides(layout, overrides))):
        recordings[scenario] = TraceRecorder()
        run_model(model, replications=1, seed=seed, trace=recordings[scenario])
    try:
        return player.build_player(drawn, recordings, title=title, tokens=tokens, start=yours)
    except ValueError:
        return ""


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
#   player's `embed`) and is put on the page with `textContent`.
# * The engine runs in a module worker made from a `data:` URL: an origin of its own,
#   which cannot read a file beside the page in any browser (measured: Firefox lets the
#   main thread and a `blob:` worker do so). The worker answers Pyodide's `fetch` from
#   the embedded bytes only.
# * Embedded, each as released: Pyodide's core files and wheels (`viewer_runtime`), the
#   author's installed simpy, openpyxl and et_xmlfile, and this package without its
#   example models and without `core/custom_logic.py` (the author's own code, which only
#   a LOGIC block imports, and a viewer refuses those).
# The viewer's own rules, after the tokens and the rules it shares with the player
# (`player.style_sheet`, D697): one style sheet, pinned by its hash.
_CSS = """#status { margin: 8px 0; padding: 6px 10px; background: var(--panel); border-left: 3px solid var(--accent);
          border-radius: var(--radius); line-height: 1.4; min-height: calc(2.8em + 12px); }
/* Lines whose text changes keep their height and width (bug 841). */
#status.warn { border-left-color: var(--down); }
.warn { color: var(--down); }
.unit { color: var(--muted); }
.lever { padding: 8px 0; border-bottom: 1px solid var(--line); }
.lever-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.lever-head label { min-width: 0; overflow-wrap: anywhere; }
.lever-head input[type=number] { width: 88px; padding: 2px 6px; text-align: right; font-weight: 600;
                                 font-variant-numeric: tabular-nums; background: var(--raised);
                                 border: 1px solid var(--line); border-radius: var(--radius); }
.lever-track { position: relative; }
.lever-track input[type=range] { display: block; width: 100%; margin: 6px 0 2px; accent-color: var(--accent); }
.lever-default { position: absolute; top: 2px; bottom: 0; width: 2px; margin-left: -1px; background: var(--base);
                 pointer-events: none; }
.lever-foot { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 2px 8px; color: var(--muted);
              font-size: 11px; }
.lever-state.moved { color: var(--ink); font-weight: 600; }
#changed { color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase;
            display: inline-block; min-width: 11ch; text-align: right; }
.reps { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin: 10px 0 4px; }
.reps input { width: 70px; padding: 2px 6px; background: var(--raised); border: 1px solid var(--line);
              border-radius: var(--radius); }
.actions { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.actions button { width: 100%; padding: 8px; }
#progress { display: block; min-height: 1.4em; font-variant-numeric: tabular-nums; }
.answer { border-left: 4px solid var(--idle); }
.answer.yes { border-left-color: var(--run); }
.answer.no { border-left-color: var(--down); }
.answer.unclear { border-left-color: var(--blocked); }
.kicker { margin: 0 0 4px; color: var(--muted); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
#answer-head { margin: 0 0 4px; font-size: 18px; font-weight: 700; min-height: 1.4em; }
#answer-detail { line-height: 1.4; min-height: 4.2em; }
.stale #results, .stale #answer-box, .stale #results-table { opacity: 0.45; }
#player { display: block; width: 100%; height: 62vh; min-height: 420px; border: 0; }
/* One column: the frame's own panels stack too, so it needs the height of all of them. */
@media (max-width: 900px) { #player { height: 820px; } }
.footbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; padding: 8px 12px;
           background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
.footbar .note { flex: 1 1 240px; text-align: right; font-size: 12px; }
#compare-pick { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
#compare-pick[hidden] { display: none; }
#picture-canvas { display: block; max-width: 100%; height: auto; border: 1px solid var(--line); }
pre { white-space: pre-wrap; font-size: 11px; max-height: 420px; overflow: auto; border: 1px solid var(--line);
      padding: 8px; }
details { margin-top: 10px; }
.sig { font-weight: 600; }
.gone { display: none; }
/* Its line is kept while hidden, so the results below it do not move when it shows. */
#stale { min-height: 1.4em; margin: 2px 0; }
#stale.gone { display: block; visibility: hidden; }
"""

# What the engine is asked, in the worker's Python. MODEL and BASELINE are the texts of
# the data blocks, never read by the main thread's JavaScript on the way.
_BOOT = """
import json
from nagasim import viewer
_LAYOUT = json.loads(MODEL)
_BASE = json.loads(BASELINE)
_PAGE = json.loads(PAGE)
def _run(args):
    return json.dumps(viewer.page_run(_LAYOUT, _BASE, json.loads(args), title=_PAGE["title"],
                                      tokens=_PAGE["tokens"], on_progress=PROGRESS, say=STATUS,
                                      asked=_PAGE.get("asked")),
                      allow_nan=False)
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
  py.globals.set("PAGE", cfg.page);
  py.globals.set("PROGRESS", function (done, total) { postMessage(["progress", done, total]); });
  py.globals.set("STATUS", function (text) { postMessage(["status", String(text)]); });
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

/* The values of *now* that differ from *initial*: what a run sets on top of initial. */
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

/* How long a run of *count* replications takes: those, and the two the page records
   for the drawing, yours and the other (#834). */
function estimateSeconds(perReplication, count) { return perReplication * (count + 2); }

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

/* The saved scenarios read back from this browser (D709): the text is anyone's, so only
   the page's own inputs and plain values are kept, at most 20. */
function savedFrom(text, paths) {
  var list, out = [];
  try { list = JSON.parse(text); } catch (e) { return out; }
  if (!Array.isArray(list)) { return out; }
  list.slice(0, 20).forEach(function (item) {
    var values = {};
    if (!item || typeof item !== "object") { return; }
    paths.forEach(function (path) {
      if (Object.prototype.hasOwnProperty.call(item, path) &&
          ["number", "boolean", "string"].indexOf(typeof item[path]) >= 0) { values[path] = item[path]; }
    });
    out.push(values);
  });
  return out;
}

/* *text* in lines no wider than *width* on the canvas pen. */
function wrap(pen, text, width) {
  var out = [], line = "";
  String(text).split(" ").forEach(function (word) {
    var next = line ? line + " " + word : word;
    if (line && pen.measureText(next).width > width) { out.push(line); line = word; } else { line = next; }
  });
  if (line) { out.push(line); }
  return out;
}

""" + player.PANELS + """
/* ---- the page half ---- */

function boot() {
  var el = function (id) { return document.getElementById(id); };
  var D = JSON.parse(el("data").textContent);
  var modelText = el("model").textContent, baselineText = el("baseline").textContent;
  var author = JSON.parse(baselineText);
  var paths = D.inputs.map(function (entry) { return entry.path; });
  /* Saved scenarios are kept per model: its title and its inputs. */
  var KEY = "naga-viewer:" + D.title + ":" + paths.join("|");
  var state = {worker: null, ready: false, starting: false, stopped: false, pending: null,
               running: false, shown: null, since: 0, clock: null, said: "Running", target: null,
               initial: {}, read: {}, write: {}, marks: {}, tabs: [], tab: 0, saved: [], player: ""};

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
    el("run").disabled = el("cmp-run").disabled = state.running || state.starting || !(state.ready || state.stopped);
    el("cancel").disabled = !(state.running || state.starting);
  }
  function merged(base, over) {
    var out = {}, path;
    for (path in base) { out[path] = base[path]; }
    for (path in over) { if (Object.prototype.hasOwnProperty.call(over, path)) { out[path] = over[path]; } }
    return out;
  }
  function equal(a, b) { return !Object.keys(moved(a, b)).length && !Object.keys(moved(b, a)).length; }

  /* -- levers (Goal 10): label, value, range, default marker, note, changed or not -- */
  function lever(entry, index) {
    var path = entry.path, row = add("div", el("inputs"), null, "lever");
    var head = add("div", row, null, "lever-head"), label = add("label", head, entry.label || path);
    var start = D.values[path], shown, control, slide, foot;
    if (entry.unit) { add("span", label, " (" + entry.unit + ")", "unit"); }
    if (entry.kind === "switch") {
      control = add("input", head); control.type = "checkbox"; control.checked = start === true;
      control.addEventListener("change", changed);
      start = control.checked; shown = start ? "on" : "off";
      state.read[path] = function () { return control.checked; };
      state.write[path] = function (value) { control.checked = value === true; };
    } else if (entry.kind === "choice") {
      var values = entry.choices.map(function (choice) { return choice.value; });
      var at = function (value) { return Math.max(0, values.indexOf(value)); };
      control = add("select", head);
      entry.choices.forEach(function (choice, i) { add("option", control, choice.label || String(choice.value)).value = String(i); });
      control.value = String(at(start));
      control.addEventListener("change", changed);
      start = values[at(start)]; shown = entry.choices[at(start)].label || String(start);
      state.read[path] = function () { return values[Number(control.value)]; };
      state.write[path] = function (value) { control.value = String(at(value)); };
    } else {
      start = Number(start); shown = fmt(start);
      control = add("input", head); control.type = "number";
      slide = add("input", add("div", row, null, "lever-track")); slide.type = "range";
      [slide, control].forEach(function (node) {
        node.min = entry.min; node.max = entry.max; node.step = entry.step; node.value = start;
      });
      slide.setAttribute("aria-label", entry.label || path);
      add("span", slide.parentNode, null, "lever-default").style.left =
        Math.min(100, Math.max(0, 100 * (start - entry.min) / (entry.max - entry.min))) + "%";
      slide.addEventListener("input", function () { control.value = slide.value; changed(); });
      control.addEventListener("input", function () { slide.value = control.value; changed(); });
      state.read[path] = function () { return control.value === "" ? NaN : Number(control.value); };
      state.write[path] = function (value) { control.value = slide.value = value; };
    }
    control.id = "lever-" + index;
    label.htmlFor = control.id;
    /* One short item each side: the note (else the range) and whether it moved. The
       range is always on hover. */
    foot = add("div", row, null, "lever-foot");
    if (slide) { control.title = slide.title = fmt(entry.min) + " to " + fmt(entry.max); }
    add("span", foot, entry.note || (slide ? slide.title : ""), entry.note ? "lever-note" : "");
    state.marks[path] = [add("span", foot, "unchanged", "lever-state"), shown];
    state.initial[path] = start;
  }
  function inputs() {
    D.inputs.forEach(lever);
    if (!D.inputs.length) { add("p", el("inputs"), "This model exposes no inputs; you can run it as it is.", "note"); }
    if (D.frozen) { add("p", el("inputs"), "Data as read on " + D.frozen + ".", "note"); }
    var reps = el("reps");
    reps.min = 1; reps.max = D.limits.max_replications; reps.value = D.limits.replications;
    reps.addEventListener("input", estimate);
    el("reps-note").textContent = "1 to " + D.limits.max_replications + ", seed " + author.seed;
  }
  function now() {
    var out = {}, path;
    for (path in state.read) { out[path] = state.read[path](); }
    return out;
  }
  function estimate() {
    var count = Number(el("reps").value) || 0, most = D.limits.max_replications;
    var wait = estimateSeconds(D.estimate, count), longest = estimateSeconds(D.estimate, most);
    el("estimate").textContent = (wait >= 5 ? "About " + seconds(wait) : "A few seconds") +
      "; more, up to " + most + " replications" + (longest >= 5 ? " (" + seconds(longest) + ")" : "") +
      ", if the answer is not clear.";
  }
  /* A lever moved: say what differs from the defaults; a tab other than Yours is left
     for Yours, which takes the levers; a result shown for other values goes grey. */
  function changed() {
    var values = now(), count = 0, path, differs, tab = state.tabs[state.tab], yours = state.tabs[state.yours];
    for (path in state.marks) {
      differs = values[path] !== state.initial[path];
      count += differs ? 1 : 0;
      state.marks[path][0].textContent = differs ? "default " + state.marks[path][1] : "unchanged";
      state.marks[path][0].className = "lever-state" + (differs ? " moved" : "");
    }
    el("changed").textContent = count ? count + " changed" : "";
    estimate();
    if (tab !== yours && !equal(tab.values, values)) { state.tab = state.yours; tabs(); }
    if (state.tab === state.yours) { yours.values = values; }
    el("side").className = "side" + (state.shown && state.shown.values && !equal(state.shown.values, values) ? " stale" : "");
    el("stale").className = el("side").className === "side" ? "gone" : "note warn";
  }

  /* -- scenario tabs: the baseline, the author's scenarios on the levers, Yours, the
        ones saved in this browser (D709) -- */
  function tabs() {
    clear(el("tabs"));
    state.tabs.forEach(function (tab, i) {
      var button = add("button", el("tabs"), tab.name, "tab" + (tab.kind === "base" ? " base" : "") + (i === state.tab ? " on" : ""));
      button.type = "button"; button.value = String(i);
      button.setAttribute("aria-pressed", String(i === state.tab));
    });
    el("remove").hidden = !state.tabs[state.tab] || state.tabs[state.tab].kind !== "saved";
  }
  function pick(i) {
    var tab = state.tabs[i], path;
    if (!tab || i === state.tab) { return; }
    state.tab = i;
    for (path in state.write) { state.write[path](tab.values[path]); }
    tabs(); changed();
    if (tab.answer) { show(tab.answer); }
  }

  /* -- the answer, the results and the trust card (Goals 6-8) -- */
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
  }
  function made(record, mine, names) {
    var parts = [], path, label = {};
    D.inputs.forEach(function (entry) { label[entry.path] = entry.label || entry.path; });
    for (path in record.inputs) { parts.push((label[path] || path) + " = " + record.inputs[path]); }
    var seeds = record.seeds;
    el("made").textContent = (mine ? names[0] + ": " + (parts.join(", ") || "the inputs as the model has them")
      : "The author's run of the model as it is") + ". " + record.replications + " replications, seeds " +
      seeds[0] + " to " + seeds[seeds.length - 1] + ", common random numbers; the model's own run length." +
      (mine ? " * the 95% interval of the difference excludes 0." : "");
  }
  function show(answer) {
    var record = answer.record, mine = Object.keys(record.inputs).length > 0, key = mine ? "Yours" : "Baseline";
    var names = answer.names || ["Yours", "Baseline"], found = record.answer, trust = (record.trust || {})[key];
    state.shown = answer;
    el("results-title").textContent = answer.names ? "Results: " + names[0] + (mine ? " against " + names[1] : "")
      : "Results of the author's run";
    el("key-yours").textContent = mine ? names[0] : names[1];
    el("key-base").textContent = names[1];
    clear(el("results"));
    var seen = resultsPanel((record.results || {})[key] || [], el("results"), add);
    el("key-base").hidden = !mine || !seen.base;
    el("key-target").hidden = !seen.target;
    clear(el("trust"));
    if (trust) { trustPanel(trust, el("trust"), add, D.unit); }
    el("trust-box").hidden = !trust;
    el("answer-box").hidden = !found;
    if (found) {
      el("answer-box").className = "panel answer " + found.word;
      el("answer-kicker").textContent = !answer.names ? "Answer for the author's run" :
        "Answer for " + (names[0] === "Yours" ? "your version" : names[0]);
      el("answer-head").textContent = found.headline;
      el("answer-detail").textContent = found.detail;
      /* The input data's spread, quoted beside the verdict and never merged with it (D737). */
      var note = record.input_note && typeof record.input_note.text === "string" ? record.input_note.text : "";
      el("answer-note").textContent = note;
      el("answer-note").hidden = !note;
    }
    table(record, mine); made(record, mine, names);
    el("same").textContent = answer.same ? sameText(answer.same, record.versions, author.versions) :
      answer.names && names[1] !== "Baseline" ? "Run against " + names[1] + " in place of the author's baseline." : "";
    el("same").className = answer.same && (answer.same.different || answer.same.bounds_different) ? "note warn" : "note";
    if (answer.player) {
      frame(answer.player);
      el("watch-title").textContent = names[0] + " beside " + names[1];
      el("watch-note").textContent = "One replication of each, seed " + author.seed + ", recorded in this " +
        "browser.";
    } else if (answer.names) {
      el("watch-note").textContent = "This run could not be recorded for the drawing in this browser; the results stand.";
    }
    var kept = {};
    Object.keys(answer).forEach(function (name) { if (name !== "player") { kept[name] = answer[name]; } });
    el("record").textContent = JSON.stringify(kept);
    changed();
  }
  /* The player, in a frame of its own origin, showing only the line and the lanes, in
     the page's theme. */
  function frame(text) {
    var theme = document.documentElement.dataset.theme;
    state.player = text;
    el("player").srcdoc = text.replace('<html lang="en">', '<html lang="en" class="framed"' +
      (theme === "light" || theme === "dark" ? ' data-theme="' + theme + '"' : "") + ">");
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
    state.ready = false; state.starting = false; state.pending = null; state.running = false;
    clearInterval(state.clock); buttons();
    status("The engine could not start in this browser (" + text + "). Open this file in Chrome, Edge " +
      "or Firefox on a computer; Safari, phones and tablets are not supported.", true);
  }
  function heard(event) {
    var message = event.data, kind = message[0], go;
    if (kind === "stage") {
      status("Starting the engine in this browser: step " + message[1] + " of 4, " + STAGES[message[1]] +
        ". About 10 s in all.");
    } else if (kind === "ready") {
      state.ready = true; state.starting = false; buttons();
      status("Ready after " + seconds((performance.now() - state.since) / 1000) +
        ". The engine runs in this browser; nothing is sent anywhere.");
      if (state.pending) { go = state.pending; state.pending = null; go(); }
    } else if (kind === "progress") {
      el("progress").textContent = "replication " + message[1] + " of " + message[2];
    } else if (kind === "status") {
      state.said = message[1];
      status(message[1] + "...");
    } else if (kind === "done") {
      state.running = false; clearInterval(state.clock); buttons();
      var answer = JSON.parse(message[1]);
      el("progress").textContent = "";
      if (answer.error) { status("Not run: " + answer.error, true); return; }
      status("Run finished in " + seconds((performance.now() - state.since) / 1000) + ".");
      answer.names = state.target.names; answer.values = state.target.values;
      if (state.target.tab) { state.target.tab.answer = answer; }
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
                                       packages: D.packages,
                                       page: JSON.stringify({title: D.title, tokens: D.tokens, asked: D.asked || null})}]);
  }
  /* Run *args* in the engine; the answer goes to *target* ({names, values, tab}). */
  function send(args, target) {
    if (!state.ready) { state.pending = function () { send(args, target); }; start(); return; }
    args.replications = Number(el("reps").value);
    args.names = target.names;
    state.running = true; state.since = performance.now(); state.said = "Running"; state.target = target; buttons();
    el("progress").textContent = "";
    status("Running...");
    state.clock = setInterval(function () {
      status(state.said + ": " + seconds((performance.now() - state.since) / 1000) + " so far.");
    }, 1000);
    state.worker.postMessage(["run", JSON.stringify(args)]);
  }
  function run() {
    var tab = state.tabs[state.tab], values = now();
    send({values: moved(state.initial, values)},
         {names: [tab.name, "Baseline"], values: values, tab: tab.kind === "base" ? null : tab});
  }
  /* Two of the tabs, one against the other: the second runs in place of the baseline. */
  function compare() {
    var a = state.tabs[Number(el("cmp-a").value)], b = state.tabs[Number(el("cmp-b").value)];
    if (!a || !b || a === b) { status("Pick two different scenarios to compare.", true); return; }
    send({values: moved(b.values, a.values), against: moved(state.initial, b.values)},
         {names: [a.name, b.name], values: a.values, tab: null});
  }
  /* A busy worker hears nothing, so Cancel ends it: the run and the interpreter are
     lost, the inputs and the last complete result stay. After a run a new engine starts
     at once; an engine stopped while it was starting starts again when Run is pressed. */
  function cancel() {
    var ran = state.running;
    state.worker.terminate(); state.worker = null;
    state.running = false; state.starting = false; state.ready = false; state.pending = null;
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
  /* The text of a compressed block, read once. */
  function unpack(block, then) {
    if (!el(block).textContent) { return; }
    var raw = Uint8Array.from(atob(el(block).textContent), function (c) { return c.charCodeAt(0); });
    el(block).textContent = "";
    new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"))).text().then(then);
  }

  /* -- the footer (D709): kept in this browser, nothing leaves the page -- */
  function save() {
    var values = now(), tab = state.tabs[state.tab];
    state.saved.push(values);
    keep();
    state.made += 1;     /* never reused after a removal */
    state.tabs.push({name: "Saved " + state.made, kind: "saved", values: values, answer: tab.answer});
    state.tab = state.tabs.length - 1;
    tabs();
  }
  function keep() {
    try { window.localStorage.setItem(KEY, JSON.stringify(state.saved)); }
    catch (e) { status("Saved for this visit only: this browser keeps nothing for this page."); }
  }
  /* Removes the saved scenario shown; the others keep their names until the page opens
     again, then they are numbered afresh. */
  function remove() {
    var tab = state.tabs[state.tab], at = state.tabs.indexOf(tab);
    if (!tab || tab.kind !== "saved") { return; }
    state.saved.splice(state.tabs.filter(function (t) { return t.kind === "saved"; }).indexOf(tab), 1);
    keep();
    state.tabs.splice(at, 1);
    state.tab = -1;
    pick(Math.min(at, state.tabs.length - 1));
  }
  function comparePick() {
    var box = el("compare-pick");
    box.hidden = !box.hidden;
    ["cmp-a", "cmp-b"].forEach(function (id, side) {
      clear(el(id));
      state.tabs.forEach(function (tab, i) { add("option", el(id), tab.name).value = String(i); });
      el(id).value = String(side ? (state.tab ? 0 : state.yours) : state.tab);
    });
  }
  /* A picture of the answer for a report: drawn on a canvas, which the browser saves as
     a PNG (right-click, "Save image as"). The line is in a frame of its own origin and
     is not in it. */
  function picture() {
    var canvas = el("picture-canvas"), pen = canvas.getContext("2d"), width = 960, pad = 28, lines = [], y = pad;
    var look = window.getComputedStyle(document.documentElement), record = state.shown.record;
    var key = Object.keys(record.inputs).length ? "Yours" : "Baseline";
    function tok(name) { return look.getPropertyValue("--" + name).trim(); }
    function put(text, size, colour, bold) {
      pen.font = (bold ? "bold " : "") + size + "px " + tok("font");
      wrap(pen, text, width - 2 * pad).forEach(function (line) { lines.push([line, size, colour, bold]); });
    }
    function all() {
      lines = [];
      put(el("title").textContent, 22, "ink", true);
      put(el("results-title").textContent, 13, "muted", false);
      if (record.answer) { put(record.answer.headline, 18, "ink", true); put(record.answer.detail, 14, "muted", false); }
      ((record.results || {})[key] || []).forEach(function (row) {
        put(row.label + ": " + fmt(row.yours.mean) + (row.unit ? " " + row.unit : "") +
            (row.yours.lo !== null ? " (95% interval " + span(row.yours.lo, row.yours.hi) + ")" : "") +
            (row.base ? ", baseline " + fmt(row.base.mean) : "") + (row.verdict ? ", " + row.verdict : ""), 14, "ink", false);
      });
      put(el("made").textContent, 12, "muted", false);
      put("Naga viewer, " + new Date().toISOString().slice(0, 10) + ".", 12, "muted", false);
    }
    all();
    canvas.width = width;
    canvas.height = pad * 2 + lines.reduce(function (sum, line) { return sum + line[1] * 1.5; }, 0);
    pen.fillStyle = tok("panel"); pen.fillRect(0, 0, canvas.width, canvas.height);
    pen.textBaseline = "top";
    lines.forEach(function (line) {
      pen.font = (line[3] ? "bold " : "") + line[1] + "px " + tok("font");
      pen.fillStyle = tok(line[2]);
      pen.fillText(line[0], pad, y);
      y += line[1] * 1.5;
    });
    el("picture-box").hidden = false;
  }

  /* The theme switch (D703); the frame is told, so its replay goes on. Its origin is
     opaque (sandboxed), so the message goes to "*": it carries a theme word only. */
  themeSwitch(el("theme"), function () {
    var win = el("player").contentWindow;
    if (win) { win.postMessage(["theme", document.documentElement.dataset.theme || "auto"], "*"); }
  });

  document.title = D.title;
  el("title").textContent = D.question || D.title;
  el("subtitle").textContent = D.question ? D.title : "The model as its author left it: move the levers and run your version.";
  el("versions").textContent = "Naga " + author.versions.nagasim + ", Python " + D.python + " (Pyodide " +
    D.pyodide + "), all in this file. Anyone with the file can read the model.";
  inputs();
  try { state.saved = savedFrom(window.localStorage.getItem(KEY) || "[]", paths); } catch (e) { state.saved = []; }
  state.made = state.saved.length;
  state.tabs = [{name: "Baseline", kind: "base", values: state.initial, answer: {record: author, values: state.initial}}]
    .concat(D.scenarios.map(function (scenario) {
      return {name: scenario.name, kind: "author", values: merged(state.initial, scenario.values)};
    }));
  state.yours = state.tabs.length;
  state.tabs.push({name: "Yours", kind: "yours", values: merged(state.initial, {})});
  state.saved.forEach(function (values, i) {
    state.tabs.push({name: "Saved " + (i + 1), kind: "saved", values: merged(state.initial, values)});
  });
  tabs();
  show(state.tabs[0].answer);
  el("tabs").addEventListener("click", function (event) {
    var target = event && event.target;
    if (target && target.parentNode === el("tabs")) { pick(Number(target.value)); }
  });
  el("run").addEventListener("click", run);
  el("cancel").addEventListener("click", cancel);
  el("reset").addEventListener("click", function () {
    var path;
    for (path in state.write) { state.write[path](state.initial[path]); }
    changed();
  });
  el("save").addEventListener("click", save);
  el("remove").addEventListener("click", remove);
  el("compare").addEventListener("click", comparePick);
  el("cmp-run").addEventListener("click", compare);
  el("picture").addEventListener("click", picture);
  el("notices").addEventListener("toggle", function () {
    if (el("notices").open) { unpack("notices-data", function (text) { el("notices-text").textContent = text; }); }
  });
  if (el("player-data").textContent) {
    el("watch-note").textContent = "One replication, seed " + author.seed + ", recorded when this file was made.";
    unpack("player-data", frame);
  } else {
    el("watch").hidden = true;
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
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'sha256-__THEME_HASH__' 'sha256-__MAIN_HASH__' 'sha256-__PLAYER_HASH__' blob: 'wasm-unsafe-eval'; style-src 'sha256-__CSS_HASH__' 'sha256-__PLAYER_CSS_HASH__'; img-src data:; worker-src data:; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Naga viewer</title>
<style>__CSS__</style>
<script id="theme-pick">__THEME__</script>
</head>
<body>
<header class="topbar">
<div class="ident"><h1 id="title"></h1><p class="note" id="subtitle"></p></div>
<div id="tabs" class="tabs" aria-label="Scenario"></div>
<button id="theme" type="button" aria-label="Light or dark">Theme: auto</button>
</header>
<p id="status">This page needs JavaScript.</p>
<main class="console levers">
<section class="panel" aria-labelledby="levers-h">
<div class="panel-head"><h2 id="levers-h">Levers</h2><span id="changed"></span></div>
<div id="inputs"></div>
<div class="reps"><label for="reps">Replications</label><input id="reps" type="number" step="1">
<span class="note" id="reps-note"></span></div>
<div class="actions">
<button id="run" type="button" class="primary" disabled>Run my version</button>
<button id="reset" type="button">Reset to defaults</button>
<button id="cancel" type="button" disabled>Cancel</button>
</div>
<p class="note"><span id="progress"></span> <span id="estimate"></span></p>
</section>
<section class="centre">
<div class="panel" id="watch">
<div class="panel-head"><h2 id="watch-title">The author's recorded run</h2></div>
<iframe id="player" sandbox="allow-scripts" title="The recorded run"></iframe>
<p class="note" id="watch-note"></p>
</div>
</section>
<aside class="side" id="side">
<div class="panel answer" id="answer-box" hidden>
<p class="kicker" id="answer-kicker"></p>
<p id="answer-head"></p>
<p class="note" id="answer-detail"></p>
<p class="note" id="answer-note" hidden></p>
</div>
<div class="panel" id="results-box">
<div class="panel-head"><h2 id="results-title">Results of the author's run</h2><span class="legend"><span class="key yours" id="key-yours">yours</span><span class="key base" id="key-base">baseline</span><span class="key target" id="key-target">target</span></span></div>
<p class="gone" id="stale">Levers changed: run again to update.</p>
<div id="results"></div>
<details><summary class="note">All numbers</summary>
<div class="scroll"><table id="results-table"><thead id="results-head"></thead><tbody id="results-body"></tbody></table></div>
<p class="note" id="made"></p>
<p class="note" id="same"></p>
</details>
</div>
<div class="panel" id="trust-box"><div class="panel-head"><h2>Can I trust this?</h2></div><table id="trust" class="trust"></table></div>
</aside>
</main>
<footer class="footbar">
<button id="save" type="button">Save as scenario</button>
<button id="remove" type="button" hidden>Remove this saved scenario</button>
<button id="picture" type="button">Picture for the report</button>
<button id="compare" type="button">Compare two scenarios</button>
<span id="compare-pick" hidden><select id="cmp-a" aria-label="Scenario"></select> against
<select id="cmp-b" aria-label="Against"></select>
<button id="cmp-run" type="button" class="primary" disabled>Run the comparison</button></span>
<span class="note" id="versions"></span>
</footer>
<div class="panel" id="picture-box" hidden>
<p class="note">Right-click the picture and choose "Save image as" to keep it as a PNG.</p>
<canvas id="picture-canvas" width="960" height="200"></canvas>
</div>
<details id="notices"><summary class="note">Licences of the software in this file</summary>
<pre id="notices-text">Reading...</pre></details>
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


def _quoted(asked) -> list:
    """The recorded answers a page carries: the input-uncertainty and ABC calibration ones
    the trust card and the input note quote (`uncertainty.trust_texts`, `what_if_note`),
    the latest few, as plain JSON. Only the fields those read, each kind by an allow-list:
    a page is shared, and a record's own arguments (data files, inputs) are not for it."""
    return _quoting(asked)[0][-_QUOTED:]


def _quoting(asked) -> tuple:
    """(every row `_quoted` keeps, in order; the index in *asked* of the first entry that is
    no object, or of a kind it quotes but not well formed, or None). One pass serves both
    the page, which drops such an entry, and `read_asked`, which names it."""
    rows, bad = [], None
    for index, a in enumerate(asked or []):
        row = None
        if not isinstance(a, dict):
            pass
        elif a.get("tool") == "input_uncertainty":
            if isinstance(a.get("kpis"), list):
                row = {k: a.get(k) for k in ("tool", "measure", "resamples_used", "replications")}
                row["kpis"] = [{k: r.get(k) for k in ("kpi", "mean", "input_interval", "input_share")}
                               for r in a["kpis"] if isinstance(r, dict)]
                if isinstance(a.get("overrides"), list):     # what `same_change` compares
                    row["overrides"] = [{k: o.get(k) for k in ("path", "value")}
                                        for o in a["overrides"] if isinstance(o, dict)]
        elif a.get("tool") == "calibrate" and a.get("method") == "abc":
            row = {k: a.get(k) for k in ("tool", "method", "samples", "accepted", "abc_tolerance")}
        else:
            continue                    # another tool's row: not for a page, and no fault
        row = None if row is None else player.plain(row)
        if row is not None and _well_formed(row):
            rows.append(row)
        elif bad is None:
            bad = index
    return rows, bad


def _well_formed(row: dict) -> bool:
    """The fields the quotes format hold what they format (`uncertainty.trust_texts`): a row
    read from a file (the editor's store, an `--asked` file) is as easily written by hand as
    by a tool, and a string where a number goes would break the whole trust card."""
    def number(value):
        # is_finite_number also refuses an int too large for a float (#1132), where
        # math.isfinite raised OverflowError.
        return value is None or is_finite_number(value)

    if row["tool"] == "calibrate":
        return all(number(row[k]) for k in ("samples", "accepted", "abc_tolerance"))
    if not (row["measure"] in (None, "level", "difference")
            and number(row["resamples_used"]) and number(row["replications"])):
        return False
    for r in row["kpis"]:
        pair = r["input_interval"]
        if not (isinstance(r["kpi"], str) and number(r["mean"]) and number(r["input_share"])
                and (pair is None or (isinstance(pair, list) and len(pair) == 2
                                      and all(number(v) for v in pair)))):
            return False
    return all(isinstance(o["path"], str) for o in row.get("overrides", []))


def read_asked(path) -> list:
    """The recorded answers in the CALLER's file *path* (`--asked FILE`), as a page carries
    them (`_quoted`). The file is the JSON list a study version's asked.json holds, or a
    store file the editor keeps; other tools' rows in it are skipped. Raises
    `netguard.GuardError`: E_FILE_MISSING or E_FILE_REFUSED (`netguard.read_caller_file`),
    E_ASKED_MALFORMED for a file that is no such list, or holds an input-uncertainty or ABC
    row the trust card cannot quote (named by its index)."""
    from nagasim import netguard

    data = netguard.read_caller_file(path, max_bytes=ASKED_BYTES, what="the --asked file")
    try:
        rows = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise netguard.GuardError("E_ASKED_MALFORMED",
                                  f"the --asked file is not JSON ({type(exc).__name__})") from None
    if not isinstance(rows, list):
        raise netguard.GuardError("E_ASKED_MALFORMED", "the --asked file is not a JSON list",
                                  "it holds the rows of a study version's asked.json")
    kept, bad = _quoting(rows)
    if bad is not None and not isinstance(rows[bad], dict):
        raise netguard.GuardError("E_ASKED_MALFORMED",
                                  f"row {bad} of the --asked file is not an object")
    if bad is not None:
        raise netguard.GuardError("E_ASKED_MALFORMED",
                                  f"row {bad} of the --asked file is not a well-formed "
                                  f"{rows[bad]['tool']} answer",
                                  "the trust card quotes its numbers, KPI names and intervals")
    return kept[-_QUOTED:]


def _recording(model: dict, baseline: dict, title: str, should_stop=None, tokens=None,
               asked=None) -> str:
    """The player page (`player.build_player`) of the author's baseline: its first
    replication run again with its seed and a recorder. "" when it cannot be made (a
    tape over the player's size limit): the viewer is then written without it."""
    from nagasim.api import run_model
    from nagasim.core.trace import TraceRecorder

    recorder = TraceRecorder()
    try:
        results = run_model(model, replications=baseline["replications"],
                            seed=baseline["seed"], trace=recorder, should_stop=should_stop)
        return player.build_player(model, {experiments.BASELINE: recorder}, results=results, title=title,
                                   tokens=tokens, asked=asked)
    except ValueError:
        return ""


def _lever_scenarios(model: dict, definition: dict) -> list[dict]:
    """The author's scenarios a viewer can show as tabs: [{"name", "values"}] for each
    one whose overrides all set the dashboard's inputs to values they allow."""
    paths, out = {entry["path"] for entry in definition["inputs"]}, []
    for scenario in model.get("scenarios") or []:
        values = {o.get("path"): o.get("value") for o in scenario.get("overrides") or [] if isinstance(o, dict)}
        if not values or not set(values) <= paths:
            continue
        try:
            dashboard.input_overrides(model, values)
        except ValueError:
            continue
        out.append({"name": str(scenario.get("name"))[:40], "values": values})
    return out


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
                 recording: str | None = None, tokens: dict | None = None,
                 on_progress=None, should_stop=None, asked=None) -> dict:
    """Write the viewer page of *layout* (a model dict or file) to *path* and return
    {"bytes", "estimate", "warnings", "baseline"}.

    *asked* is the recorded tool answers of the model (`agent_tools.core.recorded`): the
    page carries the input-uncertainty and ABC ones (`_quoted`), for its trust card and
    its answer card's input note; without them both say "not measured" / nothing.

    *runtime* is the folder of the browser runtime (`viewer_runtime.resolve_dir`); this
    function never downloads. The author's baseline is run here, with the definition's
    own replications, unless *baseline* (a `what_if` record of this model) is given.
    *frozen* is the date the model's data was read on, when it was frozen into the copy.
    *recording* is the player page of the author's run (default: the first baseline
    replication is run again here with a recorder, D200, and `player.build_player`
    makes it; "" for none): the page shows it in a frame, as the author's run, until a
    run in the page replaces it with the viewer's run beside the baseline (`watch_page`).
    *tokens* is the look of the page and of both players (`player.DEFAULT_TOKENS`).
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

    from nagasim import viewer_runtime
    from nagasim.api import load_layout

    if not engine_on_disk():
        raise ValueError(_NOT_ON_DISK)
    tokens = player.check_tokens(tokens)                            # a bad token: refused first

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
        baseline = what_if(model, should_stop=should_stop, asked=_quoted(asked))
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
        "python": str(lock["info"]["python"]), "frozen": frozen, "tokens": tokens,
        "question": (model.get("dashboard") or {}).get("question"),
        "scenarios": _lever_scenarios(model, definition), "unit": units_of(model.get("simulation")).time,
        "asked": _quoted(asked),
    }
    worker = _WORKER.replace("__BOOT__", json.dumps(_BOOT))
    if not (worker.isascii() and _MAIN.isascii()) or "</script" in worker + _MAIN:
        raise AssertionError("the viewer's scripts must be ASCII and hold no closing tag")
    notices = base64.b64encode(gzip.compress(_notices(licences).encode("utf-8"), 9, mtime=0))
    if recording is None:
        recording = _recording(model, baseline, data["title"], should_stop, tokens, data["asked"])
    step(4)
    packed = b""
    if recording:
        packed = base64.b64encode(gzip.compress(recording.encode("ascii"), 9, mtime=0))
    if not recording:
        warnings.append(problem("W_VIEWER_EMPTY", "the author's run could not be recorded for "
                                "this model: the viewer has no run to watch", ""))
    # The baseline's time is rounded: it is the one thing in the page that is measured.
    shipped = {**baseline, "seconds": round(baseline["seconds"], 2)}
    # The frame's player inherits this policy, so its sheet and its theme script are
    # pinned here too (D697, D703).
    css = player.style_sheet(tokens, _CSS)
    sha = player.csp_hash
    parts = {"THEME": player.THEME, "THEME_HASH": sha(player.THEME), "MAIN_HASH": sha(_MAIN),
             "CSS_HASH": sha(css), "CSS": css, "DATA": player.embed(data),
             "MODEL": player.embed(model), "BASELINE": player.embed(shipped), "WORKER": worker, "MAIN": _MAIN,
             "NOTICES": notices.decode("ascii"), "PLAYER": packed.decode("ascii"),
             "PLAYER_HASH": sha(player.SCRIPT), "PLAYER_CSS_HASH": sha(player.style_sheet(tokens)),
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
    add_model_argument(parser, help="the model file, or a bundled model's name (naga examples)", nargs="?")
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
    parser.add_argument("--asked", metavar="FILE", help=ASKED_HELP)
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
    try:
        asked = read_asked(args.asked) if args.asked else None
    except netguard.GuardError as exc:
        sys.stderr.write(f"--asked: {exc.code}: {exc.message}\n")
        return 2
    layout, frozen = args.model, None
    if args.freeze:
        layout, read = freeze_data(args.model, Path(args.model).parent,
                                   network=netguard.policy_from_args(args))
        frozen = datetime.date.today().isoformat()
        out(f"data read once from {len(read)} source(s) ({', '.join(read) or 'none'}) and "
            "written into the copy, which names no source\n")
    made = build_viewer(layout, args.output, runtime=args.runtime,
                        title=args.title or Path(args.model).stem, frozen=frozen, asked=asked)
    for warning in made["warnings"]:
        sys.stderr.write(f"warning {warning.code}: {warning.message}\n")
    out(f"wrote {args.output}: {made['bytes'] / 1e6:.1f} MB; one replication takes about "
        f"{made['estimate']:.2f} s in a browser\n{_SAYS}\n")
    return 0
