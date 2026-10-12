"""Independent oracle for milestones L2 (several pending decisions) and L3
(a player capacity cap composed with a schedule/failure), PHASE6.

Written from the task's SPEC only, against the CURRENT (pre-L2/L3) committed
sources -- `nagasim/core/session.py`, `nagasim/core/control.py` and
`nagasim/core/resource_manager.py` were read at their last-committed state
(git status was clean; no diff of the other agent's concurrent work was
read), plus `nagasim/core/schema.py`'s existing `session_driven_resources`/
`_validate_dashboard_controls` (the pre-L3 rule this milestone lifts) and
`tests/test_schedules.py` (capacity_schedule/failure JSON shapes) and
`tests/test_session_oracle.py`/`tests/test_game_oracle.py` (SimSession block/oracle
conventions, reused verbatim below rather than reinvented).

Unlike test_session_oracle.py (gated on `session.py` not existing yet) or
test_game_oracle.py (gated on brand-new method/parameter names), every method
this file calls -- `SimSession.set_capacity`/`decide`/`pending`/`advance`,
`validate_model_structured` -- already exists pre-L2/L3; only its BEHAVIOR
changes. So no `pytest.importorskip`/`skipif` is needed for the file to
collect; a test below simply fails (ValueError/TypeError/AssertionError)
until its milestone lands, which the task brief says is expected.

Hand derivations
-----------------
(1) Schedule + cap composition: Server capacity 2, capacity_schedule
    "shifts" = 2 on [0,10), 1 on [10,20), 2 on [20,inf). CREATE(Constant 1.0,
    max_arrivals=4, arrivals 0,1,2,3) -> PROCESS(Constant 7.0) -- long jobs
    that keep the queue backed up. Player: set_capacity(1) at t=5,
    set_capacity(2) at t=25. Effective(t) = min(S(t), C(t)) (no failure):
    2 on [0,5), 1 on [5,25) (S's own 2->1->2 wiggle at 10/20 never binds,
    since C=1 is already the tighter constraint throughout), 2 on [25,inf).
    By hand: E0 seizes unit A at 0 (free@7), E1 seizes B at 1 (free@8), E2/E3
    queue. At t=5 the cap's blocker queues (both units busy). At t=7 (E0
    exits) the blocker outranks E2/E3 and takes A, holding it through 25. At
    t=8 (E1 exits) E2 (FIFO) takes B, finishes at 8+7=15; E3 then takes B at
    15, finishes at 15+7=22. Nothing else happens at the schedule's own 10/20
    transitions (the composed effective capacity does not change there) --
    this is the discriminating point: an implementation that reduces
    capacity by SUMMING the schedule's and the cap's own independent blocker
    counts (1 + 1 = 2 held, instead of the formula's `min` giving 1 held)
    would instead let a second, schedule-only blocker queue at t=10 and grab
    B ahead of E3 when it frees at 15, releasing it only at t=20 -- E3 would
    exit at 27, not 22. Exit times sorted by creation: [7, 8, 15, 22].
    (The spec also invites an "available" time-average check; skipped here
    -- see GUESS.)

(2) Failure beats the cap (#227): Server capacity 1 (no schedule, so
    S(t) == the declared capacity, 1), a failure with Constant uptime=3.0 /
    downtime=2.0 (down on [3,5)). CREATE(Constant 1.0, max_arrivals=2,
    arrivals 0,1) -> PROCESS(Constant 2.5). Player: set_capacity(0) at t=2,
    set_capacity(1) at t=4. By hand: E0 seizes the only unit at 0 (free@2.5);
    E1 arrives at 1 and queues. At t=2 the cap's blocker (wants 1-0=1 unit)
    queues (the unit is still busy). At 2.5 the blocker outranks E1 and takes
    the unit. At t=3 the failure's OWN blocker also wants the unit but must
    queue behind nothing free. At t=4 the player raises the cap back to 1,
    releasing the cap's blocker -- but the failure's still-waiting blocker
    (queued since 3) outranks E1 and immediately claims the freed unit, so
    E1 still does not run. Only at t=5, when the failure's own downtime ends
    and its blocker releases, does E1 finally seize, finishing at 5+2.5=7.5.
    Exit times: [2.5, 7.5] -- nothing served on [2, 5) despite the cap being
    raised at 4, which is the "failure beats the cap" point.

(3) No set_capacity call: a session on a model with both a capacity_schedule
    and a failure, advanced in chunks and never touched, must equal a plain
    `run_single_simulation` bit for bit -- reusing test_session_oracle.py case
    (a)'s equivalence pattern. Not gated on either milestone: it never
    exercises the new cap mechanism, so it is a pure regression check.

(4) A capacity slider control on a resource with a capacity_schedule must no
    longer raise E_DASHBOARD_CONTROL (the pre-L3 rule read directly out of
    `schema.py`'s `_validate_dashboard_controls`: "resource ... has a
    capacity schedule or a failure, which already sets its capacity in
    force").

(5) Two decisions in one step: CREATE(entities_per_arrival=2, max_arrivals=1)
    -> Policy DECIDE ("route", 2 links) creates entities 1 and 2 at t=0 in
    the same arrival batch, both reaching the DECIDE before any env step
    completes. `pending()` must list 2 requests with distinct, increasing
    "id"s. Resolving the SECOND id with option 1 and then the default (None)
    -- which must resolve whatever is left, i.e. the oldest, trivially,
    since only one request remains -- routes one entity each way: A=1, B=1,
    regardless of which physical entity holds which id (no guess needed for
    that count). `replay` on the resulting `game_log` must reproduce it.

(6) `decide(..., request_id=<unknown>)` on a live pending decision raises
    ValueError.

RESOLVED AMBIGUITIES (GUESS):
  - Cases (1)/(2) assume the cap is implemented the same way `set_capacity`
    already does pre-L3 (its own WAIT-rule blocker pool, sized off the
    resource's declared capacity), now coexisting with the schedule's/
    failure's OWN pre-existing blocker pools, but with the three composed so
    the total held-back count follows the spec's `max(0, min(S, C) - D)`
    rather than summing independent pools. This is inferred from the spec
    formula, not from reading the L3 diff; case (1) is deliberately built so
    a naive per-mechanism sum would give a different, also hand-computable,
    wrong answer (E3 at 27 instead of 22). # GUESS
  - Case (1)'s "available" time-average check (spec: "if easily
    hand-computable") is skipped: it would need assuming a specific
    resource_state/KPI emission point for a resource driven by TWO
    independently-changing mechanisms at once, which is exactly the
    unlanded detail under test. The exit-times check alone is unambiguous.
  - Case (5) does not assume which physical entity (id 1 or id 2) gets the
    lower request id; only that resolving one specific id and then the
    default leaves exactly one entity on each branch.
"""
import pytest

