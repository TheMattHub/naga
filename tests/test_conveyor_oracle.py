"""Independent oracle for milestone C1 (Stella conveyors, PHASE6 depth bucket
C, docs/history/PHASE6-DEPTH-C-PLAN-2026-09-27.md -- wire format and
semantics, binding -- and docs/history/STELLA-CONVEYOR-SEMANTICS-2026-09-27.md
-- the slat-model rules and worked examples A/B/C).

Written from those two documents only, before/independently of C1's own
implementation (core/levels.py, core/sd.py, schema changes, and
tests/test_conveyor.py were not read). Every expected number is a hand
derivation from the plan's wire format and the research note's SS2 algorithm,
cross-checked row by row with conservation (stock[t+dt] = stock[t] +
inflow_vol - leak_vol - outflow_vol, using volume = rate * dt); examples A, B
and C additionally reuse the research note's own worked tables verbatim. The
capacity (case 4) and staggered-zone (case 5) scenarios are not in the note
and are derived here from its formulas, slat-by-slat, in each test's
docstring.

RESOLVED AMBIGUITIES (GUESS):
  - Reading convention: the note's tables show START-of-step stock and the
    flow RATE (volume/DT) held over [t, t+DT) -- confirmed as rates, not
    per-DT volumes, by the milestone bullet list itself ("leak rate
    12.5,25,37.5,50") and by mass-balance (a volume reading would not
    balance). Mapped onto the product's existing SD engine as: advance the DES
    clock to exactly *t* with `step_until(env, t)`, call
    `LevelSystem.sync(t)`, then read `Level.level` (stock) and `Flow.actual`
    (rate) -- the same pair every other SD oracle in this repo reads
    (test_levels.py's `_level`/`_flow` helpers, test_sd_oracles.py's `_run`).
    `step_until` may be called repeatedly with increasing *t* on the same
    env, as tests/test_agent_population_oracle.py already does for entity events. # GUESS
  - `Flow.actual` (not `.desired`) is what a conveyor's ADMITTED (capacity/
    in_limit-clamped) inflow reports, reusing the same pre-existing
    saturation convention an ordinary bounded level's inflow already uses
    (`core/levels.py`'s existing `.actual` vs `.desired` split; PHASE6-
    DEPTH-C-PLAN.md: "the flow reports the admitted rate, so refused
    material stays upstream"). # GUESS
  - Primary-outflow and leak-flow `"rate"` is the literal empty string `""`
    (the wire format's own example), and a leak's `"zone"` omitted means
    the documented default `[0, 1]` (whole belt). # not a guess -- both
    given directly by the plan doc's wire format / the research note's
    `leak_start`/`leak_end` defaults.
  - `leak_kind` lives on the LEVEL's `"conveyor"` dict (applies to every
    leak flow off that conveyor), matching the research note's
    `exponential_leak` being conveyor-wide, not per-flow. # not a guess,
    directly stated ("for *all* leak flows on this conveyor").

Cases 1-3 are the note's examples A, B, C verbatim; 4 is capacity back-
pressure (hand-derived, not in the note); 5 is the staggered two-zone case
(hand-derived from the note's "remaining, not entering" rule); 6 is the
V0=200 no-leak drain; 7 is validation (E_CONVEYOR_METHOD/_GRID/_OUTFLOW/
_LEAK, W_CONVEYOR_TRANSIT).
"""
import pytest

from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

from conftest import layout as make_layout, problem_codes


# --------------------------------------------------------------- run helpers
def _sim(time_limit, dt, method="euler"):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1,
            "sd": {"dt": dt, "method": method}}


def _sd_layout(**sections):
    """A blockless model: pure SD (levels + flows only), no CREATE/DISPOSE
    (same convention as test_sd_oracles.py's `_sd_layout`)."""
    return make_layout([], [], **sections)


def _prepare(layout_dict):
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout_dict)
    if env.ctx.level_system is not None:
        env.ctx.level_system.start(layout_dict["simulation"]["time_limit"])
    return env


def _sample(env, t):
    """Advance to grid point *t* and sync, so `.level`/`.actual` are the
    values right after that grid step's recompute (see the GUESS above)."""
    # Coordinator reconciliation (harness only): step_until reads 0 as "no
    # limit", and an SD grid wakes forever, so t=0 advances by a hair instead.
    step_until(env, t or 1e-12)
    env.ctx.level_system.sync(t)


