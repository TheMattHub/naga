"""Conveyor plan B Task 4: the open-port rule without the other checks (`conveyor_checks.open_ports`,
`floor.open_ports`), so the editor indexes a gesture's ports with no `problems()` run.

Layouts are built with `floor.add_belt_piece` / `add_transfer_piece` (one rule with the agent
tools); expected ports are read by hand from each layout."""
from nagasim.core import conveyor_checks
from nagasim.core import floor as D
from nagasim.core.document import ModelDocument


def _line():
    """A (0,0) 2 m -> B snapped to A.out, 3 m -> transfer X fed by B with exits out and side;
    A diverts at 1 m to the left (d0). Open: A.in, A.d0, X.out, X.side."""
    doc = ModelDocument()
    a = D.add_belt_piece(doc, [{"straight": 2.0}], pose=(0.0, 0.0, 0.0), name="A")
    params = dict(doc.blocks[a].params)
    params["diverts"] = [{"at": 1.0, "side": "left", "condition": "True"}]
    doc.set_params(a, params)
    a_out = next(p for p in D.piece_ports(doc, a) if p.name == "out")
    b = D.add_belt_piece(doc, [{"straight": 3.0}], source=a_out, name="B")
    b_out = next(p for p in D.piece_ports(doc, b) if p.name == "out")
    D.add_transfer_piece(doc, exits=[{"port": "out", "angle": 0}, {"port": "side", "angle": 90}],
                         source=b_out, name="X")
    return doc


def _problem_pairs(doc):
    return [(p.block, p.fields["port"]) for p in conveyor_checks.static_checks(doc.to_layout())
            if p.code == "W_CONVEYOR_OPEN_PORT"]


def test_open_ports_are_the_open_port_problems_in_order():
    doc = _line()
    pairs = conveyor_checks.open_ports(doc.to_layout())
    assert pairs == _problem_pairs(doc)
    assert set(pairs) == {("A", "in"), ("A", "d0"), ("X", "out"), ("X", "side")}


def test_no_floor_piece_has_no_open_port():
    doc = ModelDocument()
    doc.add_block("CONVEYOR", (0, 0))          # a legacy belt: no path, not on the floor
    assert conveyor_checks.open_ports(doc.to_layout()) == []
    assert conveyor_checks.open_ports({}) == []
    assert D.open_ports(doc) == []


def test_document_open_ports_are_laid_out_by_piece_ports(monkeypatch):
    doc = _line()
    # The point of the helper: no full validation behind it.
    monkeypatch.setattr(ModelDocument, "problems",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("problems()")))
    found = D.open_ports(doc)
    assert {(p.block, p.name) for p in found} == {("A", "in"), ("A", "d0"), ("X", "out"),
                                                  ("X", "side")}
    laid = {(p.block, p.name): p for name in ("A", "X") for p in D.piece_ports(doc, name)}
    for port in found:
        assert laid[(port.block, port.name)] == port


def test_a_malformed_path_is_skipped_not_raised():
    doc = _line()
    params = dict(doc.blocks["B"].params)
    params["path"] = [{"straight": -1}]
    doc.set_params("B", params)
    names = {(p.block, p.name) for p in D.open_ports(doc)}
    assert ("B", "out") not in names and ("A", "in") in names
