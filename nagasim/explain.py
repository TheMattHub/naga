"""Explain a KPI: the measured contributors to it, ranked, and one suggested test.

    answer = explain.explain_kpi(layout, "system/Avg Throughput Time")
    answer = explain.explain_kpi(layout, kpi, overrides=[{"path": ..., "value": ...}])

Every row is computed from KPIs a run already reports (`resource/...`, `buffer/...`,
`bottleneck/...`), so its numbers match the results tables (D713). A row is "measured" and
the test is "suggested": a ranking says how the KPI is made up, not why, and the suggested
test (capacity +1 of the top resource, as `what_if` arguments) is how to check the top row;
it is never run here (D716).

The rows, by KPI:

* a time KPI (`system/Avg Throughput Time`): per resource, the wait of a completed entity
  there, `Time in Queue Avg (incl. waiting)` x `Total Seizes` / `Total Entities Completed`
  (D714); a WIP KPI (`system/Avg WIP`, `system/Avg WIP (Little)`) the same times the
  throughput (Little). The KPI minus the waits is one row, "processing and moving", when it
  is not negative; the waits need not add up to the KPI (entities still in the system at the
  end), and `coverage` says how far apart they are.
* `system/Throughput (per time unit)`: per resource its bottleneck share
  (`bottleneck/<resource>/Total %`, collected only with `outputs.bottleneck`), else its usage,
  with usage, down and blocked beside it.
* `resource/<name>/<metric>`: that resource's own usage, down, blocked and wait, in that order.

With overrides the same rows are compared on common random numbers, ranked by the size of the
paired difference with "not distinguishable" rows last (D715); the replications follow
`experiments.what_if`'s doubling on the KPI asked about.

`rows_of` and `compare` are pure over per-replication tables (`{kpi: [value per replication]}`,
`table_of`); `explain_kpi` runs the model through `api.run_model` or
`experiments.paired_until_clear`.
"""
from __future__ import annotations

import argparse
import json
import math

from nagasim.core.stats import ci95, paired_diff_ci

__all__ = ["MEASURED", "SUGGESTED", "REST", "table_of", "blocks_of", "rows_of", "compare",
           "suggested_test", "factors_from", "explain_kpi", "render", "main"]

MEASURED = "measured"
SUGGESTED = "suggested test"
REST = "processing and moving"

TIME_KPI = "system/Avg Throughput Time"
WIP_KPIS = ("system/Avg WIP", "system/Avg WIP (Little)")
THROUGHPUT = "system/Throughput (per time unit)"
COMPLETED = "system/Total Entities Completed"
WAIT, SEIZES = "Time in Queue Avg (incl. waiting)", "Total Seizes"
USAGE, DOWN, BLOCKED = "Usage % (snapshot)", "Down %", "Blocked %"
SHARE = "Total %"       # bottleneck/<resource>/Total %: the Roser share, sole and shifting


# ---------------------------------------------------------------- tables
def table_of(results) -> dict[str, list[float]]:
    """`{kpi: [value per replication, in Run order]}` from a results dict. A KPI a
    replication did not report is 0 there (a resource that replication never used)."""
    from nagasim.experiments import flatten_kpis

    flat = flatten_kpis(results)
    index = {run: i for i, run in enumerate(sorted(set(flat["Run"])))}
    table: dict[str, list[float]] = {}
    for run, kpi, value in zip(flat["Run"], flat["KPI"], flat["Value"]):
        table.setdefault(kpi, [0.0] * len(index))[index[run]] = float(value)
    return table


def blocks_of(layout) -> dict[str, list[str]]:
    """`{resource: [blocks that hold it]}` in the model as a run sees it (submodel
    instances expanded), to read each resource's `buffer/<block>/Blocked %` beside it."""
    from nagasim.core import submodels
    from nagasim.core.block_registry import resource_block_types
    from nagasim.core.schema import load_layout

    model = load_layout(layout)
    flat = submodels.flatten(model) if submodels.has_submodels(model) else model
    types = set(resource_block_types())
    out: dict[str, list[str]] = {}
    for block in flat.get("blocks") or []:
        name = (block.get("params") or {}).get("resource_name")
        if str(block.get("type", "")).upper() in types and isinstance(name, str) and name:
            out.setdefault(name, []).append(block["name"])
    return out


