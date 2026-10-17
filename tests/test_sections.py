"""The section table (core/sections.py, ARCH-REVIEW B1) against the hand lists
it replaced, snapshot here as literals, and against the JSON Schema."""
import importlib

from nagasim import experiments
from nagasim import agent_tools
from nagasim.core import outline, schema, sections, submodels
from nagasim.core.document import ModelDocument
from nagasim.modelschema import build_json_schema

# The hand lists as they were before the table (order included where it is
# observable: RECORD_SECTIONS is the saved file's key order).
_OLD_RECORD = ("resources", "resource_sets", "schedules", "failures", "tables",
               "sequences", "variables", "entities", "timeseries_inputs",
               "levels", "flows", "conversions", "fixed_costs", "fleets",
               "motion_profiles", "chargers", "auxiliaries", "delays", "subscripts", "agent_types",
               "populations",
               "reality_checks")
_OLD_CLONABLE = {"resources", "resource_sets", "schedules", "failures", "levels", "flows",
                 "fixed_costs", "conversions", "fleets", "variables", "auxiliaries", "delays",
                 "agent_types", "populations", "chargers", "motion_profiles"}
_OLD_CONTINUOUS = {"levels", "flows", "fixed_costs", "conversions", "fleets", "auxiliaries",
                   "delays", "agent_types", "populations", "chargers"}
_OLD_NAMED = {"blocks", "resources", "resource_sets", "schedules", "failures", "variables",
              "timeseries_inputs", "levels", "flows", "fixed_costs", "tables", "sequences",
              "fleets", "motion_profiles", "chargers", "auxiliaries", "delays", "agent_types",
              "populations", "conversions"}          # D145 (2026-09-27): conversions became overridable too.
# "entities" was in the old list too, but dead: apply_overrides rejected the path first.
_OLD_BINDABLE = {"resources", "resource_sets", "variables", "schedules", "failures", "tables"}
_OLD_MIGRATE = {"resource_sets": [], "links": [], "resources": [], "entities": [],
                "variables": [], "schedules": [], "failures": [], "scenarios": [],
                "simulation": {"time_limit": None, "warmup": 0, "replications": 1,
                               "time_unit": "minutes", "length_unit": "meters",
                               "currency": "EUR", "energy_price": 0},
                "annotations": [], "submodels": {}, "background": None, "info": {},
                "data_sources": [], "data_bindings": [], "initial_state": {}, "tables": [],
                "sequences": [], "orders": [], "timeseries_inputs": [], "levels": [],
                "flows": [], "fixed_costs": [], "conversions": [], "network": None,
                "fleets": [], "motion_profiles": [], "chargers": [], "auxiliaries": [], "delays": [], "subscripts": [],
                "agent_types": [], "populations": [], "reality_checks": [], "assertions": [],
                "schema_version": schema.CURRENT_VERSION}


def test_table_still_holds_the_old_hand_lists():
    """The old lists are frozen above; a new section may extend the table
    (its own row is covered by the schema round trip below), but nothing the
    old lists named may drop out or change."""
    assert [s for s in sections.RECORD_SECTIONS if s in _OLD_RECORD] == list(_OLD_RECORD)
    assert sections.RECORD_SECTIONS == submodels.RECORD_SECTIONS
    assert _OLD_CLONABLE <= set(submodels._CLONABLE_SECTIONS)
    assert _OLD_CONTINUOUS <= set(submodels._CONTINUOUS_SECTIONS)
    assert _OLD_NAMED <= set(experiments._NAMED_SECTIONS)
    assert experiments._ID_SECTIONS == ("orders",)
    assert _OLD_NAMED | {"orders", "links"} <= set(experiments._SECTIONS)
    assert _OLD_BINDABLE <= set(agent_tools._BINDABLE)
    migrated = schema.migrate({"schema_version": schema.CURRENT_VERSION})
    for key, old in _OLD_MIGRATE.items():
        got = migrated[key]
        if isinstance(old, dict) and old:     # a new default key may join `simulation`
            assert old.items() <= got.items(), key
        else:
            assert got == old, key


def test_every_section_is_in_the_json_schema_and_back():
    json_schema = build_json_schema()
    top = json_schema["properties"]
    assert set(top) - {"$schema", "schema_version"} == {s.name for s in sections.SECTIONS}
    # The default shape names a `$def` after the row: it must exist.
    for name, spec in top.items():
        ref = (spec.get("items") or spec).get("$ref")
        assert ref is None or ref.rsplit("/", 1)[1] in json_schema["$defs"], name


