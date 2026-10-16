"""Fixture tests for schema versioning and validation (DEVLOG #33, #78)."""
import json
from pathlib import Path

import pytest

from nagasim.core import schema as S
from nagasim.core.problems import PROBLEM_CODES
from nagasim.core.schema import (
    CURRENT_VERSION, ModelValidationError, migrate, validate_graph_structured,
    validate_layout_structured, validate_model,
)
from nagasim.core.quantities import fill_units
from nagasim.core.schema.migrate import _fill_block_defaults
from nagasim.modelschema import build_json_schema


def _v28(layout: dict) -> dict:
    """*layout* as v28 writes it (units spec, D159-D160): the model's length
    unit and every missing/"base" unit filled with what it meant."""
    import copy
    out = copy.deepcopy(layout)
    out["simulation"] = {**out["simulation"], "length_unit": "meters"}
    _fill_block_defaults(out["blocks"])      # every left-out parameter, D471
    return fill_units(out)

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
# Every migration from an older file gains these empty sections -- three for
# v12 -> v13 (ROADMAP 3.4), "tables" for v13 -> v14 (PHASE4 A1), "sequences"
# for v14 -> v15 (PHASE4 A2), "orders" for v15 -> v16 (PHASE4 A3), the three
# continuous ones for v16 -> v17 (PHASE4 D1/D2) and "fixed_costs" for
# v17 -> v18 (PHASE4 D5), and "network"/"fleets" for v19 -> v20 (PHASE4 E1), and
# "auxiliaries"/"delays"/"subscripts" for v20 -> v21 (PHASE5 SD-2/SD-3),
# "reality_checks" for v22 -> v23 (PHASE6 S8) and "assertions" for v24 -> v25
# (PHASE6 W7 Q3), and "motion_profiles" (D157, no version bump: migrate adds
# any missing record section)
# -- so the exact-equality assertions below spread
# them in.
_V17_SECTIONS = {"timeseries_inputs": [], "levels": [], "flows": [], "fixed_costs": [],
                 "conversions": [], "network": None, "fleets": [], "motion_profiles": [], "chargers": [],
                 "auxiliaries": [], "delays": [], "subscripts": [],
                 "agent_types": [], "populations": [], "reality_checks": [], "assertions": []}
_V13_SECTIONS = {"data_sources": [], "data_bindings": [], "initial_state": {}, "tables": [],
                 "sequences": [], "orders": [], **_V17_SECTIONS}
CURATED_EXAMPLES = ["single_server.json", "decide_routing.json", "hold_signal_flow.json",
                    "priority_rework.json"]


def test_gen1_layout_migrates_to_v4():
    gen1 = {
        "blocks": [
            {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
             "params": {"time_between_arrivals": 2, "entities_per_arrival": 1,
                        "max_entities": 5, "attributes": {"e_type": "X"}}},
            {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [1, 0],
             "params": {"deletion_delay": 3}},
        ],
        "links": [["create_1", "dispose_1"]],
    }

    migrated = migrate(gen1)

    assert migrated["schema_version"] == CURRENT_VERSION
    create_params = migrated["blocks"][0]["params"]
    assert create_params["distribution"] == "Constant"
    assert create_params["dist_params"] == {"value": 2}
    assert create_params["max_arrivals"] == 5
    assert create_params["e_type"] == "X"
    assert "time_between_arrivals" not in create_params
    assert "max_entities" not in create_params
    assert "attributes" not in create_params

    dispose_params = migrated["blocks"][1]["params"]
    assert dispose_params == {"distribution": "Constant", "dist_params": {"value": 3},
                             "time_unit": "minutes", "remove_agent": False}

    # positions shift from top-left (v3-and-earlier) to centre (v4): (pos + (40, 20)) * 1.5,
    # snapped to the 20-unit grid (120x60 Qt blocks vs 80x40 legacy pixel blocks)
    assert migrated["blocks"][0]["position"] == [60, 40]
    assert migrated["blocks"][1]["position"] == [60, 40]

    # missing sections get v4 defaults
    assert migrated["resources"] == []
    assert migrated["entities"] == []
    assert migrated["simulation"] == {"time_limit": None, "warmup": 0, "replications": 1,
                                      "time_unit": "minutes", "length_unit": "meters",
                                      "currency": "EUR", "energy_price": 0}

    assert validate_layout_structured(migrated) == []


