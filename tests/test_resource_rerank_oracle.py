"""Independent oracle for milestone H5 (live queue reranking):

  - A resource's `ranking_expr` (LowestValueFirst/HighestValueFirst) is today
    evaluated ONCE, when the request joins the queue (documented ceiling,
    ARCHITECTURE.md 5.38: "a queue rank is computed when the entity joins the
    queue, so an agent whose acuity rises while it waits keeps its old
    place"). NEW resource field `"rerank": true|false` (default false): when
    true, just before every grant, every waiting request's rank is
    re-evaluated in its own original scope (its entity, its agent) and the
    queue re-sorted by (seize priority, new rank, request time), ties by
    request time. FIFO/LIFO are unaffected either way.

Written from the task's SPEC only, never from a run of the engine or of the
implementation another agent is landing concurrently in
`nagasim/core/resource_manager.py` / `tests/test_resource_rerank.py` (neither
read here). Wire format for `discipline`/`ranking_expr`/priority-tie-breaking
is read from the EXISTING (pre-H5) `tests/test_resources.py` and
`docs/ARCHITECTURE.md` 5.38 (H3's reference case, `entity.agent.acuity`
HighestValueFirst); the agent statechart wire format (attributes, message
transitions with actions, `AGENT("Pop", i)` send targets) is read from
`tests/test_agent_population_oracle.py`, `tests/test_agent_deterioration_oracle.py` and
`nagasim/core/agents.py`'s own docstrings (`compile_target`, `Transition`,
`send.py`) -- never from `resource_manager.py`.

All three cases are fully deterministic (Constant distributions / exact
message timing), so every expected order is derived by hand below, not
sampled.

RESOLVED AMBIGUITIES (GUESS):
  - The new field's name on the low-level `ResourceManager.register(...,
    rerank=...)` call matches the JSON resource field name "rerank" exactly,
    the same way `discipline=`/`ranking_expr=` already do. # GUESS
  - `TNOW` (and model variables) are readable inside `ranking_expr`, the same
    scope every other block expression gets (`core/expressions.py`'s
    docstring lists `entity, TNOW/now, HORIZON, true/false, model
    variables` as the standard scope) -- not verified specifically for
    `ranking_expr`'s own evaluation call site. # GUESS
  - A rerank re-evaluates every STILL-WAITING request (not the one about to
    be granted, which does not exist yet) using the clock at that grant
    instant -- taken literally from the spec's "just before every grant". Not
    a guess about resource_manager.py's code (unread), just a restatement of
    the task spec.

Case 1 (entities, no agents): capacity-1 resource, HighestValueFirst on
`entity.score + TNOW * entity.slope` (the task's own suggested shape). An
occupier holds the unit 0..10; three entities join the queue at t=1,2,3 with
(score, slope) = (100, 0), (50, 3), (0, 6) respectively, each then held for
20 time units once granted (grants at t=10, 30, 50 -- one occupant at a time,
capacity 1).
  rerank=False (rank frozen at join time):
    E1@1: 100+1*0=100; E2@2: 50+2*3=56; E3@3: 0+3*6=18.
    Highest first, fixed for good: order = E1(100), E2(56), E3(18).
  rerank=True (rank recomputed at each grant instant, TNOW is the same value
  for every still-waiting request at that instant so only the SLOPE governs
  who overtakes whom as TNOW grows):
    Grant @T=10 (all three waiting): E1=100, E2=80, E3=60 -> E1 first (same
    top pick as frozen -- the point is what happens next).
    Grant @T=30 (E2, E3 waiting): E2=50+30*3=140; E3=0+30*6=180 -> E3 now
    beats E2 (it did not at t=2 or t=10): order continues E3, then E2.
    order = E1(100), E3(180 at T=30), E2(140 at T=30).
  The two orders differ in positions 2 and 3: [100,50,0] vs [100,0,50].

Case 2 (agents): a Doctor (capacity 1, HighestValueFirst on
`entity.agent.acuity`) queue of three CREATE-`population` patients born at
t=0,1,2 (Constant-1 interarrival, start_time 0, so indices 0,1,2 in creation
order -- the H2 oracle's own documented CREATE timing). Patient 0 seizes the
idle Doctor immediately (own SEIZE delay 0) and holds it for a fixed 5 time
units (a DELAY block); patients 1 and 2 queue behind it, both still at their
birth acuity of 0. A separate, resource-untouched trigger chain (its own
CREATE with `start_time=3`, one arrival, no population) SENDs message
"escalate" to `AGENT("Patients", 2)` at t=3 -- patient 2's Stable-
>Escalated message transition sets `self.acuity` to 10 there; patient 1 never
escalates. At the first grant (t=5, patient 0 releases), patient 1 and
patient 2 are both still waiting:
  rerank=False (rank frozen at join, t=1 and t=2, both BEFORE the t=3
  escalation): both frozen acuities are 0 -- a tie, broken by request time,
  so patient 1 (joined t=1) is granted first, patient 2 second.
    order of service = Patients:0, Patients:1, Patients:2.
  rerank=True (rank re-read live at t=5, AFTER the t=3 escalation):
  patient 1 acuity=0, patient 2 acuity=10 -> patient 2 wins the first grant.
    order of service = Patients:0, Patients:2, Patients:1.
  Read off `entity_log`'s "agent" column (H2's own convention) sorted by
  "Exited At" (`e.deletion_time`): with a fixed 5-unit service time for every
  patient, service order and exit order coincide (no ties past the first
  grant -- only one patient is ever left waiting for the second grant).

Case 3 (rerank=true with FIFO): the exact case-1 layout and timings, but
`discipline="FIFO"` (no `ranking_expr` needed, FIFO does not consult it) and
`rerank=True` -- the spec says rerank has no effect on FIFO/LIFO, so the
service order must be plain request-time order: E1(100), E2(50), E3(0),
regardless of score/slope.
"""
import pytest