from conftest import chain, create, dispose, layout, process

from nagasim.core import session
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import run_single_simulation


# ---------------------------------------------------------------- block helpers
# Same shapes as tests/test_session_oracle.py / tests/test_game_oracle.py (already
# committed), reproduced here rather than imported, so this file has no
# import-time dependency on either.
def _block(name, btype, params, position=None):
    return {"name": name, "description": name, "type": btype,
            "position": position or [0, 0], "params": params}


def _assign_type(name, e_type, position=None):
    return _block(name, "ASSIGN", {"assignments": [{"target": "entity.e_type", "expr": repr(e_type)}]},
                  position)


def _decide(name, mode, **params):
    base = {"mode": mode, "probs": [], "conditions": [], "policy": "", "observation": [],
            "fallback": "first", "queue_exprs": []}
    base.update(params)
    return _block(name, "DECIDE", base)


def _entity_count(kpi, e_type):
    df = kpi["entity_summary"]
    rows = df[df["Entity Type"] == e_type]
    return int(rows["Total Created"].iloc[0]) if not rows.empty else 0


def _exit_times_by_creation(kpi):
    df = kpi["entity_log"].sort_values("Created At")
    return list(df["Exited At"])


def _error_codes(doc):
    return {p.code for p in validate_model_structured(doc) if p.severity == "error"}


# ======================================================================== L3
# ------------------------------------------------ (1) schedule + cap compose
def _case1_layout():
    blocks = [create(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=4),
              process("station", "Server", 7.0), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO",
                              "capacity_schedule": "shifts"}],
                  schedules=[{"name": "shifts", "kind": "capacity", "repeat": False,
                              "points": [[10, 2], [10, 1], [1000, 2]]}],
                  simulation={"time_limit": 30, "warmup": 0, "replications": 1})


def test_schedule_and_cap_compose_by_min_not_by_summed_blockers():
    s = session.SimSession(_case1_layout(), seed=0)
    s.advance(to=5.0)
    s.set_capacity("Server", 1)
    s.advance(to=25.0)
    s.set_capacity("Server", 2)
    kpi = s.finish()

    assert _exit_times_by_creation(kpi) == pytest.approx([7.0, 8.0, 15.0, 22.0])


# ------------------------------------------------------- (2) failure beats cap
def _case2_layout():
    blocks = [create(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=2),
              process("station", "Server", 2.5), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
                  failures=[{"name": "brk", "resources": ["Server"],
                             "uptime": {"distribution": "Constant", "dist_params": {"value": 3.0}},
                             "downtime": {"distribution": "Constant", "dist_params": {"value": 2.0}}}],
                  simulation={"time_limit": 10, "warmup": 0, "replications": 1})


