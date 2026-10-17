"""Independent oracle tests for PHASE5 AB-3 (docs/history/PHASE5_PLAN.md, "Phase 5B:
agent-based modelling", the AB-3 milestone row: "space, cKDTree, movement,
trace and 2D/3D playback; tests: arrival at dist/speed, NEAR vs brute force,
network move time, trace_limit"), plus the "Design refinements" section
(`Space(kind, bounds)`, `Action.set/send/enter/transfer/move_to/signal`,
`core/agentspace.py: positions linear in time, move_to schedules the arrival
and delivers "arrived"`, `Timeline.state_at()["agents"]`, `simulation.agents.
trace_agents` default 200) and the Phase 5B intro's storage line ("columnar
numpy per population ... later x0,y0,x1,y1,t0,t1").

Written BEFORE AB-3 landed (checked: `core/agents.py` still raises
E_AGENT_SPACE for any non-null `space` on an agent type -- "space is not
available yet (PHASE5 AB-3)", `_check_type` around line 594 -- and
`core/trace.py`'s `_RECORDED_EVENTS` has no "agent_state"/"agent_moved" kind
yet). Every expected value below is a hand derivation from the plan text
alone, independent of whatever the concurrently-landing engine computes;
only wire-format details (a JSON key, a column name, an event field) may be
swapped once it lands, per the task brief -- never a VALUE.

CHOICE OF "PUBLIC ACCESSOR": the plan gives two different ways an agent's
position could be read back after a run:
  (a) the population's own storage columns, quoted verbatim from the Phase 5B
      intro: "x0,y0,x1,y1,t0,t1" -- the same per-population-column pattern
      AB-1/AB-2's own oracle tests already rely on for `pop.state[i]`,
      `pop.entered[i]`, `pop.attrs[name][i]`;
  (b) `Timeline.state_at()["agents"] = {(pop, i): (x, y, state)}`, a
      trace/playback-only view (today's `Timeline.state_at()` docstring in
      `core/playback.py` lists no "agents" key at all -- it is added by AB-3
      alongside the "agents" section of `TraceRecorder._RECORDED_EVENTS`).
Tests 1, 3, 4 and 6 use (a): it needs only the same per-population column
access already proven correct in the sibling files, with no dependency on
whether the trace/playback wiring for agents lands in the same commit. Test 5
is ABOUT the trace mechanism itself ("trace_limit ... exactly 200 agents'
segments in the trace"), so it necessarily uses (b) via `TraceRecorder`; its
own docstring below flags the extra wire-format guesses that entails.

ASSUMED WIRE FORMAT (AB-3 not landed):
  - Population placement for continuous space: `{"kind": "random", "bounds":
    [x0, y0, x1, y1]}`, literal from the plan's JSON example. A degenerate
    box (x0==x1, y0==y1) is used in tests 1, 5 and 6 to pin a single agent to
    an exact point without needing to reproduce any RNG draw by hand.
  - Population placement for NETWORK space: not given anywhere in the plan
    text (only the continuous "random"/"bounds" form is shown). Test 4
    assumes `{"kind": "node", "node": "<name>"}` by direct analogy to a
    mover fleet's own `"home": "<node>"` (`core/network.py`,
    `tests/test_network.py`'s `fleet()`), the only other place this codebase
    pins something to a network node. This is the one guess in this file
    that is a real GUESS, not a spec quote -- flagged again in test 4.
  - `move_to` completion delivers a message named "arrived" to the SAME
    agent that moved (literal plan text: "move_to schedules the arrival and
    delivers \"arrived\""; the plan gives no other candidate recipient, and
    every other action in AB-2 that names no explicit target defaults to
    "self" in some form -- SEND's default `to` is "entity.agent"). Tests 1,
    4, 5 and 6 all rely on this to observe an exact arrival TIME as a state
    transition's `entered` value (the same idiom AB-1/AB-2 use throughout),
    rather than needing to poll a live position.
  - Test 5's trace event: assumed kind "agent_moved" (named in the plan,
    "Trace events agent_state, agent_moved only for index < trace_limit"),
    carrying the agent's own index in `entity_id` and its population's name
    in `target` -- by direct analogy to how `mover_moved` already reuses
    `entity_id` for the unit index within a fleet and `target` for the
    fleet's name (`core/trace.py`). `TraceRecorder._RECORDED_EVENTS` will
    also need "agent_moved" added for the recorder to keep it at all; if
    the landed shape differs, only `_traced_indices()` below needs a swap.
  - `simulation.agents.trace_agents` (global default 200), read from the
    "Design refinements" line; population-level `trace_limit` is left unset
    in test 5 so the global default/override is what is being tested.

ADAPTED AFTER AB-3 LANDED (wire format only, per "Design refinements"; no
expected value or assertion changed):
  - `speed` is a top-level agent-type field, not `space.speed`.
  - A continuous space declares `bounds`: [0, 0, 10, 10] (tests 1, 5, 6) and
    [0, 0, 50, 50] (test 3: the field box plus the boundary probe, at most
    40 + 6). The placement's own degenerate `bounds` still pins the agents.
  - Network placement is `{"kind": "table", "nodes": ["dock"]}`, not the
    guessed `{"kind": "node", "node": "dock"}`.
  - A string `move_to` is an expression, so the node is the quoted literal
    '"store"', not the bare name "store".
  - Trace: the guessed `agent_moved` shape (entity_id = index, target =
    population) matched `core/trace.py`; unchanged.

NOT WRITTEN: "moving to a moving target / another agent's position." The
plan's `Action.move_to` grammar is explicit and closed: `{"move_to": [x, y]
| "<node>"}` -- a literal coordinate pair or a literal node name, never an
expression, `self.<attr>` or `AGENT(...)` reference. Nothing in the plan
text (the Phase 5B intro, the AB-3 row, or the "Agent authoring classes"
refinement) describes a moving-target form, so there is no spec to derive an
oracle from; inventing one would be testing a guess, not the plan.

BOUNDARY RULE FOR NEAR (test 3): the plan names `scipy.spatial.cKDTree` as
the implementation ("Space via scipy.spatial.cKDTree", "neighbours via
cKDTree built at most once per distinct query instant") but never states
whether a point exactly `r` away counts. `cKDTree.query_ball_point`'s own
default is inclusive (distance <= r, eps=0), so the boundary case below
asserts the inclusive count -- flagged here as a real ambiguity the plan
does not resolve, not a derivation, so the coordinator should re-check it
once AB-3's own NEAR implementation is readable.
"""
import numpy as np
import pytest

