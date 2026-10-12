"""Independent oracle tests for PHASE5 SD-1 (docs/PHASE5_PLAN.md, "Phase 5A:
system dynamics", D89-D91 and the SD-1 milestone row).

Written from the SPEC, not from `core/levels.py`: every expected value is a
closed form or a hand derivation, cross-checked by a tiny standalone
reference loop that integrates the same ODE by the same scheme (Euler or
classic RK4) independently of the algebra used to state the closed form.
Any test whose value the engine does not yet reproduce is
`pytest.mark.xfail(strict=True)` so the file stays green either way; the
coordinator drops the marker once SD-1 lands that behaviour.
"""
import math

import pytest

from conftest import chain, create, delay, dispose, layout as make_layout, problem_codes, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    """Prepare and run one replication in-process; returns the env."""
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _level(env, name):
    return env.ctx.level_system.levels[name]


def _sim(time_limit, dt=None, method="euler"):
    """A `simulation` section, with an `sd` grid block when `dt` is given."""
    section = {"time_limit": time_limit, "warmup": 0, "replications": 1}
    if dt is not None:
        section["sd"] = {"dt": dt, "method": method}
    return section


def _sd_layout(**sections):
    """A blockless continuous-only model: a pure SD model has no CREATE or
    DISPOSE, only levels and flows (PHASE5_PLAN "Blockless models must
    validate")."""
    return make_layout([], [], **sections)


# ============================================================ reference loops
# Each helper is a plain integration loop, independent of the closed-form
# algebra used inside the tests below -- so a mistake in one cannot cancel a
# mistake in the other.
def _euler_decay_reference(L0, k, dt, n):
    """dL/dt = -k L: L += dt * (-k * L) at each of n grid steps."""
    L = L0
    for _ in range(n):
        L += dt * (-k * L)
    return L


def _rk4_decay_reference(L0, k, dt, n):
    """dL/dt = -k L: one classic RK4 step at a time."""
    L = L0
    for _ in range(n):
        k1 = -k * L
        k2 = -k * (L + dt / 2 * k1)
        k3 = -k * (L + dt / 2 * k2)
        k4 = -k * (L + dt * k3)
        L += dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
    return L


def _euler_tnow_reference(dt, n):
    """dL/dt = TNOW: a left-Riemann sum, L += dt * t at each grid point."""
    L, t = 0.0, 0.0
    for _ in range(n):
        L += dt * t
        t += dt
    return L


def _rk4_tnow_reference(dt, n):
    """dL/dt = TNOW: RK4's four stages sample t0, t0+h/2 (twice) and t0+h,
    so the weighted average (k1+2k2+2k3+k4)/6 is exactly the midpoint rate
    t0+h/2, and each step adds the EXACT area under the (linear) rate over
    that step."""
    L, t = 0.0, 0.0
    for _ in range(n):
        h = dt
        k1, k2, k3, k4 = t, t + h / 2, t + h / 2, t + h
        L += h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        t += dt
    return L


def _euler_with_mid_step_lump(L0, k, dt, T, event_t, lump):
    """Euler on a grid with one lump strictly between two grid points: sync
    along the chord to the event (current rate held constant), apply the
    lump, re-evaluate, then continue the chord to the next grid point and
    carry on as ordinary Euler (SD-1's mid-step-event rule)."""
    stops = sorted({round(i * dt, 12) for i in range(1, int(round(T / dt)) + 1)} | {event_t})
    t, L, rate = 0.0, L0, -k * L0
    applied = False
    for stop in stops:
        step = stop - t
        if step <= 0:
            continue
        L += rate * step
        t = stop
        if not applied and abs(stop - event_t) < 1e-9:
            L += lump
            applied = True
        rate = -k * L
    return L


def _euler_grid_threshold_time(L0, k, dt, threshold):
    """Walk the Euler grid; the step that crosses `threshold` is a straight
    chord (constant rate over that step), so the crossing solves exactly --
    the same linear-crossing math `_next_step` already uses for a
    piecewise-constant rate, now applied once per grid segment instead of
    once for the whole run."""
    t, L = 0.0, L0
    while True:
        rate = -k * L
        L_next = L + rate * dt
        if L_next <= threshold:
            return t + (threshold - L) / rate
        t, L = t + dt, L_next


