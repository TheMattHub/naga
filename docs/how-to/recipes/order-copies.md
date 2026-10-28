# How do I turn one order into several jobs?

One customer order is often several tasks, such as three dishes. DUPLICATE sends copies on from the same block. The script turns each order into three kitchen jobs.

```python
--8<-- "docs/snippets/recipe-order-copies.py"
```

Save it as `recipe-order-copies.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-order-copies-output.txt"
```

## How it works

1. `m.duplicate(number_of_duplicates=3)` sends three entities on, the original and two copies, all along the same link.
2. Three 1.5-minute jobs per order every 6 minutes load the kitchen to 75%, and the run shows about 76%.
3. Each copy is a full entity, so it is counted, queued and timed on its own.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md), [DUPLICATE](../../reference/blocks/duplicate.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
