# How do I model inspecting a sample from a finite lot?

An inspector draws a small sample from a lot with a known number of defectives and does not put the parts back. The defectives found follow a Hypergeometric count. The script charges a minute of paperwork per defective found.

```python
--8<-- "docs/snippets/recipe-lot-sample.py"
```

Save it as `recipe-lot-sample.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-lot-sample-output.txt"
```

## How it works

1. `Hypergeometric(N, K, n)` is the defectives in a sample of `n` from a lot of `N` that holds `K` of them.
2. Its mean is `n * K / N`, here 1 defective a lot, so the inspector is busy 25% of the time.
3. A Binomial would draw with replacement and overstate the spread when the sample is a large part of the lot.

## Elements used

- Distributions: [Hypergeometric](../../reference/distributions/hypergeometric.md), [Binomial](../../reference/distributions/binomial.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
