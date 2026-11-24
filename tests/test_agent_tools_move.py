"""`set_layout` moves a floor piece by the editor's one move rule (agent tool gaps Task 4, O10):
the joins and merges it no longer meets are dropped and listed, area links stay, one undo step.
Qt-free, so it ships with the engine; the editor's side is `test_ui_conveyor_move_rule.py`."""
import pytest

from nagasim import agent_tools as T
from nagasim.core import floor as D
from nagasim.core.document import ModelDocument

DELAY = {"distribution": "Constant", "dist_params": {"value": 5.0}, "time_unit": "base"}


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def _out(doc, name, port="out"):
    return next(p for p in D.piece_ports(doc, name) if p.name == port)


def _links(doc) -> list:
    return sorted((k.source, k.target, (k.ports or {}).get("source_port"),
                   (k.ports or {}).get("target_port"), k.delay) for k in doc.link_records())


def _floor(ws):
    """A (0, 0, 0) straight 2, B snapped to A.out, C snapped to B.out; D at (10, 5) fed by B
    over a delayed (area) link; T a 10 m trunk at (0, 10) heading east and F a 2 m feeder from
    (4, 7) heading south whose out port ends on T's centreline at 4, merged there."""
    doc = ModelDocument()
    D.add_belt_piece(doc, [{"straight": 2.0}], pose=(0.0, 0.0, 0.0), name="A")
    D.add_belt_piece(doc, [{"straight": 2.0}], source=_out(doc, "A"), name="B")
    D.add_belt_piece(doc, [{"straight": 2.0}], source=_out(doc, "B"), name="C")
    D.add_belt_piece(doc, [{"straight": 2.0}], pose=(10.0, 5.0, 0.0), name="D")
    doc.add_link("B", "D")
    doc.set_link_delay("B", "D", DELAY)
    D.add_belt_piece(doc, [{"straight": 10.0}], pose=(0.0, 10.0, 0.0), name="T")
    D.add_belt_piece(doc, [{"straight": 2.0}], pose=(4.0, 8.0, 90.0), name="F")
    doc.set_params("T", {**doc.blocks["T"].params, "accumulating": True,
                         "merges": [{"at": 4.0, "from": "F"}]})
    doc.add_link("F", "T", source_port="out")
    doc._undo_stack.clear()
    doc._undo_labels.clear()
    ws.models["m"] = doc
    return doc


def test_set_layout_of_a_joined_piece_drops_its_joins_and_undo_restores_them(ws):
    """B moved 2 m south: B.in is 2 m from A.out and B.out 2 m from C.in, so A -> B and B -> C
    go; B -> D carries a delay (an area link) and stays. One undo step brings both back."""
    doc = _floor(ws)
    links = _links(doc)
    reply = T.call("set_layout", model="m", block="B", x=2.0, y=2.0, rotation=0.0)
    assert "error" not in reply, reply
    assert sorted(reply["dropped_links"]) == ["A -> B", "B -> C"]
    assert reply["dropped_merges"] == []
    assert [k[:2] for k in _links(doc)] == [("B", "D"), ("F", "T")]
    assert doc.link_delay("B", "D") == DELAY
    assert len(doc.undo_labels()) == 1
    assert "error" not in T.call("undo", model="m")
    assert _links(doc) == links
    assert tuple(doc.blocks["B"].layout_position) == (2.0, 0.0)


def test_set_layout_of_a_feeder_off_its_merge_drops_the_merge_with_its_link(ws):
    """F moved 3 m west: its end (1, 10) is 3 m from the merge point (4, 10), outside T's band,
    so the merge entry and the F -> T link go in the same step. The link is listed with its
    merge, not in dropped_links."""
    doc = _floor(ws)
    reply = T.call("set_layout", model="m", block="F", x=1.0, y=8.0, rotation=90.0)
    assert reply["dropped_merges"] == [{"trunk": "T", "from": "F", "at": 4.0}]
    assert reply["dropped_links"] == []
    assert doc.blocks["T"].params["merges"] == []
    assert not any(k.source == "F" for k in doc.link_records())
    T.call("undo", model="m")
    assert doc.blocks["T"].params["merges"] == [{"at": 4.0, "from": "F"}]
    assert [k[:2] for k in _links(doc) if k[0] == "F"] == [("F", "T")]


def test_set_layout_that_keeps_the_joins_drops_nothing(ws):
    """A set to its own pose: its join A -> B is dropped and made again at the same ports, which
    is no change, so nothing is listed and the links read as before. (Not B or C: once B -> C
    is gone, the area link B -> D, which names no port, takes B's free out slot, so the join
    is not made again; the editor's rule, kept as it is.)"""
    doc = _floor(ws)
    links = _links(doc)
    reply = T.call("set_layout", model="m", block="A", x=0.0, y=0.0, rotation=0.0)
    assert reply["dropped_links"] == [] and reply["dropped_merges"] == []
    assert _links(doc) == links


def test_set_layout_of_a_plain_block_is_unchanged(ws):
    """A CREATE linked to A: set_layout writes its pose, keeps the link and answers as before
    (no dropped_* keys)."""
    doc = _floor(ws)
    made = doc.add_block("CREATE", (0.0, 0.0))
    doc.add_link(made, "A")
    reply = T.call("set_layout", model="m", block=made, x=-3.0, y=1.0, rotation=45.0)
    assert "dropped_links" not in reply and "dropped_merges" not in reply
    assert tuple(doc.blocks[made].layout_position) == (-3.0, 1.0)
    assert doc.blocks[made].layout_rotation == 45.0
    assert any(k.source == made and k.target == "A" for k in doc.link_records())
