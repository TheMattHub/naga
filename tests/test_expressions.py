"""Safe expression language, model variables and attribute arithmetic
(ROADMAP 1.1; closes DEVLOG #53, #54)."""
import numpy as np
import pytest

from nagasim.core.blocks import Assign, Decide, Dispose
from nagasim.core.base_process import Block
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import (
    ExpressionError, ExpressionScope, compile_expression, parse_target,
)
from nagasim.core.sim_classes import Entity
from nagasim.core.sim_runner import initialize_sim_env

from conftest import run_layout


def scope(entity=None, ctx=None, seed=0):
    return ExpressionScope(entity=entity, ctx=ctx, rng=np.random.default_rng(seed))


def value(text, entity=None, ctx=None, seed=0):
    return compile_expression(text).evaluate(scope(entity, ctx, seed))


# -- the grammar the documentation promises ---------------------------------

ACCEPTED = [
    "1", "-2.5", "'text'", "true", "false", "None",
    "entity.priority", "entity.priority + 1", "2 * entity.size - 1",
    "7 // 2", "7 % 2", "2 ** 8", "not true", "+1",
    "entity.a > 1 and entity.b <= 2 or false",
    "1 < 2 < 3",
    "entity.e_type in ('A', 'B')", "entity.e_type not in ['A']",
    "1 if entity.flag else 2",
    "min(1, 2, 3)", "max(1, 2)", "abs(-1)", "round(1.55, 1)", "int(1.7)", "float(1)",
    "sqrt(4)", "exp(0)", "log(1)", "log(8, 2)", "log10(100)", "floor(1.7)", "ceil(1.2)",
    "RAND()", "EXPO(2)", "UNIF(0, 1)", "NORM(0, 1)", "TRIA(0, 1, 2)", "DISC(1, 0.5, 2, 0.5)",
    "NQ('Server')", "NR('Server')", "MR('Server')", "NS('buffer')",
    "TNOW", "now + 1", "counter * 2",
]


def test_allow_list_accepts_the_documented_grammar():
    """Table-driven rather than parametrized: ~40 one-line cases are dominated
    by pytest's per-test overhead, and one failure message listing every
    rejected form beats fixing them one collection at a time."""
    broken = []
    for text in ACCEPTED:
        try:
            compile_expression(text)
        except ExpressionError as exc:
            broken.append(f"{text!r}: {exc}")
    assert not broken, "the documented grammar no longer compiles:\n  " + "\n  ".join(broken)


REJECTED = [
    ("__import__('os')", "unknown function"),
    ("entity.__class__", "not accessible"),
    ("entity._id", "not accessible"),
    ("().__class__", "entity.<attribute>"),
    ("counter.bit_length()", "only calls to the built-in"),
    ("lambda: 1", "Lambda"),
    ("[x for x in (1, 2)]", "ListComp"),
    ("entity.tags[0]", "Subscript"),
    ("f'{entity.e_type}'", "JoinedStr"),
    ("(x := 1)", "NamedExpr"),
    ("nosuchfunction(1)", "unknown function"),
    ("round(1, ndigits=2)", "keyword arguments"),
    ("1 & 2", "BitAnd"),
    ("'a' in entity.e_type", "tuple or list literal"),
]


def test_allow_list_rejects_attacks_and_junk():
    escaped = []
    for text, fragment in REJECTED:
        try:
            compile_expression(text)
        except ExpressionError as exc:
            if fragment not in str(exc):
                escaped.append(f"{text!r}: expected {fragment!r} in {exc}")
        else:
            escaped.append(f"{text!r}: compiled instead of raising")
    assert not escaped, "the allow-list let something through:\n  " + "\n  ".join(escaped)


def test_syntax_error_is_an_expression_error_with_a_column():
    with pytest.raises(ExpressionError) as excinfo:
        compile_expression("1 +")
    assert excinfo.value.col is not None


def test_deep_nesting_is_an_expression_error_not_a_recursion_error():
    """(#332) The parser and the checker recurse once per level: an expression from a
    model file or an agent can be nested past Python's limit."""
    # The last two overflow the parser's own stack, which is a MemoryError, not a RecursionError.
    for text in ("1+" * 3000 + "1", "not " * 2000 + "1", "-" * 5000 + "1",
                 "-" * 7000 + "1", "1 if 1 else " * 7000 + "1"):
        with pytest.raises(ExpressionError, match="nested too deeply"):
            compile_expression(text)


