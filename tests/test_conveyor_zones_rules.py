"""CONVEYOR zones beyond the spec cases (D1356): mixed sizes, dieback of every zone, a part
that packs after the release, an untyped belt, a belt with a point, the time unit, checks.

Hand derivations. Belt 1.8 m, 0.5 per time unit, 3 zones of 0.6 m: zone 1 (1.2, 1.8],
zone 2 (0.6, 1.2], zone 3 (0, 0.6]. Exit held to t0 = 20, delay 1 unless said.
- Mixed sizes, typed belt (cell 0.01): A 0.41 (41 cells) boards at 0, B 0.70 (70 cells) at 5,
  A at 10. Fronts packed: A 1.8 (stands 3.6), B 1.8 - 0.41 = 1.39 (stands 5 + 2.78 = 7.78),
  A 1.39 - 0.70 = 0.69 (stands 10 + 1.38 = 11.38). Zones 1, 1, 2.
  Delay 1: lead 21.00; B restarts 21, 0.41 m: 21.82; A restarts 22, 1.11 m = 2.22: 24.22.
  Delay 0: 20.00, 20.82, 20 + 1.11 / 0.5 = 22.22.
  Dieback: zone 1 [3.6, 21] = 17.4 (B's [7.78, 21] inside); zone 2 [11.38, 22] = 10.62.
- Three equal parts (0.41, every 5 s), delay 1: the third stands at 0.98 from 10 + 1.96 =
  11.96 to 22: zone 2 dieback 10.04; zone 1 17.4.
- Five equal parts: the fifth is made at 20 and waits for cells (4 * 41 = 164 of 180) until
  the lead leaves at 21; free it would be at the end at 24.6. The fourth stands at 0.57
  (zone 3) from 16.14 and restarts at 23, so the fifth packs behind it at 0.16 at
  24.6 - 1.64 / 0.5 = 21.32 and stands until 23. Exits: 21, 21.82, 23.64,
  23 + 1.23 / 0.5 = 25.46, then 25.46 + 0.82 = 26.28. Dieback: 17.4, 10.04 and zone 3
  [16.14, 23] (the fifth's [21.32, 23] inside) = 6.86.
- Held to the end of a 200 run: zone 1 [3.6, 200] = 196.4, zone 2 [11.96, 200] = 188.04.
"""
import math

import pytest
from conftest import create, run_env
from test_conveyor_zones import _doc, _exits

from nagasim.core.schema import validate_model_structured

TOL = 1e-9


def _close(got, want, tol=TOL):
    assert len(got) == len(want), got
    assert all(math.isclose(g, w, abs_tol=tol) for g, w in zip(got, want)), got


def _mixed(delay):
    doc = _doc({"count": 3, "release_delay": delay}, 2)
    doc["blocks"][0] = create(name="c", e_type="A", max_arrivals=2, dist_params={"value": 10.0},
                              start_time=0)
    doc["blocks"].append(create(name="b", e_type="B", max_arrivals=1, start_time=5))
    doc["links"].append({"source": "b", "target": "belt"})
    doc["entities"] = [{"name": "A", "size": {"length": 0.41, "width": 0.3, "height": 0.2}},
                       {"name": "B", "size": {"length": 0.70, "width": 0.3, "height": 0.2}}]
    env, model = run_env(doc, 200)
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type in ("A", "B"))
    return times, model.blocks["belt"].zone_dieback_time


def test_mixed_sizes_cascade_by_their_own_lengths():
    times, dieback = _mixed(1.0)
    _close(times, [21.0, 21.82, 24.22])
    _close(dieback, [17.4, 10.62, 0.0], 1e-6)


def test_mixed_sizes_at_delay_zero_release_one_slug():
    times, _ = _mixed(0.0)
    _close(times, [20.0, 20.82, 22.22])


def test_every_zone_counts_its_dieback():
    _, model = _exits(_doc({"count": 3, "release_delay": 1.0}, 3))
    _close(model.blocks["belt"].zone_dieback_time, [17.4, 10.04, 0.0], 1e-6)


def test_a_part_that_packs_after_the_release_stands_until_the_part_ahead_restarts():
    times, model = _exits(_doc({"count": 3, "release_delay": 1.0}, 5))
    _close(times, [21.0, 21.82, 23.64, 25.46, 26.28])
    _close(model.blocks["belt"].zone_dieback_time, [17.4, 10.04, 6.86], 1e-6)


