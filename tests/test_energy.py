"""Engine semantics for W3-M1 "Energy per state"
(docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md).

Wire format used here: "power" on a resource/fleet dict (busy/idle/blocked/
down/off_shift, kW per unit -- absent keys 0, "blocked" defaults to "busy")
and "simulation.energy_price" (per kWh, a number or an expression). The
"simulation" section already holds every other model-wide run setting
(currency, warmup, time_unit, record_logs), so energy_price follows that
precedent rather than living at the model's top level.

The cost-join case uses UNLIMITED arrivals (max_arrivals=0, conftest's own
default) so the model never drains before its time_limit (DEVLOG #32).
"""
import pytest

from conftest import chain, create, dispose, layout, metric_value, process
from nagasim import api, experiments
from nagasim.core.movers import build_fleets
from nagasim.core.network import build_network
from nagasim.core.resource_manager import ResourceManager
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env


def _row(df, col, value):
    hit = df[df[col] == value]
    assert len(hit) == 1, f"expected exactly one row with {col}={value!r}, got {len(hit)}"
    return hit.iloc[0]


# kWh per state, Avg kW, Energy Cost, the blocked->busy fallback, the empty
# frame and the warm-up reset are the oracle's (test_energy_oracle.py cases
# 1-7); this file keeps the joins, wiring, validation and migration.
def test_energy_cost_joins_cost_summary_and_flat_kpis():
    """Oracle case 1's cycle (A=2h, D=1h, T=10h): 5h busy + 5h idle."""
    Pb, Pi, price = 10.0, 2.0, 0.20
    blocks = [create(dist_params={"value": 2.0}), process(resource="Server", value=1.0), dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1,
                              "power": {"busy": Pb, "idle": Pi}}],
                   simulation={"time_limit": 10.0, "warmup": 0, "replications": 1,
                              "time_unit": "hours", "energy_price": price})
    results = api.run_model(model, seed=0)
    total_kwh = 5 * Pb + 5 * Pi

    # cost_summary (D63's join, extended to "energy").
    cost_row = _row(results["cost_summary"], "Item", "Server")
    assert cost_row["Kind"] == "energy"
    assert cost_row["Amount"] == pytest.approx(total_kwh * price)

    # KPI keys (experiments._OBJECT_FRAMES): energy/<resource>/<column>.
    flat = experiments.flatten_kpis(results)
    assert "energy/Server/Energy Cost" in set(flat["KPI"])
    assert flat.loc[flat["KPI"] == "energy/Server/Total kWh", "Value"].iloc[0] == \
        pytest.approx(total_kwh)


def test_energy_price_with_no_power_costs_nothing():
    """A nonzero energy_price with no resource declaring power still joins no
    cost row (nothing to price) and Total Cost is unaffected."""
    blocks = [create(dist_params={"value": 1.0}, max_arrivals=5),
              process(resource="Server", value=0.5), dispose()]
    model = layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                   simulation={"time_limit": 10.0, "warmup": 0, "replications": 1,
                              "energy_price": 0.99})
    results = api.run_model(model, seed=0)
    assert results["energy_summary"].empty
    assert not (results["cost_summary"]["Kind"] == "energy").any()
    assert metric_value(results, "Total Cost") == pytest.approx(0.0)


# ------------------------------------------------------------------- fleets
def test_fleet_power_is_wired_through_build_fleets():
    """A fleet IS a resource (D68): declaring "power" on it must reach the
    same LoggedSimpyResource.power attribute a plain resource's does, with no
    fleet-specific energy code needed anywhere else."""
    env = initialize_sim_env(seed=0)
    env.ctx.network = build_network({
        "nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 10, "y": 0}],
        "segments": [{"from": "a", "to": "b", "length": 10}],
    })
    manager = ResourceManager(env)
    power = {"busy": 3.0, "idle": 0.5, "off_shift": 0.2}
    fleets = build_fleets(env, {"fleets": [{"name": "Forklifts", "count": 2, "speed": 1.0,
                                            "home": "a", "power": power}]},
                          env.ctx.network, manager)
    assert fleets["Forklifts"].resource.power == power
    assert manager.resources["Forklifts"].power == power


# ---------------------------------------------------------------- validation
def test_validation_flags_bad_power_and_energy_price():
    layout_ = {
        "schema_version": 24,
        "simulation": {"time_limit": 100, "energy_price": "not_a_declared_variable"},
        "blocks": [], "links": [],
        "resources": [{"name": "M", "capacity": 1, "power": {"busy": -1.0, "bogus": 2.0}}],
        "fleets": [],
    }
    problems = {p.code for p in validate_model_structured(layout_)}
    assert "E_RESOURCE_POWER" in problems       # busy: -1.0 is not >= 0
    assert "E_RESOURCE_POWER_STATE" in problems  # "bogus" is not a declared state
    assert "E_ENERGY_PRICE" in problems          # unknown variable name


def test_validation_flags_bad_fleet_power():
    layout_ = {
        "schema_version": 24,
        "simulation": {"time_limit": 100},
        "blocks": [], "links": [],
        "resources": [],
        "network": {"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 1, "y": 0}],
                   "segments": [{"from": "a", "to": "b", "length": 1}]},
        "fleets": [{"name": "F", "speed": 1.0, "power": {"busy": -2.0, "nonsense": 1.0}}],
    }
    problems = {p.code for p in validate_model_structured(layout_)}
    assert "E_FLEET_POWER" in problems
    assert "E_FLEET_POWER_STATE" in problems


def test_schema_migrates_v23_to_v24_with_no_power_or_price():
    """A model with no "power"/"energy_price" anywhere runs byte-identically:
    the bump is a no-op migration (both fields are optional)."""
    from nagasim.core.schema import CURRENT_VERSION, migrate

    assert CURRENT_VERSION >= 24
    v23 = {"schema_version": 23, "blocks": [], "links": [],
          "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}
    migrated = migrate(v23)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["simulation"]["energy_price"] == 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
