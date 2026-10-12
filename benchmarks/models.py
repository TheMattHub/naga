"""The models every library runs, and what queueing theory says about each.

All four are open Jackson networks: customers arrive at station 0 in a Poisson stream,
every service time is exponential, every queue is first come first served and unlimited,
and a customer leaving a station goes to another one with a fixed probability or leaves
the system. That one shape covers a single queue, a multi-server queue, a line and a job
shop, and its mean time in system is known exactly.
"""
from math import factorial

ARRIVAL_RATE = 1.0

# A station is (servers, service rate of one server, {next station: probability}); the
# probability left over is that of leaving the system.
MODELS = {
    "mm1": ("M/M/1 queue", [(1, 1.25, {})]),
    "mm3": ("M/M/3 queue", [(3, 0.4, {})]),
    "tandem": ("Line of 3 stations", [(1, 1.25, {1: 1.0}), (1, 1.5, {2: 1.0}), (1, 2.0, {})]),
    # The engine's bundled job shop: cutting, then drilling or assembly, 20% rework.
    "jackson": ("Job shop, 3 stations with rework",
                [(1, 1.65, {1: 0.6, 2: 0.4}), (2, 0.5, {2: 0.5}), (2, 0.55, {0: 0.2})]),
}


def visit_rates(stations) -> list[float]:
    """Arrivals per time unit at each station: the traffic equations, by iteration."""
    rates = [0.0] * len(stations)
    for _ in range(1000):
        rates = [(ARRIVAL_RATE if j == 0 else 0.0)
                 + sum(rates[i] * to.get(j, 0.0) for i, (_c, _mu, to) in enumerate(stations))
                 for j in range(len(stations))]
    return rates


def mean_time_in_system(stations) -> float:
    """Jackson's theorem: each station is an M/M/c queue at its visit rate; the mean
    number in the system is the sum over stations, and Little's law gives the time."""
    number = 0.0
    for (servers, rate, _to), arrivals in zip(stations, visit_rates(stations)):
        load = arrivals / rate
        busy = load / servers
        assert busy < 1, "unstable station"
        last = load ** servers / (factorial(servers) * (1 - busy))
        wait = last / (sum(load ** k / factorial(k) for k in range(servers)) + last)  # Erlang C
        number += wait * busy / (1 - busy) + load
    return number / ARRIVAL_RATE
