# How do I add a first-order information delay to a forecast?

A planner's forecast follows demand but with a lag, because it averages what has been seen. DELAY1 is that lag. The script raises demand at week 5, lets production follow a forecast that trails by 4 weeks, and shows the stock drain.

```python
--8<-- "docs/snippets/recipe-forecast-lag.py"
```

Save it as `recipe-forecast-lag.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-forecast-lag-output.txt"
```

## How it works

1. `DELAY1(x, tau)` moves its output towards `x` at a rate proportional to the gap, with time constant `tau`; it needs `simulation.sd`.
2. The forecast closes 63% of the gap in 4 weeks and 99% in 20, so production lags demand and the stock falls from 400 to 200.
3. The level and the two flows give the model a step grid to run on, and the auxiliaries are read from the `timeseries` frame.

## Elements used

- Functions: [`DELAY1`](../../reference/functions/system-dynamics.md#delay1), [`STEP`](../../reference/functions/system-dynamics.md#step), [`AUX`](../../reference/functions/model-state.md#aux).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
