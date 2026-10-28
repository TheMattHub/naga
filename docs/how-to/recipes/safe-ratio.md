# How do I stop a ratio dividing by zero?

A yield is good parts over parts made, and it is 0 over 0 until the line starts. ZIDZ and XIDZ return a value you choose when the divisor is zero. The script also reads a model variable with VAR.

```python
--8<-- "docs/snippets/recipe-safe-ratio.py"
```

Save it as `recipe-safe-ratio.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-safe-ratio-output.txt"
```

## How it works

1. `ZIDZ(a, b)` gives 0 when `b` is 0, and `XIDZ(a, b, x)` gives `x`.
2. `VAR("yield")` reads the model variable `yield`, a name a bare expression could not spell because it is a Python keyword.
3. Before the line starts at time 5 the ratio is 0, or 1 with XIDZ, and then it settles at 0.9.

## Elements used

- Functions: [`ZIDZ`](../../reference/functions/math.md#zidz), [`XIDZ`](../../reference/functions/math.md#xidz), [`VAR`](../../reference/functions/model-state.md#var), [`LEVEL`](../../reference/functions/model-state.md#level), [`AUX`](../../reference/functions/model-state.md#aux).
- Model-file sections: [`variables`](../../reference/model-file/variables.md), [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
