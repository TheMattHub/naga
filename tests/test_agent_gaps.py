"""PHASE6 A9: the gaps the classic ABM suite found (ARCHITECTURE 5.25, D106) --
`sender` in a target's where and the one-to-one claim, patch regrowth, the
trig functions, and tick-local `let` values. Every expected value is hand
derived or rebuilt from the documented stream, never read off the engine."""
import math

import numpy as np
import pytest

from nagasim.core import agents as A
from nagasim.core.expressions import compile_expression, ExpressionScope
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until
from conftest import agent_rng, problem_codes


def _model(*records, time_limit=1.5):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _start(layout, seed=1):
    """Deliberately NOT `conftest.run_env`: several tests call `_start` then
    `step_until` themselves multiple times, at different instants, to sample
    mid-run state -- `run_env` always steps to completion in one call. These
    models have no levels/resources/timeseries, so skipping the warm start
    and sampler changes nothing."""
    run_len = layout["simulation"]["time_limit"]
    env = initialize_sim_env(seed)
    env.ctx.sim_horizon = run_len
    prepare_model(env, layout)
    env.ctx.agent_system.start(run_len)
    return env


def _run(layout, seed=1):
    env = _start(layout, seed)
    step_until(env, layout["simulation"]["time_limit"])
    return env


def _codes(layout):
    return problem_codes(layout)


def _pop(env, name):
    return env.ctx.agent_system.by_name[name]


# ------------------------------------------------ (1) sender in a where
POINTS = [[8, 5], [5, 8.0001], [2, 2], [5, 5], [7, 7], [9.5, 9.5], [3, 4], [6.5, 2.5],
          [0, 0], [5, 2], [2.9, 6.1], [10, 10]]
SENDER, RADIUS = (5.0, 5.0), 3.0


def _pairing(where):
    cand = A.AgentType("Cand", states=["Idle", "Hit"], space=A.Space("continuous", [0, 0, 10, 10]),
                       transitions=[A.Transition("Idle", "Hit", "message", "hit")])
    sender = A.AgentType("Sender", attributes=[A.Attribute("r", str(RADIUS))], states=["A", "B"],
                         space=A.Space("continuous", [0, 0, 10, 10]),
                         transitions=[A.Transition("A", "B", "timeout", "1", actions=[
                             A.Action.send("hit", {"population": "C", "where": where})])])
    return _model(cand, sender,
                  A.Population("C", "Cand", len(POINTS), placement={"kind": "table",
                                                                     "points": POINTS}),
                  A.Population("S", "Sender", 1, placement={"kind": "table",
                                                            "points": [list(SENDER)]}))


# Squared distance within sender.r (one comparison: the numpy path), and the
# same disc through `and` (outside the numpy subset: the loop).
DISC = ("(self.x - sender.x) * (self.x - sender.x) + (self.y - sender.y) * (self.y - sender.y)"
        " <= sender.r * sender.r")
DISC_LOOP = f"({DISC}) and sender.id == 0"


@pytest.mark.parametrize("where", [DISC, DISC_LOOP])
def test_a_where_reading_the_sender_equals_brute_force(monkeypatch, where):
    want = [i for i, (x, y) in enumerate(POINTS)
            if (x - SENDER[0]) ** 2 + (y - SENDER[1]) ** 2 <= RADIUS ** 2]
    assert want == [0, 3, 4, 6, 7, 9, 10]    # (8, 5) sits exactly on the rim: inclusive
    layout = _pairing(where)
    assert not _codes(layout)
    assert (A.vectorise(where) is not None) == (where == DISC)
    hit = np.flatnonzero(_pop(_run(layout), "C").state == 1).tolist()
    assert hit == want
    monkeypatch.setattr(A.compile, "vectorise", lambda *args, **kwargs: None)   # the loop: same set
    assert np.flatnonzero(_pop(_run(layout), "C").state == 1).tolist() == want


def _claim(wolves, sheep_cells):
    grid = A.Space("grid", size=[3, 1], torus=False)
    sheep = A.AgentType("Sheep", states=["Alive"], space=grid)
    wolf = A.AgentType("Wolf", states=["Hunt", "Fed"], space=grid, transitions=[
        A.Transition("Hunt", "Fed", "timeout", "1", actions=[A.Action.remove(
            {"population": "Sheep", "where": "self.x == sender.x and self.y == sender.y",
             "random": True})])])
    return _model(sheep, wolf,
                  A.Population("Sheep", "Sheep", len(sheep_cells),
                               placement={"kind": "table", "points": sheep_cells}),
                  A.Population("Wolves", "Wolf", len(wolves),
                               placement={"kind": "table", "points": wolves}))


