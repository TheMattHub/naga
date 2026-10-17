"""Loop dominance: which feedback loop drives an SD model's behaviour, and when
(depth bucket L1, D141-D143). Loops That Matter (Schoenberg, Davidsen and
Eberlein, SDR 2020; the revised flow score of "Improving LTM", isee v2.1);
exact definitions in docs/history/LOOP-DOMINANCE-SEMANTICS-2026-09-27.md.

    from nagasim import loopdominance
    result = loopdominance.analyse("nagasim/models/sd_sir.json")
    result["loops"]            # Loop, Kind, Partition, Length, Nodes, Total
    result["loop_dominance"]   # Time, Loop, Partition, Score, Relative, Dominant

An analysis, not a model setting: nothing in the model file, no cost to an
ordinary run. Top-level module beside `modelcheck.py` (pandas, `api.run_model`).

DATA PATH (D141). Flatten and expand the layout, un-hide the generated delay
stages, add one probe auxiliary per pipeline/sample/noise delay, sample the
"timeseries" frame on the grid step and run ONE replication. Only the levels
(and those delays' outputs, and conveyor outflows, which have no equation) are
read back; every auxiliary, flow and material/information delay output is
RECOMPUTED from its equation at the recorded levels, in a stand-in context
(the `modelcheck._row_scope` pattern), so Euler and RK4 share one definition.

SCORES (D142). Per grid step t (first row t0 + dt): an instantaneous link
x -> z scores |Dxz/Dz| * sign(Dxz/Dx), Dxz = z(x at t, every other input and
the clock at t-dt) - z(t-dt); flow -> stock scores +-|e*Dflow/Dnet|; exact zero
guards. Loop score = product of its links; relative = score / sum |score| over
the loops of its partition (an SCC of stock -> stock reachability); the
dominant set is the smallest top set reaching 50%.

STRUCTURE (D143). Material/information delays are scored through their
stages; pipeline/sample/noise delays and conveyors are +1 black boxes (1 when
both ends changed, else 0). Elementary circuits up to `limit`; above it the
strongest-path search (Finding the Loops that Matter, Appendix I) at every
scored step, kept at 0.1% of partition mass at some step, 200 reported.
"""
from __future__ import annotations

from nagasim._cli import add_model_argument

import argparse
import json
import logging
import math
import sys
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from nagasim import api
from nagasim.core import sd, sd_loops, submodels
from nagasim.core.calendar import model_calendar
from nagasim.core.expressions import ExpressionScope, compile_expression
from nagasim.core.initial_state import schedule_offset
from nagasim.core.problems import problem
from nagasim.core.tables import build_tables
from nagasim.core.timeseries_inputs import build_series_table
from nagasim.core.timeunits import ModelUnits, base_factor

__all__ = ["analyse", "LoopDominanceError", "DOMINANCE_COLUMNS", "LOOP_COLUMNS", "main"]

log = logging.getLogger(__name__)

DOMINANCE_COLUMNS = ["Time", "Loop", "Partition", "Score", "Relative", "Dominant"]
LOOP_COLUMNS = ["Loop", "Kind", "Partition", "Length", "Nodes", "Total"]
MAX_STEPS = 5000        # above it every k-th grid step is scored (SDR 2020 allows it)
RETAIN = 1e-3           # strongest path: kept at 0.1% of partition mass at some step
MAX_REPORTED = 200
_PROBE = "~ltm/"        # probe auxiliary reading a stateless delay's DELAY() output
_STATEFUL = ("pipeline", "sample", "noise")


class LoopDominanceError(ValueError):
    """A refusal carrying its coded `Problem` (E_LOOP_GRID)."""

    def __init__(self, found):
        super().__init__(found.message)
        self.problem = found


# ---------------------------------------------------------------- layout
def _unhidden(layout: dict) -> dict:
    out = dict(layout)
    for section in ("levels", "flows", "auxiliaries", "delays"):
        out[section] = [{k: v for k, v in r.items() if k != "hidden"}
                        for r in layout.get(section) or []]
    return out