def test_an_untyped_belt_cascades_alike():
    doc = _doc({"count": 3, "release_delay": 1.0}, 3)
    params = doc["blocks"][1]["params"]
    del params["type"], params["gap"]
    params["spacing"] = 0.41
    doc["entities"] = []
    times, _ = _exits(doc)
    _close(times, [21.0, 21.82, 23.64])


def test_a_point_before_the_queue_changes_nothing():
    doc = _doc({"count": 3, "release_delay": 1.0}, 3)
    doc["blocks"][1]["params"]["eyes"] = [{"at": 0.2}]
    times, _ = _exits(doc)
    _close(times, [21.0, 21.82, 23.64])


def test_the_delay_may_name_its_time_unit():
    """The model is in minutes: 60 seconds is the delay 1 above."""
    times, _ = _exits(_doc({"count": 3, "release_delay": 60, "time_unit": "seconds"}, 3))
    _close(times, [21.0, 21.82, 23.64])


def test_open_stands_count_to_now():
    _, model = run_env(_doc({"count": 3, "release_delay": 1.0}, 3, hold=300.0), 200)
    _close(model.blocks["belt"].zone_dieback_time, [196.4, 188.04, 0.0], 1e-6)


def test_params_keep_zones_only_when_set():
    _, model = _exits(_doc({"count": 3, "release_delay": 1.0}, 1))
    assert model.blocks["belt"].get_params_dict()["zones"] == {"count": 3, "release_delay": 1.0}
    _, model = _exits(_doc(None, 1))
    assert "zones" not in model.blocks["belt"].get_params_dict()


@pytest.mark.parametrize("zones, accumulating, case", [
    ({"count": 0}, True, "zones"),
    ({"count": 2.5}, True, "zones"),
    ({"count": 2, "release_delay": -1}, True, "zones"),
    ({"count": 2, "spacing": 1}, True, "zones"),
    ({"count": 2, "time_unit": "fortnights"}, True, "zones"),
    ({"count": 2, "release_delay": 1}, False, "zones_belt"),
])
def test_bad_zones_are_a_problem(zones, accumulating, case):
    doc = _doc(zones, 1)
    doc["blocks"][1]["params"]["accumulating"] = accumulating
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
    assert [p.fields["case"] for p in found] == [case]


def test_good_zones_are_no_problem():
    doc = _doc({"count": 2, "release_delay": 1.5, "time_unit": "seconds"}, 1)
    assert not [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]


def test_a_trace_stands_a_part_in_its_zone_until_its_restart():
    """The third part (front 0.98, zone 2) is traced standing there at t0 = 20 and moving
    from 0.98 at 22; tracing changes no exit."""
    from nagasim.core.trace import TraceRecorder
    recorder = TraceRecorder()
    env, _ = run_env(_doc({"count": 3, "release_delay": 1.0}, 3), 200, subscribers=[recorder])
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "CASE")
    _close(times, [21.0, 21.82, 23.64])
    third = max(e.id for e in env.ctx.all_entities if e.e_type == "CASE")
    stages = [(e.t, e.state, e.x0) for e in recorder.events
              if e.kind == "belt_stage" and e.entity_id == third and e.t >= 20]
    assert (20.0, "packed", pytest.approx(0.98)) in stages
    assert any(t == pytest.approx(22.0) and s == "move" and x == pytest.approx(0.98)
               for t, s, x in stages)


def _untyped_zoned():
    doc = _doc({"count": 3, "release_delay": 1.0}, 3)
    params = doc["blocks"][1]["params"]
    del params["type"], params["gap"]
    params["spacing"] = 0.41
    doc["entities"] = []
    return doc


def test_a_zoned_belt_is_refused_in_a_state_by_name():
    from nagasim import state
    doc = _untyped_zoned()
    doc["simulation"]["time_limit"] = 30
    assert state.refused_kind(doc) == "a conveyor with zones"


def test_a_part_on_a_zoned_belt_in_an_initial_state_is_refused():
    doc = _untyped_zoned()
    doc["initial_state"] = {"clock": 2.0, "entities": [
        {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 3.0, "entered": 1.0}}]}
    found = [p for p in validate_model_structured(doc) if p.code == "E_INITIAL_BELT"]
    assert [p.fields["case"] for p in found] == ["refuse"]


