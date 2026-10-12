"""Headless performance benchmark suite (ROADMAP 2.5).

Runs a fixed set of models down the same path a real run takes (`setup_run`,
which starts the time-series sampler exactly as a normal run does, then
`step_until`, then `calc_kpis`) and prints one table: model, sim time,
entities completed, SimPy events processed, run seconds (setup + step_until),
kpi seconds (`calc_kpis`), total seconds, events/s, entities/s, peak memory
(MB). Stdlib + the package only, no pytest-benchmark.

    PY tools/bench.py                          # print the table
    PY tools/bench.py --models mm1_100k        # a subset
    PY tools/bench.py --repeat 5               # minimum of 5 timed runs (default 3)
    PY tools/bench.py --no-memory              # skip the tracemalloc pass
    PY tools/bench.py --json out.json          # also write raw results
    PY tools/bench.py --compare baseline.json  # print the ratio vs a saved run

Times are CPU seconds (`time.process_time`), the minimum over --repeat runs;
no tracemalloc and no event counting inside them. One extra pass per model
counts SimPy events and, unless --no-memory, runs under `tracemalloc` for the
peak-memory column (it is slow and only meaningful for memory). Models that
run several replications (`many_short_reps`) sum all replications in one
repetition.

`--compare` reads any older file: a model or column the saved run lacks is
shown as n/a. The old `wall_seconds` column (step_until only, no sampler) is
compared against the new `run_seconds`. `tools/bench_baseline.json` is the
pre-optimisation snapshot from this machine. `mm1_100k` is now time-limited
(run length 100,000) so the sampler runs; the old file drained 100k arrivals
without it, so its figures are not like-for-like.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nagasim.core.sim_runner import setup_run, step_until  # noqa: E402
from nagasim.core.outputs import calc_kpis  # noqa: E402

MODELS_DIR = REPO_ROOT / "nagasim" / "models"

try:
    import psutil
    _HAVE_PSUTIL = True
except ImportError:
    _HAVE_PSUTIL = False


# ---------------------------------------------------------------- models --
def _load(name: str) -> dict:
    return json.loads((MODELS_DIR / name).read_text())


def _mm1_100k() -> tuple[dict, float, float]:
    """M/M/1, rho=0.8, time limit 100,000 (~100k arrivals; single_server.json).
    Time-limited on purpose: that is the case where the sampler is active
    (a drained run has no horizon to sample over). The first version drained
    100k arrivals instead; the event counts differ by well under 0.1%."""
    layout = _load("single_server.json")
    layout["simulation"]["replications"] = 1
    return layout, 100_000.0, 0.0


def _jackson_100k() -> tuple[dict, float, float]:
    """The Jackson job-shop network, drained at ~100k arrivals."""
    layout = _load("job_shop_jackson.json")
    layout["blocks"][0]["params"]["max_arrivals"] = 100_000
    layout["simulation"]["replications"] = 1
    return layout, 0.0, 0.0


def _call_centre_20d() -> tuple[dict, float, float]:
    """call_centre_day.json's NHPP arrival schedule repeated over 20 days."""
    layout = _load("call_centre_day.json")
    sim = layout["simulation"]
    return layout, float(sim["time_limit"]) * 20, float(sim.get("warmup", 0))


def _block(name, btype, params, pos=(0, 0)):
    return {"name": name, "description": name, "type": btype, "position": list(pos), "params": params}


def _link(src, tgt):
    return {"source": src, "target": tgt}


