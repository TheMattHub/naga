"""A small hand-built model of the intralogistics case (docs/cases/intralogistics-e3d-test).

It is the known-good input of the case checks and the base of their broken mutants. It is
not a faithful model of the case: boxes come from an AMR resource, the forklifts are a
resource of 2 and the router picks the line with the fewest boxes on their way (the slot
resources of each line). Each line's robot is one carrier BATCH that loads an empty pallet
by the 2 x 2 pattern EURO_2x2, the robot's cycle planned once per slot.
"""
from __future__ import annotations

import copy

SLOTS = 6          # boxes "on their way" a line takes before the router skips it
BOX_EVERY = 16.0   # a box leaves the warehouse every 16 s, a line takes every third
# Pallet supply matched to the lines: a line fills a pallet of 4 every 4 x 3 x 16 = 192 s, so
# an empty pallet per line every 192 s waits about 20 s for its first box and none piles up.
PALLET_EVERY = 4 * 3 * BOX_EVERY

# An arm with the geometry of an IRB 6700-200/2.60 and made-up speeds (tests/robotcases.py).
ROBOT = {"name": "IRB", "structure": "serial6",
         "params": {"base_height": 0.78, "shoulder_offset": 0.32, "upper_arm": 1.125,
                    "elbow_offset": 0.2, "forearm": 1.1425, "wrist_flange": 0.2},
         "axes": [{"name": f"a{k + 1}", "range": list(r), "speed": v, "accel": 4 * v}
                  for k, (r, v) in enumerate(zip(
                      [(-170, 170), (-65, 85), (-180, 70), (-300, 300), (-130, 130), (-360, 360)],
                      [100, 90, 90, 170, 120, 190]))],
         "tool": {"xyz": [0, 0, 0.2], "rpy_deg": [0, 0, 0]}, "payload_max": 200}

# 2 x 2 boxes of 0.6 x 0.4 x 0.25 m on a 1.2 x 0.8 m pallet, one layer.
PATTERN = {"name": "EURO_2x2", "pallet": {"length": 1.2, "width": 0.8, "deck_height": 0.144},
           "item": {"length": 0.6, "width": 0.4, "height": 0.25, "weight_kg": 12},
           "layers": {"A": [{"x": x, "y": y} for y in (-0.2, 0.2) for x in (-0.3, 0.3)]},
           "stack": [{"layer": "A"}], "limits": {"layers": 1}}


def _robot(i):
    """Line i's robot at (0, 10 i): one BATCH that loads a pallet slot by slot, the pallet
    1.6 m to the robot's side (axis 1 turns 170 degrees each way, so a pallet right behind
    it is out of reach) and the pick 1.6 m in front."""
    y = 10.0 * i
    b = _block(f"pallet_{i}", "BATCH", {
        "batch_size": 4, "batched_entity_type": "PALLET", "carrier_type": "PALLET",
        "pattern": "EURO_2x2", "robot": "IRB", "robot_at": [0.0, -1.6, 0.0],
        "targets": {"pick": {"xyz": [1.6, y, 0.8]}},
        "segments": [{"kind": "ptp", "to": "pick"}, {"kind": "pick", "time": 1.0},
                     {"kind": "ptp", "to": "slot_above"}, {"kind": "ptp", "to": "slot"},
                     {"kind": "place", "time": 1.0}]},
        "Palletizing robot, a cycle per pallet slot")
    b["layout_position"] = [0.0, y + 1.6]
    return b


def _block(name, type_, params=None, description=""):
    return {"name": name, "type": type_, "description": description, "position": [0.0, 0.0],
            "params": params or {}}


def _delay(resource, seconds):
    return {"action": "Seize Delay Release", "resource_name": resource,
            "distribution": "Constant", "dist_params": {"value": seconds}}


