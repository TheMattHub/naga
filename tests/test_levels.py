"""Levels, flows and time-series inputs (PHASE4 D1/D2).

Every assertion here is EXACT or hand-computed: the whole point of the design
is that a piecewise-constant rate makes a level piecewise linear, so there is a
closed form for every number and nothing needs a statistical tolerance.
"""
from pathlib import Path

import pytest

from nagasim.api import run_model
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until
from nagasim.core.timeseries_inputs import (
    SeriesError, build_series, series_csv, series_from_csv,
)
from conftest import chain, create, dispose, layout as make_layout, metric_value, run_env

_MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _run(layout, run_len, warmup=0.0, seed=0):
    """Prepare and run one replication in-process; returns the env."""
    env, _model = run_env(layout, run_len, seed=seed, warmup=warmup)
    return env


def _level(env, name):
    return env.ctx.level_system.levels[name]


def _flow(env, name):
    return env.ctx.level_system.flow_index[name]


def _minimal(**sections):
    """A layout whose only entity work is one CREATE -> DISPOSE pair: the
    levels run on their own process, so the discrete side just has to exist."""
    c, d = create(max_arrivals=1), dispose()
    return make_layout([c, d], chain(c, d), **sections)


# =========================================================== time-series inputs
def test_step_and_linear_interpolation_and_the_three_end_rules():
    entry = {"name": "p", "start": 0, "step": 10, "values": [1.0, 3.0, 2.0]}
    step = build_series(entry)
    assert [step.value_at(t) for t in (-5, 0, 9.999, 10, 15, 20, 25)] == \
           [1.0, 1.0, 1.0, 3.0, 3.0, 2.0, 2.0]

    linear = build_series({**entry, "interpolation": "linear"})
    assert linear.value_at(5) == pytest.approx(2.0)          # halfway 1 -> 3
    assert linear.value_at(15) == pytest.approx(2.5)         # halfway 3 -> 2

    # Past the end: hold the last value, cycle with the period, or refuse.
    assert step.value_at(1000) == 2.0                         # "hold" is the default
    repeat = build_series({**entry, "beyond": "repeat"})      # period = 3 * 10 = 30
    assert [repeat.value_at(t) for t in (30, 40, 50, 65)] == [1.0, 3.0, 2.0, 1.0]
    with pytest.raises(SeriesError):
        build_series({**entry, "beyond": "error"}).value_at(31)


def test_next_change_is_the_exact_instant_the_value_moves():
    """What `core/levels.py` schedules on, so it must be exact, including the
    wrap of a repeating daily profile."""
    step = build_series({"name": "p", "start": 0, "step": 10, "values": [1.0, 3.0, 2.0]})
    assert step.next_change(0) == 10
    assert step.next_change(9.5) == 10
    assert step.next_change(10) == 20
    assert step.next_change(20) is None                       # "hold": never again

    repeat = build_series({"name": "p", "start": 0, "step": 10, "values": [1.0, 3.0, 2.0],
                           "beyond": "repeat"})
    assert repeat.next_change(20) == 30                       # the wrap point
    assert repeat.next_change(35) == 40


def test_a_series_round_trips_through_csv():
    entry = {"name": "load", "start": 0, "step": 60, "values": [1.0, 2.5, 0.5],
             "beyond": "repeat"}
    back = series_from_csv(series_csv(entry), "load", beyond="repeat")
    assert back["times"] == [0.0, 60.0, 120.0]
    assert back["values"] == [1.0, 2.5, 0.5]
    assert build_series(back).value_at(70) == 2.5


def test_the_series_time_unit_converts_into_the_models_base_unit():
    hourly = build_series({"name": "p", "start": 0, "step": 1, "values": [1.0, 2.0],
                           "time_unit": "hours"}, base_unit="minutes")
    assert hourly.times == (0.0, 60.0)
    assert hourly.value_at(59) == 1.0 and hourly.value_at(60) == 2.0


def test_a_malformed_series_and_an_unknown_ts_name_are_problems():
    from nagasim.core.schema import validate_model_structured

    bad = _minimal(timeseries_inputs=[{"name": "p", "times": [0, 5, 3], "values": [1, 2, 3]},
                                      {"name": "p", "step": 1, "values": [1]}])
    codes = {p.code for p in validate_model_structured(bad)}
    assert {"E_SERIES_TIMES", "E_SERIES_NAME"} <= codes

    c, d = create(max_arrivals=1), dispose()
    c["params"]["dist_params"] = {"value": "TS(\"nope\")"}
    unknown = make_layout([c, d], chain(c, d), timeseries_inputs=[])
    assert "E_SERIES_UNKNOWN" in {p.code for p in validate_model_structured(unknown)}


# ================================================================ exact tanks
def test_a_tank_with_constant_rates_hits_its_bounds_at_the_closed_form_time():
    """In 3/min, out 1/min, tank 0..100 starting at 10: net +2, so it fills at
    t = (100 - 10) / 2 = 45. After that the inflow is capped to the outflow, so
    the level stays at 100 and the spillage is exactly 2/min."""
    env = _run(_minimal(
        levels=[{"name": "Tank", "capacity": 100, "initial": 10}],
        flows=[{"name": "fill", "to": "Tank", "rate": "3"},
               {"name": "drain", "from": "Tank", "rate": "1"}],
    ), run_len=100)

    tank = _level(env, "Tank")
    assert tank.level == pytest.approx(100.0)
    assert tank.spilled == pytest.approx(2.0 * (100 - 45))    # 110
    assert tank.full_fraction(100.0) == pytest.approx(55 / 100)
    # `Total` is what actually flowed: 3/min while filling, then 1/min once the
    # tank is full and the inflow is scaled down to match the outflow.
    assert _flow(env, "fill").total == pytest.approx(3 * 45 + 1 * 55)
    assert _flow(env, "drain").total == pytest.approx(1 * 100)
    # Trapezoid average: ramp 10 -> 100 over 45, then flat at 100 for 55.
    assert tank.time_avg(100.0) == pytest.approx((0.5 * (10 + 100) * 45 + 100 * 55) / 100)


