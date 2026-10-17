"""Independent oracle tests for depth bucket L1 (loop dominance / "Loops
That Matter"), written from docs/history/LOOP-DOMINANCE-SEMANTICS-2026-09-27.md
(definitions, worked examples A-E) and docs/history/PHASE6-DEPTH-L-PLAN-2026-09-27.md
(Oracles items 1-9), NOT from nagasim/loopdominance.py -- this file is the
independent check on that module, written concurrently by another agent and
deliberately not read here.

Every expected value is either a closed form from the semantics note, a hand
derivation, or a fresh numpy integration of the same scheme the engine uses
(Euler or classic RK4) -- never the engine's own recorded output. Two oracles
(6 and 7) are the plan's own structural-equivalence checks (formulation
invariance, macro-vs-hand-built expansion) rather than closed forms; that is
how the plan itself defines those two items.

`nagasim.loopdominance` does not exist yet, so it is imported lazily inside
each test (via `_analyse`) rather than at module level, letting this file
collect cleanly before the implementation lands.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from conftest import layout as make_layout
from conftest import sd_layout as _sd_layout

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


# --------------------------------------------------------------- layout helpers
def _sim(time_limit, dt, method="euler"):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1,
            "sd": {"dt": dt, "method": method}}


def _analyse(layout, **kw):
    from nagasim import loopdominance      # lazy: the module may not exist yet
    return loopdominance.analyse(layout, **kw)


def _node_key(nodes):
    """Canonicalise a `loops` frame's Nodes cell for equality checks,
    whatever container/representation the implementation picked (a set/list
    of (section, name) pairs, or a formatted string)."""
    try:
        return frozenset(tuple(n) if isinstance(n, (list, tuple)) else n for n in nodes)
    except TypeError:
        return str(nodes)


def _loop_id_by_token(loops_df, token):
    """The single Loop id whose Nodes text contains *token* -- a flow/aux
    name chosen, at each call site, to be an unambiguous substring."""
    hits = [row.Loop for row in loops_df.itertuples() if token in str(row.Nodes)]
    assert len(hits) == 1, f"expected exactly one loop containing {token!r}, got {hits}"
    return hits[0]


def _rows(df, **eq):
    out = df
    for key, value in eq.items():
        out = out[out[key] == value]
    return out


# --------------------------------------------------------- reference formulas
def _sign(v):
    return (v > 0) - (v < 0)


def _link_score(dxz, dx, dz):
    """SDR 2020 Eq. 1, a general link x -> z (z = f(x, y, ...))."""
    if dz == 0 or dx == 0:
        return 0.0
    return abs(dxz / dz) * _sign(dxz / dx)


def _flow_stock_score(is_inflow, d_flow, d_net):
    """Improving LTM Eq. 3 (Stella v2.1), a flow -> stock link."""
    if d_net == 0:
        return 0.0
    sign = 1.0 if is_inflow else -1.0
    return sign * abs(d_flow / d_net)


def _euler_series(rate, y0, dt, n):
    """y(0..n*dt) on n Euler steps of dy/dt = rate(y); y[0] is y0."""
    ys, y = [y0], y0
    for _ in range(n):
        y = y + dt * rate(y)
        ys.append(y)
    return ys


def _rk4_series(rate, y0, dt, n):
    """Same, n classic 4-stage RK4 steps (matches tests/test_sd_oracles.py's
    own RK4 reference loops)."""
    ys, y = [y0], y0
    for _ in range(n):
        k1 = rate(y)
        k2 = rate(y + dt / 2 * k1)
        k3 = rate(y + dt / 2 * k2)
        k4 = rate(y + dt * k3)
        y = y + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        ys.append(y)
    return ys


# ================================================================ oracle 1
def test_oracle1_link_score_unit_cases():
    """Item 1: the link-score formulas (SDR 2020 Eq. 1 general link,
    Improving LTM Eq. 3 flow->stock) checked against the papers' own worked
    numbers, independent of any model or engine run. The AND clause (an
    engine-checked application of the SAME Eq. 3 formula on a real loop) is
    test_oracle3_example_b below: B_goal/B_drain are Eq. 3 applied to an
    inflow and an outflow on one stock, exactly like this Table 3 case, just
    with different numbers -- building a fresh model to hit THESE EXACT
    numbers (4/5, 1/5, ...) while remaining a real cycle would over-fit the
    construction to arbitrary literature numbers rather than the formula.
    """
    # SDR 2020 Table 1: z = 2x + y, x: 5->7, y: 4->5.
    z_prev, z_now = 2 * 5 + 4, 2 * 7 + 5
    dz = z_now - z_prev
    dxz = (2 * 7 + 4) - z_prev      # x at t, y held at t-dt
    dyz = (2 * 5 + 5) - z_prev      # y at t, x held at t-dt
    assert _link_score(dxz, 7 - 5, dz) == pytest.approx(4 / 5)
    assert _link_score(dyz, 5 - 4, dz) == pytest.approx(1 / 5)

    # SDR 2020 Table 2: z = (w + x) / y, w: 7->10, x: 2->4, y: 3->5.
    def f(w, x, y):
        return (w + x) / y
    w0, w1, x0, x1, y0, y1 = 7, 10, 2, 4, 3, 5
    z_prev, z_now = f(w0, x0, y0), f(w1, x1, y1)
    assert z_prev == pytest.approx(3.0) and z_now == pytest.approx(2.8)   # the note's own check
    dz = z_now - z_prev
    dwz = f(w1, x0, y0) - z_prev
    dxz = f(w0, x1, y0) - z_prev
    dyz = f(w0, x0, y1) - z_prev
    assert _link_score(dwz, w1 - w0, dz) == pytest.approx(5.0)
    assert _link_score(dxz, x1 - x0, dz) == pytest.approx(10 / 3, abs=1e-3)
    assert _link_score(dyz, y1 - y0, dz) == pytest.approx(-6.0)

    # Improving LTM Table 3, flow -> stock: inflow 5->10, outflow 4->5.
    net_prev, net_now = 5 - 4, 10 - 5
    d_net = net_now - net_prev
    assert _flow_stock_score(True, 10 - 5, d_net) == pytest.approx(1.25)
    assert _flow_stock_score(False, 5 - 4, d_net) == pytest.approx(-0.25)


# ================================================================ oracle 2
@pytest.mark.parametrize("method,dt", [("euler", 1.0), ("euler", 0.5),
                                        ("rk4", 1.0), ("rk4", 0.25)])
def test_oracle2_example_a_isolated_loop_is_exactly_one(method, dt):
    """Note example A: S0=100, aux g=0.1*S, inflow=g -- the sole flow on S,
    so the isolated-loop invariant (section 1) makes its score exactly +1 at
    every scored step, for any dt or method."""
    run_length = 3.0
    layout = _sd_layout(
        levels=[{"name": "S", "initial": 100.0, "min": None}],
        auxiliaries=[{"name": "g", "expr": '0.1*LEVEL("S")'}],
        flows=[{"name": "inflow", "rate": 'AUX("g")', "to": "S"}],
        simulation=_sim(run_length, dt, method))
    result = _analyse(layout)
    dom = result["loop_dominance"]
    assert not result["truncated"]
    assert len(dom["Loop"].unique()) == 1
    n = round(run_length / dt)
    assert len(dom) == n            # no row at t=0 (section 2)
    assert np.allclose(dom["Score"].to_numpy(dtype=float), 1.0, atol=1e-9)
    assert np.allclose(dom["Relative"].to_numpy(dtype=float), 1.0, atol=1e-9)
    assert dom["Dominant"].astype(bool).all()


# ================================================================ oracle 3
@pytest.mark.parametrize("method,dt", [("euler", 0.5), ("euler", 0.1),
                                        ("rk4", 0.5), ("rk4", 0.1)])
def test_oracle3_example_b_two_balancing_loops_constant_relative(method, dt):
    """Note example B: goal-seeking inflow (G-S)/tau plus a proportional
    outflow d*S give two balancing loops with CONSTANT relative scores
    -2/3 (goal loop) and -1/3 (drain loop), independent of dt and method --
    Eq. 3 (Improving LTM) applied to a real inflow/outflow pair, so this
    also stands as the engine-side check for oracle 1's flow->stock formula."""
    run_length = 2.0
    layout = _sd_layout(
        levels=[{"name": "S", "initial": 0.0, "min": None}],
        auxiliaries=[{"name": "G", "expr": "100"}],
        flows=[
            {"name": "inflow", "rate": '(AUX("G") - LEVEL("S")) / 2', "to": "S", "biflow": True},
            {"name": "outflow", "rate": '0.25*LEVEL("S")', "from": "S"},
        ],
        simulation=_sim(run_length, dt, method))
    result = _analyse(layout)
    dom, loops_df = result["loop_dominance"], result["loops"]
    assert not result["truncated"]
    goal_id = _loop_id_by_token(loops_df, "inflow")
    drain_id = _loop_id_by_token(loops_df, "outflow")
    goal_rel = dom[dom["Loop"] == goal_id]["Relative"].to_numpy(dtype=float)
    drain_rel = dom[dom["Loop"] == drain_id]["Relative"].to_numpy(dtype=float)
    n = round(run_length / dt)
    assert goal_rel.size == n and drain_rel.size == n
    assert np.allclose(goal_rel, -2 / 3, atol=1e-9)
    assert np.allclose(drain_rel, -1 / 3, atol=1e-9)


