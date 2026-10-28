# Which inputs matter? Sensitivity and design of experiments

> **In short**
>
> - **You build:** a script that ranks three inputs of a call centre by how far they move the mean wait. It then runs a [full factorial](../glossary.md#full-factorial) design to see their [effects](../glossary.md#main-effect) together.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [replications tutorial](replications-and-warm-up.md).
> - **Time:** about fifteen minutes.

A model has many inputs, and an experiment on all of them at once is slow. By the end you rank the inputs with a [tornado](../glossary.md#tornado) table, run a [DOE](../glossary.md#doe) over the three that matter, and read each [main effect](../glossary.md#main-effect) and the signs of an [interaction](../glossary.md#interaction).

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-doe.py`. It models a call centre with six agents. It names three [factors](../glossary.md#factor): the number of agents, the arrival rate and the service rate. The [response](../glossary.md#response) is the mean wait of a caller.

```python
--8<-- "docs/snippets/goal-doe.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-doe.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-doe-output.txt"
```

3. Read the first table, the tornado. Each factor moves alone from its low to its high value while the other two stay in the middle of their ranges. `Swing` is the distance between the two results. The table is sorted by swing, so the first row is the input to look at first.

4. Read the second table. A full factorial runs every combination of the low and high value of each factor, so three factors give eight points. Every point runs on the same seeds, so differences between points are not luck. Point 2 has few agents, a high arrival rate and a slow service, and it waits far longer than the rest.

5. Read the third table. For each factor it gives the mean wait at its low and high level, taken over all points, and the `Effect`, the change between the two levels. `Effect LB` and `Effect UB` are the ends of a 95% interval on that change. When the interval holds zero, five replications per point cannot separate the effect from noise.

6. Compare the tables. The tornado changes one input at a time and cannot see that agents matter most when arrivals are high and service is slow. The factorial can, because point 2 sits far above the others. An average over all points, the main effect, spreads that one bad corner over every row. That is what an interaction looks like.

## Check yourself

- Predict, then run: if you raise `replications` from 5 to 20, will the effect intervals get narrower or wider? Check `Effect LB` and `Effect UB`.
- Explain it back: why does the tornado rank agents first while the main-effect interval for agents still holds zero?

## Done when

- [ ] The script prints a tornado table with three rows, a design table with eight points and a main-effect table with six rows.
- [ ] You can say what `Swing` measures and what it does not test.
- [ ] You can point at the design row that shows an interaction.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `ModelValidationError: override path 'blocks.nope.params.distribution': no blocks entry named 'nope'` | The block name in a factor's path is not one the model has | Build the path from the block object, as the script does with `arrive.name`. |
| `ValueError: unknown response KPI` | The response key is misspelled | Copy the key from the [KPI reference](../reference/kpis.md). |
| The design has fewer points than you expect | An integer factor with a narrow range has fewer distinct levels than you asked for | Widen the range. An integer factor from 3 to 4 has two levels however many you ask for. |
| Every effect interval holds zero | Too few replications for the noise | Raise `replications`, or use fewer and wider factors. |

## Elements this page uses

- Python: `run_design`, `full_factorial` and `tornado` from `nagasim.optimize` (its entry point [`optimize`](../reference/api/optimize.md) is on the previous page), and [`Model`](../reference/api/Model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [Exponential](../reference/distributions/exponential.md).
- KPI: `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Background: [Experiments](../experiments.md#design-of-experiments) covers the three design types and the command line (`naga doe`).

Next: [Fit input distributions from data](fit-input-distributions.md).
