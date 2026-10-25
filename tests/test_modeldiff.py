"""Semantic diff (PHASE6 W7 Q1, ARCHITECTURE D124). The independent oracle,
tests/test_modeldiff_oracle.py, covers the hand-derived cases from the plan; this
file covers the identity-discovery mechanism itself and the sections the
oracle does not touch (network, orders, annotations, list leaves, the CLI's
error path)."""
import json

from conftest import chain, create, dispose, layout, process

from nagasim import modeldiff


def _resource(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0, "cost_time_unit": "hours"}


def test_section_identities_discovered_not_hardcoded():
    """A sample of sections spanning several phases: if a new one appears in
    the schema, it is picked up automatically -- no per-section registration."""
    identities = modeldiff._section_identities()
    assert len(identities) > 20
    for name_keyed in ("resources", "tables", "levels", "flows", "auxiliaries",
                       "sequences", "fleets", "scenarios", "data_sources", "reality_checks"):
        assert identities[name_keyed] == ("name",)
    assert identities["orders"] == ("id",)
    assert identities["annotations"] == ("id",)
    assert identities["blocks"] == ("name",)
    assert identities["links"][:2] == ("source", "target")


def test_network_nodes_and_segments_matched_by_identity():
    a = layout([], [], network={"nodes": [{"name": "dock", "x": 0, "y": 0}],
                                 "segments": [{"from": "dock", "to": "aisle", "length": 10}]})
    b = layout([], [], network={"nodes": [{"name": "dock", "x": 5, "y": 0}],
                                 "segments": [{"from": "dock", "to": "aisle", "length": 10},
                                              {"from": "aisle", "to": "pack", "length": 5}]})
    changes = modeldiff.diff(a, b)
    kinds = {(c.kind, c.path) for c in changes}
    assert ("changed", "network.nodes.dock.x") in kinds
    assert ("added", "network.segments.aisle->pack") in kinds


def test_orders_and_annotations_matched_by_id():
    a = layout([], [], orders=[{"id": "WO-1", "release": 0, "quantity": 1}],
               annotations=[{"id": "note1", "kind": "text", "text": "old"}])
    b = layout([], [], orders=[{"id": "WO-1", "release": 0, "quantity": 2}],
               annotations=[{"id": "note1", "kind": "text", "text": "new"}])
    changes = modeldiff.diff(a, b)
    assert {(c.kind, c.path, c.old, c.new) for c in changes} == {
        ("changed", "orders.WO-1.quantity", 1, 2),
        ("changed", "annotations.note1.text", "old", "new"),
    }


def test_list_leaf_diffs_by_whole_value_not_identity():
    """resource_sets[].members is a list of plain strings, not records: it is
    a leaf, compared whole, never split into per-item added/removed."""
    a = layout([], [], resource_sets=[{"name": "Drilling", "members": ["Drill1", "Drill2"],
                                       "rule": "PreferredOrder"}])
    b = layout([], [], resource_sets=[{"name": "Drilling", "members": ["Drill1", "Drill2", "Drill3"],
                                       "rule": "PreferredOrder"}])
    changes = modeldiff.diff(a, b)
    assert len(changes) == 1
    assert changes[0].kind == "changed"
    assert changes[0].path == "resource_sets.Drilling.members"
    assert changes[0].old == ["Drill1", "Drill2"]
    assert changes[0].new == ["Drill1", "Drill2", "Drill3"]


def test_parallel_links_with_no_port_tiebreak_by_position():
    """Two links between the same pair with no port info cannot be told apart
    by identity alone; the second one added is reported added, not as a
    spurious change to the first (ARCHITECTURE ceilings table)."""
    c, d = create(), dispose()
    a = layout([c, d], [{"source": "create_1", "target": "dispose_1"}])
    b = layout([c, d], [{"source": "create_1", "target": "dispose_1"},
                        {"source": "create_1", "target": "dispose_1"}])
    changes = modeldiff.diff(a, b)
    assert len(changes) == 1
    assert changes[0].kind == "added"
    assert changes[0].path == "links.create_1->dispose_1"


def test_submodels_dict_section_reports_whole_record_added_removed():
    """submodels is a {name: definition} object, not a list -- the dict key
    IS the identity, so a whole submodel appearing/disappearing is one
    added/removed change, not a flood of field-level "changed X -> None"."""
    a = layout([], [], submodels={"Cell": {"parameters": [], "blocks": [], "links": []}})
    b = layout([], [], submodels={})
    changes = modeldiff.diff(a, b)
    assert len(changes) == 1
    assert changes[0].kind == "removed"
    assert changes[0].path == "submodels.Cell"


def test_textconv_output_has_one_line_per_record():
    c, p, d = create(), process(), dispose()
    doc = layout([c, p, d], chain(c, p, d), resources=[_resource("Server")])
    text = modeldiff.textconv(doc)
    lines = text.splitlines()
    assert any(line.startswith("blocks.process_1 ") for line in lines)
    assert any(line.startswith("resources.Server ") for line in lines)
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_cli_json_output(tmp_path):
    c, p_old, d = create(), process(value=1.0), dispose()
    p_new = process(value=2.0)
    a = layout([c, p_old, d], chain(c, p_old, d), resources=[_resource("Server")])
    b = layout([c, p_new, d], chain(c, p_new, d), resources=[_resource("Server")])
    path_a, path_b = tmp_path / "a.json", tmp_path / "b.json"
    path_a.write_text(json.dumps(a))
    path_b.write_text(json.dumps(b))

    from nagasim.cli import main as launcher_main
    code = launcher_main(["diff", str(path_a), str(path_b), "--json"])
    assert code == 1


def test_cli_missing_file_exits_2(capsys):
    from nagasim.cli import main as launcher_main
    code = launcher_main(["diff", "does-not-exist-a.json", "does-not-exist-b.json"])
    assert code == 2
    assert "naga diff" in capsys.readouterr().err


def test_gaining_counter_offsets_is_a_canvas_only_change():
    c, d = create(), dispose()
    a = layout([c, d], chain(c, d))
    gained = {**c, "counter_offsets": {"out": [4, -6]}}
    b = layout([gained, d], chain(c, d))
    changes = modeldiff.diff(a, b)
    assert [(ch.kind, ch.path) for ch in changes] == [("moved", "blocks.create_1.counter_offsets")]
    assert modeldiff._only_moved(c, gained)
