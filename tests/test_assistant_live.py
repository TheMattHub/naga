"""Live smoke test of the assistant's tool loop against the real Anthropic API (marker `live`).

Everything else in the suite scripts the client, so a wire-format change on Anthropic's side would
go unseen. This one test closes that gap: it needs ANTHROPIC_API_KEY in the environment (the key
store and files are deliberately not read, so a run is always the caller's explicit choice) and
costs a few cents on the cheapest model. Run: pytest -q -m live
"""
import os

import pytest

from nagasim import assistant as asst
from nagasim import llm
from nagasim.core.document import ModelDocument

pytestmark = [pytest.mark.live, pytest.mark.slow,
              pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                                 reason="ANTHROPIC_API_KEY not set (live API test)")]

CHEAPEST_MODEL = "claude-haiku-5-5"


def test_assistant_answers_from_one_read_only_tool_call():
    assert CHEAPEST_MODEL in llm.ANTHROPIC_MODELS      # the id must stay one the assistant offers
    doc = ModelDocument.load(
        os.path.join(os.path.dirname(asst.__file__), "models", "single_server.json"))
    before = doc.to_layout()
    provider = llm.build_provider(llm.resolve_config(
        provider="anthropic", model=CHEAPEST_MODEL, environ=os.environ))
    # Only validate_model is offered: read-only, so "nothing was written" is by construction
    # and also checked below.
    session = asst.AssistantSession(asst.WorkspaceAdapter(doc), provider,
                                    select_tools=lambda kind, extra=(): ["validate_model"],
                                    max_tool_rounds=4)
    result = session.run_turn("Call the validate_model tool, then tell me whether the model is "
                              "valid and the name of the resource it uses.")
    assert result.error is None, result.error
    assert [c.name for c in result.tool_calls] == ["validate_model"]
    assert not result.hit_round_cap and not result.cancelled
    assert "server" in result.text.lower()          # the model's one resource is "Server"
    assert doc.to_layout() == before and not doc.can_undo
