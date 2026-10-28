# How do I turn a heading and a distance into x and y?

A vehicle that flies a leg at a known heading ends up at an x and y offset, and you may need the heading back. RAD, COS and SIN go one way, and ATAN2 and DEG the other. The script flies 5 km at 53.13 degrees.

```python
--8<-- "docs/snippets/recipe-bearing.py"
```

Save it as `recipe-bearing.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-bearing-output.txt"
```

## How it works

1. `RAD` and `DEG` convert between degrees and radians, because `SIN` and `COS` work in radians.
2. `5 * COS(RAD(53.13))` and `5 * SIN(RAD(53.13))` give the 3 and 4 km offsets, and `ATAN2(y, x)` returns the heading.
3. `ATAN2` of the negated offsets is the way home, -126.87 degrees, which is 233.13 degrees counter-clockwise from east.

## Elements used

- Functions: [`SIN`](../../reference/functions/trigonometry.md#sin), [`COS`](../../reference/functions/trigonometry.md#cos), [`RAD`](../../reference/functions/trigonometry.md#rad), [`DEG`](../../reference/functions/trigonometry.md#deg), [`ATAN2`](../../reference/functions/trigonometry.md#atan2), [`ACOS`](../../reference/functions/trigonometry.md#acos).
- Model-file sections: [`auxiliaries`](../../reference/model-file/auxiliaries.md), [`levels`](../../reference/model-file/levels.md), [`flows`](../../reference/model-file/flows.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
