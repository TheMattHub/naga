"""`Schedule._index_at` by bisect gives the linear scan's answer everywhere
(performance report 2026-10-07, C2)."""
import math
import random

import pytest

from nagasim.core.schedules import Schedule


def _scan_index_at(schedule, t):
    """The scan `_index_at` replaced, kept here as the oracle."""
    if schedule.repeat:
        t = math.fmod(t, schedule.cycle_length)
        if t < 0:
            t += schedule.cycle_length
    for i, bound in enumerate(schedule.bounds):
        if t < bound:
            return i
    return len(schedule.values) - 1


@pytest.mark.parametrize("repeat", [False, True])
def test_index_at_matches_the_scan(repeat):
    rnd = random.Random(20261007)
    for n_points in (1, 2, 3, 7, 40):
        points = [[rnd.choice([0.1, 1.0, 2.5, 60.0, rnd.uniform(0.01, 100)]), i]
                  for i in range(n_points)]
        schedule = Schedule("s", "arrival", points, repeat=repeat)
        total = schedule.cycle_length
        times = [0.0, -0.0, -1.0, -total, -total * 2.5, total, total * 3, total * 3.5,
                 math.inf if not repeat else total * 1e6, 1e-300, -1e-300]
        for bound in schedule.bounds:           # exactly on, and one float either side
            times += [bound, math.nextafter(bound, -math.inf), math.nextafter(bound, math.inf),
                      bound + total, -bound]
        times += [rnd.uniform(-2 * total, 4 * total) for _ in range(2000)]
        for t in times:
            assert schedule._index_at(t) == _scan_index_at(schedule, t), (n_points, t)
