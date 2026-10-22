"""Reference models: bundled models whose answer is known exactly, reproduced by a run.

The cases, their closed forms and the page `docs/validation/reference-models.md` are
`tools/gen_engine_docs.py`'s (`REFERENCE_CASES`). Each test here

1. derives the case's exact value a SECOND way, sharing no formula with the generator
   (the Markov chain summed state by state, or the differential equation
   integrated with a small step), and compares the two;
2. repeats the page's run (same seed, replications and run length) and asserts the exact
   value is inside the run's 95% Student-t interval over replications, or, for a model
   with no randomness, that the run gives the exact value to 1e-4;
3. asserts the page's line for the case is what that run gives.

Confidence level: 95% per case. Six cases have an interval, so over arbitrary seeds 0.3
of them would miss by chance (at least one in about one seed set in four). The seed is
fixed (0, the page's), so the outcome is deterministic and at seed 0 none misses. A case
that starts to fail is a possible ENGINE BUG and is investigated as one: the interval is
never widened and the seed never changed to make it pass. `MAX_HALF_WIDTH` keeps the
intervals narrow enough to mean something.

Every parameter is read from the model file. The formulas are standard; see Gross and
Harris, "Fundamentals of Queueing Theory" (queues, Jackson networks), Sterman, "Business
Dynamics" (logistic and Bass models) and Keeling and Rohani, "Modeling Infectious Diseases
in Humans and Animals" (SIR). No figure is taken from them.
"""
import importlib
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
gen = importlib.import_module("tools.gen_engine_docs")

PAGE = gen.DOCS / "validation" / "reference-models.md"
CASES = {case[0]: case for case in gen.REFERENCE_CASES}
MAX_HALF_WIDTH = 0.08       # of the exact value: a wider interval would prove little


def _check(name: str, second_derivation: float, rel: float = 1e-6) -> None:
    case = CASES[name]
    result = exact, mean, half, replications = gen.reference_result(case)
    assert exact == pytest.approx(second_derivation, rel=rel), "the closed form is wrong"
    if replications > 1:
        assert half <= MAX_HALF_WIDTH * abs(exact), f"{name}: interval +-{half:.3g} is too wide"
        assert abs(exact - mean) <= half, (
            f"{name}: exact {exact:.6g} is outside {mean:.6g} +- {half:.3g} "
            f"({replications} replications, seed {gen.REFERENCE_SEED}): investigate the engine")
    else:
        assert mean == pytest.approx(exact, rel=1e-4), f"{name}: the run gives {mean!r}"
    assert gen.reference_row(case, result) in PAGE.read_text(encoding="utf-8").splitlines(), \
        f"{name}: docs/validation/reference-models.md is out of date: run `python tools/gen_engine_docs.py`"


def _blocks(model: str) -> tuple[dict, dict]:
    layout = gen._layout(model)
    return layout, {block["type"]: block["params"] for block in layout["blocks"]}


def _birth_death_lq(lam: float, mu: float, servers: int, states: int = 5000) -> float:
    """Mean number waiting from the chain itself: balance across the cut between n and
    n + 1 gives p[n + 1] = p[n] lam / (mu min(n + 1, servers)); normalise; sum (n - c) p[n]."""
    p, total, waiting = 1.0, 0.0, 0.0
    for n in range(states):
        total += p
        waiting += max(n - servers, 0) * p
        p *= lam / (mu * min(n + 1, servers))
    return waiting / total


def _rk4(rate, y: list, t_end: float, dt: float, watch: int = 0) -> tuple[list, float]:
    """(y(t_end), the largest y[watch] met on the way) of dy/dt = rate(y), classical RK4."""
    peak = y[watch]
    for _ in range(round(t_end / dt)):
        k1 = rate(y)
        k2 = rate([a + dt / 2 * b for a, b in zip(y, k1)])
        k3 = rate([a + dt / 2 * b for a, b in zip(y, k2)])
        k4 = rate([a + dt * b for a, b in zip(y, k3)])
        y = [a + dt / 6 * (b + 2 * c + 2 * d + e) for a, b, c, d, e in zip(y, k1, k2, k3, k4)]
        peak = max(peak, y[watch])
    return y, peak


# -- discrete-event -------------------------------------------------------------

@pytest.mark.slow
def test_mm1_mean_number_waiting():
    """M/M/1, Poisson arrivals at lam, exponential service at mu, rho = lam / mu < 1.

    The number in system is a birth-death chain with p[n] = (1 - rho) rho^n. The number
    waiting is n - 1 for n >= 1, so Lq = sum_{n>=1} (n - 1)(1 - rho) rho^n
    = rho^2 (1 - rho) sum_{m>=0} m rho^(m-1) = rho^2 (1 - rho) / (1 - rho)^2
    = rho^2 / (1 - rho). KPI: the time-average queue length of the server, after a
    warm-up several relaxation times long. Second derivation: the chain, summed."""
    _layout, blocks = _blocks("single_server")
    _check("M/M/1 queue", _birth_death_lq(blocks["CREATE"]["dist_params"]["rate"],
                                          blocks["PROCESS"]["dist_params"]["rate"], 1))


