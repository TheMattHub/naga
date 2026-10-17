"""A battery on every fleet kind (D602): workers (D114) and gantries (D118) drain
and charge exactly as vehicles do (D164-D166); `capacity_kwh` <= 0 or no `battery`
is a fleet without one (D601)."""
import pytest

from nagasim import api
from nagasim.core import schema

from conftest import chain, create, dispose, layout, process, run_env

NET = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0},
                 {"name": "C", "x": 20, "y": 0}],
       "segments": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]}
SIM = {"time_limit": 200.0, "warmup": 0, "replications": 1, "time_unit": "hours"}
# knee 1: POWER() of a tapering charger is held between events (engine.md 5.26 ceiling).
DOCK = {"name": "dock", "node": "C", "points": 1, "power_kw": 5.0, "knee": 1}
SWAP = {"name": "dock", "node": "C", "points": 1, "power_kw": 5.0, "kind": "swap",
        "swap_time": "0.2", "spare_packs": 2}
NIGHT = {"name": "night", "kind": "capacity", "points": [[5, 0], [5, 1]], "repeat": True}


def battery(**fields):
    return {"capacity_kwh": 10.0, "low": 0.4, "target": 0.9, **fields}


def worker_fleet(**spec):
    """One worker at A walking 10 m/h, 2 kW busy, 0.2 kW idle."""
    return {"name": "W", "kind": "worker", "speed": 10.0, "home": "A",
            "units": [{"name": "Ann"}], "power": {"busy": 2.0, "idle": 0.2}, **spec}


def worker_model(fleet, chargers=(DOCK,), jobs=40, **sections):
    """A job every 3 h at B: walk A/C -> B, 1 h of work there."""
    blocks = [create(max_arrivals=jobs, dist_params={"value": 3.0}),
              process(resource="", value=1.0, worker_fleet="W", node="B"), dispose()]
    return layout(blocks, chain(*blocks), network=NET, fleets=[fleet], chargers=list(chargers),
                  simulation=SIM, **sections)


def crane(**spec):
    return {"name": "Crane", "motion": "gantry", "speed": 1.0, "speed_x": 10.0, "speed_y": 10.0,
            "hoist_time": 0.1, "home": "A", "load_time": "0.2", "unload_time": "0.2",
            "park": "stay", "power": {"busy": 1.0, "idle": 0.2}, **spec}


def gantry_model(fleet, jobs=40):
    """A move A -> B every 3 h; the dock at C is reached by a gantry hop."""
    station = lambda name, node: {"name": name, "description": name, "type": "STATION",
                                  "position": [1, 0], "params": {"node": node}}
    blocks = [create(max_arrivals=jobs, dist_params={"value": 3.0}), station("S_A", "A"),
              {"name": "move", "description": "move", "type": "TRANSPORT", "position": [2, 0],
               "params": {"fleet": "Crane", "destination": "S_B", "priority": 0}},
              station("S_B", "B"), dispose()]
    links = [{"source": "create_1", "target": "S_A"}, {"source": "S_A", "target": "move"},
             {"source": "move", "target": "dispose_1"}, {"source": "S_B", "target": "dispose_1"}]
    return layout(blocks, links, network={**NET, "segments": []}, fleets=[fleet],
                  chargers=[DOCK], simulation=SIM)


def run(doc, fleet_name):
    """(fleet, chargers, end time, [(event, t, resource, unit)])."""
    seen = []
    record = lambda name, **kw: seen.append((name, kw.get("t"), kw.get("resource"),
                                             kw.get("unit")))
    env, _model = run_env(doc, SIM["time_limit"], subscribers=(record,))
    return env.ctx.fleets[fleet_name], env.ctx.chargers, env.now, seen


def intervals(seen, start, end, resource=None):
    out, opened = [], None
    for name, t, res, _unit in seen:
        if resource is not None and res != resource:
            continue
        if name == start:
            opened = t
        elif name == end and opened is not None:
            out.append((opened, t))
            opened = None
    return out


def assert_balanced(fleet, chargers, t_end):
    totals = fleet.battery_totals(t_end)
    charged = sum(c.energy(t_end) * c.efficiency for c in chargers.values())
    mover = fleet.movers[0]
    assert totals["min_soc"] >= 0 and totals["depleted"] == 0
    assert totals["charge_trips"] > 0 and totals["drain_kwh"] > 0
    assert totals["drain_kwh"] == pytest.approx(
        charged + (fleet.initial_soc - totals["final_soc"]) * fleet.capacity_kwh)
    assert mover.drain_kwh == totals["drain_kwh"]


def test_worker_fleet_battery_drains_and_charges():
    doc = worker_model(worker_fleet(battery=battery()))
    assert schema.validate_layout_structured(doc) == []
    fleet, chargers, t_end, seen = run(doc, "W")
    assert_balanced(fleet, chargers, t_end)
    # Never charging while it holds a work item: the one unit's holds and its
    # charges never overlap.
    holds = intervals(seen, "entity_seized", "entity_released", "W")
    charges = intervals(seen, "charge_start", "charge_end")
    assert len(holds) >= 30 and charges
    for c0, c1 in charges:
        assert all(c1 <= h0 or c0 >= h1 for h0, h1 in holds), (c0, c1)


