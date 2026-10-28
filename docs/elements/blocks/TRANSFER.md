Use TRANSFER where a single entity moves a lump of a continuous level. A truck unloads 20 tonnes into a tank, an order takes 5 units from stock, or a delivery refills a store. It is the bridge between entities and levels. Rates that run continuously belong in flows, not here.

## Behaviour and rules

The `amount` is an expression, and a negative result counts as 0. In `remove` mode, the block takes the amount from the level down to its minimum. In `add` mode, it puts the amount in up to the level's capacity, or without a limit if it has none.

With `wait` off, the block moves what the level can pay for right now and goes on. The shortfall is recorded on the level as `unmet` for a removal and as `spilled` for an addition. With `wait` on, the entity queues until the level can pay the full amount, then takes it all at once. Waiting demands are served first in first out.

An amount the level never reaches waits until the end of the run, as in HOLD's `Infinite` mode. A level that is a conveyor cannot be written by TRANSFER. A level name that is not declared is caught by a check. The `level_summary` frame reports the totals.
