"""LOGIC classes from a module the caller names (D914): `run_model(logic=...)` and
`naga run --logic MODULE`, never a model key."""
import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core.schema import ModelValidationError

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).resolve().parents[1]


def _model(logic_class="RouteBig"):
    return {
        "blocks": [
            {"name": "create_1", "type": "CREATE", "position": [0, 0],
             "params": {"distribution": "Exponential", "dist_params": {"rate": 1.0}}},
            {"name": "logic_1", "type": "LOGIC", "position": [1, 0],
             "params": {"logic_class": logic_class}},
            {"name": "out_a", "type": "DISPOSE", "position": [2, 0]},
            {"name": "out_b", "type": "DISPOSE", "position": [2, 1]},
        ],
        "links": [{"source": "create_1", "target": "logic_1"},
                  {"source": "logic_1", "target": "out_a"},
                  {"source": "logic_1", "target": "out_b"}],
        "simulation": {"time_limit": 50, "replications": 1},
    }


def _to(results, target):
    """Entities that took the LOGIC block's link to *target*, over every run."""
    flow = results["material_flow_summary"]
    return int(flow[(flow["From"] == "logic_1") & (flow["To"] == target)]["Entities"].sum())


@pytest.fixture
def on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(DATA))
    # Spawned pool workers read PYTHONPATH too (no -I anywhere): this tree first.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT), str(DATA)]))
    yield
    sys.modules.pop("my_logic", None)


def test_logic_from_caller_module(on_path):
    results = api.run_model(_model(), logic="my_logic")
    assert _to(results, "out_b") > 0 and _to(results, "out_a") == 0


def test_default_still_custom_logic():
    results = api.run_model(_model("SampleLogic"))
    assert _to(results, "out_a") > 0 and _to(results, "out_b") == 0


def test_caller_module_falls_back_to_default_class(on_path):
    """A class the caller's module lacks still comes from custom_logic."""
    results = api.run_model(_model("SampleLogic"), logic="my_logic")
    assert _to(results, "out_a") > 0


def test_module_object_accepted(on_path):
    results = api.run_model(_model(), logic=importlib.import_module("my_logic"), workers=1)
    assert _to(results, "out_b") > 0


def test_module_object_without_importable_name_refused_with_workers():
    module = types.ModuleType("made_up_here")
    module.RouteBig = type("RouteBig", (), {"execute": lambda self, entity: 1})
    with pytest.raises(ValueError, match="made_up_here.*workers=1"):
        api.run_model(_model(), logic=module, workers=2, replications=2)
    # One process needs no name: the object itself is used.
    assert _to(api.run_model(_model(), logic=module), "out_b") > 0


@pytest.mark.slow
def test_workers_import_by_name(on_path):
    model = _model()
    model["simulation"]["replications"] = 2
    results = api.run_model(model, logic="my_logic", workers=2)
    assert sorted(results["system"]["Run"].unique()) == [0, 1]
    assert _to(results, "out_b") > 0 and _to(results, "out_a") == 0


def test_missing_module_named(tmp_path, capsys):
    with pytest.raises(ImportError, match="no_such_mod"):
        api.run_model(_model(), logic="no_such_mod")
    path = tmp_path / "model.json"
    path.write_text(json.dumps(_model()), encoding="utf-8")
    assert api.main([str(path), "--logic", "no_such_mod"]) == 2
    assert "no_such_mod" in capsys.readouterr().out


def test_missing_class_named(on_path, tmp_path, capsys):
    from nagasim.core.logic_authoring import LogicClassNotFound
    with pytest.raises(LogicClassNotFound, match="'Nowhere'.*my_logic"):
        api.run_model(_model("Nowhere"), logic="my_logic")
    path = tmp_path / "model.json"
    path.write_text(json.dumps(_model("Nowhere")), encoding="utf-8")
    assert api.main([str(path), "--logic", "my_logic"]) == 2
    out = capsys.readouterr().out
    assert "Nowhere" in out and "my_logic" in out


def test_model_file_cannot_name_a_module(on_path):
    """No schema key names a module: on a block it is an unknown key; where the
    engine tolerates unknown keys (top level, `simulation`) it is ignored, so the
    class is still looked up in custom_logic only, even with my_logic importable."""
    from nagasim.core.logic_authoring import LogicClassNotFound
    model = _model()
    model["blocks"][1]["params"]["logic_module"] = "my_logic"
    with pytest.raises(ModelValidationError, match="logic_module"):
        api.run_model(model)
    model = _model()
    model["logic_module"] = "my_logic"
    model["simulation"]["logic_module"] = "my_logic"
    with pytest.raises(LogicClassNotFound, match="in nagasim.core.custom_logic$"):
        api.run_model(model)
