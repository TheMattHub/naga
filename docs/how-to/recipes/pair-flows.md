# How do I make two kinds of entity wait for each other?

A ride needs a rider and a driver, and each waits until the other is there. MATCH holds each kind until a full set is present. The script matches one rider with one driver.

```python
--8<-- "docs/snippets/recipe-pair-flows.py"
```

Save it as `recipe-pair-flows.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-pair-flows-output.txt"
```

## How it works

1. `m.match(number_to_match=2)` waits for one entity of each type that reaches it, then releases them together.
2. Riders arrive every 3 minutes and drivers every 4, so the riders queue: 99 were created and 76 matched.
3. The side that arrives more often builds the queue, so a long wait for one kind shows where to add capacity.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [MATCH](../../reference/blocks/match.md), [DELAY](../../reference/blocks/delay.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
