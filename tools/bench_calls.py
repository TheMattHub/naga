"""Deterministic cost of the engine models: cProfile call counts, per run phase (speed review 2026-11-23).

On a loaded machine wall-clock timings drift by tens of percent; the number of Python calls a model
makes for a fixed seed does not. This runs each model down the real path (`setup_run`, `step_until`,
`calc_kpis`) under cProfile, after one short warm-up run outside the profile, and prints the total
and primitive calls per phase, per entity and per SimPy event, and the heaviest functions. The counts
are identical run to run for one command. The garbage collector is collected before and held off
during each profiled phase: otherwise it closes the suspended entity processes of an earlier run
inside the profile at a moment that varies, and cProfile counts each close as a call (counts before
2026-11-23's review fix, finding 4, were taken with it on). A before/after proof still runs one model
per process.

    PY tools/bench_calls.py                          # every model
    PY tools/bench_calls.py --models tandem_lean     # a subset
    PY tools/bench_calls.py --json out.json          # also every function's ncalls/tottime, for a diff
    PY tools/bench_calls.py --cpu 7 --models mm1     # min/median/max process CPU of 7 unprofiled runs

Models: the nine of `tools/bench.py`, the four queueing models of `benchmarks/` (2 replications of
10,000, warm-up 1,000; `_lean` = logs and sampler off) and `conveyor_eyes_2000` (2,000 parts onto a
100 m belt with 20 eyes, `tests/regressions` R72). With PYTHONPATH pointing at an older tree, only
the queueing models run (`--models mm1 tandem_lean ...`): their documents come from this tree's
`benchmarks/with_naga.py`. Run with PYTHONHASHSEED=0: set iteration order then matches between runs.
"""
from __future__ import annotations

try:       # first: the shipped registries only, never the user's trusted plugins (tools/_isolate.py)
    from tools import _isolate  # noqa: F401
except ImportError:     # run as a script: tools/ itself is on sys.path
    import _isolate  # noqa: F401
import argparse
import copy
import cProfile
import gc
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
QUEUEING = ("mm1", "mm3", "tandem", "jackson")


def _file_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _queueing(name, lean):
    sys.path.insert(0, str(ROOT / "benchmarks"))       # with_naga imports `models` beside it
    try:
        models = _file_module("models", ROOT / "benchmarks" / "models.py")
        sys.modules["models"] = models
        with_naga = _file_module("bench_with_naga", ROOT / "benchmarks" / "with_naga.py")
    finally:
        sys.path.remove(str(ROOT / "benchmarks"))
    doc = with_naga.layout(models.MODELS[name][1], 10_000, 1_000, 2, lean=lean)
    return doc, 10_000.0, 1_000.0, 2


def _conveyor():
    for folder in (ROOT / "tests", ROOT / "tests" / "regressions"):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    from test_regressions_1600_up import _packed_belt_with_many_eyes
    return _packed_belt_with_many_eyes(2000), None, 0.0, 1


def build(name):
    """(document, run length or None for the model's own, warm-up, replications)."""
    base = name.removesuffix("_lean")
    if base in QUEUEING:
        return _queueing(base, name.endswith("_lean"))
    if name == "conveyor_eyes_2000":
        return _conveyor()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import bench
    doc, run_length, warmup = bench.MODELS[name]()
    return doc, run_length, warmup, bench.REPLICATIONS.get(name, 1)


def names():
    import bench    # noqa: F401  (tools/ is on sys.path when run as a script)
    from bench import MODELS
    return [*MODELS, *QUEUEING, *(f"{q}_lean" for q in QUEUEING), "conveyor_eyes_2000"]


def _run(doc, run_length, warmup, reps, profilers=None, upto=None):
    """One pass down the real path; *profilers* = {phase: cProfile.Profile} or None."""
    from nagasim.core.outputs import calc_kpis
    from nagasim.core.sim_runner import setup_run, step_until

    def phase(key, fn):
        prof = profilers and profilers[key]
        if not prof:
            return fn()
        collecting = gc.isenabled()
        gc.collect()
        gc.disable()
        prof.enable()
        try:
            return fn()
        finally:
            prof.disable()
            if collecting:
                gc.enable()
    for seed in range(reps):
        length = run_length
        if length is None:
            length = (doc.get("simulation") or {}).get("time_limit") or 0
        env, _model = phase("setup", lambda: setup_run(copy.deepcopy(doc), seed=seed, run_len=length or 0,
                                                       warmup=warmup))
        phase("step", lambda: step_until(env, upto if upto is not None else length))
        phase("kpis", lambda: calc_kpis(env, warmup=warmup, run_id=seed))


