"""PHASE6 A3: neighbour aggregates (docs/PHASE6_PLAN.md's A3 row; ARCHITECTURE
5.25's AB-3 "inclusive radius"/self-exclusion, AB-4 vectorised, C5 alive mask,
A1 tick snapshot and A2 categorical-attribute rules, all reused as-is by
NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX/KNN_SUM/KNN_MEAN). Every expected value
below is either a numpy brute force over the SAME alive/state/self-exclusion
rule NEAR/NEAREST already use, or a hand derivation from the tick-snapshot
rule test_agents_tick.py already proves for AGENT(...).v.
"""
import math

import numpy as np
import pytest

from nagasim.core import agents as A
from conftest import problem_codes, run_env


def _model(*records, time_limit=1.5):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _run(layout, seed=1):
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env


def _pop(env, name):
    return next(p for p in env.ctx.agent_system.populations if p.name == name)


def _codes(layout):
    return problem_codes(layout)


# Agent i sits at FIELD_POINTS[i] and its "value" attribute IS i -- so a
# brute-force reduction over a set of neighbour indices reduces the indices
# themselves, with no separate value table to keep in sync.
FIELD_POINTS = [(2.0, 3.0), (40.0, 5.0), (12.0, 30.0), (8.0, 8.0),
                (25.0, 25.0), (33.0, 1.0), (5.0, 45.0), (19.0, 19.0)]
N = len(FIELD_POINTS)
RING_POINTS = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]


def _field_type(categorical=False):
    attrs = [A.Attribute("value", "self.id")]
    if categorical:
        attrs.append(A.Attribute("tag", "\"a\"", kind="category", categories=["a", "b"]))
    return A.AgentType("Field", attributes=attrs, states=["Idle", "Busy"],
                       space=A.Space("continuous", [0, 0, 50, 50]))


def _field_pop(idle=N, busy=0):
    return A.Population("Field", "Field", N, initial_states={"Idle": idle, "Busy": busy},
                        placement={"kind": "table", "points": [list(p) for p in FIELD_POINTS]})


def _probe(name, point, expr):
    """One agent pinned at *point*; a t=0 transition writes `self.result =
    expr` -- the same trick test_agent_movement_oracle.py's NEAR test uses."""
    atype = A.AgentType(name, attributes=[A.Attribute("result", "-1")], states=["A", "B"],
                        space=A.Space("continuous", [0, 0, 50, 50]),
                        transitions=[A.Transition("A", "B", "timeout", "0",
                                                  actions=[A.Action.set("self.result", expr)])])
    pop = A.Population(name, name, 1, initial_states={"A": 1},
                       placement={"kind": "table", "points": [list(point)]})
    return atype, pop


def _brute(point, r, state_mask=None):
    """SUM/MEAN/MIN/MAX of Field's "value" over agents within r (inclusive),
    optionally state-masked: the independent oracle for NEAR_*."""
    pts = np.array(FIELD_POINTS)
    mask = np.hypot(pts[:, 0] - point[0], pts[:, 1] - point[1]) <= r
    if state_mask is not None:
        mask &= state_mask
    values = np.flatnonzero(mask).astype(float)     # value == index
    return {"SUM": float(values.sum()) if len(values) else 0.0,
            "MEAN": float(values.mean()) if len(values) else 0.0,
            "MIN": float(values.min()) if len(values) else math.nan,
            "MAX": float(values.max()) if len(values) else math.nan}


def _knn_brute(point, k, state_mask=None):
    """The k nearest Field agents to *point* (state-masked BEFORE the k are
    chosen), ties broken by ascending index -- the independent oracle for
    KNN_*, using the same np.lexsort tie-break as `agents.knn_aggregate`."""
    pts, idx = np.array(FIELD_POINTS), np.arange(N)
    if state_mask is not None:
        pts, idx = pts[state_mask], idx[state_mask]
    d2 = (pts[:, 0] - point[0]) ** 2 + (pts[:, 1] - point[1]) ** 2
    order = np.lexsort((idx, d2))[:k]
    return idx[order].astype(float)


# ---------------------------------------------------- 1. NEAR_* vs brute force
def test_near_aggregates_match_brute_force_including_the_boundary():
    """Four query points/radii unrelated to the field, plus one built to land
    EXACTLY on the boundary (AB-3's inclusive radius)."""
    fx, fy = FIELD_POINTS[0]
    combos = [("QA", (10.0, 10.0), 8.0), ("QA2", (10.0, 10.0), 30.0),
              ("QB", (33.0, 1.0), 5.0), ("QBound", (fx + 6.0, fy), 6.0)]
    types, pops = [_field_type()], [_field_pop()]
    for how in ("SUM", "MEAN", "MIN", "MAX"):
        for name, point, r in combos:
            atype, pop = _probe(f"{name}_{how}", point, f'NEAR_{how}("Field", "value", {r})')
            types.append(atype)
            pops.append(pop)
    layout = _model(*types, *pops)
    assert not _codes(layout)
    env = _run(layout)
    for how in ("SUM", "MEAN", "MIN", "MAX"):
        for name, point, r in combos:
            expected = _brute(point, r)[how]
            got = float(_pop(env, f"{name}_{how}").attrs["result"][0])
            if math.isnan(expected):
                assert math.isnan(got), (how, name)
            else:
                assert got == pytest.approx(expected), (how, name)


