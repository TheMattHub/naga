"""Twenty machines that fail and are repaired (agents with two states): how many run on average.

Derivation. Each machine alternates between running (exponential, mean up) and broken
(exponential, mean down) on its own, since there is a technician for every breakdown. The share
of time it runs is up / (up + down) = 50 / 55, so 20 * 50 / 55 = 18.18 machines run on average.

Tolerance, from theory. With a = 1 / up, b = 1 / down and p = up / (up + down), the indicator
"running" of one machine has autocovariance p (1 - p) exp(-(a + b) t); integrating it twice,
the variance of its time average over a long run T is 2 p (1 - p) / ((a + b) T). The machines
are independent, so the standard deviation of the average number running is
sqrt(n * 2 p (1 - p) / ((a + b) T)) = 0.055 over 5,000 hours; the tolerance is five of them.
(All machines start running, which moves the average by n (1 - p) / ((a + b) T) = 0.002.)
"""
import random
import sys

import simpy

REQUEST = (
    "A plant has 20 identical machines. Each one runs for 50 hours on average before it breaks "
    "down, and a repair takes 5 hours on average; both times are exponentially distributed. "
    "There are enough technicians that a broken machine never waits for one, and a repaired "
    "machine starts running again at once. All machines are running at the start. Simulate "
    "5,000 hours and tell me the average number of machines running over that time.")
MACHINES, UP, DOWN, RUN = 20, 50.0, 5.0, 5_000
_P = UP / (UP + DOWN)
ANSWER = MACHINES * _P
TOLERANCE = 5 * (MACHINES * 2 * _P * (1 - _P) / ((1 / UP + 1 / DOWN) * RUN)) ** 0.5

NAGA = [
    ("write_model", {"layout": {
        "agent_types": [{"name": "Machine", "initial_state": "Running",
                         "states": [{"name": "Running"}, {"name": "Broken"}],
                         "transitions": [
                             {"from": "Running", "to": "Broken", "timeout": f"EXPO({UP})"},
                             {"from": "Broken", "to": "Running", "timeout": f"EXPO({DOWN})"}]}],
        "populations": [{"name": "Machines", "type": "Machine", "count": MACHINES}],
        "simulation": {"time_limit": RUN, "warmup": 0, "replications": 1, "time_unit": "hours"}}}),
    ("run_model", {}),
    ("get_kpi_table", {"frame": "agent_summary"}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=5_000, warmup=0, replications=1, time_unit="hours")
m.add_agent_type(name="Machine", initial_state="Running",
                 states=[{"name": "Running"}, {"name": "Broken"}],
                 transitions=[{"from": "Running", "to": "Broken", "timeout": "EXPO(50)"},
                              {"from": "Broken", "to": "Running", "timeout": "EXPO(5)"}])
m.add_population(name="Machines", type="Machine", count=20)
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['agent/Machines/Running/Avg Count']:.4f}")
"""


def naga_answer(results):
    return next(row["Avg Count"] for row in results[-1]["rows"] if row["State"] == "Running")


def simulate(seed=0):
    rng, env, running = random.Random(seed), simpy.Environment(), [0.0]

    def machine():
        while True:
            up = rng.expovariate(1 / UP)
            running[0] += min(up, RUN - env.now)        # a stretch the end of the run cuts off
            yield env.timeout(up)
            yield env.timeout(rng.expovariate(1 / DOWN))

    for _ in range(MACHINES):
        env.process(machine())
    env.run(until=RUN)
    return running[0] / RUN


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
