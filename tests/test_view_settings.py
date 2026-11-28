"""Plan K1 Task 3, engine side: the model's 3D display settings in `view` (queue display D1504,
draw guard D1506), their defaults, schema and undo, and the `set_view` agent tool (D1508).
Engine only (no product import), so the naga export carries it; the drawing side is in
`test_queue_display.py`."""
import json
from pathlib import Path

import pytest

from nagasim.core.document import ModelDocument, view_settings

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _guard(on=True, limit=20000, limit_2d=50000) -> dict:
    """A whole draw guard; plan M (D1526) added the 2D limit."""
    return {"on": on, "limit": limit, "limit_2d": limit_2d}


def test_an_old_model_opens_compact_with_the_guard_on_at_20000():
    doc = ModelDocument.load(str(MODELS / "single_server.json"))
    assert "queue_display" not in (doc.view or {}) and "draw_guard" not in (doc.view or {})
    assert view_settings(doc.view) == {"queue_display": "compact",
                                       "draw_guard": _guard(), "layers": {}}
    # A partial guard keeps the other keys at their defaults.
    assert view_settings({"draw_guard": {"on": False}})["draw_guard"] == _guard(on=False)


def test_set_view_settings_is_one_undo_step_and_refuses_bad_values():
    doc = ModelDocument()
    doc.set_view_settings(queue_display="tall", draw_guard={"limit": 500})
    assert doc.view == {"queue_display": "tall", "draw_guard": _guard(limit=500)}
    assert doc.dirty
    doc.undo()
    assert view_settings(doc.view)["queue_display"] == "compact"
    for bad in ({"queue_display": "huge"}, {"draw_guard": {"limit": 0}},
                {"draw_guard": {"limit": True}}, {"draw_guard": {"on": 1}},
                {"draw_guard": {"size": 5}}, {"draw_guard": [1]},
                {"draw_guard": {"limit_2d": "9"}}, {"draw_guard": {"limit_2d": -1}},
                {"draw_guard": {"limit_2d": 2_000_000_001}}):
        with pytest.raises(ValueError):
            doc.set_view_settings(**bad)
    assert not doc.can_undo


def test_the_settings_survive_a_save_and_match_the_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim import modelschema

    doc = ModelDocument.load(str(MODELS / "single_server.json"))
    doc.set_view_settings(queue_display="tall", draw_guard={"on": False, "limit": 7, "limit_2d": 9})
    doc.save(str(tmp_path / "m.json"))
    saved = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert saved["view"] == {"queue_display": "tall", "draw_guard": _guard(False, 7, 9)}
    assert ModelDocument.load(str(tmp_path / "m.json")).view == saved["view"]
    validator = jsonschema.Draft202012Validator(modelschema.build_json_schema())
    assert not list(validator.iter_errors(saved))
    for bad in ({"queue_display": "huge"}, {"draw_guard": {"limit": 0}},
                {"draw_guard": {"on": "yes"}}, {"draw_guard": {"colour": 1}},
                {"draw_guard": {"limit_2d": 0}}):
        assert list(validator.iter_errors({**saved, "view": bad})), bad


def test_set_view_round_trips_and_refuses_bad_values(tmp_path):
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    out = T.call("set_view", model="m", queue_display="tall", draw_guard={"limit": 50_000})
    assert out["ok"] and out["view"] == {"queue_display": "tall",
                                         "draw_guard": _guard(limit=50_000), "layers": {}}
    out = T.call("set_view", model="m", draw_guard={"on": False, "limit_2d": 100})
    assert out["view"]["draw_guard"] == _guard(False, 50_000, 100)
    # Read back from the file the tool keeps, not from the edit summary (view is canvas-only).
    assert T.call("get_model_json", model="m")["layout"]["view"]["queue_display"] == "tall"
    for bad in ({"queue_display": "huge"}, {"draw_guard": {"limit": -1}},
                {"draw_guard": {"limit": False}}, {"draw_guard": {"colour": 1}},
                {"draw_guard": {"limit_2d": "50000"}}):
        assert T.call("set_view", model="m", **bad)["error"]["code"] == "E_VIEW", bad
    assert T.call("undo", model="m")["ok"]
    assert T.call("set_view", model="m")["view"]["draw_guard"]["on"] is True
    (schema,) = [s for s in T.tool_schemas() if s["name"] == "set_view"]
    assert set(schema["input_schema"]["properties"]) == {"model", "queue_display", "draw_guard",
                                                         "layers"}