# --------------------------------------------------------- 2. NEAR state filter
def test_near_state_filter_matches_brute_force():
    point, r = (20.0, 20.0), 40.0
    busy_mask = np.arange(N) >= 4                    # 0-3 Idle, 4-7 Busy
    atype, pop = _probe("QS", point, f'NEAR_MEAN("Field", "value", {r}, "Busy")')
    layout = _model(_field_type(), _field_pop(idle=4, busy=4), atype, pop)
    assert not _codes(layout)
    env = _run(layout)
    expected = _brute(point, r, state_mask=busy_mask)["MEAN"]
    assert float(_pop(env, "QS").attrs["result"][0]) == pytest.approx(expected)


# ------------------------------------------------------ 3. self-exclusion (NEAR)
def test_near_excludes_self_from_its_own_population():
    ring_type = A.AgentType(
        "Ring", attributes=[A.Attribute("value", "self.id"), A.Attribute("result", "-1")],
        states=["A", "B"], space=A.Space("continuous", [0, 0, 10, 10]),
        transitions=[A.Transition("A", "B", "timeout", "0",
                                  actions=[A.Action.set("self.result",
                                                        'NEAR_SUM("Ring", "value", 100)')])])
    ring_pop = A.Population("Ring", "Ring", len(RING_POINTS),
                            initial_states={"A": len(RING_POINTS)},
                            placement={"kind": "table", "points": [list(p) for p in RING_POINTS]})
    layout = _model(ring_type, ring_pop)
    assert not _codes(layout)
    ring = _pop(_run(layout), "Ring")
    total = sum(range(len(RING_POINTS)))
    for i in range(len(RING_POINTS)):
        assert float(ring.attrs["result"][i]) == pytest.approx(total - i)     # everyone but self


# --------------------------------------------------- 4. empty neighbourhood rule
def test_empty_neighbourhood_sum_mean_are_zero_min_max_are_nan():
    point, r = (1000.0, 1000.0), 1.0                  # nowhere near the field
    types, pops = [_field_type()], [_field_pop()]
    for how in ("SUM", "MEAN", "MIN", "MAX"):
        atype, pop = _probe(f"E_{how}", point, f'NEAR_{how}("Field", "value", {r})')
        types.append(atype)
        pops.append(pop)
    env = _run(_model(*types, *pops))
    assert float(_pop(env, "E_SUM").attrs["result"][0]) == 0.0
    assert float(_pop(env, "E_MEAN").attrs["result"][0]) == 0.0
    assert math.isnan(float(_pop(env, "E_MIN").attrs["result"][0]))
    assert math.isnan(float(_pop(env, "E_MAX").attrs["result"][0]))


# ------------------------------------------------------------ 5. KNN vs ties
def test_knn_ties_are_broken_by_ascending_index():
    """Four agents all exactly distance 10 from the origin: KNN_MEAN(k=2) must
    take indices 0,1 (not whichever pair a tree traversal happens to return)."""
    tie_points = [(10.0, 0.0), (0.0, 10.0), (-10.0, 0.0), (0.0, -10.0)]
    tie_type = A.AgentType("Ties", attributes=[A.Attribute("value", "self.id")], states=["S"],
                           space=A.Space("continuous", [-20, -20, 20, 20]))
    tie_pop = A.Population("Ties", "Ties", len(tie_points), initial_states={"S": len(tie_points)},
                           placement={"kind": "table", "points": [list(p) for p in tie_points]})
    q_mean_type, q_mean_pop = _probe("QMean", (0.0, 0.0), 'KNN_MEAN("Ties", "value", 2)')
    q_sum_type, q_sum_pop = _probe("QSum3", (0.0, 0.0), 'KNN_SUM("Ties", "value", 3)')
    layout = _model(tie_type, tie_pop, q_mean_type, q_mean_pop, q_sum_type, q_sum_pop)
    assert not _codes(layout)
    env = _run(layout)
    assert float(_pop(env, "QMean").attrs["result"][0]) == pytest.approx(0.5)      # (0 + 1) / 2
    assert float(_pop(env, "QSum3").attrs["result"][0]) == pytest.approx(3.0)      # 0 + 1 + 2


