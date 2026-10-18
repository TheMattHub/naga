# Step 6: Reading the results and the event log

Every run returns more than the headline number: summaries for each kind of component, and a log of what happened. This step runs the model twenty times and reads both.

```python
--8<-- "docs/snippets/learn-6.py"
```

`results.kpis()` gives each metric's mean over the replications with the half-width of its 95% interval, and a metric's key is `family/name` or `family/object/name`, the same keys scenarios and optimisation use. The frames `entity_log` and `resource_log` hold one row for each customer and for each request, seize and release of a resource: the event log from which every summary was computed. The log is a pandas frame, so any question about a run can be answered from it.

It prints:

```text
--8<-- "docs/snippets/learn-6-output.txt"
```

From here the [KPI reference](../reference/kpis.md) lists every metric and frame, the [Python API](../python-api.md) shows optimisation and calibration, and the [modelling guide](../modelling-guide/index.md) explains the parts of a model one by one. To start from the log of a real plant instead of a blank model, see [an event log to a model](../event-log-tutorial.md).
