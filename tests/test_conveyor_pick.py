"""The pick on the move (D1343): a robot called at an intercept divert meets its part at the
meeting point its plan computes, for a MOTION and a robot pattern BATCH (plan
2026-11-19-p3-conveyor-detail, Task 4).

Hand facts, gantry_pick_cell: the belt runs 0.5 m/s, the call is at 0.5 m (reached at
boarding + 1.0), the approach takes 2.0 s and meets the part at 1.5 m (reached at boarding
+ 3.0); the cycle is 2.0 + 1.0 + 2.0 + 1.0 s. The belt is 4 m long (its end at boarding +
8.0) and the section between the call and the meeting point holds one part.
"""
from __future__ import annotations

import pytest

from conftest import create, process, run_env
from robotcases import gantry_pallet_cell, gantry_pick_cell
from test_conveyor_divert_spec import approx_all, exits, pick

from nagasim import api
from nagasim.core import beltpick, pallets, schema
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder
from nagasim.settings import trustwrite

NO_WAIT = pytest.approx(0.0, abs=1e-9)


def _block(doc, name):
    return next(b for b in doc["blocks"] if b["name"] == name)


def traced(doc):
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    return results, recorder.events


def offs(events):
    return [(e.t, e.x0) for e in events if e.kind == "belt_stage" and e.state == "off"]


def divert_row(results):
    return results["conveyor_diverts"].iloc[0]


def meet_rows(results):
    frame = results.get("motion_summary")
    return [] if frame is None else frame[frame["Segment Kind"] == "meet"].to_dict("records")


def assert_offs(found, expected):
    assert [t for t, _x in found] == approx_all([t for t, _x in expected])
    assert [x for _t, x in found] == approx_all([x for _t, x in expected])


def test_a_free_robot_meets_the_part_on_the_move():
    results, events = traced(gantry_pick_cell())
    assert_offs(offs(events), [(3.0, 1.5), (13.0, 1.5)])
    starts = [e.t for e in events if e.kind == "cycle_phase" and e.block == "robot"
              and e.value == 0.0]
    assert starts == approx_all([1.0, 11.0])
    assert exits(results, "BOX") == approx_all([7.0, 17.0])
    row = divert_row(results)
    assert (row["Diverted"], row["Skipped"]) == (2, 0)
    assert row["Max Wait"] == NO_WAIT
    assert meet_rows(results) == []


def test_a_busy_robot_leaves_the_part_standing_at_the_meet():
    """Part 2 stands at 1.5 from 5; the robot is free at 7 and there at 9. Part 3 waits at
    the call from 5 to 9 (the section to the meeting point holds one part), reaches 1.5 at
    11; the robot is free at 13 and there at 15."""
    results, events = traced(gantry_pick_cell(arrivals=3, every=2.0))
    assert exits(results, "BOX") == approx_all([7.0, 13.0, 19.0])
    assert [t for t, _x in offs(events)] == approx_all([3.0, 9.0, 15.0])
    row = divert_row(results)
    assert row["Max Wait"] == pytest.approx(4.0)
    assert row["Avg Wait"] == pytest.approx(8 / 3)
    rows = Timeline(events).state_at(7.0)["belts"]["belt"]
    assert [x for _i, x, _s in rows] == approx_all([1.5, 0.5])
    assert [i for i, _x, _s in rows] == sorted(i for i, _x, _s in rows)     # part 2, then 3


def test_pass_lets_the_part_ride_on_when_the_robot_is_busy():
    results = api.run_model(gantry_pick_cell("pass", arrivals=3, every=2.0), replications=1)
    assert exits(results, "BOX") == approx_all([7.0, 10.0, 12.0])
    row = divert_row(results)
    assert (row["Diverted"], row["Skipped"]) == (1, 2)


