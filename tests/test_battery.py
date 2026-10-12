"""Batteries and charging (automation sub-project 2, D164-D166)."""
import copy

import pytest

from nagasim.core.battery import charge_hours, soc_after


# -- Task 1: the math (the hand-checked values are in test_battery_oracle.py) ----

def test_across_knee_is_the_sum():
    assert charge_hours(10, 0.5, 0.9, 5) == pytest.approx(
        charge_hours(10, 0.5, 0.8, 5) + charge_hours(10, 0.8, 0.9, 5))


def test_soc_after_inverts_charge_hours():
    for s1 in (0.3, 0.8, 0.95, 1.0):
        assert soc_after(10, 0.1, charge_hours(10, 0.1, s1, 5), 5) == pytest.approx(s1)


def test_no_taper_when_knee_is_one_or_floor_is_one():
    assert charge_hours(10, 0.0, 1.0, 5, knee=1.0) == pytest.approx(2.0)
    assert charge_hours(10, 0.0, 1.0, 5, floor=1.0) == pytest.approx(2.0)


# -- Task 2: the chargers section and a fleet's battery ---------------------------

from nagasim.core import schema                                    # noqa: E402
from nagasim.core.document import ModelDocument                    # noqa: E402

from conftest import create, dispose, layout                       # noqa: E402

# A line A - B - C, 10 m apart, and an island no segment reaches.
NET = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0},
                 {"name": "C", "x": 20, "y": 0}, {"name": "island", "x": 0, "y": 50}],
       "segments": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]}


def amr(count=1, power=None, park="stay", **battery):
    """A fleet at A driving 10 m per hour, drawing 2 kW busy and nothing idle."""
    return {"name": "AMR", "count": count, "speed": 10.0, "home": "A", "park": park,
            "power": power or {"busy": 2.0},
            "battery": {"capacity_kwh": 10.0, "low": 0.3, **battery}}


def dock(name="dock", node="C", **fields):
    return {"name": name, "node": node, "points": 1, "power_kw": 5.0, **fields}


def station(name, node):
    return {"name": name, "description": name, "type": "STATION", "position": [1, 0],
            "params": {"node": node}}


def model(fleet, chargers, *, jobs=0, every=1.0, start=0.0, run=100.0, **sections):
    """*jobs* entities, one every *every* hours from *start*, carried A -> B."""
    blocks = [create(max_arrivals=jobs, dist_params={"value": every}, start_time=start),
              station("S_A", "A"),
              {"name": "move", "description": "move", "type": "TRANSPORT", "position": [2, 0],
               "params": {"fleet": "AMR", "destination": "S_B", "priority": 0}},
              station("S_B", "B"), dispose()]
    links = [{"source": "create_1", "target": "S_A"}, {"source": "S_A", "target": "move"},
             {"source": "move", "target": "dispose_1"},
             {"source": "S_B", "target": "dispose_1"}]
    return layout(blocks, links, network=NET, fleets=[fleet], chargers=chargers,
                  simulation={"time_limit": run, "warmup": 0, "replications": 1,
                              "time_unit": "hours"}, **sections)


def codes(doc):
    return {p.code for p in schema.validate_layout_structured(doc)}


WINDOW = {"name": "night", "kind": "capacity", "points": [[10, 0], [4, 1]], "repeat": True}


def test_a_valid_battery_model_has_no_problems():
    for doc in (model(amr(), [dock()]),
                model(amr(strategy="opportunity", opportunity_below=0.8), [dock()]),
                model(amr(strategy="swap"), [dock(kind="swap", swap_time="TRIA(2,3,5)",
                                                  swap_time_unit="minutes", spare_packs=2)]),
                model(amr(strategy="scheduled", schedule="night", chargers=["dock"]), [dock()],
                      schedules=[WINDOW])):
        assert schema.validate_layout_structured(doc) == []


