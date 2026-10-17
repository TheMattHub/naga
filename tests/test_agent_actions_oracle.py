"""Independent oracle tests for PHASE5 AB-2 (docs/history/PHASE5_PLAN.md, "Phase 5B:
agent-based modelling", the AB-2 milestone row, its "Coupling to DES and SD"
paragraph and the "Design refinements" section; docs/ARCHITECTURE.md 5.25
lists the codes AB-1 shipped and names messages/SEND/enter/transfer/signal
as "Not yet").

Written BEFORE `core/agents.py` grew AB-2 (checked: as of this file, the
module still has `_BUILT_TRIGGERS = ("timeout", "rate", "condition")` and a
comment "message" is AB-2"; no `send`/`enter`/`transfer`/`signal` action
handling, no `core/blocks/send.py`). Every expected value below is derived
from the plan's prose alone, the same way tests/test_agents_oracles.py
derived AB-1's. Nothing here was copied from an implementation.

ASSUMED WIRE FORMAT (AB-2 not landed; these are the plan's own action-shape
pattern applied consistently, not a guess at engine internals):
  - `{"target": "self.<attr>"|"<var>", "expr": ...}` -- a set action (AB-1,
    confirmed already in `core/agents.py`'s validator).
  - `{"enter": "<block-name>", "e_type": "<type>"}` -- literal from the plan
    text ("Action.set/send/enter/transfer/signal"; "{"enter": "<block>",
    "e_type"}").
  - `{"transfer": "<level>", "amount": ..., "mode": ...}` -- literal from the
    plan text; this action moves an SD LEVEL's quantity via
    `LevelSystem.move`, not an entity or agent (the plan is explicit: "`
    transfer` uses a `LevelSystem.move(level, amount, mode)` factored out of
    the TRANSFER block"). Because of that, "agents transferred out to DES
    blocks" is not literally what `transfer` does per the plan text; test 5
    below tests `enter` conservation only, and flags this reading in its own
    docstring rather than inventing an entity-transfer semantics the plan
    does not describe.
  - `{"send": "<message-name>", "to": id | {"population": ..., "where": ...}
    | {"population": ..., "random": true}, "payload": ...}` -- the plan gives
    `{"send", "to": ..., "payload"}` without naming what the bare `"send"`
    key holds; by the same pattern as `"enter": "<block>"` and
    `"transfer": "<level>"` (the action-type key holds its own primary
    target/name), `"send"` is assumed to hold the message name that a
    receiving transition's `"message"` trigger matches against (the plan's
    own SIR snippet: `{"from": "S", "to": "I", "message": "infect", ...}`).
  - `{"signal": "<name>"}` -- same pattern, matching a HOLD block's
    `signal_name` (`core/blocks/signal.py`, `core/blocks/hold.py`, already
    shipped and reused here unmodified).
  - SEND block: `type: "SEND"`, `params: {"message": ..., "to": ...
    (default "entity.agent"), "payload": ...}` (plan text, "Coupling to DES
    and SD").
  - Dropped-message counter: the plan says only "counted", not where.
    Assumed `env.ctx.agent_system.dropped` (a plain int), the simplest shape
    consistent with `AgentSystem`'s other plain counters/accessors in AB-1.
    This is the one place a landed engine's real key name may need a
    one-line swap; nothing about test 3's LOGIC depends on the guess.
"""
import numpy as np
import pytest

from conftest import chain, create, delay, dispose, layout as make_layout, problem_codes, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _sim(time_limit):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1}


def _agent_layout(blocks=(), links=(), **sections):
    """A blockless agent-only model unless *blocks*/*links* are given (see
    PHASE5_PLAN "Blockless models must validate")."""
    return make_layout(list(blocks), list(links), **sections)


def _item(name="Item", **over):
    d = {"name": name, "parameters": {}, "attributes": [],
         "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
         "transitions": [{"from": "A", "to": "B", "rate": "0.5"}]}
    if "states" in over and "initial_state" not in over:
        over["initial_state"] = over["states"][0]["name"]
    d.update(over)
    return d


