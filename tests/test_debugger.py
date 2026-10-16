"""Run debugger (PHASE7 7.5): `SimSession.advance(until=)`, the stops and readers of
`core/debugger.py`, and the exit check -- a paused, stepped and inspected run gives
the golden hash of an uninterrupted one."""
import json
from pathlib import Path

import pytest

from conftest import chain, create, delay, dispose, layout

from nagasim.core.context import build_context
from nagasim.core.debugger import (Stops, entity_view, readonly_expression, watch_functions,
                                   watch_names, watch_values)
from nagasim.core.expressions import FUNCTIONS, ExpressionError, functions_with
from nagasim.core.session import SimSession
from nagasim.core.sim_classes import Entity
from nagasim.core.trace import TraceRecorder
from tools.make_golden import GOLDEN_DIR, MODELS as GOLDEN_MODELS, SEEDS, golden_hash, payload_of

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _load(name):
    return json.loads((MODELS / name).read_text(encoding="utf-8"))


def _line(**sections):
    """create (one per time unit, six) -> delay 2.5 -> dispose, to t = 20."""
    blocks = [create(max_arrivals=6), delay("delay_1", 2.5), dispose()]
    sections.setdefault("simulation", {"time_limit": 20, "warmup": 0, "replications": 1})
    return layout(blocks, chain(*blocks), **sections)


def _frames_equal(got, expected):
    assert set(got) == set(expected)
    for name in expected:
        assert got[name].equals(expected[name]), name


# -- D195: entity ids belong to the run ----------------------------------------------
def test_two_sessions_stepped_in_turn_equal_each_alone():
    single = _load("single_server.json")
    alone = SimSession(_line(), seed=0).finish(), SimSession(single, seed=1).finish()
    a, b = SimSession(_line(), seed=0), SimSession(single, seed=1)
    for t in (1.5, 3.5, 5.5):
        a.advance(to=t)
        b.advance(to=40 * t)
    _frames_equal(a.finish(), alone[0])
    _frames_equal(b.finish(), alone[1])


def test_a_bare_entity_draws_from_the_newest_context():
    ctx = build_context(0)
    assert Entity().id == 1
    assert next(ctx.entity_ids) == 2


def _routed():
    """create (one per time unit, four) -> Policy DECIDE "route" -> a | b."""
    blocks = [create(max_arrivals=4),
              {"name": "route", "description": "route", "type": "DECIDE", "position": [1, 0],
               "params": {"mode": "Policy", "probs": [], "conditions": [], "policy": "route",
                          "observation": [], "fallback": "first", "queue_exprs": []}},
              dispose("a"), dispose("b")]
    links = [{"source": s, "target": t} for s, t in
             (("create_1", "route"), ("route", "a"), ("route", "b"))]
    return layout(blocks, links, simulation={"time_limit": 10, "warmup": 0, "replications": 1})


# -- SimSession.advance(until=) ------------------------------------------------
def test_until_stops_before_the_event_and_leaves_the_clock_where_it_was():
    s = SimSession(_line(), seed=0)
    asked = []

    def third_time():
        asked.append((s._steps, s.now, s.env.peek()))
        return len(asked) == 3

    assert s.advance(until=third_time) == "until"
    assert [steps for steps, _now, _next in asked] == [0, 1, 2]     # asked before each event
    assert (s._steps, s.now, s.env.peek()) == asked[-1]             # and that one did not run
    assert s.advance(until=lambda: True) == "until" and s._steps == 2


def test_until_and_to_the_first_to_hold_wins_and_until_wins_a_tie():
    s = SimSession(_line(), seed=0)
    late = lambda: s.now >= 3
    assert s.advance(to=1.55, until=late) == "time" and s.now == 1.55
    # Both hold before the same event: "until", and the clock is not moved to `to`.
    between = (s.now + s.env.peek()) / 2
    assert s.now < between
    assert s.advance(to=between, until=lambda: True) == "until" and s.now == 1.55
    assert s.advance(to=10, until=late) == "until" and s.now == 3.0