def _num(value):
    return float(value) if value is not None and math.isfinite(value) else None


def _stat(values) -> dict:
    stat = ci95([v for v in values if math.isfinite(v)])
    return {"mean": _num(stat["mean"]), "lb": _num(stat["lb"]), "ub": _num(stat["ub"])}


def _family(kpi: str) -> str:
    if kpi == TIME_KPI:
        return "time"
    if kpi in WIP_KPIS:
        return "wip"
    if kpi == THROUGHPUT:
        return "throughput"
    if kpi.startswith("resource/") and kpi.count("/") >= 2:
        return "resource"
    raise ValueError(f"explain_kpi explains {TIME_KPI}, {', '.join(WIP_KPIS)}, {THROUGHPUT} "
                     f"or one resource's resource/<name>/<metric>, not {kpi!r}")


def _resources(table) -> list[str]:
    """Resources seized at least once in some replication, in table order."""
    suffix = "/" + SEIZES
    return [key[len("resource/"):-len(suffix)] for key, values in table.items()
            if key.startswith("resource/") and key.endswith(suffix) and any(v > 0 for v in values)]


def _get(table, key, n) -> list[float]:
    return table.get(key) or [0.0] * n


def _blocked(table, blocks, resource, n):
    """The resource's largest `Blocked %` over its blocks, per replication, or None."""
    keys = [f"buffer/{b}/{BLOCKED}" for b in (blocks or {}).get(resource, ())]
    keys = [k for k in keys if k in table]
    if not keys:
        return None
    return [max(table[k][i] for k in keys) for i in range(n)]


def _series(table: dict, kpi: str, blocks: dict | None) -> dict:
    """The KPI and its rows as per-replication values: {"target", "rows": [{"row",
    "resource", "values", "beside"}], "notes", "ranked"}."""
    family = _family(kpi)
    if kpi not in table:
        raise ValueError(f"unknown KPI: {kpi!r}")
    target = table[kpi]
    n = len(target)
    rows, notes = [], []
    if family in ("time", "wip"):
        done = _get(table, COMPLETED, n)
        factor = _get(table, THROUGHPUT, n) if family == "wip" else [1.0] * n
        for resource in _resources(table):
            wait = _get(table, f"resource/{resource}/{WAIT}", n)
            seizes = _get(table, f"resource/{resource}/{SEIZES}", n)
            values = [w * s / c * f if c > 0 else math.nan
                      for w, s, c, f in zip(wait, seizes, done, factor)]
            rows.append({"row": resource, "resource": resource, "values": values})
        if not any(c > 0 for c in done):
            notes.append("no entity completed in any replication: the rows are empty")
            rows = []
        else:
            rest = [k - sum(r["values"][i] for r in rows) for i, k in enumerate(target)]
            if _stat(rest)["mean"] is not None and _stat(rest)["mean"] >= 0:
                rows.append({"row": REST, "resource": None, "values": rest})
        return {"family": family, "target": target, "rows": rows, "notes": notes, "ranked": True}
    if family == "throughput":
        roser = any(key.startswith("bottleneck/") for key in table)
        if not roser:
            notes.append("bottleneck shares were not collected (the model does not set "
                         f"outputs.bottleneck): rows ranked by {USAGE}")
        for resource in _resources(table):
            key = f"bottleneck/{resource}/{SHARE}" if roser else f"resource/{resource}/{USAGE}"
            beside = {USAGE: _get(table, f"resource/{resource}/{USAGE}", n)} if roser else {}
            beside[DOWN] = _get(table, f"resource/{resource}/{DOWN}", n)
            blocked = _blocked(table, blocks, resource, n)
            if blocked is not None:
                beside[BLOCKED] = blocked
            rows.append({"row": resource, "resource": resource, "values": _get(table, key, n),
                         "beside": beside})
        return {"family": family, "target": target, "rows": rows, "notes": notes, "ranked": True,
                "metric": "bottleneck share (Total %)" if roser else USAGE}
    resource = kpi[len("resource/"):kpi.rindex("/")]
    for metric in (USAGE, DOWN):
        key = f"resource/{resource}/{metric}"
        rows.append({"row": key, "resource": resource, "values": _get(table, key, n)})
    for block in (blocks or {}).get(resource, ()):
        key = f"buffer/{block}/{BLOCKED}"
        if key in table:
            rows.append({"row": key, "resource": resource, "values": table[key]})
    key = f"resource/{resource}/{WAIT}"
    rows.append({"row": key, "resource": resource, "values": _get(table, key, n)})
    return {"family": family, "target": target, "rows": rows, "notes": notes, "ranked": False}