# ============================================================ 1. same-instant
def test_message_cascade_resolves_at_the_exact_same_instant():
    """A(t=3, timeout) sends "m1" to the one Bee, whose message transition
    sends "m2" to the one Checker, whose own message transition then fires
    too -- all three hops stamped 3.0, not 3.0 + a tick each. The rule relied
    on: "Messages: FIFO drained synchronously at the same instant" (PHASE5_
    PLAN, Phase 5B intro) -- delivery and the receiving transition happen
    within the SAME simulated instant as the send, not deferred to a later
    pass, so a 2-hop cascade fully resolves before the clock is allowed to
    move past 3. Ties within an instant break "(time, population, index,
    transition)" (same section) -- irrelevant here (one agent per population)
    but it is the rule that guarantees the cascade order is deterministic.
    An UNRELATED DES arrival is scheduled for the same instant (CREATE with
    start_time=3 -> DISPOSE) to show the cascade does not need to wait for,
    or get preempted by, other same-tick DES activity: both land on the same
    float 3.0. (Sub-instant firing order between independent branches is not
    otherwise observable from model state -- the exact-equality check on 3.0
    for every hop, plus the unrelated branch also landing on 3.0, is the
    strongest independently checkable form of "at the same instant".)
    """
    sender = _item("Sender", states=[{"name": "Wait"}, {"name": "Done"}],
                   transitions=[{"from": "Wait", "to": "Done", "timeout": "3",
                                "actions": [{"send": "m1",
                                            "to": {"population": "Bees", "random": True},
                                            "payload": "0"}]}])
    bee = _item("Bee", states=[{"name": "Idle"}, {"name": "Done"}],
               transitions=[{"from": "Idle", "to": "Done", "message": "m1",
                            "actions": [{"send": "m2",
                                        "to": {"population": "Checkers", "random": True},
                                        "payload": "0"}]}])
    checker = _item("Checker", states=[{"name": "Idle"}, {"name": "Done"}],
                    transitions=[{"from": "Idle", "to": "Done", "message": "m2"}])

    unrelated = create("marker", start_time=3, max_arrivals=1)
    out = dispose("out")

    layout = _agent_layout(
        blocks=[unrelated, out], links=chain(unrelated, out),
        agent_types=[sender, bee, checker],
        populations=[{"name": "Senders", "type": "Sender", "count": 1,
                     "initial": [{"state": "Wait", "count": 1}]},
                    {"name": "Bees", "type": "Bee", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]},
                    {"name": "Checkers", "type": "Checker", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]}],
        simulation=_sim(10.0))
    env = _run(layout, 10.0)

    assert float(_pop(env, "Bees").entered[0]) == 3.0
    assert float(_pop(env, "Checkers").entered[0]) == 3.0
    entities = list(env.ctx.all_entities)
    assert len(entities) == 1
    assert entities[0].deletion_time == 3.0


# ==================================================================== 2. FIFO
def test_three_same_instant_messages_are_drained_in_fifo_order():
    """Three Senders (index 0,1,2), same constant timeout=3, each send "ping"
    with payload = its own id + 1 (so payloads 1, 2, 3). Tie rule relied on:
    "Ties fire in (time, population, index, transition) order" (PHASE5_PLAN,
    Phase 5B intro) -- within the Senders population at the t=3 tie, index
    order is 0, 1, 2, so the messages enqueue (and, per "FIFO drained
    synchronously") are delivered in that same order: payload 1, then 2,
    then 3. The one Receiver accumulates them positionally,
    `self.acc = self.acc*10 + payload`, on a self-loop transition (re-enters
    its own listening state) -- a base-10 encoding where every one of the 6
    possible orderings of {1,2,3} gives a distinct final value, so the exact
    final value (123) is proof of the exact order, not just consistency with
    it (e.g. order 2,1,3 would give 213; order 3,2,1 would give 321)."""
    sender = _item("Sender", states=[{"name": "Wait"}, {"name": "Done"}],
                   transitions=[{"from": "Wait", "to": "Done", "timeout": "3",
                                "actions": [{"send": "ping",
                                            "to": {"population": "Receivers", "random": True},
                                            "payload": "self.id + 1"}]}])
    receiver = _item("Receiver", attributes=[{"name": "acc", "init": "0"}],
                     states=[{"name": "Idle"}],
                     transitions=[{"from": "Idle", "to": "Idle", "message": "ping",
                                  "actions": [{"target": "self.acc",
                                              "expr": "self.acc * 10 + payload"}]}])
    layout = _agent_layout(
        agent_types=[sender, receiver],
        populations=[{"name": "Senders", "type": "Sender", "count": 3,
                     "initial": [{"state": "Wait", "count": 3}]},
                    {"name": "Receivers", "type": "Receiver", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]}],
        simulation=_sim(5.0))
    env = _run(layout, 5.0)
    receivers = _pop(env, "Receivers")
    assert float(receivers.attrs["acc"][0]) == 123.0
    assert float(receivers.entered[0]) == 3.0


