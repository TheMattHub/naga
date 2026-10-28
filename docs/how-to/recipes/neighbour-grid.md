# How do I let an agent read the agents in the cells around it?

On a grid, neighbours are the cells around you and not the agents within a distance. NEIGHBOUR_SUM and NEIGHBOUR_MEAN reduce an attribute over them. The script puts three houses in a row and reads the heat of the houses next to each.

```python
--8<-- "docs/snippets/recipe-neighbour-grid.py"
```

Save it as `recipe-neighbour-grid.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-neighbour-grid-output.txt"
```

## How it works

1. `NEIGHBOUR_SUM(pop, attr)` and `NEIGHBOUR_MEAN(pop, attr)` use the eight Moore cells, or the four von Neumann cells if you ask for them.
2. The middle house has two neighbours, 1.5 and 3.5, so its sum is 5.0; each end house has one, so their sums are 2.5.
3. The agent's own cell is never counted, and an empty neighbourhood gives 0.

## Elements used

- Functions: [`NEIGHBOUR_SUM`](../../reference/functions/agent-space.md#neighbour_sum), [`NEIGHBOUR_MEAN`](../../reference/functions/agent-space.md#neighbour_mean).
- Model-file sections: [`agent_types`](../../reference/model-file/agent_types.md), [`populations`](../../reference/model-file/populations.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
