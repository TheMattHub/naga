"""A machine with a fixed cycle time (M/D/1): the mean wait before a job starts.

Derivation. Poisson arrivals at rate lam, one server, a general service time S. The
Pollaczek-Khinchine formula gives the mean wait in queue Wq = lam E[S^2] / (2 (1 - rho)),
rho = lam E[S]. With a constant service s, E[S^2] = s^2: Wq = 0.2 * 9 / (2 * 0.4) = 2.25 minutes
(an exponential service of the same mean would wait twice as long).

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "Bottles reach a labelling machine at random, on average one every 5 minutes, with "
    "exponentially distributed gaps. The machine labels one bottle at a time and always takes "
    "exactly 3 minutes. Bottles queue in arrival order. Simulate 100,000 minutes, discard the "
    "first 2,000, and tell me how long a bottle waits on average before its labelling starts "
    "(the wait only, not the labelling), in minutes.")
LAM, SERVICE = 1 / 5, 3.0
RUN, WARMUP = 100_000, 2_000
ANSWER = LAM * SERVICE ** 2 / (2 * (1 - LAM * SERVICE))
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Labeller", "capacity": 1}],
        "blocks": [{"name": "arrive", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "label", "type": "PROCESS",
                    "params": {"resource_name": "Labeller", "distribution": "Constant", "dist_params": {"value": SERVICE}}},
                   {"name": "leave", "type": "DISPOSE"}],
        "links": [{"source": "arrive", "target": "label"},
               {"source": "label", "target": "leave"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


def naga_answer(results):
    return results[-1]["resources"]["Labeller"]["Time in Queue Avg"]["mean"]


def simulate(seed=0):
    rng, env, waits = random.Random(seed), simpy.Environment(), []
    machine = simpy.Resource(env, capacity=1)

    def bottle():
        arrived = env.now
        with machine.request() as turn:
            yield turn
            if arrived >= WARMUP:
                waits.append(env.now - arrived)
            yield env.timeout(SERVICE)

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(bottle())

    env.process(arrivals())
    env.run(until=RUN)
    return sum(waits) / len(waits)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
