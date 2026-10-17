from __future__ import annotations

from nagasim.core.schema import ModelValidationError
from nagasim.core.stats import ci95
from dataclasses import dataclass
from nagasim.core.schema import load_layout
import itertools
import numpy as np
import pandas as pd
from .evaluation import _Evaluator, _response

# ---------------------------------------------------------------- ranking and selection
@dataclass
class SelectionResult:
    """What `select_best` returns.

    `table` carries each candidate's mean with a Student-t CI, the
    replications it received, and "Eliminated at": the replication count at
    which screening dropped it, 0 for a candidate that survived to the end.
    `guaranteed` is False when the replication cap, a cancellation or an
    exhausted budget stopped the procedure before its own stopping rule did --
    the winner is then simply the best sample mean among the survivors, with no
    probability statement attached.
    """

    best: str
    best_overrides: list
    table: pd.DataFrame
    replications: dict
    survivors: list
    confidence: float
    indifference: float
    goal: str
    response: str
    guaranteed: bool = True
    budget_used: int = 0


def _candidates(candidates) -> list[tuple[str, object, list]]:
    """[(name, cache key, overrides)] from names, override lists, or dicts with
    "name"/"overrides" (and an optional "key" to share an evaluator's cache)."""
    out = []
    for index, entry in enumerate(candidates):
        if isinstance(entry, dict) and ("overrides" in entry or "name" in entry):
            name = str(entry.get("name") or f"candidate {index + 1}")
            overrides = list(entry.get("overrides") or [])
            key = entry.get("key", name)
        elif isinstance(entry, str):
            name, overrides, key = entry, [], entry
        else:
            name = f"candidate {index + 1}"
            overrides = list(entry or [])
            key = name
        out.append((name, key, overrides))
    return out