# ---------------------------------------------------------------- level and change
def rows_of(table: dict, kpi: str, blocks: dict | None = None) -> dict:
    """The rows of *kpi* in one batch of replications (*table*: `table_of`), ranked.

    Returns {"kpi", "value": {mean, lb, ub}, "rows": [{"row", "label": "measured",
    "resource" (None for the rest row), "mean", "lb", "ub", "share" (time and WIP KPIs),
    "beside" (throughput)}], "coverage" (time and WIP KPIs: the waits over the KPI),
    "summary", "notes"}. Raises ValueError for a KPI it does not explain.
    """
    s = _series(table, kpi, blocks)
    value = _stat(s["target"])
    rows = []
    for row in s["rows"]:
        out = {"row": row["row"], "label": MEASURED, "resource": row["resource"],
               **_stat(row["values"])}
        if s["family"] in ("time", "wip"):
            out["share"] = (_num(out["mean"] / value["mean"])
                            if out["mean"] is not None and value["mean"] else None)
        if "beside" in row:
            out["beside"] = {k: _stat(v)["mean"] for k, v in row["beside"].items()}
        rows.append(out)
    if s["ranked"]:
        rows.sort(key=lambda r: -(r["mean"] if r["mean"] is not None else -math.inf))
    coverage = None
    if s["family"] in ("time", "wip") and rows and value["mean"]:
        coverage = _num(sum(r["mean"] or 0.0 for r in rows if r["resource"]) / value["mean"])
    notes = list(s["notes"])
    if coverage is not None:
        notes.append(f"waiting rows cover {coverage:.0%} of the KPI"
                     + ("" if coverage <= 1 else
                        ": they count waits of entities still in the system at the end, "
                        f"which the KPI does not, so there is no '{REST}' row"))
    if s["ranked"] and len(rows) > 1 and None not in (rows[0]["lb"], rows[1]["ub"])             and rows[1]["ub"] >= rows[0]["lb"]:
        notes.append("the top two rows are not separated at this number of replications (#859)")
    if not rows:
        summary = "no rows"
    elif not s["ranked"]:
        summary = f"the measured rows of {kpi}"
    else:
        summary = f"{rows[0]['row']} holds the largest measured {s.get('metric', 'share')}"
    return {"kpi": kpi, "value": value, "rows": rows, "coverage": coverage,
            "summary": summary, "notes": notes}


def _verdict(diff: dict) -> str:
    lb, ub = diff["lb"], diff["ub"]
    if lb == 0 and ub == 0:
        return "unchanged"
    if lb is None or ub is None or lb <= 0 <= ub:
        return "not distinguishable"
    return "higher" if diff["mean"] > 0 else "lower"


def _paired(base, changed) -> dict:
    d = paired_diff_ci(changed, base)
    out = {"baseline": _stat(base)["mean"], "scenario": _stat(changed)["mean"],
           "diff": _num(d["mean"]), "diff_lb": _num(d["lb"]), "diff_ub": _num(d["ub"])}
    out["verdict"] = _verdict({"mean": out["diff"], "lb": out["diff_lb"], "ub": out["diff_ub"]})
    return out


