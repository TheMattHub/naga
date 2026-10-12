"""Layout, paths, movers and conveyors (PHASE4 E1, ARCHITECTURE D68).

Every timing here is hand-computed: a path network turns travel into
`distance / speed`, which is arithmetic, so nothing in this file needs an
oracle or a statistical tolerance.
"""
import json
import math
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core import schema
from nagasim.core.document import ModelDocument
from nagasim.core.network import build_network, point_on_path
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder

from conftest import create, dispose, layout

ROOT = Path(__file__).resolve().parents[1]

# A 3-4-5 triangle plus a long way round, so the shortest path is not the only
# path and the arithmetic is exact in binary:
#
#     dock (0,0) --50-- store (30,40)
#       |                   |
#       80                  100          (via "far")
#       +------- far -------+
NETWORK = {
    "nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "store", "x": 30, "y": 40},
              {"name": "far", "x": 0, "y": 80}],
    "segments": [{"from": "dock", "to": "store"},
                 {"from": "dock", "to": "far"},
                 {"from": "far", "to": "store", "length": 100}],
}


def station(name, node, position=(2, 0), **params):
    return {"name": name, "description": name, "type": "STATION",
            "position": list(position), "params": {"node": node, **params}}


def route(name="route_1", travel="time", value=0.0, speed="", position=(1, 0)):
    return {"name": name, "description": name, "type": "ROUTE", "position": list(position),
            "params": {"distribution": "Constant", "dist_params": {"value": value},
                       "travel": travel, "speed": speed}}


def transport(name, fleet, destination="", position=(3, 0), priority=0):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": list(position),
            "params": {"fleet": fleet, "destination": destination, "priority": priority}}


def fleet(name="Forklifts", **spec):
    base = {"name": name, "count": 1, "speed": 5.0, "speed_empty": 10.0,
            "load_time": "1", "unload_time": "1", "home": "dock"}
    base.update(spec)
    return base


# -- geometry ---------------------------------------------------------------