def test_gen2_layout_migrates_to_v4():
    gen2 = {
        "blocks": [
            {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
             "params": {"e_type": "DEFAULT", "distribution": "Exponential", "dist_params": {"rate": 1.0},
                        "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
            {"name": "delay_1", "description": "d", "type": "DELAY", "position": [1, 0],
             "params": {"distribution": "Constant", "distribution_params": {"value": 5}}},
            {"name": "dispose_1", "description": "e", "type": "DISPOSE", "position": [2, 0],
             "params": {"distribution": "Constant", "distribution_params": {}}},  # vestigial, empty
        ],
        "links": [["create_1", "delay_1"], ["delay_1", "dispose_1"]],
    }

    migrated = migrate(gen2)

    delay_params = migrated["blocks"][1]["params"]
    assert delay_params == {"distribution": "Constant", "dist_params": {"value": 5},
                           "time_unit": "minutes"}
    assert "distribution_params" not in delay_params

    dispose_params = migrated["blocks"][2]["params"]
    assert "distribution_params" not in dispose_params  # dropped, never populated dist_params

    assert validate_layout_structured(migrated) == []


def test_gen3_layout_migrates_to_v4_shifting_positions_once():
    gen3 = {
        "schema_version": 3,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [10, 20],
                    "params": {}}],
        "links": [],
        "resources": [], "entities": [], "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    migrated = migrate(gen3)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["blocks"][0]["position"] == [80, 60]
    assert migrated["variables"] == []      # v4 -> v5 adds the section
    assert migrated is not gen3  # never mutates or aliases the input
    assert gen3["blocks"][0]["position"] == [10, 20]  # input untouched


def test_current_version_layout_passes_through_unchanged():
    v18 = {
        "schema_version": CURRENT_VERSION,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [], "annotations": [], "submodels": {},
        "background": None, "info": {},
        "data_sources": [], "data_bindings": [], "initial_state": {}, "tables": [],
        "sequences": [], "orders": [], "timeseries_inputs": [], "levels": [], "flows": [],
        "fixed_costs": [],
        "conversions": [], "network": None, "fleets": [], "motion_profiles": [], "chargers": [],
        "auxiliaries": [], "delays": [],
        "subscripts": [], "agent_types": [], "populations": [], "reality_checks": [],
        "assertions": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "currency": "EUR", "energy_price": 0},
    }
    v18 = _v28(v18)               # a current file already carries its units
    migrated = migrate(v18)
    assert migrated == v18
    assert migrated is not v18  # never mutates or aliases the input


def test_v14_layout_migrates_to_v15_gaining_an_empty_sequences_section():
    """v14 -> v15 (PHASE4 A2): "sequences" appears empty and nothing else
    changes -- nothing builds or reads a routing until a ROUTE block asks for
    one, so a v14 model runs exactly as it did."""
    v14 = {
        "schema_version": 14,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [], "annotations": [], "submodels": {},
        "background": None, "info": {},
        "data_sources": [], "data_bindings": [], "initial_state": {}, "tables": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "currency": "EUR"},
    }
    migrated = migrate(v14)
    assert migrated == _v28({**v14, "schema_version": CURRENT_VERSION, "sequences": [], "orders": [],
                        **_V17_SECTIONS, "simulation": {**v14["simulation"], "energy_price": 0}})
    assert "sequences" not in v14  # input untouched


def test_v13_layout_migrates_to_v14_gaining_an_empty_tables_section():
    """v13 -> v14 (PHASE4 A1): "tables" appears empty and nothing else changes
    -- nothing builds or reads a lookup table until an expression asks for
    one, so a v13 model runs exactly as it did."""
    v13 = {
        "schema_version": 13,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [], "annotations": [], "submodels": {},
        "background": None, "info": {},
        "data_sources": [], "data_bindings": [], "initial_state": {},
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "currency": "EUR"},
    }
    migrated = migrate(v13)
    assert migrated == _v28({**v13, "schema_version": CURRENT_VERSION, "tables": [], "sequences": [],
                        "orders": [], **_V17_SECTIONS,
                        "simulation": {**v13["simulation"], "energy_price": 0}})
    assert "tables" not in v13  # input untouched


