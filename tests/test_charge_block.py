"""CHARGE block (Phase 15 M4, D825-D829): per-entity charging under a shared
site cap. Every expected value is the hand derivation in
docs/superpowers/specs/2026-10-26-phase15-m4-design.md, "Tests", cases 1-10.

Vehicles come from an order book (release time + attributes need/kw/limit),
pass CHARGE, get `left_at` stamped and are disposed; the block's power is the
variable `kw`, read by a meter flow whose Total and Peak Rate are the site's.
"""
import pytest

from nagasim.core.schema import validate_model_structured

from conftest import layout as make_layout, run_env


def _layout(cars, unit="hours", **charge):
    params = {"energy": "entity.need", "max_power": "entity.kw", "time_limit": "entity.limit",
              "time_limit_unit": "hours", "power_variable": "kw", **charge}
    blocks = [
        {"name": "cars", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "EV", "arrival_mode": "Orders"}},
        {"name": "charge", "type": "CHARGE", "position": [100, 0], "params": params},
        {"name": "stamp", "type": "ASSIGN", "position": [200, 0],
         "params": {"assignments": [{"target": "entity.left_at", "expr": "TNOW"}]}},
        {"name": "gone", "type": "DISPOSE", "position": [300, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}]
    links = [{"source": a, "target": b} for a, b in
             (("cars", "charge"), ("charge", "stamp"), ("stamp", "gone"))]
    doc = make_layout(blocks, links, variables=[{"name": "kw", "initial": 0.0}],
                      flows=[{"name": "meter", "rate": 'VAR("kw")', "rate_time_unit": "hours"}])
    doc["simulation"]["time_unit"] = unit
    doc["orders"] = [{"id": tag, "release": release, "quantity": 1, "source": "cars",
                      "attributes": {"tag": tag, "need": need, "kw": kw, "limit": limit}}
                     for tag, release, need, kw, limit in cars]
    return doc


def _run(cars, run_len=20.0, **kw):
    doc = _layout(cars, **kw)
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]
    env, _ = run_env(doc, run_len)
    meter = env.ctx.level_system.flow_index["meter"]
    out = {e.tag: e for e in env.ctx.all_entities if getattr(e, "tag", None)}
    return out, meter


def _check(car, left_at, charged, full, time):
    assert car.left_at == pytest.approx(left_at, abs=1e-9)
    assert car.charged_kwh == pytest.approx(charged, abs=1e-9)
    assert car.charge_full == full
    assert car.charge_time == pytest.approx(time, abs=1e-9)


def test_1_no_cap_three_cars_charge_flat_out():
    cars, meter = _run([(t, 0, 22, 11, 4) for t in "ABC"])
    for car in cars.values():
        _check(car, 2, 22, 1, 2)
    assert meter.total == pytest.approx(66)
    assert meter.peak_rate == pytest.approx(33)


def test_2_the_time_limit_cuts_a_session_short():
    cars, meter = _run([("A", 0, 30, 10, 2)])
    _check(cars["A"], 2, 20, 0, 2)
    assert meter.total == pytest.approx(20)


def test_3_equal_share_passes_a_finished_cars_share_on():
    cars, meter = _run([("A", 0, 10, 11, 9), ("B", 0, 30, 11, 9)], site_limit=20)
    _check(cars["A"], 1, 10, 1, 1)
    _check(cars["B"], 31 / 11, 30, 1, 31 / 11)
    assert meter.total == pytest.approx(40)
    assert meter.peak_rate == pytest.approx(20)


def test_4_equal_share_is_water_filling():
    cars, meter = _run([("A", 0, 8, 4, 9), ("B", 0, 22, 11, 9), ("C", 0, 22, 11, 9)],
                       site_limit=20)
    _check(cars["A"], 2, 8, 1, 2)
    _check(cars["B"], 2.6, 22, 1, 2.6)
    _check(cars["C"], 2.6, 22, 1, 2.6)
    assert meter.total == pytest.approx(52)
    assert meter.peak_rate == pytest.approx(20)