from nagasim.core.network import build_network
from nagasim.core.trace import TraceRecorder

from conftest import layout as make_layout, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0, recorder=None):
    """Optional `TraceRecorder` subscribed the way `sim_runner.run_single_simulation`
    subscribes one (before `prepare_model`, so a resource's/agent's baseline
    event is not missed) -- giving ONE run both raw population-column access
    and, when needed, trace events, instead of two separate runs that would
    have to assume their RNG draws line up."""
    subscribers = (recorder,) if recorder is not None else ()
    env, _model = run_env(layout, run_len, seed=seed, subscribers=subscribers)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _sim(time_limit, **agents):
    d = {"time_limit": time_limit, "warmup": 0, "replications": 1}
    if agents:
        d["agents"] = agents
    return d


def _agent_layout(blocks=(), links=(), **sections):
    return make_layout(list(blocks), list(links), **sections)


def _interp(x0, y0, x1, y1, t0, t1, t):
    frac = 0.0 if t1 <= t0 else min(max((t - t0) / (t1 - t0), 0.0), 1.0)
    return x0 + frac * (x1 - x0), y0 + frac * (y1 - y0)


# ============================================================== 1. arrival
def test_arrival_at_distance_over_speed_and_exact_mid_flight_interpolation():
    """(0,0) -> (3,4) is the 3-4-5 triangle: distance 5, speed 2, so arrival
    is EXACTLY t0 + 2.5 (5/2, exact in binary floating point). A short run
    (1.25, half of 2.5) catches the agent still on the open segment, whose
    own x0,y0,x1,y1,t0,t1 must already read (0,0,3,4,0.0,2.5) -- the segment
    is committed at the moment `move_to` fires, not filled in only on
    arrival -- and the position at t=1.25 is the exact linear interpolation
    (1.5, 2.0), independently recomputed here, not read off any engine
    interpolation helper."""
    mover = {"name": "Mover", "parameters": {}, "attributes": [],
             "states": [{"name": "Start"}, {"name": "Moving"}, {"name": "Arrived"}],
             "initial_state": "Start", "space": {"kind": "continuous", "bounds": [0, 0, 10, 10]},
             "speed": "2",
             "transitions": [
                 {"from": "Start", "to": "Moving", "timeout": "0",
                  "actions": [{"move_to": [3, 4]}]},
                 {"from": "Moving", "to": "Arrived", "message": "arrived"}]}
    population = {"name": "Movers", "type": "Mover", "count": 1,
                  "initial": [{"state": "Start", "count": 1}],
                  "placement": {"kind": "random", "bounds": [0, 0, 0, 0]}}
    layout = _agent_layout(agent_types=[mover], populations=[population], simulation=_sim(5.0))

    mid = _run(layout, 1.25, seed=0)
    pop_mid = _pop(mid, "Movers")
    assert int(pop_mid.state[0]) == pop_mid.type.index["Moving"]
    assert (float(pop_mid.x0[0]), float(pop_mid.y0[0]), float(pop_mid.x1[0]),
            float(pop_mid.y1[0]), float(pop_mid.t0[0]), float(pop_mid.t1[0])) == (
        pytest.approx(0.0), pytest.approx(0.0), pytest.approx(3.0),
        pytest.approx(4.0), pytest.approx(0.0), pytest.approx(2.5))
    x, y = _interp(float(pop_mid.x0[0]), float(pop_mid.y0[0]), float(pop_mid.x1[0]),
                    float(pop_mid.y1[0]), float(pop_mid.t0[0]), float(pop_mid.t1[0]), 1.25)
    assert (x, y) == pytest.approx((1.5, 2.0))

    done = _run(layout, 5.0, seed=0)
    pop_done = _pop(done, "Movers")
    assert int(pop_done.state[0]) == pop_done.type.index["Arrived"]
    assert float(pop_done.entered[0]) == pytest.approx(2.5, rel=1e-12)


