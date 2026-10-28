# How do I delay goods in transit by a fixed time or a spread of times?

Goods that leave on day 5 either all arrive exactly 4 days later, or arrive over a spread of days around the mean. DELAYP is the first, and DELAYN and DELAY3 the second. The script sends a dispatch step through all three.

```python
--8<-- "docs/snippets/recipe-transit-delay.py"
```

Save it as `recipe-transit-delay.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-transit-delay-output.txt"
```

## How it works

1. `DELAYP(x, tau)` is a pipeline: the output is `x` exactly `tau` later. `DELAYN(x, tau, n)` and `DELAY3(x, tau)` spread the output over `n` and 3 stages.
2. All three have a 4-day total delay, so the spread versions start earlier and finish later than the exact pipeline.
3. Use the pipeline for a fixed lead time and the staged forms when the transit time itself varies.

## Elements used

- Functions: [`DELAYP`](../../reference/functions/system-dynamics.md#delayp), [`DELAYN`](../../reference/functions/system-dynamics.md#delayn), [`DELAY3`](../../reference/functions/system-dynamics.md#delay3), [`STEP`](../../reference/functions/system-dynamics.md#step).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
