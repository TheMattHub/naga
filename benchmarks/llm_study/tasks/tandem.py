"""Two machines in a row (tandem M/M/1 queues): the mean time a part spends in the line.

Derivation. Poisson arrivals at rate lam; the departures of an M/M/1 queue are again Poisson at
rate lam (Burke), so each station is an M/M/1 queue of its own and the times add:
W = 1 / (mu1 - lam) + 1 / (mu2 - lam) = 1 / (1/3 - 1/5) + 1 / (1/3.5 - 1/5) = 7.5 + 11.67.

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "Parts arrive at a small line at random, one every 5 minutes on average (exponential gaps). "
    "Each part is first cut on a saw, which takes 3 minutes on average, and then drilled, which "
    "takes 3.5 minutes on average; both times are exponentially distributed. There is one saw "
    "and one drill, each works on one part at a time, and parts wait in front of each in arrival "
    "order with no limit on space. Simulate 100,000 minutes, discard the first 2,000, and report "
    "the average time a part spends in the line from arrival to leaving the drill, in minutes.")
LAM, MEANS = 1 / 5, (3.0, 3.5)
RUN, WARMUP = 100_000, 2_000
ANSWER = sum(1 / (1 / mean - LAM) for mean in MEANS)
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Saw", "capacity": 1}, {"name": "Drill", "capacity": 1}],
        "blocks": [{"name": "arrive", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "cut", "type": "PROCESS",
                    "params": {"resource_name": "Saw", "distribution": "Exponential", "dist_params": {"rate": 1 / MEANS[0]}}},
                   {"name": "drill", "type": "PROCESS",
                    "params": {"resource_name": "Drill", "distribution": "Exponential", "dist_params": {"rate": 1 / MEANS[1]}}},
                   {"name": "leave", "type": "DISPOSE"}],
        "links": [{"source": "arrive", "target": "cut"},
               {"source": "cut", "target": "drill"},
               {"source": "drill", "target": "leave"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=100_000, warmup=2_000, replications=1)
m.add_resource(name="Saw")
m.add_resource(name="Drill")
arrive = m.create(distribution=nb.Exponential(rate=1 / 5))
cut = m.process(resource_name="Saw", distribution=nb.Exponential(rate=1 / 3))
drill = m.process(resource_name="Drill", distribution=nb.Exponential(rate=1 / 3.5))
arrive >> cut >> drill >> m.dispose()
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['system/Avg Throughput Time']:.4f}")
"""


def naga_answer(results):
    return results[-1]["system"]["Avg Throughput Time"]["mean"]


def simulate(seed=0):
    rng, env, times = random.Random(seed), simpy.Environment(), []
    machines = [simpy.Resource(env, capacity=1) for _ in MEANS]

    def part():
        arrived = env.now
        for machine, mean in zip(machines, MEANS):
            with machine.request() as turn:
                yield turn
                yield env.timeout(rng.expovariate(1 / mean))
        if arrived >= WARMUP:
            times.append(env.now - arrived)

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(part())

    env.process(arrivals())
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