def test_an_empty_level_scales_its_outflows_proportionally_and_records_unmet():
    """Two draws of 3/min and 1/min on a tank holding 20 with a 2/min refill.
    Net -2 until empty at t = 10; from then the 4/min of demand is served by
    the 2/min of supply, split 3:1 -- so `unmet` accrues at exactly 2/min."""
    env = _run(_minimal(
        levels=[{"name": "Tank", "capacity": 100, "initial": 20}],
        flows=[{"name": "refill", "to": "Tank", "rate": "2"},
               {"name": "big", "from": "Tank", "rate": "3"},
               {"name": "small", "from": "Tank", "rate": "1"}],
    ), run_len=30)

    tank = _level(env, "Tank")
    assert tank.level == pytest.approx(0.0)
    assert tank.unmet == pytest.approx(2.0 * 20)              # 2/min for the last 20 minutes
    # Proportional split of the 2/min the refill supplies: 1.5 and 0.5.
    assert _flow(env, "big").total == pytest.approx(3 * 10 + 1.5 * 20)
    assert _flow(env, "small").total == pytest.approx(1 * 10 + 0.5 * 20)
    assert _flow(env, "big").limited_fraction(30.0) == pytest.approx(20 / 30)


def test_efficiency_and_power_limits_bound_what_reaches_the_level():
    """Charge 10/min at 80% into a level that accepts at most 4/min: the level
    gains 4/min, the flow is cut to 5/min drawn, and 4/min of the 8/min that
    would have arrived is spilled."""
    env = _run(_minimal(
        levels=[{"name": "Battery", "capacity": 1000, "initial": 0, "max_inflow": 4}],
        flows=[{"name": "charge", "to": "Battery", "rate": "10", "efficiency": 0.8}],
    ), run_len=10)

    battery = _level(env, "Battery")
    assert battery.level == pytest.approx(40.0)
    assert _flow(env, "charge").total == pytest.approx(50.0)  # 5/min drawn for 10 minutes
    assert battery.spilled == pytest.approx(40.0)            # the 4/min that did not fit


# ======================================================= series-driven rates
def test_a_step_series_rate_integrates_to_the_hand_computed_sum():
    """Rate 1, 4 then 2 for an hour each: the tank holds 1*60 + 4*60 + 2*60."""
    env = _run(_minimal(
        timeseries_inputs=[{"name": "supply", "start": 0, "step": 60, "values": [1, 4, 2]}],
        levels=[{"name": "Tank", "initial": 0}],
        flows=[{"name": "in", "to": "Tank", "rate": "TS(\"supply\")"}],
    ), run_len=180)
    assert _level(env, "Tank").level == pytest.approx(60 * (1 + 4 + 2))


def test_a_repeating_daily_profile_integrates_over_several_days():
    """A 4-point 6-hour profile repeated over three days (base unit hours)."""
    values = [0, 2, 5, 1]
    env = _run(_minimal(
        timeseries_inputs=[{"name": "pv", "start": 0, "step": 6, "values": values,
                            "beyond": "repeat"}],
        levels=[{"name": "Tank", "initial": 0}],
        flows=[{"name": "in", "to": "Tank", "rate": "TS(\"pv\")"}],
        simulation={"time_limit": 72, "warmup": 0, "replications": 1, "time_unit": "hours"},
    ), run_len=72)
    assert _level(env, "Tank").level == pytest.approx(3 * 6 * sum(values))


def test_a_linear_series_rate_is_resampled_at_the_knots():
    """Documented approximation: a `linear` series feeding a FLOW rate is held
    constant between knots, so the integral is the LEFT sum, not the trapezoid.
    Use `step` (or more knots) when the difference matters."""
    env = _run(_minimal(
        timeseries_inputs=[{"name": "ramp", "start": 0, "step": 10, "values": [0, 10],
                            "interpolation": "linear"}],
        levels=[{"name": "Tank", "initial": 0}],
        flows=[{"name": "in", "to": "Tank", "rate": "TS(\"ramp\")"}],
    ), run_len=10)
    assert _level(env, "Tank").level == pytest.approx(0.0)    # left sample of the ramp is 0


# ============================================== thresholds, signals, transfers
def test_a_threshold_releases_a_held_entity_at_the_exact_instant():
    """Tank draining at 2/min from 50 crosses 30 at t = 10; the threshold fires
    the signal the HOLD is waiting on, so the entity moves at exactly t = 10."""
    c = create(max_arrivals=1)
    hold = {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Signal", "signal_name": "low", "conditions": [],
                       "scan_interval": 1.0}}
    d = dispose()
    lay = make_layout(
        [c, hold, d], chain(c, hold, d),
        levels=[{"name": "Tank", "initial": 50,
                 "thresholds": [{"value": 30, "direction": "falling", "signal": "low",
                                 "variable": "tank_at_alarm"}]}],
        flows=[{"name": "out", "from": "Tank", "rate": "2"}],
        variables=[{"name": "tank_at_alarm", "initial": 0}])
    env = _run(lay, run_len=40)

    entities = list(env.ctx.all_entities)
    assert len(entities) == 1
    assert entities[0].deletion_time == pytest.approx(10.0)
    assert env.ctx.variables["tank_at_alarm"] == pytest.approx(30.0)


def test_an_entity_draws_stock_and_a_backorder_waits_for_the_exact_refill():
    """Stock starts at 5 and refills at 1/min. Three entities arrive at t=1,2,3
    and each draws 4 with `wait`. At t=1 the stock is 6, so the first is served
    at once and leaves 2. The second arrives at t=2 (stock 3) and waits for the
    level to reach 4, which is t=3; the third then waits from 0 for four more
    units, so it leaves at t=7. Every instant is scheduled in closed form, not
    polled."""
    c = create(max_arrivals=3, distribution="Constant", dist_params={"value": 1.0},
               start_time=1)
    c["params"]["dist_params"] = {"value": 1.0}
    draw = {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 0],
            "params": {"level": "Stock", "amount": "4", "mode": "remove", "wait": True}}
    d = dispose()
    env = _run(make_layout(
        [c, draw, d], chain(c, draw, d),
        levels=[{"name": "Stock", "capacity": 100, "initial": 5}],
        flows=[{"name": "replenish", "to": "Stock", "rate": "1"}]), run_len=20)

    exits = sorted(e.deletion_time for e in env.ctx.all_entities)
    assert exits == pytest.approx([1.0, 3.0, 7.0])
    stock = _level(env, "Stock")
    assert stock.level == pytest.approx(5 + 20 - 12)
    assert stock.unmet == 0.0                        # a waiting draw is never "unmet"


