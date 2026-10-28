# How do I model a time that is a fixed part plus a random part?

A repair is a fixed diagnosis followed by a random fix, and no single distribution says that. CustomExpression takes any expression. The script uses `2 + EXPO(3)` as the bay's repair time.

```python
--8<-- "docs/snippets/recipe-formula-time.py"
```

Save it as `recipe-formula-time.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-formula-time-output.txt"
```

## How it works

1. `CustomExpression` evaluates its text each time a time is needed, so `EXPO`, `UNIF`, `NORM`, `TRIA` and `DISC` are all available.
2. The mean of `2 + EXPO(3)` is 5 minutes, so one car every 8 minutes loads the bay to about 62%.
3. It cannot be fitted from data, so use it for a known structure and a named family for a fitted shape.

## Elements used

- Distributions: [CustomExpression](../../reference/distributions/custom-expression.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
