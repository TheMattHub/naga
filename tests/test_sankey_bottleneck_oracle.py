"""Independent oracle for Phase 6D W3 M2 (Sankey and bottleneck analyser),
docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md. Every expected number here is
derived BY HAND from the spec and from Roser, Nakano, Tanaka's active-period
method -- never by running the implementation (M2 is being built by a
concurrent agent). Failures because a name/field does not exist yet are
expected until that work lands; the coordinator reconciles wire-format
guesses against what M2 actually ships.

Wire format assumed from the plan text plus the universal `"Run"` column
every existing `outputs.py` frame carries (resource_summary, the pre-existing
SD `flow_summary`, ...):
  - `material_flow_summary`: Run, From, To, Entities -- one row per (From, To)
    link with at least one traversal after warm-up. (Named so because
    `flow_summary` is the SD flow-rate frame, PHASE4 D1.)
  - `bottleneck_summary`: Run, Resource, "Sole %", "Shifting %", "Total %".
  - Opt-in collection: model-level `{"outputs": {"bottleneck": true}}`.
  - Pure analyser function: name/module/signature/return shape are all
    guesses (`active_period_shares(intervals_by_resource, t0, t1)`); routed
    through one adapter below so the coordinator only has to edit one spot.
  - `svgcharts.sankey`: every other svgcharts function takes plain lists, not
    a DataFrame (and `core/svgcharts.py` is NOT on CLAUDE.md's pandas-allowed
    module list) -- so despite the plan's shorthand "`svgcharts.sankey(frame)`",
    this oracle calls it with a plain list of (From, To, Entities) tuples,
    the same shape `bar_chart`/`line_chart` use for their own series data.
"""
from __future__ import annotations

import importlib

import pytest

from conftest import chain, create, dispose, layout, process

from nagasim import api

FLOW_FRAME = "material_flow_summary"
BOTTLENECK_FRAME = "bottleneck_summary"
FLOW_COLS = {"Run", "From", "To", "Entities"}
BOTTLENECK_COLS = {"Run", "Resource", "Sole %", "Shifting %", "Total %"}


def _decide(name="decide_1", **params):
    base = {"mode": "Condition", "conditions": ["True"], "probs": []}
    base.update(params)
    return {"name": name, "description": name, "type": "DECIDE", "position": [1, 0], "params": base}


def _flow_row(df, frm, to):
    row = df[(df["From"] == frm) & (df["To"] == to)]
    assert len(row) == 1, f"expected exactly one {frm}->{to} row, got {len(row)}"
    return row.iloc[0]


def _active_period_shares(intervals_by_resource, t0, t1):
    """Adapter for the guessed pure analyser (see module docstring)."""
    candidates = [
        ("nagasim.core.outputs", "active_period_shares"),
        ("nagasim.core.blocking", "active_period_shares"),
        ("nagasim.core.bottleneck", "active_period_shares"),
    ]
    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(mod, fn_name, None)
        if fn is not None:
            return fn(intervals_by_resource, t0, t1)
    pytest.fail(f"none of {candidates} exist yet (M2 not implemented, or under a different name)")


def _as_pct(value):
    """Accept either a 0..1 fraction or an already-scaled 0..100 percentage."""
    return value * 100.0 if value <= 1.0 else value


def _sankey(rows):
    """Adapter for the guessed `svgcharts.sankey` signature (see module
    docstring): a plain list of (From, To, Entities) tuples, not a DataFrame."""
    from nagasim import svgcharts
    return svgcharts.sankey(rows)


# -- case 1: deterministic DECIDE, exact per-link counts, conservation ------

def test_flow_summary_exact_counts_and_conservation():
    """Create(1/tick, 6 arrivals) -> Decide(Condition: entity.id % 2 == 0)
    -> {dispose_a, dispose_b}. Entity ids start at 1 in every run
    (`SimulationContext.entity_ids`, D195) and CREATE hands out ids
    1..6 in arrival order, so the condition alternates deterministically:
    odd ids (1,3,5) take the ELSE branch (dispose_b), even ids (2,4,6) take
    branch 0 (dispose_a) -- 3 and 3 by hand, no chance involved.
    """
    create_blk = create(max_arrivals=6, dist_params={"value": 1.0})
    decide_blk = _decide(conditions=["entity.id % 2 == 0"])
    dispose_a, dispose_b = dispose("dispose_a"), dispose("dispose_b")
    links = chain(create_blk, decide_blk) + [
        {"source": "decide_1", "target": "dispose_a"},
        {"source": "decide_1", "target": "dispose_b"},
    ]
    model = layout([create_blk, decide_blk, dispose_a, dispose_b], links)

    results = api.run_model(model, replications=1)
    flow = results[FLOW_FRAME]
    assert set(flow.columns) == FLOW_COLS

    into_decide = _flow_row(flow, "create_1", "decide_1")["Entities"]
    to_a = _flow_row(flow, "decide_1", "dispose_a")["Entities"]
    to_b = _flow_row(flow, "decide_1", "dispose_b")["Entities"]
    assert into_decide == 6
    assert to_a == 3 and to_b == 3
    assert to_a + to_b == into_decide          # conservation through a transparent block


