# How many replications do I need, and what warm-up?

> **In short**
>
> - **You build:** a script that finds how many [replications](../glossary.md#replication) a target [precision](../glossary.md#precision) needs, and how long a [warm-up](../glossary.md#warm-up) to set.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the model from [step 1 of the learning path](../learn/1-one-queue.md), a single [queue](../glossary.md#queue).
> - **Time:** about ten minutes.

One run of a model is one sample, so a result needs many runs and a stated precision. By the end you ask the engine for the replications a precision target needs. You read the [half-width](../glossary.md#half-width) it reaches. You set a warm-up from the data instead of a guess.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-replications.py`. It builds the one-queue model, asks for a precision, asks for a warm-up, and asks for the precision again with that warm-up.

```python
--8<-- "docs/snippets/goal-replications.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-replications.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-replications-output.txt"
```

3. Read the first line. `run_until_precision` runs five [replications](../glossary.md#replication) first. Then it estimates how many more it needs so that the [half-width](../glossary.md#half-width) of the [confidence interval](../glossary.md#confidence-interval) is within 10% of the mean. It keeps adding until the target is met or the cap of 200 is reached.

4. Read the second line. The model starts empty, so the first customers wait less than they would in a busy hour. `suggest_warmup` applies [MSER-5](../glossary.md#mser-5) to 50 longer runs and returns the time after which the start-up bias is gone. `reliable: True` means the answer fell in the first half of the run, so you can trust it.

5. Read the third line. The warm-up discards the empty start of each run, so every replication now measures a busy system. Here the same precision needs more replications than before, because each one now keeps less of the day.

6. Compare both means with the exact value. This queue is an M/M/1 queue, and its exact mean wait is 3.2 minutes. Both intervals hold 3.2, which is what a 95% interval should do about nineteen times in twenty.

## Check yourself

- Predict, then run: halving the target to `relative=0.05` should need about four times the replications, because the half-width shrinks with the square root of the count. Change it and compare with the cap. `report["met"]` tells you whether the cap stopped the run.
- Explain it back: why do you set the warm-up from long runs and then apply it to the short day?

## Done when

- [ ] The script prints three lines and the first says `target met: True`.
- [ ] You can say what `reliable` means and what you do when it is `False`.
- [ ] You can say why a warm-up needs more replications for the same precision.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'nagasim'` | The package is not installed in the Python you ran | Run `pip install nagasim` with the same interpreter, then run the script again. |
| `ValueError: unknown KPI` | The KPI key is misspelled | Copy the key from the [KPI reference](../reference/kpis.md). It has the form `resource/Server/Time in Queue Avg`. |
| `target met: False` and the count equals the cap | The target needs more than `max_replications` | Relax `relative`, or pass a higher `max_replications`. Say which you chose when you report the result. |
| `reliable: False` | The suggested warm-up fell in the second half of the run, so the run is too short to tell | Make the runs longer or add replications, then ask again. Do not use the suggestion as it stands. |

## Elements this page uses

- Python: [`run_until_precision`](../reference/api/run_until_precision.md), [`suggest_warmup`](../reference/api/suggest_warmup.md) and [`run_model`](../reference/api/run_model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [Exponential](../reference/distributions/exponential.md).
- KPI: `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Background: [Experiments](../experiments.md#warm-up-and-the-mser-suggestion) explains MSER-5 in full.

Next: [Optimise a decision](optimise-a-decision.md).
