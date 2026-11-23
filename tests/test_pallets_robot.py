"""A robot's cycle per pallet slot: one cycle plan per slot through `plan_cycle`, the robot
at `robot_at` from the pallet's block, the targets `slot` and `slot_above` on each slot."""
import copy

import numpy as np
import pytest

from cases.handbuilt import ROBOT

from nagasim.core import schema
from nagasim.core.pallets import slot_cycles, slots
from nagasim.core.robotics.moves import cell_pose, plan_cycle
from nagasim.core.robotics.records import robot_from_record

from conftest import block, create, dispose, layout, run_env

PICK = {"xyz": [1.6, 0.0, 0.8]}
SEGMENTS = [{"kind": "ptp", "to": "pick"}, {"kind": "pick", "time": 1.0},
            {"kind": "ptp", "to": "slot_above"}, {"kind": "ptp", "to": "slot"},
            {"kind": "place", "time": 1.0}]


def pattern(scale=1.0, far=False):
    """A 2 x 2 layer of 0.4 x 0.3 items on a 1.2 x 0.8 pallet (lengths times *scale*); with
    *far*, slot 4 is 1.4 m out (an overhang lets the check pass)."""
    s = scale
    layer = [{"x": -0.3 * s, "y": -0.2 * s}, {"x": 0.3 * s, "y": -0.2 * s},
             {"x": -0.3 * s, "y": 0.2 * s}, {"x": 0.3 * s, "y": (1.4 if far else 0.2) * s}]
    return {"name": "P1", "pallet": {"length": 1.2 * s, "width": 0.8 * s, "deck_height": 0.144 * s},
            "item": {"length": 0.4 * s, "width": 0.3 * s, "height": 0.25 * s, "weight_kg": 12},
            "overhang": 2.0 * s if far else 0.0,
            "layers": {"A": layer}, "stack": [{"layer": "A"}], "limits": {"layers": 1}}


def cell(scale=1.0, far=False, unit="meters", **params):
    """The pallet BATCH at (0, 1.6 m), the robot 1.6 m toward -y from it (at the origin)."""
    s = scale
    batch = block("palletizer", "BATCH", {
        "batch_size": 4, "batched_entity_type": "PALLET", "carrier_type": "PALLET",
        "pattern": "P1", "robot": "IRB", "robot_at": [0.0, -1.6 * s, 0.0],
        "targets": {"pick": copy.deepcopy(PICK)}, "segments": copy.deepcopy(SEGMENTS),
        **params})
    batch["layout_position"] = [0.0, 1.6 * s]
    blocks = [create("pallets", e_type="PALLET", max_arrivals=1, dist_params={"value": 1000.0}),
              create("boxes", e_type="BOX", entities_per_arrival=4, max_arrivals=1),
              batch, dispose("carriers")]
    links = [{"source": "pallets", "target": "palletizer"}, {"source": "boxes", "target": "palletizer"},
             {"source": "palletizer", "target": "carriers"}]
    doc = layout(blocks, links, robots=[copy.deepcopy(ROBOT)], pallet_patterns=[pattern(s, far)])
    doc["simulation"].update(time_unit="seconds", length_unit=unit)
    return doc, batch


def by_hand(slot) -> float:
    """The cycle of one slot with its targets written out as cell frames."""
    top = slot.z + 0.125
    here = {"xyz": [slot.x, 1.6 + slot.y, top], "yaw_deg": slot.rotation}
    targets = {"pick": PICK, "slot": here,
               "slot_above": {**here, "xyz": [slot.x, 1.6 + slot.y, top + 0.3]}}
    segments = [{**s, "payload_kg": 12} if s["kind"] == "ptp" else s for s in SEGMENTS]
    plan = plan_cycle(robot_from_record(ROBOT), targets, segments, base=cell_pose(0, 0, 0))
    assert plan.errors == ()
    return sum(m.seconds for m in plan.moves.values()) + 2.0


def test_each_slot_gets_its_own_cycle():
    doc, batch = cell()
    cycles, errors = slot_cycles(doc, batch)
    assert errors == []
    assert len(set(round(c, 9) for c in cycles)) > 1               # near and far slots differ
    assert cycles == pytest.approx([by_hand(s) for s in slots(pattern())], abs=1e-9)


def test_far_slot_is_out_of_reach_before_the_run():
    doc, batch = cell(far=True)
    _cycles, errors = slot_cycles(doc, batch)
    assert {k for k, _e in errors} == {4}
    found = [p for p in schema.validate_model_structured(doc) if p.code == "E_ROBOT_REACH"]
    assert len(found) == 1 and "slot 4" in found[0].message and found[0].block == "palletizer"


def test_millimetre_model_plans_the_same():
    metres, mm = cell(), cell(scale=1000.0, unit="millimeters")
    assert slot_cycles(*mm)[0] == pytest.approx(slot_cycles(*metres)[0], abs=1e-9)