def compare(base: dict, changed: dict, kpi: str, blocks: dict | None = None) -> dict:
    """The rows of *kpi* for a baseline and a changed model run on the same seeds
    (two `table_of` tables), ranked by |paired difference|, "not distinguishable" and
    "unchanged" rows last (D715).

    Returns {"kpi", "value": {baseline, scenario, diff, diff_lb, diff_ub, verdict},
    "rows": [{"row", "label": "measured", "resource", the same six keys}], "summary",
    "notes"}.
    """
    a, b = _series(base, kpi, blocks), _series(changed, kpi, blocks)
    n = len(a["target"])
    by_name = {r["row"]: r for r in b["rows"]}
    names = [r["row"] for r in a["rows"]] + [r["row"] for r in b["rows"]
                                             if r["row"] not in {x["row"] for x in a["rows"]}]
    first = {r["row"]: r for r in a["rows"]}
    rows = []
    for name in names:
        one, two = first.get(name), by_name.get(name)
        if name == REST and (one is None or two is None):
            continue        # a negative remainder on one side is no row to compare
        resource = (one or two)["resource"]
        rows.append({"row": name, "label": MEASURED, "resource": resource,
                     **_paired(one["values"] if one else [0.0] * n,
                               two["values"] if two else [0.0] * n)})
    moved = [r for r in rows if r["verdict"] in ("higher", "lower")]
    still = [r for r in rows if r["verdict"] not in ("higher", "lower")]
    key = lambda r: -abs(r["diff"] or 0.0)  # noqa: E731
    rows = sorted(moved, key=key) + sorted(still, key=key)
    summary = (f"{moved[0]['row']} has the largest measured difference"
               if moved else "no row moved distinguishably at this number of replications")
    return {"kpi": kpi, "value": _paired(a["target"], b["target"]), "rows": rows,
            "summary": summary, "notes": list(dict.fromkeys(a["notes"] + b["notes"]))}


# ---------------------------------------------------------------- suggested test
def suggested_test(layout, resource: str, kpi: str, overrides: list | None = None):
    """(test, note): `what_if` arguments that give *resource* one more unit of capacity
    on the model *overrides* describe, labelled "suggested test" and never run (D716); or
    (None, why not)."""
    from nagasim import experiments, recommend
    from nagasim.core.schema import load_layout

    overrides = list(overrides or [])
    model = experiments.apply_overrides(load_layout(layout), overrides)
    try:
        path = recommend.override_of({"verb": "set_capacity", "target": resource, "value": 1},
                                     model)["path"]
    except recommend.Refused as exc:
        return None, f"no suggested test: {exc.message}"
    current = experiments.value_at(model, path)
    if not isinstance(current, int) or isinstance(current, bool):
        return None, f"no suggested test: the capacity of {resource} is not a whole number"
    changed = [o for o in overrides if o.get("path") != path]
    changed.append({"path": path, "value": current + 1})
    return {"label": SUGGESTED, "tool": "what_if",
            "arguments": {"overrides": changed, "kpis": [kpi]},
            "text": f"capacity of {resource} from {current} to {current + 1}"}, None


# ---------------------------------------------------------------- factors for a search (D730)
FACTOR_SHARE = 0.95     # the top resource rows that hold this much of the rows' total
MAX_FACTORS = 4


def _capacity_factor(model, resource: str):
    """(factor, None) for *resource*'s capacity, an integer from max(1, current - 1) to
    current + 2, or (None, why not)."""
    from nagasim import experiments, recommend
    try:
        path = recommend.override_of({"verb": "set_capacity", "target": resource, "value": 1},
                                     model)["path"]
    except recommend.Refused as exc:
        return None, f"{resource} left out: {exc.message}"
    current = experiments.value_at(model, path)
    if not isinstance(current, int) or isinstance(current, bool):
        return None, f"{resource} left out: its capacity is not a whole number"
    return {"path": path, "kind": "integer", "low": max(1, current - 1),
            "high": current + 2}, None


def _sensitivity_factor(model, row: dict):
    """A factor for one sensitivity row: its own low/high ends (the +-delta it was varied
    over, capacities +-1); integer for a capacity or an integer value."""
    from nagasim import experiments
    path = row["path"]
    current = experiments.value_at(model, path)     # a path that addresses nothing raises here
    current = row.get("value", current)
    low, high = row.get("low"), row.get("high")
    if low is None or high is None:     # a ranked row without its ends: +-20%, as varied
        low, high = ((max(1, current - 1), current + 1) if path.endswith(".capacity")
                     else (current * 0.8, current * 1.2))
    whole = path.endswith(".capacity") or (isinstance(current, int)
                                           and not isinstance(current, bool))
    low, high = sorted((low, high))
    return {"path": path, "kind": "integer" if whole else "continuous",
            "low": int(low) if whole else float(low), "high": int(high) if whole else float(high)}


