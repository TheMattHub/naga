# Entities and attributes

## Entities

An entity is the thing flowing through the model: a customer, a job, a part. Every entity has:

| Field | Meaning |
|---|---|
| `id` | Auto-assigned identity counter, unique within a replication. |
| `e_type` | Its entity type, upper-cased automatically. Names its row in `entities` and can drive an entity type's `holding_cost` (see [Costing](costing.md)). |
| `creation_time` / `deletion_time` / `throughput_time` | Set by CREATE / DISPOSE; `None` until set. |
| arbitrary attributes | Anything an ASSIGN block sets, e.g. `entity.priority = 3`. Not declared anywhere, not type-checked. |

CREATE's `e_type` parameter sets the type for everything that block creates; a model can have
several CREATE blocks producing different types feeding into the same downstream blocks.

## Attributes

An attribute is any name you assign with ASSIGN: `entity.<name> = <expression>`. There is no
attribute schema to declare up front -- the first ASSIGN that sets `entity.priority` is what makes
`priority` exist on that entity from then on. Reading an attribute that was never assigned raises
an expression error at run time (fails loud rather than silently returning `None` or 0).

```jsonc
{"type": "ASSIGN", "params": {"assignments": [
  {"target": "entity.priority", "expr": "DISC(1, 0.2, 2, 0.5, 3, 0.3)"},
  {"target": "entity.cost_class", "expr": "'premium' if entity.priority == 3 else 'standard'"}
]}}
```

Assignments in one ASSIGN block run in order, so a later one can read what an earlier one just
wrote (as `cost_class` does above, reading `priority`).

## Reading attributes elsewhere

Any expression -- a DECIDE condition, a distribution parameter, a RECORD's `expr`, a resource's
`ranking_expr` -- can read `entity.<name>` the same way. See
[Expressions and variables](expressions-variables.md) for the full grammar.

## Batch and duplicate entities

BATCH/MATCH wrap several entities into one batch entity (`batch_elems`); the members leave
`live_entities` and carry `in_batch` until SEPARATE splits the batch again. DUPLICATE makes shallow
copies of an entity, each with its own id. See
[Batching and matching](batching-matching.md).
