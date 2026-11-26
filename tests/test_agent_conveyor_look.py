"""Plan H Task 3: the assistant reads and sets a conveyor piece's look through the piece tools
(`update_block(look, scope, reset)`, `place_piece(look, scope)`, `get_ports` read-back). No new
tool; `scope` and `reset` choose what to edit and grant nothing."""
import copy

import pytest

from nagasim import agent_tools as T
from nagasim import api


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def ok(result):
    assert "error" not in result, result.get("error")
    return result


def _three(model="m"):
    """A -> B -> C, three snapped roller pieces: one chain."""
    ok(T.call("new_model", name=model))
    ok(T.call("place_piece", model=model, name="A", type="roller", segments=[{"straight": 2.0}]))
    ok(T.call("place_piece", model=model, name="B", type="roller", snap_to="A.out",
              segments=[{"straight": 2.0}]))
    ok(T.call("place_piece", model=model, name="C", type="roller", snap_to="B.out",
              segments=[{"straight": 2.0}]))


def _params(model, name):
    return copy.deepcopy(T.workspace().document(model).blocks[name].params)


def _without_look(layout):
    out = copy.deepcopy(layout)
    for block in out["blocks"]:
        (block.get("params") or {}).pop("look", None)
        (block.get("params") or {}).pop("look_set", None)
    return out


def test_the_assistant_builds_a_guarded_line_with_an_opening_and_reads_it_back():
    from nagasim import assistant
    from nagasim.core.document import ModelDocument

    doc = ModelDocument()
    adapter = assistant.WorkspaceAdapter(doc)
    adapter.offer({"add_block", "place_piece", "update_block", "connect", "get_ports"})
    adapter.begin_turn("t")
    run = adapter.run_tool
    ok(run("add_block", {"type": "CREATE", "name": "src", "params": {
        "max_arrivals": 5, "distribution": "Constant", "dist_params": {"value": 1.0}}}))
    ok(run("place_piece", {"name": "A", "type": "roller", "segments": [{"straight": 3.0}],
                           "look": {"guide_left": 0.1, "guide_right": 0.1, "guide_style": "flat"}}))
    ok(run("place_piece", {"name": "B", "type": "roller", "snap_to": "A.out",
                           "segments": [{"straight": 2.0}]}))
    answer = ok(run("update_block", {"name": "B", "scope": "piece", "look": {
        "openings": [{"side": "left", "start": 0.5, "end": 1.5}]}}))
    assert answer["look_changed"] == ["B"] and answer["look_skipped"] == {}
    ok(run("connect", {"source": "src", "target": "A"}))
    ok(run("add_block", {"type": "DISPOSE", "name": "end"}))
    ok(run("connect", {"source": "B", "target": "end", "source_port": "out"}))
    adapter.commit_turn()

    b = ok(run("get_ports", {"block": "B"}))
    assert b["look"]["guide_left"] == 0.1 and b["look"]["guide_style"] == "flat"
    assert b["look"]["openings"] == [{"side": "left", "start": 0.5, "end": 1.5}]
    assert b["chain"] == ["A", "B"] and b["look_set"] == []
    assert ok(run("get_ports", {"block": "A"}))["look"]["openings"] == []

    layout = doc.to_layout()
    assert not [p for p in doc.problems() if p.severity == "error"]
    plain = _without_look(layout)
    assert plain != layout
    runs = [api.run_model(m) for m in (layout, plain)]
    assert runs[0].kpis().equals(runs[1].kpis())
    assert runs[0].kpi("system/Total Entities Completed") == 5      # parts crossed both belts


def test_update_block_spreads_and_reports_skips(ws):
    _three()
    ok(T.call("update_block", model="m", name="B", look={"width": 0.7}, scope="piece"))
    answer = ok(T.call("update_block", model="m", name="A", look={"width": 0.9}))
    assert answer["look_changed"] == ["A", "C"]
    assert answer["look_skipped"] == {"B": ["width"]}
    assert ok(T.call("get_ports", model="m", block="B"))["look_set"] == ["width"]
    answer = ok(T.call("update_block", model="m", name="B", reset=["width"]))
    assert answer["look_changed"] == ["B"]          # a reset acts on the named piece only
    b = ok(T.call("get_ports", model="m", block="B"))
    assert b["look"]["width"] == 0.9 and b["look_set"] == []


