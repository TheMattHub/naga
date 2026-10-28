# How do I draw order sizes from my own data?

Order sizes are whole numbers, and you may know either their range or their measured shares. DiscreteUniform covers the first, EmpiricalDiscrete the second. The script charges a minute per item.

```python
--8<-- "docs/snippets/recipe-order-sizes.py"
```

Save it as `recipe-order-sizes.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-order-sizes-output.txt"
```

## How it works

1. `DiscreteUniform(low, high)` makes each whole number from `low` to `high` equally likely; 1 to 6 has a mean of 3.5.
2. `EmpiricalDiscrete(values, probs)` returns each value with the share you measured, here 1, 2 and 5 with 50%, 30% and 20%.
3. Their means, 3.5 and 2.1, give loads of about 87% and 52% at one order every 4 minutes.

## Elements used

- Distributions: [DiscreteUniform](../../reference/distributions/discrete-uniform.md), [EmpiricalDiscrete](../../reference/distributions/empirical-discrete.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