def _graph_layout(layout: dict) -> dict:
    """What `sd_loops.dependency_graph` reads for LTM (D143): no `initial` edges, a
    material/information delay reads its stages through `output_text`, a
    pipeline's delay_time (read once, at build) is no edge."""
    out = dict(layout, subscripts=[])
    out["levels"] = [{k: v for k, v in r.items() if k != "initial"} for r in layout["levels"]]
    delays = []
    for d in layout["delays"]:
        kind = d.get("kind", "material")
        text = sd.output_text(d) if kind in ("material", "information") else d.get("input")
        entry = {"name": d["name"], "kind": kind, "locals": d.get("locals")}
        if kind != "noise":
            entry["input"] = text
        delays.append(entry)
    out["delays"] = delays
    return out


def _owner(name: str, visible: set) -> str:
    """The visible record a hidden one belongs to: a stage `d~s1` shows as
    delay `d`, an inline macro's `f#1~s1` (and `f#1` itself) as its owner `f`."""
    if name in visible:
        return name
    base = name.split("~", 1)[0]
    return base if base in visible else base.split("#", 1)[0]


# ---------------------------------------------------------------- run
def _run(layout: dict, stateful: list, seed: int, samples: int, run_len: float) -> pd.DataFrame:
    run = dict(layout)
    run["auxiliaries"] = list(layout["auxiliaries"]) + [
        {"name": _PROBE + name, "expr": f"DELAY({json.dumps(name)})"} for name in stateful]
    run["simulation"] = {**(layout.get("simulation") or {}), "timeseries_points": samples}
    results = api.run_model(run, replications=1, seed=seed, run_length=run_len, record_logs=False)
    frame = None if results is None else results.get("timeseries")
    if frame is None or frame.empty:
        raise ValueError("loop dominance: the run produced no timeseries")
    return frame.pivot(index="Time", columns="Series", values="Value").sort_index()


def _context(layout: dict, run_len: float):
    """The stand-in ctx every recomputation reads (`modelcheck._row_scope`):
    stub levels/flows/delays, AUX() through a pre-filled `aux_memo`, the
    model's variables at their initial values, its tables and TS() profiles."""
    sim = layout.get("simulation") or {}
    env = SimpleNamespace(now=0.0)
    system = SimpleNamespace(levels={}, flow_index={}, delays={}, _last_t=0.0, env=env,
                             peeking=False, _initialising=False)
    ctx = SimpleNamespace(
        now=0.0, aux_memo={}, auxiliaries=sd.build_auxiliaries(layout), level_system=system,
        variables={v["name"]: v.get("initial", 0) for v in layout.get("variables") or []},
        tables=build_tables(layout.get("tables") or []),
        series=build_series_table(layout.get("timeseries_inputs") or [],
                                  sim.get("time_unit", "minutes"), schedule_offset(layout),
                                  model_calendar(layout)),
        sim_horizon=run_len, deterministic=True, time_unit=sim.get("time_unit", "minutes"))

    def at(t):
        ctx.now = env.now = system._last_t = t
    return ctx, at


# ---------------------------------------------------------------- loops
def _partitions(graph: dict, stocks: list) -> dict:
    """{stock: partition key}: SCCs of stock -> stock reachability.
    ponytail: O(stocks^2) closure; Tarjan if a model with thousands of stocks
    ever needs it."""
    stock_set, direct = set(stocks), {}
    for s in stocks:
        seen, todo, hit = set(), list(graph[s]), set()
        while todo:
            v = todo.pop()
            if v in seen:
                continue
            seen.add(v)
            if v in stock_set:
                hit.add(v)
            else:
                todo.extend(graph[v])
        direct[s] = hit
    reach = {}
    for s in stocks:
        seen, todo = set(), list(direct[s])
        while todo:
            v = todo.pop()
            if v not in seen:
                seen.add(v)
                todo.extend(direct[v])
        reach[s] = seen
    key = {}
    for s in sorted(stocks):
        if s not in key:
            for u in [s] + [u for u in reach[s] if s in reach[u]]:
                key[u] = s
    return key


def _canonical(cycle: tuple) -> tuple:
    k = cycle.index(min(cycle))
    return cycle[k:] + cycle[:k]