def test_until_is_not_asked_while_a_decision_is_pending_or_once_the_run_is_over():
    """Review focus: a call when nothing can move answers the same state again."""
    s = SimSession(_routed(), seed=0)
    assert s.advance() == "decision"
    asked = []
    waiting = (s._steps, s.now)
    assert s.advance(until=lambda: asked.append(1) or True) == "decision"
    assert s.advance(to=5, until=lambda: asked.append(1) or True) == "decision"
    assert asked == [] and (s._steps, s.now) == waiting
    s.finish()
    assert s.advance(until=lambda: asked.append(1) or True) == "drained" and asked == []


def test_until_is_not_asked_on_the_step_that_raises_a_decision():
    s = SimSession(_routed(), seed=0)
    asked = []
    assert s.advance(until=lambda: asked.append(s._steps) or False) == "decision"
    assert asked == [0, 1]          # the next step ran the DECIDE: no question once it was pending


def test_until_is_not_asked_at_the_horizon_with_events_still_pending():
    s = SimSession(_line(simulation={"time_limit": 3.2, "warmup": 0, "replications": 1}), seed=0)
    asked = []
    assert s.advance(until=lambda: asked.append(s._steps) or False) == "horizon"
    assert asked and s.env.peek() > s.horizon           # the events before the limit were asked
    before = list(asked)
    assert s.advance(until=lambda: asked.append(s._steps) or True) == "horizon"
    assert asked == before


def test_single_stepping_to_the_end_is_the_plain_run():
    plain = SimSession(_line(), seed=0)
    expected = plain.finish()
    s = SimSession(_line(), seed=0)
    state = "until"
    while state == "until":
        n = s._steps
        state = s.advance(until=lambda: s._steps > n)
    assert state == "drained" and s._steps == plain._steps
    _frames_equal(s.finish(), expected)


# -- Stops -----------------------------------------------------------------------
def test_a_block_stop_reports_the_entity_and_continuing_moves_on():
    s = SimSession(_line(), seed=0)
    stops = Stops(s, [{"block": "delay_1"}, {"block": "create_1"}])
    assert s.advance(until=stops) == "until" and s.now == 0.0
    assert stops.take_hits() == [
        {"stop": 1, "block": "create_1", "entity_id": 1, "entity_type": "DEFAULT"},
        {"stop": 0, "block": "delay_1", "entity_id": 1, "entity_type": "DEFAULT"}]
    assert stops.take_hits() == []
    assert s.advance(until=stops) == "until" and s.now == 1.0
    assert [hit["entity_id"] for hit in stops.take_hits()] == [2, 2]
    stops.close()


def test_an_instance_name_catches_entry_from_outside_only():
    trace = TraceRecorder()
    s = SimSession(_load("two_cell_line.json"), seed=0, trace=trace)
    stops = Stops(s, [{"block": "cell_1"}])
    hits = []
    while s.now < 200 and s.advance(to=200, until=stops) == "until":
        hits += stops.take_hits()
    moves = [e for e in trace.events
             if e.kind == "entity_moved" and (e.target or "").startswith("cell_1/")]
    entered = [e for e in moves if not e.block.startswith("cell_1/")]
    assert len(moves) > len(entered) > 0            # the rework loop moves inside the cell
    assert [h["entity_id"] for h in hits] == [e.entity_id for e in entered]
    assert {h["block"] for h in hits} == {"cell_1"}


def test_a_stop_behind_a_delayed_link_fires_at_departure():
    blocks = [create(max_arrivals=2), dispose()]
    links = [{"source": "create_1", "target": "dispose_1",
              "delay": {"distribution": "Constant", "dist_params": {"value": 4.0}}}]
    s = SimSession(layout(blocks, links), seed=0)
    stops = Stops(s, [{"block": "dispose_1"}])
    assert s.advance(until=stops) == "until"
    assert s.now == 0.0 and stops.take_hits()[0]["entity_id"] == 1     # it arrives at t = 4