def test_v12_layout_migrates_to_v13_gaining_the_empty_twin_sections():
    """v12 -> v13 (ROADMAP 3.4): "data_sources", "data_bindings" and
    "initial_state" appear empty and nothing else changes -- bindings only
    resolve when a caller asks, and an empty initial state injects nothing, so
    a v12 model runs exactly as it did."""
    v12 = {
        "schema_version": 12,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [], "annotations": [], "submodels": {},
        "background": None, "info": {},
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "currency": "EUR"},
    }
    migrated = migrate(v12)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated == _v28({**v12, "schema_version": CURRENT_VERSION, **_V13_SECTIONS,
                        "simulation": {**v12["simulation"], "energy_price": 0}})
    assert "initial_state" not in v12  # input untouched


def test_v10_layout_migrates_to_v11_gaining_an_empty_annotations_section():
    """v10 -> v11 (ROADMAP 2.3 part A): "annotations" appears and nothing else
    changes -- the engine ignores this section, so a v10 model with none
    behaves exactly as before."""
    v10 = {
        "schema_version": 10,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "currency": "EUR"},
    }
    migrated = migrate(v10)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["annotations"] == []
    assert migrated == _v28({**v10, "schema_version": CURRENT_VERSION, "annotations": [],
                        "submodels": {}, "background": None, "info": {}, **_V13_SECTIONS,
                        "simulation": {**v10["simulation"], "energy_price": 0}})
    assert "annotations" not in v10  # input untouched


def test_v9_layout_migrates_to_v10_gaining_a_currency_default():
    """v9 -> v10 (ROADMAP 2.2): "simulation.currency" defaults to "EUR" and
    nothing else changes -- a v9 model with no cost fields runs and reports
    identically to before, with every new KPI column present and reading 0."""
    v9 = {
        "schema_version": 9,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes"},
    }
    migrated = migrate(v9)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["simulation"]["currency"] == "EUR"
    assert migrated == _v28({**v9, "schema_version": CURRENT_VERSION, "annotations": [],
                        "submodels": {}, "background": None, "info": {}, **_V13_SECTIONS,
                        "simulation": {**v9["simulation"], "currency": "EUR", "energy_price": 0}})
    assert "currency" not in v9["simulation"]  # input untouched


def test_v8_layout_migrates_to_v9_gaining_an_empty_scenarios_section():
    """v8 -> v9 (ROADMAP 1.6): "scenarios" appears and nothing else changes --
    with none declared only the implicit Baseline runs, exactly as before."""
    v8 = {
        "schema_version": 8,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes"},
    }
    migrated = migrate(v8)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["scenarios"] == []
    assert migrated == _v28({**v8, "schema_version": CURRENT_VERSION, "scenarios": [], "annotations": [],
                        "submodels": {}, "background": None, "info": {}, **_V13_SECTIONS,
                        "simulation": {**v8["simulation"], "currency": "EUR", "energy_price": 0}})
    assert "scenarios" not in v8  # input untouched