@pytest.mark.parametrize("fleet, chargers, code", [
    (amr(capacity_kwh=0), [dock()], "E_BATTERY"),
    (amr(low=0.9, target=0.8), [dock()], "E_BATTERY"),
    (amr(initial=1.5), [dock()], "E_BATTERY"),
    (amr(strategy="opportunity"), [dock()], "E_BATTERY"),
    (amr(strategy="solar"), [dock()], "E_BATTERY"),
    (amr(strategy="scheduled"), [dock()], "E_BATTERY"),
    ({**amr(), "kind": "worker"}, [dock()], "E_BATTERY"),
    (amr(), [dock(node="nowhere")], "E_CHARGER"),
    (amr(), [dock(points=0)], "E_CHARGER"),
    (amr(), [dock(power_kw=0)], "E_CHARGER"),
    (amr(), [dock(knee=1.2)], "E_CHARGER"),
    (amr(), [dock(taper_floor=0)], "E_CHARGER"),
    (amr(), [dock(kind="swap")], "E_CHARGER"),
    (amr(), [dock(kind="swap", swap_time="TRIA(")], "E_CHARGER"),
    (amr(), [dock(), dock()], "E_CHARGER"),
    (amr(), [dock(name="AMR")], "E_CHARGER"),
    (amr(chargers=["nope"]), [dock()], "E_CHARGER_UNKNOWN"),
    (amr(strategy="scheduled", schedule="nope"), [dock()], "E_BATTERY_SCHEDULE"),
    (amr(strategy="swap"), [dock()], "W_NO_CHARGER"),
    (amr(), [dock(node="island")], "W_NO_CHARGER"),
    (amr(), [], "W_NO_CHARGER"),
])
def test_each_battery_problem_has_its_code(fleet, chargers, code):
    assert code in codes(model(fleet, chargers))


def test_an_unreachable_charger_names_the_node_to_join():
    # A library asset's charger sits at a node the user still has to join to the aisles.
    hints = [p.hint for p in schema.validate_layout_structured(
        model(amr(), [dock(node="island")])) if p.code == "W_NO_CHARGER"]
    assert hints == ["join node 'island' (dock) to the network the fleet drives on; until "
                     "then the vehicles never charge"]


def test_a_model_without_chargers_round_trips_unchanged():
    doc = model({"name": "AMR", "count": 1, "speed": 10.0, "home": "A"}, [])
    del doc["chargers"]
    saved = ModelDocument(doc).to_layout()
    assert saved["chargers"] == [] and "battery" not in saved["fleets"][0]
    assert ModelDocument(saved).to_layout() == saved
    assert codes(doc) == set()


# -- Task 3: the charging runtime ------------------------------------------------
# Every model: time in hours, the line A - B - C at 1 h per segment, jobs carried
# A -> B. A 10 kWh battery drawing 2 kW busy loses 0.2 SoC per segment driven;
# the dock at C gives 5 kW, i.e. 0.5 SoC per hour below the 0.8 knee.

import logging                                                     # noqa: E402

from nagasim import api                                            # noqa: E402
from nagasim.core.trace import TraceRecorder                       # noqa: E402

from conftest import run_env                                       # noqa: E402


def run(doc, run_len=100.0, warmup=0.0):
    """(fleet, {charger name: Charger}, trace events)."""
    recorder = TraceRecorder()
    env, _model = run_env(doc, run_len, warmup=warmup, subscribers=(recorder,))
    return env.ctx.fleets["AMR"], env.ctx.chargers, recorder.events


def at(events, kind):
    return [(e.t, e.value) for e in events if e.kind == kind]


def test_threshold_charges_after_the_job_that_crosses_low():
    # Job 1 ends at t=1 with 0.8. Job 2 (t=10): B -> A -> B, ends at t=12 with 0.4,
    # below low: drive to C (t=13, 0.2), charge to 0.9, then free again.
    fleet, chargers, events = run(model(amr(low=0.5, target=0.9), [dock()], jobs=2, every=10.0))
    hours = charge_hours(10, 0.2, 0.9, 5)
    assert at(events, "battery") == [(1.0, pytest.approx(0.8)), (12.0, pytest.approx(0.4))]
    assert at(events, "charge_start") == [(13.0, pytest.approx(0.2))]
    assert at(events, "charge_end") == [(pytest.approx(13.0 + hours), pytest.approx(0.9))]
    mover = fleet.movers[0]
    assert fleet._free == [mover] and mover.node == "C" and mover.charge_trips == 1
    assert mover.times["charging"] == pytest.approx(hours)
    assert chargers["dock"].grid_kwh == pytest.approx(7.0) and chargers["dock"].sessions == 1
    assert chargers["dock"].peak_kw == 5.0 and chargers["dock"].points.count == 0


