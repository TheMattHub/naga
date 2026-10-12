"""Independent oracle for Phase 6D W4 P4 (CP-SAT proposer),
docs/history/PHASE6-6D-W4-PLAN-2026-09-25.md ("P4 CP-SAT proposer" and
"Common rules") and ARCHITECTURE.md 5.16 (orders: CREATE arrival_mode
"Orders", per-order `sequence` with per-station step times).

Every expected number below comes from code written IN THIS FILE, never from
running CP-SAT or `nagasim.cpsat` (which does not exist yet -- P4 is a later
milestone, and other agents may be editing the packages while this file is
written): a brute-force job-shop solver that enumerates all (3!)^3 = 216
per-machine processing orders for a 3-job x 3-machine instance, builds each
combination's semi-active schedule by topological sort (a combination whose
job-order and machine-order constraints form a cycle is infeasible and
discarded), and keeps the minimum makespan; and a separate FIFO event
simulation and a 3!-permutation enumeration for the 1-machine weighted-
tardiness case. `nagasim.cpsat` is imported lazily, inside each test function
that needs it (the P2 oracle precedent), so a missing module fails those
tests individually with a clear ImportError rather than failing collection
for the whole file; `pytest.importorskip("ortools")` guards only the cases
that need CP-SAT to actually solve (1, 2, 4, 6), per the task brief, since
`ortools` is a lazy optional dependency (D122) that may not be installed.

Wire format is GUESSED from the plan text, same status as `test_schedule_log_oracle.py`'s
`svgcharts.gantt` guess -- flagged, not asserted as the only legal shape:
`cpsat.propose(layout, objective=..., time_limit_s=...)` returns an object
with `.layout` (the proposed, override-applied model dict) and either
`.makespan` or `.objective` (the solved objective value); `_propose()` below
is the ONE adapter that reads that shape, so the coordinator reconciles the
guess in one place, not in every test. The proposed layout's `orders` carry
the SAME `release`/`due`/`priority` fields as the input, plus (unchecked
here, not in the six requested cases) a `plan` attribute per D122.
"""
from __future__ import annotations

import itertools
import sys
from collections import deque

import pytest

from conftest import create, dispose, layout
from test_sequences import _station, _station_process, _route, _step

# ============================================================ the 3x3 instance
# Each job visits every machine exactly once, in a different order per job
# (a genuine job shop, not a flow shop); durations are integers chosen so the
# optimal makespan AND the optimal per-machine schedule are both unique
# (confirmed by exhaustive enumeration below: exactly 1 of the 63 feasible
# combinations reaches the minimum). All three jobs release at t=0, so FIFO
# dispatch (arrival order at each machine, ties broken by declaration order
# A, B, C -- the convention `test_schedule_log_oracle.py`'s flow shop already uses)
# is forced to make a real choice, and it makes the WRONG one: FIFO's
# makespan (12) is strictly worse than the optimum (11), which is the whole
# point of a proposer.
JOBS = {
    "A": [("M1", 4), ("M2", 2), ("M3", 3)],
    "B": [("M2", 3), ("M1", 3), ("M3", 2)],
    "C": [("M3", 2), ("M2", 4), ("M1", 1)],
}
RELEASE = {"A": 0, "B": 0, "C": 0}
MACHINES = ["M1", "M2", "M3"]
JOB_IDS = ["A", "B", "C"]
RESOURCE_OF_MACHINE = {"M1": "R1", "M2": "R2", "M3": "R3"}
BLOCK_OF_MACHINE = {"M1": "p1", "M2": "p2", "M3": "p3"}


# ---------------------------------------------------------- brute-force solver
def _ops_on_machine(machine):
    return [(job, idx) for job in JOB_IDS
            for idx, (m, _d) in enumerate(JOBS[job]) if m == machine]


