# How do I limit the work waiting between two machines?

A buffer between two machines holds a few parts. When it is full the machine before it stops and waits. The script tries a buffer of 1, 5 and 50 parts between a cutter and a welder.

```python
--8<-- "docs/snippets/recipe-buffer-wip.py"
```

Save it as `recipe-buffer-wip.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-buffer-wip-output.txt"
```

## How it works

1. `m.buffer(capacity=n)` is a finite store between two blocks; the upstream machine is blocked while it is full.
2. A buffer of 1 blocks the cutter a quarter of the time and loses about 100 finished parts. A buffer of 5 finishes as many as 50.
3. So the first few places matter most, and you find the right size by running a few.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md), [BUFFER](../../reference/blocks/buffer.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
