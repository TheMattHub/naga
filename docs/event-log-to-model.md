# Event log to model

Most plants and offices already record what happened: which order went through which step,
when it started and finished, and who did it. `naga discover` reads that event log and writes
a draft model from it, with a report of what it inferred, what it assumed and what it left out.
The draft validates and runs, so the first model of a process can come from its own history
instead of a blank file. New to it? [From an event log to a model in five
minutes](event-log-tutorial.md) walks through the bundled sample log, `job_shop_log`.

## Draft a model

A log is one row per event: the case, the activity, an instant and, when the log has them,
the lifecycle (`schedule`, `start`, `complete`) and the resource.

```csv title="orders.csv"
case,activity,timestamp,lifecycle,resource
o1,Pick,2026-10-16T08:00:00,start,Ann
o1,Pick,2026-10-16T08:04:00,complete,Ann
o1,Pack,2026-10-16T08:05:00,start,Bob
o1,Pack,2026-10-16T08:07:30,complete,Bob
o2,Pick,2026-10-16T08:03:00,start,Cy
o2,Pick,2026-10-16T08:08:00,complete,Cy
o2,Pack,2026-10-16T08:08:00,start,Bob
o2,Pack,2026-10-16T08:11:00,complete,Bob
o3,Pick,2026-10-16T08:06:00,start,Ann
o3,Pick,2026-10-16T08:09:00,complete,Ann
o3,Pack,2026-10-16T08:12:00,start,Bob
o3,Pack,2026-10-16T08:14:00,complete,Bob
o4,Pick,2026-10-16T08:10:00,start,Cy
o4,Pick,2026-10-16T08:15:00,complete,Cy
o4,Pack,2026-10-16T08:15:00,start,Bob
o4,Pack,2026-10-16T08:18:00,complete,Bob
```

```console
naga discover orders.csv -o draft.json --report report.json
naga validate draft.json
naga run draft.json
```

The draft has one CREATE fitted to the gaps between case arrivals, one block per activity
with its time fitted by the engine's own distribution fitting (`naga fit`), the routing the
log shows, and a resource per name in the resource column. Here Pick is done by Ann and Cy,
so it seizes a resource set of the two; Pack by Bob alone. The draft is in `naga fmt` form,
and `naga export --python draft.json` turns it into builder code.

## What the log can tell

| The log has | The draft gets |
|---|---|
| start and complete | service time (complete - start) and the wait before it |
| schedule too | the wait before a case's first activity, and transfer times between activities |
| complete only | the time since the case's previous event: a DELAY with no resource |
| a resource column | the resources, each with the most it ever had in service at once |
| one row per instance, with start and complete columns | the same as start and complete |

Columns are found by their usual names (`case`, `case:concept:name`, `activity`,
`concept:name`, `timestamp`, `time:timestamp`, `lifecycle:transition`, `org:resource`, `start`,
`end`, ...); `--case`, `--activity`, `--timestamp` and the like name any other. XES files
(`.xes`) are read too. An instant is ISO 8601 text or a number; `--time-unit` sets the draft's
unit (minutes by default), and a number is taken as already in it.

**Routing.** By default the draft routes by chance: after each activity, a DECIDE to the
activities that followed it in the log, rework loops included. When the next activity clearly
depends on the one before (a test on the log), and a few variants cover the cases, the draft
routes each variant as its own sequence instead (a ROUTE and STATION blocks). `--routing
markov` or `--routing variants` forces one.

## Is my log good enough?

`naga discover LOG --check` reads the log and writes no model. It names the columns it
found, counts cases, events, activities and resources, gives the time span, the share of
activity instances with a start, a schedule and a resource, and the cases that may have been
cut by the log's ends. Each finding comes with what it means for a draft, and a verdict:
"good", "usable with care" or "weak".

```console
naga discover job_shop_log --check
```

The findings are the ones that move a draft's numbers: too few cases, complete events only,
no `schedule` events, no resource, a log spanning few cycle times, cases running at its start
or end, arrivals that drift over the log, crowd into working hours or into weekdays, work
done in shifts, cases arriving in batches, overlapping activities, and rows dropped. The same
check heads every report, so a draft always says what its log was like.

## Next steps

The report ends with two lists. `next_steps.check` is what to check in the draft: compare its
run with the observed KPIs, set the real resource capacities, read the routing chances, and
one step for each finding of the check (time the first queue, add a schedule to the arrivals,
add the shifts, and so on). `next_steps.never` is what a log cannot tell: shift patterns,
priorities, batching rules, decisions made on a case's data, and which parallel steps wait for
each other, each worded for what this log showed.

## Check a draft against its log

`log_from_trace` turns a run into the event log it would have written, and `log_kpis` gives a
log's throughput, cycle time and per-activity service and wait, so a draft can be checked
against the log it came from on the same numbers.

```python
from nagasim import discover as d
from nagasim.api import run_model
from nagasim.core.trace import TraceRecorder

trace = TraceRecorder()
run_model("model.json", replications=1, trace=trace, run_length=500, warmup=0)
log = d.log_from_trace(trace)
layout, report = d.discover(log)
print(report["routing"]["mode"], d.log_kpis(log)["throughput"])
```

## How accurate is it?