def test_5_first_come_serves_in_plug_order():
    cars, meter = _run([("A", 0, 8, 4, 9), ("B", 0, 22, 11, 9), ("C", 0, 22, 11, 9)],
                       site_limit=20, sharing="first_come")
    _check(cars["A"], 2, 8, 1, 2)
    _check(cars["B"], 2, 22, 1, 2)
    _check(cars["C"], 34 / 11, 22, 1, 34 / 11)
    assert meter.total == pytest.approx(52)
    assert meter.peak_rate == pytest.approx(20)


@pytest.mark.parametrize("sharing, a_left, b_time", [("equal", 2.5, 1.0), ("first_come", 2.0, 1.5)])
def test_6_an_arrival_re_shares_the_cap(sharing, a_left, b_time):
    cars, meter = _run([("A", 0, 20, 10, 9), ("B", 1, 5, 10, 9)], site_limit=10, sharing=sharing)
    _check(cars["A"], a_left, 20, 1, a_left)
    _check(cars["B"], 1 + b_time, 5, 1, b_time)
    assert meter.total == pytest.approx(25)
    assert meter.peak_rate == pytest.approx(10)


def test_7_a_starved_car_leaves_at_its_limit_with_nothing():
    cars, _ = _run([("A", 0, 100, 10, 10), ("B", 0, 5, 10, 2)], site_limit=10,
                   sharing="first_come")
    _check(cars["B"], 2, 0, 0, 2)


def test_8_a_full_car_can_stay_plugged_until_its_limit():
    cars, meter = _run([("A", 0, 10, 10, 4)], leave_when_full=False)
    _check(cars["A"], 4, 10, 1, 1)
    assert meter.total == pytest.approx(10)


def test_9_minutes_model_converts_the_limit_and_its_origin():
    cars, meter = _run([("A", 0, 30, 10, 2)], unit="minutes", run_len=600)
    _check(cars["A"], 120, 20, 0, 2)
    assert meter.total == pytest.approx(20)
    cars, _ = _run([("B", 90, 30, 10, 2)], unit="minutes", run_len=600, time_limit_from="60")
    _check(cars["B"], 180, 15, 0, 1.5)
    cars, _ = _run([("C", 150, 30, 10, 2)], unit="minutes", run_len=600, time_limit_from="0")
    _check(cars["C"], 150, 0, 0, 0)


def test_10_edges_need_zero_limit_zero_power_zero():
    cars, meter = _run([("N", 0, 0, 10, 4), ("L", 0, 10, 10, 0), ("P", 0, 10, 0, 3)])
    _check(cars["N"], 0, 0, 1, 0)
    _check(cars["L"], 0, 0, 0, 0)
    _check(cars["P"], 3, 0, 0, 3)
    assert meter.total == pytest.approx(0)


@pytest.mark.parametrize("cap, left_at, peak", [(0, 2.0, 10.0), (9, 18 / 8.1, 9.0)])
def test_11_efficiency_puts_grid_kw_on_the_variable_and_caps_the_grid_side(cap, left_at, peak):
    cars, meter = _run([("A", 0, 18, 9, 9)], efficiency=0.9, site_limit=cap)
    _check(cars["A"], left_at, 18, 1, left_at)
    assert meter.total == pytest.approx(20)
    assert meter.peak_rate == pytest.approx(peak)


def test_a_bad_sharing_rule_efficiency_and_a_stay_without_limit_are_problems():
    doc = _layout([("A", 0, 1, 1, 1)], sharing="fair", leave_when_full=False, time_limit="",
                  power_variable="nope", efficiency=0)
    codes = [p.code for p in validate_model_structured(doc)]
    assert codes.count("E_CHARGE") == 3
    assert "E_UNDECLARED_VARIABLE" in codes
