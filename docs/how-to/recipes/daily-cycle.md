# How do I add a daily or weekly cycle to a rate?

Demand, solar output and many flows swing over a day and drop at weekends. SIN with PI makes the daily swing, and MODULO picks the days of the week. The script drives a tank pump with both.

```python
--8<-- "docs/snippets/recipe-daily-cycle.py"
```

Save it as `recipe-daily-cycle.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-daily-cycle-output.txt"
```

## How it works

1. `10 * (1 + 0.5 * SIN(2 * PI() * TNOW / 24))` swings 50% either way around 10 once every 24 hours.
2. `MODULO(TNOW / 24, 7) >= 5` is true on the sixth and seventh day, and the rate then falls to 20%.
3. A weekday gains about 230 m3 and a weekend day about 46, close to the 240 and 48 an exact integral gives.

## Elements used

- Functions: [`SIN`](../../reference/functions/trigonometry.md#sin), [`PI`](../../reference/functions/trigonometry.md#pi), [`MODULO`](../../reference/functions/math.md#modulo), [`IF`](../../reference/functions/math.md#if).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
