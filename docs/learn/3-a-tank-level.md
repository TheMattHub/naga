# Step 3: A tank level

A tank fills and drains continuously, which a queue model cannot say, so this step puts one in the same model. The tank starts at 20 litres, fills at 5 litres a minute and drains at 2, and a watcher customer waits for the moment it reaches 50 litres.

```python
--8<-- "docs/snippets/learn-3.py"
```

A level has a capacity and thresholds, and a threshold fires a named signal when the level crosses its value. The watcher is an entity that waits on the signal with a HOLD block and then leaves, so the time it spent in the model is the instant the tank crossed. The engine solves that instant exactly instead of stepping a clock, which is why the printed time is 10.0 and not a value near it.

It prints:

```text
--8<-- "docs/snippets/learn-3-output.txt"
```

The last line works the answer out by hand: 30 litres to go at a net 3 litres a minute is 10 minutes. The queue, the schedule and the tank all run on one clock in one model. The next step adds a fourth kind of component, agents.

Next: [Agents](4-agents.md).
