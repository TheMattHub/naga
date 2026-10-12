"""PHASE6 S2 (docs/PHASE6_PLAN.md, "SD completeness"): the SD function pack --
INITIAL, ACTIVE INITIAL, SAMPLE IF TRUE, TREND, FORECAST, NPV, ZIDZ/XIDZ,
MODULO, TABLE_EXTRAPOLATE, PROD/VMIN/VMAX and NOISE_NORMAL.

Written from Vensim's own semantics (see `core/sd.py`'s `_value_macro`
docstrings), cross-checked by a hand derivation or a small reference loop for
each function -- the same convention `test_sd_constructs_oracle.py` uses for SMOOTH/
DELAY. Cheap: every model here is a handful of levels/flows/auxiliaries.
"""
import pytest

from nagasim.api import run_model
from nagasim.core import sd
from nagasim.core.expressions import ExpressionScope, compile_expression

from conftest import problem_codes, run_env

_DUMMY_LEVEL = {"name": "_dummy", "initial": 0.0}
_DUMMY_FLOW = {"name": "_noop", "from": "_dummy", "rate": "0"}


def _codes(layout):
    return problem_codes(layout, severity=None)


def _run(layout, run_len, seed=0):
    """Prepare and run one replication in-process, for exact access to
    hidden levels/delays."""
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def _aux_series(layout, name):
    """The full (Time, Value) history of `aux/<name>` from `run_model`'s fixed-
    grid timeseries sampler -- independent of any hidden-record naming."""
    ts = run_model(layout)["timeseries"]
    rows = ts[ts["Series"] == f"aux/{name}"].sort_values("Time")
    return list(rows["Time"]), list(rows["Value"])


def _aux_finals(layout):
    """{aux name: final sampled value} from ONE `run_model`."""
    ts = run_model(layout)["timeseries"].sort_values("Time")
    aux = ts[ts["Series"].str.startswith("aux/")]
    return {series[4:]: rows["Value"].iloc[-1] for series, rows in aux.groupby("Series")}


def _sd_layout(auxiliaries, time_limit=5.0, dt=0.25, method="euler", **extra):
    layout = {"levels": [_DUMMY_LEVEL], "flows": [_DUMMY_FLOW], "auxiliaries": auxiliaries,
              "simulation": {"time_limit": time_limit, "sd": {"dt": dt, "method": method}}}
    layout.update(extra)
    return layout


# ============================================================ ZIDZ / XIDZ / MODULO
def test_zidz_and_xidz_fall_back_when_dividing_by_zero_else_the_quotient():
    layout = _sd_layout([{"name": "za", "expr": "ZIDZ(10, 0)"},
                         {"name": "zb", "expr": "ZIDZ(10, 4)"},
                         {"name": "xa", "expr": "XIDZ(10, 0, -1)"},
                         {"name": "xb", "expr": "XIDZ(10, 4, -1)"},
                         {"name": "xc", "expr": "XIDZ(10, 0)"}])   # x defaults to 0 (ZIDZ)
    assert _codes(layout) == set()
    assert _aux_finals(layout) == pytest.approx(
        {"za": 0.0, "zb": 2.5, "xa": -1.0, "xb": 2.5, "xc": 0.0})


# MODULO's sign rule is test_regressions.py::test_168_... and the sdx `rounding` model.


# ============================================================ TABLE_EXTRAPOLATE
def test_table_extrapolate_continues_the_slope_table_clamps():
    layout = _sd_layout(
        [{"name": "clamped", "expr": 'TABLE("t", 15)'},
         {"name": "extrapolated", "expr": 'TABLE_EXTRAPOLATE("t", 15)'},
         {"name": "below", "expr": 'TABLE_EXTRAPOLATE("t", -5)'}],
        tables=[{"name": "t", "keys": [0, 10], "values": [0, 10], "key_mode": "linear"}])
    assert _codes(layout) == set()
    finals = _aux_finals(layout)
    assert finals["clamped"] == pytest.approx(10.0)       # D50: TABLE clamps
    assert finals["extrapolated"] == pytest.approx(15.0)  # slope 1, continued
    assert finals["below"] == pytest.approx(-5.0)