# ============================================================= 3. dropped
def test_dropped_counter_counts_only_unmatched_messages():
    """5 Ghosts (timeout=1) send "ghost" to the one Target, which has NO
    transition triggered by "ghost" in any state -- all 5 must be dropped and
    counted. 1 Pinger (timeout=2, after the ghosts) sends "ping", which DOES
    match (Idle -> Done) -- that one must NOT be in the dropped count. Per
    the plan: "unmatched ones dropped and counted" (Phase 5B intro)."""
    ghost = _item("Ghost", states=[{"name": "Wait"}, {"name": "Done"}],
                  transitions=[{"from": "Wait", "to": "Done", "timeout": "1",
                               "actions": [{"send": "ghost",
                                           "to": {"population": "Targets", "random": True},
                                           "payload": "0"}]}])
    pinger = _item("Pinger", states=[{"name": "Wait"}, {"name": "Done"}],
                   transitions=[{"from": "Wait", "to": "Done", "timeout": "2",
                                "actions": [{"send": "ping",
                                            "to": {"population": "Targets", "random": True},
                                            "payload": "0"}]}])
    target = _item("Target", states=[{"name": "Idle"}, {"name": "Done"}],
                   transitions=[{"from": "Idle", "to": "Done", "message": "ping"}])
    layout = _agent_layout(
        agent_types=[ghost, pinger, target],
        populations=[{"name": "Ghosts", "type": "Ghost", "count": 5,
                     "initial": [{"state": "Wait", "count": 5}]},
                    {"name": "Pingers", "type": "Pinger", "count": 1,
                     "initial": [{"state": "Wait", "count": 1}]},
                    {"name": "Targets", "type": "Target", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]}],
        simulation=_sim(5.0))
    env = _run(layout, 5.0)
    assert int(_pop(env, "Targets").state[0]) == 1     # Done: the ping matched
    assert int(env.ctx.agent_system.dropped) == 5      # only the 5 ghosts


# =========================================================== 4. SEND block
def test_send_block_delivers_exactly_k_messages_at_the_entities_pass_times():
    """CREATE(K=4, interarrival=2, start_time=0) -> SEND("tick", to Watchers)
    -> DISPOSE: entities are created at 0, 2, 4, 6 (the established
    CREATE convention: first arrival AT start_time, then +interarrival each
    -- see test_agents_oracles.py's golden test) and SEND is a zero-time
    block, so all 4 messages land at those same instants. The Watcher counts
    them on a self-loop (`self.n += 1`); final n must be exactly K, and its
    last update time must be the last entity's pass time (6.0)."""
    K, T = 4, 2.0
    c = create("arrivals", max_arrivals=K, start_time=0,
              distribution="Constant", dist_params={"value": T})
    send = {"name": "notify", "description": "notify", "type": "SEND", "position": [1, 0],
           "params": {"message": "tick", "to": {"population": "Watchers", "random": True},
                      "payload": "entity.id"}}
    out = dispose("out")
    watcher = _item("Watcher", attributes=[{"name": "n", "init": "0"}],
                    states=[{"name": "Idle"}],
                    transitions=[{"from": "Idle", "to": "Idle", "message": "tick",
                                 "actions": [{"target": "self.n", "expr": "self.n + 1"}]}])
    layout = _agent_layout(
        blocks=[c, send, out], links=chain(c, send, out),
        agent_types=[watcher],
        populations=[{"name": "Watchers", "type": "Watcher", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]}],
        simulation=_sim(10.0))
    env = _run(layout, 10.0)
    watchers = _pop(env, "Watchers")
    assert float(watchers.attrs["n"][0]) == float(K)
    assert float(watchers.entered[0]) == (K - 1) * T
    assert len(list(env.ctx.all_entities)) == K


