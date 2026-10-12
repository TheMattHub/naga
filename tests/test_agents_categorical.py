"""PHASE6 A2: categorical agent attributes (`core/agents.py`, ARCHITECTURE
5.25). A category is stored as its index in the SAME float column a numeric
attribute uses (codes are exact in float64); `self.<attr>` reads back the
NAME, so a plain `==`/`!=`/`in` comparison works through the general
evaluator with no new grammar. Hand-derived, cheap models throughout.
"""
import json

import numpy as np
import pytest

from conftest import problem_codes, run_env
from nagasim.core import agents as A
from nagasim.core.schema import migrate
from nagasim.core.sim_runner import run_single_simulation
from nagasim.experiments import flatten_kpis


def _model(*records, time_limit=20):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _run(layout, seed=1):
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env.ctx.agent_system


def _codes(layout):
    return problem_codes(layout)


CATS = ["red", "blue", "green"]


# ---------------------------------------------------------------- init
def test_init_from_a_literal_and_from_a_table_column():
    literal = A.AgentType("Person", states=["S"], initial="S",
                          attributes=[A.Attribute("colour", init='"blue"', kind="category",
                                                  categories=CATS)])
    layout = _model(literal, A.Population("People", "Person", 4))
    pop = _run(layout).by_name["People"]
    assert list(pop.attrs["colour"]) == [1.0, 1.0, 1.0, 1.0]      # "blue" is code 1
    assert [pop.read("colour", i) for i in range(4)] == ["blue"] * 4

    # Tables stay numeric-only (the same contract TABLE()/schedules rely on):
    # a category column holds the CODE (0-based index into "categories"), not
    # the name -- [0, 2, 1] below is [red, green, blue].
    from_table = A.AgentType("Person", states=["S"], initial="S",
                             attributes=[A.Attribute("colour", init={"column": "colour"},
                                                     kind="category", categories=CATS)])
    layout2 = _model(from_table, A.Population("People", "Person", count={"table": "People"}))
    layout2["tables"] = [{"name": "People", "keys": [0, 1, 2], "columns": ["colour"],
                          "values": [[0], [2], [1]]}]
    pop2 = _run(layout2).by_name["People"]
    assert [pop2.read("colour", i) for i in range(3)] == ["red", "green", "blue"]


# ---------------------------------------------------------------- comparisons
def _traffic_light():
    """Own condition (S -> I on colour == "red"), a guard using `in`, and a
    "message" transition whose action sets from another categorical attribute
    of the same categories."""
    return A.AgentType(
        "Person", states=["S", "I", "R"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS),
                   A.Attribute("target", init='"green"', kind="category", categories=CATS)],
        transitions=[
            A.Transition("S", "I", "condition", 'self.colour == "red"'),
            A.Transition("I", "R", "condition", "true", guard='self.colour in ("red", "blue")',
                        actions=[A.Action.set("self.colour", "self.target")])])


def test_comparison_in_guard_and_condition_is_exact():
    pop = _run(_model(_traffic_light(), A.Population("People", "Person", 5))).by_name["People"]
    # Every agent starts "red": the S->I condition fires for all of them, the
    # guard ("red" or "blue") holds too, so every agent ends in R with colour
    # set from its own "target" attribute ("green").
    assert list(pop.state) == [pop.type.index["R"]] * 5
    assert [pop.read("colour", i) for i in range(5)] == ["green"] * 5


def test_not_equal_and_state_literal_still_work_together():
    kind = A.AgentType(
        "Person", states=["S", "I"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "I", "condition", 'self.colour != "blue" and self.state != "I"')])
    pop = _run(_model(kind, A.Population("People", "Person", 3))).by_name["People"]
    assert list(pop.state) == [pop.type.index["I"]] * 3


# ---------------------------------------------------------------- AB-4 vectorised path
def test_numpy_path_is_bit_identical_to_the_forced_loop(monkeypatch):
    def snapshot():
        pop = _run(_model(_traffic_light(), A.Population("People", "Person", 30))).by_name["People"]
        return (pop.state.tolist(), {k: v.tolist() for k, v in pop.attrs.items()})

    fast = snapshot()
    monkeypatch.setattr(A.compile, "vectorise", lambda *args, **kwargs: None)
    assert snapshot() == fast