def _strongest(graph: dict, stocks: list, ls: np.ndarray, index: dict) -> set:
    """Strongest-path loop discovery (Finding, Appendix I) at every scored
    step, zero links skipped, ties by node name. ponytail: recursive, so a
    path over ~900 nodes hits Python's recursion limit; exponential worst case
    (the published heuristic's own)."""
    found: set = set()
    for t in range(ls.shape[1]):
        out = {v: sorted(((abs(ls[index[(v, w)], t]), w) for w in graph[v]
                          if ls[index[(v, w)], t] != 0), key=lambda p: (-p[0], p[1][1], p[1][0]))
               for v in graph}
        for target in stocks:
            best: dict = {}
            stack: list = []
            on: set = set()

            def visit(v, score):
                if v in on:
                    if v == target:
                        found.add(_canonical(tuple(stack)))
                    return
                if score < best.get(v, 0.0):
                    return
                best[v] = score
                on.add(v)
                stack.append(v)
                for s, w in out[v]:
                    visit(w, score * s)
                on.discard(v)
                stack.pop()
            visit(target, 1.0)
    return found


def _relative(scores: np.ndarray, part: np.ndarray) -> np.ndarray:
    rel = np.zeros_like(scores)
    for p in np.unique(part):
        rows = part == p
        denom = np.abs(scores[rows]).sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            rel[rows] = np.where(denom > 0, scores[rows] / denom, 0.0)
    return rel


def _dominant(rel: np.ndarray, part: np.ndarray) -> np.ndarray:
    """Per step and partition, the smallest set by descending |relative|
    whose |relative| sum reaches 0.5."""
    out = np.zeros(rel.shape, dtype=bool)
    for p in np.unique(part):
        ids = np.flatnonzero(part == p)
        mag = np.abs(rel[ids])
        order = np.argsort(-mag, axis=0, kind="stable")
        ranked = np.take_along_axis(mag, order, axis=0)
        before = np.cumsum(ranked, axis=0) - ranked
        keep = (before < 0.5) & (ranked > 0)
        cols = np.broadcast_to(np.arange(rel.shape[1]), order.shape)
        out[ids[order[keep]], cols[keep]] = True
    return out