def _build_schedule(machine_orders):
    """The semi-active schedule for one (per-machine job order) combination:
    a topological sort over job-precedence edges (a job's own step order)
    plus machine-precedence edges (this combination's order), each operation
    starting at the max of its predecessors' end times. Returns None if those
    two edge sets form a cycle (a combination no valid schedule can realise --
    "discard infeasible cyclic ones")."""
    all_ops = [(job, idx) for job in JOB_IDS for idx in range(len(JOBS[job]))]
    preds = {op: set() for op in all_ops}
    for job in JOB_IDS:
        for idx in range(1, len(JOBS[job])):
            preds[(job, idx)].add((job, idx - 1))
    for order in machine_orders.values():
        for i in range(1, len(order)):
            preds[order[i]].add(order[i - 1])
    succs = {op: [] for op in all_ops}
    for op, ps in preds.items():
        for p in ps:
            succs[p].append(op)
    indeg = {op: len(preds[op]) for op in all_ops}
    queue = deque(op for op in all_ops if indeg[op] == 0)
    topo = []
    while queue:
        op = queue.popleft()
        topo.append(op)
        for s in succs[op]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    if len(topo) != len(all_ops):
        return None                                              # cycle
    start, end = {}, {}
    for op in topo:
        job, idx = op
        duration = JOBS[job][idx][1]
        earliest = RELEASE[job] if idx == 0 else end[(job, idx - 1)]
        for p in preds[op]:
            earliest = max(earliest, end[p])
        start[op] = earliest
        end[op] = earliest + duration
    return start, end


def brute_force_optimum():
    """Exhaustive (3!)^3 = 216 search, independent of CP-SAT and of the
    implementation. Returns (best_makespan, best_machine_orders): confirmed
    by hand-inspection to be the UNIQUE minimum (11), reached by exactly one
    of the 63 feasible combinations, with machine orders
    M1: A, B, C -- M2: B, A, C -- M3: C, A, B."""
    machine_ops = {m: _ops_on_machine(m) for m in MACHINES}
    best_makespan, best_orders = None, None
    for combo in itertools.product(*(itertools.permutations(machine_ops[m]) for m in MACHINES)):
        machine_orders = dict(zip(MACHINES, combo))
        sched = _build_schedule(machine_orders)
        if sched is None:
            continue
        makespan = max(sched[1].values())
        if best_makespan is None or makespan < best_makespan:
            best_makespan, best_orders = makespan, machine_orders
    return best_makespan, best_orders


# --------------------------------------------------------------- FIFO oracle
def fifo_makespan():
    """The as-is dispatch rule (FIFO: earliest-ready-first per machine, ties
    broken by declaration order A, B, C), simulated as an ordinary
    discrete-event loop -- independent of the topological-sort solver above,
    so the two serve as a cross-check on each other when they happen to
    agree (they need not, and here they do not: see the module docstring)."""
    pos = {j: 0 for j in JOB_IDS}
    job_ready = {j: RELEASE[j] for j in JOB_IDS}      # time job j's current op can queue
    job_queued = {j: False for j in JOB_IDS}          # already sitting in a machine's queue
    machine_busy_until = {m: None for m in MACHINES}  # None = idle
    machine_queue = {m: [] for m in MACHINES}
    done = {j: False for j in JOB_IDS}
    end = {}

    def machine_of(j):
        return JOBS[j][pos[j]][0]

    while not all(done.values()):
        candidates = [job_ready[j] for j in JOB_IDS if not done[j] and not job_queued[j]]
        candidates += [t for t in machine_busy_until.values() if t is not None]
        t = min(candidates)
        for m in MACHINES:                            # 1) machines finishing at t go idle
            if machine_busy_until[m] == t:
                machine_busy_until[m] = None
        for j in JOB_IDS:                              # 2) jobs arriving at t join their queue
            if not done[j] and not job_queued[j] and job_ready[j] == t:
                machine_queue[machine_of(j)].append(j)
                job_queued[j] = True
        for m in MACHINES:                             # 3) idle machines dispatch their front job
            if machine_busy_until[m] is None and machine_queue[m]:
                j = machine_queue[m].pop(0)
                duration = JOBS[j][pos[j]][1]
                end[(j, pos[j])] = t + duration
                machine_busy_until[m] = t + duration
                job_queued[j] = False
                pos[j] += 1
                if pos[j] >= len(JOBS[j]):
                    done[j] = True
                else:
                    job_ready[j] = t + duration
    return max(end.values())