def test_the_eq_literal_comparison_vectorises_as_a_code_comparison():
    fn = A.vectorise('self.colour == "blue"', names=set(), cats={"colour": CATS})
    assert fn is not None

    class _Pop:
        front = None

        def view(self):
            return None, {"colour": np.array([0.0, 1.0, 2.0, 1.0])}

    hit = fn(_Pop(), np.arange(4), None)
    assert hit.tolist() == [False, True, False, True]
    # An undeclared literal matches no code, the same rule self.state uses.
    fn2 = A.vectorise('self.colour == "purple"', names=set(), cats={"colour": CATS})
    assert fn2(_Pop(), np.arange(4), None).tolist() == [False, False, False, False]


# ---------------------------------------------------------------- validation
_BASE_TYPE = {"name": "Person", "states": [{"name": "S"}], "initial_state": "S"}
_BASE_POP = {"name": "People", "type": "Person", "count": 3}


def _layout(attributes=(), pop_extra=None, transitions=()):
    kind = dict(_BASE_TYPE, attributes=list(attributes), transitions=list(transitions))
    pop = dict(_BASE_POP, **(pop_extra or {}))
    return {"blocks": [], "links": [], "agent_types": [kind], "populations": [pop],
           "simulation": {"time_limit": 5}}


@pytest.mark.parametrize("attributes, pop_extra, transitions, code", [
    ([{"name": "c", "kind": "bogus"}], None, (), "E_AGENT_ATTRIBUTE"),           # bad kind
    ([{"name": "c", "kind": "category", "categories": ["red"]}], None, (),       # < 2 categories
     "E_AGENT_ATTRIBUTE"),
    ([{"name": "c", "kind": "category", "categories": ["red", "red"]}], None, (),  # duplicate
     "E_AGENT_ATTRIBUTE"),
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"green"'}],
     None, (), "E_AGENT_ATTRIBUTE"),                                             # unknown literal
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}],
     None, [{"from": "S", "to": "S", "condition": 'SUM("People", "c") > 0'}],
     "E_AGENT_ATTRIBUTE"),                                                       # SUM of a category
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}],
     None, [{"from": "S", "to": "S", "condition": 'COUNT("People", "nope", "red") > 0'}],
     "E_AGENT_ATTRIBUTE"),                            # 3-arg (pop, attr, value): unknown attr
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}],
     None, [{"from": "S", "to": "S", "condition": 'COUNT("People", "S", "c", "green") > 0'}],
     "E_AGENT_ATTRIBUTE"),                            # 4-arg (pop, state, attr, value): undeclared value
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}],
     None, [{"from": "S", "to": "S", "condition": 'COUNT("People", "nope", "c", "red") > 0'}],
     "E_AGENT_STATE"),                                # 4-arg: undeclared state
    ([{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}],
     {"color_by": "nope"}, (), "E_AGENT_ATTRIBUTE"),                             # bad color_by
    ([{"name": "c", "init": "0"}], {"color_by": "c"}, (), "E_AGENT_ATTRIBUTE"),  # color_by numeric
])
def test_every_category_problem_code(attributes, pop_extra, transitions, code):
    assert code in _codes(_layout(attributes, pop_extra, transitions))


def test_a_valid_categorical_model_has_no_problems():
    attrs = [{"name": "c", "kind": "category", "categories": ["red", "blue"], "init": '"red"'}]
    assert not _codes(_layout(attrs, {"color_by": "c", "colors": {"red": "RED"}},
                              [{"from": "S", "to": "S",
                               "condition": 'COUNT("People", "S", "c", "red") >= 0'}]))


def test_set_from_another_categorical_attribute_of_the_same_categories():
    kind = A.AgentType(
        "Person", states=["S", "I"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS),
                   A.Attribute("target", init='"blue"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "I", "condition", "true",
                                  actions=[A.Action.set("self.colour", "self.target")])])
    pop = _run(_model(kind, A.Population("People", "Person", 4))).by_name["People"]
    assert [pop.read("colour", i) for i in range(4)] == ["blue"] * 4


def test_arithmetic_on_a_category_raises_at_run_time():
    """No compile-time type inference across arbitrary expressions (a
    deliberate scope cut, PHASE6 A2): a category used as a number fails when
    it is actually evaluated, with a clear ExpressionError."""
    from nagasim.core.expressions import ExpressionError
    kind = A.AgentType(
        "Person", states=["S"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "S", "condition", "self.colour + 1 > 0")])
    with pytest.raises(ExpressionError):
        _run(_model(kind, A.Population("People", "Person", 1)))


