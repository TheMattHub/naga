# How do I model an approval or reaction time that is never negative?

A person's reaction or an approval step takes time that is positive and skewed to the right. The inverse Gaussian is the time a drifting quantity needs to reach a level. The script uses it as the approver's time and checks its mean first.

```python
--8<-- "docs/snippets/recipe-decision-delay.py"
```

Save it as `recipe-decision-delay.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-decision-delay-output.txt"
```

## How it works

1. `InverseGaussian(mu, lambda)` is the textbook form: `mu` is the mean and `lambda` the shape.
2. A larger `lambda` gives less spread around the mean; `distribution_mean` reads the mean back.
3. The approver is busy 60% of the time because a 3-minute mean arrives every 5 minutes.

## Elements used

- Distributions: [InverseGaussian](../../reference/distributions/inverse-gaussian.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
