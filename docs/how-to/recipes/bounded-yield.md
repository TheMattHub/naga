# How do I keep a skewed time between a minimum and a maximum?

A filling step can take at most 10 minutes and never less than 2, and it is usually closer to the top. JohnsonSB lives on a fixed interval and can be skewed, while JohnsonSU has no bounds and suits heavy tails. The script runs a filler with each.

```python
--8<-- "docs/snippets/recipe-bounded-yield.py"
```

Save it as `recipe-bounded-yield.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-bounded-yield-output.txt"
```

## How it works

1. `JohnsonSB(a1, a2, min, max)` stays inside `[min, max]`, and the shape parameters set the skew.
2. `JohnsonSU(a, b, loc, scale)` is a flexible fit with no bounds, so a draw below zero is cut to zero.
3. Both are set to a similar centre, around 7 minutes, so the two fillers are close in load.

## Elements used

- Distributions: [JohnsonSB](../../reference/distributions/johnson-sb.md), [JohnsonSU](../../reference/distributions/johnson-su.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
