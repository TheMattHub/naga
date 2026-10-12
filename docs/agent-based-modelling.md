# Agent-based modelling

Populations of agents, each running its own small statechart, alongside the ordinary block flow
chart and system dynamics -- the same model file, the same `run_model`. One `AgentSystem` process runs a whole population (not one SimPy process per
agent), so a 10,000-agent SIR epidemic over 1,000 time units is a few thousand events, under a
second.

## Agent types and populations

An **agent type** (`agent_types`) declares the states, transitions, parameters and attributes every
agent of that kind shares; a **population** (`populations`) is *N* agents of one type, with how
many start in which state:

```jsonc
"agent_types": [{"name": "Person", "parameters": {"beta": 0.3, "gamma": 0.1},
  "attributes": [{"name": "susceptibility", "init": "UNIF(0.5, 1.5)"}],
  "states": [{"name": "S"}, {"name": "I", "color": "RED"}, {"name": "R"}],
  "initial_state": "S",
  "transitions": [
    {"from": "S", "to": "I", "rate": "self.susceptibility * beta * COUNT(\"People\", \"I\") / COUNT(\"People\")"},
    {"from": "I", "to": "R", "timeout": "EXPO(1 / gamma)"}]}],
"populations": [{"name": "People", "type": "Person", "count": 10000,
  "initial": [{"state": "I", "count": 10}]}]
```

