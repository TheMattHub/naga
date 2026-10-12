# Experiments

Everything about running more than one replication, comparing designs, and searching for a good
one: `nagasim.experiments` (scenarios, CRN, precision, warm-up) and `nagasim.optimize` (DOE,
ranking and selection, optimization), driven from the command line (`naga scenarios`, `naga doe`,
`naga optimize`) or from Python.

## Replications

A model runs several times ("replications"), one fresh `Environment` per seed
(`seed, seed+1, ..., seed+N-1`), because a single run is one sample from a stochastic process.
Every summary KPI comes with a 95% confidence interval across replications
(Student-t, `ddof=1`) -- read the interval, not just the mean; a narrow one earned by too few
replications is worse than a wide one that is honest about it.

## Warm-up and the MSER suggestion

Warm-up is a **scheduled statistics reset**, not a post-hoc filter: at `t = warmup`, every
collector resets so entities/statistics created before that point never enter a KPI. Pick a
warm-up long enough that the system has reached steady state (queueing systems typically start
empty, which biases early KPIs low).

**Run > Run... > Suggest warm-up** runs MSER-5 (White 1997): groups the across-replication
mean throughput-time series into batches of 5 observations and picks the truncation point that
minimizes the batch-mean variance of the tail. `reliable=False` means the minimum fell in the
second half of the batch means -- the standard MSER caveat: a still-drifting series can "look"
best when truncated near the end. Treat that as "the run is too short to tell," and lengthen the
run rather than trusting the suggestion.

## Run to precision

Instead of guessing a replication count, **run to precision**
(`nagasim.experiments.run_until_precision`) keeps adding replications until a
named KPI's confidence-interval half-width is within a relative or absolute target. After the
first `n0` replications it estimates the remaining count from `n >= (t * s / h)^2`, capped at
doubling per step so an optimistic first estimate cannot blow the budget in one call.

## Scenarios and CRN

A scenario is a named set of parameter overrides on the base model, stored in the model file
itself (so it travels with the model it belongs to). An override addresses a model parameter by
path -- `resources.Server.capacity`, `blocks.Cutting.params.dist_params.rate`,
`variables.rework_count.initial`, and so on; it can only change a value the model already has,
never invent a new one. A table's values are one value in this grammar, replaced whole. `conversions` (yields and release curves) is overridable too, since those change the
dynamics; entity types are not, since their only numeric field, `holding_cost`, feeds cost KPIs
only -- a holding-cost what-if is a post-hoc rescale (total holding cost = holding_cost x
time-averaged WIP), not a rerun.

A scenario may override `simulation.time_limit` or `simulation.warmup` and then runs with its own
window when the run length is left to the model (`naga scenarios`, `naga invest` and the agent tools
by default); a run length given explicitly (`--run-length`) applies
to every scenario. Compare count KPIs (entities completed, costs) across different windows with
care: the difference is partly the window, so prefer rates, or the investment case, which scales
each scenario by its own window.

**Common random numbers (CRN, the default)**: replication *r* of every scenario uses seed
`seed + r`, so a block a scenario did not touch draws identical numbers in every scenario. The
comparison then uses a **paired-t** interval on the per-replication differences -- normally much
narrower than an independent comparison, because the shared sampling noise cancels. `--no-crn`
uses disjoint seed blocks per scenario and **Welch's** interval instead. "Significant" means the
difference interval excludes 0.

## Parallel workers, and when they help

`--workers N` spreads work over a `ProcessPoolExecutor`. The unit of work is one replication of one
**design point** -- a scenario, a point of a design, a candidate the optimizer is trying -- so a
study of N points with R replications each has N x R tasks to fill the workers, whatever R is. That
matters most for the commonest shape of all, a screening factorial with ONE replication per point:
before, it used a single core. Measured on the bundled biogas plant, 27 full-year points, one
replication each: **148.7 s to 37.6 s on nine workers**, identical numbers.

Results are re-ordered by seed before concatenation, so the output is bit-identical to running
sequentially -- a replication depends on the seed it was given, never on which worker ran it.
`--workers auto` times one replication in-process and only pools when at least 3 tasks remain
**and** the total work left (that time x the tasks left) is worth the pool's start-up cost, about
3 seconds. Rule of thumb from measurement on Windows: one short run is faster sequential; a long
one, or many short ones, gives 2-4x with 4-9 workers.