# ================================================================ oracle 4
def test_oracle4_example_c_logistic_births_deaths_relative_closed_form():
    """Note example C: births r*P (inflow), deaths r*P*P/K (outflow, P read
    twice but ONE link per section 1's "one equation, one link" rule) --
    relative R1 = K/(K+P(t)+P(t-dt)), relative B1 = -(P(t)+P(t-dt))/
    (K+P(t)+P(t-dt)), and B1 first dominates at t=23.75 under Euler dt=0.25.
    P(t) is integrated here with a fresh numpy Euler loop (never read from
    the engine)."""
    r, K, P0, dt, run_length = 0.2, 1000.0, 10.0, 0.25, 24.0
    n = round(run_length / dt)
    P = np.array(_euler_series(lambda p: r * p - r * p * p / K, P0, dt, n))

    layout = _sd_layout(
        levels=[{"name": "P", "initial": P0, "min": None}],
        flows=[
            {"name": "births", "rate": f'{r}*LEVEL("P")', "to": "P"},
            {"name": "deaths", "rate": f'{r}*LEVEL("P")*LEVEL("P")/{K}', "from": "P"},
        ],
        simulation=_sim(run_length, dt, "euler"))
    result = _analyse(layout)
    dom, loops_df = result["loop_dominance"], result["loops"]
    assert not result["truncated"]

    r1_id = _rows(loops_df, Kind="R")["Loop"].iloc[0]
    b1_id = _rows(loops_df, Kind="B")["Loop"].iloc[0]
    r1 = dom[dom["Loop"] == r1_id].sort_values("Time")
    b1 = dom[dom["Loop"] == b1_id].sort_values("Time")
    times = r1["Time"].to_numpy(dtype=float)
    assert np.allclose(times, np.arange(1, n + 1) * dt, atol=1e-9)

    sum_pt = P[1:] + P[:-1]                       # P(t) + P(t-dt) per grid row
    expected_r1 = K / (K + sum_pt)
    expected_b1 = -sum_pt / (K + sum_pt)
    assert np.allclose(r1["Relative"].to_numpy(dtype=float), expected_r1, rtol=1e-7)
    assert np.allclose(b1["Relative"].to_numpy(dtype=float), expected_b1, rtol=1e-7)

    # Anchors given verbatim in the semantics note (section 6, example C).
    idx = {round(float(t), 6): i for i, t in enumerate(times)}
    assert expected_r1[idx[0.25]] == pytest.approx(0.97992, abs=5e-6)
    assert expected_b1[idx[0.25]] == pytest.approx(-0.02008, abs=5e-6)
    assert expected_r1[idx[20.0]] == pytest.approx(0.60067, abs=5e-6)
    assert expected_r1[idx[23.5]] == pytest.approx(0.50006, abs=5e-6)
    assert expected_b1[idx[23.75]] == pytest.approx(-0.50611, abs=5e-6)

    dominant_b1_times = b1[b1["Dominant"].astype(bool)].sort_values("Time")["Time"].to_numpy(dtype=float)
    assert dominant_b1_times[0] == pytest.approx(23.75, abs=1e-9)


