"""tools/bench.py: one tiny run through the timing function, result keys only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bench  # noqa: E402


def test_run_one_reports_split_timings(monkeypatch):
    # The split is CPU time (`time.process_time`), whose tick is about 16 ms on Windows: the
    # KPIs of a tiny model often cost less than one tick and read as 0.0, more often still
    # while other processes hold the CPU. A clock that moves by one step per reading makes
    # the split exact instead of lucky.
    ticks = iter(range(1, 10_000))
    monkeypatch.setattr(bench.time, "process_time", lambda: next(ticks) * 0.001)
    monkeypatch.setitem(bench.MODELS, "tiny", lambda: bench._overloaded_single_server(50.0))
    r = bench.run_one("tiny", repeat=1, memory=False)
    for key in ("run_seconds", "kpi_seconds", "total_seconds", "events", "peak_memory_mb"):
        assert key in r
    assert r["kpi_seconds"] > 0 and r["events"] > 0
    assert abs(r["total_seconds"] - r["run_seconds"] - r["kpi_seconds"]) < 1e-9


def test_call_counts_are_the_same_on_a_second_run(monkeypatch):
    """tools/bench_calls.py (speed review 2026-11-23): the counts it ranks and proves by repeat
    exactly for one seed, phases included, and they come from the raw profiler entries. They hold
    because the tool keeps the garbage collector off while it profiles: a collection inside the
    profile closes an earlier run's suspended processes, each close a call."""
    import bench_calls
    monkeypatch.setitem(bench.MODELS, "tiny", lambda: bench._overloaded_single_server(50.0))
    bench_calls.profile("tiny", top=3)
    first, second = bench_calls.profile("tiny", top=3), bench_calls.profile("tiny", top=3)
    assert first["calls"] == second["calls"] > 0 and first["phases"] == second["phases"]
    assert first["calls"] == sum(p["calls"] for p in first["phases"].values())
    assert first["events"] > 0 and first["entities"] > 0
