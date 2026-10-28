"""tools/bench_scale.py: one tiny in-process cell; the event counter, the profile split, the copy builder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bench_scale  # noqa: E402


def test_cell_event_count_matches_step_wrapper_and_profile_sums_to_100():
    r = bench_scale._run_cell("jackson", 1, 100.0, profile=True, check_events=True)
    assert r["events"] > 0 and r["events"] == r["events_wrapper_count"]
    assert r["step_s"] > 0 and r["kpi_s"] > 0
    assert abs(sum(r["profile_split"]["by_group_pct"].values()) - 100.0) < 1e-6
    assert len(r["profile_split"]["top15"]) == 15


def test_line_copies_get_unique_blocks_and_links():
    d, _length, _warmup = bench_scale._scale_line(3)
    names = [b["name"] for b in d["blocks"]]
    assert len(names) == len(set(names)) == 27
    assert all(ln["source"] in names and ln["target"] in names for ln in d["links"])
