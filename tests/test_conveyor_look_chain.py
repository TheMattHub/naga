"""Conveyor look settings, plan H Task 2: the chain a look spreads along, and `apply_look`, the
one write that spreads it and skips the fields a piece keeps by hand. Expected chains are worked
out by hand from each small rig."""
import pytest

from nagasim.core import conveyor_look as L
from nagasim.core import floor
from nagasim.core.document import ModelDocument

OPENING = {"side": "left", "start": 0.5, "end": 1.0}


def _pieces(doc, *specs):
    """Add straight 3-unit pieces: each spec is "name" (roller) or ("name", kind)."""
    for spec in specs:
        name, kind = (spec, "roller") if isinstance(spec, str) else spec
        floor.add_belt_piece(doc, [{"straight": 3.0}], kind=kind, pose=(0.0, 0.0, 0.0),
                             name=name)


def _line(*names):
    doc = ModelDocument()
    _pieces(doc, *names)
    for src, tgt in zip(names, names[1:]):
        doc.add_link(src, tgt)
    return doc


def _width(doc, name):
    return L.look_of(doc.blocks[name].params)["width"]


def _look_set(doc, name):
    return doc.blocks[name].params.get("look_set", [])


def test_the_chain_stops_at_a_divert_a_merge_and_a_type_change():
    doc = ModelDocument()
    _pieces(doc, "A", "B", "C", "X", "Y", "D", "E", "F", ("G", "belt"))
    doc.add_link("A", "B")
    doc.add_link("B", "C")
    doc.add_link("C", "D")
    doc.add_link("C", "X", source_port="d1")         # two diverts: three links out of C
    doc.add_link("C", "Y", source_port="d2")
    doc.add_link("D", "E")
    doc.add_link("F", "E")                           # a feeder join: two links into E
    doc.add_link("E", "G")                           # roller to belt: a type change
    assert L.chain_of(doc, "A") == ["A", "B", "C"]
    assert L.chain_of(doc, "C") == ["A", "B", "C"]
    assert L.chain_of(doc, "D") == ["D"]
    assert L.chain_of(doc, "E") == ["E"]
    assert L.chain_of(doc, "F") == ["F"]
    assert L.chain_of(doc, "G") == ["G"]
    assert L.chain_of(doc, "X") == ["X"]


def test_a_curve_stays_in_the_chain():
    doc = ModelDocument()
    floor.add_belt_piece(doc, [{"straight": 2.0}], kind="roller", name="A")
    floor.add_belt_piece(doc, [{"arc": {"radius": 1.0, "angle": 90.0, "turn": "left"}}],
                         kind="roller", name="B")
    floor.add_belt_piece(doc, [{"straight": 2.0}], kind="roller", name="C")
    doc.add_link("A", "B")
    doc.add_link("B", "C")
    assert L.chain_of(doc, "C") == ["A", "B", "C"]


def test_a_delayed_link_stays_in_the_chain():
    doc = _line("A", "B")
    doc.set_link_delay("A", "B", {"dist": "constant", "params": [2.0]})
    doc.set_link_area("A", "B", {"capacity": 3})
    assert doc.link_delay("A", "B") is not None
    assert L.chain_of(doc, "A") == ["A", "B"]
    assert L.chain_of(doc, "B") == ["A", "B"]


def test_a_gap_on_a_piece_stays_in_the_chain():
    doc = _line("A", "B", "C")
    doc.set_params("B", {**doc.blocks["B"].params, "gap": 0.2})
    assert L.chain_of(doc, "A") == ["A", "B", "C"]


def test_a_lone_piece_and_a_block_that_is_no_conveyor():
    doc = _line("A")
    assert L.chain_of(doc, "A") == ["A"]
    doc.add_block("DISPOSE", (0.0, 0.0), "out")
    doc.add_link("A", "out")
    assert L.chain_of(doc, "A") == ["A"]
    assert L.chain_of(doc, "out") == ["out"]


