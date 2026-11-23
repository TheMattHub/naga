"""The optional block key `asset_3d` ("library/asset"): display only. The engine accepts it, keeps it
through load, save and `naga fmt`, refuses a malformed value, and never reads it in a run."""
import copy
import json

import pytest

from nagasim import api, modelfmt
from nagasim.core import schema
from nagasim.core.document import ModelDocument
from nagasim.core.trace import TraceRecorder

from conftest import chain, create, dispose, layout, process
from test_motion import _valid as motion_model

ASSET = "automation/irb6700"


def _with_asset(doc: dict, index: int = 1, value=ASSET) -> dict:
    doc = copy.deepcopy(doc)
    doc["blocks"][index]["asset_3d"] = value
    return doc


def _plain():
    blocks = [create(max_arrivals=5), process(), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}])


def _codes(doc):
    return [(p.code, p.path) for p in schema.validate_model_structured(doc)
            if p.severity == "error" or p.code == "W_UNKNOWN_KEY"]


def test_motion_block_round_trips_through_the_document():
    doc = _with_asset(motion_model())
    model = ModelDocument(doc)
    assert model.blocks["cell"].asset_3d == ASSET
    assert model.blocks["create_1"].asset_3d == ""
    saved = model.to_layout()
    assert saved["blocks"][1]["asset_3d"] == ASSET
    assert "asset_3d" not in saved["blocks"][0]       # saved only when set
    assert not (model.blocks["cell"].extra or {})     # a real field, not an unknown key


@pytest.mark.parametrize("bad", [7, ["a/b"], None])
def test_a_non_string_value_is_not_turned_into_text(bad):
    # check refuses it (E_FIELD_TYPE); "7" would be a different, well-formed-looking value
    assert ModelDocument(_with_asset(motion_model(), value=bad)).blocks["cell"].asset_3d == ""


def test_fmt_keeps_it_and_orders_it_after_layout_rotation():
    doc = _with_asset(motion_model())
    doc["blocks"][1]["layout_rotation"] = 90.0
    text = modelfmt.dumps(modelfmt.minimal(doc))
    assert modelfmt.dumps(modelfmt.minimal(json.loads(text))) == text
    block = json.loads(text)["blocks"][1]
    assert block["asset_3d"] == ASSET
    keys = list(block)
    assert keys.index("asset_3d") == keys.index("layout_rotation") + 1


def test_a_well_formed_value_raises_no_problem():
    assert _codes(_with_asset(_plain())) == []


@pytest.mark.parametrize("bad", ["../x", "Automation/IRB", "automation", "a/b/c", "", 7])
def test_a_malformed_value_is_refused_at_its_block(bad):
    found = _codes(_with_asset(_plain(), value=bad))
    assert found and all(path == "/blocks/1/asset_3d" for _, path in found), found
    # a string of the wrong form is E_ASSET_3D, a value of another JSON type is E_FIELD_TYPE
    assert {code for code, _ in found} == {"E_FIELD_TYPE" if bad == 7 else "E_ASSET_3D"}


def test_a_run_never_reads_it():
    def run(doc):
        rec = TraceRecorder()
        result = api.run_model(doc, replications=1, run_length=50, seed=3, trace=rec)
        return result, [repr(e) for e in rec.events]
    (a, events_a), (b, events_b) = run(_with_asset(_plain())), run(_plain())
    assert events_a == events_b and events_a, "the run recorded no events"
    assert list(a) == list(b)
    assert all(a[name].equals(b[name]) for name in a)


def test_set_asset_3d_is_one_undo_step_and_refuses_a_malformed_name():
    model = ModelDocument(_plain())
    seen = []
    model.subscribe(lambda kind, **info: seen.append((kind, info.get("action"))))
    model.set_asset_3d({"process_1": ASSET, "create_1": "Not/A Name"})
    assert model.blocks["process_1"].asset_3d == ASSET
    assert model.blocks["create_1"].asset_3d == ""          # E_ASSET_3D's rule: not stored
    assert ("blocks", "asset_3d") in seen and model.dirty
    model.set_asset_3d({"process_1": ""})                   # cleared: gone from the saved file
    assert model.blocks["process_1"].asset_3d == ""
    assert "asset_3d" not in model.to_layout()["blocks"][1]
    model.undo()
    assert model.blocks["process_1"].asset_3d == ASSET
    assert model.to_layout()["blocks"][1]["asset_3d"] == ASSET
