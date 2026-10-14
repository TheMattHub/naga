"""Machining and inspection with rework (a Jackson network): the mean time a job is in the shop.

Derivation. Jobs arrive at rate lam; a fraction p of inspected jobs goes back to machining, so
each job is machined and inspected 1 / (1 - p) times on average and both stations see the rate
lam / (1 - p) = 0.125 a minute. In a Jackson network every station behaves as an M/M/1 queue at
its own total rate, with rho_i / (1 - rho_i) jobs on average: rho = 0.625 at machining (1.667
jobs) and 0.5 at inspection (1 job). Little's law over the whole shop divides the jobs in it by
the outside arrival rate: W = (1.667 + 1) / 0.1 = 26.67 minutes.

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "Jobs reach a workshop at random, one every 10 minutes on average (exponential gaps). Every "
    "job is machined (5 minutes on average) and then inspected (4 minutes on average); both "
    "times are exponential. There is one machine and one inspector, each handling one job at a "
    "time, first come first served. One inspection in five fails: a failed job goes back to the "
    "machine queue and is machined and inspected again, and can fail again with the same chance. "
    "Simulate 200,000 minutes, discard the first 5,000, and give me the average time a job "
    "spends in the workshop from arrival until it passes inspection, in minutes.")
LAM, MEANS, FAIL = 1 / 10, (5.0, 4.0), 0.2
RUN, WARMUP = 200_000, 5_000
_RHO = [LAM / (1 - FAIL) * mean for mean in MEANS]
ANSWER = sum(rho / (1 - rho) for rho in _RHO) / LAM
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Machine", "capacity": 1}, {"name": "Inspector", "capacity": 1}],
        "blocks": [{"name": "arrive", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "machine", "type": "PROCESS",
                    "params": {"resource_name": "Machine", "distribution": "Exponential", "dist_params": {"rate": 1 / MEANS[0]}}},
                   {"name": "inspect", "type": "PROCESS",
                    "params": {"resource_name": "Inspector", "distribution": "Exponential", "dist_params": {"rate": 1 / MEANS[1]}}},
                   {"name": "passed", "type": "DECIDE", "params": {"mode": "Chance", "probs": [FAIL]}},
                   {"name": "leave", "type": "DISPOSE"}],
        # a DECIDE's links are its branches: branch 0 (the inspection failed), then the else
        "links": [{"source": "arrive", "target": "machine"},
               {"source": "machine", "target": "inspect"},
               {"source": "inspect", "target": "passed"},
               {"source": "passed", "target": "machine"},
               {"source": "passed", "target": "leave"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


def naga_answer(results):
    return results[-1]["system"]["Avg Throughput Time"]["mean"]


def simulate(seed=0):
    rng, env, times = random.Random(seed), simpy.Environment(), []
    stations = [simpy.Resource(env, capacity=1) for _ in MEANS]

    def job():
        arrived = env.now
        while True:
            for station, mean in zip(stations, MEANS):
                with station.request() as turn:
                    yield turn
                    yield env.timeout(rng.expovariate(1 / mean))
            if rng.random() >= FAIL:
                break
        if arrived >= WARMUP:
            times.append(env.now - arrived)

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(job())

    env.process(arrivals())
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
