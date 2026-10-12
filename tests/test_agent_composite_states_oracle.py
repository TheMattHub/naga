"""Independent oracle tests for PHASE6 depth bucket K, milestone K1 ("Hierarchy
engine": composite and history states -- docs/history/PHASE6-DEPTH-K-PLAN-2026-09-27.md,
its "Oracles" section, items 1-7). Every expected value below is a hand derivation
from that spec's "Semantics (binding)" section alone.

Written while another agent implements K1 in `nagasim/core/agents.py` concurrently
(the working tree already carries their in-progress diff to `agents.py` and
`outputs.py` at the time this file was written) -- this file never reads that diff.
The wire format (states carrying `parent`/`initial`, transitions carrying
`history`) is copied verbatim from the plan's own worked example; nothing here was
confirmed against the landed code.

These tests are EXPECTED TO FAIL until K1 lands (no `parent`/`initial`/`history`
support exists yet on HEAD). `PY -m pytest --collect-only -q
tests/test_agent_composite_states_oracle.py` collects cleanly, which is all that is checked now.

ASSUMPTIONS (spec is silent, stated explicitly so a reviewer can challenge them):

A1. Placing an agent directly into a leaf via a population's `initial` list (e.g.
    `{"state": "Idle", "count": 1}` where Idle's parent is a composite) makes that
    composite part of the active configuration from t=0, with its own pending
    transitions armed from t=0 -- the natural reading of "An agent is always in
    exactly one leaf; its active configuration is that leaf and its ancestors,"
    extended to the t=0 initial configuration the same way a flat model's own-rate
    transition is already armed from t=0 for an initially-placed agent. Whether
    the composite's own ENTRY ACTIONS fire at t=0 for this case is NOT tested here
    (oracle 1 below sidesteps it by starting at a top-level leaf instead, where no
    ancestor entry is possible).
A2. `agent_summary`'s composite rows (spec: "agent_summary gets composite rows")
    reuse the same frame/column layout as leaf rows, with the composite's own name
    in the "State" column -- the spec names the frame, not a new column shape.
A3. Per-agent-per-composite history storage is reset on death, so a recycled slot's
    new occupant sees "no record" for every composite (spec's own words: "recycled
    slot after a death" is explicitly listed alongside "first entry" as a
    no-record case) -- oracle 4's last test checks only the externally observable
    outcome (which leaf the recycled slot's newcomer lands in), not how that reset
    is implemented.

Oracle 7's fingerprint (a bundled flat model's `agent_summary`) was captured from
a `git worktree` checkout of HEAD (feef721, pre-K1, before the concurrent agent's
changes), run once, then the worktree was removed -- not from this session's dirty
working tree.
"""
import numpy as np
import pytest

from conftest import layout as make_layout, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _sim(time_limit):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1}


def _agent_layout(**sections):
    return make_layout([], [], **sections)


def _log_action(code):
    """One digit `code` appended to `self.log` (base-10 accumulator, oracle 1)."""
    return {"target": "self.log", "expr": f"self.log * 10 + {code}"}


