"""set_model_info (G1) and save_model(short=True) (G2) of the agent-tool-gaps plan."""
import json

import pytest

from nagasim import agent_tools as T, modelfmt
from nagasim.core.document import ModelDocument


pytestmark = pytest.mark.usefixtures("answers_match_schemas")


@pytest.fixture
def ws(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("add_block", model="m", type="CREATE", name="src")
    T.call("add_block", model="m", type="DISPOSE", name="out")
    T.call("connect", model="m", source="src", target="out")
    return tmp_path


def doc():
    return T.core._WORKSPACE.document("m")


def info(**kw):
    out = T.call("set_model_info", model="m", **kw)
    assert "error" not in out, out
    return doc().info


def test_set_change_and_remove_keys(ws):
    assert info(title="T", category="Teaching", description="d", author="a", notes="n") == {
        "title": "T", "category": "Teaching", "description": "d", "author": "a", "notes": "n"}
    # None leaves a key as it is, a value changes it, "" removes it.
    assert info(title="T2", author="") == {
        "title": "T2", "category": "Teaching", "description": "d", "notes": "n"}


def test_other_info_keys_are_kept(ws):
    doc().set_info(requires_plugins=["x"], labels="all")
    assert info(title="T") == {"requires_plugins": ["x"], "labels": "all", "title": "T"}
    assert info(title="") == {"requires_plugins": ["x"], "labels": "all"}


def test_one_undo_entry_and_none_for_a_no_op(ws):
    d = doc()
    depth = len(d._undo_stack)
    info(title="T", notes="n")
    assert len(d._undo_stack) == depth + 1
    info(title="T", notes="n")              # same values
    info(author="")                         # removes a key that is not there
    T.call("set_model_info", model="m")     # nothing given
    assert len(d._undo_stack) == depth + 1
    d.undo()
    assert d.info == {}


def test_refusals(ws):
    for bad in ("x" * 2001, 5, ["a"]):
        out = T.call("set_model_info", model="m", title=bad)
        assert "error" in out
        assert out["error"]["code"] in ("E_INFO", "E_BAD_ARGUMENT"), out
    assert doc().info == {}
    assert "error" not in T.call("set_model_info", model="m", title="x" * 2000)
    out = T.call("set_model_info", model="nope", title="x")
    assert out["error"]["code"] == "E_UNKNOWN_MODEL"


def test_too_long_text_gets_the_info_code(ws):
    out = T.call("set_model_info", model="m", title="x" * 2001)
    assert out["error"]["code"] == "E_INFO"


def test_set_info_keeps_its_old_callers_behaviour():
    d = ModelDocument()
    d.set_info(description="a", author="b")
    d.set_info(notes="n")
    assert d.info == {"description": "a", "author": "b", "notes": "n"}


# -- short save ---------------------------------------------------------------------------

def test_short_save_is_the_naga_fmt_bytes(ws):
    long_path, short_path = ws / "long.json", ws / "short.json"
    info(title="T", description="d")
    assert "error" not in T.call("save_model", model="m", path="long.json")
    assert "error" not in T.call("save_model", model="m", path="short.json", short=True)
    modelfmt.format_file(long_path)
    assert short_path.read_bytes() == long_path.read_bytes()
    assert b"\r" not in short_path.read_bytes()


def test_short_file_reopens_as_the_same_model(ws):
    T.call("save_model", model="m", path="long.json")
    T.call("save_model", model="m", path="short.json", short=True)
    T.call("open_model", path="long.json", name="a")
    T.call("open_model", path="short.json", name="b")
    la, lb = (T.core._WORKSPACE.document(n).to_layout() for n in "ab")
    assert la == lb
    assert len((ws / "short.json").read_text()) < len((ws / "long.json").read_text())


def test_default_save_stays_the_long_form(ws):
    T.call("save_model", model="m", path="a.json")
    T.call("save_model", model="m", path="b.json", short=False)
    text = (ws / "a.json").read_text(encoding="utf-8")
    assert text == (ws / "b.json").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2)
    assert "written_by" in json.loads(text)["info"]      # the stamp is written, short form too
    T.call("save_model", model="m", path="c.json", short=True)
    assert "written_by" in json.loads((ws / "c.json").read_text())["info"]
