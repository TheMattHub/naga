"""Stella conveyors (PHASE6 depth C1): the mechanics around the worked examples
the independent oracle (test_conveyor_oracle.py) checks -- validation, a transit
latched per batch, arrest, several inflows, conveyor-to-conveyor chaining, the
initial spread, the authoring records and a game-mode snapshot. Every number
is hand-derived; DT = 0.5 throughout, so an inflow of 100/time is 50 per DT.
"""
import pytest

from conftest import chain, create, dispose, layout

from nagasim.core import sd
from nagasim.core.schema import validate_model_structured
from nagasim.core.session import SimSession
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until


def _doc(levels, flows, T=4.0, **sd_spec):
    return layout([], [], levels=levels, flows=flows,
                  simulation={"time_limit": T, "warmup": 0, "replications": 1,
                              "sd": {"dt": 0.5, **sd_spec}})


def _trace(doc, times, *names):
    """{t: (value, ...)} for each level/flow name, read right after grid step t."""
    env = initialize_sim_env(seed=0)
    prepare_model(env, doc)
    system = env.ctx.level_system
    system.start(doc["simulation"]["time_limit"])
    out = {}
    for t in times:
        step_until(env, t or 1e-12)        # 0 would mean "no limit"
        system.sync(t)
        out[t] = tuple(system.levels[n].level if n in system.levels
                       else system.flow_index[n].actual for n in names)
    return out


def _belt(**conveyor):
    return {"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": 2, **conveyor}}


OUT = {"name": "out", "rate": "", "from": "Belt"}


def test_validation_codes():
    codes = lambda doc: {p.code for p in validate_model_structured(doc)}
    assert "E_CONVEYOR_SPEC" in codes(_doc([{"name": "Belt", "conveyor": {}}], [OUT]))
    assert "E_CONVEYOR_OUTFLOW" in codes(_doc([_belt()], [{**OUT, "rate": "5"}]))
    bad_zone = {"name": "lk", "rate": "", "from": "Belt",
                "leak": {"fraction": 0.1, "zone": [0.6, 0.4]}}
    assert "E_CONVEYOR_LEAK" in codes(_doc([_belt()], [OUT, bad_zone]))
    clean = codes(_doc([_belt(capacity="10 * 2", leak_kind="exponential")],
                       [OUT, {**bad_zone, "leak": {"fraction": "0.1"}}]))
    assert not {c for c in clean if "CONVEYOR" in c}


def test_transit_is_latched_per_batch():
    """T = 2 (4 slats) before t=1, then 1 (2 slats). The batches admitted at
    0 and 0.5 keep their 4 slats, so the batch of 0 and the one of 1.0 both
    exit at 2.0 (100 -> rate 200): the shorter transit did not shorten what
    was already on the belt."""
    doc = _doc([_belt(transit_time="2 - STEP(1, 1)")],
               [{"name": "in", "rate": "100", "to": "Belt"}, OUT])
    got = _trace(doc, [1.5, 2.0, 2.5, 3.0, 3.5], "Belt", "out")
    assert got == {1.5: (150.0, 0.0), 2.0: (200.0, 200.0), 2.5: (150.0, 200.0),
                   3.0: (100.0, 100.0), 3.5: (100.0, 100.0)}


def test_arrest_freezes_the_belt_and_its_flows():
    """T = 1 (2 slats), arrested over [1, 2): nothing enters (Source keeps
    its stock), nothing moves or exits; at 2.0 the first batch leaves."""
    doc = _doc([{"name": "Source", "initial": 1000.0},
                _belt(transit_time=1, arrest="STEP(1, 1) - STEP(1, 2)")],
               [{"name": "in", "rate": "100", "from": "Source", "to": "Belt"}, OUT])
    got = _trace(doc, [1.0, 1.5, 2.0, 2.5], "Belt", "Source", "in", "out")
    assert got == {1.0: (100.0, 900.0, 0.0, 0.0), 1.5: (100.0, 900.0, 0.0, 0.0),
                   2.0: (100.0, 900.0, 100.0, 100.0), 2.5: (100.0, 850.0, 100.0, 100.0)}


def test_several_inflows_take_the_room_in_listed_order():
    """capacity 60: "a" takes its 50, "b" the 10 left (rate 20); then full.
    in_limit 80/time (40 per DT) instead: "a" gets 40, "b" nothing."""
    flows = [{"name": "a", "rate": "100", "to": "Belt"},
             {"name": "b", "rate": "100", "to": "Belt"}, OUT]
    got = _trace(_doc([_belt(capacity=60)], flows), [0.0, 0.5], "a", "b", "Belt")
    assert got == {0.0: (100.0, 20.0, 0.0), 0.5: (0.0, 0.0, 60.0)}
    got = _trace(_doc([_belt(in_limit="80")], flows), [0.0], "a", "b")
    assert got == {0.0: (80.0, 0.0)}


def test_chained_conveyor_is_not_capacity_limited_at_entry():
    """A (T=1) pours 50 per DT into B (T=1, capacity 10) from t=1: B holds
    50 > 10, and B's own equation-driven inflow "c" is refused meanwhile (it
    got the 10 of room at t=0 only)."""
    doc = _doc([{"name": "A", "conveyor": {"transit_time": 1}},
                {"name": "B", "conveyor": {"transit_time": 1, "capacity": 10}}],
               [{"name": "in", "rate": "100", "to": "A"},
                {"name": "A_out", "rate": "", "from": "A", "to": "B"},
                {"name": "c", "rate": "100", "to": "B"},
                {"name": "B_out", "rate": "", "from": "B"}])
    got = _trace(doc, [0.0, 1.0, 1.5, 2.0], "B", "A_out", "c", "B_out")
    assert got == {0.0: (0.0, 0.0, 20.0, 0.0), 1.0: (10.0, 100.0, 0.0, 20.0),
                   1.5: (50.0, 100.0, 0.0, 0.0), 2.0: (100.0, 100.0, 0.0, 100.0)}


