# From an event log to a model in five minutes

This page takes a real-looking event log from a job shop to a running model, and shows what
to check before trusting it. The log ships with the engine as `job_shop_log`: one 8-hour
shift of three stations, Cutting, Drilling and Assembly, written the way a shop's MES exports
it. By the end you have a draft model, a run of it, and a list of what the log could not tell.

## 1. Look at the log first

A draft is only as good as its log, so the first command reads the log without drafting
anything. `--check` says which columns it found, how many cases and events there are, what
share of the work has a start and a resource, and what each gap will mean for the model.

```console
naga discover job_shop_log --check
```

This log is "usable with care". It has starts, completions and a resource on every row, so
each station gets a service time and a resource. It has no `schedule` events, though, so it
never says when a job reached Cutting, only when Cutting started on it:

```text
  [warn] no schedule events: the wait before a case's first activity is not in the log: the case is taken to arrive when that activity starts, so the draft under-states the queue there
```

The check is cheap and changes nothing on disk. Run it on every new log before you draft.

## 2. Draft the model

One command writes the draft and its report. The draft has one CREATE fitted to the gaps
between jobs, one PROCESS per station with its fitted service time, the routing the log shows
(rework from Assembly back to Cutting included), and a resource per machine name.

```console
naga discover job_shop_log -o draft.json --report report.json
naga validate draft.json
```

Drilling is done by Drill1 and Drill2 in the log, so its PROCESS seizes a resource set of
the two. The draft validates as it is: it is an ordinary model file, ready to run and to edit.

## 3. Run it and compare

A draft earns trust when its run gives back the log's own numbers. The report keeps the
log's throughput and cycle time under `observed`. The log is one shift that began with an
empty shop, so the fair comparison is ten draft shifts of the same length, each turned into
the event log it would have written and measured the same way.

```python
import json
from statistics import mean
from nagasim import discover as d
from nagasim.api import run_model
from nagasim.core.trace import TraceRecorder

report = json.load(open("report.json"))
shifts = []
for seed in range(1, 11):
    trace = TraceRecorder()
    run_model("draft.json", replications=1, seed=seed, trace=trace, warmup=0)
    shifts.append(d.log_kpis(d.log_from_trace(trace, ready=False)))
print("throughput: log", round(report["observed"]["throughput"], 3),
      "draft", round(mean(s["throughput"] for s in shifts), 3))
cycles = sorted(round(s["cycle_time"], 1) for s in shifts)
print("cycle time: log", round(report["observed"]["cycle_time"], 1),
      "draft shifts", cycles[0], "to", cycles[-1])
```

Throughput agrees within about 2%. Cycle time is another matter: with the drills about 80%
busy, one shift's mean cycle time swings from about 5 to well over 10 minutes from one draft
shift to the next, and the log's 6.3 is a single draw of that. A log is one replication of
the real system, so judge the draft by whether the log falls inside its spread, and draft
from more shifts when the spread is too wide to decide.

## 4. Read the next steps

The report ends with two lists. "Next steps" says what to check in the draft, tailored to
this log; "what a log cannot tell" names what no log shows, whatever its quality.

```console
naga discover job_shop_log -o draft.json
```

For this log the next steps are to set each machine's real count (the draft's capacity is the
most the log ever saw busy at once, a lower bound), to read each DECIDE's chances, and to
time the queue in front of Cutting some other way. The second list is the same for every
log: shifts, priorities, batching, decisions made on a case's data, and which parallel steps
wait for each other. A modeller adds those by hand, and the draft gives them a place to go.

## Where to go next

The [event log page](event-log-to-model.md) has every column the reader understands, the
routing and fitting rules, and how accurate the drafts are on public real logs. Try your own
log with `--check` first.
