# How do I work out an angle, a length or a count in an expression?

A layout figure often needs a little geometry: the angle of a ramp, its run on the floor, or how many pallets fit along it. The math and trigonometry functions do that inside an expression, so the figure follows the inputs. The script works out a 20 m ramp that rises 1.5 m and prints a few other shapes the same functions give.

```python
--8<-- "docs/snippets/recipe-ramp-geometry.py"
```

Save it as `recipe-ramp-geometry.py` and run it with `python`. Nothing here is random, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-ramp-geometry-output.txt"
```

## How it works

1. `ASIN`, `ATAN` and `TAN` work in radians; `DEG` and `RAD` convert, so the ramp's 4.3 degrees and 7.5 % grade agree.
2. `sqrt` gives the run on the floor, `ceil` rounds the pallet count up, and `log10` and `log(x, base)` count decades and doublings.
3. `TANH`, `SINH` and `COSH` give smooth S-curves and sags, for example a conveyor speed that eases in.

## Elements used

- Functions: [`ASIN`](../../reference/functions/trigonometry.md#asin), [`ATAN`](../../reference/functions/trigonometry.md#atan), [`TAN`](../../reference/functions/trigonometry.md#tan), [`TANH`](../../reference/functions/trigonometry.md#tanh), [`SINH`](../../reference/functions/trigonometry.md#sinh), [`COSH`](../../reference/functions/trigonometry.md#cosh), [`DEG`](../../reference/functions/trigonometry.md#deg), [`RAD`](../../reference/functions/trigonometry.md#rad), [`sqrt`](../../reference/functions/math.md#sqrt), [`ceil`](../../reference/functions/math.md#ceil), [`log`](../../reference/functions/math.md#log), [`log10`](../../reference/functions/math.md#log10).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