def build() -> dict:
    blocks = [
        _block("warehouse_boxes", "CREATE", {"e_type": "BOX", "distribution": "Constant",
                                             "dist_params": {"value": BOX_EVERY}},
               "Boxes leave the warehouse"),
        _block("amr_trip", "PROCESS", _delay("AMR", 60.0), "4 AMRs, one box per trip"),
        _block("trunk", "CONVEYOR", {"length": 4.0, "speed": 0.5, "spacing": 2.0},
               "Trunk conveyor, one box every 4 s"),
        _block("router_turn", "ASSIGN", {"assignments": [{"target": "turn", "expr": "turn + 1"}]},
               "Moves the router's turn on by one"),
        # In turn, skipping a line with SLOTS boxes on their way; with all at SLOTS, the fewest.
        # A key per line, the router takes the smallest: the turn order below the limit, the
        # box count (offset past any turn order) at or above it.
        _block("photo_eye_router", "DECIDE", {
            "mode": "Policy", "policy": "router", "fallback": "shortest",
            "queue_exprs": [f'IF(NR("way_{i}") + NQ("way_{i}") >= {SLOTS}, '
                            f'100 + NR("way_{i}") + NQ("way_{i}"), MODULO({i} - turn, 3))'
                            for i in (1, 2, 3)]},
            "Photo-eye router: in turn, skipping a line with 6 boxes on their way"),
    ]
    links = [("warehouse_boxes", "amr_trip"), ("amr_trip", "trunk"), ("trunk", "router_turn"),
             ("router_turn", "photo_eye_router")]
    resources = [{"name": "AMR", "capacity": 4}, {"name": "forklift", "capacity": 2}]
    for i in (1, 2, 3):
        resources += [{"name": f"way_{i}", "capacity": SLOTS}, {"name": f"stacker_{i}", "capacity": 1}, {"name": f"wrapper_{i}", "capacity": 1}]
        blocks += [
            _block(f"way{i}_take", "SEIZE", {"resource_name": f"way_{i}"}, "Counts a box on its way"),
            _block(f"line{i}_infeed", "CONVEYOR", {"length": 6.0, "speed": 0.5, "spacing": 1.0}),
        ]
        chain = [f"way{i}_take", f"line{i}_infeed"]
        if i == 2:
            blocks.append(_block("line2_buffer", "CONVEYOR", {"length": 12.0, "speed": 0.5, "spacing": 1.0},
                                 "12-zone accumulation buffer"))
            chain.append("line2_buffer")
        blocks += [
            _block(f"way{i}_free", "RELEASE", {"resource_name": f"way_{i}"}),
            _block(f"line{i}_handoff", "CONVEYOR", {"length": 1.0, "speed": 0.5, "spacing": 1.0}),
            _block(f"empty_pallets_{i}", "CREATE", {"e_type": "PALLET", "distribution": "Constant",
                                                    "dist_params": {"value": PALLET_EVERY}},
                   "Empty pallets for the robot"),
            _robot(i),
            _block(f"stacker_{i}", "PROCESS", _delay(f"stacker_{i}", 15.0), "Pallet stacker"),
            _block(f"wrapper_{i}", "PROCESS", _delay(f"wrapper_{i}", 70.0), "Wrapper"),
            _block(f"forklift_{i}", "PROCESS", _delay("forklift", 36.0), "Forklift takes the pallet to the exit"),
        ]
        chain += [f"way{i}_free", f"line{i}_handoff", f"pallet_{i}", f"stacker_{i}",
                  f"wrapper_{i}", f"forklift_{i}", "exit"]
        links += ([("photo_eye_router", chain[0]), (f"empty_pallets_{i}", f"pallet_{i}")]
                  + list(zip(chain, chain[1:])))
    blocks.append(_block("exit", "DISPOSE", {}, "Exit, 40 m from the lines"))
    return {"schema_version": 30, "info": {"title": "Intralogistics case, hand-built"},
            "simulation": {"time_unit": "seconds", "time_limit": 28800, "warmup": 1800,
                           "replications": 10},
            "variables": [{"name": "turn", "initial": 0}],
            "robots": [ROBOT], "pallet_patterns": [copy.deepcopy(PATTERN)], "resources": resources, "blocks": blocks,
            "links": [{"source": a, "target": b} for a, b in links]}


def eye_router() -> dict:
    """The case with its router moved onto the trunk belt: a photo-eye 1 m along assigns the
    turn and the line (the DECIDE's own keys, so the same choice), and two diverts send a part
    to line 1 or line 2 at 2 m and 3 m; the rest leave at the end, to line 3."""
    m = copy.deepcopy(build())
    decide = next(b for b in m["blocks"] if b["name"] == "photo_eye_router")
    k1, k2, k3 = decide["params"]["queue_exprs"]
    line = f"IF({k1} <= {k2} and {k1} <= {k3}, 1, IF({k2} <= {k3}, 2, 3))"
    m["blocks"] = [b for b in m["blocks"] if b["name"] not in ("router_turn", "photo_eye_router")]
    trunk = next(b for b in m["blocks"] if b["name"] == "trunk")
    trunk["description"] = "Trunk conveyor with the router's photo-eye"
    trunk["params"] = {
        "length": 4.0, "speed": 0.5, "spacing": 2.0, "part_length": 0.4,
        "eyes": [{"at": 1.0, "assign": [{"target": "turn", "expr": "turn + 1"},
                                        {"target": "entity.line", "expr": line}]}],
        "diverts": [{"at": 2.0, "condition": "entity.line == 1"},
                    {"at": 3.0, "condition": "entity.line == 2"}]}
    gone = ("trunk", "router_turn"), ("router_turn", "photo_eye_router")
    links = [l for l in m["links"] if (l["source"], l["target"]) not in gone
             and l["source"] != "photo_eye_router"]
    # the order is the belt's: link 0 is the end, then the diverts in order
    m["links"] = links + [{"source": "trunk", "target": f"way{i}_take"} for i in (3, 1, 2)]
    return m


