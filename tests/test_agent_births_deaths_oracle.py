"""Independent oracle tests for PHASE6 6A milestone C5 (docs/PHASE6_PLAN.md, "Births and
deaths": alive mask + free list, `spawn`/`remove` actions, `max_count`, generation tags on
stored refs; COUNT/SUM/MEAN/NEAR/`where`/`to`/agent_summary/trace/playback skip dead agents;
CRN rule for recycled slots). Every expected value is a hand derivation or a tiny standalone
reference computation, independent of the engine's own storage/free-list implementation.

Written from the plan's one-line row plus the task brief alone -- `nagasim/core/agents.py` had
NO spawn/remove/max_count support at the time this file was written (an Opus engine agent was
implementing C5 concurrently). Wire-format guesses below (marked GUESS) may need adjusting once
C5 lands; no expected VALUE was ever read off the landed implementation.

Stream recipe (D107, `conftest.agent_rng`): agent j's first draw call is Philox at counter
(0, 0, j, 0) under the population's key. For an INITIAL agent, j is its slot index. For a SPAWNED
agent the brief gives j = n_initial + birth_serial (serial 0 for the first spawn of that
population), independent of which slot the birth lands in -- `_rebuild_E` below takes j directly
so both cases share one helper.

GUESS notes (wire format only, to be reconciled once C5's actual grammar is known):
- Action verbs: `{"remove": "self"}` and `{"spawn": pop, "count"?, "state"?, "init"?}`, one verb
  per action dict -- the same shape as the landed AB-2 verbs (`target`/`expr`, `send`/`to`,
  `enter`/`e_type`, `transfer`/`amount`, `move_to`), which the brief's own JSON matches.
- A transition may target its own source state (`"from": "X", "to": "X"`); nothing in the
  landed AB-1/AB-2 validator forbids it (checked: `validate_structured`'s only "from"/"to" rule
  is state membership), and the brief's own worked description ("re-entering its state") assumes
  exactly this.
- "Recycled slot" (test 3) and "aggregates skip the dead" (test 4) never read a new C5-only
  array by guessed name: they only use pre-C5, already-stable accessors --
  `_Population.attrs[name]` (existing since AB-1), `pop.state`/`pop.entered`, and the expression
  builtins COUNT/SUM/MEAN/NEAR (called here via `AgentSystem.aggregate`/`.count`/`.near`, already
  documented pre-C5 methods) -- so these tests should keep working unchanged after C5 lands,
  modulo those methods' own semantics correctly excluding dead agents as the brief requires.
- Spawn action `count`: a literal int (the validator's OWN hint text, `'"count": 1"'`) is
  currently rejected by a validator bug (`agents.py::spawn_problems`, ~line 908-915: the
  `if`/`elif str`/`elif count is not None` chain has no branch for "a valid int", so it always
  falls into the last `elif` and gets flagged) -- a real finding, not a wire-format choice; every
  `count` below uses the string-expression spelling (`"1"`), which takes the other, working
  branch, so these tests still exercise the CRN/recycling/max_count logic the milestone is about.
- Test 7 (stale ref): the brief's literal `entity.agent.<attr>` chain does not compile under the
  landed (pre-C5) expression grammar (`core/expressions.py::_check` only allows a single
  `entity.<attr>`/`self.<attr>`/`AGENT(...).attr` hop, and `entity.agent` is itself an Attribute
  node, not a bare Name, so nesting `.={attr}` on it is rejected today). The one CONFIRMED,
  already-landed mechanism for "using a stored agent ref later" is `entity.agent` as a SEND
  block's `to` (`core/agents.py::compile_target`'s default, `"to": "entity.agent"`), which
  resolves through `AgentSystem.ref` -- the natural place for a generation check. Test 7 uses
  that route and accepts EITHER outcome the brief allows (a clear exception, or a silent drop
  that never reaches the wrong/recycled occupant) as a pass; only "the message silently reaches
  the impostor" fails it.
"""
import numpy as np
import pytest

