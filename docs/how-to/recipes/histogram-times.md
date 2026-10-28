# How do I use a histogram from a time study as a service time?

A time study gives bins and the share of jobs in each, not a formula. EmpiricalContinuous draws from exactly that histogram. The script gives a bench three bins and checks the load against the mean worked out by hand.

```python
--8<-- "docs/snippets/recipe-histogram-times.py"
```

Save it as `recipe-histogram-times.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-histogram-times-output.txt"
```

## How it works

1. `values` are the bin edges, one more than `probs`, and `probs` are the share of jobs in each bin.
2. A draw picks a bin by its weight and then a point inside it, so the result is continuous and not a resample of the points.
3. The hand mean uses the midpoint of each bin, and the run's load matches it.

## Elements used

- Distributions: [EmpiricalContinuous](../../reference/distributions/empirical-continuous.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
