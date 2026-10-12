"""PHASE6 A1: the tick scheduler (ARCHITECTURE 5.25, D99). Every expected value
is hand-derived or rebuilt from the documented stream, never read off the engine."""
import json

import numpy as np
import pytest

from nagasim.core import agents as A
from conftest import agent_seed, problem_codes
from nagasim.core.base_process import deterministic_hash
from nagasim.core.schema import migrate
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until


def _model(*records, time_limit, **sections):
    layout = A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}},
                   *records)
    layout.update(sections)
    return layout


def _run(layout, seed=1):
    """Deliberately NOT `conftest.run_env`: this file's oracles assert exact
    float equality on a level driven by a piecewise-constant COUNT() rate, and
    `run_env`'s sampler (default 200 points/run) syncs the level system at each
    sample -- real production behaviour, but repeated partial syncs round
    differently than one final sync (floating-point addition is not
    associative), which broke test_ticks_coexist_with_events_and_an_sd_level's
    exact `==` by ~4e-15. Same setup otherwise (no warm start/initial_state
    needed: these models carry none)."""
    run_len = layout["simulation"]["time_limit"]
    env = initialize_sim_env(seed)
    env.ctx.sim_horizon = run_len
    prepare_model(env, layout)
    if env.ctx.level_system is not None:
        env.ctx.level_system.start(run_len)
    env.ctx.agent_system.start(run_len)
    step_until(env, run_len)
    if env.ctx.level_system is not None:
        env.ctx.level_system.sync(run_len)
    return env


def _codes(layout):
    return problem_codes(layout)


# ------------------------------------------------ (1) synchronous vs sequential
def _ring(activation, time_limit):
    cell = A.AgentType("Cell", attributes=[A.Attribute("v", "10 * (self.id + 1)")], states=["S"],
                       tick=A.Tick(1, activation),
                       on_tick=[A.Action.set("self.v", 'AGENT("Ring", (self.id + 2) % 3).v')])
    return _model(cell, A.Population("Ring", "Cell", 3), time_limit=time_limit)


@pytest.mark.parametrize("activation, after1, after2", [
    # v = [10, 20, 30]; each copies its left neighbour (i - 1) mod 3.
    ("synchronous", [30, 10, 20], [20, 30, 10]),    # everyone reads the tick start: a rotation
    ("sequential", [30, 30, 30], [30, 30, 30]),     # 0 <- 30, then 1 <- new v0, 2 <- new v1
])
def test_ring_copy_left_neighbour(activation, after1, after2):
    layout = _ring(activation, 1.5)
    assert not _codes(layout)
    assert _run(layout).ctx.agent_system.by_name["Ring"].attrs["v"].tolist() == after1
    assert _run(_ring(activation, 2.5)).ctx.agent_system.by_name["Ring"].attrs["v"].tolist() \
        == after2


# ------------------------------------------------ (2) random order from its stream
def test_random_activation_is_the_documented_permutation():
    n, seed = 6, 1
    kind = A.AgentType("R", attributes=["rank"], states=["S"], tick=A.Tick(1, "random"),
                       on_tick=[A.Action.set("order", "order + 1"),
                                A.Action.set("self.rank", "order")])

    def perm(k):
        return np.random.default_rng(agent_seed(seed, "Pop", k,
                                                deterministic_hash("tick"))).permutation(n)
    for ticks in (1, 2):
        layout = _model(kind, A.Population("Pop", "R", n), time_limit=ticks + 0.5,
                        variables=[{"name": "order", "initial": 0}])
        assert not _codes(layout)
        rank = _run(layout, seed).ctx.agent_system.by_name["Pop"].attrs["rank"]
        expected = np.zeros(n)
        expected[perm(ticks)] = np.arange(1, n + 1) + n * (ticks - 1)
        assert rank.tolist() == expected.tolist()
    assert perm(1).tolist() != perm(2).tolist() != list(range(n))


# ------------------------------------------------ (3) exact instants
def test_tick_instants_are_exactly_k_dt_over_1e4_ticks():
    # 1e4 ticks, not 1e5 (suite pass 4): accumulating t += dt already drifts
    # within the first 1000 steps (the check below), so 10x that is ample.
    n, dt = 10_000, 0.1
    kind = A.AgentType("C", attributes=["k", "bad"], states=["S"], tick=A.Tick(dt, "sequential"),
                       on_tick=[A.Action.set("self.k", "self.k + 1"),
                                A.Action.set("self.bad", f"self.bad + (TNOW != self.k * {dt})")])
    env = _run(_model(kind, A.Population("P", "C", 1), time_limit=n * dt + dt / 2))
    system = env.ctx.agent_system
    pop = system.by_name["P"]
    assert system.ticks == n and pop.attrs["k"][0] == n and pop.attrs["bad"][0] == 0
    t, drift = 0.0, 0
    for k in range(1, 1001):        # what accumulating t += dt would have done instead
        t += dt
        drift += t != k * dt
    assert drift > 0


# ------------------------------------------------ (4) Game of Life blinker
def _life(activation, ticks):
    """5x5 torus, cell id = 5 * row + col; the blinker never touches the wrap."""
    around = " + ".join(
        f'AGENT("Grid", ((floor(self.id / 5) + {dr}) % 5) * 5 + (self.id % 5 + {dc}) % 5).alive'
        for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc)
    cell = A.AgentType(
        "Life", states=["S"], tick=A.Tick(1, activation),
        attributes=[A.Attribute("alive", "IF(self.id == 11 or self.id == 12 or self.id == 13, 1, 0)")],
        on_tick=[A.Action.set("self.alive", f"IF(({around}) == 3 or (self.alive == 1 and "
                                            f"({around}) == 2), 1, 0)")])
    layout = _model(cell, A.Population("Grid", "Life", 25), time_limit=ticks + 0.5)
    alive = _run(layout).ctx.agent_system.by_name["Grid"].attrs["alive"]
    return set(np.flatnonzero(alive).tolist())


