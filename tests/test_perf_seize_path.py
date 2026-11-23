"""Speed review A+B (2026-11-23): the Python calls a plain seize-delay-release visit costs.

A call-count budget, not a time: for one seed the count is the same on every run, so the
guard holds on a loaded machine and fails on the first call a change adds to every visit.
The model is the benchmark's tandem line, lean (no logs, no sampler): two FIFO servers,
exponential arrivals and services. Counted from the profiler's raw entries, as
`tools/bench_calls.py` does (pstats can merge two functions that share a label).
"""
import cProfile
import gc
import sys

import pytest

from conftest import chain, create, dispose, layout, process
from nagasim.core.sim_runner import setup_run, step_until

pytestmark = pytest.mark.perf

RUN = 2_000.0
# Calls per station visit while stepping (arrivals and disposals included), and SimPy events
# per visit. Measured 2026-11-23 on this model: 220.6 calls before review A+B (4cf802a8), 185.1
# after, 181.56 after reviews E and C (garbage collector held off); 5.50 events on all. The
# budget fails on two more calls per visit.
CALLS_PER_VISIT = 183.0
EVENTS_PER_VISIT = 5.51


def _tandem():
    c = create("c", distribution="Exponential", dist_params={"rate": 1.0})
    p1 = process("p1", resource="S1", value=1.25, distribution="Exponential")
    p2 = process("p2", resource="S2", value=1.1, distribution="Exponential")
    d = dispose("d")
    blocks = [c, p1, p2, d]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "S1", "capacity": 1}, {"name": "S2", "capacity": 1}],
                  simulation={"time_limit": RUN, "warmup": 0, "replications": 1,
                              "record_logs": False, "timeseries_points": 0})


def _count():
    setup_run(_tandem(), seed=0, run_len=50.0, warmup=0.0)       # imports and caches first
    env, model = setup_run(_tandem(), seed=0, run_len=RUN, warmup=0.0)
    # As `tools/bench_calls.py`: a collection inside the profile would close the processes of
    # an earlier run (this file's or another test's in the worker), each close a counted call
    collecting = gc.isenabled()
    gc.collect()
    gc.disable()
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        step_until(env, RUN)
    finally:
        profiler.disable()
        if collecting:
            gc.enable()
    entries = profiler.getstats()
    calls = sum(entry.callcount for entry in entries)
    events = sum(entry.callcount for entry in entries
                 if not isinstance(entry.code, str) and entry.code.co_name == "step"
                 and entry.code.co_filename.replace("\\", "/").endswith("simpy/core.py"))
    visits = sum(env.ctx.resource_manager.get(name).seize_count for name in ("S1", "S2"))
    return calls, events, visits


# cProfile's counts differ between Python versions; the public engine CI runs 3.11-3.14
@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="call budgets measured on 3.12")
def test_a_plain_visit_stays_within_its_call_budget():
    calls, events, visits = _count()
    assert visits > 3_000
    assert events / visits <= EVENTS_PER_VISIT, "a plain visit schedules more SimPy events"
    assert calls / visits <= CALLS_PER_VISIT, f"{calls / visits:.2f} calls per visit"
