"""Block fields convert to the engine's base once, at load (units spec, D159)."""
import pytest

from conftest import chain, create, dispose, layout, metric_value
from nagasim import api
from nagasim.core.base_process import Block
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.sim_runner import initialize_sim_env
from nagasim.core.timeunits import ModelUnits, base_factor


def _built(type_name, params, units=ModelUnits("minutes")):
    env = initialize_sim_env(seed=0)
    env.ctx.units, env.ctx.time_unit = units, units.time
    block = BLOCK_REGISTRY[type_name].factory(env, Block(position=(0, 0), type=type_name, name="b"))
    block.set_params_from_dict(params)
    return block


def test_create_start_time_and_hold_scan_interval_convert():
    assert _built("CREATE", {"start_time": 90, "start_time_unit": "seconds"}).start_time == 1.5
    assert _built("CREATE", {"start_time": 3}).start_time == 3
    assert _built("HOLD", {"scan_interval": 2, "scan_interval_unit": "hours"}).scan_interval == 120.0


def test_conveyor_route_and_setup_units_convert():
    belt = _built("CONVEYOR", {"length": 3, "length_unit": "kilometers", "speed": 1,
                               "speed_unit": "m/s", "spacing": 500,
                               "spacing_unit": "millimeters"})
    assert (belt.length, belt.speed, belt.spacing) == (3000.0, 60.0, 0.5)
    kmh = base_factor("speed", "km/h", ModelUnits("minutes"))
    assert _built("ROUTE", {"speed": "entity.v", "speed_unit": "km/h"}).speed \
        == f"(entity.v) * {kmh!r}"
    assert _built("ROUTE", {"speed": ""}).speed == ""
    assert _built("PROCESS", {"setup": "30", "setup_unit": "seconds"})._setup_factor \
        == pytest.approx(1 / 60)


def test_a_conveyor_in_m_per_s_takes_the_same_physical_time_in_a_minutes_model():
    blocks = [create(max_arrivals=1),
              {"name": "belt", "type": "CONVEYOR", "position": [1, 0],
               "params": {"length": 60, "length_unit": "meters", "speed": 1,
                          "speed_unit": "m/s", "spacing": 1}},
              dispose()]
    kpi = api.run_model(layout(blocks, chain(*blocks)), replications=1, seed=0)
    assert metric_value(kpi, "Avg Throughput Time") == pytest.approx(1.0)
