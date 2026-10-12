"""Analytical oracles for DEVLOG M1: the KPIs must reproduce closed-form queueing
results, exactly where the model is deterministic and inside a confidence interval
where it is stochastic.

Runtime budget: the whole module stays a few seconds; if it grows, cut run_len or
replications rather than the assertions.
"""
import functools
import math

import numpy as np
import pytest

from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.stats import ci95

from conftest import metric_value as _metric, resource_row as _resource_row

# Sized by measurement, not habit: 20 x 3000 keeps the M/M/2 Lq interval about
# 13% wide (the assertions demand < 15%) and costs 2.2s less than 20 x 4000.
REPLICATIONS = 20
RUN_LEN = 3000
WARMUP = 300


# ---------------------------------------------------------------- helpers
def _mmc_model(make_model, *, arrival_rate, service_rate, capacity, filename):
    """Create(Exp) -> Seize(Exp service) -> Release -> Dispose, one resource."""
    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Exponential",
                    "dist_params": {"rate": arrival_rate},
                    "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
        {"name": "seize_1", "description": "s", "type": "SEIZE", "position": [1, 0],
         "params": {"distribution": "Exponential", "dist_params": {"rate": service_rate},
                    "priority": 0, "quantity": 1, "resource_name": "Server"}},
        {"name": "release_1", "description": "r", "type": "RELEASE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                    "resource_name": "Server"}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [3, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "seize_1"], ["seize_1", "release_1"], ["release_1", "dispose_1"]]
    resources = [{"name": "Server", "capacity": capacity, "type": "Resource"}]
    return make_model(blocks, links, resources=resources, filename=filename)


def _seeded_runs(path, replications, run_len, warmup, pool=None):
    """One KPI dict per seed, in seed order. Over the session pool when one is
    given (PHASE5 O7): a replication is seeded from its own seed, so the
    numbers are identical whoever runs it; only the wall time changes."""
    run = functools.partial(run_single_simulation, path, run_len=run_len, warmup=warmup)
    if pool is None:
        return [run(seed=seed) for seed in range(replications)]
    futures = [pool.submit(run, seed=seed) for seed in range(replications)]
    return [future.result() for future in futures]


def _replicate(path, replications=REPLICATIONS, run_len=RUN_LEN, warmup=WARMUP, pool=None):
    """Per-replication (utilization, avg queue length, avg throughput time)."""
    utils, queues, times = [], [], []
    for kpi in _seeded_runs(path, replications, run_len, warmup, pool):
        row = _resource_row(kpi)
        utils.append(row["Usage % (snapshot)"] / 100.0)
        queues.append(row["Queue Length Avg"])
        times.append(_metric(kpi, "Avg Throughput Time"))
    return utils, queues, times


def _assert_theory_in_ci(samples, theory, label, max_half_width_pct=0.15):
    st = ci95(samples, confidence=0.99)
    half = st["ub"] - st["mean"]
    assert st["lb"] <= theory <= st["ub"], (
        f"{label}: theory {theory:.4f} outside 99% CI [{st['lb']:.4f}, {st['ub']:.4f}] "
        f"(mean {st['mean']:.4f})"
    )
    assert half < max_half_width_pct * abs(theory), (
        f"{label}: CI half-width {half:.4f} is not < {max_half_width_pct:.0%} of "
        f"the theoretical {theory:.4f} -- the estimator is too noisy to be a useful oracle"
    )


def _erlang_c_lq(lam, mu, c):
    """Expected queue length of an M/M/c queue (Erlang-C)."""
    a = lam / mu
    rho = a / c
    p0 = 1.0 / (sum(a ** n / math.factorial(n) for n in range(c))
                + a ** c / (math.factorial(c) * (1 - rho)))
    return p0 * (a ** c) * rho / (math.factorial(c) * (1 - rho) ** 2), rho


def test_mm1_sanity_with_a_loose_tolerance(make_model, worker_pool):
    """The M/M/1 family's one oracle: few replications, short run, wide CI
    tolerance, and in the DEFAULT suite so a broken estimator is caught on
    every `pytest` run. The PROCESS-block form needs no oracle of its own --
    `test_seize_release_chain_equals_one_process_block` below proves the two
    forms bit-identical, and the Jackson case study runs the tight statistical
    version of both (suite-reduction pass, PHASE4 B4/B5)."""
    path = _mmc_model(make_model, arrival_rate=1.0, service_rate=1.25, capacity=1,
                      filename="mm1_fast.json")
    utils, queues, times = _replicate(path, replications=8, run_len=1500, warmup=150, pool=worker_pool)

    _assert_theory_in_ci(utils, 0.8, "M/M/1 utilization (fast)", max_half_width_pct=0.5)
    _assert_theory_in_ci(queues, 3.2, "M/M/1 Queue Length Avg (fast)", max_half_width_pct=1.0)
    _assert_theory_in_ci(times, 4.0, "M/M/1 Avg Throughput Time (fast)", max_half_width_pct=0.6)


# ------------------------------------------------- Seize/Release == Process
def test_seize_release_chain_equals_one_process_block(make_model):
    """The Seize -> Release chain and a single PROCESS block share the seizing
    code path, so the statistical M/M/1 oracle only has to run ONCE (on the
    PROCESS form, below): here the two forms are proven bit-identical instead.

    Block names seed the RNG streams, so both service blocks are called
    "station"; RELEASE draws nothing (constant 0), so every stream lines up and
    the KPI frames must be equal element for element -- a far sharper (and ~40x
    cheaper) statement than "both land inside the same confidence interval"."""
    common = {"resources": [{"name": "Server", "capacity": 1, "discipline": "FIFO"}]}
    create = {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
              "params": {"e_type": "DEFAULT", "distribution": "Exponential",
                         "dist_params": {"rate": 1.0}, "entities_per_arrival": 1,
                         "max_arrivals": 0, "start_time": 0}}
    dispose = {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [3, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}
    service = {"distribution": "Exponential", "dist_params": {"rate": 1.25},
               "priority": 0, "quantity": 1, "resource_name": "Server"}

    chain = make_model(
        [create,
         {"name": "station", "description": "s", "type": "SEIZE", "position": [1, 0],
          "params": dict(service)},
         {"name": "release_1", "description": "r", "type": "RELEASE", "position": [2, 0],
          "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                     "resource_name": "Server"}},
         dispose],
        [["create_1", "station"], ["station", "release_1"], ["release_1", "dispose_1"]],
        filename="chain.json", **common)
    single = make_model(
        [create,
         {"name": "station", "description": "s", "type": "PROCESS", "position": [1, 0],
          "params": {"action": "Seize Delay Release", **service}},
         dispose],
        [["create_1", "station"], ["station", "dispose_1"]],
        filename="single.json", **common)

    a = run_single_simulation(chain, seed=0, run_len=500, warmup=50)
    b = run_single_simulation(single, seed=0, run_len=500, warmup=50)
    for frame in ("system", "resource_summary", "entity_summary"):
        assert a[frame].equals(b[frame]), f"{frame} differs between Seize/Release and Process"


def test_a_capacity_two_resource_really_serves_two_entities_at_once(make_model):
    """DEVLOG #17, exactly instead of statistically: two entities arrive
    together at a capacity-2 resource. If the capacity were silently forced to
    1 the second would queue and the run would take twice as long -- so the
    multi-server M/M/c oracle does not need to be run to catch that (the
    resource-SET M/M/2 oracle covers the queueing theory)."""
    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 100.0},
                    "entities_per_arrival": 2, "max_arrivals": 2, "start_time": 0}},
        {"name": "station", "description": "s", "type": "PROCESS", "position": [1, 0],
         "params": {"action": "Seize Delay Release", "distribution": "Constant",
                    "dist_params": {"value": 10.0}, "priority": 0, "quantity": 1,
                    "resource_name": "Server"}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "station"], ["station", "dispose_1"]]
    path = make_model(blocks, links, filename="cap2.json",
                      resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO"}])

    kpi = run_single_simulation(path, seed=0, run_len=60, warmup=0)

    row = _resource_row(kpi)
    assert row["Queue Length Max"] == pytest.approx(0.0, abs=1e-9)
    assert _metric(kpi, "Avg Throughput Time") == pytest.approx(10.0, abs=1e-9)
    assert row["Total Seizes"] == 2


# ---------------------------------------------------------------- D/D/1
def test_dd1_is_exact(make_model):
    """Constant IAT 2, constant service 1: utilization 0.5, no queue, W = 1."""
    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 2.0},
                    "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
        {"name": "seize_1", "description": "s", "type": "SEIZE", "position": [1, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 1.0},
                    "priority": 0, "quantity": 1, "resource_name": "Server"}},
        {"name": "release_1", "description": "r", "type": "RELEASE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                    "resource_name": "Server"}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [3, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "seize_1"], ["seize_1", "release_1"], ["release_1", "dispose_1"]]
    resources = [{"name": "Server", "capacity": 1, "type": "Resource"}]
    path = make_model(blocks, links, resources=resources, filename="dd1.json")

    kpi = run_single_simulation(path, seed=0, run_len=1000, warmup=0)
    row = _resource_row(kpi)

    assert row["Usage % (snapshot)"] / 100.0 == pytest.approx(0.5, abs=1e-6)
    assert row["Queue Length Avg"] == pytest.approx(0.0, abs=1e-6)
    assert row["Queue Length Max"] == pytest.approx(0.0, abs=1e-6)
    assert _metric(kpi, "Avg Throughput Time") == pytest.approx(1.0, abs=1e-6)
    assert _metric(kpi, "Simulation Time") == pytest.approx(1000.0)


# ---------------------------------------------------------------- reproducibility
def test_same_seed_gives_identical_results(make_model):
    """DEVLOG #25: no global RNG state, so a seed fully determines a replication."""
    path = _mmc_model(make_model, arrival_rate=1.0, service_rate=1.25, capacity=1,
                      filename="repro.json")

    first = run_single_simulation(path, seed=0, run_len=500, warmup=50)
    again = run_single_simulation(path, seed=0, run_len=500, warmup=50)
    other = run_single_simulation(path, seed=1, run_len=500, warmup=50)

    assert first["system"].equals(again["system"])
    assert not first["system"].equals(other["system"])


# ---------------------------------------------------------------- warm-up
def test_warmup_excludes_the_initial_burst(make_model):
    """DEVLOG #22: warm-up resets the collectors instead of filtering records.

    100 arrivals in [0, 100) then 50 in [100, 150): with warmup=100 only the
    second wave may be counted, and the seize collector must agree.
    """
    blocks = [
        {"name": "create_burst", "description": "burst", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                    "entities_per_arrival": 1, "max_arrivals": 100, "start_time": 0}},
        {"name": "create_late", "description": "late", "type": "CREATE", "position": [0, 1],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                    "entities_per_arrival": 1, "max_arrivals": 50, "start_time": 100}},
        {"name": "seize_1", "description": "s", "type": "SEIZE", "position": [1, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.5},
                    "priority": 0, "quantity": 1, "resource_name": "Server"}},
        {"name": "release_1", "description": "r", "type": "RELEASE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                    "resource_name": "Server"}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [3, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_burst", "seize_1"], ["create_late", "seize_1"],
             ["seize_1", "release_1"], ["release_1", "dispose_1"]]
    resources = [{"name": "Server", "capacity": 1, "type": "Resource"}]
    path = make_model(blocks, links, resources=resources, filename="warmup.json")

    hot = run_single_simulation(path, seed=0, run_len=200, warmup=0)
    cold = run_single_simulation(path, seed=0, run_len=200, warmup=100)

    assert _metric(hot, "Total Entities Created") == 150
    assert _metric(cold, "Total Entities Created") == 50
    assert _metric(cold, "Total Entities Completed") == 50
    assert _resource_row(cold)["Total Seizes"] == 50

    # the last arrival is at t=149 and leaves at 149.5, so the run drains there
    observed = _metric(cold, "Simulation Time")
    assert observed == pytest.approx(49.5)

    # 50 arrivals x 0.5 time units of service over the observed window
    assert _resource_row(cold)["Usage % (snapshot)"] / 100.0 == pytest.approx(25.0 / observed, abs=1e-4)


