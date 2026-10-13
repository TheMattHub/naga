"""SimSession implementation cases the G3 oracle (test_session_oracle.py) leaves out:
snapshot values, set_constant on an SD model and its replay, a drained run,
set_probs."""
import pytest

from conftest import chain, create, dispose, layout

from nagasim.core.session import SimSession


def test_snapshot_set_constant_and_replay_on_an_sd_stock():
    """Stock' = AUX("r"): r = 1 until t=4, then 3, so Stock = 4 at t=4 and
    4 + 3*2 = 10 at t=6, 4 + 3*6 = 22 at the horizon (10)."""
    c, d = create("c", max_arrivals=1), dispose("d")
    doc = layout([c, d], chain(c, d),
                 levels=[{"name": "Stock", "initial": 0}],
                 flows=[{"name": "in", "to": "Stock", "rate": 'AUX("r")'}],
                 auxiliaries=[{"name": "r", "expr": "1"}],
                 dashboard={"widgets": [
                     {"name": "stock", "kind": "kpi", "expr": 'LEVEL("Stock")'},
                     {"name": "l", "kind": "line", "series": [{"label": "clock", "expr": "TNOW"}]}]},
                 simulation={"time_limit": 10, "warmup": 0, "replications": 1})
    s = SimSession(doc, seed=0)
    assert s.advance(to=4) == "time"
    assert s.snapshot() == {"t": 4.0, "widgets": {"stock": 4.0, "l": {"clock": 4.0}}}
    s.set_constant("r", 3)
    s.advance(to=6)
    assert s.snapshot()["widgets"]["stock"] == pytest.approx(10.0)
    played = s.finish()
    assert played["level_summary"]["Final"].iloc[0] == pytest.approx(22.0)
    assert [e["action"] for e in s.game_log] == ["set_constant"]
    assert list(played["game_log"]["Action"]) == ["set_constant"]

    replayed = SimSession.replay(doc, 0, s.game_log)
    for frame in ("level_summary", "flow_summary", "game_log"):
        assert played[frame].equals(replayed[frame]), frame


def test_snapshot_peeks_a_blended_level_without_integrating_it():
    """DEVLOG #229: Tank 100 (x = 0) fed 10/unit of pure x, so at t=1.3 the
    snapshot reads 113 and x = 13/113 while the engine stays where it was."""
    c, d = create("c", max_arrivals=1), dispose("d")
    doc = layout([c, d], chain(c, d),
                 levels=[{"name": "Tank", "capacity": 1000, "initial": 100,
                          "components": [{"name": "x", "initial_fraction": 0.0}]}],
                 flows=[{"name": "in", "to": "Tank", "rate": "10.0", "components": {"x": 1.0}}],
                 dashboard={"widgets": [
                     {"name": "l", "kind": "kpi", "expr": 'LEVEL("Tank")'},
                     {"name": "x", "kind": "kpi", "expr": 'FRACTION("Tank", "x")'}]},
                 simulation={"time_limit": 5, "warmup": 0, "replications": 1})
    s = SimSession(doc, seed=0)
    s.advance(to=1.3)
    before = s.ctx.level_system._last_t
    widgets = s.snapshot()["widgets"]
    assert widgets["l"] == pytest.approx(113.0) and widgets["x"] == pytest.approx(13 / 113)
    assert s.ctx.level_system._last_t == before < 1.3


def test_advance_reports_a_drained_model_and_keeps_its_clock():
    c, d = create("c", max_arrivals=2), dispose("d")
    s = SimSession(layout([c, d], chain(c, d)), seed=0)
    assert s.horizon == 0
    assert s.advance() == "drained"
    assert s.now == 1.0
    assert s.advance(to=5) == "drained"
    assert s.now == 1.0         # the KPIs end at the drain, as a normal run's do
    assert s.finish()["system"] is not None


def test_set_probs_reroutes_a_chance_decide():
    """Arrivals at t=0..5 through Chance [1.0] + ELSE; probs [0.0] at t=2.5
    sends the last three down ELSE."""
    blocks = [create(max_arrivals=6),
              {"name": "dec", "description": "dec", "type": "DECIDE", "position": [1, 0],
               "params": {"mode": "Chance", "probs": [1.0], "conditions": []}},
              dispose("d_a"), dispose("d_b")]
    links = [{"source": "create_1", "target": "dec"}, {"source": "dec", "target": "d_a"},
             {"source": "dec", "target": "d_b"}]
    s = SimSession(layout(blocks, links, simulation={"time_limit": 8, "warmup": 0,
                                                     "replications": 1}), seed=0)
    s.advance(to=2.5)
    for bad in ([0.7, 0.7], [0.5, 0.5, 0.0], [-0.1]):
        with pytest.raises(ValueError):
            s.set_probs("dec", bad)
    with pytest.raises(ValueError):
        s.set_probs("d_a", [0.5])
    s.set_probs("dec", [0.0])
    s.finish()
    assert s.model.blocks["d_a"].disposed_entities == 3
    assert s.model.blocks["d_b"].disposed_entities == 3


