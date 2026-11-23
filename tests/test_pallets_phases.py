"""A robot BATCH's cycle phases (blocks-in-3D, Task 5): each slot traces one `cycle_phase` per
segment of its plan, as MOTION does, so playback can pose a palletizer's robot; the run's
results do not change."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from robotcases import gantry_pallet_cell
from test_pallets_robot import SEGMENTS, cell

from nagasim import api
from nagasim.core import pallets
from nagasim.core.blocks import batch as batch_module
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder

MODEL = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "palletizing_line.json"
KINDS = ["ptp", "pick", "ptp", "ptp", "place", "ptp"]


def bundled() -> dict:
    return json.loads(MODEL.read_text(encoding="utf-8"))


SHEET = 3.0        # seconds, the models' time unit


def sheeted_bundled() -> dict:
    """The bundled line (two layers) with a sheet before layer 2: the belt-pick path."""
    doc = bundled()
    doc["pallet_patterns"][0]["sheet"] = {"time": SHEET}
    return doc


def sheeted_cell() -> dict:
    """`cell()` stacked two layers high with a sheet between: the fixed-pick path."""
    doc, palletizer = cell()
    record = doc["pallet_patterns"][0]
    record["stack"], record["limits"] = [{"layer": "A"}, {"layer": "A"}], {"layers": 2}
    record["sheet"] = {"time": SHEET}
    palletizer["params"]["batch_size"] = 8
    next(b for b in doc["blocks"] if b["name"] == "boxes")["params"]["entities_per_arrival"] = 8
    return doc


def layered_cell() -> dict:
    """`sheeted_cell()` placing a layer at a time."""
    doc = sheeted_cell()
    next(b for b in doc["blocks"] if b["name"] == "palletizer")["params"]["place_per"] = "layer"
    return doc


def traced(doc, run_length=None):
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, run_length=run_length, trace=recorder)
    return results, recorder.events


def phases_of(events, block):
    """{member id: [(t, kind, index, duration)]} of *block*'s cycle_phase events."""
    out: dict = {}
    for e in events:
        if e.kind == "cycle_phase" and e.block == block:
            out.setdefault(e.entity_id, []).append((e.t, e.state, int(e.value), e.duration))
    return out


def absorbed(events, block):
    return {e.entity_id: e.t for e in events if e.kind == "entity_absorbed" and e.block == block}


@pytest.mark.parametrize("doc", [bundled(), cell()[0], gantry_pallet_cell()],
                         ids=["palletizing_line", "fixed_pick", "belt_pick"])
def test_each_slot_s_phases_sum_to_its_cycle(doc):
    cycles, phases = pallets.robot_cycles(doc), pallets.robot_phases(doc)
    assert set(phases) == set(cycles) == {"palletizer"}
    assert len(phases["palletizer"]) == len(cycles["palletizer"])
    for cycle, plan in zip(cycles["palletizer"], phases["palletizer"]):
        assert [kind for kind, _s in plan] == [s["kind"] for s in
                                              next(b for b in doc["blocks"] if b["name"] ==
                                                   "palletizer")["params"]["segments"]]
        assert abs(sum(s for _k, s in plan) - cycle) <= 1e-9


def test_slot_plans_give_one_cycle_plan_per_slot_in_slot_order():
    """What the 3D view poses a palletizer's robot from (blocks in 3D, Task 8)."""
    from nagasim.core.robotics.moves import CyclePlan
    doc = bundled()
    block = next(b for b in doc["blocks"] if b["name"] == "palletizer")
    plans = pallets.slot_plans(doc, block)
    record = pallets.pattern_records(doc)[pallets.robot_block_pattern(doc, block)]
    assert len(plans) == len(pallets.slot_cycles(doc, block)[0]) == len(pallets.slots(record))
    assert all(isinstance(p, CyclePlan) and not p.errors and p.moves for p in plans)
    # Each slot's plan is the one its phases were timed from.
    for plan, phases in zip(plans, pallets.slot_phases(doc, block)):
        assert all(plan.moves[i].seconds == phases[i][1] for i in plan.moves)
    assert pallets.slot_plans(doc, {"name": "x", "params": {}}) == []


def test_the_bundled_palletizer_traces_its_phases_and_its_place_count():
    _results, events = traced(bundled(), run_length=120)
    per_member = phases_of(events, "palletizer")
    done = absorbed(events, "palletizer")
    assert done, "the palletizer placed boxes in 120 s"
    for member, rows in per_member.items():
        assert [(kind, index) for _t, kind, index, _d in rows] == \
            list(zip(KINDS, range(6)))[:len(rows)]
        if member in done:      # a finished slot: the segments run back to back to its end
            assert len(rows) == 6
            for (t, _k, _i, d), (t_next, *_rest) in zip(rows, rows[1:]):
                assert t + d == pytest.approx(t_next, abs=1e-9)
            t, _k, _i, d = rows[-1]
            assert t + d == pytest.approx(done[member], abs=1e-9)
    # Playback's `placed` counts the place segments begun: each placed box, and at most the
    # one in progress.
    placed = Timeline(events).state_at(120.0)["placed"].get("palletizer", 0)
    begun = {m for m, rows in per_member.items() if any(k == "place" for _t, k, _i, _d in rows)}
    assert set(done) <= begun and len(begun - set(done)) <= 1
    assert placed == len(begun)


