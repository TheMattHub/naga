"""A car park with ten spaces and no queue (an Erlang loss system): the share turned away.

Derivation. Poisson arrivals at rate lam, c = 10 spaces, a stay of mean s, a car that finds the
park full drives on. The number of parked cars is a birth-death chain whose stationary law is
the Poisson law of mean a = lam s = 7.5 cut off at c, and a Poisson arrival sees that law, so
the share turned away is Erlang's B formula, B(c, a) = (a^c / c!) / sum_{k<=c} a^k / k!,
computed below by its recursion B(n) = a B(n-1) / (n + a B(n-1)), B(0) = 1.

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "A small car park has 10 spaces. Cars arrive at random, on average one every 2 minutes "
    "(exponential gaps), and a car that parks stays 15 minutes on average, exponentially "
    "distributed. There is nowhere to wait: a car that finds all 10 spaces taken drives away "
    "and does not come back. Simulate 200,000 minutes and tell me what percentage of the "
    "arriving cars are turned away.")
LAM, STAY, SPACES = 1 / 2, 15.0, 10
RUN = 200_000


def _erlang_b(servers, load):
    blocked = 1.0
    for n in range(1, servers + 1):
        blocked = load * blocked / (n + load * blocked)
    return blocked


ANSWER = 100 * _erlang_b(SPACES, LAM * STAY)
TOLERANCE = None

NAGA = [
    ("upsert_record", {"section": "resources", "record": {"name": "Spaces", "capacity": SPACES}}),
    ("add_block", {"type": "CREATE", "name": "cars",
                   "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}}),
    ("add_block", {"type": "PROCESS", "name": "park",
                   "params": {"resource_name": "Spaces", "balk_if": f'NR("Spaces") >= {SPACES}',
                              "distribution": "Exponential", "dist_params": {"rate": 1 / STAY}}}),
    ("add_block", {"type": "DISPOSE", "name": "leave"}),
    ("add_block", {"type": "DISPOSE", "name": "turned_away"}),
    ("connect", {"source": "cars", "target": "park"}),
    ("connect", {"source": "park", "target": "leave"}),
    ("connect", {"source": "park", "target": "turned_away"}),    # branch 1: left without service
    ("set_simulation_settings", {"run_length": RUN, "warmup": 0, "replications": 1,
                                 "time_unit": "minutes"}),
    ("validate_model", {}),
    ("run_model", {}),
    ("get_kpi_table", {"frame": "queue_summary"}),
]


def naga_answer(results):
    created = results[-2]["system"]["Total Entities Created"]["mean"]
    return 100 * results[-1]["rows"][0]["Balked"] / created


def simulate(seed=0):
    rng, env, cars = random.Random(seed), simpy.Environment(), [0, 0]
    spaces = simpy.Resource(env, capacity=SPACES)

    def car():
        cars[0] += 1
        if spaces.count == SPACES:
            cars[1] += 1
            return
        with spaces.request() as space:
            yield space
            yield env.timeout(rng.expovariate(1 / STAY))

    def arrivals():
        while True:
            yield env.timeout(rng.expovariate(LAM))
            env.process(car())

    env.process(arrivals())
    env.run(until=RUN)
    return 100 * cars[1] / cars[0]


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
