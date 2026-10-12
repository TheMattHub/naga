"""MOTION block and motion profiles (automation sub-project 1, D156-D158)."""
import math

import pytest

from nagasim.core import motion

UR10E = {"name": "UR10e", "kind": "robot", "speed": 1.0, "accel": 2.0,
         "payload_max": 12.5, "payload_derate": 0.6, "collaborative_speed": 0.25}


def test_move_time_is_trapezoidal_when_the_move_reaches_full_speed():
    # v^2/a = 0.5 m, so 0.8 m reaches 1 m/s: 0.8/1 + 1/2
    assert motion.move_time(0.8, 1.0, 2.0) == pytest.approx(1.3)


def test_move_time_is_triangular_below_v_squared_over_a_and_continuous_at_it():
    assert motion.move_time(0.2, 1.0, 2.0) == pytest.approx(2 * math.sqrt(0.1))
    assert motion.move_time(0.5, 1.0, 2.0) == pytest.approx(1.0)      # both formulas agree
    assert motion.move_time(0.0, 1.0, 2.0) == 0.0


def test_payload_derates_linearly_to_the_floor_at_payload_max():
    assert motion.derate(UR10E, 0) == 1.0
    assert motion.derate(UR10E, 6.25) == pytest.approx(0.8)
    assert motion.derate(UR10E, 12.5) == pytest.approx(0.6)            # boundary: exactly the floor
    assert motion.derate({"speed": 1.0, "accel": 1.0}, 50) == 1.0      # no payload_max: no derating
    assert motion.derate({**UR10E, "payload_derate": None}, 12.5) == 1.0


def test_collaborative_speed_replaces_the_speed_and_is_still_derated():
    assert motion.move_speed(UR10E, 0, collaborative=True) == pytest.approx(0.25)
    assert motion.move_speed(UR10E, 12.5, collaborative=True) == pytest.approx(0.15)
    assert motion.move_speed(UR10E, 5) == pytest.approx(1.0 - 0.4 * 0.4)


def test_nominal_cycle_uses_means_and_nominal_speed():
    segments = [{"kind": "move", "distance_m": 0.8},
                {"kind": "pick", "time": "TRIA(0.8,1.0,1.4)"},
                {"kind": "move", "distance_m": 0.8, "payload_kg": 5},
                {"kind": "place", "time": "1.0"},
                {"kind": "process", "time": "NORM(12,1)"}]
    loaded = motion.move_time(0.8, motion.move_speed(UR10E, 5), 2.0)
    assert motion.nominal_cycle_seconds(segments, UR10E) == pytest.approx(
        1.3 + 3.2 / 3 + loaded + 1.0 + 12.0)


def test_nominal_cycle_converts_units_and_gives_up_on_what_needs_a_run():
    assert motion.nominal_cycle_seconds([{"kind": "dwell", "time": "2"}], None, "minutes") == 120.0
    assert motion.nominal_cycle_seconds([{"kind": "dwell", "time": "2"}], None, "base",
                                        "hours") == 7200.0
    assert motion.nominal_cycle_seconds([{"kind": "dwell", "time": "entity.size"}], None) is None
    assert motion.nominal_cycle_seconds([{"kind": "move", "distance_m": 1}], None) is None


from nagasim.core import schema
from nagasim.core.document import ModelDocument
from nagasim.modelschema import build_json_schema

from conftest import chain, create, dispose, layout, problem_codes, run_env

NET = {"nodes": [{"name": "cell", "x": 0, "y": 0}, {"name": "near", "x": 1.5, "y": 0},
                 {"name": "far", "x": 10, "y": 0}, {"name": "island", "x": 0, "y": 1}],
       "segments": [{"from": "cell", "to": "near"}, {"from": "cell", "to": "far"},
                    # one-way: island is reachable FROM cell, cell is not from island
                    {"from": "cell", "to": "island", "two_way": False}]}
CYCLE = [{"kind": "move", "distance_m": 0.8}, {"kind": "pick", "time": "1.0"},
         {"kind": "move", "distance_m": 0.8, "payload_kg": 5}, {"kind": "place", "time": "0.5"},
         {"kind": "process", "time": "2"}]


