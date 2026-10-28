# How do I count the defects in a batch?

A batch of parts holds a number of defective ones, each defective with the same chance. The count is Binomial, and one part is the Bernoulli case. The script charges one minute of rework per defect.

```python
--8<-- "docs/snippets/recipe-binomial-defects.py"
```

Save it as `recipe-binomial-defects.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-binomial-defects-output.txt"
```

## How it works

1. `Binomial(n, p)` is the defect count in `n` parts when each is bad with probability `p`; the mean is `n * p`.
2. `Bernoulli(p)` is a single yes-or-no part, returning 1 with probability `p` and 0 otherwise.
3. A batch of 20 with `p = 0.1` has 2 defects on average, which is 2 minutes of rework every 4 minutes.

## Elements used

- Distributions: [Binomial](../../reference/distributions/binomial.md), [Bernoulli](../../reference/distributions/bernoulli.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