def test_a_backorder_grant_can_itself_cross_a_threshold():
    """A lump is a lump: filling a queued draw drops the level discontinuously,
    and a threshold on the way down must fire exactly as a continuous crossing
    does -- which is what a reorder point depends on."""
    c = create(max_arrivals=1, dist_params={"value": 1.0})
    draw = {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 0],
            "params": {"level": "Stock", "amount": "10", "mode": "remove", "wait": True}}
    d = dispose()
    env = _run(make_layout(
        [c, draw, d], chain(c, draw, d),
        variables=[{"name": "reorder_at", "initial": -1}],
        levels=[{"name": "Stock", "capacity": 100, "initial": 0,
                 "thresholds": [{"value": 5, "direction": "falling",
                                 "signal": "reorder", "variable": "reorder_at"}]}],
        flows=[{"name": "replenish", "to": "Stock", "rate": "1"}]), run_len=30)

    # The draw waits until the level reaches 10 (t = 10), then takes it to 0 --
    # straight through the threshold at 5, which the continuous curve never
    # visited on the way down.
    assert list(env.ctx.all_entities)[0].deletion_time == pytest.approx(10.0)
    assert env.ctx.variables["reorder_at"] == pytest.approx(0.0)


def test_a_transfer_without_wait_takes_what_it_can_and_records_the_shortfall():
    c = create(max_arrivals=1)
    draw = {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 0],
            "params": {"level": "Stock", "amount": "10", "mode": "remove", "wait": False}}
    d = dispose()
    env = _run(make_layout([c, draw, d], chain(c, draw, d),
                           levels=[{"name": "Stock", "initial": 3}]), run_len=5)
    assert _level(env, "Stock").level == pytest.approx(0.0)
    assert _level(env, "Stock").unmet == pytest.approx(7.0)


def test_a_level_chattering_at_a_bound_generates_a_bounded_number_of_events(monkeypatch):
    """Item (i), Zeno: a tiny level driven by huge rates must not schedule an
    event every floating-point epsilon. The saturation rule makes the net rate
    exactly 0 AT the bound and `_snap` puts the level exactly on it, so the
    tank fills once and is then quiet for the rest of the run."""
    from nagasim.core import levels as levels_mod

    calls = []
    original = levels_mod.LevelSystem._recompute
    monkeypatch.setattr(levels_mod.LevelSystem, "_recompute",
                        lambda self: (calls.append(self.env.now), original(self))[1])

    env = _run(_minimal(
        levels=[{"name": "Tank", "capacity": 1, "initial": 0}],
        flows=[{"name": "fill", "to": "Tank", "rate": "1000"},
               {"name": "leak", "from": "Tank", "rate": "999"}],
    ), run_len=1000)

    tank = _level(env, "Tank")
    assert tank.level == pytest.approx(1.0)          # pinned at capacity
    assert tank.spilled == pytest.approx(1.0 * (1000 - 1))   # 1/min once full, from t=1
    # t=0 (start), t=1 (full), t=1000 (the horizon wake-up). Nothing else.
    assert len(calls) <= 4, f"re-evaluated {len(calls)} times: the bound is chattering"


# ================================================== costs, impacts and frames
def test_a_time_varying_price_integrates_exactly_into_the_cost_kpi():
    """Rate 2/h against a tariff of 0.10 then 0.30 per unit, 6 hours each:
    cost = 2*6*0.10 + 2*6*0.30 = 4.8, impact = 2*12*0.4 = 9.6."""
    results = run_model(_minimal(
        timeseries_inputs=[{"name": "tariff", "start": 0, "step": 6, "values": [0.10, 0.30]}],
        levels=[{"name": "Store", "initial": 0}],
        flows=[{"name": "buy", "to": "Store", "rate": "2", "price": "TS(\"tariff\")",
                "emission_factor": "0.4"}],
        simulation={"time_limit": 12, "warmup": 0, "replications": 1, "time_unit": "hours"},
    ), replications=1, run_length=12)

    flows = results["flow_summary"]
    assert float(flows.loc[flows["Flow"] == "buy", "Cost"].iloc[0]) == pytest.approx(4.8)
    assert float(flows.loc[flows["Flow"] == "buy", "Impact"].iloc[0]) == pytest.approx(9.6)
    assert metric_value(results, "Total Flow Cost") == pytest.approx(4.8)
    assert metric_value(results, "Total Impact") == pytest.approx(9.6)
    # Flow cost joins Total Cost the way a resource cost does.
    assert metric_value(results, "Total Cost") == pytest.approx(4.8)


def test_a_model_without_levels_gains_no_system_row_and_two_empty_frames():
    """The fixed-shape rule (A3/A4 precedent): the new frames exist for every
    model, empty with their columns, and the two new system rows appear only
    for a model that actually has flows -- so no golden hash moves."""
    results = run_model(_minimal(), replications=1, run_length=10)
    assert results["level_summary"].empty and results["flow_summary"].empty
    assert list(results["level_summary"].columns)[:2] == ["Run", "Level"]
    metrics = set(results["system"]["Metric"])
    assert "Total Flow Cost" not in metrics and "Total Impact" not in metrics


# ============================================ reach: bindings, assets, control
def test_a_series_binding_refreshes_a_profile_from_a_csv(tmp_path):
    """The `series:` data-binding transform (PHASE4 D2), the profile twin of
    `table:`: two source columns replace the declared samples outright."""
    # databind is product, not engine; skip cleanly under the engine wheel.
    resolve_bindings = pytest.importorskip("nagasim.databind").resolve_bindings

    source = tmp_path / "load.csv"
    source.write_text("hour,kw\n0,1.0\n1,4.0\n2,2.0\n", encoding="utf-8")
    layout = _minimal(
        timeseries_inputs=[{"name": "load", "times": [0, 1], "values": [0.0, 0.0]}],
        data_sources=[{"name": "meter", "kind": "csv", "location": str(source)}],
        data_bindings=[{"name": "load", "source": "meter", "column": "kw",
                        "transform": "series:hour", "target": "timeseries_inputs.load.values",
                        "enabled": True}])

    bound, _report = resolve_bindings(layout, base_dir=tmp_path)
    series = bound["timeseries_inputs"][0]
    assert series["times"] == [0.0, 1.0, 2.0]
    assert series["values"] == [1.0, 4.0, 2.0]