def _codes(layout_dict):
    return problem_codes(layout_dict, severity=None)


# ======================================================= 1: example A, no leak
def test_example_a_no_leak_pure_transit_delay():
    """Research note SS4 example A: T=2, DT=0.5 (N=4), inflow 100/time from
    an unlimited source (a flow with no `from`), V0=0. No leak -> a pure
    transit delay: nothing exits before t=T, then outflow jumps straight to
    the steady inflow rate."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "in", "rate": "100", "to": "Belt"},
               {"name": "out", "rate": "", "from": "Belt"}],
        simulation=_sim(3.0, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0), (0.5, 50.0, 0.0), (1.0, 100.0, 0.0),
            (1.5, 150.0, 0.0), (2.0, 200.0, 100.0), (2.5, 200.0, 100.0)]
    for t, stock, outflow in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(stock), t
        assert system.flow_index["out"].actual == pytest.approx(outflow), t


# ================================================== 2: example B, linear leak
def test_example_b_linear_leak_full_zone():
    """Example B: same grid, one linear leak flow, fraction 0.5, default
    zone [0,1] (the whole belt). Numbers from the note's table; cross-
    checked by mass balance (stock[t+dt] = stock[t] + 50 - leak_rate*0.5 -
    outflow_rate*0.5), e.g. 93.75 = 50 + 50 - 12.5*0.5 - 0."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "in", "rate": "100", "to": "Belt"},
               {"name": "out", "rate": "", "from": "Belt"},
               {"name": "leak", "rate": "", "from": "Belt", "leak": {"fraction": "0.5"}}],
        simulation=_sim(3.0, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0, 0.0), (0.5, 50.0, 0.0, 12.5), (1.0, 93.75, 0.0, 25.0),
            (1.5, 131.25, 0.0, 37.5), (2.0, 162.5, 50.0, 50.0), (2.5, 162.5, 50.0, 50.0)]
    for t, stock, outflow, leak in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(stock), t
        assert system.flow_index["out"].actual == pytest.approx(outflow), t
        assert system.flow_index["leak"].actual == pytest.approx(leak), t