def test_a_part_standing_at_the_meet_stops_a_non_accumulating_belt():
    """Every 2.5: part 2 is called at 3.5 (the robot works part 1 until 7) and stands at
    1.5 from 5.5, which stops the belt with part 3 at 0.25. The robot is there at 9.0, the
    belt runs again, part 3 reaches the call at 9.5 and 1.5 at 11.5, and stands until the
    robot, free at 13, is there at 15."""
    results, events = traced(gantry_pick_cell(arrivals=3, every=2.5, accumulating=False))
    assert [t for t, _x in offs(events)] == approx_all([3.0, 9.0, 15.0])
    halted = [(e.t, e.x0) for e in events if e.kind == "belt_stage" and e.state == "halted"]
    assert halted == [(pytest.approx(5.5), pytest.approx(0.25))]
    resumed = [e for e in events if e.kind == "belt_stage" and e.state == "ride"
               and e.t == pytest.approx(9.0)]
    assert [e.x0 for e in resumed] == approx_all([0.25])
    assert exits(results, "BOX") == approx_all([7.0, 13.0, 19.0])
    assert divert_row(results)["Max Wait"] == pytest.approx(3.5)


def _hog(name, resource, hold):
    return ([create(name=f"{name}_src", e_type="Hog", max_arrivals=1, start_time=0),
             process(name=name, resource=resource, value=hold)],
            [{"source": f"{name}_src", "target": name}, {"source": name, "target": "dispose_1"}])


def test_a_stopped_belt_keeps_the_robot_waiting_at_the_meet():
    """Part 1 is passed at 1.0 (a hog has the robot until 2) and stops the belt at its end
    at 8.0 (another hog has R until 20). Part 2 is called at 7.0, the robot is there at 9.0;
    part 2 halts at 1.0 at 8.0, resumes at 20 and reaches 1.5 at 21.0."""
    doc = gantry_pick_cell("pass", arrivals=2, every=6.0, accumulating=False)
    _block(doc, "belt")["params"]["exit_resource"] = "R"
    for link in doc["links"]:
        if link == {"source": "belt", "target": "dispose_end"}:
            link["target"] = "pick"
    doc["blocks"].append(pick("R"))
    doc["links"].append({"source": "pick", "target": "dispose_end"})
    doc["resources"].append({**doc["resources"][0], "name": "R"})
    for name, resource, hold in (("hog_robot", "Robot", 2.0), ("hog_r", "R", 20.0)):
        blocks, links = _hog(name, resource, hold)
        doc["blocks"] += blocks
        doc["links"] += links
    assert not [p for p in schema.validate_model_structured(doc) if p.severity == "error"]
    recorder = TraceRecorder()
    env, model = run_env(doc, 100, subscribers=[recorder])
    boxes = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "BOX")
    assert boxes == approx_all([20.0, 25.0])
    assert_offs(offs(recorder.events)[-1:], [(21.0, 1.5)])
    halted = [e for e in recorder.events if e.kind == "belt_stage" and e.state == "halted"]
    assert [(e.t, e.x0) for e in halted][-1] == (pytest.approx(8.0), pytest.approx(1.0))
    assert model.blocks["robot"].motion_stats["meet"] == [1, pytest.approx(12.0)]


def test_a_preempted_robot_arrives_late_and_the_part_waits():
    """The approach runs 1 to 2 and 3 to 4 (an urgent job has the robot from 2 to 3); the
    part reaches 1.5 at 3 and stands there until 4."""
    doc = gantry_pick_cell(arrivals=1)
    _block(doc, "robot")["params"].update(priority=1, on_preempt="resume")
    urgent = process(name="urgent", resource="Robot", value=1.0, preempt=True)
    doc["blocks"] += [create(name="urgent_src", e_type="Urgent", max_arrivals=1, start_time=2),
                      urgent]
    doc["links"] += [{"source": "urgent_src", "target": "urgent"},
                     {"source": "urgent", "target": "dispose_1"}]
    results, events = traced(doc)
    assert_offs(offs(events), [(4.0, 1.5)])
    assert exits(results, "BOX") == approx_all([8.0])
    assert divert_row(results)["Max Wait"] == pytest.approx(1.0)
    assert meet_rows(results) == []


def test_a_part_the_condition_refuses_rides_past_the_robot():
    doc = gantry_pick_cell()
    _block(doc, "belt")["params"]["diverts"][0]["condition"] = "False"
    results = api.run_model(doc, replications=1)
    assert exits(results, "BOX") == approx_all([8.0, 18.0])
    row = divert_row(results)
    assert (row["Diverted"], row["Passed"]) == (0, 2)


