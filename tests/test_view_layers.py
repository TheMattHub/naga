"""Plan K2 Task 1 (spec tests, written from the brief before the code): the model's 3D layer keys,
`view.layers` (D1516), a block's user `layer` (D1513) and `queue_row.cell` (D1517). Engine only (no
product import), so the naga export carries it; the drawing side is in `test_view3d_layers.py`.

Assumptions where the brief leaves a detail open:
- `view_settings(view)["layers"]` is a dict; a kind or user layer missing from it is shown, so
  readers use `.get(name, True)`. A bad value is dropped (read as shown), a bool is kept.
- A cleared block layer reads as the key absent from the saved block or None (both accepted).
- A bad layer or cell is a `W_VIEW_SETTING` problem of severity warning whose path names `layers`,
  `layer` or `cell`; the exact path text is not fixed.
- The block's `layer` is a field of `ModelDocument.blocks[name]` called `layer`, as `queue_row` is.
- `set_view(layers=...)` merges into the stored layers (as `draw_guard` merges its keys) and a
  bad value is E_VIEW, like the other `set_view` arguments.
"""
import json
from pathlib import Path

import pytest

from nagasim.core import queuerow
from nagasim.core.document import ModelDocument, view_settings

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _layout():
    return json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))


def _process(layout):
    return next(b for b in layout["blocks"] if str(b.get("type")).upper() == "PROCESS")


def _load(tmp_path, layout):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(layout), encoding="utf-8")
    return ModelDocument.load(str(path)), path


def _warnings(doc, *words):
    return [p for p in doc.problems() if p.code == "W_VIEW_SETTING" and p.severity == "warning"
            and any(w in p.path for w in words)]


def _errors(doc):
    # The file's own errors before any K2 key (none for the bundled model).
    return [p for p in doc.problems() if p.severity == "error"]


def _same_results(a, b):
    assert set(a) == set(b)
    assert all(a[k].equals(b[k]) if hasattr(a[k], "equals") else a[k] == b[k] for k in a)


def _run(layout):
    from nagasim import api

    return api.run_model(json.loads(json.dumps(layout)), replications=1, seed=3, run_length=200)


def _with_cell(cell):
    layout = _layout()
    _process(layout)["queue_row"] = {"slots": 2, "cell": cell}
    return layout


def test_view_settings_returns_layers_and_a_missing_layer_is_shown():
    assert isinstance(view_settings({})["layers"], dict)
    assert view_settings({})["layers"].get("tiles", True) is True
    out = view_settings({"layers": {"tiles": False, "Racks": False, "names": True}})["layers"]
    assert out.get("tiles", True) is False
    assert out.get("names", True) is True
    assert out.get("pads", True) is True                      # not mentioned: shown
    assert out["Racks"] is False                              # unknown name with a bool: kept


@pytest.mark.parametrize("layers", [{"tiles": "no"}, {"tiles": 0}, {"tiles": None}, {"tiles": [False]}])
def test_a_non_bool_layer_value_warns_and_reads_as_shown(tmp_path, layers):
    doc, _ = _load(tmp_path, {**_layout(), "view": {"layers": layers}})
    assert view_settings(doc.view)["layers"].get("tiles", True) is True
    assert len(_warnings(doc, "layers")) == 1, [p.path for p in doc.problems()]


@pytest.mark.parametrize("layers", [["tiles"], "tiles", 3, True])
def test_a_non_dict_layers_warns_and_reads_as_all_shown(tmp_path, layers):
    doc, _ = _load(tmp_path, {**_layout(), "view": {"layers": layers}})
    assert all(v is True for v in view_settings(doc.view)["layers"].values())
    assert len(_warnings(doc, "layers")) == 1


def test_an_unknown_layer_name_with_a_bool_is_kept_without_a_warning(tmp_path):
    doc, _ = _load(tmp_path, {**_layout(), "view": {"layers": {"Racks": False}}})
    assert view_settings(doc.view)["layers"]["Racks"] is False
    assert not _warnings(doc, "layers")


def test_a_block_layer_round_trips_through_save_and_load(tmp_path):
    layout = _layout()
    block = _process(layout)["name"]
    _process(layout)["layer"] = "Racks"
    doc, _ = _load(tmp_path, layout)
    assert doc.blocks[block].layer == "Racks"
    assert not _warnings(doc, "layer")
    doc.save(str(tmp_path / "saved.json"))
    saved = json.loads((tmp_path / "saved.json").read_text(encoding="utf-8"))
    assert next(b for b in saved["blocks"] if b["name"] == block)["layer"] == "Racks"
    assert ModelDocument.load(str(tmp_path / "saved.json")).blocks[block].layer == "Racks"


