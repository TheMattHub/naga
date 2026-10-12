"""Independent oracle for milestone G1 (dashboard widgets):

  - Top-level `"dashboard": {"widgets": [...]}` section; four widget kinds
    (kpi, gauge, line, bar), each carrying an expression (kpi/gauge: `expr`)
    or a 1-4/1-8-entry `series` list of {label, expr} (line/bar).
  - Widgets are sampled at EXACTLY the instants the existing `timeseries`
    sampler already uses (ROADMAP 1.7, `core/timeseries.py`); results gain a
    "dashboard" frame with columns Run, Time, Widget, Series, Value, Warmup
    (Series = "" for kpi/gauge, the series label for line/bar).
  - No dashboard section -> empty (but correctly shaped) frame; sampling
    must never change any other result (same precedent as the timeseries
    sampler itself, which draws no random numbers).
  - Validation error codes: E_DASHBOARD_NAME, E_DASHBOARD_KIND,
    E_DASHBOARD_EXPR, E_DASHBOARD_SERIES, E_DASHBOARD_GAUGE.

Written from the task's SPEC only, before G1 landed. `tests/test_dashboard.py`
(the implementer's own tests) was not read, and neither was any G1 diff.

HAND DERIVATIONS
=================
Case 1 (D/D/1 queue, exact): CREATE interarrival Constant=2 (start_time=0,
max_arrivals=3 -> arrivals at t=0,2,4, per CREATE's own documented timing,
same as test_agent_population_oracle.py's convention), PROCESS "Seize Delay Release"
against a capacity-1 "Server" with Constant service=3. By hand:

  [0,2):  A0 in service (seized at 0, done at 3).           NQ=0 NR=1
  [2,3):  A1 arrived at 2, waits (Server busy till 3).      NQ=1 NR=1
  [3,4):  A0 leaves at 3; A1 seizes (done at 6).             NQ=0 NR=1
  [4,6):  A2 arrived at 4, waits (Server busy till 6).      NQ=1 NR=1
  [6,9):  A1 leaves at 6; A2 seizes (done at 9).             NQ=0 NR=1
  [9,.):  A2 leaves at 9; nothing left (max_arrivals=3) -- model drains.
          NQ=0 NR=0, and no arrivals/departures ever land at the exact
          same instant, so no sample time is genuinely ambiguous.

  Widgets: kpi "q" = NQ("Server"); kpi "clock" = TNOW; line "l" =
  [NQ("Server"), NR("Server")]. Expected values are computed from the times
  actually reported in the run's own "timeseries" frame (whatever grid
  `timeseries_points` produces) via the breakpoint table above -- not by
  guessing how many samples the sampler takes, only how to score a given
  sample time, which is exact and independent of that detail.

Case 2 (SD stock, exact): `levels=[{"name": "Stock", "initial": 5}]`,
`flows=[{"name": "in", "to": "Stock", "rate": "2"}]` (test_levels.py's own
`levels=`/`flows=` shape). No spillage/bound, so the closed form is exact for
the whole run: LEVEL("Stock") at time t = 5 + 2*t. Same "score whatever times
were actually sampled" approach as case 1 -- LEVEL()'s value is a pure
function of t regardless of the sampling grid.

Case 3: same queue model (case 1's blocks/resources) run with and without a
`dashboard` section; "system" and "resource_summary" must be byte-identical
(pandas `.equals`), the same invariance precedent the timeseries sampler
itself already has to meet.

Case 4 (validation): one minimal valid layout (CREATE -> DISPOSE, no
resources needed since the exprs below are all TNOW) plus one dashboard
section per error code; asserts an error dashboard raises exactly that code's
presence and a valid dashboard raises none of the E_DASHBOARD_* codes.

RESOLVED AMBIGUITIES (GUESS)
  - The empty "dashboard" frame (no dashboard declared) has exactly the six
    columns the spec's wire format implies: Run, Time, Widget, Series, Value,
    Warmup, in that order -- same convention `_TIMESERIES_COLS` already uses
    for the sibling "timeseries" frame. # GUESS
  - E_DASHBOARD_SERIES is triggered here only via an out-of-bounds series
    count (5 entries on a `line`, max 4); the duplicate-label and
    wrong-kind-shape triggers the spec also assigns to this code are not
    separately exercised (one layout per code, per the task brief). # GUESS
  - An expression that does not compile (`"NQ("`, confirmed by hand against
    `nagasim.core.expressions.compile_expression` to raise `ExpressionError`
    with "was never closed") is enough to trigger E_DASHBOARD_EXPR; the
    "unknown name" half of that code is not separately exercised. # GUESS

OBSERVED (not read from source, only from running this file against the repo
as it stood while writing it -- the other agent's concurrent landing, not
consulted directly): E_DASHBOARD_NAME/KIND/SERIES/GAUGE already fire as
specified, but a non-compiling expr currently raises the codebase's existing
generic "E_EXPRESSION" (the same code RECORD/ranking_expr/etc. already use
for their own expr fields) rather than a dedicated "E_DASHBOARD_EXPR". Left
asserting the SPEC's literal code name, since the brief lists it on equal
footing with the other four -- flagged here for whoever reconciles spec vs.
implementation, not silently adjusted to match what already runs.
"""
import math

