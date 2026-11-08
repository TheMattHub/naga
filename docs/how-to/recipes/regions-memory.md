# How do I total a subscripted value and remember a past one?

A model with regions needs their total, their best and their worst, and a planner often needs a value from the past: the opening price, the stock at the last stock-take, or goods sent four days ago. The array functions fold a subscripted value, and the memory functions keep or delay one. The script runs three regions for 20 days and prints each answer on day 0 and on day 20.

```python
--8<-- "docs/snippets/recipe-regions-memory.py"
```

Save it as `recipe-regions-memory.py` and run it with `python`. Nothing here is random, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-regions-memory-output.txt"
```

## How it works

1. `SUM`, `PROD`, `VMAX` and `VMIN` fold `AUX("sales[*]")` over every region of the subscript.
2. `INITIAL` holds day 0's value, `SAMPLE_IF_TRUE` keeps the last value read while its condition held, and `DELAY` reads a named delay's output.
3. `ACTIVE_INITIAL` gives one value while initial values are set up and another while running; `TABLE_EXTRAPOLATE` continues a table's last slope.

## Elements used

- Functions: [`SUM`](../../reference/functions/arrays.md#sum), [`PROD`](../../reference/functions/arrays.md#prod), [`VMAX`](../../reference/functions/arrays.md#vmax), [`VMIN`](../../reference/functions/arrays.md#vmin), [`INITIAL`](../../reference/functions/system-dynamics.md#initial), [`SAMPLE_IF_TRUE`](../../reference/functions/system-dynamics.md#sample_if_true), [`ACTIVE_INITIAL`](../../reference/functions/system-dynamics.md#active_initial), [`DELAY`](../../reference/functions/model-state.md#delay), [`TABLE_EXTRAPOLATE`](../../reference/functions/model-state.md#table_extrapolate), [`LEVEL`](../../reference/functions/model-state.md#level), [`AUX`](../../reference/functions/model-state.md#aux).
- Model-file sections: [`subscripts`](../../reference/model-file/subscripts.md), [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`delays`](../../reference/model-file/delays.md), [`tables`](../../reference/model-file/tables.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
