"""Item code in the model file: the definition's `code` record, the document API, and
flattening's hand-off to the engine (`item_code`, the `item` stamp on hook blocks)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from nagasim import modelschema
from nagasim.core import schema, submodels
from nagasim.core.document import ModelDocument

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
GATE_SRC = "class Gate:\n    def on_entry(self, entity, item):\n        return 0\n"


def block(name, type_name, **params):
    return {"name": name, "description": name, "type": type_name, "position": [0, 0], "params": params}


def gate_definition(code=None, hook="entry"):
    route = block("route", "LOGIC", **({"hook": hook} if hook else {}))
    out = {"info": {}, "parameters": [{"name": "limit", "default": 1}],
           "blocks": [block("in", "SUB_ENTRY", port="in"), route,
                      block("out", "SUB_EXIT", port="out")],
           "links": [{"source": "in", "target": "route"}, {"source": "route", "target": "out"}],
           "annotations": []}
    if code is not None:
        out["code"] = code
    return out


def gate_model(**definitions):
    blocks = [block("c1", "CREATE"), block("c2", "CREATE"),
              block("g1", "SUBMODEL", definition="Gate", arguments={"limit": 3}),
              block("g2", "SUBMODEL", definition="Gate", arguments={"limit": 5}),
              block("d1", "DISPOSE"), block("d2", "DISPOSE")]
    links = [{"source": "c1", "target": "g1"}, {"source": "g1", "target": "d1"},
             {"source": "c2", "target": "g2"}, {"source": "g2", "target": "d2"}]
    return {"schema_version": schema.CURRENT_VERSION, "blocks": blocks, "links": links,
            "resources": [], "variables": [], "annotations": [],
            "submodels": definitions or {"Gate": gate_definition({"class": "Gate", "source": GATE_SRC})},
            "simulation": {"time_limit": 20, "warmup": 0, "replications": 1}}


def test_code_round_trips_through_the_document():
    layout = gate_model()
    doc = ModelDocument(layout)
    assert doc.to_layout()["submodels"]["Gate"]["code"] == layout["submodels"]["Gate"]["code"]
    assert doc.definition_code("Gate") == {"class": "Gate", "source": GATE_SRC}
    doc.set_definition_code("Gate", None)
    assert "code" not in doc.to_layout()["submodels"]["Gate"]
    assert doc.definition_code("Gate") is None
    doc.undo()
    assert doc.to_layout()["submodels"]["Gate"]["code"] == {"class": "Gate", "source": GATE_SRC}
    with pytest.raises(ValueError):
        doc.set_definition_code("Nope", {"class": "Gate", "source": GATE_SRC})


def test_a_definition_without_code_saves_byte_identical():
    raw = json.loads((MODELS / "two_cell_line.json").read_text(encoding="utf-8"))
    once = ModelDocument(raw).to_layout()
    assert all("code" not in d for d in once["submodels"].values())
    assert json.dumps(ModelDocument(copy.deepcopy(once)).to_layout()) == json.dumps(once)


def test_flatten_stamps_the_hook_block_and_collects_code():
    flat = submodels.flatten(gate_model())
    routes = {b["name"]: b for b in flat["blocks"]}
    assert routes["g1/route"]["item"] == "g1"
    assert routes["g2/route"]["item"] == "g2"
    assert flat["item_code"]["g2"]["locals"]["limit"] == 5
    assert flat["item_code"]["g1"]["source"] == GATE_SRC
    assert flat["item_code"]["g1"]["class"] == "Gate"
    assert flat["item_code"]["g1"]["definition"] == "Gate"
    assert "fingerprint" not in flat["item_code"]["g1"]
    assert "submodels" not in flat


def test_a_definition_without_code_stamps_nothing():
    flat = submodels.flatten(gate_model(Gate=gate_definition()))
    assert "item_code" not in flat
    assert all("item" not in b for b in flat["blocks"])


def test_flatten_replaces_an_incoming_item_code():
    layout = gate_model()
    layout["item_code"] = {"x": {"definition": "Gate", "class": "Gate", "source": "class Gate: pass"}}
    layout["submodels"]["Gate"]["blocks"][1]["item"] = "x"      # a stamp in the file is not kept
    flat = submodels.flatten(layout)
    assert set(flat["item_code"]) == {"g1", "g2"}
    assert {b["name"]: b.get("item") for b in flat["blocks"]}["g1/route"] == "g1"


def test_extends_inherits_code_whole():
    own = {"class": "Gate", "source": GATE_SRC + "# own\n"}
    layout = gate_model(Gate=gate_definition({"class": "Gate", "source": GATE_SRC}),
                        Gate2={"extends": "Gate"}, Gate3={"extends": "Gate", "code": own})
    layout["blocks"][2]["params"]["definition"] = "Gate2"
    layout["blocks"][3]["params"]["definition"] = "Gate3"
    flat = submodels.flatten(layout)
    assert flat["item_code"]["g1"]["source"] == GATE_SRC
    assert flat["item_code"]["g1"]["definition"] == "Gate2"
    assert flat["item_code"]["g2"]["source"] == own["source"]


def test_schema_refuses_an_unknown_code_key():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(modelschema.build_json_schema())
    code = {"class": "Gate", "source": GATE_SRC, "timer": None, "watch": [], "signals": [],
            "placed": {"author": "Naga", "fingerprint": "sha256:0"}}
    good = gate_model(Gate=gate_definition(code, hook=None))
    assert not list(validator.iter_errors(good))
    bad = gate_model(Gate=gate_definition(code, hook=None))
    bad["submodels"]["Gate"]["code"]["run_on_open"] = True
    assert list(validator.iter_errors(bad))
