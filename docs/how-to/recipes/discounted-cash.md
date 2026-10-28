# How do I discount a stream of cash?

A plant that earns every year is worth less in today's money the later the cash arrives. NPV accumulates a stream at a discount rate. The script earns 100 a year for 20 years at 5%.

```python
--8<-- "docs/snippets/recipe-discounted-cash.py"
```

Save it as `recipe-discounted-cash.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-discounted-cash-output.txt"
```

## How it works

1. `NPV(stream, rate, init, factor)` adds up the stream discounted at `rate`, on the model's step grid; it needs `simulation.sd`.
2. After 20 years the cash banked is 2,000 and its present value is 1,268, against 1,264 from the exact formula.
3. A smaller `dt` closes the small gap, because the sum is taken a step at a time.

## Elements used

- Functions: [`NPV`](../../reference/functions/system-dynamics.md#npv), [`STEP`](../../reference/functions/system-dynamics.md#step).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
