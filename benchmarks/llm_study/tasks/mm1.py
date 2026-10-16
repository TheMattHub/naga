"""One clerk, one queue (M/M/1): the mean time a customer spends in the shop.

Derivation. Poisson arrivals at rate lam, exponential service at rate mu, one server, first come
first served. The number in the system is geometric with mean rho / (1 - rho), rho = lam / mu;
Little's law divides it by lam: W = 1 / (mu - lam) = 1 / (1/4 - 1/6) = 12 minutes.

Tolerance: statistical (None), five standard deviations of this file's own estimate at the
requested run length, measured by `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "Customers walk into a small shop at random, on average one every 6 minutes. A single clerk "
    "serves them one at a time, in the order they arrive. A service takes 4 minutes on average; "
    "both the service times and the gaps between arrivals are exponentially distributed. "
    "Simulate 120,000 minutes, leave out the first 2,000 as warm-up, and tell me the average "
    "time a customer spends in the shop, waiting plus being served, in minutes.")
LAM, MU = 1 / 6, 1 / 4
RUN, WARMUP = 120_000, 2_000
ANSWER = 1 / (MU - LAM)
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Clerk", "capacity": 1}],
        "blocks": [{"name": "arrive", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "serve", "type": "PROCESS",
                    "params": {"resource_name": "Clerk", "distribution": "Exponential", "dist_params": {"rate": MU}}},
                   {"name": "leave", "type": "DISPOSE"}],
        "links": [{"source": "arrive", "target": "serve"},
               {"source": "serve", "target": "leave"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=120_000, warmup=2_000, replications=1)
m.add_resource(name="Clerk")
arrive = m.create(distribution=nb.Exponential(rate=1 / 6))
serve = m.process(resource_name="Clerk", distribution=nb.Exponential(rate=1 / 4))
arrive >> serve >> m.dispose()
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['system/Avg Throughput Time']:.4f}")
"""


def naga_answer(results):
    return results[-1]["system"]["Avg Throughput Time"]["mean"]


def simulate(seed=0):
    rng, env, times = random.Random(seed), simpy.Environment(), []
    clerk = simpy.Resource(env, capacity=1)

    def customer():
        arrived = env.now
        with clerk.request() as turn:
            yield turn
            yield env.timeout(rng.expovariate(MU))
        if arrived >= WARMUP:
            times.append(env.now - arrived)

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(customer())

    env.process(arrivals())
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
