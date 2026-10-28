"""Validation names the bad key and suggests the right one (D910-D913): did-you-mean
hints, unknown ASSIGN entry keys, literal DISC/TRIA checks and the structured
problems a ModelValidationError carries to the CLI."""
import copy
import json
import pickle
from pathlib import Path

import pytest

from nagasim import cli
from nagasim.api import run_model
from nagasim.core import suggest
from nagasim.core.expressions import literal_problems
from nagasim.core.schema import (ModelValidationError, fatal_problems, prepare,
                                 validate_model_structured)

ROOT = Path(__file__).resolve().parent.parent
BASE = json.loads((ROOT / "nagasim" / "models" / "single_server.json").read_text(encoding="utf-8"))


def _layout(process_params=None, assign=None):
    """single_server, with the PROCESS params patched and an ASSIGN in front of it."""
    layout = copy.deepcopy(BASE)
    process = next(b for b in layout["blocks"] if b["type"] == "PROCESS")
    process["params"].update(process_params or {})
    if assign is not None:
        layout["blocks"].insert(1, {"name": "assign_1", "type": "ASSIGN",
                                    "params": {"assignments": assign}})
        layout["links"] = [{"source": "create_1", "target": "assign_1"},
                           {"source": "assign_1", "target": "process_1"},
                           {"source": "process_1", "target": "dispose_1"}]
    return layout


def _found(layout, code):
    return [p for p in validate_model_structured(layout) if p.code == code]


def test_close_and_did_you_mean():
    assert suggest.close("resorce", ["resource", "queue"]) == ["resource"]
    assert suggest.did_you_mean("maen", ["mean", "stddev"]) == "did you mean 'mean'?"
    assert suggest.did_you_mean("zzz", ["mean", "stddev"]) == ""


def test_unknown_param_suggests():
    layout = _layout()
    layout["blocks"][1]["params"]["resorce_name"] = "Server"
    [p] = _found(layout, "E_PARAM_UNKNOWN")
    assert p.hint.startswith("did you mean 'resource_name'? PROCESS takes: ")
    assert "resource_name" in p.hint.split("takes: ", 1)[1]


def test_no_close_match_keeps_hint():
    layout = _layout()
    layout["blocks"][1]["params"]["zzz"] = 1
    [p] = _found(layout, "E_PARAM_UNKNOWN")
    assert p.hint.startswith("PROCESS takes: ") and "did you mean" not in p.hint


def test_dist_param_suggests():
    layout = _layout({"distribution": "Normal", "dist_params": {"maen": 3, "stddev": 1}})
    [p] = _found(layout, "E_DIST_PARAM_UNKNOWN")
    assert p.hint == "did you mean 'mean'? Normal takes: mean, stddev"


def test_dist_param_rate_note_kept():
    [p] = _found(_layout({"dist_params": {"mean": 0.8}}), "E_DIST_PARAM_UNKNOWN")
    assert "did you mean" not in p.hint and p.hint.endswith("a rate is 1 / the mean")


def test_assign_entry_wrong_keys():
    layout = _layout(assign=[{"attr": "x", "value": "1"}])
    [p] = _found(layout, "E_ASSIGN_KEY")
    assert p.path == "/blocks/1/params/assignments/0/attr"
    assert "'attr'" in p.message and "'value'" in p.message
    assert p.hint.endswith("an ASSIGN entry takes: target, expr")
    assert not _found(layout, "E_ASSIGN_TARGET") and not _found(layout, "E_EXPRESSION")


def test_assign_misspelt_key_suggests_and_checks_the_rest():
    layout = _layout(assign=[{"target": "entity.x", "exprr": "1"}])
    [p] = _found(layout, "E_ASSIGN_KEY")
    assert p.hint.startswith("did you mean 'expr'? ")
    assert not _found(layout, "E_ASSIGN_TARGET") and not _found(layout, "E_EXPRESSION")
    bad_target = _layout(assign=[{"target": "1x", "note": "", "expr": "1"}])
    assert _found(bad_target, "E_ASSIGN_KEY") and _found(bad_target, "E_ASSIGN_TARGET")