def test_parallel_links_count_twice():
    doc = _line("A", "B")
    doc.add_link("A", "B", source_port="d1")         # a divert back onto the same piece
    assert L.chain_of(doc, "A") == ["A"]
    assert L.chain_of(doc, "B") == ["B"]


def test_a_ring_is_one_chain_listed_once():
    doc = _line("A", "B", "C")
    doc.add_link("C", "A")
    chain = L.chain_of(doc, "B")
    assert sorted(chain) == ["A", "B", "C"] and len(chain) == 3
    result = L.apply_look(doc, "B", {"width": 0.7})
    assert sorted(result.changed) == ["A", "B", "C"]
    assert [_width(doc, n) for n in "ABC"] == [0.7, 0.7, 0.7]


def _hand_set_b():
    doc = _line("A", "B", "C")
    result = L.apply_look(doc, "B", {"width": 0.7}, scope="piece")
    assert result.changed == ["B"] and result.skipped == {}
    assert _look_set(doc, "B") == ["width"]
    return doc


def test_chain_scope_skips_a_piece_set_by_hand():
    doc = _hand_set_b()
    result = L.apply_look(doc, "A", {"width": 0.9})
    assert result == L.LookResult(changed=["A", "C"], skipped={"B": ["width"]})
    assert [_width(doc, n) for n in "ABC"] == [0.9, 0.7, 0.9]


def test_a_hand_set_value_stays_even_when_it_equals_the_chain():
    doc = _line("A", "B", "C")
    L.apply_look(doc, "B", {"width": 0.9}, scope="piece")
    result = L.apply_look(doc, "A", {"width": 0.9})
    assert result.changed == ["A", "C"] and result.skipped == {"B": ["width"]}
    assert _look_set(doc, "B") == ["width"]


def test_chain_scope_named_on_the_hand_set_piece_keeps_its_mark():
    """Spec Builder check item 3 (D1474): the named piece takes the value and the
    field stays by hand until it is reset, so a later write from A still skips B."""
    doc = _hand_set_b()
    L.apply_look(doc, "A", {"width": 0.9})
    result = L.apply_look(doc, "B", {"width": 0.9})
    assert result.changed == ["B"] and result.skipped == {}
    assert _width(doc, "B") == 0.9 and _look_set(doc, "B") == ["width"]
    result = L.apply_look(doc, "A", {"width": 0.8})
    assert result.skipped == {"B": ["width"]} and _width(doc, "B") == 0.9
    L.apply_look(doc, "B", reset=["width"])
    assert _width(doc, "B") == 0.8 and _look_set(doc, "B") == []


def test_width_and_height_read_the_field_look_of_resolves():
    """The per-frame readers read one field and never drift from look_of."""
    for kind in L.TYPES:
        cases = [{"type": kind}, {"type": kind, "look": {"width": 0.9, "height": 1.1}},
                 {"type": kind, "look": {"width": 99, "height": "high"}},          # refused
                 {"type": kind, "look": "not a look"}]
        for params in cases:
            look = L.look_of(params)
            assert L.width_m(params) == look["width"], (kind, params)
            assert L.height_m(params) == look["height"], (kind, params)
    assert L.width_m({"type": ""}) is None and L.height_m({}) is None


def test_reset_takes_the_nearest_upstream_value():
    doc = _line("A", "B", "C")
    L.apply_look(doc, "A", {"width": 0.8})
    L.apply_look(doc, "B", {"width": 0.7}, scope="piece")
    L.apply_look(doc, "C", {"width": 0.6}, scope="piece")
    assert L.chain_value(doc, "C", "width") == 0.8
    result = L.apply_look(doc, "C", reset=["width"])
    assert result.changed == ["C"]
    assert _width(doc, "C") == 0.8 and _look_set(doc, "C") == []

    L.apply_look(doc, "A", {"width": 0.8}, scope="piece")
    L.apply_look(doc, "C", {"width": 0.6}, scope="piece")
    assert L.chain_value(doc, "C", "width") == L.default("roller", "width")
    L.apply_look(doc, "C", reset=["width"])
    assert _width(doc, "C") == L.default("roller", "width") and _look_set(doc, "C") == []
    assert "width" not in doc.blocks["C"].params["look"]        # the default is not stored


