"""`naga`: the engine-only command line (R3, O8). `python -m nagasim` and the
`naga` console script both call main() here.

    naga                 -> usage (the engine ships no editor)
    naga --help | help   -> list the subcommands below, no Qt import
    naga COMMAND ...     -> a headless engine mode (see COMMANDS below for the
                             full list and one-line summaries); each also
                             takes its own --help

This module must never import or name anything under `product` (the O8
engine/product boundary): `product/cli.py` extends COMMANDS below with the
product's own commands (library, mes, its MES `twin`, and its own `mcp` and `assist`) and opens the Qt
editor when given no arguments.
"""
from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import sys

# Subcommand -> (module under nagasim, its entry function, a one-line summary).
# ONE table drives dispatch below, `tools/gen_docs.py`'s CLI reference and
# `naga --help` alike (ARCH-REVIEW-PLAN B4; `product.cli.COMMANDS` extends
# this one with the product's own commands).
#
# Order here is the order `naga --help` and the generated cli.md list
# commands in; it is not meaningful otherwise.
COMMANDS = {
    "examples": ("examples", "main", "list the models that ship in the package"),
    "run": ("api", "main", "run a model headless"),
    "scenarios": ("experiments", "main", "run named scenarios headless and compare them"),
    "invest": ("investment", "main",
               "payback, NPV and ROI of each scenario against a baseline"),
    "fit": ("inputfit", "main", "fit an input distribution to CSV data"),
    "doe": ("optimize", "main", "design of experiments over model parameters"),
    "optimize": ("optimize", "main",
                "search model parameters for the best KPI within a replication budget"),
    "policy-eval": ("rl", "main", "evaluate a controller against a Policy-mode DECIDE"),
    "schema": ("modelschema", "main_schema", "JSON Schema of the model file format"),
    "validate": ("modelschema", "main_validate", "structured validation problems, exit 1 on errors"),
    "fmt": ("modelfmt", "main", "rewrite model files in their shortest canonical form"),
    "export": ("build", "main", "write a model as Python code that builds it (nagasim.build)"),
    "diff": ("modeldiff", "main", "semantic diff of two model files by record identity"),
    "merge": ("modeldiff", "merge_main", "three-way merge of model files by record identity"),
    "check": ("modelcheck", "main", "run a model's assertions and reality checks"),
    "ci-template": ("modelcheck", "main",
                    "print a GitHub Actions workflow running 'naga check'"),
    "generate": ("tablegen", "main", "build a model from Stations/Products/Routings tables"),
    "discover": ("discover", "main",
                 "draft a model from an event log (CSV or XES), with a report of what it assumed"),
    "sdimport": ("sdimport", "main", "translate an XMILE/Vensim file into a model JSON"),
    "viewer": ("viewer", "main",
               "one HTML file in which the engine re-runs a model in a browser"),
    "bind": ("databind", "main", "resolve a model's data bindings and show what each one read"),
    "watch": ("live", "main", "print what a model's live data sources send, as it arrives"),
    "state": ("state", "main",
              "take a model's state from a run, and check one before a start"),
    "plant": ("plant", "main",
              "map what a plant sends into an observed timeline: check, replay a log, watch"),
    "twin": ("twin", "main",
             "an operational twin: monitor a plant, forecast from its state, compare, report"),
    "calibrate": ("calibrate", "main", "fit model parameters to observed KPIs"),
    "forecast": ("forecast", "main", "short-horizon forecast with prediction bands"),
    "plan": ("planning", "main", "deterministic plan, optional CP-SAT proposal and risk"),
    "loops": ("loopdominance", "main", "loop dominance (Loops That Matter) of an SD model"),
    "study": ("studyrerun", "main", "check a study folder: rerun every version and compare its KPIs"),
    "skill": ("skillcli", "main", "show or copy the simulation-study guide for an assistant"),
    "mcp": ("mcp_server", "main", "Model Context Protocol server over stdio"),
    "assist": ("assistant", "main", "one headless assistant turn"),
}

# Command words that belong to the product, not the engine (O8): named here as
# plain strings (never a `product.*` import or module reference) so an
# engine-only install can still print a one-line hint instead of "unknown
# command" for them.
_PRODUCT_COMMAND_NAMES = ("library", "mes")

# An ordinary input-shape failure (bad path, malformed model file, wrong
# --policy/--method name, a missing optional backend), turned into a one-line
# message instead of a raw traceback below. schema.ModelValidationError is a
# ValueError subclass, so it needs no entry of its own.
_INPUT_ERRORS = (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, ImportError)


def _dispatch(entry, argv: list, prog: str, name: str) -> int:
    """Runs one subcommand's entry point, turning `_INPUT_ERRORS` into
    `prog name: message` on stderr (exit 2) instead of a raw traceback.
    NAGASIM_CLI_DEBUG=1 keeps the traceback for debugging."""
    try:
        return entry(argv)
    except _INPUT_ERRORS as exc:
        if os.environ.get("NAGASIM_CLI_DEBUG"):
            raise
        sys.stderr.write(f"{prog} {name}: {exc}\n")
        return 2


def _print_help(prog: str = "naga") -> None:
    print(f"usage: {prog} [COMMAND] [ARGS ...]")
    print()
    print("Each command also takes its own --help.")
    print()
    print("commands:")
    width = max(len(name) for name in COMMANDS)
    for name, (_module, _attr, summary) in COMMANDS.items():
        print(f"  {name:<{width}}  {summary}")


def main(argv=None, prog: str = "naga") -> int:
    # First thing, before anything parses argv: on Windows (and in the frozen
    # build) a ProcessPoolExecutor child re-runs this executable, and without
    # this call it would re-enter main() per worker (ROADMAP 1.6 parallel
    # replications).
    multiprocessing.freeze_support()

    # Plugin blocks/distributions/exporters (ROADMAP 2.4 B): registered once
    # here, before any subcommand runs, so they show up in --export alike.
    from nagasim.plugins import load_plugins
    load_plugins()

    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("-h", "--help", "help"):
        _print_help(prog)
        return 0

    name = args[0] if args else None

    if name in ("doe", "optimize"):
        from nagasim.optimize import main as optimize_main
        return _dispatch(lambda a: optimize_main(a, command=name), args[1:], prog, name)

    if name in ("check", "ci-template"):
        # Both subcommands share nagasim.modelcheck's own argparse subparsers,
        # which need the subcommand word kept, unlike every other command
        # here, which drops it (args[1:]).
        from nagasim.modelcheck import main as modelcheck_main
        return _dispatch(modelcheck_main, args, prog, name)

    if name in COMMANDS:
        module, attr, _summary = COMMANDS[name]
        entry = getattr(importlib.import_module(f"nagasim.{module}"), attr)
        return _dispatch(entry, args[1:], prog, name)

    if name in _PRODUCT_COMMAND_NAMES:
        sys.stderr.write(f"{prog}: '{name}' is part of the Naga product; "
                         "install the 'product' package for it.\n")
        return 2

    _print_help(prog)
    return 0 if name is None else 2


if __name__ == "__main__":
    raise SystemExit(main())