def test_a_chain_write_named_on_a_hand_set_piece_keeps_the_mark(ws):
    """D1474, spec item 3: with the default scope, B takes the value and keeps width
    by hand, so the next line-wide write from A skips it."""
    _three()
    ok(T.call("update_block", model="m", name="B", look={"width": 0.7}, scope="piece"))
    answer = ok(T.call("update_block", model="m", name="B", look={"width": 0.9}))
    assert answer["look_changed"] == ["A", "B", "C"]
    b = ok(T.call("get_ports", model="m", block="B"))
    assert b["look"]["width"] == 0.9 and b["look_set"] == ["width"]
    answer = ok(T.call("update_block", model="m", name="A", look={"width": 0.8}))
    assert answer["look_skipped"] == {"B": ["width"]}


def test_a_rename_with_a_look_answers_the_new_name(ws):
    _three()
    answer = ok(T.call("update_block", model="m", name="A", new_name="A2",
                       look={"frame_color": "green"}))
    assert answer["block"] == "A2" and answer["look_changed"] == ["A2", "B", "C"]


def test_an_answer_without_a_look_carries_no_look_keys(ws):
    _three()
    answer = ok(T.call("update_block", model="m", name="A", params={"speed": 0.5}))
    assert "look_changed" not in answer and "look_skipped" not in answer
    assert "look_changed" not in ok(T.call("place_piece", model="m", name="D",
                                           segments=[{"straight": 1.0}], snap_to="C.out"))


def test_update_block_refuses_look_inside_params(ws):
    _three()
    before = T.workspace().document("m").to_layout()
    for key in ("look", "look_set"):
        answer = T.call("update_block", model="m", name="A", params={key: {"width": 0.7}})
        assert answer["error"]["code"] == "E_LOOK"
        assert "look=" in answer["error"]["hint"]
    assert T.workspace().document("m").to_layout() == before


def test_a_bad_look_is_E_LOOK_with_the_fields(ws):
    _three()
    answer = T.call("update_block", model="m", name="A", look={"belt_kind": "x"})
    assert answer["error"]["code"] == "E_LOOK"
    assert "roller_pitch" in answer["error"]["hint"] and "belt_kind" in answer["error"]["message"]


def test_a_refused_look_writes_nothing_else(ws):
    """The params, the new piece and its link are written before the look is checked; the
    refusal rolls them back with it."""
    _three()
    before = T.workspace().document("m").to_layout()
    answer = T.call("update_block", model="m", name="A", params={"speed": 3.0},
                    look={"belt_kind": "x"})
    assert answer["error"]["code"] == "E_LOOK"
    answer = T.call("place_piece", model="m", name="D", type="roller", snap_to="C.out",
                    segments=[{"straight": 1.0}], look={"width": "wide"})
    assert answer["error"]["code"] == "E_LOOK"
    assert T.workspace().document("m").to_layout() == before


def test_a_refused_look_inside_a_turn_leaves_no_half_edit():
    from nagasim import assistant
    from nagasim.core.document import ModelDocument

    doc = ModelDocument()
    adapter = assistant.WorkspaceAdapter(doc)
    adapter.offer({"place_piece"})
    adapter.begin_turn("t")
    ok(adapter.run_tool("place_piece", {"name": "A", "type": "roller",
                                        "segments": [{"straight": 2.0}]}))
    answer = adapter.run_tool("place_piece", {"name": "B", "type": "roller", "snap_to": "A.out",
                                              "segments": [{"straight": 1.0}],
                                              "look": {"belt_kind": "x"}})
    adapter.commit_turn()
    assert answer["error"]["code"] == "E_LOOK"
    assert set(doc.blocks) == {"A"} and not doc.links