from conftest import agent_rng, layout as make_layout, delay, dispose, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _sim(time_limit):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1}


def _agent_layout(blocks=(), links=(), **sections):
    return make_layout(list(blocks), list(links), **sections)


def _rebuild_E(seed, pop_name, j):
    """Rebuild agent j's own E ~ Exp(1) draw (j = slot index for an initial agent,
    n_initial + birth_serial for a spawned one -- the brief's own rule)."""
    return agent_rng(seed, pop_name, j).standard_exponential()


# ================================================================ 1. pure death
def test_pure_death_times_match_the_rebuilt_streams_and_count_drops_exactly():
    """N agents in state A, each with an own-rate transition to B carrying a
    {"remove": "self"} action: death time = E_i/mu from the rebuilt per-slot stream
    (i is the slot index, an initial agent, AB-1's own rule). COUNT(pop) at three
    checkpoints (three separate short runs, since CRN pins the draws regardless of
    run length) must equal exactly the number of agents whose death time is still
    in the future -- no off-by-one, no partial credit for a death at t itself."""
    seed, pop_name, mu, N = 0, "People", 0.6, 6
    deaths = [_rebuild_E(seed, pop_name, i) / mu for i in range(N)]
    atype = {"name": "Item", "parameters": {"mu": mu}, "attributes": [],
             "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
             "transitions": [{"from": "A", "to": "B", "rate": "mu",
                              "actions": [{"remove": "self"}]}]}
    pop = {"name": pop_name, "type": "Item", "count": N,
           "initial": [{"state": "A", "count": N}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(3.0))

    for t, expected_alive in ((0.4, 5), (1.0, 4), (2.0, 2)):     # D107 streams
        env = _run(layout, t, seed=seed)
        assert sum(1 for d in deaths if d > t) == expected_alive
        assert env.ctx.agent_system.count(pop_name) == expected_alive


# ================================================================ 2. birth serial
def test_spawned_children_fire_from_the_birth_serial_stream():
    """One Parent agent self-loops on a constant timeout T0, spawning one Child each
    time; each Child k (serial k, n_initial=0 for the never-populated Child pop) has
    its own-rate A->B transition, whose firing time must be spawn_time_k + E_k/lambda
    with E_k rebuilt at stream key n_initial + k = k -- checked as a SET (sorted), so
    the test does not assume slot index == birth order."""
    seed, T0, lam = 0, 1.5, 1.3
    child_pop = "Child"
    expected = sorted((k + 1) * T0 + _rebuild_E(seed, child_pop, k) / lam for k in range(3))
    run_len = 5.9   # > the 3rd child's firing, < the 4th spawn at 4*T0=6.0

    parent_type = {"name": "Par", "parameters": {}, "attributes": [],
                   "states": [{"name": "Loop"}, {"name": "Idle"}], "initial_state": "Loop",
                   "transitions": [{"from": "Loop", "to": "Loop", "timeout": str(T0),
                                    "actions": [{"spawn": child_pop, "count": "1", "state": "A"}]}]}
    child_type = {"name": "Kid", "parameters": {"lam": lam}, "attributes": [],
                  "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                  "transitions": [{"from": "A", "to": "B", "rate": "lam"}]}
    parent_pop = {"name": "Parent", "type": "Par", "count": 1,
                  "initial": [{"state": "Loop", "count": 1}]}
    child = {"name": child_pop, "type": "Kid", "count": 0, "max_count": 5, "initial": []}
    layout = _agent_layout(agent_types=[parent_type, child_type], populations=[parent_pop, child],
                            simulation=_sim(run_len))
    env = _run(layout, run_len, seed=seed)

    kid = _pop(env, child_pop)
    b_index = kid.type.index["B"]
    reached_b = sorted(float(t) for t, s in zip(kid.entered, kid.state) if int(s) == b_index)
    assert len(reached_b) == 3
    assert reached_b == pytest.approx(expected, rel=1e-9)


# ============================================================ 3. recycled slot
def test_recycled_slot_draws_from_the_birth_serial_not_the_dead_slots_stream():
    """People starts with agent0 (Die) and agent1 (Spawn). Agent0 dies fast (own
    rate); agent1 later spawns a new agent into the pool (its only free slot is
    agent0's, since nothing else died) with its own state "A" -> "B" transition.
    The new agent's firing time must come from key n_initial + 0 = 2 (a fresh
    birth serial), NOT from key 0 (agent0's already-used slot stream) -- the two
    hypotheses are checked to differ by a real margin, not by coincidence."""
    seed, pop_name = 0, "People"
    mu_die, t_spawn, lam2 = 5.0, 3.0, 1.3
    E0 = _rebuild_E(seed, pop_name, 0)          # agent0's own (already-spent) stream
    E2 = _rebuild_E(seed, pop_name, 2)          # n_initial(2) + serial(0): the correct key
    assert abs(E2 - E0) > 0.1                   # a real discriminator
    t_die = E0 / mu_die
    expected_new = t_spawn + E2 / lam2
    wrong_new = t_spawn + E0 / lam2
    assert t_die < t_spawn        # ordering the test relies on
    run_len = 5.0                 # < 2*t_spawn: exactly one spawn fires

    atype = {"name": "Item", "parameters": {"mu_die": mu_die, "lam2": lam2}, "attributes": [],
             "states": [{"name": "Die"}, {"name": "Spawn"}, {"name": "A"}, {"name": "B"}],
             "initial_state": "Die",
             "transitions": [
                 {"from": "Die", "to": "B", "rate": "mu_die", "actions": [{"remove": "self"}]},
                 {"from": "Spawn", "to": "Spawn", "timeout": str(t_spawn),
                  "actions": [{"spawn": pop_name, "count": "1", "state": "A"}]},
                 {"from": "A", "to": "B", "rate": "lam2"},
             ]}
    pop = {"name": pop_name, "type": "Item", "count": 2,
           "initial": [{"state": "Die", "count": 1}, {"state": "Spawn", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(run_len))
    env = _run(layout, run_len, seed=seed)

    pop_rt = _pop(env, pop_name)
    a_idx, b_idx = pop_rt.type.index["A"], pop_rt.type.index["B"]
    newcomer = [i for i, s in enumerate(pop_rt.state) if int(s) in (a_idx, b_idx)]
    assert len(newcomer) == 1        # only the spawned agent can reach A/B
    i = newcomer[0]
    assert int(pop_rt.state[i]) == b_idx
    assert abs(expected_new - wrong_new) > 0.1
    assert float(pop_rt.entered[i]) == pytest.approx(expected_new, rel=1e-9)


# =========================================================== 4. aggregates skip dead
def test_sum_mean_and_near_skip_dead_agents():
    """5 agents on a line (x=0..4, y=0), attribute val=self.id, each with an own-rate
    death (remove). At a checkpoint where only agent1 has died (t=0.2, chosen from
    the rebuilt D107 streams below), SUM/MEAN(val) and NEAR(pop, 10) queried from the
    last-surviving agent (index 2, whose own death is far in the future) must match
    brute force over the alive set {0,2,3,4} only."""
    seed, pop_name, mu, N = 0, "Crowd", 0.5, 5
    deaths = [_rebuild_E(seed, pop_name, i) / mu for i in range(N)]
    t = 0.2
    alive = [i for i in range(N) if deaths[i] > t]
    assert alive == [0, 2, 3, 4] and deaths[1] < t     # sanity: exactly agent1 is dead

    atype = {"name": "Item", "parameters": {"mu": mu},
             "attributes": [{"name": "val", "init": "self.id"}],
             "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
             "transitions": [{"from": "A", "to": "B", "rate": "mu",
                              "actions": [{"remove": "self"}]}],
             "space": {"kind": "continuous", "bounds": [0, 0, 100, 100]}}
    pop = {"name": pop_name, "type": "Item", "count": N,
           "initial": [{"state": "A", "count": N}],
           "placement": {"kind": "table", "points": [[float(i), 0.0] for i in range(N)]}}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(t))
    env = _run(layout, t, seed=seed)

    system = env.ctx.agent_system
    pop_rt = _pop(env, pop_name)
    assert system.aggregate("SUM", pop_name, "val") == pytest.approx(sum(alive), rel=1e-12)
    assert system.aggregate("MEAN", pop_name, "val") == pytest.approx(
        sum(alive) / len(alive), rel=1e-12)
    near_count = system.near(pop_name, 10.0, None, pop_rt.scope(2))
    assert near_count == len(alive) - 1     # excludes self (agent 2)


# ============================================= 5. agent_summary time-average
def test_agent_summary_avg_count_with_deaths_equals_the_hand_integral_exactly():
    """3 agents enter A at t=0, each with a distinct DETERMINISTIC (constant, no
    stream) own timeout = self.id + 1, i.e. death at t=1,2,3, then stay dead (remove).
    Over [0, 4], count(A) = 3 on [0,1), 2 on [1,2), 1 on [2,3), 0 on [3,4]: an
    integral of exactly 3+2+1+0 = 6, average 6/4 = 1.5, no floating-point slop."""
    from nagasim.core.outputs import summarize_agents

    pop_name, T = "People", 4.0
    atype = {"name": "Item", "parameters": {},
             "attributes": [{"name": "death_t", "init": "self.id + 1"}],
             "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
             "transitions": [{"from": "A", "to": "B", "timeout": "self.death_t",
                              "actions": [{"remove": "self"}]}]}
    pop = {"name": pop_name, "type": "Item", "count": 3,
           "initial": [{"state": "A", "count": 3}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(T))
    env = _run(layout, T, seed=0)
    frame = summarize_agents(env, warmup=0.0, run_id=None)
    row = frame[(frame["Population"] == pop_name) & (frame["State"] == "A")].iloc[0]
    assert row["Avg Count"] == pytest.approx(1.5, rel=1e-12)


# =================================================================== 6. max_count
def test_spawning_beyond_max_count_is_a_clear_runtime_error():
    """A population already at its (explicit) max_count=1 tries to spawn one more
    at t=0: must fail with a clear, on-topic error, not silently grow past the cap
    or corrupt storage."""
    atype = {"name": "Item2", "parameters": {}, "attributes": [],
             "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
             "transitions": [{"from": "A", "to": "B", "timeout": "0",
                              "actions": [{"spawn": "Single", "count": "1", "state": "A"}]}]}
    pop = {"name": "Single", "type": "Item2", "count": 1, "max_count": 1,
           "initial": [{"state": "A", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(1.0))
    with pytest.raises(Exception, match=r"(?i)max.?count"):
        _run(layout, 1.0)


def test_a_model_without_spawn_or_remove_is_bit_identical_to_pre_c5_head():
    """Population "People" (Item type, own-rate A->B at mu=0.7, no
    spawn/remove/max_count anywhere), seed=0, N=5, time_limit=30 -- C5 must not
    change one bit of a model that never uses its new verbs/field. The values
    were hard-coded from pre-C5 HEAD 475ee5e; D107 re-keyed the streams, so
    they are now the rebuilt E_i / mu and their exact time average."""
    seed, pop_name, mu, N, run_len = 0, "People", 0.7, 5, 30.0
    atype = {"name": "Item", "parameters": {"mu": mu}, "attributes": [],
             "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
             "transitions": [{"from": "A", "to": "B", "rate": "mu"}]}
    pop = {"name": pop_name, "type": "Item", "count": N,
           "initial": [{"state": "A", "count": N}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(run_len))
    env = _run(layout, run_len, seed=seed)
    pop_rt = _pop(env, pop_name)

    expected_states = [1, 1, 1, 1, 1]
    expected_entered = [_rebuild_E(seed, pop_name, i) / mu for i in range(N)]
    assert [int(s) for s in pop_rt.state] == expected_states
    assert [float(x) for x in pop_rt.entered] == pytest.approx(expected_entered, rel=1e-12)

    from nagasim.core.outputs import summarize_agents
    frame = summarize_agents(env, warmup=0.0, run_id=None)
    row = frame[(frame["Population"] == pop_name) & (frame["State"] == "A")].iloc[0]
    assert row["Avg Count"] == pytest.approx(sum(expected_entered) / run_len, rel=1e-12)


# ==================================================================== 7. stale ref
def test_stale_ref_never_silently_reaches_the_recycled_impostor():
    """Agent0 (Die), while still alive, creates a DES entity holding entity.agent =
    ("Item", 0) (the "enter" action) and dies (remove) in the same firing. Agent1
    (Spawn) later spawns a newcomer that recycles agent0's freed slot and waits in
    "A" for a message "poke". The DES entity, after a DELAY that ends well after the
    recycle, reaches a SEND block whose default `to` is "entity.agent" -- the one
    already-landed mechanism for addressing a stored agent ref. C5's generation tag
    must stop this stale ref from silently reaching the impostor now sitting in that
    slot: either the run raises a clear error (checked to be non-empty), or the
    message is dropped and the impostor's "hit" attribute (set only by receiving it)
    stays 0. Only "the impostor's hit becomes 1" -- a silent misdelivery -- fails
    this test."""
    seed, pop_name = 0, "Item"
    mu_die, t_spawn, t_delay = 5.0, 2.0, 3.0     # t_die << t_spawn << (t_die + t_delay)
    run_len = 6.0

    record = delay(name="Record", value=t_delay)
    ping = {"name": "Ping", "description": "Ping", "type": "SEND", "position": [2, 0],
            "params": {"message": "poke", "to": "entity.agent", "payload": ""}}
    end = dispose(name="Dispose1")
    blocks = [record, ping, end]
    links = [{"source": "Record", "target": "Ping"}, {"source": "Ping", "target": "Dispose1"}]

    atype = {"name": "Item", "parameters": {"mu_die": mu_die},
             "attributes": [{"name": "hit", "init": "0"}],
             "states": [{"name": "Die"}, {"name": "Spawn"}, {"name": "A"}, {"name": "Hit"}],
             "initial_state": "Die",
             "transitions": [
                 {"from": "Die", "to": "Hit", "rate": "mu_die",
                  "actions": [{"enter": "Record"}, {"remove": "self"}]},
                 {"from": "Spawn", "to": "Spawn", "timeout": str(t_spawn),
                  "actions": [{"spawn": pop_name, "count": "1", "state": "A"}]},
                 {"from": "A", "to": "Hit", "message": "poke",
                  "actions": [{"target": "self.hit", "expr": "1"}]},
             ]}
    pop = {"name": pop_name, "type": "Item", "count": 2,
           "initial": [{"state": "Die", "count": 1}, {"state": "Spawn", "count": 1}]}
    layout = _agent_layout(blocks=blocks, links=links, agent_types=[atype], populations=[pop],
                            simulation=_sim(run_len))

    try:
        env = _run(layout, run_len, seed=seed)
    except Exception as exc:
        assert str(exc)          # raised clearly: an acceptable outcome
        return

    pop_rt = _pop(env, pop_name)
    a_idx, hit_idx = pop_rt.type.index["A"], pop_rt.type.index["Hit"]
    newcomer = [i for i, s in enumerate(pop_rt.state) if int(s) in (a_idx, hit_idx)]
    assert len(newcomer) == 1
    assert float(pop_rt.attrs["hit"][newcomer[0]]) == 0.0   # never reached the impostor
