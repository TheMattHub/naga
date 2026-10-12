# Batching and matching

## BATCH

Groups entities by count only. Each arrival is put into the block's own store; as soon as
`batch_size` items are waiting, exactly that many are taken **atomically** (one synchronous store
operation, so two arrivals at the same instant cannot both trigger a batch from overlapping items)
and combined into one batch entity of type `batched_entity_type`. The members leave
`live_entities` -- the batch entity stands for them in every KPI -- and carry `in_batch` until a
SEPARATE splits the batch again.

## SEPARATE

Splits a batch entity back into its `batch_elems` children. `mode` controls what the children's
attributes look like afterwards:

| Mode | Children get |
|---|---|
| Retain original entity values | Their own attributes, from before they were batched. |
| Take all representative values | Every attribute of the parent batch entity (identity/lifecycle fields excluded, so children keep distinct ids). |
| Take specific representative values | Only the attributes listed in `attrs`. |

The parent is always destroyed at SEPARATE; its accumulated cost splits evenly across the
children (see [Costing](costing.md)).

## MATCH

Matches `number_to_match` **distinct entity types** together (not Arena's attribute-based match --
a deliberate simpler semantics, documented rather than replicated). The arriving entity is always
stored first, so it can never be dropped while waiting; the match is then taken atomically, oldest
waiting entity of each type first. `batch_option` controls whether the matched entities are
combined into one batch entity or forwarded separately.

## DUPLICATE

Forwards the original entity unchanged, then forwards `number_of_duplicates - 1` shallow copies,
each a fresh entity with its own id -- unlike BATCH, this does not consume anything; it multiplies
the flow.

## When to use which

| Need | Block |
|---|---|
| Group N arrivals of one flow into a unit load | BATCH / SEPARATE |
| Synchronize arrivals of *different* types (e.g. a part and its paperwork) | MATCH |
| Fan an entity out into several independent copies (e.g. broadcast to several downstream stages) | DUPLICATE |