from nagasim.core import agents as A
from nagasim.core.base_process import Block
from nagasim.core.blocks import Release, Seize
from nagasim.core.sim_classes import Entity
from nagasim.core.sim_runner import initialize_sim_env, run_single_simulation

from conftest import chain, dispose, problem_codes, run_env


# =========================================================== case 1 & 3
# Low-level harness (Seize/Release blocks feeding a Recorder), the same shape
# `tests/test_resources.py` already uses for discipline oracles -- copied in
# rather than imported, so this file has no dependency on that test module's
# internals changing under it.
class Recorder:
    """Terminal stub that records the order entities reach it."""

    def __init__(self):
        self.order = []
        self.block = Block(position=(9, 9), type="DISPOSE", name="rec")
        self.next_process = []

    def self_run(self, entity):
        self.order.append(entity)


def _lane(env, resource_name, index, recorder, *, delay, priority=0):
    seize = Seize(env, Block(position=(index, 0), type="SEIZE", name=f"seize_{index}"),
                  delay=delay, priority=priority, quantity=1)
    release = Release(env, Block(position=(index, 1), type="RELEASE", name=f"release_{index}"))
    seize.set_params_from_dict({"resource_name": resource_name, "priority": priority,
                                "quantity": 1, "distribution": "Constant", "dist_params": {"value": delay}})
    release.set_params_from_dict({"resource_name": resource_name,
                                 "distribution": "Constant", "dist_params": {"value": 0}})
    seize.next_process = [release]
    release.next_process = [recorder]
    return seize


def _feed(env, seize, entity, at):
    def _run():
        yield env.timeout(at)
        seize.self_run(entity)
    env.process(_run())


# (join_time, score, slope) for the three queued entities in both case 1 and
# case 3 -- see the module docstring for the hand derivation.
_ENTRIES = [(1, 100, 0), (2, 50, 3), (3, 0, 6)]
_OCCUPIER_DELAY = 10
_QUEUED_DELAY = 20


def _run_case1_or_3(discipline, ranking_expr, rerank):
    env = initialize_sim_env(seed=0)
    env.ctx.resource_manager.register("R", capacity=1, discipline=discipline,
                                      ranking_expr=ranking_expr, rerank=rerank)   # GUESS: kwarg name
    recorder = Recorder()
    occupier = Entity()
    occupier.score, occupier.slope = 0, 0    # every entity needs the ranking attributes
    occupier_lane = _lane(env, "R", 0, recorder, delay=_OCCUPIER_DELAY)
    _feed(env, occupier_lane, occupier, 0)

    for index, (join_time, score, slope) in enumerate(_ENTRIES):
        entity = Entity()
        entity.score, entity.slope = score, slope
        lane = _lane(env, "R", index + 1, recorder, delay=_QUEUED_DELAY)
        _feed(env, lane, entity, join_time)

    env.run()
    return [e.score for e in recorder.order if e is not occupier]