def _label(code):
    if isinstance(code, str):                   # a built-in: "<built-in method ...>"
        return ("~", 0, code)
    return (code.co_filename, code.co_firstlineno, code.co_name)


def _key(func):
    path, line, name = func
    path = path.replace("\\", "/")
    for marker in ("/nagasim/", "/simpy/", "/pandas/", "/numpy/"):
        if marker in path:
            path = marker.strip("/") + "/" + path.split(marker, 1)[1]
            break
    else:
        path = path.rsplit("/", 1)[-1]
    return f"{path}:{line}({name})"


def profile(name, top=25):
    doc, run_length, warmup, reps = build(name)
    _run(doc, run_length, warmup, 1, upto=min(50.0, run_length or 50.0))     # warm-up: imports, caches
    profilers = {k: cProfile.Profile() for k in ("setup", "step", "kpis")}
    _run(doc, run_length, warmup, reps, profilers)
    out = {"model": name, "replications": reps, "phases": {}, "functions": {}}
    fn = out["functions"]
    for key, prof in profilers.items():
        # The raw entries, not pstats: pstats keys by (file, line, name), so two generated
        # functions with one label (dataclass `<string>:2(__init__)`) overwrite each other in an
        # order that follows memory addresses, and its totals then differ from run to run.
        calls = primitive = 0
        for entry in prof.getstats():
            label = _key(_label(entry.code))
            nc, cc = entry.callcount, entry.callcount - entry.reccallcount
            calls, primitive = calls + nc, primitive + cc
            row = fn.setdefault(label, [0, 0, 0.0, 0.0])
            row[0] += nc
            row[1] += cc
            row[2] = round(row[2] + entry.inlinetime, 4)
            row[3] = round(row[3] + entry.totaltime, 4)
        out["phases"][key] = {"calls": calls, "primitive": primitive}
    out["entities"] = sum(v[0] for k, v in fn.items() if k.startswith("nagasim/core/sim_classes.py")
                          and k.endswith("(__init__)") and ":31(" in k) or None
    out["events"] = sum(v[0] for k, v in fn.items() if k.startswith("simpy/core.py") and k.endswith("(step)"))
    out["calls"] = sum(p["calls"] for p in out["phases"].values())
    out["primitive"] = sum(p["primitive"] for p in out["phases"].values())
    out["top_tottime"] = sorted(fn, key=lambda k: -fn[k][2])[:top]
    out["top_ncalls"] = sorted(fn, key=lambda k: -fn[k][0])[:top]
    return out


def cpu(name, repeats):
    doc, run_length, warmup, reps = build(name)
    _run(doc, run_length, warmup, 1, upto=min(50.0, run_length or 50.0))
    seconds = []
    for _ in range(repeats):
        start = time.process_time()
        _run(doc, run_length, warmup, reps)
        seconds.append(time.process_time() - start)
    return {"model": name, "min": min(seconds), "median": statistics.median(seconds), "max": max(seconds)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--models", nargs="*", help="default: all")
    parser.add_argument("--json", type=Path, help="write the full results here")
    parser.add_argument("--top", type=int, default=12, help="functions shown per model")
    parser.add_argument("--cpu", type=int, metavar="N", help="time N unprofiled runs instead")
    args = parser.parse_args(argv)
    results = []
    for name in args.models or names():
        if args.cpu:
            r = cpu(name, args.cpu)
            print(f"{name}: cpu min {r['min']:.3f} median {r['median']:.3f} max {r['max']:.3f}", flush=True)
        else:
            r = profile(name, args.top)
            per = (f", {r['calls'] / r['entities']:.0f} per entity" if r["entities"] else "") + (
                f", {r['calls'] / r['events']:.1f} per event" if r["events"] else "")
            phases = ", ".join(f"{k} {v['calls']:,}" for k, v in r["phases"].items())
            print(f"{name}: {r['calls']:,} calls ({r['primitive']:,} primitive; {phases}); "
                  f"{r['entities']} entities, {r['events']:,} events{per}", flush=True)
            for k in r["top_tottime"][:args.top]:
                nc, _cc, tt, ct = r["functions"][k]
                print(f"    {tt:8.3f} {ct:8.3f} {nc:>10,}  {k}")
        results.append(r)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
