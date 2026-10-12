"""Independent oracle for batteries and charging (automation sub-project 2, D164-D166).

Written ONLY from docs/superpowers/specs/2026-09-30-batteries-and-charging-design.md and
the "Spec clarifications" / Task 1-4 "Interfaces" of the plan. No battery or charger
implementation was opened. Every expected number is hand-derived in the test's docstring.
`nagasim.core.battery` is imported INSIDE the tests so the file collects before it exists.

Shared model (hours, plain distance units): a two-node network dock(0,0) -- store(10,0),
length 10; vehicles at speed 10 (1 h per leg); load/unload 0 unless stated; entities go
Dock -> TRANSPORT -> Store. The first CREATE arrival is at t=0 (then every `gap`).

ASSUMPTIONS where the spec/plan is silent (the coordinator reconciles these):
 A1  Trace field mapping: a `battery`, `charge_start` or `charge_end` event carries its SoC
     (0-1) in `TraceEvent.value`; only `t` and `value` are asserted, never the charger name.
 A2  `charge_end` is also emitted (with the SoC reached) when an opportunity charge is
     interrupted by a dispatch.
 A3  `battery_summary` / `charging_summary` use exactly the plan's column names; "Avg Wait"
     is the mean over ALL sessions (a session that never waited counts 0).
 A4  `Min SoC` tracks the lowest settled SoC, negative values included (D166).
 A5  Constant strings ("0.25") are valid for `swap_time` and a capacity schedule's
     `[duration, value]` points are in model units; value > 0 means "window open".
 A6  A swapped-in pack is at SoC 1.0; spare packs start full; the removed pack recharges to
     1.0 at the station's power_kw with the taper. Whether that recharge starts when the swap
     starts or ends is not specified, so the spare-pack test accepts either.
 A7  Initial `battery.initial` applies to every vehicle of the fleet.
 A8  `Peak kW` = largest instantaneous sum of session powers at a session start.
"""
import math

import pytest

from conftest import create, dispose, layout

from nagasim import api
from nagasim.core.trace import TraceRecorder

NETWORK = {"nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "store", "x": 10, "y": 0}],
           "segments": [{"from": "dock", "to": "store", "length": 10}]}


# ------------------------------------------------------------------- builders
def _station(name, node, position):
    return {"name": name, "description": name, "type": "STATION", "position": list(position),
            "params": {"node": node}}


def _route():
    return {"name": "route_1", "description": "route_1", "type": "ROUTE", "position": [1, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                       "travel": "time", "speed": ""}}


def _transport():
    return {"name": "carry", "description": "carry", "type": "TRANSPORT", "position": [3, 0],
            "params": {"fleet": "AMR", "destination": "", "priority": 0}}


def charger(name, node="store", **kw):
    base = {"name": name, "node": node, "kind": "charge", "points": 1, "power_kw": 5.0,
            "knee": 0.8, "taper_floor": 0.1, "efficiency": 1.0}
    base.update(kw)
    return base


def model(battery, chargers, *, count=1, arrivals=1, gap=1.0, per_arrival=1, start_time=0,
          power=None, handle="0", schedules=None):
    fleet = {"name": "AMR", "count": count, "speed": 10.0, "speed_empty": 10.0,
             "load_time": handle, "unload_time": handle, "home": "dock",
             "power": power if power is not None else {"busy": 2.0},
             "battery": battery}
    doc = layout(
        [create(max_arrivals=arrivals, dist_params={"value": gap}, sequence="InOut",
                entities_per_arrival=per_arrival, start_time=start_time),
         _route(), _station("Dock", "dock", (2, 0)), _transport(),
         _station("Store", "store", (4, 0)), dispose()],
        [{"source": "create_1", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "Dock", "target": "carry"},
         {"source": "carry", "target": "dispose_1"},
         {"source": "Store", "target": "dispose_1"}],
        network=NETWORK, fleets=[fleet], chargers=chargers,
        sequences=[{"name": "InOut", "steps": [{"station": "Dock"}, {"station": "Store"}]}],
        simulation={"time_limit": 50, "warmup": 0, "replications": 1, "time_unit": "hours"})
    if schedules:
        doc["schedules"] = schedules
    return doc


def bat(**kw):
    """Battery block: 10 kWh, threshold strategy, low 0.5, target 0.9 unless overridden."""
    base = {"capacity_kwh": 10.0, "initial": 1.0, "low": 0.5, "target": 0.9,
            "strategy": "threshold"}
    base.update(kw)
    return base