def test_case3_fifo_makespan_is_at_least_the_optimum():
    """Case 3: the as-is FIFO deterministic makespan, computed independently
    above, is >= the brute-force optimum -- the exact value (12 vs 11) is
    asserted, not just the inequality, since the instance was built so the
    two differ (a proposer that could never beat FIFO would be pointless)."""
    optimum, _ = brute_force_optimum()
    assert optimum == 11
    assert fifo_makespan() == 12
    assert fifo_makespan() >= optimum


# ============================================================ layout builders
def _job_shop_layout(run_length=30):
    """3 orders (A, B, C), each with its own `sequence` naming the station
    order from JOBS above (ARCHITECTURE 5.16); one single-server FIFO
    resource per machine (R1/R2/R3 behind stations M1/M2/M3, process blocks
    p1/p2/p3) -- the base model `cpsat.propose` sees. FIFO is the resources'
    OWN discipline here; P4's contract is that a proposal returns its own
    override with `discipline: "Plan"` set, not that the base model needs it."""
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0), _route()]
    for m in MACHINES:
        blocks.append(_station(m))
    for m in MACHINES:
        blocks.append(_station_process(BLOCK_OF_MACHINE[m], RESOURCE_OF_MACHINE[m]))
    blocks.append(dispose())
    links = [{"source": "release", "target": "route_1"}, {"source": "route_1", "target": "dispose_1"}]
    for m in MACHINES:
        links.append({"source": m, "target": BLOCK_OF_MACHINE[m]})
        links.append({"source": BLOCK_OF_MACHINE[m], "target": "route_1"})
    sequences = [{"name": job, "steps": [_step(m, float(d)) for m, d in JOBS[job]]}
                 for job in JOB_IDS]
    orders = [{"id": job, "release": float(RELEASE[job]), "due": 100.0, "quantity": 1,
               "entity_type": job, "sequence": job, "priority": 1}
              for job in JOB_IDS]
    resources = [{"name": RESOURCE_OF_MACHINE[m], "capacity": 1, "discipline": "FIFO"}
                 for m in MACHINES]
    return layout(blocks, links, resources=resources, sequences=sequences, orders=orders,
                  outputs={"schedule": True},
                  simulation={"time_limit": run_length, "warmup": 0, "replications": 1,
                              "deterministic": True})


# ------------------------------------------------------- weighted-tardiness case
# One machine, 3 orders, all released at t=0. Weight is the order `priority`
# (D122: "weights are order priority"). Enumerating the 6 permutations by
# hand (sequential completion times on one machine): X,Y,Z is the UNIQUE
# minimum, weighted tardiness 6 -- X finishes at 4 (due 4, on time), Y at 6
# (due 6, on time), Z at 12 (due 6, tardy 6 * weight 1 = 6). Every other
# order scores strictly worse (8, 10, 18, 34, 36), so the optimum is also
# unique here, not just its value.
TARDINESS_ORDERS = {
    "X": {"proc": 4.0, "due": 4.0, "weight": 1},
    "Y": {"proc": 2.0, "due": 6.0, "weight": 5},
    "Z": {"proc": 6.0, "due": 6.0, "weight": 1},
}
TARDINESS_JOB_IDS = ["X", "Y", "Z"]


def wt_brute_force_optimum():
    best = None
    for perm in itertools.permutations(TARDINESS_JOB_IDS):
        t, wt = 0.0, 0.0
        for job in perm:
            spec = TARDINESS_ORDERS[job]
            t += spec["proc"]
            wt += spec["weight"] * max(0.0, t - spec["due"])
        if best is None or wt < best[0]:
            best = (wt, perm)
    return best                                             # (6.0, ("X", "Y", "Z"))