# ============================================================ PROD / VMIN / VMAX
def test_prod_vmin_vmax_reduce_a_subscripted_level():
    """P[a]=1, P[b]=2, P[c]=4 at t=1 (constant inflows): PROD=8 (not the
    pairwise SUM's 7 -- the #124 star-binding rule reused with `*`), VMIN=1,
    VMAX=4, distinguishable from each other and from SUM=7/MEAN=7/3."""
    layout = {
        "subscripts": [{"name": "region", "elements": ["a", "b", "c"]}],
        "levels": [{"name": "P", "initial": "0", "dims": ["region"]}],
        "flows": [{"name": "fa", "to": "P[a]", "rate": "1"},
                  {"name": "fb", "to": "P[b]", "rate": "2"},
                  {"name": "fc", "to": "P[c]", "rate": "4"}],
        "auxiliaries": [
            {"name": "sprod", "expr": 'PROD(LEVEL("P[*]"))'},
            {"name": "smin", "expr": 'VMIN(LEVEL("P[*]"))'},
            {"name": "smax", "expr": 'VMAX(LEVEL("P[*]"))'},
            {"name": "ssum", "expr": 'SUM(LEVEL("P[*]"))'},
        ],
        "simulation": {"time_limit": 1.0}}
    assert _codes(layout) == set()
    assert _aux_finals(layout) == pytest.approx({"sprod": 8.0, "smin": 1.0, "smax": 4.0,
                                                 "ssum": 7.0})


def test_prod_vmin_vmax_one_argument_form_left_unexpanded_is_e_subscript_aggregate():
    """No "[*]" to unroll (PHASE6 S2 mirrors SUM/MEAN's own E_SUBSCRIPT_AGGREGATE)."""
    layout = _sd_layout([{"name": "bad", "expr": "PROD(1 + 2)"}])
    assert _codes(layout) == {"E_SUBSCRIPT_AGGREGATE"}


# ============================================================ INITIAL
# The held t0 value is test_regressions.py::test_161_...; ACTIVE_INITIAL is
# test_160_..., SAMPLE_IF_TRUE test_158_..., NPV test_159_...
def test_initial_outside_a_level_flow_aux_or_delay_field_is_e_sd_function_context():
    layout = {"blocks": [{"name": "a", "type": "ASSIGN", "position": [0, 0],
                          "params": {"assignments": [{"target": "x", "expr": "INITIAL(1)"}]}}],
              "links": [], "variables": [{"name": "x", "initial": 0.0}]}
    assert "E_SD_FUNCTION_CONTEXT" in _codes(layout)


# ============================================================ TREND / FORECAST
def test_trend_of_an_exponential_input_converges_to_the_growth_rate():
    """dL/dt = g*L: Vensim's own closed form for TREND at steady state is
    EXACTLY g (SMOOTH's lag term cancels algebraically), not merely close."""
    g = 0.2
    layout = {"levels": [{"name": "L", "initial": 1.0}],
              "flows": [{"name": "grow", "to": "L", "rate": f'{g}*LEVEL("L")'}],
              "auxiliaries": [{"name": "trend", "expr": 'TREND(LEVEL("L"), 2, 0)'}],
              "simulation": {"time_limit": 40.0, "sd": {"dt": 0.05}}}
    assert _codes(layout) == set()
    assert _aux_finals(layout)["trend"] == pytest.approx(g, rel=2e-3)


def test_forecast_matches_x_times_one_plus_trend_times_horizon():
    """FORECAST(x, t, h) = x*(1 + TREND(x, t)*h) exactly (Vensim's own
    definition) -- checked against the model's OWN trend reading, not a
    separate closed form, since Euler drift on the exponential level would
    make an independent formula only approximately right."""
    g, horizon = 0.2, 5.0
    layout = {"levels": [{"name": "L", "initial": 1.0}],
              "flows": [{"name": "grow", "to": "L", "rate": f'{g}*LEVEL("L")'}],
              "auxiliaries": [
                  {"name": "trend", "expr": 'TREND(LEVEL("L"), 2, 0)'},
                  {"name": "fcst", "expr": f'FORECAST(LEVEL("L"), 2, {horizon})'}],
              "simulation": {"time_limit": 40.0, "sd": {"dt": 0.05}}}
    assert _codes(layout) == set()
    env = _run(layout, 40.0)
    L_final = env.ctx.level_system.levels["L"].level
    scope = ExpressionScope(ctx=env.ctx)
    trend_final = compile_expression('AUX("trend")').evaluate(scope)
    fcst_final = compile_expression('AUX("fcst")').evaluate(scope)
    assert fcst_final == pytest.approx(L_final * (1 + trend_final * horizon), rel=1e-9)