def test_reset_falls_back_to_the_nearest_downstream_value():
    doc = _line("A", "B", "C")
    L.apply_look(doc, "C", {"width": 0.8})          # the whole chain at 0.8
    L.apply_look(doc, "A", {"width": 0.6}, scope="piece")
    L.apply_look(doc, "B", {"width": 0.7}, scope="piece")
    assert L.chain_value(doc, "B", "width") == 0.8   # A is by hand, so C counts
    assert L.chain_value(doc, "A", "width") == 0.8


def test_openings_never_spread():
    doc = _line("A", "B", "C")
    result = L.apply_look(doc, "A", {"openings": [OPENING], "guide_left": 0.1})
    assert result.changed == ["A", "B", "C"]
    assert doc.blocks["A"].params["look"]["openings"] == [OPENING]
    for name in "BC":
        assert "openings" not in doc.blocks[name].params["look"]
    assert [L.look_of(doc.blocks[n].params)["guide_left"] for n in "ABC"] == [0.1] * 3
    assert all(_look_set(doc, n) == [] for n in "ABC")


def test_set_openings_writes_one_piece():
    doc = _line("A", "B")
    L.set_openings(doc, "B", [OPENING])
    assert doc.blocks["B"].params["look"] == {"openings": [OPENING]}
    assert doc.blocks["A"].params["look"] == {}
    L.set_openings(doc, "B", [])
    assert doc.blocks["B"].params["look"] == {}
    with pytest.raises(ValueError):
        L.set_openings(doc, "B", [{"side": "up", "start": 0, "end": 1}])


def test_one_change_is_one_undo_step():
    doc = _line("A", "B", "C")
    before = {n: dict(doc.blocks[n].params) for n in "ABC"}
    depth = len(doc.undo_labels())
    L.apply_look(doc, "B", {"width": 0.7, "frame_color": "blue"})
    assert len(doc.undo_labels()) == depth + 1 and doc.undo_labels()[0] == "Edit look"
    doc.undo()
    # an undo restores the saved form, which leaves an empty look out (I3)
    assert {n: dict(doc.blocks[n].params) for n in "ABC"} \
        == {n: {k: v for k, v in p.items() if k not in ("look", "look_set")}
            for n, p in before.items()}


def test_a_change_that_changes_nothing_adds_no_undo_step():
    doc = _line("A", "B")
    depth = len(doc.undo_labels())
    result = L.apply_look(doc, "A", {"width": L.default("roller", "width")})
    assert result.changed == [] and len(doc.undo_labels()) == depth


@pytest.mark.parametrize("kwargs", [
    {"look": {"width": 0.7}, "scope": "line"},
    {"look": {"belt_kind": "green_pvc"}},
    {"look": {"width": 0}},
    {"look": {"width": 0.7}, "reset": ["width"]},
    {"reset": ["openings"]},
    {"reset": ["belt_kind"]},
    {"look": {"openings": [{"side": "up", "start": 0, "end": 1}]}},
])
def test_refusals_are_value_errors(kwargs):
    doc = _line("A", "B")
    before = doc.to_layout()
    with pytest.raises(ValueError):
        L.apply_look(doc, "A", **kwargs)
    assert doc.to_layout() == before


def test_an_untyped_piece_takes_no_look():
    doc = ModelDocument()
    floor.add_belt_piece(doc, [{"straight": 3.0}], name="U")
    doc.add_block("DISPOSE", (0.0, 0.0), "out")
    for name in ("U", "out"):
        with pytest.raises(ValueError):
            L.apply_look(doc, name, {"width": 0.7})


def test_a_refusal_names_the_field():
    doc = _line("A")
    with pytest.raises(ValueError, match="belt_kind"):
        L.apply_look(doc, "A", {"belt_kind": "green_pvc", "width": 0.7})


