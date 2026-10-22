from __future__ import annotations

from nagasim.core.schema import ModelValidationError
from nagasim.core.stats import ci95, paired_diff_ci
from dataclasses import dataclass
from nagasim.core.schema import load_layout
import logging
import math
import numpy as np
import pandas as pd
from .designs import latin_hypercube
from .evaluation import _Evaluator, _response
from .factors import _point_key, overrides_for
from .selection import SelectionResult, select_best

# ---------------------------------------------------------------- optimisation
@dataclass
class _Record:
    """One evaluated point of the factor space."""

    key: tuple
    point: dict
    objective: np.ndarray            # sign-adjusted: higher is always better
    feasibility: str = "feasible"

    @property
    def mean(self) -> float:
        return float(np.nanmean(self.objective)) if self.objective.size else np.nan


@dataclass
class OptimizationResult:
    """What `optimize` returns. `history` records every evaluated point (factor
    values, mean, CI, feasibility, replications, budget spent) in evaluation
    order, so the search is auditable and can be resumed from its best point."""

    best: dict
    best_overrides: list
    history: pd.DataFrame
    converged: bool
    budget_used: int
    response: str
    goal: str
    feasible: bool = True
    selection: SelectionResult | None = None
    sampler: str = ""                # the optuna sampler actually used, "" otherwise
    pareto: pd.DataFrame | None = None   # method="optuna-nsga2": the non-dominated set

    @property
    def confirmed(self) -> dict | None:
        """The KN confirmation of a single-objective search, None when there was
        none (one candidate, or no budget left)."""
        sel = self.selection
        if sel is None:
            return None
        return {"best": dict(self.best), "confidence": sel.confidence,
                "guaranteed": sel.guaranteed, "candidates": list(sel.table["Candidate"])}

    @property
    def best_mean(self) -> float:
        """The winner's response mean on every replication it received -- the
        selection step's own estimate where there was one, since it is the
        estimate with the most replications behind it."""
        label = _fmt_point(self.best)
        if self.selection is not None:
            rows = self.selection.table[self.selection.table["Candidate"] == label]
            if not rows.empty:
                return float(rows["Mean"].iloc[0])
        if self.history.empty:
            return float("nan")
        rows = self.history[self.history["Point"] == label]
        return float(rows["Mean"].iloc[-1]) if not rows.empty else float("nan")

    def trajectory(self) -> pd.DataFrame:
        """Best-so-far objective against replications spent."""
        return self.history[["Budget used", "Best so far"]].copy()


def _fmt_point(point: dict) -> str:
    return ", ".join(f"{name}={value}" for name, value in point.items())


def _feasibility(constraints, values: dict, confidence: float) -> str:
    """"feasible" / "infeasible" / "unknown" for one point, judged on each
    constraint KPI's confidence interval: a point is infeasible only when an
    interval lies entirely on the wrong side, feasible when every interval lies
    entirely on the right side, and "unknown" in between (more replications may
    settle it)."""
    verdict = "feasible"
    for kpi, op, bound in constraints:
        arr = values.get(kpi)
        if arr is None:
            raise ValueError(f"unknown constraint KPI: {kpi!r}")
        stat = ci95(arr)
        lb, ub = stat["lb"], stat["ub"]
        if not np.isfinite(lb) or not np.isfinite(ub):
            lb = ub = stat["mean"]
        if op == "<=":
            if lb > bound:
                return "infeasible"
            if ub > bound:
                verdict = "unknown"
        else:
            if ub < bound:
                return "infeasible"
            if lb < bound:
                verdict = "unknown"
    return verdict


_FEASIBILITY_RANK = {"feasible": 2, "unknown": 1, "infeasible": 0}