def test_runaway_expressions_are_stopped_at_evaluation():
    """Compile-time legal, so the guard has to be at evaluation (exponent and
    sequence-repetition caps)."""
    for text in ("9 ** 9 ** 9", "'a' * 10 ** 9"):
        compile_expression(text)
        with pytest.raises(ExpressionError, match="exceeds the limit"):
            value(text)


# -- semantics --------------------------------------------------------------

SEMANTICS = [
    ("2 + 3 * 4", 14), ("(2 + 3) * 4", 20), ("7 / 2", 3.5), ("7 // 2", 3), ("7 % 2", 1),
    ("2 ** 10", 1024), ("-(2 + 3)", -5), ("not false", True),
    ("true and false", False), ("true or false", True),
    ("1 < 2 < 3", True), ("1 < 2 < 0", False),
    ("2 in (1, 2, 3)", True), ("4 not in [1, 2]", True),
    ("10 if 1 > 0 else 20", 10), ("10 if 1 < 0 else 20", 20),
    ("min(3, 1, 2)", 1), ("round(2.345, 2)", 2.35), ("floor(-1.2)", -2),
]


def test_arithmetic_boolean_and_compare_semantics():
    wrong = [f"{text!r} -> {value(text)!r}, expected {expected!r}"
             for text, expected in SEMANTICS if value(text) != expected]
    assert not wrong, "\n  ".join([""] + wrong)


def test_entity_attributes_are_readable_and_missing_ones_name_themselves():
    entity = Entity()
    entity.size = 4
    assert value("2 * entity.size + 1", entity=entity) == 9
    assert value("entity.e_type == 'DEFAULT'", entity=entity) is True
    with pytest.raises(ExpressionError, match="entity has no attribute 'missing'"):
        value("entity.missing", entity=entity)


def test_names_expose_what_an_expression_reads():
    expression = compile_expression("counter + entity.size + max(1, 2)")
    assert expression.names == {"counter", "entity"}


def test_unknown_name_is_a_compile_error_when_variables_are_supplied():
    compile_expression("counter + 1", variables={"counter"})
    with pytest.raises(ExpressionError, match="unknown name 'countr'"):
        compile_expression("countr + 1", variables={"counter"})
    # without the variable set, the failure waits until evaluation
    compile_expression("countr + 1")
    with pytest.raises(ExpressionError, match="unknown name 'countr'"):
        value("countr + 1")


def test_parse_target_splits_entity_attributes_from_variables_and_refuses_junk():
    assert parse_target("entity.priority") == ("entity", "priority")
    assert parse_target("counter") == ("variable", "counter")
    for text in ("entity._id", "entity.", "2counter", "now"):
        with pytest.raises(ExpressionError):
            parse_target(text)


# -- sampling functions ------------------------------------------------------

def test_sampling_functions_are_reproducible_per_seed():
    for text in ("RAND()", "EXPO(2)", "UNIF(1, 3)", "NORM(0, 1)", "TRIA(0, 1, 2)"):
        assert value(text, seed=1) == value(text, seed=1), text
        assert value(text, seed=1) != value(text, seed=2), text


def test_disc_returns_a_listed_value_and_validates_probabilities():
    draws = {value("DISC(1, 0.3, 2, 0.7)", seed=s) for s in range(20)}
    assert draws <= {1, 2} and len(draws) == 2
    with pytest.raises(ExpressionError, match="sum to"):
        value("DISC(1, 0.3, 2, 0.3)")
    with pytest.raises(ExpressionError, match=">= 0"):
        value("DISC(1, -0.5, 2, 1.5)")


# -- live model state --------------------------------------------------------

