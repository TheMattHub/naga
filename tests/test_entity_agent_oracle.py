"""Independent oracle for milestone H1 (agent<->DES coupling, read/write side):

  - DES block expressions can read `entity.agent.<attr>` and
    `entity.agent.state == "<Name>"` / `!=`.
  - ASSIGN can target `entity.agent.<attr>`, writing that agent's attribute
    and immediately waking the agent system so a condition transition
    re-checks at the same instant.
  - Reading `entity.agent.<attr>` for an entity whose agent has been removed
    is a runtime error; writing `entity.agent.state` is a validation error.

Written from the task's SPEC only, before H1 landed today. Every expected
value below is hand-computed from the model's own timing (Constant delays,
`timeout "0"` transitions), never read off a run. `core/expressions.py`,
`core/blocks/assign.py`, `core/blocks/create.py`, `core/blocks/dispose.py`
and `outputs.py` were not read for their H1/H2 diffs; the DES-side
conventions used here (ASSIGN's `{"target", "expr"}` shape, DECIDE's
"Condition" mode taking one extra link as an implicit ELSE, `chain()`/
`_block()`/`create()`/`delay()`/`dispose()`) come from tests/test_agents.py,
tests/test_hold_match.py and tests/conftest.py, which already exist.

RESOLVED AMBIGUITIES (GUESS):
  - ASSIGN target `"entity.agent.<attr>"` -- assign.py's own docstring
    already gives the shape `{"target": "entity.<attr>" | "<variable>",
    "expr": ...}`; H1's task text says the target has "the same ASSIGN
    param shape as other targets such as entity.x", so `entity.agent.<attr>`
    is assumed to slot into the same `target` string, not a new key. # GUESS
  - Writing `entity.agent.state`/`entity.agent.<attr>` is checked here only
    for "flagged as an error by validate_model_structured", not for a
    specific error code -- the task names no code, and guessing one wrong
    would fail the test for the wrong reason. # GUESS
  - A stale (removed) agent's attribute read raises some `Exception` at
    evaluation time. The engine already refuses a stale `AGENT(...)` ref the
    same way (D98, C5's `ALIVE()`), so `entity.agent.<attr>` on a removed
    agent is assumed to reuse that same refusal path; the exact exception
    class is not asserted, only that evaluating the block raises. # GUESS
  - A transition's `actions` list may hold both `Action.enter(...)` and
    `Action.remove("self")`, run in order -- both are pre-existing (AB-1/
    AB-2, C5) independent action verbs; nothing in their docs forbids
    combining them on one transition. # GUESS
"""
import pytest

from nagasim.core import agents as A

from conftest import chain, delay, dispose, problem_codes, run_env


def _block(name, btype, **params):
    return {"name": name, "description": name, "type": btype, "position": [0, 0],
            "params": params}


def _model(*records, time_limit=10):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _codes(layout):
    return problem_codes(layout)


def _run(layout, seed=1):
    """Run headless; hand back (env, model) so a test can read both the
    agent system and the DES block instances (e.g. Dispose.disposed_entities)."""
    return run_env(layout, layout["simulation"]["time_limit"], seed=seed)


# --------------------------------------------------------------- read side
def test_reads_entity_agent_attribute_and_state():
    """One agent: Init->B fires at t=0 (timeout "0") and enters "probe", so by
    the time the entity clears a 1-unit DELAY the agent has long been in B.
    Two ASSIGNs into plain variables read `entity.agent.state` and
    `entity.agent.score` (init "42", never written) -- both unambiguous by
    t=1, since the transition that created the entity fired at t=0."""
    watched = A.AgentType("Watched", attributes=[A.Attribute("score", "42")],
                          states=["Init", "B"],
                          transitions=[A.Transition("Init", "B", "timeout", "0",
                                                    actions=[A.Action.enter("probe")])],
                          initial="Init")
    layout = _model(watched, A.Population("W", "Watched", 1))
    layout["blocks"] = [
        delay("probe", 1),
        _block("check_state", "ASSIGN",
              assignments=[{"target": "checked_state", "expr": 'entity.agent.state == "B"'}]),
        _block("check_score", "ASSIGN",
              assignments=[{"target": "checked_score", "expr": "entity.agent.score"}]),
        dispose("out"),
    ]
    layout["links"] = chain(*layout["blocks"])
    layout["variables"] = [{"name": "checked_state", "initial": 0}, {"name": "checked_score", "initial": 0}]
    assert not _codes(layout)
    env, model = _run(layout)
    assert env.ctx.variables["checked_state"] is True
    assert env.ctx.variables["checked_score"] == 42
    assert model.blocks["out"].disposed_entities == 1


