"""Independent oracle tests for PHASE5 AB-1 (docs/history/PHASE5_PLAN.md, "Phase 5B:
agent-based modelling", the AB-1 milestone row and the "Design refinements"
section). Every expected value is a hand derivation or a tiny standalone
reference computation, independent of the engine's own integrated-hazard-heap
algebra.

`core/agents.py` landed mid-session (concurrently, by another agent) while
this file was being written from the plan text alone. Its interface was
peeked at ONLY to fix wire-format details the plan leaves ambiguous or gets
wrong (a JSON key name, a list vs. dict accessor) -- never to read off an
expected VALUE; every number below was derived by hand from the plan's own
algorithm description (and, for the piecewise/product cases, cross-checked
against the documented `Lambda(t) + E/w_i` key formula) before the module was
read. See RESOLVED AMBIGUITIES below for what changed and why.

RESOLVED AMBIGUITIES (plan text vs. the landed interface):

1. Stream recipe. The plan writes `default_rng(SeedSequence([seed,
   hash(pop), i]))`. Python's builtin `hash()` is salted per process for
   str/bytes, so a literal `hash(pop)` would be non-reproducible across
   sessions -- the exact bug this codebase already worked around for the
   identical pattern: `core/base_process.py` defines `deterministic_hash`
   (md5-based, stable) and uses it in exactly this `SeedSequence([seed,
   deterministic_hash(x)])` shape (`base_process.py:90`,
   `resource_manager.py:161`). The landed `core/agents.py` does the same
   (`_Population._hash = deterministic_hash(self.name)`), confirming the
   guess. D107 (#149) since re-keyed agent streams to counter-based Philox;
   `_rebuild_E` below uses that recipe (`conftest.agent_rng`).
2. Top-level JSON key. The plan's worked example uses `"agents": [...]`;
   the landed schema uses `"agent_types": [...]` (`AgentType.SECTION =
   "agent_types"`, and `validate_structured`/`build_agent_system` both read
   `layout.get("agent_types")`). Every layout below uses `agent_types=`.
2b. `env.ctx.agent_system.populations` is a plain list of `_Population`
   objects (each with a `.name`), not a name-keyed dict like
   `level_system.levels`; `_pop()` below does the linear lookup.
3. Condition "scan" polling. The plan says conditions get an "optional
   `scan` polling". Resolved in AB-1: literal STEP/PULSE/RAMP instants,
   literal clock comparisons (`TNOW >= 7`) and TS() knots are AgentSystem
   wake-ups, and a condition on a held input (LEVEL, AUX, ...) takes a
   `scan` interval. Tests 2 and 5 therefore run with NO other event in the
   model: they check that the engine wakes itself at the exact instant.
4. `agent_summary` accessor. The plan names the frame, not its accessor;
   `nagasim.core.outputs.summarize_agents(env, warmup=..., run_id=...)`
   matches the established `summarize_levels`/`summarize_flows` naming pair
   and was confirmed to exist with the documented columns.
5. AB-1's own E_ code set isn't enumerated on its milestone row (just "every
   `E_` code"); test 7 covers every agent/population code reachable without
   messages/SEND (an AB-2 concern per that milestone's own row):
   `E_AGENT_TYPE_NAME`, `E_AGENT_STATE`, `E_AGENT_TRANSITION`,
   `E_AGENT_RATE_FORM`, `E_AGENT_ATTRIBUTE`, `E_AGENT_ACTION`,
   `E_AGENT_EXPRESSION`, `E_AGENT_SPACE`, `E_POPULATION_NAME`,
   `E_POPULATION_TYPE`, `E_POPULATION_COUNT`, `E_POPULATION_UNKNOWN`. The
   plan's own one-line gloss for the last one turned out to mean "COUNT/SUM/
   MEAN names an undeclared population" (`core/problems.py`), not an unknown
   state in a population's initial placement (that case is `E_AGENT_STATE`)
   -- test 7's case for it was fixed accordingly.
"""
import numpy as np
import pytest

from conftest import (agent_rng, chain, create, delay, dispose, layout as make_layout,
                      problem_codes, run_env)


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _sim(time_limit):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1}


