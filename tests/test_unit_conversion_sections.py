"""Section fields convert once, through the converter (units spec, D159)."""
import pytest

from nagasim.core import movers, sd
from nagasim.core.agents.compile import _speed_text, _tick_in_base, _trigger_in_base
from nagasim.core.entity_manager import EntityManager
from nagasim.core.network import build_network
from nagasim.core.schedules import build_schedule
from nagasim.core.timeunits import ModelUnits

MIN = ModelUnits("minutes")


def test_fleet_speeds_share_one_unit_key():
    assert movers._speeds({"speed": 1, "speed_empty": 2, "speed_unit": "m/s"}, MIN) \
        == (60.0, 120.0, 0.0, 0.0)
    assert movers._speeds({"speed": 1.5}, MIN) == (1.5, 1.5, 0.0, 0.0)


def test_network_segment_length_and_speed_limit_convert():
    net = build_network({"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 10, "y": 0}],
                         "segments": [{"from": "a", "to": "b", "length": 1,
                                       "length_unit": "kilometers", "speed": 1,
                                       "speed_unit": "m/s"}]}, MIN)
    assert (net.segments[0].length, net.segments[0].speed) == (1000.0, 60.0)
    plain = build_network({"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 10, "y": 0}],
                           "segments": [{"from": "a", "to": "b"}]})
    assert plain.segments[0].length == 10.0


def test_sd_delay_time_converts_once_in_expand():
    layout = {"simulation": {"time_unit": "minutes"}, "blocks": [], "links": [],
              "delays": [{"name": "d", "input": "1", "delay_time": 2, "delay_time_unit": "hours",
                          "kind": "material", "order": 1}]}
    out = sd.expand(layout)
    assert next(d for d in out["delays"] if d["name"] == "d")["delay_time"] == 120.0


def test_agent_speed_and_holding_cost_convert():
    assert _speed_text({"speed": "self.v", "speed_unit": "m/s"}, MIN) == "(self.v) * 60.0"
    assert _speed_text({}, MIN) == "1"
    entities = EntityManager()
    entities.load_from_json([{"name": "A", "holding_cost": 6, "holding_cost_time_unit": "hours"}],
                            MIN)
    assert entities.get_type("A")["holding_cost"] == pytest.approx(0.1)


def test_agent_transition_triggers_and_ticks_convert():
    assert _trigger_in_base({"from": "a", "to": "b", "timeout": "5", "timeout_unit": "hours"},
                            MIN) == ("300.0", None, 1.0)
    assert _trigger_in_base({"from": "a", "to": "b", "timeout": "self.t"}, MIN) \
        == ("self.t", None, 1.0)
    text, scan, factor = _trigger_in_base({"from": "a", "to": "b", "rate": "0.5",
                                           "rate_unit": "hours", "scan": 2, "scan_unit": "hours"},
                                          MIN)
    assert (text, scan) == ("0.5", 120.0) and factor == pytest.approx(1 / 60)
    assert _tick_in_base({"dt": 2, "phase": 1, "time_unit": "hours", "activation": "random"},
                         MIN) == {"dt": 120.0, "phase": 60.0, "activation": "random"}


def test_arrival_schedule_values_are_rates_per_its_unit():
    schedule = build_schedule({"name": "s", "kind": "arrival", "points": [[1, 60]],
                               "time_unit": "hours"}, "minutes")
    assert schedule.durations == [60.0] and schedule.values == [pytest.approx(1.0)]
