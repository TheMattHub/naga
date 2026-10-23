# Layout, paths and vehicles

When distance matters -- a warehouse, a yard, a hospital corridor, an AGV aisle -- guessing a
travel time is the weakest part of the model. Put the assets where they really are and let the
geometry produce the time instead.

Three pieces, and deliberately no more: a **path network**, **mover fleets** that drive on it,
and **conveyors**.

## The path network

One section, `network`, with nodes at layout coordinates and segments joining them:

```json
"network": {
  "length_factor": 1.0,
  "nodes": [{"name": "dock", "x": 0, "y": 10}, {"name": "aisle_w", "x": 20, "y": 10}],
  "segments": [{"from": "dock", "to": "aisle_w", "length": 20, "two_way": true}]
}
```

Coordinates are metres once the background image declares a scale (**View > Set scale from two
points...**); otherwise they are whatever unit you mean by them. The engine only ever divides a
length by a speed, so the two must simply agree.

A segment's `length` defaults to the straight-line distance between its endpoints times
`length_factor` -- 1.0 for straight aisles, 1.25 for "the real route is a quarter longer than
the crow flies". Give it explicitly for a ramp, a curve or a detour.

Attach a block to a node with a STATION's **`node`** parameter. That is what makes a station a
*place*: travel to and from it is the shortest path along the segments.

### Reading a distance

`DISTANCE("a", "b")` is the shortest path between two places, where either name may be a node, a
STATION block or a station alias:

```
DISTANCE(entity.station, "Shipping") / 1.4      a picker walking at 1.4 m/s
TABLE("Handling", entity.e_type) + DISTANCE("dock", "pick") / 60
```

The network is built once per run and every shortest-path tree is cached, so a repeated call is
a dict lookup.

!!! note "Quickest, not shortest, once a speed limit exists"
    A segment's optional `speed` caps how fast a vehicle crosses it, and it also **weights the
    route**: a vehicle takes the quickest way at its own speed, so a short aisle behind a 1 m/s
    limit loses to a longer free one. One route tree is cached per (start, speed), and most models
    have one or two distinct speeds. A network that declares no limit anywhere routes by length,
    which is the same ordering. `DISTANCE()` and the `nearest` dispatch rule stay by length --
    "nearest" is about metres.

## Walking: ROUTE in network mode

A ROUTE block normally samples a transfer-time distribution. Set `travel` to `"network"` and it
divides the shortest path between the two stations' nodes by the entity's own `speed`
expression:

```json
{"type": "ROUTE", "params": {"travel": "network", "speed": "entity.walk_speed"}}
```

That is people and self-propelled loads: no vehicle is requested, nothing queues.

## Driving: mover fleets and TRANSPORT

A `fleets` entry is a pool of vehicles:

```json
{"name": "Forklifts", "count": 3, "speed": 50, "speed_empty": 70,
 "load_time": "0.4", "unload_time": "0.4", "home": "dock",
 "park": "stay", "dispatch": "nearest", "select": "nearest"}
```

**A fleet is a resource.** It registers under its own name, so everything resources already do
works on it unchanged: a capacity schedule puts trucks on shift, a failure breaks one down,
`NQ("Forklifts")` and `IDLE("Forklifts")` read it in an expression, and utilization, seize
counts and the three cost fields report in the ordinary `resource_summary`. What a fleet adds is
**position per vehicle**, which is what dispatching needs.

A **TRANSPORT** block runs the whole cycle:

> request a vehicle -> it drives **empty** to the entity -> **load** -> it drives **loaded** to
> the destination -> **unload** -> free

The destination is the next station of the entity's routing, exactly as ROUTE would choose it,
or the STATION the block names in `destination`. When the routing is finished, TRANSPORT
forwards along its own drawn link instead, like ROUTE.

### The two rules

| Rule | Chooses | Values |
|---|---|---|
| `dispatch` | whose request is served when a vehicle frees | `fifo`, `nearest` (shortest empty drive) |
| `select` | which idle vehicle takes a granted request | `nearest`, `longest_idle`, `cyclic` |

A TRANSPORT's `priority` is the primary key under both rules, so an urgent load always outranks
the geometry. "Longest waiting" needs no rule of its own: that is what `fifo` is.

