"""POWER("name") feeds a resource's, fleet's or charger's kW to a flow (D600).

Each model: one CREATE, one PROCESS on "M" with power busy 10 kW / idle 2 kW,
and a flow `rate: POWER("M")` into a level. Integrating that rate must give
exactly the kWh `energy_summary` reports, because both read the same per-state
partition (`outputs._resource_energy_rows`, `outputs.power_now`).
"""
import pytest

from conftest import chain, create, dispose, layout, process
from nagasim import api
from nagasim.core.context import SimulationContext
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import setup_run


def _model(rate='POWER("M")', time_unit="hours", run=10.0, warmup=0.0, every=2.0, service=1.5):
    blocks = [create(dist_params={"value": every}),
              process(resource="M", value=service), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "M", "capacity": 1, "power": {"busy": 10, "idle": 2}}],
                  levels=[{"name": "Meter", "initial": 0}],
                  flows=[{"name": "draw", "to": "Meter", "rate": rate}],
                  simulation={"time_limit": run, "warmup": warmup, "replications": 1,
                              "time_unit": time_unit})


def _row(df, col, value):
    hit = df[df[col] == value]
    assert len(hit) == 1
    return hit.iloc[0]


def _kwh(results, name="M"):
    return float(_row(results["energy_summary"], "Resource", name)["Total kWh"])


def test_power_flow_matches_energy_summary():
    results = api.run_model(_model(), seed=0)
    meter = float(_row(results["level_summary"], "Level", "Meter")["Final"])
    assert _kwh(results) > 0
    assert meter == pytest.approx(_kwh(results), abs=1e-6)


def test_power_flow_matches_energy_summary_in_minutes():
    results = api.run_model(_model('POWER("M") / 60', "minutes", run=600.0, every=120.0,
                                   service=90.0), seed=0)
    meter = float(_row(results["level_summary"], "Level", "Meter")["Final"])
    assert meter == pytest.approx(_kwh(results), abs=1e-6)


def test_power_flow_respects_warmup():
    results = api.run_model(_model(warmup=3.0), seed=0)
    total = float(_row(results["flow_summary"], "Flow", "draw")["Total"])
    assert total == pytest.approx(_kwh(results), abs=1e-6)


def test_power_flow_follows_the_blocked_state():
    """A blocked unit draws its own "blocked" kW: M1 (1 h) feeds M2 (2 h) through
    a zero-capacity buffer, so M1 alternates busy and blocked (energy oracle case 5)."""
    src = create("src", dist_params={"value": 0.0}, max_arrivals=20)
    blocks = [src, process("m1", resource="M1", value=1.0),
              {"name": "buf", "description": "buf", "type": "BUFFER", "position": [2, 0],
               "params": {"capacity": 0}},
              process("m2", resource="M2", value=2.0), dispose("out")]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "M1", "capacity": 1, "power": {"busy": 5, "blocked": 1}},
                              {"name": "M2", "capacity": 1}],
                   levels=[{"name": "Meter", "initial": 0}],
                   flows=[{"name": "draw", "to": "Meter", "rate": 'POWER("M1")'}],
                   simulation={"time_limit": 11.0, "warmup": 0, "replications": 1,
                               "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    assert _kwh(results, "M1") == pytest.approx(6 * 5 + 5 * 1)
    total = float(_row(results["flow_summary"], "Flow", "draw")["Total"])
    assert total == pytest.approx(_kwh(results, "M1"), abs=1e-6)


# -- a battery fleet charging at an untapered dock -------------------------------
NET = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 10, "y": 0},
                 {"name": "C", "x": 20, "y": 0}],
       "segments": [{"from": "A", "to": "B"}, {"from": "B", "to": "C"}]}


def _battery_model(rate):
    """Two jobs A -> B at 10 m/h; the second drops the 10 kWh battery below low,
    so the vehicle drives to the 5 kW dock at C (knee 1: no taper) and charges."""
    station = lambda name, node: {"name": name, "description": name, "type": "STATION",
                                  "position": [1, 0], "params": {"node": node}}
    blocks = [create(max_arrivals=2, dist_params={"value": 10.0}), station("S_A", "A"),
              {"name": "move", "description": "move", "type": "TRANSPORT", "position": [2, 0],
               "params": {"fleet": "AMR", "destination": "S_B", "priority": 0}},
              station("S_B", "B"), dispose()]
    links = [{"source": "create_1", "target": "S_A"}, {"source": "S_A", "target": "move"},
             {"source": "move", "target": "dispose_1"}, {"source": "S_B", "target": "dispose_1"}]
    return layout(blocks, links, network=NET,
                  fleets=[{"name": "AMR", "count": 1, "speed": 10.0, "home": "A", "park": "stay",
                           "power": {"busy": 2.0},
                           "battery": {"capacity_kwh": 10.0, "low": 0.5, "target": 0.9}}],
                  chargers=[{"name": "dock", "node": "C", "points": 1, "power_kw": 5.0,
                             "knee": 1, "efficiency": 0.8}],
                  levels=[{"name": "Meter", "initial": 0}],
                  flows=[{"name": "draw", "to": "Meter", "rate": rate}],
                  simulation={"time_limit": 100.0, "warmup": 0, "replications": 1,
                              "time_unit": "hours"})


def test_power_charger_matches_charger_energy():
    results = api.run_model(_battery_model('POWER("dock")'), seed=0)
    total = float(_row(results["flow_summary"], "Flow", "draw")["Total"])
    assert _kwh(results, "dock") > 0
    assert total == pytest.approx(_kwh(results, "dock"), abs=1e-6)


def test_power_battery_fleet_is_zero():
    results = api.run_model(_battery_model('POWER("AMR")'), seed=0)
    flow = _row(results["flow_summary"], "Flow", "draw")
    assert float(flow["Total"]) == 0.0 and float(flow["Peak Rate"]) == 0.0


def test_power_unknown_name_is_a_problem():
    found = [p for p in validate_model_structured(_model('POWER("Nope")'))
             if p.code == "E_POWER_UNKNOWN"]
    assert len(found) == 1 and "M" in found[0].hint


def test_power_of_a_computed_unknown_name_fails_the_run():
    with pytest.raises(Exception, match="unknown resource, fleet or charger 'Nope'"):
        api.run_model(_model('POWER("No" + "pe")'), seed=0)


# -- no POWER read, no extra wake-up ---------------------------------------------
def _events(doc, run=10.0):
    env, _model = setup_run(doc, seed=0, run_len=run, warmup=0.0)
    count = 0
    while env.peek() <= run:
        env.step()
        count += 1
    return count, env.ctx.level_system


def test_power_without_reader_adds_no_wakeups(monkeypatch):
    plain, system = _events(_model(rate="2"))
    assert system.reads_power is False
    powered, system = _events(_model())
    assert system.reads_power is True
    # Switch the seize/release wake-up off: the plain model must not notice,
    # the POWER model must (so the switch is the hook it claims to be).
    monkeypatch.setattr(SimulationContext, "wake_power", lambda self: None)
    assert _events(_model(rate="2"))[0] == plain
    assert _events(_model())[0] < powered
