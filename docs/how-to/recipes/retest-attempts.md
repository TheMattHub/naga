# How do I model retesting until a part passes?

A part is tested again until it passes, and each try costs the same time. The number of tries is Geometric. If you instead count the rejects before a set number of good parts, it is Negative binomial. The script runs both.

```python
--8<-- "docs/snippets/recipe-retest-attempts.py"
```

Save it as `recipe-retest-attempts.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-retest-attempts-output.txt"
```

## How it works

1. `Geometric(p)` is the number of attempts up to and including the first pass, with mean `1 / p`.
2. `NegativeBinomial(r, p)` is the number of failures before `r` successes, with mean `r * (1 - p) / p`.
3. At one minute per try the first loads the bench to 50%, and the second, with its mean of 0.75, to about 19%.

## Elements used

- Distributions: [Geometric](../../reference/distributions/geometric.md), [NegativeBinomial](../../reference/distributions/negative-binomial.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
