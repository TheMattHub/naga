# Expressions and variables

## The expression language

A model file is data, not code. Every expression a model can carry -- ASSIGN targets/values,
DECIDE/HOLD conditions, RECORD's `expr`, the CustomExpression distribution, and any
expression-valued distribution parameter -- is compiled by
`core/expressions.py`: `ast.parse` the text, check it against an allow-list, then evaluate by
walking the tree. Nothing reaches Python's `eval`/`exec`, so a model file cannot import a module,
call an arbitrary builtin, or touch any attribute other than `entity.<name>`. A bad expression is
a load-time problem with a code (`naga validate`), not a run-time crash.

See the generated [expression reference](../reference/functions/index.md) for the full grammar table
and function catalogue (`RAND`, `EXPO`, `UNIF`, `NORM`, `TRIA`, `DISC`, `NQ`, `NR`, `MR`, `NS`,
`TABLE`, and the usual math functions).

## Model variables

Declared in the **Variables** table (or the top-level `variables` section of the model file):
name + `initial` value. Read by any expression as a bare name; written by ASSIGN
(`{"target": "rework_count", "expr": "rework_count + 1"}`). Reset to `initial` at the start of
every replication -- a variable never carries a value from one replication into the next.

## Sampling inside an expression

`RAND()`, `EXPO(mean)`, `UNIF(low, high)`, `NORM(mean, sd)`, `TRIA(low, mode, high)` and `DISC(...)`
all draw from the **calling block's own RNG stream**, the same one its distribution parameters use
-- so a model stays reproducible run-to-run for a fixed seed even when a condition or an
expression-valued parameter calls one of these.

## Live model state

`NQ("name")` / `NR("name")` / `MR("name")` read a resource or resource set's current queue length,
units in service, and scheduled capacity; `NS("name")` reads a store's item count (a Hold/Batch/
Match block's internal store, by block or store name). Useful for a condition like
`NQ("Server") < 5` in a DECIDE, or in a SEIZE/PROCESS's own `balk_if` -- see
[Queue behaviours: balking, reneging, preemption](resources-queues.md).

## Lookup tables

`TABLE("ProcessTime", entity.e_type, "Cut")` reads a number out of the model's `tables` section --
a 1-D or 2-D grid keyed by strings or numbers, with `exact`, `step` or `linear` matching per
dimension. It is how a duration, a cost or a setup time depends on WHAT is being processed instead
of on chance. See
the bundled model `table_process_times` for a worked example.

## Submodel parameters

Inside a submodel definition, an instance's arguments are visible as plain names in every
expression, exactly like a model variable, but scoped to that instance only -- see
[Submodels](submodels.md).
