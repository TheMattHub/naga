"""Independent oracle tests for PHASE5 SD-2 (docs/PHASE5_PLAN.md, "Phase 5A:
system dynamics", Constructs table SD-2 row and "Design refinements": Delay
records, inline SMOOTH/DELAY1/DELAY3/DELAYN/DELAYP shorthand, auxiliaries,
STEP/PULSE/RAMP/IF, level `initial` as an expression).

Written from the SPEC, not from `core/sd.py`: every expected value is a
closed form or a hand derivation for the DISCRETE Euler scheme the design
text describes (each construct's differential equation, stepped once per
grid point, rate held constant over the step -- the same convention
`test_sd_oracles.py` uses for SD-1), cross-checked by a small reference loop
in this file. No xfail: SD-2 has not landed yet in this working tree, so most
of these are expected to fail or error today; run the file to see which.

Hidden-level access: the Constructs table states inline macros expand to
"hidden levels `<owner>~s<k>`". This file reads those hidden stocks straight
out of `env.ctx.level_system.levels[...]`, exactly as `test_sd_oracles.py`
reads ordinary levels -- hidden records are only skipped from *summaries*
("hidden records skipped (goldens unchanged)"), not from the runtime dict.
That naming is the one real guess in this file; if SD-2 lands a different
scheme the affected tests need a rename, not a redesign (see the report).
"""
import math

import pytest
from scipy import stats

from nagasim.api import run_model

from conftest import layout as make_layout, problem_codes, run_env


# --------------------------------------------------------------- run helpers
def _run(layout, run_len, seed=0):
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _level(env, name):
    return env.ctx.level_system.levels[name]


def _sim(time_limit, dt=None, method="euler"):
    section = {"time_limit": time_limit, "warmup": 0, "replications": 1}
    if dt is not None:
        section["sd"] = {"dt": dt, "method": method}
    return section


def _sd_layout(**sections):
    """A blockless continuous-only model (SD-1's own convention)."""
    return make_layout([], [], **sections)


# The blockless-model skip (PHASE5_PLAN "Design refinements": SD-1 skips
# E_NO_CREATE/E_NO_DISPOSE "when `blocks == []` and the model declares
# levels, flows or populations") is stated in terms of levels/flows, not
# auxiliaries alone -- so a pure-aux layout needs one inert level/flow pair
# to validate. Not part of what these tests check.
_DUMMY_LEVEL = {"name": "_dummy", "initial": 0.0}
_DUMMY_FLOW = {"name": "_noop", "from": "_dummy", "rate": "0"}


# ============================================================ reference loops
def _euler_delay_chain(tau, order, dt, n, x=1.0, init=0.0):
    """`order` first-order stages in series, theta = tau/order, each stage i
    fed by the previous stage's OUTFLOW (stage 1 fed by the constant input
    x): S_i += dt * (in_i - S_i/theta), all rates computed from the PRE-step
    state (synchronous Euler, D89's convention). Returns (S_last_after_n,
    theta) so a caller picks material (S/theta) or the raw stock as needed.
    Initial stock is `init * theta` per the design text."""
    theta = tau / order
    S = [init * theta] * order
    for _ in range(n):
        ins = [x] + [S[i] / theta for i in range(order - 1)]
        S = [S[i] + dt * (ins[i] - S[i] / theta) for i in range(order)]
    return S[-1], theta


def _euler_smooth(tau, dt, n, x=1.0, init=0.0):
    """Order-1 information delay: dS/dt = (x - S)/tau, output = S itself
    (not S/tau -- SMOOTH delays a level-like quantity, not a flow)."""
    S = init
    for _ in range(n):
        S = S + dt * (x - S) / tau
    return S


def test_reference_loops_agree_with_closed_forms():
    """Sanity on the helpers before trusting them against the engine."""
    tau, dt, n = 4.0, 0.1, 30
    r = 1 - dt / tau
    S1, theta1 = _euler_delay_chain(tau, 1, dt, n)
    assert theta1 == tau
    assert S1 / theta1 == pytest.approx(1 - r ** n, rel=1e-12)     # DELAY1 output
    assert _euler_smooth(tau, dt, n) == pytest.approx(1 - r ** n, rel=1e-12)  # SMOOTH output