@pytest.mark.slow
def test_mm2_mean_number_waiting_is_erlang_c():
    """M/M/c with c = 2 (two tellers taken as one set), a = lam / mu, rho = a / c.

    Balance gives p[n] = p0 a^n / n! for n <= c and p[n] = p[c] rho^(n-c) beyond, so
    1 / p0 = sum_{n<c} a^n / n! + (a^c / c!) / (1 - rho), and
    Lq = sum_{n>c} (n - c) p[n] = p[c] rho / (1 - rho)^2 = p0 a^c rho / (c! (1 - rho)^2)
    (the Erlang C formula times rho / (1 - rho)). Second derivation: the chain, summed."""
    layout, blocks = _blocks("two_teller_set")
    (tellers,) = layout["resource_sets"]
    _check("M/M/2 queue (Erlang C)",
           _birth_death_lq(blocks["CREATE"]["dist_params"]["rate"],
                           blocks["PROCESS"]["dist_params"]["rate"], len(tellers["members"])))


@pytest.mark.slow
def test_mg1_mean_number_waiting_is_pollaczek_khinchine():
    """M/G/1: Poisson arrivals at lam, service time S of any distribution, rho = lam E[S].

    An arrival (who sees time averages, arrivals being Poisson) waits for the customer in
    service to finish and for those queued ahead: Wq = rho E[residual] + Lq E[S], the mean
    residual service time seen in service being E[S^2] / (2 E[S]). With Lq = lam Wq
    (Little): Wq = lam E[S^2] / 2 + rho Wq, so Lq = lam^2 E[S^2] / (2 (1 - rho)).
    Here S is uniform on [low, high]: E[S] = (low + high) / 2,
    E[S^2] = (low^2 + low high + high^2) / 3.

    Second derivation, with no residual-time argument: the number left behind by a
    departure is a Markov chain, n -> max(n - 1, 0) + (arrivals during one service), whose
    stationary distribution is also the time-stationary one. Its transition matrix is
    built from a[k] = E[exp(-lam S) (lam S)^k / k!], integrated numerically, and solved."""
    _layout, blocks = _blocks("single_server_general")
    lam = blocks["CREATE"]["dist_params"]["rate"]
    low, high = blocks["PROCESS"]["dist_params"]["low"], blocks["PROCESS"]["dist_params"]["high"]
    states, s = 200, np.linspace(low, high, 4001)
    term, a = np.exp(-lam * s), []
    for k in range(states + 1):
        a.append(float(np.sum((term[1:] + term[:-1]) / 2) * (s[1] - s[0]) / (high - low)))
        term = term * lam * s / (k + 1)
    transition = np.zeros((states, states))
    for n in range(states):
        first = max(n - 1, 0)
        transition[n, first:] = a[:states - first]
        transition[n, -1] += 1.0 - transition[n].sum()       # the truncated tail
    balance = transition.T - np.eye(states)
    balance[-1] = 1.0                                        # replace one equation by sum = 1
    p = np.linalg.solve(balance, np.eye(states)[-1])
    in_system = float(p @ np.arange(states))
    _check("M/G/1 queue (Pollaczek-Khinchine)", in_system - lam * (low + high) / 2)


@pytest.mark.slow
def test_open_jackson_network_mean_time_in_the_shop():
    """Three stations with chance routing and a rework loop back to the first.

    Flow conservation gives each station's total arrival rate: lam_i = gamma_i +
    sum_j lam_j p_ji (external arrivals plus what the others send). Jackson's theorem: the
    stationary distribution is the product of independent M/M/c queues at those rates,
    feedback or not. So L_i = Lq_i + lam_i / mu_i (waiting plus in service), and Little's
    law on the whole shop gives the mean time in it: W = sum_i L_i / gamma, gamma the
    external arrival rate. KPI: the mean time from creation to disposal.

    Second derivation: the traffic equations by repeated substitution (not the linear
    solve), each station from its own chain, summed."""
    gamma, routing, service = gen.open_network(gen._layout("job_shop_jackson"))
    assert len(service) == 3 and len(gamma) == 1
    lam = dict.fromkeys(service, 0.0)
    for _ in range(500):
        lam = {i: gamma.get(i, 0.0) + sum(lam[j] * routing[j].get(i, 0.0) for j in service)
               for i in service}
    in_shop = sum(_birth_death_lq(lam[i], mu, c) + lam[i] / mu for i, (mu, c) in service.items())
    _check("Open Jackson network", in_shop / sum(gamma.values()))