# ==================================================== 2. moving target -- SKIPPED
# See the module docstring's "NOT WRITTEN" paragraph: `Action.move_to`'s
# grammar (`[x, y] | "<node>"`) has no expression/reference form in the plan
# text, so "move to a moving target or another agent's position" has no
# spec to derive an oracle from.


# ==================================================== 3. NEAR vs brute force
def test_near_matches_a_brute_force_count_over_the_populations_own_columns():
    """30 Field agents placed uniformly in [0,40] x [0,40] (never move); five
    Probe populations, each ONE agent PINNED at an exact point (a degenerate
    placement box, the same trick test 1 uses for (0,0)) that writes
    `self.near_count = NEAR("Field", r)` into an attribute on a t=0 timeout
    transition. Four combinations are fixed query points/radii chosen with
    no relation to the random field; a fifth is constructed to land EXACTLY
    on the boundary (see the module docstring's inclusive-boundary note).

    Two runs, same seed: the first places only "Field" and reads its exact
    positions off `x0`/`y0` (never-moved, so the current position IS the
    initial one); the boundary probe's point is then built from that
    reading. The second run adds the probes to the SAME "Field" population
    (same name, count, bounds) and is asserted to reproduce BIT-IDENTICAL
    field positions before it is trusted for the brute-force comparison --
    an explicit check of the per-population RNG isolation the plan documents
    for firing times ("agent i's stream is keyed by (seed, hash(pop), i)
    alone"), extended here to placement rather than assumed silently.
    """
    field_type = {"name": "Field", "parameters": {}, "attributes": [],
                  "states": [{"name": "Idle"}], "initial_state": "Idle",
                  "space": {"kind": "continuous", "bounds": [0, 0, 50, 50]}, "speed": "1",
                  "transitions": []}
    N, bounds = 30, [0, 0, 40, 40]
    field_pop = {"name": "Field", "type": "Field", "count": N,
                 "initial": [{"state": "Idle", "count": N}],
                 "placement": {"kind": "random", "bounds": bounds}}

    solo = _agent_layout(agent_types=[field_type], populations=[field_pop], simulation=_sim(1.0))
    env_solo = _run(solo, 1.0, seed=0)
    field_solo = _pop(env_solo, "Field")
    xy_solo = np.array([field_solo.x0, field_solo.y0], dtype=float).T
    assert xy_solo.shape == (N, 2)

    fixed = [("QA", (10.0, 10.0), 8.0), ("QA2", (10.0, 10.0), 20.0),
             ("QB", (30.0, 30.0), 8.0), ("QB2", (30.0, 30.0), 20.0)]
    fx, fy = xy_solo[0]
    boundary_point, boundary_r = (fx + 6.0, fy), 6.0

    def _probe(pname, point, r):
        atype = {"name": pname, "parameters": {}, "attributes": [{"name": "near_count", "init": "0"}],
                 "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                 "space": {"kind": "continuous", "bounds": [0, 0, 50, 50]}, "speed": "1",
                 "transitions": [{"from": "A", "to": "B", "timeout": "0",
                                  "actions": [{"target": "self.near_count",
                                              "expr": f'NEAR("Field", {r})'}]}]}
        pop = {"name": pname, "type": pname, "count": 1, "initial": [{"state": "A", "count": 1}],
               "placement": {"kind": "random", "bounds": [point[0], point[1], point[0], point[1]]}}
        return atype, pop

    combos = fixed + [("QBound", boundary_point, boundary_r)]
    probe_types, probe_pops = [], []
    for name, point, r in combos:
        atype, pop = _probe(name, point, r)
        probe_types.append(atype)
        probe_pops.append(pop)

    full = _agent_layout(agent_types=[field_type] + probe_types,
                         populations=[field_pop] + probe_pops, simulation=_sim(1.0))
    env_full = _run(full, 1.0, seed=0)
    field_full = _pop(env_full, "Field")
    xy_full = np.array([field_full.x0, field_full.y0], dtype=float).T
    assert np.array_equal(xy_solo, xy_full)          # RNG isolation, not assumed

    for name, point, r in combos:
        pop = _pop(env_full, name)
        engine_count = int(pop.attrs["near_count"][0])
        brute = int((np.hypot(xy_full[:, 0] - point[0], xy_full[:, 1] - point[1]) <= r).sum())
        assert engine_count == brute, (name, point, r)


