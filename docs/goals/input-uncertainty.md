# How sure am I, given my input data? Input uncertainty

> **In short**
>
> - **You build:** a script that reports a mean wait with two intervals, one from run-to-run noise and one that adds the [input uncertainty](../glossary.md#input-uncertainty) of a [distribution](../glossary.md#distribution) fitted to 30 observations.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [fitting tutorial](fit-input-distributions.md).
> - **Time:** about ten minutes.

A confidence interval from many [replications](../glossary.md#replication) only covers the random draws of the model. It does not cover the error in the data the inputs were fitted to, and more replications never shrink that error. By the end you measure it with a [bootstrap](../glossary.md#bootstrap), read the wider interval, and see what share of the spread comes from the inputs.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-uncertainty.py`. The 30 handling times are drawn from a seeded lognormal, as a stand-in for your own data. The model's service time is replaced by a distribution fitted to them.

```python
--8<-- "docs/snippets/goal-uncertainty.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-uncertainty.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-uncertainty-output.txt"
```

3. Read the first line. The engine fits the 30 observations once. Then it redraws the data 20 times with replacement, refits each draw in the same family, and runs the model five times on each fit. The 105 runs are 21 fits times five [replications](../glossary.md#replication). All of them use the same seeds, so the fits differ only in their inputs.

4. Read the interval from run-to-run noise. It is the usual 95% [confidence interval](../glossary.md#confidence-interval) over five replications of the point fit. It is what you would report if the fit were exact.

5. Read the interval with the inputs' uncertainty. It is wider, because it adds the spread of the 20 refits in quadrature. It always contains the first interval.

6. Read the share. It is the part of the spread of the mean that comes from the inputs. More replications shrink the rest of it. Only more data shrinks this part. A large share tells you to collect data before you run more replications.

## Check yourself

- Predict, then run: if the sample has 120 observations instead of 30 (`size=120`), will the input share rise or fall? Change it and check.
- Explain it back: why does the second interval contain the first?

## Done when

- [ ] The script prints a run count, a mean, two intervals and a share.
- [ ] You can say which of the two intervals you would report, and why.
- [ ] You can say what to do when the share is large.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `ValueError: at least 2 resamples` | `resamples` is below 2 | Pass `resamples=20` or more. A few dozen make the interval steadier. |
| `ModelValidationError: override path 'blocks.nope.params.distribution': no blocks entry named 'nope'` | The input's path names a block the model does not have | Build the path from the block object, as the script does with `serve.name`. |
| `ValueError` naming the number of runs and `at most 2000` | `(resamples + 1) x replications` is over the limit | Lower `resamples` or `replications`. The check runs before any run starts. |
| A note says the interval is rough | Fewer than 30 observations, or fewer than 20 usable resamples | Collect more data, or raise `resamples`. |

## Elements this page uses

- Python: `input_uncertainty` from `nagasim.uncertainty`, [`Model`](../reference/api/Model.md) and [`best_fit`](../reference/api/best_fit.md) for the fit it repeats.
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [Exponential](../reference/distributions/exponential.md).
- KPI: `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Command line: `naga uncertainty` runs the same analysis on a model file and a data file.

Next: [Calibrate a model to data](calibrate-to-data.md).
