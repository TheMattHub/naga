"""Three-way merge (PHASE6 W7 Q2, ARCHITECTURE D125). The independent oracle,
tests/test_modelmerge_oracle.py, covers the plan's hand-derived cases; this file
covers the rules it does not touch: list order, both-added records, moves
against deletes, network/submodels, and the CLI's --json, default output and
error path."""
import copy
import json

from conftest import chain, create, delay, dispose, layout, process

from nagasim import modeldiff
from nagasim.cli import main as launcher_main


def _resource(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0, "cost_time_unit": "hours"}


def _base():
    c, p, d = create(), process(), dispose()
    return layout([c, p, d], chain(c, p, d), resources=[_resource("Server")])


def test_order_is_ours_then_theirs_additions_in_theirs_order():
    base = _base()
    ours = copy.deepcopy(base)
    ours["resources"] = [_resource("B"), _resource("Server")]       # reorder + add
    theirs = copy.deepcopy(base)
    theirs["resources"] = [_resource("Z"), _resource("Server"), _resource("A")]
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert conflicts == []
    assert [r["name"] for r in merged["resources"]] == ["B", "Server", "Z", "A"]


def test_record_added_on_both_sides_differently_conflicts_keeps_ours():
    base = _base()
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    ours["resources"].append(_resource("Spare", 2))
    theirs["resources"].append(_resource("Spare", 3))
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert [(c.path, c.reason, c.base) for c in conflicts] == [("resources.Spare", "added", None)]
    assert conflicts[0].theirs["capacity"] == 3
    assert {r["name"]: r["capacity"] for r in merged["resources"]}["Spare"] == 2


def test_moves_never_conflict():
    base = _base()
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    theirs["blocks"][1]["position"] = [7, 7]            # only theirs moves process_1
    ours["blocks"][2]["params"]["dist_params"]["value"] = 1.0  # unrelated real edit
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert conflicts == []
    assert merged["blocks"][1]["position"] == [7, 7]

    # A delete against a pure move: the delete wins, no conflict.
    ours = layout([base["blocks"][0], base["blocks"][2]], chain(base["blocks"][0], base["blocks"][2]),
                  resources=[_resource("Server")])
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert conflicts == []
    assert "process_1" not in {b["name"] for b in merged["blocks"]}


def test_field_deleted_on_one_side_changed_on_other_conflicts():
    base = _base()
    base["resources"][0]["note"] = "a"
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    del ours["resources"][0]["note"]
    theirs["resources"][0]["note"] = "b"
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert [(c.path, c.reason, c.ours, c.theirs) for c in conflicts] == [
        ("resources.Server.note", "delete-modify", None, "b")]
    assert "note" not in merged["resources"][0]


def test_network_and_submodels_merge_by_identity():
    net = {"nodes": [{"name": "dock", "x": 0, "y": 0}], "segments": []}
    base = layout([], [], network=net, submodels={"Cell": {"parameters": [], "blocks": [], "links": []}})
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    ours["network"]["nodes"][0]["x"] = 5
    theirs["network"]["nodes"].append({"name": "pack", "x": 9, "y": 0})
    theirs["submodels"]["Line"] = {"parameters": [], "blocks": [], "links": []}
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    assert [n["name"] for n in merged["network"]["nodes"]] == ["dock", "pack"]
    assert merged["network"]["nodes"][0]["x"] == 5
    assert set(merged["submodels"]) == {"Cell", "Line"}
    assert conflicts == []


def test_link_to_block_the_other_side_deleted_is_a_validation_conflict():
    """Theirs deletes delay_1; ours adds a link into it. The dangling link is
    ours' own record, so the merged file keeps it and the conflict says why."""
    c, p, dl, d = create(), process(), delay(), dispose()
    base = layout([c, p, dl, d], chain(c, p, d), resources=[_resource("Server")])
    ours = copy.deepcopy(base)
    ours["links"].append({"source": "delay_1", "target": "dispose_1"})
    theirs = layout([c, p, d], chain(c, p, d), resources=[_resource("Server")])
    merged, conflicts = modeldiff.merge(base, ours, theirs)
    validation = [x for x in conflicts if x.reason == "validation"]
    assert [x.path for x in validation] == ["links.delay_1->dispose_1"]
    assert "unknown block: delay_1" in validation[0].detail


def test_cli_default_overwrites_ours_and_json(tmp_path, capsys):
    base = _base()
    ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
    ours["resources"][0]["capacity"] = 2
    theirs["resources"][0]["capacity"] = 3
    paths = []
    for name, doc in (("base", base), ("ours", ours), ("theirs", theirs)):
        paths.append(tmp_path / f"{name}.json")
        paths[-1].write_text(json.dumps(doc))
    code = launcher_main(["merge", *map(str, paths), "--json"])
    out = capsys.readouterr()
    assert code == 1
    assert "conflict resources.Server.capacity (changed)" in out.err
    assert json.loads(out.out)[0]["path"] == "resources.Server.capacity"
    written = json.loads(paths[1].read_text())     # %A overwritten, keeps ours
    assert written["resources"][0]["capacity"] == 2


def test_cli_error_exit_2(tmp_path, capsys):
    missing = str(tmp_path / "nope.json")
    assert launcher_main(["merge", missing, missing, missing]) == 2
    assert "naga merge" in capsys.readouterr().err
