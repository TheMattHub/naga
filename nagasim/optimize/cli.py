from __future__ import annotations

from pathlib import Path
from nagasim import api, experiments
from nagasim._cli import add_model_argument, setup_cli
import argparse
import pandas as pd
import sys
from .designs import full_factorial, latin_hypercube, one_at_a_time
from .doe import run_design
from .factors import Factor
from .optimization import _MIN_IMPROVEMENT, _fmt_point, optimize
from ._constants import _KIND_ALIASES

# ---------------------------------------------------------------- CLI
_FACTOR_HELP = ("a factor, as 'path=kind:low:high[:step]' or 'path=cat:a|b|c'. "
                "kind is float/int/cat. Several comma-separated paths before the '=' are "
                "driven with the same value. May repeat. Example: "
                "'resources.Agents.capacity=int:3:6'")
# argparse runs a help string through %-formatting, so a literal per cent sign
# in an example KPI key has to be doubled or `--help` raises (it did: the
# committed CLI reference had the traceback baked in until ROADMAP 3.1).
_CONSTRAINT_HELP = ("a KPI constraint, as '<kpi key><=<value>' or '<kpi key>>=<value>'. "
                    "May repeat. Example: 'resource/Agents/Usage %% (snapshot)<=85'")
_DESIGN_HELP = "full[:levels] (default full:2), lhs:<n>, or oat"


def parse_factor(spec: str) -> Factor:
    """`path[,path...]=kind:low:high[:step]` or `path[,path...]=cat:a|b|c`."""
    paths, sep, rest = spec.partition("=")
    if not sep or not paths.strip() or not rest.strip():
        raise ValueError(f"factor {spec!r}: expected 'path=kind:low:high[:step]' "
                         f"or 'path=cat:a|b|c'")
    path_list = [p.strip() for p in paths.split(",") if p.strip()]
    kind, _, tail = rest.partition(":")
    kind = _KIND_ALIASES.get(kind.strip(), kind.strip())
    if kind == "categorical":
        levels = [_literal(v) for v in tail.split("|") if v != ""]
        if not levels:
            raise ValueError(f"factor {spec!r}: a categorical factor needs 'cat:a|b|c'")
        return Factor(path_list, "categorical", levels=levels)
    parts = [p for p in tail.split(":") if p != ""]
    if len(parts) < 2:
        raise ValueError(f"factor {spec!r}: expected 'path={kind}:low:high[:step]'")
    step = float(parts[2]) if len(parts) > 2 else None
    return Factor(path_list, kind, float(parts[0]), float(parts[1]), step=step)


def parse_constraint(spec: str) -> tuple[str, str, float]:
    """`<kpi key><=<value>` or `<kpi key>>=<value>`."""
    for op in ("<=", ">="):
        kpi, sep, bound = spec.partition(op)
        if sep and kpi.strip() and bound.strip():
            return kpi.strip(), op, float(bound)
    raise ValueError(f"constraint {spec!r}: expected '<kpi key><=<value>' or '<kpi key>>=<value>'")