@pytest.mark.parametrize("fields, chargers, sections", [
    ({"strategy": "opportunity", "opportunity_below": 0.8, "low": 0.2}, (DOCK,), {}),
    ({"strategy": "swap"}, (SWAP,), {}),
    ({"strategy": "scheduled", "schedule": "night", "low": 0.2}, (DOCK,),
     {"schedules": [NIGHT]}),
], ids=["opportunity", "swap", "scheduled"])
def test_every_strategy_applies_to_a_worker_fleet(fields, chargers, sections):
    doc = worker_model(worker_fleet(battery=battery(**fields)), chargers, **sections)
    assert schema.validate_layout_structured(doc) == []
    fleet, _chargers, t_end, seen = run(doc, "W")
    holds = intervals(seen, "entity_seized", "entity_released", "W")
    charges = intervals(seen, "charge_start", "charge_end")
    assert len(holds) >= 30 and charges
    assert fleet.battery_totals(t_end)["charge_trips"] > 0
    for c0, c1 in charges:
        assert all(c1 <= h0 or c0 >= h1 for h0, h1 in holds), (c0, c1)


def test_gantry_fleet_battery_drains_and_charges():
    doc = gantry_model(crane(battery=battery()))
    assert schema.validate_layout_structured(doc) == []
    fleet, chargers, t_end, seen = run(doc, "Crane")
    assert_balanced(fleet, chargers, t_end)
    # The crane reached the dock by a direct hop to C's coordinates.
    assert any(name == "charge_start" for name, *_ in seen)
    assert fleet.movers[0].distance_empty > 0


@pytest.mark.parametrize("make", [
    lambda b: worker_model(worker_fleet(**b), chargers=()),
    lambda b: gantry_model(crane(**b)),
], ids=["worker", "gantry"])
def test_battery_off_is_unchanged_for_workers_and_gantries(make):
    off = api.run_model(make({"battery": {"capacity_kwh": 0, "low": 0.3}}), replications=1)
    none = api.run_model(make({}), replications=1)
    for frame in ("fleet_summary", "energy_summary", "entity_summary", "resource_summary"):
        if frame in none:
            assert off[frame].equals(none[frame]), frame
    assert off["battery_summary"].empty and none["battery_summary"].empty


def test_power_of_a_battery_worker_fleet_is_zero_and_its_charger_carries_the_energy():
    def metered(rate):
        doc = worker_model(worker_fleet(battery=battery()),
                           levels=[{"name": "Meter", "initial": 0}],
                           flows=[{"name": "draw", "to": "Meter", "rate": rate}])
        results = api.run_model(doc, seed=0)
        flows = results["flow_summary"]
        return float(flows[flows["Flow"] == "draw"].iloc[0]["Total"]), results

    fleet_total, _ = metered('POWER("W")')
    assert fleet_total == 0.0
    dock_total, results = metered('POWER("dock")')
    energy = results["energy_summary"]
    dock_kwh = float(energy[energy["Resource"] == "dock"].iloc[0]["Total kWh"])
    assert dock_kwh > 0 and dock_total == pytest.approx(dock_kwh, abs=1e-3)   # frame: 4 decimals


@pytest.mark.parametrize("doc, kind", [
    (lambda: worker_model(worker_fleet(battery=battery())), "a worker fleet"),
    (lambda: gantry_model(crane(battery=battery())), "a gantry"),
], ids=["worker", "gantry"])
def test_a_state_of_a_battery_worker_or_gantry_is_refused_by_name(doc, kind):
    """A vehicle record cannot carry these fleets (D386), battery or not: refused, not lost."""
    from nagasim import state
    with pytest.raises(state.StateError, match=kind):
        state.take(doc(), 50.0)


def test_679_an_idle_worker_charges():
    # 0.2 kW idle from 1.0: 0.5 at t=25, walks to C (2 h at 2 kW): 0.1 there at t=27.
    doc = worker_model(worker_fleet(battery=battery(low=0.5)), jobs=1)
    doc["blocks"][0]["params"]["start_time"] = 1000.0
    fleet, _chargers, t_end, seen = run(doc, "W")
    assert [t for name, t, _r, _u in seen if name == "charge_start"][0] == pytest.approx(27.0)
    totals = fleet.battery_totals(t_end)
    assert totals["depleted"] == 0 and totals["min_soc"] > 0 and totals["charge_trips"] >= 2


def test_679_an_idle_gantry_charges():
    # 0.2 kW idle from 1.0: 0.4 at t=30, then one hop to C: 20 m at 10 m/h + 0.1 h hoist.
    doc = gantry_model(crane(battery=battery()), jobs=1)
    doc["blocks"][0]["params"]["start_time"] = 1000.0
    fleet, _chargers, t_end, seen = run(doc, "Crane")
    starts = [t for name, t, _r, _u in seen if name == "charge_start"]
    assert starts[0] == pytest.approx(32.1)
    totals = fleet.battery_totals(t_end)
    assert totals["depleted"] == 0 and totals["charge_trips"] >= 2