# ========================================================= 4. network move
def test_network_move_time_equals_shortest_length_over_speed():
    """The `tests/test_network.py` triangle (reused verbatim -- already
    independently tested `core/network.py` geometry, not AB-3 code): dock
    (0,0) -> store (30,40) directly is the 50 m hypotenuse; the only other
    route, via `far`, is a hand-checked-longer detour of 80 + 100 = 180 m.
    A Runner starts AT `dock` (a one-node table placement) and `move_to`s "store" at speed 5: shortest length / speed =
    50 / 5 = 10.0 exactly. Cross-checked two ways: by hand (50 < 180, so the
    direct segment IS the shortest path) and against `core/network.py`'s own
    already-shipped Dijkstra (`Network.distance`), independent of whatever
    routing AB-3's agent space does internally.
    """
    network = {
        "nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "store", "x": 30, "y": 40},
                  {"name": "far", "x": 0, "y": 80}],
        "segments": [{"from": "dock", "to": "store"}, {"from": "dock", "to": "far"},
                     {"from": "far", "to": "store", "length": 100}],
    }
    net = build_network(network)
    assert net.distance("dock", "store") == pytest.approx(50.0)
    assert net.path("dock", "store") == ["dock", "store"]
    assert net.path_length(["dock", "far", "store"]) == pytest.approx(180.0)
    expected_time = 50.0 / 5.0
    assert expected_time == pytest.approx(10.0)

    runner = {"name": "Runner", "parameters": {}, "attributes": [],
              "states": [{"name": "Start"}, {"name": "Moving"}, {"name": "Arrived"}],
              "initial_state": "Start", "space": {"kind": "network"}, "speed": "5",
              "transitions": [
                  {"from": "Start", "to": "Moving", "timeout": "0",
                   "actions": [{"move_to": '"store"'}]},
                  {"from": "Moving", "to": "Arrived", "message": "arrived"}]}
    population = {"name": "Runners", "type": "Runner", "count": 1,
                  "initial": [{"state": "Start", "count": 1}],
                  "placement": {"kind": "table", "nodes": ["dock"]}}
    layout = _agent_layout(agent_types=[runner], populations=[population],
                           network=network, simulation=_sim(20.0))
    env = _run(layout, 20.0, seed=0)
    pop_rt = _pop(env, "Runners")
    assert int(pop_rt.state[0]) == pop_rt.type.index["Arrived"]
    assert float(pop_rt.entered[0]) == pytest.approx(expected_time, rel=1e-12)


