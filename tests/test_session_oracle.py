"""Independent oracle for milestone G3 (`nagasim.core.session.SimSession`,
PHASE6 depth bucket G, D133/D134, docs/history/PHASE6-DEPTH-G-PLAN-2026-09-26.md).

Written from the task's fixed API only, before G3 landed: `session.py` does
not exist yet, so `pytest.importorskip` skips this whole module until it
does. Every expected number below is hand-derived from block semantics that
already exist and are unchanged by G3 -- CREATE's arrival timing
(`core/blocks/create.py`: `start_time=0` fires the first arrival immediately,
`start_time=T` waits until T then fires immediately there), DECIDE Condition/
Policy routing (`core/blocks/decide.py`), ASSIGN's variable write
(`core/blocks/assign.py`: `ctx.variables[name] = value`, exactly what
`set_variable` is documented to do), and the resource manager's WAIT rule
(`core/resource_manager.py`: a capacity-reducing blocker outranks every
entity request but only takes a unit once it is actually free, and a
capacity RAISE releases a held blocker back to the front of the queue) --
none of `session.py` itself was read.

Hand derivations
-----------------
(a) Equivalence: CREATE (Constant 1.0, max_arrivals=6, start 0) -> PROCESS
    (Server, capacity 1, Constant service 0.7) -> DISPOSE. Fully deterministic;
    the chunked session and a plain `run_single_simulation` must agree exactly.

(b) set_variable vs ASSIGN: CREATE (Constant 1.0, max_arrivals=6) fires at
    t = 0..5 into a Condition DECIDE on `gate == 1` (initial 1); each branch
    tags the entity's e_type (A / B) before DISPOSE. Flipping `gate` to 0 at
    t=2.5 (session: `set_variable`; comparison model: a second CREATE with
    start_time=2.5, max_arrivals=1, feeding an ASSIGN `gate = 0`) sends
    entities at t=0,1,2 (gate still 1) down A and t=3,4,5 (gate now 0) down B:
    3 and 3.

(c) set_capacity / WAIT rule: Server capacity 2, CREATE (Constant 1.0,
    max_arrivals=4, arrivals at t=0,1,2,3) -> PROCESS (Constant service 4.0).
    A seizes at 0 (free), B seizes at 1 (free), C queues at 2, D queues at 3
    (both units busy). `set_capacity(Server, 1)` at t=2.5 queues ONE blocker;
    it outranks C/D (WAIT rule: it still waits for a free unit, but jumps the
    queue ahead of any entity once one frees). A releases at 0+4=4 -> the
    blocker (not C) takes that unit. B releases at 1+4=5 -> C (next in FIFO
    order behind the now-resolved blocker) seizes, finishing at 5+4=9.
    `set_capacity(Server, 2)` at t=6 releases the blocker's unit -> D (the
    only one left waiting) seizes immediately at t=6, finishing at 6+4=10.
    Exit times sorted by creation: [4, 5, 9, 10].

(d) Decisions: CREATE (Constant 1.0, max_arrivals=6, arrivals/ids 1..6 in
    order -- every context has its own `entity_ids`, starting at 1, D195)
    into a Policy DECIDE (`policy="route"`, 2 links, tagging
    e_type A/B). Answering option 1 for even ids and 0 for odd: ids 1,3,5 ->
    A (3), ids 2,4,6 -> B (3); six decisions, one "decide" game_log entry each.

(e) Replay: no new arithmetic -- a played mixed game (Policy decisions
    routing A/B, plus a `set_variable` flip partway through gating a second,
    Condition DECIDE) must equal its own `SimSession.replay(...)` bit for bit.

(f) Errors: every call in the "Illegal API calls" list below must raise
    ValueError. `decide_1` sits behind a DELAY(2.0) so nothing is pending
    right after construction, regardless of whether __init__ eagerly runs the
    first CREATE batch the way `run_single_simulation` does (`start.self_run()`
    before `step_until`) -- the "nothing pending" case must be genuinely true
    however that construction detail lands.

RESOLVED AMBIGUITIES (GUESS):
  - A `game_log` entry for `s.decide(policy, option)` has `action == "decide"`
    and `value == option` (mirroring the method name/argument); used in (d)
    to check "one entry per decision" and the recorded option sequence.
    Replay (e) never inspects field semantics -- `s.game_log` is only ever
    passed back to `SimSession.replay` opaquely, so it does not depend on
    this guess. # GUESS
  - `set_capacity`'s declared maximum is the resource's registered
    `"capacity"` (2 in case f); requesting above it or below 0 is out of
    range. # GUESS
"""
import pytest

from conftest import create, delay, dispose, layout

session = pytest.importorskip("nagasim.core.session")

from nagasim.core.sim_runner import run_single_simulation  # noqa: E402
from conftest import decide as _decide
from conftest import entity_count as _entity_count
from conftest import block as _block


