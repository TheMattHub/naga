"""Independent oracle for milestone G4 (PHASE6 depth bucket G, engine parts):
dashboard "controls" validation (E_DASHBOARD_CONTROL), `SimSession.
apply_control`, `Timeline.append`, and `SimSession(trace=...)`/`take_events`.

Written from the task's fixed SPEC only. Unlike test_session_oracle.py/
test_dashboard_oracle.py, the modules this milestone extends (nagasim.core.session,
nagasim.core.playback, nagasim.core.schema, nagasim.core.trace) already exist
-- G1 and G3 landed them -- so a whole-module `pytest.importorskip` cannot
gate this file the way it gated G3's. Instead every case below is gated on
the SPECIFIC new member it needs (a method, a constructor parameter, or the
"E_DASHBOARD_CONTROL" code string appearing in schema.py's source), computed
once at import time, so the file always collects and only the not-yet-landed
cases are skipped -- per the task's instruction to use "pytest.importorskip /
feature checks so the file collects before the implementation lands".

Only already-committed code was read to write this: session.py's, playback.
py's and trace.py's own docstrings/G1-G3 bodies (for the existing `advance`/
`pending`/`decide`/`set_capacity`/`set_variable`/`game_log` contracts and the
`Timeline`/`TraceRecorder` constructors), schema.py's existing `_validate_
dashboard`/`problem()` conventions (G1, for the E_DASHBOARD_* code style), and
tests/test_session_oracle.py / tests/test_dashboard_oracle.py (already-landed oracles) for
model-building helpers and hand-derived numbers. No G4 diff or the other
agent's tests/test_game.py was read.

Hand derivations REUSED from tests/test_session_oracle.py (already-landed,
independently hand-derived there from CREATE/DECIDE/PROCESS/WAIT-rule
semantics that G4 does not change -- not re-derived here, only reused):
  - capacity slider / WAIT rule: CREATE(Constant 1.0, max_arrivals=4) ->
    PROCESS(Server capacity 2, Constant service 4.0) -> DISPOSE. Lowering
    capacity to 1 at t=2.5 then raising it back to 2 at t=6.0 gives exit
    times [4, 5, 9, 10] sorted by creation (G3 case c).
  - decision routing: CREATE(Constant 1.0, max_arrivals=6) -> Policy DECIDE
    ("route", 2 links) -> tag A/B -> DISPOSE. Answering option 1 for an even
    entity id and 0 for an odd one gives options [0, 1, 0, 1, 0, 1] and entity
    counts A=3/B=3 (G3 case d).
  - variable slider / ASSIGN-equivalent gating: CREATE(Constant 1.0,
    max_arrivals=6) -> Condition DECIDE on "gate == 1" (initial 1) -> tag A/B
    -> DISPOSE. Flipping "gate" to 0 at t=2.5 gives entity counts A=3/B=3
    (G3 case b).
`apply_control` must reduce to exactly the same underlying call (per the
spec), so reusing these numbers is testing THIS milestone's own claim, not
assuming it.

RESOLVED AMBIGUITIES (GUESS):
  - The SPEC lumps several E_DASHBOARD_CONTROL triggers under one bullet each
    ("unknown target (undeclared variable / auxiliary / resource)"; "capacity
    slider max above the resource's declared capacity or min < 0"). Per the
    task's "one layout per rule" brief (and G1 oracle's own precedent for
    E_DASHBOARD_SERIES), only ONE trigger per such bullet is exercised here:
    an undeclared *variable* target (not auxiliary/resource), and a capacity
    slider's max ABOVE the resource's capacity (not min < 0). # GUESS
  - `Timeline.append`'s "an earlier t raises ValueError" is tested both
    for an out-of-order list handed to ONE `append` call and for a second
    `append` call whose first event is earlier than the previous call's last
    -- the SPEC does not say which of the two (or both) is meant. # GUESS
  - `apply_control`'s slider bound check uses the CONTROL's own declared
    "min"/"max" (not the underlying resource's/variable's own range), so the
    capacity-slider out-of-range case below deliberately sets a control max
    BELOW the resource's actual declared capacity, to tell the two apart --
    directly from the spec text ("value outside [min, max] raises
    ValueError"), not a guess.
"""
import inspect
import random

import pytest

from conftest import chain, create, dispose, layout, problem_codes, process

import nagasim.core.schema as schema_mod
from nagasim.core import playback, session
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.trace import TraceEvent, TraceRecorder

