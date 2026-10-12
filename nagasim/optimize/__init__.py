"""Design of experiments, ranking and selection, black-box search (ROADMAP 2.1).

Top-level module beside `api.py` / `experiments.py`: it needs pandas and scipy
and sits above the engine, and it imports no Qt. Dependencies point downward
only -- optimize -> experiments -> api -> core.

    from nagasim import optimize
    factors = [optimize.Factor("resources.Server.capacity", "integer", 1, 4)]
    design = optimize.full_factorial(factors, levels=2)
    result = optimize.run_design(layout, factors, design, replications=5)
    result.main_effects("system/Avg Throughput Time")

Three entry points, all of them budget-aware and all evaluating through the one
cached evaluator below:

* `run_design`  -- run a design (factorial / Latin hypercube / one-at-a-time)
                   and report main effects and two-factor interactions;
* `select_best` -- the fully sequential KN indifference-zone procedure over a
                   finite set of candidates, with a stated guarantee;
* `optimize`    -- pattern search on the noisy mean over continuous/integer
                   factors, finishing with `select_best` over the top points.

A factor addresses the model through the scenario override grammar
(`experiments.list_override_paths`), and may drive SEVERAL paths with the same
value at once -- a capacity change that must also move a schedule, say.
"""
from __future__ import annotations

__all__ = ["Factor", "parse_factor", "parse_constraint", "full_factorial", "latin_hypercube",
           "one_at_a_time", "overrides_for", "DesignResult", "run_design", "SelectionResult",
           "select_best", "OptimizationResult", "optimize", "sensitivity", "tornado", "sobol",
           "build_design", "main"]

from ._constants import KINDS, _DEFAULT_RESPONSE_PREFIX, _KIND_ALIASES

# Public names plus the few privates `calibrate.py` and the tests reach for
# (`_Evaluator`, `_point_key`, `_Record`, `_pattern_search`, `_fmt_point`,
# `_FACTOR_HELP`, `_write_table`); `evaluation._evaluate_point` is patched on
# its own module.
from .factors import Factor, _point_key, overrides_for
from .designs import full_factorial, latin_hypercube, one_at_a_time
from .evaluation import _Evaluator
from .doe import DesignResult, run_design
from .selection import SelectionResult, select_best
from .optimization import OptimizationResult, _Record, _fmt_point, _pattern_search, optimize
from .sensitivity import sensitivity
from .tornado import tornado
from .sobol import sobol
from .cli import _FACTOR_HELP, _write_table, build_design, main, parse_constraint, parse_factor
