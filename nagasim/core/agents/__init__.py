"""Agent-based modelling: authoring classes, validation and the runtime
(PHASE5 AB-1, ARCHITECTURE 5.25, D93/D94; decisions 4 and 6).

AUTHORING (decision 6, Python API first). `AgentType`, `Attribute`, `State`,
`Transition`, `Action` and `Population` are plain records with `to_dict()`
and `from_dict()`; `add(layout, *records)` appends them to the `agent_types`
and `populations` sections. The engine only ever runs the JSON:

    from nagasim.api import run_model
    from nagasim.core import agents
    person = agents.AgentType(
        "Person", parameters={"beta": 0.3, "gamma": 0.1}, states=["S", "I", "R"],
        initial="S", transitions=[
            agents.Transition("S", "I", "rate", 'beta * COUNT("People", "I") / COUNT("People")'),
            agents.Transition("I", "R", "timeout", "EXPO(1 / gamma)")])
    run_model(agents.add({"simulation": {"time_limit": 100}}, person,
                         agents.Population("People", "Person", 1000, {"I": 10})))

RUNTIME (D93). One `AgentSystem` process for every population, the
`LevelSystem` shape: next instant in closed form, `timeout | wake`. Storage is
columnar numpy per population (`state`, `entered`, `epoch` for lazy heap
deletion, one float column per attribute); agent i's stream is
draw call n of agent i is Philox at counter `(0, n, i, 0)` under the
population's key (D107): 4 bytes per agent, no object per agent.

* timeout: `entered + value` on one absolute-time heap;
* rate (Anderson's modified next-reaction method): on entering the state the
  agent draws E ~ Exp(1) from its own stream and is keyed at
  `Lambda(t) + E / w_i` in the (population, transition) heap, `Lambda` the
  integral of the shared rate (piecewise constant between agent events, the
  D60 rule); the heap fires when `Lambda` reaches its smallest key. Exact,
  O(log N) per firing, and CRN-preserving: agent i's draws never depend on the
  others. `Lambda` is re-anchored only when the shared rate CHANGES, so events
  that leave it alone (another population's) leave every time bit-identical;
* condition: re-checked after each event batch (a condition without `self`
  once per transition, one with `self` for the agents in the state: numpy
  over the columns when `vectorise` accepts it, AB-4, else one by one).

WAKE-UPS. Shared rates and conditions are re-evaluated after every agent
event batch and at their CHANGE INSTANTS: the literal STEP/PULSE/RAMP times
(`Expression.instants`, the SD-2 registration the level engine uses), a
literal clock comparison (`TNOW >= 7` wakes at 7; `TNOW > 7` at the next float
after 7) and the knots of every TS() series read. Between instants a shared
rate is then piecewise constant and its hazard exact. A rate that varies
CONTINUOUSLY with time (a bare TNOW, RAMP, a linear TS) is refused
(E_AGENT_RATE_FORM): its hazard would need a quadratic, and the product form is
the contract. Model state that moves outside the agents (LEVEL, AUX, FLOW, NQ,
...) is HELD between agent events unless the transition sets `scan`: an extra
re-check on the grid k * scan (W_AGENT_SCAN otherwise); an ASSIGN writing a
variable wakes the system itself.

Rate forms (D94), classified when built: SHARED (no `self`), OWN (only `self`,
parameters, constants and pure functions: `w_i`, shared part 1) or a PRODUCT
of the two over `*` and `/`; anything else is E_AGENT_RATE_FORM. Ties fire in
(time, population, index, transition) order, after the "arrived" messages of
the arrivals at that instant (#144), at most max(10,000, 100 x agents)
firings and queued messages per instant (DEVLOG #131). A guard that is false when its trigger fires consumes a timeout,
re-arms a rate with a fresh draw (memoryless), and simply holds a condition.
On a firing: the source's exit actions, the transition's actions, the move,
the target's entry actions, then the target's transitions are armed (so an
entry action can set what an own rate reads).

MESSAGES (PHASE5 AB-2, D95). A `send` action or a SEND block queues one message
per recipient on ONE FIFO; it is delivered synchronously at the same instant,
as soon as the firing (or the block) that sent it has finished, before the
next tied event. Delivery fires the first "message" transition out of the
recipient's CURRENT state whose name matches ("" matches any) and whose guard
holds, with `payload` readable in that guard and its actions; otherwise the
message is dropped and counted against that state (`agent_summary` "Dropped
Messages", `AgentSystem.dropped`). A message sent from inside a delivery
queues behind the rest, so a cascade is breadth-first. `enter` spawns a DES
entity into a block with `entity.agent = (population, index)` (the SEND
block's default `to`); `transfer` is an SD level move (`LevelSystem.move`,
the TRANSFER block's instant branch), not an agent or entity operation;
`signal` raises a DES signal like the SIGNAL block.

SPACE AND MOVEMENT (PHASE5 AB-3, ARCHITECTURE 5.25). An agent type's `space`
is `{"kind": "continuous", "bounds": [x0, y0, x1, y1]} | {"kind": "network"}`
(network reuses the model's own `network` section); `speed` is an expression
(self, parameters, constants) evaluated once per `move_to`. Position is a
LINEAR MOTION SEGMENT per agent -- columns `x0, y0, x1, y1, t0, t1` -- so a
frame at any t is exact with no per-tick update; a network mover ALSO keeps
its node path, and `core.network.point_on_path` places it on the real
polyline (still a pure function of the segment, D68). `move_to` (an action)
resolves its target -- a point, a node name, or another agent's current
position/node -- computes the travel time (continuous: distance / speed;
network: `Network.path(...)` length / speed, the cached Dijkstra tree) and
schedules the arrival on its own absolute-time heap (the `_timeouts` shape);
on arrival the agent's position is exact at the destination and an "arrived"
message is delivered (D95's own machinery -- no new trigger kind). `NEAR(pop,
r[, state])` / `NEAREST(pop[, state])` read `self`'s position and a
`scipy.spatial.cKDTree` of *pop*'s positions AT THE CURRENT INSTANT, built at
most once per distinct instant (a one-slot cache: repeat queries at the same t
reuse it). Trace: `agent_state` (every firing, `_take`) and `agent_moved`
(every placement and `move_to`) are emitted only for `index <
simulation.agents.trace_agents` (default 200, D75-style budget); 2D/3D
playback read them through `Timeline.state_at()["agents"]`.

BIRTHS AND DEATHS (PHASE6 C5, D98). The columns are a CAPACITY: slot i is
alive while `state[i] >= 0` (the alive mask; -1 is a dead or never-used
slot), dead slots wait on a free list (a heap, lowest slot first) and the
columns grow x2 up to `population.max_count` (default: its `count`, so a
model without births allocates exactly what it did). `{spawn: "<Pop>",
count?, state?, init?: {attr: expr}}` adds agents; `{remove: <target>}`
("self", or any send target) removes them. A birth's stream is keyed by the
population's BIRTH SERIAL, Philox counter `(0, n, count + serial, 0)`,
never by its slot: a recycled slot never reuses a stream, and a newborn draws
exactly as an initial agent with that index would (placement, attribute
inits, then the arming). A removal bumps `epoch` (its timeouts, rates and a
pending batch go stale), `move_epoch` and the slot's GENERATION, and drops the
messages queued to it (counted against its last state). A stored reference
(`AGENT()`, `entity.agent`) is an `AgentRef`: the (population, index) pair,
tagged with that generation, so one that outlives its agent raises instead of
reaching a stranger (`ALIVE(ref)` tests it first). Every scan -- COUNT, SUM,
MEAN, NEAR/NEAREST, `where` and population sends -- reads live agents only.

COMPOSITE STATES (PHASE6 K1, D136-D139). A state naming a `parent` nests in
it; `state` still holds the LEAF code, and `_Type` precomputes each state's
path, depth, initial leaf and descendant leaves. A transition out of a
composite is live while any leaf under it is active: one epoch row per depth,
so an inner move leaves an outer timeout or rate alone. `_take_nested` runs
UML external semantics (exit innermost first up to the LCA, the actions,
entry outermost first, arming per state) and `history` re-enters a composite
at its recorded child (shallow) or leaf (deep).

TICKS (PHASE6 A1, D99). A type or population may declare `tick: {dt,
activation?, phase?}` (the population's keys override the type's): tick k runs
at `phase + k * dt`, from the integer k (no drift), for every k with that
instant strictly after the run start. Each agent alive at the tick start runs
the type's `on_tick` actions, then the first "tick" transition out of its
current state whose condition ("" = always) and guard hold. Activation:
"sequential" (index order), "random" (a permutation of the live indices from
`default_rng(stream_seed(seed, pop, k, hash("tick")))`, D107), or
"synchronous" (default): two passes in index
order, every on_tick first, then every tick transition, and every EXPRESSION
read of the population (self.x, self.state, AGENT().x, COUNT, SUM/MEAN, the
numpy paths) sees the tick-start snapshot `_Population.front` while writes go
to the live columns -- double buffering, committed by dropping the snapshot.
An agent born during the tick reads live and first acts at the next one.
Ordering at an instant: the event-driven batches settle first, then the due
ticks run (population order), then the instant settles again -- so conditions,
shared rates and SD levels (#140) see a tick's result at the tick's instant.

GRID SPACE AND PATCHES (PHASE6 A4, D102). `space: {"kind": "grid", "size":
[w, h], "cell"?: 1, "torus"?: false, "neighbourhood"?: "moore"|"von_neumann",
"patches"?: [{name, init?, diffuse?, evaporate?}], "patch_tick"?: dt}` is a
continuous space [0, w*cell] x [0, h*cell] whose agents only ever sit at cell
CENTRES: the AB-3 segment columns hold them (a zero-length segment), so
NEAR, playback and the trace need nothing new. Cell (cx, cy) = floor(x /
cell); several agents per cell, no per-cell capacity. Placement: a uniform
cell (one flat-index draw from the agent's stream) or a table of cells.
Moves are INSTANTANEOUS (no "arrived" message): `move_to` a cell [cx, cy]
(wrapped on a torus, an error off a bounded grid) or another agent's cell,
`step: [dx, dy]` (off the edge of a bounded grid: the agent stays), and
`move_random: "any"|"free"` (a uniformly drawn neighbourhood cell, from the
agent's own stream; "free": holding no live agent of its own population; no
candidate: it stays and draws nothing). A per-(state, cell) count array per
population, kept by every placement, move, state change and death, answers
NEIGHBOURS (the Moore 8 or von Neumann 4 cells around self's, own cell
excluded, each cell once) and CELL_COUNT (one cell, self included) in
O(cells); NEIGHBOUR_SUM/MEAN scan the population. A patch is a float (h, w)
array per agent TYPE (its populations share it), initialised at start from a
number or an expression of cx, cy (row-major, its own stream (seed, type,
patch, tag)), read by PATCH("name"[, dx, dy]) (off a bounded grid: 0) and
written by a `patch.<name>` set at the agent's own cell. At each
`patch_tick` (k * dt, after the population ticks of that instant) every patch
diffuses then evaporates, NetLogo's rules: a cell hands `diffuse` x its value
out in equal 1/8 (Moore) or 1/4 (von Neumann) shares, a share with no cell to
go to (non-torus edge) staying home, so the total is conserved; evaporation
multiplies by (1 - evaporate). A synchronous tick's reads see the tick-start
patches (a snapshot, D99's rule); positions and cell counts are live, as in
A1. Playback shows the tokens only (no patch layer); after a run,
`AgentSystem.patch_values(type, name)` returns a patch.

LINKS AND NETWORKS (PHASE6 A5, D103). Agents of ONE population may be linked:
`population.network: {kind: "random" (p | mean_degree) | "small_world" (k,
beta) | "scale_free" (m) | "table" (table: "from"/"to" columns) | "none",
directed?, link_on_spawn?}` draws the initial links at start from the
population's network stream `[seed, hash(pop), 0, hash("network")]`.
Adjacency sets are the mutable truth, a CSR copy (rebuilt lazily after a
change) serves reads; `{link: <target>}`/`{unlink: <target>}` change them,
a death drops them, a newborn starts unlinked (or linked to its spawner).
`LINKED(pop[, state])`, `LINK_SUM/LINK_MEAN(attr[, state])`,
`IS_LINKED(ref)` read them; `to: "links"` / `{links: true, where?,
random?}` sends along them. A synchronous tick's reads see its start links.

HEADING AND STEERING (PHASE6 A6, D104). A continuous-space agent type gets a
built-in `self.heading` (degrees, 0 = east, counter-clockwise -- NetLogo's own
turtles use 0 = north, clockwise, the opposite convention), read through
`_Population.read` like `state`/`id` but writable (`set self.heading`), live
during a tick like position (no snapshot). `{forward: <distance>}` moves along
it: an INSTANT jump inside on_tick (NetLogo style, the tick already is the
model's discrete step) or, outside one, a TIMED segment at `speed` ending in
an "arrived" message -- forward outside a tick literally IS a `move_to`
towards the computed endpoint, reusing D95's own arrival machinery.
`{turn: <degrees>}` rotates it (left positive), `{face: <target>}` points it
at a point or another agent, both instant. `{random_walk: {step, turn_sd?}}`
turns by a normal draw (mean 0, sd `turn_sd`) from the agent's OWN stream then
forwards -- the same instant/timed split as `forward`. A space's `bounds` gets
an `edge`: "none" (default, unbounded, `move_to`'s own convention, unaffected),
"clamp" (stop at the wall), "wrap" (torus, exact modulo) or "bounce" (an exact
closed-form triangle-wave fold per axis -- any distance, even several widths,
reflects correctly with no per-step iteration); only `forward`/`random_walk`
fold, `move_to`/`turn`/`face` do not. `NEAR_HEADING(pop, r[, state])` is the
CIRCULAR mean heading of the same neighbourhood `NEAR` counts (350 and 10
average to 0, not 180); composed with the existing `NEAR_SUM`/`NEAR_MEAN` it
gives boids-style separation/alignment/cohesion with no flocking engine.

PYTHON BEHAVIOUR (PHASE6 A7, D105). A type's `python` holds def statements;
the action `{python: "<fn>"}` calls fn(agent, api) wherever an action may sit.
It runs only when the caller's environment sets NAGASIM_ALLOW_AGENT_PYTHON=1;
validation merely parses it. `AgentView`/`AgentApi` are the whole surface.
numpy + scipy.spatial + stdlib.

The package, bottom up: `common` (vocabulary, stream seeds, rate forms),
`authoring` (the records), `python` (agent Python: the consent gate and the
one exec), `validation` (`validate_structured`), `space` (grid, links and the
AgentSystem space methods), `compile` (action closures, `vectorise`, `_Type`)
and `runtime` (`AgentSystem`). Names are re-exported here, so importers use
`nagasim.core.agents` alone; a monkeypatch targets the module that defines
the name.
"""
from nagasim.core.agents.authoring import (Action, AgentType, Attribute, Network, Population, Space,
                                           State, Tick, Transition, add)
