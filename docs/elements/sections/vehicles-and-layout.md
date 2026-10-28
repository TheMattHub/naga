These sections describe things that move and the floor they move on. `network` is a path network of nodes and segments. `fleets` are pools of vehicles or workers that carry entities on it. `chargers` recharge batteries. `motion_profiles` and `zones` describe robots and their safety areas. Use them when distances and travel times matter.

## Behaviour and rules

Travel time is exact geometry: the shortest path on the network divided by the speed. A segment can limit the speed. The `length_factor` scales straight-line distances for a segment with no length of its own. A node is named by a STATION through its `node`.

A fleet is a resource under its own name. A TRANSPORT block requests one of its vehicles, which drives empty to the entity, loads, drives loaded to the destination and unloads. `dispatch` decides whose request is served when a vehicle frees, and `select` decides which idle vehicle serves it. With `park` set to `home`, an idle vehicle drives back to its `home` node.

A fleet with `kind` set to `worker` supplies people to PROCESS and SEIZE blocks through `worker_fleet`. A fleet with a `battery` drains it at its power draw and recharges at a charger when its job is done. A fleet with `motion` set to `gantry` ignores the network. A motion profile gives a robot its speed, acceleration, payload derating and a collaborative speed. A zone slows a robot to that speed when a worker is inside.
