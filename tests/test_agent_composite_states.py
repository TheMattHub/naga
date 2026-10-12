"""PHASE6 K1 (D136-D139): composite and history states in `core/agents.py`
-- validation, flat invariance, exit/entry order, a composite timeout that
survives inner moves, history, innermost priority, COUNT on a composite,
composite statistics and the v26 -> v27 migration."""
import copy

import numpy as np
import pytest

from nagasim.core.schema import CURRENT_VERSION, migrate, validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

# Entry/exit codes appended to self.log: log = log * 10 + code.
_LOG = "self.log * 10 + {}"


def _act(code):
    return [{"target": "self.log", "expr": _LOG.format(code)}]


def _machine(transitions, states=None, count=1, time_limit=100, initial=None, **pop):
    states = states or [
        {"name": "Working", "entry": _act(1), "exit": _act(2)},
        {"name": "Idle", "parent": "Working", "entry": _act(3), "exit": _act(4)},
        {"name": "Busy", "parent": "Working", "entry": _act(5), "exit": _act(6)},
        {"name": "Broken", "entry": _act(7), "exit": _act(8)}]
    kind = {"name": "Machine", "attributes": [{"name": "log", "init": "0"},
                                              {"name": "go", "init": "0"}],
            "states": states, "transitions": transitions}
    population = {"name": "M", "type": "Machine", "count": count, **pop}
    if initial:
        population["initial"] = initial
    return {"blocks": [], "links": [], "simulation": {"time_limit": time_limit},
            "agent_types": [kind], "populations": [population]}


def _start(layout, seed=1):
    """Deliberately NOT `conftest.run_env`: 7 call sites use `_start` on its
    own (not through `_run`) to sample mid-run state and keep stepping
    afterwards -- `run_env` always steps to completion in one call."""
    run_len = layout["simulation"]["time_limit"]
    env = initialize_sim_env(seed)
    env.ctx.sim_horizon = run_len
    prepare_model(env, layout)
    env.ctx.agent_system.start(run_len)
    return env


def _run(layout, until, seed=1):
    env = _start(layout, seed)
    step_until(env, until)
    return env.ctx.agent_system


def _codes(layout):
    """Deliberately NOT `conftest.problem_codes`: this file's assertions need
    each problem's `.path` alongside its code, not just the code."""
    return sorted((p.code, p.path) for p in validate_model_structured(layout)
                  if p.severity == "error")


def _state(system, i=0):
    pop = system.by_name["M"]
    return pop.type.states[int(pop.state[i])]


def test_validation_codes():
    ok = _machine([{"from": "Broken", "to": "Working", "timeout": "1", "history": "deep"}])
    assert _codes(ok) == []
    bad = _machine([{"from": "Broken", "to": "Idle", "timeout": "1", "history": "deep"},
                    {"from": "Broken", "to": "Working", "timeout": "1", "history": "last"}],
                   states=[{"name": "Working", "initial": "Broken"},
                           {"name": "Idle", "parent": "Working", "initial": "Busy"},
                           {"name": "Busy", "parent": "Nowhere"},
                           {"name": "Broken"},
                           {"name": "X", "parent": "Y"}, {"name": "Y", "parent": "X"}])
    at = "/agent_types/0"
    assert _codes(bad) == sorted([
        ("E_AGENT_STATE", f"{at}/states/0/initial"),        # names a non-child
        ("E_AGENT_STATE", f"{at}/states/1/initial"),        # initial on a leaf
        ("E_AGENT_STATE", f"{at}/states/2/parent"),         # unknown parent
        ("E_AGENT_STATE", f"{at}/states/4/parent"),         # cycle X <-> Y
        ("E_AGENT_STATE", f"{at}/states/5/parent"),
        ("E_AGENT_TRANSITION", f"{at}/transitions/0/history"),     # leaf target
        ("E_AGENT_TRANSITION", f"{at}/transitions/1/history")])    # not shallow/deep


