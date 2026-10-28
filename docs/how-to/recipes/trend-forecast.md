# How do I extrapolate a growing trend?

Sales that grow steadily can be projected a few weeks ahead from their recent growth rate. TREND measures the rate and FORECAST applies it. The script grows sales by 5 a week and projects 5 weeks ahead.

```python
--8<-- "docs/snippets/recipe-trend-forecast.py"
```

Save it as `recipe-trend-forecast.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-trend-forecast-output.txt"
```

## How it works

1. `TREND(x, avg_time)` is the fractional growth of `x` against its smoothed value, and `FORECAST(x, avg_time, horizon)` extends `x` along it.
2. At week 20, sales are 199.75 and the forecast for week 25 is 227, against a true 225, so the projection is close but not exact.
3. The averaging time sets the lag of both functions; a shorter time reacts faster and is noisier.

## Elements used

- Functions: [`TREND`](../../reference/functions/system-dynamics.md#trend), [`FORECAST`](../../reference/functions/system-dynamics.md#forecast), [`RAMP`](../../reference/functions/system-dynamics.md#ramp).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