# ---------------------------------------------------------------- analysis
def analyse(layout, *, seed: int = 0, run_length=None, limit: int = 1000) -> dict:
    """Loops That Matter over one grid-sampled run of *layout* (a path or a
    dict). *run_length* falls back to the model's own; *limit* is the
    exhaustive-search threshold (elementary circuits), above which the
    strongest-path search takes over.

    Returns {"loop_dominance": DataFrame[DOMINANCE_COLUMNS] (one row per loop
    per scored step), "loops": DataFrame[LOOP_COLUMNS] (Kind R/B/U/D from the
    run, Total the mean relative score, Nodes the visible record names),
    "truncated" (True when the strongest-path search replaced enumeration),
    "step" (the scoring interval), "problems" (W_LOOP_HYBRID)}.
    Raises LoopDominanceError (E_LOOP_GRID) without `simulation.sd`."""
    base = api.load_layout(layout)
    if not ((base.get("simulation") or {}).get("sd") or {}).get("dt"):
        raise LoopDominanceError(problem(
            "E_LOOP_GRID", "loop dominance needs the SD step grid (simulation.sd)",
            "/simulation/sd", hint="set simulation.sd = {dt, method} (Run > Run experiment)"))
    flat = sd.expand(submodels.flatten(base))
    problems = []
    if flat.get("blocks") or flat.get("populations"):
        problems.append(problem(
            "W_LOOP_HYBRID", "loop dominance scores the SD layer only: variables written by "
            "DES blocks or agents are read at their initial values", "/"))
        log.warning(problems[-1].message)

    dt = sd._grid_dt(flat)
    horizon = api.run_settings(base, 1, 0.0, run_length)[2]
    steps = int(math.floor(horizon / dt + 1e-9)) if horizon else 0
    if steps < 1:
        raise ValueError("loop dominance needs a run length of at least one grid step")
    stride = math.ceil(steps / MAX_STEPS)
    samples = steps // stride
    step = stride * dt

    visible = {str(r["name"]) for s in ("levels", "flows", "auxiliaries", "delays")
               for r in flat.get(s) or [] if not r.get("hidden")}
    lay = _unhidden(flat)
    delays = {str(d["name"]): d for d in lay["delays"]}
    stateful = [n for n, d in delays.items() if d.get("kind", "material") in _STATEFUL]
    conveyors = {str(r["name"]) for r in lay["levels"] if r.get("conveyor")}
    flows = {str(f["name"]): f for f in lay["flows"]}

    glay = _graph_layout(lay)
    nodes, _signs, graph = sd_loops.dependency_graph(glay, probe=False)
    for section, field in (("flows", "rate"), ("auxiliaries", "expr"), ("delays", "input")):
        for r in glay[section]:          # FLOW() reads: flow -> reader
            target = (section, str(r["name"]))
            for ref in sd_loops.refs(r.get(field)).get("FLOW", ()):
                if ("flows", ref) in nodes and ("flows", ref) != target:
                    graph[("flows", ref)].add(target)
    for name, f in flows.items():                       # conveyor -> each outflow (+1 box)
        if f.get("from") in conveyors:
            graph[("levels", f["from"])].add(("flows", name))

    pivot = _run(lay, stateful, seed, samples, samples * step)
    times = pivot.index.to_numpy(dtype=float)
    order = sorted(nodes)
    at_ = {node: j for j, node in enumerate(order)}
    values = np.zeros((len(order), len(times)))
    ctx, at = _context(lay, samples * step)
    system, memo = ctx.level_system, ctx.aux_memo
    base_unit = (lay.get("simulation") or {}).get("time_unit", "minutes")

    setters, recorded, computed, to_base = [], [], {}, {}
    for j, (section, name) in enumerate(order):
        column = None
        if section == "levels":
            stub = system.levels[name] = SimpleNamespace(level=0.0)
            setters.append(lambda v, o=stub: setattr(o, "level", v))
            column = f"level/{name}"
        elif section == "flows":
            # Values here are base-unit rates; FLOW() and the trace are per the
            # flow's own rate_time_unit (#678), so the stub carries `per` and a
            # recorded column is turned back into the base unit.
            per = base_factor("time", flows[name].get("rate_time_unit", "base") or "base",
                              ModelUnits(base_unit))
            stub = system.flow_index[name] = SimpleNamespace(actual=0.0, per=per)
            setters.append(lambda v, o=stub: setattr(o, "actual", v))
            if flows[name].get("from") in conveyors:
                column = f"flow/{name}"
                to_base[j] = 1.0 / per
            else:
                computed[j] = _flow_eval(flows[name], ctx, base_unit)
        elif section == "delays":
            stub = sd_loops.StubOutput(0.0)
            system.delays[name] = (stub, None)
            setters.append(lambda v, o=stub: setattr(o, "value", v))
            if name in stateful:
                column = f"aux/{_PROBE}{name}"
            else:
                d = delays[name]
                computed[j] = _eval(compile_expression(sd.output_text(d)),
                                    ExpressionScope(ctx=ctx, locals=d.get("locals")))
        else:
            setters.append(lambda v, n=name: memo.__setitem__(n, v))
            expr, own = ctx.auxiliaries[name]
            computed[j] = _eval(expr, ExpressionScope(ctx=ctx, locals=own))
        if column is not None:
            if column not in pivot:
                raise ValueError(f"loop dominance: the run recorded no '{column}'")
            values[j] = pivot[column].to_numpy(dtype=float) * to_base.get(j, 1.0)
            recorded.append(j)

    deps = {j: set() for j in computed}
    for x, targets in graph.items():
        for z in targets:
            if at_[z] in computed and at_[x] in computed:
                deps[at_[z]].add(at_[x])
    try:
        topo = list(TopologicalSorter(deps).static_order())
    except CycleError as exc:
        raise ValueError(f"loop dominance: simultaneous equations (FLOW() cycle): "
                         f"{exc.args[1]}") from None

    edges = sorted((x, w) for x in graph for w in graph[x])
    index = {e: k for k, e in enumerate(edges)}
    partial = [(k, at_[x], at_[z]) for k, (x, z) in enumerate(edges) if at_[z] in computed]
    partials = np.zeros((len(edges), len(times)))
    for i, t in enumerate(times):
        at(t)
        for j in recorded:
            setters[j](values[j, i])
        for j in topo:
            values[j, i] = v = _checked(computed[j], order[j], t)
            setters[j](v)
        if i == 0:
            continue
        at(times[i - 1])
        for j in range(len(order)):
            setters[j](values[j, i - 1])
        for k, x, z in partial:
            setters[x](values[x, i])
            partials[k, i] = _checked(computed[z], order[z], times[i - 1])
            setters[x](values[x, i - 1])

    ls = _link_scores(edges, at_, values, partials, computed, flows)
    stocks = [n for n in order if n[0] == "levels" or (n[0] == "delays" and n[1] in stateful)]
    part_of = _partitions(graph, stocks)

    circuits, _ = sd_loops.elementary_circuits(graph, max(int(limit), 1) + 1)
    truncated = len(circuits) > max(int(limit), 1)
    if truncated:
        circuits = sorted(_strongest(graph, stocks, ls, index))
    loops = [c for c in circuits if any(n in part_of for n in c)]
    scores = np.array([np.prod(ls[[index[(c[i], c[(i + 1) % len(c)])] for i in range(len(c))]],
                               axis=0) for c in loops]).reshape(len(loops), ls.shape[1])
    part_key = [part_of[next(n for n in c if n in part_of)] for c in loops]
    if truncated:
        loops, scores, part_key = _retain(loops, scores, part_key)
    return _frames(loops, scores, part_key, times, visible, truncated, step, problems)