# ============================================================ 5. trace_limit
def _trace_limit_layout(trace_agents=None):
    """300 identical Movers, all pinned at (0,0), all moving to (3,4) at
    speed 2 on a literal `timeout: "0"` -- fully deterministic (no RNG
    anywhere in the model), so every agent's arrival is checkable directly,
    independent of how many of them the trace decided to keep."""
    mover = {"name": "Mover2", "parameters": {}, "attributes": [],
             "states": [{"name": "Start"}, {"name": "Moving"}, {"name": "Arrived"}],
             "initial_state": "Start", "space": {"kind": "continuous", "bounds": [0, 0, 10, 10]},
             "speed": "2",
             "transitions": [
                 {"from": "Start", "to": "Moving", "timeout": "0",
                  "actions": [{"move_to": [3, 4]}]},
                 {"from": "Moving", "to": "Arrived", "message": "arrived"}]}
    population = {"name": "Runners", "type": "Mover2", "count": 300,
                  "initial": [{"state": "Start", "count": 300}],
                  "placement": {"kind": "random", "bounds": [0, 0, 0, 0]}}
    agents_section = {} if trace_agents is None else {"trace_agents": trace_agents}
    return _agent_layout(agent_types=[mover], populations=[population],
                         simulation=_sim(5.0, **agents_section))


def _traced_indices(events, pop_name):
    """ASSUMED shape (module docstring): `agent_moved`, `entity_id` = the
    agent's own index, `target` = its population's name -- the same field
    reuse `mover_moved` already does for a fleet's unit index/fleet name."""
    return {e.entity_id for e in events if e.kind == "agent_moved" and e.target == pop_name}


@pytest.mark.parametrize("trace_agents, expected_traced", [(None, 200), (10, 10), (0, 0)])
def test_trace_limit_caps_traced_agents_but_never_their_movement(trace_agents, expected_traced):
    layout = _trace_limit_layout(trace_agents)
    recorder = TraceRecorder()
    env = _run(layout, 5.0, seed=0, recorder=recorder)
    pop_rt = _pop(env, "Runners")

    assert len(_traced_indices(recorder.events, "Runners")) == expected_traced

    # Untraced agents still moved correctly: EVERY one of the 300 arrived at
    # exactly 2.5, regardless of how many of them the trace kept.
    assert int((pop_rt.state == pop_rt.type.index["Arrived"]).sum()) == 300
    assert np.asarray(pop_rt.entered, dtype=float) == pytest.approx([2.5] * 300, rel=1e-12)


# =================================================================== 6. CRN
def test_adding_movement_to_one_population_leaves_another_bit_identical():
    """Same own-rate model as test_agents_oracles.py's test 1 (state A -> B
    at a bare constant rate, no `self`): run alone, then again alongside an
    unrelated SECOND population that HAS space and moves (a `move_to`
    action) -- "People"'s firing time must not move by a single bit. Proves
    that giving one population a spatial/movement track does not perturb an
    unrelated population's own RNG-driven firing, extending AB-1's test 6
    (CRN isolation between populations) to movement specifically.
    """
    seed, pop_name, lam = 0, "People", 0.4
    item_type = {"name": "Item", "parameters": {"lam": lam}, "attributes": [],
                 "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
                 "transitions": [{"from": "A", "to": "B", "rate": "lam"}]}
    population = {"name": pop_name, "type": "Item", "count": 1,
                  "initial": [{"state": "A", "count": 1}]}
    run_len = 50.0

    alone = _agent_layout(agent_types=[item_type], populations=[population], simulation=_sim(run_len))
    env_alone = _run(alone, run_len, seed=seed)
    t_alone = float(_pop(env_alone, pop_name).entered[0])

    mover_type = {"name": "Mover3", "parameters": {}, "attributes": [],
                  "states": [{"name": "Start"}, {"name": "Moving"}], "initial_state": "Start",
                  "space": {"kind": "continuous", "bounds": [0, 0, 10, 10]}, "speed": "1",
                  "transitions": [{"from": "Start", "to": "Moving", "timeout": "1",
                                   "actions": [{"move_to": [5, 5]}]}]}
    mover_pop = {"name": "Movers", "type": "Mover3", "count": 3,
                 "initial": [{"state": "Start", "count": 3}],
                 "placement": {"kind": "random", "bounds": [0, 0, 10, 10]}}
    together = _agent_layout(agent_types=[item_type, mover_type],
                             populations=[population, mover_pop], simulation=_sim(run_len))
    env_together = _run(together, run_len, seed=seed)
    t_together = float(_pop(env_together, pop_name).entered[0])

    assert t_together == t_alone   # bit-identical, not approx
