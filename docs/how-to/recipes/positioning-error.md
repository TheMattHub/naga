# How do I model a correction time that grows with a positioning error?

A robot that misses its target by some distance spends time putting it right. If each axis of the miss is normal, the size of the miss is Rayleigh. The square of that size is ChiSquare with 2 degrees of freedom. The script uses each as the correction time.

```python
--8<-- "docs/snippets/recipe-positioning-error.py"
```

Save it as `recipe-positioning-error.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-positioning-error-output.txt"
```

## How it works

1. `Rayleigh(sigma)` is the length of a two-axis error whose axes are normal with standard deviation `sigma`; its mean is 1.25 for `sigma = 1`.
2. `ChiSquare(df)` with `df = 2` is that length squared, with mean 2, for a cost that grows with the square of the miss.
3. One minute per unit of error loads the robot to about 42%, and one minute per squared unit to about 67%.

## Elements used

- Distributions: [Rayleigh](../../reference/distributions/rayleigh.md), [ChiSquare](../../reference/distributions/chi-square.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
