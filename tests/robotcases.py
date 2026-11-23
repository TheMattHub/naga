"""Shared robot test cases: template parameter sets, the IRB-like test arm, a
gantry, the datasheet spec and a robot cell model. Made-up values: the arm has
the geometry of an IRB 6700-200/2.60 and synthetic speeds, so exact times in
tests never depend on the shipped asset or on the default acceleration ramp."""
import copy
import dataclasses
import json
from pathlib import Path

import numpy as np

from nagasim.core.robotics.templates import template_robot

ROOT = Path(__file__).resolve().parents[1]

CASES = {
    "serial6": dict(params=dict(base_height=0.78, shoulder_offset=0.32, upper_arm=1.125, elbow_offset=0.2,
                                forearm=1.1425, wrist_flange=0.2),
                    ranges=[(-170, 170), (-65, 85), (-180, 70), (-300, 300), (-130, 130), (-360, 360)]),
    "palletizer4": dict(params=dict(base_height=0.9, shoulder_offset=0.25, upper_arm=1.3, forearm=1.4,
                                    wrist_drop=0.3),
                        ranges=[(-180, 180), (-40, 85), (-20, 120), (-300, 300)]),
    "scara4": dict(params=dict(base_height=0.4, arm1=0.35, arm2=0.25, stroke=0.2, flange_drop=0.05),
                   ranges=[(-130, 130), (-145, 145), (0.0, 0.2), (-360, 360)]),
    "delta": dict(params=dict(mount_height=1.5, base_radius=0.2, effector_radius=0.05, upper_arm=0.35,
                              forearm=0.8),
                  ranges=[(-40, 90)] * 3),
    "gantry": dict(params=dict(mount_height=2.5), ranges=[(0, 3.0), (0, 2.0), (0, 1.0)]),
    "serial7": dict(params=dict(base_height=0.34, upper_arm=0.4, forearm=0.4, wrist_flange=0.126),
                    ranges=[(-170, 170), (-120, 120)] * 3 + [(-175, 175)]),
}


def case_robot(structure: str, **kw):
    """The CASES robot of a structure: speed 100 and accel 400 on every axis."""
    c = CASES[structure]
    axes = [{"range": list(r), "speed": 100.0, "accel": 400.0} for r in c["ranges"]]
    return template_robot(structure, c["params"], axes, **kw)


IRB_DIMS = dict(CASES["serial6"]["params"])
IRB_RANGES = [tuple(r) for r in CASES["serial6"]["ranges"]]
IRB_SPEEDS = [100, 90, 90, 170, 120, 190]
IRB_TOOL = {"xyz": [0, 0, 0.2], "rpy_deg": [0, 0, 0]}
IRB_AXES = [{"name": f"a{i + 1}", "range": list(r), "speed": s, "accel": 4 * s}
            for i, (r, s) in enumerate(zip(IRB_RANGES, IRB_SPEEDS))]

IRB_RECORD = {"name": "IRB", "structure": "serial6", "params": dict(IRB_DIMS),
              "axes": copy.deepcopy(IRB_AXES), "tool": dict(IRB_TOOL), "payload_max": 175}


def irb_robot(payload_derate=1.0):
    r = template_robot("serial6", IRB_DIMS, copy.deepcopy(IRB_AXES), name="IRB", tool=IRB_TOOL)
    return dataclasses.replace(r, payload_max=175.0, payload_derate=float(payload_derate))


def gantry_robot(tcp_speed=None):
    axes = [{"name": "x", "range": [0, 3.0], "speed": 1.0, "accel": 2.0},
            {"name": "y", "range": [0, 2.0], "speed": 1.0, "accel": 2.0},
            {"name": "z", "range": [0, 1.0], "speed": 0.5, "accel": 1.0}]
    r = template_robot("gantry", CASES["gantry"]["params"], axes, name="gantry")
    return dataclasses.replace(r, tcp_speed=tcp_speed)


# The datasheet spec of the robot asset tests (accelerations not given).
FULL_SPEC = {"structure": "serial6", "name": "Test arm", "reach_m": 2.60, "payload_kg": 175,
             "footprint_m": [1.004, 0.72], "dimensions": dict(IRB_DIMS),
             "axes": [{"range": [-170, 170], "speed": 100}, {"range": [-65, 85], "speed": 90},
                      {"range": [-180, 70], "speed": 90}, {"range": [-300, 300], "speed": 170},
                      {"range": [-130, 130], "speed": 120}, {"range": [-360, 360], "speed": 190}],
             "source": "test", "approximate": True}