# ================================================ 5. enter conservation
def test_enter_conservation_at_several_checkpoints():
    """5 Workers, agent i spawns (Idle -> Spawned, `enter`) at t = i+1 (the
    "enter" action, per the plan, "spawns an entity through spawn_entity...
    calls block.self_run", straight into a named block -- here DELAY(2) ->
    DISPOSE, no CREATE block). Note on scope: the plan places `transfer` on
    SD LEVELS ("`transfer` uses `LevelSystem.move`", not an entity/agent
    operation), so this test exercises `enter` only -- it does not attempt an
    entity-side "transfer" semantics the plan text does not describe.

    Conservation check, at 5 independent checkpoints (run lengths 2,4,6,8,10
    -- a fresh run to each length, since nothing here can pause mid-run):
    (agents still Idle, read from the AGENT population's own state array) +
    (entities in system + disposed, read from the DES entity log) must equal
    5, the population size FIXED by the test, independently of both sources.
    This is a real cross-check, not algebra that holds by construction: a
    double-spawn or a missed spawn on the `enter` boundary desyncs the two
    independently-computed sides and breaks the sum against the known
    constant 5."""
    N = 5
    spawn_times = [i + 1.0 for i in range(N)]      # 1..5
    proc_time = 2.0
    worker = _item("Worker", states=[{"name": "Idle"}, {"name": "Spawned"}],
                   transitions=[{"from": "Idle", "to": "Spawned", "timeout": "self.id + 1",
                                "actions": [{"enter": "process", "e_type": "WorkerEntity"}]}])
    d = delay("process", value=proc_time)
    out = dispose("done")
    layout = _agent_layout(
        blocks=[d, out], links=chain(d, out),
        agent_types=[worker],
        populations=[{"name": "Workers", "type": "Worker", "count": N,
                     "initial": [{"state": "Idle", "count": N}]}],
        simulation=_sim(10.0))

    for run_len in (2.0, 4.0, 6.0, 8.0, 10.0):
        env = _run(layout, run_len)
        workers = _pop(env, "Workers")
        remaining_agents = int((workers.state == workers.type.index["Idle"]).sum())
        entities = list(env.ctx.all_entities)
        in_system = sum(1 for e in entities if e.deletion_time is None)
        disposed = sum(1 for e in entities if e.deletion_time is not None)
        assert remaining_agents + in_system + disposed == N, run_len

        # independent, hand-derived cross-check of each term (not just the sum)
        expected_spawned = sum(1 for t in spawn_times if t <= run_len)
        expected_disposed = sum(1 for t in spawn_times if t + proc_time <= run_len)
        assert N - remaining_agents == expected_spawned, run_len
        assert len(entities) == expected_spawned, run_len
        assert disposed == expected_disposed, run_len


# =========================================================== 6. signal
def test_agent_signal_action_releases_every_held_entity():
    """3 entities arrive at a HOLD (mode "Signal", signal_name "release") at
    t=0,1,2 and wait indefinitely (existing `core/blocks/hold.py`,
    unmodified: "wait for a signal", `ctx.signals[name]`). One Signaler agent
    fires an action `{"signal": "release"}` at t=5 (timeout). All 3 held
    entities must release at exactly 5.0 -- the HOLD's own `signal.succeed()`
    broadcast semantics (`core/blocks/signal.py`), now reachable from an
    agent action instead of only a SIGNAL block."""
    c = create("arrivals", max_arrivals=3, start_time=0,
              distribution="Constant", dist_params={"value": 1.0})
    gate = {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
           "params": {"mode": "Signal", "signal_name": "release"}}
    out = dispose("out")
    signaler = _item("Signaler", states=[{"name": "Idle"}, {"name": "Done"}],
                     transitions=[{"from": "Idle", "to": "Done", "timeout": "5",
                                  "actions": [{"signal": "release"}]}])
    layout = _agent_layout(
        blocks=[c, gate, out], links=chain(c, gate, out),
        agent_types=[signaler],
        populations=[{"name": "Signalers", "type": "Signaler", "count": 1,
                     "initial": [{"state": "Idle", "count": 1}]}],
        simulation=_sim(10.0))
    env = _run(layout, 10.0)
    entities = list(env.ctx.all_entities)
    assert len(entities) == 3
    assert all(e.deletion_time == 5.0 for e in entities)


