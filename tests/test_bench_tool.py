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
