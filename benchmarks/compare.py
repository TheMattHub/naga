"""Naga against raw SimPy, salabim and Ciw on the same models.

    pip install -r benchmarks/requirements.txt
    python benchmarks/compare.py            # minutes; writes results.json and results.md here
    python benchmarks/compare.py --quick    # seconds; prints the table, writes nothing
    python benchmarks/compare.py --render   # results.md again from results.json

The models are in `models.py`, one implementation per library in `with_<library>.py`. Every
implementation runs the same replications of the same run length and returns the mean time
in system of each replication.

Correct before fast: an implementation is timed only if queueing theory's value lies inside
the 99.9% confidence interval of its mean. (Twenty intervals are checked at once; at 95%
one of them would miss by chance in most runs.) A wrong one gets no timing and the script
exits 1. The table shows the usual 95% interval.

The time is CPU seconds (`time.process_time`) of the whole call, building the model and
reading the statistic included, the minimum over the repeats, in one process, one library
after the other. Customers per second divides the expected number of arrivals, the same for
every library, by that time.
"""
import argparse
import gc
import json
import os
import platform
import sys
import time
from datetime import date
from importlib import import_module
from pathlib import Path

HERE = Path(__file__).resolve().parent

LIBRARIES = {"SimPy": "simpy", "salabim": "salabim", "Ciw": "ciw", "Naga": "nagasim"}
FULL = {"run_length": 10_000, "warmup": 1_000, "replications": 10, "repeats": 3}
QUICK = {"run_length": 1_000, "warmup": 100, "replications": 5, "repeats": 1}


def implementations():
    import with_ciw
    import with_naga
    import with_salabim
    import with_simpy

    return {"SimPy": with_simpy.run, "salabim": with_salabim.run, "Ciw": with_ciw.run,
            "Naga": with_naga.run,
            "Naga, logs and sampler off": lambda *args: with_naga.run(*args, lean=True)}


def interval(means, level):
    """(mean, half-width) of the two-sided *level* confidence interval over replications."""
    from scipy import stats

    n, mean = len(means), sum(means) / len(means)
    deviation = (sum((x - mean) ** 2 for x in means) / (n - 1)) ** 0.5
    return mean, stats.t.ppf(0.5 + level / 2, n - 1) * deviation / n ** 0.5


def measure(run, stations, theory, settings):
    """One table row: the estimate, and the timing if the estimate agrees with theory."""
    args = (stations, settings["run_length"], settings["warmup"], settings["replications"])
    seconds = []
    for _ in range(settings["repeats"]):
        gc.collect()
        start = time.process_time()
        means = run(*args)
        seconds.append(time.process_time() - start)
        mean, wide = interval(means, 0.999)
        if abs(mean - theory) > wide:
            return {"mean": mean, "ci95": interval(means, 0.95)[1], "seconds": None}
    return {"mean": mean, "ci95": interval(means, 0.95)[1], "seconds": min(seconds)}


def benchmark(settings, quick):
    from models import ARRIVAL_RATE, MODELS, mean_time_in_system

    runs = implementations()
    runs["Naga"](MODELS["mm1"][1], 100, 10, 2)      # first-run set-up (plugins) is not the model's
    customers = ARRIVAL_RATE * settings["run_length"] * settings["replications"]
    results = {"date": date.today().isoformat(), "quick": quick,
               "python": platform.python_version(),
               "machine": f"{platform.processor() or platform.machine()}, {os.cpu_count()} logical "
                          f"CPUs, {platform.system()} {platform.release()}",
               "versions": {name: import_module(package).__version__ for name, package in LIBRARIES.items()},
               "settings": settings, "customers": customers, "models": {}}
    for model, (title, stations) in MODELS.items():
        theory = mean_time_in_system(stations)
        rows = {}
        for library, run in runs.items():
            rows[library] = row = measure(run, stations, theory, settings)
            if row["seconds"]:
                row["customers_per_second"] = customers / row["seconds"]
        results["models"][model] = {"title": title, "theory": theory, "rows": rows}
    return results


def render(results) -> str:
    """The Markdown the guide page includes, from a results file's content alone."""
    s, v = results["settings"], results["versions"]
    lines = [
        f"Measured {results['date']} with Python {results['python']} on {results['machine']}. "
        f"SimPy {v['SimPy']}, salabim {v['salabim']}, Ciw {v['Ciw']}, Naga {v['Naga']}. "
        f"Each model: {s['replications']} replications of {s['run_length']:,} time units, the first "
        f"{s['warmup']:,} discarded, about {results['customers']:,.0f} customers in all. CPU seconds, "
        f"the minimum of {s['repeats']} runs."
        + (" A QUICK RUN: too short to compare speeds." if results["quick"] else ""),
        "",
        "| Model | Library | Mean time in system (95% interval) | Theory | CPU seconds "
        "| Customers per second | Times SimPy's |",
        "|---|---|---|---|---|---|---|"]
    for model, entry in results["models"].items():
        base = entry["rows"]["SimPy"]["seconds"]
        for library, row in entry["rows"].items():
            timing = ("wrong: not timed", "", "") if row["seconds"] is None else (
                f"{row['seconds']:.2f}", f"{row['customers_per_second']:,.0f}",
                f"{row['seconds'] / base:.1f}" if base else "")
            lines.append(f"| `{model}`: {entry['title']} | {library} | {row['mean']:.3f} +/- "
                         f"{row['ci95']:.3f} | {entry['theory']:.3f} | " + " | ".join(timing) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--quick", action="store_true", help="short runs, one repeat, no files")
    parser.add_argument("--render", action="store_true", help="write results.md from results.json")
    parser.add_argument("--out", type=Path, help="folder for results.json and results.md")
    args = parser.parse_args(argv)
    out = args.out or (None if args.quick else HERE)
    if args.render:
        results = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
    else:
        results = benchmark(QUICK if args.quick else FULL, args.quick)
    table = render(results)
    sys.stdout.write(table)
    if out:
        (out / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8",
                                          newline="\n")
        (out / "results.md").write_text(table, encoding="utf-8", newline="\n")
    wrong = [f"{model}/{library}" for model, entry in results["models"].items()
             for library, row in entry["rows"].items() if row["seconds"] is None]
    if wrong:
        sys.stderr.write("disagrees with theory, not timed: " + ", ".join(wrong) + "\n")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.path[:0] = [str(HERE), str(HERE.parent)]    # the files beside this one; this checkout's engine
    sys.exit(main())
