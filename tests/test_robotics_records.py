"""The `robots` model section: the record shape, its checks and the run's robots."""
import copy
import json

import numpy as np
import pytest

from robotcases import IRB_RECORD, ROOT, irb_robot

from nagasim.agent_tools.records import RECORD_TOOL_SECTIONS
from nagasim.core import schema
from nagasim.core.document import ModelDocument
from nagasim.core.robotics.chain import fk
from nagasim.core.robotics.records import robot_from_record
from nagasim.core.utils import cache_key
from nagasim.modelschema import build_json_schema


def single_server_with(**sections) -> dict:
    doc = json.loads((ROOT / "nagasim" / "models" / "single_server.json").read_text(encoding="utf-8"))
    doc.update(copy.deepcopy(sections))
    return doc


def robot_problems(layout) -> list:
    return [p for p in schema.validate_model_structured(layout) if p.code.startswith("E_ROBOT")]


def test_a_valid_robot_has_no_problem_and_builds_its_chain():
    layout = single_server_with(robots=[IRB_RECORD])
    assert robot_problems(layout) == []
    q = [10, 20, -30, 40, 50, 60]
    assert np.allclose(fk(robot_from_record(IRB_RECORD), q), fk(irb_robot(), q), atol=1e-12)


def test_the_record_carries_payload_tool_and_home():
    record = {**copy.deepcopy(IRB_RECORD), "payload_derate": 0.5, "tcp_speed": 2.0, "tcp_accel": 4.0,
              "home": [0, 10, 0, 0, 30, 0], "reach_m": 2.6, "approximate": True, "source": "test"}
    robot = robot_from_record(record)
    assert (robot.payload_max, robot.payload_derate, robot.tcp_speed, robot.tcp_accel) == (175, 0.5, 2.0, 4.0)
    assert list(robot.home) == [0, 10, 0, 0, 30, 0] and robot.reach_m == 2.6
    assert np.allclose(robot.tool[:3, 3], [0, 0, 0.2])
    assert robot_from_record({**record, "tcp_speed": 60, "speed_unit": "m/min"}).tcp_speed == pytest.approx(1.0)


def test_a_chain_record_builds_from_its_joints():
    record = {"name": "slide", "structure": "chain",
              "joints": [{"name": "x", "type": "prismatic", "origin": [0, 0, 0], "axis": [1, 0, 0]}],
              "axes": [{"name": "x", "range": [0, 2], "speed": 1, "accel": 2}]}
    robot = robot_from_record(record)
    assert robot.structure == "chain" and np.allclose(fk(robot, [1.5])[:3, 3], [1.5, 0, 0])


@pytest.mark.parametrize("change, text", [
    (lambda r: r.update(structure="arm9"), "unknown structure"),
    (lambda r: r["axes"].pop(), "6 axes"),
    (lambda r: r["axes"][1].update(range=[85, -65]), "range"),
    (lambda r: r["axes"][0].update(speed=0), "speed"),
    (lambda r: r["axes"][0].pop("accel"), "accel"),
    (lambda r: r["params"].pop("forearm"), "forearm"),
    (lambda r: r.update(home=[0, 200, 0, 0, 0, 0]), "home"),
    (lambda r: r.update(joints=[]), "joints"),        # joints only with structure "chain"
    (lambda r: r.update(payload_derate=1.5), "payload_derate"),
    (lambda r: r.update(payload_max=-1), "payload_max"),
    (lambda r: r.update(tool={"xyz": [0, 0]}), "tool"),
    (lambda r: r.update(colour="red"), "colour"),
])
def test_each_bad_robot_is_E_ROBOT_with_its_reason(change, text):
    record = copy.deepcopy(IRB_RECORD)
    change(record)
    with pytest.raises(ValueError, match=text):
        robot_from_record(record)
    found = robot_problems(single_server_with(robots=[record]))
    assert [p.code for p in found] == ["E_ROBOT"]
    assert text in found[0].message and found[0].fields["case"] == "said"
    assert found[0].path.startswith("/robots/0")


def test_a_chain_record_needs_its_joints():
    record = {"name": "c", "structure": "chain", "axes": [{"name": "x", "range": [0, 1], "speed": 1, "accel": 1}]}
    with pytest.raises(ValueError, match="joints"):
        robot_from_record(record)


def test_a_duplicate_robot_name_is_E_ROBOT_case_name():
    found = robot_problems(single_server_with(robots=[IRB_RECORD, IRB_RECORD]))
    assert [(p.code, p.fields) for p in found] == [
        ("E_ROBOT", {"case": "name", "index": 1, "name": "IRB"})]
    assert found[0].path == "/robots/1/name"


def test_robots_is_a_record_section_the_document_saves_and_record_tools_list():
    assert "robots" in RECORD_TOOL_SECTIONS
    doc = ModelDocument(single_server_with(robots=[IRB_RECORD]))
    assert doc.to_layout()["robots"] == [IRB_RECORD]
    assert "robot" in build_json_schema()["$defs"]


def test_the_cache_key_of_a_record_is_its_content():
    edited = copy.deepcopy(IRB_RECORD)
    assert cache_key(edited) == cache_key(json.loads(json.dumps(IRB_RECORD)))
    edited["axes"][0]["speed"] = 50
    assert cache_key(edited) != cache_key(IRB_RECORD)