def _literal(text: str):
    """A number where the text is one, otherwise the text itself -- the same
    rule the override tables use for free-form cells."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def build_design(spec: str, factors, seed: int) -> pd.DataFrame:
    kind, _, arg = (spec or "full:2").partition(":")
    kind = kind.strip().lower()
    if kind in ("full", "factorial"):
        return full_factorial(factors, int(arg or 2))
    if kind in ("lhs", "latin", "lhd"):
        return latin_hypercube(factors, int(arg or 10), seed=seed)
    if kind in ("oat", "one-at-a-time"):
        return one_at_a_time(factors)
    raise ValueError(f"unknown design {spec!r}: expected {_DESIGN_HELP}")


def _write_table(frame: pd.DataFrame, out: str, sheet: str) -> None:
    path = Path(out)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_excel(path, index=False, sheet_name=sheet)
    print(f"Written to {path}")


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    add_model_argument(parser)
    parser.add_argument("--factor", action="append", dest="factors", default=None,
                        required=True, help=_FACTOR_HELP)
    parser.add_argument("--response", required=True,
                        help="the KPI key to analyse, e.g. 'system/Avg Throughput Time'")
    parser.add_argument("--replications", type=int, default=5,
                        help="replications per design point (default: 5)")
    parser.add_argument("--run-length", type=float, default=None,
                        help="default: the model's own 'simulation.time_limit'")
    parser.add_argument("--warmup", type=float, default=None,
                        help="default: the model's own 'simulation.warmup'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1,
                        help="replications to run in parallel processes (default: 1)")
    parser.add_argument("--out", help="write the result table here (.xlsx or .csv)")
    parser.add_argument("--report", help="write an HTML report here")


def main(argv=None, command: str = "doe") -> int:
    """`naga doe model.json ...` and `naga optimize model.json ...`.

    Dispatched from `nagasim.cli` without importing Qt.
    """
    setup_cli()

    parser = argparse.ArgumentParser(
        prog=f"naga {command}",
        description=("Run a design of experiments over model parameters." if command == "doe"
                     else "Search model parameters for the best KPI within a replication budget."))
    _common_arguments(parser)
    if command == "doe":
        parser.add_argument("--design", default="full:2", help=_DESIGN_HELP)
        parser.add_argument("--no-crn", action="store_true",
                            help="give each design point its own seed block instead of CRN")
        parser.add_argument("--surrogate", choices=("linear", "quadratic", "auto"), default=None,
                            help="fit a response surface over the design; "
                                 "included in --report as its own section")
        parser.add_argument("--dataset", default=None,
                            help="write one row per (design point, replication) here, as "
                                 "factors + responses + seed (.csv, or .parquet if available)")
    else:
        parser.add_argument("--goal", choices=("min", "max"), default="min")
        parser.add_argument("--budget", type=int, default=300,
                            help="total replications the search may spend (default: 300)")
        parser.add_argument("--constraint", action="append", dest="constraints", default=None,
                            help=_CONSTRAINT_HELP)
        parser.add_argument("--method", default="auto", choices=("auto", "pattern", "optuna"))
        parser.add_argument("--indifference", type=float, default=None,
                            help="the smallest difference worth detecting in the final "
                                 "ranking-and-selection step (default: 5%% of the best mean)")
        parser.add_argument("--min-improvement", type=float, default=_MIN_IMPROVEMENT,
                            help="relative gain a move must reach before the search widens that "
                                 "factor's step (default: %(default)s = 1%% of the objective). "
                                 "0 widens on any significant gain")
    args = parser.parse_args(argv)

    factors = [parse_factor(spec) for spec in args.factors]
    layout = api.load_layout(args.model)

    if command == "doe":
        design = build_design(args.design, factors, args.seed)
        result = run_design(layout, factors, design, responses=[args.response],
                            replications=args.replications, seed=args.seed,
                            crn=not args.no_crn, workers=args.workers,
                            run_length=args.run_length, warmup=args.warmup)
        table = result.table
        print(table.to_string(index=False))
        print()
        print(result.main_effects(args.response).to_string(index=False))
        surrogate_model = None
        if args.surrogate:
            from nagasim import surrogate as surrogate_mod
            surrogate_model = surrogate_mod.fit_surrogate(result, args.response, kind=args.surrogate)
            print()
            print(surrogate_model.coefficients.to_string(index=False))
            print(f"R2={surrogate_model.r2:.4f} adjR2={surrogate_model.r2_adj:.4f} "
                 f"CV RMSE={surrogate_model.cv_rmse:.4g}"
                 f"{' (lack of fit)' if surrogate_model.lack_of_fit else ''}")
        if args.dataset:
            from nagasim import surrogate as surrogate_mod
            frame = surrogate_mod.export_dataset(result, path=args.dataset)
            print(f"Dataset written to {args.dataset} ({len(frame)} rows)")
        if args.out:
            _write_table(table, args.out, "Design")
        if args.report:
            _write_report(layout, [], result, None, args, surrogate=surrogate_model)
        return 0

    constraints = [parse_constraint(spec) for spec in (args.constraints or [])]
    result = optimize(layout, factors, response=args.response, goal=args.goal,
                      constraints=constraints, budget=args.budget,
                      replications_per_point=args.replications, method=args.method,
                      seed=args.seed, indifference=args.indifference, workers=args.workers,
                      run_length=args.run_length, warmup=args.warmup,
                      min_improvement=args.min_improvement)
    print(f"Best point: {_fmt_point(result.best)}")
    print(f"{args.response} = {result.best_mean:.6g} "
          f"({'converged' if result.converged else 'budget or step limit reached'}; "
          f"{result.budget_used} replications)")
    if result.selection is not None:
        print()
        print(result.selection.table.to_string(index=False))
    if args.out:
        _write_table(result.history, args.out, "Search")
    if args.report:
        _write_report(layout, result.best_overrides, None, result, args)
    return 0


def _write_report(layout, overrides, design, optimization, args, *, surrogate=None) -> None:
    """The report's primary run is a plain run of the model the study points
    at: the baseline for a DOE, the winning point for an optimisation."""
    from nagasim.report import build_report

    results = api.run_model(experiments.apply_overrides(layout, overrides),
                            replications=args.replications, seed=args.seed,
                            run_length=args.run_length, warmup=args.warmup)
    build_report(layout, results, design=design, optimization=optimization, surrogate=surrogate,
                 title=Path(args.model).stem, path=args.report)
    print(f"Report written to {args.report}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[2:] if len(sys.argv) > 1 else None,
                          command=sys.argv[1] if len(sys.argv) > 1 else "doe"))