@pytest.mark.parametrize("kind", ["linear", "exponential"])
def test_initial_value_is_spread_as_the_leak_steady_state(kind):
    """V = 120, leak 0.5 over T = 2 (4 slats). Linear: slats in the ratio
    1 : .875 : .75 : .625 (sum 3.25), so the oldest (.625 s, s = 120/3.25)
    loses .125 s and exits .5 s -> rate s. Exponential: sums to V too."""
    doc = _doc([{**_belt(leak_kind=kind), "initial": 120.0}],
               [OUT, {"name": "lk", "rate": "", "from": "Belt", "leak": {"fraction": 0.5}}])
    belt, out = _trace(doc, [0.0], "Belt", "out")[0.0]
    assert belt == pytest.approx(120.0)
    if kind == "linear":
        assert out == pytest.approx(120.0 / 3.25)


def test_authoring_records_carry_conveyor_and_leak():
    doc = sd.add({"blocks": [], "links": [], "simulation": {"time_limit": 3}},
                 sd.SDSettings(0.5), sd.Stock("Belt", conveyor={"transit_time": 2}),
                 sd.Flow("in", "100", target="Belt"), sd.Flow("out", "", source="Belt"),
                 sd.Flow("lk", "", source="Belt", leak={"fraction": 0.5}))
    assert doc["levels"][0]["conveyor"] == {"transit_time": 2}
    assert sd.Flow.from_dict(doc["flows"][2]).leak == {"fraction": 0.5}
    assert _trace(doc, [2.0], "Belt", "lk")[2.0] == (162.5, 50.0)      # example B


def test_snapshot_mid_step_reads_the_last_grid_value_and_changes_nothing():
    """At t=1.3 the belt shows its t=1.0 value (piecewise constant) and a
    snapshot leaves every frame identical to an unobserved run."""
    c, d = create("c", max_arrivals=1), dispose("d")
    doc = layout([c, d], chain(c, d), levels=[_belt()],
                 flows=[{"name": "in", "rate": "100", "to": "Belt"}, OUT,
                        {"name": "lk", "rate": "", "from": "Belt", "leak": {"fraction": 0.5}}],
                 dashboard={"widgets": [{"name": "belt", "kind": "kpi", "expr": 'LEVEL("Belt")'}]},
                 simulation={"time_limit": 5, "warmup": 0, "replications": 1,
                             "sd": {"dt": 0.5}})
    watched = SimSession(doc, seed=0)
    watched.advance(to=1.3)
    assert watched.snapshot()["widgets"]["belt"] == pytest.approx(93.75)
    played, plain = watched.finish(), SimSession(doc, seed=0).finish()
    assert set(played["flow_summary"]["Flow"]) >= {"out", "lk"}
    for frame in ("level_summary", "flow_summary"):
        assert played[frame].equals(plain[frame]), frame


# =============================================================== C2: refusing writes
# A conveyor's own value is the sum of its cohorts (`_step_conveyors` recomputes
# it every grid step), so anything that pokes `level.level` directly -- a
# TRANSFER, an agent transfer action, initial_state, or a per-element copy a
# subscripted conveyor would need -- would be silently overwritten at the next
# step (the C1 gap this milestone's DEVLOG row calls out). Each is refused
# with E_CONVEYOR_WRITE, one per site; the bug protocol does not apply (these
# are feature gaps, not regressions), so these stay lean, one test each.

def test_transfer_block_on_a_conveyor_is_refused():
    c, d = create("c", max_arrivals=1), dispose("d")
    draw = {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 0],
            "params": {"level": "Belt", "amount": "1"}}
    doc = layout([c, draw, d], chain(c, draw, d), levels=[_belt()], flows=[OUT],
                 simulation={"time_limit": 4.0, "warmup": 0, "replications": 1,
                             "sd": {"dt": 0.5}})
    assert "E_CONVEYOR_WRITE" in {p.code for p in validate_model_structured(doc)}


def test_agent_transfer_action_on_a_conveyor_is_refused():
    from nagasim.core import agents as A
    kind = A.AgentType("Taker", states=["A", "B"], transitions=[
        A.Transition("A", "B", "timeout", "1", actions=[A.Action.transfer("Belt", 1)])])
    doc = A.add(_doc([_belt()], [OUT]), kind, A.Population("T", "Taker", 1))
    assert "E_CONVEYOR_WRITE" in {p.code for p in validate_model_structured(doc)}


def test_initial_state_on_a_conveyor_is_refused():
    doc = _doc([_belt()], [OUT])
    doc["initial_state"] = {"levels": {"Belt": 5.0}}
    assert "E_CONVEYOR_WRITE" in {p.code for p in validate_model_structured(doc)}


def test_subscripted_conveyor_is_refused():
    doc = _doc([{**_belt(), "dims": ["Region"]}], [OUT])
    doc["subscripts"] = [{"name": "Region", "elements": ["north", "south"]}]
    assert "E_CONVEYOR_WRITE" in {p.code for p in validate_model_structured(doc)}


def test_move_on_a_conveyor_raises_at_run_time():
    """The runtime choke point TRANSFER and a transfer action both call
    (`LevelSystem.move`), exercised directly: a hand-built layout that skips
    validation must still be refused, not silently overwritten."""
    doc = _doc([_belt()], [OUT])
    env = initialize_sim_env(seed=0)
    prepare_model(env, doc)
    env.ctx.level_system.start(doc["simulation"]["time_limit"])
    with pytest.raises(ValueError, match="conveyor"):
        env.ctx.level_system.move("Belt", 1.0, "remove")