def run(doc):
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    return results, recorder


def ev(recorder, kind):
    return sorted((e for e in recorder.events if e.kind == kind), key=lambda e: e.t)


def ts(events):
    return [e.t for e in events]


def vals(events):
    return [e.value for e in events]


# Hand constants (C=10 kWh, P=5 kW, knee 0.8, floor 0.1): taper factor above the knee is
# C(1-k)/(P(1-f)) = 10*0.2/(5*0.9) = 2/4.5, times ln(g(a)/g(b)), g(s) = 1 - 0.9(s-0.8)/0.2.
TAPER = 2.0 / 4.5
UP_TO_90 = TAPER * math.log(1.0 / 0.55)        # 0.8 -> 0.9: g(0.9) = 1 - 0.9*0.1/0.2 = 0.55
UP_TO_100 = TAPER * math.log(10.0)             # 0.8 -> 1.0: g(1.0) = 0.1


# ----------------------------------------------------------------- pure maths
def test_charge_time_below_across_and_above_the_knee():
    """C=10, P=5, k=0.8, f=0.1.
    below : 0.2 -> 0.6 is 4 kWh at 5 kW = 0.8 h.
    above : 0.8 -> 0.9: (2/4.5) * ln(1/0.55) = 0.444444 * 0.597837 = 0.265705 h
            (sanity: mean power ~ (5+2.75)/2 ~ 3.9 kW for 1 kWh ~ 0.26 h).
    across: 0.5 -> 1.0 = 0.3*10/5 (=0.6 h below the knee) + (2/4.5)*ln(10)
            = 0.6 + 1.023371 = 1.623371 h (finite at 100 %).
    no taper: knee=1.0 -> flat 5 kW: 0 -> 1 is 10/5 = 2 h."""
    from nagasim.core.battery import charge_hours

    assert charge_hours(10.0, 0.2, 0.6, 5.0) == pytest.approx(0.8)
    assert charge_hours(10.0, 0.8, 0.9, 5.0) == pytest.approx(UP_TO_90)
    assert UP_TO_90 == pytest.approx(0.265705, abs=1e-6)
    assert charge_hours(10.0, 0.5, 1.0, 5.0) == pytest.approx(0.6 + UP_TO_100)
    assert 0.6 + UP_TO_100 == pytest.approx(1.623371, abs=1e-6)
    assert charge_hours(10.0, 0.0, 1.0, 5.0, knee=1.0) == pytest.approx(2.0)
    assert charge_hours(10.0, 0.7, 0.7, 5.0) == 0.0


def test_power_and_soc_after_follow_the_taper():
    """P=5, k=0.8, f=0.1: power is 5 up to 0.8, then falls linearly to 0.1*5 = 0.5 at
    100 %: at 0.9 it is 5*(1 - 0.9*(0.1/0.2)) = 5*0.55 = 2.75. soc_after inverts the time:
    0.5 + 0.6 h*5/10 = 0.8 (exactly the knee); 0.2 + 0.4 h*5/10 = 0.4; 0.8 after
    UP_TO_90 hours is 0.9; a very long charge is capped at 1.0."""
    from nagasim.core.battery import power_at, soc_after

    assert power_at(0.5, 5.0) == pytest.approx(5.0)
    assert power_at(0.8, 5.0) == pytest.approx(5.0)
    assert power_at(0.9, 5.0) == pytest.approx(2.75)
    assert power_at(1.0, 5.0) == pytest.approx(0.5)
    assert soc_after(10.0, 0.5, 0.6, 5.0) == pytest.approx(0.8)
    assert soc_after(10.0, 0.2, 0.4, 5.0) == pytest.approx(0.4)
    assert soc_after(10.0, 0.8, UP_TO_90, 5.0) == pytest.approx(0.9)
    assert soc_after(10.0, 0.5, 100.0, 5.0) == pytest.approx(1.0)


def test_drain_kw_per_state():
    """Plan clarification: empty/loaded/handling draw power["busy"], idle draws
    power["idle"], charging draws 0, a missing key or no map draws 0."""
    from nagasim.core.battery import drain_kw

    power = {"busy": 3.0, "idle": 0.5}
    assert [drain_kw(power, s) for s in ("empty", "loaded", "handling")] == [3.0, 3.0, 3.0]
    assert drain_kw(power, "idle") == 0.5
    assert drain_kw(power, "charging") == 0.0
    assert drain_kw({"busy": 3.0}, "idle") == 0.0
    assert drain_kw(None, "empty") == 0.0