def test_a_dollar_level_is_cloned_per_submodel_instance_with_its_flows():
    """What lets an asset library package a "Battery" (PHASE4 D1): a `$` level
    inside a definition is per-instance, and every flow touching it follows,
    the way a failure follows its resource."""
    from nagasim.core.submodels import flatten

    c, d = create(max_arrivals=1), dispose()
    instance = {"name": "cell", "description": "cell", "type": "SUBMODEL", "position": [1, 0],
                "params": {"definition": "Store", "arguments": {}}}
    layout = make_layout(
        [c, instance, d], chain(c, instance, d),
        levels=[{"name": "Tank", "capacity": 100, "initial": 10}],
        flows=[{"name": "fill", "to": "Tank", "rate": "1"}],
        submodels={"Store": {
            "parameters": [],
            "blocks": [
                {"name": "in", "description": "in", "type": "SUB_ENTRY", "position": [0, 0],
                 "params": {"port": "in"}},
                {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 0],
                 "params": {"level": "$Tank", "amount": "1", "mode": "remove", "wait": False}},
                {"name": "out", "description": "out", "type": "SUB_EXIT", "position": [2, 0],
                 "params": {"port": "out"}},
            ],
            "links": [{"source": "in", "target": "draw"}, {"source": "draw", "target": "out"}],
        }})

    flat = flatten(layout)
    # The TEMPLATE is consumed (PHASE4 D3): a cloned level left at the top
    # level would be a second live tank beside the instance's.
    assert [level["name"] for level in flat["levels"]] == ["cell/Tank"]
    assert [flow["name"] for flow in flat["flows"]] == ["cell/fill"]
    clone = flat["flows"][0]
    assert clone["to"] == "cell/Tank"          # the flow followed its level
    block = next(b for b in flat["blocks"] if b["name"] == "cell/draw")
    assert block["params"]["level"] == "cell/Tank"


def test_a_continuous_asset_clones_its_flows_from_scoped_and_reads_its_parameters():
    """PHASE4 D3 (ARCHITECTURE D62): an asset with NO blocks -- a PV array, a
    grid connection -- has no `$` reference to trigger cloning, so naming the
    record under `scoped` is the trigger, and the clone carries the instance's
    parameters as `locals` so its rate expression reads them."""
    from nagasim.core.submodels import flatten

    def instance(name, kwp):
        return {"name": name, "description": name, "type": "SUBMODEL", "position": [0, 0],
                "params": {"definition": "PV", "arguments": {"kwp": kwp}}}

    layout = make_layout(
        [instance("pv1", 1.0), instance("pv2", 3.0)], [],
        timeseries_inputs=[{"name": "sun", "start": 0, "step": 1, "values": [0.0, 2.0],
                            "beyond": "repeat"}],
        flows=[{"name": "PV", "rate": "TS(profile) * kwp", "tag": "generation"}],
        submodels={"PV": {"parameters": [{"name": "kwp", "default": 1.0},
                                         {"name": "profile", "default": "sun", "type": "name"}],
                          "blocks": [], "links": [], "scoped": {"flows": {"PV": {}}}}})

    flat = flatten(layout)
    assert [flow["name"] for flow in flat["flows"]] == ["pv1/PV", "pv2/PV"]
    assert flat["flows"][0]["locals"] == {"kwp": 1.0, "profile": "sun"}
    assert flat["flows"][1]["locals"]["kwp"] == 3.0
    assert flat["flows"][0]["tag"] == "generation"          # the tag is shared: it is the bus

    # Both arrays run, each on its own kWp, and FLOWSUM adds them up.
    env = _run(flat, run_len=1.5)
    assert _flow(env, "pv1/PV").total == pytest.approx(1.0)      # 0 for an hour, then 2 kW
    assert _flow(env, "pv2/PV").total == pytest.approx(3.0)
    assert env.ctx.level_system.flow_sum("generation") == pytest.approx(8.0)


def test_a_fixed_cost_is_cloned_per_asset_instance_and_reads_its_parameters():
    """PHASE4 D5: a capex clones exactly like a flow -- naming it under
    `scoped.fixed_costs` is the trigger, and the clone carries the instance's
    parameters, so two arrays of different sizes cost different money."""
    from nagasim.core.submodels import flatten
    from nagasim.api import run_model

    def instance(name, kwp):
        return {"name": name, "description": name, "type": "SUBMODEL", "position": [0, 0],
                "params": {"definition": "PV", "arguments": {"kwp": kwp}}}

    layout = make_layout(
        [instance("pv1", 1.0), instance("pv2", 3.0)], [],
        timeseries_inputs=[{"name": "sun", "start": 0, "step": 1, "values": [0.0, 2.0],
                            "beyond": "repeat"}],
        flows=[{"name": "PV", "rate": "TS(\"sun\") * kwp", "tag": "generation"}],
        fixed_costs=[{"name": "PV capital", "amount": "kwp * 900", "category": "Capital"}],
        submodels={"PV": {"parameters": [{"name": "kwp", "default": 1.0}],
                          "blocks": [], "links": [],
                          "scoped": {"flows": {"PV": {}},
                                     "fixed_costs": {"PV capital": {}}}}})
    layout["simulation"]["time_limit"] = 4

    flat = flatten(layout)
    assert [c["name"] for c in flat["fixed_costs"]] == ["pv1/PV capital", "pv2/PV capital"]
    assert flat["fixed_costs"][1]["locals"] == {"kwp": 3.0}

    results = run_model(layout, replications=1, run_length=4)
    amounts = dict(zip(results["cost_summary"]["Item"], results["cost_summary"]["Amount"]))
    assert amounts == {"pv1/PV capital": 900.0, "pv2/PV capital": 2700.0}


def test_a_malformed_fixed_cost_and_a_non_numeric_peak_price_are_problems():
    from nagasim.core.schema import validate_model_structured

    bad = make_layout([], [], fixed_costs=[{"name": "", "amount": 1},
                                           {"name": "A", "amount": "nope +"},
                                           {"name": "A", "amount": [1]}],
                      flows=[{"name": "f", "rate": "1", "peak_price": "free"}])
    codes = {p.code for p in validate_model_structured(bad)}
    assert {"E_FIXED_COST_NAME", "E_FIXED_COST_AMOUNT", "E_FLOW_PEAK_PRICE"} <= codes


