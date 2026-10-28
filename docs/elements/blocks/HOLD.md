Use HOLD to keep entities waiting until something outside the flow happens: a shift starts, a gate opens, a buffer has room, a machine is idle. In `Signal` mode, a [SIGNAL](signal.md) block releases them. In `Condition` mode, the entity waits until its own tests are true. `Infinite` mode parks entities for good.

## Behaviour and rules

All waiting entities sit in the block's store, so the `store_summary` frame reports how many wait and for how long. In `Signal` mode, the entity waits for the next broadcast of that signal name. A signal that fires before the entity arrives is missed. The signal is not remembered.

A signal releases every entity waiting on that name at that instant. They then leave one by one in the same instant. In `Condition` mode, all conditions must be true. An entity whose conditions are already true passes without waiting.

The block re-checks the conditions whenever something they read changes, such as a variable, a queue length, a resource state, a store or a level threshold. If a condition reads the clock, a time series or a random draw, nothing announces a change. The block then also re-checks every `scan_interval`.

In `Infinite` mode, the entity never leaves. It stays in the work in progress until the run ends. This is intended and not a leak.