# ============================================= 3: example C, exponential leak
def test_example_c_exponential_leak():
    """Example C: T=1, DT=0.5 (N=2), exponential leak f=0.2/time. Steady
    outflow = inflow*(1 - f*dt)^N = 100*0.9^2 = 81, leak = 19; both reached
    exactly at t=T since N=2 and the belt starts empty."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0,
                 "conveyor": {"transit_time": "1", "leak_kind": "exponential"}}],
        flows=[{"name": "in", "rate": "100", "to": "Belt"},
               {"name": "out", "rate": "", "from": "Belt"},
               {"name": "leak", "rate": "", "from": "Belt", "leak": {"fraction": "0.2"}}],
        simulation=_sim(2.0, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0, 0.0), (0.5, 50.0, 0.0, 10.0), (1.0, 95.0, 81.0, 19.0),
            (1.5, 95.0, 81.0, 19.0)]
    for t, stock, outflow, leak in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(stock), t
        assert system.flow_index["out"].actual == pytest.approx(outflow), t
        assert system.flow_index["leak"].actual == pytest.approx(leak), t


# ======================================================= 4: capacity back-pressure
def test_capacity_back_pressure_throttles_admission_and_backs_up_upstream():
    """Not in the research note -- hand-derived from the plan's own formula
    (PHASE6-DEPTH-C-PLAN.md: "admitted inflow per DT = min(requested*DT,
    (capacity - contents) + outflow volume)", `contents` read at the START
    of the step, same convention as every other row here).

    Belt: transit_time=2, DT=0.5 (N=4), capacity=100, no leak. Source: a big
    upstream stock (10000) feeding Belt through flow "feed" wanting a
    constant 200/time (100/DT) -- deliberately equal to the whole capacity,
    so Belt fills in one DT then admits NOTHING until its first batch
    completes a full transit (4 DTs) and exits, freeing exactly one more
    DT's worth of room. Traced slat-by-slat (ages 0..3, newest first):

      step1 [0,.5):  admit=min(100,(100-0)+0)=100    -> slats [100,0,0,0]
      step2 [.5,1):  admit=min(100,(100-100)+0)=0     -> slats [0,100,0,0]
      step3 [1,1.5): admit=min(100,(100-100)+0)=0     -> slats [0,0,100,0]
      step4 [1.5,2): admit=min(100,(100-100)+0)=0     -> slats [0,0,0,100]
      step5 [2,2.5): exit=slat3=100 (rate 200);
                     admit=min(100,(100-100)+100)=100 -> slats [100,0,0,0]
                     (period N: back to step1's shape)

    "feed"'s reported rate is the ADMITTED volume/DT (0 or 200/time) -- the
    other 100/DT Source wanted to send but could not just stays in Source
    (GUESS above: capacity clamps the upstream flow's `.actual`, not just
    Belt's own inflow bookkeeping)."""
    layout = _sd_layout(
        levels=[{"name": "Source", "initial": 10000.0},
                {"name": "Belt", "initial": 0.0,
                 "conveyor": {"transit_time": "2", "capacity": "100"}}],
        flows=[{"name": "feed", "rate": "200", "from": "Source", "to": "Belt"},
               {"name": "out", "rate": "", "from": "Belt"}],
        simulation=_sim(3.0, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [
        # t,   Belt,  feed(admitted rate), out(exit rate), Source
        (0.0, 0.0, 200.0, 0.0, 10000.0),
        (0.5, 100.0, 0.0, 0.0, 9900.0),
        (1.0, 100.0, 0.0, 0.0, 9900.0),
        (1.5, 100.0, 0.0, 0.0, 9900.0),
        (2.0, 100.0, 200.0, 200.0, 9900.0),
        (2.5, 100.0, 0.0, 0.0, 9800.0),
    ]
    for t, belt, feed, out, source in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(belt), t
        assert system.flow_index["feed"].actual == pytest.approx(feed), t
        assert system.flow_index["out"].actual == pytest.approx(out), t
        assert system.levels["Source"].level == pytest.approx(source), t


# ========================================================= 5: staggered zones
def test_staggered_zones_seventyfive_percent_leaks():
    """Two linear leaks, f=0.5 each, on zones [0,0.5] (entry side) and
    [0.5,1] (exit side), N=4. Stella's default applies each f to what
    SURVIVED the earlier zone (STELLA-CONVEYOR-SEMANTICS.md SS2 "staggered
    zones"): zone1 removes 50% of each 50-unit cohort entering (12.5
    vol/DT over its 2 slats/ages), leaving 25; zone2 removes 50% of that
    surviving 25 (6.25 vol/DT over its 2 slats), leaving 12.5 to exit --
    75% leaked, 25% (12.5 of 50) exits, matching the milestone spec.

    Per-cohort-age budget (age 0 = just entered): ages 0-1 (zone1) leak
    12.5 vol/DT each; ages 2-3 (zone2) leak 6.25 vol/DT each; age 3 exits
    afterwards with the remaining 12.5. Rows below sum these budgets over
    however many cohorts are at the relevant ages, and are cross-checked
    by conservation (stock[t+dt] = stock[t] + 50 - (leak1+leak2+out)*0.5),
    e.g. 131.25 = 112.5 + 50 - (50+12.5+0)*0.5."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "in", "rate": "100", "to": "Belt"},
               {"name": "out", "rate": "", "from": "Belt"},
               {"name": "leak1", "rate": "", "from": "Belt",
                "leak": {"fraction": "0.5", "zone": [0.0, 0.5]}},
               {"name": "leak2", "rate": "", "from": "Belt",
                "leak": {"fraction": "0.5", "zone": [0.5, 1.0]}}],
        simulation=_sim(3.0, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [
        # t,    stock,   leak1, leak2, out
        (0.0, 0.0, 0.0, 0.0, 0.0),
        (0.5, 50.0, 25.0, 0.0, 0.0),
        (1.0, 87.5, 50.0, 0.0, 0.0),
        (1.5, 112.5, 50.0, 12.5, 0.0),
        (2.0, 131.25, 50.0, 25.0, 25.0),
        (2.5, 131.25, 50.0, 25.0, 25.0),
    ]
    for t, stock, leak1, leak2, out in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(stock), t
        assert system.flow_index["leak1"].actual == pytest.approx(leak1), t
        assert system.flow_index["leak2"].actual == pytest.approx(leak2), t
        assert system.flow_index["out"].actual == pytest.approx(out), t


# ========================================== 6: initial value, no leak, drains
def test_initial_value_spreads_evenly_with_no_leak_and_drains_out():
    """V=200, no leak, T=2, DT=0.5 (N=4): the initial scalar spreads evenly
    (V/N=50 per slat -- the no-leak steady-state distribution rule,
    STELLA-CONVEYOR-SEMANTICS.md SS2 "initial distribution"). Every slat
    already holds material, so -- unlike examples A-C, which start empty
    and are silent for a full T -- outflow starts immediately at V/N per
    DT = 50/0.5 = 100/time, and the belt drains to exactly 0 at t=T (no
    inflow)."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 200.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "out", "rate": "", "from": "Belt"}],
        simulation=_sim(2.5, 0.5))
    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 200.0, 100.0), (0.5, 150.0, 100.0), (1.0, 100.0, 100.0),
            (1.5, 50.0, 100.0), (2.0, 0.0, 0.0)]
    for t, stock, outflow in rows:
        _sample(env, t)
        assert system.levels["Belt"].level == pytest.approx(stock), t
        assert system.flow_index["out"].actual == pytest.approx(outflow), t


# ==================================================================== 7: validation
def test_rk4_with_a_conveyor_is_e_conveyor_method():
    """"the slat model is per-DT; RK stages have no meaning" -- rk4 with a
    conveyor is rejected outright, whatever else the model does."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "out", "rate": "", "from": "Belt"}],
        simulation=_sim(2.0, 0.5, method="rk4"))
    assert "E_CONVEYOR_METHOD" in _codes(layout)


def test_no_sd_grid_with_a_conveyor_is_e_conveyor_grid():
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "out", "rate": "", "from": "Belt"}],
        simulation={"time_limit": 2.0, "warmup": 0, "replications": 1})  # no "sd" key
    assert "E_CONVEYOR_GRID" in _codes(layout)


def test_two_primary_outflows_is_e_conveyor_outflow():
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "out1", "rate": "", "from": "Belt"},
               {"name": "out2", "rate": "", "from": "Belt"}],
        simulation=_sim(2.0, 0.5))
    assert "E_CONVEYOR_OUTFLOW" in _codes(layout)


def test_no_primary_outflow_is_e_conveyor_outflow():
    """Only a leak flow, no plain outflow: still exactly zero primaries."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "2"}}],
        flows=[{"name": "leak", "rate": "", "from": "Belt", "leak": {"fraction": "0.1"}}],
        simulation=_sim(2.0, 0.5))
    assert "E_CONVEYOR_OUTFLOW" in _codes(layout)