def test_game_of_life_blinker_has_period_two():
    horizontal, vertical = {11, 12, 13}, {7, 12, 17}
    for ticks in range(1, 5):
        assert _life("synchronous", ticks) == (vertical if ticks % 2 else horizontal)
    assert _life("sequential", 1) != vertical      # without the double buffer it breaks


# ------------------------------------------------ (5) coexistence
LAMP = A.AgentType("Lamp", states=["Off", "On"], tick=A.Tick(1),
                   transitions=[A.Transition("Off", "On", "tick", "self.id < TNOW")])
WATCH = A.AgentType("Watch", states=["Idle", "Done"],
                    transitions=[A.Transition("Idle", "Done", "condition",
                                              'COUNT("Lamps", "On") >= 2')])
CLOCK = A.AgentType("Clock", states=["A", "B"], transitions=[
    A.Transition("A", "B", "timeout", "0.25"), A.Transition("B", "A", "timeout", "0.25")])


def test_ticks_coexist_with_events_and_an_sd_level():
    layout = _model(LAMP, WATCH, CLOCK, A.Population("Lamps", "Lamp", 4),
                    A.Population("W", "Watch", 1), A.Population("Clock", "Clock", 1),
                    time_limit=5.5, levels=[{"name": "Lit"}],
                    flows=[{"name": "glow", "rate": 'COUNT("Lamps", "On")', "to": "Lit"}])
    assert not _codes(layout)
    env = _run(layout)
    system = env.ctx.agent_system
    # Agent j turns on at the tick t = j + 1: COUNT On = 0, 1, 2, 3 on [0, 4), 4 on [4, 5.5].
    assert system.count("Lamps", "On") == 4
    assert env.ctx.level_system.levels["Lit"].level == 0 + 1 + 2 + 3 + 4 * 1.5
    # The condition sees the tick at its own instant (ticks run, then the instant settles).
    assert system.by_name["W"].entered[0] == 2.0
    clock = system.by_name["Clock"]
    alone = _run(_model(CLOCK, A.Population("Clock", "Clock", 1), time_limit=5.5))
    assert sum(clock.entries) == 22
    assert clock.entered.tolist() == alone.ctx.agent_system.by_name["Clock"].entered.tolist()


# ------------------------------------------------ (6) no tick, no change; numpy path
def test_a_model_without_ticks_schedules_none():
    layout = _model(CLOCK, A.Population("Clock", "Clock", 1), time_limit=2)
    system = _run(layout).ctx.agent_system
    assert system._tick_pops == [] and system.ticks == 0


def test_synchronous_columns_equal_the_forced_loop(monkeypatch):
    kind = A.AgentType("V", attributes=[A.Attribute("x", "self.id")], states=["S"],
                       tick=A.Tick(0.5),
                       on_tick=[A.Action.set("self.x", "self.x * 2 + self.id"),
                                A.Action.set("self.x", 'self.x + MEAN("P", "x")')])
    layout = _model(kind, A.Population("P", "V", 7), time_limit=3)
    fast = _run(layout).ctx.agent_system.by_name["P"]
    assert fast.type.tick_vecs
    monkeypatch.setattr(A.AgentSystem, "_tick_columns", lambda self, pop, idx: False)
    slow = _run(layout).ctx.agent_system.by_name["P"]
    assert fast.attrs["x"].tolist() == slow.attrs["x"].tolist()
    # Both sets read the tick start: x' = x + mean(x), the doubling is overwritten.
    x = np.arange(7.0)
    for _ in range(6):
        x = x + x.mean()
    assert fast.attrs["x"].tolist() == x.tolist()


# ------------------------------------------------ validation, schema, API
@pytest.mark.parametrize("patch, code", [
    ({"tick": {"dt": 0}}, "E_AGENT_TICK"),
    ({"tick": {"dt": 1, "activation": "parallel"}}, "E_AGENT_TICK"),
    ({"tick": {"dt": 1, "phase": 1}}, "E_AGENT_TICK"),
    ({"tick": {"dt": 1, "every": 2}}, "E_AGENT_TICK"),
    ({"tick": None}, "E_AGENT_TICK"),                               # on_tick with no tick
    ({"transitions": [{"from": "S", "to": "S", "tick": 1}]}, "E_AGENT_TRANSITION"),
    ({"transitions": [{"from": "S", "to": "S", "tick": "self.nope > 0"}]}, "E_AGENT_ATTRIBUTE"),
])
def test_bad_tick_specs(patch, code):
    layout = _ring("synchronous", 2)
    layout["agent_types"][0].update(patch)
    if layout["agent_types"][0]["tick"] is None:
        del layout["agent_types"][0]["tick"]
    assert code in _codes(layout)


def test_population_tick_overrides_the_type():
    layout = _ring("synchronous", 1.5)
    del layout["agent_types"][0]["tick"]
    layout["populations"][0]["tick"] = {"dt": 1, "activation": "sequential"}
    assert not _codes(layout)
    assert _run(layout).ctx.agent_system.by_name["Ring"].attrs["v"].tolist() == [30, 30, 30]


def test_json_schema_and_record_round_trip():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    layout = _model(LAMP, A.Population("Lamps", "Lamp", 4, tick=A.Tick(activation="random")),
                    time_limit=3)
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(migrate(layout)))
    assert not errors, errors[0].message
    data = json.loads(json.dumps(layout))
    assert A.AgentType.from_dict(data["agent_types"][0]) == LAMP
    assert A.Population.from_dict(data["populations"][0]).tick == {"activation": "random"}