`park: "home"` sends a vehicle back to its home node once it is empty, and it is **available the
whole way**: a request arriving mid-return diverts it from exactly where it has got to, turning
round if that is quicker. The return counts as empty travel, not idle time. `park: "stay"` (the
default) leaves it where it dropped off, which is what makes `nearest` dispatching worth having.

A capacity schedule or a failure takes a **named** vehicle out of service -- the one that has been
standing still longest, wherever it is standing -- so the rest of the fleet keeps its positions
while one is out. It still waits for a free vehicle rather than interrupting a job, so the down
time itself is unchanged.

### Multi-load tours

`"loads": 3` lets one vehicle carry up to three entities per trip. When a vehicle is granted to a
TRANSPORT, up to two more TRANSPORTs **already waiting** on the fleet ride along (in `dispatch`
order, priority first). The vehicle visits every pick-up in nearest-neighbour order from where it
stands, then every drop in nearest-neighbour order, and each entity goes on from its own drop.
It never waits to fill, the fleet still counts vehicles (utilization is vehicle time), and the
`fleet_summary` column **Loads per Trip** shows how full the trips were. A worker fleet carries one load.

### Gantry cranes

`"motion": "gantry"` (default `"network"`) is a different kind of fleet: a bridge crane that
moves on its own two axes, not the path network.

```json
{"name": "Crane", "motion": "gantry", "speed": 1.0, "speed_x": 2.0, "speed_y": 1.5,
 "hoist_time": 5.0, "home": "cell_1"}
```

Travel between two nodes is `max(|dx| / speed_x, |dy| / speed_y) + hoist_time` -- the two axes
move at once, so the hop is bounded by the SLOWER one, not their combined distance, plus a fixed
`hoist_time` for raising and lowering the load. A gantry fleet needs no segment between its
nodes at all (node coordinates are all it reads) and ignores segment `capacity`: it is never on
one to begin with. TRANSPORT, tours and `dispatch`/`select` work exactly as above -- `nearest`
simply ranks by this same travel time instead of network distance. The `fleet_summary` distance
columns still report the honest straight-line distance driven, never the (longer) sum the two
axis moves would imply.

### What you get back

`fleet_summary`: trips, loaded and empty distance, the four-way split of vehicle time
(loaded / empty / loading-unloading / idle) and the wait for a vehicle. `segment_summary`:
traversals and distance driven per aisle -- where the kilometres actually went -- plus, for a
segment with a `capacity`, **Wait Time** (total time vehicles stood waiting to enter it) and
**Max Occupancy**.

### Segment traffic

A segment's `capacity` is how many vehicles may be on it at once; a two-way segment counts both
directions together. It is **zone control**: a vehicle asks for its next limited segment at the
end of the one it is on, and waits there -- still holding that one -- until there is room. Workers,
tours and `park: "home"` returns obey it too; a return diverted while waiting gives its places
back. Walking entities ignore it.

Two vehicles meeting head-on on a two-way capacity-1 aisle each wait for the other for ever.
Validation warns (`W_SEGMENT_DEADLOCK_RISK`) whenever such a segment sits on a network with more
than one vehicle, and a run that really locks up stops with a **deadlock** error naming who waits
for which segment. Make the aisle one-way, give it capacity 2, or add a passing place.

Empty distance is the number a layout study attacks. Every metre of it is a vehicle moving no
work.

## Workers

A fleet with `"kind": "worker"` is a pool of people, not vehicles: a SEIZE or PROCESS names it
with `worker_fleet` (and, optionally, `worker_skill`) instead of TRANSPORT carrying a part.

```json
{"name": "Ann", "kind": "worker", "speed": 10, "home": "A",
 "units": [{"name": "Ann", "skills": ["weld"]}, {"name": "Bob", "skills": ["paint"]}]}
```