def test_approach_is_a_length_in_the_model_unit():
    default = slot_cycles(*cell())[0]
    higher = slot_cycles(*cell(approach=0.6))[0]
    in_mm = slot_cycles(*cell(scale=1000.0, unit="millimeters", approach=600))[0]
    assert higher == pytest.approx(in_mm, abs=1e-9)
    assert all(h > d for h, d in zip(higher, default))
    assert slot_cycles(*cell(approach="0.3"))[0] == pytest.approx(default, abs=1e-12)
    assert not [p for p in schema.validate_model_structured(cell(approach=0.6)[0])
                if p.severity == "error"]


def test_an_edited_pattern_or_robot_is_planned_afresh():
    doc, batch = cell()
    first = slot_cycles(doc, batch)[0]
    doc["pallet_patterns"][0]["layers"]["A"][0]["x"] = -0.5        # slot 1 further out
    assert slot_cycles(doc, batch)[0][0] != first[0]
    doc, batch = cell()
    for axis in doc["robots"][0]["axes"]:
        axis["speed"] /= 2
    assert all(slow > fast for slow, fast in zip(slot_cycles(doc, batch)[0], first))


def test_a_cell_turned_a_quarter_plans_the_same():
    doc, batch = cell()
    for s in doc["pallet_patterns"][0]["layers"]["A"]:
        s["rotation"] = 30
    doc["pallet_patterns"][0]["overhang"] = 1.0
    straight = slot_cycles(doc, batch)[0]
    batch["layout_position"], batch["layout_rotation"] = [-1.6, 0.0], 90     # block, robot, pick
    batch["params"]["targets"]["pick"]["xyz"] = [0.0, 1.6, 0.8]
    assert slot_cycles(doc, batch)[0] == pytest.approx(straight, abs=1e-9)


def test_reserved_target_names_cannot_be_defined():
    doc, batch = cell(targets={"pick": PICK, "slot": {"xyz": [0, 0, 0]}})
    found = [p for p in schema.validate_model_structured(doc) if p.code == "E_ROBOT_TARGET"]
    assert [(p.block, p.fields["case"], p.fields["target"]) for p in found] == [
        ("palletizer", "reserved", "slot")]


def test_a_robot_needs_the_pattern_named():
    doc, _batch = cell(pattern='IF(1 > 0, "P1", "P1")')
    found = [p for p in schema.validate_model_structured(doc) if p.code == "E_PALLET_UNKNOWN"]
    assert [p.fields["case"] for p in found] == ["robot"]


def test_pallet_time_is_the_sum_of_its_slot_cycles():
    doc, batch = cell(member_time="100")                 # ignored: the robot times each item
    env, _ = run_env(doc, 1000)
    pallet = next(e for e in env.ctx.all_entities if e.e_type == "PALLET")
    assert pallet.deletion_time == pytest.approx(sum(slot_cycles(doc, batch)[0]), abs=1e-9)
    assert np.isfinite(pallet.deletion_time)


def test_a_depalletizing_robot_picks_each_slot_top_first():
    doc, batch = cell()
    for key in ("robot", "robot_at", "targets", "segments"):
        batch["params"].pop(key)
    batch["params"]["member_time"] = "0"
    depal = block("depalletizer", "SEPARATE", {
        "mode": "Retain original entity values", "attrs": [], "carrier": "keep", "pattern": "P1",
        "robot": "IRB", "robot_at": [0.0, -1.6, 0.0], "targets": {"drop": copy.deepcopy(PICK)},
        "segments": [{"kind": "ptp", "to": "slot_above"}, {"kind": "ptp", "to": "slot"},
                     {"kind": "pick", "time": 1.0}, {"kind": "ptp", "to": "drop"},
                     {"kind": "place", "time": 1.0}]})
    depal["layout_position"] = [0.0, 1.6]
    doc["blocks"] += [depal, dispose("members")]
    doc["links"] = [k for k in doc["links"] if k["source"] != "palletizer"] + [
        {"source": "palletizer", "target": "depalletizer"},
        {"source": "depalletizer", "target": "members"},
        {"source": "depalletizer", "target": "carriers"}]
    assert not [p for p in schema.validate_model_structured(doc) if p.severity == "error"]
    depal["params"]["pattern"] = "P9"
    assert [(p.block, p.fields["case"]) for p in schema.validate_model_structured(doc)
            if p.code == "E_PALLET_UNKNOWN"] == [("depalletizer", "unknown")]
    depal["params"]["pattern"] = "P1"
    cycles, errors = slot_cycles(doc, depal)
    assert errors == [] and len(cycles) == 4
    env, _ = run_env(doc, 1000)
    boxes = sorted((e for e in env.ctx.all_entities if e.e_type == "BOX"), key=lambda e: e.deletion_time)
    assert [b.slot for b in boxes] == [4, 3, 2, 1]
    expected = np.cumsum([cycles[k - 1] for k in (4, 3, 2, 1)])
    assert [b.deletion_time for b in boxes] == pytest.approx(list(expected), abs=1e-9)