def test_failure_beats_the_players_cap():
    s = session.SimSession(_case2_layout(), seed=0)
    s.advance(to=2.0)
    s.set_capacity("Server", 0)
    s.advance(to=4.0)
    s.set_capacity("Server", 1)
    kpi = s.finish()

    assert _exit_times_by_creation(kpi) == pytest.approx([2.5, 7.5])


# ------------------------------------------------------- (3) no-op equivalence
def _case3_layout():
    blocks = [create(distribution="Exponential", dist_params={"rate": 1.0}, max_arrivals=20),
              process("station", "Server", 1.25, distribution="Exponential"), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO",
                              "capacity_schedule": "shifts"}],
                  schedules=[{"name": "shifts", "kind": "capacity", "repeat": True,
                              "points": [[10, 2], [10, 1]]}],
                  failures=[{"name": "brk", "resources": ["Server"],
                             "uptime": {"distribution": "Constant", "dist_params": {"value": 8.0}},
                             "downtime": {"distribution": "Constant", "dist_params": {"value": 1.0}}}],
                  simulation={"time_limit": 60, "warmup": 0, "replications": 1})


def test_session_without_set_capacity_matches_a_plain_run():
    doc = _case3_layout()
    s = session.SimSession(doc, seed=0)
    s.advance(to=15.0)
    s.advance(to=40.0)
    kpi = s.finish()

    baseline = run_single_simulation(doc, seed=0, run_len=60, warmup=0)
    for frame in ("system", "resource_summary", "entity_log"):
        assert kpi[frame].equals(baseline[frame]), f"{frame} differs"


# ------------------------------------------------- (4) dashboard validation
def _case4_layout():
    blocks = [create(max_arrivals=2), process("station", "Server", 1.0), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO",
                              "capacity_schedule": "shifts"}],
                  schedules=[{"name": "shifts", "kind": "capacity", "repeat": True,
                              "points": [[5, 2], [5, 1]]}],
                  dashboard={"widgets": [], "controls": [
                      {"name": "cap_ctrl", "kind": "slider", "target": {"capacity": "Server"},
                       "min": 0, "max": 2}]},
                  simulation={"time_limit": 20, "warmup": 0, "replications": 1})


def test_capacity_slider_on_a_scheduled_resource_validates_clean():
    assert "E_DASHBOARD_CONTROL" not in _error_codes(_case4_layout())


# ======================================================================== L2
# ------------------------------------------------------- (5) several decisions
def _policy_links():
    return [
        {"source": "create_1", "target": "decide_1"},
        {"source": "decide_1", "target": "assign_a"},
        {"source": "decide_1", "target": "assign_b"},
        {"source": "assign_a", "target": "dispose_a"},
        {"source": "assign_b", "target": "dispose_b"},
    ]


def _multi_decision_layout():
    blocks = [
        create(entities_per_arrival=2, max_arrivals=1),
        _decide("decide_1", "Policy", policy="route"),
        _assign_type("assign_a", "A", [2, 0]), _assign_type("assign_b", "B", [2, 1]),
        dispose("dispose_a"), dispose("dispose_b"),
    ]
    return layout(blocks, _policy_links(), simulation={"time_limit": 5, "warmup": 0, "replications": 1})


def test_two_decisions_in_one_step_get_increasing_ids_and_answer_out_of_order():
    s = session.SimSession(_multi_decision_layout(), seed=0)
    assert s.advance() == "decision"
    pending = s.pending()
    assert len(pending) == 2
    ids = [req["id"] for req in pending]
    assert len(set(ids)) == 2 and ids[1] > ids[0]

    # the clock must not move while any decision of the two is unanswered
    assert s.advance() == "decision"
    assert s.now == 0.0

    s.decide("route", 1, request_id=ids[1])   # answer the newest first ...
    s.decide("route", 0)                      # ... then the default: the oldest (the only one left)
    kpi = s.finish()

    assert _entity_count(kpi, "A") == 1 and _entity_count(kpi, "B") == 1

    replayed = session.SimSession.replay(_multi_decision_layout(), 0, s.game_log)
    for frame in ("system", "entity_summary", "entity_log"):
        assert kpi[frame].equals(replayed[frame]), f"{frame} differs after replay"


# --------------------------------------------------------------------- (6) error
def _single_policy_layout():
    blocks = [
        create(max_arrivals=1),
        _decide("decide_1", "Policy", policy="route"),
        _assign_type("assign_a", "A", [2, 0]), _assign_type("assign_b", "B", [2, 1]),
        dispose("dispose_a"), dispose("dispose_b"),
    ]
    return layout(blocks, _policy_links(), simulation={"time_limit": 5, "warmup": 0, "replications": 1})


def test_decide_with_an_unknown_request_id_raises_value_error():
    s = session.SimSession(_single_policy_layout(), seed=0)
    assert s.advance() == "decision"
    with pytest.raises(ValueError):
        s.decide("route", 0, request_id=999999)
    s.decide("route", 0)   # leave the run in a clean, finishable state
    s.finish()