`units` replaces `count` when a fleet needs named people and per-unit skills; each unit's `home`
defaults to the fleet's. The block must sit on a `node` (the place the worker walks to). Order at
the block: its own resource first (so balking, reneging and `queue_capacity` still apply to the
machine), then the worker request, restricted to units whose skills include `worker_skill` -- a
request nobody idle qualifies for is skipped, not queued behind, so a free painter is not blocked
by a waiting weld job. The worker walks **empty** to the node, is held for the service (and any
changeover), and is freed when the work ends (or, with `worker_when_blocked: "stays"`, when a
[blocked station](finite-buffers.md#workers-at-a-blocked-station) hands its part on); parking
follows the fleet's `park` rule exactly like a vehicle. A worker fleet is still a fleet, so it is a resource: utilization, schedules, failures,
costs and energy need no worker-specific reading.

## Conveyors

A CONVEYOR is not a DELAY. Three numbers define it:

```json
{"length": 0, "speed": 12, "spacing": 1.4, "accumulating": true,
 "exit_resource": "Loaders", "from_node": "pick", "to_node": "ship"}
```

* **capacity** = `floor(length / spacing)` -- how many entities fit on the belt;
* **travel time** = `length / speed`;
* **entry rhythm** = consecutive entries are at least `spacing / speed` apart, so the belt can
  never feed faster than `speed / spacing`.

`length: 0` takes the length from the network, between `from_node` and `to_node`.

**What blocks the exit.** A block downstream always accepts an entity, so a conveyor accumulates
only against something the model names: `exit_resource`, the end stop. An entity at the end must
take one unit of that resource before it may leave, and it carries the resource onward -- so the
natural pairing is a PROCESS with action `"Delay Release"` on the same resource at the far end.
With no `exit_resource` the exit never blocks, which is what a free-running belt is.

Then the two modes differ:

| Mode | When the exit blocks |
|---|---|
| `accumulating` (default) | the blocked entity stops at the end and the ones behind pack against it, each taking `spacing` of belt; the belt keeps loading while cells remain |
| non-accumulating | the whole belt stops -- nobody moves, nothing new gets on, and each entity resumes exactly where it stopped |

`conveyor_summary` reports entered, exited, average and maximum occupancy, blocked time and
average transit time.

**Stops along the belt.** A belt can also hold each part at points along its length, with
`stops`: `at` is the distance from the entry, `time` a number or an expression, and an optional
`resource` is seized for the stop and released when its time ends.

```json
{"length": 12, "speed": 0.5, "spacing": 1.2,
 "stops": [{"at": 4, "time": 30, "resource": "Labeller"}, {"at": 8, "time": "TRIA(5, 8, 12)"}]}
```

On an accumulating belt the parts behind pack against a stop as they do against the end, and
each section between two stops holds `floor(gap / spacing)` parts, so a queue reaches back past
a stop only by holding the part that is there. A non-accumulating belt stops whole while a part
is at a stop. The `conveyor_stops` frame, present only when a belt has stops, gives each stop's
parts, resource waits, busy share and blocked share.

!!! note "A full belt stops the machine feeding it"
    A conveyor's cells are a finite space, exactly like a buffer's places: a station that has
    finished a part and finds the belt full -- or stopped, or inside the entry rhythm
    `spacing / speed` -- keeps holding the part and stays **blocked**, and its blocked share shows
    up in `buffer_summary`. The part leaves the machine when it is physically on the belt.

## Ceilings in this build

Recorded so a model written today keeps meaning the same thing later:

* segment capacity binds vehicles only: walking entities (ROUTE `travel = "network"`) pass
  freely, and there is no deadlock avoidance -- only detection;
* no way to hold a vehicle across several legs outside a TRANSPORT; a multi-load tour is
  planned nearest-neighbour (not the shortest tour) and never waits for more loads;
* a conveyor's stops hold a part on the belt, but only its **end** hands a part on, so a belt
  with mid-line diverts needs one conveyor per zone; a state cannot carry a belt with stops;
* gantry cranes sharing a runway do not collide with each other -- there is no ordering
  constraint on the shared axis, only each crane's own two-axis geometry.

## Drawing coordinates

A block keeps its schematic `position` and may have a `layout_position` and a `layout_rotation`
(degrees): where it stands on the floor plan. A submodel's own path-network nodes turn with it, so
the distances to a rotated one change accordingly. Nothing else in a run reads them.

Worked example: the bundled model `warehouse_forklifts`.