# -- feature gates (G4 not landed yet: every check below is currently False) --
_HAS_APPLY_CONTROL = hasattr(session.SimSession, "apply_control")
_HAS_TRACE_KWARG = "trace" in inspect.signature(session.SimSession.__init__).parameters
_HAS_TAKE_EVENTS = hasattr(session.SimSession, "take_events")
_HAS_TIMELINE_APPEND = hasattr(playback.Timeline, "append")
_HAS_DASHBOARD_CONTROL_CODE = "E_DASHBOARD_CONTROL" in schema_mod.PROBLEM_CODES

_skip_control_api = pytest.mark.skipif(not _HAS_APPLY_CONTROL,
                                       reason="G4 not landed: SimSession.apply_control")
_skip_trace_api = pytest.mark.skipif(not (_HAS_TRACE_KWARG and _HAS_TAKE_EVENTS),
                                     reason="G4 not landed: SimSession(trace=...)/take_events")
_skip_timeline_append = pytest.mark.skipif(not _HAS_TIMELINE_APPEND,
                                           reason="G4 not landed: Timeline.append")
_skip_dashboard_control = pytest.mark.skipif(not _HAS_DASHBOARD_CONTROL_CODE,
                                             reason="G4 not landed: dashboard control validation")


# ---------------------------------------------------------------- block helpers
# Same shapes as tests/test_session_oracle.py's own (committed, G3), reproduced here
# rather than imported, so this file has no import-time dependency on it.
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