SEG = [{"kind": "ptp", "to": "above_pick"}, {"kind": "pick", "time": 1.0},
       {"kind": "ptp", "to": "above_place"}, {"kind": "place", "time": 1.0}]
TARGETS = {"above_pick": {"joints": [90, 0, 0, 0, 30, 0]}, "above_place": {"joints": [0, 0, 0, 0, 30, 0]}}


def robot_cell(segments=SEG, targets=TARGETS, record=IRB_RECORD, **params) -> dict:
    """single_server.json in seconds with the robot record, `process_1` a MOTION
    cell at (10, 5) facing south (rotation 90) and a 4 m CONVEYOR `belt_1` at
    (12, 5), rotation 0, beside it (not linked)."""
    doc = json.loads((ROOT / "nagasim" / "models" / "single_server.json").read_text(encoding="utf-8"))
    doc["simulation"]["time_unit"] = "seconds"
    doc["robots"] = [copy.deepcopy(record)]
    cell = next(b for b in doc["blocks"] if b["name"] == "process_1")
    cell["type"] = "MOTION"
    cell["layout_position"] = [10.0, 5.0]
    cell["layout_rotation"] = 90.0
    cell["params"] = {"action": "Seize Delay Release", "resource_name": "Server", "robot": record["name"],
                      "targets": copy.deepcopy(targets), "segments": copy.deepcopy(segments),
                      "time_unit": "seconds", **params}
    doc["blocks"].append({"name": "belt_1", "type": "CONVEYOR", "position": [520.0, 480.0],
                          "layout_position": [12.0, 5.0], "layout_rotation": 0.0,
                          "params": {"length": 4.0, "speed": 0.5, "spacing": 0.5}})
    return doc


ZEROS = np.zeros(6)


# The robots-section record of gantry_robot().
GANTRY_RECORD = {"name": "gantry", "structure": "gantry", "params": dict(CASES["gantry"]["params"]),
                 "axes": [{"name": "x", "range": [0, 3.0], "speed": 1.0, "accel": 2.0},
                          {"name": "y", "range": [0, 2.0], "speed": 1.0, "accel": 2.0},
                          {"name": "z", "range": [0, 1.0], "speed": 0.5, "accel": 1.0}]}

BOX_TARGET = {"belt": "belt", "divert": 0, "z": 2.5}


def _gantry_belt_doc(arrivals, every, accumulating, on_late, robot_block, more_blocks=(),
                     more_links=(), **sections):
    """create_1 -> belt (4 m at 0.5 m/s, a pick divert at 0.5 m) -> dispose_end or, by link 1,
    *robot_block* -> dispose_1; seconds and meters, the robot record GANTRY_RECORD."""
    def named(name, btype, params, at=None):
        out = {"name": name, "description": name, "type": btype, "position": [0, 0], "params": params}
        if at is not None:
            out["layout_position"], out["layout_rotation"] = list(at), 0.0
        return out

    blocks = [
        named("create_1", "CREATE", {"e_type": "BOX", "arrival_mode": "Interarrival",
                                     "schedule_name": "", "distribution": "Constant",
                                     "dist_params": {"value": float(every)},
                                     "entities_per_arrival": 1, "max_arrivals": int(arrivals),
                                     "start_time": 0}),
        named("belt", "CONVEYOR", {"length": 4.0, "speed": 0.5, "spacing": 1.0,
                                   "accumulating": bool(accumulating),
                                   "diverts": [{"at": 0.5, "condition": "True", "intercept": True,
                                                "on_late": on_late}]}, at=(2.0, 0.0)),
        named("dispose_end", "DISPOSE", {"distribution": "Constant", "dist_params": {"value": 0.0}}),
        robot_block,
        named("dispose_1", "DISPOSE", {"distribution": "Constant", "dist_params": {"value": 0.0}}),
        *more_blocks]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_end"},
             {"source": "belt", "target": robot_block["name"]},
             {"source": robot_block["name"], "target": "dispose_1"}, *more_links]
    doc = {"schema_version": 9, "blocks": blocks, "links": links, "resources": [],
           "resource_sets": [], "entities": [], "variables": [], "schedules": [], "failures": [],
           "simulation": {"time_limit": None, "warmup": 0, "replications": 1,
                          "time_unit": "seconds", "length_unit": "meters"},
           "robots": [copy.deepcopy(GANTRY_RECORD)]}
    doc.update(sections)
    return doc


