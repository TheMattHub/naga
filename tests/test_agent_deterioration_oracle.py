"""Independent oracle for milestone H3 (agents + DES: patients deteriorating
while queued), built from closed-form queueing theory and hand Lindley
recursion, never from a run of the engine or of the template another agent is
building in parallel (`nagasim/models/ed_patient_agents.json`, not read here).

Model: Poisson arrivals (CREATE, Exponential interarrival) -> SEIZE a
capacity-1 "Server" (own delay 0, so SEIZE's forward instant IS the service
start) -> ASSIGN counts n_service_starts and, conditionally on
`entity.agent.state == "Deteriorated"`, n_deteriorated (the ternary form
`a if c else b` from `core/expressions.py`'s own grammar, not a DECIDE) ->
DELAY the service time -> RELEASE -> DISPOSE(remove_agent=True). Each
arriving entity carries a CREATE `population="Patients"` agent (D130) born in
state "Stable" with a transition to "Deteriorated" (D129/D130, section 5.38
of ARCHITECTURE.md); reading `entity.agent.state` at the SEIZE/ASSIGN instant
sees the agent's state exactly at the moment service starts.

Case 1 (stochastic): M/M/1 FIFO, lambda=0.8, mu=1, and a rate transition
Stable->Deteriorated at theta=0.5. The wait Wq before service has LST
W*(s) = (1-rho) + rho*(mu-lambda)/(mu-lambda+s), rho=lambda/mu; since the
transition is memoryless (rate, not timeout), P(deteriorated at service
start) = P(no Stable->Deteriorated event during Wq) = 1 - W*(theta) (the
LST evaluated at the hazard rate is exactly the chance the competing
exponential clock beats Wq -- a standard M/M/1-with-a-competing-clock
identity, not read off any run). Numerically p = 1 - (0.2 + 0.8*0.2/0.7)
= 1 - 3/7 = 4/7 = 0.571428...

Tolerance: replication-level batch means, not a naive binomial bound --
successive customers' waits in a queue are autocorrelated, so treating each
service start as an iid Bernoulli(p) trial understates the true variance.
Each of R independent replications (its own seed) contributes ONE p_hat_r
over its own N service starts; ci95() (`core/stats.py`, Student-t over the
R replication means, the same estimator `tests/test_oracles.py` already
uses for M/M/c) then absorbs the within-replication correlation because the
unit of replication is the whole trajectory, not the customer.

Warm-up: every replication starts EMPTY, so its earliest service starts see
a shorter-than-steady-state queue (lower p than theory) -- a review over four
25-rep blocks (seeds 0-99) found p_hat consistently ~1.5-2% low, about 3 SE
below theory when pooled, which is the empty-start transient, not sampling
noise. The counters skip service starts before `WARMUP_TIME`, the classic
M/M/1 relaxation time `1 / (sqrt(mu) - sqrt(lambda))**2` (~90 time units at
rho=0.8) -- the time constant for the queue-length process to forget its
initial condition (Morse 1958; used here as a rule of thumb, one relaxation
time). A separate, ungated `n_total_starts` still counts every service start
(warmup included) so the "did the horizon drain the whole run" check stays
exact.

Case 2 (deterministic): D/D/1, interarrival a=2, service s=3 (s > a, so a
short run's queue predictably backs up -- Lindley recursion W_0=0,
W_n = max(0, W_{n-1} + s - a) gives waits 0, 1, 2, 3 for 4 arrivals, by
hand, not simulated). Deterioration is a "timeout" transition at tau=1.5
(no randomness anywhere): deteriorated at service start iff wait >= tau,
i.e. customers 2 and 3 (waits 2, 3) but not 0, 1 (waits 0, 1) -- an exact
count, 2 of 4.
"""
import math

import pytest

from nagasim.core import agents as A
from nagasim.core.stats import ci95

from conftest import chain, dispose, problem_codes, run_env

RESOURCE = [{"name": "Server", "capacity": 1, "type": "Resource"}]
COUNTERS = [{"name": "n_total_starts", "initial": 0}, {"name": "n_service_starts", "initial": 0},
            {"name": "n_deteriorated", "initial": 0}]