def _assign_type(name, e_type, position=None):
    return _block(name, "ASSIGN", {"assignments": [{"target": "entity.e_type", "expr": repr(e_type)}]},
                  position)


def _process(name, resource, value, position=None):
    return _block(name, "PROCESS",
                  {"action": "Seize Delay Release", "distribution": "Constant",
                   "dist_params": {"value": value}, "priority": 0, "quantity": 1,
                   "resource_name": resource}, position)


def _exit_times_by_creation(kpi):
    df = kpi["entity_log"].sort_values("Created At")
    return list(df["Exited At"])


# ------------------------------------------------------------- (a) equivalence
def test_advance_in_chunks_matches_a_plain_run():
    doc = layout(
        [create(max_arrivals=6), _process("station", "Server", 0.7), dispose()],
        [{"source": "create_1", "target": "station"}, {"source": "station", "target": "dispose_1"}],
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        simulation={"time_limit": 8, "warmup": 0, "replications": 1})

    s = session.SimSession(doc, seed=0)
    assert s.now == 0
    assert s.horizon == 8
    s.advance(to=1.5)
    s.advance(to=3.3)
    s.advance(to=6.0)
    played = s.finish()

    baseline = run_single_simulation(doc, seed=0, run_len=8, warmup=0)
    for frame in ("system", "resource_summary"):
        assert played[frame].equals(baseline[frame]), f"{frame} differs"


# ------------------------------------------------------ (b) set_variable == ASSIGN
def _gate_layout(extra_blocks=(), extra_links=()):
    blocks = [
        create(max_arrivals=6),
        _decide("decide_1", "Condition", conditions=["gate == 1"]),
        _assign_type("assign_a", "A", [2, 0]),
        _assign_type("assign_b", "B", [2, 1]),
        dispose("dispose_a"), dispose("dispose_b"),
        *extra_blocks,
    ]
    links = [
        {"source": "create_1", "target": "decide_1"},
        {"source": "decide_1", "target": "assign_a"},
        {"source": "decide_1", "target": "assign_b"},
        {"source": "assign_a", "target": "dispose_a"},
        {"source": "assign_b", "target": "dispose_b"},
        *extra_links,
    ]
    return layout(blocks, links, variables=[{"name": "gate", "initial": 1}],
                  simulation={"time_limit": 8, "warmup": 0, "replications": 1})


def test_set_variable_matches_an_equivalent_assign():
    session_doc = _gate_layout()
    s = session.SimSession(session_doc, seed=0)
    s.advance(to=2.5)
    s.set_variable("gate", 0)
    session_kpi = s.finish()

    assign_doc = _gate_layout(
        # CREATE's start_time is int-typed (core/blocks/create.py SPEC), so the
        # 2.5 delay to the flip goes on a DELAY block instead, fired by a
        # trigger entity created at t=0.
        extra_blocks=[create(name="create_2", max_arrivals=1),
                      delay("delay_trigger", 2.5),
                      _block("assign_gate", "ASSIGN", {"assignments": [{"target": "gate", "expr": "0"}]}, [1, 1]),
                      dispose("dispose_trigger")],
        extra_links=[{"source": "create_2", "target": "delay_trigger"},
                     {"source": "delay_trigger", "target": "assign_gate"},
                     {"source": "assign_gate", "target": "dispose_trigger"}])
    assign_kpi = run_single_simulation(assign_doc, seed=0, run_len=8, warmup=0)

    for kpi in (session_kpi, assign_kpi):
        assert _entity_count(kpi, "A") == 3
        assert _entity_count(kpi, "B") == 3


# ------------------------------------------------------- (c) set_capacity / WAIT
def test_set_capacity_follows_the_wait_rule():
    doc = layout(
        [create(max_arrivals=4), _process("station", "Server", 4.0), dispose()],
        [{"source": "create_1", "target": "station"}, {"source": "station", "target": "dispose_1"}],
        resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO"}],
        simulation={"time_limit": 15, "warmup": 0, "replications": 1})

    s = session.SimSession(doc, seed=0)
    s.advance(to=2.5)
    s.set_capacity("Server", 1)
    s.advance(to=6.0)
    s.set_capacity("Server", 2)
    kpi = s.finish()

    assert _exit_times_by_creation(kpi) == [4.0, 5.0, 9.0, 10.0]


# ------------------------------------------------------------------ (d) decisions
def _policy_layout():
    blocks = [
        create(max_arrivals=6),
        _decide("decide_1", "Policy", policy="route"),
        _assign_type("assign_a", "A", [2, 0]), _assign_type("assign_b", "B", [2, 1]),
        dispose("dispose_a"), dispose("dispose_b"),
    ]
    links = [
        {"source": "create_1", "target": "decide_1"},
        {"source": "decide_1", "target": "assign_a"},
        {"source": "decide_1", "target": "assign_b"},
        {"source": "assign_a", "target": "dispose_a"},
        {"source": "assign_b", "target": "dispose_b"},
    ]
    return layout(blocks, links, simulation={"time_limit": 8, "warmup": 0, "replications": 1})