On a log written by one of our own models, a draft gives back throughput within 0.5%, cycle
time within 3.5%, service within 1% and waits within about 6% (the round trip in the tests). Real
logs are harder, so three public ones were tried: draft from the first half of the cases by
arrival, run the draft, and compare it with the second half. The first half against the
second is the log's own drift, the floor no draft can beat. Each cell is the draft against
the second half, with the drift in brackets.

| Log | Drafted from | Throughput | Cycle time | Service | Wait |
|---|---|---|---|---|---|
| Sepsis Cases (a hospital) | whole log: 1,050 cases, 15,214 events, complete only | -5% (-17%) | +50% (+60%) | - | - |
| BPI Challenge 2012 (loan applications) | first 9,372 of 13,087 cases: 190,000 of 262,200 events | +13% (-3%) | -97% (-12%) | 25% (24%) | -100% (11%) |
| BPI Challenge 2017 (loan applications) | first 4,901 of 31,509 cases: 190,000 of 1.2 M events | -20% (-64%) | -8% (+8%) | 18% (22%) | -100% (13%) |

Service and wait are the median gap over the activities with a start seen 30 times or more.
The two BPI logs are over the 200,000 event ceiling, so each was cut to the cases in arrival
order that fit 190,000 events; the ceiling was not raised. Three seeds each, the first half's
window as warm-up. The scripts are in `benchmarks/eventlog/` and the numbers in its
`results.json`.

**Throughput is close, within or near the log's own drift.** In Sepsis the draft is 5% off
while the halves differ by 17%; the publisher randomised each case's timestamps (keeping the
times within it), so the arrivals there test the fit and nothing more. BPI 2017 starts empty
on 1 January 2016, so its first half holds the ramp-up and the halves differ by 64%; a draft
from that half runs at its arrival rate and comes out 20% short of the second. BPI 2012's
13% follows from its cycle time, below: the draft's cases leave within hours, so its
completions follow its arrivals at once, while the log's lag them by weeks.

**Service times are right where the log shows them, and waits are not.** Drafted and run on
the same half, BPI 2012's service times come back within 1.4% (median); against the other
half the gap, 25%, is the log's own drift. In BPI 2017 the reader drops 73,675 rows whose
lifecycle it does not read (suspend, resume, abort, withdraw), so a service time there
includes the suspended hours and the times are heavy-tailed. The waits, however, come back as
zero in both: in these offices a work item waits tens of hours (72 h on average for a
follow-up call on an offer) at clerks who are busy 2% of the time or less. That is a calendar, a choice or the customer, not a queue, and a
draft whose resources are free makes no queue. The check now names this ("activities wait
long while the resources are mostly idle"), and the next steps say to model those waits by
hand.

**Cycle time follows from where the log keeps its time.** BPI 2017 keeps most of a case's
time in complete-only steps (waiting on the customer), which the draft runs as delays, so its
cycle time is 8% off, inside the drift. BPI 2012 keeps it in the waits above, and the draft's
cases finish in hours instead of weeks: 97% short. Sepsis's draft is 6% off the half it was
drawn from and 50% off the other, as the halves themselves differ by 60%: its long tail of
cases returning months later (416 of 1,050 cases arrive within the last 99th-percentile cycle
time) does not repeat from half to half. So run `--check` first: on these logs it flagged
the idle waits, the dropped rows, the office hours and the cases cut at the end before a
draft was made. Two causes it cannot see: a log that starts with an empty system (BPI 2017's
ramp-up) and timestamps changed by the publisher (Sepsis).

The logs are from 4TU.ResearchData and are not shipped with Naga: Sepsis Cases (doi
[10.4121/uuid:915d2bfb-7e84-49ad-a286-dc35f063a460](https://doi.org/10.4121/uuid:915d2bfb-7e84-49ad-a286-dc35f063a460)),
BPI Challenge 2012 ([10.4121/uuid:3926db30-f712-4394-aebc-75976070e91f](https://doi.org/10.4121/uuid:3926db30-f712-4394-aebc-75976070e91f))
and BPI Challenge 2017 ([10.4121/uuid:5f3067df-f10b-45da-b98b-86ae4c7a310b](https://doi.org/10.4121/uuid:5f3067df-f10b-45da-b98b-86ae4c7a310b)),
by their authors, under the terms 4TU states on each dataset's page.

## What it does not infer

The report's `assumptions` list says what the draft takes for granted, and `dropped` what it
left out and why. Read both before trusting a number. In short: arrivals are one stationary
rate; resources are there all the time (no shifts, breaks or failures), with the observed peak
as their capacity, which is a lower bound; parallel work in one case is run one activity after
the other; entity types, priorities, batching and decisions by data are not inferred. Without
`schedule` events, a case is taken to arrive when its first activity starts, so the queue in
front of it is not in the log.

A log is treated as untrusted data. It is read whole into memory, not streamed, so its size is
capped: a file over 50 MB, 200 000 events, 200 activities or 1000 resources is refused (a log
at the ceilings takes about 300 MB); a name from the log is folded to letters, digits and `_` before it
names a block or a resource, and nothing in the log becomes an expression or a path. An XES
file with a DOCTYPE or ENTITY declaration is refused before it is parsed, so no entity is ever
expanded.