def motion_block(name="cell", **params):
    base = {"action": "Seize Delay Release", "resource_name": "Robot", "profile": "UR10e",
            "hrc": "none", "segments": [dict(s) for s in CYCLE]}
    base.update(params)
    return {"name": name, "description": name, "type": "MOTION", "position": [1, 0],
            "params": base}


def motion_codes(doc):
    return {p.code for p in schema.validate_layout_structured(doc) if p.code.startswith("E_MOTION")}


def _valid():
    blocks = [create(max_arrivals=1), motion_block(), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Robot", "capacity": 1}],
                  motion_profiles=[dict(UR10E)], network=NET)


def _block(doc):
    return doc["blocks"][1]["params"]


def test_a_valid_motion_model_has_no_motion_problem():
    doc = _valid()
    _block(doc).update(segments=[{"kind": "move", "distance_m": 1, "payload_kg": 12.5}],
                       hrc="separation", zone={"node": "cell", "radius_m": 1.5})
    assert motion_codes(doc) == set()


@pytest.mark.parametrize("change, code", [
    (lambda d: _block(d).update(profile="Nobody"), "E_MOTION_PROFILE_UNKNOWN"),
    (lambda d: _block(d).update(segments=[{"kind": "fly", "time": "1"}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "move"}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "pick"}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "move", "distance_m": -1}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "dwell", "time": -2}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "dwell", "time": "-2"}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "pick", "distance_m": 1}]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[]), "E_MOTION_SEGMENT"),
    (lambda d: _block(d).update(segments=[{"kind": "move", "distance_m": 1, "payload_kg": 13}]),
     "E_MOTION_PAYLOAD"),
    (lambda d: _block(d).update(hrc="separation", zone={"node": "nowhere", "radius_m": 1}),
     "E_MOTION_ZONE"),
    (lambda d: _block(d).update(hrc="separation", zone={"node": "cell", "radius_m": 0}),
     "E_MOTION_ZONE"),
    (lambda d: _block(d).update(hrc="sometimes"), "E_MOTION_ZONE"),
    (lambda d: d["motion_profiles"][0].update(speed=0), "E_MOTION_PROFILE"),
    (lambda d: d["motion_profiles"][0].update(accel=-1), "E_MOTION_PROFILE"),
    (lambda d: d["motion_profiles"][0].update(payload_derate=1.5), "E_MOTION_PROFILE"),
    (lambda d: d["motion_profiles"].append(dict(UR10E)), "E_MOTION_PROFILE"),
    (lambda d: (d["motion_profiles"][0].pop("collaborative_speed"),
                _block(d).update(hrc="collaborative")), "E_MOTION_PROFILE"),
])
def test_each_motion_problem_has_its_code(change, code):
    doc = _valid()
    change(doc)
    assert code in motion_codes(doc)


def test_motion_profiles_is_a_record_section_the_document_saves():
    doc = ModelDocument()
    doc.set_motion_profiles([dict(UR10E)])
    assert doc.to_layout()["motion_profiles"] == [UR10E]
    assert schema.migrate({"schema_version": schema.CURRENT_VERSION})["motion_profiles"] == []
    assert build_json_schema()["$defs"]["motion_profile"]["required"] == ["name", "speed", "accel"]


def test_the_run_context_carries_the_profiles_by_name():
    blocks = [create(max_arrivals=1), dispose()]
    env, _model = run_env(layout(blocks, chain(*blocks), motion_profiles=[dict(UR10E)]), 1)
    assert env.ctx.motion_profiles == {"UR10e": {**UR10E, "speed_unit": "m/s",
                                                 "payload_unit": "kg"}}   # v28 fills them


from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder

SIM_S = {"time_limit": 1000, "warmup": 0, "replications": 1, "time_unit": "seconds"}
LOADED = motion.move_time(0.8, motion.move_speed(UR10E, 5), 2.0)


def cell_model(segments=CYCLE, *, fleets=(), extra_blocks=(), extra_links=(), sim=SIM_S,
               arrivals=1, **params):
    blocks = [create(max_arrivals=arrivals, dist_params={"value": 100.0}),
              motion_block(segments=[dict(s) for s in segments], **params), dispose()]
    return layout(blocks + list(extra_blocks), chain(*blocks) + list(extra_links),
                  resources=[{"name": "Robot", "capacity": 1}], motion_profiles=[dict(UR10E)],
                  network=NET, fleets=list(fleets), simulation=dict(sim))


