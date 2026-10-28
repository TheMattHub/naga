# How do I model repair times with a long tail?

Repair and lifetime data often have a heavier tail than a lognormal. LogLogistic and the two Pearson types cover that. The script sets each to a 3-minute mean and runs the same bay.

```python
--8<-- "docs/snippets/recipe-repair-tail.py"
```

Save it as `recipe-repair-tail.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-repair-tail-output.txt"
```

## How it works

1. `LogLogistic(c, scale)`, `PearsonTypeV(a, scale)` and `PearsonTypeVI(a, b, scale)` are right-skewed and never negative.
2. `distribution_mean` returns the mean of each, so you can set the parameters to a mean you know before you compare tails.
3. At the same mean the Pearson VI queue waits longest here, which shows why you should not pick a family by its mean alone.

## Elements used

- Distributions: [LogLogistic](../../reference/distributions/log-logistic.md), [PearsonTypeV](../../reference/distributions/pearson-type-v.md), [PearsonTypeVI](../../reference/distributions/pearson-type-vi.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