def test_capacity_cap_on_a_scheduled_failing_resource():
    """L3: R (3 units) has shift 3 until t=4 then 2 and fails at 6 for 2. Cap 1
    at t=1, 2 at t=5, 3 at t=7 (during the failure: waits for the repair), 0
    at t=9. In force: 3, 1, 1, 2, 0, 0, 2, 0 -> area 3+4+2+0+2+0 = 11 of 10."""
    c, d = create("c", max_arrivals=1), dispose("d")
    doc = layout([c, d], chain(c, d),
                 resources=[{"name": "R", "capacity": 3, "capacity_schedule": "shift"}],
                 schedules=[{"name": "shift", "kind": "capacity", "repeat": False,
                             "points": [[4, 3], [100, 2]]}],
                 failures=[{"name": "f", "resources": ["R"],
                            "uptime": {"distribution": "Constant", "dist_params": {"value": 6}},
                            "downtime": {"distribution": "Constant", "dist_params": {"value": 2}}}],
                 simulation={"time_limit": 10, "warmup": 0, "replications": 1})
    s = SimSession(doc, seed=0)
    res = s.ctx.resource_manager.resources["R"]
    for t, cap, in_force in ((1, 1, 1), (5, 2, 2), (7, 3, 0), (9, 0, 0)):
        s.advance(to=t)
        s.set_capacity("R", cap)
        s.advance(to=t + 0.5)
        assert res.scheduled_capacity == in_force, t
    played = s.finish()
    assert res.available_area(10) == pytest.approx(11.0)
    assert res.down_units.area(10) == pytest.approx(4.0)        # the failure took both units
    assert res.down_fraction(10) == pytest.approx(0.2)
    replayed = SimSession.replay(doc, 0, s.game_log)
    assert played["resource_summary"].equals(replayed["resource_summary"])


def _two_at_once():
    """One arrival of two entities into Policy DECIDE "route" -> a | b."""
    route = {"name": "route", "description": "route", "type": "DECIDE", "position": [1, 0],
             "params": {"mode": "Policy", "probs": [], "conditions": [], "policy": "route",
                        "observation": [], "fallback": "first", "queue_exprs": []}}
    blocks = [create(max_arrivals=1, entities_per_arrival=2), route, dispose("a"), dispose("b")]
    links = [{"source": s, "target": t} for s, t in
             (("create_1", "route"), ("route", "a"), ("route", "b"))]
    return layout(blocks, links, simulation={"time_limit": 5, "warmup": 0, "replications": 1})


class _Where(dict):
    """A bus subscriber (passed as `trace=`): entity id -> the dispose it left by."""

    def __call__(self, name, **payload):
        if name == "entity_disposed":
            self[payload["entity"].id] = payload["block"].name


def test_two_decisions_of_one_step_are_pending_answered_in_any_order_and_replayed():
    """L2: both requests pend at t=0 with ids 0, 1; the clock waits for both;
    request 1 answered first (entity 2 -> b), then the oldest (entity 1 -> a)."""
    where = _Where()
    s = SimSession(_two_at_once(), seed=0, trace=where)
    assert s.advance() == "decision"
    assert [(p["id"], p["entity_id"], p["t"]) for p in s.pending()] == [(0, 1, 0), (1, 2, 0)]
    assert s.advance(to=3) == "decision" and s.now == 0
    with pytest.raises(ValueError):
        s.decide("route", 0, request_id=7)
    s.decide("route", 1, request_id=1)
    assert [p["id"] for p in s.pending()] == [0]
    s.decide("route", 0)                        # None: the oldest still pending
    with pytest.raises(ValueError):
        s.decide("route", 0)
    s.finish()
    assert where == {1: "a", 2: "b"}
    assert [(e["value"], e["request_id"]) for e in s.game_log] == [(1, 1), (0, 0)]

    replayed = _Where()
    SimSession.replay(_two_at_once(), 0, s.game_log, trace=replayed)
    assert replayed == where


def test_a_paused_run_leaves_no_stop_event_in_the_queue_on_any_simpy():
    """The audit of the first engine CI run's note. SimPy 4.1.2 puts the stop event of
    `Environment.run(until=)` back into the queue (4.1.1 dropped it), so a `peek()` after
    it gives the pause time and not the next event's. The engine never calls `run`: it
    steps and moves the clock itself (`step_until`, `_move_clock`), so no such event
    exists, here or in the debugger, game mode, a state taken at a pause or the sampler.
    This runs on both versions (CI installs 4.1.2)."""
    import re
    from pathlib import Path

    import simpy

    import nagasim
    from conftest import process

    env = simpy.Environment()
    env.timeout(5)
    env.run(until=3)
    # What SimPy itself does: 5 before 4.1.2, 3 from it on. The engine depends on neither.
    assert env.peek() in (3, 5)
    called = [path.name for path in Path(nagasim.__file__).parent.rglob("*.py")
              if re.search(r"\benv\.run\(|StopSimulation", path.read_text(encoding="utf-8"))]
    assert called == []         # a first call of it is a reason to read this test again

    c = create("c", distribution="Exponential", dist_params={"rate": 0.2})
    p, d = process("p", value=0.25, distribution="Exponential"), dispose("d")
    model = layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}],
                   simulation={"time_limit": 200, "warmup": 0, "replications": 1})
    whole = SimSession(model, seed=3)
    assert whole.advance() == "horizon"
    paused = SimSession(model, seed=3)
    for k in range(1, 40):
        assert paused.advance(to=k * 5.5) == ("time" if k * 5.5 < 200 else "horizon")
        # Nothing of the pause is queued: the next event is the model's, after the pause.
        assert not [item for item in paused.env._queue
                    if type(item[3]) is simpy.Event and not item[3].callbacks]
        assert paused.env.peek() > paused.now == min(k * 5.5, 200.0)
    one, other = whole.finish(), paused.finish()
    assert one["system"].equals(other["system"])
    assert one["resource_summary"].equals(other["resource_summary"])
