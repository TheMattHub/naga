# How do I let an agent react to the agents near it?

A vehicle slows down when others are close, or a sensor reads the load of its neighbours. The NEAR functions reduce an attribute over the agents within a radius. The script places three sensors 2 m apart and reads the sum, smallest, largest and mean load within 5 m.

```python
--8<-- "docs/snippets/recipe-near-agents.py"
```

Save it as `recipe-near-agents.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-near-agents-output.txt"
```

## How it works

1. `NEAR_SUM`, `NEAR_MIN`, `NEAR_MAX` and `NEAR_MEAN` take the population, the attribute and the radius, and leave the agent itself out.
2. The sensors carry loads of 10, 12 and 14, so the sums run from 22 to 26 and the means from 11 to 13.
3. With nobody in range, `MIN` and `MAX` are NaN, so guard them with an IF on `NEAR` when agents can be alone.

## Elements used

- Functions: [`NEAR_SUM`](../../reference/functions/agent-space.md#near_sum), [`NEAR_MIN`](../../reference/functions/agent-space.md#near_min), [`NEAR_MAX`](../../reference/functions/agent-space.md#near_max), [`NEAR_MEAN`](../../reference/functions/agent-space.md#near_mean).
- Model-file sections: [`agent_types`](../../reference/model-file/agent_types.md), [`populations`](../../reference/model-file/populations.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
