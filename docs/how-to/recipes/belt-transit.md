# How do I move parts along a belt?

A belt delays each part by its length over its speed and holds only so many parts. The script runs a 30 m belt at 1 and 2 metres a second.

```python
--8<-- "docs/snippets/recipe-belt-transit.py"
```

Save it as `recipe-belt-transit.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-belt-transit-output.txt"
```

## How it works

1. `m.conveyor(length, speed, spacing)` moves a part from one end to the other in `length / speed`, and `spacing` is the gap between parts.
2. At 1 m/s a part takes 30 s, and at 2 m/s 15 s, so the belt holds 3 parts and then 1.5 on average.
3. The `conveyor_summary` frame also reports blocked time, so a full belt shows up there.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md), [CONVEYOR](../../reference/blocks/conveyor.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
