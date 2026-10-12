"""Loop listing (PHASE6 C6, docs/history/PHASE5-CLOSING-REVIEW-2026-09-24.md
Part C): elementary circuits on the stock-and-flow graph, polarity by a
finite-difference probe at the model's initial state, and the enumeration cap.
"""
from nagasim.core import sd_loops


def _sir_layout():
    """The same fixture as `tests/test_ui_sdview.py::_sir_document` (kept as
    a plain layout here, no Qt): infect drains S into I, recover drains I
    into R -- the textbook two-loop-plus-depletion structure."""
    return {
        "levels": [{"name": "S", "initial": 990, "min": 0},
                   {"name": "I", "initial": 10, "min": 0},
                   {"name": "R", "initial": 0, "min": 0}],
        "auxiliaries": [{"name": "beta", "expr": "0.3"}, {"name": "gamma", "expr": "0.1"}],
        "flows": [
            {"name": "infect", "rate": 'AUX("beta")*LEVEL("S")*LEVEL("I")/1000',
             "from": "S", "to": "I"},
            {"name": "recover", "rate": 'AUX("gamma")*LEVEL("I")', "from": "I", "to": "R"},
        ],
    }


def _goal_seeking_layout():
    """A single stock adjusted towards a fixed goal: the textbook one-loop
    balancing structure (Stock -> Gap -> adjust -> Stock)."""
    return {
        "levels": [{"name": "Stock", "initial": 0.0, "min": None}],
        "auxiliaries": [{"name": "Goal", "expr": "100"},
                        {"name": "Gap", "expr": 'AUX("Goal") - LEVEL("Stock")'}],
        "flows": [{"name": "adjust", "rate": 'AUX("Gap")/5', "to": "Stock"}],
    }


def test_sir_loops_match_the_textbook_structure():
    found, truncated = sd_loops.loops(_sir_layout())
    assert not truncated
    by_id = {loop.id: loop for loop in found}
    assert sorted(by_id) == ["B1", "B2", "R1"]

    # R1: the reinforcing infection loop, through I and the infect flow.
    r1 = by_id["R1"]
    assert r1.length == 2
    assert set(r1.nodes) == {("flows", "infect"), ("levels", "I")}

    # B1/B2: susceptible depletion (via S) and recovery (via I) both balance,
    # each a 2-node loop through its own flow.
    balancing_nodes = {frozenset(by_id["B1"].nodes), frozenset(by_id["B2"].nodes)}
    assert balancing_nodes == {frozenset({("flows", "infect"), ("levels", "S")}),
                               frozenset({("flows", "recover"), ("levels", "I")})}


def test_goal_seeking_model_has_exactly_one_balancing_loop():
    found, truncated = sd_loops.loops(_goal_seeking_layout())
    assert not truncated
    assert len(found) == 1
    loop = found[0]
    assert loop.kind == "B"
    assert loop.id == "B1"
    assert set(loop.nodes) == {("levels", "Stock"), ("auxiliaries", "Gap"), ("flows", "adjust")}


def test_a_model_with_no_flows_has_no_loops():
    found, truncated = sd_loops.loops({"levels": [{"name": "L", "initial": 1.0}]})
    assert found == []
    assert not truncated


def test_enumeration_is_capped_with_a_truncation_note():
    """Four stocks, every ordered pair joined by its own flow (12 flows):
    combinatorially many elementary circuits through the level/flow graph --
    comfortably more than a small `limit`, and a valid model (unlike a
    cyclic auxiliary graph, which `validate_structured` refuses outright, so
    that is not a case `loops` needs to survive)."""
    names = ["S0", "S1", "S2", "S3"]
    levels = [{"name": n, "initial": 10.0, "min": None} for n in names]
    flows = [{"name": f"f_{i}_{j}", "rate": f'LEVEL("{i}")*0.01', "from": i, "to": j}
            for i in names for j in names if i != j]
    layout = {"levels": levels, "flows": flows}

    found, truncated = sd_loops.loops(layout, limit=5)
    assert truncated
    assert len(found) == 5

    # Raising the cap on the very same model finds strictly more loops.
    found_more, truncated_more = sd_loops.loops(layout, limit=200)
    assert not truncated_more
    assert len(found_more) > 5
