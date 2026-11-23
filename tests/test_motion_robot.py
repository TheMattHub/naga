"""MOTION runs robot moves (ptp, lin): deterministic times in the cycle, problems before the run,
states mid-move, the cell frame and a robot placed as an asset."""
import copy

import numpy as np
import pytest

from conftest import problem_codes, run_env
from robotcases import IRB_RECORD, SEG, TARGETS, robot_cell

from nagasim.core import motion
from nagasim.core.document import ModelDocument
from nagasim.core.robotics.moves import cell_pose, places_of, robot_base
from nagasim.core.submodels import flatten
from nagasim.core.trace import TraceRecorder



def _cell(segments=SEG, targets=TARGETS, *, time_limit=60.0, every=6.0, **params):
    """robot_cell with one job every *every* seconds (no random arrivals)."""
    doc = robot_cell(segments, targets, **params)
    doc["blocks"][0]["params"] = {"distribution": "Constant", "dist_params": {"value": every}}
    doc["simulation"] = {**doc["simulation"], "time_limit": time_limit, "warmup": 0, "replications": 1}
    return doc


def _per_cycle(doc, run_len=60.0, **kw):
    _env, model = run_env(doc, run_len, **kw)
    block = model.blocks["process_1"]
    assert block.cycles > 0
    return block.cycle_time_total / block.cycles


def test_the_robot_cell_is_valid():
    assert problem_codes(_cell()) == set()


def test_the_cycle_is_the_robot_times_plus_the_timed_segments():
    # both moves are 90 deg on axis 1 only -> 1.15 s each; cycle 4.3 s
    assert _per_cycle(_cell()) == pytest.approx(4.3)
    doc = _cell()
    plan = motion.block_robot_plan(doc, doc["blocks"][1])
    assert motion.nominal_cycle_seconds(SEG, None, "seconds", "minutes",
                                        plan=plan) == pytest.approx(4.3)


def test_robot_moves_draw_no_random_numbers():
    timed = [dict(s) for s in SEG]
    timed[1]["time"], timed[3]["time"] = "TRIA(0.5,1,1.5)", "TRIA(0.8,1,1.6)"
    constant = [dict(s) for s in timed]
    constant[0], constant[2] = {"kind": "dwell", "time": 1.15}, {"kind": "dwell", "time": 1.15}

    def phases(doc):
        recorder = TraceRecorder()
        run_env(doc, 60.0, seed=3, subscribers=[recorder])
        return [(e.state, round(e.duration, 9)) for e in recorder.events
                if e.kind == "cycle_phase" and e.state in ("pick", "place")]

    robot, plain = phases(_cell(timed)), phases(_cell(constant))
    assert robot == plain and len(robot) > 10 and len({d for _s, d in robot}) > 2


def test_a_model_without_robots_runs_as_before():
    # Review Focus 3: tests/test_motion.py and tests/test_motion_oracle.py pass unchanged, and the
    # bundled models are proved identical by tools/regress_digest.py; here: no robot, no plan.
    doc = _cell()
    doc.pop("robots")
    for block in doc["blocks"]:
        if block["name"] == "process_1":
            block["params"] = {"action": "Seize Delay Release", "resource_name": "Server",
                               "profile": "Arm", "segments": [{"kind": "move", "distance_m": 0.8},
                                                              {"kind": "pick", "time": 1.0}]}
    doc["motion_profiles"] = [{"name": "Arm", "speed": 1.0, "accel": 2.0}]
    run_env(doc, 30.0)
    assert _per_cycle(doc, 30.0) == pytest.approx(1.3 + 1.0)


def _in_cell(**cycle):
    spec = {"stage": 0, "until": 10.5, "times": [1.15, 1.0, 1.15, 1.0], **cycle}
    return {"block": "process_1", "count": 1, "age": 5.0, "in_service": 1, "cycle": spec}


def test_a_state_mid_ptp_resumes_and_finishes_at_until():
    """Review Focus 4: the ptp in progress ends at 10.5; pick (1.0), the ptp back (1.15) and
    place (1.0) follow, so the job leaves at 13.65."""
    doc = _cell(time_limit=30.0)
    doc["blocks"][0]["params"]["start_time"] = 100.0        # no arrival in the window
    doc["initial_state"] = {"clock": 10.0, "entities": [_in_cell()]}
    assert problem_codes(doc) == set()
    recorder = TraceRecorder()
    run_env(doc, 30.0, subscribers=[recorder])
    phases = [(e.state, e.t) for e in recorder.events if e.kind == "cycle_phase"]
    assert phases == [("pick", pytest.approx(10.5)), ("ptp", pytest.approx(11.5)),
                      ("place", pytest.approx(12.65))]
    assert [e.t for e in recorder.events if e.kind == "entity_disposed"] == [pytest.approx(13.65)]