# ========================================= 7. grammar / sandbox
def test_agent_reads_the_addressed_index_not_a_neighbour():
    """`AGENT("Vals", i).attr` must read agent i's own column, not agent 0's
    by default. Two Vals agents, attribute `x` initialised to `self.id` (0
    and 1, no RNG involved), and one Reader whose condition is
    `AGENT("Vals", 1).x > 0.5` -- true only if index 1's x (=1) is read; if
    the engine mis-addressed to index 0 (x=0) the condition would be false
    for the entire run and the Reader would never move. The condition is
    static (reads no TNOW/STEP/level), so it must be resolved at start and
    the Reader should already be in "B" as of t=0."""
    vals = _item("Vals", attributes=[{"name": "x", "init": "self.id"}],
                states=[{"name": "Only"}], transitions=[])
    reader = _item("Reader", states=[{"name": "A"}, {"name": "B"}],
                   transitions=[{"from": "A", "to": "B",
                                "condition": 'AGENT("Vals", 1).x > 0.5'}])
    layout = _agent_layout(
        agent_types=[vals, reader],
        populations=[{"name": "Vals", "type": "Vals", "count": 2,
                     "initial": [{"state": "Only", "count": 2}]},
                    {"name": "Readers", "type": "Reader", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}],
        simulation=_sim(5.0))
    env = _run(layout, 5.0)
    readers = _pop(env, "Readers")
    assert int(readers.state[0]) == 1          # B
    assert float(readers.entered[0]) == 0.0


def test_self_set_action_writes_the_addressed_attribute():
    """`self.<attr>` as a set-action TARGET (not just a read, per test 2's
    `self.acc`): a bare timeout transition sets `self.x = 42` once."""
    kind = _item("Setter", attributes=[{"name": "x", "init": "0"}],
                states=[{"name": "A"}, {"name": "B"}],
                transitions=[{"from": "A", "to": "B", "timeout": "1",
                             "actions": [{"target": "self.x", "expr": "42"}]}])
    layout = _agent_layout(
        agent_types=[kind],
        populations=[{"name": "P", "type": "Setter", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}],
        simulation=_sim(3.0))
    env = _run(layout, 3.0)
    assert float(_pop(env, "P").attrs["x"][0]) == 42.0


@pytest.mark.parametrize("expr_field, expr", [
    ("condition", "self.__class__ == self.__class__"),
    ("condition", 'AGENT("P", 0).__dict__ == AGENT("P", 0).__dict__'),
])
def test_sandbox_refuses_dunder_attribute_access_without_evaluating_it(expr_field, expr):
    """`self.<attr>`/`AGENT(...).attr` are resolved through
    `population.column(attr)[i]`, "never `getattr`" (ARCHITECTURE 5.25 /
    PHASE5_PLAN Expressions): `__class__`/`__dict__` are not declared
    attributes and must be refused by VALIDATION (a problem code), never
    reach the interpreter. This only checks `validate_model_structured` --
    the layout is never run, so there is no risk of the expression actually
    executing if the refusal were missing."""
    kind = _item("P", attributes=[{"name": "x", "init": "0"}],
                transitions=[{"from": "A", "to": "B", expr_field: expr}])
    layout = _agent_layout(
        agent_types=[kind],
        populations=[{"name": "P", "type": "P", "count": 1,
                     "initial": [{"state": "A", "count": 1}]}])
    codes = problem_codes(layout)
    assert codes, "a dunder attribute access must raise at least one error-level problem"
    assert codes & {"E_AGENT_ATTRIBUTE", "E_AGENT_EXPRESSION"}, codes
