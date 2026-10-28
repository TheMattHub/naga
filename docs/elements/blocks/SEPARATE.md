Use SEPARATE to split a batch made by [BATCH](batch.md) or [MATCH](match.md) back into its members: unpack a pallet, split a lot, free a truck's load. It differs from [DUPLICATE](duplicate.md), which makes new copies and does not need a batch.

## Behaviour and rules

The batch entity must carry members. An entity that is not a batch stops the run with an error. The batch leaves the work in progress and its members come back to it, one after the other in the same instant, along the first outgoing link. The `release_order` is `fifo` or `lifo`.

The batch's cost is split evenly across its members. The `mode` sets what a member takes from the batch. The default keeps its own values and records the batch in its `batch_info`. `Take all representative values` copies the batch's attributes. `Take specific representative values` copies only the ones listed in `attrs`.

With `carrier` set to `keep`, the batch is a vehicle that lives on, such as a truck. Its members leave on the first link one at a time, each after `member_time`. The carrier then leaves on the second link, which is required. Each member takes the cost it brought.
