"""CONVEYOR zones with cascade release (conveyor system A, Task 4), written from the brief.

API assumptions (not from the implementation):
- CONVEYOR param `zones` = {"count": int, "release_delay": s}; default {} (no zones).
- Typed belt (`type` "belt"), entity type CASE with size.length 0.41 -> cell_length 0.41, gap 0.
- Per-zone dieback seconds (zone occupied and not moving) is `model.blocks[name].zone_dieback_time`,
  a list with one number per zone, index 0 = zone 1 (at the exit). Task 12 reads it.
- The exit is held by `exit_resource` "Op", seized at t = 0 by a hog for 20 s; a PROCESS "pick"
  (Delay Release, 0) hands Op back at once, so each belt exit is the item's deletion_time.

Common numbers: belt 1.8 m, speed 0.5 m/s (free flow 3.6 s), cell 0.41 m (0.82 s per cell),
capacity floor(1.8 / 0.41) = 4. Parts board every 5 s from t = 0, far apart enough to queue
unhindered: part k boards at 5k, its nose reaches the stop at nose 1.8 - 0.41 * q after
(1.8 - 0.41 * q) / 0.5 s, i.e. lead (q = 0) at 3.6, q = 1 (nose 1.39) at 5 + 2.78 = 7.78,
q = 2 (nose 0.98) at 10 + 1.96 = 11.96, q = 3 (nose 0.57) at 15 + 1.14 = 16.14. All before t = 20,
so at t = 20 the queue stands nose to tail.
"""
import math

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt, hog
from test_conveyor_divert_spec import pick

TOL = 1e-9


def _doc(zones, arrivals, hold=20.0):
    conveyor = belt(length=1.8, speed=0.5, exit_resource="Op" if hold else "")
    conveyor["params"].update({"type": "belt", "gap": 0.0})
    if zones is not None:
        conveyor["params"]["zones"] = zones
    blocks = [create(name="c", e_type="CASE", max_arrivals=arrivals,
                     dist_params={"value": 5.0}, start_time=0), conveyor]
    links = [{"source": "c", "target": "belt"}]
    if hold:
        blocks += [pick("Op"), dispose()]
        links += [{"source": "belt", "target": "pick"}, {"source": "pick", "target": "dispose_1"}]
        hog_blocks, hog_links = hog("Op", hold)
        blocks += hog_blocks
        links += hog_links
    else:
        blocks += [dispose()]
        links += [{"source": "belt", "target": "dispose_1"}]
    doc = layout(blocks, links, resources=[{"name": "Op", "capacity": 1}] if hold else [],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1})
    doc["entities"] = [{"name": "CASE", "size": {"length": 0.41, "width": 0.3, "height": 0.2}}]
    return doc


def _exits(doc):
    env, model = run_env(doc, 200)
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "CASE")
    return times, model


def _assert_times(got, want):
    assert len(got) == len(want)
    assert all(math.isclose(g, w, abs_tol=TOL) for g, w in zip(got, want)), got


def test_zero_delay_releases_one_slug():
    """Three parts queue at noses 1.8, 1.39, 0.98; Op frees at t0 = 20. Delay 0: all restart
    at 20 and keep the 0.41 m spacing. Lead: exit now, 20.00. Second: 0.41 m / 0.5 = 0.82 s,
    20.82. Third: (1.8 - 0.98) = 0.82 m / 0.5 = 1.64 s, 21.64 (headway 0.82 s)."""
    times, _ = _exits(_doc({"count": 3, "release_delay": 0.0}, 3))
    _assert_times(times, [20.0, 20.82, 21.64])


def test_one_second_delay_cascades_by_zone():
    """Zones are 0.6 m: zone 1 (1.2, 1.8], zone 2 (0.6, 1.2], zone 3 (0, 0.6]. Noses 1.8 and
    1.39 are in zone 1, 0.98 in zone 2. Restart = t0 + j * 1: zone 1 at 21, zone 2 at 22.
    Lead exits 21.00; second 21 + 0.41/0.5 = 21.82; third restarts 22 and travels
    0.82 m / 0.5 = 1.64 s: 23.64. Third's nose is never nearer than a cell to the second's
    (second is gone by 21.82)."""
    times, _ = _exits(_doc({"count": 3, "release_delay": 1.0}, 3))
    _assert_times(times, [21.0, 21.82, 23.64])


def test_free_flow_adds_no_time():
    """One part, zones count 3, delay 1, nothing holds the exit: 1.8 m / 0.5 = 3.6 s exactly
    (it boards at 0 and exits at 3.6)."""
    times, _ = _exits(_doc({"count": 3, "release_delay": 1.0}, 1, hold=0))
    _assert_times(times, [3.6])


def test_empty_zones_equal_todays_accumulating_belt():
    """zones omitted, zones {} and zones count 3 / delay 0 give the same exits for the same
    feed: 20.00, 20.82, 21.64 (as the zero-delay test; today's accumulating belt)."""
    want = [20.0, 20.82, 21.64]
    for zones in (None, {}, {"count": 3, "release_delay": 0.0}):
        times, _ = _exits(_doc(zones, 3))
        _assert_times(times, want)


def test_two_zones_four_parts_delay_two():
    """Zones 0.9 m: zone 1 (0.9, 1.8], zone 2 (0, 0.9]. Four parts, noses 1.8, 1.39, 0.98
    (all zone 1: 0.98 > 0.9) and 0.57 (zone 2). t0 = 20, delay 2: zone 1 restarts at 22,
    zone 2 at 24. Exits: lead 22.00; second 22 + 0.82 = 22.82; third 22 + 0.82 m / 0.5 =
    23.64; fourth 24 + (1.8 - 0.57) = 1.23 m / 0.5 = 2.46 s: 26.46."""
    times, _ = _exits(_doc({"count": 2, "release_delay": 2.0}, 4))
    _assert_times(times, [22.0, 22.82, 23.64, 26.46])


def test_zone_dieback_time_of_a_held_single_part():
    """One part, 3 zones, delay 1, exit held to t0 = 20. Its nose reaches zone 1 at 1.2 m
    (2.4 s, moving) and stops at the exit at 3.6; zone 1 is occupied and still from 3.6 until
    the zone restarts at t0 + 1 * 1 = 21: 21 - 3.6 = 17.4 s. Zones 2 and 3 never hold a part:
    0. Assumed attribute: Conveyor.zone_dieback_time, a list, index 0 = zone 1."""
    _, model = _exits(_doc({"count": 3, "release_delay": 1.0}, 1))
    dieback = model.blocks["belt"].zone_dieback_time
    assert len(dieback) == 3
    assert math.isclose(dieback[0], 17.4, abs_tol=1e-6)
    assert dieback[1] == 0 and dieback[2] == 0