# ------------------------------------------------------------------- the runs
def test_drain_per_state_for_a_known_trip():
    """C=20 kWh, power busy 2 / idle 0.5, load = unload = 0.5 h, never below low (0.25).
    Job 1 (t=0, vehicle at dock): load 0.5 + loaded 1 + unload 0.5 = 2 h busy = 4 kWh
    -> SoC 1 - 4/20 = 0.8, released t=2. Idle 2 -> 3 = 1 h * 0.5 = 0.5 kWh.
    Job 2 (t=3, vehicle at store): empty 1 + load 0.5 + loaded 1 + unload 0.5 = 3 h
    = 6 kWh -> released t=6 at 0.8 - 0.025 - 0.3 = 0.475. Total drain 10.5 kWh."""
    doc = model(bat(capacity_kwh=20.0, low=0.25), [charger("c1", "dock")], arrivals=2, gap=3.0,
                power={"busy": 2.0, "idle": 0.5}, handle="0.5")
    results, rec = run(doc)
    batt = ev(rec, "battery")
    assert ts(batt) == pytest.approx([2.0, 6.0])
    assert vals(batt) == pytest.approx([0.8, 0.475])
    row = results["battery_summary"].iloc[0]
    assert row["Drain kWh"] == pytest.approx(10.5)
    assert row["Min SoC"] == pytest.approx(0.475)
    assert row["Charge Trips"] == 0


def test_threshold_charges_at_the_charger_then_serves_again():
    """C=10, busy 1.5 kW: initial 0.55, low 0.5, target 0.9, P=5 kW,
    charger at store (no drive). Job 1: loaded 1 h = 1.5 kWh -> 0.4 < low at t=1.
    Charge 0.4 -> 0.9: (0.8-0.4)*10/5 = 0.8 h + UP_TO_90 (0.265705) = 1.065705 h:
    charge_start t=1 (0.4), charge_end t=2.065705 (0.9), grid 5 kWh.
    Entity 2 arrives t=1.5, vehicle free only at 2.065705, then empty 1 h + loaded 1 h:
    delivered 4.065705 -> 2.565705 in system. Avg time (1 + 2.565705)/2.
    Job 2 drains 3 kWh -> 0.6 (>= low): no second charge."""
    doc = model(bat(initial=0.55), [charger("c1")], arrivals=2, gap=1.5,
                power={"busy": 1.5})
    results, rec = run(doc)
    end = 1.0 + 0.8 + UP_TO_90
    assert ts(ev(rec, "charge_start")) == pytest.approx([1.0])
    assert vals(ev(rec, "charge_start")) == pytest.approx([0.4])
    assert ts(ev(rec, "charge_end")) == pytest.approx([end])
    assert vals(ev(rec, "charge_end")) == pytest.approx([0.9])
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(
        (1.0 + (end + 2.0 - 1.5)) / 2)
    row = results["charging_summary"].iloc[0]
    assert row["Sessions"] == 1 and row["Grid kWh"] == pytest.approx(5.0)
    assert vals(ev(rec, "battery"))[-1] == pytest.approx(0.6)


def test_threshold_drives_to_a_far_charger_as_empty_travel():
    """C=10, busy 2 kW, initial 0.6, low 0.5, charger at dock, efficiency 0.8, P=5.
    Job: loaded 1 h = 2 kWh -> 0.4 < low at t=1 (at store). Drive to dock 1 h empty draws
    2 kWh -> 0.2, charge_start t=2. Charge 0.2 -> 0.9: 0.6*10/5 = 1.2 h + 0.265705 =
    1.465705 h -> charge_end t=3.465705. Battery energy 7 kWh -> grid 7/0.8 = 8.75.
    Distance Empty = the 10 driven to the charger (the job itself started at home)."""
    doc = model(bat(initial=0.6), [charger("c1", "dock", efficiency=0.8)])
    results, rec = run(doc)
    assert ts(ev(rec, "charge_start")) == pytest.approx([2.0])
    assert vals(ev(rec, "charge_start")) == pytest.approx([0.2])
    assert ts(ev(rec, "charge_end")) == pytest.approx([2.0 + 1.2 + UP_TO_90])
    assert results["charging_summary"].iloc[0]["Grid kWh"] == pytest.approx(8.75)
    assert results["fleet_summary"].iloc[0]["Distance Empty"] == pytest.approx(10.0)
    bs = results["battery_summary"].iloc[0]
    assert bs["Charge Trips"] == 1 and bs["Min SoC"] == pytest.approx(0.2)


