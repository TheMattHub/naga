# Step 1: One queue

A queue with one server is the smallest model worth running, and the rest of this path grows it. Customers arrive about once a minute, a single server takes 0.8 minutes on average to serve one, and the model runs for one eight-hour day.

```python
--8<-- "docs/snippets/learn-1.py"
```

`nagasim.build` writes a model file from Python: `m.create` is the source of arrivals, `m.process` is the service, `a >> b` links two blocks, and `m.dispose()` is where customers leave. Times are exponential, so this is the textbook M/M/1 queue, and its mean wait is known exactly: 3.2 minutes. The numbers are in minutes because the model's time unit is the default.

It prints:

```text
--8<-- "docs/snippets/learn-1-output.txt"
```

One run is one possible day. The mean wait it printed is close to the exact value but it is not an interval, which is why step 5 runs the day twenty times. The next step keeps this model and adds capacity that follows a schedule.

Next: [Two servers and a schedule](2-two-servers-and-a-schedule.md).
