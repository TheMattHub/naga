# How do I calibrate a model to data?

> **In short**
>
> - **You build:** a script that tunes the [service rate](../glossary.md#service-rate) of a one-server [queue](../glossary.md#queue) until the model's mean wait matches a measured 3.2 minutes.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [replications tutorial](replications-and-warm-up.md).
> - **Time:** about ten minutes.

A model is credible when it reproduces what the real system did. By the end you give a [calibration](../glossary.md#calibration) one [calibration target](../glossary.md#calibration-target) and one parameter to tune. Then you read how well the tuned model matches, and ask [ABC](../glossary.md#abc) which values the data allow.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-calibrate.py`. The model starts with a wrong guess of 1.0 for the service rate. The real value, which the script does not know, is 1.25 per minute, so a mean wait of 3.2 minutes is what the real system would show.

```python
--8<-- "docs/snippets/goal-calibrate.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-calibrate.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-calibrate-output.txt"
```

3. Read the first line. `calibrate` searches the service rate between 0.9 and 2.0 and spends at most 80 replications. It found 1.26, close to the real 1.25.

4. Read the second line. It compares the tuned model with your target. A target with a `tolerance` passes when the simulated mean is within that distance of the observed value. This is a verdict on the mean, so check the confidence interval in `fit.table` too, because a wide interval makes a pass weak.

5. Read the third line. `fit.as_scenario()` turns the tuned value into a [scenario](../glossary.md#scenario) with an [override path](../glossary.md#override-path). Add it to the model to keep the result.

6. Read the last line. `method="abc"` draws many parameter values and keeps the ones whose results lie nearest the target. The result is an interval, not one number. Here the data allow a service rate from 1.19 to 1.36, so the single best value hides how loosely one mean wait pins the rate.

## Check yourself

- Predict, then run: if you add a second target, `observed` utilisation of 80 per cent for `resource/Server/Usage % (snapshot)`, will the ABC interval get wider or narrower? Try it.
- Explain it back: why does the tuned wait differ a little from 3.2 even though the rate is almost exact?

## Done when

- [ ] The script prints a tuned rate, a pass or fail line, a scenario and an interval.
- [ ] You can say what the interval means that the best value alone does not.
- [ ] You can say why one target rarely pins down more than one parameter.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `pass: False` although the rate is close | The simulated mean is noisy at 5 replications, so it can land outside the tolerance | Raise `replications_per_point` or `run_length`, or widen `tolerance` to what you can measure. |
| The best value sits at 0.9 or 2.0 | The true value is outside the range, or the target cannot be reached | Widen the factor range, and check that the observed number comes from the same unit and period as the KPI. |
| `ValueError` naming a KPI | The KPI key is misspelt | The message lists close matches. The [KPI reference](../reference/kpis.md) has every key. |
| The ABC interval fills the whole range | One target says little about this parameter | Add targets of other kinds, such as utilisation, or a sample to match as a distribution. |

## Elements this page uses

- Python: [`calibrate`](../reference/api/calibrate.md) from `nagasim.calibrate`, `Factor` from `nagasim.optimize`, and [`Model`](../reference/api/Model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [Exponential](../reference/distributions/exponential.md).
- KPI: `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Command line: [`naga calibrate`](../reference/cli/twin.md#naga-calibrate) runs the same search on a model file.
- Background: [Digital twin](../digital-twin.md) uses calibration to keep a model matched to a plant.

Next: [Connect live data end to end](connect-live-data.md).
