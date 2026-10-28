# How do I route entities by a rule I write in Python?

Chance and conditions cover most routing, but sometimes the rule is code. A LOGIC block calls a class you write and goes down the link it returns. The script sends light parcels one way and heavy ones the other.

```python
--8<-- "docs/snippets/recipe-custom-routing.py"
```

Save it as `recipe-custom-routing.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-custom-routing-output.txt"
```

## How it works

1. The class needs an `execute(self, entity, ctx)` method that returns the index of the outgoing link: 0 for the first link drawn, 1 for the second.
2. You pass the module that holds the class to `run(logic=...)`; only the caller names it, and a model file cannot.
3. The `material_flow_summary` frame shows how many parcels took each link.

## Elements used

- Blocks: [CREATE](../../reference/blocks/create.md), [ASSIGN](../../reference/blocks/assign.md), [LOGIC](../../reference/blocks/logic.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