# -- system dynamics ------------------------------------------------------------

def test_logistic_growth_follows_its_closed_form():
    """dP/dt = r P (1 - P / K). Substituting u = 1 / P gives the linear du/dt = r (1 / K - u),
    so u(t) = 1 / K + (1 / P0 - 1 / K) exp(-r t) and P(t) = K / (1 + (K / P0 - 1) exp(-r t)).
    KPI: the stock at the end of a run to t = 25, mid-curve. Second derivation: the
    equation integrated here with a step fifty times smaller than the model's."""
    layout = gen._layout("sd_population")
    r, k = gen._aux(layout, "SD growth rate"), gen._aux(layout, "SD carrying capacity")
    (stock,) = layout["levels"]
    (end,), _peak = _rk4(lambda y: [r * y[0] * (1 - y[0] / k)], [float(stock["initial"])], 25, 0.002)
    _check("Logistic growth", end)


def test_bass_diffusion_follows_its_closed_form():
    """dA/dt = (p + q A / m)(m - A), A(0) = 0. With F = A / m: dF/dt = (p + q F)(1 - F), and
    partial fractions give ln((p + q F) / (1 - F)) = (p + q) t + ln p, so
    F(t) = (1 - exp(-(p + q) t)) / (1 + (q / p) exp(-(p + q) t)). KPI: adopters at the end
    of a run to t = 10. Second derivation: the equation integrated here with a small step."""
    layout = gen._layout("sd_bass_diffusion")
    m, p, q = (gen._aux(layout, f"SD {name}") for name in ("market potential", "innovation coeff",
                                                             "imitation coeff"))
    (end,), _peak = _rk4(lambda y: [(p + q * y[0] / m) * (m - y[0])], [0.0], 10, 0.001)
    _check("Bass diffusion", end)


def test_sir_epidemic_peaks_where_its_first_integral_says():
    """dS/dt = -beta S I / N, dI/dt = beta S I / N - gamma I. Dividing, dI/dS = -1 + rho / S
    with rho = gamma N / beta, so I + S - rho ln S is constant along the epidemic. I peaks
    where dI/dt = 0, at S = rho; there I_max = I0 + S0 - rho - rho ln(S0 / rho), which with
    I0 + S0 = N is N - rho (1 + ln(S0 / rho)). KPI: the largest value the Infected stock
    takes. Second derivation: the two equations integrated here, the largest I kept."""
    layout = gen._layout("sd_sir")
    n, beta, gamma = (gen._aux(layout, f"SD {name}") for name in ("population", "beta", "gamma"))
    i0 = float(gen._named(layout["levels"], "SD Infected")["initial"])
    _end, peak = _rk4(lambda y: [-beta * y[0] * y[1] / n, beta * y[0] * y[1] / n - gamma * y[1]],
                      [n - i0, i0], layout["simulation"]["time_limit"], 0.005, watch=1)
    _check("SIR epidemic", peak)


# -- agent-based ----------------------------------------------------------------

def test_an_agent_alternating_idle_and_busy_is_busy_its_renewal_share_of_the_time():
    """One agent, Idle for an exponential time of rate lam, then Busy for one of rate mu.

    A cycle is one idle and one busy period; by the renewal-reward theorem the long-run
    share of time busy is E[busy] / E[cycle] = (1 / mu) / (1 / lam + 1 / mu) = lam / (lam + mu).
    KPI: the time-average count of agents in Busy. The agent starts Idle, which biases a
    finite run by about -(share) / ((lam + mu) T), under 1e-4 here and far inside the interval.
    Second derivation: the two-state chain's balance equations, solved."""
    rates = gen._layout("agents_server_utilization")["agent_types"][0]["parameters"]
    lam, mu = rates["arrival_rate"], rates["service_rate"]
    _idle, busy = np.linalg.solve(np.array([[lam, -mu], [1.0, 1.0]]), np.array([0.0, 1.0]))
    _check("Busy/idle cycle", float(busy))


