"""Delayed conversion, blended levels and equipment-limited flows (Bio-1,
ARCHITECTURE D64/D65/D66).

Same contract as `test_levels.py`: every assertion is exact or hand-computed.
Cohorts and curve steps land on one grid, so a release schedule is a finite sum
of rectangles; a component's content is piecewise linear, so its balance closes
to the last unit.
"""
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core.conversions import half_life_knots
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

from conftest import chain, create, dispose, layout as make_layout, run_env

_MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
DEMO = _MODELS / "biogas_digester_demo.json"


def _run(layout, run_len, warmup=0.0, seed=0):
    env, _model = run_env(layout, run_len, seed=seed, warmup=warmup)
    return env


def _substrate(env, conversion, name):
    found = env.ctx.level_system.conversion_index[conversion]
    return next(s for s in found.substrates if s.name == name)


def _hourly(blocks=(), links=(), **sections):
    """A layout with no entity work at all: conversions and flows run on the
    level system's own process."""
    sections.setdefault("simulation", {"time_limit": None, "warmup": 0, "replications": 1,
                                       "time_unit": "hours"})
    return make_layout(list(blocks), list(links), **sections)


# ============================================================ G1: conversions
def test_one_cohort_reproduces_its_release_curve_step_by_step():
    """A single tonne fed in hour 0 closes at t=1 and then releases at exactly
    the rate each curve step asks for: 0.4 of 100 Nm3 over the first 4 h, the
    rest over the next 6 h, and nothing after."""
    layout = _hourly(
        conversions=[{"name": "D", "cohort_interval": 1, "unit": "Nm3", "substrates": [
            {"name": "s", "feed": "1 if TNOW < 1 else 0", "yield": 100.0,
             "curve": [[4, 0.4], [10, 1.0]]}]}],
        levels=[{"name": "Gas", "capacity": 1000, "initial": 0}],
        flows=[{"name": "gas", "to": "Gas", "rate": "CONV(\"D\")"}])

    env = _run(layout, 20)
    system = env.ctx.level_system
    substrate = _substrate(env, "D", "s")

    assert substrate.fed == pytest.approx(1.0)
    assert substrate.produced == pytest.approx(100.0)       # the whole yield, exactly
    assert substrate.unreleased == pytest.approx(0.0)
    assert substrate.peak == pytest.approx(0.4 * 100 / 4)   # the first step's rate, 10/h
    assert system.levels["Gas"].level == pytest.approx(100.0)


def test_a_half_life_curve_conserves_the_yield_and_its_knots_are_the_decay():
    knots = half_life_knots(60.0, 12, 6.0)
    assert len(knots) == 12
    assert knots[-1] == (360.0, pytest.approx(1.0))
    # Renormalised, so a knot sits slightly ABOVE the raw decay curve.
    assert knots[0][1] == pytest.approx((1 - 2 ** -0.5) / (1 - 2 ** -6))

    layout = _hourly(
        conversions=[{"name": "D", "cohort_interval": 1, "substrates": [
            {"name": "s", "feed": "2.0", "yield": 200.0, "half_life": 60, "steps": 12}]}])
    env = _run(layout, 500)
    substrate = _substrate(env, "D", "s")

    # 500 h of feed at 2/h, closed into 500 hourly cohorts: every unit of the
    # yield is either released or still in the pipeline.
    assert substrate.fed == pytest.approx(1000.0)
    assert substrate.produced + substrate.unreleased == pytest.approx(1000.0 * 200.0)


def test_a_steady_state_initial_inventory_gives_a_flat_output_from_t_zero():
    """`initial_feed_rate` precomputes the cohort ladder a constant feed would
    have left behind, so a year-long study needs no start-up transient: the
    output is `feed x yield` from the first hour."""
    layout = _hourly(
        conversions=[{"name": "D", "cohort_interval": 1, "substrates": [
            {"name": "s", "feed": "0.5", "yield": 200.0, "half_life": 60, "steps": 12,
             "initial_feed_rate": 0.5}]}])
    env = _run(layout, 100)
    substrate = _substrate(env, "D", "s")

    # 0.5 t/h x 200 Nm3/t = 100 Nm3/h, held flat -- the peak is the steady rate.
    assert substrate.peak == pytest.approx(100.0, abs=1e-6)
    assert substrate.produced == pytest.approx(100.0 * 100.0, rel=1e-9)