def test_update_block_layer_is_one_undo_step_and_empty_clears_none_keeps(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]

    def stored():
        return T.call("get_model_json", model="m")["layout"]["blocks"][0]

    assert T.call("update_block", model="m", name=block, description="Press", layer="Racks")["ok"]
    assert stored()["layer"] == "Racks"
    assert T.call("undo", model="m")["ok"]                    # layer and description at once
    assert "layer" not in stored() and stored().get("description", "") != "Press"
    # Ruling (K2 T1): None keeps the layer, since an MCP client sends an omitted argument as its
    # default None; "" clears it.
    assert T.call("update_block", model="m", name=block, layer="Racks")["ok"]
    assert T.call("update_block", model="m", name=block, layer=None)["ok"]
    assert stored()["layer"] == "Racks"
    assert T.call("update_block", model="m", name=block, layer="")["ok"]
    assert stored().get("layer") in (None, "")
    (schema,) = [s for s in T.tool_schemas() if s["name"] == "update_block"]
    assert "layer" in schema["input_schema"]["properties"]


@pytest.mark.parametrize("bad", [5, True, ["Racks"], {"a": 1}, 1.5])
def test_a_non_string_block_layer_warns_and_never_blocks_a_run(tmp_path, bad):
    layout = _layout()
    good = _run(layout)
    _process(layout)["layer"] = bad
    doc, _ = _load(tmp_path, layout)
    assert len(_warnings(doc, "layer")) == 1, [(p.code, p.path) for p in doc.problems()]
    assert not _errors(doc)
    _same_results(_run(layout), good)


def test_queue_row_cell_is_three_positive_numbers_within_1000_m():
    for good in ([0.5, 0.5, 0.5], [1000, 1000, 1000], [1e-6, 2, 3], [1, 2, 3.5]):
        assert queuerow.edit_problem({"cell": good}) is None, good
        assert queuerow.problems(_with_cell(good)) == [], good
    for bad in ([1, 2], [1, 2, 3, 4], [1, "a", 3], [0, 1, 1], [1, 1, -0.1], [True, 1, 1], 3,
                "1,2,3", [1000.5, 1, 1], [1, 1, 1001], [float("inf"), 1, 1]):
        found = queuerow.problems(_with_cell(bad))
        assert [p.code for p in found] == ["W_VIEW_SETTING"], bad
        assert found[0].severity == "warning" and "cell" in found[0].path, bad
        # Editor-only: a bad cell never refuses the row or its slots (as count_at, final review M2).
        assert queuerow.override_problem({"slots": 2, "cell": bad}) is None, bad
        assert queuerow.slots_of({"slots": 2, "cell": bad})["slots"] == 2


@pytest.mark.parametrize("cell", [[1, 2, 3], [0, 1, 1], [1, 1, 1001], [1, 2], "x"])
def test_a_cell_never_changes_the_run_good_or_bad(cell):
    layout = _layout()
    _process(layout)["queue_row"] = {"slots": 2}
    _same_results(_run(_with_cell(cell)), _run(layout))


def test_the_update_block_tool_takes_a_cell_in_one_undo_step(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]
    assert T.call("update_block", model="m", name=block, queue_row={"slots": 2, "cell": [1, 0.5, 2]})["ok"]
    row = T.call("get_model_json", model="m")["layout"]["blocks"][0]["queue_row"]
    assert row == {"slots": 2, "cell": [1.0, 0.5, 2.0]}
    assert T.call("undo", model="m")["ok"]
    assert "queue_row" not in T.call("get_model_json", model="m")["layout"]["blocks"][0]
    out = T.call("update_block", model="m", name=block, queue_row={"cell": [0, 1, 1]})
    assert out["error"]["code"] == "E_QUEUE_ROW"              # the tool refuses what the file only warns about


