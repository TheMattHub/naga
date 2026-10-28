# How do I fit input distributions from data?

> **In short**
>
> - **You build:** a script that fits candidate [distributions](../glossary.md#distribution) to 200 handling times, picks the winner, and shows what the fit changes in a [queue](../glossary.md#queue).
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the one-queue model from [step 1 of the learning path](../learn/1-one-queue.md).
> - **Time:** about ten minutes.

A model is only as good as its input distributions, and an exponential is a poor default for most real service times. By the end you run a [distribution fit](../glossary.md#distribution-fit), read its [AIC](../glossary.md#aic) and [KS test](../glossary.md#ks-test), and put the winner into a model as a distribution.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-fit.py`. The 200 handling times are drawn from a seeded lognormal, so the page can print the same numbers every time. This is a stand-in for your own data. To use a file, replace the line with `inputfit.load_column("times.csv").values`.

```python
--8<-- "docs/snippets/goal-fit.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-fit.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-fit-output.txt"
```

3. Read the first three lines. `fit_candidates` fits every family that suits the data and ranks them by AIC. A lower AIC is a better fit after the penalty for extra parameters. The parameters are in the engine's own naming, so you can paste them into a model.

4. Compare the first two lines. The top two families differ by less than one AIC point, which is no real difference. Pick the one you can explain to your reader. The lognormal is the usual choice for handling times, and the data were drawn from one.

5. Read the KS p-value. A high value means the sample does not contradict the fit. It is optimistic here, because the parameters were estimated from the same sample, so use it to reject a poor fit and not to prove a good one.

6. Read the last two lines. `best_fit` returns the winner, and `nb.Distribution(name, params)` turns it into a distribution the builder accepts. The same queue with an exponential of the same mean waits longer, because an exponential has more spread than these data. Matching the mean is not enough.

## Check yourself

- Predict, then run: if you pass `candidates=["Exponential", "Normal"]` to `fit_candidates`, which family wins and what happens to the AIC? Try it.
- Explain it back: why is a high KS p-value not proof that the fit is right?

## Done when

- [ ] The script prints three ranked fits, the winner and two mean waits.
- [ ] You can say why two fits within one AIC point are a tie.
- [ ] You can say why matching only the mean of an input can mislead a result.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `best_fit` returns `None` | No family can be fit to the data, for instance when all values are equal | Look at the data with `inputfit.describe(values)`. Use an empirical distribution if no family fits. |
| A fit of one or two values returns a strange family | There is too little data to tell families apart. One value gave `Geometric` with `p` equal to 1.0 | Collect more data. Under 30 observations, treat the choice as rough. |
| The winner is a family you cannot explain | AIC ranks fit and not meaning | Choose from the top few the one that matches the process, and say so. |
| `KeyError: column 'x' not found` | The column name is not in the file | The message lists the available columns. Pick one of them. |

## Elements this page uses

- Python: [`best_fit`](../reference/api/best_fit.md) and `fit_candidates` from `nagasim.inputfit`, and [`Model`](../reference/api/Model.md).
- Distributions: [InverseGaussian](../reference/distributions/inverse-gaussian.md), [Lognormal](../reference/distributions/lognormal.md), [Erlang](../reference/distributions/erlang.md) and [Exponential](../reference/distributions/exponential.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Background: the [Python API guide](../python-api.md) shows where fitting sits among the entry points.

Next: [Run with input uncertainty](input-uncertainty.md).