def test_two_vehicles_queue_for_one_point_while_the_third_keeps_working():
    # Two entities at t=0 and two at t=2.5; three vehicles start at 0.6. The first two
    # reach C at t=2 with 0.2: one charges 2 -> 3.2 (to 0.8), the other waits for it.
    doc = model(amr(count=3, initial=0.6, low=0.5, target=0.8), [dock()], jobs=2, every=2.5)
    doc["blocks"][0]["params"]["entities_per_arrival"] = 2
    fleet, chargers, events = run(doc)
    starts, ends = at(events, "charge_start"), at(events, "charge_end")
    assert starts[0][0] == 2.0 and ends[0][0] == pytest.approx(3.2)
    assert starts[1][0] == pytest.approx(3.2)
    # The third vehicle delivered its job at t=3.5, while the other two were at C.
    assert (3.5, pytest.approx(0.4)) in at(events, "battery")
    assert chargers["dock"].wait.max == pytest.approx(1.2)
    assert len(fleet._free) == 3 and fleet.resource.count == 0       # all four jobs done


def test_a_dispatch_interrupts_an_opportunity_charge_and_settles_it():
    # Free at t=1 with 0.8 < 0.9: drive to C (t=2, 0.6), charge. Job 2 at t=2.3 takes
    # the vehicle off the charger: 0.3 h at 5 kW = 1.5 kWh, SoC 0.75.
    fleet, chargers, events = run(
        model(amr(low=0.1, strategy="opportunity", opportunity_below=0.9), [dock()],
              jobs=2, every=2.3), run_len=5.0)
    assert at(events, "charge_start")[0] == (2.0, pytest.approx(0.6))
    assert at(events, "charge_end")[0] == (2.3, pytest.approx(soc_after(10, 0.6, 0.3, 5)))
    assert chargers["dock"].grid_kwh == pytest.approx(1.5)
    assert chargers["dock"].points.count == 0 and chargers["dock"].running == []
    # C -> A -> B from t=2.3: delivered at 5.3 (the run stops at 5, on the loaded leg).
    assert fleet.movers[0].state == "loaded" and fleet.movers[0].session is None


def test_a_dispatch_on_the_way_to_an_opportunity_charge_diverts_the_vehicle():
    # Job 2 at t=1.5 finds the vehicle half-way B -> C: back to B (2.0), A (3.0), B (4.0).
    _fleet, chargers, events = run(
        model(amr(low=0.1, strategy="opportunity", opportunity_below=0.9), [dock()],
              jobs=2, every=1.5), run_len=4.5)
    assert at(events, "battery") == [(1.0, pytest.approx(0.8)), (4.0, pytest.approx(0.2))]
    assert at(events, "charge_start") == [] and chargers["dock"].sessions == 0


def test_a_swap_waits_for_the_first_pack_to_recharge():
    # Both vehicles reach C at t=2 with 0.2. One swaps 2 -> 2.5 with the only spare;
    # its old pack recharges 0.2 -> 1.0, and only then can the other swap.
    doc = model(amr(count=2, initial=0.6, low=0.5, strategy="swap"),
                [dock(kind="swap", swap_time="0.5", spare_packs=1)], jobs=1)
    doc["blocks"][0]["params"]["entities_per_arrival"] = 2
    fleet, chargers, events = run(doc)
    recharged = 2.5 + charge_hours(10, 0.2, 1.0, 5)
    assert at(events, "charge_start") == [(2.0, pytest.approx(0.2)),
                                          (pytest.approx(recharged), pytest.approx(0.2))]
    assert at(events, "charge_end") == [(2.5, 1.0), (pytest.approx(recharged + 0.5), 1.0)]
    assert chargers["dock"].grid_kwh == pytest.approx(16.0)         # two packs, 8 kWh each
    assert sorted(m.soc for m in fleet.movers) == [1.0, 1.0]


