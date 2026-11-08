# How do I let an agent read the agents it is linked to?

Shops in a chain trade with their partners and watch their nearest rivals, so each needs to read its links and its neighbours. The link functions read an agent's network, and the nearest-neighbour functions read space. The script places four shops on a line, links each to its two ring neighbours and prints what each one reads.

```python
--8<-- "docs/snippets/recipe-linked-agents.py"
```

Save it as `recipe-linked-agents.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-linked-agents-output.txt"
```

## How it works

1. `LINKED`, `LINK_SUM` and `LINK_MEAN` read `self`'s out-links; `IS_LINKED` asks about one agent, and `ALIVE` checks a stored reference.
2. `KNN_SUM` and `KNN_MEAN` read the k nearest agents in space, and `NEAREST` gives the distance to the closest other one.
3. `DISTANCE` is the shortest road between two nodes of the path network, here 50 m from the depot to the yard.

## Elements used

- Functions: [`LINKED`](../../reference/functions/agents.md#linked), [`LINK_SUM`](../../reference/functions/agents.md#link_sum), [`LINK_MEAN`](../../reference/functions/agents.md#link_mean), [`IS_LINKED`](../../reference/functions/agents.md#is_linked), [`ALIVE`](../../reference/functions/agents.md#alive), [`AGENT`](../../reference/functions/agents.md#agent), [`KNN_SUM`](../../reference/functions/agent-space.md#knn_sum), [`KNN_MEAN`](../../reference/functions/agent-space.md#knn_mean), [`NEAREST`](../../reference/functions/agent-space.md#nearest), [`DISTANCE`](../../reference/functions/model-state.md#distance).
- Model-file sections: [`agent_types`](../../reference/model-file/agent_types.md), [`populations`](../../reference/model-file/populations.md), [`network`](../../reference/model-file/network.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
