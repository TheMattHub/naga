"""PHASE6 A4: grid space and patches (D102). Every expected value is hand
derived: neighbourhood counts from a drawn 5 x 5 layout, one diffusion step
of a spike from NetLogo's formula, evaporation as a power, move_random from
the agent's rebuilt stream, and move_free / the synchronous tick by walking
the rule through by hand.
"""
import numpy as np
import pytest

from nagasim.core import agents as A
from conftest import AgentStream, problem_codes, run_env

MOORE = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))


def _model(*records, time_limit=1.5):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _run(layout, seed=1):
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env


def _pop(env, name):
    return env.ctx.agent_system.by_name[name]


def _codes(layout):
    return problem_codes(layout)


def _grid(**extra):
    return A.Space("grid", size=[5, 5], **extra)


# Five "Cells" agents around a probe at (0, 0) of a 5 x 5 grid (v = its index):
# (0, 0) shares the probe's own cell, (1, 0), (0, 1), (1, 1) touch it on any
# grid, (4, 4) and (4, 0) only across a torus edge, (2, 2) never.
CELLS = [[0, 0], [1, 0], [0, 1], [1, 1], [4, 4], [4, 0], [2, 2]]
PROBES = {
    "moore": 'NEIGHBOURS("Cells")',
    "busy": 'NEIGHBOURS("Cells", "Busy")',
    "sum": 'NEIGHBOUR_SUM("Cells", "v")',
    "mean": 'NEIGHBOUR_MEAN("Cells", "v", "Busy")',
    "here": 'CELL_COUNT("Cells")',
    "corner": 'CELL_COUNT("Cells", -1, -1)',
    "patch": 'PATCH("food", -1, 0)',
}


def _neighbourhood_env(torus, neighbourhood):
    space = dict(kind="grid", size=[5, 5], torus=torus, neighbourhood=neighbourhood)
    cells = A.AgentType("Cells", attributes=[A.Attribute("v", "self.id")],
                        states=["Idle", "Busy"], space=space)
    probe = A.AgentType(
        "Probe", attributes=[A.Attribute(k, "-1") for k in PROBES], states=["A", "B"],
        space={**space, "patches": [{"name": "food", "init": "cx * 10 + cy"}]},
        transitions=[A.Transition("A", "B", "timeout", "0", actions=[
            A.Action.set(f"self.{k}", text) for k, text in PROBES.items()])])
    # Busy: agents 1, 3, 4 (the first three by index are Idle... then Busy).
    return _run(_model(cells, probe,
                       A.Population("Cells", "Cells", len(CELLS),
                                    initial_states={"Idle": 1, "Busy": 3},
                                    placement={"kind": "table", "points": CELLS}),
                       A.Population("P", "Probe", 1, placement={"kind": "table",
                                                                "points": [[0, 0]]})))


@pytest.mark.parametrize("torus, hood, want", [
    # Idle: 0; Busy: 1, 2, 3; Idle again: 4, 5, 6 (the type's initial state).
    (False, "moore", dict(moore=3, busy=3, sum=1 + 2 + 3, mean=2.0, here=1, corner=0, patch=0.0)),
    (True, "moore", dict(moore=5, busy=3, sum=1 + 2 + 3 + 4 + 5, mean=2.0, here=1, corner=1,
                         patch=40.0)),
    (False, "von_neumann", dict(moore=2, busy=2, sum=1 + 2, mean=1.5, here=1, corner=0,
                                patch=0.0)),
    (True, "von_neumann", dict(moore=3, busy=2, sum=1 + 2 + 5, mean=1.5, here=1, corner=1,
                               patch=40.0)),
])
def test_neighbourhoods_bounded_and_torus(torus, hood, want):
    env = _neighbourhood_env(torus, hood)
    probe = _pop(env, "P")
    got = {k: probe.attrs[k][0] for k in PROBES}
    assert got == want


def _patch_env(size=(5, 5), torus=True, hood="moore", init="(cx == 2) * (cy == 2) * 100",
               diffuse=0.0, evaporate=0.0, ticks=1):
    space = A.Space("grid", size=list(size), torus=torus, neighbourhood=hood, patch_tick=1,
                    patches=[{"name": "p", "init": init, "diffuse": diffuse,
                              "evaporate": evaporate}])
    return _run(_model(A.AgentType("Cell", states=["S"], space=space),
                       A.Population("Pop", "Cell", 1), time_limit=ticks + 0.5))


@pytest.mark.parametrize("hood, offsets", [("moore", MOORE),
                                           ("von_neumann", ((0, -1), (-1, 0), (1, 0), (0, 1)))])