def _eval(expr, scope):
    return lambda: float(expr.evaluate(scope))


def _flow_eval(flow: dict, ctx, base_unit: str):
    """A flow's EQUATION value (semantics note section 2): its rate in the base
    time unit, the uniflow clamp at 0 and `min_rate`; no saturation."""
    scale = base_factor("rate", flow.get("rate_time_unit", "base") or "base", ModelUnits(base_unit))
    text = str(flow.get("rate") or "0")
    expr = compile_expression(text if scale == 1.0 else f"({text}) * {scale!r}")
    scope = ExpressionScope(ctx=ctx, locals=flow.get("locals"))
    biflow = bool(flow.get("biflow"))
    floor = None if flow.get("min_rate") is None else float(flow["min_rate"]) * scale

    def value():
        v = float(expr.evaluate(scope))
        if v < 0.0 and not biflow:
            v = 0.0
        return 0.0 if floor and abs(v) < floor else v
    return value


def _checked(fn, node, t):
    try:
        return fn()
    except Exception as exc:        # any evaluation failure, named at its record and time
        raise ValueError(f"loop dominance: {sd_loops.KIND[node[0]]} '{node[1]}' cannot be recomputed at "
                         f"t={t:g}: {exc}") from None


def _link_scores(edges, at_, values, partials, computed, flows):
    """One row per edge, one column per scored step (D142, D143)."""
    dv = np.diff(values, axis=1)
    ls = np.zeros((len(edges), dv.shape[1]))
    nets: dict = {}
    for f in flows.values():                # Dnet per level: sum e*Din - sum Dout
        j = at_[("flows", str(f["name"]))]
        for side, coef in (("to", float(f.get("efficiency", 1.0) or 1.0)), ("from", -1.0)):
            level = f.get(side)
            if level not in (None, "") and ("levels", level) in at_:
                nets[level] = nets.get(level, 0.0) + coef * dv[j]
    with np.errstate(divide="ignore", invalid="ignore"):
        for k, (x, z) in enumerate(edges):
            dx, dz = dv[at_[x]], dv[at_[z]]
            if x[0] == "flows" and z[0] == "levels":
                f = flows[x[1]]
                inflow = f.get("to") == z[1]
                coef = float(f.get("efficiency", 1.0) or 1.0) if inflow else 1.0
                dnet = nets[z[1]]
                ls[k] = np.where(dnet != 0, (1 if inflow else -1) * np.abs(coef * dx / dnet), 0.0)
            elif at_[z] in computed:
                dxz = partials[k, 1:] - values[at_[z], :-1]
                ls[k] = np.where((dz != 0) & (dx != 0), np.abs(dxz / dz) * np.sign(dxz) * np.sign(dx),
                                 0.0)
            else:                           # stateless delay input, conveyor -> outflow
                ls[k] = ((dx != 0) & (dz != 0)).astype(float)
    return ls


def _retain(loops, scores, part_key):
    """Strongest path: keep a loop reaching 0.1% of its partition's |score| at
    some step; at most 200, top-at-some-step loops first, then by mean |relative|."""
    ids = {}
    part = np.array([ids.setdefault(p, len(ids)) for p in part_key], dtype=int)
    mass = np.zeros_like(scores)
    for p in np.unique(part):
        rows = part == p
        mass[rows] = np.abs(scores[rows]).sum(axis=0)
    keep = np.flatnonzero(((np.abs(scores) >= RETAIN * mass) & (scores != 0)).any(axis=1))
    rel = np.abs(_relative(scores[keep], part[keep]))
    top = np.zeros(len(keep), dtype=bool)
    for p in np.unique(part[keep]):
        rows = np.flatnonzero(part[keep] == p)
        best = rows[np.argmax(rel[rows], axis=0)]
        top[best[rel[rows].max(axis=0) > 0]] = True
    ranked = sorted(range(len(keep)), key=lambda r: (not top[r], -rel[r].mean()))[:MAX_REPORTED]
    kept = sorted(keep[r] for r in ranked)
    return [loops[r] for r in kept], scores[kept], [part_key[r] for r in kept]