def test_leak_on_a_non_conveyor_flow_is_e_conveyor_leak():
    """A `leak` marker on a flow leaving a plain (non-conveyor) level."""
    layout = _sd_layout(
        levels=[{"name": "Plain", "initial": 100.0}],
        flows=[{"name": "drip", "rate": "", "from": "Plain", "leak": {"fraction": "0.1"}}],
        simulation=_sim(2.0, 0.5))
    assert "E_CONVEYOR_LEAK" in _codes(layout)


def test_non_multiple_transit_time_is_w_conveyor_transit():
    """T=1.3 is not a multiple of DT=0.5 (N = round(1.3/0.5) = round(2.6) =
    3, effective transit 1.5 != 1.3): a warning, not an error, and no
    E_CONVEYOR_* error should also fire on this otherwise-valid model."""
    layout = _sd_layout(
        levels=[{"name": "Belt", "initial": 0.0, "conveyor": {"transit_time": "1.3"}}],
        flows=[{"name": "out", "rate": "", "from": "Belt"}],
        simulation=_sim(3.0, 0.5))
    problems = validate_model_structured(layout)
    codes = {p.code for p in problems}
    assert "W_CONVEYOR_TRANSIT" in codes
    assert not any(p.severity == "error" and p.code.startswith("E_CONVEYOR") for p in problems)