def test_v7_layout_migrates_to_v8_adding_time_unit_and_link_objects():
    """v7 -> v8 (ROADMAP 1.5): simulation.time_unit defaults to "minutes" and
    every [src, tgt] link pair becomes {"source", "target"} -- nothing else
    changes, so a v7 model keeps behaving exactly as it did."""
    v7 = {
        "schema_version": 7,
        "blocks": [{"name": "create_1", "description": "c", "type": "CREATE", "position": [50, 40],
                    "params": {}},
                   {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [80, 40],
                    "params": {}}],
        "links": [["create_1", "dispose_1"]],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    migrated = migrate(v7)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["simulation"]["time_unit"] == "minutes"
    assert migrated["links"] == [{"source": "create_1", "target": "dispose_1"}]
    assert v7["links"] == [["create_1", "dispose_1"]]  # input untouched


def test_v6_layout_gains_the_schedule_sections():
    """v6 -> v7 (ROADMAP 1.4): empty "schedules"/"failures" appear and nothing
    else changes, so a v6 model keeps behaving exactly as it did."""
    v6 = {
        "schema_version": 6,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        "resource_sets": [], "entities": [], "variables": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    migrated = migrate(v6)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["schedules"] == [] and migrated["failures"] == []
    assert migrated["resources"] == [{**v6["resources"][0], "cost_time_unit": "hours"}]
    assert migrated["blocks"] == _v28(v6)["blocks"]        # a unit filled, D159; defaults, D471


def test_v5_resource_type_migrates_to_a_queue_discipline():
    """v5 -> v6 (ROADMAP 1.2): `type` disappears, `discipline` defaults to FIFO
    and the resource_sets section appears. Priority semantics are preserved
    because every resource is a PriorityResource now."""
    v5 = {
        "schema_version": 5,
        "blocks": [{"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [50, 40],
                    "params": {}}],
        "links": [],
        "resources": [{"name": "Server", "capacity": 2, "type": "PriorityResource"},
                      {"name": "Dock", "capacity": 1, "type": "Resource"}],
        "entities": [], "variables": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }

    migrated = migrate(v5)

    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["resources"] == [
        {"name": "Server", "capacity": 2, "discipline": "FIFO", "cost_time_unit": "hours"},
        {"name": "Dock", "capacity": 1, "discipline": "FIFO", "cost_time_unit": "hours"},
    ]
    assert migrated["resource_sets"] == []
    assert v5["resources"][0]["type"] == "PriorityResource"  # input untouched
    assert validate_layout_structured(migrated) == []


def test_hold_legacy_scan_for_conditions_mode_migrates_to_condition():
    """DEVLOG #6: the UI's old "Scan for Conditions" HOLD mode string
    normalises to "Condition" -- unconditionally (C3.6), since a HOLD block
    copy-pasted from an old file can carry it inside an otherwise
    CURRENT-version layout, not just a stale schema_version."""
    for version in (5, CURRENT_VERSION):
        layout = {
            "schema_version": version,
            "blocks": [{"name": "hold_1", "description": "h", "type": "HOLD", "position": [50, 40],
                        "params": {"mode": "Scan for Conditions", "conditions": ["True"]}}],
            "links": [], "resources": [], "entities": [], "variables": [],
            "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
        }

        migrated = migrate(layout)

        assert migrated["schema_version"] == CURRENT_VERSION
        assert migrated["blocks"][0]["params"]["mode"] == "Condition"
        assert layout["blocks"][0]["params"]["mode"] == "Scan for Conditions"  # input untouched


def test_v4_free_form_assign_migrates_to_an_ordered_assignment_list():
    """v4 -> v5 (ROADMAP 1.1): ASSIGN's {attr: value} dict becomes
    [{target, expr}] in the same order, strings keeping their quotes."""
    v4 = {
        "schema_version": 4,
        "blocks": [{"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [50, 40],
                    "params": {"e_type": "B", "priority": 2, "flag": True}}],
        "links": [], "resources": [], "entities": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }

    migrated = migrate(v4)

    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["variables"] == []
    assert migrated["blocks"][0]["params"] == {"assignments": [
        {"target": "entity.e_type", "expr": "'B'"},
        {"target": "entity.priority", "expr": "2"},
        {"target": "entity.flag", "expr": "True"},
    ]}
    assert migrated["blocks"][0]["position"] == [50, 40]   # v4 positions are already centres
    assert validate_layout_structured(migrated) == []


def test_validate_layout_checks_disciplines_ranking_expressions_and_sets():
    """ROADMAP 1.2: a resource declares its queue discipline, a set its members
    and rule, and the two share one name space."""
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [], "links": [], "variables": [],
        "resources": [
            {"name": "A", "capacity": 1, "discipline": "Sideways"},
            {"name": "B", "capacity": 1, "discipline": "LowestValueFirst"},
            {"name": "C", "capacity": 1, "discipline": "HighestValueFirst",
             "ranking_expr": "entity.x +"},
        ],
        "resource_sets": [
            {"name": "A", "members": ["A"], "rule": "Cyclical"},
            {"name": "S", "members": ["A", "ghost"], "rule": "RoundRobin"},
            {"name": "Empty", "members": [], "rule": "Random"},
        ],
    }

    problems = [p.message for p in validate_layout_structured(layout)]

    assert any("unknown discipline 'Sideways'" in p for p in problems)
    assert any(p == "resource 'B': discipline 'LowestValueFirst' needs a ranking_expr"
               for p in problems)
    assert any(p.startswith("resource 'C': ranking_expr: ") for p in problems)
    assert any("name already used by a resource or set" in p for p in problems)
    assert any("unknown member 'ghost'" in p for p in problems)
    assert any("unknown rule 'RoundRobin'" in p for p in problems)
    assert any(p == "resource set 'Empty': no members" for p in problems)


def test_validate_layout_checks_resource_cost_fields_and_entity_holding_cost():
    """ROADMAP 2.2: cost_busy/cost_idle/cost_per_use must be numbers >= 0,
    cost_time_unit a known unit (or "base"), and an entity's holding_cost
    must be a number >= 0."""
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [], "links": [], "variables": [],
        "resources": [
            {"name": "A", "capacity": 1, "discipline": "FIFO", "cost_busy": -5},
            {"name": "B", "capacity": 1, "discipline": "FIFO", "cost_time_unit": "fortnights"},
        ],
        "entities": [{"name": "DEFAULT", "holding_cost": -1}],
    }

    problems = [p.message for p in validate_layout_structured(layout)]

    assert any(p == "resource 'A': cost_busy must be a number >= 0" for p in problems)
    assert any("cost_time_unit" in p and "fortnights" in p for p in problems)
    assert any(p == "entity 'DEFAULT': holding_cost must be a number >= 0" for p in problems)


