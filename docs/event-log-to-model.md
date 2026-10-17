# Event log to model

Most plants and offices already record what happened: which order went through which step,
when it started and finished, and who did it. `naga discover` reads that event log and writes
a draft model from it, with a report of what it inferred, what it assumed and what it left out.
The draft validates and runs, so the first model of a process can come from its own history
instead of a blank file. New to it? [From an event log to a model in five
minutes](event-log-tutorial.md) walks through the bundled sample log, `job_shop_log`.

## Draft a model

A log is one row per event: the case, the activity, an instant and, when the log has them,
the lifecycle (`schedule`, `start`, `suspend`, `resume`, `complete`) and the resource.

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
| suspend and resume | the service time less the suspended time; the suspended time is reported, not modelled |
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

**Idle waits.** Some work waits a long time at people or machines that are mostly free,
because of a calendar, a choice or the customer, and no queue in a draft makes that wait. With
`--idle-waits` (`idle_waits=True` in Python and in the agent tool), each activity the check
flags as waiting long at mostly idle resources gets a DELAY before it, named
`idle_wait_<activity>` and fitted to the log's waits the same way service times are. The
report's `idle_waits` lists the activities that got one. Such a delay is a modelled wait, not a
queue: it does not grow when the load does. The option is off by default, and then the draft
is exactly as without it.

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
or end, a log that starts empty, arrivals that drift over the log, crowd into working hours
or into weekdays, work done in shifts, cases arriving in batches, overlapping activities, and
rows dropped. The same check heads every report, so a draft always says what its log was like.

A log that starts empty is told by its work in progress. The check compares the mean number
of cases open over the log's first mean cycle time with that over the next one: a log begun
with the system running gives about 1, one begun empty about 0.5, and under 0.6 is flagged.
The finding is a warning when one mean cycle time is at least 5% of the time cases arrive
over, and `ok` below that, where the ramp-up weighs little. Then the log's start is a ramp-up,
so give the draft a warm-up before comparing it with a steady stretch, or draft from a later
part of the log. The check says nothing when it cannot tell: fewer than 30 cases arrive in
that first cycle time (a smaller count swings the ratio from 0.1 to 2), or a case has fewer
than five activity instances on average (a running case enters the log at its first event,
so with two or three a case a log begun running reads about 0.55). The bundled sample and
short logs of our own models fall under the first rule, so they are never flagged. A log of
complete cases, such as the three public ones below, starts empty by construction: no case
began before its window.

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
| the same, `--idle-waits` | | +12% (-3%) | -16% (-12%) | 24% (24%) | 7% (11%) |
| BPI Challenge 2017 (loan applications) | first 4,901 of 31,509 cases: 190,000 of 1.2 M events | -17% (-64%) | -14% (+8%) | 24% (27%) | -100% (35%) |
| the same, `--idle-waits` | | -18% (-64%) | -13% (+8%) | 24% (27%) | 62% (35%) |

Service and wait are the median gap over the activities with a start seen 30 times or more;
-100% means every wait came back as zero. The two BPI logs are over the 200,000 event
ceiling, so each was cut to the cases in arrival order that fit 190,000 events; the ceiling
was not raised. Three seeds each, the first half's window as warm-up. Sepsis has no idle
waits, so `--idle-waits` changes nothing there. The scripts are in `benchmarks/eventlog/` and
the numbers in its `results.json`.

**Throughput follows the arrival rate of the half the draft came from.** In Sepsis the
draft is 5% off while the halves differ by 17%; the publisher randomised each case's
timestamps (keeping the times within it), so the arrivals there test the fit and nothing
more. In BPI 2012 the draft's CREATE runs at the first half's 3.69 cases an hour, while the
second half's cases arrive at 3.26, and the draft comes out 12% to 13% over the second half's
completions. Against its own half it is 16% over, because that half completes fewer cases in
its window than arrive in it; the empty start the check flags would do that, but it is not
measured. In BPI 2017 the halves differ by 64%. The draft's CREATE runs at the first half's
3.10 cases an hour, 9% under the second half's 3.40, and with 89% of the measured window's
cases finished it comes out 17% to 18% short. Against its own half it is 128% over, as that
half completes few of its cases within its window. So a draft takes its half's arrival rate,
and in BPI 2017 it has not caught up with it within the measured window; the arrival rates
are in `results.json`, and why each half completes fewer cases than arrive in its window is
not measured.

**Service times are right where the log shows them.** Drafted and run on the same half, the
service times come back within 1.4% (median) in BPI 2012 and within 4.7% in BPI 2017, where
they were 19% off before suspend and resume were read. Against the other half the gaps, 25% and
24%, are the log's own drift (24% and 27%). BPI 2017 has 58,771 suspend and resume rows: each
pair's interval now leaves the service time (4,534 suspended instances, 51 hours suspended on
average), and 550 repeated suspends with nothing running are ignored and counted. Its 14,904
abort and withdraw rows each end their instance, which is dropped and counted; left open, an
aborted instance took the next one's resume. So the service times are the log's own; what is
left between the halves is the log changing.

**Waits come back with `--idle-waits`, and only then.** In these offices a work item waits
tens of hours at clerks who are busy 2% of the time or less. That is a calendar, a choice or
the customer, not a queue, so a draft whose resources are free gives every wait back as zero.
With `--idle-waits` the check's flagged activities get a delay fitted to their waits: in BPI
2012, all six activities with a start, and the median wait gap falls from 100% to 7%, inside
the log's own 11%. In BPI 2017 the check flags two of the four, so the median gap only falls to
62%: the two it does not flag still wait close to nothing. The delays are fitted to the log and do
not grow with the load, so they suit a draft run at the log's load, not a what-if with more
work.

**Cycle time follows from where the log keeps its time.** BPI 2012 keeps it in the waits
above: without the option the draft's cases finish in hours instead of weeks, 97% short;
with it, 16% short against a drift of 12%. BPI 2017 keeps most of a case's time in
complete-only steps (waiting on the customer), which the draft runs as delays, so its cycle
time is 13% to 14% short with or without the option. Sepsis's draft is 6% off the half it was drawn
from and 50% off the other, as the halves themselves differ by 60%: its long tail of cases
returning months later (416 of 1,050 cases arrive within the last 99th-percentile cycle
time) does not repeat from half to half. So run `--check` first: on these logs it flagged
the idle waits, the empty start of all three, the office hours and the cases cut at the end
before a draft was made. One cause it cannot see is timestamps changed by the publisher
(Sepsis).

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

Suspended work is read but not modelled. A `suspend` and the `resume` after it take their
interval out of the activity's service time, and the report gives each activity's suspended
instances and mean suspended time (`activities[].suspended`, and an `[ok]` line in the check).
The draft has no block for that time, so its cycle times come out shorter by it. A suspend
with nothing running, a resume with nothing suspended, and a suspend never resumed (its time
stays in the service time) are counted under `dropped`. An `abort` (`ate_abort`, `pi_abort`)
or a `withdraw` ends its activity instance, which is dropped and counted, and one with
nothing to end is ignored and counted; a row with any other lifecycle is dropped and counted.

A log is treated as untrusted data. It is read whole into memory, not streamed, so its size is
capped: a file over 50 MB, 200 000 events, 200 activities or 1000 resources is refused (a log
at the ceilings takes about 300 MB); a name from the log is folded to letters, digits and `_` before it
names a block or a resource, and nothing in the log becomes an expression or a path. An XES
file with a DOCTYPE or ENTITY declaration is refused before it is parsed, so no entity is ever
expanded.
