"""The checks of an item's code: parse-only (the code never runs, the trust store is never
read), coded E_ITEM_CODE for the source and E_ITEM_HOOK for its wiring."""
from __future__ import annotations

import os

from nagasim.core import plainlines, schema
from test_item_code_model import GATE_SRC, block, gate_definition, gate_model


def problems_of(layout, code=None):
    found = schema.validate_model_structured(layout)
    return [p for p in found if code is None or p.code == code]


def with_code(source=GATE_SRC, hook="entry", **extra):
    return gate_model(Gate=gate_definition({"class": "Gate", "source": source, **extra}, hook=hook))


def test_a_valid_item_has_no_item_problem():
    assert not [p for p in problems_of(with_code()) if p.code.startswith("E_ITEM")]


def test_a_syntax_error_is_coded_with_its_line():
    found = problems_of(with_code("class Gate:\n    def on_entry(self, entity, item)\n        return 0\n"),
                        "E_ITEM_CODE")
    assert len(found) == 1 and found[0].fields["case"] == "syntax"
    assert found[0].fields["line"] == 2
    assert found[0].path == "/submodels/Gate/code"
    assert plainlines.plain(found[0]).startswith("Item Gate: its code does not parse at line 2")


def test_checking_never_runs_the_code(monkeypatch):
    monkeypatch.delenv("RAN", raising=False)
    source = ("import os\nos.environ['RAN'] = '1'\nclass Gate:\n"
              "    def on_entry(self, entity, item):\n        return 0\n")
    assert problems_of(with_code(source), "E_ITEM_CODE") == []
    assert "RAN" not in os.environ


def test_wrong_hook_arguments_name_the_wanted_ones():
    source = GATE_SRC + "    def on_timer(self):\n        pass\n"
    found = problems_of(with_code(source, timer=5), "E_ITEM_CODE")
    assert [p.fields["case"] for p in found] == ["hook_args"]
    assert found[0].fields["wanted"] == "self, item"
    assert plainlines.plain(found[0]) == "Item Gate: on_timer takes (self, item)"


def test_a_missing_class_and_no_hook_are_named():
    assert problems_of(with_code(GATE_SRC, **{"class": "Other"}), "E_ITEM_CODE")[0].fields["case"] == "class"
    found = problems_of(with_code("class Gate:\n    pass\n", hook=None), "E_ITEM_CODE")
    assert [p.fields["case"] for p in found] == ["no_hook"]


def test_entry_hook_block_needs_on_entry():
    source = "class Gate:\n    def on_timer(self, item):\n        pass\n"
    found = problems_of(with_code(source, timer=5), "E_ITEM_HOOK")
    assert [p.fields["case"] for p in found] == ["missing"]
    assert found[0].block == "route" and found[0].fields["hook"] == "on_entry"
    # the same hook block at the top level has no item
    layout = gate_model(Gate=gate_definition())
    layout["blocks"].append(block("top", "LOGIC", hook="entry"))
    layout["links"] += [{"source": "top", "target": "d1"}]
    layout["blocks"][0], layout["links"][0] = block("c1", "CREATE"), {"source": "c1", "target": "top"}
    cases = {p.block: p.fields["case"] for p in problems_of(layout, "E_ITEM_HOOK")}
    assert cases["top"] == "no_code"
    assert cases["g1/route"] == "no_code"     # a hook in a definition without code


def test_a_hook_value_must_be_entry_or_exit():
    found = problems_of(with_code(hook="sideways"), "E_ITEM_HOOK")
    assert [p.fields["case"] for p in found] == ["hook_value", "hook_value"]
    assert found[0].fields["value"] == "sideways"


def test_timer_and_on_timer_go_together():
    assert [p.fields["case"] for p in problems_of(with_code(timer=5), "E_ITEM_HOOK")] == ["timer"]
    source = GATE_SRC + "    def on_timer(self, item):\n        pass\n"
    assert [p.fields["case"] for p in problems_of(with_code(source, timer=None), "E_ITEM_HOOK")] == ["timer"]
    assert problems_of(with_code(source, timer=5), "E_ITEM_HOOK") == []


def test_watch_names_a_model_variable():
    found = problems_of(with_code(watch=["ghost"]), "E_ITEM_HOOK")
    assert [p.fields["case"] for p in found] == ["watch"]
    assert found[0].fields["variable"] == "ghost"


def test_signals_and_on_signal_go_together():
    assert [p.fields["case"] for p in problems_of(with_code(signals=["go"]), "E_ITEM_HOOK")] == ["signals"]


def test_a_top_level_item_code_key_is_refused():
    layout = with_code()
    layout["item_code"] = {}
    found = problems_of(layout, "E_ITEM_HOOK")
    assert [p.fields["case"] for p in found] == ["top_level"]


def test_too_long_code_is_one_problem():
    found = problems_of(with_code("#" * 200_001), "E_ITEM_CODE")
    assert [p.fields["case"] for p in found] == ["size"]
