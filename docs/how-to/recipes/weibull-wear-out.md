# How do I model a machine that wears out?

A machine that wears out fails at about the same age, while a memoryless machine fails at any age. Weibull with a shape above 1 is the wear-out case. The script gives a press a Weibull uptime and an exponential one with the same 100-minute mean.

```python
--8<-- "docs/snippets/recipe-weibull-wear-out.py"
```

Save it as `recipe-weibull-wear-out.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-weibull-wear-out-output.txt"
```

## How it works

1. `Weibull(scale, shape)` with `shape = 3` bunches failures around the mean; `shape = 1` is the exponential.
2. `add_failure` takes the uptime and downtime as distributions, so any family in the reference can drive a breakdown.
3. Both presses fail 100 minutes apart on average, and the regular failures cost a little less waiting.

## Elements used

- Distributions: [Weibull](../../reference/distributions/weibull.md), [Exponential](../../reference/distributions/exponential.md), [Constant](../../reference/distributions/constant.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Model-file sections: [`failures`](../../reference/model-file/failures.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