def test_rerank_false_keeps_the_join_time_rank():
    served = _run_case1_or_3("HighestValueFirst", "entity.score + TNOW * entity.slope", False)
    assert served == [100, 50, 0]


def test_rerank_true_reorders_by_the_live_rank_at_each_grant():
    served = _run_case1_or_3("HighestValueFirst", "entity.score + TNOW * entity.slope", True)
    assert served == [100, 0, 50]   # E3 (slope 6) overtakes E2 (slope 3) by T=30


def test_rerank_true_has_no_effect_on_fifo():
    served = _run_case1_or_3("FIFO", None, True)
    assert served == [100, 50, 0]   # plain request-time order, ranking ignored


# =========================================================== case 2 (agents)
def _create(name, distribution, dist_params, n, population, start_time=0):
    params = {"e_type": "DEFAULT", "distribution": distribution, "dist_params": dist_params,
              "entities_per_arrival": 1, "max_arrivals": n, "start_time": start_time}
    if population is not None:
        params["population"] = population
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0], "params": params}


def _seize(name, resource):
    return {"name": name, "description": name, "type": "SEIZE", "position": [0, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                       "priority": 0, "quantity": 1, "resource_name": resource}}


def _delay(name, value):
    return {"name": name, "description": name, "type": "DELAY", "position": [0, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": value}}}


def _release(name, resource):
    return {"name": name, "description": name, "type": "RELEASE", "position": [0, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}, "resource_name": resource}}


def _send(name, message, to):
    return {"name": name, "description": name, "type": "SEND", "position": [0, 0],
            "params": {"message": message, "to": to}}


def _model(*records, time_limit):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _codes(layout):
    return problem_codes(layout)


def _run(layout, seed, run_len):
    return run_env(layout, run_len, seed=seed)


def _doctor_agents_layout(rerank):
    patient = A.AgentType(
        "Patient", states=["Stable", "Escalated"], initial="Stable",
        attributes=[{"name": "acuity", "init": "0"}],
        transitions=[A.Transition("Stable", "Escalated", "message", "escalate",
                                  actions=[{"target": "self.acuity", "expr": "10"}])])
    pop = A.Population("Patients", "Patient", 0, max_count=3)
    time_limit = 20.0
    layout = _model(patient, pop, time_limit=time_limit)

    create_patients = _create("arrive", "Constant", {"value": 1.0}, 3, "Patients")
    seize = _seize("seize_doc", "Doctor")
    service = _delay("service", 5.0)
    release = _release("release_doc", "Doctor")
    out = dispose("out")
    out["params"]["remove_agent"] = True

    trigger = _create("trigger", "Constant", {"value": 1.0}, 1, None, start_time=3)
    escalate = _send("escalate", "escalate", 'AGENT("Patients", 2)')
    trigger_out = dispose("trigger_out")

    layout["blocks"] = [create_patients, seize, service, release, out, trigger, escalate, trigger_out]
    layout["links"] = chain(create_patients, seize, service, release, out) + chain(trigger, escalate, trigger_out)
    layout["resources"] = [{"name": "Doctor", "capacity": 1, "discipline": "HighestValueFirst",
                            "ranking_expr": "entity.agent.acuity", "rerank": rerank}]
    return layout, time_limit


def _service_order_by_agent(layout, seed, time_limit):
    assert not _codes(layout)
    results = run_single_simulation(layout, seed=seed, run_len=time_limit, warmup=0)
    log = results["entity_log"].sort_values("Exited At")
    # Drop the trigger entity (no agent -> blank "agent" column, H2's convention).
    return [a for a in log["Agent"] if a]


def test_agents_rerank_false_ties_broken_by_request_time():
    layout, time_limit = _doctor_agents_layout(rerank=False)
    served = _service_order_by_agent(layout, seed=1, time_limit=time_limit)
    assert served == ["Patients:0", "Patients:1", "Patients:2"]


def test_agents_rerank_true_lets_the_escalated_patient_overtake():
    layout, time_limit = _doctor_agents_layout(rerank=True)
    served = _service_order_by_agent(layout, seed=1, time_limit=time_limit)
    assert served == ["Patients:0", "Patients:2", "Patients:1"]