def select_best(layout, candidates, *, response: str, goal: str = "min", confidence: float = 0.95,
                indifference: float = 0.0, n0: int = 10, max_replications: int = 100,
                crn: bool = True, workers: int = 1, seed: int = 0, run_length=None, warmup=None,
                evaluator=None, on_progress=None, should_stop=None,
                record_logs=None) -> SelectionResult:
    """Pick the best of a finite set of candidates by the fully sequential
    indifference-zone procedure KN (Kim and Nelson 2001).

    GUARANTEE. Let d = *indifference* > 0 and 1 - alpha = *confidence*. If the
    per-replication outputs are i.i.d. normal across replications (correlation
    ACROSS candidates is allowed and expected, so common random numbers are
    fine), then

        P{the selected candidate is the true best} >= 1 - alpha
        whenever the best candidate's true mean is at least d better
        than every other candidate's mean;

    and when no such gap exists, the selected candidate's mean is within d of
    the best with at least that probability. The guarantee needs the procedure
    to run to its own stopping rule: if *max_replications*, a cancellation or
    an exhausted evaluator budget ends it first, the best surviving sample mean
    is returned with `guaranteed=False`.

    Mechanics. With k candidates and a first stage of *n0* replications,

        eta = 0.5 * ((2 alpha / (k - 1))^(-2/(n0-1)) - 1),  h2 = 2 eta (n0 - 1)

    and the pairwise variance of DIFFERENCES S2_ij (estimated once, from the
    first stage -- this is what makes the procedure CRN-aware: common random
    numbers shrink S2_ij, and the procedure spends fewer replications rather
    than claiming a wrong guarantee). After each replication r every surviving
    candidate i is screened against every other survivor j:

        i survives if mean_i(r) >= mean_j(r) - W_ij(r) for all j,
        W_ij(r) = max(0, (d / 2r) (h2 S2_ij / d^2 - r))

    so a clearly inferior candidate stops receiving replications as soon as the
    evidence against it exceeds that shrinking tolerance. One replication is
    added to each survivor per round until one is left.

    *candidates* are override lists, names, or dicts with "name"/"overrides".
    A "min" goal is handled by negating the response.
    """
    entries = _candidates(candidates)
    if not entries:
        raise ValueError("select_best needs at least one candidate")
    base = load_layout(layout) if layout is not None else {}
    evaluator = evaluator or _Evaluator(base, run_length=run_length, warmup=warmup,
                                        workers=workers, record_logs=record_logs,
                                        kpis=[response])
    started = evaluator.used
    sign = -1.0 if goal == "min" else 1.0
    k = len(entries)
    n0 = max(int(n0), 2)
    max_replications = max(int(max_replications), n0)
    indifference = float(indifference)

    def seeds_for(index: int, count: int) -> list[int]:
        # Without CRN each candidate needs its own seed block; the block is
        # sized by the cap so a later round never collides with its neighbour.
        offset = seed if crn else seed + index * max_replications
        return [offset + r for r in range(count)]

    data: dict[str, np.ndarray] = {}
    stopped = False
    # The first stage is k independent candidates: one wave of tasks (PHASE4 T3).
    settled = evaluator.values_many([(key, overrides, seeds_for(index, n0))
                                     for index, (_name, key, overrides) in enumerate(entries)],
                                    should_stop=should_stop)
    for index, (name, key, overrides) in enumerate(entries):
        if key not in settled:
            stopped = True
            break
        data[name] = sign * _response(settled[key], response)
    if on_progress is not None:
        on_progress(evaluator.used - started, k * max_replications)

    if not data:
        raise ModelValidationError(["no candidate could be evaluated within the budget"])

    alive = [name for name, _key, _ovr in entries if name in data]
    survivors = [list(alive)]
    eliminated: dict[str, int] = {}
    r = n0

    if len(alive) > 1 and not stopped:
        alpha = max(1e-9, 1.0 - float(confidence))
        eta = 0.5 * ((2.0 * alpha / (k - 1)) ** (-2.0 / (n0 - 1)) - 1.0)
        h2 = 2.0 * eta * (n0 - 1)
        # S2_ij from the first stage only, as KN prescribes.
        var = {}
        for i, j in itertools.combinations(alive, 2):
            diff = data[i][:n0] - data[j][:n0]
            var[(i, j)] = var[(j, i)] = float(np.var(diff, ddof=1))

        index_of = {name: i for i, (name, _k, _o) in enumerate(entries)}
        while len(alive) > 1 and r <= max_replications:
            means = {name: float(np.nanmean(data[name][:r])) for name in alive}

            def width(i, j) -> float:
                if indifference <= 0:
                    return 0.0
                return max(0.0, (indifference / (2.0 * r))
                           * (h2 * var[(i, j)] / indifference ** 2 - r))

            keep = [i for i in alive
                    if all(means[i] >= means[j] - width(i, j) for j in alive if j != i)]
            if not keep:                       # degenerate tie: the top mean stays
                keep = [max(alive, key=lambda n: means[n])]
            if len(keep) > 1 and getattr(evaluator, "deterministic", False):
                # DEVLOG #96. On a model that does not depend on the seed, every
                # further replication returns these same numbers: the screening
                # tolerance is 0 (the variance of the differences is 0), so no
                # survivor can ever eliminate another and the procedure would
                # buy replications until the cap. Tied candidates are equally
                # best, so the tie is resolved the way a design study wants it
                # -- the first one offered -- and the guarantee still holds.
                keep = [min(keep, key=index_of.__getitem__)]
            for name in alive:
                if name not in keep:
                    eliminated[name] = r
            alive = keep
            survivors.append(list(alive))
            if len(alive) == 1 or r >= max_replications:
                break
            if should_stop is not None and should_stop():
                stopped = True
                break
            r += 1
            # Every survivor gets its extra replication in the SAME wave.
            settled = evaluator.values_many(
                [(entries[index_of[n]][1], entries[index_of[n]][2], seeds_for(index_of[n], r))
                 for n in alive], should_stop=should_stop)
            for name in alive:
                values = settled.get(entries[index_of[name]][1])
                if values is None:
                    stopped = True
                    r -= 1
                    break
                data[name] = sign * _response(values, response)
            if stopped:
                break
            if on_progress is not None:
                on_progress(evaluator.used - started, k * max_replications)

    best = max(alive, key=lambda n: float(np.nanmean(data[n])))
    by_name = {name: (key, overrides) for name, key, overrides in entries}
    rows = []
    for name in data:
        values = sign * data[name]
        stat = ci95(values)
        rows.append({"Candidate": name, "Mean": stat["mean"], "LB": stat["lb"], "UB": stat["ub"],
                     "N": int(np.isfinite(values).sum()), "Eliminated at": eliminated.get(name, 0),
                     "Selected": name == best})
    table = pd.DataFrame(rows, columns=["Candidate", "Mean", "LB", "UB", "N",
                                        "Eliminated at", "Selected"])
    return SelectionResult(best=best, best_overrides=list(by_name[best][1]), table=table,
                           replications={n: int(v.size) for n, v in data.items()},
                           survivors=survivors, confidence=confidence,
                           indifference=indifference, goal=goal, response=response,
                           guaranteed=bool(len(alive) == 1 and not stopped),
                           budget_used=evaluator.used - started)

