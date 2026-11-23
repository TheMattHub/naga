"""Conveyor release: metering, singulation, slugging (conveyor system A, Task 5), written from the
brief and spec section 5 before the code.

API assumptions (not from the implementation):
- CONVEYOR param `release` is a dict, default {}; exactly one of {"metering": {"gap": m}},
  {"metering": {"interval": m}}, {"singulation": {"gap": m}}, {"slug": {"count": n, "timeout": s}}.
- More than one form at once: validate_model_structured reports an ERROR with code
  "E_CONVEYOR_RELEASE" (the exact code is an assumption; the brief only says "refused at load").
- Typed belt shape as in tests/test_conveyor_types.py: type "belt", gap 0, entity DEFAULT size
  length 0.41, so cell_length 0.41. Belt length 3.0 m, speed 0.5 m/s: a free item reaches the end
  3.0 / 0.5 = 6.0 s after boarding. Exit = the moment the item leaves the end (deletion at DISPOSE).
Derivations: task-5-derivations.md.
"""
import math

from conftest import problem_codes, run_env
from test_conveyor_divert_spec import approx_all, plain_line
from test_conveyor_types import exits_of, sized, typed

L, V, ITEM = 3.0, 0.5, 0.41
FIRST = L / V  # 6.0 s: first item's arrival at the end on a free belt


def metered(release, arrivals, feed_gap):
    conveyor = typed(length=L, speed=V)
    conveyor["params"]["release"] = release
    doc = plain_line(conveyor, arrivals=arrivals, gap=feed_gap)
    doc["entities"] = [sized("DEFAULT", ITEM)]
    return doc


def exit_times(release, arrivals, feed_gap, run_len=100):
    env, _model = run_env(metered(release, arrivals, feed_gap), run_len)
    times = exits_of(env)
    assert len(times) == arrivals
    return times


def check(times, expected):
    assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(times, expected)), (times, expected)


def test_release_empty_is_todays_belt():
    """release {}: 4 items fed every 0.82 s (= 0.41 / 0.5, the cell time, so no boarding wait).
    Board 0, 0.82, 1.64, 2.46; exits 6.00, 6.82, 7.64, 8.46."""
    check(exit_times({}, 4, 0.82), [6.0, 6.82, 7.64, 8.46])


def test_metering_gap():
    """gap 0.40: headway (0.41 + 0.40) / 0.5 = 1.62 s. First 6.0, then 7.62, 9.24, 10.86.
    Item 4 reaches the end by 8.46, so it queues; 4 items fit the belt (capacity 7)."""
    check(exit_times({"metering": {"gap": 0.40}}, 4, 0.82), [6.0, 7.62, 9.24, 10.86])


def test_singulation_gap_is_metering_gap():
    """singulation {gap: 0.40} is a synonym: the same 1.62 s headways."""
    check(exit_times({"singulation": {"gap": 0.40}}, 4, 0.82), [6.0, 7.62, 9.24, 10.86])


def test_metering_interval():
    """interval 0.60 (lead to lead): headway 0.60 / 0.5 = 1.20 s. Exits 6.0, 7.2, 8.4, 9.6."""
    check(exit_times({"metering": {"interval": 0.60}}, 4, 0.82), [6.0, 7.2, 8.4, 9.6])


def test_metering_interval_never_below_item_length():
    """interval 0.30 < 0.41: clamped to the item length, headway 0.41 / 0.5 = 0.82 s.
    Exits 6.0, 6.82, 7.64, 8.46 (a 0.30 / 0.5 = 0.60 s headway would give 6.0, 6.6, 7.2, 7.8)."""
    check(exit_times({"metering": {"interval": 0.30}}, 4, 0.82), [6.0, 6.82, 7.64, 8.46])


def test_slug_of_three():
    """An item is queued when it stops at its accumulated position (nose at end - (k-1)*cell),
    not in free flow. Feed every 1.82 s: entries 0, 1.82, 3.64. Queued at 0 + 3.0/0.5 = 6.00;
    1.82 + (3.0-0.41)/0.5 = 7.00; 3.64 + (3.0-0.82)/0.5 = 8.00 (relative 0, 1, 2). Count 3 is
    met at t = 8: released nose to tail, 0.82 s apart: exits 8.00, 8.82, 9.64. Timeout 100
    never fires."""
    check(exit_times({"slug": {"count": 3, "timeout": 100.0}}, 3, 1.82), [8.0, 8.82, 9.64])


def test_slug_timeout():
    """Count 3, timeout 5, two items entering at 0 and 1.82 queue at 6.00 and 7.00 (accumulated
    positions, as above). The timer runs from the first (6) and fires at 11; release nose to
    tail: exits 11.00, 11.82."""
    check(exit_times({"slug": {"count": 3, "timeout": 5.0}}, 2, 1.82), [11.0, 11.82])


def test_two_release_forms_are_refused():
    """Two forms at once is an error at validation, code E_CONVEYOR_RELEASE (assumed)."""
    for release in ({"metering": {"gap": 0.4}, "slug": {"count": 3, "timeout": 5.0}},
                    {"metering": {"gap": 0.4}, "singulation": {"gap": 0.4}}):
        assert "E_CONVEYOR_RELEASE" in problem_codes(metered(release, 1, 1.0))
