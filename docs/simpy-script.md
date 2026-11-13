# From a SimPy script

A SimPy model you already have can become a Naga model without a rewrite. You add a few calls to your own script so it writes an event log, run it as you always do, and let `naga discover` draft the model from that log. Naga never runs your script: it only reads the log the script wrote.

## Add the helper

The helper is one standard-library file, `simpylog.py`, shipped inside the package as `nagasim/simpylog.py`. You can import it as `nagasim.simpylog`, or copy that file from the installed package next to your script and import it as `simpylog`. Call `ready` when an order reaches a step, `start` when it gets the machine, and `complete` when it leaves. Without `ready` the log has no queue waits, and the draft would see a process that never queues. Orders must also arrive over time: if every case starts at the same instant, `naga discover` has no arrival rate to fit and stops.

```python title="my_script.py"
import random

import simpy
from nagasim.simpylog import EventLog

def order(env, name, machine, log):
    log.ready(name, "Pack", env.now)
    with machine.request() as req:
        yield req
        log.start(name, "Pack", env.now, resource="Pack1")
        yield env.timeout(random.expovariate(1 / 2.0))
        log.complete(name, "Pack", env.now, resource="Pack1")

def source(env, machine, log):
    for i in range(100):
        env.process(order(env, f"o{i}", machine, log))
        yield env.timeout(random.expovariate(1 / 3.0))

random.seed(1)
env = simpy.Environment()
machine = simpy.Resource(env, 1)
with EventLog("log.csv") as log:
    env.process(source(env, machine, log))
    env.run()
```

## Draft and check the model

Run your script, then give `naga discover` the log and the unit your script counts in (`env.now` is written as it is). Compare the draft with the log before you trust it: the `check_draft` agent tool runs the draft and reports whether its cycle time, throughput and service times match the log. A log of fewer than 30 cases can only give a partial verdict.

```console
python my_script.py
naga discover log.csv --time-unit minutes -o draft.json --report report.json
naga validate draft.json
```

The draft is a starting point built from one run of one script. Read its report for what was inferred and what was assumed, as in [Event log to model](event-log-to-model.md).