# ---------------------------------------------------------------- resource set
@pytest.mark.slow
def test_mm2_as_a_set_of_two_single_servers_matches_erlang_c(make_model, worker_pool):
    """ROADMAP 1.2: two capacity-1 resources behind a set with
    SmallestNumberBusy is an M/M/2 queue.

    Where the waiting entities are counted matters: an entity queued on a set
    has a request in BOTH member queues, so it is counted once, on the set's
    own collectors, and the member rows report no queue at all. The set row is
    therefore the queue KPI to compare with Lq, and its utilization is busy
    units over the members' total capacity.
    """
    lam, mu, c = 1.5, 1.0, 2
    lq_theory, rho_theory = _erlang_c_lq(lam, mu, c)
    # Suite-reduction pass (PHASE4 D1/D2): this was the second most expensive
    # test in the whole suite at the module's 20 x 3000 settings (7.2 s of a
    # 95 s budget). 12 x 2000 costs 2.7 s. Measured on this machine, the 99%
    # interval half-width moves from 1.5% to 2.7% of theory for utilization and
    # from 13.2% to 18.7% for Lq, and both still contain the theoretical value
    # -- so the ORACLE is unchanged and only its resolution is coarser, which
    # is why the Lq bound below is stated explicitly instead of taking the
    # module default of 15%.
    reps, run_len, warmup = 12, 2000, 200

    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Exponential",
                    "dist_params": {"rate": lam},
                    "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
        {"name": "process_1", "description": "p", "type": "PROCESS", "position": [1, 0],
         "params": {"action": "Seize Delay Release", "distribution": "Exponential",
                    "dist_params": {"rate": mu}, "priority": 0, "quantity": 1,
                    "resource_name": "Tellers"}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "process_1"], ["process_1", "dispose_1"]]
    resources = [{"name": "TellerA", "capacity": 1, "discipline": "FIFO"},
                 {"name": "TellerB", "capacity": 1, "discipline": "FIFO"}]
    layout = {
        "blocks": blocks, "links": links, "resources": resources,
        "resource_sets": [{"name": "Tellers", "members": ["TellerA", "TellerB"],
                           "rule": "SmallestNumberBusy"}],
        "entities": [], "variables": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }

    utils, queues, member_queues = [], [], []
    for kpi in _seeded_runs(layout, reps, run_len, warmup, worker_pool):
        row = _resource_row(kpi, "Tellers")
        utils.append(row["Usage % (snapshot)"] / 100.0)
        queues.append(row["Queue Length Avg"])
        member_queues.append(sum(_resource_row(kpi, m)["Queue Length Avg"]
                                 for m in ("TellerA", "TellerB")))

    _assert_theory_in_ci(utils, rho_theory, "M/M/2 set utilization", max_half_width_pct=0.10)
    _assert_theory_in_ci(queues, lq_theory, "M/M/2 set Queue Length Avg",
                         max_half_width_pct=0.25)
    assert max(member_queues) == 0, "a set member must never queue a wait-for-any request"


