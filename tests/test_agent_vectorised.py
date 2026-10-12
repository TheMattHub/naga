"""PHASE5 AB-4: vectorised agent paths and perf guards (`core/agents.py`,
ARCHITECTURE 5.25). The numpy paths (own conditions, `where` sends, attribute
inits) must be bit-identical to the per-agent loop they replace: every
comparison below is exact equality of whole columns and summaries."""
import time

import numpy as np
import pytest

from nagasim.core import agents as A
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from conftest import problem_codes, run_env


def _model(*records, time_limit=100):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _run(layout, seed=1):
    # Was hand-rolled without the warm start/level start/sampler `run_env`
    # covers (docs/history/ARCH-REVIEW-PLAN-2026-09-27.md D1) -- harmless drift
    # here since these models have no levels/resources/timeseries, but now on
    # the one shared path.
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env.ctx.agent_system


def _sir(n, seeds=10, time_limit=1000):
    person = A.AgentType(
        "Person", parameters={"beta": 0.25, "gamma": 0.1},
        attributes=[A.Attribute("g", "1"), A.Attribute("t_inf", "-1")],
        states=["S", "I", "R"], initial="S",
        transitions=[A.Transition("S", "I", "rate", f'beta * COUNT("P", "I") / {n}'),
                     A.Transition("I", "R", "rate", "self.g * gamma")])
    return _model(person, A.Population("P", "Person", n, {"I": seeds}), time_limit=time_limit)


def _sir_firings(n, seeds=10):
    """Run an SIR to extinction; every firing is one infection or one
    recovery, so firings = 2(N - survivors) - seeds exactly (the plan's
    "2N - survivors", the seeds never being infected)."""
    started = time.perf_counter()
    system = _run(_sir(n, seeds))
    elapsed = time.perf_counter() - started
    s, i, _r = system.by_name["P"].counts
    assert i == 0
    assert system.firings == 2 * (n - s) - seeds
    return elapsed


def test_perf_proxy_2k_sir_firings_are_exact_and_fast():
    """Fast proxy for the slow guards (PHASE5 design refinements: 2k agents
    under 0.5 s; about 0.08 s here)."""
    assert _sir_firings(2000) < 0.5


@pytest.mark.slow
def test_perf_10k_sir_under_3s():
    """AB-4 guard: 10k agents <= 3 s (measured 0.37 s, 17,704 firings)."""
    assert _sir_firings(10_000) < 3.0


@pytest.mark.slow
def test_perf_100k_sir_scaled_to_20k():
    """AB-4 guard: 100k agents <= 20 s, run at 20k with a proportional 4 s so
    the full set stays under its ceiling (the SD-3 precedent). Full size
    measured 2026-09-24: 4.1 s, 178,778 firings, peak 114 MB (about 1.1 KB per
    agent, nearly all the per-agent stream every S agent needs for its t=0 draw;
    D107's counter-based streams removed that)."""
    assert _sir_firings(20_000) < 4.0


def _expo_run(n):
    """n agents, one EXPO(1) timeout each: (seconds, system)."""
    kind = A.AgentType("T", states=["S", "D"],
                       transitions=[A.Transition("S", "D", "timeout", "EXPO(1)")])
    started = time.perf_counter()
    system = _run(_model(kind, A.Population("P", "T", n)))
    assert system.by_name["P"].counts == [0, n]
    return time.perf_counter() - started, system


def test_agent_streams_cost_bytes_not_objects():
    """D107 (#149): one draw by each of 20k agents allocates no per-agent
    object -- only the preallocated 4-byte call counter (was ~1 KB: a
    Generator over a SeedSequence per agent, 114 MB peak for 100k)."""
    import tracemalloc
    kind = A.AgentType("T", states=["S"])
    n = 20_000
    pop = _run(_model(kind, A.Population("P", "T", n), time_limit=1)).by_name["P"]
    tracemalloc.start()
    for i in range(n):
        pop.rng(i).standard_exponential()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < n and pop.draws.tolist() == [1] * n        # < 1 byte per agent


def test_perf_proxy_20k_agents_one_draw_each():
    """Fast proxy for the 1M guard below (about 0.3 s here)."""
    assert _expo_run(20_000)[0] < 3.0


@pytest.mark.slow
def test_perf_250k_agents_one_expo_timeout_each():
    """D107 guard, run at a QUARTER of the 1M target (60 s, 400 MB) so the full
    set stays under its ceiling: 250k agents, one EXPO timeout each, < 15 s and
    < 100 MB of resident growth. Measured 2026-09-25 at 1M: 20 s, +173 MB
    (before D107: 80 s, +1.2 GB). Memory is checked when psutil is present."""
    try:
        import psutil
        rss = psutil.Process().memory_info().rss
    except ImportError:
        psutil = None
    elapsed, system = _expo_run(250_000)
    assert elapsed < 15.0
    if psutil is not None:
        assert psutil.Process().memory_info().rss - rss < 100e6


def _cells(n=150, cond="self.hits * 2 >= 3 - self.h", where="self.h > 0.9"):
    """Guards, `where`-filtered sends, an own condition, a random and two
    vectorisable attribute inits."""
    cell = A.AgentType(
        "Cell", parameters={"k": 2},
        attributes=[A.Attribute("h", "UNIF(0, 1)"), A.Attribute("hits", "0"),
                    A.Attribute("w", "k * self.h - 1")],
        states=["A", "B", "C"], initial="A",
        transitions=[
            A.Transition("A", "B", "timeout", "EXPO(10)", guard="self.h > 0.2",
                         actions=[A.Action.send("ping", {"population": "Q", "where": where})]),
            A.Transition("A", "A", "message", "ping",
                         actions=[A.Action.set("self.hits", "self.hits + 1")]),
            A.Transition("B", "C", "condition", cond),
            A.Transition("B", "A", "timeout", "5")])
    return _model(cell, A.Population("Q", "Cell", n), time_limit=50)