import pytest

from nagasim.core.sim_runner import run_single_simulation

from conftest import chain, create, dispose, layout as make_layout, problem_codes, process


# ------------------------------------------------------------- case 1: queue
def _queue_layout(dashboard=None, *, timeseries_points=21):
    """CREATE(Constant 2, max_arrivals=3) -> PROCESS(Seize Delay Release,
    Server, Constant 3) -> DISPOSE, against a capacity-1 "Server"."""
    c = create("c", distribution="Constant", dist_params={"value": 2.0}, max_arrivals=3)
    p = process("p", resource="Server", value=3.0, distribution="Constant")
    d = dispose("d")
    sections = dict(
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        simulation={"time_limit": None, "warmup": 0, "replications": 1,
                    "timeseries_points": timeseries_points},
    )
    if dashboard is not None:
        sections["dashboard"] = dashboard
    return make_layout([c, p, d], chain(c, p, d), **sections)


# Breakpoints (lo inclusive, hi exclusive) -> (NQ, NR), hand-derived above.
_QUEUE_BREAKPOINTS = [
    (0, 2, 0, 1),
    (2, 3, 1, 1),
    (3, 4, 0, 1),
    (4, 6, 1, 1),
    (6, 9, 0, 1),
    (9, math.inf, 0, 0),
]


def _expected_nq_nr(t):
    for lo, hi, nq, nr in _QUEUE_BREAKPOINTS:
        if lo <= t < hi:
            return nq, nr
    raise AssertionError(f"time {t} falls outside the hand-derived horizon")  # pragma: no cover


_QUEUE_DASHBOARD = {"widgets": [
    {"name": "q", "kind": "kpi", "expr": "NQ(\"Server\")"},
    {"name": "clock", "kind": "kpi", "expr": "TNOW"},
    {"name": "l", "kind": "line", "series": [
        {"label": "queue", "expr": "NQ(\"Server\")"},
        {"label": "inservice", "expr": "NR(\"Server\")"},
    ]},
]}


def test_queue_dashboard_matches_hand_derived_nq_nr_and_tnow():
    layout = _queue_layout(_QUEUE_DASHBOARD)
    assert not problem_codes(layout)
    results = run_single_simulation(layout, seed=1, run_len=20, warmup=0)

    ts = results["timeseries"]
    dash = results["dashboard"]
    assert not dash.empty

    # Same sampling grid as the existing timeseries sampler (ROADMAP 1.7).
    assert sorted(dash["Time"].unique().tolist()) == sorted(ts["Time"].unique().tolist())

    for t in sorted(dash["Time"].unique().tolist()):
        exp_nq, exp_nr = _expected_nq_nr(t)

        q_row = dash[(dash["Widget"] == "q") & (dash["Time"] == t)]
        assert q_row["Series"].tolist() == [""]
        assert q_row["Value"].iloc[0] == pytest.approx(exp_nq)

        clock_row = dash[(dash["Widget"] == "clock") & (dash["Time"] == t)]
        assert clock_row["Value"].iloc[0] == pytest.approx(t)

        l_queue = dash[(dash["Widget"] == "l") & (dash["Time"] == t) & (dash["Series"] == "queue")]
        assert l_queue["Value"].iloc[0] == pytest.approx(exp_nq)
        l_in = dash[(dash["Widget"] == "l") & (dash["Time"] == t) & (dash["Series"] == "inservice")]
        assert l_in["Value"].iloc[0] == pytest.approx(exp_nr)

    # Every sample time is Warmup=False (warmup=0): the dashboard's own
    # Warmup flag must agree with the sibling timeseries frame's.
    assert bool(dash["Warmup"].any()) is False