def test_an_expression_stop_fires_on_the_flip_once_and_never_when_already_true():
    s = SimSession(_line(variables=[{"name": "x", "initial": 0}]), seed=0)
    stops = Stops(s, [{"expr": "TNOW >= 0"}, {"expr": "x > 0"}])
    assert s.advance(to=2.5, until=stops) == "time" and stops.take_hits() == []
    s.set_variable("x", 1)
    assert s.advance(to=9, until=stops) == "until" and s.now == 2.5       # before the next event
    assert stops.take_hits() == [{"stop": 1, "expr": "x > 0"}]
    assert s.advance(to=4.5, until=stops) == "time"                       # still true: no hit
    s.set_variable("x", 0)
    s.advance(to=5.5, until=stops)
    s.set_variable("x", 2)
    assert s.advance(until=stops) == "until"
    assert stops.take_hits() == [{"stop": 1, "expr": "x > 0"}]


def test_a_stop_that_cannot_be_read_is_false_and_never_breaks_the_run():
    """Review focus: a misspelt name, no entity in hand, a division by zero."""
    s = SimSession(_line(), seed=0)
    stops = Stops(s, [{"expr": 'NQ("Servr") > 3'}, {"expr": "entity.cost > 0"},
                      {"expr": "1 / 0"}])
    assert s.advance(until=stops) == "drained" and stops.take_hits() == []
    assert watch_values(s, ['NQ("Servr") > 3']) == {
        'NQ("Servr") > 3': "error: unknown resource 'Servr'"}


def test_every_expression_stop_is_read_before_the_event_so_none_is_late_or_twice():
    """No short circuit: two stops that flip before the same event both hit at once."""
    s = SimSession(_line(variables=[{"name": "x", "initial": 0}]), seed=0)
    stops = Stops(s, [{"expr": "x > 0"}, {"expr": "x >= 1"}])
    assert s.advance(to=2.5, until=stops) == "time" and stops.take_hits() == []
    s.set_variable("x", 1)
    assert s.advance(to=9, until=stops) == "until" and s.now == 2.5
    assert stops.take_hits() == [{"stop": 0, "expr": "x > 0"}, {"stop": 1, "expr": "x >= 1"}]
    assert s.advance(until=stops) == "drained" and stops.take_hits() == []     # still true: no more


def test_a_failed_or_nan_read_is_false_for_the_whole_run():
    """A stop that reads 0 and then fails, or goes NaN, never fires; one that goes from
    failing to true does, so a failure did not count as true."""
    nan = "x * 1e308 * 10 - x * 1e308 * 10"        # 0 while x is 0, inf - inf once x is 1
    s = SimSession(_line(variables=[{"name": "x", "initial": 0}]), seed=0)
    stops = Stops(s, [{"expr": nan}, {"expr": "x / x"}])    # "x / x" divides by zero while x is 0
    assert s.advance(to=2.5, until=stops) == "time" and stops.take_hits() == []
    s.set_variable("x", 1)
    assert s.advance(until=stops) == "until"
    assert stops.take_hits() == [{"stop": 1, "expr": "x / x"}]
    assert s.advance(until=stops) == "drained" and stops.take_hits() == []


@pytest.mark.parametrize("stops", [
    [{"block": "nope"}], [{"blok": "delay_1"}], [{"block": "delay_1", "expr": "1"}], ["delay_1"],
    [{"block": 3}], [{"expr": "RAND() > 0.5"}], [{"expr": "1 +"}], [{"expr": "__import__('os')"}],
    [{"expr": ["x > 0"]}], [{"expr": 3}], [{"expr": None}],
    # D597: a narrowed stop
    [{"entity": True}], [{"entity": "1"}], [{"when": "TNOW > 0"}],
    [{"block": "delay_1", "when": "RAND() > 0"}], [{"block": "delay_1", "when": 3}],
    [{"expr": "TNOW > 0", "when": "TNOW > 1"}], [{"entity": 1, "expr": "1"}]])