def _euler_grid_capped_rate(cap, dt, n):
    """dL/dt = min(TNOW, cap): the desired rate TNOW is re-evaluated and
    capped once per grid step, held constant over the step like every other
    Euler rate here."""
    total, t = 0.0, 0.0
    for _ in range(n):
        total += dt * min(t, cap)
        t += dt
    return total


def test_reference_loops_agree_with_their_closed_forms():
    """Sanity on the helpers themselves, checked once here so the per-model
    tests below can compare the engine straight against the loop."""
    L0, k, dt, n = 100.0, 0.5, 0.2, 10
    assert _euler_decay_reference(L0, k, dt, n) == pytest.approx(L0 * (1 - k * dt) ** n, rel=1e-12)
    h = dt
    quartic = 1 - k * h + (k * h) ** 2 / 2 - (k * h) ** 3 / 6 + (k * h) ** 4 / 24
    assert _rk4_decay_reference(L0, k, dt, n) == pytest.approx(L0 * quartic ** n, rel=1e-12)

    dt2, n2 = 0.5, 4
    assert _euler_tnow_reference(dt2, n2) == pytest.approx(dt2 ** 2 * n2 * (n2 - 1) / 2, rel=1e-12)
    assert _rk4_tnow_reference(dt2, n2) == pytest.approx((n2 * dt2) ** 2 / 2, rel=1e-12)

    assert _euler_with_mid_step_lump(100.0, 0.5, 1.0, T=2.0, event_t=0.3, lump=-10.0) == \
        pytest.approx(24.375, rel=1e-12)
    assert _euler_grid_threshold_time(100.0, 0.5, 0.5, 50.0) == pytest.approx(11 / 9, rel=1e-12)
    assert _euler_grid_capped_rate(5.0, 1.0, 8) == pytest.approx(25.0, rel=1e-12)


