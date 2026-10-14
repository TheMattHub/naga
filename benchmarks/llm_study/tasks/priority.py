"""Urgent jobs go first (a non-preemptive priority M/M/1 queue): the mean time of an urgent job.

Derivation. Two Poisson classes, urgent at rate lam1 and routine at rate lam2, one server, the
same exponential service of mean s for both, urgent first but a job in service is never
interrupted. An arriving job finds on average W0 = (lam1 + lam2) E[S^2] / 2 = (lam1 + lam2) s^2
of unfinished service at the server (E[S^2] = 2 s^2), and an urgent one also waits for the
urgent jobs ahead of it: Wq1 = W0 / (1 - rho1), rho1 = lam1 s (Cobham). Its time in the shop is
Wq1 + s = 2.8 / 0.8 + 4 = 7.5 minutes. (First come first served would give 13.33.)

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "One technician repairs devices. Urgent repairs come in at random, on average one every 20 "
    "minutes, and routine repairs on average one every 8 minutes; both arrive with exponential "
    "gaps. Any repair takes 4 minutes on average, exponentially distributed. The technician "
    "always takes a waiting urgent repair before any routine one, but never drops a repair "
    "already started. Simulate 200,000 minutes, discard the first 5,000, and tell me the "
    "average time an urgent repair spends in the workshop, waiting plus repair, in minutes.")
URGENT, ROUTINE, SERVICE = 1 / 20, 1 / 8, 4.0
RUN, WARMUP = 200_000, 5_000
ANSWER = (URGENT + ROUTINE) * SERVICE ** 2 / (1 - URGENT * SERVICE) + SERVICE
TOLERANCE = None


def _repair(priority):
    return {"resource_name": "Technician", "priority": priority, "distribution": "Exponential",
            "dist_params": {"rate": 1 / SERVICE}}


NAGA = [
    ("write_model", {"layout": {
        "resources": [{"name": "Technician", "capacity": 1}],
        "blocks": [{"name": "urgent", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": URGENT}}},
                   {"name": "routine", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": ROUTINE}}},
                   {"name": "repair_urgent", "type": "PROCESS", "params": _repair(0)},
                   {"name": "repair_routine", "type": "PROCESS", "params": _repair(1)},
                   {"name": "urgent_done", "type": "RECORD",
                    "params": {"stat_name": "urgent time", "kind": "tally",
                               "expr": "TNOW - entity.creation_time"}},
                   {"name": "leave", "type": "DISPOSE"}],
        "links": [{"source": "urgent", "target": "repair_urgent"},
               {"source": "repair_urgent", "target": "urgent_done"},
               {"source": "urgent_done", "target": "leave"},
               {"source": "routine", "target": "repair_routine"},
               {"source": "repair_routine", "target": "leave"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


def naga_answer(results):
    return results[-1]["user_stats"]["urgent time"]["mean"]


def simulate(seed=0):
    rng, env, times = random.Random(seed), simpy.Environment(), []
    technician = simpy.PriorityResource(env, capacity=1)

    def repair(priority):
        arrived = env.now
        with technician.request(priority=priority) as turn:
            yield turn
            yield env.timeout(rng.expovariate(1 / SERVICE))
        if priority == 0 and arrived >= WARMUP:
            times.append(env.now - arrived)

    def arrivals(rate, priority):
        while True:
            yield env.timeout(rng.expovariate(rate))
            env.process(repair(priority))

    env.process(arrivals(URGENT, 0))
    env.process(arrivals(ROUTINE, 1))
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