# ================================================================ oracle 5
def test_oracle5_example_d_sd_population_single_flow_flips_polarity():
    """Note example D on the shipped sd_population.json: one flow reads P at
    two sites (one link, one loop) -- relative +1 while the flow's own
    equation value rises, -1 from the first step after its peak; runtime
    polarity U. P(t) is re-integrated here with a fresh numpy RK4 loop
    (the file's own grid, dt=0.1, method rk4), never read from the engine."""
    layout = json.loads((MODELS / "sd_population.json").read_text())
    r, K, P0 = 0.2, 1000.0, 10.0
    dt = layout["simulation"]["sd"]["dt"]
    time_limit = layout["simulation"]["time_limit"]
    n = round(time_limit / dt)
    P = np.array(_rk4_series(lambda p: r * p * (1 - p / K), P0, dt, n))

    result = _analyse(layout)
    dom, loops_df = result["loop_dominance"], result["loops"]
    u1_id = _rows(loops_df, Kind="U")["Loop"].iloc[0]
    row = dom[dom["Loop"] == u1_id].sort_values("Time")
    times = row["Time"].to_numpy(dtype=float)
    assert np.allclose(times, np.arange(1, n + 1) * dt, atol=1e-6)

    f = r * P * (1 - P / K)                        # the flow's own equation value at P(t)
    df = f[1:] - f[:-1]
    expected = np.sign(df)                          # +1 rising, -1 falling, 0 on an exact flat step
    assert np.allclose(row["Relative"].to_numpy(dtype=float), expected, atol=1e-6)
    # A genuine polarity flip happens well inside this 50-time-unit run
    # (the continuous inflection is at P=K/2, t* = ln(99)/0.2 ~= 22.98).
    assert (expected == 1.0).any() and (expected == -1.0).any()