def phases(doc, run_len=1000):
    recorder = TraceRecorder()
    run_env(doc, run_len, subscribers=[recorder])
    return [e for e in recorder.events if e.kind == "cycle_phase"], recorder.events


def test_the_service_is_the_segment_times_in_order_and_each_start_is_traced():
    found, _ = phases(cell_model())
    assert [(e.state, int(e.value)) for e in found] == [
        ("move", 0), ("pick", 1), ("move", 2), ("place", 3), ("process", 4)]
    durations = [1.3, 1.0, LOADED, 0.5, 2.0]
    assert [e.duration for e in found] == pytest.approx(durations)
    starts = [e.t - found[0].t for e in found]
    assert starts == pytest.approx([sum(durations[:i]) for i in range(5)])


def test_collaborative_mode_moves_every_segment_at_the_collaborative_speed():
    found, _ = phases(cell_model(hrc="collaborative"))
    moves = [e.duration for e in found if e.state == "move"]
    assert moves == pytest.approx([motion.move_time(0.8, 0.25, 2.0),
                                   motion.move_time(0.8, motion.move_speed(UR10E, 5, True),
                                                    2.0)])


@pytest.mark.parametrize("home, collaborative", [("near", True), ("far", False),
                                                  ("island", False)])
def test_separation_counts_a_worker_at_the_radius_and_not_one_that_cannot_reach_it(
        home, collaborative):
    """Review focus 2: `near` is exactly radius_m (1.5) away -> inside; `far` is
    10 away; `island` has no path to the zone node (distance inf) -> outside."""
    fleet = {"name": "Ops", "kind": "worker", "speed": 1.0, "home": home,
             "units": [{"name": "Ann"}]}
    found, _ = phases(cell_model(fleets=[fleet], hrc="separation",
                                 zone={"node": "cell", "radius_m": 1.5}))
    assert found[0].duration == pytest.approx(
        motion.move_time(0.8, 0.25, 2.0) if collaborative else 1.3)


def test_the_cycle_is_the_same_physical_time_in_a_minutes_model():
    """Review focus 3: moves are seconds, segment times are in the block's
    time_unit (default seconds); both land in the model's base unit."""
    seconds = [e.duration for e in phases(cell_model())[0]]
    minutes = [e.duration for e in phases(cell_model(sim={**SIM_S, "time_unit": "minutes"}),
                                          run_len=20)[0]]
    assert minutes == pytest.approx([d / 60 for d in seconds])
    as_base = [e.duration for e in phases(cell_model(time_unit="base"))[0]]
    assert as_base == pytest.approx(seconds)


@pytest.mark.parametrize("on_preempt, starts, indices", [
    ("resume", [0.0, 1.5, 4.0], [0, 1, 2]),
    ("restart", [0.0, 1.5, 3.0, 4.5, 6.0], [0, 1, 0, 1, 2]),
])
def test_a_preempted_cycle_resumes_the_segment_in_progress(on_preempt, starts, indices):
    """Review focus 1: three 1.5 s dwells from t=0; an urgent 1 s job preempts
    the robot at t=2. "resume" finishes dwell 1's remaining 1.0 at t=4 without
    re-tracing it; "restart" runs the whole cycle again from t=3."""
    urgent_src = create("urgent_src", max_arrivals=1, start_time=2)
    urgent = {"name": "urgent", "description": "urgent", "type": "PROCESS", "position": [1, 1],
              "params": {"action": "Seize Delay Release", "distribution": "Constant",
                         "dist_params": {"value": 1.0}, "priority": 0, "quantity": 1,
                         "resource_name": "Robot", "preempt": True}}
    doc = cell_model([{"kind": "dwell", "time": "1.5"}] * 3, priority=1, on_preempt=on_preempt,
                     extra_blocks=[urgent_src, urgent],
                     extra_links=[{"source": "urgent_src", "target": "urgent"},
                                  {"source": "urgent", "target": "dispose_1"}])
    assert not [p for p in schema.validate_model_structured(doc) if p.severity == "error"]
    recorder = TraceRecorder()
    env, model = run_env(doc, 100, subscribers=[recorder])
    found = [e for e in recorder.events if e.kind == "cycle_phase"]
    assert [e.t for e in found] == pytest.approx(starts)
    assert [int(e.value) for e in found] == indices
    cell = model.blocks["cell"]
    assert cell.cycles == 1 and cell.motion_stats == {"dwell": [3, pytest.approx(4.5)]}


