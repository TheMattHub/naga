# How do I run arrivals from a fixed timetable?

A delivery plan or a roster lists the exact times things arrive. FixedSchedule takes that list. The script books five trucks and unloads them at one dock.

```python
--8<-- "docs/snippets/recipe-timetable-arrivals.py"
```

Save it as `recipe-timetable-arrivals.py` and run it with `python`. The script seeds itself, so the numbers below are the same on your machine.

```text
--8<-- "docs/snippets/recipe-timetable-arrivals-output.txt"
```

## How it works

1. A CREATE with a `FixedSchedule` makes one entity at each listed time, and none at any other time.
2. So `schedule=[10, 25, 30, 90, 150]` gives the five bookings at 10, 25, 30, 90 and 150.
3. A `start_time` drops the listed times before it, so `start_time=26` would leave the trucks at 30, 90 and 150.

## Elements used

- Distributions: [FixedSchedule](../../reference/distributions/fixed-schedule.md), [Constant](../../reference/distributions/constant.md).
- Blocks: [CREATE](../../reference/blocks/create.md), [PROCESS](../../reference/blocks/process.md), [DISPOSE](../../reference/blocks/dispose.md).
- Python: [`Model`](../../reference/api/Model.md).

Back to all [recipes](index.md).