def factors_from(layout, answer: dict, sensitivity: dict | None = None, *,
                 share: float = FACTOR_SHARE, limit: int = MAX_FACTORS) -> dict:
    """The factors an `optimize` call can search, from an explain answer (and optionally a
    study_sensitivity answer or its sensitivity.json): the capacities of the top resource rows
    that together hold *share* of the resource rows' total, then the sensitivity paths that
    change the answer, at most *limit*, each path once.

    The rows are measured shares, not causes (D713): the list is a hypothesis the search
    tests. Returns {"factors": [{"path", "kind", "low", "high"}], "from": [why each one],
    "notes"}; the factors go to `optimize(factors=...)` unchanged."""
    from nagasim.core.schema import load_layout

    model = load_layout(layout)
    rows = [r for r in answer.get("rows") or [] if r.get("resource")]
    if answer.get("mode") == "change":
        weight = lambda r: abs(r.get("diff") or 0.0)  # noqa: E731
    else:
        weight = lambda r: max(r.get("mean") or 0.0, 0.0)  # noqa: E731
    total = sum(weight(r) for r in rows)
    out, why, notes, seen, held = [], [], [], set(), 0.0
    for r in rows:
        if r["resource"] in seen or len(out) >= limit or (total and held >= share * total):
            continue
        seen.add(r["resource"])
        held += weight(r)
        factor, note = _capacity_factor(model, r["resource"])
        if note:
            notes.append(note)
            continue
        out.append(factor)
        why.append(f"capacity of {r['resource']}: row '{r['row']}' "
                   f"({100 * weight(r) / total:.0f}% of the rows)" if total else
                   f"capacity of {r['resource']}: row '{r['row']}'")
    rows_by_path = {r["path"]: r for r in (sensitivity or {}).get("factors") or []
                    if isinstance(r, dict) and "path" in r}
    paths = (sensitivity or {}).get("changes_answer") or []
    for path in paths if isinstance(paths, list) else []:
        # #944: the sensitivity argument is LLM-shaped and the runs are already spent: a bad
        # path is left out with a note, never the whole answer.
        if not isinstance(path, str):
            notes.append(f"path left out: {path!r} is not a path")
            continue
        if len(out) >= limit or path in {f["path"] for f in out}:
            continue
        try:
            factor = _sensitivity_factor(model, rows_by_path.get(path, {"path": path}))
        except (ValueError, TypeError) as exc:     # ModelValidationError is a ValueError
            notes.append(f"path left out: {path}: {exc}")
            continue
        out.append(factor)
        why.append(f"{path}: a goal verdict flips over its sensitivity range")
    if not out:
        notes.append("no factor: no resource row with a whole-number capacity and no "
                     "sensitivity path that changes the answer")
    return {"factors": out, "from": why, "notes": notes}


# ---------------------------------------------------------------- runner
def explain_kpi(layout, kpi: str, overrides: list | None = None, *, replications: int = 5,
                cap: int = 50, seed: int = 0, run_length: float | None = None,
                warmup: float | None = None, should_stop=None) -> dict:
    """Explain *kpi* of *layout* in one call. Without *overrides*: one batch of
    *replications* (at least 2) and `rows_of`. With them: `experiments.paired_until_clear`
    on the KPI (start *replications*, doubled up to *cap*) and `compare`.

    Returns the rows' dict plus {"mode": "level" | "change", "n", "completed", "unit" (the
    model's time unit for a time KPI, "entities" for WIP, else None), "suggested_test":
    {label, tool, arguments, text} or None}. Raises ValueError for a
    KPI it does not explain, ModelValidationError for an override the model cannot take.
    """
    from nagasim import api, experiments
    from nagasim.core.schema import load_layout

    family = _family(kpi)       # refuse an unexplained KPI before any run
    model = load_layout(layout)
    blocks = blocks_of(model)
    n = max(2, int(replications))
    if overrides:
        n, _met, result, _table = experiments.paired_until_clear(
            model, overrides, [kpi], start=n, cap=cap, seed=seed, run_length=run_length,
            warmup=warmup, should_stop=should_stop)
        if result is None:
            return {"kpi": kpi, "mode": "change", "n": n, "completed": False, "rows": [],
                    "suggested_test": None, "summary": "stopped before a verdict", "notes": []}
        answer = compare(table_of(result.results[experiments.BASELINE]),
                         table_of(result.results[experiments.WHAT_IF]), kpi, blocks)
        mode = "change"
    else:
        results = api.run_model(model, replications=n, seed=seed, run_length=run_length,
                                warmup=warmup, should_stop=should_stop)
        if results is None:
            return {"kpi": kpi, "mode": "level", "n": n, "completed": False, "rows": [],
                    "suggested_test": None, "summary": "stopped before the batch finished",
                    "notes": []}
        answer = rows_of(table_of(results), kpi, blocks)
        mode = "level"
    test = None
    # The top resource row (#857): "processing and moving" can rank first on a lightly
    # loaded line, and the test is still for the largest resource row.
    top = next((r for r in answer["rows"] if r["resource"]), None)
    if top is not None:
        test, note = suggested_test(model, top["resource"], kpi, overrides)
        if note:
            answer["notes"].append(note)
    unit = ((model.get("simulation") or {}).get("time_unit", "minutes") if family == "time"
            else "entities" if family == "wip" else None)
    return {**answer, "mode": mode, "n": n, "completed": True, "unit": unit,
            "suggested_test": test}