def test_nq_nr_mr_and_ns_read_live_state(env):
    resource = env.ctx.resource_manager.register("Server", capacity=2)

    def occupy():
        request = resource.request()
        yield request
        yield env.timeout(10)
        resource.release(request)

    for _ in range(3):      # 2 served, 1 queued
        env.process(occupy())
    store = env.ctx.store_manager.register("hold_1_hold")
    store.put(Entity())
    env.run(until=1)

    ctx = env.ctx
    assert value("NR('Server')", ctx=ctx) == 2
    assert value("NQ('Server')", ctx=ctx) == 1
    assert value("MR('Server')", ctx=ctx) == 2
    assert value("NS('hold_1')", ctx=ctx) == 1        # block name, store is '<name>_hold'
    assert value("TNOW", ctx=ctx) == 1
    with pytest.raises(ExpressionError, match="unknown resource"):
        value("NQ('Nope')", ctx=ctx)


# -- Assign block ------------------------------------------------------------

def test_assign_runs_in_order_and_writes_entities_and_variables(env):
    env.ctx.variables = {"counter": 0}
    assign = Assign(env, Block(position=(0, 0), type="ASSIGN", name="assign1"), assignments=[
        {"target": "entity.size", "expr": "2"},
        {"target": "entity.size", "expr": "entity.size * 5"},     # sees the line above
        {"target": "counter", "expr": "counter + entity.size"},
        {"target": "entity.e_type", "expr": "'b'"},
    ])
    dispose = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose1"))
    assign.next_process = [dispose]

    entity = Entity()
    entity.creation_time = 0
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)
    assign.self_run(entity)

    assert entity.size == 10
    assert env.ctx.variables["counter"] == 10
    assert entity.e_type == "B"          # e_type keeps its upper-casing setter


def test_assign_failure_names_the_block_and_the_assignment(env):
    assign = Assign(env, Block(position=(0, 0), type="ASSIGN", name="assign1"),
                    assignments=[{"target": "entity.x", "expr": "entity.nope + 1"}])
    with pytest.raises(ExpressionError, match="Assign block 'assign1'"):
        assign.self_run(Entity())


def test_decide_condition_routes_on_a_legacy_condition(env):
    decide = Decide(env, Block(position=(0, 0), type="DECIDE", name="decide1"),
                    mode="Condition", conditions=['entity.e_type == "A"'])
    matched = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose_a"))
    other = Dispose(env, Block(position=(2, 0), type="DISPOSE", name="dispose_else"))
    decide.next_process = [matched, other]

    for e_type in ("A", "B"):
        entity = Entity()
        entity.e_type = e_type
        entity.creation_time = 0
        env.ctx.all_entities.append(entity)
        env.ctx.live_entities.append(entity)
        decide.self_run(entity)
    env.run(until=5)

    assert (matched.disposed_entities, other.disposed_entities) == (1, 1)


# -- distributions -----------------------------------------------------------

def test_distribution_parameter_may_be_an_expression():
    entity = Entity()
    entity.size = 3
    dist = StochasticDistribution("Constant", {"value": "2 * entity.size"})
    assert dist.sample(np.random.default_rng(0), scope=scope(entity=entity)) == 6


def test_custom_expression_distribution_uses_the_expression_engine():
    dist = StochasticDistribution("CustomExpression", {"expression": "1 + UNIF(0, 1)"})
    first = dist.sample(np.random.default_rng(3))
    assert 1 <= first <= 2
    assert dist.sample(np.random.default_rng(3)) == first
    bad = StochasticDistribution("CustomExpression", {"expression": "random.gauss(5, 1)"})
    with pytest.raises(ExpressionError):
        bad.sample(np.random.default_rng(0))


# -- model variables end to end ---------------------------------------------

def _counting_model(make_model):
    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                    "entities_per_arrival": 1, "max_arrivals": 3, "start_time": 0}},
        {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [1, 0],
         "params": {"assignments": [{"target": "counter", "expr": "counter + 1"}]}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "assign_1"], ["assign_1", "dispose_1"]]
    return make_model(blocks, links, variables=[{"name": "counter", "initial": 0}])


def test_model_variables_start_from_their_initial_value_every_replication(make_model):
    path = _counting_model(make_model)
    for _ in range(2):
        env = initialize_sim_env(seed=0)
        run_layout(env, path, run_len=10)
        assert env.ctx.variables == {"counter": 3}   # never 6: each run starts at 0
