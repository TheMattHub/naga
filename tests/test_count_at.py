"""Plan K1 Task 4, engine side: a block's `queue_row.count_at` (D1507), the 3D count label's offset
`[dx, dz, h]` in metres, its validation, schema and undo, and `update_block(queue_row=...)` (D1508).
Engine only (no product import), so the naga export carries it; the drawing side is in
`test_count_labels.py`."""
import json
from pathlib import Path

import pytest

from nagasim.core import queuerow
from nagasim.core.document import ModelDocument

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def test_count_at_is_three_numbers_within_1000_m_with_a_height_of_0_or_more():
    assert queuerow.edit_problem({"count_at": [0.5, -1, 2]}) is None
    assert queuerow.edit_problem({"count_at": [-1000, 1000, 1000]}) is None
    assert queuerow.count_at_of({"direction": 90, "count_at": [0.5, -1, 2]}) == (0.5, -1.0, 2.0)
    assert queuerow.count_at_of({"direction": 90}) is None
    for bad in ([1, 2], [1, 2, 3, 4], [1, "a", 0], [1, 2, -0.1], [True, 0, 0], 3,
                [1e308, -1e308, 1e308], [1000.5, 0, 0], [0, 0, 1001]):      # M3: bounded
        assert queuerow.count_at_problem({"count_at": bad}), bad
        assert queuerow.edit_problem({"count_at": bad}), bad
        assert queuerow.count_at_of({"count_at": bad}) is None
        # Final review M2: editor-only, so a bad offset never refuses the row or its slots.
        assert queuerow.override_problem({"slots": 2, "count_at": bad}) is None, bad
        assert queuerow.slots_of({"slots": 2, "count_at": bad})["slots"] == 2
    # The row's slots still count with a label offset beside them (D1113).
    assert queuerow.slots_of({"slots": 2, "count_at": [0, 0, 0]})["slots"] == 2


def test_a_bad_count_at_is_a_warning_and_the_run_is_the_same():
    """Final review M2: a malformed `count_at` is W_VIEW_SETTING, not E_QUEUE_ROW, and the run
    (its slot counter included) is the one without it."""
    from nagasim import api

    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    block = next(b for b in layout["blocks"] if str(b.get("type")).upper() == "PROCESS")
    block["queue_row"] = {"slots": 2}
    good = api.run_model(json.loads(json.dumps(layout)), replications=1, seed=3, run_length=200)
    block["queue_row"] = {"slots": 2, "count_at": [0, 0, -1]}
    found = queuerow.problems(layout)
    assert [p.code for p in found] == ["W_VIEW_SETTING"] and found[0].severity == "warning"
    assert found[0].path.endswith("/queue_row/count_at")
    bad = api.run_model(json.loads(json.dumps(layout)), replications=1, seed=3, run_length=200)
    assert set(bad) == set(good)
    assert all(bad[key].equals(good[key]) if hasattr(good[key], "equals") else bad[key] == good[key]
               for key in good)


def test_set_queue_rows_stores_count_at_as_floats_in_one_undo_step():
    doc = ModelDocument()
    name = doc.add_block("PROCESS", (0, 0))
    doc.set_queue_rows({name: {"direction": 90, "count_at": [1, 0, 2]}})
    assert doc.blocks[name].queue_row == {"direction": 90.0, "count_at": [1.0, 0.0, 2.0]}
    doc.set_queue_rows({name: {"count_at": [0, 0, -1]}})            # refused: not stored
    assert doc.blocks[name].queue_row["count_at"] == [1.0, 0.0, 2.0]
    doc.undo()
    assert doc.blocks[name].queue_row is None


def test_count_at_survives_a_save_and_matches_the_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim import modelschema

    doc = ModelDocument.load(str(MODELS / "single_server.json"))
    name = next(iter(doc.blocks))
    doc.set_queue_rows({name: {"count_at": [0.25, 0.5, 0.0]}})
    doc.save(str(tmp_path / "m.json"))
    saved = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    block = next(b for b in saved["blocks"] if b["name"] == name)
    assert block["queue_row"] == {"count_at": [0.25, 0.5, 0.0]}
    assert ModelDocument.load(str(tmp_path / "m.json")).blocks[name].queue_row == block["queue_row"]
    validator = jsonschema.Draft202012Validator(modelschema.build_json_schema())
    assert not list(validator.iter_errors(saved))
    for bad in ([1, 2], ["a", 0, 0], [0, 0, 0, 0]):
        block["queue_row"] = {"count_at": bad}
        assert list(validator.iter_errors(saved)), bad
    # Final review M3: the schema holds the same bounds as the model check.
    for bad in ([0, 0, -1], [1001, 0, 0], [0, -1001, 0]):
        block["queue_row"] = {"count_at": bad}
        assert list(validator.iter_errors(saved)), bad
        assert [p.code for p in queuerow.problems(saved)] == ["W_VIEW_SETTING"], bad


def test_update_block_merges_the_queue_row_in_one_undo_step(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]
    assert T.call("update_block", model="m", name=block, queue_row={"direction": 90, "slots": 2})["ok"]
    out = T.call("update_block", model="m", name=block, description="Press",
                 queue_row={"count_at": [0, 0.5, 1.5]})
    assert out["ok"]
    row = T.call("get_model_json", model="m")["layout"]["blocks"][0]["queue_row"]
    assert row == {"direction": 90.0, "slots": 2, "count_at": [0.0, 0.5, 1.5]}     # merged, kept
    assert T.call("undo", model="m")["ok"]                  # the label and the description at once
    layout = T.call("get_model_json", model="m")["layout"]
    assert "count_at" not in layout["blocks"][0]["queue_row"]
    assert layout["blocks"][0].get("description", "") != "Press"
    # A null drops a key; dropping the last one leaves no queue_row.
    assert T.call("update_block", model="m", name=block, queue_row={"direction": None, "slots": None})["ok"]
    assert "queue_row" not in T.call("get_model_json", model="m")["layout"]["blocks"][0]


def test_update_block_refuses_a_bad_queue_row_and_writes_nothing(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]
    for bad in ({"count_at": [0, 0]}, {"count_at": [0, 0, -1]}, {"count_at": [1e308, 0, 0]}, {"colour": 1}):
        out = T.call("update_block", model="m", name=block, description="Press", queue_row=bad)
        assert out["error"]["code"] == "E_QUEUE_ROW", bad
    layout = T.call("get_model_json", model="m")["layout"]["blocks"][0]
    assert "queue_row" not in layout and layout.get("description", "") != "Press"
    (schema,) = [s for s in T.tool_schemas() if s["name"] == "update_block"]
    assert "queue_row" in schema["input_schema"]["properties"]


def test_apply_edits_carries_the_queue_row(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    block = T.call("add_block", model="m", type="PROCESS")["block"]
    out = T.call("apply_edits", model="m",
                 edits=[{"tool": "update_block", "args": {"name": block, "queue_row": {"count_at": [1, 0, 0]}}}])
    assert out["ok"], out
    assert T.call("get_model_json", model="m")["layout"]["blocks"][0]["queue_row"] == {"count_at": [1.0, 0.0, 0.0]}