# ==================================================== -kL: Euler vs RK4 (D90)
def test_euler_exponential_decay_matches_the_geometric_factor():
    """dL/dt = -k L via a flow rate `k*LEVEL(\"L\")`: Euler's grid makes this
    exactly L0*(1-k dt)^n."""
    L0, k, dt, n = 100.0, 0.5, 0.2, 10
    layout = _sd_layout(
        levels=[{"name": "L", "initial": L0, "min": None}],
        flows=[{"name": "decay", "from": "L", "rate": "0.5*LEVEL(\"L\")"}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    expected = _euler_decay_reference(L0, k, dt, n)
    assert _level(env, "L").level == pytest.approx(expected, rel=1e-12)


def test_rk4_exponential_decay_matches_the_quartic_factor():
    """Same model under RK4: the effective rate per step is the
    (k1+2k2+2k3+k4)/6 weighted average, giving the quartic factor
    1 - hk + (hk)^2/2 - (hk)^3/6 + (hk)^4/24 per step -- close to, but
    measurably distinct from, both the Euler factor and exp(-hk)."""
    L0, k, dt, n = 100.0, 0.5, 0.2, 10
    layout = _sd_layout(
        levels=[{"name": "L", "initial": L0, "min": None}],
        flows=[{"name": "decay", "from": "L", "rate": "0.5*LEVEL(\"L\")"}],
        simulation=_sim(n * dt, dt, "rk4"))
    env = _run(layout, n * dt)
    expected = _rk4_decay_reference(L0, k, dt, n)
    euler = L0 * (1 - k * dt) ** n
    exact = L0 * math.exp(-k * n * dt)
    assert abs(expected - euler) > 1e-4 * L0          # genuinely a different scheme...
    assert 0 < abs(expected - exact) < 1e-4 * L0      # ...but 4th-order close to the true ODE
    assert _level(env, "L").level == pytest.approx(expected, rel=1e-12)


# ============================================== dL/dt = TNOW: exact for RK4
def test_euler_dl_dt_tnow_is_the_left_riemann_sum():
    """rate = TNOW alone reads no LEVEL but is still DYNAMIC (D89 lists
    LEVEL, FRACTION, RAMP and TNOW), so it re-integrates on every grid
    wake-up. Euler holds the rate at the value TNOW had at the START of
    each step, so the total is the left-Riemann sum dt^2 * n(n-1)/2."""
    dt, n = 0.5, 4
    layout = _sd_layout(
        levels=[{"name": "T", "initial": 0.0}],
        flows=[{"name": "grow", "to": "T", "rate": "TNOW"}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    expected = _euler_tnow_reference(dt, n)
    assert _level(env, "T").level == pytest.approx(expected, rel=1e-12)


def test_rk4_dl_dt_tnow_is_exact_t_squared_over_two():
    """RK4's stage clock makes each step's effective rate the exact midpoint
    rate, so the total is exactly T^2/2 no matter how coarse dt is
    (PHASE5_PLAN: "RK4 is exact for this polynomial")."""
    dt, n = 0.5, 4
    T = n * dt
    layout = _sd_layout(
        levels=[{"name": "T", "initial": 0.0}],
        flows=[{"name": "grow", "to": "T", "rate": "TNOW"}],
        simulation=_sim(T, dt, "rk4"))
    env = _run(layout, T)
    expected = _rk4_tnow_reference(dt, n)
    assert expected == pytest.approx(T ** 2 / 2, rel=1e-12)
    assert _level(env, "T").level == pytest.approx(expected, rel=1e-12)


# ===================================================== mid-step event (D90)
def test_a_mid_step_transfer_syncs_the_chord_lumps_then_continues():
    """A TRANSFER strictly between two grid points (t=0.3, grid dt=1.0):
    SD-1's rule is sync along the chord to the event, apply the lump,
    re-evaluate, then a partial step to the same grid point, then carry on.

    Hand check: step [0, 0.3] at rate -50 -> L=85; lump -10 -> L=75;
    re-evaluate rate -37.5; partial step [0.3, 1.0] (0.7) -> L=48.75; grid
    step [1, 2] at the recomputed rate -24.375 -> L=24.375.
    (CREATE.start_time is typed int, so the 0.3 offset comes from a DELAY,
    not a fractional start_time.)
    """
    L0, k, dt = 100.0, 0.5, 1.0
    c = create(max_arrivals=1)
    wait = delay("wait", value=0.3)
    lump = {"name": "lump", "description": "lump", "type": "TRANSFER", "position": [1, 0],
            "params": {"level": "L", "amount": "10", "mode": "remove", "wait": False}}
    d = dispose()
    layout = make_layout(
        [c, wait, lump, d], chain(c, wait, lump, d),
        levels=[{"name": "L", "initial": L0}],
        flows=[{"name": "decay", "from": "L", "rate": "0.5*LEVEL(\"L\")"}],
        simulation=_sim(2.0, dt, "euler"))
    env = _run(layout, 2.0)
    expected = _euler_with_mid_step_lump(L0, k, dt, T=2.0, event_t=0.3, lump=-10.0)
    assert _level(env, "L").level == pytest.approx(expected, rel=1e-12)


# =========================================== threshold and max_outflow (D90)
def test_a_threshold_crossing_on_the_euler_chord_is_exact():
    """A DYNAMIC decay rate under a dt=0.5 grid: without a grid, the level's
    own target-crossing math holds the t=0 rate constant for the WHOLE run
    and would cross 50 at t=(100-50)/50=1.0; the grid instead re-evaluates
    every 0.5 and crosses within the third segment, at exactly 11/9 -- a
    measurably different instant, so this genuinely exercises the grid."""
    L0, k, dt = 100.0, 0.5, 0.5
    hold = {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Signal", "signal_name": "low", "conditions": [], "scan_interval": 1.0}}
    c = create(max_arrivals=1)
    d = dispose()
    layout = make_layout(
        [c, hold, d], chain(c, hold, d),
        levels=[{"name": "L", "initial": L0,
                 "thresholds": [{"value": 50, "direction": "falling", "signal": "low",
                                 "variable": "cross_val"}]}],
        flows=[{"name": "decay", "from": "L", "rate": "0.5*LEVEL(\"L\")"}],
        variables=[{"name": "cross_val", "initial": 0}],
        simulation=_sim(5.0, dt, "euler"))
    env = _run(layout, 5.0)
    expected = _euler_grid_threshold_time(L0, k, dt, 50.0)
    naive = (L0 - 50.0) / (k * L0)
    assert abs(expected - naive) > 0.1        # a real discriminator, not a coincidence
    entities = list(env.ctx.all_entities)
    assert entities[0].deletion_time == pytest.approx(expected, rel=1e-12)


def test_max_outflow_caps_the_rate_at_each_grid_step():
    """A dynamic outflow rate (TNOW, growing from 0) crosses a max_outflow=5
    cap partway through the run: each grid step's realised rate is
    min(TNOW-at-the-step-start, cap), so the total drawn over 8 steps of
    dt=1 is dt * sum(min(0..7, 5)) = 0+1+2+3+4+5+5+5 = 25."""
    dt, n, cap = 1.0, 8, 5.0
    layout = _sd_layout(
        levels=[{"name": "M", "initial": 100.0, "max_outflow": cap}],
        flows=[{"name": "drain", "from": "M", "rate": "TNOW"}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    drawn = _euler_grid_capped_rate(cap, dt, n)
    assert _level(env, "M").level == pytest.approx(100.0 - drawn, rel=1e-12)


# Re-evaluation count (D89) is asserted in test_sd.py::test_only_dynamic_rates_are_evaluated_on_a_grid_wake_up.


# ============================================================ goldens (D89)
def test_goldens_are_unchanged_without_simulation_sd():
    """Without `simulation.sd` nothing new runs at all (D89: "without the
    key nothing changes and every golden stays bit-identical"). This is the
    same dynamic-decay model as the Euler/RK4 tests above, but the values
    are hard-coded from a run against current HEAD (pre-SD-1): the level's
    own naive target-crossing treats the t=0 rate as constant for the WHOLE
    run, so it drives straight to 0 at t=100/50=2 in one jump and then
    holds flat -- not real exponential decay, but the documented current
    behaviour that must not move."""
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 100.0}],
        flows=[{"name": "decay", "from": "L", "rate": "0.5*LEVEL(\"L\")"}],
        simulation=_sim(5.0))
    env = _run(layout, 5.0)
    level = _level(env, "L")
    assert level.level == pytest.approx(0.0, abs=1e-12)
    assert level.time_avg(5.0) == pytest.approx(20.0, rel=1e-12)


# ================================================================ validation
def test_rk4_with_a_random_sampler_in_a_dynamic_rate_is_refused():
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 10.0}],
        flows=[{"name": "out", "from": "L", "rate": "LEVEL(\"L\")*UNIF(0.9, 1.1)"}],
        simulation=_sim(5.0, 0.5, "rk4"))
    codes = problem_codes(layout, severity=None)
    assert "E_SD_RK4_RANDOM" in codes


def test_bad_sd_settings_is_a_problem():
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 10.0}],
        flows=[{"name": "out", "from": "L", "rate": "1"}],
        simulation={"time_limit": 5, "warmup": 0, "replications": 1,
                    "sd": {"dt": -1, "method": "euler"}})
    codes = problem_codes(layout, severity=None)
    assert "E_SD_SETTINGS" in codes


