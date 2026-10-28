Use ASSIGN to stamp data on an entity or to set a model variable as the entity passes. Typical jobs are a due date, a priority, a product type, a counter or a flag that a later DECIDE reads. It takes no simulated time. To measure something without changing the model, use [RECORD](record.md). To change a stock, use [TRANSFER](transfer.md).

## Behaviour and rules

Assignments run in the listed order, so a later one sees what an earlier one wrote. A target `entity.<name>` sets an attribute of the entity. A target `entity.agent.<name>` writes a column of the entity's agent. Any other name sets a model variable, which must be declared in `variables`.

Assigning `entity.sequence` restarts the entity's routing at step 0. Writing a variable wakes any [HOLD](hold.md) that waits on a condition reading it, and updates the levels and agents that depend on it.

A bad target or expression raises an error that names the block and the assignment. The error appears when the first entity reaches the block, not when the file loads, unless a check of the model catches it earlier.
