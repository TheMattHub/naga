# How do I cost a model and find the cost drivers?

> **In short**
>
> - **You build:** a script that prices a call centre's day and splits the total into agents busy, agents idle and callers waiting. It then ranks the inputs by how far they move the total.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [sensitivity tutorial](sensitivity-and-doe.md).
> - **Time:** about ten minutes.

A total cost is only useful when you know what it is made of. By the end you put a cost on a [resource](../glossary.md#resource) and on every caller in the system, and read the split. Then you use a [tornado](../glossary.md#tornado) to see which input is the main [cost driver](../glossary.md#cost-driver).

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-cost.py`. It is the call centre of the optimisation tutorial with six agents. Read the two cost lines with care, because they use different time units.

```python
--8<-- "docs/snippets/goal-cost.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-cost.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-cost-output.txt"
```

3. Know the two units. `cost_busy` and `cost_idle` are quoted per `cost_time_unit`, which the script sets to hours. The [holding cost](../glossary.md#holding-cost) of an entity is per model [time unit](../glossary.md#time-unit), which is minutes here. It does not follow `cost_time_unit`. A holding cost of 0.5 therefore means 0.5 per minute, and 30 per hour.

4. Read the split. A day costs 2170. Agents working cost 769, agents waiting for a call cost 287, and callers in the system cost 1114, which is more than half. The waiting callers cost more than the agents do.

5. Read the hand check. The holding cost is the price per minute times the average number of callers in the system times the 480 minutes of the day. The average is 4.64 callers, and 0.5 times 4.64 times 480 is 1114, the same figure. If your own check does not match, a unit is wrong.

6. Read the tornado. Each row moves one input from its low to its high value with the others in the middle, sorted by the swing in total cost. The arrival rate moves the total most, by 351. The service rate follows with 325, then the busy cost with 227.

7. Read the last row. The number of agents moves the total by only 17. Another agent adds idle cost but removes waiting, and the two nearly cancel. The number of agents is a poor lever here, but the arrival and service rates are strong levers.

## Check yourself

- Predict, then run: if you set `holding_cost` to 0.25, will the callers' share of the day stay above half? Change it and check.
- Explain it back: why does adding an agent change the total so little when it changes the wait so much?

## Done when

- [ ] The script prints a three-line split, a total, a hand check and a four-row tornado.
- [ ] You can say which unit `holding_cost` is in, and which unit `cost_busy` is in.
- [ ] You can name the main cost driver and say what it tells you to measure better.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| The holding cost is 60 times too large or too small | `holding_cost` was read as per hour, or `cost_busy` as per minute | Holding is per model time unit. Resource costs are per `cost_time_unit`. Run the hand check. |
| The total has no holding line | The entity type has no `holding_cost`, or the entities have a different `e_type` | Give the CREATE block `e_type="Call"` for the entity type that has the cost. |
| `unknown section 'entities'` in the tornado | Entity settings are not override paths | Vary a resource, a block or a variable instead. |
| The ranking changes between runs | Five replications leave noise in the swings | Raise `replications`, and compare swings only when they differ clearly. |

## Elements this page uses

- Python: [`Model`](../reference/api/Model.md) with `add_resource` and `add_entity`, [`Results`](../reference/api/Results.md), and `Factor` and `tornado` from `nagasim.optimize`.
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- KPIs: `Busy Cost` and `Idle Cost` in the [resource summary](../reference/kpis.md#resource_summary), and `Total Entity Holding Cost` and `Total Cost` in the [system frame](../reference/kpis.md#system).
- Background: [Experiments](../experiments.md) covers searches over costs, and the [model file format](../model-file-format.md) lists every cost field.

Back to the [learning path](../learn/index.md#by-goal).