from nagasim.core.agents.common import (ACTIVATIONS, MAX_FIRINGS_PER_INSTANT, NETWORK_KINDS,
                                        TRIGGERS, _GRID_CALLS, _HELD_CALLS, _LINK_CALLS, _PURE,
                                        _SAMPLER_CALLS, rate_parts, timing)
from nagasim.core.agents.compile import _PER_AGENT, _VEC_CALLS, _run_vec, compile_target, vectorise
from nagasim.core.agents.python import (PYTHON_CONSENT, AgentApi, AgentView, PythonRefused,
                                        _load_python)
from nagasim.core.agents.runtime import AgentRef, AgentSystem, _Population, build_agent_system
from nagasim.core.agents.space import _Links
from nagasim.core.agents.validation import (bad_aggregates, entered_blocks, literal_problems,
                                            rewrite_state_literal, rewrite_state_references,
                                            target_problems, validate_structured)

__all__ = ["AgentType", "Attribute", "State", "Transition", "Action", "Population", "Space",
           "Tick", "AgentRef", "add", "TRIGGERS", "ACTIVATIONS", "MAX_FIRINGS_PER_INSTANT", "AgentSystem", "build_agent_system",
           "rate_parts", "timing", "validate_structured", "bad_aggregates", "compile_target",
           "target_problems", "literal_problems", "rewrite_state_literal", "rewrite_state_references",
           "vectorise", "Network", "NETWORK_KINDS"]
