# How do I model handling times with an occasional very long one?

Manual work is usually quick, with now and then a job that takes far longer. A lognormal service time has that shape. The script below runs the same packing queue twice, once with a lognormal time and once with a constant of the same mean.

```python
--8<-- "docs/snippets/recipe-lognormal-tail.py"
```

Save it as `recipe-lognormal-tail.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-lognormal-tail-output.txt"
```

## How it works

1. `Lognormal(mu, sigma)` takes the mean and spread of the logarithm of the time, so its own mean is `exp(mu + sigma^2 / 2)`.
2. Both services average 1.52 minutes, but the lognormal queue waits longer, because the long jobs hold the packer while others line up.
3. Use it when your data are positive and right-skewed; the [fit recipe](../../goals/fit-input-distributions.md) shows how to test that.

## Elements used

- Distributions: [Lognormal](../../reference/distributions/lognormal.md), [Exponential](../../reference/distributions/exponential.md), [Constant](../../reference/distributions/constant.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
