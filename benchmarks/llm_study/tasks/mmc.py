"""Three agents, one queue (M/M/3): the mean wait before a call is answered.

Derivation. Poisson arrivals at rate lam, c = 3 servers each exponential at rate mu, offered load
a = lam / mu = 2.25, rho = a / c = 0.75. The probability that an arrival waits is Erlang's C
formula, C = (a^c / c!) / ((1 - rho) * sum_{k<c} a^k / k! + a^c / c!); the wait of those who wait
is exponential at rate c mu - lam, so Wq = C / (c mu - lam). Both are computed below.

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys
from math import factorial

import simpy

REQUEST = (
    "A help line gets a call every 2 minutes on average, at random. Three agents answer; a call "
    "takes 4.5 minutes on average to handle. Callers wait in one line and are answered in the "
    "order they called. Assume exponential times between calls and exponential handling times. "
    "Simulate 200,000 minutes, leave out the first 1,000, and give me the average time a caller "
    "waits before an agent picks up, in minutes.")
LAM, MU, SERVERS = 1 / 2, 1 / 4.5, 3
RUN, WARMUP = 200_000, 1_000
_A = LAM / MU
_TOP = _A ** SERVERS / factorial(SERVERS)
_C = _TOP / ((1 - _A / SERVERS) * sum(_A ** k / factorial(k) for k in range(SERVERS)) + _TOP)
ANSWER = _C / (SERVERS * MU - LAM)
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Agents", "capacity": SERVERS}],
        "blocks": [{"name": "calls", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "handle", "type": "PROCESS",
                    "params": {"resource_name": "Agents", "distribution": "Exponential", "dist_params": {"rate": MU}}},
                   {"name": "done", "type": "DISPOSE"}],
        "links": [{"source": "calls", "target": "handle"},
               {"source": "handle", "target": "done"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


def naga_answer(results):
    return results[-1]["resources"]["Agents"]["Time in Queue Avg"]["mean"]


def simulate(seed=0):
    rng, env, waits = random.Random(seed), simpy.Environment(), []
    agents = simpy.Resource(env, capacity=SERVERS)

    def call():
        called = env.now
        with agents.request() as turn:
            yield turn
            if called >= WARMUP:
                waits.append(env.now - called)
            yield env.timeout(rng.expovariate(MU))

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(call())

    env.process(arrivals())
    env.run(until=RUN)
    return sum(waits) / len(waits)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