def _frames(loops, scores, part_key, times, visible, truncated, step, problems) -> dict:
    kinds = []
    for row in scores:
        pos, neg = bool((row > 0).any()), bool((row < 0).any())
        kinds.append("U" if pos and neg else "R" if pos else "B" if neg else "D")
    ranked = sorted(range(len(loops)), key=lambda r: (len(loops[r]), loops[r]))
    counters = {"R": 0, "B": 0, "U": 0, "D": 0}
    ids = [""] * len(loops)
    parts: dict = {}
    for r in ranked:
        counters[kinds[r]] += 1
        ids[r] = f"{kinds[r]}{counters[kinds[r]]}"
        parts.setdefault(part_key[r], len(parts) + 1)
    part = np.array([parts[p] for p in part_key], dtype=int)
    scores = scores[ranked] if len(loops) else scores
    part = part[ranked] if len(loops) else part
    rel = _relative(scores, part)
    dom = _dominant(rel, part)
    names = [ids[r] for r in ranked]
    n, steps = len(ranked), scores.shape[1]
    dominance = pd.DataFrame({
        "Time": np.repeat(times[1:1 + steps], n), "Loop": np.tile(np.array(names, dtype=object), steps),
        "Partition": np.tile(part, steps), "Score": scores.T.ravel(), "Relative": rel.T.ravel(),
        "Dominant": dom.T.ravel()}, columns=DOMINANCE_COLUMNS)
    rows = []
    for k, r in enumerate(ranked):
        shown = []
        for section, name in loops[r]:
            owner = _owner(name, visible)
            if not shown or shown[-1] != owner:
                shown.append(owner)
        if len(shown) > 1 and shown[0] == shown[-1]:
            shown.pop()
        rows.append({"Loop": names[k], "Kind": kinds[r], "Partition": int(part[k]),
                     "Length": len(loops[r]), "Nodes": shown,
                     "Total": float(rel[k].mean()) if steps else 0.0})
    return {"loop_dominance": dominance, "loops": pd.DataFrame(rows, columns=LOOP_COLUMNS),
            "truncated": truncated, "step": step, "problems": problems}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    """`naga loops model.json`: the loop table of a Loops That Matter
    analysis, headless. Dispatched from `nagasim.cli` without Qt."""
    parser = argparse.ArgumentParser(
        prog="naga loops",
        description="Loop dominance (Loops That Matter) of an SD model: which feedback loop "
                    "drives the behaviour and when. Needs simulation.sd.")
    add_model_argument(parser)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--run-length", type=float, default=None,
                        help="run length in base time units (default: the model's own)")
    parser.add_argument("--limit", type=int, default=1000,
                        help="elementary circuits enumerated before the strongest-path "
                             "search takes over (default: 1000)")
    parser.add_argument("--csv", metavar="FILE",
                        help="write the loop table to FILE and the per-step scores to "
                             "FILE's stem + '_dominance.csv'")
    args = parser.parse_args(argv)
    try:
        result = analyse(args.model, seed=args.seed, run_length=args.run_length, limit=args.limit)
    except LoopDominanceError as exc:
        sys.stderr.write(f"naga loops: {exc.problem.code}: {exc.problem.message}\n")
        return 1
    for found in result["problems"]:
        sys.stderr.write(f"warning: {found.code}: {found.message}\n")
    table = result["loops"].assign(Nodes=result["loops"]["Nodes"].map(" -> ".join))
    print(table.to_string(index=False) if len(table) else "no feedback loops")
    if result["truncated"]:
        print(f"(strongest-path search: more than {args.limit} loops, the strongest "
              f"{len(table)} kept)")
    if args.csv:
        path = Path(args.csv)
        table.to_csv(path, index=False)
        other = path.with_name(f"{path.stem}_dominance.csv")
        result["loop_dominance"].to_csv(other, index=False)
        print(f"Written {path} and {other}")
    return 0