# How much better a move has to be, RELATIVE to the objective's own size, before
# the pattern search widens that factor's step (PHASE4 T7, ARCHITECTURE D80).
# 1% of the incumbent's mean: a real but negligible gain still moves the point,
# it just stops buying the next poll a step twice as wide. 0 restores the old
# "any significant gain doubles the step" behaviour.
_MIN_IMPROVEMENT = 0.01


def _better(a, b) -> bool:
    """Whether record *a* beats *b* on feasibility first, then mean."""
    if b is None:
        return True
    return ((_FEASIBILITY_RANK[a.feasibility], a.mean)
            > (_FEASIBILITY_RANK[b.feasibility], b.mean))


def optimize(layout, factors, *, response: str, goal: str = "min", constraints=(),
             budget: int = 300, replications_per_point: int = 5, method: str = "auto",
             seed: int = 0, confidence: float = 0.95, indifference: float | None = None,
             workers: int = 1, run_length=None, warmup=None, max_replications_per_point=None,
             select_top: int = 3, on_progress=None, should_stop=None,
             evaluator=None, warm_start_points: list[dict] | None = None,
             pool=None, min_improvement: float = _MIN_IMPROVEMENT,
             record_logs=None, objectives=None) -> OptimizationResult:
    """Search the factor space for the best response within a replication budget.

    The winner is confirmed with ranking and selection (`select_best`).

    The built-in method (``method="auto"`` or ``"pattern"``, no extra
    dependency) works in these steps:

    1. Seed the search with a Latin hypercube of about ``10 * n_factors`` points,
       bounded by the budget, each run for *replications_per_point* replications
       on the same seeds as every other point (common random numbers, so
       differences between points have low variance).
    2. Take the best of them as the incumbent: feasibility first, then the mean.
    3. Try the ``2n`` coordinate neighbours of the incumbent at each factor's own
       current step (a fraction of its range, rounded for integer factors and
       clipped to the bounds).
    4. A neighbour replaces the incumbent only if it wins: it is feasible where
       the incumbent is not, or a paired-t interval on the per-replication
       differences, at *confidence*, excludes 0 in its favour.
    5. An ambiguous challenger (better mean, interval straddling 0) gets more
       replications, and so does the incumbent on the same seeds, up to
       *max_replications_per_point*, then is retested once.
    6. The winning factor's step doubles only when the gain is at least
       *min_improvement* of the objective's own size; an axis whose neighbours
       beat nothing is halved, and a full sweep with no move halves every axis.
       The search converges when every factor's step is below its own grid unit.
    7. Finally `select_best` (KN) runs over the top *select_top* distinct points
       on whatever budget is left, for a statistically defensible winner.

    Every point is cached on its rounded coordinates, so an integer factor never
    pays for the same point twice, and ``history`` records each evaluation. The
    budget is a hard cap: an evaluation that would exceed it is not run.

    Parameters
    ----------
    layout : dict or str
        The model, or a path to it.
    factors : list of Factor
        What to vary: an override path, a kind and bounds.
    response : str
        The KPI key to optimise.
    goal : {"min", "max"}, default "min"
        Direction of the response.
    constraints : sequence, default ()
        Feasibility conditions on other KPIs.
    budget : int, default 300
        Hard cap on replications.
    replications_per_point : int, default 5
        Replications of each evaluated point.
    method : {"auto", "pattern", "optuna", "optuna-gp", "optuna-nsga2"}, default "auto"
        ``"optuna"`` runs optuna's TPE sampler instead (optional dependency,
        imported lazily) and finishes with the same selection step.
        ``"optuna-gp"`` uses optuna's Gaussian-process sampler, better on small
        budgets and smooth responses; it needs torch, and without it the search
        falls back to multivariate TPE (``result.sampler`` says which ran).
        ``"optuna-nsga2"`` is multi-objective over *objectives* and returns the
        non-dominated set in ``result.pareto`` instead of one confirmed winner.
    objectives : sequence of (kpi, goal), optional
        The objectives of ``"optuna-nsga2"`` (at least two) instead of
        *response*/*goal*; ``best`` is then the front's winner on the first.
    seed : int, default 0
        Seed of replication 0.
    confidence : float, default 0.95
        Level of the intervals used to accept a move.
    indifference : float, optional
        The difference below which two points count as equal in the selection.
    workers : int, default 1
        Process-pool size.
    run_length, warmup : optional
        Fall back to the model's own settings.
    max_replications_per_point : int, optional
        Cap for the extra replications of an ambiguous comparison.
    select_top : int, default 3
        Points the final selection compares.
    on_progress, should_stop : callable, optional
        As in `nagasim.api.run_model`.
    evaluator : optional
        Replaces the internal evaluator, for a caller that needs its own response
        KPIs (as `nagasim.calibrate` does) or wants the cached per-replication
        values afterwards. It carries its own budget, so *budget* then only sizes
        the search's internal splits.
    warm_start_points : list of dict, optional
        Evaluated first, through the same gate as every other point, so they are
        simulated rather than trusted and count in *budget*.
        `nagasim.surrogate.Surrogate.suggest` proposes such points.
    pool : ProcessPoolExecutor, optional
        A pool the caller owns.
    min_improvement : float, default 0.01
        How much better a move must be, as a fraction of the objective's size, to
        count as a success: below it the point is still taken but the step does
        not widen and no replications are bought to settle it. 0 restores "any
        significant gain is a success".
    record_logs : bool, optional
        As in `nagasim.api.run_model`.

    Returns
    -------
    OptimizationResult
        ``best`` (factor values), ``best_mean``, ``history`` and the selection
        outcome.

    See Also
    --------
    nagasim.experiments.run_scenarios : Compare points you name yourself.
    nagasim.calibrate.calibrate : Search for parameters that reproduce observations.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim.optimize import Factor, optimize
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> factor = Factor(path="blocks.process_1.params.dist_params.rate",
    ...                 kind="continuous", low=1.1, high=2.0, name="rate")
    >>> found = optimize(str(path), [factor], response="system/Avg Queue Length", goal="min",
    ...                  budget=12, replications_per_point=2, run_length=200, warmup=20, seed=0)
    >>> 1.1 <= found.best["rate"] <= 2.0
    True
    """
    base = load_layout(layout)
    factors = list(factors)
    if not factors:
        raise ValueError("optimize needs at least one factor")
    constraints = [tuple(c) for c in constraints]
    sign = -1.0 if goal == "min" else 1.0
    floor_gain = max(float(min_improvement), 0.0)
    reps = max(int(replications_per_point), 2)
    cap = int(max_replications_per_point or 4 * reps)
    evaluator = evaluator or _Evaluator(base, run_length=run_length, warmup=warmup,
                                        workers=workers, budget=int(budget), pool=pool,
                                        record_logs=record_logs,
                                        kpis=[response, *(c[0] for c in constraints),
                                              *(o[0] for o in (objectives or ()))])
    history: list[dict] = []
    records: dict[tuple, _Record] = {}
    best_so_far = [-math.inf]
    # The search may not spend the whole budget: the final ranking-and-selection
    # step is part of the answer, not an extra, so it gets its own slice.
    search_limit = int(budget) - min(int(budget) // 4, max(int(select_top), 1) * reps)

    def seeds(count: int) -> list[int]:
        return [seed + r for r in range(count)]

    def evaluate_all(points, count: int = reps) -> list:
        """Evaluate (or top up) SEVERAL independent points as ONE wave of
        (point, replication) tasks (PHASE4 T3); one `_Record` or None -- the
        budget forbade it -- per point, in the order given.

        The wave is priced against the search's own slice of the budget before
        anything runs, point by point in order, so the cap is exactly as hard
        as it was when every point was evaluated on its own.
        """
        keys = [_point_key(factors, point) for point in points]
        coerced = [{f.name: value for f, value in zip(factors, key)} for key in keys]
        requests, projected = [], 0
        room = search_limit - evaluator.used
        for key, point in zip(keys, coerced):
            cost = len(evaluator.missing(key, seeds(count)))
            if projected + cost > room:
                break
            projected += cost
            requests.append((key, overrides_for(factors, point), seeds(count)))
        settled = evaluator.values_many(requests, should_stop=should_stop)

        out: list = []
        for key, point in zip(keys, coerced):
            values = settled.get(key)
            if values is None:
                out.append(None)
                continue
            record = _Record(key=key, point=point, objective=sign * _response(values, response))
            record.feasibility = _feasibility(constraints, values, confidence)
            records[key] = record
            if record.feasibility != "infeasible" and record.mean > best_so_far[0]:
                best_so_far[0] = record.mean
            stat = ci95(sign * record.objective)
            reached = sign * best_so_far[0] if math.isfinite(best_so_far[0]) else stat["mean"]
            history.append({"Point": _fmt_point(point), **point, "Mean": stat["mean"],
                            "LB": stat["lb"], "UB": stat["ub"], "N": int(record.objective.size),
                            "Feasibility": record.feasibility, "Budget used": evaluator.used,
                            "Best so far": reached})
            out.append(record)
        if on_progress is not None:
            on_progress(evaluator.used, int(budget))
        return out

    def evaluate(point: dict, count: int = reps) -> _Record | None:
        """Evaluate (or top up) one point; None when the budget forbids it."""
        return evaluate_all([point], count)[0]

    def prefers(a: _Record, b: _Record | None) -> bool:
        """Plain ordering (feasibility first, then mean) for picking a starting
        point out of the pilot design -- no statistical claim attached."""
        if b is None:
            return True
        rank_a, rank_b = _FEASIBILITY_RANK[a.feasibility], _FEASIBILITY_RANK[b.feasibility]
        return (rank_a, a.mean) > (rank_b, b.mean)

    def wins(challenger: _Record, incumbent: _Record) -> bool:
        """A move needs evidence, not a better sample mean."""
        rank_a = _FEASIBILITY_RANK[challenger.feasibility]
        rank_b = _FEASIBILITY_RANK[incumbent.feasibility]
        if rank_a != rank_b:
            return rank_a > rank_b
        n = min(challenger.objective.size, incumbent.objective.size)
        diff = paired_diff_ci(challenger.objective[:n], incumbent.objective[:n], confidence)
        if diff["lb"] > 0:
            return True
        if not (diff["mean"] > 0) or challenger.objective.size >= cap:
            return False
        if diff["mean"] <= floor_gain * abs(incumbent.mean):
            # ARCHITECTURE D80: no replications are bought to settle a difference
            # too small to matter even if it is real.
            return False
        # Ambiguous but promising: buy more replications for BOTH (same seeds,
        # so the comparison stays paired) and ask once more.
        more = min(cap, challenger.objective.size * 2)
        topped_up, refreshed = evaluate_all([challenger.point, incumbent.point], more)
        if topped_up is None or refreshed is None:
            return False
        incumbent.objective = refreshed.objective
        incumbent.feasibility = refreshed.feasibility
        challenger.objective = topped_up.objective
        challenger.feasibility = topped_up.feasibility
        n = min(challenger.objective.size, incumbent.objective.size)
        return paired_diff_ci(challenger.objective[:n], incumbent.objective[:n],
                              confidence)["lb"] > 0

    if method == "optuna-nsga2":
        return _pareto_search(evaluator, factors, objectives, constraints, confidence,
                              int(budget), reps, seed, should_stop, on_progress)
    sampler_used = [""]
    if method in ("optuna", "optuna-gp"):
        incumbent, converged = _optuna_search(evaluate, factors, budget, reps, seed, prefers,
                                              "gp" if method == "optuna-gp" else "tpe",
                                              sampler_used)
    else:
        incumbent, converged = _pattern_search(evaluate_all, factors, budget, reps, seed,
                                               prefers, wins, should_stop, warm_start_points,
                                               floor_gain)
    if incumbent is None:
        raise ModelValidationError(["the budget was too small to evaluate a single point"])

    # Final confirmation over the best distinct points, on the leftover budget.
    selection = None
    ranked = sorted(records.values(), key=lambda r: (_FEASIBILITY_RANK[r.feasibility], r.mean),
                    reverse=True)[:max(int(select_top), 1)]
    if len(ranked) > 1 and evaluator.remaining > 0:
        gap = indifference
        if gap is None:
            gap = max(abs(incumbent.mean) * 0.05, 1e-9)
        selection = select_best(
            None, [{"name": _fmt_point(r.point), "key": r.key,
                    "overrides": overrides_for(factors, r.point)} for r in ranked],
            response=response, goal=goal, confidence=confidence, indifference=gap,
            n0=min(reps, min(r.objective.size for r in ranked)),
            max_replications=int(min(cap, reps + evaluator.remaining // len(ranked))),
            seed=seed, evaluator=evaluator, should_stop=should_stop)
        chosen = next((r for r in ranked if _fmt_point(r.point) == selection.best), None)
        if chosen is not None:
            incumbent = chosen

    frame = pd.DataFrame(history)
    return OptimizationResult(best=dict(incumbent.point),
                              best_overrides=overrides_for(factors, incumbent.point),
                              history=frame, converged=converged, budget_used=evaluator.used,
                              response=response, goal=goal,
                              feasible=incumbent.feasibility != "infeasible",
                              selection=selection, sampler=sampler_used[0])


# `_pattern_search`/`_optuna_search` moved here from the Sobol-indices section
# of the original file: both are called only by `optimize` above, never by
# `sobol`, so keeping them there would have made this module import Sobol's
# just to call back into itself (a real cycle) -- pure code motion, no
# behaviour change.
def _pattern_search(evaluate_all, factors, budget, reps, seed, prefers, wins, should_stop,
                    warm_start=None, min_improvement=_MIN_IMPROVEMENT):
    """Steps 1-6 of `optimize`'s docstring; returns (incumbent, converged).

    Converged means EVERY factor's step shrank below its own grid unit with no
    improvement left to find -- not that the budget ran out. *warm_start*
    (ROADMAP 3.7 B item 7), when given, is evaluated before the Latin hypercube
    pilot, on the SAME `evaluate_all()` gate -- so it costs budget exactly like
    a pilot point.

    PARALLELISM (PHASE4 T3). The points of the pilot design are independent of
    each other, and so are the 2n poll points of one iteration: each group goes
    to the workers as ONE wave. Iterations stay sequential -- the next poll is
    around wherever this one moved. Because a whole poll is evaluated, the move
    is the BEST of it rather than the first that wins, and the statistical test
    (`wins`, which buys replications of its own) is run once per iteration
    instead of up to 2n times.

    STEP SCALING (PHASE4 T7, ARCHITECTURE D80). The step is PER FACTOR and it
    grows only on a gain that is large relative to the objective's own scale.
    The poll already values each axis separately, so it is also the screening:
    an axis whose own two neighbours beat nothing is refined on the spot, and
    once its step falls below its grid unit it stops being polled at all, which
    is how the budget moves to the factors the objective actually responds to.
    """
    n = len(factors)
    incumbent = None
    warm = [{f.name: point.get(f.name, f.from_unit(0.5)) for f in factors}
            for point in (warm_start or [])]
    for record in evaluate_all(warm):
        if record is None:
            return incumbent, False
        if prefers(record, incumbent):
            incumbent = record
    pilot = max(4, min(10 * n, max(int(budget) // (2 * reps), 2)))
    pilot_points = [{f.name: row[f.name] for f in factors}
                    for _index, row in latin_hypercube(factors, pilot, seed=seed).iterrows()]
    for record in evaluate_all(pilot_points):
        if record is None:
            break
        if prefers(record, incumbent):
            incumbent = record
    if incumbent is None:
        return None, False

    # One grid unit, PER FACTOR: for an integer or categorical factor a step
    # below 1/range can never move the point, so there is nothing left to try
    # below that; a continuous factor stops at 1% of its range, which is finer
    # than the noise on a simulated mean ever justifies.
    floor = {f.name: (1.0 / max(f.high - f.low, 1.0) if f.kind != "continuous" else 0.01)
             for f in factors}
    step = {f.name: 0.25 for f in factors}
    rounds = 0
    while any(step[f.name] >= floor[f.name] for f in factors) and rounds < 200 * n:
        rounds += 1
        if should_stop is not None and should_stop():
            return incumbent, False
        trials, axes = [], []
        for factor in factors:
            if step[factor.name] < floor[factor.name]:
                continue                 # this axis is finished: stop paying for it
            for direction in (1.0, -1.0):
                trial = dict(incumbent.point)
                trial[factor.name] = factor.from_unit(
                    factor.to_unit(incumbent.point[factor.name]) + direction * step[factor.name])
                if trial[factor.name] != incumbent.point[factor.name]:
                    trials.append(trial)
                    axes.append(factor.name)
        moved = False
        if trials:                       # empty once a step cannot move any factor
            polled = [(axis, record) for axis, record
                      in zip(axes, evaluate_all(trials)) if record is not None]
            if not polled:
                return incumbent, False              # out of budget
            best: dict[str, _Record] = {}
            for axis, record in polled:
                if _better(record, best.get(axis)):
                    best[axis] = record
            axis = max(best, key=lambda a: (_FEASIBILITY_RANK[best[a].feasibility], best[a].mean))
            challenger = best[axis]
            moved = wins(challenger, incumbent)
            if moved:
                # `wins` may have bought replications for both, so the gain is
                # read after it: a move that is real but negligible against the
                # objective's own scale keeps the point and NOT the step (D80).
                gain = challenger.mean - incumbent.mean
                scale = abs(incumbent.mean)
                big = gain > min_improvement * scale if scale > 0 else gain > 0
                for other, record in best.items():
                    if other != axis and record.mean <= incumbent.mean:
                        step[other] *= 0.5       # nothing on that axis at this resolution
                incumbent = challenger
                if big:
                    step[axis] = min(1.0, step[axis] * 2.0)
        if not moved:
            for name in step:
                step[name] *= 0.5
    return incumbent, bool(all(step[f.name] < floor[f.name] for f in factors))


def _suggest(trial, factors) -> dict:
    point = {}
    for factor in factors:
        if factor.kind == "categorical":
            point[factor.name] = trial.suggest_categorical(factor.name, factor.levels)
        elif factor.kind == "integer":
            point[factor.name] = trial.suggest_int(factor.name, int(factor.low), int(factor.high))
        else:
            point[factor.name] = trial.suggest_float(factor.name, factor.low, factor.high,
                                                     step=factor.step)
    return point


def _optuna():
    """Optuna, imported here and nowhere else, so nothing in the default path
    needs it."""
    try:
        import optuna
    except ImportError as exc:      # pragma: no cover - depends on the environment
        raise ImportError("the optuna methods need the optional optuna package "
                          "(pip install \"nagasim[ml]\", or: pip install optuna); "
                          "the built-in method needs nothing") from exc
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    return optuna


def _optuna_search(evaluate, factors, budget, reps, seed, prefers, kind="tpe", used=None):
    """`method="optuna"` (TPE) and `"optuna-gp"` over the same factor space."""
    optuna = _optuna()
    best: list = [None]

    def objective(trial):
        record = evaluate(_suggest(trial, factors))
        if record is None:
            trial.study.stop()
            return -math.inf
        if prefers(record, best[0]):
            best[0] = record
        return record.mean            # sign-adjusted: the study always maximises here

    sampler = optuna.samplers.TPESampler(seed=seed)
    if kind == "gp":
        # optuna's GPSampler imports torch, which is not in the `ml` extra:
        # without it use multivariate TPE (models interactions) and say so.
        try:
            import torch  # noqa: F401
            sampler = optuna.samplers.GPSampler(seed=seed)
        except ImportError:
            logging.getLogger(__name__).warning(
                "optuna-gp needs torch, which is not installed: using multivariate TPE")
            sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True)
    if used is not None:
        used[0] = type(sampler).__name__
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=max(1, int(budget) // reps), catch=())
    return best[0], False       # stops on the trial budget, never on convergence


def _pareto_search(evaluator, factors, objectives, constraints, confidence, budget, reps, seed,
                   should_stop, on_progress):
    """`method="optuna-nsga2"`: NSGA-II over the objectives' means; the result is
    the non-dominated set of feasible points. No KN step: with several objectives
    there is no single winner to confirm.

    ponytail: membership is judged on sample means, so a point near the front
    may be on it by noise; add per-objective intervals if that bites.
    """
    objectives = [tuple(o) for o in (objectives or ())]
    if len(objectives) < 2:
        raise ValueError("method='optuna-nsga2' needs objectives: at least two (kpi, goal) pairs")
    signs = [-1.0 if g == "min" else 1.0 for _, g in objectives]
    optuna = _optuna()
    rows: dict[tuple, dict] = {}
    lost = [-math.inf] * len(objectives)

    def objective(trial):
        key = _point_key(factors, _suggest(trial, factors))
        point = {f.name: v for f, v in zip(factors, key)}
        seeds = [seed + r for r in range(reps)]
        if len(evaluator.missing(key, seeds)) > budget - evaluator.used:
            trial.study.stop()
            return lost
        values = evaluator.values_many([(key, overrides_for(factors, point), seeds)],
                                       should_stop=should_stop).get(key)
        if values is None:
            trial.study.stop()
            return lost
        means = [s * float(np.nanmean(_response(values, k)))
                 for s, (k, _) in zip(signs, objectives)]
        rows[key] = {"point": point, "means": means,
                     "feasible": _feasibility(constraints, values, confidence) != "infeasible"}
        if on_progress is not None:
            on_progress(evaluator.used, budget)
        return means

    study = optuna.create_study(directions=["maximize"] * len(objectives),
                                sampler=optuna.samplers.NSGAIISampler(seed=seed))
    study.optimize(objective, n_trials=max(1, budget // reps), catch=())
    if not rows:
        raise ModelValidationError(["the budget was too small to evaluate a single point"])
    feasible = [r for r in rows.values() if r["feasible"]]
    pool = feasible or list(rows.values())
    front = [r for r in pool if not any(
        all(o >= m for o, m in zip(other["means"], r["means"]))
        and any(o > m for o, m in zip(other["means"], r["means"])) for other in pool)]
    table = pd.DataFrame([{"Point": _fmt_point(r["point"]), **r["point"],
                           **{k: s * m for s, (k, _), m in zip(signs, objectives, r["means"])}}
                          for r in front])
    # `best` is the front's winner on the first objective, so callers that only
    # read best/best_overrides keep working; the front itself is `pareto`.
    top = max(front, key=lambda r: r["means"][0])
    history = pd.DataFrame([{"Point": _fmt_point(r["point"]), **r["point"],
                             "Mean": signs[0] * r["means"][0]} for r in rows.values()])
    return OptimizationResult(best=dict(top["point"]),
                              best_overrides=overrides_for(factors, top["point"]),
                              history=history, converged=False, budget_used=evaluator.used,
                              response=objectives[0][0], goal=objectives[0][1],
                              feasible=bool(feasible), sampler="NSGAIISampler", pareto=table)