def test_a_bad_stop_is_refused_and_leaves_the_bus_alone(stops):
    s = SimSession(_line(), seed=0)
    # The "stop {index}: " prefix is relied on: the game worker cuts the message at ": ".
    with pytest.raises(ValueError, match=r"^stop 1: "):     # ExpressionError is one
        Stops(s, [{"block": "delay_1"}] + stops)
    assert not s.ctx.bus.has_subscribers


def _hits_to_the_end(s, stops):
    hits = []
    while s.advance(until=stops) == "until":
        hits += stops.take_hits()
    stops.close()
    return hits


def test_a_when_condition_reads_the_entering_entity():
    s = SimSession(_line(), seed=0)
    hits = _hits_to_the_end(s, Stops(s, [{"block": "delay_1", "when": 'entity.name == "Entity 3"'},
                                         {"block": "delay_1", "when": "nope > 1"}]))
    assert hits == [{"stop": 0, "block": "delay_1", "entity_id": 3, "entity_type": "DEFAULT"}]


def test_623_the_documented_when_example_fires_and_a_bare_type_never_does():
    """DEVLOG #623: the Stops docstring showed `type == "red"`, which never holds."""
    s = SimSession(_line(), seed=0)
    hits = _hits_to_the_end(s, Stops(s, [
        {"block": "delay_1", "when": 'entity.e_type == "DEFAULT"'},
        {"block": "delay_1", "when": 'type == "DEFAULT"'}]))
    assert {h["stop"] for h in hits} == {0} and len(hits) == 6
    assert 'entity.e_type == "Red"' in Stops.__doc__


def test_an_entity_stop_follows_one_entity_through_every_block():
    s = SimSession(_line(), seed=0)
    hits = _hits_to_the_end(s, Stops(s, [{"entity": 2}, {"block": "dispose_1", "entity": 4}]))
    assert [(h["stop"], h["block"], h["entity_id"]) for h in hits] == [
        (0, "create_1", 2), (0, "delay_1", 2), (0, "dispose_1", 2), (1, "dispose_1", 4)]


@pytest.mark.parametrize("stops", [None, []])
def test_no_stops_is_a_predicate_that_never_stops(stops):
    s = SimSession(_line(), seed=0)
    nothing = Stops(s, stops)
    assert not s.ctx.bus.has_subscribers
    assert s.advance(until=nothing) == "drained" and nothing.take_hits() == []


def test_stops_subscribe_only_for_a_block_stop_and_close_unsubscribes():
    s = SimSession(_line(), seed=0)
    only_expr = Stops(s, [{"expr": "TNOW > 1"}])
    assert not s.ctx.bus.has_subscribers
    only_expr.close()
    stops = Stops(s, [{"block": "delay_1"}])
    assert s.ctx.bus.has_subscribers
    stops.close()
    stops.close()                                   # twice is safe
    assert not s.ctx.bus.has_subscribers


# -- readers -----------------------------------------------------------------------
def test_readonly_expression_refuses_every_sampler():
    assert readonly_expression('NQ("Server") + 1').text == 'NQ("Server") + 1'
    for text in ("RAND()", "EXPO(1)", "UNIF(0, 1)", "NORM(0, 1)", "TRIA(0, 1, 2)",
                 "1 + DISC(1, 1)"):
        with pytest.raises(ExpressionError, match="draws a random number"):
            readonly_expression(text)


