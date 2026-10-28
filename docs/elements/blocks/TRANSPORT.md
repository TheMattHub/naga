Use TRANSPORT when a vehicle must come and fetch the entity: a forklift, an AGV, a truck, a mobile robot. It replaces the fixed transfer time of [ROUTE](route.md) with a real trip. For a fixed link time with no vehicle, use a link delay or ROUTE.

## Behaviour and rules

The block does the whole cycle. The entity requests a vehicle from the `fleet`. The vehicle drives empty to the entity's node, loads, drives loaded to the destination, unloads and is freed. The entity is delivered when the unload ends. The vehicle is busy for the whole cycle, and also for the drive home if the fleet parks at home.

The `destination` is a STATION. If it is empty, the entity goes to the next station of its sequence, as ROUTE would choose. Every time is exact geometry: the shortest path on the network divided by the fleet's loaded or empty speed, capped by any segment speed limit. A vehicle request outranks the fleet's dispatching rule by its `priority`, lower first.

With `keep_vehicle` on, the vehicle stays at the drop and the entity's next TRANSPORT on that fleet drives it with no new request. A RELEASE that names the fleet frees it, as does the entity leaving the flow. A fleet with `loads` above 1 can carry several waiting entities on one tour. The `fleet_summary` frame reports trips and distances.
