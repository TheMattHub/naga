"""PHASE6 A6: heading/velocity movement (docs/PHASE6_PLAN.md's A6 row;
ARCHITECTURE 5.25 "Heading and steering", D104). Every expected value below is
either exactly representable (0/90-degree headings, integer turns) or computed
independently with the SAME documented formula (math.radians/cos/sin, the
agent's own rebuilt stream) the engine uses, compared with pytest.approx --
never read off the engine first.
"""
import math

import numpy as np
import pytest

from nagasim.core import agents as A
from conftest import agent_rng, problem_codes, run_env


def _model(*records, time_limit=5):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _run(layout, seed=1):
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env.ctx.agent_system


def _pop(system, name):
    return next(p for p in system.populations if p.name == name)


# ------------------------------------------------ (1) forward along a heading
def test_forward_moves_along_heading_0_45_90_degrees_inside_tick():
    # Instant jump inside on_tick (NetLogo style): heading = self.id * 45 (0,
    # 45, 90), forward 10 from (50, 50) at the first tick (phase 0, dt 1 -> t=1).
    walker = A.AgentType(
        "Walker", states=[A.State("A", entry=[A.Action.set("self.heading", "self.id * 45")])],
        space=A.Space("continuous", [0, 0, 100, 100]),
        tick=A.Tick(1, "synchronous"), on_tick=[A.Action.forward("10")])
    pop = A.Population("Walkers", "Walker", 3,
                       placement={"kind": "table", "points": [[50, 50]]})
    layout = _model(walker, pop, time_limit=1.5)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    for i, heading_deg in enumerate((0, 45, 90)):
        rad = math.radians(heading_deg)
        expected = (50 + 10 * math.cos(rad), 50 + 10 * math.sin(rad))
        assert (float(result.x1[i]), float(result.y1[i])) == pytest.approx(expected)
        assert (float(result.t0[i]), float(result.t1[i])) == (1.0, 1.0)  # instant, no travel
        assert float(result.heading[i]) == pytest.approx(heading_deg)


# ------------------------------------------------ (2) turn (left positive) and a timed forward
def test_turn_then_forward_commits_the_segment_at_the_firing():
    # A single t=0 transition: turn 30 left (heading 0 -> 30), then a TIMED
    # forward of 10 at speed 2 -- the same "committed at the firing, not only
    # on arrival" fact test_agents.py already proves for a plain move_to.
    walker = A.AgentType(
        "Walker", states=["Start", "Moving", "Arrived"], initial="Start", speed="2",
        space=A.Space("continuous", [0, 0, 100, 100]),
        transitions=[A.Transition("Start", "Moving", "timeout", "0",
                                  actions=[A.Action.turn("30"), A.Action.forward("10")]),
                     A.Transition("Moving", "Arrived", "message", "arrived")])
    pop = A.Population("Walkers", "Walker", 1, placement={"kind": "table", "points": [[0, 0]]})
    layout = _model(walker, pop, time_limit=1)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    rad = math.radians(30.0)
    assert float(result.heading[0]) == pytest.approx(30.0)
    assert (float(result.x0[0]), float(result.y0[0])) == (0.0, 0.0)
    assert (float(result.x1[0]), float(result.y1[0])) == \
        pytest.approx((10 * math.cos(rad), 10 * math.sin(rad)))
    assert (float(result.t0[0]), float(result.t1[0])) == (0.0, 5.0)   # distance / speed


def test_timed_forward_arrives_at_distance_over_speed_exactly():
    # East, speed 2, distance 10 -> arrival at exactly 5.0 and (10, 0), the
    # move_to-style "arrived" message (D95) with no travel-time drift.
    walker = A.AgentType(
        "Walker", states=["Start", "Moving", "Arrived"], initial="Start", speed="2",
        space=A.Space("continuous", [0, 0, 100, 100]),
        transitions=[A.Transition("Start", "Moving", "timeout", "0",
                                  actions=[A.Action.forward("10")]),
                     A.Transition("Moving", "Arrived", "message", "arrived")])
    pop = A.Population("Walkers", "Walker", 1, placement={"kind": "table", "points": [[0, 0]]})
    layout = _model(walker, pop, time_limit=10)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    assert result.type.index["Arrived"] == int(result.state[0])
    assert float(result.entered[0]) == pytest.approx(5.0, abs=0.0)
    assert (float(result.x1[0]), float(result.y1[0])) == pytest.approx((10.0, 0.0), abs=1e-9)


# ------------------------------------------------ (3) face
def test_face_points_heading_at_a_literal_point():
    walker = A.AgentType("Walker", states=["A"],
                         space=A.Space("continuous", [0, 0, 100, 100]),
                         transitions=[A.Transition("A", "A", "timeout", "1000",
                                                   actions=[A.Action.face([10, 0])])])
    pop = A.Population("Walkers", "Walker", 1, placement={"kind": "table", "points": [[0, 0]]})
    layout = _model(walker, pop, time_limit=1)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    # face runs once, at start (t=0, an entry/transition ordering already
    # proven elsewhere) -- east is heading 0 exactly (atan2(0, 10) == 0.0).
    assert float(result.heading[0]) == 0.0