@pytest.mark.parametrize("at", [0.7, 1.6, 2.5, 6.3])
def test_a_state_taken_mid_robot_move_is_carried_exactly(at):
    from test_state import _carried
    taken = _carried(_cell(), at)
    assert any("cycle" in e for e in taken["entities"])


def test_a_state_whose_robot_time_disagrees_is_refused():
    doc = _cell(time_limit=30.0)
    doc["initial_state"] = {"clock": 10.0, "entities": [_in_cell(times=[1.15, 1.0, 9.9, 1.0])]}
    assert "E_INITIAL_CYCLE" in problem_codes(doc)
    doc["initial_state"] = {"clock": 10.0, "entities": [_in_cell(times=[1.15, 1.0, 1.15, 1.0])]}
    assert "E_INITIAL_CYCLE" not in problem_codes(doc)


def test_a_minutes_model_converts_robot_seconds():
    doc = _cell(time_limit=1.0, every=0.1)
    doc["simulation"]["time_unit"] = "minutes"
    assert _per_cycle(doc, 1.0) == pytest.approx(4.3 / 60)


ON_BELT = {"above_pick": {"on": "belt_1", "offset": [0, 0, 0.8]},
           "above_place": {"joints": [0, 0, 0, 0, 30, 0]}}


def _move_block(doc, name, dy):
    """South on the plan: the belt then sits at another angle from the robot's axis 1."""
    block = next(b for b in doc["blocks"] if b["name"] == name)
    block["layout_position"] = [block["layout_position"][0], block["layout_position"][1] + dy]


def test_moving_the_conveyor_on_the_plan_changes_the_cycle_time():
    doc = _cell(targets=ON_BELT)
    assert problem_codes(doc) == set()
    before = _per_cycle(doc)
    _move_block(doc, "belt_1", 0.5)
    after = _per_cycle(doc)
    assert after != pytest.approx(before)
    _move_block(doc, "belt_1", -0.5)
    assert _per_cycle(doc) == pytest.approx(before)


def test_a_frame_target_without_a_floor_plan_place_is_E_ROBOT_TARGET_case_place():
    doc = _cell(targets={**TARGETS, "above_pick": {"xyz": [12.0, 5.0, 0.8]}})
    assert problem_codes(doc) == set()
    del next(b for b in doc["blocks"] if b["name"] == "process_1")["layout_position"]
    found = [p for p in ModelDocument(doc).problems() if p.code == "E_ROBOT_TARGET"]
    assert [(p.fields["case"], p.fields["target"], p.path) for p in found] == [
        ("place", "above_pick", "/blocks/1/params/segments/0/to")]


def test_an_edited_axis_speed_changes_the_next_validation():
    params = _cell()["blocks"][1]["params"]
    record = copy.deepcopy(IRB_RECORD)
    first = motion.robot_plan({"IRB": record}, params, 1.0).moves[0].seconds
    record["axes"][0]["speed"] = 50
    assert motion.robot_plan({"IRB": record}, params, 1.0).moves[0].seconds > first + 0.5
    doc = _cell()
    doc["robots"][0]["axes"][0]["speed"] = 50
    assert _per_cycle(doc) > 4.3 + 1.0


def no_robot_named(doc):
    doc["blocks"][1]["params"]["robot"] = "Ghost"


def unknown_target(doc):
    doc["blocks"][1]["params"]["segments"][0]["to"] = "nowhere"


def frame_out_of_reach(doc):
    doc["blocks"][1]["params"]["targets"]["above_pick"] = {"xyz": [20.0, 5.0, 1.0]}


def lin_to_q5_zero(doc):
    doc["blocks"][1]["params"]["targets"]["above_place"] = {"joints": [0, 0, 0, 0, 0, 0]}
    doc["blocks"][1]["params"]["segments"][2]["kind"] = "lin"


def lin_with_wrist_flip(doc):
    doc["blocks"][1]["params"]["targets"]["above_place"] = {"joints": [90, 0, 0, 180, -30, 180]}
    doc["blocks"][1]["params"]["segments"][2]["kind"] = "lin"


def over_time_1s_for_1_15s(doc):
    doc["blocks"][1]["params"]["segments"][0]["time"] = 1.0


def payload_200kg(doc):
    doc["blocks"][1]["params"]["segments"][2]["payload_kg"] = 200


def hrc_collaborative(doc):
    doc["blocks"][1]["params"]["hrc"] = "collaborative"