def test_count_category_filter_combined_with_state():
    kind = A.AgentType(
        "Person", states=["S", "I"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "I", "condition", "self.id < 2")])
    system = _run(_model(kind, A.Population("People", "Person", 5)))
    assert system.count("People", "I", "colour", "red") == 2
    assert system.count("People", "S", "colour", "red") == 3
    assert system.count("People", attr="colour", value="red") == 5


def test_count_expression_dispatches_by_arg_count_not_position():
    """COUNT(pop, attr, value) (3 args, no state) and COUNT(pop, state, attr,
    value) (4 args) through the grammar itself, not the Python method."""
    from nagasim.core.expressions import compile_expression, ExpressionScope

    kind = A.AgentType(
        "Person", states=["S", "I"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "I", "condition", "self.id < 2")])
    system = _run(_model(kind, A.Population("People", "Person", 5)))
    scope = ExpressionScope(ctx=system.env.ctx)
    no_state = compile_expression('COUNT("People", "colour", "red")').closure()
    with_state = compile_expression('COUNT("People", "S", "colour", "red")').closure()
    assert no_state(scope) == 5
    assert with_state(scope) == 3


# ---------------------------------------------------------------- outputs
def test_agent_attributes_reports_exact_category_shares():
    kind = A.AgentType(
        "Person", states=["S", "I"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)],
        transitions=[A.Transition("S", "I", "condition", "self.id < 3",
                                  actions=[A.Action.set("self.colour", '"blue"')])])
    layout = _model(kind, A.Population("People", "Person", 5))
    results = run_single_simulation(layout, seed=0, run_len=5, warmup=0)
    frame = results["agent_attributes"].set_index("Agent Attribute")

    assert frame.loc["People/colour=red/All", "End Mean"] == pytest.approx(2 / 5)
    assert frame.loc["People/colour=blue/All", "End Mean"] == pytest.approx(3 / 5)
    assert frame.loc["People/colour=red/All", "Category"] == "red"
    assert np.isnan(frame.loc["People/colour=red/All", "End P50"])       # no percentile
    assert np.isnan(frame.loc["People/colour=red/All", "Time-Avg Mean"])  # numeric-only (C2)

    log = results["agent_attribute_log"]
    assert sorted(log["Value"]) == ["blue", "blue", "blue", "red", "red"]

    kpi = flatten_kpis(results)
    row = kpi[kpi["KPI"] == "agent/People/colour=blue/All/End Mean"]
    assert row["Value"].iloc[0] == pytest.approx(3 / 5)


def test_agent_attributes_frame_is_unchanged_for_a_numeric_only_population():
    """No categorical attribute -> no "Category" content and the same numeric
    stats PHASE6 C2 always reported (bit-identity of the existing behaviour)."""
    kind = A.AgentType("Person", states=["S"], initial="S", attributes=[A.Attribute("x", "5")])
    layout = _model(kind, A.Population("People", "Person", 2))
    results = run_single_simulation(layout, seed=0, run_len=5, warmup=0)
    frame = results["agent_attributes"].set_index("Agent Attribute")
    row = frame.loc["People/x/All"]
    assert row["End Mean"] == pytest.approx(5.0)
    assert row["Category"] == ""


# ---------------------------------------------------------------- schema / round trip
def test_json_schema_accepts_a_categorical_attribute_and_color_by():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    kind = A.AgentType(
        "Person", states=["S"], initial="S",
        attributes=[A.Attribute("colour", init='"red"', kind="category", categories=CATS)])
    layout = _model(kind, A.Population("People", "Person", 3,
                                       color_by="colour", colors={"red": "RED"}))
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(migrate(layout)))
    assert not errors, errors[0].message


def test_attribute_and_population_round_trip_through_json():
    attribute = A.Attribute.from_dict(json.loads(json.dumps(
        A.Attribute("colour", init='"red"', kind="category", categories=CATS).to_dict())))
    assert attribute.kind == "category"
    assert attribute.categories == CATS
    numeric = A.Attribute.from_dict(json.loads(json.dumps(A.Attribute("x", "0").to_dict())))
    assert "kind" not in numeric.to_dict() and "categories" not in numeric.to_dict()

    population = A.Population.from_dict(json.loads(json.dumps(
        A.Population("People", "Person", 3, color_by="colour",
                    colors={"red": "RED"}).to_dict())))
    assert population.to_dict()["color_by"] == "colour"
    assert population.to_dict()["colors"] == {"red": "RED"}