@pytest.mark.slow
def test_an_epidemic_of_agents_infects_the_exact_mean_number_of_its_markov_chain():
    """N agents; a susceptible is infected at rate beta I / N, an infected recovers at gamma.

    The counts (S, I) are a Markov chain: infections happen at total rate beta S I / N and
    recoveries at gamma I, so the next event is an infection with probability
    q(S) = beta S / (beta S + gamma N) (I cancels) and a recovery otherwise, and the
    epidemic ends when I reaches 0. The mean of N - S at that moment is a finite sum over
    the chain's paths: exact for this N, not a large-population limit. KPI: the Recovered
    count at t = 300, by when no agent is infected. The generator sums level by level in S
    with a linear filter; second derivation here: the probability pushed state by state.

    The limit for large N is the cross-check: along the SIR equations I + S - rho ln S is
    constant (rho = gamma N / beta, see the system-dynamics test), which at I = 0 gives
    S_inf = S0 exp(-(beta / (gamma N)) (N - S_inf)). The exact mean sits a few agents
    below it (order 1, against a spread of order sqrt(N))."""
    layout = gen._layout("agents_sir_agents")
    rates, (people,) = layout["agent_types"][0]["parameters"], layout["populations"]
    n, beta, gamma = people["count"], rates["beta"], rates["gamma"]
    i0 = sum(row["count"] for row in people["initial"])
    level, mean_s, ended = [0.0] * (n + 2), 0.0, 0.0
    level[i0] = 1.0
    for s in range(n - i0, -1, -1):
        q, below = beta * s / (beta * s + gamma * n), [0.0] * (n + 2)
        for i in range(n - s, 0, -1):
            below[i + 1] += level[i] * q
            level[i - 1] += level[i] * (1 - q)
        mean_s += s * level[0]
        ended += level[0]
        level = below
    assert ended == pytest.approx(1.0, abs=1e-9)
    s_inf = n - i0
    for _ in range(200):
        s_inf = (n - i0) * math.exp(-beta / (gamma * n) * (n - s_inf))
    assert 0 < (n - s_inf) - (n - mean_s) < 0.005 * n
    _check("SIR epidemic of agents", n - mean_s)


# -- rate-based -----------------------------------------------------------------

def _tank_by_small_steps() -> tuple[float, float]:
    """(average level, spilled) of the tank model, by stepping it in thousandths of a
    minute and clipping at the bounds: no crossing time is computed."""
    layout = gen._layout("tank_fill_and_drain")
    (tank,), (pump,) = layout["levels"], layout["timeseries_inputs"]
    draw = float(gen._named(layout["flows"], "draw")["rate"])
    level, area, spilled, dt = float(tank["initial"]), 0.0, 0.0, 0.001
    for step in range(round(layout["simulation"]["time_limit"] / dt)):
        fill = pump["values"][min(int(step * dt // pump["step"]), len(pump["values"]) - 1)]
        wanted = level + (fill - draw) * dt
        spilled += max(wanted - tank["capacity"], 0.0)
        new = min(max(wanted, tank["min"]), tank["capacity"])
        area += (level + new) / 2 * dt
        level = new
    return area / layout["simulation"]["time_limit"], spilled


def test_tank_average_level_is_the_area_under_three_straight_lines():
    """Capacity C, start L0, fill rate f until the pump stops at t1, draw rate d < f, run T.

    The level rises at f - d and is full at t_f = (C - L0) / (f - d) (before t1); it stays
    full until t1; it then falls at d and is empty at t_e = t1 + C / d (before T). The area
    under the level is (L0 + C) / 2 t_f + C (t1 - t_f) + C / 2 (t_e - t1), and the average
    is that over T. The engine computes the two crossing instants in closed form (no time
    step), so the run must give the value to rounding. Second derivation: small steps."""
    _check("Tank, average level", _tank_by_small_steps()[0], rel=1e-3)


def test_tank_overflow_is_the_surplus_while_full():
    """While the tank is full and the pump runs (t_f to t1, see above) the surplus f - d
    cannot enter: spilled = (f - d) (t1 - t_f). Second derivation: small steps."""
    _check("Tank, overflow", _tank_by_small_steps()[1], rel=1e-3)


# -- the page ---------------------------------------------------------------------

def test_the_page_around_its_table_is_the_generators_and_shows_every_case(tmp_path):
    """The table's lines are checked case by case above (each against its own run); here
    the rest of the page, and that the table holds those lines and no other."""
    committed = PAGE.read_text(encoding="utf-8")
    rows = [line for line in committed.splitlines() if line.startswith("| ")][1:]
    assert [row.split(" | ")[0][2:] for row in rows] == list(CASES)
    gen._write(tmp_path, PAGE.name, [gen.reference_page(rows)])
    assert (tmp_path / PAGE.name).read_text(encoding="utf-8") == committed, \
        "docs/validation/reference-models.md is out of date: run `python tools/gen_engine_docs.py`"
    assert {case[1] for case in gen.REFERENCE_CASES} == {"Discrete-event", "System dynamics",
                                                         "Agent-based", "Rate-based"}
    assert all(f"naga run {case[2]}" in committed for case in gen.REFERENCE_CASES)