def test_flowsum_is_the_site_bus_and_its_priority_argument_is_the_merit_order():
    """Two batteries on one bus (PHASE4 D3): the second sees only the surplus
    the first actually took, so the balance stays exact."""
    layout = _minimal(
        levels=[{"name": "A", "capacity": 100, "initial": 0, "max_inflow": 1},
                {"name": "B", "capacity": 100, "initial": 0}],
        flows=[{"name": "pv", "rate": "5", "tag": "generation"},
               {"name": "load", "rate": "2", "tag": "demand"},
               # A charges first (priority 1) but its power limit caps it at 1.
               {"name": "charge_a", "to": "A", "tag": "storage_in", "priority": 1,
                "rate": "max(FLOWSUM(\"generation\") - FLOWSUM(\"demand\") "
                        "- FLOWSUM(\"storage_in\", 1), 0)"},
               {"name": "charge_b", "to": "B", "tag": "storage_in", "priority": 2,
                "rate": "max(FLOWSUM(\"generation\") - FLOWSUM(\"demand\") "
                        "- FLOWSUM(\"storage_in\", 2), 0)"}])
    env = _run(layout, run_len=10.0)
    # Surplus 3 kW: A takes its 1 kW limit, B takes exactly the remaining 2.
    assert _level(env, "A").level == pytest.approx(10.0)
    assert _level(env, "B").level == pytest.approx(20.0)
    assert env.ctx.level_system.flow_sum("storage_in") == pytest.approx(3.0)
    assert env.ctx.level_system.flow_sum("storage_in", 2) == pytest.approx(1.0)
    assert env.ctx.level_system.flow_sum("nothing_carries_this") == 0.0


def test_peak_rate_is_the_highest_rate_a_flow_actually_held():
    """What a demand charge is billed on (PHASE4 D3)."""
    results = run_model(_minimal(
        timeseries_inputs=[{"name": "p", "start": 0, "step": 1, "values": [1.0, 4.0, 2.0]}],
        flows=[{"name": "grid", "rate": "TS(\"p\")"}],
    ), replications=1, run_length=3.0)
    row = results["flow_summary"].iloc[0]
    assert row["Peak Rate"] == pytest.approx(4.0)
    assert row["Total"] == pytest.approx(7.0)


def test_a_warm_start_sets_the_level_the_run_begins_at():
    env = _run(_minimal(
        levels=[{"name": "Tank", "capacity": 100, "initial": 10}],
        flows=[{"name": "fill", "to": "Tank", "rate": "1"}],
        initial_state={"levels": {"Tank": 60}},
    ), run_len=10)
    assert _level(env, "Tank").level == pytest.approx(70.0)


def _dispatch_model():
    """A decision clock: a CREATE ticking every time unit into a Policy-mode
    DECIDE whose branches ASSIGN a setpoint the flow rates read. No new engine
    machinery -- this is the documented way a trainable dispatch reaches a
    continuous model (PHASE4 D1 item 7)."""
    clock = create("clock", max_arrivals=0, dist_params={"value": 1.0})
    decide = {"name": "dispatch", "description": "dispatch", "type": "DECIDE",
              "position": [1, 0],
              "params": {"mode": "Policy", "policy": "dispatch", "probs": [], "conditions": [],
                         "observation": ["LEVEL(\"Battery\")"], "fallback": "first",
                         "queue_exprs": []}}
    charge = {"name": "set_charge", "description": "c", "type": "ASSIGN", "position": [2, 0],
              "params": {"assignments": [{"target": "setpoint", "expr": "1"}]}}
    drain = {"name": "set_discharge", "description": "d", "type": "ASSIGN", "position": [2, 1],
             "params": {"assignments": [{"target": "setpoint", "expr": "-1"}]}}
    d = dispose()
    links = [{"source": "clock", "target": "dispatch"},
             {"source": "dispatch", "target": "set_charge"},
             {"source": "dispatch", "target": "set_discharge"},
             {"source": "set_charge", "target": "dispose_1"},
             {"source": "set_discharge", "target": "dispose_1"}]
    return make_layout(
        [clock, decide, charge, drain, d], links,
        variables=[{"name": "setpoint", "initial": 0}],
        levels=[{"name": "Battery", "capacity": 10, "initial": 5}],
        flows=[{"name": "charge", "to": "Battery", "rate": "max(setpoint, 0)"},
               {"name": "discharge", "from": "Battery", "rate": "max(-setpoint, 0)"}])


def test_a_policy_observes_the_level_and_acts_on_a_setpoint():
    """`SimEnv` drives the clock loop unchanged: the observation is the live
    level and the action moves it. Always charging fills the battery, always
    discharging empties it."""
    from nagasim.rl import SimEnv, evaluate_policy

    layout = _dispatch_model()
    env = SimEnv(layout, policy="dispatch", reward="LEVEL(\"Battery\")", horizon=10.0)
    observation, _info = env.reset(seed=0)
    assert observation[0] == pytest.approx(5.0)          # the level, not a queue length
    env.step(0)                                          # charge for one time unit
    assert env.last_request.observation[0] == pytest.approx(6.0)

    charging = evaluate_policy(layout, lambda request: 0, episodes=1, policy="dispatch",
                               reward="LEVEL(\"Battery\")", horizon=10.0)
    draining = evaluate_policy(layout, lambda request: 1, episodes=1, policy="dispatch",
                               reward="LEVEL(\"Battery\")", horizon=10.0)
    assert charging["mean"] > draining["mean"]