def test_playback_poses_the_palletizer_mid_segment_with_one_arm():
    doc = gantry_pallet_cell()
    _results, events = traced(doc)
    rows = phases_of(events, "palletizer")
    assert len(rows) == 2 and all(len(r) == 6 for r in rows.values())
    timeline = Timeline(events)
    for member, segs in rows.items():
        for t, kind, index, d in segs:
            if d <= 0:
                continue
            state = timeline.state_at(t + d / 2)
            assert state["phases"]["palletizer"][:2] == (kind, index)
            assert len(state["phases_all"]["palletizer"]) == 1


def test_a_fixed_pick_slot_runs_its_planned_segments_to_the_slot_s_end():
    doc, _batch = cell()
    _results, events = traced(doc, run_length=100)
    rows, done = phases_of(events, "palletizer"), absorbed(events, "palletizer")
    assert len(rows) == 4 and set(rows) == set(done)
    plans = pallets.robot_phases(doc)["palletizer"]
    # The slots fill in order (one layer, no sheet), so the k-th member started is slot k's.
    for slot, member in enumerate(sorted(rows, key=lambda m: rows[m][0][0])):
        segs = rows[member]
        assert [k for _t, k, _i, _d in segs] == [s["kind"] for s in SEGMENTS]
        assert [d for *_x, d in segs] == pytest.approx([s for _k, s in plans[slot]])
        t, *_x, d = segs[-1]
        assert t + d == pytest.approx(done[member], abs=1e-9)


@pytest.mark.parametrize("doc", [sheeted_cell(), sheeted_bundled()],
                         ids=["fixed_pick", "belt_pick"])
def test_segment_0_of_a_slot_opening_a_layer_takes_the_sheet_time(doc):
    _results, events = traced(doc, run_length=200)
    rows, done = phases_of(events, "palletizer"), absorbed(events, "palletizer")
    plans = pallets.robot_phases(doc)["palletizer"]
    openers = 0
    # One pallet place, so slots fill in order: the k-th member started takes slot k % 8.
    for k, member in enumerate(sorted(rows, key=lambda m: rows[m][0][0])):
        segs, plan = rows[member], plans[k % len(plans)]
        extra = segs[0][3] - plan[0][1]
        assert extra == pytest.approx(0.0, abs=1e-9) or extra == pytest.approx(SHEET, abs=1e-9)
        if k % len(plans) == 4:         # slot 5 opens layer 2
            assert extra == pytest.approx(SHEET, abs=1e-9)
            openers += 1
        if member in done:
            t, *_x, d = segs[-1]
            assert t + d == pytest.approx(done[member], abs=1e-9)
    assert openers >= 1


def test_a_layer_at_a_time_counts_every_box_it_places():
    _results, events = traced(layered_cell(), run_length=300)
    done = absorbed(events, "palletizer")
    assert len(done) == 8
    timeline = Timeline(events)
    assert timeline.state_at(300.0)["placed"] == {"palletizer": 8}
    # Mid-place of a layer: one running phase, one arm drawn.
    first = min((e for e in events if e.kind == "cycle_phase" and e.state == "place"
                 and e.duration > 0), key=lambda e: e.t)
    state = timeline.state_at(first.t + first.duration / 2)
    assert state["phases"]["palletizer"][:2] == ("place", 4)
    assert len(state["phases_all"]["palletizer"]) == 1


@pytest.mark.parametrize("doc", [bundled(), cell()[0], gantry_pallet_cell(), sheeted_cell(),
                                 sheeted_bundled(), layered_cell()],
                         ids=["palletizing_line", "fixed_pick", "belt_pick", "sheeted_fixed",
                              "sheeted_belt", "layer_at_a_time"])
def test_the_phases_leave_every_result_frame_as_it_was(doc, monkeypatch):
    # Traced, so the phases are emitted and not only timed.
    on = api.run_model(doc, replications=1, run_length=300, keep_empty=True,
                       trace=TraceRecorder())
    monkeypatch.setattr(batch_module, "EMIT_PHASES", False)
    off = api.run_model(doc, replications=1, run_length=300, keep_empty=True)
    assert set(on) == set(off)
    for name in on:
        pd.testing.assert_frame_equal(on[name], off[name], check_exact=True, obj=name)
