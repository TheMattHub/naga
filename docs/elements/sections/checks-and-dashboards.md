These sections guard and present a model. `assertions` check that a KPI stays on the right side of a bound. `path_checks` check which blocks entities visit. A `dashboard` defines the tiles, gauges and charts that a viewer sees, and the inputs a viewer may change. Use the first two as tests of a model, and the third for people who did not build it.

## Behaviour and rules

An assertion names a KPI key, an operator and a value. It passes only when the whole confidence interval over its `replications` clears the bound, at the given `confidence`. The operator `==` means inside the value plus or minus `tolerance`. `naga check` runs the assertions, and assertions with the same replication count run together.

A path check has one of four rules. `order` needs an entity to reach a first block before a later one. `never` forbids a block. `at_most` limits the visits. `always` requires that every entity that left through a DISPOSE visited the block. A path check is judged on one traced replication.

A dashboard widget is an expression sampled at the same instants as the time series. Without `pages`, the dashboard that Present mode shows is generated from the model. The `limits` cap what a run started by a viewer may cost.