# ============ blocked tandem line (PHASE4 T5, ARCHITECTURE D78) =============
def _tandem_markov(mu1, mu2, places):
    """Exact stationary solution of a SATURATED two-machine line with `places`
    between the machines, solved numerically here like the M/M/1+M chain above.

    State `(n, blocked)`: *n* is the number of parts in the buffer plus the one
    in machine 2 (0 .. places+1), and `blocked` is 1 when machine 1 is holding a
    finished part it cannot put down -- only possible with the buffer full and
    machine 2 busy, i.e. at n = places+1. Machine 1 never starves (saturated
    source) and machine 2 works whenever n >= 1.

    Returns (throughput, P(machine 1 blocked)).
    """
    states = [(n, 0) for n in range(places + 2)] + [(places + 1, 1)]
    index = {s: i for i, s in enumerate(states)}
    rates = np.zeros((len(states), len(states)))
    for state in states:
        n, blocked = state
        i = index[state]
        if not blocked:                     # machine 1 finishes a part
            rates[i, index[(n + 1, 0) if n < places + 1 else (places + 1, 1)]] += mu1
        if n >= 1:                          # machine 2 finishes one
            # When machine 1 was blocked its part goes straight into the place
            # that just freed, so *n* is unchanged and the block clears.
            rates[i, index[(places + 1, 0) if blocked else (n - 1, 0)]] += mu2
    generator = rates - np.diag(rates.sum(axis=1))
    # pi Q = 0 with sum(pi) = 1, as one least-squares system.
    lhs = np.vstack([generator.T, np.ones(len(states))])
    rhs = np.zeros(len(states) + 1)
    rhs[-1] = 1.0
    pi = np.linalg.lstsq(lhs, rhs, rcond=None)[0]
    throughput = mu2 * sum(p for state, p in zip(states, pi) if state[0] >= 1)
    return throughput, float(pi[index[(places + 1, 1)]])