@pytest.mark.parametrize("view, shown", [
    ({"draw_guard": True}, {"queue_display": "compact", "draw_guard": _guard()}),
    ({"draw_guard": {"limit": "20000"}}, {"queue_display": "compact", "draw_guard": _guard()}),
    ({"draw_guard": {"on": False, "limit": None}}, {"queue_display": "compact",
                                                     "draw_guard": _guard(on=False)}),
    ({"draw_guard": {"limit": 10**12}}, {"queue_display": "compact", "draw_guard": _guard()}),
    ({"queue_display": "huge", "draw_guard": {"limit": 7}}, {"queue_display": "compact",
                                                             "draw_guard": _guard(limit=7)}),
    ({"queue_display": ["tall"]}, {"queue_display": "compact", "draw_guard": _guard()}),
    # Plan M Review Focus 5: a bad limit_2d (text, negative, past the maximum) warns only.
    ({"draw_guard": {"limit_2d": "many"}}, {"queue_display": "compact", "draw_guard": _guard()}),
    ({"draw_guard": {"limit_2d": -5}}, {"queue_display": "compact", "draw_guard": _guard()}),
    ({"draw_guard": {"limit_2d": 3 * 10**9}}, {"queue_display": "compact", "draw_guard": _guard()}),
])
def test_a_bad_view_value_opens_at_its_default_with_one_warning(tmp_path, view, shown):
    """Checkpoint I3: `view_settings` never raises; a value it replaces is one W_VIEW_SETTING
    warning, never an error (re-review N2), and the file opens."""
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    path = tmp_path / "m.json"
    path.write_text(json.dumps({**layout, "view": view}), encoding="utf-8")
    doc = ModelDocument.load(str(path))
    assert view_settings(doc.view) == {**shown, "layers": {}}      # K2: no layers, every one shown
    codes = [p.code for p in doc.problems() if p.path.startswith("/view")]
    assert codes == ["W_VIEW_SETTING"], codes
    # Setting the guard again over the bad value keeps the other key and writes a good one.
    doc.set_view_settings(draw_guard={"on": True})
    assert not [p for p in doc.problems() if p.path.startswith("/view/draw_guard")]


def test_an_empty_guard_takes_no_undo_step_and_the_limit_has_a_maximum():
    doc = ModelDocument()
    doc.set_view_settings(draw_guard={})
    assert not doc.can_undo and not doc.view
    with pytest.raises(ValueError):
        doc.set_view_settings(draw_guard={"limit": 2_000_000_001})
    doc.set_view_settings(draw_guard={"limit": 2_000_000_000})
    assert view_settings(doc.view)["draw_guard"]["limit"] == 2_000_000_000


def test_a_model_with_a_bad_view_value_still_runs(tmp_path):
    """Re-review N2: `view` is editor-only, so a bad value there never blocks a run."""
    from nagasim.api import run_model

    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    path = tmp_path / "m.json"
    bad = {"draw_guard": {"limit": "x", "limit_2d": -1}}          # plan M: limit_2d too
    path.write_text(json.dumps({**layout, "view": bad}), encoding="utf-8")
    assert run_model(str(path), replications=1) is not None
    doc = ModelDocument.load(str(path))
    assert [p.severity for p in doc.problems() if p.path.startswith("/view")] == ["warning"] * 2


