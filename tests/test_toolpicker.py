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


# --- the hosted haiku picker (D672), through a fake anthropic module: no network ---

FAKE_KEY = "sk-ant-fake-0123456789"


class _FakeAnthropic:
    """Stands in for anthropic.Anthropic: records each call and answers `ranking` (or raises it)."""
    ranking = ["study_validate", "not_a_tool", "run_model"]
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = self

    def create(self, **kwargs):
        _FakeAnthropic.calls.append(kwargs)
        if isinstance(self.ranking, Exception):
            raise self.ranking
        block = type("B", (), {"type": "tool_use", "input": {"ranking": self.ranking}})()
        return type("R", (), {"content": [block]})()


@pytest.fixture
def haiku(monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_FakeAnthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    _FakeAnthropic.calls = []
    monkeypatch.setattr(_FakeAnthropic, "ranking", ["study_validate", "not_a_tool", "run_model"])
    discover.use_picker("haiku")
    yield _FakeAnthropic
    discover.use_picker(None)


def test_haiku_ranks_every_exposed_tool_and_its_picks_come_first(haiku, monkeypatch):
    from nagasim import toolsearch
    allowed = frozenset(list(agent_tools.TOOLS)[:40]) | {"study_validate", "run_model"}
    monkeypatch.setattr(discover, "exposed", allowed)
    out = agent_tools.call("find_tool", query="zzz unmatched words validate", limit=5)
    sent = haiku.calls[0]
    assert set(sent["tools"][0]["input_schema"]["properties"]["ranking"]["items"]["enum"]) == allowed
    assert sent["messages"] == [{"role": "user", "content": "zzz unmatched words validate"}]
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    names = [h["name"] for h in out["tools"]]
    plain = [h["name"] for h in toolsearch.search(discover.catalogue_index(), "zzz unmatched words validate",
                                                  limit=20, choices=allowed)]
    assert names[:2] == ["study_validate", "run_model"]
    assert names[2:] == [n for n in plain if n not in names[:2]][:3]
    assert out["guidance"] == toolpicker.GUIDANCE
    assert all(set(h) == {"name", "score", "summary", "args"} for h in out["tools"])


def test_haiku_failure_keeps_find_tools_order_and_never_logs_the_key(haiku, caplog):
    from nagasim import toolsearch
    haiku.ranking = RuntimeError(f"401 for key {FAKE_KEY}")
    out = agent_tools.call("find_tool", query=QUERY)
    plain = toolsearch.search(discover.catalogue_index(), QUERY, limit=5)
    assert out["tools"] == plain
    assert "RuntimeError" in caplog.text
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in json.dumps(out)


def test_haiku_without_key_or_package_is_one_clear_error(monkeypatch):
    import sys
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NAGASIM_LLM_API_KEY", raising=False)
    with pytest.raises(toolpicker.PickerUnavailable, match="ANTHROPIC_API_KEY"):
        discover.use_picker("haiku")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setitem(sys.modules, "anthropic", None)      # import anthropic -> ImportError
    with pytest.raises(toolpicker.PickerUnavailable, match=r"nagasim\[assistant\]") as err:
        discover.use_picker("haiku")
    assert FAKE_KEY not in str(err.value) and discover.picker is None


def test_the_flag_offers_haiku():
    from nagasim import mcp_server
    p = argparse.ArgumentParser()
    mcp_server.add_arguments(p)
    assert p.parse_args(["--tool-picker", "haiku"]).tool_picker == "haiku"


@pytest.mark.slow
@pytest.mark.skipif(not __import__("os").environ.get("ANTHROPIC_API_KEY")
                    or importlib.util.find_spec("anthropic") is None,
                    reason="needs ANTHROPIC_API_KEY and the anthropic package (a real API call)")
def test_the_real_haiku_picker_ranks_validate_first():
    try:
        discover.use_picker("haiku")
        out = agent_tools.call("find_tool", query="check the model against a textbook answer", limit=3)
        assert "study_validate" in [h["name"] for h in out["tools"]]
    finally:
        discover.use_picker(None)