# --------------------------------------------------------------- case 2: SD
def _sd_layout(dashboard=None, *, timeseries_points=10):
    c, d = create("c", max_arrivals=1), dispose("d")
    sections = dict(
        levels=[{"name": "Stock", "initial": 5}],
        flows=[{"name": "in", "to": "Stock", "rate": "2"}],
        simulation={"time_limit": None, "warmup": 0, "replications": 1,
                    "timeseries_points": timeseries_points},
    )
    if dashboard is not None:
        sections["dashboard"] = dashboard
    return make_layout([c, d], chain(c, d), **sections)


_SD_DASHBOARD = {"widgets": [{"name": "stock", "kind": "kpi", "expr": "LEVEL(\"Stock\")"}]}


def test_sd_stock_dashboard_matches_closed_form():
    """LEVEL("Stock") = initial + rate * t = 5 + 2*t exactly, whatever the
    sampling grid actually produces (no bound, no spillage: the level never
    deviates from the closed form for the run's whole length)."""
    layout = _sd_layout(_SD_DASHBOARD)
    assert not problem_codes(layout)
    results = run_single_simulation(layout, seed=0, run_len=10, warmup=0)

    dash = results["dashboard"]
    assert not dash.empty
    assert sorted(dash["Time"].unique().tolist()) == sorted(results["timeseries"]["Time"].unique().tolist())

    stock = dash[dash["Widget"] == "stock"]
    for t, value in zip(stock["Time"], stock["Value"]):
        assert value == pytest.approx(5.0 + 2.0 * t)


# --------------------------------------------------------- case 3: invariance
def test_dashboard_section_never_changes_other_results():
    with_dash = run_single_simulation(_queue_layout(_QUEUE_DASHBOARD), seed=1, run_len=20, warmup=0)
    without_dash = run_single_simulation(_queue_layout(None), seed=1, run_len=20, warmup=0)

    assert with_dash["system"].equals(without_dash["system"])
    assert with_dash["resource_summary"].equals(without_dash["resource_summary"])
    # No dashboard section -> empty (but present, correctly-shaped) frame.
    assert without_dash["dashboard"].empty
    assert list(without_dash["dashboard"].columns) == \
        ["Run", "Time", "Widget", "Series", "Value", "Warmup"]  # GUESS


# ------------------------------------------------------------ case 4: validation
def _codes(layout):
    return problem_codes(layout)


def _minimal_layout(dashboard):
    c, d = create("c", max_arrivals=1), dispose("d")
    return make_layout([c, d], chain(c, d), dashboard=dashboard)


_VALID_DASHBOARD = {"widgets": [
    {"name": "a", "kind": "kpi", "expr": "TNOW"},
    {"name": "b", "kind": "gauge", "expr": "TNOW", "max": 100},
    {"name": "c_widget", "kind": "line", "series": [{"label": "x", "expr": "TNOW"}]},
    {"name": "d_widget", "kind": "bar", "series": [{"label": "y", "expr": "TNOW"}]},
]}


def test_valid_dashboard_raises_no_dashboard_error():
    assert not ({c for c in _codes(_minimal_layout(_VALID_DASHBOARD)) if c.startswith("E_DASHBOARD")})


def test_duplicate_widget_name_is_e_dashboard_name():
    dashboard = {"widgets": [
        {"name": "a", "kind": "kpi", "expr": "TNOW"},
        {"name": "a", "kind": "kpi", "expr": "TNOW"},
    ]}
    assert "E_DASHBOARD_NAME" in _codes(_minimal_layout(dashboard))


def test_unknown_kind_is_e_dashboard_kind():
    dashboard = {"widgets": [{"name": "a", "kind": "pie", "expr": "TNOW"}]}
    assert "E_DASHBOARD_KIND" in _codes(_minimal_layout(dashboard))


def test_expression_that_does_not_compile_is_e_dashboard_expr():
    dashboard = {"widgets": [{"name": "a", "kind": "kpi", "expr": "NQ("}]}
    assert "E_DASHBOARD_EXPR" in _codes(_minimal_layout(dashboard))


def test_out_of_bounds_series_count_is_e_dashboard_series():
    dashboard = {"widgets": [{"name": "a", "kind": "line", "series": [
        {"label": f"s{i}", "expr": "TNOW"} for i in range(5)   # max is 4
    ]}]}
    assert "E_DASHBOARD_SERIES" in _codes(_minimal_layout(dashboard))


def test_gauge_missing_max_is_e_dashboard_gauge():
    dashboard = {"widgets": [{"name": "a", "kind": "gauge", "expr": "TNOW"}]}   # no "max"
    assert "E_DASHBOARD_GAUGE" in _codes(_minimal_layout(dashboard))