# ---------------------------------------------------------------- text and CLI
def _g(value) -> str:
    return "-" if value is None else f"{value:.4g}"


def render(answer: dict) -> str:
    """The answer as plain text, one row a line."""
    mode, lines = answer.get("mode", "level"), []
    kpi = answer["kpi"] + (f" ({answer['unit']})" if answer.get("unit") else "")
    if not answer.get("completed", True):
        return f"{kpi}: {answer['summary']}"
    v = answer["value"]
    if mode == "change":
        lines.append(f"{kpi} ({answer['n']} paired replications): {_g(v['baseline'])} -> "
                     f"{_g(v['scenario'])}, difference {_g(v['diff'])} "
                     f"[{_g(v['diff_lb'])}, {_g(v['diff_ub'])}] {v['verdict']}")
        for i, r in enumerate(answer["rows"], 1):
            lines.append(f"{i:>3}. {r['row']}: {_g(r['baseline'])} -> {_g(r['scenario'])}, "
                         f"difference {_g(r['diff'])} [{_g(r['diff_lb'])}, {_g(r['diff_ub'])}] "
                         f"{r['verdict']}  ({r['label']})")
    else:
        lines.append(f"{kpi} ({answer['n']} replications): {_g(v['mean'])} "
                     f"[{_g(v['lb'])}, {_g(v['ub'])}]")
        for i, r in enumerate(answer["rows"], 1):
            share = f"  {r['share']:.0%}" if r.get("share") is not None else ""
            beside = "".join(f"  {k} {_g(x)}" for k, x in (r.get("beside") or {}).items())
            lines.append(f"{i:>3}. {r['row']}: {_g(r['mean'])} [{_g(r['lb'])}, {_g(r['ub'])}]"
                         f"{share}{beside}  ({r['label']})")
    lines.append(answer["summary"])
    lines.extend(answer["notes"])
    test = answer.get("suggested_test")
    if test:
        lines.append(f"{test['label']}: {test['text']}; {test['tool']} "
                     f"{json.dumps(test['arguments'])}")
    return "\n".join(lines)


def _override(text: str) -> dict:
    path, sep, value = text.partition("=")
    if not sep or not path:
        raise argparse.ArgumentTypeError(f"--set takes PATH=VALUE, not {text!r}")
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    return {"path": path, "value": parsed}


def main(argv=None) -> int:
    """`naga explain MODEL KPI [--set PATH=VALUE ...]`."""
    from nagasim._cli import add_model_argument, setup_cli

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga explain",
        description="Rank the measured contributors to a KPI, each with its 95% interval, "
                    "and suggest one what-if test of the top one. With --set, compare the "
                    "baseline and the changed model on paired seeds instead.")
    add_model_argument(parser)
    parser.add_argument("kpi", help="e.g. 'system/Avg Throughput Time', "
                                    "'system/Throughput (per time unit)' or "
                                    "'resource/<name>/Usage %% (snapshot)'")
    parser.add_argument("--set", action="append", type=_override, default=None,
                        dest="overrides", metavar="PATH=VALUE",
                        help="an override (the scenario path grammar); may repeat. The value "
                             "is read as JSON, else as text")
    parser.add_argument("--replications", type=int, default=5,
                        help="replications (default 5); with --set the first count, doubled "
                             "up to 50 until the KPI's difference is clear")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        answer = explain_kpi(args.model, args.kpi, args.overrides,
                             replications=args.replications, seed=args.seed)
    except ValueError as exc:       # ModelValidationError included
        print(f"naga explain: {exc}")
        return 2
    print(render(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