def test_one_diffusion_step_of_a_spike_is_the_formula(hood, offsets):
    rate = 0.4
    values = _pop(_patch_env(hood=hood, diffuse=rate), "Pop").system.patch_values("Cell", "p")
    want = np.zeros((5, 5))
    want[2, 2] = 100 * (1 - rate)
    for dx, dy in offsets:
        want[2 + dy, 2 + dx] = 100 * rate / len(offsets)
    assert np.array_equal(values, want)


def test_bounded_corner_keeps_the_shares_that_fall_off():
    rate = 0.4
    values = _pop(_patch_env(torus=False, init="(cx == 0) * (cy == 0) * 80", diffuse=rate),
                  "Pop").system.patch_values("Cell", "p")
    share = 80 * rate / 8
    assert values[0, 0] == pytest.approx(80 * (1 - rate) + 5 * share)    # 5 of 8 off-grid
    assert values[0, 1] == values[1, 0] == values[1, 1] == share
    assert values.sum() == pytest.approx(80)


@pytest.mark.parametrize("torus", [True, False])
def test_diffusion_conserves_the_total(torus):
    def total(diffuse):         # the same seed draws the same random init either way
        env = _patch_env(size=(7, 4), torus=torus, init="UNIF(0, 10)", diffuse=diffuse,
                         ticks=10)
        return env.ctx.agent_system.patch_values("Cell", "p")
    start, end = total(0.0), total(0.3)
    assert len(np.unique(start)) == 28 and not np.array_equal(start, end)
    assert end.sum() == pytest.approx(start.sum(), rel=1e-12)


def test_evaporation_is_a_factor_per_tick():
    values = _pop(_patch_env(init="8", evaporate=0.25, ticks=3), "Pop").system \
        .patch_values("Cell", "p")
    assert np.array_equal(values, np.full((5, 5), 8 * 0.75 ** 3))


def test_move_random_matches_the_rebuilt_stream():
    walker = A.AgentType("W", states=["S"], space=_grid(), tick={"dt": 1},
                         on_tick=[A.Action.move_random()])
    env = _run(_model(walker, A.Population("Walk", "W", 1, placement={
        "kind": "table", "points": [[2, 2]]}), time_limit=6.5))
    pop = _pop(env, "Walk")
    rng = AgentStream(env.ctx.seed, "Walk", 0)
    x, y = 2, 2
    for _ in range(6):
        cells = [(x + dx, y + dy) for dx, dy in MOORE if 0 <= x + dx < 5 and 0 <= y + dy < 5]
        x, y = cells[int(rng.integers(len(cells)))]
    assert (pop.cx[0], pop.cy[0]) == (x, y)
    assert (pop.x1[0], pop.y1[0]) == (x + 0.5, y + 0.5)        # the cell's centre


def test_step_and_move_to_on_a_bounded_grid():
    mover = A.AgentType("M", states=["A", "B", "C"], space=_grid(), transitions=[
        A.Transition("A", "B", "timeout", "1", actions=[A.Action.move_to([4, 3]),
                                                        A.Action.step(1, 0)]),   # off: stays
        A.Transition("B", "C", "timeout", "1", actions=[A.Action.step(-2, "self.id - 1")])])
    pop = _pop(_run(_model(mover, A.Population("Ms", "M", 1), time_limit=3)), "Ms")
    assert (pop.cx[0], pop.cy[0]) == (2, 2)
    assert pop.occ[pop.state[0], 2, 2] == 1 and pop.occ.sum() == 1


def test_move_free_fills_the_hole_in_index_order():
    """A 3 x 3 grid full but for (2, 2), sequential ticks, everyone move_free:
    only an agent touching the hole has a candidate -- (1, 1) takes it, then
    (2, 1) takes the hole (1, 1) left, then (1, 2) the hole (2, 1) left."""
    full = [[x, y] for y in range(3) for x in range(3) if (x, y) != (2, 2)]
    kind = A.AgentType("S", states=["S"], space=A.Space("grid", size=[3, 3]),
                       tick={"dt": 1, "activation": "sequential"},
                       on_tick=[A.Action.move_random(free=True)])
    pop = _pop(_run(_model(kind, A.Population("Sch", "S", 8, placement={
        "kind": "table", "points": full}))), "Sch")
    moved = {4: (2, 2), 5: (1, 1), 7: (2, 1)}
    for i, (x, y) in enumerate(full):
        assert (pop.cx[i], pop.cy[i]) == moved.get(i, (x, y))
    assert set(np.flatnonzero(pop.draws)) == {4, 5, 7}     # a stayer draws nothing
    assert pop.occ[0, 2, 1] == 0 and pop.occ.sum() == 8