# ================================================================ oracle 6
@pytest.mark.parametrize("method,dt", [("euler", 0.5), ("rk4", 0.25)])
def test_oracle6_example_e_formulation_invariance(method, dt):
    """Note example E: model B's two flows replaced by one net flow whose
    rate reads two auxiliaries, in_aux=(G-S)/tau and out_aux=d*S -- Improving
    LTM's central claim is that the loop scores are unchanged (-2/3, -1/3),
    even though there is now only one flow in the stock-and-flow graph (the
    two loops run through the auxiliary layer instead of two flows)."""
    run_length = 2.0
    layout = _sd_layout(
        levels=[{"name": "S", "initial": 0.0, "min": None}],
        auxiliaries=[
            {"name": "in_aux", "expr": '(100 - LEVEL("S")) / 2'},
            {"name": "out_aux", "expr": '0.25*LEVEL("S")'},
        ],
        flows=[{"name": "net_flow", "rate": 'AUX("in_aux") - AUX("out_aux")',
                "to": "S", "biflow": True}],
        simulation=_sim(run_length, dt, method))
    result = _analyse(layout)
    dom, loops_df = result["loop_dominance"], result["loops"]
    goal_id = _loop_id_by_token(loops_df, "in_aux")
    drain_id = _loop_id_by_token(loops_df, "out_aux")
    goal_rel = dom[dom["Loop"] == goal_id]["Relative"].to_numpy(dtype=float)
    drain_rel = dom[dom["Loop"] == drain_id]["Relative"].to_numpy(dtype=float)
    assert np.allclose(goal_rel, -2 / 3, atol=1e-9)
    assert np.allclose(drain_rel, -1 / 3, atol=1e-9)


# ================================================================ oracle 7
def test_oracle7_smooth_in_a_loop_equals_the_hand_built_expansion():
    """Note section 3: a SMOOTH macro is scored through its expanded stage
    (level + flow) exactly as if the modeller had built it by hand -- the
    plan's own definition of item 7 is this structural equivalence, not a
    closed form. Model 1 uses the inline SMOOTH macro; model 2 spells out
    the same order-1 information delay as an explicit level "M" and flow
    "Mf" with `_stage_records`' own equations: M0 = S0 (Vensim's rule), Mf
    rate = (LEVEL(S) - LEVEL(M)) / tau. A probe run showed the macro's
    hidden delay RECORD is itself one more graph node beyond its stage
    level/flow (its own edges drop, but it stays a node whose expression is
    the stage's output, per the design note's "becomes an auxiliary"), so
    the hand-built model adds the matching pass-through auxiliary "M_out"
    rather than reading LEVEL("M") directly -- confirmed by both models then
    reporting the same two loop lengths (2 and 5) AND the same Total per
    loop before this test compares every scored step."""
    dt, method, run_length = 0.5, "euler", 5.0
    macro_layout = _sd_layout(
        levels=[{"name": "S", "initial": 100.0, "min": None}],
        flows=[{"name": "outflow", "rate": 'SMOOTH(LEVEL("S"), 3)*0.1', "from": "S"}],
        simulation=_sim(run_length, dt, method))
    hand_layout = _sd_layout(
        levels=[{"name": "S", "initial": 100.0, "min": None},
                {"name": "M", "initial": 100.0, "min": None}],
        auxiliaries=[{"name": "M_out", "expr": 'LEVEL("M")'}],
        flows=[
            {"name": "Mf", "rate": '(LEVEL("S") - LEVEL("M")) / 3', "to": "M", "biflow": True},
            {"name": "outflow2", "rate": 'AUX("M_out")*0.1', "from": "S"},
        ],
        simulation=_sim(run_length, dt, method))

    macro_result = _analyse(macro_layout)
    hand_result = _analyse(hand_layout)
    macro_loops, hand_loops = macro_result["loops"], hand_result["loops"]
    assert sorted(macro_loops["Length"]) == sorted(hand_loops["Length"])
    assert sorted(macro_loops["Loop"]) == sorted(hand_loops["Loop"])

    macro_dom = macro_result["loop_dominance"]
    hand_dom = hand_result["loop_dominance"]
    for loop_id in sorted(macro_loops["Loop"]):
        m = macro_dom[macro_dom["Loop"] == loop_id].sort_values("Time")["Score"].to_numpy(dtype=float)
        h = hand_dom[hand_dom["Loop"] == loop_id].sort_values("Time")["Score"].to_numpy(dtype=float)
        assert m.shape == h.shape and m.size > 0
        assert np.allclose(m, h, rtol=1e-9, atol=1e-9)