# =============================================== the two bundled example models
def test_the_pv_battery_example_balances_its_energy_exactly():
    """`energy_pv_battery.json` is deterministic, so every number is exact. The
    check is the energy balance: every kWh the PV produced went to the load,
    into the battery, or out to the grid."""
    results = run_model(str(_MODELS / "energy_pv_battery.json"), replications=1)
    flows = {row["Flow"]: row for _, row in results["flow_summary"].iterrows()}
    battery = results["level_summary"].iloc[0]

    pv, export = flows["pv_generation"]["Total"], flows["grid_export"]["Total"]
    charge, discharge = flows["charge"]["Total"], flows["discharge"]["Total"]
    assert pv == pytest.approx(29.8 * 7)                 # the profile, seven times
    direct = 18.1 * 7 - discharge * 0.95 - flows["grid_import"]["Total"]
    assert direct + charge + export == pytest.approx(pv)

    assert battery["Total In"] == pytest.approx(charge * 0.95)
    # Total Cost is the grid bill PLUS the two lump sums (PHASE4 D5): the
    # battery's prorated capital charge and the demand charge on the peak
    # import power, both itemised in the cost frame.
    costs = results["cost_summary"]
    assert list(costs["Kind"]) == ["fixed", "peak"]
    assert float(costs.loc[costs["Item"] == "Battery capital", "Amount"].iloc[0]) == \
        pytest.approx(10 * 450 * 0.09 * 168 / 8760, abs=1e-4)
    assert float(costs.loc[costs["Item"] == "grid_import", "Amount"].iloc[0]) == \
        pytest.approx(flows["grid_import"]["Peak Rate"] * 1.5, abs=1e-4)
    assert metric_value(results, "Total Cost") == pytest.approx(     # rounded to 4 dp
        flows["grid_import"]["Cost"] + flows["grid_export"]["Cost"]
        + float(costs["Amount"].sum()), abs=1e-4)
    assert metric_value(results, "Total Impact") == pytest.approx(
        flows["grid_import"]["Total"] * 0.35)
    # Self-consumption, the headline KPI of a PV study.
    assert (pv - export) / pv == pytest.approx(0.602, abs=0.001)


def test_sizing_the_battery_trades_capital_against_the_grid_bill():
    """The optimizer addresses a level's capacity and the fixed cost priced on
    it as ONE factor over two paths, on a tiny budget (PHASE4 D5).

    Before D5 the capital charge was a pseudo-flow and the optimum came from
    diminishing returns alone (anywhere in 6..18). With a real capital charge
    the curve has a proper minimum: 17.83 EUR at 0 kWh, 5.47 at 8, 13.39 at 20."""
    from nagasim.optimize import Factor, optimize

    result = optimize(str(_MODELS / "energy_pv_battery.json"),
                      [Factor(["levels.Battery.capacity", "variables.battery_kwh.initial"],
                              kind="continuous", low=0.0, high=20.0, step=2.0,
                              name="battery_kwh")],
                      response="system/Total Cost", goal="min", budget=20,
                      replications_per_point=1, workers=1)
    assert 4.0 <= result.best["battery_kwh"] <= 12.0


def test_the_inventory_example_conserves_every_unit():
    """`inventory_sS.json`: opening stock + received - sold = closing stock, a
    conservation identity that fails the moment level accounting goes wrong."""
    results = run_model(str(_MODELS / "inventory_sS.json"), replications=1)
    stock = results["level_summary"].iloc[0]
    stats = dict(zip(results["user_summary"]["Statistic"], results["user_summary"]["Value"]))

    assert stats["orders_placed"] == 10      # placed at 1.25 + 3.5k <= 34
    assert stock["Total In"] == pytest.approx(9 * 70)    # the tenth is still in transit
    assert stock["Total Out"] == pytest.approx(stats["units_sold"])
    assert 80 + stock["Total In"] - stock["Total Out"] == pytest.approx(stock["Final"])
    assert stock["Final"] == pytest.approx(25.0)
    assert stock["Unmet"] == 0.0
    assert metric_value(results, "Avg Throughput Time") == 0.0   # nobody was backordered
    # PHASE4 D5: 35 EUR per order placed, charged by a RECORD with kind="cost".
    costs = results["cost_summary"]
    assert list(costs["Item"]) == ["ordering cost"] and list(costs["Kind"]) == ["event"]
    assert float(costs["Amount"].iloc[0]) == 10 * 35
    assert metric_value(results, "Total Cost") == 350.0


def test_the_longer_lead_time_scenario_backorders_customers():
    from nagasim import experiments

    result = experiments.run_scenarios(str(_MODELS / "inventory_sS.json"),
                                       ["Longer lead time"], workers=1)
    kpis = experiments.flatten_kpis(result.results["Longer lead time"])
    waited = float(kpis.loc[kpis["KPI"] == "system/Avg Throughput Time", "Value"].iloc[0])
    average = float(kpis.loc[kpis["KPI"] == "level/Stock/Average", "Value"].iloc[0])
    assert waited > 0, "a 4-day lead time must leave customers waiting for stock"
    assert average < 20, "and the shelf must run far lower than the baseline's 49.7"


def test_a_rate_is_re_evaluated_only_when_something_it_reads_has_moved():
    """Bio-4 cost guard (ARCHITECTURE 5.19). A COUNT, not a clock, so it fails
    the same way on any machine and says by how much.

    Only `_saturate` runs between two passes of one re-evaluation, and all it
    writes is the realised rates. A rate that reads neither FLOW() nor
    FLOWSUM() therefore cannot change between passes and is evaluated once; one
    that does is evaluated again only when a flow or a tag it actually names
    has moved. Ten days of the biogas plant: 62 flows, 6.3 passes per
    re-evaluation, and 69 983 rate evaluations instead of the 132 000 that
    re-evaluating every flow on every pass costs.
    """
    evaluations = [0]

    def count(fn):
        def counted(scope):
            evaluations[0] += 1
            return fn(scope)
        return counted

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, str(_MODELS / "biogas_plant_library.json"))
    system = env.ctx.level_system
    # Wrap each flow's compiled rate closure where the engine binds it.
    system._rates = tuple((f, count(fn), scope, reads)
                          for f, fn, scope, reads in system._rates)
    system._varying_rates = tuple(e for e in system._rates if e[3] is None or e[3])

    system.start(240.0)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 240.0)

    assert len(system.flows) == 62, "the guard must be measuring the whole plant"
    assert evaluations[0] < 85_000, (
        f"{evaluations[0]} rate evaluations over 10 days, was 69 983: a rate whose inputs "
        f"cannot have moved is being re-evaluated again")


