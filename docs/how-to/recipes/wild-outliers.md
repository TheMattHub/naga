# What happens to a queue when service times have wild outliers?

Some data have extreme values on both sides, and the Cauchy distribution is the standard model of that. It has no mean at all. The script runs the same bench five times with a Cauchy service and with a tight normal one centred on the same minute.

```python
--8<-- "docs/snippets/recipe-wild-outliers.py"
```

Save it as `recipe-wild-outliers.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-wild-outliers-output.txt"
```

## How it works

1. `Cauchy(x0, gamma)` has a location and a scale but no finite mean, so the average over a run never settles.
2. A negative draw is cut to 0 with a warning, and one very large draw ties up the bench for a long time.
3. Five runs give waits from under a minute to over thirty, while the normal service gives half a minute every time.

## Elements used

- Distributions: [Cauchy](../../reference/distributions/cauchy.md), [Normal](../../reference/distributions/normal.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
