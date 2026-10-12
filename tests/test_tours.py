"""Multi-load tours: a fleet's `loads: k` (PHASE6 W3-M4, ARCHITECTURE D116).

A straight line A (0) - B (10) - C (20) - D (30), speed 10 both ways, so every
leg is its length / 10 and every timing below is hand arithmetic.
"""
import math

import pytest

from nagasim import api
from nagasim.core import schema
from nagasim.core.trace import TraceRecorder

from conftest import create, dispose, layout

NET = {"nodes": [{"name": n, "x": x, "y": 0} for n, x in
                 (("A", 0), ("B", 10), ("C", 20), ("D", 30))],
       "segments": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"},
                    {"from": "C", "to": "D"}]}
SIM = {"time_limit": 1000, "warmup": 0, "replications": 1}


def station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": {"node": node}}


def transport(name, destination, priority=0):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [2, 0],
            "params": {"fleet": "V", "destination": destination, "priority": priority}}


def fleet(**spec):
    base = {"name": "V", "count": 1, "speed": 10.0, "home": "A", "loads": 2,
            "load_time": "1", "unload_time": "1"}
    base.update(spec)
    return base


def lanes(*lanes, fleet_spec=None, **sections):
    """One CREATE -> STATION (pick-up) -> TRANSPORT per lane; every drop
    station leads to one DISPOSE. A lane is (tag, pick node, drop node, create
    params, transport priority)."""
    blocks, links, drops = [dispose()], [], {}
    for tag, pick, drop, params, priority in lanes:
        blocks += [create(f"c_{tag}", e_type=tag, **params), station(f"S_{tag}", pick),
                   transport(f"t_{tag}", f"D_{drop}", priority)]
        links += [{"source": f"c_{tag}", "target": f"S_{tag}"},
                  {"source": f"S_{tag}", "target": f"t_{tag}"},
                  {"source": f"t_{tag}", "target": "dispose_1"}]
        drops[drop] = station(f"D_{drop}", drop)
    for block in drops.values():
        blocks.append(block)
        links.append({"source": block["name"], "target": "dispose_1"})
    return layout(blocks, links, network=NET, fleets=[fleet_spec or fleet()], simulation=SIM,
                  **sections)


def times(results, e_type):
    rows = results["entity_summary"]
    row = rows[rows["Entity Type"] == e_type].iloc[0]
    return row["Min Time"], row["Max Time"]


# One shared trip, k=1 as today, nearest-neighbour pick-ups and no waiting to
# fill are the oracle's (test_tours_oracle.py cases 1-4).
def test_nearest_neighbour_picks_then_drops_and_each_entity_leaves_at_its_drop():
    """No handling. From A: pick P at B (1), Q at C (2); drops from C: D is 10
    away, A 20, so P is dropped at D (3) and Q at A (6)."""
    one = {"max_arrivals": 1}
    doc = lanes(("P", "B", "D", one, 0), ("Q", "C", "A", one, 0),
                fleet_spec=fleet(load_time="0", unload_time="0"))
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    assert times(results, "P") == pytest.approx((3.0, 3.0))
    assert times(results, "Q") == pytest.approx((6.0, 6.0))
    row = results["fleet_summary"].iloc[0]
    assert (row["Distance Empty"], row["Distance Loaded"]) == (10.0, 50.0)
    # Playback: one mover_moved per leg, empty to the first pick-up only.
    legs = [(e.path, e.state) for e in recorder.events
            if e.kind == "mover_moved" and e.state != "idle"]
    assert legs == [(("A", "B"), "empty"), (("B", "C"), "loaded"),
                    (("C", "D"), "loaded"), (("D", "C", "B", "A"), "loaded")]
    # Q's ride spans every leg after its pick-up: C -> D -> A, 4 time units.
    ride = next(e for e in recorder.events if e.kind == "entity_moved" and e.block == "t_Q")
    assert ride.path == ("C", "D", "C", "B", "A")
    assert (ride.duration, ride.travel) == pytest.approx((4.0, 4.0))


def test_attached_loads_record_their_wait_and_k_caps_the_tour():
    """Three at A for D, k=2: the first two tour 0-7 (waits 0, 0); the third
    waits 7, then 3 back + 1 + 3 + 1 -> out at 15."""
    three = {"max_arrivals": 1, "entities_per_arrival": 3}
    results = api.run_model(lanes(("X", "A", "D", three, 0)), replications=1)
    assert times(results, "X") == pytest.approx((6.0, 15.0))
    row = results["fleet_summary"].iloc[0]
    assert (row["Trips"], row["Loads per Trip"]) == (2, 1.5)
    assert row["Max Request Wait"] == pytest.approx(7.0)
    assert row["Avg Request Wait"] == pytest.approx(7.0 / 3)
    res = results["resource_summary"]
    assert res[res["Resource"] == "V"].iloc[0]["Total Seizes"] == 2   # vehicles, not loads


def test_attachment_follows_priority_before_arrival():
    """The vehicle carries E0 0-5 (at D). LO (priority 5) waits from 1, two HI
    (priority 0) from 2: at 5 a HI leads and the other HI rides along
    (back 3, load, load, 3, unload, unload: out 14 and 15); LO goes 15-23."""
    doc = lanes(("E", "A", "D", {"max_arrivals": 1}, 0),
                ("LO", "A", "D", {"max_arrivals": 1, "start_time": 1}, 5),
                ("HI", "A", "D", {"max_arrivals": 1, "start_time": 2,
                                  "entities_per_arrival": 2}, 0))
    results = api.run_model(doc, replications=1)
    assert times(results, "HI") == pytest.approx((12.0, 13.0))
    assert times(results, "LO") == pytest.approx((22.0, 22.0))


def test_loads_per_trip_is_nan_without_trips():
    doc = lanes(("X", "A", "D", {"max_arrivals": 1, "start_time": 5000}, 0))
    row = api.run_model(doc, replications=1)["fleet_summary"].iloc[0]
    assert row["Trips"] == 0 and math.isnan(row["Loads per Trip"])


@pytest.mark.parametrize("spec, bad", [({"loads": "2"}, True),   # loads 0: oracle case 5
                                       ({"loads": True}, True), ({"loads": 3}, False),
                                       ({"loads": 2, "kind": "worker"}, True),
                                       ({"loads": 1, "kind": "worker"}, False)])
def test_loads_validation(spec, bad):
    doc = lanes(("X", "A", "D", {"max_arrivals": 1}, 0), fleet_spec=fleet(**spec))
    codes = {p.code for p in schema.validate_layout_structured(doc)}
    assert ("E_FLEET_LOADS" in codes) == bad
