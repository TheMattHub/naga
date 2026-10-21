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


# --- the offline backup for haiku (D673): a fake CrossEncoder score, no torch, no network ---

def _backup_score(query, docs):
    return [float(i) for i in range(len(docs))]       # the last candidate scores best


@pytest.fixture
def backup(monkeypatch):
    monkeypatch.setattr(toolpicker, "check_backup", lambda: None)
    monkeypatch.setattr(toolpicker, "_crossencoder", lambda path: _backup_score)


def test_backup_ranks_every_tool_when_haiku_raises(haiku, backup, caplog):
    from nagasim import toolsearch
    haiku.ranking = RuntimeError(f"401 for key {FAKE_KEY}")
    discover.use_picker("haiku", "some/folder")
    allowed = toolsearch.search(discover.catalogue_index(), QUERY, limit=999, rerank=toolpicker.guarded(lambda q, d: [0.0] * len(d), str, pool="all"))
    out = agent_tools.call("find_tool", query=QUERY, limit=5)
    assert [h["name"] for h in out["tools"]] == [h["name"] for h in allowed][::-1][:5]   # from all tools, not the top ten
    assert "RuntimeError" in caplog.text and FAKE_KEY not in caplog.text


def test_both_raising_keeps_find_tools_order(monkeypatch):
    from nagasim import toolsearch

    def broken(query, docs):
        raise RuntimeError("down")
    monkeypatch.setattr(discover, "picker", toolpicker.guarded(broken, str, pool="all", backup=broken))
    assert (agent_tools.call("find_tool", query=QUERY)["tools"]
            == toolsearch.search(discover.catalogue_index(), QUERY, limit=5))


def test_haiku_answering_does_not_use_the_backup(haiku, monkeypatch):
    monkeypatch.setattr(toolpicker, "check_backup", lambda: None)
    monkeypatch.setattr(toolpicker, "_crossencoder", lambda path: _backup_score)
    discover.use_picker("haiku", "some/folder")
    out = agent_tools.call("find_tool", query=QUERY)
    assert out["tools"][0]["name"] == "study_validate"      # Haiku's pick, not the backup's reversed order


def test_no_key_with_a_backup_runs_on_the_backup_alone(monkeypatch, backup, caplog):
    from nagasim import toolsearch
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    discover.use_picker_soon("haiku", "some/folder").join(10)
    assert discover.picker is not None and discover.picker.pool == "all"
    assert "backup alone" in caplog.text
    allowed = toolsearch.search(discover.catalogue_index(), QUERY, limit=999, rerank=toolpicker.guarded(lambda q, d: [0.0] * len(d), str, pool="all"))
    assert [h["name"] for h in agent_tools.call("find_tool", query=QUERY, limit=5)["tools"]] == [h["name"] for h in allowed][::-1][:5]


def test_no_key_without_a_backup_is_still_an_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The package check comes first; CI has no `assistant` extra, so pretend it is there.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    with pytest.raises(toolpicker.PickerUnavailable, match="ANTHROPIC_API_KEY"):
        discover.use_picker_soon("haiku")
    assert discover.picker is None


def test_the_backup_flag_needs_haiku(capsys):
    from nagasim import mcp_server
    p = argparse.ArgumentParser()
    mcp_server.add_arguments(p)
    assert p.parse_args([]).tool_picker_backup is None
    for argv in (["--tool-picker-backup", "x"], ["--tool-picker", "gte", "--tool-picker-backup", "x"]):
        assert mcp_server.serve(p.parse_args(argv)) == 1
        assert "needs --tool-picker haiku" in capsys.readouterr().out


@pytest.mark.parametrize("cuda, want", [(True, "float16"), (False, "float32")])
def test_a_cross_encoder_loads_half_on_a_gpu_and_full_on_cpu(monkeypatch, cuda, want):
    # D674: half precision gives the same picks and is 3x faster on a GPU, but 6x slower on CPU.
    import sys
    import types
    seen = {}

    class _CrossEncoder:
        def __init__(self, name, **kwargs):
            seen.update(kwargs)

    torch = types.SimpleNamespace(float16="float16", float32="float32",
                                  cuda=types.SimpleNamespace(is_available=lambda: cuda))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=_CrossEncoder))
    toolpicker._crossencoder("some/folder")
    assert seen == {"model_kwargs": {"torch_dtype": want}}