def _snapshot(system):
    pop = system.by_name["Q"]
    return (system.firings, pop.state.tolist(), pop.entered.tolist(), pop.epoch.tolist(),
            {k: v.tolist() for k, v in pop.attrs.items()}, repr(pop.summary(50.0)))


@pytest.mark.parametrize("cond, where", [
    ("self.hits * 2 >= 3 - self.h", "self.h > 0.9"),
    ("self.hits >= 1", "0.9 < self.h + 0 * self.id")])
def test_numpy_paths_are_bit_identical_to_the_per_agent_loop(monkeypatch, cond, where):
    fast = _snapshot(_run(_cells(cond=cond, where=where)))
    monkeypatch.setattr(A.compile, "vectorise", lambda *args, **kwargs: None)
    assert _snapshot(_run(_cells(cond=cond, where=where))) == fast
    assert fast[0] > 150 and fast[1].count(2) > 0      # the condition does fire


def test_the_numpy_subset_is_chosen_at_compile_time():
    for text in ("self.x * 2 >= 3 - self.h", 'self.state != "I"', "-self.x < k", "self.id / 2",
                 'COUNT("P", "I") > self.x', "abs(self.x) > 1", "COS(RAD(self.x)) < 0.5"):
        assert A.vectorise(text) is not None, text      # A9: abs and the math calls
    for text in ("self.x > 1 and TNOW > 5", "not self.x", "IF(self.x, 1, 2)", "self.x < 1 < 2",
                 "RAND() < self.x", "self.x % 2 == 1", "self.x ** 2", "round(self.x) > 1",
                 'NEAR("P", 5) > 1', "self.state"):
        assert A.vectorise(text) is None, text


def test_state_literals_and_a_zero_divisor():
    """`self.state == "B"` compares state codes; numpy runs under
    errstate(raise), so 1/0 is not silently inf: the loop raises the same
    ExpressionError it always did."""
    system = _run(_cells(n=40))
    pop = system.by_name["Q"]
    for text, name in (('self.state == "B"', "B"), ('"Nope" != self.state', None)):
        hit = A._run_vec(A.vectorise(text), pop, pop.ids, bool).tolist()
        assert hit == [(pop.read("state", i) == name) is (name is not None) for i in range(40)]
    pop.attrs["hits"][3] = 0.0
    assert A._run_vec(A.vectorise("1 / self.hits > 0"), pop, pop.ids, bool) is None
    with pytest.raises(ExpressionError):
        compile_expression("1 / self.hits > 0").evaluate(pop.scope(3))


def test_column_init_only_when_no_agent_reads_a_half_initialised_population():
    kinds = {
        True: [A.AgentType("T", attributes=[A.Attribute("x", "UNIF(0, 1)")], states=["S"])],
        False: [A.AgentType("T", attributes=[A.Attribute("x", 'SUM("P", "x") + 1')],
                            states=["S"]),
                A.AgentType("T", attributes=["x"], states=[A.State("S", entry=[
                    A.Action.set("self.x", "1")])])]}
    for expected, types in kinds.items():
        for kind in types:
            system = _run(_model(kind, A.Population("P", "T", 3), time_limit=1))
            assert system.by_name["P"].type.columns is expected


def test_streams_are_created_only_by_a_draw():
    kind = A.AgentType("T", attributes=[A.Attribute("x", "2"), A.Attribute("y", "UNIF(0, 1)")],
                       states=["S", "D"], transitions=[A.Transition("S", "D", "timeout", "3")])
    system = _run(_model(kind, A.Population("P", "T", 50, {"D": 40}), time_limit=10))
    pop = system.by_name["P"]
    assert pop.draws.tolist() == [1] * 50           # y's UNIF: every agent draws once
    kind.attributes = [A.Attribute("x", "2").to_dict()]
    system = _run(_model(kind, A.Population("P", "T", 50, {"D": 40}), time_limit=10))
    pop = system.by_name["P"]
    assert not pop.draws.any() and pop.attrs["x"].tolist() == [2.0] * 50
    assert pop.counts == [0, 50] and pop.entered[40:].tolist() == [3.0] * 10


def test_sum_and_mean_take_a_state_filter():
    system = _run(_cells(n=100))
    pop = system.by_name["Q"]
    scope = ExpressionScope(ctx=system.env.ctx)
    in_b = pop.attrs["h"][pop.state == 1]
    assert compile_expression('SUM("Q", "h", "B")').evaluate(scope) == float(in_b.sum())
    assert compile_expression('MEAN("Q", "h", "B")').evaluate(scope) == float(in_b.mean())
    assert compile_expression('MEAN("Q", "h", "C")').evaluate(scope) == (
        float(pop.attrs["h"][pop.state == 2].mean()) if pop.counts[2] else 0.0)
    assert compile_expression('SUM("Q", "h")').evaluate(scope) == float(pop.attrs["h"].sum())
    layout = _cells(n=10, cond='MEAN("Q", "h", "Nope") > 0 and self.h > 0')
    assert problem_codes(layout) == {"E_AGENT_STATE"}
    layout = _cells(n=10, cond='SUM("Q", "h", 1) > 0 and self.h > 0')
    assert problem_codes(layout) == {"E_AGGREGATE_AGENTS"}