def test_place_piece_with_chain_scope_spreads_to_the_chain_it_joins(ws):
    _three()
    answer = ok(T.call("place_piece", model="m", name="D", type="roller", snap_to="C.out",
                       segments=[{"straight": 1.0}], look={"frame_color": "blue"}))
    assert answer["look_changed"] == ["A", "B", "C", "D"]
    assert ok(T.call("get_ports", model="m", block="A"))["look"]["frame_color"] == "blue"


def test_apply_edits_carries_a_look(ws):
    _three("one")
    ok(T.call("place_piece", model="one", name="D", type="roller", snap_to="C.out",
              segments=[{"straight": 1.0}], look={"height": 0.9}))
    ok(T.call("update_block", model="one", name="B", look={"guide_left": 0.1}, scope="piece"))
    _three("batch")
    ok(T.call("apply_edits", model="batch", edits=[
        {"tool": "place_piece", "args": {"name": "D", "type": "roller", "snap_to": "C.out",
                                         "segments": [{"straight": 1.0}], "look": {"height": 0.9}}},
        {"tool": "update_block", "args": {"name": "B", "look": {"guide_left": 0.1},
                                          "scope": "piece"}}]))
    one = T.workspace().document("one").to_layout()
    batch = T.workspace().document("batch").to_layout()
    assert one["blocks"] == batch["blocks"] and one["links"] == batch["links"]
    assert _params("batch", "B")["look_set"] == ["guide_left"]


def test_a_batch_names_the_pieces_a_look_write_skipped(ws):
    """Edit 0 has no look, edit 1 sets B by hand, edit 2 spreads from A and skips B."""
    _three()
    answer = ok(T.call("apply_edits", model="m", edits=[
        {"tool": "update_block", "args": {"name": "C", "params": {"speed": 0.5}}},
        {"tool": "update_block", "args": {"name": "B", "look": {"width": 0.7}, "scope": "piece"}},
        {"tool": "update_block", "args": {"name": "A", "look": {"width": 0.9}}}]))
    assert answer["looks"] == {
        "1": {"look_changed": ["B"], "look_skipped": {}},
        "2": {"look_changed": ["A", "C"], "look_skipped": {"B": ["width"]}}}
    assert "looks" not in ok(T.call("apply_edits", model="m", edits=[
        {"tool": "update_block", "args": {"name": "C", "params": {"speed": 0.6}}}]))


def test_get_ports_on_a_transfer_has_no_look(ws):
    _three()
    ok(T.call("place_transfer", model="m", name="X", snap_to="C.out",
              exits=[{"port": "straight", "angle": 0}]))
    answer = ok(T.call("get_ports", model="m", block="X"))
    assert not {"look", "look_set", "chain"} & set(answer)


def test_scope_is_no_permission():
    for name in ("update_block", "place_piece"):
        props = T.TOOLS[name].input_schema["properties"]
        assert not [p for p in props if any(w in p.lower() for w in ("allow", "permit", "trust"))]
        assert props["scope"]["enum"] == ["chain", "piece"]


def test_a_look_that_breaks_a_cross_rule_is_refused_whole(ws):
    _three()
    ok(T.call("update_block", model="m", name="B", look={"roller_diameter": 0.07}, scope="piece"))
    doc = T.workspace().document("m")
    before = doc.to_layout()
    for name, look, scope in (("A", {"roller_pitch": 0.04}, "piece"),
                              ("A", {"roller_pitch": 0.065}, "chain")):
        answer = T.call("update_block", model="m", name=name, look=look, scope=scope)
        assert answer["error"]["code"] == "E_LOOK"
        assert doc.to_layout() == before
    ok(T.call("new_model", name="n"))
    ok(T.call("place_piece", model="n", name="A", type="chain", segments=[{"straight": 2.0}]))
    answer = T.call("update_block", model="n", name="A", look={"width": 0.5})
    assert answer["error"]["code"] == "E_LOOK"
    assert "look" not in T.workspace().document("n").blocks["A"].params or \
        not T.workspace().document("n").blocks["A"].params["look"]


def test_get_ports_reads_a_string_look_set_as_empty(ws):
    _three()
    doc = T.workspace().document("m")
    doc.set_params("A", {**doc.blocks["A"].params, "look_set": "width"})
    assert ok(T.call("get_ports", model="m", block="A"))["look_set"] == []
