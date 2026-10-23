"""Input uncertainty: how much of a KPI's spread comes from the data its inputs were fitted to.

    answer = uncertainty.input_uncertainty(
        layout, [{"path": "blocks.process_1.params.distribution", "values": service_times}])

A result run on fitted inputs carries two kinds of error: the run-to-run noise a confidence
interval shows, and the error of the fit itself, which more replications never shrink. This
module measures the second with a bootstrap:

* a distribution input (`<...>.distribution`) is fitted to all of its data; each resample of
  the data (with replacement, the same size) is refitted in that same family (D719);
* an arrival-rate table (`schedules.<name>.points`) is rebuilt from whole cycles drawn with
  replacement, because arrivals within one cycle are not independent (D720).

The model then runs once for the point fit and once per resample, every one on the same
replication seeds (common random numbers, `experiments.run_scenarios`), so resamples differ
only in their inputs. The resampling has its own random stream (`inputfit.bootstrap_rng`),
never one of the engine's.

Per KPI, with R replications and B usable resamples (D719):

* `run_ci`: the Student-t interval over the point fit's R replications;
* the inputs' variance: the variance of the resample means on common random numbers. The
  resamples share their replication seeds, so their means share the run-to-run noise and
  their spread is the inputs'; nothing is subtracted for run noise (the bootstrap analysis
  of variance of Ankenman and Nelson, 2012, subtracts it, but that is for independent
  noise per resample and cut a real input error to 0 here, #938);
* `input_interval`: `run_ci` widened by `t(B-1) x sqrt(inputs' variance)`, the two
  half-widths added in quadrature, so it always holds `run_ci`, and a KPI the inputs do
  not move keeps a zero width. It is symmetric, so a skewed effect (a queue near
  saturation) can push its lower bound below 0; that bound is cut at 0 when no run gave
  a negative value;
* `input_share`: the inputs' variance over the variance of the KPI's mean (inputs' plus
  run-to-run sem squared). More replications shrink the rest; only more data shrinks it.

With `overrides=` (a change, as `what_if` takes it) the answer is about the change's
difference instead (D737): each resample, and the point fit, runs the baseline and the
changed model on the same seeds, and the per-replication differences take the place of the
levels above. A level's spread is not a difference's: two models on the same input draws
share most of it, so a change can be clear while the level it moves is uncertain.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
from scipy import stats as sstats

from nagasim import experiments, inputfit
from nagasim.core.stats import ci95

__all__ = ["MAX_RUNS", "NOT_MEASURED", "input_uncertainty", "interval", "render", "same_change",
           "trust_texts", "what_if_note", "main"]

# Every run is a full model run: (resamples + 1) x replications of them. A study bigger
# than this is refused before the first run, with the count, rather than left to grind.
MAX_RUNS = 2000
ROUGH_BELOW = 20        # fewer usable resamples than this: the note says the interval is rough
SMALL_SAMPLE = 30       # fewer observations than this: the note says the bootstrap is rough


def _kind(spec: dict) -> str:
    path = str(spec.get("path", ""))
    if path.endswith(".distribution"):
        return "distribution"
    if path.endswith(".points"):
        if not spec.get("bin_width") or not spec.get("cycle"):
            raise ValueError(f"{path}: an arrival table needs a bin_width and a cycle "
                             "(whole cycles are what is resampled)")
        return "arrival rates"
    raise ValueError(f"{path!r}: an input is a '...distribution' field or a schedule's "
                     "'schedules.<name>.points'")


def _draw(spec: dict, resamples: int, rng) -> tuple[dict, list[dict], list]:
    """(what the input reports, its point overrides, one override list or None per resample)."""
    path, values = spec["path"], np.asarray(spec.get("values") or [], dtype=float)
    kind = _kind(spec)
    if values.size < 2:
        raise ValueError(f"{path}: {values.size} usable value(s); a bootstrap needs data")
    if kind == "distribution":
        best = inputfit.best_fit(values)
        if best is None:
            raise ValueError(f"{path}: no distribution could be fit to this data")
        base = path.rpartition(".")[0]
        point_params = {k: v.item() if hasattr(v, "item") else v
                        for k, v in best.dist_params.items()}
        info = {"path": path, "kind": kind, "n": int(values.size),
                "point": {"distribution": best.distribution, "dist_params": point_params}}

        def overrides(params):
            return [{"path": path, "value": best.distribution},
                    {"path": f"{base}.dist_params", "value": params}]
        draws = [None if p is None else overrides(p)
                 for p in inputfit.bootstrap_fits(values, best.distribution, resamples, rng)]
        return info, overrides(point_params), draws
    bin_width, cycle = float(spec["bin_width"]), float(spec["cycle"])
    point = inputfit.arrival_rates(values, bin_width=bin_width, cycle=cycle)
    info = {"path": path, "kind": kind, "n": int(values.size), "cycles": point.cycles,
            "point": point.points}
    tables = inputfit.bootstrap_arrival_rates(values, bin_width=bin_width, cycle=cycle,
                                              resamples=resamples, rng=rng)
    return info, [{"path": path, "value": point.points}], [[{"path": path, "value": t}]
                                                           for t in tables]


def interval(point, resamples, confidence: float = 0.95, *, floor: bool = True) -> dict:
    """The per-KPI answer from the point fit's per-replication values and one array of
    per-replication values per resample (the module docstring has the formulas).
    Undefined parts are None: fewer than 2 replications or 2 usable resamples. `floor=False`
    (a difference) never cuts the lower bound at 0: whether it crosses 0 is the question."""
    run = ci95(point, confidence)
    rows = [r[np.isfinite(r)] for r in (np.asarray(v, dtype=float) for v in resamples)]
    rows = [r for r in rows if r.size >= 2]
    out = {"mean": run["mean"], "run_ci": None, "input_interval": None, "input_share": None,
           "resamples_used": len(rows)}
    if not np.isfinite(run["sem"]):
        return out
    out["run_ci"] = [run["lb"], run["ub"]]
    if len(rows) < 2:
        return out
    inputs = float(np.var([r.mean() for r in rows], ddof=1))     # on CRN: no subtraction (#938)
    half = math.hypot(run["ub"] - run["mean"],
                      float(sstats.t.ppf(0.5 + confidence / 2.0, len(rows) - 1)) * math.sqrt(inputs))
    total = inputs + run["sem"] ** 2
    low = run["mean"] - half
    if floor and low < 0.0 <= min(run["lb"], *(float(r.min()) for r in rows)) \
            and np.min(point) >= 0.0:
        low = 0.0       # a KPI no run took below 0 (a time, a count) is not given a negative bound
    out["input_interval"] = [low, run["mean"] + half]
    out["input_share"] = inputs / total if total > 0 else 0.0
    return out


def input_uncertainty(layout, inputs: list[dict], *, kpis: list[str] | None = None,
                      resamples: int = 20, replications: int = 5, seed: int = 0,
                      run_length: float | None = None, warmup: float | None = None,
                      workers: int | str = 1, should_stop=None,
                      overrides: list[dict] | None = None) -> dict:
    """Each KPI's run-to-run interval, its interval with the inputs' uncertainty, and the
    inputs' share of the spread.

    Parameters
    ----------
    layout : dict or str
        The model.
    inputs : list of dict
        ``{"path", "values"}`` per input: a ``...distribution`` path takes a sample, a
        ``schedules.<name>.points`` path takes arrival timestamps plus ``bin_width`` and
        ``cycle`` in base time units.
    kpis : list of str, optional
        `experiments.flatten_kpis` keys; default every ``system/`` KPI.
    resamples, replications : int
        Bootstrap resamples (at least 2) and replications per model (at least 2); the
        model runs ``(resamples + 1) x replications`` times, at most `MAX_RUNS`.
    seed : int
        Replication r of every run uses ``seed + r``; the resampling draws from
        ``inputfit.bootstrap_rng(seed)``, a stream of its own.
    overrides : list of dict, optional
        A change (``{"path", "value"}``, as `what_if` takes it): every KPI row is then the
        changed model minus the baseline, both run per resample on the same seeds, and the
        run count doubles. Applied after the resample's inputs, so an override of an input
        path wins.

    Returns
    -------
    dict
        ``{"completed", "measure": "level" | "difference", "overrides" (None for a
        level), "resamples", "resamples_used", "failed": {path: count},
        "replications", "runs", "seed", "inputs", "kpis": [{"kpi", "mean", "run_ci",
        "input_interval", "input_share", "resamples_used"}], "notes"}``. Raises
        ValueError for a request it cannot answer (ModelValidationError for a path the
        model does not have), before any run; and when fewer than half the resamples
        (at least 2) could be refitted.
    """
    from nagasim.core.schema import load_layout

    resamples, replications = int(resamples), int(replications)
    models = 2 if overrides else 1          # a difference runs the baseline and the change
    if not inputs:
        raise ValueError("no inputs: name at least one input and its data")
    if replications < 2:
        raise ValueError("at least 2 replications: the run-to-run spread needs them")
    if resamples < 2:
        raise ValueError("at least 2 resamples")
    if (resamples + 1) * replications * models > MAX_RUNS:
        raise ValueError(f"{resamples} resamples x {replications} replications"
                         + (" x 2 models" if models == 2 else "") + " is "
                         f"{(resamples + 1) * replications * models} runs; at most {MAX_RUNS}")
    for spec in inputs:
        _kind(spec)                         # every input's shape, before any fitting
    base = load_layout(layout)
    if overrides:
        experiments.apply_overrides(base, overrides)    # a path the model lacks: refused now
    rng = inputfit.bootstrap_rng(seed)
    infos, point_overrides, per_input = [], [], []
    for spec in inputs:
        info, point, draws = _draw(spec, resamples, rng)
        infos.append(info)
        point_overrides += point
        per_input.append(draws)
    failed = {info["path"]: sum(d is None for d in draws) for info, draws in zip(infos, per_input)}
    usable = [b for b in range(resamples) if all(draws[b] is not None for draws in per_input)]
    floor = max(2, math.ceil(resamples / 2))
    if len(usable) < floor:
        raise ValueError(f"only {len(usable)} of {resamples} resamples could be refit (at least "
                         f"{floor} needed); failed per input: {failed}")

    model = experiments.apply_overrides(base, point_overrides)
    change = list(overrides or [])
    model["scenarios"] = [{"name": f"resample {b + 1}",
                           "overrides": [o for draws in per_input for o in draws[b]]}
                          for b in usable]
    if change:      # each resample's changed twin, the change last so it wins
        model["scenarios"] += [{"name": f"{sc['name']} changed", "overrides": sc["overrides"] + change}
                               for sc in model["scenarios"]]
        model["scenarios"].insert(0, {"name": "changed", "overrides": change})
    result = experiments.run_scenarios(model, replications=replications, seed=seed,
                                       run_length=run_length, warmup=warmup, crn=True,
                                       workers=workers, should_stop=should_stop)
    runs = (len(usable) + 1) * replications * models
    answer = {"completed": result.completed, "measure": "difference" if change else "level",
              "overrides": change or None, "resamples": resamples,
              "resamples_used": len(usable), "failed": failed, "replications": replications,
              "runs": runs, "seed": int(seed), "inputs": infos, "kpis": [], "notes": []}
    if not result.completed:
        answer["notes"].append("stopped before every run finished; nothing is reported")
        return answer

    flats = {name: experiments.flatten_kpis(res) for name, res in result.results.items()}
    baseline = flats.pop(experiments.BASELINE)
    names = kpis or sorted(k for k in set(baseline["KPI"]) if k.startswith("system/"))

    def series(flat, kpi):
        return flat[flat["KPI"] == kpi].sort_values("Run")["Value"].to_numpy(dtype=float)

    def values(name, kpi):      # a level, or the change's per-replication difference
        flat = baseline if name == experiments.BASELINE else flats[name]
        if not change:
            return series(flat, kpi)
        twin = flats["changed" if name == experiments.BASELINE else f"{name} changed"]
        return series(twin, kpi) - series(flat, kpi)

    for kpi in names:
        answer["kpis"].append({"kpi": kpi, **interval(
            values(experiments.BASELINE, kpi), [values(f"resample {b + 1}", kpi) for b in usable],
            floor=not change)})
    answer["notes"] = _notes(answer)
    return answer


def _notes(answer: dict) -> list[str]:
    used, reps = answer["resamples_used"], answer["replications"]
    notes = [] if answer["measure"] == "level" else [
        "Each KPI row is the change's difference (changed model minus baseline), both run per "
        "resample on the same seeds; a level's spread is not a difference's."]
    notes += [f"The input interval rests on {used} bootstrap resamples of the data, {reps} "
             f"replications each on common random numbers ({answer['runs']} runs)."
             + (f" Fewer than {ROUGH_BELOW} resamples: read it as a rough guide."
                if used < ROUGH_BELOW else "")]
    cycles = [f"{i['path']} resamples whole cycles ({i['cycles']} of them)"
              for i in answer["inputs"] if i["kind"] == "arrival rates"]
    notes.append("The bootstrap takes each input's observations as independent draws"
                 + (f"; {'; '.join(cycles)}." if cycles else "."))
    notes.append(f"The inputs' share is their part of the variance of the KPI's mean over "
                 f"{reps} replications: more replications shrink the rest, only more data "
                 f"shrinks this part.")
    for path, count in answer["failed"].items():
        if count:
            notes.append(f"{path}: {count} of {answer['resamples']} resamples could not be "
                         f"refit and were left out.")
    for info in answer["inputs"]:
        if info["n"] < SMALL_SAMPLE:
            notes.append(f"{info['path']}: {info['n']} observations; with fewer than "
                         f"{SMALL_SAMPLE} the bootstrap itself is rough.")
    return notes


# ---------------------------------------------------------------- a note on a what_if (C3, D737)
def same_change(a, b) -> bool:
    """Two override lists name the same change (order of keys aside)."""
    return json.dumps(a or [], sort_keys=True, default=str) == json.dumps(b or [], sort_keys=True,
                                                                         default=str)


def what_if_note(asked, overrides, kpis) -> dict | None:
    """The input-data note for a `what_if` answer, from recorded `input_uncertainty` answers
    (`agent_tools` keeps them in `Workspace.asked`): the latest difference measured for these
    same overrides, else the latest level, labelled as one; None when neither covers the
    verdict's KPIs. It only quotes: no run, and the verdict is never changed or merged with it
    (D737)."""
    recorded = [a for a in asked or [] if isinstance(a, dict)
                and a.get("tool") == "input_uncertainty" and a.get("kpis")]
    pick = next((a for a in reversed(recorded) if a.get("measure") == "difference"
                 and same_change(a.get("overrides"), overrides)), None) \
        or next((a for a in reversed(recorded) if a.get("measure", "level") == "level"), None)
    if pick is None:
        return None
    wanted = [k for k in kpis if isinstance(k, str)]
    rows = [r for k in wanted for r in pick["kpis"] if r.get("kpi") == k and r.get("input_interval")]
    if not rows:
        return None
    measure, used, reps = pick.get("measure", "level"), pick.get("resamples_used"), pick.get("replications")
    spans = "; ".join(f"{r['kpi']} {_pair(r['input_interval'])}" for r in rows)
    if measure == "difference":
        text = (f"Input data: the change's difference over {used} resamples of the data "
                f"({reps} replications each) is {spans}; the verdict above is on run noise.")
    else:
        text = (f"Input data, measured on the level and not on this change ({used} resamples, "
                f"{reps} replications each): {spans}; a level's spread is not the difference's, "
                f"which input_uncertainty with these overrides measures.")
    return {"measure": measure, "resamples": used, "replications": reps,
            "kpis": [{k: r.get(k) for k in ("kpi", "mean", "input_interval", "input_share")}
                     for r in rows], "text": text}


# ---------------------------------------------------------------- the trust card's rows (D739)
NOT_MEASURED = "not measured"


def trust_texts(asked) -> dict:
    """{"input", "params"}: one line each for the trust card from recorded answers (the latest
    `input_uncertainty` and the latest ABC `calibrate`), or NOT_MEASURED. Each carries its own
    basis; the three uncertainties (run noise, input data, parameters) are never summed (D739)."""
    rows = [a for a in asked or [] if isinstance(a, dict)]
    inp = next((a for a in reversed(rows) if a.get("tool") == "input_uncertainty" and a.get("kpis")), None)
    abc = next((a for a in reversed(rows) if a.get("tool") == "calibrate" and a.get("method") == "abc"
                and a.get("accepted") is not None), None)
    out = {"input": NOT_MEASURED, "params": NOT_MEASURED}
    if inp is not None:
        first = inp["kpis"][0]
        share = first.get("input_share")
        out["input"] = (f"{inp.get('measure', 'level')}, {inp.get('resamples_used')} resamples x "
                        f"{inp.get('replications')} replications; {first.get('kpi')} "
                        f"{_pair(first.get('input_interval'))}"
                        + (f", inputs {share * 100:.0f}% of the variance" if isinstance(share, (int, float)) else ""))
    if abc is not None:
        out["params"] = (f"{abc['accepted']} of {abc.get('samples')} samples accepted, "
                         f"tolerance {_g(abc.get('abc_tolerance'))}")
    return out


# ---------------------------------------------------------------- text and CLI
def _g(value) -> str:
    return "-" if value is None else f"{value:.4g}"


def _pair(pair) -> str:
    return "-" if pair is None else f"[{_g(pair[0])}, {_g(pair[1])}]"


def render(answer: dict) -> str:
    """The answer as plain text, one KPI a line, then the notes."""
    lines = [f"{answer['resamples_used']} resamples of the data, {answer['replications']} "
             f"replications each"
             + ("; each row is the change's difference" if answer.get("measure") == "difference" else "")]
    for row in answer["kpis"]:
        share = "-" if row["input_share"] is None else f"{row['input_share']:.0%}"
        lines.append(f"{row['kpi']}: {_g(row['mean'])}; runs {_pair(row['run_ci'])}; "
                     f"with inputs {_pair(row['input_interval'])}; inputs' share {share}")
    return "\n".join(lines + answer["notes"])


def _data_arg(text: str) -> tuple[str, str, str | None]:
    """PATH=FILE[:COLUMN]; a colon followed by a path separator is a drive letter, not a column."""
    path, sep, rest = text.partition("=")
    if not sep or not path or not rest:
        raise argparse.ArgumentTypeError(f"--data takes PATH=FILE[:COLUMN], not {text!r}")
    file, colon, column = rest.rpartition(":")
    if colon and column and len(file) > 1 and not set(column) & set("/\\"):
        return path, file, column
    return path, rest, None


def main(argv=None) -> int:
    """`naga uncertainty MODEL --data PATH=FILE[:COLUMN] ...`."""
    from nagasim._cli import add_model_argument, setup_cli

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga uncertainty",
        description="How much of each KPI's spread comes from the data its inputs were "
                    "fitted to: each input's data is resampled and refitted, the model run "
                    "per resample on the same seeds, and each KPI gets its run-to-run 95% "
                    "interval, its interval with the inputs' uncertainty, and the inputs' "
                    "share.")
    add_model_argument(parser)
    parser.add_argument("--data", action="append", type=_data_arg, required=True,
                        metavar="PATH=FILE[:COLUMN]",
                        help="an input and its data: a '...distribution' field and a sample, "
                             "or 'schedules.<name>.points' and arrival timestamps; may repeat")
    parser.add_argument("--kpi", action="append", default=None,
                        help="a KPI key (e.g. 'system/Avg Throughput Time'); may repeat. "
                             "Default: every system KPI")
    parser.add_argument("--resamples", type=int, default=20, help="bootstrap resamples (default 20)")
    parser.add_argument("--replications", type=int, default=5,
                        help="replications per resample, the same seeds for each (default 5)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--run-length", type=float, default=None)
    parser.add_argument("--warmup", type=float, default=None)
    parser.add_argument("--bin-width", type=float, default=None,
                        help="an arrival table's bin width, in base time units")
    parser.add_argument("--cycle", type=float, default=None,
                        help="an arrival table's cycle (e.g. one day), in base time units")
    args = parser.parse_args(argv)
    try:
        inputs = [{"path": path, "values": inputfit.load_column(file, column).values.tolist(),
                   "bin_width": args.bin_width, "cycle": args.cycle}
                  for path, file, column in args.data]
        answer = input_uncertainty(args.model, inputs, kpis=args.kpi, resamples=args.resamples,
                                   replications=args.replications, seed=args.seed,
                                   run_length=args.run_length, warmup=args.warmup)
    except (ValueError, OSError) as exc:        # ModelValidationError included
        print(f"naga uncertainty: {exc}")
        return 2
    print(render(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