# Fix round 1 (review I1-I4). Hand derivations, same belt, 3 zones, delay 1, Op held to 20:
# - P1, R23: b1 (zoned, spacing 0.41) hands to b2 (0.41 m, one cell, exit Op). Part 1 rides
#   b1 to 3.6, b2 to 4.42 and waits Op: exits 20. Part 2 reaches b1's end at 8.6 and stands
#   (b2 full) until part 1 leaves b2 at 20; it waits its zone-1 delay there, so b2 takes it at
#   21 and it rides 0.82: 21.82. b1 zone 1 dieback [8.6, 21] = 12.4.
# - P2, I2: parts at 0 and 5 queue (fronts 1.8, 1.39, zone 1, restart 21); a third boards at
#   18.5 (free at the end at 22.1) and packs at 0.98 (zone 2) at 22.1 - (1.8 - 0.98) / 0.5 =
#   20.46, after t0. Its own zone restarts at 20 + 2 = 22, so it
#   exits 22 + 1.64 = 23.64 (as a part that stood in zone 2 at t0). Zone 2 dieback 22 - 20.46
#   = 1.54.
# - P4, I3: the same with pick holding Op 3: the second part stands at the exit 21.82-24
#   (a second release); the third's zone 2 stand [20.46, 22] still counts: 1.54.


def _handover(zones):
    from conftest import dispose, layout
    from test_conveyor_divert_spec import belt, hog, pick
    b1 = belt(name="b1", length=1.8, speed=0.5, spacing=0.41)
    if zones:
        b1["params"]["zones"] = zones
    b2 = belt(name="b2", length=0.41, speed=0.5, spacing=0.41, exit_resource="Op")
    blocks = [create(name="c", max_arrivals=2, dist_params={"value": 5.0}, start_time=0),
              b1, b2, pick("Op"), dispose()]
    links = [{"source": "c", "target": "b1"}, {"source": "b1", "target": "b2"},
             {"source": "b2", "target": "pick"}, {"source": "pick", "target": "dispose_1"}]
    hog_blocks, hog_links = hog("Op", 20.0)
    doc = layout(blocks + hog_blocks, links + hog_links, resources=[{"name": "Op", "capacity": 1}],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1})
    env, model = run_env(doc, 200)
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "DEFAULT")
    return times, model.blocks["b1"]


def test_a_part_handed_to_a_next_belt_waits_its_zone_delay_first():
    times, b1 = _handover({"count": 3, "release_delay": 1.0})
    _close(times, [20.0, 21.82])
    _close(b1.zone_dieback_time, [12.4, 0.0, 0.0], 1e-6)
    times, _ = _handover(None)
    _close(times, [20.0, 20.82])


def _late(start, pick_hold=0.0):
    doc = _doc({"count": 3, "release_delay": 1.0}, 2)
    for block in doc["blocks"]:
        if block["name"] == "pick":
            block["params"]["dist_params"] = {"value": pick_hold}
    doc["blocks"].append(create(name="c3", e_type="CASE", max_arrivals=1, start_time=start))
    doc["links"].append({"source": "c3", "target": "belt"})
    return _exits(doc)


def test_a_part_that_packs_after_the_release_keeps_its_own_zone_stagger():
    times, model = _late(18.5)
    _close(times, [21.0, 21.82, 23.64])
    _close(model.blocks["belt"].zone_dieback_time, [17.4, 1.54, 0.0], 1e-6)


def test_a_late_stand_counts_when_a_second_release_catches_the_part():
    _, model = _late(18.5, pick_hold=3.0)
    assert math.isclose(model.blocks["belt"].zone_dieback_time[1], 1.54, abs_tol=1e-6)


@pytest.mark.parametrize("field, value", [
    ("stops", [{"at": 0.5, "time": 1.0}]),
    ("merges", [{"at": 0.5, "from": "c"}]),
])
def test_zones_with_stops_or_merges_are_refused(field, value):
    doc = _doc({"count": 3, "release_delay": 1.0}, 1)
    doc["blocks"][1]["params"][field] = value
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
    assert [p.fields["case"] for p in found] == ["zones_points"]


def test_a_run_refuses_zones_with_a_stop():
    doc = _doc({"count": 3, "release_delay": 1.0}, 1)
    doc["blocks"][1]["params"]["stops"] = [{"at": 0.5, "time": 1.0}]
    with pytest.raises(Exception, match="zones"):
        run_env(doc, 10)