@pytest.mark.parametrize("chargers, winner", [
    # least drive time wins even against the alphabetical order: store (0 h) beats dock (1 h)
    ([charger("a_dock", "dock"), charger("z_store", "store")], "z_store"),
    # equal drive and wait: ties by name
    ([charger("b", "store"), charger("a", "store")], "a"),
])
def test_charger_choice_is_least_drive_plus_wait_then_name(chargers, winner):
    """Same job as the threshold test (below low at store at t=1); one session, at the
    spec's chosen charger."""
    results, _ = run(model(bat(initial=0.6), chargers))
    sessions = results["charging_summary"].set_index("Charger")["Sessions"]
    assert sessions[winner] == 1 and sessions.sum() == 1


def test_two_vehicles_contend_for_one_charger_point():
    """2 vehicles at dock, 2 entities at t=0, one point at dock, P=5, C=10, initial 0.65,
    busy 2 kW. Each carries one load (1 h, 2 kWh -> 0.45 < low 0.5), is released at t=1 at
    store, drives back 1 h (-> 0.25) and reaches the charger at t=2. Charge 0.25 -> 0.9 =
    0.55*10/5 = 1.1 h + 0.265705 = 1.365705 h.
    Vehicle 1: start 2, end 3.365705. Vehicle 2 queues: start 3.365705, end 4.731410.
    Sessions 2, grid 2*6.5 = 13 kWh, Avg Wait (0 + 1.365705)/2 = 0.682853, Peak 5 kW."""
    doc = model(bat(initial=0.65), [charger("c1", "dock")], count=2, per_arrival=2)
    results, rec = run(doc)
    d = 1.1 + UP_TO_90
    assert ts(ev(rec, "charge_start")) == pytest.approx([2.0, 2.0 + d])
    assert ts(ev(rec, "charge_end")) == pytest.approx([2.0 + d, 2.0 + 2 * d])
    row = results["charging_summary"].iloc[0]
    assert row["Sessions"] == 2 and row["Grid kWh"] == pytest.approx(13.0)
    assert row["Avg Wait"] == pytest.approx(d / 2)
    assert row["Peak kW"] == pytest.approx(5.0)


def test_opportunity_charge_is_interrupted_by_a_dispatch():
    """C=10, busy 2 kW, initial 0.8, opportunity_below 0.8, low 0.25, charger at store,
    P=2 kW (stays under the knee). Job 1: loaded 1 h = 2 kWh -> 0.6 at t=1 (idle, below
    0.8, point free, no drive): charge_start t=1. Entity 2 arrives t=1.5: the vehicle is
    dispatchable (0.6 >= low) and leaves at once: charge_end t=1.5 with SoC
    0.6 + 2 kW*0.5 h/10 = 0.7. Then empty 1 h (-> 0.5) + loaded 1 h (-> 0.3) delivers at
    3.5, released with 0.3. Entity 2 never waits: times 1 and 2, mean 1.5."""
    doc = model(bat(initial=0.8, low=0.25, strategy="opportunity", opportunity_below=0.8),
                [charger("c1", power_kw=2.0)], arrivals=2, gap=1.5)
    results, rec = run(doc)
    starts, ends = ev(rec, "charge_start"), ev(rec, "charge_end")
    assert starts[0].t == pytest.approx(1.0) and ends[0].t == pytest.approx(1.5)
    assert ends[0].value == pytest.approx(0.7)
    assert results["entity_summary"].iloc[0]["Avg Time"] == pytest.approx(1.5)
    release = [e for e in ev(rec, "battery") if e.t == pytest.approx(3.5)]
    assert release and release[0].value == pytest.approx(0.3)