def _count_assign(warmup=0.0):
    """n_total_starts counts every service start (the drain check); the other
    two skip anything before `warmup` (TNOW), so the empty-start transient
    can't bias p_hat = n_deteriorated / n_service_starts (see module docstring
    "Warm-up"). warmup=0.0 (case 2) makes all three counters agree."""
    after = f"TNOW >= {warmup!r}"
    return {
        "name": "count", "description": "count", "type": "ASSIGN", "position": [0, 0],
        "params": {"assignments": [
            {"target": "n_total_starts", "expr": "n_total_starts + 1"},
            {"target": "n_service_starts", "expr": f"n_service_starts + (1 if {after} else 0)"},
            {"target": "n_deteriorated",
             "expr": f'n_deteriorated + (1 if ({after} and entity.agent.state == "Deteriorated") else 0)'},
        ]},
    }


def _create(name, distribution, dist_params, n, population):
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0],
            "params": {"e_type": "DEFAULT", "distribution": distribution, "dist_params": dist_params,
                       "entities_per_arrival": 1, "max_arrivals": n, "start_time": 0,
                       "population": population}}


def _seize(name, distribution, dist_params):
    return {"name": name, "description": name, "type": "SEIZE", "position": [0, 0],
            "params": {"distribution": distribution, "dist_params": dist_params,
                       "priority": 0, "quantity": 1, "resource_name": "Server"}}


def _delay_dist(name, distribution, dist_params):
    return {"name": name, "description": name, "type": "DELAY", "position": [0, 0],
            "params": {"distribution": distribution, "dist_params": dist_params}}


def _release(name):
    return {"name": name, "description": name, "type": "RELEASE", "position": [0, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}, "resource_name": "Server"}}


def _model(*records, time_limit):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _codes(layout):
    return problem_codes(layout)


def _run(layout, seed, run_len):
    return run_env(layout, run_len, seed=seed)


# ------------------------------------------------------------- case 1: M/M/1
LAMBDA, MU, THETA = 0.8, 1.0, 0.5
RHO = LAMBDA / MU
# W*(THETA) from the LST formula in the module docstring; p = 1 - W*(THETA).
P_DETERIORATED = 1.0 - ((1.0 - RHO) + RHO * (MU - LAMBDA) / (MU - LAMBDA + THETA))

# M/M/1 relaxation time (Morse 1958): the queue-length process's own time
# constant for forgetting its (empty) initial condition. One relaxation time
# is the warm-up: service starts before it are excluded from p_hat.
WARMUP_TIME = 1.0 / (math.sqrt(MU) - math.sqrt(LAMBDA)) ** 2

REPLICATIONS = 25
ARRIVALS_PER_REP = 800


def _mm1_deteriorating_model(n):
    patient = A.AgentType("Patient", states=["Stable", "Deteriorated"], initial="Stable",
                          transitions=[A.Transition("Stable", "Deteriorated", "rate", str(THETA))])
    pop = A.Population("Patients", "Patient", 0, max_count=n)
    # Generous horizon (queue is stable, rho=0.8): mean total arrival span is
    # n/lambda; x3 leaves ample room for the tail to drain after arrivals stop.
    time_limit = n / LAMBDA * 3
    layout = _model(patient, pop, time_limit=time_limit)
    create = _create("arrive", "Exponential", {"rate": LAMBDA}, n, "Patients")
    seize = _seize("seize", "Constant", {"value": 0.0})
    service = _delay_dist("service", "Exponential", {"rate": MU})
    release = _release("release")
    out = dispose("out")
    out["params"]["remove_agent"] = True
    count = _count_assign(WARMUP_TIME)
    layout["blocks"] = [create, seize, count, service, release, out]
    layout["links"] = chain(create, seize, count, service, release, out)
    layout["variables"] = [dict(v) for v in COUNTERS]
    layout["resources"] = list(RESOURCE)
    return layout, time_limit