def test_every_validator_says_a_bad_view_value_once(tmp_path):
    """Final review M1: the `validate_model` tool and `naga validate` say W_VIEW_SETTING too,
    once per bad value, as `write_model` and the document do; a whole float limit is taken."""
    from nagasim import agent_tools as T
    from nagasim.modelcheck import _validation_problems

    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    view = {"draw_guard": {"limit": "50000"}, "queue_display": "huge"}
    path = tmp_path / "m.json"
    path.write_text(json.dumps({**layout, "view": view}), encoding="utf-8")
    assert [p.code for p in _validation_problems(str(path)) if p.path.startswith("/view")] == \
        ["W_VIEW_SETTING"] * 2
    workspace = T.Workspace(str(tmp_path))
    workspace.models["m"] = ModelDocument.load(str(path))
    T.use_workspace(workspace)
    out = T.call("validate_model", model="m")
    assert [p["code"] for p in out["problems"] if p["path"].startswith("/view")] == ["W_VIEW_SETTING"] * 2
    assert out["ok"]
    doc = ModelDocument.load(str(path))
    assert [p.code for p in doc.problems() if p.path.startswith("/view")] == ["W_VIEW_SETTING"] * 2
    # The schema's integer takes 20000.0, so the reader does too (final review M3).
    assert view_settings({"draw_guard": {"limit": 20000.0}})["draw_guard"]["limit"] == 20000


@pytest.mark.parametrize("view, allowed, applied, waits", [
    # D1519: a stricter model guard applies at once; a looser one waits for the user.
    ({"draw_guard": {"limit": 500}}, None, _guard(limit=500), False),
    ({"draw_guard": {"on": False}}, None, _guard(), True),
    ({"draw_guard": {"limit": 2_000_000_000}}, None, _guard(), True),
    ({"draw_guard": {"on": False}}, _guard(on=False), _guard(on=False), False),
    ({"draw_guard": {"limit": 90_000}}, _guard(limit=50_000), _guard(limit=50_000), True),
    ({"draw_guard": {"limit": 50_000}}, _guard(limit=50_000), _guard(limit=50_000), False),
    # The user's own stricter setting never stops the file from using the default's room.
    ({"draw_guard": {"limit": 15_000}}, _guard(limit=500), _guard(limit=15_000), False),
    # Plan M (D1527, Review Focus 2): limit_2d follows the same rule, each limit on its own, so a
    # file that raises one keeps the other it tightens; a confirmation without limit_2d (an
    # older caller) leaves the 2D default as its ceiling.
    ({"draw_guard": {"limit_2d": 10}}, None, _guard(limit_2d=10), False),
    ({"draw_guard": {"limit_2d": 10**9}}, None, _guard(), True),
    ({"draw_guard": {"limit_2d": 10**9}}, _guard(limit_2d=10**9), _guard(limit_2d=10**9), False),
    ({"draw_guard": {"limit": 10, "limit_2d": 10**9}}, None, _guard(limit=10), True),
    ({"draw_guard": {"limit": 10**6, "limit_2d": 10}}, None, _guard(limit_2d=10), True),
    ({"draw_guard": {"limit_2d": 10**9}}, {"on": True, "limit": 20000}, _guard(), True),
])
def test_a_model_file_tightens_the_draw_guard_but_only_the_user_loosens_it(view, allowed, applied,
                                                                           waits):
    from nagasim.core.document import applied_draw_guard

    assert applied_draw_guard(view, allowed) == (applied, waits)


def test_set_view_alone_does_not_loosen_the_applied_guard(tmp_path):
    """D1519: the tool writes the model, but the guard it loosens waits for the user."""
    from nagasim import agent_tools as T
    from nagasim.core.document import applied_draw_guard

    T.use_workspace(T.Workspace(str(tmp_path)))
    assert T.call("new_model", name="m")["ok"]
    assert T.call("set_view", model="m", draw_guard={"on": False})["ok"]
    view = T.call("get_model_json", model="m")["layout"]["view"]
    assert view["draw_guard"]["on"] is False
    assert applied_draw_guard(view) == (_guard(), True)
    assert T.call("set_view", model="m", draw_guard={"on": True, "limit_2d": 10**9})["ok"]
    view = T.call("get_model_json", model="m")["layout"]["view"]
    assert applied_draw_guard(view) == (_guard(), True)
    assert T.call("set_view", model="m", draw_guard={"limit_2d": 10})["ok"]
    view = T.call("get_model_json", model="m")["layout"]["view"]
    assert applied_draw_guard(view) == (_guard(limit_2d=10), False)       # tightening is at once
    (schema,) = [s for s in T.tool_schemas() if s["name"] == "set_view"]
    assert "Allow in the app" in schema["input_schema"]["properties"]["draw_guard"]["description"]