# =========================================================== 1. DELAY1 (D_material)
def test_delay1_step_response_matches_1_minus_1_minus_dt_over_tau_pow_n():
    """DELAY1 of a unit step (from 0) under Euler: derived from dS/dt =
    input - S/tau with input=1, S(0)=0 -> S_n = tau*(1-(1-dt/tau)^n), so the
    macro's output S_n/tau = 1-(1-dt/tau)^n exactly (PHASE5_PLAN SD-2 row).
    Inline shorthand `DELAY1(x, tau)` inside an auxiliary "y" -> hidden
    level "y~s1" holding S."""
    tau, dt, n = 4.0, 0.5, 6
    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'DELAY1(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    S_engine = _level(env, "y~s1").level
    expected = 1 - (1 - dt / tau) ** n
    ref_S, theta = _euler_delay_chain(tau, 1, dt, n)
    assert ref_S / theta == pytest.approx(expected, rel=1e-12)
    assert S_engine / tau == pytest.approx(expected, rel=1e-12)


# ============================================================ 2. DELAY3 vs Erlang-3
def test_delay3_continuous_limit_matches_erlang3_cdf():
    """DELAY3 of a unit step: in the continuous limit the cascade's output
    is the Erlang-3(rate=3/tau) CDF, 1 - e^{-t/theta}(1 + t/theta +
    (t/theta)^2/2) with theta=tau/3 -- three first-order lags in series fed
    a step converge to a gamma-shaped rise. A small dt=tau/2000 keeps the
    Euler bias well under the loose 1e-3 tolerance stated here."""
    tau, T = 4.0, 3.0
    dt, n = tau / 2000, round(3.0 / (tau / 2000))
    theta = tau / 3
    erlang3 = 1 - math.exp(-T / theta) * (1 + T / theta + (T / theta) ** 2 / 2)

    ref_S, ref_theta = _euler_delay_chain(tau, 3, dt, n)
    assert ref_S / ref_theta == pytest.approx(erlang3, abs=1e-3)

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'DELAY3(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(T, dt, "euler"))
    env = _run(layout, T)
    engine_out = _level(env, "y~s3").level / theta
    assert engine_out == pytest.approx(erlang3, abs=1e-3)


def test_delay3_exact_discrete_matches_a_3_stage_euler_reference():
    """Same model, a coarse dt=0.5: the engine must match the 3-stage Euler
    loop bit-for-bit (rel 1e-12), independent of the continuous-limit
    comparison above."""
    tau, dt, n = 4.0, 0.5, 6
    theta = tau / 3
    ref_S, ref_theta = _euler_delay_chain(tau, 3, dt, n)
    assert ref_theta == theta

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'DELAY3(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    assert _level(env, "y~s3").level == pytest.approx(ref_S, rel=1e-12)


# ============================================================ 3. DELAYN vs gamma.cdf
def test_delayn_continuous_limit_matches_scipy_gamma_cdf():
    """DELAYN(x, tau, n) with n=5: continuous limit -> gamma.cdf(t, a=n,
    scale=tau/n) (the general Erlang-n result item 2 specialises to n=3)."""
    tau, order, T = 4.0, 5, 3.0
    dt = tau / 2000
    n_steps = round(T / dt)
    expected = stats.gamma.cdf(T, a=order, scale=tau / order)

    ref_S, ref_theta = _euler_delay_chain(tau, order, dt, n_steps)
    assert ref_S / ref_theta == pytest.approx(expected, abs=1e-3)

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'DELAYN(STEP(1,0), %r, %d, 0)' % (tau, order)}],
        simulation=_sim(T, dt, "euler"))
    env = _run(layout, T)
    engine_out = _level(env, "y~s%d" % order).level / (tau / order)
    assert engine_out == pytest.approx(expected, abs=1e-3)


def test_delayn_exact_discrete_matches_an_n_stage_euler_reference():
    tau, order, dt, n = 4.0, 5, 0.5, 6
    theta = tau / order
    ref_S, ref_theta = _euler_delay_chain(tau, order, dt, n)
    assert ref_theta == theta

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'DELAYN(STEP(1,0), %r, %d, 0)' % (tau, order)}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    assert _level(env, "y~s%d" % order).level == pytest.approx(ref_S, rel=1e-12)


# ==================================================================== 4. conservation
def test_material_delay_conserves_input_minus_output_minus_stock_in_transit():
    """For a material delay, input integral = output integral + stock in
    transit at every instant (integrate dS/dt = input - output). Two
    separate constructs of the SAME shape share the same deterministic
    trajectory: a plain level "InInt" fed directly by the input expression,
    and an aux "y" = DELAY1(input, tau) whose hidden stock "y~s1" IS the
    stock in transit, feeding a level "OutInt" via flow rate DELAY1(...)/tau
    (the macro's own output). Checked at several run lengths (before,
    during and after the transient), rel 1e-9."""
    tau, dt = 4.0, 0.25
    for T in (1.0, tau, 3 * tau):
        layout = _sd_layout(
            levels=[{"name": "InInt", "initial": 0.0, "min": None},
                    {"name": "OutInt", "initial": 0.0, "min": None}],
            flows=[{"name": "capture_in", "rate": "STEP(1,0)", "to": "InInt"},
                   {"name": "capture_out", "to": "OutInt",
                    "rate": 'DELAY1(STEP(1,0), %r, 0)' % tau}],
            auxiliaries=[],
            simulation=_sim(T, dt, "euler"))
        env = _run(layout, T)
        in_int = _level(env, "InInt").level
        out_int = _level(env, "OutInt").level
        stock = _level(env, "capture_out~s1").level
        assert in_int == pytest.approx(out_int + stock, rel=1e-9), T