def test_a_year_of_hourly_cohorts_costs_one_event_per_hour():
    """The compute claim (D64): three substrates x 12-step curves x 8760 hourly
    cohorts re-evaluate 8761 times, NOT 3 x 8760 x 12. Every curve step lands on
    the shared cohort grid and a cohort close is `steps + 1` dict writes."""
    substrates = [{"name": f"s{i}", "feed": "1.0", "yield": 200.0, "half_life": 60, "steps": 12}
                  for i in range(3)]
    layout = _hourly(
        conversions=[{"name": "D", "cohort_interval": 1, "substrates": substrates}],
        levels=[{"name": "Gas", "capacity": 1e12, "initial": 0}],
        flows=[{"name": "gas", "to": "Gas", "rate": "CONV(\"D\")"}])

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, layout)
    system = env.ctx.level_system
    calls = []
    original = system._recompute
    system._recompute = lambda: (calls.append(1), original())[1]
    system.start(8760)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 8760)
    system.sync(8760)

    assert len(calls) == 8761                   # one per cohort grid instant, plus t=0
    for substrate in system.conversions[0].substrates:
        assert substrate.produced + substrate.unreleased == pytest.approx(8760.0 * 200.0)


def test_a_malformed_conversion_and_an_unknown_conv_name_are_problems():
    from nagasim.core.schema import validate_layout_structured

    layout = _hourly(
        conversions=[{"name": "D", "cohort_interval": 0, "substrates": [
            {"name": "s", "feed": "0", "yield": 10.0, "curve": [[4, 0.4], [10, 0.9]]},
            {"name": "s"}]}],
        flows=[{"name": "gas", "rate": "CONV(\"Other\")"}])

    codes = {p.code for p in validate_layout_structured(layout)}
    assert {"E_CONVERSION_INTERVAL", "E_CONVERSION_CURVE", "E_CONVERSION_SUBSTRATE",
            "E_CONVERSION_UNKNOWN"} <= codes


def test_a_conversion_is_cloned_per_submodel_instance():
    """Two digesters are two independent cohort pipelines (item 9): a
    `scoped.conversions` key clones the template, and the clone's feed
    expression reads its own instance's flow."""
    from nagasim.core import submodels

    layout = _hourly(
        blocks=[{"name": "a", "type": "SUBMODEL", "position": [0, 0],
                 "params": {"definition": "Digester", "arguments": {"tonnes": 2.0}}},
                {"name": "b", "type": "SUBMODEL", "position": [1, 0],
                 "params": {"definition": "Digester", "arguments": {"tonnes": 5.0}}}],
        levels=[{"name": "Gas", "capacity": 1e9, "initial": 0}],
        flows=[{"name": "Feed", "rate": "tonnes"},
               {"name": "Out", "to": "Gas", "rate": "CONV(\"$Dig\")"}],
        conversions=[{"name": "Dig", "cohort_interval": 1, "substrates": [
            {"name": "s", "feed": "FLOW(\"$Feed\")", "yield": 100.0, "half_life": 10}]}],
        submodels={"Digester": {"blocks": [], "links": [],
                                "parameters": [{"name": "tonnes", "default": 1.0}],
                                "scoped": {"levels": {"Gas": {}}, "flows": {"Feed": {}, "Out": {}},
                                           "conversions": {"Dig": {}}}}})

    flat = submodels.flatten(layout)
    names = [c["name"] for c in flat["conversions"]]

    assert names == ["a/Dig", "b/Dig"]      # the template itself left the layout
    assert flat["conversions"][0]["substrates"][0]["feed"] == 'FLOW("a/Feed")'
    assert flat["conversions"][1]["locals"] == {"tonnes": 5.0}


# ======================================================== G2: blended levels
def test_a_blended_level_conserves_its_component_exactly():
    """The zero-order hold decides WHAT SHARE an outflow carries; it can never
    create or destroy the component, because the level loses exactly what the
    outflows take. In + initial - out = final content, to the last unit."""
    layout = _hourly(
        levels=[{"name": "Tank", "capacity": 100, "initial": 40,
                 "components": [{"name": "x", "initial_fraction": 0.25}]}],
        flows=[{"name": "in", "to": "Tank", "rate": "3.0", "components": {"x": 0.8}},
               {"name": "out", "from": "Tank", "rate": "2.0"}])

    env = _run(layout, 25)
    tank = env.ctx.level_system.levels["Tank"]

    assert tank.level == pytest.approx(40 + 25)
    assert tank.comp_in["x"] == pytest.approx(0.8 * 3.0 * 25)
    assert tank.content["x"] == pytest.approx(0.25 * 40 + tank.comp_in["x"] - tank.comp_out["x"])
    # The blend really moved towards the inflow's 0.8 and stayed inside it.
    assert 0.25 < tank.fraction("x") < 0.8