def _blocked_line(make_model, *, mu1, mu2, places, filename, arrivals=1800):
    """A saturated two-machine line: every part released at t=0, so machine 1
    is never starved and the only thing that stops it is a full buffer."""
    blocks = [
        {"name": "src", "description": "src", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant",
                    "dist_params": {"value": 0.0}, "entities_per_arrival": 1,
                    "max_arrivals": arrivals, "start_time": 0}},
        {"name": "m1", "description": "m1", "type": "PROCESS", "position": [1, 0],
         "params": {"action": "Seize Delay Release", "distribution": "Exponential",
                    "dist_params": {"rate": mu1}, "priority": 0, "quantity": 1,
                    "resource_name": "M1"}},
        {"name": "buf", "description": "buf", "type": "BUFFER", "position": [2, 0],
         "params": {"capacity": places}},
        {"name": "m2", "description": "m2", "type": "PROCESS", "position": [3, 0],
         "params": {"action": "Seize Delay Release", "distribution": "Exponential",
                    "dist_params": {"rate": mu2}, "priority": 0, "quantity": 1,
                    "resource_name": "M2"}},
        {"name": "out", "description": "out", "type": "DISPOSE", "position": [4, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["src", "m1"], ["m1", "buf"], ["buf", "m2"], ["m2", "out"]]
    return make_model(blocks, links,
                      resources=[{"name": "M1", "capacity": 1}, {"name": "M2", "capacity": 1}],
                      filename=filename)


def _line_samples(path, *, replications=6, run_len=1500, warmup=150, pool=None):
    """Per-replication (throughput, fraction of time machine 1 was blocked).

    "Simulation Time" is already the OBSERVED window (warm-up excluded), so it
    is the divisor the seize count needs.
    """
    throughputs, blocked = [], []
    for kpi in _seeded_runs(path, replications, run_len, warmup, pool):
        window = _metric(kpi, "Simulation Time")
        served = _resource_row(kpi, "M2")["Total Seizes"]
        throughputs.append(served / window)
        rows = kpi["buffer_summary"].set_index("Block")
        blocked.append(rows.loc["m1", "Blocked %"] / 100.0)
    return throughputs, blocked


@pytest.mark.slow
def test_blocked_tandem_line_matches_its_markov_chain(make_model, worker_pool):
    """Two exponential stations with a finite buffer have a small Markov chain
    with an exact answer; both the throughput and P(blocked) must land in it.

    Checked with NO buffer (pure blocking after service, where equal rates give
    the textbook 2/3 of a single machine) and with two places.
    """
    for places in (0, 2):
        theory_rate, theory_blocked = _tandem_markov(1.0, 1.0, places)
        path = _blocked_line(make_model, mu1=1.0, mu2=1.0, places=places,
                             filename=f"line_{places}.json")
        rates, blocked = _line_samples(path, pool=worker_pool)
        _assert_theory_in_ci(rates, theory_rate, f"tandem throughput, buffer {places}")
        # A looser noise guard than the throughput's: a probability around 0.2
        # is a smaller number to estimate, and the interval containing the
        # theory is what the oracle is actually for.
        _assert_theory_in_ci(blocked, theory_blocked, f"P(blocked), buffer {places}",
                             max_half_width_pct=0.2)