def gantry_pick_cell(on_late="wait", arrivals=2, every=10.0, accumulating=True) -> dict:
    """A MOTION gantry at the origin picks each box from a moving belt: the call at 0.5 m,
    met at 1.5 m after 2.0 s (the third case of the oracle's gantry intercept), then
    pick 1.0, back home 2.0 and place 1.0, a 6.0 s cycle."""
    robot = {"name": "robot", "description": "robot", "type": "MOTION", "position": [0, 0],
             "layout_position": [0.0, 0.0], "layout_rotation": 0.0,
             "params": {"action": "Seize Delay Release", "resource_name": "Robot", "robot": "gantry",
                        "targets": {"box": dict(BOX_TARGET), "home": {"joints": [0, 0, 0]}},
                        "segments": [{"kind": "ptp", "to": "box"}, {"kind": "pick", "time": 1.0},
                                     {"kind": "ptp", "to": "home"}, {"kind": "place", "time": 1.0}],
                        "time_unit": "seconds"}}
    resources = [{"name": "Robot", "capacity": 1, "discipline": "FIFO", "cost_busy": 0.0,
                  "cost_idle": 0.0, "cost_per_use": 0.0, "cost_time_unit": "hours"}]
    return _gantry_belt_doc(arrivals, every, accumulating, on_late, robot, resources=resources)


# Two 0.4 m boxes side by side on a raised 0.8 x 0.4 m pallet: slot tops 1.75 m above the floor,
# inside the gantry's 1.5-2.5 m tool heights, and slot_above (0.3 m higher) at 2.05 m.
TWO_PATTERN = {"name": "TWO", "pallet": {"length": 0.8, "width": 0.4, "deck_height": 1.5},
               "item": {"length": 0.4, "width": 0.4, "height": 0.25, "weight_kg": 1.0},
               "layers": {"A": [{"x": -0.2, "y": 0.0}, {"x": 0.2, "y": 0.0}]},
               "stack": [{"layer": "A"}], "limits": {"layers": 1}}


def gantry_pallet_cell(arrivals=2, every=10.0) -> dict:
    """gantry_pick_cell's belt and robot as a pattern BATCH `palletizer`: the pallet at
    (1.5, -1.0) on the floor plan (cell y 1.0, inside the y stroke), the gantry moved by
    `robot_at` back to the origin, so it meets each box as the MOTION robot does."""
    palletizer = {"name": "palletizer", "description": "palletizer", "type": "BATCH",
                  "position": [0, 0], "layout_position": [1.5, -1.0], "layout_rotation": 0.0,
                  "params": {"batch_size": 2, "batched_entity_type": "PALLET",
                             "carrier_type": "PALLET", "pattern": "TWO", "robot": "gantry",
                             "robot_at": [-1.5, 1.0, 0.0],
                             "targets": {"box": dict(BOX_TARGET), "home": {"joints": [0, 0, 0]}},
                             "segments": [{"kind": "ptp", "to": "box"}, {"kind": "pick", "time": 1.0},
                                          {"kind": "ptp", "to": "slot_above"},
                                          {"kind": "ptp", "to": "slot"},
                                          {"kind": "place", "time": 1.0},
                                          {"kind": "ptp", "to": "home"}]}}
    pallets = {"name": "pallets", "description": "pallets", "type": "CREATE", "position": [0, 0],
               "params": {"e_type": "PALLET", "arrival_mode": "Interarrival", "schedule_name": "",
                          "distribution": "Constant", "dist_params": {"value": 1000.0},
                          "entities_per_arrival": 1, "max_arrivals": 1, "start_time": 0}}
    return _gantry_belt_doc(arrivals, every, True, "wait", palletizer, more_blocks=[pallets],
                            more_links=[{"source": "pallets", "target": "palletizer"}],
                            pallet_patterns=[copy.deepcopy(TWO_PATTERN)])