def test_a_scheduled_charge_stops_at_the_end_of_the_window():
    # The window is open 0.5 -> 2.5. Freed at t=1 (0.8 < target 1.0): at C at t=2 with
    # 0.6, and half an hour of charge (0.4 h to the knee, 0.1 h tapering) is all it leaves.
    night = {"name": "night", "kind": "capacity", "points": [[0.5, 0], [2.0, 1], [100, 0]]}
    fleet, _chargers, events = run(
        model(amr(low=0.1, strategy="scheduled", schedule="night"), [dock()], jobs=1,
              schedules=[night]))
    assert at(events, "charge_start") == [(2.0, pytest.approx(0.6))]
    assert at(events, "charge_end") == [(2.5, pytest.approx(soc_after(10, 0.6, 0.5, 5)))]
    assert fleet._free == fleet.movers


def test_a_window_opening_sends_idle_vehicles_to_charge():
    # No job at all: the vehicle stands at A with 0.5 until the window opens at t=1.
    night = {"name": "night", "kind": "capacity", "points": [[1.0, 0], [9.0, 1], [100, 0]]}
    fleet, _chargers, events = run(
        model(amr(initial=0.5, low=0.1, strategy="scheduled", schedule="night"), [dock()],
              start=1000.0, schedules=[night]))
    assert at(events, "charge_start") == [(3.0, pytest.approx(0.1))]
    assert at(events, "charge_end") == [(pytest.approx(3.0 + charge_hours(10, 0.1, 1.0, 5)),
                                         pytest.approx(1.0))]
    assert fleet.movers[0].homing is None and fleet.movers[0].state == "idle"
    # Playback sees the drive to the charger, although no block ordered it.
    assert [(e.t, e.path) for e in events if e.kind == "mover_moved" and e.duration] == [
        (1.0, ("A", "B", "C"))]


def test_a_battery_too_small_for_the_job_is_counted_depleted_and_the_run_completes(caplog):
    with caplog.at_level(logging.WARNING, logger="nagasim.core.movers"):
        fleet, _chargers, events = run(model(amr(capacity_kwh=1.0), [dock()], jobs=3, every=20.0))
    mover = fleet.movers[0]
    assert mover.depleted == 3 and mover.trips == 3 and mover.min_soc < 0
    assert len(at(events, "charge_end")) == 3 and mover.soc == pytest.approx(1.0)
    assert sum("W_BATTERY_DEPLETED" in r.message for r in caplog.records) == 1


def test_a_shift_takes_a_free_vehicle_never_the_one_charging_held():
    # Vehicle 0 charges 2 -> 3.2 holding its slot; at t=2.5 the shift drops to one.
    shift = {"name": "shift", "kind": "capacity", "points": [[2.5, 2], [100, 1]]}
    spec = {**amr(count=2, initial=0.6, low=0.5, target=0.8), "capacity_schedule": "shift"}
    fleet, _chargers, events = run(model(spec, [dock()], jobs=1, schedules=[shift]))
    assert [m.unit for m in fleet._down] == [1]
    assert at(events, "charge_end") == [(pytest.approx(3.2), pytest.approx(0.8))]
    assert fleet._free == [fleet.movers[0]]


def test_a_shift_may_take_an_opportunity_charging_vehicle_and_its_charge_ends():
    shift = {"name": "shift", "kind": "capacity", "points": [[2.3, 1], [100, 0]]}
    spec = {**amr(low=0.1, strategy="opportunity", opportunity_below=0.9),
            "capacity_schedule": "shift"}
    fleet, chargers, events = run(model(spec, [dock()], jobs=1, schedules=[shift]))
    assert at(events, "charge_end") == [(2.3, pytest.approx(0.75))]
    assert fleet._down == fleet.movers and chargers["dock"].points.count == 0


def test_warm_up_during_a_charge_resets_the_counters_and_keeps_the_charge():
    doc = model(amr(low=0.5, target=0.9), [dock()], jobs=2, every=10.0)
    fleet, chargers, events = run(doc, warmup=13.5)
    mover = fleet.movers[0]
    assert at(events, "charge_end") == [(pytest.approx(13.0 + charge_hours(10, 0.2, 0.9, 5)),
                                         pytest.approx(0.9))]
    assert mover.charge_trips == 0 and mover.drain_kwh == 0.0 and mover.trips == 0
    assert mover.min_soc == pytest.approx(0.2)        # as it stood at the reset
    # Half an hour of the charge (0.2 -> 0.45) was warm-up: 10 x (0.9 - 0.45) kWh remain.
    assert chargers["dock"].grid_kwh == pytest.approx(4.5)


