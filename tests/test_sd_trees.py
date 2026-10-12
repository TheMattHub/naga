"""Causes/uses trees (PHASE6 S4): `sd_loops.causes`/`sd_loops.uses` walk the same
stock-and-flow dependency graph `sd_loops.loops` builds (`sd_loops.dependency_graph`),
in the reverse direction for causes."""
from nagasim.core import sd_loops


def _sir_layout():
    """Same fixture as `tests/test_sd_loops.py::_sir_layout`: infect drains S
    into I, recover drains I into R."""
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


def _names(children) -> set:
    return {child.name for child in children}


def test_causes_of_infect_are_its_inputs_down_to_the_depth_limit():
    tree = sd_loops.causes(_sir_layout(), "infect", depth=1)
    assert tree.kind == "flow" and tree.name == "infect" and not tree.cycle
    assert _names(tree.children) == {"beta", "S", "I"}
    assert all(child.children == [] for child in tree.children)   # depth exhausted
    assert sd_loops.causes(_sir_layout(), "infect", depth=0).children == []


def test_uses_of_I_are_the_flows_that_read_it():
    tree = sd_loops.uses(_sir_layout(), "I", depth=1)
    assert tree.kind == "level"
    assert _names(tree.children) == {"infect", "recover"}


def test_cycle_back_to_an_ancestor_is_marked_not_expanded():
    """Causes of infect -> I (a level infect fills) -> infect again (the flow
    that fills I): the second `infect` closes a loop back to the root and
    must stop there instead of recursing forever."""
    tree = sd_loops.causes(_sir_layout(), "infect", depth=5)
    i_node = next(c for c in tree.children if c.name == "I")
    inner_infect = next(c for c in i_node.children if c.name == "infect")
    assert inner_infect.cycle
    assert inner_infect.children == []


def test_unknown_name_returns_none():
    assert sd_loops.causes(_sir_layout(), "nope") is None
    assert sd_loops.uses(_sir_layout(), "nope") is None