def test_set_view_layers_round_trips(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    out = T.call("set_view", model="m", layers={"tiles": False})
    assert out["ok"] and out["view"]["layers"].get("tiles", True) is False
    out = T.call("set_view", model="m", layers={"Racks": False})
    assert out["view"]["layers"].get("tiles", True) is False       # merged, not replaced
    assert out["view"]["layers"]["Racks"] is False
    assert T.call("get_model_json", model="m")["layout"]["view"]["layers"]["tiles"] is False
    out = T.call("set_view", model="m", layers={"tiles": True})
    assert out["view"]["layers"].get("tiles", True) is True
    for bad in ({"tiles": "no"}, {"tiles": 1}):
        assert T.call("set_view", model="m", layers=bad)["error"]["code"] == "E_VIEW", bad
    # Ruling (K2 T1): a list is refused by the shared argument check first, as a list draw_guard is.
    assert T.call("set_view", model="m", layers=["tiles"])["error"]["code"] == "E_BAD_ARGUMENT"
    assert T.call("undo", model="m")["ok"]
    (schema,) = [s for s in T.tool_schemas() if s["name"] == "set_view"]
    assert "layers" in schema["input_schema"]["properties"]


def test_layers_survive_a_save_and_match_the_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim import modelschema

    layout = _layout()
    _process(layout)["layer"] = "Racks"
    _process(layout)["queue_row"] = {"slots": 2, "cell": [1, 1, 1]}
    doc, _ = _load(tmp_path, {**layout, "view": {"layers": {"tiles": False, "Racks": True}}})
    doc.save(str(tmp_path / "s.json"))
    saved = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert saved["view"]["layers"] == {"tiles": False, "Racks": True}
    validator = jsonschema.Draft202012Validator(modelschema.build_json_schema())
    assert not list(validator.iter_errors(saved))
    assert list(validator.iter_errors({**saved, "view": {"layers": {"tiles": "no"}}}))
    assert list(validator.iter_errors({**saved, "view": {"layers": [1]}}))


def test_a_model_with_every_k2_key_runs_to_the_same_results(tmp_path):
    layout = _layout()
    _process(layout)["layer"] = "Racks"
    _process(layout)["queue_row"] = {"slots": 2, "cell": [1, 1, 1]}
    layout["view"] = {"layers": {"tiles": False, "pads": False, "Racks": False}}
    doc, _ = _load(tmp_path, layout)
    assert not _errors(doc) and not _warnings(doc, "layer", "cell")
    assert view_settings(doc.view)["layers"]["Racks"] is False      # the keys were really read
    base = _layout()
    _process(base)["queue_row"] = {"slots": 2}
    _same_results(_run(layout), _run(base))


def test_review_focus_4_a_bad_layers_value_and_a_bad_block_layer_open_run_and_warn(tmp_path):
    from nagasim.api import run_model
    from nagasim.modelcheck import _validation_problems

    layout = _layout()
    _process(layout)["layer"] = 5
    layout["view"] = {"layers": {"tiles": "no"}}
    doc, path = _load(tmp_path, layout)
    assert len(_warnings(doc, "layers")) == 1
    assert len([p for p in _warnings(doc, "layer") if p.path.endswith("/layer")]) == 1
    assert view_settings(doc.view)["layers"].get("tiles", True) is True
    assert not _errors(doc)
    assert run_model(str(path), replications=1) is not None
    found = [p for p in _validation_problems(str(path)) if p.code == "W_VIEW_SETTING"]
    assert len(found) == 2 and all(p.severity == "warning" for p in found)


def _notes(doc):
    seen = []
    doc.subscribe(lambda kind, **info: seen.append((kind, info.get("action"))))
    return seen


def test_undo_and_redo_of_a_view_or_layer_edit_send_the_narrow_change(tmp_path):
    """K2 final review I1: an undo or redo that changes only `view` or block `layer` keys sends
    the same narrow change as the edit ("view", or "blocks"/"layer"), so the canvas keeps a
    loaded run; any other difference still sends "reset"."""
    doc, _ = _load(tmp_path, _layout())
    block = _process(_layout())["name"]
    seen = _notes(doc)
    doc.set_layers({block: "Racks"})
    doc.set_view_settings(layers={"items": False})
    doc.set_view_settings(queue_display="tall")
    del seen[:]
    doc.undo()
    doc.undo()
    doc.undo()
    assert seen == [("view", None), ("view", None), ("blocks", "layer")]
    assert doc.blocks[block].layer == "" and not doc.view
    del seen[:]
    doc.redo(3)
    assert sorted(seen) == [("blocks", "layer"), ("view", None)]
    assert doc.blocks[block].layer == "Racks" and view_settings(doc.view)["layers"]["items"] is False
    doc.set_description(block, "Press")
    del seen[:]
    doc.undo()
    assert seen == [("reset", None)]


def test_update_block_refuses_a_kind_name_as_a_user_layer(tmp_path):
    """K2 final review M1: the same rule as the Properties Layer field, case-insensitive."""
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]
    for name in ("tiles", "Items", " FLOOR "):
        answer = T.call("update_block", model="m", name=block, layer=name)
        assert answer["error"]["code"] == "E_BAD_ARGUMENT", answer
        assert answer["error"]["hint"]
    assert "layer" not in T.call("get_model_json", model="m")["layout"]["blocks"][0]