def test_watch_values_are_scalars_or_an_error_text():
    s = SimSession(_line(variables=[{"name": "x", "initial": 2},
                                    {"name": "tag", "initial": "hot"}]), seed=0)
    s.advance(to=1.5)
    assert watch_values(s, ["1 +"])["1 +"].startswith("error: syntax error")
    assert watch_values(s, ["TNOW", "x * 2", "x > 1", "tag", "nope", "RAND()", "entity.cost",
                            "(1, 2)"]) == {
        "TNOW": 1.5, "x * 2": 4, "x > 1": True, "tag": "hot",
        "nope": "error: unknown name 'nope'",
        "RAND()": "error: RAND() draws a random number; a watch or a stop only reads",
        "entity.cost": "error: no entity is available in this context (col 0)",
        "(1, 2)": "error: tuple is not a number, a truth value or a text"}


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_a_read_that_runs_out_of_stack_or_memory_is_reported_not_raised(monkeypatch, error):
    """Such a read is an error text for a watch and a false reading for a stop."""
    from nagasim.core import expressions
    s = SimSession(_line(variables=[{"name": "x", "initial": 2}]), seed=0)
    stops = Stops(s, [{"expr": "x > 1"}])

    def exhausted(self, scope):
        raise error()               # the real ones carry no text
    monkeypatch.setattr(expressions.Expression, "evaluate", exhausted)
    assert watch_values(s, ["x * 2"]) == {"x * 2": f"error: {error.__name__}"}
    assert stops() is False and stops.take_hits() == []
    stops.close()


def test_a_watch_and_an_entity_view_are_bounded_and_json_ready():
    """Watch texts come from a model, a person or an LLM: a huge value must not escape."""
    s = SimSession(_line(), seed=0)
    s.advance(to=1.5)
    texts = ["(2 ** 1024) ** 15", "'a' * 10000 * 10000", "2 ** 1023", "1e308 * 10"]
    values = watch_values(s, texts)
    assert values["(2 ** 1024) ** 15"] == "error: number too large to show"   # repr would raise
    assert values["'a' * 10000 * 10000"] == "a" * 200 + "...[cut, 100000000 characters]"
    assert values["2 ** 1023"] == 2 ** 1023                 # 1024 bits is still shown
    assert values["1e308 * 10"] == float("inf")             # floats pass as they are
    assert len(json.dumps(values)) < 1000
    entity = s.ctx.live_entities._items[2]
    entity.big, entity.long = 2 ** 5000, "y" * 1000
    view = entity_view(s, 2)
    assert view["attributes"]["big"] == "error: number too large to show"
    assert view["attributes"]["long"] == "y" * 200 + "...[cut, 1000 characters]"
    assert len(json.dumps(view)) < 1000
    entity.boxed = [2 ** 20000]             # a container whose repr raises
    assert entity_view(s, 2)["attributes"]["boxed"] == "error: list cannot be shown"


DEEP = {"plus": "1+" * 3000 + "1", "not": "not " * 2000 + "1", "minus": "-" * 5000 + "1",
        "parser": "-" * 7000 + "1"}       # the parser's own stack: a MemoryError


@pytest.mark.parametrize("name", DEEP)
def test_a_deeply_nested_expression_is_refused_like_any_other(name):
    """(#332) Nested past the parser's stack, a watch or a stop text is an error, not a crash."""
    text = DEEP[name]
    s = SimSession(_line(), seed=0)
    with pytest.raises(ExpressionError, match="nested too deeply"):
        readonly_expression(text)
    assert watch_values(s, [text]) == {text: "error: expression is nested too deeply"}
    with pytest.raises(ValueError, match=r"^stop 0: "):
        Stops(s, [{"expr": text}])


def test_a_watch_and_a_stop_peek_a_level_without_integrating_it():
    c, d = create("c", max_arrivals=1), dispose("d")
    doc = layout([c, d], chain(c, d), levels=[{"name": "Tank", "capacity": 1000, "initial": 100}],
                 flows=[{"name": "in", "to": "Tank", "rate": "10.0"}],
                 simulation={"time_limit": 5, "warmup": 0, "replications": 1})
    s = SimSession(doc, seed=0)
    s.advance(to=1.3)
    before = s.ctx.level_system._last_t
    stops = Stops(s, [{"expr": 'LEVEL("Tank") > 120'}])
    assert watch_values(s, ['LEVEL("Tank")'])['LEVEL("Tank")'] == pytest.approx(113.0)
    assert stops() is False
    assert s.ctx.level_system._last_t == before < 1.3 and s.ctx.level_system.peeking is False