# --------------------------------------------------------------- write side
def test_assign_to_entity_agent_wakes_the_agent_system_at_the_same_instant():
    """Agent starts in "Wait" (attribute x=0) and enters "hold" at t=0. A
    2-unit DELAY, then ASSIGN sets entity.agent.x = 5. The agent's own
    condition transition Wait->Done fires on self.x >= 5: since ASSIGN must
    wake the agent system before forwarding, the transition -- and therefore
    the state -- is already "Done" at t=2 exactly, not one instant later."""
    flag = A.AgentType("Flag", attributes=[A.Attribute("x", "0")], states=["Init", "Wait", "Done"],
                       transitions=[A.Transition("Init", "Wait", "timeout", "0",
                                                 actions=[A.Action.enter("hold")]),
                                    A.Transition("Wait", "Done", "condition", "self.x >= 5")],
                       initial="Init")
    layout = _model(flag, A.Population("F", "Flag", 1))
    layout["blocks"] = [
        delay("hold", 2),
        _block("set_x", "ASSIGN", assignments=[{"target": "entity.agent.x", "expr": "5"}]),
        dispose("out"),
    ]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    env, model = _run(layout)
    pop = env.ctx.agent_system.by_name["F"]
    assert pop.state[0] == pop.type.index["Done"]
    assert pop.entered[0] == 2                          # the transition's own clock
    assert model.blocks["out"].disposed_entities == 1


def test_decide_on_entity_agent_state_routes_by_the_same_instant_condition():
    """Three agents enter at t=0, 1, 2 (timeout "self.id"), each waits 2 units,
    then ASSIGN stamps entity.agent.x = TNOW (=2, 3, 4 for agents 0, 1, 2).
    The condition Enter->B is self.x >= 3: agent 0 (x=2) stays in "Enter",
    agents 1 and 2 (x=3, 4) reach "B" -- at the same instant the ASSIGN runs,
    so a DECIDE right after it, testing entity.agent.state == "B", must
    already see the post-transition state: 1 entity to the false branch,
    2 to the true branch."""
    worker = A.AgentType("Worker", attributes=[A.Attribute("x", "0")],
                         states=["Init", "Enter", "B"],
                         transitions=[A.Transition("Init", "Enter", "timeout", "self.id",
                                                   actions=[A.Action.enter("wait")]),
                                      A.Transition("Enter", "B", "condition", "self.x >= 3")],
                         initial="Init")
    layout = _model(worker, A.Population("P", "Worker", 3), time_limit=8)
    wait, assign = delay("wait", 2), _block("set_x", "ASSIGN",
                                           assignments=[{"target": "entity.agent.x", "expr": "TNOW"}])
    decide = _block("route", "DECIDE", mode="Condition",
                    conditions=['entity.agent.state == "B"'])
    dispose_true, dispose_false = dispose("out_true"), dispose("out_false")
    layout["blocks"] = [wait, assign, decide, dispose_true, dispose_false]
    layout["links"] = chain(wait, assign, decide) + [
        {"source": "route", "target": "out_true"},
        {"source": "route", "target": "out_false"},
    ]
    assert not _codes(layout)
    env, model = _run(layout)
    assert model.blocks["out_true"].disposed_entities == 2
    assert model.blocks["out_false"].disposed_entities == 1
    pop = env.ctx.agent_system.by_name["P"]
    assert list(pop.state) == [pop.type.index["Enter"], pop.type.index["B"], pop.type.index["B"]]


# --------------------------------------------------------------- errors
def test_writing_entity_agent_state_is_a_validation_error():
    """`entity.agent.<attr>` is read-only from DES: an ASSIGN targeting it
    (here entity.agent.state, but any entity.agent.<attr> target per the
    task's spec) must be flagged, not silently accepted."""
    flag = A.AgentType("Flag", states=["A", "B"],
                       transitions=[A.Transition("A", "B", "timeout", "1")])
    layout = _model(flag, A.Population("F", "Flag", 1))
    layout["blocks"] = [
        delay("d", 1),
        _block("bad", "ASSIGN", assignments=[{"target": "entity.agent.state", "expr": '"B"'}]),
        dispose("out"),
    ]
    layout["links"] = chain(*layout["blocks"])
    assert _codes(layout)                # at least one error (code unspecified -- see GUESS)


def test_reading_a_removed_agents_attribute_raises_at_runtime():
    """The transition that creates the entity also removes its own agent
    (Action.remove("self"), an existing C5 action) in the same firing, so by
    the time the DES chain reaches the read, the agent slot is stale.
    Reading entity.agent.x downstream must raise rather than return garbage
    or silently read a reused slot."""
    ghost = A.AgentType("Ghost", attributes=[A.Attribute("x", "1")], states=["Init", "Gone"],
                        transitions=[A.Transition("Init", "Gone", "timeout", "0",
                                                  actions=[A.Action.enter("wait"),
                                                          A.Action.remove("self")])],
                        initial="Init")
    layout = _model(ghost, A.Population("G", "Ghost", 1))
    layout["blocks"] = [
        delay("wait", 1),
        _block("read", "ASSIGN", assignments=[{"target": "seen", "expr": "entity.agent.x"}]),
        dispose("out"),
    ]
    layout["links"] = chain(*layout["blocks"])
    layout["variables"] = [{"name": "seen", "initial": 0}]
    assert not _codes(layout)
    with pytest.raises(Exception):
        _run(layout)