def test_cycle_phase_events_only_add_the_phases_view_to_playback():
    _found, events = phases(cell_model())
    without = [e for e in events if e.kind != "cycle_phase"]
    state, plain = Timeline(events).state_at(2.0), Timeline(without).state_at(2.0)
    assert state.pop("phases") and plain.pop("phases") == {}
    assert state.pop("phases_all") and plain.pop("phases_all") == {}
    state.pop("placed"), plain.pop("placed")        # counted from the same events
    assert state == plain


def test_a_segment_time_expression_is_compiled_at_validation():
    doc = cell_model([{"kind": "pick", "time": "TRIA(1, 2"}])
    assert "E_EXPRESSION" in {p.code for p in schema.validate_layout_structured(doc)}


from nagasim import api, experiments


def _frame(doc, **run):
    results = api.run_model(doc, replications=1, seed=0, **run)
    return results, results["motion_summary"].set_index("Motion")


def test_motion_summary_reports_time_per_kind_the_breakdown_and_collaboration():
    results, frame = _frame(cell_model(arrivals=3), run_length=1000)
    cycle = 1.3 + 1.0 + LOADED + 0.5 + 2.0
    assert frame.loc["cell/cycle", "Segments"] == 3
    assert frame.loc["cell/cycle", "Mean Time"] == pytest.approx(cycle)
    assert frame.loc["cell/move", "Segments"] == 6
    assert frame.loc["cell/move", "Mean Time"] == pytest.approx((1.3 + LOADED) / 2)
    assert frame.loc["cell/move", "Collaborative %"] == 0.0
    kinds = frame[frame["Segment Kind"] != "cycle"]
    assert kinds["Cycle Share %"].sum() == pytest.approx(100.0)
    flat = experiments.flatten_kpis(results)
    assert flat.loc[flat["KPI"] == "motion/cell/cycle/Mean Time", "Value"].iloc[0] == \
        pytest.approx(cycle)


def test_motion_stats_reset_at_warmup_and_use_means_when_deterministic():
    """Review focus 4: jobs every 10 s, a 4.8 s cycle, warm-up 50 s: the cycles
    completing at 50 + 4.8, ..., 90 + 4.8 count (5); deterministic mode turns
    TRIA(1,2,6) into its mean 3."""
    segments = [{"kind": "dwell", "time": "1.8"}, {"kind": "pick", "time": "TRIA(1,2,6)"}]
    doc = cell_model(segments, arrivals=10)
    doc["blocks"][0]["params"]["dist_params"] = {"value": 10.0}
    _results, frame = _frame(doc, run_length=100, warmup=50, deterministic=True)
    assert frame.loc["cell/cycle", "Segments"] == 5
    assert frame.loc["cell/pick", "Mean Time"] == pytest.approx(3.0)
    assert frame.loc["cell/cycle", "Mean Time"] == pytest.approx(4.8)


def test_a_model_without_motion_gets_an_empty_frame_with_its_columns():
    blocks = [create(max_arrivals=1), dispose()]
    results = api.run_model(layout(blocks, chain(*blocks)), replications=1, run_length=5)
    assert results["motion_summary"].empty
    assert "Collaborative %" in results["motion_summary"].columns


def test_a_motion_asset_binds_its_distances_and_validates_them_after_flattening():
    """Review focus 5: `{"$arg": "walk_m"}` inside `segments` flattens into a
    number, and a negative argument is caught after flattening."""
    libs = pytest.importorskip("product.libraries")
    raw = libs.load_library(libs.builtin_dir() / "manufacturing.stratlib.json")
    asset = next(a for a in raw["assets"] if a["id"] == "manual_assembly_motion")
    test = dict(asset["tests"][0])
    assert problem_codes(libs.harness_layout(asset, test)) == set()
    bad = {**test, "arguments": {**test["arguments"], "walk_m": -1.0}}
    assert "E_MOTION_SEGMENT" in problem_codes(libs.harness_layout(asset, bad))
    assert libs.run_tests(raw, "manual_assembly_motion")[0]["passed"]