`parameters` are the type's own named constants (an expression scope's locals, like a submodel's
parameters); `attributes` are per-agent numeric columns, each initialised once per agent from `init`
(the agent's own random stream, so two runs with the same seed draw the same values). `self.<attr>`
reads an agent's own row; `self.state`/`self.id` are read-only.

A population with attributes also gets an `agent_attributes` results frame: one row per
(population, attribute, state-or-`"All"`), with End Mean/P05/P50/P95/Min/Max over the agents alive at
the end of the run and a Time-Avg Mean -- the exact time average of the population-wide mean,
piecewise-constant between agent events (the same treatment `agent_summary`'s own Avg Count gets),
computed for the `"All"` row only. KPI keys `agent/<population>/<attribute>/<state>/<column>`; the
"Agent attributes" export sheet.

### Populations from data

`count` may name a model [table](model-file-format.md#tables) instead of a literal number:

```jsonc
"tables": [{"name": "People", "keys": [0, 1, 2],
  "columns": ["age"], "values": [[34], [61], [8]]}],
"agent_types": [{"name": "Person", "attributes": [{"name": "age", "init": {"column": "age"}}],
  "states": [{"name": "S"}]}],
"populations": [{"name": "People", "type": "Person", "count": {"table": "People"}}]
```

`{"table": "<name>"}` is the table's ROW COUNT: one agent per row, row *i* for agent *i*. An
attribute's `init` may then read `{"column": "<col>"}` instead of an expression -- row *i* of the
POPULATION's own table for agent *i*, straight from the data, no random draw. `{"table": "<name>",
"column": "<col>"}` is instead the SUM of that column, for a table whose rows are groups (a
headcount by region) rather than individual agents -- a group table like this has no row-per-agent
attribute init, since a row is no longer one agent. `max_count` (births and deaths, below) defaults
to the resolved count either way. A spawned agent has no table row of its own: its data-column
attributes start at 0, same as any other attribute's default, unless the spawn action's own `init`
sets them. Python: `Population("People", "Person", count={"table": "People"})`, `Attribute("age",
init={"column": "age"})`.

### Categorical attributes

An attribute is numeric by default; `"kind": "category"` makes it a closed set of names instead --
stored as that name's position in the list (a code, exact in the same column a numeric attribute
uses), so it costs nothing extra to keep:

```jsonc
{"name": "colour", "kind": "category", "categories": ["red", "blue", "green"], "init": "\"red\""}
```

`init` is a quoted category name, `{"column": "<col>"}` (a table column, holding the CODE --
tables stay numeric-only, the contract `TABLE()`/schedules already rely on -- not the name), or any
expression that evaluates to one of the declared names (another `self.<attr>` of the same
categories, `IF(...)`, ...); an unknown name, at compile time for a literal or at run time otherwise,
is `E_AGENT_ATTRIBUTE`. `self.<attr>` reads back the NAME, so `self.colour == "red"`,
`!=` and `in ("red", "blue")` work exactly as `self.state == "<name>"` already does -- no new
grammar -- and the `==`/`!=`-to-a-literal form compiles to a fast code comparison over the whole
population (AB-4) the same way a state comparison does. A `set` action's `expr` must land on one of
the categories too (another categorical attribute of the same list, most simply); arithmetic on a
category, or an expression that lands on something else, raises the same way. `SUM`/`MEAN` refuse a
categorical attribute (`E_AGENT_ATTRIBUTE`: they average codes, which is not a number worth having);
count by category instead:

```text
COUNT("People", "colour", "red")            -- agents whose colour is "red"
COUNT("People", "I", "colour", "red")       -- also in state "I"
```

`agent_attributes` reports a categorical attribute as one row per (population, attribute,
state-or-`"All"`, category) instead, "End Mean" holding that category's SHARE (fraction) of the
group instead of a percentile -- the other stat columns and Time-Avg Mean stay NaN. KPI keys fold the
category in: `agent/<population>/<attribute>=<category>/<state>/End Mean`.

## States and transitions

A **transition** fires on exactly one trigger:

| Trigger | Meaning |
|---|---|
| `timeout` | a duration expression, drawn once on entering the source state |
| `rate` | a hazard rate per agent per time unit (see "Rate forms" below) |
| `condition` | a boolean, re-checked after every agent event (add `"scan": <interval>` to also poll on a grid); it cannot draw random numbers -- use a `rate` |
| `message` | a name (`""` matches any) delivered by a `send` action, a SEND block, or `move_to`'s own `"arrived"` |

A transition may add a `guard` (checked when the trigger fires; false consumes a timeout, redraws a
rate, or just holds a condition) and `actions` -- `Action.set`, `.send`, `.enter`, `.transfer`,
`.signal`, `.move_to`. On a firing: the source's `exit` actions, the transition's own actions, the
move, the target's `entry` actions, then the target's own transitions are armed -- so an entry
action can set something an outgoing rate reads.

## Rate forms: shared, own, product

A `rate` expression is classified once, when the model is built:

* **shared** -- no `self` (`COUNT("People", "I") / COUNT("People")`): the same hazard for every
  agent in the state, changing only when the shared part changes.
* **own** -- only `self`, parameters, constants and a short list of pure functions (`min`, `max`,
  `abs`, `IF`, ...): a per-agent weight, constant for that agent's whole time in the state.
* **product** -- `own * shared` or `own / shared` (either order): the common case, `self.
  susceptibility * beta * COUNT(...)` above is one.

Anything else -- a rate that mixes `self` into the shared part some other way, or varies
continuously with time (a bare `TNOW`, a linear `RAMP`) -- is refused (`E_AGENT_RATE_FORM`) with a
hint to write it as `own * shared`. The reason is exactness: each agent's hazard integrates to a
closed form between the shared rate's change points (Anderson's modified next-reaction method),
so the runtime never redraws every agent's clock when only one of them fires --
O(log N) per event, not O(N).

## Messages

`Action.send(message, to, payload=None)` (or a `SEND` block) queues one message per recipient on a
single FIFO, delivered synchronously as soon as the sender's own firing (or block) finishes, before
the next tied event. Delivery fires the first `message` transition out of the recipient's CURRENT
state whose name matches (`""` matches any) and whose guard holds, with `payload` readable in that
guard and its actions; otherwise the message is dropped and counted (`agent_summary`'s "Dropped
Messages"). `to` is an agent id, `{"population": p, "where": cond}`, or `{"population": p, "random":
true}`.

Inside a `where` (of a send, a `remove`, a `link`), `self` is the candidate and `sender.<attr>` the
agent doing the sending (`sender.x`, `sender.y`, `sender.id`, `sender.state`, its attributes); it is
refused anywhere else, and a `where` still may not draw. With `"random": true` the one candidate is
drawn from the SENDER's stream, which makes a one-to-one claim -- a wolf eating ONE sheep on its own
cell:

```json
{"remove": {"population": "Sheep", "random": true,
            "where": "(self.x - sender.x) * (self.x - sender.x) + (self.y - sender.y) * (self.y - sender.y) < 0.25"}}
```

A removed sheep is gone for the next wolf, so two wolves never eat the same one. A `where` that is
one comparison (as above) runs in numpy; `and`/`or` fall back to a per-candidate loop.

`Action.enter(block, e_type=None)` spawns a DES entity with `entity.agent` set to the sending
agent, so a block downstream can read whose agent created it. `Action.transfer(level, amount, mode)`
moves an SD level (conservation checked exactly as the TRANSFER block's own instant branch).
`Action.signal(name)` raises a DES signal like the SIGNAL block.

## Space, movement, NEAR

An agent type's `space` is `{"kind": "continuous", "bounds": [x0, y0, x1, y1]}` or `{"kind":
"network"}` (the model's own `network` section); `speed` is an expression evaluated once per
`Action.move_to(target)` call. Position is stored as one linear motion segment per agent (`x0, y0,
x1, y1, t0, t1`), so a frame at any instant is exact with no per-tick update; on a network, the
segment follows the real polyline (`Network.path`); playback draws the token along the straight chord
between departure and arrival. Arrival delivers an `"arrived"` message -- no new trigger kind, just
the usual message machinery -- before any other event tied at that instant.

`self.x` and `self.y` read an agent's current position (read-only, any space, unless the type
declares attributes with those names). `NEAR(pop, r[, state])` and `NEAREST(pop[, state])` read `self`'s position against a
`scipy.spatial.cKDTree` of *pop*'s current positions, built at most once per distinct simulation
instant (repeat queries at the same time reuse it).

### Neighbour aggregates

`NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX(pop, attr, r[, state])` reduce an attribute over the same
neighbourhood `NEAR` counts: alive agents of *pop* within `r` of `self`, inclusive, self excluded
when *pop* is self's own population, optionally restricted to `state`. `KNN_SUM/KNN_MEAN(pop, attr,
k[, state])` reduce over the `k` nearest instead, the state filter applied *before* the k are chosen;
ties at the cutoff distance break by ascending agent index, not tree-traversal order. An empty
neighbourhood is `0` for SUM/MEAN (no agents to add or average) and `NaN` for MIN/MAX (no identity
element). The attribute must be numeric -- a categorical one is `E_AGENT_ATTRIBUTE`, the same rule
`SUM`/`MEAN` already enforce; use `COUNT(pop, attr, value)` to count a category instead.

```text
NEAR_MEAN("Cars", "speed", 50)             -- average speed within 50m, self excluded
KNN_SUM("Cars", "load", 3, "Loaded")       -- total load of the 3 nearest loaded cars
```

## Grid space and patches

A grid space divides the plane into `w x h` square cells, NetLogo style:

```json
"space": {"kind": "grid", "size": [50, 50], "cell": 1, "torus": true,
          "neighbourhood": "moore",
          "patches": [{"name": "food", "init": "TABLE(\"food0\", cx, cy)",
                       "diffuse": 0.5, "evaporate": 0.1}],
          "patch_tick": 1}
```

* **Cells.** Cell `(cx, cy)` covers `[cx * cell, (cx + 1) * cell)` along x, and the same along y.
  Agents always sit at a cell centre, so several agents can share a cell. There is no per-cell
  capacity; use `CELL_COUNT` in a guard if you need one. `NEAR`, `KNN_*` and playback work as in a
  continuous space. A population's placement is `{"kind": "random"}` (a uniform cell drawn from the
  agent's own stream) or `{"kind": "table", "points": [[cx, cy], ...]}`.
* **Neighbourhood.** `"moore"` (default) is the 8 surrounding cells and `"von_neumann"` the 4 sharing
  an edge. The agent's own cell is never part of it. On a torus the edges wrap; on a bounded grid a
  cell at the edge simply has fewer neighbours.
* **Moves are instant** (no travel time and no `"arrived"` message):
  `{"move_to": [cx, cy]}` or `{"move_to": "AGENT(\"Pop\", i)"}` (that agent's cell);
  `{"step": [dx, dy]}` (integers or expressions; a step off a bounded grid leaves the agent where it
  is); `{"move_random": "any"}` moves to a neighbourhood cell drawn uniformly from the agent's own
  stream; `{"move_random": "free"}` does the same among cells holding no agent of its own population.
  With no free cell the agent stays and draws nothing (Schelling's move).
* **Counting.** `NEIGHBOURS(pop[, state])` counts *pop*'s agents in self's neighbourhood.
  `NEIGHBOUR_SUM/NEIGHBOUR_MEAN(pop, attr[, state])` reduce an attribute over the same agents
  (`0` if none). `CELL_COUNT(pop[, dx, dy])` counts one cell, self's own by default and self
  included, and is `0` off a bounded grid. *pop* must be on a grid of the same size.
* **Patches** are per-cell numbers shared by every population of the type. `init` is a number or an
  expression of `cx`, `cy` (`TABLE`, parameters and draws allowed; no `self`).
  `PATCH("food"[, dx, dy])` reads self's cell or one offset from it (`0` off a bounded grid).
  `{"target": "patch.food", "expr": "PATCH(\"food\") + 1"}` writes self's cell.
* **Diffusion and evaporation** run at every `patch_tick` (`k * patch_tick`, after the agents'
  own ticks at that instant). Each cell keeps `1 - diffuse` of its value and hands `diffuse / 8`
  (Moore) or `diffuse / 4` (von Neumann) to each neighbour. On a bounded grid, a share with no cell
  to go to stays home, so the total is conserved. Evaporation then multiplies every cell by
  `1 - evaporate`.
* **Regrowth** (`"grow": {"rate": r, "max": m}`) runs last: a cell below `m` gains `r`, clipped at
  `m`; one at or above it is left alone. Without `max`, each cell grows back to its own initial
  value (Sugarscape's capacity landscape). NetLogo's grass countdown is `rate 1, max 30`, with
  "green" meaning `PATCH("grass") >= 30`.
* **Synchronous ticks** read the tick-start patches, like every other read. Positions and cell
  counts are live, so an agent sees the moves made earlier in the same tick.
* A grid call in a rate is `E_AGENT_RATE_FORM`: neighbours change between events. Use a condition
  or a tick transition instead. Grid features without a grid space are `E_AGENT_GRID`, and patch
  mistakes are `E_AGENT_PATCH`.
* Playback draws the agents only, not the patches. After a run, read a patch with
  `env.ctx.agent_system.patch_values("<type>", "<patch>")`, an `(h, w)` array indexed `[cy, cx]`.

```python
from nagasim.core import agents

walker = agents.AgentType(
    "Ant", states=["S"], tick={"dt": 1},
    space=agents.Space("grid", size=[50, 50], torus=True,
                       patches=[{"name": "trail", "diffuse": 0.2, "evaporate": 0.05}],
                       patch_tick=1),
    on_tick=[agents.Action.move_random(),
             agents.Action.set("patch.trail", 'PATCH("trail") + 1')])
```

## Links and networks

Agents of one population can be linked to each other, with or without a space. A population's
`network` draws the initial links at run start:

```json
{"name": "People", "type": "Person", "count": 500,
 "network": {"kind": "small_world", "k": 4, "beta": 0.1}}
```

| `kind` | Keys | Links |
|---|---|---|
| `random` | `p` or `mean_degree` | Erdos-Renyi G(n, p): each pair linked with probability `p` (`mean_degree` = `p (n - 1)`). |
| `small_world` | `k` (even), `beta` | Watts-Strogatz: a ring where each agent links to its `k` nearest, then each link rewired to a random agent with probability `beta` (`beta = 0` is the plain ring). |
| `scale_free` | `m` | Barabasi-Albert: agents `0..m-1` start unlinked, each later agent links to `m` earlier ones picked in proportion to their degree; `m (n - m)` links. |
| `table` | `table` | The rows of a model table with columns `from` and `to` (agent indices). |
| `none` | | No initial links; link actions add them. |

* Links are undirected unless `"directed": true` (`random` and `table` only). A directed link
  `i -> j` counts for `i`: `LINKED`, `to: "links"` and `IS_LINKED` follow out-links.
* The network has its own random stream, so adding one never moves any agent's draws. The same
  seed gives the same network.
* Links join agents of **one** population. A link between two populations is refused at run time.
* **Reading links.** `LINKED("People"[, "state"])` counts self's linked neighbours (in that state).
  `LINK_SUM("attr"[, "state"])` and `LINK_MEAN("attr"[, "state"])` reduce an attribute over them
  (`0` if none). `IS_LINKED(AGENT("People", i))` tests one link. Like the grid calls, these are
  per-agent: they are allowed in a condition, a guard, an action or a tick transition, not in a
  rate (`E_AGENT_RATE_FORM`).
* **Sending.** `"to": "links"` sends to every linked neighbour, in index order.
  `{"links": true, "where": "...", "random": true}` filters them or picks one, like a
  population target.
* **Changing links.** `{"link": <target>}` and `{"unlink": <target>}` take any send target
  (`"links"` included, so `{"unlink": "links"}` drops them all). Self is skipped.
* **Births and deaths.** A removed agent loses all its links. A newborn starts unlinked, or linked
  to the agent that spawned it with `"link_on_spawn": true`.
* **Synchronous ticks** read the tick-start links, like every other read. Sends and link actions
  use the live links.
* **Playback** draws a thin line between two linked agents on the floor plan (2D only), for the
  agents under the trace cap. `system.links_of("People")` returns the live links after a run.
* Mistakes in `network` are `E_AGENT_NETWORK`.

An SIR spreading along a scale-free network: an infected agent recovers after an exponential
delay and, as it does, infects every susceptible neighbour (the others drop the message):

```python
person = agents.AgentType(
    "Person", states=["S", "I", "R"], initial="S",
    transitions=[agents.Transition("S", "I", "message", "infect"),
                 agents.Transition("I", "R", "timeout", "EXPO(5)",
                                   actions=[agents.Action.send("infect", "links")])])
people = agents.Population("People", "Person", 1000, {"I": 5},
                           network=agents.Network("scale_free", m=2))
```

## Heading and steering

A continuous-space agent type gets a built-in `self.heading` (degrees, `0` = east,
counter-clockwise). It reads and writes like any other attribute, but is not one -- it needs no
`Attribute` declaration and cannot be named as one:

```text
"heading" is 0 = east, counter-clockwise around the plane.
NetLogo's own turtles use 0 = north, clockwise -- the OPPOSITE convention. Porting a NetLogo
model: heading_here = (90 - heading_netlogo) mod 360.
```

Four actions move or turn along it, all needing `"space": {"kind": "continuous", ...}`:

| Action | Effect |
|---|---|
| `{"forward": "<distance>"}` | Move `<distance>` along `self.heading`. |
| `{"turn": "<degrees>"}` | Rotate `self.heading` by `<degrees>`, left (counter-clockwise) positive. |
| `{"face": <target>}` | Point `self.heading` at a literal point `[x, y]`, an expression giving one, or another agent (`'AGENT("Pop", i)'`). |
| `{"random_walk": {"step": "<d>", "turn_sd": "<sd>"}}` | Turn by a draw from `NORMAL(0, sd)` on self's own stream, then move `step`. |

`turn`, `face` and a `random_walk`'s turn never touch position, and run instantly whether inside a
tick or not. `forward` (and the forward half of `random_walk`) picks its own timing from where the
action sits:

* **Inside `on_tick`** it is an INSTANT jump (NetLogo style: the tick is already the model's own
  discrete step), with no travel time and no message.
* **Anywhere else** (a transition, a state's entry/exit) it is a TIMED move at the type's `speed`,
  landing exactly like `move_to` -- same segment, same `"arrived"` message on arrival, so an
  `Action.forward` can sit next to an `Action.move_to` in the same transition's actions with no
  special-casing.

```python
ant = agents.AgentType(
    "Ant", states=["S"], tick={"dt": 1},
    space=agents.Space("continuous", [0, 0, 100, 100]),
    on_tick=[agents.Action.random_walk(step=1, turn_sd=15)])
```

**Edges.** A continuous space's `bounds` take an `"edge"`, which only `forward`/`random_walk` obey
(`move_to`, `turn` and `face` never fold a position):

| `edge` | Behaviour |
|---|---|
| `"none"` (default) | Unbounded -- the same as before this feature, so an existing model is untouched. |
| `"clamp"` | Stop at the wall. |
| `"wrap"` | A torus: reappear on the opposite side, exact modulo arithmetic. |
| `"bounce"` | Reflect off the wall, mirroring `self.heading`; exact for any distance (even several widths) with no simulated sub-steps. |

**Steering.** `NEAR_HEADING(pop, r[, state])` is the CIRCULAR mean heading of the same
neighbourhood `NEAR` counts -- averaging 350 degrees and 10 degrees gives 0, not the 180 a plain
mean would. It composes with the existing `NEAR_SUM`/`NEAR_MEAN` for a boids-style flock, with no
separate flocking engine:

```python
# Alignment: turn toward the neighbourhood's average heading.
agents.Action.turn('(NEAR_HEADING("Boids", 10) - self.heading + 540) % 360 - 180')
# Separation: steer away from a neighbour that is too close (NEAR > 0 within 2).
# Cohesion: face the average position -- approximate it with NEAR_MEAN of "x"/"y" attributes,
# or track a nearby flockmate with `face: 'AGENT("Boids", j)'`.
```

(The alignment turn above takes the shortest way around the circle: `(target - self + 540) % 360 -
180` is in `(-180, 180]`, so a flock never spins the long way round to align.)

`SIN`, `COS`, `TAN` and `ATAN2(y, x)` work in radians, like Python; `RAD`/`DEG` convert and `PI()`
is pi, so a heading's unit vector is `(COS(RAD(self.heading)), SIN(RAD(self.heading)))`. A flock's
polarisation is then `SQRT(MEAN("Boids", "ux") ** 2 + MEAN("Boids", "uy") ** 2)`.

## Births and deaths

Two actions change a population's size during the run:

```json
{"spawn": "Cells", "count": 1, "state": "Young", "init": {"energy": "self.energy / 2"}}
{"remove": "self"}
```

`spawn` adds `count` agents (default 1) in `state` (default the type's initial state). A newborn
evaluates its own attribute inits first; then each `init` expression is evaluated in the SPAWNER's
scope (`self` is the parent) and overwrites that attribute; then its entry actions run and its
transitions are armed. `remove` takes `"self"` or any send target (an index, `AGENT("Pop", i)`,
`{"population": "Pop", "where": "..."}`); removing the agent that is firing ends its firing there.
Messages still queued to a removed agent are dropped and counted in "Dropped Messages".

A population holds at most `max_count` live agents (default: its `count`, so set it whenever a model
spawns); a spawn beyond it stops the run with `E_POPULATION_FULL`. Python: `Action.spawn(pop, count,
state, init)`, `Action.remove(target)`, `Population(..., max_count=500)`.

* COUNT, SUM/MEAN, NEAR/NEAREST, population sends, `agent_summary` (Avg Count, Final) and the
  playback tokens all see live agents only; a removal counts as leaving its state (Avg Sojourn), a
  birth is not an Entry.
* A removed agent's slot is reused by a later birth, but never its random stream: the s-th birth of a
  population draws from stream key `count + s`, so a newborn draws
  exactly as an initial agent with that index would.
* A reference kept to a removed agent (`entity.agent`, an `AGENT()` value) raises an error when used,
  even if its slot now holds a newborn; test it with `ALIVE(ref)` first.
* Births and deaths start with the run: a spawn or remove in an entry action run while the
  populations initialise is an error.

## Tick-based models

Cellular automata, Schelling, Sugarscape and boids are written as "every step, every agent does X".
Give the type (or one population) a `tick`, and its agents run `on_tick` actions and `tick`
transitions at every step:

```json
{"name": "Cell", "states": [{"name": "S"}], "attributes": [{"name": "v", "init": "self.id"}],
 "tick": {"dt": 1, "activation": "synchronous"},
 "on_tick": [{"target": "self.v", "expr": "AGENT(\"Ring\", (self.id + 2) % 3).v"}],
 "transitions": [{"from": "S", "to": "S", "tick": "self.v > 100"}]}
```

Tick k runs at `phase + k * dt` (`phase` defaults to 0 and must be below `dt`), for every tick after
the run start, computed from k so a long run never drifts. A population's `tick` keys override its
type's. At each tick, each agent alive at its start runs `on_tick`, then takes the first `tick`
transition out of its current state whose condition holds (`""` fires every tick; `guard` and
`actions` work as usual). `activation` decides what an agent sees of the others:

* `synchronous` (default): everyone reads the state as of the tick start -- `self.x`, `AGENT().x`,
  `self.state`, COUNT and SUM/MEAN of the population -- and the writes all land at the end, like a
  cellular automaton. Every agent's `on_tick` runs first, then every agent's tick transitions. An
  agent's own second action also reads the tick-start value, so compute a new value in ONE
  expression, or once in a `let` (below).
* `sequential`: index order, each agent seeing the writes of those before it.
* `random`: a fresh random order every tick, drawn from the population's own tick stream
  (keyed by the population and k), so replications and scenarios keep
  common random numbers.

On a 3-agent ring where each agent copies its left neighbour, `v = [10, 20, 30]` becomes `[30, 10,
20]` after one synchronous tick (a rotation) but `[30, 30, 30]` after one sequential tick. Ticks share
the clock with everything else: at an instant, the event-driven transitions settle first, then the
ticks run, then conditions, rates and SD levels see the result at that same instant. A tick agent
may send messages, spawn, remove and move; an agent born during a tick first acts at the next one.
A synchronous `on_tick` made only of `self.x` sets in the numpy subset runs column-wise. Python:
`AgentType(..., tick=Tick(1, "random"), on_tick=[...])`, `Population(..., tick=Tick(0.5))`,
`Transition("S", "T", "tick", "self.v > 100")`. A bad `tick`, or tick behaviour on a population
with no `dt`, is `E_AGENT_TICK`.

**Tick-local values.** `"let": {"name": "<expression>", ...}` on the type is evaluated in order,
once per agent per tick, before its `on_tick`; `on_tick` and the tick transitions read each as a
bare name, and a later `let` may read an earlier one. A draw in a `let` is ONE draw from the agent's
own stream, shared by every action of that tick, under any activation:

```json
"let": {"v1": "min(self.v + 1, vmax)", "vn": "IF(UNIF(0, 1) < p and v1 > 0, v1 - 1, v1)"},
"on_tick": [{"step": ["vn", 0]}, {"target": "self.v", "expr": "vn"}]
```

A `let` name must be new (not a parameter, variable or reserved name), else `E_AGENT_TICK`.

## Python behaviour

When a rule is easier to write as code than as actions, an agent type can carry `python`: plain
`def` statements. The action `{"python": "<function>"}` calls `function(agent, api)`. It works
anywhere an action list does: `on_tick`, a state's entry or exit actions, or a transition's actions.

```json
{"name": "Walker", "states": [{"name": "S"}], "attributes": [{"name": "x"}, {"name": "v"}],
 "parameters": {"n": 50}, "tick": {"dt": 1},
 "on_tick": [{"python": "step"}],
 "python": "def step(agent, api):\n    agent.set('x', agent.get('x') + api.normal(0, 1))\n    left = api.agent('Ring', (agent.id + 1) % api.params['n'])\n    agent.set('v', left.get('v'))\n"}
```

**Running it needs your consent.** A model file cannot switch Python on. Its code runs only when
the process running the model has the environment variable `NAGASIM_ALLOW_AGENT_PYTHON=1`. That
means your shell, or `os.environ` in your own
script. Without it the run stops with an error that says so, and validation only parses the code. Set it
only for models whose code you trust. With consent, the code has the same standing as a
`custom_logic.py` class or a plugin: Python cannot sandbox code running in its own process.

What the code can reach:

| `agent` (this agent) | |
| --- | --- |
| `agent.id`, `.population`, `.type`, `.state`, `.pos`, `.alive` | read |
| `agent.get("attr")` | an attribute, `state`, `id` or `heading`; a synchronous tick reads its start |
| `agent.set("attr", value)` | this agent only, checked like a set action (categories, numbers) |

| `api` | |
| --- | --- |
| `api.now`, `api.payload`, `api.params` | the clock, a message's payload (else None), the type's parameters (read-only) |
| `api.let(name)` | a `let` value of this tick, the same value (draw included) the expressions read |
| `api.random()`, `.uniform(a, b)`, `.normal(m, sd)`, `.integers(lo, hi)` | draws from the agent's own stream, exactly as `RAND`/`UNIF`/`NORM` do |
| `api.count(pop[, state])`, `.sum(pop, attr[, state])`, `.mean(...)` | as COUNT/SUM/MEAN |
| `api.agent(pop, i)`, `.near(pop, r[, state])`, `.links([state])` | other agents as read-only views, ascending index |
| `api.send(message, to, payload=None)` | `to`: an agent or a list of agents |
| `api.spawn(pop, count=1, state=None, init={attr: value})`, `api.remove(target=None)` | births and deaths (`None`: this agent) |

`agent` and `api` live for one call: kept across calls (a mutable default, say), a write, draw, send
or remove through them raises.

The code is checked at load. Only `def` statements may sit at the top level. Imports, `global`,
`class`, `yield`, `with`, decorators and `_`-prefixed names are refused. Attributes are limited to
the two views, `math` and plain list/dict/str methods. Builtins are limited to `abs`, `min`, `max`,
`len`, `range`, `sum`, `sorted` and similar. A problem is `E_AGENT_PYTHON` with its line number.
An exception while running stops the run and names the type, the function, the agent, the time
and the line. A Python on_tick draws exactly what the equivalent declarative one does, so common
random numbers and bit-for-bit comparisons hold. Each call adds about 1 microsecond. Scenarios and
data bindings cannot override `python`, and a library cannot carry it.

## Playback

In a recorded run, an agent with a `space` is a small token coloured by its
current state, alongside entity tokens and vehicles (`Timeline.state_at()["agents"]`); only the
first `simulation.agents.trace_agents` agents of a population are traced (default 200), the same
budget rule entity tokens use for a crowded queue.

## Performance notes

* Storage is columnar numpy per population (about 100 B/agent plus 1 KB once an agent's random
  stream is created, lazily, on first use) -- there is no second, "vectorised" engine, just this
  storage plus numpy aggregates.
* `COUNT(pop, "state")` against a literal state name is an O(1) counter, not a scan.
* `SUM`/`MEAN` vectorise over numpy when the expression is array-safe, else fall back to a plain
  per-agent loop.
* At most `max(10,000, 100 x agents)` firings (and queued messages) are allowed per simulation
  instant -- past that the run stops with a hint pointing at a condition that stays true across its
  own transition, a zero timeout loop, or two agents messaging each other forever.

## Worked example: SIR

Python classes:

```python
from nagasim.api import run_model
from nagasim.core import agents as A

person = A.AgentType(
    "Person", parameters={"beta": 0.3, "gamma": 0.1}, states=["S", "I", "R"], initial="S",
    transitions=[
        A.Transition("S", "I", "rate", 'beta * COUNT("People", "I") / COUNT("People")'),
        A.Transition("I", "R", "timeout", "EXPO(1 / gamma)")])

layout = A.add({"simulation": {"time_limit": 100}}, person,
               A.Population("People", "Person", 10000, {"I": 10}))
results = run_model(layout, replications=1)
print(results["agent_summary"])   # Avg/Min/Max/Final count and Avg Sojourn per (Population, State)
```

The same model as JSON (the agent tools only ever produce this shape):

```jsonc
{"simulation": {"time_limit": 100},
 "agent_types": [{"name": "Person", "parameters": {"beta": 0.3, "gamma": 0.1},
   "states": ["S", "I", "R"], "initial_state": "S",
   "transitions": [
     {"from": "S", "to": "I", "rate": "beta * COUNT(\"People\", \"I\") / COUNT(\"People\")"},
     {"from": "I", "to": "R", "timeout": "EXPO(1 / gamma)"}]}],
 "populations": [{"name": "People", "type": "Person", "count": 10000, "initial": [{"state": "I", "count": 10}]}]}
```

For R0 = beta/gamma = 2.5 the attack rate (final R / N) matches the closed-form root of
`z = 1 - e^{-R0 z}` within its 95% CI over a handful of replications.

## Agents in submodels

A population and its type are cloned for every submodel instance that uses them, like a level:
inside a definition, write the population as `$People` (`COUNT("$People", "I")`,
`AGENT("$People", i)`, `{"population": "$People"}`, `{"spawn": "$People"}`, a SEND block's `to`),
or name it under the definition's `scoped.populations`. Each instance then gets its own
`<instance>/People` and `<instance>/Person`, with its own random streams, and its arguments reach
the clone through `scoped` overrides (`"parameters.beta": {"$arg": "beta"}`, `"count": {"$arg": "N"}`).

See `nagasim.core.agents` for the full authoring API (`AgentType`, `State`, `Transition`, `Action`,
`Space`, `Population`), [System dynamics](system-dynamics.md) for the level/flow constructs an
agent's `transfer` action moves, and [Security](security.md) for how every file/socket surface a
model can reach is confined.