# =============================================================== 1. action order
def test_action_order_sibling_move_exit_to_top_and_reentry_via_initial():
    """A 2-level chart -- Working{Idle, Busy} (composite, `initial`="Idle") and a
    top-level Broken -- where every state's entry/exit appends its own one-digit
    code to `self.log` (base-10: log = log*10 + code) and every transition's own
    action always appends 9. Codes: Broken entry=7/exit=8, Working entry=5/exit=6,
    Idle entry=1/exit=2, Busy entry=3/exit=4.

    Three standing transition RULES, each a plain constant timeout=1, drive a
    natural repeating cycle: Broken->Working (composite target, no history -> its
    own `initial` descends to Idle), Idle->Busy (a sibling move) and Busy->Broken
    (an exit to the top level). Starting the agent at the top-level leaf Broken
    (not inside Working) makes the very first entry unambiguous (assumption A1
    does not apply: Broken has no ancestor composite).

    Hand-derived firing sequence and the spec's own order rule (exit actions leaf
    -> LCA, excluding LCA; the transition's actions; entry actions LCA -> target
    leaf, including a descent through `initial`):

      t=0  create in Broken:                                  entry Broken (7)
      t=1  Broken->Working (LCA=root; enters Working then,
           via `initial`, Idle):              exit Broken(8), action(9),
                                               entry Working(5), entry Idle(1)
      t=2  Idle->Busy (LCA=Working, sibling
           move -- Working itself is untouched):  exit Idle(2), action(9),
                                                   entry Busy(3)
      t=3  Busy->Broken (LCA=root, exit to
           top level -- leaf-up: Busy then
           Working):                         exit Busy(4), exit Working(6),
                                              action(9), entry Broken(7)
      t=4  Broken->Working again (the same
           rule as t=1 -- a genuine RE-ENTRY,
           Working having been exited at t=3):  exit Broken(8), action(9),
                                                 entry Working(5), entry Idle(1)

    Digit stream: 7, 8,9,5,1, 2,9,3, 4,6,9,7, 8,9,5,1 -> log = 7895129346978951
    (16 digits, well under 2**53 -- exact in float64, no rounding slop). Run to
    4.5 (after the 4th firing, before the NEXT Idle->Busy at t=5) so the log is
    frozen at exactly this value.
    """
    codes = [7, 8, 9, 5, 1, 2, 9, 3, 4, 6, 9, 7, 8, 9, 5, 1]
    expected_log = 0
    for d in codes:
        expected_log = expected_log * 10 + d
    assert expected_log == 7895129346978951          # sanity on the fold itself

    atype = {
        "name": "Machine", "parameters": {}, "attributes": [{"name": "log", "init": "0"}],
        "initial_state": "Broken",
        "states": [
            {"name": "Working", "initial": "Idle", "entry": [_log_action(5)], "exit": [_log_action(6)]},
            {"name": "Idle", "parent": "Working", "entry": [_log_action(1)], "exit": [_log_action(2)]},
            {"name": "Busy", "parent": "Working", "entry": [_log_action(3)], "exit": [_log_action(4)]},
            {"name": "Broken", "entry": [_log_action(7)], "exit": [_log_action(8)]},
        ],
        "transitions": [
            {"from": "Broken", "to": "Working", "timeout": "1", "actions": [_log_action(9)]},
            {"from": "Idle", "to": "Busy", "timeout": "1", "actions": [_log_action(9)]},
            {"from": "Busy", "to": "Broken", "timeout": "1", "actions": [_log_action(9)]},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 1, "initial": [{"state": "Broken", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(4.5))

    env = _run(layout, 4.5)
    pop_rt = _pop(env, pop_name)
    assert float(pop_rt.attrs["log"][0]) == pytest.approx(expected_log, rel=1e-12)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Idle"   # landed back in Idle after t=4


# ======================================================== 2. surviving timeout
def test_composite_timeout_survives_inner_sibling_moves_and_fires_at_exactly_ten():
    """Working{Idle, Busy} (composite `initial`="Idle") has its OWN timeout=10 to
    a top-level Done; Idle<->Busy cycle on their own timeout=3 the whole time the
    agent is inside Working. Spec: "A move between siblings inside S leaves S's
    timeout/rate untouched" -- so Working's own pending timeout, armed once when
    Working became active (t=0, assumption A1: the agent starts placed in the leaf
    Idle), must fire at exactly t=10 regardless of how many Idle<->Busy flips
    happened in between, not restart on each one.

    Hand-derived inner cycle (timeout=3, starting Idle at t=0): Idle->Busy@3,
    Busy->Idle@6, Idle->Busy@9 -- three flips, all strictly before t=10, at which
    point Working's own timeout preempts the next flip (which would be at t=12)
    and the agent lands in Done with `entered`==10.0 exactly (a plain constant
    timeout, no draw, no floating-point slop: 0+10==10.0 bit-exact).

    Discriminator: a buggy implementation that restarts Working's own timeout on
    every inner move would instead fire at 9+10=19 (last flip before a naive
    restart was at t=9) -- a materially different, non-Done, still-cycling result
    at t=11, not a rounding-level difference.
    """
    atype = {
        "name": "Machine", "parameters": {}, "attributes": [],
        "initial_state": "Idle",
        "states": [
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Busy", "parent": "Working"},
            {"name": "Done"},
        ],
        "transitions": [
            {"from": "Working", "to": "Done", "timeout": "10"},
            {"from": "Idle", "to": "Busy", "timeout": "3"},
            {"from": "Busy", "to": "Idle", "timeout": "3"},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 1, "initial": [{"state": "Idle", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(11.0))

    env = _run(layout, 11.0)
    pop_rt = _pop(env, pop_name)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Done"
    assert float(pop_rt.entered[0]) == pytest.approx(10.0, rel=1e-12)


# ============================================================ 3. rate on a composite (slow)
@pytest.mark.slow
def test_composite_rate_time_to_first_breakdown_is_exponential_regardless_of_inner_moves():
    """Working{Idle, Busy} has a composite-level own-rate breakdown Working->Broken
    at a constant rate `lam`, while Idle<->Busy cycle on a fixed timeout the whole
    time. The spec's own-rate machinery (per-agent CRN, AB-1) makes each agent's
    time-to-breakdown Exp(lam) when the rate is evaluated from a stable state; the
    binding claim under test is that a composite-level rate is NO DIFFERENT: moving
    between Idle and Busy must not pause, reset, double-count or otherwise distort
    the accumulated hazard, so across many agents the empirical breakdown-time
    distribution must still be Exp(lam) -- checked with a one-sample KS test
    against `scipy.stats.expon(scale=1/lam)` rather than an exact per-agent
    rebuild (an exact CRN rebuild would not by itself rule out every way a
    composite-vs-leaf hazard bookkeeping bug could show up; the marginal
    distribution is a robustness check independent of exactly how the engine
    keys the composite rate's own stream).

    M=200 agents, lam=0.5, run_len=20 (P(Exp(0.5) > 20) = exp(-10) ~ 4.5e-5, so
    censoring is negligible for M=200 -- expected < 0.01 agents truncated). Inner
    Idle<->Busy cycle every 1.0 time units the whole run, purely to exercise the
    "moves happen constantly" clause; it plays no role in the expected
    distribution. Marked slow (many agents); kept small for a ~1-2 s cost."""
    from scipy import stats

    M, lam, run_len = 200, 0.5, 20.0
    atype = {
        "name": "Machine", "parameters": {"lam": lam}, "attributes": [],
        "initial_state": "Idle",
        "states": [
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Busy", "parent": "Working"},
            {"name": "Broken"},
        ],
        "transitions": [
            {"from": "Working", "to": "Broken", "rate": "lam"},
            {"from": "Idle", "to": "Busy", "timeout": "1"},
            {"from": "Busy", "to": "Idle", "timeout": "1"},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": M,
           "initial": [{"state": "Idle", "count": M}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(run_len))

    env = _run(layout, run_len, seed=0)
    pop_rt = _pop(env, pop_name)
    broken_idx = pop_rt.type.index["Broken"]
    times = [float(t) for t, s in zip(pop_rt.entered, pop_rt.state) if int(s) == broken_idx]
    assert len(times) >= int(0.99 * M)          # negligible censoring at run_len=20

    result = stats.kstest(times, "expon", args=(0, 1.0 / lam))
    assert result.pvalue > 1e-3


# ==================================================================== 4. history
def _history_model(history, first_history=None):
    """A 3-level chart -- Out(top) <-> Working{Idle, Busy{Fast, Slow}} -- shared
    by every history test. `history` is the flag on the RE-entering transition
    (after Working has been exited once); `first_history` (default: same as
    `history`) is the flag on the very FIRST Out->Working transition, where no
    record can exist yet regardless of the flag's value.

    Fixed timeline (every timeout below is a plain constant=1, no draw):
      t=0  create in Out
      t=1  Out->Working  (first_history; no record yet -> `initial`): Idle
      t=2  Idle->Busy     (Busy is itself composite, `initial`="Fast"): Fast
      t=3  Fast->Slow                                                : Slow
      t=4  Working->Out  (Working's own timeout=3 from its t=1 entry;
           exits recording Working's history shallow=Busy, deep=Slow,
           and Busy's own history shallow=deep=Slow)                 : Out
      t=5  Out->Working again (history): shallow -> Busy's `initial` (Fast);
                                          deep    -> the recorded leaf (Slow);
                                          no history key at all -> `initial` (Idle)
    """
    fh = history if first_history is None else first_history
    atype = {
        "name": "Machine", "parameters": {}, "attributes": [],
        "initial_state": "Out",
        "states": [
            {"name": "Out"},
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Busy", "parent": "Working", "initial": "Fast"},
            {"name": "Fast", "parent": "Busy"},
            {"name": "Slow", "parent": "Busy"},
        ],
        "transitions": [
            # Guards keep the two Out->Working transitions apart: both are armed
            # on every entry to Out, and a tie goes to declaration order.
            dict(_history_transition("Out", "Working", "1", fh), guard="TNOW < 2"),
            {"from": "Idle", "to": "Busy", "timeout": "1"},
            {"from": "Fast", "to": "Slow", "timeout": "1"},
            {"from": "Working", "to": "Out", "timeout": "3"},
            dict(_history_transition("Out", "Working", "1", history), guard="TNOW >= 2"),
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 1, "initial": [{"state": "Out", "count": 1}]}
    return _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(6.0)), pop_name


def _history_transition(src, dst, timeout, history):
    out = {"from": src, "to": dst, "timeout": timeout}
    if history is not None:
        out["history"] = history
    return out


def test_history_first_entry_always_uses_initial_regardless_of_the_flag():
    """Before Working has ever been exited, `history` (any value) has no record
    to use -- both variants below must land in Idle (Working's own `initial`) at
    the very first entry, t=1."""
    for history in ("shallow", "deep"):
        layout, pop_name = _history_model(history)
        env = _run(layout, 1.5)
        pop_rt = _pop(env, pop_name)
        assert pop_rt.type.states[int(pop_rt.state[0])] == "Idle"


def test_history_shallow_reenters_the_recorded_child_then_its_own_initial():
    """After Working is exited once (Working's history: shallow child=Busy) and
    re-entered with history="shallow", the engine descends to Busy, then to
    BUSY'S OWN `initial` (Fast) -- shallow does not recurse into Busy's own
    recorded history. Final leaf at t=5.5 (after the 2nd Out->Working, before
    any further transition) must be Fast, not Slow."""
    layout, pop_name = _history_model("shallow")
    env = _run(layout, 5.5)
    pop_rt = _pop(env, pop_name)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Fast"


def test_history_deep_reenters_the_exact_recorded_leaf():
    """Same run with history="deep": Working's deep record is the exact leaf
    Slow, so the 2nd Out->Working must land directly in Slow at t=5.5, entry
    actions running for every state on the path (Working, Busy, Slow) -- not
    just checked here (state order is oracle 1's job), only the final leaf."""
    layout, pop_name = _history_model("deep")
    env = _run(layout, 5.5)
    pop_rt = _pop(env, pop_name)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Slow"


def test_history_absent_on_the_transition_always_uses_initial():
    """Same chart, but the RE-entering transition carries no `history` key at
    all (the first one still needs some value to be unambiguous about the
    first-entry case, tested separately above, so it is fixed to "deep" here --
    irrelevant, since no record exists at t=1 either way). Even though Working's
    history record (shallow=Busy, deep=Slow) exists by t=4, a transition without
    `history` must ignore it and land in Idle at t=5.5 -- the plain UML default,
    which is what makes "shallow"/"deep" an opt-in on the TRANSITION, not a
    standing property of the composite."""
    layout, pop_name = _history_model(None, first_history="deep")
    env = _run(layout, 5.5)
    pop_rt = _pop(env, pop_name)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Idle"


def test_history_on_a_recycled_slot_never_inherits_the_dead_occupants_record():
    """Two agents share one population/type. Agent0 (slot 0) goes Idle->Busy->Fast
    ->Slow (each timeout=1, landing in Slow at t=2) then dies via a composite-level
    Working->Broken timeout=5 (fires at t=0+5=5, from wherever it is -- Slow --
    with a {"remove": "self"} action), which exits Working and records ITS
    history (deep=Slow) for slot 0 the instant before the slot goes free.

    Agent1 (slot 1) sits in SpawnLoop the whole time and self-loops on timeout=6,
    spawning ONE new Machine (state "Enter") at t=6 -- by then slot 0 is the only
    free slot, so the newcomer occupies it. Enter->Working fires immediately
    (timeout=0, same instant) with history="deep". Per assumption A3, a recycled
    slot's history is reset on death, so the newcomer -- which has never been
    inside Working before -- must fall back to `initial` (Idle), NOT the dead
    agent0's recorded Slow (the two are unambiguously different leaves: this is
    a real discriminator, not a coincidence).
    """
    atype = {
        "name": "Machine", "parameters": {}, "attributes": [],
        "initial_state": "Idle",
        "states": [
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Busy", "parent": "Working", "initial": "Fast"},
            {"name": "Fast", "parent": "Busy"},
            {"name": "Slow", "parent": "Busy"},
            {"name": "Broken"},
            {"name": "SpawnLoop"},
            {"name": "Enter"},
        ],
        "transitions": [
            {"from": "Idle", "to": "Busy", "timeout": "1"},
            {"from": "Fast", "to": "Slow", "timeout": "1"},
            {"from": "Working", "to": "Broken", "timeout": "5", "actions": [{"remove": "self"}]},
            {"from": "SpawnLoop", "to": "SpawnLoop", "timeout": "6",
             "actions": [{"spawn": "Machines", "count": "1", "state": "Enter"}]},
            {"from": "Enter", "to": "Working", "timeout": "0", "history": "deep"},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 2, "max_count": 2,
           "initial": [{"state": "Idle", "count": 1}, {"state": "SpawnLoop", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(6.4))

    env = _run(layout, 6.4)
    pop_rt = _pop(env, pop_name)
    assert env.ctx.agent_system.count(pop_name) == 2       # respawn restored the count
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Idle"       # slot 0: the newcomer
    assert pop_rt.type.states[int(pop_rt.state[1])] == "SpawnLoop"  # slot 1: agent1, untouched


# =============================================================== 5. priority
def test_leaf_condition_wins_over_its_parents_condition_at_the_same_instant():
    """Working{Idle} has a composite-level condition Working->Retired
    ("TNOW >= 5"); Idle (its child) has its OWN condition Idle->Standby, the SAME
    literal condition. Both become true at the identical instant t=5 -- spec's
    priority rule ("those from the leaf win over those from its parent") means
    the leaf's transition (Idle->Standby) must fire, not the parent's
    (Working->Retired). Final state at t=8 must be Standby, entered==5.0 exactly
    (both are literal TNOW comparisons, no draw)."""
    atype = {
        "name": "Machine", "parameters": {}, "attributes": [],
        "initial_state": "Idle",
        "states": [
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Retired"},
            {"name": "Standby"},
        ],
        "transitions": [
            {"from": "Working", "to": "Retired", "condition": "TNOW >= 5"},
            {"from": "Idle", "to": "Standby", "condition": "TNOW >= 5"},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 1, "initial": [{"state": "Idle", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(8.0))

    env = _run(layout, 8.0)
    pop_rt = _pop(env, pop_name)
    assert pop_rt.type.states[int(pop_rt.state[0])] == "Standby"
    assert float(pop_rt.entered[0]) == pytest.approx(5.0, rel=1e-12)


# ============================================================= 6. statistics
def test_composite_avg_count_equals_sum_of_leaves_and_entries_count_boundary_only():
    """Working{Idle, Busy} cycles Idle<->Busy on timeout=0.7 the whole time it is
    active; the agent starts in the top-level Out, ping-pongs Out<->Working twice
    (Out->Working timeout=1, Working->Out timeout=5 measured from Working's own
    entry) over a run of 7.5 time units:

      t=0    Out
      t=1    Out->Working: Idle (Working's own exit-timer armed for t=1+5=6)
      t=1.7, 2.4, 3.1, 3.8, 4.5, 5.2, 5.9   Idle<->Busy flips (7 of them, all < 6;
             the 8th would be at 6.6 > 6, preempted)
      t=6    Working->Out (Working's own timer; NOT reset by the 7 sibling
             flips above -- this is the same claim oracle 2 checks, restated
             here for the statistics)
      t=7    Out->Working again: Idle
      t=7.5  run ends (0.5 into the 2nd Working visit, before its own next flip
             at 7.7)

    Two independent, hand-derivable claims:
    (a) time-in-Working = [1, 6) union [7, 7.5) = 5.0 + 0.5 = 5.5 out of 7.5, and
        since Idle/Busy are mutually exclusive and jointly cover every instant
        the agent is in Working, Avg Count(Working) must equal EXACTLY
        Avg Count(Idle) + Avg Count(Busy) read from the same frame (a structural
        identity, not a coincidence of these particular numbers) -- and equal
        5.5/7.5 in absolute terms.
    (b) Entries(Working) must be exactly 2 (the two Out->Working crossings, at
        t=1 and t=7) -- a "boundary crossings only" count -- while the 7 inner
        flips plus the 2 composite entries give Entries(Idle)+Entries(Busy) = 9
        (Idle: t=1, 2.4, 3.8, 5.2, 7 = 5; Busy: t=1.7, 3.1, 4.5, 5.9 = 4), a much
        larger number that an inner-move-counts-as-entry bug would leak into
        Working's own count too.
    """
    from nagasim.core.outputs import summarize_agents

    atype = {
        "name": "Machine", "parameters": {}, "attributes": [],
        "initial_state": "Out",
        "states": [
            {"name": "Out"},
            {"name": "Working", "initial": "Idle"},
            {"name": "Idle", "parent": "Working"},
            {"name": "Busy", "parent": "Working"},
        ],
        "transitions": [
            {"from": "Out", "to": "Working", "timeout": "1"},
            {"from": "Working", "to": "Out", "timeout": "5"},
            {"from": "Idle", "to": "Busy", "timeout": "0.7"},
            {"from": "Busy", "to": "Idle", "timeout": "0.7"},
        ],
    }
    pop_name = "Machines"
    pop = {"name": pop_name, "type": "Machine", "count": 1, "initial": [{"state": "Out", "count": 1}]}
    layout = _agent_layout(agent_types=[atype], populations=[pop], simulation=_sim(7.5))

    env = _run(layout, 7.5)
    frame = summarize_agents(env, warmup=0.0, run_id=None)

    def _row(state):
        sub = frame[(frame["Population"] == pop_name) & (frame["State"] == state)]
        assert len(sub) == 1, f"expected exactly one {state!r} row, got {len(sub)}"
        return sub.iloc[0]

    working, idle, busy = _row("Working"), _row("Idle"), _row("Busy")
    assert working["Avg Count"] == pytest.approx(5.5 / 7.5, rel=1e-9)
    assert working["Avg Count"] == pytest.approx(idle["Avg Count"] + busy["Avg Count"], rel=1e-9)
    assert int(working["Entries"]) == 2
    assert int(idle["Entries"]) + int(busy["Entries"]) == 9


# ==================================================== 7. flat-model invariance
def test_a_bundled_flat_agents_model_is_unchanged_after_k1():
    """`nagasim/models/agents_random_walker.json` (a flat, pre-existing model --
    no `parent`/`initial`/`history` anywhere) must give byte-identical
    `agent_summary` numbers before and after K1 (spec: "Flat models are unchanged
    byte-for-byte -- same results, same draws"). The fingerprint below was
    captured from a `git worktree` checkout of HEAD (feef721, pre-K1) via
    `nagasim.api.run_model(..., replications=1, warmup=0, seed=0)`, read once,
    then the worktree was removed -- not from this session's dirty working tree,
    which the concurrent K1 agent has already modified."""
    from nagasim.api import run_model

    res = run_model("nagasim/models/agents_random_walker.json", replications=1, warmup=0, seed=0)
    frame = res["agent_summary"]

    def _row(state):
        sub = frame[(frame["Population"] == "random_walker_1/Walkers") & (frame["State"] == state)]
        assert len(sub) == 1
        return sub.iloc[0]

    moving, arrived = _row("Moving"), _row("Arrived")
    assert moving["Avg Count"] == pytest.approx(0.4, rel=1e-12)
    assert moving["Min"] == 0 and moving["Max"] == 1 and moving["Final"] == 0
    assert int(moving["Entries"]) == 0
    assert moving["Avg Sojourn"] == pytest.approx(40.0, rel=1e-12)
    assert int(moving["Dropped Messages"]) == 0

    assert arrived["Avg Count"] == pytest.approx(0.6, rel=1e-12)
    assert arrived["Min"] == 0 and arrived["Max"] == 1 and arrived["Final"] == 1
    assert int(arrived["Entries"]) == 1
    assert bool(np.isnan(arrived["Avg Sojourn"]))          # still open at run end
    assert int(arrived["Dropped Messages"]) == 0