@pytest.mark.parametrize("edit, code, path", [
    (no_robot_named, "E_ROBOT_UNKNOWN", "robot"), (unknown_target, "E_ROBOT_TARGET", "segments/0/to"),
    (frame_out_of_reach, "E_ROBOT_REACH", "segments/0/to"), (lin_to_q5_zero, "E_ROBOT_SINGULAR", "segments/2/to"),
    (lin_with_wrist_flip, "E_ROBOT_LIN", "segments/2/to"), (over_time_1s_for_1_15s, "E_ROBOT_MOVE_TIME", "segments/0/time"),
    (payload_200kg, "E_MOTION_PAYLOAD", "segments/2/payload_kg"), (hrc_collaborative, "E_MOTION_ZONE", "hrc"),
])
def test_each_robot_problem_comes_before_the_run(edit, code, path):
    doc = _cell()
    edit(doc)
    found = [p for p in ModelDocument(doc).problems() if p.code == code]
    assert [p.path for p in found] == [f"/blocks/1/params/{path}"], found
    assert found[0].block == "process_1"


def test_robot_moves_without_a_robot_are_E_ROBOT_UNKNOWN():
    doc = _cell()
    doc["blocks"][1]["params"]["robot"] = ""
    found = [p for p in ModelDocument(doc).problems() if p.code == "E_ROBOT_UNKNOWN"]
    assert len(found) == 1 and found[0].fields == {"robot": ""}


def test_a_profile_is_needed_only_for_move_segments():
    doc = _cell()
    assert "E_MOTION_PROFILE_UNKNOWN" not in problem_codes(doc)
    doc["blocks"][1]["params"]["segments"].append({"kind": "move", "distance_m": 0.5})
    assert "E_MOTION_PROFILE_UNKNOWN" in problem_codes(doc)


def _reports(segment, **kw):
    out = []
    motion.validate_segment(segment, 0, None, lambda code, text, path, hint, fields=None: out.append(
        (code, path)), **kw)
    return out


def test_tending_refuses_robot_kinds():
    # tending calls validate_segment without robot_kinds: ptp is an unknown kind there
    assert _reports({"kind": "ptp", "to": "a"}) == [("E_MOTION_SEGMENT", "segments/0/kind")]
    assert _reports({"kind": "ptp", "to": "a"}, robot_kinds=True) == []


def test_a_robot_move_time_must_be_a_number():
    assert _reports({"kind": "ptp", "to": "a", "time": "TRIA(1,2,3)"}, robot_kinds=True) == [
        ("E_MOTION_SEGMENT", "segments/0/time")]
    assert _reports({"kind": "lin", "to": "a", "time": -1}, robot_kinds=True) == [
        ("E_MOTION_SEGMENT", "segments/0/time")]
    assert _reports({"kind": "lin", "to": "a", "speed": 2}, robot_kinds=True) == [
        ("E_MOTION_SEGMENT", "segments/0/speed")]
    doc = _cell()
    doc["blocks"][1]["params"]["segments"][0]["time"] = "TRIA(1,2,3)"
    assert "E_MOTION_SEGMENT" in problem_codes(doc)


def _asset_model(instance_place=True):
    """The robot cell as an asset: the MOTION block inside a definition, the instance placed."""
    doc = _cell()
    cell = next(b for b in doc["blocks"] if b["name"] == "process_1")
    inner = {k: v for k, v in cell.items() if k not in ("layout_position", "layout_rotation")}
    inner["name"] = "robot"
    ports = [{"name": "entry", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
             {"name": "exit", "type": "SUB_EXIT", "position": [2, 0], "params": {"port": "out"}}]
    doc["submodels"] = {"RobotCell": {"blocks": [ports[0], inner, ports[1]],
                                      "links": [{"source": "entry", "target": "robot"},
                                                {"source": "robot", "target": "exit"}]}}
    instance = {"name": "cell_1", "type": "SUBMODEL", "position": cell["position"],
                "params": {"definition": "RobotCell"}}
    if instance_place:
        instance.update(layout_position=[10.0, 5.0], layout_rotation=90.0)
    doc["blocks"] = [instance if b is cell else b for b in doc["blocks"]]
    for link in doc["links"]:
        for end in ("source", "target"):
            link[end] = "cell_1" if link[end] == "process_1" else link[end]
    return doc


def test_a_robot_placed_as_an_asset_gets_its_instances_base_pose():
    flat = flatten(_asset_model())
    assert np.allclose(robot_base(flat, "cell_1/robot"), cell_pose(10.0, 5.0, 90.0))
    assert places_of(flat)["belt_1"].length == 4.0
    doc = _asset_model()
    next(b for b in doc["submodels"]["RobotCell"]["blocks"] if b["name"] == "robot")["params"]["targets"]["above_pick"] = {
        "xyz": [12.0, 5.0, 0.8]}
    assert problem_codes(doc) == set()
    assert robot_base(flatten(_asset_model(instance_place=False)), "cell_1/robot") is None


def test_blocks_outside_a_placed_instance_are_flattened_as_before():
    flat = flatten(_asset_model())
    outside = [b for b in flat["blocks"] if "/" not in b["name"]]
    assert outside and all("instance_place" not in b for b in outside)
    unplaced = flatten(_asset_model(instance_place=False))
    assert all("instance_place" not in b for b in unplaced["blocks"])
