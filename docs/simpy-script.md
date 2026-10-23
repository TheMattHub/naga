# From a SimPy script

A SimPy model you already have can become a Naga model without a rewrite. You add a few calls to your own script so it writes an event log, run it as you always do, and let `naga discover` draft the model from that log. Naga never runs your script: it only reads the log the script wrote.

## Add the helper

`simpylog.py` is one standard-library file, so you can copy it next to your script. Call `ready` when an order reaches a step, `start` when it gets the machine, and `complete` when it leaves. Without `ready` the log has no queue waits, and the draft would see a process that never queues.

<!-- no-run: this is your own SimPy script -->
```python
import simpy
from simpylog import EventLog

def order(env, name, machine, log):
    log.ready(name, "Pack", env.now)
    with machine.request() as req:
        yield req
        log.start(name, "Pack", env.now, resource="Pack1")
        yield env.timeout(2.0)
        log.complete(name, "Pack", env.now, resource="Pack1")

env = simpy.Environment()
machine = simpy.Resource(env, 1)
with EventLog("log.csv") as log:
    for i in range(100):
        env.process(order(env, f"o{i}", machine, log))
    env.run()
```

## Draft and check the model

Run your script, then give `naga discover` the log and the unit your script counts in (`env.now` is written as it is). Compare the draft with the log before you trust it: the `check_draft` agent tool runs the draft and reports whether its cycle time, throughput and service times match the log. A log of fewer than 30 cases can only give a partial verdict.

<!-- no-run: needs the log your script wrote -->
```console
python my_script.py
naga discover log.csv --time-unit minutes -o draft.json --report report.json
naga validate draft.json
```

The draft is a starting point built from one run of one script. Read its report for what was inferred and what was assumed, as in [Event log to model](event-log-to-model.md).
