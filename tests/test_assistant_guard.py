"""The assistant's edit guard (D688): facts and law warnings that are new or gone after an edit."""
import copy

from nagasim import assistant as asst
from nagasim.core.document import ModelDocument


def _flow():
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0), "arrive")
    doc.add_block("PROCESS", (200, 0), "serve")
    doc.add_block("DISPOSE", (400, 0), "leave")
    doc.set_params("serve", {**doc.blocks["serve"].params, "action": "Delay"})
    doc.add_link("arrive", "serve")
    doc.add_link("serve", "leave")
    layout = doc.to_layout()
    layout["simulation"] = {**(layout.get("simulation") or {}), "time_limit": 50, "replications": 1}
    return layout


def test_guard_lists_a_new_never_entered_fact():
    before = _flow()
    after = copy.deepcopy(before)
    # an edit that leaves a new block nothing reaches (the same fact as a removed link, with a
    # model that still validates)
    after["blocks"] = [*after["blocks"], {"type": "DISPOSE", "name": "side", "position": [400, 100], "params": {}}]
    lines = asst.guard_lines(before, after)
    assert any(line.startswith("New fact") and "side" in line for line in lines), lines
    assert asst.guard_lines(before, before) == []


def test_guard_skips_over_budget_with_a_note():
    lines = asst.guard_lines(_flow(), _flow(), budget_s=0.0)
    assert len(lines) == 1 and lines[0].startswith("Run check skipped")