def test_exit_entry_order_sibling_move_exit_and_deep_reentry():
    """Idle -> Busy at 1 (siblings: Working untouched), Working -> Broken at 2
    (the composite's own timeout, not restarted by the inner move), deep
    history back into Busy at 3."""
    layout = _machine([{"from": "Idle", "to": "Busy", "timeout": "1", "actions": _act(9)},
                       {"from": "Working", "to": "Broken", "timeout": "2", "actions": _act(9)},
                       {"from": "Broken", "to": "Working", "timeout": "1", "history": "deep",
                        "actions": _act(9)}])
    env = _start(layout)
    pop = env.ctx.agent_system.by_name["M"]
    assert pop.attrs["log"][0] == 13
    step_until(env, 1.5)
    assert pop.attrs["log"][0] == 13495
    step_until(env, 2.5)
    assert pop.attrs["log"][0] == 134956297 and _state(env.ctx.agent_system) == "Broken"
    step_until(env, 3.5)
    assert pop.attrs["log"][0] == 1349562978915 and _state(env.ctx.agent_system) == "Busy"
    rows = {r["State"]: r for r in pop.summary(3.5)}
    # Composite statistics count boundary crossings only; Avg Count adds up.
    assert (rows["Working"]["Entries"], rows["Idle"]["Entries"], rows["Busy"]["Entries"]) \
        == (1, 0, 2)
    assert rows["Working"]["Avg Sojourn"] == 2.0 and rows["Busy"]["Avg Sojourn"] == 1.0
    assert rows["Working"]["Avg Count"] == pytest.approx(
        rows["Idle"]["Avg Count"] + rows["Busy"]["Avg Count"], abs=1e-12)
    assert rows["Working"]["Avg Count"] == pytest.approx(2.5 / 3.5, abs=1e-12)


def test_composite_timeout_survives_inner_cycling():
    layout = _machine([{"from": "Idle", "to": "Busy", "timeout": "3"},
                       {"from": "Busy", "to": "Idle", "timeout": "3"},
                       {"from": "Working", "to": "Broken", "timeout": "10"}])
    system = _run(layout, 20)
    pop = system.by_name["M"]
    assert _state(system) == "Broken" and pop.entered[0] == 10.0
    assert [pop.entries[pop.type.index[s]] for s in ("Idle", "Busy", "Broken")] == [1, 2, 1]


def test_composite_rate_is_drawn_once_across_inner_moves():
    layout = _machine([{"from": "Idle", "to": "Busy", "timeout": "1"},
                       {"from": "Busy", "to": "Idle", "timeout": "1"},
                       {"from": "Working", "to": "Broken", "rate": "1e-9"}])
    system = _run(layout, 5.5)
    assert _state(system) == "Busy" and system.by_name["M"].draws[0] == 1


def _history_layout(history):
    states = [{"name": "Top"}, {"name": "C", "parent": "Top"}, {"name": "Mid", "parent": "Top"},
              {"name": "A", "parent": "Mid"}, {"name": "B", "parent": "Mid"}, {"name": "Out"}]
    back = {"from": "Out", "to": "Top", "timeout": "1"}
    if history:
        back["history"] = history
    return _machine([{"from": "C", "to": "B", "timeout": "1"},
                     {"from": "Top", "to": "Out", "timeout": "1.5"}, back],
                    states=states, max_count=1)


@pytest.mark.parametrize("history, leaf", [(None, "C"), ("shallow", "A"), ("deep", "B")])
def test_history_none_shallow_deep_and_recycled_slot(history, leaf):
    env = _start(_history_layout(history))
    system = env.ctx.agent_system
    step_until(env, 2.6)
    assert _state(system) == leaf
    if history == "deep":
        # A recycled slot has no record: back to Top's initial, C.
        pop = system.by_name["M"]
        system.from_block(system.remove_agent, pop, 0)
        assert system.from_block(system.add_agent, pop, "Out") == 0
        step_until(env, 3.7)
        assert _state(system) == "C"


def test_innermost_condition_and_message_win():
    both = [{"from": "Working", "to": "Broken", "condition": "self.go == 1",
             "actions": _act(8)},
            {"from": "Idle", "to": "Busy", "condition": "self.go == 1", "actions": _act(9)}]
    layout = _machine(both)
    layout["agent_types"][0]["attributes"][1]["init"] = "1"
    for state in layout["agent_types"][0]["states"]:
        state.pop("entry"), state.pop("exit")
    system = _run(layout, 1)
    # The leaf's fires first; the parent's, still true, fires in the next pass.
    assert system.by_name["M"].attrs["log"][0] == 98 and _state(system) == "Broken"
    messages = [{"from": "Working", "to": "Broken", "message": "m"},
                {"from": "Idle", "to": "Busy", "message": "m"}]
    env = _start(_machine(messages))
    system = env.ctx.agent_system
    system.send("m", [(system.by_name["M"], 0)])
    assert _state(system) == "Busy"