def _batch_separate_match_chain(n: int = 50_000) -> tuple[dict, float, float]:
    """CREATE -> ASSIGN(random type) -> MATCH(2) -> SEPARATE -> BATCH(4) ->
    SEPARATE -> DISPOSE: exercises Batch/Separate/Match's store/live_entities
    bookkeeping repeatedly per entity."""
    layout = {
        "schema_version": 10,
        "blocks": [
            _block("create_1", "CREATE", {
                "e_type": "DEFAULT", "arrival_mode": "Interarrival", "schedule_name": "",
                "distribution": "Constant", "dist_params": {"value": 1.0}, "time_unit": "base",
                "entities_per_arrival": 1, "max_arrivals": n, "start_time": 0}),
            _block("assign_1", "ASSIGN", {
                "assignments": [{"target": "entity.e_type",
                                 "expr": "'A' if RAND() < 0.5 else 'B'"}]}),
            _block("match_1", "MATCH", {
                "number_to_match": 2, "batch_option": True,
                "batched_entity_type": "MATCHED", "type_store_map": {}}),
            _block("separate_1", "SEPARATE", {"mode": "Retain original entity values", "attrs": []}),
            _block("batch_1", "BATCH", {"batch_size": 4, "batched_entity_type": "GROUP"}),
            _block("separate_2", "SEPARATE", {"mode": "Retain original entity values", "attrs": []}),
            _block("dispose_1", "DISPOSE", {"distribution": "Constant", "dist_params": {"value": 0.0},
                                            "time_unit": "base"}),
        ],
        "links": [_link("create_1", "assign_1"), _link("assign_1", "match_1"),
                  _link("match_1", "separate_1"), _link("separate_1", "batch_1"),
                  _link("batch_1", "separate_2"), _link("separate_2", "dispose_1")],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    return layout, 0.0, 0.0


def _expression_heavy(n: int = 50_000) -> tuple[dict, float, float]:
    """CREATE -> ASSIGN(4 expressions) -> DECIDE(2 conditions + ELSE) ->
    RECORD(tally) -> DISPOSE, all three branches sharing one RECORD/DISPOSE."""
    layout = {
        "schema_version": 10,
        "blocks": [
            _block("create_1", "CREATE", {
                "e_type": "DEFAULT", "arrival_mode": "Interarrival", "schedule_name": "",
                "distribution": "Constant", "dist_params": {"value": 1.0}, "time_unit": "base",
                "entities_per_arrival": 1, "max_arrivals": n, "start_time": 0}),
            _block("assign_1", "ASSIGN", {"assignments": [
                {"target": "entity.size", "expr": "UNIF(1, 10)"},
                {"target": "entity.priority", "expr": "min(max(entity.size * 2 - 3, 0), 20)"},
                {"target": "score", "expr": "sqrt(entity.size) + log(entity.priority + 1)"},
                {"target": "entity.cost", "expr": "score * 1.5 if entity.size > 5 else score"},
            ]}),
            _block("decide_1", "DECIDE", {
                "mode": "Condition",
                "conditions": ["entity.size > 7", "entity.priority < 5"], "probs": []}),
            _block("record_1", "RECORD", {"stat_name": "cost_tally", "kind": "tally",
                                          "expr": "entity.cost"}),
            _block("dispose_1", "DISPOSE", {"distribution": "Constant", "dist_params": {"value": 0.0},
                                            "time_unit": "base"}),
        ],
        "links": [_link("create_1", "assign_1"), _link("assign_1", "decide_1"),
                  _link("decide_1", "record_1"), _link("decide_1", "record_1"),
                  _link("decide_1", "record_1"), _link("record_1", "dispose_1")],
        "resources": [], "resource_sets": [], "entities": [], "variables": [{"name": "score", "initial": 0}],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    return layout, 0.0, 0.0


def _overloaded_single_server(time_limit: float = 16_000.0) -> tuple[dict, float, float]:
    """single_server.json with arrivals at 3/min against service at 1.25/min:
    the wait queue grows ~1.75/min, so it ends in the thousands (a quadratic
    wait-queue sort shows up here and not in the stable models)."""
    layout = _load("single_server.json")
    layout["blocks"][0]["params"]["dist_params"] = {"rate": 3.0}
    layout["simulation"]["replications"] = 1
    return layout, time_limit, 0.0


def _overloaded_resource_set(time_limit: float = 8_000.0) -> tuple[dict, float, float]:
    """two_teller_set.json (two-member set) with arrivals at 3.5/min against
    2 x 1/min service: the queue grows ~1.5/min."""
    layout = _load("two_teller_set.json")
    layout["blocks"][0]["params"]["dist_params"] = {"rate": 3.5}
    layout["simulation"]["replications"] = 1
    return layout, time_limit, 0.0


def _biogas_plant() -> tuple[dict, float, float]:
    """The bundled continuous model (levels, flows, submodels), as saved."""
    layout = _load("biogas_plant_library.json")
    sim = layout["simulation"]
    layout["simulation"]["replications"] = 1
    return layout, float(sim["time_limit"]), float(sim.get("warmup", 0))


def _many_short_reps() -> tuple[dict, float, float]:
    """single_server.json for 50 time units (~50 arrivals) with a warm-up:
    per-replication overhead (setup, calc_kpis) dominates. Replication count:
    REPLICATIONS below."""
    layout = _load("single_server.json")
    layout["simulation"]["replications"] = 1
    return layout, 50.0, 5.0


MODELS = {
    "mm1_100k": _mm1_100k,
    "jackson_100k": _jackson_100k,
    "call_centre_20d": _call_centre_20d,
    "batch_separate_match_chain": _batch_separate_match_chain,
    "expression_heavy": _expression_heavy,
    "overloaded_single_server": _overloaded_single_server,
    "overloaded_resource_set": _overloaded_resource_set,
    "biogas_plant": _biogas_plant,
    "many_short_reps": _many_short_reps,
}
REPLICATIONS = {"many_short_reps": 400}     # default 1; each runs setup -> step -> kpis, like a study


# --------------------------------------------------------------- running --
def _run_path(layout: dict, run_length: float, warmup: float, reps: int,
              seeds_from: int = 0, count_events: bool = False) -> dict:
    """One benchmark repetition down the real path (`run_single_simulation`'s
    own three steps, so the sampler starts as in a normal run). The setup and
    step_until are timed together as `run`, calc_kpis as `kpis`, in CPU
    seconds; the per-step counter only exists when *count_events* is set,
    because wrapping env.step costs time."""
    run = kpis_t = 0.0
    events = 0
    kpis = None
    for seed in range(seeds_from, seeds_from + reps):
        t0 = time.process_time()
        env, _model = setup_run(copy.deepcopy(layout), seed=seed, run_len=run_length, warmup=warmup)
        if count_events:
            orig_step = env.step

            def _counting_step(orig_step=orig_step):
                nonlocal events
                events += 1
                return orig_step()

            env.step = _counting_step
        step_until(env, run_length)
        t1 = time.process_time()
        kpis = calc_kpis(env, warmup=warmup, run_id=seed)
        t2 = time.process_time()
        run += t1 - t0
        kpis_t += t2 - t1
    return {"run": run, "kpis": kpis_t, "events": events, "last_kpis": kpis}


def run_one(name: str, repeat: int = 3, memory: bool = True) -> dict:
    layout, run_length, warmup = MODELS[name]()
    reps = REPLICATIONS.get(name, 1)

    # Time: no tracemalloc, no event counting; the minimum total wins and its
    # run/kpis split is reported (the two minima separately would not add up).
    best = min((_run_path(layout, run_length, warmup, reps) for _ in range(max(1, repeat))),
               key=lambda r: r["run"] + r["kpis"])
    run_s, kpi_s = best["run"], best["kpis"]
    total = run_s + kpi_s

    # Audit pass: events and (unless --no-memory) peak memory.
    if memory:
        tracemalloc.start()
    audit = _run_path(layout, run_length, warmup, reps, count_events=True)
    peak = 0
    if memory:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    sys_df = audit["last_kpis"]["system"]
    # Only the last replication's frame is kept, so scale it by reps (an
    # estimate for many_short_reps; exact for the single-replication models).
    completed = int(sys_df.loc[sys_df["Metric"] == "Total Entities Completed", "Value"].iloc[0]) * reps
    sim_time = float(sys_df.loc[sys_df["Metric"] == "Simulation Time", "Value"].iloc[0])
    events = audit["events"]

    result = {
        "model": name,
        "sim_time": sim_time,
        "entities_completed": completed,
        "events": events,
        "run_seconds": run_s,
        "kpi_seconds": kpi_s,
        "total_seconds": total,
        "events_per_s": events / run_s if run_s > 0 else float("nan"),
        "entities_per_s": completed / total if total > 0 else float("nan"),
        "peak_memory_mb": peak / 1_048_576 if memory else float("nan"),
    }
    if _HAVE_PSUTIL:
        result["rss_after_mb"] = psutil.Process().memory_info().rss / 1_048_576
    return result


def run_all(names=None, repeat: int = 3, memory: bool = True) -> list[dict]:
    return [run_one(name, repeat, memory) for name in (names or MODELS)]


# ---------------------------------------------------------------- report --
_COLS = ["model", "sim_time", "entities_completed", "events", "run_seconds", "kpi_seconds",
         "total_seconds", "events_per_s", "entities_per_s", "peak_memory_mb"]


def _cell(value) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def print_table(results: list[dict]) -> None:
    widths = {c: max(len(c), *(len(_cell(r[c])) for r in results)) for c in _COLS}
    header = "  ".join(c.ljust(widths[c]) for c in _COLS)
    print(header)
    print("-" * len(header))
    for r in results:
        print("  ".join(_cell(r[c]).ljust(widths[c]) for c in _COLS))


# new column -> the old column it is compared against (older files lack the new ones).
_COMPARE = {"run_seconds": "wall_seconds", "kpi_seconds": None, "total_seconds": None,
            "events_per_s": "events_per_s", "entities_per_s": "entities_per_s",
            "peak_memory_mb": "peak_memory_mb"}


def print_compare(results: list[dict], baseline: list[dict]) -> None:
    base_by_model = {b["model"]: b for b in baseline}
    print(f"{'model':30}" + "".join(f"{c + ' (x)':>20}" for c in _COMPARE))
    for r in results:
        b = base_by_model.get(r["model"])
        if b is None:
            print(f"{r['model']:30}  n/a (no baseline entry)")
            continue
        cells = []
        for new_col, old_col in _COMPARE.items():
            old = b.get(new_col, b.get(old_col)) if old_col else b.get(new_col)
            new = r[new_col]
            if not old or old != old or new != new:      # missing, zero or NaN
                cells.append(f"{'n/a':>20}")
            else:
                cells.append(f"{new / old:>19.2f}x")
        print(f"{r['model']:30}" + "".join(cells))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Naga performance benchmark suite (ROADMAP 2.5)")
    parser.add_argument("--models", nargs="*", choices=list(MODELS), help="default: all")
    parser.add_argument("--json", help="write raw results here")
    parser.add_argument("--compare", help="print the ratio against this baseline JSON")
    parser.add_argument("--repeat", type=int, default=3, help="timed repetitions, minimum wins (default 3)")
    parser.add_argument("--no-memory", action="store_true", help="skip the tracemalloc pass")
    args = parser.parse_args(argv)

    results = run_all(args.models, repeat=args.repeat, memory=not args.no_memory)
    print_table(results)

    if args.compare:
        baseline = json.loads(Path(args.compare).read_text())
        print()
        print_compare(results, baseline)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nWritten to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