def test_good_assign_is_quiet():
    assert not fatal_problems(validate_model_structured(
        _layout(assign=[{"target": "entity.x", "expr": "DISC(1, 0.5, 2, 0.5)"}])))


@pytest.mark.parametrize("text, expected", [
    # DISC(v1, p1, v2, p2, ...) as `_disc` reads it: probabilities in the odd places.
    ("DISC(1, 0.2, 2, 1.0)", "DISC probabilities sum to 1.2, must be 1"),
    ("DISC(1, 0.5, 2, 0.5)", None),
    ("DISC(1, 0.5, x, 0.5)", None),                 # a computed argument: left to run time
    ("DISC(1, 1.5, 2, -0.5)", "DISC probabilities must be >= 0"),
    ("DISC(1, 0.5, 2)", "DISC takes value/probability pairs: DISC(v1, p1, v2, p2, ...)"),
    ("DISC(-1, 1)", None),                          # unary minus is a literal
    ("TRIA(5, 2, 9)", "TRIA(5, 2, 9) needs low <= mode <= high"),
    ("TRIA(1, 2, 9)", None),
    ("TRIA(-3, -2.5, 0)", None),
    ("TRIA(1, x, 9)", None),
    ("2 * TRIA(9, 2, 1) + 1", "TRIA(9, 2, 1) needs low <= mode <= high"),
    ("TRIA(", None),                                # a syntax error is reported elsewhere
])
def test_literal_problems(text, expected):
    assert literal_problems(text) == ([expected] if expected else [])


def test_literal_problems_are_expression_errors():
    layout = _layout(assign=[{"target": "entity.x", "expr": "TRIA(5, 2, 9)"}])
    [p] = _found(layout, "E_EXPRESSION")
    assert p.path == "/blocks/1/params/assignments/0/expr"
    assert "low <= mode <= high" in p.message
    disc = _layout(assign=[{"target": "entity.x", "expr": "DISC(1, 0.2, 2, 1.0)"}])
    assert "sum to 1.2" in _found(disc, "E_EXPRESSION")[0].message


def test_error_carries_structure():
    with pytest.raises(ModelValidationError) as info:
        run_model(_layout(assign=[{"attr": "x", "value": "1"}]), replications=1, seed=0,
                  run_length=10)
    err = info.value
    assert err.structured[0].code == "E_ASSIGN_KEY"
    assert err.problems == [p.message for p in err.structured]
    back = pickle.loads(pickle.dumps(err))
    assert back.structured == err.structured and back.problems == err.problems
    assert str(back) == str(err)


def test_plain_error_has_no_structure():
    err = pickle.loads(pickle.dumps(ModelValidationError(["one", "two"])))
    assert err.problems == ["one", "two"] and err.structured == []


def test_cli_prints_codes_and_hints(tmp_path, capsys):
    model = tmp_path / "bad.json"
    model.write_text(json.dumps(_layout(assign=[{"attr": "x", "value": "1"}])), encoding="utf-8")
    assert cli.main(["run", str(model), "--replications", "1", "--run-length", "10"]) == 2
    err = capsys.readouterr().err
    assert err.count("E_ASSIGN_KEY /blocks/1/params/assignments/0/attr: ") == 1
    assert "    hint: an ASSIGN entry takes: target, expr" in err
    assert err.count("ERROR ") == 1


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_bundled_models_still_valid():
    """No shipped model fails, and no shipped expression (models and libraries) trips
    the literal checks."""
    for path in sorted((ROOT / "nagasim" / "models").glob("*.json")):
        _, problems = prepare(path)      # as a load reads it: libraries resolved
        assert not fatal_problems(problems), path.name
    files = [*(ROOT / "nagasim" / "models").glob("*.json"),
             *(ROOT / "product" / "assetlibs").glob("*.nagalib.json")]
    assert len(files) > 60
    for path in files:
        for text in _strings(json.loads(path.read_text(encoding="utf-8"))):
            if "DISC(" in text or "TRIA(" in text:
                assert not literal_problems(text), (path.name, text)