def test_every_validate_hook_resolves():
    for s in sections.SECTIONS:
        if s.validate:
            module, _, function = s.validate.partition(":")
            assert callable(getattr(importlib.import_module(module), function)), s.name


def test_generated_aliases_edit_through_the_generic_methods():
    doc = ModelDocument()
    doc.upsert_level({"name": "Tank", "initial": 1})
    doc.upsert_level({"name": "Tank", "initial": 2})         # replaced in place
    doc.rename_level("Tank", "Silo")
    assert doc.levels == [{"name": "Silo", "initial": 2}]
    doc.upsert_order({"id": "WO-1", "due": 5})                 # keyed by "id"
    doc.remove_order("WO-1")
    doc.remove_level("Silo")
    assert doc.levels == [] and doc.orders == []
    doc.undo()
    assert doc.levels == [{"name": "Silo", "initial": 2}]
    for s in sections.SECTIONS:
        if s.item:
            assert callable(getattr(doc, f"set_{s.name}")), s.name


def test_capabilities_of_every_bundled_model():
    """U2/D151: every bundled model's derived capabilities; a few hand-checked,
    and each one names only sections the table knows."""
    import json
    from pathlib import Path

    models = Path(sections.__file__).resolve().parents[1] / "models"
    found = {p.stem: sections.capabilities(json.loads(p.read_text(encoding="utf-8")))
             for p in sorted(models.glob("*.json"))}
    assert found["single_server"] == {"process"}
    assert found["sd_sir"] == {"system_dynamics"}          # a blockless SD asset, not a process
    assert found["agents_machine_repair"] == {"agents"}
    assert found["clinic_twin"] == {"process", "data"}
    assert found["warehouse_forklifts"] == {"process", "logistics", "planning"}
    assert all(caps <= set(sections.CAPABILITIES) and caps for caps in found.values())
    known = {s.name for s in sections.SECTIONS}
    assert all(set(secs) <= known for _doc, secs in sections.CAPABILITIES.values())
    assert sections.capabilities({}) == set()


# What the editor's hand lists said before the table carried it (D185): the
# Model data tab each section sat under. A section left out had no tab.
_OLD_TABS = {"orders": "Orders", "resources": "Resources", "resource_sets": "Resource sets",
             "schedules": "Schedules", "failures": "Failures", "tables": "Lookup tables",
             "sequences": "Sequences", "variables": "Variables", "entities": "Entities",
             "timeseries_inputs": "Time series", "levels": "Stocks", "flows": "Stocks",
             "conversions": "Conversions", "fixed_costs": "Costs", "fleets": "Fleets",
             "motion_profiles": "Motion profiles", "chargers": "Chargers",
             "auxiliaries": "Stocks", "delays": "Stocks", "agent_types": "Agents",
             "populations": "Agents", "data_sources": "Data sources",
             "data_bindings": "Bindings", "initial_state": "Initial state",
             "investment": "Investment", "dashboard": "Dashboard"}


def test_headings_match_the_editors_old_tab_lists():
    """D202: `Section.tab` is the heading a client lists a section under,
    here as the editor's own lists had it; C13: every heading in one of the
    families, sections sharing a heading in the same one, none without."""
    assert _OLD_TABS.items() <= {s.name: s.tab for s in sections.SECTIONS if s.tab}.items()
    assert {s.group for s in sections.SECTIONS if s.tab} == set(sections.FAMILIES)
    assert all(s.group == "" for s in sections.SECTIONS if not s.tab)


def test_what_a_client_derives_from_the_headings_is_well_defined():
    """D203/D204. A heading shows or hides as a whole: its sections agree on
    the capability that owns them. A kind (a section's `item`) names one
    section, and it is the kind the outline already uses. A capability is
    first edited under the heading of the first section it lists."""
    owner = {section: name for name, (_doc, owned) in sections.CAPABILITIES.items()
             for section in owned}
    owners = {}
    for s in sections.SECTIONS:
        if s.tab:
            owners.setdefault(s.tab, set()).add(owner.get(s.name))
    assert all(len(found) == 1 for found in owners.values()), owners
    item = {s.name: s.item for s in sections.SECTIONS if s.item}
    assert len(set(item.values())) == len(item)
    assert all(item[section] == kind for _label, section, kind in outline.RECORD_GROUPS)
    assert {name: next((_OLD_TABS[s] for s in owned if s in _OLD_TABS), None)
            for name, (_doc, owned) in sections.CAPABILITIES.items()} == {
        "process": None, "system_dynamics": "Stocks", "agents": "Agents",
        "data": "Data sources", "logistics": "Fleets", "planning": "Orders"}