def _play_policy_game(s):
    """Answer every pending decision: option 1 for an even entity id, 0 for
    odd. Returns the ordered list of options handed to `decide`."""
    options = []
    while True:
        state = s.advance()
        if state != "decision":
            return options
        for req in s.pending():
            option = 1 if req["entity_id"] % 2 == 0 else 0
            s.decide(req["policy"], option)
            options.append(option)


def test_policy_decisions_route_by_id_parity():
    s = session.SimSession(_policy_layout(), seed=0)
    options = _play_policy_game(s)
    kpi = s.finish()

    assert options == [0, 1, 0, 1, 0, 1]
    assert _entity_count(kpi, "A") == 3
    assert _entity_count(kpi, "B") == 3

    decide_entries = [e for e in s.game_log if e["action"] == "decide"]
    assert len(decide_entries) == 6
    assert [e["value"] for e in decide_entries] == options


# ---------------------------------------------------------------------- (e) replay
def _mixed_layout():
    """Policy routing (A/B) feeding a second, Condition DECIDE gated on
    `bonus` -- a played game exercising both an edit (set_variable) and
    decisions (decide), per the plan's oracle (e)."""
    blocks = [
        create(max_arrivals=6),
        _decide("decide_1", "Policy", policy="route"),
        _assign_type("assign_a", "A", [2, 0]), _assign_type("assign_b", "B", [2, 1]),
        _decide("decide_gate", "Condition", conditions=["bonus == 0"]),
        dispose("dispose_a_early"), dispose("dispose_a_late"), dispose("dispose_b"),
    ]
    links = [
        {"source": "create_1", "target": "decide_1"},
        {"source": "decide_1", "target": "assign_a"},
        {"source": "decide_1", "target": "assign_b"},
        {"source": "assign_a", "target": "decide_gate"},
        {"source": "decide_gate", "target": "dispose_a_early"},
        {"source": "decide_gate", "target": "dispose_a_late"},
        {"source": "assign_b", "target": "dispose_b"},
    ]
    return layout(blocks, links, variables=[{"name": "bonus", "initial": 0}],
                  simulation={"time_limit": 10, "warmup": 0, "replications": 1})


def test_replay_reproduces_a_mixed_edit_and_decision_game():
    s = session.SimSession(_mixed_layout(), seed=0)
    answered = 0
    while True:
        state = s.advance()
        if state != "decision":
            break
        for req in s.pending():
            s.decide(req["policy"], 1 if req["entity_id"] % 2 == 0 else 0)
            answered += 1
            if answered == 3:
                s.set_variable("bonus", 1)
    played = s.finish()

    replayed = session.SimSession.replay(_mixed_layout(), 0, s.game_log)
    for frame in ("system", "resource_summary", "entity_summary", "entity_log"):
        assert played[frame].equals(replayed[frame]), f"{frame} differs after replay"


# ------------------------------------------------------------------- (f) errors
def _error_layout():
    blocks = [
        create(max_arrivals=2),
        delay("delay_1", 2.0),   # keeps decide_1 unreached until an advance() is made
        _decide("decide_1", "Policy", policy="route"),
        dispose("dispose_a"), dispose("dispose_b"),
    ]
    links = [
        {"source": "create_1", "target": "delay_1"},
        {"source": "delay_1", "target": "decide_1"},
        {"source": "decide_1", "target": "dispose_a"},
        {"source": "decide_1", "target": "dispose_b"},
    ]
    return layout(blocks, links, variables=[{"name": "v", "initial": 0}],
                  resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO"}],
                  simulation={"time_limit": 10, "warmup": 0, "replications": 1})


def test_illegal_calls_raise_value_error():
    s = session.SimSession(_error_layout(), seed=0)

    with pytest.raises(ValueError):
        s.set_variable("nope", 1)
    with pytest.raises(ValueError):
        s.set_constant("nope", 1)
    with pytest.raises(ValueError):
        s.set_capacity("NoSuchResource", 1)
    with pytest.raises(ValueError):
        s.set_capacity("Server", 3)     # above the declared maximum (2)
    with pytest.raises(ValueError):
        s.set_capacity("Server", -1)    # below 0
    with pytest.raises(ValueError):
        s.set_probs("NoSuchBlock", [0.5, 0.5])
    with pytest.raises(ValueError):
        s.decide("route", 0)            # nothing pending yet (still inside delay_1)

    state = s.advance()
    assert state == "decision"
    req = s.pending()[0]
    with pytest.raises(ValueError):
        s.decide(req["policy"], 99)     # option out of range
    with pytest.raises(ValueError):
        s.decide("no-such-policy", 0)   # unknown policy

    s.decide(req["policy"], 0)          # leave the run in a clean, finishable state
    s.finish()