def test_entity_view_shows_public_attributes_and_none_once_the_entity_is_gone():
    """Review focus: the entity being inspected is disposed."""
    s = SimSession(_line(), seed=0)
    s.advance(to=1.5)
    first, second = (s.ctx.live_entities._items[i] for i in (1, 2))
    first.colour, first.partner, first.note, first._hidden = "red", second, "x" * 200, 1
    view = entity_view(s, 1)
    assert (view["id"], view["type"], view["created"]) == (1, "DEFAULT", 0.0)
    attributes = view["attributes"]
    assert attributes["colour"] == "red" and attributes["partner"] == 2
    assert attributes["cost"] == 0.0
    assert len(attributes["note"]) == 200 and "_hidden" not in attributes
    first.note = ["x" * 200]
    assert entity_view(s, 1)["attributes"]["note"].endswith("...")
    assert len(entity_view(s, 1)["attributes"]["note"]) == 80
    s.advance(to=3)                                  # entity 1 left at 2.5
    assert entity_view(s, 1) is None and entity_view(s, 99) is None
    assert entity_view(s, "1") is None


@pytest.mark.parametrize("entity_id", [[1], True, 1.0, "1", None, {"id": 1}])
def test_entity_view_answers_only_to_an_int_id(entity_id):
    s = SimSession(_line(), seed=0)
    s.advance(to=1.5)
    assert entity_view(s, 1)["id"] == 1             # it is there, and 1 == True == 1.0
    assert entity_view(s, entity_id) is None


# -- the exit check --------------------------------------------------------------------
def _expressions(session) -> list[str]:
    """Two stops that flip during any run: the clock, and the first piece of model state."""
    ctx = session.ctx
    if ctx.resource_manager.resources:
        state = f'NQ("{next(iter(ctx.resource_manager.resources))}") > 0'
    elif ctx.level_system is not None and ctx.level_system.levels:
        name, level = next(iter(ctx.level_system.levels.items()))
        state = f'LEVEL("{name}") > {level.level!r}'
    elif ctx.agent_system is not None:
        population = ctx.agent_system.populations[0]
        state = f'COUNT("{population.name}", "{population.type.states[0]}") > 0'
    else:
        state = "TNOW > 2"
    return [f"TNOW > {session.horizon / 3!r}", state]


def _debugged(doc, seed, traced, rounds=120):
    """One run under a fixed mix of every debugger call; returns its results."""
    trace = TraceRecorder() if traced else None
    s = SimSession(doc, seed, trace=trace)
    exprs = _expressions(s)
    names = set(s.model.blocks) | {name.split("/")[0] for name in s.model.blocks}
    # D597: a condition read inside an event, and one entity followed, change nothing either.
    narrowed = [{"block": sorted(names)[0], "when": "TNOW >= 0 and entity.cost >= 0"},
                {"entity": 1}] if names else []
    stops = Stops(s, [{"block": name} for name in sorted(names)]
                  + [{"expr": text} for text in exprs] + narrowed)
    # The odd chunks (a quarter of the rounds, 2 units on average) add up to 1.5 times the
    # run's own length (a model may drain long before its horizon): the mix of calls spans
    # the whole run, not its first quarter.
    plain = SimSession(doc, seed)
    plain.finish()
    unit = plain.now * 3 / rounds
    for i in range(rounds):
        kind = i % 4
        if kind == 0:               # one step: the next trace event, or the next event
            seen, n = len(trace.events) if traced else 0, s._steps
            state = s.advance(until=(lambda: len(trace.events) > seen) if traced
                              else (lambda: s._steps > n))
        elif kind == 1 and names:   # to the next stop (a model with blocks has one soon)
            state = s.advance(until=stops)
        elif kind == 2:             # an odd chunk of time
            state = s.advance(to=s.now + unit * (1 + i % 3))
        else:                       # a time or a stop, whichever comes first
            state = s.advance(to=s.now + unit * 0.75, until=stops)
        hits = stops.take_hits()
        values = watch_values(s, exprs + ["TNOW", "RAND()", "nope"])
        assert values["TNOW"] == s.now and values["RAND()"].startswith("error")
        # RAND() and "nope" are meant to fail; a state watch that did too would pass unseen.
        assert not [t for t in exprs + ["TNOW"] if str(values[t]).startswith("error")], values
        for hit in hits[:3]:
            if "entity_id" in hit:
                entity_view(s, hit["entity_id"])
        s.snapshot()
        if state in ("horizon", "drained"):
            break
    assert i >= 8, "the mix never got going"
    stops.close()
    return s.finish()


