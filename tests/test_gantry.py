"""Gantry cranes: fleet `motion: "gantry"` (PHASE6 W3-M7, ARCHITECTURE D118).

Travel between two nodes is the direct Chebyshev hop `max(|dx| / speed_x,
|dy| / speed_y) + hoist_time`, by coordinates alone -- no path-finding, no
segment capacity. The independent oracle (`tests/test_gantry_oracle.py`) covers
the wire format and the basic timing/nearest/playback cases; this file covers
validation and the mechanisms the oracle does not reach: honest distance
reporting and segment capacity really not applying.
"""
import pytest

from nagasim import api
from nagasim.core import schema

from conftest import create, dispose, layout


def crane(name="Crane", speed_x=1.0, speed_y=1.0, hoist_time=0.0, home="Home", **overrides):
    base = {"name": name, "motion": "gantry", "speed": 1.0, "speed_x": speed_x,
            "speed_y": speed_y, "hoist_time": hoist_time, "home": home,
            "load_time": "0", "unload_time": "0", "park": "stay",
            "dispatch": "fifo", "select": "nearest"}
    base.update(overrides)
    return base


def station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": {"node": node}}


def transport(name, fleet_name, destination, priority=0):
    return {"name": name, "description": name, "type": "TRANSPORT", "position": [2, 0],
            "params": {"fleet": fleet_name, "destination": destination, "priority": priority}}


def lanes(*lanes, fleet_spec, network, **sections):
    """One CREATE -> STATION (pick-up) -> TRANSPORT per lane (destination
    named directly, no sequence needed); every drop station leads to DISPOSE."""
    blocks, links, drops = [dispose()], [], {}
    for tag, pick, drop, params, priority in lanes:
        blocks += [create(f"c_{tag}", e_type=tag, **params), station(f"S_{tag}", pick),
                   transport(f"t_{tag}", fleet_spec["name"], f"D_{drop}", priority)]
        links += [{"source": f"c_{tag}", "target": f"S_{tag}"},
                  {"source": f"S_{tag}", "target": f"t_{tag}"},
                  {"source": f"t_{tag}", "target": "dispose_1"}]
        drops[drop] = station(f"D_{drop}", drop)
    for block in drops.values():
        blocks.append(block)
        links.append({"source": block["name"], "target": "dispose_1"})
    return layout(blocks, links, network=network, fleets=[fleet_spec],
                  simulation={"time_limit": 1000, "warmup": 0, "replications": 1}, **sections)


def times(results, e_type="X"):
    rows = results["entity_summary"]
    row = rows[rows["Entity Type"] == e_type].iloc[0]
    return row["Min Time"], row["Max Time"]


def codes(fleet_spec, network):
    doc = layout([dispose()], [], network=network, fleets=[fleet_spec],
                 simulation={"time_limit": 10, "warmup": 0, "replications": 1})
    return {p.code for p in schema.validate_layout_structured(doc)}


NET_2 = {"nodes": [{"name": "Home", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 10}],
         "segments": []}


# -- validation ---------------------------------------------------------------

@pytest.mark.parametrize("spec, bad", [
    (crane(motion="teleport"), True),
    ({"name": "Crane", "motion": "gantry", "speed": 1.0, "home": "Home"}, True),  # no speed_x/y
    (crane(hoist_time=-1.0), True),
    (crane(kind="worker"), True),
    (crane(), False),
], ids=["unknown_motion", "no_axis_speeds", "negative_hoist", "worker", "valid"])
def test_motion_validation(spec, bad):
    assert ("E_FLEET_MOTION" in codes(spec, NET_2)) == bad


# -- honest distance reporting (Chebyshev-free) --------------------------------

def test_fleet_summary_distance_is_straight_line_not_chebyshev():
    """Dock == home (empty leg 0); Dock -> Drop is dx=30, dy=40 -- a 3-4-5
    triangle, so the honest straight-line distance is exactly 50, not the 40
    (max) a Chebyshev reading -- or the 70 a Manhattan one -- would report."""
    net = {"nodes": [{"name": "Dock", "x": 0, "y": 0}, {"name": "Drop", "x": 30, "y": 40}],
           "segments": []}
    fleet = crane(speed_x=10.0, speed_y=10.0, home="Dock")
    doc = lanes(("X", "Dock", "Drop", {"max_arrivals": 1}, 0), fleet_spec=fleet, network=net)
    row = api.run_model(doc, replications=1)["fleet_summary"].iloc[0]
    assert (row["Distance Empty"], row["Distance Loaded"]) == pytest.approx((0.0, 50.0))


# -- segment capacity does not apply (D117 stays off) --------------------------

NET_SHARED = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0}],
              "segments": [{"from": "A", "to": "B", "length": 999.0, "two_way": True,
                            "capacity": 1}]}


def test_gantry_units_are_not_counted_toward_the_deadlock_risk_warning():
    """A two-way capacity-1 segment with two GANTRY units must not warn: they
    never touch a segment (D117 is a network-motion mechanism only)."""
    fleet = crane(speed_x=10.0, speed_y=1.0, home="A",
                  units=[{"name": "C1", "home": "A"}, {"name": "C2", "home": "B"}])
    assert "W_SEGMENT_DEADLOCK_RISK" not in codes(fleet, NET_SHARED)


def test_two_gantry_cranes_cross_the_shared_segment_with_no_blocking():
    """C1 (home A) carries A -> B, C2 (home B) carries B -> A, at the same
    time, over a two-way capacity-1 segment that would deadlock two ordinary
    vehicles (D117's own motivating case). Both finish in exactly 1.0 (dx=10,
    speed_x=10, dy=0): no wait, no deadlock error."""
    fleet = crane(speed_x=10.0, speed_y=1.0, home="A",
                  units=[{"name": "C1", "home": "A"}, {"name": "C2", "home": "B"}])
    doc = lanes(("X", "A", "B", {"max_arrivals": 1}, 0), ("Y", "B", "A", {"max_arrivals": 1}, 0),
                fleet_spec=fleet, network=NET_SHARED)
    results = api.run_model(doc, replications=1)
    assert times(results, "X") == pytest.approx((1.0, 1.0))
    assert times(results, "Y") == pytest.approx((1.0, 1.0))


# The interrupted park-home return (position by interpolation) and a gantry
# multi-load tour are both asserted by test_regressions.py::test_194 (which
# imports crane/lanes/times from here).


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