# ================================================================ oracle 8
def test_oracle8_partitions_hiring_loop_and_delay_stage_loop_separate():
    """Oracle item 8: sd_workforce_delay1.json's hiring loop (goal-seeking,
    the only loop on the Workforce stock) and its DELAY1's own internal
    self-drain loop (the only loop on the hidden reporting stage) are each
    alone on their stock, so the isolated-loop invariant makes both relative
    -1 at every step -- but they sit in DIFFERENT partitions: the reporting
    delay reads the Workforce but nothing feeds back from it (the DELAY1
    output is a dashboard reading, not an input to hiring), so the two
    stocks are not mutually reachable and share no SCC."""
    layout = json.loads((MODELS / "sd_workforce_delay1.json").read_text())
    result = _analyse(layout)
    dom, loops_df = result["loop_dominance"], result["loops"]
    assert len(loops_df) == 2
    assert set(loops_df["Kind"]) == {"B"}
    assert len(set(loops_df["Partition"])) == 2

    for loop_id in loops_df["Loop"]:
        rel = dom[dom["Loop"] == loop_id]["Relative"].to_numpy(dtype=float)
        assert rel.size > 0
        assert np.allclose(rel, -1.0, atol=1e-7)


# ================================================================ oracle 9
def test_oracle9_strongest_path_keeps_the_top_loops_by_total():
    """Oracle item 9: a three-stock arms-race model (every ordered pair
    joined by its own flow, 6 flows -- the same all-pairs construction
    tests/test_sd_loops.py uses for 4 stocks, here for 3) has more
    elementary circuits than a small `limit`; the strongest-path fallback
    (Finding, Appendix I) should still keep every loop the exhaustive run
    ranks in its top three by |Total|. Initial levels are deliberately
    asymmetric (10/15/25): equal levels with this fully symmetric flow
    structure is a fixed point (every stock's net rate is exactly zero by
    symmetry), which would make every loop permanently inactive (kind D,
    Total 0) and the test vacuous."""
    inits = {"A": 10.0, "B": 15.0, "C": 25.0}
    names = list(inits)
    levels = [{"name": n, "initial": inits[n], "min": None} for n in names]
    flows = [{"name": f"f_{i}_{j}", "rate": f'LEVEL("{i}")*0.05', "from": i, "to": j}
             for i in names for j in names if i != j]
    layout = _sd_layout(levels=levels, flows=flows, simulation=_sim(5.0, 1.0, "euler"))

    exhaustive = _analyse(layout, limit=1000)
    assert not exhaustive["truncated"]
    ex_loops = exhaustive["loops"]
    assert len(ex_loops) >= 4          # enough loops to make "top three" meaningful

    truncated = _analyse(layout, limit=2)
    assert truncated["truncated"]
    tr_keys = {_node_key(n) for n in truncated["loops"]["Nodes"]}

    top3 = ex_loops.reindex(ex_loops["Total"].abs().sort_values(ascending=False).index).head(3)
    for _, row in top3.iterrows():
        assert _node_key(row["Nodes"]) in tr_keys