def mutant(kind: str) -> dict:
    """The known-good model with one thing broken."""
    m = copy.deepcopy(build())
    blocks = {b["name"]: b for b in m["blocks"]}
    if kind == "missing_line":          # line 3 is gone
        gone = {n for n in blocks if n.endswith("_3") or "3_" in n or n.startswith("way3")
                or n == "line3_infeed"}
        m["blocks"] = [b for b in m["blocks"] if b["name"] not in gone]
        m["links"] = [l for l in m["links"] if l["source"] not in gone and l["target"] not in gone]
        m["blocks"] = [b for b in m["blocks"] if True]
        for b in m["blocks"]:
            if b["name"] == "photo_eye_router":
                b["params"]["queue_exprs"] = b["params"]["queue_exprs"][:2]
    elif kind == "backward_link":       # a wrapped pallet goes back to line 1's infeed
        m["links"].append({"source": "wrapper_1", "target": "line1_infeed"})
    elif kind == "seventh_box":         # the router sends everything to line 1, past its 6
        blocks["photo_eye_router"]["params"]["fallback"] = "first"
        for r in m["resources"]:
            if r["name"] == "way_1":
                r["capacity"] = 99
    elif kind == "pallet_of_five":      # line 1's pattern closes a pallet at 5 boxes
        five = {**copy.deepcopy(PATTERN), "name": "FIVE", "stack": [{"layer": "A"}, {"layer": "A"}],
                "limits": {"layers": 2, "items": 5}}
        m["pallet_patterns"].append(five)
        blocks["pallet_1"]["params"]["pattern"] = "FIVE"
    elif kind == "big_buffer":
        blocks["line2_buffer"]["params"]["length"] = 15.0
    elif kind in ("congested", "ignores_limit"):
        # Line 2's buffer lets a box in every 67 s while the router offers one every 48 s, so
        # line 2 fills to 6 on its way and the router must skip it: without this the limit is
        # never reached and a router that ignores it looks right.
        blocks["line2_buffer"]["params"]["speed"] = 0.015
        if kind == "ignores_limit":     # in turn only, whatever is on the way
            blocks["photo_eye_router"]["params"]["queue_exprs"] = [
                f"MODULO({i} - turn, 3)" for i in (1, 2, 3)]
    elif kind == "loop_back":           # one box in five on line 1 goes back to the trunk
        # A neutral name and a DECIDE: the old name-based checker saw no role on this link.
        m["blocks"].append(_block("recheck", "DECIDE", {"mode": "Chance", "probs": [0.2, 0.8]}))
        m["links"] = [l for l in m["links"] if (l["source"], l["target"]) != ("line1_handoff", "pallet_1")]
        m["links"] += [{"source": "line1_handoff", "target": "recheck"},
                       {"source": "recheck", "target": "trunk"},
                       {"source": "recheck", "target": "pallet_1"}]
    elif kind == "fixed_robot":         # line 1's robot is one fixed time a box, no moves
        params = blocks["pallet_1"]["params"]
        for key in ("robot", "robot_at", "targets", "segments"):
            params.pop(key)
        params["member_time"] = "6"
    elif kind == "one_position":        # line 1's robot places every box at one spot
        params = blocks["pallet_1"]["params"]
        params["targets"]["place"] = {"xyz": [0.0, 11.6, 0.4]}
        params["segments"] = [s if s.get("to") not in ("slot", "slot_above") else {**s, "to": "place"}
                              for s in params["segments"]]
    elif kind == "out_of_reach":        # line 1's pallet 3.5 m away, past the 2.6 m reach
        blocks["pallet_1"]["params"]["robot_at"] = [0.0, -3.5, 0.0]
        blocks["pallet_1"]["layout_position"] = [0.0, 13.5]
    else:
        raise ValueError(kind)
    return m