def _tardiness_layout(run_length=30):
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0), _route(),
              _station("M"), _station_process("p1", "R1"), dispose()]
    links = [{"source": "release", "target": "route_1"}, {"source": "route_1", "target": "dispose_1"},
             {"source": "M", "target": "p1"}, {"source": "p1", "target": "route_1"}]
    sequences = [{"name": job, "steps": [_step("M", TARDINESS_ORDERS[job]["proc"])]}
                 for job in TARDINESS_JOB_IDS]
    orders = [{"id": job, "release": 0.0, "due": TARDINESS_ORDERS[job]["due"], "quantity": 1,
               "entity_type": job, "sequence": job, "priority": TARDINESS_ORDERS[job]["weight"]}
              for job in TARDINESS_JOB_IDS]
    resources = [{"name": "R1", "capacity": 1, "discipline": "FIFO"}]
    return layout(blocks, links, resources=resources, sequences=sequences, orders=orders,
                  outputs={"schedule": True},
                  simulation={"time_limit": run_length, "warmup": 0, "replications": 1,
                              "deterministic": True})


# ============================================================ cpsat adapter
def _propose(layout_dict, objective, time_limit_s=10):
    """The ONE place that reads the guessed `cpsat.propose` result shape
    (`.layout` plus `.makespan` or `.objective`) -- see module docstring."""
    from nagasim import cpsat
    result = cpsat.propose(layout_dict, objective=objective, time_limit_s=time_limit_s)
    proposed_layout = result.layout
    # Coordinator wire reconciliation: the result carries both; the solved
    # value of the REQUESTED objective is `.objective` (makespan == objective
    # only when objective="makespan").
    return proposed_layout, result.objective


# ============================================================ Case 1: makespan
def test_case1_makespan_objective_matches_brute_force_optimum():
    pytest.importorskip("ortools")
    optimum, _ = brute_force_optimum()
    _proposed_layout, objective_value = _propose(_job_shop_layout(), objective="makespan")
    assert objective_value == pytest.approx(optimum)


# ============================================================ Case 2: Plan replay
def test_case2_deterministic_plan_discipline_reproduces_the_proposed_makespan():
    pytest.importorskip("ortools")
    from nagasim import api
    optimum, best_orders = brute_force_optimum()
    proposed_layout, objective_value = _propose(_job_shop_layout(), objective="makespan")
    assert objective_value == pytest.approx(optimum)

    results = api.run_model(proposed_layout, replications=1)
    sched = results["schedule_log"]
    assert sched["End"].max() == pytest.approx(optimum)
    for m in MACHINES:
        rows = sched[sched["Resource"] == RESOURCE_OF_MACHINE[m]].sort_values("Start")
        expected_order = [job for job, _idx in best_orders[m]]
        assert list(rows["Order"]) == expected_order


# ============================================================ Case 4: release floor
def test_case4_proposed_release_never_earlier_than_original():
    pytest.importorskip("ortools")
    proposed_layout, _objective_value = _propose(_job_shop_layout(), objective="makespan")
    orders_by_id = {o["id"]: o for o in proposed_layout["orders"]}
    for job in JOB_IDS:
        assert orders_by_id[job]["release"] >= RELEASE[job] - 1e-9


# ============================================================ Case 5: missing ortools
def test_case5_missing_ortools_raises_a_clear_error(monkeypatch):
    """Only meaningful once `nagasim.cpsat` exists -- it names the module
    whose lazy `import ortools` (D122: inside its one entry function) this
    test needs to observe failing. Skips (not fails) until then, per the
    task brief ("only if importable module exists")."""
    cpsat = pytest.importorskip("nagasim.cpsat")
    monkeypatch.setitem(sys.modules, "ortools", None)   # forces the next `import ortools` to raise
    with pytest.raises(Exception) as exc_info:
        cpsat.propose(_job_shop_layout(), objective="makespan", time_limit_s=1)
    assert "ortools" in str(exc_info.value).lower()


# ============================================================ Case 6: weighted tardiness
def test_case6_weighted_tardiness_objective_matches_hand_enumeration():
    pytest.importorskip("ortools")
    from nagasim import api
    optimum_wt, optimum_sequence = wt_brute_force_optimum()
    assert (optimum_wt, optimum_sequence) == (6.0, ("X", "Y", "Z"))

    proposed_layout, objective_value = _propose(_tardiness_layout(), objective="weighted_tardiness")
    assert objective_value == pytest.approx(optimum_wt)

    results = api.run_model(proposed_layout, replications=1)
    sched = results["schedule_log"]
    rows = sched[sched["Resource"] == "R1"].sort_values("Start")
    assert list(rows["Order"]) == list(optimum_sequence)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
