# Which decision is best? Optimise it

> **In short**
>
> - **You build:** a script that searches for the number of call-centre agents with the lowest daily cost.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [replications tutorial](replications-and-warm-up.md).
> - **Time:** about ten minutes.

An [objective](../glossary.md#objective) such as cost has a best value over your [decision variables](../glossary.md#decision-variable), and the engine can search for it. By the end you describe one decision as a [factor](../glossary.md#factor), give the search a [budget](../glossary.md#budget) of [replications](../glossary.md#replication), and read which choice won and what the search tried.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-optimize.py`. It models a call centre where two calls arrive every minute and an agent takes two minutes per call. Agents cost 20 an hour whether busy or idle, and every caller in the system costs 0.5 a minute. More agents cost more wages and fewer waiting callers, so the total cost has a lowest point.

```python
--8<-- "docs/snippets/goal-optimize.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-optimize.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-optimize-output.txt"
```

3. Read the first line. It names the winner and its mean cost. The search confirms its winner with ranking and selection, a final comparison of the best few points. That step gives the winner more [replications](../glossary.md#replication) than the table shows, so its mean differs a little from the table row.

4. Read the table. Each row is one number of agents the search tried, with the mean cost over `N` replications. Cost falls as agents are added up to the winner and rises after it. The offered load is four agents' work, so five agents are busy 80% of the time. Callers queue, and holding cost dominates.

5. Look at how the decision was described. `Factor` names the [override path](../glossary.md#override-path) of the value to change, its kind (`integer`), and its low and high bound. Every point runs on the same seeds, so the difference between two points is the effect of the decision and not of luck. See [common random numbers](../glossary.md#common-random-numbers).

6. Treat the answer as advice for this model. The search never changes the model file. To keep the result, add it as a [scenario](../glossary.md#scenario) or set the value yourself.

## Check yourself

- Predict, then run: if a caller in the system cost 1.0 a minute instead of 0.5, would the best number of agents be lower, the same or higher? Change `holding_cost` and compare.
- Explain it back: why can the search not simply choose the largest number of agents?

## Done when

- [ ] The script prints a best number of agents and a table with one row per number tried.
- [ ] You can say what `budget` limits and why the winner has more replications than the table shows.
- [ ] You can say what a search does not change in your model.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `ModelValidationError: override path 'resources.Agent.capacity': no resources entry named 'Agent'` | The factor's path names something the model does not have | Copy the name from the model. Here the resource is `Agents`. |
| `ValueError: unknown response KPI: 'system/Nope'` | The response key is misspelled | Copy the key from the [KPI reference](../reference/kpis.md). |
| `ValueError: factor 'a': high 1.0 is below low 3.0` | The bounds are the wrong way round | Swap `low` and `high`. |
| The table has fewer rows than the range | The budget ran out before every value was tried, or the search did not need them | Raise `budget` to try more points. A search is not an exhaustive sweep. |

## Elements this page uses

- Python: [`optimize`](../reference/api/optimize.md), with its `Factor` class, and [`Model`](../reference/api/Model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [Exponential](../reference/distributions/exponential.md).
- KPI: `Total Cost` in the [system summary](../reference/kpis.md#system).
- Background: [Experiments](../experiments.md#optimization) explains the search method and its guarantee.

Next: [Sensitivity and design of experiments](sensitivity-and-doe.md).