# -- case 2: warm-up reset, and an empty fixed-column frame -----------------

def test_flow_summary_resets_at_warmup():
    """Create(1/tick, 10 arrivals at t=0..9) -> Dispose, warmup=4.5 (between
    arrivals 4 and 5, so there is no boundary ambiguity). Entities 0..4
    (ids 1-5, created/forwarded at t=0..4) traversed the link before warm-up
    and must not count; entities 5..9 (ids 6-10, t=5..9) traversed after and
    must -- exactly 5, by hand.
    """
    create_blk = create(max_arrivals=10, dist_params={"value": 1.0})
    dispose_blk = dispose()
    model = layout([create_blk, dispose_blk], chain(create_blk, dispose_blk),
                   simulation={"time_limit": 12, "warmup": 4.5, "replications": 1})

    results = api.run_model(model, replications=1)
    row = _flow_row(results[FLOW_FRAME], "create_1", "dispose_1")
    assert row["Entities"] == 5


def test_flow_summary_empty_with_fixed_columns_when_nothing_moves():
    """A CREATE whose first arrival is past the limit (max_arrivals=0 means
    unlimited in this engine; coordinator wire fix): no entity is ever created, so `forward` never
    runs and no link is ever traversed -- material_flow_summary must be an EMPTY frame
    with its fixed columns (the A3/E1 precedent CLAUDE.md's Common rules cite)."""
    create_blk = create(start_time=10**9, max_arrivals=1)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                 "params": {"distribution": "Constant", "dist_params": {"value": 1.0}}}
    dispose_blk = dispose()
    model = layout([create_blk, delay_blk, dispose_blk],
                   chain(create_blk, delay_blk, dispose_blk),
                   simulation={"time_limit": 5, "warmup": 0, "replications": 1})

    results = api.run_model(model, replications=1)
    flow = results[FLOW_FRAME]
    assert set(flow.columns) == FLOW_COLS
    assert len(flow) == 0


# -- case 3: deterministic two-station line, Roser sole/shifting by hand ----

def test_bottleneck_two_station_line_sole_and_shifting():
    """Create(interarrival 2, 6 arrivals at t=0,2,..,10) -> S1 (service 1,
    capacity 1) -> S2 (service 3, capacity 1, unbounded queue) -> Dispose.
    outputs.bottleneck=true. time_limit=19.5 (past the last event, no
    boundary ambiguity; 0.5 of dead tail counts toward neither resource).

    S1 is never backlogged (service 1 < interarrival 2, queue in front of it
    is always empty on arrival): job i occupies S1 during [2i, 2i+1) for
    i=0..5 -- SIX separate 1-unit active periods, each strictly shorter than
    anything S2 produces.

    S2 receives job i at t=2i+1 and is permanently overloaded (service 3 >
    interarrival 2, so its queue only grows): job0 runs [1,4), job1 [4,7),
    job2 [7,10), job3 [10,13), job4 [13,16), job5 [16,19) -- back to back,
    ONE continuous 18-unit active period [1, 19).

    Roser: at every instant, the resource with the longest CURRENT active
    period is the bottleneck. S1's active period [0,1) is the only one active
    before S2 ever starts -> S1 is (trivially) sole bottleneck for [0, 1).
    From t=1 on, S2 is active throughout [1, 19) (length 18), which strictly
    exceeds every S1 sub-period (length 1) it overlaps -- S2 is the bottleneck
    for the whole rest of the run, and since S1's short periods never become
    the longest at any instant there is no current/next-bottleneck overlap:
    S1: Sole = [0,1) = 1. S2: Sole = [1,19) = 18. Shifting = 0 for both
    (genuine overlapping-period shifting is exercised separately in case 4,
    on hand-picked intervals through the pure analyser).
    Denominator = observed window = time_limit - warmup = 19.5.
    """
    create_blk = create(max_arrivals=6, dist_params={"value": 2.0})
    s1 = process(name="station1", resource="S1", value=1.0)
    s2 = process(name="station2", resource="S2", value=3.0)
    dispose_blk = dispose()
    # Coordinator wire fix: a keeper arrival past the limit keeps the queue
    # non-empty, so the observed window is the time limit, not the drain time
    # (DEVLOG #32) -- the denominator derived above.
    keeper = [create(name="keeper_create", start_time=10**9, max_arrivals=1),
              dispose(name="keeper_dispose")]
    model = layout(
        [create_blk, s1, s2, dispose_blk, *keeper],
        chain(create_blk, s1, s2, dispose_blk) + chain(*keeper),
        resources=[{"name": "S1", "capacity": 1}, {"name": "S2", "capacity": 1}],
        outputs={"bottleneck": True},
        simulation={"time_limit": 19.5, "warmup": 0, "replications": 1},
    )

    results = api.run_model(model, replications=1)
    bn = results[BOTTLENECK_FRAME]
    assert set(bn.columns) == BOTTLENECK_COLS
    bn = bn.set_index("Resource")

    horizon = 19.5
    tol = 1.0   # percentage points: hedges an unconfirmed edge/rounding convention
    assert bn.loc["S1", "Sole %"] == pytest.approx(100 * 1 / horizon, abs=tol)
    assert bn.loc["S1", "Shifting %"] == pytest.approx(0.0, abs=tol)
    assert bn.loc["S2", "Sole %"] == pytest.approx(100 * 18 / horizon, abs=tol)
    assert bn.loc["S2", "Shifting %"] == pytest.approx(0.0, abs=tol)
    for res in ("S1", "S2"):
        assert bn.loc[res, "Total %"] == pytest.approx(
            bn.loc[res, "Sole %"] + bn.loc[res, "Shifting %"], abs=1e-6)


