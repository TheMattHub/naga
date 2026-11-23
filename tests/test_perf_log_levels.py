"""Run log levels (D1395), Task 6: what a kpi run saves, as a call count.

A call-count budget, not a time: for one seed the count is the same on every run, so the guard
holds on a loaded machine. single_server for 20,000 time units runs at full (the default) and at
a named kpi, setup, step and KPIs all counted, as `tools/log_level_cost.py` counts them. kpi
must stay at or under 0.85x of full's calls; it fails when the logs or the sampler start costing
again below full (a live-row frame, a log append, a sample).
"""
import cProfile
import gc
import sys
from pathlib import Path

import pytest

from nagasim import api, loglevels
from nagasim.core.outputs import calc_kpis
from nagasim.core.sim_runner import setup_run, step_until

pytestmark = pytest.mark.perf

RUN = 20_000.0
WARMUP = 100.0
SINGLE = Path(api.__file__).parent / "models" / "single_server.json"
# Measured 2026-11-23 on 3.12: 0.79x (see the plan's appendix A6). The budget leaves room for
# a few calls per entity before it fails.
KPI_SHARE = 0.85


def _count(layout) -> int:
    setup_run(layout, seed=0, run_len=50.0, warmup=0.0)          # imports and caches first
    # As `tests/test_perf_seize_path.py`: a collection inside the profile would close the
    # processes of an earlier run, each close a counted call.
    collecting = gc.isenabled()
    gc.collect()
    gc.disable()
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        env, _model = setup_run(layout, seed=0, run_len=RUN, warmup=WARMUP)
        step_until(env, RUN)
        calc_kpis(env, warmup=WARMUP, run_id=0)
    finally:
        profiler.disable()
        if collecting:
            gc.enable()
    return sum(entry.callcount for entry in profiler.getstats())


# cProfile's counts differ between Python versions; the public engine CI runs 3.11-3.14
@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="call budgets measured on 3.12")
def test_a_kpi_run_stays_within_its_share_of_a_full_run():
    layout = api.load_layout(SINGLE)
    full = _count(layout)
    kpi = _count(loglevels.apply_level(layout, "kpi"))
    assert kpi <= KPI_SHARE * full, f"kpi makes {kpi / full:.3f}x the calls of full"