def test_the_hold_is_re_evaluated_before_the_fraction_drifts_past_its_tolerance():
    """The drift crossing is solved in closed form, so the fraction an outflow
    draws at is never more than `blend_tolerance` away from the true one."""
    layout = _hourly(
        levels=[{"name": "Tank", "capacity": 1000, "initial": 100, "blend_tolerance": 0.01,
                 "components": [{"name": "x", "initial_fraction": 0.0}]}],
        flows=[{"name": "in", "to": "Tank", "rate": "10.0", "components": {"x": 1.0}},
               {"name": "out", "from": "Tank", "rate": "4.0"}])

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, layout)
    system = env.ctx.level_system
    system.start(50)
    for start in model.start_blocks():
        start.self_run()
    tank = system.levels["Tank"]
    worst = 0.0
    for t in range(1, 51):
        step_until(env, t)
        system.sync(t)
        worst = max(worst, abs(tank.fraction("x") - tank.hold["x"]))

    assert worst <= 0.01 + 1e-9
    assert tank.comp_in["x"] == pytest.approx(10.0 * 50)
    assert tank.content["x"] == pytest.approx(tank.comp_in["x"] - tank.comp_out["x"])


def test_draining_a_blended_level_holds_its_fraction_exactly():
    """With no inflow the hold is EXACT, not an approximation: `C - f L` has
    derivative `in_rate (f_in - f)`, which is 0. So a tank emptying keeps its
    blend to the last unit and needs no re-evaluation at all."""
    layout = _hourly(
        levels=[{"name": "Tank", "capacity": 100, "initial": 80,
                 "components": [{"name": "x", "initial_fraction": 0.6}]}],
        flows=[{"name": "out", "from": "Tank", "rate": "2.0"}])

    env = _run(layout, 30)
    tank = env.ctx.level_system.levels["Tank"]

    assert tank.level == pytest.approx(20.0)
    assert tank.fraction("x") == pytest.approx(0.6, abs=1e-12)
    assert tank.comp_out["x"] == pytest.approx(0.6 * 60.0)


# ================================================ G3: equipment-limited flows
def _engine_layout(flow_extra=None, failures=None, schedules=None, resource=None):
    return _hourly(
        resources=[resource or {"name": "Engine", "capacity": 1, "discipline": "FIFO"}],
        failures=failures or [],
        schedules=schedules or [],
        levels=[{"name": "Tank", "capacity": 1e6, "initial": 1e5}],
        flows=[{"name": "burn", "from": "Tank", "rate": "AVAILABLE(\"Engine\") * 10",
                **(flow_extra or {})}])


def test_a_failure_stops_an_available_limited_flow_for_exactly_the_downtime():
    """D66: the rate reads AVAILABLE(), and the level engine is woken the
    instant a failure's blocker takes the unit -- no polling, no time step. 100
    hours at 10/h minus a 12 h outage is exactly 880."""
    layout = _engine_layout(failures=[{
        "name": "maint", "resources": ["Engine"],
        "uptime": {"distribution": "Constant", "dist_params": {"value": 40}},
        "downtime": {"distribution": "Constant", "dist_params": {"value": 12}}}])

    env = _run(layout, 100)
    burn = env.ctx.level_system.flow_index["burn"]

    # Outages start at 40 and at 92: 12 h and 8 h inside the window.
    assert burn.total == pytest.approx((100 - 12 - 8) * 10.0)


def test_a_capacity_schedule_derates_the_same_flow_proportionally():
    layout = _engine_layout(
        resource={"name": "Engine", "capacity": 4, "discipline": "FIFO",
                  "capacity_schedule": "shift"},
        schedules=[{"name": "shift", "kind": "capacity", "repeat": True,
                    "points": [[10, 4], [10, 2]]}])

    env = _run(layout, 40)
    burn = env.ctx.level_system.flow_index["burn"]

    # 4 units for 10 h then 2 for 10 h, twice: (4 + 2) / 2 x 10 x 40 h.
    assert burn.total == pytest.approx((20 * 4 + 20 * 2) * 10.0)


