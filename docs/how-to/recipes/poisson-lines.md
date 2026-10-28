# How do I model the number of lines on an order?

An order has a random number of lines, with a known average. A Poisson count suits that. The script makes the delay equal to the count, at one minute per line.

```python
--8<-- "docs/snippets/recipe-poisson-lines.py"
```

Save it as `recipe-poisson-lines.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-poisson-lines-output.txt"
```

## How it works

1. `Poisson(lambda)` returns a whole number with mean `lambda`; the keyword `lambda` is reserved in Python, so the script uses `nb.Distribution`.
2. Here one minute per line means a 6-minute mean job every 8 minutes, so the picker is busy about 75% of the time.
3. Counts are not durations, so scale them by a time per unit as the delay does here.

## Elements used

- Distributions: [Poisson](../../reference/distributions/poisson.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