# --------------------------------- 6. KNN vs brute force, with a state filter
def test_knn_matches_brute_force_and_respects_the_state_filter():
    point, k = (20.0, 20.0), 3
    busy_mask = np.arange(N) >= 4
    a1, p1 = _probe("KAll", point, f'KNN_MEAN("Field", "value", {k})')
    a2, p2 = _probe("KBusy", point, f'KNN_MEAN("Field", "value", {k}, "Busy")')
    layout = _model(_field_type(), _field_pop(idle=4, busy=4), a1, p1, a2, p2)
    assert not _codes(layout)
    env = _run(layout)
    assert float(_pop(env, "KAll").attrs["result"][0]) == pytest.approx(
        _knn_brute(point, k).mean())
    assert float(_pop(env, "KBusy").attrs["result"][0]) == pytest.approx(
        _knn_brute(point, k, busy_mask).mean())


# ------------------------------------------------------- 7. self-exclusion (KNN)
def test_knn_excludes_self_from_its_own_population():
    ring_type = A.AgentType(
        "Ring2", attributes=[A.Attribute("value", "self.id"), A.Attribute("result", "-1")],
        states=["A", "B"], space=A.Space("continuous", [0, 0, 10, 10]),
        transitions=[A.Transition("A", "B", "timeout", "0",
                                  actions=[A.Action.set("self.result",
                                                        'KNN_MEAN("Ring2", "value", 2)')])])
    ring_pop = A.Population("Ring2", "Ring2", len(RING_POINTS),
                            initial_states={"A": len(RING_POINTS)},
                            placement={"kind": "table", "points": [list(p) for p in RING_POINTS]})
    env = _run(_model(ring_type, ring_pop))
    ring = _pop(env, "Ring2")
    pts, idx_all = np.array(RING_POINTS), np.arange(len(RING_POINTS))
    for i in range(len(RING_POINTS)):
        others = idx_all[idx_all != i]
        d2 = (pts[others, 0] - pts[i, 0]) ** 2 + (pts[others, 1] - pts[i, 1]) ** 2
        expected = others[np.lexsort((others, d2))[:2]].astype(float).mean()
        assert float(ring.attrs["result"][i]) == pytest.approx(expected)


# --------------------------------------------- 8. synchronous tick reads `front`
def test_synchronous_tick_reads_the_tick_start_snapshot():
    """Same rotation idiom test_agents_tick.py proves for AGENT(...).v, through
    NEAR_SUM instead: v = [10, 20, 30] at three close points (r=10 covers every
    pair); a synchronous tick's on_tick sums the OTHER two agents' `v` -- if it
    read live (already-written) values instead of the tick-start snapshot, an
    agent later in the batch would sum values some earlier agent already
    overwrote, and the result would depend on activation order."""
    cell = A.AgentType("Cell", attributes=[A.Attribute("v", "10 * (self.id + 1)")], states=["S"],
                       space=A.Space("continuous", [0, 0, 10, 10]), tick=A.Tick(1, "synchronous"),
                       on_tick=[A.Action.set("self.v", 'NEAR_SUM("Ring3", "v", 10)')])
    pop = A.Population("Ring3", "Cell", 3, initial_states={"S": 3},
                       placement={"kind": "table", "points": [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]})
    env = _run(_model(cell, pop))
    assert _pop(env, "Ring3").attrs["v"].tolist() == pytest.approx([50.0, 40.0, 30.0])


# -------------------------------------------------------------- 9. validation
def test_literal_validation_matches_the_a2_rules():
    field_type, field_pop = _field_type(categorical=True), _field_pop()

    def codes_for(expr):
        atype, pop = _probe("P", (0.0, 0.0), expr)
        return _codes(_model(field_type, field_pop, atype, pop))

    assert "E_AGENT_ATTRIBUTE" in codes_for('NEAR_SUM("Field", "state", 5)')       # not a column
    assert "E_AGENT_ATTRIBUTE" in codes_for('NEAR_MEAN("Field", "tag", 5)')        # categorical
    assert "E_AGENT_ATTRIBUTE" in codes_for('KNN_SUM("Field", "tag", 3)')          # categorical
    assert "E_AGENT_STATE" in codes_for('NEAR_SUM("Field", "value", 5, "Gone")')   # unknown state
    assert "E_AGENT_STATE" in codes_for('KNN_MEAN("Field", "value", 3, "Gone")')
    assert "E_POPULATION_UNKNOWN" in codes_for('NEAR_SUM("Nope", "value", 5)')
    assert "E_POPULATION_UNKNOWN" in codes_for('KNN_MEAN("Nope", "value", 3)')
    assert not codes_for('NEAR_SUM("Field", "value", 5)')
    assert not codes_for('KNN_MEAN("Field", "value", 3)')
