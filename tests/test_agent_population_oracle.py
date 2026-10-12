"""Independent oracle for milestone H2 (agent<->DES coupling, population side):

  - CREATE's optional `population` param: each entity created also creates
    one new agent in that population (initial state, statechart running),
    and `entity.agent` points to it.
  - DISPOSE's optional `remove_agent` (bool, default false): removes the
    entity's agent at disposal; a no-op for an entity with no live agent.
  - `entity_log` gets a column identifying each entity's agent, "" when none.
  - An unknown population name on CREATE is a validation error.

Written from the task's SPEC only, before H2 landed today. Expected counts
are hand-computed from CREATE's own documented timing (`core/blocks/
create.py`'s self_process: with `start_time=0` the first arrival is
immediate, so Constant-1 interarrival with `max_arrivals=N` creates at
t = 0, 1, ..., N-1) and from `AgentType`/`Population`/`Transition`'s own
existing semantics (`core/agents.py`, unchanged by H2). The H1/H2 diffs in
`core/blocks/create.py`, `core/blocks/dispose.py`, `core/expressions.py` and
`outputs.py` were not read.

RESOLVED AMBIGUITIES (GUESS):
  - `Population(..., count=0, max_count=N)` pre-reserves N agent slots for
    CREATE to fill dynamically, the same shape `Action.spawn`'s C5 dynamic
    populations already use ("`max_count`: the most agents alive at once
    when spawn adds some" -- `core/agents.py` `Population` docstring).
    H2's CREATE-driven births are assumed to grow the same way. # GUESS
  - Birth order assigns population indices 0, 1, 2, ... in creation order,
    the same convention an agent's own `enter` action already uses
    (test_agents.py: `[e.agent for e in entities] == [("W", 0), ("W", 1),
    ("W", 2)]` for three agents entering in id order). # GUESS
  - The new `entity_log` column is named "agent", lower-case, exactly as the
    task's spec text writes it -- unlike every existing column in
    `_ENTITY_LOG_COLS` ("Entity ID", "Type", "Created At", ...), which are
    Title Case. Taken literally rather than normalized to match, since the
    spec is the only source for this name. # GUESS
  - `DISPOSE`'s `remove_agent` and `CREATE`'s `population` are plain extra
    keys in the existing `params` dict (same shape as every other block
    param), not a new top-level block field. # GUESS
  - An unknown population on CREATE is checked here only for "flagged as an
    error by validate_model_structured", not a specific code -- plausibly
    `E_POPULATION_UNKNOWN` (already used for an unknown population in an
    agent transition's condition, per test_agents.py), but that is a
    different validation context, so the code is not asserted. # GUESS
"""
import pytest

from nagasim.core import agents as A
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, run_single_simulation, step_until

from conftest import chain, create, delay, dispose, problem_codes


def _model(*records, time_limit=10):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _codes(layout):
    return problem_codes(layout)


def _dispose(name="out", remove_agent=None):
    block = dispose(name)
    if remove_agent is not None:
        block["params"]["remove_agent"] = remove_agent
    return block


def _create(name="c", n=5, population=None):
    params = dict(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=n)
    if population is not None:
        params["population"] = population
    return create(name, **params)


def _run(layout, seed=1, until=None):
    """Prepare and run headless up to `until` (default the model's own
    time_limit); returns (env, model) so a caller can sample mid-run state
    and then keep stepping with a second call using the same env/model."""
    run_len = until if until is not None else layout["simulation"]["time_limit"]
    env = initialize_sim_env(seed)
    env.ctx.sim_horizon = layout["simulation"]["time_limit"]
    model = prepare_model(env, layout)
    if env.ctx.level_system is not None:
        env.ctx.level_system.start(layout["simulation"]["time_limit"])
    if env.ctx.agent_system is not None:
        env.ctx.agent_system.start(layout["simulation"]["time_limit"])
    for start in model.start_blocks():
        start.self_run()
    step_until(env, run_len)
    return env, model


# ------------------------------------------------------------- births/deaths
def test_create_population_births_one_agent_per_entity_and_dispose_removes_them():
    """5 arrivals at t = 0..4 (Constant-1 interarrival, start_time 0), each
    DELAY(2) then DISPOSE(remove_agent=True). By t=3.5: arrivals so far are
    #0,1,2,3 (created at 0,1,2,3) = 4 births; disposals happen at
    creation+2, so #0 (at t=2) and #1 (at t=3) have already left = 2
    removed. Live = 4 - 2 = 2 (agents #2 and #3, created at 2 and 3, leaving
    at 4 and 5). By the end (well past t=6) every agent born has also been
    removed."""
    item = A.AgentType("Item", states=["Alive"], initial="Alive")
    layout = _model(item, A.Population("Pop", "Item", 0, max_count=5), time_limit=10)
    layout["blocks"] = [_create("c", 5, population="Pop"), delay("wait", 2), _dispose("out", True)]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    env, model = _run(layout, until=3.5)
    pop = env.ctx.agent_system.by_name["Pop"]
    assert pop.births == 4 and pop.live == 2             # born so far: 0,1,2,3; #0,#1 already removed
    step_until(env, 10)
    assert pop.births == 5 and pop.live == 0
    assert model.blocks["out"].disposed_entities == 5