def test_count_family_and_births_descend_into_composites():
    layout = _machine([{"from": "Working", "to": "Broken", "rate": "1e-9"}], count=10,
                      initial=[{"state": "Broken", "count": 3}, {"state": "Busy", "count": 2}],
                      max_count=11)
    layout["agent_types"][0]["attributes"][0]["init"] = "1"
    for state in layout["agent_types"][0]["states"]:
        state.pop("entry"), state.pop("exit")
    env = _start(layout)
    system = env.ctx.agent_system
    counts = {s: system.count("M", s) for s in ("Working", "Idle", "Busy", "Broken")}
    assert counts == {"Working": 7, "Idle": 5, "Busy": 2, "Broken": 3}
    assert system.aggregate("SUM", "M", "log", "Working") == 7.0
    i = system.from_block(system.add_agent, system.by_name["M"], "Working")
    assert _state(system, i) == "Idle" and system.count("M", "Working") == 8


def test_flat_population_unchanged_beside_a_nested_one():
    flat = {"name": "P", "attributes": [{"name": "h", "init": "UNIF(0, 1)"}],
            "states": [{"name": "S"}, {"name": "I"}, {"name": "R"}],
            "transitions": [{"from": "S", "to": "I", "rate": "0.5 * COUNT(\"F\", \"I\") / 50"},
                            {"from": "I", "to": "R", "timeout": "EXPO(2)"},
                            {"from": "R", "to": "S", "condition": "self.h > 0.5"}]}
    alone = {"blocks": [], "links": [], "simulation": {"time_limit": 30}, "agent_types": [flat],
             "populations": [{"name": "F", "type": "P", "count": 50,
                              "initial": [{"state": "I", "count": 5}]}]}
    mixed = _machine([{"from": "Working", "to": "Broken", "condition": "self.go == 0"}],
                     time_limit=30)
    mixed["agent_types"].append(flat)
    mixed["populations"].insert(0, alone["populations"][0])
    a, b = _run(alone, 30).by_name["F"], _run(mixed, 30).by_name["F"]
    assert a.type.levels == 1 and a.type.live_out is a.type.out
    assert a.state.tolist() == b.state.tolist() and a.entered.tolist() == b.entered.tolist()
    assert repr(a.summary(30.0)) == repr(b.summary(30.0))


def test_schema_v27_migration_adds_nothing():
    from nagasim.core.quantities import fill_units
    assert CURRENT_VERSION >= 27
    v26 = _machine([{"from": "Broken", "to": "Working", "timeout": "1", "history": "shallow"}])
    v26["schema_version"] = 26
    migrated = migrate(v26)
    assert migrated["schema_version"] == CURRENT_VERSION
    # v27 adds nothing; v28 only writes out the units (D159)
    assert migrated["agent_types"] == fill_units(copy.deepcopy(v26))["agent_types"]
    assert migrated["populations"] == v26["populations"]


def test_agent_attributes_have_composite_rows():
    from nagasim.core.outputs import summarize_agent_attributes
    layout = _machine([{"from": "Working", "to": "Broken", "rate": "1e-9"}], count=4,
                      initial=[{"state": "Busy", "count": 1}])
    env = _start(layout)
    step_until(env, 1)
    frame, _log = summarize_agent_attributes(env)
    means = frame[frame["Attribute"] == "log"].set_index("State")["End Mean"]
    # Entry actions run outermost first: Working (1) then its leaf (3 or 5).
    assert means["Working"] == np.mean([15, 13, 13, 13]) and means["Busy"] == 15
    assert np.isnan(means["Broken"])


def test_self_state_compared_with_a_composite_warns():
    """K review: `self.state` is the leaf, so a literal naming a composite
    never matches -- W_AGENT_STATE_COMPOSITE points at the expression."""
    layout = _machine([{"from": "Idle", "to": "Busy", "condition": 'self.state == "Working"'},
                       {"from": "Busy", "to": "Idle", "condition": '"Idle" != self.state'}])
    codes = {(p.code, p.path) for p in validate_model_structured(layout)}
    assert ("W_AGENT_STATE_COMPOSITE", "/agent_types/0/transitions/0/condition") in codes
    assert not any(c == "W_AGENT_STATE_COMPOSITE" and t.endswith("/1/condition") for c, t in codes)