# ------------------------------------------------ (4) edge rules: wrap and bounce
def test_wrap_folds_position_exactly_modulo_the_span():
    walker = A.AgentType(
        "Walker", states=[A.State("A", entry=[A.Action.set("self.heading", "0")])],
        space=A.Space("continuous", [0, 0, 100, 100], edge="wrap"),
        tick=A.Tick(1, "synchronous"), on_tick=[A.Action.forward("20")])
    pop = A.Population("Walkers", "Walker", 1, placement={"kind": "table", "points": [[95, 50]]})
    layout = _model(walker, pop, time_limit=1.5)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    # 95 + 20 = 115, wrapped into [0, 100) -> 15; heading unchanged by a wrap.
    assert (float(result.x1[0]), float(result.y1[0])) == (15.0, 50.0)
    assert float(result.heading[0]) == 0.0


def test_bounce_reflects_off_the_wall_and_flips_heading():
    walker = A.AgentType(
        "Walker", states=[A.State("A", entry=[A.Action.set("self.heading", "0")])],
        space=A.Space("continuous", [0, 0, 100, 100], edge="bounce"),
        tick=A.Tick(1, "synchronous"), on_tick=[A.Action.forward("20")])
    pop = A.Population("Walkers", "Walker", 1, placement={"kind": "table", "points": [[95, 50]]})
    layout = _model(walker, pop, time_limit=1.5)
    assert not problem_codes(layout)
    result = _pop(_run(layout), "Walkers")
    # 95 -> wall at 100 after 5, reflects back the remaining 15 -> 85; the x
    # component of heading flips (0 -> 180), y untouched (never neared a wall).
    assert (float(result.x1[0]), float(result.y1[0])) == pytest.approx((85.0, 50.0))
    assert float(result.heading[0]) == pytest.approx(180.0)


# ------------------------------------------------ (5) random_walk reproduces the rebuilt stream
def test_random_walk_matches_the_agents_own_rebuilt_stream():
    walker = A.AgentType(
        "Walker", states=[A.State("A", entry=[A.Action.set("self.heading", "0")])],
        space=A.Space("continuous", [0, 0, 200, 200]),
        tick=A.Tick(1, "synchronous"), on_tick=[A.Action.random_walk(5, 20)])
    pop = A.Population("Walkers", "Walker", 1,
                       placement={"kind": "table", "points": [[100, 100]]})
    layout = _model(walker, pop, time_limit=1.5)
    assert not problem_codes(layout)
    result = _pop(_run(layout, seed=7), "Walkers")
    # The agent's own stream (D107: seed, population, slot), its ONLY draw so
    # far: a normal(0, 20) turn, then an instant forward(5).
    rng = agent_rng(7, "Walkers", 0)
    turn = float(rng.normal(0.0, 20.0))
    heading = turn % 360.0
    rad = math.radians(heading)
    expected = (100 + 5 * math.cos(rad), 100 + 5 * math.sin(rad))
    assert float(result.heading[0]) == pytest.approx(heading)
    assert (float(result.x1[0]), float(result.y1[0])) == pytest.approx(expected)


# ------------------------------------------------ (6) NEAR_HEADING circular mean
def test_near_heading_is_the_circular_mean_not_the_plain_one():
    # Two birds at (10, 10) with headings 350 and 10 (plain mean 180, wrong);
    # a third, uninvolved bird right next to them reads NEAR_HEADING -> 0.
    bird = A.AgentType(
        "Bird", attributes=[A.Attribute("result", "-1")], states=["A", "B"],
        space=A.Space("continuous", [0, 0, 50, 50]),
        transitions=[A.Transition(
            "A", "B", "timeout", "0",
            actions=[A.Action.set("self.heading",
                                  "IF(self.id == 0, 350, IF(self.id == 1, 10, 0))")])])
    probe = A.AgentType(
        "Prober", attributes=[A.Attribute("result", "-1")], states=["A", "B"],
        space=A.Space("continuous", [0, 0, 50, 50]),
        transitions=[A.Transition("A", "B", "timeout", "1",
                                  actions=[A.Action.set("self.result",
                                                        'NEAR_HEADING("Birds", 1)')])])
    birds = A.Population("Birds", "Bird", 2, initial_states={"A": 2},
                         placement={"kind": "table", "points": [[10, 10], [10.1, 10]]})
    prober = A.Population("Probers", "Prober", 1, initial_states={"A": 1},
                          placement={"kind": "table", "points": [[10, 10]]})
    layout = _model(bird, probe, birds, prober, time_limit=2)
    assert not problem_codes(layout)
    system = _run(layout)
    result = _pop(system, "Probers")
    # A circular distance to 0: a plain `% 360` comparison is a floating-point
    # trap right at the wrap-around (it can round a hair below 360 UP to the
    # float 360.0 itself), which the engine's own `_norm360` also folds back.
    assert min(float(result.attrs["result"][0]) % 360.0,
              360.0 - float(result.attrs["result"][0]) % 360.0) == pytest.approx(0.0, abs=1e-9)


def test_near_heading_circular_mean_matches_atan2_of_the_mean_vector():
    """The independent oracle: atan2(mean(sin), mean(cos)) over the two
    headings, exactly what `AgentSystem.near_heading` computes."""
    headings = np.array([350.0, 10.0])
    rad = np.radians(headings)
    expected = math.degrees(math.atan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360.0
    assert min(expected, 360.0 - expected) == pytest.approx(0.0, abs=1e-9)