def test_validate_layout_accepts_well_formed_cost_fields():
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [], "links": [], "variables": [],
        "resources": [{"name": "A", "capacity": 1, "discipline": "FIFO",
                       "cost_busy": 10.0, "cost_idle": 2.0, "cost_per_use": 0.5,
                       "cost_time_unit": "hours"}],
        "entities": [{"name": "DEFAULT", "holding_cost": 0.1}],
    }
    assert validate_layout_structured(layout) == []


def test_validate_layout_accepts_a_well_formed_set_and_ranked_resource():
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [], "links": [], "variables": [],
        "resources": [
            {"name": "A", "capacity": 1, "discipline": "FIFO"},
            {"name": "B", "capacity": 2, "discipline": "HighestValueFirst",
             "ranking_expr": "entity.urgency"},
        ],
        "resource_sets": [{"name": "Pool", "members": ["A", "B"], "rule": "SmallestNumberBusy"}],
    }
    assert validate_layout_structured(layout) == []


def test_validate_layout_reports_a_bad_expression_with_the_block_name():
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [
            {"name": "assign_1", "type": "ASSIGN", "position": [0, 0],
             "params": {"assignments": [{"target": "entity.x", "expr": "__import__('os')"}]}},
            {"name": "decide_1", "type": "DECIDE", "position": [1, 0],
             "params": {"mode": "Condition", "probs": [], "conditions": ["entity.x >"]}},
            {"name": "delay_1", "type": "DELAY", "position": [2, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": "entity.x +"}}},
        ],
        "links": [], "variables": [],
    }

    problems = [p.message for p in validate_layout_structured(layout)]

    assert any(p.startswith("assign_1: __import__('os'): ") for p in problems)
    assert any(p.startswith("decide_1: entity.x >: ") for p in problems)
    assert any(p.startswith("delay_1: entity.x +: ") for p in problems)


def test_validate_layout_checks_variable_names_and_assignment_targets():
    layout = {
        "schema_version": CURRENT_VERSION,
        "blocks": [{"name": "assign_1", "type": "ASSIGN", "position": [0, 0],
                    "params": {"assignments": [{"target": "missing_var", "expr": "wip + 1"}]}}],
        "links": [],
        "variables": [{"name": "wip", "initial": 0}, {"name": "wip", "initial": 1},
                      {"name": "2bad", "initial": 0}, {"name": "now", "initial": 0}],
    }

    problems = [p.message for p in validate_layout_structured(layout)]

    assert "duplicate variable name: wip" in problems
    assert any("'2bad': not a valid identifier" in p for p in problems)
    assert any("'now': reserved name" in p for p in problems)
    assert "assign_1: missing_var: undeclared variable" in problems


def test_future_schema_version_is_refused():
    layout = {"schema_version": CURRENT_VERSION + 1, "blocks": [], "links": []}
    with pytest.raises(ModelValidationError):
        migrate(layout)


def test_validate_layout_reports_unknown_distribution_and_missing_link_target():
    layout = {
        "schema_version": 4,
        "blocks": [
            {"name": "create_1", "type": "CREATE", "position": [0, 0],
             "params": {"e_type": "DEFAULT", "distribution": "Empirical", "dist_params": {},
                        "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
            {"name": "dispose_1", "type": "DISPOSE", "position": [1, 0], "params": {}},
        ],
        "links": [["create_1", "dispose_1"], ["dispose_1", "missing_block"]],
    }

    problems = [p.message for p in validate_layout_structured(layout)]

    assert any("unknown distribution" in p for p in problems)
    assert any("missing_block" in p for p in problems)


def test_validate_layout_warns_on_unbounded_below_duration_distribution():
    layout = {
        "schema_version": 4,
        "blocks": [
            {"name": "delay_1", "type": "DELAY", "position": [0, 0],
             "params": {"distribution": "Normal", "dist_params": {"mean": 0.1, "stddev": 1}}},
        ],
        "links": [],
    }

    problems = validate_layout_structured(layout)

    assert any(p.severity == "warning" and "delay_1" in p.message and "Normal" in p.message
               for p in problems)


def test_curated_example_models_validate_clean():
    for filename in CURATED_EXAMPLES:
        migrated = migrate(json.loads((MODELS_DIR / filename).read_text()))
        assert validate_layout_structured(migrated) == [], filename


# --- validate_graph_structured / validate_model -----------------------------------------

def _model(blocks, links):
    return {"schema_version": 4, "blocks": blocks, "links": links,
            "resources": [], "entities": [], "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}


def _block(name, btype, params=None):
    return {"name": name, "description": name, "type": btype, "position": [0, 0], "params": params or {}}


def test_validate_graph_requires_create_and_dispose():
    layout = _model([_block("delay_1", "DELAY")], [])
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("no CREATE block" in p for p in problems)
    assert any("no DISPOSE block" in p for p in problems)


def test_validate_graph_terminal_non_dispose_is_a_problem():
    layout = _model(
        [_block("create_1", "CREATE"), _block("delay_1", "DELAY")],
        [["create_1", "delay_1"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("delay_1" in p and "not DISPOSE" in p for p in problems)


def test_validate_graph_cycle_does_not_recurse():
    # create_1 -> a -> b -> a (rework loop, cycles are legal) -> b -> dispose_1
    decide = _block("b", "DECIDE", {"mode": "Chance", "probs": [0.5], "conditions": []})
    layout = _model(
        [_block("create_1", "CREATE"), _block("a", "DELAY"), decide, _block("dispose_1", "DISPOSE")],
        [["create_1", "a"], ["a", "b"], ["b", "a"], ["b", "dispose_1"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert problems == []


def test_validate_graph_warns_on_unreachable_block():
    layout = _model(
        [_block("create_1", "CREATE"), _block("dispose_1", "DISPOSE"), _block("orphan", "DELAY")],
        [["create_1", "dispose_1"]],
    )
    problems = validate_graph_structured(layout)
    assert any(p.severity == "warning" and "orphan" in p.message for p in problems)


def test_validate_graph_extra_outgoing_links_reported():
    layout = _model(
        [_block("create_1", "CREATE"), _block("delay_1", "DELAY"), _block("dispose_1", "DISPOSE"),
         _block("dispose_2", "DISPOSE")],
        [["create_1", "delay_1"], ["delay_1", "dispose_1"], ["delay_1", "dispose_2"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("delay_1" in p and "outgoing links" in p for p in problems)


def test_validate_graph_create_needs_outgoing_link():
    layout = _model([_block("create_1", "CREATE"), _block("dispose_1", "DISPOSE")], [])
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("create_1" in p and "no outgoing link" in p for p in problems)


def test_validate_graph_dispose_must_have_no_outgoing_link():
    layout = _model(
        [_block("create_1", "CREATE"), _block("dispose_1", "DISPOSE")],
        [["create_1", "dispose_1"], ["dispose_1", "create_1"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("dispose_1" in p and "no outgoing link" in p for p in problems)


def test_validate_graph_decide_chance_needs_else_branch_when_probs_sum_below_one():
    decide = _block("decide_1", "DECIDE", {"mode": "Chance", "probs": [0.5], "conditions": []})
    layout = _model(
        [_block("create_1", "CREATE"), decide, _block("dispose_1", "DISPOSE")],
        [["create_1", "decide_1"], ["decide_1", "dispose_1"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("decide_1" in p and "ELSE" in p for p in problems)


def test_validate_graph_decide_chance_ok_with_else_branch():
    decide = _block("decide_1", "DECIDE", {"mode": "Chance", "probs": [0.5], "conditions": []})
    layout = _model(
        [_block("create_1", "CREATE"), decide, _block("dispose_1", "DISPOSE"), _block("dispose_2", "DISPOSE")],
        [["create_1", "decide_1"], ["decide_1", "dispose_1"], ["decide_1", "dispose_2"]],
    )
    assert validate_graph_structured(layout) == []


def test_validate_graph_decide_chance_probs_over_one_is_a_problem():
    decide = _block("decide_1", "DECIDE", {"mode": "Chance", "probs": [0.6, 0.6], "conditions": []})
    layout = _model(
        [_block("create_1", "CREATE"), decide, _block("dispose_1", "DISPOSE"), _block("dispose_2", "DISPOSE")],
        [["create_1", "decide_1"], ["decide_1", "dispose_1"], ["decide_1", "dispose_2"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("decide_1" in p and "must be <= 1" in p for p in problems)


def test_validate_graph_decide_condition_mode_checks_link_count():
    decide = _block("decide_1", "DECIDE", {"mode": "Condition", "probs": [], "conditions": ["True", "False"]})
    layout = _model(
        [_block("create_1", "CREATE"), decide, _block("dispose_1", "DISPOSE")],
        [["create_1", "decide_1"], ["decide_1", "dispose_1"]],
    )
    problems = [p.message for p in validate_graph_structured(layout)]
    assert any("decide_1" in p and "conditions" in p for p in problems)


def test_curated_example_models_validate_clean_with_validate_model():
    for filename in CURATED_EXAMPLES:
        migrated = migrate(json.loads((MODELS_DIR / filename).read_text()))
        non_warnings = [p for p in validate_model(migrated) if not p.startswith("warning:")]
        assert non_warnings == [], filename


# ======================= generated JSON Schema (P3.1) =====================
ROOT = Path(__file__).resolve().parents[1]
MODELS = sorted((ROOT / "nagasim" / "models").glob("*.json"))


def _layout(**overrides):
    base = {
        "schema_version": S.CURRENT_VERSION,
        "blocks": [
            {"name": "c", "type": "CREATE", "position": [0, 0],
             "params": {"distribution": "Exponential", "dist_params": {"rate": 1.0},
                        "max_arrivals": 3}},
            {"name": "d", "type": "DISPOSE", "position": [100, 0], "params": {}},
        ],
        "links": [{"source": "c", "target": "d"}],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [], "annotations": [],
        "submodels": {}, "background": None, "info": {},
        "simulation": {"time_limit": 100, "warmup": 0, "replications": 1},
    }
    base.update(overrides)
    return base


# -- structured problems -----------------------------------------------------

def test_a_valid_model_has_no_problems():
    assert S.validate_model_structured(_layout()) == []


def test_every_code_emitted_is_declared_and_severity_matches_the_prefix():
    layout = _layout(blocks=[
        {"name": "c", "type": "NOPE", "position": [0, 0], "params": {}},
        {"name": "c", "type": "DELAY", "position": [0, 0],
         "params": {"distribution": "Normal", "dist_params": {"mean": 1, "sd": 1}}},
    ], links=[{"source": "c", "target": "ghost"}])
    problems = S.validate_model_structured(layout)
    assert problems
    for p in problems:
        assert p.code in PROBLEM_CODES, f"undeclared code {p.code}"
        assert p.severity == ("warning" if p.code.startswith("W_") else "error")
    codes = {p.code for p in problems}
    assert {"E_UNKNOWN_BLOCK_TYPE", "E_LINK_UNKNOWN_BLOCK", "E_DUPLICATE_BLOCK",
            "W_UNBOUNDED_DURATION"} <= codes


def test_validate_model_is_a_formatter_over_the_structured_one():
    """Messages must not change: every existing caller and test reads them."""
    layout = _layout(blocks=[{"name": "c", "type": "CREATE", "position": [0, 0],
                              "params": {"nonsense": 1}}],
                     links=[])
    assert S.validate_model(layout) == [p.message for p in S.validate_model_structured(layout)]


def test_a_problem_carries_a_path_a_block_and_a_hint():
    layout = _layout()
    layout["blocks"][0]["params"]["dist_params"] = {"rate": "entity.nope +"}
    problem = next(p for p in S.validate_model_structured(layout) if p.code == "E_EXPRESSION")
    assert problem.path == "/blocks/0/params/dist_params/rate"
    assert problem.block == "c"
    assert problem.hint


def test_the_missing_dispose_hint_says_what_to_do():
    """ROADMAP 3.1: hints are concrete, not a restatement of the problem."""
    layout = _layout(blocks=[{"name": "c", "type": "CREATE", "position": [0, 0],
                              "params": {"max_arrivals": 1}}], links=[])
    hints = {p.code: p.hint for p in S.validate_model_structured(layout)}
    assert "add a DISPOSE block" in hints["E_NO_DISPOSE"]
    assert "add a DISPOSE block or link this block to one" == hints["E_TERMINAL_NOT_DISPOSE"]


def test_unknown_enumerations_hint_with_the_allowed_values():
    layout = _layout(resources=[{"name": "R", "capacity": 1, "discipline": "RANDOMISH"}])
    problem = next(p for p in S.validate_model_structured(layout)
                   if p.code == "E_RESOURCE_DISCIPLINE")
    assert "use one of:" in problem.hint and "FIFO" in problem.hint


# -- JSON Schema -------------------------------------------------------------

def test_the_schema_is_self_consistent():
    """Structural self-check, always run: every $ref resolves and every block
    type and distribution the registries know has a variant."""
    schema = build_json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    defs = schema["$defs"]

    refs = []

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                refs.append(node["$ref"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert refs
    for ref in refs:
        assert ref.startswith("#/$defs/"), ref
        assert ref.split("/")[-1] in defs, f"dangling {ref}"

    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.core.distributions import DISTRIBUTION_REGISTRY
    block_titles = {v["title"] for v in defs["block"]["oneOf"]}
    assert block_titles == set(BLOCK_REGISTRY)
    dist_titles = {v["title"] for v in defs["duration"]["oneOf"]}
    assert dist_titles == set(DISTRIBUTION_REGISTRY)


def test_every_bundled_example_validates_against_the_schema():
    """`jsonschema` is NOT a dependency (packaging/product.spec even excludes
    it); when the machine happens to have it, use it for the real check.
    One validator built once, then every bundled example through it."""
    jsonschema = pytest.importorskip(
        "jsonschema", reason="jsonschema is not a dependency; the structural self-check "
                             "above is the always-on guard")
    schema = build_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)

    found = []
    for path in MODELS:
        layout = S.migrate(json.loads(path.read_text(encoding="utf-8")))
        found += [f"{path.stem} {e.json_path}: {e.message}"
                  for e in sorted(validator.iter_errors(layout), key=lambda e: e.json_path)]
    assert not found, "\n".join(found[:10])


def test_plugin_registrations_reach_the_schema(monkeypatch):
    """A plugin block must appear without touching modelschema.py."""
    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.core.block_spec import BlockSpec, ParamSpec
    from nagasim.core.blocks.delay import Delay

    spec = BlockSpec(type_name="PLUGINISH", cls=Delay,
                     params=(ParamSpec("amount", float, 1.0, description="how much"),),
                     factory=lambda env, block: None)
    monkeypatch.setitem(BLOCK_REGISTRY, "PLUGINISH", spec)
    titles = {v["title"] for v in build_json_schema()["$defs"]["block"]["oneOf"]}
    assert "PLUGINISH" in titles


# -- CLI ---------------------------------------------------------------------

def test_the_schema_and_validate_cli_modes_work(tmp_path, capsys):
    """Both CLI modes in process: `nagasim.cli.main` IS the dispatcher, nothing
    here needs process isolation, and tests/test_packaging.py owns the one
    subprocess that proves the module entry points (suite reduction, P4-D5)."""
    from nagasim.cli import main as launcher_main

    out = tmp_path / "model.schema.json"
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(_layout(links=[{"source": "c", "target": "ghost"}])))
    good_model = ROOT / "nagasim" / "models" / "job_shop_jackson.json"

    assert launcher_main(["schema", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["$defs"]["block"]["oneOf"]

    capsys.readouterr()                       # drop the schema chatter
    assert launcher_main(["validate", str(good_model), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == []

    assert launcher_main(["validate", str(broken), "--json"]) == 1
    problems = json.loads(capsys.readouterr().out.strip())
    assert "E_LINK_UNKNOWN_BLOCK" in {p["code"] for p in problems}