# ============================================================ 5. SMOOTH
def test_smooth_matches_the_same_exponential_approach_closed_form():
    """SMOOTH(x, tau) (order-1 information delay) of a unit step from 0:
    dS/dt=(x-S)/tau, output=S directly. Algebraically this reduces to the
    SAME closed form as DELAY1's output, 1-(1-dt/tau)^n, even though the
    internal recursion differs (S itself is the answer here, not S/tau)."""
    tau, dt, n = 4.0, 0.5, 6
    expected = 1 - (1 - dt / tau) ** n
    assert _euler_smooth(tau, dt, n) == pytest.approx(expected, rel=1e-12)

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'SMOOTH(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(n * dt, dt, "euler"))
    env = _run(layout, n * dt)
    assert _level(env, "y~s1").level == pytest.approx(expected, rel=1e-12)


def test_smooth_continuous_limit_is_the_exponential_1_minus_e_to_minus_t_over_tau():
    tau, T = 4.0, 3.0
    dt = tau / 2000
    n = round(T / dt)
    exact = 1 - math.exp(-T / tau)
    assert _euler_smooth(tau, dt, n) == pytest.approx(exact, abs=1e-4)

    layout = _sd_layout(
        levels=[_DUMMY_LEVEL], flows=[_DUMMY_FLOW],
        auxiliaries=[{"name": "y", "expr": 'SMOOTH(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(T, dt, "euler"))
    env = _run(layout, T)
    assert _level(env, "y~s1").level == pytest.approx(exact, abs=1e-4)


# ============================================================ 6. DELAYP (pipeline)
def test_delayp_reproduces_an_off_grid_step_exactly_at_t0_plus_tau():
    """DELAYP is event-driven (Constructs table: "exact", no dt dependence;
    unlike SMOOTH/DELAYn it is not listed as needing `sd` -- E_SD_REQUIRES_DT
    is only for the macros that need a grid to integrate). A level fed by
    DELAYP(STEP(1,0), tau) should be 0 until t0+tau, then rise at exactly
    the input's slope -- here a step, so the fed level is h*(T-t0-tau) for
    T>t0+tau. tau=1.3 is deliberately off any dt=0.5 grid point."""
    tau, dt, T = 1.3, 0.5, 4.0
    expected = 1.0 * (T - tau)
    for sd in (True, False):
        sections = {"levels": [{"name": "Out", "initial": 0.0, "min": None}],
                     "flows": [{"name": "in", "to": "Out", "rate": 'DELAYP(STEP(1,0), %r)' % tau}],
                     "simulation": _sim(T, dt, "euler") if sd else _sim(T)}
        layout = _sd_layout(**sections)
        env = _run(layout, T)
        assert _level(env, "Out").level == pytest.approx(expected, rel=1e-9), sd


# ==================================================== 7. STEP/PULSE/RAMP w/o sd
def test_step_without_sd_gives_an_exact_level_change():
    """A level fed by STEP(h,t0) alone (no `simulation.sd`): the literal
    time t0 is registered as a change instant (Constructs table), so the
    level is exactly h*(T-t0) for T>t0 -- no grid needed."""
    h, t0, T = 5.0, 3.0, 10.0
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 0.0, "min": None}],
        flows=[{"name": "in", "to": "L", "rate": "STEP(%r,%r)" % (h, t0)}],
        simulation=_sim(T))
    env = _run(layout, T)
    assert _level(env, "L").level == pytest.approx(h * (T - t0), rel=1e-12)


def test_pulse_area_without_sd_is_exact():
    """PULSE(h,t0,width): the fed level's final value is exactly h*width
    once T has passed the pulse (two exact change instants, t0 and
    t0+width, both registered without a grid)."""
    h, t0, width, T = 4.0, 2.0, 1.5, 10.0
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 0.0, "min": None}],
        flows=[{"name": "in", "to": "L", "rate": "PULSE(%r,%r,%r)" % (h, t0, width)}],
        simulation=_sim(T))
    env = _run(layout, T)
    assert _level(env, "L").level == pytest.approx(h * width, rel=1e-12)


