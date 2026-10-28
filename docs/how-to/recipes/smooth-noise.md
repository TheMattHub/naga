# How do I smooth a noisy signal?

A daily reading jumps around, and a decision that follows it jumps too. SMOOTH and SMOOTH3 average it over a time constant. The script adds seeded noise to a reading of 100 and compares the spread before and after.

```python
--8<-- "docs/snippets/recipe-smooth-noise.py"
```

Save it as `recipe-smooth-noise.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-smooth-noise-output.txt"
```

## How it works

1. `NOISE_NORMAL(mean, sd, lo, hi)` is a normal draw cut to `[lo, hi]`, fixed for a given seed, so the page prints the same numbers every time.
2. `SMOOTH(x, tau)` is a first-order average; `SMOOTH3` chains three stages of `tau / 3` each.
3. A 5-day average cuts the standard deviation from about 20 to about 5, at the cost of a lag of a few days.

## Elements used

- Functions: [`NOISE_NORMAL`](../../reference/functions/system-dynamics.md#noise_normal), [`SMOOTH`](../../reference/functions/system-dynamics.md#smooth), [`SMOOTH3`](../../reference/functions/system-dynamics.md#smooth3).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