@pytest.mark.parametrize("traced", [True, False])
def test_a_debugged_run_gives_the_golden_hash(traced):
    for model in GOLDEN_MODELS:
        for seed in SEEDS:
            record = json.loads((GOLDEN_DIR / f"{Path(model).stem}_seed{seed}.json").read_text())
            results = _debugged(_load(model), seed, traced)
            assert golden_hash(payload_of(results)) == record["hash"], (model, seed)


@pytest.mark.parametrize("model", ["energy_pv_battery.json", "sd_sir.json",
                                   "agents_machine_repair.json", "two_cell_line.json"])
def test_a_debugged_run_equals_an_undebugged_one_frame_for_frame(model):
    doc = _load(model)
    expected = SimSession(doc, seed=0).finish()
    for traced in (True, False):
        _frames_equal(_debugged(doc, 0, traced), expected)


# -- a pending decision ---------------------------------------------------------------
ANSWERS = 40


def _answer(session, count):
    """Answer everything pending by a fixed rule; returns the new count."""
    for request in session.pending():
        session.decide(request["policy"], count % len(request["options"]), request["id"])
        count += 1
    return count


def _played_plainly(doc):
    s, count = SimSession(doc, seed=0), 0
    while count < ANSWERS and s.advance() == "decision":
        count = _answer(s, count)
    return s, s.finish()


def _played_debugged(doc):
    trace = TraceRecorder()
    s, count = SimSession(doc, seed=0, trace=trace), 0
    exprs = _expressions(s)
    stops = Stops(s, [{"block": name} for name in s.model.blocks]
                  + [{"expr": text} for text in exprs])
    i = 0
    while count < ANSWERS:
        i += 1
        if i % 3 == 0:
            state = s.advance(until=stops)
        elif i % 3 == 1:
            state = s.advance(to=s.now + 0.31, until=stops)
        else:
            seen = len(trace.events)
            state = s.advance(until=lambda: len(trace.events) > seen)
        stops.take_hits()
        watch_values(s, exprs)
        if state in ("horizon", "drained"):
            # `advance(to=)` stops before the events at the horizon itself; a plain
            # advance takes them, and one of them may raise the last decision.
            state = s.advance()
            if state != "decision":
                break
        if state != "decision":
            continue
        # While it waits: every call answers "decision" and nothing moves.
        waiting = (s._steps, s.now, len(trace.events))
        for call in (lambda: s.advance(until=stops), lambda: s.advance(to=s.now + 5, until=stops),
                     lambda: s.advance(until=lambda: True), lambda: s.advance(to=s.now + 5)):
            assert call() == "decision"
        watch_values(s, exprs)
        entity_view(s, s.pending()[0]["entity_id"])
        s.snapshot()
        assert (s._steps, s.now, len(trace.events)) == waiting
        count = _answer(s, count)
    stops.close()
    return s, s.finish()


@pytest.mark.parametrize("model", ["ed_game.json", "parallel_routing_policy.json"])
def test_stops_steps_and_reads_around_decisions_leave_the_game_as_it_was(model):
    doc = _load(model)
    plain, expected = _played_plainly(doc)
    debugged, got = _played_debugged(doc)
    assert len(plain.game_log) >= 10
    assert debugged.game_log == plain.game_log          # entry for entry, "step" included
    _frames_equal(got, expected)
    _frames_equal(SimSession.replay(doc, 0, plain.game_log), expected)


# -- step back (D598): a session resumed at an earlier point is that run, exactly ------
def _state(s, trace):
    return (s._steps, s.now, json.dumps(s.snapshot(), sort_keys=True, default=str),
            list(trace.events))


