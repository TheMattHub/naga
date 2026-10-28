# How do I share a site's power limit between chargers?

A depot with a grid limit cannot give every car its full power at once. CHARGE shares a site limit between the cars charging. The script runs the same cars on a 100 kW and on a 60 kW site.

```python
--8<-- "docs/snippets/recipe-site-power.py"
```

Save it as `recipe-site-power.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-site-power-output.txt"
```

## How it works

1. `m.charge(energy, max_power, site_limit)` charges each car with up to `max_power` while the total stays under `site_limit`.
2. A car alone needs 24 minutes for 20 kWh at 50 kW; overlaps stretch that, and a 60 kW limit stretches it much more.
3. The `charge_time` of each entity is recorded, so you can read the delay the limit causes.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [CHARGE](../../reference/blocks/charge.md), [RECORD](../../reference/blocks/record.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
