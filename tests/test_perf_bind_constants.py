"""7.3 (performance report 2026-10-07): a flow's submodel parameters are folded into
its compiled rate. The binding is cached by text AND values, so the same text with
other arguments -- another instance, another model in the process -- keeps its own."""
import pytest

from nagasim.core.expressions import (ExpressionError, ExpressionScope, bind_constants,
                                      compile_expression)
from conftest import chain, create, dispose, layout as make_layout, run_env

_NAMES = {"k", "bus", "n", "z", "p", "now"}


def _value(text, constants):
    """Evaluated WITHOUT locals in the scope: only a folded name can answer."""
    return bind_constants(text, _NAMES, constants).evaluate(ExpressionScope())


def test_same_text_bound_to_other_values_keeps_its_own_in_both_orders():
    text = '"bus" + bus if k * 2 > 5 else k'
    first, second = {"k": 2.0, "bus": "_a"}, {"k": 3.0, "bus": "_b"}
    for order in ((first, second), (second, first), (first, second)):
        assert [_value(text, c) for c in order] == [2.0 if c is first else "bus_b" for c in order]
    # The shared compiled expression is untouched: it still reads the scope.
    shared = compile_expression(text, _NAMES)
    assert shared.evaluate(ExpressionScope(locals={"k": 9.0, "bus": "_z"})) == "bus_z"


def test_equal_but_different_constants_do_not_share_a_binding():
    assert [type(_value("n + 0", {"n": n})) for n in (1, 1.0, True, 1)] == [int, float, int, int]
    assert [repr(_value("z * 1.0", {"z": z})) for z in (0.0, -0.0, 0.0)] == ["0.0", "-0.0", "0.0"]


def test_a_folded_bus_name_is_a_literal_read():
    bound = bind_constants('FLOWSUM("bus" + bus, n) * k', _NAMES, {"bus": "_a", "n": 2, "k": 1.5})
    assert bound.bus_reads == frozenset({("tag", "bus_a")})
    assert compile_expression('FLOWSUM("bus" + bus, n) * k', _NAMES).bus_reads is None
    assert bound.evaluate(ExpressionScope()) == 0.0        # no run: a bus with nothing on it


def test_what_is_not_folded_still_answers_or_fails_at_evaluation():
    # A failing fold and the guarded operators are left for the evaluation to report.
    for text, constants in (("1 / z", {"z": 0}), ("2 ** p", {"p": 10 ** 9}),
                            ('"ab" * p', {"p": 10 ** 9})):
        bound = bind_constants(text, _NAMES, constants)
        with pytest.raises(ExpressionError):
            bound.evaluate(ExpressionScope(locals=constants))
    # A reserved name is never a parameter; a value that is not a plain literal stays a lookup.
    assert bind_constants("now", _NAMES, {"now": 5}) is compile_expression("now", _NAMES)
    listed = {"k": [1, 2]}
    assert bind_constants("k", _NAMES, listed).evaluate(ExpressionScope(locals=listed)) == [1, 2]


def _plant(a, b):
    """Two instances of one asset, as `submodels.flatten` leaves them: the same
    rate texts, each flow carrying its instance's arguments as `locals`."""
    c, d = create(max_arrivals=1), dispose()
    flows = []
    for name, args in (("a", a), ("b", b)):
        flows += [{"name": f"{name}/src", "rate": "k", "tag": f"bus_{name}", "locals": dict(args)},
                  {"name": f"{name}/read", "rate": 'FLOWSUM("bus" + bus) * k',
                   "locals": dict(args)}]
    return make_layout([c, d], chain(c, d), flows=flows)


def _reads(layout):
    env, _model = run_env(layout, 2.0)
    system = env.ctx.level_system
    assert {flow.name: reads for flow, _fn, _scope, reads in system._rates
            if flow.name.endswith("read")} == {
        f"{name}/read": frozenset({("tag", "bus" + flow["locals"]["bus"])})
        for name, flow in (("a", layout["flows"][1]), ("b", layout["flows"][3]))}
    return {name: flow.actual for name, flow in system.flow_index.items()}


def test_two_models_with_the_same_rate_texts_in_one_process_in_both_orders():
    one = _plant({"k": 2.0, "bus": "_a"}, {"k": 5.0, "bus": "_b"})
    two = _plant({"k": 3.0, "bus": "_a"}, {"k": 7.0, "bus": "_a"})
    want_one = {"a/src": 2.0, "a/read": 4.0, "b/src": 5.0, "b/read": 25.0}
    want_two = {"a/src": 3.0, "a/read": 9.0, "b/src": 7.0, "b/read": 21.0}
    for layout, want in ((one, want_one), (two, want_two), (one, want_one)):
        assert _reads(layout) == want
