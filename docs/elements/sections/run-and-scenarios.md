These sections control how a model is run and compared. `simulation` sets the run length, the warm-up, the replications, the units and the optional step grid and calendar. `outputs` switches on extra collection. `scenarios` are named sets of parameter overrides. `reality_checks` test a stock-and-flow model against an input and a constraint.

## Behaviour and rules

The `time_unit` is the model's base unit. Every duration without its own unit is in it. Statistics are reset at `warmup`. Each of the `replications` is an independent run with its own seed. The `deterministic` mode samples every distribution at its mean and forces one replication. Without a `time_limit`, a run ends when the model drains, which only works for a model that can drain.

`record_logs` set to false empties the three event logs and keeps every summary exact. This is what a large study wants. The `outputs` options cost memory or time, so each is off by default.

A scenario is a list of overrides applied in order to the base model. An override is a path and a value, for example `resources.Server.capacity`. An override with a missing path or value is an error. The experiments run each scenario and compare them.