## Design of experiments

```console
naga doe single_server --factor "resources.Server.capacity=int:1:2" --response "system/Avg Throughput Time" --design full:2 --replications 2 --run-length 200
```

In general: `naga doe MODEL --factor path=kind:low:high[:step] --response system/... --design
full:LEVELS|lhs:N|oat`. A factor addresses the model through the same override-path grammar as a
scenario, and may drive several paths at once with one value. Three design types:

| Design | What it does |
|---|---|
| `full:LEVELS` (full factorial) | Every combination of every factor's levels. Grows fast with factor count -- use for a handful of factors. |
| `lhs:N` (Latin hypercube) | N points, stratified one per 1/N of each continuous factor's range. Scales to many factors. |
| `oat` (one-at-a-time) | The baseline point, then one point per factor level with everything else held at baseline. Cheapest, weakest (misses interactions). |

`run_design`'s result reports main effects and two-factor interactions per response, all under
common random numbers by default (every design point runs on the same seeds, so point-to-point
differences are low-variance).

## Ranking and selection: the KN guarantee

`select_best` picks the best of a finite set of candidates by the fully sequential
indifference-zone procedure **KN** (Kim and Nelson, 2001). With indifference zone `d > 0` and
confidence `1 - alpha`:

> If the per-replication outputs are i.i.d. normal across replications (correlation *across*
> candidates is fine -- CRN is expected), then P{the selected candidate is the true best} >= 1 -
> alpha whenever the best candidate's true mean is at least `d` better than every other
> candidate's; when no such gap exists, the selected candidate's mean is within `d` of the best
> with at least that probability.

The guarantee needs the procedure to run to its own stopping rule; if `max_replications`, a
cancellation, or an exhausted budget ends it first, the best surviving sample mean is returned
with `guaranteed=False` instead of silently claiming the guarantee held.

## Optimization

```console
naga optimize single_server --factor "blocks.process_1.params.dist_params.rate=continuous:1.1:2.0" --response "system/Avg Queue Length" --goal min --budget 12 --replications 2 --run-length 200
```

`naga optimize MODEL --factor ... --response system/... --goal min|max --budget N`
searches continuous/integer/categorical factors for the best response within a replication
budget, an optional `optuna` backend aside (`--method optuna`, never a hard dependency). The
built-in method (`auto`/`pattern`):

1. Seed the search with a Latin hypercube (~10 x factor count points, bounded by the budget), all
   on the same CRN seeds.
2. Take the feasible point with the best mean as the incumbent.
3. Pattern search: try the 2n coordinate neighbours at each factor's **own** current step; a
   neighbour replaces the incumbent only if it *wins* (feasible where the incumbent is not, or a
   paired-t interval on the per-replication differences excludes 0 in its favour). An ambiguous
   challenger gets more replications before being retested.
4. The winning factor's step doubles only when the gain is worth it (see below); an axis whose own
   two neighbours beat nothing is halved on the spot, and a sweep with no move at all halves every
   axis. Converges when every factor's step is below its own grid unit, or the budget runs out.
5. Finishes by confirming the winner (and runners-up) with `select_best` on a reserved slice of
   the budget, so the final answer carries the KN guarantee above.

`--constraint path>=value` (repeatable) marks a design point infeasible without spending a full
evaluation comparing it as a winner. A search never changes the model file: it reports the best
point, and you keep it by adding it as a scenario.

### Significant is not the same as worth doing

With common random numbers a paired difference is measured very precisely -- which means a gain of
a few hundred on a response of half a million is *statistically significant*. A search that treats
every significant gain as a success spends its budget on the factor that improves consistently,
however little, and never gets a fine enough step on the factor that matters. That really happened
on the biogas sizing study; the tutorial records it.

`--min-improvement` is the guard: a relative threshold (default `0.01`, one per cent of the
objective) below which a move is still **taken** -- it is better, after all -- but does not widen
that factor's step and does not buy extra replications to settle itself. Raise it on a study where
only a few per cent matters; set it to `0` for the older behaviour.

Because the step is per factor and every poll values each axis separately, a factor the response
barely answers to converges quickly and then stops being polled at all, and the two evaluations an
iteration it was costing go to the factors that move the answer.
