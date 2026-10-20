"""The optional tool picker (D671): off = find_tool unchanged; on = re-ranked top ten plus the
three-tries guidance; turned on only by the person starting the server; a ranking error falls back."""
import argparse
import importlib.util
import json

import pytest

from nagasim import agent_tools, toolpicker
from nagasim.agent_tools import discover

QUERY = "validate the study against a formula"


@pytest.fixture
def fake_picker(monkeypatch):
    """A picker that reverses find_tool's order, built through the real `guarded` wrapper."""
    seen = []

    def score(query, docs):
        seen.append(docs)
        return [float(i) for i in range(len(docs))]       # the last candidate scores best
    monkeypatch.setattr(discover, "picker", toolpicker.guarded(score, lambda n: f"doc of {n}"))
    return seen


def test_picker_off_leaves_the_answer_byte_identical():
    assert discover.picker is None
    from nagasim import toolsearch
    expected = {"tools": toolsearch.search(discover.catalogue_index(), QUERY, limit=5)}
    assert json.dumps(agent_tools.call("find_tool", query=QUERY)) == json.dumps(expected)


def test_a_picker_reorders_the_top_ten_and_the_answer_says_try_three(fake_picker):
    from nagasim import toolsearch
    plain = [h["name"] for h in toolsearch.search(discover.catalogue_index(), QUERY, limit=10)]
    out = agent_tools.call("find_tool", query=QUERY, limit=3)
    assert [h["name"] for h in out["tools"]] == plain[::-1][:3]
    assert out["guidance"] == toolpicker.GUIDANCE and "first 3" in out["guidance"]
    assert fake_picker[0] == [f"doc of {n}" for n in plain]


def test_a_ranking_error_keeps_find_tools_order(monkeypatch):
    from nagasim import toolsearch

    def broken(query, docs):
        raise RuntimeError("GPU gone")
    monkeypatch.setattr(discover, "picker", toolpicker.guarded(broken, str))
    plain = toolsearch.search(discover.catalogue_index(), QUERY, limit=5)
    assert agent_tools.call("find_tool", query=QUERY)["tools"] == plain


def test_the_document_is_the_text_find_tool_indexes():
    spec = agent_tools.TOOLS["study_validate"]
    doc = toolpicker.document({"name": spec.name, "description": spec.description, "input_schema": spec.input_schema})
    assert doc.startswith("study_validate: ") and " Arguments: study: " in doc


def test_the_flag_is_cli_only_and_off_by_default():
    from nagasim import mcp_server
    p = argparse.ArgumentParser()
    mcp_server.add_arguments(p)
    assert p.parse_args([]).tool_picker is None
    assert p.parse_args(["--tool-picker", "gte"]).tool_picker == "gte"
    with pytest.raises(SystemExit):
        p.parse_args(["--tool-picker", "anything"])
    assert all(not ({"picker", "tool_picker", "rerank"} & set(spec.input_schema["properties"]))
               for spec in agent_tools.TOOLS.values())


def test_an_unknown_name_or_a_missing_package_is_one_clear_error(monkeypatch):
    with pytest.raises(toolpicker.PickerUnavailable, match="Unknown tool picker"):
        toolpicker.load("other", str)
    if importlib.util.find_spec("sentence_transformers") is None:
        with pytest.raises(toolpicker.PickerUnavailable, match=r"nagasim\[picker\]"):
            discover.use_picker("gte")
        assert discover.picker is None


@pytest.mark.slow
@pytest.mark.skipif(importlib.util.find_spec("sentence_transformers") is None,
                    reason="needs the picker extra (sentence-transformers)")
def test_the_real_gte_picker_ranks_validate_first():
    try:
        discover.use_picker("gte")
        out = agent_tools.call("find_tool", query="check the model against a textbook answer", limit=3)
        assert "study_validate" in [h["name"] for h in out["tools"]] and "guidance" in out
    finally:
        discover.use_picker(None)
