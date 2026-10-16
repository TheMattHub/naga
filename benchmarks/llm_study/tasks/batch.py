"""Trays of five through a tunnel oven (batching): the mean time of an item.

Derivation. Items arrive in a Poisson stream at rate lam and leave for the oven as soon as k = 5
are waiting. The j-th item of a tray waits for the k - j arrivals after it, each gap of mean
1 / lam, so the mean wait over the tray is (k - 1) / (2 lam) = 4 / (2 * 0.5) = 4 minutes. The
tunnel takes any number of trays at once, so nothing queues there: every item adds exactly 3
minutes. W = 4 + 3 = 7 minutes.

Tolerance: statistical (None), see `study.py calibrate`.
"""
import random
import sys

import simpy

REQUEST = (
    "Pastries come off a line at random, on average one every 2 minutes (exponential gaps). They "
    "are collected on a tray; as soon as a tray holds 5 pastries it goes into a tunnel oven and a "
    "new tray is started. The tunnel is long enough that any number of trays can be inside at the "
    "same time, and every tray takes exactly 3 minutes to pass through. Simulate 40,000 minutes, "
    "discard the first 1,000, and give me the average time a pastry takes from coming off the "
    "line to leaving the oven, in minutes.")
LAM, TRAY, TUNNEL = 1 / 2, 5, 3.0
RUN, WARMUP = 40_000, 1_000
ANSWER = (TRAY - 1) / (2 * LAM) + TUNNEL
TOLERANCE = None

NAGA = [
    ("write_model", {"layout": {
        "blocks": [{"name": "pastries", "type": "CREATE", "params": {"distribution": "Exponential", "dist_params": {"rate": LAM}}},
                   {"name": "tray", "type": "BATCH", "params": {"batch_size": TRAY}},
                   {"name": "tunnel", "type": "DELAY", "params": {"distribution": "Constant", "dist_params": {"value": TUNNEL}}},
                   {"name": "unload", "type": "SEPARATE"},
                   {"name": "baked", "type": "RECORD",
                    "params": {"stat_name": "pastry time", "kind": "tally",
                               "expr": "TNOW - entity.creation_time"}},
                   {"name": "out", "type": "DISPOSE"}],
        "links": [{"source": "pastries", "target": "tray"},
               {"source": "tray", "target": "tunnel"},
               {"source": "tunnel", "target": "unload"},
               {"source": "unload", "target": "baked"},
               {"source": "baked", "target": "out"}],
        "simulation": {"time_limit": RUN, "warmup": WARMUP, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=40_000, warmup=1_000, replications=1)
pastries = m.create(distribution=nb.Exponential(rate=1 / 2))
tray = m.batch(batch_size=5)
tunnel = m.delay(distribution=nb.Constant(value=3.0))
baked = m.record(stat_name="pastry time", kind="tally", expr="TNOW - entity.creation_time")
pastries >> tray >> tunnel >> m.separate() >> baked >> m.dispose()
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['user/pastry time']:.4f}")
"""


def naga_answer(results):
    return results[-1]["user_stats"]["pastry time"]["mean"]


def simulate(seed=0):
    rng, env, times, tray = random.Random(seed), simpy.Environment(), [], []

    def bake(arrivals):
        yield env.timeout(TUNNEL)
        times.extend(env.now - arrived for arrived in arrivals if arrived >= WARMUP)

    def line():
        nonlocal tray
        while True:
            yield env.timeout(rng.expovariate(LAM))
            tray.append(env.now)
            if len(tray) == TRAY:
                env.process(bake(tray))
                tray = []

    env.process(line())
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 0):.4f}")