def test_a_forecast_puts_a_band_on_a_level_kpi():
    """Level and flow KPIs are ordinary `flatten_kpis` keys, so the forecast
    machinery needs no change at all to band them."""
    from nagasim.forecast import forecast

    result = forecast(str(_MODELS / "energy_pv_battery.json"), horizon=48.0, replications=3,
                      kpis=["level/Battery/Average", "flow/grid_import/Total"], workers=1)
    keys = set(result.kpis["KPI"])
    assert "level/Battery/Average" in keys and "flow/grid_import/Total" in keys


# ============================================ trajectory breakpoints (T2, D75)
def test_a_traced_level_records_breakpoints_playback_interpolates_exactly():
    """PHASE4 T2 (D75). The same tank as above -- 10 up by a net 2/min, full at
    t = 45 -- recorded on the trace as BREAKPOINTS, not samples: a level is
    piecewise linear between two re-evaluations (D60), so the corners are the
    whole curve and `Timeline.level_at` interpolates between them exactly.
    """
    from nagasim.core.playback import Timeline
    from nagasim.core.trace import TraceRecorder

    layout = _minimal(levels=[{"name": "Tank", "capacity": 100, "initial": 10}],
                      flows=[{"name": "fill", "to": "Tank", "rate": "3"},
                             {"name": "drain", "from": "Tank", "rate": "1"}])
    recorder = TraceRecorder()
    run_model(layout, replications=1, run_length=100, trace=recorder, seed=0)

    breakpoints = [e for e in recorder.events if e.kind == "level_changed"]
    assert 0 < len(breakpoints) <= 12        # corners, not 100 samples
    timeline = Timeline(recorder.events)
    assert timeline.level_at("Tank", 0.0) == pytest.approx(10.0)
    assert timeline.level_at("Tank", 20.0) == pytest.approx(50.0)     # 10 + 2*20
    assert timeline.level_at("Tank", 45.0) == pytest.approx(100.0)    # exactly full
    assert timeline.level_at("Tank", 70.0) == pytest.approx(100.0)    # capped, flat
    assert timeline.state_at(20.0)["levels"] == {"Tank": pytest.approx(50.0)}
    assert timeline.level_at("Nope", 20.0) is None

    # A TRANSFER lump is two breakpoints at ONE instant (the value before the
    # jump and the value after), so the jump is drawn as a jump.
    from nagasim.core.trace import TraceEvent

    def corner(t, value):
        return TraceEvent(t=t, kind="level_changed", entity_id=-1, e_type="", block="",
                          target=None, resource="Tank", value=value)

    lump = Timeline([corner(0.0, 10.0), corner(5.0, 20.0), corner(5.0, 0.0),
                     corner(10.0, 10.0)])
    assert lump.level_at("Tank", 4.0) == pytest.approx(18.0)
    assert lump.level_at("Tank", 5.0) == pytest.approx(0.0)
    assert lump.level_at("Tank", 7.5) == pytest.approx(5.0)


def test_a_run_without_tracing_records_no_level_breakpoints(monkeypatch):
    """The recording costs one attribute test per re-evaluation when nobody is
    listening -- which is every run of every study (D75)."""
    from nagasim.core.events import EventBus

    emitted = []
    monkeypatch.setattr(EventBus, "emit",
                        lambda self, name, **payload: emitted.append(name))
    _run(_minimal(levels=[{"name": "Tank", "capacity": 100, "initial": 10}],
                  flows=[{"name": "fill", "to": "Tank", "rate": "3"}]), run_len=100)
    assert emitted == []


def test_levels_validation_lives_in_its_own_module():
    """Wave E3: the "levels"/"flows" checks live only in core/levels_validation.py."""
    from nagasim.core import levels, levels_validation

    assert not hasattr(levels, "validate_levels_structured")
    assert levels_validation.LEAK_KINDS == ("linear", "exponential")


# ============================================== a flow's own rate_time_unit (#678)
def _per_hour(*flows):
    return _minimal(flows=list(flows),
                    simulation={"time_limit": 60.0, "replications": 1, "time_unit": "minutes"})


def test_flow_rates_read_and_report_in_their_own_rate_time_unit():
    """In a minutes model a flow written per hour is read per hour by FLOW() and
    FLOWSUM(), reported per hour as Avg/Peak Rate, and its threshold is per hour."""
    env = _run(_per_hour(
        {"name": "a", "rate": "6", "tag": "bus", "rate_time_unit": "hours",
         "thresholds": [{"value": 5, "direction": "rising", "variable": "a_rate"}]},
        {"name": "b", "rate": "4", "tag": "bus", "rate_time_unit": "hours"},
        {"name": "sum", "rate": 'FLOWSUM("bus")', "rate_time_unit": "hours"},
        {"name": "base", "rate": 'FLOW("a")'}), 60.0)
    a, total, base = _flow(env, "a"), _flow(env, "sum"), _flow(env, "base")
    assert a.total == pytest.approx(6.0)                # one hour at 6 per hour
    assert total.total == pytest.approx(10.0)
    assert base.total == pytest.approx(360.0)           # FLOW("a") = 6, read per minute here
    assert a.avg_rate(60.0) == pytest.approx(6.0)
    assert total.peak_rate == pytest.approx(10.0)
    assert a.starts == 1
    assert env.ctx.variables["a_rate"] == pytest.approx(6.0)     # written per hour too


def test_a_tag_mixing_rate_time_units_is_refused():
    from nagasim.core.schema.migrate import ModelValidationError
    with pytest.raises(ModelValidationError, match="rate_time_unit"):
        _run(_per_hour({"name": "a", "rate": "6", "tag": "bus", "rate_time_unit": "hours"},
                       {"name": "b", "rate": "1", "tag": "bus"}), 60.0)


def _codes(layout):
    from nagasim.core.schema import validate_layout_structured
    return [(p.code, p.path) for p in validate_layout_structured(layout)
            if p.code in ("E_FLOW_TAG", "W_RATE_UNIT_READ")]


def test_validation_names_a_tag_mixing_rate_time_units():
    from nagasim.core.schema import validate_layout_structured
    layout = _per_hour({"name": "a", "rate": "6", "tag": "bus", "rate_time_unit": "hours"},
                       {"name": "b", "rate": "1", "tag": "bus"})
    found = [p for p in validate_layout_structured(layout) if p.code == "E_FLOW_TAG"]
    assert [p.path for p in found] == ["/flows/1/rate_time_unit"]
    assert "a: hours, b: base" in found[0].message and "Energy" in found[0].hint