LATE_RULE = ("class Late:\n"
             "    def on_signal(self, name, item):\n"
             "        self.n = getattr(self, 'n', 0) + 1\n"
             "        item.set('late_wait', self.n % 2)\n")


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    """An empty trust store of this test's own, the rule's source trusted in it (as
    tests/cases/test_router_item.py does)."""
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path))
    trustwrite.trust_source(LATE_RULE, label="late rule", by="test")


def test_item_code_decides_wait_or_pass_per_box(trusted):
    """A person's own rule (decision 8): an eye at 0.2 raises "seen", the item's on_signal
    writes late_wait (1, 0, 1), and the same firing stamps it on the box. Box 1 finds the
    robot free; box 2 finds it busy and passes (the end at 10.0); box 3 finds it busy, waits
    and stands at 1.5 from 7.0, where the robot, free at 7.0, is at 9.0."""
    doc = gantry_pick_cell("entity.late == 1", arrivals=3, every=2.0)
    _block(doc, "belt")["params"]["eyes"] = [
        {"at": 0.2, "signal": "seen", "assign": [{"target": "entity.late", "expr": "late_wait"}]}]
    doc["variables"] = [{"name": "late_wait", "initial": 0}]
    doc["submodels"] = {"Late": {"parameters": [], "blocks": [], "links": [],
                                 "code": {"class": "Late", "source": LATE_RULE,
                                          "signals": ["seen"]}}}
    doc["blocks"].append({"name": "late_rule", "type": "SUBMODEL", "position": [0, 0],
                          "params": {"definition": "Late", "arguments": {}}})
    doc["schema_version"] = schema.CURRENT_VERSION
    assert not [p for p in schema.validate_model_structured(doc) if p.severity == "error"]
    results = api.run_model(doc, replications=1)
    assert exits(results, "BOX") == approx_all([7.0, 10.0, 13.0])
    row = divert_row(results)
    assert (row["Diverted"], row["Skipped"]) == (2, 1)
    assert row["Max Wait"] == pytest.approx(2.0)


def absorbed(events):
    return [e.t for e in events if e.kind == "entity_absorbed" and e.block == "palletizer"]


def test_a_palletizer_takes_each_box_from_the_moving_belt(monkeypatch):
    """Box 1 is called at 1.0 and met at 3.0; it goes aboard when its slot's cycle, counted
    from the call, ends. Box 2 likewise from 21.0, and the full pallet leaves then. The
    robot's side is MOTION's code: beltpick.robot_arrives, once per box."""
    calls = []
    real = beltpick.robot_arrives

    def counted(env, entity):
        calls.append(env.now)
        return (yield from real(env, entity))

    monkeypatch.setattr(beltpick, "robot_arrives", counted)
    doc = gantry_pallet_cell(arrivals=2, every=20.0)
    cycles, errors = pallets.slot_cycles(doc, _block(doc, "palletizer"))
    assert errors == [] and len(cycles) == 2
    results, events = traced(doc)
    assert_offs(offs(events), [(3.0, 1.5), (23.0, 1.5)])
    assert calls == approx_all([3.0, 23.0])
    assert absorbed(events) == approx_all([1.0 + cycles[0], 21.0 + cycles[1]])
    assert exits(results, "PALLET") == approx_all([21.0 + cycles[1]])
    assert divert_row(results)["Max Wait"] == NO_WAIT


def test_a_busy_palletizer_leaves_the_box_standing():
    """Box 2 is called at 3.0 while the robot places box 1, rides to 1.5 at 5.0 and stands
    there; the robot takes it at 1.0 + C0 and meets it 2.0 later."""
    doc = gantry_pallet_cell(arrivals=2, every=2.0)
    cycles, _errors = pallets.slot_cycles(doc, _block(doc, "palletizer"))
    results, events = traced(doc)
    met = 1.0 + cycles[0] + 2.0
    assert_offs(offs(events), [(3.0, 1.5), (met, 1.5)])
    stand = [(e.t, e.x0) for e in events if e.kind == "belt_stage" and e.state == "handover"]
    assert stand == [(pytest.approx(5.0), pytest.approx(1.5))]
    assert divert_row(results)["Max Wait"] == pytest.approx(met - 5.0)
