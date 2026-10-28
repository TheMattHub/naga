# How do I test a model with a step, a ramp and a pulse?

Before you trust a model, push a known input through it and see how it answers. STEP, RAMP and PULSE are the three standard inputs. The script feeds each into its own tank that drains 10% an hour.

```python
--8<-- "docs/snippets/recipe-test-inputs.py"
```

Save it as `recipe-test-inputs.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-test-inputs-output.txt"
```

## How it works

1. `STEP(h, t0)` jumps to `h` at `t0`, `RAMP(s, t0)` climbs by `s` a unit of time from `t0`, and `PULSE(h, t0, w)` holds `h` for `w`.
2. The step settles towards 50, the ramp keeps climbing, and the pulse rises, then drains back towards zero.
3. If a response does not look like that, the structure is wrong, and not the data.

## Elements used

- Functions: [`STEP`](../../reference/functions/system-dynamics.md#step), [`RAMP`](../../reference/functions/system-dynamics.md#ramp), [`PULSE`](../../reference/functions/system-dynamics.md#pulse).
- Model-file sections: [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
