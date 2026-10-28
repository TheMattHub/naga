# How do I stop a cycle time from going negative?

A normal cycle time has no floor, so a draw can fall below zero when the spread is large. The engine cuts such a draw to zero and warns. BoundedNormal keeps every draw between a minimum and a maximum instead. The script runs one cell with each.

```python
--8<-- "docs/snippets/recipe-bounded-cycle.py"
```

Save it as `recipe-bounded-cycle.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-bounded-cycle-output.txt"
```

## How it works

1. `Normal(mean, stddev)` can draw a negative time, which the run cuts to 0 with a warning.
2. `BoundedNormal(mean, stddev, min, max)` keeps each draw inside the limits, which also moves its mean, here from 2.00 to 2.08 minutes.
3. The two cells wait differently, so choose the bound that matches the physical limit and not the one that looks tidy.

## Elements used

- Distributions: [Normal](../../reference/distributions/normal.md), [BoundedNormal](../../reference/distributions/bounded-normal.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