def _agent_layout(blocks=(), links=(), **sections):
    """A blockless agent-only model unless *blocks*/*links* are given
    (PHASE5_PLAN "Blockless models must validate": `blocks == []` skips
    E_NO_CREATE/E_NO_DISPOSE when the model declares levels, flows OR
    populations)."""
    return make_layout(list(blocks), list(links), **sections)


def _rebuild_E(seed, pop_name, i):
    """Rebuild agent i's own E_i ~ Exp(1) draw (ambiguity #1)."""
    return agent_rng(seed, pop_name, i).standard_exponential()


def _piecewise_firing_time(E, segments):
    """Reference form of integral_0^t* lambda(s) ds = E for a piecewise
    constant lambda: segments = [(t0, t1, rate), ...] covering [0, t*]."""
    acc = 0.0
    for t0, t1, rate in segments:
        seg = rate * (t1 - t0)
        if acc + seg >= E:
            return t0 + (E - acc) / rate
        acc += seg
    raise ValueError("E exceeds the total integral of the given segments")


# ============================================================ 1. CRN own rate
def test_own_rate_firing_time_matches_the_rebuilt_crn_stream():
    """One agent, state A -> B at a constant rate `lam` (a bare parameter, no
    `self`); firing time = t_entry + E/lam = E/lam (t_entry = 0), E rebuilt
    from the documented per-agent stream. No other event in the model, so the
    rate is evaluated once at t=0 and never needs a re-check."""
    seed, pop_name, lam = 0, "People", 0.4
    E = _rebuild_E(seed, pop_name, 0)
    expected = E / lam
    layout = _agent_layout(
        agent_types=[{"name": "Item", "parameters": {"lam": lam}, "attributes": [],
                      "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                      "transitions": [{"from": "A", "to": "B", "rate": "lam"}]}],
        populations=[{"name": pop_name, "type": "Item", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}],
        simulation=_sim(expected * 3))
    env = _run(layout, expected * 3, seed=seed)
    pop_rt = _pop(env, pop_name)
    assert int(pop_rt.state[0]) == 1  # state B is index 1 (declaration order)
    assert float(pop_rt.entered[0]) == pytest.approx(expected, rel=1e-12)


# =================================================== 2. piecewise shared rate
def test_piecewise_shared_rate_firing_time_solves_the_hand_integral():
    """rate(t) = 0.2 for t < 1, 0.8 for t >= 1 via `0.2 + STEP(0.6, 1)`: no
    `self`, so the "shared" rate form. Nothing else happens in the model: the
    engine must wake at the STEP's literal instant t=1 by itself (ambiguity
    #3); a stale t=0 rate would give E/0.2, which the discriminator rules out."""
    seed, pop_name = 0, "People"
    E = _rebuild_E(seed, pop_name, 0)
    naive = E / 0.2
    expected = _piecewise_firing_time(E, [(0.0, 1.0, 0.2), (1.0, 1e6, 0.8)])
    assert abs(expected - naive) > 0.1        # a real discriminator, not a coincidence
    layout = _agent_layout(
        agent_types=[{"name": "Item", "parameters": {}, "attributes": [],
                      "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                      "transitions": [{"from": "A", "to": "B", "rate": "0.2 + STEP(0.6, 1)"}]}],
        populations=[{"name": pop_name, "type": "Item", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}],
        simulation=_sim(expected + 5.0))
    env = _run(layout, expected + 5.0, seed=seed)
    pop_rt = _pop(env, pop_name)
    assert int(pop_rt.state[0]) == 1
    assert float(pop_rt.entered[0]) == pytest.approx(expected, rel=1e-9)


# ========================================================= 3. product form
def test_product_rate_firing_time_solves_own_times_shared_equals_e():
    """rate = self.k * 0.5 (product form, w_i = self.k, the documented
    `Lambda(t) + E/w_i` key with Lambda(t) = 0.5*t): two single-agent
    populations of different AgentTypes with different constant `k`, so
    t_i = E_i / (0.5 * k_i)."""
    seed = 0

    def _make(pop_name, k):
        agent_type = f"Item_{pop_name}"
        atype = {"name": agent_type, "parameters": {}, "attributes": [{"name": "k", "init": str(k)}],
                 "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                 "transitions": [{"from": "A", "to": "B", "rate": "self.k * 0.5"}]}
        population = {"name": pop_name, "type": agent_type, "count": 1,
                      "initial": [{"state": "A", "count": 1}]}
        return atype, population

    a1, p1 = _make("P1", 1.0)
    a2, p2 = _make("P2", 2.0)
    E1, E2 = _rebuild_E(seed, "P1", 0), _rebuild_E(seed, "P2", 0)
    t1, t2 = E1 / (0.5 * 1.0), E2 / (0.5 * 2.0)
    run_len = max(t1, t2) * 3
    layout = _agent_layout(
        agent_types=[a1, a2], populations=[p1, p2], simulation=_sim(run_len))
    env = _run(layout, run_len, seed=seed)
    assert float(_pop(env, "P1").entered[0]) == pytest.approx(t1, rel=1e-12)
    assert float(_pop(env, "P2").entered[0]) == pytest.approx(t2, rel=1e-12)


# ======================================================= 4. constant timeout
def test_constant_timeout_gives_avg_count_n_over_2_exactly():
    """N agents enter state A at t=0, all transition to B after a fixed
    timeout T0 (deterministic: a plain constant, no distribution, so no
    stream is even drawn), then stay in B. Over [0, 2*T0], count(A) = N on
    [0, T0) and 0 on [T0, 2*T0], so the time average is exactly N/2."""
    from nagasim.core.outputs import summarize_agents  # ambiguity #4

    N, T0, pop_name = 20, 4.0, "People"
    layout = _agent_layout(
        agent_types=[{"name": "Item", "parameters": {}, "attributes": [],
                      "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                      "transitions": [{"from": "A", "to": "B", "timeout": str(T0)}]}],
        populations=[{"name": pop_name, "type": "Item", "count": N,
                     "initial": [{"state": "A", "count": N}]}],
        simulation=_sim(2 * T0))
    env = _run(layout, 2 * T0)
    frame = summarize_agents(env, warmup=0.0, run_id=None)
    row = frame[(frame["Population"] == pop_name) & (frame["State"] == "A")].iloc[0]
    assert row["Avg Count"] == pytest.approx(N / 2, rel=1e-12)


# ============================================================ 5. condition
def test_condition_transition_fires_at_exactly_t_equals_7():
    """A condition on TNOW with no other event in the model: the literal
    comparison must be solved to a wake-up at exactly 7 (ambiguity #3)."""
    pop_name = "People"
    layout = _agent_layout(
        agent_types=[{"name": "Item", "parameters": {}, "attributes": [],
                      "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                      "transitions": [{"from": "A", "to": "B", "condition": "TNOW >= 7"}]}],
        populations=[{"name": pop_name, "type": "Item", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}],
        simulation=_sim(10.0))
    env = _run(layout, 10.0)
    pop_rt = _pop(env, pop_name)
    assert int(pop_rt.state[0]) == 1
    assert float(pop_rt.entered[0]) == 7.0


# ============================================ 6. unrelated population = CRN
def test_an_unrelated_population_leaves_firing_times_bit_identical():
    """Same own-rate model as test 1, run alone and again alongside an
    unrelated second population; agent i's stream is keyed by (seed,
    hash(pop), i) alone, so an unrelated population's presence must not
    change a single bit of the first population's firing time (CRN
    isolation, not just approximate agreement)."""
    seed, pop_name, lam = 0, "People", 0.4
    item_type = {"name": "Item", "parameters": {"lam": lam}, "attributes": [],
                 "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                 "transitions": [{"from": "A", "to": "B", "rate": "lam"}]}
    population = {"name": pop_name, "type": "Item", "count": 1,
                  "initial": [{"state": "A", "count": 1}]}
    run_len = 50.0

    alone = _agent_layout(agent_types=[item_type], populations=[population], simulation=_sim(run_len))
    env_alone = _run(alone, run_len, seed=seed)
    t_alone = float(_pop(env_alone, pop_name).entered[0])

    other_type = {"name": "Other", "parameters": {}, "attributes": [],
                  "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                  "transitions": [{"from": "A", "to": "B", "rate": "0.9"}]}
    other_pop = {"name": "Others", "type": "Other", "count": 7,
                 "initial": [{"state": "A", "count": 7}]}
    together = _agent_layout(agent_types=[item_type, other_type], populations=[population, other_pop],
                              simulation=_sim(run_len))
    env_together = _run(together, run_len, seed=seed)
    t_together = float(_pop(env_together, pop_name).entered[0])

    assert t_together == t_alone  # bit-identical, not approx


# ================================================================= 7. E_ codes
def _base_agent_type(**over):
    d = {"name": "Item", "parameters": {}, "attributes": [],
         "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
         "transitions": [{"from": "A", "to": "B", "rate": "0.5"}]}
    d.update(over)
    return d


_E_CODE_CASES = [
    ("E_AGENT_TYPE_NAME", _agent_layout(
        agent_types=[_base_agent_type(name="Item"), _base_agent_type(name="Item")],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_STATE", _agent_layout(
        agent_types=[_base_agent_type(initial_state="Ghost")],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_TRANSITION", _agent_layout(
        agent_types=[_base_agent_type(transitions=[{"from": "A", "to": "B", "rate": "0.5", "timeout": "1"}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_RATE_FORM", _agent_layout(
        agent_types=[_base_agent_type(
            attributes=[{"name": "k", "init": "1.0"}],
            transitions=[{"from": "A", "to": "B", "rate": "self.k + COUNT(\"P\")"}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_ATTRIBUTE", _agent_layout(
        agent_types=[_base_agent_type(attributes=[{"name": "state", "init": "0"}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_ACTION", _agent_layout(
        agent_types=[_base_agent_type(
            transitions=[{"from": "A", "to": "B", "rate": "0.5",
                         "actions": [{"target": "self.state", "expr": "1"}]}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_EXPRESSION", _agent_layout(
        agent_types=[_base_agent_type(transitions=[{"from": "A", "to": "B", "rate": "other.k"}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_AGENT_SPACE", _agent_layout(
        agent_types=[_base_agent_type(space={"kind": "bogus"})],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
    ("E_POPULATION_NAME", _agent_layout(
        agent_types=[_base_agent_type()],
        populations=[{"name": "", "type": "Item", "count": 1}])),
    ("E_POPULATION_TYPE", _agent_layout(
        agent_types=[_base_agent_type()],
        populations=[{"name": "P", "type": "NoSuchType", "count": 1}])),
    ("E_POPULATION_COUNT", _agent_layout(
        agent_types=[_base_agent_type()],
        populations=[{"name": "P", "type": "Item", "count": -5}])),
    ("E_POPULATION_UNKNOWN", _agent_layout(
        agent_types=[_base_agent_type(
            transitions=[{"from": "A", "to": "B", "rate": "0.1 * COUNT(\"Ghost\")"}])],
        populations=[{"name": "P", "type": "Item", "count": 1}])),
]


@pytest.mark.parametrize("code, layout", _E_CODE_CASES, ids=[c for c, _ in _E_CODE_CASES])
def test_every_ab1_e_code_is_reachable_through_a_minimal_bad_layout(code, layout):
    assert code in problem_codes(layout, severity=None)


# ==================================================================== goldens
def test_a_model_without_agents_is_bit_identical_to_pre_ab1_head():
    """Hard-coded from a run of the current HEAD (commit 33cadae, pre-AB-1)
    in an isolated worktree, per the task brief: CREATE(max_arrivals=5,
    interarrival=Constant(1)) -> DELAY(Constant(3)) -> DISPOSE over
    time_limit=20 disposes exactly 5 entities at t=3,4,5,6,7."""
    c = create(max_arrivals=5)
    d = delay("wait", value=3.0)
    dis = dispose()
    layout = make_layout([c, d, dis], chain(c, d, dis), simulation=_sim(20))
    env = _run(layout, 20)
    entities = list(env.ctx.all_entities)
    assert len(entities) == 5
    assert [e.deletion_time for e in entities] == [pytest.approx(x, rel=1e-12)
                                                     for x in (3.0, 4.0, 5.0, 6.0, 7.0)]