def _chain_doc(kind):
    doc = ModelDocument()
    for name in "ABC":
        floor.add_belt_piece(doc, [{"straight": 3.0}], kind=kind, pose=(0.0, 0.0, 0.0), name=name)
    doc.add_link("A", "B")
    doc.add_link("B", "C")
    return doc


def _refused(doc, name, look, scope, *words):
    before = doc.to_layout()
    with pytest.raises(ValueError) as caught:
        L.apply_look(doc, name, look, scope=scope)
    assert all(w in str(caught.value) for w in words)
    assert doc.to_layout() == before              # nothing written, on any piece


def test_a_pitch_under_the_default_diameter_is_refused():
    _refused(_chain_doc("roller"), "A", {"roller_pitch": 0.04}, "piece", "A", "roller_diameter")


def test_a_chain_narrower_than_its_strands_is_refused():
    _refused(_chain_doc("chain"), "A", {"width": 0.5}, "chain", "strands")


def test_a_chain_pitch_against_a_hand_set_diameter_is_refused_naming_that_piece():
    doc = _chain_doc("roller")
    L.apply_look(doc, "B", {"roller_diameter": 0.07}, scope="piece")
    _refused(doc, "A", {"roller_pitch": 0.065}, "chain", "B keeps roller_diameter by hand",
             "change that field on B first")
    # the same write with a diameter that fits is accepted
    L.apply_look(doc, "A", {"roller_pitch": 0.08}, scope="chain")


def test_the_two_fields_together_are_accepted():
    doc = _chain_doc("roller")
    L.apply_look(doc, "A", {"roller_pitch": 0.04, "roller_diameter": 0.03}, scope="chain")
    assert L.look_of(doc.blocks["C"].params)["roller_pitch"] == 0.04


def test_clearing_the_type_drops_the_look_but_an_unknown_type_keeps_it():
    doc = _chain_doc("roller")
    L.apply_look(doc, "A", {"width": 0.7}, scope="piece")
    doc.set_params("A", {**doc.blocks["A"].params, "type": ""})
    assert doc.blocks["A"].params["look"] == {} and doc.blocks["A"].params["look_set"] == []
    L.apply_look(doc, "B", {"width": 0.7}, scope="piece")
    doc.set_params("B", {**doc.blocks["B"].params, "type": "nosuch"})
    assert doc.blocks["B"].params["look"] == {"width": 0.7}


def test_a_piece_snapped_to_another_type_starts_without_its_look():
    doc = _chain_doc("belt")
    L.apply_look(doc, "C", {"width": 0.9}, scope="chain")
    floor.add_belt_piece(doc, [{"straight": 2.0}], kind="roller", name="R",
                         source=next(p for p in floor.piece_ports(doc, "C") if p.name == "out"))
    assert not doc.blocks["R"].params.get("look")


def test_a_height_spread_gives_raised_pieces_default_legs():
    """D1486 and the plan's builder check 3: raising a pallet line gives its pieces the 1.5 m
    leg default, and the piece that keeps leg_spacing by hand keeps its own 1.0 m."""
    doc = ModelDocument()
    _pieces(doc, ("A", "pallet"), ("B", "pallet"), ("C", "pallet"))
    doc.add_link("A", "B")
    doc.add_link("B", "C")
    doc.set_params("B", {**doc.blocks["B"].params, "look": {"leg_spacing": 1.0},
                         "look_set": ["leg_spacing"]})
    L.apply_look(doc, "A", {"height": 0.6})
    looks = {n: L.look_of(doc.blocks[n].params) for n in "ABC"}
    assert {n: look["leg_spacing"] for n, look in looks.items()} == {"A": 1.5, "B": 1.0, "C": 1.5}
    assert {n: look["height"] for n, look in looks.items()} == {"A": 0.6, "B": 0.6, "C": 0.6}
    assert L.chain_value(doc, "B", "leg_spacing") == 1.5          # a reset takes A's default