def test_ramp_is_dynamic_and_rk4_integrates_it_exactly():
    """RAMP(slope,t0[,t1]) is a DYNAMIC rate (PHASE5 D89 lists RAMP with
    TNOW): without `simulation.sd` a rate is held between events, so the
    rising part needs the grid. Its literal t0/t1 are change instants, so
    with t0 and t1 on the grid RK4 (exact for a linear rate) gives the exact
    area: 0.5*slope*(t1-t0)^2 up to t1, then the held value slope*(t1-t0)
    (Vensim convention) for the rest."""
    slope, t0, t1, T = 2.0, 1.0, 3.0, 5.0
    expected = 0.5 * slope * (t1 - t0) ** 2 + slope * (t1 - t0) * (T - t1)
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 0.0, "min": None}],
        flows=[{"name": "in", "to": "L", "rate": "RAMP(%r,%r,%r)" % (slope, t0, t1)}],
        simulation=_sim(T, 0.5, "rk4"))
    env = _run(layout, T)
    assert _level(env, "L").level == pytest.approx(expected, rel=1e-12)


# ==================================================================== 8. AUX
def test_aux_indirection_is_bit_identical_to_the_inline_expression():
    """A flow reading AUX("a") with a's expr = k*LEVEL(name) must behave
    identically to the same flow with the expression inlined -- same seed,
    same grid, same everything else, only the indirection differs."""
    k, dt, T = 0.3, 0.5, 5.0
    inline = _sd_layout(
        levels=[{"name": "L", "initial": 50.0}],
        flows=[{"name": "out", "from": "L", "rate": '%r*LEVEL("L")' % k}],
        simulation=_sim(T, dt, "euler"))
    via_aux = _sd_layout(
        levels=[{"name": "L", "initial": 50.0}],
        flows=[{"name": "out", "from": "L", "rate": 'AUX("a")'}],
        auxiliaries=[{"name": "a", "expr": '%r*LEVEL("L")' % k}],
        simulation=_sim(T, dt, "euler"))
    a = _level(_run(inline, T), "L").level
    b = _level(_run(via_aux, T), "L").level
    assert a == b          # bit-identical, not just approx


def test_aux_cycle_is_a_problem():
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 10.0}],
        flows=[{"name": "out", "from": "L", "rate": "1"}],
        auxiliaries=[{"name": "a", "expr": 'AUX("b")'}, {"name": "b", "expr": 'AUX("a")'}])
    codes = problem_codes(layout, severity=None)
    assert "E_AUX_CYCLE" in codes


def test_unknown_aux_is_a_problem():
    layout = _sd_layout(
        levels=[{"name": "L", "initial": 10.0}],
        flows=[{"name": "out", "from": "L", "rate": 'AUX("missing")'}])
    codes = problem_codes(layout, severity=None)
    assert "E_AUX_UNKNOWN" in codes


# =========================================================== 9. level initial expr
def test_level_initial_expression_reads_another_levels_initial_in_topological_order():
    layout = _sd_layout(
        levels=[{"name": "A", "initial": 10.0},
                {"name": "B", "initial": 'LEVEL("A")*2'}],
        flows=[{"name": "noop_a", "from": "A", "rate": "0"},
               {"name": "noop_b", "from": "B", "rate": "0"}],
        simulation=_sim(1.0))
    env = _run(layout, 0.0)
    assert _level(env, "A").level == pytest.approx(10.0, rel=1e-12)
    assert _level(env, "B").level == pytest.approx(20.0, rel=1e-12)


def test_level_initial_cycle_is_a_problem():
    layout = _sd_layout(
        levels=[{"name": "A", "initial": 'LEVEL("B")'},
                {"name": "B", "initial": 'LEVEL("A")'}],
        flows=[{"name": "noop_a", "from": "A", "rate": "0"},
               {"name": "noop_b", "from": "B", "rate": "0"}])
    codes = problem_codes(layout, severity=None)
    assert "E_LEVEL_INITIAL_CYCLE" in codes


# ================================================== 10. hidden levels stay hidden
def test_hidden_delay_stages_do_not_appear_in_level_summary():
    tau, dt, T = 4.0, 0.5, 3.0
    layout = _sd_layout(
        levels=[{"name": "Visible", "initial": 1.0}],
        flows=[{"name": "noop", "from": "Visible", "rate": "0"}],
        auxiliaries=[{"name": "y", "expr": 'DELAY3(STEP(1,0), %r, 0)' % tau}],
        simulation=_sim(T, dt, "euler"))
    frame = run_model(layout)["level_summary"]
    names = set(frame["Level"])
    assert names == {"Visible"}
    assert not any(n.startswith("y~s") for n in names)