# ============================================================ NOISE_NORMAL
def test_noise_normal_is_bounded_deterministic_and_redraws_each_grid_point():
    layout = {"levels": [_DUMMY_LEVEL],
              "flows": [{"name": "_noop", "from": "_dummy", "rate": "0*TNOW"}],
              "auxiliaries": [{"name": "noise", "expr": "NOISE_NORMAL(0, 1, -2, 2)"}],
              "simulation": {"time_limit": 5.0, "sd": {"dt": 1.0}}}
    assert _codes(layout) == set()
    _, v1 = _aux_series(layout, "noise")
    _, v2 = _aux_series(layout, "noise")
    assert v1 == v2                                       # CRN: same seed, same draws
    assert all(-2.0 <= v <= 2.0 for v in v1)
    at_grid_points = sorted(set(round(v, 9) for v in v1))
    assert len(at_grid_points) > 1                        # it actually redraws


def test_noise_normal_is_refused_under_rk4():
    layout = {"levels": [_DUMMY_LEVEL],
              "flows": [{"name": "f", "from": "_dummy",
                        "rate": '0*LEVEL("_dummy") + 0*NOISE_NORMAL(0, 1, -1, 1)'}],
              "simulation": {"time_limit": 5.0, "sd": {"dt": 1.0, "method": "rk4"}}}
    assert "E_SD_RK4_RANDOM" in _codes(layout)


# ============================================================ macro-name hygiene (#130)
def test_value_macro_and_delay_shorthand_share_one_counter_no_collision():
    """A flow using a delay-shorthand macro and a value macro gets `f#1`/`f#2`
    (not both `f#1`) -- the same shared-counter rule `_next_name` gives every
    inline macro of one owner."""
    layout = {"auxiliaries": [
        {"name": "f", "expr": 'SAMPLE_IF_TRUE(TNOW > 1, TNOW, 0) + SMOOTH(TNOW, 2)'}]}
    out = sd.expand(layout)
    names = [d["name"] for d in out["delays"]]
    assert names == ["f#1", "f#2"]
    assert len(set(names)) == 2


def test_user_delay_colliding_with_a_generated_value_macro_name_is_e_delay_name():
    layout = {"levels": [_DUMMY_LEVEL], "flows": [_DUMMY_FLOW],
              "delays": [{"name": "f#1", "input": "1", "delay_time": "1"}],
              "auxiliaries": [{"name": "f", "expr": "SAMPLE_IF_TRUE(TNOW > 1, TNOW, 0)"}],
              "simulation": {"time_limit": 5.0, "sd": {"dt": 0.5}}}
    assert "E_DELAY_NAME" in _codes(layout)


def test_new_delay_kinds_require_their_own_fields_not_input_delay_time():
    problems = _codes(
        {"levels": [_DUMMY_LEVEL], "flows": [_DUMMY_FLOW],
         "delays": [{"name": "s", "kind": "sample"}, {"name": "n", "kind": "noise"}],
         "simulation": {"time_limit": 1.0}})
    assert problems == {"E_DELAY_SPEC"}


def test_lookup_section_covers_what_sd_and_sd_loops_use():
    """`core/expressions.LOOKUP_SECTION` (wave E3) is the one source
    `core/sd.py`, `core/sd_loops.py` and `ui/sdview.py` build their own
    origin-function subsets from; a stale copy in any of them would silently
    drop an SD-diagram arrow or a dependency-graph edge."""
    from nagasim.core.expressions import LOOKUP_SECTION, lookup_sections
    from nagasim.core import sd_loops

    assert LOOKUP_SECTION["LEVEL"] == "levels"
    assert LOOKUP_SECTION["FLOW"] == "flows"
    assert LOOKUP_SECTION["FRACTION"] == "levels"
    assert LOOKUP_SECTION["AUX"] == "auxiliaries"
    assert LOOKUP_SECTION["DELAY"] == "delays"
    assert sd._LOOKUP_SECTION == lookup_sections("LEVEL", "FRACTION", "FLOW", "AUX", "DELAY")
    assert sd_loops._FUNC_SECTION == lookup_sections("LEVEL", "AUX", "DELAY")


def test_resource_state_functions_are_nq_nr_mr_available_idle():
    """`core/schema/sections_validation.py`'s dashboard check reads this trait
    (wave E3) instead of a hand-written tuple; NS is deliberately excluded
    (it is "store_state", not "resource_state")."""
    from nagasim.core.expressions import functions_with

    assert functions_with("resource_state") == ("NQ", "NR", "MR", "AVAILABLE", "IDLE")
