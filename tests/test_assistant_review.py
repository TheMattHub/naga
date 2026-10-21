"""Review of an agent's edit (D680): the turn's undo step is named, the dock lists what the turn
changed and offers Keep or Revert, and the next turn tells the agent the answer.
The dock's half is in test_ui_assistant_review.py (product).
"""
import pytest

from nagasim import assistant as asst
from nagasim.core.document import ModelDocument
from test_assistant import ScriptedProvider, calls_reply, make_adapter, text_reply


def _model():
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0), "arrive")
    doc.add_block("PROCESS", (200, 0), "serve")
    return doc


def _edit_turn():
    """One turn that adds a block, changes a parameter and moves a block."""
    return [calls_reply(
        ("c1", "add_block", {"type": "DISPOSE", "name": "leave", "position": [400, 0]}),
        ("c2", "update_block", {"name": "serve", "params": {"quantity": 2}}),
        ("c3", "update_block", {"name": "arrive", "position": [10, 10]})), text_reply("Done.")]


# ---------------------------------------------------------------- engine
def test_a_turn_is_one_undo_step_named_by_its_label_and_by_default_Edit():
    named, plain = _model(), _model()
    for doc, label in ((named, asst.turn_label("add a sink")), (plain, None)):
        provider = ScriptedProvider(_edit_turn())
        asst.AssistantSession(make_adapter(doc), provider).run_turn("add a sink", label=label)
    assert named.undo_labels()[0] == "Assistant: add a sink"
    assert plain.undo_labels()[0] == "Edit"
    assert named.undo_labels()[1:] == plain.undo_labels()[1:]       # still ONE step for the turn


def test_an_adapter_whose_begin_turn_takes_no_label_still_runs_a_turn():
    class Bare:                     # the study room's shape
        offered = ()

        def offer(self, names): pass
        def begin_turn(self): pass
        def commit_turn(self): pass
        def rollback_turn(self, exc=None): pass
        def model_summary(self): return {}
    result = asst.AssistantSession(Bare(), ScriptedProvider([text_reply("hi")])).run_turn("hello")
    assert result.error is None and result.text == "hi"


@pytest.mark.parametrize("prompt, label", [
    ("add a sink", "Assistant: add a sink"),
    ("x" * 60, "Assistant: " + "x" * 40),                            # the first 40 characters
    ("add a sink\n\n  and   connect it", "Assistant: add a sink and connect it"),   # one line
])
def test_turn_label(prompt, label):
    assert asst.turn_label(prompt) == label


def test_review_lines_list_added_removed_changed_and_one_count_for_moves():
    doc = _model()
    doc.add_block("DISPOSE", (400, 0), "leave")
    before = doc.to_layout()
    with doc.transaction():
        doc.add_block("DELAY", (300, 100), "wait")
        doc.remove_blocks(["leave"])
        doc.set_params("serve", {**doc.blocks["serve"].params, "quantity": 2})
        doc.move_blocks({"arrive": (5, 5), "serve": (205, 5)})
    assert asst.review_lines(before, doc.to_layout()) == [
        "removed blocks.leave", "changed blocks.serve.params.quantity: 1 -> 2",
        "added blocks.wait", "2 items moved on the canvas"]
    assert asst.review_lines(before, before) == []


def test_review_lines_cut_a_long_line_and_a_long_list():
    doc = _model()
    before = doc.to_layout()
    with doc.transaction():
        doc.set_params("serve", {**doc.blocks["serve"].params, "setup": "s" * 500})
        for i in range(5):
            doc.add_block("DELAY", (i * 10, 100), f"q{i}")
    lines = asst.review_lines(before, doc.to_layout(), limit=3, width=60)
    assert len(lines) == 4 and lines[-1] == "... and 3 more changes"
    assert lines[0] == "added blocks.q0" and lines[2] == "added blocks.q2"
    long = asst.review_lines(before, doc.to_layout(), width=60)[-1]
    assert len(long) == 60 and long.endswith("...")