def test_the_one_to_one_claim_removes_one_candidate_drawn_from_the_actors_stream():
    # Sheep 0-4 share the wolf's cell (0, 0), sheep 5-7 sit next door.
    cells = [[0, 0]] * 5 + [[1, 0]] * 3
    layout = _claim([[0, 0]], cells)
    assert not _codes(layout)
    for seed in (1, 2, 3):
        alive = _pop(_run(layout, seed), "Sheep").state >= 0
        # The wolf's own stream, rebuilt: its first draw picks among the 5.
        want = int(agent_rng(seed, "Wolves", 0).integers(5))
        assert np.flatnonzero(~alive).tolist() == [want]


def test_two_wolves_cannot_claim_the_same_sheep():
    # One sheep at (2, 0) and two wolves on it: the first claims it, the second
    # finds no candidate (no draw); the sheep next door is never touched.
    layout = _claim([[2, 0], [2, 0]], [[2, 0], [1, 0]])
    pop = _pop(_run(layout), "Sheep")
    assert (pop.state >= 0).tolist() == [False, True] and pop.live == 1


def test_sender_is_refused_outside_a_where_and_checked_inside_one():
    def layout(**over):
        where = over.get("where", "self.x == sender.x")
        kind = A.AgentType("T", attributes=[A.Attribute("v")], states=["A", "B"],
                           space=A.Space("continuous", [0, 0, 1, 1]),
                           transitions=[A.Transition("A", "B", "timeout", "1", actions=[
                               over.get("action", A.Action.remove(
                                   {"population": "P", "where": where}))])])
        return _model(kind, A.Population("P", "T", 2))
    assert not _codes(layout())
    assert not _codes(layout(where="self.v < sender.v and sender.id != self.id"))
    assert "E_AGENT_ATTRIBUTE" in _codes(layout(where="sender.nope > 0"))
    assert "E_AGENT_EXPRESSION" in _codes(layout(where="sender.v > RAND()"))      # #132 holds
    assert "E_AGENT_EXPRESSION" in _codes(layout(action=A.Action.set("self.v", "sender.v")))
    assert "E_AGENT_EXPRESSION" in _codes(layout(where="sender == 1"))
    with pytest.raises(Exception, match="sender"):
        compile_expression("sender.v").evaluate(ExpressionScope())


# ------------------------------------------------ (2) patch regrowth
def _field(time_limit, grow):
    grid = A.Space("grid", size=[2, 1], patch_tick=1, patches=[
        {"name": "g", "init": "cx * 10", "grow": {"rate": 3, "max": 7}},
        {"name": "s", "init": "4 + cx", "grow": grow}])
    eater = A.AgentType("E", states=["A", "B"], space=grid, transitions=[
        A.Transition("A", "B", "timeout", "0.5", actions=[A.Action.set("patch.s", "0")])])
    return _model(eater, A.Population("E", "E", 1, placement={"kind": "table",
                                                              "points": [[0, 0]]}),
                  time_limit=time_limit)


def test_grow_reaches_its_max_and_clips_exactly():
    layout = _field(3.5, {"rate": 1.5})
    assert not _codes(layout)
    env = _start(layout)
    system = env.ctx.agent_system
    # g: cell 0 gains 3 per tick up to 7; cell 1 starts at 10, above max: left alone.
    # s: max defaults to each cell's init (4, 5); cell 0 is eaten at t=0.5.
    for t, g0, s0 in ((1.5, 3.0, 1.5), (2.5, 6.0, 3.0), (3.5, 7.0, 4.0)):
        step_until(env, t)
        assert system.patch_values("E", "g").tolist() == [[g0, 10.0]]
        assert system.patch_values("E", "s").tolist() == [[s0, 5.0]]


@pytest.mark.parametrize("grow", [{"rate": -1}, {"rate": 1, "cap": 2}, {"max": 2}, 5])
def test_a_bad_grow_is_refused(grow):
    assert "E_AGENT_PATCH" in _codes(_field(1.5, grow))


def test_grow_needs_a_patch_tick():
    grid = A.Space("grid", size=[2, 1], patches=[{"name": "g", "grow": {"rate": 1}}])
    assert "E_AGENT_PATCH" in _codes(_model(A.AgentType("E", states=["A"], space=grid),
                                            A.Population("E", "E", 1)))