def test_a_flow_threshold_counts_starts_and_charges_one_lump_per_start():
    """The DEVLOG gap "a generator START cost needs a flow threshold" (D66): a
    rising crossing of the realised rate is a start, counted and charged."""
    layout = _engine_layout(
        flow_extra={"thresholds": [{"value": 5, "direction": "rising", "cost": 30,
                                    "cost_item": "Engine starts", "category": "Operations"}]},
        failures=[{"name": "maint", "resources": ["Engine"],
                   "uptime": {"distribution": "Constant", "dist_params": {"value": 40}},
                   "downtime": {"distribution": "Constant", "dist_params": {"value": 5}}}])

    env = _run(layout, 100)
    burn = env.ctx.level_system.flow_index["burn"]

    # t=0 (first start), then back after each of the two outages.
    assert burn.starts == 3
    assert env.ctx.cost_items["Engine starts"].amount == pytest.approx(90.0)
    assert env.ctx.cost_items["Engine starts"].category == "Operations"


def test_min_rate_is_semi_continuous_and_adds_no_event_of_its_own():
    """The non-chattering rule (D66): the gate is applied to the rate the
    expression ASKED for, before saturation, so it never schedules anything.
    A flow whose demand slides below its minimum load goes straight to 0."""
    layout = _hourly(
        levels=[{"name": "Tank", "capacity": 100, "initial": 100}],
        flows=[{"name": "draw", "from": "Tank", "rate": "LEVEL(\"Tank\") / 10",
                "min_rate": 4.0}],
        timeseries_inputs=[{"name": "tick", "start": 0, "step": 1, "values": [0, 1],
                            "beyond": "repeat"}])

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, layout)
    system = env.ctx.level_system
    calls = []
    original = system._recompute
    system._recompute = lambda: (calls.append(1), original())[1]
    system.start(60)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 60)
    system.sync(60)
    draw = system.flow_index["draw"]

    assert draw.actual == 0.0                   # demand fell under 4/h: off, not throttled
    assert system.levels["Tank"].level > 30.0
    # Only the series knots re-evaluate: the gate itself schedules nothing.
    assert len(calls) <= 62


# ============================================================== the demo model
@pytest.fixture(scope="module")
def demo():
    return api.run_model(str(DEMO), replications=1, seed=0)


def test_the_biogas_demo_conserves_gas_methane_and_feed(demo):
    conversions = {row["Conversion"]: row for _, row in demo["conversion_summary"].iterrows()}
    flows = {row["Flow"]: row for _, row in demo["flow_summary"].iterrows()}
    holder = demo["level_summary"].set_index("Level").loc["Gas holder"]
    component = demo["component_summary"].iloc[0]

    # Every tonne fed is either gas already released or still in the pipeline.
    for name, yield_per_unit in (("Digester/maize", 200.0), ("Digester/slurry", 25.0)):
        row = conversions[name]
        assert row["Produced"] + row["Unreleased"] == \
               pytest.approx(row["Total In"] * yield_per_unit)
    # The gas the digester released is the gas the holder received.
    assert flows["Biogas"]["Total"] == pytest.approx(
        conversions["Digester/maize"]["Produced"] + conversions["Digester/slurry"]["Produced"])
    # ... and the holder's balance closes over the CHP and the flare.
    assert holder["Total Out"] == pytest.approx(flows["CHP gas"]["Total"] + flows["Flare"]["Total"])
    assert holder["Final"] == pytest.approx(300 + holder["Total In"] - holder["Total Out"])
    # Methane, over a run whose feed mix changes every other day.
    assert component["Final Content"] == pytest.approx(
        0.53 * 300 + component["Total In"] - component["Total Out"])
    assert 0.52 < component["Min Fraction"] <= component["Max Fraction"] < 0.60


def test_the_demo_chp_starts_are_counted_charged_and_bounded(demo):
    flows = {row["Flow"]: row for _, row in demo["flow_summary"].iterrows()}
    costs = {row["Item"]: row for _, row in demo["cost_summary"].iterrows()}
    chp = flows["CHP gas"]

    assert costs["CHP starts"]["Amount"] == pytest.approx(25.0 * chp["Starts"])
    # Semi-continuous: the engine is off or at its minimum load, never between.
    assert chp["Peak Rate"] == pytest.approx(250.0)
    # The hysteresis band (two level thresholds writing chp_gate) keeps the
    # cycling to a few per day over 30 days, not one per event.
    assert 0 < chp["Starts"] < 30 * 12