@pytest.mark.parametrize("activation, seen, final", [("synchronous", [0, 0, 0], 1.0),
                                                     ("sequential", [0, 1, 2], 3.0)])
def test_synchronous_tick_reads_tick_start_patches(activation, seen, final):
    """Three agents in one cell each add 1 to its patch; in a synchronous tick
    each reads the tick-start 0 (and writes 1), in index order each reads the
    last write."""
    kind = A.AgentType("T", attributes=[A.Attribute("seen", "-1")], states=["S"],
                       space=_grid(patches=[{"name": "p"}]),
                       tick={"dt": 1, "activation": activation},
                       on_tick=[A.Action.set("self.seen", 'PATCH("p")'),
                                A.Action.set("patch.p", 'PATCH("p") + 1')])
    env = _run(_model(kind, A.Population("Ts", "T", 3, placement={
        "kind": "table", "points": [[1, 1]]})))
    assert _pop(env, "Ts").attrs["seen"].tolist() == seen
    assert env.ctx.agent_system.patch_values("T", "p")[1, 1] == final


def _bad(space=None, on_tick=(), transitions=(), placement=None, attributes=()):
    kind = A.AgentType("G", attributes=list(attributes) or [A.Attribute("x")],
                       states=["S", "T"], space=space, transitions=list(transitions),
                       tick={"dt": 1} if on_tick else None, on_tick=list(on_tick))
    return _codes(_model(kind, A.Population("Gs", "G", 2, placement=placement)))


GOOD = {"kind": "grid", "size": [4, 4], "patches": [{"name": "food", "init": "cx + cy",
                                                    "diffuse": 0.5}], "patch_tick": 1}


@pytest.mark.parametrize("code, kwargs", [
    ("E_AGENT_SPACE", dict(space={"kind": "grid", "size": [0, 4]})),
    ("E_AGENT_SPACE", dict(space={"kind": "grid", "size": [4, 4], "neighbourhood": "hex"})),
    ("E_AGENT_PLACEMENT", dict(space=GOOD, placement={"kind": "table", "points": [[4, 0]]})),
    ("E_AGENT_PLACEMENT", dict(space=GOOD, placement={"kind": "random",
                                                      "bounds": [0, 0, 1, 1]})),
    ("E_AGENT_GRID", dict(space=None, on_tick=[A.Action.step(1, 0)])),
    ("E_AGENT_GRID", dict(space={"kind": "continuous", "bounds": [0, 0, 5, 5]},
                          on_tick=[A.Action.set("self.x", 'NEIGHBOURS("Gs")')])),
    ("E_AGENT_GRID", dict(space=None, on_tick=[A.Action.set("patch.food", "1")])),
    ("E_AGENT_PATCH", dict(space=GOOD, on_tick=[A.Action.set("self.x", 'PATCH("fod")')])),
    ("E_AGENT_PATCH", dict(space=GOOD, on_tick=[A.Action.set("patch.fod", "1")])),
    ("E_AGENT_PATCH", dict(space={**GOOD, "patch_tick": None})),
    ("E_AGENT_PATCH", dict(space={**GOOD, "patches": [{"name": "f", "diffuse": 2}]})),
    ("E_AGENT_PATCH", dict(space={**GOOD, "patches": [{"name": "f", "init": "self.x"}]})),
    ("E_AGENT_ACTION", dict(space=GOOD, on_tick=[{"move_random": "maybe"}])),
    ("E_AGENT_ACTION", dict(space=GOOD, on_tick=[{"step": [0.5, 0]}])),
    ("E_AGENT_ACTION", dict(space=GOOD, on_tick=[{"move_to": [1.5, 0]}])),
    ("E_POPULATION_UNKNOWN", dict(space=GOOD, on_tick=[A.Action.set("self.x",
                                                                    'NEIGHBOURS("Nope")')])),
    ("E_AGENT_RATE_FORM", dict(space=GOOD, transitions=[
        A.Transition("S", "T", "rate", 'NEIGHBOURS("Gs")')])),
])
def test_grid_validation_codes(code, kwargs):
    assert code in _bad(**kwargs)


def test_a_valid_grid_model_has_no_errors():
    assert not _bad(space=GOOD, placement={"kind": "table", "points": [[3, 3]]}, on_tick=[
        A.Action.set("self.x", 'NEIGHBOUR_MEAN("Gs", "x", "S") + CELL_COUNT("Gs", 1, 0)'),
        A.Action.set("patch.food", 'PATCH("food", 0, 1) * 0.5'),
        A.Action.step(1, "self.id"), A.Action.move_random(free=True),
        A.Action.move_to([0, 0])])