# ------------------------------------------------ (3) trigonometry
@pytest.mark.parametrize("text, want", [
    ("SIN(PI() / 6)", math.sin(math.pi / 6)), ("COS(0)", 1.0), ("TAN(PI() / 4)", math.tan(math.pi / 4)),
    ("ATAN2(1, -1)", 3 * math.pi / 4), ("SQRT(2)", math.sqrt(2)), ("DEG(PI())", 180.0),
    ("RAD(180)", math.pi), ("DEG(ATAN2(-1, 0))", -90.0), ("PI()", math.pi)])
def test_trig_functions_are_pythons_own(text, want):
    assert compile_expression(text).evaluate(ExpressionScope()) == want


TRIG = ("COS(RAD(self.heading)) * SIN(self.a) + ATAN2(self.a, 1 + self.id) + SQRT(self.a)"
        " + TAN(self.x / 100) - abs(self.y - 50) / DEG(1)")


def _trig():
    kind = A.AgentType("B", attributes=[A.Attribute("a", "self.id * 0.37"), A.Attribute("u")],
                       states=[A.State("S", entry=[A.Action.set("self.heading", "UNIF(0, 360)")])],
                       space=A.Space("continuous", [0, 0, 100, 100]), tick=A.Tick(1),
                       on_tick=[A.Action.set("self.u", TRIG), A.Action.set("self.a", "self.a + 1")])
    return _model(kind, A.Population("B", "B", 40), time_limit=3.5)


def test_the_numpy_path_of_the_math_calls_equals_the_loop(monkeypatch):
    assert A.vectorise(TRIG) is not None
    layout = _trig()
    assert not _codes(layout)
    fast = _pop(_run(layout), "B")
    assert fast.type.tick_vecs                      # the columnar tick really ran
    fast = fast.attrs["u"].tolist()
    monkeypatch.setattr(A.compile, "vectorise", lambda *args, **kwargs: None)
    assert _pop(_run(layout), "B").attrs["u"].tolist() == fast


# ------------------------------------------------ (4) let
def _let(activation, time_limit=3.5):
    kind = A.AgentType("L", attributes=[A.Attribute(n) for n in ("a", "b", "c")], states=["S"],
                       tick=A.Tick(1, activation), let={"r": "UNIF(0, 1)", "r2": "2 * r"},
                       on_tick=[A.Action.set("self.a", "r"), A.Action.set("self.b", "r2")],
                       transitions=[A.Transition("S", "S", "tick", "r < 2",
                                                 actions=[A.Action.set("self.c", "r")])])
    return _model(kind, A.Population("L", "L", 5), time_limit=time_limit)


@pytest.mark.parametrize("activation", ["synchronous", "random", "sequential"])
def test_a_let_is_one_draw_shared_by_every_action_of_the_tick(activation):
    layout = _let(activation)
    assert not _codes(layout)
    pop = _pop(_run(layout, seed=3), "L")
    # Agent i's own stream: one uniform per tick, three ticks, nothing else
    # drawn -- so the last tick's r is its draw call 2 (D107).
    want = [agent_rng(3, "L", i, 2).uniform(0, 1) for i in range(5)]
    assert pop.attrs["a"].tolist() == want            # identical under every activation
    assert pop.attrs["c"].tolist() == want            # the transition pass reads the same r
    assert pop.attrs["b"].tolist() == [2 * r for r in want]


@pytest.mark.parametrize("let, code", [
    ({"k": "1"}, "E_AGENT_TICK"),                     # shadows the parameter k
    ({"TNOW": "1"}, "E_AGENT_TICK"),                  # reserved
    ({"r": "nope + 1"}, "E_AGENT_EXPRESSION"),
    ({"r": "q", "q": "1"}, "E_AGENT_EXPRESSION"),     # in order: q is not yet defined
])
def test_a_bad_let_is_refused(let, code):
    kind = A.AgentType("L", parameters={"k": 1}, attributes=[A.Attribute("a")], states=["S"],
                       tick=A.Tick(1), let=let, on_tick=[A.Action.set("self.a", "1")])
    assert code in _codes(_model(kind, A.Population("L", "L", 1)))


def test_a_let_is_visible_only_to_the_tick():
    kind = A.AgentType("L", attributes=[A.Attribute("a")], tick=A.Tick(1), let={"r": "1"},
                       states=[A.State("S", entry=[A.Action.set("self.a", "r")])])
    assert "E_AGENT_EXPRESSION" in _codes(_model(kind, A.Population("L", "L", 1)))


def test_the_json_schema_accepts_let_and_grow():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.core.schema import migrate
    from nagasim.modelschema import build_json_schema
    layout = _field(1.5, {"rate": 1})
    layout["agent_types"][0]["let"] = {"r": "UNIF(0, 1)"}
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(
        migrate(layout)))
    assert not errors, [e.message for e in errors]