def test_no_reachable_charger_leaves_the_vehicle_free_and_logs_once(caplog):
    doc = model(amr(low=0.9), [dock(node="island")], jobs=3, every=5.0)
    with caplog.at_level(logging.WARNING, logger="nagasim.core.movers"):
        fleet, chargers, events = run(doc)
    assert fleet._free == fleet.movers and fleet.movers[0].trips == 3
    assert at(events, "charge_start") == [] and chargers["dock"].sessions == 0
    assert sum("W_NO_CHARGER" in r.message for r in caplog.records) == 1


def test_a_battery_fleet_draws_no_grid_power_of_its_own():
    fleet, _chargers, _events = run(model(amr(), [dock()], jobs=1))
    assert fleet.resource.power is None and fleet.power == {"busy": 2.0}


NO_BATTERY = {"name": "AMR", "count": 2, "speed": 10.0, "home": "A", "park": "home",
              "load_time": "0.1", "unload_time": "0.2", "power": {"busy": 2.0, "idle": 0.5}}


NO_BATTERY_ROW = {
    "Run": 0, "Fleet": "AMR", "Vehicles": 2, "Trips": 5, "Loads per Trip": 1.0,
    "Distance Loaded": 50.0, "Distance Empty": 50.0, "Travel Loaded %": 30.12048192771084,
    "Travel Empty %": 30.12048192771085, "Load/Unload %": 9.036144578313252,
    "Idle %": 30.722891566265066, "Avg Request Wait": 0.0, "Max Request Wait": 0.0}
NO_BATTERY_ENERGY = {
    "Run": 0, "Resource": "AMR", "Busy kWh": 13.0, "Idle kWh": 5.05, "Blocked kWh": 0.0,
    "Down kWh": 0.0, "Off Shift kWh": 0.0, "Total kWh": 18.05, "Avg kW": 2.1747,
    "Energy Cost": 0.0}


def test_a_fleet_without_a_battery_reports_what_it_always_did():
    # The expected values were recorded on the code BEFORE batteries existed.
    results = api.run_model(model(dict(NO_BATTERY), [], jobs=5, every=1.5, run=20.0),
                            replications=1)
    row = results["fleet_summary"].iloc[0].to_dict()
    assert row == NO_BATTERY_ROW
    energy = results["energy_summary"]
    assert energy[energy["Resource"] == "AMR"].iloc[0].to_dict() == NO_BATTERY_ENERGY


# -- Task 4: the frames ------------------------------------------------------------

CHARGING_COLS = ["Run", "Charger", "Kind", "Points", "Sessions", "Utilization %", "Avg Wait",
                 "Avg Queue", "Grid kWh", "Peak kW", "Energy Cost"]
BATTERY_COLS = ["Run", "Fleet", "Capacity kWh", "Charging %", "Charge Trips", "Min SoC",
                "Mean Final SoC", "Depleted", "Drain kWh"]


def test_both_frames_are_empty_with_fixed_columns_without_batteries():
    results = api.run_model(model(dict(NO_BATTERY), [], jobs=1, run=5.0), replications=1)
    for key, cols in (("charging_summary", CHARGING_COLS), ("battery_summary", BATTERY_COLS)):
        assert results[key].empty and list(results[key].columns) == cols


