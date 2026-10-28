# How do I pack parts on a pallet and unpack them later?

Parts travel together on a pallet and are handled one by one at the end. BATCH makes the pallet and SEPARATE unpacks it. The script packs 60 parts five to a pallet and sends the pallets on a 15-minute trip.

```python
--8<-- "docs/snippets/recipe-unpack-pallet.py"
```

Save it as `recipe-unpack-pallet.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-unpack-pallet-output.txt"
```

## How it works

1. `m.batch(batch_size=5, batched_entity_type="PALLET")` holds parts until five are there, then sends one pallet.
2. `m.separate()` releases the five parts again, so 12 pallets give back all 60 parts.
3. A part waits in the pallet while it fills, so its time in the model is 22 minutes and not 15.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [BATCH](../../reference/blocks/batch.md), [DELAY](../../reference/blocks/delay.md), [SEPARATE](../../reference/blocks/separate.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
