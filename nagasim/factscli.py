"""`naga facts MODEL [--seed N] [--run-length T] [--json]` (D679): run one
replication with a trace recorder and print its run facts (`core/runfacts.py`).

Exit codes: 0 the run happened, whatever its facts say; 2 a usage error (a bad
option, no file at the path); 3 the model could not run (it does not load, it is
invalid, it never ends without a run length, or the run failed).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from nagasim._cli import add_model_argument, setup_cli
from nagasim.core.runfacts import facts
from nagasim.core.trace import TraceRecorder

__all__ = ["run", "main"]


def run(model, *, seed: int = 0, run_length: float | None = None, should_stop=None) -> dict | None:
    """One replication of *model* (a path or a layout dict), recorded and read.
    *run_length* None takes the model's own. Returns {"seed", "end_time", "truncated",
    "facts": [{"kind", "text", "block", "resource", "t", "count", "duration"}]};
    raises whatever a model that cannot run raises. None when *should_stop* cut the run short
    (facts of a cut run say nothing about the model)."""
    from nagasim.api import run_settings
    from nagasim.core.schema import ModelValidationError, fatal_problems, format_problems, prepare
    from nagasim.core.sim_runner import setup_run, step_until

    layout, problems = prepare(model)
    fatal = fatal_problems(problems)
    if fatal:
        raise ModelValidationError(format_problems(fatal))
    # The window `naga run` would use for one replication, so the facts are its run's. These
    # lines repeat SimSession.__init__'s preamble, which takes no run_length: keep them in step.
    _replications, warmup, length = run_settings(layout, 1, None, run_length)
    recorder = TraceRecorder()
    env, _model = setup_run(layout, seed=seed, run_len=length, warmup=warmup,
                            subscribers=(recorder,), prepared=True)
    if step_until(env, length, should_stop):
        return None
    end = env.ctx.sim_end_time
    found = facts(recorder.events, layout, end_time=end, truncated=recorder.truncated)
    return {"seed": seed, "end_time": end, "truncated": recorder.truncated,
            "facts": [dataclasses.asdict(fact) for fact in found]}


def main(argv=None) -> int:
    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga facts",
        description="Run one replication and list what happened in it that nothing else "
                    "reports: blocks never entered, resources never seized, resources idle "
                    "while entities waited for them, entities left at the end. Facts, not "
                    "verdicts: exit 0 whenever the run happened.")
    add_model_argument(parser)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--run-length", type=float, default=None,
                        help="default: the model's own 'simulation.time_limit'; 0 = run until "
                             "the queue drains")
    parser.add_argument("--json", action="store_true", help="print the facts as JSON")
    args = parser.parse_args(argv)
    if not Path(args.model).is_file():
        sys.stderr.write(f"naga facts: no model file at {args.model}\n")
        return 2
    try:
        answer = run(args.model, seed=args.seed, run_length=args.run_length)
    except Exception as exc:        # any failure of the model is "could not run" (exit 3)
        if os.environ.get("NAGASIM_CLI_DEBUG"):
            raise
        sys.stderr.write(f"naga facts: {args.model}: the model could not run: {exc}\n")
        return 3
    if args.json:
        print(json.dumps({"model": args.model, **answer}, indent=2))
        return 0
    found = answer["facts"]
    print(f"{args.model}: seed {args.seed}, run to t={answer['end_time']:g}, "
          f"{len(found)} fact{'s' * (len(found) != 1)}")
    for fact in found:
        print(f"  [{fact['kind']}] {fact['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