@pytest.mark.slow
def test_mm1_deterioration_probability_matches_the_competing_clock_lst():
    """Measured ~2.8s for 25 reps x 800 arrivals (this module's own machine) --
    over the 2s target, hence @pytest.mark.slow (see the module docstring's
    tolerance-derivation note); cut REPLICATIONS/ARRIVALS_PER_REP together with
    the tolerance below if this needs to shrink further."""
    p_hats = []
    for seed in range(REPLICATIONS):
        layout, time_limit = _mm1_deteriorating_model(ARRIVALS_PER_REP)
        assert not _codes(layout)
        env, model = _run(layout, seed, time_limit)
        n_total = env.ctx.variables["n_total_starts"]
        assert n_total == ARRIVALS_PER_REP, (
            f"seed {seed}: only {n_total}/{ARRIVALS_PER_REP} service starts by t={time_limit} "
            "-- horizon too short for the queue to drain"
        )
        n_starts = env.ctx.variables["n_service_starts"]         # post-warmup only
        n_det = env.ctx.variables["n_deteriorated"]
        assert 0 < n_starts < n_total                             # warmup actually excluded some
        p_hats.append(n_det / n_starts)
        # Conservation (D130): remove_agent=True removes an agent exactly when
        # its entity is disposed, so at ANY instant live agents == entities
        # still in the model -- checked here at the run's own end.
        pop = env.ctx.agent_system.by_name["Patients"]
        assert pop.live == len(env.ctx.live_entities)

    stats = ci95(p_hats, confidence=0.99)
    assert stats["lb"] <= P_DETERIORATED <= stats["ub"], (
        f"theory p={P_DETERIORATED:.6f} outside 99% CI [{stats['lb']:.6f}, {stats['ub']:.6f}] "
        f"(mean {stats['mean']:.6f} over {REPLICATIONS} reps of {ARRIVALS_PER_REP})"
    )
    half_width_pct = (stats["ub"] - stats["mean"]) / P_DETERIORATED
    assert half_width_pct < 0.15, (
        f"CI half-width is {half_width_pct:.1%} of theory -- too noisy to be a useful oracle "
        f"(mean {stats['mean']:.6f}, theory {P_DETERIORATED:.6f})"
    )


# -------------------------------------------------------- case 2: D/D/1 exact
A_INTERARRIVAL, S_SERVICE, TAU, N_DD1 = 2.0, 3.0, 1.5, 4
# Lindley recursion by hand: W_0=0, W_n = max(0, W_{n-1} + s - a).
DD1_WAITS = [0.0, 1.0, 2.0, 3.0]
DD1_N_DETERIORATED = sum(1 for w in DD1_WAITS if w >= TAU)   # 2 (customers 2, 3)


def test_dd1_deterioration_count_matches_the_hand_lindley_recursion():
    patient = A.AgentType("Patient", states=["Stable", "Deteriorated"], initial="Stable",
                          transitions=[A.Transition("Stable", "Deteriorated", "timeout", str(TAU))])
    pop = A.Population("Patients", "Patient", 0, max_count=N_DD1)
    time_limit = 50.0   # far past the 4th arrival (t=6) plus its service (ends t=15)
    layout = _model(patient, pop, time_limit=time_limit)
    create = _create("arrive", "Constant", {"value": A_INTERARRIVAL}, N_DD1, "Patients")
    seize = _seize("seize", "Constant", {"value": 0.0})
    service = _delay_dist("service", "Constant", {"value": S_SERVICE})
    release = _release("release")
    out = dispose("out")
    out["params"]["remove_agent"] = True
    count = _count_assign(0.0)   # no warm-up: 4 arrivals is the whole point
    layout["blocks"] = [create, seize, count, service, release, out]
    layout["links"] = chain(create, seize, count, service, release, out)
    layout["variables"] = [dict(v) for v in COUNTERS]
    layout["resources"] = list(RESOURCE)
    assert not _codes(layout)

    env, model = _run(layout, seed=0, run_len=time_limit)
    assert env.ctx.variables["n_total_starts"] == N_DD1
    assert env.ctx.variables["n_service_starts"] == N_DD1
    assert env.ctx.variables["n_deteriorated"] == DD1_N_DETERIORATED
    assert model.blocks["out"].disposed_entities == N_DD1
    pop_sys = env.ctx.agent_system.by_name["Patients"]
    assert pop_sys.live == len(env.ctx.live_entities) == 0   # all disposed by t=50