def test_dispose_without_remove_agent_leaves_every_born_agent_alive():
    """Same 5 arrivals, DISPOSE with remove_agent left at its false default:
    every entity leaves the DES model but its agent persists."""
    item = A.AgentType("Item", states=["Alive"], initial="Alive")
    layout = _model(item, A.Population("Pop", "Item", 0, max_count=5), time_limit=10)
    layout["blocks"] = [_create("c", 5, population="Pop"), delay("wait", 2), _dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    env, model = _run(layout)
    pop = env.ctx.agent_system.by_name["Pop"]
    assert pop.births == 5 and pop.live == 5
    assert model.blocks["out"].disposed_entities == 5


def test_dispose_remove_agent_is_a_noop_without_a_live_agent():
    """An entity created WITHOUT a population has no agent at all; disposing
    it with remove_agent=True must not error (spec: "no-op for entities
    without a live agent")."""
    layout = _model(time_limit=5)
    layout["blocks"] = [_create("c", 3, population=None), delay("wait", 1), _dispose("out", True)]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    env, model = _run(layout)
    assert model.blocks["out"].disposed_entities == 3


# ------------------------------------------------------------- statechart
def test_born_agents_statechart_runs_from_birth():
    """Agents born at t = 0..4 each carry a New->Old timeout("3") transition
    that starts counting from ITS OWN birth: at t=5 the ones born at 0, 1, 2
    have already reached Old (Old at 3, 4, 5 respectively -- the third
    exactly at the sample instant), the ones born at 3, 4 are still New
    (Old at 6, 7). 3 Old / 2 New, hand-derived from birth time + 3."""
    ticker = A.AgentType("Ticker", states=["New", "Old"], initial="New",
                         transitions=[A.Transition("New", "Old", "timeout", "3")])
    layout = _model(ticker, A.Population("Pop", "Ticker", 0, max_count=5), time_limit=10)
    layout["blocks"] = [_create("c", 5, population="Pop"), delay("wait", 1), _dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    env, model = _run(layout, until=5)
    system = env.ctx.agent_system
    assert system.count("Pop", "Old") == 3
    assert system.count("Pop", "New") == 2


# ------------------------------------------------------------- entity_log
def test_entity_log_agent_column_matches_birth_order():
    """3 entities created (t=0,1,2) each with its own new "Pop" agent, born
    in creation order: entity_log's agent column, read in Created-At order,
    is "Pop:0", "Pop:1", "Pop:2" (the same population-index-by-birth-order
    convention an agent's own `enter` action already uses)."""
    item = A.AgentType("Item", states=["Alive"], initial="Alive")
    layout = _model(item, A.Population("Pop", "Item", 0, max_count=3), time_limit=10)
    layout["blocks"] = [_create("c", 3, population="Pop"), delay("wait", 1), _dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    results = run_single_simulation(layout, seed=1, run_len=10, warmup=0)
    log = results["entity_log"].sort_values("Created At")
    assert list(log["Agent"]) == ["Pop:0", "Pop:1", "Pop:2"]     # GUESS: column named "agent" (reconciled: "Agent", Title Case like its siblings)


def test_entity_log_agent_column_blank_without_population():
    """An entity created without a `population` param has no agent: its
    entity_log row's agent column is the empty string, not null/None."""
    layout = _model(time_limit=5)
    layout["blocks"] = [_create("c", 2, population=None), delay("wait", 1), _dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert not _codes(layout)
    results = run_single_simulation(layout, seed=1, run_len=5, warmup=0)
    log = results["entity_log"]
    assert (log["Agent"] == "").all() and len(log) == 2


# ------------------------------------------------------------- validation
def test_unknown_population_on_create_is_a_validation_error():
    item = A.AgentType("Item", states=["Alive"], initial="Alive")
    layout = _model(item, A.Population("Pop", "Item", 0, max_count=1), time_limit=5)
    layout["blocks"] = [_create("c", 1, population="Nope"), delay("wait", 1), _dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert _codes(layout)                # at least one error (code unspecified -- see GUESS)
