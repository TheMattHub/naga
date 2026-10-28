# How do I model a job made of several short steps?

A bake, a wash or an inspection often runs through a fixed number of steps in a row. An Erlang time is the sum of k exponential steps. The script compares four steps of 0.3 minutes with one exponential step of 1.2 minutes.

```python
--8<-- "docs/snippets/recipe-erlang-steps.py"
```

Save it as `recipe-erlang-steps.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-erlang-steps-output.txt"
```

## How it works

1. `Erlang(k, rate)` adds `k` exponential steps that each have the given rate, so its mean is `k / rate`.
2. Both services average 1.2 minutes, but the Erlang has a quarter of the variance, so the queue waits less.
3. Raise `k` for a more regular job; at `k = 1` it is the exponential again.

## Elements used

- Distributions: [Erlang](../../reference/distributions/erlang.md), [Exponential](../../reference/distributions/exponential.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
