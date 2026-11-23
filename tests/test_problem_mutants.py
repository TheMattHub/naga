"""A mutant model per problem code (D1336): each check behind the editor's Problems panel is
proved to fire. One bundled model, valid as shipped; one small edit per code makes its check
speak, read the way the panel reads it (`ModelDocument.problems`, or the refusal to open the
model). Adding a code's mutant is one line in MUTANTS; WITHOUT_A_MUTANT only shrinks."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core.document import ModelDocument
from nagasim.core.problems import PROBLEM_CODES
from nagasim.core.schema import ModelValidationError

BASE = json.loads((Path(api.__file__).parent / "models" / "single_server.json").read_text(encoding="utf-8"))
PANEL = "nagasim.core.document:ModelDocument.problems"     # the Problems panel's model checks


# -- edits -------------------------------------------------------------------------------
def _block(layout, name):
    return next(b for b in layout["blocks"] if b["name"] == name)


def _add(key, *entries):
    """Append *entries* to the top-level list *key*."""
    def edit(layout):
        layout.setdefault(key, []).extend(copy.deepcopy(list(entries)))
    return edit


def _set(path, value):
    """Set the value at *path* ("simulation.warmup", "blocks.1.params.distribution")."""
    def edit(layout):
        *parents, last = path.split(".")
        here = layout
        for part in parents:
            here = here[int(part)] if isinstance(here, list) else here.setdefault(part, {})
        if isinstance(here, list):
            here[int(last)] = value
        else:
            here[last] = value
    return edit


def _drop(path):
    def edit(layout):
        *parents, last = path.split(".")
        here = layout
        for part in parents:
            here = here[int(part)] if isinstance(here, list) else here[part]
        del here[int(last) if isinstance(here, list) else last]
    return edit


def _all(*edits):
    def edit(layout):
        for one in edits:
            one(layout)
    return edit


def _remove_block(name):
    def edit(layout):
        layout["blocks"] = [b for b in layout["blocks"] if b["name"] != name]
        layout["links"] = [k for k in layout["links"] if name not in (k["source"], k["target"])]
    return edit


def _rename(old, new):
    def edit(layout):
        _block(layout, old)["name"] = new
        for link in layout["links"]:
            for end in ("source", "target"):
                link[end] = new if link[end] == old else link[end]
    return edit


def _insert(btype, params, *more_targets):
    """A *btype* block put between create_1 and process_1, linked on to *more_targets* too."""
    def edit(layout):
        layout["blocks"].append({"name": "added", "type": btype, "position": [360.0, 320.0], "params": params})
        layout["links"][0] = {"source": "create_1", "target": "added"}
        layout["links"] += [{"source": "added", "target": target} for target in ("process_1", *more_targets)]
    return edit


def _robot_cell(**edits):
    """process_1 a MOTION cell of the IRB-like test robot on the floor plan, then *edits*:
    {"targets.<name>": target, "segments.<i>.<field>": value, "<param>": value}."""
    from robotcases import IRB_RECORD, SEG, TARGETS
    params = {"action": "Seize Delay Release", "resource_name": "Server", "robot": "IRB",
              "targets": copy.deepcopy(TARGETS), "segments": copy.deepcopy(SEG), "time_unit": "seconds"}
    steps = [_add("robots", IRB_RECORD), _set("blocks.1.type", "MOTION"), _set("blocks.1.params", params),
             _set("blocks.1.layout_position", [10.0, 5.0])]
    steps += [_set(f"blocks.1.params.{path}", value) for path, value in edits.items()]
    return _all(*steps)


def _belt_cell(**params):
    """process_1 a CONVEYOR of length 10, speed 1, spacing 1, with *params* on top."""
    return _all(_set("blocks.1.type", "CONVEYOR"),
                _set("blocks.1.params", {"length": 10.0, "speed": 1.0, "spacing": 1.0, **params}))


def _floor_belt(**params):
    """process_1 a CONVEYOR on the floor (a path of 10 m from (0, 0)), with *params* on top."""
    return _all(_set("blocks.1.type", "CONVEYOR"),
                _set("blocks.1.params", {"path": [{"straight": 10.0}], "speed": 1.0,
                                         "spacing": 1.0, **params}))


_FLOOR_PIECE = {"path": [{"straight": 2.0}], "speed": 1.0, "spacing": 1.0}


def _pallet(**changes):
    """A valid 2-slot pallet pattern with *changes* ({"field": value} at the top level)."""
    record = {"name": "P1", "pallet": {"length": 1.2, "width": 0.8, "deck_height": 0.144},
              "item": {"length": 0.4, "width": 0.3, "height": 0.25},
              "layers": {"A": [{"x": -0.3, "y": 0}, {"x": 0.3, "y": 0}]}, "stack": [{"layer": "A"}],
              "limits": {"layers": 2}}
    return _add("pallet_patterns", {**record, **changes})


def _gate(source="class Gate:\n    def on_entry(self, entity, item):\n        return 0\n", **code):
    """A definition `Gate` with item code (in -> LOGIC hook entry -> out), placed nowhere."""
    return {"parameters": [], "code": {"class": "Gate", "source": source, **code},
            "blocks": [{"name": "in", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
                       {"name": "route", "type": "LOGIC", "position": [0, 0], "params": {"hook": "entry"}},
                       {"name": "out", "type": "SUB_EXIT", "position": [0, 0], "params": {"port": "out"}}],
            "links": [{"source": "in", "target": "route"}, {"source": "route", "target": "out"}]}


_VAR = {"name": "level", "initial": 0}
_TANK = {"name": "tank", "capacity": 10, "initial": 0}

# code -> the edit that makes its check fire on BASE. Errors first; one line each.
MUTANTS = {
    "E_ROBOT": _add("robots", {"name": "R", "structure": "arm9", "axes": []}),
    "E_ROBOT_UNKNOWN": _robot_cell(robot="Ghost"),
    "E_ROBOT_TARGET": _robot_cell(**{"segments.0.to": "nowhere"}),
    "E_ROBOT_REACH": _robot_cell(**{"targets.above_pick": {"joints": [200, 0, 0, 0, 0, 0]}}),
    "E_ROBOT_SINGULAR": _robot_cell(**{"targets.above_place": {"joints": [0] * 6}, "segments.2.kind": "lin"}),
    "E_ROBOT_LIN": _robot_cell(**{"targets.above_place": {"joints": [90, 0, 0, 180, -30, 180]},
                                  "segments.2.kind": "lin"}),
    "E_ROBOT_MOVE_TIME": _robot_cell(**{"segments.0.time": 1.0}),
    "E_ROBOT_INTERCEPT": _robot_cell(**{"targets.above_pick": {"belt": "belt_1", "divert": 0}}),
    "E_PALLET_SHAPE": _pallet(stack=[{"layer": "B"}]),
    "E_PALLET_OVERLAP": _pallet(layers={"A": [{"x": 0, "y": 0}, {"x": 0.1, "y": 0}]}),
    "E_PALLET_OVERHANG": _pallet(layers={"A": [{"x": 0.5, "y": 0}]}),
    "E_PALLET_EMPTY": _pallet(stack=[]),
    "E_PALLET_UNKNOWN": _all(_pallet(), _insert("BATCH", {"batch_size": 1, "carrier_type": "PALLET",
                                                          "pattern": '"P9"'})),
    "E_LINK_UNKNOWN_BLOCK": _add("links", {"source": "process_1", "target": "nowhere"}),
    "E_LINK_AREA": _set("links.1", {"source": "process_1", "target": "dispose_1", "transfer_time": 1,
                                    "delay": {"distribution": "Constant", "dist_params": {"value": 1}}}),
    "E_DUPLICATE_BLOCK": _add("blocks", {"name": "dispose_1", "type": "DISPOSE", "position": [0, 0]}),
    "E_UNKNOWN_BLOCK_TYPE": _set("blocks.2.type", "NOT_A_BLOCK"),
    "E_NO_CREATE": _remove_block("create_1"),
    "E_NO_DISPOSE": _remove_block("dispose_1"),
    "E_CREATE_NO_OUTPUT": _drop("links.0"),
    "E_DISPOSE_HAS_OUTPUT": _add("links", {"source": "dispose_1", "target": "process_1"}),
    "E_TOO_MANY_OUTPUTS": _add("links", {"source": "create_1", "target": "dispose_1"}),
    "E_CREATE_ZERO_INTERARRIVAL": _all(_set("blocks.0.params.distribution", "Constant"),
                                       _set("blocks.0.params.dist_params", {"value": 0})),
    "E_RESOURCE_CAPACITY": _all(_set("resources.0.capacity_schedule", "shifts"),
                                _add("schedules", {"name": "shifts", "kind": "capacity", "points": [[10, 2]]})),
    "E_DUPLICATE_RESOURCE": _add("resources", {"name": "Server", "capacity": 1}),
    "E_RESOURCE_DISCIPLINE": _set("resources.0.discipline", "NOT_A_RULE"),
    "E_RESOURCE_UNKNOWN": _set("blocks.1.params.resource_name", "Ghost"),
    "E_UNKNOWN_DISTRIBUTION": _set("blocks.1.params.distribution", "NotADistribution"),
    "E_DIST_PARAM_UNKNOWN": _set("blocks.1.params.dist_params", {"rat": 1.25}),
    "E_DIST_PARAM_VALUE": _set("blocks.1.params.dist_params", {"rate": -1.0}),
    "E_PARAM_UNKNOWN": _set("blocks.1.params.not_a_param", 1),
    "E_PARAM_TYPE": _set("blocks.1.params.resource_name", 5),
    "E_WARMUP_PAST_END": _set("simulation.warmup", 2000),
    "E_TIME_UNIT": _set("simulation.time_unit", "fortnights"),
    "E_UNIT": _set("simulation.length_unit", "furlongs"),
    "E_VARIABLE_NAME": _add("variables", {"name": "1bad", "initial": 0}),
    "E_VARIABLE_RESERVED": _add("variables", {"name": "entity", "initial": 0}),
    "E_DUPLICATE_VARIABLE": _add("variables", _VAR, _VAR),
    "E_VARIABLE_INITIAL": _add("variables", {"name": "level", "initial": [1]}),
    "E_VARIABLE_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'VAR("ghost")'}),
    "E_EXPRESSION": _set("blocks.1.params.dist_params", {"rate": "1 +* 2"}),
    "E_TIMESERIES_COUNTS": _set("simulation.timeseries_counts", ["ghost"]),
    "E_LOG_LEVEL": _set("simulation.log_level", "verbose"),
    "E_FAILURE_NAME": _add("failures", {"name": "", "resources": ["Server"],
                                        "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.01}},
                                        "downtime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}}),
    "E_FAILURE_RESOURCES": _add("failures", {"name": "f", "resources": [],
                                             "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.01}},
                                             "downtime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}}),
    "E_FAILURE_RESOURCE_UNKNOWN": _add("failures", {"name": "f", "resources": ["Ghost"],
                                                    "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.01}},
                                                    "downtime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}}),
    "E_DISTRIBUTION_MISSING": _add("failures", {"name": "f", "resources": ["Server"], "uptime": {},
                                                "downtime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}}),
    "E_RESOURCE_SCHEDULE": _set("resources.0.capacity_schedule", "ghost"),
    "E_RESOURCE_COST": _set("resources.0.cost_busy", -1),
    "E_RANKING_REQUIRED": _set("resources.0.discipline", "LowestValueFirst"),
    "E_SET_RULE": _add("resource_sets", {"name": "pool", "rule": "NOT_A_RULE", "members": ["Server"]}),
    "E_SET_EMPTY": _add("resource_sets", {"name": "pool", "members": []}),
    "E_SET_MEMBER": _add("resource_sets", {"name": "pool", "members": ["Ghost"]}),
    "E_SCHEDULE_NAME": _add("schedules", {"name": "", "kind": "capacity", "points": [[10, 1]]}),
    "E_DUPLICATE_SCHEDULE": _add("schedules", {"name": "s", "kind": "capacity", "points": [[10, 1]]},
                                 {"name": "s", "kind": "capacity", "points": [[10, 1]]}),
    "E_SCHEDULE_KIND": _add("schedules", {"name": "s", "kind": "not_a_kind", "points": [[10, 1]]}),
    "E_SCHEDULE_POINTS": _add("schedules", {"name": "s", "kind": "capacity", "points": []}),
    "E_SCHEDULE_POINT": _add("schedules", {"name": "s", "kind": "capacity", "points": [[-10, 1]]}),
    "E_SCENARIO_NAME": _add("scenarios", {"name": "", "overrides": []}),
    "E_DUPLICATE_SCENARIO": _add("scenarios", {"name": "a", "overrides": []}, {"name": "a", "overrides": []}),
    "E_SCENARIO_OVERRIDE": _add("scenarios", {"name": "a", "overrides": [{"value": 2}]}),
    "E_SCENARIO_GOAL": _add("scenarios", {"name": "a", "overrides": [], "goal": 5}),
    "E_ASSERTION_NAME": _add("assertions", {"name": "", "kpi": "k", "op": "<", "value": 1}),
    "E_DUPLICATE_ASSERTION": _add("assertions", {"name": "a", "kpi": "k", "op": "<", "value": 1},
                                  {"name": "a", "kpi": "k", "op": "<", "value": 1}),
    "E_ASSERTION_KPI": _add("assertions", {"name": "a", "kpi": "", "op": "<", "value": 1}),
    "E_ASSERTION_OP": _add("assertions", {"name": "a", "kpi": "k", "op": "~", "value": 1}),
    "E_ASSERTION_REPLICATIONS": _add("assertions", {"name": "a", "kpi": "k", "op": "<", "value": 1, "replications": 0}),
    "E_ASSERTION_CONFIDENCE": _add("assertions", {"name": "a", "kpi": "k", "op": "<", "value": 1, "confidence": 2}),
    "E_PATH_CHECK_NAME": _add("path_checks", {"name": "", "rule": "never", "block": "process_1"}),
    "E_DUPLICATE_PATH_CHECK": _add("path_checks", {"name": "p", "rule": "never", "block": "process_1"},
                                   {"name": "p", "rule": "never", "block": "process_1"}),
    "E_PATH_CHECK_RULE": _add("path_checks", {"name": "p", "rule": "sometimes", "block": "process_1"}),
    "E_PATH_CHECK_BLOCK": _add("path_checks", {"name": "p", "rule": "never", "block": "ghost"}),
    "E_PATH_CHECK_COUNT": _add("path_checks", {"name": "p", "rule": "at_most", "block": "process_1", "count": -1}),
    "E_TABLE_NAME": _add("tables", {"name": "", "keys": ["a"], "values": [1]}),
    "E_TABLE_SHAPE": _add("tables", {"name": "t", "keys": ["a", "a"], "values": [1, 2]}),
    "E_TABLE_MODE": _add("tables", {"name": "t", "keys": ["a"], "values": [1], "key_mode": "sometimes"}),
    "E_TABLE_VALUE": _add("tables", {"name": "t", "keys": ["a"], "values": ["x"]}),
    "E_SEQUENCE_NAME": _add("sequences", {"name": "", "steps": [{"station": "process_1"}]}),
    "E_SEQUENCE_EMPTY": _add("sequences", {"name": "q", "steps": []}),
    "E_SEQUENCE_STEP": _add("sequences", {"name": "q", "steps": [5]}),
    "E_SEQUENCE_STATION_UNKNOWN": _add("sequences", {"name": "q", "steps": [{"station": "ghost"}]}),
    "E_FIXED_COST_NAME": _add("fixed_costs", {"name": "", "amount": 1}),
    "E_FIXED_COST_AMOUNT": _add("fixed_costs", {"name": "rent", "amount": "lots"}),
    "E_ANNOTATION_ID": _add("annotations", {"id": "", "kind": "text", "text": "x", "position": [0, 0]}),
    "E_DUPLICATE_ANNOTATION": _add("annotations", {"id": "a", "kind": "text", "text": "x", "position": [0, 0]},
                                   {"id": "a", "kind": "text", "text": "x", "position": [0, 0]}),
    "E_ANNOTATION_KIND": _add("annotations", {"id": "a", "kind": "arrow", "text": "x", "position": [0, 0]}),
    "E_ANNOTATION_SHAPE": _add("annotations", {"id": "a", "kind": "text", "text": "x", "position": [0]}),
    "E_BACKGROUND": _set("background", {"path": "", "position": [0, 0]}),
    "E_DASHBOARD_NAME": _set("dashboard", {"widgets": [{"name": "", "kind": "kpi", "expr": "TNOW"}]}),
    "E_DASHBOARD_KIND": _set("dashboard", {"widgets": [{"name": "w", "kind": "pie", "expr": "TNOW"}]}),
    "E_DASHBOARD_EXPR": _set("dashboard", {"widgets": [{"name": "w", "kind": "kpi", "expr": ""}]}),
    "E_DASHBOARD_SERIES": _set("dashboard", {"widgets": [{"name": "w", "kind": "kpi", "expr": "TNOW", "series": []}]}),
    "E_DASHBOARD_GAUGE": _set("dashboard", {"widgets": [{"name": "w", "kind": "gauge", "expr": "TNOW", "max": "x"}]}),
    "E_REALITY_CHECK_NAME": _add("reality_checks", {"name": "", "inputs": {}, "constraint": "TNOW >= 0"}),
    "E_DUPLICATE_REALITY_CHECK": _add("reality_checks", {"name": "r", "inputs": {}, "constraint": "TNOW >= 0"},
                                      {"name": "r", "inputs": {}, "constraint": "TNOW >= 0"}),
    "E_REALITY_CHECK_INPUT": _add("reality_checks", {"name": "r", "inputs": {"": 1}, "constraint": "TNOW >= 0"}),
    "E_REALITY_CHECK_WHEN": _add("reality_checks", {"name": "r", "inputs": {}, "constraint": "TNOW >= 0",
                                                    "when": "sometimes"}),
    "E_ENTITY_COST": _set("entities.0.holding_cost", -1),
    "E_DISTANCE_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'DISTANCE("a", "b")'}),
    "E_DECIDE_PROBS": _insert("DECIDE", {"probs": [0.8, 0.8]}, "dispose_1"),
    "E_DECIDE_BRANCHES": _insert("DECIDE", {"probs": [0.2, 0.2, 0.2]}, "dispose_1"),
    "E_DECIDE_POLICY_NAME": _insert("DECIDE", {"mode": "Policy"}, "dispose_1"),
    "W_ROUTE_RULE": _insert("DECIDE", {"mode": "Rule", "rule": {"kind": "condition",
                                                                "conditions": ["True", "True"]}},
                            "dispose_1"),
    "E_DECIDE_RULE": _insert("DECIDE", {"mode": "Rule", "rule": {"kind": "ghost"}}, "dispose_1"),
    "E_DECIDE_FALLBACK": _insert("DECIDE", {"mode": "Policy", "policy": "p", "fallback": "ghost"}, "dispose_1"),
    "E_DECIDE_QUEUE_EXPRS": _insert("DECIDE", {"mode": "Policy", "policy": "p", "fallback": "shortest",
                                               "queue_exprs": ["NQ(\"Server\")"]}, "dispose_1"),
    "W_DECIDE_NO_ELSE": _insert("DECIDE", {"mode": "Condition", "conditions": ["TNOW > 1"]}),
    "E_ASSIGN_KEY": _insert("ASSIGN", {"assignments": [{"target": "entity.x", "expr": "1", "bogus": 1}]}),
    "E_UNDECLARED_VARIABLE": _insert("ASSIGN", {"assignments": [{"target": "ghost", "expr": "1"}]}),
    "E_ASSIGN_TARGET": _insert("ASSIGN", {"assignments": [{"target": "entity.", "expr": "1"}]}),
    "E_RECORD_STAT_NAME": _insert("RECORD", {"stat_name": ""}),
    "E_ARRIVAL_MODE": _set("blocks.0.params.arrival_mode", "Whenever"),
    "E_ARRIVAL_SCHEDULE": _set("blocks.0.params.arrival_mode", "Schedule"),
    "E_SEQUENCE_UNKNOWN": _set("blocks.0.params.sequence", "ghost"),
    "E_QUEUE_NO_LEAVE_LINK": _set("blocks.1.params.renege_after", 5),
    "E_ON_PREEMPT": _set("blocks.1.params.on_preempt", "ghost"),
    "E_DETERMINISTIC_NO_MEAN": _all(_set("simulation.deterministic", True),
                                    _set("blocks.1.params.distribution", "Cauchy"),
                                    _set("blocks.1.params.dist_params", {"location": 1, "scale": 1})),
    "E_FIELD_RANGE": _set("resources.0.capacity", -1),
    "E_FIELD_TYPE": _set("resources.0.capacity", "two"),
    "W_UNKNOWN_KEY": _set("entities.0.cost", 1),
    "W_QUEUE_LEAVE_LINK_UNUSED": _all(_add("blocks", {"name": "dispose_2", "type": "DISPOSE", "position": [840, 480]}),
                                      _add("links", {"source": "process_1", "target": "dispose_2"})),
    "W_UNREACHABLE": _all(_add("blocks", {"name": "spare", "type": "DELAY", "position": [520, 480],
                                          "params": {"distribution": "Constant", "dist_params": {"value": 1}}}),
                          _add("links", {"source": "spare", "target": "dispose_1"})),
    "W_LONG_NAME": _rename("dispose_1", "customers_leave_the_shop"),
    "E_LEVEL_NAME": _add("levels", {"name": "", "capacity": 10, "initial": 0}),
    "E_LEVEL_BOUNDS": _add("levels", {"name": "tank", "capacity": 10, "min": 20, "initial": 0}),
    "E_LEVEL_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'LEVEL("ghost")'}),
    "E_FLOW_LEVEL": _add("flows", {"name": "fill", "rate": "1", "to": "ghost"}),
    "E_FLOW_NAME": _all(_add("levels", _TANK), _add("flows", {"name": "", "rate": "1", "to": "tank"})),
    "E_FLOW_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'FLOW("ghost")'}),
    "E_SERIES_NAME": _add("timeseries_inputs", {"name": "", "start": 0, "step": 1, "values": [1, 2]}),
    "E_SERIES_SHAPE": _add("timeseries_inputs", {"name": "price", "start": 0, "step": 1, "values": []}),
    "E_SERIES_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'TS("ghost")'}),
    "E_AUX_NAME": _add("auxiliaries", {"name": "", "expr": "1"}),
    "E_AUX_UNKNOWN": _set("blocks.1.params.dist_params", {"rate": 'AUX("ghost")'}),
    "E_ASSET_3D": _set("blocks.1.asset_3d", "../x"),
    "E_AUX_CYCLE": _add("auxiliaries", {"name": "a", "expr": 'AUX("b")'}, {"name": "b", "expr": 'AUX("a")'}),
    "E_DATA_SOURCE_NAME": _add("data_sources", {"name": "", "kind": "csv", "location": "data/x.csv"}),
    "E_DATA_SOURCE_KIND": _add("data_sources", {"name": "calls", "kind": "telepathy", "location": "data/x.csv"}),
    "E_DATA_BINDING_NAME": _add("data_bindings", {"name": "", "source": "calls", "transform": "orders", "target": "orders"}),
    "E_DATA_BINDING_SOURCE": _add("data_bindings", {"name": "b", "source": "ghost", "transform": "orders", "target": "orders"}),
    "E_ORDER_ID": _add("orders", {"id": "", "release": 1.0, "quantity": 1}),
    "E_INITIAL_BLOCK": _set("initial_state", {"time": 0.0, "entities": [{"block": "ghost", "count": 1}]}),
    "E_INITIAL_COUNT": _set("initial_state", {"time": 0.0, "entities": [{"block": "process_1", "count": -1}]}),
    "E_INITIAL_KEY": _set("initial_state", {"time": 0.0, "bogus": 1, "entities": []}),
    "E_UNKNOWN_SUBMODEL": _set("blocks.1", {"name": "process_1", "type": "SUBMODEL", "position": [520.0, 320.0],
                                            "params": {"definition": "Ghost"}}),
    "E_ITEM_CODE": _set("submodels", {"Gate": _gate("class Gate:\n    def on_entry(self, entity, item)\n"
                                                    "        return 0\n")}),
    "E_ITEM_HOOK": _set("submodels", {"Gate": _gate(timer=5)}),
    "E_CONVEYOR_EYE": _belt_cell(eyes=[{"at": 99}]),
    "W_CONVEYOR_OPEN_PORT": _floor_belt(diverts=[{"at": 5.0, "condition": "True"}]),
    # a second piece at the same place, fed by the CREATE and draining to the DISPOSE
    "W_CONVEYOR_OVERLAP": _all(_floor_belt(), _add("blocks", {"name": "added", "type": "CONVEYOR",
                                                              "position": [360.0, 320.0],
                                                              "params": _FLOOR_PIECE}),
                               _add("links", {"source": "create_1", "target": "added"},
                                    {"source": "added", "target": "dispose_1"})),
    # a piece linked to process_1 whose end (2, 0) is not process_1's entry (0, 0)
    "W_CONVEYOR_SNAP": _all(_floor_belt(), _insert("CONVEYOR", _FLOOR_PIECE)),
    "E_CONVEYOR_PATH": _belt_cell(path=[{"straight": -1}]),
    "E_CONVEYOR_RELEASE": _belt_cell(release={"metering": {"gap": 0.4}, "slug": {"count": 2, "timeout": 5.0}}),
    "E_CONVEYOR_PARAM": _belt_cell(zones={"count": 2}, stops=[{"at": 5.0, "time": 1.0}]),
    "E_CONVEYOR_REVERSE": _belt_cell(reversible=True, stops=[{"at": 5.0, "time": 1.0}]),
    # one exit named by two links (checkpoint T3 I2)
    "E_CONVEYOR_DIVERT": _all(_belt_cell(diverts=[{"at": 2.0, "condition": "True"}]),
                              _set("links.1.source_port", "out"),
                              _add("links", {"source": "process_1", "target": "dispose_1",
                                             "source_port": "out"})),
}


def codes_of(layout) -> set:
    """The codes the editor shows for *layout*: its Problems panel's, or those of the
    refusal to open it (#1973)."""
    try:
        return {p.code for p in ModelDocument(layout).problems()}
    except ModelValidationError as refused:
        return {p.code for p in refused.structured}


# The codes the panel's checks can raise (`tools/screen_codes.py`, read from the code) that no
# mutant proves yet. Only goes down: a new code needs a mutant, and one given a mutant leaves.
# Many need a second base model (agents, SD, conveyors, fleets, submodels); a code no edit can
# reach is a finding, filed as a DEVLOG bug.
WITHOUT_A_MUTANT = {
    "E_AGENT_ACTION", "E_AGENT_ATTRIBUTE", "E_AGENT_EXPRESSION", "E_AGENT_GRID", "E_AGENT_NETWORK",
    "E_AGENT_PATCH", "E_AGENT_PLACEMENT", "E_AGENT_PYTHON", "E_AGENT_RATE_FORM", "E_AGENT_SPACE",
    "E_AGENT_STATE", "E_AGENT_TICK", "E_AGENT_TRANSITION", "E_AGENT_TYPE_NAME",
    "E_AGGREGATE_AGENTS", "E_BATCH_MEMBER_TIME", "E_BATTERY", "E_BATTERY_SCHEDULE",
    "E_BUFFER_CAPACITY", "E_BUFFER_LEVEL", "E_BUILDING", "E_CALENDAR_DATE", "E_CALENDAR_START",
    "E_CALENDAR_TIMEZONE", "E_CHARGE", "E_CHARGER", "E_CHARGER_UNKNOWN", "E_CONVERSION_CURVE",
    "E_CONVERSION_INTERVAL", "E_CONVERSION_NAME", "E_CONVERSION_SUBSTRATE", "E_CONVERSION_UNKNOWN",
    "E_CONVEYOR_GRID", "E_CONVEYOR_LEAK", "E_CONVEYOR_MERGE",
    "E_CONVEYOR_METHOD", "E_CONVEYOR_OUTFLOW", "E_CONVEYOR_SPEC",
    "E_CONVEYOR_STOP", "E_CONVEYOR_WRITE", "E_CRANE", "E_DASHBOARD_CONTROL",
    "E_DASHBOARD_DEFINITION", "E_DATA_BINDING_TARGET", "E_DATA_BINDING_TRANSFORM",
    "E_DATA_CONNECTION", "E_DATA_CREDENTIALS", "E_DATA_FILTER", "E_DATA_SOURCE_LOCATION",
    "E_DATA_SOURCE_OPTION", "E_DELAY_NAME", "E_DELAY_SPEC", "E_DELAY_UNKNOWN", "E_DISPATCH",
    "E_DUPLICATE_FAILURE", "E_DUPLICATE_STATION", "E_DUPLICATE_SUBMODEL_PORT", "E_ENERGY_PRICE",
    "E_EXTENDS_CYCLE", "E_EXTENDS_UNKNOWN", "E_FLEET_ARM", "E_FLEET_FIELD", "E_FLEET_LOADS",
    "E_FLEET_MOTION", "E_FLEET_NAME", "E_FLEET_NODE", "E_FLEET_POWER", "E_FLEET_POWER_STATE",
    "E_FLEET_UNITS", "E_FLEET_UNKNOWN", "E_FLOW_BIFLOW", "E_FLOW_COMPONENT", "E_FLOW_EFFICIENCY",
    "E_FLOW_MIN_RATE", "E_FLOW_PEAK_PRICE", "E_FLOW_TAG", "E_FLOW_THRESHOLD", "E_HOLD_SPACE_TARGET",
    "E_INITIAL_AGE", "E_INITIAL_ARRIVALS", "E_INITIAL_BATCH", "E_INITIAL_BELT",
    "E_INITIAL_BLOCK_TYPE", "E_INITIAL_BUSY", "E_INITIAL_CARRIER", "E_INITIAL_CLOCK",
    "E_INITIAL_CYCLE", "E_INITIAL_FAILURE", "E_INITIAL_FLEET", "E_INITIAL_IN_SERVICE",
    "E_INITIAL_ORDERS", "E_INITIAL_QUEUE_CAPACITY", "E_INITIAL_REMAINING", "E_INITIAL_RESOURCE",
    "E_INITIAL_SCHEDULE_OFFSET", "E_INITIAL_SET", "E_INITIAL_VARIABLE", "E_INVESTMENT",
    "E_LEVEL_COMPONENT", "E_LEVEL_INITIAL_CYCLE", "E_LEVEL_THRESHOLD", "E_MATCH_ATTRIBUTE",
    "E_MOTION_PAYLOAD", "E_MOTION_PROFILE", "E_MOTION_PROFILE_UNKNOWN", "E_MOTION_SEGMENT",
    "E_MOTION_ZONE", "E_MOTION_ZONE_UNKNOWN", "E_NETWORK_NODE", "E_NETWORK_NODE_UNKNOWN",
    "E_NETWORK_SEGMENT", "E_NETWORK_SHAPE", "E_NETWORK_UNREACHABLE", "E_ORDER_ENTITY_TYPE",
    "E_ORDER_FIELD", "E_ORDER_SOURCE", "E_PARAM_MISSING", "E_PLUGIN_NOT_LOADED",
    "E_POPULATION_COUNT", "E_POPULATION_NAME", "E_POPULATION_TYPE", "E_POPULATION_UNKNOWN",
    "E_POWER_UNKNOWN", "E_PREEMPT_BLOCKING", "E_PREEMPT_RESOURCE_SET", "E_PREEMPT_SCOPE",
    "E_QUEUE_ROW", "E_RECORD_KIND", "E_RECORD_KIND_CONFLICT", "E_RESOURCE_POWER",
    "E_RESOURCE_POWER_STATE", "E_ROUTE_NO_SEQUENCES", "E_SCHEDULE_EXCEPTION",
    "E_SCHEDULE_NO_CALENDAR", "E_SCHEDULE_WEEKLY", "E_SD_DELAY_ORDER", "E_SD_FUNCTION_CONTEXT",
    "E_SD_REQUIRES_DT", "E_SD_RK4_RANDOM", "E_SD_SETTINGS", "E_SEND_TARGET", "E_SEPARATE_CARRIER",
    "E_SEPARATE_CARRIER_LINK", "E_SEPARATE_MEMBER_TIME", "E_SEPARATE_RELEASE_ORDER",
    "E_SERIES_MODE", "E_SERIES_NO_CALENDAR", "E_SERIES_TIMES", "E_STOCKPILE", "E_SUBMODEL",
    "E_SUBMODEL_ARGUMENTS", "E_SUBMODEL_ARGUMENT_MISSING", "E_SUBMODEL_ARGUMENT_UNKNOWN",
    "E_SUBMODEL_ARG_REFERENCE", "E_SUBMODEL_ARG_TYPE", "E_SUBMODEL_ARG_UNKNOWN", "E_SUBMODEL_CYCLE",
    "E_SUBMODEL_DEAD_END", "E_SUBMODEL_PORT_NAME", "E_SUBMODEL_PORT_REQUIRED",
    "E_SUBMODEL_PORT_UNKNOWN", "E_SUBMODEL_REPEAT", "E_SUBMODEL_REPEAT_COUNT", "E_SUBMODEL_SCOPE",
    "E_SUBMODEL_SCOPED_NAME", "E_SUBSCRIPT_AGGREGATE", "E_SUBSCRIPT_DIM", "E_SUBSCRIPT_ELEMENT",
    "E_SUBSCRIPT_MAP", "E_SUBSCRIPT_NAME", "E_SUBSCRIPT_RANGE", "E_SUBSCRIPT_SIZE",
    "E_SUBSCRIPT_UNKNOWN", "E_TABLE_KEYS", "E_TABLE_UNKNOWN", "E_TENDING", "E_TERMINAL_NOT_DISPOSE",
    "E_TRANSFER_MODE", "E_TRANSPORT_KEEP_ARM", "E_TRAVEL_MODE", "E_UNIT_MISMATCH", "E_UNIT_TIME",
    "E_WORKER_FLEET", "E_WORKER_NO_NODE", "E_WORKER_PREEMPT", "E_WORKER_SKILL_UNKNOWN",
    "E_WORKER_WHEN_BLOCKED", "E_ZONE", "W_AGENT_MESSAGE_UNUSED", "W_AGENT_SCAN",
    "W_AGENT_STATE_COMPOSITE", "W_AVAILABLE_IS_CAPACITY", "W_BACKGROUND_MISSING", "W_BLOCKING_LOOP",
    "W_CONVEYOR_LEVEL_KEY", "W_CONVEYOR_STOP_HELD", "W_CONVEYOR_TRANSIT", "W_CONVEYOR_ZONE",
    "W_CREATE_NO_ORDERS", "W_DATA_ALLOW_NETWORK_IGNORED", "W_DISPATCH_FORECAST", "W_DISPATCH_MIXED",
    "W_EMPIRICAL_CONTINUOUS", "W_FLOW_CLOCK_NO_GRID", "W_INITIAL_STATE_WARMUP",
    "W_INVESTMENT_NO_CAPEX", "W_INVESTMENT_ONE_SCENARIO", "W_LIBRARY_ITEM_CLASH", "W_NO_CHARGER",
    "W_OPENING_OFF_WALL", "W_ORDERS_NOT_CONSUMED", "W_ORDER_DUE_BEFORE_RELEASE", "W_ORDER_FIELD",
    "W_PLAN_STATION_UNKNOWN", "W_PREEMPT_NO_PRIORITY", "W_QUEUE_ROW_CROSSING",
    "W_RATE_UNIT_CHANGED", "W_RATE_UNIT_READ", "W_RATE_UNIT_UNCHECKED", "W_REF_UNSET",
    "W_RERANK_UNRANKED", "W_SCENARIO_PATH", "W_SCHEDULE_EXCEPTION_UNUSED", "W_SD_RK4_BUS",
    "W_SEGMENT_DEADLOCK_RISK", "W_SERIES_BEFORE_START", "W_SERIES_PERIOD_UNUSED",
    "W_STATION_UNUSED", "W_TENDING_DEADLOCK", "W_TENDING_STAY", "W_TENDING_UNUSED",
    "W_TRANSPORT_KEEP_UNFREED", "W_UNBOUNDED_DURATION", "W_UNIT_MISMATCH", "W_UNIT_MISSING",
    "W_UNIT_TIME", "W_UNIT_UNPARSED", "W_WALL_CROSSING", "W_WRITTEN_BY_NEWER"}

# R59: codes only a run raises, which the reach finds through a block class its checks read;
# no model edit makes the panel show them, so a run test is their proof (named per code).
RUN_ONLY = {
    "W_SAMPLES_NOTHING": "tests/test_log_levels.py::test_a_series_run_of_a_model_that_samples_nothing_says_so",
    "W_CONVEYOR_HANDED_TO_NOTHING": "tests/test_conveyor_checks.py::test_a_part_handed_to_nothing_holds_its_belt_blocked",
    "W_ROUTE_FORBIDDEN": "tests/test_conveyor_checks.py::test_a_part_leaving_by_an_exit_its_rule_would_not_choose_now_is_warned",
    "W_ROUTE_HELD_AT_END": "tests/test_run_problems.py::test_a_run_warning_is_in_the_api_result_once",
    "W_REVERSED_HELD_AT_END": "tests/test_run_problems.py::test_a_run_warning_is_in_the_api_result_once",
}


def test_the_base_model_has_no_problem():
    assert codes_of(copy.deepcopy(BASE)) == set()


@pytest.mark.parametrize("code", sorted(MUTANTS))
def test_a_mutant_raises_its_code(code):
    layout = copy.deepcopy(BASE)
    MUTANTS[code](layout)
    assert code in codes_of(layout)


def test_every_mutant_is_a_registered_code():
    assert set(MUTANTS) <= set(PROBLEM_CODES) and not set(MUTANTS) & WITHOUT_A_MUTANT
    assert set(RUN_ONLY) <= set(PROBLEM_CODES) and not set(RUN_ONLY) & (set(MUTANTS) | WITHOUT_A_MUTANT)


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "product").is_dir(),
                    reason="reads the product's Problems panel source (monorepo only)")
@pytest.mark.slow
def test_every_panel_code_has_a_mutant_or_is_listed():
    """Over 3 s: the reach is read from the source."""
    from tools import screen_codes
    reach = screen_codes.codes_of(screen_codes.reach([PANEL]), PROBLEM_CODES)
    bare = reach - set(MUTANTS) - set(RUN_ONLY)
    assert not bare - WITHOUT_A_MUTANT, f"reach the Problems panel with no mutant: {sorted(bare - WITHOUT_A_MUTANT)}"
    assert not WITHOUT_A_MUTANT - bare, f"have a mutant now, drop them: {sorted(WITHOUT_A_MUTANT - bare)}"