@pytest.mark.parametrize("spare_packs, ends", [
    (None, [1.25, 1.5]),       # unlimited packs: only the single swap point serialises them
    (1, None),                  # one spare: the second vehicle waits for a recharged pack
])
def test_swap_with_and_without_spare_packs(spare_packs, ends):
    """2 vehicles, C=10, initial 0.65, busy 2 kW, low 0.5: both released at t=1 at store
    with 0.45. Swap station at store: 1 point, swap_time 0.25 h, P=5. Swap sets SoC 1.0.
    No spare limit: swap 1 is 1 -> 1.25, swap 2 is 1.25 -> 1.5 (charge_end 1.25 and 1.5,
    both SoC 1.0). With spare_packs=1: vehicle 1 takes the only pack (ends 1.25) and its
    removed pack, 0.45 -> 1.0, needs R = 0.35*10/5 + (2/4.5) ln 10 = 0.7 + 1.023370 =
    1.723370 h. Vehicle 2's swap ends 0.25 h after that pack is back: between
    1.0 + R + 0.25 (recharge began at swap start) and 1.25 + R + 0.25 (at swap end)."""
    swap = charger("sw", kind="swap", swap_time="0.25", **(
        {"spare_packs": spare_packs} if spare_packs else {}))
    doc = model(bat(initial=0.65, strategy="swap"), [swap], count=2, per_arrival=2)
    _, rec = run(doc)
    got = ts(ev(rec, "charge_end"))
    assert vals(ev(rec, "charge_end")) == pytest.approx([1.0, 1.0])
    if ends is not None:
        assert got == pytest.approx(ends)
        return
    refill = 0.7 + UP_TO_100
    assert got[0] == pytest.approx(1.25)
    assert 1.0 + refill + 0.25 - 1e-9 <= got[1] <= 1.25 + refill + 0.25 + 1e-9


def test_scheduled_charge_ends_at_the_window_end():
    """Schedule "win": [[2,0],[1,1],[1000,0]] = open on [2,3). C=10, initial 0.7, busy 2 kW,
    low 0.25, charger at store, P=2 kW. Entity arrives t=1.5: loaded 1 h (2 kWh -> 0.5),
    vehicle freed at 2.5 inside the window and charges holding its slot. Reaching target
    0.9 would take (0.9-0.5)*10/2 = 2 h (all below the knee), but the window ends at 3:
    charge_end t=3.0 with SoC 0.5 + 2*0.5/10 = 0.6."""
    doc = model(bat(initial=0.7, low=0.25, strategy="scheduled", schedule="win"),
                [charger("c1", power_kw=2.0)], start_time=1.5,
                schedules=[{"name": "win", "kind": "capacity",
                            "points": [[2, 0], [1, 1], [1000, 0]]}])
    _, rec = run(doc)
    assert ts(ev(rec, "charge_start")) == pytest.approx([2.5])
    assert ts(ev(rec, "charge_end")) == pytest.approx([3.0])
    assert vals(ev(rec, "charge_end")) == pytest.approx([0.6])


def test_depleted_is_counted_once_per_crossing_and_the_run_completes():
    """C=1 kWh, busy 2 kW, no charger (W_NO_CHARGER is only a warning). Job 1: loaded 1 h =
    2 kWh -> SoC -1.0 (crossed 0 once, mid-drive). Job 2 (t=3, vehicle at store): empty 1 h
    + loaded 1 h = 4 kWh -> -5.0, already below 0 so no new crossing. Drain 6 kWh,
    Depleted 1, Min SoC -5, both entities delivered."""
    doc = model(bat(capacity_kwh=1.0, low=0.25), [], arrivals=2, gap=3.0)
    results, _ = run(doc)
    row = results["battery_summary"].iloc[0]
    assert row["Depleted"] == 1
    assert row["Drain kWh"] == pytest.approx(6.0)
    assert row["Min SoC"] == pytest.approx(-5.0)


def test_models_without_batteries_get_empty_fixed_column_frames():
    """Plan Task 4: both frames exist, are empty and keep their fixed columns."""
    doc = model(None, [])
    del doc["fleets"][0]["battery"], doc["chargers"]
    results, _ = run(doc)
    assert results["charging_summary"].empty and results["battery_summary"].empty
    assert {"Charger", "Kind", "Points", "Sessions", "Utilization %", "Avg Wait", "Avg Queue",
            "Grid kWh", "Peak kW", "Energy Cost"} <= set(results["charging_summary"].columns)
    assert {"Fleet", "Capacity kWh", "Charging %", "Charge Trips", "Min SoC",
            "Mean Final SoC", "Depleted", "Drain kWh"} <= set(results["battery_summary"].columns)