# -- case 4: hand-picked overlapping intervals through the pure analyser ----

def test_active_period_shares_alternating_overlap():
    """Two resources, synthetic intervals (no simulation at all):
      A active: [0, 5), [8, 12)      B active: [3, 10)
    By hand, instant by instant over [0, 12):
      [0, 3):  only A active                    -> sole A            (len 3)
      [3, 5):  A (period len 5, ending) and      -> shifting A/B      (len 2)
               B (period len 7, taking over) overlap
      [5, 8):  only B active                     -> sole B            (len 3)
      [8, 10): B (current, period len 7, ending)  -> shifting A/B      (len 2)
               and A's 2nd period (len 4, next) overlap
      [10, 12): only A active (2nd period)        -> sole A            (len 2)
    A: Sole = 3+2=5, Shifting = 2+2=4, Total = 9   -> 41.667% / 33.333% / 75%
    B: Sole = 3,     Shifting = 2+2=4, Total = 7   -> 25%     / 33.333% / 58.333%
    (Shifting time is credited to BOTH resources sharing the overlap, per
    Roser's method -- it is not partitioned between them.)
    Sanity: Sole_A + Sole_B + Shifting(unique wall time 4) = 5+3+4 = 12 = horizon.
    """
    intervals = {"A": [(0.0, 5.0), (8.0, 12.0)], "B": [(3.0, 10.0)]}
    shares = _active_period_shares(intervals, 0.0, 12.0)

    def get(resource, key):
        row = shares[resource]
        # tolerate a dict keyed "sole"/"shifting" or "Sole %"/"Shifting %"
        for candidate in (key, key.lower(), f"{key.capitalize()} %"):
            if candidate in row:
                return _as_pct(row[candidate])
        pytest.fail(f"can't find {key!r} in {row!r} for resource {resource!r}")

    tol = 0.5
    assert get("A", "sole") == pytest.approx(100 * 5 / 12, abs=tol)
    assert get("A", "shifting") == pytest.approx(100 * 4 / 12, abs=tol)
    assert get("B", "sole") == pytest.approx(100 * 3 / 12, abs=tol)
    assert get("B", "shifting") == pytest.approx(100 * 4 / 12, abs=tol)


# -- case 5: bottleneck_summary is empty without the opt-in flag ------------

def test_bottleneck_summary_empty_without_outputs_flag(simple_model_path):
    """`outputs.bottleneck` absent (the default) -> active intervals are
    never collected (spec: "memory grows with the event count"), so
    bottleneck_summary must be an EMPTY frame with its fixed columns even
    though the model has plenty of entity flow."""
    results = api.run_model(simple_model_path, replications=1)
    bn = results[BOTTLENECK_FRAME]
    assert set(bn.columns) == BOTTLENECK_COLS
    assert len(bn) == 0


# -- case 6: svgcharts.sankey draws only the nonzero links ------------------

def test_sankey_svg_is_ascii_with_one_path_per_nonzero_link():
    rows = [("A", "B", 5), ("B", "C", 3), ("A", "C", 0)]   # last link never used
    svg = _sankey(rows)
    assert isinstance(svg, str) and svg.isascii()
    assert svg.count("<path") == 2                          # A->B and B->C only