# ============================================================== 1. validation
def _controls_layout(controls):
    """CREATE -> Policy DECIDE ("route", 2 outgoing links) -> tag A/B ->
    DISPOSE, against a capacity-3 "Server" and a declared variable "rate" --
    one shared, otherwise-valid layout with enough surface (a capacity, a
    variable, a 2-link Policy DECIDE) to exercise every E_DASHBOARD_CONTROL
    rule by changing only *controls*."""
    blocks = [
        create(max_arrivals=2),
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
    return layout(blocks, links,
                  resources=[{"name": "Server", "capacity": 3, "discipline": "FIFO"}],
                  variables=[{"name": "rate", "initial": 1}],
                  dashboard={"widgets": [], "controls": controls},
                  simulation={"time_limit": 8, "warmup": 0, "replications": 1})


def _codes(lay):
    return problem_codes(lay)


_VALID_CONTROLS = [
    {"name": "cap", "kind": "slider", "target": {"capacity": "Server"}, "min": 0, "max": 3},
    {"name": "rate_slider", "kind": "slider", "target": {"variable": "rate"}, "min": 0, "max": 10,
     "step": 0.5},
    {"name": "route_decision", "kind": "decision", "policy": "route", "options": ["A", "B"]},
]


@_skip_dashboard_control
def test_valid_control_set_raises_no_dashboard_control_error():
    assert "E_DASHBOARD_CONTROL" not in _codes(_controls_layout(_VALID_CONTROLS))


@_skip_dashboard_control
def test_missing_control_name_is_e_dashboard_control():
    controls = [{"kind": "slider", "target": {"variable": "rate"}, "min": 0, "max": 10}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_duplicate_control_name_is_e_dashboard_control():
    one = {"name": "x", "kind": "slider", "target": {"variable": "rate"}, "min": 0, "max": 10}
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout([one, dict(one)]))


@_skip_dashboard_control
def test_unknown_control_kind_is_e_dashboard_control():
    controls = [{"name": "x", "kind": "knob", "target": {"variable": "rate"}, "min": 0, "max": 10}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_unknown_control_target_is_e_dashboard_control():
    # GUESS: only the "undeclared variable" half of this rule is exercised.
    controls = [{"name": "x", "kind": "slider", "target": {"variable": "ghost"}, "min": 0, "max": 10}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_min_not_less_than_max_is_e_dashboard_control():
    controls = [{"name": "x", "kind": "slider", "target": {"variable": "rate"}, "min": 5, "max": 5}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_non_positive_step_is_e_dashboard_control():
    controls = [{"name": "x", "kind": "slider", "target": {"variable": "rate"}, "min": 0, "max": 10,
                "step": 0}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_capacity_slider_max_above_declared_capacity_is_e_dashboard_control():
    # GUESS: only the "max above capacity" half of this rule is exercised, not "min < 0".
    controls = [{"name": "x", "kind": "slider", "target": {"capacity": "Server"}, "min": 0, "max": 99}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_decision_policy_with_no_policy_decide_is_e_dashboard_control():
    # decide_1's own policy is "route", not "ghost".
    controls = [{"name": "x", "kind": "decision", "policy": "ghost", "options": ["A", "B"]}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


@_skip_dashboard_control
def test_decision_options_count_mismatch_is_e_dashboard_control():
    # decide_1 ("route") has 2 outgoing links; 3 options is a mismatch.
    controls = [{"name": "x", "kind": "decision", "policy": "route", "options": ["A", "B", "C"]}]
    assert "E_DASHBOARD_CONTROL" in _codes(_controls_layout(controls))


# ==================================================== 2. apply_control parity
def _capacity_layout(control_max=2):
    c, p, d = create(max_arrivals=4), process("station", "Server", 4.0), dispose()
    return layout([c, p, d], chain(c, p, d),
                  resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO"}],
                  dashboard={"widgets": [], "controls": [
                      {"name": "cap_ctrl", "kind": "slider", "target": {"capacity": "Server"},
                       "min": 0, "max": control_max}]},
                  simulation={"time_limit": 15, "warmup": 0, "replications": 1})


@_skip_control_api
def test_apply_control_capacity_slider_matches_set_capacity_directly():
    doc = _capacity_layout(control_max=2)

    sa = session.SimSession(doc, seed=0)
    sa.advance(to=2.5)
    sa.apply_control("cap_ctrl", 1)
    sa.advance(to=6.0)
    sa.apply_control("cap_ctrl", 2)
    played = sa.finish()

    sb = session.SimSession(doc, seed=0)
    sb.advance(to=2.5)
    sb.set_capacity("Server", 1)
    sb.advance(to=6.0)
    sb.set_capacity("Server", 2)
    direct = sb.finish()

    assert _exit_times_by_creation(played) == [4.0, 5.0, 9.0, 10.0]
    for frame in ("system", "resource_summary", "entity_log"):
        assert played[frame].equals(direct[frame]), f"{frame} differs"
    assert sa.game_log == sb.game_log


@_skip_control_api
def test_apply_control_capacity_slider_out_of_range_raises():
    # Control max (1) is tighter than the resource's own declared capacity
    # (2): set_capacity(Server, 2) would be legal directly, so this only
    # exercises the CONTROL's own bound, not the resource's.
    s = session.SimSession(_capacity_layout(control_max=1), seed=0)
    with pytest.raises(ValueError):
        s.apply_control("cap_ctrl", 2)


def _gate_layout():
    blocks = [
        create(max_arrivals=6),
        _decide("decide_1", "Condition", conditions=["gate == 1"]),
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
    return layout(blocks, links, variables=[{"name": "gate", "initial": 1}],
                  dashboard={"widgets": [], "controls": [
                      {"name": "gate_ctrl", "kind": "slider", "target": {"variable": "gate"},
                       "min": 0, "max": 1}]},
                  simulation={"time_limit": 8, "warmup": 0, "replications": 1})


@_skip_control_api
def test_apply_control_variable_slider_matches_set_variable_directly():
    doc = _gate_layout()

    sa = session.SimSession(doc, seed=0)
    sa.advance(to=2.5)
    sa.apply_control("gate_ctrl", 0)
    played = sa.finish()

    sb = session.SimSession(doc, seed=0)
    sb.advance(to=2.5)
    sb.set_variable("gate", 0)
    direct = sb.finish()

    assert _entity_count(played, "A") == 3 and _entity_count(played, "B") == 3
    for frame in ("system", "entity_summary", "entity_log"):
        assert played[frame].equals(direct[frame]), f"{frame} differs"
    assert sa.game_log == sb.game_log


@_skip_control_api
def test_apply_control_variable_slider_out_of_range_raises():
    s = session.SimSession(_gate_layout(), seed=0)
    with pytest.raises(ValueError):
        s.apply_control("gate_ctrl", 2)     # control's own max is 1


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
    return layout(blocks, links,
                  dashboard={"widgets": [], "controls": [
                      {"name": "route_ctrl", "kind": "decision", "policy": "route",
                       "options": ["A", "B"]}]},
                  simulation={"time_limit": 8, "warmup": 0, "replications": 1})


def _play(s, use_apply_control):
    """Answer every pending decision, option 1 for an even entity id / 0 for
    odd (G3 case d's own rule); via `apply_control` or `decide` directly."""
    options = []
    while True:
        state = s.advance()
        if state != "decision":
            return options
        for req in s.pending():
            option = 1 if req["entity_id"] % 2 == 0 else 0
            if use_apply_control:
                s.apply_control("route_ctrl", option)
            else:
                s.decide(req["policy"], option)
            options.append(option)


@_skip_control_api
def test_apply_control_decision_matches_decide_directly():
    sa = session.SimSession(_policy_layout(), seed=0)
    options_a = _play(sa, True)
    played = sa.finish()

    sb = session.SimSession(_policy_layout(), seed=0)
    options_b = _play(sb, False)
    direct = sb.finish()

    assert options_a == options_b == [0, 1, 0, 1, 0, 1]
    assert _entity_count(played, "A") == 3 and _entity_count(played, "B") == 3
    for frame in ("system", "entity_summary", "entity_log"):
        assert played[frame].equals(direct[frame]), f"{frame} differs"
    assert sa.game_log == sb.game_log


@_skip_control_api
def test_apply_control_decision_option_out_of_range_raises():
    s = session.SimSession(_policy_layout(), seed=0)
    s.advance()
    with pytest.raises(ValueError):
        s.apply_control("route_ctrl", 99)


# ===================================================== 3. Timeline.append
def _event(t, kind="entity_created"):
    return TraceEvent(t=t, kind=kind, entity_id=1, e_type="A", block="b", target=None)


def _small_run_events():
    """CREATE(Constant 2, max_arrivals=3) -> PROCESS(Server capacity 1,
    Constant 3) -> DISPOSE (test_dashboard_oracle.py's own D/D/1 queue model): a
    real, engine-produced trace. This case checks `Timeline.append`'s
    EQUIVALENCE to one-shot construction on the same event list -- it never
    depends on what the events actually mean, so no hand derivation is
    needed here."""
    c = create("c", distribution="Constant", dist_params={"value": 2.0}, max_arrivals=3)
    p = process("p", resource="Server", value=3.0, distribution="Constant")
    d = dispose("d")
    doc = layout([c, p, d], chain(c, p, d),
                resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
                simulation={"time_limit": None, "warmup": 0, "replications": 1})
    recorder = TraceRecorder()
    run_single_simulation(doc, seed=1, run_len=20, warmup=0, subscribers=[recorder])
    return recorder.events


@_skip_timeline_append
def test_timeline_append_matches_one_shot_construction_split_at_3_points():
    events = _small_run_events()
    assert len(events) >= 8, "the queue model must yield enough events for 4 non-trivial chunks"

    reference = playback.Timeline(events)

    split_points = sorted(random.Random(20260926).sample(range(1, len(events)), 3))
    bounds = [0, *split_points, len(events)]
    chunks = [events[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]

    incremental = playback.Timeline([])
    for chunk in chunks:
        incremental.append(chunk)

    duration = reference.duration
    for i in range(20):
        t = duration * i / 19 if duration else 0.0
        assert incremental.state_at(t) == reference.state_at(t), f"state differs at t={t}"


@_skip_timeline_append
def test_timeline_append_rejects_an_earlier_time_across_calls():
    tl = playback.Timeline([])
    tl.append([_event(5.0)])
    with pytest.raises(ValueError):
        tl.append([_event(3.0)])


@_skip_timeline_append
def test_timeline_append_rejects_out_of_order_events_within_one_call():
    # GUESS: the SPEC's "events in non-decreasing time" is read as applying
    # within a single append() call too, not just across successive calls.
    tl = playback.Timeline([])
    with pytest.raises(ValueError):
        tl.append([_event(5.0), _event(3.0)])


# ================================================== 4. take_events / trace
def _one_shot_layout():
    c, p, d = create(max_arrivals=6), process("station", "Server", 0.7), dispose()
    return layout([c, p, d], chain(c, p, d),
                  resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
                  simulation={"time_limit": 8, "warmup": 0, "replications": 1})


@_skip_trace_api
def test_take_events_concatenation_equals_one_shot_trace():
    doc = _one_shot_layout()

    full_recorder = TraceRecorder()
    run_single_simulation(doc, seed=0, run_len=8, warmup=0, subscribers=[full_recorder])
    full_events = list(full_recorder.events)

    s = session.SimSession(doc, seed=0, trace=TraceRecorder())
    collected = []
    for to in (1.5, 3.3, 6.0):
        s.advance(to=to)
        collected += s.take_events()
    s.finish()
    collected += s.take_events()

    assert collected == full_events
