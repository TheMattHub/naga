# Step 2: Two servers and a schedule

Real servers are not always there, so this step adds a second desk that is closed for the first four hours and open for the last four. The two desks form a resource set, and the service block now seizes the set instead of the one server.

```python
--8<-- "docs/snippets/learn-2.py"
```

A schedule is a list of `[duration, value]` points, here the capacity `0` for 240 minutes and then `1` for 240 minutes. The set's `PreferredOrder` rule gives a customer the first free member in list order, so the second desk only takes work the first cannot. Only one line of step 1 changed, the one naming the resource the service block seizes.

It prints:

```text
--8<-- "docs/snippets/learn-2-output.txt"
```

The table shows the effect: the first desk serves most customers, the second desk is busy a third of the half day it is open, and the set's row weighs each desk by the hours it was open. The wait drops because capacity arrives when the queue is longest.

Next: [A tank level](3-a-tank-level.md).