def test_validation_warns_a_read_across_time_units():
    layout = _per_hour(
        {"name": "a", "rate": "6", "tag": "bus", "rate_time_unit": "hours"},
        {"name": "same", "rate": 'FLOWSUM("bus") + FLOW("a")', "rate_time_unit": "hours"},
        {"name": "other", "rate": 'FLOWSUM("bus")'})
    layout["conversions"] = [{"name": "D", "time_unit": "hours", "substrates": [
        {"name": "s", "feed": 'FLOW("a")', "yield": 1.0, "curve": [[1, 1.0]]}]}]
    layout["flows"].append({"name": "gas", "rate": 'CONV("D")', "rate_time_unit": "hours"})
    assert _codes(layout) == [("W_RATE_UNIT_READ", "/flows/2/rate"),
                              ("W_RATE_UNIT_READ", "/conversions/0/substrates/0/feed")]


def test_validation_warns_power_outside_hours_and_literal_tags_beside_computed_ones():
    layout = _per_hour(
        {"name": "a", "rate": "6", "tag": "bus", "rate_time_unit": "hours"},
        {"name": "kw", "rate": 'POWER("M")'},
        {"name": "kwh", "rate": 'POWER("M")', "rate_time_unit": "hours"},
        {"name": "mixed", "rate": 'FLOWSUM("bus") + FLOWSUM("b" + "us")'})
    layout["resources"] = [{"name": "M", "capacity": 1, "power": {"busy": 10}}]
    assert _codes(layout) == [("W_RATE_UNIT_READ", "/flows/1/rate"),
                              ("W_RATE_UNIT_READ", "/flows/3/rate")]


def test_validation_checks_a_computed_rate_name_against_every_one_it_may_read():
    """#690, D606: a computed FLOWSUM tag (or FLOW/CONV name) may read any of its kind;
    one unit among them is checked, several that differ cannot be (W_RATE_UNIT_UNCHECKED)."""
    def codes(*extra):
        layout = _per_hour({"name": "a", "rate": "6", "tag": "kw", "rate_time_unit": "hours"},
                           *extra)
        return [(p.code, p.path) for p in validate_layout_structured(layout)
                if p.code.startswith("W_RATE_UNIT")]

    from nagasim.core.schema import validate_layout_structured
    pick = 'FLOWSUM(IF(TNOW > 1, "kw", "m"))'
    assert codes({"name": "r", "rate": pick, "rate_time_unit": "hours"}) == []
    assert codes({"name": "m", "rate": "1", "tag": "m"},
                 {"name": "r", "rate": pick, "rate_time_unit": "hours"}) == [
        ("W_RATE_UNIT_UNCHECKED", "/flows/2/rate")]
    assert codes({"name": "r", "rate": "FLOWSUM(t)", "rate_time_unit": "hours",
                  "locals": {"t": "kw"}}) == []                  # folds to a per-hour tag


def test_validation_of_computed_rate_names_folds_locals_and_reads_conversions():
    """#690 follow-ups: a FLOWSUM folded from locals beside a computed FLOW() is checked
    as its literal; a computed CONV() and a conversion feed's locals are checked; a
    malformed `locals` is no crash."""
    from nagasim.core.schema import validate_layout_structured

    def codes(extra=(), conversions=()):
        layout = _per_hour({"name": "a", "rate": "6", "tag": "kw", "rate_time_unit": "hours"},
                           *extra)
        layout["conversions"] = list(conversions)
        return [(p.code, p.path) for p in validate_layout_structured(layout)
                if p.code.startswith("W_RATE_UNIT")]

    assert codes([{"name": "r", "rate": 'FLOWSUM(t) + FLOW(IF(TNOW > 1, "a", "r"))',
                   "rate_time_unit": "hours", "locals": {"t": "kw"}}]) == []
    feed = {"name": "s", "feed": "FLOWSUM(t)", "yield": 1.0, "curve": [[1, 1.0]]}
    assert codes(conversions=[{"name": "D", "locals": {"t": "kw"}, "substrates": [feed]}]) == [
        ("W_RATE_UNIT_READ", "/conversions/0/substrates/0/feed")]
    two = [{"name": "D", "time_unit": "hours", "substrates": [{**feed, "feed": "1"}]},
           {"name": "E", "substrates": [{**feed, "feed": "1"}]}]
    assert codes([{"name": "r", "rate": 'CONV(IF(TNOW > 1, "D", "E"))',
                   "rate_time_unit": "hours"}], two) == [
        ("W_RATE_UNIT_CHANGED", "/conversions/0/time_unit"),
        ("W_RATE_UNIT_UNCHECKED", "/flows/1/rate")]
    for bad in (["t"], "t", 5):
        assert isinstance(codes([{"name": "r", "rate": "FLOWSUM(t)", "locals": bad}]), list)


def test_a_hold_reading_a_computed_level_name_wakes_at_the_exact_draw():
    """D810: a Condition HOLD whose LEVEL name is computed (a library asset bound to
    another asset's stock) cannot name the level it watches, so it watches every
    level: it releases at the draw's instant (t=2.5), not at the next scan (t=100)."""
    waiter = create("waiter", max_arrivals=1)
    hold = {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Condition", "signal_name": "", "scan_interval": 100.0,
                       "conditions": ['LEVEL("Stock" if 1 > 0 else "Other") <= 3']}}
    drawer = create("drawer", max_arrivals=1, start_time=2.5)
    draw = {"name": "draw", "description": "draw", "type": "TRANSFER", "position": [1, 1],
            "params": {"level": "Stock", "amount": "8", "mode": "remove", "wait": False}}
    out_a, out_b = dispose("out_a"), dispose("out_b")
    env = _run(make_layout(
        [waiter, hold, out_a, drawer, draw, out_b],
        chain(waiter, hold, out_a) + chain(drawer, draw, out_b),
        levels=[{"name": "Stock", "capacity": 100, "initial": 10},
                {"name": "Other", "capacity": 100, "initial": 10}]), run_len=50)

    left = {e.creation_time: e.deletion_time for e in env.ctx.all_entities}
    assert left[0.0] == pytest.approx(2.5)