def test_biflow_between_two_bounded_levels_is_a_problem():
    layout = _sd_layout(
        levels=[{"name": "A", "capacity": 100, "min": 0, "initial": 50},
                {"name": "B", "capacity": 100, "min": 0, "initial": 50}],
        flows=[{"name": "x", "from": "A", "to": "B", "rate": "1", "biflow": True}])
    codes = problem_codes(layout, severity=None)
    assert "E_FLOW_BIFLOW" in codes


def test_a_blockless_sd_model_validates_without_create_or_dispose():
    """PHASE5_PLAN design refinement: SD-1 skips E_NO_CREATE/E_NO_DISPOSE
    for a blockless model that declares levels or flows."""
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 10.0}],
        flows=[{"name": "out", "from": "L", "rate": "1"}])
    codes = problem_codes(layout, severity=None)
    assert not ({"E_NO_CREATE", "E_NO_DISPOSE"} & codes)


# ==================================================================== D91
def test_min_null_lets_a_level_go_negative_under_constant_outflow():
    """D91: `min: null` is unbounded; without it the default min=0 floors
    the level. Both are checked here so a regression in either direction
    (null not honoured, or the default silently changing) fails the same
    test."""
    unbounded = _sd_layout(levels=[{"name": "L", "initial": 10.0, "min": None}],
                            flows=[{"name": "out", "from": "L", "rate": "1"}])
    env = _run(unbounded, 20.0)
    assert _level(env, "L").level == pytest.approx(-10.0, rel=1e-12)

    bounded = _sd_layout(levels=[{"name": "L", "initial": 10.0}],
                          flows=[{"name": "out", "from": "L", "rate": "1"}])
    env = _run(bounded, 20.0)
    assert _level(env, "L").level == pytest.approx(0.0, abs=1e-12)