def _drive(doc, seed, rounds=40):
    """A traced run moved by steps and odd chunks; [(position, state)] at every pause."""
    trace = TraceRecorder()
    s = SimSession(doc, seed, trace=trace)
    plain = SimSession(doc, seed)
    plain.finish()
    unit = plain.now * 2 / rounds
    seen = []
    for i in range(rounds):
        if i % 2:
            state = s.advance(to=s.now + unit * (0.5 + i % 3))
        else:
            n = len(trace.events)
            state = s.advance(until=lambda: len(trace.events) > n)
        seen.append(((s._steps, s.now, len(s.game_log)), _state(s, trace)))
        if state in ("horizon", "drained"):
            break
    return s, seen


def test_a_resumed_session_is_the_run_at_that_point_and_ends_with_the_golden_hash():
    for model in GOLDEN_MODELS:
        seed = SEEDS[0]
        record = json.loads((GOLDEN_DIR / f"{Path(model).stem}_seed{seed}.json").read_text())
        doc = _load(model)
        run, seen = _drive(doc, seed)
        for (step, t, n), expected in (seen[len(seen) // 3], seen[2 * len(seen) // 3]):
            trace = TraceRecorder()
            back = SimSession.resumed(doc, seed, run.game_log[:n], step, t, trace=trace)
            assert _state(back, trace) == expected, (model, step)
            assert golden_hash(payload_of(back.finish())) == record["hash"], (model, step)


def test_a_resumed_game_keeps_its_answers_and_ends_as_the_plain_game():
    doc = _load("ed_game.json")
    plain, expected = _played_plainly(doc)
    s, count, marks = SimSession(doc, seed=0), 0, []
    while count < ANSWERS and s.advance() == "decision":
        marks.append((s._steps, s.now, len(s.game_log), count))
        count = _answer(s, count)
    step, t, n, count = marks[len(marks) // 2]
    back = SimSession.resumed(doc, 0, s.game_log[:n], step, t)
    assert back.pending() and back.game_log == s.game_log[:n]
    while count < ANSWERS:
        count = _answer(back, count)
        if back.advance() != "decision":
            break
    assert back.game_log == plain.game_log
    _frames_equal(back.finish(), expected)


# -- what a watch field offers (composing a watch, D360) ---------------------------
def test_watch_functions_are_the_registry_minus_what_a_watch_cannot_read():
    hints = watch_functions()
    assert set(hints) <= set(FUNCTIONS) and not set(hints) & set(functions_with("sampler"))
    assert not {"NEAR", "SMOOTH", "INITIAL"} & set(hints)
    assert hints["NQ"] == 'NQ("name")' and hints["PI"] == "PI()"
    assert hints["TS"] == 'TS("name"[, b])' and hints["min"] == "min(a, ...)"
    assert hints["IF"] == "IF(a, b, c)"
    for hint in hints.values():         # the hint, less its optional part, is a valid watch
        call = hint.split("[")[0].replace(", ...", "")
        readonly_expression(call if call.endswith(")") else call + ")")


def test_watch_names_follow_the_function_and_are_the_flattened_names():
    doc = _load("two_cell_line.json")
    assert ("cell_1/Machine", "resources") in watch_names(doc, "NQ")
    assert {section for _, section in watch_names(doc, "NQ")} == {"resources"}
    doc = _line(resources=[{"name": "Server", "capacity": 1}],
                resource_sets=[{"name": "Crew", "members": ["Server"]}],
                variables=[{"name": "x", "initial": 0}], levels="not a list")
    assert watch_names(doc, "NQ") == [("Server", "resources"), ("Crew", "resource_sets")]
    assert watch_names(doc, "VAR") == [("x", "variables")] and watch_names(doc, "LEVEL") == []
    assert ("delay_1", "blocks") in watch_names(doc, "NS")
    everything = watch_names(doc)       # a function the registry says nothing about: all
    assert everything == watch_names(doc, "DISTANCE") == watch_names(doc, "max")
    assert {"Server", "Crew", "x", "delay_1"} <= {name for name, _ in everything}
