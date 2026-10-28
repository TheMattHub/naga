# Step 5: Replications and scenarios

One run is one sample, so a claim about the model needs many runs and a comparison needs the same random numbers on both sides. This step compares the model with a version whose service is faster, using twenty replications each.

```python
--8<-- "docs/snippets/learn-5.py"
```

A scenario is a name and a list of overrides, each a path into the model and a new value. `run_scenarios` runs the baseline and every scenario on common random numbers: the same random draws on each side. A difference is then the effect of the change and not of luck. It then compares each scenario with the baseline by a paired test. The `comparison` table has the mean of each, the difference, and whether that difference is significant at the 95% level.

It prints:

```text
--8<-- "docs/snippets/learn-5-output.txt"
```

Faster service lowers the mean wait, and the paired test calls the difference significant. This is the answer a decision needs: a number, a test of whether it is more than luck, and the comparison made fairly. The last step reads the rest of what a run returns.

Next: [Reading the results and the event log](6-results.md).