def test_shortest_path_prefers_the_direct_segment_and_DISTANCE_reads_it():
    """Dijkstra on length: dock->store is the 50 m hypotenuse, not the 180 m
    detour, and `DISTANCE()` reports the same number from an expression."""
    network = build_network(NETWORK)
    assert network.distance("dock", "store") == pytest.approx(50.0)
    assert network.path("dock", "store") == ["dock", "store"]
    # The detour exists and is longer, so the choice above was a real one.
    assert network.path_length(["dock", "far", "store"]) == pytest.approx(180.0)
    assert network.distance("dock", "dock") == 0.0

    model = layout([create(max_arrivals=1), _record_distance(), dispose()],
                   [{"source": "create_1", "target": "probe"},
                    {"source": "probe", "target": "dispose_1"}],
                   network=NETWORK,
                   simulation={"time_limit": 10, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)
    value = results["user_summary"].set_index("Statistic").loc["walk", "Value"]
    assert value == pytest.approx(50.0)


def _record_distance():
    return {"name": "probe", "description": "probe", "type": "RECORD", "position": [1, 0],
            "params": {"kind": "tally", "stat_name": "walk",
                       "expr": "DISTANCE(\"dock\", \"store\")"}}


def test_an_unreachable_pair_a_routing_uses_is_a_validation_error():
    """A one-way segment that strands a routing step is caught before the run,
    naming both places (the alternative is a crash halfway through)."""
    one_way = {"nodes": NETWORK["nodes"],
               "segments": [{"from": "dock", "to": "store", "two_way": False}]}
    model = layout(
        [create(max_arrivals=1, sequence="Back"), route(), station("Dock", "dock"),
         station("Store", "store", position=(4, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "dispose_1"},
         {"source": "Store", "target": "dispose_1"}],
        network=one_way,
        sequences=[{"name": "Back", "steps": [{"station": "Store"}, {"station": "Dock"}]}])
    codes = {p.code for p in schema.validate_model_structured(model)}
    assert "E_NETWORK_UNREACHABLE" in codes
    message = next(p.message for p in schema.validate_model_structured(model)
                   if p.code == "E_NETWORK_UNREACHABLE")
    assert "'Store'" in message and "'Dock'" in message


def test_point_on_path_interpolates_by_length_not_by_leg():
    """Playback's whole geometry: a constant-speed trip covers a fraction of
    the LENGTH equal to the fraction of the time."""
    network = build_network(NETWORK)
    path = ["dock", "far", "store"]              # 80 m then 100 m = 180 m
    assert point_on_path(network, path, 0.0) == pytest.approx((0.0, 0.0))
    assert point_on_path(network, path, 40.0 / 180.0) == pytest.approx((0.0, 40.0))
    # Half way = 90 m: 10 m along the second leg, which runs (0,80) -> (30,40).
    x, y = point_on_path(network, path, 0.5)
    assert (x, y) == pytest.approx((10.0 * 30.0 / 100.0, 80.0 - 10.0 * 40.0 / 100.0))
    assert point_on_path(network, path, 1.0) == pytest.approx((30.0, 40.0))


# -- free-path travel on ROUTE ----------------------------------------------

def test_route_in_network_mode_takes_distance_over_speed():
    """A picker walking at 2 units/time covers the 50 m hypotenuse in 25."""
    model = _walk_model(speed="2")
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Avg Time"] == pytest.approx(25.0)

    # Halve the speed and the trip takes exactly twice as long: geometry, not
    # a distribution.
    slower = api.run_model(_walk_model(speed="1"), replications=1)
    assert slower["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(50.0)


def _walk_model(speed):
    return layout(
        [create(max_arrivals=1, sequence="Walk"), route(travel="network", speed=speed),
         station("Dock", "dock"), station("Store", "store", position=(4, 0)),
         route(name="route_2", travel="network", speed=speed, position=(5, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "route_1"},
         {"source": "Store", "target": "route_2"},
         {"source": "route_2", "target": "dispose_1"}],
        network=NETWORK,
        sequences=[{"name": "Walk", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
        simulation={"time_limit": 400, "warmup": 0, "replications": 1})


def test_a_segment_speed_limit_slows_the_trip_but_the_route_is_still_the_quickest():
    """A limit caps the mover's own speed AND weights the route (PHASE4 T7).
    At 1 m/s the 50 m hypotenuse still takes 50 against the detour's 90, so the
    route is unchanged and only the time moves."""
    limited = {"nodes": NETWORK["nodes"],
               "segments": [dict(NETWORK["segments"][0], speed=1.0),
                            NETWORK["segments"][1], NETWORK["segments"][2]]}
    model = _walk_model(speed="2")
    model["network"] = limited
    results = api.run_model(model, replications=1)
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(50.0)


def test_a_short_slow_segment_loses_to_a_longer_fast_one():
    """PHASE4 T7: routes are by TIME. The 50 m hypotenuse capped at 0.2 m/s
    takes 250; the 180 m detour at the walker's own 2 m/s takes 90, so the
    detour is the route -- by length it never would be."""
    limited = {"nodes": NETWORK["nodes"],
               "segments": [dict(NETWORK["segments"][0], speed=0.2),
                            NETWORK["segments"][1], NETWORK["segments"][2]]}
    model = _walk_model(speed="2")
    model["network"] = limited
    results = api.run_model(model, replications=1)
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(90.0)

    network = build_network(limited)
    assert network.path("dock", "store", 2.0) == ["dock", "far", "store"]
    assert network.path("dock", "store") == ["dock", "store"]      # by length, unchanged


# -- movers -----------------------------------------------------------------

def _transport_model(fleets, arrivals=3, gap=10.0, limit=400, **create_params):
    """dock -> TRANSPORT -> store, one entity per arrival."""
    return layout(
        [create(max_arrivals=arrivals, dist_params={"value": gap}, sequence="InOut",
                **create_params),
         route(), station("Dock", "dock"), transport("carry", fleets[0]["name"]),
         station("Store", "store", position=(4, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "carry"},
         {"source": "carry", "target": "dispose_1"},
         {"source": "Store", "target": "dispose_1"}],
        network=NETWORK, fleets=fleets,
        sequences=[{"name": "InOut", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
        simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def test_one_transport_trip_is_empty_leg_plus_load_plus_loaded_leg_plus_unload():
    """The vehicle starts at the dock, so: 0 empty + 1 load + 50/5 loaded +
    1 unload = 12. The second entity waits for it to come back: it is freed at
    12, drives 50/10 = 5 back, loads, carries, unloads -> 17 more."""
    results = api.run_model(_transport_model([fleet()], arrivals=3, gap=10.0), replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(12.0)
    # arrival 20 -> vehicle free at 22 -> 5 + 1 + 10 + 1 -> out at 39
    assert row["Avg Time"] == pytest.approx((12.0 + 19.0 + 26.0) / 3)

    fleets = results["fleet_summary"].iloc[0]
    assert fleets["Trips"] == 3
    assert fleets["Distance Loaded"] == pytest.approx(150.0)    # 3 x 50 loaded
    assert fleets["Distance Empty"] == pytest.approx(100.0)     # 2 x 50 fetching
    segments = results["segment_summary"].set_index("Segment")
    assert segments.loc["dock<->store", "Traversals"] == 5
    assert segments.loc["dock<->store", "Distance Driven"] == pytest.approx(250.0)


def test_park_home_sends_the_vehicle_back_and_pays_for_it_in_empty_distance():
    """`park: "home"` is part of the job, so the return leg is empty distance
    and the next fetch starts from home, not from the drop-off."""
    results = api.run_model(_transport_model([fleet(park="home")], arrivals=2, gap=100.0),
                            replications=1)
    row = results["fleet_summary"].iloc[0]
    assert row["Distance Loaded"] == pytest.approx(100.0)       # 2 x 50
    assert row["Distance Empty"] == pytest.approx(100.0)        # 2 x 50 going home
    # Both entities are fetched from home with no waiting, so both take 12.
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(12.0)


def test_a_vehicle_on_its_way_home_is_diverted_from_where_it_actually_is():
    """PHASE4 T7. Home is `far`, so the return is NOT on the way to the next
    pick-up and every number is hand-computed:

    entity 1 at t=0 -- far->dock empty 80/10 = 8, load 1, dock->store loaded
    50/5 = 10, unload 1: delivered at 20, vehicle free AT ONCE at the store and
    driving home along store->far (100 m at 10).

    entity 2 at t=24 -- the vehicle is 4 units into that return, so 40 m along
    the segment. To reach the dock it may carry on to `far` (60 + 80 = 140) or
    turn back to the store (40 + 50 = 90): it turns back, 4 more units to the
    store at 28, store->dock 50/10 = 5 at 33, load 1, loaded 10, unload 1 -> out
    at 45, i.e. 21 in the system. Waiting for the vehicle to reach home first
    would have cost 26.
    """
    model = _transport_model([fleet(park="home", home="far")], arrivals=2, gap=24.0)
    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(20.0)
    assert row["Max Time"] == pytest.approx(21.0)

    # The return legs are EMPTY travel, not idle: 80 fetching + 40 driven before
    # the divert + 40 back + 50 fetching + 100 finally getting home.
    totals = results["fleet_summary"].iloc[0]
    assert totals["Distance Empty"] == pytest.approx(310.0)
    assert totals["Distance Loaded"] == pytest.approx(100.0)
    # Whole segments only: the abandoned 40 m of far<->store is not a traversal.
    segments = results["segment_summary"].set_index("Segment")["Traversals"]
    assert dict(segments) == {"dock<->store": 3, "dock<->far": 1, "far<->store": 1}


def test_a_shift_takes_a_NAMED_vehicle_so_the_fleet_keeps_its_positions():
    """PHASE4 T7. Two vehicles start at the dock. The first job leaves vehicle 0
    at the store at t=12; at t=15 the shift drops the fleet to one vehicle and
    takes the one that has been standing still longest -- vehicle 1, still at
    the dock. The job at t=20 therefore has to be fetched from the store: 50/10
    empty, load, 50/5 loaded, unload = 17 in the system, and 50 m of empty
    travel. With an anonymous unit it would have been served from the dock in
    12, with no empty travel at all.
    """
    model = _transport_model([fleet(count=2, capacity_schedule="Short shift")],
                             arrivals=2, gap=20.0)
    model["schedules"] = [{"name": "Short shift", "kind": "capacity",
                           "points": [[15, 2], [385, 1]]}]
    assert not schema.validate_model(model)

    results = api.run_model(model, replications=1)
    row = results["entity_summary"].iloc[0]
    assert row["Min Time"] == pytest.approx(12.0)
    assert row["Max Time"] == pytest.approx(17.0)
    totals = results["fleet_summary"].iloc[0]
    assert totals["Distance Empty"] == pytest.approx(50.0)
    assert totals["Distance Loaded"] == pytest.approx(100.0)


@pytest.mark.parametrize("rule, expected", [("fifo", ["A", "B"]), ("nearest", ["B", "A"])])
def test_one_vehicle_serves_two_waiting_requests_in_the_rules_own_order(rule, expected):
    """Two requests are waiting when the only vehicle frees at the store: A was
    made first, at the FAR node (180 m away); B second, at the store itself
    (0 m). `fifo` takes A, `nearest` takes B -- the decision is made when the
    vehicle frees, which is the whole point of live dispatching.
    """
    served = _dispatch_order(rule)
    assert served == expected


def _dispatch_order(rule):
    """One vehicle, three jobs, hand-timed.

    `seed` is created at 0 at the dock and carried to the store: the vehicle is
    busy 0..12 and frees AT THE STORE. `A` is created at 1 at `far` (100 m from
    the store), `B` at 2 at the store itself (0 m), so at t=12 exactly two
    requests are waiting and the rule alone decides between them.
    """
    blocks = [
        create(name="seed", max_arrivals=1, dist_params={"value": 1.0}, sequence="Seed"),
        create(name="a_src", max_arrivals=1, dist_params={"value": 1.0}, start_time=1,
               sequence="FromFar"),
        create(name="b_src", max_arrivals=1, dist_params={"value": 1.0}, start_time=2,
               sequence="FromStore"),
        route(name="route_seed"), route(name="route_a", position=(1, 1)),
        route(name="route_b", position=(1, 2)),
        station("Dock", "dock"), station("Far", "far", position=(2, 1)),
        station("Store", "store", position=(4, 0)),
        station("StoreIn", "store", position=(2, 2), station="StoreIn"),
        transport("carry_seed", "Forklifts"),
        transport("carry_a", "Forklifts", destination="Dock", position=(3, 1)),
        transport("carry_b", "Forklifts", destination="Dock", position=(3, 2)),
        dispose(),
    ]
    links = [{"source": "seed", "target": "route_seed"},
             {"source": "a_src", "target": "route_a"},
             {"source": "b_src", "target": "route_b"},
             {"source": "route_seed", "target": "dispose_1"},
             {"source": "route_a", "target": "dispose_1"},
             {"source": "route_b", "target": "dispose_1"},
             {"source": "Dock", "target": "carry_seed"},
             {"source": "Far", "target": "carry_a"},
             {"source": "StoreIn", "target": "carry_b"},
             {"source": "Store", "target": "dispose_1"},
             {"source": "carry_seed", "target": "dispose_1"},
             {"source": "carry_a", "target": "dispose_1"},
             {"source": "carry_b", "target": "dispose_1"}]
    model = layout(
        blocks, links, network=NETWORK,
        fleets=[fleet(count=1, dispatch=rule)],
        sequences=[{"name": "Seed", "steps": [{"station": "Dock"}, {"station": "Store"}]},
                   {"name": "FromFar", "steps": [{"station": "Far"}, {"station": "Dock"}]},
                   {"name": "FromStore",
                    "steps": [{"station": "StoreIn"}, {"station": "Dock"}]}],
        simulation={"time_limit": 400, "warmup": 0, "replications": 1})
    assert not schema.validate_model(model)

    recorder = TraceRecorder()
    api.run_model(model, replications=1, trace=recorder)
    picked_up = sorted((e.t, e.block) for e in recorder.events
                       if e.kind == "entity_moved" and e.block in ("carry_a", "carry_b"))
    return [{"carry_a": "A", "carry_b": "B"}[block] for _t, block in picked_up]


def test_a_fleet_is_a_resource_so_a_failure_holds_it_for_exactly_the_repair():
    """A fleet registers under its own name, so `failures` works on it with no
    mover-specific code at all -- that is the whole point of reusing the
    resource machinery (ARCHITECTURE D68).

    The vehicle is idle when it breaks at t=5, so the blocker is granted at
    once and the repair runs 5..25. The single entity arrives at 10, waits the
    15 that are left of the repair, then makes its ordinary 12-unit trip.
    """
    model = _transport_model([fleet(count=1)], arrivals=1, gap=10.0, start_time=10)
    baseline = api.run_model(model, replications=1)["entity_summary"].iloc[0]["Avg Time"]
    assert baseline == pytest.approx(12.0)

    model["failures"] = [{"name": "Flat tyre", "resources": ["Forklifts"],
                          "uptime": {"distribution": "Constant", "dist_params": {"value": 5.0}},
                          "downtime": {"distribution": "Constant", "dist_params": {"value": 20.0}}}]
    results = api.run_model(model, replications=1)
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(12.0 + 15.0)
    row = results["resource_summary"].set_index("Resource").loc["Forklifts"]
    assert row["Down %"] > 0


def test_the_trace_carries_the_path_so_playback_lands_on_the_hand_computed_point():
    """Item 7 of E1: mover and entity positions are interpolated from trace
    data alone. Halfway through the loaded leg the vehicle is halfway along the
    polyline, which for dock->store is (15, 20)."""
    recorder = TraceRecorder()
    api.run_model(_transport_model([fleet()], arrivals=1), replications=1, trace=recorder)

    loaded = [e for e in recorder.events if e.kind == "mover_moved" and e.state == "loaded"]
    assert len(loaded) == 1
    move = loaded[0]
    assert move.path == ("dock", "store")
    assert move.duration == pytest.approx(10.0)        # 50 / 5
    network = build_network(NETWORK)
    assert point_on_path(network, move.path, 0.5) == pytest.approx((15.0, 20.0))

    # The entity's own move carries the same path, with the unload NOT counted
    # as travel -- so it is at the destination for the last unit of `duration`.
    carried = next(e for e in recorder.events
                   if e.kind == "entity_moved" and e.block == "carry")
    assert carried.path == ("dock", "store")
    assert carried.duration == pytest.approx(11.0)     # travel 10 + unload 1
    assert carried.travel == pytest.approx(10.0)

    timeline = Timeline(recorder.events)
    state = timeline.state_at(move.t + 5.0)
    _entity_id, (src, dst, progress, _e_type, path, _branch) =         next(iter(state["in_transit"].items()))
    assert (src, dst) == ("carry", "Store")
    assert path == ("dock", "store")
    assert point_on_path(network, path, progress) == pytest.approx((15.0, 20.0))
    assert state["movers"][("Forklifts", 0)][1] == pytest.approx(0.5)


# -- conveyors ---------------------------------------------------------------

def _conveyor_model(arrivals=5, gap=0.5, length=10.0, speed=1.0, spacing=2.0,
                    accumulating=True, exit_resource="", limit=200):
    blocks = [create(max_arrivals=arrivals, dist_params={"value": gap}),
              {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [1, 0],
               "params": {"length": length, "speed": speed, "spacing": spacing,
                          "accumulating": accumulating, "exit_resource": exit_resource,
                          "from_node": "", "to_node": ""}}]
    links = [{"source": "create_1", "target": "belt"}]
    if exit_resource:
        blocks.append({"name": "pick", "description": "pick", "type": "PROCESS",
                       "position": [2, 0],
                       "params": {"action": "Delay Release", "resource_name": exit_resource,
                                  "distribution": "Constant", "dist_params": {"value": 5.0},
                                  "priority": 0, "quantity": 1}})
        links.append({"source": "belt", "target": "pick"})
        links.append({"source": "pick", "target": "dispose_1"})
    else:
        links.append({"source": "belt", "target": "dispose_1"})
    blocks.append(dispose())
    return layout(blocks, links,
                  resources=[{"name": exit_resource, "capacity": 1}] if exit_resource else [],
                  simulation={"time_limit": limit, "warmup": 0, "replications": 1})


def test_a_free_conveyor_is_exact_travel_plus_an_entry_rhythm():
    """length 10 / speed 1 = 10 to cross; spacing 2 / speed 1 = 2 between
    entries, so five entities offered every 0.5 enter at 0, 2, 4, 6, 8 and
    leave at 10, 12, 14, 16, 18 -- the belt, not the arrivals, sets the pace."""
    results = api.run_model(_conveyor_model(arrivals=5, gap=0.5), replications=1)
    row = results["conveyor_summary"].iloc[0]
    assert row["Capacity"] == 5                         # floor(10 / 2)
    assert row["Entered"] == 5 and row["Exited"] == 5
    # Entity 1 rides for 10; entity 5 waits 8 at the entry, then rides 10.
    entities = results["entity_summary"].iloc[0]
    assert entities["Min Time"] == pytest.approx(10.0)
    assert entities["Max Time"] == pytest.approx(10.0 + 8.0 - 2.0)   # arrived at 2.5, on at 8
    assert row["Avg Transit Time"] == pytest.approx(10.0)


def test_an_accumulating_belt_packs_entities_behind_a_blocked_exit():
    """The end stop is a capacity-1 resource held for 5 by the picker, so one
    entity leaves every 5. The others stop on the belt and close the gap
    `spacing / speed = 2` behind the one in front, which is exactly what
    accumulating means -- the belt keeps running behind them."""
    results = api.run_model(
        _conveyor_model(arrivals=4, gap=0.5, exit_resource="Picker"), replications=1)
    row = results["conveyor_summary"].iloc[0]
    assert row["Entered"] == 4 and row["Exited"] == 4
    assert row["Blocked %"] > 0
    # Entries are at 0.5, 2.5, 4.5, 6.5 (the spacing rhythm); the belt takes 10;
    # the picker then lets one off every 5, so they leave the belt at 10.5,
    # 15.5, 20.5 and 25.5 -- transits of 10, 13, 16 and 19.
    assert row["Avg Transit Time"] == pytest.approx((10 + 13 + 16 + 19) / 4)
    entities = results["entity_summary"].iloc[0]
    assert entities["Min Time"] == pytest.approx(15.0)      # 10 on the belt + 5 picking
    assert entities["Max Time"] == pytest.approx(28.5)


def test_a_non_accumulating_belt_stops_the_whole_belt_while_its_exit_is_blocked():
    """The two modes differ in what happens to the entity BEHIND the blocked one.

    Belt: 20 long, speed 1, spacing 2. Two entities get on at 0 and 6 and would
    reach the end at 20 and 26. A third stream hogs the end stop from 19 to 22,
    so the first is blocked for 2 and leaves at 22 either way.

    The second is the test: an accumulating belt keeps carrying it, so it
    reaches the end on time at 26; a non-accumulating one stands still for
    those 2, so it arrives at 28. Transit times 22 and 20 against 22 and 22.
    """
    def model(accumulating):
        blocks = [
            create(max_arrivals=2, dist_params={"value": 6.0}),
            {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [1, 0],
             "params": {"length": 20.0, "speed": 1.0, "spacing": 2.0,
                        "accumulating": accumulating, "exit_resource": "Picker",
                        "from_node": "", "to_node": ""}},
            {"name": "drop", "description": "drop", "type": "PROCESS", "position": [2, 0],
             "params": {"action": "Delay Release", "resource_name": "Picker",
                        "distribution": "Constant", "dist_params": {"value": 0.0},
                        "priority": 0, "quantity": 1}},
            create(name="hog_src", max_arrivals=1, dist_params={"value": 100.0}, start_time=19),
            {"name": "hog", "description": "hog", "type": "PROCESS", "position": [1, 1],
             "params": {"action": "Seize Delay Release", "resource_name": "Picker",
                        "distribution": "Constant", "dist_params": {"value": 3.0},
                        "priority": 0, "quantity": 1}},
            dispose()]
        links = [{"source": "create_1", "target": "belt"},
                 {"source": "belt", "target": "drop"},
                 {"source": "drop", "target": "dispose_1"},
                 {"source": "hog_src", "target": "hog"},
                 {"source": "hog", "target": "dispose_1"}]
        return layout(blocks, links, resources=[{"name": "Picker", "capacity": 1}],
                      simulation={"time_limit": 80, "warmup": 0, "replications": 1})

    packed = api.run_model(model(True), replications=1)["conveyor_summary"].iloc[0]
    stopped = api.run_model(model(False), replications=1)["conveyor_summary"].iloc[0]
    assert packed["Avg Transit Time"] == pytest.approx((22.0 + 20.0) / 2)
    assert stopped["Avg Transit Time"] == pytest.approx((22.0 + 22.0) / 2)
    assert packed["Exited"] == stopped["Exited"] == 2
    # Both belts were blocked for the same 2 units; the percentages differ only
    # because the stopped belt drains 2 later, so the window is longer.
    assert packed["Blocked %"] > 0 and stopped["Blocked %"] > 0


def _belt_backpressure_model(front):
    """`front` -> CONVEYOR -> DISPOSE. The belt is one cell long: length 2,
    spacing 2 (capacity floor(2/2) = 1), speed 1 (travel 2, entry rhythm 2)."""
    belt = {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [2, 0],
            "params": {"length": 2.0, "speed": 1.0, "spacing": 2.0, "accumulating": True,
                       "exit_resource": "", "from_node": "", "to_node": ""}}
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}), front, belt, dispose()]
    return layout(blocks, [{"source": "create_1", "target": front["name"]},
                           {"source": front["name"], "target": "belt"},
                           {"source": "belt", "target": "dispose_1"}],
                  resources=[{"name": "M1", "capacity": 1}],
                  simulation={"time_limit": 10, "warmup": 0, "replications": 1})


def test_a_full_belt_blocks_the_machine_feeding_it():
    """PHASE4 T7. The belt holds one part and takes one every 2; the machine
    makes one every 0.5, so it has to stand there holding the finished part.

    Hand-computed: part 1 seizes at 0, is done at 0.5 and goes straight on the
    belt, which frees the machine at 0.5 and drops the part at 2.5. Part 2
    seizes at 1, is done at 1.5 and waits for the cell until 2.5. Part 3 seizes
    at 2.5, is done at 3.0 and waits until 4.5. The last part leaves the belt at
    6.5, and over that window the machine is held 0.5 +
    1.5 + 2.0 = 4.0 (61.54%), of which 2.5 is blocked (38.46%) -- before T7 it
    was held only 1.5, the belt having taken each part the instant it was made.
    """
    front = {"name": "m1", "description": "m1", "type": "PROCESS", "position": [1, 0],
             "params": {"action": "Seize Delay Release", "resource_name": "M1",
                        "distribution": "Constant", "dist_params": {"value": 0.5},
                        "priority": 0, "quantity": 1}}
    results = api.run_model(_belt_backpressure_model(front), replications=1)

    entities = results["entity_summary"].iloc[0]
    assert entities["Min Time"] == pytest.approx(2.5)
    assert entities["Max Time"] == pytest.approx(4.5)
    usage = results["resource_summary"].set_index("Resource").loc["M1", "Usage % (snapshot)"]
    assert usage == pytest.approx(100 * 4.0 / 6.5, abs=0.01)
    blocked = results["buffer_summary"].set_index("Block").loc["m1"]
    assert blocked["Blocked %"] == pytest.approx(100 * 2.5 / 6.5, abs=0.01)
    # The belt reports in its own frame and gets no buffer row of its own.
    assert "belt" not in set(results["buffer_summary"]["Block"])


def test_a_buffer_in_front_of_a_belt_really_holds_the_part():
    """PHASE4 T7, the other half: a BUFFER's place is given back when the part
    is physically ON the belt, not when it is offered. Part 2 arrives at 1 and
    the only cell is busy until 2, so the buffer really holds a part for 1 of
    the run's 4 -- an average content of 0.25, where before it was 0."""
    front = {"name": "buf", "description": "buf", "type": "BUFFER", "position": [1, 0],
             "params": {"capacity": 2, "level": ""}}
    model = _belt_backpressure_model(front)
    model["blocks"][0]["params"]["max_arrivals"] = 2
    results = api.run_model(model, replications=1)

    row = results["buffer_summary"].set_index("Block").loc["buf"]
    assert row["Entered"] == 2
    assert row["Max Content"] == 1
    assert row["Avg Content"] == pytest.approx(0.25)


def test_a_conveyor_takes_its_length_from_the_network_when_it_has_none():
    model = _conveyor_model(arrivals=1, length=0.0, speed=5.0, spacing=10.0)
    model["network"] = NETWORK
    model["blocks"][1]["params"]["from_node"] = "dock"
    model["blocks"][1]["params"]["to_node"] = "store"
    assert not schema.validate_model(model)
    results = api.run_model(model, replications=1)
    row = results["conveyor_summary"].iloc[0]
    assert row["Length"] == pytest.approx(50.0)
    assert row["Capacity"] == 5
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(10.0)


# -- authoring ---------------------------------------------------------------

def test_document_edits_nodes_segments_and_fleets_with_one_undo_step_each():
    doc = ModelDocument()
    doc.upsert_node({"name": "dock", "x": 0, "y": 0})
    doc.upsert_node({"name": "store", "x": 30, "y": 40})
    doc.upsert_segment({"from": "dock", "to": "store"})
    doc.upsert_fleet({"name": "Forklifts", "count": 2, "speed": 2.0, "home": "dock"})
    assert [n["name"] for n in doc.network_nodes()] == ["dock", "store"]
    assert doc.to_layout()["fleets"][0]["count"] == 2

    # Renaming a node rewrites the geometry AND everything attached to it.
    doc.add_block("STATION", (0, 0), name="Dock")
    doc.set_params("Dock", {"station": "", "node": "dock"})
    doc.rename_node("dock", "inbound")
    assert doc.blocks["Dock"].params["node"] == "inbound"
    assert doc.network_segments()[0]["from"] == "inbound"
    assert doc.to_layout()["fleets"][0]["home"] == "inbound"
    doc.undo()
    assert doc.blocks["Dock"].params["node"] == "dock"

    # Deleting a node takes its segments with it: a dangling end is not a model.
    doc.remove_node("dock")
    assert doc.network_segments() == []


def test_an_agent_lays_out_a_network_and_queries_a_distance(tmp_path):
    """The agent surface has to be able to BUILD the geometry, not just read
    it: place nodes, join them, park a fleet, ask how far it is."""
    from nagasim import agent_tools as T

    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("new_model", name="wh")
    for name, x, y in (("dock", 0, 0), ("store", 30, 40), ("far", 0, 80)):
        assert "error" not in T.call("upsert_node", model="wh", name=name, x=x, y=y)
    T.call("upsert_segment", model="wh", source="dock", target="store")
    T.call("upsert_segment", model="wh", source="dock", target="far")
    T.call("upsert_record", model="wh", section="fleets",
           record={"name": "Forklifts", "count": 2, "speed": 2.0, "home": "dock"})

    answer = T.call("get_distance", model="wh", source="dock", target="store")
    assert answer["distance"] == pytest.approx(50.0)
    assert answer["path"] == ["dock", "store"]

    net = T.call("get_network", model="wh")
    assert [n["name"] for n in net["nodes"]] == ["dock", "store", "far"]
    assert net["unreachable"] == []                 # everything hangs off the dock
    assert T.call("list_records", model="wh", section="fleets")["records"][0]["count"] == 2

    # Cutting the model in two is visible before anything is run.
    T.call("remove_segment", model="wh", source="dock", target="far")
    assert ["far", "dock"] in T.call("get_network", model="wh")["unreachable"]
    T.call("remove_node", model="wh", name="far")
    assert T.call("get_network", model="wh")["unreachable"] == []


def test_the_warehouse_example_shows_the_geometry_driving_the_KPIs():
    """The bundled proof (`nagasim/models/warehouse_forklifts.json`).

    The basics -- that it produces statistics under its own settings -- are
    already guarded for every example by
    `test_every_example_produces_statistics_under_its_own_settings`. What is
    checked here is the thing this model exists to show: KPIs that move because
    the BUILDING changed, in the direction hand reasoning gives.
    """
    from nagasim import experiments

    path = ROOT / "nagasim" / "models" / "warehouse_forklifts.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    by_name = {s["name"]: s["overrides"] for s in layout["scenarios"]}

    def run(overrides=()):
        variant = experiments.apply_overrides(layout, overrides)
        return api.run_model(variant, replications=2, seed=0, warmup=120, run_length=600)

    base, far_dock = run(), run(by_name["Dock across the yard"])

    # Every trip's first leg grows by 70 m, so the driving grows and the
    # trucks -- 70-odd per cent busy to start with -- become the constraint.
    assert _fleet(far_dock, "Distance Loaded") > _fleet(base, "Distance Loaded")
    assert _fleet(far_dock, "Distance Empty") > _fleet(base, "Distance Empty")
    assert _usage(far_dock, "Forklifts") > _usage(base, "Forklifts")
    assert _metric(far_dock, "Avg Throughput Time") > _metric(base, "Avg Throughput Time")

    # A fourth truck cannot make more pallets arrive; it cuts the WAIT for one.
    more = run(by_name["Four forklifts"])
    assert _usage(more, "Forklifts") < _usage(base, "Forklifts")
    assert _fleet(more, "Avg Request Wait") < _fleet(base, "Avg Request Wait")

    # A dispatching rule changes the deadheading and nothing else: the same
    # loads travel the same loaded metres either way.
    fifo = run(by_name["FIFO dispatch"])
    assert _fleet(fifo, "Distance Empty") > _fleet(base, "Distance Empty")
    assert _fleet(fifo, "Distance Loaded") == pytest.approx(_fleet(base, "Distance Loaded"))


def _fleet(results, column):
    return float(results["fleet_summary"][column].mean())


def _usage(results, resource):
    rows = results["resource_summary"]
    return float(rows[rows["Resource"] == resource]["Usage % (snapshot)"].mean())


def _metric(results, name):
    rows = results["system"]
    return float(rows[rows["Metric"] == name]["Value"].astype(float).mean())


def test_a_model_with_no_network_pays_nothing_and_reports_empty_frames(simple_model_path):
    results = api.run_model(simple_model_path, replications=1, run_length=20)
    for key in ("fleet_summary", "segment_summary", "conveyor_summary"):
        assert results[key].empty
        assert list(results[key].columns)[0] == "Run"


def test_a_scoped_fleet_clones_per_instance_and_the_template_is_consumed():
    """A fleet IS a resource (D68), so `$Fleet` in a TRANSPORT clones it per
    instance exactly as `$Machine` clones a machine -- which is what lets one
    Vehicle fleet asset be both the forklifts and the AGVs (PHASE4 E2). The
    TEMPLATE goes, unlike a resource's: a leftover fleet would register
    vehicles of its own and need a home node only an instance knows."""
    from nagasim.core import submodels

    definition = {
        "parameters": [{"name": "vehicles", "default": 1, "type": "int"},
                       {"name": "base", "default": "dock", "type": "name"}],
        "scoped": {"fleets": {"Movers": {"count": {"$arg": "vehicles"},
                                         "home": {"$arg": "base"}}}},
        "blocks": [{"name": "in", "type": "SUB_ENTRY", "position": [0, 0],
                    "params": {"port": "in"}},
                   {"name": "carry", "type": "TRANSPORT", "position": [1, 0],
                    "params": {"fleet": "$Movers", "destination": "", "priority": 0}},
                   {"name": "out", "type": "SUB_EXIT", "position": [2, 0],
                    "params": {"port": "out"}}],
        "links": [{"source": "in", "target": "carry"}, {"source": "carry", "target": "out"}],
    }
    model = schema.migrate({
        "submodels": {"Fleet": definition},
        "network": NETWORK,
        "fleets": [{"name": "Movers", "count": 9, "speed": 5.0, "home": "dock"}],
        "blocks": [{"name": "forklifts", "type": "SUBMODEL", "position": [0, 0],
                    "params": {"definition": "Fleet",
                               "arguments": {"vehicles": 3, "base": "dock"}}},
                   {"name": "agvs", "type": "SUBMODEL", "position": [1, 0],
                    "params": {"definition": "Fleet",
                               "arguments": {"vehicles": 2, "base": "store"}}}],
        "links": [],
    })
    flat = submodels.flatten(model)

    assert {f["name"]: (f["count"], f["home"]) for f in flat["fleets"]} == {
        "forklifts/Movers": (3, "dock"), "agvs/Movers": (2, "store")}
    assert {b["params"]["fleet"] for b in flat["blocks"] if b["type"] == "TRANSPORT"} == {
        "forklifts/Movers", "agvs/Movers"}
