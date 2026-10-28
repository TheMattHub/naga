These sections hold the values that expressions read. `variables` are numbers that a model changes while it runs. `tables` are fixed lookups by one or two keys. `timeseries_inputs` are profiles over time, such as a tariff or a demand curve. `entities` describe the entity types: how they look and what holding them costs.

## Behaviour and rules

A variable is read as a bare name in an expression and written by an ASSIGN block. Its `initial` value is restored at the start of every replication. A table is read with `TABLE("name", key)` or with two keys for a 2-D table. The `key_mode` decides what happens between keys: `exact`, `step` or `linear`. A missing exact key is a run-time error unless the table has a `default`.

A time series is read with `TS("name")` for now, or with a second argument for another time. Its `interpolation` is `step` by default, which is exact for a rate. Past the last sample, `beyond` decides whether the last value holds, the series repeats, or the run stops with an error. Sample times must increase.

An entity type is named by the `e_type` of a CREATE block. A `holding_cost` charges each entity for the time it spends in the system, from its creation to its disposal, and only time after the warm-up counts.