def test_the_threshold_model_reports_its_charge_and_its_grid_energy():
    # The Task 3 threshold timeline: one 7 kWh session at 0.2 per kWh. The arrivals run
    # out, so the observed window ends when the charge does, at 13 h + the charge.
    doc = model(amr(low=0.5, target=0.9), [dock(efficiency=0.875)], jobs=2, every=10.0, run=30.0)
    doc["simulation"]["energy_price"] = 0.2
    results = api.run_model(doc, replications=1)
    hours = charge_hours(10, 0.2, 0.9, 5)
    assert results["charging_summary"].iloc[0].to_dict() == pytest.approx({
        "Run": 0, "Charger": "dock", "Kind": "charge", "Points": 1, "Sessions": 1,
        "Utilization %": 100 * hours / (13 + hours), "Avg Wait": 0.0, "Avg Queue": 0.0,
        "Grid kWh": 8.0, "Peak kW": 5.0, "Energy Cost": 1.6})        # 7 kWh / 0.875
    assert results["battery_summary"].iloc[0].to_dict() == pytest.approx({
        "Run": 0, "Fleet": "AMR", "Capacity kWh": 10.0, "Charging %": 100 * hours / (13 + hours),
        "Charge Trips": 1, "Min SoC": 0.2, "Mean Final SoC": 0.9, "Depleted": 0,
        "Drain kWh": 8.0})                 # 1 + 2 + 1 segments at 2 kWh each
    # The grid sees the charger, not the vehicles: one energy row, joined to the costs.
    energy = results["energy_summary"]
    assert list(energy["Resource"]) == ["dock"]
    assert energy.iloc[0][["Busy kWh", "Total kWh", "Energy Cost"]].tolist() == [8.0, 8.0, 1.6]
    system = results["system"].set_index("Metric")["Value"]
    assert system["Total Cost"] == pytest.approx(1.6)
    assert results["fleet_summary"].iloc[0]["Trips"] == 2


def test_a_charge_running_at_the_end_reports_the_soc_and_the_energy_reached_so_far():
    # The run stops at t=14, one hour into the charge from 0.2: 0.7, 5 kWh from the grid.
    doc = model(amr(low=0.5, target=0.9), [dock()], jobs=2, every=10.0, run=14.0)
    results = api.run_model(doc, replications=1)
    row = results["battery_summary"].iloc[0]
    assert row["Mean Final SoC"] == pytest.approx(0.7) and row["Min SoC"] == pytest.approx(0.2)
    assert results["charging_summary"].iloc[0]["Grid kWh"] == pytest.approx(5.0)
    assert results["energy_summary"].iloc[0]["Total kWh"] == pytest.approx(5.0)


# -- D167: a charger clones per submodel instance ----------------------------------

from nagasim.core import submodels                                 # noqa: E402

BAY = {"parameters": [{"name": "points", "default": 1, "type": "int"}],
       "scoped": {"chargers": {"dock": {"points": {"$arg": "points"}}}},
       "blocks": [], "links": []}


def bays(*instances, **kwargs):
    """The A -> B model with one `Bay` instance per (name, points): each brings a
    charger at its own node `<name>/plug`, 10 m beyond C."""
    doc = copy.deepcopy(model(amr(), [dock(node="$plug")], **kwargs))      # NET is shared
    doc["submodels"] = {"Bay": BAY}
    for name, points in instances:
        doc["blocks"].append({"name": name, "description": name, "type": "SUBMODEL",
                              "position": [0, 5],
                              "params": {"definition": "Bay", "arguments": {"points": points}}})
        doc["network"]["nodes"].append({"name": f"{name}/plug", "x": 30, "y": 0})
        doc["network"]["segments"].append({"from": "C", "to": f"{name}/plug"})
    return doc


def test_a_scoped_charger_clones_per_instance_and_the_template_is_consumed():
    flat = submodels.flatten(bays(("bay_1", 1), ("bay_2", 3)))
    assert [(c["name"], c["node"], c["points"]) for c in flat["chargers"]] == [
        ("bay_1/dock", "bay_1/plug", 1), ("bay_2/dock", "bay_2/plug", 3)]
    assert schema.validate_layout_structured(flat) == []


def test_chargers_no_definition_names_flatten_unchanged():
    doc = model(amr(), [dock()])
    assert submodels.flatten(doc)["chargers"] == doc["chargers"]


def test_a_battery_fleet_charges_at_a_cloned_charger():
    frame = api.run_model(bays(("bay_1", 1), jobs=4, every=2.0, run=40.0),
                          replications=1)["charging_summary"]
    assert list(frame["Charger"]) == ["bay_1/dock"] and frame["Sessions"].iloc[0] >= 1
